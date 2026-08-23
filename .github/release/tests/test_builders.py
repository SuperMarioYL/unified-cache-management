"""Formal upstream selection and functional Builder identity contracts."""

from __future__ import annotations

import copy
import importlib
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
core = importlib.import_module("ucm_release.core")
policy = importlib.import_module("ucm_release.policy")
upstream = importlib.import_module("ucm_release.upstream")


def _policy() -> dict[str, object]:
    return policy.resolve(
        repository="release-org/unified-cache-management",
        version_override="0.7.60rc1",
    )


def _fixture() -> dict[str, object]:
    return core.load_json(TAG_FIXTURE)


def _selection(
    snapshot: Path = FIXTURE, tag_fixture: dict[str, object] | None = None
) -> dict[str, object]:
    return upstream.resolve_upstreams(
        _policy(),
        tag_fixture=tag_fixture or _fixture(),
        snapshot_dir=snapshot,
    )


def _catalog(selection: dict[str, object] | None = None) -> dict[str, object]:
    return builders.catalog_from_selection(
        selection or _selection(), owner="release-org", formal_policy=_policy()
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    destination = tmp_path / "builders"
    shutil.copytree(FIXTURE, destination)
    return destination


def test_formal_selection_separates_22_wheel_builds_10_runtimes_and_problems() -> None:
    selection = _selection()

    assert len(selection["wheel_builds"]) == 22
    assert len(selection["runtimes"]) == 10
    assert len(selection["problems"]) == 2
    assert {item["backend"] for item in selection["problems"]} == {"cann-a5"}
    assert {item["variant"] for item in selection["wheel_builds"]} == {
        "default",
        "a2",
        "a3",
        "a5",
    }
    assert "310p" not in {item["variant"] for item in selection["wheel_builds"]}


def test_stable_is_preferred_over_newer_rc_without_minor_ceiling() -> None:
    selection = _selection()
    sources = {
        item["product_id"]: (item["source_ref"], item["channel"])
        for item in selection["runtimes"]
    }

    assert sources == {
        "vllm": ("v0.27.1", "stable"),
        "vllm-ascend": ("v0.23.0", "stable"),
    }

    fixture = _fixture()
    vllm_tags = fixture["repositories"]["docker.io/vllm/vllm-openai"]["pages"][0][
        "tags"
    ]
    fixture["repositories"]["docker.io/vllm/vllm-openai"]["pages"][0]["tags"] = [
        tag for tag in vllm_tags if tag not in {"v0.21.0", "v0.27.1"}
    ]
    fixture["runtime_architectures"]["docker.io/vllm/vllm-openai:v0.28.0rc2"] = [
        "amd64",
        "arm64",
    ]
    for suffix in ("-cu129", "-ubuntu2404", "-cu129-ubuntu2404"):
        fixture["repositories"]["docker.io/vllm/vllm-openai"]["pages"][0][
            "tags"
        ].append("v0.28.0rc2" + suffix)
        fixture["runtime_architectures"][
            "docker.io/vllm/vllm-openai:v0.28.0rc2" + suffix
        ] = ["amd64", "arm64"]
    fallback = _selection(tag_fixture=fixture)
    vllm = next(item for item in fallback["runtimes"] if item["product_id"] == "vllm")
    assert (vllm["source_ref"], vllm["channel"]) == ("v0.28.0rc2", "rc")


def test_runtime_matrix_keeps_all_os_variants_and_exact_wheel_links() -> None:
    selection = _selection()
    runtimes = {item["id"]: item for item in selection["runtimes"]}

    assert set(runtimes) == {
        "cu129-ubuntu2204",
        "cu129-ubuntu2404",
        "cu130-ubuntu2204",
        "cu130-ubuntu2404",
        "cann910-a2-ubuntu2204",
        "cann910-a2-openeuler2403",
        "cann910-a3-ubuntu2204",
        "cann910-a3-openeuler2403",
        "cann910-a5-ubuntu2204",
        "cann910-a5-openeuler2403",
    }
    assert all(
        item["architectures"] == ["amd64", "arm64"] for item in runtimes.values()
    )
    assert (
        runtimes["cu129-ubuntu2204"]["wheel_build_ids"]
        == runtimes["cu129-ubuntu2404"]["wheel_build_ids"]
    )
    assert (
        runtimes["cann910-a3-ubuntu2204"]["wheel_build_ids"]
        == runtimes["cann910-a3-openeuler2403"]["wheel_build_ids"]
    )


def test_mooncake_and_builder_revision_come_from_selected_source() -> None:
    selection = _selection()
    ascend = [
        item for item in selection["wheel_builds"] if item["accelerator"] == "ascend"
    ]
    assert {item["mooncake_version"] for item in ascend} == {"0.3.11.post1"}
    assert all(len(item["recipe_revision"]) == 12 for item in selection["wheel_builds"])

    catalog = _catalog(selection)
    assert len(catalog["builders"]) == 22
    assert all(
        item["target_tag"].endswith("-r" + item["recipe_revision"])
        for item in catalog["builders"]
    )
    assert all("v0.23.0" not in item["target_tag"] for item in catalog["builders"])


def test_recipe_revision_changes_with_materialized_recipe(snapshot: Path) -> None:
    baseline = _selection(snapshot)
    path = (
        snapshot
        / "vllm-project/vllm-ascend/.github/workflows/dockerfiles/Dockerfile.buildwheel.a2"
    )
    path.write_text(path.read_text(encoding="utf-8") + "\n# functional revision\n")
    changed = _selection(snapshot)

    baseline_revisions = {
        item["id"]: item["recipe_revision"] for item in baseline["wheel_builds"]
    }
    changed_revisions = {
        item["id"]: item["recipe_revision"] for item in changed["wheel_builds"]
    }
    assert (
        changed_revisions["cann910-a2-cp312-amd64"]
        != baseline_revisions["cann910-a2-cp312-amd64"]
    )
    assert (
        changed_revisions["cann910-a3-cp312-amd64"]
        == baseline_revisions["cann910-a3-cp312-amd64"]
    )


def test_recipe_revision_changes_with_source_image_content() -> None:
    fixture = _fixture()
    baseline = _selection(tag_fixture=copy.deepcopy(fixture))
    image = "docker.io/pytorch/manylinux2_28-builder:cuda13.0-recipe"
    fixture["source_image_digests"][image] = "sha256:" + "e" * 64
    changed = _selection(tag_fixture=fixture)
    before = {item["id"]: item for item in baseline["wheel_builds"]}
    after = {item["id"]: item for item in changed["wheel_builds"]}

    assert (
        after["cuda130-cp312-amd64"]["recipe_revision"]
        != before["cuda130-cp312-amd64"]["recipe_revision"]
    )
    assert (
        after["cuda130-cp312-arm64"]["recipe_revision"]
        == before["cuda130-cp312-arm64"]["recipe_revision"]
    )


def test_recipe_revision_changes_when_a_source_tag_moves_commit() -> None:
    first = upstream.resolve_upstreams(
        _policy(),
        tag_fixture=_fixture(),
        snapshot_dir=FIXTURE,
        source_commit_resolver=lambda _repository, _ref: "a" * 40,
    )
    second = upstream.resolve_upstreams(
        _policy(),
        tag_fixture=_fixture(),
        snapshot_dir=FIXTURE,
        source_commit_resolver=lambda _repository, _ref: "b" * 40,
    )
    first_revisions = {
        item["id"]: item["recipe_revision"] for item in first["wheel_builds"]
    }
    second_revisions = {
        item["id"]: item["recipe_revision"] for item in second["wheel_builds"]
    }
    assert all(
        first_revisions[item] != second_revisions[item] for item in first_revisions
    )


def test_sync_is_append_only_and_checks_all_22_targets() -> None:
    catalog = _catalog()
    first = catalog["builders"][0]
    existing = {first["target_repository"]: [first["target_tag"]]}
    existing["ghcr.io/release-org/retired"] = ["keep-me"]

    sync = builders.compute_sync_plan(catalog, existing)

    assert len(sync["builders"]) == 21
    assert "deletions" not in sync
    assert all(item["checks"]["commands"] for item in sync["builders"])
    assert {
        item["backend"]: item["checks"]["blocking"] for item in catalog["builders"]
    }["cann-a5"] is False


def test_builder_registry_inventory_accepts_only_promoted_final_tags() -> None:
    formal = _policy()
    builder = _catalog()["builders"][0]
    labels = builders.builder_labels(builder)
    final_tag = builder["target_tag"]
    candidate_tag = final_tag + "-candidate-10"

    def load_tags(repository):
        return (
            ["legacy-r1", candidate_tag, final_tag]
            if repository == builder["target_repository"]
            else []
        )

    def load_config(reference):
        return {
            "created": "2026-08-23T12:00:00Z",
            "config": {"Labels": labels},
        }

    inventory = builders.scan_registry_builders(
        formal, tag_loader=load_tags, config_loader=load_config
    )

    assert len(inventory["builders"]) == 1
    record = inventory["builders"][0]
    assert record["id"] == builder["id"]
    assert record["target_tag"] == final_tag
    assert record["checked"] is True
    reopened = builders.catalog_from_registry_records([record])
    assert reopened["builders"][0]["target_tag"] == final_tag


def test_supported_runtime_requires_complete_formal_architectures() -> None:
    fixture = _fixture()
    reference = "docker.io/vllm/vllm-openai:v0.27.1-cu129"
    fixture["runtime_architectures"][reference] = ["amd64"]

    with pytest.raises(ValueError, match="formal runtime architectures"):
        _selection(tag_fixture=fixture)


def test_pinned_tags_are_owned_by_runtime_inspection_not_formal_resolver() -> None:
    with pytest.raises(ValueError, match="runtime inspection"):
        upstream.resolve_upstreams(
            _policy(),
            tag_fixture=_fixture(),
            snapshot_dir=FIXTURE,
            pinned_upstreams=["docker.io/vllm/vllm-openai:nightly-deadbeef"],
        )


def test_new_ascend_variant_is_synced_and_isolated_without_native_policy(
    snapshot: Path,
) -> None:
    repository = snapshot / "vllm-project/vllm-ascend"
    wheel_dockerfile = (
        repository / ".github/workflows/dockerfiles/Dockerfile.buildwheel.a4"
    )
    wheel_dockerfile.write_text(
        "ARG PY_VERSION=3.12\n"
        "FROM quay.io/ascend/manylinux:9.1.0-960-manylinux_2_34-py${PY_VERSION}\n"
        'ARG SOC_VERSION="ascend960_test"\n'
        "RUN python3 -m pip install -r requirements.txt\n"
        "RUN python3 setup.py bdist_wheel\n",
        encoding="utf-8",
    )
    wheel_workflow_path = (
        repository / ".github/workflows/schedule_release_code_and_wheel.yml"
    )
    wheel_workflow = yaml.safe_load(wheel_workflow_path.read_text(encoding="utf-8"))
    wheel_workflow["jobs"]["build_and_release_wheel_a4"] = {
        "strategy": {
            "matrix": {
                "os": ["ubuntu-24.04", "ubuntu-24.04-arm"],
                "python-version": ["3.10", "3.11", "3.12"],
            }
        },
        "steps": [
            {
                "run": (
                    "docker build -f .github/workflows/dockerfiles/"
                    "Dockerfile.buildwheel.a4 --build-arg PY_VERSION=${{ "
                    "matrix.python-version }} ."
                )
            }
        ],
    }
    wheel_workflow_path.write_text(
        yaml.safe_dump(wheel_workflow, sort_keys=False), encoding="utf-8"
    )
    image_workflow_path = (
        repository / ".github/workflows/schedule_image_build_and_push.yaml"
    )
    image_workflow = yaml.safe_load(image_workflow_path.read_text(encoding="utf-8"))
    image_workflow["jobs"]["image_build"]["strategy"]["matrix"]["include"].append(
        {"build_meta": {"dockerfile": "Dockerfile.a4", "suffix": "a4"}}
    )
    image_workflow_path.write_text(
        yaml.safe_dump(image_workflow, sort_keys=False), encoding="utf-8"
    )
    (repository / "Dockerfile.a4").write_text(
        "FROM quay.io/ascend/cann:9.1.0-960-ubuntu22.04-py3.12\n"
        "ARG MOONCAKE_TAG=0.3.11.post1\n"
        'ARG SOC_VERSION="ascend960_test"\n',
        encoding="utf-8",
    )
    fixture = _fixture()
    fixture["repositories"]["quay.io/ascend/vllm-ascend"]["pages"][0]["tags"].append(
        "v0.23.0-a4"
    )
    fixture["runtime_architectures"]["quay.io/ascend/vllm-ascend:v0.23.0-a4"] = [
        "amd64",
        "arm64",
    ]
    for index, python_version in enumerate(("3.10", "3.11", "3.12"), start=1):
        fixture["source_image_digests"][
            "quay.io/ascend/manylinux:9.1.0-960-manylinux_2_34-py" + python_version
        ] = ("sha256:" + str(index) * 64)

    selection = _selection(snapshot, fixture)
    a4_builds = [item for item in selection["wheel_builds"] if item["variant"] == "a4"]
    assert len(a4_builds) == 6
    assert any(problem["backend"] == "cann-a4" for problem in selection["problems"])
    catalog = _catalog(selection)
    assert all(
        item["checks"]["blocking"] is False
        for item in catalog["builders"]
        if item["backend"] == "cann-a4"
    )
