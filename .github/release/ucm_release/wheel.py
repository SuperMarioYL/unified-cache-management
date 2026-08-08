"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import email.parser
import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from .core import DEFAULT_COMPATIBILITY, DEFAULT_RELEASE, DEFAULT_SCHEMA_DIR, expand_wheel_specs, validate_config


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_wheel(
    path: Path,
    spec_id: str,
    expected_sha256: str,
    *,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    if DIGEST_RE.fullmatch(expected_sha256) is None:
        raise ValueError("expected SHA256 must be sha256:<64 lowercase hex>")
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    specs = {item["spec_id"]: item for item in expand_wheel_specs(release)}
    if spec_id not in specs:
        raise ValueError(f"unknown wheel spec: {spec_id}")
    spec = specs[spec_id]
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"wheel SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        filename_name, filename_version, _, filename_tags = parse_wheel_filename(path.name)
    except Exception as error:
        raise ValueError(f"invalid wheel filename: {error}") from error
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        wheel_names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError("wheel must contain exactly one METADATA and one WHEEL file")
        parser = email.parser.Parser()
        metadata = parser.parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        wheel_metadata = parser.parsestr(archive.read(wheel_names[0]).decode("utf-8"))
    distribution = metadata.get("Name", "")
    version = metadata.get("Version", "")
    if canonicalize_name(distribution) != "uc-manager":
        raise ValueError(f"unexpected wheel distribution: {distribution}")
    if canonicalize_name(distribution) != canonicalize_name(str(filename_name)):
        raise ValueError("METADATA distribution does not match wheel filename")
    if version != str(filename_version):
        raise ValueError("METADATA version does not match wheel filename")
    if version != release["ucm_version"]:
        raise ValueError(f"wheel version {version} does not match UCM version {release['ucm_version']}")
    filename_tag_strings = {str(tag) for tag in filename_tags}
    metadata_tags = set(wheel_metadata.get_all("Tag", []))
    if not metadata_tags or not metadata_tags.issubset(filename_tag_strings):
        raise ValueError("WHEEL tags do not match wheel filename tags")
    cpu_suffixes = {"amd64": ("x86_64",), "arm64": ("aarch64", "arm64")}[spec["cpu_arch"]]
    if not any(
        tag.startswith(f"{spec['python_abi']}-{spec['python_abi']}-")
        and tag.endswith(cpu_suffixes)
        and ("manylinux" in tag or "linux" in tag)
        for tag in filename_tag_strings
    ):
        raise ValueError("wheel tags do not match declared Python ABI and CPU architecture")
    requires_dist = metadata.get_all("Requires-Dist", [])
    if requires_dist != release["python_runtime_dependencies"]:
        raise ValueError("wheel runtime dependencies do not match release.yaml")
    return {
        "schema_version": 1,
        "kind": "ucm-wheel-inspection",
        "spec_id": spec_id,
        "filename": path.name,
        "sha256": actual_sha256,
        "size": path.stat().st_size,
        "distribution": distribution,
        "version": version,
        "tags": sorted(filename_tag_strings),
        "requires_dist": requires_dist,
        "python_abi": spec["python_abi"],
        "cpu_arch": spec["cpu_arch"],
        "declaration_sha256": spec["declaration_sha256"],
    }
