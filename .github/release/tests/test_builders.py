from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
cli = importlib.import_module("ucm_release.cli")
FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "builders"


def _discover(snapshot: Path = FIXTURE) -> dict[str, object]:
    return builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=snapshot,
        owner="release-org",
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "builders"
    shutil.copytree(FIXTURE, destination)
    return destination


def test_snapshot_discovers_current_eight_upstream_builders() -> None:
    catalog = _discover()

    upstream = [item for item in catalog["builders"] if item["build_mode"] != "copy"]
    assert len(upstream) == 8
    assert {item["target_tag"] for item in upstream} == {
        "cuda12.9-cp312-manylinux2_28-amd64-r1",
        "cuda12.9-cp312-manylinux2_28-arm64-r1",
        "cuda13.0-cp312-manylinux2_28-amd64-r1",
        "cuda13.0-cp312-manylinux2_28-arm64-r1",
        "cann9.1.0-a2-cp312-manylinux2_34-mooncake0.3.9-amd64-r1",
        "cann9.1.0-a2-cp312-manylinux2_34-mooncake0.3.9-arm64-r1",
        "cann9.1.0-a3-cp312-manylinux2_34-mooncake0.3.9-amd64-r1",
        "cann9.1.0-a3-cp312-manylinux2_34-mooncake0.3.9-arm64-r1",
    }


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


def test_duplicate_upstream_tasks_collapse_by_capability(snapshot: Path) -> None:
    pipeline = snapshot / "vllm-project/vllm/.buildkite/release-pipeline.yaml"
    pipeline.write_text(
        pipeline.read_text(encoding="utf-8")
        + "\n- id: build-wheel-x86-cuda-13-0\n"
        + "  commands:\n"
        + "  - docker build --build-arg BUILD_BASE_IMAGE=pytorch/manylinux2_28-builder:cuda13.0 .\n",
        encoding="utf-8",
    )

    upstream = [
        item for item in _discover(snapshot)["builders"] if item["build_mode"] != "copy"
    ]

    assert len(upstream) == 8


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

    catalog["builders"].append(wanted)
    catalog["builders"].append(dict(wanted))
    with pytest.raises(ValueError) as multiple:
        builders.select_builders(catalog, release)
    assert "multiple (2) builder for requested capability" in str(multiple.value)
    assert "cpu_arch=amd64" in str(multiple.value)


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
