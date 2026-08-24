"""Deterministic, entirely non-executing retention cleanup planning."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .common import canonical_json, sha256_envelope


class CleanupError(ValueError):
    """Raised when cleanup inventory cannot be interpreted unambiguously."""


_TOP_KEYS = {
    "kind",
    "schema_version",
    "mode",
    "objects",
    "references",
    "failures",
}
_OBJECT_KEYS = {
    "id",
    "kind",
    "channel",
    "coordinate",
    "identity",
    "created_at",
    "state",
}
_REFERENCE_KEYS = {"id", "object_id", "identity", "source", "active"}
_FAILURE_KEYS = {"object_id", "reason"}
_CHANNELS = {"pr", "develop", "nightly", "draft", "rc", "stable", "hotfix"}
_PROTECTED_CHANNELS = {"rc", "stable", "hotfix"}
_TEMPORARY_CHANNELS = _CHANNELS - _PROTECTED_CHANNELS
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILE_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_OCI_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CleanupError(f"{name} must be a mapping")
    return value


def _exact(value: object, name: str, keys: set[str]) -> dict[str, Any]:
    mapping = _mapping(value, name)
    if set(mapping) != keys:
        raise CleanupError(
            f"{name} keys mismatch: missing={sorted(keys - set(mapping))} "
            f"unknown={sorted(set(mapping) - keys)}"
        )
    return mapping


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise CleanupError(f"{name} must be a list")
    return value


def _safe_string(value: object, name: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CleanupError(f"{name} must be a non-empty safe string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CleanupError(f"{name} must be a non-empty safe string")
    if identifier and not _ID.fullmatch(value):
        raise CleanupError(f"{name} must be a non-empty safe string")
    return value


def _timestamp(value: object, name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise CleanupError(f"{name} must be canonical RFC3339 UTC with Z")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise CleanupError(f"{name} must be canonical RFC3339 UTC with Z") from error
    return value, parsed


def _identity(value: object, kind: str, name: str) -> str:
    if not isinstance(value, str):
        raise CleanupError(f"{name} identity must be a string")
    pattern = _FILE_IDENTITY if kind in {"artifact", "chart"} else _OCI_IDENTITY
    if not pattern.fullmatch(value):
        expected = (
            "64 lowercase hex characters"
            if kind in {"artifact", "chart"}
            else "sha256 OCI digest"
        )
        raise CleanupError(f"{name} identity must be {expected}")
    return value


def _reference_identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not (
        _FILE_IDENTITY.fullmatch(value) or _OCI_IDENTITY.fullmatch(value)
    ):
        raise CleanupError(f"{name} identity must be a file hash or OCI digest")
    return value


def _retention(config: dict[str, Any]) -> dict[str, int | None]:
    expected: dict[str, int | None] = {
        "develop": 14,
        "draft": 30,
        "hotfix": None,
        "nightly": 14,
        "pr": 7,
        "rc": None,
        "stable": None,
    }
    value = config.get("retention_days")
    if value != expected:
        raise CleanupError("retention config drift; refusing cleanup planning")
    return expected


def build_cleanup_plan(
    config: dict[str, Any], inventory_value: object, as_of_value: str
) -> dict[str, Any]:
    """Validate a closed-world inventory and return a sorted preview only."""
    retention = _retention(config)
    _, as_of = _timestamp(as_of_value, "as-of")
    inventory = _exact(inventory_value, "cleanup inventory", _TOP_KEYS)
    if (
        inventory["kind"] != "cleanup-inventory"
        or inventory["schema_version"] != 2
        or inventory["mode"] != "read-only"
    ):
        raise CleanupError(
            "cleanup inventory kind, schema_version, or mode is unsupported"
        )

    objects: list[dict[str, Any]] = []
    objects_by_id: dict[str, dict[str, Any]] = {}
    coordinates: set[tuple[str, str]] = set()
    identities: set[str] = set()
    for index, raw in enumerate(_list(inventory["objects"], "objects")):
        item = _exact(raw, f"objects[{index}]", _OBJECT_KEYS)
        object_id = _safe_string(item["id"], f"objects[{index}].id", identifier=True)
        if object_id in objects_by_id:
            raise CleanupError(f"duplicate object id: {object_id}")
        kind = item["kind"]
        if kind not in {"artifact", "image", "chart"}:
            raise CleanupError(f"objects[{index}].kind is unsupported")
        channel = item["channel"]
        if channel not in _CHANNELS:
            raise CleanupError(f"objects[{index}].channel is unknown")
        identity = _identity(item["identity"], kind, f"objects[{index}]")
        created_text, created_at = _timestamp(
            item["created_at"], f"objects[{index}].created_at"
        )
        if created_at > as_of:
            raise CleanupError(f"objects[{index}].created_at is in the future")
        state = item["state"]
        if state not in {"temporary", "protected"}:
            raise CleanupError(f"objects[{index}].state is unsupported")
        coordinate = _safe_string(item["coordinate"], f"objects[{index}].coordinate")
        coordinate_key = (kind, coordinate)
        if coordinate_key in coordinates:
            raise CleanupError(f"duplicate coordinate for kind {kind}: {coordinate}")
        coordinates.add(coordinate_key)
        parsed = {
            "id": object_id,
            "kind": kind,
            "channel": channel,
            "coordinate": coordinate,
            "identity": identity,
            "created_at": created_text,
            "_created_at": created_at,
            "state": state,
        }
        objects.append(parsed)
        objects_by_id[object_id] = parsed
        identities.add(identity)

    active_identities: set[str] = set()
    references: list[dict[str, Any]] = []
    reference_ids: set[str] = set()
    for index, raw in enumerate(_list(inventory["references"], "references")):
        item = _exact(raw, f"references[{index}]", _REFERENCE_KEYS)
        reference_id = _safe_string(
            item["id"], f"references[{index}].id", identifier=True
        )
        if reference_id in reference_ids:
            raise CleanupError(f"duplicate reference id: {reference_id}")
        reference_ids.add(reference_id)
        identity = _reference_identity(item["identity"], f"references[{index}]")
        object_id = item["object_id"]
        if object_id is not None:
            object_id = _safe_string(
                object_id, f"references[{index}].object_id", identifier=True
            )
            if object_id not in objects_by_id:
                raise CleanupError(f"dangling reference object_id: {object_id}")
            if objects_by_id[object_id]["identity"] != identity:
                raise CleanupError(
                    f"reference identity drift for object id: {object_id}"
                )
        elif identity not in identities:
            raise CleanupError(f"dangling reference identity: {identity}")
        if item["source"] not in {"active-release", "active-draft", "shared-object"}:
            raise CleanupError(f"references[{index}].source is unsupported")
        if not isinstance(item["active"], bool):
            raise CleanupError(f"references[{index}].active must be a boolean")
        if item["active"]:
            active_identities.add(identity)
        references.append(
            {
                "active": item["active"],
                "id": reference_id,
                "identity": identity,
                "object_id": object_id,
                "source": item["source"],
            }
        )

    failures: dict[str, str] = {}
    for index, raw in enumerate(_list(inventory["failures"], "failures")):
        item = _exact(raw, f"failures[{index}]", _FAILURE_KEYS)
        object_id = _safe_string(
            item["object_id"], f"failures[{index}].object_id", identifier=True
        )
        if object_id not in objects_by_id:
            raise CleanupError(f"dangling failure object_id: {object_id}")
        if object_id in failures:
            raise CleanupError(f"duplicate failure object_id: {object_id}")
        failures[object_id] = _safe_string(item["reason"], f"failures[{index}].reason")

    live_identities = set(active_identities)
    expired: dict[str, bool] = {}
    for item in objects:
        days = retention[item["channel"]]
        try:
            retention_boundary = (
                as_of - timedelta(days=days) if days is not None else None
            )
        except OverflowError as error:
            raise CleanupError(
                "as-of retention boundary underflows supported datetime range"
            ) from error
        is_expired = (
            item["channel"] in _TEMPORARY_CHANNELS
            and item["state"] == "temporary"
            and days is not None
            and retention_boundary is not None
            and item["_created_at"] < retention_boundary
        )
        expired[item["id"]] = is_expired
        if not is_expired:
            live_identities.add(item["identity"])

    operations: list[dict[str, Any]] = []
    counts = {"delete_preview": 0, "skip": 0, "would_fail": 0}
    for item in sorted(objects, key=lambda value: value["id"]):
        if item["channel"] in _PROTECTED_CHANNELS:
            action, reason = "skip", "protected-channel"
        elif item["state"] == "protected":
            action, reason = "skip", "protected-state"
        elif not expired[item["id"]]:
            action, reason = "skip", "not-expired"
        elif item["identity"] in live_identities:
            action, reason = "skip", "shared-or-live-reference"
        elif item["id"] in failures:
            action, reason = "would-fail", failures[item["id"]]
        else:
            action, reason = "delete-preview", "expired"
        counts[action.replace("-", "_")] += 1
        operations.append(
            {
                "action": action,
                "channel": item["channel"],
                "coordinate": item["coordinate"],
                "executed": False,
                "identity": item["identity"],
                "kind": item["kind"],
                "object_id": item["id"],
                "reason": reason,
            }
        )

    normalized_inventory = {
        "failures": [
            {"object_id": object_id, "reason": reason}
            for object_id, reason in sorted(failures.items())
        ],
        "kind": "cleanup-inventory",
        "mode": "read-only",
        "objects": [
            {key: value for key, value in item.items() if key != "_created_at"}
            for item in sorted(objects, key=lambda value: value["id"])
        ],
        "references": sorted(references, key=lambda value: value["id"]),
        "schema_version": 2,
    }
    unsigned = {
        "as_of": as_of_value,
        "inventory_sha256": hashlib.sha256(
            canonical_json(normalized_inventory).encode()
        ).hexdigest(),
        "kind": "cleanup-plan",
        "mode": "read-only",
        "operations": operations,
        "schema_version": 2,
        "summary": {**counts, "total": len(operations)},
    }
    return sha256_envelope(unsigned)
