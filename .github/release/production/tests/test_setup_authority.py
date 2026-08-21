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
from conftest import PRODUCTION_ROOT, REPO_ROOT
from ucm_release_production.build import (
    authority_from_task,
    project_build_task,
    tool_wheel_authority,
    wheel_build_config_from_task,
)
from ucm_release_production.common import canonical_bytes, sha256_envelope
from ucm_release_production.config import load_config
from ucm_release_production.tags import parse_tag

SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40
DIGEST = "sha256:" + "3" * 64
SOURCE_DATE_EPOCH = "1700000000"
PROFILE_DISTRIBUTIONS = {
    "cuda130": "uc-manager-cuda130",
    "cann900-a2": "uc-manager-cann900-a2-mc039",
    "cann900-a3": "uc-manager-cann900-a3-mc039",
}
PRODUCTION_CONFIG = PRODUCTION_ROOT / "production-release.json"


def _architecture() -> str:
    return "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"


def _authority(
    *,
    profile: str = "cuda130",
    distribution: str | None = None,
    base_version: str = "0.6.0",
    stage: str = "rc",
    wheel_version: str | None = None,
) -> dict[str, Any]:
    architecture = _architecture()
    if wheel_version is None:
        wheel_version = "0.6.0rc1"
    result: dict[str, Any] = {
        "schema_version": 2,
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
    release_package = root / ".github" / "release" / "ucm_release"
    release_package.parent.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / ".github" / "release" / "ucm_release", release_package)
    return root


def _run_setup(
    tmp_path: Path,
    authority: dict[str, Any],
    *,
    env_mutation: dict[str, str] | None = None,
    source_version: str | None = None,
) -> subprocess.CompletedProcess[str]:
    source = _source_copy(
        tmp_path,
        source_version or str(authority.get("base_version", "0.6.0")),
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
        if not key.startswith("UCM_RELEASE_")
        and key
        not in {"UCM_BUILD_CONFIG", "SOURCE_DATE_EPOCH", "PLATFORM", "UCM_DIST_NAME"}
    }
    build_config = {
        "authority": authority,
        "distribution": PROFILE_DISTRIBUTIONS[profile],
        "kind": "ucm-wheel-build-config",
        "platform": platform_name,
        "python": {"abi": "cp312", "version": "3.12"},
        "runtime_requirements": ["packaging==24.2", "wrapt==1.17.2"],
        "schema_version": 1,
    }
    config_path = tmp_path / "wheel-build.json"
    config_path.write_text(
        json.dumps(build_config, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    env["UCM_BUILD_CONFIG"] = str(config_path)
    if env_mutation:
        env.update(env_mutation)
    return subprocess.run(
        [shutil.which("python") or sys.executable, "setup.py", "--name", "--version"],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("profile", list(PROFILE_DISTRIBUTIONS))
def test_schema_v2_build_config_controls_exact_distribution(
    tmp_path: Path, profile: str
) -> None:
    result = _run_setup(tmp_path, _authority(profile=profile))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == [
        PROFILE_DISTRIBUTIONS[profile],
        "0.6.0rc1",
    ]


def test_generated_schema_v2_config_runs_root_setup_without_version_ini(
    tmp_path: Path,
) -> None:
    config = load_config(PRODUCTION_CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    source = sha256_envelope(
        {
            "kind": "ucm-production-source-identity",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": intent.stage,
            "tag_name": intent.tag_name,
            "tag_object_sha": "2" * 40,
            "source_commit_sha": SOURCE_SHA,
            "source_branch": intent.release_branch,
            "tagger": "release-operator",
            "tagged_at": "2026-08-13T08:00:00Z",
            "tag_message_sha256": "3" * 64,
            "control_default_branch": "develop",
            "control_sha": "4" * 40,
            "lineage": None,
        }
    )
    architecture = _architecture()
    task = project_build_task(
        config,
        intent,
        source,
        f"cuda130-{architecture}",
    )
    authority = authority_from_task(
        task,
        source_tree="5" * 40,
        source_archive_sha256="sha256:" + "6" * 64,
        source_date_epoch=int(SOURCE_DATE_EPOCH),
        build_context_sha256="sha256:" + "7" * 64,
        tool_wheels=tool_wheel_authority(config, architecture),
    )
    build_config = wheel_build_config_from_task(task, authority)
    config_path = tmp_path / "wheel-build.json"
    config_path.write_bytes(canonical_bytes(build_config) + b"\n")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UCM_RELEASE_")
        and key
        not in {"UCM_BUILD_CONFIG", "SOURCE_DATE_EPOCH", "PLATFORM", "UCM_DIST_NAME"}
    }
    env["UCM_BUILD_CONFIG"] = str(config_path)

    result = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert not (REPO_ROOT / "version.ini").exists()
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "0.6.0rc1"


@pytest.mark.parametrize(
    ("stage", "base_version", "wheel_version"),
    [
        ("draft", "0.6.0", "0.6.0.dev1"),
        ("rc", "0.6.0", "0.6.0rc1"),
        ("stable", "0.6.0", "0.6.0"),
        ("hotfix", "0.6.1", "0.6.1"),
    ],
)
def test_schema_v2_build_config_preserves_stage_version_rules(
    tmp_path: Path, stage: str, base_version: str, wheel_version: str
) -> None:
    result = _run_setup(
        tmp_path,
        _authority(
            stage=stage,
            base_version=base_version,
            wheel_version=wheel_version,
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == wheel_version


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(distribution="uc-manager-evil"), "invalid"),
        (
            lambda value: value.update(
                distribution="uc-manager-cann900-a2-mc039"
            ),
            "invalid",
        ),
        (lambda value: value.pop("stage"), "schema-v2"),
        (lambda value: value.update(unexpected=True), "schema-v2"),
        (
            lambda value: value.update(stage="stable", wheel_version="0.6.0+local"),
            "invalid",
        ),
        (
            lambda value: value.update(stage="draft", wheel_version="0.6.0rc1"),
            "invalid",
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


def test_schema_v2_build_config_ignores_legacy_release_environment(
    tmp_path: Path,
) -> None:
    result = _run_setup(
        tmp_path,
        _authority(),
        env_mutation={
            "UCM_RELEASE_DISTRIBUTION": "uc-manager-cann900-a3-mc039",
            "UCM_RELEASE_VERSION": "9.9.9",
            "PLATFORM": "maca",
            "SOURCE_DATE_EPOCH": "1",
            "UCM_DIST_NAME": "ignored",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["uc-manager-cuda130", "0.6.0rc1"]


def test_setup_rejects_compact_schema_v1_authority(tmp_path: Path) -> None:
    authority = _authority()
    authority["schema_version"] = 1
    for field in ("distribution", "base_version", "stage"):
        authority.pop(field)

    result = _run_setup(tmp_path, authority)

    assert result.returncode != 0
    assert "extended schema-v1 or production schema-v2" in result.stderr


def test_schema_v2_build_config_ignores_non_authoritative_version_ini(
    tmp_path: Path,
) -> None:
    result = _run_setup(tmp_path, _authority(), source_version="0.6.1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "0.6.0rc1"
