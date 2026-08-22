"""Functional contract for the generated compact release plan."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "builders"
TAG_FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
compact = importlib.import_module("ucm_release.compact")
core = importlib.import_module("ucm_release.core")
upstream = importlib.import_module("ucm_release.upstream")


def _inputs():
    release = core.load_catalog(version_override="0.7.59rc7")
    selection = upstream.resolve_upstreams(
        release,
        builders.load_config(),
        tag_fixture=core.load_json(TAG_FIXTURE),
        snapshot_dir=FIXTURE,
    )
    catalog = builders.catalog_from_selection(selection, owner="release-org")
    return release, selection, catalog


def _plan():
    release, selection, catalog = _inputs()
    return compact.resolve_plan(
        release,
        upstream_selection=selection,
        builder_catalog=catalog,
        route="release",
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_plan_expands_current_16_8_4_matrix_without_fixed_counts() -> None:
    plan = _plan()
    assert len(plan["wheels"]) == 16
    assert len(plan["images"]) == 8
    assert len(plan["families"]) == 4
    assert [item["id"] for item in plan["families"]] == [
        "cann900-a2",
        "cann900-a3",
        "cuda129",
        "cuda130",
    ]
    assert len({item["id"] for item in plan["wheels"]}) == 16
    assert len({item["id"] for item in plan["images"]}) == 8


def test_plan_projects_four_unique_distributions() -> None:
    plan = _plan()
    assert plan["publish"]["pypi"]["distributions"] == [
        "uc-manager-cann-a2",
        "uc-manager-cann-a3",
        "uc-manager-cuda-cu129",
        "uc-manager-cuda-cu130",
    ]
    assert {item["dist_name"] for item in plan["wheels"]} == set(
        plan["publish"]["pypi"]["distributions"]
    )


def test_image_pairing_uses_cp312_and_exact_runtime_variant() -> None:
    plan = _plan()
    wheels = {item["id"]: item for item in plan["wheels"]}
    assert all(
        wheels[item["wheel_id"]]["python_abi"] == "cp312" for item in plan["images"]
    )
    assert {item["family_id"]: item["runtime"]["tag"] for item in plan["images"]} == {
        "cann900-a2": "v0.22.2rc1",
        "cann900-a3": "v0.22.2rc1-a3",
        "cuda129": "v0.21.2-cu129",
        "cuda130": "v0.21.2",
    }
    assert all(item["id"].startswith(("cann900", "cuda")) for item in plan["images"])
    targets = {item["family_id"]: item["target_tag"] for item in plan["images"]}
    assert "-cu129-ucm-" in targets["cuda129"]
    assert "-cu130-ucm-" in targets["cuda130"]


def test_plan_keeps_top_level_contract_and_no_index_matrix_field() -> None:
    plan = _plan()
    assert set(plan) == {
        "kind",
        "route",
        "version",
        "release_tag",
        "publish",
        "chart",
        "wheels",
        "images",
        "families",
        "wheel_matrix",
        "image_matrix",
    }
    assert "image_index_matrix" not in plan


def test_plan_contains_no_audit_or_digest_fields() -> None:
    plan = _plan()
    keys = _all_keys(plan)
    assert not {
        key
        for key in keys
        if "sha" in key
        or "digest" in key
        or "authority" in key
        or "evidence" in key
        or "seal" in key
    }
    assert "@sha256:" not in json.dumps(plan)


def test_group_filter_selects_all_ascend_abis_and_architectures() -> None:
    plan = _plan()
    selected = [item for item in plan["wheels"] if item["profile_id"] == "cann900-a2"]
    assert len(selected) == 6
    assert {item["python_abi"] for item in selected} == {"cp310", "cp311", "cp312"}
    assert {item["cpu_arch"] for item in selected} == {"amd64", "arm64"}


def test_exact_pinned_family_filter_is_supported() -> None:
    release, selection, catalog = _inputs()
    plan = compact.resolve_plan(
        release,
        upstream_selection=selection,
        builder_catalog=catalog,
        route="pr",
        pinned_upstreams=["quay.io/ascend/vllm-ascend:v0.22.2rc1-a3"],
    )
    assert [item["id"] for item in plan["families"]] == ["cann900-a3"]
    assert len(plan["wheels"]) == 6
    assert len(plan["images"]) == 2


def test_wheel_result_manifest_uses_actual_filename_tags(tmp_path: Path) -> None:
    task = next(
        item for item in _plan()["wheels"] if item["id"] == "cuda130-cp312-amd64"
    )
    wheel = (
        tmp_path
        / "uc_manager_cuda_cu130-0.7.59rc7-cp312-cp312-manylinux_2_28_x86_64.whl"
    )
    wheel.write_bytes(b"fixture")
    result = compact.record_wheel_result(task, wheel)
    assert result == {
        "kind": "ucm-wheel-result",
        "schema_version": 1,
        "task_id": "cuda130-cp312-amd64",
        "distribution": "uc-manager-cuda-cu130",
        "version": "0.7.59rc7",
        "python_abi": "cp312",
        "cpu_arch": "amd64",
        "filename": wheel.name,
        "platform_tags": ["manylinux_2_28_x86_64"],
    }


@pytest.mark.parametrize(
    "filename",
    [
        "wrong-0.7.59rc7-cp312-cp312-linux_x86_64.whl",
        "uc_manager_cuda_cu130-0.7.59rc7-cp311-cp311-linux_x86_64.whl",
        "uc_manager_cuda_cu130-0.7.59rc7-cp312-cp312-linux_aarch64.whl",
    ],
)
def test_wheel_result_rejects_distribution_abi_and_arch_drift(
    tmp_path: Path, filename: str
) -> None:
    task = next(
        item for item in _plan()["wheels"] if item["id"] == "cuda130-cp312-amd64"
    )
    wheel = tmp_path / filename
    wheel.write_bytes(b"fixture")
    with pytest.raises(ValueError):
        compact.record_wheel_result(task, wheel)
