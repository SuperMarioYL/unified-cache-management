from __future__ import annotations

import base64
import csv
import email.parser
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import PRODUCTION_ROOT, REPO_ROOT
from ucm_release_production.build import (
    authority_from_task,
    compare_wheel_candidates,
    docker_build_projection,
    project_build_task,
    seal_built_wheel,
    wheel_build_config_from_task,
)
from ucm_release_production.common import (
    ProductionError,
    canonical_bytes,
    sha256_envelope,
)
from ucm_release_production.config import load_config
from ucm_release_production.tags import parse_tag

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE_SHA = "1" * 40


def test_production_wheel_dockerfile_consumes_only_canonical_release_config() -> None:
    source = (
        REPO_ROOT / ".github" / "release" / "production" / "docker" / "Dockerfile.wheel"
    ).read_text(encoding="utf-8")

    release_args = {
        name
        for name in re.findall(r"^ARG ([A-Z0-9_]+)", source, re.MULTILINE)
        if name.startswith("UCM_")
    }
    assert release_args == {"UCM_BUILDER_IMAGE", "UCM_BUILD_CONFIG"}
    for forbidden in (
        "UCM_RELEASE_",
        "ARG PLATFORM",
        "ARG SOURCE_DATE_EPOCH",
        "ARG UCM_DIST_NAME",
        "re.subn",
    ):
        assert forbidden not in source
    assert "COPY ${UCM_BUILD_CONFIG} /tmp/wheel-build.json" in source
    assert "COPY trusted-control/ucm_release /tmp/release-control/ucm_release" in source
    assert "wheel prepare-source" in source
    assert "UCM_BUILD_CONFIG=/tmp/wheel-build.json" in source
    assert "FROM scratch AS production-wheel" in source


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
    assert all(task["python_version"] == "3.12" for task in tasks)
    assert all(task["python_abi"] == "cp312" for task in tasks)
    assert [task["build_platform"] for task in tasks[::2]] == [
        "cuda",
        "ascend",
        "ascend-a3",
    ]
    assert [task["wheel_platform"] for task in tasks[::2]] == [
        "manylinux_2_28",
        "linux",
        "linux",
    ]
    assert all(
        task["runtime_requirements"] == ["packaging==24.2", "wrapt==1.17.2"]
        for task in tasks
    )
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


