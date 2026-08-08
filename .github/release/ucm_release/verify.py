"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import re
from types import MappingProxyType
from typing import Any, Callable

from .core import sha256_value
from .registry import (
    RegistryBlocker,
    TARGET_REPOSITORIES,
    build_candidate,
    inventory_digest,
    reconcile,
    scan_registry,
    parse_upstream_tag,
    validate_public_tag,
    validate_snapshot,
)


EXPECTED_BLOCKERS = [
    "duplicate-conflicting-inventory",
    "missing-linux-arm64",
    "production-wheel-unpublished",
]
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[._:-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
OPERATION_CONTRACTS = MappingProxyType(
    {
        "fixture-read": ("read", "upstream-tag"),
        "crane-digest": ("read", "upstream-tag"),
        "crane-manifest": ("read", "upstream-digest"),
        "registry-inventory-read": ("read", "digest"),
        "build-plan": ("plan", "target-tag"),
    }
)
KNOWN_WRITE_OPERATION_TYPES = frozenset(
    {
        "registry-push",
        "registry-copy",
        "registry-tag",
        "crane-push",
        "crane-copy",
        "crane-tag",
    }
)


def _inventory(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    inventory = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": entries or [],
    }
    inventory["inventory_sha256"] = inventory_digest(inventory)
    return inventory


def _entry(
    candidate: dict[str, Any],
    digest: str,
    *,
    revision: int = 1,
    observed_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "repository": candidate["target_repository"],
        "tag": f"{candidate['tag_base']}-r{revision}",
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": observed_digest or digest,
        "evidence_digest": digest,
    }


def expect_blocker(code: str, operation: Callable[[], object]) -> str:
    """Accept only the exact typed blocker expected by a verification scenario."""
    try:
        operation()
    except RegistryBlocker as error:
        if error.code != code:
            raise ValueError(f"expected blocker {code}, got {error.code}") from error
        return error.code
    raise ValueError(f"expected blocker {code} was not raised")


def _validate_operation_reference(reference_kind: str, reference: object) -> None:
    if not isinstance(reference, str):
        raise ValueError("operation has malformed reference")
    if reference_kind == "digest":
        valid = DIGEST_RE.fullmatch(reference) is not None
    elif reference_kind == "upstream-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and REPOSITORY_RE.fullmatch(repository) is not None
            and repository.rsplit("/", 1)[-1] in TARGET_REPOSITORIES
            and DIGEST_RE.fullmatch(digest) is not None
        )
    elif reference_kind == "upstream-tag":
        repository, separator, tag = reference.rpartition(":")
        valid = separator == ":" and REPOSITORY_RE.fullmatch(repository) is not None
        if valid:
            try:
                parse_upstream_tag(repository.rsplit("/", 1)[-1], tag)
            except ValueError:
                valid = False
    elif reference_kind == "target-tag":
        matching = [
            repository
            for repository in TARGET_REPOSITORIES.values()
            if reference.startswith(repository + ":")
        ]
        valid = len(matching) == 1
        if valid:
            try:
                validate_public_tag(reference.removeprefix(matching[0] + ":"))
            except ValueError:
                valid = False
    else:  # pragma: no cover - immutable mapping owns this branch.
        raise ValueError(f"unknown operation reference contract: {reference_kind}")
    if not valid:
        raise ValueError(
            f"operation has malformed reference for {reference_kind}: {reference}"
        )


def audit_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive zero-write evidence from emitted operation ledgers."""
    if not isinstance(operations, list):
        raise ValueError("operation ledger must be an array")
    operation_types: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {
            "type",
            "capability",
            "reference",
        }:
            raise ValueError(
                "malformed ledger entry: expected exactly type/capability/reference"
            )
        operation_type = operation["type"]
        if operation_type in KNOWN_WRITE_OPERATION_TYPES:
            raise ValueError(
                f"write-capable operation type is forbidden: {operation_type}"
            )
        if operation_type not in OPERATION_CONTRACTS:
            raise ValueError(f"unknown operation type: {operation_type}")
        expected_capability, reference_kind = OPERATION_CONTRACTS[operation_type]
        if operation["capability"] != expected_capability:
            raise ValueError(
                f"operation capability mismatch for {operation_type}: "
                f"expected {expected_capability}, got {operation['capability']}"
            )
        _validate_operation_reference(reference_kind, operation["reference"])
        identity = (operation_type, operation["reference"])
        if identity in identities:
            raise ValueError(f"duplicate operation identity: {identity}")
        identities.add(identity)
        operation_types.add(operation_type)
    return {
        "operation_count": len(operations),
        "operation_types": sorted(operation_types),
        "write_capable_operations": [],
        "write_count": 0,
    }


def audit_operation_batches(
    operation_batches: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Audit each producer ledger independently, then aggregate proven summaries."""
    if not isinstance(operation_batches, list):
        raise ValueError("operation ledger batches must be an array")
    audits = [audit_operations(batch) for batch in operation_batches]
    return {
        "operation_count": sum(audit["operation_count"] for audit in audits),
        "operation_types": sorted(
            {
                operation_type
                for audit in audits
                for operation_type in audit["operation_types"]
            }
        ),
        "write_capable_operations": [],
        "write_count": 0,
    }


