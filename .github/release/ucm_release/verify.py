"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .core import (
    sha256_value,
)
from .registry import (
    FIXTURE_TARGET_REPOSITORIES,
    parse_fixture_upstream_tag,
    validate_public_tag,
)

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


def _source_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source SHA must be a full lowercase Git commit")
    return value


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
