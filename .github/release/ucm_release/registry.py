"""Read-only OCI registry discovery and deterministic image reconciliation."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from . import core
from .core import (
    DEFAULT_SCHEMA_DIR,
    canonical_bytes,
    load_json,
    sha256_value,
    validate_schema,
)

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
VERSION = r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?"
OCI_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[._:-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
RESOLVED_TASK_FIELDS = {
    "wheel": {
        "task_id",
        "spec_id",
        "profile_id",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_version",
        "python_abi",
        "wheel_version",
        "wheel_platform",
        "binary_profile_id",
        "validation_targets",
        "required_native",
        "forbidden_native",
        "allowed_dt_needed",
        "external_required_dependencies",
        "declaration_sha256",
        "runner",
        "platform",
        "builder",
        "builder_sha256",
        "build",
        "dependency_lock_sha256",
        "dependency_lock",
        "runtime_requirements",
        "runtime_patch_manifest",
        "runtime_patch_manifest_sha256",
        "write_authority",
        "build_eligible",
        "artifact_name",
        "task_sha256",
    },
    "image": {
        "task_id",
        "family_task_id",
        "wheel_task_id",
        "spec_id",
        "profile_id",
        "compatibility_rule_id",
        "runtime_patch_rule_id",
        "runtime_patch_product",
        "runtime_patch_strategy",
        "runtime_patch_variants",
        "runner",
        "cpu_arch",
        "platform",
        "builder",
        "builder_sha256",
        "build",
        "runtime",
        "runtime_sha256",
        "target_repository",
        "target_tag",
        "python_abi",
        "python_version",
        "wheel_version",
        "wheel_platform",
        "required_native",
        "forbidden_native",
        "allowed_dt_needed",
        "external_required_dependencies",
        "dependency_lock_sha256",
        "dependency_lock",
        "runtime_requirements",
        "runtime_patch_manifest_sha256",
        "write_authority",
        "build_eligible",
        "artifact_name",
        "wheel_artifact_name",
        "task_sha256",
    },
    "family": {
        "task_id",
        "product_id",
        "control_task_id",
        "control_arch",
        "control_runner",
        "runner",
        "cpu_arch",
        "platform",
        "builder",
        "builder_sha256",
        "runtime",
        "runtime_sha256",
        "snapshot_sha256",
        "target_repository",
        "target_tag",
        "image_task_ids",
        "wheel_task_ids",
        "member_set_sha256",
        "write_authority",
        "artifact_name",
        "task_sha256",
    },
}
# Legacy Task 3 regression authority. Production resolution starts at
# ``resolve_catalog`` and must never consume these concrete fixture coordinates.
FIXTURE_TARGET_REPOSITORIES = {
    "vllm-openai": "ghcr.io/modelengine-group/vllm-openai",
    "vllm-ascend": "ghcr.io/modelengine-group/vllm-ascend",
}
FIXTURE_UPSTREAM_REPOSITORIES = {
    "vllm-openai": "docker.io/vllm/vllm-openai",
    "vllm-ascend": "quay.io/ascend/vllm-ascend",
}
SNAPSHOT_KEYS = {
    "schema_version",
    "kind",
    "repository",
    "upstream_tag",
    "index_digest",
    "platforms",
}
PLATFORM_KEYS = {
    "os",
    "architecture",
    "manifest_digest",
    "config_digest",
}
INVENTORY_KEYS = {
    "schema_version",
    "kind",
    "repositories",
    "entries",
    "inventory_sha256",
}
ENTRY_KEYS = {
    "repository",
    "tag",
    "build_key_sha256",
    "observed_digest",
    "evidence_digest",
}
CANDIDATE_KEYS = {
    "schema_version",
    "kind",
    "fixture_only",
    "unpublished",
    "ucm_version",
    "target_repository",
    "tag_base",
    "tag_family_sha256",
    "build_key_sha256",
    "build_inputs",
}
BUILD_INPUT_KEYS = {
    "release_manifest_sha256",
    "wheel",
    "upstream",
    "compatibility_rule_id",
    "compatibility_rule",
    "compatibility_rule_sha256",
    "implementation_digest",
}
WHEEL_INPUT_KEYS = {
    "spec_id",
    "sha256",
    "declaration_sha256",
    "version",
    "accelerator",
    "accelerator_runtime",
    "npu_arch_or_na",
    "os",
    "cpu_arch",
    "python_abi",
    "binary_profile_id",
}
UPSTREAM_INPUT_KEYS = {
    "repository",
    "exact_upstream_tag",
    "index_digest",
    "platforms",
}
COMMON_WHEEL_RECORD_KEYS = {
    "schema_version",
    "kind",
    "source_kind",
    "spec_id",
    "filename",
    "sha256",
    "size",
    "distribution",
    "version",
    "tags",
    "requires_dist",
    "python_abi",
    "cpu_arch",
    "declaration_sha256",
    "status",
    "trust_level",
    "published",
    "publication_eligible",
}
WHEEL_RECORD_KEYS_BY_SOURCE = {
    "fixture": COMMON_WHEEL_RECORD_KEYS | {"fixture_binding"},
    "builder-candidate": COMMON_WHEEL_RECORD_KEYS | {"builder_evidence"},
}
COMPATIBILITY_RULE_KEYS = {
    "id",
    "upstream_products",
    "version_specifier",
    "variants",
    "accelerator",
    "accelerator_runtimes",
    "npu_architectures",
    "operating_systems",
    "cpu_architectures",
    "python_abis",
    "upstream_channels",
}
OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
FIXTURE_STAGING_REPOSITORY = "ghcr.io/release-org/ucm-release-staging"
CRANE_VERSION = "0.20.3"
SECONDARY_RATE_LIMIT_BACKOFF_SECONDS = (60.0, 120.0, 240.0)
IDEMPOTENT_REGISTRY_READ_OPERATIONS = frozenset(
    {"blob", "digest", "ls", "manifest", "validate"}
)
SECONDARY_RATE_LIMIT_MARKERS = (
    "you have exceeded a secondary rate limit",
    "you have triggered an abuse detection mechanism",
)
CRANE_BINARY_SHA256 = {
    (
        "linux",
        "x86_64",
    ): "sha256:675f3b2f1696c1f6bc55b1ef535163364119776999f3d1471e4558ed35bab548",
    (
        "linux",
        "aarch64",
    ): "sha256:34bdb2ae7a56139c69cf745ab5cad3d7368e69896d8980e7bcf1ca194854a2ef",
    (
        "darwin",
        "arm64",
    ): "sha256:d34f51061a226d1b183480cc7fdc1f7ec410676445cbb2432d89900ac2eb1cb3",
}
MEMBER_RECORD_KEYS = {
    "schema_version",
    "kind",
    "status",
    "spec_id",
    "profile_id",
    "family_id",
    "platform",
    "target_repository",
    "target_tag",
    "staging_repository",
    "staging_visibility",
    "staging_tag",
    "candidate_task_sha256",
    "publication_task_sha256",
    "build_key_sha256",
    "wheel_sha256",
    "member_digest",
    "member_size",
    "config_digest",
    "annotations",
    "source_sha",
    "image_result_sha256",
    "recipe_sha256",
    "content_identity_sha256",
    "content_identity",
    "manifest",
    "config",
    "layers",
    "readback_sha256",
    "prewrite_visibility_evidence_sha256",
    "visibility_evidence_sha256",
    "collision_model",
    "operations",
    "record_sha256",
}
INDEX_RECORD_KEYS = {
    "schema_version",
    "kind",
    "status",
    "source_sha",
    "family_id",
    "target_repository",
    "target_tag",
    "index_build_key_sha256",
    "index_digest",
    "manifest_sha256",
    "member_digests",
    "authenticated_readback_sha256",
    "authenticated_closure_sha256",
    "anonymous_readback_sha256",
    "anonymous_closure_sha256",
    "collision_model",
    "operations",
    "record_sha256",
}
MEMBER_ANNOTATION_KEYS = {
    "io.ucm.release.build-key-sha256",
    "io.ucm.release.candidate-task-sha256",
    "io.ucm.release.family-id",
    "io.ucm.release.platform",
    "io.ucm.release.spec-id",
    "io.ucm.release.wheel-sha256",
}
CONTENT_IDENTITY_KEYS = {
    "manifest_digest",
    "config_digest",
    "layers",
    "diff_ids",
    "annotations",
    "labels",
    "created",
    "history",
    "source",
    "task_sha256",
    "build_key_sha256",
    "wheel_sha256",
    "recipe_sha256",
    "content_identity_sha256",
}
CONTENT_IDENTITY_SOURCE_KEYS = {
    "repository",
    "repository_url",
    "commit",
    "tree",
    "archive_sha256",
    "context_sha256",
}
CONTENT_IDENTITY_LAYER_KEYS = {"mediaType", "digest", "size"}
BUILDKIT_REWRITTEN_TIMESTAMP_ANNOTATION = "buildkit/rewritten-timestamp"


class RegistryBlocker(ValueError):
    """A known fail-closed loop blocker with a stable evidence code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _created_epoch(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
        )
        is None
    ):
        raise ValueError(f"{label} created timestamp is invalid")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError as error:
        raise ValueError(f"{label} created timestamp is invalid") from error
    return str(int(parsed.timestamp()))


def _validate_layer_descriptor_annotations(
    descriptor: dict[str, Any], *, created: object, label: str
) -> dict[str, str] | None:
    """Validate the pinned release producer contract, not generic OCI metadata."""
    allowed_keys = CONTENT_IDENTITY_LAYER_KEYS | {"annotations"}
    if set(descriptor) not in (CONTENT_IDENTITY_LAYER_KEYS, allowed_keys):
        raise ValueError(f"{label} descriptor fields are invalid")
    if "annotations" not in descriptor:
        return None
    annotations = descriptor["annotations"]
    if not isinstance(annotations, dict) or set(annotations) != {
        BUILDKIT_REWRITTEN_TIMESTAMP_ANNOTATION
    }:
        raise ValueError(f"{label} descriptor annotations are invalid")
    timestamp = annotations[BUILDKIT_REWRITTEN_TIMESTAMP_ANNOTATION]
    if (
        not isinstance(timestamp, str)
        or re.fullmatch(r"(?:0|[1-9][0-9]*)", timestamp) is None
    ):
        raise ValueError(f"{label} rewritten timestamp annotation is invalid")
    if timestamp != _created_epoch(created, label):
        raise ValueError(f"{label} rewritten timestamp differs from created")
    return copy.deepcopy(annotations)


def _collision_model_evidence() -> dict[str, Any]:
    """Describe observed-state checks without claiming unavailable Registry tag CAS."""
    return {
        "model": "observed-state-fail-closed",
        "in_system_serialization": "repository-concurrency",
        "fresh_prewrite_read": True,
        "exact_postwrite_readback": True,
        "external_admin_atomicity": "unavailable",
    }


def _validate_collision_model(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} collision model must be an object")
    _exact_keys(value, set(_collision_model_evidence()), f"{label} collision model")
    if (
        value["model"] != "observed-state-fail-closed"
        or value["in_system_serialization"] != "repository-concurrency"
        or value["fresh_prewrite_read"] is not True
        or value["exact_postwrite_readback"] is not True
        or value["external_admin_atomicity"] != "unavailable"
    ):
        raise ValueError(
            f"{label} collision model must disclose the Registry CAS boundary"
        )
    return copy.deepcopy(value)


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{label} fields mismatch: missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be an immutable sha256:<64 lowercase hex> digest"
        )
    return value


def _repository(value: object) -> str:
    if not isinstance(value, str) or REPOSITORY_RE.fullmatch(value) is None:
        raise ValueError(
            "repository must be a canonical lowercase OCI repository without tag or digest"
        )
    return value


def _fixture_product_for_repository(repository: str) -> str:
    product = repository.rsplit("/", 1)[-1]
    if FIXTURE_UPSTREAM_REPOSITORIES.get(product) != repository:
        raise ValueError(f"unsupported exact upstream repository: {repository}")
    return product


def parse_fixture_upstream_tag(product: str, tag: str) -> dict[str, str]:
    """Parse only canonical stable/RC tags and the supported Ascend suffixes."""
    if product not in FIXTURE_TARGET_REPOSITORIES or not isinstance(tag, str):
        raise ValueError("product must be vllm-openai or vllm-ascend")
    if product == "vllm-openai":
        match = re.fullmatch(f"({VERSION})", tag)
        if match is None:
            raise ValueError(f"unsupported vllm-openai upstream tag: {tag}")
        npu_arch = "na"
        operating_system = "ubuntu-22.04"
    else:
        match = re.fullmatch(f"({VERSION})(-a3)?(-openeuler)?", tag)
        if match is None:
            raise ValueError(f"unsupported vllm-ascend upstream tag: {tag}")
        npu_arch = "a3" if match.group(2) else "a2"
        operating_system = "openEuler-24.03" if match.group(3) else "ubuntu-22.04"
    version = match.group(1)
    return {
        "product": product,
        "exact_upstream_tag": tag,
        "upstream_version": version,
        "channel": "rc" if "rc" in version else "stable",
        "npu_arch": npu_arch,
        "operating_system": operating_system,
        "target_repository": FIXTURE_TARGET_REPOSITORIES[product],
    }


def validate_fixture_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Require one immutable index with exact linux/amd64 and linux/arm64 chains."""
    if not isinstance(snapshot, dict):
        raise ValueError("registry snapshot must be an object")
    _exact_keys(snapshot, SNAPSHOT_KEYS, "registry snapshot")
    if (
        snapshot["schema_version"] != 1
        or snapshot["kind"] != "upstream-registry-snapshot"
    ):
        raise ValueError("registry snapshot identity must be schema version 1")
    repository = _repository(snapshot["repository"])
    parse_fixture_upstream_tag(
        _fixture_product_for_repository(repository), snapshot["upstream_tag"]
    )
    index_digest = _digest(snapshot["index_digest"], "snapshot index")
    platforms = snapshot["platforms"]
    if not isinstance(platforms, list):
        raise ValueError("snapshot platforms must be an array")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, platform_entry in enumerate(platforms):
        if not isinstance(platform_entry, dict):
            raise ValueError(f"snapshot platform {index} must be an object")
        _exact_keys(platform_entry, PLATFORM_KEYS, f"snapshot platform {index}")
        if platform_entry["os"] not in {"linux"} or platform_entry[
            "architecture"
        ] not in {
            "amd64",
            "arm64",
        }:
            raise ValueError("snapshot platform must be linux/amd64 or linux/arm64")
        identity = (platform_entry["os"], platform_entry["architecture"])
        if identity in seen:
            raise ValueError(
                f"duplicate snapshot platform: {identity[0]}/{identity[1]}"
            )
        seen.add(identity)
        normalized.append(
            {
                "os": platform_entry["os"],
                "architecture": platform_entry["architecture"],
                "manifest_digest": _digest(
                    platform_entry["manifest_digest"], f"{identity} manifest"
                ),
                "config_digest": _digest(
                    platform_entry["config_digest"], f"{identity} config"
                ),
            }
        )
    required = {("linux", "amd64"), ("linux", "arm64")}
    if seen != required:
        missing = sorted(required - seen)
        extra = sorted(seen - required)
        if missing == [("linux", "arm64")] and not extra:
            raise RegistryBlocker(
                "missing-linux-arm64",
                "snapshot is missing required linux/arm64 platform",
            )
        raise ValueError(
            f"snapshot requires exact linux platforms; missing={missing}, extra={extra}"
        )
    all_digests = [
        index_digest,
        *[
            item[key]
            for item in normalized
            for key in ("manifest_digest", "config_digest")
        ],
    ]
    if len(all_digests) != len(set(all_digests)):
        raise ValueError("snapshot digest chain contains duplicate mutable identities")
    normalized.sort(key=lambda item: item["architecture"])
    result = copy.deepcopy(snapshot)
    result["platforms"] = normalized
    return result


def _unique_json(text: str, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _crane_binary(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        if not path.is_file():
            raise ValueError(f"crane executable does not exist: {value}")
        return value
    if "/" in value or re.fullmatch(r"[A-Za-z0-9_.+-]+", value) is None:
        raise ValueError(
            "crane executable must be an absolute path or an explicit PATH name"
        )
    return value


def _host_platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    cpu_authority = core.host_cpu_toolchain_authority(platform.machine())
    machine = (
        "arm64"
        if system == "darwin" and cpu_authority.cpu_arch == "arm64"
        else cpu_authority.wheel_arch
    )
    return system, machine


def _minimal_registry_environment(
    *, docker_config: str | None = None
) -> dict[str, str]:
    environment: dict[str, str] = {}
    selected_config = docker_config or os.environ.get("DOCKER_CONFIG")
    if selected_config:
        environment["DOCKER_CONFIG"] = selected_config
    for key in (
        "HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


def resolve_pinned_crane() -> str:
    """Resolve the reviewed crane release and reject path/version byte drift."""
    located = shutil.which("crane")
    if located is None:
        raise ValueError("pinned crane v0.20.3 is not installed on PATH")
    executable = str(Path(located).resolve())
    result = subprocess.run(
        [executable, "version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_minimal_registry_environment(),
        check=False,
    )
    version = result.stdout.strip()
    if result.returncode != 0 or version not in {CRANE_VERSION, "v" + CRANE_VERSION}:
        raise ValueError(f"registry transport requires crane v0.20.3, got {version!r}")
    expected = CRANE_BINARY_SHA256.get(_host_platform_key())
    if expected is None:
        raise ValueError(f"unsupported crane host platform: {_host_platform_key()}")
    observed = "sha256:" + hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    if observed != expected:
        raise ValueError(
            f"crane binary digest mismatch: expected {expected}, observed {observed}"
        )
    return executable


def _stream_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def resolve_pinned_buildx() -> str:
    """Resolve the reviewed standalone Buildx v0.19.2 plugin by fixed path/bytes."""
    from . import image

    authority = image.real_image_toolchain_authority()
    if authority["buildx_version"] != "v0.19.2":
        raise ValueError("image toolchain authority must require Buildx v0.19.2")
    if platform.system().lower() != "linux":
        raise ValueError("production Buildx publication requires a Linux runner")
    architecture = core.host_cpu_toolchain_authority(platform.machine()).cpu_arch
    home = os.environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise ValueError("production Buildx resolution requires an absolute HOME")
    configured = Path(home) / ".docker" / "cli-plugins" / "docker-buildx"
    try:
        executable = configured.resolve(strict=True)
    except OSError as error:
        raise ValueError(
            f"pinned Buildx plugin is unavailable: {configured}"
        ) from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("pinned Buildx plugin must be one executable regular file")
    expected = authority["buildx_linux_sha256"][architecture]
    observed = _stream_file_sha256(executable)
    if observed != expected:
        raise ValueError(
            f"Buildx binary digest mismatch: expected {expected}, observed {observed}"
        )
    try:
        result = subprocess.run(
            [str(executable), "version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_minimal_registry_environment(),
            check=False,
        )
    except OSError as error:
        raise ValueError(f"failed to execute pinned Buildx plugin: {error}") from error
    fields = result.stdout.strip().split()
    if (
        result.returncode != 0
        or len(fields) < 2
        or fields[0] != "github.com/docker/buildx"
        or fields[1] != authority["buildx_version"]
    ):
        raise ValueError(
            "registry index transport requires Buildx v0.19.2, got "
            f"{result.stdout.strip()!r}"
        )
    return str(executable)


def _resolve_loopback_buildx() -> str:
    """Resolve an internally selected v0.19.2 standalone plugin for local loopback."""
    if platform.system().lower() == "linux":
        return resolve_pinned_buildx()
    home = os.environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise ValueError("loopback Buildx resolution requires an absolute HOME")
    configured = Path(home) / ".docker" / "cli-plugins" / "docker-buildx"
    try:
        executable = configured.resolve(strict=True)
    except OSError as error:
        raise ValueError(
            f"loopback Buildx plugin is unavailable: {configured}"
        ) from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("loopback Buildx plugin must be one executable regular file")
    result = subprocess.run(
        [str(executable), "version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_minimal_registry_environment(),
        check=False,
    )
    fields = result.stdout.strip().split()
    if (
        result.returncode != 0
        or len(fields) < 2
        or fields[0] != "github.com/docker/buildx"
        or not fields[1].startswith("v0.19.2")
    ):
        raise ValueError(
            "loopback contract requires a Buildx v0.19.2 standalone plugin"
        )
    return str(executable)


def _crane(crane_binary: str, operation: str, reference: str) -> str:
    if operation not in {"digest", "manifest"}:
        raise ValueError(
            "only read-only crane digest and manifest operations are allowed"
        )
    try:
        result = subprocess.run(
            [_crane_binary(crane_binary), operation, reference],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_minimal_registry_environment(),
            check=False,
        )
    except OSError as error:
        raise ValueError(f"failed to execute pinned crane binary: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ValueError(f"crane {operation} failed for {reference}: {detail}")
    return result.stdout.strip()


def enumerate_repository_tags(
    repository: str,
    *,
    fixture: dict[str, Any] | None = None,
    max_tags: int,
) -> dict[str, Any]:
    """Enumerate one complete repository tag set through the pinned read transport."""
    repository = _repository(repository)
    if not isinstance(max_tags, int) or isinstance(max_tags, bool) or max_tags < 1:
        raise ValueError("max_tags must be a positive integer")
    operations: list[dict[str, Any]] = []
    tags: list[str] = []
    if fixture is not None:
        if not isinstance(fixture, dict) or set(fixture) not in (
            {"pages", "snapshots"},
            {"pages", "snapshots", "list_error"},
        ):
            raise ValueError("registry enumeration fixture fields are noncanonical")
        if "list_error" in fixture:
            detail = fixture["list_error"]
            if not isinstance(detail, str) or not detail:
                raise ValueError("registry fixture list_error must be non-empty")
            raise ValueError(f"fixture tag listing failed: {detail}")
        pages = fixture["pages"]
        if not isinstance(pages, list) or not pages:
            raise ValueError("registry enumeration fixture requires complete pages")
        for page_index, page in enumerate(pages, start=1):
            if not isinstance(page, dict) or set(page) != {"tags", "next_page"}:
                raise ValueError(f"registry fixture page {page_index} is malformed")
            page_tags = page["tags"]
            if not isinstance(page_tags, list) or any(
                not isinstance(tag, str) or OCI_TAG_RE.fullmatch(tag) is None
                for tag in page_tags
            ):
                raise ValueError(
                    f"registry fixture page {page_index} tags are malformed"
                )
            expected_next = (
                None if page_index == len(pages) else f"page-{page_index + 1}"
            )
            if page["next_page"] != expected_next:
                raise ValueError(
                    "registry enumeration fixture pagination is incomplete"
                )
            tags.extend(page_tags)
            operations.append(
                {
                    "type": "fixture-tag-page-read",
                    "capability": "read",
                    "reference": repository,
                    "page": page_index,
                }
            )
    else:
        crane_binary = resolve_pinned_crane()
        result = _run_registry_tool(crane_binary, ["ls", repository])
        tags = result.stdout.splitlines()
        if any(OCI_TAG_RE.fullmatch(tag) is None for tag in tags):
            raise ValueError("crane ls returned a malformed OCI tag")
        operations.append(
            {
                "type": "crane-tag-list",
                "capability": "read",
                "reference": repository,
            }
        )
    normalized = sorted(set(tags))
    if len(normalized) > max_tags:
        raise ValueError(
            f"registry tag limit max_tags={max_tags} exceeded by exact set of "
            f"{len(normalized)}"
        )
    return {
        "schema_version": 1,
        "kind": "registry-tag-list",
        "repository": repository,
        "tags": normalized,
        "operations": operations,
    }


def resolve_repository_tag(
    repository: str,
    upstream_tag: str,
    *,
    required_architectures: list[str],
    fixture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one configured tag to an exact index/member/config digest chain."""
    repository = _repository(repository)
    if not isinstance(upstream_tag, str) or OCI_TAG_RE.fullmatch(upstream_tag) is None:
        raise ValueError("upstream tag must use canonical OCI tag syntax")
    if (
        not isinstance(required_architectures, list)
        or not 1 <= len(required_architectures) <= 64
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", item) is None
            for item in required_architectures
        )
        or len(required_architectures) != len(set(required_architectures))
    ):
        raise ValueError("required architectures must be a bounded unique array")
    operations: list[dict[str, Any]] = []
    tagged_reference = f"{repository}:{upstream_tag}"
    if fixture is not None:
        raw_snapshot = copy.deepcopy(fixture)
        operations.append(
            {
                "type": "fixture-snapshot-read",
                "capability": "read",
                "reference": tagged_reference,
            }
        )
    else:
        crane_binary = resolve_pinned_crane()
        operations.append(
            {
                "type": "crane-digest",
                "capability": "read",
                "reference": tagged_reference,
            }
        )
        digest_result = _run_registry_tool(crane_binary, ["digest", tagged_reference])
        index_digest = _digest(digest_result.stdout.strip(), "crane index")
        index_reference = f"{repository}@{index_digest}"
        operations.append(
            {
                "type": "crane-manifest",
                "capability": "read",
                "reference": index_reference,
            }
        )
        index_result = _run_registry_tool(crane_binary, ["manifest", index_reference])
        index = _unique_json(index_result.stdout, "crane index")
        if index.get("mediaType") not in OCI_INDEX_MEDIA_TYPES:
            raise ValueError("resolved index digest did not return an OCI/Docker index")
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list):
            raise ValueError("crane index must contain a manifests array")
        platforms: list[dict[str, Any]] = []
        for descriptor in descriptors:
            if not isinstance(descriptor, dict) or not isinstance(
                descriptor.get("platform"), dict
            ):
                raise ValueError("crane index descriptors require a platform object")
            if descriptor.get("mediaType") not in OCI_MANIFEST_MEDIA_TYPES:
                raise ValueError(
                    "index platform descriptor is not an OCI/Docker manifest"
                )
            platform_value = descriptor["platform"]
            manifest_digest = _digest(descriptor.get("digest"), "platform manifest")
            child_reference = f"{repository}@{manifest_digest}"
            operations.append(
                {
                    "type": "crane-manifest",
                    "capability": "read",
                    "reference": child_reference,
                }
            )
            child_result = _run_registry_tool(
                crane_binary, ["manifest", child_reference]
            )
            child = _unique_json(
                child_result.stdout, f"platform manifest {manifest_digest}"
            )
            if child.get("mediaType") != descriptor.get("mediaType"):
                raise ValueError(
                    "platform manifest media type does not match index descriptor"
                )
            config = child.get("config")
            if not isinstance(config, dict):
                raise ValueError("platform manifest requires a config descriptor")
            platforms.append(
                {
                    "os": platform_value.get("os"),
                    "architecture": platform_value.get("architecture"),
                    "manifest_digest": manifest_digest,
                    "config_digest": _digest(config.get("digest"), "platform config"),
                }
            )
        raw_snapshot = {
            "schema_version": 1,
            "kind": "upstream-registry-snapshot",
            "repository": repository,
            "upstream_tag": upstream_tag,
            "index_digest": index_digest,
            "platforms": platforms,
        }
    if not isinstance(raw_snapshot, dict):
        raise ValueError("registry snapshot must be an object")
    _exact_keys(raw_snapshot, SNAPSHOT_KEYS, "registry snapshot")
    if (
        raw_snapshot["schema_version"] != 1
        or raw_snapshot["kind"] != "upstream-registry-snapshot"
        or raw_snapshot["repository"] != repository
        or raw_snapshot["upstream_tag"] != upstream_tag
    ):
        raise ValueError("registry snapshot identity differs from exact request")
    index_digest = _digest(raw_snapshot["index_digest"], "snapshot index")
    platform_values = raw_snapshot["platforms"]
    if not isinstance(platform_values, list):
        raise ValueError("snapshot platforms must be an array")
    members: dict[str, dict[str, str]] = {}
    digest_chain = [index_digest]
    for index, member in enumerate(platform_values):
        if not isinstance(member, dict):
            raise ValueError(f"snapshot platform {index} must be an object")
        _exact_keys(member, PLATFORM_KEYS, f"snapshot platform {index}")
        architecture = member["architecture"]
        if member["os"] != "linux" or architecture not in required_architectures:
            raise ValueError("snapshot contains an unselected platform")
        if architecture in members:
            raise ValueError(f"duplicate snapshot platform: linux/{architecture}")
        manifest_digest = _digest(
            member["manifest_digest"], f"linux/{architecture} manifest"
        )
        config_digest = _digest(member["config_digest"], f"linux/{architecture} config")
        digest_chain.extend((manifest_digest, config_digest))
        members[architecture] = {
            "manifest_digest": manifest_digest,
            "config_digest": config_digest,
        }
    missing = sorted(set(required_architectures) - set(members))
    if missing:
        raise RegistryBlocker(
            f"missing-linux-{missing[0]}",
            f"snapshot is missing required linux architectures: {missing}",
        )
    if len(digest_chain) != len(set(digest_chain)):
        raise ValueError("snapshot digest chain contains duplicate mutable identities")
    return {
        "schema_version": 1,
        "kind": "registry-scan-result",
        "fixture_only": fixture is not None,
        "snapshot": {
            "repository": repository,
            "tag": upstream_tag,
            "index_digest": index_digest,
            "members": {key: members[key] for key in sorted(members)},
        },
        "operations": operations,
    }


