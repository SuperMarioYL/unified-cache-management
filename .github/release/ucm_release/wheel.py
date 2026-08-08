"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import email.parser
import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from .core import DEFAULT_COMPATIBILITY, DEFAULT_RELEASE, DEFAULT_SCHEMA_DIR, expand_wheel_specs, validate_config


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_wheel_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and "\\" not in name
        and all(part not in {"", ".", ".."} for part in name.split("/"))
        and path.as_posix() == name
    )


def _verify_record(archive: zipfile.ZipFile, record_name: str) -> None:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if len(names) != len(set(names)) or any(not _safe_wheel_name(name) for name in names):
        raise ValueError("wheel contains duplicate, unsafe, or noncanonical members")
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    if any(len(row) != 3 for row in rows):
        raise ValueError("wheel RECORD must contain exactly three columns")
    by_name = {row[0]: row for row in rows}
    if len(by_name) != len(rows) or set(by_name) != set(names):
        raise ValueError("wheel RECORD does not exactly cover archive files")
    for name in names:
        _, encoded_digest, encoded_size = by_name[name]
        if name == record_name:
            if encoded_digest or encoded_size:
                raise ValueError("wheel RECORD self-entry must have empty digest and size")
            continue
        if not encoded_digest.startswith("sha256="):
            raise ValueError(f"wheel RECORD entry lacks SHA256: {name}")
        encoded = encoded_digest.partition("=")[2]
        try:
            expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise ValueError(f"wheel RECORD has invalid SHA256: {name}") from error
        content = archive.read(name)
        if hashlib.sha256(content).digest() != expected:
            raise ValueError(f"wheel RECORD SHA256 mismatch: {name}")
        if not encoded_size.isdecimal() or int(encoded_size) != len(content):
            raise ValueError(f"wheel RECORD size mismatch: {name}")


def _unique_json(data: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _verify_production_evidence(
    archive: zipfile.ZipFile,
    wheel_metadata: email.message.Message,
    spec: dict[str, Any],
) -> dict[str, Any]:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise ValueError("production wheel requires exactly one RECORD")
    _verify_record(archive, record_names[0])
    native_names = [
        name
        for name in names
        if name.endswith(".so")
        and "ucm_custom_ops" in PurePosixPath(name).name
        and archive.read(name).startswith(b"\x7fELF")
    ]
    if wheel_metadata.get("Root-Is-Purelib", "").lower() != "false" or not native_names:
        raise ValueError("production wheel requires a native custom-op shared object with ELF evidence")
    build_names = [name for name in names if name.endswith(".dist-info/ucm-build.json")]
    if len(build_names) != 1:
        raise ValueError("production wheel requires exactly one embedded ucm-build.json")
    binding = _unique_json(archive.read(build_names[0]), build_names[0])
    required = {
        "schema_version", "spec_id", "source_commit", "build_context_digest",
        "accelerator", "accelerator_runtime", "npu_arch_or_na", "os", "cpu_arch",
        "python_abi", "binary_profile_id",
    }
    if set(binding) != required:
        raise ValueError(
            "embedded build binding fields mismatch: "
            f"missing={sorted(required - set(binding))}, extra={sorted(set(binding) - required)}"
        )
    if binding["schema_version"] != 1:
        raise ValueError("embedded build binding schema_version must be 1")
    if re.fullmatch(r"[0-9a-f]{40}", str(binding["source_commit"])) is None:
        raise ValueError("embedded build binding requires immutable source_commit")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", str(binding["build_context_digest"])) is None:
        raise ValueError("embedded build binding requires immutable build_context_digest")
    bound_fields = (
        "spec_id", "accelerator", "accelerator_runtime", "npu_arch_or_na", "os",
        "cpu_arch", "python_abi", "binary_profile_id",
    )
    for field in bound_fields:
        if binding[field] != spec[field]:
            raise ValueError(
                f"embedded build binding {field} does not match planned spec: "
                f"{binding[field]!r} != {spec[field]!r}"
            )
    return {
        "source_commit": binding["source_commit"],
        "build_context_digest": binding["build_context_digest"],
        "native_artifacts": sorted(native_names),
        "record_status": "passed",
    }


def inspect_wheel(
    path: Path,
    spec_id: str,
    expected_sha256: str,
    source_kind: str,
    *,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    if source_kind not in {"fixture", "production"}:
        raise ValueError("source_kind must be fixture or production")
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
    production_evidence: dict[str, Any] | None = None
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        wheel_names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError("wheel must contain exactly one METADATA and one WHEEL file")
        parser = email.parser.Parser()
        metadata = parser.parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        wheel_metadata = parser.parsestr(archive.read(wheel_names[0]).decode("utf-8"))
        if source_kind == "production":
            production_evidence = _verify_production_evidence(archive, wheel_metadata, spec)
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
    result = {
        "schema_version": 1,
        "kind": "ucm-wheel-inspection",
        "source_kind": source_kind,
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
        "status": "fixture-only" if source_kind == "fixture" else "candidate-inspected",
        "publication_eligible": False,
    }
    if production_evidence is not None:
        result["production_evidence"] = production_evidence
    return result
