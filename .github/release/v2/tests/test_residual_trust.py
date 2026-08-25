from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
CONTROLLER = "release-control-dry-run.yml"
CONTROLLER_USES = (
    "SuperMarioYL/unified-cache-management/"
    ".github/workflows/release-control-dry-run.yml@main"
)
MANUAL_WRAPPERS = {
    "draft-environment-dry-run.yml": {
        "operation": "draft-environment",
        "environment": "${{ inputs.environment }}",
        "intent_json": "${{ inputs.intent_json }}",
        "nonce": "${{ inputs.nonce }}",
    },
    "release-lifecycle-dry-run.yml": {
        "operation": "protected-lifecycle",
        "stage": "${{ inputs.stage }}",
        "source_sha": "${{ inputs.source_sha }}",
        "intent_json": "${{ inputs.intent_json }}",
        "inventory_json": "${{ inputs.inventory_json }}",
        "promotion_json": "${{ inputs.promotion_json }}",
        "promotion_source_lifecycle_plan_json": (
            "${{ inputs.promotion_source_lifecycle_plan_json }}"
        ),
        "promotion_source_manifest_json": (
            "${{ inputs.promotion_source_manifest_json }}"
        ),
        "known_issues_json": "${{ inputs.known_issues_json }}",
    },
    "release-cleanup-dry-run.yml": {
        "operation": "cleanup",
        "as_of": "${{ inputs.as_of }}",
        "inventory_json": "${{ inputs.inventory_json }}",
    },
    "repository-policy-audit-dry-run.yml": {
        "operation": "policy-audit",
        "repository_role": "${{ inputs.repository_role }}",
        "snapshot_json": "${{ inputs.snapshot_json }}",
    },
}


def _workflow(name: str) -> dict[str, Any]:
    path = WORKFLOW_ROOT / name
    assert path.is_file(), f"missing workflow: {name}"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _events(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("mutation", "replacement"),
    [
        ("same-name-wrong-event", ("workflow_event", "workflow_dispatch")),
        ("same-name-wrong-path", ("workflow_path", ".github/workflows/attacker.yml")),
        (
            "same-name-wrong-ref",
            ("workflow_path", ".github/workflows/push-check.yml@main"),
        ),
    ],
)
def test_develop_event_validator_binds_exact_push_workflow_path(
    mutation: str, replacement: tuple[str, str]
) -> None:
    """Catches a same-name run from another event or workflow file becoming source data."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    parameters = inspect.signature(validator.validate_develop_workflow_run).parameters
    assert {"workflow_event", "workflow_path"} <= set(parameters), mutation
    values = {
        "workflow_name": "Push Commit Checks",
        "workflow_event": "push",
        "workflow_path": ".github/workflows/push-check.yml@develop",
        "conclusion": "success",
        "head_branch": "develop",
        "head_repository": "SuperMarioYL/unified-cache-management",
        "event_repository": "SuperMarioYL/unified-cache-management",
        "head_sha": "a" * 40,
    }
    key, value = replacement
    values[key] = value

    with pytest.raises(validator.ReadOnlyGitHubError):
        validator.validate_develop_workflow_run(**values)


def test_develop_event_validator_accepts_realistic_push_workflow_identity() -> None:
    """Catches rejecting the exact production push workflow path and develop ref."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    parameters = inspect.signature(validator.validate_develop_workflow_run).parameters
    assert {"workflow_event", "workflow_path"} <= set(parameters)

    assert (
        validator.validate_develop_workflow_run(
            workflow_name="Push Commit Checks",
            workflow_event="push",
            workflow_path=".github/workflows/push-check.yml@develop",
            conclusion="success",
            head_branch="develop",
            head_repository="SuperMarioYL/unified-cache-management",
            event_repository="SuperMarioYL/unified-cache-management",
            head_sha="a" * 40,
        )
        == "a" * 40
    )


def test_develop_workflow_passes_event_and_path_to_the_embedded_gate() -> None:
    """Catches the workflow validating only a display name shared by another run."""
    source = (WORKFLOW_ROOT / "develop-release-dry-run.yml").read_text(encoding="utf-8")

    assert "${{ github.event.workflow_run.event }}" in source
    assert "${{ github.event.workflow_run.path }}" in source
    assert 'WORKFLOW_EVENT"] != "push"' in source
    assert 'WORKFLOW_PATH"] != ".github/workflows/push-check.yml@develop"' in source


