"""Read-only OCI registry discovery and deterministic image reconciliation."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

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
WHEEL_RECORD_KEYS = {
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
    if product not in TARGET_REPOSITORIES:
        raise ValueError(f"unsupported upstream repository product: {product}")
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
    if not isinstance(record, dict) or set(record) != WHEEL_RECORD_KEYS:
        raise ValueError(
            "wheel inspection record is not a complete Task 2 fixture result"
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
