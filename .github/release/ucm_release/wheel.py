"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import copy
import json
import re
from typing import Any


from .core import (
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
