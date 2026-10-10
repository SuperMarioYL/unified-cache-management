"""Catalog publication evidence without losing unfinished or orphaned versions."""

from __future__ import annotations

import importlib
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
catalog = importlib.import_module("ucm_release.cleanup_inventory")
domain = importlib.import_module("ucm_release.cleanup_records")
remote_ops = importlib.import_module("ucm_release.cleanup_remote")
versions = importlib.import_module("ucm_release.version_config")
REPOSITORY = "release-org/unified-cache-management"
PREFIX = f"/repos/{REPOSITORY}"
TAG = "nightly/v0.9.0-20261009-1"
SHA = "a" * 40


class RemoteError(remote_ops.RemoteError):
    def __init__(self, status):
        super().__init__(f"GitHub API HTTP {status}", status=status)


class FakeRemote(remote_ops.ProductionRemote):
    repository = REPOSITORY

    def __init__(self):
        self.releases = []
        self.refs = []
        self.tag_objects = {}
        self.workflows = {"release-nightly.yml": [], "release-tag.yml": []}
        self.runs = {}
        self.artifacts = {}
        self.jobs = {}
        self.downloads = {}
        self.download_accepts = {}
        self.errors = {}
        self.calls = []

    def json_request(self, method, path):
        self.calls.append((method, path))
        assert method == "GET"
        parsed = urlparse(path)
        path = parsed.path
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        start = (page - 1) * 100
        if path in self.errors:
            raise self.errors[path]
        if path == PREFIX + "/releases":
            return self.releases[start : start + 100]
        if path == PREFIX + "/git/matching-refs/tags/":
            return self.refs
        if path.startswith(PREFIX + "/git/ref/tags/"):
            tag = path.split("/git/ref/tags/", 1)[1]
            for ref in self.refs:
                if ref["ref"] == "refs/tags/" + tag:
                    return ref
            raise RemoteError(404)
        if path.startswith(PREFIX + "/releases/"):
            release_id = int(path.rsplit("/", 1)[-1])
            for release in self.releases:
                if release["id"] == release_id:
                    return release
            raise RemoteError(404)
        if "/git/tags/" in path:
            return self.tag_objects[path.rsplit("/", 1)[-1]]
        if "/actions/workflows/" in path:
            name = path.split("/actions/workflows/", 1)[1].split("/", 1)[0]
            runs = self.workflows.get(name, [])
            source_sha = parse_qs(parsed.query).get("head_sha", [None])[0]
            if source_sha is not None:
                runs = [run for run in runs if run["head_sha"] == source_sha]
            return {"workflow_runs": runs[start : start + 100]}
        if path.endswith("/artifacts"):
            run_id = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            return {"artifacts": self.artifacts.get(run_id, [])[start : start + 100]}
        if "/actions/runs/" in path and path.endswith("/jobs"):
            run_id = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            return {"jobs": self.jobs.get(run_id, [])[start : start + 100]}
        if "/actions/runs/" in path:
            run_id = int(path.rsplit("/", 1)[-1])
            if run_id not in self.runs:
                raise RemoteError(404)
            return self.runs[run_id]
        raise AssertionError(path)

    def request(self, method, path, *, accept="application/vnd.github+json"):
        self.calls.append((method, path))
        self.download_accepts[path] = accept
        assert method == "GET"
        if path in self.errors:
            raise self.errors[path]
        return self.downloads[path]

    def add_run(self, run, *, listed=True):
        self.runs[run["id"]] = run
        if listed:
            self.workflows[run["path"].rsplit("/", 1)[-1]].append(run)

    def add_asset(self, release, value, *, asset_id=10, name="release-cleanup.json"):
        release["assets"].append(
            {"name": name, "id": asset_id, "url": "https://untrusted.test/unused"}
        )
        self.downloads[f"{PREFIX}/releases/assets/{asset_id}"] = json.dumps(
            value
        ).encode()

    def add_artifact(
        self,
        run,
        record,
        *,
        artifact_id=100,
        phase="",
        attempt=1,
        extra=False,
        filename="release-cleanup.json",
        family="nightly",
    ):
        name = f"ucm-{family}-cleanup{phase}-run-{run['id']}-attempt-{attempt}"
        self.artifacts.setdefault(run["id"], []).append(
            {
                "id": artifact_id,
                "name": name,
                "expired": False,
                "workflow_run": {"id": run["id"]},
            }
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(filename, json.dumps(record))
            if extra:
                archive.writestr("../untrusted.py", "code")
        self.downloads[f"{PREFIX}/actions/artifacts/{artifact_id}/zip"] = (
            output.getvalue()
        )


def _release(tag=TAG, *, release_id=1, draft=True, tag_name=None, owned=True):
    return {
        "id": release_id,
        "tag_name": tag_name or tag,
        "name": tag,
        "draft": draft,
        "prerelease": True,
        "assets": [],
        "body": "Status: `artifacts-failed`" if owned else "user notes",
    }


def _ref(tag=TAG, *, sha=SHA, kind="commit"):
    return {"ref": "refs/tags/" + tag, "object": {"type": kind, "sha": sha}}


def _run(
    *,
    run_id=123,
    attempt=1,
    status="completed",
    event="schedule",
    path="release-nightly.yml",
    branch="develop",
    sha=SHA,
    repository=REPOSITORY,
):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "status": status,
        "event": event,
        "path": ".github/workflows/" + path,
        "head_branch": branch,
        "head_sha": sha,
        "head_repository": {"full_name": repository},
        "repository": {"full_name": REPOSITORY},
    }


