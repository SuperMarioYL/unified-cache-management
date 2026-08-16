"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import re
from types import MappingProxyType
from typing import Any

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
