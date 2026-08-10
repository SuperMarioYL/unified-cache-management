"""Read-only OCI registry discovery and deterministic image reconciliation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import core
from .core import (
    DEFAULT_SCHEMA_DIR,
    canonical_bytes,
    load_json,
    sha256_value,
    validate_schema,
)

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
VERSION = r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?"
OCI_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[._:-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
TARGET_REPOSITORIES = {
    "vllm-openai": "ghcr.io/modelengine-group/vllm-openai",
    "vllm-ascend": "ghcr.io/modelengine-group/vllm-ascend",
}
UPSTREAM_REPOSITORIES = {
    "vllm-openai": "docker.io/vllm/vllm-openai",
    "vllm-ascend": "quay.io/ascend/vllm-ascend",
}
SNAPSHOT_KEYS = {
    "schema_version",
    "kind",
    "repository",
    "upstream_tag",
    "index_digest",
    "platforms",
}
PLATFORM_KEYS = {
    "os",
    "architecture",
    "manifest_digest",
    "config_digest",
}
INVENTORY_KEYS = {
    "schema_version",
    "kind",
    "repositories",
    "entries",
    "inventory_sha256",
}
ENTRY_KEYS = {
    "repository",
    "tag",
    "build_key_sha256",
    "observed_digest",
    "evidence_digest",
}
CANDIDATE_KEYS = {
    "schema_version",
    "kind",
    "fixture_only",
    "unpublished",
    "ucm_version",
    "target_repository",
    "tag_base",
    "tag_family_sha256",
    "build_key_sha256",
    "build_inputs",
}
BUILD_INPUT_KEYS = {
    "release_manifest_sha256",
    "wheel",
    "upstream",
    "compatibility_rule_id",
    "compatibility_rule",
    "compatibility_rule_sha256",
    "implementation_digest",
}
WHEEL_INPUT_KEYS = {
    "spec_id",
    "sha256",
    "declaration_sha256",
    "version",
    "accelerator",
    "accelerator_runtime",
    "npu_arch_or_na",
    "os",
    "cpu_arch",
    "python_abi",
    "binary_profile_id",
}
UPSTREAM_INPUT_KEYS = {
    "repository",
    "exact_upstream_tag",
    "index_digest",
    "platforms",
}
COMMON_WHEEL_RECORD_KEYS = {
    "schema_version",
    "kind",
    "source_kind",
    "spec_id",
    "filename",
    "sha256",
    "size",
    "distribution",
    "version",
    "tags",
    "requires_dist",
    "python_abi",
    "cpu_arch",
    "declaration_sha256",
    "status",
    "trust_level",
    "published",
    "publication_eligible",
}
WHEEL_RECORD_KEYS_BY_SOURCE = {
    "fixture": COMMON_WHEEL_RECORD_KEYS | {"fixture_binding"},
    "builder-candidate": COMMON_WHEEL_RECORD_KEYS | {"builder_evidence"},
}
COMPATIBILITY_RULE_KEYS = {
    "id",
    "accelerator",
    "accelerator_runtimes",
    "npu_architectures",
    "operating_systems",
    "cpu_architectures",
    "python_abis",
    "upstream_channels",
}
OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
STAGING_REPOSITORY = "ghcr.io/supermarioyl/ucm-release-staging"
CANONICAL_MEMBER_SPEC_IDS = [
    "cuda130-amd64",
    "cuda130-arm64",
    "cann900-a2-amd64",
    "cann900-a2-arm64",
    "cann900-a3-amd64",
    "cann900-a3-arm64",
]
MEMBER_RECORD_KEYS = {
    "schema_version",
    "kind",
    "status",
    "spec_id",
    "profile_id",
    "family_id",
    "platform",
    "target_repository",
    "target_tag",
    "staging_repository",
    "staging_visibility",
    "staging_tag",
    "candidate_task_sha256",
    "publication_task_sha256",
    "build_key_sha256",
    "wheel_sha256",
    "member_digest",
    "member_size",
    "config_digest",
    "annotations",
    "record_sha256",
}
MEMBER_ANNOTATION_KEYS = {
    "io.ucm.release.build-key-sha256",
    "io.ucm.release.candidate-task-sha256",
    "io.ucm.release.family-id",
    "io.ucm.release.platform",
    "io.ucm.release.spec-id",
    "io.ucm.release.wheel-sha256",
}


class RegistryBlocker(ValueError):
    """A known fail-closed loop blocker with a stable evidence code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{label} fields mismatch: missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be an immutable sha256:<64 lowercase hex> digest"
        )
    return value


def _repository(value: object) -> str:
    if not isinstance(value, str) or REPOSITORY_RE.fullmatch(value) is None:
        raise ValueError(
            "repository must be a canonical lowercase OCI repository without tag or digest"
        )
    return value


def _product_for_repository(repository: str) -> str:
    product = repository.rsplit("/", 1)[-1]
    if UPSTREAM_REPOSITORIES.get(product) != repository:
        raise ValueError(f"unsupported exact upstream repository: {repository}")
    return product


