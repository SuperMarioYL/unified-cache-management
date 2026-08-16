from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

core = importlib.import_module("ucm_release.core")
registry = importlib.import_module("ucm_release.registry")
verify = importlib.import_module("ucm_release.verify")
image = importlib.import_module("ucm_release.image")
chart = importlib.import_module("ucm_release.chart")
wheel = importlib.import_module("ucm_release.wheel")


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _registry_fixture() -> dict[str, object]:
    return json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )


def _catalog_for_single_resolution() -> dict[str, object]:
    catalog = core.load_yaml(core.DEFAULT_RELEASE)
    selectors = {
        "cuda": ("vllm", "default"),
        "a2": ("vllm-ascend", "a2"),
        "a3": ("vllm-ascend", "a3"),
    }
    for case in catalog["chart"]["validation_cases"]:
        case.pop("image_repository", None)
        case.pop("image_digest", None)
        case["product_id"], case["variant"] = selectors[case["name"]]
    return catalog


def _run_cli(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ucm_release", *arguments],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": PYTHONPATH},
        text=True,
        capture_output=True,
        check=check,
    )


def _fake_catalog_crane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    digest_overrides: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    fixture = _registry_fixture()
    payload: dict[str, object] = {"tags": {}, "snapshots": {}}
    for repository, repository_fixture in fixture["repositories"].items():
        payload["tags"][repository] = sorted(
            {tag for page in repository_fixture["pages"] for tag in page["tags"]}
        )
        payload["snapshots"].update(repository_fixture["snapshots"])
    for tag, digest in (digest_overrides or {}).items():
        payload["snapshots"][tag]["index_digest"] = digest
    log = tmp_path / f"{name}.log"
    crane = tmp_path / name
    crane.write_text(
        f"""#!{sys.executable}
import json
import pathlib
import sys

DATA = json.loads({json.dumps(json.dumps(payload))})
LOG = pathlib.Path({str(log)!r})
with LOG.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
operation = sys.argv[1]
reference = sys.argv[2]
if operation == "ls":
    print("\\n".join(DATA["tags"][reference]))
elif operation == "digest":
    repository, tag = reference.rsplit(":", 1)
    snapshot = DATA["snapshots"][tag]
    assert snapshot["repository"] == repository
    print(snapshot["index_digest"])
elif operation == "manifest":
    repository, digest = reference.rsplit("@", 1)
    for snapshot in DATA["snapshots"].values():
        if snapshot["repository"] != repository:
            continue
        if snapshot["index_digest"] == digest:
            print(json.dumps({{
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {{
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": member["manifest_digest"],
                        "platform": {{"os": "linux", "architecture": member["architecture"]}},
                    }}
                    for member in snapshot["platforms"]
                ],
            }}))
            raise SystemExit(0)
        for member in snapshot["platforms"]:
            if member["manifest_digest"] == digest:
                print(json.dumps({{
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {{"digest": member["config_digest"]}},
                }}))
                raise SystemExit(0)
    raise SystemExit("unknown digest")
else:
    raise SystemExit("unsupported operation")
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    return crane, log


def test_registry_tag_enumeration_consumes_complete_pages_deterministically() -> None:
    fixture = _registry_fixture()["repositories"]["docker.io/vllm/vllm-openai"]

    result = registry.enumerate_repository_tags(
        "docker.io/vllm/vllm-openai", fixture=fixture, max_tags=16
    )

    assert result["tags"] == [
        "latest",
        "v0.20.9",
        "v0.21.0",
        "v0.21.0-nightly",
        "v0.21.0.dev1",
    ]
    assert result["operations"] == [
        {
            "type": "fixture-tag-page-read",
            "capability": "read",
            "reference": "docker.io/vllm/vllm-openai",
            "page": 1,
        },
        {
            "type": "fixture-tag-page-read",
            "capability": "read",
            "reference": "docker.io/vllm/vllm-openai",
            "page": 2,
        },
    ]


def test_catalog_tag_selection_filters_versions_channels_and_variants() -> None:
    resolver = registry
    catalog = core.load_catalog()
    fixture = _registry_fixture()
    tag_lists = {
        repository: sorted(
            {tag for page in repository_fixture["pages"] for tag in page["tags"]}
        )
        for repository, repository_fixture in fixture["repositories"].items()
    }

    selected, exclusions = resolver.select_catalog_tags(catalog, tag_lists)

    assert [
        (
            item["product_id"],
            item["tag"],
            item["version"],
            item["channel"],
            item["variant"],
        )
        for item in selected
    ] == [
        ("vllm", "v0.21.0", "0.21.0", "stable", "default"),
        ("vllm-ascend", "v0.22.1rc1", "0.22.1rc1", "rc", "a2"),
        ("vllm-ascend", "v0.22.1rc1-a3", "0.22.1rc1", "rc", "a3"),
    ]
    assert {(item["tag"], item["reason"]) for item in exclusions} == {
        ("latest", "malformed-tag"),
        ("v0.20.9", "version-outside-specifier"),
        ("v0.21.0-nightly", "excluded-pattern"),
        ("v0.21.0.dev1", "excluded-pattern"),
        ("v0.22.1", "unsupported-channel"),
        ("v0.22.1rc1-a2", "excluded-pattern"),
        ("v0.22.1rc1-a5", "excluded-pattern"),
        ("v0.22.1rc1-custom", "excluded-pattern"),
    }


def test_current_catalog_fixture_freezes_one_scan_and_dynamic_github_matrices() -> None:
    """The checked-in 6/3 snapshot is fixture evidence, not matrix authority."""
    resolver = registry
    catalog = core.load_catalog()
    catalog["scan_limits"] = {
        "max_tags_per_repository": 32,
        "max_selected_upstreams": 8,
    }

    plan = resolver.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    assert plan["kind"] == "ucm-resolved-build-plan"
    assert plan["schema_version"] == 1
    assert plan["counts"] == {
        "scanned_tags": 11,
        "selected_upstreams": 3,
        "excluded_tags": 8,
        "wheel_tasks": 6,
        "image_tasks": 6,
        "family_tasks": 3,
    }
    assert len(plan["github_wheel_matrix"]["include"]) == plan["counts"]["wheel_tasks"]
    assert len(plan["github_image_matrix"]["include"]) == plan["counts"]["image_tasks"]
    assert (
        len(plan["github_family_matrix"]["include"]) == plan["counts"]["family_tasks"]
    )
    assert all(
        set(item) == {"task_id", "runner"}
        for item in plan["github_wheel_matrix"]["include"]
    )
    assert all(
        set(item) == {"task_id", "runner", "wheel_task_id"}
        for item in plan["github_image_matrix"]["include"]
    )
    assert all(
        set(item)
        == {
            "task_id",
            "family_task_id",
            "runner",
            "control_task_id",
            "control_arch",
        }
        and item["task_id"] == item["family_task_id"]
        for item in plan["github_family_matrix"]["include"]
    )
    assert {item["task_id"] for item in plan["expected_artifacts"]["wheels"]} == {
        item["task_id"] for item in plan["wheel_tasks"]
    }
    assert {item["task_id"] for item in plan["expected_artifacts"]["images"]} == {
        item["task_id"] for item in plan["image_tasks"]
    }
    assert {item["task_id"] for item in plan["expected_artifacts"]["families"]} == {
        item["task_id"] for item in plan["family_tasks"]
    }
    assert plan["config_sha256"] == core.sha256_value(catalog)
    assert plan["source_sha256"] == core.sha256_value(plan["source"])
    assert plan["scan_sha256"].startswith("sha256:")
    assert plan["resolved_plan_sha256"].startswith("sha256:")
    assert plan == resolver.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )


def test_arm64_only_family_binds_control_runner_and_tool_arch_in_plan() -> None:
    catalog = core.load_catalog()
    catalog["upstream_products"] = [
        product for product in catalog["upstream_products"] if product["id"] == "vllm"
    ]
    catalog["upstream_products"][0]["required_cpu_architectures"] = ["arm64"]
    catalog["compatibility"]["rules"] = [
        rule
        for rule in catalog["compatibility"]["rules"]
        if rule["accelerator"] == "cuda"
    ]
    catalog["compatibility"]["rules"][0]["cpu_architectures"] = ["arm64"]
    catalog["runtime_patch_rules"] = [
        rule for rule in catalog["runtime_patch_rules"] if rule["product"] == "vllm"
    ]
    catalog["chart"]["validation_cases"] = [
        case
        for case in catalog["chart"]["validation_cases"]
        if case["product_id"] == "vllm"
    ]
    catalog["pr_smoke"]["image_selectors"] = [
        {"product_id": "vllm", "variant": "default", "cpu_arch": "arm64"}
    ]
    fixture = _registry_fixture()
    fixture["repositories"] = {
        "docker.io/vllm/vllm-openai": fixture["repositories"][
            "docker.io/vllm/vllm-openai"
        ]
    }
    selected_snapshot = fixture["repositories"]["docker.io/vllm/vllm-openai"][
        "snapshots"
    ]["v0.21.0"]
    selected_snapshot["platforms"] = [
        member
        for member in selected_snapshot["platforms"]
        if member["architecture"] == "arm64"
    ]

    plan = registry.resolve_catalog(
        catalog,
        source_sha="b" * 40,
        lane="feature-candidate",
        fixture=fixture,
    )

    assert len(plan["image_tasks"]) == len(plan["family_tasks"]) == 1
    image_task = plan["image_tasks"][0]
    family_task = plan["family_tasks"][0]
    matrix = plan["github_family_matrix"]["include"][0]
    assert family_task["control_task_id"] == image_task["task_id"]
    assert family_task["control_arch"] == "arm64"
    assert family_task["control_runner"] == image_task["runner"]
    assert matrix == {
        "task_id": family_task["task_id"],
        "family_task_id": family_task["task_id"],
        "runner": image_task["runner"],
        "control_task_id": image_task["task_id"],
        "control_arch": "arm64",
    }


def test_real_image_authority_is_selected_from_one_exact_frozen_plan() -> None:
    """Real image authority carries both task identity and originating plan hash."""
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["image_tasks"][0]

    authority = image.real_image_authority_from_plan(
        plan,
        task_id=selected["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert {
        key: authority[key]
        for key in (
            "task_id",
            "family_task_id",
            "task_sha256",
            "resolved_plan_sha256",
        )
    } == {
        "task_id": selected["task_id"],
        "family_task_id": selected["family_task_id"],
        "task_sha256": selected["task_sha256"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
    }
    with pytest.raises(ValueError, match="plan hash"):
        image.real_image_authority_from_plan(
            plan,
            task_id=selected["task_id"],
            expected_plan_sha256=_digest("f"),
        )


def test_real_image_result_cannot_reopen_without_its_frozen_plan() -> None:
    """A real result must fail on missing plan context before schema reopening."""
    with pytest.raises(ValueError, match="frozen resolved plan"):
        image.validate_image_result({"candidate_kind": "real-candidate"})


def test_real_image_result_schema_requires_exact_plan_and_task_identity() -> None:
    """Fixture results stay isolated while every real result carries closure IDs."""
    schema = json.loads(
        (RELEASE_ROOT / "schemas" / "image-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    real_branch = next(
        branch
        for branch in schema["oneOf"]
        if branch.get("properties", {}).get("candidate_kind", {}).get("const")
        == "real-candidate"
    )

    assert {
        "task_id",
        "family_task_id",
        "task_sha256",
        "resolved_plan_sha256",
    }.issubset(real_branch["required"])


def test_real_image_result_emits_identity_from_selected_plan_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Result emission copies closure IDs from selection, never from family inference."""
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["image_tasks"][0]
    task = image.real_image_authority_from_plan(
        plan,
        task_id=selected["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    digest = _digest("9")
    authority = {
        "task": task,
        "wheel_inspection": {"declaration_sha256": digest},
        "wheel_embedded_build": {
            "accelerator": "cuda",
            "accelerator_runtime": "13.0",
            "npu_arch_or_na": "na",
            "os": "linux",
            "binary_profile_id": selected["profile_id"],
            "build_key": digest,
        },
    }
    payload = {
        "task_id": selected["task_id"],
        "family_task_id": selected["family_task_id"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "source": {
            "repository": plan["source"]["repository"],
            "repository_url": f"https://github.com/{plan['source']['repository']}",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "archive_sha256": digest,
            "context_sha256": digest,
        },
        "task_sha256": selected["task_sha256"],
        "build_key_sha256": digest,
        "family_id": selected["family_task_id"],
        "profile_id": selected["profile_id"],
        "spec_id": selected["spec_id"],
        "runtime_patch_variants": copy.deepcopy(selected["runtime_patch_variants"]),
        "target_platform": selected["platform"],
        "target_repository": selected["target_repository"],
        "target_tag": selected["target_tag"],
        "base": {"subject": selected["runtime"]["repository"] + "@" + digest},
        "wheel": {
            "filename": "uc_manager.whl",
            "sha256": digest,
            "size": 1,
            "version": selected["wheel_version"],
        },
        "runtime_dependencies": copy.deepcopy(
            selected["dependency_lock"]["runtime_dependencies"]
        ),
        "dependency_lock": {"sha256": digest},
        "implementation": {"aggregate_sha256": digest},
        "toolchain": copy.deepcopy(task["toolchain"]),
    }
    recipe = {"payload": payload, "payload_sha256": digest}
    evidence = {
        "schema_version": 1,
        "kind": "ucm-real-image-build-evidence",
        "recipe_sha256": digest,
        "build_key_sha256": digest,
        "base_verification": {
            "schema_version": 1,
            "kind": "ucm-base-verification",
            "base_subject": payload["base"]["subject"],
            "target_platform": selected["platform"],
            "status": "passed",
        },
        "install": {},
        "runtime": {},
        "oci": {
            "output": "local-oci",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "platform": selected["platform"],
            "published": False,
        },
        "oci_closure": {},
    }

    def _load_context(
        _path: Path, *, task_authority: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object], None, None]:
        assert task_authority == task
        return authority, recipe, None, None

    monkeypatch.setattr(image, "_load_real_context", _load_context)
    monkeypatch.setattr(
        image, "verify_real_runtime_evidence", lambda *_args: {"variant": "passed"}
    )
    monkeypatch.setattr(
        image,
        "real_content_identity",
        lambda *_args: {"content_identity_sha256": digest, "manifest_digest": digest},
    )
    monkeypatch.setattr(image, "validate_schema", lambda *_args: None)

    def _unexpected_catalog_reopen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("real image result reopened current catalog state")

    monkeypatch.setattr(image.release_core, "load_catalog", _unexpected_catalog_reopen)

    result = image.verify_real_image(
        tmp_path,
        evidence,
        output_mode="feature",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
        task_id=selected["task_id"],
    )

    assert {
        key: result[key]
        for key in (
            "task_id",
            "family_task_id",
            "task_sha256",
            "resolved_plan_sha256",
        )
    } == {
        "task_id": selected["task_id"],
        "family_task_id": selected["family_task_id"],
        "task_sha256": selected["task_sha256"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
    }
    assert result["wheel"]["requires_dist"] == selected["runtime_requirements"]


def test_verify_real_image_recomputes_context_with_selected_task_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening a real context must not resolve family/architecture again."""
    catalog = copy.deepcopy(core.load_catalog())
    dependency_bytes = {
        "alpha-runtime": b"reviewed alpha runtime wheel bytes",
        "packaging": b"reviewed packaging wheel bytes",
        "wrapt": b"reviewed non-current wrapt wheel bytes",
    }
    packaging = catalog["python_build_lock"]["packages"]["packaging"]
    packaging["sha256"] = (
        "sha256:" + hashlib.sha256(dependency_bytes["packaging"]).hexdigest()
    )
    template = next(
        declaration
        for declaration in catalog["python_runtime_dependencies"]
        if "wheel_artifacts" in declaration
    )

    def declaration(name: str, version: str, import_name: str) -> dict[str, object]:
        artifacts = copy.deepcopy(template["wheel_artifacts"])
        for abi, architectures in artifacts.items():
            for architecture, artifact in architectures.items():
                artifact["filename"] = (
                    f"{name.replace('-', '_')}-{version}-{abi}-{architecture}.whl"
                )
                artifact["sha256"] = (
                    "sha256:" + hashlib.sha256(dependency_bytes[name]).hexdigest()
                )
        return {
            "requirement": f"{name}=={version}",
            "import_name": import_name,
            "wheel_artifacts": artifacts,
        }

    catalog["python_runtime_dependencies"] = [
        {"python_build_lock": "packaging", "import_name": "packaging"},
        declaration("wrapt", "1.18.0", "wrapt"),
        declaration("alpha-runtime", "2.0", "alpha_runtime"),
    ]
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["image_tasks"][0]
    task = image.real_image_authority_from_plan(
        plan,
        task_id=selected["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    digest = _digest("8")
    wheel_path = tmp_path / "uc_manager.whl"
    wheel_path.write_bytes(b"wheel")
    wheel_sha256 = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    runtime_dependency_paths = []
    for record in task["runtime_dependencies"]:
        path = tmp_path / record["filename"]
        path.write_bytes(dependency_bytes[record["name"]])
        assert (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            == record["sha256"]
        )
        runtime_dependency_paths.append(path)
    inspection = {
        "filename": wheel_path.name,
        "sha256": wheel_sha256,
        "declaration_sha256": digest,
    }
    embedded_build = {
        "accelerator": "cuda",
        "accelerator_runtime": "13.0",
        "npu_arch_or_na": "na",
        "os": "linux",
        "binary_profile_id": selected["profile_id"],
        "build_key": digest,
    }
    bindings = {
        "source_sha": plan["source"]["commit"],
        "source_tree": "b" * 40,
        "source_archive_sha256": digest,
        "build_context_sha256": digest,
        "source_date_epoch": 1,
        "build_key": digest,
        "builder_coordinate": "example.invalid/builder@" + digest,
        "builder_config_digest": digest,
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "tool_wheels": {},
        "native_members": {},
        "elf_machines": [],
        "dt_needed": {},
        "dependency_closure": {},
        "unresolved_dependencies": [],
    }

    def _inspect_wheel(
        candidate_path: Path,
        _inspection: object,
        **kwargs: object,
    ) -> dict[str, object]:
        assert kwargs["task_authority"] == task
        return {
            "inspection": copy.deepcopy(inspection),
            "embedded_authority": {},
            "embedded_build": copy.deepcopy(embedded_build),
            "embedded_closure": {},
            "bindings": copy.deepcopy(bindings),
            "wheel_sha256": wheel_sha256,
            "wheel_size": candidate_path.stat().st_size,
        }

    monkeypatch.setattr(image, "inspect_real_wheel_candidate", _inspect_wheel)
    monkeypatch.setattr(
        image,
        "validate_real_base_authority",
        lambda record, _task: copy.deepcopy(record),
    )
    base = {
        "subject": selected["runtime"]["repository"] + "@" + digest,
        "index": {"digest": digest},
        "manifest": {"digest": digest},
        "config": {"digest": digest},
    }
    context = tmp_path / "context"
    recipe = image.prepare_real_context(
        wheel_path=wheel_path,
        wheel_inspection=inspection,
        base_record=base,
        runtime_dependency_paths=runtime_dependency_paths,
        output_dir=context,
        task_authority=task,
    )
    assert [
        (record["name"], record["version"])
        for record in recipe["payload"]["runtime_dependencies"]
    ] == [("alpha-runtime", "2.0"), ("packaging", "24.2"), ("wrapt", "1.18.0")]

    def _unexpected_current_catalog_resolution(
        *_args: object, **_kwargs: object
    ) -> None:
        raise AssertionError(
            "real context recomputation reopened current catalog state"
        )

    monkeypatch.setattr(
        image.release_core, "load_catalog", _unexpected_current_catalog_resolution
    )
    monkeypatch.setattr(
        image, "verify_real_runtime_evidence", lambda *_args: {"variant": "passed"}
    )
    monkeypatch.setattr(
        image,
        "real_content_identity",
        lambda *_args: {"content_identity_sha256": digest, "manifest_digest": digest},
    )
    monkeypatch.setattr(image, "validate_schema", lambda *_args: None)
    evidence = {
        "schema_version": 1,
        "kind": "ucm-real-image-build-evidence",
        "recipe_sha256": recipe["payload_sha256"],
        "build_key_sha256": recipe["payload"]["build_key_sha256"],
        "base_verification": {
            "schema_version": 1,
            "kind": "ucm-base-verification",
            "base_subject": base["subject"],
            "target_platform": selected["platform"],
            "status": "passed",
        },
        "install": {},
        "runtime": {},
        "oci": {
            "output": "local-oci",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "platform": selected["platform"],
            "published": False,
        },
        "oci_closure": {},
    }

    result = image.verify_real_image(
        context,
        evidence,
        output_mode="feature",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
        task_id=selected["task_id"],
    )

    assert result["task_id"] == selected["task_id"]


def test_unscoped_real_image_authority_entrypoints_are_not_exposed() -> None:
    """Real authority selection cannot reopen the current family matrix."""
    assert not hasattr(image, "real_image_authority")
    assert not hasattr(image, "real_image_authorities")


def test_real_image_task_authority_requires_resolved_plan_hash() -> None:
    """An image task object alone is not sufficient production authority."""
    assert not hasattr(image, "real_image_authority_from_task")


def test_real_base_builder_requires_exact_task_authority(tmp_path: Path) -> None:
    """Base bytes cannot be interpreted through family/architecture fallback."""
    with pytest.raises(TypeError):
        image.real_base_record_from_files(
            "family",
            "amd64",
            index_path=tmp_path / "index.json",
            manifest_path=tmp_path / "manifest.json",
            config_path=tmp_path / "config.json",
        )


def test_real_wheel_validator_requires_exact_task_authority(tmp_path: Path) -> None:
    """Wheel validation cannot resolve a task from caller-authored coordinates."""
    with pytest.raises(TypeError):
        image.inspect_real_wheel_candidate(
            "family",
            "amd64",
            tmp_path / "candidate.whl",
            {},
        )


def test_real_wheel_validator_rejects_tampered_task_authority_before_wheel_io(
    tmp_path: Path,
) -> None:
    """A self-inconsistent task authority fails before candidate wheel reads."""
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["image_tasks"][0]
    task = image.real_image_authority_from_plan(
        plan,
        task_id=selected["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    task["target_tag"] = "forged"

    with pytest.raises(ValueError, match="task authority identity"):
        image.inspect_real_wheel_candidate(
            tmp_path / "missing.whl",
            {},
            task_authority=task,
        )


def test_real_context_builder_requires_exact_task_authority(tmp_path: Path) -> None:
    """Context creation rejects missing task authority before reading artifacts."""
    with pytest.raises(TypeError):
        image.prepare_real_context(
            wheel_path=tmp_path / "candidate.whl",
            wheel_inspection={},
            base_record={},
            wrapt_path=tmp_path / "wrapt.whl",
            packaging_path=tmp_path / "packaging.whl",
            output_dir=tmp_path / "context",
        )


def test_real_authority_cli_requires_plan_hash_and_task() -> None:
    """The CLI cannot enumerate or accept a caller-authored task authority."""
    result = _run_cli("image", "real-authorities", check=False)

    assert result.returncode == 2
    assert "required" in result.stderr


def test_real_oci_requires_plan_binding_before_evidence_extraction(
    tmp_path: Path,
) -> None:
    """Missing plan identity fails before OCI reads or evidence-directory writes."""
    context = tmp_path / "context"
    context.mkdir()
    (context / image.CONTEXT_RECIPE).write_text(
        json.dumps({"payload": {"candidate_kind": "real-candidate"}}),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"

    with pytest.raises(ValueError, match="frozen resolved plan, hash, and task ID"):
        image.verify_oci(
            context,
            tmp_path / "missing.oci.tar",
            evidence_dir=evidence_dir,
        )

    assert not evidence_dir.exists()


def test_task_toolchain_authority_is_bound_to_selected_wheel_task() -> None:
    """A wheel build cannot borrow toolchain pins through a profile lookup."""
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["wheel_tasks"][0]

    authority = image.task_toolchain_authority(
        plan,
        task_kind="wheel",
        task_id=selected["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert authority["task_kind"] == "wheel"
    assert authority["task_id"] == selected["task_id"]
    assert authority["task_sha256"] == selected["task_sha256"]
    assert authority["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert authority["toolchain"]["kind"] == "ucm-real-image-toolchain-authority"


def test_stdlib_python_selector_supports_exact_image_task(
    tmp_path: Path,
) -> None:
    """Image setup Python is selected before third-party release code is installed."""
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["image_tasks"][0]
    plan_path = tmp_path / "resolved-plan.json"
    output_path = tmp_path / "github-output.txt"
    plan_path.write_bytes(core.canonical_bytes(plan) + b"\n")

    completed = subprocess.run(
        [
            sys.executable,
            str(RELEASE_ROOT / "select_task_python.py"),
            "--plan",
            str(plan_path),
            "--task-kind",
            "image",
            "--task-id",
            selected["task_id"],
            "--expected-sha256",
            plan["resolved_plan_sha256"],
            "--expected-source-sha",
            plan["source"]["commit"],
            "--github-output",
            str(output_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output_path.read_text(encoding="utf-8") == (
        f"python_version={selected['python_version']}\n"
    )

    rejected_output = tmp_path / "rejected-github-output.txt"
    rejected = subprocess.run(
        [
            sys.executable,
            str(RELEASE_ROOT / "select_task_python.py"),
            "--plan",
            str(plan_path),
            "--task-kind",
            "image",
            "--task-id",
            selected["task_id"],
            "--expected-sha256",
            plan["resolved_plan_sha256"],
            "--expected-source-sha",
            "b" * 40,
            "--github-output",
            str(rejected_output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "source" in rejected.stderr.lower()
    assert not rejected_output.exists()


def test_hosted_task_apis_reject_source_outside_the_frozen_plan() -> None:
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    binding = {
        "resolved_plan": plan,
        "expected_plan_sha256": plan["resolved_plan_sha256"],
    }

    with pytest.raises(ValueError, match="source"):
        verify.hosted_wheel_task(
            plan["wheel_tasks"][0], "b" * 40, 1_700_000_000, **binding
        )
    with pytest.raises(ValueError, match="source"):
        verify.hosted_image_task(
            plan["image_tasks"][0], "b" * 40, 1_700_000_000, **binding
        )


def test_pr_smoke_projection_selects_images_and_their_wheel_dependencies() -> None:
    catalog = core.load_catalog()

    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    smoke = plan["pr_smoke"]
    selected_images = {
        item["task_id"] for item in smoke["github_image_matrix"]["include"]
    }
    selected_wheels = {
        item["task_id"] for item in smoke["github_wheel_matrix"]["include"]
    }
    assert selected_images
    assert selected_images < {item["task_id"] for item in plan["image_tasks"]}
    assert selected_wheels == {
        item["wheel_task_id"]
        for item in plan["image_tasks"]
        if item["task_id"] in selected_images
    }
    assert all(
        set(item) == {"task_id", "runner", "wheel_task_id"}
        for item in smoke["github_image_matrix"]["include"]
    )
    assert all(
        set(item) == {"task_id", "runner"}
        for item in smoke["github_wheel_matrix"]["include"]
    )


def test_hosted_aggregation_projection_distinguishes_smoke_from_full_plan() -> None:
    """PR consumes its declared selectors while feature consumes every frozen task."""
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    smoke = verify.select_hosted_task_projection(
        plan,
        wheel_matrix=plan["pr_smoke"]["github_wheel_matrix"],
        image_matrix=plan["pr_smoke"]["github_image_matrix"],
    )
    full = verify.select_hosted_task_projection(
        plan,
        wheel_matrix=plan["github_wheel_matrix"],
        image_matrix=plan["github_image_matrix"],
    )

    assert len(smoke["wheel_tasks"]) == len(
        plan["pr_smoke"]["github_wheel_matrix"]["include"]
    )
    assert len(smoke["image_tasks"]) == len(
        plan["pr_smoke"]["github_image_matrix"]["include"]
    )
    assert len(full["wheel_tasks"]) == plan["counts"]["wheel_tasks"]
    assert len(full["image_tasks"]) == plan["counts"]["image_tasks"]
    assert {task["wheel_task_id"] for task in smoke["image_tasks"]} == {
        task["task_id"] for task in smoke["wheel_tasks"]
    }


@pytest.mark.parametrize("selection", ["smoke", "full"])
def test_aggregate_real_cli_executes_with_exact_selected_projection(
    selection: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The executable aggregator receives 2/2 for PR and N/N for feature."""
    cli = importlib.import_module("ucm_release.cli")
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    if selection == "smoke":
        wheel_matrix = plan["pr_smoke"]["github_wheel_matrix"]
        image_matrix = plan["pr_smoke"]["github_image_matrix"]
    else:
        wheel_matrix = plan["github_wheel_matrix"]
        image_matrix = plan["github_image_matrix"]
    plan_path = tmp_path / "resolved-plan.json"
    wheels_path = tmp_path / "wheel-matrix.json"
    images_path = tmp_path / "image-matrix.json"
    output_path = tmp_path / "aggregate.json"
    plan_path.write_bytes(core.canonical_bytes(plan) + b"\n")
    wheels_path.write_bytes(core.canonical_bytes(wheel_matrix) + b"\n")
    images_path.write_bytes(core.canonical_bytes(image_matrix) + b"\n")
    observed: dict[str, object] = {}

    def aggregate(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        wheel_count = len(kwargs["wheel_matrix"]["include"])
        image_count = len(kwargs["image_matrix"]["include"])
        return {
            "payload": {
                "families": [],
                "wheels": [{} for _ in range(wheel_count)],
                "images": [{} for _ in range(image_count)],
                "second_reconcile": {"task_count": 0},
            },
            "payload_sha256": _digest("7"),
        }

    monkeypatch.setattr(verify, "aggregate_real_hosted_evidence", aggregate)
    assert (
        cli.main(
            [
                "loop",
                "aggregate-real",
                "--wheel-dir",
                str(tmp_path / "wheels"),
                "--image-dir",
                str(tmp_path / "images"),
                "--repository",
                "SuperMarioYL/unified-cache-management",
                "--ref",
                "refs/heads/feature/test",
                "--source-sha",
                "a" * 40,
                "--resolved-plan",
                str(plan_path),
                "--expected-plan-sha256",
                plan["resolved_plan_sha256"],
                "--selected-wheel-matrix",
                str(wheels_path),
                "--selected-image-matrix",
                str(images_path),
                "--output",
                str(output_path),
                "--run-id",
                "42",
                "--attempt",
                "3",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert observed["resolved_plan"] == plan
    assert observed["expected_plan_sha256"] == plan["resolved_plan_sha256"]
    assert observed["wheel_matrix"] == wheel_matrix
    assert observed["image_matrix"] == image_matrix
    expected = 2 if selection == "smoke" else plan["counts"]["image_tasks"]
    assert len(observed["image_matrix"]["include"]) == expected


def test_registry_fixture_can_represent_a_complete_list_failure() -> None:
    fixture = copy.deepcopy(
        _registry_fixture()["repositories"]["docker.io/vllm/vllm-openai"]
    )
    fixture["list_error"] = "fixture registry unavailable"

    with pytest.raises(ValueError, match="fixture tag listing failed"):
        registry.enumerate_repository_tags(
            "docker.io/vllm/vllm-openai", fixture=fixture, max_tags=16
        )


def test_select_validates_resolved_plan_hash_and_returns_exactly_one_task() -> None:
    resolver = registry
    catalog = core.load_catalog()
    catalog["scan_limits"] = {
        "max_tags_per_repository": 32,
        "max_selected_upstreams": 8,
    }
    plan = resolver.resolve_catalog(
        catalog,
        source_sha="b" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    task_id = plan["image_tasks"][0]["task_id"]

    assert (
        resolver.select_task(
            plan,
            task_kind="image",
            task_id=task_id,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
        == plan["image_tasks"][0]
    )

    tampered = copy.deepcopy(plan)
    tampered["image_tasks"][0]["runner"] = "unreviewed-runner"
    with pytest.raises(ValueError, match="resolved plan hash mismatch"):
        resolver.select_task(
            tampered,
            task_kind="image",
            task_id=task_id,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )


def test_protected_drift_check_rereads_every_selected_tag_and_fails_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = core.load_catalog()
    _fake_catalog_crane(tmp_path, monkeypatch, name="crane-resolve")
    plan = registry.resolve_catalog(
        catalog,
        source_sha="c" * 40,
        lane="protected-tag",
    )
    assert plan["fixture_only"] is False

    _, stable_log = _fake_catalog_crane(tmp_path, monkeypatch, name="crane-stable")
    verified = registry.verify_upstream_drift(plan)
    assert verified["verified_tags"] == 3
    assert len(verified["operations"]) == 3
    assert len(stable_log.read_text(encoding="utf-8").splitlines()) == 3

    _, drift_log = _fake_catalog_crane(
        tmp_path,
        monkeypatch,
        name="crane-drift",
        digest_overrides={"v0.21.0": _digest("f")},
    )
    with pytest.raises(ValueError, match="upstream tag drift"):
        registry.verify_upstream_drift(plan)
    assert len(drift_log.read_text(encoding="utf-8").splitlines()) == 3


def test_github_release_create_selection_requires_exact_live_protected_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create request cannot exist without the complete frozen task authority."""
    catalog = core.load_catalog()
    _fake_catalog_crane(tmp_path, monkeypatch, name="crane-release-plan")
    source_sha = "d" * 40
    plan = registry.resolve_catalog(
        catalog,
        source_sha=source_sha,
        lane="protected-tag",
    )

    selection = verify.select_github_release_pages(
        [],
        source_sha,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert selection["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert selection["plan_binding"]["source_sha"] == source_sha
    assert selection["plan_binding"]["task_sets"] == {
        kind: [
            {
                "task_id": task["task_id"],
                "task_sha256": task["task_sha256"],
                "artifact_name": task["artifact_name"],
            }
            for task in plan[f"{kind}_tasks"]
        ]
        for kind in ("wheel", "image", "family")
    }
    assert plan["resolved_plan_sha256"] in selection["create_request"]["body"]
    assert (
        selection["plan_binding"]["plan_binding_sha256"]
        in selection["create_request"]["body"]
    )
    state = verify.plan_github_release(
        None,
        source_sha,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    assert state["plan_binding"] == selection["plan_binding"]

    with pytest.raises(ValueError, match="source|plan"):
        verify.select_github_release_pages(
            [],
            "e" * 40,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
    with pytest.raises(ValueError, match="protected plan|plan hash|resolved plan"):
        verify.select_github_release_pages(
            "malformed-pages",
            source_sha,
            resolved_plan=plan,
            expected_plan_sha256=_digest("f"),
        )


def test_github_release_identity_and_transport_are_derived_from_frozen_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = core.load_catalog()
    catalog["source"].update(
        {
            "repository": "FutureOrg/unified-cache-next",
            "release_tag": "v0.5.0rc1",
        }
    )
    catalog["chart"].update({"name": "future-cache-chart", "version": "1.2.3-rc.4"})
    _fake_catalog_crane(tmp_path, monkeypatch, name="crane-custom-source")
    source_sha = "5" * 40
    plan = registry.resolve_catalog(
        catalog,
        source_sha=source_sha,
        lane="protected-tag",
    )

    def _unexpected_catalog_reopen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("GitHub Release consumer reopened current catalog")

    monkeypatch.setattr(verify, "load_catalog", _unexpected_catalog_reopen)
    selected = verify.select_github_release_pages(
        [],
        source_sha,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert plan["source"] == {
        "repository": "FutureOrg/unified-cache-next",
        "staging_repository": catalog["source"]["staging_repository"],
        "default_branch": catalog["source"]["default_branch"],
        "release_tag": "v0.5.0rc1",
        "release_policy": catalog["source"]["release_policy"],
        "version_file": catalog["version_file"],
        "ucm_version": catalog["ucm_version"],
        "commit": source_sha,
    }
    assert plan["chart"]["name"] == "future-cache-chart"
    assert selected["create_request"]["tag_name"] == "v0.5.0rc1"
    assert selected["operations"][0]["reference"] == (
        "https://api.github.com/repos/FutureOrg/unified-cache-next/releases"
    )

    authority = selected["create_request"]
    release_id = 42
    remote = {
        "id": release_id,
        "assets": [],
        "upload_url": (
            "https://uploads.github.com/repos/FutureOrg/unified-cache-next/"
            f"releases/{release_id}/assets{{?name,label}}"
        ),
        "url": (
            "https://api.github.com/repos/FutureOrg/unified-cache-next/"
            f"releases/{release_id}"
        ),
        "assets_url": (
            "https://api.github.com/repos/FutureOrg/unified-cache-next/"
            f"releases/{release_id}/assets"
        ),
        "html_url": (
            "https://github.com/FutureOrg/unified-cache-next/releases/tag/"
            "untagged-0123456789abcdefabcd"
        ),
        "author": {"login": "github-actions[bot]", "type": "Bot"},
        "draft": True,
        "prerelease": True,
        "tag_name": authority["tag_name"],
        "name": authority["name"],
        "body": authority["body"],
    }
    state = verify.plan_github_release(
        remote,
        source_sha,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    assert state["upload_url"] == (
        "https://uploads.github.com/repos/FutureOrg/unified-cache-next/"
        f"releases/{release_id}/assets"
    )


def test_real_image_source_label_authority_is_derived_from_frozen_plan() -> None:
    catalog = core.load_catalog()
    catalog["source"].update(
        {
            "repository": "FutureOrg/unified-cache-next",
        }
    )
    plan = registry.resolve_catalog(
        catalog,
        source_sha="6" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    selected = plan["image_tasks"][0]

    authority = image.real_image_authority_from_plan(
        plan,
        task_id=selected["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert authority["source_repository"] == "FutureOrg/unified-cache-next"
    assert authority["source_repository_url"] == (
        "https://github.com/FutureOrg/unified-cache-next"
    )


def test_release_asset_validation_uses_frozen_chart_without_catalog_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = core.load_catalog()
    catalog["chart"].update({"name": "future-cache-chart", "version": "1.2.3-rc.4"})
    _fake_catalog_crane(tmp_path, monkeypatch, name="crane-frozen-chart")
    plan = registry.resolve_catalog(
        catalog,
        source_sha="7" * 40,
        lane="protected-tag",
    )
    authorities = verify._canonical_release_asset_authorities(
        plan["wheel_tasks"],
        chart_authority=plan["chart"],
        include_plan=True,
    )
    assets = []
    for index, authority in enumerate(authorities):
        path = tmp_path / authority["name"]
        path.write_text(f"asset-{index}\n", encoding="utf-8")
        assets.append(
            {
                **copy.deepcopy(authority),
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "path": str(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets",
        "source_sha": plan["source"]["commit"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "assets": assets,
    }
    manifest["assets_sha256"] = core.sha256_value(
        verify._release_asset_identity_payload(manifest)
    )

    monkeypatch.setattr(
        verify,
        "load_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("release asset validation reopened current catalog")
        ),
    )

    assert (
        verify.validate_release_asset_manifest(
            manifest,
            allowed_root=tmp_path,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
        == manifest
    )


def test_catalog_source_owner_is_derived_from_repository_identity() -> None:
    catalog = core.load_yaml(core.DEFAULT_RELEASE)
    catalog["source"].pop("owner", None)
    catalog["source"]["repository"] = "FutureOrg/unified-cache-next"

    core.validate_schema(
        catalog,
        core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json"),
    )
    plan = registry.resolve_catalog(
        catalog,
        source_sha="8" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    assert plan["source"]["repository"] == "FutureOrg/unified-cache-next"


def test_chart_runtime_selection_uses_only_frozen_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = registry.resolve_catalog(
        core.load_catalog(),
        source_sha="9" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    monkeypatch.setattr(
        chart,
        "load_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Chart reopened current catalog")
        ),
        raising=False,
    )

    cases = chart.resolve_chart_validation_cases(
        plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert [case["family_task_id"] for case in cases] == [
        task["task_id"] for task in plan["family_tasks"]
    ]


@pytest.mark.parametrize(
    "architectures", [["riscv64"], ["loong64", "riscv64", "s390x"]]
)
def test_registry_resolution_accepts_custom_repository_and_dynamic_members(
    architectures: list[str],
) -> None:
    repository = "registry.example/future/runtime"
    platforms = [
        {
            "os": "linux",
            "architecture": architecture,
            "manifest_digest": _digest(f"{index + 1:x}"),
            "config_digest": _digest(f"{index + 5:x}"),
        }
        for index, architecture in enumerate(architectures)
    ]
    fixture = {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": repository,
        "upstream_tag": "v1.2.3",
        "index_digest": _digest("f"),
        "platforms": platforms,
    }

    resolved = registry.resolve_repository_tag(
        repository,
        "v1.2.3",
        required_architectures=architectures,
        fixture=fixture,
    )

    assert list(resolved["snapshot"]["members"]) == sorted(architectures)


def test_release_state_cli_requires_independent_plan_hash_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI selection/state emit no write plan when the frozen hash is absent or wrong."""
    catalog = core.load_catalog()
    _fake_catalog_crane(tmp_path, monkeypatch, name="crane-release-cli")
    source_sha = "e" * 40
    plan = registry.resolve_catalog(
        catalog,
        source_sha=source_sha,
        lane="protected-tag",
    )
    plan_path = tmp_path / "resolved-plan.json"
    pages_path = tmp_path / "pages.json"
    select_request_path = tmp_path / "select-request.json"
    selection_path = tmp_path / "selection.json"
    state_request_path = tmp_path / "state-request.json"
    state_path = tmp_path / "state.json"
    plan_path.write_bytes(core.canonical_bytes(plan) + b"\n")
    pages_path.write_text("[]\n", encoding="utf-8")
    select_request_path.write_bytes(
        core.canonical_bytes(
            {
                "pages": str(pages_path),
                "source_sha": source_sha,
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
            }
        )
        + b"\n"
    )

    selected = _run_cli(
        "release",
        "select-pages",
        "--input",
        str(select_request_path),
        "--output",
        str(selection_path),
        check=False,
    )
    assert selected.returncode == 0, selected.stderr
    selection = core.load_json(selection_path)
    state_request_path.write_bytes(
        core.canonical_bytes(
            {
                **selection["plan_request"],
                "resolved_plan": str(plan_path),
            }
        )
        + b"\n"
    )
    planned = _run_cli(
        "release",
        "plan-state",
        "--input",
        str(state_request_path),
        "--output",
        str(state_path),
        check=False,
    )
    assert planned.returncode == 0, planned.stderr
    assert (
        core.load_json(state_path)["resolved_plan_sha256"]
        == plan["resolved_plan_sha256"]
    )

    state_path.unlink()
    request = core.load_json(state_request_path)
    request["resolved_plan_sha256"] = _digest("f")
    state_request_path.write_bytes(core.canonical_bytes(request) + b"\n")
    rejected = _run_cli(
        "release",
        "plan-state",
        "--input",
        str(state_request_path),
        "--output",
        str(state_path),
        check=False,
    )
    assert rejected.returncode != 0
    assert not state_path.exists()


def test_catalog_cli_validates_resolves_and_selects_from_the_frozen_file(
    tmp_path: Path,
) -> None:
    validated = json.loads(_run_cli("catalog", "validate").stdout)
    assert validated["kind"] == "ucm-catalog-validation"

    plan_path = tmp_path / "resolved-plan.json"
    resolved = json.loads(
        _run_cli(
            "catalog",
            "resolve",
            "--lane",
            "feature-candidate",
            "--source-sha",
            "d" * 40,
            "--fixture",
            str(RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"),
            "--output",
            str(plan_path),
        ).stdout
    )
    assert plan_path.read_bytes() == core.canonical_bytes(resolved) + b"\n"

    task_id = resolved["wheel_tasks"][0]["task_id"]
    selected = json.loads(
        _run_cli(
            "catalog",
            "select",
            "--plan",
            str(plan_path),
            "--task-kind",
            "wheel",
            "--task-id",
            task_id,
            "--expected-plan-sha256",
            resolved["resolved_plan_sha256"],
        ).stdout
    )
    assert selected == resolved["wheel_tasks"][0]


def test_resolution_fails_when_a_selected_tag_is_missing_required_architecture() -> (
    None
):
    resolver = registry
    catalog = core.load_catalog()
    catalog["scan_limits"] = {
        "max_tags_per_repository": 32,
        "max_selected_upstreams": 8,
    }
    fixture = _registry_fixture()
    fixture["repositories"]["docker.io/vllm/vllm-openai"]["snapshots"]["v0.21.0"][
        "platforms"
    ] = fixture["repositories"]["docker.io/vllm/vllm-openai"]["snapshots"]["v0.21.0"][
        "platforms"
    ][
        :1
    ]

    with pytest.raises(
        registry.RegistryBlocker, match="missing required linux architectures"
    ):
        resolver.resolve_catalog(
            catalog,
            source_sha="e" * 40,
            lane="feature-candidate",
            fixture=fixture,
        )


def test_target_coordinates_are_product_configuration_not_embedded_snapshot() -> None:
    resolver = registry
    catalog = core.load_catalog()

    plan = resolver.resolve_catalog(
        catalog,
        source_sha="f" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    assert {task["target_repository"] for task in plan["family_tasks"]} == {
        "ghcr.io/supermarioyl/vllm-openai",
        "ghcr.io/supermarioyl/vllm-ascend",
    }
    assert {task["target_tag"] for task in plan["family_tasks"]} == {
        "v0.21.0-ucm-0.5.0rc1-r1",
        "v0.22.1rc1-ucm-0.5.0rc1-r1",
        "v0.22.1rc1-a3-ucm-0.5.0rc1-r1",
    }


def test_removed_image_families_key_is_rejected_as_duplicate_authority() -> None:
    catalog = core.load_catalog()
    catalog["image_families"] = []

    with pytest.raises(ValueError, match="release catalog requires exact key set"):
        registry.resolve_catalog(
            catalog,
            source_sha="f" * 40,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )


def test_scan_and_matrix_overflow_fail_without_truncation() -> None:
    resolver = registry
    catalog = core.load_catalog()
    catalog["scan_limits"]["max_selected_upstreams"] = 2
    with pytest.raises(
        ValueError,
        match="max_selected_upstreams=2 exceeded by exact set of 3",
    ):
        resolver.resolve_catalog(
            catalog,
            source_sha="1" * 40,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )

    catalog["scan_limits"]["max_selected_upstreams"] = 8
    catalog["matrix_limits"]["max_family_tasks"] = 2
    with pytest.raises(
        ValueError,
        match="max_family_tasks=2 exceeded by exact generated set of 3",
    ):
        resolver.resolve_catalog(
            catalog,
            source_sha="1" * 40,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )


def test_incomplete_fixture_pagination_fails_closed() -> None:
    fixture = copy.deepcopy(
        _registry_fixture()["repositories"]["docker.io/vllm/vllm-openai"]
    )
    fixture["pages"][0]["next_page"] = None

    with pytest.raises(ValueError, match="pagination is incomplete"):
        registry.enumerate_repository_tags(
            "docker.io/vllm/vllm-openai", fixture=fixture, max_tags=16
        )


def test_catalog_validate_does_not_depend_on_legacy_compatibility_adapter(
    tmp_path: Path,
) -> None:
    catalog = core.load_yaml(core.DEFAULT_RELEASE)
    catalog["compatibility"]["excluded_upstream_patterns"].append("preview")
    catalog_path = tmp_path / "release.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")

    result = _run_cli(
        "catalog", "validate", "--catalog", str(catalog_path), check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["kind"] == "ucm-catalog-validation"


def test_fixture_plan_cannot_acquire_protected_publication_authority() -> None:
    catalog = core.load_catalog()
    fixture = _registry_fixture()

    feature_plan = registry.resolve_catalog(
        catalog,
        source_sha="2" * 40,
        lane="feature-candidate",
        fixture=fixture,
    )
    assert feature_plan["fixture_only"] is True

    with pytest.raises(ValueError, match="fixture.*protected"):
        registry.resolve_catalog(
            catalog,
            source_sha="2" * 40,
            lane="protected-tag",
            fixture=fixture,
        )

    forged = copy.deepcopy(feature_plan)
    forged["lane"] = "protected-tag"
    forged["resolved_plan_sha256"] = core.sha256_value(
        {key: value for key, value in forged.items() if key != "resolved_plan_sha256"}
    )
    task_id = forged["family_tasks"][0]["task_id"]
    with pytest.raises(ValueError, match="fixture.*protected"):
        registry.select_task(
            forged,
            task_kind="family",
            task_id=task_id,
            expected_plan_sha256=forged["resolved_plan_sha256"],
        )
    with pytest.raises(ValueError, match="fixture.*protected"):
        registry.verify_upstream_drift(forged, fixture=fixture)


def test_duplicate_canonical_variant_suffixes_are_rejected() -> None:
    catalog = core.load_catalog()
    catalog["upstream_products"][0]["variants"].append(
        {"id": "renamed-default", "tag_suffix": "", "npu_arch": "na"}
    )

    with pytest.raises(ValueError, match="duplicate canonical variant suffix"):
        registry.select_catalog_tags(
            catalog,
            {
                "docker.io/vllm/vllm-openai": ["v0.21.0"],
                "quay.io/ascend/vllm-ascend": ["v0.22.1rc1"],
            },
        )


def test_upstream_variant_tag_suffixes_are_catalog_declared_not_id_inferred() -> None:
    catalog = core.load_catalog()
    product = next(
        item for item in catalog["upstream_products"] if item["id"] == "vllm-ascend"
    )
    product["variants"][0]["id"] = "future-default"
    product["variants"][0]["tag_suffix"] = ""
    product["variants"][1]["id"] = "future-accelerator"
    product["variants"][1]["tag_suffix"] = "-x9"

    selected, exclusions = registry.select_catalog_tags(
        catalog,
        {
            "docker.io/vllm/vllm-openai": ["v0.21.0"],
            "quay.io/ascend/vllm-ascend": [
                "v0.22.1rc1",
                "v0.22.1rc1-x9",
            ],
        },
    )

    assert {
        (item["tag"], item["variant"])
        for item in selected
        if item["product_id"] == "vllm-ascend"
    } == {
        ("v0.22.1rc1", "future-default"),
        ("v0.22.1rc1-x9", "future-accelerator"),
    }
    assert not [item for item in exclusions if item["product_id"] == "vllm-ascend"]


def test_sparse_self_hashed_plan_is_not_a_valid_canonical_envelope() -> None:
    sparse = {
        "kind": "ucm-resolved-build-plan",
        "schema_version": 1,
        "fixture_only": False,
        "lane": "feature-candidate",
        "source": {
            "repository": "SuperMarioYL/unified-cache-management",
            "commit": "3" * 40,
        },
        "source_sha256": core.sha256_value(
            {
                "repository": "SuperMarioYL/unified-cache-management",
                "commit": "3" * 40,
            }
        ),
        "wheel_tasks": [],
        "image_tasks": [],
        "family_tasks": [],
    }
    sparse["resolved_plan_sha256"] = core.sha256_value(sparse)

    with pytest.raises(ValueError, match="top-level fields"):
        registry.validate_resolved_plan(sparse)


def test_self_consistent_rehashed_task_cannot_add_untyped_authority() -> None:
    """A valid envelope hash cannot turn an unknown task field into authority."""
    plan = registry.resolve_catalog(
        core.load_catalog(),
        source_sha="3" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    forged = copy.deepcopy(plan)
    task = forged["wheel_tasks"][0]
    task["untyped_authority"] = "attacker-controlled"
    task["task_sha256"] = core.sha256_value(
        {key: value for key, value in task.items() if key != "task_sha256"}
    )
    forged["resolved_plan_sha256"] = core.sha256_value(
        {key: value for key, value in forged.items() if key != "resolved_plan_sha256"}
    )

    with pytest.raises(ValueError, match="wheel task fields"):
        registry.validate_resolved_plan(forged)


def test_self_consistent_rehashed_image_cannot_diverge_from_wheel_task() -> None:
    plan = registry.resolve_catalog(
        core.load_catalog(),
        source_sha="4" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    forged = copy.deepcopy(plan)
    image_task = forged["image_tasks"][0]
    image_task["python_abi"] = "cp999"
    image_task["task_sha256"] = core.sha256_value(
        {key: value for key, value in image_task.items() if key != "task_sha256"}
    )
    forged["resolved_plan_sha256"] = core.sha256_value(
        {key: value for key, value in forged.items() if key != "resolved_plan_sha256"}
    )

    with pytest.raises(ValueError, match="image/wheel linkage"):
        registry.validate_resolved_plan(forged)


def test_catalog_select_can_require_an_independent_expected_plan_hash(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="4" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    plan_path = tmp_path / "resolved-plan.json"
    plan_path.write_bytes(core.canonical_bytes(plan) + b"\n")
    task_id = plan["wheel_tasks"][0]["task_id"]

    rejected = _run_cli(
        "catalog",
        "select",
        "--plan",
        str(plan_path),
        "--task-kind",
        "wheel",
        "--task-id",
        task_id,
        "--expected-plan-sha256",
        _digest("f"),
        check=False,
    )
    assert rejected.returncode == 2
    assert "expected plan hash mismatch" in rejected.stderr

    selected = json.loads(
        _run_cli(
            "catalog",
            "select",
            "--plan",
            str(plan_path),
            "--task-kind",
            "wheel",
            "--task-id",
            task_id,
            "--expected-plan-sha256",
            plan["resolved_plan_sha256"],
        ).stdout
    )
    assert selected == plan["wheel_tasks"][0]


def test_catalog_resolves_once_for_all_downstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resolved catalog plan must not reopen Registry state downstream."""
    catalog_path = tmp_path / "release.yaml"
    catalog_path.write_text(
        yaml.safe_dump(_catalog_for_single_resolution(), sort_keys=False),
        encoding="utf-8",
    )
    catalog = core.load_catalog(catalog_path)
    plan = registry.resolve_catalog(
        catalog,
        source_sha="9" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    expected_plan_sha256 = plan["resolved_plan_sha256"]

    def unexpected_rescan(*_args, **_kwargs):
        raise AssertionError("downstream consumer reopened Registry state")

    monkeypatch.setattr(registry, "enumerate_repository_tags", unexpected_rescan)
    monkeypatch.setattr(registry, "resolve_repository_tag", unexpected_rescan)
    wheel_task = registry.select_task(
        plan,
        task_kind="wheel",
        task_id=plan["wheel_tasks"][0]["task_id"],
        expected_plan_sha256=expected_plan_sha256,
    )
    image_task = registry.select_task(
        plan,
        task_kind="image",
        task_id=plan["image_tasks"][0]["task_id"],
        expected_plan_sha256=expected_plan_sha256,
    )

    assert {
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "binary_profile_id",
        "declaration_sha256",
    } <= set(wheel_task)
    assert (
        verify.hosted_wheel_task(
            wheel_task,
            "9" * 40,
            1_700_000_000,
            resolved_plan=plan,
            expected_plan_sha256=expected_plan_sha256,
        )["task_id"]
        == wheel_task["task_id"]
    )
    assert (
        verify.hosted_image_task(
            image_task,
            "9" * 40,
            1_700_000_000,
            resolved_plan=plan,
            expected_plan_sha256=expected_plan_sha256,
        )["task_id"]
        == image_task["task_id"]
    )
    assert (
        image.real_image_authority_from_plan(
            plan,
            task_id=image_task["task_id"],
            expected_plan_sha256=expected_plan_sha256,
        )["task_sha256"]
        == image_task["task_sha256"]
    )
    chart_cases = chart.resolve_chart_validation_cases(
        plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    assert [case["name"] for case in chart_cases] == ["cuda", "a2", "a3"]
    assert {
        (case["image_repository"], case["image_digest"]) for case in chart_cases
    } == {
        (task["runtime"]["repository"], task["runtime"]["index_digest"])
        for task in plan["family_tasks"]
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ("core", "plan"),
        ("core", "matrix", "--lane", "feature-candidate"),
        (
            "core",
            "hosted-matrix",
            "--source-sha",
            "a" * 40,
            "--source-date-epoch",
            "1700000000",
            "--output",
            "unused.json",
        ),
    ],
)
def test_legacy_core_plan_and_matrix_cli_routes_are_not_callable(
    arguments: tuple[str, ...],
) -> None:
    rejected = _run_cli(*arguments, check=False)

    assert rejected.returncode == 2
    assert "invalid choice" in rejected.stderr
    cli_text = (RELEASE_ROOT / "ucm_release" / "cli.py").read_text(encoding="utf-8")
    assert 'core_actions.add_parser("plan")' not in cli_text
    assert "--require-publishable" not in cli_text


def test_real_wheel_entrypoints_validate_selected_task_before_artifact_io(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="8" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    task = copy.deepcopy(plan["wheel_tasks"][0])
    task["runner"] = "tampered-runner"
    task_path = tmp_path / "tampered-wheel-task.json"
    task_path.write_bytes(core.canonical_bytes(task) + b"\n")
    missing = tmp_path / "must-not-be-opened"

    operations = [
        lambda: wheel.inspect_wheel(
            missing,
            task["spec_id"],
            _digest("1"),
            "builder-candidate",
            task_path=task_path,
        ),
        lambda: wheel.build_authority_record(
            tmp_path / "authority.json",
            task["spec_id"],
            "8" * 40,
            1_700_000_000,
            "example.invalid/builder@" + _digest("2"),
            missing,
            missing,
            missing,
            missing,
            missing,
            task_path,
        ),
        lambda: wheel.preflight_dependencies(
            missing, task["spec_id"], task_path=task_path
        ),
        lambda: wheel.audit_dependency_closure(
            missing,
            tmp_path / "closure.json",
            task["spec_id"],
            missing,
            task_path=task_path,
        ),
        lambda: wheel.seal_wheel(
            missing,
            tmp_path / "sealed",
            task["spec_id"],
            "8" * 40,
            _digest("3"),
            1_700_000_000,
            missing,
            missing,
            task_path=task_path,
        ),
    ]

    for operation in operations:
        with pytest.raises(ValueError, match="wheel task hash mismatch"):
            operation()


def test_real_image_entrypoints_use_task_authority_without_family_arch_lookup(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="7" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    task = plan["image_tasks"][0]
    authority = image.real_image_authority_from_plan(
        plan,
        task_id=task["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    authority["task_sha256"] = _digest("4")
    missing = tmp_path / "must-not-be-opened"

    with pytest.raises(ValueError, match="task authority identity"):
        image.real_base_record_from_files(
            index_path=missing,
            manifest_path=missing,
            config_path=missing,
            task_authority=authority,
        )
    with pytest.raises(ValueError, match="task authority identity"):
        image.prepare_real_context(
            wheel_path=missing,
            wheel_inspection={},
            base_record={},
            runtime_dependency_paths=[missing],
            output_dir=tmp_path / "context",
            task_authority=authority,
        )


def test_v2_catalog_operations_never_open_legacy_compatibility_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catalog, fixture wheel, plan, and Chart selection share one YAML authority."""
    original_load_yaml = core.load_yaml

    def reject_legacy_yaml(path: Path) -> dict[str, object]:
        if Path(path).name == "compatibility.yaml":
            raise AssertionError("production reopened the deleted compatibility YAML")
        return original_load_yaml(path)

    monkeypatch.setattr(core, "load_yaml", reject_legacy_yaml)
    catalog = core.load_catalog()
    manifest = core._build_fixture_release_manifest()
    fixture = wheel.build_fixture_wheel(
        tmp_path / "wheel", "6" * 40, manifest["wheel_specs"][0]["spec_id"]
    )
    plan = registry.resolve_catalog(
        catalog,
        source_sha="6" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    assert fixture["inspection"]["spec_id"] == plan["wheel_tasks"][0]["spec_id"]
    assert chart.resolve_chart_validation_cases(
        plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )


def test_legacy_compatibility_adapter_cli_schema_and_file_are_removed() -> None:
    """The v1 YAML adapter cannot remain as an alternate runtime authority."""
    assert not hasattr(core, "DEFAULT_COMPATIBILITY")
    assert not hasattr(core, "COMPATIBILITY_KEYS")
    assert not hasattr(core, "compatibility_adapter")
    assert not hasattr(core, "validate_config")
    assert not (RELEASE_ROOT / "compatibility.yaml").exists()
    schema = core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json")
    assert schema.get("$ref") == "#/$defs/release"
    assert "compatibility" not in schema["$defs"]
    result = _run_cli(
        "config", "validate", "--compatibility", "missing.yaml", check=False
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_manifest_and_image_result_schemas_have_no_legacy_compatibility_fields() -> (
    None
):
    """Published-shaped results bind the catalog/plan, not the removed adapter."""
    manifest = core._build_fixture_release_manifest()
    assert "compatibility_sha256" not in manifest
    for name in ("release-manifest.schema.json", "image-result.schema.json"):
        schema_text = (RELEASE_ROOT / "schemas" / name).read_text(encoding="utf-8")
        assert "compatibility_sha256" not in schema_text
        assert "compatibility_rule_id" not in schema_text
        assert "compatibility_rule_sha256" not in schema_text


def test_v2_catalog_still_rejects_overlapping_compatibility_rules(
    tmp_path: Path,
) -> None:
    """Deleting the adapter must not weaken v2 rule ambiguity validation."""
    catalog = core.load_catalog()
    duplicate = copy.deepcopy(catalog["compatibility"]["rules"][0])
    duplicate["id"] = "overlapping-copy"
    catalog["compatibility"]["rules"].append(duplicate)
    catalog_path = tmp_path / "release.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="overlap"):
        core.load_catalog(catalog_path)


def test_v2_catalog_rejects_semantic_range_overlap_without_a_current_target() -> None:
    """Future targets cannot make two previously accepted rule selectors ambiguous."""
    catalog = core.load_catalog()
    product = next(
        item for item in catalog["upstream_products"] if item["id"] == "vllm"
    )
    product["version_specifier"] = ">=0.30,<0.31"
    broad = next(
        item
        for item in catalog["compatibility"]["rules"]
        if item["id"] == "cuda-supported"
    )
    broad["version_specifier"] = ">=0.18,<0.23"
    subset = copy.deepcopy(broad)
    subset.update(
        {
            "id": "cuda-021-subset",
            "version_specifier": "==0.21.*",
            "cpu_architectures": ["amd64"],
            "upstream_channels": ["stable", "rc"],
        }
    )
    catalog["compatibility"]["rules"].append(subset)

    with pytest.raises(ValueError, match="semantic.*overlap|overlap.*semantic"):
        core.validate_catalog(catalog)


def _catalog_with_cuda_version_rules(
    left_specifier: str, right_specifier: str
) -> dict[str, object]:
    catalog = core.load_catalog()
    left = next(
        item
        for item in catalog["compatibility"]["rules"]
        if item["id"] == "cuda-supported"
    )
    left["version_specifier"] = left_specifier
    right = copy.deepcopy(left)
    right.update(
        {
            "id": "cuda-version-peer",
            "version_specifier": right_specifier,
        }
    )
    catalog["compatibility"]["rules"].append(right)
    return catalog


def test_v2_catalog_rejects_public_exact_and_local_exact_overlap() -> None:
    """A public equality includes local builds of the same public version."""
    witness = Version("1.0+foo")
    assert SpecifierSet("==1.0").contains(witness, prereleases=True)
    assert SpecifierSet("==1.0+foo").contains(witness, prereleases=True)

    with pytest.raises(ValueError, match="semantic.*overlap|overlap.*semantic"):
        core.validate_catalog(_catalog_with_cuda_version_rules("==1.0", "==1.0+foo"))


def test_v2_catalog_rejects_local_exact_at_inclusive_public_upper_bound() -> None:
    """Inclusive public bounds include a local build at that boundary."""
    witness = Version("1.0+foo")
    assert SpecifierSet("==1.0+foo").contains(witness, prereleases=True)
    assert SpecifierSet("<=1.0").contains(witness, prereleases=True)

    with pytest.raises(ValueError, match="semantic.*overlap|overlap.*semantic"):
        core.validate_catalog(_catalog_with_cuda_version_rules("==1.0+foo", "<=1.0"))


@pytest.mark.parametrize(
    ("left_specifier", "right_specifier"),
    [
        ("==1.0+foo", "==1.0+bar"),
        ("==1.0", "==2.0"),
    ],
)
def test_v2_catalog_allows_disjoint_exact_local_versions(
    left_specifier: str,
    right_specifier: str,
) -> None:
    """Distinct exact versions remain provably disjoint under PEP 440."""
    core.validate_catalog(
        _catalog_with_cuda_version_rules(left_specifier, right_specifier)
    )


@pytest.mark.parametrize(
    "dimension",
    ["version", "channel", "variant", "cpu-architecture"],
)
def test_v2_catalog_allows_compatibility_rules_with_a_disjoint_dimension(
    dimension: str,
) -> None:
    """Two rules remain unambiguous when at least one selector cannot intersect."""
    catalog = core.load_catalog()
    if dimension == "variant":
        rule = next(
            item
            for item in catalog["compatibility"]["rules"]
            if item["id"] == "ascend-supported"
        )
        rule["variants"] = ["a2"]
        other = copy.deepcopy(rule)
        other.update({"id": "ascend-a3-only", "variants": ["a3"]})
    else:
        rule = next(
            item
            for item in catalog["compatibility"]["rules"]
            if item["id"] == "cuda-supported"
        )
        other = copy.deepcopy(rule)
        other["id"] = f"cuda-disjoint-{dimension}"
        if dimension == "version":
            rule["version_specifier"] = ">=0.18,<0.21"
            other["version_specifier"] = ">=0.21,<0.23"
        elif dimension == "channel":
            rule["upstream_channels"] = ["stable"]
            other["upstream_channels"] = ["rc"]
        else:
            rule["cpu_architectures"] = ["amd64"]
            other["cpu_architectures"] = ["arm64"]
    catalog["compatibility"]["rules"].append(other)

    core.validate_catalog(catalog)
