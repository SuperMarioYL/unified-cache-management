from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ucm_release_production.common import ProductionError, sha256_envelope
from ucm_release_production.github_release import (
    GitHubReleaseClient,
    GitHubNotFound,
    GitHubReleasePlan,
    GitHubResponseLost,
    ReleaseAsset,
    finalize_release,
    prepare_release,
    readback_release,
    upload_assets,
)
from ucm_release_production.common import canonical_bytes

REPOSITORY = "OctoCat/unified-cache-management"
SOURCE = "1" * 40


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _channels(stage: str, *, complete: bool = True) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for spec_id in (
        "cuda130-amd64",
        "cuda130-arm64",
        "cann900-a2-amd64",
        "cann900-a2-arm64",
        "cann900-a3-amd64",
        "cann900-a3-arm64",
    ):
        records.append(
            sha256_envelope(
                {
                    "kind": "ucm-production-channel-record",
                    "schema_version": 1,
                    "channel": "ghcr-member",
                    "stage": stage,
                    "status": (
                        "complete" if complete else "visibility-configuration-required"
                    ),
                    "spec_id": spec_id,
                }
            )
        )
    image_tag = "draft-v0.6.0-1" if stage == "draft" else "v0.6.0rc1"
    suffix = "-private" if stage == "draft" else ""
    for profile_id, image_name, digest_character in (
        ("cuda130", "ucm-cuda", "a"),
        ("cann900-a2", "ucm-cann-a2", "b"),
        ("cann900-a3", "ucm-cann-a3", "c"),
    ):
        repository = f"ghcr.io/octocat/{image_name}{suffix}"
        records.append(
            sha256_envelope(
                {
                    "kind": "ucm-production-channel-record",
                    "schema_version": 1,
                    "channel": "ghcr-index",
                    "stage": stage,
                    "status": "complete",
                    "profile_id": profile_id,
                    "repository": repository,
                    "reference": f"{repository}:{image_tag}",
                    "index_digest": "sha256:" + digest_character * 64,
                    "source_sha": SOURCE,
                }
            )
        )
    if stage != "draft":
        records.append(
            sha256_envelope(
                {
                    "kind": "ucm-production-channel-record",
                    "schema_version": 1,
                    "channel": "chart-oci",
                    "stage": stage,
                    "status": "complete",
                    "name": "unified-cache-pd",
                }
            )
        )
    return tuple(records)


def _plan(
    tmp_path: Path, *, stage: str = "rc", complete: bool = True
) -> GitHubReleasePlan:
    version = "0.6.0rc1" if stage == "rc" else "0.6.0.dev1"
    tag = "v0.6.0rc1" if stage == "rc" else "draft/v0.6.0-1"
    names = []
    for profile, platform in (
        ("cuda", "manylinux_2_28"),
        ("cann_a2", "linux"),
        ("cann_a3", "linux"),
    ):
        for arch in ("x86_64", "aarch64"):
            names.append(
                f"uc_manager_{profile}-{version}-cp312-cp312-{platform}_{arch}.whl"
            )
    chart_version = "0.6.0-rc.1" if stage == "rc" else "0.6.0-draft.1"
    names.append(f"unified-cache-pd-{chart_version}.tgz")
    assets: list[ReleaseAsset] = []
    for position, name in enumerate(names):
        path = tmp_path / name
        path.write_bytes(f"release-asset-{position}-{name}\n".encode())
        assets.append(ReleaseAsset.from_path(path))
    return GitHubReleasePlan(
        stage=stage,
        repository=REPOSITORY,
        repository_id=42,
        tag_name=tag,
        source_sha=SOURCE,
        version="0.6.0",
        candidate_sha256="2" * 64,
        environment_status="waived-for-preview",
        assets=tuple(assets),
        channel_records=_channels(stage, complete=complete),
    )


