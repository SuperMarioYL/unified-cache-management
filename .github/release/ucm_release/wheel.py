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
import struct
import time
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from .core import (
    DEFAULT_COMPATIBILITY,
    DEFAULT_RELEASE,
    DEFAULT_SCHEMA_DIR,
    canonical_bytes,
    build_matrix,
    expand_wheel_specs,
    validate_config,
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIXTURE_MARKER = "ucm/_fixture_build.py"
COMPONENT_MANIFEST = "ucm/ucm-native-components.json"
HOST_PATH_MARKERS = (
    b"/Users/",
    b"/home/runner/",
    b"/private/var/",
    b"/var/folders/",
    b"/tmp/",
)
ELF_MACHINES = {"amd64": (62, "EM_X86_64"), "arm64": (183, "EM_AARCH64")}
SHARED_LIBRARY_COMPONENTS = {
    "metrics",
    "posixstore",
    "compressor",
    "cachestore",
    "emptystore",
    "fakestore",
    "mooncakestore",
    "ds3fsstore",
}


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


def _component_for_member(name: str, known_components: set[str]) -> str | None:
    basename = PurePosixPath(name).name
    for component in sorted(known_components, key=len, reverse=True):
        if component in SHARED_LIBRARY_COMPONENTS:
            if basename == f"lib{component}.so":
                return component
        elif re.fullmatch(
            rf"{re.escape(component)}(?:\.cpython-312-[A-Za-z0-9_-]+)?\.so",
            basename,
        ):
            return component
    return None


def _elf_string(data: bytes, offset: int, size: int, label: str) -> str:
    if offset < 0 or offset >= len(data):
        raise ValueError(f"ELF string offset is invalid in {label}")
    limit = min(len(data), offset + size)
    end = data.find(b"\0", offset, limit)
    if end < 0:
        raise ValueError(f"ELF string is unterminated in {label}")
    try:
        return data[offset:end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"ELF dynamic string is not UTF-8 in {label}") from error


def _inspect_elf(data: bytes, label: str) -> dict[str, Any]:
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ValueError(f"native member is not ELF: {label}")
    if data[4] != 2 or data[5] != 1 or data[6] != 1:
        raise ValueError(f"ELF must be 64-bit little-endian version 1: {label}")
    try:
        header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    except struct.error as error:
        raise ValueError(f"ELF header is truncated: {label}") from error
    machine = header[1]
    program_offset = header[4]
    program_size = header[8]
    program_count = header[9]
    if program_size != 56 or program_count < 1 or program_count > 1024:
        raise ValueError(f"ELF program header table is invalid: {label}")
    if program_offset + program_size * program_count > len(data):
        raise ValueError(f"ELF program header table is truncated: {label}")
    loads: list[tuple[int, int, int]] = []
    dynamics: list[tuple[int, int]] = []
    for index in range(program_count):
        values = struct.unpack_from(
            "<IIQQQQQQ", data, program_offset + index * program_size
        )
        kind, _, file_offset, virtual_address, _, file_size, _, _ = values
        if file_offset + file_size > len(data):
            raise ValueError(f"ELF program segment is truncated: {label}")
        if kind == 1:
            loads.append((virtual_address, file_offset, file_size))
        elif kind == 2:
            dynamics.append((file_offset, file_size))
    if len(dynamics) != 1:
        raise ValueError(f"ELF requires exactly one PT_DYNAMIC segment: {label}")
    dynamic_offset, dynamic_size = dynamics[0]
    if dynamic_size % 16 or dynamic_size > 1024 * 1024:
        raise ValueError(f"ELF dynamic segment is invalid: {label}")
    entries: list[tuple[int, int]] = []
    for offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
        tag, value = struct.unpack_from("<QQ", data, offset)
        entries.append((tag, value))
        if tag == 0:
            break
    if not entries or entries[-1][0] != 0:
        raise ValueError(f"ELF dynamic segment lacks DT_NULL: {label}")
    string_addresses = [value for tag, value in entries if tag == 5]
    string_sizes = [value for tag, value in entries if tag == 10]
    if len(string_addresses) != 1 or len(string_sizes) != 1:
        raise ValueError(f"ELF dynamic string table is ambiguous: {label}")
    string_address = string_addresses[0]
    string_size = string_sizes[0]
    string_offset: int | None = None
    for virtual_address, file_offset, file_size in loads:
        if virtual_address <= string_address < virtual_address + file_size:
            string_offset = file_offset + string_address - virtual_address
            break
    if string_offset is None or string_offset + string_size > len(data):
        raise ValueError(f"ELF dynamic string table is outside PT_LOAD: {label}")
    needed = sorted(
        _elf_string(data, string_offset + value, string_size - value, label)
        for tag, value in entries
        if tag == 1
    )
    runpaths = [
        _elf_string(data, string_offset + value, string_size - value, label)
        for tag, value in entries
        if tag in {15, 29}
    ]
    if runpaths:
        raise ValueError(f"ELF RPATH/RUNPATH is forbidden in {label}: {runpaths}")
    if any(marker in data for marker in HOST_PATH_MARKERS):
        raise ValueError(f"ELF source/path leakage detected in {label}")
    return {"machine": machine, "needed": needed}


def _verify_native_evidence(
    archive: zipfile.ZipFile,
    spec: dict[str, Any],
) -> dict[str, Any]:
    required = spec["required_native"]
    forbidden = spec["forbidden_native"]
    known = set(required) | set(forbidden)
    native_members: dict[str, str] = {}
    elf_evidence: dict[str, dict[str, Any]] = {}
    for item in archive.infolist():
        name = item.filename
        if item.is_dir() or not name.endswith(".so"):
            continue
        data = archive.read(name)
        if not data.startswith(b"\x7fELF"):
            continue
        component = _component_for_member(name, known)
        if component is None:
            raise ValueError(f"unclassified native wheel member: {name}")
        if component in native_members:
            raise ValueError(f"duplicate native component {component!r}")
        native_members[component] = name
        elf_evidence[name] = _inspect_elf(data, name)
    actual = set(native_members)
    missing = [item for item in required if item not in actual]
    present_forbidden = [item for item in forbidden if item in actual]
    extras = sorted(actual - set(required) - set(forbidden))
    if missing:
        raise ValueError(f"required native components are missing: {missing}")
    if present_forbidden:
        raise ValueError(
            f"forbidden native components are present: {present_forbidden}"
        )
    if extras or len(actual) != len(required):
        raise ValueError(f"native component set is not exact: extras={extras}")
    expected_machine, machine_name = ELF_MACHINES[spec["cpu_arch"]]
    wrong_machines = {
        name: evidence["machine"]
        for name, evidence in elf_evidence.items()
        if evidence["machine"] != expected_machine
    }
    if wrong_machines:
        raise ValueError(
            f"ELF machine does not match {spec['cpu_arch']}: {wrong_machines}"
        )
    allowed_needed = set(spec["allowed_dt_needed"])
    unapproved = {
        name: sorted(set(evidence["needed"]) - allowed_needed)
        for name, evidence in elf_evidence.items()
        if set(evidence["needed"]) - allowed_needed
    }
    if unapproved:
        raise ValueError(f"unapproved ELF DT_NEEDED entries: {unapproved}")
    ordered_members = {component: native_members[component] for component in required}
    return {
        "native_components": list(required),
        "native_members": ordered_members,
        "native_artifacts": list(ordered_members.values()),
        "elf_machines": [machine_name],
        "dt_needed": {
            name: elf_evidence[name]["needed"] for name in sorted(elf_evidence)
        },
        "unresolved_dependencies": [],
    }


def _verify_component_manifest(
    archive: zipfile.ZipFile,
    spec: dict[str, Any],
    profile_id: str,
    source_sha: str,
    build_key: str,
) -> tuple[dict[str, Any], str]:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if names.count(COMPONENT_MANIFEST) != 1:
        raise ValueError("wheel requires exactly one native component manifest")
    raw = archive.read(COMPONENT_MANIFEST)
    manifest = _unique_json(raw, COMPONENT_MANIFEST)
    expected = {
        "schema_version": 1,
        "kind": "ucm-native-components",
        "profile_id": profile_id,
        "spec_id": spec["spec_id"],
        "source_sha": source_sha,
        "build_key": build_key,
        "version": spec["wheel_version"],
        "cpu_arch": spec["cpu_arch"],
        "required_native": spec["required_native"],
        "forbidden_native": spec["forbidden_native"],
        "installed_targets": spec["required_native"],
    }
    if manifest != expected:
        missing_native = sorted(
            set(spec["required_native"]) - set(manifest.get("installed_targets", []))
        )
        raise ValueError(
            "native component manifest does not match source/profile/architecture/"
            f"required native authority; missing={missing_native}"
        )
    if raw != canonical_bytes(manifest) + b"\n":
        raise ValueError("native component manifest is noncanonical")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return manifest, digest


def _verify_builder_candidate_evidence(
    archive: zipfile.ZipFile,
    wheel_metadata: email.message.Message,
    spec: dict[str, Any],
    profile_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if FIXTURE_MARKER in names:
        raise ValueError("builder candidate must not contain a fixture binding marker")
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise ValueError("builder candidate requires exactly one RECORD")
    _verify_record(archive, record_names[0])
    if wheel_metadata.get("Root-Is-Purelib", "").lower() != "false":
        raise ValueError("builder candidate must declare Root-Is-Purelib: false")
    build_names = [name for name in names if name.endswith(".dist-info/ucm-build.json")]
    if len(build_names) != 1:
        raise ValueError(
            "builder candidate requires exactly one embedded ucm-build.json"
        )
    binding = _unique_json(archive.read(build_names[0]), build_names[0])
    required = {
        "schema_version",
        "spec_id",
        "kind",
        "source_kind",
        "profile_id",
        "source_sha",
        "build_key",
        "source_date_epoch",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_abi",
        "binary_profile_id",
        "wheel_version",
        "wheel_platform",
        "required_native",
        "forbidden_native",
        "allowed_dt_needed",
        "native_members",
        "component_manifest_sha256",
    }
    if set(binding) != required:
        raise ValueError(
            "embedded build binding fields mismatch: "
            f"missing={sorted(required - set(binding))}, extra={sorted(set(binding) - required)}"
        )
    if (
        binding["schema_version"] != 1
        or binding["kind"] != "ucm-native-wheel-build"
        or binding["source_kind"] != "builder-candidate"
    ):
        raise ValueError("embedded build binding identity is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", str(binding["source_sha"])) is None:
        raise ValueError("embedded build binding requires immutable source_commit")
    if binding["build_key"] != task["task_sha256"]:
        raise ValueError("embedded build binding build_key is not canonical")
    if (
        not isinstance(binding["source_date_epoch"], int)
        or isinstance(binding["source_date_epoch"], bool)
        or binding["source_date_epoch"] < 315532800
    ):
        raise ValueError("embedded build binding SOURCE_DATE_EPOCH is invalid")
    bound_fields = (
        "spec_id",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_abi",
        "binary_profile_id",
        "wheel_version",
        "wheel_platform",
        "required_native",
        "forbidden_native",
        "allowed_dt_needed",
    )
    for field in bound_fields:
        if binding[field] != spec[field]:
            raise ValueError(
                f"embedded build binding {field} does not match planned spec: "
                f"{binding[field]!r} != {spec[field]!r}"
            )
    if binding["profile_id"] != profile_id:
        raise ValueError(
            "embedded build binding profile_id does not match planned spec"
        )
    native = _verify_native_evidence(archive, spec)
    _, component_digest = _verify_component_manifest(
        archive,
        spec,
        profile_id,
        binding["source_sha"],
        binding["build_key"],
    )
    if binding["component_manifest_sha256"] != component_digest:
        raise ValueError("embedded component manifest digest mismatch")
    if binding["native_members"] != native["native_members"]:
        raise ValueError("embedded native member map does not match wheel bytes")
    if archive.read(build_names[0]) != canonical_bytes(binding) + b"\n":
        raise ValueError("embedded build binding is noncanonical")
    return {
        "source_commit": binding["source_sha"],
        "build_context_digest": binding["build_key"],
        "build_key": binding["build_key"],
        "source_date_epoch": binding["source_date_epoch"],
        **native,
        "record_status": "passed",
    }


def _canonical_metadata(version: str, dependencies: list[str]) -> bytes:
    lines = [
        "Metadata-Version: 2.1",
        "Name: uc-manager",
        f"Version: {version}",
        "Summary: Unified Cache Management",
        "Requires-Python: >=3.10",
        *(f"Requires-Dist: {dependency}" for dependency in dependencies),
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _canonical_wheel_metadata(tag: str) -> bytes:
    return "\n".join(
        [
            "Wheel-Version: 1.0",
            "Generator: ucm-release-sealer-v1",
            "Root-Is-Purelib: false",
            f"Tag: {tag}",
            "",
        ]
    ).encode("utf-8")


def _record_bytes(members: dict[str, bytes], record_name: str) -> bytes:
    rows: list[list[str]] = []
    for name in sorted(members):
        content = members[name]
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .decode("ascii")
            .rstrip("=")
        )
        rows.append([name, f"sha256={digest}", str(len(content))])
    rows.append([record_name, "", ""])
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or not 315532800 <= source_date_epoch <= 4354819199
    ):
        raise ValueError("SOURCE_DATE_EPOCH must fit the canonical ZIP timestamp range")
    values = list(time.gmtime(source_date_epoch)[:6])
    values[5] -= values[5] % 2
    return tuple(values)  # type: ignore[return-value]


def _check_member_path_leakage(name: str, data: bytes) -> None:
    if any(marker in data for marker in HOST_PATH_MARKERS):
        raise ValueError(f"source/path leakage detected in wheel member: {name}")


def _expected_wheel_tag(spec: dict[str, Any]) -> str:
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}[spec["cpu_arch"]]
    return (
        f"{spec['python_abi']}-{spec['python_abi']}-"
        f"{spec['wheel_platform']}_{architecture}"
    )


