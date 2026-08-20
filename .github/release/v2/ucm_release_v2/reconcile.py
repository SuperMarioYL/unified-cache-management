"""Strict offline, non-executing reconciliation for protected releases."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .common import SafePathError, canonical_json, safe_posix_path, sha256_envelope
from .environment import EnvironmentError, export_request, verify_result
from .lifecycle import LifecycleError, validate_plan, verify_envelope


class ReconcileError(ValueError):
    """Raised when offline release inputs are ambiguous or malformed."""


_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RC_VERSION = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)rc([1-9][0-9]*)$")
_STABLE_VERSION = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
_PLATFORMS = ("linux/amd64", "linux/arm64")
_MANIFEST_KEYS = {
    "artifacts",
    "kind",
    "lifecycle_plan_sha256",
    "mode",
    "schema_version",
    "sha256",
    "source_sha",
    "stage",
    "validation",
    "version",
}
_MANIFEST_VALIDATION = {
    "file_bytes": "passed",
    "lifecycle_plan": "passed",
    "oci_identity": "passed",
    "product_closure": "passed",
    "registry_readback": "unexecuted",
    "runtime": "unexecuted",
}
_INVENTORY_KEYS = {"kind", "schema_version", "mode", "targets"}
_TARGET_KEYS = {"coordinate", "identity", "kind", "name"}
_PROMOTION_KEYS = {
    "accepted",
    "kind",
    "mode",
    "reason",
    "schema_version",
    "sha256",
    "source_artifact_manifest_sha256",
    "source_lifecycle_plan_sha256",
    "source_sha",
    "source_stage",
    "source_version",
    "target_stage",
    "target_version",
}
_ENVIRONMENT_RESULT_KEYS = {
    "artifact_manifest_sha256",
    "artifacts",
    "checks",
    "environment",
    "evidence_level",
    "kind",
    "lifecycle_plan_sha256",
    "mode",
    "nonce",
    "request_sha256",
    "schema_version",
    "sha256",
    "source_sha",
    "stage",
    "verdict",
    "version",
}
_RECONCILE_KEYS = {
    "artifact_manifest_sha256",
    "blockers",
    "environment_request_sha256",
    "environment_result_sha256",
    "environment_lifecycle_plan_sha256",
    "environment_manifest_sha256",
    "inventory_sha256",
    "kind",
    "lifecycle_plan_sha256",
    "mode",
    "operations",
    "production_ready",
    "promotion_evidence_sha256",
    "promotion_source_lifecycle_plan_sha256",
    "promotion_source_manifest_sha256",
    "schema_version",
    "sha256",
    "simulated_environment",
    "source_sha",
    "stage",
    "status",
    "version",
}


def load_json(path: Path, label: str) -> object:
    """Read external JSON while rejecting duplicate keys at every nesting level."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ReconcileError(f"{label} contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeError) as error:
        raise ReconcileError(f"cannot read {label}: {path}") from error
    except json.JSONDecodeError as error:
        raise ReconcileError(f"{label} must be valid JSON") from error