class FakeReleaseClient:
    def __init__(self) -> None:
        self.repository = REPOSITORY
        self.releases: list[dict[str, Any]] = []
        self.contents: dict[int, bytes] = {}
        self.operations: list[tuple[str, object]] = []
        self.next_release = 41
        self.next_asset = 501
        self.lose_create = False
        self.lose_upload_name: str | None = None
        self.lose_patch = False
        self.fail_upload_name: str | None = None

    def find_releases(
        self, tag_name: str, *, anonymous: bool = False
    ) -> list[dict[str, Any]]:
        self.operations.append(
            ("find-releases-anonymous" if anonymous else "find-releases", tag_name)
        )
        matches = [item for item in self.releases if item["tag_name"] == tag_name]
        if anonymous:
            matches = [item for item in matches if not item["draft"]]
        return [self._copy_release(item) for item in matches]

    def create_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.operations.append(("create-release", dict(payload)))
        release_id = self.next_release
        self.next_release += 1
        release = self._release(release_id, payload)
        self.releases.append(release)
        if self.lose_create:
            self.lose_create = False
            raise GitHubResponseLost("create response lost")
        return self._copy_release(release)

    def patch_release(self, release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.operations.append(("patch-release", (release_id, dict(payload))))
        release = self._by_id(release_id)
        release.update(payload)
        if self.lose_patch:
            self.lose_patch = False
            raise GitHubResponseLost("patch response lost")
        return self._copy_release(release)

    def list_release_assets(
        self, release_id: int, *, anonymous: bool = False
    ) -> list[dict[str, Any]]:
        self.operations.append(
            ("list-assets-anonymous" if anonymous else "list-assets", release_id)
        )
        release = self._by_id(release_id)
        if anonymous and release["draft"]:
            raise GitHubNotFound("draft assets are private")
        return [dict(item) for item in release["assets"]]

    def upload_release_asset(
        self,
        release_id: int,
        name: str,
        media_type: str,
        path: Path,
    ) -> dict[str, Any]:
        self.operations.append(("upload-asset", (release_id, name, media_type)))
        if name == self.fail_upload_name:
            raise ProductionError("simulated upload failure")
        release = self._by_id(release_id)
        raw = path.read_bytes()
        asset_id = self.next_asset
        self.next_asset += 1
        slug = (
            f"untagged-{release_id:020x}" if release["draft"] else release["tag_name"]
        )
        asset = {
            "id": asset_id,
            "name": name,
            "size": len(raw),
            "state": "uploaded",
            "digest": _digest(raw),
            "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}",
            "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/{slug}/{name}",
            "uploader": {"login": "github-actions[bot]", "type": "Bot"},
        }
        release["assets"].append(asset)
        self.contents[asset_id] = raw
        if name == self.lose_upload_name:
            self.lose_upload_name = None
            raise GitHubResponseLost("upload response lost")
        return dict(asset)

    def download_release_asset(
        self, asset: dict[str, Any], *, anonymous: bool = False
    ) -> bytes:
        self.operations.append(
            ("download-anonymous" if anonymous else "download", asset["id"])
        )
        release = next(item for item in self.releases if asset in item["assets"])
        if anonymous and release["draft"]:
            raise GitHubNotFound("draft asset is private")
        return self.contents[asset["id"]]

    def _by_id(self, release_id: int) -> dict[str, Any]:
        return next(item for item in self.releases if item["id"] == release_id)

    @staticmethod
    def _copy_release(release: dict[str, Any]) -> dict[str, Any]:
        return {**release, "assets": [dict(item) for item in release["assets"]]}

    @staticmethod
    def _release(release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": release_id,
            **payload,
            "url": f"https://api.github.com/repos/{REPOSITORY}/releases/{release_id}",
            "assets_url": f"https://api.github.com/repos/{REPOSITORY}/releases/{release_id}/assets",
            "upload_url": f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release_id}/assets{{?name,label}}",
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/untagged-{release_id:020x}",
            "author": {"login": "github-actions[bot]", "type": "Bot"},
            "assets": [],
        }


