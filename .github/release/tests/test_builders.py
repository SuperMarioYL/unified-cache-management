"""Contracts for upstream-driven selection and Builder synchronization."""

from __future__ import annotations

import copy
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "builders"
TAG_FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
compact = importlib.import_module("ucm_release.compact")
core = importlib.import_module("ucm_release.core")
upstream = importlib.import_module("ucm_release.upstream")
cli = importlib.import_module("ucm_release.cli")


def _release() -> dict[str, object]:
    return core.load_catalog(version_override="0.7.59rc7")


def _selection(snapshot: Path = FIXTURE) -> dict[str, object]:
    return upstream.resolve_upstreams(
        _release(),
        builders.load_config(),
        tag_fixture=core.load_json(TAG_FIXTURE),
        snapshot_dir=snapshot,
    )


def _catalog(snapshot: Path = FIXTURE) -> dict[str, object]:
    return builders.catalog_from_selection(_selection(snapshot), owner="release-org")


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "builders"
    shutil.copytree(FIXTURE, destination)
    return destination


def test_selection_discovers_current_16_wheel_groups_and_four_runtimes() -> None:
    selection = _selection()

    assert {
        item["family_id"]: (item["runtime_tag"], len(item["build_groups"]))
        for item in selection["upstreams"]
    } == {
        "cuda129": ("v0.21.2-cu129", 2),
        "cuda130": ("v0.21.2", 2),
        "cann900-a2": ("v0.22.2rc1", 6),
        "cann900-a3": ("v0.22.2rc1-a3", 6),
    }
    groups = [
        group for item in selection["upstreams"] for group in item["build_groups"]
    ]
    assert len(groups) == 16
    assert len({group["id"] for group in groups}) == 16
    assert {
        group["python_abi"] for group in groups if group["accelerator"] == "ascend"
    } == {
        "cp310",
        "cp311",
        "cp312",
    }
    assert "310p" not in {group["variant"] for group in groups}


def test_vllm_accepts_only_cuda_wheels_from_main_and_additional_groups() -> None:
    groups = [
        group
        for item in _selection()["upstreams"]
        if item["product_id"] == "vllm"
        for group in item["build_groups"]
    ]

    assert {group["build_group"] for group in groups} == {"cuda129", "cuda130"}
    assert {group["cpu_arch"] for group in groups} == {"amd64", "arm64"}
    assert {group["python_abi"] for group in groups} == {"cp312"}
    assert all(group["build_mode"] == "mirror" for group in groups)
    cuda129 = [group for group in groups if group["build_group"] == "cuda129"]
    assert all(group["recipe"]["build_args"]["USE_SCCACHE"] == "0" for group in cuda129)
    assert all(group["recipe"]["build_args"]["max_jobs"] == "2" for group in cuda129)
    assert all(
        group["recipe"]["build_args"]["nvcc_threads"] == "2" for group in cuda129
    )
    assert all(group["recipe"]["target"] == "base" for group in cuda129)


def test_ascend_recipe_uses_workflow_python_and_runner_matrix() -> None:
    groups = [
        group
        for item in _selection()["upstreams"]
        if item["product_id"] == "vllm-ascend"
        for group in item["build_groups"]
    ]

    assert len(groups) == 12
    assert {group["cpu_arch"] for group in groups} == {"amd64", "arm64"}
    assert {group["manylinux"] for group in groups} == {"manylinux_2_28"}
    assert {group["soc_version"] for group in groups} == {
        "ascend910b1",
        "ascend910_9391",
    }
    assert all(group["build_mode"] == "recipe-extend" for group in groups)
    assert all(
        group["recipe"]["strip_run_containing"] == "python3 setup.py bdist_wheel"
        for group in groups
    )


def test_materialized_ascend_recipe_stops_before_product_wheel() -> None:
    source = (
        FIXTURE
        / "vllm-project/vllm-ascend/.github/workflows/dockerfiles/Dockerfile.buildwheel.a2"
    ).read_text(encoding="utf-8")

    materialized = upstream.materialize_builder_recipe(
        source, "python3 setup.py bdist_wheel"
    )

    assert "pip install -r requirements.txt" in materialized
    assert "python3 setup.py bdist_wheel" not in materialized
    assert 'CMD ["/bin/bash"]' in materialized


def test_materialized_recipe_rejects_missing_or_ambiguous_marker() -> None:
    with pytest.raises(ValueError, match="exactly one RUN"):
        upstream.materialize_builder_recipe("FROM base\nRUN true\n", "bdist_wheel")
    with pytest.raises(ValueError, match="exactly one RUN"):
        upstream.materialize_builder_recipe(
            "FROM base\nRUN python3 setup.py bdist_wheel\n"
            "RUN python3 setup.py bdist_wheel\n",
            "python3 setup.py bdist_wheel",
        )


