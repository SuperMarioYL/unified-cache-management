from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
V2_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_NAMES = [
    "pr-release-dry-run.yml",
    "develop-release-dry-run.yml",
    "nightly-release-dry-run.yml",
]
RELEASE_LIFECYCLE_WORKFLOW = "release-lifecycle-dry-run.yml"
RELEASE_CONTROL_WORKFLOW = "release-control-dry-run.yml"
TASK_7_WORKFLOWS = [
    "release-cleanup-dry-run.yml",
    "repository-policy-audit-dry-run.yml",
]
PRODUCTION_WORKFLOW_NAMES = [
    "production-tag-candidate.yml",
    "_production-build-wheel.yml",
    "_production-build-image.yml",
    "production-release-controller.yml",
    "_production-release-controller.yml",
    "_production-publish-image-member.yml",
]
PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _workflow(name: str) -> dict[str, Any]:
    value = yaml.safe_load((WORKFLOW_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _events(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 resolves the plain key `on` as True.
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def _run_source(workflow: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(workflow))


def _trusted_pr_control_violations(workflow: dict[str, Any]) -> list[str]:
    """Return control-tree violations without exempting either PR event path."""
    violations: list[str] = []
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return ["jobs missing"]
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            violations.append(f"{job_name}: job invalid")
            continue
        checkouts = [
            step
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        if len(checkouts) != 1:
            violations.append(f"{job_name}: requires exactly one control checkout")
            continue
        checkout = checkouts[0]
        inputs = checkout.get("with", {})
        if inputs.get("ref") != "${{ github.workflow_sha }}":
            violations.append(f"{job_name}: checkout is not trusted workflow_sha")
        if inputs.get("path") != "control":
            violations.append(f"{job_name}: checkout path is not control")
        if inputs.get("persist-credentials") is not False:
            violations.append(f"{job_name}: checkout persists credentials")
        source = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
        if "PYTHONPATH=.github/release/v2" in source:
            violations.append(f"{job_name}: executes workspace control plane")
        for line in source.splitlines():
            if "python -m ucm_release_v2" in line and not line.strip().startswith(
                "PYTHONPATH=control/.github/release/v2 python -m ucm_release_v2"
            ):
                violations.append(f"{job_name}: CLI is not loaded from control tree")
        for line in source.splitlines():
            if (
                "--config" in line
                and "control/.github/release/v2/release.yaml" not in line
            ):
                violations.append(f"{job_name}: config is not loaded from control tree")
    return violations


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def test_production_workflows_are_explicitly_outside_the_v2_dry_run_policy() -> None:
    """Production writes have their own closed policy and cannot expand v2 scope."""
    dry_run_names = {
        *WORKFLOW_NAMES,
        RELEASE_LIFECYCLE_WORKFLOW,
        RELEASE_CONTROL_WORKFLOW,
        *TASK_7_WORKFLOWS,
    }
    assert dry_run_names.isdisjoint(PRODUCTION_WORKFLOW_NAMES)
    assert all((WORKFLOW_ROOT / name).is_file() for name in PRODUCTION_WORKFLOW_NAMES)


def test_lifecycle_validate_cli_reopens_the_generated_plan(tmp_path: Path) -> None:
    """Catches workflows accepting a plan without revalidating its digest and semantics."""
    plan_path = tmp_path / "lifecycle-plan.json"
    planned = _run_cli(
        "lifecycle",
        "plan",
        "--stage",
        "develop",
        "--trigger",
        "push",
        "--ref",
        "refs/heads/develop",
        "--source-sha",
        "a" * 40,
        "--repository-role",
        "validation",
        "--run-number",
        "42",
        "--output",
        str(plan_path),
    )
    assert planned.returncode == 0, planned.stderr

    validated = _run_cli("lifecycle", "validate", "--plan", str(plan_path))
    document = json.loads(validated.stdout)

    assert validated.returncode == 0, validated.stderr
    assert document == {
        "kind": "lifecycle-plan-validation",
        "mode": "dry-run",
        "plan_sha256": json.loads(plan_path.read_text(encoding="utf-8"))["sha256"],
        "schema_version": 2,
        "semantic_gates": [
            {"name": "canonical-self-digest", "status": "passed"},
            {"name": "configured-route", "status": "passed"},
            {"name": "configured-product-closure", "status": "passed"},
            {"name": "source-version-binding", "status": "passed"},
            {"name": "release-intent-binding", "status": "passed"},
        ],
        "source_sha": "a" * 40,
        "stage": "develop",
        "status": "passed",
    }


def test_every_plan_generating_workflow_immediately_runs_semantic_validation() -> None:
    """No workflow consumer may use a plan that only passed structural validation."""
    expected_generate_steps = {
        "develop-release-dry-run.yml": 1,
        "nightly-release-dry-run.yml": 1,
        "pr-release-dry-run.yml": 2,
        "release-control-dry-run.yml": 2,
    }
    for name, expected in expected_generate_steps.items():
        workflow = _workflow(name)
        generating_runs = [
            str(step["run"])
            for step in _steps(workflow)
            if "run" in step
            and "python -m ucm_release_v2 lifecycle plan" in str(step["run"])
        ]
        assert len(generating_runs) == expected
        assert all(
            run.index("lifecycle plan") < run.index("lifecycle validate")
            for run in generating_runs
        )


def test_workflows_have_only_the_intended_read_only_routes_and_permissions() -> None:
    """Catches an event or token-permission change that creates a write-capable route."""
    pr = _workflow("pr-release-dry-run.yml")
    develop = _workflow("develop-release-dry-run.yml")
    nightly = _workflow("nightly-release-dry-run.yml")

    assert set(_events(pr)) == {"pull_request", "issue_comment"}
    assert set(_events(develop)) == {"workflow_run"}
    assert _events(develop)["workflow_run"] == {
        "workflows": ["Push Commit Checks"],
        "branches": ["develop"],
        "types": ["completed"],
    }
    assert set(_events(nightly)) == {"schedule"}
    assert "workflow_dispatch" not in _events(nightly)

    for workflow in (pr, develop, nightly):
        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["concurrency"]["group"]
        assert workflow["concurrency"]["cancel-in-progress"] is False
        for job in workflow["jobs"].values():
            assert job["permissions"] == {"contents": "read"}


def test_every_action_is_immutable_and_every_checkout_drops_credentials() -> None:
    """Catches mutable Action tags or credential persistence in dry-run jobs."""
    for name in WORKFLOW_NAMES:
        workflow = _workflow(name)
        action_steps = [step for step in _steps(workflow) if "uses" in step]
        assert action_steps
        for step in action_steps:
            assert PIN.fullmatch(step["uses"]), step["uses"]
        checkout_steps = [
            step
            for step in action_steps
            if step["uses"].startswith("actions/checkout@")
        ]
        assert checkout_steps
        for step in checkout_steps:
            assert step["with"]["persist-credentials"] is False
            assert step["with"]["ref"] in {
                "${{ github.workflow_sha }}",
                "${{ steps.control.outputs.control_sha }}",
            }


def test_pr_workflow_keeps_fork_head_source_separate_from_trusted_control_sha() -> None:
    """Catches a fork PR executing head code or confusing source and control identity."""
    workflow = _workflow("pr-release-dry-run.yml")
    source = _run_source(workflow)
    pull_source = "\n".join(
        str(step.get("run", ""))
        for step in workflow["jobs"]["pull-request-preview"]["steps"]
    )
    checkout_refs = [
        step["with"]["ref"]
        for step in _steps(workflow)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkout_refs == ["${{ github.workflow_sha }}", "${{ github.workflow_sha }}"]
    assert _trusted_pr_control_violations(workflow) == []
    assert "${{ github.event.pull_request.head.sha }}" not in checkout_refs
    assert "${{ github.sha }}" not in checkout_refs
    assert '--source-sha "$SOURCE_SHA"' in source
    assert '--ref "refs/pull/${PR_NUMBER}/head"' in source
    assert "CONTROL_SHA" in source
    assert "BASE_SHA" in source
    assert 'observed["head"]["sha"]' in source
    assert 'current["base"]["sha"]' in source
    assert '"control_sha": os.environ["CONTROL_SHA"]' in pull_source
    assert '"source_sha": os.environ["SOURCE_SHA"]' in pull_source
    assert 're.fullmatch(r"[0-9a-f]{40}", value)' in pull_source
    assert not re.search(
        r"(?:SOURCE_SHA|source_sha).*(?:==|!=).*(?:CONTROL_SHA|control_sha)",
        pull_source,
    )
    assert not re.search(
        r"(?:CONTROL_SHA|control_sha).*(?:==|!=).*(?:SOURCE_SHA|source_sha)",
        pull_source,
    )


@pytest.mark.parametrize(
    "mutation", ["head-ref", "missing-path", "workspace-pythonpath"]
)
def test_pr_control_tree_policy_rejects_trust_boundary_mutations(mutation: str) -> None:
    """Catches future checkout or PYTHONPATH changes that execute fork-controlled code."""
    workflow = copy.deepcopy(_workflow("pr-release-dry-run.yml"))
    job = workflow["jobs"]["pull-request-preview"]
    checkout = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    if mutation == "head-ref":
        checkout["with"]["ref"] = "${{ github.event.pull_request.head.sha }}"
    elif mutation == "missing-path":
        checkout["with"].pop("path", None)
    else:
        for step in job["steps"]:
            if "run" in step:
                step["run"] = str(step["run"]).replace(
                    "PYTHONPATH=control/.github/release/v2",
                    "PYTHONPATH=.github/release/v2",
                )

    assert _trusted_pr_control_violations(workflow)


def test_issue_comment_is_pr_only_rechecks_head_and_never_interpolates_body() -> None:
    """Catches comment injection or a TOCTOU window applying a command to a changed PR head."""
    workflow = _workflow("pr-release-dry-run.yml")
    comment_jobs = [
        job
        for job in workflow["jobs"].values()
        if "github.event_name == 'issue_comment'" in str(job.get("if", ""))
    ]
    assert len(comment_jobs) == 1
    job = comment_jobs[0]
    assert "github.event.issue.pull_request" in job["if"]
    source = "\n".join(str(step.get("run", "")) for step in job["steps"])
    env_values = [
        str(value) for step in job["steps"] for value in step.get("env", {}).values()
    ]

    assert source.count("api.github.com/repos/") == 2
    assert "--body-env COMMENT_BODY" in source
    assert "${{ github.event.comment.body }}" in env_values
    assert "${{ github.event.comment.body }}" not in source
    assert '--observed-source-sha "$OBSERVED_SOURCE_SHA"' in source
    assert '--current-source-sha "$CURRENT_SOURCE_SHA"' in source
    assert "/release build <40-lowerhex-sha>" in source
    assert "requested_source_sha" in source
    assert "pull_request_target" not in str(_events(workflow))
    assert "github.event.pull_request.head.sha" not in source


def test_develop_workflow_run_executes_only_default_branch_control_code() -> None:
    """Catches mutable develop lifecycle code being checked out or executed as control."""
    workflow = _workflow("develop-release-dry-run.yml")
    source = _run_source(workflow)
    checkout = next(
        step
        for step in _steps(workflow)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"] == {
        "ref": "${{ steps.control.outputs.control_sha }}",
        "path": "control",
        "persist-credentials": False,
    }
    assert "${{ github.sha }}" not in str(workflow)
    assert "github.event.workflow_run.head_sha" in str(workflow)
    assert "github.event.workflow_run.head_branch" in str(workflow)
    assert "github.event.workflow_run.head_repository.full_name" in str(workflow)
    assert "github.event.workflow_run.name" in str(workflow)
    assert "github.event.workflow_run.conclusion" in str(workflow)
    assert "PYTHONPATH=control/.github/release/v2" in source
    assert "PYTHONPATH=.github/release/v2" not in source
    assert "checkout" not in source.lower()


def test_manual_workflows_gate_exact_main_control_before_checkout_or_cli() -> None:
    """Catches the trusted reusable controller accepting a stale selected ref."""
    for name in (
        "draft-environment-dry-run.yml",
        "release-lifecycle-dry-run.yml",
        "release-cleanup-dry-run.yml",
        "repository-policy-audit-dry-run.yml",
    ):
        workflow = _workflow(name)
        job = workflow["jobs"]["invoke-trusted-main-controller"]
        assert set(job) == {"permissions", "uses", "with"}
        assert job["uses"].endswith("release-control-dry-run.yml@main")

    controller = _workflow(RELEASE_CONTROL_WORKFLOW)
    steps = controller["jobs"]["control"]["steps"]
    source = "\n".join(str(step.get("run", "")) for step in steps)
    assert source.count("/git/ref/heads/${CONFIGURED_MAIN}") == 2
    assert source.count("--request GET") == 2
    assert source.count("--max-redirs 0") == 2
    assert "object_pairs_hook=reject_duplicates" in source
    assert "JOB_CONTEXT_JSON" in str(steps)
    assert "toJSON(job)" in str(steps)
    assert "control_sha=" in source
    assert "python -m ucm_release_v2" not in source


def test_policy_summary_never_renders_snapshot_derived_evidence() -> None:
    """Catches offline snapshot strings injecting Markdown or HTML into Job Summary."""
    source = _run_source(_workflow(RELEASE_CONTROL_WORKFLOW))

    assert "check['evidence']" not in source
    assert "f\"- `{check['id']}`: `{check['status']}`\"" in source


def test_workflows_plan_validate_upload_and_summarize_the_expected_stages() -> None:
    """Catches a route passing the wrong stage identity or omitting plan validation."""
    expected = {
        "pr-release-dry-run.yml": [
            "--stage pr",
            "--trigger pull_request",
            '--pr-number "$PR_NUMBER"',
        ],
        "develop-release-dry-run.yml": [
            "--stage develop",
            "--trigger push",
            '--run-number "$RUN_NUMBER"',
        ],
        "nightly-release-dry-run.yml": [
            "--stage nightly",
            "--trigger schedule",
            '--date "$UTC_DATE"',
            '--run-number "$RUN_NUMBER"',
        ],
    }

    for name, fragments in expected.items():
        workflow = _workflow(name)
        source = _run_source(workflow)
        for fragment in fragments:
            assert fragment in source
        assert "lifecycle validate" in source
        assert any(
            str(step.get("uses", "")).startswith("actions/upload-artifact@")
            for step in _steps(workflow)
        )
        assert "GITHUB_STEP_SUMMARY" in source
        assert "PYTHONPATH" in str(workflow)
        assert "pip install" not in source


def test_nightly_keeps_main_control_and_observes_develop_as_readonly_data() -> None:
    """Catches scheduled control code being replaced by the mutable develop source."""
    workflow = _workflow("nightly-release-dry-run.yml")
    source = _run_source(workflow)

    assert "datetime.now(timezone.utc)" in source
    assert "validate_control_identity" in source
    assert "github.workflow_sha" in str(workflow)
    assert source.count("git/ref/heads/${DEVELOP_BRANCH}") == 2
    assert source.count("--request GET") == 2
    assert "validate_develop_reads" not in source
    assert "ucm_release_v2.github_readonly" in source
    assert '--ref "refs/heads/$DEVELOP_BRANCH"' in source
    assert '--source-sha "$SOURCE_SHA"' in source


@pytest.mark.parametrize(
    "forbidden",
    [
        "pull_request_target",
        "permissions: write",
        "contents: write",
        "actions: write",
        "packages: write",
        "id-token: write",
        "docker login",
        "helm push",
        "twine upload",
        "gh release",
        "workflow_dispatch",
        "curl -X POST",
        "curl -X PATCH",
        "curl -X DELETE",
        "eval ",
    ],
)
def test_workflow_sources_contain_no_publication_or_dynamic_execution_path(
    forbidden: str,
) -> None:
    """Catches a dry-run workflow acquiring a release, registry, API-write, or eval path."""
    combined = "\n".join(
        (WORKFLOW_ROOT / name).read_text(encoding="utf-8").lower()
        for name in WORKFLOW_NAMES
    )

    assert forbidden.lower() not in combined


def test_release_lifecycle_workflow_is_manual_trusted_and_strictly_read_only() -> None:
    """A permission, checkout, trigger, or command mutation must not create a write route."""
    workflow = _workflow(RELEASE_LIFECYCLE_WORKFLOW)
    dispatch = _events(workflow)["workflow_dispatch"]["inputs"]
    assert set(dispatch) == {
        "stage",
        "source_sha",
        "intent_json",
        "inventory_json",
        "promotion_json",
        "promotion_source_lifecycle_plan_json",
        "promotion_source_manifest_json",
        "known_issues_json",
    }
    assert dispatch["stage"]["options"] == ["rc", "stable", "hotfix"]
    assert dispatch["source_sha"]["required"] is True
    assert dispatch["intent_json"]["required"] is True
    assert dispatch["inventory_json"]["required"] is True
    assert dispatch["promotion_json"]["required"] is False
    assert dispatch["promotion_source_lifecycle_plan_json"]["required"] is False
    assert dispatch["promotion_source_manifest_json"]["required"] is False
    assert dispatch["known_issues_json"]["required"] is False
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    wrapper = workflow["jobs"]["invoke-trusted-main-controller"]
    assert set(wrapper) == {"permissions", "uses", "with"}
    assert wrapper["uses"].endswith("release-control-dry-run.yml@main")
    controller = _workflow(RELEASE_CONTROL_WORKFLOW)
    actions = [step for step in _steps(controller) if "uses" in step]
    assert actions
    assert all(PIN.fullmatch(step["uses"]) for step in actions)
    checkout = next(
        step for step in actions if step["uses"].startswith("actions/checkout@")
    )
    assert checkout["with"] == {
        "ref": "${{ needs.control.outputs.control_sha }}",
        "path": "control",
        "persist-credentials": False,
    }


def test_release_lifecycle_workflow_normalizes_inputs_then_renders_without_network() -> (
    None
):
    """Raw dispatch JSON cannot be shell-interpolated or bypass duplicate-key parsing."""
    workflow = _workflow(RELEASE_CONTROL_WORKFLOW)
    source = _run_source(workflow)
    env_values = [
        str(value)
        for step in _steps(workflow)
        for value in step.get("env", {}).values()
    ]
    assert "object_pairs_hook=reject_duplicates" in source
    assert "release-inventory.json" in source
    assert "promotion-evidence.json" in source
    assert "known-issues.json" in source
    assert "${{ inputs.inventory_json }}" in env_values
    assert "${{ inputs.promotion_json }}" in env_values
    assert "${{ inputs.promotion_source_lifecycle_plan_json }}" in env_values
    assert "${{ inputs.promotion_source_manifest_json }}" in env_values
    assert "${{ inputs.inventory_json }}" not in source
    assert "${{ inputs.promotion_json }}" not in source
    assert "lifecycle plan" in source
    assert "lifecycle validate" in source
    assert "artifacts collect" in source
    assert "artifacts validate" in source
    assert "reconcile plan" in source
    assert "release render" in source
    assert source.count("--inventory preview/release-inventory.json") == 2
    assert source.count("--promotion preview/promotion-evidence.json") == 2
    assert "release-preview.md" in source
    assert "GITHUB_STEP_SUMMARY" in source
    assert "--execute" not in source
    protected_source = "\n".join(
        str(step.get("run", ""))
        for step in workflow["jobs"]["release-preview"]["steps"]
    )
    assert "environment-verification" not in protected_source
    for forbidden in (
        "wget ",
        "gh api",
        "gh release",
        "docker login",
        "helm push",
        "twine upload",
        "kubectl",
        "python -m pip",
        "pip install",
        "requests.",
        "urllib.",
        "socket.",
        "eval ",
    ):
        assert forbidden not in source.lower()
    uploads = [
        step
        for step in _steps(workflow)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert any("release-preview.md" in upload["with"]["path"] for upload in uploads)


def test_task_7_workflows_are_manual_trusted_and_read_only() -> None:
    """Catches cleanup or policy audit acquiring a non-manual, mutable, or write-capable route."""
    for name in TASK_7_WORKFLOWS:
        workflow = _workflow(name)
        assert set(_events(workflow)) == {"workflow_dispatch"}
        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["concurrency"]["cancel-in-progress"] is False
        job = workflow["jobs"]["invoke-trusted-main-controller"]
        assert set(job) == {"permissions", "uses", "with"}
        assert job["permissions"] == {"contents": "read"}
        assert job["uses"].endswith("release-control-dry-run.yml@main")

    controller = _workflow(RELEASE_CONTROL_WORKFLOW)
    actions = [step for step in _steps(controller) if "uses" in step]
    assert actions and all(PIN.fullmatch(step["uses"]) for step in actions)
    checkouts = [
        step for step in actions if step["uses"].startswith("actions/checkout@")
    ]
    assert checkouts
    assert all(
        checkout["with"]
        == {
            "ref": "${{ needs.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        }
        for checkout in checkouts
    )


def test_cleanup_workflow_strictly_normalizes_offline_inventory_and_as_of() -> None:
    """Catches dispatch input interpolation, network inventory discovery, or execution flags."""
    workflow = _workflow("release-cleanup-dry-run.yml")
    dispatch = _events(workflow)["workflow_dispatch"]["inputs"]
    assert set(dispatch) == {"as_of", "inventory_json"}
    source = _run_source(_workflow(RELEASE_CONTROL_WORKFLOW))
    controller = _workflow(RELEASE_CONTROL_WORKFLOW)
    env_values = [
        str(value)
        for step in _steps(controller)
        for value in step.get("env", {}).values()
    ]
    assert "object_pairs_hook=reject_duplicates" in source
    assert "cleanup-inventory.json" in source
    assert "cleanup-plan.json" in source
    assert "cleanup plan" in source
    assert "${{ inputs.inventory_json }}" in env_values
    assert "${{ inputs.inventory_json }}" not in source
    assert "--execute" not in source
    assert "GITHUB_STEP_SUMMARY" in source
    assert any(
        "cleanup-plan.json" in step["with"]["path"]
        for step in _steps(controller)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )


def test_policy_workflow_audits_only_the_supplied_offline_snapshot() -> None:
    """Catches repository policy audit querying or mutating GitHub instead of using its snapshot."""
    workflow = _workflow("repository-policy-audit-dry-run.yml")
    dispatch = _events(workflow)["workflow_dispatch"]["inputs"]
    assert set(dispatch) == {"repository_role", "snapshot_json"}
    assert dispatch["repository_role"]["options"] == ["validation", "production"]
    source = _run_source(_workflow(RELEASE_CONTROL_WORKFLOW))
    controller = _workflow(RELEASE_CONTROL_WORKFLOW)
    env_values = [
        str(value)
        for step in _steps(controller)
        for value in step.get("env", {}).values()
    ]
    assert "object_pairs_hook=reject_duplicates" in source
    assert "repository-policy-snapshot.json" in source
    assert "repository-policy-report.json" in source
    assert "repo-policy audit" in source
    assert "${{ inputs.snapshot_json }}" in env_values
    assert "${{ inputs.snapshot_json }}" not in source
    assert "GITHUB_STEP_SUMMARY" in source


@pytest.mark.parametrize(
    "forbidden",
    [
        "wget ",
        "gh api",
        "requests.",
        "urllib.",
        "socket.",
        "github-script",
        "contents: write",
        "actions: write",
        "packages: write",
        "id-token: write",
        "--execute",
        "git push",
        "gh release",
        "docker login",
        "kubectl",
    ],
)
def test_task_7_workflows_have_no_network_write_or_settings_route(
    forbidden: str,
) -> None:
    """Catches either offline workflow gaining a network, publication, or settings mutation."""
    combined = "\n".join(
        (WORKFLOW_ROOT / name).read_text(encoding="utf-8").lower()
        for name in [*TASK_7_WORKFLOWS, RELEASE_CONTROL_WORKFLOW]
    )
    assert forbidden not in combined