def parse_upstream_tag(product: str, tag: str) -> dict[str, str]:
    """Parse only canonical stable/RC tags and the supported Ascend suffixes."""
    if product not in TARGET_REPOSITORIES or not isinstance(tag, str):
        raise ValueError("product must be vllm-openai or vllm-ascend")
    if product == "vllm-openai":
        match = re.fullmatch(f"({VERSION})", tag)
        if match is None:
            raise ValueError(f"unsupported vllm-openai upstream tag: {tag}")
        npu_arch = "na"
        operating_system = "ubuntu-22.04"
    else:
        match = re.fullmatch(f"({VERSION})(-a3)?(-openeuler)?", tag)
        if match is None:
            raise ValueError(f"unsupported vllm-ascend upstream tag: {tag}")
        npu_arch = "a3" if match.group(2) else "a2"
        operating_system = "openEuler-24.03" if match.group(3) else "ubuntu-22.04"
    version = match.group(1)
    return {
        "product": product,
        "exact_upstream_tag": tag,
        "upstream_version": version,
        "channel": "rc" if "rc" in version else "stable",
        "npu_arch": npu_arch,
        "operating_system": operating_system,
        "target_repository": TARGET_REPOSITORIES[product],
    }


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Require one immutable index with exact linux/amd64 and linux/arm64 chains."""
    if not isinstance(snapshot, dict):
        raise ValueError("registry snapshot must be an object")
    _exact_keys(snapshot, SNAPSHOT_KEYS, "registry snapshot")
    if (
        snapshot["schema_version"] != 1
        or snapshot["kind"] != "upstream-registry-snapshot"
    ):
        raise ValueError("registry snapshot identity must be schema version 1")
    repository = _repository(snapshot["repository"])
    parse_upstream_tag(_product_for_repository(repository), snapshot["upstream_tag"])
    index_digest = _digest(snapshot["index_digest"], "snapshot index")
    platforms = snapshot["platforms"]
    if not isinstance(platforms, list):
        raise ValueError("snapshot platforms must be an array")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, platform in enumerate(platforms):
        if not isinstance(platform, dict):
            raise ValueError(f"snapshot platform {index} must be an object")
        _exact_keys(platform, PLATFORM_KEYS, f"snapshot platform {index}")
        if platform["os"] not in {"linux"} or platform["architecture"] not in {
            "amd64",
            "arm64",
        }:
            raise ValueError("snapshot platform must be linux/amd64 or linux/arm64")
        identity = (platform["os"], platform["architecture"])
        if identity in seen:
            raise ValueError(
                f"duplicate snapshot platform: {identity[0]}/{identity[1]}"
            )
        seen.add(identity)
        normalized.append(
            {
                "os": platform["os"],
                "architecture": platform["architecture"],
                "manifest_digest": _digest(
                    platform["manifest_digest"], f"{identity} manifest"
                ),
                "config_digest": _digest(
                    platform["config_digest"], f"{identity} config"
                ),
            }
        )
    required = {("linux", "amd64"), ("linux", "arm64")}
    if seen != required:
        missing = sorted(required - seen)
        extra = sorted(seen - required)
        if missing == [("linux", "arm64")] and not extra:
            raise RegistryBlocker(
                "missing-linux-arm64",
                "snapshot is missing required linux/arm64 platform",
            )
        raise ValueError(
            f"snapshot requires exact linux platforms; missing={missing}, extra={extra}"
        )
    all_digests = [
        index_digest,
        *[
            item[key]
            for item in normalized
            for key in ("manifest_digest", "config_digest")
        ],
    ]
    if len(all_digests) != len(set(all_digests)):
        raise ValueError("snapshot digest chain contains duplicate mutable identities")
    normalized.sort(key=lambda item: item["architecture"])
    result = copy.deepcopy(snapshot)
    result["platforms"] = normalized
    return result


def _unique_json(text: str, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _crane_binary(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        if not path.is_file():
            raise ValueError(f"crane executable does not exist: {value}")
        return value
    if "/" in value or re.fullmatch(r"[A-Za-z0-9_.+-]+", value) is None:
        raise ValueError(
            "crane executable must be an absolute path or an explicit PATH name"
        )
    return value


def _crane(crane_binary: str, operation: str, reference: str) -> str:
    if operation not in {"digest", "manifest"}:
        raise ValueError(
            "only read-only crane digest and manifest operations are allowed"
        )
    try:
        result = subprocess.run(
            [_crane_binary(crane_binary), operation, reference],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise ValueError(f"failed to execute pinned crane binary: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ValueError(f"crane {operation} failed for {reference}: {detail}")
    return result.stdout.strip()


def scan_registry(
    repository: str,
    upstream_tag: str,
    *,
    fixture: dict[str, Any] | None = None,
    crane_binary: str = "crane",
) -> dict[str, Any]:
    """Read and validate an upstream multi-platform snapshot without registry writes."""
    repository = _repository(repository)
    parse_upstream_tag(_product_for_repository(repository), upstream_tag)
    tagged_reference = f"{repository}:{upstream_tag}"
    if fixture is not None:
        snapshot = validate_snapshot(fixture)
        if (
            snapshot["repository"] != repository
            or snapshot["upstream_tag"] != upstream_tag
        ):
            raise ValueError(
                "fixture snapshot repository/tag does not match the exact request"
            )
        return {
            "schema_version": 1,
            "kind": "registry-scan-result",
            "fixture_only": True,
            "snapshot": snapshot,
            "operations": [
                {
                    "type": "fixture-read",
                    "capability": "read",
                    "reference": tagged_reference,
                }
            ],
        }
    operations = [
        {
            "type": "crane-digest",
            "capability": "read",
            "reference": tagged_reference,
        }
    ]
    index_digest = _digest(
        _crane(crane_binary, "digest", tagged_reference), "crane index"
    )
    index_reference = f"{repository}@{index_digest}"
    operations.append(
        {
            "type": "crane-manifest",
            "capability": "read",
            "reference": index_reference,
        }
    )
    index = _unique_json(
        _crane(crane_binary, "manifest", index_reference), "crane index"
    )
    if index.get("mediaType") not in OCI_INDEX_MEDIA_TYPES:
        raise ValueError("resolved index digest did not return an OCI/Docker index")
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list):
        raise ValueError("crane index must contain a manifests array")
    platforms: list[dict[str, str]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not isinstance(
            descriptor.get("platform"), dict
        ):
            raise ValueError("crane index descriptors require a platform object")
        platform = descriptor["platform"]
        if descriptor.get("mediaType") not in OCI_MANIFEST_MEDIA_TYPES:
            raise ValueError("index platform descriptor is not an OCI/Docker manifest")
        manifest_digest = _digest(descriptor.get("digest"), "platform manifest")
        child_reference = f"{repository}@{manifest_digest}"
        operations.append(
            {
                "type": "crane-manifest",
                "capability": "read",
                "reference": child_reference,
            }
        )
        child = _unique_json(
            _crane(crane_binary, "manifest", child_reference),
            f"platform manifest {manifest_digest}",
        )
        if child.get("mediaType") != descriptor.get("mediaType"):
            raise ValueError(
                "platform manifest media type does not match index descriptor"
            )
        config = child.get("config")
        if not isinstance(config, dict):
            raise ValueError("platform manifest requires a config descriptor")
        platforms.append(
            {
                "os": platform.get("os"),
                "architecture": platform.get("architecture"),
                "manifest_digest": manifest_digest,
                "config_digest": _digest(config.get("digest"), "platform config"),
            }
        )
    snapshot = validate_snapshot(
        {
            "schema_version": 1,
            "kind": "upstream-registry-snapshot",
            "repository": repository,
            "upstream_tag": upstream_tag,
            "index_digest": index_digest,
            "platforms": platforms,
        }
    )
    return {
        "schema_version": 1,
        "kind": "registry-scan-result",
        "fixture_only": False,
        "snapshot": snapshot,
        "operations": operations,
    }


def validate_public_tag(tag: object) -> str:
    if not isinstance(tag, str) or OCI_TAG_RE.fullmatch(tag) is None:
        raise ValueError(
            "public tag must use strict OCI tag syntax and be at most 128 bytes"
        )
    match = re.fullmatch(r"(.+)-r([1-9][0-9]*)", tag)
    if match is None:
        raise ValueError("public tag must end in canonical -rN with N >= 1")
    return tag


def _validate_release_manifest(release_manifest: dict[str, Any]) -> None:
    try:
        validate_schema(
            release_manifest,
            load_json(DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"release manifest failed Task 2 schema validation: {error}"
        ) from error
    specs = release_manifest["wheel_specs"]
    eligible = [spec for spec in specs if spec["build_eligible"]]
    blockers = sorted({reason for spec in specs for reason in spec["blocked_reasons"]})
    if (
        release_manifest["declared_wheel_count"] != len(specs)
        or release_manifest["eligible_wheel_count"] != len(eligible)
        or release_manifest["blockers"] != blockers
        or release_manifest["status"]
        != ("candidate" if len(eligible) == len(specs) else "blocked")
    ):
        raise ValueError(
            "release manifest operational counts/blockers/status are inconsistent"
        )
    expected_wheel_assets = {
        spec["spec_id"]: "candidate" if spec["build_eligible"] else "blocked"
        for spec in specs
    }
    wheel_assets = [
        asset
        for asset in release_manifest["publication"]["assets"]
        if asset["type"] == "wheel"
    ]
    actual_wheel_assets: dict[str, str] = {}
    for asset in wheel_assets:
        spec_id = asset["id"].removeprefix("wheel:")
        if spec_id in actual_wheel_assets or asset["required"] is not True:
            raise ValueError(
                "release manifest contains duplicate/non-required wheel asset"
            )
        actual_wheel_assets[spec_id] = asset["status"]
    if actual_wheel_assets != expected_wheel_assets:
        raise ValueError("release manifest wheel assets do not match operational specs")


def _select_wheel(
    release_manifest: dict[str, Any],
    wheel_records: list[dict[str, Any]],
    spec_id: str,
    fixture_mode: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not fixture_mode:
        raise RegistryBlocker(
            "production-wheel-unpublished",
            "production wheel is unpublished; Task 2 emits no production publication",
        )
    _validate_release_manifest(release_manifest)
    specs = [
        item
        for item in release_manifest.get("wheel_specs", [])
        if item.get("spec_id") == spec_id
    ]
    assets = [
        item
        for item in release_manifest.get("publication", {}).get("assets", [])
        if item.get("id") == f"wheel:{spec_id}"
    ]
    records = [item for item in wheel_records if item.get("spec_id") == spec_id]
    if len(specs) != 1 or len(assets) != 1 or len(records) != 1:
        raise ValueError(
            "wheel selection must be exact and unique in manifest, assets, and records"
        )
    spec, asset, record = specs[0], assets[0], records[0]
    expected_status = "candidate" if spec["build_eligible"] else "blocked"
    if asset != {
        "id": f"wheel:{spec_id}",
        "type": "wheel",
        "required": True,
        "status": expected_status,
    }:
        raise ValueError(
            "selected manifest wheel asset does not match its Task 2 spec status"
        )
    expected_record_keys = (
        WHEEL_RECORD_KEYS_BY_SOURCE.get(record.get("source_kind"))
        if isinstance(record, dict)
        else None
    )
    if expected_record_keys is None or set(record) != expected_record_keys:
        raise ValueError(
            "wheel inspection record is not a complete Task 2 source result"
        )
    if record["schema_version"] != 1 or record["kind"] != "ucm-wheel-inspection":
        raise ValueError("wheel inspection identity is invalid")
    _digest(record.get("sha256"), "selected wheel")
    _digest(record.get("declaration_sha256"), "selected wheel declaration")
    if record["declaration_sha256"] != spec.get("declaration_sha256"):
        raise ValueError("selected wheel declaration does not match its manifest spec")
    if (
        record["version"] != release_manifest["ucm_version"]
        or record["python_abi"] != spec["python_abi"]
        or record["cpu_arch"] != spec["cpu_arch"]
        or record["distribution"] != "uc-manager"
        or not isinstance(record["size"], int)
        or isinstance(record["size"], bool)
        or record["size"] < 1
        or not isinstance(record["tags"], list)
        or not record["tags"]
        or record["requires_dist"] != ["wrapt==1.17.2"]
    ):
        raise ValueError(
            "wheel inspection metadata does not match the selected Task 2 spec"
        )
    fixture_semantics = (
        record.get("source_kind") == "fixture"
        and record.get("status") == "fixture-only"
        and record.get("trust_level") == "fixture-only"
        and record.get("published") is False
        and record.get("publication_eligible") is False
        and isinstance(record.get("fixture_binding"), dict)
        and record["fixture_binding"].get("profile_id") == spec_id
        and record["fixture_binding"].get("marker_status") == "passed"
        and re.fullmatch(
            r"[0-9a-f]{40}", str(record["fixture_binding"].get("source_commit"))
        )
        is not None
    )
    if fixture_mode and not fixture_semantics:
        raise ValueError(
            "fixture wheel record does not have fixture-only unpublished semantics"
        )
    return spec, record


def _resolve_compatibility_rule(
    compatibility: dict[str, Any],
    compatibility_rule_id: str,
    release_manifest: dict[str, Any],
    spec: dict[str, Any],
    parsed_tag: dict[str, str],
) -> dict[str, Any]:
    try:
        validate_schema(
            compatibility,
            load_json(DEFAULT_SCHEMA_DIR / "config.schema.json"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"compatibility config failed Task 2 schema validation: {error}"
        ) from error
    if (
        compatibility.get("kind") != "compatibility-config"
        or compatibility.get("schema_version") != 1
        or compatibility.get("ucm_version") != release_manifest["ucm_version"]
    ):
        raise ValueError(
            "compatibility config identity/version does not match release manifest"
        )
    compatibility_sha256 = sha256_value(compatibility)
    if compatibility_sha256 != release_manifest["compatibility_sha256"]:
        raise ValueError("compatibility config digest does not match release manifest")
    rules = compatibility.get("rules")
    if not isinstance(rules, list):
        raise ValueError("compatibility rules must be an array")
    matches = [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("id") == compatibility_rule_id
    ]
    if len(matches) != 1:
        raise ValueError("compatibility rule id must resolve exactly once")
    rule = matches[0]
    expected_accelerator = (
        "ascend" if parsed_tag["product"] == "vllm-ascend" else "cuda"
    )
    checks = {
        "accelerator": rule.get("accelerator")
        == expected_accelerator
        == spec["accelerator"],
        "accelerator runtime": spec["accelerator_runtime"]
        in rule.get("accelerator_runtimes", []),
        "NPU architecture": spec["npu_arch_or_na"] == parsed_tag["npu_arch"]
        and spec["npu_arch_or_na"] in rule.get("npu_architectures", []),
        "operating system": spec["os"] == parsed_tag["operating_system"]
        and spec["os"] in rule.get("operating_systems", []),
        "CPU architecture": spec["cpu_arch"] in rule.get("cpu_architectures", []),
        "Python ABI": spec["python_abi"] in rule.get("python_abis", []),
        "upstream channel": parsed_tag["channel"] in rule.get("upstream_channels", []),
    }
    failed = [label for label, passed in checks.items() if not passed]
    if failed:
        raise ValueError(
            f"compatibility rule does not match selected upstream/wheel semantics: {failed}"
        )
    return copy.deepcopy(rule)


def build_candidate(
    release_manifest: dict[str, Any],
    wheel_records: list[dict[str, Any]],
    spec_id: str,
    upstream_snapshot: dict[str, Any],
    compatibility: dict[str, Any],
    compatibility_rule_id: str,
    implementation_digest: str,
    *,
    fixture_mode: bool,
) -> dict[str, Any]:
    """Bind all immutable inputs into one build key and one target tag family."""
    if not isinstance(release_manifest, dict) or not isinstance(wheel_records, list):
        raise ValueError("release manifest and wheel records have invalid types")
    spec, wheel = _select_wheel(release_manifest, wheel_records, spec_id, fixture_mode)
    snapshot = validate_snapshot(upstream_snapshot)
    if not isinstance(compatibility_rule_id, str) or not compatibility_rule_id:
        raise ValueError("compatibility rule id must be non-empty")
    implementation_digest = _digest(implementation_digest, "implementation")
    parsed = parse_upstream_tag(
        _product_for_repository(snapshot["repository"]), snapshot["upstream_tag"]
    )
    rule = _resolve_compatibility_rule(
        compatibility,
        compatibility_rule_id,
        release_manifest,
        spec,
        parsed,
    )
    ucm_version = release_manifest.get("ucm_version")
    if (
        not isinstance(ucm_version, str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?",
            ucm_version,
        )
        is None
    ):
        raise ValueError("release manifest UCM version is noncanonical")
    tag_base = f"{snapshot['upstream_tag']}-ucm-{ucm_version}"
    validate_public_tag(f"{tag_base}-r1")
    upstream_identity = {
        "repository": snapshot["repository"],
        "exact_upstream_tag": snapshot["upstream_tag"],
        "index_digest": snapshot["index_digest"],
        "platforms": snapshot["platforms"],
    }
    build_inputs = {
        "release_manifest_sha256": sha256_value(release_manifest),
        "wheel": {
            "spec_id": spec_id,
            "sha256": wheel["sha256"],
            "declaration_sha256": wheel["declaration_sha256"],
            "version": wheel["version"],
            "accelerator": spec["accelerator"],
            "accelerator_runtime": spec["accelerator_runtime"],
            "npu_arch_or_na": spec["npu_arch_or_na"],
            "os": spec["os"],
            "cpu_arch": wheel["cpu_arch"],
            "python_abi": wheel["python_abi"],
            "binary_profile_id": spec["binary_profile_id"],
        },
        "upstream": upstream_identity,
        "compatibility_rule_id": compatibility_rule_id,
        "compatibility_rule": rule,
        "compatibility_rule_sha256": sha256_value(rule),
        "implementation_digest": implementation_digest,
    }
    family = {"repository": parsed["target_repository"], "tag_base": tag_base}
    return {
        "schema_version": 1,
        "kind": "ucm-image-build-candidate",
        "fixture_only": fixture_mode,
        "unpublished": True,
        "ucm_version": ucm_version,
        "target_repository": parsed["target_repository"],
        "tag_base": tag_base,
        "tag_family_sha256": sha256_value(family),
        "build_key_sha256": sha256_value(build_inputs),
        "build_inputs": build_inputs,
    }


def _validate_candidate(candidate: dict[str, Any]) -> None:
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    _exact_keys(candidate, CANDIDATE_KEYS, "candidate")
    if (
        candidate["schema_version"] != 1
        or candidate["kind"] != "ucm-image-build-candidate"
    ):
        raise ValueError("candidate identity is invalid")
    if candidate["target_repository"] not in set(TARGET_REPOSITORIES.values()):
        raise ValueError("candidate target repository is not allowed")
    if candidate["fixture_only"] is not True or candidate["unpublished"] is not True:
        raise ValueError(
            "Task 3 accepts only fixture-only, explicitly unpublished candidates"
        )
    if (
        not isinstance(candidate["ucm_version"], str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?",
            candidate["ucm_version"],
        )
        is None
    ):
        raise ValueError("candidate UCM version is noncanonical")
    validate_public_tag(f"{candidate['tag_base']}-r1")
    inputs = candidate["build_inputs"]
    if not isinstance(inputs, dict):
        raise ValueError("candidate build inputs must be an object")
    _exact_keys(inputs, BUILD_INPUT_KEYS, "candidate build inputs")
    wheel = inputs["wheel"]
    if not isinstance(wheel, dict):
        raise ValueError("candidate wheel input must be an object")
    _exact_keys(wheel, WHEEL_INPUT_KEYS, "candidate wheel input")
    for label, digest in (
        ("release manifest", inputs["release_manifest_sha256"]),
        ("wheel", wheel["sha256"]),
        ("wheel declaration", wheel["declaration_sha256"]),
        ("implementation", inputs["implementation_digest"]),
        ("compatibility rule", inputs["compatibility_rule_sha256"]),
    ):
        _digest(digest, label)
    rule = inputs["compatibility_rule"]
    if not isinstance(rule, dict):
        raise ValueError("candidate compatibility rule must be an object")
    _exact_keys(rule, COMPATIBILITY_RULE_KEYS, "candidate compatibility rule")
    if (
        rule["id"] != inputs["compatibility_rule_id"]
        or sha256_value(rule) != inputs["compatibility_rule_sha256"]
    ):
        raise ValueError("candidate compatibility rule digest/id does not match")
    upstream = inputs["upstream"]
    if not isinstance(upstream, dict):
        raise ValueError("candidate upstream input must be an object")
    _exact_keys(upstream, UPSTREAM_INPUT_KEYS, "candidate upstream input")
    synthetic_snapshot = {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": upstream["repository"],
        "upstream_tag": upstream["exact_upstream_tag"],
        "index_digest": upstream["index_digest"],
        "platforms": upstream["platforms"],
    }
    snapshot = validate_snapshot(synthetic_snapshot)
    parsed = parse_upstream_tag(
        _product_for_repository(snapshot["repository"]), snapshot["upstream_tag"]
    )
    expected_accelerator = "ascend" if parsed["product"] == "vllm-ascend" else "cuda"
    semantic_checks = (
        wheel["version"] == candidate["ucm_version"],
        wheel["accelerator"] == expected_accelerator == rule["accelerator"],
        wheel["accelerator_runtime"] in rule["accelerator_runtimes"],
        wheel["npu_arch_or_na"] == parsed["npu_arch"]
        and wheel["npu_arch_or_na"] in rule["npu_architectures"],
        wheel["os"] == parsed["operating_system"]
        and wheel["os"] in rule["operating_systems"],
        wheel["cpu_arch"] in rule["cpu_architectures"],
        wheel["python_abi"] in rule["python_abis"],
        parsed["channel"] in rule["upstream_channels"],
        isinstance(wheel["binary_profile_id"], str)
        and bool(wheel["binary_profile_id"]),
    )
    if not all(semantic_checks):
        raise ValueError(
            "candidate compatibility rule/wheel/upstream semantics do not match"
        )
    expected_base = f"{snapshot['upstream_tag']}-ucm-{candidate['ucm_version']}"
    if (
        candidate["target_repository"] != parsed["target_repository"]
        or candidate["tag_base"] != expected_base
    ):
        raise ValueError(
            "candidate target repository or tag base does not match upstream identity"
        )
    expected_family = sha256_value(
        {
            "repository": candidate["target_repository"],
            "tag_base": candidate["tag_base"],
        }
    )
    if candidate["tag_family_sha256"] != expected_family:
        raise ValueError("candidate tag family digest does not match its identity")
    if candidate["build_key_sha256"] != sha256_value(inputs):
        raise ValueError("candidate build key does not match its immutable inputs")


def with_revision(candidate: dict[str, Any], revision: int) -> dict[str, Any]:
    _validate_candidate(candidate)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("revision must be an integer >= 1")
    result = copy.deepcopy(candidate)
    result["revision"] = revision
    result["public_tag"] = validate_public_tag(f"{candidate['tag_base']}-r{revision}")
    return result


def inventory_digest(inventory: dict[str, Any]) -> str:
    """Hash the canonical Registry read set, excluding its asserted digest field."""
    if not isinstance(inventory, dict):
        raise ValueError("registry inventory must be an object")
    base_keys = {"schema_version", "kind", "repositories", "entries"}
    if frozenset(inventory) not in {frozenset(base_keys), frozenset(INVENTORY_KEYS)}:
        raise ValueError("registry inventory fields are not canonical")
    if not isinstance(inventory["entries"], list):
        raise ValueError("registry inventory entries must be an array")
    canonical_inventory = {
        "schema_version": inventory["schema_version"],
        "kind": inventory["kind"],
        "repositories": inventory["repositories"],
        "entries": sorted(copy.deepcopy(inventory["entries"]), key=canonical_bytes),
    }
    return sha256_value(canonical_inventory)


def _validate_inventory(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(inventory, dict):
        raise ValueError("registry inventory must be an object")
    _exact_keys(inventory, INVENTORY_KEYS, "registry inventory")
    if inventory["schema_version"] != 1 or inventory["kind"] != "registry-inventory":
        raise ValueError("registry inventory identity is invalid")
    if inventory["repositories"] != sorted(TARGET_REPOSITORIES.values()):
        raise ValueError(
            "registry inventory must cover exactly the two target repositories"
        )
    actual_inventory_sha256 = inventory_digest(inventory)
    if inventory["inventory_sha256"] != actual_inventory_sha256:
        raise ValueError(
            "inventory digest mismatch: "
            f"asserted {inventory['inventory_sha256']}, actual {actual_inventory_sha256}"
        )
    entries = inventory["entries"]
    if not isinstance(entries, list):
        raise ValueError("registry inventory entries must be an array")
    by_tag: dict[tuple[str, str], dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"registry inventory entry {index} must be an object")
        _exact_keys(entry, ENTRY_KEYS, f"registry inventory entry {index}")
        if entry["repository"] not in set(TARGET_REPOSITORIES.values()):
            raise ValueError("registry inventory target repository is not allowed")
        validate_public_tag(entry["tag"])
        for field in ("build_key_sha256", "observed_digest", "evidence_digest"):
            _digest(entry[field], f"registry inventory {field}")
        identity = (entry["repository"], entry["tag"])
        if identity in by_tag:
            label = "conflicting" if by_tag[identity] != entry else "duplicate"
            raise RegistryBlocker(
                "duplicate-conflicting-inventory",
                f"{label} registry inventory entries for {identity[0]}:{identity[1]}",
            )
        by_tag[identity] = entry
    return entries


def reconcile(candidate: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Return a build task or a no-op from a passed, immutable Registry view."""
    _validate_candidate(candidate)
    entries = _validate_inventory(inventory)
    inventory_sha256 = inventory["inventory_sha256"]
    family_prefix = candidate["tag_base"] + "-r"
    family_entries = [
        entry
        for entry in entries
        if entry["repository"] == candidate["target_repository"]
        and entry["tag"].startswith(family_prefix)
    ]
    matching = [
        entry
        for entry in family_entries
        if entry["build_key_sha256"] == candidate["build_key_sha256"]
    ]
    stable = [
        entry
        for entry in matching
        if entry["observed_digest"] == entry["evidence_digest"]
    ]
    if len(stable) > 1:
        raise ValueError("conflicting stable inventory entries share one build key")
    common = {
        "schema_version": 1,
        "kind": "registry-reconcile-result",
        "candidate_build_key_sha256": candidate["build_key_sha256"],
        "inventory": copy.deepcopy(inventory),
        "inventory_sha256": inventory_sha256,
        "publication_attempted": False,
        "operations": [
            {
                "type": "registry-inventory-read",
                "capability": "read",
                "reference": inventory_sha256,
            }
        ],
    }
    if stable:
        return {**common, "decision": "already-present", "task_count": 0, "tasks": []}
    used_revisions = {
        int(entry["tag"].removeprefix(family_prefix)) for entry in family_entries
    }
    revision = 1
    while revision in used_revisions:
        revision += 1
    reason = "tag-digest-drift" if matching else "new-build-key"
    revisioned = with_revision(candidate, revision)
    task = {
        "action": "build-unpublished-candidate",
        "repository": candidate["target_repository"],
        "tag": revisioned["public_tag"],
        "revision": revision,
        "reason": reason,
        "build_key_sha256": candidate["build_key_sha256"],
        "tag_family_sha256": candidate["tag_family_sha256"],
        "concurrency_key": candidate["tag_family_sha256"],
        "precondition": {
            "type": "tag-absent",
            "repository": candidate["target_repository"],
            "tag": revisioned["public_tag"],
            "inventory_sha256": inventory_sha256,
        },
        "publication_attempted": False,
    }
    operations = [
        *common["operations"],
        {
            "type": "build-plan",
            "capability": "plan",
            "reference": f"{candidate['target_repository']}:{revisioned['public_tag']}",
        },
    ]
    return {
        **common,
        "operations": operations,
        "decision": "schedule",
        "task_count": 1,
        "tasks": [task],
    }


