from __future__ import annotations

import copy
import importlib
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
core = importlib.import_module("ucm_release.core")
registry = importlib.import_module("ucm_release.registry")

WHEELS = [
    "uc_manager_cuda-0.7.59rc1-cp312-cp312-manylinux_2_28_x86_64.whl",
    "uc_manager_cuda-0.7.59rc1-cp312-cp312-manylinux_2_28_aarch64.whl",
    "uc_manager_cann_a2-0.7.59rc1-cp312-cp312-linux_x86_64.whl",
    "uc_manager_cann_a2-0.7.59rc1-cp312-cp312-linux_aarch64.whl",
    "uc_manager_cann_a3-0.7.59rc1-cp312-cp312-linux_x86_64.whl",
    "uc_manager_cann_a3-0.7.59rc1-cp312-cp312-linux_aarch64.whl",
]
CHART = "unified-cache-pd-0.7.59-rc.1.tgz"
INDEX_DIGEST = "sha256:" + "1" * 64
AMD64_DIGEST = "sha256:" + "2" * 64
ARM64_DIGEST = "sha256:" + "3" * 64
AMD64_CONFIG_DIGEST = "sha256:" + "4" * 64
ARM64_CONFIG_DIGEST = "sha256:" + "5" * 64


def _registry_fixture() -> dict[str, object]:
    return json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )


def _builder_catalog() -> dict[str, object]:
    return builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=RELEASE_ROOT / "tests" / "fixtures" / "builders",
        owner="release-org",
    )


def _builder_root(*_args, **_kwargs) -> dict[str, object]:
    digest = "sha256:" + "d" * 64
    return {
        "index_digest": digest,
        "manifest_digest": digest,
        "config_digest": digest,
        "operations": [],
    }


def _resolved_plan(*, lane: str, fixture_only: bool | None = None) -> dict[str, object]:
    catalog = core.load_catalog(version_override="0.7.59rc1")
    fixture = _registry_fixture()
    if lane == "feature-candidate" and fixture_only is not False:
        with mock.patch.object(
            registry, "resolve_builder_root", side_effect=_builder_root
        ):
            return registry.resolve_catalog(
                catalog,
                builder_catalog=_builder_catalog(),
                source_sha="a" * 40,
                lane=lane,
                fixture=fixture,
            )

    repositories = fixture["repositories"]
    fixture_enumerate = registry.enumerate_repository_tags
    fixture_resolve = registry.resolve_repository_tag

    def enumerate_tags(repository: str, *, fixture=None, max_tags: int):
        result = fixture_enumerate(
            repository,
            fixture=repositories[repository],
            max_tags=max_tags,
        )
        result["operations"] = [
            {
                "type": "crane-tag-list",
                "capability": "read",
                "reference": repository,
            }
        ]
        return result

    def resolve_tag(
        repository: str,
        upstream_tag: str,
        *,
        required_architectures: list[str],
        fixture=None,
    ):
        result = fixture_resolve(
            repository,
            upstream_tag,
            required_architectures=required_architectures,
            fixture=repositories[repository]["snapshots"][upstream_tag],
        )
        result["operations"] = [
            {
                "type": "crane-digest",
                "capability": "read",
                "reference": f"{repository}:{upstream_tag}",
            }
        ]
        return result

    def inspect_variant(_crane, _repository, digest, product):
        if product["id"] == "vllm":
            return "default", None
        tag = next(
            tag
            for tag, snapshot in repositories[product["repository"]][
                "snapshots"
            ].items()
            if snapshot["index_digest"] == digest
        )
        return ("a3" if tag.endswith("-a3") else "a2"), None

    with (
        mock.patch.object(registry, "resolve_builder_root", side_effect=_builder_root),
        mock.patch.object(registry, "resolve_pinned_crane", return_value="crane"),
        mock.patch.object(
            registry, "enumerate_repository_tags", side_effect=enumerate_tags
        ),
        mock.patch.object(registry, "resolve_repository_tag", side_effect=resolve_tag),
        mock.patch.object(
            registry, "_inspect_upstream_variant", side_effect=inspect_variant
        ),
    ):
        return registry.resolve_catalog(
            catalog,
            builder_catalog=_builder_catalog(),
            source_sha="a" * 40,
            lane=lane,
        )


def _write_plan(path: Path, plan: dict[str, object]) -> Path:
    path.write_bytes(core.canonical_bytes(plan) + b"\n")
    return path


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    plan = _resolved_plan(lane="protected-tag")
    path = tmp_path / "resolved-plan.json"
    return _write_plan(path, plan)


def _rewrite_channel(plan_path: Path, channel: str, enabled: bool) -> None:
    plan = core.load_json(plan_path)
    plan["publish"][channel]["enabled"] = enabled
    plan["resolved_plan_sha256"] = core.sha256_value(
        {key: value for key, value in plan.items() if key != "resolved_plan_sha256"}
    )
    plan_path.write_bytes(core.canonical_bytes(plan) + b"\n")


