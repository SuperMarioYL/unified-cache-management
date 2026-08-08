"""RED structural contract for the slim UCM release package.

This test deliberately describes the target tree.  It must stay red until the
legacy release subsystem has been replaced by the compact package.
"""

from __future__ import annotations

from pathlib import Path


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
    "Dockerfile",
    "install_ucm.py",
    "inspect_runtime.py",
    "verify_base_image.py",
}


def _source_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return [candidate for candidate in path.rglob("*") if candidate.is_file()]


def test_release_tree_obeys_the_slim_structural_budget() -> None:
    """Require the final package layout without silently accepting omissions."""
    violations: list[str] = []

    package_files = [path for path in _source_files(PACKAGE_DIR) if path.suffix == ".py"]
    if not PACKAGE_DIR.is_dir():
        violations.append("missing .github/release/ucm_release package")
    if len(package_files) > 8:
        violations.append(
            f"release package has {len(package_files)} Python files; budget is at most 8"
        )

    schema_files = [path for path in _source_files(SCHEMA_DIR) if path.suffix == ".json"]
    if len(schema_files) != 3:
        violations.append(
            f"release package has {len(schema_files)} JSON schemas; contract requires exactly 3"
        )

    docker_files = {path.name for path in _source_files(DOCKER_DIR)}
    if docker_files != EXPECTED_DOCKER_FILES:
        violations.append(
            "release Docker files must be exactly "
            f"{sorted(EXPECTED_DOCKER_FILES)}, found {sorted(docker_files)}"
        )

    present_legacy_roots = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in LEGACY_RELEASE_ROOTS
        if path.exists()
    ]
    if present_legacy_roots:
        violations.append(f"legacy release roots remain: {present_legacy_roots}")

    release_sources = [*LEGACY_RELEASE_ROOTS, REPO_ROOT / ".github" / "workflows"]
    opt_release_references = [
        path.relative_to(REPO_ROOT).as_posix()
        for source in release_sources
        for path in _source_files(source)
        if "/opt/ucm-release" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    if opt_release_references:
        violations.append(
            "/opt/ucm-release is forbidden; references remain in "
            f"{sorted(opt_release_references)}"
        )

    standalone_wrapt_paths = [
        path.relative_to(REPO_ROOT).as_posix()
        for source in LEGACY_RELEASE_ROOTS
        for path in _source_files(source)
        if "wrapt" in path.name.lower()
        or "wrapt-bundle" in path.read_text(encoding="utf-8", errors="ignore")
        or "wrapt_bundle" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    if standalone_wrapt_paths:
        violations.append(
            "standalone wrapt release bundle remains in "
            f"{sorted(standalone_wrapt_paths)}"
        )

    assert not violations, "release slimming structural contract failed:\n- " + "\n- ".join(
        violations
    )
