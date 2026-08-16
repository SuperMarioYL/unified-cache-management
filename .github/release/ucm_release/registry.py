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
    canonical_bytes,
    sha256_value,
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
