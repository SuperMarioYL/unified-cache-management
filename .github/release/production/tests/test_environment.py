from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from ucm_release_production.common import ProductionError
from ucm_release_production.environment import environment_evidence
from ucm_release_production.github_api import GitHubClient


REPOSITORY = "OctoCat/unified-cache-management"
SOURCE = "1" * 40


class FixtureTransport:
    def __init__(self, values: Mapping[str, object]) -> None:
        self.values = dict(values)

    def __call__(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        assert method == "GET"
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(self.values[url]).encode(),
        )


def _client(states: list[str]) -> GitHubClient:
    root = f"https://api.github.com/repos/{REPOSITORY}"
    return GitHubClient(
        REPOSITORY,
        transport=FixtureTransport(
            {
                root
                + f"/deployments?sha={'2' * 40}&environment=release-production&per_page=100": [
                    {
                        "id": 99,
                        "ref": "develop",
                        "sha": "2" * 40,
                        "task": "deploy",
                        "environment": "release-production",
                        "transient_environment": False,
                        "creator": {"login": "SuperMarioYL"},
                    }
                ],
                root
                + "/deployments/99/statuses?per_page=100": [
                    {"state": state} for state in states
                ],
            }
        ),
    )


def test_preview_environment_readback_binds_deployment_and_source() -> None:
    result = environment_evidence(
        _client(["in_progress", "queued", "waiting"]),
        repository=REPOSITORY,
        source_sha=SOURCE,
        control_sha="2" * 40,
        control_ref="develop",
        tag_name="v0.6.0rc1",
        environment="release-production",
        stage="rc",
    )

    assert result["status"] == "waived-for-preview"
    assert result["deployment_id"] == 99
    assert result["approval_actor"] == "SuperMarioYL"


def test_environment_without_started_approved_job_is_rejected() -> None:
    with pytest.raises(ProductionError, match="approved"):
        environment_evidence(
            _client(["waiting"]),
            repository=REPOSITORY,
            source_sha=SOURCE,
            control_sha="2" * 40,
            control_ref="develop",
            tag_name="v0.6.0rc1",
            environment="release-production",
            stage="rc",
        )
