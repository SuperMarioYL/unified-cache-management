"""RED structural contract for the slim UCM release package.

This test deliberately describes the target tree.  It must stay red until the
legacy release subsystem has been replaced by the compact package.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / ".github" / "release"))
PACKAGE_DIR = REPO_ROOT / ".github" / "release" / "ucm_release"
LEGACY_RELEASE_ROOTS = (
    REPO_ROOT / "release",
    REPO_ROOT / "scripts" / "release",
    REPO_ROOT / "docker" / "release",
)


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


def test_release_tree_rejects_legacy_release_artifacts() -> None:
    """Retain the safety invariants without constraining future growth."""
    violations: list[str] = []

    if not PACKAGE_DIR.is_dir():
        violations.append("missing .github/release/ucm_release package")

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


def test_release_yaml_is_the_exact_publication_authority() -> None:
    release = yaml.safe_load(
        (REPO_ROOT / ".github" / "release" / "release.yaml").read_text(encoding="utf-8")
    )

    assert release["publish"] == {
        "pypi": {
            "enabled": False,
            "index": "https://upload.pypi.org/legacy/",
            "dists": [
                "uc-manager-cuda",
                "uc-manager-cann-a2",
                "uc-manager-cann-a3",
            ],
        },
        "ghcr": {"enabled": True, "namespace": "ghcr.io/{owner}"},
        "dockerhub": {"enabled": False, "namespace": "docker.io/{owner}"},
        "chart_oci": {
            "enabled": True,
            "namespace": "ghcr.io/{owner}/charts",
        },
        "github_release": {"enabled": True},
    }
    assert set(release["source"]) == {
        "staging_repository",
        "default_branch",
        "protected_environment",
    }
    assert set(release["chart"]) == {"source", "name", "validation_cases"}


def test_publish_plan_is_the_normalized_config_without_runtime_layers() -> None:
    core = importlib.import_module("ucm_release.core")

    plan = core.compute_publish_plan(core.load_catalog())

    assert plan == {
        "pypi": {
            "enabled": False,
            "index": "https://upload.pypi.org/legacy/",
            "dists": [
                "uc-manager-cuda",
                "uc-manager-cann-a2",
                "uc-manager-cann-a3",
            ],
        },
        "ghcr": {"enabled": True, "namespace": "ghcr.io/release-org"},
        "dockerhub": {
            "enabled": False,
            "namespace": "docker.io/release-org",
        },
        "chart_oci": {
            "enabled": True,
            "namespace": "ghcr.io/release-org/charts",
        },
        "github_release": {"enabled": True},
    }


def test_enabled_public_channel_requires_github_release_draft_barrier(
    tmp_path: Path,
) -> None:
    core = importlib.import_module("ucm_release.core")
    release = yaml.safe_load(
        (REPO_ROOT / ".github" / "release" / "release.yaml").read_text(encoding="utf-8")
    )
    release["publish"]["github_release"]["enabled"] = False
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Draft barrier"):
        core.load_catalog(path)


def test_dockerhub_publication_requires_ghcr_source_channel(tmp_path: Path) -> None:
    core = importlib.import_module("ucm_release.core")
    release = yaml.safe_load(
        (REPO_ROOT / ".github" / "release" / "release.yaml").read_text(encoding="utf-8")
    )
    release["publish"]["dockerhub"]["enabled"] = True
    release["publish"]["ghcr"]["enabled"] = False
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Docker Hub publication requires GHCR"):
        core.load_catalog(path)


@pytest.mark.parametrize(
    ("channel", "extra"),
    [
        ("pypi", {"namespace": "invalid"}),
        ("ghcr", {"index": "invalid"}),
        ("dockerhub", {"dists": ["invalid"]}),
        ("chart_oci", {"index": "invalid"}),
        ("github_release", {"namespace": "invalid"}),
    ],
)
def test_publish_channel_shapes_are_exact(
    tmp_path: Path, channel: str, extra: dict[str, object]
) -> None:
    core = importlib.import_module("ucm_release.core")
    release = yaml.safe_load(
        (REPO_ROOT / ".github" / "release" / "release.yaml").read_text(encoding="utf-8")
    )
    release["publish"][channel].update(extra)
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Additional properties are not allowed"):
        core.load_catalog(path)
