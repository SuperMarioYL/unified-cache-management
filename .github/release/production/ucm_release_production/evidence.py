"""Canonical production evidence assembly and injection-safe summary rendering."""

from __future__ import annotations

from typing import Any

from .common import (
    ProductionError,
    require_lower_commit_sha,
    require_string,
    sha256_envelope,
    verify_envelope,
)


def _identity(value: object) -> dict[str, Any]:
    identity = verify_envelope(
        value,
        kind="ucm-production-trusted-run-identity",
        schema_version=1,
    )
    for key in ("control_sha", "source_sha", "tag_object_sha"):
        require_lower_commit_sha(identity.get(key), f"trusted identity {key}")
    for key in ("repository_id", "run_id", "run_attempt"):
        if type(identity.get(key)) is not int or identity[key] < 1:
            raise ProductionError(f"trusted identity {key} is invalid")
    require_string(identity.get("repository"), "trusted identity repository")
    require_string(identity.get("default_branch"), "trusted identity default branch")
    require_string(identity.get("tag_name"), "trusted identity Tag")
    return identity


def _candidate(value: object) -> dict[str, Any]:
    candidate = verify_envelope(
        value,
        kind="ucm-production-candidate-envelope",
        schema_version=1,
    )
    require_string(candidate.get("repository"), "candidate repository")
    require_string(candidate.get("tag_name"), "candidate Tag")
    require_lower_commit_sha(candidate.get("source_sha"), "candidate source SHA")
    if (
        type(candidate.get("repository_id")) is not int
        or candidate["repository_id"] < 1
    ):
        raise ProductionError("candidate repository id is invalid")
    return candidate


def _environment(value: object) -> dict[str, Any]:
    environment = verify_envelope(
        value,
        kind="ucm-production-environment-evidence",
        schema_version=1,
    )
    if environment.get("status") not in {
        "passed",
        "waived-for-preview",
        "failed",
        "blocked",
    }:
        raise ProductionError("production environment evidence status is invalid")
    require_lower_commit_sha(
        environment.get("source_sha"), "production environment source SHA"
    )
    return environment


def assemble_evidence(
    identity_value: object,
    candidate_value: object,
    environment_value: object,
    channel_values: list[object],
) -> dict[str, Any]:
    """Bind trusted identities, candidate bytes, environment, and all channels."""

    identity = _identity(identity_value)
    candidate = _candidate(candidate_value)
    environment = _environment(environment_value)
    if (
        candidate["repository"] != identity["repository"]
        or candidate["repository_id"] != identity["repository_id"]
        or candidate["tag_name"] != identity["tag_name"]
        or candidate["source_sha"] != identity["source_sha"]
        or environment["source_sha"] != identity["source_sha"]
    ):
        raise ProductionError("production evidence cross-object identity differs")
    if not isinstance(channel_values, list):
        raise ProductionError("production channel evidence must be an array")
    channels: list[dict[str, Any]] = []
    seen: set[tuple[object, object]] = set()
    operations: list[dict[str, Any]] = []
    blockers: list[str] = []
    complete = 0
    for index, value in enumerate(channel_values):
        record = verify_envelope(
            value,
            kind="ucm-production-channel-record",
            schema_version=1,
        )
        channel = require_string(record.get("channel"), f"channel {index} type")
        reference = record.get("reference", f"record:{index}")
        key = (channel, reference)
        if key in seen:
            raise ProductionError("production channel evidence is duplicated")
        seen.add(key)
        status = record.get("status")
        if status == "complete":
            complete += 1
        elif status in {"blocked", "visibility-configuration-required"}:
            blockers.append(f"{channel}: {status}")
        else:
            raise ProductionError("production channel evidence status is invalid")
        record_operations = record.get("operations", [])
        if not isinstance(record_operations, list) or any(
            not isinstance(item, dict) for item in record_operations
        ):
            raise ProductionError("production channel operation ledger is invalid")
        operations.extend(dict(item) for item in record_operations)
        channels.append(record)
    if not channels:
        status = "blocked"
        blockers.append("no production channels were completed")
    elif blockers:
        status = "partial-publication" if complete else "blocked"
    else:
        status = "complete"
    return sha256_envelope(
        {
            "kind": "ucm-production-release-evidence",
            "schema_version": 1,
            "status": status,
            "repository": identity["repository"],
            "repository_id": identity["repository_id"],
            "default_branch": identity["default_branch"],
            "control_sha": identity["control_sha"],
            "source_sha": identity["source_sha"],
            "tag_name": identity["tag_name"],
            "tag_object_sha": identity["tag_object_sha"],
            "run_id": identity["run_id"],
            "run_attempt": identity["run_attempt"],
            "candidate_sha256": candidate["sha256"],
            "identity": identity,
            "candidate": candidate,
            "environment": environment,
            "channels": channels,
            "operations": operations,
            "blockers": blockers,
        }
    )


def _safe_markdown(value: object) -> str:
    text = str(value)
    text = " ".join(text.splitlines())
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\|")
    text = text.replace("`", "&#96;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text


def render_summary(value: object) -> str:
    """Render a compact summary without raw HTML, fences, or table injection."""

    evidence = verify_envelope(
        value,
        kind="ucm-production-release-evidence",
        schema_version=1,
    )
    blockers = evidence.get("blockers")
    if not isinstance(blockers, list):
        raise ProductionError("production evidence blockers are malformed")
    lines = [
        "# UCM production release evidence",
        "",
        f"- Status: {_safe_markdown(evidence.get('status'))}",
        f"- Repository: {_safe_markdown(evidence.get('repository'))}",
        f"- Tag: {_safe_markdown(evidence.get('tag_name'))}",
        f"- Source SHA: {_safe_markdown(evidence.get('source_sha'))}",
        f"- Control SHA: {_safe_markdown(evidence.get('control_sha'))}",
        f"- Candidate SHA256: {_safe_markdown(evidence.get('candidate_sha256'))}",
        f"- Channel records: {len(evidence.get('channels', []))}",
        "",
        "## Blockers",
        "",
    ]
    lines.extend([f"- {_safe_markdown(item)}" for item in blockers] or ["- None"])
    lines.extend(
        [
            "",
            "Hardware and Kubernetes cluster acceptance remain separate evidence layers.",
            "",
        ]
    )
    return "\n".join(lines)
