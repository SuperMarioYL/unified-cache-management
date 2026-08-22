"""Task 4A1 RED contracts for deterministic CandidateSelection."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
FIXTURE_PATH = RELEASE_ROOT / "tests" / "fixtures" / "task4-candidate-input.json"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
capabilities = importlib.import_module("ucm_release.capabilities")
cli = importlib.import_module("ucm_release.cli")
core = importlib.import_module("ucm_release.core")
products = importlib.import_module("ucm_release.products")

CATALOG_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "upstream_reads",
    "builder_sync",
    "builder_capabilities",
    "builder_revisions",
    "runtime_candidates",
    "bindings",
    "entries",
    "exclusions",
    "catalog_sha256",
}
PYTHON_COORDINATE_FIELDS = {
    "python_tag",
    "interpreter_path",
    "expected_soabi",
    "expected_wheel_tag",
}
AUTHORITY_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "toolchain_sha256",
    "recipes",
    "authority_sha256",
}
AUTHORITY_RECIPE_FIELDS = {
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
}
CANDIDATE_SELECTION_FIELDS = {
    "kind",
    "schema_version",
    "route",
    "source_sha",
    "ucm_version",
    "release_tag",
    "config_sha256",
    "catalog_sha256",
    "current_builder_authority_sha256",
    "baseline_manifest_sha256",
    "builder_capabilities",
    "builder_revisions",
    "runtime_candidates",
    "bindings",
    "baseline_selections",
    "discovered_selections",
    "exclusions",
    "blockers",
    "dependency_requests",
    "selection_sha256",
}
DISCOVERED_SELECTION_FIELDS = {
    "product_id",
    "builder_capability_id",
    "builder_revision_id",
    "runtime_id",
}
DEPENDENCY_REQUEST_FIELDS = {"request_id", "coordinate", "requirements"}
PYTHON_REQUEST_COORDINATE_FIELDS = {
    "python_tag",
    "python_abi",
    "cpu_architecture",
    "manylinux",
}
REQUIREMENT_FIELDS = {"requirement_id", "scope", "name", "version"}
SELECTION_EXCLUSION_FIELDS = {
    "reason_code",
    "product_id",
    "builder_capability_id",
    "builder_revision_id",
    "runtime_id",
    "evidence",
}
BLOCKER_FIELDS = {
    "reason_code",
    "admission_key",
    "dependency_request_id",
    "affected_coordinate",
    "evidence",
}
SELECTED_EVIDENCE_FIELDS = {
    "builder_capabilities",
    "builder_revisions",
    "runtime_candidates",
    "bindings",
}


def _fixture() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _reseal(value: dict[str, Any], field: str) -> None:
    projection = copy.deepcopy(value)
    projection.pop(field, None)
    value[field] = _canonical_digest(projection)


def _require_public_callable(module: object, name: str) -> Callable[..., Any]:
    function = getattr(module, name, None)
    assert callable(
        function
    ), f"required public API {module.__name__}.{name} is missing"
    return function


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )
    assert function is not None, f"required function {name} is missing from {path.name}"
    return function


def _call_target(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        parent = _call_target(function.value)
        return f"{parent}.{function.attr}" if parent else function.attr
    return None


class _DirectCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        if _call_target(node.func) is not None:
            self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _direct_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Call]:
    visitor = _DirectCallVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return visitor.calls


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".", 1)[0]
                aliases[local] = item.name if item.asname else local
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            for item in node.names:
                local = item.asname or item.name
                aliases[local] = ".".join(part for part in (prefix, item.name) if part)
    return aliases


def _resolve_imported_target(target: str, aliases: dict[str, str]) -> str:
    head, separator, tail = target.partition(".")
    imported = aliases.get(head)
    if imported is None:
        return target
    return imported + (separator + tail if separator else "")


def _reachable_calls(tree: ast.Module, entrypoint: str) -> list[tuple[str, ast.Call]]:
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert entrypoint in functions, f"required public seam {entrypoint} is missing"
    aliases = _import_aliases(tree)
    pending = [entrypoint]
    visited: set[str] = set()
    reachable: list[tuple[str, ast.Call]] = []
    while pending:
        function_name = pending.pop()
        if function_name in visited:
            continue
        visited.add(function_name)
        for call in _direct_calls(functions[function_name]):
            target = _call_target(call.func)
            assert target is not None
            if target in functions:
                pending.append(target)
                reachable.append((target, call))
            else:
                reachable.append((_resolve_imported_target(target, aliases), call))
    return reachable


def _reachable_call_targets(tree: ast.Module, entrypoint: str) -> set[str]:
    return {target for target, _ in _reachable_calls(tree, entrypoint)}


def _is_direct_network_call(target: str) -> bool:
    network_roots = (
        "aiohttp",
        "http.client",
        "httpx",
        "requests",
        "socket",
        "urllib.request",
        "urllib3",
    )
    return any(
        target == root or target.startswith(root + ".") for root in network_roots
    )


def _literal_subprocess_tokens(call: ast.Call) -> tuple[str, ...] | None:
    argument = (
        call.args[0]
        if call.args
        else next((item.value for item in call.keywords if item.arg == "args"), None)
    )
    if argument is None:
        return None
    try:
        value = ast.literal_eval(argument)
    except (SyntaxError, ValueError, TypeError):
        return None
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        return tuple(value)
    return None


def _is_network_command_tokens(tokens: tuple[str, ...], depth: int = 0) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable in {"curl", "wget", "pip", "pip3"} or executable.startswith("pip3."):
        return True
    if executable == "gh" and len(tokens) > 1 and tokens[1] == "api":
        return True
    if executable == "python" or executable.startswith("python3"):
        if any(
            tokens[index : index + 2] == ("-m", "pip")
            for index in range(1, len(tokens) - 1)
        ):
            return True
    if executable in {"bash", "dash", "sh"} and depth < 4:
        for index, option in enumerate(tokens[1:], start=1):
            if option.startswith("-") and "c" in option.lstrip("-"):
                if index + 1 >= len(tokens):
                    return False
                try:
                    nested = tuple(shlex.split(tokens[index + 1]))
                except ValueError:
                    return False
                return _is_network_command_tokens(nested, depth + 1)
    return False


def _is_network_subprocess(call: ast.Call) -> bool:
    tokens = _literal_subprocess_tokens(call)
    return tokens is not None and _is_network_command_tokens(tokens)


def _is_network_behavior(target: str, call: ast.Call) -> bool:
    if _is_direct_network_call(target):
        return True
    subprocess_calls = {
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
    }
    return target in subprocess_calls and _is_network_subprocess(call)


def _assemble_catalog(fixture: dict[str, Any]) -> dict[str, Any]:
    catalog = capabilities.assemble_capability_catalog(
        builder_discovery=fixture["builder_discovery"],
        runtime_discovery=fixture["runtime_discovery"],
        python_probes=fixture["python_probes"],
        mooncake_probes=fixture["mooncake_probes"],
        python_requires=fixture["python_requires"],
    )
    return capabilities.validate_capability_catalog(catalog)


def _selected_evidence_from_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    binding = copy.deepcopy(catalog["bindings"][0])
    capability = next(
        copy.deepcopy(item)
        for item in catalog["builder_capabilities"]
        if item["builder_capability_id"] == binding["builder_capability_id"]
    )
    revision = next(
        copy.deepcopy(item)
        for item in catalog["builder_revisions"]
        if item["builder_revision_id"] == binding["builder_revision_id"]
    )
    runtime = next(
        copy.deepcopy(item)
        for item in catalog["runtime_candidates"]
        if item["runtime_id"] == binding["runtime_id"]
    )
    return {
        "builder_capabilities": [capability],
        "builder_revisions": [revision],
        "runtime_candidates": [runtime],
        "bindings": [binding],
    }


def _write_repository(root: Path, fixture: dict[str, Any]) -> None:
    for relative, contents in fixture["repository_files"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents.encode("utf-8"))


def _authority_recipe_paths(fixture: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        item["recipe_path"]
        for item in fixture["expected_current_builder_authority"]["recipes"]
    )


def _task4_config() -> dict[str, Any]:
    config = core.load_catalog(version_override="0.8.0rc1")
    by_runtime_product = {
        item["runtime_product"]: item for item in config["upstream_products"]
    }
    by_runtime_product["vllm"]["runtime_tag_selectors"] = [
        "v{version}",
        "v{version}-cu{runtime.major_minor.compact}",
    ]
    by_runtime_product["vllm-ascend"]["runtime_tag_selectors"] = [
        "v{version}",
        "v{version}-{variant}",
    ]
    return config


def _schema_ready_active_release() -> dict[str, Any]:
    raw = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8"))
    resolved = core.resolve_owner_templates(
        raw, repository="example/unified-cache-management"
    )
    assert isinstance(resolved, dict)
    return resolved


def _selection(
    fixture: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    authority: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    catalog = _assemble_catalog(fixture)
    release = config or _task4_config()
    prepare = _require_public_callable(products, "prepare_candidate_selection")
    selection = prepare(
        release,
        catalog,
        authority or copy.deepcopy(fixture["expected_current_builder_authority"]),
        route="pr",
        source_sha=fixture["source_sha"],
        baseline_manifest=None,
    )
    assert isinstance(selection, dict)
    return selection, catalog, release


def _selected_runtime_tags(
    selection: dict[str, Any],
    catalog: dict[str, Any],
    *,
    product_id: str | None = None,
) -> set[str]:
    selected_ids = {item["runtime_id"] for item in selection["discovered_selections"]}
    return {
        item["runtime_tag"]
        for item in catalog["runtime_candidates"]
        if item["runtime_id"] in selected_ids
        and (product_id is None or item["product_id"] == product_id)
    }


def _coordinate_from_capability(capability: dict[str, Any]) -> dict[str, str]:
    python_tag = "cp" + capability["python_version"].replace(".", "")
    return {
        "python_tag": python_tag,
        "python_abi": capability["python_abi"],
        "cpu_architecture": capability["cpu_architecture"],
        "manylinux": capability["manylinux"],
    }


def _requirements(config: dict[str, Any]) -> list[dict[str, str]]:
    records = []
    for scope in ("build", "runtime"):
        for name, version in config["dependencies"][scope].items():
            identity = {"scope": scope, "name": name, "version": version}
            records.append({"requirement_id": _canonical_digest(identity), **identity})
    return sorted(records, key=lambda item: item["requirement_id"])


def _request(capability: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    projection = {
        "coordinate": _coordinate_from_capability(capability),
        "requirements": _requirements(config),
    }
    return {"request_id": _canonical_digest(projection), **projection}


def _future_abi(fixture: dict[str, Any]) -> None:
    probe = copy.deepcopy(fixture["python_probes"]["probes"][0])
    probe.update(
        interpreter_path="/opt/python/cp316-cp316t/bin/python",
        python_version="3.16",
        python_abi="cp316t",
        soabi="cpython-316t-x86_64-linux-gnu",
        wheel_tag="cp316-cp316t-manylinux_2_28_x86_64",
    )
    fixture["python_probes"]["probes"].append(probe)


def _second_current_revision(fixture: dict[str, Any]) -> None:
    current = fixture["builder_discovery"]["builder_facts"][0]
    historical = fixture["builder_discovery"]["builder_facts"][1]
    for field in ("recipe_source_commit", "recipe_sha256", "toolchain_sha256"):
        historical[field] = current[field]
    identity = {
        field: historical[field] for field in capabilities.BUILDER_FACT_IDENTITY_FIELDS
    }
    old_id = historical["builder_fact_id"]
    historical["builder_fact_id"] = _canonical_digest(identity)
    probe = next(
        item
        for item in fixture["python_probes"]["probes"]
        if item["builder_fact_id"] == old_id
    )
    probe["builder_fact_id"] = historical["builder_fact_id"]


def _add_selector_group(fixture: dict[str, Any], dimension: str) -> None:
    suffix = {"family": "a", "variant": "b", "architecture": "c"}[dimension]
    fact = copy.deepcopy(fixture["builder_discovery"]["builder_facts"][0])
    runtime = copy.deepcopy(
        next(
            item
            for item in fixture["runtime_discovery"]["runtime_candidates"]
            if item["runtime_image_tag"] == "v0.21.1"
        )
    )
    if dimension == "family":
        fact["accelerator_runtime"] = "cuda-12.9"
        runtime["accelerator_runtime"] = "cuda-12.9.1"
        runtime["runtime_image_tag"] = "v0.21.1-cu129"
        runtime["git_commit"] = suffix * 40
        fixture["runtime_discovery"]["upstream_reads"].append(
            {
                "project": "vllm-project/vllm",
                "source_kind": "runtime-tag",
                "source_path": runtime["runtime_image_tag"],
                "source_commit": runtime["git_commit"],
                "fact": "runtime-image",
            }
        )
    elif dimension == "variant":
        fact["variant"] = "future5"
        runtime["variant"] = "future5"
    else:
        fact["cpu_architecture"] = "arm64"
        runtime["cpu_architecture"] = "arm64"
    fact["target_tag"] = f"cuda13.0-current-{suffix}"
    fact["target_builder_digest"] = "sha256:" + suffix * 64
    fact_identity = {
        field: fact[field] for field in capabilities.BUILDER_FACT_IDENTITY_FIELDS
    }
    fact["builder_fact_id"] = _canonical_digest(fact_identity)
    fixture["builder_discovery"]["builder_facts"].append(fact)

    probe = copy.deepcopy(fixture["python_probes"]["probes"][0])
    probe["builder_fact_id"] = fact["builder_fact_id"]
    probe["target_builder_digest"] = fact["target_builder_digest"]
    probe["builder_image"] = (
        f'{fact["target_repository"]}@{fact["target_builder_digest"]}'
    )
    probe["cpu_architecture"] = fact["cpu_architecture"]
    if fact["cpu_architecture"] == "arm64":
        probe["runner"] = "ubuntu-24.04-arm"
        probe["soabi"] = "cpython-314t-aarch64-linux-gnu"
        probe["wheel_tag"] = "cp314-cp314t-manylinux_2_28_aarch64"
    fixture["python_probes"]["probes"].append(probe)

    runtime["runtime_image_digest"] = "sha256:" + suffix * 64
    fixture["runtime_discovery"]["runtime_candidates"].append(runtime)


def _add_ascend_selector_group(
    fixture: dict[str, Any],
    *,
    variant: str,
    runtimes: tuple[tuple[str, str, str], ...],
) -> None:
    authority = fixture["expected_current_builder_authority"]
    recipe = next(
        item
        for item in authority["recipes"]
        if fixture["repository_files"][item["recipe_path"]].lstrip().startswith("FROM ")
    )
    source_path = f".github/workflows/dockerfiles/Dockerfile.buildwheel.{variant}"
    fixture["builder_discovery"]["upstream_reads"].append(
        {
            "project": "vllm-project/vllm-ascend",
            "source_kind": "planner-checkout-recipe",
            "source_path": recipe["recipe_path"],
            "source_commit": fixture["source_sha"],
            "fact": "recipe",
        }
    )
    fixture["builder_discovery"]["builders"].append(
        {
            "variant": variant,
            "source_kind": "buildwheel-dockerfile",
            "source_path": source_path,
        }
    )

    for index, (runtime_version, runtime_tag, mooncake_version) in enumerate(runtimes):
        seed = f"{variant}:{runtime_version}:{runtime_tag}:{mooncake_version}"
        digest = "sha256:" + hashlib.sha256(seed.encode()).hexdigest()
        source_commit = hashlib.sha256(
            f"commit:{variant}:{runtime_version}".encode()
        ).hexdigest()[:40]
        mooncake_source_path = f"docker/Dockerfile.runtime.{variant}.{index}"
        raw_runtime = {
            "product_id": "vllm-ascend",
            "runtime_version": runtime_version,
            "channel": "rc",
            "variant": variant,
            "cpu_architecture": "amd64",
            "accelerator": "ascend",
            "accelerator_runtime": "cann-9.0",
            "mooncake_version": mooncake_version,
            "mooncake_source_path": mooncake_source_path,
            "runtime_image_repository": "quay.io/ascend/vllm-ascend",
            "runtime_image_tag": runtime_tag,
            "runtime_image_digest": digest,
            "git_tag": f"v{runtime_version}",
            "git_commit": source_commit,
        }
        runtime_identity = {
            "product_id": raw_runtime["product_id"],
            "runtime_repository": raw_runtime["runtime_image_repository"],
            "runtime_tag": raw_runtime["runtime_image_tag"],
            "variant": raw_runtime["variant"],
            "cpu_architecture": raw_runtime["cpu_architecture"],
        }
        runtime_id = _canonical_digest(runtime_identity)
        runtime_image = f'{raw_runtime["runtime_image_repository"]}@{digest}'
        fixture["runtime_discovery"]["runtime_candidates"].append(raw_runtime)
        fixture["runtime_discovery"]["upstream_reads"].append(
            {
                "project": "vllm-project/vllm-ascend",
                "source_kind": "runtime-dockerfile-and-annotated-tag",
                "source_path": mooncake_source_path,
                "source_commit": source_commit,
                "fact": "MOONCAKE_TAG",
            }
        )
        fixture["mooncake_probes"]["probes"].append(
            {
                "runtime_image_digest": digest,
                "cpu_architecture": "amd64",
                "runner": "ubuntu-24.04",
                "declared_version": mooncake_version,
                "installed_version": mooncake_version,
                "headers_path": "/usr/local/include/transfer_engine.h",
                "libraries_path": "/usr/local/lib",
            }
        )

        target_digest = (
            "sha256:" + hashlib.sha256(f"builder:{seed}".encode()).hexdigest()
        )
        fact = {
            "builder_fact_id": "",
            "project": "vllm-project/vllm-ascend",
            "accelerator": "ascend",
            "accelerator_runtime": "cann-9.0",
            "variant": variant,
            "cpu_architecture": "amd64",
            "manylinux": "manylinux_2_34",
            "source_kind": "buildwheel-dockerfile",
            "source_path": source_path,
            "source_image_repository": "quay.io/ascend/manylinux",
            "source_image_tag": f"9.0-{variant}-manylinux_2_34",
            "source_image_digest": "sha256:"
            + hashlib.sha256(f"source:{seed}".encode()).hexdigest(),
            "recipe_path": recipe["recipe_path"],
            "recipe_source_commit": recipe["recipe_source_commit"],
            "recipe_sha256": recipe["recipe_sha256"],
            "toolchain_sha256": authority["toolchain_sha256"],
            "target_repository": "ghcr.io/release-org/ucm-builder-vllm-ascend",
            "target_tag": f"cann9.0-{variant}-current-{index}",
            "target_builder_digest": target_digest,
            "mooncake_source_runtime_id": runtime_id,
            "mooncake_source_runtime_image": runtime_image,
            "mooncake_version": mooncake_version,
        }
        fact_identity = {
            field: fact[field] for field in capabilities.BUILDER_FACT_IDENTITY_FIELDS
        }
        fact["builder_fact_id"] = _canonical_digest(fact_identity)
        fixture["builder_discovery"]["builder_facts"].append(fact)
        fixture["python_probes"]["probes"].append(
            {
                "builder_fact_id": fact["builder_fact_id"],
                "builder_image": (
                    f'{fact["target_repository"]}@{fact["target_builder_digest"]}'
                ),
                "target_builder_digest": target_digest,
                "cpu_architecture": "amd64",
                "manylinux": "manylinux_2_34",
                "runner": "ubuntu-24.04",
                "interpreter_path": "/opt/python/cp314-cp314t/bin/python",
                "python_version": "3.14",
                "python_abi": "cp314t",
                "soabi": "cpython-314t-x86_64-linux-gnu",
                "wheel_tag": "cp314-cp314t-manylinux_2_34_x86_64",
            }
        )


def test_task4_a1_fixture_is_raw_separate_and_self_consistent() -> None:
    fixture = _fixture()
    authority = fixture["expected_current_builder_authority"]
    catalog = _assemble_catalog(fixture)

    assert fixture["kind"] == "task4-candidate-selection-raw-fixture"
    assert fixture["source_sha"] == fixture["builder_discovery"]["source_sha"]
    assert fixture["source_sha"] == fixture["runtime_discovery"]["source_sha"]
    assert _authority_recipe_paths(fixture) == tuple(
        sorted(_authority_recipe_paths(fixture))
    )
    assert authority["authority_sha256"] == _canonical_digest(
        {key: value for key, value in authority.items() if key != "authority_sha256"}
    )
    for recipe in authority["recipes"]:
        raw = fixture["repository_files"][recipe["recipe_path"]].encode("utf-8")
        assert recipe["recipe_sha256"] == ("sha256:" + hashlib.sha256(raw).hexdigest())
    for fact in fixture["builder_discovery"]["builder_facts"]:
        identity = {
            field: fact[field] for field in capabilities.BUILDER_FACT_IDENTITY_FIELDS
        }
        assert fact["builder_fact_id"] == _canonical_digest(identity)
    assert set(catalog) == CATALOG_FIELDS
    assert catalog["catalog_sha256"] == _canonical_digest(
        {key: value for key, value in catalog.items() if key != "catalog_sha256"}
    )
    cp314t = [
        item
        for item in catalog["builder_capabilities"]
        if item["python_abi"] == "cp314t"
    ]
    assert cp314t
    cp314t_ids = {item["builder_capability_id"] for item in cp314t}
    cp314t_bindings = [
        item
        for item in catalog["bindings"]
        if item["builder_capability_id"] in cp314t_ids
    ]
    assert cp314t_bindings
    assert all(item["python_abi"] == "cp314t" for item in cp314t_bindings)
    assert "capability-catalog-discovery.json" not in str(FIXTURE_PATH)


def test_selected_capability_evidence_has_one_public_semantic_validator() -> None:
    evidence = _selected_evidence_from_catalog(_assemble_catalog(_fixture()))
    validate = _require_public_callable(
        capabilities, "validate_selected_capability_evidence"
    )

    assert set(evidence) == SELECTED_EVIDENCE_FIELDS
    assert validate(copy.deepcopy(evidence)) == evidence


@pytest.mark.parametrize(
    ("validated_fields", "expected"),
    [
        pytest.param(
            {
                "python_version": "3.12",
                "python_abi": "cp312",
                "cpu_architecture": "amd64",
                "manylinux": "manylinux_2_28",
            },
            {
                "python_tag": "cp312",
                "interpreter_path": "/opt/python/cp312-cp312/bin/python",
                "expected_soabi": "cpython-312-x86_64-linux-gnu",
                "expected_wheel_tag": "cp312-cp312-manylinux_2_28_x86_64",
            },
            id="ordinary",
        ),
        pytest.param(
            {
                "python_version": "3.14",
                "python_abi": "cp314t",
                "cpu_architecture": "amd64",
                "manylinux": "manylinux_2_28",
            },
            {
                "python_tag": "cp314",
                "interpreter_path": "/opt/python/cp314-cp314t/bin/python",
                "expected_soabi": "cpython-314t-x86_64-linux-gnu",
                "expected_wheel_tag": "cp314-cp314t-manylinux_2_28_x86_64",
            },
            id="cp314t",
        ),
        pytest.param(
            {
                "python_version": "3.16",
                "python_abi": "cp316t",
                "cpu_architecture": "arm64",
                "manylinux": "manylinux_2_31",
            },
            {
                "python_tag": "cp316",
                "interpreter_path": "/opt/python/cp316-cp316t/bin/python",
                "expected_soabi": "cpython-316t-aarch64-linux-gnu",
                "expected_wheel_tag": "cp316-cp316t-manylinux_2_31_aarch64",
            },
            id="future-free-threaded-arm64",
        ),
    ],
)
def test_compile_python_coordinate_is_closed_generic_and_exact(
    validated_fields: dict[str, str], expected: dict[str, str]
) -> None:
    compile_coordinate = _require_public_callable(
        capabilities, "compile_python_coordinate"
    )
    coordinate = compile_coordinate(copy.deepcopy(validated_fields))

    assert set(coordinate) == PYTHON_COORDINATE_FIELDS
    assert coordinate == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("python_version", "3.15", id="version-abi-drift"),
        pytest.param("python_abi", "cp314x", id="malformed-abi"),
        pytest.param("cpu_architecture", "ppc64le", id="unsupported-arch"),
        pytest.param("manylinux", "linux_2_28", id="malformed-manylinux"),
    ],
)
def test_compile_python_coordinate_rejects_malformed_or_drifting_fields(
    field: str, value: object
) -> None:
    compile_coordinate = _require_public_callable(
        capabilities, "compile_python_coordinate"
    )
    validated_fields: dict[str, object] = {
        "python_version": "3.14",
        "python_abi": "cp314t",
        "cpu_architecture": "amd64",
        "manylinux": "manylinux_2_28",
    }
    validated_fields[field] = value
    with pytest.raises(ValueError):
        compile_coordinate(validated_fields)


def test_reachable_network_guard_classifies_literal_subprocess_commands() -> None:
    cases = [
        (
            """
