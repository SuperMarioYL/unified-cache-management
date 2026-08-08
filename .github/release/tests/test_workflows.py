"""RED workflow and staging-safety contract for the slim release lane."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_RELEASE_WORKFLOWS = {
    "_build-image.yml",
    "_build-wheel.yml",
    "release-ucm.yml",
    "release-vllm-images.yml",
}
ALLOWED_NON_RELEASE_WORKFLOWS = {
    "lint-and-test.yml",
    "pull-request.yml",
    "push-check.yml",
}
FORBIDDEN_STAGED_PATHS = {
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.cc",
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.h",
    "ucm/store/compress/cc/compressor_action.cc",
}


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8"
    )


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _strings(nested)]
    return [str(value)]


def _workflow_set_violations(workflow_dir: Path) -> list[str]:
    actual = {path.name for path in workflow_dir.glob("*.yml")}
    expected = EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS
    if actual == expected:
        return []
    return [
        "workflow file set must be exactly "
        f"{sorted(expected)}, found {sorted(actual)}"
    ]


def _has_upstream_guard(job: dict[object, object]) -> bool:
    condition = str(job.get("if", ""))
    return bool(
        re.search(
            r"github\.repository\s*==\s*['\"]ModelEngine-Group/unified-cache-management['\"]",
            condition,
        )
    )


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() == "true"


def _dangerous_job_operations(job: dict[object, object]) -> list[str]:
    """Return publication-capable operations that must be upstream-gated."""
    operations: list[str] = []
    if job.get("secrets") == "inherit":
        operations.append("secrets: inherit")
    permissions = job.get("permissions")
    if isinstance(permissions, dict) and any(
        str(value) == "write" for value in permissions.values()
    ):
        operations.append("write permission")
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

    for step in job.get("steps", []) if isinstance(job.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        action = str(step.get("uses", "")).lower()
        inputs = step.get("with", {})
        if "docker/login-action" in action:
            operations.append("registry login action")
        if "docker/build-push-action" in action and isinstance(inputs, dict) and _truthy(
            inputs.get("push")
        ):
            operations.append("container publishing action")
        if any(
            marker in action
            for marker in (
                "softprops/action-gh-release",
                "ncipollo/release-action",
                "peter-evans/repository-dispatch",
            )
        ):
            operations.append("publishing or dispatch action")
    return sorted(set(operations))


def _fork_isolation_violations(documents: dict[str, object]) -> list[str]:
    """Audit entry and locally reusable release workflows for a fork path escape."""
    violations: list[str] = []
    for filename, document in documents.items():
        if not isinstance(document, dict):
            continue
        jobs = document.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            operations = _dangerous_job_operations(job)
            if operations and not _has_upstream_guard(job):
                violations.append(
                    f"{filename}:{job_name} exposes fork candidates to "
                    f"{', '.join(operations)} without an upstream repository guard"
                )
    return violations


def test_release_workflows_are_compact_and_fork_candidate_is_read_only() -> None:
    """Demand a closed workflow set and no fork-to-publish escape path."""
    violations = _workflow_set_violations(WORKFLOW_DIR)

    entrypoint = WORKFLOW_DIR / "release-ucm.yml"
    document = yaml.safe_load(entrypoint.read_text(encoding="utf-8")) if entrypoint.exists() else {}
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    candidate = jobs.get("fork-candidate") if isinstance(jobs, dict) else None
    if not isinstance(candidate, dict):
        violations.append("release-ucm.yml must define a fork-candidate job")
    else:
        if candidate.get("permissions") != {"contents": "read"}:
            violations.append(
                "fork-candidate permissions must be exactly {'contents': 'read'}"
            )
        candidate_text = "\n".join(_strings(candidate)).lower()
        if "environment" in candidate:
            violations.append("fork-candidate must not use protected environments")
        banned_fragments = {
            "secrets.": "secrets",
            "self-hosted": "self-hosted runners",
        }
        for fragment, label in banned_fragments.items():
            if fragment in candidate_text:
                violations.append(f"fork-candidate must not use {label}")
        if re.search(r"\b(?:docker|crane)\s+(?:login|push)\b", candidate_text):
            violations.append("fork-candidate must not log in to or push a container registry")
        if re.search(r"\bgh\s+api\b.*\bdispatch", candidate_text):
            violations.append("fork-candidate must not dispatch workflows")

    documents = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in WORKFLOW_DIR.glob("*.yml")
        if path.name in EXPECTED_RELEASE_WORKFLOWS
    }
    violations.extend(_fork_isolation_violations(documents))

    assert not violations, "release workflow safety contract failed:\n- " + "\n- ".join(
        violations
    )


def test_existing_cpp_changes_are_explicitly_forbidden_from_the_stage() -> None:
    """Keep the three pre-existing C++ edits visible but outside this release commit."""
    assert all((REPO_ROOT / path).is_file() for path in FORBIDDEN_STAGED_PATHS)
    staged = set(filter(None, _git("diff", "--cached", "--name-only").splitlines()))
    assert not staged & FORBIDDEN_STAGED_PATHS, json.dumps(
        {"forbidden_staged_paths": sorted(staged & FORBIDDEN_STAGED_PATHS)}, indent=2
    )


def test_workflow_set_rejects_an_arbitrary_publish_workflow(tmp_path: Path) -> None:
    """An unrecognised workflow file cannot evade the four-workflow budget."""
    for filename in EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS:
        (tmp_path / filename).write_text("name: allowed\n")
    (tmp_path / "publish.yml").write_text("name: bypass\n")

    violations = _workflow_set_violations(tmp_path)

    assert len(violations) == 1
    assert "publish.yml" in violations[0]


def test_fork_isolation_rejects_reusable_workflow_publish_mutations() -> None:
    """Reusable workflow mutations must be rejected even when entry job is clean."""
    documents = {
        "release-ucm.yml": {
            "jobs": {
                "fork-candidate": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [{"run": "python -m ucm_release core plan"}],
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
        "registry login action",
        "container publishing action",
        "publishing or dispatch action",
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
