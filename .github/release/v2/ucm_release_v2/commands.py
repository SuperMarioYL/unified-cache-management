"""Pure parsing for read-only ``/release`` command previews."""

from __future__ import annotations

import re
from typing import Any

from .common import sha256_envelope


class CommandError(ValueError):
    """Raised when command metadata cannot be interpreted unambiguously."""


_BUILD_COMMAND = re.compile(r"/release build ([0-9a-f]{40})", flags=re.ASCII)
_SIMPLE_COMMAND = re.compile(r"/release (status|cancel)", flags=re.ASCII)
_SHA = re.compile(r"[0-9a-f]{40}", flags=re.ASCII)
_BUILD_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CommandError(f"{name} must be a non-empty trimmed string")
    if any(character in "\r\n\x00" for character in value):
        raise CommandError(f"{name} must not contain control characters")
    return value


def _source_identity(
    observed_source_sha: str | None, current_source_sha: str | None
) -> None:
    if (observed_source_sha is None) != (current_source_sha is None):
        raise CommandError(
            "observed_source_sha and current_source_sha must be provided together"
        )
    for name, value in (
        ("observed_source_sha", observed_source_sha),
        ("current_source_sha", current_source_sha),
    ):
        if value is not None and not _SHA.fullmatch(value):
            raise CommandError(
                f"{name} must be exactly 40 lowercase hexadecimal characters"
            )


def parse_command(
    body: str,
    *,
    actor: str,
    author_association: str,
    observed_source_sha: str | None = None,
    current_source_sha: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic explanation or preview; never perform an action."""
    if not isinstance(body, str):
        raise CommandError("body must be a string")
    actor = _identity(actor, "actor")
    association = _identity(author_association, "author_association")
    _source_identity(observed_source_sha, current_source_sha)

    safe_body = body.isascii() and not any(
        character in body for character in "\r\n\x00"
    )
    normalized_body = body.strip(" \t") if safe_body else ""
    build_match = _BUILD_COMMAND.fullmatch(normalized_body)
    simple_match = _SIMPLE_COMMAND.fullmatch(normalized_body)
    requested_source_sha = build_match.group(1) if build_match else None
    command = (
        "build"
        if build_match
        else simple_match.group(1) if simple_match else "unsupported"
    )
    authorized = False
    accepted = False
    reason = "unsupported-command"
    action = "none"

    if command == "build":
        if observed_source_sha is None or current_source_sha is None:
            raise CommandError(
                "build requires observed_source_sha and current_source_sha"
            )
        assert requested_source_sha is not None
        authorized = association in _BUILD_ASSOCIATIONS
        accepted = authorized
        reason = "authorized-build-preview" if authorized else "unauthorized-build"
        action = "build-preview"
        if requested_source_sha != observed_source_sha:
            accepted = False
            reason = "requested-pr-sha-mismatch"
        elif requested_source_sha != current_source_sha:
            accepted = False
            reason = "stale-pr-sha"
    elif command == "status":
        authorized = True
        accepted = True
        reason = "read-only-status"
        action = "inspect-preview"
    elif command == "cancel":
        authorized = True
        reason = "dry-run-no-actions-write"
        action = "cancel-preview"

    document: dict[str, Any] = {
        "accepted": accepted,
        "actions_write": False,
        "actor": actor,
        "author_association": association,
        "authorized": authorized,
        "command": command,
        "kind": "release-command",
        "mode": "dry-run",
        "operations": [{"action": action, "executed": False}],
        "reason": reason,
        "schema_version": 2,
    }
    if observed_source_sha is not None:
        document["observed_source_sha"] = observed_source_sha
        document["current_source_sha"] = current_source_sha
    if requested_source_sha is not None:
        document["requested_source_sha"] = requested_source_sha
    return sha256_envelope(document)


__all__ = ["CommandError", "parse_command"]