def canonical_registry_contract() -> dict[str, Any]:
    """Re-derive the exact six members and three public r1 coordinates."""
    candidate = core.build_matrix("feature-candidate")
    publication = core.build_matrix("protected-tag")
    candidate_tasks = candidate.get("tasks")
    publication_tasks = publication.get("tasks")
    if not isinstance(candidate_tasks, list) or not isinstance(publication_tasks, list):
        raise ValueError("release matrices must contain canonical task arrays")
    if [item.get("spec_id") for item in candidate_tasks] != CANONICAL_MEMBER_SPEC_IDS:
        raise ValueError("feature matrix is not the exact six-member contract")
    if [item.get("spec_id") for item in publication_tasks] != CANONICAL_MEMBER_SPEC_IDS:
        raise ValueError("protected matrix is not the exact six-member contract")
    publication_by_spec = {item["spec_id"]: item for item in publication_tasks}
    stable_fields = (
        "spec_id",
        "profile_id",
        "platform",
        "cpu_arch",
        "target_repository",
        "target_tag",
    )
    members: list[dict[str, Any]] = []
    for task in candidate_tasks:
        protected = publication_by_spec[task["spec_id"]]
        if any(task[field] != protected[field] for field in stable_fields):
            raise ValueError("feature/protected matrix stable coordinates diverged")
        if task.get("write_authority") != [] or protected.get("write_authority") != [
            "github-prerelease",
            "ghcr-final-index",
            "ghcr-private-staging",
        ]:
            raise ValueError("release matrix write authority is noncanonical")
        if task["task_sha256"] == protected["task_sha256"]:
            raise ValueError("candidate and publication task identities must differ")
        members.append(
            {
                "spec_id": task["spec_id"],
                "profile_id": task["profile_id"],
                "family_id": task["profile_id"],
                "platform": task["platform"],
                "target_repository": task["target_repository"],
                "target_tag": task["target_tag"],
                "candidate_task_sha256": task["task_sha256"],
                "publication_task_sha256": protected["task_sha256"],
            }
        )
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for member in members:
        key = (
            member["target_repository"],
            member["target_tag"],
            member["family_id"],
        )
        groups.setdefault(key, []).append(member)
    indexes: list[dict[str, Any]] = []
    for (repository, tag, family_id), grouped in sorted(groups.items()):
        grouped.sort(
            key=lambda item: ("linux/amd64", "linux/arm64").index(item["platform"])
        )
        if [item["platform"] for item in grouped] != [
            "linux/amd64",
            "linux/arm64",
        ]:
            raise ValueError("each release family requires amd64 then arm64")
        if not tag.endswith("-r1"):
            raise ValueError("the initial public image revision must be exact r1")
        indexes.append(
            {
                "family_id": family_id,
                "target_repository": repository,
                "target_tag": tag,
                "member_spec_ids": [item["spec_id"] for item in grouped],
            }
        )
    if len(indexes) != 3 or len({item["target_repository"] for item in indexes}) != 2:
        raise ValueError("release contract requires three families in two packages")
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-contract",
        "staging_repository": STAGING_REPOSITORY,
        "members": members,
        "indexes": indexes,
    }
    return {**payload, "contract_sha256": sha256_value(payload)}


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key != "record_sha256"
    }


