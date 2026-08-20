"""Workflow safety-invariant contract for the slim release lane.

Only the cross-cutting safety invariants are retained: fork-isolation
auditing and the reusable-build contract gate that runs before any
untrusted code or network access.  Structural YAML change-detector tests
(exact job names, step order, input sets, artifact names) were removed per
the slimming plan -- they asserted "we wrote what we wrote", not behaviour.
"""

from __future__ import annotations

import os
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


def _named_step(job: dict[str, object], name: str) -> dict[str, object]:
    return next(step for step in _steps(job) if step.get("name") == name)


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
    assert workflow["concurrency"] == {
        "group": "ucm-builder-sync-${{ github.repository_id }}",
        "cancel-in-progress": False,
    }

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
    assert "MANIFEST_UNKNOWN" in build_text and "NAME_UNKNOWN" in build_text
    assert "crane delete" not in "\n".join(_strings(workflow)).lower()
    assert "ucm-builder-vllm" not in build_text

    publish = jobs["publish-builder-catalog"]
    publish_text = "\n".join(_strings(publish))
    assert "crane digest" in publish_text
    assert 'crane config --platform "linux/${cpu_arch}"' in publish_text
    assert '.os == "linux" and .architecture == $architecture' in publish_text
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
    assert (
        "ARG CANN_BASE=registry.invalid/ucm/required-cann-base:invalid\n" in dockerfile
    )
    assert "ARG MOONCAKE_TAG\n" in dockerfile
    assert "FROM ${CANN_BASE}" in dockerfile
    assert "yum" in dockerfile
    assert "mooncake_installer.sh -y" in dockerfile
    assert "sync-builders.yml" in dockerfile
    assert "_prepare-builders.yml" not in dockerfile
    assert "apt-get" not in dockerfile


def test_release_dockerfiles_split_wheel_and_runtime_responsibilities() -> None:
    docker_root = REPO_ROOT / ".github" / "release" / "docker"
    wheel_path = docker_root / "Dockerfile.wheel"
    runtime_path = docker_root / "Dockerfile.runtime"

    assert not (docker_root / "Dockerfile").exists()
    assert wheel_path.is_file()
    assert runtime_path.is_file()
    assert (docker_root / "Dockerfile.builder").is_file()

    wheel_source = wheel_path.read_text(encoding="utf-8")
    stages = re.findall(r"^FROM .+ AS ([a-z0-9-]+)$", wheel_source, re.MULTILINE)
    assert stages == [
        "wheel-builder",
        "wheel-python",
        "wheel-source",
        "wheel-config",
        "wheel-build",
        "wheel",
    ]
    assert "FROM scratch AS wheel" in wheel_source
    assert set(re.findall(r"^ARG ([A-Z0-9_]+)", wheel_source, re.MULTILINE)) <= {
        "LD_LIBRARY_PATH",
        "UCM_BUILDER_IMAGE",
        "UCM_BUILD_CONFIG",
    }
    for forbidden in (
        "UCM_RELEASE_",
        "ARG PLATFORM",
        "ARG SOURCE_DATE_EPOCH",
        "ARG UCM_DIST_NAME",
        "re.subn",
        'pyproject.toml"); t =',
    ):
        assert forbidden not in wheel_source
    assert "wheel prepare-source" in wheel_source

    runtime_source = runtime_path.read_text(encoding="utf-8")
    runtime_stages = re.findall(
        r"^FROM .+ AS ([a-z0-9-]+)$", runtime_source, re.MULTILINE
    )
    assert runtime_stages == [
        "runtime-install",
        "runtime",
        "runtime-real-install",
        "runtime-real",
    ]
    assert "--target runtime-real" not in runtime_source


def test_wheel_environment_check_resolves_the_pinned_cmake_first() -> None:
    source = (
        REPO_ROOT / ".github" / "release" / "docker" / "Dockerfile.wheel"
    ).read_text(encoding="utf-8")
    wheel_config = source.split("FROM wheel-source AS wheel-config\n", 1)[1].split(
        "FROM wheel-config AS wheel-build\n", 1
    )[0]
    scripts_dir = (
        "scripts_dir=\"$(ucm-python -c 'import sysconfig; "
        'print(sysconfig.get_path("scripts"))\')";'
    )
    require_cmake = 'test -x "${scripts_dir}/cmake";'
    export_path = 'export PATH="${scripts_dir}:${PATH}";'
    resolve_cmake = 'test "$(command -v cmake)" = "${scripts_dir}/cmake";'
    check_environment = "wheel check-environment"

    assert wheel_config.count(scripts_dir) == 1
    assert (
        wheel_config.index(scripts_dir)
        < wheel_config.index(require_cmake)
        < wheel_config.index(export_path)
        < wheel_config.index(resolve_cmake)
        < wheel_config.index(check_environment)
    )


