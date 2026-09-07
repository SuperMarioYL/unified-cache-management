from __future__ import annotations

import json
from pathlib import Path

import pytest
import trigger_rtd
import yaml
from test_manifest import _manifest


@pytest.mark.parametrize("current_stable", ["v0.9.0", "v0.9.1"])
def test_release_rebuild_does_not_move_stable_alias(
    monkeypatch, tmp_path, current_stable
):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest([])))
    calls = []

    def request(path, *, method="GET", data=None):
        calls.append((path, method, data))
        if path.endswith("/projects/docs-en/") or path.endswith("/projects/docs-zh/"):
            return {"repository": {"url": "https://github.com/example/ucm.git"}}
        if method == "GET":
            return {"active": True, "ref": current_stable}
        return {}

    monkeypatch.setattr(trigger_rtd, "request", request)
    triggered = trigger_rtd.notify_release(
        "example/ucm", manifest_path, ["docs-en", "docs-zh"]
    )
    assert not any(method == "PATCH" for _, method, _ in calls)
    for project in ("docs-en", "docs-zh"):
        assert f"{project}/v0.9.0" in triggered
        assert f"{project}/latest" in triggered
        assert (f"{project}/stable" in triggered) == (current_stable == "v0.9.0")


def test_rtd_project_must_belong_to_the_release_repository(monkeypatch, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest([])))
    calls = []

    def request(path, **kwargs):
        calls.append((path, kwargs))
        return {"repository": {"url": "https://github.com/other/ucm.git"}}

    monkeypatch.setattr(trigger_rtd, "request", request)
    with pytest.raises(ValueError, match="another repository"):
        trigger_rtd.notify_release("example/ucm", manifest_path, ["docs-en"])
    assert len(calls) == 1


def test_workflow_notifies_rtd_only_after_manifest_readback():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release-ucm.yml").read_text())
    steps = workflow["jobs"]["update-release-images"]["steps"]
    publication = next(
        i for i, step in enumerate(steps) if step.get("id") == "publish-manifest"
    )
    notification = next(
        i for i, step in enumerate(steps) if "trigger_rtd.py" in step.get("run", "")
    )
    assert notification > publication
    assert notification > next(
        i for i, step in enumerate(steps) if step.get("id") == "retention"
    )
    step = steps[notification]
    assert "steps.publish-manifest.outcome == 'success'" in step["if"]
    assert "release-manifest-readback.json" in step["run"]
    assert "RTD_PROJECT_EN" in step["run"] and "RTD_PROJECT_ZH" in step["run"]
