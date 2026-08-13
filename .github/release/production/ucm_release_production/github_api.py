"""Bounded, same-repository GitHub reads for the production trust gate."""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Literal

from .common import (
    ProductionError,
    decode_json,
    require_exact_keys,
    require_lower_commit_sha,
    require_string,
    sha256_envelope,
)

_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", re.ASCII
)
_TAG = re.compile(
    r"(?:draft/v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)-[1-9][0-9]*|v(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?)",
    re.ASCII,
)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", re.ASCII)
_ARTIFACT = re.compile(
    r"ucm-production-candidate-([1-9][0-9]*)-([0-9a-f]{40})-"
    r"([0-9a-f]{40})-([1-9][0-9]*)-([1-9][0-9]*)",
    re.ASCII,
)
_Transport = Callable[[str, str, dict[str, str]], tuple[int, dict[str, str], bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class GitHubClient:
    """A GET-only client constrained to one repository under api.github.com."""

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        transport: _Transport | None = None,
        max_bytes: int = 2 * 1024 * 1024,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
            raise ProductionError("GitHub repository must be one canonical owner/name")
        if token is not None and (not token or any(ord(char) < 33 for char in token)):
            raise ProductionError("GitHub token is malformed")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ProductionError("GitHub response size limit must be positive")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ProductionError("GitHub retry attempt limit must be between 1 and 5")
        self.repository = repository
        self.token = token
        self.max_bytes = max_bytes
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.transport = transport or self._urllib_transport

    def _urllib_transport(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, method=method, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read(self.max_bytes + 1)
                return response.status, dict(response.headers.items()), raw
        except urllib.error.HTTPError as error:
            raw = error.read(self.max_bytes + 1)
            return error.code, dict(error.headers.items()), raw
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProductionError(f"GitHub GET transport failed: {error}") from None

    def _path(self, path: str) -> str:
        prefix = f"/repos/{self.repository}"
        if (
            not isinstance(path, str)
            or not path.startswith(prefix)
            or (len(path) > len(prefix) and path[len(prefix)] not in {"/", "?"})
            or "#" in path
            or "\\" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(
                part in {"", ".", ".."} for part in path.split("?")[0].split("/")[1:]
            )
        ):
            raise ProductionError("GitHub GET path must target the current repository")
        return path

    def request_json(self, method: Literal["GET"], path: str) -> object:
        if method != "GET":
            raise ProductionError("trusted GitHub client accepts GET only")
        safe_path = self._path(path)
        url = f"https://api.github.com{safe_path}"
        headers = {
            "accept": "application/vnd.github+json",
            "user-agent": "ucm-production-release-controller/1",
            "x-github-api-version": "2022-11-28",
        }
        if self.token is not None:
            headers["authorization"] = f"Bearer {self.token}"
        for attempt in range(1, self.max_attempts + 1):
            status, response_headers, raw = self.transport("GET", url, dict(headers))
            normalized_headers = {
                str(key).lower(): str(value) for key, value in response_headers.items()
            }
            content_length = normalized_headers.get("content-length")
            if content_length is not None:
                if not content_length.isdecimal():
                    raise ProductionError("GitHub response Content-Length is invalid")
                if int(content_length) > self.max_bytes:
                    raise ProductionError("GitHub response exceeds the size limit")
            if len(raw) > self.max_bytes:
                raise ProductionError("GitHub response exceeds the size limit")
            if 300 <= status <= 399:
                raise ProductionError("GitHub GET redirect is forbidden")
            secondary = status == 403 and b"secondary rate limit" in raw.lower()
            transient = status == 429 or 500 <= status <= 599 or secondary
            if transient and attempt < self.max_attempts:
                self.sleep(float(2 ** (attempt - 1)))
                continue
            if not 200 <= status <= 299:
                raise ProductionError(f"GitHub GET returned HTTP {status}")
            content_type = normalized_headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type not in {
                "application/json",
                "application/vnd.github+json",
            } and not media_type.endswith("+json"):
                raise ProductionError("GitHub response is not JSON")
            return decode_json(raw, "GitHub response")
        raise ProductionError("GitHub GET exhausted bounded retries")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProductionError(f"{label} must be an array")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductionError(f"{label} must be a positive integer")
    return value


def _repository_identity(value: object, repository: str) -> tuple[int, str]:
    response = _object(value, "GitHub repository")
    repository_id = _positive(response.get("id"), "GitHub repository id")
    if response.get("full_name") != repository:
        raise ProductionError(
            "GitHub repository identity does not match current repository"
        )
    owner = _object(response.get("owner"), "GitHub repository owner")
    if owner.get("login") != repository.split("/", 1)[0]:
        raise ProductionError("GitHub repository owner identity differs")
    branch = require_string(response.get("default_branch"), "GitHub default branch")
    if (
        _BRANCH.fullmatch(branch) is None
        or branch.startswith("refs/")
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise ProductionError("GitHub default branch is not canonical")
    return repository_id, branch


def _nested_repository(
    value: object, label: str, repository: str, repository_id: int
) -> None:
    nested = _object(value, label)
    if nested.get("id") != repository_id or nested.get("full_name") != repository:
        raise ProductionError(f"{label} differs from current repository")


def _branch_sha(value: object, branch: str) -> str:
    response = _object(value, "default branch ref")
    if response.get("ref") != f"refs/heads/{branch}":
        raise ProductionError("default branch ref path differs")
    target = _object(response.get("object"), "default branch ref object")
    if target.get("type") != "commit":
        raise ProductionError("default branch ref must target a commit")
    return require_lower_commit_sha(target.get("sha"), "default branch head")


def read_trusted_identity(
    client: GitHubClient, repository: str, run_id: int
) -> dict[str, Any]:
    """Close a successful candidate run against current repository control code."""

    if repository != client.repository:
        raise ProductionError(
            "requested repository differs from GitHub client repository"
        )
    run_id = _positive(run_id, "candidate run id")
    repo_path = f"/repos/{repository}"
    first_repo = client.request_json("GET", repo_path)
    second_repo = client.request_json("GET", repo_path)
    first_identity = _repository_identity(first_repo, repository)
    second_identity = _repository_identity(second_repo, repository)
    if first_repo != second_repo or first_identity != second_identity:
        raise ProductionError(
            "repository default identity double-read values do not match"
        )
    repository_id, default_branch = first_identity

    run = _object(
        client.request_json("GET", f"/repos/{repository}/actions/runs/{run_id}"),
        "candidate workflow run",
    )
    if run.get("id") != run_id:
        raise ProductionError("candidate run id differs")
    run_attempt = _positive(run.get("run_attempt"), "candidate run attempt")
    if run.get("event") != "push":
        raise ProductionError("candidate run event must be push")
    if run.get("status") != "completed":
        raise ProductionError("candidate run status must be completed")
    if run.get("conclusion") != "success":
        raise ProductionError("candidate run conclusion must be success")
    if run.get("path") != ".github/workflows/production-tag-candidate.yml":
        raise ProductionError("candidate run workflow path differs")
    workflow_id = _positive(run.get("workflow_id"), "candidate workflow id")
    source_sha = require_lower_commit_sha(run.get("head_sha"), "candidate source SHA")
    tag_name = require_string(run.get("head_branch"), "candidate tag name")
    if _TAG.fullmatch(tag_name) is None:
        raise ProductionError("candidate tag name is not canonical")
    _nested_repository(
        run.get("repository"), "candidate run repository", repository, repository_id
    )
    _nested_repository(
        run.get("head_repository"),
        "candidate head repository",
        repository,
        repository_id,
    )

    workflow = _object(
        client.request_json(
            "GET",
            f"/repos/{repository}/actions/workflows/production-tag-candidate.yml",
        ),
        "candidate workflow",
    )
    if (
        workflow.get("id") != workflow_id
        or workflow.get("name") != "UCM Production Tag Candidate"
        or workflow.get("path") != ".github/workflows/production-tag-candidate.yml"
        or workflow.get("state") != "active"
    ):
        raise ProductionError(
            "candidate workflow identity differs from trusted contract"
        )

    expected_references = {
        (
            f"{repository}/.github/workflows/_production-build-wheel.yml@{source_sha}",
            source_sha,
            f"refs/tags/{tag_name}",
        ),
        (
            f"{repository}/.github/workflows/_production-build-image.yml@{source_sha}",
            source_sha,
            f"refs/tags/{tag_name}",
        ),
    }
    references = _array(
        run.get("referenced_workflows"), "candidate referenced workflows"
    )
    observed_references: set[tuple[object, object, object]] = set()
    for item in references:
        reference = _object(item, "candidate referenced workflow")
        require_exact_keys(
            reference, {"path", "sha", "ref"}, "candidate referenced workflow"
        )
        observed_references.add((reference["path"], reference["sha"], reference["ref"]))
    if observed_references != expected_references:
        raise ProductionError("candidate referenced workflow identity differs")

    branch_path = f"/repos/{repository}/git/ref/heads/{default_branch}"
    first_branch = _branch_sha(client.request_json("GET", branch_path), default_branch)
    second_branch = _branch_sha(client.request_json("GET", branch_path), default_branch)
    if first_branch != second_branch:
        raise ProductionError("default branch double-read values do not match")

    artifacts_response = _object(
        client.request_json(
            "GET", f"/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
        ),
        "candidate artifacts response",
    )
    artifacts = _array(artifacts_response.get("artifacts"), "candidate artifacts")
    if artifacts_response.get("total_count") != 1 or len(artifacts) != 1:
        raise ProductionError("candidate run must contain exactly one artifact")
    artifact = _object(artifacts[0], "candidate artifact")
    artifact_id = _positive(artifact.get("id"), "candidate artifact id")
    if artifact.get("expired") is not False:
        raise ProductionError("candidate artifact is expired")
    size = _positive(artifact.get("size_in_bytes"), "candidate artifact size")
    artifact_name = require_string(artifact.get("name"), "candidate artifact name")
    match = _ARTIFACT.fullmatch(artifact_name)
    if match is None:
        raise ProductionError("candidate artifact name is not identity-bound")
    (
        artifact_repository_id,
        tag_object_sha,
        artifact_source,
        artifact_run,
        artifact_attempt,
    ) = match.groups()
    if (
        int(artifact_repository_id) != repository_id
        or artifact_source != source_sha
        or int(artifact_run) != run_id
        or int(artifact_attempt) != run_attempt
    ):
        raise ProductionError("candidate artifact name identity differs")
    artifact_run_value = _object(
        artifact.get("workflow_run"), "candidate artifact workflow run"
    )
    if (
        artifact_run_value.get("id") != run_id
        or artifact_run_value.get("head_sha") != source_sha
    ):
        raise ProductionError("candidate artifact workflow run binding differs")
    download_url = artifact.get("archive_download_url")
    expected_download = (
        f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}/zip"
    )
    if download_url != expected_download:
        raise ProductionError(
            "candidate artifact download URL differs from current repository"
        )

    return sha256_envelope(
        {
            "kind": "ucm-production-trusted-run-identity",
            "schema_version": 1,
            "repository": repository,
            "repository_id": repository_id,
            "default_branch": default_branch,
            "control_sha": first_branch,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "source_sha": source_sha,
            "tag_name": tag_name,
            "tag_object_sha": tag_object_sha,
            "referenced_workflows": sorted(
                [
                    {"path": path, "sha": sha, "ref": ref}
                    for path, sha, ref in observed_references
                ],
                key=lambda item: item["path"],
            ),
            "candidate_artifact": {
                "id": artifact_id,
                "name": artifact_name,
                "size_in_bytes": size,
                "archive_download_url": download_url,
            },
        }
    )
