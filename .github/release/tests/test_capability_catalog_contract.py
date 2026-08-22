"""RED contracts for the unified, facts-first Capability Catalog."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
FIXTURE_PATH = (
    RELEASE_ROOT / "tests" / "fixtures" / ("capability-catalog-discovery.json")
)
sys.path.insert(0, str(RELEASE_ROOT))

capabilities = importlib.import_module("ucm_release.capabilities")
cli = importlib.import_module("ucm_release.cli")

PYTHON_ABI = re.compile(r"^cp[0-9]+t?$", re.ASCII)
PYTHON_VERSION = re.compile(r"^[0-9]+\.[0-9]+$", re.ASCII)
SOABI = re.compile(r"^cpython-[0-9]+t?-(?:x86_64|aarch64)-linux-gnu$", re.ASCII)

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
CAPABILITY_FIELDS = {
    "builder_capability_id",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "python_version",
    "python_abi",
    "mooncake_version",
    "builder_revision_ids",
}
REVISION_FIELDS = {
    "builder_revision_id",
    "builder_capability_id",
    "source_image_repository",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "revision_sha256",
}
RUNTIME_FIELDS = {
    "runtime_id",
    "product_id",
    "runtime_repository",
    "runtime_tag",
    "runtime_version",
    "channel",
    "variant",
    "cpu_architecture",
    "accelerator",
    "accelerator_runtime",
    "mooncake_version",
    "runtime_image",
    "git_tag",
    "git_commit",
}
BINDING_FIELDS = {
    "builder_capability_id",
    "builder_revision_id",
    "runtime_id",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "python_version",
    "python_abi",
    "source_image",
    "target_image",
    "mooncake_version",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_builder_digest",
    "mooncake_copy_mode",
    "runtime_image",
}
ENTRY_FIELDS = {
    "builder_capability_id",
    "builder_revision_id",
    "runtime_id",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "python_version",
    "python_abi",
    "source_image",
    "target_image",
    "mooncake_version",
    "mooncake_copy_mode",
    "runtime_image",
}
EXCLUSION_FIELDS = {
    "reason_code",
    "source_kind",
    "source_id",
    "builder_capability_id",
    "builder_revision_id",
    "runtime_id",
    "evidence",
}
BUILDER_SYNC_FIELDS = {
    "mode",
    "target_digests_verified",
    "deletions",
}
BUILDER_FACT_FIELDS = {
    "builder_fact_id",
    "project",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "source_kind",
    "source_path",
    "source_image_repository",
    "source_image_tag",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
}
BUILDER_FACT_IDENTITY_FIELDS = (
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "source_image_repository",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
)
PYTHON_PROBE_FIELDS = {
    "builder_fact_id",
    "builder_image",
    "target_builder_digest",
    "cpu_architecture",
    "manylinux",
    "runner",
    "interpreter_path",
    "python_version",
    "python_abi",
    "soabi",
    "wheel_tag",
}
PYTHON_PROBE_FAILURE_FIELDS = {
    "status",
    "reason_code",
    "source_kind",
    "source_id",
    "builder_fact_id",
    "builder_image",
    "target_builder_digest",
    "cpu_architecture",
    "manylinux",
    "runner",
    "interpreter_path",
    "builder_capability_id",
    "builder_revision_id",
    "runtime_id",
    "evidence",
}
MOONCAKE_PROBE_FAILURE_FIELDS = {
    "status",
    "reason_code",
    "source_kind",
    "source_id",
    "runtime_id",
    "runtime_image",
    "runtime_image_digest",
    "cpu_architecture",
    "runner",
    "builder_capability_id",
    "builder_revision_id",
    "evidence",
}


def _load_fixture() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _require_public_callable(name: str) -> Callable[..., dict[str, Any]]:
    function = getattr(capabilities, name, None)
    message = f"required public API ucm_release.capabilities.{name} is missing"
    assert callable(function), message
    return function


def _assemble_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    original = copy.deepcopy(fixture)
    assemble = _require_public_callable("assemble_capability_catalog")

    catalog = assemble(
        builder_discovery=fixture["builder_discovery"],
        runtime_discovery=fixture["runtime_discovery"],
        python_probes=fixture["python_probes"],
        mooncake_probes=fixture["mooncake_probes"],
        python_requires=fixture["python_requires"],
    )

    assert fixture == original, "Catalog assembly must not mutate discovery facts"
    assert isinstance(catalog, dict)
    return catalog


def _assemble() -> dict[str, Any]:
    return _assemble_fixture(_load_fixture())


def test_catalog_assembly_cli_uses_neutral_version_without_overriding_source_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_sha = "7" * 40
    builder_facts = {"source_sha": source_sha}
    runtime_discovery = {"runtime_candidates": []}
    python_probes = {"probes": [], "failures": []}
    mooncake_probes = {"probes": [], "failures": []}
    captured: dict[str, Any] = {}

    def load_catalog(*, version_override: str) -> dict[str, Any]:
        captured["version_override"] = version_override
        return {"discovery": {"python_requires": ">=3.10"}}

    def load_json(path: Path) -> dict[str, Any]:
        return {
            tmp_path / "builder-facts.json": builder_facts,
            tmp_path / "runtime-discovery.json": runtime_discovery,
        }[path]

    def assemble(**values: Any) -> dict[str, Any]:
        assert values["builder_discovery"] is builder_facts
        assert values["runtime_discovery"] is runtime_discovery
        assert values["python_probes"] is python_probes
        assert values["mooncake_probes"] is mooncake_probes
        assert values["python_requires"] == ">=3.10"
        return {"source_sha": values["builder_discovery"]["source_sha"]}

    monkeypatch.setattr(cli.core, "load_catalog", load_catalog)
    monkeypatch.setattr(cli.core, "load_json", load_json)
    monkeypatch.setattr(cli, "_load_result_records", lambda *args: python_probes)
    monkeypatch.setattr(cli, "_load_mooncake_probes", lambda path: mooncake_probes)
    monkeypatch.setattr(cli.capabilities, "assemble_capability_catalog", assemble)
    monkeypatch.setattr(
        cli.capabilities,
        "validate_capability_catalog",
        lambda value: value,
    )
    monkeypatch.setattr(cli, "_write", lambda path, value: None)

    arguments = cli.build_parser().parse_args(
        [
            "catalog",
            "assemble-capabilities",
            "--builder-facts",
            str(tmp_path / "builder-facts.json"),
            "--python-probes",
            str(tmp_path / "python-probes"),
            "--runtime-discovery",
            str(tmp_path / "runtime-discovery.json"),
            "--mooncake-probes",
            str(tmp_path / "mooncake-probes"),
            "--output",
            str(tmp_path / "catalog.json"),
        ]
    )
    result = arguments.func(arguments)

    assert captured["version_override"] == "0.0.0"
    assert result["source_sha"] == source_sha
    assert result["source_sha"] != captured["version_override"]


def _catalog_digest(catalog: dict[str, Any]) -> str:
    projection = copy.deepcopy(catalog)
    projection.pop("catalog_sha256", None)
    return _canonical_digest(projection)


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _runtime_id(value: dict[str, Any]) -> str:
    return _canonical_digest(
        {
            "product_id": value["product_id"],
            "runtime_repository": value["runtime_image_repository"],
            "runtime_tag": value["runtime_image_tag"],
            "variant": value["variant"],
            "cpu_architecture": value["cpu_architecture"],
        }
    )


def _runtime_is_compatible(capability: dict[str, Any], runtime: dict[str, Any]) -> bool:
    if any(
        capability[field] != runtime[field]
        for field in (
            "accelerator",
            "variant",
            "cpu_architecture",
            "mooncake_version",
        )
    ):
        return False
    capability_runtime = capability["accelerator_runtime"]
    runtime_value = runtime["accelerator_runtime"]
    if capability["accelerator"] != "cuda":
        return capability_runtime == runtime_value
    if not (
        capability_runtime.startswith("cuda-") and runtime_value.startswith("cuda-")
    ):
        return False
    capability_version = Version(capability_runtime.removeprefix("cuda-"))
    runtime_version = Version(runtime_value.removeprefix("cuda-"))
    return capability_version.release[:2] == runtime_version.release[:2]


def _ascend_target_tag(source: dict[str, Any], runtime: dict[str, Any]) -> str:
    suffix = _canonical_digest(
        {
            "source_target_tag": source["target_tag"],
            "runtime_id": _runtime_id(runtime),
            "runtime_image_digest": runtime["runtime_image_digest"],
        }
    ).split(":", 1)[1]
    prefix = re.sub(r"[^A-Za-z0-9._-]", "-", source["target_tag"])
    prefix = prefix[:60].rstrip(".-")
    if not prefix or re.match(r"^[A-Za-z0-9_]", prefix) is None:
        prefix = "_" + prefix[1:]
    return f"{prefix}-rt-{suffix}"


def _reseal(catalog: dict[str, Any]) -> None:
    catalog["catalog_sha256"] = _catalog_digest(catalog)


def _assert_no_wall_clock_fields(value: object) -> None:
    if isinstance(value, dict):
        forbidden = {"created_at", "completed_at", "generated_at", "timestamp"}
        assert forbidden.isdisjoint(value)
        for nested in value.values():
            _assert_no_wall_clock_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_wall_clock_fields(nested)


def test_builder_fact_fixture_is_abi_independent_and_target_bound() -> None:
    """Physical Builder identity exists only after immutable target readback."""
    fixture = _load_fixture()
    discovery = fixture["builder_discovery"]
    source_builders = discovery["builders"]
    facts = discovery["builder_facts"]

    assert all("target_builder_digest" not in item for item in source_builders)
    assert all("builder_fact_id" not in item for item in source_builders)
    assert all("builder_revision_id" not in item for item in source_builders)
    assert all("python_abi" not in item for item in source_builders)
    assert all(set(fact) == BUILDER_FACT_FIELDS for fact in facts)
    assert facts == sorted(facts, key=lambda item: item["builder_fact_id"])

    runtime_by_id = {
        _runtime_id(runtime): runtime
        for runtime in fixture["runtime_discovery"]["runtime_candidates"]
    }
    mooncake_probe_by_target = {
        (probe["runtime_image_digest"], probe["cpu_architecture"]): probe
        for probe in fixture["mooncake_probes"]["probes"]
    }
    for fact in facts:
        runtime = (
            runtime_by_id[fact["mooncake_source_runtime_id"]]
            if fact["accelerator"] == "ascend"
            else None
        )
        source_matches = [
            item
            for item in source_builders
            if item["target_repository"] == fact["target_repository"]
            and (
                item["target_tag"] == fact["target_tag"]
                or (
                    runtime is not None
                    and _ascend_target_tag(item, runtime) == fact["target_tag"]
                )
            )
        ]
        assert len(source_matches) == 1
        source = source_matches[0]
        for field in (
            "accelerator",
            "accelerator_runtime",
            "variant",
            "cpu_architecture",
            "manylinux",
            "source_image_repository",
            "source_image_digest",
            "recipe_path",
            "recipe_source_commit",
            "recipe_sha256",
            "toolchain_sha256",
        ):
            assert fact[field] == source[field]
        identity = {field: fact[field] for field in BUILDER_FACT_IDENTITY_FIELDS}
        assert fact["builder_fact_id"] == _canonical_digest(identity)
        assert "python_version" not in fact
        assert "python_abi" not in fact
        if fact["accelerator"] == "cuda":
            assert fact["mooncake_source_runtime_id"] is None
            assert fact["mooncake_source_runtime_image"] is None
            assert fact["mooncake_version"] is None
        else:
            assert runtime is not None
            assert fact["mooncake_source_runtime_image"] == (
                f'{runtime["runtime_image_repository"]}@'
                f'{runtime["runtime_image_digest"]}'
            )
            assert fact["mooncake_version"] == runtime["mooncake_version"]
            probe = mooncake_probe_by_target[
                (runtime["runtime_image_digest"], fact["cpu_architecture"])
            ]
            assert probe["declared_version"] == fact["mooncake_version"]
            assert probe["installed_version"] == fact["mooncake_version"]

    shared_source = [
        fact for fact in facts if fact["source_image_digest"] == "sha256:" + "b" * 64
    ]
    assert len(shared_source) == 2
    assert len({fact["builder_fact_id"] for fact in shared_source}) == 2
    assert len({fact["target_builder_digest"] for fact in shared_source}) == 2
    assert len({fact["toolchain_sha256"] for fact in shared_source}) == 2


def test_python_probe_fixture_links_exact_fact_and_defers_revision_identity() -> None:
    """ABI probes reopen target Builders without claiming public revision IDs."""
    fixture = _load_fixture()
    facts = {
        fact["builder_fact_id"]: fact
        for fact in fixture["builder_discovery"]["builder_facts"]
    }
    probes = fixture["python_probes"]["probes"]
    failures = fixture["python_probes"]["failures"]

    assert all(set(probe) == PYTHON_PROBE_FIELDS for probe in probes)
    assert failures
    assert all(set(item) == PYTHON_PROBE_FAILURE_FIELDS for item in failures)
    assert all(item["status"] == "failed" for item in failures)
    for probe in probes:
        fact = facts[probe["builder_fact_id"]]
        assert probe["builder_image"] == (
            f'{fact["target_repository"]}@{fact["target_builder_digest"]}'
        )
        assert probe["target_builder_digest"] == fact["target_builder_digest"]
        assert probe["cpu_architecture"] == fact["cpu_architecture"]
        assert probe["manylinux"] == fact["manylinux"]
        platform_architecture = {
            "amd64": "x86_64",
            "arm64": "aarch64",
        }[fact["cpu_architecture"]]
        assert PYTHON_VERSION.fullmatch(probe["python_version"])
        abi = probe["python_abi"]
        assert PYTHON_ABI.fullmatch(abi)
        python_tag = "cp" + probe["python_version"].replace(".", "")
        assert abi in {python_tag, python_tag + "t"}
        interpreter_coordinate = probe["interpreter_path"].split("/")[-3]
        assert interpreter_coordinate == f"{python_tag}-{abi}"
        assert SOABI.fullmatch(probe["soabi"])
        assert probe["soabi"] == (
            f'cpython-{abi.removeprefix("cp")}-' f"{platform_architecture}-linux-gnu"
        )
        assert probe["wheel_tag"] == (
            f'{python_tag}-{abi}-{fact["manylinux"]}_{platform_architecture}'
        )
        assert "builder_revision_id" not in probe
        assert "builder_source_image_digest" not in probe
    for failure in failures:
        fact = facts[failure["builder_fact_id"]]
        assert failure["manylinux"] == fact["manylinux"]
        assert failure["cpu_architecture"] == fact["cpu_architecture"]

    multi_abi_fact = next(
        fact
        for fact in facts.values()
        if fact["target_tag"] == "cuda12.9-cp-all-manylinux2_28-amd64-r1"
    )
    multi_abi_probes = [
        probe
        for probe in probes
        if probe["builder_fact_id"] == multi_abi_fact["builder_fact_id"]
    ]
    assert all(PYTHON_ABI.fullmatch(probe["python_abi"]) for probe in multi_abi_probes)
    assert {
        (probe["python_version"], probe["python_abi"])
        for probe in multi_abi_probes
        if probe["python_abi"].endswith("t")
    } == {("3.14", "cp314t"), ("3.15", "cp315t")}
    a2_fact_ids = {
        item["builder_fact_id"]
        for item in facts.values()
        if item["accelerator_runtime"] == "cann-9.0" and item["variant"] == "a2"
    }
    a2_source_count = sum(
        item["accelerator_runtime"] == "cann-9.0" and item["variant"] == "a2"
        for item in fixture["builder_discovery"]["builders"]
    )
    a2_runtime_count = sum(
        item["accelerator_runtime"] == "cann-9.0" and item["variant"] == "a2"
        for item in fixture["runtime_discovery"]["runtime_candidates"]
    )
    assert len(a2_fact_ids) == a2_source_count * a2_runtime_count
    assert {
        probe["builder_fact_id"]
        for probe in probes
        if probe["builder_fact_id"] in a2_fact_ids
    } == a2_fact_ids
    mooncake_failures = fixture["mooncake_probes"]["failures"]
    assert mooncake_failures
    assert all(set(item) == MOONCAKE_PROBE_FAILURE_FIELDS for item in mooncake_failures)


def test_assembled_catalog_is_closed_digest_bound_and_valid() -> None:
    """A partial, open, or non-canonical object cannot be a Catalog artifact."""
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()

    assert set(catalog) == CATALOG_FIELDS
    assert catalog["kind"] == "ucm-capability-catalog"
    assert catalog["schema_version"] == 3
    assert catalog["source_sha"] == "1" * 40
    assert catalog["upstream_reads"]
    expected_sync = _load_fixture()["builder_discovery"]["builder_sync"]
    assert set(catalog["builder_sync"]) == BUILDER_SYNC_FIELDS
    assert catalog["builder_sync"] == expected_sync
    assert catalog["builder_sync"] == {
        "mode": "append-only",
        "target_digests_verified": True,
        "deletions": [],
    }
    assert catalog["catalog_sha256"] == _catalog_digest(catalog)
    assert validate(copy.deepcopy(catalog)) == catalog
    _assert_no_wall_clock_fields(catalog)


def test_catalog_entries_cover_discovered_dimensions_and_filter_python_requires() -> (
    None
):
    """Single-ABI or fixed-runtime assembly loses supported Builder products."""
    catalog = _assemble()
    capabilities_by_coordinate = {
        (
            item["accelerator_runtime"],
            item["variant"],
            item["cpu_architecture"],
            item["python_abi"],
        )
        for item in catalog["builder_capabilities"]
    }

    accelerator_runtimes = {
        item["accelerator_runtime"] for item in catalog["builder_capabilities"]
    }
    assert accelerator_runtimes >= {
        "cuda-12.9",
        "cuda-13.0",
        "cann-9.0",
        "cann-9.1",
    }
    assert {item["cpu_architecture"] for item in catalog["builder_capabilities"]} == {
        "amd64",
        "arm64",
    }
    fixture = _load_fixture()
    expected_supported_abis = {
        probe["python_abi"]
        for probe in fixture["python_probes"]["probes"]
        if tuple(int(part) for part in probe["python_version"].split(".")) >= (3, 10)
    }
    catalog_abis = {item["python_abi"] for item in catalog["builder_capabilities"]}
    assert catalog_abis == expected_supported_abis
    assert all(PYTHON_ABI.fullmatch(abi) for abi in catalog_abis)
    assert ("cuda-12.9", "default", "amd64", "cp310") in capabilities_by_coordinate
    assert ("cuda-12.9", "default", "amd64", "cp311") in capabilities_by_coordinate
    assert ("cuda-13.0", "default", "arm64", "cp312") in capabilities_by_coordinate
    assert ("cann-9.0", "a2", "amd64", "cp310") in capabilities_by_coordinate
    assert ("cann-9.1", "a4", "arm64", "cp311") in capabilities_by_coordinate

    assert catalog["entries"]
    assert all(ENTRY_FIELDS <= set(entry) for entry in catalog["entries"])
    assert all("@sha256:" in entry["source_image"] for entry in catalog["entries"])
    assert all("@sha256:" in entry["target_image"] for entry in catalog["entries"])
    assert all(entry["python_abi"] != "cp39" for entry in catalog["entries"])
    rejected_cp39 = [
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "python-requires-mismatch"
    ]
    assert rejected_cp39
    assert {item["evidence"]["python_abi"] for item in rejected_cp39} == {"cp39"}
    assert all(
        item["evidence"]["python_requires"] == ">=3.10" for item in rejected_cp39
    )


def test_one_physical_builder_fact_expands_only_after_multi_abi_probing() -> None:
    """One physical target becomes separate public revisions only after ABI facts."""
    fixture = _load_fixture()
    fact = next(
        item
        for item in fixture["builder_discovery"]["builder_facts"]
        if item["target_tag"] == "cuda12.9-cp-all-manylinux2_28-amd64-r1"
    )
    catalog = _assemble()
    revisions = [
        revision
        for revision in catalog["builder_revisions"]
        if revision["target_builder_digest"] == fact["target_builder_digest"]
    ]
    capabilities_by_id = {
        item["builder_capability_id"]: item for item in catalog["builder_capabilities"]
    }
    expected_abis = {
        probe["python_abi"]
        for probe in fixture["python_probes"]["probes"]
        if probe["builder_fact_id"] == fact["builder_fact_id"]
        and tuple(int(part) for part in probe["python_version"].split(".")) >= (3, 10)
    }

    assert len(revisions) == len(expected_abis)
    assert len({item["builder_revision_id"] for item in revisions}) == len(
        expected_abis
    )
    assert {
        capabilities_by_id[item["builder_capability_id"]]["python_abi"]
        for item in revisions
    } == expected_abis


def test_catalog_preserves_discovery_origins_and_filters_only_exact_310p() -> None:
    """Source provenance and future Variants must survive canonical assembly."""
    catalog = _assemble()
    reads = catalog["upstream_reads"]

    assert any(
        read["project"] == "vllm-project/vllm"
        and read["source_kind"] == "buildkite-build-base-image"
        and read["fact"] == "BUILD_BASE_IMAGE"
        for read in reads
    )
    ascend_paths = {
        read["source_path"]
        for read in reads
        if read["source_kind"] == "buildwheel-dockerfile"
    }
    assert ascend_paths == {
        ".github/workflows/dockerfiles/Dockerfile.buildwheel.310p",
        ".github/workflows/dockerfiles/Dockerfile.buildwheel.a2",
        ".github/workflows/dockerfiles/Dockerfile.buildwheel.a4",
    }
    assert any(item["variant"] == "a4" for item in catalog["builder_capabilities"])
    assert all(item["variant"] != "310p" for item in catalog["builder_capabilities"])
    assert all(item["variant"] != "310p" for item in catalog["entries"])
    filtered = next(
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "variant-filtered-310p"
    )
    assert filtered == {
        "reason_code": "variant-filtered-310p",
        "source_kind": "buildwheel-dockerfile",
        "source_id": (".github/workflows/dockerfiles/Dockerfile.buildwheel.310p"),
        "builder_capability_id": None,
        "builder_revision_id": None,
        "runtime_id": None,
        "evidence": {"variant": "310p"},
    }


def test_runtime_candidate_normalization_is_public_identity_authority() -> None:
    """Raw discovery keys must not define a second runtime identity hash."""
    normalize = _require_public_callable("normalize_runtime_candidate")
    fixture = _load_fixture()
    original = copy.deepcopy(fixture)

    for raw in fixture["runtime_discovery"]["runtime_candidates"]:
        runtime = normalize(copy.deepcopy(raw))
        assert set(runtime) == RUNTIME_FIELDS
        assert runtime["runtime_repository"] == raw["runtime_image_repository"]
        assert runtime["runtime_tag"] == raw["runtime_image_tag"]
        assert runtime["runtime_image"] == (
            f'{raw["runtime_image_repository"]}@{raw["runtime_image_digest"]}'
        )
        identity = {
            field: runtime[field]
            for field in (
                "product_id",
                "runtime_repository",
                "runtime_tag",
                "variant",
                "cpu_architecture",
            )
        }
        assert runtime["runtime_id"] == _canonical_digest(identity)
        assert runtime["runtime_id"] == _runtime_id(raw)

    assert fixture == original
    builders_tree = ast.parse(
        (RELEASE_ROOT / "ucm_release" / "builders.py").read_text(encoding="utf-8")
    )
    plan_function = next(
        node
        for node in builders_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "plan_builder_facts"
    )
    capability_calls = {
        f"{call.func.value.id}.{call.func.attr}"
        for call in ast.walk(plan_function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "capabilities"
    }
    assert "capabilities.normalize_runtime_candidate" in capability_calls
    assert "capabilities._runtime_record" not in capability_calls
    top_level_functions = {
        node.name
        for node in builders_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "normalize_runtime_candidate" not in top_level_functions


def test_runtime_candidates_remain_multi_version_and_git_source_bound() -> None:
    """Latest-wins collapse destroys fallback and baseline runtime identities."""
    catalog = _assemble()
    runtimes = catalog["runtime_candidates"]

    assert all(set(runtime) == RUNTIME_FIELDS for runtime in runtimes)
    assert {runtime["product_id"] for runtime in runtimes} == {"vllm", "vllm-ascend"}
    assert {
        runtime["runtime_version"]
        for runtime in runtimes
        if runtime["product_id"] == "vllm"
        and runtime["accelerator_runtime"] == "cuda-12.9.1"
        and runtime["cpu_architecture"] == "amd64"
    } == {"0.10.0", "0.10.1"}
    assert {
        runtime["accelerator_runtime"]
        for runtime in runtimes
        if runtime["accelerator"] == "cuda"
    } == {"cuda-12.8.1", "cuda-12.9.1", "cuda-13.0.2"}
    assert any(runtime["accelerator_runtime"] == "cann-9.0.0" for runtime in runtimes)
    assert {
        runtime["mooncake_version"]
        for runtime in runtimes
        if runtime["accelerator_runtime"] == "cann-9.0"
        and runtime["variant"] == "a2"
        and runtime["cpu_architecture"] == "amd64"
    } == {"0.3.11.post1", "0.3.12"}
    assert all("@sha256:" in runtime["runtime_image"] for runtime in runtimes)
    assert all(runtime["git_tag"].startswith("v") for runtime in runtimes)
    assert all(len(runtime["git_commit"]) == 40 for runtime in runtimes)
    assert any(runtime["variant"] == "310p" for runtime in runtimes)


def test_cuda_patch_runtimes_bind_by_family_without_losing_exact_identity() -> None:
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()
    assert validate(copy.deepcopy(catalog)) == catalog
    capabilities_by_id = {
        item["builder_capability_id"]: item for item in catalog["builder_capabilities"]
    }
    runtimes_by_id = {
        item["runtime_id"]: item for item in catalog["runtime_candidates"]
    }
    revisions = {
        item["builder_revision_id"]: item for item in catalog["builder_revisions"]
    }

    expected_cuda_pairs = {
        (revision["builder_revision_id"], runtime["runtime_id"])
        for revision in revisions.values()
        for runtime in runtimes_by_id.values()
        if capabilities_by_id[revision["builder_capability_id"]]["accelerator"]
        == "cuda"
        and _runtime_is_compatible(
            capabilities_by_id[revision["builder_capability_id"]], runtime
        )
    }
    actual_cuda_pairs = {
        (binding["builder_revision_id"], binding["runtime_id"])
        for binding in catalog["bindings"]
        if binding["accelerator"] == "cuda"
    }
    cuda_entry_pairs = {
        (entry["builder_revision_id"], entry["runtime_id"])
        for entry in catalog["entries"]
        if entry["accelerator"] == "cuda"
    }
    assert expected_cuda_pairs
    assert actual_cuda_pairs == expected_cuda_pairs
    assert cuda_entry_pairs == expected_cuda_pairs

    cuda_129_runtime_ids = {
        runtime["runtime_id"]
        for runtime in runtimes_by_id.values()
        if runtime["accelerator_runtime"] == "cuda-12.9.1"
    }
    assert len(cuda_129_runtime_ids) == 2
    cuda_129_capabilities = [
        capability
        for capability in capabilities_by_id.values()
        if capability["accelerator_runtime"] == "cuda-12.9"
    ]
    assert cuda_129_capabilities
    assert all(
        cuda_129_runtime_ids
        <= {
            binding["runtime_id"]
            for binding in catalog["bindings"]
            if binding["builder_capability_id"] == capability["builder_capability_id"]
        }
        for capability in cuda_129_capabilities
    )

    for binding in catalog["bindings"]:
        if binding["accelerator"] != "cuda":
            continue
        capability = capabilities_by_id[binding["builder_capability_id"]]
        runtime = runtimes_by_id[binding["runtime_id"]]
        assert binding["accelerator_runtime"] == capability["accelerator_runtime"]
        assert binding["accelerator_runtime"] != runtime["accelerator_runtime"]

    deliberately_unbound = {
        runtime["runtime_id"]
        for runtime in runtimes_by_id.values()
        if runtime["accelerator_runtime"] in {"cuda-12.8.1", "cann-9.0.0"}
    }
    assert len(deliberately_unbound) == 2
    assert deliberately_unbound.isdisjoint(
        {binding["runtime_id"] for binding in catalog["bindings"]}
    )


def test_mooncake_runtime_copy_is_version_exact_and_mismatch_is_local() -> None:
    """A fixed clone or global mismatch failure violates Ascend isolation."""
    fixture = _load_fixture()
    catalog = _assemble()
    all_a2_facts = [
        item
        for item in fixture["builder_discovery"]["builder_facts"]
        if item["accelerator_runtime"] == "cann-9.0" and item["variant"] == "a2"
    ]
    capabilities_by_id = {
        item["builder_capability_id"]: item for item in catalog["builder_capabilities"]
    }
    revisions_by_id = {
        item["builder_revision_id"]: item for item in catalog["builder_revisions"]
    }
    runtimes_by_id = {
        item["runtime_id"]: item for item in catalog["runtime_candidates"]
    }

    base_a2_source = next(
        item
        for item in fixture["builder_discovery"]["builders"]
        if item["target_tag"] == "cann9.0-a2-cp-all-manylinux2_34-amd64-r1"
    )
    a2_runtime_values = [
        item
        for item in fixture["runtime_discovery"]["runtime_candidates"]
        if item["accelerator_runtime"] == "cann-9.0" and item["variant"] == "a2"
    ]
    base_a2_tags = {
        _ascend_target_tag(base_a2_source, runtime) for runtime in a2_runtime_values
    }
    a2_facts = [item for item in all_a2_facts if item["target_tag"] in base_a2_tags]
    assert len(a2_facts) == 2
    assert len({item["builder_fact_id"] for item in a2_facts}) == 2
    assert len({item["target_tag"] for item in a2_facts}) == 2
    assert len({item["target_builder_digest"] for item in a2_facts}) == 2
    assert {item["mooncake_version"] for item in a2_facts} == {
        "0.3.11.post1",
        "0.3.12",
    }
    all_a2_revision_ids = set()
    for fact in all_a2_facts:
        revisions = [
            item
            for item in catalog["builder_revisions"]
            if item["target_builder_digest"] == fact["target_builder_digest"]
        ]
        assert len(revisions) == 1
        revision = revisions[0]
        all_a2_revision_ids.add(revision["builder_revision_id"])
        capability = capabilities_by_id[revision["builder_capability_id"]]
        runtime = runtimes_by_id[fact["mooncake_source_runtime_id"]]
        bindings = [
            item
            for item in catalog["bindings"]
            if item["builder_revision_id"] == revision["builder_revision_id"]
        ]
        assert capability["mooncake_version"] == fact["mooncake_version"]
        assert runtime["mooncake_version"] == fact["mooncake_version"]
        assert {item["runtime_id"] for item in bindings} == {
            fact["mooncake_source_runtime_id"]
        }
        assert all(item["mooncake_copy_mode"] == "runtime-copy" for item in bindings)
        assert all(
            item["runtime_image"] == fact["mooncake_source_runtime_image"]
            for item in bindings
        )
    assert len(all_a2_revision_ids) == len(all_a2_facts)

    long_sources = [
        item
        for item in fixture["builder_discovery"]["builders"]
        if "long-shared-prefix" in item["target_tag"]
    ]
    expected_long_pairs = {
        (_ascend_target_tag(source, runtime), _runtime_id(runtime))
        for source in long_sources
        for runtime in a2_runtime_values
    }
    long_facts = [
        item
        for item in all_a2_facts
        if (item["target_tag"], item["mooncake_source_runtime_id"])
        in expected_long_pairs
    ]
    assert len(long_facts) == len(expected_long_pairs) == 4
    assert len({item["target_tag"] for item in long_facts}) == 4
    assert len({item["builder_fact_id"] for item in long_facts}) == 4
    assert len({item["target_builder_digest"] for item in long_facts}) == 4

    cuda_fact = next(
        item
        for item in fixture["builder_discovery"]["builder_facts"]
        if item["accelerator_runtime"] == "cuda-12.9"
    )
    cuda_revisions = [
        item
        for item in catalog["builder_revisions"]
        if item["target_builder_digest"] == cuda_fact["target_builder_digest"]
    ]
    assert cuda_revisions
    for revision in cuda_revisions:
        capability = capabilities_by_id[revision["builder_capability_id"]]
        compatible_cuda_runtimes = {
            runtime["runtime_id"]
            for runtime in catalog["runtime_candidates"]
            if _runtime_is_compatible(capability, runtime)
        }
        assert len(compatible_cuda_runtimes) > 1
        assert {
            item["runtime_id"]
            for item in catalog["bindings"]
            if item["builder_revision_id"] == revision["builder_revision_id"]
        } == compatible_cuda_runtimes

    a4_fact = next(
        item
        for item in fixture["builder_discovery"]["builder_facts"]
        if item["accelerator_runtime"] == "cann-9.1" and item["variant"] == "a4"
    )
    mismatched_runtime = next(
        runtime
        for runtime in catalog["runtime_candidates"]
        if runtime["runtime_tag"] == "v0.10.0-a4"
    )
    mismatch = next(
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "mooncake-version-mismatch"
        and item["runtime_id"] == mismatched_runtime["runtime_id"]
    )
    assert mismatch["evidence"] == {
        "declared_version": "0.3.12",
        "installed_version": "0.3.11.post1",
    }
    assert mismatch["builder_capability_id"] is None
    assert mismatch["builder_revision_id"] is None
    assert mismatch["runtime_id"] == mismatched_runtime["runtime_id"]
    healthy_a4_revision = next(
        item
        for item in catalog["builder_revisions"]
        if item["target_builder_digest"] == a4_fact["target_builder_digest"]
    )
    assert any(
        item["runtime_id"] == a4_fact["mooncake_source_runtime_id"]
        and item["builder_revision_id"] == healthy_a4_revision["builder_revision_id"]
        for item in catalog["bindings"]
    )
    assert all(
        entry["runtime_id"] != mismatched_runtime["runtime_id"]
        for entry in catalog["entries"]
    )
    assert any(entry["accelerator"] == "cuda" for entry in catalog["entries"])
    assert any(
        entry["accelerator_runtime"] == "cann-9.0" for entry in catalog["entries"]
    )
    encoded = json.dumps(catalog, sort_keys=True)
    assert "0.3.9" not in encoded
    assert "git-clone" not in encoded


def test_stable_capability_retains_two_immutable_builder_revisions() -> None:
    """Shared source images must not collapse distinct physical targets."""
    catalog = _assemble()
    capability = next(
        item
        for item in catalog["builder_capabilities"]
        if item["accelerator_runtime"] == "cuda-13.0"
        and item["variant"] == "default"
        and item["cpu_architecture"] == "amd64"
        and item["python_abi"] == "cp312"
    )
    revision_ids = capability["builder_revision_ids"]
    revisions = [
        revision
        for revision in catalog["builder_revisions"]
        if revision["builder_revision_id"] in revision_ids
    ]

    assert set(capability) == CAPABILITY_FIELDS
    assert len(revision_ids) == 2
    assert revision_ids == sorted(revision_ids)
    assert len(revisions) == 2
    assert all(set(revision) == REVISION_FIELDS for revision in revisions)
    assert len({revision["source_image_digest"] for revision in revisions}) == 1
    assert len({revision["toolchain_sha256"] for revision in revisions}) == 2
    assert len({revision["target_builder_digest"] for revision in revisions}) == 2
    assert {
        binding["builder_revision_id"]
        for binding in catalog["bindings"]
        if binding["builder_capability_id"] == capability["builder_capability_id"]
    } == set(revision_ids)


def test_failed_new_builder_becomes_source_only_exclusion() -> None:
    """A failed same-run target is evidence, not a fabricated public revision."""
    fixture = _load_fixture()
    failure = fixture["builder_discovery"]["failures"][0]
    catalog = _assemble()
    exclusion = next(
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "builder-sync-failed"
    )

    assert exclusion == {
        "reason_code": "builder-sync-failed",
        "source_kind": failure["source_kind"],
        "source_id": failure["source_id"],
        "builder_capability_id": None,
        "builder_revision_id": None,
        "runtime_id": None,
        "evidence": {
            "builder_plan_id": failure["builder_plan_id"],
            "status": "failed",
            "target_repository": failure["target_repository"],
            "target_tag": failure["target_tag"],
            "target_builder_digest": None,
            "digest_readback": False,
            "failure": failure["evidence"],
        },
    }
    assert (
        exclusion["evidence"]["failure"]["plan"]["target_tag"] == failure["target_tag"]
    )
    assert all(
        (item["target_repository"], item["target_tag"])
        != (failure["target_repository"], failure["target_tag"])
        for item in catalog["builder_revisions"]
    )
    assert catalog["bindings"]


def test_probe_failures_become_local_exclusions_without_erasing_healthy_facts() -> None:
    fixture = _load_fixture()
    python_failure = fixture["python_probes"]["failures"][0]
    mooncake_failure = fixture["mooncake_probes"]["failures"][0]
    catalog = _assemble()

    python_exclusion = next(
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "python-probe-failed"
    )
    assert python_exclusion["source_id"] == python_failure["source_id"]
    assert python_exclusion["builder_capability_id"] is None
    assert python_exclusion["builder_revision_id"] is None
    assert python_exclusion["runtime_id"] is None
    assert (
        python_exclusion["evidence"]["builder_fact_id"]
        == python_failure["builder_fact_id"]
    )
    assert python_exclusion["evidence"]["manylinux"] == python_failure["manylinux"]

    mooncake_exclusion = next(
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "mooncake-probe-failed"
    )
    assert mooncake_exclusion["source_id"] == mooncake_failure["source_id"]
    assert mooncake_exclusion["builder_capability_id"] is None
    assert mooncake_exclusion["builder_revision_id"] is None
    assert mooncake_exclusion["runtime_id"] == mooncake_failure["runtime_id"]
    assert any(
        item["runtime_id"] == mooncake_failure["runtime_id"]
        and item["variant"] == "310p"
        for item in catalog["runtime_candidates"]
    )

    healthy_fact = next(
        item
        for item in fixture["builder_discovery"]["builder_facts"]
        if item["builder_fact_id"] == python_failure["builder_fact_id"]
    )
    assert any(
        item["target_builder_digest"] == healthy_fact["target_builder_digest"]
        for item in catalog["builder_revisions"]
    )
    assert any(
        item["runtime_id"] != mooncake_failure["runtime_id"]
        for item in catalog["bindings"]
    )


def test_all_builder_facts_probe_failed_rejects_empty_capability_catalog() -> None:
    baseline = _assemble()
    assert baseline["builder_capabilities"]
    fixture = _load_fixture()
    template = fixture["python_probes"]["failures"][0]
    failures = []
    for fact in fixture["builder_discovery"]["builder_facts"]:
        failure = copy.deepcopy(template)
        failure["builder_fact_id"] = fact["builder_fact_id"]
        failure["builder_image"] = (
            f'{fact["target_repository"]}@{fact["target_builder_digest"]}'
        )
        failure["target_builder_digest"] = fact["target_builder_digest"]
        failure["cpu_architecture"] = fact["cpu_architecture"]
        failure["manylinux"] = fact["manylinux"]
        failure["runner"] = (
            "ubuntu-24.04-arm"
            if fact["cpu_architecture"] == "arm64"
            else "ubuntu-24.04"
        )
        failure["interpreter_path"] = "/opt/python/cp*-cp*/bin/python"
        failure["source_id"] = (
            f'{fact["builder_fact_id"]}:{failure["interpreter_path"]}'
        )
        failures.append(failure)
    fixture["python_probes"]["probes"] = []
    fixture["python_probes"]["failures"] = failures

    with pytest.raises(ValueError):
        _assemble_fixture(fixture)


def test_capability_revision_and_runtime_digest_identities_are_recomputable() -> None:
    """Opaque or mutable identity inputs cannot support independent validation."""
    catalog = _assemble()
    capability_identity_fields = (
        "accelerator",
        "accelerator_runtime",
        "variant",
        "python_version",
        "python_abi",
        "cpu_architecture",
        "manylinux",
        "mooncake_version",
    )
    revision_identity_fields = (
        "builder_capability_id",
        "source_image_digest",
        "recipe_source_commit",
        "recipe_sha256",
        "toolchain_sha256",
        "target_builder_digest",
    )
    runtime_identity_fields = (
        "product_id",
        "runtime_repository",
        "runtime_tag",
        "variant",
        "cpu_architecture",
    )

    for capability in catalog["builder_capabilities"]:
        assert set(capability) == CAPABILITY_FIELDS
        identity = {field: capability[field] for field in capability_identity_fields}
        assert capability["builder_capability_id"] == _canonical_digest(identity)
        assert capability["builder_revision_ids"] == sorted(
            capability["builder_revision_ids"]
        )

    for revision in catalog["builder_revisions"]:
        assert set(revision) == REVISION_FIELDS
        identity = {field: revision[field] for field in revision_identity_fields}
        assert revision["builder_revision_id"] == _canonical_digest(identity)
        projection = copy.deepcopy(revision)
        projection.pop("revision_sha256")
        assert revision["revision_sha256"] == _canonical_digest(projection)

    for runtime in catalog["runtime_candidates"]:
        assert set(runtime) == RUNTIME_FIELDS
        identity = {field: runtime[field] for field in runtime_identity_fields}
        assert runtime["runtime_id"] == _canonical_digest(identity)
        assert runtime["runtime_image"].startswith(runtime["runtime_repository"])
        assert "@sha256:" in runtime["runtime_image"]


def test_bindings_entries_and_exclusions_are_closed_and_consistent() -> None:
    """Open records, projection drift, or fallback change the build target."""
    catalog = _assemble()
    capabilities_by_id = {
        item["builder_capability_id"]: item for item in catalog["builder_capabilities"]
    }
    revisions_by_id = {
        item["builder_revision_id"]: item for item in catalog["builder_revisions"]
    }
    runtimes_by_id = {
        item["runtime_id"]: item for item in catalog["runtime_candidates"]
    }
    pairs = [
        (binding["builder_revision_id"], binding["runtime_id"])
        for binding in catalog["bindings"]
    ]
    assert all(set(binding) == BINDING_FIELDS for binding in catalog["bindings"])
    assert all(set(entry) == ENTRY_FIELDS for entry in catalog["entries"])
    assert all(
        set(exclusion) == EXCLUSION_FIELDS for exclusion in catalog["exclusions"]
    )
    assert len(pairs) == len(set(pairs))

    capability_projection = (
        "accelerator",
        "accelerator_runtime",
        "variant",
        "cpu_architecture",
        "manylinux",
        "python_version",
        "python_abi",
        "mooncake_version",
    )
    revision_projection = (
        "recipe_path",
        "recipe_source_commit",
        "recipe_sha256",
        "toolchain_sha256",
        "target_builder_digest",
    )
    runtime_compatibility = (
        "accelerator",
        "variant",
        "cpu_architecture",
        "mooncake_version",
    )

    for binding in catalog["bindings"]:
        revision = revisions_by_id[binding["builder_revision_id"]]
        runtime = runtimes_by_id[binding["runtime_id"]]
        capability = capabilities_by_id[binding["builder_capability_id"]]
        assert revision["builder_capability_id"] == binding["builder_capability_id"]
        for field in capability_projection:
            assert binding[field] == capability[field]
        for field in revision_projection:
            assert binding[field] == revision[field]
        assert binding["source_image"] == (
            f'{revision["source_image_repository"]}@'
            f'{revision["source_image_digest"]}'
        )
        assert binding["target_image"] in {
            f'{revision["target_repository"]}@' f'{revision["target_builder_digest"]}',
            f'{revision["target_repository"]}:{revision["target_tag"]}@'
            f'{revision["target_builder_digest"]}',
        }
        for field in runtime_compatibility:
            assert binding[field] == runtime[field]
        assert _runtime_is_compatible(capability, runtime)
        assert binding["accelerator_runtime"] == capability["accelerator_runtime"]
        if binding["accelerator"] == "cuda":
            assert binding["accelerator_runtime"] != runtime["accelerator_runtime"]
        else:
            assert binding["accelerator_runtime"] == runtime["accelerator_runtime"]
        assert binding["runtime_image"] == runtime["runtime_image"]
        expected_copy_mode = (
            "none" if binding["accelerator"] == "cuda" else "runtime-copy"
        )
        assert binding["mooncake_copy_mode"] == expected_copy_mode
        assert (binding["mooncake_version"] is None) == (
            binding["mooncake_copy_mode"] == "none"
        )

    bindings_by_pair = {
        (binding["builder_revision_id"], binding["runtime_id"]): binding
        for binding in catalog["bindings"]
    }
    for entry in catalog["entries"]:
        pair = (entry["builder_revision_id"], entry["runtime_id"])
        binding = bindings_by_pair[pair]
        for field in ENTRY_FIELDS:
            assert entry[field] == binding[field]

    for exclusion in catalog["exclusions"]:
        assert isinstance(exclusion["source_kind"], str)
        assert exclusion["source_kind"]
        assert isinstance(exclusion["source_id"], str)
        assert exclusion["source_id"]
        assert isinstance(exclusion["evidence"], dict)
        capability_id = exclusion["builder_capability_id"]
        revision_id = exclusion["builder_revision_id"]
        runtime_id = exclusion["runtime_id"]
        assert capability_id is None or capability_id in capabilities_by_id
        assert revision_id is None or revision_id in revisions_by_id
        assert runtime_id is None or runtime_id in runtimes_by_id
        if exclusion["reason_code"] in {
            "builder-sync-failed",
            "python-probe-failed",
            "python-requires-mismatch",
            "variant-filtered-310p",
        }:
            assert capability_id is revision_id is runtime_id is None
        else:
            assert exclusion["reason_code"] in {
                "mooncake-probe-failed",
                "mooncake-version-mismatch",
            }
            assert capability_id is None
            assert revision_id is None
            assert runtime_id is not None


def test_catalog_set_like_arrays_use_approved_canonical_order() -> None:
    """Canonical digest stability requires one explicit order per set-like array."""
    catalog = _assemble()

    assert catalog["builder_capabilities"] == sorted(
        catalog["builder_capabilities"],
        key=lambda item: item["builder_capability_id"],
    )
    assert catalog["builder_revisions"] == sorted(
        catalog["builder_revisions"],
        key=lambda item: item["builder_revision_id"],
    )
    assert catalog["runtime_candidates"] == sorted(
        catalog["runtime_candidates"],
        key=lambda item: item["runtime_id"],
    )
    assert catalog["bindings"] == sorted(
        catalog["bindings"],
        key=lambda item: (item["builder_revision_id"], item["runtime_id"]),
    )
    assert catalog["entries"] == sorted(
        catalog["entries"],
        key=lambda item: (
            item["accelerator"],
            item["accelerator_runtime"],
            item["variant"],
            item["python_abi"],
            item["cpu_architecture"],
            item["builder_revision_id"],
            item["runtime_id"],
        ),
    )
    assert catalog["exclusions"] == sorted(
        catalog["exclusions"],
        key=lambda item: (
            item["reason_code"],
            item["source_kind"],
            item["source_id"],
            item["builder_capability_id"] or "",
            item["builder_revision_id"] or "",
            item["runtime_id"] or "",
        ),
    )
    assert all(
        item["builder_revision_ids"] == sorted(item["builder_revision_ids"])
        for item in catalog["builder_capabilities"]
    )


def _dangling_probe_fact(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["probes"][0]["builder_fact_id"] = "sha256:" + "f" * 64


def _wrong_probe_target_digest(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["probes"][0]["target_builder_digest"] = (
        "sha256:" + "d" * 64
    )


def _wrong_probe_target_image(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["probes"][0]["builder_image"] = (
        "ghcr.io/release-org/ucm-builder-vllm@sha256:" + "d" * 64
    )


def _conflicting_builder_fact(fixture: dict[str, Any]) -> None:
    conflict = copy.deepcopy(fixture["builder_discovery"]["builder_facts"][0])
    conflict["target_tag"] += "-conflict"
    fixture["builder_discovery"]["builder_facts"].append(conflict)


def _conflicting_python_probe(fixture: dict[str, Any]) -> None:
    conflict = copy.deepcopy(fixture["python_probes"]["probes"][0])
    conflict["wheel_tag"] = "cp39-cp39-manylinux_2_34_x86_64"
    fixture["python_probes"]["probes"].append(conflict)


def _probe_architecture_mismatch(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["probes"][0]["cpu_architecture"] = "arm64"


def _probe_manylinux_mismatch(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["probes"][0]["manylinux"] = "manylinux_2_34"


def _probe_failure_manylinux_mismatch(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["failures"][0]["manylinux"] = "manylinux_2_34"


def _generic_linux_wheel_tag(fixture: dict[str, Any]) -> None:
    probe = fixture["python_probes"]["probes"][0]
    architecture = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }[probe["cpu_architecture"]]
    probe["wheel_tag"] = (
        f'{probe["python_abi"]}-{probe["python_abi"]}-linux_{architecture}'
    )


def _wrong_wheel_platform_architecture(fixture: dict[str, Any]) -> None:
    probe = fixture["python_probes"]["probes"][0]
    wrong_architecture = {
        "amd64": "aarch64",
        "arm64": "x86_64",
    }[probe["cpu_architecture"]]
    probe["wheel_tag"] = (
        f'{probe["python_abi"]}-{probe["python_abi"]}-'
        f'{probe["manylinux"]}_{wrong_architecture}'
    )


def _free_threaded_probe(fixture: dict[str, Any]) -> dict[str, Any]:
    return next(
        probe
        for probe in fixture["python_probes"]["probes"]
        if probe["python_abi"] == "cp314t"
    )


def _free_threaded_path_abi_drift(fixture: dict[str, Any]) -> None:
    _free_threaded_probe(fixture)[
        "interpreter_path"
    ] = "/opt/python/cp314-cp314/bin/python-ft"


def _free_threaded_soabi_drift(fixture: dict[str, Any]) -> None:
    _free_threaded_probe(fixture)["soabi"] = "cpython-314-x86_64-linux-gnu"


def _free_threaded_wheel_tag_drift(fixture: dict[str, Any]) -> None:
    probe = _free_threaded_probe(fixture)
    probe["wheel_tag"] = "cp314-cp314-manylinux_2_28_x86_64"


def _duplicate_free_threaded_probe(fixture: dict[str, Any]) -> None:
    fixture["python_probes"]["probes"].append(
        copy.deepcopy(_free_threaded_probe(fixture))
    )


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_dangling_probe_fact, id="dangling-builder-fact-id"),
        pytest.param(_wrong_probe_target_digest, id="wrong-target-digest"),
        pytest.param(_wrong_probe_target_image, id="wrong-target-image"),
        pytest.param(_conflicting_builder_fact, id="conflicting-builder-fact"),
        pytest.param(_conflicting_python_probe, id="conflicting-python-probe"),
        pytest.param(_probe_architecture_mismatch, id="probe-architecture-mismatch"),
        pytest.param(_probe_manylinux_mismatch, id="probe-manylinux-mismatch"),
        pytest.param(
            _probe_failure_manylinux_mismatch,
            id="probe-failure-manylinux-mismatch",
        ),
        pytest.param(_generic_linux_wheel_tag, id="generic-linux-wheel-tag"),
        pytest.param(
            _wrong_wheel_platform_architecture,
            id="wrong-wheel-platform-architecture",
        ),
        pytest.param(
            _free_threaded_path_abi_drift,
            id="free-threaded-path-abi-drift",
        ),
        pytest.param(
            _free_threaded_soabi_drift,
            id="free-threaded-soabi-drift",
        ),
        pytest.param(
            _free_threaded_wheel_tag_drift,
            id="free-threaded-wheel-tag-drift",
        ),
        pytest.param(
            _duplicate_free_threaded_probe,
            id="duplicate-free-threaded-probe",
        ),
    ],
)
def test_assembly_rejects_dangling_or_conflicting_physical_fact_linkage(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    """Reject-all cannot pass because the unmodified fact graph is valid first."""
    validate = _require_public_callable("validate_capability_catalog")
    baseline = _assemble()
    assert validate(copy.deepcopy(baseline)) == baseline
    fixture = _load_fixture()
    mutation(fixture)

    with pytest.raises(ValueError):
        _assemble_fixture(fixture)


def _duplicate_capability_id(catalog: dict[str, Any]) -> None:
    catalog["builder_capabilities"].append(
        copy.deepcopy(catalog["builder_capabilities"][0])
    )


def _duplicate_revision_id(catalog: dict[str, Any]) -> None:
    catalog["builder_revisions"].append(copy.deepcopy(catalog["builder_revisions"][0]))


def _duplicate_runtime_id(catalog: dict[str, Any]) -> None:
    catalog["runtime_candidates"].append(
        copy.deepcopy(catalog["runtime_candidates"][0])
    )


def _unknown_revision_id(catalog: dict[str, Any]) -> None:
    ids = catalog["builder_capabilities"][0]["builder_revision_ids"]
    ids.append("sha256:" + "f" * 64)
    ids.sort()


def _malformed_digest(catalog: dict[str, Any]) -> None:
    catalog["builder_revisions"][0]["source_image_digest"] = "sha256:not-a-digest"


def _conflicting_revision(catalog: dict[str, Any]) -> None:
    conflict = copy.deepcopy(catalog["builder_revisions"][0])
    conflict["target_tag"] += "-conflict"
    catalog["builder_revisions"].append(conflict)


def _missing_binding_runtime(catalog: dict[str, Any]) -> None:
    catalog["bindings"][0]["runtime_id"] = "sha256:" + "e" * 64


def _missing_binding_revision(catalog: dict[str, Any]) -> None:
    catalog["bindings"][0]["builder_revision_id"] = "sha256:" + "d" * 64


def _missing_binding_capability(catalog: dict[str, Any]) -> None:
    catalog["bindings"][0]["builder_capability_id"] = "sha256:" + "c" * 64


def _duplicate_binding_pair(catalog: dict[str, Any]) -> None:
    catalog["bindings"].append(copy.deepcopy(catalog["bindings"][0]))


def _binding_field_drift(catalog: dict[str, Any]) -> None:
    binding = catalog["bindings"][0]
    binding["accelerator"] = "ascend" if binding["accelerator"] == "cuda" else "cuda"


def _binding_runtime_patch_projection(catalog: dict[str, Any]) -> None:
    runtimes = {
        runtime["runtime_id"]: runtime for runtime in catalog["runtime_candidates"]
    }
    binding = next(
        item for item in catalog["bindings"] if item["accelerator"] == "cuda"
    )
    binding["accelerator_runtime"] = runtimes[binding["runtime_id"]][
        "accelerator_runtime"
    ]


def _binding_cross_cuda_minor(catalog: dict[str, Any]) -> None:
    binding = next(
        item
        for item in catalog["bindings"]
        if item["accelerator_runtime"] == "cuda-12.9"
    )
    runtime = next(
        item
        for item in catalog["runtime_candidates"]
        if item["accelerator_runtime"] == "cuda-12.8.1"
    )
    binding["runtime_id"] = runtime["runtime_id"]
    binding["runtime_image"] = runtime["runtime_image"]


def _binding_cann_patch_mismatch(catalog: dict[str, Any]) -> None:
    binding = next(
        item
        for item in catalog["bindings"]
        if item["accelerator_runtime"] == "cann-9.0"
        and item["mooncake_version"] == "0.3.12"
    )
    runtime = next(
        item
        for item in catalog["runtime_candidates"]
        if item["accelerator_runtime"] == "cann-9.0.0"
    )
    binding["runtime_id"] = runtime["runtime_id"]
    binding["runtime_image"] = runtime["runtime_image"]


def _duplicate_entry_coordinate(catalog: dict[str, Any]) -> None:
    catalog["entries"].append(copy.deepcopy(catalog["entries"][0]))


def _unknown_builder_sync_field(catalog: dict[str, Any]) -> None:
    catalog["builder_sync"]["future_field"] = "unexpected"


def _non_append_builder_sync(catalog: dict[str, Any]) -> None:
    catalog["builder_sync"]["mode"] = "replace"


def _unverified_builder_sync_digests(catalog: dict[str, Any]) -> None:
    catalog["builder_sync"]["target_digests_verified"] = False


def _builder_sync_deletion(catalog: dict[str, Any]) -> None:
    catalog["builder_sync"]["deletions"] = ["ghcr.io/release-org/obsolete"]


def _unknown_catalog_field(catalog: dict[str, Any]) -> None:
    catalog["future_field"] = "must be rejected until the contract owns it"


def _unknown_capability_field(catalog: dict[str, Any]) -> None:
    catalog["builder_capabilities"][0]["future_field"] = "unexpected"


def _unknown_revision_field(catalog: dict[str, Any]) -> None:
    catalog["builder_revisions"][0]["future_field"] = "unexpected"


def _unknown_runtime_field(catalog: dict[str, Any]) -> None:
    catalog["runtime_candidates"][0]["future_field"] = "unexpected"


def _unknown_binding_field(catalog: dict[str, Any]) -> None:
    catalog["bindings"][0]["future_field"] = "unexpected"


def _unknown_entry_field(catalog: dict[str, Any]) -> None:
    catalog["entries"][0]["future_field"] = "unexpected"


def _unknown_exclusion_field(catalog: dict[str, Any]) -> None:
    catalog["exclusions"][0]["future_field"] = "unexpected"


def _noncanonical_capability_order(catalog: dict[str, Any]) -> None:
    catalog["builder_capabilities"].reverse()


def _noncanonical_revision_order(catalog: dict[str, Any]) -> None:
    catalog["builder_revisions"].reverse()


def _noncanonical_runtime_order(catalog: dict[str, Any]) -> None:
    catalog["runtime_candidates"].reverse()


def _noncanonical_binding_order(catalog: dict[str, Any]) -> None:
    catalog["bindings"].reverse()


def _noncanonical_entry_order(catalog: dict[str, Any]) -> None:
    catalog["entries"].reverse()


def _noncanonical_exclusion_order(catalog: dict[str, Any]) -> None:
    catalog["exclusions"].reverse()


def _noncanonical_nested_revision_ids(catalog: dict[str, Any]) -> None:
    capability = next(
        item
        for item in catalog["builder_capabilities"]
        if len(item["builder_revision_ids"]) > 1
    )
    capability["builder_revision_ids"].reverse()


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_duplicate_capability_id, id="duplicate-capability-id"),
        pytest.param(_duplicate_revision_id, id="duplicate-revision-id"),
        pytest.param(_duplicate_runtime_id, id="duplicate-runtime-id"),
        pytest.param(_unknown_revision_id, id="unknown-revision-id"),
        pytest.param(_malformed_digest, id="malformed-digest"),
        pytest.param(_conflicting_revision, id="conflicting-same-revision"),
        pytest.param(_missing_binding_runtime, id="missing-binding-runtime"),
        pytest.param(_missing_binding_revision, id="missing-binding-revision"),
        pytest.param(_missing_binding_capability, id="missing-binding-capability"),
        pytest.param(_duplicate_binding_pair, id="duplicate-binding-pair"),
        pytest.param(_binding_field_drift, id="binding-field-drift"),
        pytest.param(
            _binding_runtime_patch_projection,
            id="binding-runtime-patch-projection",
        ),
        pytest.param(_binding_cross_cuda_minor, id="binding-cross-cuda-minor"),
        pytest.param(
            _binding_cann_patch_mismatch,
            id="binding-cann-patch-mismatch",
        ),
        pytest.param(_duplicate_entry_coordinate, id="duplicate-entry-coordinate"),
        pytest.param(_unknown_builder_sync_field, id="unknown-builder-sync-field"),
        pytest.param(_non_append_builder_sync, id="non-append-builder-sync"),
        pytest.param(
            _unverified_builder_sync_digests,
            id="unverified-builder-sync-digests",
        ),
        pytest.param(_builder_sync_deletion, id="builder-sync-deletion"),
        pytest.param(_unknown_catalog_field, id="unknown-catalog-field"),
        pytest.param(_unknown_capability_field, id="unknown-capability-field"),
        pytest.param(_unknown_revision_field, id="unknown-revision-field"),
        pytest.param(_unknown_runtime_field, id="unknown-runtime-field"),
        pytest.param(_unknown_binding_field, id="unknown-binding-field"),
        pytest.param(_unknown_entry_field, id="unknown-entry-field"),
        pytest.param(_unknown_exclusion_field, id="unknown-exclusion-field"),
        pytest.param(
            _noncanonical_capability_order, id="noncanonical-capability-order"
        ),
        pytest.param(_noncanonical_revision_order, id="noncanonical-revision-order"),
        pytest.param(_noncanonical_runtime_order, id="noncanonical-runtime-order"),
        pytest.param(_noncanonical_binding_order, id="noncanonical-binding-order"),
        pytest.param(_noncanonical_entry_order, id="noncanonical-entry-order"),
        pytest.param(_noncanonical_exclusion_order, id="noncanonical-exclusion-order"),
        pytest.param(
            _noncanonical_nested_revision_ids,
            id="noncanonical-nested-revision-ids",
        ),
    ],
)
def test_catalog_validation_rejects_noncanonical_or_dangling_objects(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    """Reject-all cannot pass because every mutation starts from a valid Catalog."""
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()
    assert validate(copy.deepcopy(catalog)) == catalog
    mutation(catalog)
    _reseal(catalog)

    with pytest.raises(ValueError):
        validate(catalog)


def test_catalog_validation_rejects_malformed_self_digest() -> None:
    """The self digest must be a valid digest and match the canonical projection."""
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()
    assert validate(copy.deepcopy(catalog)) == catalog
    catalog["catalog_sha256"] = "sha256:not-a-digest"

    with pytest.raises(ValueError):
        validate(catalog)
