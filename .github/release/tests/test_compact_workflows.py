"""User-visible GitHub Actions contract for the compact release lane."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, object]:
    value = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def test_release_workflow_has_six_visible_stages_and_flat_build_matrices() -> None:
    jobs = _load("release-ucm.yml")["jobs"]

    assert list(jobs) == [
        "sync-builders",
        "plan",
        "build-wheels",
        "package-chart",
        "build-images",
        "publish-release",
    ]
    assert jobs["build-wheels"]["name"] == "Wheel · ${{ matrix.label }}"
    assert jobs["build-images"]["name"] == "Image · ${{ matrix.label }}"
    assert jobs["build-images"]["uses"] == "./.github/workflows/_build-image.yml"
    assert set(jobs["build-images"]["needs"]) == {"plan", "build-wheels"}
    assert set(jobs["publish-release"]["needs"]) == {
        "plan",
        "build-wheels",
        "package-chart",
        "build-images",
    }


def test_builder_sync_contains_only_prepare_and_independent_missing_builds() -> None:
    workflow = _load("sync-builders.yml")
    jobs = workflow["jobs"]

    assert list(jobs) == ["prepare", "build-missing"]
    assert jobs["build-missing"]["name"] == "Builder · ${{ matrix.label }}"
    assert (
        workflow["on"]["workflow_call"]["outputs"]["builder_catalog_artifact"]["value"]
        == "${{ jobs.prepare.outputs.builder_catalog_artifact }}"
    )


def test_reusable_builds_expose_only_functional_inputs() -> None:
    expected = {
        "_build-wheel.yml": {"wheel_id", "runner", "plan_artifact", "source_ref"},
        "_build-image.yml": {
            "image_id",
            "runner",
            "plan_artifact",
            "upload_oci",
            "source_ref",
        },
        "_build-chart.yml": {"plan_artifact", "source_ref"},
    }
    for filename, inputs in expected.items():
        workflow = _load(filename)
        assert set(workflow["on"]["workflow_call"]["inputs"]) == inputs
        text = (WORKFLOWS / filename).read_text(encoding="utf-8").lower()
        assert "resolved_plan_sha256" not in text
        assert "task_sha256" not in text
        assert "source_sha" not in text


def test_compact_wheel_passes_cpu_architecture_to_the_native_build() -> None:
    workflow = _load("_build-wheel.yml")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.wheel"
    ).read_text(encoding="utf-8")
    text = yaml.safe_dump(workflow)

    assert "UCM_CPU_ARCH=$(jq -r '.cpu_arch' out/wheel-task.json)" in text
    assert "ARG UCM_CPU_ARCH" in dockerfile
    assert 'UCM_BUILD_CPU_ARCH="${UCM_CPU_ARCH}"' in dockerfile


def test_runtime_image_checks_ucm_without_auditing_the_base_environment() -> None:
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.runtime"
    ).read_text(encoding="utf-8")

    assert "python3 -c 'import ucm'" in dockerfile
    assert "pip check" not in dockerfile


def test_removed_wrapper_workflows_are_absent() -> None:
    for name in (
        "release-vllm-images.yml",
        "release-vllm-images-protected.yml",
        "_publish-image-member.yml",
    ):
        assert not (WORKFLOWS / name).exists()


def test_single_publish_job_consumes_all_channel_switches_and_finishes_release_last() -> (
    None
):
    job = _load("release-ucm.yml")["jobs"]["publish-release"]
    steps = job["steps"]
    text = yaml.safe_dump(job)

    for channel in ("pypi", "ghcr", "dockerhub", "chart_oci", "github_release"):
        assert f".publish.{channel}.enabled" in text
    assert "${target_tag}-${arch}" in text
    assert "docker buildx imagetools create" in text
    assert steps[-1]["name"] == "Upload assets and publish GitHub Release"
    assert "gh release edit" in steps[-1]["run"]


def test_ucm_build_bot_uses_compact_plan_and_functional_build_inputs() -> None:
    workflow = _load("ucm-build-bot.yml")
    text = (WORKFLOWS / "ucm-build-bot.yml").read_text(encoding="utf-8")

    assert "ucm_release compact plan" in text
    assert "resolved_plan_sha256" not in text
    assert "task_sha256" not in text
    assert set(workflow["jobs"]["build-wheels"]["with"]) == {
        "source_ref",
        "wheel_id",
        "runner",
        "plan_artifact",
    }
    assert set(workflow["jobs"]["build-images"]["with"]) == {
        "source_ref",
        "image_id",
        "runner",
        "plan_artifact",
        "upload_oci",
    }
