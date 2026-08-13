"""Canonical JSON and strict scalar helpers for production evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_LOWER_COMMIT_SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]", re.ASCII)


class ProductionError(ValueError):
    """A controlled validation failure safe to show in Actions logs."""


def _object_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ProductionError(f"non-finite JSON number: {value}")


def canonical_bytes(value: object) -> bytes:
    """Return the sole canonical JSON encoding used for hashes and envelopes."""

    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ProductionError(f"value is not canonical JSON: {error}") from None
    return rendered.encode("utf-8")


def load_json(path: Path, label: str) -> Any:
    """Load one UTF-8 JSON document while rejecting parser extensions."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProductionError(f"cannot read {label}: {error}") from None
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProductionError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProductionError(f"{label} is not valid UTF-8") from None
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except ProductionError:
        raise
    except json.JSONDecodeError as error:
        raise ProductionError(
            f"invalid {label} JSON at line {error.lineno}, column {error.colno}"
        ) from None


def require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProductionError(
            f"{label} keys must be exactly {sorted(expected)}; "
            f"missing={missing}; extra={extra}"
        )


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        raise ProductionError(f"{label} must be a non-empty control-free string")
    return value


def require_lower_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _LOWER_SHA256.fullmatch(value) is None:
        raise ProductionError(f"{label} must be a full lowercase SHA256")
    return value


def require_lower_commit_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _LOWER_COMMIT_SHA.fullmatch(value) is None:
        raise ProductionError(f"{label} must be a full lowercase 40-hex commit SHA")
    return value


def require_sha256_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ProductionError(f"{label} must be sha256:<64 lowercase hex>")
    require_lower_sha256(value.removeprefix("sha256:"), label)
    return value


def require_posix_path(value: object, label: str) -> str:
    path = require_string(value, label)
    if "\\" in path or path.startswith("/") or path.endswith("/"):
        raise ProductionError(f"{label} must be a relative POSIX path")
    parsed = PurePosixPath(path)
    if not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ProductionError(f"{label} must be a normalized relative POSIX path")
    if parsed.as_posix() != path:
        raise ProductionError(f"{label} must be a normalized relative POSIX path")
    return path


def sha256_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add a self-independent SHA256 over all fields except ``sha256``."""

    if not isinstance(value, Mapping):
        raise ProductionError("envelope value must be an object")
    document = dict(value)
    if "sha256" in document:
        raise ProductionError("unsigned envelope must not contain sha256")
    document["sha256"] = hashlib.sha256(canonical_bytes(document)).hexdigest()
    return document


def write_json(path: Path, value: object, label: str) -> None:
    """Create a canonical JSON output without overwriting an existing file."""

    try:
        with path.open("xb") as stream:
            stream.write(canonical_bytes(value))
            stream.write(b"\n")
    except OSError as error:
        raise ProductionError(f"cannot create {label}: {error}") from None


def verify_envelope(
    document: Mapping[str, Any],
    *,
    kind: str,
    schema_version: int,
    exact_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Verify identity, shape (when supplied), and an envelope self-digest."""

    if not isinstance(document, Mapping):
        raise ProductionError("envelope must be an object")
    result = dict(document)
    if exact_keys is not None:
        require_exact_keys(result, exact_keys, f"{kind} envelope")
    if result.get("kind") != kind:
        raise ProductionError(f"envelope kind must be {kind}")
    if type(result.get("schema_version")) is not int:
        raise ProductionError("envelope schema_version must be an integer")
    if result["schema_version"] != schema_version:
        raise ProductionError(f"envelope schema_version must be {schema_version}")
    actual = require_lower_sha256(result.get("sha256"), "envelope sha256")
    unsigned = dict(result)
    del unsigned["sha256"]
    expected = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ProductionError("envelope sha256 does not match canonical content")
    return result
