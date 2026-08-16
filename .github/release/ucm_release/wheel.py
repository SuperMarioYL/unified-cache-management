"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import re
import struct
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


from .core import (
    REPO_ROOT,
    canonical_bytes,
    cpu_toolchain_authority,
    runtime_patch_manifest_sha256,
    sha256_value,
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIXTURE_MARKER = "ucm/_fixture_build.py"
COMPONENT_MANIFEST = "ucm/ucm-native-components.json"
RUNTIME_PATCH_MANIFEST = "ucm/integration/vllm/patch/runtime_patch_rules.json"
AUTHORITY_KIND = "ucm-native-build-authority"
CLOSURE_KIND = "ucm-linux-dependency-closure"
HOST_PATH_MARKERS = (
    b"/Users/",
    b"/home/runner/",
    b"/private/var/",
    b"/var/folders/",
    b"/tmp/",
)
NATIVE_MEMBER_DIRECTORIES = {
    "ucmtrans": "ucm/shared/trans",
    "metrics": "ucm/shared/metrics",
    "ucmmetrics": "ucm/shared/metrics",
    "ucmlogger": "ucm/shared/infra",
    "ucmnfsstore": "ucm/store/nfsstore",
    "ucmpcstore": "ucm/store/pcstore",
    "posixstore": "ucm/store/posix",
    "compressor": "ucm/store/compress",
    "cachestore": "ucm/store/cache",
    "emptystore": "ucm/store/empty",
    "fakestore": "ucm/store/fake",
    "ucmpipelinestore": "ucm/store/pipeline",
    "mooncakestore": "ucm/store/mooncakestore",
    "ds3fsstore": "ucm/store/ds3fs",
}
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
EXTERNAL_REQUIRED_FIELDS = {
    "dependency",
    "provider",
    "expected_mount_root",
    "relation",
    "required_at",
}


_WHEEL_DECLARATION_FIELDS = (
    "spec_id",
    "profile_id",
    "accelerator",
    "accelerator_runtime",
    "npu_arch_or_na",
    "os",
    "cpu_arch",
    "python_version",
    "python_abi",
    "wheel_version",
    "wheel_platform",
    "binary_profile_id",
    "validation_targets",
    "required_native",
    "forbidden_native",
    "allowed_dt_needed",
    "external_required_dependencies",
)


def _validate_wheel_task(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("selected wheel task must be an object")
    task = copy.deepcopy(value)
    task_payload = {key: item for key, item in task.items() if key != "task_sha256"}
    if re.fullmatch(
        r"wheel-[0-9a-f]{64}", str(task.get("task_id"))
    ) is None or task.get("task_sha256") != sha256_value(task_payload):
        raise ValueError("wheel task hash mismatch")
    missing = [field for field in _WHEEL_DECLARATION_FIELDS if field not in task]
    if missing:
        raise ValueError(f"wheel task declaration fields are missing: {missing}")
    declaration = {
        field: copy.deepcopy(task[field]) for field in _WHEEL_DECLARATION_FIELDS
    }
    if task.get("declaration_sha256") != sha256_value(declaration):
        raise ValueError("wheel task declaration hash mismatch")
    dependency_lock = task.get("dependency_lock")
    if (
        not isinstance(dependency_lock, dict)
        or task.get("dependency_lock_sha256") != sha256_value(dependency_lock)
        or task.get("runtime_patch_manifest_sha256")
        != runtime_patch_manifest_sha256(task.get("runtime_patch_manifest"))
    ):
        raise ValueError("wheel task dependency authority is invalid")
    return task


def _external_required_by_dependency(
    declarations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for declaration in declarations:
        if (
            not isinstance(declaration, dict)
            or set(declaration) != EXTERNAL_REQUIRED_FIELDS
        ):
            raise ValueError("external-required dependency declaration is invalid")
        dependency = declaration.get("dependency")
        if not isinstance(dependency, str) or dependency in result:
            raise ValueError("external-required dependency declarations are not unique")
        if (
            declaration.get("relation") != "transitive"
            or declaration.get("required_at") != "device-runtime"
            or not str(declaration.get("expected_mount_root", "")).startswith("/")
        ):
            raise ValueError("external-required dependency declaration is invalid")
        result[dependency] = declaration
    return result


def _external_required_resolution(declaration: dict[str, Any]) -> dict[str, Any]:
    return {
        **declaration,
        "direct": False,
        "kind": "external-required",
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


def _canonical_record(path: Path, label: str) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    value = _unique_json(raw, label)
    if raw != canonical_bytes(value) + b"\n":
        raise ValueError(f"{label} is noncanonical")
    return value


def _tool_wheel_authority(task: dict[str, Any]) -> dict[str, str]:
    records = task["dependency_lock"]["build_tools"]
    if (
        not isinstance(records, list)
        or not records
        or any(not isinstance(record, dict) for record in records)
    ):
        raise ValueError("build tool wheel authority is invalid")
    wheels = {record["filename"]: record["sha256"] for record in records}
    if len(wheels) != len(records):
        raise ValueError("build tool wheel authority is ambiguous")
    return dict(sorted(wheels.items()))


def _git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _validate_build_authority(
    authority: dict[str, Any],
    spec_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "task_id",
        "spec_id",
        "profile_id",
        "cpu_arch",
        "platform",
        "build",
        "python_version",
        "python_abi",
        "wheel_version",
        "wheel_platform",
        "source_sha",
        "source_tree",
        "source_archive_sha256",
        "source_date_epoch",
        "task_sha256",
        "builder_coordinate",
        "builder_config_digest",
        "dependency_lock_sha256",
        "tool_wheels",
        "required_native",
        "forbidden_native",
        "runtime_patch_manifest_sha256",
        "runtime_requirements",
        "build_context_sha256",
    }
    if set(authority) != fields:
        raise ValueError("build authority fields are not exact")
    if DIGEST_RE.fullmatch(str(authority["build_context_sha256"])) is None:
        raise ValueError("build authority context digest is invalid")
    if DIGEST_RE.fullmatch(str(authority["source_archive_sha256"])) is None:
        raise ValueError("build authority source archive digest is invalid")
    root = task["builder"]["root"]
    expected = {
        "schema_version": 1,
        "kind": AUTHORITY_KIND,
        "task_id": task["task_id"],
        "spec_id": spec_id,
        "profile_id": task["profile_id"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "build": task["build"],
        "python_version": task["python_version"],
        "python_abi": task["python_abi"],
        "wheel_version": task["wheel_version"],
        "wheel_platform": task["wheel_platform"],
        "task_sha256": task["task_sha256"],
        "builder_coordinate": f"{root['repository']}@{root['manifest_digest']}",
        "builder_config_digest": root["config_digest"],
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "tool_wheels": _tool_wheel_authority(task),
        "required_native": task["required_native"],
        "forbidden_native": task["forbidden_native"],
        "runtime_patch_manifest_sha256": task["runtime_patch_manifest_sha256"],
        "runtime_requirements": task["runtime_requirements"],
    }
    for name, value in expected.items():
        if authority[name] != value:
            raise ValueError(f"build authority {name} differs from reviewed task")
    source_sha = authority["source_sha"]
    if re.fullmatch(r"[0-9a-f]{40}", str(source_sha)) is None:
        raise ValueError("build source authority SHA is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", str(authority["source_tree"])) is None:
        raise ValueError("build source authority tree is invalid")
    checked_head = _git_value("rev-parse", "HEAD")
    if checked_head is not None:
        if checked_head != source_sha:
            raise ValueError(
                "build source authority does not match checked source HEAD"
            )
        if (
            _git_value("rev-parse", f"{source_sha}^{{tree}}")
            != authority["source_tree"]
        ):
            raise ValueError(
                "build source authority tree does not match checked source"
            )
    _zip_timestamp(authority["source_date_epoch"])
    return authority


def _validate_dependency_closure(
    closure: dict[str, Any],
    raw_wheel_sha256: str,
    authority: dict[str, Any],
    native: dict[str, Any],
    archive: zipfile.ZipFile,
    *,
    external_required_dependencies: list[dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "spec_id",
        "raw_wheel_sha256",
        "build_context_sha256",
        "native_members",
        "unresolved_dependencies",
        "closure_sha256",
    }
    if set(closure) != fields:
        raise ValueError("dependency closure fields are not exact")
    digest_input = dict(closure)
    closure_digest = digest_input.pop("closure_sha256")
    if (
        closure_digest
        != "sha256:" + hashlib.sha256(canonical_bytes(digest_input)).hexdigest()
    ):
        raise ValueError("dependency closure digest is invalid")
    if (
        closure["schema_version"] != 1
        or closure["kind"] != CLOSURE_KIND
        or closure["spec_id"] != authority["spec_id"]
        or closure["raw_wheel_sha256"] != raw_wheel_sha256
        or closure["build_context_sha256"] != authority["build_context_sha256"]
    ):
        raise ValueError("dependency closure authority binding is invalid")
    if closure["unresolved_dependencies"] != []:
        raise ValueError(
            f"dependency closure has unresolved dependencies: {closure['unresolved_dependencies']}"
        )
    expected_names = set(native["native_artifacts"])
    records = closure["native_members"]
    if not isinstance(records, dict) or set(records) != expected_names:
        raise ValueError("dependency closure native member set is not exact")
    member_digests = {
        name: "sha256:" + hashlib.sha256(archive.read(name)).hexdigest()
        for name in expected_names
    }
    member_by_basename = {PurePosixPath(name).name: name for name in expected_names}
    declared_external = _external_required_by_dependency(external_required_dependencies)
    observed_external: set[str] = set()
    for name in sorted(expected_names):
        record = records[name]
        if not isinstance(record, dict) or set(record) != {
            "dt_needed",
            "resolved_dependencies",
            "unresolved_dependencies",
        }:
            raise ValueError(f"dependency closure record is invalid: {name}")
        if record["unresolved_dependencies"] != []:
            raise ValueError(
                f"dependency closure has unresolved dependencies for {name}"
            )
        needed = native["dt_needed"][name]
        if record["dt_needed"] != needed:
            missing = sorted(set(needed) - set(record.get("dt_needed", [])))
            if missing:
                raise ValueError(
                    f"dependency closure has unresolved entries for {name}: {missing}"
                )
            raise ValueError(f"dependency closure DT_NEEDED differs for {name}")
        resolutions = record["resolved_dependencies"]
        if not isinstance(resolutions, list) or not all(
            isinstance(resolution, dict) for resolution in resolutions
        ):
            raise ValueError(f"dependency closure resolutions are invalid: {name}")
        dependencies = [resolution.get("dependency") for resolution in resolutions]
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"dependency closure has duplicate resolutions: {name}")
        direct_resolutions = {
            resolution.get("dependency")
            for resolution in resolutions
            if resolution.get("direct") is True
        }
        if direct_resolutions != set(needed):
            missing = sorted(set(needed) - direct_resolutions)
            raise ValueError(f"dependency closure has unresolved entries: {missing}")
        for resolution in resolutions:
            dependency = resolution.get("dependency")
            if not isinstance(dependency, str) or resolution.get("direct") not in {
                True,
                False,
            }:
                raise ValueError(
                    f"dependency closure resolution identity is invalid: {name}"
                )
            internal_member = member_by_basename.get(PurePosixPath(dependency).name)
            if internal_member is not None:
                expected_resolution = {
                    "dependency": dependency,
                    "direct": dependency in needed,
                    "kind": "wheel-member",
                    "member": internal_member,
                    "sha256": member_digests[internal_member],
                }
                if resolution != expected_resolution:
                    raise ValueError(
                        f"dependency closure must resolve internal {dependency} "
                        "from the exact wheel member"
                    )
            elif resolution.get("kind") == "external-required":
                declaration = declared_external.get(str(dependency))
                if declaration is None or resolution != _external_required_resolution(
                    declaration
                ):
                    raise ValueError(
                        "dependency closure external-required resolution is invalid: "
                        f"{dependency}"
                    )
                observed_external.add(str(dependency))
            elif resolution.get("kind") == "virtual":
                if resolution != {
                    "dependency": "linux-vdso.so.1",
                    "direct": dependency in needed,
                    "kind": "virtual",
                }:
                    raise ValueError(
                        f"dependency closure virtual resolution is invalid: {dependency}"
                    )
            elif (
                not isinstance(resolution, dict)
                or set(resolution) != {"dependency", "direct", "kind", "path", "sha256"}
                or resolution["kind"] != "external"
                or not isinstance(resolution["path"], str)
                or not resolution["path"].startswith("/")
                or DIGEST_RE.fullmatch(str(resolution["sha256"])) is None
            ):
                raise ValueError(
                    f"dependency closure external resolution is invalid: {dependency}"
                )
    if observed_external != set(declared_external):
        raise ValueError(
            "dependency closure external-required set differs from declaration: "
            f"expected={sorted(declared_external)}, observed={sorted(observed_external)}"
        )
    return closure


def _python_extension_suffix(spec: dict[str, Any], task: dict[str, Any]) -> str:
    architecture = cpu_toolchain_authority(spec["cpu_arch"]).wheel_arch
    checks = task.get("builder", {}).get("checks", [])
    soabi_checks = [item for item in checks if item.get("kind") == "python-soabi"]
    if len(soabi_checks) != 1:
        raise ValueError("wheel task requires one Python SOABI authority")
    prefix = soabi_checks[0].get("prefix")
    version = task.get("python_version")
    abi = task.get("python_abi")
    if (
        not isinstance(prefix, str)
        or not isinstance(version, str)
        or not isinstance(abi, str)
        or abi != "cp" + version.replace(".", "")
        or abi != spec.get("python_abi")
    ):
        raise ValueError("wheel task Python ABI/SOABI authority is inconsistent")
    return f".{prefix}-{abi.removeprefix('cp')}-{architecture}-linux-gnu.so"


def _expected_native_members(
    spec: dict[str, Any], task: dict[str, Any]
) -> dict[str, str]:
    suffix = _python_extension_suffix(spec, task)
    known = set(spec["required_native"]) | set(spec["forbidden_native"])
    missing = sorted(set(spec["required_native"]) - set(NATIVE_MEMBER_DIRECTORIES))
    if missing:
        raise ValueError(f"native component archive paths are undeclared: {missing}")
    return {
        component: (
            f"{NATIVE_MEMBER_DIRECTORIES[component]}/lib{component}.so"
            if component in SHARED_LIBRARY_COMPONENTS
            else f"{NATIVE_MEMBER_DIRECTORIES[component]}/{component}{suffix}"
        )
        for component in known & set(NATIVE_MEMBER_DIRECTORIES)
    }


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
    task: dict[str, Any],
) -> dict[str, Any]:
    required = spec["required_native"]
    forbidden = spec["forbidden_native"]
    expected_members = _expected_native_members(spec, task)
    extension_suffix = _python_extension_suffix(spec, task)
    components_by_member = {
        member: component for component, member in expected_members.items()
    }
    native_members: dict[str, str] = {}
    elf_evidence: dict[str, dict[str, Any]] = {}
    for item in archive.infolist():
        name = item.filename
        if item.is_dir() or not name.endswith(".so"):
            continue
        data = archive.read(name)
        if not data.startswith(b"\x7fELF"):
            continue
        component = components_by_member.get(name)
        if component is None:
            basename = PurePosixPath(name).name
            if any(
                basename == PurePosixPath(member).name
                or basename == component_name
                or basename == f"{component_name}.so"
                for component_name, member in expected_members.items()
            ):
                raise ValueError(f"native component archive path is not exact: {name}")
            component = next(
                (
                    forbidden_component
                    for forbidden_component in forbidden
                    if re.fullmatch(
                        rf"(?:lib)?{re.escape(forbidden_component)}(?:{re.escape(extension_suffix.removesuffix('.so'))})?\.so",
                        basename,
                    )
                ),
                None,
            )
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
    cpu_authority = cpu_toolchain_authority(spec["cpu_arch"])
    expected_machine = cpu_authority.elf_machine
    machine_name = cpu_authority.elf_machine_name
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


def _expected_wheel_tag(spec: dict[str, Any]) -> str:
    architecture = cpu_toolchain_authority(spec["cpu_arch"]).wheel_arch
    return (
        f"{spec['python_abi']}-{spec['python_abi']}-"
        f"{spec['wheel_platform']}_{architecture}"
    )
