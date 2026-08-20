"""Workflow safety-invariant contract for the slim release lane.

Only the cross-cutting safety invariants are retained: fork-isolation
auditing and the reusable-build contract gate that runs before any
untrusted code or network access.  Structural YAML change-detector tests
(exact job names, step order, input sets, artifact names) were removed per
the slimming plan -- they asserted "we wrote what we wrote", not behaviour.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_RELEASE_WORKFLOWS = {
    "_build-image.yml",
    "_publish-image-member.yml",
    "_build-wheel.yml",
    "hardware-e2e.yml",
    "release-ucm.yml",
    "release-vllm-images-protected.yml",
    "release-vllm-images.yml",
    "sync-builders.yml",
}
SAFE_FORK_ACTIONS = {
    "actions/cache",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "jlumbroso/free-disk-space",
    "azure/setup-helm",
    "docker/setup-buildx-action",
    "docker/setup-qemu-action",
    "sigstore/cosign-installer",
}


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _strings(nested)]
    return [str(value)]


def _load_workflow(path: Path) -> dict[str, object]:
    """Load Actions YAML without letting YAML 1.1 turn ``on`` into ``True``."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path.name} must contain a YAML object")
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _jobs(document: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    assert all(isinstance(job, dict) for job in jobs.values())
    return jobs  # type: ignore[return-value]


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps  # type: ignore[return-value]


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() == "true"


def _effective_permissions(
    workflow_permissions: object, job: dict[object, object]
) -> tuple[object, bool]:
    """GitHub job permissions replace workflow permissions when explicitly set."""
    if "permissions" in job:
        return job["permissions"], False
    return workflow_permissions, True


def _permissions_grant_write(permissions: object) -> bool:
    if isinstance(permissions, dict):
        return any(str(value).lower() == "write" for value in permissions.values())
    if isinstance(permissions, str):
        normalized = permissions.lower().replace(" ", "")
        return normalized == "write-all" or bool(
            re.search(r"(?:^|,)\w+:write(?:,|$)", normalized)
        )
    return False


def _action_operation(uses: object, inputs: object) -> str | None:
    if not isinstance(uses, str) or not uses:
        return None
    action = uses.split("@", 1)[0].lower()
    if action in SAFE_FORK_ACTIONS:
        return None
    if action == "docker/build-push-action":
        if isinstance(inputs, dict) and _truthy(inputs.get("push")):
            return "container publishing action"
        return None
    if action == "docker/login-action":
        return "registry credential action"
    if action.startswith("./.github/workflows/"):
        workflow_name = Path(action).name
        if workflow_name in EXPECTED_RELEASE_WORKFLOWS:
            return None
    return f"unapproved action {action}"


def _dangerous_job_operations(
    workflow_permissions: object, job: dict[object, object]
) -> list[str]:
    """Return publication-capable operations that must be upstream-gated."""
    operations: list[str] = []
    if job.get("secrets") == "inherit":
        operations.append("secrets: inherit")
    permissions, inherited = _effective_permissions(workflow_permissions, job)
    if _permissions_grant_write(permissions):
        label = (
            "workflow-inherited write permission" if inherited else "write permission"
        )
        operations.append(label)
    if "environment" in job:
        operations.append("protected environment")

    job_text = "\n".join(_strings(job)).lower()
    if "self-hosted" in job_text:
        operations.append("self-hosted runner")
    command_patterns = {
        r"\b(?:docker|crane)\s+(?:login|push|copy)\b": "registry login or publication",
        r"\bbuildx\s+build\b[^\n]*--push\b": "Buildx publication",
        r"\bgh\s+workflow\s+run\b": "workflow dispatch",
        r"\bgh\s+api\b[^\n]*(?:/dispatches\b|workflow_dispatch\b)": "GitHub dispatch API",
        r"\b(?:curl|wget)\b[^\n]*(?:/dispatches\b|workflow_dispatch\b)": "HTTP dispatch",
    }
    for pattern, label in command_patterns.items():
        if re.search(pattern, job_text):
            operations.append(label)

    job_action = _action_operation(job.get("uses"), job.get("with"))
    if job_action:
        operations.append(job_action)
    for step in job.get("steps", []) if isinstance(job.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        action_operation = _action_operation(step.get("uses"), step.get("with"))
        if action_operation:
            operations.append(action_operation)
    return sorted(set(operations))


def _has_upstream_guard(job: dict[object, object]) -> bool:
    condition = str(job.get("if", ""))
    protected_tag_guard = all(
        fragment in condition
        for fragment in (
            "github.event_name == 'push'",
            "github.ref_type == 'tag'",
            "github.ref_protected == true",
        )
    )
    protected_dispatch_guard = "github.event_name == 'workflow_dispatch'" in condition
    return protected_tag_guard or protected_dispatch_guard


def _fork_isolation_violations(documents: dict[str, object]) -> list[str]:
    """Audit entry and locally reusable release workflows for a fork path escape."""
    violations: list[str] = []
    for filename, document in documents.items():
        if not isinstance(document, dict):
            continue
        jobs = document.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        workflow_permissions = document.get("permissions")
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            operations = _dangerous_job_operations(workflow_permissions, job)
            if operations and not _has_upstream_guard(job):
                violations.append(
                    f"{filename}:{job_name} exposes fork candidates to "
                    f"{', '.join(operations)} without an upstream repository guard"
                )
    return violations


def test_fork_isolation_rejects_reusable_workflow_publish_mutations() -> None:
    """Reusable workflow mutations must be rejected even when entry job is clean."""
    documents = {
        "release-ucm.yml": {
            "jobs": {
                "fork-candidate": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [{"run": "python -m ucm_release catalog validate"}],
                }
            }
        },
        "_build-image.yml": {
            "jobs": {
                "mutated-reusable": {
                    "secrets": "inherit",
                    "runs-on": "self-hosted",
                    "steps": [
                        {"uses": "docker/login-action@v3"},
                        {
                            "uses": "docker/build-push-action@v6",
                            "with": {"push": True},
                        },
                        {"uses": "softprops/action-gh-release@v2"},
                        {
                            "run": (
                                "docker buildx build --push .\n"
                                "crane copy source target\n"
                                "gh workflow run child.yml\n"
                                "gh api --method POST repos/x/dispatches\n"
                                "curl -X POST https://api.github.com/repos/x/dispatches"
                            )
                        },
                    ],
                }
            }
        },
    }

    violations = _fork_isolation_violations(documents)

    assert len(violations) == 1
    violation = violations[0]
    for operation in (
        "secrets: inherit",
        "self-hosted runner",
        "registry credential action",
        "container publishing action",
        "unapproved action softprops/action-gh-release",
        "Buildx publication",
        "registry login or publication",
        "workflow dispatch",
        "GitHub dispatch API",
        "HTTP dispatch",
    ):
        assert operation in violation


def test_fork_isolation_allows_a_read_only_reusable_build() -> None:
    """A normal hosted build and artifact upload remain valid fork operations."""
    documents = {
        "_build-wheel.yml": {
            "jobs": {
                "build": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [
                        {"uses": "actions/checkout@full-sha"},
                        {"run": "docker buildx build --output type=oci,dest=out.tar ."},
                        {"uses": "actions/upload-artifact@full-sha"},
                    ],
                }
            }
        }
    }

    assert _fork_isolation_violations(documents) == []