def test_production_task_projects_the_exact_wheel_build_wrapper() -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cann900-a2-arm64",
    )
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )

    wrapper = wheel_build_config_from_task(task, authority)

    assert canonical_bytes(wrapper) + b"\n" == (
        json.dumps(
            {
                "authority": authority,
                "distribution": "uc-manager-cann-a2",
                "kind": "ucm-wheel-build-config",
                "platform": "ascend",
                "python": {"abi": "cp312", "version": "3.12"},
                "runtime_requirements": [
                    "packaging==24.2",
                    "wrapt==1.17.2",
                ],
                "schema_version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def test_production_docker_projection_has_exact_reduced_build_arguments() -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cuda130-amd64",
    )
    context = {
        "source_tree": "6" * 40,
        "source_archive_sha256": "sha256:" + "7" * 64,
        "build_context_sha256": "sha256:" + "8" * 64,
    }

    projection = docker_build_projection(config, task, context, 1_700_000_000)

    prefixes = {
        "BUILD",
        "PYPROJECT_HOOKS",
        "PACKAGING",
        "SETUPTOOLS",
        "WHEEL",
        "PYYAML",
        "CMAKE",
    }
    expected = {"UCM_BUILDER_IMAGE", "UCM_BUILD_CONFIG"} | {
        f"{prefix}_{suffix}"
        for prefix in prefixes
        for suffix in ("VERSION", "FILENAME", "SHA256")
    }
    assert set(projection["build_args"]) == expected
    assert projection["build_args"]["UCM_BUILD_CONFIG"] == "wheel-build.json"
    assert projection["docker_target"] == "production-wheel"
    assert not any(name.startswith("UCM_RELEASE_") for name in expected)
    assert not {"PLATFORM", "SOURCE_DATE_EPOCH", "UCM_DIST_NAME"} & expected


def test_production_projection_cli_writes_all_three_canonical_records(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cann900-a2-arm64",
    )
    context = {
        "source_tree": "6" * 40,
        "source_archive_sha256": "sha256:" + "7" * 64,
        "build_context_sha256": "sha256:" + "8" * 64,
    }
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "source-context.json"
    task_path.write_bytes(canonical_bytes(task) + b"\n")
    context_path.write_bytes(canonical_bytes(context) + b"\n")
    output_dir = tmp_path / "projection"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ucm_release_production",
            "build",
            "projection",
            "--config",
            str(CONFIG),
            "--task",
            str(task_path),
            "--source-context",
            str(context_path),
            "--source-date-epoch",
            "1700000000",
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(PRODUCTION_ROOT),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in output_dir.iterdir()} == {
        "build-authority.json",
        "build-projection.json",
        "wheel-build.json",
    }
    authority = json.loads((output_dir / "build-authority.json").read_bytes())
    wheel_config = json.loads((output_dir / "wheel-build.json").read_bytes())
    assert wheel_config == wheel_build_config_from_task(task, authority)
    for path in output_dir.iterdir():
        value = json.loads(path.read_bytes())
        assert path.read_bytes() == canonical_bytes(value) + b"\n"


@pytest.mark.parametrize(
    ("tag", "stage", "base_version", "wheel_version"),
    [
        ("draft/v0.6.0-1", "draft", "0.6.0", "0.6.0.dev1"),
        ("v0.6.0rc1", "rc", "0.6.0", "0.6.0rc1"),
        ("v0.6.0", "stable", "0.6.0", "0.6.0"),
        ("v0.6.1", "hotfix", "0.6.1", "0.6.1"),
    ],
)
def test_wheel_build_wrapper_preserves_every_production_stage(
    tag: str, stage: str, base_version: str, wheel_version: str
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag(tag, config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cuda130-amd64",
    )
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )

    wrapper = wheel_build_config_from_task(task, authority)

    assert authority["stage"] == stage
    assert authority["base_version"] == base_version
    assert authority["wheel_version"] == wheel_version
    assert wrapper["distribution"] == "uc-manager-cuda"
    assert wrapper["platform"] == "cuda"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(spec_id="cann900-a2-amd64"),
        lambda value: value.update(profile_id="cann900-a2"),
        lambda value: value.update(distribution="uc-manager-cann-a2"),
        lambda value: value.update(base_version="0.6.1"),
        lambda value: value.update(stage="stable"),
        lambda value: value.update(cpu_arch="arm64", platform="linux/arm64"),
        lambda value: value.update(platform="linux/arm64"),
        lambda value: value.update(wheel_version="0.6.0"),
        lambda value: value.update(source_sha="9" * 40),
        lambda value: value.update(task_sha256="sha256:" + "9" * 64),
        lambda value: value.update(
            builder_coordinate="registry.example/other@sha256:" + "9" * 64
        ),
        lambda value: value.update(builder_config_digest="sha256:" + "9" * 64),
        lambda value: value.update(dependency_lock_sha256="sha256:" + "9" * 64),
        lambda value: value.update(required_native=["ucmtrans"]),
        lambda value: value.update(forbidden_native=["hash_retrieval_backend"]),
    ],
)
def test_wheel_build_wrapper_rejects_every_production_authority_overlap(
    mutation: object,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        "cuda130-amd64",
    )
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )
    mutation(authority)

    with pytest.raises(ProductionError, match="authority"):
        wheel_build_config_from_task(task, authority)


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
    path: Path,
    distribution: str,
    version: str,
    payload: bytes | None = None,
    *,
    wheel_tag: str = "cp312-cp312-manylinux_2_28_x86_64",
) -> None:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    entries = {
        "ucm/__init__.py": b"",
        "ucm/native.so": payload if payload is not None else _elf64(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
            "Requires-Dist: packaging==24.2\n"
            "Requires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: ucm-production-test\n"
            f"Root-Is-Purelib: false\nTag: {wheel_tag}\n\n"
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


def _sealable_task(profile: str, architecture: str) -> dict[str, object]:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(
        config,
        intent,
        _source(intent.stage, intent.tag_name, intent.release_branch),
        f"{profile}-{architecture}",
    )
    task.pop("sha256")
    task["required_native"] = ["ucmtrans"]
    task["forbidden_native"] = ["mooncakestore"]
    return sha256_envelope(task)


def _raw_native_wheel(
    path: Path,
    task: dict[str, object],
    *,
    tags: list[str] | None = None,
    wheel_files: int = 1,
) -> None:
    architecture = str(task["cpu_arch"])
    machine = {"amd64": 62, "arm64": 183}[architecture]
    dist_info = f"{str(task['distribution']).replace('-', '_')}-{task['wheel_version']}.dist-info"
    expected_tag = (
        f"cp312-cp312-{task['wheel_platform']}_"
        f"{'x86_64' if architecture == 'amd64' else 'aarch64'}"
    )
    rendered_tags = [expected_tag] if tags is None else tags
    wheel_metadata = (
        "Wheel-Version: 1.0\nGenerator: ucm-production-test\n"
        "Root-Is-Purelib: false\n"
        + "".join(f"Tag: {tag}\n" for tag in rendered_tags)
        + "\n"
    ).encode()
    entries = {
        "ucm/shared/trans/ucmtrans.cpython-312-test.so": _elf64(machine),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {task['distribution']}\n"
            f"Version: {task['wheel_version']}\n"
            "Requires-Dist: packaging==24.2\n"
            "Requires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
    }
    if wheel_files >= 1:
        entries[f"{dist_info}/WHEEL"] = wheel_metadata
    if wheel_files >= 2:
        entries["other-1.0.dist-info/WHEEL"] = wheel_metadata
    record_name = f"{dist_info}/RECORD"
    rows: list[list[str]] = []
    for name, data in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append([name, "sha256=" + digest.decode(), str(len(data))])
    rows.append([record_name, "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[record_name] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)


@pytest.mark.parametrize(
    ("profile", "architecture", "wheel_platform", "tag_arch"),
    [
        ("cuda130", "amd64", "manylinux_2_28", "x86_64"),
        ("cuda130", "arm64", "manylinux_2_28", "aarch64"),
        ("cann900-a2", "amd64", "linux", "x86_64"),
        ("cann900-a2", "arm64", "linux", "aarch64"),
        ("cann900-a3", "amd64", "linux", "x86_64"),
        ("cann900-a3", "arm64", "linux", "aarch64"),
    ],
)
def test_sealed_wheel_filename_and_wheel_tag_share_profile_authority(
    tmp_path: Path,
    profile: str,
    architecture: str,
    wheel_platform: str,
    tag_arch: str,
) -> None:
    task = _sealable_task(profile, architecture)
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )
    raw = tmp_path / "raw.whl"
    output_dir = tmp_path / "sealed"
    raw_tag = (
        f"cp312-cp312-linux_{tag_arch}"
        if profile == "cuda130"
        else f"cp312-cp312-{wheel_platform}_{tag_arch}"
    )
    _raw_native_wheel(raw, task, tags=[raw_tag])

    record = seal_built_wheel(raw, output_dir, task, authority)

    expected_tag = f"cp312-cp312-{wheel_platform}_{tag_arch}"
    assert record["filename"].endswith(f"-{expected_tag}.whl")
    with zipfile.ZipFile(output_dir / record["filename"]) as archive:
        wheel_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        ]
        assert len(wheel_names) == 1
        parsed = email.parser.BytesParser().parsebytes(archive.read(wheel_names[0]))
        assert parsed.get_all("Tag", []) == [expected_tag]


@pytest.mark.parametrize(
    ("tags", "wheel_files", "message"),
    [
        (["cp312-cp312-manylinux_2_17_x86_64"], 1, "Tag"),
        ([], 1, "Tag"),
        (
            [
                "cp312-cp312-manylinux_2_28_x86_64",
                "cp312-cp312-manylinux_2_17_x86_64",
            ],
            1,
            "Tag",
        ),
        (None, 0, "WHEEL"),
        (None, 2, "WHEEL"),
    ],
)
def test_seal_rejects_missing_multiple_or_drifted_wheel_tags(
    tmp_path: Path,
    tags: list[str] | None,
    wheel_files: int,
    message: str,
) -> None:
    task = _sealable_task("cuda130", "amd64")
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )
    raw = tmp_path / "raw.whl"
    _raw_native_wheel(raw, task, tags=tags, wheel_files=wheel_files)

    with pytest.raises(ProductionError, match=message):
        seal_built_wheel(raw, tmp_path / "sealed", task, authority)

    assert not (tmp_path / "sealed").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("build_platform", "ascend"),
        ("wheel_platform", "linux"),
    ],
)
def test_seal_independently_rejects_rehashed_profile_tuple_drift(
    tmp_path: Path, field: str, value: str
) -> None:
    task = _sealable_task("cuda130", "amd64")
    task.pop("sha256")
    task[field] = value
    task = sha256_envelope(task)
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )
    raw = tmp_path / "raw.whl"
    _raw_native_wheel(raw, task)

    with pytest.raises(ProductionError, match=field):
        seal_built_wheel(raw, tmp_path / "sealed", task, authority)


