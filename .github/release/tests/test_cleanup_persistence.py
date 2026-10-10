"""Release attempt snapshots remain immutable and locally recoverable."""

from __future__ import annotations

import copy
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ucm_release import cleanup_records
from ucm_release.cleanup_inventory import ReleaseInventory
from ucm_release.cleanup_records import CleanupError
from ucm_release.cleanup_remote import RemoteError

REPOSITORY = "release-org/unified-cache-management"
SOURCE_SHA = "a" * 40
NIGHTLY_TAG = "nightly/v0.9.0-20261009-1"
NIGHTLY_VERSION = "0.9.0.dev20261009001"


class MemoryRemote:
    """Keep GitHub assets in memory; require local recovery before upload."""

    repository = REPOSITORY
    api_base = "https://api.github.com"

    def __init__(self, release_type, tag, version, *, output_dir):
        self.release_type = release_type
        self.tag = tag
        self.version = version
        self.output_dir = output_dir
        self.events = []
        self.payloads = {}
        self.failure = None
        self.run = {
            "id": 42,
            "repository": {"full_name": REPOSITORY},
            "path": f".github/workflows/release-{'nightly' if release_type == 'nightly' else 'tag'}.yml",
            "head_sha": SOURCE_SHA,
            "head_branch": tag,
            "event": "schedule" if release_type == "nightly" else "push",
            "status": "in_progress",
            "run_attempt": 1,
        }
        self.release = {
            "id": 10,
            "tag_name": tag,
            "draft": True,
            "name": tag,
            "upload_url": (
                f"https://uploads.github.com/repos/{REPOSITORY}"
                "/releases/10/assets{?name,label}"
            ),
            "assets": [],
        }

    def json_request(self, method, path):
        self.events.append((method, path))
        assert method == "GET"
        if path == f"/repos/{REPOSITORY}/git/ref/tags/{self.tag}":
            return {"object": {"type": "commit", "sha": SOURCE_SHA}}
        assert path == f"/repos/{REPOSITORY}/releases/10"
        return copy.deepcopy(self.release)

    def read_release_run(self, run_id):
        assert run_id == self.run["id"]
        self.events.append(("GET", f"/repos/{REPOSITORY}/actions/runs/{run_id}"))
        return copy.deepcopy(self.run)

    def list_releases(self):
        self.events.append(("GET", f"/repos/{REPOSITORY}/releases"))
        return [copy.deepcopy(self.release)]

    def request(self, method, path, *, accept=None, data=None, content_type=None):
        self.events.append((method, path))
        if method == "POST":
            parsed = urllib.parse.urlsplit(path)
            assert parsed.path == f"/repos/{REPOSITORY}/releases/10/assets"
            name = urllib.parse.parse_qs(parsed.query)["name"][0]
            local_record = self.output_dir / name
            assert local_record.exists(), "local recovery must precede upload"
            assert data == local_record.read_bytes()
            assert content_type == "application/json"
            if self.failure == "upload":
                raise RemoteError("synthetic upload failure", status=503)
            asset = {"id": 51 + len(self.payloads), "name": name}
            self.payloads[asset["id"]] = data
            self.release["assets"].append(asset)
            return json.dumps(asset).encode()
        assert method == "GET", "snapshot persistence must never delete assets"
        assert accept == "application/octet-stream"
        assert path.startswith(f"/repos/{REPOSITORY}/releases/assets/")
        if self.failure == "readback":
            raise RemoteError("synthetic readback failure", status=503)
        asset_id = int(path.rsplit("/", 1)[-1])
        if self.failure == "mismatch":
            document = json.loads(self.payloads[asset_id])
            return json.dumps(dict(document, source_sha="b" * 40)).encode()
        return self.payloads[asset_id]


@pytest.fixture(
    params=[
        ("nightly", NIGHTLY_TAG, NIGHTLY_VERSION),
        ("draft", "draft/v0.9.0-1", "0.9.0.dev1"),
    ],
    ids=["nightly", "draft"],
)
def remote(request, tmp_path):
    return MemoryRemote(*request.param, output_dir=tmp_path / "out/cleanup")


def _reference(remote, family="v1"):
    return f"ghcr.io/release-org/vllm-openai:{family}-ucm-{remote.version}"


def _plan(remote, *, family="v1"):
    reference = _reference(remote, family)
    return {
        "kind": "ucm-release-plan",
        "repository": REPOSITORY,
        "route": "release",
        "release_type": remote.release_type,
        "git_tag": remote.tag,
        "version": remote.version,
        "publish": {
            "ghcr": {"enabled": True},
            "dockerhub": {"enabled": False},
            "chart_oci": {"enabled": False},
        },
        "families": [
            {
                "create_index": False,
                "published_reference": reference,
                "members": [{"cpu_arch": "amd64", "reference": reference}],
            }
        ],
    }


def _save(remote, plan=None):
    return ReleaseInventory(remote).save_record(
        remote.tag,
        remote.run["id"],
        SOURCE_SHA,
        plan,
        run_attempt=remote.run["run_attempt"],
        release_id=10,
        output_dir=remote.output_dir,
    )


@pytest.mark.parametrize("planned", [False, True], ids=["opened", "planned"])
def test_snapshot_is_saved_locally_uploaded_and_read_back(remote, planned):
    document = _save(remote, _plan(remote) if planned else None)
    name = f"release-cleanup-42-1-{'planned' if planned else 'opened'}.json"
    assert document == {
        "kind": "ucm-release-targets",
        "schema_version": 1,
        "repository": REPOSITORY,
        "tag": remote.tag,
        "release_type": remote.release_type,
        "run_id": 42,
        "source_sha": SOURCE_SHA,
        "version": remote.version,
        "resources": (
            [{"kind": "ghcr-member", "reference": _reference(remote)}]
            if planned
            else []
        ),
    }
    assert json.loads((remote.output_dir / name).read_bytes()) == document
    assert remote.release["assets"] == [{"id": 51, "name": name}]
    assert [
        (method, path)
        for method, path in remote.events
        if method != "GET" or "/releases/assets/" in path
    ] == [
        (
            "POST",
            f"https://uploads.github.com/repos/{REPOSITORY}/releases/10/assets?name={name}",
        ),
        ("GET", f"/repos/{REPOSITORY}/releases/assets/51"),
    ]


