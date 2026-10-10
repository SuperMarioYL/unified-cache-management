from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE_ROOT))
cleanup = importlib.import_module("ucm_release.cleanup")
records_ops = importlib.import_module("ucm_release.cleanup_records")
remote_ops = importlib.import_module("ucm_release.cleanup_remote")
manifest_ops = importlib.import_module("ucm_release.manifest")
inventory_ops = importlib.import_module("ucm_release.cleanup_inventory")
runtime_ops = importlib.import_module("ucm_release.runtime")
version_ops = importlib.import_module("ucm_release.version_config")


@pytest.mark.parametrize("script", ["cleanup.py", "release.py"])
def test_release_entrypoints_run_by_filename_without_pythonpath(tmp_path, script):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(RELEASE_ROOT / "ucm_release" / script), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def _manifest(
    tag="draft/v0.8.0-3",
    *,
    release_type="draft",
    chart="ghcr.io/release-org/charts/unified-cache-chart:0.8.0-draft.3",
    ghcr_members=None,
    ghcr_indexes=None,
    dockerhub_members=None,
    dockerhub_indexes=None,
):
    manifest = json.loads(
        (RELEASE_ROOT / "tests/fixtures/release-manifest.json").read_text()
    )
    manifest["release"].update(tag=tag, type=release_type, actions_run_id=12345)
    manifest["chart"]["oci"] = chart
    template = manifest["images"][0]
    manifest["images"] = []
    channels = {
        "ghcr": (
            (
                ghcr_members
                if ghcr_members is not None
                else [
                    "ghcr.io/release-org/vllm-openai:v0.23.0-amd64",
                    "ghcr.io/release-org/vllm-openai:v0.23.0-arm64",
                ]
            ),
            (
                ghcr_indexes
                if ghcr_indexes is not None
                else ["ghcr.io/release-org/vllm-openai:v0.23.0"]
            ),
        ),
        "dockerhub": (dockerhub_members or [], dockerhub_indexes or []),
    }
    for channel, (members, indexes) in channels.items():
        for reference in indexes or members:
            image = json.loads(json.dumps(template))
            image["id"] = f"{channel}-{len(manifest['images'])}"
            image["publications"] = {"ghcr": None, "dockerhub": None}
            selected = members if indexes else [reference]
            image["publications"][channel] = {
                "pull": reference,
                "multi_arch": bool(indexes),
                "members": [
                    {"architecture": architecture, "reference": member}
                    for architecture, member in zip(("amd64", "arm64"), selected)
                ],
            }
            manifest["images"].append(image)
    return manifest


def _record(manifest: dict[str, object], release_id: int) -> dict[str, object]:
    return records_ops.record_for_tag(
        "release-org/unified-cache-management",
        manifest["release"]["tag"],
        "a" * 40,
        manifest["release"]["actions_run_id"],
        1,
        release_id,
    )


def _target(manifest: dict[str, object], releases) -> records_ops.CleanupTarget:
    tag = manifest["release"]["tag"]
    run_id = manifest["release"]["actions_run_id"]
    run = cleanup.Resource(
        "actions-run",
        f"https://github.com/release-org/unified-cache-management/actions/runs/{run_id}",
        run_id,
    )
    return cleanup.CleanupTarget(
        tag, tuple(records_ops.registry_resources(manifest)), (run,), tuple(releases)
    )


class FakeRemote:
    repository = "release-org/unified-cache-management"
    owner = "release-org"
    api_base = "https://api.github.com"

    def __init__(
        self,
        *,
        present: set[str] | None = None,
        releases: list[cleanup.Resource] | None = None,
    ) -> None:
        self.present = set(present or set())
        self.releases = list(releases or [])
        self.probe_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.release_calls: list[str] = []
        self.probe_errors: dict[str, list[BaseException]] = defaultdict(list)
        self.delete_errors: dict[str, list[BaseException]] = defaultdict(list)
        self.release_errors: list[BaseException] = []

    read_release_run = remote_ops.ProductionRemote.read_release_run

    def ensure_idle(self, run_ids) -> None:
        pass

    def probe(self, resource: cleanup.Resource) -> object | None:
        self.probe_calls.append(resource.reference)
        if self.probe_errors[resource.reference]:
            raise self.probe_errors[resource.reference].pop(0)
        return resource.reference if resource.reference in self.present else None

    def delete(self, resource: cleanup.Resource, state: object) -> None:
        assert state == resource.reference
        self.delete_calls.append(resource.reference)
        if self.delete_errors[resource.reference]:
            raise self.delete_errors[resource.reference].pop(0)
        self.present.discard(resource.reference)

    def is_absent(self, resource: cleanup.Resource, state: object) -> bool:
        return resource.reference not in self.present

    def list_releases(self):
        tag = self.releases[0].reference.split("#", 1)[0] if self.releases else ""
        self.release_calls.append(tag)
        if self.release_errors:
            raise self.release_errors.pop(0)
        return [
            {
                "id": r.identifier,
                "tag_name": r.reference.split("#", 1)[0],
                "assets": (
                    [{"name": "release-manifest.json"}] if r.holds_recovery_data else []
                ),
            }
            for r in self.releases
        ]