def test_manual_workflows_are_data_only_wrappers_for_exact_main_controller() -> None:
    """Catches branch-selected wrappers retaining any executable control surface."""
    for name, expected_inputs in MANUAL_WRAPPERS.items():
        workflow = _workflow(name)
        assert set(_events(workflow)) == {"workflow_dispatch"}
        assert workflow["permissions"] == {"contents": "read"}
        assert len(workflow["jobs"]) == 1
        job = next(iter(workflow["jobs"].values()))
        assert set(job) == {"permissions", "uses", "with"}
        assert job["permissions"] == {"contents": "read"}
        assert job["uses"] == CONTROLLER_USES
        assert job["with"] == expected_inputs
        assert "steps" not in job
        assert "runs-on" not in job


def test_reusable_controller_is_main_resolved_before_any_control_checkout() -> None:
    """Catches a reusable controller trusting caller workflow_sha or selected ref."""
    workflow = _workflow(CONTROLLER)
    assert set(_events(workflow)) == {"workflow_call"}
    assert workflow["permissions"] == {"contents": "read"}
    control = workflow["jobs"]["control"]
    source = "\n".join(str(step.get("run", "")) for step in control["steps"])
    environment = {
        str(value)
        for step in control["steps"]
        for value in step.get("env", {}).values()
    }

    assert source.count("--request GET") == 2
    assert source.count("--max-redirs 0") == 2
    assert "${{ toJSON(job) }}" in environment
    assert 'job_context["workflow_ref"]' in source
    assert 'job_context["workflow_sha"]' in source
    assert 'job_context["workflow_repository"]' in source
    assert 'job_context["workflow_file_path"]' in source
    assert "required_job_keys" in source
    assert "if not required_job_keys <= set(job_context):" in source
    assert "for key in required_job_keys" in source
    assert "set(job_context) != required_job_keys" not in source
    assert "github.workflow_sha" not in str(control)
    assert "GITHUB_REF" not in source
    assert "refs/heads/main" in source
    assert "release-control-dry-run.yml@refs/heads/main" in source
    assert control["outputs"] == {
        "control_sha": "${{ steps.control.outputs.control_sha }}"
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-repository",
        "wrong-path",
        "wrong-ref",
        "runs-on",
        "steps",
        "selected-ref-as-data",
    ],
)
def test_security_auditor_rejects_manual_wrapper_control_mutations(
    mutation: str,
) -> None:
    """Catches a wrapper changing the exact controller or regaining execution."""
    security = importlib.import_module("ucm_release_v2.security")
    name = "draft-environment-dry-run.yml"
    source = (WORKFLOW_ROOT / name).read_text(encoding="utf-8")
    replacements = {
        "wrong-repository": (
            "SuperMarioYL/unified-cache-management/.github/workflows/",
            "attacker/unified-cache-management/.github/workflows/",
        ),
        "wrong-path": (
            "release-control-dry-run.yml@main",
            "attacker-controller.yml@main",
        ),
        "wrong-ref": (
            "release-control-dry-run.yml@main",
            "release-control-dry-run.yml@develop",
        ),
        "runs-on": (
            "permissions:\n      contents: read\n    uses:",
            "permissions:\n      contents: read\n    runs-on: ubuntu-latest\n    uses:",
        ),
        "steps": (
            "permissions:\n      contents: read\n    uses:",
            "permissions:\n      contents: read\n    steps:\n      - run: echo attacker\n    uses:",
        ),
        "selected-ref-as-data": (
            "intent_json: ${{ inputs.intent_json }}",
            "intent_json: ${{ github.ref }}",
        ),
    }
    before, after = replacements[mutation]
    assert source.count(before) == 1
    mutated = source.replace(before, after, 1)

    assert security.audit_workflow_source(mutated, name), mutation


def test_policy_summary_names_omitted_free_form_evidence() -> None:
    """Catches the summary implying raw gap evidence is present or validated."""
    workflow = _workflow(CONTROLLER)
    source = "\n".join(
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    )

    assert "### Gaps (free-form evidence omitted)" in source
    assert "check['evidence']" not in source
    assert "repository identity" in source
    assert "compliance status" in source
    assert "report digest" not in source
