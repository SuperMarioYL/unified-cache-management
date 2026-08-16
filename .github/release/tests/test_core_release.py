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

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

release_core = importlib.import_module("ucm_release.core")
release_registry = importlib.import_module("ucm_release.registry")
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


def _fixture_resolved_plan(
    catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    """Resolve test tasks from the explicitly local, non-publishable fixture."""
    return release_registry.resolve_catalog(
        catalog or release_core.load_catalog(),
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
) -> subprocess.CompletedProcess[str]:
    release_path = _write_catalog(tmp_path, release)
    return _run(
        "catalog",
        "resolve",
        "--catalog",
        str(release_path),
        "--fixture",
        str(RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"),
        "--lane",
        "feature-candidate",
        "--source-sha",
        "0" * 40,
        "--output",
        str(tmp_path / "resolved-plan.json"),
        check=False,
    )


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
        ("missing-profile", "no compatible wheel profile"),
        ("extra-profile", "overlapping wheel profiles"),
        ("unresolved-lock", "missing required properties"),
        ("caller-raw-runner", "Additional properties are not allowed"),
    ],
)
def test_release_authority_mutations_fail_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    release = copy.deepcopy(release_core.load_catalog())
    if mutation == "missing-profile":
        release["wheel_profiles"].pop()
    elif mutation == "extra-profile":
        extra = _clone(release["wheel_profiles"][-1])
        assert isinstance(extra, dict)
        extra["id"] = "cann900-a5"
        release["wheel_profiles"].append(extra)
    elif mutation == "unresolved-lock":
        del release["python_build_lock"]["packages"]["wheel"]["sha256"]
    elif mutation == "caller-raw-runner":
        release["wheel_profiles"][0]["runner"] = "self-hosted"
    rejected = _reject_fixture_resolution(tmp_path / mutation, release)
    assert rejected.returncode == 2
    assert message in rejected.stderr


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


def test_fixture_resolution_rejects_compatibility_without_matching_profile(
    tmp_path: Path,
) -> None:
    """A compatibility rule that no wheel profile satisfies must fail closed."""
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    release["compatibility"]["rules"][0]["accelerator_runtimes"] = ["cuda-12.9"]

    drift = _reject_fixture_resolution(tmp_path, release)

    assert drift.returncode == 2
    assert "no compatible wheel profile" in drift.stderr


def test_setup_chart_and_configuration_share_version_authority() -> None:
    """version.ini, setup.py, the catalog, and the chart share one version."""
    version = (
        (ROOT / "version.ini").read_text(encoding="utf-8").strip().split("=", 1)[1]
    )
    setup_version = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    release_config = release_core.load_catalog()
    chart = yaml.safe_load((ROOT / "charts" / "ucm" / "Chart.yaml").read_text())
    assert setup_version == version == release_config["ucm_version"]
    assert str(chart["appVersion"]) == version
    assert release_core.python_runtime_requirements(release_config) == [
        "packaging==24.2",
        "wrapt==1.17.2",
    ]
    assert derive_chart_version(version) == chart["version"]


def test_coordinated_config_version_drift_is_rejected(tmp_path: Path) -> None:
    """A catalog version that diverges from version.ini is rejected at validate time."""
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    release["ucm_version"] = release["chart"]["app_version"] = "0.5.0rc2"
    release["chart"]["version"] = "0.5.0-rc.2"
    release["source"]["release_tag"] = "v0.5.0rc2"
    release_path = _write_catalog(tmp_path, release)
    drift = _run(
        "config",
        "validate",
        "--release",
        str(release_path),
        check=False,
    )
    assert drift.returncode == 2
    assert "does not match version.ini" in drift.stderr


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