def test_ascend_seal_rejects_non_linux_raw_tag(tmp_path: Path) -> None:
    task = _sealable_task("cann900-a2", "amd64")
    authority = authority_from_task(
        task,
        source_tree="6" * 40,
        source_archive_sha256="sha256:" + "7" * 64,
        source_date_epoch=1_700_000_000,
        build_context_sha256="sha256:" + "8" * 64,
        tool_wheels={f"tool-{index}.whl": "sha256:" + "5" * 64 for index in range(7)},
    )
    raw = tmp_path / "raw.whl"
    _raw_native_wheel(raw, task, tags=["cp312-cp312-manylinux_2_28_x86_64"])

    with pytest.raises(ProductionError, match="Tag"):
        seal_built_wheel(raw, tmp_path / "sealed", task, authority)


@pytest.mark.parametrize("inverse", [False, True])
def test_seal_rejects_ambiguous_or_inverse_task_profile_discriminator_first(
    tmp_path: Path, inverse: bool
) -> None:
    task = _sealable_task("cuda130", "amd64")
    task.pop("sha256")
    if inverse:
        task["id"] = task.pop("profile_id")
    else:
        task["id"] = "cann900-a2"
        task.update(
            distribution="uc-manager-cann-a2",
            build_platform="ascend",
            wheel_platform="linux",
        )
    task = sha256_envelope(task)
    output_dir = tmp_path / "sealed"

    with pytest.raises(
        ProductionError,
        match="must contain profile_id and must not contain id",
    ):
        seal_built_wheel(tmp_path / "missing-raw.whl", output_dir, task, {})

    assert not output_dir.exists()


@pytest.mark.parametrize(
    ("mutation", "rehash", "message"),
    [
        (lambda task: task.update(sha256="9" * 64), False, "hash"),
        (lambda task: task.pop("runner"), True, "keys"),
        (lambda task: task.update(kind="wrong"), True, "kind"),
        (lambda task: task.update(schema_version=2), True, "schema_version"),
    ],
)
def test_seal_validates_complete_task_envelope_before_raw_path(
    tmp_path: Path, mutation: object, rehash: bool, message: str
) -> None:
    task = _sealable_task("cuda130", "amd64")
    mutation(task)
    if rehash:
        task.pop("sha256", None)
        task = sha256_envelope(task)
    output_dir = tmp_path / "sealed"

    with pytest.raises(ProductionError, match=message):
        seal_built_wheel(tmp_path / "missing-raw.whl", output_dir, task, {})

    assert not output_dir.exists()


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


def test_inspect_rejects_recomputed_record_with_wrong_wheel_tag(
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
    _wheel(
        candidate,
        "uc-manager-cuda",
        "0.6.0rc1",
        wheel_tag="cp312-cp312-linux_x86_64",
    )
    trusted.write_bytes(candidate.read_bytes())

    with pytest.raises(ProductionError, match="Tag"):
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
