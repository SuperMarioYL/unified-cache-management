"""Security and data-flow contracts for documentation translation workflows."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
GATE_PATH = WORKFLOW_ROOT / "docs-translation-gate.yml"
DELIVERY_PATH = WORKFLOW_ROOT / "docs-translation-deliver.yml"
GENERATE_PATH = WORKFLOW_ROOT / "docs-translation-generate.yml"


def _workflow(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    # YAML 1.1 parses the key `on` as True. Normalize it for PyYAML.
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _all_text(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=True)


def _trigger(workflow: dict[str, Any], name: str) -> object:
    triggers = workflow["on"]
    if isinstance(triggers, str):
        assert triggers == name
        return {}
    if isinstance(triggers, list):
        assert name in triggers
        return {}
    assert isinstance(triggers, dict)
    assert name in triggers
    return triggers[name] or {}


def test_required_gate_has_one_stable_name_and_no_path_filter() -> None:
    workflow = _workflow(GATE_PATH)
    pull_request = _trigger(workflow, "pull_request")

    assert not isinstance(pull_request, dict) or "paths" not in pull_request
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert set(workflow["jobs"]) == {"sync"}
    sync = workflow["jobs"]["sync"]
    assert sync["name"] == "Docs translation synchronized"
    assert sync["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }

    text = _all_text(workflow)
    assert "secrets." not in text
    assert "DOCS_TRANSLATION_API_KEY" not in text
    assert "DOCS_TRANSLATION_APP_PRIVATE_KEY" not in text
    assert "translate check" in text
    assert "--base-ref" in text
    assert "pull_request_target" not in text


def test_required_gate_checks_the_actual_pr_head_and_handles_no_docs_delta() -> None:
    workflow = _workflow(GATE_PATH)
    sync = workflow["jobs"]["sync"]
    text = _all_text(sync)

    checkout_steps = [step for step in sync["steps"] if "uses" in step]
    assert any(step["uses"].startswith("actions/checkout@") for step in checkout_steps)
    assert any(step.get("with", {}).get("fetch-depth") == 0 for step in checkout_steps)
    assert "docs-next/docs/en" in text
    assert "docs-next/docs/zh" in text
    assert "pulls.listFiles" in text
    assert "pull_request.head.sha" in text
    assert ".github/aw/stage-pr-docs.mjs" in text
    # The required check must be emitted for every PR. Its script, rather than
    # an event path filter or job-level condition, decides whether docs changed.
    assert "if:" not in yaml.safe_dump(
        {key: value for key, value in sync.items() if key != "steps"},
        sort_keys=True,
    )


def test_one_http_generator_keeps_credentials_off_untrusted_code() -> None:
    workflow = _workflow(GENERATE_PATH)
    _trigger(workflow, "pull_request_target")
    _trigger(workflow, "workflow_dispatch")
    text = _all_text(workflow)
    assert "DOCS_TRANSLATION_API_FORMAT" in text
    assert "DOCS_TRANSLATION_API_KEY" in text
    assert "DOCS_TRANSLATION_APP_PRIVATE_KEY" not in text
    assert "contents: write" not in text
    assert "pull_request.base.sha" in text
    assert "persist-credentials: false" in text
    assert "stage-pr-docs.mjs" in text
    assert "translate prepare" in text and "translate generate" in text
    assert "translate finalize" in text
    assert (
        workflow["jobs"]["generate"]["if"]
        == "${{ vars.DOCS_TRANSLATION_API_FORMAT != '' }}"
    )
    steps = workflow["jobs"]["generate"]["steps"]
    model_steps = [
        step for step in steps if "DOCS_TRANSLATION_API_KEY" in step.get("env", {})
    ]
    assert len(model_steps) == 1
    assert model_steps[0]["if"] == "steps.prepare.outputs.has_work == 'true'"
    assert "secrets." not in _all_text(_workflow(GATE_PATH))
    assert not list(WORKFLOW_ROOT.glob("docs-translate-*.lock.yml"))
    assert not list(WORKFLOW_ROOT.glob("docs-translate-*.md"))


def test_manual_backfill_is_bounded_and_updates_one_bot_branch() -> None:
    source = GENERATE_PATH.read_text()
    assert "--missing" in source and "--max-pages 5" in source
    delivery = _workflow(DELIVERY_PATH)
    backfill = delivery["jobs"]["backfill"]
    text = _all_text(backfill)
    assert "needs.inspect.outputs.mode == 'missing'" in backfill["if"]
    assert "automation/docs-translation" in text
    assert "--force" not in text


def test_delivery_is_credential_separated_and_handles_same_repo_and_fork() -> None:
    workflow = _workflow(DELIVERY_PATH)
    _trigger(workflow, "workflow_run")
    text = _all_text(workflow)

    assert workflow["permissions"] == {"contents": "read"}
    assert "DOCS_TRANSLATION_API_KEY" not in text
    assert "DOCS_TRANSLATION_APP_ID" in text
    assert "DOCS_TRANSLATION_APP_PRIVATE_KEY" in text
    assert "translate finalize" in text
    assert "--task-dir raw/task" in text
    assert "--agent-output-dir raw/agent-output" in text
    assert "translate apply" in text
    assert "translation-artifact.json" in text
    assert "translation.patch" in text
    assert "<!-- ucm-docs-translation -->" in text
    assert "head_repository_id" in text
    assert "base_repository_id" in text
    assert "git push" in text
    assert "--force" not in text


def test_delivery_never_combines_model_and_repository_write_credentials() -> None:
    delivery = _workflow(DELIVERY_PATH)
    for job_name, job in delivery["jobs"].items():
        text = _all_text(job)
        assert not (
            "DOCS_TRANSLATION_API_KEY" in text
            and "DOCS_TRANSLATION_APP_PRIVATE_KEY" in text
        ), f"delivery job {job_name} combines model and repository credentials"


def test_external_actions_are_version_pinned() -> None:
    workflow_paths = [GATE_PATH, DELIVERY_PATH, GENERATE_PATH]
    uses: set[str] = set()
    for path in workflow_paths:
        workflow = _workflow(path)
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                if "uses" in step:
                    uses.add(step["uses"])

    assert uses
    assert all("@" in value for value in uses)
    assert all(not value.endswith("@main") for value in uses)


def test_delivery_rechecks_head_and_uses_cas_on_both_paths() -> None:
    workflow = _workflow(DELIVERY_PATH)
    assert workflow["on"]["workflow_run"]["workflows"] == [
        "UCM Documentation Translation Generate"
    ]
    writeback = workflow["jobs"]["writeback"]
    assert (
        "base_repository_id == needs.inspect.outputs.head_repository_id"
        in writeback["if"]
    )
    text = _all_text(writeback)
    assert any(
        'test "${remote_sha}" = "${EXPECTED_HEAD_SHA}"' in step.get("run", "")
        for step in writeback["steps"]
    )
    assert "translate apply" in text
    assert "permission-contents: write" in text
    assert "permission-pull-requests: read" in text
    assert writeback["environment"] == "docs-translation"
    fork = workflow["jobs"]["fork-patch"]
    assert (
        "base_repository_id != needs.inspect.outputs.head_repository_id" in fork["if"]
    )
    text = _all_text(fork)
    assert "translate apply" in text
    assert "HEAD_SHA" in text
    assert "DOCS_TRANSLATION_APP_PRIVATE_KEY" not in text


def test_fork_comment_uses_tools_from_the_validated_base_not_the_pr(tmp_path):
    workflow = _workflow(DELIVERY_PATH)
    step = workflow["jobs"]["fork-patch"]["steps"][0]
    assert step["env"]["BASE_SHA"] == "${{ needs.inspect.outputs.base_sha }}"
    # Render the actual comment without reaching its GitHub publication commands.
    render_script = step["run"].split("\ncomment_id=", 1)[0]
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "PROVIDER": "openai-responses",
        "MODEL": "configured-model",
        "HEAD_SHA": "a" * 40,
        "BASE_SHA": "b" * 40,
        "ARTIFACT_NAME": "docs-translation-pr-17-" + "a" * 40,
        "MAINTAINER_CAN_MODIFY": "true",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "base/ucm",
        "GITHUB_RUN_ID": "123",
        "PR_NUMBER": "17",
    }
    subprocess.run(["bash", "-c", render_script], env=environment, check=True)
    comment = (tmp_path / "translation-comment.md").read_text()
    assert "gh pr checkout 17 --repo 'base/ucm'" in comment
    assert "gh run download 123 --repo 'base/ucm'" in comment
    assert (
        "git fetch --no-tags 'https://github.com/base/ucm.git' '" + "b" * 40 + "'"
        in comment
    )
    assert "git archive '" + "b" * 40 + "' docs-next/tools" in comment
    assert 'trusted_dir="$(mktemp -d)"' in comment
    assert "trap 'rm -rf -- \"$trusted_dir\"' EXIT" in comment
    assert (
        'python "$trusted_dir/docs-next/tools/site.py" translate apply --checkout-root "$PWD" --artifact-dir .translation'
        in comment
    )
    assert "python docs-next/tools/site.py" not in comment
    assert "git apply" not in comment
    commands = comment.split("```bash\n", 1)[1].split("```", 1)[0]
    subprocess.run(["bash", "-n"], input=commands, text=True, check=True)
