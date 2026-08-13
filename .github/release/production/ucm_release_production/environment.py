"""Read back GitHub Actions Environment deployment evidence."""

from __future__ import annotations

from typing import Any

from .common import ProductionError, require_lower_commit_sha, sha256_envelope
from .github_api import GitHubClient


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProductionError(f"{label} must be an array")
    return value


def environment_evidence(
    client: GitHubClient,
    *,
    repository: str,
    source_sha: str,
    control_sha: str,
    control_ref: str,
    tag_name: str,
    environment: str,
    stage: str,
    minimum_deployment_id: int = 0,
) -> dict[str, Any]:
    """Require one current-run Environment deployment and its ordered status chain."""

    if repository != client.repository:
        raise ProductionError("environment repository differs from GitHub client")
    require_lower_commit_sha(source_sha, "environment source SHA")
    require_lower_commit_sha(control_sha, "environment control SHA")
    if not isinstance(control_ref, str) or not control_ref:
        raise ProductionError("production Environment control ref is invalid")
    if environment != "release-production":
        raise ProductionError("production environment name differs")
    if stage not in {"draft", "rc", "stable", "hotfix"}:
        raise ProductionError("production environment stage is invalid")
    if type(minimum_deployment_id) is not int or minimum_deployment_id < 0:
        raise ProductionError("minimum deployment id is invalid")
    deployments = _array(
        client.request_json(
            "GET",
            f"/repos/{repository}/deployments?sha={control_sha}&environment={environment}"
            "&per_page=100",
        ),
        "GitHub deployments",
    )
    matches = [
        item
        for item in deployments
        if isinstance(item, dict)
        and item.get("ref") == control_ref
        and item.get("sha") == control_sha
        and item.get("environment") == environment
        and item.get("task") == "deploy"
        and item.get("transient_environment") is False
        and type(item.get("id")) is int
        and item["id"] > minimum_deployment_id
    ]
    if not matches:
        raise ProductionError("current production Environment deployment is absent")
    deployment = max(matches, key=lambda item: int(item.get("id", 0)))
    deployment_id = deployment.get("id")
    if type(deployment_id) is not int or deployment_id < 1:
        raise ProductionError("production Environment deployment id is invalid")
    statuses = _array(
        client.request_json(
            "GET",
            f"/repos/{repository}/deployments/{deployment_id}/statuses?per_page=100",
        ),
        "GitHub deployment statuses",
    )
    if not statuses or not all(isinstance(item, dict) for item in statuses):
        raise ProductionError("production Environment statuses are absent")
    states = [item.get("state") for item in statuses]
    if "in_progress" not in states and "queued" not in states:
        raise ProductionError("production Environment did not enter an approved job")
    creator = deployment.get("creator")
    if not isinstance(creator, dict) or not isinstance(creator.get("login"), str):
        raise ProductionError("production Environment deployment creator is invalid")
    status = "waived-for-preview" if stage in {"draft", "rc"} else "passed"
    return sha256_envelope(
        {
            "kind": "ucm-production-environment-evidence",
            "schema_version": 1,
            "status": status,
            "source_sha": source_sha,
            "environment": environment,
            "deployment_id": deployment_id,
            # GitHub's deployment API exposes the deployment creator, not the
            # identity that clicked the protected-Environment approval button.
            "approval_actor": creator["login"],
        }
    )
