from __future__ import annotations

import re
from typing import Any

import yaml
from conftest import REPO_ROOT

WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"
PRODUCTION_WORKFLOWS = (
    "production-tag-candidate.yml",
    "_production-build-wheel.yml",
    "_production-build-image.yml",
    "production-release-controller.yml",
    "_production-release-controller.yml",
    "_production-publish-image-member.yml",
)
PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$", re.ASCII)
SPECS = {
    "cuda130-amd64",
    "cuda130-arm64",
    "cann900-a2-amd64",
    "cann900-a2-arm64",
    "cann900-a3-amd64",
    "cann900-a3-arm64",
}


def _workflow(name: str) -> dict[str, Any]:
    value = yaml.safe_load((WORKFLOW_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in document["jobs"].values() for step in job.get("steps", [])]


def test_exact_production_workflow_files_exist() -> None:
    assert all((WORKFLOW_ROOT / name).is_file() for name in PRODUCTION_WORKFLOWS)


def test_candidate_is_tag_only_read_only_and_has_one_aggregate_artifact() -> None:
    workflow = _workflow("production-tag-candidate.yml")

    assert workflow["name"] == "UCM Production Tag Candidate"
    assert workflow["on"] == {"push": {"tags": ["draft/v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert set(workflow["jobs"]) == {
        "route",
        "wheels",
        "images",
        "chart",
        "aggregate",
    }
    for job in workflow["jobs"].values():
        assert job["permissions"] == {"contents": "read"}
        assert "environment" not in job
        assert "secrets" not in job

    route_steps = workflow["jobs"]["route"]["steps"]
    assert route_steps[0]["name"] == "Strictly parse production Tag before checkout"
    assert "tag parse" in route_steps[0]["run"]
    assert not any("uses" in step for step in route_steps[:1])
    identity = next(step for step in route_steps if step.get("id") == "identity")
    assert identity["env"]["DEFAULT_BRANCH"] == (
        "${{ github.event.repository.default_branch }}"
    )
    assert identity["env"]["GH_TOKEN"] == "${{ github.token }}"

    wheels = workflow["jobs"]["wheels"]
    images = workflow["jobs"]["images"]
    assert wheels["uses"] == "./.github/workflows/_production-build-wheel.yml"
    assert images["uses"] == "./.github/workflows/_production-build-image.yml"
    assert set(wheels["strategy"]["matrix"]["spec_id"]) == SPECS
    assert set(images["strategy"]["matrix"]["spec_id"]) == SPECS

    uploads = [
        (job_name, step)
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert uploads[0][0] == "aggregate"
    assert uploads[0][1]["with"]["name"] == "${{ needs.route.outputs.artifact_name }}"


def test_candidate_reusables_have_closed_inputs_and_cache_only_intermediates() -> None:
    wheel = _workflow("_production-build-wheel.yml")
    image = _workflow("_production-build-image.yml")

    assert set(wheel["on"]["workflow_call"]["inputs"]) == {
        "source_sha",
        "tag_name",
        "spec_id",
        "candidate_run_id",
        "candidate_run_attempt",
        "source_identity_b64",
        "control_sha",
        "cache_namespace",
    }
    assert set(image["on"]["workflow_call"]["inputs"]) == {
        "source_sha",
        "tag_name",
        "spec_id",
        "candidate_run_id",
        "candidate_run_attempt",
        "source_identity_b64",
        "source_date_epoch",
        "control_sha",
    }
    for document in (wheel, image):
        assert document["permissions"] == {"contents": "read"}
        assert len(document["jobs"]) == 1
        job = next(iter(document["jobs"].values()))
        assert job["permissions"] == {"contents": "read"}
        assert "environment" not in job
        source = "\n".join(str(step.get("run", "")) for step in job["steps"])
        assert "docker login" not in source
        assert "crane push" not in source
        assert "helm push" not in source
        assert not any(
            str(step.get("uses", "")).startswith("actions/upload-artifact@")
            for step in job["steps"]
        )
        assert any(
            str(step.get("uses", "")).startswith("actions/cache@")
            for step in job["steps"]
        )

    image_build = next(
        step
        for step in image["jobs"]["build"]["steps"]
        if step.get("name") == "Build and inspect production OCI member"
    )
    assert image_build["env"]["SOURCE_DATE_EPOCH"] == (
        "${{ inputs.source_date_epoch }}"
    )


def test_production_wheel_workflow_stages_trusted_config_before_buildx() -> None:
    workflow = _workflow("_production-build-wheel.yml")
    steps = workflow["jobs"]["build"]["steps"]
    resolve_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Resolve production wheel authority"
    )
    build_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build and seal production wheel"
    )
    assert resolve_index < build_index

    resolve_source = str(steps[resolve_index]["run"])
    for fragment in (
        "out/projection/build-authority.json",
        "out/projection/build-projection.json",
        "out/projection/wheel-build.json",
        "out/source-context/wheel-build.json",
        "control/.github/release/ucm_release",
        "out/source-context/trusted-control/ucm_release",
    ):
        assert fragment in resolve_source

    build_source = str(steps[build_index]["run"])
    assert "--target production-wheel" in build_source
    assert "UCM_RELEASE_" not in build_source
    assert "SOURCE_DATE_EPOCH=" not in build_source
    assert "PLATFORM=" not in build_source


def test_production_image_workflows_use_complete_runtime_wheel_context() -> None:
    candidate = _workflow("_production-build-image.yml")
    resolve = next(
        step
        for step in candidate["jobs"]["build"]["steps"]
        if step.get("name") == "Resolve trusted image recipe and dependency lock"
    )
    resolve_source = str(resolve["run"])
    assert "packaging==" in resolve_source
    assert "wrapt==" in resolve_source
    assert "image context" in resolve_source
    assert "out/image-context.json" in resolve_source

    candidate_build = next(
        step
        for step in candidate["jobs"]["build"]["steps"]
        if step.get("name") == "Build and inspect production OCI member"
    )
    candidate_build_source = str(candidate_build["run"])
    assert "runtime_wheel_names" in candidate_build_source
    assert "PACKAGING_WHEEL=${packaging_name}" in candidate_build_source
    assert "WRAPT_WHEEL=${wrapt_name}" in candidate_build_source
    assert "--target production-runtime" in candidate_build_source

    publisher = _workflow("_production-publish-image-member.yml")
    publisher_context = next(
        step
        for step in publisher["jobs"]["publish"]["steps"]
        if step.get("name") == "Download and verify pinned runtime dependencies"
    )
    publisher_source = str(publisher_context["run"])
    assert "packaging==" in publisher_source
    assert "wrapt==" in publisher_source
    assert "out/image-context.json" in publisher_source

    publisher_build = next(
        step
        for step in publisher["jobs"]["publish"]["steps"]
        if step.get("name") == "Rebuild and compare the exact candidate OCI closure"
    )
    assert "PACKAGING_WHEEL" in str(publisher_build["run"])


def test_controller_has_only_successful_candidate_workflow_run_route() -> None:
    workflow = _workflow("production-release-controller.yml")

    assert workflow["name"] == "UCM Production Release Controller"
    assert workflow["on"] == {
        "workflow_run": {
            "workflows": ["UCM Production Tag Candidate"],
            "types": ["completed"],
        }
    }
    assert "workflow_dispatch" not in workflow["on"]
    assert set(workflow["jobs"]) == {"trust", "invoke"}
    trust = workflow["jobs"]["trust"]
    assert trust["permissions"] == {"actions": "read", "contents": "read"}
    assert trust["steps"][0]["name"] == "Validate candidate event before checkout"
    trust_source = str(trust["steps"][0]["run"])
    for fragment in (
        "workflow_run",
        "production-tag-candidate.yml",
        "completed",
        "success",
        "push",
        "head_repository",
        "repository_id",
    ):
        assert fragment in trust_source
    checkout_index = next(
        index
        for index, step in enumerate(trust["steps"])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout_index > 0
    assert trust["steps"][checkout_index]["with"] == {
        "ref": "${{ steps.control.outputs.control_sha }}",
        "path": "control",
        "persist-credentials": False,
    }
    invoke = workflow["jobs"]["invoke"]
    assert invoke["needs"] == "trust"
    assert invoke["uses"] == "./.github/workflows/_production-release-controller.yml"


def test_reusable_controller_separates_read_only_rebuild_from_environment_writes() -> (
    None
):
    workflow = _workflow("_production-release-controller.yml")
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "preflight",
        "rebuild-wheels",
        "compare-and-plan",
        "publish-members",
        "publish-indexes-and-chart",
        "publish-release",
        "evidence",
    }
    for name in ("preflight", "rebuild-wheels", "compare-and-plan", "evidence"):
        assert "environment" not in jobs[name]
        assert jobs[name]["permissions"].get("packages") != "write"
        assert jobs[name]["permissions"].get("contents") != "write"
    for name in ("publish-indexes-and-chart", "publish-release"):
        assert jobs[name]["environment"] == "release-production"
    assert "environment" not in jobs["publish-members"]
    member = _workflow("_production-publish-image-member.yml")
    assert member["jobs"]["publish"]["environment"] == "release-production"
    assert jobs["publish-members"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "packages": "write",
    }
    assert jobs["publish-indexes-and-chart"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "packages": "write",
    }
    assert jobs["publish-release"]["permissions"] == {
        "actions": "read",
        "contents": "write",
        "packages": "read",
    }


def test_final_evidence_reopens_the_preserved_preflight_artifact_paths() -> None:
    workflow = _workflow("_production-release-controller.yml")
    jobs = workflow["jobs"]
    preflight_upload = next(
        step
        for step in jobs["preflight"]["steps"]
        if step.get("name") == "Upload trusted preflight bridge"
    )
    evidence_assemble = next(
        step
        for step in jobs["evidence"]["steps"]
        if step.get("name") == "Assemble canonical production evidence"
    )

    assert "out/reopened/verified-envelope.json" in preflight_upload["with"]["path"]
    assert (
        "--candidate input/preflight/reopened/verified-envelope.json"
        in evidence_assemble["run"]
    )


def test_every_action_is_immutable_or_a_local_reusable_workflow() -> None:
    for name in PRODUCTION_WORKFLOWS:
        workflow = _workflow(name)
        for job in workflow["jobs"].values():
            uses_values = [job.get("uses")]
            uses_values.extend(step.get("uses") for step in job.get("steps", []))
            for uses in filter(None, uses_values):
                assert isinstance(uses, str)
                assert uses.startswith("./.github/workflows/") or PIN.fullmatch(uses)
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False
