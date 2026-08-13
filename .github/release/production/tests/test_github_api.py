from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from ucm_release_production.common import ProductionError
from ucm_release_production.github_api import GitHubClient, read_trusted_identity

REPOSITORY = "OctoCat/unified-cache-management"
RUN_ID = 101
SOURCE = "1" * 40
CONTROL = "2" * 40


class FixtureTransport:
    def __init__(
        self, responses: Mapping[str, list[tuple[int, dict[str, str], bytes]]]
    ):
        self.responses = {key: list(value) for key, value in responses.items()}
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        self.requests.append((method, url, headers))
        if url not in self.responses or not self.responses[url]:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses[url].pop(0)


def _json(value: object) -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": "application/json"}, json.dumps(value).encode()


def _url(path: str) -> str:
    return f"https://api.github.com{path}"


def _responses() -> dict[str, list[tuple[int, dict[str, str], bytes]]]:
    repository = {
        "id": 42,
        "full_name": REPOSITORY,
        "default_branch": "develop",
        "owner": {"login": "OctoCat"},
    }
    run = {
        "id": RUN_ID,
        "run_attempt": 1,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SOURCE,
        "head_branch": "v0.6.0rc1",
        "path": ".github/workflows/production-tag-candidate.yml",
        "workflow_id": 77,
        "head_repository": {"id": 42, "full_name": REPOSITORY},
        "repository": {"id": 42, "full_name": REPOSITORY},
        "referenced_workflows": [
            {
                "path": f"{REPOSITORY}/.github/workflows/_production-build-wheel.yml@{SOURCE}",
                "sha": SOURCE,
                "ref": "refs/tags/v0.6.0rc1",
            },
            {
                "path": f"{REPOSITORY}/.github/workflows/_production-build-image.yml@{SOURCE}",
                "sha": SOURCE,
                "ref": "refs/tags/v0.6.0rc1",
            },
        ],
    }
    workflow = {
        "id": 77,
        "name": "UCM Production Tag Candidate",
        "path": ".github/workflows/production-tag-candidate.yml",
        "state": "active",
    }
    branch = {"ref": "refs/heads/develop", "object": {"type": "commit", "sha": CONTROL}}
    artifacts = {
        "total_count": 1,
        "artifacts": [
            {
                "id": 501,
                "name": "ucm-production-candidate-42-"
                + "3" * 40
                + f"-{SOURCE}-{RUN_ID}-1",
                "size_in_bytes": 1234,
                "expired": False,
                "workflow_run": {"id": RUN_ID, "head_sha": SOURCE},
                "archive_download_url": _url(
                    f"/repos/{REPOSITORY}/actions/artifacts/501/zip"
                ),
            }
        ],
    }
    return {
        _url(f"/repos/{REPOSITORY}"): [_json(repository), _json(repository)],
        _url(f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}"): [_json(run)],
        _url(f"/repos/{REPOSITORY}/actions/workflows/production-tag-candidate.yml"): [
            _json(workflow)
        ],
        _url(f"/repos/{REPOSITORY}/git/ref/heads/develop"): [
            _json(branch),
            _json(branch),
        ],
        _url(f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}/artifacts?per_page=100"): [
            _json(artifacts)
        ],
    }


def test_read_trusted_identity_closes_repo_run_workflow_branch_and_artifact() -> None:
    transport = FixtureTransport(_responses())
    client = GitHubClient(REPOSITORY, token="test-token", transport=transport)

    identity = read_trusted_identity(client, REPOSITORY, RUN_ID)

    assert identity["repository_id"] == 42
    assert identity["source_sha"] == SOURCE
    assert identity["control_sha"] == CONTROL
    assert identity["tag_name"] == "v0.6.0rc1"
    assert identity["run_attempt"] == 1
    assert identity["candidate_artifact"]["id"] == 501
    assert identity["sha256"]
    assert all(method == "GET" for method, _, _ in transport.requests)
    assert all(
        url.startswith(f"https://api.github.com/repos/{REPOSITORY}")
        for _, url, _ in transport.requests
    )
    assert all(
        request[2]["authorization"] == "Bearer test-token"
        for request in transport.requests
    )


