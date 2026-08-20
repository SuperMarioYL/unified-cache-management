"""Deterministic three-backend wheel planning and environment inspection."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from .common import sha256_envelope
from .lifecycle import LifecycleError, validate_plan

_V2_ROOT = Path(__file__).resolve().parents[1]
_PACKAGING_ROOT = _V2_ROOT / "packaging"
_BACKENDS = ("cuda", "cann-a2", "cann-a3")
_METADATA_KEYS = {"backend", "conflicts", "distribution", "import_name"}
_SHA = re.compile(r"^[0-9a-f]{40}$")


class WheelError(ValueError):
    """Raised for an invalid wheel plan request or unsafe environment."""


def _load_json(path: Path, *, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise WheelError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except OSError as error:
        raise WheelError(f"cannot read {label}: {path}") from error
    except json.JSONDecodeError as error:
        raise WheelError(f"{label} must be valid JSON") from error


def _metadata() -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    for backend in _BACKENDS:
        value = _load_json(
            _PACKAGING_ROOT / backend / "distribution.json",
            label="distribution metadata",
        )
        if not isinstance(value, dict) or set(value) != _METADATA_KEYS:
            raise WheelError(
                "distribution metadata must contain exactly backend, conflicts, distribution, and import_name"
            )
        if value.get("backend") != backend or not isinstance(
            value.get("distribution"), str
        ):
            raise WheelError("distribution metadata backend or distribution is invalid")
        if value.get("import_name") != "ucm" or not isinstance(
            value.get("conflicts"), list
        ):
            raise WheelError(
                "distribution metadata import_name or conflicts is invalid"
            )
        if any(not isinstance(item, str) for item in value["conflicts"]):
            raise WheelError("distribution metadata conflicts must be strings")
        metadata.append(value)
    return metadata


def _lifecycle_plan(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    value = _load_json(path, label="lifecycle plan")
    try:
        plan = validate_plan(config, value)
    except LifecycleError as error:
        raise WheelError(f"lifecycle plan is invalid: {error}") from error
    source_sha = plan.get("source_sha")
    version = plan.get("version")
    if not isinstance(source_sha, str) or not _SHA.fullmatch(source_sha):
        raise WheelError("lifecycle plan source_sha is invalid")
    if not isinstance(version, str) or not version:
        raise WheelError("lifecycle plan version is invalid")
    expected = sorted(
        [
            {
                "kind": "wheel",
                "name": item["distribution"],
                "coordinate": f"{item['distribution']}=={version}",
            }
            for item in config["products"]["wheels"]
        ],
        key=lambda item: item["name"],
    )
    products = plan.get("products")
    actual = (
        sorted(
            (
                item
                for item in products
                if isinstance(item, dict) and item.get("kind") == "wheel"
            ),
            key=lambda item: str(item.get("name", "")),
        )
        if isinstance(products, list)
        else []
    )
    if actual != expected:
        raise WheelError(
            "lifecycle plan wheel product closure does not match configured distributions"
        )
    return plan


def build_wheel_plan(config: dict[str, Any], lifecycle_path: Path) -> dict[str, Any]:
    """Bind exactly three explicit backend coordinates to a trusted lifecycle plan."""
    if config.get("mode") != "dry-run":
        raise WheelError("mode must be dry-run")
    lifecycle = _lifecycle_plan(lifecycle_path, config)
    metadata = _metadata()
    configured = config["products"]["wheels"]
    distributions: list[dict[str, Any]] = []
    for configured_item, item in zip(configured, metadata, strict=True):
        if any(
            item[key] != configured_item[key]
            for key in ("backend", "distribution", "import_name")
        ):
            raise WheelError(
                "distribution metadata does not match configured wheel products"
            )
        distributions.append({**item, "version": lifecycle["version"]})
    plan = {
        "distributions": distributions,
        "kind": "wheel-plan",
        "lifecycle_plan": {
            "sha256": lifecycle["sha256"],
            "source_sha": lifecycle["source_sha"],
            "version": lifecycle["version"],
        },
        "mode": "dry-run",
        "schema_version": 2,
    }
    return sha256_envelope(plan)


def _guard_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ucm_release_v2_backend_guard", _PACKAGING_ROOT / "backend_guard.py"
    )
    if spec is None or spec.loader is None:
        raise WheelError("cannot load reusable backend guard")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installed_fixture(path: Path) -> list[dict[str, str]]:
    value = _load_json(path, label="installed-json")
    if not isinstance(value, list):
        raise WheelError("installed-json must be a JSON array")
    return value


def check_environment(installed_json: Path | None = None) -> dict[str, Any]:
    """Inspect fixture or local metadata without writing or detecting hardware."""
    guard = _guard_module()
    try:
        records = (
            _installed_fixture(installed_json)
            if installed_json is not None
            else guard.installed_distributions()
        )
    except WheelError as error:
        raise WheelError(f"{error}. {guard.recovery_guidance()}") from error
    try:
        report = guard.check_environment(
            records, strict_metadata=installed_json is not None
        )
    except (guard.BackendConflictError, guard.MetadataError) as error:
        label = (
            "installed-json"
            if installed_json is not None
            else "installed distributions"
        )
        raise WheelError(f"{label} is unsafe: {error}") from error
    return {
        "installed": report["installed"],
        "kind": "wheel-environment-check",
        "mode": "dry-run",
        "schema_version": 2,
        "status": report["status"],
    }