def _legacy_record(
    tag=TAG, *, release_id=1, run_id=123, attempt=1, sha=SHA, resources=False
):
    # Legacy producer JSON has eight fields; internal normalization adds provenance.
    record = {
        "kind": "ucm-release-cleanup",
        "schema_version": 1,
        "repository": REPOSITORY,
        "tag": tag,
        "source_sha": sha,
        "release_ids": [] if release_id is None else [release_id],
        "runs": [{"id": run_id, "attempt": attempt}],
        "resources": [],
    }
    if resources:
        version = versions.classify_tag(tag)["image_version"]
        record["resources"] = [
            {
                "kind": "ghcr-member",
                "reference": f"ghcr.io/release-org/vllm-openai:v1-ucm-{version}",
            }
        ]
    return record


def _legacy_manifest(tag=TAG, run_id=123):
    return {
        "kind": "ucm-release-manifest",
        "schema_version": 6,
        "tag": tag,
        "release_type": "nightly",
        "actions_run_id": run_id,
        "chart_oci": None,
        "github_release_assets": ["release-manifest.json"],
        "runtime_images": {
            "ghcr": {"indexes": [], "members": []},
            "dockerhub": {"indexes": [], "members": []},
        },
    }


def _history(*records, blocked=None):
    return {
        "kind": "ucm-nightly-cleanup-inventory",
        "schema_version": 1,
        "repository": REPOSITORY,
        "targets": list(records),
        "evidence": {},
        "blocked": blocked or {},
    }


def _nightly_catalog(remote, history=None):
    return catalog.collect_catalog(remote, history, release_type="nightly")


def _snapshot(tag, *, run_id=123, sha=SHA, resources=False):
    document = domain.new_record(REPOSITORY, tag, sha, run_id, 1)
    if resources:
        version = versions.classify_tag(tag)["image_version"]
        document["resources"] = [
            {
                "kind": "ghcr-member",
                "reference": f"ghcr.io/release-org/vllm-openai:v1-ucm-{version}",
            }
        ]
    return document, domain.record_basename(document, 1, planned=resources)