@pytest.mark.parametrize(
    ("path", "mutation", "message"),
    [
        (
            f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}",
            lambda value: value.update(event="pull_request"),
            "event",
        ),
        (
            f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}",
            lambda value: value.update(conclusion="failure"),
            "conclusion",
        ),
        (
            f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}",
            lambda value: value.update(path=".github/workflows/evil.yml"),
            "path",
        ),
        (
            f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}",
            lambda value: value["head_repository"].update(full_name="evil/fork"),
            "repository",
        ),
        (
            f"/repos/{REPOSITORY}/actions/runs/{RUN_ID}",
            lambda value: value["referenced_workflows"][0].update(sha="9" * 40),
            "referenced",
        ),
        (
            f"/repos/{REPOSITORY}/actions/workflows/production-tag-candidate.yml",
            lambda value: value.update(id=88),
            "workflow",
        ),
    ],
)
def test_trusted_identity_rejects_event_repo_workflow_and_reference_drift(
    path: str, mutation: object, message: str
) -> None:
    responses = _responses()
    url = _url(path)
    status, headers, raw = responses[url][0]
    value = json.loads(raw)
    mutation(value)
    responses[url][0] = status, headers, json.dumps(value).encode()

    with pytest.raises(ProductionError, match=message):
        read_trusted_identity(
            GitHubClient(REPOSITORY, transport=FixtureTransport(responses)),
            REPOSITORY,
            RUN_ID,
        )


def test_trusted_identity_rejects_default_branch_double_read_drift() -> None:
    responses = _responses()
    url = _url(f"/repos/{REPOSITORY}/git/ref/heads/develop")
    responses[url][1] = _json(
        {"ref": "refs/heads/develop", "object": {"type": "commit", "sha": "9" * 40}}
    )

    with pytest.raises(ProductionError, match="double-read"):
        read_trusted_identity(
            GitHubClient(REPOSITORY, transport=FixtureTransport(responses)),
            REPOSITORY,
            RUN_ID,
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'{"id":1,"id":2}', "duplicate key"),
        (b'{"value":NaN}', "non-finite"),
        (b"[", "invalid"),
    ],
)
def test_client_rejects_ambiguous_json(raw: bytes, message: str) -> None:
    path = f"/repos/{REPOSITORY}/test"
    transport = FixtureTransport(
        {_url(path): [(200, {"content-type": "application/json"}, raw)]}
    )

    with pytest.raises(ProductionError, match=message):
        GitHubClient(REPOSITORY, transport=transport).request_json("GET", path)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_client_never_follows_redirects(status: int) -> None:
    path = f"/repos/{REPOSITORY}/test"
    transport = FixtureTransport(
        {_url(path): [(status, {"location": "https://evil.example/steal"}, b"")]}
    )

    with pytest.raises(ProductionError, match="redirect"):
        GitHubClient(REPOSITORY, transport=transport).request_json("GET", path)


def test_client_retries_only_bounded_transient_failures() -> None:
    path = f"/repos/{REPOSITORY}/test"
    transport = FixtureTransport(
        {
            _url(path): [
                (500, {"content-type": "application/json"}, b'{"message":"retry"}'),
                _json({"ok": True}),
            ]
        }
    )
    sleeps: list[float] = []

    result = GitHubClient(
        REPOSITORY, transport=transport, sleep=sleeps.append
    ).request_json("GET", path)

    assert result == {"ok": True}
    assert sleeps == [1.0]


def test_client_rejects_cross_repository_and_non_get_routes() -> None:
    client = GitHubClient(REPOSITORY, transport=FixtureTransport({}))

    with pytest.raises(ProductionError, match="current repository"):
        client.request_json("GET", "/repos/evil/fork/actions/runs/1")
    with pytest.raises(ProductionError, match="GET"):
        client.request_json("POST", f"/repos/{REPOSITORY}/releases")  # type: ignore[arg-type]


def test_client_rejects_unbounded_response_before_json_parse() -> None:
    path = f"/repos/{REPOSITORY}/test"
    transport = FixtureTransport(
        {
            _url(path): [
                (
                    200,
                    {"content-type": "application/json", "content-length": "9999999"},
                    b"{}",
                )
            ]
        }
    )

    with pytest.raises(ProductionError, match="size"):
        GitHubClient(REPOSITORY, transport=transport, max_bytes=1024).request_json(
            "GET", path
        )