def _artifacts(tmp_path: Path) -> list[Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name in [*WHEELS, CHART]:
        path = tmp_path / name
        path.write_bytes((name + "-bytes").encode())
        paths.append(path)
    return paths


def _draft_state(
    plan_path: Path,
    tmp_path: Path,
    *,
    mutation: dict[str, object] | None = None,
) -> Path:
    plan = core.load_json(plan_path)
    state: dict[str, object] = {
        "kind": "ucm-publication-result",
        "schema_version": 1,
        "channel": "github_release",
        "stage": "draft",
        "status": "created",
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "release_id": 41,
        "tag": "v0.7.59rc1",
        "draft": True,
    }
    state.update(mutation or {})
    path = tmp_path / "draft-state.json"
    path.write_bytes(core.canonical_bytes(state) + b"\n")
    return path


def _public_binding(plan_path: Path, tmp_path: Path) -> Path:
    return _draft_state(
        plan_path,
        tmp_path,
        mutation={
            "status": "reused",
            "draft": False,
            "asset_names": sorted([*WHEELS, CHART]),
        },
    )


def _member_records(plan_path: Path, directory: Path) -> Path:
    plan = core.load_json(plan_path)
    directory.mkdir(parents=True, exist_ok=True)
    for task in plan["image_tasks"]:
        architecture = task["platform"].split("/", 1)[1]
        member_digest = AMD64_DIGEST if architecture == "amd64" else ARM64_DIGEST
        config_digest = (
            AMD64_CONFIG_DIGEST if architecture == "amd64" else ARM64_CONFIG_DIGEST
        )
        build_key = "sha256:" + "6" * 64
        wheel_sha = "sha256:" + "7" * 64
        recipe_sha = "sha256:" + "8" * 64
        layer_digest = "sha256:" + "9" * 64
        manifest_annotations = {
            "io.ucm.release.recipe-sha256": recipe_sha,
            "io.ucm.release.task-sha256": task["task_sha256"],
        }
        labels = {
            "org.opencontainers.image.source": (
                "https://github.com/" + plan["source"]["repository"]
            ),
            "org.opencontainers.image.revision": plan["source"]["commit"],
            "io.ucm.release.source-tree": "b" * 40,
            "io.ucm.release.source-context-sha256": "sha256:" + "a" * 64,
            "io.ucm.release.build-key-sha256": build_key,
            "io.ucm.release.task-sha256": task["task_sha256"],
            "io.ucm.release.wheel-sha256": wheel_sha,
            "io.ucm.release.recipe-sha256": recipe_sha,
        }
        content_identity = {
            "manifest_digest": member_digest,
            "config_digest": config_digest,
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": layer_digest,
                    "size": 19,
                }
            ],
            "diff_ids": ["sha256:" + "c" * 64],
            "annotations": manifest_annotations,
            "labels": labels,
            "created": "2026-08-20T00:00:00Z",
            "history": [{"created_by": "fixture"}],
            "source": {
                "repository": plan["source"]["repository"],
                "repository_url": (
                    "https://github.com/" + plan["source"]["repository"]
                ),
                "commit": plan["source"]["commit"],
                "tree": "b" * 40,
                "archive_sha256": "sha256:" + "d" * 64,
                "context_sha256": "sha256:" + "a" * 64,
            },
            "task_sha256": task["task_sha256"],
            "build_key_sha256": build_key,
            "wheel_sha256": wheel_sha,
            "recipe_sha256": recipe_sha,
        }
        content_identity["content_identity_sha256"] = core.sha256_value(
            content_identity
        )
        manifest = {
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": member_digest,
            "size": 101,
            "annotations": manifest_annotations,
        }
        config = {
            "media_type": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": 79,
            "blob_sha256": config_digest,
            "labels": labels,
        }
        staging_tag = "staging-" + build_key.removeprefix("sha256:")
        staging_repository = plan["source"]["staging_repository"]
        record: dict[str, object] = {
            "schema_version": 1,
            "kind": "ucm-registry-member-publication",
            "status": "passed",
            "resolved_plan_sha256": plan["resolved_plan_sha256"],
            "spec_id": task["spec_id"],
            "profile_id": task["profile_id"],
            "family_id": task["family_task_id"],
            "platform": task["platform"],
            "target_repository": task["target_repository"],
            "target_tag": task["target_tag"],
            "staging_repository": staging_repository,
            "staging_visibility": "private",
            "staging_tag": staging_tag,
            "candidate_task_sha256": task["task_sha256"],
            "publication_task_sha256": task["task_sha256"],
            "build_key_sha256": build_key,
            "wheel_sha256": wheel_sha,
            "member_digest": member_digest,
            "member_size": 101,
            "config_digest": config_digest,
            "annotations": {
                "io.ucm.release.build-key-sha256": build_key,
                "io.ucm.release.candidate-task-sha256": task["task_sha256"],
                "io.ucm.release.family-id": task["family_task_id"],
                "io.ucm.release.platform": task["platform"],
                "io.ucm.release.spec-id": task["spec_id"],
                "io.ucm.release.wheel-sha256": wheel_sha,
            },
            "source_sha": plan["source"]["commit"],
            "image_result_sha256": "sha256:" + "e" * 64,
            "recipe_sha256": recipe_sha,
            "content_identity_sha256": content_identity["content_identity_sha256"],
            "content_identity": content_identity,
            "manifest": manifest,
            "config": config,
            "layers": [
                {
                    "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": layer_digest,
                    "size": 19,
                    "blob_sha256": layer_digest,
                }
            ],
            "readback_sha256": core.sha256_value(
                {"manifest": manifest, "config": config}
            ),
            "operations": [
                {
                    "type": "registry-member-push-by-digest",
                    "capability": "write",
                    "reference": f"{staging_repository}@{member_digest}",
                },
                {
                    "type": "registry-staging-tag-create",
                    "capability": "write",
                    "reference": f"{staging_repository}:{staging_tag}",
                },
                {
                    "type": "registry-authenticated-digest-read",
                    "capability": "read",
                    "reference": f"{staging_repository}@{member_digest}",
                },
                {
                    "type": "registry-authenticated-manifest-read",
                    "capability": "read",
                    "reference": f"{staging_repository}@{member_digest}",
                },
                {
                    "type": "registry-authenticated-config-blob-read",
                    "capability": "read",
                    "reference": f"{staging_repository}@{config_digest}",
                },
            ],
        }
        record["record_sha256"] = core.sha256_value(record)
        (directory / f"{task['task_id']}.json").write_bytes(
            core.canonical_bytes(record) + b"\n"
        )
    return directory


def _completed(
    command: list[str], stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class OciRunner:
    def __init__(
        self, *, extra_platform: bool = False, dockerhub_digest_conflict: bool = False
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None, bytes | None]] = []
        self.extra_platform = extra_platform
        self.dockerhub_digest_conflict = dockerhub_digest_conflict

    def __call__(self, command, *, env=None, input_bytes=None):
        command = list(command)
        copied_env = dict(env) if env is not None else None
        self.calls.append((command, copied_env, input_bytes))
        if command[:4] == ["docker", "buildx", "imagetools", "create"]:
            return _completed(command)
        operation = command[1]
        if operation == "copy":
            return _completed(command)
        assert copied_env is not None
        docker_config = Path(copied_env["DOCKER_CONFIG"])
        assert docker_config.is_dir()
        assert list(docker_config.iterdir()) == []
        if operation == "digest":
            digest = (
                "sha256:" + "6" * 64
                if self.dockerhub_digest_conflict
                and command[-1].startswith("docker.io/")
                else INDEX_DIGEST
            )
            return _completed(command, digest + "\n")
        if operation == "manifest" and command[-1].rsplit("@", 1)[-1] in {
            INDEX_DIGEST,
            "sha256:" + "6" * 64,
        }:
            manifests = [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": AMD64_DIGEST,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": ARM64_DIGEST,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ]
            if self.extra_platform:
                manifests.append(
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "9" * 64,
                        "platform": {"os": "linux", "architecture": "s390x"},
                    }
                )
            return _completed(
                command,
                json.dumps(
                    {
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": manifests,
                    }
                ),
            )
        if operation == "manifest":
            config_digest = (
                AMD64_CONFIG_DIGEST
                if command[-1].endswith(AMD64_DIGEST)
                else ARM64_CONFIG_DIGEST
            )
            return _completed(
                command,
                json.dumps(
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": config_digest,
                        },
                        "layers": [],
                    }
                ),
            )
        if operation == "config":
            architecture = command[3].split("/", 1)[1]
            return _completed(
                command, json.dumps({"os": "linux", "architecture": architecture})
            )
        raise AssertionError(command)


