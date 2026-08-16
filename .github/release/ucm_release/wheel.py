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


_WHEEL_DECLARATION_FIELDS = ('spec_id', 'profile_id', 'accelerator', 'accelerator_runtime', 'npu_arch_or_na', 'os', 'cpu_arch', 'python_version', 'python_abi', 'wheel_version', 'wheel_platform', 'binary_profile_id', 'validation_targets', 'required_native', 'forbidden_native', 'allowed_dt_needed', 'external_required_dependencies')  # fmt: skip  # noqa: E501


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
    declaration = {field: copy.deepcopy(task[field]) for field in _WHEEL_DECLARATION_FIELDS}  # fmt: skip  # noqa: E501
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