import subprocess as process
def helper():
    return process.run(["/usr/bin/curl", "https://example.invalid"])
def prepare_candidate_selection():
    return helper()
""",
            True,
        ),
        (
            """
from subprocess import Popen as launch
def prepare_candidate_selection():
    return launch("wget https://example.invalid", shell=True)
""",
            True,
        ),
        (
            """
import subprocess
def prepare_candidate_selection():
    return subprocess.run(["/bin/sh", "-c", "curl https://example.invalid"])
""",
            True,
        ),
        (
            """
import subprocess
def prepare_candidate_selection():
    return subprocess.check_call(
        ["bash", "-ceu", "python3 -m pip download example"]
    )
""",
            True,
        ),
        (
            """
import subprocess
def prepare_candidate_selection():
    return subprocess.check_call(["pip3", "download", "example"])
""",
            True,
        ),
        (
            """
from subprocess import check_output
def prepare_candidate_selection():
    return check_output(args=["python3.14", "-I", "-m", "pip", "download"])
""",
            True,
        ),
        (
            """
import subprocess
def prepare_candidate_selection():
    return subprocess.call(["gh", "api", "repos/example/project"])
""",
            True,
        ),
        (
            """
import aiohttp as client
def prepare_candidate_selection():
    return client.request("GET", "https://example.invalid")
