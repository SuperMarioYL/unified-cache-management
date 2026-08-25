"""Small deterministic primitives shared by the v2 control plane."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any


class SafePathError(ValueError):
    """Raised when external input is not a canonical, safe relative POSIX path."""


def canonical_json(value: Any) -> str:
    """Return the sole JSON representation used for v2 documents."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_envelope(value: dict[str, Any]) -> dict[str, Any]:
    """Attach a digest of the document excluding its own digest field."""
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    result = dict(unsigned)
    result["sha256"] = hashlib.sha256(
        canonical_json(unsigned).encode("utf-8")
    ).hexdigest()
    return result


def safe_posix_path(value: object, label: str) -> str:
    """Return a safe relative POSIX path without normalizing attacker input."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise SafePathError(f"{label} must be a canonical safe POSIX path")
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SafePathError(f"{label} must be a canonical safe POSIX path")
    raw_parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise SafePathError(f"{label} must be a canonical safe POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or list(path.parts) != raw_parts:
        raise SafePathError(f"{label} must be a canonical safe POSIX path")
    return value
