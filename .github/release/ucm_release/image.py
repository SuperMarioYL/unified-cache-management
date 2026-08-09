"""Deterministic, fixture-only install-image context and result verification."""

from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import core as release_core
from . import registry
from . import wheel as wheel_artifact
from .core import (
    DEFAULT_SCHEMA_DIR,
    canonical_bytes,
    load_json,
    sha256_value,
    validate_schema,
)

DOCKER_ROOT = Path(__file__).resolve().parents[1] / "docker"
DOCKER_FILES = (
    "Dockerfile",
    "install_ucm.py",
    "inspect_runtime.py",
    "verify_base_image.py",
)
CONTEXT_METADATA = "image-metadata.json"
CONTEXT_RECIPE = "image-recipe.json"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?" r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
COMPILE_COMMAND_RE = re.compile(
    r"(?im)^\s*(?:RUN\s+)?[^#\n]*(?:\bcmake\b|\bninja\b|\bmake\b|\bgcc\b|g\+\+|\bclang\b|\bpip\s+wheel\b|python[^\n]*\s-m\s+build\b)"
)
SOURCE_BUILD_COMMAND_RE = re.compile(
    r"(?im)^\s*(?:RUN\s+)?[^#\n]*(?:"
    r"python[^\n]*\bsetup\.py\b|"
    r"\bpip\s+install\s+(?:\.|/workspace/ucm)(?:\s|$)|"
    r"\bucm_release\s+wheel\s+(?:native-)?build\b|"
    r"\bbuild_ext\b)"
)
FROM_RE = re.compile(
    r"(?im)^\s*FROM(?:\s+--[^\s]+)*\s+(?P<base>[^\s]+)"
    r"(?:\s+AS\s+(?P<alias>[A-Za-z0-9_.-]+))?\s*$"
)
COPY_FROM_RE = re.compile(
    r"(?im)^\s*COPY\s+(?:--[^\s]+\s+)*--from=(?P<stage>[^\s]+)\s+"
)
SOURCE_COPY_RE = re.compile(
    r"(?im)^\s*(?:COPY|ADD)\s+(?:--[^\s]+\s+)*(?:"
    r"\.(?:/)?\s+|"
    r"(?:setup\.py|pyproject\.toml|CMakeLists\.txt)(?:\s|$)|"
    r"(?:ucm|scripts)(?:/|\s))"
)
INSTALL_IMAGE_TARGET = "runtime"
REAL_INSTALL_TARGET = "runtime-real"
INSTALL_IMAGE_TARGETS = (INSTALL_IMAGE_TARGET, REAL_INSTALL_TARGET)
OCI_STREAM_CHUNK_SIZE = 1024 * 1024
OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
OCI_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
OCI_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
FIXTURE_BASE_AUTHORITY = {
    "schema_version": 1,
    "kind": "ucm-fixture-base-authority",
    "repository": "docker.io/library/python",
    "target_platform": "linux/amd64",
    "index_digest": "sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7",
    "manifest_digest": "sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49",
    "config_digest": "sha256:688a685f6a1fa9250d7c6cee916889cbca364e4b027520110e0fce80c64a13e0",
}
FIXTURE_IMAGE_TOOLCHAIN_AUTHORITY = {
    "schema_version": 1,
    "kind": "ucm-fixture-image-toolchain-authority",
    "buildx_version": "v0.19.2",
    "buildx_linux_sha256": {
        "amd64": "sha256:a5ff61c0b6d2c8ee20964a9d6dac7a7a6383c4a4a0ee8d354e983917578306ea",
        "arm64": "sha256:bd54f0e28c29789da1679bad2dd94c1923786ccd2cd80dd3a0a1d560a6baf10c",
    },
    "buildkit_image": (
        "moby/buildkit:v0.18.2@"
        "sha256:86c0ad9d1137c186e9d455912167df20e530bdf7f7c19de802e892bb8ca16552"
    ),
}
REAL_IMAGE_CONTEXT_RECORD = "image-authority.json"
REAL_REQUIREMENTS_LOCK = "requirements.lock"
REAL_DETERMINISTIC_FLAGS = [
    "--provenance=false",
    "--sbom=false",
    "oci-mediatypes=true",
    "rewrite-timestamp=true",
]


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) != keys:
        raise ValueError(
            f"{label} fields mismatch: missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase sha256:<64 hex>")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def fixture_base_authority() -> dict[str, Any]:
    """Return the single fixed base identity used by the fork candidate lane."""
    return copy.deepcopy(FIXTURE_BASE_AUTHORITY)


def validate_image_toolchain_authority(value: object) -> dict[str, Any]:
    """Validate the canonical Buildx binary and BuildKit image policy shape."""
    authority = _exact(
        value,
        {
            "schema_version",
            "kind",
            "buildx_version",
            "buildx_linux_sha256",
            "buildkit_image",
        },
        "fixture image toolchain authority",
    )
    if (
        authority["schema_version"] != 1
        or authority["kind"] != "ucm-fixture-image-toolchain-authority"
        or not isinstance(authority["buildx_version"], str)
        or re.fullmatch(
            r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            authority["buildx_version"],
        )
        is None
    ):
        raise ValueError("fixture image Buildx version is invalid")
    binary_sha256 = _exact(
        authority["buildx_linux_sha256"],
        {"amd64", "arm64"},
        "fixture image Buildx binary digests",
    )
    for architecture, digest in binary_sha256.items():
        _digest(digest, f"fixture image Buildx {architecture} digest")
    buildkit_image = authority["buildkit_image"]
    if (
        not isinstance(buildkit_image, str)
        or re.fullmatch(
            r"moby/buildkit:v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)@sha256:[0-9a-f]{64}",
            buildkit_image,
        )
        is None
    ):
        raise ValueError("fixture image BuildKit image is not digest-pinned")
    return copy.deepcopy(authority)


def fixture_image_toolchain_authority() -> dict[str, Any]:
    """Return the one image-toolchain identity consumed by planning and CI."""
    return validate_image_toolchain_authority(FIXTURE_IMAGE_TOOLCHAIN_AUTHORITY)


