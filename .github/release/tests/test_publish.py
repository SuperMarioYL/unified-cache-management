from __future__ import annotations

import copy
import importlib
import json
import shutil
import socket
import subprocess
import sys
import time
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


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    catalog = core.load_catalog(version_override="0.7.59rc1")
    with mock.patch.object(registry, "resolve_builder_root", side_effect=_builder_root):
        plan = registry.resolve_catalog(
            catalog,
            builder_catalog=_builder_catalog(),
            source_sha="a" * 40,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )
    path = tmp_path / "resolved-plan.json"
    path.write_bytes(core.canonical_bytes(plan) + b"\n")
    return path


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


def _completed(command: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class OciRunner:
    def __init__(self, *, extra_platform: bool = False) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None, bytes | None]] = []
        self.extra_platform = extra_platform

    def __call__(self, command, *, env=None, input_bytes=None):
        command = list(command)
        copied_env = dict(env) if env is not None else None
        self.calls.append((command, copied_env, input_bytes))
        operation = command[1]
        if operation == "copy":
            return _completed(command)
        assert copied_env is not None
        docker_config = Path(copied_env["DOCKER_CONFIG"])
        assert docker_config.is_dir()
        assert list(docker_config.iterdir()) == []
        if operation == "digest":
            return _completed(command, INDEX_DIGEST + "\n")
        if operation == "manifest" and command[-1].endswith(INDEX_DIGEST):
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
            return _completed(
                command,
                json.dumps(
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": "sha256:" + "4" * 64,
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
    def __init__(self, release: dict[str, object] | None = None) -> None:
        self.release = copy.deepcopy(release)
        self.calls: list[dict[str, object]] = []

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
            name = path.rsplit("name=", 1)[1]
            self.release["assets"].append({"id": len(self.release["assets"]) + 1, "name": name})
            return {"id": len(self.release["assets"]), "name": name}
        if method == "PATCH":
            assert self.release is not None and isinstance(body, dict)
            self.release.update(body)
            return copy.deepcopy(self.release)
        raise AssertionError((path, method))


def _public_release(asset_names: list[str] | None = None) -> dict[str, object]:
    names = asset_names if asset_names is not None else [*WHEELS, CHART]
    return {
        "id": 41,
        "tag_name": "v0.7.59rc1",
        "target_commitish": "a" * 40,
        "draft": False,
        "prerelease": True,
        "assets": [{"id": index, "name": name} for index, name in enumerate(names)],
    }


@pytest.mark.parametrize(
    ("channel", "operation"),
    [
        ("pypi", "publish_pypi"),
        ("dockerhub", "publish_dockerhub"),
        ("ghcr", "readback_ghcr"),
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
    elif operation == "readback_ghcr":
        result = publication.readback_ghcr(
            plan_path, run=lambda *a, **k: calls.append((a, k))
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
        "reason": "disabled",
    }
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
        plan_path, artifacts, stage="publish", run=run
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

    result = publication.readback_ghcr(
        plan_path, run=runner, crane_binary="/pinned/crane"
    )

    assert result["status"] == "verified"
    assert len(result["images"]) == 3
    assert {item["index_digest"] for item in result["images"]} == {INDEX_DIGEST}
    assert len(runner.calls) == 18
    assert all(call[0][0] == "/pinned/crane" for call in runner.calls)
    assert sum(call[0][1] == "config" for call in runner.calls) == 6
    assert all("--platform" in call[0] for call in runner.calls if call[0][1] == "config")


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
    }


def test_ghcr_readback_rejects_extra_platform(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")

    with pytest.raises(ValueError, match="exact linux/amd64 and linux/arm64"):
        publication.readback_ghcr(
            plan_path, run=OciRunner(extra_platform=True), crane_binary="crane"
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
        publication.readback_ghcr(plan_path, run=OciRunner(), crane_binary="crane")


def test_dockerhub_copies_and_reads_exact_three_families(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    _rewrite_channel(plan_path, "dockerhub", True)
    runner = OciRunner()

    published = publication.publish_dockerhub(
        plan_path, stage="publish", run=runner, crane_binary="crane"
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
    assert len(readback["images"]) == 3


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
        plan_path, package, stage="publish", run=run
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
            plan_path, stage="assets", artifacts=artifacts, api=api
        )
    assert all(call["method"] == "GET" for call in api.calls)


def test_github_release_draft_assets_finalize_and_readback(
    plan_path: Path, tmp_path: Path
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    artifacts = _artifacts(tmp_path)
    api = GithubApi()

    draft = publication.publish_github_release(plan_path, stage="draft", api=api)
    assets = publication.publish_github_release(
        plan_path, stage="assets", artifacts=artifacts, api=api
    )
    finalized = publication.publish_github_release(
        plan_path, stage="finalize", api=api
    )
    readback = publication.publish_github_release(
        plan_path, stage="readback", api=api
    )

    assert draft["status"] == "created"
    assert assets["status"] == "uploaded"
    assert assets["asset_names"] == sorted([*WHEELS, CHART])
    assert finalized["status"] == "finalized"
    assert readback["status"] == "verified"
    patches = [call for call in api.calls if call["method"] == "PATCH"]
    assert [call["body"] for call in patches] == [
        {"draft": False, "prerelease": True}
    ]


@pytest.mark.parametrize("stage", ["draft", "assets", "finalize", "readback"])
def test_github_release_rerun_reuses_exact_public_release_without_writes(
    plan_path: Path, tmp_path: Path, stage: str
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    api = GithubApi(_public_release())
    artifacts = _artifacts(tmp_path) if stage == "assets" else None

    result = publication.publish_github_release(
        plan_path, stage=stage, artifacts=artifacts, api=api
    )

    assert result["status"] in {"reused", "verified"}
    assert all(call["method"] == "GET" for call in api.calls)


def test_github_release_incomplete_public_release_fails(plan_path: Path) -> None:
    publication = importlib.import_module("ucm_release.publish")
    api = GithubApi(_public_release(WHEELS))

    with pytest.raises(ValueError, match="exact seven assets"):
        publication.publish_github_release(plan_path, stage="draft", api=api)


def test_github_assets_rerun_reuses_public_release_without_local_files(
    plan_path: Path,
) -> None:
    publication = importlib.import_module("ucm_release.publish")
    api = GithubApi(_public_release())

    result = publication.publish_github_release(
        plan_path, stage="assets", artifacts=None, api=api
    )

    assert result["status"] == "reused"
    assert all(call["method"] == "GET" for call in api.calls)


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
        ["publish", "pypi", "--plan", "p.json", "--stage", "publish", "--wheels-dir", "w"],
        ["publish", "dockerhub", "--plan", "p.json", "--stage", "readback"],
        ["publish", "chart-oci", "--plan", "p.json", "--stage", "publish", "--package", "c.tgz"],
        ["publish", "ghcr-readback", "--plan", "p.json"],
        ["publish", "github-release", "--plan", "p.json", "--stage", "draft"],
    ):
        parser.parse_args(operation)


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
    result = publication.readback_ghcr(
        plan_path, run=OciRunner(), crane_binary="crane"
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
    docker = shutil.which("docker")
    crane = shutil.which("crane")
    if docker is None or crane is None:
        pytest.skip("local OCI integration requires docker and pinned crane v0.20.3")
    version = subprocess.run(
        [crane, "version"], text=True, capture_output=True, check=False
    ).stdout.strip()
    if version not in {"0.20.3", "v0.20.3"}:
        pytest.skip(f"local OCI integration requires pinned crane v0.20.3; found {version or 'unavailable'}")
    daemon = subprocess.run(
        [docker, "info"], text=True, capture_output=True, check=False
    )
    if daemon.returncode != 0:
        pytest.skip("local OCI integration requires a working Docker daemon")
    registry_image = subprocess.run(
        [docker, "image", "inspect", "registry:2"],
        text=True,
        capture_output=True,
        check=False,
    )
    if registry_image.returncode != 0:
        pytest.skip("local OCI integration requires the local registry:2 fixture image")

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
                with urllib.request.urlopen(endpoint, timeout=1) as response:  # noqa: S310
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
