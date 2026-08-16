#!/usr/bin/env python3
"""Select the exact Python version from one frozen task using stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def select_python(
    plan_path: Path,
    task_kind: str,
    task_id: str,
    expected_sha256: str,
    expected_source_sha: str,
) -> str:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("resolved plan must be an object")
    claimed = plan.pop("resolved_plan_sha256", None)
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    if claimed != expected_sha256 or digest != expected_sha256:
        raise ValueError("resolved plan digest differs from workflow authority")
    source = plan.get("source")
    if (
        not isinstance(expected_source_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_source_sha) is None
        or not isinstance(source, dict)
        or source.get("commit") != expected_source_sha
    ):
        raise ValueError("resolved plan source differs from workflow authority")
    if task_kind not in {"wheel", "image"}:
        raise ValueError("task kind must be wheel or image")
    tasks = plan.get(f"{task_kind}_tasks")
    matches = (
        [task for task in tasks if task.get("task_id") == task_id]
        if isinstance(tasks, list)
        else []
    )
    if len(matches) != 1:
        raise ValueError(
            f"frozen plan must contain exactly one selected {task_kind} task"
        )
    version = matches[0].get("python_version")
    abi = matches[0].get("python_abi")
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+", version) is None
        or abi != "cp" + version.replace(".", "")
    ):
        raise ValueError("selected wheel task Python authority is invalid")
    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--task-kind", choices=("wheel", "image"), required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--github-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        version = select_python(
            args.plan,
            args.task_kind,
            args.task_id,
            args.expected_sha256,
            args.expected_source_sha,
        )
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"python_version={version}\n")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
