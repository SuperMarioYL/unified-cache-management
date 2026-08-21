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


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    return {value} if isinstance(value, str) else set(value)


def _artifact_steps(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(f"actions/{action}-artifact@")
    ]


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job.get("steps", []) if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {name!r} step"
    return matches[0]


def _noncomment_dockerfile(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


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

    assert {
        "prepare",
        "build-missing",
        "probe-python",
        "discover-runtimes",
        "probe-mooncake",
        "assemble-capability-catalog",
    } <= set(jobs)
    assert jobs["build-missing"]["name"] == "Builder · ${{ matrix.label }}"
    assert "capability_catalog_artifact" in outputs
    prepare = jobs["prepare"]
    assert {
        "builder_catalog_artifact",
        "has_missing",
        "matrix",
        "python_probe_matrix",
    } <= set(prepare.get("outputs", {}))
    assert prepare["outputs"]["python_probe_matrix"] == (
        "${{ steps.plan.outputs.python_probe_matrix }}"
    )
    plan_steps = [step for step in prepare["steps"] if step.get("id") == "plan"]
    assert len(plan_steps) == 1
    plan = plan_steps[0]
    plan_run = str(plan["run"])
    assert ".builder_revisions[]" in plan_run
    for field in (
        "builder_revision_id",
        "builder_image",
        "runner",
        "cpu_architecture",
    ):
        assert field in plan_run
    assert "@sha256:" in plan_run
    assert "python_probe_matrix=" in plan_run
    assert "${GITHUB_OUTPUT}" in plan_run
    assert {"prepare"} <= _needs(jobs["build-missing"])
    assert (
        outputs["capability_catalog_artifact"]["value"]
        == "${{ jobs.assemble-capability-catalog.outputs.capability_catalog_artifact }}"
    )
    assembly_outputs = jobs["assemble-capability-catalog"].get("outputs", {})
    assert "capability_catalog_artifact" in assembly_outputs
    assert assembly_outputs["capability_catalog_artifact"] == (
        "${{ steps.catalog.outputs.capability_catalog_artifact }}"
    )


def test_python_probe_matrix_enumerates_all_abis_on_native_builder_runners() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("probe-python")

    assert isinstance(job, dict), "missing stable probe-python job"
    assert {"prepare", "build-missing"} <= _needs(job)
    assert job["strategy"].get("fail-fast") is False
    assert job["strategy"].get("matrix") == (
        "${{ fromJSON(needs.prepare.outputs.python_probe_matrix) }}"
    )
    assert job["runs-on"] == "${{ matrix.runner }}"
    probe_steps = [
        step
        for step in job["steps"]
        if "/opt/python/cp*-cp*/bin/python" in str(step.get("run", ""))
    ]
    assert len(probe_steps) == 1
    probe = probe_steps[0]
    assert probe.get("env", {}).get("BUILDER_IMAGE") == "${{ matrix.builder_image }}"
    assert probe.get("env", {}).get("BUILDER_REVISION_ID") == (
        "${{ matrix.builder_revision_id }}"
    )
    run = str(probe["run"])
    assert any(
        "BUILDER_IMAGE" in line and "@sha256:" in line for line in run.splitlines()
    )
    assert re.search(r'docker run[\s\S]*"\$\{BUILDER_IMAGE\}"', run)
    assert "out/python-probe/result.json" in run
    assert "builder_revision_id" in run
    for field in (
        "builder_digest",
        "python_version",
        "python_abi",
        "wheel_tag",
        "cpu_architecture",
    ):
        assert field in run
    assert "cp312" not in run
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": (
            "ucm-python-probe-${{ matrix.id }}-run-${{ github.run_id }}-"
            "attempt-${{ github.run_attempt }}"
        ),
        "path": "out/python-probe/result.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_runtime_discovery_records_immutable_image_and_git_source_facts() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("discover-runtimes")

    assert isinstance(job, dict), "missing stable discover-runtimes job"
    assert {"prepare"} <= _needs(job)
    assert {
        "runtime_discovery_artifact",
        "runtime_probe_matrix",
    } <= set(job.get("outputs", {}))
    discover_steps = [
        step
        for step in job["steps"]
        if "out/runtime-discovery.json" in str(step.get("run", ""))
    ]
    assert len(discover_steps) == 1
    run = str(discover_steps[0]["run"])
    for project in ("vllm", "vllm-ascend"):
        assert project in run
    for field in (
        "runtime_image",
        "runtime_image_digest",
        "runtime_dockerfile",
        "git_tag",
        "git_commit",
        "variant",
        "cpu_architecture",
        "runner",
    ):
        assert field in run
    assert (
        "ucm-runtime-discovery-run-${GITHUB_RUN_ID}-attempt-"
        "${GITHUB_RUN_ATTEMPT}" in run
    )
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": job["outputs"]["runtime_discovery_artifact"],
        "path": "out/runtime-discovery.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_mooncake_probe_compares_runtime_dockerfile_tag_with_installed_version() -> (
    None
):
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("probe-mooncake")

    assert isinstance(job, dict), "missing stable probe-mooncake job"
    assert {"discover-runtimes"} <= _needs(job)
    assert job["strategy"].get("fail-fast") is False
    assert job["strategy"].get("matrix") == (
        "${{ fromJSON(needs.discover-runtimes.outputs.runtime_probe_matrix) }}"
    )
    assert job["runs-on"] == "${{ matrix.runner }}"
    probe_steps = [
        step
        for step in job["steps"]
        if "out/mooncake-probe/result.json" in str(step.get("run", ""))
    ]
    assert len(probe_steps) == 1
    probe = probe_steps[0]
    assert probe.get("env", {}).get("RUNTIME_ID") == "${{ matrix.runtime_id }}"
    assert probe.get("env", {}).get("RUNTIME_IMAGE") == "${{ matrix.runtime_image }}"
    assert probe.get("env", {}).get("RUNTIME_IMAGE_DIGEST") == (
        "${{ matrix.runtime_image_digest }}"
    )
    assert probe.get("env", {}).get("RUNTIME_DOCKERFILE") == (
        "${{ matrix.runtime_dockerfile }}"
    )
    assert probe.get("env", {}).get("CPU_ARCHITECTURE") == (
        "${{ matrix.cpu_architecture }}"
    )
    run = str(probe["run"])
    assert "MOONCAKE_TAG" in run
    assert "${RUNTIME_DOCKERFILE}" in run
    assert "${RUNTIME_IMAGE}" in run
    assert any(
        "RUNTIME_IMAGE" in line and "@sha256:" in line for line in run.splitlines()
    )
    assert "declared_version" in run
    assert "installed_version" in run
    assert any(
        "declared_version" in line
        and "installed_version" in line
        and "!=" in line
        for line in run.splitlines()
    )
    assert "reason_code" in run
    assert "mooncake-version-mismatch" in run
    for field in (
        "runtime_id",
        "runtime_image",
        "runtime_image_digest",
        "runtime_dockerfile",
        "cpu_architecture",
    ):
        assert field in run
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": (
            "ucm-mooncake-probe-${{ matrix.id }}-run-${{ github.run_id }}-"
            "attempt-${{ github.run_attempt }}"
        ),
        "path": "out/mooncake-probe/result.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_catalog_assembly_waits_for_all_results_and_calls_public_seam() -> None:
    workflow = _load("sync-builders.yml")
    assembly = workflow["jobs"].get("assemble-capability-catalog")

    assert isinstance(assembly, dict), "missing Capability Catalog assembly job"
    assert {
        "prepare",
        "build-missing",
        "probe-python",
        "discover-runtimes",
        "probe-mooncake",
    } <= _needs(assembly)
    assert assembly.get("if") == "${{ always() }}"
    downloads = _artifact_steps(assembly, "download")
    assert [step["with"] for step in downloads] == [
        {
            "name": "${{ needs.prepare.outputs.builder_catalog_artifact }}",
            "path": "input/builders",
        },
        {
            "pattern": (
                "ucm-python-probe-*-run-${{ github.run_id }}-"
                "attempt-${{ github.run_attempt }}"
            ),
            "path": "input/python-probes",
            "merge-multiple": True,
        },
        {
            "name": "${{ needs.discover-runtimes.outputs.runtime_discovery_artifact }}",
            "path": "input/runtime-discovery",
        },
        {
            "pattern": (
                "ucm-mooncake-probe-*-run-${{ github.run_id }}-"
                "attempt-${{ github.run_attempt }}"
            ),
            "path": "input/mooncake-probes",
            "merge-multiple": True,
        },
    ]
    catalog_steps = [
        step for step in assembly["steps"] if step.get("id") == "catalog"
    ]
    assert len(catalog_steps) == 1
    run = str(catalog_steps[0]["run"])
    assert "assemble_capability_catalog" in run
    assert "validate_capability_catalog" in run
    assert "out/capability-catalog.json" in run
    assert (
        "ucm-capability-catalog-run-${GITHUB_RUN_ID}-attempt-"
        "${GITHUB_RUN_ATTEMPT}" in run
    )
    assert "capability_catalog_artifact=${artifact}" in run
    assert "${GITHUB_OUTPUT}" in run
    uploads = _artifact_steps(assembly, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    output_value = "${{ steps.catalog.outputs.capability_catalog_artifact }}"
    assert assembly["outputs"]["capability_catalog_artifact"] == output_value
    assert uploads[0]["with"] == {
        "name": output_value,
        "path": "out/capability-catalog.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


@pytest.mark.parametrize("filename", ["release-ucm.yml", "ucm-build-bot.yml"])
def test_planners_consume_capability_catalog_instead_of_flat_builder_catalog(
    filename: str,
) -> None:
    workflow = _load(filename)
    plan = workflow["jobs"]["plan"]
    downloads = [
        step
        for step in _artifact_steps(plan, "download")
        if step.get("with", {}).get("path") == "input/capabilities"
    ]
    assert len(downloads) == 1
    assert downloads[0]["with"] == {
        "name": "${{ needs.sync-builders.outputs.capability_catalog_artifact }}",
        "path": "input/capabilities",
    }
    plan_steps = [
        step
        for step in plan["steps"]
        if "ucm_release compact plan" in str(step.get("run", ""))
    ]
    assert len(plan_steps) == 1
    run = str(plan_steps[0]["run"])
    assert (
        "--capability-catalog input/capabilities/capability-catalog.json" in run
    )
    assert "--builder-catalog" not in run


def test_ascend_builder_copies_mooncake_from_matching_immutable_runtime() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"]["build-missing"]
    build = _step_named(job, "Build missing Builder")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")

    assert build.get("env", {}).get("RUNTIME_IMAGE") == "${{ matrix.runtime_image }}"
    run = str(build["run"])
    assert any(
        "RUNTIME_IMAGE" in line and "@sha256:" in line for line in run.splitlines()
    )
    assert '--build-arg "MOONCAKE_RUNTIME_IMAGE=${RUNTIME_IMAGE}"' in run
    instructions = _noncomment_dockerfile(dockerfile)
    runtime_stage = re.search(
        r"^ARG\s+(?P<arg>[A-Z_]*RUNTIME_IMAGE)\s*$\n"
        r"FROM\s+\$\{(?P=arg)\}\s+AS\s+(?P<stage>[-a-z0-9]+)$",
        instructions,
        re.MULTILINE,
    )
    assert runtime_stage
    assert runtime_stage.group("arg") == "MOONCAKE_RUNTIME_IMAGE"
    stage = runtime_stage.group("stage")
    assert re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+.*include",
        instructions,
        re.MULTILINE,
    )
    assert re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+.*lib",
        instructions,
        re.MULTILINE,
    )


