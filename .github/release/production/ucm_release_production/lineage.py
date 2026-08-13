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
    require_lower_commit_sha,
    verify_envelope,
)
from .github_release import GitHubReleaseClient, GitHubNotFound
from .tags import TagIntent

_DRAFT = re.compile(
    r"draft/v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-(?P<number>[1-9][0-9]*)",
    re.ASCII,
)
_RC = re.compile(
    r"v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)rc(?P<number>[1-9][0-9]*)",
    re.ASCII,
)
_MANIFEST = "ucm-production-manifest.json"
_ENVIRONMENT = "ucm-production-environment.json"


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductionError(f"{label} must be a positive integer")
    return value


def _release_assets(
    client: GitHubReleaseClient, release: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    release_id = _positive(release.get("id"), "lineage Release id")
    values = client.list_release_assets(release_id)
    if not isinstance(values, list) or len(values) != 11:
        raise ProductionError("lineage Release must contain exactly eleven assets")
    by_name: dict[str, dict[str, Any]] = {}
    for value in values:
        item = _object(value, "lineage Release asset")
        name = item.get("name")
        if not isinstance(name, str) or name in by_name:
            raise ProductionError("lineage Release asset names are invalid")
        by_name[name] = item
    if _MANIFEST not in by_name or _ENVIRONMENT not in by_name:
        raise ProductionError("lineage Release support assets are missing")
    return by_name[_MANIFEST], by_name[_ENVIRONMENT]


def _asset_json(
    client: GitHubReleaseClient, asset: dict[str, Any], label: str
) -> dict[str, Any]:
    raw = client.download_release_asset(asset)
    value = _object(decode_json(raw, label), label)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    remote_digest = asset.get("digest")
    if remote_digest is not None and remote_digest != digest:
        raise ProductionError(f"{label} digest differs from GitHub")
    if asset.get("size") != len(raw):
        raise ProductionError(f"{label} size differs from GitHub")
    return value


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
    manifest_asset, environment_asset = _release_assets(client, release)
    manifest = verify_envelope(
        _asset_json(client, manifest_asset, "lineage production manifest"),
        kind="ucm-production-candidate-envelope",
        schema_version=1,
    )
    environment = verify_envelope(
        _asset_json(client, environment_asset, "lineage environment evidence"),
        kind="ucm-production-environment-evidence",
        schema_version=1,
    )
    if (
        manifest.get("stage") != expected_stage
        or manifest.get("tag_name") != tag_name
        or manifest.get("source_sha") != source_sha
        or manifest.get("repository") != client.repository
        or environment.get("source_sha") != source_sha
        or environment.get("status") not in {"passed", "waived-for-preview"}
    ):
        raise ProductionError("lineage Release asset identities differ")
    return {
        "release_id": _positive(release.get("id"), "lineage Release id"),
        "stage": expected_stage,
        "version": expected_version,
        "tag_name": tag_name,
        "source_commit_sha": source_sha,
        "candidate_sha256": manifest["sha256"],
        "environment_sha256": environment["sha256"],
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
    manifest_asset, environment_asset = _release_assets(client, release)
    manifest = verify_envelope(
        _asset_json(client, manifest_asset, "previous Stable manifest"),
        kind="ucm-production-candidate-envelope",
        schema_version=1,
    )
    environment = verify_envelope(
        _asset_json(client, environment_asset, "previous Stable environment"),
        kind="ucm-production-environment-evidence",
        schema_version=1,
    )
    if (
        manifest.get("stage") != "stable"
        or manifest.get("tag_name") != previous_tag
        or manifest.get("source_sha") != prior_source
        or environment.get("status") != "passed"
        or environment.get("source_sha") != prior_source
    ):
        raise ProductionError("previous Stable evidence differs")
    evidence = {
        "release_id": _positive(release.get("id"), "previous Stable Release id"),
        "candidate_sha256": manifest["sha256"],
        "environment_sha256": environment["sha256"],
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