def read_repository_tag_digest(
    repository: str,
    upstream_tag: str,
    *,
    fixture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform a fresh read of one mutable tag for protected drift verification."""
    repository = _repository(repository)
    if not isinstance(upstream_tag, str) or OCI_TAG_RE.fullmatch(upstream_tag) is None:
        raise ValueError("upstream tag must use canonical OCI tag syntax")
    reference = f"{repository}:{upstream_tag}"
    if fixture is not None:
        if (
            not isinstance(fixture, dict)
            or fixture.get("repository") != repository
            or fixture.get("upstream_tag") != upstream_tag
        ):
            raise ValueError("fresh digest fixture differs from exact request")
        digest = _digest(fixture.get("index_digest"), "fresh fixture tag")
        operation_type = "fixture-fresh-digest-read"
    else:
        crane_binary = resolve_pinned_crane()
        result = _run_registry_tool(crane_binary, ["digest", reference])
        digest = _digest(result.stdout.strip(), "fresh Registry tag")
        operation_type = "crane-fresh-digest-read"
    return {
        "repository": repository,
        "tag": upstream_tag,
        "index_digest": digest,
        "operation": {
            "type": operation_type,
            "capability": "read",
            "reference": reference,
        },
    }


def scan_fixture_registry(
    repository: str,
    upstream_tag: str,
    *,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """Validate one legacy Task 3 snapshot fixture without Registry access."""
    repository = _repository(repository)
    parse_fixture_upstream_tag(
        _fixture_product_for_repository(repository), upstream_tag
    )
    snapshot = validate_fixture_snapshot(fixture)
    if snapshot["repository"] != repository or snapshot["upstream_tag"] != upstream_tag:
        raise ValueError("fixture snapshot repository/tag does not match the request")
    return {
        "schema_version": 1,
        "kind": "registry-scan-result",
        "fixture_only": True,
        "snapshot": snapshot,
        "operations": [
            {
                "type": "fixture-read",
                "capability": "read",
                "reference": f"{repository}:{upstream_tag}",
            }
        ],
    }


def validate_public_tag(tag: object) -> str:
    if not isinstance(tag, str) or OCI_TAG_RE.fullmatch(tag) is None:
        raise ValueError(
            "public tag must use strict OCI tag syntax and be at most 128 bytes"
        )
    match = re.fullmatch(r"(.+)-r([1-9][0-9]*)", tag)
    if match is None:
        raise ValueError("public tag must end in canonical -rN with N >= 1")
    return tag


def _validate_fixture_release_manifest(release_manifest: dict[str, Any]) -> None:
    try:
        validate_schema(
            release_manifest,
            load_json(DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"release manifest failed Task 2 schema validation: {error}"
        ) from error
    specs = release_manifest["wheel_specs"]
    eligible = [spec for spec in specs if spec["build_eligible"]]
    blockers = sorted({reason for spec in specs for reason in spec["blocked_reasons"]})
    if (
        release_manifest["declared_wheel_count"] != len(specs)
        or release_manifest["eligible_wheel_count"] != len(eligible)
        or release_manifest["blockers"] != blockers
        or release_manifest["status"]
        != ("candidate" if len(eligible) == len(specs) else "blocked")
    ):
        raise ValueError(
            "release manifest operational counts/blockers/status are inconsistent"
        )
    expected_wheel_assets = {
        spec["spec_id"]: "candidate" if spec["build_eligible"] else "blocked"
        for spec in specs
    }
    wheel_assets = [
        asset
        for asset in release_manifest["publication"]["assets"]
        if asset["type"] == "wheel"
    ]
    actual_wheel_assets: dict[str, str] = {}
    for asset in wheel_assets:
        spec_id = asset["id"].removeprefix("wheel:")
        if spec_id in actual_wheel_assets or asset["required"] is not True:
            raise ValueError(
                "release manifest contains duplicate/non-required wheel asset"
            )
        actual_wheel_assets[spec_id] = asset["status"]
    if actual_wheel_assets != expected_wheel_assets:
        raise ValueError("release manifest wheel assets do not match operational specs")


def _select_fixture_wheel(
    release_manifest: dict[str, Any],
    wheel_records: list[dict[str, Any]],
    spec_id: str,
    fixture_mode: bool,
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not fixture_mode:
        raise RegistryBlocker(
            "production-wheel-unpublished",
            "production wheel is unpublished; Task 2 emits no production publication",
        )
    _validate_fixture_release_manifest(release_manifest)
    specs = [
        item
        for item in release_manifest.get("wheel_specs", [])
        if item.get("spec_id") == spec_id
    ]
    assets = [
        item
        for item in release_manifest.get("publication", {}).get("assets", [])
        if item.get("id") == f"wheel:{spec_id}"
    ]
    records = [item for item in wheel_records if item.get("spec_id") == spec_id]
    if len(specs) != 1 or len(assets) != 1 or len(records) != 1:
        raise ValueError(
            "wheel selection must be exact and unique in manifest, assets, and records"
        )
    spec, asset, record = specs[0], assets[0], records[0]
    expected_status = "candidate" if spec["build_eligible"] else "blocked"
    if asset != {
        "id": f"wheel:{spec_id}",
        "type": "wheel",
        "required": True,
        "status": expected_status,
    }:
        raise ValueError(
            "selected manifest wheel asset does not match its Task 2 spec status"
        )
    expected_record_keys = (
        WHEEL_RECORD_KEYS_BY_SOURCE.get(record.get("source_kind"))
        if isinstance(record, dict)
        else None
    )
    if expected_record_keys is None or set(record) != expected_record_keys:
        raise ValueError(
            "wheel inspection record is not a complete Task 2 source result"
        )
    if record["schema_version"] != 1 or record["kind"] != "ucm-wheel-inspection":
        raise ValueError("wheel inspection identity is invalid")
    _digest(record.get("sha256"), "selected wheel")
    _digest(record.get("declaration_sha256"), "selected wheel declaration")
    if record["declaration_sha256"] != spec.get("declaration_sha256"):
        raise ValueError("selected wheel declaration does not match its manifest spec")
    if (
        record["version"] != release_manifest["ucm_version"]
        or record["python_abi"] != spec["python_abi"]
        or record["cpu_arch"] != spec["cpu_arch"]
        or record["distribution"] != "uc-manager"
        or not isinstance(record["size"], int)
        or isinstance(record["size"], bool)
        or record["size"] < 1
        or not isinstance(record["tags"], list)
        or not record["tags"]
        or record["requires_dist"] != core.python_runtime_requirements(catalog)
    ):
        raise ValueError(
            "wheel inspection metadata does not match the selected Task 2 spec"
        )
    fixture_semantics = (
        record.get("source_kind") == "fixture"
        and record.get("status") == "fixture-only"
        and record.get("trust_level") == "fixture-only"
        and record.get("published") is False
        and record.get("publication_eligible") is False
        and isinstance(record.get("fixture_binding"), dict)
        and record["fixture_binding"].get("profile_id") == spec_id
        and record["fixture_binding"].get("marker_status") == "passed"
        and re.fullmatch(
            r"[0-9a-f]{40}", str(record["fixture_binding"].get("source_commit"))
        )
        is not None
    )
    if fixture_mode and not fixture_semantics:
        raise ValueError(
            "fixture wheel record does not have fixture-only unpublished semantics"
        )
    return spec, record


def _resolve_fixture_compatibility_rule(
    catalog: dict[str, Any],
    compatibility_rule_id: str,
    release_manifest: dict[str, Any],
    spec: dict[str, Any],
    parsed_tag: dict[str, str],
) -> dict[str, Any]:
    try:
        validate_schema(
            catalog,
            load_json(DEFAULT_SCHEMA_DIR / "config.schema.json"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"release catalog failed Task 2 schema validation: {error}"
        ) from error
    if (
        catalog.get("kind") != "release-config"
        or catalog.get("schema_version") != 2
        or catalog.get("ucm_version") != release_manifest["ucm_version"]
    ):
        raise ValueError(
            "release catalog identity/version does not match release manifest"
        )
    if sha256_value(catalog) != release_manifest["config_sha256"]:
        raise ValueError("release catalog digest does not match release manifest")
    compatibility = catalog.get("compatibility")
    rules = compatibility.get("rules") if isinstance(compatibility, dict) else None
    if not isinstance(rules, list):
        raise ValueError("compatibility rules must be an array")
    matches = [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("id") == compatibility_rule_id
    ]
    if len(matches) != 1:
        raise ValueError("compatibility rule id must resolve exactly once")
    rule = matches[0]
    expected_accelerator = (
        "ascend" if parsed_tag["product"] == "vllm-ascend" else "cuda"
    )
    checks = {
        "accelerator": rule.get("accelerator")
        == expected_accelerator
        == spec["accelerator"],
        "accelerator runtime": spec["accelerator_runtime"]
        in rule.get("accelerator_runtimes", []),
        "NPU architecture": spec["npu_arch_or_na"] == parsed_tag["npu_arch"]
        and spec["npu_arch_or_na"] in rule.get("npu_architectures", []),
        "operating system": spec["os"] == parsed_tag["operating_system"]
        and spec["os"] in rule.get("operating_systems", []),
        "CPU architecture": spec["cpu_arch"] in rule.get("cpu_architectures", []),
        "Python ABI": spec["python_abi"] in rule.get("python_abis", []),
        "upstream channel": parsed_tag["channel"] in rule.get("upstream_channels", []),
    }
    failed = [label for label, passed in checks.items() if not passed]
    if failed:
        raise ValueError(
            f"compatibility rule does not match selected upstream/wheel semantics: {failed}"
        )
    return copy.deepcopy(rule)


def build_fixture_candidate(
    release_manifest: dict[str, Any],
    wheel_records: list[dict[str, Any]],
    spec_id: str,
    upstream_snapshot: dict[str, Any],
    catalog: dict[str, Any],
    compatibility_rule_id: str,
    implementation_digest: str,
    *,
    fixture_mode: bool,
) -> dict[str, Any]:
    """Bind all immutable inputs into one build key and one target tag family."""
    if not isinstance(release_manifest, dict) or not isinstance(wheel_records, list):
        raise ValueError("release manifest and wheel records have invalid types")
    spec, wheel = _select_fixture_wheel(
        release_manifest, wheel_records, spec_id, fixture_mode, catalog
    )
    snapshot = validate_fixture_snapshot(upstream_snapshot)
    if not isinstance(compatibility_rule_id, str) or not compatibility_rule_id:
        raise ValueError("compatibility rule id must be non-empty")
    implementation_digest = _digest(implementation_digest, "implementation")
    parsed = parse_fixture_upstream_tag(
        _fixture_product_for_repository(snapshot["repository"]),
        snapshot["upstream_tag"],
    )
    rule = _resolve_fixture_compatibility_rule(
        catalog,
        compatibility_rule_id,
        release_manifest,
        spec,
        parsed,
    )
    ucm_version = release_manifest.get("ucm_version")
    if (
        not isinstance(ucm_version, str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?",
            ucm_version,
        )
        is None
    ):
        raise ValueError("release manifest UCM version is noncanonical")
    tag_base = f"{snapshot['upstream_tag']}-ucm-{ucm_version}"
    validate_public_tag(f"{tag_base}-r1")
    upstream_identity = {
        "repository": snapshot["repository"],
        "exact_upstream_tag": snapshot["upstream_tag"],
        "index_digest": snapshot["index_digest"],
        "platforms": snapshot["platforms"],
    }
    build_inputs = {
        "release_manifest_sha256": sha256_value(release_manifest),
        "wheel": {
            "spec_id": spec_id,
            "sha256": wheel["sha256"],
            "declaration_sha256": wheel["declaration_sha256"],
            "version": wheel["version"],
            "accelerator": spec["accelerator"],
            "accelerator_runtime": spec["accelerator_runtime"],
            "npu_arch_or_na": spec["npu_arch_or_na"],
            "os": spec["os"],
            "cpu_arch": wheel["cpu_arch"],
            "python_abi": wheel["python_abi"],
            "binary_profile_id": spec["binary_profile_id"],
        },
        "upstream": upstream_identity,
        "compatibility_rule_id": compatibility_rule_id,
        "compatibility_rule": rule,
        "compatibility_rule_sha256": sha256_value(rule),
        "implementation_digest": implementation_digest,
    }
    family = {"repository": parsed["target_repository"], "tag_base": tag_base}
    return {
        "schema_version": 1,
        "kind": "ucm-image-build-candidate",
        "fixture_only": fixture_mode,
        "unpublished": True,
        "ucm_version": ucm_version,
        "target_repository": parsed["target_repository"],
        "tag_base": tag_base,
        "tag_family_sha256": sha256_value(family),
        "build_key_sha256": sha256_value(build_inputs),
        "build_inputs": build_inputs,
    }


def _validate_fixture_candidate(candidate: dict[str, Any]) -> None:
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    _exact_keys(candidate, CANDIDATE_KEYS, "candidate")
    if (
        candidate["schema_version"] != 1
        or candidate["kind"] != "ucm-image-build-candidate"
    ):
        raise ValueError("candidate identity is invalid")
    if candidate["target_repository"] not in set(FIXTURE_TARGET_REPOSITORIES.values()):
        raise ValueError("candidate target repository is not allowed")
    if candidate["fixture_only"] is not True or candidate["unpublished"] is not True:
        raise ValueError(
            "Task 3 accepts only fixture-only, explicitly unpublished candidates"
        )
    if (
        not isinstance(candidate["ucm_version"], str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?",
            candidate["ucm_version"],
        )
        is None
    ):
        raise ValueError("candidate UCM version is noncanonical")
    validate_public_tag(f"{candidate['tag_base']}-r1")
    inputs = candidate["build_inputs"]
    if not isinstance(inputs, dict):
        raise ValueError("candidate build inputs must be an object")
    _exact_keys(inputs, BUILD_INPUT_KEYS, "candidate build inputs")
    wheel = inputs["wheel"]
    if not isinstance(wheel, dict):
        raise ValueError("candidate wheel input must be an object")
    _exact_keys(wheel, WHEEL_INPUT_KEYS, "candidate wheel input")
    for label, digest in (
        ("release manifest", inputs["release_manifest_sha256"]),
        ("wheel", wheel["sha256"]),
        ("wheel declaration", wheel["declaration_sha256"]),
        ("implementation", inputs["implementation_digest"]),
        ("compatibility rule", inputs["compatibility_rule_sha256"]),
    ):
        _digest(digest, label)
    rule = inputs["compatibility_rule"]
    if not isinstance(rule, dict):
        raise ValueError("candidate compatibility rule must be an object")
    _exact_keys(rule, COMPATIBILITY_RULE_KEYS, "candidate compatibility rule")
    if (
        rule["id"] != inputs["compatibility_rule_id"]
        or sha256_value(rule) != inputs["compatibility_rule_sha256"]
    ):
        raise ValueError("candidate compatibility rule digest/id does not match")
    upstream = inputs["upstream"]
    if not isinstance(upstream, dict):
        raise ValueError("candidate upstream input must be an object")
    _exact_keys(upstream, UPSTREAM_INPUT_KEYS, "candidate upstream input")
    synthetic_snapshot = {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": upstream["repository"],
        "upstream_tag": upstream["exact_upstream_tag"],
        "index_digest": upstream["index_digest"],
        "platforms": upstream["platforms"],
    }
    snapshot = validate_fixture_snapshot(synthetic_snapshot)
    parsed = parse_fixture_upstream_tag(
        _fixture_product_for_repository(snapshot["repository"]),
        snapshot["upstream_tag"],
    )
    expected_accelerator = "ascend" if parsed["product"] == "vllm-ascend" else "cuda"
    semantic_checks = (
        wheel["version"] == candidate["ucm_version"],
        wheel["accelerator"] == expected_accelerator == rule["accelerator"],
        wheel["accelerator_runtime"] in rule["accelerator_runtimes"],
        wheel["npu_arch_or_na"] == parsed["npu_arch"]
        and wheel["npu_arch_or_na"] in rule["npu_architectures"],
        wheel["os"] == parsed["operating_system"]
        and wheel["os"] in rule["operating_systems"],
        wheel["cpu_arch"] in rule["cpu_architectures"],
        wheel["python_abi"] in rule["python_abis"],
        parsed["channel"] in rule["upstream_channels"],
        isinstance(wheel["binary_profile_id"], str)
        and bool(wheel["binary_profile_id"]),
    )
    if not all(semantic_checks):
        raise ValueError(
            "candidate compatibility rule/wheel/upstream semantics do not match"
        )
    expected_base = f"{snapshot['upstream_tag']}-ucm-{candidate['ucm_version']}"
    if (
        candidate["target_repository"] != parsed["target_repository"]
        or candidate["tag_base"] != expected_base
    ):
        raise ValueError(
            "candidate target repository or tag base does not match upstream identity"
        )
    expected_family = sha256_value(
        {
            "repository": candidate["target_repository"],
            "tag_base": candidate["tag_base"],
        }
    )
    if candidate["tag_family_sha256"] != expected_family:
        raise ValueError("candidate tag family digest does not match its identity")
    if candidate["build_key_sha256"] != sha256_value(inputs):
        raise ValueError("candidate build key does not match its immutable inputs")


def with_fixture_revision(candidate: dict[str, Any], revision: int) -> dict[str, Any]:
    _validate_fixture_candidate(candidate)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("revision must be an integer >= 1")
    result = copy.deepcopy(candidate)
    result["revision"] = revision
    result["public_tag"] = validate_public_tag(f"{candidate['tag_base']}-r{revision}")
    return result


def fixture_inventory_digest(inventory: dict[str, Any]) -> str:
    """Hash the canonical Registry read set, excluding its asserted digest field."""
    if not isinstance(inventory, dict):
        raise ValueError("registry inventory must be an object")
    base_keys = {"schema_version", "kind", "repositories", "entries"}
    if frozenset(inventory) not in {frozenset(base_keys), frozenset(INVENTORY_KEYS)}:
        raise ValueError("registry inventory fields are not canonical")
    if not isinstance(inventory["entries"], list):
        raise ValueError("registry inventory entries must be an array")
    canonical_inventory = {
        "schema_version": inventory["schema_version"],
        "kind": inventory["kind"],
        "repositories": inventory["repositories"],
        "entries": sorted(copy.deepcopy(inventory["entries"]), key=canonical_bytes),
    }
    return sha256_value(canonical_inventory)


def _validate_fixture_inventory(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(inventory, dict):
        raise ValueError("registry inventory must be an object")
    _exact_keys(inventory, INVENTORY_KEYS, "registry inventory")
    if inventory["schema_version"] != 1 or inventory["kind"] != "registry-inventory":
        raise ValueError("registry inventory identity is invalid")
    if inventory["repositories"] != sorted(FIXTURE_TARGET_REPOSITORIES.values()):
        raise ValueError(
            "registry inventory must cover exactly the two target repositories"
        )
    actual_inventory_sha256 = fixture_inventory_digest(inventory)
    if inventory["inventory_sha256"] != actual_inventory_sha256:
        raise ValueError(
            "inventory digest mismatch: "
            f"asserted {inventory['inventory_sha256']}, actual {actual_inventory_sha256}"
        )
    entries = inventory["entries"]
    if not isinstance(entries, list):
        raise ValueError("registry inventory entries must be an array")
    by_tag: dict[tuple[str, str], dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"registry inventory entry {index} must be an object")
        _exact_keys(entry, ENTRY_KEYS, f"registry inventory entry {index}")
        if entry["repository"] not in set(FIXTURE_TARGET_REPOSITORIES.values()):
            raise ValueError("registry inventory target repository is not allowed")
        validate_public_tag(entry["tag"])
        for field in ("build_key_sha256", "observed_digest", "evidence_digest"):
            _digest(entry[field], f"registry inventory {field}")
        identity = (entry["repository"], entry["tag"])
        if identity in by_tag:
            label = "conflicting" if by_tag[identity] != entry else "duplicate"
            raise RegistryBlocker(
                "duplicate-conflicting-inventory",
                f"{label} registry inventory entries for {identity[0]}:{identity[1]}",
            )
        by_tag[identity] = entry
    return entries


def reconcile_fixture_candidate(
    candidate: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    """Return a build task or a no-op from a passed, immutable Registry view."""
    _validate_fixture_candidate(candidate)
    entries = _validate_fixture_inventory(inventory)
    inventory_sha256 = inventory["inventory_sha256"]
    family_prefix = candidate["tag_base"] + "-r"
    family_entries = [
        entry
        for entry in entries
        if entry["repository"] == candidate["target_repository"]
        and entry["tag"].startswith(family_prefix)
    ]
    matching = [
        entry
        for entry in family_entries
        if entry["build_key_sha256"] == candidate["build_key_sha256"]
    ]
    stable = [
        entry
        for entry in matching
        if entry["observed_digest"] == entry["evidence_digest"]
    ]
    if len(stable) > 1:
        raise ValueError("conflicting stable inventory entries share one build key")
    common = {
        "schema_version": 1,
        "kind": "registry-reconcile-result",
        "candidate_build_key_sha256": candidate["build_key_sha256"],
        "inventory": copy.deepcopy(inventory),
        "inventory_sha256": inventory_sha256,
        "publication_attempted": False,
        "operations": [
            {
                "type": "registry-inventory-read",
                "capability": "read",
                "reference": inventory_sha256,
            }
        ],
    }
    if stable:
        return {**common, "decision": "already-present", "task_count": 0, "tasks": []}
    used_revisions = {
        int(entry["tag"].removeprefix(family_prefix)) for entry in family_entries
    }
    revision = 1
    while revision in used_revisions:
        revision += 1
    reason = "tag-digest-drift" if matching else "new-build-key"
    revisioned = with_fixture_revision(candidate, revision)
    task = {
        "action": "build-unpublished-candidate",
        "repository": candidate["target_repository"],
        "tag": revisioned["public_tag"],
        "revision": revision,
        "reason": reason,
        "build_key_sha256": candidate["build_key_sha256"],
        "tag_family_sha256": candidate["tag_family_sha256"],
        "concurrency_key": candidate["tag_family_sha256"],
        "precondition": {
            "type": "tag-absent",
            "repository": candidate["target_repository"],
            "tag": revisioned["public_tag"],
            "inventory_sha256": inventory_sha256,
        },
        "publication_attempted": False,
    }
    operations = [
        *common["operations"],
        {
            "type": "build-plan",
            "capability": "plan",
            "reference": f"{candidate['target_repository']}:{revisioned['public_tag']}",
        },
    ]
    return {
        **common,
        "operations": operations,
        "decision": "schedule",
        "task_count": 1,
        "tasks": [task],
    }


def resolved_registry_contract(
    resolved_plan: dict[str, Any],
    *,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Project protected Registry member/family authority from one frozen plan."""
    validate_resolved_plan(resolved_plan)
    if (
        not isinstance(expected_plan_sha256, str)
        or resolved_plan["lane"] != "protected-tag"
        or resolved_plan["fixture_only"] is not False
        or resolved_plan["resolved_plan_sha256"] != expected_plan_sha256
    ):
        raise ValueError("Registry publication requires the exact live protected plan")
    images_by_id = {task["task_id"]: task for task in resolved_plan["image_tasks"]}
    members = [
        {
            "task_id": task["task_id"],
            "family_task_id": task["family_task_id"],
            "spec_id": task["spec_id"],
            "profile_id": task["profile_id"],
            "family_id": task["family_task_id"],
            "platform": task["platform"],
            "target_repository": task["target_repository"],
            "target_tag": task["target_tag"],
            "candidate_task_sha256": task["task_sha256"],
            "publication_task_sha256": task["task_sha256"],
        }
        for task in resolved_plan["image_tasks"]
    ]
    indexes: list[dict[str, Any]] = []
    for family in resolved_plan["family_tasks"]:
        grouped = [images_by_id[task_id] for task_id in family["image_task_ids"]]
        if any(
            task["family_task_id"] != family["task_id"]
            or task["target_repository"] != family["target_repository"]
            or task["target_tag"] != family["target_tag"]
            for task in grouped
        ):
            raise ValueError("resolved family task has inconsistent image authority")
        indexes.append(
            {
                "family_task_id": family["task_id"],
                "family_task_sha256": family["task_sha256"],
                "family_id": family["task_id"],
                "target_repository": family["target_repository"],
                "target_tag": validate_public_tag(family["target_tag"]),
                "member_task_ids": list(family["image_task_ids"]),
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "ucm-resolved-registry-contract",
        "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
        "source_sha": resolved_plan["source"]["commit"],
        "staging_repository": resolved_plan["source"]["staging_repository"],
        "members": members,
        "indexes": indexes,
    }
    return {**payload, "contract_sha256": sha256_value(payload)}


def _require_resolved_registry_contract(
    resolved_plan: object, expected_plan_sha256: object
) -> dict[str, Any]:
    if not isinstance(resolved_plan, dict) or not isinstance(expected_plan_sha256, str):
        raise ValueError("production Registry requires an exact frozen resolved plan")
    return resolved_registry_contract(
        resolved_plan, expected_plan_sha256=expected_plan_sha256
    )


_CANONICAL_UPSTREAM_TAG = re.compile(
    r"^v(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?)(?P<suffix>-[a-z0-9][a-z0-9.-]*)?$"
)


def _excluded_by_pattern(tag: str, patterns: list[str]) -> bool:
    normalized = tag.casefold()
    for pattern in patterns:
        if pattern.casefold() in normalized:
            return True
    return False


def _canonical_variant_suffixes(product: dict[str, Any]) -> dict[str, str]:
    suffixes: dict[str, str] = {}
    for variant_value in product["variants"]:
        variant = variant_value["id"]
        suffix = variant_value.get("tag_suffix")
        if (
            not isinstance(suffix, str)
            or re.fullmatch(r"(?:|-[a-z0-9][a-z0-9.-]*)", suffix) is None
        ):
            raise ValueError(
                f"upstream variant {variant!r} has an invalid declared tag suffix"
            )
        previous = suffixes.get(suffix)
        if previous is not None:
            raise ValueError(
                "duplicate canonical variant suffix "
                f"{suffix!r} for product {product['id']!r}: "
                f"{previous!r} and {variant!r}"
            )
        suffixes[suffix] = variant
    return suffixes


def validate_catalog_tag_grammar(catalog: dict[str, Any]) -> None:
    """Reject product variant declarations with ambiguous canonical tags."""
    for product in catalog["upstream_products"]:
        _canonical_variant_suffixes(product)


def _parse_product_tag(product: dict[str, Any], tag: str) -> dict[str, str]:
    match = _CANONICAL_UPSTREAM_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("malformed-tag")
    version_text = match.group("version")
    try:
        version = Version(version_text)
    except InvalidVersion as error:  # pragma: no cover - guarded by the regex.
        raise ValueError("malformed-tag") from error
    if (
        version.epoch != 0
        or version.local is not None
        or version.dev is not None
        or version.post is not None
        or str(version) != version_text
    ):
        raise ValueError("malformed-tag")
    channel = "rc" if version.pre is not None and version.pre[0] == "rc" else "stable"
    suffix = match.group("suffix") or ""
    suffixes = _canonical_variant_suffixes(product)
    variant = suffixes.get(suffix)
    if variant is None:
        raise ValueError("unsupported-variant")
    return {
        "tag": tag,
        "version": version_text,
        "channel": channel,
        "variant": variant,
    }


def select_catalog_tags(
    catalog: dict[str, Any], tag_lists: dict[str, list[str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Purely select canonical configured tags and retain every exclusion."""
    selected: list[dict[str, str]] = []
    exclusions: list[dict[str, str]] = []
    validate_catalog_tag_grammar(catalog)
    patterns = catalog["compatibility"]["excluded_upstream_patterns"]
    for product in sorted(catalog["upstream_products"], key=lambda item: item["id"]):
        repository = product["repository"]
        tags = tag_lists.get(repository)
        if not isinstance(tags, list):
            raise ValueError(
                f"tag list is missing for configured repository {repository}"
            )
        for tag in sorted(set(tags)):
            reason: str | None = None
            parsed: dict[str, str] | None = None
            if _excluded_by_pattern(tag, patterns):
                reason = "excluded-pattern"
            else:
                try:
                    parsed = _parse_product_tag(product, tag)
                except ValueError as error:
                    reason = str(error)
            if parsed is not None and parsed["channel"] not in product["channels"]:
                reason = "unsupported-channel"
            if parsed is not None and reason is None:
                if Version(parsed["version"]) not in SpecifierSet(
                    product["version_specifier"]
                ):
                    reason = "version-outside-specifier"
            if reason is not None:
                exclusions.append(
                    {
                        "product_id": product["id"],
                        "repository": repository,
                        "tag": tag,
                        "reason": reason,
                    }
                )
                continue
            assert parsed is not None
            selected.append(
                {
                    "product_id": product["id"],
                    "repository": repository,
                    **copy.deepcopy(parsed),
                }
            )
    selected.sort(
        key=lambda item: (
            item["product_id"],
            Version(item["version"]),
            item["variant"],
            item["tag"],
        )
    )
    exclusions.sort(
        key=lambda item: (
            item["product_id"],
            item["repository"],
            item["tag"],
            item["reason"],
        )
    )
    return selected, exclusions


def _target_coordinate(
    catalog: dict[str, Any], selected: dict[str, str]
) -> tuple[str, str]:
    matches = [
        product
        for product in catalog["upstream_products"]
        if product["id"] == selected["product_id"]
    ]
    if len(matches) != 1:
        raise ValueError("selected product must have exactly one target configuration")
    product = matches[0]
    target_repository = product.get("target_repository")
    target_tag_suffix = product.get("target_tag_suffix")
    if not isinstance(target_repository, str) or not isinstance(target_tag_suffix, str):
        raise ValueError("selected product target configuration is malformed")
    return target_repository, selected["tag"] + target_tag_suffix


def _artifact_set(tasks: list[dict[str, Any]], prefix: str) -> list[dict[str, str]]:
    return [
        {"task_id": task["task_id"], "name": task["artifact_name"]} for task in tasks
    ]


def _pr_smoke_projection(
    catalog: dict[str, Any],
    wheel_tasks: list[dict[str, Any]],
    image_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    selectors = catalog["pr_smoke"]["image_selectors"]
    selected_images: list[dict[str, Any]] = []
    for selector in selectors:
        matches = [
            task
            for task in image_tasks
            if task["runtime"]["product_id"] == selector["product_id"]
            and task["runtime"]["variant"] == selector["variant"]
            and task["cpu_arch"] == selector["cpu_arch"]
        ]
        if len(matches) != 1:
            raise ValueError(
                "PR smoke selector must resolve exactly one image task: "
                f"{selector!r}"
            )
        selected_images.append(matches[0])
    selected_image_ids = {task["task_id"] for task in selected_images}
    if len(selected_image_ids) != len(selected_images):
        raise ValueError("PR smoke selectors resolve duplicate image tasks")
    selected_wheel_ids = {task["wheel_task_id"] for task in selected_images}
    selected_wheels = [
        task for task in wheel_tasks if task["task_id"] in selected_wheel_ids
    ]
    if {task["task_id"] for task in selected_wheels} != selected_wheel_ids:
        raise ValueError("PR smoke image dependencies do not resolve exact wheel tasks")
    return {
        "github_wheel_matrix": {
            "include": [
                {"task_id": task["task_id"], "runner": task["runner"]}
                for task in selected_wheels
            ]
        },
        "github_image_matrix": {
            "include": [
                {
                    "task_id": task["task_id"],
                    "runner": task["runner"],
                    "wheel_task_id": task["wheel_task_id"],
                }
                for task in selected_images
            ]
        },
    }


def resolve_catalog(
    catalog: dict[str, Any],
    *,
    source_sha: str,
    lane: str,
    fixture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Snapshot configured repositories once and emit one immutable build plan."""
    core._exact_keys(
        catalog,
        core.RELEASE_KEYS,
        "release catalog",
        optional=core.OPTIONAL_CATALOG_KEYS,
    )
    core.validate_catalog(catalog)
    if fixture is not None and lane == "protected-tag":
        raise ValueError("fixture resolution cannot acquire protected-tag authority")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("source_sha must be an exact lowercase 40-hex commit")
    limits = catalog.get("scan_limits")
    if not isinstance(limits, dict) or set(limits) != {
        "max_tags_per_repository",
        "max_selected_upstreams",
    }:
        raise ValueError("catalog requires exact scan_limits")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in limits.values()
    ):
        raise ValueError("catalog scan limits must be positive integers")
    configured_repositories = {
        product["repository"] for product in catalog["upstream_products"]
    }
    repositories_fixture: dict[str, Any] | None = None
    if fixture is not None:
        if not isinstance(fixture, dict) or set(fixture) != {
            "kind",
            "schema_version",
            "repositories",
        }:
            raise ValueError("registry discovery fixture fields are noncanonical")
        if (
            fixture["kind"] != "registry-discovery-fixture"
            or fixture["schema_version"] != 1
            or not isinstance(fixture["repositories"], dict)
            or set(fixture["repositories"]) != configured_repositories
        ):
            raise ValueError("registry discovery fixture is incomplete")
        repositories_fixture = fixture["repositories"]

    tag_lists: dict[str, list[str]] = {}
    operations: list[dict[str, Any]] = []
    for repository in sorted(configured_repositories):
        repository_fixture = (
            repositories_fixture[repository]
            if repositories_fixture is not None
            else None
        )
        tag_result = enumerate_repository_tags(
            repository,
            fixture=repository_fixture,
            max_tags=limits["max_tags_per_repository"],
        )
        tag_lists[repository] = tag_result["tags"]
        operations.extend(tag_result["operations"])
    selected, exclusions = select_catalog_tags(catalog, tag_lists)
    if len(selected) > limits["max_selected_upstreams"]:
        raise ValueError(
            "registry selection limit max_selected_upstreams="
            f"{limits['max_selected_upstreams']} exceeded by exact set of "
            f"{len(selected)}"
        )

    products = {item["id"]: item for item in catalog["upstream_products"]}
    resolved_upstreams: list[dict[str, Any]] = []
    selected_fixture_tags: dict[str, set[str]] = {
        repository: set() for repository in configured_repositories
    }
    for item in selected:
        product = products[item["product_id"]]
        snapshot_fixture = None
        if repositories_fixture is not None:
            snapshots = repositories_fixture[item["repository"]]["snapshots"]
            if not isinstance(snapshots, dict) or item["tag"] not in snapshots:
                raise ValueError(
                    f"registry fixture is missing selected snapshot {item['tag']}"
                )
            snapshot_fixture = snapshots[item["tag"]]
            selected_fixture_tags[item["repository"]].add(item["tag"])
        scan = resolve_repository_tag(
            item["repository"],
            item["tag"],
            required_architectures=product["required_cpu_architectures"],
            fixture=snapshot_fixture,
        )
        operations.extend(scan["operations"])
        target_repository, target_tag = _target_coordinate(catalog, item)
        resolved_upstreams.append(
            {
                **copy.deepcopy(item),
                "index_digest": scan["snapshot"]["index_digest"],
                "members": scan["snapshot"]["members"],
                "target_repository": target_repository,
                "target_tag": target_tag,
            }
        )
    if repositories_fixture is not None:
        for repository in sorted(configured_repositories):
            snapshots = repositories_fixture[repository]["snapshots"]
            if set(snapshots) != selected_fixture_tags[repository]:
                raise ValueError(
                    f"registry fixture snapshots are not the exact selected set for {repository}"
                )

    expanded = core.expand_release_plan(catalog, resolved_upstreams, lane=lane)
    wheel_tasks = expanded["wheel_tasks"]
    image_tasks = expanded["image_tasks"]
    family_tasks = expanded["family_tasks"]
    source = {
        "repository": catalog["source"]["repository"],
        "staging_repository": catalog["source"]["staging_repository"],
        "default_branch": catalog["source"]["default_branch"],
        "release_tag": catalog["source"]["release_tag"],
        "release_policy": catalog["source"]["release_policy"],
        "version_file": catalog["version_file"],
        "ucm_version": catalog["ucm_version"],
        "commit": source_sha,
    }
    scan_evidence = {
        "resolved_upstreams": resolved_upstreams,
        "exclusions": exclusions,
        "operations": operations,
    }
    result: dict[str, Any] = {
        "kind": "ucm-resolved-build-plan",
        "schema_version": 1,
        "fixture_only": fixture is not None,
        "lane": lane,
        "source": source,
        "chart": copy.deepcopy(catalog["chart"]),
        "config_sha256": core.sha256_value(catalog),
        "source_sha256": core.sha256_value(source),
        "scan_sha256": core.sha256_value(scan_evidence),
        "resolved_upstreams": resolved_upstreams,
        "wheel_tasks": wheel_tasks,
        "image_tasks": image_tasks,
        "family_tasks": family_tasks,
        "github_wheel_matrix": {
            "include": [
                {"task_id": task["task_id"], "runner": task["runner"]}
                for task in wheel_tasks
            ]
        },
        "github_image_matrix": {
            "include": [
                {
                    "task_id": task["task_id"],
                    "runner": task["runner"],
                    "wheel_task_id": task["wheel_task_id"],
                }
                for task in image_tasks
            ]
        },
        "github_family_matrix": {
            "include": [
                {
                    "task_id": task["task_id"],
                    "family_task_id": task["task_id"],
                    "runner": task["control_runner"],
                    "control_task_id": task["control_task_id"],
                    "control_arch": task["control_arch"],
                }
                for task in family_tasks
            ]
        },
        "pr_smoke": _pr_smoke_projection(catalog, wheel_tasks, image_tasks),
        "expected_artifacts": {
            "resolved_plan": f"ucm-resolved-plan-{source_sha}",
            "wheels": _artifact_set(wheel_tasks, "wheel"),
            "images": _artifact_set(image_tasks, "image"),
            "families": _artifact_set(family_tasks, "family"),
        },
        "exclusions": exclusions,
        "operations": operations,
        "counts": {
            "scanned_tags": sum(len(tags) for tags in tag_lists.values()),
            "selected_upstreams": len(resolved_upstreams),
            "excluded_tags": len(exclusions),
            "wheel_tasks": len(wheel_tasks),
            "image_tasks": len(image_tasks),
            "family_tasks": len(family_tasks),
        },
    }
    result["resolved_plan_sha256"] = core.sha256_value(result)
    return result


def validate_resolved_plan(plan: dict[str, Any]) -> None:
    """Validate the immutable envelope and every task hash before consumption."""
    if not isinstance(plan, dict):
        raise ValueError("resolved plan must be an object")
    expected_fields = {
        "kind",
        "schema_version",
        "fixture_only",
        "lane",
        "source",
        "chart",
        "config_sha256",
        "source_sha256",
        "scan_sha256",
        "resolved_upstreams",
        "wheel_tasks",
        "image_tasks",
        "family_tasks",
        "github_wheel_matrix",
        "github_image_matrix",
        "github_family_matrix",
        "pr_smoke",
        "expected_artifacts",
        "exclusions",
        "operations",
        "counts",
        "resolved_plan_sha256",
    }
    if set(plan) != expected_fields:
        raise ValueError(
            "resolved plan top-level fields mismatch: "
            f"missing={sorted(expected_fields - set(plan))}, "
            f"extra={sorted(set(plan) - expected_fields)}"
        )
    if plan.get("kind") != "ucm-resolved-build-plan" or plan.get("schema_version") != 1:
        raise ValueError("resolved plan identity must be schema version 1")
    if not isinstance(plan["fixture_only"], bool):
        raise ValueError("resolved plan fixture_only must be boolean")
    if plan["lane"] not in {"feature-candidate", "protected-tag"}:
        raise ValueError("resolved plan lane is invalid")
    if plan["fixture_only"] and plan["lane"] == "protected-tag":
        raise ValueError("fixture plan cannot carry protected-tag authority")
    claimed_hash = plan.get("resolved_plan_sha256")
    unhashed = {
        key: value for key, value in plan.items() if key != "resolved_plan_sha256"
    }
    if claimed_hash != core.sha256_value(unhashed):
        raise ValueError("resolved plan hash mismatch")
    source = plan["source"]
    if (
        not isinstance(source, dict)
        or set(source)
        != {
            "repository",
            "staging_repository",
            "default_branch",
            "release_tag",
            "release_policy",
            "version_file",
            "ucm_version",
            "commit",
        }
        or not isinstance(source["repository"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source["repository"])
        is None
        or not isinstance(source["staging_repository"], str)
        or REPOSITORY_RE.fullmatch(source["staging_repository"]) is None
        or not isinstance(source["release_tag"], str)
        or re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?", source["release_tag"])
        is None
        or not isinstance(source["commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", source["commit"]) is None
        or not isinstance(source["default_branch"], str)
        or not source["default_branch"]
        or not isinstance(source["release_policy"], str)
        or not source["release_policy"]
        or not isinstance(source["version_file"], str)
        or not source["version_file"]
        or Path(source["version_file"]).is_absolute()
        or ".." in Path(source["version_file"]).parts
        or not isinstance(source["ucm_version"], str)
        or not source["ucm_version"]
        or source["release_tag"] != f"v{source['ucm_version']}"
    ):
        raise ValueError("resolved plan source is malformed")
    if plan["source_sha256"] != core.sha256_value(source):
        raise ValueError("resolved plan source hash mismatch")
    chart = plan["chart"]
    if (
        not isinstance(chart, dict)
        or set(chart) - {"provenance"}
        != {
            "source",
            "name",
            "version",
            "app_version",
            "publication_target",
            "validation_cases",
        }
        or any(
            not isinstance(chart[field], str) or not chart[field]
            for field in (
                "source",
                "name",
                "version",
                "app_version",
                "publication_target",
            )
        )
        or not isinstance(chart["validation_cases"], list)
        or not chart["validation_cases"]
    ):
        raise ValueError("resolved plan Chart authority is malformed")
    for hash_name in ("config_sha256", "scan_sha256", "resolved_plan_sha256"):
        if (
            not isinstance(plan[hash_name], str)
            or DIGEST_RE.fullmatch(plan[hash_name]) is None
        ):
            raise ValueError(f"resolved plan {hash_name} is malformed")

    core.validate_resolved_upstreams(plan["resolved_upstreams"])
    snapshots = plan["resolved_upstreams"]
    snapshot_hashes = [core.sha256_value(snapshot) for snapshot in snapshots]
    if len(snapshot_hashes) != len(set(snapshot_hashes)):
        raise ValueError("resolved plan snapshots must be unique")

    exclusions = plan["exclusions"]
    exclusion_fields = {"product_id", "repository", "tag", "reason"}
    if not isinstance(exclusions, list) or any(
        not isinstance(item, dict)
        or set(item) != exclusion_fields
        or any(not isinstance(item[key], str) or not item[key] for key in item)
        for item in exclusions
    ):
        raise ValueError("resolved plan exclusions are malformed")
    if exclusions != sorted(
        exclusions,
        key=lambda item: (
            item["product_id"],
            item["repository"],
            item["tag"],
            item["reason"],
        ),
    ):
        raise ValueError("resolved plan exclusions are not canonical")

    operations = plan["operations"]
    allowed_operation_types = {
        "crane-tag-list",
        "crane-digest",
        "crane-manifest",
        "fixture-tag-page-read",
        "fixture-snapshot-read",
    }
    if not isinstance(operations, list) or not operations:
        raise ValueError("resolved plan operations must be a non-empty array")
    for operation in operations:
        if (
            not isinstance(operation, dict)
            or operation.get("type") not in allowed_operation_types
            or operation.get("capability") != "read"
            or not isinstance(operation.get("reference"), str)
            or not operation["reference"]
            or set(operation)
            != (
                {"type", "capability", "reference", "page"}
                if operation.get("type") == "fixture-tag-page-read"
                else {"type", "capability", "reference"}
            )
            or (
                "page" in operation
                and (
                    not isinstance(operation["page"], int)
                    or isinstance(operation["page"], bool)
                    or operation["page"] < 1
                )
            )
        ):
            raise ValueError("resolved plan operation is malformed")
    fixture_operations = [
        operation["type"].startswith("fixture-") for operation in operations
    ]
    if (plan["fixture_only"] and not all(fixture_operations)) or (
        not plan["fixture_only"] and any(fixture_operations)
    ):
        raise ValueError("resolved plan fixture authority differs from operations")
    scan_evidence = {
        "resolved_upstreams": snapshots,
        "exclusions": exclusions,
        "operations": operations,
    }
    if plan["scan_sha256"] != core.sha256_value(scan_evidence):
        raise ValueError("resolved plan scan hash mismatch")

    task_ids: set[str] = set()
    tasks_by_kind: dict[str, list[dict[str, Any]]] = {}
    expected_write_authority = (
        []
        if plan["lane"] == "feature-candidate"
        else ["github-prerelease", "ghcr-final-index", "ghcr-private-staging"]
    )
    for task_kind in ("wheel", "image", "family"):
        tasks = plan.get(f"{task_kind}_tasks")
        if not isinstance(tasks, list):
            raise ValueError(f"resolved plan {task_kind} tasks must be an array")
        tasks_by_kind[task_kind] = tasks
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
                raise ValueError(f"resolved plan {task_kind} task is malformed")
            if set(task) != RESOLVED_TASK_FIELDS[task_kind]:
                raise ValueError(
                    f"resolved plan {task_kind} task fields mismatch: "
                    f"missing={sorted(RESOLVED_TASK_FIELDS[task_kind] - set(task))}, "
                    f"extra={sorted(set(task) - RESOLVED_TASK_FIELDS[task_kind])}"
                )
            if re.fullmatch(f"{task_kind}-[0-9a-f]{{64}}", task["task_id"]) is None:
                raise ValueError(f"resolved plan {task_kind} task ID is malformed")
            if task["task_id"] in task_ids:
                raise ValueError("resolved plan task IDs must be globally unique")
            task_ids.add(task["task_id"])
            task_payload = {
                key: value for key, value in task.items() if key != "task_sha256"
            }
            if task.get("task_sha256") != core.sha256_value(task_payload):
                raise ValueError(f"resolved plan {task_kind} task hash mismatch")
            if task_kind in {"wheel", "image"}:
                cpu_authority = core.cpu_toolchain_authority(
                    task.get("cpu_arch"),
                    location=f"resolved plan {task_kind} task cpu_arch",
                )
                if task.get("platform") != cpu_authority.oci_platform:
                    raise ValueError(
                        f"resolved plan {task_kind} platform differs from its "
                        "CPU/tool architecture"
                    )
            else:
                architectures = task.get("cpu_arch")
                platforms = task.get("platform")
                if (
                    not isinstance(architectures, list)
                    or not isinstance(platforms, list)
                    or len(architectures) != len(platforms)
                ):
                    raise ValueError(
                        "resolved plan family CPU/tool architecture projection "
                        "is malformed"
                    )
                for index, architecture in enumerate(architectures):
                    cpu_authority = core.cpu_toolchain_authority(
                        architecture,
                        location=f"resolved plan family task cpu_arch[{index}]",
                    )
                    if platforms[index] != cpu_authority.oci_platform:
                        raise ValueError(
                            "resolved plan family platform differs from its "
                            "CPU/tool architecture"
                        )
                core.cpu_toolchain_authority(
                    task.get("control_arch"),
                    location="resolved plan family control_arch",
                )
            if task.get("write_authority") != expected_write_authority:
                raise ValueError(
                    f"resolved plan {task_kind} task lane authority mismatch"
                )

    wheel_tasks = tasks_by_kind["wheel"]
    image_tasks = tasks_by_kind["image"]
    family_tasks = tasks_by_kind["family"]
    wheels_by_id = {task["task_id"]: task for task in wheel_tasks}
    families_by_id = {task["task_id"]: task for task in family_tasks}
    if len(wheels_by_id) != len(wheel_tasks) or len(families_by_id) != len(
        family_tasks
    ):
        raise ValueError("resolved plan task IDs are not unique")
    for image in image_tasks:
        if image.get("wheel_task_id") not in wheels_by_id:
            raise ValueError("resolved plan image references an unknown wheel task")
        if image.get("family_task_id") not in families_by_id:
            raise ValueError("resolved plan image references an unknown family task")
        wheel = wheels_by_id[image["wheel_task_id"]]
        linked_wheel_fields = {
            "spec_id",
            "profile_id",
            "runner",
            "cpu_arch",
            "platform",
            "builder",
            "builder_sha256",
            "build",
            "python_abi",
            "python_version",
            "wheel_version",
            "wheel_platform",
            "required_native",
            "forbidden_native",
            "allowed_dt_needed",
            "external_required_dependencies",
            "dependency_lock_sha256",
            "dependency_lock",
            "runtime_requirements",
            "runtime_patch_manifest_sha256",
            "write_authority",
            "build_eligible",
        }
        if any(image[field] != wheel[field] for field in linked_wheel_fields) or (
            image["wheel_artifact_name"] != wheel["artifact_name"]
        ):
            raise ValueError("resolved plan image/wheel linkage is inconsistent")
    for family in family_tasks:
        linked = [
            image
            for image in image_tasks
            if image["family_task_id"] == family["task_id"]
        ]
        if not linked or family.get("image_task_ids") != [
            image["task_id"] for image in linked
        ]:
            raise ValueError("resolved plan family/image linkage is inconsistent")
        expected_family_projection = {
            "control_task_id": linked[0]["task_id"],
            "control_arch": linked[0]["cpu_arch"],
            "control_runner": linked[0]["runner"],
            "runner": [image["runner"] for image in linked],
            "cpu_arch": [image["cpu_arch"] for image in linked],
            "platform": [image["platform"] for image in linked],
            "builder": [image["builder"] for image in linked],
            "builder_sha256": [image["builder_sha256"] for image in linked],
            "member_set_sha256": core.sha256_value(
                [image["task_sha256"] for image in linked]
            ),
        }
        if any(
            family[field] != value
            for field, value in expected_family_projection.items()
        ):
            raise ValueError("resolved plan family/image projection is inconsistent")
        if family.get("wheel_task_ids") != {
            image["cpu_arch"]: image["wheel_task_id"] for image in linked
        }:
            raise ValueError("resolved plan family/wheel linkage is inconsistent")
        snapshot_matches = [
            snapshot
            for snapshot in snapshots
            if core.sha256_value(snapshot) == family.get("snapshot_sha256")
        ]
        if len(snapshot_matches) != 1:
            raise ValueError("resolved plan family snapshot linkage is inconsistent")
        snapshot = snapshot_matches[0]
        expected_family_runtime = {
            "repository": snapshot["repository"],
            "tag": snapshot["tag"],
            "version": snapshot["version"],
            "channel": snapshot["channel"],
            "variant": snapshot["variant"],
            "index_digest": snapshot["index_digest"],
        }
        if (
            family.get("product_id") != snapshot["product_id"]
            or family.get("runtime") != expected_family_runtime
            or family.get("runtime_sha256")
            != core.sha256_value(expected_family_runtime)
            or family.get("target_repository") != snapshot["target_repository"]
            or family.get("target_tag") != snapshot["target_tag"]
        ):
            raise ValueError("resolved plan family/snapshot linkage is inconsistent")
        for image in linked:
            member = snapshot["members"].get(image["cpu_arch"])
            expected_runtime = {
                "product_id": snapshot["product_id"],
                "repository": snapshot["repository"],
                "tag": snapshot["tag"],
                "version": snapshot["version"],
                "channel": snapshot["channel"],
                "variant": snapshot["variant"],
                "index_digest": snapshot["index_digest"],
                **(member or {}),
            }
            if (
                image.get("runtime") != expected_runtime
                or image.get("runtime_sha256") != core.sha256_value(expected_runtime)
                or image.get("target_repository") != family["target_repository"]
                or image.get("target_tag") != family["target_tag"]
            ):
                raise ValueError("resolved plan image/snapshot linkage is inconsistent")
    if {family["snapshot_sha256"] for family in family_tasks} != set(snapshot_hashes):
        raise ValueError("resolved plan snapshot set differs from family tasks")

    expected_counts = {
        "scanned_tags": len(snapshots) + len(exclusions),
        "selected_upstreams": len(snapshots),
        "excluded_tags": len(exclusions),
        "wheel_tasks": len(wheel_tasks),
        "image_tasks": len(image_tasks),
        "family_tasks": len(family_tasks),
    }
    if plan["counts"] != expected_counts:
        raise ValueError("resolved plan counts mismatch")
    expected_wheel_matrix = {
        "include": [
            {"task_id": task["task_id"], "runner": task["runner"]}
            for task in wheel_tasks
        ]
    }
    expected_image_matrix = {
        "include": [
            {
                "task_id": task["task_id"],
                "runner": task["runner"],
                "wheel_task_id": task["wheel_task_id"],
            }
            for task in image_tasks
        ]
    }
    expected_family_matrix = {
        "include": [
            {
                "task_id": task["task_id"],
                "family_task_id": task["task_id"],
                "runner": task["control_runner"],
                "control_task_id": task["control_task_id"],
                "control_arch": task["control_arch"],
            }
            for task in family_tasks
        ]
    }
    if plan["github_wheel_matrix"] != expected_wheel_matrix:
        raise ValueError("resolved plan wheel matrix mismatch")
    if plan["github_image_matrix"] != expected_image_matrix:
        raise ValueError("resolved plan image matrix mismatch")
    if plan["github_family_matrix"] != expected_family_matrix:
        raise ValueError("resolved plan family matrix mismatch")
    smoke = plan["pr_smoke"]
    if not isinstance(smoke, dict) or set(smoke) != {
        "github_wheel_matrix",
        "github_image_matrix",
    }:
        raise ValueError("resolved plan PR smoke projection is malformed")
    smoke_wheel = smoke["github_wheel_matrix"]
    smoke_image = smoke["github_image_matrix"]
    if (
        not isinstance(smoke_wheel, dict)
        or set(smoke_wheel) != {"include"}
        or not isinstance(smoke_wheel["include"], list)
        or not smoke_wheel["include"]
        or not isinstance(smoke_image, dict)
        or set(smoke_image) != {"include"}
        or not isinstance(smoke_image["include"], list)
        or not smoke_image["include"]
    ):
        raise ValueError("resolved plan PR smoke matrices are malformed")
    expected_wheels_by_id = {
        item["task_id"]: item for item in expected_wheel_matrix["include"]
    }
    expected_images_by_id = {
        item["task_id"]: item for item in expected_image_matrix["include"]
    }
    if any(
        item != expected_wheels_by_id.get(item.get("task_id"))
        for item in smoke_wheel["include"]
        if isinstance(item, dict)
    ) or any(
        item != expected_images_by_id.get(item.get("task_id"))
        for item in smoke_image["include"]
        if isinstance(item, dict)
    ):
        raise ValueError("resolved plan PR smoke matrices differ from full plan")
    smoke_image_wheels = {item["wheel_task_id"] for item in smoke_image["include"]}
    if {item["task_id"] for item in smoke_wheel["include"]} != smoke_image_wheels:
        raise ValueError("resolved plan PR smoke wheel dependency set mismatch")
    expected_artifacts = {
        "resolved_plan": f"ucm-resolved-plan-{source['commit']}",
        "wheels": _artifact_set(wheel_tasks, "wheel"),
        "images": _artifact_set(image_tasks, "image"),
        "families": _artifact_set(family_tasks, "family"),
    }
    if plan["expected_artifacts"] != expected_artifacts:
        raise ValueError("resolved plan artifact set mismatch")


def select_task(
    plan: dict[str, Any],
    *,
    task_kind: str,
    task_id: str,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Select exactly one frozen task without consulting Registry state."""
    validate_resolved_plan(plan)
    if (
        not isinstance(expected_plan_sha256, str)
        or DIGEST_RE.fullmatch(expected_plan_sha256) is None
        or expected_plan_sha256 != plan["resolved_plan_sha256"]
    ):
        raise ValueError("expected plan hash mismatch")
    if plan["fixture_only"] is True and plan["lane"] == "protected-tag":
        raise ValueError("fixture plan cannot authorize protected task selection")
    if task_kind not in {"wheel", "image", "family"}:
        raise ValueError("task_kind must be wheel, image, or family")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a non-empty opaque identifier")
    matches = [
        task for task in plan[f"{task_kind}_tasks"] if task["task_id"] == task_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"resolved plan {task_kind} task {task_id!r} does not resolve exactly once"
        )
    return copy.deepcopy(matches[0])


def verify_upstream_drift(
    plan: dict[str, Any], *, fixture: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Re-read every selected source tag before protected publication."""
    validate_resolved_plan(plan)
    if plan["fixture_only"] is True:
        raise ValueError("fixture plan cannot pass protected drift verification")
    if plan.get("lane") != "protected-tag":
        raise ValueError("upstream drift verification requires a protected-tag plan")
    if fixture is not None:
        raise ValueError("fixture reads cannot pass protected drift verification")
    operations: list[dict[str, Any]] = []
    observations: list[dict[str, str]] = []
    drifts: list[dict[str, str]] = []
    for frozen in plan["resolved_upstreams"]:
        fresh = read_repository_tag_digest(frozen["repository"], frozen["tag"])
        operations.append(fresh["operation"])
        observations.append(
            {
                "repository": frozen["repository"],
                "tag": frozen["tag"],
                "frozen_index_digest": frozen["index_digest"],
                "fresh_index_digest": fresh["index_digest"],
            }
        )
        if fresh["index_digest"] != frozen["index_digest"]:
            drifts.append(
                {
                    "repository": frozen["repository"],
                    "tag": frozen["tag"],
                    "frozen_index_digest": frozen["index_digest"],
                    "fresh_index_digest": fresh["index_digest"],
                }
            )
    if drifts:
        detail = "; ".join(
            f"{item['repository']}:{item['tag']} changed from "
            f"{item['frozen_index_digest']} to {item['fresh_index_digest']}"
            for item in drifts
        )
        raise ValueError(f"upstream tag drift: {detail}")
    result: dict[str, Any] = {
        "kind": "ucm-upstream-drift-verification",
        "schema_version": 1,
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "verified_tags": len(observations),
        "observations": observations,
        "operations": operations,
    }
    result["verification_sha256"] = core.sha256_value(result)
    return result


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key != "record_sha256"
    }


def _validate_member_operations(record: dict[str, Any]) -> list[dict[str, str]]:
    operations = record["operations"]
    if not isinstance(operations, list) or not operations:
        raise ValueError("member operations must be a non-empty array")
    staging_repository = _repository(record.get("staging_repository"))
    member_reference = f"{staging_repository}@{record['member_digest']}"
    staging_reference = f"{staging_repository}:{record['staging_tag']}"
    push = {
        "type": "registry-member-push-by-digest",
        "capability": "write",
        "reference": member_reference,
    }
    tag = {
        "type": "registry-staging-tag-create",
        "capability": "write",
        "reference": staging_reference,
    }
    prewrite = {
        "type": "registry-anonymous-prewrite-visibility-read",
        "capability": "read",
        "reference": staging_reference,
    }
    authenticated_prewrite = {
        "type": "registry-authenticated-staging-prewrite-read",
        "capability": "read",
        "reference": staging_reference,
    }
    suffix = [
        {
            "type": "registry-authenticated-digest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-manifest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-config-blob-read",
            "capability": "read",
            "reference": f"{staging_repository}@{record['config_digest']}",
        },
        *[
            {
                "type": "registry-authenticated-layer-blob-read",
                "capability": "read",
                "reference": f"{staging_repository}@{layer['digest']}",
            }
            for layer in record["layers"]
        ],
        {
            "type": "registry-anonymous-visibility-read",
            "capability": "read",
            "reference": staging_reference,
        },
    ]
    identities: set[tuple[str, str]] = set()
    validated: list[dict[str, str]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("member operation must be an object")
        _exact_keys(operation, {"type", "capability", "reference"}, "member operation")
        identity = (operation["type"], operation["reference"])
        if identity in identities:
            raise ValueError(f"duplicate member operation identity: {identity}")
        identities.add(identity)
        validated.append(copy.deepcopy(operation))
    allowed_ledgers = [
        [prewrite, authenticated_prewrite, *suffix],
        [prewrite, authenticated_prewrite, push, *suffix],
        [prewrite, authenticated_prewrite, tag, *suffix],
        [prewrite, authenticated_prewrite, push, tag, *suffix],
    ]
    if validated not in allowed_ledgers:
        raise ValueError(
            "member operation role, order, capability, or reference is invalid"
        )
    return validated


def _validate_member_content_identity(
    record: dict[str, Any], *, source_repository_url: str
) -> dict[str, Any]:
    identity = record["content_identity"]
    if not isinstance(identity, dict):
        raise ValueError("member content identity must be an object")
    _exact_keys(identity, CONTENT_IDENTITY_KEYS, "member content identity")
    stable = {
        key: copy.deepcopy(value)
        for key, value in identity.items()
        if key != "content_identity_sha256"
    }
    if (
        identity["content_identity_sha256"] != sha256_value(stable)
        or record["content_identity_sha256"] != identity["content_identity_sha256"]
    ):
        raise ValueError("member content identity digest mismatch")
    source = identity["source"]
    if not isinstance(source, dict):
        raise ValueError("member content identity source must be an object")
    _exact_keys(source, CONTENT_IDENTITY_SOURCE_KEYS, "member content identity source")
    if (
        source["commit"] != record["source_sha"]
        or source["repository_url"] != source_repository_url
        or source["repository_url"] != f"https://github.com/{source['repository']}"
        or re.fullmatch(r"[0-9a-f]{40}", source["tree"]) is None
    ):
        raise ValueError("member content identity source differs from publication")
    _digest(source["archive_sha256"], "member source archive")
    _digest(source["context_sha256"], "member source context")
    if (
        identity["manifest_digest"] != record["member_digest"]
        or identity["config_digest"] != record["config_digest"]
        or identity["annotations"] != record["manifest"]["annotations"]
        or identity["labels"] != record["config"]["labels"]
        or identity["task_sha256"] != record["candidate_task_sha256"]
        or identity["build_key_sha256"] != record["build_key_sha256"]
        or identity["wheel_sha256"] != record["wheel_sha256"]
        or identity["recipe_sha256"] != record["recipe_sha256"]
    ):
        raise ValueError("member content identity differs from OCI publication")
    identity_layers = identity["layers"]
    if not isinstance(identity_layers, list) or not identity_layers:
        raise ValueError("member content identity layer descriptors are invalid")
    for position, layer in enumerate(identity_layers):
        if not isinstance(layer, dict):
            raise ValueError(
                f"member content identity layer {position} descriptor is invalid"
            )
        _validate_layer_descriptor_annotations(
            layer,
            created=identity["created"],
            label=f"member content identity layer {position}",
        )
    expected_layers = []
    for position, layer in enumerate(record["layers"]):
        projected = {
            "mediaType": layer["media_type"],
            "digest": layer["digest"],
            "size": layer["size"],
        }
        if "annotations" in layer:
            projected["annotations"] = copy.deepcopy(layer["annotations"])
        _validate_layer_descriptor_annotations(
            projected,
            created=identity["created"],
            label=f"member content identity layer {position}",
        )
        expected_layers.append(projected)
    if identity["layers"] != expected_layers:
        raise ValueError("member content identity layer descriptors differ")
    diff_ids = identity["diff_ids"]
    if not isinstance(diff_ids, list) or len(diff_ids) != len(expected_layers):
        raise ValueError("member content identity diff-id closure is invalid")
    for position, diff_id in enumerate(diff_ids):
        _digest(diff_id, f"member content identity diff-id {position}")
    labels = identity["labels"]
    if (
        not isinstance(labels, dict)
        or not labels
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in labels.items()
        )
    ):
        raise ValueError("member content identity labels are invalid")
    expected_release_labels = {
        "org.opencontainers.image.source": source_repository_url,
        "org.opencontainers.image.revision": record["source_sha"],
        "io.ucm.release.source-tree": source["tree"],
        "io.ucm.release.source-context-sha256": source["context_sha256"],
        "io.ucm.release.task-sha256": record["candidate_task_sha256"],
        "io.ucm.release.build-key-sha256": record["build_key_sha256"],
        "io.ucm.release.wheel-sha256": record["wheel_sha256"],
        "io.ucm.release.recipe-sha256": record["recipe_sha256"],
    }
    if any(labels.get(key) != value for key, value in expected_release_labels.items()):
        raise ValueError("member content identity release labels are invalid")
    _created_epoch(identity["created"], "member content identity")
    if (
        not isinstance(identity["history"], list)
        or not identity["history"]
        or any(not isinstance(item, dict) for item in identity["history"])
    ):
        raise ValueError("member content identity created/history is invalid")
    return copy.deepcopy(identity)


def validate_member_record(
    record: object,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Validate a canonical publication record without changing image-result state."""
    contract = _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(record, dict):
        raise ValueError("member publication record must be an object")
    _exact_keys(record, MEMBER_RECORD_KEYS, "member publication record")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != 1
    ):
        raise ValueError("member schema_version must be the exact integer 1")
    if (
        record["kind"] != "ucm-registry-member-publication"
        or record["status"] != "passed"
    ):
        raise ValueError("member publication record identity is invalid")
    authorities = [
        item
        for item in contract["members"]
        if item["spec_id"] == record["spec_id"]
        and item["candidate_task_sha256"] == record["candidate_task_sha256"]
    ]
    if len(authorities) != 1:
        raise ValueError("member task does not resolve in the frozen plan contract")
    authority = authorities[0]
    stable_fields = (
        "spec_id",
        "profile_id",
        "family_id",
        "platform",
        "target_repository",
        "target_tag",
        "candidate_task_sha256",
        "publication_task_sha256",
    )
    if any(record[field] != authority[field] for field in stable_fields):
        raise ValueError("member record differs from feature/protected task authority")
    if (
        record["staging_repository"] != resolved_plan["source"]["staging_repository"]
        or record["staging_visibility"] != "private"
    ):
        raise ValueError("member record requires the exact private staging package")
    for field in (
        "candidate_task_sha256",
        "publication_task_sha256",
        "build_key_sha256",
        "wheel_sha256",
        "member_digest",
        "config_digest",
        "image_result_sha256",
        "recipe_sha256",
        "content_identity_sha256",
        "readback_sha256",
        "prewrite_visibility_evidence_sha256",
        "visibility_evidence_sha256",
        "record_sha256",
    ):
        _digest(record[field], f"member {field}")
    if (
        record["prewrite_visibility_evidence_sha256"]
        == record["visibility_evidence_sha256"]
    ):
        raise ValueError("member prewrite/postwrite visibility evidence must differ")
    if (
        not isinstance(record["source_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", record["source_sha"]) is None
    ):
        raise ValueError("member source_sha must be one exact lowercase commit SHA")
    if (
        not isinstance(record["member_size"], int)
        or isinstance(record["member_size"], bool)
        or record["member_size"] < 1
    ):
        raise ValueError("member manifest size must be a positive integer")
    expected_tag = "staging-" + record["build_key_sha256"].removeprefix("sha256:")
    if record["staging_tag"] != expected_tag:
        raise ValueError("staging tag must contain the complete member build key")
    annotations = record["annotations"]
    if not isinstance(annotations, dict):
        raise ValueError("member annotations must be an object")
    _exact_keys(annotations, MEMBER_ANNOTATION_KEYS, "member annotations")
    expected_annotations = {
        "io.ucm.release.build-key-sha256": record["build_key_sha256"],
        "io.ucm.release.candidate-task-sha256": record["candidate_task_sha256"],
        "io.ucm.release.family-id": record["family_id"],
        "io.ucm.release.platform": record["platform"],
        "io.ucm.release.spec-id": record["spec_id"],
        "io.ucm.release.wheel-sha256": record["wheel_sha256"],
    }
    if annotations != expected_annotations:
        raise ValueError("member annotations do not close over build identity")
    manifest = record["manifest"]
    config = record["config"]
    layers = record["layers"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "media_type",
        "digest",
        "size",
        "annotations",
    }:
        raise ValueError("member manifest closure is malformed")
    if (
        manifest["media_type"] not in OCI_MANIFEST_MEDIA_TYPES
        or _digest(manifest["digest"], "member manifest digest")
        != record["member_digest"]
        or manifest["size"] != record["member_size"]
        or manifest["annotations"]
        != {
            "io.ucm.release.recipe-sha256": record["recipe_sha256"],
            "io.ucm.release.task-sha256": record["candidate_task_sha256"],
        }
    ):
        raise ValueError("member manifest does not close over recipe/task identity")
    if not isinstance(config, dict) or set(config) != {
        "media_type",
        "digest",
        "size",
        "blob_sha256",
        "labels",
    }:
        raise ValueError("member config closure is malformed")
    if (
        config["media_type"] != "application/vnd.oci.image.config.v1+json"
        or _digest(config["digest"], "member config digest") != record["config_digest"]
        or _digest(config["blob_sha256"], "member config blob")
        != record["config_digest"]
        or not isinstance(config["size"], int)
        or isinstance(config["size"], bool)
        or config["size"] < 1
    ):
        raise ValueError("member config does not close over build/task/wheel identity")
    if not isinstance(layers, list) or not layers:
        raise ValueError("member publication requires at least one content layer")
    for position, layer in enumerate(layers):
        allowed_layer_keys = {
            "media_type",
            "digest",
            "size",
            "blob_sha256",
        }
        if not isinstance(layer, dict) or set(layer) not in (
            allowed_layer_keys,
            allowed_layer_keys | {"annotations"},
        ):
            raise ValueError(f"member layer {position} closure is malformed")
        if (
            not isinstance(layer["media_type"], str)
            or not layer["media_type"].startswith("application/vnd.")
            or _digest(layer["digest"], f"member layer {position}")
            != _digest(layer["blob_sha256"], f"member layer {position} blob")
            or not isinstance(layer["size"], int)
            or isinstance(layer["size"], bool)
            or layer["size"] < 1
        ):
            raise ValueError(f"member layer {position} content closure is invalid")
        projected = {
            "mediaType": layer["media_type"],
            "digest": layer["digest"],
            "size": layer["size"],
        }
        if "annotations" in layer:
            projected["annotations"] = copy.deepcopy(layer["annotations"])
        _validate_layer_descriptor_annotations(
            projected,
            created=record["content_identity"]["created"],
            label=f"member layer {position}",
        )
    _validate_member_content_identity(
        record,
        source_repository_url=(
            f"https://github.com/{resolved_plan['source']['repository']}"
        ),
    )
    _validate_collision_model(record["collision_model"], "member")
    _validate_member_operations(record)
    if record["record_sha256"] != sha256_value(_record_payload(record)):
        raise ValueError("member publication record digest mismatch")
    return copy.deepcopy(record)


def validate_index_record(
    record: object,
    *,
    parent_plans: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Reopen one index publication against its exact canonical parent plan."""
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(record, dict):
        raise ValueError("index publication record must be an object")
    _exact_keys(record, INDEX_RECORD_KEYS, "index publication record")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != 1
    ):
        raise ValueError("index schema_version must be the exact integer 1")
    if (
        record["kind"] != "ucm-registry-index-publication"
        or record["status"] != "passed"
    ):
        raise ValueError("index publication record identity is invalid")
    parent = _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    matching = [
        plan
        for plan in parent["plans"]
        if plan["family_id"] == record["family_id"]
        and plan["target_repository"] == record["target_repository"]
        and plan["target_tag"] == record["target_tag"]
    ]
    if len(matching) != 1:
        raise ValueError("index publication family does not resolve in parent plans")
    plan = matching[0]
    if (
        record["source_sha"] != plan["source_sha"]
        or record["target_repository"] != plan["target_repository"]
        or record["target_tag"] != plan["target_tag"]
        or record["index_build_key_sha256"] != plan["index_build_key_sha256"]
        or record["member_digests"]
        != [member["member_digest"] for member in plan["members"]]
    ):
        raise ValueError("index publication differs from its canonical parent plan")
    if re.fullmatch(r"[0-9a-f]{40}", record["source_sha"]) is None:
        raise ValueError("index source SHA is invalid")
    for field in (
        "index_build_key_sha256",
        "index_digest",
        "manifest_sha256",
        "authenticated_readback_sha256",
        "authenticated_closure_sha256",
        "anonymous_readback_sha256",
        "anonymous_closure_sha256",
        "record_sha256",
    ):
        _digest(record[field], f"index {field}")
    if record["manifest_sha256"] != record["index_digest"]:
        raise ValueError("index manifest digest differs from the published index")
    if not isinstance(record["member_digests"], list):
        raise ValueError("index publication requires the planned ordered members")
    for digest in record["member_digests"]:
        _digest(digest, "index member")
    if len(set(record["member_digests"])) != len(record["member_digests"]):
        raise ValueError("index publication requires unique planned members")
    _validate_collision_model(record["collision_model"], "index")
    operations = record["operations"]
    target = f"{record['target_repository']}:{record['target_tag']}"
    create_operation = {
        "type": "registry-index-create",
        "capability": "write",
        "reference": target,
    }
    if operations not in ([], [create_operation]):
        raise ValueError("index operation role must be reuse or one canonical create")
    if plan["decision"] == "reuse" and operations:
        raise ValueError("a reuse parent plan cannot claim an index create")
    if record["record_sha256"] != sha256_value(_record_payload(record)):
        raise ValueError("index publication record digest mismatch")
    return copy.deepcopy(record)


def verify_member_readback(
    member_record: object,
    readback: object,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Close a member publication over exact manifest, config and layer bytes."""
    record = validate_member_record(
        member_record,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if not isinstance(readback, dict):
        raise ValueError("member readback must be an object")
    _exact_keys(
        readback,
        {
            "schema_version",
            "kind",
            "reference",
            "digest",
            "manifest",
            "config",
            "layers",
            "children",
            "authenticated",
            "operations",
            "readback_sha256",
        },
        "member readback",
    )
    payload = {
        key: copy.deepcopy(value)
        for key, value in readback.items()
        if key != "readback_sha256"
    }
    if readback["readback_sha256"] != sha256_value(payload):
        raise ValueError("member readback digest mismatch")
    expected_reference = f"{record['staging_repository']}@{record['member_digest']}"
    if (
        readback["schema_version"] != 1
        or readback["kind"] != "ucm-registry-readback"
        or readback["reference"] != expected_reference
        or readback["digest"] != record["member_digest"]
        or readback["authenticated"] is not True
        or readback["children"] != []
        or readback["manifest"] != record["manifest"]
        or readback["config"] != record["config"]
        or readback["layers"] != record["layers"]
        or any(
            operation not in record["operations"]
            for operation in readback["operations"]
        )
        or readback["readback_sha256"] != record["readback_sha256"]
    ):
        raise ValueError("member readback differs from publication content closure")
    return copy.deepcopy(record)


def plan_staging_tag(
    build_key_sha256: object,
    member_digest: object,
    observed_digest: object | None,
    *,
    staging_repository: object,
) -> dict[str, Any]:
    """Apply the exact absent/create, same/reuse, different/fail contract."""
    repository = _repository(staging_repository)
    build_key = _digest(build_key_sha256, "staging build key")
    expected = _digest(member_digest, "staging member")
    tag = "staging-" + build_key.removeprefix("sha256:")
    if observed_digest is None:
        decision = "create"
    else:
        observed = _digest(observed_digest, "observed staging member")
        if observed != expected:
            raise ValueError(
                f"staging tag collision for {repository}:{tag}: "
                f"expected {expected}, observed {observed}"
            )
        decision = "reuse"
    return {
        "schema_version": 1,
        "kind": "ucm-registry-staging-tag-plan",
        "repository": repository,
        "tag": tag,
        "member_digest": expected,
        "decision": decision,
    }


class BarrierBlocker(ValueError):
    """A plan-member barrier failure that carries a proven-empty operation ledger."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.operations: list[dict[str, str]] = []


def _index_manifest(
    family_id: str,
    target_repository: str,
    target_tag: str,
    members: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    source_shas = {item.get("source_sha") for item in members}
    if len(source_shas) != 1:
        raise ValueError("index members must have one exact source SHA")
    source_sha = next(iter(source_shas))
    source_repository_urls = {
        item.get("content_identity", {}).get("source", {}).get("repository_url")
        for item in members
        if isinstance(item.get("content_identity"), dict)
    }
    if (
        not isinstance(source_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
    ):
        raise ValueError("index member source SHA is invalid")
    if (
        len(source_repository_urls) != 1
        or not isinstance(next(iter(source_repository_urls)), str)
        or re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            next(iter(source_repository_urls)),
        )
        is None
    ):
        raise ValueError("index members must have one source repository authority")
    source_repository_url = next(iter(source_repository_urls))
    identity = {
        "schema_version": 1,
        "source_sha": source_sha,
        "family_id": family_id,
        "target_repository": target_repository,
        "target_tag": target_tag,
        "members": [
            {
                "platform": item["platform"],
                "member_digest": item["member_digest"],
                "build_key_sha256": item["build_key_sha256"],
                "wheel_sha256": item["wheel_sha256"],
                "candidate_task_sha256": item["candidate_task_sha256"],
                "publication_task_sha256": item["publication_task_sha256"],
            }
            for item in members
        ],
    }
    index_build_key = sha256_value(identity)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": item["member_digest"],
                "size": item["member_size"],
                "platform": {
                    "os": "linux",
                    "architecture": item["platform"].split("/", 1)[1],
                },
                "annotations": {
                    "io.ucm.release.build-key-sha256": item["build_key_sha256"],
                    "io.ucm.release.spec-id": item["spec_id"],
                },
            }
            for item in members
        ],
        "annotations": {
            "org.opencontainers.image.source": source_repository_url,
            "io.ucm.release.family-id": family_id,
            "io.ucm.release.index-build-key-sha256": index_build_key,
            "io.ucm.release.source-sha": source_sha,
        },
    }
    return (
        manifest,
        index_build_key,
        "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
    )


def plan_indexes(
    member_records: object,
    inventory: object,
    *,
    member_statuses: object,
    lane: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Plan the exact index set declared by one immutable protected plan."""
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    return _plan_indexes_from_resolved_plan(
        member_records,
        inventory,
        member_statuses=member_statuses,
        lane=lane,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )


def _plan_indexes_from_resolved_plan(
    member_records: object,
    inventory: object,
    *,
    member_statuses: object,
    lane: str | None,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str | None,
) -> dict[str, Any]:
    """Plan the exact member/family sets declared by one protected plan."""
    contract = resolved_registry_contract(
        resolved_plan, expected_plan_sha256=expected_plan_sha256
    )
    authorities = contract["members"]
    if not isinstance(member_records, list) or len(member_records) != len(authorities):
        raise BarrierBlocker("member barrier differs from the frozen image task set")
    records = [
        validate_member_record(
            item,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in member_records
    ]
    records_by_task: dict[str, dict[str, Any]] = {}
    authority_by_hash = {item["candidate_task_sha256"]: item for item in authorities}
    for record in records:
        authority = authority_by_hash.get(record["candidate_task_sha256"])
        if authority is None or authority["task_id"] in records_by_task:
            raise BarrierBlocker(
                "member barrier has missing or duplicate planned tasks"
            )
        records_by_task[authority["task_id"]] = record
    task_order = [item["task_id"] for item in authorities]
    if set(records_by_task) != set(task_order):
        raise BarrierBlocker("member barrier has missing or invented planned tasks")
    if not isinstance(member_statuses, dict) or set(member_statuses) != set(task_order):
        raise BarrierBlocker("member barrier statuses differ from frozen image tasks")
    failed = sorted(
        task_id for task_id, status in member_statuses.items() if status != "success"
    )
    if failed:
        raise BarrierBlocker(
            f"member barrier blocked by unsuccessful planned tasks: {failed}"
        )
    source_sha = contract["source_sha"]
    if {item["source_sha"] for item in records} != {source_sha}:
        raise BarrierBlocker("member barrier source differs from frozen plan")
    allowed_targets = [
        (item["target_repository"], item["target_tag"]) for item in contract["indexes"]
    ]
    if not isinstance(inventory, dict):
        raise ValueError("dynamic index inventory must be a plan-bound object")
    _exact_keys(
        inventory,
        {
            "schema_version",
            "kind",
            "entries",
            "absent",
            "operations",
            "inventory_sha256",
        },
        "dynamic index inventory",
    )
    inventory_payload = {
        key: copy.deepcopy(value)
        for key, value in inventory.items()
        if key != "inventory_sha256"
    }
    if (
        inventory["schema_version"] != 1
        or isinstance(inventory["schema_version"], bool)
        or inventory["kind"] != "ucm-registry-inventory"
        or inventory["inventory_sha256"] != sha256_value(inventory_payload)
        or not isinstance(inventory["entries"], list)
        or not isinstance(inventory["absent"], list)
        or not isinstance(inventory["operations"], list)
    ):
        raise ValueError("dynamic index inventory envelope is invalid")
    inventory_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in inventory["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("dynamic index inventory entry is malformed")
        _exact_keys(
            entry,
            {"repository", "tag", "digest", "build_key_sha256"},
            "index inventory entry",
        )
        key = (entry["repository"], entry["tag"])
        if key not in set(allowed_targets) or key in inventory_by_target:
            raise ValueError("index inventory differs from frozen family targets")
        _digest(entry["digest"], "inventory index")
        _digest(entry["build_key_sha256"], "inventory index build key")
        inventory_by_target[key] = entry
    absent_targets: list[tuple[str, str]] = []
    for entry in inventory["absent"]:
        if not isinstance(entry, dict):
            raise ValueError("dynamic index absent target is malformed")
        _exact_keys(entry, {"repository", "tag"}, "dynamic index absent target")
        key = (entry["repository"], entry["tag"])
        if (
            key not in set(allowed_targets)
            or key in inventory_by_target
            or key in absent_targets
        ):
            raise ValueError("index inventory differs from frozen family targets")
        absent_targets.append(key)
    if set(inventory_by_target) | set(absent_targets) != set(allowed_targets):
        raise ValueError("index inventory coverage differs from frozen family targets")
    if [key for key in allowed_targets if key in inventory_by_target] != list(
        inventory_by_target
    ) or [key for key in allowed_targets if key in absent_targets] != absent_targets:
        raise ValueError("index inventory target ordering differs from frozen plan")
    expected_inventory_operations: list[dict[str, str]] = []
    for repository, tag in allowed_targets:
        expected_inventory_operations.append(
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": f"{repository}:{tag}",
            }
        )
        observed = inventory_by_target.get((repository, tag))
        if observed is not None:
            expected_inventory_operations.append(
                {
                    "type": "registry-authenticated-manifest-read",
                    "capability": "read",
                    "reference": f"{repository}@{observed['digest']}",
                }
            )
    if inventory["operations"] != expected_inventory_operations:
        raise ValueError("index inventory operations differ from frozen targets")
    plans: list[dict[str, Any]] = []
    operations: list[dict[str, str]] = []
    for authority in contract["indexes"]:
        grouped = [records_by_task[task_id] for task_id in authority["member_task_ids"]]
        manifest, index_build_key, expected_digest = _index_manifest(
            authority["family_id"],
            authority["target_repository"],
            authority["target_tag"],
            grouped,
        )
        coordinate = (authority["target_repository"], authority["target_tag"])
        observed = inventory_by_target.get(coordinate)
        if observed is None:
            decision = "create"
            operations.append(
                {
                    "type": "registry-index-create",
                    "capability": "write",
                    "reference": f"{coordinate[0]}:{coordinate[1]}",
                }
            )
        elif observed["build_key_sha256"] == index_build_key:
            decision = "reuse"
        else:
            raise ValueError(
                f"r1 conflict for {coordinate[0]}:{coordinate[1]}; refusing overwrite"
            )
        plans.append(
            {
                "schema_version": 1,
                "kind": "ucm-registry-index-plan",
                "source_sha": source_sha,
                "family_task_id": authority["family_task_id"],
                "family_id": authority["family_id"],
                "target_repository": authority["target_repository"],
                "target_tag": authority["target_tag"],
                "index_build_key_sha256": index_build_key,
                "expected_index_digest": expected_digest,
                "members": grouped,
                "index_manifest": manifest,
                "decision": decision,
            }
        )
    if operations and lane != "protected-tag":
        raise ValueError(f"{lane} cannot plan write-capable final index operations")
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-plans",
        "source_sha": source_sha,
        "resolved_plan_sha256": contract["resolved_plan_sha256"],
        "member_records": [records_by_task[task_id] for task_id in task_order],
        "member_statuses": {
            task_id: member_statuses[task_id] for task_id in task_order
        },
        "inventory": copy.deepcopy(inventory),
        "plans": plans,
        "operations": operations,
    }
    return {**payload, "plans_sha256": sha256_value(payload)}


def _validate_parent_plans(
    parent_plans: object,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(parent_plans, dict):
        raise ValueError("index parent plans must be an object")
    expected_fields = {
        "schema_version",
        "kind",
        "source_sha",
        "member_records",
        "member_statuses",
        "inventory",
        "plans",
        "operations",
        "plans_sha256",
    }
    expected_fields.add("resolved_plan_sha256")
    _exact_keys(parent_plans, expected_fields, "index parent plans")
    payload = {
        key: copy.deepcopy(value)
        for key, value in parent_plans.items()
        if key != "plans_sha256"
    }
    if parent_plans["plans_sha256"] != sha256_value(payload):
        raise ValueError("index parent plans digest mismatch")
    rederived = plan_indexes(
        parent_plans["member_records"],
        parent_plans["inventory"],
        member_statuses=parent_plans["member_statuses"],
        lane="protected-tag",
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if parent_plans != rederived:
        raise ValueError("index parent plans differ from frozen plan authority")
    return copy.deepcopy(parent_plans)


def validate_index_plans(
    parent_plans: object,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Publicly reopen the exact plan-member/family parent envelope."""
    return _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )


def verify_index(
    plan: object,
    *,
    parent_plans: object,
    observed: object | None = None,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Verify one canonical r1 plan and optional readback record."""
    contract = _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(plan, dict) or plan.get("kind") != "ucm-registry-index-plan":
        raise ValueError("index plan identity is invalid")
    required = {
        "schema_version",
        "kind",
        "source_sha",
        "family_id",
        "target_repository",
        "target_tag",
        "index_build_key_sha256",
        "expected_index_digest",
        "members",
        "index_manifest",
        "decision",
    }
    required.add("family_task_id")
    _exact_keys(plan, required, "index plan")
    parent = _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    matching_parent_plans = [
        item
        for item in parent["plans"]
        if (item.get("family_task_id") == plan.get("family_task_id"))
    ]
    if len(matching_parent_plans) != 1 or matching_parent_plans[0] != plan:
        raise ValueError("index plan is not the exact canonical parent plan")
    authorities = [
        item
        for item in contract["indexes"]
        if item["family_id"] == plan["family_id"]
        and item["family_task_id"] == plan["family_task_id"]
    ]
    if len(authorities) != 1:
        raise ValueError("index family is outside canonical authority")
    authority = authorities[0]
    if (
        plan["source_sha"] != parent["source_sha"]
        or plan["target_repository"] != authority["target_repository"]
        or plan["target_tag"] != authority["target_tag"]
        or [item["candidate_task_sha256"] for item in plan["members"]]
        != [
            next(
                member["candidate_task_sha256"]
                for member in contract["members"]
                if member["task_id"] == task_id
            )
            for task_id in authority["member_task_ids"]
        ]
        or plan["decision"] not in {"create", "reuse"}
    ):
        raise ValueError("index plan coordinate or decision differs from authority")
    members = [
        validate_member_record(
            item,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
        for item in plan["members"]
    ]
    manifest, build_key, digest = _index_manifest(
        plan["family_id"], plan["target_repository"], plan["target_tag"], members
    )
    if (
        plan["index_manifest"] != manifest
        or plan["index_build_key_sha256"] != build_key
        or plan["expected_index_digest"] != digest
    ):
        raise ValueError("index plan does not close over its planned members")
    if observed is not None:
        if not isinstance(observed, dict) or observed != {
            "digest": digest,
            "build_key_sha256": build_key,
        }:
            raise ValueError("index readback differs from the canonical plan")
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-verification",
        "source_sha": plan["source_sha"],
        "family_id": plan["family_id"],
        "target_repository": plan["target_repository"],
        "target_tag": plan["target_tag"],
        "index_build_key_sha256": build_key,
        "index_digest": digest,
        "status": "passed" if observed is not None else "plan-verified",
    }
    return {**payload, "verification_sha256": sha256_value(payload)}


def _registry_reference(
    value: object,
    *,
    public_targets: set[str] | None = None,
    staging_repository: str | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError("registry reference must be a string")
    public_tags = public_targets or set()
    public_repositories = {item.rsplit(":", 1)[0] for item in public_tags}
    staging = (
        _repository(staging_repository) if staging_repository is not None else None
    )
    if value in public_tags:
        return value
    repository, separator, suffix = value.rpartition("@")
    if (
        separator == "@"
        and repository in public_repositories | ({staging} if staging else set())
        and DIGEST_RE.fullmatch(suffix) is not None
    ):
        return value
    staging_prefix = staging + ":staging-" if staging else None
    if (
        staging_prefix is not None
        and value.startswith(staging_prefix)
        and re.fullmatch(r"[0-9a-f]{64}", value.removeprefix(staging_prefix))
    ):
        return value
    raise ValueError(f"registry reference is outside the exact allowlist: {value}")


def _is_retryable_secondary_limit(arguments: list[str], detail: str) -> bool:
    if not arguments or arguments[0] not in IDEMPOTENT_REGISTRY_READ_OPERATIONS:
        return False
    normalized = " ".join(detail.casefold().split())
    return any(marker in normalized for marker in SECONDARY_RATE_LIMIT_MARKERS)


def _run_registry_tool(
    binary: str,
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    missing_ok: bool = False,
) -> subprocess.CompletedProcess[str]:
    executable = _crane_binary(binary)
    for retry_index in range(len(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS) + 1):
        try:
            result = subprocess.run(
                [executable, *arguments],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=(
                    environment
                    if environment is not None
                    else _minimal_registry_environment()
                ),
                check=False,
            )
        except OSError as error:
            raise ValueError(
                f"failed to execute pinned registry tool: {error}"
            ) from error
        if result.returncode == 0:
            return result
        retryable = _is_retryable_secondary_limit(
            arguments, result.stderr + "\n" + result.stdout
        )
        if retryable and retry_index < len(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS):
            time.sleep(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS[retry_index])
            continue
        detail = result.stderr.strip() or f"exit {result.returncode}"
        if retryable or not missing_ok:
            raise ValueError(
                f"registry tool {' '.join(arguments[:1])} failed: {detail}"
            )
        return result
    raise AssertionError("registry read retry loop exhausted without a result")


def _run_registry_tool_bytes(
    binary: str,
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> bytes:
    for retry_index in range(len(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS) + 1):
        try:
            result = subprocess.run(
                [binary, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment or _minimal_registry_environment(),
                check=False,
            )
        except OSError as error:
            raise ValueError(
                f"failed to execute pinned registry tool: {error}"
            ) from error
        if result.returncode == 0:
            return result.stdout
        decoded_stderr = result.stderr.decode(errors="replace")
        decoded_stdout = result.stdout.decode(errors="replace")
        retryable = _is_retryable_secondary_limit(
            arguments, decoded_stderr + "\n" + decoded_stdout
        )
        if retryable and retry_index < len(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS):
            time.sleep(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS[retry_index])
            continue
        detail = result.stderr.decode(errors="replace").strip() or str(
            result.returncode
        )
        raise ValueError(f"registry tool {arguments[0]} failed: {detail}")
    raise AssertionError("registry byte-read retry loop exhausted without a result")


def _reference_repository(reference: str) -> str:
    if "@" in reference:
        return reference.rsplit("@", 1)[0]
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    return reference[:colon] if colon > slash else reference


def _descriptor_closure(
    descriptor: object,
    *,
    label: str,
    repository: str,
    crane_binary: str,
    environment: dict[str, str] | None,
    retain_raw: bool,
    project_layer_annotations: bool = False,
    created: object | None = None,
) -> tuple[dict[str, Any], bytes | None]:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{label} descriptor must be an object")
    media_type = descriptor.get("mediaType")
    digest = _digest(descriptor.get("digest"), f"{label} digest")
    size = descriptor.get("size")
    if (
        not isinstance(media_type, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
    ):
        raise ValueError(f"{label} descriptor is malformed")
    reference = f"{repository}@{digest}"
    raw: bytes | None
    if retain_raw:
        raw = _run_registry_tool_bytes(
            crane_binary, ["blob", reference], environment=environment
        )
        observed_size = len(raw)
        observed = "sha256:" + hashlib.sha256(raw).hexdigest()
    else:
        raw = None
        arguments = ["blob", reference]
        for retry_index in range(len(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS) + 1):
            hasher = hashlib.sha256()
            observed_size = 0
            with tempfile.TemporaryFile() as error_stream:
                try:
                    process = subprocess.Popen(
                        [crane_binary, *arguments],
                        stdout=subprocess.PIPE,
                        stderr=error_stream,
                        env=environment or _minimal_registry_environment(),
                    )
                except OSError as error:
                    raise ValueError(
                        f"failed to execute pinned registry tool: {error}"
                    ) from error
                assert process.stdout is not None
                with process.stdout:
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk:
                            break
                        observed_size += len(chunk)
                        hasher.update(chunk)
                returncode = process.wait()
                error_stream.seek(0)
                detail = error_stream.read(8192).decode(errors="replace").strip()
            if returncode == 0:
                observed = "sha256:" + hasher.hexdigest()
                break
            retryable = _is_retryable_secondary_limit(arguments, detail)
            if retryable and retry_index < len(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS):
                time.sleep(SECONDARY_RATE_LIMIT_BACKOFF_SECONDS[retry_index])
                continue
            raise ValueError(f"registry tool blob failed: {detail or returncode}")
        else:
            raise AssertionError("registry blob retry loop exhausted without a result")
    if observed != digest or observed_size != size:
        raise ValueError(f"{label} blob bytes differ from descriptor")
    closure = {
        "media_type": media_type,
        "digest": digest,
        "size": observed_size,
        "blob_sha256": observed,
    }
    if project_layer_annotations:
        annotations = _validate_layer_descriptor_annotations(
            descriptor, created=created, label=label
        )
        if annotations is not None:
            closure["annotations"] = annotations
    return closure, raw


def _missing_manifest(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    detail = (result.stderr + "\n" + result.stdout).lower()
    return any(
        marker in detail
        for marker in ("manifest_unknown", "manifest unknown", "not found", "404")
    )


def _fresh_digest(reference: str, crane_binary: str) -> str | None:
    result = _run_registry_tool(crane_binary, ["digest", reference], missing_ok=True)
    if result.returncode == 0:
        return _digest(result.stdout.strip(), "fresh registry tag")
    if _missing_manifest(result):
        return None
    detail = result.stderr.strip() or str(result.returncode)
    raise ValueError(f"fresh Registry read failed for {reference}: {detail}")


def _transport_arguments(
    operation: str, *arguments: str, insecure: bool = False
) -> list[str]:
    return [operation, *(["--insecure"] if insecure else []), *arguments]


def _fresh_transport_digest(
    reference: str,
    crane_binary: str,
    *,
    insecure: bool = False,
    environment: dict[str, str] | None = None,
) -> str | None:
    result = _run_registry_tool(
        crane_binary,
        _transport_arguments("digest", reference, insecure=insecure),
        environment=environment,
        missing_ok=True,
    )
    if result.returncode == 0:
        return _digest(result.stdout.strip(), "fresh registry transport")
    if _missing_manifest(result):
        return None
    detail = result.stderr.strip() or str(result.returncode)
    raise ValueError(f"fresh Registry read failed for {reference}: {detail}")


def inventory_registry(*, targets: object) -> dict[str, Any]:
    """Read one explicit frozen-plan target set without catalog fallback."""
    if not isinstance(targets, list) or not targets:
        raise ValueError("registry inventory targets must be a nonempty array")
    normalized_targets = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("registry inventory target must be an object")
        _exact_keys(target, {"repository", "tag"}, "registry inventory target")
        normalized = {
            "repository": _repository(target["repository"]),
            "tag": validate_public_tag(target["tag"]),
        }
        coordinate = (normalized["repository"], normalized["tag"])
        if coordinate in seen:
            raise ValueError("registry inventory targets must be unique")
        seen.add(coordinate)
        normalized_targets.append(normalized)
    executable = resolve_pinned_crane()
    entries: list[dict[str, Any]] = []
    absent: list[dict[str, str]] = []
    operations: list[dict[str, str]] = []
    for target in normalized_targets:
        reference = f"{target['repository']}:{target['tag']}"
        operations.append(
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": reference,
            }
        )
        digest_result = _run_registry_tool(
            executable, ["digest", reference], missing_ok=True
        )
        if digest_result.returncode != 0:
            if not _missing_manifest(digest_result):
                detail = digest_result.stderr.strip() or str(digest_result.returncode)
                raise ValueError(f"registry inventory failed for {reference}: {detail}")
            absent.append(
                {
                    "repository": target["repository"],
                    "tag": target["tag"],
                }
            )
            continue
        digest = _digest(digest_result.stdout.strip(), "inventory index")
        digest_reference = f"{target['repository']}@{digest}"
        operations.append(
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": digest_reference,
            }
        )
        manifest_result = _run_registry_tool(executable, ["manifest", digest_reference])
        manifest = _unique_json(manifest_result.stdout, "inventory index manifest")
        annotations = manifest.get("annotations")
        if not isinstance(annotations, dict):
            raise ValueError("public index manifest is missing annotations")
        build_key = _digest(
            annotations.get("io.ucm.release.index-build-key-sha256"),
            "inventory index build key",
        )
        entries.append(
            {
                "repository": target["repository"],
                "tag": target["tag"],
                "digest": digest,
                "build_key_sha256": build_key,
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-inventory",
        "entries": entries,
        "absent": absent,
        "operations": operations,
    }
    return {**payload, "inventory_sha256": sha256_value(payload)}


def readback_reference(
    reference: object,
    *,
    anonymous: bool = False,
    public_targets: set[str] | None = None,
    staging_repository: str | None = None,
) -> dict[str, Any]:
    """Read manifest/config/layer bytes with isolated anonymous credentials."""
    canonical_reference = _registry_reference(
        reference,
        public_targets=public_targets,
        staging_repository=staging_repository,
    )
    executable = resolve_pinned_crane()

    def read(environment: dict[str, str] | None) -> dict[str, Any]:
        prefix = "registry-anonymous" if anonymous else "registry-authenticated"
        digest_result = _run_registry_tool(
            executable, ["digest", canonical_reference], environment=environment
        )
        digest = _digest(digest_result.stdout.strip(), "registry readback")
        repository = _reference_repository(canonical_reference)
        digest_reference = f"{repository}@{digest}"
        manifest_raw = _run_registry_tool_bytes(
            executable,
            ["manifest", digest_reference],
            environment=environment,
        )
        if "sha256:" + hashlib.sha256(manifest_raw).hexdigest() != digest:
            raise ValueError("registry manifest raw bytes differ from resolved digest")
        manifest_json = _unique_json(
            manifest_raw.decode("utf-8"), "registry readback manifest"
        )
        media_type = manifest_json.get("mediaType")
        manifest = {
            "media_type": media_type,
            "digest": digest,
            "size": len(manifest_raw),
            "annotations": copy.deepcopy(manifest_json.get("annotations", {})),
        }
        config: dict[str, Any] | None = None
        layers: list[dict[str, Any]] = []
        children: list[dict[str, Any]] = []
        closure_operations: list[dict[str, str]] = []
        if media_type in OCI_MANIFEST_MEDIA_TYPES:
            config, config_raw = _descriptor_closure(
                manifest_json.get("config"),
                label="registry config",
                repository=repository,
                crane_binary=executable,
                environment=environment,
                retain_raw=True,
            )
            assert config_raw is not None
            config_json = _unique_json(
                config_raw.decode("utf-8"), "registry config blob"
            )
            image_config = config_json.get("config", {})
            labels = (
                image_config.get("Labels", {}) if isinstance(image_config, dict) else {}
            )
            if not isinstance(labels, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in labels.items()
            ):
                raise ValueError("registry config labels must be a string map")
            config["labels"] = copy.deepcopy(labels)
            closure_operations.append(
                {
                    "type": f"{prefix}-config-blob-read",
                    "capability": "read",
                    "reference": f"{repository}@{config['digest']}",
                }
            )
            layer_descriptors = manifest_json.get("layers")
            if not isinstance(layer_descriptors, list):
                raise ValueError("registry member manifest lacks layers")
            for position, descriptor in enumerate(layer_descriptors):
                closure, _ = _descriptor_closure(
                    descriptor,
                    label=f"registry layer {position}",
                    repository=repository,
                    crane_binary=executable,
                    environment=environment,
                    retain_raw=False,
                    project_layer_annotations=True,
                    created=config_json.get("created"),
                )
                layers.append(closure)
                closure_operations.append(
                    {
                        "type": f"{prefix}-layer-blob-read",
                        "capability": "read",
                        "reference": f"{repository}@{closure['digest']}",
                    }
                )
        elif media_type in OCI_INDEX_MEDIA_TYPES:
            descriptors = manifest_json.get("manifests")
            if not isinstance(descriptors, list):
                raise ValueError("registry index lacks manifests")
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    raise ValueError("registry index child descriptor is malformed")
                children.append(copy.deepcopy(descriptor))
        else:
            raise ValueError("registry readback media type is unsupported")
        operations = [
            {
                "type": f"{prefix}-digest-read",
                "capability": "read",
                "reference": canonical_reference,
            },
            {
                "type": f"{prefix}-manifest-read",
                "capability": "read",
                "reference": digest_reference,
            },
            *closure_operations,
        ]
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": canonical_reference,
            "digest": digest,
            "manifest": manifest,
            "config": config,
            "layers": layers,
            "children": children,
            "authenticated": not anonymous,
            "operations": operations,
        }
        return {**payload, "readback_sha256": sha256_value(payload)}

    if not anonymous:
        return read(_minimal_registry_environment())
    with tempfile.TemporaryDirectory(
        prefix="ucm-anonymous-docker-config-"
    ) as directory:
        config = Path(directory) / "config.json"
        config.write_bytes(b'{"auths":{}}\n')
        environment = _minimal_registry_environment(docker_config=directory)
        return read(environment)


def verify_private_staging(
    reference: object, *, staging_repository: str, phase: str = "postwrite"
) -> dict[str, Any]:
    """Prove private visibility only from a typed anonymous authorization denial."""
    if phase not in {"prewrite", "postwrite"}:
        raise ValueError("private staging visibility phase is noncanonical")
    repository = _repository(staging_repository)
    canonical_reference = _registry_reference(reference, staging_repository=repository)
    if not canonical_reference.startswith(repository + ":staging-"):
        raise ValueError("private visibility evidence requires an exact staging tag")
    executable = resolve_pinned_crane()
    with tempfile.TemporaryDirectory(
        prefix="ucm-anonymous-docker-config-"
    ) as directory:
        (Path(directory) / "config.json").write_bytes(b'{"auths":{}}\n')
        result = _run_registry_tool(
            executable,
            ["digest", canonical_reference],
            environment=_minimal_registry_environment(docker_config=directory),
            missing_ok=True,
        )
    if result.returncode == 0:
        raise ValueError("staging reference is anonymously public")
    detail = result.stderr + "\n" + result.stdout
    line_code_denial = any(
        re.search(pattern, detail, flags=re.IGNORECASE | re.MULTILINE) is not None
        for pattern in (
            r"^\s*UNAUTHORIZED\s*:",
            r"^\s*DENIED\s*:",
        )
    )
    staging_path = repository.removeprefix("ghcr.io/")
    token_scope = urllib.parse.quote(f"repository:{staging_path}:pull", safe="")
    exact_token_url = f"https://ghcr.io/token?scope={token_scope}&service=ghcr.io"
    staging_tag = canonical_reference.removeprefix(repository + ":")
    exact_manifest_url = f"https://ghcr.io/v2/{staging_path}/manifests/{staging_tag}"
    ghcr_token_denial = any(
        exact_token_url in line
        and re.search(r":\s*(?:UNAUTHORIZED|DENIED)\s*:", line) is not None
        for line in detail.splitlines()
    )
    ghcr_manifest_denial = any(
        exact_manifest_url in line
        and re.search(r":\s*(?:UNAUTHORIZED|DENIED)\s*:", line) is not None
        for line in detail.splitlines()
    )
    typed_denial = line_code_denial or ghcr_token_denial or ghcr_manifest_denial
    if not typed_denial:
        raise ValueError("anonymous read failed without an authorization denial")
    operation = {
        "type": (
            "registry-anonymous-prewrite-visibility-read"
            if phase == "prewrite"
            else "registry-anonymous-visibility-read"
        ),
        "capability": "read",
        "reference": canonical_reference,
    }
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-private-visibility-evidence",
        "status": "anonymous-denied",
        "phase": phase,
        "returncode": result.returncode,
        "stdout_sha256": "sha256:" + hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": "sha256:" + hashlib.sha256(result.stderr.encode()).hexdigest(),
        "operation": operation,
    }
    return {**payload, "visibility_evidence_sha256": sha256_value(payload)}


def _fresh_write_authority(
    lane: object,
    *,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    task_kind: str,
    task_id: str,
) -> dict[str, Any]:
    if lane != "protected-tag":
        raise ValueError("registry writes require the protected-tag lane")
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if task_kind not in {"image", "family"} or not isinstance(task_id, str):
        raise ValueError(
            "production Registry write requires one frozen resolved plan task"
        )
    selected = select_task(
        resolved_plan,
        task_kind=task_kind,
        task_id=task_id,
        expected_plan_sha256=expected_plan_sha256,
    )
    preflight = core.tag_preflight(
        lane="protected-tag",
        authority=resolved_plan["source"],
    )
    if (
        preflight.get("kind") != "ucm-tag-preflight"
        or preflight.get("lane") != "protected-tag"
        or preflight.get("publication_allowed") is not True
        or preflight.get("write_authority")
        != ["github-prerelease", "ghcr-final-index", "ghcr-private-staging"]
    ):
        raise ValueError("fresh protected Tag preflight did not grant write authority")
    if resolved_plan["source"]["commit"] != preflight.get("source_sha"):
        raise ValueError("fresh protected Tag source differs from frozen plan")
    if selected.get("write_authority") != [
        "github-prerelease",
        "ghcr-final-index",
        "ghcr-private-staging",
    ]:
        raise ValueError("frozen plan task did not grant fresh write authority")
    return {
        "preflight": preflight,
        "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
        "selected_task": selected,
    }


def _oci_blob(
    layout_dir: Path,
    descriptor: object,
    label: str,
    *,
    retain_raw: bool,
) -> tuple[bytes | None, str]:
    if not isinstance(descriptor, dict) or set(descriptor) - {
        "mediaType",
        "digest",
        "size",
        "annotations",
        "platform",
    }:
        raise ValueError(f"{label} descriptor is malformed")
    digest = _digest(descriptor.get("digest"), f"{label} digest")
    size = descriptor.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError(f"{label} descriptor size is invalid")
    algorithm, encoded = digest.split(":", 1)
    path = layout_dir / "blobs" / algorithm / encoded
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} blob is missing from OCI layout")
    hasher = hashlib.sha256()
    observed_size = 0
    retained = bytearray() if retain_raw else None
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            observed_size += len(chunk)
            hasher.update(chunk)
            if retained is not None:
                retained.extend(chunk)
    actual = "sha256:" + hasher.hexdigest()
    if observed_size != size or actual != digest:
        raise ValueError(f"{label} blob bytes differ from descriptor")
    return bytes(retained) if retained is not None else None, digest


@contextlib.contextmanager
def materialize_oci_layout(archive_path: Path):
    """Safely reopen one Buildx OCI-layout tar as the directory crane expects."""
    archive = Path(archive_path)
    if not archive.is_file() or archive.is_symlink():
        raise ValueError("Buildx OCI archive must be one regular file")
    with tempfile.TemporaryDirectory(prefix="ucm-oci-layout-") as directory:
        root = Path(directory)
        try:
            bundle = tarfile.open(archive, mode="r:*")
        except (OSError, tarfile.TarError) as error:
            raise ValueError(f"Buildx OCI archive is unreadable: {error}") from error
        with bundle:
            seen: set[str] = set()
            for member in bundle.getmembers():
                name = PurePosixPath(member.name)
                if (
                    not member.name
                    or member.name.startswith("/")
                    or "\\" in member.name
                    or any(part in {"", ".", ".."} for part in name.parts)
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                    or getattr(member, "sparse", None)
                ):
                    raise ValueError(
                        f"Buildx OCI archive contains unsafe member {member.name!r}"
                    )
                canonical_name = name.as_posix()
                if canonical_name in seen:
                    raise ValueError(
                        f"Buildx OCI archive contains duplicate path {canonical_name!r}"
                    )
                seen.add(canonical_name)
                target = root.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError(
                        f"Buildx OCI archive member is not a regular file: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read OCI archive member {member.name}")
                with source, target.open("xb") as stream:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
        layout = _unique_json(
            (root / "oci-layout").read_text(encoding="utf-8"), "OCI layout marker"
        )
        if layout != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Buildx OCI layout marker is noncanonical")
        index = _unique_json(
            (root / "index.json").read_text(encoding="utf-8"), "OCI layout index"
        )
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or len(descriptors) != 1:
            raise ValueError("member OCI layout must contain exactly one manifest")
        manifest_raw, manifest_digest = _oci_blob(
            root, descriptors[0], "OCI member manifest", retain_raw=True
        )
        assert manifest_raw is not None
        manifest = _unique_json(manifest_raw.decode("utf-8"), "OCI member manifest")
        if manifest.get("mediaType") not in OCI_MANIFEST_MEDIA_TYPES:
            raise ValueError("OCI member has an unsupported manifest media type")
        config_raw, config_digest = _oci_blob(
            root, manifest.get("config"), "OCI member config", retain_raw=True
        )
        assert config_raw is not None
        config = _unique_json(config_raw.decode("utf-8"), "OCI member config")
        layers = manifest.get("layers")
        if not isinstance(layers, list):
            raise ValueError("OCI member layers must be an array")
        for position, descriptor in enumerate(layers):
            _oci_blob(
                root,
                descriptor,
                f"OCI member layer {position}",
                retain_raw=False,
            )
        yield {
            "layout_dir": root,
            "index": index,
            "manifest": manifest,
            "manifest_digest": manifest_digest,
            "config": config,
            "config_digest": config_digest,
            "layers": copy.deepcopy(layers),
        }


def _push_materialized_member(
    materialized: dict[str, Any],
    *,
    repository: str,
    crane_binary: str,
    insecure: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Shared production/loopback OCI-layout-directory push executor."""
    digest = materialized["manifest_digest"]
    target = f"{repository}@{digest}"
    observed = _fresh_transport_digest(
        target, crane_binary, insecure=insecure, environment=environment
    )
    decision = "reuse" if observed == digest else "create"
    if observed is not None and observed != digest:
        raise ValueError("digest coordinate returned impossible content drift")
    operations: list[dict[str, str]] = []
    if decision == "create":
        push_result = _run_registry_tool(
            crane_binary,
            _transport_arguments(
                "push",
                str(materialized["layout_dir"]),
                target,
                insecure=insecure,
            ),
            environment=environment,
        )
        if push_result.stdout.strip() != target:
            raise ValueError(
                "crane push stdout did not report the exact full reference coordinate"
            )
        operations.append(
            {
                "type": "registry-member-push-by-digest",
                "capability": "write",
                "reference": target,
            }
        )
    if (
        _fresh_transport_digest(
            target, crane_binary, insecure=insecure, environment=environment
        )
        != digest
    ):
        raise ValueError("member push post-write digest readback drifted")
    return {"decision": decision, "digest": digest, "operations": operations}


def _apply_digest_tag(
    *,
    repository: str,
    digest: str,
    tag: str,
    crane_binary: str,
    insecure: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Shared production/loopback fresh create-or-reuse tag executor."""
    reference = f"{repository}:{tag}"
    observed = _fresh_transport_digest(
        reference, crane_binary, insecure=insecure, environment=environment
    )
    if observed is not None and observed != digest:
        raise ValueError(f"registry tag collision for {reference}")
    operations: list[dict[str, str]] = []
    decision = "reuse" if observed == digest else "create"
    if decision == "create":
        _run_registry_tool(
            crane_binary,
            _transport_arguments(
                "tag", f"{repository}@{digest}", tag, insecure=insecure
            ),
            environment=environment,
        )
        operations.append(
            {
                "type": "registry-staging-tag-create",
                "capability": "write",
                "reference": reference,
            }
        )
    if (
        _fresh_transport_digest(
            reference, crane_binary, insecure=insecure, environment=environment
        )
        != digest
    ):
        raise ValueError("registry tag post-write digest readback drifted")
    return {
        "decision": decision,
        "collision_model": _collision_model_evidence(),
        "operations": operations,
    }


def _validate_image_result_content_identity(result: dict[str, Any]) -> dict[str, Any]:
    identity = result.get("content_identity")
    if not isinstance(identity, dict):
        raise ValueError("image result content identity must be an object")
    _exact_keys(identity, CONTENT_IDENTITY_KEYS, "image result content identity")
    stable = {
        key: copy.deepcopy(value)
        for key, value in identity.items()
        if key != "content_identity_sha256"
    }
    if (
        identity["content_identity_sha256"] != sha256_value(stable)
        or result.get("content_identity_sha256") != identity["content_identity_sha256"]
    ):
        raise ValueError("image result content identity digest mismatch")
    source = identity.get("source")
    result_source = result.get("source")
    if not isinstance(source, dict) or not isinstance(result_source, dict):
        raise ValueError("image result content identity source is missing")
    _exact_keys(source, CONTENT_IDENTITY_SOURCE_KEYS, "image result content source")
    if any(
        result_source.get(key) != source[key] for key in CONTENT_IDENTITY_SOURCE_KEYS
    ):
        raise ValueError("image result content identity source differs from result")
    expected_values = {
        "task_sha256": result.get("task_key"),
        "build_key_sha256": result.get("build_key_sha256"),
        "wheel_sha256": result.get("wheel", {}).get("sha256"),
        "recipe_sha256": result.get("recipe_sha256"),
    }
    if any(identity.get(key) != value for key, value in expected_values.items()):
        raise ValueError("image result content identity task/build closure differs")
    if identity.get("manifest_digest") != result.get("oci", {}).get("digest"):
        raise ValueError("image result content identity manifest differs")
    expected_annotations = {
        "io.ucm.release.recipe-sha256": result.get("recipe_sha256"),
        "io.ucm.release.task-sha256": result.get("task_key"),
    }
    if identity.get("annotations") != expected_annotations:
        raise ValueError("image result content identity annotations differ")
    labels = identity.get("labels")
    expected_labels = {
        "org.opencontainers.image.source": source["repository_url"],
        "org.opencontainers.image.revision": source["commit"],
        "io.ucm.release.source-tree": source["tree"],
        "io.ucm.release.source-context-sha256": source["context_sha256"],
        "io.ucm.release.task-sha256": result.get("task_key"),
        "io.ucm.release.build-key-sha256": result.get("build_key_sha256"),
        "io.ucm.release.wheel-sha256": result.get("wheel", {}).get("sha256"),
        "io.ucm.release.recipe-sha256": result.get("recipe_sha256"),
    }
    if (
        not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected_labels.items())
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in labels.items()
        )
    ):
        raise ValueError("image result content identity labels differ")
    layers = identity.get("layers")
    diff_ids = identity.get("diff_ids")
    if (
        not isinstance(layers, list)
        or not layers
        or not isinstance(diff_ids, list)
        or len(diff_ids) != len(layers)
    ):
        raise ValueError("image result content identity layer closure is invalid")
    for position, (layer, diff_id) in enumerate(zip(layers, diff_ids, strict=True)):
        if not isinstance(layer, dict):
            raise ValueError(
                f"image result content identity layer {position} is invalid"
            )
        _validate_layer_descriptor_annotations(
            layer,
            created=identity.get("created"),
            label=f"image result content identity layer {position}",
        )
        _digest(layer["digest"], f"image result content layer {position}")
        _digest(diff_id, f"image result content diff-id {position}")
        if (
            not isinstance(layer["mediaType"], str)
            or not layer["mediaType"].startswith("application/vnd.")
            or not isinstance(layer["size"], int)
            or isinstance(layer["size"], bool)
            or layer["size"] < 1
        ):
            raise ValueError(
                f"image result content identity layer {position} is invalid"
            )
    _created_epoch(identity.get("created"), "image result content identity")
    if (
        not isinstance(identity.get("history"), list)
        or not identity["history"]
        or any(not isinstance(item, dict) for item in identity["history"])
    ):
        raise ValueError("image result content identity history is invalid")
    return copy.deepcopy(identity)


def publish_member(
    archive_path: Path,
    *,
    image_result: object,
    lane: str,
    selected_task: dict[str, Any],
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Publish one real candidate and derive its record only from trusted readback."""
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(selected_task, dict):
        raise ValueError("member publication requires one frozen image task")
    write_authority = _fresh_write_authority(
        lane,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
        task_kind="image",
        task_id=selected_task.get("task_id"),
    )
    from . import image

    result = image.validate_image_result(
        image_result,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
        task_id=selected_task.get("task_id"),
    )
    if (
        result.get("candidate_kind") != "real-candidate"
        or result.get("unpublished") is not True
        or result.get("oci", {}).get("published") is not False
    ):
        raise ValueError("member publication requires a real unpublished image result")
    selected = select_task(
        resolved_plan,
        task_kind="image",
        task_id=selected_task.get("task_id"),
        expected_plan_sha256=expected_plan_sha256,
    )
    if selected != selected_task:
        raise ValueError("selected member task differs from frozen plan")
    matches = [
        item
        for item in resolved_registry_contract(
            resolved_plan, expected_plan_sha256=expected_plan_sha256
        )["members"]
        if item["task_id"] == selected["task_id"]
    ]
    if len(matches) != 1:
        raise ValueError("selected member task does not resolve in Registry authority")
    authority = matches[0]
    expected_authority = {
        "spec_id": result.get("spec_id"),
        "profile_id": result.get("profile_id"),
        "family_id": result.get("family_id"),
        "platform": result.get("target_platform"),
        "target_repository": result.get("target_repository"),
        "target_tag": result.get("target_tag"),
        "candidate_task_sha256": result.get("task_key"),
    }
    if any(authority[key] != value for key, value in expected_authority.items()):
        raise ValueError("image result differs from canonical member authority")
    if result.get("source", {}).get("commit") != write_authority["preflight"].get(
        "source_sha"
    ):
        raise ValueError("member source differs from live protected tag")
    staging_repository = _repository(resolved_plan["source"].get("staging_repository"))
    identity = _validate_image_result_content_identity(result)
    crane_binary = resolve_pinned_crane()
    staging_tag = "staging-" + result["build_key_sha256"].removeprefix("sha256:")
    staging_reference = f"{staging_repository}:{staging_tag}"
    with materialize_oci_layout(archive_path) as materialized:
        descriptor = materialized["index"]["manifests"][0]
        manifest = materialized["manifest"]
        config = materialized["config"]
        expected_layers = manifest.get("layers")
        if (
            not isinstance(identity, dict)
            or materialized["manifest_digest"] != result.get("oci", {}).get("digest")
            or materialized["manifest_digest"] != identity.get("manifest_digest")
            or materialized["config_digest"] != identity.get("config_digest")
            or manifest.get("annotations", {}) != identity.get("annotations")
            or config.get("config", {}).get("Labels", {}) != identity.get("labels")
            or config.get("rootfs", {}).get("diff_ids") != identity.get("diff_ids")
            or config.get("created") != identity.get("created")
            or config.get("history") != identity.get("history")
            or expected_layers != identity.get("layers")
        ):
            raise ValueError(
                "Buildx OCI bytes differ from image-result content identity"
            )
        prewrite_visibility = verify_private_staging(
            staging_reference,
            staging_repository=staging_repository,
            phase="prewrite",
        )
        operations: list[dict[str, str]] = [
            copy.deepcopy(prewrite_visibility["operation"]),
            {
                "type": "registry-authenticated-staging-prewrite-read",
                "capability": "read",
                "reference": staging_reference,
            },
        ]
        observed_staging_digest = _fresh_transport_digest(
            staging_reference, crane_binary
        )
        staging_plan = plan_staging_tag(
            result["build_key_sha256"],
            result["oci"]["digest"],
            observed_staging_digest,
            staging_repository=staging_repository,
        )
        core.require_default_head_for_create(
            write_authority["preflight"],
            staging_plan["decision"],
            resource=staging_reference,
        )
        push_result = _push_materialized_member(
            materialized,
            repository=staging_repository,
            crane_binary=crane_binary,
        )
        operations.extend(push_result["operations"])
        member_size = descriptor["size"]
        manifest_record = {
            "media_type": manifest["mediaType"],
            "digest": materialized["manifest_digest"],
            "size": member_size,
            "annotations": copy.deepcopy(manifest.get("annotations", {})),
        }
        config_descriptor = manifest["config"]
        config_record = {
            "media_type": config_descriptor["mediaType"],
            "digest": materialized["config_digest"],
            "size": config_descriptor["size"],
            "blob_sha256": materialized["config_digest"],
            "labels": copy.deepcopy(config.get("config", {}).get("Labels", {})),
        }
        layer_records = []
        for item in expected_layers:
            layer_record = {
                "media_type": item["mediaType"],
                "digest": item["digest"],
                "size": item["size"],
                "blob_sha256": item["digest"],
            }
            if "annotations" in item:
                layer_record["annotations"] = copy.deepcopy(item["annotations"])
            layer_records.append(layer_record)

    tag_result = _apply_digest_tag(
        repository=staging_repository,
        digest=result["oci"]["digest"],
        tag=staging_tag,
        crane_binary=crane_binary,
    )
    operations.extend(tag_result["operations"])
    digest_reference = f"{staging_repository}@{result['oci']['digest']}"
    readback = readback_reference(
        digest_reference, staging_repository=staging_repository
    )
    operations.extend(copy.deepcopy(readback["operations"]))
    visibility = verify_private_staging(
        staging_reference, staging_repository=staging_repository
    )
    operations.append(copy.deepcopy(visibility["operation"]))
    annotations = {
        "io.ucm.release.build-key-sha256": result["build_key_sha256"],
        "io.ucm.release.candidate-task-sha256": result["task_key"],
        "io.ucm.release.family-id": authority["family_id"],
        "io.ucm.release.platform": authority["platform"],
        "io.ucm.release.spec-id": authority["spec_id"],
        "io.ucm.release.wheel-sha256": result["wheel"]["sha256"],
    }
    record_authority = {
        key: authority[key]
        for key in (
            "spec_id",
            "profile_id",
            "family_id",
            "platform",
            "target_repository",
            "target_tag",
            "candidate_task_sha256",
            "publication_task_sha256",
        )
    }
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-member-publication",
        "status": "passed",
        **copy.deepcopy(record_authority),
        "staging_repository": staging_repository,
        "staging_visibility": "private",
        "staging_tag": staging_tag,
        "build_key_sha256": result["build_key_sha256"],
        "wheel_sha256": result["wheel"]["sha256"],
        "member_digest": result["oci"]["digest"],
        "member_size": member_size,
        "config_digest": materialized["config_digest"],
        "annotations": annotations,
        "source_sha": result["source"]["commit"],
        "image_result_sha256": result["result_sha256"],
        "recipe_sha256": result["recipe_sha256"],
        "content_identity_sha256": result["content_identity_sha256"],
        "content_identity": copy.deepcopy(result["content_identity"]),
        "manifest": manifest_record,
        "config": config_record,
        "layers": layer_records,
        "readback_sha256": readback["readback_sha256"],
        "prewrite_visibility_evidence_sha256": prewrite_visibility[
            "visibility_evidence_sha256"
        ],
        "visibility_evidence_sha256": visibility["visibility_evidence_sha256"],
        "collision_model": copy.deepcopy(tag_result["collision_model"]),
        "operations": operations,
    }
    record = {**payload, "record_sha256": sha256_value(payload)}
    verify_member_readback(
        record,
        readback,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    return record


def _require_member_record_for_selected_image_task(
    record: dict[str, Any], selected_task: dict[str, Any]
) -> None:
    """Bind a validated publication record to the caller-selected image task."""
    expected = {
        "spec_id": selected_task.get("spec_id"),
        "profile_id": selected_task.get("profile_id"),
        "family_id": selected_task.get("family_task_id"),
        "platform": selected_task.get("platform"),
        "target_repository": selected_task.get("target_repository"),
        "target_tag": selected_task.get("target_tag"),
        "candidate_task_sha256": selected_task.get("task_sha256"),
        "publication_task_sha256": selected_task.get("task_sha256"),
    }
    if any(record.get(field) != value for field, value in expected.items()):
        raise ValueError("member record differs from the selected image task")


def push_member_by_digest(
    archive_path: Path,
    member_record: object,
    *,
    lane: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    task_id: str,
) -> dict[str, Any]:
    """Push one safely reopened Buildx OCI layout to the staging content digest."""
    authority = _fresh_write_authority(
        lane,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
        task_kind="image",
        task_id=task_id,
    )
    record = validate_member_record(
        member_record,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    _require_member_record_for_selected_image_task(record, authority["selected_task"])
    staging_repository = _repository(resolved_plan["source"].get("staging_repository"))
    crane_binary = resolve_pinned_crane()
    with materialize_oci_layout(archive_path) as materialized:
        descriptor = materialized["index"]["manifests"][0]
        if (
            materialized["manifest_digest"] != record["member_digest"]
            or descriptor.get("size") != record["member_size"]
            or materialized["config_digest"] != record["config_digest"]
            or materialized["manifest"].get("annotations")
            != record["manifest"]["annotations"]
            or materialized["config"].get("config", {}).get("Labels", {})
            != record["config"]["labels"]
            or materialized["manifest"].get("layers")
            != [
                {
                    "mediaType": item["media_type"],
                    "digest": item["digest"],
                    "size": item["size"],
                    **(
                        {"annotations": copy.deepcopy(item["annotations"])}
                        if "annotations" in item
                        else {}
                    ),
                }
                for item in record["layers"]
            ]
        ):
            raise ValueError("Buildx OCI layout differs from member publication record")
        push = _push_materialized_member(
            materialized,
            repository=staging_repository,
            crane_binary=crane_binary,
        )
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-member-push",
        "digest": push["digest"],
        "record_sha256": record["record_sha256"],
        "preflight_sha256": authority["preflight"].get("preflight_sha256"),
        "resolved_plan_sha256": authority["resolved_plan_sha256"],
        "operations": push["operations"],
    }
    return {**payload, "push_sha256": sha256_value(payload)}


def apply_staging_tag(
    member_record: object,
    *,
    lane: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
    task_id: str,
) -> dict[str, Any]:
    """Create a GC tag only when absent; identity is a read-only reuse."""
    authority = _fresh_write_authority(
        lane,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
        task_kind="image",
        task_id=task_id,
    )
    record = validate_member_record(
        member_record,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    _require_member_record_for_selected_image_task(record, authority["selected_task"])
    staging_repository = _repository(resolved_plan["source"].get("staging_repository"))
    crane_binary = resolve_pinned_crane()
    transport = _apply_digest_tag(
        repository=staging_repository,
        digest=record["member_digest"],
        tag=record["staging_tag"],
        crane_binary=crane_binary,
    )
    plan = plan_staging_tag(
        record["build_key_sha256"],
        record["member_digest"],
        record["member_digest"] if transport["decision"] == "reuse" else None,
        staging_repository=staging_repository,
    )
    return {
        **plan,
        "collision_model": copy.deepcopy(transport["collision_model"]),
        "operations": transport["operations"],
    }


def _run_imagetools(
    buildx_command: str | tuple[str, ...],
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> bytes:
    command = (buildx_command,) if isinstance(buildx_command, str) else buildx_command
    try:
        result = subprocess.run(
            [*command, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment or _minimal_registry_environment(),
            check=False,
        )
    except OSError as error:
        raise ValueError(
            f"failed to execute pinned Buildx imagetools: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ValueError(
            f"Buildx imagetools create failed: {detail or result.returncode}"
        )
    return result.stdout


def _create_index_transport(
    *,
    common_arguments: list[str],
    target: str,
    expected_manifest: dict[str, Any] | None,
    inventory_digest: str | None,
    requested_decision: str,
    buildx_command: tuple[str, ...],
    crane_binary: str,
    insecure: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Shared production/loopback Buildx dry-run, create, and raw readback executor."""
    dry_stdout = _run_imagetools(
        buildx_command,
        [*common_arguments, "--dry-run"],
        environment=environment,
    )
    if not dry_stdout.endswith(b"\n") or dry_stdout[:-1].endswith(b"\n"):
        raise ValueError("docker imagetools dry-run must append exactly one LF")
    raw_manifest = dry_stdout[:-1]
    rendered = _unique_json(
        raw_manifest.decode("utf-8"), "docker imagetools dry-run manifest"
    )
    if expected_manifest is not None and rendered != expected_manifest:
        raise ValueError("docker imagetools dry-run differs from exact index intent")
    expected_digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    if inventory_digest is not None and inventory_digest != expected_digest:
        raise ValueError(
            f"r1 conflict for {target}; inventory bytes differ from Buildx dry-run"
        )
    fresh_digest = _fresh_transport_digest(
        target, crane_binary, insecure=insecure, environment=environment
    )
    if fresh_digest is not None and fresh_digest != expected_digest:
        raise ValueError(
            f"fresh final r1 conflict for {target}: observed {fresh_digest}"
        )
    decision = "reuse" if fresh_digest == expected_digest else "create"
    if requested_decision == "reuse" and decision != "reuse":
        raise ValueError("fresh final r1 is missing after a caller reuse decision")
    operations: list[dict[str, str]] = []
    if decision == "create":
        _run_imagetools(
            buildx_command,
            common_arguments,
            environment=environment,
        )
        operations.append(
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": target,
            }
        )
    if (
        _fresh_transport_digest(
            target, crane_binary, insecure=insecure, environment=environment
        )
        != expected_digest
    ):
        raise ValueError("final index post-write digest readback drifted")
    repository = _reference_repository(target)
    postwrite_raw = _run_registry_tool_bytes(
        crane_binary,
        _transport_arguments(
            "manifest", f"{repository}@{expected_digest}", insecure=insecure
        ),
        environment=environment,
    )
    if postwrite_raw != raw_manifest:
        raise ValueError("final index post-write manifest bytes differ from dry-run")
    return {
        "rendered": rendered,
        "raw_manifest": raw_manifest,
        "index_digest": expected_digest,
        "decision": decision,
        "collision_model": _collision_model_evidence(),
        "operations": operations,
        "postwrite_manifest_sha256": "sha256:"
        + hashlib.sha256(postwrite_raw).hexdigest(),
    }


PROVISIONAL_INDEX_KEYS = {
    "schema_version",
    "kind",
    "status",
    "source_sha",
    "family_id",
    "target_repository",
    "target_tag",
    "index_build_key_sha256",
    "index_digest",
    "manifest_sha256",
    "member_digests",
    "authenticated_readback",
    "authenticated_closure",
    "collision_model",
    "operations",
    "decision",
    "postwrite_manifest_sha256",
    "preflight_sha256",
    "verification_sha256",
    "parent_plans_sha256",
    "provisional_sha256",
}
DYNAMIC_PROVISIONAL_INDEX_KEYS = PROVISIONAL_INDEX_KEYS | {
    "resolved_plan_sha256",
    "family_task_id",
    "family_task_sha256",
}
INDEX_READBACK_KEYS = {
    "schema_version",
    "kind",
    "reference",
    "digest",
    "manifest",
    "config",
    "layers",
    "children",
    "authenticated",
    "operations",
    "readback_sha256",
}
INDEX_READBACK_MANIFEST_KEYS = {
    "media_type",
    "digest",
    "size",
    "annotations",
}
FINALIZED_INDEX_KEYS = {
    "schema_version",
    "kind",
    "status",
    "family_id",
    "record",
    "provisional",
    "provisional_sha256",
    "authenticated_readback",
    "anonymous_readback",
    "anonymous_closure",
    "operation_audit",
    "finalization_sha256",
}


def _validate_prepared_index_readback(
    readback: object,
    *,
    plan: dict[str, Any],
    expected_digest: str,
    authenticated: bool,
) -> dict[str, Any]:
    """Reopen one exact final-index read without trusting caller coordinates."""
    from . import verify

    if not isinstance(readback, dict):
        raise ValueError("prepared index readback must be an object")
    _exact_keys(readback, INDEX_READBACK_KEYS, "prepared index readback")
    expected_reference = f"{plan['target_repository']}:{plan['target_tag']}"
    expected_manifest = plan["index_manifest"]
    manifest = readback["manifest"]
    if not isinstance(manifest, dict):
        raise ValueError("prepared index readback manifest must be an object")
    _exact_keys(
        manifest,
        INDEX_READBACK_MANIFEST_KEYS,
        "prepared index readback manifest",
    )
    mode = "authenticated" if authenticated else "anonymous"
    expected_operations = [
        {
            "type": f"registry-{mode}-digest-read",
            "capability": "read",
            "reference": expected_reference,
        },
        {
            "type": f"registry-{mode}-manifest-read",
            "capability": "read",
            "reference": f"{plan['target_repository']}@{expected_digest}",
        },
    ]
    if (
        not isinstance(readback["schema_version"], int)
        or isinstance(readback["schema_version"], bool)
        or readback["schema_version"] != 1
        or readback["kind"] != "ucm-registry-readback"
        or readback["reference"] != expected_reference
        or readback["digest"] != expected_digest
        or not isinstance(readback["authenticated"], bool)
        or readback["authenticated"] is not authenticated
        or readback["config"] is not None
        or readback["layers"] != []
        or readback["children"] != expected_manifest["manifests"]
        or manifest["media_type"] != expected_manifest["mediaType"]
        or manifest["digest"] != expected_digest
        or not isinstance(manifest["size"], int)
        or isinstance(manifest["size"], bool)
        or manifest["size"] < 1
        or manifest["annotations"] != expected_manifest["annotations"]
        or readback["operations"] != expected_operations
    ):
        raise ValueError(f"prepared index {mode} readback differs from parent intent")
    payload = {
        key: copy.deepcopy(value)
        for key, value in readback.items()
        if key != "readback_sha256"
    }
    if readback.get("readback_sha256") != sha256_value(payload):
        raise ValueError("prepared index readback hash mismatch")
    verify.audit_operations(
        readback["operations"],
        lane="protected-tag",
        public_targets={expected_reference},
    )
    return copy.deepcopy(readback)


INDEX_CLOSURE_KEYS = {
    "schema_version",
    "kind",
    "source_sha",
    "family_id",
    "reference",
    "member_digests",
    "authenticated",
    "tool",
    "command",
    "returncode",
    "stdout_sha256",
    "stderr_sha256",
    "operation",
    "validation_sha256",
}


def _validate_index_closure_evidence(
    evidence: object,
    *,
    plan: dict[str, Any],
    index_digest: str,
    authenticated: bool,
) -> dict[str, Any]:
    """Reopen a pinned recursive validation of final-repository child closure."""
    from . import verify

    if not isinstance(evidence, dict):
        raise ValueError("index remote validation evidence must be an object")
    _exact_keys(evidence, INDEX_CLOSURE_KEYS, "index remote validation evidence")
    mode = "authenticated" if authenticated else "anonymous"
    reference = f"{plan['target_repository']}@{index_digest}"
    operation = {
        "type": f"registry-{mode}-recursive-validate",
        "capability": "read",
        "reference": reference,
    }
    if (
        not isinstance(evidence["schema_version"], int)
        or isinstance(evidence["schema_version"], bool)
        or evidence["schema_version"] != 1
        or evidence["kind"] != "ucm-registry-index-remote-validation"
        or evidence["source_sha"] != plan["source_sha"]
        or evidence["family_id"] != plan["family_id"]
        or evidence["reference"] != reference
        or evidence["member_digests"]
        != [item["member_digest"] for item in plan["members"]]
        or not isinstance(evidence["authenticated"], bool)
        or evidence["authenticated"] is not authenticated
        or evidence["tool"] != {"name": "crane", "version": CRANE_VERSION}
        or evidence["command"] != ["validate", "--remote", reference, "--fast"]
        or not isinstance(evidence["returncode"], int)
        or isinstance(evidence["returncode"], bool)
        or evidence["returncode"] != 0
        or evidence["operation"] != operation
    ):
        raise ValueError("index remote validation differs from parent authority")
    _digest(evidence["stdout_sha256"], "index validation stdout")
    _digest(evidence["stderr_sha256"], "index validation stderr")
    verify.audit_operations(
        [operation],
        lane="protected-tag",
        public_targets={f"{plan['target_repository']}:{plan['target_tag']}"},
    )
    payload = {
        key: copy.deepcopy(value)
        for key, value in evidence.items()
        if key != "validation_sha256"
    }
    if evidence["validation_sha256"] != sha256_value(payload):
        raise ValueError("index remote validation hash mismatch")
    return copy.deepcopy(evidence)


def _validate_remote_index_closure(
    plan: dict[str, Any],
    *,
    index_digest: str,
    anonymous: bool = False,
) -> dict[str, Any]:
    """Run pinned crane recursive fast validation without downloading layer bodies."""
    crane_binary = resolve_pinned_crane()
    reference = f"{plan['target_repository']}@{index_digest}"
    command = ["validate", "--remote", reference, "--fast"]

    def execute(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        try:
            return _run_registry_tool(
                crane_binary,
                command,
                environment=environment,
            )
        except ValueError as error:
            raise ValueError(
                "final repository recursive child manifest validation failed"
            ) from error

    if anonymous:
        with tempfile.TemporaryDirectory(
            prefix="ucm-anonymous-docker-config-"
        ) as directory:
            (Path(directory) / "config.json").write_bytes(b'{"auths":{}}\n')
            result = execute(_minimal_registry_environment(docker_config=directory))
    else:
        result = execute(_minimal_registry_environment())
    mode = "anonymous" if anonymous else "authenticated"
    operation = {
        "type": f"registry-{mode}-recursive-validate",
        "capability": "read",
        "reference": reference,
    }
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-remote-validation",
        "source_sha": plan["source_sha"],
        "family_id": plan["family_id"],
        "reference": reference,
        "member_digests": [item["member_digest"] for item in plan["members"]],
        "authenticated": not anonymous,
        "tool": {"name": "crane", "version": CRANE_VERSION},
        "command": command,
        "returncode": result.returncode,
        "stdout_sha256": "sha256:" + hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": "sha256:" + hashlib.sha256(result.stderr.encode()).hexdigest(),
        "operation": operation,
    }
    evidence = {**payload, "validation_sha256": sha256_value(payload)}
    return _validate_index_closure_evidence(
        evidence,
        plan=plan,
        index_digest=index_digest,
        authenticated=not anonymous,
    )


def prepare_index(
    plan: object,
    *,
    parent_plans: object,
    lane: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Create/reuse one r1 and close authenticated state, deferring anonymity."""
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    authority = _fresh_write_authority(
        lane,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
        task_kind="family",
        task_id=(plan or {}).get("family_task_id") if isinstance(plan, dict) else None,
    )
    verification = verify_index(
        plan,
        parent_plans=parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    parent = _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    matches = [
        item
        for item in parent["plans"]
        if (item.get("family_task_id") == plan.get("family_task_id"))
    ]
    if len(matches) != 1 or matches[0] != plan:
        raise ValueError("prepared index plan is not the exact parent plan")
    plan = matches[0]
    if authority["preflight"].get("source_sha") != plan["source_sha"]:
        raise ValueError("prepared index source differs from live protected tag")
    core.require_default_head_for_create(
        authority["preflight"],
        plan["decision"],
        resource=f"{plan['target_repository']}:{plan['target_tag']}",
    )
    crane_binary = resolve_pinned_crane()
    buildx_binary = resolve_pinned_buildx()
    staging_repository = _repository(resolved_plan["source"].get("staging_repository"))
    target = f"{plan['target_repository']}:{plan['target_tag']}"
    with tempfile.TemporaryDirectory(prefix="ucm-index-inputs-") as directory:
        source_files: list[Path] = []
        for position, member in enumerate(plan["members"]):
            source = Path(directory) / f"{position}-{member['platform'].split('/')[-1]}"
            source.write_bytes(
                f"{staging_repository}@{member['member_digest']}".encode("utf-8")
            )
            source_files.append(source)
        common_arguments = [
            "imagetools",
            "create",
            "--tag",
            target,
            "--annotation",
            "index:org.opencontainers.image.source="
            + plan["index_manifest"]["annotations"]["org.opencontainers.image.source"],
            "--annotation",
            f"index:io.ucm.release.family-id={plan['family_id']}",
            "--annotation",
            (
                "index:io.ucm.release.index-build-key-sha256="
                + plan["index_build_key_sha256"]
            ),
            "--annotation",
            "index:io.ucm.release.source-sha=" + plan["source_sha"],
        ]
        for member, source in zip(plan["members"], source_files, strict=True):
            scope = f"manifest-descriptor[{member['platform']}]"
            common_arguments.extend(
                [
                    "--annotation",
                    f"{scope}:io.ucm.release.build-key-sha256={member['build_key_sha256']}",
                    "--annotation",
                    f"{scope}:io.ucm.release.spec-id={member['spec_id']}",
                    "--file",
                    str(source),
                ]
            )

        parent_inventory = parent["inventory"]
        inventory_entries = parent_inventory["entries"]
        inventory_matches = [
            item
            for item in inventory_entries
            if item["repository"] == plan["target_repository"]
            and item["tag"] == plan["target_tag"]
        ]
        transport = _create_index_transport(
            common_arguments=common_arguments,
            target=target,
            expected_manifest=plan["index_manifest"],
            inventory_digest=(
                inventory_matches[0]["digest"] if inventory_matches else None
            ),
            requested_decision=plan["decision"],
            buildx_command=(buildx_binary,),
            crane_binary=crane_binary,
        )
    expected_digest = transport["index_digest"]
    rendered = transport["rendered"]
    operations = transport["operations"]
    decision = transport["decision"]
    authenticated = readback_reference(target, public_targets={target})
    if rendered != plan["index_manifest"]:
        raise ValueError("prepared index transport differs from parent intent")
    authenticated = _validate_prepared_index_readback(
        authenticated,
        plan=plan,
        expected_digest=expected_digest,
        authenticated=True,
    )
    authenticated_closure = _validate_remote_index_closure(
        plan, index_digest=expected_digest
    )
    preflight_sha256 = authority["preflight"].get("preflight_sha256") or sha256_value(
        authority["preflight"]
    )
    provisional_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-provisional",
        "status": "authenticated-passed",
        "source_sha": plan["source_sha"],
        "family_id": plan["family_id"],
        "target_repository": plan["target_repository"],
        "target_tag": plan["target_tag"],
        "index_build_key_sha256": plan["index_build_key_sha256"],
        "index_digest": expected_digest,
        "manifest_sha256": expected_digest,
        "member_digests": [item["member_digest"] for item in plan["members"]],
        "authenticated_readback": authenticated,
        "authenticated_closure": authenticated_closure,
        "collision_model": copy.deepcopy(transport["collision_model"]),
        "operations": operations,
        "decision": decision,
        "postwrite_manifest_sha256": transport["postwrite_manifest_sha256"],
        "preflight_sha256": preflight_sha256,
        "verification_sha256": verification["verification_sha256"],
        "parent_plans_sha256": parent["plans_sha256"],
        "resolved_plan_sha256": authority["resolved_plan_sha256"],
        "family_task_id": authority["selected_task"]["task_id"],
        "family_task_sha256": authority["selected_task"]["task_sha256"],
    }
    provisional = {
        **provisional_payload,
        "provisional_sha256": sha256_value(provisional_payload),
    }
    return validate_provisional_index(
        provisional,
        parent_plans=parent,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )


def validate_provisional_index(
    provisional: object,
    *,
    parent_plans: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Validate the strict authenticated envelope without treating it as final."""
    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(provisional, dict):
        raise ValueError("provisional index must be an object")
    _exact_keys(
        provisional,
        DYNAMIC_PROVISIONAL_INDEX_KEYS,
        "provisional index",
    )
    if (
        not isinstance(provisional["schema_version"], int)
        or isinstance(provisional["schema_version"], bool)
        or provisional["schema_version"] != 1
        or provisional["kind"] != "ucm-registry-index-provisional"
        or provisional["status"] != "authenticated-passed"
    ):
        raise ValueError("provisional index identity is invalid")
    parent = _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if provisional["parent_plans_sha256"] != parent["plans_sha256"]:
        raise ValueError("provisional index parent hash mismatch")
    matches = [
        item
        for item in parent["plans"]
        if (item.get("family_task_id") == provisional.get("family_task_id"))
    ]
    if len(matches) != 1:
        raise ValueError("provisional index family is not parent-bound")
    plan = matches[0]
    expected_fields = {
        "source_sha": plan["source_sha"],
        "target_repository": plan["target_repository"],
        "target_tag": plan["target_tag"],
        "index_build_key_sha256": plan["index_build_key_sha256"],
        "member_digests": [item["member_digest"] for item in plan["members"]],
    }
    if any(provisional[key] != value for key, value in expected_fields.items()):
        raise ValueError("provisional index differs from its exact parent plan")
    family_task = select_task(
        resolved_plan,
        task_kind="family",
        task_id=provisional["family_task_id"],
        expected_plan_sha256=expected_plan_sha256,
    )
    if (
        provisional["resolved_plan_sha256"] != resolved_plan["resolved_plan_sha256"]
        or provisional["family_task_sha256"] != family_task["task_sha256"]
        or plan.get("family_task_id") != family_task["task_id"]
    ):
        raise ValueError("provisional index differs from frozen family authority")
    for field in (
        "index_digest",
        "manifest_sha256",
        "postwrite_manifest_sha256",
        "preflight_sha256",
        "verification_sha256",
        "parent_plans_sha256",
        "provisional_sha256",
    ):
        _digest(provisional[field], f"provisional index {field}")
    if not (
        provisional["manifest_sha256"]
        == provisional["postwrite_manifest_sha256"]
        == provisional["index_digest"]
    ):
        raise ValueError("provisional index manifest hashes disagree")
    if (
        provisional["verification_sha256"]
        != verify_index(
            plan,
            parent_plans=parent,
            resolved_plan=resolved_plan,
            expected_plan_sha256=expected_plan_sha256,
        )["verification_sha256"]
    ):
        raise ValueError("provisional index verification hash mismatch")
    _validate_collision_model(provisional["collision_model"], "provisional index")
    decision = provisional["decision"]
    target = f"{plan['target_repository']}:{plan['target_tag']}"
    expected_operations = (
        [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": target,
            }
        ]
        if decision == "create"
        else []
    )
    if (
        decision not in {"create", "reuse"}
        or (plan["decision"] == "reuse" and decision != "reuse")
        or provisional["operations"] != expected_operations
    ):
        raise ValueError("provisional index decision/operations are invalid")
    _validate_prepared_index_readback(
        provisional["authenticated_readback"],
        plan=plan,
        expected_digest=provisional["index_digest"],
        authenticated=True,
    )
    _validate_index_closure_evidence(
        provisional["authenticated_closure"],
        plan=plan,
        index_digest=provisional["index_digest"],
        authenticated=True,
    )
    payload = {
        key: copy.deepcopy(value)
        for key, value in provisional.items()
        if key != "provisional_sha256"
    }
    if provisional["provisional_sha256"] != sha256_value(payload):
        raise ValueError("provisional index hash mismatch")
    return copy.deepcopy(provisional)


def finalize_index(
    provisional: object,
    *,
    parent_plans: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Perform deferred anonymous closure and retain reopenable final evidence."""
    from . import verify

    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    parent = _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    prepared = validate_provisional_index(
        provisional,
        parent_plans=parent,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    plan = next(
        item
        for item in parent["plans"]
        if (item.get("family_task_id") == prepared.get("family_task_id"))
    )
    target = f"{plan['target_repository']}:{plan['target_tag']}"
    anonymous = readback_reference(target, anonymous=True, public_targets={target})
    anonymous = _validate_prepared_index_readback(
        anonymous,
        plan=plan,
        expected_digest=prepared["index_digest"],
        authenticated=False,
    )
    anonymous_closure = _validate_remote_index_closure(
        plan, index_digest=prepared["index_digest"], anonymous=True
    )
    authenticated = prepared["authenticated_readback"]
    authenticated_closure = prepared["authenticated_closure"]
    if (
        anonymous["manifest"] != authenticated["manifest"]
        or anonymous["children"] != authenticated["children"]
    ):
        raise ValueError("anonymous index closure differs from authenticated bytes")
    record_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-publication",
        "status": "passed",
        "source_sha": plan["source_sha"],
        "family_id": plan["family_id"],
        "target_repository": plan["target_repository"],
        "target_tag": plan["target_tag"],
        "index_build_key_sha256": plan["index_build_key_sha256"],
        "index_digest": prepared["index_digest"],
        "manifest_sha256": prepared["manifest_sha256"],
        "member_digests": copy.deepcopy(prepared["member_digests"]),
        "authenticated_readback_sha256": authenticated["readback_sha256"],
        "authenticated_closure_sha256": authenticated_closure["validation_sha256"],
        "anonymous_readback_sha256": anonymous["readback_sha256"],
        "anonymous_closure_sha256": anonymous_closure["validation_sha256"],
        "collision_model": copy.deepcopy(prepared["collision_model"]),
        "operations": copy.deepcopy(prepared["operations"]),
    }
    record = {**record_payload, "record_sha256": sha256_value(record_payload)}
    record = validate_index_record(
        record,
        parent_plans=parent,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    finalization_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-finalization",
        "status": "anonymous-passed",
        "family_id": plan["family_id"],
        "record": record,
        "provisional": copy.deepcopy(prepared),
        "provisional_sha256": prepared["provisional_sha256"],
        "authenticated_readback": copy.deepcopy(authenticated),
        "anonymous_readback": copy.deepcopy(anonymous),
        "anonymous_closure": copy.deepcopy(anonymous_closure),
        "operation_audit": {
            "publication": verify.audit_operations(
                prepared["operations"],
                lane="protected-tag",
                public_targets={target},
            ),
            "authenticated": verify.audit_operations(
                authenticated["operations"],
                lane="protected-tag",
                public_targets={target},
            ),
            "anonymous": verify.audit_operations(
                anonymous["operations"],
                lane="protected-tag",
                public_targets={target},
            ),
            "authenticated_closure": verify.audit_operations(
                [authenticated_closure["operation"]],
                lane="protected-tag",
                public_targets={target},
            ),
            "anonymous_closure": verify.audit_operations(
                [anonymous_closure["operation"]],
                lane="protected-tag",
                public_targets={target},
            ),
        },
    }
    finalized = {
        **finalization_payload,
        "finalization_sha256": sha256_value(finalization_payload),
    }
    return validate_finalized_index(
        finalized,
        parent_plans=parent,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )


def validate_finalized_index(
    finalized: object,
    *,
    parent_plans: object,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Reopen one anonymous finalization envelope against its parent/provisional."""
    from . import verify

    _require_resolved_registry_contract(resolved_plan, expected_plan_sha256)
    if not isinstance(finalized, dict):
        raise ValueError("finalized index must be an object")
    _exact_keys(finalized, FINALIZED_INDEX_KEYS, "finalized index")
    if (
        not isinstance(finalized["schema_version"], int)
        or isinstance(finalized["schema_version"], bool)
        or finalized["schema_version"] != 1
        or finalized["kind"] != "ucm-registry-index-finalization"
        or finalized["status"] != "anonymous-passed"
    ):
        raise ValueError("finalized index identity is invalid")
    parent = _validate_parent_plans(
        parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    record = validate_index_record(
        finalized["record"],
        parent_plans=parent,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    provisional = validate_provisional_index(
        finalized["provisional"],
        parent_plans=parent,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if finalized["family_id"] != record["family_id"]:
        raise ValueError("finalized index family differs from record")
    if (
        finalized["provisional_sha256"] != provisional["provisional_sha256"]
        or provisional["family_id"] != record["family_id"]
        or provisional["index_digest"] != record["index_digest"]
        or provisional["member_digests"] != record["member_digests"]
        or provisional["operations"] != record["operations"]
    ):
        raise ValueError("finalized index differs from strict provisional")
    plan = next(
        item
        for item in parent["plans"]
        if (item.get("family_task_id") == provisional.get("family_task_id"))
    )
    target = f"{plan['target_repository']}:{plan['target_tag']}"
    authenticated = _validate_prepared_index_readback(
        finalized["authenticated_readback"],
        plan=plan,
        expected_digest=record["index_digest"],
        authenticated=True,
    )
    anonymous = _validate_prepared_index_readback(
        finalized["anonymous_readback"],
        plan=plan,
        expected_digest=record["index_digest"],
        authenticated=False,
    )
    authenticated_closure = _validate_index_closure_evidence(
        provisional["authenticated_closure"],
        plan=plan,
        index_digest=record["index_digest"],
        authenticated=True,
    )
    anonymous_closure = _validate_index_closure_evidence(
        finalized["anonymous_closure"],
        plan=plan,
        index_digest=record["index_digest"],
        authenticated=False,
    )
    if (
        authenticated != provisional["authenticated_readback"]
        or authenticated["manifest"] != anonymous["manifest"]
        or authenticated["children"] != anonymous["children"]
        or authenticated["readback_sha256"] != record["authenticated_readback_sha256"]
        or anonymous["readback_sha256"] != record["anonymous_readback_sha256"]
        or authenticated_closure["validation_sha256"]
        != record["authenticated_closure_sha256"]
        or anonymous_closure["validation_sha256"] != record["anonymous_closure_sha256"]
    ):
        raise ValueError("finalized index readback closure mismatch")
    expected_audit = {
        "publication": verify.audit_operations(
            record["operations"], lane="protected-tag", public_targets={target}
        ),
        "authenticated": verify.audit_operations(
            authenticated["operations"],
            lane="protected-tag",
            public_targets={target},
        ),
        "anonymous": verify.audit_operations(
            anonymous["operations"],
            lane="protected-tag",
            public_targets={target},
        ),
        "authenticated_closure": verify.audit_operations(
            [authenticated_closure["operation"]],
            lane="protected-tag",
            public_targets={target},
        ),
        "anonymous_closure": verify.audit_operations(
            [anonymous_closure["operation"]],
            lane="protected-tag",
            public_targets={target},
        ),
    }
    if finalized["operation_audit"] != expected_audit:
        raise ValueError("finalized index operation audit mismatch")
    payload = {
        key: copy.deepcopy(value)
        for key, value in finalized.items()
        if key != "finalization_sha256"
    }
    if finalized["finalization_sha256"] != sha256_value(payload):
        raise ValueError("finalized index hash mismatch")
    return copy.deepcopy(finalized)


def create_index(
    plan: object,
    *,
    parent_plans: object,
    lane: str,
    resolved_plan: dict[str, Any],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Compatibility wrapper: prepare then immediately close anonymous readback."""
    provisional = prepare_index(
        plan,
        parent_plans=parent_plans,
        lane=lane,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    finalized = finalize_index(
        provisional,
        parent_plans=parent_plans,
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    record = finalized["record"]
    return {
        **record,
        "decision": provisional["decision"],
        "postwrite_manifest_sha256": provisional["postwrite_manifest_sha256"],
        "preflight_sha256": provisional["preflight_sha256"],
        "verification_sha256": provisional["verification_sha256"],
    }


def _loopback_request(
    base_url: str,
    method: str,
    path_or_url: str,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    url = (
        path_or_url
        if path_or_url.startswith(("http://", "https://"))
        else base_url + path_or_url
    )
    request = urllib.request.Request(url, data=data, method=method)
    if content_type is not None:
        request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read(), dict(response.headers.items())


def _loopback_upload_blob(base_url: str, repository: str, payload: bytes) -> str:
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    status, _, headers = _loopback_request(
        base_url, "POST", f"/v2/{repository}/blobs/uploads/"
    )
    if status != 202 or "Location" not in headers:
        raise ValueError("loopback Registry did not open a blob upload")
    location = headers["Location"]
    separator = "&" if "?" in location else "?"
    upload_url = location + separator + urllib.parse.urlencode({"digest": digest})
    status, _, _ = _loopback_request(
        base_url,
        "PUT",
        upload_url,
        data=payload,
        content_type="application/octet-stream",
    )
    if status != 201:
        raise ValueError("loopback Registry did not commit the exact blob digest")
    return digest


def _loopback_push_manifest(
    base_url: str,
    repository: str,
    reference: str,
    manifest: dict[str, Any],
) -> tuple[str, int]:
    raw = canonical_bytes(manifest)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    status, _, _ = _loopback_request(
        base_url,
        "PUT",
        f"/v2/{repository}/manifests/{reference}",
        data=raw,
        content_type=manifest["mediaType"],
    )
    if status != 201:
        raise ValueError("loopback Registry did not commit the manifest")
    return digest, len(raw)


def run_loopback_registry_contract(
    *,
    docker_binary: str = "docker",
    crane_binary: str = "crane",
) -> dict[str, Any]:
    """Exercise tiny digest-only manifests in a disposable pinned Registry 2.8.3."""
    registry_image = (
        "docker.io/library/registry:2.8.3@"
        "sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
    )
    crane_version = _run_registry_tool(crane_binary, ["version"])
    if crane_version.stdout.strip() not in {"0.20.3", "v0.20.3"}:
        raise ValueError(
            f"loopback contract requires crane v0.20.3, got {crane_version.stdout.strip()}"
        )
    loopback_buildx = _resolve_loopback_buildx()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    nonce = uuid.uuid4().hex
    network = f"ucm-registry-contract-network-{nonce}"
    volume = f"ucm-registry-contract-volume-{nonce}"
    container = f"ucm-registry-contract-{nonce}"
    base_url = f"http://127.0.0.1:{port}"
    registry_host = f"127.0.0.1:{port}"
    operations: list[dict[str, str]] = []
    loopback_auth = tempfile.TemporaryDirectory(prefix="ucm-loopback-docker-config-")
    (Path(loopback_auth.name) / "config.json").write_bytes(b'{"auths":{}}\n')
    loopback_environment = _minimal_registry_environment(
        docker_config=loopback_auth.name
    )
    created_network = False
    created_volume = False
    created_container = False

    def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [docker_binary, *arguments],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise ValueError(f"failed to execute docker: {error}") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or str(result.returncode)
            raise ValueError(f"docker {' '.join(arguments[:2])} failed: {detail}")
        return result

    cleanup_errors: list[str] = []
    try:
        docker("network", "create", network)
        created_network = True
        docker("volume", "create", volume)
        created_volume = True
        docker(
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            network,
            "--publish",
            f"127.0.0.1:{port}:5000",
            "--volume",
            f"{volume}:/var/lib/registry",
            registry_image,
        )
        created_container = True
        deadline = time.monotonic() + 20
        while True:
            try:
                status, _, _ = _loopback_request(base_url, "GET", "/v2/")
                if status == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                logs = docker("logs", container, check=False)
                raise ValueError(
                    "loopback Registry did not become ready: "
                    + (logs.stderr.strip() or logs.stdout.strip())
                )
            time.sleep(0.2)

        staging_repository = "ucm-contract/staging"
        final_repository = "ucm-contract/final"
        local_staging_repository = f"{registry_host}/{staging_repository}"
        local_final_repository = f"{registry_host}/{final_repository}"
        descriptors: list[dict[str, Any]] = []
        member_closures: list[dict[str, Any]] = []
        registry_member_closure_count = 0
        final_repository_child_closure_count = 0
        with tempfile.TemporaryDirectory(prefix="ucm-loopback-oci-") as scratch:
            scratch_root = Path(scratch)
            for architecture in ("amd64", "arm64"):
                layout = scratch_root / architecture
                blobs = layout / "blobs" / "sha256"
                blobs.mkdir(parents=True)
                layer = f"tiny loopback {architecture} layer\n".encode()
                layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
                (blobs / layer_digest.split(":", 1)[1]).write_bytes(layer)
                config = canonical_bytes(
                    {
                        "architecture": architecture,
                        "os": "linux",
                        "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
                        "config": {},
                    }
                )
                config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
                (blobs / config_digest.split(":", 1)[1]).write_bytes(config)
                manifest = {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {
                        "mediaType": "application/vnd.oci.image.config.v1+json",
                        "digest": config_digest,
                        "size": len(config),
                    },
                    "layers": [
                        {
                            "mediaType": "application/vnd.oci.image.layer.v1.tar",
                            "digest": layer_digest,
                            "size": len(layer),
                        }
                    ],
                    "annotations": {"io.ucm.release.loopback": architecture},
                }
                manifest_raw = canonical_bytes(manifest)
                manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
                (blobs / manifest_digest.split(":", 1)[1]).write_bytes(manifest_raw)
                descriptor = {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": len(manifest_raw),
                    "platform": {"os": "linux", "architecture": architecture},
                }
                (layout / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
                (layout / "index.json").write_bytes(
                    canonical_bytes(
                        {
                            "schemaVersion": 2,
                            "mediaType": "application/vnd.oci.image.index.v1+json",
                            "manifests": [descriptor],
                        }
                    )
                )
                archive = scratch_root / f"{architecture}.oci.tar"
                with tarfile.open(archive, "w") as bundle:
                    for path in sorted(layout.rglob("*")):
                        bundle.add(
                            path,
                            arcname=path.relative_to(layout).as_posix(),
                            recursive=False,
                        )
                with materialize_oci_layout(archive) as materialized:
                    push = _push_materialized_member(
                        materialized,
                        repository=local_staging_repository,
                        crane_binary=crane_binary,
                        insecure=True,
                        environment=loopback_environment,
                    )
                    operations.extend(push["operations"])
                tag = _apply_digest_tag(
                    repository=local_staging_repository,
                    digest=manifest_digest,
                    tag=f"member-{architecture}",
                    crane_binary=crane_binary,
                    insecure=True,
                    environment=loopback_environment,
                )
                operations.extend(tag["operations"])
                manifest_read = _run_registry_tool_bytes(
                    crane_binary,
                    [
                        "manifest",
                        "--insecure",
                        f"{local_staging_repository}@{manifest_digest}",
                    ],
                    environment=loopback_environment,
                )
                if manifest_read != manifest_raw:
                    raise ValueError("loopback member manifest readback differs")
                config_closure, config_read = _descriptor_closure(
                    manifest["config"],
                    label="loopback registry config",
                    repository=local_staging_repository,
                    crane_binary=crane_binary,
                    environment=loopback_environment,
                    retain_raw=True,
                )
                if config_read != config or config_closure["digest"] != config_digest:
                    raise ValueError("loopback registry config closure differs")
                layer_closure, _ = _descriptor_closure(
                    manifest["layers"][0],
                    label="loopback registry layer",
                    repository=local_staging_repository,
                    crane_binary=crane_binary,
                    environment=loopback_environment,
                    retain_raw=False,
                )
                if layer_closure["digest"] != layer_digest:
                    raise ValueError("loopback registry layer closure differs")
                registry_member_closure_count += 1
                descriptors.append(descriptor)
                member_closures.append(
                    {
                        "manifest_raw": manifest_raw,
                        "manifest": manifest,
                        "config_raw": config,
                        "layer_raw": layer,
                    }
                )

            index_reference = f"{local_final_repository}:r1"
            source_files: list[Path] = []
            for position, descriptor in enumerate(descriptors):
                if (
                    _fresh_transport_digest(
                        f"{local_final_repository}@{descriptor['digest']}",
                        crane_binary,
                        insecure=True,
                        environment=loopback_environment,
                    )
                    is not None
                ):
                    raise ValueError(
                        "loopback final child unexpectedly existed before index create"
                    )
                source = scratch_root / f"index-source-{position}"
                source.write_bytes(
                    f"{local_staging_repository}@{descriptor['digest']}".encode()
                )
                source_files.append(source)
            index_arguments = [
                "imagetools",
                "create",
                "--tag",
                index_reference,
                "--annotation",
                "index:io.ucm.release.loopback=dual-arch",
            ]
            for source in source_files:
                index_arguments.extend(["--file", str(source)])
            index_transport = _create_index_transport(
                common_arguments=index_arguments,
                target=index_reference,
                expected_manifest=None,
                inventory_digest=None,
                requested_decision="create",
                buildx_command=(loopback_buildx,),
                crane_binary=crane_binary,
                insecure=True,
                environment=loopback_environment,
            )
            index_raw = index_transport["raw_manifest"]
            index_digest = index_transport["index_digest"]
            operations.extend(index_transport["operations"])
            for descriptor, closure in zip(descriptors, member_closures, strict=True):
                final_child = f"{local_final_repository}@{descriptor['digest']}"
                final_manifest = _run_registry_tool_bytes(
                    crane_binary,
                    ["manifest", "--insecure", final_child],
                    environment=loopback_environment,
                )
                if final_manifest != closure["manifest_raw"]:
                    raise ValueError("loopback cross-repository child manifest differs")
                config_closure, config_read = _descriptor_closure(
                    closure["manifest"]["config"],
                    label="loopback final repository config",
                    repository=local_final_repository,
                    crane_binary=crane_binary,
                    environment=loopback_environment,
                    retain_raw=True,
                )
                if (
                    config_read != closure["config_raw"]
                    or config_closure["digest"]
                    != closure["manifest"]["config"]["digest"]
                ):
                    raise ValueError("loopback cross-repository config closure differs")
                layer_closure, layer_read = _descriptor_closure(
                    closure["manifest"]["layers"][0],
                    label="loopback final repository layer",
                    repository=local_final_repository,
                    crane_binary=crane_binary,
                    environment=loopback_environment,
                    retain_raw=True,
                )
                if (
                    layer_read != closure["layer_raw"]
                    or layer_closure["digest"]
                    != closure["manifest"]["layers"][0]["digest"]
                ):
                    raise ValueError("loopback cross-repository layer closure differs")
                final_repository_child_closure_count += 1
            _run_registry_tool(
                crane_binary,
                [
                    "validate",
                    "--remote",
                    f"{local_final_repository}@{index_digest}",
                    "--fast",
                    "--insecure",
                ],
                environment=loopback_environment,
            )
        operations.extend(
            [
                {
                    "type": "loopback-index-read",
                    "capability": "read",
                    "reference": (f"{local_final_repository}@{index_digest}"),
                },
            ]
        )
        index = _unique_json(index_raw.decode(), "loopback index")
        mutated = copy.deepcopy(index)
        mutated["annotations"]["io.ucm.release.loopback"] = "mutated"
        try:
            _loopback_request(
                base_url,
                "PUT",
                f"/v2/{final_repository}/manifests/{index_digest}",
                data=canonical_bytes(mutated),
                content_type=mutated["mediaType"],
            )
        except urllib.error.HTTPError as error:
            if error.code not in {400, 404}:
                raise
            negative_mutation = "blocked"
        else:
            raise ValueError("loopback Registry accepted same-name digest mutation")
        payload = {
            "schema_version": 1,
            "kind": "ucm-loopback-registry-contract",
            "registry_image": registry_image,
            "crane_version": "v0.20.3",
            "member_count": 2,
            "registry_member_closure_count": registry_member_closure_count,
            "final_repository_child_closure_count": (
                final_repository_child_closure_count
            ),
            "final_child_references": [
                f"{local_final_repository}@{item['digest']}" for item in descriptors
            ],
            "final_child_closure_sha256": sha256_value(
                [f"{local_final_repository}@{item['digest']}" for item in descriptors]
            ),
            "cross_repository_copy": True,
            "index_digest": index_digest,
            "negative_mutation": negative_mutation,
            "operations": operations,
            "status": "passed",
        }
    finally:
        if created_container:
            result = docker("rm", "--force", container, check=False)
            if result.returncode != 0:
                cleanup_errors.append(result.stderr.strip() or "container cleanup")
        if created_volume:
            result = docker("volume", "rm", volume, check=False)
            if result.returncode != 0:
                cleanup_errors.append(result.stderr.strip() or "volume cleanup")
        if created_network:
            result = docker("network", "rm", network, check=False)
            if result.returncode != 0:
                cleanup_errors.append(result.stderr.strip() or "network cleanup")
        loopback_auth.cleanup()
    if cleanup_errors:
        raise ValueError(f"loopback cleanup failed: {cleanup_errors}")
    return {**payload, "contract_sha256": sha256_value(payload)}
