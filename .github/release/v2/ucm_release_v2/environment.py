"""Strict simulated blue/yellow environment request and result envelopes."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import SafePathError, safe_posix_path, sha256_envelope
from .lifecycle import LifecycleError, validate_plan, verify_envelope


class EnvironmentError(ValueError):
    """Raised when simulated environment evidence breaks its self-digested closure."""


CHECK_NAMES = (
    "abi",
    "accelerator",
    "chart-render",
    "cluster",
    "image-pull",
    "import",
    "install",
    "runtime",
    "smoke",
)
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORMS = ("linux/amd64", "linux/arm64")
_MANIFEST_VALIDATION = {
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
_REQUEST_KEYS = {
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
}
_RESULT_KEYS = {
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
_OPERATIONS = [
    {"action": "execute-environment-checks", "executed": False},
    {"action": "collect-production-evidence", "executed": False},
]


def _load_json(path: Path, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise EnvironmentError(f"{label} contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeError) as error:
        raise EnvironmentError(f"cannot read {label}: {path}") from error
    except json.JSONDecodeError as error:
        raise EnvironmentError(f"{label} must be valid JSON") from error


def _exact(value: object, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise EnvironmentError(
            f"{label} must contain exactly {', '.join(sorted(keys))}"
        )
    return value


def _trimmed(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EnvironmentError(f"{label} must be a non-empty trimmed string")
    return value


def _hex(value: object, label: str, pattern: re.Pattern[str], length: int) -> str:
    result = _trimmed(value, label)
    if not pattern.fullmatch(result):
        raise EnvironmentError(
            f"{label} must be exactly {length} lowercase hexadecimal characters"
        )
    return result


def _choice(value: object, label: str, choices: tuple[str, ...]) -> str:
    result = _trimmed(value, label)
    if result not in choices:
        raise EnvironmentError(f"{label} must be one of {', '.join(choices)}")
    return result


def _literal(value: object, label: str, expected: str) -> str:
    result = _trimmed(value, label)
    if result != expected:
        raise EnvironmentError(f"{label} must be {expected}")
    return result


def _digest(value: object, label: str) -> str:
    result = _trimmed(value, label)
    if not _OCI_DIGEST.fullmatch(result):
        raise EnvironmentError(f"{label} must be a lowercase sha256 OCI digest")
    return result


def _path(value: object, label: str) -> str:
    try:
        return safe_posix_path(value, label)
    except SafePathError as error:
        raise EnvironmentError(str(error)) from error


def _expected_products(config: dict[str, Any], version: str) -> list[dict[str, str]]:
    repository = config["repositories"]["production"]
    products: list[dict[str, str]] = []
    for item in config["products"]["wheels"]:
        products.append(
            {
                "kind": "wheel",
                "name": item["distribution"],
                "coordinate": f"{item['distribution']}=={version}",
            }
        )
    for item in config["products"]["images"]:
        products.append(
            {
                "kind": "image",
                "name": item["family"],
                "coordinate": item["repository"].replace("{repository}", repository)
                + f":{version}",
            }
        )
    chart = config["products"]["chart"]
    products.append(
        {
            "kind": "chart",
            "name": chart["name"],
            "coordinate": f"{chart['name']}@{version}",
        }
    )
    return sorted(products, key=lambda item: (item["kind"], item["name"]))


def _artifact(item: object, index: int, version: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise EnvironmentError(f"artifacts[{index}] must be an object")
    kind = _trimmed(item.get("kind"), f"artifacts[{index}].kind")
    if kind in {"wheel", "chart"}:
        value = _exact(
            item,
            f"artifacts[{index}]",
            {"kind", "name", "coordinate", "version", "path", "sha256", "size"},
        )
        checksum = value["sha256"]
        size = value["size"]
        if not isinstance(checksum, str) or not _HEX_64.fullmatch(checksum):
            raise EnvironmentError(f"artifacts[{index}].sha256 is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise EnvironmentError(f"artifacts[{index}].size is invalid")
        normalized = {
            "kind": kind,
            "name": _trimmed(value["name"], f"artifacts[{index}].name"),
            "coordinate": _trimmed(
                value["coordinate"], f"artifacts[{index}].coordinate"
            ),
            "version": _trimmed(value["version"], f"artifacts[{index}].version"),
            "path": _path(value["path"], f"artifacts[{index}].path"),
            "sha256": checksum,
            "size": size,
        }
    elif kind == "image":
        value = _exact(
            item,
            f"artifacts[{index}]",
            {"kind", "name", "coordinate", "version", "digest", "platforms"},
        )
        platforms = value["platforms"]
        if not isinstance(platforms, list) or len(platforms) != 2:
            raise EnvironmentError(
                f"artifacts[{index}].platforms must contain exactly two entries"
            )
        normalized_platforms: list[dict[str, str]] = []
        for platform_index, raw in enumerate(platforms):
            platform = _exact(
                raw,
                f"artifacts[{index}].platforms[{platform_index}]",
                {"platform", "digest"},
            )
            normalized_platforms.append(
                {
                    "platform": _trimmed(platform["platform"], "platform name"),
                    "digest": _digest(platform["digest"], "platform digest"),
                }
            )
        if tuple(item["platform"] for item in normalized_platforms) != _PLATFORMS:
            raise EnvironmentError(
                f"artifacts[{index}].platforms must be canonical linux/amd64, linux/arm64"
            )
        normalized = {
            "kind": "image",
            "name": _trimmed(value["name"], f"artifacts[{index}].name"),
            "coordinate": _trimmed(
                value["coordinate"], f"artifacts[{index}].coordinate"
            ),
            "version": _trimmed(value["version"], f"artifacts[{index}].version"),
            "digest": _digest(value["digest"], f"artifacts[{index}].digest"),
            "platforms": normalized_platforms,
        }
    else:
        raise EnvironmentError(
            f"artifacts[{index}].kind must be wheel, image, or chart"
        )
    if normalized["version"] != version:
        raise EnvironmentError(
            "artifact version does not match environment release version"
        )
    return normalized


def _artifacts(
    config: dict[str, Any], value: object, version: str
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 7:
        raise EnvironmentError(
            "artifacts must contain exactly 3 wheels, 3 images, and 1 chart"
        )
    normalized = [_artifact(item, index, version) for index, item in enumerate(value)]
    if normalized != value or normalized != sorted(
        normalized, key=lambda item: (item["kind"], item["name"])
    ):
        raise EnvironmentError("artifacts must use canonical stable ordering")
    identities = [
        {"kind": item["kind"], "name": item["name"], "coordinate": item["coordinate"]}
        for item in normalized
    ]
    if identities != _expected_products(config, version):
        raise EnvironmentError(
            "artifacts product closure does not match configured production products"
        )
    return normalized


def _request_checks(value: object) -> list[dict[str, Any]]:
    expected = [
        {"evidence_level": "simulated", "name": name, "verified": False}
        for name in CHECK_NAMES
    ]
    if value != expected:
        raise EnvironmentError(
            "required_checks must be the exact canonical simulated check set"
        )
    return expected


def _validate_request(config: dict[str, Any], value: object) -> dict[str, Any]:
    request = _exact(value, "environment request", _REQUEST_KEYS)
    try:
        verify_envelope(request, kind="environment-test-request")
    except LifecycleError as error:
        raise EnvironmentError(f"environment request is invalid: {error}") from error
    _choice(request["environment"], "environment", tuple(config["environments"]))
    _literal(
        request["evidence_level"],
        "environment request evidence_level",
        "simulated",
    )
    _hex(request["nonce"], "environment request nonce", _HEX_32, 32)
    _hex(request["source_sha"], "environment request source_sha", _HEX_40, 40)
    for key in ("lifecycle_plan_sha256", "artifact_manifest_sha256"):
        _hex(request[key], f"environment request {key}", _HEX_64, 64)
    _literal(request["stage"], "environment request stage", "draft")
    version = _trimmed(request["version"], "environment request version")
    _artifacts(config, request["artifacts"], version)
    _request_checks(request["required_checks"])
    if request["operations"] != _OPERATIONS:
        raise EnvironmentError("environment request operations must remain unexecuted")
    return request


def export_request(
    config: dict[str, Any],
    lifecycle_path: Path,
    manifest_path: Path,
    environment: str,
    nonce: str,
) -> dict[str, Any]:
    """Export a self-digested request after reopening plan and manifest structure."""
    environment = _choice(environment, "environment", tuple(config["environments"]))
    nonce = _hex(nonce, "nonce", _HEX_32, 32)
    plan_value = _load_json(lifecycle_path, "lifecycle plan")
    try:
        plan = validate_plan(config, plan_value)
    except LifecycleError as error:
        raise EnvironmentError(f"lifecycle plan is invalid: {error}") from error
    if plan["stage"] != "draft" or plan["repository_role"] != "production":
        raise EnvironmentError(
            "environment export requires a Draft production lifecycle plan"
        )
    manifest = _exact(
        _load_json(manifest_path, "artifact manifest"),
        "artifact manifest",
        _MANIFEST_KEYS,
    )
    try:
        verify_envelope(manifest, kind="artifact-manifest")
    except LifecycleError as error:
        raise EnvironmentError(f"artifact manifest is invalid: {error}") from error
    _hex(manifest["source_sha"], "artifact manifest source_sha", _HEX_40, 40)
    _literal(manifest["stage"], "artifact manifest stage", "draft")
    _trimmed(manifest["version"], "artifact manifest version")
    _hex(
        manifest["lifecycle_plan_sha256"],
        "artifact manifest lifecycle_plan_sha256",
        _HEX_64,
        64,
    )
    for key in ("source_sha", "stage", "version"):
        if manifest[key] != plan[key]:
            raise EnvironmentError(
                f"artifact manifest {key} does not match lifecycle plan"
            )
    if manifest["lifecycle_plan_sha256"] != plan["sha256"]:
        raise EnvironmentError(
            "artifact manifest lifecycle_plan_sha256 does not match lifecycle plan"
        )
    if manifest["validation"] != _MANIFEST_VALIDATION:
        raise EnvironmentError("artifact manifest validation evidence is invalid")
    artifacts = _artifacts(config, manifest["artifacts"], plan["version"])
    request = sha256_envelope(
        {
            "kind": "environment-test-request",
            "schema_version": 2,
            "mode": "dry-run",
            "environment": environment,
            "evidence_level": "simulated",
            "nonce": nonce,
            "source_sha": plan["source_sha"],
            "stage": "draft",
            "version": plan["version"],
            "lifecycle_plan_sha256": plan["sha256"],
            "artifact_manifest_sha256": manifest["sha256"],
            "artifacts": deepcopy(artifacts),
            "required_checks": _request_checks(
                [
                    {"evidence_level": "simulated", "name": name, "verified": False}
                    for name in CHECK_NAMES
                ]
            ),
            "operations": deepcopy(_OPERATIONS),
        }
    )
    return _validate_request(config, request)


def simulate_result(
    config: dict[str, Any], request_path: Path, verdict: str, fail_check: str | None
) -> dict[str, Any]:
    """Create only explicit simulated fixture evidence from a valid request."""
    request = _validate_request(config, _load_json(request_path, "environment request"))
    verdict = _choice(verdict, "simulated verdict", ("passed", "failed"))
    if verdict == "passed" and fail_check is not None:
        raise EnvironmentError("a passed simulated verdict cannot include --fail-check")
    if verdict == "failed":
        if fail_check is None:
            raise EnvironmentError(
                "a failed simulated verdict requires --fail-check from the required check set"
            )
        fail_check = _choice(fail_check, "failed simulated check", CHECK_NAMES)
    checks = [
        {"name": name, "status": "failed" if name == fail_check else "passed"}
        for name in CHECK_NAMES
    ]
    return sha256_envelope(
        {
            "kind": "environment-test-result",
            "schema_version": 2,
            "mode": "dry-run",
            "evidence_level": "simulated",
            "request_sha256": request["sha256"],
            "nonce": request["nonce"],
            "environment": request["environment"],
            "source_sha": request["source_sha"],
            "stage": request["stage"],
            "version": request["version"],
            "lifecycle_plan_sha256": request["lifecycle_plan_sha256"],
            "artifact_manifest_sha256": request["artifact_manifest_sha256"],
            "artifacts": deepcopy(request["artifacts"]),
            "checks": checks,
            "verdict": verdict,
        }
    )


def verify_result(
    config: dict[str, Any], request_path: Path, result_path: Path
) -> dict[str, Any]:
    """Verify replay identity and checks while permanently blocking production promotion."""
    request = _validate_request(config, _load_json(request_path, "environment request"))
    result = _exact(
        _load_json(result_path, "environment result"),
        "environment result",
        _RESULT_KEYS,
    )
    try:
        verify_envelope(result, kind="environment-test-result")
    except LifecycleError as error:
        raise EnvironmentError(f"environment result is invalid: {error}") from error
    _literal(
        result["evidence_level"],
        "environment result evidence_level",
        "simulated",
    )
    _hex(result["request_sha256"], "environment result request_sha256", _HEX_64, 64)
    _hex(result["nonce"], "environment result nonce", _HEX_32, 32)
    _choice(
        result["environment"],
        "environment result environment",
        tuple(config["environments"]),
    )
    _hex(result["source_sha"], "environment result source_sha", _HEX_40, 40)
    _literal(result["stage"], "environment result stage", "draft")
    version = _trimmed(result["version"], "environment result version")
    _hex(
        result["lifecycle_plan_sha256"],
        "environment result lifecycle_plan_sha256",
        _HEX_64,
        64,
    )
    _hex(
        result["artifact_manifest_sha256"],
        "environment result artifact_manifest_sha256",
        _HEX_64,
        64,
    )
    _artifacts(config, result["artifacts"], version)
    closure = {
        "request_sha256": "sha256",
        "nonce": "nonce",
        "environment": "environment",
        "source_sha": "source_sha",
        "stage": "stage",
        "version": "version",
        "lifecycle_plan_sha256": "lifecycle_plan_sha256",
        "artifact_manifest_sha256": "artifact_manifest_sha256",
        "artifacts": "artifacts",
    }
    for result_key, request_key in closure.items():
        if result[result_key] != request[request_key]:
            raise EnvironmentError(
                f"environment result {result_key} does not match request replay identity"
            )
    checks = result["checks"]
    if not isinstance(checks, list) or len(checks) != len(CHECK_NAMES):
        raise EnvironmentError(
            "environment result checks must contain the exact required set"
        )
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(checks):
        check = _exact(raw, f"environment result checks[{index}]", {"name", "status"})
        name = _trimmed(check["name"], f"environment result checks[{index}].name")
        status = _choice(
            check["status"],
            "environment result check status",
            ("passed", "failed"),
        )
        normalized.append({"name": name, "status": status})
    if [item["name"] for item in normalized] != list(CHECK_NAMES):
        raise EnvironmentError(
            "environment result checks must be unique and use the exact canonical set"
        )
    verdict = _choice(
        result["verdict"],
        "environment result verdict",
        ("passed", "failed"),
    )
    failures = [item for item in normalized if item["status"] == "failed"]
    if verdict == "passed" and failures:
        raise EnvironmentError("passed verdict requires every check to pass")
    if verdict == "failed" and not failures:
        raise EnvironmentError("failed verdict requires at least one failed check")
    return {
        "gates": {
            "production": {"status": "blocked"},
            "simulated_environment": {"status": verdict},
        },
        "kind": "environment-verification",
        "mode": "dry-run",
        "production_gate": "blocked",
        "reason": "simulated-evidence-cannot-satisfy-production-gate",
        "schema_version": 2,
        "simulated_verdict": verdict,
        "status": "accepted",
    }


__all__ = [
    "CHECK_NAMES",
    "EnvironmentError",
    "export_request",
    "simulate_result",
    "verify_result",
]
