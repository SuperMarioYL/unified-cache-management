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
    assert workflow["on"] == {"push": {"tags": ["draft/v*", "v*"]}}
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