def test_builder_catalog_carries_source_ref_recipe_and_append_only_identity() -> None:
    catalog = _catalog()

    assert catalog["schema_version"] == 2
    assert len(catalog["builders"]) == 16
    assert all(item["source_ref"].startswith("v") for item in catalog["builders"])
    assert all(isinstance(item["recipe"], dict) for item in catalog["builders"])
    assert len({item["target_tag"] for item in catalog["builders"]}) == 16
    assert all(item["source_ref"] in item["target_tag"] for item in catalog["builders"])

    existing = {
        item["target_repository"]: [item["target_tag"]]
        for item in catalog["builders"][:1]
    }
    existing["ghcr.io/release-org/retired"] = ["keep-me"]
    sync = builders.compute_sync_plan(catalog, existing)
    assert len(sync["builders"]) == 15
    assert "deletions" not in sync


def test_builder_owner_is_normalized() -> None:
    catalog = builders.catalog_from_selection(_selection(), owner="Release-Org")
    assert {item["target_repository"] for item in catalog["builders"]} == {
        "ghcr.io/release-org/ucm-builder-vllm",
        "ghcr.io/release-org/ucm-builder-vllm-ascend",
    }


def test_missing_runtime_variant_fails_before_builder_matrix() -> None:
    fixture = core.load_json(TAG_FIXTURE)
    pages = fixture["repositories"]["docker.io/vllm/vllm-openai"]["pages"]
    for page in pages:
        page["tags"] = [tag for tag in page["tags"] if tag != "v0.21.2-cu129"]

    with pytest.raises(ValueError, match="runtime variant 'v0.21.2-cu129'"):
        upstream.resolve_upstreams(
            _release(),
            builders.load_config(),
            tag_fixture=fixture,
            snapshot_dir=FIXTURE,
        )


def test_missing_upstream_file_is_contextual(snapshot: Path) -> None:
    target = snapshot / "vllm-project/vllm/docker/versions.json"
    target.unlink()
    with pytest.raises(ValueError, match="snapshot missing"):
        _selection(snapshot)


def test_malformed_vllm_recipe_fails_before_builder_matrix(snapshot: Path) -> None:
    pipeline = snapshot / "vllm-project/vllm/.buildkite/release-pipeline.yaml"
    value = pipeline.read_text(encoding="utf-8")
    pipeline.write_text(
        value.replace(
            "--build-arg BUILD_BASE_IMAGE=pytorch/manylinux2_28-builder:cuda12.9-recipe",
            "--build-arg BUILD_BASE_IMAGE=example/base:latest",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed CUDA builder image"):
        _selection(snapshot)


def test_new_variant_without_ucm_contract_fails_before_build_tasks() -> None:
    selection = _selection()
    future = copy.deepcopy(selection["upstreams"][0])
    future["family_id"] = "cann920-a5"
    future["runtime_variant"] = "a5"
    future["runtime_tag"] = "v0.22.2rc1-a5"
    future["target_tag"] = str(future["target_tag"]) + "-a5"
    for group in future["build_groups"]:
        group["id"] = str(group["id"]).replace("cann900-a2", "cann920-a5")
        group["build_group"] = "cann920-a5"
        group["backend"] = "cann-a5"
        group["variant"] = "a5"
        group["runtime_variant"] = "a5"
    selection["upstreams"].append(future)
    catalog = builders.catalog_from_selection(selection, owner="release-org")

    with pytest.raises(ValueError, match="has no UCM native contract"):
        compact.resolve_plan(
            _release(),
            builder_catalog=catalog,
            upstream_selection=selection,
            route="release",
        )


def test_missing_builder_architecture_and_abi_mismatch_fail_plan() -> None:
    selection = _selection()
    catalog = _catalog()
    catalog["builders"] = [
        item for item in catalog["builders"] if item["id"] != "cuda129-cp312-arm64"
    ]
    with pytest.raises(ValueError, match="matching Builder is missing"):
        compact.resolve_plan(
            _release(),
            builder_catalog=catalog,
            upstream_selection=selection,
            route="release",
        )

    catalog = _catalog()
    item = next(
        item for item in catalog["builders"] if item["id"] == "cuda129-cp312-arm64"
    )
    item["python_abi"] = "cp311"
    with pytest.raises(ValueError, match="Builder python_abi does not match"):
        compact.resolve_plan(
            _release(),
            builder_catalog=catalog,
            upstream_selection=selection,
            route="release",
        )


def test_cli_resolves_selection_and_builder_catalog(tmp_path: Path, capsys) -> None:
    selection_path = tmp_path / "selection.json"
    assert (
        cli.main(
            [
                "upstreams",
                "resolve",
                "--tag-fixture",
                str(TAG_FIXTURE),
                "--snapshot",
                str(FIXTURE),
                "--output",
                str(selection_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    catalog_path = tmp_path / "catalog.json"
    assert (
        cli.main(
            [
                "builders",
                "discover",
                "--selection",
                str(selection_path),
                "--owner",
                "release-org",
                "--output",
                str(catalog_path),
            ]
        )
        == 0
    )
    assert len(json.loads(catalog_path.read_text())["builders"]) == 16


def test_builder_config_contains_no_retained_capability_matrix() -> None:
    config = yaml.safe_load((RELEASE_ROOT / "builders.yaml").read_text())
    assert "retained_builders" not in config
    assert {item["product_id"] for item in config["projects"]} == {
        "vllm",
        "vllm-ascend",
    }
