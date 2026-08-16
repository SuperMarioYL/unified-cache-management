"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import copy
import json
import re
import struct
import subprocess
import time
from pathlib import PurePosixPath
from typing import Any


from .core import (
    REPO_ROOT,
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