@pytest.mark.parametrize(
    "release_type,tag", [("nightly", TAG), ("draft", "draft/v0.9.0-1")]
)
@pytest.mark.parametrize("storage", ["asset", "artifact"])
def test_immutable_snapshot_evidence_binds_any_release_type(release_type, tag, storage):
    remote = FakeRemote()
    release = _release(tag)
    remote.releases = [release]
    remote.refs = [_ref(tag)]
    run = _run(path="release-tag.yml", event="push", branch=tag)
    remote.add_run(run)
    document, name = _snapshot(tag, resources=True)
    if storage == "asset":
        remote.add_asset(release, document, name=name)
    else:
        remote.add_artifact(
            run, document, filename=name, family="release", phase="-plan"
        )

    result = catalog.collect_catalog(remote, release_type=release_type)

    assert result.records == (domain.decode_snapshot(document, REPOSITORY, name, 1),)
    assert result.blocked == {}
    assert result.active_tags == set()
    assert all(method == "GET" for method, _ in remote.calls)


@pytest.mark.parametrize(
    "release_type,tag", [("nightly", TAG), ("draft", "draft/v0.9.0-1")]
)
@pytest.mark.parametrize("storage", ["asset", "artifact"])
def test_moved_tag_keeps_ordinary_attempt_sources_and_blocks_nightly_conflicts(
    release_type, tag, storage
):
    remote = FakeRemote()
    release = _release(tag)
    remote.releases = [release]
    moved_sha = "b" * 40
    remote.refs = [_ref(tag, sha=moved_sha)]
    for index, sha in enumerate((SHA, moved_sha)):
        run_id = 123 + index
        run = _run(
            run_id=run_id, path="release-tag.yml", event="push", branch=tag, sha=sha
        )
        remote.add_run(run)
        document, name = _snapshot(tag, run_id=run_id, sha=sha, resources=True)
        document["resources"][0]["reference"] = document["resources"][0][
            "reference"
        ].replace("v1-", f"v{index + 1}-")
        if storage == "asset":
            remote.add_asset(release, document, name=name, asset_id=10 + index)
        else:
            remote.add_artifact(
                run,
                document,
                filename=name,
                family="release",
                phase="-plan",
                artifact_id=100 + index,
            )

    result = catalog.collect_catalog(remote, release_type=release_type)

    assert len(result.records) == 1
    if release_type == "nightly":
        assert "conflicting source SHAs" in result.blocked[tag]
    else:
        assert result.blocked == {}
        record = result.records[0]
        assert record["source_sha"] is None
        assert record["attempt_sources"] == [
            {"id": 123, "attempt": 1, "source_sha": SHA},
            {"id": 124, "attempt": 1, "source_sha": moved_sha},
        ]
        assert len(record["resources"]) == 2


@pytest.mark.parametrize(
    "release_type,tag", [("nightly", TAG), ("draft", "draft/v0.9.0-1")]
)
@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
def test_snapshot_active_tag_only_source_is_protected(release_type, tag, status):
    remote = FakeRemote()
    remote.refs = [_ref(tag)]
    run = _run(status=status, path="release-tag.yml", event="push", branch=tag)
    remote.add_run(run)
    document, name = _snapshot(tag)
    remote.add_artifact(run, document, filename=name, family="release")

    result = catalog.collect_catalog(remote, release_type=release_type)

    assert len(result.records) == 1
    assert result.active_tags == {tag}
    assert result.blocked == {}


def test_manifestless_failed_drafts_count_and_remain_explicitly_blocked():
    remote = FakeRemote()
    remote.releases = [_release(), _release("nightly/v0.9.0-20261008-1", release_id=2)]
    remote.refs = [_ref(), _ref("nightly/v0.9.0-20261008-1")]
    result = _nightly_catalog(remote)
    assert len(result.records) == 2
    assert set(result.blocked) == {item["tag"] for item in result.records}
    assert (
        domain.select_retention(result.records, 1)
        .candidates[0]["tag"]
        .endswith("20261008-1")
    )