def test_push_and_pull_request_callers_use_explicit_minimum_permissions() -> None:
    """Normal fork validation callers must not inherit the repository token default."""
    push = _load_workflow(WORKFLOW_DIR / "push-check.yml")
    pull_request = _load_workflow(WORKFLOW_DIR / "pull-request.yml")
    assert push["permissions"] == {"contents": "read"}
    assert _jobs(push)["lint-and-unit-tests"]["permissions"] == {"contents": "read"}
    assert pull_request["permissions"] == {"contents": "read"}
    for job_name, job in _jobs(pull_request).items():
        permissions, _ = _effective_permissions(pull_request["permissions"], job)
        if job_name == "release-catalog-smoke":
            assert permissions == {"contents": "read", "packages": "write"}
        else:
            assert permissions == {"contents": "read"}, job_name


def test_sync_builders_has_discovery_dynamic_sync_and_catalog_contract() -> None:
    assert not (WORKFLOW_DIR / "_prepare-builders.yml").exists()
    workflow = _load_workflow(WORKFLOW_DIR / "sync-builders.yml")
    triggers = workflow["on"]
    assert triggers["schedule"] == [{"cron": "0 4 * * *"}]
    assert triggers["workflow_dispatch"] is None
    assert (
        triggers["workflow_call"]["outputs"]["builder_catalog_artifact"]["value"]
        == "${{ jobs.publish-builder-catalog.outputs.builder_catalog_artifact }}"
    )

    jobs = _jobs(workflow)
    assert list(jobs) == [
        "discover-project-builders",
        "read-existing-builder-pool",
        "compute-missing-builders",
        "build-missing-builders",
        "publish-builder-catalog",
        "builder-sync-success",
    ]
    discover_text = "\n".join(_strings(jobs["discover-project-builders"]))
    assert "ucm_release builders discover" in discover_text
    assert "builder-catalog.json" in discover_text

    read_text = "\n".join(_strings(jobs["read-existing-builder-pool"]))
    assert "target_repository" in read_text and "unique" in read_text
    assert "crane ls" in read_text
    assert "missing repository" in read_text.lower()

    compute = jobs["compute-missing-builders"]
    compute_text = "\n".join(_strings(compute))
    assert "ucm_release builders sync-plan" in compute_text
    assert set(compute["outputs"]) == {"has_missing", "matrix"}

    build = jobs["build-missing-builders"]
    assert (
        build["strategy"]["matrix"]
        == "${{ fromJSON(needs.compute-missing-builders.outputs.matrix) }}"
    )
    assert "matrix.cpu_arch" in build["runs-on"]
    build_text = "\n".join(_strings(build))
    assert "crane digest" in build_text
    assert build_text.count("crane copy") >= 2
    assert "Dockerfile.builder" in build_text
    assert "MOONCAKE_TAG" in build_text and "TARGET_TAG" in build_text
    assert "crane delete" not in "\n".join(_strings(workflow)).lower()
    assert "ucm-builder-vllm" not in build_text

    publish = jobs["publish-builder-catalog"]
    publish_text = "\n".join(_strings(publish))
    assert "crane digest" in publish_text
    assert "builder-catalog.json" in publish_text
    assert "always()" in publish["if"]
    assert "needs.build-missing-builders.result == 'skipped'" in publish["if"]
    assert publish["outputs"]["builder_catalog_artifact"] == (
        "${{ steps.publish.outputs.builder_catalog_artifact }}"
    )
    assert jobs["builder-sync-success"]["needs"] == [
        "discover-project-builders",
        "read-existing-builder-pool",
        "compute-missing-builders",
        "build-missing-builders",
        "publish-builder-catalog",
    ]