def _control_references(manifest: dict[str, object]) -> tuple[str, str]:
    run = "https://github.com/release-org/unified-cache-management/actions/runs/" + str(
        manifest["release"]["actions_run_id"]
    )
    return run, str(manifest["release"]["tag"])


def test_rich_single_arch_pull_and_member_are_deleted_once() -> None:
    resources = records_ops.registry_resources(
        _manifest(
            ghcr_members=["ghcr.io/release-org/vllm-openai:v0.23.0-amd64"],
            ghcr_indexes=[],
        )
    )

    assert [
        (item.kind, item.reference)
        for item in resources
        if item.kind.startswith("ghcr-")
    ] == [
        (
            "ghcr-member",
            "ghcr.io/release-org/vllm-openai:v0.23.0-amd64",
        )
    ]


def test_manifest_rejects_duplicate_or_wrong_registry_references() -> None:
    duplicate = _manifest(
        ghcr_members=["ghcr.io/release-org/vllm:v1"],
        ghcr_indexes=["ghcr.io/release-org/vllm:v1"],
    )
    with pytest.raises(cleanup.CleanupError, match="must be unique"):
        manifest_ops.validate_manifest(duplicate)

    wrong_registry = _manifest(dockerhub_members=["ghcr.io/release-org/vllm:v1"])
    with pytest.raises(cleanup.CleanupError, match="docker.io"):
        manifest_ops.validate_manifest(wrong_registry)


def test_release_discovery_preserves_all_exact_tag_release_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    releases = [
        {
            "id": 8,
            "tag_name": manifest["release"]["tag"],
            "assets": [
                {
                    "name": "release-manifest.json",
                    "url": "https://api.github.test/assets/8",
                }
            ],
        },
        {"id": 9, "tag_name": manifest["release"]["tag"], "assets": []},
        {"id": 10, "tag_name": "draft/v0.8.0-2", "assets": []},
    ]
    monkeypatch.setattr(remote, "list_releases", lambda: releases)
    resources = inventory_ops.ReleaseInventory(remote).resolve_releases(
        str(manifest["release"]["tag"])
    )
    assert [(item.identifier, item.holds_recovery_data) for item in resources] == [
        (8, True),
        (9, False),
    ]


def test_retention_counts_each_tag_once_including_old_schemas() -> None:
    records = [
        _record(_manifest("draft/v0.8.0-1"), 1),
        _record(_manifest("draft/v0.8.0-1"), 2),
        _record(_manifest("draft/v0.8.0-2"), 3),
        _record(_manifest("draft/v0.8.0-3"), 4),
        _record(_manifest("draft/v0.8.0-4"), 7),
        _record(
            _manifest("v0.8.0rc1", release_type="prerelease"),
            5,
        ),
    ]
    old_schema = _manifest("draft/v0.7.0-1")
    old_schema["schema_version"] = 5
    records.append(_record(old_schema, 6))

    selection = records_ops.select_retention(
        records,
        current_tag="draft/v0.8.0-4",
        release_type="draft",
        max_count=3,
        pypi_enabled=False,
    )

    assert [record["tag"] for record in selection.candidates] == [
        "draft/v0.8.0-1",
        "draft/v0.8.0-2",
    ]
    assert selection.skipped_reason is None


def test_retention_counts_failed_nightly_without_a_manifest() -> None:
    failed = _record(_manifest("nightly/v0.8.1-20260825-1", release_type="nightly"), 1)
    successful = _record(
        _manifest("nightly/v0.8.1-20260825-2", release_type="nightly"), 2
    )
    latest = _record(_manifest("nightly/v0.8.1-20260826-1", release_type="nightly"), 3)
    selection = records_ops.select_retention(
        [failed, successful, latest],
        release_type="nightly",
        max_count=2,
        pypi_enabled=False,
    )
    assert selection.candidates == (failed,)


@pytest.mark.parametrize(
    ("max_count", "pypi_enabled"),
    [(-1, False), (7, True)],
)
def test_retention_skip_does_not_query_remote_manifests(
    max_count: int, pypi_enabled: bool
) -> None:
    class NoReadRemote:
        def list_release_records(self):
            raise AssertionError("retention skip must not query Releases")

    arguments = SimpleNamespace(
        current_tag="draft/v0.8.0-3",
        release_type="draft",
        max_count=max_count,
        pypi_enabled=pypi_enabled,
        fail_resource=None,
    )

    assert cleanup._run_retention(arguments, NoReadRemote()) == []


def test_retry_reprobes_and_waits_zero_five_fifteen_before_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    resource = cleanup.Resource("ghcr-member", "ghcr.io/release-org/vllm:v1")
    remote = FakeRemote(present={resource.reference})
    remote.delete_errors[resource.reference] = [
        cleanup.RemoteError("HTTP 503 one", status=503),
        cleanup.RemoteError("HTTP 429 two", status=429),
    ]
    sleeps: list[float] = []

    failure = cleanup.delete_resource_with_retry(
        remote, resource, sleeper=sleeps.append
    )

    assert failure is None
    assert remote.probe_calls == [resource.reference] * 3
    assert remote.delete_calls == [resource.reference] * 3
    assert sleeps == [5.0, 15.0]
    log = capsys.readouterr().out
    assert f"reference={resource.reference}" in log
    assert "attempt=1/3 delay=0s" in log
    assert "attempt=2/3 delay=5s" in log
    assert "attempt=3/3 delay=15s" in log