def test_catalog_collects_release_run_and_evidence_beyond_first_page():
    remote = FakeRemote()
    remote.releases = [
        _release(f"v0.8.{number}", release_id=1000 + number) for number in range(100)
    ] + [_release()]
    remote.refs = [_ref()]
    remote.workflows["release-nightly.yml"] = [
        _run(run_id=1000 + number, repository="other/repository")
        for number in range(100)
    ]
    run = _run()
    remote.add_run(run)
    remote.artifacts[run["id"]] = [
        {"id": 1000 + number, "name": "unrelated-evidence"} for number in range(100)
    ]
    remote.add_artifact(run, _legacy_record(resources=True))

    result = _nightly_catalog(remote)

    assert result.records == (
        domain.validate_record(_legacy_record(resources=True), REPOSITORY),
    )
    assert result.blocked == {}
    for path in (
        PREFIX + "/releases",
        PREFIX + "/actions/workflows/release-nightly.yml/runs",
        PREFIX + "/actions/runs/123/artifacts",
    ):
        assert ("GET", path + "?per_page=100&page=2") in remote.calls
    assert all(method == "GET" for method, _ in remote.calls)


def test_supported_legacy_manifest_recovers_published_and_untagged_drafts():
    remote = FakeRemote()
    published = _release(draft=False)
    orphan = _release(
        "nightly/v0.9.0-20261008-1", release_id=2, tag_name="untagged-abcd", owned=False
    )
    remote.releases = [published, orphan]
    remote.add_asset(published, _legacy_manifest(), name="release-manifest.json")
    remote.add_asset(
        orphan,
        _legacy_manifest(orphan["name"], 124),
        asset_id=11,
        name="release-manifest.json",
    )
    result = _nightly_catalog(remote)
    assert len(result.records) == 2
    assert result.blocked == {}
    assert result.orphans == ()  # GitHub orphan Drafts still occupy quota.


def test_untagged_name_requires_pipeline_evidence_and_ordinary_drafts_stay_out():
    remote = FakeRemote()
    remote.releases = [
        _release(tag_name="untagged-a"),
        _release(
            "nightly/v0.9.0-20261008-1",
            release_id=2,
            tag_name="untagged-b",
            owned=False,
        ),
        _release("draft/v0.9.0", release_id=3),
    ]
    result = _nightly_catalog(remote)
    assert [record["tag"] for record in result.records] == [TAG]
    assert TAG in result.blocked


def test_live_producer_record_binds_release_source_run_and_actual_tag():
    remote = FakeRemote()
    release = _release()
    remote.releases = [release]
    remote.refs = [_ref()]
    remote.add_asset(release, _legacy_record(resources=True))
    remote.add_run(_run(), listed=False)
    result = _nightly_catalog(remote)
    assert len(result.records[0]["resources"]) == 1
    assert result.blocked == {}
    assert all(method == "GET" for method, _ in remote.calls)
    assert not any("untrusted.test" in path for _, path in remote.calls)


def test_actions_zip_download_uses_rest_media_type_and_release_assets_use_octet_stream():
    remote = FakeRemote()
    release = _release()
    remote.releases = [release]
    remote.add_asset(release, _legacy_record())
    run = _run()
    remote.add_run(run)
    remote.add_artifact(run, _legacy_record(release_id=None))

    result = _nightly_catalog(remote)

    assert result.blocked == {}
    assert remote.download_accepts[PREFIX + "/actions/artifacts/100/zip"] == (
        "application/vnd.github+json"
    )
    assert remote.download_accepts[PREFIX + "/releases/assets/10"] == (
        "application/octet-stream"
    )


@pytest.mark.parametrize(
    "change",
    ["release", "source", "repository", "caller_tag", "workflow", "future_attempt"],
)
def test_producer_record_binding_failures_block_the_version(change):
    remote = FakeRemote()
    release = _release()
    remote.releases = [release]
    remote.refs = [_ref()]
    record = _legacy_record(resources=True)
    run = _run()
    if change == "release":
        record["release_ids"] = [2]
    elif change == "source":
        run["head_sha"] = "b" * 40
    elif change == "repository":
        run["head_repository"]["full_name"] = "untrusted/repo"
    elif change == "caller_tag":
        run.update(
            path=".github/workflows/release-tag.yml",
            event="push",
            head_branch="nightly/v0.9.0-20261008-1",
        )
    elif change == "workflow":
        run["path"] = ".github/workflows/arbitrary.yml"
    else:
        record["runs"][0]["attempt"] = 2
    remote.add_asset(release, record)
    remote.runs[123] = run
    result = _nightly_catalog(remote)
    assert len(result.records) == 1
    assert TAG in result.blocked


