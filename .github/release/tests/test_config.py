"""RED structural contract for the slim UCM release package.

This test deliberately describes the target tree.  It must stay red until the
legacy release subsystem has been replaced by the compact package.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / ".github" / "release" / "ucm_release"
SCHEMA_DIR = REPO_ROOT / ".github" / "release" / "schemas"
DOCKER_DIR = REPO_ROOT / ".github" / "release" / "docker"
LEGACY_RELEASE_ROOTS = (
    REPO_ROOT / "release",
    REPO_ROOT / "scripts" / "release",
    REPO_ROOT / "docker" / "release",
)
EXPECTED_DOCKER_FILES = {
    "Dockerfile.builder",
    "Dockerfile.runtime",
    "Dockerfile.wheel",
    "install_ucm.py",
    "inspect_runtime.py",
    "mooncake_installer.sh",
    "verify_base_image.py",
}


def _source_files(path: Path, *, excluded_parts: set[str] | None = None) -> list[Path]:
    if not path.exists():
        return []
    exclusions = excluded_parts or set()
    return [
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file()
        and not exclusions.intersection(candidate.relative_to(path).parts)
    ]


def _docker_layout_violation(docker_dir: Path) -> str | None:
    actual = {
        path.relative_to(docker_dir).as_posix()
        for path in _source_files(docker_dir, excluded_parts={"__pycache__"})
    }
    if actual != EXPECTED_DOCKER_FILES:
        return (
            "release Docker files must be exactly "
            f"{sorted(EXPECTED_DOCKER_FILES)}, found {sorted(actual)}"
        )
    return None


def _forbidden_release_content_paths(repo_root: Path) -> tuple[list[str], list[str]]:
    """Scan both old and new implementation roots, never the contract tests."""
    modern_root = repo_root / ".github" / "release"
    roots = (
        (modern_root, {"tests", "__pycache__"}),
        (repo_root / "release", {"__pycache__"}),
        (repo_root / "scripts" / "release", {"__pycache__"}),
        (repo_root / "docker" / "release", {"__pycache__"}),
        (repo_root / ".github" / "workflows", {"__pycache__"}),
    )
    opt_references: list[str] = []
    standalone_wrapt_paths: list[str] = []
    for source, excluded_parts in roots:
        for path in _source_files(source, excluded_parts=excluded_parts):
            relative = path.relative_to(repo_root).as_posix()
            content = path.read_text(encoding="utf-8", errors="ignore")
            if "/opt/ucm-release" in content:
                opt_references.append(relative)
            if (
                "wrapt" in path.name.lower()
                or "wrapt-bundle" in content
                or "wrapt_bundle" in content
            ):
                standalone_wrapt_paths.append(relative)
    return sorted(opt_references), sorted(standalone_wrapt_paths)


def test_release_tree_obeys_the_slim_structural_budget() -> None:
    """Require the final package layout without silently accepting omissions."""
    violations: list[str] = []

    package_files = [
        path for path in _source_files(PACKAGE_DIR) if path.suffix == ".py"
    ]
    if not PACKAGE_DIR.is_dir():
        violations.append("missing .github/release/ucm_release package")
    if len(package_files) > 8:
        violations.append(
            f"release package has {len(package_files)} Python files; budget is at most 8"
        )

    schema_files = [
        path for path in _source_files(SCHEMA_DIR) if path.suffix == ".json"
    ]
    if len(schema_files) != 3:
        violations.append(
            f"release package has {len(schema_files)} JSON schemas; contract requires exactly 3"
        )

    docker_violation = _docker_layout_violation(DOCKER_DIR)
    if docker_violation:
        violations.append(docker_violation)

    present_legacy_roots = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in LEGACY_RELEASE_ROOTS
        if path.exists()
    ]
    if present_legacy_roots:
        violations.append(f"legacy release roots remain: {present_legacy_roots}")

    opt_release_references, standalone_wrapt_paths = _forbidden_release_content_paths(
        REPO_ROOT
    )
    if opt_release_references:
        violations.append(
            "/opt/ucm-release is forbidden; references remain in "
            f"{sorted(opt_release_references)}"
        )

    if standalone_wrapt_paths:
        violations.append(
            "standalone wrapt release bundle remains in "
            f"{sorted(standalone_wrapt_paths)}"
        )

    failure_message = "release slimming structural contract failed:\n- " + "\n- ".join(
        violations
    )
    assert not violations, failure_message


def test_forbidden_content_scan_covers_the_new_release_tree(tmp_path: Path) -> None:
    """A post-deletion implementation cannot bypass the old-root-only scan."""
    modern_package = tmp_path / ".github" / "release" / "ucm_release"
    modern_package.mkdir(parents=True)
    (modern_package / "runner.py").write_text("tool = '/opt/ucm-release/run'\n")
    (modern_package / "wrapt_bundle.py").write_text("pass\n")
    (tmp_path / ".github" / "release" / "tests").mkdir()
    (tmp_path / ".github" / "release" / "tests" / "test_contract.py").write_text(
        "example = '/opt/ucm-release is only test text'\n"
    )

    opt_paths, wrapt_paths = _forbidden_release_content_paths(tmp_path)

    assert opt_paths == [".github/release/ucm_release/runner.py"]
    assert wrapt_paths == [".github/release/ucm_release/wrapt_bundle.py"]


def test_docker_layout_rejects_a_nested_duplicate_of_an_allowed_name(
    tmp_path: Path,
) -> None:
    """Compare relative paths so duplicate basenames cannot satisfy the budget."""
    for filename in EXPECTED_DOCKER_FILES:
        (tmp_path / filename).write_text("placeholder\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "Dockerfile").write_text("duplicate\n")

    violation = _docker_layout_violation(tmp_path)

    assert violation is not None
    assert "nested/Dockerfile" in violation


def test_release_config_profiles_are_dynamic_and_have_no_runner_escape_hatch() -> None:
    release = yaml.safe_load(
        (REPO_ROOT / ".github" / "release" / "release.yaml").read_text(encoding="utf-8")
    )

    profiles = release["wheel_profiles"]
    assert profiles
    assert len({item["id"] for item in profiles}) == len(profiles)
    assert all(item["cpu_arch"] for item in profiles)
    assert all(len(set(item["cpu_arch"])) == len(item["cpu_arch"]) for item in profiles)
    assert {item["id"]: item["builder_manylinux"] for item in profiles} == {
        "cuda130": "manylinux_2_28",
        "cann900-a2": "manylinux_2_34",
        "cann900-a3": "manylinux_2_34",
    }
    assert all("runner" not in item for item in release["wheel_profiles"])
    assert set(release["runner_map"]) == {
        architecture for profile in profiles for architecture in profile["cpu_arch"]
    }


def test_toolchain_lock_owns_builder_requirements_but_no_builder_coordinates() -> None:
    toolchain = yaml.safe_load(
        (REPO_ROOT / ".github" / "release" / "toolchain.lock.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert set(toolchain["builders"]) == {"cuda130", "cann900-a2", "cann900-a3"}
    for profile_builders in toolchain["builders"].values():
        assert set(profile_builders) == {"amd64", "arm64"}
        for requirement in profile_builders.values():
            assert set(requirement) == {"sources", "copy_paths", "checks"}

    core = importlib.import_module("ucm_release.core")
    catalog = core.load_catalog()
    for profile in catalog["wheel_profiles"]:
        assert set(profile["builders"]) == set(profile["cpu_arch"])
        assert all(
            "root" not in requirement for requirement in profile["builders"].values()
        )


def _publish_catalog() -> dict:
    core = importlib.import_module("ucm_release.core")
    return core.load_catalog()


_ALL_ALLOW = {
    "pypi": "true",
    "ghcr": "true",
    "dockerhub": "true",
    "chart_oci": "true",
    "github_release": "true",
}


def test_publish_plan_defaults_to_all_off_when_dry_run() -> None:
    """dry_run=true blocks every channel regardless of other layers."""
    core = importlib.import_module("ucm_release.core")
    plan = core.compute_publish_plan(
        _publish_catalog(),
        lane="protected-tag",
        allow=_ALL_ALLOW,
        request="",
        dry_run=True,
    )
    assert all(value is False for value in plan.values())


def test_publish_plan_feature_lane_never_publishes() -> None:
    """Feature lanes are build-only; publication requires protected-tag."""
    core = importlib.import_module("ucm_release.core")
    plan = core.compute_publish_plan(
        _publish_catalog(),
        lane="feature-candidate",
        allow=_ALL_ALLOW,
        request="",
        dry_run=False,
    )
    assert all(value is False for value in plan.values())


def test_publish_plan_respects_config_repo_and_run_layers() -> None:
    """Config-disabled channels stay off even when repo vars allow them."""
    core = importlib.import_module("ucm_release.core")
    plan = core.compute_publish_plan(
        _publish_catalog(),
        lane="protected-tag",
        allow={
            "pypi": "true",
            "ghcr": "true",
            "chart_oci": "true",
            "github_release": "true",
        },
        request="",
        dry_run=False,
    )
    assert plan["pypi"] is True
    assert plan["ghcr"] is True
    assert plan["dockerhub"] is False
    assert plan["chart_oci"] is True
    assert plan["github_release"] is True


def test_publish_plan_request_subset_only_enables_named_channels() -> None:
    """A non-empty request narrows to only the named channels."""
    core = importlib.import_module("ucm_release.core")
    plan = core.compute_publish_plan(
        _publish_catalog(),
        lane="protected-tag",
        allow=_ALL_ALLOW,
        request="ghcr",
        dry_run=False,
    )
    assert plan["ghcr"] is True
    assert plan["pypi"] is False
    assert plan["chart_oci"] is False


def test_publish_plan_repo_variable_unset_means_disabled() -> None:
    """A missing repo variable (empty string) blocks the channel."""
    core = importlib.import_module("ucm_release.core")
    plan = core.compute_publish_plan(
        _publish_catalog(),
        lane="protected-tag",
        allow={},
        request="",
        dry_run=False,
    )
    assert all(value is False for value in plan.values())


def test_publish_plan_rejects_unknown_channel() -> None:
    """An unknown channel in the request is a hard error, not a silent skip."""
    core = importlib.import_module("ucm_release.core")
    with pytest.raises(ValueError, match="unknown publish channels"):
        core.compute_publish_plan(
            _publish_catalog(),
            lane="protected-tag",
            allow={},
            request="pypi,unknown",
            dry_run=False,
        )
