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
WORKFLOW_SUFFIXES = {".yml", ".yaml"}
SAFE_FORK_ACTIONS = {
    "actions/cache",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "azure/setup-helm",
    "docker/setup-buildx-action",
    "docker/setup-qemu-action",
    "sigstore/cosign-installer",
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
    actual = {path.name for path in _workflow_paths(workflow_dir)}
    expected = EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS
    if actual == expected:
        return []
    return [
        "workflow file set must be exactly "
        f"{sorted(expected)}, found {sorted(actual)}"
    ]


def _workflow_paths(workflow_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in workflow_dir.iterdir()
        if path.is_file() and path.suffix in WORKFLOW_SUFFIXES
    )


def _release_workflow_documents(workflow_dir: Path) -> dict[str, object]:
    """Audit expected release files and any unallowlisted workflow extension."""
    documents: dict[str, object] = {}
    for path in _workflow_paths(workflow_dir):
        if (
            path.name in EXPECTED_RELEASE_WORKFLOWS
            or path.name not in ALLOWED_NON_RELEASE_WORKFLOWS
        ):
            documents[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return documents


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
        label = "workflow-inherited write permission" if inherited else "write permission"
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

    documents = _release_workflow_documents(WORKFLOW_DIR)
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
    """An unrecognised YAML workflow cannot evade the four-workflow budget."""
    for filename in EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS:
        (tmp_path / filename).write_text("name: allowed\n")
    (tmp_path / "publish.yaml").write_text("name: bypass\n")

    violations = _workflow_set_violations(tmp_path)

    assert len(violations) == 1
    assert "publish.yaml" in violations[0]


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


def test_yaml_workflow_inherits_write_permissions_and_rejects_unknown_actions(
    tmp_path: Path,
) -> None:
    """Both permission inheritance and unknown action capability apply to .yaml."""
    (tmp_path / "publish.yaml").write_text(
        """
permissions: write-all
jobs:
  inherited-permission:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/create-release@v1
  job-permission:
    permissions:
      contents: write
    runs-on: ubuntu-24.04
    steps:
      - run: echo publish
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "copy.yaml").write_text(
        """
permissions:
  contents: write
jobs:
  inherited-map-permission:
    runs-on: ubuntu-24.04
    steps:
      - run: echo publish
""".lstrip(),
        encoding="utf-8",
    )

    violations = _fork_isolation_violations(_release_workflow_documents(tmp_path))

    assert len(violations) == 3
    assert any(
        "publish.yaml:inherited-permission" in violation
        and "workflow-inherited write permission" in violation
        and "unapproved action actions/create-release" in violation
        for violation in violations
    )
    assert any(
        "publish.yaml:job-permission" in violation and "write permission" in violation
        for violation in violations
    )
    assert any(
        "copy.yaml:inherited-map-permission" in violation
        and "workflow-inherited write permission" in violation
        for violation in violations
    )