def validate_member_record(record: object) -> dict[str, Any]:
    """Validate a canonical publication record without changing image-result state."""
    if not isinstance(record, dict):
        raise ValueError("member publication record must be an object")
    _exact_keys(record, MEMBER_RECORD_KEYS, "member publication record")
    if (
        record["schema_version"] != 1
        or record["kind"] != "ucm-registry-member-publication"
        or record["status"] != "passed"
    ):
        raise ValueError("member publication record identity is invalid")
    contract = canonical_registry_contract()
    authorities = [
        item for item in contract["members"] if item["spec_id"] == record["spec_id"]
    ]
    if len(authorities) != 1:
        raise ValueError("member spec does not resolve in the canonical contract")
    authority = authorities[0]
    stable_fields = (
        "spec_id",
        "profile_id",
        "family_id",
        "platform",
        "target_repository",
        "target_tag",
        "candidate_task_sha256",
        "publication_task_sha256",
    )
    if any(record[field] != authority[field] for field in stable_fields):
        raise ValueError("member record differs from feature/protected task authority")
    if (
        record["staging_repository"] != STAGING_REPOSITORY
        or record["staging_visibility"] != "private"
    ):
        raise ValueError("member record requires the exact private staging package")
    for field in (
        "candidate_task_sha256",
        "publication_task_sha256",
        "build_key_sha256",
        "wheel_sha256",
        "member_digest",
        "config_digest",
        "record_sha256",
    ):
        _digest(record[field], f"member {field}")
    if (
        not isinstance(record["member_size"], int)
        or isinstance(record["member_size"], bool)
        or record["member_size"] < 1
    ):
        raise ValueError("member manifest size must be a positive integer")
    expected_tag = "staging-" + record["build_key_sha256"].removeprefix("sha256:")
    if record["staging_tag"] != expected_tag:
        raise ValueError("staging tag must contain the complete member build key")
    annotations = record["annotations"]
    if not isinstance(annotations, dict):
        raise ValueError("member annotations must be an object")
    _exact_keys(annotations, MEMBER_ANNOTATION_KEYS, "member annotations")
    expected_annotations = {
        "io.ucm.release.build-key-sha256": record["build_key_sha256"],
        "io.ucm.release.candidate-task-sha256": record["candidate_task_sha256"],
        "io.ucm.release.family-id": record["family_id"],
        "io.ucm.release.platform": record["platform"],
        "io.ucm.release.spec-id": record["spec_id"],
        "io.ucm.release.wheel-sha256": record["wheel_sha256"],
    }
    if annotations != expected_annotations:
        raise ValueError("member annotations do not close over build identity")
    if record["record_sha256"] != sha256_value(_record_payload(record)):
        raise ValueError("member publication record digest mismatch")
    return copy.deepcopy(record)