""",
            True,
        ),
        (
            """
import subprocess
import urllib.parse
def prepare_candidate_selection():
    subprocess.run(["git", "status"])
    return urllib.parse.urlparse("https://example.invalid")
""",
            False,
        ),
        (
            """
import subprocess
def dead_helper():
    return subprocess.run(["curl", "https://example.invalid"])
def prepare_candidate_selection(command):
    return subprocess.run(command)
""",
            False,
        ),
    ]
    for source, expected in cases:
        calls = _reachable_calls(ast.parse(source), "prepare_candidate_selection")
        assert (
            any(_is_network_behavior(target, call) for target, call in calls)
            is expected
        )


def test_catalog_shape_and_assembly_share_python_coordinate_owner() -> None:
    catalog = _assemble_catalog(_fixture())
    assert set(catalog) == CATALOG_FIELDS

    capability_tree = ast.parse(
        (RELEASE_ROOT / "ucm_release" / "capabilities.py").read_text(encoding="utf-8")
    )
    capability_calls = _reachable_call_targets(
        capability_tree, "assemble_capability_catalog"
    )
    assert any(
        target.rsplit(".", 1)[-1] == "compile_python_coordinate"
        for target in capability_calls
    )
    product_tree = ast.parse(
        (RELEASE_ROOT / "ucm_release" / "products.py").read_text(encoding="utf-8")
    )
    product_calls = _reachable_calls(product_tree, "prepare_candidate_selection")
    assert any(
        target.rsplit(".", 1)[-1] == "compile_python_coordinate"
        for target, _ in product_calls
    )
    assert not any(_is_network_behavior(target, call) for target, call in product_calls)
    assert any(
        target.endswith("builders.validate_current_builder_authority")
        for target, _ in product_calls
    )
    validation_calls = _reachable_call_targets(
        product_tree, "validate_candidate_selection"
    )
    assert any(
        target.endswith("capabilities.validate_selected_capability_evidence")
        for target in validation_calls
    )
    assert any(
        target.rsplit(".", 1)[-1] == "compile_python_coordinate"
        for target in validation_calls
    )
    assert "compile_python_coordinate" not in {
        node.name for node in product_tree.body if isinstance(node, ast.FunctionDef)
    }
    private_authority_policies = {
        node.name
        for node in product_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("_")
        and "authority" in node.name
        and ("validate" in node.name or "policy" in node.name)
    }
    assert private_authority_policies == set()


def test_builder_recipe_paths_are_one_owner_shared_with_discovery() -> None:
    expected_paths = _authority_recipe_paths(_fixture())
    paths = getattr(builders, "CURRENT_BUILDER_RECIPE_PATHS", None)
    assert paths is not None
    assert set(paths) == set(expected_paths)
    assert tuple(sorted(paths)) == expected_paths

    source = RELEASE_ROOT / "ucm_release" / "builders.py"
    for function_name in (
        "discover_builder_sources",
        "freeze_current_builder_authority",
    ):
        function = _function_node(source, function_name)
        assert "CURRENT_BUILDER_RECIPE_PATHS" in {
            node.id for node in ast.walk(function) if isinstance(node, ast.Name)
        }


def test_freeze_current_builder_authority_hashes_owned_raw_files(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    _write_repository(tmp_path, fixture)
    freeze = _require_public_callable(builders, "freeze_current_builder_authority")
    validate = _require_public_callable(builders, "validate_current_builder_authority")
    authority = freeze(source_sha=fixture["source_sha"], repository_root=tmp_path)

    assert set(authority) == AUTHORITY_FIELDS
    assert authority == fixture["expected_current_builder_authority"]
    assert authority["recipes"] == sorted(
        authority["recipes"], key=lambda item: item["recipe_path"]
    )
    assert all(set(item) == AUTHORITY_RECIPE_FIELDS for item in authority["recipes"])
    for recipe in authority["recipes"]:
        expected = (
            "sha256:"
            + hashlib.sha256(
                (tmp_path / recipe["recipe_path"]).read_bytes()
            ).hexdigest()
        )
        assert recipe["recipe_sha256"] == expected
    assert validate(copy.deepcopy(authority)) == authority


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-field",
        "authority-hash-drift",
        "invalid-source",
        "missing-recipe",
        "extra-recipe",
        "recipe-order",
        "recipe-hash-malformed",
        "toolchain-hash-malformed",
        "recipe-source-drift",
    ],
)
def test_current_builder_authority_validator_rejects_noncanonical_objects(
    mutation: str,
) -> None:
    validate = _require_public_callable(builders, "validate_current_builder_authority")
    authority = copy.deepcopy(_fixture()["expected_current_builder_authority"])
    if mutation == "extra-field":
        authority["extra"] = "unknown"
    elif mutation == "authority-hash-drift":
        authority["authority_sha256"] = "sha256:" + "f" * 64
    elif mutation == "invalid-source":
        authority["source_sha"] = "not-a-commit"
        _reseal(authority, "authority_sha256")
    elif mutation == "missing-recipe":
        authority["recipes"].pop()
        _reseal(authority, "authority_sha256")
    elif mutation == "extra-recipe":
        authority["recipes"].append(
            {
                "recipe_path": ".github/release/docker/Dockerfile.extra",
                "recipe_source_commit": authority["source_sha"],
                "recipe_sha256": "sha256:" + "e" * 64,
            }
        )
        _reseal(authority, "authority_sha256")
    elif mutation == "recipe-order":
        authority["recipes"].reverse()
        _reseal(authority, "authority_sha256")
    elif mutation == "recipe-hash-malformed":
        authority["recipes"][0]["recipe_sha256"] = "not-a-digest"
        _reseal(authority, "authority_sha256")
    elif mutation == "toolchain-hash-malformed":
        authority["toolchain_sha256"] = "not-a-digest"
        _reseal(authority, "authority_sha256")
    else:
        authority["recipes"][0]["recipe_source_commit"] = "3" * 40
        _reseal(authority, "authority_sha256")
    with pytest.raises(ValueError):
        validate(authority)


@pytest.mark.parametrize("mode", ["missing-recipe", "missing-toolchain", "escape"])
def test_freeze_current_builder_authority_rejects_missing_or_escaping_sources(
    tmp_path: Path, mode: str
) -> None:
    fixture = _fixture()
    _write_repository(tmp_path, fixture)
    owned = tmp_path / _authority_recipe_paths(fixture)[0]
    if mode == "missing-toolchain":
        recipe_paths = set(_authority_recipe_paths(fixture))
        toolchain_path = next(
            item for item in fixture["repository_files"] if item not in recipe_paths
        )
        (tmp_path / toolchain_path).unlink()
    else:
        owned.unlink()
    if mode == "escape":
        outside = tmp_path.parent / f"{tmp_path.name}-outside-recipe"
        outside.write_bytes(b"outside recipe\n")
        owned.symlink_to(outside)
    freeze = _require_public_callable(builders, "freeze_current_builder_authority")
    with pytest.raises(ValueError):
        freeze(source_sha=fixture["source_sha"], repository_root=tmp_path)


def test_active_schema_v3_declares_exact_pins_and_ordered_selectors() -> None:
    raw = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8"))
    selectors = {
        item["runtime_product"]: item.get("runtime_tag_selectors")
        for item in raw["upstream_products"]
    }
    assert selectors == {
        "vllm": [
            "v{version}",
            "v{version}-cu{runtime.major_minor.compact}",
        ],
        "vllm-ascend": ["v{version}", "v{version}-{variant}"],
    }
    for versions in raw["dependencies"].values():
        for version in versions.values():
            assert isinstance(version, str)
            try:
                parsed = Version(version)
            except InvalidVersion as error:
                raise AssertionError("dependency version is not PEP 440") from error
            assert str(parsed) == version


def test_schema_binds_exact_selector_policy_to_runtime_product() -> None:
    raw = _schema_ready_active_release()
    schema = core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json")
    release_schema = schema["$defs"]["release"]
    core.validate_schema(raw, release_schema, root=schema)

    mutations = (
        (
            "vllm",
            ["v{version}-cu{runtime.major_minor.compact}", "v{version}"],
        ),
        ("vllm", ["v{version}", "v{version}-{variant}"]),
        ("vllm-ascend", ["v{version}-{variant}", "v{version}"]),
        (
            "vllm-ascend",
            ["v{version}", "v{version}-cu{runtime.major_minor.compact}"],
        ),
    )
    for runtime_product, selectors in mutations:
        mutated = copy.deepcopy(raw)
        product = next(
            item
            for item in mutated["upstream_products"]
            if item["runtime_product"] == runtime_product
        )
        product["runtime_tag_selectors"] = selectors
        with pytest.raises(ValueError):
            core.validate_schema(mutated, release_schema, root=schema)


@pytest.mark.parametrize(
    "invalid_pin",
    [
        pytest.param(">=24.2", id="range"),
        pytest.param("24.*", id="wildcard"),
        pytest.param("1.0RC1", id="noncanonical"),
        pytest.param("1!24.2", id="epoch"),
        pytest.param("24.2+local", id="local"),
    ],
)
def test_schema_rejects_nonexact_or_noncanonical_dependency_pins(
    invalid_pin: str,
) -> None:
    raw = _schema_ready_active_release()
    schema = core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json")
    release_schema = schema["$defs"]["release"]
    core.validate_schema(raw, release_schema, root=schema)
    mutated = copy.deepcopy(raw)
    mutated["dependencies"]["build"]["packaging"] = invalid_pin
    with pytest.raises(ValueError):
        core.validate_schema(mutated, release_schema, root=schema)


def test_schema_rejects_noncanonical_dependency_name() -> None:
    raw = _schema_ready_active_release()
    schema = core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json")
    release_schema = schema["$defs"]["release"]
    core.validate_schema(raw, release_schema, root=schema)
    mutated = copy.deepcopy(raw)
    mutated["dependencies"]["runtime"] = {"Wrapt": "1.17.2"}
    with pytest.raises(ValueError):
        core.validate_schema(mutated, release_schema, root=schema)


def test_runtime_tag_selector_compiler_preserves_policy_and_future_values() -> None:
    compile_selectors = _require_public_callable(
        products, "compile_runtime_tag_selectors"
    )
    vllm = compile_selectors(
        ["v{version}", "v{version}-cu{runtime.major_minor.compact}"]
    )
    ascend = compile_selectors(["v{version}", "v{version}-{variant}"])

    values = {
        "version": "1.2.3",
        "variant": "future5",
        "runtime_major_minor_compact": "149",
    }
    assert [selector.render(**values) for selector in vllm] == [
        "v1.2.3",
        "v1.2.3-cu149",
    ]
    assert [selector.render(**values) for selector in ascend] == [
        "v1.2.3",
        "v1.2.3-future5",
    ]


@pytest.mark.parametrize(
    "selectors",
    [
        pytest.param(["v{version}", "v{version}"], id="duplicate"),
        pytest.param(["v{unknown}"], id="unknown-field"),
        pytest.param(["v{version}-{variant.tag_suffix}"], id="old-variant-field"),
        pytest.param(["v{version}/bad"], id="non-oci"),
    ],
)
def test_runtime_tag_selector_compiler_rejects_invalid_contracts(
    selectors: list[str],
) -> None:
    compile_selectors = _require_public_callable(
        products, "compile_runtime_tag_selectors"
    )
    with pytest.raises(ValueError):
        compile_selectors(selectors)


@pytest.mark.parametrize(
    ("runtime_product", "selectors"),
    [
        pytest.param(
            "vllm",
            ["v{version}-cu{runtime.major_minor.compact}", "v{version}"],
            id="vllm-order",
        ),
        pytest.param(
            "vllm",
            ["v{version}", "v{version}-{variant}"],
            id="vllm-policy",
        ),
        pytest.param(
            "vllm-ascend",
            ["v{version}-{variant}", "v{version}"],
            id="ascend-order",
        ),
        pytest.param(
            "vllm-ascend",
            ["v{version}", "v{version}-cu{runtime.major_minor.compact}"],
            id="ascend-policy",
        ),
    ],
)
def test_prepare_selection_rejects_runtime_product_selector_policy_drift(
    runtime_product: str, selectors: list[str]
) -> None:
    config = _task4_config()
    product = next(
        item
        for item in config["upstream_products"]
        if item["runtime_product"] == runtime_product
    )
    product["runtime_tag_selectors"] = selectors
    with pytest.raises(ValueError):
        _selection(_fixture(), config=config)


def test_candidate_selection_is_closed_exact_and_cp314t_request() -> None:
    selection, catalog, config = _selection(_fixture())
    validate = _require_public_callable(products, "validate_candidate_selection")

    assert set(selection) == CANDIDATE_SELECTION_FIELDS
    assert selection["kind"] == "ucm-candidate-selection"
    assert selection["schema_version"] == 3
    assert selection["route"] == "pr"
    assert selection["source_sha"] == catalog["source_sha"]
    assert selection["ucm_version"] == config["ucm_version"]
    assert selection["release_tag"] == config["source"]["release_tag"]
    assert selection["config_sha256"] == _canonical_digest(config)
    assert selection["catalog_sha256"] == catalog["catalog_sha256"]
    assert selection["current_builder_authority_sha256"] == (
        _fixture()["expected_current_builder_authority"]["authority_sha256"]
    )
    assert selection["baseline_manifest_sha256"] is None
    assert selection["baseline_selections"] == []
    assert selection["blockers"] == []
    assert selection["selection_sha256"] == _canonical_digest(
        {key: value for key, value in selection.items() if key != "selection_sha256"}
    )
    assert validate(copy.deepcopy(selection)) == selection

    catalog_arrays = {
        "builder_capabilities": "builder_capability_id",
        "builder_revisions": "builder_revision_id",
        "runtime_candidates": "runtime_id",
    }
    for field, identity in catalog_arrays.items():
        catalog_by_id = {item[identity]: item for item in catalog[field]}
        assert all(item == catalog_by_id[item[identity]] for item in selection[field])
    assert all(item in catalog["bindings"] for item in selection["bindings"])
    assert all(
        set(item) == DISCOVERED_SELECTION_FIELDS
        for item in selection["discovered_selections"]
    )
    assert all(
        set(item) == SELECTION_EXCLUSION_FIELDS for item in selection["exclusions"]
    )
    assert all(set(item) == BLOCKER_FIELDS for item in selection["blockers"])

    assert len(selection["dependency_requests"]) == len(
        {item["request_id"] for item in selection["dependency_requests"]}
    )
    assert selection["dependency_requests"] == sorted(
        selection["dependency_requests"], key=lambda item: item["request_id"]
    )
    for request in selection["dependency_requests"]:
        assert set(request) == DEPENDENCY_REQUEST_FIELDS
        assert set(request["coordinate"]) == PYTHON_REQUEST_COORDINATE_FIELDS
        assert all(set(item) == REQUIREMENT_FIELDS for item in request["requirements"])
        assert request["requirements"] == _requirements(config)
        assert request["request_id"] == _canonical_digest(
            {
                "coordinate": request["coordinate"],
                "requirements": request["requirements"],
            }
        )
    capability = next(
        item
        for item in selection["builder_capabilities"]
        if item["python_abi"] == "cp314t"
    )
    assert _request(capability, config) in selection["dependency_requests"]
    assert _coordinate_from_capability(capability) == {
        "python_tag": "cp314",
        "python_abi": "cp314t",
        "cpu_architecture": "amd64",
        "manylinux": "manylinux_2_28",
    }
    authority = _fixture()["expected_current_builder_authority"]
    recipes = {item["recipe_path"]: item for item in authority["recipes"]}
    for revision in selection["builder_revisions"]:
        recipe = recipes[revision["recipe_path"]]
        assert revision["recipe_source_commit"] == recipe["recipe_source_commit"]
        assert revision["recipe_sha256"] == recipe["recipe_sha256"]
        assert revision["toolchain_sha256"] == authority["toolchain_sha256"]

    selected_tags = _selected_runtime_tags(selection, catalog)
    assert "v0.21.1" in selected_tags
    assert "v0.21.1-cu130" not in selected_tags
    assert "v0.21.1-rocm" not in selected_tags
    assert {"v0.21.1-cu130", "v0.21.1-rocm"} <= {
        item["runtime_tag"] for item in catalog["runtime_candidates"]
    }
    assert not (
        {"wheel_tasks", "image_tasks", "family_tasks", "chart_task", "matrices"}
        & set(selection)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "capability-identity",
        "revision-identity",
        "revision-self-digest",
        "runtime-identity",
        "binding-projection",
        "runtime-compatibility",
        "discovered-product",
    ],
)
def test_candidate_selection_validator_rejects_resealed_evidence_drift(
    mutation: str,
) -> None:
    selection, _, _ = _selection(_fixture())
    validate = _require_public_callable(products, "validate_candidate_selection")
    if mutation == "capability-identity":
        capability = selection["builder_capabilities"][0]
        assert capability["accelerator_runtime"] != "cuda-12.9"
        capability["accelerator_runtime"] = "cuda-12.9"
    elif mutation == "revision-identity":
        revision = selection["builder_revisions"][0]
        drift = "sha256:" + "e" * 64
        assert revision["source_image_digest"] != drift
        revision["source_image_digest"] = drift
    elif mutation == "revision-self-digest":
        revision = selection["builder_revisions"][0]
        drift = "sha256:" + "e" * 64
        assert revision["revision_sha256"] != drift
        revision["revision_sha256"] = drift
    elif mutation == "runtime-identity":
        runtime = selection["runtime_candidates"][0]
        assert runtime["runtime_tag"] != "v9.9.9"
        runtime["runtime_tag"] = "v9.9.9"
    elif mutation == "binding-projection":
        binding = selection["bindings"][0]
        drift = "ghcr.io/example/drift@sha256:" + "e" * 64
        assert binding["source_image"] != drift
        binding["source_image"] = drift
    elif mutation == "runtime-compatibility":
        runtime = selection["runtime_candidates"][0]
        assert runtime["accelerator_runtime"] != "cuda-12.9.1"
        runtime["accelerator_runtime"] = "cuda-12.9.1"
    else:
        discovered = selection["discovered_selections"][0]
        assert discovered["product_id"] != "wrong-product"
        discovered["product_id"] = "wrong-product"
    _reseal(selection, "selection_sha256")
    with pytest.raises(ValueError):
        validate(selection)


@pytest.mark.parametrize(
    ("removed_tags", "expected_tag", "expects_unsupported"),
    [
        pytest.param(set(), "v0.21.1", False, id="preferred-latest"),
        pytest.param(
            {"v0.21.1"},
            "v0.21.1-cu130",
            False,
            id="same-version-second-selector",
        ),
        pytest.param(
            {"v0.21.1", "v0.21.1-cu130"},
            "v0.21.0",
            True,
            id="unsupported-latest-falls-back-older",
        ),
    ],
)
def test_selection_uses_version_buckets_then_declared_selector_order(
    removed_tags: set[str], expected_tag: str, expects_unsupported: bool
) -> None:
    fixture = _fixture()
    fixture["runtime_discovery"]["runtime_candidates"] = [
        item
        for item in fixture["runtime_discovery"]["runtime_candidates"]
        if item["runtime_image_tag"] not in removed_tags
    ]
    selection, catalog, _ = _selection(fixture)

    assert _selected_runtime_tags(selection, catalog) == {expected_tag}
    exclusions = {item["reason_code"] for item in selection["exclusions"]}
    assert ("runtime-flavor-unsupported" in exclusions) is expects_unsupported


@pytest.mark.parametrize(
    ("variant", "runtimes", "expected_tag", "expects_unsupported"),
    [
        pytest.param(
            "a2",
            (
                ("0.22.1rc1", "v0.22.1rc1", "0.3.12"),
                ("0.22.1rc1", "v0.22.1rc1-a2", "0.3.13"),
            ),
            "v0.22.1rc1",
            False,
            id="a2-base-before-variant",
        ),
        pytest.param(
            "a3",
            (("0.22.1rc1", "v0.22.1rc1-a3", "0.3.12"),),
            "v0.22.1rc1-a3",
            False,
            id="a3-variant",
        ),
        pytest.param(
            "a5",
            (("0.22.1rc1", "v0.22.1rc1-a5", "0.3.12"),),
            "v0.22.1rc1-a5",
            False,
            id="future-a5-variant",
        ),
        pytest.param(
            "a4",
            (
                ("0.22.2rc1", "v0.22.2rc1-openeuler", "0.3.13"),
                ("0.22.1rc1", "v0.22.1rc1-a4", "0.3.12"),
            ),
            "v0.22.1rc1-a4",
            True,
            id="unsupported-newest-falls-back-older",
        ),
    ],
)
def test_ascend_selection_uses_catalog_variant_and_ordered_selectors(
    variant: str,
    runtimes: tuple[tuple[str, str, str], ...],
    expected_tag: str,
    expects_unsupported: bool,
) -> None:
    fixture = _fixture()
    _add_ascend_selector_group(fixture, variant=variant, runtimes=runtimes)
    selection, catalog, _ = _selection(fixture)

    ascend_selections = [
        item
        for item in selection["discovered_selections"]
        if item["product_id"] == "vllm-ascend"
    ]
    assert len(ascend_selections) == 1
    selected = ascend_selections[0]
    assert selected == {
        "product_id": "vllm-ascend",
        "builder_capability_id": selected["builder_capability_id"],
        "builder_revision_id": selected["builder_revision_id"],
        "runtime_id": selected["runtime_id"],
    }
    ascend_runtimes = [
        item
        for item in catalog["runtime_candidates"]
        if item["product_id"] == "vllm-ascend"
    ]
    assert {item["runtime_tag"] for item in ascend_runtimes} == {
        item[1] for item in runtimes
    }
    runtime = next(
        item
        for item in selection["runtime_candidates"]
        if item["runtime_id"] == selected["runtime_id"]
    )
    capability = next(
        item
        for item in selection["builder_capabilities"]
        if item["builder_capability_id"] == selected["builder_capability_id"]
    )
    revision = next(
        item
        for item in selection["builder_revisions"]
        if item["builder_revision_id"] == selected["builder_revision_id"]
    )
    selected_bindings = [
        item
        for item in selection["bindings"]
        if item["builder_capability_id"] == selected["builder_capability_id"]
        and item["builder_revision_id"] == selected["builder_revision_id"]
        and item["runtime_id"] == selected["runtime_id"]
    ]
    assert len(selected_bindings) == 1
    binding = selected_bindings[0]
    assert [
        item
        for item in selection["bindings"]
        if item["accelerator"] == "ascend" and item["variant"] == variant
    ] == [binding]
    expected_mooncake = next(
        mooncake_version
        for _, runtime_tag, mooncake_version in runtimes
        if runtime_tag == expected_tag
    )
    assert runtime["runtime_tag"] == expected_tag
    assert _selected_runtime_tags(selection, catalog, product_id="vllm-ascend") == {
        expected_tag
    }
    assert capability["mooncake_version"] == expected_mooncake
    assert runtime["mooncake_version"] == expected_mooncake
    assert binding["mooncake_version"] == expected_mooncake
    assert binding["mooncake_copy_mode"] == "runtime-copy"
    assert binding["runtime_image"] == runtime["runtime_image"]
    assert revision["builder_capability_id"] == capability["builder_capability_id"]
    assert revision["builder_revision_id"] == binding["builder_revision_id"]
    selected_fact = next(
        item
        for item in fixture["builder_discovery"]["builder_facts"]
        if item["target_builder_digest"] == revision["target_builder_digest"]
    )
    assert selected_fact["mooncake_source_runtime_id"] == runtime["runtime_id"]
    assert selected_fact["mooncake_source_runtime_image"] == runtime["runtime_image"]
    assert selected_fact["mooncake_version"] == expected_mooncake

    unselected_runtime_ids = {
        item["runtime_id"]
        for item in ascend_runtimes
        if item["runtime_id"] != runtime["runtime_id"]
    }
    assert not unselected_runtime_ids & {
        item["runtime_id"] for item in selection["runtime_candidates"]
    }
    unselected_bindings = [
        item
        for item in catalog["bindings"]
        if item["runtime_id"] in unselected_runtime_ids
    ]
    if unselected_runtime_ids:
        assert len(unselected_bindings) == len(unselected_runtime_ids)
    assert not {item["builder_capability_id"] for item in unselected_bindings} & {
        item["builder_capability_id"] for item in selection["builder_capabilities"]
    }
    assert not {item["builder_revision_id"] for item in unselected_bindings} & {
        item["builder_revision_id"] for item in selection["builder_revisions"]
    }
    if variant == "a2":
        assert expected_tag == "v0.22.1rc1"
        assert expected_mooncake == "0.3.12"
        assert {item["runtime_tag"] for item in ascend_runtimes} - {expected_tag} == {
            "v0.22.1rc1-a2"
        }
    exclusions = {item["reason_code"] for item in selection["exclusions"]}
    assert ("runtime-flavor-unsupported" in exclusions) is expects_unsupported


@pytest.mark.parametrize("dimension", ["family", "variant", "architecture"])
def test_selection_keeps_family_variant_and_architecture_groups_independent(
    dimension: str,
) -> None:
    fixture = _fixture()
    _add_selector_group(fixture, dimension)
    selection, catalog, _ = _selection(fixture)
    capabilities_by_id = {
        item["builder_capability_id"]: item
        for item in selection["builder_capabilities"]
    }
    runtimes_by_id = {
        item["runtime_id"]: item for item in selection["runtime_candidates"]
    }
    groups = {
        (
            item["product_id"],
            capabilities_by_id[item["builder_capability_id"]]["accelerator_runtime"],
            capabilities_by_id[item["builder_capability_id"]]["variant"],
            capabilities_by_id[item["builder_capability_id"]]["cpu_architecture"],
            runtimes_by_id[item["runtime_id"]]["runtime_tag"],
        )
        for item in selection["discovered_selections"]
    }
    assert (
        "vllm",
        "cuda-13.0",
        "default",
        "amd64",
        "v0.21.1",
    ) in groups
    expected = {
        "family": ("vllm", "cuda-12.9", "default", "amd64", "v0.21.1-cu129"),
        "variant": ("vllm", "cuda-13.0", "future5", "amd64", "v0.21.1"),
        "architecture": ("vllm", "cuda-13.0", "default", "arm64", "v0.21.1"),
    }[dimension]
    assert expected in groups
    assert all(
        item in catalog["runtime_candidates"] for item in runtimes_by_id.values()
    )


def test_selection_does_not_use_array_or_digest_order_as_authority() -> None:
    fixture = _fixture()
    original, original_catalog, _ = _selection(fixture)

    reordered = _fixture()
    for section, field in (
        ("builder_discovery", "upstream_reads"),
        ("builder_discovery", "builder_facts"),
        ("runtime_discovery", "upstream_reads"),
        ("runtime_discovery", "runtime_candidates"),
        ("python_probes", "probes"),
    ):
        reordered[section][field].reverse()
    reordered_selection, _, _ = _selection(reordered)
    assert reordered_selection == original

    digest_flip = _fixture()
    by_tag = {
        item["runtime_image_tag"]: item
        for item in digest_flip["runtime_discovery"]["runtime_candidates"]
    }
    by_tag["v0.21.1"]["runtime_image_digest"] = "sha256:" + "f" * 64
    by_tag["v0.21.1-cu130"]["runtime_image_digest"] = "sha256:" + "0" * 64
    digest_selection, digest_catalog, _ = _selection(digest_flip)
    assert _selected_runtime_tags(original, original_catalog) == {"v0.21.1"}
    assert _selected_runtime_tags(digest_selection, digest_catalog) == {"v0.21.1"}


def test_selection_rejects_same_selector_tie() -> None:
    fixture = _fixture()
    duplicate = copy.deepcopy(
        next(
            item
            for item in fixture["runtime_discovery"]["runtime_candidates"]
            if item["runtime_image_tag"] == "v0.21.1"
        )
    )
    duplicate["runtime_image_repository"] = "docker.io/example/vllm-mirror"
    duplicate["runtime_image_digest"] = "sha256:" + "d" * 64
    fixture["runtime_discovery"]["runtime_candidates"].append(duplicate)
    with pytest.raises(ValueError):
        _selection(fixture)


def test_selection_excludes_zero_current_revision() -> None:
    fixture = _fixture()
    authority = copy.deepcopy(fixture["expected_current_builder_authority"])
    authority["recipes"][0]["recipe_sha256"] = "sha256:" + "d" * 64
    _reseal(authority, "authority_sha256")

    selection, _, _ = _selection(fixture, authority=authority)
    exclusions = [
        item
        for item in selection["exclusions"]
        if item["reason_code"] == "current-builder-revision-unavailable"
    ]
    assert exclusions
    assert all(set(item) == SELECTION_EXCLUSION_FIELDS for item in exclusions)
    assert all(item["evidence"] for item in exclusions)


def test_selection_rejects_multiple_current_revisions() -> None:
    fixture = _fixture()
    _second_current_revision(fixture)
    with pytest.raises(ValueError):
        _selection(fixture)


@pytest.mark.parametrize("drift", ["source", "catalog", "authority"])
def test_selection_rejects_cross_run_source_closure(drift: str) -> None:
    fixture = _fixture()
    catalog = _assemble_catalog(fixture)
    authority = copy.deepcopy(fixture["expected_current_builder_authority"])
    source_sha = fixture["source_sha"]
    if drift == "source":
        source_sha = "5" * 40
    elif drift == "catalog":
        catalog["source_sha"] = "5" * 40
        _reseal(catalog, "catalog_sha256")
    else:
        authority["source_sha"] = "5" * 40
        for recipe in authority["recipes"]:
            recipe["recipe_source_commit"] = authority["source_sha"]
        _reseal(authority, "authority_sha256")
    prepare = _require_public_callable(products, "prepare_candidate_selection")
    with pytest.raises(ValueError):
        prepare(
            _task4_config(),
            catalog,
            authority,
            route="pr",
            source_sha=source_sha,
            baseline_manifest=None,
        )


def test_selection_grows_for_future_abi_without_fixed_allowlist() -> None:
    baseline, _, _ = _selection(_fixture())
    expanded_fixture = _fixture()
    _future_abi(expanded_fixture)
    expanded, _, _ = _selection(expanded_fixture)

    baseline_abis = {
        item["coordinate"]["python_abi"] for item in baseline["dependency_requests"]
    }
    expanded_abis = {
        item["coordinate"]["python_abi"] for item in expanded["dependency_requests"]
    }
    assert expanded_abis == baseline_abis | {"cp316t"}


def test_prepare_candidate_selection_rejects_nonnull_baseline_until_task4b() -> None:
    fixture = _fixture()
    prepare = _require_public_callable(products, "prepare_candidate_selection")
    with pytest.raises(ValueError):
        prepare(
            _task4_config(),
            _assemble_catalog(fixture),
            copy.deepcopy(fixture["expected_current_builder_authority"]),
            route="release",
            source_sha=fixture["source_sha"],
            baseline_manifest={"kind": "unvalidated-manifest"},
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-field",
        "selection-hash-drift",
        "request-id-drift",
        "requirement-id-drift",
        "malformed-blocker",
        "request-order",
    ],
)
def test_candidate_selection_validator_rejects_noncanonical_objects(
    mutation: str,
) -> None:
    fixture = _fixture()
    if mutation == "request-order":
        _future_abi(fixture)
    selection, _, _ = _selection(fixture)
    validate = _require_public_callable(products, "validate_candidate_selection")
    if mutation == "extra-field":
        selection["extra"] = "unknown"
    elif mutation == "selection-hash-drift":
        selection["selection_sha256"] = "sha256:" + "f" * 64
    elif mutation == "request-id-drift":
        selection["dependency_requests"][0]["request_id"] = "sha256:" + "f" * 64
        _reseal(selection, "selection_sha256")
    elif mutation == "requirement-id-drift":
        request = selection["dependency_requests"][0]
        request["requirements"][0]["requirement_id"] = "sha256:" + "f" * 64
        request["request_id"] = _canonical_digest(
            {
                "coordinate": request["coordinate"],
                "requirements": request["requirements"],
            }
        )
        _reseal(selection, "selection_sha256")
    elif mutation == "malformed-blocker":
        selection["blockers"].append(
            {
                "reason_code": "baseline-capability-missing",
                "admission_key": None,
                "dependency_request_id": None,
                "affected_coordinate": None,
                "evidence": {},
            }
        )
        _reseal(selection, "selection_sha256")
    else:
        selection["dependency_requests"].reverse()
        _reseal(selection, "selection_sha256")
    with pytest.raises(ValueError):
        validate(selection)


@pytest.mark.parametrize("field", ["ucm_version", "release_tag"])
def test_candidate_selection_validator_rejects_empty_top_level_strings(
    field: str,
) -> None:
    selection, _, _ = _selection(_fixture())
    selection[field] = ""
    _reseal(selection, "selection_sha256")
    validate = _require_public_callable(products, "validate_candidate_selection")
    with pytest.raises(ValueError):
        validate(selection)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("scope", "test", id="scope"),
        pytest.param("name", "", id="empty-name"),
        pytest.param("name", "PyYAML", id="noncanonical-name"),
        pytest.param("version", ">=1.0", id="version-range"),
        pytest.param("version", "1.0RC1", id="noncanonical-version"),
        pytest.param("version", "1!1.0", id="version-epoch"),
        pytest.param("version", "1.0+local", id="version-local"),
    ],
)
def test_candidate_selection_validator_rejects_requirement_semantic_drift(
    field: str, value: str
) -> None:
    selection, _, _ = _selection(_fixture())
    request = selection["dependency_requests"][0]
    requirement = request["requirements"][0]
    requirement[field] = value
    identity = {
        "scope": requirement["scope"],
        "name": requirement["name"],
        "version": requirement["version"],
    }
    requirement["requirement_id"] = _canonical_digest(identity)
    request["requirements"].sort(key=lambda item: item["requirement_id"])
    request["request_id"] = _canonical_digest(
        {
            "coordinate": request["coordinate"],
            "requirements": request["requirements"],
        }
    )
    _reseal(selection, "selection_sha256")
    validate = _require_public_callable(products, "validate_candidate_selection")
    with pytest.raises(ValueError):
        validate(selection)


def test_candidate_selection_validator_rejects_duplicate_exclusions() -> None:
    fixture = _fixture()
    authority = copy.deepcopy(fixture["expected_current_builder_authority"])
    authority["recipes"][0]["recipe_sha256"] = "sha256:" + "d" * 64
    _reseal(authority, "authority_sha256")
    selection, _, _ = _selection(fixture, authority=authority)
    assert selection["exclusions"]
    selection["exclusions"].append(copy.deepcopy(selection["exclusions"][0]))
    _reseal(selection, "selection_sha256")
    validate = _require_public_callable(products, "validate_candidate_selection")
    with pytest.raises(ValueError):
        validate(selection)


def test_prepare_candidates_cli_loads_validates_freezes_once_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = cli.build_parser()
    release_path = tmp_path / "release.yaml"
    schema_dir = tmp_path / "schemas"
    catalog_path = tmp_path / "catalog.json"
    output_path = tmp_path / "selection.json"
    arguments_list = [
        "plan",
        "prepare-candidates",
        "--release",
        str(release_path),
        "--schema-dir",
        str(schema_dir),
        "--repository-root",
        str(tmp_path),
        "--capability-catalog",
        str(catalog_path),
        "--route",
        "pr",
        "--source-sha",
        "4" * 40,
        "--output",
        str(output_path),
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(
            [*arguments_list, "--baseline-manifest", str(tmp_path / "baseline.json")]
        )
    arguments = parser.parse_args(arguments_list)

    config = {"config": "validated"}
    raw_catalog = {"catalog": "raw"}
    catalog = {"catalog": "validated"}
    authority = {"authority": "frozen"}
    selection = {"selection": "written"}
    freeze_calls: list[dict[str, object]] = []
    prepare_calls: list[tuple[object, ...]] = []
    writes: list[tuple[Path, object]] = []

    def fake_load_catalog(*args: object, **kwargs: object) -> dict[str, str]:
        assert args[:2] == (release_path, schema_dir)
        assert kwargs["repository_root"] == tmp_path
        return config

    def fake_load_json(path: Path) -> object:
        assert path == catalog_path
        return raw_catalog

    def fake_validate(value: object) -> dict[str, str]:
        assert value is raw_catalog
        return catalog

    def fake_freeze(**kwargs: object) -> dict[str, str]:
        freeze_calls.append(kwargs)
        return authority

    def fake_prepare(*args: object, **kwargs: object) -> dict[str, str]:
        prepare_calls.append((*args, kwargs))
        return selection

    monkeypatch.setattr(core, "load_catalog", fake_load_catalog)
    monkeypatch.setattr(core, "load_json", fake_load_json)
    monkeypatch.setattr(capabilities, "validate_capability_catalog", fake_validate)
    monkeypatch.setattr(
        builders, "freeze_current_builder_authority", fake_freeze, raising=False
    )
    monkeypatch.setattr(
        products, "prepare_candidate_selection", fake_prepare, raising=False
    )
    monkeypatch.setattr(cli, "products", products, raising=False)
    monkeypatch.setattr(cli, "_write", lambda path, value: writes.append((path, value)))

    assert not hasattr(arguments, "baseline_manifest")
    assert arguments.func(arguments) is selection
    assert freeze_calls == [{"source_sha": "4" * 40, "repository_root": tmp_path}]
    assert prepare_calls == [
        (
            config,
            catalog,
            authority,
            {
                "route": "pr",
                "source_sha": "4" * 40,
            },
        )
    ]
    assert writes == [(output_path, selection)]