def test_404_is_idempotent_and_permanent_errors_do_not_retry() -> None:
    missing = cleanup.Resource("git-tag", "draft/v0.8.0-3")
    missing_remote = FakeRemote()
    missing_remote.probe_errors[missing.reference] = [
        cleanup.RemoteError("not found", status=404)
    ]

    assert (
        cleanup.delete_resource_with_retry(
            missing_remote, missing, sleeper=lambda _: None
        )
        is None
    )
    assert missing_remote.delete_calls == []

    for status in (400, 401, 403, 422):
        resource = cleanup.Resource("github-release", f"tag#{status}")
        remote = FakeRemote(present={resource.reference})
        remote.delete_errors[resource.reference] = [
            cleanup.RemoteError(f"HTTP {status}", status=status)
        ]
        sleeps: list[float] = []

        failure = cleanup.delete_resource_with_retry(
            remote, resource, sleeper=sleeps.append
        )

        assert failure is not None
        assert failure.attempts == 1
        assert remote.probe_calls == [resource.reference]
        assert sleeps == []


def test_dockerhub_503_attempts_three_times_and_stage_one_continues_then_blocks() -> (
    None
):
    manifest = _manifest(dockerhub_members=["docker.io/release-org/vllm:v1"])
    phase_one = records_ops.registry_resources(manifest)
    run, tag = _control_references(manifest)
    release = cleanup.Resource("github-release", f"{tag}#99", 99)
    present = {item.reference for item in phase_one} | {run, tag, release.reference}
    remote = FakeRemote(present=present, releases=[release])
    target = next(
        item.reference for item in phase_one if item.kind.startswith("dockerhub-")
    )
    sleeps: list[float] = []

    report = cleanup.execute_cleanup(
        _target(manifest, remote.releases),
        remote,
        sleeper=sleeps.append,
        fail_resource=target,
    )

    assert report.completed is False
    assert report.stopped_phase == 1
    assert report.failures[0].resource.reference == target
    assert report.failures[0].attempts == 3
    # DockerHub retries retain the first resolved digest instead of probing the Tag again.
    assert remote.probe_calls.count(target) == 1
    assert target not in remote.delete_calls
    assert all(
        item.reference in remote.delete_calls
        for item in phase_one
        if item.reference != target
    )
    assert run not in remote.probe_calls
    assert tag in remote.present and release.reference in remote.present
    assert sleeps == [5.0, 15.0]


def _recovery_fixture(release_type, kind="ghcr-member"):
    tag = "draft/v0.8.0-3" if release_type == "draft" else "nightly/v0.8.0-20261010-1"
    coordinates = version_ops.classify_tag(tag)
    image_tag = f"v1-ucm-{runtime_ops.oci_tag_version(coordinates['version'])}"
    reference = (
        f"ghcr.io/release-org/charts/unified-cache-chart:{coordinates['chart_version']}"
        if kind == "chart-oci"
        else f"ghcr.io/release-org/vllm-openai:{image_tag}"
    )
    registry = (
        cleanup.Resource(kind, reference),
        cleanup.Resource(
            "dockerhub-member", f"docker.io/release-org/vllm-openai:{image_tag}"
        ),
    )
    actions = tuple(
        cleanup.Resource(
            "actions-run",
            f"https://github.com/release-org/unified-cache-management/actions/runs/{run_id}",
            run_id,
            holds_recovery_data=run_id == 12346,
        )
        for run_id in (12345, 12346)
    )
    releases = tuple(
        cleanup.Resource(
            "github-release",
            f"{tag}#{release_id}",
            release_id,
            holds_recovery_data=release_id == 2,
        )
        for release_id in (1, 2)
    )
    target = cleanup.CleanupTarget(tag, registry, actions, releases)
    record = records_ops.record_for_tag(
        FakeRemote.repository, tag, "a" * 40, 12345, 1, 2
    )
    record["runs"].append({"id": 12346, "attempt": 1})
    record["release_ids"].append(1)
    record["resources"] = [
        {"kind": resource.kind, "reference": resource.reference}
        for resource in registry
    ]
    record = records_ops.validate_record(record, FakeRemote.repository)
    resources = (*registry, *actions, *releases)
    remote = FakeRemote(
        present={tag} | {resource.reference for resource in resources},
        releases=list(releases),
    )
    return record, target, remote