class GithubApi:
    def __init__(
        self,
        release: dict[str, object] | None = None,
        contents: dict[str, bytes] | None = None,
    ) -> None:
        self.release = copy.deepcopy(release)
        self.contents = dict(contents or {})
        if self.release is not None:
            for asset in self.release.get("assets", []):
                name = asset["name"]
                self.contents.setdefault(name, (name + "-bytes").encode())
        self.calls: list[dict[str, object]] = []

    @staticmethod
    def _asset(asset_id: int, name: str, content: bytes) -> dict[str, object]:
        return {
            "id": asset_id,
            "name": name,
            "size": len(content),
            "url": f"https://api.github.test/assets/{asset_id}",
            "browser_download_url": f"https://downloads.github.test/{name}",
        }

    def download(self, url: str) -> bytes:
        if "/assets/" in url:
            asset_id = int(url.rsplit("/", 1)[1])
            assert self.release is not None
            name = next(
                asset["name"]
                for asset in self.release["assets"]
                if asset["id"] == asset_id
            )
        else:
            name = urllib.parse.unquote(url.rsplit("/", 1)[1])
        return self.contents[name]

    def public_json(self, _url: str) -> dict[str, object]:
        assert self.release is not None
        return copy.deepcopy(self.release)

    def __call__(
        self,
        path: str,
        *,
        method: str = "GET",
        body: object | None = None,
        content_type: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, object] | None:
        self.calls.append(
            {
                "path": path,
                "method": method,
                "body": body,
                "content_type": content_type,
                "allow_missing": allow_missing,
            }
        )
        if method == "GET":
            if self.release is None and allow_missing:
                return None
            if self.release is None:
                raise ValueError("release missing")
            return copy.deepcopy(self.release)
        if method == "POST" and path.endswith("/releases"):
            assert isinstance(body, dict)
            self.release = {
                "id": 41,
                "tag_name": body["tag_name"],
                "target_commitish": body["target_commitish"],
                "draft": True,
                "prerelease": True,
                "assets": [],
            }
            return copy.deepcopy(self.release)
        if method == "POST" and "uploads.github.com" in path:
            assert self.release is not None
            assert isinstance(body, bytes)
            name = urllib.parse.unquote(path.rsplit("name=", 1)[1])
            asset_id = len(self.release["assets"]) + 1
            self.contents[name] = body
            asset = self._asset(asset_id, name, body)
            self.release["assets"].append(asset)
            return copy.deepcopy(asset)
        if method == "PATCH":
            assert self.release is not None and isinstance(body, dict)
            self.release.update(body)
            return copy.deepcopy(self.release)
        raise AssertionError((path, method))


class DraftByIdGithubApi(GithubApi):
    def __call__(
        self,
        path: str,
        *,
        method: str = "GET",
        body: object | None = None,
        content_type: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, object] | None:
        if method == "GET" and "/releases/tags/" in path:
            raise ValueError("GitHub Draft tag lookup failed: HTTP 404")
        if (
            method == "GET"
            and self.release is not None
            and path.endswith(f"/releases/{self.release['id']}")
        ):
            self.calls.append(
                {
                    "path": path,
                    "method": method,
                    "body": body,
                    "content_type": content_type,
                    "allow_missing": allow_missing,
                }
            )
            return copy.deepcopy(self.release)
        return super().__call__(
            path,
            method=method,
            body=body,
            content_type=content_type,
            allow_missing=allow_missing,
        )


def _public_release(asset_names: list[str] | None = None) -> dict[str, object]:
    names = asset_names if asset_names is not None else [*WHEELS, CHART]
    return {
        "id": 41,
        "tag_name": "v0.7.59rc1",
        "target_commitish": "a" * 40,
        "draft": False,
        "prerelease": True,
        "assets": [
            GithubApi._asset(index, name, (name + "-bytes").encode())
            for index, name in enumerate(names, start=1)
        ],
    }