def real_image_toolchain_authority(
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Return the reviewed Buildx, BuildKit, frontend, and determinism authority."""
    fixture = fixture_image_toolchain_authority()
    dockerfile = (Path(docker_root) / "Dockerfile").read_text(encoding="utf-8")
    first_line = dockerfile.splitlines()[0] if dockerfile.splitlines() else ""
    prefix = "# syntax="
    if (
        not first_line.startswith(prefix)
        or re.fullmatch(
            r"docker/dockerfile:v?[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}",
            first_line.removeprefix(prefix),
        )
        is None
    ):
        raise ValueError("Dockerfile frontend must be versioned and digest-pinned")
    if REAL_INSTALL_TARGET not in _docker_stages(dockerfile):
        raise ValueError("Dockerfile is missing real install-only runtime target")
    result = {
        "schema_version": 1,
        "kind": "ucm-real-image-toolchain-authority",
        "buildx_version": fixture["buildx_version"],
        "buildx_linux_sha256": fixture["buildx_linux_sha256"],
        "buildkit_image": fixture["buildkit_image"],
        "dockerfile_frontend": first_line.removeprefix(prefix),
        "deterministic_flags": list(REAL_DETERMINISTIC_FLAGS),
    }
    result["authority_sha256"] = sha256_value(result)
    return result


def real_image_authorities(
    *,
    release_path: Path = release_core.DEFAULT_RELEASE,
    compatibility_path: Path = release_core.DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    docker_root: Path = DOCKER_ROOT,
) -> list[dict[str, Any]]:
    """Project the only six install-only member authorities from release.yaml."""
    release, _ = release_core.validate_config(
        release_path, compatibility_path, schema_dir
    )
    matrix = release_core.build_matrix(
        "feature-candidate", release_path, compatibility_path, schema_dir
    )
    tasks = matrix["tasks"]
    if len(tasks) != 6:
        raise ValueError("real image authority requires exactly six matrix tasks")
    toolchain = real_image_toolchain_authority(docker_root)
    result: list[dict[str, Any]] = []
    for task in tasks:
        architecture = task["cpu_arch"]
        authority: dict[str, Any] = {
            "schema_version": 1,
            "kind": "ucm-real-image-task-authority",
            "candidate_kind": "real-candidate",
            "fixture_only": False,
            "unpublished": True,
            "publication_attempted": False,
            "spec_id": task["spec_id"],
            "family_id": task["profile_id"],
            "profile_id": task["profile_id"],
            "cpu_arch": architecture,
            "platform": task["platform"],
            "python_abi": task["python_abi"],
            "wheel_version": task["wheel_version"],
            "builder": copy.deepcopy(task["builder"]),
            "runtime": copy.deepcopy(task["runtime"]),
            "target_repository": task["target_repository"],
            "target_tag": task["target_tag"],
            "required_native": copy.deepcopy(task["required_native"]),
            "forbidden_native": copy.deepcopy(task["forbidden_native"]),
            "allowed_dt_needed": copy.deepcopy(task["allowed_dt_needed"]),
            "dependency_lock_sha256": task["dependency_lock_sha256"],
            "wrapt_wheel": copy.deepcopy(release["wrapt_wheels"][architecture]),
            "task_sha256": task["task_sha256"],
            "toolchain": copy.deepcopy(toolchain),
        }
        authority["authority_sha256"] = sha256_value(authority)
        result.append(authority)
    return result


def real_image_authority(
    family_id: str,
    architecture: str,
    *,
    release_path: Path = release_core.DEFAULT_RELEASE,
    compatibility_path: Path = release_core.DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Resolve one member only by its reviewed family and CPU architecture."""
    authorities = real_image_authorities(
        release_path=release_path,
        compatibility_path=compatibility_path,
        schema_dir=schema_dir,
        docker_root=docker_root,
    )
    families = {item["family_id"] for item in authorities}
    if family_id not in families:
        raise ValueError(f"unknown real image family: {family_id!r}")
    if architecture not in {"amd64", "arm64"}:
        raise ValueError(f"unknown real image architecture: {architecture!r}")
    matches = [
        item
        for item in authorities
        if item["family_id"] == family_id and item["cpu_arch"] == architecture
    ]
    if len(matches) != 1:
        raise ValueError("real image family/architecture does not resolve uniquely")
    return matches[0]


def _validate_base_authority(value: object) -> dict[str, Any]:
    authority = _exact(
        value,
        {
            "schema_version",
            "kind",
            "repository",
            "target_platform",
            "index_digest",
            "manifest_digest",
            "config_digest",
        },
        "fixture base authority",
    )
    for field in ("index_digest", "manifest_digest", "config_digest"):
        _digest(authority[field], f"fixture base {field}")
    if authority != FIXTURE_BASE_AUTHORITY:
        raise ValueError("fixture base authority differs from release policy")
    return copy.deepcopy(authority)


def _json_bytes(content: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            value[key] = item
        return value

    try:
        value = json.loads(content, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON in {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _docker_stages(dockerfile: str) -> dict[str, dict[str, Any]]:
    """Parse the named Docker stages and their prior-stage dependencies."""
    matches = list(FROM_RE.finditer(dockerfile))
    stages: dict[str, dict[str, Any]] = {}
    aliases_by_index: list[str | None] = []
    for index, match in enumerate(matches):
        alias_value = match.group("alias")
        alias = alias_value.lower() if alias_value is not None else None
        if alias is not None and alias in stages:
            raise ValueError(f"Dockerfile has duplicate stage alias {alias!r}")
        end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(dockerfile)
        )
        body = dockerfile[match.end() : end]
        dependencies: set[str] = set()
        base = match.group("base").lower()
        if base in stages:
            dependencies.add(base)
        elif base.isdecimal() and int(base) < len(aliases_by_index):
            dependency = aliases_by_index[int(base)]
            if dependency is not None:
                dependencies.add(dependency)
        for copy_match in COPY_FROM_RE.finditer(body):
            reference = copy_match.group("stage").lower()
            if reference in stages:
                dependencies.add(reference)
            elif reference.isdecimal() and int(reference) < len(aliases_by_index):
                dependency = aliases_by_index[int(reference)]
                if dependency is not None:
                    dependencies.add(dependency)
        if alias is not None:
            stages[alias] = {
                "body": body,
                "dependencies": dependencies,
            }
        aliases_by_index.append(alias)
    return stages


def _docker_instruction_arguments(body: str) -> list[tuple[str, list[str]]]:
    """Parse shell/JSON arguments for the instructions relevant to the audit."""
    parsed: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        match = re.match(r"^\s*(COPY|ADD|RUN)\s+(.+?)\s*$", line, re.IGNORECASE)
        if match is None:
            continue
        instruction = match.group(1).upper()
        raw = match.group(2)
        # Shell-form instructions may continue across physical lines.  The
        # existing whole-stage regexes audit those commands; only parse a
        # complete shell line here.  JSON form is necessarily self-contained
        # and must be parsed so array-form COPY/RUN cannot evade the audit.
        if not raw.startswith("[") and raw.endswith("\\"):
            continue
        json_offset = raw.find("[")
        prefix = raw[:json_offset].strip() if json_offset >= 0 else ""
        prefix_arguments = shlex.split(prefix) if prefix else []
        json_form = json_offset >= 0 and all(
            argument.startswith("--") for argument in prefix_arguments
        )
        try:
            if json_form:
                json_arguments = json.loads(raw[json_offset:])
                if not isinstance(json_arguments, list) or not all(
                    isinstance(argument, str) for argument in json_arguments
                ):
                    raise ValueError("JSON arguments must be a string array")
                arguments = [*prefix_arguments, *json_arguments]
            else:
                arguments = shlex.split(raw)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(
                f"Dockerfile has invalid {instruction} instruction: {error}"
            ) from error
        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise ValueError(
                f"Dockerfile {instruction} arguments must be a string array"
            )
        parsed.append((instruction, arguments))
    return parsed


def _source_copy_argument(argument: str) -> bool:
    normalized = posixpath.normpath("/" + argument.lstrip("/")).lstrip("/") or "."
    return normalized in {
        ".",
        "setup.py",
        "pyproject.toml",
        "CMakeLists.txt",
        "ucm",
        "scripts",
    } or normalized.startswith(("ucm/", "scripts/"))


def _audit_stage_instructions(stage_name: str, body: str, target: str) -> None:
    if COMPILE_COMMAND_RE.search(body) or SOURCE_BUILD_COMMAND_RE.search(body):
        raise ValueError(
            f"install-only target {target!r} reaches compile/source-build "
            f"commands in stage {stage_name!r}"
        )
    if SOURCE_COPY_RE.search(body):
        raise ValueError(
            f"install-only target {target!r} reaches a UCM source COPY "
            f"in stage {stage_name!r}"
        )
    for instruction, arguments in _docker_instruction_arguments(body):
        if instruction in {"COPY", "ADD"}:
            sources = [value for value in arguments if not value.startswith("--")][:-1]
            if any(_source_copy_argument(source) for source in sources):
                raise ValueError(
                    f"install-only target {target!r} reaches a UCM "
                    f"source {instruction} in stage {stage_name!r}"
                )
        elif instruction == "RUN":
            normalized = "RUN " + " ".join(arguments)
            if COMPILE_COMMAND_RE.search(normalized) or SOURCE_BUILD_COMMAND_RE.search(
                normalized
            ):
                raise ValueError(
                    f"install-only target {target!r} reaches "
                    f"compile/source-build commands in stage {stage_name!r}"
                )


def _audit_install_only_target(dockerfile: str) -> None:
    """Reject source builds in the final runtime stage dependency closure."""
    stages = _docker_stages(dockerfile)
    for target in INSTALL_IMAGE_TARGETS:
        if target not in stages:
            if target == REAL_INSTALL_TARGET:
                continue
            raise ValueError(f"Dockerfile is missing install-only target {target!r}")
        pending = [target]
        reachable: set[str] = set()
        while pending:
            stage_name = pending.pop()
            if stage_name in reachable:
                continue
            reachable.add(stage_name)
            pending.extend(stages[stage_name]["dependencies"])
        for stage_name in sorted(reachable):
            body = stages[stage_name]["body"]
            _audit_stage_instructions(stage_name, body, target)


def implementation_digests(docker_root: Path = DOCKER_ROOT) -> dict[str, Any]:
    """Hash the recipe/helpers and enforce an install-only runtime stage graph."""
    if not docker_root.is_dir():
        raise ValueError(
            f"Docker implementation directory does not exist: {docker_root}"
        )
    files: dict[str, str] = {}
    for filename in DOCKER_FILES:
        path = docker_root / filename
        if not path.is_file():
            raise ValueError(f"Docker implementation is missing {filename}")
        content = path.read_bytes()
        text = content.decode("utf-8")
        if filename != "Dockerfile" and (
            COMPILE_COMMAND_RE.search(text) or SOURCE_BUILD_COMMAND_RE.search(text)
        ):
            raise ValueError(
                f"compile command is forbidden in install-only file {filename}"
            )
        files[filename] = "sha256:" + hashlib.sha256(content).hexdigest()
    dockerfile = (docker_root / "Dockerfile").read_text(encoding="utf-8")
    _audit_install_only_target(dockerfile)
    if "ARG BASE_IMAGE" not in dockerfile or "FROM ${BASE_IMAGE}" not in dockerfile:
        raise ValueError("Dockerfile must use the authorized BASE_IMAGE argument")
    identity = {
        "files": files,
        "base_authority_sha256": sha256_value(FIXTURE_BASE_AUTHORITY),
        "image_toolchain_authority_sha256": sha256_value(
            fixture_image_toolchain_authority()
        ),
    }
    return {**identity, "aggregate_sha256": sha256_value(identity)}


def _base_blob(
    value: object, label: str, media_types: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    blob = _exact(value, {"media_type", "digest", "size", "raw"}, label)
    if blob["media_type"] not in media_types:
        raise ValueError(f"{label} has unsupported base media type")
    digest = _digest(blob["digest"], f"{label} digest")
    size = blob["size"]
    raw = blob["raw"]
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or not isinstance(raw, str)
    ):
        raise ValueError(f"{label} raw bytes/size are invalid")
    raw_bytes = raw.encode("utf-8")
    if len(raw_bytes) != size:
        raise ValueError(f"{label} base size does not match its raw bytes")
    if "sha256:" + hashlib.sha256(raw_bytes).hexdigest() != digest:
        raise ValueError(f"{label} base digest does not match its raw bytes")
    return blob, _json_bytes(raw_bytes, label)


def _validate_base(
    base_record: object,
    target_platform: str,
    *,
    candidate_kind: str = "fixture-candidate",
) -> dict[str, Any]:
    base = _exact(
        base_record,
        {
            "schema_version",
            "kind",
            "fixture_only",
            "repository",
            "index",
            "manifest",
            "config",
        },
        "base record",
    )
    identities = {
        "fixture-candidate": ("fixture-base-image-record", True),
        "real-candidate": ("ucm-real-base-image-record", False),
    }
    if candidate_kind not in identities:
        raise ValueError("base candidate kind is invalid")
    expected_kind, expected_fixture = identities[candidate_kind]
    if (
        base["schema_version"] != 1
        or base["kind"] != expected_kind
        or base["fixture_only"] is not expected_fixture
    ):
        label = "fixture-only" if expected_fixture else "real"
        raise ValueError(f"base record must retain {label} identity")
    repository = base["repository"]
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise ValueError("base repository must be canonical and contain no mutable tag")
    try:
        target_os, target_architecture = target_platform.split("/", 1)
    except ValueError as error:
        raise ValueError("base target platform must be os/architecture") from error
    if target_os != "linux" or target_architecture not in {"amd64", "arm64"}:
        raise ValueError("base target platform must be linux/amd64 or linux/arm64")

    index_blob, index = _base_blob(base["index"], "base index", OCI_INDEX_MEDIA_TYPES)
    manifest_blob, manifest = _base_blob(
        base["manifest"], "base manifest", OCI_MANIFEST_MEDIA_TYPES
    )
    config_blob, config = _base_blob(
        base["config"], "base config", OCI_CONFIG_MEDIA_TYPES
    )
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != index_blob["media_type"]
        or not isinstance(index.get("manifests"), list)
    ):
        raise ValueError("base index structure/media type is invalid")
    platform_descriptors = []
    for descriptor in index["manifests"]:
        if not isinstance(descriptor, dict):
            raise ValueError("base index manifest descriptor must be an object")
        platform = descriptor.get("platform")
        if isinstance(platform, dict) and (
            platform.get("os"),
            platform.get("architecture"),
        ) == (target_os, target_architecture):
            platform_descriptors.append(descriptor)
    if len(platform_descriptors) != 1:
        raise ValueError("base index must contain one exact target platform manifest")
    platform_descriptor = platform_descriptors[0]
    if (
        platform_descriptor.get("mediaType") != manifest_blob["media_type"]
        or platform_descriptor.get("digest") != manifest_blob["digest"]
        or platform_descriptor.get("size") != manifest_blob["size"]
    ):
        raise ValueError("base index descriptor does not bind the platform manifest")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != manifest_blob["media_type"]
        or not isinstance(manifest.get("config"), dict)
        or not isinstance(manifest.get("layers"), list)
    ):
        raise ValueError("base platform manifest structure/media type is invalid")
    config_descriptor = manifest["config"]
    if (
        config_descriptor.get("mediaType") != config_blob["media_type"]
        or config_descriptor.get("digest") != config_blob["digest"]
        or config_descriptor.get("size") != config_blob["size"]
    ):
        raise ValueError("base manifest descriptor does not bind the platform config")
    if (config.get("os"), config.get("architecture")) != (
        target_os,
        target_architecture,
    ):
        raise ValueError(
            "base config platform does not match requested target platform"
        )
    descriptor_platform = platform_descriptor["platform"]
    if descriptor_platform.get("variant") != config.get("variant"):
        raise ValueError("base index/config platform variants do not match")
    result = copy.deepcopy(base)
    result["platform"] = {
        "os": target_os,
        "architecture": target_architecture,
        "variant": config.get("variant"),
        "manifest_media_type": manifest_blob["media_type"],
        "manifest_digest": manifest_blob["digest"],
        "manifest_size": manifest_blob["size"],
        "config_media_type": config_blob["media_type"],
        "config_digest": config_blob["digest"],
        "config_size": config_blob["size"],
    }
    result["subject"] = f"{repository}@{manifest_blob['digest']}"
    return result


def validate_real_base_authority(
    base_record: object, task_authority: object
) -> dict[str, Any]:
    """Reopen a real base descriptor chain and bind it to the exact Task 1 member."""
    if not isinstance(task_authority, dict):
        raise ValueError("real image task authority must be an object")
    task = real_image_authority(
        task_authority.get("family_id"), task_authority.get("cpu_arch")
    )
    if task_authority != task:
        raise ValueError("real image task authority differs from release config")
    if not isinstance(base_record, dict):
        raise ValueError("real base authority must be an object")
    raw_keys = {
        "schema_version",
        "kind",
        "fixture_only",
        "repository",
        "index",
        "manifest",
        "config",
    }
    if set(base_record) not in (raw_keys, raw_keys | {"platform", "subject"}):
        raise ValueError("real base authority fields are noncanonical")
    if (
        base_record.get("kind") != "ucm-real-base-image-record"
        or base_record.get("fixture_only") is not False
    ):
        raise ValueError("real base authority requires distinct real identity")
    if base_record.get("repository") != task["runtime"]["repository"]:
        raise ValueError("real base repository differs from exact task repository")
    for label in ("index", "manifest", "config"):
        blob = base_record.get(label)
        digest = blob.get("digest") if isinstance(blob, dict) else None
        expected = task["runtime"][f"{label}_digest"]
        if digest != expected:
            raise ValueError(f"real base {label} digest differs from exact task")
    raw_record = {key: copy.deepcopy(base_record[key]) for key in raw_keys}
    base = _validate_base(raw_record, task["platform"], candidate_kind="real-candidate")
    if set(base_record) != raw_keys and base != base_record:
        raise ValueError("real base descriptor projection is noncanonical")
    if base["subject"] != (
        f"{task['runtime']['repository']}@{task['runtime']['manifest_digest']}"
    ):
        raise ValueError("real base subject differs from exact task platform member")
    return base


def real_base_record_from_files(
    family_id: str,
    architecture: str,
    *,
    index_path: Path,
    manifest_path: Path,
    config_path: Path,
) -> dict[str, Any]:
    """Build and reopen a real base record from three raw Registry blobs."""
    task = real_image_authority(family_id, architecture)
    paths = {
        "index": Path(index_path),
        "manifest": Path(manifest_path),
        "config": Path(config_path),
    }
    parsed: dict[str, dict[str, Any]] = {}
    raw_blobs: dict[str, bytes] = {}
    for label, path in paths.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"real base {label} blob is missing")
        raw_blobs[label] = path.read_bytes()
        parsed[label] = _json_bytes(raw_blobs[label], f"real base {label}")
    manifest_config = parsed["manifest"].get("config")
    if not isinstance(manifest_config, dict):
        raise ValueError("real base manifest config descriptor is missing")
    media_type_values = {
        "index": parsed["index"].get("mediaType"),
        "manifest": parsed["manifest"].get("mediaType"),
        "config": manifest_config.get("mediaType"),
    }
    media_types = {
        "index": OCI_INDEX_MEDIA_TYPES,
        "manifest": OCI_MANIFEST_MEDIA_TYPES,
        "config": OCI_CONFIG_MEDIA_TYPES,
    }
    blobs: dict[str, Any] = {}
    for label, raw in raw_blobs.items():
        media_type = media_type_values[label]
        if media_type not in media_types[label]:
            raise ValueError(f"real base {label} media type is invalid")
        blobs[label] = {
            "media_type": media_type,
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "raw": raw.decode("utf-8"),
        }
    record = {
        "schema_version": 1,
        "kind": "ucm-real-base-image-record",
        "fixture_only": False,
        "repository": task["runtime"]["repository"],
        **blobs,
    }
    return validate_real_base_authority(record, task)


def require_fixture_base_authority(
    base_record: object, target_platform: str
) -> dict[str, Any]:
    raw_keys = {
        "schema_version",
        "kind",
        "fixture_only",
        "repository",
        "index",
        "manifest",
        "config",
    }
    if not isinstance(base_record, dict):
        raise ValueError("authoritative base record must be an object")
    if set(base_record) == raw_keys:
        base = _validate_base(base_record, target_platform)
    elif set(base_record) == raw_keys | {"platform", "subject"}:
        base = _validate_base(
            {key: copy.deepcopy(base_record[key]) for key in raw_keys},
            target_platform,
        )
        if base != base_record:
            raise ValueError("validated base projection is noncanonical")
    else:
        raise ValueError("authoritative base record fields are invalid")
    authority = fixture_base_authority()
    if (
        target_platform != authority["target_platform"]
        or base["repository"] != authority["repository"]
        or base["index"]["digest"] != authority["index_digest"]
        or base["manifest"]["digest"] != authority["manifest_digest"]
        or base["config"]["digest"] != authority["config_digest"]
    ):
        raise ValueError("base descriptor chain differs from fixture base authority")
    return base


def _single_embedded_json(
    archive: zipfile.ZipFile, suffix: str, label: str
) -> dict[str, Any]:
    names = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(names) != 1:
        raise ValueError(f"builder-candidate requires one embedded {label}")
    raw = archive.read(names[0])
    value = _json_bytes(raw, label)
    if raw != canonical_bytes(value) + b"\n":
        raise ValueError(f"embedded {label} is noncanonical")
    return value


