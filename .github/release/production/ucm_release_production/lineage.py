"""Resolve prior production Releases into strict stage-lineage evidence."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .common import (
    ProductionError,
    canonical_bytes,
    decode_json,
    require_exact_keys,
    require_lower_commit_sha,
    require_lower_sha256,
)
from .github_release import (
    GitHubNotFound,
    GitHubReleaseClient,
    delivery_asset_names,
)
from .tags import TagIntent

_DRAFT = re.compile(
    r"draft/v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-(?P<number>[1-9][0-9]*)",
    re.ASCII,
)
_RC = re.compile(
    r"v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)rc(?P<number>[1-9][0-9]*)",
    re.ASCII,
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_LINEAGE_MARKER = re.compile(
    r"<!-- ucm-production-lineage-v1 (\{[^\r\n]*\}) -->", re.ASCII
)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductionError(f"{label} must be a positive integer")
    return value


def _release_assets(
    client: GitHubReleaseClient,
    release: dict[str, Any],
    *,
    expected_stage: str,
    expected_version: str,
    tag_name: str,
) -> dict[str, dict[str, Any]]:
    release_id = _positive(release.get("id"), "lineage Release id")
    values = client.list_release_assets(release_id)
    if not isinstance(values, list) or len(values) != 7:
        raise ProductionError("lineage Release must contain exactly seven assets")
    by_name: dict[str, dict[str, Any]] = {}
    for value in values:
        item = _object(value, "lineage Release asset")
        name = item.get("name")
        if not isinstance(name, str) or name in by_name:
            raise ProductionError("lineage Release asset names are invalid")
        digest = item.get("digest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ProductionError("lineage Release asset digest is invalid")
        by_name[name] = {
            "id": _positive(item.get("id"), "lineage Release asset id"),
            "size": _positive(item.get("size"), "lineage Release asset size"),
            "digest": digest,
        }
    expected = set(delivery_asset_names(expected_stage, tag_name, expected_version))
    if set(by_name) != expected:
        raise ProductionError("lineage Release delivery asset set differs")
    return by_name


def _release_marker(release: dict[str, Any], source_sha: str) -> dict[str, Any]:
    body = release.get("body")
    if not isinstance(body, str):
        raise ProductionError("lineage Release body is missing")
    matches = _LINEAGE_MARKER.findall(body)
    if len(matches) != 1:
        raise ProductionError("lineage Release marker is not unique")
    marker = _object(
        decode_json(matches[0].encode("ascii"), "lineage Release marker"),
        "lineage Release marker",
    )
    require_exact_keys(
        marker,
        {"candidate_sha256", "environment_status", "source_sha"},
        "lineage Release marker",
    )
    if (
        require_lower_commit_sha(marker["source_sha"], "lineage marker source")
        != source_sha
    ):
        raise ProductionError("lineage Release marker source differs")
    require_lower_sha256(marker["candidate_sha256"], "lineage candidate SHA256")
    if marker["environment_status"] not in {"passed", "waived-for-preview"}:
        raise ProductionError("lineage Release environment status is invalid")
    return marker


def _candidate_release(
    client: GitHubReleaseClient,
    release: dict[str, Any],
    *,
    expected_stage: str,
    expected_version: str,
    source_sha: str,
) -> dict[str, Any]:
    tag_name = release.get("tag_name")
    pattern = _DRAFT if expected_stage == "draft" else _RC
    if not isinstance(tag_name, str) or (match := pattern.fullmatch(tag_name)) is None:
        raise ProductionError("lineage Release Tag is not canonical")
    if match.group("version") != expected_version:
        raise ProductionError("lineage Release version differs")
    expected_state = (True, False) if expected_stage == "draft" else (False, True)
    if (release.get("draft"), release.get("prerelease")) != expected_state:
        raise ProductionError("lineage Release state differs")
    if release.get("target_commitish") != source_sha:
        raise ProductionError("lineage Release source commit differs")
    assets = _release_assets(
        client,
        release,
        expected_stage=expected_stage,
        expected_version=expected_version,
        tag_name=tag_name,
    )
    marker = _release_marker(release, source_sha)
    return {
        "release_id": _positive(release.get("id"), "lineage Release id"),
        "stage": expected_stage,
        "version": expected_version,
        "tag_name": tag_name,
        "source_commit_sha": source_sha,
        "candidate_sha256": marker["candidate_sha256"],
        "environment_status": marker["environment_status"],
        "asset_closure_sha256": hashlib.sha256(canonical_bytes(assets)).hexdigest(),
    }


def _list_releases(client: GitHubReleaseClient) -> list[dict[str, Any]]:
    return client.list_releases()


def _ancestry(repository_root: Path, prior_sha: str, source_sha: str) -> bool:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(Path(repository_root).resolve()),
            "merge-base",
            "--is-ancestor",
            prior_sha,
            source_sha,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def resolve_release_lineage(
    client: GitHubReleaseClient,
    intent: TagIntent,
    source_sha: str,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any] | None:
    """Resolve the highest accepted prior Release required by one stage."""

    source_sha = require_lower_commit_sha(source_sha, "lineage source SHA")
    if intent.stage == "draft":
        return None
    releases = _list_releases(client)
    if intent.stage in {"rc", "stable"}:
        prior_stage = "draft" if intent.stage == "rc" else "rc"
        pattern = _DRAFT if prior_stage == "draft" else _RC
        candidates: list[tuple[int, dict[str, Any]]] = []
        for release in releases:
            tag_name = release.get("tag_name")
            if (
                not isinstance(tag_name, str)
                or (match := pattern.fullmatch(tag_name)) is None
            ):
                continue
            if match.group("version") != intent.version:
                continue
            try:
                record = _candidate_release(
                    client,
                    release,
                    expected_stage=prior_stage,
                    expected_version=intent.version,
                    source_sha=source_sha,
                )
            except (ProductionError, GitHubNotFound):
                continue
            candidates.append((int(match.group("number")), record))
        if not candidates:
            raise ProductionError(
                f"{intent.stage} has no accepted {prior_stage} Release"
            )
        _number, selected = max(candidates, key=lambda item: item[0])
        evidence_sha = hashlib.sha256(canonical_bytes(selected)).hexdigest()
        return {
            "accepted": True,
            "stage": prior_stage,
            "version": intent.version,
            "tag_name": selected["tag_name"],
            "source_commit_sha": source_sha,
            "evidence_sha256": evidence_sha,
        }

    major, minor, patch = (int(item) for item in intent.version.split("."))
    previous_version = f"{major}.{minor}.{patch - 1}"
    previous_tag = f"v{previous_version}"
    matches = [item for item in releases if item.get("tag_name") == previous_tag]
    if len(matches) != 1:
        raise ProductionError("hotfix requires exactly one previous Stable Release")
    release = matches[0]
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ProductionError("hotfix lineage Release is not Stable")
    prior_source = require_lower_commit_sha(
        release.get("target_commitish"), "previous Stable source SHA"
    )
    if repository_root is None or not _ancestry(
        repository_root, prior_source, source_sha
    ):
        raise ProductionError("hotfix source does not descend from previous Stable")
    assets = _release_assets(
        client,
        release,
        expected_stage="stable",
        expected_version=previous_version,
        tag_name=previous_tag,
    )
    marker = _release_marker(release, prior_source)
    if marker["environment_status"] != "passed":
        raise ProductionError("previous Stable environment did not pass")
    evidence = {
        "release_id": _positive(release.get("id"), "previous Stable Release id"),
        "candidate_sha256": marker["candidate_sha256"],
        "asset_closure_sha256": hashlib.sha256(canonical_bytes(assets)).hexdigest(),
    }
    return {
        "accepted": True,
        "stage": "stable",
        "version": previous_version,
        "tag_name": previous_tag,
        "source_commit_sha": prior_source,
        "evidence_sha256": hashlib.sha256(canonical_bytes(evidence)).hexdigest(),
        "ancestry_verified": True,
    }