@pytest.mark.parametrize("release_type", ["draft", "nightly"])
@pytest.mark.parametrize(
    "kind,fault",
    [
        ("chart-oci", "probe-400"),
        ("ghcr-member", "delete-403"),
        ("ghcr-index", "readback-503"),
        ("ghcr-member", "shared-tag"),
    ],
)
def test_ghcr_failures_skip_only_that_resource_and_preserve_recovery_for_retry(
    release_type, kind, fault
):
    _, target, remote = _recovery_fixture(release_type, kind)
    ghcr, dockerhub = target.registry
    if fault == "probe-400":
        remote.probe_errors[ghcr.reference] = [
            cleanup.RemoteError("HTTP 400", status=400)
        ]
    elif fault == "delete-403":
        remote.delete_errors[ghcr.reference] = [
            cleanup.RemoteError("HTTP 403", status=403)
        ]
    elif fault == "shared-tag":
        remote.probe_errors[ghcr.reference] = [
            remote_ops.UnsafePackageVersion(ghcr.reference, ["shared-latest"])
        ]
    else:

        class ReadbackUnavailableRemote(FakeRemote):
            def is_absent(self, resource, state):
                if resource.reference == ghcr.reference:
                    # DELETE removed the version, but neither confirmation nor
                    # subsequent probes can establish absence during this attempt.
                    self.probe_errors[ghcr.reference] = [
                        cleanup.RemoteError("HTTP 503 read unavailable", status=503)
                        for _ in range(2)
                    ]
                    raise cleanup.RemoteError(
                        "HTTP 503 readback unavailable", status=503
                    )
                return super().is_absent(resource, state)

        remote = ReadbackUnavailableRemote(
            present=remote.present, releases=remote.releases
        )
    sleeps = []

    report = cleanup.execute_cleanup(target, remote, sleeper=sleeps.append)

    assert report.completed is False
    assert report.stopped_phase is None
    assert report.failures == ()
    assert len(report.skipped) == 1
    skipped = report.skipped[0]
    assert skipped.resource == ghcr
    assert skipped.attempts == (3 if fault == "readback-503" else 1)
    assert sleeps == ([5.0, 15.0] if fault == "readback-503" else [])
    assert dockerhub.reference not in remote.present
    assert target.tag not in remote.present
    for resource in (*target.actions, *target.releases):
        if resource.holds_recovery_data:
            assert resource.reference in remote.present
            assert resource.reference not in remote.probe_calls
        else:
            assert resource.reference not in remote.present
    if fault in {"probe-400", "shared-tag"}:
        assert ghcr.reference not in remote.delete_calls
    if fault == "readback-503":
        assert remote.probe_calls.count(ghcr.reference) == 3
        assert remote.delete_calls.count(ghcr.reference) == 1
        assert ghcr.reference not in remote.present

    retry = cleanup.execute_cleanup(target, remote, sleeper=lambda _: None)

    assert retry.completed is True
    assert retry.failures == retry.skipped == ()
    assert remote.present == set()


def test_dockerhub_failure_remains_blocking_when_ghcr_is_skipped(tmp_path, monkeypatch):
    record, target, remote = _recovery_fixture("draft")
    ghcr, dockerhub = target.registry
    remote.probe_errors[ghcr.reference] = [
        cleanup.RemoteError("GHCR HTTP 403", status=403)
    ]
    remote.delete_errors[dockerhub.reference] = [
        cleanup.RemoteError("DockerHub HTTP 403", status=403)
    ]

    report = cleanup.execute_cleanup(target, remote, sleeper=lambda _: None)

    assert report.completed is False
    assert report.stopped_phase == 1
    assert [failure.resource for failure in report.failures] == [dockerhub]
    assert [failure.resource for failure in report.skipped] == [ghcr]
    assert all(
        resource.reference in remote.present
        for resource in (*target.actions, *target.releases)
    )
    assert target.tag in remote.present

    remote.delete_calls.clear()
    remote.probe_errors[ghcr.reference] = [
        cleanup.RemoteError("GHCR HTTP 403", status=403)
    ]
    remote.delete_errors[dockerhub.reference] = [
        cleanup.RemoteError("DockerHub HTTP 403", status=403)
    ]
    _install_recovery_inventory(monkeypatch, record, target, remote, [])
    report_path = tmp_path / "report.json"

    assert cleanup.main(["tag", "--tag", target.tag, "--report", str(report_path)]) == 1

    result = json.loads(report_path.read_text())
    assert result["status"] == "failed"
    assert result["results"][0]["status"] == "blocked"
    assert [issue["reference"] for issue in result["results"][0]["failures"]] == [
        dockerhub.reference
    ]
    assert [issue["reference"] for issue in result["results"][0]["skipped"]] == [
        ghcr.reference
    ]


def _install_recovery_inventory(monkeypatch, record, target, remote, saved):
    class SelectedInventory:
        def __init__(self, client):
            assert client is remote

        def resolve_record(self, selected):
            assert selected == record
            return inventory_ops.CleanupResolution(target)

        def persist_recovery(self, resolution):
            assert remote.delete_calls == []
            saved.append(resolution)

    monkeypatch.setattr(cleanup, "_production_remote", lambda _: remote)
    monkeypatch.setattr(cleanup, "ReleaseInventory", SelectedInventory)
    monkeypatch.setattr(
        cleanup,
        "collect_catalog",
        lambda *args, **kwargs: SimpleNamespace(
            records=[record], orphans=[], active_tags=set(), blocked={}
        ),
    )


