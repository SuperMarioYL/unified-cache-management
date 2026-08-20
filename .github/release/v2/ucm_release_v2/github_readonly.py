"""Strict offline validation for read-only GitHub ref observations."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


class ReadOnlyGitHubError(ValueError):
    """Raised when GitHub readback is malformed, stale, or ambiguously identified."""


_BRANCH = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def validate_control_identity(
    *, configured_main: object, event_default_branch: object, event_ref_name: object
) -> str:
    """Prove scheduled control code came from the configured default branch."""
    if (
        not isinstance(configured_main, str)
        or not _BRANCH.fullmatch(configured_main)
        or configured_main != "main"
    ):
        raise ReadOnlyGitHubError("configured main branch is unsafe")
    if event_default_branch != configured_main or event_ref_name != configured_main:
        raise ReadOnlyGitHubError(
            "nightly control must run from the configured default branch"
        )
    return configured_main


def _observed_commit(value: object, *, branch: str, label: str) -> str:
    if not isinstance(value, dict):
        raise ReadOnlyGitHubError(f"{label} GitHub ref response must be an object")
    if value.get("ref") != f"refs/heads/{branch}":
        raise ReadOnlyGitHubError(f"{label} GitHub ref response does not match branch")
    target = value.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise ReadOnlyGitHubError(f"{label} GitHub ref target must be a commit")
    sha = target.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ReadOnlyGitHubError(f"{label} GitHub ref SHA is malformed")
    return sha


def validate_develop_reads(first: object, second: object, *, branch: str) -> str:
    """Return a commit SHA only after two identical develop-ref observations."""
    if not _BRANCH.fullmatch(branch) or branch != "develop":
        raise ReadOnlyGitHubError("develop branch identity is unsafe")
    first_sha = _observed_commit(first, branch=branch, label="first")
    second_sha = _observed_commit(second, branch=branch, label="second")
    if first_sha != second_sha:
        raise ReadOnlyGitHubError("develop ref changed between read-only observations")
    return first_sha


def validate_reusable_control_reads(
    first: object,
    second: object,
    *,
    configured_main: object,
    repository: object,
    allowed_repositories: tuple[str, ...],
    job_context: object,
) -> str:
    """Bind a called reusable controller to two exact default-main observations."""
    if configured_main != "main":
        raise ReadOnlyGitHubError("reusable control requires configured main")
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise ReadOnlyGitHubError("caller repository identity is malformed")
    if (
        not allowed_repositories
        or any(not _REPOSITORY.fullmatch(item) for item in allowed_repositories)
        or repository not in allowed_repositories
    ):
        raise ReadOnlyGitHubError("caller repository is outside the explicit allowlist")
    required_job_keys = {
        "workflow_file_path",
        "workflow_ref",
        "workflow_repository",
        "workflow_sha",
    }
    if not isinstance(job_context, dict) or not required_job_keys <= set(job_context):
        raise ReadOnlyGitHubError("called workflow job identity fields are incomplete")
    if any(not isinstance(job_context[key], str) for key in required_job_keys):
        raise ReadOnlyGitHubError("called workflow job identity values must be strings")
    job_workflow_ref = job_context["workflow_ref"]
    job_workflow_sha = job_context["workflow_sha"]
    job_workflow_repository = job_context["workflow_repository"]
    job_workflow_file_path = job_context["workflow_file_path"]
    expected_path = ".github/workflows/release-control-dry-run.yml"
    expected_ref = f"{repository}/{expected_path}@refs/heads/main"
    if job_workflow_repository != repository:
        raise ReadOnlyGitHubError(
            "called workflow repository is not the allowed repository"
        )
    if job_workflow_file_path != expected_path:
        raise ReadOnlyGitHubError("called workflow path is not the trusted controller")
    if job_workflow_ref != expected_ref:
        raise ReadOnlyGitHubError(
            "called workflow ref is not the exact main controller"
        )
    if not isinstance(job_workflow_sha, str) or not _SHA.fullmatch(job_workflow_sha):
        raise ReadOnlyGitHubError("called workflow SHA is malformed")
    first_sha = _observed_commit(first, branch="main", label="first")
    second_sha = _observed_commit(second, branch="main", label="second")
    if first_sha != second_sha:
        raise ReadOnlyGitHubError("main ref changed between read-only observations")
    if first_sha != job_workflow_sha:
        raise ReadOnlyGitHubError("called workflow SHA does not equal observed main")
    return first_sha


def validate_develop_workflow_run(
    *,
    workflow_name: object,
    workflow_event: object,
    workflow_path: object,
    conclusion: object,
    head_branch: object,
    head_repository: object,
    event_repository: object,
    head_sha: object,
) -> str:
    """Accept only a successful same-repository Push Commit Checks develop run."""
    if workflow_name != "Push Commit Checks":
        raise ReadOnlyGitHubError("develop controller requires Push Commit Checks")
    if workflow_event != "push":
        raise ReadOnlyGitHubError("develop source workflow event must be push")
    if workflow_path != ".github/workflows/push-check.yml@develop":
        raise ReadOnlyGitHubError(
            "develop source workflow path must be push-check at develop"
        )
    if conclusion != "success":
        raise ReadOnlyGitHubError("develop source workflow conclusion must be success")
    if head_branch != "develop":
        raise ReadOnlyGitHubError("develop source workflow head_branch must be develop")
    if (
        not isinstance(event_repository, str)
        or not _REPOSITORY.fullmatch(event_repository)
        or head_repository != event_repository
    ):
        raise ReadOnlyGitHubError(
            "develop source workflow must belong to the current repository"
        )
    if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
        raise ReadOnlyGitHubError("develop source head_sha is malformed")
    return head_sha


def load_json(path: Path, label: str) -> object:
    """Load one API response while rejecting duplicate keys at every level."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReadOnlyGitHubError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeError) as error:
        raise ReadOnlyGitHubError(f"cannot read {label}") from error
    except json.JSONDecodeError as error:
        raise ReadOnlyGitHubError(f"{label} must be valid JSON") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source_sha = validate_develop_reads(
            load_json(args.first, "first response"),
            load_json(args.second, "second response"),
            branch=args.branch,
        )
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"source_sha={source_sha}\n")
    except ReadOnlyGitHubError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
