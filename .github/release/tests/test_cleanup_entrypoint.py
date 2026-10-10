"""The public cleanup CLI uses one lifecycle for every release type."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from test_retention_records import (
    REPOSITORY,
    SOURCE_SHA,
    FakeRemote,
    _release,
    _run_payload,
)
from ucm_release import cleanup
from ucm_release import cleanup_records as records
from ucm_release.cleanup_inventory import ReleaseInventory
from ucm_release.cleanup_remote import _GitHubRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
TYPES = ["draft", "nightly"]


def _tag(kind, number):
    return (
        f"draft/v0.8.0-{number}"
        if kind == "draft"
        else f"nightly/v0.8.0-202610{number:02d}-1"
    )


def _world(kind, count=3):
    remote = FakeRemote()
    for number in range(1, count + 1):
        tag = _tag(kind, number)
        run_id = 100 + number
        remote.runs[run_id] = dict(_run_payload(run_id), head_branch=tag)
        document = records.new_record(REPOSITORY, tag, SOURCE_SHA, run_id, 1)
        name = records.record_basename(document, 1)
        remote.payloads[number] = document
        remote.releases.append(
            _release(release_id=number, tag=tag, assets=[{"id": number, "name": name}])
        )
        remote.present.update(
            {
                tag,
                f"{tag}#{number}",
                f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
            }
        )
    return remote


@pytest.mark.parametrize("kind", TYPES)
def test_local_record_cli_saves_a_bound_snapshot_without_credentials(tmp_path, kind):
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "PYTHONPATH"}
    }
    command = [
        sys.executable,
        str(ROOT / "ucm_release/cleanup.py"),
        "record",
        "--repository",
        REPOSITORY,
        "--tag",
        _tag(kind, 1),
        "--source-sha",
        SOURCE_SHA,
        "--run-id",
        "101",
        "--run-attempt",
        "2",
        "--output-dir",
        str(tmp_path),
    ]
    result = subprocess.run(
        command, env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    output = tmp_path / "release-cleanup-101-2-opened.json"
    document = records.decode_snapshot(
        json.loads(output.read_text()), REPOSITORY, output.name
    )
    assert document["runs"] == [{"id": 101, "attempt": 2}]
    assert document["resources"] == []


@pytest.mark.parametrize("kind", TYPES)
def test_retention_cli_defers_active_excess_versions(tmp_path, monkeypatch, kind):
    remote = _world(kind)
    remote.runs[101]["status"] = "queued"
    monkeypatch.setattr(cleanup, "_production_remote", lambda arguments: remote)
    report = tmp_path / "report.json"
    status = cleanup.main(
        [
            "retention",
            "--repository",
            REPOSITORY,
            "--release-type",
            kind,
            "--max-count",
            "2",
            "--pypi-enabled",
            "false",
            "--report",
            str(report),
        ]
    )
    assert status == 0
    value = json.loads(report.read_text())
    assert value["status"] == "deferred"
    assert sorted(item["status"] for item in value["results"]) == [
        "deferred",
        "kept",
        "kept",
    ]
    assert not any(event[0] in {"POST", "delete"} for event in remote.events)


@pytest.mark.parametrize("kind", TYPES)
def test_one_blocked_version_does_not_prevent_independent_cleanup(
    tmp_path, monkeypatch, kind
):
    remote = _world(kind)
    remote.payloads[1] = dict(remote.payloads[1], repository="foreign/repo")
    monkeypatch.setattr(cleanup, "_production_remote", lambda arguments: remote)
    report = tmp_path / "report.json"
    status = cleanup.main(
        [
            "retention",
            "--repository",
            REPOSITORY,
            "--release-type",
            kind,
            "--max-count",
            "1",
            "--pypi-enabled",
            "false",
            "--report",
            str(report),
        ]
    )
    assert status == 1
    assert _tag(kind, 1) in remote.present
    assert _tag(kind, 2) not in remote.present
    assert {item["status"] for item in json.loads(report.read_text())["results"]} == {
        "kept",
        "blocked",
        "deleted",
    }


@pytest.mark.parametrize("kind", TYPES)
def test_tag_preview_uses_the_same_target_without_remote_mutation(
    tmp_path, monkeypatch, kind
):
    remote = _world(kind, 1)
    original = remote.present.copy()
    monkeypatch.setattr(cleanup, "_production_remote", lambda arguments: remote)
    report = tmp_path / "report.json"
    status = cleanup.main(
        [
            "tag",
            "--repository",
            REPOSITORY,
            "--tag",
            _tag(kind, 1),
            "--dry-run",
            "--report",
            str(report),
        ]
    )
    assert status == 0
    assert remote.present == original
    assert not any(event[0] in {"POST", "delete"} for event in remote.events)
    assert json.loads(report.read_text())["results"][0]["status"] == "would-delete"


@pytest.mark.parametrize("kind", TYPES)
def test_incomplete_release_snapshot_keeps_the_artifact_run_until_last(kind):
    remote = _world(kind, 1)
    saved = records.decode_snapshot(
        remote.payloads[1], REPOSITORY, "release-cleanup-101-1-opened.json", 1
    )
    remote.runs[101]["run_attempt"] = 2
    latest = records.record_for_tag(REPOSITORY, _tag(kind, 1), SOURCE_SHA, 101, 2, 1)
    target = (
        ReleaseInventory(remote)
        .resolve_record(records.merge_records([saved, latest], REPOSITORY)[0])
        .target
    )
    assert target.actions[0].holds_recovery_data is True
    assert target.releases[0].holds_recovery_data is False
    remote.payloads[2] = records.new_record(
        REPOSITORY, _tag(kind, 1), SOURCE_SHA, 101, 2
    )
    remote.releases[0]["assets"].append(
        {"id": 2, "name": "release-cleanup-101-2-opened.json"}
    )
    complete = (
        ReleaseInventory(remote)
        .resolve_record(records.merge_records([saved, latest], REPOSITORY)[0])
        .target
    )
    assert complete.actions[0].holds_recovery_data is False
    assert complete.releases[0].holds_recovery_data is True


def test_signed_download_redirect_does_not_forward_the_credential():
    request = urllib.request.Request(
        "https://api.github.com/repos/release-org/repo/actions/artifacts/1/zip",
        headers={
            "Authorization": "Bearer test-only",
            "Accept": "application/octet-stream",
        },
    )
    handler = _GitHubRedirectHandler()
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://blob.example.test/signed.zip"
    )
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("Accept") == "application/octet-stream"
    with pytest.raises(cleanup.CleanupError):
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://blob.example.test/file"
        )


@pytest.mark.parametrize("kind", TYPES)
def test_unowned_untagged_draft_with_the_same_name_is_not_a_cleanup_target(kind):
    remote = _world(kind, 1)
    manual = _release(release_id=99, tag="untagged-user")
    manual.update(name=_tag(kind, 1), draft=True, body="user notes")
    remote.releases.append(manual)
    record = records.decode_snapshot(
        remote.payloads[1], REPOSITORY, "release-cleanup-101-1-opened.json", 1
    )
    target = ReleaseInventory(remote).resolve_record(record).target
    assert [release.identifier for release in target.releases] == [1]
    assert not any(event[0] in {"POST", "delete"} for event in remote.events)


def test_pending_plan_does_not_cover_another_attempt_recovered_only_from_artifacts():
    from test_retention_records import TAG, _legacy_remote

    remote = _legacy_remote()
    remote.runs[101] = _run_payload(101)
    snapshot = records.new_record(REPOSITORY, TAG, SOURCE_SHA, 101, 1)
    snapshot["resources"] = [
        {
            "kind": "ghcr-member",
            "reference": "ghcr.io/release-org/vllm:v0.23.1-ucm-0.8.0.dev1",
        }
    ]
    record = records.decode_snapshot(
        snapshot, REPOSITORY, "release-cleanup-101-1-planned.json", 10
    )
    resolution = ReleaseInventory(remote).resolve_record(record)
    assert [item.run["id"] for item in resolution.pending_recovery] == [100]
    assert all(action.holds_recovery_data for action in resolution.target.actions)
    assert not resolution.target.releases[0].holds_recovery_data


def test_scheduled_artifact_preserves_the_frozen_tag_commit():
    import io
    import zipfile

    from ucm_release.cleanup_inventory import collect_catalog

    remote = _world("nightly", 1)
    run = remote.runs[101]
    run.update(
        path=".github/workflows/release-nightly.yml",
        event="schedule",
        head_branch="develop",
        head_sha="b" * 40,
    )
    remote.releases[0]["assets"] = []
    snapshot = remote.payloads[1]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("release-cleanup-101-1-opened.json", json.dumps(snapshot))
    remote.archives[50] = archive.getvalue()
    remote.artifacts[101] = [
        {
            "id": 50,
            "name": "ucm-release-cleanup-run-101-attempt-1",
            "expired": False,
            "workflow_run": {"id": 101},
        }
    ]
    catalog = collect_catalog(remote, release_type="nightly")
    assert catalog.blocked == {}
    assert catalog.records[0]["source_sha"] == SOURCE_SHA
    assert catalog.records[0]["runs"] == [{"id": 101, "attempt": 1}]


def test_legacy_preview_report_uses_the_resolved_resources(tmp_path, monkeypatch):
    from test_retention_records import TAG, _legacy_remote

    remote = _legacy_remote()
    monkeypatch.setattr(cleanup, "_production_remote", lambda arguments: remote)
    report = tmp_path / "report.json"
    assert (
        cleanup.main(
            [
                "tag",
                "--repository",
                REPOSITORY,
                "--tag",
                TAG,
                "--dry-run",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    result = json.loads(report.read_text())["results"][0]
    assert result["registry_references"] == 2
    assert result["run_ids"] == [100]
    assert result["release_ids"] == [10]
