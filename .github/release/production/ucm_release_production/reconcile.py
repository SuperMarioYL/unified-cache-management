"""Normalize production channel inventory and plan no-overwrite publication."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .common import (
    ProductionError,
    require_exact_keys,
    require_sha256_digest,
    require_string,
    sha256_envelope,
    verify_envelope,
)
from .config import validate_config
from .tags import TagIntent

_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", re.ASCII
)
_COORDINATE = re.compile(
    r"(?:github-release|github-release-asset)://[^\x00-\x20\x7f]+|"
    r"(?:oci://)?ghcr\.io/[a-z0-9._/-]+(?::[^\x00-\x20\x7f]+|@sha256:[0-9a-f]{64})",
    re.ASCII,
)
_STAGES = {"draft", "rc", "stable", "hotfix"}
_STATES = {"complete", "partial"}
_INVENTORY_KEYS = {
    "kind",
    "schema_version",
    "repository",
    "repository_id",
    "objects",
    "sha256",
}
_OBJECT_KEYS = {"coordinate", "stage", "state", "identity"}


def _repository(value: object) -> str:
    repository = require_string(value, "inventory repository")
    if _REPOSITORY.fullmatch(repository) is None:
        raise ProductionError("inventory repository must be one owner/name pair")
    return repository


def _repository_id(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ProductionError("inventory repository_id must be a positive integer")
    return value


def _coordinate(value: object) -> str:
    coordinate = require_string(value, "inventory coordinate")
    if _COORDINATE.fullmatch(coordinate) is None:
        raise ProductionError("inventory coordinate is outside approved channels")
    return coordinate


def _normalize_objects(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProductionError("inventory objects must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ProductionError(f"inventory object {index} must be an object")
        require_exact_keys(item, _OBJECT_KEYS, f"inventory object {index}")
        coordinate = _coordinate(item["coordinate"])
        if coordinate in seen:
            raise ProductionError(
                f"inventory contains duplicate coordinate: {coordinate}"
            )
        seen.add(coordinate)
        stage = item["stage"]
        if stage not in _STAGES:
            raise ProductionError("inventory object stage is invalid")
        state = item["state"]
        if state not in _STATES:
            raise ProductionError("inventory object state is invalid")
        identity = item["identity"]
        if state == "complete":
            require_sha256_digest(identity, "complete inventory identity")
        elif identity is not None:
            require_sha256_digest(identity, "partial inventory identity")
        result.append(
            {
                "coordinate": coordinate,
                "stage": stage,
                "state": state,
                "identity": identity,
            }
        )
    return sorted(result, key=lambda item: item["coordinate"])


def build_inventory(
    repository: str, repository_id: int, objects: list[dict[str, object]]
) -> dict[str, Any]:
    return sha256_envelope(
        {
            "kind": "ucm-production-channel-inventory",
            "schema_version": 1,
            "repository": _repository(repository),
            "repository_id": _repository_id(repository_id),
            "objects": _normalize_objects(objects),
        }
    )


def reopen_inventory(value: object) -> dict[str, Any]:
    inventory = verify_envelope(
        value,
        kind="ucm-production-channel-inventory",
        schema_version=1,
        exact_keys=_INVENTORY_KEYS,
    )
    normalized = build_inventory(
        inventory["repository"], inventory["repository_id"], inventory["objects"]
    )
    if normalized != inventory:
        raise ProductionError("channel inventory is not canonical")
    return inventory


def _desired_items(
    intent: TagIntent, candidate: dict[str, Any], config: dict[str, Any]
) -> list[dict[str, str]]:
    repository = candidate["repository"]
    owner = repository.split("/", 1)[0].lower()
    image_by_profile = {
        item["profile_id"]: item for item in config["products"]["images"]
    }
    draft = intent.stage == "draft"
    items: list[dict[str, str]] = []
    for wheel in candidate["wheels"]:
        filename = PurePosixPath(wheel["path"]).name
        items.append(
            {
                "kind": "wheel-asset",
                "coordinate": (
                    f"github-release-asset://{repository}/{intent.tag_name}/{filename}"
                ),
                "desired_identity": wheel["file_sha256"],
            }
        )
    for member in candidate["image_members"]:
        product = image_by_profile[member["profile_id"]]
        basename = product["draft_basename"] if draft else product["basename"]
        items.append(
            {
                "kind": "image-member",
                "coordinate": (
                    f"ghcr.io/{owner}/{basename}@{member['manifest_digest']}"
                ),
                "desired_identity": member["record_sha256"],
            }
        )
    for index in candidate["image_indexes"]:
        product = image_by_profile[index["profile_id"]]
        basename = product["draft_basename"] if draft else product["basename"]
        items.append(
            {
                "kind": "image-index",
                "coordinate": f"ghcr.io/{owner}/{basename}:{intent.image_tag}",
                "desired_identity": index["record_sha256"],
            }
        )
    chart = candidate["chart"]
    chart_filename = PurePosixPath(chart["path"]).name
    items.append(
        {
            "kind": "chart-asset",
            "coordinate": (
                f"github-release-asset://{repository}/{intent.tag_name}/{chart_filename}"
            ),
            "desired_identity": chart["file_sha256"],
        }
    )
    if config["channels"][intent.stage]["publish_chart"]:
        items.append(
            {
                "kind": "chart-oci",
                "coordinate": (
                    f"oci://ghcr.io/{owner}/charts/unified-cache-pd:"
                    f"{intent.chart_version}"
                ),
                "desired_identity": chart["content_tree_sha256"],
            }
        )
    items.append(
        {
            "kind": "github-release",
            "coordinate": f"github-release://{repository}/{intent.tag_name}",
            "desired_identity": "sha256:" + candidate["sha256"],
        }
    )
    for item in items:
        _coordinate(item["coordinate"])
        require_sha256_digest(item["desired_identity"], "desired channel identity")
    if len({item["coordinate"] for item in items}) != len(items):
        raise ProductionError("desired publication coordinates are not unique")
    return items


def plan_publication(
    intent: TagIntent,
    candidate_value: object,
    inventory_value: object,
    config_value: object,
) -> dict[str, Any]:
    """Return an atomic create/reuse/blocked plan; conflict means zero writes."""

    config = validate_config(config_value)
    candidate = verify_envelope(
        candidate_value,
        kind="ucm-production-candidate-envelope",
        schema_version=1,
    )
    inventory = reopen_inventory(inventory_value)
    if (
        candidate.get("repository") != inventory["repository"]
        or candidate.get("repository_id") != inventory["repository_id"]
    ):
        raise ProductionError(
            "candidate repository differs from channel inventory repository"
        )
    if (
        candidate.get("stage") != intent.stage
        or candidate.get("tag_name") != intent.tag_name
    ):
        raise ProductionError("candidate Tag intent differs from requested publication")
    remote = {item["coordinate"]: item for item in inventory["objects"]}
    planned: list[dict[str, Any]] = []
    for desired in _desired_items(intent, candidate, config):
        existing = remote.get(desired["coordinate"])
        if existing is None:
            decision = "create"
            reason = "absent"
            observed_identity = None
        elif existing["stage"] != intent.stage:
            decision = "blocked"
            reason = "cross-stage-occupancy"
            observed_identity = existing["identity"]
        elif existing["state"] == "partial":
            decision = "blocked"
            reason = "partial-publication"
            observed_identity = existing["identity"]
        elif existing["identity"] == desired["desired_identity"]:
            decision = "reuse"
            reason = "identical"
            observed_identity = existing["identity"]
        else:
            decision = "blocked"
            reason = "identity-conflict"
            observed_identity = existing["identity"]
        planned.append(
            {
                **desired,
                "decision": decision,
                "reason": reason,
                "observed_identity": observed_identity,
            }
        )
    blocked = [item for item in planned if item["decision"] == "blocked"]
    publishable = not blocked
    operations = (
        [
            {
                "action": "create",
                "kind": item["kind"],
                "coordinate": item["coordinate"],
                "identity": item["desired_identity"],
            }
            for item in planned
            if item["decision"] == "create"
        ]
        if publishable
        else []
    )
    return sha256_envelope(
        {
            "kind": "ucm-production-publish-plan",
            "schema_version": 1,
            "repository": inventory["repository"],
            "repository_id": inventory["repository_id"],
            "stage": intent.stage,
            "tag_name": intent.tag_name,
            "source_sha": candidate["source_sha"],
            "candidate_sha256": candidate["sha256"],
            "inventory_sha256": inventory["sha256"],
            "publishable": publishable,
            "items": planned,
            "operations": operations,
            "blockers": [
                {
                    "coordinate": item["coordinate"],
                    "reason": item["reason"],
                }
                for item in blocked
            ],
        }
    )