def _exact(value: object, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReconcileError(f"{label} must contain exactly {', '.join(sorted(keys))}")
    return value


def _trimmed(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReconcileError(f"{label} must be a non-empty trimmed string")
    return value


def _choice(value: object, label: str, choices: tuple[str, ...]) -> str:
    result = _trimmed(value, label)
    if result not in choices:
        raise ReconcileError(f"{label} must be one of {', '.join(choices)}")
    return result


def _hex(value: object, label: str, pattern: re.Pattern[str], length: int) -> str:
    result = _trimmed(value, label)
    if not pattern.fullmatch(result):
        raise ReconcileError(
            f"{label} must be exactly {length} lowercase hexadecimal characters"
        )
    return result


def _digest(value: object, label: str) -> str:
    result = _trimmed(value, label)
    if not _OCI_DIGEST.fullmatch(result):
        raise ReconcileError(f"{label} must be a lowercase sha256 OCI digest")
    return result


def _path(value: object, label: str) -> str:
    try:
        return safe_posix_path(value, label)
    except SafePathError as error:
        raise ReconcileError(str(error)) from error


def _artifact(item: object, index: int, version: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ReconcileError(f"artifact manifest artifacts[{index}] must be an object")
    kind = _choice(
        item.get("kind"), f"artifacts[{index}].kind", ("wheel", "image", "chart")
    )
    common = {
        "coordinate": _trimmed(
            item.get("coordinate"), f"artifacts[{index}].coordinate"
        ),
        "kind": kind,
        "name": _trimmed(item.get("name"), f"artifacts[{index}].name"),
        "version": _trimmed(item.get("version"), f"artifacts[{index}].version"),
    }
    if common["version"] != version:
        raise ReconcileError("artifact version does not match release version")
    if kind in {"wheel", "chart"}:
        value = _exact(
            item,
            f"artifacts[{index}]",
            {"coordinate", "kind", "name", "path", "sha256", "size", "version"},
        )
        checksum = _hex(value["sha256"], f"artifacts[{index}].sha256", _HEX_64, 64)
        size = value["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReconcileError(
                f"artifacts[{index}].size must be a non-negative integer"
            )
        return common | {
            "path": _path(value["path"], f"artifacts[{index}].path"),
            "sha256": checksum,
            "size": size,
        }
    value = _exact(
        item,
        f"artifacts[{index}]",
        {"coordinate", "digest", "kind", "name", "platforms", "version"},
    )
    raw_platforms = value["platforms"]
    if not isinstance(raw_platforms, list) or len(raw_platforms) != 2:
        raise ReconcileError(
            f"artifacts[{index}].platforms must contain exactly two entries"
        )
    platforms: list[dict[str, str]] = []
    for platform_index, raw in enumerate(raw_platforms):
        platform = _exact(
            raw,
            f"artifacts[{index}].platforms[{platform_index}]",
            {"digest", "platform"},
        )
        platforms.append(
            {
                "digest": _digest(platform["digest"], "platform digest"),
                "platform": _choice(platform["platform"], "platform name", _PLATFORMS),
            }
        )
    if tuple(item["platform"] for item in platforms) != _PLATFORMS:
        raise ReconcileError(
            f"artifacts[{index}].platforms must be canonical linux/amd64, linux/arm64"
        )
    return common | {
        "digest": _digest(value["digest"], f"artifacts[{index}].digest"),
        "platforms": platforms,
    }


def _artifacts(value: object, version: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 7:
        raise ReconcileError(
            "artifact manifest must contain exactly 3 wheels, 3 images, and 1 chart"
        )
    normalized = [_artifact(item, index, version) for index, item in enumerate(value)]
    counts = {
        kind: sum(item["kind"] == kind for item in normalized)
        for kind in ("wheel", "image", "chart")
    }
    if counts != {"wheel": 3, "image": 3, "chart": 1}:
        raise ReconcileError(
            "artifact manifest must contain exactly 3 wheels, 3 images, and 1 chart"
        )
    if value != normalized or normalized != sorted(
        normalized, key=lambda item: (item["kind"], item["name"])
    ):
        raise ReconcileError(
            "artifact manifest artifacts must use canonical stable ordering"
        )
    return normalized


def load_release_inputs(
    config: dict[str, Any],
    lifecycle_path: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reopen and semantically validate the protected plan and transport manifest."""
    try:
        plan = validate_plan(config, load_json(lifecycle_path, "lifecycle plan"))
    except LifecycleError as error:
        raise ReconcileError(f"lifecycle plan is invalid: {error}") from error
    if (
        plan["stage"] not in {"rc", "stable", "hotfix"}
        or plan["repository_role"] != "production"
    ):
        raise ReconcileError(
            "reconciliation requires an RC, Stable, or Hotfix production plan"
        )
    manifest = _exact(
        load_json(manifest_path, "artifact manifest"),
        "artifact manifest",
        _MANIFEST_KEYS,
    )
    try:
        verify_envelope(manifest, kind="artifact-manifest")
    except LifecycleError as error:
        raise ReconcileError(f"artifact manifest is invalid: {error}") from error
    _hex(manifest["source_sha"], "artifact manifest source_sha", _HEX_40, 40)
    _choice(manifest["stage"], "artifact manifest stage", ("rc", "stable", "hotfix"))
    version = _trimmed(manifest["version"], "artifact manifest version")
    _hex(
        manifest["lifecycle_plan_sha256"],
        "artifact manifest lifecycle_plan_sha256",
        _HEX_64,
        64,
    )
    for key in ("source_sha", "stage", "version"):
        if manifest[key] != plan[key]:
            raise ReconcileError(
                f"artifact manifest {key} does not match lifecycle plan"
            )
    if manifest["lifecycle_plan_sha256"] != plan["sha256"]:
        raise ReconcileError(
            "artifact manifest lifecycle_plan_sha256 does not match lifecycle plan"
        )
    if manifest["validation"] != _MANIFEST_VALIDATION:
        raise ReconcileError("artifact manifest validation evidence is invalid")
    artifacts = _artifacts(manifest["artifacts"], version)
    products = [
        {"kind": item["kind"], "name": item["name"], "coordinate": item["coordinate"]}
        for item in artifacts
    ]
    if products != plan["products"]:
        raise ReconcileError(
            "artifact manifest product closure does not match lifecycle plan"
        )
    return plan, manifest


def _identity(artifact: dict[str, Any]) -> str:
    return artifact["digest"] if artifact["kind"] == "image" else artifact["sha256"]


def _inventory(
    path: Path,
    planned_artifacts: list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[tuple[str, str], dict[str, str]],
    dict[tuple[str, str], list[dict[str, str]]],
]:
    inventory = _exact(
        load_json(path, "release inventory"), "release inventory", _INVENTORY_KEYS
    )
    if inventory["kind"] != "release-inventory" or inventory["schema_version"] != 2:
        raise ReconcileError("release inventory kind and schema_version are invalid")
    if inventory["mode"] != "read-only":
        raise ReconcileError("release inventory mode must be read-only")
    raw_targets = inventory["targets"]
    if not isinstance(raw_targets, list) or len(raw_targets) > len(planned_artifacts):
        raise ReconcileError(
            "release inventory targets must be an array with at most seven entries"
        )
    expected = {(item["kind"], item["name"]) for item in planned_artifacts}
    targets: list[dict[str, str]] = []
    logical_names: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_targets):
        target = _exact(raw, f"release inventory targets[{index}]", _TARGET_KEYS)
        kind = _choice(
            target["kind"], f"targets[{index}].kind", ("wheel", "image", "chart")
        )
        name = _trimmed(target["name"], f"targets[{index}].name")
        coordinate = _trimmed(target["coordinate"], f"targets[{index}].coordinate")
        identity = (
            _digest(target["identity"], f"targets[{index}].identity")
            if kind == "image"
            else _hex(target["identity"], f"targets[{index}].identity", _HEX_64, 64)
        )
        logical = (kind, name)
        if logical not in expected:
            raise ReconcileError(
                f"release inventory contains unknown target: {kind}:{name}"
            )
        if logical in logical_names:
            raise ReconcileError(
                f"release inventory contains duplicate target: {kind}:{name}"
            )
        logical_names.add(logical)
        targets.append(
            {"coordinate": coordinate, "identity": identity, "kind": kind, "name": name}
        )
    targets.sort(key=lambda item: (item["kind"], item["name"]))
    normalized = {
        "kind": "release-inventory",
        "mode": "read-only",
        "schema_version": 2,
        "targets": targets,
    }
    coordinate_inventory: dict[tuple[str, str], list[dict[str, str]]] = {}
    for target in targets:
        coordinate_inventory.setdefault(
            (target["kind"], target["coordinate"]), []
        ).append(target)
    return (
        normalized,
        {(item["kind"], item["name"]): item for item in targets},
        coordinate_inventory,
    )


def _verify_read_only_envelope(value: dict[str, Any], kind: str) -> None:
    if (
        value.get("kind") != kind
        or value.get("schema_version") != 2
        or value.get("mode") != "read-only"
    ):
        raise ReconcileError(f"{kind} envelope must be schema_version 2 read-only")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
        raise ReconcileError(f"{kind} envelope has an invalid sha256")
    unsigned = dict(value)
    unsigned.pop("sha256")
    if hashlib.sha256(canonical_json(unsigned).encode()).hexdigest() != digest:
        raise ReconcileError(f"{kind} envelope sha256 does not match its content")


def _promotion(path: Path) -> dict[str, Any]:
    promotion = _exact(
        load_json(path, "promotion evidence"), "promotion evidence", _PROMOTION_KEYS
    )
    _verify_read_only_envelope(promotion, "promotion-evidence")
    _choice(promotion["source_stage"], "promotion source_stage", ("rc", "stable"))
    source_version = _trimmed(promotion["source_version"], "promotion source_version")
    _choice(promotion["target_stage"], "promotion target_stage", ("stable", "hotfix"))
    _trimmed(promotion["target_version"], "promotion target_version")
    _hex(promotion["source_sha"], "promotion source_sha", _HEX_40, 40)
    _hex(
        promotion["source_lifecycle_plan_sha256"],
        "promotion source_lifecycle_plan_sha256",
        _HEX_64,
        64,
    )
    _hex(
        promotion["source_artifact_manifest_sha256"],
        "promotion source_artifact_manifest_sha256",
        _HEX_64,
        64,
    )
    if not isinstance(promotion["accepted"], bool):
        raise ReconcileError("promotion accepted must be a boolean")
    _trimmed(promotion["reason"], "promotion reason")
    if promotion["source_stage"] == "rc" and not _RC_VERSION.fullmatch(source_version):
        raise ReconcileError("RC promotion source_version must be x.y.zrcN")
    if promotion["source_stage"] == "stable" and not _STABLE_VERSION.fullmatch(
        source_version
    ):
        raise ReconcileError("Stable promotion source_version must be x.y.z")
    if not _STABLE_VERSION.fullmatch(promotion["target_version"]):
        raise ReconcileError("promotion target_version must be x.y.z")
    return promotion


def _promotion_lineage(
    config: dict[str, Any],
    promotion: dict[str, Any] | None,
    lifecycle_path: Path | None,
    manifest_path: Path | None,
) -> tuple[bool, str | None, str | None]:
    if promotion is None:
        if lifecycle_path is not None or manifest_path is not None:
            raise ReconcileError(
                "promotion source anchors require --promotion evidence"
            )
        return False, None, None
    if lifecycle_path is None and manifest_path is None:
        return False, None, None
    if lifecycle_path is None or manifest_path is None:
        raise ReconcileError(
            "--promotion-source-lifecycle-plan and --promotion-source-manifest must be provided together"
        )
    source_plan, source_manifest = load_release_inputs(
        config, lifecycle_path, manifest_path
    )
    expected = {
        "source_artifact_manifest_sha256": source_manifest["sha256"],
        "source_lifecycle_plan_sha256": source_plan["sha256"],
        "source_sha": source_plan["source_sha"],
        "source_stage": source_plan["stage"],
        "source_version": source_plan["version"],
    }
    for key, value in expected.items():
        if promotion[key] != value:
            raise ReconcileError(
                f"promotion {key} does not match reopened source lineage"
            )
    return True, source_plan["sha256"], source_manifest["sha256"]


def _promotion_blockers(
    plan: dict[str, Any],
    promotion: dict[str, Any] | None,
    *,
    anchored: bool,
) -> list[str]:
    stage = plan["stage"]
    if stage == "rc":
        if promotion is not None:
            raise ReconcileError("RC reconciliation does not accept promotion evidence")
        return []
    if promotion is None:
        return ["promotion-evidence-required"]
    blockers: list[str] = []
    if not anchored:
        blockers.append("promotion-unanchored")
    if not promotion["accepted"]:
        blockers.append("promotion-not-accepted")
    if promotion["target_stage"] != stage:
        blockers.append("promotion-target-stage-mismatch")
    if promotion["target_version"] != plan["version"]:
        blockers.append("promotion-target-version-mismatch")
    target = _STABLE_VERSION.fullmatch(plan["version"])
    assert target is not None
    major, minor, patch = (int(target.group(index)) for index in (1, 2, 3))
    if stage == "stable":
        if promotion["source_sha"] != plan["source_sha"]:
            blockers.append("promotion-source-sha-mismatch")
        if promotion["source_stage"] != "rc":
            blockers.append("promotion-source-stage-mismatch")
        source = _RC_VERSION.fullmatch(promotion["source_version"])
        if source is None or tuple(int(source.group(index)) for index in (1, 2, 3)) != (
            major,
            minor,
            patch,
        ):
            blockers.append("promotion-source-version-mismatch")
    else:
        if promotion["source_stage"] != "stable":
            blockers.append("promotion-source-stage-mismatch")
        source = _STABLE_VERSION.fullmatch(promotion["source_version"])
        if patch < 1:
            blockers.append("hotfix-target-patch-must-be-positive")
        elif source is None or tuple(
            int(source.group(index)) for index in (1, 2, 3)
        ) != (major, minor, patch - 1):
            blockers.append("promotion-source-version-mismatch")
    return blockers


def _expected_draft_version(
    plan: dict[str, Any], promotion: dict[str, Any] | None
) -> re.Pattern[str]:
    suffix = rf"\.dev0\+draft\.g\.{re.escape(plan['source_sha'][:12])}"
    if plan["stage"] == "rc":
        return re.compile(rf"^{re.escape(plan['version'])}{suffix}$")
    if plan["stage"] == "stable":
        if promotion is None:
            raise ReconcileError(
                "Stable environment replay requires promotion evidence to define the RC line"
            )
        return re.compile(rf"^{re.escape(promotion['source_version'])}{suffix}$")
    return re.compile(rf"^{re.escape(plan['version'])}rc[1-9][0-9]*{suffix}$")


def _environment_replay(
    config: dict[str, Any],
    lifecycle_path: Path | None,
    manifest_path: Path | None,
    request_path: Path | None,
    result_path: Path | None,
    plan: dict[str, Any],
    manifest: dict[str, Any],
    promotion: dict[str, Any] | None,
    *,
    stable_promotion_eligible: bool,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    if (request_path is None) != (result_path is None):
        raise ReconcileError(
            "--environment-request and --environment-result must be provided together"
        )
    if (lifecycle_path is None) != (manifest_path is None):
        raise ReconcileError(
            "--environment-lifecycle-plan and --environment-manifest must be provided together"
        )
    if request_path is None or result_path is None:
        if lifecycle_path is not None or manifest_path is not None:
            raise ReconcileError(
                "environment anchors require request and result evidence"
            )
        return "not-provided", None, None, None, None
    try:
        verification = verify_result(config, request_path, result_path)
    except EnvironmentError as error:
        raise ReconcileError(f"environment replay is invalid: {error}") from error
    request = _exact(
        load_json(request_path, "environment request"),
        "environment request",
        {
            "artifact_manifest_sha256",
            "artifacts",
            "environment",
            "evidence_level",
            "kind",
            "lifecycle_plan_sha256",
            "mode",
            "nonce",
            "operations",
            "required_checks",
            "schema_version",
            "sha256",
            "source_sha",
            "stage",
            "version",
        },
    )
    result = _exact(
        load_json(result_path, "environment result"),
        "environment result",
        _ENVIRONMENT_RESULT_KEYS,
    )
    if request["source_sha"] != plan["source_sha"]:
        raise ReconcileError(
            "environment replay source_sha does not match protected release source"
        )
    planned_logical = sorted(
        (item["kind"], item["name"]) for item in manifest["artifacts"]
    )
    replay_logical = sorted(
        (item["kind"], item["name"]) for item in request["artifacts"]
    )
    if replay_logical != planned_logical or len(set(replay_logical)) != 7:
        raise ReconcileError(
            "environment replay logical product closure does not match protected release"
        )
    version = request["version"]
    if plan["stage"] == "stable" and not stable_promotion_eligible:
        expected_version = None
    else:
        expected_version = _expected_draft_version(plan, promotion)
    if not isinstance(version, str) or (
        expected_version is not None and not expected_version.fullmatch(version)
    ):
        raise ReconcileError(
            "environment replay Draft version does not match protected release line"
        )
    if lifecycle_path is None or manifest_path is None:
        return (
            "unanchored-simulation",
            request["sha256"],
            result["sha256"],
            None,
            None,
        )
    try:
        reconstructed = export_request(
            config,
            lifecycle_path,
            manifest_path,
            request["environment"],
            request["nonce"],
        )
    except EnvironmentError as error:
        raise ReconcileError(
            f"environment origin anchors are invalid: {error}"
        ) from error
    if reconstructed != request:
        raise ReconcileError(
            "environment request does not match reopened Draft lifecycle plan and manifest"
        )
    if plan["stage"] == "stable" and not stable_promotion_eligible:
        return (
            "not-eligible",
            request["sha256"],
            result["sha256"],
            request["lifecycle_plan_sha256"],
            request["artifact_manifest_sha256"],
        )
    verdict = verification["simulated_verdict"]
    return (
        f"draft-{verdict}",
        request["sha256"],
        result["sha256"],
        request["lifecycle_plan_sha256"],
        request["artifact_manifest_sha256"],
    )


def _operations(
    artifacts: list[dict[str, Any]],
    logical_inventory: dict[tuple[str, str], dict[str, str]],
    coordinate_inventory: dict[tuple[str, str], list[dict[str, str]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    operations: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for artifact in artifacts:
        target = {key: artifact[key] for key in ("coordinate", "kind", "name")}
        identity = _identity(artifact)
        logical_key = (artifact["kind"], artifact["name"])
        coordinate_key = (artifact["kind"], artifact["coordinate"])
        existing = logical_inventory.get(logical_key)
        occupiers = coordinate_inventory.get(coordinate_key, [])
        reverse_occupied = any(
            (occupier["kind"], occupier["name"]) != logical_key
            for occupier in occupiers
        )
        if existing is None and not reverse_occupied:
            action = "create-preview"
        elif (
            existing is not None
            and not reverse_occupied
            and existing["coordinate"] == artifact["coordinate"]
            and existing["identity"] == identity
        ):
            action = "skip-identical"
        else:
            action = "conflict"
            conflicts.append(f"target-conflict:{artifact['kind']}:{artifact['name']}")
        operations.append(
            {
                "action": action,
                "executed": False,
                "identity": identity,
                "target": target,
            }
        )
    return operations, conflicts


def validate_reconcile_plan(
    value: object,
    plan: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate the strict content-addressed reconcile-plan contract for rendering."""
    document = _exact(value, "reconcile plan", _RECONCILE_KEYS)
    try:
        verify_envelope(document, kind="reconcile-plan")
    except LifecycleError as error:
        raise ReconcileError(f"reconcile plan is invalid: {error}") from error
    for field, expected in (
        ("artifact_manifest_sha256", manifest["sha256"]),
        ("lifecycle_plan_sha256", plan["sha256"]),
        ("source_sha", plan["source_sha"]),
        ("stage", plan["stage"]),
        ("version", plan["version"]),
    ):
        if document[field] != expected:
            raise ReconcileError(
                f"reconcile plan {field} does not match release inputs"
            )
    _hex(document["inventory_sha256"], "reconcile inventory_sha256", _HEX_64, 64)
    promotion_digest = document["promotion_evidence_sha256"]
    if promotion_digest is not None:
        _hex(promotion_digest, "reconcile promotion_evidence_sha256", _HEX_64, 64)
    for key in (
        "environment_lifecycle_plan_sha256",
        "environment_manifest_sha256",
        "environment_request_sha256",
        "environment_result_sha256",
        "promotion_source_lifecycle_plan_sha256",
        "promotion_source_manifest_sha256",
    ):
        environment_digest = document[key]
        if environment_digest is not None:
            _hex(environment_digest, f"reconcile {key}", _HEX_64, 64)
    if (document["environment_request_sha256"] is None) != (
        document["environment_result_sha256"] is None
    ):
        raise ReconcileError(
            "reconcile environment request/result digests must be paired"
        )
    if (document["environment_lifecycle_plan_sha256"] is None) != (
        document["environment_manifest_sha256"] is None
    ):
        raise ReconcileError("reconcile environment anchor digests must be paired")
    if (document["promotion_source_lifecycle_plan_sha256"] is None) != (
        document["promotion_source_manifest_sha256"] is None
    ):
        raise ReconcileError("reconcile promotion source digests must be paired")
    if document["production_ready"] is not False:
        raise ReconcileError("reconcile production_ready must remain false")
    _choice(
        document["status"], "reconcile status", ("blocked", "conflict-free-preview")
    )
    _choice(
        document["simulated_environment"],
        "simulated environment",
        (
            "not-provided",
            "not-eligible",
            "unanchored-simulation",
            "draft-passed",
            "draft-failed",
        ),
    )
    simulated = document["simulated_environment"]
    request_present = document["environment_request_sha256"] is not None
    anchor_present = document["environment_lifecycle_plan_sha256"] is not None
    if simulated == "not-provided" and (request_present or anchor_present):
        raise ReconcileError("not-provided environment must not carry digests")
    if simulated == "unanchored-simulation" and (not request_present or anchor_present):
        raise ReconcileError("unanchored simulation digest state is inconsistent")
    if simulated in {"draft-passed", "draft-failed"} and not (
        request_present and anchor_present
    ):
        raise ReconcileError("Draft environment evidence requires origin anchors")
    blockers = document["blockers"]
    if not isinstance(blockers, list) or not blockers:
        raise ReconcileError(
            "reconcile blockers must be a sorted unique non-empty string array"
        )
    for index, blocker in enumerate(blockers):
        _trimmed(blocker, f"reconcile blockers[{index}]")
    if blockers != sorted(set(blockers)):
        raise ReconcileError(
            "reconcile blockers must be a sorted unique non-empty string array"
        )
    operations = document["operations"]
    if not isinstance(operations, list) or len(operations) != 7:
        raise ReconcileError("reconcile operations must contain exactly seven entries")
    expected_targets = plan["products"]
    for index, raw in enumerate(operations):
        operation = _exact(
            raw,
            f"reconcile operations[{index}]",
            {"action", "executed", "identity", "target"},
        )
        _choice(
            operation["action"],
            "reconcile action",
            ("create-preview", "skip-identical", "conflict"),
        )
        if operation["executed"] is not False:
            raise ReconcileError("reconcile operation executed must remain false")
        target = _exact(
            operation["target"],
            "reconcile operation target",
            {"coordinate", "kind", "name"},
        )
        if target != expected_targets[index]:
            raise ReconcileError(
                "reconcile operations must match canonical planned targets"
            )
        if target["kind"] == "image":
            _digest(operation["identity"], "reconcile operation identity")
        else:
            _hex(operation["identity"], "reconcile operation identity", _HEX_64, 64)
    has_hard_blocker = any(
        item != "external-environment-evidence-required" for item in blockers
    )
    expected_status = "blocked" if has_hard_blocker else "conflict-free-preview"
    if document["status"] != expected_status:
        raise ReconcileError("reconcile status does not match blockers")
    return document


def build_reconcile_plan(
    config: dict[str, Any],
    lifecycle_path: Path,
    manifest_path: Path,
    inventory_path: Path,
    promotion_path: Path | None = None,
    promotion_source_lifecycle_path: Path | None = None,
    promotion_source_manifest_path: Path | None = None,
    environment_lifecycle_path: Path | None = None,
    environment_manifest_path: Path | None = None,
    environment_request_path: Path | None = None,
    environment_result_path: Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic preview; this module exposes no execution function."""
    plan, manifest = load_release_inputs(config, lifecycle_path, manifest_path)
    normalized_inventory, logical_inventory, coordinate_inventory = _inventory(
        inventory_path, manifest["artifacts"]
    )
    if plan["stage"] == "rc" and promotion_path is not None:
        raise ReconcileError("RC reconciliation does not accept promotion evidence")
    promotion = _promotion(promotion_path) if promotion_path is not None else None
    (
        promotion_anchored,
        promotion_source_lifecycle_sha256,
        promotion_source_manifest_sha256,
    ) = _promotion_lineage(
        config,
        promotion,
        promotion_source_lifecycle_path,
        promotion_source_manifest_path,
    )
    promotion_blockers = _promotion_blockers(
        plan, promotion, anchored=promotion_anchored
    )
    blockers = list(promotion_blockers)
    (
        simulated_environment,
        environment_request_sha256,
        environment_result_sha256,
        environment_lifecycle_plan_sha256,
        environment_manifest_sha256,
    ) = _environment_replay(
        config,
        environment_lifecycle_path,
        environment_manifest_path,
        environment_request_path,
        environment_result_path,
        plan,
        manifest,
        promotion,
        stable_promotion_eligible=not promotion_blockers,
    )
    blockers.append("external-environment-evidence-required")
    if simulated_environment != "not-provided":
        blockers.append("draft-simulated-evidence-only")
    if simulated_environment == "not-eligible":
        blockers.append("draft-environment-promotion-ineligible")
    if simulated_environment == "unanchored-simulation":
        blockers.append("draft-environment-unanchored")
    operations, conflicts = _operations(
        manifest["artifacts"], logical_inventory, coordinate_inventory
    )
    blockers.extend(conflicts)
    blockers = sorted(set(blockers))
    hard_blockers = [
        item for item in blockers if item != "external-environment-evidence-required"
    ]
    document = sha256_envelope(
        {
            "artifact_manifest_sha256": manifest["sha256"],
            "blockers": blockers,
            "environment_lifecycle_plan_sha256": environment_lifecycle_plan_sha256,
            "environment_manifest_sha256": environment_manifest_sha256,
            "environment_request_sha256": environment_request_sha256,
            "environment_result_sha256": environment_result_sha256,
            "inventory_sha256": hashlib.sha256(
                canonical_json(normalized_inventory).encode()
            ).hexdigest(),
            "kind": "reconcile-plan",
            "lifecycle_plan_sha256": plan["sha256"],
            "mode": "dry-run",
            "operations": operations,
            "production_ready": False,
            "promotion_evidence_sha256": (
                promotion["sha256"] if promotion is not None else None
            ),
            "promotion_source_lifecycle_plan_sha256": promotion_source_lifecycle_sha256,
            "promotion_source_manifest_sha256": promotion_source_manifest_sha256,
            "schema_version": 2,
            "simulated_environment": simulated_environment,
            "source_sha": plan["source_sha"],
            "stage": plan["stage"],
            "status": "blocked" if hard_blockers else "conflict-free-preview",
            "version": plan["version"],
        }
    )
    return validate_reconcile_plan(document, plan, manifest)


__all__ = [
    "ReconcileError",
    "build_reconcile_plan",
    "load_json",
    "load_release_inputs",
    "validate_reconcile_plan",
]
