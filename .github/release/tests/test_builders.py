from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
cli = importlib.import_module("ucm_release.cli")
core = importlib.import_module("ucm_release.core")
FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "builders"


def _discover(snapshot: Path = FIXTURE) -> dict[str, object]:
    return builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=snapshot,
        owner="release-org",
    )


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RELEASE_ROOT)
    env["GITHUB_REPOSITORY"] = "release-org/unified-cache-management"
    return subprocess.run(
        [sys.executable, "-m", "ucm_release", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "builders"
    shutil.copytree(FIXTURE, destination)
    return destination


def test_snapshot_excludes_310p_and_covers_both_architectures() -> None:
    catalog = _discover()

    upstream = [item for item in catalog["builders"] if item["build_mode"] != "copy"]
    assert upstream
    assert all(item["variant"] != "310p" for item in upstream)
    assert {item["cpu_arch"] for item in upstream} == {"amd64", "arm64"}


def test_owner_is_lowercased_for_explicit_and_inferred_oci_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=FIXTURE,
        owner="Release-Org",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mixed-Owner/Unified-Cache-Management")
    inferred = builders.discover_builders(
        RELEASE_ROOT / "builders.yaml", snapshot_dir=FIXTURE
    )

    for catalog, owner in ((explicit, "release-org"), (inferred, "mixed-owner")):
        assert all(
            item["target_repository"].startswith(f"ghcr.io/{owner}/")
            for item in catalog["builders"]
        )
        assert all(
            "GHCR.io" not in item["source_image"] for item in catalog["builders"]
        )


def test_builder_config_rejects_duplicate_keys_with_file_context(
    tmp_path: Path,
) -> None:
    config = tmp_path / "builders.yaml"
    config.write_text(
        "kind: builder-discovery-config\n"
        "kind: builder-discovery-config\n"
        "schema_version: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        builders.load_config(config)

    assert str(config) in str(error.value)
    assert "duplicate YAML key: kind" in str(error.value)


def test_builder_config_rejects_malformed_yaml_with_file_context(
    tmp_path: Path,
) -> None:
    config = tmp_path / "builders.yaml"
    config.write_text("projects: [\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        builders.load_config(config)

    assert f"{config}: malformed YAML" in str(error.value)


@pytest.mark.parametrize("reference", ["target", "source"])
def test_discovery_rejects_invalid_resulting_oci_references(
    tmp_path: Path, reference: str
) -> None:
    config_value = yaml.safe_load(
        (RELEASE_ROOT / "builders.yaml").read_text(encoding="utf-8")
    )
    if reference == "target":
        config_value["projects"][0][
            "target_repository"
        ] = "GHCR.io/{owner}/ucm-builder-vllm"
    else:
        config_value["retained_builders"][0][
            "source_image"
        ] = "GHCR.io/{owner}/ucm-builder:tag"
    config = tmp_path / "builders.yaml"
    config.write_text(yaml.safe_dump(config_value, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match=r"invalid OCI repository"):
        builders.discover_builders(config, snapshot_dir=FIXTURE, owner="release-org")


def test_sync_plan_schedules_only_missing_target_tags() -> None:
    catalog = _discover()
    first = catalog["builders"][0]
    existing = {first["target_repository"]: [first["target_tag"]]}

    plan = builders.compute_sync_plan(catalog, existing)

    assert first not in plan["builders"]
    assert plan["matrix"] == {"include": plan["builders"]}
    assert len(plan["builders"]) == len(catalog["builders"]) - 1
    assert "deletions" not in plan


def test_selects_exactly_current_six_release_builders() -> None:
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )

    selection = builders.select_builders(_discover(), release)

    assert len(selection["builders"]) == 6
    assert {item["target_tag"] for item in selection["builders"]} == {
        "cuda13.0-cp312-manylinux2_28-amd64-r1",
        "cuda13.0-cp312-manylinux2_28-arm64-r1",
        "cann9.0.0-a2-cp312-manylinux2_34-mooncake0.3.9-amd64-r1",
        "cann9.0.0-a2-cp312-manylinux2_34-mooncake0.3.9-arm64-r1",
        "cann9.0.0-a3-cp312-manylinux2_34-mooncake0.3.9-amd64-r1",
        "cann9.0.0-a3-cp312-manylinux2_34-mooncake0.3.9-arm64-r1",
    }
    assert all("12.9" not in item["target_tag"] for item in selection["builders"])
    assert all("9.1.0" not in item["target_tag"] for item in selection["builders"])
    assert any("12.9" in item["target_tag"] for item in _discover()["builders"])
    assert any("9.1.0" in item["target_tag"] for item in _discover()["builders"])


def _current_selection() -> dict[str, object]:
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    return builders.select_builders(_discover(), release)


def test_bind_selection_adds_only_current_six_builder_coordinates() -> None:
    catalog = core.load_catalog()
    original = copy.deepcopy(catalog)

    bound = builders.bind_selection(catalog, _current_selection())

    assert catalog == original
    roots = {
        (profile["id"], architecture): requirement["root"]
        for profile in bound["wheel_profiles"]
        for architecture, requirement in profile["builders"].items()
    }
    assert len(roots) == 6
    assert {root["repository"] for root in roots.values()} == {
        "ghcr.io/release-org/ucm-builder-vllm",
        "ghcr.io/release-org/ucm-builder-vllm-ascend",
    }
    assert {root["tag"] for root in roots.values()} == {
        "cuda13.0-cp312-manylinux2_28-amd64-r1",
        "cuda13.0-cp312-manylinux2_28-arm64-r1",
        "cann9.0.0-a2-cp312-manylinux2_34-mooncake0.3.9-amd64-r1",
        "cann9.0.0-a2-cp312-manylinux2_34-mooncake0.3.9-arm64-r1",
        "cann9.0.0-a3-cp312-manylinux2_34-mooncake0.3.9-amd64-r1",
        "cann9.0.0-a3-cp312-manylinux2_34-mooncake0.3.9-arm64-r1",
    }
    assert all(set(root) == {"repository", "tag"} for root in roots.values())


def test_bind_selection_rejects_missing_and_duplicate_coordinates() -> None:
    selection = _current_selection()
    selection["builders"].pop()
    selection["matrix"]["include"] = selection["builders"]
    with pytest.raises(ValueError, match="missing builder selection"):
        builders.bind_selection(core.load_catalog(), selection)

    selection = _current_selection()
    selection["builders"].append(copy.deepcopy(selection["builders"][0]))
    selection["matrix"]["include"] = selection["builders"]
    with pytest.raises(ValueError, match="duplicate builder selection"):
        builders.bind_selection(core.load_catalog(), selection)


def test_bind_selection_rejects_unknown_profile_and_architecture() -> None:
    selection = _current_selection()
    selection["builders"][0]["profile_id"] = "unknown-profile"
    selection["matrix"]["include"] = selection["builders"]
    with pytest.raises(ValueError, match="unknown release profile"):
        builders.bind_selection(core.load_catalog(), selection)

    selection = _current_selection()
    selection["builders"][0]["cpu_arch"] = "s390x"
    selection["matrix"]["include"] = selection["builders"]
    with pytest.raises(ValueError, match="undeclared architecture"):
        builders.bind_selection(core.load_catalog(), selection)


def test_bind_selection_rejects_profile_capability_mismatch() -> None:
    selection = _current_selection()
    selected = selection["builders"]
    cuda_amd64 = next(
        item
        for item in selected
        if item["profile_id"] == "cuda130" and item["cpu_arch"] == "amd64"
    )
    cann_a2_amd64 = next(
        item
        for item in selected
        if item["profile_id"] == "cann900-a2" and item["cpu_arch"] == "amd64"
    )
    selected.remove(cann_a2_amd64)
    cuda_amd64["profile_id"] = "cann900-a2"
    selection["matrix"]["include"] = selected

    with pytest.raises(ValueError, match="does not match release profile"):
        builders.bind_selection(core.load_catalog(), selection)


def test_release_profile_owns_builder_manylinux_selection() -> None:
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    assert {
        profile["id"]: profile["builder_manylinux"]
        for profile in release["wheel_profiles"]
    } == {
        "cuda130": "manylinux_2_28",
        "cann900-a2": "manylinux_2_34",
        "cann900-a3": "manylinux_2_34",
    }
    catalog = _discover()
    for item in catalog["builders"]:
        if item["accelerator_runtime"] == "cann-9.0.0":
            item["manylinux"] = "manylinux_2_35"

    with pytest.raises(ValueError) as error:
        builders.select_builders(catalog, release)

    assert "missing builder for requested capability" in str(error.value)
    assert "manylinux=manylinux_2_34" in str(error.value)


def test_310p_is_the_only_excluded_ascend_variant() -> None:
    catalog = _discover()
    variants = {item["variant"] for item in catalog["builders"]}

    assert "310p" not in variants
    assert {"a2", "a3"} <= variants


def test_future_nonexcluded_ascend_variant_enters_catalog(snapshot: Path) -> None:
    directory = snapshot / "vllm-project/vllm-ascend/.github/workflows/dockerfiles"
    (directory / "Dockerfile.buildwheel.a5").write_text(
        "ARG PY_VERSION=3.12\n"
        "FROM quay.io/ascend/manylinux:9.2.0-a5-manylinux_2_34-py${PY_VERSION}\n",
        encoding="utf-8",
    )

    added = [
        item for item in _discover(snapshot)["builders"] if item["variant"] == "a5"
    ]

    assert {item["cpu_arch"] for item in added} == {"amd64", "arm64"}


def test_mocked_live_github_source_matches_snapshot_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    class Response:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self.payload

    def urlopen(request):
        url = request.full_url
        requested.append(url)
        for project in ("vllm-project/vllm", "vllm-project/vllm-ascend"):
            if url == f"https://api.github.com/repos/{project}":
                return Response(b'{"default_branch":"main"}')
        contents_prefix = (
            "https://api.github.com/repos/vllm-project/vllm-ascend/contents/"
            ".github/workflows/dockerfiles?ref=main"
        )
        if url == contents_prefix:
            directory = FIXTURE / (
                "vllm-project/vllm-ascend/.github/workflows/dockerfiles"
            )
            payload = [{"name": path.name} for path in sorted(directory.iterdir())]
            return Response(json.dumps(payload).encode("utf-8"))
        raw_prefix = "https://raw.githubusercontent.com/"
        if url.startswith(raw_prefix):
            relative = url.removeprefix(raw_prefix)
            project_and_path = relative.replace("/main/", "/", 1)
            return Response((FIXTURE / project_and_path).read_bytes())
        raise AssertionError(f"unexpected GitHub URL {url}")

    monkeypatch.setattr(builders.urllib.request, "urlopen", urlopen)

    live = builders.discover_builders(
        RELEASE_ROOT / "builders.yaml", owner="release-org"
    )
    snapshot_catalog = _discover()

    canonical = lambda value: json.dumps(  # noqa: E731
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert canonical(live) == canonical(snapshot_catalog)
    assert "https://api.github.com/repos/vllm-project/vllm" in requested
    assert "https://api.github.com/repos/vllm-project/vllm-ascend" in requested


def test_duplicate_upstream_tasks_collapse_by_capability(snapshot: Path) -> None:
    before = [
        item for item in _discover(snapshot)["builders"] if item["build_mode"] != "copy"
    ]
    pipeline = snapshot / "vllm-project/vllm/.buildkite/release-pipeline.yaml"
    pipeline.write_text(
        pipeline.read_text(encoding="utf-8")
        + "\n- id: build-wheel-x86-cuda-13-0\n"
        + "  commands:\n"
        + "  - docker build --build-arg BUILD_BASE_IMAGE=pytorch/manylinux2_28-builder:cuda13.0 .\n",
        encoding="utf-8",
    )

    after = [
        item for item in _discover(snapshot)["builders"] if item["build_mode"] != "copy"
    ]
    identity_fields = (
        "project",
        "accelerator",
        *builders.CAPABILITY_FIELDS,
        "target_repository",
    )
    identities = lambda items: {  # noqa: E731
        tuple(item[field] for field in identity_fields) for item in items
    }

    assert identities(after) == identities(before)
    assert len(after) == len(identities(after))


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("docker build .", "missing BUILD_BASE_IMAGE"),
        (
            "docker build --build-arg BUILD_BASE_IMAGE=example/base:latest .",
            "malformed BUILD_BASE_IMAGE",
        ),
    ],
)
def test_vllm_build_base_image_errors_include_project_file_and_task(
    snapshot: Path, command: str, message: str
) -> None:
    pipeline = snapshot / "vllm-project/vllm/.buildkite/release-pipeline.yaml"
    pipeline.write_text(
        "steps:\n- id: build-wheel-x86-cuda-13-0\n"
        f"  commands: [{json.dumps(command)}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        _discover(snapshot)

    detail = str(error.value)
    assert "vllm-project/vllm/.buildkite/release-pipeline.yaml" in detail
    assert "task build-wheel-x86-cuda-13-0" in detail
    assert message in detail


def test_vllm_missing_python_and_missing_matrix_are_contextual(snapshot: Path) -> None:
    versions = snapshot / "vllm-project/vllm/docker/versions.json"
    versions.write_text('{"variable":{}}\n', encoding="utf-8")
    with pytest.raises(
        ValueError, match=r"vllm-project/vllm/docker/versions.json: missing Python"
    ):
        _discover(snapshot)

    versions.write_text(
        '{"variable":{"PYTHON_VERSION":{"default":"3.12"}}}\n', encoding="utf-8"
    )
    pipeline = snapshot / "vllm-project/vllm/.buildkite/release-pipeline.yaml"
    pipeline.write_text("steps: []\n", encoding="utf-8")
    with pytest.raises(
        ValueError,
        match=r"vllm-project/vllm/.buildkite/release-pipeline.yaml: missing .* matrix",
    ):
        _discover(snapshot)


def test_malformed_buildkite_yaml_has_project_and_file_context(snapshot: Path) -> None:
    pipeline = snapshot / "vllm-project/vllm/.buildkite/release-pipeline.yaml"
    pipeline.write_text("steps: [\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        _discover(snapshot)

    assert "vllm-project/vllm/.buildkite/release-pipeline.yaml: malformed YAML" in str(
        error.value
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "ARG PY_VERSION=3.12\nFROM quay.io/ascend/manylinux:${CANN}-a2-manylinux_2_34-py${PY_VERSION}\n",
            "unresolved ARG CANN in FROM",
        ),
        ("ARG PY_VERSION=3.12\nRUN true\n", "missing FROM"),
        (
            "FROM quay.io/ascend/manylinux:9.1.0-a2-manylinux_2_34-py3.12\n",
            "missing ARG PY_VERSION",
        ),
    ],
)
def test_ascend_arg_and_from_errors_include_project_file(
    snapshot: Path, content: str, message: str
) -> None:
    dockerfile = snapshot / (
        "vllm-project/vllm-ascend/.github/workflows/dockerfiles/Dockerfile.buildwheel.a2"
    )
    dockerfile.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        _discover(snapshot)

    detail = str(error.value)
    assert (
        "vllm-project/vllm-ascend/.github/workflows/dockerfiles/Dockerfile.buildwheel.a2"
        in detail
    )
    assert message in detail


def test_malformed_ascend_variant_is_not_silently_ignored(snapshot: Path) -> None:
    directory = snapshot / "vllm-project/vllm-ascend/.github/workflows/dockerfiles"
    (directory / "Dockerfile.buildwheel.a2_bad").write_text(
        "ARG PY_VERSION=3.12\n"
        "FROM quay.io/ascend/manylinux:9.1.0-a2-manylinux_2_34-py${PY_VERSION}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=r"Dockerfile\.buildwheel\.a2_bad: malformed variant"
    ):
        _discover(snapshot)


def test_existing_exact_tags_produce_empty_no_delete_plan() -> None:
    catalog = _discover()
    existing: dict[str, list[str]] = {}
    for item in catalog["builders"]:
        existing.setdefault(item["target_repository"], []).append(item["target_tag"])
    existing.setdefault("ghcr.io/release-org/retired", []).append("keep-me")

    plan = builders.compute_sync_plan(catalog, existing)

    assert plan["builders"] == []
    assert plan["matrix"] == {"include": []}
    assert "deletions" not in plan


def test_selection_missing_and_multiple_candidates_hard_fail() -> None:
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    catalog = _discover()
    wanted = next(
        item
        for item in catalog["builders"]
        if item["target_tag"] == "cuda13.0-cp312-manylinux2_28-amd64-r1"
    )
    catalog["builders"].remove(wanted)
    with pytest.raises(ValueError) as missing:
        builders.select_builders(catalog, release)
    assert "missing builder for requested capability" in str(missing.value)
    assert "accelerator_runtime=cuda-13.0" in str(missing.value)
    assert "nearest candidates" in str(missing.value)

    alternate = dict(wanted)
    alternate["project"] = "downstream/vllm"
    alternate["target_repository"] = "ghcr.io/release-org/alternate-vllm-builder"
    catalog["builders"].append(wanted)
    catalog["builders"].append(alternate)
    with pytest.raises(ValueError) as multiple:
        builders.select_builders(catalog, release)
    detail = str(multiple.value)
    assert "multiple (2) builder for requested capability" in detail
    assert "cpu_arch=amd64" in detail
    primary_ref = f"{wanted['target_repository']}:{wanted['target_tag']}"
    alternate_ref = f"{alternate['target_repository']}:{alternate['target_tag']}"
    assert detail.index(primary_ref) < detail.index("cuda12.9")
    assert detail.index(alternate_ref) < detail.index("cuda12.9")


def test_selection_rejects_duplicate_profile_ids() -> None:
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    release["wheel_profiles"].append(dict(release["wheel_profiles"][0]))

    with pytest.raises(ValueError, match=r"duplicate release profile id: cuda130"):
        builders.select_builders(_discover(), release)


def test_selection_rejects_duplicate_profile_architectures() -> None:
    release = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    release["wheel_profiles"][0]["cpu_arch"] = ["amd64", "amd64"]

    with pytest.raises(
        ValueError, match=r"wheel_profiles\[0\]: cpu_arch contains duplicates"
    ):
        builders.select_builders(_discover(), release)


def test_builders_cli_writes_canonical_json_and_stdout(tmp_path: Path, capsys) -> None:
    catalog_path = tmp_path / "builder-catalog.json"

    assert (
        cli.main(
            [
                "builders",
                "discover",
                "--config",
                str(RELEASE_ROOT / "builders.yaml"),
                "--snapshot",
                str(FIXTURE),
                "--owner",
                "release-org",
                "--output",
                str(catalog_path),
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    assert stdout == catalog_path.read_text(encoding="utf-8")
    assert json.loads(stdout)["kind"] == "ucm-builder-catalog"

    existing_path = tmp_path / "existing.json"
    existing_path.write_text("{}\n", encoding="utf-8")
    sync_path = tmp_path / "sync-plan.json"
    assert (
        cli.main(
            [
                "builders",
                "sync-plan",
                "--catalog",
                str(catalog_path),
                "--existing",
                str(existing_path),
                "--output",
                str(sync_path),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == sync_path.read_text(encoding="utf-8")
    assert json.loads(sync_path.read_text(encoding="utf-8"))["matrix"]["include"]

    selection_path = tmp_path / "builder-selection.json"
    assert (
        cli.main(
            [
                "builders",
                "select",
                "--catalog",
                str(catalog_path),
                "--release",
                str(RELEASE_ROOT / "release.yaml"),
                "--output",
                str(selection_path),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == selection_path.read_text(encoding="utf-8")
    assert (
        len(json.loads(selection_path.read_text(encoding="utf-8"))["matrix"]["include"])
        == 6
    )


def test_builders_cli_failures_leave_no_partial_output(tmp_path: Path) -> None:
    bad_config = tmp_path / "builders.yaml"
    bad_config.write_text(
        "kind: builder-discovery-config\nkind: builder-discovery-config\n",
        encoding="utf-8",
    )
    discover_output = tmp_path / "discover.json"
    discover = _run_cli(
        "builders",
        "discover",
        "--config",
        str(bad_config),
        "--snapshot",
        str(FIXTURE),
        "--output",
        str(discover_output),
    )
    assert discover.returncode == 2
    assert discover.stdout == ""
    assert str(bad_config) in discover.stderr
    assert "duplicate YAML key: kind" in discover.stderr
    assert not discover_output.exists()

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(_discover(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    bad_release = tmp_path / "release.yaml"
    bad_release.write_text(
        "kind: release-config\n"
        + (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    select_output = tmp_path / "select.json"
    select = _run_cli(
        "builders",
        "select",
        "--catalog",
        str(catalog_path),
        "--release",
        str(bad_release),
        "--output",
        str(select_output),
    )
    assert select.returncode == 2
    assert select.stdout == ""
    assert str(bad_release) in select.stderr
    assert "duplicate YAML key: kind" in select.stderr
    assert not select_output.exists()


def test_catalog_resolve_requires_builder_catalog(tmp_path: Path) -> None:
    result = _run_cli(
        "catalog",
        "resolve",
        "--fixture",
        str(RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"),
        "--lane",
        "feature-candidate",
        "--source-sha",
        "0" * 40,
        "--output",
        str(tmp_path / "resolved-plan.json"),
    )

    assert result.returncode == 2
    assert "--builder-catalog" in result.stderr


def test_catalog_resolve_with_builder_catalog_keeps_canonical_output(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_catalog_path = tmp_path / "builder-catalog.json"
    builder_catalog_path.write_text(
        json.dumps(_discover(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    digest = "sha256:" + "e" * 64
    monkeypatch.setattr(
        cli.catalog_resolution,
        "resolve_builder_root",
        lambda repository, tag, *, architecture: {
            "index_digest": digest,
            "manifest_digest": digest,
            "config_digest": digest,
            "operations": [],
        },
    )
    output = tmp_path / "resolved-plan.json"

    assert (
        cli.main(
            [
                "catalog",
                "resolve",
                "--builder-catalog",
                str(builder_catalog_path),
                "--fixture",
                str(RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"),
                "--lane",
                "feature-candidate",
                "--source-sha",
                "0" * 40,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == output.read_text(encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8"))["counts"]["wheel_tasks"] == 6
