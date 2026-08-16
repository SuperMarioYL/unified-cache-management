"""Deterministic, fixture-only install-image context and result verification."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import posixpath
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from . import core as release_core
from . import registry
from . import wheel as wheel_artifact
from .core import (
    DEFAULT_SCHEMA_DIR,
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
        set(release_core.CPU_TOOLCHAIN_AUTHORITIES),
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


def _real_image_authority_from_selected_tasks(
    task: dict[str, Any],
    wheel_task: dict[str, Any],
    *,
    resolved_plan_sha256: str,
    source_repository: str,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Project already-selected image/wheel tasks from one validated plan."""
    if not isinstance(task, dict):
        raise ValueError("real image task must be an object")
    payload = {key: value for key, value in task.items() if key != "task_sha256"}
    dependency_lock = task.get("dependency_lock")
    runtime = task.get("runtime")
    runtime_patch_product = task.get("runtime_patch_product")
    runtime_patch_variants = task.get("runtime_patch_variants")
    runtime_requirements = task.get("runtime_requirements")
    expected_runtime_products = {"vllm", runtime_patch_product}
    if (
        re.fullmatch(r"image-[0-9a-f]{64}", str(task.get("task_id"))) is None
        or task.get("task_sha256") != sha256_value(payload)
        or not isinstance(dependency_lock, dict)
        or set(dependency_lock) != {"build_tools", "runtime_dependencies"}
        or task.get("dependency_lock_sha256") != sha256_value(dependency_lock)
        or not isinstance(runtime_requirements, list)
        or not runtime_requirements
        or not isinstance(runtime, dict)
        or runtime_patch_product not in {"vllm", "vllm-ascend"}
        or not isinstance(runtime_patch_variants, dict)
        or set(runtime_patch_variants) != expected_runtime_products
        or any(
            not isinstance(value, str) or not value
            for value in runtime_patch_variants.values()
        )
        or runtime.get("variant") != runtime_patch_variants.get(runtime_patch_product)
    ):
        raise ValueError("real image task identity or runtime variant is invalid")
    wheel_task = wheel_artifact._validate_wheel_task(wheel_task)
    if (
        task.get("wheel_task_id") != wheel_task["task_id"]
        or task.get("spec_id") != wheel_task["spec_id"]
        or task.get("profile_id") != wheel_task["profile_id"]
        or task.get("cpu_arch") != wheel_task["cpu_arch"]
    ):
        raise ValueError("real image task does not bind the selected wheel task")
    if (
        not isinstance(resolved_plan_sha256, str)
        or DIGEST_RE.fullmatch(resolved_plan_sha256) is None
    ):
        raise ValueError("real image resolved plan hash is invalid")
    if (
        not isinstance(source_repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repository) is None
    ):
        raise ValueError("real image source repository is invalid")
    runtime_versions: dict[str, str] = {}
    for raw_requirement in runtime_requirements:
        try:
            requirement = Requirement(raw_requirement)
        except (InvalidRequirement, TypeError) as error:
            raise ValueError("real image runtime requirement is invalid") from error
        name = canonicalize_name(requirement.name)
        specifiers = list(requirement.specifier)
        if name in runtime_versions or len(specifiers) != 1:
            raise ValueError("real image runtime requirement is not exact")
        specifier = specifiers[0]
        if specifier.operator != "==" or "*" in specifier.version:
            raise ValueError("real image runtime requirement is not exact")
        try:
            runtime_versions[name] = str(Version(specifier.version))
        except InvalidVersion as error:
            raise ValueError(
                "real image runtime requirement version is invalid"
            ) from error
    runtime_dependencies = copy.deepcopy(dependency_lock["runtime_dependencies"])
    if (
        not isinstance(runtime_dependencies, list)
        or not runtime_dependencies
        or len(runtime_dependencies) != len(runtime_versions)
        or len({record.get("name") for record in runtime_dependencies})
        != len(runtime_dependencies)
    ):
        raise ValueError("real image runtime dependency authority is invalid")
    for record in runtime_dependencies:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "name",
                "version",
                "requirement",
                "import_name",
                "filename",
                "sha256",
            }
            or runtime_versions.get(record["name"]) != record["version"]
            or record["requirement"] != f"{record['name']}=={record['version']}"
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", record["import_name"]) is None
            or not isinstance(record["filename"], str)
            or not record["filename"].endswith(".whl")
            or DIGEST_RE.fullmatch(str(record["sha256"])) is None
        ):
            raise ValueError(
                "real image dependency wheels differ from runtime requirements"
            )
    authority: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ucm-real-image-task-authority",
        "candidate_kind": "real-candidate",
        "fixture_only": False,
        "unpublished": True,
        "publication_attempted": False,
        "source_repository": source_repository,
        "source_repository_url": f"https://github.com/{source_repository}",
        "task_id": task["task_id"],
        "family_task_id": task["family_task_id"],
        "wheel_task_id": task["wheel_task_id"],
        "wheel_task": copy.deepcopy(wheel_task),
        "spec_id": task["spec_id"],
        "family_id": task["family_task_id"],
        "profile_id": task["profile_id"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "python_abi": task["python_abi"],
        "wheel_version": task["wheel_version"],
        "builder": copy.deepcopy(task["builder"]),
        "runtime": copy.deepcopy(task["runtime"]),
        "runtime_patch_variants": copy.deepcopy(runtime_patch_variants),
        "target_repository": task["target_repository"],
        "target_tag": task["target_tag"],
        "required_native": copy.deepcopy(task["required_native"]),
        "forbidden_native": copy.deepcopy(task["forbidden_native"]),
        "allowed_dt_needed": copy.deepcopy(task["allowed_dt_needed"]),
        "external_required_dependencies": copy.deepcopy(
            task["external_required_dependencies"]
        ),
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "runtime_requirements": copy.deepcopy(runtime_requirements),
        "runtime_dependencies": runtime_dependencies,
        "task_sha256": task["task_sha256"],
        "resolved_plan_sha256": resolved_plan_sha256,
        "toolchain": real_image_toolchain_authority(docker_root),
    }
    authority["authority_sha256"] = sha256_value(authority)
    return authority