def plan_staging_tag(
    build_key_sha256: object,
    member_digest: object,
    observed_digest: object | None,
) -> dict[str, Any]:
    """Apply the exact absent/create, same/reuse, different/fail contract."""
    build_key = _digest(build_key_sha256, "staging build key")
    expected = _digest(member_digest, "staging member")
    tag = "staging-" + build_key.removeprefix("sha256:")
    if observed_digest is None:
        decision = "create"
    else:
        observed = _digest(observed_digest, "observed staging member")
        if observed != expected:
            raise ValueError(
                f"staging tag collision for {STAGING_REPOSITORY}:{tag}: "
                f"expected {expected}, observed {observed}"
            )
        decision = "reuse"
    return {
        "schema_version": 1,
        "kind": "ucm-registry-staging-tag-plan",
        "repository": STAGING_REPOSITORY,
        "tag": tag,
        "member_digest": expected,
        "decision": decision,
    }


class BarrierBlocker(ValueError):
    """A six-member barrier failure that carries a proven-empty operation ledger."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.operations: list[dict[str, str]] = []


def _index_manifest(
    family_id: str,
    target_repository: str,
    target_tag: str,
    members: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    identity = {
        "schema_version": 1,
        "family_id": family_id,
        "target_repository": target_repository,
        "target_tag": target_tag,
        "members": [
            {
                "platform": item["platform"],
                "member_digest": item["member_digest"],
                "build_key_sha256": item["build_key_sha256"],
                "wheel_sha256": item["wheel_sha256"],
                "candidate_task_sha256": item["candidate_task_sha256"],
                "publication_task_sha256": item["publication_task_sha256"],
            }
            for item in members
        ],
    }
    index_build_key = sha256_value(identity)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": item["member_digest"],
                "size": item["member_size"],
                "platform": {
                    "os": "linux",
                    "architecture": item["platform"].split("/", 1)[1],
                },
                "annotations": {
                    "io.ucm.release.build-key-sha256": item["build_key_sha256"],
                    "io.ucm.release.spec-id": item["spec_id"],
                },
            }
            for item in members
        ],
        "annotations": {
            "io.ucm.release.family-id": family_id,
            "io.ucm.release.index-build-key-sha256": index_build_key,
        },
    }
    return (
        manifest,
        index_build_key,
        "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
    )


def plan_indexes(
    member_records: object,
    inventory: object,
    *,
    member_statuses: object | None = None,
    lane: str | None = None,
) -> dict[str, Any]:
    """Plan all three exact r1 indexes after one indivisible six-member barrier."""
    if not isinstance(member_records, list):
        raise BarrierBlocker("six-member barrier requires a member record array")
    if len(member_records) != 6:
        raise BarrierBlocker("six-member barrier requires exactly six records")
    records = [validate_member_record(item) for item in member_records]
    by_spec: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["spec_id"] in by_spec:
            raise BarrierBlocker("six-member barrier rejects duplicate members")
        by_spec[record["spec_id"]] = record
    if set(by_spec) != set(CANONICAL_MEMBER_SPEC_IDS):
        raise BarrierBlocker("six-member barrier has missing or invented members")
    if member_statuses is not None:
        if not isinstance(member_statuses, dict) or set(member_statuses) != set(
            CANONICAL_MEMBER_SPEC_IDS
        ):
            raise BarrierBlocker("six-member barrier statuses are incomplete")
        failed = sorted(
            spec_id
            for spec_id, status in member_statuses.items()
            if status != "success"
        )
        if failed:
            raise BarrierBlocker(
                f"six-member barrier blocked by unsuccessful members: {failed}"
            )
    if not isinstance(inventory, list) or any(
        not isinstance(item, dict) for item in inventory
    ):
        raise ValueError("index inventory must be an array of objects")
    inventory_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    allowed_targets = {
        (item["target_repository"], item["target_tag"])
        for item in canonical_registry_contract()["indexes"]
    }
    for entry in inventory:
        _exact_keys(
            entry,
            {"repository", "tag", "digest", "build_key_sha256"},
            "index inventory entry",
        )
        key = (entry["repository"], entry["tag"])
        if key not in allowed_targets:
            raise ValueError(
                "index inventory coordinate is outside the exact allowlist"
            )
        if key in inventory_by_target:
            raise ValueError("duplicate index inventory coordinate")
        _digest(entry["digest"], "inventory index")
        _digest(entry["build_key_sha256"], "inventory index build key")
        inventory_by_target[key] = entry
    plans: list[dict[str, Any]] = []
    operations: list[dict[str, str]] = []
    for authority in canonical_registry_contract()["indexes"]:
        grouped = [by_spec[spec_id] for spec_id in authority["member_spec_ids"]]
        manifest, index_build_key, expected_digest = _index_manifest(
            authority["family_id"],
            authority["target_repository"],
            authority["target_tag"],
            grouped,
        )
        coordinate = (authority["target_repository"], authority["target_tag"])
        observed = inventory_by_target.get(coordinate)
        if observed is None:
            decision = "create"
            operations.append(
                {
                    "type": "registry-index-create",
                    "capability": "write",
                    "reference": f"{coordinate[0]}:{coordinate[1]}",
                }
            )
        elif (
            observed["digest"] == expected_digest
            and observed["build_key_sha256"] == index_build_key
        ):
            decision = "reuse"
        else:
            raise ValueError(
                f"r1 conflict for {coordinate[0]}:{coordinate[1]}; refusing r2 or overwrite"
            )
        plans.append(
            {
                "schema_version": 1,
                "kind": "ucm-registry-index-plan",
                "family_id": authority["family_id"],
                "target_repository": authority["target_repository"],
                "target_tag": authority["target_tag"],
                "index_build_key_sha256": index_build_key,
                "expected_index_digest": expected_digest,
                "members": grouped,
                "index_manifest": manifest,
                "decision": decision,
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-plans",
        "plans": plans,
        "operations": operations,
    }
    if operations and lane not in {None, "protected-tag"}:
        raise ValueError(f"{lane} cannot plan write-capable final index operations")
    return {**payload, "plans_sha256": sha256_value(payload)}


def verify_index(plan: object, observed: object | None = None) -> dict[str, Any]:
    """Verify one canonical r1 plan and optional readback record."""
    if not isinstance(plan, dict) or plan.get("kind") != "ucm-registry-index-plan":
        raise ValueError("index plan identity is invalid")
    required = {
        "schema_version",
        "kind",
        "family_id",
        "target_repository",
        "target_tag",
        "index_build_key_sha256",
        "expected_index_digest",
        "members",
        "index_manifest",
        "decision",
    }
    _exact_keys(plan, required, "index plan")
    members = [validate_member_record(item) for item in plan["members"]]
    manifest, build_key, digest = _index_manifest(
        plan["family_id"], plan["target_repository"], plan["target_tag"], members
    )
    if (
        plan["index_manifest"] != manifest
        or plan["index_build_key_sha256"] != build_key
        or plan["expected_index_digest"] != digest
    ):
        raise ValueError("index plan does not close over its exact two members")
    if observed is not None:
        if not isinstance(observed, dict) or observed != {
            "digest": digest,
            "build_key_sha256": build_key,
        }:
            raise ValueError("index readback differs from the canonical plan")
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-verification",
        "family_id": plan["family_id"],
        "target_repository": plan["target_repository"],
        "target_tag": plan["target_tag"],
        "index_build_key_sha256": build_key,
        "index_digest": digest,
        "status": "passed" if observed is not None else "plan-verified",
    }
    return {**payload, "verification_sha256": sha256_value(payload)}


def _registry_reference(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("registry reference must be a string")
    contract = canonical_registry_contract()
    public_repositories = {item["target_repository"] for item in contract["indexes"]}
    public_tags = {
        f"{item['target_repository']}:{item['target_tag']}"
        for item in contract["indexes"]
    }
    if value in public_tags:
        return value
    repository, separator, suffix = value.rpartition("@")
    if (
        separator == "@"
        and repository in public_repositories | {STAGING_REPOSITORY}
        and DIGEST_RE.fullmatch(suffix) is not None
    ):
        return value
    staging_prefix = STAGING_REPOSITORY + ":staging-"
    if value.startswith(staging_prefix) and re.fullmatch(
        r"[0-9a-f]{64}", value.removeprefix(staging_prefix)
    ):
        return value
    raise ValueError(f"registry reference is outside the exact allowlist: {value}")


def _run_registry_tool(
    binary: str,
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    missing_ok: bool = False,
) -> subprocess.CompletedProcess[str]:
    executable = _crane_binary(binary)
    try:
        result = subprocess.run(
            [executable, *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
    except OSError as error:
        raise ValueError(f"failed to execute pinned registry tool: {error}") from error
    if result.returncode != 0 and not missing_ok:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ValueError(f"registry tool {' '.join(arguments[:1])} failed: {detail}")
    return result


def _missing_manifest(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    detail = (result.stderr + "\n" + result.stdout).lower()
    return any(
        marker in detail
        for marker in ("manifest_unknown", "manifest unknown", "not found", "404")
    )


def inventory_registry(*, crane_binary: str = "crane") -> dict[str, Any]:
    """Read the three exact public r1 coordinates without inventing local state."""
    entries: list[dict[str, Any]] = []
    absent: list[dict[str, str]] = []
    operations: list[dict[str, str]] = []
    for target in canonical_registry_contract()["indexes"]:
        reference = f"{target['target_repository']}:{target['target_tag']}"
        operations.append(
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": reference,
            }
        )
        digest_result = _run_registry_tool(
            crane_binary, ["digest", reference], missing_ok=True
        )
        if digest_result.returncode != 0:
            if not _missing_manifest(digest_result):
                detail = digest_result.stderr.strip() or str(digest_result.returncode)
                raise ValueError(f"registry inventory failed for {reference}: {detail}")
            absent.append(
                {
                    "repository": target["target_repository"],
                    "tag": target["target_tag"],
                }
            )
            continue
        digest = _digest(digest_result.stdout.strip(), "inventory index")
        digest_reference = f"{target['target_repository']}@{digest}"
        operations.append(
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": digest_reference,
            }
        )
        manifest_result = _run_registry_tool(
            crane_binary, ["manifest", digest_reference]
        )
        manifest = _unique_json(manifest_result.stdout, "inventory index manifest")
        annotations = manifest.get("annotations")
        if not isinstance(annotations, dict):
            raise ValueError("public index manifest is missing annotations")
        build_key = _digest(
            annotations.get("io.ucm.release.index-build-key-sha256"),
            "inventory index build key",
        )
        entries.append(
            {
                "repository": target["target_repository"],
                "tag": target["target_tag"],
                "digest": digest,
                "build_key_sha256": build_key,
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-inventory",
        "entries": entries,
        "absent": absent,
        "operations": operations,
    }
    return {**payload, "inventory_sha256": sha256_value(payload)}


def readback_reference(
    reference: object,
    *,
    crane_binary: str = "crane",
    anonymous: bool = False,
) -> dict[str, Any]:
    """Read digest and manifest, isolating anonymous auth in a fresh empty config."""
    canonical_reference = _registry_reference(reference)

    def read(environment: dict[str, str] | None) -> dict[str, Any]:
        prefix = "registry-anonymous" if anonymous else "registry-authenticated"
        digest_result = _run_registry_tool(
            crane_binary, ["digest", canonical_reference], environment=environment
        )
        digest = _digest(digest_result.stdout.strip(), "registry readback")
        repository = canonical_reference.rsplit("@", 1)[0].rsplit(":", 1)[0]
        digest_reference = f"{repository}@{digest}"
        manifest_result = _run_registry_tool(
            crane_binary,
            ["manifest", digest_reference],
            environment=environment,
        )
        manifest = _unique_json(manifest_result.stdout, "registry readback manifest")
        operations = [
            {
                "type": f"{prefix}-digest-read",
                "capability": "read",
                "reference": canonical_reference,
            },
            {
                "type": f"{prefix}-manifest-read",
                "capability": "read",
                "reference": digest_reference,
            },
        ]
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": canonical_reference,
            "digest": digest,
            "manifest": manifest,
            "authenticated": not anonymous,
            "operations": operations,
        }
        return {**payload, "readback_sha256": sha256_value(payload)}

    if not anonymous:
        return read(None)
    with tempfile.TemporaryDirectory(
        prefix="ucm-anonymous-docker-config-"
    ) as directory:
        config = Path(directory) / "config.json"
        config.write_bytes(b'{"auths":{}}\n')
        environment = os.environ.copy()
        environment["DOCKER_CONFIG"] = directory
        return read(environment)


def _fresh_write_authority(lane: object) -> dict[str, Any]:
    if lane != "protected-tag":
        raise ValueError("registry writes require the protected-tag lane")
    preflight = core.tag_preflight(lane="protected-tag")
    matrix = core.build_matrix("protected-tag")
    if (
        preflight.get("kind") != "ucm-tag-preflight"
        or preflight.get("lane") != "protected-tag"
        or preflight.get("publication_allowed") is not True
        or preflight.get("write_authority")
        != ["github-prerelease", "ghcr-final-index", "ghcr-private-staging"]
    ):
        raise ValueError("fresh protected Tag preflight did not grant write authority")
    tasks = matrix.get("tasks")
    if (
        matrix.get("lane") != "protected-tag"
        or not isinstance(tasks, list)
        or [item.get("spec_id") for item in tasks] != CANONICAL_MEMBER_SPEC_IDS
        or any(
            item.get("write_authority")
            != ["github-prerelease", "ghcr-final-index", "ghcr-private-staging"]
            for item in tasks
        )
    ):
        raise ValueError("fresh protected matrix did not grant exact write authority")
    return {"preflight": preflight, "matrix": matrix}


def push_member_by_digest(
    archive_path: Path,
    member_record: object,
    *,
    lane: str,
    crane_binary: str = "crane",
) -> dict[str, Any]:
    """Push one canonical archive only to the staging content digest."""
    authority = _fresh_write_authority(lane)
    record = validate_member_record(member_record)
    archive = Path(archive_path)
    if not archive.is_file() or archive.is_symlink():
        raise ValueError("member archive must be one regular local file")
    target = f"{STAGING_REPOSITORY}@{record['member_digest']}"
    result = _run_registry_tool(crane_binary, ["push", str(archive), target])
    observed = _digest(result.stdout.strip(), "pushed member")
    if observed != record["member_digest"]:
        raise ValueError(
            f"push-by-digest returned {observed}, expected {record['member_digest']}"
        )
    operation = {
        "type": "registry-member-push-by-digest",
        "capability": "write",
        "reference": target,
    }
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-member-push",
        "digest": observed,
        "record_sha256": record["record_sha256"],
        "preflight_sha256": authority["preflight"].get("preflight_sha256"),
        "matrix_sha256": authority["matrix"]["matrix_sha256"],
        "operations": [operation],
    }
    return {**payload, "push_sha256": sha256_value(payload)}


def apply_staging_tag(
    member_record: object,
    observed_digest: object | None,
    *,
    lane: str,
    crane_binary: str = "crane",
) -> dict[str, Any]:
    """Create a GC tag only when absent; identity is a read-only reuse."""
    record = validate_member_record(member_record)
    plan = plan_staging_tag(
        record["build_key_sha256"], record["member_digest"], observed_digest
    )
    operations: list[dict[str, str]] = []
    if plan["decision"] == "create":
        _fresh_write_authority(lane)
        source = f"{STAGING_REPOSITORY}@{record['member_digest']}"
        _run_registry_tool(crane_binary, ["tag", source, plan["tag"]])
        operations.append(
            {
                "type": "registry-staging-tag-create",
                "capability": "write",
                "reference": f"{STAGING_REPOSITORY}:{plan['tag']}",
            }
        )
    return {**plan, "operations": operations}


def create_index(
    plan: object,
    *,
    lane: str,
    docker_binary: str = "docker",
) -> dict[str, Any]:
    """Create one exact r1 with imagetools only after fresh protected authority."""
    verification = verify_index(plan)
    if plan["decision"] == "reuse":
        return {**verification, "decision": "reuse", "operations": []}
    _fresh_write_authority(lane)
    executable = Path(docker_binary)
    if executable.is_absolute() and not executable.is_file():
        raise ValueError(f"docker executable does not exist: {docker_binary}")
    target = f"{plan['target_repository']}:{plan['target_tag']}"
    members = [
        f"{STAGING_REPOSITORY}@{item['member_digest']}" for item in plan["members"]
    ]
    arguments = [
        docker_binary,
        "buildx",
        "imagetools",
        "create",
        "--tag",
        target,
        "--annotation",
        (
            "index:io.ucm.release.index-build-key-sha256="
            + plan["index_build_key_sha256"]
        ),
        *members,
    ]
    try:
        result = subprocess.run(
            arguments,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise ValueError(f"failed to execute docker imagetools: {error}") from error
    if result.returncode != 0:
        raise ValueError(
            f"docker imagetools create failed: {result.stderr.strip() or result.returncode}"
        )
    operation = {
        "type": "registry-index-create",
        "capability": "write",
        "reference": target,
    }
    return {**verification, "decision": "create", "operations": [operation]}


def _loopback_request(
    base_url: str,
    method: str,
    path_or_url: str,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    url = (
        path_or_url
        if path_or_url.startswith(("http://", "https://"))
        else base_url + path_or_url
    )
    request = urllib.request.Request(url, data=data, method=method)
    if content_type is not None:
        request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read(), dict(response.headers.items())


def _loopback_upload_blob(base_url: str, repository: str, payload: bytes) -> str:
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    status, _, headers = _loopback_request(
        base_url, "POST", f"/v2/{repository}/blobs/uploads/"
    )
    if status != 202 or "Location" not in headers:
        raise ValueError("loopback Registry did not open a blob upload")
    location = headers["Location"]
    separator = "&" if "?" in location else "?"
    upload_url = location + separator + urllib.parse.urlencode({"digest": digest})
    status, _, _ = _loopback_request(
        base_url,
        "PUT",
        upload_url,
        data=payload,
        content_type="application/octet-stream",
    )
    if status != 201:
        raise ValueError("loopback Registry did not commit the exact blob digest")
    return digest


def _loopback_push_manifest(
    base_url: str,
    repository: str,
    reference: str,
    manifest: dict[str, Any],
) -> tuple[str, int]:
    raw = canonical_bytes(manifest)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    status, _, _ = _loopback_request(
        base_url,
        "PUT",
        f"/v2/{repository}/manifests/{reference}",
        data=raw,
        content_type=manifest["mediaType"],
    )
    if status != 201:
        raise ValueError("loopback Registry did not commit the manifest")
    return digest, len(raw)


def run_loopback_registry_contract(
    *,
    docker_binary: str = "docker",
    crane_binary: str = "crane",
) -> dict[str, Any]:
    """Exercise tiny digest-only manifests in a disposable pinned Registry 2.8.3."""
    registry_image = (
        "docker.io/library/registry:2.8.3@"
        "sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
    )
    crane_version = _run_registry_tool(crane_binary, ["version"])
    if crane_version.stdout.strip() not in {"0.20.3", "v0.20.3"}:
        raise ValueError(
            f"loopback contract requires crane v0.20.3, got {crane_version.stdout.strip()}"
        )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    nonce = uuid.uuid4().hex
    network = f"ucm-registry-contract-network-{nonce}"
    volume = f"ucm-registry-contract-volume-{nonce}"
    container = f"ucm-registry-contract-{nonce}"
    base_url = f"http://127.0.0.1:{port}"
    registry_host = f"127.0.0.1:{port}"
    operations: list[dict[str, str]] = []
    created_network = False
    created_volume = False
    created_container = False

    def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [docker_binary, *arguments],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise ValueError(f"failed to execute docker: {error}") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or str(result.returncode)
            raise ValueError(f"docker {' '.join(arguments[:2])} failed: {detail}")
        return result

    cleanup_errors: list[str] = []
    try:
        docker("network", "create", network)
        created_network = True
        docker("volume", "create", volume)
        created_volume = True
        docker(
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            network,
            "--publish",
            f"127.0.0.1:{port}:5000",
            "--volume",
            f"{volume}:/var/lib/registry",
            registry_image,
        )
        created_container = True
        deadline = time.monotonic() + 20
        while True:
            try:
                status, _, _ = _loopback_request(base_url, "GET", "/v2/")
                if status == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                logs = docker("logs", container, check=False)
                raise ValueError(
                    "loopback Registry did not become ready: "
                    + (logs.stderr.strip() or logs.stdout.strip())
                )
            time.sleep(0.2)

        repository = "ucm-contract/scratch"
        descriptors: list[dict[str, Any]] = []
        for architecture in ("amd64", "arm64"):
            config = canonical_bytes(
                {
                    "architecture": architecture,
                    "os": "linux",
                    "rootfs": {"type": "layers", "diff_ids": []},
                    "config": {},
                }
            )
            config_digest = _loopback_upload_blob(base_url, repository, config)
            manifest = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": len(config),
                },
                "layers": [],
                "annotations": {
                    "io.ucm.release.loopback": architecture,
                },
            }
            manifest_digest = (
                "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()
            )
            observed_digest, manifest_size = _loopback_push_manifest(
                base_url, repository, manifest_digest, manifest
            )
            if observed_digest != manifest_digest:
                raise ValueError("loopback member digest drifted during push")
            reference = f"{registry_host}/{repository}@{manifest_digest}"
            operations.append(
                {
                    "type": "loopback-member-push-by-digest",
                    "capability": "write",
                    "reference": reference,
                }
            )
            manifest_read = _run_registry_tool(
                crane_binary, ["manifest", "--insecure", reference]
            )
            if _unique_json(manifest_read.stdout, "loopback member") != manifest:
                raise ValueError("loopback member manifest readback differs")
            operations.append(
                {
                    "type": "loopback-manifest-read",
                    "capability": "read",
                    "reference": reference,
                }
            )
            blob_read = _run_registry_tool(
                crane_binary,
                [
                    "blob",
                    "--insecure",
                    f"{registry_host}/{repository}@{config_digest}",
                ],
            )
            if blob_read.stdout.encode() != config:
                raise ValueError("loopback config blob readback differs")
            operations.append(
                {
                    "type": "loopback-config-read",
                    "capability": "read",
                    "reference": (f"{registry_host}/{repository}@{config_digest}"),
                }
            )
            descriptors.append(
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": manifest_size,
                    "platform": {"os": "linux", "architecture": architecture},
                }
            )
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": descriptors,
            "annotations": {"io.ucm.release.loopback": "dual-arch"},
        }
        index_digest, _ = _loopback_push_manifest(base_url, repository, "r1", index)
        index_reference = f"{registry_host}/{repository}:r1"
        digest_read = _run_registry_tool(
            crane_binary, ["digest", "--insecure", index_reference]
        )
        if digest_read.stdout.strip() != index_digest:
            raise ValueError("loopback index digest readback differs")
        index_read = _run_registry_tool(
            crane_binary,
            [
                "manifest",
                "--insecure",
                f"{registry_host}/{repository}@{index_digest}",
            ],
        )
        if _unique_json(index_read.stdout, "loopback index") != index:
            raise ValueError("loopback index readback differs")
        operations.extend(
            [
                {
                    "type": "loopback-index-create",
                    "capability": "write",
                    "reference": index_reference,
                },
                {
                    "type": "loopback-index-read",
                    "capability": "read",
                    "reference": (f"{registry_host}/{repository}@{index_digest}"),
                },
            ]
        )
        mutated = copy.deepcopy(index)
        mutated["annotations"]["io.ucm.release.loopback"] = "mutated"
        try:
            _loopback_request(
                base_url,
                "PUT",
                f"/v2/{repository}/manifests/{index_digest}",
                data=canonical_bytes(mutated),
                content_type=mutated["mediaType"],
            )
        except urllib.error.HTTPError as error:
            if error.code not in {400, 404}:
                raise
            negative_mutation = "blocked"
        else:
            raise ValueError("loopback Registry accepted same-name digest mutation")
        payload = {
            "schema_version": 1,
            "kind": "ucm-loopback-registry-contract",
            "registry_image": registry_image,
            "crane_version": "v0.20.3",
            "member_count": 2,
            "index_digest": index_digest,
            "negative_mutation": negative_mutation,
            "operations": operations,
            "status": "passed",
        }
    finally:
        if created_container:
            result = docker("rm", "--force", container, check=False)
            if result.returncode != 0:
                cleanup_errors.append(result.stderr.strip() or "container cleanup")
        if created_volume:
            result = docker("volume", "rm", volume, check=False)
            if result.returncode != 0:
                cleanup_errors.append(result.stderr.strip() or "volume cleanup")
        if created_network:
            result = docker("network", "rm", network, check=False)
            if result.returncode != 0:
                cleanup_errors.append(result.stderr.strip() or "network cleanup")
    if cleanup_errors:
        raise ValueError(f"loopback cleanup failed: {cleanup_errors}")
    return {**payload, "contract_sha256": sha256_value(payload)}