def test_builder_dockerfile_targets_official_manylinux_pool() -> None:
    dockerfile = (
        REPO_ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")
    assert "ARG CANN_BASE\n" in dockerfile
    assert "ARG MOONCAKE_TAG\n" in dockerfile
    assert "FROM ${CANN_BASE}" in dockerfile
    assert "yum" in dockerfile
    assert "mooncake_installer.sh -y" in dockerfile
    assert "sync-builders.yml" in dockerfile
    assert "_prepare-builders.yml" not in dockerfile
    assert "apt-get" not in dockerfile


def test_release_and_bot_consume_same_run_builder_catalog() -> None:
    release = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    assert "workflow_call" in release["on"]
    release_jobs = _jobs(release)
    sync = release_jobs["sync-builders"]
    assert sync["uses"] == "./.github/workflows/sync-builders.yml"
    assert sync["permissions"] == {"contents": "read", "packages": "write"}
    assert "sync-builders" in release_jobs["plan"]["needs"]
    release_plan_text = "\n".join(_strings(release_jobs["plan"]))
    assert "needs.sync-builders.outputs.builder_catalog_artifact" in release_plan_text
    assert "--builder-catalog input/builders/builder-catalog.json" in release_plan_text

    bot = _load_workflow(WORKFLOW_DIR / "ucm-build-bot.yml")
    bot_jobs = _jobs(bot)
    assert bot_jobs["sync-builders"]["needs"] == "permission-check"
    assert bot_jobs["sync-builders"]["uses"] == "./.github/workflows/sync-builders.yml"
    assert bot_jobs["sync-builders"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert set(bot_jobs["plan"]["needs"]) == {"permission-check", "sync-builders"}
    bot_plan_text = "\n".join(_strings(bot_jobs["plan"]))
    assert "needs.sync-builders.outputs.builder_catalog_artifact" in bot_plan_text
    assert "--builder-catalog input/builders/builder-catalog.json" in bot_plan_text
    assert "--pin-upstream" in bot_plan_text


def test_release_routes_use_full_matrices_and_develop_only_daily_dispatch() -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    jobs = _jobs(workflow)
    plan_text = "\n".join(_strings(jobs["plan"]))
    assert 'route="pr"' in plan_text
    assert 'route="daily"' in plan_text
    assert 'route="release"' in plan_text
    assert re.search(
        r'"\$\{EVENT_NAME\}" == workflow_dispatch\s+\) && \\\n'
        r'\s+"\$\{REF\}" == refs/heads/develop',
        plan_text,
    )
    assert 'build_wheel_matrix="${wheel_matrix}"' in plan_text
    assert 'build_image_matrix="${image_matrix}"' in plan_text
    assert 'build_wheel_matrix="${smoke_wheel_matrix}"' not in plan_text
    assert 'build_image_matrix="${smoke_image_matrix}"' not in plan_text
    assert "dry_run_val=true" in plan_text


def test_release_route_terminal_jobs_gate_every_required_stage() -> None:
    jobs = _jobs(_load_workflow(WORKFLOW_DIR / "release-ucm.yml"))
    terminals = {name for name in jobs if name.endswith("-loop-success")}
    assert terminals == {
        "pr-loop-success",
        "daily-loop-success",
        "release-loop-success",
    }
    expected_needs = {
        "pr-loop-success": {
            "plan",
            "build-wheels",
            "package-chart",
            "reconcile-images-feature",
        },
        "daily-loop-success": {
            "plan",
            "build-wheels",
            "package-chart",
            "reconcile-images-feature",
        },
        "release-loop-success": {
            "plan",
            "build-wheels",
            "package-chart",
            "reconcile-images-protected",
            "prepare-release-draft",
            "anonymous-registry-readback",
            "publish-release",
        },
    }
    routes = {
        "pr-loop-success": "pr",
        "daily-loop-success": "daily",
        "release-loop-success": "release",
    }
    for name, required in expected_needs.items():
        job = jobs[name]
        assert set(job["needs"]) == required
        condition = str(job["if"])
        assert "always()" in condition
        assert f"needs.plan.outputs.route == '{routes[name]}'" in condition
        for dependency in required:
            assert f"needs.{dependency}.result == 'success'" in condition


def test_protected_workflow_author_and_builder_permission_are_explicit() -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "pull-request.yml")
    jobs = _jobs(workflow)
    precheck_text = "\n".join(_strings(jobs["pre-check"]))
    assert '"SuperMarioYL"' in precheck_text
    assert jobs["release-catalog-smoke"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }


def test_release_tests_checkout_fetches_tags_for_git_describe() -> None:
    """Release tests need complete tag history because version loading calls git describe."""
    workflow = _load_workflow(WORKFLOW_DIR / "lint-and-test.yml")
    checkout = next(
        step
        for step in _steps(_jobs(workflow)["release-tests"])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["fetch-tags"] is True


@pytest.mark.parametrize(
    ("filename", "valid_environment", "invalid_environment"),
    [
        (
            "_build-wheel.yml",
            {
                "SOURCE_SHA": "a" * 40,
                "TASK_ID": "wheel-" + "1" * 64,
                "RESOLVED_PLAN_ARTIFACT": "ucm-resolved-plan-test",
                "RESOLVED_PLAN_SHA256": "sha256:" + "2" * 64,
            },
            [
                {"SOURCE_SHA": "refs/heads/feature/cicd"},
                {"TASK_ID": ""},
                {"TASK_ID": "image-" + "1" * 64},
                {"RESOLVED_PLAN_SHA256": "sha256:nope"},
            ],
        ),
        (
            "_build-image.yml",
            {
                "SOURCE_SHA": "b" * 40,
                "TASK_ID": "image-" + "3" * 64,
                "RESOLVED_PLAN_ARTIFACT": "ucm-resolved-plan-test",
                "RESOLVED_PLAN_SHA256": "sha256:" + "4" * 64,
            },
            [
                {"SOURCE_SHA": "refs/heads/feature/cicd"},
                {"TASK_ID": ""},
                {"TASK_ID": "wheel-" + "3" * 64},
                {"RESOLVED_PLAN_ARTIFACT": "bad/name"},
            ],
        ),
    ],
)
def test_reusable_build_contract_gate_runs_before_checkout_or_untrusted_code(
    tmp_path: Path,
    filename: str,
    valid_environment: dict[str, str],
    invalid_environment: list[dict[str, str]],
) -> None:
    """Malformed calls must fail before checkout, Actions, Python, or network."""
    workflow = _load_workflow(WORKFLOW_DIR / filename)
    steps = _steps(_jobs(workflow)["build"])
    gate = steps[0]
    assert gate.get("name") == "Validate reusable build contract"
    assert gate.get("shell") == "bash"
    assert "uses" not in gate
    command = str(gate.get("run", ""))
    assert "set -euo pipefail" in command
    assert "python" not in command.lower()
    assert "curl" not in command

    checkout_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    setup_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    first_repo_or_network_index = next(
        index
        for index, step in enumerate(steps)
        if index > setup_index
        and any(
            marker in "\n".join(_strings(step))
            for marker in ("python", "ucm_release", "curl", "download-artifact")
        )
    )
    assert 0 < checkout_index < setup_index < first_repo_or_network_index

    base_environment = {**__import__("os").environ, **valid_environment}
    marker = tmp_path / "later-step-ran"
    wrapped = command + '\nprintf later >"${MARKER}"\n'
    valid = subprocess.run(
        ["bash", "-c", wrapped],
        env={**base_environment, "MARKER": str(marker)},
        check=False,
    )
    assert valid.returncode == 0
    assert marker.read_text(encoding="utf-8") == "later"

    for index, mutation in enumerate(invalid_environment):
        marker = tmp_path / f"invalid-{index}"
        rejected = subprocess.run(
            ["bash", "-c", wrapped],
            env={
                **base_environment,
                **mutation,
                "MARKER": str(marker),
            },
            check=False,
        )
        assert rejected.returncode == 2
        assert not marker.exists()