def test_ascend_builder_has_no_tag_inference_or_fixed_mooncake_clone() -> None:
    workflow = _load("sync-builders.yml")
    build = _step_named(workflow["jobs"]["build-missing"], "Build missing Builder")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")
    build_source = str(build.get("run", "")) + "\n" + yaml.safe_dump(
        build.get("with", {}), sort_keys=False
    )
    instructions = _noncomment_dockerfile(dockerfile)
    active_source = build_source + "\n" + instructions

    assert "mooncake_installer.sh" not in active_source
    assert re.search(r"^ARG\s+MOONCAKE_TAG(?:\s|$)", instructions, re.MULTILINE) is None
    assert "--build-arg \"MOONCAKE_TAG=" not in build_source
    assert (
        re.search(
            r"git\s+clone(?:(?!&&).)*(?:kvcache-ai/)?Mooncake",
            instructions,
            re.IGNORECASE | re.DOTALL,
        )
        is None
    )
    assert not any(
        "mooncake" in line.lower() and "TARGET_TAG" in line
        for line in build_source.splitlines()
    )
    assert "0.3.9" not in active_source


def test_probe_matrices_isolate_failures_and_always_upload_results() -> None:
    workflow = _load("sync-builders.yml")
    for job_id in ("probe-python", "probe-mooncake"):
        job = workflow["jobs"].get(job_id)
        assert isinstance(job, dict), f"missing stable {job_id} job"
        assert job["strategy"].get("fail-fast") is False
        uploads = _artifact_steps(job, "upload")
        assert len(uploads) == 1
        assert uploads[0].get("if") == "${{ always() }}"


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
