"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from . import chart, image, wheel
from .core import (
    _build_fixture_release_manifest,
    canonical_bytes,
    load_catalog,
    python_runtime_requirements,
    sha256_value,
)
from .registry import (
    FIXTURE_TARGET_REPOSITORIES,
    RegistryBlocker,
    build_fixture_candidate,
    fixture_inventory_digest,
    parse_fixture_upstream_tag,
    reconcile_fixture_candidate,
    scan_fixture_registry,
    validate_public_tag,
    validate_resolved_plan,
    validate_fixture_snapshot,
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
        "fixture-read": ("read", "fixture-upstream-tag"),
        "crane-digest": ("read", "upstream-tag"),
        "crane-manifest": ("read", "upstream-digest"),
        "registry-inventory-read": ("read", "digest"),
        "build-plan": ("plan", "fixture-target-tag"),
        "registry-member-push-by-digest": ("write", "staging-digest"),
        "registry-staging-tag-create": ("write", "staging-tag"),
        "registry-index-create": ("write", "public-target"),
        "registry-authenticated-digest-read": (
            "read",
            "registry-read-tag-or-digest",
        ),
        "registry-authenticated-manifest-read": ("read", "registry-read-digest"),
        "registry-authenticated-config-blob-read": (
            "read",
            "registry-read-digest",
        ),
        "registry-authenticated-layer-blob-read": (
            "read",
            "registry-read-digest",
        ),
        "registry-anonymous-digest-read": ("read", "registry-read-tag"),
        "registry-anonymous-manifest-read": ("read", "registry-read-digest"),
        "registry-anonymous-config-blob-read": ("read", "registry-read-digest"),
        "registry-anonymous-layer-blob-read": ("read", "registry-read-digest"),
        "registry-anonymous-prewrite-visibility-read": ("read", "staging-tag"),
        "registry-authenticated-staging-prewrite-read": (
            "read",
            "staging-tag",
        ),
        "registry-anonymous-visibility-read": ("read", "staging-tag"),
        "registry-authenticated-recursive-validate": (
            "read",
            "registry-read-digest",
        ),
        "registry-anonymous-recursive-validate": (
            "read",
            "registry-read-digest",
        ),
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
        "registry-member-push-by-digest",
        "registry-staging-tag-create",
        "registry-index-create",
    }
)
WORKFLOW_REFS = [
    "release-ucm.yml",
    "_build-wheel.yml",
    "release-vllm-images.yml",
    "_build-image.yml",
]
REQUIRED_SCENARIOS = [
    "new-input-one-task",
    "identical-input-zero-tasks",
    "tag-digest-drift-r2",
    "complete-digest-chain",
    "required-failures-block",
    "fixture-candidate-full-zero-reconcile",
]
REQUIRED_IMAGE_GATES = {
    "base_verified",
    "wheel_verified",
    "install",
    "pip_check",
    "direct_url",
    "ucm_import",
    "runtime_dependency_imports",
    "abi",
}


def _source_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source SHA must be a full lowercase Git commit")
    return value


def run_bound_artifact_name(
    logical_name: object, run_id: object, run_attempt: object
) -> str:
    """Bind physical Actions Artifact identity to one workflow run attempt."""
    if (
        not isinstance(logical_name, str)
        or not logical_name
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", logical_name) is None
    ):
        raise ValueError("logical artifact name is invalid")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[1-9][0-9]*", run_id) is None
        or not isinstance(run_attempt, int)
        or isinstance(run_attempt, bool)
        or run_attempt < 1
    ):
        raise ValueError("artifact run identity is invalid")
    return f"{logical_name}-run-{run_id}-attempt-{run_attempt}"


def validate_run_bound_artifact_name(
    physical_name: object, logical_name: object, run: object
) -> str:
    """Reject stale/cross-attempt Artifact names before reopening payload bytes."""
    if not isinstance(run, dict) or set(run) != {"run_id", "run_attempt"}:
        raise ValueError("artifact run envelope is invalid")
    expected = run_bound_artifact_name(logical_name, run["run_id"], run["run_attempt"])
    if physical_name != expected:
        raise ValueError("physical artifact name is not bound to this run attempt")
    return expected


def resolve_run_bound_artifact_directories(
    root: Path,
    logical_names: object,
    *,
    run: object,
    label: str,
) -> dict[str, Path]:
    """Resolve an exact set of downloaded Artifact directories for one attempt."""
    root = Path(root)
    if (
        not root.is_dir()
        or root.is_symlink()
        or not isinstance(logical_names, list)
        or not logical_names
        or any(not isinstance(item, str) for item in logical_names)
        or len(set(logical_names)) != len(logical_names)
        or not isinstance(label, str)
        or not label
    ):
        raise ValueError(f"{label or 'run-bound'} artifact root/set is invalid")
    physical_by_logical = {
        logical_name: validate_run_bound_artifact_name(
            run_bound_artifact_name(
                logical_name,
                run.get("run_id") if isinstance(run, dict) else None,
                run.get("run_attempt") if isinstance(run, dict) else None,
            ),
            logical_name,
            run,
        )
        for logical_name in logical_names
    }
    observed = {
        path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    }
    expected = set(physical_by_logical.values())
    if observed != expected:
        raise ValueError(
            f"{label} artifact directories are missing, stale, or extra for this attempt"
        )
    return {
        logical_name: root / physical_name
        for logical_name, physical_name in physical_by_logical.items()
    }


