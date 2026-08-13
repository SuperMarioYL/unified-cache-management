"""Offline, content-addressed artifact manifest collection and validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .common import SafePathError, safe_posix_path, sha256_envelope
from .lifecycle import LifecycleError, validate_plan, verify_envelope


class ArtifactError(ValueError):
    """Raised when an artifact identity cannot be safely reproduced offline."""


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FILE_KINDS = frozenset({"wheel", "chart"})
_PLATFORMS = ("linux/amd64", "linux/arm64")
_VALIDATION = {
    "file_bytes": "passed",
    "lifecycle_plan": "passed",
    "oci_identity": "passed",
    "product_closure": "passed",
    "registry_readback": "unexecuted",
    "runtime": "unexecuted",
}
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


def _load_json(path: Path, *, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except OSError as error:
        raise ArtifactError(f"cannot read {label}: {path}") from error
    except json.JSONDecodeError as error:
        raise ArtifactError(f"{label} must be valid JSON") from error


def _exact_mapping(value: object, *, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ArtifactError(f"{label} must contain exactly {', '.join(sorted(keys))}")
    return value


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArtifactError(f"{label} must be a non-empty trimmed string")
    return value


def _lifecycle_plan(
    path: Path, config: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    value = _load_json(path, label="lifecycle plan")
    try:
        plan = validate_plan(config, value)
    except LifecycleError as error:
        raise ArtifactError(f"lifecycle plan is invalid: {error}") from error
    return plan, plan["products"]


def _base_dir(value: Path) -> Path:
    try:
        base = value.resolve(strict=True)
    except OSError as error:
        raise ArtifactError(f"cannot resolve base-dir: {value}") from error
    if not base.is_dir():
        raise ArtifactError("base-dir must be a directory")
    return base


def _artifact_file(base: Path, path: object) -> tuple[str, Path]:
    try:
        canonical = safe_posix_path(path, "artifact path")
    except SafePathError as error:
        raise ArtifactError(str(error)) from error
    relative = PurePosixPath(canonical)
    candidate = base.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, ValueError) as error:
        raise ArtifactError(
            "artifact path escapes base-dir through a symlink or invalid target"
        ) from error
    if not resolved.is_file():
        raise ArtifactError("artifact path must identify a regular file")
    return canonical, resolved


def _digest(value: object, *, label: str) -> str:
    result = _string(value, label=label)
    if not _DIGEST.fullmatch(result):
        raise ArtifactError(
            f"{label} must be sha256:<64 lowercase hexadecimal characters>"
        )
    return result


def _platforms(value: object, *, require_sorted: bool) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != len(_PLATFORMS):
        raise ArtifactError(
            "OCI platforms must contain exactly linux/amd64 and linux/arm64"
        )
    records: list[dict[str, str]] = []
    for index, item in enumerate(value):
        platform = _exact_mapping(
            item, label=f"OCI platforms[{index}]", keys={"platform", "digest"}
        )
        name = _string(platform["platform"], label=f"OCI platforms[{index}].platform")
        records.append(
            {
                "platform": name,
                "digest": _digest(
                    platform["digest"], label=f"OCI platforms[{index}].digest"
                ),
            }
        )
    if {item["platform"] for item in records} != set(_PLATFORMS):
        raise ArtifactError(
            "OCI platforms must contain exactly linux/amd64 and linux/arm64"
        )
    ordered = sorted(records, key=lambda item: item["platform"])
    if require_sorted and records != ordered:
        raise ArtifactError("OCI platforms must use stable platform ordering")
    return ordered


def _product(record: dict[str, Any], *, index: int) -> tuple[str, str, str]:
    kind = _string(record["kind"], label=f"record[{index}].kind")
    name = _string(record["name"], label=f"record[{index}].name")
    coordinate = _string(record["coordinate"], label=f"record[{index}].coordinate")
    return kind, name, coordinate


def _records(
    path: Path, base: Path, expected: list[dict[str, str]], version: str
) -> list[dict[str, Any]]:
    value = _load_json(path, label="records-json")
    if not isinstance(value, list):
        raise ArtifactError("records-json must be a JSON array")
    artifacts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ArtifactError(f"record[{index}] must be a JSON object")
        kind = raw.get("kind")
        if kind in _FILE_KINDS:
            record = _exact_mapping(
                raw,
                label=f"record[{index}]",
                keys={"kind", "name", "coordinate", "path"},
            )
            item_kind, name, coordinate = _product(record, index=index)
            relative, resolved = _artifact_file(base, record["path"])
            content = resolved.read_bytes()
            artifacts.append(
                {
                    "kind": item_kind,
                    "name": name,
                    "coordinate": coordinate,
                    "version": version,
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
        elif kind == "image":
            record = _exact_mapping(
                raw,
                label=f"record[{index}]",
                keys={"kind", "name", "coordinate", "digest", "platforms"},
            )
            item_kind, name, coordinate = _product(record, index=index)
            artifacts.append(
                {
                    "kind": item_kind,
                    "name": name,
                    "coordinate": coordinate,
                    "version": version,
                    "digest": _digest(
                        record["digest"], label=f"record[{index}].digest"
                    ),
                    "platforms": _platforms(record["platforms"], require_sorted=False),
                }
            )
        else:
            raise ArtifactError(f"record[{index}].kind must be wheel, image, or chart")
        identity = (item_kind, name, coordinate)
        if identity in seen:
            raise ArtifactError(f"duplicate artifact product coordinate: {coordinate}")
        seen.add(identity)
    actual = sorted(
        (
            {
                "kind": item["kind"],
                "name": item["name"],
                "coordinate": item["coordinate"],
            }
            for item in artifacts
        ),
        key=lambda item: (item["kind"], item["name"]),
    )
    if actual != expected:
        raise ArtifactError(
            "artifact record product closure does not exactly match lifecycle plan"
        )
    return sorted(artifacts, key=lambda item: (item["kind"], item["name"]))


def collect_artifacts(
    config: dict[str, Any], lifecycle_path: Path, records_path: Path, base_dir: Path
) -> dict[str, Any]:
    """Collect exactly the plan products from local bytes and OCI declarations only."""
    if config.get("mode") != "dry-run":
        raise ArtifactError("mode must be dry-run")
    plan, expected = _lifecycle_plan(lifecycle_path, config)
    artifacts = _records(records_path, _base_dir(base_dir), expected, plan["version"])
    return sha256_envelope(
        {
            "kind": "artifact-manifest",
            "schema_version": 2,
            "mode": "dry-run",
            "lifecycle_plan_sha256": plan["sha256"],
            "source_sha": plan["source_sha"],
            "stage": plan["stage"],
            "version": plan["version"],
            "artifacts": artifacts,
            "validation": dict(_VALIDATION),
        }
    )


def _manifest_item(
    item: object, *, index: int, base: Path, version: str
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ArtifactError(f"manifest artifacts[{index}] must be an object")
    kind = item.get("kind")
    if kind in _FILE_KINDS:
        artifact = _exact_mapping(
            item,
            label=f"manifest artifacts[{index}]",
            keys={"kind", "name", "coordinate", "version", "path", "sha256", "size"},
        )
        relative, resolved = _artifact_file(base, artifact["path"])
        checksum = _string(
            artifact["sha256"], label=f"manifest artifacts[{index}].sha256"
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or not isinstance(artifact["size"], int)
            or isinstance(artifact["size"], bool)
            or artifact["size"] < 0
        ):
            raise ArtifactError("file artifact checksum or size is invalid")
        content = resolved.read_bytes()
        if checksum != hashlib.sha256(content).hexdigest() or artifact["size"] != len(
            content
        ):
            raise ArtifactError(
                "file artifact checksum or size does not match local bytes"
            )
        normalized = {
            "kind": _string(artifact["kind"], label="artifact kind"),
            "name": _string(artifact["name"], label="artifact name"),
            "coordinate": _string(artifact["coordinate"], label="artifact coordinate"),
            "version": _string(artifact["version"], label="artifact version"),
            "path": relative,
            "sha256": checksum,
            "size": artifact["size"],
        }
    elif kind == "image":
        artifact = _exact_mapping(
            item,
            label=f"manifest artifacts[{index}]",
            keys={"kind", "name", "coordinate", "version", "digest", "platforms"},
        )
        normalized = {
            "kind": _string(artifact["kind"], label="artifact kind"),
            "name": _string(artifact["name"], label="artifact name"),
            "coordinate": _string(artifact["coordinate"], label="artifact coordinate"),
            "version": _string(artifact["version"], label="artifact version"),
            "digest": _digest(artifact["digest"], label="artifact digest"),
            "platforms": _platforms(artifact["platforms"], require_sorted=True),
        }
    else:
        raise ArtifactError("manifest artifact kind must be wheel, image, or chart")
    if normalized["version"] != version:
        raise ArtifactError("artifact version does not match manifest version")
    return normalized


def validate_artifacts(
    config: dict[str, Any], lifecycle_path: Path, manifest_path: Path, base_dir: Path
) -> dict[str, Any]:
    """Reopen local files and fail closed on any plan or manifest identity drift."""
    if config.get("mode") != "dry-run":
        raise ArtifactError("mode must be dry-run")
    plan, expected = _lifecycle_plan(lifecycle_path, config)
    value = _load_json(manifest_path, label="artifact manifest")
    manifest = _exact_mapping(value, label="artifact manifest", keys=_MANIFEST_KEYS)
    try:
        verify_envelope(manifest, kind="artifact-manifest")
    except LifecycleError as error:
        raise ArtifactError(f"artifact manifest is invalid: {error}") from error
    for key in ("lifecycle_plan_sha256", "source_sha", "stage", "version"):
        plan_key = "sha256" if key == "lifecycle_plan_sha256" else key
        if manifest[key] != plan[plan_key]:
            raise ArtifactError(
                f"artifact manifest {key} does not match lifecycle plan"
            )
    if manifest.get("validation") != _VALIDATION:
        raise ArtifactError("artifact manifest validation evidence is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ArtifactError("artifact manifest artifacts must be an array")
    normalized = [
        _manifest_item(
            item, index=index, base=_base_dir(base_dir), version=plan["version"]
        )
        for index, item in enumerate(artifacts)
    ]
    kind_counts = {
        kind: sum(item["kind"] == kind for item in normalized)
        for kind in ("wheel", "image", "chart")
    }
    if len(normalized) != 7 or kind_counts != {"wheel": 3, "image": 3, "chart": 1}:
        raise ArtifactError(
            "artifact manifest artifact count must be exactly 3 wheels, 3 images, and 1 chart"
        )
    if artifacts != normalized or normalized != sorted(
        normalized, key=lambda item: (item["kind"], item["name"])
    ):
        raise ArtifactError(
            "artifact manifest artifacts must use canonical stable ordering"
        )
    actual = [
        {"kind": item["kind"], "name": item["name"], "coordinate": item["coordinate"]}
        for item in normalized
    ]
    if actual != expected or len(
        {(item["kind"], item["name"], item["coordinate"]) for item in normalized}
    ) != len(normalized):
        raise ArtifactError(
            "artifact manifest product closure does not exactly match lifecycle plan"
        )
    return {
        "kind": "artifact-manifest-validation",
        "schema_version": 2,
        "mode": "dry-run",
        "status": "passed",
        "manifest_sha256": manifest["sha256"],
        "lifecycle_plan_sha256": plan["sha256"],
        "source_sha": plan["source_sha"],
        "stage": plan["stage"],
        "version": plan["version"],
        "validation": dict(_VALIDATION),
    }


__all__ = ["ArtifactError", "collect_artifacts", "validate_artifacts"]
