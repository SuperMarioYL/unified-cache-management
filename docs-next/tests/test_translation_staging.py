"""Run the real staging script against a bounded, local GitHub API fixture."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/aw/stage-pr-docs.mjs"
SHA = "a" * 40


def stage(tmp_path: Path, entry_type: str, mode: str):
    content = b"# Guide\n"
    tree = [
        {"path": "docs-next/docs/en/guides", "type": "tree", "mode": "040000"},
        {
            "path": "docs-next/docs/en/guides/start.md",
            "type": entry_type,
            "mode": mode,
            "sha": "b" * 40,
            "size": len(content),
        },
    ]
    payloads = {
        "/repos/base/repo/pulls/1": {
            "state": "open",
            "head": {"sha": SHA, "repo": {"full_name": "fork/repo"}},
            "base": {"repo": {"full_name": "base/repo"}},
        },
        f"/repos/fork/repo/git/trees/{SHA}": {"truncated": False, "tree": tree},
        f"/repos/fork/repo/git/blobs/{'b' * 40}": {
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
        },
    }
    script = """
      const payloads = JSON.parse(process.env.MOCK_PAYLOADS);
      globalThis.fetch = async (url) => {
        const value = payloads[new URL(url).pathname];
        if (!value) throw new Error('unexpected API path');
        return {ok: true, json: async () => value};
      };
      await import(process.env.STAGING_SCRIPT);
    """
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        env={
            **os.environ,
            "GITHUB_WORKSPACE": str(tmp_path),
            "BASE_REPOSITORY": "base/repo",
            "HEAD_REPOSITORY": "fork/repo",
            "HEAD_SHA": SHA,
            "PR_NUMBER": "1",
            "GH_TOKEN": "fixture",
            "STAGING_SCRIPT": SCRIPT.as_uri(),
            "MOCK_PAYLOADS": json.dumps(payloads),
        },
        capture_output=True,
        text=True,
    )


def test_staging_accepts_regular_nested_tree_entries(tmp_path):
    result = stage(tmp_path, "blob", "100644")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "docs-next/docs/en/guides/start.md").read_text() == "# Guide\n"


@pytest.mark.parametrize("entry_type,mode", [("blob", "120000"), ("commit", "160000")])
def test_staging_still_rejects_symlinks_and_gitlinks(tmp_path, entry_type, mode):
    result = stage(tmp_path, entry_type, mode)
    assert result.returncode != 0
    assert "symlink or non-file entry" in result.stderr
