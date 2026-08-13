from __future__ import annotations

import csv
import base64
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from ucm_release_production.build import (
    authority_from_task,
    compare_wheel_candidates,
    project_build_task,
)
from ucm_release_production.common import (
    ProductionError,
    canonical_bytes,
    sha256_envelope,
)
from ucm_release_production.config import load_config
from ucm_release_production.tags import parse_tag

from conftest import PRODUCTION_ROOT

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE_SHA = "1" * 40


def _source(stage: str, tag_name: str, branch: str) -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-source-identity",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": stage,
            "tag_name": tag_name,
            "tag_object_sha": "2" * 40,
            "source_commit_sha": SOURCE_SHA,
            "source_branch": branch,
            "tagger": "release-operator",
            "tagged_at": "2026-08-13T08:00:00Z",
            "tag_message_sha256": "3" * 64,
            "control_default_branch": "develop",
            "control_sha": "4" * 40,
            "lineage": None,
        }
    )


def test_project_build_tasks_are_exactly_three_profiles_by_two_arches() -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    source = _source(intent.stage, intent.tag_name, intent.release_branch)

    tasks = [
        project_build_task(config, intent, source, f"{profile}-{arch}")
        for profile in ("cuda130", "cann900-a2", "cann900-a3")
        for arch in ("amd64", "arm64")
    ]

    assert [task["spec_id"] for task in tasks] == [
        "cuda130-amd64",
        "cuda130-arm64",
        "cann900-a2-amd64",
        "cann900-a2-arm64",
        "cann900-a3-amd64",
        "cann900-a3-arm64",
    ]
    assert [task["distribution"] for task in tasks[::2]] == [
        "uc-manager-cuda",
        "uc-manager-cann-a2",
        "uc-manager-cann-a3",
    ]
    assert all(task["wheel_version"] == "0.6.0rc1" for task in tasks)
    assert all(task["source_sha"] == SOURCE_SHA for task in tasks)
    assert all(task["write_authority"] == [] for task in tasks)
    assert all(task["sha256"] for task in tasks)
    assert "OctoCat" not in canonical_bytes(tasks).decode()


def test_hotfix_build_task_uses_the_protected_patch_version_as_base() -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.1", config)
    source = _source(intent.stage, intent.tag_name, intent.release_branch)

    task = project_build_task(config, intent, source, "cuda130-amd64")

    assert task["stage"] == "hotfix"
    assert task["base_version"] == "0.6.1"
    assert task["wheel_version"] == "0.6.1"


def test_projected_task_is_the_only_schema_v2_setup_authority_source() -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cann900-a2-arm64",
    )
    tools = {f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)}

    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels=tools,
    )

    assert authority["schema_version"] == 2
    assert authority["profile_id"] == "cann900-a2"
    assert authority["distribution"] == "uc-manager-cann-a2"
    assert authority["base_version"] == "0.6.0"
    assert authority["stage"] == "rc"
    assert authority["wheel_version"] == "0.6.0rc1"
    assert authority["task_sha256"] == "sha256:" + task["sha256"]
    assert authority["builder_coordinate"].endswith(
        "@sha256:638fc04eaa3654fcf14688096ed4e9d88ea0d905fa8685eed4b36d5fffe8fd8d"
    )
    assert authority["tool_wheels"] == tools


@pytest.mark.parametrize(
    ("spec_id", "message"),
    [
        ("cuda130-ppc64le", "spec"),
        ("cuda999-amd64", "spec"),
        ("cuda130-amd64\n", "spec"),
    ],
)
def test_project_build_task_rejects_caller_invented_specs(
    spec_id: str, message: str
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)

    with pytest.raises(ProductionError, match=message):
        project_build_task(
            config,
            intent,
            _source(intent.stage, intent.tag_name, intent.release_branch),
            spec_id,
        )


def test_project_build_task_rejects_source_intent_drift() -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    source = _source("draft", "draft/v0.6.0-1", "0.6.0-release")

    with pytest.raises(ProductionError, match="source identity"):
        project_build_task(config, intent, source, "cuda130-amd64")


def _elf64(machine: int = 62) -> bytes:
    header = bytearray(64)
    header[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    header[16:18] = (3).to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    header[20:24] = (1).to_bytes(4, "little")
    return bytes(header)


def _wheel(
    path: Path, distribution: str, version: str, payload: bytes | None = None
) -> None:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    entries = {
        "ucm/__init__.py": b"",
        "ucm/native.so": payload if payload is not None else _elf64(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
            "Requires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: ucm-production-test\n"
            "Root-Is-Purelib: false\nTag: cp312-cp312-manylinux_2_28_x86_64\n\n"
        ).encode(),
    }
    rows: list[list[str]] = []
    for name, data in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append([name, "sha256=" + digest.decode(), str(len(data))])
    record_name = f"{dist_info}/RECORD"
    rows.append([record_name, "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[record_name] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)


def test_compare_wheel_candidates_requires_byte_and_metadata_equality(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cuda130-amd64",
    )
    candidate = tmp_path / "candidate.whl"
    trusted = tmp_path / "trusted.whl"
    _wheel(candidate, "uc-manager-cuda", "0.6.0rc1")
    trusted.write_bytes(candidate.read_bytes())

    result = compare_wheel_candidates(candidate, trusted, task)

    assert result["identical"] is True
    assert result["distribution"] == "uc-manager-cuda"
    assert result["version"] == "0.6.0rc1"
    assert result["sha256"].startswith("sha256:")


def test_compare_wheel_candidates_rejects_byte_drift(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cuda130-amd64",
    )
    candidate = tmp_path / "candidate.whl"
    trusted = tmp_path / "trusted.whl"
    _wheel(candidate, "uc-manager-cuda", "0.6.0rc1")
    _wheel(trusted, "uc-manager-cuda", "0.6.0rc1", _elf64() + b"drift")

    with pytest.raises(ProductionError, match="byte-for-byte"):
        compare_wheel_candidates(candidate, trusted, task)


@pytest.mark.parametrize(
    ("distribution", "version", "message"),
    [
        ("uc-manager", "0.6.0rc1", "distribution"),
        ("uc-manager-cuda", "0.6.0", "version"),
    ],
)
def test_compare_wheel_candidates_rejects_metadata_drift(
    tmp_path: Path, distribution: str, version: str, message: str
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cuda130-amd64",
    )
    candidate = tmp_path / "candidate.whl"
    trusted = tmp_path / "trusted.whl"
    _wheel(candidate, distribution, version)
    trusted.write_bytes(candidate.read_bytes())

    with pytest.raises(ProductionError, match=message):
        compare_wheel_candidates(candidate, trusted, task)