@pytest.mark.parametrize("release_type", ["draft", "nightly"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["partial", "preview"])
def test_ghcr_skip_cli_succeeds_and_reports_the_unconfirmed_resource(
    tmp_path, monkeypatch, release_type, dry_run
):
    record, target, remote = _recovery_fixture(release_type)
    ghcr, dockerhub = target.registry
    remote.probe_errors[ghcr.reference] = [
        cleanup.RemoteError("HTTP 403 denied", status=403)
    ]
    saved = []
    _install_recovery_inventory(monkeypatch, record, target, remote, saved)
    report_path, summary_path = tmp_path / "report.json", tmp_path / "summary.md"
    original = remote.present.copy()
    arguments = [
        "tag",
        "--repository",
        remote.repository,
        "--tag",
        target.tag,
        "--report",
        str(report_path),
        "--summary",
        str(summary_path),
    ]
    if dry_run:
        arguments.append("--dry-run")

    assert cleanup.main(arguments) == 0

    report = json.loads(report_path.read_text())
    assert report["status"] == ("dry-run" if dry_run else "partial")
    result = report["results"][0]
    assert result["status"] == ("would-delete" if dry_run else "partial")
    assert result.get("failures", []) == []
    assert result["skipped"] == [
        {
            "kind": ghcr.kind,
            "reference": ghcr.reference,
            "attempts": 1,
            "error": "HTTP 403 denied",
        }
    ]
    summary = summary_path.read_text()
    assert "skipped GHCR resources" in summary
    assert ghcr.reference in summary and "HTTP 403 denied" in summary
    if dry_run:
        assert saved == []
        assert remote.delete_calls == []
        assert remote.present == original
        assert dockerhub.reference in remote.probe_calls
    else:
        assert len(saved) == 1
        assert dockerhub.reference not in remote.present
        assert target.tag not in remote.present


def test_actions_failure_blocks_tag_and_releases() -> None:
    manifest = _manifest(
        chart=None, ghcr_members=[], ghcr_indexes=[], dockerhub_members=[]
    )
    run, tag = _control_references(manifest)
    release = cleanup.Resource("github-release", f"{tag}#99", 99)
    remote = FakeRemote(present={run, tag, release.reference}, releases=[release])
    remote.delete_errors[run] = [
        cleanup.RemoteError("conflict", status=409),
        cleanup.RemoteError("conflict", status=409),
        cleanup.RemoteError("conflict", status=409),
    ]

    report = cleanup.execute_cleanup(
        _target(manifest, remote.releases), remote, sleeper=lambda _: None
    )

    assert report.stopped_phase == 2
    assert remote.probe_calls.count(run) == 3
    assert tag not in remote.probe_calls
    assert remote.release_calls == []


def test_tag_failure_keeps_release_manifest_and_rerun_recovers_from_404() -> None:
    manifest = _manifest(chart=None, ghcr_members=[], ghcr_indexes=[])
    run, tag = _control_references(manifest)
    release = cleanup.Resource("github-release", f"{tag}#99", 99)
    remote = FakeRemote(present={run, tag, release.reference}, releases=[release])
    remote.delete_errors[tag] = [
        cleanup.RemoteError("server", status=503),
        cleanup.RemoteError("server", status=503),
        cleanup.RemoteError("server", status=503),
    ]

    first = cleanup.execute_cleanup(
        _target(manifest, remote.releases), remote, sleeper=lambda _: None
    )

    assert first.stopped_phase == 3
    assert run not in remote.present
    assert tag in remote.present
    assert release.reference in remote.present
    assert remote.release_calls == []

    second = cleanup.execute_cleanup(
        _target(manifest, remote.releases), remote, sleeper=lambda _: None
    )

    assert second.completed is True
    assert tag not in remote.present
    assert release.reference not in remote.present
    assert remote.probe_calls.count(run) == 2


def test_release_ids_are_frozen_before_tag_deletion() -> None:
    manifest = _manifest(chart=None, ghcr_members=[], ghcr_indexes=[])
    run, tag = _control_references(manifest)
    release = cleanup.Resource("github-release", f"{tag}#99", 99)

    class TagDependentRemote(FakeRemote):
        def list_releases(self):
            releases = super().list_releases()
            return [
                release for release in releases if release["tag_name"] in self.present
            ]

    remote = TagDependentRemote(
        present={run, tag, release.reference}, releases=[release]
    )

    report = cleanup.execute_cleanup(
        _target(manifest, remote.releases), remote, sleeper=lambda _: None
    )

    assert report.completed is True
    assert release.reference in remote.delete_calls
    assert release.reference not in remote.present
    assert remote.release_calls == []


def test_executor_uses_known_release_id_without_tag_discovery() -> None:
    tag = "nightly/v0.8.0-20260825-1"
    run = cleanup.Resource(
        "actions-run",
        "https://github.com/release-org/unified-cache-management/actions/runs/12345",
        12345,
        holds_recovery_data=True,
    )
    release = cleanup.Resource("github-release", f"{tag}#99", 99)
    remote = FakeRemote(present={run.reference, tag, release.reference})
    target = cleanup.CleanupTarget(tag, (), (run,), (release,))

    report = cleanup.execute_cleanup(target, remote, sleeper=lambda _: None)

    assert report.completed is True
    assert release.reference in remote.delete_calls
    assert remote.delete_calls == [tag, release.reference, run.reference]
    assert remote.release_calls == []


def test_release_probe_uses_id_after_tag_name_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    release = cleanup.Resource("github-release", "nightly/v0.8.0-20260825-1#99", 99)
    path = "/repos/release-org/unified-cache-management/releases/99"
    calls: list[tuple[str, str]] = []

    def github_json(method: str, request_path: str):
        calls.append((method, request_path))
        return {"id": 99, "tag_name": "untagged-example"}

    monkeypatch.setattr(remote, "json_request", github_json)

    assert remote.probe(release) == path
    assert calls == [("GET", path)]


def test_release_probe_rejects_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    release = cleanup.Resource("github-release", "nightly/v0.8.0-20260825-1#99", 99)
    monkeypatch.setattr(remote, "json_request", lambda method, path: [])

    with pytest.raises(cleanup.CleanupError):
        remote.probe(release)


@pytest.mark.parametrize("delete_status", [204, 404])
def test_release_deletion_requires_readback_to_confirm_absence(
    monkeypatch: pytest.MonkeyPatch, delete_status: int
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    release = cleanup.Resource("github-release", "nightly/v0.8.0-20260825-1#99", 99)
    path = "/repos/release-org/unified-cache-management/releases/99"
    calls: list[tuple[str, str]] = []

    def github_json(method: str, request_path: str):
        calls.append((method, request_path))
        if method == "DELETE":
            if delete_status == 404:
                raise cleanup.RemoteError("not found", status=404)
            return None
        return {"id": 99, "tag_name": "nightly/v0.8.0-20260825-1"}

    monkeypatch.setattr(remote, "json_request", github_json)

    failure = cleanup.delete_resource_with_retry(
        remote, release, sleeper=lambda _: None
    )

    assert failure is not None
    assert failure.attempts == 3
    assert calls == [("GET", path), ("DELETE", path), ("GET", path)] * 3


def test_release_deletion_succeeds_after_readback_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    release = cleanup.Resource("github-release", "nightly/v0.8.0-20260825-1#99", 99)
    calls: list[str] = []

    def github_json(method: str, path: str):
        calls.append(method)
        if method == "DELETE":
            return None
        if calls == ["GET"]:
            return {"id": 99, "tag_name": "nightly/v0.8.0-20260825-1"}
        raise cleanup.RemoteError("not found", status=404)

    monkeypatch.setattr(remote, "json_request", github_json)

    assert (
        cleanup.delete_resource_with_retry(remote, release, sleeper=lambda _: None)
        is None
    )
    assert calls == ["GET", "DELETE", "GET"]


def test_release_get_404_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    release = cleanup.Resource("github-release", "nightly/v0.8.0-20260825-1#99", 99)
    calls: list[str] = []

    def github_json(method: str, path: str):
        calls.append(method)
        raise cleanup.RemoteError("not found", status=404)

    monkeypatch.setattr(remote, "json_request", github_json)

    assert (
        cleanup.delete_resource_with_retry(remote, release, sleeper=lambda _: None)
        is None
    )
    assert calls == ["GET"]


def test_release_phase_attempts_every_exact_release_independently() -> None:
    manifest = _manifest(chart=None, ghcr_members=[], ghcr_indexes=[])
    run, tag = _control_references(manifest)
    first = cleanup.Resource("github-release", f"{tag}#1", 1)
    second = cleanup.Resource("github-release", f"{tag}#2", 2)
    remote = FakeRemote(
        present={run, tag, first.reference, second.reference},
        releases=[first, second],
    )
    remote.delete_errors[first.reference] = [
        cleanup.RemoteError("forbidden", status=403)
    ]

    report = cleanup.execute_cleanup(
        _target(manifest, remote.releases), remote, sleeper=lambda _: None
    )

    assert report.stopped_phase == 4
    assert report.failures[0].resource == first
    assert first.reference in remote.present
    assert second.reference not in remote.present
    assert second.reference in remote.delete_calls


def test_unbacked_release_failure_preserves_manifest_holder_for_retry() -> None:
    manifest = _manifest(chart=None, ghcr_members=[], ghcr_indexes=[])
    run, tag = _control_references(manifest)
    unbacked = cleanup.Resource("github-release", f"{tag}#1", 1)
    holder = cleanup.Resource("github-release", f"{tag}#2", 2, holds_recovery_data=True)
    remote = FakeRemote(
        present={run, tag, unbacked.reference, holder.reference},
        releases=[unbacked, holder],
    )
    remote.delete_errors[unbacked.reference] = [
        cleanup.RemoteError("forbidden", status=403)
    ]

    report = cleanup.execute_cleanup(
        _target(manifest, remote.releases), remote, sleeper=lambda _: None
    )

    assert report.stopped_phase == 4
    assert unbacked.reference in remote.present
    assert holder.reference in remote.present
    assert holder.reference not in remote.delete_calls


def test_ghcr_package_version_with_another_tag_is_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    monkeypatch.setattr(remote, "_owner_package_prefix", lambda: "/users/release-org")
    monkeypatch.setattr(
        remote,
        "list_pages",
        lambda path: [
            {
                "id": 77,
                "metadata": {"container": {"tags": ["v0.23.0"]}},
            }
        ],
    )
    monkeypatch.setattr(
        remote,
        "json_request",
        lambda method, path: {
            "id": 77,
            "metadata": {"container": {"tags": ["v0.23.0", "shared-latest"]}},
        },
    )
    resource = cleanup.Resource(
        "ghcr-index",
        "ghcr.io/release-org/vllm-openai:v0.23.0",
        ("v0.23.0",),
    )

    with pytest.raises(remote_ops.UnsafePackageVersion, match="shared-latest"):
        remote.probe(resource)


def test_ghcr_allows_other_target_tags_from_the_same_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(
        chart=None,
        ghcr_indexes=["ghcr.io/release-org/vllm-openai:v0.23.0"],
        ghcr_members=[
            "ghcr.io/release-org/vllm-openai:v0.23.0-amd64",
            "ghcr.io/release-org/vllm-openai:v0.23.0-arm64",
        ],
    )
    resources = records_ops.registry_resources(manifest)
    assert resources[0].identifier == ("v0.23.0", "v0.23.0-amd64", "v0.23.0-arm64")
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    monkeypatch.setattr(remote, "_owner_package_prefix", lambda: "/users/release-org")
    monkeypatch.setattr(
        remote,
        "list_pages",
        lambda path: [
            {
                "id": 77,
                "metadata": {"container": {"tags": ["v0.23.0", "v0.23.0-amd64"]}},
            }
        ],
    )

    monkeypatch.setattr(
        remote,
        "json_request",
        lambda method, path: {
            "id": 77,
            "metadata": {"container": {"tags": ["v0.23.0", "v0.23.0-amd64"]}},
        },
    )
    assert remote.probe(resources[0]).endswith("/77")


@pytest.mark.parametrize(
    ("detail", "status", "retryable"),
    [
        ("MANIFEST_UNKNOWN", 404, False),
        ("context deadline exceeded", None, True),
        ("HTTP 429 too many requests", 429, True),
        ("unauthorized", 401, False),
    ],
)
def test_crane_errors_are_structurally_classified(
    detail: str, status: int | None, retryable: bool
) -> None:
    error = cleanup.ProductionRemote._crane_error(detail)

    assert error.status == status
    assert error.is_retryable is retryable


def test_dockerhub_delete_uses_the_probed_manifest_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        remote,
        "_run_crane",
        lambda operation, reference: calls.append((operation, reference)) or "",
    )
    resource = cleanup.Resource(
        "dockerhub-member", "docker.io/release-org/vllm-openai:v0.23.0-amd64"
    )
    digest = "sha256:" + "a" * 64

    remote.delete(resource, digest)

    assert calls == [("delete", f"docker.io/release-org/vllm-openai@{digest}")]


def test_rerunning_an_old_tag_does_not_replace_the_latest_deletion_boundary() -> None:
    records = [_record(_manifest(f"draft/v0.8.0-{i}"), i) for i in range(1, 5)]
    selection = records_ops.select_retention(
        records,
        current_tag="draft/v0.8.0-1",
        release_type="draft",
        max_count=3,
        pypi_enabled=False,
    )
    assert selection.candidates == ()


@pytest.mark.parametrize("contents", ["{", "[]"])
def test_record_cli_rejects_invalid_plan_before_remote_writes(
    tmp_path, monkeypatch, capsys, contents
):
    path = tmp_path / "invalid-plan.json"
    path.write_text(contents)
    remote = SimpleNamespace(
        save_record=lambda *args: pytest.fail(
            "invalid input must not reach publication"
        )
    )
    monkeypatch.setattr(cleanup, "_production_remote", lambda args: remote)
    status = cleanup.main(
        [
            "record-targets",
            "--tag",
            "draft/v0.8.0-1",
            "--release-type",
            "draft",
            "--run-id",
            "100",
            "--source-sha",
            "a" * 40,
            "--plan",
            str(path),
        ]
    )
    assert status == 2
    assert "Traceback" not in capsys.readouterr().err


def test_retention_resolves_and_preserves_known_release_before_execution(
    monkeypatch,
) -> None:
    manifest = _manifest("draft/v0.8.0-1", chart=None, ghcr_members=[], ghcr_indexes=[])
    run, tag = _control_references(manifest)
    release = cleanup.Resource("github-release", f"{tag}#99", 99)
    record = _record(manifest, 99)
    saved = []

    class SelectedInventory:
        def __init__(self, remote):
            self.remote = remote

        def resolve_record(self, selected):
            assert selected == record
            target = _target(manifest, [release])
            return inventory_ops.CleanupResolution(target)

        def persist_recovery(self, resolution):
            assert self.remote.delete_calls == []
            saved.append(resolution)

    monkeypatch.setattr(cleanup, "ReleaseInventory", SelectedInventory)
    monkeypatch.setattr(
        cleanup,
        "collect_catalog",
        lambda *args, **kwargs: SimpleNamespace(
            records=[record], orphans=[], active_tags=set(), blocked={}
        ),
    )
    remote = FakeRemote(present={run, tag, release.reference})
    arguments = SimpleNamespace(
        current_tag="draft/v0.8.0-2",
        release_type="draft",
        max_count=1,
        pypi_enabled=False,
        fail_resource=None,
    )

    assert cleanup._run_retention(arguments, remote) == []
    assert len(saved) == 1
    assert release.reference in remote.delete_calls
    assert release.reference not in remote.present


@pytest.mark.parametrize("transient_failure", [None, "delete", "readback"])
def test_dockerhub_retry_and_readback_keep_the_original_digest(
    monkeypatch, transient_failure
):
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    repository = "docker.io/release-org/vllm-openai"
    resource = cleanup.Resource("dockerhub-member", f"{repository}:v0.23.0-amd64")
    original = "sha256:" + "a" * 64
    replacement = "sha256:" + "b" * 64
    present = {original, replacement}
    tag_digest = original
    calls = []
    failed_once = False

    def crane(operation, reference):
        nonlocal tag_digest, failed_once
        calls.append((operation, reference))
        if reference == resource.reference:
            return tag_digest
        assert reference == f"{repository}@{original}"
        if operation == "delete":
            tag_digest = replacement
            if transient_failure == "delete" and not failed_once:
                failed_once = True
                raise cleanup.RemoteError("HTTP 503", status=503)
            present.discard(original)
            return ""
        if transient_failure == "readback" and not failed_once:
            failed_once = True
            raise cleanup.RemoteError("readback timed out")
        if original not in present:
            raise cleanup.RemoteError("manifest unknown", status=404)
        return original

    monkeypatch.setattr(remote, "_run_crane", crane)
    sleeps = []
    assert (
        cleanup.delete_resource_with_retry(remote, resource, sleeper=sleeps.append)
        is None
    )
    assert calls.count(("digest", resource.reference)) == 1
    assert ("digest", f"{repository}@{original}") in calls
    assert all(reference != f"{repository}@{replacement}" for _, reference in calls)
    assert present == {replacement}
    assert sleeps == ([] if transient_failure is None else [5.0])


def test_dockerhub_probe_can_retry_until_a_digest_is_resolved(monkeypatch):
    remote = cleanup.ProductionRemote("release-org/unified-cache-management", "token")
    repository = "docker.io/release-org/vllm-openai"
    resource = cleanup.Resource("dockerhub-member", f"{repository}:v0.23.0-amd64")
    digest = "sha256:" + "a" * 64
    calls = []

    def crane(operation, reference):
        calls.append((operation, reference))
        if len(calls) == 1:
            raise cleanup.RemoteError("probe timed out")
        if reference == resource.reference:
            return digest
        assert reference == f"{repository}@{digest}"
        if operation == "delete":
            return ""
        raise cleanup.RemoteError("manifest unknown", status=404)

    monkeypatch.setattr(remote, "_run_crane", crane)
    sleeps = []
    assert (
        cleanup.delete_resource_with_retry(remote, resource, sleeper=sleeps.append)
        is None
    )
    assert calls == [
        ("digest", resource.reference),
        ("digest", resource.reference),
        ("delete", f"{repository}@{digest}"),
        ("digest", f"{repository}@{digest}"),
    ]
    assert sleeps == [5.0]


def test_local_record_and_event_validation_do_not_construct_a_remote(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        cleanup,
        "_production_remote",
        lambda _: pytest.fail("local commands need no token"),
    )
    identity = [
        "--repository",
        "release-org/unified-cache-management",
        "--tag",
        "draft/v0.8.0-1",
        "--source-sha",
        "a" * 40,
        "--run-id",
        "100",
        "--run-attempt",
        "2",
    ]
    assert cleanup.main(["record", *identity, "--output-dir", str(tmp_path)]) == 0
    path = tmp_path / "release-cleanup-100-2-opened.json"
    assert path.is_file()
    assert cleanup.main(["validate-record", *identity, "--input", str(path)]) == 0


@pytest.mark.parametrize(
    ("option", "mismatched_value"),
    [("--run-id", "101"), ("--run-attempt", "3"), ("--source-sha", "b" * 40)],
)
def test_event_validation_rejects_a_different_publication_binding(
    tmp_path, monkeypatch, option, mismatched_value
):
    snapshot = records_ops.new_record(
        "release-org/unified-cache-management", "draft/v0.8.0-1", "a" * 40, 100, 2
    )
    path = tmp_path / records_ops.record_basename(snapshot, 2)
    path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(
        cleanup, "_production_remote", lambda _: pytest.fail("validation is read-only")
    )
    identity = [
        "--repository",
        "release-org/unified-cache-management",
        "--tag",
        "draft/v0.8.0-1",
        "--source-sha",
        "a" * 40,
        "--run-id",
        "100",
        "--run-attempt",
        "2",
    ]
    identity[identity.index(option) + 1] = mismatched_value
    assert cleanup.main(["validate-record", *identity, "--input", str(path)]) == 2
    assert json.loads(path.read_text()) == snapshot