def test_duplicate_release_records_merge_and_wrong_additional_release_id_is_blocked():
    remote = FakeRemote()
    first, second = _release(), _release(release_id=2)
    remote.releases = [first, second, _release("v0.9.0", release_id=3)]
    remote.add_asset(first, _legacy_record())
    remote.add_asset(second, _legacy_record(release_id=2), asset_id=11)
    result = _nightly_catalog(remote)
    assert len(result.records) == 1
    assert result.records[0]["release_ids"] == [1, 2]
    forged = _legacy_record()
    forged["release_ids"].append(3)
    remote.add_asset(
        first, forged, asset_id=12
    )  # Duplicate asset itself is also unsafe.
    result = _nightly_catalog(remote)
    assert TAG in result.blocked


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
def test_active_tag_only_source_is_protected_and_counts_toward_quota(status):
    remote = FakeRemote()
    remote.refs = [_ref()]
    run = _run(status=status, path="release-tag.yml", event="push", branch=TAG)
    remote.add_run(run)
    remote.add_artifact(run, _legacy_record(release_id=None))
    result = _nightly_catalog(remote)
    assert len(result.records) == 1
    assert result.active_tags == {TAG}
    assert result.blocked == {}


def test_unknown_active_nightly_caller_conservatively_protects_matching_source_sha():
    remote = FakeRemote()
    remote.refs = [_ref(), _ref("nightly/v0.9.0-20261008-1")]
    remote.add_run(_run(status="queued"))
    result = _nightly_catalog(remote)
    assert result.active_tags == {TAG, "nightly/v0.9.0-20261008-1"}


def test_prepare_artifact_without_a_live_tag_or_release_does_not_create_phantom_quota():
    remote = FakeRemote()
    run = _run()
    remote.add_run(run)
    remote.add_artifact(run, _legacy_record(release_id=None))
    result = _nightly_catalog(remote)
    assert result.records == result.orphans == ()


def test_plan_artifact_and_prior_attempts_recover_exact_union_after_attachment_failure():
    remote = FakeRemote()
    remote.refs = [_ref()]
    run = _run(attempt=2)
    remote.add_run(run)
    remote.add_artifact(
        run, _legacy_record(release_id=None), artifact_id=100, attempt=1
    )
    remote.add_artifact(
        run,
        _legacy_record(attempt=2, resources=True),
        artifact_id=101,
        phase="-plan",
        attempt=2,
    )
    result = _nightly_catalog(remote)
    assert result.blocked == {}
    assert result.records[0]["runs"] == [
        {"id": 123, "attempt": 1},
        {"id": 123, "attempt": 2},
    ]
    assert len(result.records[0]["resources"]) == 1


@pytest.mark.parametrize("defect", ["sha", "attempt", "tag", "zip"])
def test_artifact_binding_and_zip_shape_fail_closed(defect):
    remote = FakeRemote()
    remote.refs = [_ref()]
    run = _run(path="release-tag.yml", event="push", branch=TAG)
    remote.add_run(run)
    record = _legacy_record(release_id=None)
    if defect == "sha":
        record["source_sha"] = "b" * 40
    if defect == "attempt":
        record["runs"][0]["attempt"] = 2
    if defect == "tag":
        record["tag"] = "nightly/v0.9.0-20261008-1"
    remote.add_artifact(run, record, extra=defect == "zip")
    result = _nightly_catalog(remote)
    assert TAG in result.blocked
    assert len(result.records) == 1


def test_untrusted_workflow_run_artifacts_are_never_downloaded():
    remote = FakeRemote()
    remote.refs = [_ref()]
    run = _run(repository="attacker/repo")
    remote.add_run(run)
    remote.add_artifact(run, _legacy_record(release_id=None))
    result = _nightly_catalog(remote)
    assert TAG in result.blocked
    assert not any(path.endswith("/zip") for _, path in remote.calls)