@pytest.mark.parametrize(
    ("channel", "operation"),
    [
        ("pypi", "publish_pypi"),
        ("dockerhub", "publish_dockerhub"),
        ("ghcr", "publish_ghcr"),
        ("chart_oci", "publish_chart_oci"),
        ("github_release", "publish_github_release"),
    ],
)
def test_disabled_channel_returns_canonical_skip_without_calls(
    plan_path: Path, tmp_path: Path, channel: str, operation: str
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    _rewrite_channel(plan_path, channel, False)
    calls: list[object] = []

    if operation == "publish_pypi":
        result = publication.publish_pypi(
            plan_path, [], stage="publish", run=lambda *a, **k: calls.append((a, k))
        )
        stage = "publish"
    elif operation == "publish_dockerhub":
        result = publication.publish_dockerhub(
            plan_path, stage="publish", run=lambda *a, **k: calls.append((a, k))
        )
        stage = "publish"
    elif operation == "publish_ghcr":
        result = publication.publish_ghcr(
            plan_path,
            stage="readback",
            run=lambda *a, **k: calls.append((a, k)),
        )
        stage = "readback"
    elif operation == "publish_chart_oci":
        result = publication.publish_chart_oci(
            plan_path,
            tmp_path / "missing.tgz",
            stage="publish",
            run=lambda *a, **k: calls.append((a, k)),
        )
        stage = "publish"
    else:
        result = publication.publish_github_release(
            plan_path,
            stage="draft",
            api=lambda *a, **k: calls.append((a, k)),
        )
        stage = "draft"

    assert result == {
        "kind": "ucm-publication-result",
        "schema_version": 1,
        "channel": channel,
        "stage": stage,
        "status": "skipped",
        "resolved_plan_sha256": core.load_json(plan_path)["resolved_plan_sha256"],
        "reason": "disabled",
    }
    assert calls == []


def test_disabled_channel_short_circuits_before_feature_authority_and_inputs(
    tmp_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    path = _write_plan(
        tmp_path / "feature-plan.json",
        _resolved_plan(lane="feature-candidate", fixture_only=True),
    )
    calls: list[object] = []

    result = publication.publish_pypi(
        path,
        [],
        stage="publish",
        draft_state=tmp_path / "missing-draft.json",
        run=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert result["status"] == "skipped"
    assert calls == []


@pytest.mark.parametrize(
    ("lane", "fixture_only"),
    [
        ("feature-candidate", False),
        ("feature-candidate", True),
    ],
)
def test_enabled_publish_rejects_feature_or_fixture_plan_before_transport(
    tmp_path: Path, lane: str, fixture_only: bool
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    path = _write_plan(
        tmp_path / "plan.json",
        _resolved_plan(lane=lane, fixture_only=fixture_only),
    )
    _rewrite_channel(path, "pypi", True)
    wheels = _artifacts(tmp_path / "artifacts")[:-1]
    calls: list[object] = []

    with pytest.raises(ValueError, match="non-fixture protected-tag"):
        publication.publish_pypi(
            path,
            wheels,
            stage="publish",
            draft_state=_draft_state(path, tmp_path),
            run=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

    assert calls == []


def test_enabled_readback_rejects_feature_plan_before_transport(tmp_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    path = _write_plan(
        tmp_path / "plan.json",
        _resolved_plan(lane="feature-candidate", fixture_only=False),
    )
    calls: list[object] = []

    with pytest.raises(ValueError, match="non-fixture protected-tag"):
        publication.publish_ghcr(
            path,
            stage="readback",
            run=lambda *args, **kwargs: calls.append((args, kwargs)),
            crane_binary="crane",
        )

    assert calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"resolved_plan_sha256": "sha256:" + "9" * 64},
        {"tag": "v9.9.9"},
        {"release_id": "42"},
        {"draft": False},
        {"status": "verified"},
    ],
)
def test_publish_rejects_draft_state_drift_before_transport(
    plan_path: Path, tmp_path: Path, mutation: dict[str, object]
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    _rewrite_channel(plan_path, "dockerhub", True)
    calls: list[object] = []

    with pytest.raises(ValueError, match="Draft state"):
        publication.publish_dockerhub(
            plan_path,
            stage="publish",
            draft_state=_draft_state(
                plan_path,
                tmp_path,
                mutation=mutation,
            ),
            run=lambda *args, **kwargs: calls.append((args, kwargs)),
            crane_binary="crane",
        )

    assert calls == []


def test_public_release_binding_makes_every_channel_publish_a_zero_write_reuse(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    for channel in ("pypi", "dockerhub"):
        _rewrite_channel(plan_path, channel, True)
    artifacts = _artifacts(tmp_path / "artifacts")
    binding = _public_binding(plan_path, tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))

    results = [
        publication.publish_pypi(
            plan_path,
            artifacts[:-1],
            stage="publish",
            draft_state=binding,
            run=runner,
        ),
        publication.publish_ghcr(
            plan_path,
            stage="publish",
            members_dir=tmp_path / "missing-members",
            draft_state=binding,
            run=runner,
        ),
        publication.publish_dockerhub(
            plan_path,
            stage="publish",
            draft_state=binding,
            run=runner,
        ),
        publication.publish_chart_oci(
            plan_path,
            artifacts[-1],
            stage="publish",
            draft_state=binding,
            run=runner,
        ),
    ]

    assert [result["status"] for result in results] == ["reused"] * 4
    assert calls == []


def test_pypi_publish_and_readback_use_exact_six_wheels_and_three_dists(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    _rewrite_channel(plan_path, "pypi", True)
    artifacts = _artifacts(tmp_path)[:-1]
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        return _completed(list(command))

    published = publication.publish_pypi(
        plan_path,
        artifacts,
        stage="publish",
        draft_state=_draft_state(plan_path, tmp_path),
        run=run,
    )

    def get_json(url: str) -> dict[str, object]:
        dist = url.split("/pypi/", 1)[1].split("/", 1)[0]
        filenames = [name for name in WHEELS if name.startswith(dist.replace("-", "_"))]
        return {
            "info": {"name": dist, "version": "0.7.59rc1"},
            "urls": [{"filename": name} for name in filenames],
        }

    readback = publication.publish_pypi(
        plan_path, artifacts, stage="readback", http_get=get_json
    )

    assert calls == [
        [
            sys.executable,
            "-m",
            "twine",
            "upload",
            "--repository-url",
            "https://upload.pypi.org/legacy/",
            *[str(path) for path in artifacts],
        ]
    ]
    assert published["status"] == "published"
    assert readback["status"] == "verified"
    assert published["resolved_plan_sha256"] == readback["resolved_plan_sha256"]
    assert readback["distributions"] == [
        "uc-manager-cuda==0.7.59rc1",
        "uc-manager-cann-a2==0.7.59rc1",
        "uc-manager-cann-a3==0.7.59rc1",
    ]


def test_ghcr_readback_uses_empty_auth_and_records_index_digest(
    plan_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    runner = OciRunner()

    result = publication.publish_ghcr(
        plan_path,
        stage="readback",
        run=runner,
        crane_binary="/pinned/crane",
    )

    assert result["status"] == "verified"
    assert (
        result["resolved_plan_sha256"]
        == core.load_json(plan_path)["resolved_plan_sha256"]
    )
    assert len(result["images"]) == 3
    assert {item["index_digest"] for item in result["images"]} == {INDEX_DIGEST}
    assert len(runner.calls) == 18
    assert all(call[0][0] == "/pinned/crane" for call in runner.calls)
    assert sum(call[0][1] == "config" for call in runner.calls) == 6
    assert all(
        "--platform" in call[0] for call in runner.calls if call[0][1] == "config"
    )


def test_publication_matrix_has_exact_three_families_and_six_images(
    plan_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    plan = core.load_json(plan_path)

    publication._require_release_image_matrix(plan)
    plan["family_tasks"].pop()

    with pytest.raises(
        ValueError,
        match=(
            "publication matrix requires exactly 3 family tasks and 6 image tasks; "
            "got family_tasks=2, image_tasks=6"
        ),
    ):
        publication._require_release_image_matrix(plan)


def test_ghcr_publish_validates_six_members_and_creates_three_indexes(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    members_dir = _member_records(plan_path, tmp_path / "members")
    runner = OciRunner()

    result = publication.publish_ghcr(
        plan_path,
        stage="publish",
        members_dir=members_dir,
        draft_state=_draft_state(plan_path, tmp_path),
        run=runner,
        crane_binary="crane",
    )

    creates = [
        command
        for command, _environment, _input in runner.calls
        if command[:4] == ["docker", "buildx", "imagetools", "create"]
    ]
    assert len(creates) == 3
    assert all(command[4] == "--tag" for command in creates)
    assert all(
        command[-2:]
        == [
            "ghcr.io/release-org/ucm-release-staging@" + AMD64_DIGEST,
            "ghcr.io/release-org/ucm-release-staging@" + ARM64_DIGEST,
        ]
        for command in creates
    )
    assert result["status"] == "published"
    assert len(result["images"]) == 3
    assert all(image["index_digest"] == INDEX_DIGEST for image in result["images"])
    assert all(
        image["members"]
        == [
            {
                "platform": "linux/amd64",
                "manifest_digest": AMD64_DIGEST,
                "config_digest": AMD64_CONFIG_DIGEST,
            },
            {
                "platform": "linux/arm64",
                "manifest_digest": ARM64_DIGEST,
                "config_digest": ARM64_CONFIG_DIGEST,
            },
        ]
        for image in result["images"]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "exactly six member-result JSON files"),
        ("target", "member target differs from resolved image task"),
        ("plan", "member resolved_plan_sha256 differs from resolved plan"),
        ("record", "record_sha256"),
        ("readback", "readback_sha256"),
        ("content", "content identity"),
        ("operations", "operations"),
        ("layers", "layers"),
    ],
)
def test_ghcr_publish_rejects_missing_or_drifted_member_results_before_transport(
    plan_path: Path, tmp_path: Path, mutation: str, message: str
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    members_dir = _member_records(plan_path, tmp_path / "members")
    first = sorted(members_dir.glob("*.json"))[0]
    if mutation == "missing":
        first.rename(tmp_path / first.name)
    else:
        record = core.load_json(first)
        if mutation == "target":
            record["target_tag"] = "wrong"
        elif mutation == "plan":
            record["resolved_plan_sha256"] = "sha256:" + "9" * 64
        elif mutation == "record":
            record["record_sha256"] = "sha256:" + "0" * 64
        elif mutation == "readback":
            record["readback_sha256"] = "sha256:" + "0" * 64
        elif mutation == "content":
            record["content_identity"]["wheel_sha256"] = "sha256:" + "0" * 64
            record["content_identity"]["content_identity_sha256"] = core.sha256_value(
                {
                    key: value
                    for key, value in record["content_identity"].items()
                    if key != "content_identity_sha256"
                }
            )
            record["content_identity_sha256"] = record["content_identity"][
                "content_identity_sha256"
            ]
        elif mutation == "operations":
            record["operations"].pop()
        else:
            record["layers"][0]["digest"] = "sha256:" + "0" * 64
            record["layers"][0]["blob_sha256"] = "sha256:" + "0" * 64
        if mutation != "record":
            record["record_sha256"] = core.sha256_value(
                {key: value for key, value in record.items() if key != "record_sha256"}
            )
        first.write_bytes(core.canonical_bytes(record) + b"\n")
    calls: list[object] = []

    with pytest.raises(ValueError, match=message):
        publication.publish_ghcr(
            plan_path,
            stage="publish",
            members_dir=members_dir,
            draft_state=_draft_state(plan_path, tmp_path),
            run=lambda *args, **kwargs: calls.append((args, kwargs)),
            crane_binary="crane",
        )

    assert calls == []


def test_shared_oci_reader_returns_exact_index_and_platforms() -> None:
    publication = importlib.import_module("ucm_release.publish")

    result = publication.inspect_oci_reference(
        "ghcr.io/release-org/example:v1",
        run=OciRunner(),
        crane_binary="/pinned/crane",
    )

    assert result == {
        "reference": "ghcr.io/release-org/example:v1",
        "index_digest": INDEX_DIGEST,
        "platforms": ["linux/amd64", "linux/arm64"],
        "members": [
            {
                "platform": "linux/amd64",
                "manifest_digest": AMD64_DIGEST,
                "config_digest": AMD64_CONFIG_DIGEST,
            },
            {
                "platform": "linux/arm64",
                "manifest_digest": ARM64_DIGEST,
                "config_digest": ARM64_CONFIG_DIGEST,
            },
        ],
    }


def test_ghcr_readback_rejects_extra_platform(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")

    with pytest.raises(ValueError, match="exact linux/amd64 and linux/arm64"):
        publication.publish_ghcr(
            plan_path,
            stage="readback",
            run=OciRunner(extra_platform=True),
            crane_binary="crane",
        )


def test_ghcr_readback_requires_exact_three_families_and_six_images(
    plan_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    plan = core.load_json(plan_path)
    plan["family_tasks"].pop()
    plan["resolved_plan_sha256"] = core.sha256_value(
        {key: value for key, value in plan.items() if key != "resolved_plan_sha256"}
    )
    plan_path.write_bytes(core.canonical_bytes(plan) + b"\n")

    with pytest.raises(ValueError, match="resolved plan|three family|3 family"):
        publication.publish_ghcr(
            plan_path,
            stage="readback",
            run=OciRunner(),
            crane_binary="crane",
        )


def test_dockerhub_copies_and_reads_exact_three_families(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    _rewrite_channel(plan_path, "dockerhub", True)
    runner = OciRunner()

    published = publication.publish_dockerhub(
        plan_path,
        stage="publish",
        draft_state=_draft_state(plan_path, plan_path.parent),
        run=runner,
        crane_binary="crane",
    )
    copied = [call[0] for call in runner.calls]
    assert len(copied) == 3
    assert all(command[1] == "copy" for command in copied)
    assert all(command[-1].startswith("docker.io/release-org/") for command in copied)
    runner.calls.clear()

    readback = publication.publish_dockerhub(
        plan_path, stage="readback", run=runner, crane_binary="crane"
    )
    assert published["status"] == "published"
    assert readback["status"] == "verified"
    assert published["resolved_plan_sha256"] == readback["resolved_plan_sha256"]
    assert len(readback["images"]) == 3
    assert all(
        image["source"]["index_digest"] == image["target"]["index_digest"]
        and image["source"]["members"] == image["target"]["members"]
        for image in readback["images"]
    )


def test_dockerhub_readback_rejects_source_target_digest_conflict(
    plan_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    _rewrite_channel(plan_path, "dockerhub", True)

    with pytest.raises(
        ValueError, match="Docker Hub target index differs from GHCR source"
    ):
        publication.publish_dockerhub(
            plan_path,
            stage="readback",
            run=OciRunner(dockerhub_digest_conflict=True),
            crane_binary="crane",
        )


def test_chart_oci_publish_and_pull_roundtrip_bytes(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    package = tmp_path / CHART
    package.write_bytes(b"exact-chart-bytes")
    pull_dir = tmp_path / "pull"
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        command = list(command)
        calls.append(command)
        if command[1] == "pull":
            destination = Path(command[command.index("--destination") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / CHART).write_bytes(b"exact-chart-bytes")
        return _completed(command)

    published = publication.publish_chart_oci(
        plan_path,
        package,
        stage="publish",
        draft_state=_draft_state(plan_path, tmp_path),
        run=run,
    )
    readback = publication.publish_chart_oci(
        plan_path, package, stage="readback", readback_dir=pull_dir, run=run
    )

    assert calls == [
        ["helm", "push", str(package), "oci://ghcr.io/release-org/charts"],
        [
            "helm",
            "pull",
            "oci://ghcr.io/release-org/charts/unified-cache-pd",
            "--version",
            "0.7.59-rc.1",
            "--destination",
            str(pull_dir),
        ],
    ]
    assert published["status"] == "published"
    assert readback["status"] == "verified"
    assert published["resolved_plan_sha256"] == readback["resolved_plan_sha256"]
    assert readback["filename"] == CHART


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_github_assets_validate_complete_local_set_before_api_write(
    plan_path: Path, tmp_path: Path, mutation: str
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    artifacts = _artifacts(tmp_path / "a")
    if mutation == "missing":
        artifacts.pop()
    elif mutation == "extra":
        extra = tmp_path / "extra.txt"
        extra.write_bytes(b"extra")
        artifacts.append(extra)
    else:
        duplicate = tmp_path / "duplicate" / WHEELS[0]
        duplicate.parent.mkdir()
        duplicate.write_bytes(b"duplicate")
        artifacts.append(duplicate)
    api = GithubApi(
        {
            "id": 41,
            "tag_name": "v0.7.59rc1",
            "target_commitish": "a" * 40,
            "draft": True,
            "prerelease": True,
            "assets": [],
        }
    )

    with pytest.raises(ValueError, match="exact seven release assets"):
        publication.publish_github_release(
            plan_path,
            stage="assets",
            artifacts=artifacts,
            draft_state=_draft_state(plan_path, tmp_path),
            api=api,
        )
    assert all(call["method"] == "GET" for call in api.calls)


def test_github_assets_reject_same_names_with_wrong_existing_bytes_before_write(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    artifacts = _artifacts(tmp_path / "artifacts")
    release = _public_release()
    release["draft"] = True
    contents = {path.name: path.read_bytes() for path in artifacts}
    contents[WHEELS[0]] = b"wrong-existing-bytes"
    api = GithubApi(release, contents)

    with pytest.raises(ValueError, match="asset bytes differ"):
        publication.publish_github_release(
            plan_path,
            stage="assets",
            artifacts=artifacts,
            draft_state=_draft_state(plan_path, tmp_path),
            api=api,
            download_bytes=api.download,
        )

    assert all(call["method"] == "GET" for call in api.calls)


def test_github_assets_resolve_same_run_draft_by_release_id_when_tag_lookup_is_missing(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    artifacts = _artifacts(tmp_path / "artifacts")
    api = DraftByIdGithubApi(
        {
            "id": 41,
            "tag_name": "v0.7.59rc1",
            "target_commitish": "a" * 40,
            "draft": True,
            "prerelease": True,
            "assets": [],
        }
    )

    result = publication.publish_github_release(
        plan_path,
        stage="assets",
        artifacts=artifacts,
        draft_state=_draft_state(plan_path, tmp_path),
        api=api,
        download_bytes=api.download,
    )

    assert result["status"] == "uploaded"
    assert result["release_id"] == 41
    get_paths = [call["path"] for call in api.calls if call["method"] == "GET"]
    assert get_paths == [
        "repos/release-org/unified-cache-management/releases/41",
        "repos/release-org/unified-cache-management/releases/41",
    ]


def test_github_release_draft_assets_finalize_and_readback(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    artifacts = _artifacts(tmp_path)
    api = GithubApi()

    draft = publication.publish_github_release(plan_path, stage="draft", api=api)
    draft_state = tmp_path / "draft-state.json"
    draft_state.write_bytes(core.canonical_bytes(draft) + b"\n")
    assets = publication.publish_github_release(
        plan_path,
        stage="assets",
        artifacts=artifacts,
        draft_state=draft_state,
        api=api,
        download_bytes=api.download,
    )
    asset_state = tmp_path / "asset-state.json"
    asset_state.write_bytes(core.canonical_bytes(assets) + b"\n")
    finalized = publication.publish_github_release(
        plan_path,
        stage="finalize",
        draft_state=draft_state,
        asset_state=asset_state,
        api=api,
        download_bytes=api.download,
    )
    authenticated_calls = len(api.calls)
    readback = publication.publish_github_release(
        plan_path,
        stage="readback",
        asset_state=asset_state,
        api=lambda *_args, **_kwargs: pytest.fail(
            "public readback used authenticated GitHub API"
        ),
        http_get=api.public_json,
        download_bytes=api.download,
    )

    assert draft["status"] == "created"
    assert assets["status"] == "uploaded"
    assert assets["asset_names"] == sorted([*WHEELS, CHART])
    assert len(assets["asset_manifest"]) == 7
    assert finalized["status"] == "finalized"
    assert readback["status"] == "verified"
    assert finalized["asset_manifest"] == assets["asset_manifest"]
    assert readback["asset_manifest"] == assets["asset_manifest"]
    assert len(api.calls) == authenticated_calls
    assert {
        draft["resolved_plan_sha256"],
        assets["resolved_plan_sha256"],
        finalized["resolved_plan_sha256"],
        readback["resolved_plan_sha256"],
    } == {core.load_json(plan_path)["resolved_plan_sha256"]}
    patches = [call for call in api.calls if call["method"] == "PATCH"]
    assert [call["body"] for call in patches] == [{"draft": False, "prerelease": True}]


def test_public_github_readback_rejects_wrong_same_name_bytes(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    artifacts = _artifacts(tmp_path / "artifacts")
    api = GithubApi(_public_release())
    manifest = publication._asset_manifest(artifacts)
    state = {
        "kind": "ucm-publication-result",
        "schema_version": 1,
        "channel": "github_release",
        "stage": "assets",
        "status": "reused",
        "resolved_plan_sha256": core.load_json(plan_path)["resolved_plan_sha256"],
        "release_id": 41,
        "tag": "v0.7.59rc1",
        "draft": False,
        "asset_names": sorted([*WHEELS, CHART]),
        "asset_manifest": manifest,
    }
    asset_state = tmp_path / "asset-state.json"
    asset_state.write_bytes(core.canonical_bytes(state) + b"\n")
    api.contents[WHEELS[0]] = b"wrong-public-bytes"

    with pytest.raises(ValueError, match="asset bytes differ"):
        publication.publish_github_release(
            plan_path,
            stage="readback",
            asset_state=asset_state,
            http_get=api.public_json,
            download_bytes=api.download,
        )


@pytest.mark.parametrize("stage", ["draft", "assets", "finalize", "readback"])
def test_github_release_rerun_reuses_exact_public_release_without_writes(
    plan_path: Path, tmp_path: Path, stage: str
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    api = GithubApi(_public_release())
    artifacts = _artifacts(tmp_path)
    public_binding = _public_binding(plan_path, tmp_path)
    asset_result = {
        "kind": "ucm-publication-result",
        "schema_version": 1,
        "channel": "github_release",
        "stage": "assets",
        "status": "reused",
        "resolved_plan_sha256": core.load_json(plan_path)["resolved_plan_sha256"],
        "release_id": 41,
        "tag": "v0.7.59rc1",
        "draft": False,
        "asset_names": sorted([*WHEELS, CHART]),
        "asset_manifest": publication._asset_manifest(artifacts),
    }
    asset_state = tmp_path / "asset-state.json"
    asset_state.write_bytes(core.canonical_bytes(asset_result) + b"\n")

    kwargs: dict[str, object] = {"stage": stage}
    if stage == "draft":
        kwargs["api"] = api
    elif stage == "assets":
        kwargs.update(
            artifacts=artifacts,
            draft_state=public_binding,
            api=api,
            download_bytes=api.download,
        )
    elif stage == "finalize":
        kwargs.update(
            draft_state=public_binding,
            asset_state=asset_state,
            api=api,
            download_bytes=api.download,
        )
    else:
        kwargs.update(
            asset_state=asset_state,
            api=lambda *_args, **_kwargs: pytest.fail(
                "public readback used authenticated API"
            ),
            http_get=api.public_json,
            download_bytes=api.download,
        )

    result = publication.publish_github_release(plan_path, **kwargs)

    assert result["status"] in {"reused", "verified"}
    assert all(call["method"] == "GET" for call in api.calls)


def test_github_release_incomplete_public_release_fails(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    api = GithubApi(_public_release(WHEELS))

    with pytest.raises(ValueError, match="exact seven assets"):
        publication.publish_github_release(plan_path, stage="draft", api=api)


def test_cli_publish_surface_has_no_layered_switches() -> None:
    cli = importlib.import_module("ucm_release.cli")
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "publish",
                "plan",
                "--output",
                "plan.json",
                "--allow",
                "{}",
            ]
        )
    for operation in (
        [
            "publish",
            "pypi",
            "--plan",
            "p.json",
            "--stage",
            "publish",
            "--wheels-dir",
            "w",
            "--draft-state",
            "draft.json",
            "--output",
            "out.json",
        ],
        [
            "publish",
            "dockerhub",
            "--plan",
            "p.json",
            "--stage",
            "readback",
            "--output",
            "out.json",
        ],
        [
            "publish",
            "chart-oci",
            "--plan",
            "p.json",
            "--stage",
            "publish",
            "--package",
            "c.tgz",
            "--draft-state",
            "draft.json",
            "--output",
            "out.json",
        ],
        [
            "publish",
            "ghcr",
            "--plan",
            "p.json",
            "--stage",
            "readback",
            "--output",
            "out.json",
        ],
        [
            "publish",
            "github-release",
            "--plan",
            "p.json",
            "--stage",
            "draft",
            "--output",
            "out.json",
        ],
        [
            "publish",
            "github-release",
            "--plan",
            "p.json",
            "--stage",
            "finalize",
            "--draft-state",
            "draft.json",
            "--asset-state",
            "assets.json",
            "--output",
            "out.json",
        ],
        [
            "publish",
            "github-release",
            "--plan",
            "p.json",
            "--stage",
            "readback",
            "--asset-state",
            "assets.json",
            "--output",
            "out.json",
        ],
    ):
        parser.parse_args(operation)


def test_every_publish_cli_writes_exact_stdout_to_atomic_output(
    plan_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("ucm_release.cli")
    publication = importlib.import_module("ucm_release.publish")
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir()
    artifacts_dir = tmp_path / "assets"
    artifacts_dir.mkdir()
    members_dir = tmp_path / "members"
    members_dir.mkdir()
    package = tmp_path / CHART
    package.write_bytes(b"chart")
    draft_state = _draft_state(plan_path, tmp_path)
    asset_state = tmp_path / "asset-state.json"
    asset_state.write_text("{}\n", encoding="utf-8")

    commands = [
        (
            "publish_pypi",
            [
                "publish",
                "pypi",
                "--plan",
                str(plan_path),
                "--stage",
                "publish",
                "--wheels-dir",
                str(wheels_dir),
                "--draft-state",
                str(draft_state),
            ],
        ),
        (
            "publish_pypi",
            [
                "publish",
                "pypi",
                "--plan",
                str(plan_path),
                "--stage",
                "readback",
                "--wheels-dir",
                str(wheels_dir),
            ],
        ),
        (
            "publish_ghcr",
            [
                "publish",
                "ghcr",
                "--plan",
                str(plan_path),
                "--stage",
                "publish",
                "--members-dir",
                str(members_dir),
                "--draft-state",
                str(draft_state),
            ],
        ),
        (
            "publish_ghcr",
            [
                "publish",
                "ghcr",
                "--plan",
                str(plan_path),
                "--stage",
                "readback",
            ],
        ),
        (
            "publish_dockerhub",
            [
                "publish",
                "dockerhub",
                "--plan",
                str(plan_path),
                "--stage",
                "publish",
                "--draft-state",
                str(draft_state),
            ],
        ),
        (
            "publish_dockerhub",
            [
                "publish",
                "dockerhub",
                "--plan",
                str(plan_path),
                "--stage",
                "readback",
            ],
        ),
        (
            "publish_chart_oci",
            [
                "publish",
                "chart-oci",
                "--plan",
                str(plan_path),
                "--stage",
                "publish",
                "--package",
                str(package),
                "--draft-state",
                str(draft_state),
            ],
        ),
        (
            "publish_chart_oci",
            [
                "publish",
                "chart-oci",
                "--plan",
                str(plan_path),
                "--stage",
                "readback",
                "--package",
                str(package),
                "--readback-dir",
                str(tmp_path / "pull"),
            ],
        ),
        (
            "publish_github_release",
            [
                "publish",
                "github-release",
                "--plan",
                str(plan_path),
                "--stage",
                "draft",
            ],
        ),
        (
            "publish_github_release",
            [
                "publish",
                "github-release",
                "--plan",
                str(plan_path),
                "--stage",
                "assets",
                "--artifacts-dir",
                str(artifacts_dir),
                "--draft-state",
                str(draft_state),
            ],
        ),
        (
            "publish_github_release",
            [
                "publish",
                "github-release",
                "--plan",
                str(plan_path),
                "--stage",
                "finalize",
                "--draft-state",
                str(draft_state),
                "--asset-state",
                str(asset_state),
            ],
        ),
        (
            "publish_github_release",
            [
                "publish",
                "github-release",
                "--plan",
                str(plan_path),
                "--stage",
                "readback",
                "--asset-state",
                str(asset_state),
            ],
        ),
    ]

    for index, (function_name, arguments) in enumerate(commands):
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        expected = {
            "kind": "ucm-publication-result",
            "schema_version": 1,
            "channel": function_name,
            "stage": arguments[arguments.index("--stage") + 1],
            "status": "fake",
            "resolved_plan_sha256": core.load_json(plan_path)["resolved_plan_sha256"],
        }

        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return expected

        monkeypatch.setattr(publication, function_name, fake)
        output = tmp_path / f"result-{index}.json"

        assert cli.main([*arguments, "--output", str(output)]) == 0

        stdout = capsys.readouterr().out
        assert stdout == output.read_text(encoding="utf-8")
        assert json.loads(stdout) == expected
        assert len(calls) == 1

    plan_output = tmp_path / "plan-result.json"
    assert (
        cli.main(
            [
                "publish",
                "plan",
                "--catalog",
                str(RELEASE_ROOT / "release.yaml"),
                "--repository-root",
                str(ROOT),
                "--repository",
                "release-org/unified-cache-management",
                "--output",
                str(plan_output),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == plan_output.read_text(encoding="utf-8")
    assert json.loads(plan_output.read_text(encoding="utf-8"))["kind"] == (
        "ucm-publish-plan"
    )


def test_publish_cli_rejects_stage_inappropriate_inputs_before_adapter(
    plan_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("ucm_release.cli")
    publication = importlib.import_module("ucm_release.publish")
    draft = _draft_state(plan_path, tmp_path)
    path = tmp_path / "input"
    path.mkdir()
    package = tmp_path / CHART
    package.write_bytes(b"chart")
    calls: list[object] = []

    def called(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    for name in (
        "publish_pypi",
        "publish_ghcr",
        "publish_dockerhub",
        "publish_chart_oci",
        "publish_github_release",
    ):
        monkeypatch.setattr(publication, name, called)

    invalid = [
        ["pypi", "--stage", "publish", "--wheels-dir", str(path)],
        [
            "pypi",
            "--stage",
            "readback",
            "--wheels-dir",
            str(path),
            "--draft-state",
            str(draft),
        ],
        ["ghcr", "--stage", "publish", "--members-dir", str(path)],
        ["ghcr", "--stage", "readback", "--members-dir", str(path)],
        ["dockerhub", "--stage", "publish"],
        ["dockerhub", "--stage", "readback", "--draft-state", str(draft)],
        [
            "chart-oci",
            "--stage",
            "publish",
            "--package",
            str(package),
            "--readback-dir",
            str(path),
            "--draft-state",
            str(draft),
        ],
        [
            "chart-oci",
            "--stage",
            "readback",
            "--package",
            str(package),
            "--draft-state",
            str(draft),
            "--readback-dir",
            str(path),
        ],
        ["github-release", "--stage", "draft", "--artifacts-dir", str(path)],
        ["github-release", "--stage", "assets", "--draft-state", str(draft)],
        [
            "github-release",
            "--stage",
            "finalize",
            "--draft-state",
            str(draft),
            "--artifacts-dir",
            str(path),
        ],
        ["github-release", "--stage", "readback", "--draft-state", str(draft)],
    ]
    for index, arguments in enumerate(invalid):
        output = tmp_path / f"invalid-{index}.json"
        with pytest.raises(SystemExit):
            cli.main(
                [
                    "publish",
                    *arguments,
                    "--plan",
                    str(plan_path),
                    "--output",
                    str(output),
                ]
            )
        assert not output.exists()
    assert calls == []


def test_publish_cli_failure_preserves_existing_output_atomically(
    plan_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("ucm_release.cli")
    publication = importlib.import_module("ucm_release.publish")
    output = tmp_path / "result.json"
    output.write_text("existing-complete-result\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise ValueError("transport failed")

    monkeypatch.setattr(publication, "publish_dockerhub", fail)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "publish",
                "dockerhub",
                "--plan",
                str(plan_path),
                "--stage",
                "publish",
                "--draft-state",
                str(_draft_state(plan_path, tmp_path)),
                "--output",
                str(output),
            ]
        )

    assert output.read_text(encoding="utf-8") == "existing-complete-result\n"


def test_expected_asset_names_are_literal_and_unique(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")

    assert publication.expected_release_asset_names(plan_path) == sorted(
        [*WHEELS, CHART]
    )
    assert len(set(WHEELS)) == 6


def test_no_publication_result_uses_a_member_digest_for_an_index(
    plan_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    result = publication.publish_ghcr(
        plan_path,
        stage="readback",
        run=OciRunner(),
        crane_binary="crane",
    )

    assert INDEX_DIGEST != AMD64_DIGEST != ARM64_DIGEST
    assert all(item["index_digest"] == INDEX_DIGEST for item in result["images"])


def test_chart_readback_rejects_byte_drift(plan_path: Path, tmp_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    package = tmp_path / CHART
    package.write_bytes(b"local")

    def run(command, **_kwargs):
        destination = Path(command[command.index("--destination") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / CHART).write_bytes(b"remote")
        return _completed(list(command))

    with pytest.raises(ValueError, match="differs from local package"):
        publication.publish_chart_oci(
            plan_path,
            package,
            stage="readback",
            readback_dir=tmp_path / "pull",
            run=run,
        )


def test_local_registry_dual_arch_index_exercises_shared_reader(tmp_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    crane = shutil.which("crane")
    version = (
        subprocess.run(
            [crane, "version"], text=True, capture_output=True, check=False
        ).stdout.strip()
        if crane is not None
        else "unavailable"
    )
    if version not in {"0.20.3", "v0.20.3"}:
        pytest.skip(
            f"local OCI integration requires pinned crane v0.20.3; found {version or 'unavailable'}"
        )
    docker = shutil.which("docker")
    assert (
        docker is not None
    ), "local OCI integration requires docker once crane is pinned"
    daemon = subprocess.run(
        [docker, "info"], text=True, capture_output=True, check=False
    )
    assert (
        daemon.returncode == 0
    ), "local OCI integration requires a working Docker daemon"
    registry_image = subprocess.run(
        [docker, "image", "inspect", "registry:2"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert (
        registry_image.returncode == 0
    ), "local OCI integration requires the local registry:2 fixture image"

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    container = "ucm-publish-test-" + uuid.uuid4().hex
    reference = f"localhost:{port}/ucm:test"
    started = subprocess.run(
        [
            docker,
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--publish",
            f"127.0.0.1:{port}:5000",
            "registry:2",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    try:
        endpoint = f"http://127.0.0.1:{port}/v2/"
        for _attempt in range(30):
            try:
                with urllib.request.urlopen(
                    endpoint, timeout=1
                ) as response:  # noqa: S310
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("ephemeral local OCI registry did not become ready")

        for architecture in ("amd64", "arm64"):
            created = subprocess.run(
                [
                    crane,
                    "append",
                    "--oci-empty-base",
                    "--platform",
                    f"linux/{architecture}",
                    "--new_tag",
                    f"localhost:{port}/ucm:{architecture}",
                    "--insecure",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            assert created.returncode == 0, created.stderr
        indexed = subprocess.run(
            [
                crane,
                "index",
                "append",
                "--manifest",
                f"localhost:{port}/ucm:amd64",
                "--manifest",
                f"localhost:{port}/ucm:arm64",
                "--tag",
                reference,
                "--insecure",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert indexed.returncode == 0, indexed.stderr

        result = publication.inspect_oci_reference(
            reference, crane_binary=crane, insecure=True
        )
        assert result["index_digest"].startswith("sha256:")
        assert result["platforms"] == ["linux/amd64", "linux/arm64"]
    finally:
        removed = subprocess.run(
            [docker, "rm", "--force", container],
            text=True,
            capture_output=True,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
