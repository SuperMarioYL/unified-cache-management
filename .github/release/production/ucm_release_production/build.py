"""Projection and deterministic comparison for production native wheels."""

from __future__ import annotations

import base64
import csv
import email.parser
import hashlib
import io
import re
import struct
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    ProductionError,
    canonical_bytes,
    require_lower_commit_sha,
    require_sha256_digest,
    sha256_envelope,
    verify_envelope,
)
from .config import validate_config
from .tags import TagIntent

_PROFILES = ("cuda130", "cann900-a2", "cann900-a3")
_ARCHITECTURES = ("amd64", "arm64")
_RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
_PLATFORMS = {"amd64": "linux/amd64", "arm64": "linux/arm64"}
_PROFILE_BUILD_SETTINGS = {
    "cuda130": ("uc-manager-cuda", "cuda"),
    "cann900-a2": ("uc-manager-cann-a2", "ascend"),
    "cann900-a3": ("uc-manager-cann-a3", "ascend-a3"),
}
_PRODUCTION_WHEEL_TASK_KEYS = {
    "kind",
    "schema_version",
    "spec_id",
    "profile_id",
    "distribution",
    "cpu_arch",
    "platform",
    "runner",
    "python_version",
    "python_abi",
    "wheel_platform",
    "base_version",
    "stage",
    "wheel_version",
    "source_sha",
    "source_identity_sha256",
    "builder",
    "runtime",
    "required_native",
    "forbidden_native",
    "dependency_lock_sha256",
    "runtime_requirements",
    "write_authority",
    "sha256",
}
_PRODUCTION_AUTHORITY_KEYS = {
    "schema_version",
    "kind",
    "spec_id",
    "profile_id",
    "distribution",
    "base_version",
    "stage",
    "cpu_arch",
    "platform",
    "wheel_version",
    "source_sha",
    "source_tree",
    "source_archive_sha256",
    "source_date_epoch",
    "task_sha256",
    "builder_coordinate",
    "builder_config_digest",
    "dependency_lock_sha256",
    "tool_wheels",
    "required_native",
    "forbidden_native",
    "build_context_sha256",
}
_NATIVE_REQUIRED_COMMON = (
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
)
_NATIVE_OPTIONAL = (
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
)
_SOURCE_IDENTITY_KEYS = {
    "kind",
    "schema_version",
    "repository",
    "repository_id",
    "stage",
    "tag_name",
    "tag_object_sha",
    "source_commit_sha",
    "source_branch",
    "tagger",
    "tagged_at",
    "tag_message_sha256",
    "control_default_branch",
    "control_sha",
    "lineage",
    "sha256",
}
_WHEEL_PATH = re.compile(r"[A-Za-z0-9_.+\-]+\.whl", re.ASCII)