def test_annotated_tag_is_resolved_and_binds_source_sha():
    remote = FakeRemote()
    remote.refs = [_ref(sha="b" * 40, kind="tag")]
    remote.tag_objects["b" * 40] = {"object": {"type": "commit", "sha": SHA}}
    result = _nightly_catalog(remote, _history(_legacy_record(release_id=None)))
    assert result.records[0]["source_sha"] == SHA
    assert result.blocked == {}


def test_history_orphans_are_outside_quota_and_explicit_missing_evidence_is_preserved():
    remote = FakeRemote()
    live = _legacy_record()
    old = _legacy_record(
        "nightly/v0.9.0-20261008-1", release_id=2, run_id=124, resources=True
    )
    blocked_tag = "nightly/v0.7.0-20260910-1"
    remote.releases = [_release()]
    result = _nightly_catalog(
        remote,
        _history(live, old, blocked={blocked_tag: "plan was removed before archival"}),
    )
    assert [record["tag"] for record in result.records] == [TAG]
    assert {record["tag"] for record in result.orphans} == {old["tag"], blocked_tag}
    assert result.blocked == {blocked_tag: "plan was removed before archival"}


def test_history_run_only_orphan_remains_cleanup_target_but_empty_snapshot_record_does_not():
    remote = FakeRemote()
    run_only = _legacy_record(release_id=None)
    empty = _legacy_record("nightly/v0.9.0-20261008-1", release_id=None)
    empty["runs"] = []
    result = _nightly_catalog(remote, _history(run_only, empty))
    assert result.records == ()
    assert [record["tag"] for record in result.orphans] == [TAG]


@pytest.mark.parametrize("mutation", ["repository", "schema", "fields"])
def test_historical_inventory_global_identity_is_strict(mutation):
    document = _history(_legacy_record())
    if mutation == "repository":
        document["repository"] = "another/repo"
    elif mutation == "schema":
        document["schema_version"] = 2
    else:
        document["extra"] = True
    with pytest.raises(domain.CleanupRecordError, match="historical cleanup inventory"):
        _nightly_catalog(FakeRemote(), document)


@pytest.mark.parametrize(
    "path",
    [
        "/releases",
        "/git/matching-refs/tags/",
        "/actions/workflows/release-nightly.yml/runs",
    ],
)
def test_global_403_prevents_cleanup_using_an_incomplete_catalog(path):
    remote = FakeRemote()
    remote.errors[PREFIX + path] = RemoteError(403)
    with pytest.raises(RemoteError, match="403"):
        _nightly_catalog(remote)


def test_per_version_asset_or_run_403_is_visible_and_other_versions_are_retained():
    remote = FakeRemote()
    first, second = _release(), _release("nightly/v0.9.0-20261008-1", release_id=2)
    remote.releases = [first, second]
    remote.add_asset(first, _legacy_record())
    remote.add_asset(
        second, _legacy_record(second["name"], release_id=2, run_id=124), asset_id=11
    )
    remote.errors[PREFIX + "/actions/runs/123"] = RemoteError(403)
    result = _nightly_catalog(remote)
    assert len(result.records) == 2
    assert TAG in result.blocked
    assert TAG in result.active_tags
    assert second["name"] not in result.blocked


def test_history_and_live_tag_source_conflict_is_not_silently_merged():
    remote = FakeRemote()
    remote.refs = [_ref(sha="b" * 40)]
    result = _nightly_catalog(remote, _history(_legacy_record(release_id=None)))
    assert TAG in result.blocked
    assert "conflicting source SHAs" in result.blocked[TAG]


def test_conflicting_asset_tag_cannot_invent_an_additional_quota_slot():
    remote = FakeRemote()
    release = _release()
    remote.releases = [release]
    remote.add_asset(release, _legacy_record("nightly/v0.9.0-20261008-1"))
    result = _nightly_catalog(remote)
    assert [record["tag"] for record in result.records] == [TAG]
    assert "conflicting identities" in result.blocked[TAG]
