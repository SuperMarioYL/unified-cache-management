"""Strict production Tag routing and immutable source-lineage validation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .common import (
    ProductionError,
    require_exact_keys,
    require_lower_commit_sha,
    require_lower_sha256,
    require_string,
    sha256_envelope,
    verify_envelope,
)

_NUMBER = r"(0|[1-9][0-9]*)"
_POSITIVE = r"([1-9][0-9]*)"
_DRAFT = re.compile(rf"draft/v{_NUMBER}\.{_NUMBER}\.{_NUMBER}-{_POSITIVE}", re.ASCII)
_RC = re.compile(rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}rc{_POSITIVE}", re.ASCII)
_FINAL = re.compile(rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}", re.ASCII)
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", re.ASCII
)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", re.ASCII)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z",
    re.ASCII,
)
_INTENT_KEYS = {
    "kind",
    "schema_version",
    "stage",
    "tag_name",
    "version",
    "wheel_version",
    "chart_version",
    "image_tag",
    "release_branch",
    "draft_number",
    "rc_number",
    "sha256",
}


@dataclass(frozen=True)
class TagIntent:
    stage: str
    tag_name: str
    version: str
    wheel_version: str
    chart_version: str
    image_tag: str
    release_branch: str
    draft_number: int | None
    rc_number: int | None


def _version_parts(value: str, label: str) -> tuple[int, int, int]:
    match = re.fullmatch(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}", value, re.ASCII)
    if match is None:
        raise ProductionError(f"{label} must be canonical X.Y.Z")
    return tuple(int(item) for item in match.groups())


def parse_tag(tag_name: str, config: dict[str, Any]) -> TagIntent:
    """Parse one canonical production Tag into all channel-specific versions."""

    if not isinstance(tag_name, str):
        raise ProductionError("tag name must be an ASCII string")
    base = _version_parts(config.get("base_version"), "base_version")
    release_line = config.get("release_line")
    if release_line != f"{base[0]}.{base[1]}":
        raise ProductionError("tag release line does not match base_version")

    draft = _DRAFT.fullmatch(tag_name)
    rc = _RC.fullmatch(tag_name)
    final = _FINAL.fullmatch(tag_name)
    if draft is not None:
        major, minor, patch, number = (int(item) for item in draft.groups())
        stage = "draft"
        draft_number: int | None = number
        rc_number: int | None = None
    elif rc is not None:
        major, minor, patch, number = (int(item) for item in rc.groups())
        stage = "rc"
        draft_number = None
        rc_number = number
    elif final is not None:
        major, minor, patch = (int(item) for item in final.groups())
        stage = "stable" if (major, minor, patch) == base else "hotfix"
        draft_number = None
        rc_number = None
    else:
        raise ProductionError("tag name does not match a canonical production tag")

    if (major, minor) != base[:2]:
        raise ProductionError("tag does not belong to the configured release line")
    if stage in {"draft", "rc", "stable"} and patch != base[2]:
        raise ProductionError(f"tag stage {stage} must use the base version")
    if stage == "hotfix" and patch <= base[2]:
        raise ProductionError("hotfix tag must increment the configured patch version")

    version = f"{major}.{minor}.{patch}"
    if stage == "draft":
        assert draft_number is not None
        wheel_version = f"{version}.dev{draft_number}"
        chart_version = f"{version}-draft.{draft_number}"
        image_tag = f"draft-v{version}-{draft_number}"
        release_branch = config["release_branch"]
    elif stage == "rc":
        assert rc_number is not None
        wheel_version = f"{version}rc{rc_number}"
        chart_version = f"{version}-rc.{rc_number}"
        image_tag = tag_name
        release_branch = config["release_branch"]
    elif stage == "stable":
        wheel_version = chart_version = version
        image_tag = tag_name
        release_branch = config["release_branch"]
    else:
        wheel_version = chart_version = version
        image_tag = tag_name
        release_branch = f"hotfix/{version}"

    return TagIntent(
        stage=stage,
        tag_name=tag_name,
        version=version,
        wheel_version=wheel_version,
        chart_version=chart_version,
        image_tag=image_tag,
        release_branch=release_branch,
        draft_number=draft_number,
        rc_number=rc_number,
    )


def intent_document(intent: TagIntent) -> dict[str, Any]:
    return sha256_envelope(
        {
            "kind": "ucm-production-tag-intent",
            "schema_version": 1,
            **asdict(intent),
        }
    )


def reopen_intent(value: object, config: dict[str, Any]) -> TagIntent:
    document = verify_envelope(
        value,
        kind="ucm-production-tag-intent",
        schema_version=1,
        exact_keys=_INTENT_KEYS,
    )
    intent = parse_tag(document["tag_name"], config)
    if document != intent_document(intent):
        raise ProductionError("tag intent does not match its canonical tag projection")
    return intent


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _two_equal(value: object, label: str) -> tuple[Any, Any]:
    if not isinstance(value, list) or len(value) != 2:
        raise ProductionError(f"{label} must contain exactly two reads")
    if value[0] != value[1]:
        raise ProductionError(f"{label} double-read values do not match")
    return value[0], value[1]


def _branch(value: object, label: str) -> str:
    branch = require_string(value, label)
    if (
        _BRANCH.fullmatch(branch) is None
        or branch.startswith("refs/")
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise ProductionError(f"{label} is not a canonical branch name")
    return branch


def _lineage(
    intent: TagIntent, value: object, source_sha: str
) -> dict[str, Any] | None:
    if intent.stage == "draft":
        if value is not None:
            raise ProductionError("Draft must not claim prior release lineage")
        return None
    if value is None:
        prior = {"rc": "Draft", "stable": "RC", "hotfix": "previous Stable"}[
            intent.stage
        ]
        raise ProductionError(f"{intent.stage} requires accepted {prior} lineage")
    lineage = _object(value, f"{intent.stage} lineage")
    common_keys = {
        "accepted",
        "stage",
        "version",
        "tag_name",
        "source_commit_sha",
        "evidence_sha256",
    }
    if intent.stage == "hotfix":
        common_keys.add("ancestry_verified")
    require_exact_keys(lineage, common_keys, f"{intent.stage} lineage")
    if lineage["accepted"] is not True:
        raise ProductionError(f"{intent.stage} lineage must be accepted")
    require_lower_sha256(lineage["evidence_sha256"], "lineage evidence_sha256")
    prior_source = require_lower_commit_sha(
        lineage["source_commit_sha"], "lineage source_commit_sha"
    )
    if intent.stage == "rc":
        if lineage["stage"] != "draft":
            raise ProductionError("RC lineage must reference a Draft")
        if lineage["version"] != intent.version:
            raise ProductionError("RC lineage Draft version must match")
        if _DRAFT.fullmatch(lineage["tag_name"]) is None:
            raise ProductionError("RC lineage must reference a canonical Draft tag")
        if prior_source != source_sha:
            raise ProductionError("RC lineage Draft must use the same source commit")
    elif intent.stage == "stable":
        if lineage["stage"] != "rc":
            raise ProductionError("Stable lineage must reference an accepted RC")
        if lineage["version"] != intent.version:
            raise ProductionError("Stable lineage RC version must match")
        if _RC.fullmatch(lineage["tag_name"]) is None:
            raise ProductionError("Stable lineage must reference a canonical RC tag")
        if prior_source != source_sha:
            raise ProductionError("Stable lineage RC must use the same source commit")
    else:
        major, minor, patch = _version_parts(intent.version, "hotfix version")
        previous = f"{major}.{minor}.{patch - 1}"
        if lineage["stage"] != "stable":
            raise ProductionError("Hotfix lineage must reference the previous Stable")
        if lineage["version"] != previous or lineage["tag_name"] != f"v{previous}":
            raise ProductionError(
                "Hotfix lineage must reference the previous Stable version"
            )
        if lineage["ancestry_verified"] is not True:
            raise ProductionError("Hotfix lineage must include verified ancestry")
        if prior_source == source_sha:
            raise ProductionError("Hotfix lineage must use a different source commit")
    return lineage


def verify_ref_snapshot(intent: TagIntent, snapshot: object) -> dict[str, Any]:
    """Close annotated Tag, source branch, control branch, and prior lineage."""

    value = _object(snapshot, "ref snapshot")
    require_exact_keys(
        value,
        {
            "kind",
            "schema_version",
            "repository",
            "repository_id",
            "tag",
            "source_branch",
            "control",
            "lineage",
        },
        "ref snapshot",
    )
    if value["kind"] != "ucm-production-ref-snapshot":
        raise ProductionError("ref snapshot kind is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ProductionError("ref snapshot schema_version must be 1")
    repository = require_string(value["repository"], "snapshot repository")
    if _REPOSITORY.fullmatch(repository) is None:
        raise ProductionError("snapshot repository must be owner/name")
    repository_id = value["repository_id"]
    if type(repository_id) is not int or repository_id < 1:
        raise ProductionError("snapshot repository_id must be a positive integer")

    tag = _object(value["tag"], "tag snapshot")
    require_exact_keys(tag, {"name", "ref_reads", "object_reads"}, "tag snapshot")
    if tag["name"] != intent.tag_name:
        raise ProductionError("tag name does not match release intent")
    ref_read, _ = _two_equal(tag["ref_reads"], "tag ref")
    ref = _object(ref_read, "tag ref read")
    require_exact_keys(ref, {"object_type", "object_sha"}, "tag ref read")
    if ref["object_type"] != "tag":
        raise ProductionError("production tag must be an annotated tag object")
    tag_object_sha = require_lower_commit_sha(ref["object_sha"], "tag object SHA")

    object_read, _ = _two_equal(tag["object_reads"], "tag object")
    tag_object = _object(object_read, "tag object read")
    require_exact_keys(
        tag_object,
        {
            "tag_object_sha",
            "target_type",
            "peeled_commit_sha",
            "tagger",
            "tagged_at",
            "message_sha256",
        },
        "tag object read",
    )
    if tag_object["tag_object_sha"] != tag_object_sha:
        raise ProductionError("tag object SHA does not match tag ref")
    if tag_object["target_type"] != "commit":
        raise ProductionError("annotated tag must peel directly to a commit")
    source_sha = require_lower_commit_sha(
        tag_object["peeled_commit_sha"], "peeled commit SHA"
    )
    tagger = require_string(tag_object["tagger"], "tagger")
    tagged_at = require_string(tag_object["tagged_at"], "tagged_at")
    if _TIMESTAMP.fullmatch(tagged_at) is None:
        raise ProductionError("tagged_at must be a canonical UTC timestamp")
    message_sha256 = require_lower_sha256(
        tag_object["message_sha256"], "tag message_sha256"
    )

    source = _object(value["source_branch"], "source branch snapshot")
    require_exact_keys(source, {"name", "head_reads"}, "source branch snapshot")
    if source["name"] != intent.release_branch:
        raise ProductionError("source branch does not match release intent")
    source_read, _ = _two_equal(source["head_reads"], "source branch")
    source_head = require_lower_commit_sha(source_read, "source branch head")
    if source_head != source_sha:
        raise ProductionError("source branch head does not equal peeled tag commit")

    control = _object(value["control"], "control snapshot")
    require_exact_keys(control, {"default_branch", "head_reads"}, "control snapshot")
    control_branch = _branch(control["default_branch"], "control default_branch")
    control_read, _ = _two_equal(control["head_reads"], "control branch")
    control_sha = require_lower_commit_sha(control_read, "control branch head")
    lineage = _lineage(intent, value["lineage"], source_sha)

    return sha256_envelope(
        {
            "kind": "ucm-production-source-identity",
            "schema_version": 1,
            "repository": repository,
            "repository_id": repository_id,
            "stage": intent.stage,
            "tag_name": intent.tag_name,
            "tag_object_sha": tag_object_sha,
            "source_commit_sha": source_sha,
            "source_branch": intent.release_branch,
            "tagger": tagger,
            "tagged_at": tagged_at,
            "tag_message_sha256": message_sha256,
            "control_default_branch": control_branch,
            "control_sha": control_sha,
            "lineage": lineage,
        }
    )
