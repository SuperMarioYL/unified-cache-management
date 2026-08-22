"""Planner pure-function contract for the slim release package.

Only pure input/output tests of the planner are retained: matrix expansion
(platform loaders and driver boundaries), compatibility filtering (profiles
fail closed on overlap/absence), and version derivation consistency across
version.ini, setup.py, the catalog, and the Helm chart.  The wheel-seal,
native-ELF, ldd-closure, source-context, and fixture-wheel change-detector
suites were removed per the slimming plan -- they asserted fixture bytes
rather than planner behaviour.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

release_core = importlib.import_module("ucm_release.core")
release_registry = importlib.import_module("ucm_release.registry")
release_builders = importlib.import_module("ucm_release.builders")
derive_chart_version = release_core.derive_chart_version

ASCEND_EXTERNAL_REQUIRED = {
    "dependency": "libascend_hal.so",
    "provider": "host-ascend-driver",
    "expected_mount_root": "/usr/local/Ascend/driver/lib64",
    "relation": "transitive",
    "required_at": "device-runtime",
}


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": PYTHONPATH}
    return subprocess.run(
        [sys.executable, "-m", "ucm_release", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _fixture_registry() -> dict[str, object]:
    return release_core.load_json(
        RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"
    )


def _builder_catalog() -> dict[str, object]:
    return release_builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=RELEASE_ROOT / "tests" / "fixtures" / "builders",
        owner="release-org",
    )


def _resolved_builder_root() -> dict[str, object]:
    digest = "sha256:" + "d" * 64
    return {
        "index_digest": digest,
        "manifest_digest": digest,
        "config_digest": digest,
        "operations": [],
    }


def _fixture_resolved_plan(
    catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    """Resolve test tasks from the explicitly local, non-publishable fixture."""
    with mock.patch.object(
        release_registry, "resolve_builder_root", return_value=_resolved_builder_root()
    ):
        return release_registry.resolve_catalog(
            catalog or release_core.load_catalog(),
            builder_catalog=_builder_catalog(),
            source_sha="0" * 40,
            lane="feature-candidate",
            fixture=_fixture_registry(),
        )


def _clone(value: object) -> object:
    return json.loads(json.dumps(value))


def _write_catalog(directory: Path, release: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    release_path = directory / "release.yaml"
    release_path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    return release_path


def _reject_fixture_resolution(
    tmp_path: Path,
    release: dict,
) -> str:
    release_path = _write_catalog(tmp_path, release)
    try:
        catalog = release_core.load_catalog(release_path)
        _fixture_resolved_plan(catalog)
    except ValueError as error:
        return str(error)
    raise AssertionError("fixture resolution unexpectedly succeeded")


def test_fixture_plan_projects_platform_loaders_and_driver_boundary() -> None:
    """The local fixture keeps loaders explicit and only Ascend may defer HAL."""
    plan = _fixture_resolved_plan()
    assert plan["fixture_only"] is True
    platform_loaders = {"ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1"}

    for task in plan["wheel_tasks"]:
        assert platform_loaders <= set(task["allowed_dt_needed"])
        if task["accelerator"] == "ascend":
            assert task["external_required_dependencies"] == [ASCEND_EXTERNAL_REQUIRED]
        else:
            assert task["external_required_dependencies"] == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("extra-profile", "overlapping wheel profiles"),
        ("unresolved-lock", "missing required properties"),
        ("missing-builder-manylinux", "missing required properties"),
        ("caller-raw-runner", "Additional properties are not allowed"),
    ],
)
def test_release_authority_mutations_fail_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    release = copy.deepcopy(release_core.load_catalog())
    if mutation == "extra-profile":
        extra = _clone(release["wheel_profiles"][-1])
        assert isinstance(extra, dict)
        extra["id"] = "cann900-a5"
        release["wheel_profiles"].append(extra)
    elif mutation == "unresolved-lock":
        del release["python_build_lock"]["packages"]["wheel"]["sha256"]
    elif mutation == "missing-builder-manylinux":
        del release["wheel_profiles"][0]["builder_manylinux"]
    elif mutation == "caller-raw-runner":
        release["wheel_profiles"][0]["runner"] = "self-hosted"
    rejected = _reject_fixture_resolution(tmp_path / mutation, release)
    assert message in rejected


def test_missing_profile_excludes_unsupported_target_from_feature_plan() -> None:
    release = copy.deepcopy(release_core.load_catalog())
    removed_profile = release["wheel_profiles"].pop()
    assert removed_profile["id"] == "cann900-a3"
    fixture = _fixture_registry()
    del fixture["repositories"]["quay.io/ascend/vllm-ascend"]["snapshots"][
        "v0.22.2rc1-a3"
    ]

    with mock.patch.object(
        release_registry, "resolve_builder_root", return_value=_resolved_builder_root()
    ):
        plan = release_registry.resolve_catalog(
            release,
            builder_catalog=_builder_catalog(),
            source_sha="0" * 40,
            lane="feature-candidate",
            fixture=fixture,
        )

    assert any(
        item["product_id"] == "vllm-ascend"
        and item["tag"].endswith("-a3")
        and item["reason"] == "compatibility-unsupported"
        for item in plan["exclusions"]
    )
    assert plan["image_tasks"]
    assert all(task["runtime"]["variant"] != "a3" for task in plan["image_tasks"])
    assert all(task["runtime"]["variant"] != "a3" for task in plan["family_tasks"])


def test_feature_preflight_planner_mode_has_no_write_authority() -> None:
    """A feature lane is build-only: the planner never grants publication authority."""
    feature = json.loads(
        _run(
            "core",
            "tag-preflight",
            "--lane",
            "feature-candidate",
            "--catalog-planner",
        ).stdout
    )
    assert feature["publication_allowed"] is False
    assert feature["write_authority"] == []


def test_fixture_resolution_excludes_compatibility_without_matching_profile() -> None:
    """An unsupported target is explained and omitted from a feature plan."""
    release = release_core.load_catalog()
    release["compatibility"]["rules"][0]["accelerator_runtimes"] = ["cuda-12.9"]
    fixture = _fixture_registry()
    del fixture["repositories"]["docker.io/vllm/vllm-openai"]["snapshots"]["v0.21.2"]

    with mock.patch.object(
        release_registry, "resolve_builder_root", return_value=_resolved_builder_root()
    ):
        plan = release_registry.resolve_catalog(
            release,
            builder_catalog=_builder_catalog(),
            source_sha="0" * 40,
            lane="feature-candidate",
            fixture=fixture,
        )

    assert any(
        item["product_id"] == "vllm" and item["reason"] == "compatibility-unsupported"
        for item in plan["exclusions"]
    )
    assert plan["image_tasks"]
    assert all(task["runtime"]["product_id"] != "vllm" for task in plan["image_tasks"])
    assert all(task["product_id"] != "vllm" for task in plan["family_tasks"])
    assert all(task["profile_id"] != "cuda130" for task in plan["wheel_tasks"])


def test_setup_chart_and_configuration_share_version_authority() -> None:
    """version.ini is authoritative for setup.py, the catalog, and the Chart."""
    setup_version = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    release_config = release_core.load_catalog()
    assert setup_version == release_config["ucm_version"] == release_core.read_version()
    assert (
        release_core.derive_chart_version(release_config["ucm_version"])
        == release_config["chart"]["version"]
    )
    assert release_core.python_runtime_requirements(release_config) == ["wrapt==1.17.2"]


def test_coordinated_config_version_drift_is_rejected() -> None:
    """All published version forms derive from one explicit version number."""
    release = release_core.load_catalog(version_override="0.5.0rc2")
    assert release["ucm_version"] == "0.5.0rc2"
    assert release["source"]["release_tag"] == "v0.5.0rc2"
    assert release["chart"]["app_version"] == "0.5.0rc2"
    assert release["chart"]["version"] == release_core.derive_chart_version("0.5.0rc2")
    with pytest.raises(ValueError):
        release_core.load_catalog(version_override="not-a-version")


def test_json_array_loader_preserves_duplicate_key_rejection(tmp_path: Path) -> None:
    """REST arrays stay type-explicit without weakening duplicate-key parsing."""
    valid = tmp_path / "valid-array.json"
    valid.write_text('[{"id":1}]\n', encoding="utf-8")
    assert release_core.load_json_array(valid) == [{"id": 1}]
    with pytest.raises(ValueError, match="JSON object"):
        release_core.load_json(valid)

    duplicate = tmp_path / "duplicate-array.json"
    duplicate.write_text('[{"id":1,"id":2}]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        release_core.load_json_array(duplicate)


def test_wheel_tasks_carry_dist_name_and_unique_filenames() -> None:
    """Each wheel task carries its profile dist_name and the derived wheel
    filename is globally unique, so cann900-a2/a3 no longer collide on the
    shared ``linux`` wheel_platform."""
    wheel = importlib.import_module("ucm_release.wheel")
    plan = _fixture_resolved_plan()
    assert len(plan["wheel_tasks"]) >= 4
    filenames: set[str] = set()
    for task in plan["wheel_tasks"]:
        assert task["dist_name"], f"dist_name missing on {task['task_id']}"
        assert task["dist_name"].startswith("uc-manager")
        arch = release_core.cpu_toolchain_authority(task["cpu_arch"]).wheel_arch
        filename = (
            f"{wheel._dist_filename_component(task['dist_name'])}-"
            f"{task['wheel_version']}-{task['python_abi']}-{task['python_abi']}-"
            f"{task['wheel_platform']}_{arch}.whl"
        )
        assert filename not in filenames, f"wheel filename collision: {filename}"
        filenames.add(filename)
    # cann900-a2 and cann900-a3 both use wheel_platform=linux but must differ.
    dist_names = {task["dist_name"] for task in plan["wheel_tasks"]}
    assert "uc-manager-cann-a2" in dist_names and "uc-manager-cann-a3" in dist_names


def test_dist_name_helpers_use_profile_name() -> None:
    """The sealer/fixture name helpers derive output from the profile dist_name."""
    wheel = importlib.import_module("ucm_release.wheel")
    assert wheel._dist_filename_component("uc-manager-cann-a2") == "uc_manager_cann_a2"
    assert wheel._dist_filename_component("uc-manager") == "uc_manager"
    metadata = wheel._canonical_metadata(
        "uc-manager-cann-a3", "0.7.55", ["wrapt==1.17.2"]
    )
    assert b"Name: uc-manager-cann-a3" in metadata
    assert b"Version: 0.7.55" in metadata
    assert b"Requires-Dist: wrapt==1.17.2" in metadata
