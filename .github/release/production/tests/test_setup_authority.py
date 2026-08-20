from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT

SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40
DIGEST = "sha256:" + "3" * 64
SOURCE_DATE_EPOCH = "1700000000"
PROFILE_DISTRIBUTIONS = {
    "cuda130": "uc-manager-cuda",
    "cann900-a2": "uc-manager-cann-a2",
    "cann900-a3": "uc-manager-cann-a3",
}


def _architecture() -> str:
    return "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"


def _authority(
    *,
    profile: str = "cuda130",
    schema_version: int = 2,
    distribution: str | None = None,
    base_version: str = "0.6.0",
    stage: str = "rc",
    wheel_version: str | None = None,
) -> dict[str, Any]:
    architecture = _architecture()
    if wheel_version is None:
        wheel_version = "0.5.0rc1+cuda130" if schema_version == 1 else "0.6.0rc1"
    result: dict[str, Any] = {
        "schema_version": schema_version,
        "kind": "ucm-native-build-authority",
        "spec_id": f"{profile}-{architecture}",
        "profile_id": profile,
        "cpu_arch": architecture,
        "platform": f"linux/{architecture}",
        "wheel_version": wheel_version,
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "source_archive_sha256": DIGEST,
        "source_date_epoch": int(SOURCE_DATE_EPOCH),
        "task_sha256": DIGEST,
        "builder_coordinate": "registry.example/builder@" + DIGEST,
        "builder_config_digest": DIGEST,
        "dependency_lock_sha256": DIGEST,
        "tool_wheels": {f"tool-{index}.whl": DIGEST for index in range(7)},
        "required_native": ["ucmtrans"],
        "forbidden_native": ["mooncakestore"],
        "build_context_sha256": DIGEST,
    }
    if schema_version == 2:
        result.update(
            {
                "distribution": distribution or PROFILE_DISTRIBUTIONS[profile],
                "base_version": base_version,
                "stage": stage,
            }
        )
    return result


def _source_copy(tmp_path: Path, version: str) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    shutil.copyfile(REPO_ROOT / "setup.py", root / "setup.py")
    (root / "version.ini").write_text(f"VLLM_UC_VERSION={version}\n", encoding="utf-8")
    return root


def _run_setup(
    tmp_path: Path,
    authority: dict[str, Any],
    *,
    env_mutation: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    source_version = "0.5.0rc1" if authority["schema_version"] == 1 else "0.6.0"
    source = _source_copy(tmp_path, source_version)
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(
        json.dumps(authority, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    profile = str(authority["profile_id"])
    platform_name = (
        "ascend-a3"
        if profile.endswith("-a3")
        else "ascend" if profile.endswith("-a2") else "cuda"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UCM_RELEASE_") and key != "SOURCE_DATE_EPOCH"
    }
    env.update(
        {
            "UCM_RELEASE_BUILD": "1",
            "UCM_RELEASE_AUTHORITY_FILE": str(authority_path),
            "UCM_RELEASE_PROFILE": profile,
            "UCM_RELEASE_SOURCE_SHA": SOURCE_SHA,
            "UCM_RELEASE_VERSION": str(authority["wheel_version"]),
            "UCM_RELEASE_BUILD_KEY": DIGEST,
            "UCM_RELEASE_REQUIRED_TARGETS": "ucmtrans",
            "UCM_RELEASE_FORBIDDEN_TARGETS": "mooncakestore",
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
            "PLATFORM": platform_name,
        }
    )
    if authority["schema_version"] == 2:
        env["UCM_RELEASE_DISTRIBUTION"] = str(authority["distribution"])
    if env_mutation:
        env.update(env_mutation)
    return subprocess.run(
        [sys.executable, "setup.py", "--name", "--version"],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_schema_v1_release_authority_still_builds_uc_manager(tmp_path: Path) -> None:
    result = _run_setup(tmp_path, _authority(schema_version=1))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["uc-manager", "0.5.0rc1+cuda130"]


@pytest.mark.parametrize("profile", list(PROFILE_DISTRIBUTIONS))
def test_schema_v2_authority_controls_exact_distribution(
    tmp_path: Path, profile: str
) -> None:
    result = _run_setup(tmp_path, _authority(profile=profile))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == [
        PROFILE_DISTRIBUTIONS[profile],
        "0.6.0rc1",
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(distribution="uc-manager-evil"), "distribution"),
        (lambda value: value.update(distribution="uc-manager-cann-a2"), "distribution"),
        (lambda value: value.pop("stage"), "fields"),
        (lambda value: value.update(unexpected=True), "fields"),
        (
            lambda value: value.update(stage="stable", wheel_version="0.6.0+local"),
            "version",
        ),
        (
            lambda value: value.update(stage="draft", wheel_version="0.6.0rc1"),
            "version",
        ),
        (
            lambda value: value.update(base_version="0.6.1", wheel_version="0.6.1rc1"),
            "version.ini",
        ),
    ],
)
def test_schema_v2_authority_mutations_fail_closed(
    tmp_path: Path, mutation: object, message: str
) -> None:
    authority = _authority()
    mutation(authority)

    result = _run_setup(tmp_path, authority)

    assert result.returncode != 0
    assert message.lower() in result.stderr.lower()


def test_schema_v2_environment_must_match_distribution(tmp_path: Path) -> None:
    result = _run_setup(
        tmp_path,
        _authority(),
        env_mutation={"UCM_RELEASE_DISTRIBUTION": "uc-manager-cann-a3"},
    )

    assert result.returncode != 0
    assert "distribution" in result.stderr.lower()


def test_schema_v1_rejects_schema_v2_extra_fields(tmp_path: Path) -> None:
    authority = _authority(schema_version=1)
    authority["distribution"] = "uc-manager-cuda"

    result = _run_setup(tmp_path, authority)

    assert result.returncode != 0
    assert "fields" in result.stderr.lower()