def real_image_authority_from_plan(
    resolved_plan: dict[str, Any],
    *,
    task_id: str,
    expected_plan_sha256: str,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    """Project one real image authority from an exact frozen-plan selection."""
    task = registry.select_task(
        resolved_plan,
        task_kind="image",
        task_id=task_id,
        expected_plan_sha256=expected_plan_sha256,
    )
    wheel_task = registry.select_task(
        resolved_plan,
        task_kind="wheel",
        task_id=task["wheel_task_id"],
        expected_plan_sha256=expected_plan_sha256,
    )
    return _real_image_authority_from_selected_tasks(
        task,
        wheel_task,
        resolved_plan_sha256=resolved_plan["resolved_plan_sha256"],
        source_repository=resolved_plan["source"]["repository"],
        docker_root=docker_root,
    )


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
    cpu_authority = release_core.cpu_toolchain_authority(
        target_architecture, location="base target platform architecture"
    )
    if target_os != "linux" or target_platform != cpu_authority.oci_platform:
        raise ValueError("base target platform differs from CPU/tool architecture")

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


def _require_real_image_task_authority(task_authority: object) -> dict[str, Any]:
    if not isinstance(task_authority, dict):
        raise ValueError("real image task authority must be an object")
    task = copy.deepcopy(task_authority)
    claimed = task.pop("authority_sha256", None)
    if (
        task.get("kind") != "ucm-real-image-task-authority"
        or claimed != sha256_value(task)
        or task.get("fixture_only") is not False
        or DIGEST_RE.fullmatch(str(task.get("resolved_plan_sha256"))) is None
    ):
        raise ValueError("real image task authority identity is invalid")
    wheel_task = wheel_artifact._validate_wheel_task(task.get("wheel_task"))
    if (
        task.get("wheel_task_id") != wheel_task["task_id"]
        or task.get("spec_id") != wheel_task["spec_id"]
        or task.get("profile_id") != wheel_task["profile_id"]
        or task.get("cpu_arch") != wheel_task["cpu_arch"]
    ):
        raise ValueError("real image task authority has an invalid wheel binding")
    task["authority_sha256"] = claimed
    return task


def validate_real_base_authority(
    base_record: object, task_authority: object
) -> dict[str, Any]:
    """Reopen a real base descriptor chain and bind it to the exact Task 1 member."""
    task = _require_real_image_task_authority(task_authority)
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


def _project_dependency_closure(
    closure: object,
    native_members: object,
    dt_needed: object,
    *,
    normalize_external_locations: bool,
) -> dict[str, Any]:
    """Validate one closure and optionally omit immutable-root-local locations."""
    if (
        not isinstance(closure, dict)
        or not isinstance(native_members, dict)
        or not native_members
        or not isinstance(dt_needed, dict)
    ):
        raise ValueError("dependency closure must be a non-empty object")
    expected_members = set(native_members.values())
    if (
        not all(isinstance(member, str) and member for member in expected_members)
        or set(closure) != expected_members
        or set(dt_needed) != expected_members
    ):
        raise ValueError("dependency closure native member set is not exact")

    projected: dict[str, Any] = {}
    record_fields = {
        "dt_needed",
        "resolved_dependencies",
        "unresolved_dependencies",
    }
    external_fields = {"dependency", "direct", "kind", "path", "sha256"}
    wheel_member_fields = {
        "dependency",
        "direct",
        "kind",
        "member",
        "sha256",
    }
    external_required_fields = {
        "dependency",
        "direct",
        "kind",
        "provider",
        "expected_mount_root",
        "relation",
        "required_at",
    }
    for member in sorted(expected_members):
        record = closure[member]
        expected_needed = dt_needed[member]
        if (
            not isinstance(record, dict)
            or set(record) != record_fields
            or not isinstance(expected_needed, list)
            or not all(isinstance(item, str) and item for item in expected_needed)
            or len(expected_needed) != len(set(expected_needed))
            or record["dt_needed"] != expected_needed
            or record["unresolved_dependencies"] != []
        ):
            raise ValueError(f"dependency closure record is invalid: {member}")
        resolutions = record["resolved_dependencies"]
        if not isinstance(resolutions, list) or not all(
            isinstance(resolution, dict) for resolution in resolutions
        ):
            raise ValueError(f"dependency closure resolutions are invalid: {member}")
        dependencies = [resolution.get("dependency") for resolution in resolutions]
        if not all(
            isinstance(dependency, str) and dependency for dependency in dependencies
        ) or len(dependencies) != len(set(dependencies)):
            raise ValueError(f"dependency closure resolutions are not unique: {member}")
        direct_dependencies = {
            resolution["dependency"]
            for resolution in resolutions
            if resolution.get("direct") is True
        }
        if direct_dependencies != set(expected_needed):
            raise ValueError(f"dependency closure direct dependencies differ: {member}")

        projected_resolutions: list[dict[str, Any]] = []
        for resolution in resolutions:
            dependency = resolution["dependency"]
            direct = resolution.get("direct")
            kind = resolution.get("kind")
            if type(direct) is not bool:
                raise ValueError(
                    f"dependency closure resolution direct flag is invalid: {member}"
                )
            if kind == "external":
                path = resolution.get("path")
                if (
                    set(resolution) != external_fields
                    or not isinstance(path, str)
                    or not PurePosixPath(path).is_absolute()
                    or DIGEST_RE.fullmatch(str(resolution.get("sha256"))) is None
                ):
                    raise ValueError(
                        f"dependency closure external resolution is invalid: {dependency}"
                    )
                if normalize_external_locations:
                    projected_resolutions.append(
                        {
                            "dependency": dependency,
                            "direct": direct,
                            "kind": kind,
                        }
                    )
                else:
                    projected_resolutions.append(copy.deepcopy(resolution))
            elif kind == "wheel-member":
                wheel_member = resolution.get("member")
                if (
                    set(resolution) != wheel_member_fields
                    or wheel_member not in expected_members
                    or DIGEST_RE.fullmatch(str(resolution.get("sha256"))) is None
                ):
                    raise ValueError(
                        "dependency closure wheel-member resolution is invalid: "
                        f"{dependency}"
                    )
                projected_resolutions.append(copy.deepcopy(resolution))
            elif kind == "virtual":
                if set(resolution) != {"dependency", "direct", "kind"} or (
                    dependency != "linux-vdso.so.1"
                ):
                    raise ValueError(
                        f"dependency closure virtual resolution is invalid: {dependency}"
                    )
                projected_resolutions.append(copy.deepcopy(resolution))
            elif kind == "external-required":
                mount_root = resolution.get("expected_mount_root")
                if (
                    set(resolution) != external_required_fields
                    or direct is not False
                    or not isinstance(resolution.get("provider"), str)
                    or not resolution["provider"]
                    or not isinstance(mount_root, str)
                    or not PurePosixPath(mount_root).is_absolute()
                    or resolution.get("relation") != "transitive"
                    or resolution.get("required_at") != "device-runtime"
                ):
                    raise ValueError(
                        "dependency closure external-required resolution is invalid: "
                        f"{dependency}"
                    )
                projected_resolutions.append(copy.deepcopy(resolution))
            else:
                raise ValueError(
                    f"dependency closure resolution kind is invalid: {dependency}"
                )
        projected[member] = {
            "dt_needed": copy.deepcopy(record["dt_needed"]),
            "resolved_dependencies": projected_resolutions,
            "unresolved_dependencies": [],
        }
    return projected


def _matches_python_command(value: object, python_abi: object) -> bool:
    if (
        not isinstance(value, str)
        or not isinstance(python_abi, str)
        or re.fullmatch(r"cp[0-9]{2,}", python_abi) is None
    ):
        return False
    digits = python_abi.removeprefix("cp")
    version = f"{digits[0]}.{digits[1:]}"
    return PurePosixPath(value).name in {"python", "python3", f"python{version}"}


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
    runtime_dependencies = payload.get("runtime_dependencies")
    if (
        not isinstance(wheel, dict)
        or not isinstance(runtime_dependencies, list)
        or not runtime_dependencies
        or any(not isinstance(value, dict) for value in runtime_dependencies)
    ):
        raise ValueError("real recipe wheel authority is missing")
    if install.get("kind") == "ucm-real-install-result" and (
        install.get("wheel_filename") != wheel.get("filename")
        or install.get("wheel_sha256") != wheel.get("sha256")
        or install.get("runtime_dependencies") != runtime_dependencies
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
        or not _matches_python_command(preinstall_command[0], wheel.get("python_abi"))
    ):
        raise ValueError("real preinstall purge is not the exact reviewed command")
    command = install.get("pip_command")
    expected_command = payload.get("dependency_lock", {}).get("pip_command")
    if command is not None and (
        not isinstance(command, list)
        or not isinstance(expected_command, list)
        or command[1:] != expected_command[1:]
        or not _matches_python_command(command[0], wheel.get("python_abi"))
    ):
        raise ValueError("real pip command is not the exact offline hashed install")
    expected_packages = {"uc-manager": wheel.get("version")}
    expected_packages.update(
        {record["name"]: record.get("version") for record in runtime_dependencies}
    )
    if install.get("installed_packages") != expected_packages:
        raise ValueError("real installed package versions do not match")
    expected_imports = {"ucm": "passed"}
    expected_imports.update(
        {record["import_name"]: "passed" for record in runtime_dependencies}
    )
    if install.get("imports") != expected_imports:
        raise ValueError("real import gate did not pass")
    direct_urls = install.get("direct_urls")
    if not isinstance(direct_urls, dict):
        raise ValueError("real direct_url evidence is missing")
    expected_direct = {
        "uc-manager": (wheel.get("filename"), wheel.get("sha256")),
    }
    expected_direct.update(
        {
            record["name"]: (record.get("filename"), record.get("sha256"))
            for record in runtime_dependencies
        }
    )
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
    expected_variants = payload.get("runtime_patch_variants")
    if (
        not isinstance(expected_variants, dict)
        or not expected_variants
        or runtime.get("runtime_patch_variants") != expected_variants
    ):
        raise ValueError("real runtime patch variant map differs from recipe")
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
    builder_coordinate = expected_native.get("builder_coordinate")
    base = payload.get("base")
    base_subject = base.get("subject") if isinstance(base, dict) else None
    if (
        not isinstance(builder_coordinate, str)
        or not isinstance(base_subject, str)
        or "@" not in builder_coordinate
        or "@" not in base_subject
        or DIGEST_RE.fullmatch(builder_coordinate.rsplit("@", 1)[1]) is None
        or DIGEST_RE.fullmatch(base_subject.rsplit("@", 1)[1]) is None
    ):
        raise ValueError("dependency closure immutable root authority is invalid")
    same_root = builder_coordinate == base_subject
    expected_closure = _project_dependency_closure(
        expected_native.get("dependency_closure"),
        expected_native.get("native_members"),
        expected_native.get("dt_needed"),
        normalize_external_locations=not same_root,
    )
    runtime_closure = _project_dependency_closure(
        runtime.get("dependency_closure"),
        runtime.get("native_members"),
        runtime.get("dt_needed"),
        normalize_external_locations=not same_root,
    )
    if runtime_closure != expected_closure:
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
        "runtime_dependency_imports": "passed",
        "abi": "passed",
        "native_members": "passed",
        "elf": "passed",
        "dependency_closure": "passed",
        "variant": "passed",
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
        or re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            str(source.get("repository")),
        )
        is None
        or source.get("repository_url")
        != f"https://github.com/{source.get('repository')}"
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
            "org.opencontainers.image.source": source.get("repository_url"),
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
        registry._validate_layer_descriptor_annotations(
            layer, created=created, label=f"real OCI layer {position}"
        )
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


