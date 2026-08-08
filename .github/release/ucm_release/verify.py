"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
from typing import Any, Callable

from .core import sha256_value
from .registry import build_candidate, reconcile, validate_snapshot


EXPECTED_BLOCKERS = [
    "duplicate-conflicting-inventory",
    "missing-linux-arm64",
    "production-wheel-unpublished",
]
ZERO_WRITE_AUDIT = {
    "publication_actions": [],
    "registry_write_commands": [],
    "write_count": 0,
}


def _inventory(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "registry-inventory",
        "entries": entries or [],
    }


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


def _blocked(label: str, operation: Callable[[], object]) -> str | None:
    try:
        operation()
    except (KeyError, TypeError, ValueError):
        return label
    return None


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
        _blocked(
            "duplicate-conflicting-inventory",
            lambda: reconcile(candidate, _inventory([stable, conflicting])),
        ),
        _blocked("missing-linux-arm64", lambda: validate_snapshot(snapshot)),
        _blocked(
            "production-wheel-unpublished",
            lambda: build_candidate(**production_case, fixture_mode=False),
        ),
    ]
    return sorted(item for item in results if item is not None)


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
        "compatibility_rule_id",
        "implementation_digest",
    }
    if set(case) != required:
        raise ValueError(
            "loop verification fields mismatch: "
            f"missing={sorted(required - set(case))}, extra={sorted(set(case) - required)}"
        )
    candidate = build_candidate(**case, fixture_mode=True)
    snapshot = validate_snapshot(case["upstream_snapshot"])
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
    blockers = _required_blockers(case, candidate)

    first_fixture_result = reconcile(candidate, _inventory())
    completed_entry = _entry(candidate, digest)
    final_fixture_result = reconcile(candidate, _inventory([completed_entry]))
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
        "expected_blockers": copy.deepcopy(EXPECTED_BLOCKERS),
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": False,
        "zero_write_audit": copy.deepcopy(ZERO_WRITE_AUDIT),
    }
    return {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-envelope",
        "run": copy.deepcopy(run or {}),
        "payload": payload,
        "payload_sha256": sha256_value(payload),
    }