def test_absent_release_is_created_as_draft_and_resumes_exactly(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()

    first = prepare_release(plan, client)
    second = prepare_release(plan, client)

    assert first["decision"] == "create"
    assert second["decision"] == "resume-draft"
    assert first["release"]["draft"] is True
    assert first["release"]["prerelease"] is False
    assert sum(action == "create-release" for action, _ in client.operations) == 1


def test_release_description_has_copyable_tag_and_digest_image_pulls(
    tmp_path: Path,
) -> None:
    client = FakeReleaseClient()

    prepared = prepare_release(_plan(tmp_path, stage="draft"), client)

    assert (
        """Pull images from GHCR:

```bash
docker pull ghcr.io/octocat/ucm-cuda-private:draft-v0.6.0-1
docker pull ghcr.io/octocat/ucm-cann-a2-private:draft-v0.6.0-1
docker pull ghcr.io/octocat/ucm-cann-a3-private:draft-v0.6.0-1
```

Immutable image references:

```bash
docker pull ghcr.io/octocat/ucm-cuda-private@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
docker pull ghcr.io/octocat/ucm-cann-a2-private@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
docker pull ghcr.io/octocat/ucm-cann-a3-private@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
```"""
        in prepared["release"]["body"]
    )


def test_release_description_carries_versioned_lineage_marker(tmp_path: Path) -> None:
    client = FakeReleaseClient()

    prepared = prepare_release(_plan(tmp_path, stage="draft"), client)

    assert (
        '<!-- ucm-production-lineage-v1 {"candidate_sha256":"'
        + "2" * 64
        + '","environment_status":"waived-for-preview","source_sha":"'
        + SOURCE
        + '"} -->'
    ) in prepared["release"]["body"]


def test_prepare_recovers_exact_create_response_loss(tmp_path: Path) -> None:
    client = FakeReleaseClient()
    client.lose_create = True

    prepared = prepare_release(_plan(tmp_path), client)

    assert prepared["decision"] == "create-response-loss-recovered"
    assert len(client.releases) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target_commitish", "9" * 40, "source"),
        ("body", "foreign body", "body"),
        ("prerelease", True, "state"),
    ],
)
def test_prepare_rejects_wrong_source_marker_body_or_state(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    prepare_release(plan, client)
    client.releases[0][field] = value

    with pytest.raises(ProductionError, match=message):
        prepare_release(plan, client)
    assert sum(action == "create-release" for action, _ in client.operations) == 1


def test_duplicate_release_tag_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    prepare_release(plan, client)
    client.releases.append(client._copy_release(client.releases[0]))
    client.releases[-1]["id"] = 99

    with pytest.raises(ProductionError, match="duplicate"):
        prepare_release(plan, client)


def test_release_plan_rejects_backend_architecture_asset_substitution(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    wrong = tmp_path / "uc_manager_cuda-0.6.0rc1-cp312-cp312-linux_x86_64.whl"
    wrong.write_bytes(b"wrong-platform")

    with pytest.raises(ProductionError, match="asset set"):
        GitHubReleasePlan(
            stage=plan.stage,
            repository=plan.repository,
            repository_id=plan.repository_id,
            tag_name=plan.tag_name,
            source_sha=plan.source_sha,
            version=plan.version,
            candidate_sha256=plan.candidate_sha256,
            environment_status=plan.environment_status,
            assets=(ReleaseAsset.from_path(wrong), *plan.assets[1:]),
            channel_records=plan.channel_records,
        )


def test_uploads_exact_assets_and_identical_rerun_is_read_only(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()

    first = upload_assets(plan, client)
    before = list(client.operations)
    second = upload_assets(plan, client)

    assert len(first["assets"]) == 7
    assert [item["name"] for item in first["assets"]] == [
        asset.name for asset in plan.assets
    ]
    assert second["decision"] == "reuse-assets"
    assert not any(
        action == "upload-asset" for action, _ in client.operations[len(before) :]
    )


def test_asset_conflict_duplicate_id_and_foreign_name_block_before_upload(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    upload_assets(plan, client)
    release = client.releases[0]
    release["assets"][0]["digest"] = "sha256:" + "9" * 64
    before = len(client.operations)
    with pytest.raises(ProductionError, match="conflict"):
        upload_assets(plan, client)
    assert not any(action == "upload-asset" for action, _ in client.operations[before:])

    release["assets"][0]["digest"] = plan.assets[0].sha256
    duplicate = dict(release["assets"][0])
    duplicate["name"] = plan.assets[1].name
    duplicate["digest"] = plan.assets[1].sha256
    duplicate["size"] = plan.assets[1].size
    release["assets"][1] = duplicate
    with pytest.raises(ProductionError, match="duplicate.*id"):
        upload_assets(plan, client)

    release["assets"][1] = dict(release["assets"][0], id=777, name="foreign.bin")
    with pytest.raises(ProductionError, match="foreign"):
        upload_assets(plan, client)


def test_upload_response_loss_and_partial_publication_resume(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    client.lose_upload_name = plan.assets[2].name

    recovered = upload_assets(plan, client)
    assert recovered["decision"] == "create-assets-response-loss-recovered"

    client2 = FakeReleaseClient()
    client2.fail_upload_name = plan.assets[3].name
    with pytest.raises(ProductionError, match="simulated"):
        upload_assets(plan, client2)
    assert client2.releases[0]["draft"] is True
    assert len(client2.releases[0]["assets"]) == 3
    client2.fail_upload_name = None
    resumed = upload_assets(plan, client2)
    assert len(resumed["assets"]) == 7
    assert len({item["name"] for item in resumed["assets"]}) == 7


def test_rc_finalizes_only_after_channels_and_assets_then_anonymous_readback(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    upload_assets(plan, client)

    record = finalize_release(plan, client)

    assert record["status"] == "complete"
    assert record["release_state"] == "prerelease"
    assert record["visibility"] == "public"
    assert record["asset_count"] == 7
    assert all(item["anonymous_sha256"] == item["digest"] for item in record["assets"])
    schema = __import__("json").loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "production-channel-record.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(record)
    patch = next(
        value for action, value in client.operations if action == "patch-release"
    )
    assert patch[1] == {"draft": False, "prerelease": True, "make_latest": "false"}


def test_incomplete_required_channel_keeps_rc_release_draft(tmp_path: Path) -> None:
    plan = _plan(tmp_path, complete=False)
    client = FakeReleaseClient()
    upload_assets(plan, client)
    before = len(client.operations)

    with pytest.raises(ProductionError, match="mandatory channel"):
        finalize_release(plan, client)

    assert client.releases[0]["draft"] is True
    assert not any(
        action == "patch-release" for action, _ in client.operations[before:]
    )


def test_patch_response_loss_recovers_from_exact_final_state(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    upload_assets(plan, client)
    client.lose_patch = True

    record = finalize_release(plan, client)

    assert record["status"] == "complete"
    assert record["decision"] == "create"
    assert record["operations"][-1]["outcome"] == "response-loss-recovered"


def test_draft_stays_private_and_requires_anonymous_non_public_readback(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, stage="draft")
    client = FakeReleaseClient()

    record = finalize_release(plan, client)

    assert record["release_state"] == "draft"
    assert record["visibility"] == "private"
    assert record["anonymous_readback"] == {"status": "not-found"}
    assert not any(action == "patch-release" for action, _ in client.operations)


def test_already_final_identical_release_is_reused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    first = finalize_release(plan, client)
    before = len(client.operations)

    second = finalize_release(plan, client)

    assert first["release_id"] == second["release_id"]
    assert second["decision"] == "reuse"
    assert not any(
        action in {"create-release", "upload-asset", "patch-release"}
        for action, _ in client.operations[before:]
    )


def test_readback_rejects_asset_list_drift_and_download_byte_drift(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    client = FakeReleaseClient()
    finalize_release(plan, client)
    release = client.releases[0]
    removed = release["assets"].pop()
    with pytest.raises(ProductionError, match="asset set"):
        readback_release(plan, client)

    release["assets"].append(removed)
    asset_id = release["assets"][0]["id"]
    client.contents[asset_id] = b"changed"
    with pytest.raises(ProductionError, match="download"):
        readback_release(plan, client)


def test_real_client_uses_closed_api_upload_routes_and_lengths(tmp_path: Path) -> None:
    requests: list[tuple[str, str, dict[str, str], bytes | None]] = []
    release = FakeReleaseClient._release(
        41,
        {
            "tag_name": "v0.6.0rc1",
            "target_commitish": SOURCE,
            "name": "UCM v0.6.0rc1",
            "body": "body",
            "draft": True,
            "prerelease": False,
            "make_latest": "false",
        },
    )
    asset = {
        "id": 501,
        "name": "asset.bin",
        "size": 5,
        "state": "uploaded",
        "digest": _digest(b"bytes"),
        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/501",
        "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v0.6.0rc1/asset.bin",
        "uploader": {"login": "github-actions[bot]", "type": "Bot"},
    }

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        requests.append((method, url, dict(headers), body))
        if method == "POST" and "uploads.github.com" in url:
            raw = canonical_bytes(asset)
            return (
                201,
                {"content-type": "application/json", "content-length": str(len(raw))},
                raw,
            )
        raw = canonical_bytes(release)
        return (
            201 if method == "POST" else 200,
            {"content-type": "application/json", "content-length": str(len(raw))},
            raw,
        )

    client = GitHubReleaseClient(REPOSITORY, token="token", transport=transport)
    client.create_release({"tag_name": "v0.6.0rc1"})
    client.patch_release(41, {"draft": False})
    path = tmp_path / "asset.bin"
    path.write_bytes(b"bytes")
    client.upload_release_asset(41, path.name, "application/octet-stream", path)

    assert [(method, urllib_host(url)) for method, url, _, _ in requests] == [
        ("POST", "api.github.com"),
        ("PATCH", "api.github.com"),
        ("POST", "uploads.github.com"),
    ]
    for _, _, headers, body in requests:
        assert headers["authorization"] == "Bearer token"
        assert headers["content-length"] == str(len(body or b""))


def urllib_host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (302, {"location": "https://evil.example/steal"}, "redirect"),
        (
            200,
            {"content-type": "application/json", "content-length": "999999999"},
            "size",
        ),
        (201, {"content-type": "text/html"}, "JSON"),
    ],
)
def test_real_client_rejects_redirect_size_and_media_drift(
    status: int, headers: dict[str, str], message: str
) -> None:
    client = GitHubReleaseClient(
        REPOSITORY,
        token="token",
        transport=lambda method, url, request_headers, body: (status, headers, b"{}"),
        max_json_bytes=100,
    )

    with pytest.raises(ProductionError, match=message):
        client.create_release({"tag_name": "v0.6.0rc1"})


def test_real_client_rejects_cross_repo_asset_and_write_response_loss(
    tmp_path: Path,
) -> None:
    client = GitHubReleaseClient(
        REPOSITORY,
        token="token",
        transport=lambda method, url, headers, body: (_ for _ in ()).throw(
            TimeoutError()
        ),
    )
    path = tmp_path / "asset.bin"
    path.write_bytes(b"bytes")

    with pytest.raises(GitHubResponseLost):
        client.upload_release_asset(41, path.name, "application/octet-stream", path)
    with pytest.raises(ProductionError, match="asset"):
        client.download_release_asset(
            {"id": 1, "url": "https://api.github.com/repos/evil/fork/releases/assets/1"}
        )


def test_real_client_follows_one_official_asset_redirect_without_token() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    location = "https://release-assets.githubusercontent.com/github-production-release-asset/42/asset.bin?sig=fixed"

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        calls.append((method, url, dict(headers)))
        if len(calls) == 1:
            return 302, {"location": location}, b""
        return 200, {"content-length": "5"}, b"bytes"

    client = GitHubReleaseClient(REPOSITORY, token="token", transport=transport)
    raw = client.download_release_asset(
        {
            "id": 501,
            "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/501",
        }
    )

    assert raw == b"bytes"
    assert calls[1][1] == location
    assert "authorization" in calls[0][2]
    assert "authorization" not in calls[1][2]


def test_real_client_rejects_asset_redirect_to_unapproved_host() -> None:
    client = GitHubReleaseClient(
        REPOSITORY,
        token="token",
        transport=lambda method, url, headers, body: (
            302,
            {"location": "https://evil.example/steal"},
            b"",
        ),
    )

    with pytest.raises(ProductionError, match="unapproved"):
        client.download_release_asset(
            {
                "id": 501,
                "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/501",
            }
        )
