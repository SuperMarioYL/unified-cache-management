from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

_deterministic_repack = importlib.import_module(
    "ucm_release.chart"
)._deterministic_repack
release_core = importlib.import_module("ucm_release.core")
release_wheel = importlib.import_module("ucm_release.wheel")
derive_chart_version = release_core.derive_chart_version


EXPECTED_TASK_IDS = [
    "cuda130-amd64",
    "cuda130-arm64",
    "cann900-a2-amd64",
    "cann900-a2-arm64",
    "cann900-a3-amd64",
    "cann900-a3-arm64",
]

EXPECTED_TARGETS = {
    "ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1",
    "ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-ucm-0.5.0rc1-r1",
    "ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-a3-ucm-0.5.0rc1-r1",
}

CUDA_AMD64_BUILD_KEY = (
    "sha256:d0ee32872f80529f63b4aa85f2e9160f8aa115e5db493f0adceba983377bacbf"
)
A2_AMD64_BUILD_KEY = (
    "sha256:c18c73be8e9bd04f710c304b84df807d78462e5af0abfdcff24ab65b3db81e7d"
)
SOURCE_DATE_EPOCH = 1_700_000_000
REVIEWED_SOURCE_SHA = subprocess.run(
    ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()
CUDA_REQUIRED_NATIVE = [
    "ucmtrans",
    "metrics",
    "ucmmetrics",
    "ucmlogger",
    "ucmnfsstore",
    "ucmpcstore",
    "posixstore",
    "compressor",
    "cachestore",
    "emptystore",
    "fakestore",
    "ucmpipelinestore",
]
CUDA_FORBIDDEN_NATIVE = [
    "mooncakestore",
    "ds3fsstore",
    "uc_hash_ext",
    "ucm_custom_ops",
    "hash_retrieval_backend",
    "hamming",
    "gsa_prefetch",
    "kvstar_retrieve",
    "retrieval_backend",
    "gsa_offload_ops",
]

NATIVE_MEMBERS = {
    "ucmtrans": "ucm/shared/trans/ucmtrans.cpython-312-x86_64-linux-gnu.so",
    "metrics": "ucm/shared/metrics/libmetrics.so",
    "ucmmetrics": "ucm/shared/metrics/ucmmetrics.cpython-312-x86_64-linux-gnu.so",
    "ucmlogger": "ucm/shared/infra/logger/ucmlogger.cpython-312-x86_64-linux-gnu.so",
    "ucmnfsstore": "ucm/store/nfsstore/ucmnfsstore.cpython-312-x86_64-linux-gnu.so",
    "ucmpcstore": "ucm/store/pcstore/ucmpcstore.cpython-312-x86_64-linux-gnu.so",
    "posixstore": "ucm/store/posix/libposixstore.so",
    "compressor": "ucm/store/compress/libcompressor.so",
    "cachestore": "ucm/store/cache/libcachestore.so",
    "emptystore": "ucm/store/empty/libemptystore.so",
    "fakestore": "ucm/store/fake/libfakestore.so",
    "ucmpipelinestore": "ucm/store/pipeline/ucmpipelinestore.cpython-312-x86_64-linux-gnu.so",
    "mooncakestore": "ucm/store/mooncakestore/libmooncakestore.so",
    "ds3fsstore": "ucm/store/ds3fs/libds3fsstore.so",
}


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": PYTHONPATH}
    return subprocess.run(
        [sys.executable, "-m", "ucm_release", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _clone(value: object) -> object:
    return json.loads(json.dumps(value))


def _write_canonical_json(path: Path, value: object) -> None:
    path.write_bytes(release_core.canonical_bytes(value) + b"\n")


def _git(repository: Path, *arguments: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _protected_tag_repository(
    tmp_path: Path,
) -> tuple[Path, dict[str, str], Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=develop")
    _git(repository, "config", "user.email", "release-test@example.invalid")
    _git(repository, "config", "user.name", "Release Test")
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/SuperMarioYL/unified-cache-management.git",
    )
    (repository / "version.ini").write_text(
        "VLLM_UC_VERSION=0.5.0rc1\n", encoding="utf-8"
    )
    chart_directory = repository / "charts" / "ucm"
    chart_directory.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "charts" / "ucm" / "Chart.yaml", chart_directory / "Chart.yaml"
    )
    _git(repository, "add", "version.ini", "charts/ucm/Chart.yaml")
    _git(repository, "commit", "-m", "release source")
    tag_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v0.5.0rc1", tag_sha)
    (repository / "develop.txt").write_text("after release\n", encoding="utf-8")
    _git(repository, "add", "develop.txt")
    _git(repository, "commit", "-m", "advance develop")
    default_branch_sha = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "update-ref",
        "refs/remotes/origin/develop",
        default_branch_sha,
    )
    _git(repository, "checkout", "--detach", tag_sha)

    event_path = tmp_path / "event.json"
    event = {
        "after": tag_sha,
        "ref": "refs/tags/v0.5.0rc1",
        "repository": {
            "default_branch": "develop",
            "full_name": "SuperMarioYL/unified-cache-management",
            "owner": {"login": "SuperMarioYL"},
        },
        "sender": {"login": "SuperMarioYL"},
    }
    event_path.write_text(json.dumps(event), encoding="utf-8")
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_ACTOR": "SuperMarioYL",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REF": "refs/tags/v0.5.0rc1",
        "GITHUB_REF_NAME": "v0.5.0rc1",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REPOSITORY": "SuperMarioYL/unified-cache-management",
        "GITHUB_REPOSITORY_OWNER": "SuperMarioYL",
        "GITHUB_SHA": tag_sha,
        "UCM_RELEASE_POLICY": "owner-reviewed-v1",
    }
    return repository, environment, event_path, tag_sha, default_branch_sha


def _run_protected_tag_preflight(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    monkeypatch.setattr(release_core, "REPO_ROOT", repository)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return release_core.tag_preflight(lane="protected-tag")


def _rewrite_event(event_path: Path, **updates: object) -> None:
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event.update(updates)
    event_path.write_text(json.dumps(event), encoding="utf-8")


def _reject_config(
    tmp_path: Path,
    release: dict,
    *,
    compatibility: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    return _run(
        "config",
        "validate",
        "--release",
        str(release_path),
        "--compatibility",
        str(compatibility_path),
        check=False,
    )


def _fixture_wheel(directory: Path, *, version: str = "0.5.0rc1") -> Path:
    assert version == "0.5.0rc1"
    output = directory / "fixture-only-wheel"
    if output.exists():
        return next(output.glob("*.whl"))
    builder = importlib.import_module("ucm_release.wheel")
    built = builder.build_fixture_wheel(
        output,
        "0" * 40,
        "cuda130-amd64",
    )
    return Path(built["wheel_path"])


def _drop_record(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
            if not item.is_dir() and not item.filename.endswith(".dist-info/RECORD")
        }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)


def _builder_candidate_wheel(
    directory: Path,
    *,
    include_native: bool,
    runtime: str = "cuda-13.0",
) -> Path:
    version = "0.5.0rc1"
    filename = f"uc_manager-{version}-cp312-cp312-manylinux_2_17_x86_64.whl"
    wheel = directory / filename
    dist_info = f"uc_manager-{version}.dist-info"
    entries: dict[str, bytes] = {
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: uc-manager\n"
            f"Version: {version}\nRequires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
            "Tag: cp312-cp312-manylinux_2_17_x86_64\n\n"
        ).encode(),
        f"{dist_info}/ucm-build.json": json.dumps(
            {
                "schema_version": 1,
                "spec_id": "cuda130-amd64",
                "source_commit": "d" * 40,
                "build_context_digest": "sha256:" + "e" * 64,
                "accelerator": "cuda",
                "accelerator_runtime": runtime,
                "npu_arch_or_na": "na",
                "os": "ubuntu-22.04",
                "cpu_arch": "amd64",
                "python_abi": "cp312",
                "binary_profile_id": "release-cuda130",
            },
            sort_keys=True,
        ).encode(),
        "ucm/__init__.py": b"__version__ = 'fixture'\n",
    }
    if include_native:
        entries["ucm/ucm_custom_ops.cpython-312-linux.so"] = b"\x7fELFsynthetic"
    rows: list[list[str]] = []
    for name, data in entries.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        rows.append([name, f"sha256={digest}", str(len(data))])
    rows.append([f"{dist_info}/RECORD", "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return wheel


def _elf64(
    *,
    machine: int = 62,
    needed: tuple[str, ...] = ("libc.so.6",),
    rpath: str | None = None,
    leaked_path: bytes = b"",
) -> bytes:
    """Build the smallest ELF64 image needed to exercise the real parser."""
    strings = bytearray(b"\0")
    offsets: dict[str, int] = {}
    for value in (*needed, *((rpath,) if rpath is not None else ())):
        offsets[value] = len(strings)
        strings.extend(value.encode("utf-8") + b"\0")
    dynamic: list[tuple[int, int]] = []
    dynamic_offset = 64 + 2 * 56
    dynamic_size = (3 + len(needed) + (1 if rpath is not None else 0)) * 16
    strings_offset = dynamic_offset + dynamic_size
    dynamic.extend([(5, strings_offset), (10, len(strings))])
    dynamic.extend((1, offsets[value]) for value in needed)
    if rpath is not None:
        dynamic.append((29, offsets[rpath]))
    dynamic.append((0, 0))
    dynamic_bytes = b"".join(struct.pack("<QQ", *entry) for entry in dynamic)
    payload = dynamic_bytes + bytes(strings) + leaked_path
    file_size = dynamic_offset + len(payload)
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
    header = ident + struct.pack(
        "<HHIQQQIHHHHHH",
        3,
        machine,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        2,
        0,
        0,
        0,
    )
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, file_size, file_size, 0x1000)
    dyn = struct.pack(
        "<IIQQQQQQ",
        2,
        4,
        dynamic_offset,
        dynamic_offset,
        dynamic_offset,
        len(dynamic_bytes),
        len(dynamic_bytes),
        8,
    )
    return header + load + dyn + payload


def _native_component_manifest(
    *,
    profile_id: str = "cuda130",
    spec_id: str = "cuda130-amd64",
    source_sha: str = REVIEWED_SOURCE_SHA,
    build_key: str = CUDA_AMD64_BUILD_KEY,
    cpu_arch: str = "amd64",
    required: list[str] | None = None,
    forbidden: list[str] | None = None,
) -> bytes:
    value = {
        "schema_version": 1,
        "kind": "ucm-native-components",
        "profile_id": profile_id,
        "spec_id": spec_id,
        "source_sha": source_sha,
        "build_key": build_key,
        "version": "0.5.0rc1+cuda130",
        "cpu_arch": cpu_arch,
        "required_native": required or CUDA_REQUIRED_NATIVE,
        "forbidden_native": forbidden or CUDA_FORBIDDEN_NATIVE,
        "installed_targets": required or CUDA_REQUIRED_NATIVE,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _raw_native_wheel(
    directory: Path,
    *,
    version: str = "0.5.0rc1+cuda130",
    profile_id: str = "cuda130",
    spec_id: str = "cuda130-amd64",
    build_key: str = CUDA_AMD64_BUILD_KEY,
    forbidden: list[str] | None = None,
    required: list[str] | None = None,
    extra_components: tuple[str, ...] = (),
    machine: int = 62,
    needed: tuple[str, ...] = ("libc.so.6",),
    rpath: str | None = None,
    leaked_path: bytes = b"",
    manifest_overrides: dict[str, object] | None = None,
) -> Path:
    required = required or CUDA_REQUIRED_NATIVE
    forbidden = forbidden or CUDA_FORBIDDEN_NATIVE
    dist_info = f"uc_manager-{version}.dist-info"
    entries: dict[str, bytes] = {
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: uc-manager\n"
            f"Version: {version}\nRequires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
            "Tag: cp312-cp312-linux_x86_64\n\n"
        ).encode(),
        "ucm/__init__.py": f"__version__ = {version!r}\n".encode(),
    }
    manifest = json.loads(
        _native_component_manifest(
            profile_id=profile_id,
            spec_id=spec_id,
            build_key=build_key,
            required=required,
            forbidden=forbidden,
        )
    )
    manifest["version"] = version
    if manifest_overrides:
        manifest.update(manifest_overrides)
    entries["ucm/ucm-native-components.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    elf = _elf64(
        machine=machine,
        needed=needed,
        rpath=rpath,
        leaked_path=leaked_path,
    )
    for component in [*required, *extra_components]:
        entries[NATIVE_MEMBERS[component]] = elf
    raw = directory / f"uc_manager-{version}-cp312-cp312-linux_x86_64.whl"
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return raw


def _seal_native_wheel(
    tmp_path: Path,
    raw: Path,
    *,
    spec_id: str = "cuda130-amd64",
    source_sha: str = REVIEWED_SOURCE_SHA,
    build_key: str = CUDA_AMD64_BUILD_KEY,
    authority_mutation=None,
    closure_resolved: tuple[str, ...] = ("libc.so.6",),
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    matrix = release_core.build_matrix("feature-candidate")
    task = next(item for item in matrix["tasks"] if item["spec_id"] == spec_id)
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    tool_wheels = {
        value["filename"]: value["sha256"]
        for value in release["python_build_lock"]["packages"].values()
    }
    cmake = release["python_build_lock"]["cmake"]["artifacts"][task["cpu_arch"]]
    tool_wheels[cmake["filename"]] = cmake["sha256"]
    source_tree = _git(ROOT, "rev-parse", f"{REVIEWED_SOURCE_SHA}^{{tree}}")
    root = task["builder"]["root"]
    authority = {
        "schema_version": 1,
        "kind": "ucm-native-build-authority",
        "spec_id": spec_id,
        "profile_id": task["profile_id"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "wheel_version": task["wheel_version"],
        "source_sha": REVIEWED_SOURCE_SHA,
        "source_tree": source_tree,
        "source_archive_sha256": "sha256:"
        + hashlib.sha256(b"unit-test-source-archive").hexdigest(),
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "task_sha256": task["task_sha256"],
        "builder_coordinate": f"{root['repository']}@{root['manifest_digest']}",
        "builder_config_digest": root["config_digest"],
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "tool_wheels": dict(sorted(tool_wheels.items())),
        "required_native": task["required_native"],
        "forbidden_native": task["forbidden_native"],
    }
    if authority_mutation is not None:
        authority_mutation(authority)
    authority["build_context_sha256"] = (
        "sha256:"
        + hashlib.sha256(b"ucm-build-context-v1\0unit-test-source-archive").hexdigest()
    )
    authority_path = tmp_path / "build-authority.json"
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_bytes(release_core.canonical_bytes(authority) + b"\n")

    with zipfile.ZipFile(raw) as archive:
        native_names = sorted(
            item.filename
            for item in archive.infolist()
            if not item.is_dir() and item.filename.endswith(".so")
        )
    closure_members = {}
    for name in native_names:
        resolutions = [
            {
                "dependency": needed,
                "direct": True,
                "kind": "external",
                "path": f"/verified-root/{needed}",
                "sha256": "sha256:" + hashlib.sha256(needed.encode()).hexdigest(),
            }
            for needed in closure_resolved
        ]
        closure_members[name] = {
            "dt_needed": list(closure_resolved),
            "resolved_dependencies": resolutions,
            "unresolved_dependencies": [],
        }
    closure = {
        "schema_version": 1,
        "kind": "ucm-linux-dependency-closure",
        "spec_id": spec_id,
        "raw_wheel_sha256": "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest(),
        "build_context_sha256": authority["build_context_sha256"],
        "native_members": closure_members,
        "unresolved_dependencies": [],
    }
    closure["closure_sha256"] = (
        "sha256:" + hashlib.sha256(release_core.canonical_bytes(closure)).hexdigest()
    )
    closure_path = tmp_path / "dependency-closure.json"
    closure_path.write_bytes(release_core.canonical_bytes(closure) + b"\n")
    return _run(
        "wheel",
        "seal",
        str(raw),
        "--spec-id",
        spec_id,
        "--source-sha",
        source_sha,
        "--build-key",
        build_key,
        "--source-date-epoch",
        str(SOURCE_DATE_EPOCH),
        "--authority-file",
        str(authority_path),
        "--dependency-closure",
        str(closure_path),
        "--output-dir",
        str(tmp_path / "sealed"),
        check=check,
    )


def _rewrite_zip(
    source: Path,
    output: Path,
    transform,
    *,
    reverse: bool = False,
    mode: int = 0o644,
) -> None:
    with zipfile.ZipFile(source) as archive:
        entries = {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
            if not item.is_dir()
        }
    transform(entries)
    record_name = next(name for name in entries if name.endswith(".dist-info/RECORD"))
    entries.pop(record_name)
    rows: list[list[str]] = []
    for name in sorted(entries):
        data = entries[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append([name, f"sha256={digest.decode()}", str(len(data))])
    rows.append([record_name, "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[record_name] = record.getvalue().encode()
    names = sorted(entries, reverse=reverse)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(2023, 11, 14, 22, 13, 20))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = mode << 16
            archive.writestr(info, entries[name])


def _write_configs(
    directory: Path,
    release: dict,
    compatibility: dict | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    release_path = directory / "release.yaml"
    compatibility_path = directory / "compatibility.yaml"
    release_path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    compatibility_path.write_text(
        yaml.safe_dump(
            compatibility
            or yaml.safe_load(
                (RELEASE_ROOT / "compatibility.yaml").read_text(encoding="utf-8")
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return release_path, compatibility_path


def _resolved_cuda_release() -> dict:
    return yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())


def _write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)


def test_real_release_matrix_is_exact_immutable_and_stable() -> None:
    first = json.loads(_run("core", "matrix", "--lane", "feature-candidate").stdout)
    second = json.loads(_run("core", "matrix", "--lane", "feature-candidate").stdout)

    assert first == second
    assert first["kind"] == "ucm-real-wheel-matrix"
    assert first["lane"] == "feature-candidate"
    assert [task["spec_id"] for task in first["tasks"]] == EXPECTED_TASK_IDS
    assert len(first["tasks"]) == 6
    assert {task["platform"] for task in first["tasks"]} == {
        "linux/amd64",
        "linux/arm64",
    }
    assert {task["runner"] for task in first["tasks"]} == {
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
    }
    assert {task["wheel_version"] for task in first["tasks"]} == {
        "0.5.0rc1+cuda130",
        "0.5.0rc1+cann900.a2",
        "0.5.0rc1+cann900.a3",
    }
    assert {
        f"{task['target_repository']}:{task['target_tag']}" for task in first["tasks"]
    } == EXPECTED_TARGETS
    assert all(task["write_authority"] == [] for task in first["tasks"])
    assert all(task["build_eligible"] is True for task in first["tasks"])
    assert all(task["task_sha256"].startswith("sha256:") for task in first["tasks"])
    assert first["matrix_sha256"].startswith("sha256:")


def test_release_config_carries_exact_builder_runtime_and_dependency_authorities() -> (
    None
):
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())

    assert [profile["id"] for profile in release["wheel_profiles"]] == [
        "cuda130",
        "cann900-a2",
        "cann900-a3",
    ]
    assert release["runner_map"] == {
        "amd64": "ubuntu-24.04",
        "arm64": "ubuntu-24.04-arm",
    }
    assert len(release["image_families"]) == 3
    assert (
        len({family["target_repository"] for family in release["image_families"]}) == 2
    )
    assert {
        f"{family['target_repository']}:{family['target_tag']}"
        for family in release["image_families"]
    } == EXPECTED_TARGETS

    cuda = release["wheel_profiles"][0]
    assert cuda["builders"]["amd64"]["root"] == {
        "repository": "docker.io/pytorch/manylinux2_28-builder",
        "tag": "cuda13.0",
        "index_digest": "sha256:83d73c3fd2782b23de8a1873820236273d8a6db911aea15c017766c9a40e723c",
        "manifest_digest": "sha256:746796491b3a375ee352c60ad1265c599bb1aa1762a0de46927e0f4139832918",
        "config_digest": "sha256:0c34d69ef0b04dbf678564146d299bdad59f7d2b8e166b4f59df5e2fce3a34f2",
    }
    assert cuda["builders"]["arm64"]["root"]["manifest_digest"] == (
        "sha256:48eb3eb1b3ab79fb30e49d3692a60dc15f05a3d1c4ba328400af5f06b2e6949c"
    )
    assert cuda["builders"]["amd64"]["sources"] == []

    families = {family["id"]: family for family in release["image_families"]}
    assert families["cuda130"]["runtime"]["members"]["amd64"] == {
        "manifest_digest": "sha256:4ac9b7c6dabc3ec762c0edef4e9245abe98373844da91cc53ee42e5c58280c5b",
        "config_digest": "sha256:2497255b1272ba3ae9581acd51349f840038f228d0709cd9f6a142d39008d290",
    }
    assert (
        families["cann900-a2"]["runtime"]["members"]["arm64"]["manifest_digest"]
        == "sha256:638fc04eaa3654fcf14688096ed4e9d88ea0d905fa8685eed4b36d5fffe8fd8d"
    )
    assert families["cann900-a3"]["runtime"]["index_digest"] == (
        "sha256:e3d89f09a1c1d85f0ec6a1cc26e3c807b7bc8a7ec0f97a830dbef63ab50d8f81"
    )

    assert release["python_build_lock"]["packages"]["build"] == {
        "version": "1.3.0",
        "filename": "build-1.3.0-py3-none-any.whl",
        "sha256": "sha256:7145f0b5061ba90a1500d60bd1b13ca0a8a4cebdd0cc16ed8adf1c0e739f43b4",
    }
    assert (
        release["python_build_lock"]["cmake"]["artifacts"]["arm64"]["sha256"]
        == "sha256:42d9883b8958da285d53d5f69d40d9650c2d1bcf922d82b3ebdceb2b3a7d4521"
    )
    assert release["wrapt_wheels"]["amd64"]["sha256"] == (
        "sha256:bc570b5f14a79734437cb7b0500376b6b791153314986074486e0b0fa8d71d98"
    )
    assert all(
        "libmetrics.so" in profile["allowed_dt_needed"]
        for profile in release["wheel_profiles"]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-profile", "exact production profile set"),
        ("extra-profile", "array is longer than maxItems"),
        ("swapped-architecture-digest", "canonical release authority"),
        ("mutable-tag-only-authority", "missing required properties"),
        ("basename-only-evil-repository", "expected one of"),
        ("unresolved-lock", "missing required properties"),
        ("duplicated-public-coordinate", "public image coordinates must be unique"),
        ("caller-raw-runner", "Additional properties are not allowed"),
    ],
)
def test_release_authority_mutations_fail_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    if mutation == "missing-profile":
        release["wheel_profiles"].pop()
    elif mutation == "extra-profile":
        extra = _clone(release["wheel_profiles"][-1])
        assert isinstance(extra, dict)
        extra["id"] = "cann900-a5"
        release["wheel_profiles"].append(extra)
    elif mutation == "swapped-architecture-digest":
        members = release["image_families"][0]["runtime"]["members"]
        members["amd64"]["manifest_digest"], members["arm64"]["manifest_digest"] = (
            members["arm64"]["manifest_digest"],
            members["amd64"]["manifest_digest"],
        )
    elif mutation == "mutable-tag-only-authority":
        del release["image_families"][0]["runtime"]["index_digest"]
    elif mutation == "basename-only-evil-repository":
        release["image_families"][1]["runtime"][
            "repository"
        ] = "evil.example/ascend/vllm-ascend"
    elif mutation == "unresolved-lock":
        del release["python_build_lock"]["packages"]["wheel"]["sha256"]
    elif mutation == "duplicated-public-coordinate":
        release["image_families"][2]["target_tag"] = release["image_families"][1][
            "target_tag"
        ]
    elif mutation == "caller-raw-runner":
        release["wheel_profiles"][0]["runner"] = "self-hosted"
    rejected = _reject_config(tmp_path / mutation, release)
    assert rejected.returncode == 2
    assert message in rejected.stderr


def test_feature_preflight_has_no_write_authority_without_identity_arguments() -> None:
    feature = json.loads(
        _run("core", "tag-preflight", "--lane", "feature-candidate").stdout
    )
    assert feature["publication_allowed"] is False
    assert feature["write_authority"] == []


def test_tag_preflight_rejects_caller_supplied_identity_arguments() -> None:
    rejected = _run(
        "core",
        "tag-preflight",
        "--lane",
        "protected-tag",
        "--repository",
        "SuperMarioYL/unified-cache-management",
        "--repository-owner",
        "SuperMarioYL",
        "--ref-name",
        "v0.5.0rc1",
        "--source-sha",
        "a" * 40,
        "--default-branch",
        "develop",
        "--ref-protected",
        "true",
        check=False,
    )
    assert rejected.returncode == 2
    assert "unrecognized arguments" in rejected.stderr


def test_tag_preflight_binds_actual_tag_head_and_default_branch_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, _, tag_sha, default_branch_sha = _protected_tag_repository(
        tmp_path
    )
    protected = _run_protected_tag_preflight(monkeypatch, repository, environment)
    assert protected["publication_allowed"] is True
    assert protected["write_authority"] == [
        "github-prerelease",
        "ghcr-final-index",
        "ghcr-private-staging",
    ]
    assert protected["ref"] == "refs/tags/v0.5.0rc1"
    assert protected["ref_type"] == "tag"
    assert protected["source_sha"] == tag_sha
    assert protected["tag_commit_sha"] == tag_sha
    assert protected["checked_head_sha"] == tag_sha
    assert protected["default_branch_ref"] == "refs/remotes/origin/develop"
    assert protected["default_branch_sha"] == default_branch_sha


def test_tag_preflight_rejects_nonexistent_context_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, event_path, _, _ = _protected_tag_repository(tmp_path)
    nonexistent_sha = "a" * 40
    environment["GITHUB_SHA"] = nonexistent_sha
    _rewrite_event(event_path, after=nonexistent_sha)
    with pytest.raises(ValueError, match="source_sha"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_tag_preflight_rejects_branch_named_like_release_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, event_path, _, _ = _protected_tag_repository(tmp_path)
    environment["GITHUB_REF"] = "refs/heads/v0.5.0rc1"
    environment["GITHUB_REF_TYPE"] = "branch"
    _rewrite_event(event_path, ref="refs/heads/v0.5.0rc1")
    with pytest.raises(ValueError, match="ref"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_tag_preflight_rejects_wrong_actor_even_when_event_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, event_path, _, _ = _protected_tag_repository(tmp_path)
    environment["GITHUB_ACTOR"] = "attacker"
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["sender"] = {"login": "attacker"}
    event_path.write_text(json.dumps(event), encoding="utf-8")
    with pytest.raises(ValueError, match="actor"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_tag_preflight_rejects_tag_commit_different_from_checked_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, _, _, default_branch_sha = _protected_tag_repository(
        tmp_path
    )
    _git(repository, "checkout", "--detach", default_branch_sha)
    with pytest.raises(ValueError, match="checked_head"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_tag_preflight_rejects_tag_unreachable_from_origin_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, event_path, _, _ = _protected_tag_repository(tmp_path)
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    unrelated_sha = _git(repository, "commit-tree", tree, input_text="unrelated\n")
    _git(repository, "tag", "--force", "v0.5.0rc1", unrelated_sha)
    _git(repository, "checkout", "--detach", unrelated_sha)
    environment["GITHUB_SHA"] = unrelated_sha
    _rewrite_event(event_path, after=unrelated_sha)
    with pytest.raises(ValueError, match="default_branch_ancestry"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_tag_preflight_rejects_forged_environment_event_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, _, _, _ = _protected_tag_repository(tmp_path)
    environment["GITHUB_REPOSITORY"] = "attacker/unified-cache-management"
    with pytest.raises(ValueError, match="repository"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_tag_preflight_rejects_forged_release_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, environment, _, _, _ = _protected_tag_repository(tmp_path)
    environment["UCM_RELEASE_POLICY"] = "caller-claimed"
    with pytest.raises(ValueError, match="release_policy"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)


def test_config_is_strict_and_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    valid = json.loads(_run("config", "validate").stdout)
    assert valid == {
        "compatibility_rules": 2,
        "schema_version": 1,
        "wheel_profiles": 3,
    }

    release_config = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    release_config["unexpected"] = True
    bad_release = tmp_path / "release.yaml"
    bad_release.write_text(yaml.safe_dump(release_config), encoding="utf-8")
    rejected = _run(
        "config",
        "validate",
        "--release",
        str(bad_release),
        check=False,
    )
    assert rejected.returncode == 2
    assert "Additional properties are not allowed" in rejected.stderr

    schemas = tmp_path / "schemas"
    shutil.copytree(RELEASE_ROOT / "schemas", schemas)
    schema = schemas / "config.schema.json"
    text = schema.read_text(encoding="utf-8")
    schema.write_text(
        text.replace('"$schema":', '"$schema": "duplicate",\n  "$schema":', 1)
    )
    duplicate = _run(
        "config",
        "validate",
        "--schema-dir",
        str(schemas),
        check=False,
    )
    assert duplicate.returncode == 2
    assert "duplicate JSON key" in duplicate.stderr


def test_schema_validation_is_operational_for_configs_and_manifest(
    tmp_path: Path,
) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    assert release["kind"] == "release-config"
    compatibility = yaml.safe_load((RELEASE_ROOT / "compatibility.yaml").read_text())
    assert compatibility["kind"] == "compatibility-config"
    schema = json.loads((RELEASE_ROOT / "schemas/config.schema.json").read_text())
    assert schema["oneOf"] == [
        {"$ref": "#/$defs/release"},
        {"$ref": "#/$defs/compatibility"},
    ]

    release["schema_version"] = "1"
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    wrong_type = _run(
        "config",
        "validate",
        "--release",
        str(release_path),
        "--compatibility",
        str(compatibility_path),
        check=False,
    )
    assert wrong_type.returncode == 2
    assert "expected integer" in wrong_type.stderr

    schemas = tmp_path / "schemas"
    shutil.copytree(RELEASE_ROOT / "schemas", schemas)
    manifest_schema = json.loads((schemas / "release-manifest.schema.json").read_text())
    manifest_schema["properties"]["kind"]["const"] = "schema-was-not-applied"
    (schemas / "release-manifest.schema.json").write_text(
        json.dumps(manifest_schema), encoding="utf-8"
    )
    rejected_manifest = _run("core", "plan", "--schema-dir", str(schemas), check=False)
    assert rejected_manifest.returncode == 2
    assert "schema-was-not-applied" in rejected_manifest.stderr


def test_schema_refs_enforce_required_enum_pattern_and_unique_items(
    tmp_path: Path,
) -> None:
    original = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    mutations: list[tuple[dict, str]] = []

    missing = json.loads(json.dumps(original))
    del missing["chart"]["name"]
    mutations.append((missing, "missing required properties"))

    wrong_enum = json.loads(json.dumps(original))
    wrong_enum["chart"]["validation_cases"][0]["name"] = "gpu"
    mutations.append((wrong_enum, "expected one of"))

    wrong_pattern = json.loads(json.dumps(original))
    first_chart_case = wrong_pattern["chart"]["validation_cases"][0]
    first_chart_case["image_digest"] = "sha256:not-a-digest"
    mutations.append((wrong_pattern, "does not match pattern"))

    duplicate_array = json.loads(json.dumps(original))
    duplicate_array["wheel_profiles"][0]["cpu_arch"] = ["amd64", "amd64"]
    mutations.append((duplicate_array, "array items must be unique"))

    for index, (release, message) in enumerate(mutations):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        release_path, compatibility_path = _write_configs(case_dir, release)
        rejected = _run(
            "config",
            "validate",
            "--release",
            str(release_path),
            "--compatibility",
            str(compatibility_path),
            check=False,
        )
        assert rejected.returncode == 2
        assert message in rejected.stderr


def test_compatibility_and_profile_references_cannot_drift(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    compatibility = yaml.safe_load((RELEASE_ROOT / "compatibility.yaml").read_text())
    compatibility["rules"][0]["accelerator_runtimes"] = [
        "cuda-13.0",
        "cuda-12.9",
    ]
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    drift = _run(
        "config",
        "validate",
        "--release",
        str(release_path),
        "--compatibility",
        str(compatibility_path),
        check=False,
    )
    assert drift.returncode == 2
    assert "compatibility/profile drift" in drift.stderr


def test_all_six_specs_derive_resolved_immutable_authorities() -> None:
    manifest = json.loads(_run("core", "plan", "--require-publishable").stdout)
    assert manifest["eligible_wheel_count"] == 6
    assert all(spec["build_eligible"] for spec in manifest["wheel_specs"])
    assert all(spec["blocked_reasons"] == [] for spec in manifest["wheel_specs"])
    assert all(
        lock["status"] == "resolved" and "@sha256:" in lock["identity"]
        for spec in manifest["wheel_specs"]
        for lock in spec["locks"]
    )
    assert all(
        spec["runner"]["identity"].startswith("runner://github-hosted/")
        for spec in manifest["wheel_specs"]
    )


def test_core_plan_contains_the_exact_six_buildable_specs(tmp_path: Path) -> None:
    output = tmp_path / "release-manifest.json"
    planned = _run("core", "plan", "--output", str(output))
    manifest = json.loads(planned.stdout)
    assert manifest == json.loads(output.read_text(encoding="utf-8"))
    assert manifest["ucm_version"] == "0.5.0rc1"
    assert manifest["declared_wheel_count"] == 6
    assert manifest["eligible_wheel_count"] == 6
    assert len(manifest["wheel_specs"]) == 6
    assert {item["accelerator"] for item in manifest["wheel_specs"]} == {
        "cuda",
        "ascend",
    }
    assert all(item["build_eligible"] for item in manifest["wheel_specs"])
    assert all(item["blocked_reasons"] == [] for item in manifest["wheel_specs"])
    assert manifest["publication"]["target"] == "github-release"
    assert {item["type"] for item in manifest["publication"]["assets"]} == {
        "wheel",
        "helm-chart",
    }
    assert "wrapt" not in json.dumps(manifest).lower()

    publishable = _run("core", "plan", "--require-publishable")
    assert json.loads(publishable.stdout)["status"] == "candidate"


def test_setup_chart_and_configuration_share_version_authority() -> None:
    version = (
        (ROOT / "version.ini").read_text(encoding="utf-8").strip().split("=", 1)[1]
    )
    setup_version = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    release_config = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    chart = yaml.safe_load((ROOT / "charts" / "ucm" / "Chart.yaml").read_text())
    assert setup_version == version == release_config["ucm_version"]
    assert str(chart["appVersion"]) == version
    assert chart["version"] == "0.5.0-rc.1"
    assert release_config["python_runtime_dependencies"] == ["wrapt==1.17.2"]
    assert derive_chart_version(version) == "0.5.0-rc.1"


def test_coordinated_config_version_drift_is_rejected(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    compatibility = yaml.safe_load((RELEASE_ROOT / "compatibility.yaml").read_text())
    release["ucm_version"] = release["chart"]["app_version"] = "0.5.0rc2"
    release["chart"]["version"] = "0.5.0-rc.2"
    compatibility["ucm_version"] = "0.5.0rc2"
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    drift = _run(
        "config",
        "validate",
        "--release",
        str(release_path),
        "--compatibility",
        str(compatibility_path),
        check=False,
    )
    assert drift.returncode == 2
    assert "does not match version.ini" in drift.stderr


def test_wheel_inspection_binds_sha_metadata_version_and_spec(tmp_path: Path) -> None:
    wheel = _fixture_wheel(tmp_path)
    digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    inspected = json.loads(
        _run(
            "wheel",
            "inspect",
            str(wheel),
            "--spec-id",
            "cuda130-amd64",
            "--expected-sha256",
            digest,
            "--source-kind",
            "fixture",
        ).stdout
    )
    assert inspected["sha256"] == digest
    assert inspected["distribution"] == "uc-manager"
    assert inspected["version"] == "0.5.0rc1"
    assert inspected["requires_dist"] == ["wrapt==1.17.2"]
    assert inspected["python_abi"] == "cp312"
    assert inspected["cpu_arch"] == "amd64"
    assert inspected["status"] == "fixture-only"
    assert inspected["publication_eligible"] is False

    wrong = _run(
        "wheel",
        "inspect",
        str(wheel),
        "--spec-id",
        inspected["spec_id"],
        "--expected-sha256",
        "sha256:" + "0" * 64,
        "--source-kind",
        "fixture",
        check=False,
    )
    assert wrong.returncode == 2
    assert "wheel SHA256 mismatch" in wrong.stderr


def test_synthetic_wheel_is_only_an_unpublished_builder_candidate(
    tmp_path: Path,
) -> None:
    spec_id = "cuda130-amd64"
    synthetic = _builder_candidate_wheel(tmp_path, include_native=True)
    _drop_record(synthetic)
    digest = "sha256:" + hashlib.sha256(synthetic.read_bytes()).hexdigest()
    production_lane = _run(
        "wheel",
        "inspect",
        str(synthetic),
        "--spec-id",
        spec_id,
        "--expected-sha256",
        digest,
        "--source-kind",
        "production",
        check=False,
    )
    assert production_lane.returncode == 2
    assert "invalid choice: 'production'" in production_lane.stderr

    legacy = _builder_candidate_wheel(tmp_path, include_native=True)
    legacy_digest = "sha256:" + hashlib.sha256(legacy.read_bytes()).hexdigest()
    rejected_legacy = _run(
        "wheel",
        "inspect",
        str(legacy),
        "--spec-id",
        spec_id,
        "--expected-sha256",
        legacy_digest,
        "--source-kind",
        "builder-candidate",
        check=False,
    )
    assert rejected_legacy.returncode == 2
    assert (
        "ucm_custom_ops" in rejected_legacy.stderr
        or "binding" in rejected_legacy.stderr
        or "authority" in rejected_legacy.stderr
    )


def test_release_setup_requires_controlled_values_and_uses_local_version(
    tmp_path: Path,
) -> None:
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UCM_RELEASE_") and key != "SOURCE_DATE_EPOCH"
    }
    missing_platform = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        env={**base_env, "UCM_RELEASE_BUILD": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_platform.returncode != 0
    assert "PLATFORM" in missing_platform.stderr

    authority_root = tmp_path / "setup-authority"
    authority_root.mkdir()
    raw = _raw_native_wheel(authority_root)
    setup_arch = (
        "arm64" if os.uname().machine.lower() in {"arm64", "aarch64"} else "amd64"
    )
    setup_spec = f"cuda130-{setup_arch}"
    setup_task = next(
        item
        for item in release_core.build_matrix("feature-candidate")["tasks"]
        if item["spec_id"] == setup_spec
    )
    _seal_native_wheel(
        authority_root,
        raw,
        spec_id=setup_spec,
        build_key=setup_task["task_sha256"],
        check=False,
    )
    release_env = {
        **base_env,
        "UCM_RELEASE_BUILD": "1",
        "PLATFORM": "cuda",
        "UCM_RELEASE_PROFILE": "cuda130",
        "UCM_RELEASE_SOURCE_SHA": REVIEWED_SOURCE_SHA,
        "UCM_RELEASE_VERSION": "0.5.0rc1+cuda130",
        "UCM_RELEASE_BUILD_KEY": setup_task["task_sha256"],
        "UCM_RELEASE_REQUIRED_TARGETS": ",".join(CUDA_REQUIRED_NATIVE),
        "UCM_RELEASE_FORBIDDEN_TARGETS": ",".join(CUDA_FORBIDDEN_NATIVE),
        "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        "UCM_RELEASE_AUTHORITY_FILE": str(authority_root / "build-authority.json"),
    }
    controlled = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        env=release_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert controlled.returncode == 0, controlled.stderr
    assert controlled.stdout.strip() == "0.5.0rc1+cuda130"

    wrong = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        env={**release_env, "UCM_RELEASE_VERSION": "0.5.0rc1+cann900.a2"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert wrong.returncode != 0
    assert "version" in wrong.stderr.lower()


def test_release_setup_rejects_self_consistent_caller_forged_authority() -> None:
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UCM_RELEASE_") and key != "SOURCE_DATE_EPOCH"
    }
    forged = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        env={
            **base_env,
            "UCM_RELEASE_BUILD": "1",
            "PLATFORM": "cuda",
            "UCM_RELEASE_PROFILE": "cuda999",
            "UCM_RELEASE_SOURCE_SHA": "0" * 40,
            "UCM_RELEASE_VERSION": "0.5.0rc1+cuda999",
            "UCM_RELEASE_BUILD_KEY": "sha256:" + "0" * 64,
            "UCM_RELEASE_REQUIRED_TARGETS": "foo",
            "UCM_RELEASE_FORBIDDEN_TARGETS": "bar",
            "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert forged.returncode != 0
    assert "authority" in forged.stderr.lower()


def test_wheel_seal_is_deterministic_and_inspection_recomputes_exact_native_evidence(
    tmp_path: Path,
) -> None:
    raw = _raw_native_wheel(tmp_path)
    first = json.loads(_seal_native_wheel(tmp_path / "first", raw).stdout)
    second = json.loads(_seal_native_wheel(tmp_path / "second", raw).stdout)
    first_wheel = Path(first["wheel_path"])
    second_wheel = Path(second["wheel_path"])
    assert first_wheel.name == (
        "uc_manager-0.5.0rc1+cuda130-cp312-cp312-manylinux_2_28_x86_64.whl"
    )
    assert first_wheel.read_bytes() == second_wheel.read_bytes()
    assert first["wheel_sha256"] == second["wheel_sha256"]

    inspected = json.loads(
        _run(
            "wheel",
            "inspect",
            str(first_wheel),
            "--spec-id",
            "cuda130-amd64",
            "--expected-sha256",
            first["wheel_sha256"],
            "--source-kind",
            "builder-candidate",
        ).stdout
    )
    evidence = inspected["builder_evidence"]
    assert evidence["source_commit"] == REVIEWED_SOURCE_SHA
    assert evidence["build_key"] == CUDA_AMD64_BUILD_KEY
    assert evidence["native_components"] == CUDA_REQUIRED_NATIVE
    assert evidence["elf_machines"] == ["EM_X86_64"]
    assert evidence["unresolved_dependencies"] == []
    assert evidence["record_status"] == "passed"
    assert inspected["version"] == "0.5.0rc1+cuda130"
    assert inspected["status"] == "candidate-inspected"
    assert inspected["publication_eligible"] is False

    with zipfile.ZipFile(first_wheel) as archive:
        infos = archive.infolist()
        assert [item.filename for item in infos] == sorted(
            item.filename for item in infos
        )
        assert all(item.date_time == (2023, 11, 14, 22, 13, 20) for item in infos)
        assert all(item.external_attr >> 16 == 0o644 for item in infos)
        assert all(item.create_system == 3 for item in infos)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("bad-source", "source"),
        ("wrong-profile", "profile"),
        ("wrong-architecture", "architecture"),
        ("incomplete-native", "required native"),
        ("forbidden-native", "forbidden native"),
        ("mooncake-on-cuda", "forbidden native"),
        ("wrong-elf-machine", "ELF machine"),
        ("unapproved-needed", "DT_NEEDED"),
        ("rpath", "RPATH"),
        ("host-path", "path leakage"),
    ],
)
def test_wheel_seal_rejects_invalid_native_builds(
    tmp_path: Path, mutation: str, message: str
) -> None:
    kwargs: dict[str, object] = {}
    seal_kwargs: dict[str, str] = {}
    if mutation == "bad-source":
        seal_kwargs["source_sha"] = "not-a-commit"
    elif mutation == "wrong-profile":
        kwargs["manifest_overrides"] = {"profile_id": "cann900-a2"}
    elif mutation == "wrong-architecture":
        kwargs["manifest_overrides"] = {"cpu_arch": "arm64"}
    elif mutation == "incomplete-native":
        kwargs["required"] = CUDA_REQUIRED_NATIVE[:-1]
    elif mutation == "forbidden-native":
        kwargs["extra_components"] = ("ds3fsstore",)
    elif mutation == "mooncake-on-cuda":
        kwargs["extra_components"] = ("mooncakestore",)
    elif mutation == "wrong-elf-machine":
        kwargs["machine"] = 183
    elif mutation == "unapproved-needed":
        kwargs["needed"] = ("libc.so.6", "libevil.so")
    elif mutation == "rpath":
        kwargs["rpath"] = "/tmp/host-libs"
    elif mutation == "host-path":
        kwargs["leaked_path"] = b"/Users/builder/private/source.cc\0"
    raw = _raw_native_wheel(tmp_path, **kwargs)
    rejected = _seal_native_wheel(tmp_path, raw, check=False, **seal_kwargs)
    assert rejected.returncode == 2
    assert message.lower() in rejected.stderr.lower()


def test_wheel_seal_requires_mooncake_for_ascend(tmp_path: Path) -> None:
    ascend_required = CUDA_REQUIRED_NATIVE + ["mooncakestore"]
    raw = _raw_native_wheel(
        tmp_path,
        version="0.5.0rc1+cann900.a2",
        profile_id="cann900-a2",
        spec_id="cann900-a2-amd64",
        build_key=A2_AMD64_BUILD_KEY,
        forbidden=CUDA_FORBIDDEN_NATIVE[1:],
        required=CUDA_REQUIRED_NATIVE,
    )
    rejected = _seal_native_wheel(
        tmp_path,
        raw,
        spec_id="cann900-a2-amd64",
        build_key=A2_AMD64_BUILD_KEY,
        check=False,
    )
    assert rejected.returncode == 2
    assert "mooncakestore" in rejected.stderr
    assert ascend_required[-1] == "mooncakestore"


@pytest.mark.parametrize(
    ("component", "moved_member"),
    (
        ("metrics", "arbitrary/location/libmetrics.so"),
        ("ucmtrans", "ucm/shared/trans/ucmtrans.so"),
    ),
)
def test_wheel_seal_rejects_native_member_moved_from_exact_archive_path(
    tmp_path: Path, component: str, moved_member: str
) -> None:
    raw = _raw_native_wheel(tmp_path)

    def move(entries: dict[str, bytes]) -> None:
        entries[moved_member] = entries.pop(NATIVE_MEMBERS[component])

    moved = tmp_path / f"moved-{component}.whl"
    with zipfile.ZipFile(raw) as archive:
        entries = {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
            if not item.is_dir()
        }
    move(entries)
    with zipfile.ZipFile(moved, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    rejected = _seal_native_wheel(tmp_path, moved, check=False)
    assert rejected.returncode == 2
    assert "archive path" in rejected.stderr.lower()


def test_wheel_seal_rejects_allowed_but_unresolved_dependency(tmp_path: Path) -> None:
    raw = _raw_native_wheel(tmp_path, needed=("libcuda.so.1",))
    rejected = _seal_native_wheel(tmp_path, raw, closure_resolved=(), check=False)
    assert rejected.returncode == 2
    assert "unresolved" in rejected.stderr.lower()


def test_wheel_seal_requires_libmetrics_from_exact_wheel_member(
    tmp_path: Path,
) -> None:
    raw = _raw_native_wheel(tmp_path, needed=("libmetrics.so",))
    rejected = _seal_native_wheel(
        tmp_path, raw, closure_resolved=("libmetrics.so",), check=False
    )
    assert rejected.returncode == 2
    assert "exact wheel member" in rejected.stderr.lower()


def test_ldd_closure_rejects_transitive_not_found() -> None:
    with pytest.raises(ValueError, match="transitive.*not found"):
        release_wheel._parse_ldd_output(
            "ucm/store/cache/libcachestore.so",
            ["libc.so.6"],
            """\
libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6 (0x0000ffff)
libtransitive.so.1 => not found
/lib/ld-linux-aarch64.so.1 (0x0000aaaa)
""",
        )


def test_ldd_closure_records_direct_transitive_virtual_and_loader_lines() -> None:
    assert (
        release_wheel._parse_ldd_output(
            "ucm/store/cache/libcachestore.so",
            ["libc.so.6"],
            """\
linux-vdso.so.1 (0x0000ffff)
libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6 (0x00001111)
libgcc_s.so.1 => /lib/aarch64-linux-gnu/libgcc_s.so.1 (0x00002222)
/lib/ld-linux-aarch64.so.1 (0x00003333)
""",
        )
        == [
            {
                "dependency": "/lib/ld-linux-aarch64.so.1",
                "direct": False,
                "kind": "located",
                "path": "/lib/ld-linux-aarch64.so.1",
            },
            {
                "dependency": "libc.so.6",
                "direct": True,
                "kind": "located",
                "path": "/lib/aarch64-linux-gnu/libc.so.6",
            },
            {
                "dependency": "libgcc_s.so.1",
                "direct": False,
                "kind": "located",
                "path": "/lib/aarch64-linux-gnu/libgcc_s.so.1",
            },
            {
                "dependency": "linux-vdso.so.1",
                "direct": False,
                "kind": "virtual",
            },
        ]
    )


@pytest.mark.parametrize(
    "line",
    (
        "libextra.so.1 (0x0000ffff)",
        "unexpected ldd diagnostic",
        "libextra.so.1 => relative/libextra.so.1 (0x0000ffff)",
    ),
)
def test_ldd_closure_rejects_malformed_or_unbound_lines(line: str) -> None:
    with pytest.raises(ValueError, match="malformed|unbound|absolute"):
        release_wheel._parse_ldd_output(
            "ucm/store/cache/libcachestore.so",
            ["libc.so.6"],
            f"libc.so.6 => /lib/libc.so.6 (0x1)\n{line}\n",
        )


def test_source_context_is_a_no_git_canonical_archive_of_actual_bytes(
    tmp_path: Path,
) -> None:
    prepared = json.loads(
        _run(
            "wheel",
            "context",
            "--source-sha",
            REVIEWED_SOURCE_SHA,
            "--output-dir",
            str(tmp_path / "context"),
        ).stdout
    )
    archive = Path(prepared["source_archive_path"])
    manifest = Path(prepared["source_manifest_path"])
    source_root = tmp_path / "source"
    source_root.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(source_root, filter="data")
    assert not (source_root / ".git").exists()

    verified = json.loads(
        _run(
            "wheel",
            "verify-context",
            "--archive",
            str(archive),
            "--manifest",
            str(manifest),
            "--source-root",
            str(source_root),
        ).stdout
    )
    raw = archive.read_bytes()
    assert verified["source_archive_sha256"] == (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )
    assert verified["build_context_sha256"] == (
        "sha256:" + hashlib.sha256(b"ucm-build-context-v1\0" + raw).hexdigest()
    )
    assert verified["source_sha"] == REVIEWED_SOURCE_SHA
    assert verified["source_tree"] == _git(
        ROOT, "rev-parse", f"{REVIEWED_SOURCE_SHA}^{{tree}}"
    )


@pytest.mark.parametrize(
    "injected_name", ("build/injected.so", "dist/uc_manager.whl", "ucm/injected.so")
)
def test_source_context_rejects_ignored_build_artifact_in_extracted_root(
    tmp_path: Path, injected_name: str
) -> None:
    prepared = json.loads(
        _run(
            "wheel",
            "context",
            "--source-sha",
            REVIEWED_SOURCE_SHA,
            "--output-dir",
            str(tmp_path / "context"),
        ).stdout
    )
    archive = Path(prepared["source_archive_path"])
    source_root = tmp_path / "source"
    source_root.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(source_root, filter="data")
    injected = source_root / injected_name
    injected.parent.mkdir(exist_ok=True)
    injected.write_bytes(b"ignored build output")

    rejected = _run(
        "wheel",
        "verify-context",
        "--archive",
        str(archive),
        "--manifest",
        prepared["source_manifest_path"],
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "extra" in rejected.stderr.lower()


@pytest.mark.parametrize("mutation", ("context-digest", "source-sha", "source-tree"))
def test_source_context_rejects_mutated_digest_or_source_identity(
    tmp_path: Path, mutation: str
) -> None:
    prepared = json.loads(
        _run(
            "wheel",
            "context",
            "--source-sha",
            REVIEWED_SOURCE_SHA,
            "--output-dir",
            str(tmp_path / "context"),
        ).stdout
    )
    archive = Path(prepared["source_archive_path"])
    manifest_path = Path(prepared["source_manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    field = {
        "context-digest": "build_context_sha256",
        "source-sha": "source_sha",
        "source-tree": "source_tree",
    }[mutation]
    manifest[field] = (
        "0" * 40 if field != "build_context_sha256" else "sha256:" + "0" * 64
    )
    _write_canonical_json(manifest_path, manifest)
    source_root = tmp_path / "source"
    source_root.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(source_root, filter="data")

    rejected = _run(
        "wheel",
        "verify-context",
        "--archive",
        str(archive),
        "--manifest",
        str(manifest_path),
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "context" in rejected.stderr.lower() or "source" in rejected.stderr.lower()


def test_wheel_seal_rejects_forged_source_even_when_manifest_agrees(
    tmp_path: Path,
) -> None:
    forged_source = "e" * 40
    raw = _raw_native_wheel(tmp_path, manifest_overrides={"source_sha": forged_source})
    rejected = _seal_native_wheel(tmp_path, raw, source_sha=forged_source, check=False)
    assert rejected.returncode == 2
    assert "source authority" in rejected.stderr.lower()


@pytest.mark.parametrize("mutation", ("builder", "tool", "task", "tree"))
def test_wheel_seal_rejects_mutated_build_authority(
    tmp_path: Path, mutation: str
) -> None:
    raw = _raw_native_wheel(tmp_path)

    def mutate(authority: dict[str, object]) -> None:
        if mutation == "builder":
            authority["builder_coordinate"] = "evil.invalid/builder@sha256:" + "0" * 64
        elif mutation == "tool":
            tools = authority["tool_wheels"]
            assert isinstance(tools, dict)
            tools[next(iter(tools))] = "sha256:" + "0" * 64
        elif mutation == "task":
            authority["task_sha256"] = "sha256:" + "0" * 64
        else:
            authority["source_tree"] = "0" * 40

    rejected = _seal_native_wheel(tmp_path, raw, authority_mutation=mutate, check=False)
    assert rejected.returncode == 2
    assert "authority" in rejected.stderr.lower()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("trailing-bytes", "trailing bytes"),
        ("corrupt-zip", "corrupt"),
        ("order", "order"),
        ("mode", "mode"),
        ("forged-record", "RECORD"),
        ("wrong-machine", "ELF machine"),
        ("unapproved-needed", "DT_NEEDED"),
        ("host-path", "path leakage"),
    ],
)
def test_builder_inspection_rejects_tampered_sealed_wheels(
    tmp_path: Path, mutation: str, message: str
) -> None:
    raw = _raw_native_wheel(tmp_path)
    sealed = json.loads(_seal_native_wheel(tmp_path / "base", raw).stdout)
    wheel = Path(sealed["wheel_path"])
    tampered = tmp_path / wheel.name
    if mutation == "trailing-bytes":
        tampered.write_bytes(wheel.read_bytes() + b"not-zip")
    elif mutation == "corrupt-zip":
        raw_bytes = bytearray(wheel.read_bytes())
        with zipfile.ZipFile(wheel) as archive:
            item = next(
                value
                for value in archive.infolist()
                if value.filename.endswith("libmetrics.so")
            )
        name_length = int.from_bytes(
            raw_bytes[item.header_offset + 26 : item.header_offset + 28], "little"
        )
        extra_length = int.from_bytes(
            raw_bytes[item.header_offset + 28 : item.header_offset + 30], "little"
        )
        data_offset = item.header_offset + 30 + name_length + extra_length
        raw_bytes[data_offset + item.compress_size // 2] ^= 0xFF
        tampered.write_bytes(raw_bytes)
    elif mutation == "order":
        _rewrite_zip(wheel, tampered, lambda entries: None, reverse=True)
    elif mutation == "mode":
        _rewrite_zip(wheel, tampered, lambda entries: None, mode=0o600)
    else:

        def transform(entries: dict[str, bytes]) -> None:
            native = next(name for name in entries if name.endswith("libmetrics.so"))
            if mutation == "forged-record":
                entries[native] += b"forged"
                return
            if mutation == "wrong-machine":
                entries[native] = _elf64(machine=183)
            elif mutation == "unapproved-needed":
                entries[native] = _elf64(needed=("libc.so.6", "libevil.so"))
            elif mutation == "host-path":
                entries[native] = _elf64(
                    leaked_path=b"/home/runner/work/ucm/private.cc\0"
                )

        if mutation == "forged-record":
            with zipfile.ZipFile(wheel) as archive:
                entries = {
                    item.filename: archive.read(item.filename)
                    for item in archive.infolist()
                    if not item.is_dir()
                }
            native = next(name for name in entries if name.endswith("libmetrics.so"))
            entries[native] += b"forged"
            with zipfile.ZipFile(tampered, "w") as archive:
                for name in sorted(entries):
                    info = zipfile.ZipInfo(name, date_time=(2023, 11, 14, 22, 13, 20))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    archive.writestr(info, entries[name])
        else:
            _rewrite_zip(wheel, tampered, transform)
    digest = "sha256:" + hashlib.sha256(tampered.read_bytes()).hexdigest()
    rejected = _run(
        "wheel",
        "inspect",
        str(tampered),
        "--spec-id",
        "cuda130-amd64",
        "--expected-sha256",
        digest,
        "--source-kind",
        "builder-candidate",
        check=False,
    )
    assert rejected.returncode == 2
    assert message.lower() in rejected.stderr.lower()


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm 3 is required")
def test_chart_package_runs_cuda_a2_a3_and_is_byte_deterministic(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    record_a = json.loads(_run("chart", "package", "--output-dir", str(first)).stdout)
    record_b = json.loads(_run("chart", "package", "--output-dir", str(second)).stdout)
    assert record_a == record_b
    assert record_a["rendered_cases"] == ["cuda", "a2", "a3"]
    assert record_a["rendered_evidence"] == {
        "cuda": {
            "image": "docker.io/vllm/vllm-openai@sha256:a230095847e93bd4df9888b33dab956fa9504537b828a23657d2b26fed57b5c9",
            "resource": "nvidia.com/gpu",
        },
        "a2": {
            "image": "quay.io/ascend/vllm-ascend@sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d",
            "resource": "huawei.com/Ascend910",
        },
        "a3": {
            "image": "quay.io/ascend/vllm-ascend@sha256:e3d89f09a1c1d85f0ec6a1cc26e3c807b7bc8a7ec0f97a830dbef63ab50d8f81",
            "resource": "huawei.com/Ascend910",
        },
    }
    assert record_a["checks"] == {
        "helm_lint": "passed",
        "helm_package": "passed",
        "helm_template": "passed",
    }
    assert record_a["publication_target"] == "github-release"
    archive_a = first / record_a["filename"]
    archive_b = second / record_b["filename"]
    assert archive_a.read_bytes() == archive_b.read_bytes()
    assert (
        record_a["sha256"]
        == "sha256:" + hashlib.sha256(archive_a.read_bytes()).hexdigest()
    )

    with tarfile.open(archive_a, "r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(
        member.name for member in members
    )
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "root" for member in members)
    assert all(member.mtime == 0 for member in members)
    assert all(
        member.mode == (0o755 if member.isdir() else 0o644) for member in members
    )

    provenance = json.loads(
        (ROOT / "charts" / "ucm" / "SOURCE_PROVENANCE.json").read_text(encoding="utf-8")
    )
    assert provenance["source"] == {
        "commit": "33ac2a37f146a4515e232e4d7a8abaa14d8ef1d7",
        "remote": "https://github.com/SuperMarioYL/uc-stack.git",
        "tree_sha256": "sha256:5a0aa3113c14931e30c88c7f8508b3c742f985e5ede4a8ec48cac77c195c5a2e",
    }
    assert "/Users/" not in json.dumps(provenance)


@pytest.mark.parametrize(
    "member",
    ["/absolute", "chart/../escape", "chart//duplicate", "./chart/file"],
)
def test_chart_repacker_rejects_unsafe_or_noncanonical_paths(
    tmp_path: Path, member: str
) -> None:
    source = tmp_path / "unsafe.tgz"
    info = tarfile.TarInfo(member)
    info.size = 1
    _write_tar(source, [(info, b"x")])
    with pytest.raises(ValueError, match="unsafe or noncanonical Chart member"):
        _deterministic_repack(source, tmp_path / "out.tgz")


def test_chart_repacker_rejects_duplicate_and_non_regular_members(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.tgz"
    first = tarfile.TarInfo("chart/file")
    first.size = 1
    second = tarfile.TarInfo("chart/file")
    second.size = 1
    _write_tar(duplicate, [(first, b"a"), (second, b"b")])
    with pytest.raises(ValueError, match="duplicate Chart member"):
        _deterministic_repack(duplicate, tmp_path / "duplicate-out.tgz")

    linked = tmp_path / "linked.tgz"
    symlink = tarfile.TarInfo("chart/link")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "file"
    _write_tar(linked, [(symlink, None)])
    with pytest.raises(ValueError, match="unsupported member"):
        _deterministic_repack(linked, tmp_path / "linked-out.tgz")
