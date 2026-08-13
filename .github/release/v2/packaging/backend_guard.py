"""Backend-neutral mixed-install guard bundled by every future UCM wheel.

This module deliberately receives package metadata instead of inspecting CUDA,
CANN, devices, or host drivers.  Backend selection is an install-time choice.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Iterable, Mapping
from typing import Any

LEGACY_DISTRIBUTION = "uc-manager"
V2_DISTRIBUTIONS = (
    "uc-manager-cuda",
    "uc-manager-cann-a2",
    "uc-manager-cann-a3",
)
UCM_DISTRIBUTIONS = (LEGACY_DISTRIBUTION, *V2_DISTRIBUTIONS)
_NORMALIZE = re.compile(r"[-_.]+")
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_COMPACT = re.compile(r"[-_.\s]+")
_COMMON_PEP440 = re.compile(
    r"^v?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
    r"(?:[-_.]?(?:a|alpha|b|beta|rc|c|pre|preview)(?:[-_.]?[0-9]+)?)?"
    r"(?:(?:[-_.]?(?:post|rev|r)(?:[-_.]?[0-9]+)?)|(?:[-_][0-9]+))?"
    r"(?:[-_.]?dev(?:[-_.]?[0-9]+)?)?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?$",
    re.IGNORECASE,
)


class GuardError(RuntimeError):
    """Base class for errors that must describe a safe recovery path."""


class BackendConflictError(GuardError):
    """Raised when a Python environment has more than one UCM distribution."""


class MetadataError(GuardError):
    """Raised when installed-distribution metadata cannot be trusted."""


def recovery_guidance() -> str:
    """Return one deterministic recovery procedure for every unsafe state."""
    names = " ".join(UCM_DISTRIBUTIONS)
    return (
        f"Uninstall all UCM distributions: python -m pip uninstall -y {names}; "
        "then install exactly one backend, for example: "
        "python -m pip install uc-manager-cuda==<version>."
    )


def _metadata_error(reason: str) -> MetadataError:
    return MetadataError(f"{reason}. {recovery_guidance()}")


def canonicalize_name(name: str) -> str:
    """Normalize one package name according to PEP 503."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise _metadata_error(
            "installed distribution name must be a non-empty PEP 508 name"
        )
    if not _NAME.fullmatch(name):
        raise _metadata_error(
            "installed distribution name must be a valid PEP 508 name"
        )
    return _NORMALIZE.sub("-", name).lower()


def _validated_version(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _metadata_error(
            "installed distribution version must be a non-empty PEP 440 version"
        )
    if not _COMMON_PEP440.fullmatch(value):
        raise _metadata_error(
            "installed distribution version must use a supported PEP 440 form"
        )
    return value


def _declares_ucm(record: Mapping[str, Any]) -> bool:
    top_level = record.get("top_level", ())
    if not isinstance(top_level, (list, tuple, set)):
        return False
    return any(isinstance(item, str) and item.strip() == "ucm" for item in top_level)


def _looks_like_ucm_family(name: object, record: Mapping[str, Any]) -> bool:
    if _declares_ucm(record):
        return True
    if not isinstance(name, str):
        return False
    return _COMPACT.sub("", name.strip().lower()).startswith("ucmanager")


def _normalize_records(
    records: Iterable[Mapping[str, Any]], *, strict_metadata: bool
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            if strict_metadata:
                raise _metadata_error(
                    f"installed distribution at index {index} must be an object"
                )
            continue
        name_value = record.get("name")
        suspected = _looks_like_ucm_family(name_value, record)
        if strict_metadata and set(record) != {"name", "version"}:
            raise _metadata_error(
                f"installed distribution at index {index} must contain exactly name and version"
            )
        try:
            name = canonicalize_name(name_value)
            version = _validated_version(record.get("version"))
        except MetadataError:
            if strict_metadata or suspected:
                raise
            continue
        if _declares_ucm(record) and name not in V2_DISTRIBUTIONS:
            raise _metadata_error(
                f"unapproved distribution provides top-level ucm: {name}=={version}"
            )
        if suspected and name not in UCM_DISTRIBUTIONS:
            raise _metadata_error(
                f"unapproved UCM-family distribution detected: {name}=={version}"
            )
        if name in UCM_DISTRIBUTIONS:
            normalized.append({"name": name, "version": version})
    return sorted(normalized, key=lambda item: (item["name"], item["version"]))


def check_environment(
    records: Iterable[Mapping[str, Any]], *, strict_metadata: bool = True
) -> dict[str, Any]:
    """Return a safe environment state or fail closed with remediation steps."""
    relevant = _normalize_records(records, strict_metadata=strict_metadata)
    names = {item["name"] for item in relevant}
    if LEGACY_DISTRIBUTION in names:
        found = ", ".join(f"{item['name']}=={item['version']}" for item in relevant)
        raise MetadataError(
            f"legacy distribution is unsafe for a v2 environment: {found}. "
            f"{recovery_guidance()}"
        )
    duplicate = len(relevant) != len(names)
    if duplicate or len(names) > 1:
        found = ", ".join(f"{item['name']}=={item['version']}" for item in relevant)
        raise BackendConflictError(
            f"conflicting UCM distributions detected: {found}. {recovery_guidance()}"
        )
    if not relevant:
        return {"installed": [], "status": "absent"}
    return {"installed": relevant, "status": "compatible"}


def installed_distributions() -> list[dict[str, Any]]:
    """Read installed package metadata only; do not infer a backend from hardware."""
    records: list[dict[str, Any]] = []
    for distribution in importlib.metadata.distributions():
        try:
            name = distribution.metadata.get("Name")
        except Exception:  # Metadata is third-party input during host inspection.
            name = None
        try:
            version: object = distribution.version
        except Exception:  # Metadata is third-party input during host inspection.
            version = None
        try:
            top_level = (distribution.read_text("top_level.txt") or "").splitlines()
        except Exception:  # Metadata is third-party input during host inspection.
            top_level = []
        records.append({"name": name, "version": version, "top_level": top_level})
    return records


def require_single_backend() -> dict[str, Any]:
    """Runtime entry point for a wheel's import guard."""
    return check_environment(installed_distributions(), strict_metadata=False)