def _required_blockers(case: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    snapshot = copy.deepcopy(case["upstream_snapshot"])
    snapshot["platforms"] = [
        item for item in snapshot["platforms"] if item["architecture"] != "arm64"
    ]
    stable = _entry(candidate, case["upstream_snapshot"]["index_digest"])
    conflicting = copy.deepcopy(stable)
    conflicting["observed_digest"] = "sha256:" + "f" * 64
    production_case = copy.deepcopy(case)
    results = [
        expect_blocker(
            "duplicate-conflicting-inventory",
            lambda: reconcile(candidate, _inventory([stable, conflicting])),
        ),
        expect_blocker("missing-linux-arm64", lambda: validate_snapshot(snapshot)),
        expect_blocker(
            "production-wheel-unpublished",
            lambda: build_candidate(**production_case, fixture_mode=False),
        ),
    ]
    return sorted(results)


def _artifact_digests(
    candidate: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    return {
        "release_manifest_sha256": candidate["build_inputs"]["release_manifest_sha256"],
        "wheel": copy.deepcopy(candidate["build_inputs"]["wheel"]),
        "upstream": {
            "index_digest": snapshot["index_digest"],
            "platforms": copy.deepcopy(snapshot["platforms"]),
        },
        "implementation_digest": candidate["build_inputs"]["implementation_digest"],
        "compatibility_rule_sha256": candidate["build_inputs"][
            "compatibility_rule_sha256"
        ],
        "build_key_sha256": candidate["build_key_sha256"],
        "tag_family_sha256": candidate["tag_family_sha256"],
    }


def verify_loop(
    case: dict[str, Any], *, run: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Exercise six fixture scenarios and hash only their deterministic payload."""
    if not isinstance(case, dict):
        raise ValueError("loop verification input must be an object")
    required = {
        "release_manifest",
        "wheel_records",
        "spec_id",
        "upstream_snapshot",
        "compatibility",
        "compatibility_rule_id",
        "implementation_digest",
    }
    if set(case) != required:
        raise ValueError(
            "loop verification fields mismatch: "
            f"missing={sorted(required - set(case))}, extra={sorted(set(case) - required)}"
        )
    requested_snapshot = validate_snapshot(case["upstream_snapshot"])
    scan_result = scan_registry(
        requested_snapshot["repository"],
        requested_snapshot["upstream_tag"],
        fixture=requested_snapshot,
    )
    fixture_case = {**case, "upstream_snapshot": scan_result["snapshot"]}
    candidate = build_candidate(**fixture_case, fixture_mode=True)
    snapshot = scan_result["snapshot"]
    digest = snapshot["index_digest"]

    new_result = reconcile(candidate, _inventory())
    stable_inventory = _inventory([_entry(candidate, digest)])
    same_result = reconcile(candidate, stable_inventory)
    drift_inventory = _inventory(
        [
            _entry(
                candidate,
                digest,
                observed_digest="sha256:" + "f" * 64,
            )
        ]
    )
    drift_result = reconcile(candidate, drift_inventory)
    blockers = _required_blockers(fixture_case, candidate)

    first_fixture_result = reconcile(candidate, _inventory())
    completed_entry = _entry(candidate, digest)
    final_fixture_result = reconcile(candidate, _inventory([completed_entry]))
    operation_batches = [
        scan_result["operations"],
        *[
            result["operations"]
            for result in (
                new_result,
                same_result,
                drift_result,
                first_fixture_result,
                final_fixture_result,
            )
        ],
    ]
    zero_write_audit = audit_operation_batches(operation_batches)
    digest_chain = _artifact_digests(candidate, snapshot)
    platforms = digest_chain["upstream"]["platforms"]
    complete_chain = (
        len(platforms) == 2
        and {item["architecture"] for item in platforms} == {"amd64", "arm64"}
        and all(item["manifest_digest"] and item["config_digest"] for item in platforms)
    )

    scenarios = [
        {
            "name": "new-input-one-task",
            "passed": new_result["task_count"] == 1
            and new_result["tasks"][0]["revision"] == 1,
            "task_count": new_result["task_count"],
            "task_tags": [item["tag"] for item in new_result["tasks"]],
        },
        {
            "name": "identical-input-zero-tasks",
            "passed": same_result["task_count"] == 0,
            "task_count": same_result["task_count"],
            "task_tags": [],
        },
        {
            "name": "tag-digest-drift-r2",
            "passed": drift_result["task_count"] == 1
            and drift_result["tasks"][0]["revision"] == 2
            and drift_result["inventory"] == drift_inventory,
            "task_count": drift_result["task_count"],
            "task_tags": [item["tag"] for item in drift_result["tasks"]],
        },
        {
            "name": "complete-digest-chain",
            "passed": complete_chain,
            "platform_count": len(platforms),
        },
        {
            "name": "required-failures-block",
            "passed": blockers == EXPECTED_BLOCKERS,
            "blockers": blockers,
        },
        {
            "name": "fixture-candidate-full-zero-reconcile",
            "passed": first_fixture_result["task_count"] == 1
            and final_fixture_result["task_count"] == 0,
            "initial_task_count": first_fixture_result["task_count"],
            "final_task_count": final_fixture_result["task_count"],
        },
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-payload",
        "must_green": all(item["passed"] for item in scenarios),
        "scenarios": scenarios,
        "artifact_digests": digest_chain,
        "compatibility_rule_id": case["compatibility_rule_id"],
        "expected_blockers": {
            "scenario_codes": copy.deepcopy(EXPECTED_BLOCKERS),
            "production": copy.deepcopy(case["release_manifest"]["blockers"]),
        },
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": zero_write_audit["write_count"] != 0,
        "zero_write_audit": zero_write_audit,
    }
    return {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-envelope",
        "run": copy.deepcopy(run or {}),
        "payload": payload,
        "payload_sha256": sha256_value(payload),
    }