def validate_image_result(
    result: object,
    *,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    resolved_plan: dict[str, Any] | None = None,
    expected_plan_sha256: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Reopen a canonical image result and revalidate its embedded byte chains."""
    if not isinstance(result, dict):
        raise ValueError("image result must be an object")
    selected_task: dict[str, Any] | None = None
    if result.get("candidate_kind") == "real-candidate":
        if (
            not isinstance(resolved_plan, dict)
            or not isinstance(expected_plan_sha256, str)
            or not isinstance(task_id, str)
            or not task_id
        ):
            raise ValueError(
                "real image result requires its frozen resolved plan, hash, and task ID"
            )
        selected_task = registry.select_task(
            resolved_plan,
            task_kind="image",
            task_id=task_id,
            expected_plan_sha256=expected_plan_sha256,
        )
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
        assert selected_task is not None
        task = real_image_authority_from_plan(
            resolved_plan,
            task_id=task_id,
            expected_plan_sha256=expected_plan_sha256,
        )
        if (
            result.get("fixture_only") is not False
            or result.get("unpublished") is not True
            or result.get("publication_attempted") is not False
            or result.get("task_id") != selected_task["task_id"]
            or result.get("family_task_id") != selected_task["family_task_id"]
            or result.get("task_sha256") != selected_task["task_sha256"]
            or result.get("resolved_plan_sha256")
            != resolved_plan["resolved_plan_sha256"]
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
        layers = content.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ValueError("real image result content identity layers are invalid")
        for position, layer in enumerate(layers):
            if not isinstance(layer, dict):
                raise ValueError(
                    f"real image result content identity layer {position} is invalid"
                )
            registry._validate_layer_descriptor_annotations(
                layer,
                created=content.get("created"),
                label=f"real image result content identity layer {position}",
            )
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