def _verify_canonical_builder_archive(
    archive: zipfile.ZipFile,
    binding: dict[str, Any],
    metadata_name: str,
    wheel_name: str,
    record_name: str,
    dependencies: list[str],
) -> None:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if names != sorted(names):
        raise ValueError("sealed wheel member order is noncanonical")
    expected_timestamp = _zip_timestamp(binding["source_date_epoch"])
    for item in infos:
        if item.is_dir():
            raise ValueError("sealed wheel must not contain directory entries")
        if item.date_time != expected_timestamp:
            raise ValueError(f"sealed wheel timestamp is noncanonical: {item.filename}")
        if item.create_system != 3 or item.external_attr >> 16 != 0o644:
            raise ValueError(f"sealed wheel mode is noncanonical: {item.filename}")
        if item.compress_type != zipfile.ZIP_DEFLATED:
            raise ValueError(
                f"sealed wheel compression is noncanonical: {item.filename}"
            )
        if item.extra or item.comment:
            raise ValueError(
                f"sealed wheel ZIP metadata is noncanonical: {item.filename}"
            )
    if archive.comment:
        raise ValueError("sealed wheel ZIP comment is forbidden")
    tag = _expected_wheel_tag(binding)
    if archive.read(metadata_name) != _canonical_metadata(
        binding["wheel_version"], dependencies
    ):
        raise ValueError("sealed wheel METADATA bytes are noncanonical")
    if archive.read(wheel_name) != _canonical_wheel_metadata(tag):
        raise ValueError("sealed wheel WHEEL bytes are noncanonical")
    expected_record = _record_bytes(
        {name: archive.read(name) for name in names if name != record_name},
        record_name,
    )
    if archive.read(record_name) != expected_record:
        raise ValueError("sealed wheel RECORD bytes are noncanonical")