def prepare_source_context(
    repository: Path, source_sha: str, output_dir: Path
) -> dict[str, Any]:
    """Export one exact Git commit without importing control code from that commit."""

    repository = Path(repository).resolve()
    output_dir = Path(output_dir)
    require_lower_commit_sha(source_sha, "source context commit")
    if (
        not (repository / ".git").exists()
        and not subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    ):
        raise ProductionError("source repository is not a Git checkout")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ProductionError("source context output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    def git(*args: str, text: bool = True) -> str | bytes:
        completed = subprocess.run(
            ["git", "-C", str(repository), *args],
            text=text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            raise ProductionError("Git source context command failed")
        return completed.stdout

    object_type = str(git("cat-file", "-t", source_sha)).strip()
    if object_type != "commit":
        raise ProductionError("source context identity must be a commit")
    tree = str(git("rev-parse", f"{source_sha}^{{tree}}")).strip()
    require_lower_commit_sha(tree, "source context tree")
    commit = git("cat-file", "commit", source_sha, text=False)
    archive = git("archive", "--format=tar", source_sha, text=False)
    assert isinstance(commit, bytes) and isinstance(archive, bytes)
    commit_path = output_dir / "source-commit.payload"
    archive_path = output_dir / "ucm-source.tar"
    commit_path.write_bytes(commit)
    archive_path.write_bytes(archive)
    manifest = {
        "kind": "ucm-production-source-context",
        "schema_version": 1,
        "source_sha": source_sha,
        "source_tree": tree,
        "source_commit_sha256": "sha256:" + hashlib.sha256(commit).hexdigest(),
        "source_archive_sha256": "sha256:" + hashlib.sha256(archive).hexdigest(),
    }
    manifest["build_context_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            b"ucm-production-source-context-v1\0"
            + canonical_bytes(manifest)
            + b"\0"
            + commit
            + b"\0"
            + archive
        ).hexdigest()
    )
    (output_dir / "source-context.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return manifest


def tool_wheel_authority(config: dict[str, Any], architecture: str) -> dict[str, str]:
    """Project the exact seven build-tool wheel filenames and digests."""

    config = validate_config(config)
    if architecture not in _ARCHITECTURES:
        raise ProductionError("tool wheel architecture is invalid")
    result: dict[str, str] = {}
    for item in config["toolchain"]["python_build"].values():
        result[item["filename"]] = "sha256:" + item["sha256"]
    for name in ("pyyaml", "cmake"):
        item = config["toolchain"][name][architecture]
        result[item["filename"]] = "sha256:" + item["sha256"]
    if len(result) != 7:
        raise ProductionError(
            "production tool wheel authority must contain seven files"
        )
    return dict(sorted(result.items()))


def docker_build_projection(
    config: dict[str, Any], task: dict[str, Any], context: dict[str, Any], epoch: int
) -> dict[str, Any]:
    """Project only fixed Docker target/build arguments for one native wheel."""

    config = validate_config(config)
    if type(epoch) is not int or not 315532800 <= epoch <= 4354819199:
        raise ProductionError("source epoch is outside the canonical ZIP range")
    architecture = task["cpu_arch"]
    tools = tool_wheel_authority(config, architecture)
    authority = authority_from_task(
        task,
        source_tree=context["source_tree"],
        source_archive_sha256=context["source_archive_sha256"],
        source_date_epoch=epoch,
        build_context_sha256=context["build_context_sha256"],
        tool_wheels=tools,
    )
    platform_arg = {
        "cuda130": "cuda",
        "cann900-a2": "ascend",
        "cann900-a3": "ascend-a3",
    }[task["profile_id"]]
    build_args: dict[str, str] = {
        "SOURCE_DATE_EPOCH": str(epoch),
        "UCM_BUILDER_IMAGE": (
            f"{task['builder']['repository']}@{task['builder']['manifest_digest']}"
        ),
        "PLATFORM": platform_arg,
        "UCM_RELEASE_PROFILE": task["profile_id"],
        "UCM_RELEASE_DISTRIBUTION": task["distribution"],
        "UCM_RELEASE_SOURCE_SHA": task["source_sha"],
        "UCM_RELEASE_VERSION": task["wheel_version"],
        "UCM_RELEASE_BUILD_KEY": "sha256:" + task["sha256"],
        "UCM_RELEASE_REQUIRED_TARGETS": ",".join(task["required_native"]),
        "UCM_RELEASE_FORBIDDEN_TARGETS": ",".join(task["forbidden_native"]),
    }
    prefixes = {
        "build": "BUILD",
        "pyproject-hooks": "PYPROJECT_HOOKS",
        "packaging": "PACKAGING",
        "setuptools": "SETUPTOOLS",
        "wheel": "WHEEL",
    }
    for name, prefix in prefixes.items():
        item = config["toolchain"]["python_build"][name]
        build_args[f"{prefix}_VERSION"] = item["version"]
        build_args[f"{prefix}_FILENAME"] = item["filename"]
        build_args[f"{prefix}_SHA256"] = "sha256:" + item["sha256"]
    for name, prefix in (("pyyaml", "PYYAML"), ("cmake", "CMAKE")):
        group = config["toolchain"][name]
        item = group[architecture]
        build_args[f"{prefix}_VERSION"] = group["version"]
        build_args[f"{prefix}_FILENAME"] = item["filename"]
        build_args[f"{prefix}_SHA256"] = "sha256:" + item["sha256"]
    return {
        "docker_target": "production-wheel",
        "platform": task["platform"],
        "runner": task["runner"],
        "build_args": dict(sorted(build_args.items())),
        "authority": authority,
    }


_NATIVE_DIRECTORIES = {
    "ucmtrans": "ucm/shared/trans",
    "metrics": "ucm/shared/metrics",
    "ucmmetrics": "ucm/shared/metrics",
    "ucmlogger": "ucm/shared/infra",
    "ucmnfsstore": "ucm/store/nfsstore",
    "ucmpcstore": "ucm/store/pcstore",
    "posixstore": "ucm/store/posix",
    "compressor": "ucm/store/compress",
    "cachestore": "ucm/store/cache",
    "emptystore": "ucm/store/empty",
    "fakestore": "ucm/store/fake",
    "ucmpipelinestore": "ucm/store/pipeline",
    "mooncakestore": "ucm/store/mooncakestore",
    "ds3fsstore": "ucm/store/ds3fs",
}
_SHARED_LIBRARIES = {
    "metrics",
    "posixstore",
    "compressor",
    "cachestore",
    "emptystore",
    "fakestore",
    "mooncakestore",
    "ds3fsstore",
}


def _native_component(name: str, task: dict[str, Any]) -> str | None:
    basename = PurePosixPath(name).name
    for component in (*task["required_native"], *task["forbidden_native"]):
        if component not in _NATIVE_DIRECTORIES:
            continue
        if not name.startswith(_NATIVE_DIRECTORIES[component] + "/"):
            continue
        if component in _SHARED_LIBRARIES:
            expected = f"lib{component}.so"
            if basename == expected:
                return component
        elif re.fullmatch(
            rf"{re.escape(component)}\.cpython-312-[A-Za-z0-9_-]+\.so", basename
        ):
            return component
    return None


def _verify_task_native_members(
    entries: dict[str, bytes], task: dict[str, Any]
) -> None:
    observed: dict[str, str] = {}
    expected_machine = {"amd64": 62, "arm64": 183}[task["cpu_arch"]]
    for name, raw in entries.items():
        if not name.endswith(".so"):
            continue
        if len(raw) < 20 or raw[:6] != b"\x7fELF\x02\x01":
            raise ProductionError(f"wheel native member is not ELF64: {name}")
        if struct.unpack_from("<H", raw, 18)[0] != expected_machine:
            raise ProductionError(f"wheel native member architecture differs: {name}")
        component = _native_component(name, task)
        if component is None or component in observed:
            raise ProductionError(f"wheel native component is not exact: {name}")
        observed[component] = name
    required = set(task["required_native"])
    forbidden = set(task["forbidden_native"])
    if set(observed) != required or set(observed) & forbidden:
        raise ProductionError("wheel required/forbidden native closure differs")


def seal_built_wheel(
    raw_wheel: Path,
    output_dir: Path,
    task: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Canonicalize one native wheel and bind its schema-v2 build authority."""

    raw_wheel = Path(raw_wheel)
    output_dir = Path(output_dir)
    if not raw_wheel.is_file() or raw_wheel.is_symlink():
        raise ProductionError("raw production wheel must be one regular file")
    if authority.get("schema_version") != 2 or authority.get("task_sha256") != (
        "sha256:" + task["sha256"]
    ):
        raise ProductionError("production build authority differs from task")
    try:
        with zipfile.ZipFile(raw_wheel) as archive:
            metadata, record_name, _ = _metadata(archive)
            _record(archive, record_name)
            entries = {
                item.filename: archive.read(item.filename)
                for item in archive.infolist()
                if not item.is_dir() and item.filename != record_name
            }
    except zipfile.BadZipFile:
        raise ProductionError("raw production wheel is not a ZIP archive") from None
    if (
        metadata.get("Name") != task["distribution"]
        or metadata.get("Version") != task["wheel_version"]
        or metadata.get_all("Requires-Dist", []) != task["runtime_requirements"]
    ):
        raise ProductionError("raw production wheel metadata differs from task")
    _verify_task_native_members(entries, task)
    dist_info = PurePosixPath(record_name).parent.as_posix()
    authority_name = f"{dist_info}/ucm-production-build-authority.json"
    if authority_name in entries:
        raise ProductionError("raw production wheel already contains authority")
    entries[authority_name] = canonical_bytes(authority) + b"\n"
    record_rows: list[list[str]] = []
    for name in sorted(entries):
        raw = entries[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
        record_rows.append([name, "sha256=" + digest.decode(), str(len(raw))])
    record_rows.append([record_name, "", ""])
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(record_rows)
    entries[record_name] = buffer.getvalue().encode()

    epoch = authority["source_date_epoch"]
    import time

    timestamp_values = list(time.gmtime(epoch)[:6])
    timestamp_values[5] -= timestamp_values[5] % 2
    timestamp = tuple(timestamp_values)
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}[task["cpu_arch"]]
    filename = (
        f"{task['distribution'].replace('-', '_')}-{task['wheel_version']}-"
        f"cp312-cp312-{task['wheel_platform']}_{architecture}.whl"
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ProductionError("production wheel output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / filename
    with zipfile.ZipFile(
        output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[name])
    inspected = _inspect_wheel(output, task)
    record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": filename,
            "file_sha256": inspected["sha256"],
            "task_sha256": "sha256:" + task["sha256"],
            "source_sha": task["source_sha"],
            "runtime_requirements": task["runtime_requirements"],
        }
    )
    (output_dir / "record.json").write_bytes(canonical_bytes(record) + b"\n")
    return record


def _profile(config: dict[str, Any], profile_id: str) -> dict[str, Any]:
    matches = [item for item in config["build_profiles"] if item["id"] == profile_id]
    if len(matches) != 1:
        raise ProductionError(f"build spec has no unique profile: {profile_id}")
    return matches[0]


def _source_identity(value: object, intent: TagIntent) -> tuple[dict[str, Any], str]:
    source = verify_envelope(
        value,
        kind="ucm-production-source-identity",
        schema_version=1,
        exact_keys=_SOURCE_IDENTITY_KEYS,
    )
    if (
        source["stage"] != intent.stage
        or source["tag_name"] != intent.tag_name
        or source["source_branch"] != intent.release_branch
    ):
        raise ProductionError("source identity does not match Tag intent")
    source_sha = require_lower_commit_sha(
        source["source_commit_sha"], "source identity commit"
    )
    return source, source_sha


def _dependency_lock(config: dict[str, Any], architecture: str) -> dict[str, Any]:
    toolchain = config["toolchain"]
    return {
        "python_build": toolchain["python_build"],
        "pyyaml": toolchain["pyyaml"][architecture],
        "cmake": toolchain["cmake"][architecture],
        "wrapt": toolchain["wrapt"][architecture],
    }


def _runtime_requirements(config: dict[str, Any]) -> list[str]:
    toolchain = config["toolchain"]
    return sorted(
        [
            "packaging==" + toolchain["python_build"]["packaging"]["version"],
            "wrapt==" + toolchain["wrapt"]["version"],
        ]
    )


def project_build_task(
    config: dict[str, Any],
    intent: TagIntent,
    source: object,
    spec_id: str,
) -> dict[str, Any]:
    """Project one of the only six production wheel build tasks."""

    config = validate_config(config)
    valid = [f"{profile}-{arch}" for profile in _PROFILES for arch in _ARCHITECTURES]
    if spec_id not in valid:
        raise ProductionError("spec_id is not one of the six production build specs")
    profile_id, architecture = next(
        (profile, arch)
        for profile in _PROFILES
        for arch in _ARCHITECTURES
        if spec_id == f"{profile}-{arch}"
    )
    source_value, source_sha = _source_identity(source, intent)
    profile = _profile(config, profile_id)
    required_native = list(_NATIVE_REQUIRED_COMMON)
    if profile_id != "cuda130":
        required_native.append("mooncakestore")
    forbidden_native = sorted(set(_NATIVE_OPTIONAL) - set(required_native))
    dependency_lock = _dependency_lock(config, architecture)
    unsigned = {
        "kind": "ucm-production-wheel-build-task",
        "schema_version": 1,
        "spec_id": spec_id,
        "profile_id": profile_id,
        "distribution": profile["distribution"],
        "cpu_arch": architecture,
        "platform": _PLATFORMS[architecture],
        "runner": _RUNNERS[architecture],
        "python_version": profile["python_version"],
        "python_abi": profile["python_abi"],
        "wheel_platform": profile["wheel_platform"],
        "base_version": (
            intent.version if intent.stage == "hotfix" else config["base_version"]
        ),
        "stage": intent.stage,
        "wheel_version": intent.wheel_version,
        "source_sha": source_sha,
        "source_identity_sha256": source_value["sha256"],
        "builder": profile["builders"][architecture],
        "runtime": profile["runtime"][architecture],
        "required_native": required_native,
        "forbidden_native": forbidden_native,
        "dependency_lock_sha256": "sha256:"
        + hashlib.sha256(canonical_bytes(dependency_lock)).hexdigest(),
        "runtime_requirements": _runtime_requirements(config),
        "write_authority": [],
    }
    return sha256_envelope(unsigned)


def authority_from_task(
    task: dict[str, Any],
    *,
    source_tree: str,
    source_archive_sha256: str,
    source_date_epoch: int,
    build_context_sha256: str,
    tool_wheels: dict[str, str],
) -> dict[str, Any]:
    """Create the schema-v2 setup authority from a trusted projected task."""

    require_lower_commit_sha(source_tree, "source tree")
    for label, value in {
        "source archive": source_archive_sha256,
        "build context": build_context_sha256,
        "dependency lock": task["dependency_lock_sha256"],
    }.items():
        require_sha256_digest(value, label)
    if (
        type(source_date_epoch) is not int
        or not 315532800 <= source_date_epoch <= 4354819199
    ):
        raise ProductionError("source_date_epoch must fit the canonical ZIP range")
    builder = task["builder"]
    return {
        "schema_version": 2,
        "kind": "ucm-native-build-authority",
        "spec_id": task["spec_id"],
        "profile_id": task["profile_id"],
        "distribution": task["distribution"],
        "base_version": task["base_version"],
        "stage": task["stage"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "wheel_version": task["wheel_version"],
        "source_sha": task["source_sha"],
        "source_tree": source_tree,
        "source_archive_sha256": source_archive_sha256,
        "source_date_epoch": source_date_epoch,
        "task_sha256": "sha256:" + task["sha256"],
        "builder_coordinate": (f"{builder['repository']}@{builder['manifest_digest']}"),
        "builder_config_digest": builder["config_digest"],
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "tool_wheels": tool_wheels,
        "required_native": task["required_native"],
        "forbidden_native": task["forbidden_native"],
        "build_context_sha256": build_context_sha256,
    }


def wheel_build_config_from_task(
    task: dict[str, Any], authority: dict[str, Any]
) -> dict[str, Any]:
    """Project the canonical pre-Buildx setup wrapper for a production task."""
    task = verify_envelope(
        task,
        kind="ucm-production-wheel-build-task",
        schema_version=1,
        exact_keys=_PRODUCTION_WHEEL_TASK_KEYS,
    )
    if not isinstance(authority, dict) or set(authority) != _PRODUCTION_AUTHORITY_KEYS:
        raise ProductionError("production wheel build authority fields are not exact")
    builder = task["builder"]
    expected = {
        "schema_version": 2,
        "kind": "ucm-native-build-authority",
        "spec_id": task["spec_id"],
        "profile_id": task["profile_id"],
        "distribution": task["distribution"],
        "base_version": task["base_version"],
        "stage": task["stage"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "wheel_version": task["wheel_version"],
        "source_sha": task["source_sha"],
        "task_sha256": "sha256:" + task["sha256"],
        "builder_coordinate": (f"{builder['repository']}@{builder['manifest_digest']}"),
        "builder_config_digest": builder["config_digest"],
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "required_native": task["required_native"],
        "forbidden_native": task["forbidden_native"],
    }
    mismatches = [
        field for field, value in expected.items() if authority.get(field) != value
    ]
    if mismatches:
        raise ProductionError(
            f"production wheel build authority differs from task: {mismatches}"
        )
    profile = task["profile_id"]
    profile_settings = _PROFILE_BUILD_SETTINGS.get(profile)
    if (
        profile_settings is None
        or task["distribution"] != profile_settings[0]
        or task["spec_id"] != f"{profile}-{task['cpu_arch']}"
        or task["platform"] != _PLATFORMS.get(task["cpu_arch"])
        or task["python_version"] != "3.12"
        or task["python_abi"] != "cp312"
    ):
        raise ProductionError("production wheel build task profile is invalid")
    return {
        "authority": authority,
        "distribution": task["distribution"],
        "kind": "ucm-wheel-build-config",
        "platform": profile_settings[1],
        "python": {
            "abi": task["python_abi"],
            "version": task["python_version"],
        },
        "runtime_requirements": task["runtime_requirements"],
        "schema_version": 1,
    }


def _safe_member(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return all(part not in {"", ".", ".."} for part in path.parts)


def _metadata(archive: zipfile.ZipFile) -> tuple[email.message.Message, str, bytes]:
    files = [item for item in archive.infolist() if not item.is_dir()]
    names = [item.filename for item in files]
    if len(names) != len(set(names)) or any(not _safe_member(name) for name in names):
        raise ProductionError("wheel contains duplicate or unsafe members")
    metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(metadata_names) != 1 or len(record_names) != 1:
        raise ProductionError("wheel must contain exactly one METADATA and RECORD")
    raw_metadata = archive.read(metadata_names[0])
    metadata = email.parser.BytesParser().parsebytes(raw_metadata)
    return metadata, record_names[0], raw_metadata


def _record(archive: zipfile.ZipFile, record_name: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error):
        raise ProductionError("wheel RECORD is invalid UTF-8 CSV") from None
    expected = {item.filename for item in archive.infolist() if not item.is_dir()}
    if {row[0] for row in rows if len(row) == 3} != expected or any(
        len(row) != 3 for row in rows
    ):
        raise ProductionError("wheel RECORD does not cover the exact member set")
    for name, digest, size in rows:
        if name == record_name:
            if digest or size:
                raise ProductionError("wheel RECORD self row must be unhashed")
            continue
        raw = archive.read(name)
        actual = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
        if digest != f"sha256={actual.decode()}" or size != str(len(raw)):
            raise ProductionError(f"wheel RECORD digest differs for {name}")


def _native_architecture(archive: zipfile.ZipFile, architecture: str) -> None:
    native = [
        item.filename
        for item in archive.infolist()
        if not item.is_dir() and item.filename.endswith(".so")
    ]
    if not native:
        raise ProductionError("wheel has no native shared objects")
    expected_machine = {"amd64": 62, "arm64": 183}[architecture]
    for name in native:
        raw = archive.read(name)
        if len(raw) < 20 or raw[:6] != b"\x7fELF\x02\x01":
            raise ProductionError(
                f"wheel native member is not ELF64 little-endian: {name}"
            )
        if struct.unpack_from("<H", raw, 18)[0] != expected_machine:
            raise ProductionError(f"wheel native member architecture differs: {name}")


def _inspect_wheel(path: Path, task: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file() or _WHEEL_PATH.fullmatch(path.name) is None:
        raise ProductionError("wheel path must name one regular .whl file")
    try:
        with zipfile.ZipFile(path) as archive:
            metadata, record_name, _ = _metadata(archive)
            _record(archive, record_name)
            _native_architecture(archive, task["cpu_arch"])
    except zipfile.BadZipFile:
        raise ProductionError("wheel is not a valid ZIP archive") from None
    distribution = metadata.get("Name")
    version = metadata.get("Version")
    requires_dist = metadata.get_all("Requires-Dist", [])
    if distribution != task["distribution"]:
        raise ProductionError("wheel distribution differs from production task")
    if version != task["wheel_version"]:
        raise ProductionError("wheel version differs from production task")
    if requires_dist != task["runtime_requirements"]:
        raise ProductionError("wheel dependency metadata is not the reviewed closure")
    return {
        "distribution": distribution,
        "version": version,
        "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def compare_wheel_candidates(
    candidate: Path, trusted: Path, task: dict[str, Any]
) -> dict[str, Any]:
    """Reopen both wheels and require exact, reproducible production bytes."""

    candidate_result = _inspect_wheel(candidate, task)
    trusted_result = _inspect_wheel(trusted, task)
    if (
        candidate_result != trusted_result
        or candidate.read_bytes() != trusted.read_bytes()
    ):
        raise ProductionError(
            "candidate and trusted wheels are not byte-for-byte identical"
        )
    return {
        "kind": "ucm-production-wheel-comparison",
        "schema_version": 1,
        **candidate_result,
        "identical": True,
        "task_sha256": task["sha256"],
    }