def test_wheel_workflow_materializes_config_before_buildx_with_two_build_args() -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    steps = _steps(_jobs(workflow)["build"])
    names = [step.get("name") for step in steps]
    authority_name = "Materialize canonical wheel authority and build config"
    authority_index = names.index(authority_name)
    wheelhouse_index = names.index(
        "Materialize the task-bound offline build tool wheelhouse"
    )
    build_index = names.index("Build, seal, and export the real native wheel")
    assert wheelhouse_index < authority_index < build_index

    task_source = str(
        _named_step(
            _jobs(workflow)["build"],
            "Select frozen wheel task and derive canonical build inputs",
        )["run"]
    )
    assert "build_args:" not in task_source

    authority_source = str(steps[authority_index]["run"])
    for fragment in (
        "wheel authority",
        "--task-file out/selected-task.json",
        "--builder-coordinate",
        "--wheelhouse out/source-context/build-wheels",
        "--source-archive out/source-context/ucm-source.tar",
        "--source-root",
        "--output out/source-context/build-authority.json",
        "wheel build-config",
        "--authority-file out/source-context/build-authority.json",
        "--output out/source-context/wheel-build.json",
    ):
        assert fragment in authority_source

    build_source = str(steps[build_index]["run"])
    assert "-f .github/release/docker/Dockerfile.wheel" in build_source
    assert "--target wheel" in build_source
    assert "UCM_BUILDER_IMAGE=${builder_coordinate}" in build_source
    assert "UCM_BUILD_CONFIG=wheel-build.json" in build_source
    assert ".build_args" not in build_source
    assert "UCM_RELEASE_" not in build_source

    record_source = str(
        _named_step(
            _jobs(workflow)["build"],
            "Reopen the sealed wheel and expose immutable identities",
        )["run"]
    )
    assert "build-authority.json" in record_source
    assert "wheel-build.json" in record_source


def test_image_workflow_hashes_runtime_source_but_keeps_context_filename() -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    source = "\n".join(_strings(workflow))
    runtime_path = ".github/release/docker/Dockerfile.runtime"

    assert f"sed -n '1s/^# syntax=//p' {runtime_path}" in source
    assert f"cp {runtime_path} context/Dockerfile" in source
    assert source.count(f"sha256sum {runtime_path}") == 2
    assert 'files:{"Dockerfile":$df' in source
    assert "--target runtime-real" in source
    assert ".github/release/docker/Dockerfile |" not in source


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


def test_terminal_jobs_run_for_their_event_without_result_dependent_if() -> None:
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
    applicability = {
        "pr-loop-success": "github.event_name == 'pull_request'",
        "daily-loop-success": "github.ref == 'refs/heads/develop'",
        "release-loop-success": "startsWith(github.ref, 'refs/tags/v')",
    }
    for name, required in expected_needs.items():
        job = jobs[name]
        assert set(job["needs"]) == required
        condition = str(job["if"])
        assert "always()" in condition
        assert applicability[name] in condition
        assert "needs." not in condition


@pytest.mark.parametrize(
    ("filename", "job_name", "allowed_skipped"),
    [
        ("release-ucm.yml", "pr-loop-success", set()),
        ("release-ucm.yml", "daily-loop-success", set()),
        ("release-ucm.yml", "release-loop-success", set()),
        ("sync-builders.yml", "builder-sync-success", {"BUILD_MISSING_RESULT"}),
    ],
)
def test_terminal_shell_fails_closed_for_failed_or_skipped_prerequisites(
    filename: str, job_name: str, allowed_skipped: set[str]
) -> None:
    job = _jobs(_load_workflow(WORKFLOW_DIR / filename))[job_name]
    assert "always()" in str(job["if"])
    assert "needs." not in str(job["if"])
    step = _steps(job)[0]
    command = str(step["run"])
    expressions = step["env"]
    assert isinstance(expressions, dict) and expressions
    valid = {str(name): "success" for name in expressions}

    passed = subprocess.run(
        ["bash", "-c", command], env={**os.environ, **valid}, check=False
    )
    assert passed.returncode == 0

    for name in valid:
        for result in ("failed", "skipped"):
            if name in allowed_skipped and result == "skipped":
                continue
            rejected = subprocess.run(
                ["bash", "-c", command],
                env={**os.environ, **valid, name: result},
                check=False,
            )
            assert rejected.returncode != 0, (job_name, name, result)

    for name in allowed_skipped:
        accepted = subprocess.run(
            ["bash", "-c", command],
            env={**os.environ, **valid, name: "skipped"},
            check=False,
        )
        assert accepted.returncode == 0


