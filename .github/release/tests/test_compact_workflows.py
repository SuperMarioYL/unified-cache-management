"""User-visible GitHub Actions contract for the compact release lane."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    value = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _job_source(job: dict[str, Any]) -> str:
    return yaml.safe_dump(job, sort_keys=False)


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    return {value} if isinstance(value, str) else set(value)


def _artifact_steps(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(f"actions/{action}-artifact@")
    ]


def _is_always(value: object) -> bool:
    return str(value).replace("${{", "").replace("}}", "").strip() == "always()"


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
    assert jobs["build-images"]["with"]["upload_oci"] == (
        "${{ needs.plan.outputs.route == 'release' || "
        "inputs.deliver_full_oci == true }}"
    )


def test_builder_sync_exports_run_scoped_capability_catalog_from_assembly() -> None:
    workflow = _load("sync-builders.yml")
    jobs = workflow["jobs"]
    outputs = workflow["on"]["workflow_call"]["outputs"]

    assert "prepare" in jobs
    assert "build-missing" in jobs
    assert jobs["build-missing"]["name"] == "Builder · ${{ matrix.label }}"
    assert "capability_catalog_artifact" in outputs, (
        "sync-builders must export the assembled Capability Catalog artifact"
    )
    assert (
        outputs["capability_catalog_artifact"]["value"]
        == "${{ jobs.assemble-capability-catalog.outputs.capability_catalog_artifact }}"
    )
    assembly = jobs.get("assemble-capability-catalog")
    assert isinstance(assembly, dict)
    source = _job_source(assembly)
    assert (
        "ucm-capability-catalog-run-${GITHUB_RUN_ID}-attempt-"
        "${GITHUB_RUN_ATTEMPT}" in source
    )
    uploads = _artifact_steps(assembly, "upload")
    assert any(
        "capability_catalog_artifact"
        in str(step.get("with", {}).get("name", ""))
        and "capability-catalog.json" in str(step.get("with", {}).get("path", ""))
        for step in uploads
    )


def test_python_probe_matrix_enumerates_all_abis_on_native_builder_runners() -> None:
    workflow = _load("sync-builders.yml")
    candidates = [
        (job_id, job)
        for job_id, job in workflow["jobs"].items()
        if "/opt/python/cp*-cp*/bin/python" in _job_source(job)
    ]

    assert candidates, "missing native Builder Python probe matrix"
    _, job = candidates[0]
    source = _job_source(job)
    assert job.get("strategy", {}).get("fail-fast") is False
    assert "matrix" in job.get("strategy", {})
    runs_on = str(job.get("runs-on", ""))
    assert any(
        selector in runs_on
        for selector in ("matrix.runner", "matrix.cpu_architecture", "matrix.cpu_arch")
    )
    assert "docker run" in source
    assert "builder_digest" in source
    assert "python_version" in source
    assert "python_abi" in source
    assert "wheel_tag" in source
    assert "cpu_architecture" in source
    assert "cp312" not in source


def test_runtime_discovery_records_immutable_image_and_git_source_facts() -> None:
    workflow = _load("sync-builders.yml")
    required = {
        "runtime_image_digest",
        "git_tag",
        "git_commit",
        "variant",
        "cpu_architecture",
    }
    candidates = [
        job
        for job in workflow["jobs"].values()
        if required <= set(re.findall(r"[a-z][a-z0-9_-]*", _job_source(job).lower()))
    ]

    assert candidates, "missing runtime image and Git-source discovery job"
    source = _job_source(candidates[0])
    assert "@sha256:" in source or "runtime_image_digest" in source
    assert "runtime-discovery.json" in source


def test_mooncake_probe_compares_runtime_dockerfile_tag_with_installed_version() -> (
    None
):
    workflow = _load("sync-builders.yml")
    candidates = [
        job
        for job in workflow["jobs"].values()
        if "MOONCAKE_TAG" in _job_source(job)
        and "installed_version" in _job_source(job)
    ]

    assert candidates, "missing native Mooncake declaration/installation probe"
    source = _job_source(candidates[0])
    assert "Dockerfile" in source
    assert "runtime_image_digest" in source
    runs_on = str(candidates[0].get("runs-on", ""))
    assert any(
        selector in runs_on
        for selector in ("matrix.runner", "matrix.cpu_architecture", "matrix.cpu_arch")
    )
    assert "mooncake-probe.json" in source


def test_catalog_assembly_waits_for_all_results_and_calls_public_seam() -> None:
    workflow = _load("sync-builders.yml")
    assembly = workflow["jobs"].get("assemble-capability-catalog")

    assert isinstance(assembly, dict), "missing Capability Catalog assembly job"
    source = _job_source(assembly)
    needs = _needs(assembly)
    assert "build-missing" in needs
    assert len(needs) >= 4
    assert _is_always(assembly.get("if"))
    downloads = _artifact_steps(assembly, "download")
    downloaded = "\n".join(_job_source(step).lower() for step in downloads)
    for result in ("builder", "python", "runtime", "mooncake"):
        assert result in downloaded
    assert "assemble_capability_catalog" in source
    assert "validate_capability_catalog" in source
    uploads = _artifact_steps(assembly, "upload")
    assert uploads
    assert all(_is_always(step.get("if")) for step in uploads)


@pytest.mark.parametrize("filename", ["release-ucm.yml", "ucm-build-bot.yml"])
def test_planners_consume_capability_catalog_instead_of_flat_builder_catalog(
    filename: str,
) -> None:
    workflow = _load(filename)
    sync_outputs = workflow["jobs"]["sync-builders"].get("outputs", {})
    plan = workflow["jobs"]["plan"]
    source = _job_source(plan)

    assert "capability_catalog_artifact" in sync_outputs or (
        "needs.sync-builders.outputs.capability_catalog_artifact" in source
    )
    assert "--capability-catalog" in source
    assert "capability-catalog.json" in source
    assert "--builder-catalog" not in source


def test_ascend_builder_copies_mooncake_from_matching_immutable_runtime() -> None:
    workflow = (WORKFLOWS / "sync-builders.yml").read_text(encoding="utf-8")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")

    assert "matrix.runtime_image" in workflow
    runtime_stage = re.search(
        r"^ARG\s+(?P<arg>[A-Z_]*RUNTIME_IMAGE)\s*$\n"
        r"FROM\s+\$\{(?P=arg)\}\s+AS\s+(?P<stage>[-a-z0-9]+)$",
        dockerfile,
        re.MULTILINE,
    )
    assert runtime_stage
    assert runtime_stage.group("arg") in workflow
    stage = runtime_stage.group("stage")
    assert re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+.*include", dockerfile, re.MULTILINE
    )
    assert re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+.*lib", dockerfile, re.MULTILINE
    )


def test_ascend_builder_has_no_tag_inference_or_fixed_mooncake_clone() -> None:
    workflow = (WORKFLOWS / "sync-builders.yml").read_text(encoding="utf-8")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")
    active_source = workflow + "\n" + dockerfile

    assert "mooncake_installer.sh" not in active_source
    assert "MOONCAKE_TAG" not in active_source
    assert "git clone" not in active_source
    assert "mooncake_version=\"$(printf '%s' \"${TARGET_TAG}\"" not in active_source
    assert "0.3.9" not in active_source


def test_probe_matrices_isolate_failures_and_always_upload_results() -> None:
    workflow = _load("sync-builders.yml")
    probe_jobs = [
        job
        for job_id, job in workflow["jobs"].items()
        if job_id != "build-missing" and "matrix" in job.get("strategy", {})
    ]

    assert len(probe_jobs) >= 2, "missing dynamic native probe matrices"
    for job in probe_jobs:
        assert job["strategy"].get("fail-fast") is False
        uploads = _artifact_steps(job, "upload")
        assert uploads
        assert all(_is_always(step.get("if")) for step in uploads)


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