@pytest.mark.parametrize("planned", [False, True], ids=["opened", "planned"])
def test_repeating_same_attempt_stage_reads_its_existing_asset(remote, planned):
    plan = _plan(remote) if planned else None
    expected = _save(remote, plan)
    saved_payloads = dict(remote.payloads)
    remote.events.clear()
    assert _save(remote, plan) == expected
    assert remote.payloads == saved_payloads
    assert len(remote.release["assets"]) == 1
    assert all(method == "GET" for method, _ in remote.events)
    assert remote.events[-1] == (
        "GET",
        f"/repos/{REPOSITORY}/releases/assets/51",
    )


def test_conflicting_same_attempt_stage_cannot_replace_the_saved_snapshot(remote):
    _save(remote, _plan(remote))
    saved_payloads = dict(remote.payloads)
    remote.events.clear()
    with pytest.raises(CleanupError, match="snapshot conflicts"):
        _save(remote, _plan(remote, family="v2"))
    assert remote.payloads == saved_payloads
    assert len(remote.release["assets"]) == 1
    assert all(method == "GET" for method, _ in remote.events)


def test_all_attempts_and_stages_remain_available_for_inventory_union(remote):
    _save(remote)
    _save(remote, _plan(remote))
    first_attempt_payloads = dict(remote.payloads)
    remote.run["run_attempt"] = 2
    _save(remote)
    _save(remote, _plan(remote, family="v2"))
    assert all(
        remote.payloads[asset_id] == payload
        for asset_id, payload in first_attempt_payloads.items()
    )
    expected_names = {
        f"release-cleanup-42-{attempt}-{stage}.json"
        for attempt in (1, 2)
        for stage in ("opened", "planned")
    }
    assert {asset["name"] for asset in remote.release["assets"]} == expected_names
    assert {path.name for path in remote.output_dir.iterdir()} == expected_names
    inventory = ReleaseInventory(remote)
    records = [inventory.decode_asset(asset, 10) for asset in remote.release["assets"]]
    merged = cleanup_records.merge_records(records, REPOSITORY)[0]
    assert merged["release_ids"] == [10]
    assert merged["runs"] == [{"id": 42, "attempt": 1}, {"id": 42, "attempt": 2}]
    assert merged["resources"] == [
        {"kind": "ghcr-member", "reference": _reference(remote, family)}
        for family in ("v1", "v2")
    ]
    assert merged["attempt_sources"] == [
        {"id": 42, "attempt": attempt, "source_sha": SOURCE_SHA} for attempt in (1, 2)
    ]
    assert not any(method == "DELETE" for method, _ in remote.events)


@pytest.mark.parametrize("failure", ["upload", "readback", "mismatch"])
def test_failed_upload_or_readback_leaves_a_local_recovery_snapshot(remote, failure):
    remote.failure = failure
    with pytest.raises(CleanupError, match="failure|readback differs"):
        _save(remote, _plan(remote))
    output = remote.output_dir / "release-cleanup-42-1-planned.json"
    document = json.loads(output.read_bytes())
    recovered = cleanup_records.decode_snapshot(document, REPOSITORY, output.name)
    assert recovered["tag"] == remote.tag
    assert recovered["runs"] == [{"id": 42, "attempt": 1}]
    assert recovered["resources"] == [
        {"kind": "ghcr-member", "reference": _reference(remote)}
    ]
    assert any(method == "POST" for method, _ in remote.events)
    assert not any(method == "DELETE" for method, _ in remote.events)


@pytest.mark.parametrize("field,value", [("tag_name", "other-tag"), ("id", 11)])
def test_selected_release_must_match_its_tag_and_id_before_upload(remote, field, value):
    remote.release[field] = value
    if field == "tag_name":
        remote.release["name"] = "other-tag"
    with pytest.raises(CleanupError, match="does not match its Tag and ID"):
        _save(remote)
    assert remote.release["assets"] == []
    assert all(method == "GET" for method, _ in remote.events)


def test_historical_nightly_aggregate_remains_readable_without_rewriting(tmp_path):
    remote = MemoryRemote(
        "nightly", NIGHTLY_TAG, NIGHTLY_VERSION, output_dir=tmp_path / "out/cleanup"
    )
    remote.run["run_attempt"] = 2
    aggregate = {
        "kind": "ucm-release-cleanup",
        "schema_version": 1,
        "repository": REPOSITORY,
        "tag": NIGHTLY_TAG,
        "source_sha": SOURCE_SHA,
        "release_ids": [9, 10],
        "runs": [{"id": 42, "attempt": 1}, {"id": 42, "attempt": 2}],
        "resources": [{"kind": "ghcr-member", "reference": _reference(remote)}],
    }
    remote.payloads[50] = json.dumps(aggregate).encode()
    asset = {"id": 50, "name": "release-cleanup.json"}
    decoded = ReleaseInventory(remote).decode_asset(asset, 10)
    assert decoded["release_ids"] == aggregate["release_ids"]
    assert decoded["runs"] == aggregate["runs"]
    assert decoded["resources"] == aggregate["resources"]
    assert json.loads(remote.payloads[50]) == aggregate
    assert all(method == "GET" for method, _ in remote.events)
    assert not remote.output_dir.exists()