def extract_index_publication_record(
    envelope: object,
    parent_plans: object,
    family_id: object,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Reopen Task 4's create envelope before extracting its strict record."""
    from . import registry

    if not isinstance(envelope, dict) or not isinstance(family_id, str):
        raise ValueError("index publication envelope is malformed")
    extras = {
        "verification_sha256",
        "decision",
        "postwrite_manifest_sha256",
        "preflight_sha256",
    }
    if set(envelope) != registry.INDEX_RECORD_KEYS | extras:
        raise ValueError("index publication envelope fields are noncanonical")
    for field in (
        "verification_sha256",
        "postwrite_manifest_sha256",
        "preflight_sha256",
    ):
        if DIGEST_RE.fullmatch(str(envelope[field])) is None:
            raise ValueError(f"index publication envelope {field} is invalid")
    record = {key: copy.deepcopy(envelope[key]) for key in registry.INDEX_RECORD_KEYS}
    record = registry.validate_index_record(
        record,
        parent_plans=parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    plans = parent_plans.get("plans") if isinstance(parent_plans, dict) else None
    matches = (
        [item for item in plans if item.get("family_id") == family_id]
        if isinstance(plans, list) and all(isinstance(item, dict) for item in plans)
        else []
    )
    if len(matches) != 1 or record["family_id"] != family_id:
        raise ValueError("index publication family is not parent-bound")
    parent_decision = matches[0].get("decision")
    decision = envelope["decision"]
    if decision not in {"create", "reuse"}:
        raise ValueError("index publication decision is invalid")
    if parent_decision == "reuse" and decision != "reuse":
        raise ValueError("an index reuse plan cannot become a create")
    expected_operations = (
        [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": f"{record['target_repository']}:{record['target_tag']}",
            }
        ]
        if decision == "create"
        else []
    )
    if record["operations"] != expected_operations:
        raise ValueError("index publication decision differs from its operation ledger")
    if envelope["postwrite_manifest_sha256"] != record["index_digest"]:
        raise ValueError("index post-write bytes differ from the published digest")
    return record


def validate_index_readbacks(
    readbacks: object,
    index_records: object,
    *,
    parent_plans: object,
    anonymous: bool,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> list[dict[str, Any]]:
    """Bind Registry readbacks to the exact frozen family task set."""
    from . import registry

    contract = registry._require_resolved_registry_contract(
        resolved_plan, expected_plan_sha256
    )
    if not isinstance(anonymous, bool):
        raise ValueError("index readback authentication mode must be boolean")
    if not isinstance(readbacks, list) or not isinstance(index_records, list):
        raise ValueError("index readback closure requires two arrays")
    contracts = contract["indexes"]
    if len(readbacks) != len(contracts) or len(index_records) != len(contracts):
        raise ValueError("index readback closure differs from frozen family tasks")
    parent = registry.validate_index_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    records = [
        registry.validate_index_record(
            item,
            parent_plans=parent,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in index_records
    ]
    by_family = {
        item.get("family_id"): item for item in records if isinstance(item, dict)
    }
    if set(by_family) != {item["family_id"] for item in contracts}:
        raise ValueError("index records do not cover frozen family tasks")
    validated: list[dict[str, Any]] = []
    for authority, readback in zip(contracts, readbacks, strict=True):
        record = by_family[authority["family_id"]]
        plans = [
            item
            for item in parent["plans"]
            if item["family_id"] == authority["family_id"]
        ]
        if len(plans) != 1:
            raise ValueError("index readback family does not resolve in parent plans")
        validated.append(
            registry._validate_prepared_index_readback(
                readback,
                plan=plans[0],
                expected_digest=record["index_digest"],
                authenticated=not anonymous,
            )
        )
    return validated


def _validated_publication_members(
    *,
    member_records: object,
    parent_plans: object,
    source_sha: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], dict[str, Any]]:
    from . import registry

    contract = registry._require_resolved_registry_contract(
        resolved_plan, expected_plan_sha256
    )
    source_sha = _source_sha(source_sha)
    if not isinstance(member_records, list):
        raise ValueError("protected publication member records must be an array")
    parent = registry.validate_index_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    members = [
        registry.validate_member_record(
            item,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in member_records
    ]
    authority_key = "candidate_task_sha256"
    member_order = [item[authority_key] for item in contract["members"]]
    by_authority = {item[authority_key]: item for item in members}
    if set(by_authority) != set(member_order) or len(members) != len(member_order):
        raise ValueError("protected publication differs from frozen member tasks")
    members = [by_authority[value] for value in member_order]
    if any(item["source_sha"] != source_sha for item in members):
        raise ValueError("protected member source SHA differs from the tag")
    if members != parent["member_records"] or parent["source_sha"] != source_sha:
        raise ValueError("protected members differ from their exact parent plans")
    family_order = [item["family_task_id"] for item in contract["indexes"]]
    return parent, members, family_order, contract


def _publication_operation_audit(
    operation_batches: list[list[dict[str, Any]]],
    *,
    staging_repository: str,
    public_targets: set[str] | None = None,
) -> dict[str, Any]:
    audits = [
        audit_operations(
            batch,
            lane="protected-tag",
            staging_repository=staging_repository,
            public_targets=public_targets,
        )
        for batch in operation_batches
    ]
    return {
        "batch_count": len(audits),
        "operation_count": sum(item["operation_count"] for item in audits),
        "write_count": sum(item["write_count"] for item in audits),
        "ledger_sha256": sha256_value(operation_batches),
        "batches": audits,
    }


def _validate_member_artifact_collection(
    collection: object,
    members: list[dict[str, Any]],
    *,
    source_sha: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    record_sha256s = {
        authority["task_id"]: member["record_sha256"]
        for authority, member in zip(contract["members"], members, strict=True)
    }
    resolved_plan_sha256 = contract["resolved_plan_sha256"]
    normalized_keys = {
        "schema_version",
        "kind",
        "source_sha",
        "member_record_sha256s",
        "member_preflight_sha256s",
        "collection_sha256",
        "resolved_plan_sha256",
    }
    if isinstance(collection, dict) and set(collection) == normalized_keys:
        preflight_sha256s = collection.get("member_preflight_sha256s")
        if (
            collection.get("schema_version") != 1
            or isinstance(collection.get("schema_version"), bool)
            or collection.get("kind") != "ucm-member-artifact-collection"
            or collection.get("source_sha") != source_sha
            or collection.get("resolved_plan_sha256") != resolved_plan_sha256
            or collection.get("member_record_sha256s") != record_sha256s
            or not isinstance(preflight_sha256s, dict)
            or set(preflight_sha256s) != set(record_sha256s)
            or any(
                DIGEST_RE.fullmatch(str(value)) is None
                for value in preflight_sha256s.values()
            )
            or collection.get("collection_sha256")
            != sha256_value(
                {
                    key: value
                    for key, value in collection.items()
                    if key != "collection_sha256"
                }
            )
        ):
            raise ValueError("normalized member artifact collection is invalid")
        return copy.deepcopy(collection)
    if (
        not isinstance(collection, dict)
        or set(collection)
        != {
            "schema_version",
            "kind",
            "source_sha",
            "member_records",
            "member_record_sha256s",
            "member_preflight_sha256s",
            "collection_sha256",
            "resolved_plan_sha256",
        }
        or collection.get("schema_version") != 1
        or isinstance(collection.get("schema_version"), bool)
        or collection.get("kind") != "ucm-member-artifact-collection"
        or collection.get("source_sha") != source_sha
        or collection.get("resolved_plan_sha256") != resolved_plan_sha256
        or not isinstance(collection.get("member_records"), list)
        or not all(
            isinstance(path, str) and path for path in collection["member_records"]
        )
        or collection.get("collection_sha256")
        != sha256_value(
            {
                key: value
                for key, value in collection.items()
                if key not in {"member_records", "collection_sha256"}
            }
        )
    ):
        raise ValueError("member artifact collection evidence is invalid")
    preflight_sha256s = collection.get("member_preflight_sha256s")
    if (
        collection.get("member_record_sha256s") != record_sha256s
        or not isinstance(preflight_sha256s, dict)
        or set(preflight_sha256s) != set(record_sha256s)
        or any(
            DIGEST_RE.fullmatch(str(value)) is None
            for value in preflight_sha256s.values()
        )
        or len(collection["member_records"]) != len(members)
        or [Path(path).name for path in collection["member_records"]]
        != [f"{key}.json" for key in record_sha256s]
    ):
        raise ValueError("member artifact collection differs from frozen member tasks")
    return {
        "schema_version": 1,
        "kind": collection["kind"],
        "source_sha": source_sha,
        "resolved_plan_sha256": resolved_plan_sha256,
        "member_record_sha256s": copy.deepcopy(record_sha256s),
        "member_preflight_sha256s": copy.deepcopy(preflight_sha256s),
        "collection_sha256": collection["collection_sha256"],
    }


def _validate_provisional_artifact_collection(
    collection: object,
    provisionals: list[dict[str, Any]],
    *,
    parent: dict[str, Any],
    source_sha: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    provisional_sha256s = {
        item["family_task_id"]: item["provisional_sha256"] for item in provisionals
    }
    preflight_sha256s = {
        item["family_task_id"]: item["preflight_sha256"] for item in provisionals
    }
    normalized_keys = {
        "schema_version",
        "kind",
        "source_sha",
        "parent_plans_sha256",
        "provisional_sha256s",
        "provisional_preflight_sha256s",
        "collection_sha256",
        "resolved_plan_sha256",
    }
    if isinstance(collection, dict) and set(collection) == normalized_keys:
        if (
            collection.get("schema_version") != 1
            or isinstance(collection.get("schema_version"), bool)
            or collection.get("kind") != "ucm-provisional-artifact-collection"
            or collection.get("source_sha") != source_sha
            or collection.get("resolved_plan_sha256")
            != contract["resolved_plan_sha256"]
            or collection.get("parent_plans_sha256") != parent["plans_sha256"]
            or collection.get("provisional_sha256s") != provisional_sha256s
            or collection.get("provisional_preflight_sha256s") != preflight_sha256s
            or collection.get("collection_sha256")
            != sha256_value(
                {
                    key: value
                    for key, value in collection.items()
                    if key != "collection_sha256"
                }
            )
        ):
            raise ValueError("normalized provisional artifact collection is invalid")
        return copy.deepcopy(collection)
    if (
        not isinstance(collection, dict)
        or set(collection)
        != {
            "schema_version",
            "kind",
            "source_sha",
            "parent_plans_sha256",
            "provisional_indexes",
            "provisional_sha256s",
            "provisional_preflight_sha256s",
            "collection_sha256",
            "resolved_plan_sha256",
        }
        or collection.get("schema_version") != 1
        or isinstance(collection.get("schema_version"), bool)
        or collection.get("kind") != "ucm-provisional-artifact-collection"
        or collection.get("source_sha") != source_sha
        or collection.get("resolved_plan_sha256") != contract["resolved_plan_sha256"]
        or collection.get("parent_plans_sha256") != parent["plans_sha256"]
        or not isinstance(collection.get("provisional_indexes"), list)
        or not all(
            isinstance(path, str) and path for path in collection["provisional_indexes"]
        )
        or collection.get("collection_sha256")
        != sha256_value(
            {
                key: value
                for key, value in collection.items()
                if key not in {"provisional_indexes", "collection_sha256"}
            }
        )
    ):
        raise ValueError("provisional artifact collection evidence is invalid")
    if (
        collection.get("provisional_sha256s") != provisional_sha256s
        or collection.get("provisional_preflight_sha256s") != preflight_sha256s
        or len(collection["provisional_indexes"]) != len(provisionals)
        or [Path(path).name for path in collection["provisional_indexes"]]
        != [f"{key}.json" for key in provisional_sha256s]
    ):
        raise ValueError(
            "provisional artifact collection differs from frozen family tasks"
        )
    return {
        "schema_version": 1,
        "kind": collection["kind"],
        "source_sha": source_sha,
        "resolved_plan_sha256": contract["resolved_plan_sha256"],
        "parent_plans_sha256": parent["plans_sha256"],
        "provisional_sha256s": copy.deepcopy(provisional_sha256s),
        "provisional_preflight_sha256s": copy.deepcopy(preflight_sha256s),
        "collection_sha256": collection["collection_sha256"],
    }


def authenticated_registry_publication_evidence(
    *,
    member_records: object,
    member_collection: object,
    provisional_indexes: object,
    provisional_collection: object,
    parent_plans: object,
    source_sha: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate the frozen member/family sets without anonymous publication."""
    from . import registry

    parent, members, family_order, contract = _validated_publication_members(
        member_records=member_records,
        parent_plans=parent_plans,
        source_sha=source_sha,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if not isinstance(provisional_indexes, list):
        raise ValueError("authenticated publication provisionals must be an array")
    provisionals = [
        registry.validate_provisional_index(
            item,
            parent_plans=parent,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in provisional_indexes
    ]
    by_family = {item["family_task_id"]: item for item in provisionals}
    if len(provisionals) != len(family_order) or set(by_family) != set(family_order):
        raise ValueError("authenticated publication differs from frozen family tasks")
    provisionals = [by_family[family_id] for family_id in family_order]
    member_collection_evidence = _validate_member_artifact_collection(
        member_collection,
        members,
        source_sha=parent["source_sha"],
        contract=contract,
    )
    provisional_collection_evidence = _validate_provisional_artifact_collection(
        provisional_collection,
        provisionals,
        parent=parent,
        source_sha=parent["source_sha"],
        contract=contract,
    )
    batches: list[list[dict[str, Any]]] = [
        *[copy.deepcopy(item["operations"]) for item in members],
        *[copy.deepcopy(item["operations"]) for item in provisionals],
        *[
            copy.deepcopy(item["authenticated_readback"]["operations"])
            for item in provisionals
        ],
        *[
            [copy.deepcopy(item["authenticated_closure"]["operation"])]
            for item in provisionals
        ],
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-authenticated-registry-publication-payload",
        "source_sha": parent["source_sha"],
        "resolved_plan_sha256": contract["resolved_plan_sha256"],
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "wheel_sha256s": [item["wheel_sha256"] for item in members],
        "member_records": copy.deepcopy(members),
        "member_collection": member_collection_evidence,
        "parent_plans": copy.deepcopy(parent),
        "provisional_indexes": copy.deepcopy(provisionals),
        "provisional_collection": provisional_collection_evidence,
        "parent_plans_sha256": parent["plans_sha256"],
        "operation_audit": _publication_operation_audit(
            batches,
            staging_repository=contract["staging_repository"],
            public_targets={
                f"{item['target_repository']}:{item['target_tag']}"
                for item in contract["indexes"]
            },
        ),
        "publication": {
            "registry": "authenticated-passed",
            "anonymous": "pending",
            "github_release": "pending",
        },
    }
    return _envelope(payload, run)


def protected_registry_publication_evidence(
    *,
    member_records: object,
    member_collection: object,
    finalized_indexes: object,
    provisional_collection: object,
    parent_plans: object,
    source_sha: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build final deterministic evidence only after exact anonymous closure."""
    from . import registry

    parent, members, family_order, contract = _validated_publication_members(
        member_records=member_records,
        parent_plans=parent_plans,
        source_sha=source_sha,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if not isinstance(finalized_indexes, list):
        raise ValueError("protected finalizations must be an array")
    finalizations = [
        registry.validate_finalized_index(
            item,
            parent_plans=parent,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in finalized_indexes
    ]
    final_by_family = {
        item["provisional"]["family_task_id"]: item for item in finalizations
    }
    if len(finalizations) != len(family_order) or set(final_by_family) != set(
        family_order
    ):
        raise ValueError("protected publication differs from frozen family tasks")
    finalizations = [final_by_family[family_id] for family_id in family_order]
    provisionals = [item["provisional"] for item in finalizations]
    member_collection_evidence = _validate_member_artifact_collection(
        member_collection,
        members,
        source_sha=source_sha,
        contract=contract,
    )
    provisional_collection_evidence = _validate_provisional_artifact_collection(
        provisional_collection,
        provisionals,
        parent=parent,
        source_sha=source_sha,
        contract=contract,
    )
    indexes = [item["record"] for item in finalizations]
    registry_payload = {
        "status": "published",
        "candidate_task_sha256": sha256_value(
            [item["candidate_task_sha256"] for item in members]
        ),
        "publication_task_sha256": sha256_value(
            [item["publication_task_sha256"] for item in members]
        ),
        "member_records": members,
        "index_records": indexes,
    }
    if not isinstance(resolved_plan, dict):  # guarded by the contract above
        raise ValueError("protected release evidence requires its frozen plan")
    manifest = {
        "schema_version": 1,
        "kind": "ucm-protected-release-plan-manifest",
        "source_sha": source_sha,
        "resolved_plan_sha256": contract["resolved_plan_sha256"],
        "config_sha256": resolved_plan["config_sha256"],
        "wheel_tasks": [
            {key: task[key] for key in ("task_id", "task_sha256", "artifact_name")}
            for task in resolved_plan["wheel_tasks"]
        ],
        "image_tasks": [
            {
                key: task[key]
                for key in (
                    "task_id",
                    "task_sha256",
                    "family_task_id",
                    "artifact_name",
                )
            }
            for task in resolved_plan["image_tasks"]
        ],
        "family_tasks": [
            {
                key: task[key]
                for key in (
                    "task_id",
                    "task_sha256",
                    "image_task_ids",
                    "artifact_name",
                )
            }
            for task in resolved_plan["family_tasks"]
        ],
        "publication": {
            "registry": copy.deepcopy(registry_payload),
            "github_release": "pending",
        },
    }
    operation_batches: list[list[dict[str, Any]]] = [
        *[copy.deepcopy(item["operations"]) for item in members],
        *[copy.deepcopy(item["record"]["operations"]) for item in finalizations],
        *[
            copy.deepcopy(item["authenticated_readback"]["operations"])
            for item in finalizations
        ],
        *[
            [copy.deepcopy(item["provisional"]["authenticated_closure"]["operation"])]
            for item in finalizations
        ],
        *[
            copy.deepcopy(item["anonymous_readback"]["operations"])
            for item in finalizations
        ],
        *[
            [copy.deepcopy(item["anonymous_closure"]["operation"])]
            for item in finalizations
        ],
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-protected-registry-publication-payload",
        "source_sha": source_sha,
        "resolved_plan_sha256": contract["resolved_plan_sha256"],
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "wheel_sha256s": [item["wheel_sha256"] for item in members],
        "member_records": copy.deepcopy(members),
        "member_collection": member_collection_evidence,
        "members": [
            {
                "spec_id": item["spec_id"],
                "build_key_sha256": item["build_key_sha256"],
                "member_digest": item["member_digest"],
                "record_sha256": item["record_sha256"],
            }
            for item in members
        ],
        "indexes": [
            {
                "family_id": item["family_id"],
                "index_build_key_sha256": item["index_build_key_sha256"],
                "index_digest": item["index_digest"],
                "record_sha256": item["record_sha256"],
            }
            for item in indexes
        ],
        "index_records": copy.deepcopy(indexes),
        "parent_plans": copy.deepcopy(parent),
        "finalized_indexes": copy.deepcopy(finalizations),
        "provisional_collection": provisional_collection_evidence,
        "parent_plans_sha256": parent["plans_sha256"],
        "release_manifest_sha256": sha256_value(manifest),
        "operation_audit": _publication_operation_audit(
            operation_batches,
            staging_repository=contract["staging_repository"],
            public_targets={
                f"{item['target_repository']}:{item['target_tag']}"
                for item in contract["indexes"]
            },
        ),
        "publication": {
            "registry": "published",
            "anonymous": "passed",
            "github_release": "pending",
        },
    }
    return _envelope(payload, run)


def _envelope(payload: dict[str, Any], run: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "payload": payload,
        "payload_sha256": sha256_value(payload),
        "github": copy.deepcopy(run or {}),
    }


def _file_sha256(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"release artifact is not a regular file: {path}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    if raw != canonical_bytes(value) + b"\n":
        raise ValueError(f"{label} must use canonical JSON bytes")
    return value


def _require_plan_bound_hosted_task(
    task: object,
    *,
    task_kind: str,
    source_sha: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    from . import registry

    if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
        raise ValueError(f"hosted {task_kind} task must be an object")
    selected = registry.select_task(
        resolved_plan,
        task_kind=task_kind,
        task_id=task["task_id"],
        expected_plan_sha256=expected_plan_sha256,
    )
    if selected != task:
        raise ValueError(f"hosted {task_kind} task differs from the frozen plan")
    if resolved_plan["source"]["commit"] != source_sha:
        raise ValueError(f"hosted {task_kind} source differs from the frozen plan")
    return selected


def hosted_wheel_task(
    task: dict[str, Any],
    source_sha: str,
    source_date_epoch: int,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Materialize one frozen wheel task into Docker build inputs."""
    source_sha = _source_sha(source_sha)
    task = _require_plan_bound_hosted_task(
        task,
        task_kind="wheel",
        source_sha=source_sha,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or not 315532800 <= source_date_epoch <= 4354819199
    ):
        raise ValueError("hosted source date epoch is outside the ZIP timestamp range")
    if not isinstance(task, dict):
        raise ValueError("hosted wheel task must be an object")
    task_payload = {key: value for key, value in task.items() if key != "task_sha256"}
    if re.fullmatch(
        r"wheel-[0-9a-f]{64}", str(task.get("task_id"))
    ) is None or task.get("task_sha256") != sha256_value(task_payload):
        raise ValueError("hosted wheel task identity is invalid")
    build = task.get("build")
    dependency_lock = task.get("dependency_lock")
    if (
        not isinstance(build, dict)
        or set(build) != {"docker_target", "platform_arg"}
        or not isinstance(dependency_lock, dict)
        or set(dependency_lock) != {"build_tools", "runtime_dependencies"}
        or task.get("dependency_lock_sha256") != sha256_value(dependency_lock)
    ):
        raise ValueError("hosted wheel task build authority is invalid")
    build_tools = dependency_lock["build_tools"]
    if (
        not isinstance(build_tools, list)
        or not build_tools
        or any(not isinstance(record, dict) for record in build_tools)
    ):
        raise ValueError("hosted wheel build tool authority is invalid")
    profile_id = task["profile_id"]
    root = task["builder"]["root"]
    build_args = {
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "UCM_BUILDER_IMAGE": f"{root['repository']}@{root['manifest_digest']}",
        "PLATFORM": build["platform_arg"],
        "UCM_RELEASE_TASK_ID": task["task_id"],
        "UCM_RELEASE_SPEC_ID": task["spec_id"],
        "UCM_RELEASE_PROFILE": profile_id,
        "UCM_RELEASE_SOURCE_SHA": source_sha,
        "UCM_RELEASE_VERSION": task["wheel_version"],
        "UCM_RELEASE_BUILD_KEY": task["task_sha256"],
        "UCM_RELEASE_PYTHON_VERSION": task["python_version"],
        "UCM_RELEASE_PYTHON_ABI": task["python_abi"],
        "UCM_RELEASE_WHEEL_PLATFORM": task["wheel_platform"],
        "UCM_RELEASE_BUILD_SETTINGS": canonical_bytes(build).decode("utf-8"),
        "UCM_RUNTIME_PATCH_MANIFEST_SHA256": task["runtime_patch_manifest_sha256"],
        "UCM_RELEASE_REQUIRED_TARGETS": ",".join(task["required_native"]),
        "UCM_RELEASE_FORBIDDEN_TARGETS": ",".join(task["forbidden_native"]),
    }
    build_tool_lock = "".join(
        f"{record['name']} @ file:///wheelhouse/{record['filename']} "
        f"--hash={record['sha256']}\n"
        for record in build_tools
    )
    result = {
        "task_id": task["task_id"],
        "spec_id": task["spec_id"],
        "profile_id": profile_id,
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "runner": task["runner"],
        "task_sha256": task["task_sha256"],
        "builder_coordinate": build_args["UCM_BUILDER_IMAGE"],
        "docker_target": build["docker_target"],
        "source_sha": source_sha,
        "source_date_epoch": source_date_epoch,
        "wheel_artifact": task["artifact_name"],
        "build_tools": copy.deepcopy(build_tools),
        "build_tool_lock": build_tool_lock,
        "build_tool_lock_sha256": "sha256:"
        + hashlib.sha256(build_tool_lock.encode()).hexdigest(),
        "build_args": dict(sorted(build_args.items())),
    }
    result["hosted_task_sha256"] = sha256_value(result)
    return result


def hosted_image_task(
    task: dict[str, Any],
    source_sha: str,
    source_date_epoch: int,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Materialize one frozen image task's artifact and dependency bindings."""
    source_sha = _source_sha(source_sha)
    task = _require_plan_bound_hosted_task(
        task,
        task_kind="image",
        source_sha=source_sha,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or not 315532800 <= source_date_epoch <= 4354819199
        or not isinstance(task, dict)
    ):
        raise ValueError("hosted image task input is invalid")
    payload = {key: value for key, value in task.items() if key != "task_sha256"}
    if (
        re.fullmatch(r"image-[0-9a-f]{64}", str(task.get("task_id"))) is None
        or task.get("task_sha256") != sha256_value(payload)
        or re.fullmatch(r"wheel-[0-9a-f]{64}", str(task.get("wheel_task_id"))) is None
    ):
        raise ValueError("hosted image task identity is invalid")
    result = {
        "task_id": task["task_id"],
        "spec_id": task["spec_id"],
        "profile_id": task["profile_id"],
        "family_task_id": task["family_task_id"],
        "wheel_task_id": task["wheel_task_id"],
        "runner": task["runner"],
        "source_sha": source_sha,
        "source_date_epoch": source_date_epoch,
        "task_sha256": task["task_sha256"],
        "runtime_patch_variants": copy.deepcopy(task["runtime_patch_variants"]),
        "wheel_artifact": task["wheel_artifact_name"],
        "image_artifact": task["artifact_name"],
        "build_args": {
            "UCM_RUNTIME_PATCH_VARIANTS": canonical_bytes(
                task["runtime_patch_variants"]
            ).decode("utf-8"),
        },
    }
    result["hosted_task_sha256"] = sha256_value(result)
    return result


def build_real_family_plans(
    image_results: list[dict[str, Any]],
    *,
    source_sha: str,
    resolved_plan: dict[str, Any],
    selected_image_task_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create the frozen plan's exact unpublished family index set."""
    source_sha = _source_sha(source_sha)
    validate_resolved_plan(resolved_plan)
    if resolved_plan["source"]["commit"] != source_sha:
        raise ValueError("resolved plan source differs from real image results")
    if not isinstance(image_results, list):
        raise ValueError("real candidate image results must be a list")
    all_expected = {task["task_id"]: task for task in resolved_plan["image_tasks"]}
    selected_ids = (
        list(all_expected)
        if selected_image_task_ids is None
        else selected_image_task_ids
    )
    if (
        not isinstance(selected_ids, list)
        or not selected_ids
        or any(not isinstance(task_id, str) for task_id in selected_ids)
        or len(selected_ids) != len(set(selected_ids))
        or not set(selected_ids).issubset(all_expected)
    ):
        raise ValueError("selected real image task set is invalid")
    expected = {task_id: all_expected[task_id] for task_id in selected_ids}
    observed: dict[str, dict[str, Any]] = {}
    for result in image_results:
        if not isinstance(result, dict):
            raise ValueError("real image result must be an object")
        matches = [
            task
            for task in expected.values()
            if task["task_sha256"] == result.get("task_key")
        ]
        if len(matches) != 1:
            raise ValueError("real image results contain an unknown or duplicate task")
        task = matches[0]
        task_id = task["task_id"]
        if task_id in observed:
            raise ValueError("real image results contain an unknown or duplicate task")
        source = result.get("source")
        oci = result.get("oci")
        required_identity = (
            result.get("candidate_kind") == "real-candidate"
            and result.get("fixture_only") is False
            and result.get("unpublished") is True
            and result.get("publication_attempted") is False
            and result.get("status") == "real-verified-unpublished"
            and result.get("family_id") == task["family_task_id"]
            and result.get("profile_id") == task["profile_id"]
            and result.get("target_platform") == task["platform"]
            and result.get("target_repository") == task["target_repository"]
            and result.get("target_tag") == task["target_tag"]
            and result.get("task_key") == task["task_sha256"]
            and isinstance(source, dict)
            and source.get("commit") == source_sha
            and isinstance(oci, dict)
            and oci.get("platform") == task["platform"]
            and oci.get("published") is False
        )
        if not required_identity:
            raise ValueError(f"real image result differs from resolved task: {task_id}")
        for field in (
            "build_key_sha256",
            "result_sha256",
            "content_identity_sha256",
        ):
            if DIGEST_RE.fullmatch(str(result.get(field))) is None:
                raise ValueError(f"real image result {field} is invalid")
        if DIGEST_RE.fullmatch(str(oci.get("digest"))) is None:
            raise ValueError("real image OCI digest is invalid")
        observed[task_id] = result
    if set(observed) != set(expected):
        raise ValueError("real image results do not match the exact resolved task set")

    families: list[dict[str, Any]] = []
    for family in resolved_plan["family_tasks"]:
        if not set(family["image_task_ids"]).issubset(expected):
            continue
        family_tasks_by_id = {
            task_id: expected[task_id] for task_id in family["image_task_ids"]
        }
        family_tasks = sorted(
            family_tasks_by_id.values(), key=lambda item: item["platform"]
        )
        members = [
            {
                "platform": task["platform"],
                "spec_id": task["spec_id"],
                "task_id": task["task_id"],
                "task_sha256": task["task_sha256"],
                "manifest_digest": observed[task["task_id"]]["oci"]["digest"],
                "build_key_sha256": observed[task["task_id"]]["build_key_sha256"],
                "content_identity_sha256": observed[task["task_id"]][
                    "content_identity_sha256"
                ],
                "image_result_sha256": observed[task["task_id"]]["result_sha256"],
            }
            for task in family_tasks
        ]
        family_payload = {
            "schema_version": 1,
            "kind": "ucm-real-candidate-index-plan",
            "family_task_id": family["task_id"],
            "family_task_sha256": family["task_sha256"],
            "target_repository": family["target_repository"],
            "target_tag": family["target_tag"],
            "members": members,
            "unpublished": True,
            "publication_attempted": False,
        }
        families.append({**family_payload, "plan_sha256": sha256_value(family_payload)})
    inventory_payload = {
        "schema_version": 1,
        "kind": "ucm-real-candidate-inventory",
        "families": copy.deepcopy(families),
    }
    return {
        "families": families,
        "candidate_inventory": {
            **inventory_payload,
            "inventory_sha256": sha256_value(inventory_payload),
        },
        "second_reconcile": {
            "decision": "already-present",
            "task_count": 0,
            "tasks": [],
        },
    }


def select_hosted_task_projection(
    resolved_plan: dict[str, Any],
    *,
    wheel_matrix: object,
    image_matrix: object,
) -> dict[str, Any]:
    """Select one exact dependency-closed hosted subset from a frozen plan."""
    validate_resolved_plan(resolved_plan)
    expected_wheels = {
        item["task_id"]: item
        for item in resolved_plan["github_wheel_matrix"]["include"]
    }
    expected_images = {
        item["task_id"]: item
        for item in resolved_plan["github_image_matrix"]["include"]
    }

    def selected_ids(
        matrix: object, expected: dict[str, dict[str, Any]], label: str
    ) -> list[str]:
        if (
            not isinstance(matrix, dict)
            or set(matrix) != {"include"}
            or not isinstance(matrix["include"], list)
            or not matrix["include"]
        ):
            raise ValueError(
                f"selected {label} matrix must contain a non-empty include"
            )
        task_ids: list[str] = []
        for item in matrix["include"]:
            if not isinstance(item, dict) or item != expected.get(item.get("task_id")):
                raise ValueError(f"selected {label} matrix differs from frozen plan")
            task_ids.append(item["task_id"])
        if len(task_ids) != len(set(task_ids)):
            raise ValueError(f"selected {label} matrix contains duplicate tasks")
        return task_ids

    wheel_ids = selected_ids(wheel_matrix, expected_wheels, "wheel")
    image_ids = selected_ids(image_matrix, expected_images, "image")
    wheel_by_id = {task["task_id"]: task for task in resolved_plan["wheel_tasks"]}
    image_by_id = {task["task_id"]: task for task in resolved_plan["image_tasks"]}
    dependency_ids = {image_by_id[task_id]["wheel_task_id"] for task_id in image_ids}
    if set(wheel_ids) != dependency_ids:
        raise ValueError("selected image tasks and wheel dependencies differ")
    selected_image_ids = set(image_ids)
    family_tasks = [
        task
        for task in resolved_plan["family_tasks"]
        if set(task["image_task_ids"]).issubset(selected_image_ids)
    ]
    payload = {
        "wheel_task_ids": wheel_ids,
        "image_task_ids": image_ids,
        "family_task_ids": [task["task_id"] for task in family_tasks],
    }
    return {
        "wheel_tasks": [wheel_by_id[task_id] for task_id in wheel_ids],
        "image_tasks": [image_by_id[task_id] for task_id in image_ids],
        "family_tasks": family_tasks,
        "projection_sha256": sha256_value(payload),
    }


def _artifact_directory(root: Path, artifact_name: str, label: str) -> Path:
    root = Path(root)
    direct = root / artifact_name
    matches = [direct] if direct.is_dir() else []
    if root.is_dir() and root.name == artifact_name:
        matches.append(root)
    unique = sorted(set(matches))
    if len(unique) != 1 or any(path.is_symlink() for path in unique):
        raise ValueError(f"{label} artifact directory is missing or ambiguous")
    return unique[0]


def _one_file(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(
        path
        for path in Path(directory).glob(pattern)
        if path.is_file() and not path.is_symlink()
    )
    if len(matches) != 1:
        raise ValueError(f"{label} requires exactly one file matching {pattern}")
    return matches[0]


def _real_chart_summary(
    result_path: Path,
    package_path: Path,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    result = _load_canonical_json(result_path, "real hosted Chart result")
    with tempfile.TemporaryDirectory() as temporary:
        expected_dir = Path(temporary) / "chart"
        expected = chart.package_chart(
            expected_dir,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        expected_package = expected_dir / expected["filename"]
        if result != expected:
            raise ValueError("real hosted Chart result differs from fresh packaging")
        if (
            not Path(package_path).is_file()
            or Path(package_path).is_symlink()
            or Path(package_path).name != expected["filename"]
            or Path(package_path).read_bytes() != expected_package.read_bytes()
        ):
            raise ValueError("real hosted Chart package differs from fresh packaging")
    return {
        "filename": result["filename"],
        "sha256": result["sha256"],
        "release_tree_sha256": result["release_tree_sha256"],
        "rendered_cases": copy.deepcopy(result["rendered_cases"]),
        "status": result["status"],
    }


def aggregate_real_hosted_evidence(
    *,
    wheel_dir: Path,
    image_dir: Path,
    source_sha: str,
    repository: str,
    ref: str,
    chart_result_path: Path | None = None,
    chart_package_path: Path | None = None,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    wheel_matrix: object,
    image_matrix: object,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reopen the frozen plan's exact wheels/images and derive feature evidence."""
    source_sha = _source_sha(source_sha)
    validate_resolved_plan(resolved_plan)
    if (
        resolved_plan["source"]["commit"] != source_sha
        or resolved_plan["resolved_plan_sha256"] != expected_plan_sha256
    ):
        raise ValueError("real hosted aggregation differs from the frozen plan")
    if (chart_result_path is None) != (chart_package_path is None):
        raise ValueError(
            "real hosted Chart result and package must be supplied together"
        )
    wheel_root = Path(wheel_dir)
    image_root = Path(image_dir)
    if not wheel_root.is_dir() or not image_root.is_dir():
        raise ValueError("real hosted wheel and image artifact roots must exist")

    projection = select_hosted_task_projection(
        resolved_plan,
        wheel_matrix=wheel_matrix,
        image_matrix=image_matrix,
    )
    wheel_tasks = projection["wheel_tasks"]
    image_tasks = projection["image_tasks"]
    wheel_logical_names = [task["artifact_name"] for task in wheel_tasks]
    wheel_artifacts = resolve_run_bound_artifact_directories(
        wheel_root, wheel_logical_names, run=run, label="real hosted wheel"
    )
    task_records: dict[str, dict[str, Any]] = {}
    for planned_task, logical_name in zip(
        wheel_tasks, wheel_logical_names, strict=True
    ):
        task_path = wheel_artifacts[logical_name] / "hosted-task.json"
        task = _load_canonical_json(task_path, "real hosted task record")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or task_id in task_records:
            raise ValueError("real hosted task records are duplicated or malformed")
        task_records[task_id] = task
    epochs = {task.get("source_date_epoch") for task in task_records.values()}
    if len(epochs) != 1:
        raise ValueError("real hosted tasks disagree on source date epoch")
    source_date_epoch = next(iter(epochs))
    expected_tasks = {
        item["task_id"]: hosted_wheel_task(
            item,
            source_sha,
            source_date_epoch,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in wheel_tasks
    }
    if task_records != expected_tasks:
        raise ValueError("real hosted task records differ from the frozen plan")

    image_artifacts = resolve_run_bound_artifact_directories(
        image_root,
        [item["artifact_name"] for item in image_tasks],
        run=run,
        label="real hosted image",
    )
    wheel_summaries: list[dict[str, Any]] = []
    image_results: list[dict[str, Any]] = []
    image_summaries: list[dict[str, Any]] = []
    for planned_task in wheel_tasks:
        task_id = planned_task["task_id"]
        spec_id = planned_task["spec_id"]
        task = expected_tasks[task_id]
        wheel_artifact = wheel_artifacts[task["wheel_artifact"]]
        task_path = wheel_artifact / "hosted-task.json"
        if _load_canonical_json(task_path, f"{spec_id} hosted wheel task") != task:
            raise ValueError(f"{spec_id} wheel artifact task record differs")
        wheel_path = _one_file(wheel_artifact, "*.whl", f"{spec_id} wheel")
        inspection_path = wheel_artifact / "wheel-inspection.json"
        seal_path = wheel_artifact / "wheel-seal.json"
        source_context_path = wheel_artifact / "source-context.json"
        inspection = _load_canonical_json(
            inspection_path, f"{spec_id} wheel inspection"
        )
        seal = _load_canonical_json(seal_path, f"{spec_id} wheel seal")
        source_context = _load_canonical_json(
            source_context_path, f"{spec_id} source context"
        )
        wheel_sha256 = _file_sha256(wheel_path)
        reopened = wheel.inspect_wheel(
            wheel_path,
            spec_id,
            wheel_sha256,
            "builder-candidate",
            task=planned_task,
        )
        builder = reopened.get("builder_evidence")
        if (
            inspection != reopened
            or seal.get("source_kind") != "builder-candidate"
            or seal.get("publication_status") != "unpublished"
            or seal.get("publication_eligible") is not False
            or seal.get("spec_id") != spec_id
            or seal.get("source_sha") != source_sha
            or seal.get("build_key") != task["task_sha256"]
            or seal.get("wheel_sha256") != wheel_sha256
            or seal.get("inspection_sha256") != _file_sha256(inspection_path)
            or seal.get("runtime_patch_manifest_sha256")
            != planned_task["runtime_patch_manifest_sha256"]
            or not isinstance(builder, dict)
            or builder.get("source_commit") != source_sha
            or builder.get("build_key") != task["task_sha256"]
            or builder.get("source_date_epoch") != source_date_epoch
            or builder.get("runtime_patch_manifest_sha256")
            != planned_task["runtime_patch_manifest_sha256"]
            or source_context.get("source_sha") != source_sha
            or source_context.get("build_context_sha256")
            != builder.get("build_context_digest")
        ):
            raise ValueError(f"{spec_id} real wheel closure does not reopen")
        wheel_summaries.append(
            {
                "spec_id": spec_id,
                "task_id": task_id,
                "task_sha256": task["task_sha256"],
                "hosted_task_sha256": task["hosted_task_sha256"],
                "artifact": task["wheel_artifact"],
                "filename": wheel_path.name,
                "wheel_sha256": wheel_sha256,
                "wheel_size": wheel_path.stat().st_size,
                "inspection_sha256": _file_sha256(inspection_path),
                "runtime_patch_manifest_sha256": planned_task[
                    "runtime_patch_manifest_sha256"
                ],
                "source_tree": source_context.get("source_tree"),
                "source_context_sha256": source_context.get("build_context_sha256"),
            }
        )

    wheel_summary_by_task = {item["task_id"]: item for item in wheel_summaries}
    for planned_image in image_tasks:
        task_id = planned_image["task_id"]
        spec_id = planned_image["spec_id"]
        task = hosted_image_task(
            planned_image,
            source_sha,
            source_date_epoch,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        wheel_summary = wheel_summary_by_task[planned_image["wheel_task_id"]]
        wheel_sha256 = wheel_summary["wheel_sha256"]
        image_artifact = image_artifacts[task["image_artifact"]]
        if (
            _load_canonical_json(
                image_artifact / "hosted-task.json", f"{spec_id} hosted image task"
            )
            != task
        ):
            raise ValueError(f"{spec_id} image artifact task record differs")
        result_path = image_artifact / "image-result.json"
        recipe_path = image_artifact / "image-recipe.json"
        result = image.validate_image_result(
            _load_canonical_json(result_path, f"{spec_id} image result"),
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
            task_id=task_id,
        )
        recipe = _load_canonical_json(recipe_path, f"{spec_id} image recipe")
        compact = image.validate_real_compact_oci_evidence(
            image_artifact / "oci-evidence",
            image_result=result,
            recipe=recipe,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
            task_id=task_id,
        )
        if (
            result.get("spec_id") != spec_id
            or result.get("task_key") != task["task_sha256"]
            or result.get("source", {}).get("commit") != source_sha
            or result.get("wheel", {}).get("sha256") != wheel_sha256
            or compact.get("manifest_digest") != result.get("oci", {}).get("digest")
        ):
            raise ValueError(f"{spec_id} image result differs from wheel/task closure")
        image_results.append(result)
        image_summaries.append(
            {
                "spec_id": spec_id,
                "task_id": task_id,
                "task_sha256": task["task_sha256"],
                "artifact": task["image_artifact"],
                "manifest_digest": result["oci"]["digest"],
                "build_key_sha256": result["build_key_sha256"],
                "content_identity_sha256": result["content_identity_sha256"],
                "image_result_sha256": result["result_sha256"],
                "image_result_file_sha256": _file_sha256(result_path),
                "recipe_sha256": result["recipe_sha256"],
                "compact_closure_sha256": compact["closure_sha256"],
            }
        )

    planned = build_real_family_plans(
        image_results,
        source_sha=source_sha,
        resolved_plan=resolved_plan,
        selected_image_task_ids=[task["task_id"] for task in image_tasks],
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ucm-real-hosted-image-loop-payload",
        "mode": "feature-candidate",
        "repository": repository,
        "ref": ref,
        "source_sha": source_sha,
        "source_date_epoch": source_date_epoch,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
        "task_projection_sha256": projection["projection_sha256"],
        "selected_wheel_task_ids": [task["task_id"] for task in wheel_tasks],
        "selected_image_task_ids": [task["task_id"] for task in image_tasks],
        "wheels": wheel_summaries,
        "images": image_summaries,
        "families": planned["families"],
        "candidate_inventory": planned["candidate_inventory"],
        "second_reconcile": planned["second_reconcile"],
        "publication": {"status": "blocked", "attempted": False},
    }
    if chart_result_path is not None and chart_package_path is not None:
        payload["kind"] = "ucm-real-hosted-release-loop-payload"
        payload["chart"] = _real_chart_summary(
            chart_result_path,
            chart_package_path,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
    return _envelope(payload, run)


def prepare_candidate_loop(
    build_record: dict[str, Any],
    wheel_record: dict[str, Any],
    *,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the actual fixture candidate, first task, and six-scenario proof."""
    source_sha = _source_sha(source_sha)
    required_build = {
        "schema_version",
        "kind",
        "fixture_only",
        "publication_status",
        "publication_eligible",
        "source_sha",
        "profile_id",
        "wheel_sha256",
        "inspection_sha256",
    }
    if not isinstance(build_record, dict) or set(build_record) != required_build:
        raise ValueError("fixture wheel build record fields are noncanonical")
    if (
        build_record["schema_version"] != 1
        or build_record["kind"] != "ucm-fixture-wheel-build"
        or build_record["fixture_only"] is not True
        or build_record["publication_status"] != "unpublished"
        or build_record["publication_eligible"] is not False
        or build_record["source_sha"] != source_sha
    ):
        raise ValueError("fixture wheel build record does not bind the source")
    inspection_sha256 = (
        "sha256:" + hashlib.sha256(canonical_bytes(wheel_record) + b"\n").hexdigest()
    )
    if (
        build_record["wheel_sha256"] != wheel_record.get("sha256")
        or build_record["inspection_sha256"] != inspection_sha256
        or build_record["profile_id"] != wheel_record.get("spec_id")
        or wheel_record.get("fixture_binding")
        != {
            "source_commit": source_sha,
            "profile_id": build_record["profile_id"],
            "marker_status": "passed",
        }
        or wheel_record.get("status") != "fixture-only"
        or wheel_record.get("trust_level") != "fixture-only"
        or wheel_record.get("published") is not False
        or wheel_record.get("publication_eligible") is not False
    ):
        raise ValueError("fixture wheel inspection does not match its build record")

    manifest = _build_fixture_release_manifest()
    catalog = load_catalog()
    snapshot = {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": "docker.io/vllm/vllm-openai",
        "upstream_tag": "v0.10.2",
        "index_digest": "sha256:" + "1" * 64,
        "platforms": [
            {
                "os": "linux",
                "architecture": "amd64",
                "manifest_digest": "sha256:" + "2" * 64,
                "config_digest": "sha256:" + "3" * 64,
            },
            {
                "os": "linux",
                "architecture": "arm64",
                "manifest_digest": "sha256:" + "4" * 64,
                "config_digest": "sha256:" + "5" * 64,
            },
        ],
    }
    source_case = {
        "release_manifest": manifest,
        "wheel_records": [copy.deepcopy(wheel_record)],
        "spec_id": wheel_record["spec_id"],
        "upstream_snapshot": snapshot,
        "catalog": catalog,
        "compatibility_rule_id": "cuda-supported",
        "implementation_digest": image.implementation_digests()["aggregate_sha256"],
    }
    candidate = build_fixture_candidate(**source_case, fixture_mode=True)
    inventory = _fixture_inventory()
    first = reconcile_fixture_candidate(candidate, inventory)
    if first["task_count"] != 1 or first["tasks"][0]["revision"] != 1:
        raise ValueError("new fixture input must schedule exactly one r1 task")
    loop = verify_loop(source_case, run=run)
    scenarios = loop["payload"]["scenarios"]
    if (
        [item["name"] for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item["passed"] is True for item in scenarios)
        or loop["payload"]["publication_attempted"] is not False
    ):
        raise ValueError("fixture loop did not pass all deterministic scenarios")
    return {
        "source_sha": source_sha,
        "source_case": source_case,
        "candidate": candidate,
        "inventory": inventory,
        "first_reconcile": first,
        "image_input": {
            "source_case": source_case,
            "candidate": candidate,
            "task": first["tasks"][0],
            "inventory": inventory,
            "target_platform": "linux/amd64",
        },
        "loop_verification": loop,
    }


def complete_candidate_loop(
    prepared: dict[str, Any],
    image_result: dict[str, Any],
    *,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize a verified local result and require the second fixture reconcile to be zero."""
    source_sha = _source_sha(source_sha)
    required_prepared = {
        "source_sha",
        "source_case",
        "candidate",
        "inventory",
        "first_reconcile",
        "image_input",
        "loop_verification",
    }
    if not isinstance(prepared, dict) or set(prepared) != required_prepared:
        raise ValueError("prepared loop fields are noncanonical")
    if prepared["source_sha"] != source_sha:
        raise ValueError("prepared loop source SHA mismatch")
    candidate = prepared["candidate"]
    first = prepared["first_reconcile"]
    image_input = prepared["image_input"]
    if (
        image_input.get("candidate") != candidate
        or image_input.get("inventory") != prepared["inventory"]
        or first.get("tasks") != [image_input.get("task")]
    ):
        raise ValueError(
            "prepared image input is not the exact first fixture reconcile task"
        )
    if not isinstance(image_result, dict):
        raise ValueError("image result must be an object")
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    image_result = image.validate_image_result(image_result)
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    build_inputs = candidate["build_inputs"]
    wheel_input = build_inputs["wheel"]
    wheel_records = prepared["source_case"].get("wheel_records")
    if not isinstance(wheel_records, list) or len(wheel_records) != 1:
        raise ValueError("prepared loop must retain one exact wheel inspection")
    wheel_record = wheel_records[0]
    expected_wheel = {
        "filename": wheel_record["filename"],
        "sha256": wheel_record["sha256"],
        "size": wheel_record["size"],
        "spec_id": wheel_input["spec_id"],
        "declaration_sha256": wheel_input["declaration_sha256"],
        "version": wheel_input["version"],
        "python_abi": wheel_input["python_abi"],
        "cpu_arch": wheel_input["cpu_arch"],
        "accelerator": wheel_input["accelerator"],
        "accelerator_runtime": wheel_input["accelerator_runtime"],
        "npu_arch_or_na": wheel_input["npu_arch_or_na"],
        "os": wheel_input["os"],
        "binary_profile_id": wheel_input["binary_profile_id"],
        "requires_dist": python_runtime_requirements(
            prepared["source_case"]["catalog"]
        ),
    }
    target_platform = image_input["target_platform"]
    target_architecture = target_platform.split("/", 1)[1]
    upstream = build_inputs["upstream"]
    upstream_platforms = [
        item
        for item in upstream["platforms"]
        if item["architecture"] == target_architecture
    ]
    if len(upstream_platforms) != 1:
        raise ValueError("candidate does not have one exact target platform")
    upstream_platform = upstream_platforms[0]
    manifest = prepared["source_case"]["release_manifest"]
    expected_source = {
        "release_manifest_sha256": build_inputs["release_manifest_sha256"],
        "config_sha256": manifest["config_sha256"],
        "upstream_repository": upstream["repository"],
        "upstream_index_digest": upstream["index_digest"],
        "upstream_platform_manifest_digest": upstream_platform["manifest_digest"],
        "upstream_platform_config_digest": upstream_platform["config_digest"],
    }
    if (
        image_result["build_key_sha256"] != candidate["build_key_sha256"]
        or image_result["task_key"] != sha256_value(image_input["task"])
        or image_result["ucm_version"] != candidate["ucm_version"]
        or image_result["target_platform"] != target_platform
        or image_result["wheel"] != expected_wheel
        or image_result["source"] != expected_source
        or image_result["implementation"]["aggregate_sha256"]
        != build_inputs["implementation_digest"]
    ):
        raise ValueError("image result does not bind the exact candidate input closure")
    gates = image_result.get("gates")
    if (
        not isinstance(gates, dict)
        or set(gates) != REQUIRED_IMAGE_GATES
        or any(value != "passed" for value in gates.values())
    ):
        raise ValueError("image result required gates did not all pass")
    if (
        image_result.get("runtime_validation") != "external-required"
        or image_result.get("device_validation") != "external-required"
    ):
        raise ValueError(
            "fixture runtime and device validation must remain external-required"
        )
    oci_digest = image_result.get("oci", {}).get("digest")
    if not isinstance(oci_digest, str) or DIGEST_RE.fullmatch(oci_digest) is None:
        raise ValueError("image result OCI digest is invalid")

    task = image_input["task"]
    entry = {
        "repository": candidate["target_repository"],
        "tag": task["tag"],
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": oci_digest,
        "evidence_digest": oci_digest,
    }
    inventory = _fixture_inventory([entry])
    second = reconcile_fixture_candidate(candidate, inventory)
    if second["task_count"] != 0 or second["decision"] != "already-present":
        raise ValueError("completed fixture candidate did not reconcile to zero")

    accepted = {
        "a2": parse_fixture_upstream_tag("vllm-ascend", "v0.10.2")["npu_arch"],
        "a3": parse_fixture_upstream_tag("vllm-ascend", "v0.10.2-a3")["npu_arch"],
    }
    rejected: list[str] = []
    for suffix in ("310p", "a5"):
        try:
            parse_fixture_upstream_tag("vllm-ascend", f"v0.10.2-{suffix}")
        except ValueError:
            rejected.append(suffix)
    if accepted != {"a2": "a2", "a3": "a3"} or rejected != ["310p", "a5"]:
        raise ValueError("Ascend compatibility boundary is not A2/A3 only")
    loop = prepared["loop_verification"]
    if not isinstance(loop, dict) or set(loop) != {
        "schema_version",
        "kind",
        "run",
        "payload",
        "payload_sha256",
    }:
        raise ValueError("prepared loop verification envelope is noncanonical")
    recomputed_loop = verify_loop(prepared["source_case"], run=loop["run"])
    if loop != recomputed_loop:
        raise ValueError("prepared loop verification does not match recomputation")
    scenarios = loop.get("payload", {}).get("scenarios", [])
    if (
        [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item.get("passed") is True for item in scenarios)
        or loop.get("payload", {}).get("must_green") is not True
    ):
        raise ValueError("prepared deterministic scenario evidence is incomplete")
    source_batches = loop["payload"].get("operation_batches")
    if not isinstance(source_batches, list):
        raise ValueError("prepared loop is missing its operation ledger")
    if audit_operation_batches(source_batches) != loop["payload"].get(
        "zero_write_audit"
    ):
        raise ValueError("prepared zero-write audit does not match its ledger")
    operation_batches = copy.deepcopy(source_batches) + [
        copy.deepcopy(second["operations"])
    ]
    operation_audit = audit_operation_batches(operation_batches)
    if operation_audit["write_count"] != 0:
        raise ValueError("completed candidate attempted a write")
    write_audit = {
        **operation_audit,
        "ledger_sha256": sha256_value(operation_batches),
    }
    publication_attempted = (
        image_result["publication_attempted"] or write_audit["write_count"] != 0
    )
    if publication_attempted or image_result["unpublished"] is not True:
        raise ValueError("completed fixture candidate must remain unpublished")
    payload = {
        "schema_version": 1,
        "kind": "ucm-vllm-candidate-loop-payload",
        "source_sha": source_sha,
        "candidate_identity": {
            "repository": candidate["target_repository"],
            "tag": task["tag"],
            "build_key_sha256": candidate["build_key_sha256"],
        },
        "upstream_index_digest": candidate["build_inputs"]["upstream"]["index_digest"],
        "first_reconcile_sha256": sha256_value(first),
        "second_reconcile_sha256": sha256_value(second),
        "first_task_count": first["task_count"],
        "second_task_count": second["task_count"],
        "image_result_sha256": image_result["result_sha256"],
        "oci_digest": oci_digest,
        "loop_payload_sha256": loop["payload_sha256"],
        "scenarios": copy.deepcopy(scenarios),
        "compatibility": {"accepted": ["a2", "a3"], "rejected": ["310p", "a5"]},
        "required_gates": copy.deepcopy(gates),
        "runtime_validation": image_result["runtime_validation"],
        "device_validation": image_result["device_validation"],
        "expected_blocked": copy.deepcopy(loop["payload"]["expected_blockers"]),
        "publication": {
            "status": "blocked" if image_result["unpublished"] else "invalid",
            "attempted": publication_attempted,
        },
        "operation_batches": operation_batches,
        "write_audit": write_audit,
    }
    return {"second_reconcile": second, "evidence": _envelope(payload, run)}


def aggregate_release_evidence(
    *,
    build_record_path: Path,
    wheel_record_path: Path,
    wheel_path: Path,
    chart_result_path: Path,
    chart_package_path: Path,
    image_result_path: Path,
    oci_evidence_dir: Path,
    image_recipe_path: Path,
    image_metadata_path: Path,
    image_prepare_path: Path,
    buildkit_metadata_path: Path,
    image_archive_sha256_path: Path,
    completed_loop_path: Path,
    second_reconcile_path: Path,
    image_loop_path: Path,
    repository: str,
    ref: str,
    source_sha: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reopen every release artifact and recompute the exact candidate closure."""
    source_sha = _source_sha(source_sha)
    validate_resolved_plan(resolved_plan)
    if (
        resolved_plan["source"]["commit"] != source_sha
        or resolved_plan["resolved_plan_sha256"] != expected_plan_sha256
    ):
        raise ValueError("fixture aggregation differs from the frozen resolved plan")
    build_record_path = Path(build_record_path)
    wheel_record_path = Path(wheel_record_path)
    wheel_path = Path(wheel_path)
    chart_result_path = Path(chart_result_path)
    chart_package_path = Path(chart_package_path)
    image_result_path = Path(image_result_path)
    oci_evidence_dir = Path(oci_evidence_dir)
    image_recipe_path = Path(image_recipe_path)
    image_metadata_path = Path(image_metadata_path)
    image_prepare_path = Path(image_prepare_path)
    buildkit_metadata_path = Path(buildkit_metadata_path)
    image_archive_sha256_path = Path(image_archive_sha256_path)
    completed_loop_path = Path(completed_loop_path)
    second_reconcile_path = Path(second_reconcile_path)
    image_loop_path = Path(image_loop_path)

    build_record = _load_canonical_json(build_record_path, "wheel build record")
    wheel_record = _load_canonical_json(wheel_record_path, "wheel inspection")
    actual_wheel_sha256 = _file_sha256(wheel_path)
    with tempfile.TemporaryDirectory() as temporary:
        expected_fixture = wheel.build_fixture_wheel(
            Path(temporary) / "wheel",
            source_sha,
            build_record.get("profile_id"),
        )
        expected_wheel_path = Path(expected_fixture["wheel_path"])
        if (
            wheel_path.read_bytes() != expected_wheel_path.read_bytes()
            or wheel_record != expected_fixture["inspection"]
            or build_record != expected_fixture["build_record"]
        ):
            raise ValueError(
                "actual fixture wheel/build/inspection differs from authoritative rebuild"
            )
    if wheel_path.name != wheel_record.get("filename"):
        raise ValueError("wheel filename does not match its inspection")
    inspected = wheel.inspect_wheel(
        wheel_path,
        build_record.get("profile_id"),
        actual_wheel_sha256,
        "fixture",
    )
    if inspected != wheel_record:
        raise ValueError("actual wheel does not match its canonical inspection")
    if build_record.get("inspection_sha256") != _file_sha256(wheel_record_path):
        raise ValueError("wheel build record does not bind inspection bytes")
    prepared = prepare_candidate_loop(
        build_record,
        wheel_record,
        source_sha=source_sha,
        run={},
    )

    chart_result = _load_canonical_json(chart_result_path, "Chart result")
    with tempfile.TemporaryDirectory() as temporary:
        expected_chart_dir = Path(temporary) / "chart"
        expected_chart_result = chart.package_chart(
            expected_chart_dir,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        expected_chart_package = expected_chart_dir / expected_chart_result["filename"]
        if chart_result != expected_chart_result:
            raise ValueError("Chart result does not match fresh validation")
        if chart_package_path.name != expected_chart_result["filename"]:
            raise ValueError("Chart package filename is noncanonical")
        if not chart_package_path.is_file():
            raise ValueError("Chart package is not a regular file")
        if chart_package_path.read_bytes() != expected_chart_package.read_bytes():
            raise ValueError("Chart package bytes do not match fresh validation")

    image_result = image.validate_image_result(
        _load_canonical_json(image_result_path, "image result")
    )
    if (
        image.require_fixture_base_authority(
            image_result["base"], image_result["target_platform"]
        )
        != image_result["base"]
    ):
        raise ValueError("image result base is not the authoritative fixture base")
    if not buildkit_metadata_path.is_file():
        raise ValueError("BuildKit metadata is not a regular file")
    try:
        buildkit_metadata = json.loads(buildkit_metadata_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"BuildKit metadata is invalid JSON: {error}") from error
    compact_oci = image.validate_compact_oci_evidence(
        oci_evidence_dir,
        image_result=image_result,
        image_recipe_path=image_recipe_path,
        image_metadata_path=image_metadata_path,
        image_prepare_path=image_prepare_path,
        wheel_path=wheel_path,
        buildkit_metadata=buildkit_metadata,
    )
    if compact_oci["wheel_sha256"] != actual_wheel_sha256:
        raise ValueError("compact OCI evidence does not bind the actual wheel")
    if not image_archive_sha256_path.is_file():
        raise ValueError("OCI archive digest record is not a regular file")
    archive_record = image_archive_sha256_path.read_text(encoding="utf-8").strip()
    archive_parts = archive_record.split()
    if (
        len(archive_parts) != 2
        or "sha256:" + archive_parts[0] != compact_oci["archive_sha256"]
        or Path(archive_parts[1]).name != "image.oci.tar"
    ):
        raise ValueError("OCI archive digest record does not match compact evidence")
    completed = _load_canonical_json(completed_loop_path, "completed loop")
    second_reconcile = _load_canonical_json(
        second_reconcile_path, "second fixture reconcile"
    )
    image_loop = _load_canonical_json(image_loop_path, "image loop evidence")
    if set(completed) != {"second_reconcile", "evidence"}:
        raise ValueError("completed loop fields are noncanonical")
    completed_evidence = completed.get("evidence")
    if not isinstance(completed_evidence, dict) or set(completed_evidence) != {
        "payload",
        "payload_sha256",
        "github",
    }:
        raise ValueError("completed loop evidence fields are noncanonical")
    recomputed = complete_candidate_loop(
        prepared,
        image_result,
        source_sha=source_sha,
        run=completed_evidence["github"],
    )
    if completed != recomputed:
        raise ValueError("completed loop does not match full recomputation")
    if second_reconcile != recomputed["second_reconcile"]:
        raise ValueError(
            "standalone second fixture reconcile disagrees with completed loop"
        )
    if image_loop != recomputed["evidence"]:
        raise ValueError("image loop envelope disagrees with completed loop")

    image_payload = recomputed["evidence"]["payload"]
    scenarios = image_payload["scenarios"]
    operation_batches = image_payload["operation_batches"]
    derived_operation_audit = audit_operation_batches(operation_batches)
    derived_write_audit = {
        **derived_operation_audit,
        "ledger_sha256": sha256_value(operation_batches),
    }
    if image_payload["write_audit"] != derived_write_audit:
        raise ValueError("completed write audit does not match its operation ledger")
    must_green = {
        "fixture_wheel": (
            build_record["wheel_sha256"] == actual_wheel_sha256
            and inspected["status"] == "fixture-only"
            and inspected["published"] is False
        ),
        "helm_cuda_a2_a3": (
            chart_result["rendered_cases"] == ["cuda", "a2", "a3"]
            and set(chart_result["checks"].values()) == {"passed"}
            and chart_result["status"] == "candidate-verified"
        ),
        "install_only_image": (
            set(image_result["gates"]) == REQUIRED_IMAGE_GATES
            and set(image_result["gates"].values()) == {"passed"}
            and image_result["status"] == "fixture-verified-unpublished"
        ),
        "second_reconcile_zero": (
            second_reconcile["task_count"] == 0
            and second_reconcile["decision"] == "already-present"
        ),
    }
    if (
        not all(must_green.values())
        or [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item.get("passed") is True for item in scenarios)
        or image_payload["publication"] != {"status": "blocked", "attempted": False}
        or derived_write_audit["write_count"] != 0
    ):
        raise ValueError("aggregate candidate closure did not pass every required gate")
    payload = {
        "mode": "fork-dry-run",
        "repository": repository,
        "ref": ref,
        "source_sha": source_sha,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "must_green": must_green,
        "scenarios": copy.deepcopy(scenarios),
        "compatibility": copy.deepcopy(image_payload["compatibility"]),
        "candidate_identity": copy.deepcopy(image_payload["candidate_identity"]),
        "artifact_digests": {
            "wheel_sha256": wheel_record["sha256"],
            "wheel_inspection_sha256": build_record["inspection_sha256"],
            "chart_sha256": chart_result["sha256"],
            "chart_tree_sha256": chart_result["release_tree_sha256"],
            "upstream_index_digest": image_payload["upstream_index_digest"],
            "oci_digest": image_payload["oci_digest"],
            "image_result_sha256": image_payload["image_result_sha256"],
            "first_reconcile_sha256": image_payload["first_reconcile_sha256"],
            "second_reconcile_sha256": image_payload["second_reconcile_sha256"],
            "image_loop_payload_sha256": image_loop["payload_sha256"],
            "oci_evidence_closure_sha256": compact_oci["closure_sha256"],
            "oci_manifest_digest": compact_oci["oci_digest"],
            "oci_config_digest": compact_oci["config_digest"],
            "oci_archive_sha256": compact_oci["archive_sha256"],
            "build_record_file_sha256": _file_sha256(build_record_path),
            "image_result_file_sha256": _file_sha256(image_result_path),
            "second_reconcile_file_sha256": _file_sha256(second_reconcile_path),
        },
        "required_gates": copy.deepcopy(image_payload["required_gates"]),
        "expected_blocked": [
            "production-wheel-builders",
            "accelerator-runtime",
            "cuda-device",
            "ascend-a2-device",
            "ascend-a3-device",
            "protected-environment",
            "registry-publication-and-readback",
        ],
        "publication": copy.deepcopy(image_payload["publication"]),
        "operation_batches": copy.deepcopy(operation_batches),
        "write_audit": copy.deepcopy(derived_write_audit),
    }
    evidence = _envelope(payload, run)
    evidence["github"]["non_deterministic_artifact_file_sha256"] = {
        "completed_loop": _file_sha256(completed_loop_path),
        "image_loop": _file_sha256(image_loop_path),
        "buildkit_metadata": _file_sha256(buildkit_metadata_path),
    }
    return evidence


def _fixture_inventory(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    inventory = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": entries or [],
    }
    inventory["inventory_sha256"] = fixture_inventory_digest(inventory)
    return inventory


def _fixture_entry(
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


def _validate_operation_reference(
    reference_kind: str,
    reference: object,
    *,
    staging_repository: str | None = None,
    public_targets: set[str] | None = None,
) -> None:
    if not isinstance(reference, str):
        raise ValueError("operation has malformed reference")
    if reference_kind == "digest":
        valid = DIGEST_RE.fullmatch(reference) is not None
    elif reference_kind == "upstream-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and REPOSITORY_RE.fullmatch(repository) is not None
            and DIGEST_RE.fullmatch(digest) is not None
        )
    elif reference_kind == "upstream-tag":
        repository, separator, tag = reference.rpartition(":")
        valid = (
            separator == ":"
            and REPOSITORY_RE.fullmatch(repository) is not None
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) is not None
        )
    elif reference_kind == "fixture-upstream-tag":
        repository, separator, tag = reference.rpartition(":")
        valid = separator == ":" and REPOSITORY_RE.fullmatch(repository) is not None
        if valid:
            try:
                parse_fixture_upstream_tag(repository.rsplit("/", 1)[-1], tag)
            except ValueError:
                valid = False
    elif reference_kind == "fixture-target-tag":
        matching = [
            repository
            for repository in FIXTURE_TARGET_REPOSITORIES.values()
            if reference.startswith(repository + ":")
        ]
        valid = len(matching) == 1
        if valid:
            try:
                validate_public_tag(reference.removeprefix(matching[0] + ":"))
            except ValueError:
                valid = False
    elif reference_kind == "staging-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and staging_repository is not None
            and REPOSITORY_RE.fullmatch(staging_repository) is not None
            and repository == staging_repository
            and DIGEST_RE.fullmatch(digest) is not None
        )
    elif reference_kind == "staging-tag":
        prefix = (staging_repository or "") + ":staging-"
        valid = (
            staging_repository is not None
            and REPOSITORY_RE.fullmatch(staging_repository) is not None
            and reference.startswith(prefix)
            and re.fullmatch(r"[0-9a-f]{64}", reference.removeprefix(prefix))
            is not None
        )
    elif reference_kind == "public-target":
        allowed_targets = set() if public_targets is None else public_targets
        valid = reference in allowed_targets
    elif reference_kind in {"registry-read-tag", "registry-read-tag-or-digest"}:
        public_tags = set() if public_targets is None else public_targets
        staging_prefix = (staging_repository or "") + ":staging-"
        valid = reference in public_tags or (
            staging_repository is not None
            and REPOSITORY_RE.fullmatch(staging_repository) is not None
            and reference.startswith(staging_prefix)
            and re.fullmatch(r"[0-9a-f]{64}", reference.removeprefix(staging_prefix))
            is not None
        )
        if not valid and reference_kind == "registry-read-tag-or-digest":
            repository, separator, digest = reference.rpartition("@")
            valid = (
                separator == "@"
                and repository
                in {
                    *(
                        {staging_repository}
                        if staging_repository is not None
                        else set()
                    ),
                    *{target.rsplit(":", 1)[0] for target in public_tags},
                }
                and DIGEST_RE.fullmatch(digest) is not None
            )
    elif reference_kind == "registry-read-digest":
        allowed_targets = set() if public_targets is None else public_targets
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and repository
            in {
                *({staging_repository} if staging_repository is not None else set()),
                *{target.rsplit(":", 1)[0] for target in allowed_targets},
            }
            and DIGEST_RE.fullmatch(digest) is not None
        )
    else:  # pragma: no cover - immutable mapping owns this branch.
        raise ValueError(f"unknown operation reference contract: {reference_kind}")
    if not valid:
        if reference_kind in {"staging-digest", "staging-tag", "public-target"}:
            raise ValueError(
                f"operation reference is outside the exact allowlist: {reference}"
            )
        raise ValueError(
            f"operation has malformed reference for {reference_kind}: {reference}"
        )


def audit_operations(
    operations: list[dict[str, Any]],
    *,
    lane: str | None = None,
    staging_repository: str | None = None,
    public_targets: set[str] | None = None,
) -> dict[str, Any]:
    """Derive zero-write evidence from emitted operation ledgers."""
    if not isinstance(operations, list):
        raise ValueError("operation ledger must be an array")
    if lane not in {None, "feature-candidate", "protected-tag"}:
        raise ValueError(f"unknown operation audit lane: {lane}")
    operation_types: set[str] = set()
    identities: set[tuple[str, str]] = set()
    write_capable_operations: list[dict[str, Any]] = []
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
        if operation_type in KNOWN_WRITE_OPERATION_TYPES and lane in {
            None,
            "feature-candidate",
        }:
            if lane == "feature-candidate":
                raise ValueError(
                    f"feature-candidate rejects write-capable operation: {operation_type}"
                )
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
        _validate_operation_reference(
            reference_kind,
            operation["reference"],
            staging_repository=staging_repository,
            public_targets=public_targets,
        )
        if operation_type in KNOWN_WRITE_OPERATION_TYPES:
            write_capable_operations.append(copy.deepcopy(operation))
        identity = (operation_type, operation["reference"])
        if identity in identities:
            raise ValueError(f"duplicate operation identity: {identity}")
        identities.add(identity)
        operation_types.add(operation_type)
    return {
        "operation_count": len(operations),
        "operation_types": sorted(operation_types),
        "write_capable_operations": write_capable_operations,
        "write_count": len(write_capable_operations),
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
    stable = _fixture_entry(candidate, case["upstream_snapshot"]["index_digest"])
    conflicting = copy.deepcopy(stable)
    conflicting["observed_digest"] = "sha256:" + "f" * 64
    production_case = copy.deepcopy(case)
    results = [
        expect_blocker(
            "duplicate-conflicting-inventory",
            lambda: reconcile_fixture_candidate(
                candidate, _fixture_inventory([stable, conflicting])
            ),
        ),
        expect_blocker(
            "missing-linux-arm64", lambda: validate_fixture_snapshot(snapshot)
        ),
        expect_blocker(
            "production-wheel-unpublished",
            lambda: build_fixture_candidate(**production_case, fixture_mode=False),
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
        "catalog",
        "compatibility_rule_id",
        "implementation_digest",
    }
    if set(case) != required:
        raise ValueError(
            "loop verification fields mismatch: "
            f"missing={sorted(required - set(case))}, extra={sorted(set(case) - required)}"
        )
    requested_snapshot = validate_fixture_snapshot(case["upstream_snapshot"])
    scan_result = scan_fixture_registry(
        requested_snapshot["repository"],
        requested_snapshot["upstream_tag"],
        fixture=requested_snapshot,
    )
    fixture_case = {**case, "upstream_snapshot": scan_result["snapshot"]}
    candidate = build_fixture_candidate(**fixture_case, fixture_mode=True)
    snapshot = scan_result["snapshot"]
    digest = snapshot["index_digest"]

    new_result = reconcile_fixture_candidate(candidate, _fixture_inventory())
    stable_inventory = _fixture_inventory([_fixture_entry(candidate, digest)])
    same_result = reconcile_fixture_candidate(candidate, stable_inventory)
    drift_inventory = _fixture_inventory(
        [
            _fixture_entry(
                candidate,
                digest,
                observed_digest="sha256:" + "f" * 64,
            )
        ]
    )
    drift_result = reconcile_fixture_candidate(candidate, drift_inventory)
    blockers = _required_blockers(fixture_case, candidate)

    first_fixture_result = reconcile_fixture_candidate(candidate, _fixture_inventory())
    completed_entry = _fixture_entry(candidate, digest)
    final_fixture_result = reconcile_fixture_candidate(
        candidate, _fixture_inventory([completed_entry])
    )
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
        "operation_batches": copy.deepcopy(operation_batches),
        "zero_write_audit": zero_write_audit,
    }
    return {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-envelope",
        "run": copy.deepcopy(run or {}),
        "payload": payload,
        "payload_sha256": sha256_value(payload),
    }
