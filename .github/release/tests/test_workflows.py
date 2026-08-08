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
OBSOLETE_RELEASE_WORKFLOWS = {
    "_package-chart.yml",
    "_promote-image-index.yml",
    "_verify-image.yml",
    "_verify-wheel.yml",
    "discover-upstream.yml",
    "pr-build-artifacts.yml",
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


def test_release_workflows_are_compact_and_fork_candidate_is_read_only() -> None:
    """Demand only four workflows and an explicitly isolated fork-candidate job."""
    actual = {
        path.name
        for path in WORKFLOW_DIR.glob("*.yml")
        if path.name in EXPECTED_RELEASE_WORKFLOWS | OBSOLETE_RELEASE_WORKFLOWS
    }
    violations: list[str] = []
    if actual != EXPECTED_RELEASE_WORKFLOWS:
        violations.append(
            "release workflow set must be exactly "
            f"{sorted(EXPECTED_RELEASE_WORKFLOWS)}, found {sorted(actual)}"
        )

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