def seal_wheel(
    path: Path,
    output_dir: Path,
    spec_id: str,
    source_sha: str,
    build_key: str,
    source_date_epoch: int,
    *,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    """Seal one native builder output into the sole deterministic candidate wheel."""
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("release wheel source SHA must be a full lowercase Git commit")
    if DIGEST_RE.fullmatch(build_key) is None:
        raise ValueError("release wheel build key must be sha256:<64 lowercase hex>")
    timestamp = _zip_timestamp(source_date_epoch)
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    specs = {item["spec_id"]: item for item in expand_wheel_specs(release)}
    if spec_id not in specs:
        raise ValueError(f"unknown wheel spec: {spec_id}")
    spec = specs[spec_id]
    profile_id = spec_id.removesuffix(f"-{spec['cpu_arch']}")
    tasks = {
        item["spec_id"]: item
        for item in build_matrix(
            "feature-candidate", release_path, compatibility_path, schema_dir
        )["tasks"]
    }
    task = tasks[spec_id]
    if build_key != task["task_sha256"]:
        raise ValueError(
            f"release wheel build key does not match exact task authority for {spec_id}"
        )
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("sealed wheel output directory must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = Path(path).read_bytes()
    end_record = raw.rfind(b"PK\x05\x06")
    if end_record < 0 or end_record + 22 > len(raw):
        raise ValueError("input wheel ZIP end record is missing")
    comment_size = int.from_bytes(raw[end_record + 20 : end_record + 22], "little")
    if end_record + 22 + comment_size != len(raw):
        raise ValueError("input wheel contains trailing bytes")
    try:
        with zipfile.ZipFile(path) as integrity_archive:
            bad_member = integrity_archive.testzip()
    except (zipfile.BadZipFile, zlib.error) as error:
        raise ValueError(f"input wheel ZIP is corrupt: {error}") from error
    if bad_member is not None:
        raise ValueError(f"input wheel CRC is corrupt: {bad_member}")
    with zipfile.ZipFile(path) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or any(
            not _safe_wheel_name(name) for name in names
        ):
            raise ValueError("input wheel contains duplicate or unsafe members")
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError("input wheel requires exactly one METADATA and WHEEL")
        metadata_name = metadata_names[0]
        dist_info = metadata_name.removesuffix("/METADATA")
        expected_dist_info = f"uc_manager-{spec['wheel_version']}.dist-info"
        if dist_info != expected_dist_info or wheel_names[0] != f"{dist_info}/WHEEL":
            raise ValueError(
                "input wheel dist-info path does not match controlled version"
            )
        metadata = email.parser.Parser().parsestr(
            archive.read(metadata_name).decode("utf-8")
        )
        if canonicalize_name(metadata.get("Name", "")) != "uc-manager":
            raise ValueError("input wheel distribution must be uc-manager")
        if metadata.get("Version") != spec["wheel_version"]:
            raise ValueError("input wheel version is not the controlled local version")
        if (
            metadata.get_all("Requires-Dist", [])
            != release["python_runtime_dependencies"]
        ):
            raise ValueError(
                "input wheel runtime dependencies do not match release.yaml"
            )
        _, component_digest = _verify_component_manifest(
            archive, spec, profile_id, source_sha, build_key
        )
        native = _verify_native_evidence(archive, spec)
        members = {
            name: archive.read(name)
            for name in names
            if not name.endswith(".dist-info/METADATA")
            and not name.endswith(".dist-info/WHEEL")
            and not name.endswith(".dist-info/RECORD")
            and not name.endswith(".dist-info/RECORD.jws")
            and not name.endswith(".dist-info/RECORD.p7s")
            and not name.endswith(".dist-info/ucm-build.json")
        }
    for name, data in members.items():
        _check_member_path_leakage(name, data)
    tag = _expected_wheel_tag(spec)
    metadata_name = f"{expected_dist_info}/METADATA"
    wheel_name = f"{expected_dist_info}/WHEEL"
    build_name = f"{expected_dist_info}/ucm-build.json"
    record_name = f"{expected_dist_info}/RECORD"
    binding = {
        "schema_version": 1,
        "kind": "ucm-native-wheel-build",
        "source_kind": "builder-candidate",
        "profile_id": profile_id,
        "spec_id": spec_id,
        "source_sha": source_sha,
        "build_key": build_key,
        "source_date_epoch": source_date_epoch,
        "accelerator": spec["accelerator"],
        "accelerator_runtime": spec["accelerator_runtime"],
        "npu_arch_or_na": spec["npu_arch_or_na"],
        "os": spec["os"],
        "cpu_arch": spec["cpu_arch"],
        "python_abi": spec["python_abi"],
        "binary_profile_id": spec["binary_profile_id"],
        "wheel_version": spec["wheel_version"],
        "wheel_platform": spec["wheel_platform"],
        "required_native": spec["required_native"],
        "forbidden_native": spec["forbidden_native"],
        "allowed_dt_needed": spec["allowed_dt_needed"],
        "native_members": native["native_members"],
        "component_manifest_sha256": component_digest,
    }
    members[metadata_name] = _canonical_metadata(
        spec["wheel_version"], release["python_runtime_dependencies"]
    )
    members[wheel_name] = _canonical_wheel_metadata(tag)
    members[build_name] = canonical_bytes(binding) + b"\n"
    members[record_name] = _record_bytes(members, record_name)
    filename = f"uc_manager-{spec['wheel_version']}-{tag}.whl"
    wheel_path = output_dir / filename
    with zipfile.ZipFile(
        wheel_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name], compress_type=zipfile.ZIP_DEFLATED)
    wheel_sha256 = _sha256(wheel_path)
    inspection = inspect_wheel(
        wheel_path,
        spec_id,
        wheel_sha256,
        "builder-candidate",
        release_path=release_path,
        compatibility_path=compatibility_path,
        schema_dir=schema_dir,
    )
    inspection_path = output_dir / "wheel-inspection.json"
    _write_canonical(inspection_path, inspection)
    result = {
        "schema_version": 1,
        "kind": "ucm-native-wheel-seal",
        "source_kind": "builder-candidate",
        "publication_status": "unpublished",
        "publication_eligible": False,
        "spec_id": spec_id,
        "source_sha": source_sha,
        "build_key": build_key,
        "wheel_path": str(wheel_path),
        "wheel_sha256": wheel_sha256,
        "inspection_path": str(inspection_path),
        "inspection_sha256": _sha256(inspection_path),
    }
    _write_canonical(output_dir / "wheel-seal.json", result)
    return result


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
    profile_id = spec_id.removesuffix(f"-{spec['cpu_arch']}")
    tasks = {
        item["spec_id"]: item
        for item in build_matrix(
            "feature-candidate", release_path, compatibility_path, schema_dir
        )["tasks"]
    }
    task = tasks[spec_id]
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
        with zipfile.ZipFile(path) as integrity_archive:
            bad_member = integrity_archive.testzip()
    except (zipfile.BadZipFile, zlib.error) as error:
        raise ValueError(f"wheel ZIP is corrupt: {error}") from error
    if bad_member is not None:
        raise ValueError(f"wheel CRC is corrupt: {bad_member}")
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
                archive, wheel_metadata, spec, profile_id, task
            )
            build_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/ucm-build.json")
            )
            record_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/RECORD")
            )
            binding = _unique_json(archive.read(build_name), build_name)
            _verify_canonical_builder_archive(
                archive,
                binding,
                metadata_names[0],
                wheel_names[0],
                record_name,
                release["python_runtime_dependencies"],
            )
            for name in archive.namelist():
                if not name.endswith(".dist-info/RECORD"):
                    _check_member_path_leakage(name, archive.read(name))
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
    expected_version = (
        release["ucm_version"] if source_kind == "fixture" else spec["wheel_version"]
    )
    if version != expected_version:
        raise ValueError(
            f"wheel version {version} does not match planned version {expected_version}"
        )
    filename_tag_strings = {str(tag) for tag in filename_tags}
    metadata_tags = set(wheel_metadata.get_all("Tag", []))
    if not metadata_tags or metadata_tags != filename_tag_strings:
        raise ValueError("WHEEL tags do not match wheel filename tags")
    if source_kind == "builder-candidate":
        expected_tags = {_expected_wheel_tag(spec)}
    else:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}[spec["cpu_arch"]]
        expected_tags = {
            f"{spec['python_abi']}-{spec['python_abi']}-linux_{architecture}"
        }
    if filename_tag_strings != expected_tags:
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