def inspect_real_wheel_candidate(
    family_id: str,
    architecture: str,
    wheel_path: Path,
    inspection: object,
    *,
    release_path: Path = release_core.DEFAULT_RELEASE,
    compatibility_path: Path = release_core.DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Reinspect sealed bytes as builder-candidate and reverse them to one task."""
    task = real_image_authority(
        family_id,
        architecture,
        release_path=release_path,
        compatibility_path=compatibility_path,
        schema_dir=schema_dir,
        docker_root=docker_root,
    )
    path = Path(wheel_path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("real builder-candidate wheel must be one regular file")
    raw_sha256 = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        reopened = wheel_artifact.inspect_wheel(
            path,
            task["spec_id"],
            raw_sha256,
            "builder-candidate",
            release_path=release_path,
            compatibility_path=compatibility_path,
            schema_dir=schema_dir,
        )
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as error:
        raise ValueError(
            f"real image requires raw builder-candidate wheel bytes: {error}"
        ) from error
    if not isinstance(inspection, dict) or inspection != reopened:
        raise ValueError(
            "supplied builder-candidate inspection differs from raw wheel reinspection"
        )
    evidence = reopened.get("builder_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("builder-candidate inspection lacks native build evidence")
    if (
        reopened.get("source_kind") != "builder-candidate"
        or reopened.get("status") != "candidate-inspected"
        or reopened.get("trust_level") != "unpublished-builder-candidate"
        or reopened.get("published") is not False
        or reopened.get("publication_eligible") is not False
        or evidence.get("build_key") != task["task_sha256"]
    ):
        raise ValueError("builder-candidate inspection cannot authorize a real task")
    with zipfile.ZipFile(path) as archive:
        build = _single_embedded_json(
            archive, ".dist-info/ucm-build.json", "wheel build binding"
        )
        authority = _single_embedded_json(
            archive,
            ".dist-info/ucm-build-authority.json",
            "wheel build authority",
        )
        closure = _single_embedded_json(
            archive,
            ".dist-info/ucm-dependency-closure.json",
            "wheel dependency closure",
        )
    bindings = {
        "source_sha": evidence.get("source_commit"),
        "source_tree": authority.get("source_tree"),
        "source_archive_sha256": authority.get("source_archive_sha256"),
        "build_context_sha256": evidence.get("build_context_digest"),
        "build_key": evidence.get("build_key"),
        "source_date_epoch": evidence.get("source_date_epoch"),
        "builder_coordinate": authority.get("builder_coordinate"),
        "builder_config_digest": authority.get("builder_config_digest"),
        "dependency_lock_sha256": authority.get("dependency_lock_sha256"),
        "tool_wheels": authority.get("tool_wheels"),
        "native_members": evidence.get("native_members"),
        "elf_machines": evidence.get("elf_machines"),
        "dt_needed": evidence.get("dt_needed"),
        "dependency_closure": closure.get("native_members"),
        "unresolved_dependencies": evidence.get("unresolved_dependencies"),
    }
    if (
        build.get("source_sha") != bindings["source_sha"]
        or build.get("source_tree") != bindings["source_tree"]
        or build.get("build_context_sha256") != bindings["build_context_sha256"]
        or build.get("build_key") != task["task_sha256"]
        or authority.get("task_sha256") != task["task_sha256"]
        or authority.get("spec_id") != task["spec_id"]
        or authority.get("builder_config_digest")
        != task.get("builder", {}).get("root", {}).get("config_digest")
        and "builder" in task
    ):
        raise ValueError("wheel source/build-key authority does not match exact task")
    if (
        not isinstance(bindings["source_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", bindings["source_sha"]) is None
        or not isinstance(bindings["source_tree"], str)
        or re.fullmatch(r"[0-9a-f]{40}", bindings["source_tree"]) is None
        or _digest(bindings["build_context_sha256"], "wheel build context")
        != bindings["build_context_sha256"]
        or bindings["unresolved_dependencies"] != []
    ):
        raise ValueError("wheel source/native/dependency authority is incomplete")
    return {
        "schema_version": 1,
        "kind": "ucm-real-wheel-authority",
        "task": copy.deepcopy(task),
        "inspection": copy.deepcopy(reopened),
        "embedded_build": build,
        "embedded_authority": authority,
        "embedded_closure": closure,
        "bindings": bindings,
        "wheel_sha256": raw_sha256,
        "wheel_size": path.stat().st_size,
    }


def audit_real_context(context_dir: Path, expected_files: set[str]) -> None:
    """Require a source-free, symlink-free, recursively flat exact file set."""
    context = Path(context_dir)
    if not context.is_dir() or context.is_symlink():
        raise ValueError("real context must be a regular directory")
    actual: set[str] = set()
    for root, directories, files in os.walk(context, followlinks=False):
        root_path = Path(root)
        if root_path != context:
            raise ValueError("real context must be flat and contain no directories")
        if directories:
            raise ValueError("real context must be flat and contain no directories")
        for filename in files:
            path = root_path / filename
            if path.is_symlink():
                raise ValueError(f"real context rejects symlink: {filename}")
            if not path.is_file():
                raise ValueError(
                    f"real context entry is not a regular file: {filename}"
                )
            actual.add(filename)
    unexpected = sorted(actual - set(expected_files))
    missing = sorted(set(expected_files) - actual)
    if unexpected or missing:
        source_markers = {
            "setup.py",
            "pyproject.toml",
            "CMakeLists.txt",
            "ucm-source.tar",
            ".git",
        }
        if source_markers & set(unexpected):
            raise ValueError(
                f"real context source files violate allowlist: {unexpected}"
            )
        if any(name.endswith(".whl") for name in unexpected):
            raise ValueError(f"real context wheel set violates allowlist: {unexpected}")
        if any(
            name in {"compiler", "cmake", "ninja", "make", "gcc", "g++"}
            for name in unexpected
        ):
            raise ValueError(
                f"real context build tool violates allowlist: {unexpected}"
            )
        raise ValueError(
            f"real context exact allowlist mismatch: missing={missing}, extra={unexpected}"
        )


def build_real_dependency_lock(
    task_authority: dict[str, Any],
    wheel_path: Path,
    wrapt_path: Path,
    *,
    wheel_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate the two-artifact offline direct-reference hash lock."""
    task = real_image_authority(
        task_authority.get("family_id"), task_authority.get("cpu_arch")
    )
    if task_authority != task:
        raise ValueError("real dependency task authority is noncanonical")
    wrapt = Path(wrapt_path)
    expected_wrapt = task["wrapt_wheel"]
    if (
        not wrapt.is_file()
        or wrapt.is_symlink()
        or wrapt.name != expected_wrapt["filename"]
    ):
        raise ValueError("exact architecture-specific wrapt wheel is missing")
    wrapt_sha256 = "sha256:" + hashlib.sha256(wrapt.read_bytes()).hexdigest()
    if wrapt_sha256 != expected_wrapt["sha256"]:
        raise ValueError("wrapt wheel SHA256 differs from release authority")
    wheel = Path(wheel_path)
    if not wheel.is_file() or wheel.is_symlink() or wheel.suffix != ".whl":
        raise ValueError("exact UCM wheel is missing")
    wheel_sha256 = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    if wheel_record is not None and (
        wheel_record.get("filename") != wheel.name
        or wheel_record.get("sha256") != wheel_sha256
    ):
        raise ValueError("UCM wheel bytes differ from builder-candidate inspection")
    requirements = (
        f"uc-manager @ file:///wheelhouse/{wheel.name} "
        f"--hash={wheel_sha256}\n"
        f"wrapt @ file:///wheelhouse/{wrapt.name} "
        f"--hash={wrapt_sha256}\n"
    )
    return {
        "schema_version": 1,
        "kind": "ucm-real-runtime-dependency-lock",
        "requirements": requirements,
        "sha256": "sha256:" + hashlib.sha256(requirements.encode()).hexdigest(),
        "wheel": {"filename": wheel.name, "sha256": wheel_sha256},
        "wrapt": {"filename": wrapt.name, "sha256": wrapt_sha256},
        "preinstall_command": [
            "python",
            "-m",
            "pip",
            "uninstall",
            "--yes",
            "uc-manager",
            "wrapt",
        ],
        "pip_command": [
            "python",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links=/wheelhouse",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "-r",
            "/wheelhouse/requirements.lock",
        ],
    }


def verify_real_runtime_evidence(recipe: object, evidence: object) -> dict[str, str]:
    """Verify offline install plus installed native/ELF/closure evidence."""
    if not isinstance(recipe, dict) or set(recipe) != {"payload", "payload_sha256"}:
        raise ValueError("real runtime recipe envelope is invalid")
    payload = recipe["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("candidate_kind") != "real-candidate"
        or recipe["payload_sha256"] != sha256_value(payload)
    ):
        raise ValueError("real runtime recipe digest is invalid")
    if not isinstance(evidence, dict):
        raise ValueError("real runtime evidence must be an object")
    install = evidence.get("install")
    runtime = evidence.get("runtime")
    if (
        not isinstance(install, dict)
        or install.get("kind") not in {None, "ucm-real-install-result"}
        or install.get("status") != "passed"
    ):
        raise ValueError("real install gate did not pass")
    if install.get("pip_check") != "passed":
        raise ValueError("real pip check gate did not pass")
    wheel = payload.get("wheel")
    wrapt = payload.get("wrapt_wheel")
    if not isinstance(wheel, dict) or not isinstance(wrapt, dict):
        raise ValueError("real recipe wheel authority is missing")
    if install.get("kind") == "ucm-real-install-result" and (
        install.get("wheel_filename") != wheel.get("filename")
        or install.get("wheel_sha256") != wheel.get("sha256")
        or install.get("wrapt_filename") != wrapt.get("filename")
        or install.get("wrapt_sha256") != wrapt.get("sha256")
        or install.get("version") != wheel.get("version")
    ):
        raise ValueError("real install wheel identity differs from recipe")
    preinstall_command = install.get("preinstall_command")
    expected_preinstall_command = payload.get("dependency_lock", {}).get(
        "preinstall_command"
    )
    if (
        not isinstance(preinstall_command, list)
        or not isinstance(expected_preinstall_command, list)
        or preinstall_command[1:] != expected_preinstall_command[1:]
        or not str(preinstall_command[0]).endswith(("python", "python3", "python3.12"))
    ):
        raise ValueError("real preinstall purge is not the exact reviewed command")
    command = install.get("pip_command")
    expected_command = payload.get("dependency_lock", {}).get("pip_command")
    if command is not None and (
        not isinstance(command, list)
        or not isinstance(expected_command, list)
        or command[1:] != expected_command[1:]
        or not str(command[0]).endswith(("python", "python3", "python3.12"))
    ):
        raise ValueError("real pip command is not the exact offline hashed install")
    if install.get("installed_packages") != {
        "uc-manager": wheel.get("version"),
        "wrapt": "1.17.2",
    }:
        raise ValueError("real installed package versions do not match")
    if install.get("imports") != {"ucm": "passed", "wrapt": "passed"}:
        raise ValueError("real import gate did not pass")
    direct_urls = install.get("direct_urls")
    if not isinstance(direct_urls, dict):
        raise ValueError("real direct_url evidence is missing")
    expected_direct = {
        "uc-manager": (wheel.get("filename"), wheel.get("sha256")),
        "wrapt": (wrapt.get("filename"), wrapt.get("sha256")),
    }
    for distribution, (filename, digest) in expected_direct.items():
        direct = direct_urls.get(distribution)
        if (
            not isinstance(direct, dict)
            or direct.get("url") != f"file:///wheelhouse/{filename}"
            or direct.get("archive_info", {}).get("hash")
            != "sha256=" + str(digest).removeprefix("sha256:")
        ):
            raise ValueError(
                f"real {distribution} direct_url does not bind wheel bytes"
            )
    if not isinstance(runtime, dict) or runtime.get("kind") not in {
        None,
        "ucm-real-runtime-inspection",
    }:
        raise ValueError("real runtime inspection is missing")
    abi = runtime.get("abi")
    expected_abi = wheel.get("python_abi")
    if abi != {
        "expected_python_abi": expected_abi,
        "observed_python_abi": expected_abi,
        "status": "passed",
    }:
        raise ValueError("real runtime ABI gate did not pass")
    expected_native = wheel.get("builder_evidence")
    if not isinstance(expected_native, dict):
        raise ValueError("real recipe lacks Task 2 native evidence")
    if runtime.get("native_members") != expected_native.get("native_members"):
        raise ValueError("installed native member paths differ from Task 2 inspection")
    if runtime.get("elf_machines") != expected_native.get("elf_machines"):
        raise ValueError("installed ELF machine differs from Task 2 inspection")
    if runtime.get("dt_needed") != expected_native.get("dt_needed"):
        raise ValueError("installed ELF DT_NEEDED differs from Task 2 inspection")
    if runtime.get("dependency_closure") != expected_native.get("dependency_closure"):
        raise ValueError("installed dependency closure differs from Task 2 inspection")
    for label in ("accelerator_runtime", "device"):
        state = runtime.get(label)
        if not isinstance(state, dict) or state.get("status") != "external-required":
            raise ValueError(f"real {label} must remain external-required")
    if (
        runtime.get("hardware_passed") is not False
        or runtime.get("status") != "external-required"
        or runtime.get("package_version") != wheel.get("version")
    ):
        raise ValueError("real runtime/device evidence is noncanonical")
    return {
        "install": "passed",
        "pip_check": "passed",
        "direct_url": "passed",
        "ucm_import": "passed",
        "wrapt_import": "passed",
        "abi": "passed",
        "native_members": "passed",
        "elf": "passed",
        "dependency_closure": "passed",
    }


def _epoch_timestamp(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 315532800:
        raise ValueError("real source epoch is invalid")
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def real_content_identity(recipe: object, closure: object) -> dict[str, Any]:
    """Derive immutable member identity while excluding run/signature envelopes."""
    if not isinstance(recipe, dict) or set(recipe) != {"payload", "payload_sha256"}:
        raise ValueError("real content recipe envelope is invalid")
    payload = recipe["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("candidate_kind") != "real-candidate"
        or recipe["payload_sha256"] != sha256_value(payload)
    ):
        raise ValueError("real content recipe digest is invalid")
    if not isinstance(closure, dict):
        raise ValueError("real OCI closure must be an object")
    source = payload.get("source")
    wheel = payload.get("wheel")
    base = payload.get("base")
    base_config_blob = base.get("config") if isinstance(base, dict) else None
    base_config_raw = (
        base_config_blob.get("raw") if isinstance(base_config_blob, dict) else None
    )
    if (
        not isinstance(source, dict)
        or not isinstance(wheel, dict)
        or not isinstance(base_config_raw, str)
    ):
        raise ValueError("real content recipe source/wheel/base authority is missing")
    base_config = _json_bytes(base_config_raw.encode(), "real recipe base config")
    config_value = base_config.get("config", {})
    base_labels = (
        config_value.get("Labels", {}) if isinstance(config_value, dict) else {}
    )
    base_history = base_config.get("history", [])
    if not isinstance(base_labels, dict) or not isinstance(base_history, list):
        raise ValueError("real recipe base labels/history are invalid")
    expected_labels = copy.deepcopy(base_labels)
    expected_labels.update(
        {
            "org.opencontainers.image.revision": source.get("commit"),
            "io.ucm.release.source-tree": source.get("tree"),
            "io.ucm.release.source-context-sha256": source.get("context_sha256"),
            "io.ucm.release.task-sha256": payload.get("task_sha256"),
            "io.ucm.release.build-key-sha256": payload.get("build_key_sha256"),
            "io.ucm.release.wheel-sha256": wheel.get("sha256"),
            "io.ucm.release.recipe-sha256": recipe.get("payload_sha256"),
        }
    )
    labels = closure.get("labels")
    if labels != expected_labels:
        raise ValueError("real OCI config labels do not bind recipe authority")
    expected_annotations = {
        "io.ucm.release.recipe-sha256": recipe.get("payload_sha256"),
        "io.ucm.release.task-sha256": payload.get("task_sha256"),
    }
    if closure.get("annotations") != expected_annotations:
        raise ValueError("real OCI manifest annotations do not bind recipe authority")
    created = _epoch_timestamp(payload.get("source_date_epoch"))
    history = closure.get("history")
    if (
        closure.get("created") != created
        or not isinstance(history, list)
        or len(history) <= len(base_history)
        or history[: len(base_history)] != base_history
        or any(
            not isinstance(item, dict)
            or item.get("created") != created
            or not isinstance(item.get("created_by"), str)
            or not item["created_by"]
            for item in history[len(base_history) :]
        )
    ):
        raise ValueError("real OCI created/history is not source-epoch deterministic")
    layers = closure.get("layers")
    diff_ids = closure.get("diff_ids")
    if (
        not isinstance(layers, list)
        or not layers
        or not isinstance(diff_ids, list)
        or len(layers) != len(diff_ids)
    ):
        raise ValueError("real OCI layer/diff-id closure is invalid")
    for position, (layer, diff_id) in enumerate(zip(layers, diff_ids, strict=True)):
        if not isinstance(layer, dict):
            raise ValueError(f"real OCI layer {position} is invalid")
        _digest(layer.get("digest"), f"real OCI layer {position}")
        if not isinstance(layer.get("size"), int) or layer["size"] < 1:
            raise ValueError(f"real OCI layer {position} size is invalid")
        _digest(diff_id, f"real OCI diff-id {position}")
    stable = {
        "manifest_digest": _digest(
            closure.get("manifest_digest"), "real OCI manifest digest"
        ),
        "config_digest": _digest(
            closure.get("config_digest"), "real OCI config digest"
        ),
        "layers": copy.deepcopy(layers),
        "diff_ids": copy.deepcopy(diff_ids),
        "annotations": copy.deepcopy(expected_annotations),
        "labels": copy.deepcopy(expected_labels),
        "created": created,
        "history": copy.deepcopy(history),
        "source": copy.deepcopy(source),
        "task_sha256": payload.get("task_sha256"),
        "build_key_sha256": payload.get("build_key_sha256"),
        "wheel_sha256": wheel.get("sha256"),
        "recipe_sha256": recipe.get("payload_sha256"),
    }
    return {**stable, "content_identity_sha256": sha256_value(stable)}


def prepare_real_context(
    *,
    family_id: str,
    architecture: str,
    wheel_path: Path,
    wheel_inspection: dict[str, Any],
    base_record: dict[str, Any],
    wrapt_path: Path,
    output_dir: Path,
    release_path: Path = release_core.DEFAULT_RELEASE,
    compatibility_path: Path = release_core.DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Prepare one exact source-free real install-only member context."""
    task = real_image_authority(
        family_id,
        architecture,
        release_path=release_path,
        compatibility_path=compatibility_path,
        schema_dir=schema_dir,
        docker_root=docker_root,
    )
    wheel_authority = inspect_real_wheel_candidate(
        family_id,
        architecture,
        Path(wheel_path),
        wheel_inspection,
        release_path=release_path,
        compatibility_path=compatibility_path,
        schema_dir=schema_dir,
        docker_root=docker_root,
    )
    base = validate_real_base_authority(base_record, task)
    dependency_lock = build_real_dependency_lock(
        task,
        Path(wheel_path),
        Path(wrapt_path),
        wheel_record=wheel_authority["inspection"],
    )
    bindings = wheel_authority["bindings"]
    implementation = implementation_digests(docker_root)
    source = {
        "commit": bindings["source_sha"],
        "tree": bindings["source_tree"],
        "archive_sha256": bindings["source_archive_sha256"],
        "context_sha256": bindings["build_context_sha256"],
    }
    wheel = {
        "filename": wheel_authority["inspection"]["filename"],
        "sha256": wheel_authority["wheel_sha256"],
        "size": wheel_authority["wheel_size"],
        "spec_id": task["spec_id"],
        "version": task["wheel_version"],
        "python_abi": task["python_abi"],
        "cpu_arch": task["cpu_arch"],
        "builder_evidence": {
            "source_commit": bindings["source_sha"],
            "source_tree": bindings["source_tree"],
            "build_context_digest": bindings["build_context_sha256"],
            "build_key": bindings["build_key"],
            "builder_coordinate": bindings["builder_coordinate"],
            "builder_config_digest": bindings["builder_config_digest"],
            "dependency_lock_sha256": bindings["dependency_lock_sha256"],
            "tool_wheels": copy.deepcopy(bindings["tool_wheels"]),
            "native_members": copy.deepcopy(bindings["native_members"]),
            "elf_machines": copy.deepcopy(bindings["elf_machines"]),
            "dt_needed": copy.deepcopy(bindings["dt_needed"]),
            "dependency_closure": copy.deepcopy(bindings["dependency_closure"]),
            "unresolved_dependencies": copy.deepcopy(
                bindings["unresolved_dependencies"]
            ),
        },
    }
    identity_inputs = {
        "schema_version": 1,
        "kind": "ucm-real-image-build-key-input",
        "source": source,
        "source_date_epoch": bindings["source_date_epoch"],
        "task_sha256": task["task_sha256"],
        "task_authority_sha256": task["authority_sha256"],
        "wheel_sha256": wheel["sha256"],
        "wheel_inspection_sha256": sha256_value(wheel_authority["inspection"]),
        "wheel_build_key": bindings["build_key"],
        "profile_id": task["profile_id"],
        "cpu_arch": task["cpu_arch"],
        "base": {
            "index_digest": base["index"]["digest"],
            "manifest_digest": base["manifest"]["digest"],
            "config_digest": base["config"]["digest"],
        },
        "implementation_sha256": implementation["aggregate_sha256"],
        "dependency_lock_sha256": dependency_lock["sha256"],
        "task_dependency_lock_sha256": task["dependency_lock_sha256"],
        "toolchain_sha256": task["toolchain"]["authority_sha256"],
        "deterministic_flags": copy.deepcopy(task["toolchain"]["deterministic_flags"]),
    }
    build_key = sha256_value(identity_inputs)
    authority_payload = {
        "schema_version": 1,
        "kind": "ucm-real-image-source-authority",
        "candidate_kind": "real-candidate",
        "fixture_only": False,
        "unpublished": True,
        "publication_attempted": False,
        "task": copy.deepcopy(task),
        "wheel_inspection": copy.deepcopy(wheel_authority["inspection"]),
        "wheel_embedded_authority": copy.deepcopy(
            wheel_authority["embedded_authority"]
        ),
        "wheel_embedded_build": copy.deepcopy(wheel_authority["embedded_build"]),
        "wheel_embedded_closure": copy.deepcopy(wheel_authority["embedded_closure"]),
        "base": copy.deepcopy(base),
        "dependency_lock": copy.deepcopy(dependency_lock),
        "build_key_inputs": identity_inputs,
        "build_key_sha256": build_key,
    }
    authority = {
        **authority_payload,
        "authority_sha256": sha256_value(authority_payload),
    }
    context_files = sorted(
        [
            *DOCKER_FILES,
            REAL_IMAGE_CONTEXT_RECORD,
            CONTEXT_RECIPE,
            REAL_REQUIREMENTS_LOCK,
            wheel["filename"],
            task["wrapt_wheel"]["filename"],
        ]
    )
    payload = {
        "schema_version": 1,
        "kind": "ucm-install-image-recipe",
        "candidate_kind": "real-candidate",
        "fixture_only": False,
        "unpublished": True,
        "publication_attempted": False,
        "source_date_epoch": bindings["source_date_epoch"],
        "family_id": family_id,
        "profile_id": task["profile_id"],
        "spec_id": task["spec_id"],
        "target_platform": task["platform"],
        "target_repository": task["target_repository"],
        "target_tag": task["target_tag"],
        "task_sha256": task["task_sha256"],
        "build_key_sha256": build_key,
        "authority_sha256": authority["authority_sha256"],
        "source": source,
        "base": copy.deepcopy(base),
        "wheel": wheel,
        "wrapt_wheel": copy.deepcopy(task["wrapt_wheel"]),
        "dependency_lock": {
            "filename": REAL_REQUIREMENTS_LOCK,
            "sha256": dependency_lock["sha256"],
            "preinstall_command": copy.deepcopy(dependency_lock["preinstall_command"]),
            "pip_command": copy.deepcopy(dependency_lock["pip_command"]),
        },
        "implementation": implementation,
        "toolchain": copy.deepcopy(task["toolchain"]),
        "build": {
            "target": REAL_INSTALL_TARGET,
            "base_image": base["subject"],
            "output": "local-oci-or-canonical-member",
            "build_args": {
                "BASE_IMAGE": base["subject"],
                "SOURCE_DATE_EPOCH": str(bindings["source_date_epoch"]),
                "TARGETPLATFORM": task["platform"],
                "UCM_WHEEL": wheel["filename"],
                "WRAPT_WHEEL": task["wrapt_wheel"]["filename"],
                "UCM_SOURCE_SHA": source["commit"],
                "UCM_SOURCE_TREE": source["tree"],
                "UCM_SOURCE_CONTEXT_SHA256": source["context_sha256"],
                "UCM_TASK_SHA256": task["task_sha256"],
                "UCM_BUILD_KEY_SHA256": build_key,
                "UCM_WHEEL_SHA256": wheel["sha256"],
            },
        },
        "context_files": context_files,
    }
    recipe = {"payload": payload, "payload_sha256": sha256_value(payload)}

    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"real build context must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for filename in DOCKER_FILES:
        shutil.copyfile(Path(docker_root) / filename, output / filename)
    shutil.copyfile(Path(wheel_path), output / wheel["filename"])
    shutil.copyfile(Path(wrapt_path), output / task["wrapt_wheel"]["filename"])
    _write_json(output / REAL_IMAGE_CONTEXT_RECORD, authority)
    _write_json(output / CONTEXT_RECIPE, recipe)
    (output / REAL_REQUIREMENTS_LOCK).write_text(
        dependency_lock["requirements"], encoding="utf-8"
    )
    audit_real_context(output, set(context_files))
    return recipe


def _derive_recipe(
    *,
    source_case: dict[str, Any],
    candidate: dict[str, Any],
    task: dict[str, Any],
    inventory: dict[str, Any],
    base_record: dict[str, Any],
    target_platform: str,
    wheel_path: Path,
    docker_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_keys = {
        "release_manifest",
        "wheel_records",
        "spec_id",
        "upstream_snapshot",
        "compatibility",
        "compatibility_rule_id",
        "implementation_digest",
    }
    _exact(source_case, source_keys, "Task 3 source case")
    implementations = implementation_digests(docker_root)
    if source_case["implementation_digest"] != implementations["aggregate_sha256"]:
        raise ValueError(
            "Task 3 implementation digest does not match Docker implementation"
        )
    try:
        recomputed_candidate = registry.build_candidate(
            **source_case, fixture_mode=True
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"cannot recompute the full Task 3 fixture candidate: {error}"
        ) from error
    if candidate != recomputed_candidate:
        raise ValueError("caller candidate does not match recomputed Task 3 candidate")
    reconcile_result = registry.reconcile(recomputed_candidate, inventory)
    if reconcile_result["task_count"] != 1:
        raise ValueError(
            "Task 3 fixture inventory must schedule exactly one candidate task"
        )
    recomputed_task = reconcile_result["tasks"][0]
    if task != recomputed_task:
        raise ValueError("caller task does not match recomputed Task 3 candidate task")
    if (
        task.get("action") != "build-unpublished-candidate"
        or task.get("publication_attempted") is not False
    ):
        raise ValueError("Task 3 task is not an unpublished build candidate")

    records = source_case["wheel_records"]
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("image context requires the exact single Task 2 wheel record")
    wheel_record = records[0]
    if (
        not isinstance(wheel_record, dict)
        or wheel_record.get("filename") != wheel_path.name
    ):
        raise ValueError("wheel path does not match the Task 2 inspection record")
    actual_wheel_sha256 = (
        "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    )
    if actual_wheel_sha256 != wheel_record.get("sha256"):
        raise ValueError("wheel bytes do not match the Task 2 inspection record")
    actual_inspection = wheel_artifact.inspect_wheel(
        wheel_path,
        source_case["spec_id"],
        actual_wheel_sha256,
        "fixture",
    )
    if actual_inspection != wheel_record:
        raise ValueError("actual wheel inspection differs from Task 2 record")
    fixture_binding = wheel_record.get("fixture_binding", {})
    with tempfile.TemporaryDirectory() as temporary:
        expected_fixture = wheel_artifact.build_fixture_wheel(
            Path(temporary) / "wheel",
            fixture_binding.get("source_commit"),
            source_case["spec_id"],
        )
        if (
            wheel_path.read_bytes() != Path(expected_fixture["wheel_path"]).read_bytes()
            or wheel_record != expected_fixture["inspection"]
        ):
            raise ValueError("fixture wheel differs from authoritative rebuild")
    wheel_input = recomputed_candidate["build_inputs"]["wheel"]
    if (
        wheel_record.get("source_kind") != "fixture"
        or wheel_record.get("status") != "fixture-only"
        or wheel_record.get("trust_level") != "fixture-only"
        or wheel_record.get("published") is not False
        or wheel_record.get("publication_eligible") is not False
    ):
        raise ValueError("wheel record must remain fixture-only and unpublished")
    if wheel_input["sha256"] != actual_wheel_sha256:
        raise ValueError("candidate wheel SHA does not match the actual wheel bytes")

    base = _validate_base(base_record, target_platform)
    target_architecture = target_platform.split("/", 1)[1]
    if wheel_input["cpu_arch"] != target_architecture:
        raise ValueError("wheel CPU architecture does not match target platform")
    upstream = recomputed_candidate["build_inputs"]["upstream"]
    upstream_platforms = [
        item
        for item in upstream["platforms"]
        if item["architecture"] == target_architecture
    ]
    if len(upstream_platforms) != 1:
        raise ValueError("candidate must contain one exact upstream target platform")
    upstream_platform = upstream_platforms[0]
    manifest = source_case["release_manifest"]
    if (
        sha256_value(manifest)
        != recomputed_candidate["build_inputs"]["release_manifest_sha256"]
    ):
        raise ValueError("release manifest digest does not match candidate")

    metadata = {
        "schema_version": 1,
        "kind": "ucm-image-source-metadata",
        "source_case": copy.deepcopy(source_case),
        "candidate": copy.deepcopy(candidate),
        "task": copy.deepcopy(task),
        "inventory": copy.deepcopy(inventory),
        "base_record": copy.deepcopy(base_record),
        "target_platform": target_platform,
        "wheel_record": copy.deepcopy(wheel_record),
    }
    context_files = sorted(
        [*DOCKER_FILES, wheel_path.name, CONTEXT_RECIPE, CONTEXT_METADATA]
    )
    payload = {
        "schema_version": 1,
        "kind": "ucm-install-image-recipe",
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": False,
        "source_date_epoch": 0,
        "target_platform": target_platform,
        "build_key_sha256": recomputed_candidate["build_key_sha256"],
        "task_sha256": sha256_value(recomputed_task),
        "metadata_sha256": sha256_value(metadata),
        "source": {
            "release_manifest_sha256": recomputed_candidate["build_inputs"][
                "release_manifest_sha256"
            ],
            "config_sha256": manifest["config_sha256"],
            "compatibility_sha256": manifest["compatibility_sha256"],
            "compatibility_rule_id": recomputed_candidate["build_inputs"][
                "compatibility_rule_id"
            ],
            "compatibility_rule_sha256": recomputed_candidate["build_inputs"][
                "compatibility_rule_sha256"
            ],
            "upstream_repository": upstream["repository"],
            "upstream_index_digest": upstream["index_digest"],
            "upstream_platform_manifest_digest": upstream_platform["manifest_digest"],
            "upstream_platform_config_digest": upstream_platform["config_digest"],
        },
        "base": base,
        "wheel": {
            "filename": wheel_record["filename"],
            "sha256": actual_wheel_sha256,
            "size": wheel_record["size"],
            "spec_id": wheel_input["spec_id"],
            "declaration_sha256": wheel_input["declaration_sha256"],
            "version": wheel_input["version"],
            "python_abi": wheel_input["python_abi"],
            "cpu_arch": wheel_input["cpu_arch"],
            "accelerator": wheel_input["accelerator"],
            "accelerator_runtime": wheel_input["accelerator_runtime"],
            "npu_arch_or_na": wheel_input["npu_arch_or_na"],
            "os": wheel_input["os"],
            "binary_profile_id": wheel_input["binary_profile_id"],
            "requires_dist": ["wrapt==1.17.2"],
        },
        "implementation": implementations,
        "context_files": context_files,
    }
    return metadata, {"payload": payload, "payload_sha256": sha256_value(payload)}


def prepare_context(
    *,
    source_case: dict[str, Any],
    candidate: dict[str, Any],
    task: dict[str, Any],
    inventory: dict[str, Any],
    base_record: dict[str, Any],
    target_platform: str,
    wheel_path: Path,
    output_dir: Path,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Create the exact seven-file Buildx context after recomputing source closure."""
    wheel_path = Path(wheel_path)
    output_dir = Path(output_dir)
    metadata, recipe = _derive_recipe(
        source_case=source_case,
        candidate=candidate,
        task=task,
        inventory=inventory,
        base_record=base_record,
        target_platform=target_platform,
        wheel_path=wheel_path,
        docker_root=docker_root,
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"build context must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in DOCKER_FILES:
        shutil.copyfile(docker_root / filename, output_dir / filename)
    shutil.copyfile(wheel_path, output_dir / wheel_path.name)
    _write_json(output_dir / CONTEXT_METADATA, metadata)
    _write_json(output_dir / CONTEXT_RECIPE, recipe)
    return recipe


def prepare_context_bundle(
    image_input: dict[str, Any],
    *,
    wheel_dir: Path,
    expected_source_sha: str,
    base_authority: dict[str, Any],
    base_index_path: Path,
    base_manifest_path: Path,
    base_config_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Reopen fixed Registry blobs and prepare the exact Task 4 context."""
    required = {
        "source_case",
        "candidate",
        "task",
        "inventory",
        "target_platform",
    }
    _exact(image_input, required, "image workflow input")
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_sha) is None:
        raise ValueError("expected source SHA must be a full lowercase Git commit")
    source_case = image_input.get("source_case")
    records = (
        source_case.get("wheel_records") if isinstance(source_case, dict) else None
    )
    binding = (
        records[0].get("fixture_binding")
        if isinstance(records, list)
        and len(records) == 1
        and isinstance(records[0], dict)
        else None
    )
    if (
        not isinstance(binding, dict)
        or binding.get("source_commit") != expected_source_sha
    ):
        raise ValueError(
            "image wheel fixture source does not match expected source SHA"
        )
    authority = _validate_base_authority(base_authority)
    if image_input["target_platform"] != authority["target_platform"]:
        raise ValueError("image input platform differs from fixture base authority")
    paths = {
        "index": Path(base_index_path),
        "manifest": Path(base_manifest_path),
        "config": Path(base_config_path),
    }
    expected = {
        "index": authority["index_digest"],
        "manifest": authority["manifest_digest"],
        "config": authority["config_digest"],
    }
    raw: dict[str, bytes] = {}
    parsed: dict[str, dict[str, Any]] = {}
    for label, path in paths.items():
        content = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != expected[label]:
            raise ValueError(f"base {label} digest does not match fetched bytes")
        raw[label] = content
        parsed[label] = _json_bytes(content, f"base {label}")
    manifest = parsed["manifest"]
    config_descriptor = manifest.get("config")
    if not isinstance(config_descriptor, dict):
        raise ValueError("base manifest is missing its config descriptor")
    index_media_type = parsed["index"].get("mediaType")
    manifest_media_type = manifest.get("mediaType")
    config_media_type = config_descriptor.get("mediaType")
    if not all(
        isinstance(value, str)
        for value in (index_media_type, manifest_media_type, config_media_type)
    ):
        raise ValueError("base descriptor media types are missing")
    base_record = {
        "schema_version": 1,
        "kind": "fixture-base-image-record",
        "fixture_only": True,
        "repository": authority["repository"],
        "index": {
            "media_type": index_media_type,
            "digest": expected["index"],
            "size": len(raw["index"]),
            "raw": raw["index"].decode("utf-8"),
        },
        "manifest": {
            "media_type": manifest_media_type,
            "digest": expected["manifest"],
            "size": len(raw["manifest"]),
            "raw": raw["manifest"].decode("utf-8"),
        },
        "config": {
            "media_type": config_media_type,
            "digest": expected["config"],
            "size": len(raw["config"]),
            "raw": raw["config"].decode("utf-8"),
        },
    }
    wheels = sorted(Path(wheel_dir).glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("image workflow input requires one exact wheel")
    return prepare_context(
        **image_input,
        base_record=base_record,
        wheel_path=wheels[0],
        output_dir=Path(output_dir),
    )


def _load_context(context_dir: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    metadata = load_json(context_dir / CONTEXT_METADATA)
    recipe = load_json(context_dir / CONTEXT_RECIPE)
    recipe_envelope = _exact(recipe, {"payload", "payload_sha256"}, "recipe")
    payload = _exact(
        recipe_envelope["payload"],
        {
            "schema_version",
            "kind",
            "fixture_only",
            "unpublished",
            "publication_attempted",
            "source_date_epoch",
            "target_platform",
            "build_key_sha256",
            "task_sha256",
            "metadata_sha256",
            "source",
            "base",
            "wheel",
            "implementation",
            "context_files",
        },
        "recipe payload",
    )
    wheel = _exact(
        payload["wheel"],
        {
            "filename",
            "sha256",
            "size",
            "spec_id",
            "declaration_sha256",
            "version",
            "python_abi",
            "cpu_arch",
            "accelerator",
            "accelerator_runtime",
            "npu_arch_or_na",
            "os",
            "binary_profile_id",
            "requires_dist",
        },
        "recipe wheel",
    )
    actual_files = sorted(path.name for path in context_dir.iterdir() if path.is_file())
    if actual_files != payload["context_files"]:
        raise ValueError("build context allowlist mismatch")
    return metadata, recipe, context_dir / wheel["filename"]


def _verify_evidence(
    recipe: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, str]:
    _exact(
        evidence,
        {
            "schema_version",
            "kind",
            "recipe_sha256",
            "build_key_sha256",
            "base_verification",
            "install",
            "runtime",
            "oci",
        },
        "image build evidence",
    )
    if (
        evidence["schema_version"] != 1
        or evidence["kind"] != "ucm-image-build-evidence"
    ):
        raise ValueError("image build evidence identity is invalid")
    payload = recipe["payload"]
    if evidence["recipe_sha256"] != recipe["payload_sha256"]:
        raise ValueError("evidence recipe digest mismatch")
    if evidence["build_key_sha256"] != payload["build_key_sha256"]:
        raise ValueError("evidence build key mismatch")

    base = _exact(
        evidence["base_verification"],
        {"schema_version", "kind", "base_subject", "target_platform", "status"},
        "base verification",
    )
    if base != {
        "schema_version": 1,
        "kind": "ucm-base-verification",
        "base_subject": payload["base"]["subject"],
        "target_platform": payload["target_platform"],
        "status": "passed",
    }:
        raise ValueError("base verification did not pass for the authorized subject")

    install = _exact(
        evidence["install"],
        {
            "schema_version",
            "kind",
            "wheel_filename",
            "wheel_sha256",
            "version",
            "requires_dist",
            "pip_command",
            "pip_check",
            "direct_url",
            "installed_packages",
            "imports",
            "status",
        },
        "install result",
    )
    wheel = payload["wheel"]
    if (
        install["schema_version"] != 1
        or install["kind"] != "ucm-install-result"
        or install["wheel_filename"] != wheel["filename"]
        or install["wheel_sha256"] != wheel["sha256"]
        or install["version"] != wheel["version"]
        or install["requires_dist"] != ["wrapt==1.17.2"]
        or install["status"] != "passed"
    ):
        raise ValueError("wheel install result does not match the recipe")
    command = install["pip_command"]
    if (
        not isinstance(command, list)
        or len(command) != 8
        or command[1:7]
        != [
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
        ]
        or Path(command[7]).name != wheel["filename"]
        or "--no-deps" in command
    ):
        raise ValueError(
            "pip install command is not the ordinary dependency-resolving command"
        )
    if install["pip_check"] != "passed":
        raise ValueError("pip check required gate failed")
    if install["imports"] != {"ucm": "passed", "wrapt": "passed"}:
        raise ValueError("required import gate failed")
    if install["installed_packages"] != {
        "uc-manager": wheel["version"],
        "wrapt": "1.17.2",
    }:
        raise ValueError("installed package versions do not match")
    expected_archive_hash = "sha256=" + wheel["sha256"].removeprefix("sha256:")
    direct_url = install["direct_url"]
    if (
        not isinstance(direct_url, dict)
        or Path(str(direct_url.get("url", "")).removeprefix("file://")).name
        != wheel["filename"]
        or direct_url.get("archive_info", {}).get("hash") != expected_archive_hash
    ):
        raise ValueError("direct_url does not bind the installed wheel bytes")

    runtime = _exact(
        evidence["runtime"],
        {
            "schema_version",
            "kind",
            "python_version",
            "soabi",
            "package_version",
            "shared_objects",
            "abi",
            "accelerator_runtime",
            "device",
            "hardware_passed",
            "status",
        },
        "runtime inspection",
    )
    abi = _exact(
        runtime["abi"],
        {"expected_python_abi", "observed_python_abi", "status"},
        "runtime ABI",
    )
    if (
        runtime["schema_version"] != 1
        or runtime["kind"] != "ucm-runtime-inspection"
        or runtime["package_version"] != wheel["version"]
        or abi
        != {
            "expected_python_abi": wheel["python_abi"],
            "observed_python_abi": wheel["python_abi"],
            "status": "passed",
        }
    ):
        raise ValueError("runtime ABI required gate failed")
    external = {
        "accelerator_runtime": "fixture image build cannot validate the accelerator runtime",
        "device": "fixture image build cannot validate accelerator hardware",
    }
    for label, reason in external.items():
        if runtime[label] != {"status": "external-required", "reason": reason}:
            raise ValueError(
                "fixture hardware/runtime evidence must remain external-required"
            )
    if (
        runtime["hardware_passed"] is not False
        or runtime["status"] != "external-required"
    ):
        raise ValueError("fixture hardware cannot be self-asserted as passed")

    oci = _exact(
        evidence["oci"],
        {"output", "media_type", "digest", "platform", "published"},
        "OCI evidence",
    )
    if (
        oci["output"] != "local-oci"
        or oci["media_type"]
        not in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
        }
        or _digest(oci["digest"], "OCI digest") != oci["digest"]
        or oci["platform"] != payload["target_platform"]
    ):
        raise ValueError("OCI evidence does not describe the authorized local output")
    if oci["published"] is not False:
        raise ValueError("fixture OCI output must never be published")
    return {
        "base_verified": "passed",
        "wheel_verified": "passed",
        "install": "passed",
        "pip_check": "passed",
        "direct_url": "passed",
        "ucm_import": "passed",
        "wrapt_import": "passed",
        "abi": "passed",
    }


def verify_image(
    context_dir: Path,
    evidence: dict[str, Any],
    *,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    """Recompute context closure, validate required gates, and emit no publication."""
    context_dir = Path(context_dir)
    metadata, recipe, wheel_path = _load_context(context_dir)
    metadata = _exact(
        metadata,
        {
            "schema_version",
            "kind",
            "source_case",
            "candidate",
            "task",
            "inventory",
            "base_record",
            "target_platform",
            "wheel_record",
        },
        "image metadata",
    )
    if (
        metadata["schema_version"] != 1
        or metadata["kind"] != "ucm-image-source-metadata"
    ):
        raise ValueError("image metadata identity is invalid")
    if (
        metadata["wheel_record"]
        != metadata["source_case"].get("wheel_records", [None])[0]
    ):
        raise ValueError("metadata wheel record is not the exact Task 2 record")
    recomputed_metadata, recomputed_recipe = _derive_recipe(
        source_case=metadata["source_case"],
        candidate=metadata["candidate"],
        task=metadata["task"],
        inventory=metadata["inventory"],
        base_record=metadata["base_record"],
        target_platform=metadata["target_platform"],
        wheel_path=wheel_path,
        docker_root=context_dir,
    )
    if metadata != recomputed_metadata:
        raise ValueError("image metadata does not match recomputed source closure")
    if recipe != recomputed_recipe:
        raise ValueError("image recipe does not match recomputed source closure")
    gates = _verify_evidence(recipe, evidence)
    payload = recipe["payload"]
    result_payload = {
        "schema_version": 1,
        "kind": "ucm-image-result",
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": False,
        "recipe_sha256": recipe["payload_sha256"],
        "build_key_sha256": payload["build_key_sha256"],
        "task_key": payload["task_sha256"],
        "ucm_version": payload["wheel"]["version"],
        "source": copy.deepcopy(payload["source"]),
        "base": copy.deepcopy(payload["base"]),
        "target_platform": payload["target_platform"],
        "wheel": copy.deepcopy(payload["wheel"]),
        "implementation": copy.deepcopy(payload["implementation"]),
        "oci": copy.deepcopy(evidence["oci"]),
        "gates": gates,
        "runtime_validation": "external-required",
        "device_validation": "external-required",
        "status": "fixture-verified-unpublished",
    }
    result = {**result_payload, "result_sha256": sha256_value(result_payload)}
    schema = load_json(Path(schema_dir) / "image-result.schema.json")
    validate_schema(result, schema)
    return result


def validate_image_result(
    result: object, *, schema_dir: Path = DEFAULT_SCHEMA_DIR
) -> dict[str, Any]:
    """Reopen a canonical image result and revalidate its embedded byte chains."""
    if not isinstance(result, dict):
        raise ValueError("image result must be an object")
    schema = load_json(Path(schema_dir) / "image-result.schema.json")
    validate_schema(result, schema)
    payload = {
        key: copy.deepcopy(value)
        for key, value in result.items()
        if key != "result_sha256"
    }
    if result["result_sha256"] != sha256_value(payload):
        raise ValueError("image result digest does not match its payload")
    if result.get("candidate_kind") == "real-candidate":
        task = real_image_authority(
            result.get("family_id"), result["target_platform"].split("/", 1)[1]
        )
        if (
            result.get("fixture_only") is not False
            or result.get("unpublished") is not True
            or result.get("publication_attempted") is not False
            or result.get("task_key") != task["task_sha256"]
            or result.get("profile_id") != task["profile_id"]
            or result.get("spec_id") != task["spec_id"]
            or result.get("target_repository") != task["target_repository"]
            or result.get("target_tag") != task["target_tag"]
        ):
            raise ValueError("real image result differs from exact task authority")
        if validate_real_base_authority(result["base"], task) != result["base"]:
            raise ValueError("real image result base authority is noncanonical")
        if result["implementation"] != implementation_digests():
            raise ValueError("real image result implementation digest is not current")
        content = result.get("content_identity")
        if not isinstance(content, dict):
            raise ValueError("real image result content identity is missing")
        stable = {
            key: copy.deepcopy(value)
            for key, value in content.items()
            if key != "content_identity_sha256"
        }
        if (
            content.get("content_identity_sha256") != sha256_value(stable)
            or result.get("content_identity_sha256")
            != content.get("content_identity_sha256")
            or result["oci"]["digest"] != content.get("manifest_digest")
            or result["oci"]["platform"] != result["target_platform"]
            or result["oci"]["published"] is not False
        ):
            raise ValueError("real image result content identity is invalid")
        return copy.deepcopy(result)
    target_platform = result["target_platform"]
    base_record = {
        key: copy.deepcopy(result["base"][key])
        for key in (
            "schema_version",
            "kind",
            "fixture_only",
            "repository",
            "index",
            "manifest",
            "config",
        )
    }
    if _validate_base(base_record, target_platform) != result["base"]:
        raise ValueError("image result base descriptor closure is noncanonical")
    if result["implementation"] != implementation_digests():
        raise ValueError("image result implementation digest is not current")
    if result["wheel"]["version"] != result["ucm_version"]:
        raise ValueError("image result wheel and UCM versions disagree")
    if result["wheel"]["cpu_arch"] != target_platform.split("/", 1)[1]:
        raise ValueError("image result wheel architecture does not match platform")
    if result["oci"]["platform"] != target_platform:
        raise ValueError("image result OCI platform does not match target platform")
    return copy.deepcopy(result)


def _canonical_tar_name(name: str, label: str) -> str:
    path = Path(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != name.rstrip("/")
    ):
        raise ValueError(f"{label} contains noncanonical member {name!r}")
    return name.rstrip("/")


def _load_real_context(
    context_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path]:
    """Rebuild a real context contract from raw wheels and reviewed authorities."""
    context = Path(context_dir)
    recipe = load_json(context / CONTEXT_RECIPE)
    authority = load_json(context / REAL_IMAGE_CONTEXT_RECORD)
    if (
        not isinstance(recipe.get("payload"), dict)
        or recipe["payload"].get("candidate_kind") != "real-candidate"
    ):
        raise ValueError("context is not a real-candidate image recipe")
    payload = recipe["payload"]
    if recipe.get("payload_sha256") != sha256_value(payload):
        raise ValueError("real context recipe payload digest is invalid")
    wheel = payload.get("wheel")
    wrapt = payload.get("wrapt_wheel")
    if not isinstance(wheel, dict) or not isinstance(wrapt, dict):
        raise ValueError("real context wheel authority is missing")
    wheel_path = context / str(wheel.get("filename"))
    wrapt_path = context / str(wrapt.get("filename"))
    expected_files = payload.get("context_files")
    if not isinstance(expected_files, list) or not all(
        isinstance(item, str) for item in expected_files
    ):
        raise ValueError("real context allowlist is invalid")
    audit_real_context(context, set(expected_files))
    target_platform = payload.get("target_platform")
    if not isinstance(target_platform, str) or "/" not in target_platform:
        raise ValueError("real context target platform is invalid")
    with tempfile.TemporaryDirectory() as temporary:
        rebuilt_dir = Path(temporary) / "context"
        rebuilt_recipe = prepare_real_context(
            family_id=payload.get("family_id"),
            architecture=target_platform.split("/", 1)[1],
            wheel_path=wheel_path,
            wheel_inspection=authority.get("wheel_inspection"),
            base_record=authority.get("base"),
            wrapt_path=wrapt_path,
            output_dir=rebuilt_dir,
            docker_root=context,
        )
        rebuilt_authority = load_json(rebuilt_dir / REAL_IMAGE_CONTEXT_RECORD)
        rebuilt_lock = (rebuilt_dir / REAL_REQUIREMENTS_LOCK).read_bytes()
    if recipe != rebuilt_recipe or authority != rebuilt_authority:
        raise ValueError(
            "real context recipe/authority differs from source recomputation"
        )
    if (context / REAL_REQUIREMENTS_LOCK).read_bytes() != rebuilt_lock:
        raise ValueError("real context requirements lock differs from recomputation")
    return authority, recipe, wheel_path, wrapt_path


class _DigestingReader:
    """Hash a binary stream while never requesting an unbounded read."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._sha256 = hashlib.sha256()
        self.size = 0

    def read(self, size: int | None = OCI_STREAM_CHUNK_SIZE) -> bytes:
        if size is None or size < 0:
            size = OCI_STREAM_CHUNK_SIZE
        content = self._stream.read(min(size, OCI_STREAM_CHUNK_SIZE))
        if content:
            self._sha256.update(content)
            self.size += len(content)
        return content

    def drain(self) -> None:
        while self.read(OCI_STREAM_CHUNK_SIZE):
            pass

    def hexdigest(self) -> str:
        return self._sha256.hexdigest()


def _read_stream_bytes(stream: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        content = stream.read(OCI_STREAM_CHUNK_SIZE)
        if not content:
            return b"".join(chunks)
        chunks.append(content)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            content = stream.read(OCI_STREAM_CHUNK_SIZE)
            if not content:
                return "sha256:" + digest.hexdigest()
            digest.update(content)


def _descriptor_stream(
    archive: tarfile.TarFile, descriptor: dict[str, Any], label: str
) -> tuple[_DigestingReader, str, int]:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{label} descriptor must be an object")
    digest = _digest(descriptor.get("digest"), f"{label} digest")
    size = descriptor.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError(f"{label} descriptor size must be a positive integer")
    member_name = "blobs/sha256/" + digest.removeprefix("sha256:")
    try:
        member = archive.getmember(member_name)
        stream = archive.extractfile(member)
    except KeyError as error:
        raise ValueError(f"OCI layout is missing {label} blob {digest}") from error
    if stream is None or not member.isfile():
        raise ValueError(f"OCI {label} blob is not a regular file")
    return _DigestingReader(stream), digest, size


def _finish_descriptor_stream(
    stream: _DigestingReader, digest: str, size: int, label: str
) -> None:
    stream.drain()
    if stream.size != size or "sha256:" + stream.hexdigest() != digest:
        raise ValueError(f"OCI {label} blob does not match descriptor size/digest")


def _descriptor_blob(
    archive: tarfile.TarFile, descriptor: dict[str, Any], label: str
) -> bytes:
    stream, digest, size = _descriptor_stream(archive, descriptor, label)
    content = _read_stream_bytes(stream)
    _finish_descriptor_stream(stream, digest, size, label)
    return content


def evidence_from_oci(
    context_dir: Path,
    oci_path: Path,
    *,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Derive all build evidence directly from a standard local OCI archive."""
    context_dir = Path(context_dir)
    oci_path = Path(oci_path)
    candidate_recipe = load_json(context_dir / CONTEXT_RECIPE)
    candidate_payload = candidate_recipe.get("payload")
    real_candidate = (
        isinstance(candidate_payload, dict)
        and candidate_payload.get("candidate_kind") == "real-candidate"
    )
    if real_candidate:
        context_authority, recipe, wheel_path, _ = _load_real_context(context_dir)
    else:
        _, recipe, wheel_path = _load_context(context_dir)
        context_authority = None
    payload = recipe["payload"]
    target_os, target_architecture = payload["target_platform"].split("/", 1)
    try:
        archive = tarfile.open(oci_path, mode="r")
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"cannot open local OCI archive: {error}") from error
    with archive:
        seen: set[str] = set()
        for member in archive.getmembers():
            name = _canonical_tar_name(member.name, "OCI layout")
            if name in seen:
                raise ValueError(f"OCI layout contains duplicate member {name}")
            seen.add(name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"OCI layout contains unsupported member type {name}")
        try:
            layout_stream = archive.extractfile("oci-layout")
            index_stream = archive.extractfile("index.json")
        except KeyError as error:
            raise ValueError("OCI layout requires oci-layout and index.json") from error
        if layout_stream is None:
            raise ValueError("OCI oci-layout is not a regular file")
        layout_raw = _read_stream_bytes(layout_stream)
        if _json_bytes(layout_raw, "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("unsupported OCI image layout version")
        if index_stream is None:
            raise ValueError("OCI index.json is not a regular file")
        index_raw = _read_stream_bytes(index_stream)
        index = _json_bytes(index_raw, "OCI index.json")
        if (
            index.get("schemaVersion") != 2
            or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
            or not isinstance(index.get("manifests"), list)
            or len(index["manifests"]) != 1
        ):
            raise ValueError(
                "local OCI output must contain one standard image manifest"
            )
        manifest_descriptor = index["manifests"][0]
        if not isinstance(manifest_descriptor, dict):
            raise ValueError("OCI image manifest descriptor must be an object")
        descriptor_platform = manifest_descriptor.get("platform")
        if not isinstance(descriptor_platform, dict) or (
            descriptor_platform.get("os"),
            descriptor_platform.get("architecture"),
        ) != (target_os, target_architecture):
            raise ValueError("OCI descriptor platform does not match recipe")
        if (
            manifest_descriptor.get("mediaType")
            != "application/vnd.oci.image.manifest.v1+json"
        ):
            raise ValueError("OCI descriptor is not an image manifest")
        manifest_raw = _descriptor_blob(archive, manifest_descriptor, "manifest")
        manifest = _json_bytes(manifest_raw, "OCI manifest")
        if (
            manifest.get("schemaVersion") != 2
            or manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
            or not isinstance(manifest.get("config"), dict)
            or not isinstance(manifest.get("layers"), list)
            or not manifest["layers"]
        ):
            raise ValueError("OCI manifest structure is invalid")
        config_descriptor = manifest["config"]
        if (
            config_descriptor.get("mediaType")
            != "application/vnd.oci.image.config.v1+json"
        ):
            raise ValueError("OCI manifest config media type is invalid")
        config_raw = _descriptor_blob(archive, config_descriptor, "config")
        config = _json_bytes(config_raw, "OCI config")
        if (config.get("os"), config.get("architecture")) != (
            target_os,
            target_architecture,
        ):
            raise ValueError("OCI config platform does not match recipe")
        rootfs = config.get("rootfs")
        if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
            raise ValueError("OCI config rootfs.type must be layers")
        diff_ids = rootfs.get("diff_ids")
        if not isinstance(diff_ids, list) or len(diff_ids) != len(manifest["layers"]):
            raise ValueError(
                "OCI config rootfs diff_ids must match the layer count exactly"
            )
        for diff_index, diff_id in enumerate(diff_ids):
            _digest(diff_id, f"OCI rootfs diff_id {diff_index}")

        expected_paths = {
            "usr/local/share/ucm-release/image-recipe.json": "recipe",
            "usr/local/share/ucm-release/base-verification.json": "base_verification",
            "usr/local/share/ucm-release/install-result.json": "install",
            "usr/local/share/ucm-release/runtime-inspection.json": "runtime",
        }
        if real_candidate:
            expected_paths["usr/local/share/ucm-release/image-authority.json"] = (
                "authority"
            )
        else:
            expected_paths["usr/local/share/ucm-release/image-metadata.json"] = (
                "metadata"
            )
            expected_paths[f"tmp/{payload['wheel']['filename']}"] = "wheel"
        observed: dict[str, bytes] = {}
        for layer_index, layer_descriptor in enumerate(manifest["layers"]):
            if (
                not isinstance(layer_descriptor, dict)
                or layer_descriptor.get("mediaType") not in OCI_LAYER_MEDIA_TYPES
            ):
                raise ValueError("OCI image contains an unsupported layer media type")
            layer_stream, layer_digest, layer_size = _descriptor_stream(
                archive, layer_descriptor, f"layer {layer_index}"
            )
            media_type = layer_descriptor["mediaType"]
            gzip_stream: gzip.GzipFile | None = None
            if media_type != "application/vnd.oci.image.layer.v1.tar":
                gzip_stream = gzip.GzipFile(fileobj=layer_stream, mode="rb")
                uncompressed_stream = _DigestingReader(gzip_stream)
            else:
                uncompressed_stream = _DigestingReader(layer_stream)
            try:
                layer_archive = tarfile.open(fileobj=uncompressed_stream, mode="r|")
                with layer_archive:
                    layer_seen: set[str] = set()
                    for member in layer_archive:
                        name = _canonical_tar_name(
                            member.name, f"OCI layer {layer_index}"
                        )
                        if name in layer_seen:
                            raise ValueError(
                                f"OCI layer contains duplicate member {name}"
                            )
                        layer_seen.add(name)
                        label = expected_paths.get(name)
                        if label is None:
                            continue
                        if label in observed or not member.isfile():
                            raise ValueError(
                                f"OCI image contains duplicate/non-file {label} evidence"
                            )
                        stream = layer_archive.extractfile(member)
                        if stream is None:
                            raise ValueError(f"cannot read OCI {label} evidence")
                        observed[label] = _read_stream_bytes(stream)
                uncompressed_stream.drain()
            except (gzip.BadGzipFile, EOFError, OSError) as error:
                raise ValueError(
                    f"OCI gzip layer {layer_index} cannot be decompressed: {error}"
                ) from error
            except tarfile.TarError as error:
                raise ValueError(
                    f"cannot read OCI layer {layer_index}: {error}"
                ) from error
            finally:
                if gzip_stream is not None:
                    gzip_stream.close()
            observed_diff_id = "sha256:" + uncompressed_stream.hexdigest()
            if observed_diff_id != diff_ids[layer_index]:
                raise ValueError(
                    f"OCI layer {layer_index} does not match rootfs diff_id order"
                )
            _finish_descriptor_stream(
                layer_stream,
                layer_digest,
                layer_size,
                f"layer {layer_index}",
            )
        missing = sorted(set(expected_paths.values()) - set(observed))
        if missing:
            raise ValueError(
                f"OCI image is missing required embedded evidence: {missing}"
            )
        context_recipe = load_json(context_dir / CONTEXT_RECIPE)
        if _json_bytes(observed["recipe"], "embedded recipe") != context_recipe:
            raise ValueError("OCI embedded recipe does not match context")
        if real_candidate:
            if (
                _json_bytes(observed["authority"], "embedded authority")
                != context_authority
            ):
                raise ValueError("OCI embedded real authority does not match context")
        else:
            context_metadata = load_json(context_dir / CONTEXT_METADATA)
            if (
                _json_bytes(observed["metadata"], "embedded metadata")
                != context_metadata
            ):
                raise ValueError("OCI embedded metadata does not match context")
            if observed["wheel"] != wheel_path.read_bytes():
                raise ValueError(
                    "OCI embedded wheel does not match the authorized context bytes"
                )
        if evidence_dir is not None:
            evidence_dir = Path(evidence_dir)
            if evidence_dir.exists() and any(evidence_dir.iterdir()):
                raise ValueError(
                    f"OCI evidence directory must be absent or empty: {evidence_dir}"
                )
            evidence_dir.mkdir(parents=True, exist_ok=True)
            raw_files = {
                "oci-layout.json": layout_raw,
                "index.json": index_raw,
                "manifest.json": manifest_raw,
                "config.json": config_raw,
            }
            for filename, content in raw_files.items():
                (evidence_dir / filename).write_bytes(content)
            compact_closure = {
                "schema_version": 1,
                "kind": (
                    "ucm-real-compact-oci-evidence"
                    if real_candidate
                    else "ucm-compact-oci-evidence"
                ),
                "target_platform": payload["target_platform"],
                "manifest_descriptor": copy.deepcopy(manifest_descriptor),
                "config_descriptor": copy.deepcopy(config_descriptor),
                "layers": copy.deepcopy(manifest["layers"]),
                "diff_ids": copy.deepcopy(diff_ids),
                "recipe_payload_sha256": recipe["payload_sha256"],
                "wheel_sha256": payload["wheel"]["sha256"],
                "archive_sha256": _file_sha256(oci_path),
                "archive_size": oci_path.stat().st_size,
            }
            if real_candidate:
                compact_closure["authority_sha256"] = payload["authority_sha256"]
                compact_closure["annotations"] = copy.deepcopy(
                    manifest.get("annotations", {})
                )
                compact_closure["labels"] = copy.deepcopy(
                    config.get("config", {}).get("Labels", {})
                )
                compact_closure["created"] = config.get("created")
                compact_closure["history"] = copy.deepcopy(config.get("history"))
            else:
                compact_closure["metadata_sha256"] = payload["metadata_sha256"]
            _write_json(evidence_dir / "closure.json", compact_closure)
        common_evidence = {
            "schema_version": 1,
            "kind": (
                "ucm-real-image-build-evidence"
                if real_candidate
                else "ucm-image-build-evidence"
            ),
            "recipe_sha256": recipe["payload_sha256"],
            "build_key_sha256": payload["build_key_sha256"],
            "base_verification": _json_bytes(
                observed["base_verification"], "embedded base verification"
            ),
            "install": _json_bytes(observed["install"], "embedded install result"),
            "runtime": _json_bytes(observed["runtime"], "embedded runtime inspection"),
            "oci": {
                "output": "local-oci",
                "media_type": manifest_descriptor["mediaType"],
                "digest": manifest_descriptor["digest"],
                "platform": payload["target_platform"],
                "published": False,
            },
        }
        if real_candidate:
            common_evidence["oci_closure"] = {
                "manifest_digest": manifest_descriptor["digest"],
                "config_digest": config_descriptor["digest"],
                "layers": copy.deepcopy(manifest["layers"]),
                "diff_ids": copy.deepcopy(diff_ids),
                "annotations": copy.deepcopy(manifest.get("annotations", {})),
                "labels": copy.deepcopy(config.get("config", {}).get("Labels", {})),
                "created": config.get("created"),
                "history": copy.deepcopy(config.get("history")),
            }
        return common_evidence


def verify_real_image(
    context_dir: Path,
    evidence: dict[str, Any],
    *,
    output_mode: str,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    """Recompute a real context and emit an unpublished member result."""
    if output_mode not in {"feature", "production"}:
        raise ValueError("real image output mode must be feature or production")
    authority, recipe, _, _ = _load_real_context(Path(context_dir))
    payload = recipe["payload"]
    _exact(
        evidence,
        {
            "schema_version",
            "kind",
            "recipe_sha256",
            "build_key_sha256",
            "base_verification",
            "install",
            "runtime",
            "oci",
            "oci_closure",
        },
        "real image build evidence",
    )
    if (
        evidence["schema_version"] != 1
        or evidence["kind"] != "ucm-real-image-build-evidence"
        or evidence["recipe_sha256"] != recipe["payload_sha256"]
        or evidence["build_key_sha256"] != payload["build_key_sha256"]
    ):
        raise ValueError("real image evidence identity differs from recipe")
    base_verification = evidence["base_verification"]
    if base_verification != {
        "schema_version": 1,
        "kind": "ucm-base-verification",
        "base_subject": payload["base"]["subject"],
        "target_platform": payload["target_platform"],
        "status": "passed",
    }:
        raise ValueError("real base image gate did not pass")
    gates = {
        "base_verified": "passed",
        "wheel_verified": "passed",
        **verify_real_runtime_evidence(recipe, evidence),
    }
    identity = real_content_identity(recipe, evidence["oci_closure"])
    oci = evidence["oci"]
    if (
        not isinstance(oci, dict)
        or oci.get("output") != "local-oci"
        or oci.get("digest") != identity["manifest_digest"]
        or oci.get("platform") != payload["target_platform"]
        or oci.get("published") is not False
    ):
        raise ValueError("real OCI evidence differs from content identity")
    task = authority["task"]
    wheel_result = {
        "filename": payload["wheel"]["filename"],
        "sha256": payload["wheel"]["sha256"],
        "size": payload["wheel"]["size"],
        "spec_id": task["spec_id"],
        "declaration_sha256": authority["wheel_inspection"]["declaration_sha256"],
        "version": payload["wheel"]["version"],
        "python_abi": task["python_abi"],
        "cpu_arch": task["cpu_arch"],
        "accelerator": authority["wheel_embedded_build"]["accelerator"],
        "accelerator_runtime": authority["wheel_embedded_build"]["accelerator_runtime"],
        "npu_arch_or_na": authority["wheel_embedded_build"]["npu_arch_or_na"],
        "os": authority["wheel_embedded_build"]["os"],
        "binary_profile_id": authority["wheel_embedded_build"]["binary_profile_id"],
        "requires_dist": ["wrapt==1.17.2"],
    }
    source = {
        "commit": payload["source"]["commit"],
        "tree": payload["source"]["tree"],
        "archive_sha256": payload["source"]["archive_sha256"],
        "context_sha256": payload["source"]["context_sha256"],
        "task_sha256": payload["task_sha256"],
        "wheel_build_key": authority["wheel_embedded_build"]["build_key"],
    }
    result_payload = {
        "schema_version": 1,
        "kind": "ucm-image-result",
        "candidate_kind": "real-candidate",
        "fixture_only": False,
        "unpublished": True,
        "publication_attempted": False,
        "output_mode": output_mode,
        "recipe_sha256": recipe["payload_sha256"],
        "content_identity_sha256": identity["content_identity_sha256"],
        "build_key_sha256": payload["build_key_sha256"],
        "task_key": payload["task_sha256"],
        "family_id": payload["family_id"],
        "profile_id": payload["profile_id"],
        "spec_id": payload["spec_id"],
        "ucm_version": payload["wheel"]["version"].split("+", 1)[0],
        "source": source,
        "base": copy.deepcopy(payload["base"]),
        "target_platform": payload["target_platform"],
        "target_repository": payload["target_repository"],
        "target_tag": payload["target_tag"],
        "wheel": wheel_result,
        "wrapt_wheel": copy.deepcopy(payload["wrapt_wheel"]),
        "dependency_lock": copy.deepcopy(payload["dependency_lock"]),
        "implementation": copy.deepcopy(payload["implementation"]),
        "toolchain": copy.deepcopy(payload["toolchain"]),
        "oci": {
            "output": (
                "local-oci" if output_mode == "feature" else "canonical-member-record"
            ),
            "media_type": oci["media_type"],
            "digest": identity["manifest_digest"],
            "platform": payload["target_platform"],
            "published": False,
        },
        "content_identity": identity,
        "gates": gates,
        "runtime_validation": "external-required",
        "device_validation": "external-required",
        "status": "real-verified-unpublished",
    }
    result = {**result_payload, "result_sha256": sha256_value(result_payload)}
    schema = load_json(Path(schema_dir) / "image-result.schema.json")
    validate_schema(result, schema)
    return result


def validate_real_compact_oci_evidence(
    evidence_dir: Path,
    *,
    image_result: object,
    recipe: object,
) -> dict[str, Any]:
    """Reopen saved real descriptors after the large OCI archive is discarded."""
    directory = Path(evidence_dir)
    expected_files = {
        "oci-layout.json",
        "index.json",
        "manifest.json",
        "config.json",
        "closure.json",
    }
    if (
        not directory.is_dir()
        or {path.name for path in directory.iterdir() if path.is_file()}
        != expected_files
        or any(path.is_symlink() for path in directory.iterdir())
    ):
        raise ValueError("real compact OCI evidence file set is noncanonical")
    raw: dict[str, bytes] = {}
    values: dict[str, dict[str, Any]] = {}
    for name in expected_files:
        raw[name] = (directory / name).read_bytes()
        values[name] = _json_bytes(raw[name], f"real compact {name}")
    if values["oci-layout.json"] != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("real compact OCI layout version is invalid")
    closure = values["closure.json"]
    if raw["closure.json"] != canonical_bytes(closure) + b"\n":
        raise ValueError("real compact OCI closure bytes are noncanonical")
    index = values["index.json"]
    manifest = values["manifest.json"]
    config = values["config.json"]
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError("real compact OCI index must contain one manifest")
    manifest_descriptor = manifests[0]
    config_descriptor = manifest.get("config")
    if not isinstance(manifest_descriptor, dict) or not isinstance(
        config_descriptor, dict
    ):
        raise ValueError("real compact OCI descriptors are missing")
    if (
        manifest_descriptor.get("digest")
        != "sha256:" + hashlib.sha256(raw["manifest.json"]).hexdigest()
        or manifest_descriptor.get("size") != len(raw["manifest.json"])
        or config_descriptor.get("digest")
        != "sha256:" + hashlib.sha256(raw["config.json"]).hexdigest()
        or config_descriptor.get("size") != len(raw["config.json"])
    ):
        raise ValueError("real compact OCI raw descriptors do not reopen")
    result = validate_image_result(image_result)
    if result.get("candidate_kind") != "real-candidate":
        raise ValueError("real compact OCI evidence requires a real result")
    if not isinstance(recipe, dict) or recipe.get("payload_sha256") != result.get(
        "recipe_sha256"
    ):
        raise ValueError("real compact OCI recipe does not match result")
    identity = result["content_identity"]
    expected_closure = {
        "schema_version": 1,
        "kind": "ucm-real-compact-oci-evidence",
        "target_platform": result["target_platform"],
        "manifest_descriptor": manifest_descriptor,
        "config_descriptor": config_descriptor,
        "layers": manifest.get("layers"),
        "diff_ids": config.get("rootfs", {}).get("diff_ids"),
        "recipe_payload_sha256": result["recipe_sha256"],
        "wheel_sha256": result["wheel"]["sha256"],
        "archive_sha256": closure.get("archive_sha256"),
        "archive_size": closure.get("archive_size"),
        "authority_sha256": recipe["payload"]["authority_sha256"],
        "annotations": manifest.get("annotations", {}),
        "labels": config.get("config", {}).get("Labels", {}),
        "created": config.get("created"),
        "history": config.get("history"),
    }
    _digest(expected_closure["archive_sha256"], "real OCI archive digest")
    if (
        not isinstance(expected_closure["archive_size"], int)
        or expected_closure["archive_size"] < 1
        or closure != expected_closure
        or manifest_descriptor.get("digest") != identity["manifest_digest"]
        or config_descriptor.get("digest") != identity["config_digest"]
        or manifest.get("layers") != identity["layers"]
        or config.get("rootfs", {}).get("diff_ids") != identity["diff_ids"]
        or manifest.get("annotations", {}) != identity["annotations"]
        or config.get("config", {}).get("Labels", {}) != identity["labels"]
        or config.get("created") != identity["created"]
        or config.get("history") != identity["history"]
    ):
        raise ValueError("real compact OCI closure differs from result identity")
    stable = {
        "manifest_digest": manifest_descriptor["digest"],
        "config_digest": config_descriptor["digest"],
        "layers": copy.deepcopy(identity["layers"]),
        "diff_ids": copy.deepcopy(identity["diff_ids"]),
        "content_identity_sha256": identity["content_identity_sha256"],
        "archive_sha256": closure["archive_sha256"],
        "raw_sha256": {
            name: "sha256:" + hashlib.sha256(content).hexdigest()
            for name, content in sorted(raw.items())
        },
    }
    return {**stable, "closure_sha256": sha256_value(stable)}


def verify_oci(
    context_dir: Path,
    oci_path: Path,
    *,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    evidence_dir: Path | None = None,
    output_mode: str = "feature",
) -> dict[str, Any]:
    """Verify a Buildx local OCI output without caller-authored result summaries."""
    evidence = evidence_from_oci(context_dir, oci_path, evidence_dir=evidence_dir)
    if evidence.get("kind") == "ucm-real-image-build-evidence":
        result = verify_real_image(
            context_dir,
            evidence,
            output_mode=output_mode,
            schema_dir=schema_dir,
        )
        if evidence_dir is not None:
            validate_real_compact_oci_evidence(
                evidence_dir,
                image_result=result,
                recipe=load_json(Path(context_dir) / CONTEXT_RECIPE),
            )
        if Path(oci_path).is_file():
            Path(oci_path).unlink()
        return result
    return verify_image(context_dir, evidence, schema_dir=schema_dir)


def _raw_compact_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    if not Path(path).is_file():
        raise ValueError(f"{label} is not a regular file")
    content = Path(path).read_bytes()
    return content, _json_bytes(content, label)


def validate_compact_oci_evidence(
    evidence_dir: Path,
    *,
    image_result: object,
    image_recipe_path: Path,
    image_metadata_path: Path,
    image_prepare_path: Path,
    wheel_path: Path,
    buildkit_metadata: object,
) -> dict[str, Any]:
    """Reopen compact raw OCI descriptors and bind them to the image result."""
    evidence_dir = Path(evidence_dir)
    expected_files = {
        "oci-layout.json",
        "index.json",
        "manifest.json",
        "config.json",
        "closure.json",
    }
    if (
        not evidence_dir.is_dir()
        or {path.name for path in evidence_dir.iterdir() if path.is_file()}
        != expected_files
    ):
        raise ValueError("compact OCI evidence file set is noncanonical")
    layout_raw, layout = _raw_compact_json(
        evidence_dir / "oci-layout.json", "compact OCI layout"
    )
    index_raw, index = _raw_compact_json(
        evidence_dir / "index.json", "compact OCI index"
    )
    manifest_raw, manifest = _raw_compact_json(
        evidence_dir / "manifest.json", "compact OCI manifest"
    )
    config_raw, config = _raw_compact_json(
        evidence_dir / "config.json", "compact OCI config"
    )
    closure_raw, closure = _raw_compact_json(
        evidence_dir / "closure.json", "compact OCI closure"
    )
    if closure_raw != canonical_bytes(closure) + b"\n":
        raise ValueError("compact OCI closure bytes are noncanonical")
    if layout != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("compact OCI layout version is invalid")
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
        or not isinstance(index.get("manifests"), list)
        or len(index["manifests"]) != 1
    ):
        raise ValueError("compact OCI index is invalid")
    manifest_descriptor = index["manifests"][0]
    if not isinstance(manifest_descriptor, dict):
        raise ValueError("compact OCI manifest descriptor is invalid")
    expected_manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    if (
        manifest_descriptor.get("digest") != expected_manifest_digest
        or manifest_descriptor.get("size") != len(manifest_raw)
        or manifest_descriptor.get("mediaType")
        != "application/vnd.oci.image.manifest.v1+json"
    ):
        raise ValueError("compact OCI manifest bytes do not match index descriptor")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
        or not isinstance(manifest.get("config"), dict)
        or not isinstance(manifest.get("layers"), list)
    ):
        raise ValueError("compact OCI manifest structure is invalid")
    config_descriptor = manifest["config"]
    expected_config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    if (
        config_descriptor.get("digest") != expected_config_digest
        or config_descriptor.get("size") != len(config_raw)
        or config_descriptor.get("mediaType")
        != "application/vnd.oci.image.config.v1+json"
    ):
        raise ValueError("compact OCI config bytes do not match manifest descriptor")
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if (
        not isinstance(rootfs, dict)
        or rootfs.get("type") != "layers"
        or not isinstance(diff_ids, list)
        or not manifest["layers"]
        or len(diff_ids) != len(manifest["layers"])
    ):
        raise ValueError("compact OCI layer descriptors and diff_ids disagree")
    for position, (layer, diff_id) in enumerate(
        zip(manifest["layers"], diff_ids, strict=True)
    ):
        if not isinstance(layer, dict):
            raise ValueError(f"compact OCI layer {position} descriptor is invalid")
        if layer.get("mediaType") not in OCI_LAYER_MEDIA_TYPES:
            raise ValueError(f"compact OCI layer {position} media type is invalid")
        _digest(layer.get("digest"), f"compact OCI layer {position} digest")
        if not isinstance(layer.get("size"), int) or layer["size"] < 1:
            raise ValueError(f"compact OCI layer {position} size is invalid")
        _digest(diff_id, f"compact OCI diff_id {position}")

    recipe_raw, recipe = _raw_compact_json(image_recipe_path, "image recipe")
    metadata_raw, metadata = _raw_compact_json(image_metadata_path, "image metadata")
    prepare_raw, image_prepare = _raw_compact_json(
        image_prepare_path, "image prepare result"
    )
    if recipe_raw != canonical_bytes(recipe) + b"\n":
        raise ValueError("image recipe bytes are noncanonical")
    if metadata_raw != canonical_bytes(metadata) + b"\n":
        raise ValueError("image metadata bytes are noncanonical")
    if prepare_raw != canonical_bytes(image_prepare) + b"\n":
        raise ValueError("image prepare result bytes are noncanonical")
    recipe = _exact(recipe, {"payload", "payload_sha256"}, "image recipe")
    if recipe["payload_sha256"] != sha256_value(recipe["payload"]):
        raise ValueError("image recipe payload digest is invalid")
    payload = recipe["payload"]
    if image_prepare != recipe:
        raise ValueError("image prepare result is not the exact recipe")
    if payload.get("metadata_sha256") != sha256_value(metadata):
        raise ValueError("image recipe does not bind metadata")
    expected_metadata, expected_recipe = _derive_recipe(
        source_case=metadata.get("source_case"),
        candidate=metadata.get("candidate"),
        task=metadata.get("task"),
        inventory=metadata.get("inventory"),
        base_record=metadata.get("base_record"),
        target_platform=metadata.get("target_platform"),
        wheel_path=Path(wheel_path),
        docker_root=DOCKER_ROOT,
    )
    if metadata != expected_metadata or recipe != expected_recipe:
        raise ValueError("image metadata/recipe does not match source recomputation")
    result = validate_image_result(image_result)
    if (
        result["recipe_sha256"] != recipe["payload_sha256"]
        or result["build_key_sha256"] != payload.get("build_key_sha256")
        or result["target_platform"] != payload.get("target_platform")
        or result["wheel"] != payload.get("wheel")
        or result["source"] != payload.get("source")
        or result["base"] != payload.get("base")
        or result["implementation"] != payload.get("implementation")
    ):
        raise ValueError("image result does not match recipe closure")
    expected_closure = {
        "schema_version": 1,
        "kind": "ucm-compact-oci-evidence",
        "target_platform": result["target_platform"],
        "manifest_descriptor": copy.deepcopy(manifest_descriptor),
        "config_descriptor": copy.deepcopy(config_descriptor),
        "layers": copy.deepcopy(manifest["layers"]),
        "diff_ids": copy.deepcopy(diff_ids),
        "recipe_payload_sha256": recipe["payload_sha256"],
        "metadata_sha256": payload["metadata_sha256"],
        "wheel_sha256": payload["wheel"]["sha256"],
        "archive_sha256": closure.get("archive_sha256"),
        "archive_size": closure.get("archive_size"),
    }
    _digest(expected_closure["archive_sha256"], "OCI archive digest")
    if (
        not isinstance(expected_closure["archive_size"], int)
        or expected_closure["archive_size"] < 1
        or closure != expected_closure
    ):
        raise ValueError("compact OCI closure is noncanonical")
    platform = manifest_descriptor.get("platform")
    expected_platform = {
        "os": result["target_platform"].split("/", 1)[0],
        "architecture": result["target_platform"].split("/", 1)[1],
    }
    if platform != expected_platform or (
        config.get("os"),
        config.get("architecture"),
    ) != (expected_platform["os"], expected_platform["architecture"]):
        raise ValueError("compact OCI platform does not match image result")
    if result["oci"] != {
        "output": "local-oci",
        "media_type": manifest_descriptor["mediaType"],
        "digest": manifest_descriptor["digest"],
        "platform": result["target_platform"],
        "published": False,
    }:
        raise ValueError("image result OCI identity does not match raw evidence")

    if not isinstance(buildkit_metadata, dict):
        raise ValueError("BuildKit metadata must be an object")
    buildkit_descriptor = buildkit_metadata.get("containerimage.descriptor")
    if isinstance(buildkit_descriptor, str):
        buildkit_descriptor = _json_bytes(
            buildkit_descriptor.encode(), "BuildKit image descriptor"
        )
    if not isinstance(buildkit_descriptor, dict):
        raise ValueError("BuildKit image descriptor is missing")
    descriptor_projection = {
        key: copy.deepcopy(buildkit_descriptor.get(key))
        for key in ("mediaType", "digest", "size", "platform")
    }
    manifest_projection = {
        key: copy.deepcopy(manifest_descriptor.get(key))
        for key in ("mediaType", "digest", "size", "platform")
    }
    if (
        buildkit_metadata.get("containerimage.digest") != manifest_descriptor["digest"]
        or buildkit_metadata.get("containerimage.config.digest")
        != config_descriptor["digest"]
        or descriptor_projection != manifest_projection
    ):
        raise ValueError("BuildKit metadata does not match raw OCI descriptors")
    stable = {
        "oci_digest": manifest_descriptor["digest"],
        "config_digest": config_descriptor["digest"],
        "platform": result["target_platform"],
        "layers": copy.deepcopy(manifest["layers"]),
        "diff_ids": copy.deepcopy(diff_ids),
        "recipe_payload_sha256": recipe["payload_sha256"],
        "metadata_sha256": payload["metadata_sha256"],
        "wheel_sha256": payload["wheel"]["sha256"],
        "archive_sha256": closure["archive_sha256"],
        "raw_sha256": {
            "oci_layout": "sha256:" + hashlib.sha256(layout_raw).hexdigest(),
            "index": "sha256:" + hashlib.sha256(index_raw).hexdigest(),
            "manifest": expected_manifest_digest,
            "config": expected_config_digest,
            "recipe": "sha256:" + hashlib.sha256(recipe_raw).hexdigest(),
            "metadata": "sha256:" + hashlib.sha256(metadata_raw).hexdigest(),
        },
    }
    return {**stable, "closure_sha256": sha256_value(stable)}
