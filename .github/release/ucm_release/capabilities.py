"""Canonical normalization for release capability coordinates."""

from __future__ import annotations

import re

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_ACCELERATOR_RUNTIME = re.compile(r"^(cuda|cann)-(.+)$", re.ASCII)
_COMPACT = re.compile(r"^[a-z0-9]+$", re.ASCII)
_VARIANT = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$", re.ASCII
)


def _parse_version(value: object, label: str) -> Version:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty version string")
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"{label} is not a valid version") from exc
    if parsed.epoch or parsed.local:
        raise ValueError(f"{label} must not use epochs or local versions")
    return parsed


def _compact_version(parsed: Version, label: str) -> str:
    compact = "".join(str(part) for part in parsed.release)
    if parsed.pre is not None:
        compact += f"{parsed.pre[0]}{parsed.pre[1]}"
    if parsed.post is not None:
        compact += f"post{parsed.post}"
    if parsed.dev is not None:
        compact += f"dev{parsed.dev}"
    if _COMPACT.fullmatch(compact) is None:
        raise ValueError(f"{label} does not have a canonical compact form")
    return compact


def compact_accelerator_runtime(value: object) -> str:
    """Return the canonical compact token for a CUDA or CANN runtime."""
    if not isinstance(value, str):
        raise ValueError("accelerator runtime must be a string")
    match = _ACCELERATOR_RUNTIME.fullmatch(value)
    if match is None:
        raise ValueError(
            "accelerator runtime must use cuda-<version> or cann-<version>"
        )
    parsed = _parse_version(match.group(2), "accelerator runtime version")
    return _compact_version(parsed, "accelerator runtime version")


def compact_mooncake_version(value: object) -> str:
    """Return the canonical compact token for a Mooncake version."""
    return _compact_version(
        _parse_version(value, "Mooncake version"), "Mooncake version"
    )


def normalize_variant(value: object) -> str:
    """Normalize one non-empty OCI/PEP 503-safe variant token."""
    if not isinstance(value, str) or _VARIANT.fullmatch(value) is None:
        raise ValueError("variant must be a non-empty OCI/PEP 503-safe token")
    normalized = canonicalize_name(value)
    if not normalized or _COMPACT.fullmatch(normalized.replace("-", "")) is None:
        raise ValueError("variant has no canonical token")
    return normalized


def python_version_from_abi(value: object) -> str:
    """Derive the dotted CPython version represented by a cpXY ABI."""
    if not isinstance(value, str):
        raise ValueError("Python ABI must be a string")
    match = re.fullmatch(r"cp([0-9])([0-9]{1,2})", value, re.ASCII)
    if match is None:
        raise ValueError("Python ABI must use canonical cpXY form")
    return f"{match.group(1)}.{int(match.group(2))}"
