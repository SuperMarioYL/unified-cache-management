"""Read-only OCI registry discovery and deterministic image reconciliation."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .core import sha256_value


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
INVENTORY_KEYS = {"schema_version", "kind", "entries"}
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
    "implementation_digest",
}
WHEEL_INPUT_KEYS = {"spec_id", "sha256", "declaration_sha256"}
UPSTREAM_INPUT_KEYS = {
    "repository",
    "exact_upstream_tag",
    "index_digest",
    "platforms",
}


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
        operating_system = "linux"
    else:
        match = re.fullmatch(f"({VERSION})(-a3)?(-openeuler)?", tag)
        if match is None:
            raise ValueError(f"unsupported vllm-ascend upstream tag: {tag}")
        npu_arch = "a3" if match.group(2) else "a2"
        operating_system = "openEuler" if match.group(3) else "linux"
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
    if fixture is not None:
        snapshot = validate_snapshot(fixture)
        if (
            snapshot["repository"] != repository
            or snapshot["upstream_tag"] != upstream_tag
        ):
            raise ValueError(
                "fixture snapshot repository/tag does not match the exact request"
            )
        return snapshot
    tagged_reference = f"{repository}:{upstream_tag}"
    index_digest = _digest(
        _crane(crane_binary, "digest", tagged_reference), "crane index"
    )
    index = _unique_json(
        _crane(crane_binary, "manifest", tagged_reference), "crane index"
    )
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
        manifest_digest = _digest(descriptor.get("digest"), "platform manifest")
        child = _unique_json(
            _crane(crane_binary, "manifest", f"{repository}@{manifest_digest}"),
            f"platform manifest {manifest_digest}",
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
    return validate_snapshot(
        {
            "schema_version": 1,
            "kind": "upstream-registry-snapshot",
            "repository": repository,
            "upstream_tag": upstream_tag,
            "index_digest": index_digest,
            "platforms": platforms,
        }
    )


def validate_public_tag(tag: object) -> str:
    if not isinstance(tag, str) or OCI_TAG_RE.fullmatch(tag) is None:
        raise ValueError(
            "public tag must use strict OCI tag syntax and be at most 128 bytes"
        )
    match = re.fullmatch(r"(.+)-r([1-9][0-9]*)", tag)
    if match is None:
        raise ValueError("public tag must end in canonical -rN with N >= 1")
    return tag


def _select_wheel(
    release_manifest: dict[str, Any],
    wheel_records: list[dict[str, Any]],
    spec_id: str,
    fixture_mode: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not fixture_mode:
        raise ValueError(
            "production wheel is unpublished; Task 2 emits no production publication"
        )
    if release_manifest.get("kind") != "ucm-core-release-manifest":
        raise ValueError("release manifest kind is invalid")
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
    if not spec.get("build_eligible") or spec.get("blocked_reasons"):
        raise ValueError("selected manifest wheel spec is blocked")
    if (
        asset.get("type") != "wheel"
        or asset.get("status") != "candidate"
        or asset.get("required") is not True
    ):
        raise ValueError("selected manifest wheel asset is not a required candidate")
    _digest(record.get("sha256"), "selected wheel")
    _digest(record.get("declaration_sha256"), "selected wheel declaration")
    if record["declaration_sha256"] != spec.get("declaration_sha256"):
        raise ValueError("selected wheel declaration does not match its manifest spec")
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


def build_candidate(
    release_manifest: dict[str, Any],
    wheel_records: list[dict[str, Any]],
    spec_id: str,
    upstream_snapshot: dict[str, Any],
    compatibility_rule_id: str,
    implementation_digest: str,
    *,
    fixture_mode: bool,
) -> dict[str, Any]:
    """Bind all immutable inputs into one build key and one target tag family."""
    if not isinstance(release_manifest, dict) or not isinstance(wheel_records, list):
        raise ValueError("release manifest and wheel records have invalid types")
    _, wheel = _select_wheel(release_manifest, wheel_records, spec_id, fixture_mode)
    snapshot = validate_snapshot(upstream_snapshot)
    if not isinstance(compatibility_rule_id, str) or not compatibility_rule_id:
        raise ValueError("compatibility rule id must be non-empty")
    implementation_digest = _digest(implementation_digest, "implementation")
    parsed = parse_upstream_tag(
        _product_for_repository(snapshot["repository"]), snapshot["upstream_tag"]
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
        },
        "upstream": upstream_identity,
        "compatibility_rule_id": compatibility_rule_id,
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
    ):
        _digest(digest, label)
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


def _validate_inventory(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(inventory, dict):
        raise ValueError("registry inventory must be an object")
    _exact_keys(inventory, INVENTORY_KEYS, "registry inventory")
    if inventory["schema_version"] != 1 or inventory["kind"] != "registry-inventory":
        raise ValueError("registry inventory identity is invalid")
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
            raise ValueError(
                f"{label} registry inventory entries for {identity[0]}:{identity[1]}"
            )
        by_tag[identity] = entry
    return entries


def reconcile(candidate: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Return a build task or a no-op from a passed, immutable Registry view."""
    _validate_candidate(candidate)
    entries = _validate_inventory(inventory)
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
        "publication_attempted": False,
        "registry_write_commands": [],
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
        "publication_attempted": False,
    }
    return {**common, "decision": "schedule", "task_count": 1, "tasks": [task]}
