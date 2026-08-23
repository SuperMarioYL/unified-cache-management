"""Functional contract for the shared compact Release/PR plan."""

from __future__ import annotations

import copy
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
policy = importlib.import_module("ucm_release.policy")
upstream = importlib.import_module("ucm_release.upstream")


def _inputs():
    formal = policy.resolve(
        repository="release-org/unified-cache-management",
        version_override="0.7.60rc1",
    )
    selection = upstream.resolve_upstreams(
        formal,
        tag_fixture=core.load_json(TAG_FIXTURE),
        snapshot_dir=FIXTURE,
    )
    catalog = builders.catalog_from_selection(
        selection, owner="release-org", formal_policy=formal
    )
    return formal, selection, catalog


def _plan():
    formal, selection, catalog = _inputs()
    return compact.resolve_plan(
        formal,
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


def test_blocked_a5_projects_16_wheels_16_members_and_8_indexes() -> None:
    plan = _plan()

    assert (len(plan["wheels"]), len(plan["images"]), len(plan["families"])) == (
        16,
        16,
        8,
    )
    assert all(item["create_index"] for item in plan["families"])
    assert all(len(item["members"]) == 2 for item in plan["families"])
    assert not any("a5" in item["id"] for item in plan["wheels"])
    assert not any("a5" in item["id"] for item in plan["families"])


def test_supported_a5_projects_22_wheels_20_members_and_10_indexes() -> None:
    formal, selection, catalog = _inputs()
    formal = copy.deepcopy(formal)
    formal["backends"]["cann-a5"] = {
        "status": "supported",
        "platform": "ascend-a5",
        "distribution": "uc-manager-cann-a5",
    }

    plan = compact.resolve_plan(
        formal,
        upstream_selection=selection,
        builder_catalog=catalog,
        route="release",
    )

    assert (len(plan["wheels"]), len(plan["images"]), len(plan["families"])) == (
        22,
        20,
        10,
    )


def test_os_runtime_families_reuse_explicit_cp312_wheel_ids() -> None:
    plan = _plan()
    images = {item["id"]: item for item in plan["images"]}

    assert (
        images["cu129-ubuntu2204-amd64"]["wheel_id"]
        == images["cu129-ubuntu2404-amd64"]["wheel_id"]
    )
    assert (
        images["cann910-a3-ubuntu2204-arm64"]["wheel_id"]
        == images["cann910-a3-openeuler2403-arm64"]["wheel_id"]
    )
    assert all(item["runtime"]["python_abi"] == "cp312" for item in plan["images"])
    assert {item["runtime"]["os_id"] for item in plan["images"]} == {
        "ubuntu",
        "openeuler",
    }


def test_plan_projects_four_distributions_from_platform_policy() -> None:
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


def test_plan_keeps_top_level_contract_without_problem_or_index_matrix() -> None:
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
    assert "problems" not in plan
    keys = _all_keys(plan)
    assert not {key for key in keys if "digest" in key or "authority" in key}
    assert "@sha256:" not in json.dumps(plan)


def test_family_members_are_the_authority_for_index_inputs() -> None:
    plan = _plan()
    family = next(item for item in plan["families"] if item["id"] == "cu130-ubuntu2404")

    assert family["members"] == [
        {
            "image_id": "cu130-ubuntu2404-amd64",
            "cpu_arch": "amd64",
            "reference": (
                "ghcr.io/release-org/vllm-openai:"
                "v0.27.1-ubuntu2404-ucm-0.7.60rc1-r1-amd64"
            ),
        },
        {
            "image_id": "cu130-ubuntu2404-arm64",
            "cpu_arch": "arm64",
            "reference": (
                "ghcr.io/release-org/vllm-openai:"
                "v0.27.1-ubuntu2404-ucm-0.7.60rc1-r1-arm64"
            ),
        },
    ]


def test_formal_all_plan_can_be_retagged_for_pr_publication() -> None:
    formal, selection, catalog = _inputs()
    plan = compact.resolve_plan(
        formal,
        upstream_selection=selection,
        builder_catalog=catalog,
        route="pr",
    )
    retagged = compact.retag_pr_plan(
        plan, pr_number=12, author="Release-Author", run_id=345
    )

    assert all(
        family["target_tag"].startswith("pr-12-release-author-run-345-")
        for family in retagged["families"]
    )
    assert all(
        image["target_tag"]
        == next(
            family["target_tag"]
            for family in retagged["families"]
            if family["id"] == image["family_id"]
        )
        for image in retagged["images"]
    )


def test_single_arch_pr_build_has_one_member_and_no_index() -> None:
    formal, selection, catalog = _inputs()
    selection = copy.deepcopy(selection)
    runtime = next(
        item for item in selection["runtimes"] if item["id"] == "cu130-ubuntu2404"
    )
    runtime["architectures"] = ["amd64"]
    runtime["member_references"] = {"amd64": runtime["member_references"]["amd64"]}
    runtime["wheel_build_ids"] = {"amd64": runtime["wheel_build_ids"]["amd64"]}
    runtime["channel"] = "pinned"
    selection["runtimes"] = [runtime]

    plan = compact.resolve_plan(
        formal,
        upstream_selection=selection,
        builder_catalog=catalog,
        route="pr",
    )

    assert (len(plan["wheels"]), len(plan["images"]), len(plan["families"])) == (
        1,
        1,
        1,
    )
    family = plan["families"][0]
    assert family["create_index"] is False
    assert family["published_reference"].endswith("-amd64")
    assert family["members"] == [
        {
            "image_id": "cu130-ubuntu2404-amd64",
            "cpu_arch": "amd64",
            "reference": family["published_reference"],
        }
    ]


def test_multiple_pr_runtime_tags_with_same_capability_reuse_wheels() -> None:
    formal, selection, catalog = _inputs()
    selection = copy.deepcopy(selection)
    selected = [
        item
        for item in selection["runtimes"]
        if item["id"] in {"cu130-ubuntu2204", "cu130-ubuntu2404"}
    ]
    for runtime in selected:
        runtime["channel"] = "pinned"
    selection["runtimes"] = selected

    plan = compact.resolve_plan(
        formal,
        upstream_selection=selection,
        builder_catalog=catalog,
        route="pr",
    )

    assert len(plan["wheels"]) == 2
    assert len(plan["images"]) == 4
    assert len(plan["families"]) == 2


def test_wheel_result_manifest_uses_actual_filename_tags(tmp_path: Path) -> None:
    task = next(
        item for item in _plan()["wheels"] if item["id"] == "cuda130-cp312-amd64"
    )
    wheel = (
        tmp_path
        / "uc_manager_cuda_cu130-0.7.60rc1-cp312-cp312-manylinux_2_28_x86_64.whl"
    )
    wheel.write_bytes(b"fixture")
    result = compact.record_wheel_result(task, wheel)
    assert result["task_id"] == "cuda130-cp312-amd64"
    assert result["filename"] == wheel.name
    assert result["platform_tags"] == ["manylinux_2_28_x86_64"]


@pytest.mark.parametrize(
    "filename",
    [
        "wrong-0.7.60rc1-cp312-cp312-linux_x86_64.whl",
        "uc_manager_cuda_cu130-0.7.60rc1-cp311-cp311-linux_x86_64.whl",
        "uc_manager_cuda_cu130-0.7.60rc1-cp312-cp312-linux_aarch64.whl",
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
