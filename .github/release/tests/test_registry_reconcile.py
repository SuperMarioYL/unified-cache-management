"""Loopback registry integration contract for the slim release package.

Only the real registry:2.8.3 push/pull integration test and the minimal
subprocess-environment safety check are retained.  The hundreds of fixture
evidence, JSON-shape, and reconciliation change-detector tests were removed
per the slimming plan -- they exercised the verify.py fixture loop that D1
deletes rather than observable behaviour.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"


def _modules():
    sys.path.insert(0, str(RELEASE_ROOT))
    return (
        importlib.import_module("ucm_release.registry"),
        None,
    )


def test_registry_subprocess_environment_is_minimal_and_keeps_login_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry subprocesses inherit only proxy/transport env, never secrets."""
    registry, _ = _modules()
    values = {
        "HOME": "/runner/home",
        "DOCKER_CONFIG": "/runner/docker-config",
        "SSL_CERT_FILE": "/etc/certs.pem",
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "NO_PROXY": "127.0.0.1",
        "GITHUB_TOKEN": "must-not-leak",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "must-not-leak",
        "PATH": "/attacker/path",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    environment = registry._minimal_registry_environment()

    assert environment == {
        "HOME": "/runner/home",
        "DOCKER_CONFIG": "/runner/docker-config",
        "SSL_CERT_FILE": "/etc/certs.pem",
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "NO_PROXY": "127.0.0.1",
    }

    crane = tmp_path / "crane"
    crane.write_text("not executed", encoding="utf-8")
    invocation: dict[str, object] = {}

    def run(
        arguments: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        invocation.update({"arguments": arguments, **options})
        return subprocess.CompletedProcess(
            arguments, 0, stdout="sha256:" + "1" * 64 + "\n", stderr=""
        )

    monkeypatch.setattr(registry.subprocess, "run", run)
    reference = "docker.io/vllm/vllm-openai:v0.10.2"
    assert registry._crane(str(crane), "digest", reference) == "sha256:" + "1" * 64
    assert invocation["env"] == environment
    assert invocation["arguments"] == [str(crane), "digest", reference]