def test_protected_workflow_uses_pr_author_case_insensitively(
    tmp_path: Path,
) -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "pull-request.yml")
    jobs = _jobs(workflow)
    permission = next(
        step
        for step in _steps(jobs["pre-check"])
        if step.get("id") == "permission-check"
    )
    assert permission["env"]["PR_AUTHOR"] == (
        "${{ github.event.pull_request.user.login }}"
    )
    assert "ACTOR" not in permission["env"]
    command = str(permission["run"])
    assert '"SuperMarioYL"' in command

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Workflow Test"],
        cwd=repository,
        check=True,
    )
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    protected = repository / ".github" / "workflows" / "changed.yml"
    protected.parent.mkdir(parents=True)
    protected.write_text("name: changed\n", encoding="utf-8")
    subprocess.run(["git", "add", str(protected)], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "change workflow"], cwd=repository, check=True
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", base],
        cwd=repository,
        check=True,
    )
    base_env = {
        **os.environ,
        "BASE_REF": "main",
        "HEAD_BRANCH_NAME": "HEAD",
    }
    author_allowed = subprocess.run(
        ["bash", "-c", command],
        cwd=repository,
        env={**base_env, "PR_AUTHOR": "supermarioyl", "ACTOR": "outsider"},
        check=False,
    )
    actor_not_author = subprocess.run(
        ["bash", "-c", command],
        cwd=repository,
        env={**base_env, "PR_AUTHOR": "outsider", "ACTOR": "SuperMarioYL"},
        check=False,
    )
    assert author_allowed.returncode == 0
    assert actor_not_author.returncode != 0
    assert jobs["release-catalog-smoke"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }


def test_builder_recheck_rejects_transient_registry_errors_before_write(
    tmp_path: Path,
) -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "sync-builders.yml")
    build = _jobs(workflow)["build-missing-builders"]
    step = _named_step(build, "Build or copy the missing builder only")
    command = str(step["run"])
    binary = tmp_path / "crane"
    marker = tmp_path / "write-attempted"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = digest ]; then echo 'unauthorized: transient' >&2; exit 1; fi\n"
        'if [ "$1" = copy ]; then : >"$WRITE_MARKER"; exit 0; fi\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "WRITE_MARKER": str(marker),
            "BUILD_MODE": "mirror",
            "SOURCE_IMAGE": "docker.io/example/source:tag",
            "TARGET_REPOSITORY": "ghcr.io/example/target",
            "TARGET_TAG": "tag",
        },
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_catalog_verification_rejects_platform_config_mismatch(tmp_path: Path) -> None:
    workflow = _load_workflow(WORKFLOW_DIR / "sync-builders.yml")
    publish = _jobs(workflow)["publish-builder-catalog"]
    step = _named_step(publish, "Verify every catalog target is readable")
    command = str(step["run"])
    catalog_dir = tmp_path / "input" / "discovery"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "builder-catalog.json").write_text(
        '{"builders":[{"target_repository":"ghcr.io/example/target",'
        '"target_tag":"tag","cpu_arch":"amd64"}]}\n',
        encoding="utf-8",
    )
    crane = tmp_path / "crane"
    crane.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  digest) echo sha256:abc ;;\n"
        '  config) printf \'{"os":"%s","architecture":"%s"}\\n\' '
        '"$CRANE_CONFIG_OS" "$CRANE_CONFIG_ARCH" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CRANE_CONFIG_OS": "linux",
        "CRANE_CONFIG_ARCH": "amd64",
    }
    matched = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, env=environment, check=False
    )
    mismatched = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env={**environment, "CRANE_CONFIG_ARCH": "arm64"},
        check=False,
    )
    assert matched.returncode == 0
    assert mismatched.returncode != 0


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
