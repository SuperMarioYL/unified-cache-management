from __future__ import annotations

import base64
import copy
import csv
import hashlib
import importlib
import io
import json
import os
import runpy
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest
import setuptools
import yaml

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

_deterministic_repack = importlib.import_module(
    "ucm_release.chart"
)._deterministic_repack
release_core = importlib.import_module("ucm_release.core")
release_registry = importlib.import_module("ucm_release.registry")
release_verify = importlib.import_module("ucm_release.verify")
release_wheel = importlib.import_module("ucm_release.wheel")
derive_chart_version = release_core.derive_chart_version

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
ASCEND_REQUIRED_NATIVE = [*CUDA_REQUIRED_NATIVE, "mooncakestore"]
ASCEND_FORBIDDEN_NATIVE = CUDA_FORBIDDEN_NATIVE[1:]

ASCEND_EXTERNAL_REQUIRED = {
    "dependency": "libascend_hal.so",
    "provider": "host-ascend-driver",
    "expected_mount_root": "/usr/local/Ascend/driver/lib64",
    "relation": "transitive",
    "required_at": "device-runtime",
}

NATIVE_MEMBERS = {
    "ucmtrans": "ucm/shared/trans/ucmtrans.cpython-312-x86_64-linux-gnu.so",
    "metrics": "ucm/shared/metrics/libmetrics.so",
    "ucmmetrics": "ucm/shared/metrics/ucmmetrics.cpython-312-x86_64-linux-gnu.so",
    "ucmlogger": "ucm/shared/infra/ucmlogger.cpython-312-x86_64-linux-gnu.so",
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


def _fixture_registry() -> dict[str, object]:
    return release_core.load_json(
        RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"
    )


def _fixture_resolved_plan(
    catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    """Resolve test tasks from the explicitly local, non-publishable fixture."""
    return release_registry.resolve_catalog(
        catalog or release_core.load_catalog(),
        source_sha="0" * 40,
        lane="feature-candidate",
        fixture=_fixture_registry(),
    )


def _fixture_wheel_task(
    spec_id: str,
    catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    return next(
        task
        for task in _fixture_resolved_plan(catalog)["wheel_tasks"]
        if task["spec_id"] == spec_id
    )


def _fixture_build_key(spec_id: str) -> str:
    return str(_fixture_wheel_task(spec_id)["task_sha256"])


def _write_fixture_wheel_task(
    directory: Path,
    spec_id: str,
    catalog: dict[str, object] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{spec_id}-fixture-task.json"
    _write_canonical_json(path, _fixture_wheel_task(spec_id, catalog))
    return path


def _clone(value: object) -> object:
    return json.loads(json.dumps(value))


def _write_canonical_json(path: Path, value: object) -> None:
    path.write_bytes(release_core.canonical_bytes(value) + b"\n")


def _git_object_sha1(kind: str, payload: bytes) -> str:
    header = f"{kind} {len(payload)}\0".encode()
    return hashlib.sha1(header + payload).hexdigest()  # noqa: S324 - Git object ID


def _source_context_digest(archive: bytes, commit_payload: bytes) -> str:
    material = (
        b"ucm-build-context-v2\0"
        + len(archive).to_bytes(8, "big")
        + archive
        + commit_payload
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _rewrite_source_archive_comment(path: Path, comment: str) -> None:
    with tarfile.open(path) as source:
        members = source.getmembers()
        payloads = {
            member.name: source.extractfile(member).read()
            for member in members
            if member.isfile()
        }
    rewritten = path.with_suffix(".rewritten.tar")
    with tarfile.open(rewritten, "w", pax_headers={"comment": comment}) as target:
        for member in members:
            target.addfile(
                member,
                io.BytesIO(payloads[member.name]) if member.isfile() else None,
            )
    rewritten.replace(path)


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
        "https://github.com/release-org/unified-cache-management.git",
    )
    (repository / "version.ini").write_text(
        "VLLM_UC_VERSION=0.5.0rc1\n", encoding="utf-8"
    )
    chart_directory = repository / "charts" / "ucm"
    chart_directory.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "charts" / "ucm" / "Chart.yaml", chart_directory / "Chart.yaml"
    )
    shutil.copytree(
        ROOT / "ucm" / "integration" / "vllm" / "patch",
        repository / "ucm" / "integration" / "vllm" / "patch",
    )
    _git(repository, "add", "version.ini", "charts/ucm/Chart.yaml", "ucm")
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
            "full_name": "release-org/unified-cache-management",
            "owner": {"login": "release-org"},
        },
        "sender": {"login": "release-org"},
    }
    event_path.write_text(json.dumps(event), encoding="utf-8")
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_ACTOR": "release-org",
        "GITHUB_TRIGGERING_ACTOR": "release-org",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REF": "refs/tags/v0.5.0rc1",
        "GITHUB_REF_NAME": "v0.5.0rc1",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REPOSITORY": "release-org/unified-cache-management",
        "GITHUB_REPOSITORY_OWNER": "release-org",
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


def _reject_fixture_resolution(
    tmp_path: Path,
    release: dict,
) -> subprocess.CompletedProcess[str]:
    release_path = _write_catalog(tmp_path, release)
    return _run(
        "catalog",
        "resolve",
        "--catalog",
        str(release_path),
        "--fixture",
        str(RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"),
        "--lane",
        "feature-candidate",
        "--source-sha",
        "0" * 40,
        "--output",
        str(tmp_path / "resolved-plan.json"),
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
            f"Version: {version}\nRequires-Dist: packaging==24.2\n"
            "Requires-Dist: wrapt==1.17.2\n\n"
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
    build_key: str | None = None,
    cpu_arch: str = "amd64",
    required: list[str] | None = None,
    forbidden: list[str] | None = None,
) -> bytes:
    build_key = build_key or _fixture_build_key(spec_id)
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
    build_key: str | None = None,
    forbidden: list[str] | None = None,
    required: list[str] | None = None,
    extra_components: tuple[str, ...] = (),
    machine: int = 62,
    needed: tuple[str, ...] = ("libc.so.6",),
    rpath: str | None = None,
    leaked_path: bytes = b"",
    manifest_overrides: dict[str, object] | None = None,
    python_abi: str = "cp312",
    native_members: dict[str, str] | None = None,
) -> Path:
    required = required or CUDA_REQUIRED_NATIVE
    forbidden = forbidden or CUDA_FORBIDDEN_NATIVE
    dist_info = f"uc_manager-{version}.dist-info"
    entries: dict[str, bytes] = {
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: uc-manager\n"
            f"Version: {version}\nRequires-Dist: packaging==24.2\n"
            "Requires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
            f"Tag: {python_abi}-{python_abi}-linux_x86_64\n\n"
        ).encode(),
        "ucm/__init__.py": f"__version__ = {version!r}\n".encode(),
    }
    patch_manifest = release_core.runtime_patch_manifest(release_core.load_catalog())
    for rule in patch_manifest["rules"]:
        for declaration in rule["imports"]:
            module_parts = declaration["module"].split(".")
            module_member = "/".join(module_parts) + ".py"
            entries[module_member] = (ROOT / module_member).read_bytes()
            for length in range(1, len(module_parts)):
                package_member = "/".join(module_parts[:length]) + "/__init__.py"
                package_path = ROOT / package_member
                if package_path.is_file():
                    entries.setdefault(package_member, package_path.read_bytes())
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
    component_members = native_members or NATIVE_MEMBERS
    for component in [*required, *extra_components]:
        entries[component_members[component]] = elf
    raw = directory / (
        f"uc_manager-{version}-{python_abi}-{python_abi}-linux_x86_64.whl"
    )
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
    build_key: str | None = None,
    authority_mutation=None,
    closure_resolved: tuple[str, ...] = ("libc.so.6",),
    closure_external_required: dict[str, str] | None = None,
    catalog: dict[str, object] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    catalog = catalog or release_core.load_catalog()
    task = _fixture_wheel_task(spec_id, catalog)
    build_key = build_key or str(task["task_sha256"])
    tool_wheels = {
        value["filename"]: value["sha256"]
        for value in task["dependency_lock"]["build_tools"]
    }
    source_tree = _git(ROOT, "rev-parse", f"{REVIEWED_SOURCE_SHA}^{{tree}}")
    root = task["builder"]["root"]
    authority = {
        "schema_version": 1,
        "kind": "ucm-native-build-authority",
        "task_id": task["task_id"],
        "spec_id": spec_id,
        "profile_id": task["profile_id"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "build": task["build"],
        "python_version": task["python_version"],
        "python_abi": task["python_abi"],
        "wheel_version": task["wheel_version"],
        "wheel_platform": task["wheel_platform"],
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
        "runtime_patch_manifest_sha256": task["runtime_patch_manifest_sha256"],
        "runtime_requirements": task["runtime_requirements"],
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
    task_path = _write_fixture_wheel_task(tmp_path, spec_id, catalog)

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
        if closure_external_required is not None:
            resolutions.append(
                {
                    **closure_external_required,
                    "direct": False,
                    "kind": "external-required",
                }
            )
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
    config_arguments: list[str] = []
    if catalog is not None and catalog != release_core.load_catalog():
        release_path = _write_catalog(tmp_path / "config", catalog)
        config_arguments = [
            "--release",
            str(release_path),
        ]
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
        "--task-file",
        str(task_path),
        "--output-dir",
        str(tmp_path / "sealed"),
        *config_arguments,
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


def _write_catalog(directory: Path, release: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    release_path = directory / "release.yaml"
    release_path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    return release_path


def _write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)


def test_fixture_plan_projects_platform_loaders_and_driver_boundary() -> None:
    """The local fixture keeps loaders explicit and only Ascend may defer HAL."""
    plan = _fixture_resolved_plan()
    assert plan["fixture_only"] is True
    platform_loaders = {"ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1"}

    for task in plan["wheel_tasks"]:
        assert platform_loaders <= set(task["allowed_dt_needed"])
        if task["accelerator"] == "ascend":
            assert task["external_required_dependencies"] == [ASCEND_EXTERNAL_REQUIRED]
        else:
            assert task["external_required_dependencies"] == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-profile", "no compatible wheel profile"),
        ("extra-profile", "overlapping wheel profiles"),
        ("unresolved-lock", "missing required properties"),
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
    elif mutation == "unresolved-lock":
        del release["python_build_lock"]["packages"]["wheel"]["sha256"]
    elif mutation == "caller-raw-runner":
        release["wheel_profiles"][0]["runner"] = "self-hosted"
    rejected = _reject_fixture_resolution(tmp_path / mutation, release)
    assert rejected.returncode == 2
    assert message in rejected.stderr


def test_feature_preflight_planner_mode_has_no_write_authority() -> None:
    feature = json.loads(
        _run(
            "core",
            "tag-preflight",
            "--lane",
            "feature-candidate",
            "--catalog-planner",
        ).stdout
    )
    assert feature["publication_allowed"] is False
    assert feature["write_authority"] == []


def test_tag_preflight_cli_requires_frozen_plan_or_explicit_planner_mode(
    tmp_path: Path,
) -> None:
    missing = _run("core", "tag-preflight", "--lane", "feature-candidate", check=False)
    assert missing.returncode == 2
    assert "frozen plan" in missing.stderr

    plan = _fixture_resolved_plan()
    plan_path = tmp_path / "resolved-plan.json"
    plan_path.write_bytes(release_core.canonical_bytes(plan) + b"\n")
    selected = json.loads(
        _run(
            "core",
            "tag-preflight",
            "--lane",
            "feature-candidate",
            "--resolved-plan",
            str(plan_path),
            "--expected-plan-sha256",
            plan["resolved_plan_sha256"],
        ).stdout
    )
    assert selected["repository"] == plan["source"]["repository"]
    assert selected["publication_allowed"] is False

    tampered = _run(
        "core",
        "tag-preflight",
        "--lane",
        "feature-candidate",
        "--resolved-plan",
        str(plan_path),
        "--expected-plan-sha256",
        "sha256:" + "f" * 64,
        check=False,
    )
    assert tampered.returncode == 2
    assert "plan hash" in tampered.stderr


def test_tag_preflight_rejects_caller_supplied_identity_arguments() -> None:
    rejected = _run(
        "core",
        "tag-preflight",
        "--lane",
        "protected-tag",
        "--repository",
        "release-org/unified-cache-management",
        "--repository-owner",
        "release-org",
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


def test_tag_preflight_rejects_foreign_or_missing_triggering_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rerun initiator must have the same exact owner authority as the actor."""
    repository, environment, _, _, _ = _protected_tag_repository(tmp_path)
    environment["GITHUB_TRIGGERING_ACTOR"] = "attacker"
    with pytest.raises(ValueError, match="triggering_actor"):
        _run_protected_tag_preflight(monkeypatch, repository, environment)
    environment.pop("GITHUB_TRIGGERING_ACTOR")
    monkeypatch.delenv("GITHUB_TRIGGERING_ACTOR", raising=False)
    with pytest.raises(ValueError, match="triggering_actor"):
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


def test_json_array_loader_preserves_duplicate_key_rejection(tmp_path: Path) -> None:
    """REST arrays stay type-explicit without weakening duplicate-key parsing."""
    valid = tmp_path / "valid-array.json"
    valid.write_text('[{"id":1}]\n', encoding="utf-8")
    assert release_core.load_json_array(valid) == [{"id": 1}]
    with pytest.raises(ValueError, match="JSON object"):
        release_core.load_json(valid)

    duplicate = tmp_path / "duplicate-array.json"
    duplicate.write_text('[{"id":1,"id":2}]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        release_core.load_json_array(duplicate)


def test_release_cli_reopens_array_output_across_command_boundary(
    tmp_path: Path,
) -> None:
    """An empty REST observation stays an explicit JSON array across the CLI."""
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-asset-download-plan",
        "release_id": 41,
        "assets_sha256": "sha256:" + "1" * 64,
        "asset_download_slug": "fixture-only",
        "require_complete": False,
        "downloads": [],
    }
    download_plan = {**payload, "plan_sha256": release_core.sha256_value(payload)}
    download_plan_path = tmp_path / "download-plan.json"
    _write_canonical_json(download_plan_path, download_plan)
    request_path = tmp_path / "complete-request.json"
    _write_canonical_json(
        request_path,
        {
            "download_plan": str(download_plan_path),
            "download_root": str(download_root),
        },
    )
    observed_path = tmp_path / "observed-assets.json"

    _run(
        "release",
        "complete-downloads",
        "--input",
        str(request_path),
        "--output",
        str(observed_path),
    )

    assert release_core.load_json_array(observed_path) == []


def test_config_schema_validation_is_operational_for_the_single_catalog(
    tmp_path: Path,
) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    assert release["kind"] == "release-config"
    schema = json.loads((RELEASE_ROOT / "schemas/config.schema.json").read_text())
    assert schema["$ref"] == "#/$defs/release"

    release["schema_version"] = "2"
    release_path = _write_catalog(tmp_path, release)
    wrong_type = _run(
        "config",
        "validate",
        "--release",
        str(release_path),
        check=False,
    )

    assert wrong_type.returncode == 2
    assert "expected integer" in wrong_type.stderr


def test_schema_refs_enforce_required_enum_pattern_and_unique_items(
    tmp_path: Path,
) -> None:
    original = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    mutations: list[tuple[dict, str]] = []

    missing = json.loads(json.dumps(original))
    del missing["chart"]["name"]
    mutations.append((missing, "missing required properties"))

    wrong_enum = json.loads(json.dumps(original))
    wrong_enum["lanes"][0] = "unknown-lane"
    mutations.append((wrong_enum, "expected one of"))

    wrong_pattern = json.loads(json.dumps(original))
    wrong_pattern["source"]["repository"] = "missing-owner-separator"
    mutations.append((wrong_pattern, "does not match pattern"))

    duplicate_array = json.loads(json.dumps(original))
    duplicate_array["wheel_profiles"][0]["cpu_arch"] = ["amd64", "amd64"]
    mutations.append((duplicate_array, "array items must be unique"))

    for index, (release, message) in enumerate(mutations):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        release_path = _write_catalog(case_dir, release)
        rejected = _run(
            "config",
            "validate",
            "--release",
            str(release_path),
            check=False,
        )
        assert rejected.returncode == 2
        assert message in rejected.stderr


def test_fixture_resolution_rejects_compatibility_without_matching_profile(
    tmp_path: Path,
) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    release["compatibility"]["rules"][0]["accelerator_runtimes"] = ["cuda-12.9"]

    drift = _reject_fixture_resolution(tmp_path, release)

    assert drift.returncode == 2
    assert "no compatible wheel profile" in drift.stderr


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
    assert release_core.python_runtime_requirements(release_config) == [
        "packaging==24.2",
        "wrapt==1.17.2",
    ]
    assert derive_chart_version(version) == "0.5.0-rc.1"


def test_coordinated_config_version_drift_is_rejected(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    release["ucm_version"] = release["chart"]["app_version"] = "0.5.0rc2"
    release["chart"]["version"] = "0.5.0-rc.2"
    release["source"]["release_tag"] = "v0.5.0rc2"
    release_path = _write_catalog(tmp_path, release)
    drift = _run(
        "config",
        "validate",
        "--release",
        str(release_path),
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
    assert inspected["requires_dist"] == ["packaging==24.2", "wrapt==1.17.2"]
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
    task_path = _write_fixture_wheel_task(tmp_path, spec_id)
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
        "--task-file",
        str(task_path),
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
        for item in _fixture_resolved_plan()["wheel_tasks"]
        if item["spec_id"] == setup_spec
    )
    configured_python = shutil.which(f"python{setup_task['python_version']}")
    if configured_python is None:
        pytest.skip(f"configured Python {setup_task['python_version']} is unavailable")
    setup_runtime = subprocess.run(
        [configured_python, "-c", "import setuptools"],
        text=True,
        capture_output=True,
        check=False,
    )
    if setup_runtime.returncode != 0:
        pytest.skip(
            f"configured Python {setup_task['python_version']} lacks setuptools"
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
        "UCM_RELEASE_TASK_ID": setup_task["task_id"],
        "UCM_RELEASE_SPEC_ID": setup_task["spec_id"],
        "UCM_RELEASE_PROFILE": "cuda130",
        "UCM_RELEASE_SOURCE_SHA": REVIEWED_SOURCE_SHA,
        "UCM_RELEASE_VERSION": "0.5.0rc1+cuda130",
        "UCM_RELEASE_BUILD_KEY": setup_task["task_sha256"],
        "UCM_RELEASE_PYTHON_VERSION": setup_task["python_version"],
        "UCM_RELEASE_PYTHON_ABI": setup_task["python_abi"],
        "UCM_RELEASE_WHEEL_PLATFORM": setup_task["wheel_platform"],
        "UCM_RELEASE_BUILD_SETTINGS": json.dumps(
            setup_task["build"], sort_keys=True, separators=(",", ":")
        ),
        "UCM_RUNTIME_PATCH_MANIFEST_SHA256": setup_task[
            "runtime_patch_manifest_sha256"
        ],
        "UCM_RELEASE_REQUIRED_TARGETS": ",".join(CUDA_REQUIRED_NATIVE),
        "UCM_RELEASE_FORBIDDEN_TARGETS": ",".join(CUDA_FORBIDDEN_NATIVE),
        "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        "UCM_RELEASE_AUTHORITY_FILE": str(authority_root / "build-authority.json"),
    }
    controlled = subprocess.run(
        [configured_python, "setup.py", "--version"],
        cwd=ROOT,
        env=release_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert controlled.returncode == 0, controlled.stderr
    assert controlled.stdout.strip() == "0.5.0rc1+cuda130"

    wrong = subprocess.run(
        [configured_python, "setup.py", "--version"],
        cwd=ROOT,
        env={**release_env, "UCM_RELEASE_VERSION": "0.5.0rc1+cann900.a2"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert wrong.returncode != 0
    assert "version" in wrong.stderr.lower()


def test_release_setup_binds_pybind_to_the_invoking_python() -> None:
    """The wheel tag and every pybind extension must use the same CPython ABI."""
    setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
    assert 'f"-DPYTHON_EXECUTABLE={sys.executable}"' in setup_text
    assert 'f"-DPython_EXECUTABLE={sys.executable}"' in setup_text


def test_release_setup_configures_cmake_with_invoking_python_development_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modern FindPython must resolve headers from the same invoking CPython."""
    monkeypatch.setenv("PLATFORM", "cuda")
    monkeypatch.delenv("UCM_RELEASE_BUILD", raising=False)
    monkeypatch.setattr(setuptools, "setup", lambda **_kwargs: None)
    setup_namespace = runpy.run_path(str(ROOT / "setup.py"))

    calls: list[list[str]] = []

    def record_call(command: list[str], **_kwargs: object) -> None:
        calls.append(command)

    monkeypatch.setattr(subprocess, "check_call", record_call)
    builder = setup_namespace["CMakeBuild"](setuptools.Distribution())
    builder.build_temp = str(tmp_path / "build")
    builder.build_lib = str(tmp_path / "install")
    extension = setup_namespace["CMakeExtension"]("ucm", str(ROOT))
    builder.build_cmake(extension)

    configure_argv = calls[0]
    assert configure_argv[0] == "cmake"
    assert f"-DPython_EXECUTABLE={sys.executable}" in configure_argv
    assert f"-DPython_INCLUDE_DIR={sysconfig.get_path('include')}" in configure_argv
    assert f"-DPython_ROOT_DIR={sys.prefix}" in configure_argv


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


def test_hosted_task_projects_complete_setup_and_builder_authority() -> None:
    plan = _fixture_resolved_plan()
    task = plan["wheel_tasks"][0]

    hosted = release_verify.hosted_wheel_task(
        task,
        plan["source"]["commit"],
        SOURCE_DATE_EPOCH,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )

    assert hosted["docker_target"] == task["build"]["docker_target"]
    assert hosted["build_args"]["UCM_RELEASE_TASK_ID"] == task["task_id"]
    assert hosted["build_args"]["UCM_RELEASE_SPEC_ID"] == task["spec_id"]
    assert hosted["build_args"]["UCM_RELEASE_PYTHON_VERSION"] == task["python_version"]
    assert hosted["build_args"]["UCM_RELEASE_PYTHON_ABI"] == task["python_abi"]
    assert hosted["build_args"]["UCM_RELEASE_WHEEL_PLATFORM"] == task["wheel_platform"]
    assert (
        json.loads(hosted["build_args"]["UCM_RELEASE_BUILD_SETTINGS"]) == task["build"]
    )
    assert (
        hosted["build_args"]["UCM_RUNTIME_PATCH_MANIFEST_SHA256"]
        == task["runtime_patch_manifest_sha256"]
    )


def test_release_setup_accepts_opaque_profile_and_validates_every_explicit_value(
    tmp_path: Path,
) -> None:
    architecture = (
        "arm64" if os.uname().machine.lower() in {"arm64", "aarch64"} else "amd64"
    )
    task = next(
        item
        for item in _fixture_resolved_plan()["wheel_tasks"]
        if item["profile_id"] == "cuda130" and item["cpu_arch"] == architecture
    )
    invoking_python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    invoking_python_abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    authority = {
        "schema_version": 1,
        "kind": "ucm-native-build-authority",
        "task_id": task["task_id"],
        "spec_id": "opaque-spec-value",
        "profile_id": "renamed-profile-value",
        "cpu_arch": architecture,
        "platform": task["platform"],
        "build": {"docker_target": "wheel", "platform_arg": "cuda"},
        "python_version": invoking_python_version,
        "python_abi": invoking_python_abi,
        "wheel_version": "0.5.0rc1+synthetic.7",
        "wheel_platform": task["wheel_platform"],
        "source_sha": REVIEWED_SOURCE_SHA,
        "source_tree": _git(ROOT, "rev-parse", f"{REVIEWED_SOURCE_SHA}^{{tree}}"),
        "source_archive_sha256": "sha256:" + "1" * 64,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "task_sha256": "sha256:" + "2" * 64,
        "builder_coordinate": task["builder"]["root"]["repository"]
        + "@"
        + task["builder"]["root"]["manifest_digest"],
        "builder_config_digest": task["builder"]["root"]["config_digest"],
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "tool_wheels": {
            f"tool-{index}.whl": "sha256:" + str(index) * 64 for index in range(1, 4)
        },
        "required_native": task["required_native"],
        "forbidden_native": task["forbidden_native"],
        "runtime_patch_manifest_sha256": task["runtime_patch_manifest_sha256"],
        "runtime_requirements": [
            "alpha-runtime==2.0",
            "packaging==24.2",
            "wrapt==1.18.0",
        ],
        "build_context_sha256": "sha256:" + "3" * 64,
    }
    authority_path = tmp_path / "authority.json"
    _write_canonical_json(authority_path, authority)
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UCM_RELEASE_")
        and key not in {"SOURCE_DATE_EPOCH", "PLATFORM"}
    }
    environment = {
        **base_env,
        "UCM_RELEASE_BUILD": "1",
        "PLATFORM": "cuda",
        "UCM_RELEASE_TASK_ID": task["task_id"],
        "UCM_RELEASE_SPEC_ID": "opaque-spec-value",
        "UCM_RELEASE_PROFILE": "renamed-profile-value",
        "UCM_RELEASE_SOURCE_SHA": REVIEWED_SOURCE_SHA,
        "UCM_RELEASE_VERSION": "0.5.0rc1+synthetic.7",
        "UCM_RELEASE_BUILD_KEY": "sha256:" + "2" * 64,
        "UCM_RELEASE_PYTHON_VERSION": invoking_python_version,
        "UCM_RELEASE_PYTHON_ABI": invoking_python_abi,
        "UCM_RELEASE_WHEEL_PLATFORM": task["wheel_platform"],
        "UCM_RELEASE_BUILD_SETTINGS": json.dumps(
            authority["build"], sort_keys=True, separators=(",", ":")
        ),
        "UCM_RUNTIME_PATCH_MANIFEST_SHA256": task["runtime_patch_manifest_sha256"],
        "UCM_RELEASE_REQUIRED_TARGETS": ",".join(task["required_native"]),
        "UCM_RELEASE_FORBIDDEN_TARGETS": ",".join(task["forbidden_native"]),
        "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        "UCM_RELEASE_AUTHORITY_FILE": str(authority_path),
    }

    accepted = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "0.5.0rc1+synthetic.7"

    egg_base = tmp_path / "egg-base"
    egg_base.mkdir()
    metadata = subprocess.run(
        [sys.executable, "setup.py", "egg_info", "--egg-base", str(egg_base)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert metadata.returncode == 0, metadata.stderr
    requires = next(egg_base.glob("*.egg-info/requires.txt"))
    assert requires.read_text(encoding="utf-8").splitlines() == [
        "alpha-runtime==2.0",
        "packaging==24.2",
        "wrapt==1.18.0",
    ]

    for field, environment_name, tampered in (
        ("platform", "PLATFORM", "ascend"),
        ("version", "UCM_RELEASE_VERSION", "0.5.0rc1+tampered"),
        ("spec", "UCM_RELEASE_SPEC_ID", "foreign-spec"),
        ("Python ABI", "UCM_RELEASE_PYTHON_ABI", "cp311"),
        ("required targets", "UCM_RELEASE_REQUIRED_TARGETS", "foreign"),
        (
            "runtime patch manifest",
            "UCM_RUNTIME_PATCH_MANIFEST_SHA256",
            "sha256:" + "f" * 64,
        ),
    ):
        rejected = subprocess.run(
            [sys.executable, "setup.py", "--version"],
            cwd=ROOT,
            env={**environment, environment_name: tampered},
            text=True,
            capture_output=True,
            check=False,
        )
        assert rejected.returncode != 0, field
        assert field.lower() in rejected.stderr.lower()


def test_build_environment_executes_typed_checks_from_the_wheel_task(
    tmp_path: Path,
) -> None:
    required_file = tmp_path / "runtime.h"
    required_file.write_text("fixture\n", encoding="utf-8")
    architecture = (
        "arm64" if os.uname().machine.lower() in {"arm64", "aarch64"} else "amd64"
    )
    task = {
        "task_id": "wheel-" + "a" * 64,
        "task_sha256": "sha256:" + "b" * 64,
        "cpu_arch": architecture,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "builder": {
            "checks": [
                {
                    "kind": "python",
                    "version": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
                },
                {"kind": "python-soabi", "prefix": "cpython"},
                {"kind": "command", "name": "sh"},
                {
                    "kind": "command-version",
                    "name": Path(sys.executable).name,
                    "arguments": ["--version"],
                    "contains": f"Python {sys.version_info.major}.{sys.version_info.minor}",
                },
                {"kind": "file", "path": str(required_file)},
                {"kind": "directory", "path": str(tmp_path)},
            ]
        },
    }

    evidence = release_wheel.check_build_environment(
        task, python_executable=Path(sys.executable)
    )

    assert evidence["task_id"] == task["task_id"]
    assert [item["kind"] for item in evidence["checks"]] == [
        "python",
        "python-soabi",
        "command",
        "command-version",
        "file",
        "directory",
    ]
    missing = copy.deepcopy(task)
    missing["builder"]["checks"][-2]["path"] = str(tmp_path / "missing.h")
    with pytest.raises(ValueError, match="required file"):
        release_wheel.check_build_environment(
            missing, python_executable=Path(sys.executable)
        )


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
    task_path = _write_fixture_wheel_task(tmp_path, "cuda130-amd64")

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
            "--task-file",
            str(task_path),
        ).stdout
    )
    evidence = inspected["builder_evidence"]
    assert evidence["source_commit"] == REVIEWED_SOURCE_SHA
    assert evidence["build_key"] == _fixture_build_key("cuda130-amd64")
    assert evidence["native_components"] == CUDA_REQUIRED_NATIVE
    assert evidence["native_members"]["ucmlogger"] == (
        "ucm/shared/infra/ucmlogger.cpython-312-x86_64-linux-gnu.so"
    )
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


def test_cp311_full_authority_build_args_seal_and_audit_path(tmp_path: Path) -> None:
    """A synthetic cp311 profile must stay cp311 through planning and wheel audit."""
    catalog = copy.deepcopy(release_core.load_catalog())
    profile = next(
        item for item in catalog["wheel_profiles"] if item["id"] == "cuda130"
    )
    profile["id"] = "opaque-python-profile"
    profile["python_version"] = "3.11"
    profile["python_abi"] = "cp311"
    profile["wheel_version"] = "0.5.0rc1+synthetic.cp311"
    for builder in profile["builders"].values():
        python_check = next(
            item for item in builder["checks"] if item["kind"] == "python"
        )
        python_check["version"] = "3.11"
        python_check["abi"] = "cp311"
    cuda_rule = next(
        item
        for item in catalog["compatibility"]["rules"]
        if item["accelerator"] == "cuda"
    )
    cuda_rule["python_abis"] = ["cp311"]
    pyyaml_cp312 = catalog["python_build_lock"]["pyyaml"]["artifacts"]["cp312"]
    catalog["python_build_lock"]["pyyaml"]["artifacts"]["cp311"] = {
        architecture: {
            **copy.deepcopy(artifact),
            "filename": artifact["filename"].replace("cp312", "cp311"),
        }
        for architecture, artifact in pyyaml_cp312.items()
    }
    runtime_artifacts = catalog["python_runtime_dependencies"][0]["wheel_artifacts"]
    wrapt_cp312 = runtime_artifacts["cp312"]
    runtime_artifacts["cp311"] = {
        architecture: {
            **copy.deepcopy(artifact),
            "filename": artifact["filename"].replace("cp312", "cp311"),
        }
        for architecture, artifact in wrapt_cp312.items()
    }
    plan = _fixture_resolved_plan(catalog)
    task = next(
        item
        for item in plan["wheel_tasks"]
        if item["profile_id"] == "opaque-python-profile" and item["cpu_arch"] == "amd64"
    )

    hosted = release_verify.hosted_wheel_task(
        task,
        plan["source"]["commit"],
        SOURCE_DATE_EPOCH,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    assert hosted["build_args"]["UCM_RELEASE_PYTHON_VERSION"] == "3.11"
    assert hosted["build_args"]["UCM_RELEASE_PYTHON_ABI"] == "cp311"
    assert "cp311" in next(
        record["filename"]
        for record in hosted["build_tools"]
        if record["name"] == "pyyaml"
    )

    cp311_members = {
        component: member.replace("cpython-312", "cpython-311")
        for component, member in NATIVE_MEMBERS.items()
    }
    raw = _raw_native_wheel(
        tmp_path,
        version=profile["wheel_version"],
        profile_id=profile["id"],
        spec_id=task["spec_id"],
        build_key=task["task_sha256"],
        python_abi="cp311",
        native_members=cp311_members,
    )
    sealed_process = _seal_native_wheel(
        tmp_path,
        raw,
        spec_id=task["spec_id"],
        build_key=task["task_sha256"],
        catalog=catalog,
        check=False,
    )
    assert sealed_process.returncode == 0, sealed_process.stderr
    sealed = json.loads(sealed_process.stdout)

    assert "-cp311-cp311-" in Path(sealed["wheel_path"]).name
    native_members = sealed["inspection"]["builder_evidence"]["native_members"]
    assert all("cpython-312" not in member for member in native_members.values())
    assert native_members["ucmlogger"].endswith(
        "ucmlogger.cpython-311-x86_64-linux-gnu.so"
    )


def test_runtime_patch_manifest_is_canonical_bound_and_required_in_sealed_wheel(
    tmp_path: Path,
) -> None:
    raw = _raw_native_wheel(tmp_path)
    sealed = json.loads(_seal_native_wheel(tmp_path / "sealed", raw).stdout)
    wheel = Path(sealed["wheel_path"])
    task_path = _write_fixture_wheel_task(tmp_path, "cuda130-amd64")
    manifest_path = "ucm/integration/vllm/patch/runtime_patch_rules.json"
    expected_manifest = release_core.runtime_patch_manifest(release_core.load_catalog())
    expected_bytes = release_core.canonical_bytes(expected_manifest) + b"\n"
    expected_sha256 = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()

    with zipfile.ZipFile(wheel) as archive:
        assert archive.namelist().count(manifest_path) == 1
        assert archive.read(manifest_path) == expected_bytes
        authority_name = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/ucm-build-authority.json")
        )
        build_name = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/ucm-build.json")
        )
        authority = json.loads(archive.read(authority_name))
        build = json.loads(archive.read(build_name))
    assert authority["runtime_patch_manifest_sha256"] == expected_sha256
    assert build["runtime_patch_manifest_sha256"] == expected_sha256
    assert sealed["runtime_patch_manifest_sha256"] == expected_sha256
    assert sealed["inspection"]["runtime_patch_manifest_sha256"] == expected_sha256

    for name, transform, message in (
        (
            "removed.whl",
            lambda entries: entries.pop(manifest_path),
            "runtime patch manifest",
        ),
        (
            "tampered.whl",
            lambda entries: entries.__setitem__(manifest_path, b"{}\n"),
            "runtime patch manifest",
        ),
        (
            "missing-adapter.whl",
            lambda entries: entries.pop(
                "ucm/integration/vllm/patch/load_failure_patch.py"
            ),
            "runtime patch adapter",
        ),
    ):
        candidate_dir = tmp_path / name
        candidate_dir.mkdir()
        candidate = candidate_dir / wheel.name
        _rewrite_zip(wheel, candidate, transform)
        digest = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
        rejected = _run(
            "wheel",
            "inspect",
            str(candidate),
            "--spec-id",
            "cuda130-amd64",
            "--expected-sha256",
            digest,
            "--source-kind",
            "builder-candidate",
            "--task-file",
            str(task_path),
            check=False,
        )
        assert rejected.returncode == 2
        assert message in rejected.stderr.lower()

    duplicate_dir = tmp_path / "duplicate"
    duplicate_dir.mkdir()
    duplicate = duplicate_dir / wheel.name
    shutil.copyfile(wheel, duplicate)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(duplicate, "a") as archive:
            archive.writestr(manifest_path, expected_bytes)
    duplicate_digest = "sha256:" + hashlib.sha256(duplicate.read_bytes()).hexdigest()
    rejected_duplicate = _run(
        "wheel",
        "inspect",
        str(duplicate),
        "--spec-id",
        "cuda130-amd64",
        "--expected-sha256",
        duplicate_digest,
        "--source-kind",
        "builder-candidate",
        "--task-file",
        str(task_path),
        check=False,
    )
    assert rejected_duplicate.returncode == 2
    assert "duplicate" in rejected_duplicate.stderr.lower()


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
        build_key=_fixture_build_key("cann900-a2-amd64"),
        forbidden=CUDA_FORBIDDEN_NATIVE[1:],
        required=CUDA_REQUIRED_NATIVE,
    )
    rejected = _seal_native_wheel(
        tmp_path,
        raw,
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
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
        (
            "ucmlogger",
            "ucm/shared/infra/logger/ucmlogger.cpython-312-x86_64-linux-gnu.so",
        ),
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


def test_wheel_seal_accepts_exact_transitive_ascend_driver_requirement(
    tmp_path: Path,
) -> None:
    """A sealed Ascend closure may defer only the declared host driver SONAME."""
    raw = _raw_native_wheel(
        tmp_path,
        version="0.5.0rc1+cann900.a2",
        profile_id="cann900-a2",
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
        required=ASCEND_REQUIRED_NATIVE,
        forbidden=ASCEND_FORBIDDEN_NATIVE,
    )

    accepted = _seal_native_wheel(
        tmp_path,
        raw,
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
        closure_external_required=ASCEND_EXTERNAL_REQUIRED,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    (("provider", "image-builder"), ("relation", "direct")),
)
def test_wheel_seal_rejects_mutated_external_requirement(
    tmp_path: Path, field: str, value: str
) -> None:
    """Caller-authored closure evidence cannot weaken the reviewed declaration."""
    raw = _raw_native_wheel(
        tmp_path,
        version="0.5.0rc1+cann900.a2",
        profile_id="cann900-a2",
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
        required=ASCEND_REQUIRED_NATIVE,
        forbidden=ASCEND_FORBIDDEN_NATIVE,
    )
    declaration = {**ASCEND_EXTERNAL_REQUIRED, field: value}

    rejected = _seal_native_wheel(
        tmp_path,
        raw,
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
        closure_external_required=declaration,
        check=False,
    )

    assert rejected.returncode == 2
    assert "external-required" in rejected.stderr


def test_wheel_seal_rejects_ascend_driver_requirement_for_cuda(
    tmp_path: Path,
) -> None:
    """A CUDA closure cannot borrow the host Ascend driver exception."""
    raw = _raw_native_wheel(tmp_path)

    rejected = _seal_native_wheel(
        tmp_path,
        raw,
        closure_external_required=ASCEND_EXTERNAL_REQUIRED,
        check=False,
    )

    assert rejected.returncode == 2
    assert "external-required" in rejected.stderr


def test_dependency_closure_audit_preserves_external_required_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real audit loop must not treat an abstract driver record as a file path."""
    raw = _raw_native_wheel(
        tmp_path,
        version="0.5.0rc1+cann900.a2",
        profile_id="cann900-a2",
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
        required=ASCEND_REQUIRED_NATIVE,
        forbidden=ASCEND_FORBIDDEN_NATIVE,
    )
    _seal_native_wheel(
        tmp_path,
        raw,
        spec_id="cann900-a2-amd64",
        build_key=_fixture_build_key("cann900-a2-amd64"),
        closure_external_required=ASCEND_EXTERNAL_REQUIRED,
        check=False,
    )

    real_run = subprocess.run

    def completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        stdout = ""
        if command[0] == "ldd":
            stdout = """\
libc.so.6 => /bin/sh (0x00001111)
libascend_hal.so => not found
libascend_hal.so => not found
"""
        if command[0] in {"ldd", "readelf"}:
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(release_wheel.sys, "platform", "linux")
    monkeypatch.setattr(release_wheel.subprocess, "run", completed)
    record = release_wheel.audit_dependency_closure(
        raw,
        tmp_path / "audited-closure.json",
        "cann900-a2-amd64",
        tmp_path / "build-authority.json",
        task_path=_write_fixture_wheel_task(tmp_path, "cann900-a2-amd64"),
    )

    for member in record["native_members"].values():
        assert {
            **ASCEND_EXTERNAL_REQUIRED,
            "direct": False,
            "kind": "external-required",
        } in member["resolved_dependencies"]
        assert member["unresolved_dependencies"] == []
    assert record["unresolved_dependencies"] == []


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
    ("loader", "path"),
    (
        ("ld-linux-x86-64.so.2", "/lib64/ld-linux-x86-64.so.2"),
        ("ld-linux-aarch64.so.1", "/lib/ld-linux-aarch64.so.1"),
    ),
)
def test_ldd_closure_binds_direct_platform_loader_soname_to_absolute_path(
    loader: str, path: str
) -> None:
    """ldd prints a loader path, while ELF DT_NEEDED carries its SONAME."""
    assert release_wheel._parse_ldd_output(
        "ucm/store/cache/libcachestore.so",
        [loader],
        f"{path} (0x00003333)\n",
    ) == [
        {
            "dependency": loader,
            "direct": True,
            "kind": "located",
            "path": path,
        }
    ]


@pytest.mark.parametrize("spec_id", ("cann900-a2-amd64", "cann900-a3-arm64"))
def test_preflight_deduplicates_only_declared_transitive_hal(spec_id: str) -> None:
    """Repeated driver misses are one declared device-runtime requirement."""
    evidence = release_wheel.validate_preflight_ldd(
        _fixture_wheel_task(spec_id),
        "/usr/local/lib/libmooncake_store.so",
        ["libtransfer_engine.so"],
        """\
libtransfer_engine.so => /usr/local/lib/libtransfer_engine.so (0x00001111)
libascend_hal.so => not found
libascend_hal.so => not found
""",
    )

    assert evidence["resolved_dependencies"] == [
        {
            **ASCEND_EXTERNAL_REQUIRED,
            "direct": False,
            "kind": "external-required",
        },
        {
            "dependency": "libtransfer_engine.so",
            "direct": True,
            "kind": "located",
            "path": "/usr/local/lib/libtransfer_engine.so",
        },
    ]
    assert evidence["unexpected_unresolved"] == []


def test_preflight_reads_direct_needed_without_applying_wheel_elf_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned Mooncake base ELF is inspected for NEEDED, not wheel-only policy."""
    binary = tmp_path / "libmooncake_store.so"
    binary.write_bytes(b"host runtime ELF bytes are supplied by the pinned image")

    def completed(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if command[0] == "readelf":
            stdout = """\
 0x0000000000000001 (NEEDED) Shared library: [libtransfer_engine.so]
 0x0000000000000001 (NEEDED) Shared library: [ld-linux-x86-64.so.2]
"""
        else:
            stdout = """\
libtransfer_engine.so => /usr/local/lib/libtransfer_engine.so (0x00001111)
/lib64/ld-linux-x86-64.so.2 (0x00002222)
libascend_hal.so => not found
"""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_wheel.subprocess, "run", completed)
    evidence = release_wheel.preflight_dependencies(
        binary,
        "cann900-a2-amd64",
        task=_fixture_wheel_task("cann900-a2-amd64"),
    )

    assert evidence["status"] == "passed"
    assert evidence["unexpected_unresolved"] == []


def test_preflight_rejects_declared_hal_mixed_with_an_unknown_missing_soname() -> None:
    """A declared driver boundary must not hide an unrelated broken dependency."""
    with pytest.raises(ValueError, match="unexpected unresolved.*libfoo"):
        release_wheel.validate_preflight_ldd(
            _fixture_wheel_task("cann900-a2-amd64"),
            "/usr/local/lib/libmooncake_store.so",
            [],
            """\
libascend_hal.so => not found
libfoo.so => not found
""",
        )


def test_cuda_and_direct_dependencies_cannot_use_the_ascend_hal_boundary() -> None:
    """HAL is Ascend-only and its declaration is transitive, never DT_NEEDED."""
    with pytest.raises(ValueError, match="unexpected unresolved.*libascend_hal"):
        release_wheel.validate_preflight_ldd(
            _fixture_wheel_task("cuda130-amd64"),
            "ucm/store/cache/libcachestore.so",
            [],
            "libascend_hal.so => not found\n",
        )
    with pytest.raises(ValueError, match="direct.*external-required|transitive"):
        release_wheel.validate_preflight_ldd(
            _fixture_wheel_task("cann900-a3-arm64"),
            "/usr/local/lib/libmooncake_store.so",
            ["libascend_hal.so"],
            "libascend_hal.so => not found\n",
        )


def test_wheel_and_runtime_ldd_parsers_record_identical_external_requirement() -> None:
    """The install-only image must preserve the sealed wheel closure exactly."""
    declarations = [ASCEND_EXTERNAL_REQUIRED]
    output = "libascend_hal.so => not found\nlibascend_hal.so => not found\n"
    wheel_closure = release_wheel._parse_ldd_output(
        "ucm/store/mooncakestore/libmooncakestore.so",
        [],
        output,
        external_required_dependencies=declarations,
    )
    runtime_module = runpy.run_path(str(RELEASE_ROOT / "docker" / "inspect_runtime.py"))
    runtime_closure = runtime_module["_parse_ldd"](
        "ucm/store/mooncakestore/libmooncakestore.so",
        [],
        output,
        {},
        external_required_dependencies=declarations,
    )

    assert (
        runtime_closure
        == wheel_closure
        == [
            {
                **ASCEND_EXTERNAL_REQUIRED,
                "direct": False,
                "kind": "external-required",
            }
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
    commit_payload = Path(prepared["source_commit_payload_path"])
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
            "--commit-payload",
            str(commit_payload),
            "--expected-source-sha",
            REVIEWED_SOURCE_SHA,
            "--source-root",
            str(source_root),
        ).stdout
    )
    raw = archive.read_bytes()
    commit_raw = commit_payload.read_bytes()
    assert verified["source_archive_sha256"] == (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )
    assert verified["build_context_sha256"] == (_source_context_digest(raw, commit_raw))
    assert verified["source_commit_payload_sha256"] == (
        "sha256:" + hashlib.sha256(commit_raw).hexdigest()
    )
    assert _git_object_sha1("commit", commit_raw) == REVIEWED_SOURCE_SHA
    assert verified["source_sha"] == REVIEWED_SOURCE_SHA
    assert verified["source_tree"] == _git(
        ROOT, "rev-parse", f"{REVIEWED_SOURCE_SHA}^{{tree}}"
    )


def test_no_git_verifier_rejects_self_consistent_untrusted_source_sha(
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
    manifest_path = Path(prepared["source_manifest_path"])
    fake_source_sha = "f" * 40
    _rewrite_source_archive_comment(archive, fake_source_sha)
    archive_raw = archive.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    manifest["source_sha"] = fake_source_sha
    manifest["source_archive_sha256"] = (
        "sha256:" + hashlib.sha256(archive_raw).hexdigest()
    )
    manifest["build_context_sha256"] = (
        "sha256:" + hashlib.sha256(b"ucm-build-context-v1\0" + archive_raw).hexdigest()
    )
    _write_canonical_json(manifest_path, manifest)
    source_root = tmp_path / "source"
    source_root.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(source_root, filter="data")

    with pytest.raises(ValueError, match="trusted expected source"):
        release_wheel.verify_source_context(archive, manifest_path, source_root)


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
    commit_payload = Path(prepared["source_commit_payload_path"])
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
        "--commit-payload",
        str(commit_payload),
        "--expected-source-sha",
        REVIEWED_SOURCE_SHA,
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
    commit_payload = Path(prepared["source_commit_payload_path"])
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
        "--commit-payload",
        str(commit_payload),
        "--expected-source-sha",
        REVIEWED_SOURCE_SHA,
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "context" in rejected.stderr.lower() or "source" in rejected.stderr.lower()


def test_source_context_rejects_self_consistent_tar_with_nonexistent_source_sha(
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
    manifest_path = Path(prepared["source_manifest_path"])
    commit_payload = Path(prepared["source_commit_payload_path"])
    fake_source_sha = "f" * 40
    _rewrite_source_archive_comment(archive, fake_source_sha)
    archive_raw = archive.read_bytes()
    commit_raw = commit_payload.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        {
            "source_sha": fake_source_sha,
            "source_archive_sha256": "sha256:"
            + hashlib.sha256(archive_raw).hexdigest(),
            "source_commit_payload_sha256": "sha256:"
            + hashlib.sha256(commit_raw).hexdigest(),
            "build_context_sha256": _source_context_digest(archive_raw, commit_raw),
        }
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
        "--commit-payload",
        str(commit_payload),
        "--expected-source-sha",
        fake_source_sha,
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "commit object" in rejected.stderr.lower()


def test_source_context_rejects_commit_payload_from_another_commit(
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
    manifest_path = Path(prepared["source_manifest_path"])
    commit_payload = Path(prepared["source_commit_payload_path"])
    commit_raw = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "commit", f"{REVIEWED_SOURCE_SHA}^"],
        capture_output=True,
        check=True,
    ).stdout
    commit_payload.write_bytes(commit_raw)
    manifest = json.loads(manifest_path.read_text())
    manifest["source_commit_payload_sha256"] = (
        "sha256:" + hashlib.sha256(commit_raw).hexdigest()
    )
    manifest["build_context_sha256"] = _source_context_digest(
        archive.read_bytes(), commit_raw
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
        "--commit-payload",
        str(commit_payload),
        "--expected-source-sha",
        REVIEWED_SOURCE_SHA,
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "commit object" in rejected.stderr.lower()


@pytest.mark.parametrize("mutation", ("duplicate-tree", "malformed-header"))
def test_source_context_rejects_malformed_commit_payload(
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
    commit_payload = Path(prepared["source_commit_payload_path"])
    tree = json.loads(manifest_path.read_text())["source_tree"]
    malformed = (
        f"tree {tree}\ntree {tree}\n\nmessage\n".encode()
        if mutation == "duplicate-tree"
        else f"tree {tree}\nmalformed\n\nmessage\n".encode()
    )
    fake_source_sha = _git_object_sha1("commit", malformed)
    commit_payload.write_bytes(malformed)
    _rewrite_source_archive_comment(archive, fake_source_sha)
    archive_raw = archive.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        {
            "source_sha": fake_source_sha,
            "source_archive_sha256": "sha256:"
            + hashlib.sha256(archive_raw).hexdigest(),
            "source_commit_payload_sha256": "sha256:"
            + hashlib.sha256(malformed).hexdigest(),
            "build_context_sha256": _source_context_digest(archive_raw, malformed),
        }
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
        "--commit-payload",
        str(commit_payload),
        "--expected-source-sha",
        fake_source_sha,
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "commit" in rejected.stderr.lower()


@pytest.mark.parametrize(
    "mutation", ("wrong-type", "wrong-kind", "wrong-schema", "extra-manifest")
)
def test_source_context_rejects_wrong_object_type_or_extra_manifest_field(
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
    manifest_path = Path(prepared["source_manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    if mutation == "wrong-type":
        manifest["source_object_type"] = "blob"
    elif mutation == "wrong-kind":
        manifest["kind"] = "caller-source-context"
    elif mutation == "wrong-schema":
        manifest["schema_version"] = 2
    else:
        manifest["caller_trust"] = True
    _write_canonical_json(manifest_path, manifest)
    source_root = tmp_path / "source"
    source_root.mkdir()
    with tarfile.open(prepared["source_archive_path"]) as bundle:
        bundle.extractall(source_root, filter="data")

    rejected = _run(
        "wheel",
        "verify-context",
        "--archive",
        prepared["source_archive_path"],
        "--manifest",
        str(manifest_path),
        "--commit-payload",
        prepared["source_commit_payload_path"],
        "--expected-source-sha",
        REVIEWED_SOURCE_SHA,
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert (
        "manifest" in rejected.stderr.lower()
        or "object type" in rejected.stderr.lower()
    )


def test_source_context_rejects_external_expected_source_sha_mismatch(
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
    source_root = tmp_path / "source"
    source_root.mkdir()
    with tarfile.open(prepared["source_archive_path"]) as bundle:
        bundle.extractall(source_root, filter="data")

    rejected = _run(
        "wheel",
        "verify-context",
        "--archive",
        prepared["source_archive_path"],
        "--manifest",
        prepared["source_manifest_path"],
        "--commit-payload",
        prepared["source_commit_payload_path"],
        "--expected-source-sha",
        "f" * 40,
        "--source-root",
        str(source_root),
        check=False,
    )
    assert rejected.returncode == 2
    assert "expected source" in rejected.stderr.lower()


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
    task_path = _write_fixture_wheel_task(tmp_path, "cuda130-amd64")
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
        "--task-file",
        str(task_path),
        check=False,
    )
    assert rejected.returncode == 2
    assert message.lower() in rejected.stderr.lower()


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm 3 is required")
def test_fixture_chart_package_is_plan_derived_and_byte_deterministic(
    tmp_path: Path,
) -> None:
    plan = _fixture_resolved_plan()
    assert plan["fixture_only"] is True
    plan_path = tmp_path / "fixture-resolved-plan.json"
    _write_canonical_json(plan_path, plan)
    plan_arguments = (
        "--resolved-plan",
        str(plan_path),
        "--expected-plan-sha256",
        plan["resolved_plan_sha256"],
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    record_a = json.loads(
        _run("chart", "package", "--output-dir", str(first), *plan_arguments).stdout
    )
    record_b = json.loads(
        _run("chart", "package", "--output-dir", str(second), *plan_arguments).stdout
    )
    assert record_a == record_b
    assert record_a["rendered_cases"] == [
        case["name"] for case in plan["chart"]["validation_cases"]
    ]
    expected_evidence = {}
    for case in plan["chart"]["validation_cases"]:
        family = next(
            task
            for task in plan["family_tasks"]
            if task["product_id"] == case["product_id"]
            and task["runtime"]["variant"] == case["variant"]
        )
        expected_evidence[case["name"]] = {
            "image": (
                f"{family['runtime']['repository']}@"
                f"{family['runtime']['index_digest']}"
            ),
            "resource": case["expected_resource"],
        }
    assert record_a["rendered_evidence"] == expected_evidence
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
    assert provenance["source"]["commit"] == "33ac2a37f146a4515e232e4d7a8abaa14d8ef1d7"
    assert provenance["source"]["tree_sha256"] == (
        "sha256:5a0aa3113c14931e30c88c7f8508b3c742f985e5ede4a8ec48cac77c195c5a2e"
    )
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
