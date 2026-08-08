"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import ast
import base64
import csv
import email.parser
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from .core import (
    DEFAULT_COMPATIBILITY,
    DEFAULT_RELEASE,
    DEFAULT_SCHEMA_DIR,
    canonical_bytes,
    expand_wheel_specs,
    validate_config,
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIXTURE_MARKER = "ucm/_fixture_build.py"


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_canonical(path: Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def build_fixture_wheel(
    output_dir: Path,
    source_sha: str,
    profile_id: str,
    *,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    """Build one deterministic, source-bound wheel for the fork candidate lane."""
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("fixture wheel source SHA must be a full lowercase Git commit")
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    specs = {item["spec_id"]: item for item in expand_wheel_specs(release)}
    if profile_id not in specs:
        raise ValueError(f"unknown fixture wheel profile: {profile_id}")
    spec = specs[profile_id]
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("fixture wheel output directory must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    version = release["ucm_version"]
    platform = {"amd64": "x86_64", "arm64": "aarch64"}[spec["cpu_arch"]]
    tag = f"{spec['python_abi']}-{spec['python_abi']}-linux_{platform}"
    filename = f"uc_manager-{version}-{tag}.whl"
    dist_info = f"uc_manager-{version}.dist-info"
    members = {
        "ucm/__init__.py": f"__version__ = {version!r}\n",
        "ucm/_fixture_build.py": (
            f"SOURCE_SHA = {source_sha!r}\nPROFILE_ID = {profile_id!r}\n"
        ),
        f"{dist_info}/METADATA": "\n".join(
            [
                "Metadata-Version: 2.1",
                "Name: uc-manager",
                f"Version: {version}",
                "Requires-Dist: wrapt==1.17.2",
                "",
            ]
        ),
        f"{dist_info}/WHEEL": "\n".join(
            [
                "Wheel-Version: 1.0",
                "Generator: ucm-fork-fixture-only",
                "Root-Is-Purelib: false",
                f"Tag: {tag}",
                "",
            ]
        ),
    }
    record_rows: list[list[str]] = []
    for name, content in members.items():
        raw = content.encode("utf-8")
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
            .decode("ascii")
            .rstrip("=")
        )
        record_rows.append([name, f"sha256={digest}", str(len(raw))])
    record_name = f"{dist_info}/RECORD"
    record_rows.append([record_name, "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(record_rows)
    members[record_name] = record_buffer.getvalue()

    wheel_path = output_dir / filename
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name])
    wheel_sha256 = _sha256(wheel_path)
    inspection = inspect_wheel(
        wheel_path,
        profile_id,
        wheel_sha256,
        "fixture",
        release_path=release_path,
        compatibility_path=compatibility_path,
        schema_dir=schema_dir,
    )
    inspection_path = output_dir / "wheel-inspection.json"
    _write_canonical(inspection_path, inspection)
    inspection_sha256 = _sha256(inspection_path)
    build_record = {
        "schema_version": 1,
        "kind": "ucm-fixture-wheel-build",
        "fixture_only": True,
        "publication_status": "unpublished",
        "publication_eligible": False,
        "source_sha": source_sha,
        "profile_id": profile_id,
        "wheel_sha256": wheel_sha256,
        "inspection_sha256": inspection_sha256,
    }
    _write_canonical(output_dir / "fixture-build.json", build_record)
    (output_dir / "wheel.sha256").write_text(wheel_sha256 + "\n", encoding="utf-8")
    return {
        "wheel_path": str(wheel_path),
        "wheel_sha256": wheel_sha256,
        "inspection_sha256": inspection_sha256,
        "inspection": inspection,
        "build_record": build_record,
    }


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
    if len(names) != len(set(names)) or any(
        not _safe_wheel_name(name) for name in names
    ):
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
                raise ValueError(
                    "wheel RECORD self-entry must have empty digest and size"
                )
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


def _verify_builder_candidate_evidence(
    archive: zipfile.ZipFile,
    wheel_metadata: email.message.Message,
    spec: dict[str, Any],
) -> dict[str, Any]:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if FIXTURE_MARKER in names:
        raise ValueError("builder candidate must not contain a fixture binding marker")
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise ValueError("builder candidate requires exactly one RECORD")
    _verify_record(archive, record_names[0])
    native_names = [
        name
        for name in names
        if name.endswith(".so")
        and "ucm_custom_ops" in PurePosixPath(name).name
        and archive.read(name).startswith(b"\x7fELF")
    ]
    if wheel_metadata.get("Root-Is-Purelib", "").lower() != "false" or not native_names:
        raise ValueError(
            "builder candidate requires a native custom-op shared object with ELF evidence"
        )
    build_names = [name for name in names if name.endswith(".dist-info/ucm-build.json")]
    if len(build_names) != 1:
        raise ValueError(
            "builder candidate requires exactly one embedded ucm-build.json"
        )
    binding = _unique_json(archive.read(build_names[0]), build_names[0])
    required = {
        "schema_version",
        "spec_id",
        "source_commit",
        "build_context_digest",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_abi",
        "binary_profile_id",
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
    if (
        re.fullmatch(r"sha256:[0-9a-f]{64}", str(binding["build_context_digest"]))
        is None
    ):
        raise ValueError(
            "embedded build binding requires immutable build_context_digest"
        )
    bound_fields = (
        "spec_id",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_abi",
        "binary_profile_id",
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


def _verify_fixture_binding(
    archive: zipfile.ZipFile, spec: dict[str, Any]
) -> dict[str, str]:
    """Parse the unique canonical fixture marker as literals without execution."""
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if len(names) != len(set(names)) or any(
        not _safe_wheel_name(name) for name in names
    ):
        raise ValueError("fixture wheel contains duplicate or unsafe members")
    metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
    wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(metadata_names) != 1 or len(wheel_names) != 1 or len(record_names) != 1:
        raise ValueError("fixture wheel requires one METADATA, WHEEL, and RECORD")
    dist_info = record_names[0].removesuffix("/RECORD")
    expected_names = {
        "ucm/__init__.py",
        FIXTURE_MARKER,
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/RECORD",
    }
    if set(names) != expected_names or names.count(FIXTURE_MARKER) != 1:
        raise ValueError("fixture wheel requires exactly the canonical member set")
    _verify_record(archive, record_names[0])
    raw = archive.read(FIXTURE_MARKER)
    try:
        text = raw.decode("utf-8")
        module = ast.parse(text, filename=FIXTURE_MARKER, mode="exec")
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ValueError(f"fixture binding marker is invalid: {error}") from error
    values: dict[str, str] = {}
    for statement in module.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.Constant)
            or not isinstance(statement.value.value, str)
        ):
            raise ValueError("fixture binding accepts only literal assignments")
        name = statement.targets[0].id
        if name not in {"SOURCE_SHA", "PROFILE_ID"} or name in values:
            raise ValueError("fixture binding has duplicate or extra fields")
        values[name] = statement.value.value
    if set(values) != {"SOURCE_SHA", "PROFILE_ID"}:
        raise ValueError("fixture binding is missing required fields")
    source_sha = values["SOURCE_SHA"]
    profile_id = values["PROFILE_ID"]
    canonical = f"SOURCE_SHA = {source_sha!r}\nPROFILE_ID = {profile_id!r}\n"
    if text != canonical:
        raise ValueError("fixture binding marker bytes are noncanonical")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("fixture binding source SHA is invalid")
    if profile_id != spec["spec_id"]:
        raise ValueError("fixture binding profile does not match the planned spec")
    return {
        "source_commit": source_sha,
        "profile_id": profile_id,
        "marker_status": "passed",
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
    if source_kind not in {"fixture", "builder-candidate"}:
        raise ValueError("source_kind must be fixture or builder-candidate")
    if DIGEST_RE.fullmatch(expected_sha256) is None:
        raise ValueError("expected SHA256 must be sha256:<64 lowercase hex>")
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    specs = {item["spec_id"]: item for item in expand_wheel_specs(release)}
    if spec_id not in specs:
        raise ValueError(f"unknown wheel spec: {spec_id}")
    spec = specs[spec_id]
    if source_kind == "builder-candidate" and not spec["build_eligible"]:
        raise ValueError(
            "builder candidate planned spec has unresolved locks or runner"
        )
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"wheel SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    raw_wheel = Path(path).read_bytes()
    end_record = raw_wheel.rfind(b"PK\x05\x06")
    if end_record < 0 or end_record + 22 > len(raw_wheel):
        raise ValueError("wheel ZIP end record is missing")
    comment_size = int.from_bytes(
        raw_wheel[end_record + 20 : end_record + 22], "little"
    )
    if end_record + 22 + comment_size != len(raw_wheel):
        raise ValueError("wheel contains trailing bytes after the ZIP end record")
    try:
        filename_name, filename_version, _, filename_tags = parse_wheel_filename(
            path.name
        )
    except Exception as error:
        raise ValueError(f"invalid wheel filename: {error}") from error
    builder_evidence: dict[str, Any] | None = None
    fixture_binding: dict[str, str] | None = None
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        wheel_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        ]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError(
                "wheel must contain exactly one METADATA and one WHEEL file"
            )
        parser = email.parser.Parser()
        metadata = parser.parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        wheel_metadata = parser.parsestr(archive.read(wheel_names[0]).decode("utf-8"))
        if source_kind == "builder-candidate":
            builder_evidence = _verify_builder_candidate_evidence(
                archive, wheel_metadata, spec
            )
        else:
            fixture_binding = _verify_fixture_binding(archive, spec)
    distribution = metadata.get("Name", "")
    version = metadata.get("Version", "")
    if canonicalize_name(distribution) != "uc-manager":
        raise ValueError(f"unexpected wheel distribution: {distribution}")
    if canonicalize_name(distribution) != canonicalize_name(str(filename_name)):
        raise ValueError("METADATA distribution does not match wheel filename")
    if version != str(filename_version):
        raise ValueError("METADATA version does not match wheel filename")
    if version != release["ucm_version"]:
        raise ValueError(
            f"wheel version {version} does not match UCM version {release['ucm_version']}"
        )
    filename_tag_strings = {str(tag) for tag in filename_tags}
    metadata_tags = set(wheel_metadata.get_all("Tag", []))
    if not metadata_tags or not metadata_tags.issubset(filename_tag_strings):
        raise ValueError("WHEEL tags do not match wheel filename tags")
    cpu_suffixes = {"amd64": ("x86_64",), "arm64": ("aarch64", "arm64")}[
        spec["cpu_arch"]
    ]
    if not any(
        tag.startswith(f"{spec['python_abi']}-{spec['python_abi']}-")
        and tag.endswith(cpu_suffixes)
        and ("manylinux" in tag or "linux" in tag)
        for tag in filename_tag_strings
    ):
        raise ValueError(
            "wheel tags do not match declared Python ABI and CPU architecture"
        )
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
        "trust_level": (
            "fixture-only"
            if source_kind == "fixture"
            else "unpublished-builder-candidate"
        ),
        "published": False,
        "publication_eligible": False,
    }
    if builder_evidence is not None:
        result["builder_evidence"] = builder_evidence
    if fixture_binding is not None:
        result["fixture_binding"] = fixture_binding
    return result
