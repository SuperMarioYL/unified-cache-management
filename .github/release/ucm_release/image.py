"""Deterministic, fixture-only install-image context and result verification."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

from . import registry
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


def implementation_digests(docker_root: Path = DOCKER_ROOT) -> dict[str, Any]:
    """Hash the exact Docker recipe/helper implementation and reject compilation."""
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
        if COMPILE_COMMAND_RE.search(text):
            raise ValueError(
                f"compile command is forbidden in install-only file {filename}"
            )
        files[filename] = "sha256:" + hashlib.sha256(content).hexdigest()
    dockerfile = (docker_root / "Dockerfile").read_text(encoding="utf-8")
    if "ARG BASE_IMAGE" not in dockerfile or "FROM ${BASE_IMAGE}" not in dockerfile:
        raise ValueError("Dockerfile must use the authorized BASE_IMAGE argument")
    forbidden_copy = re.compile(
        r"(?im)^\s*COPY\s+(?:--[^\s]+\s+)*(?:setup\.py|CMakeLists\.txt|ucm/|scripts/)"
    )
    if forbidden_copy.search(dockerfile):
        raise ValueError("Dockerfile attempts to copy UCM source or build scripts")
    return {"files": files, "aggregate_sha256": sha256_value(files)}


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


def _validate_base(base_record: object, target_platform: str) -> dict[str, Any]:
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
    if (
        base["schema_version"] != 1
        or base["kind"] != "fixture-base-image-record"
        or base["fixture_only"] is not True
    ):
        raise ValueError("base record must retain fixture-only identity")
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
    base_repository: str,
    base_index_path: Path,
    base_manifest_path: Path,
    base_config_path: Path,
    expected_index_digest: str,
    expected_manifest_digest: str,
    expected_config_digest: str,
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
    paths = {
        "index": Path(base_index_path),
        "manifest": Path(base_manifest_path),
        "config": Path(base_config_path),
    }
    expected = {
        "index": _digest(expected_index_digest, "base index digest"),
        "manifest": _digest(expected_manifest_digest, "base manifest digest"),
        "config": _digest(expected_config_digest, "base config digest"),
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
        "repository": base_repository,
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


def _descriptor_blob(
    archive: tarfile.TarFile, descriptor: dict[str, Any], label: str
) -> bytes:
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
    content = stream.read()
    if (
        len(content) != size
        or "sha256:" + hashlib.sha256(content).hexdigest() != digest
    ):
        raise ValueError(f"OCI {label} blob does not match descriptor size/digest")
    return content


def evidence_from_oci(context_dir: Path, oci_path: Path) -> dict[str, Any]:
    """Derive all build evidence directly from a standard local OCI archive."""
    context_dir = Path(context_dir)
    oci_path = Path(oci_path)
    _, recipe, wheel_path = _load_context(context_dir)
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
        if layout_stream is None or _json_bytes(layout_stream.read(), "oci-layout") != {
            "imageLayoutVersion": "1.0.0"
        }:
            raise ValueError("unsupported OCI image layout version")
        if index_stream is None:
            raise ValueError("OCI index.json is not a regular file")
        index = _json_bytes(index_stream.read(), "OCI index.json")
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
        manifest = _json_bytes(
            _descriptor_blob(archive, manifest_descriptor, "manifest"), "OCI manifest"
        )
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
        config = _json_bytes(
            _descriptor_blob(archive, config_descriptor, "config"), "OCI config"
        )
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
            "usr/local/share/ucm-release/image-metadata.json": "metadata",
            "usr/local/share/ucm-release/base-verification.json": "base_verification",
            "usr/local/share/ucm-release/install-result.json": "install",
            "usr/local/share/ucm-release/runtime-inspection.json": "runtime",
            f"tmp/{payload['wheel']['filename']}": "wheel",
        }
        observed: dict[str, bytes] = {}
        allowed_layer_media = {
            "application/vnd.oci.image.layer.v1.tar",
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
        }
        for layer_index, layer_descriptor in enumerate(manifest["layers"]):
            if (
                not isinstance(layer_descriptor, dict)
                or layer_descriptor.get("mediaType") not in allowed_layer_media
            ):
                raise ValueError("OCI image contains an unsupported layer media type")
            layer_bytes = _descriptor_blob(
                archive, layer_descriptor, f"layer {layer_index}"
            )
            media_type = layer_descriptor["mediaType"]
            if media_type == "application/vnd.oci.image.layer.v1.tar":
                uncompressed_layer = layer_bytes
            else:
                try:
                    uncompressed_layer = gzip.decompress(layer_bytes)
                except (gzip.BadGzipFile, EOFError, OSError) as error:
                    raise ValueError(
                        f"OCI gzip layer {layer_index} cannot be decompressed: {error}"
                    ) from error
            observed_diff_id = (
                "sha256:" + hashlib.sha256(uncompressed_layer).hexdigest()
            )
            if observed_diff_id != diff_ids[layer_index]:
                raise ValueError(
                    f"OCI layer {layer_index} does not match rootfs diff_id order"
                )
            try:
                layer_archive = tarfile.open(
                    fileobj=io.BytesIO(uncompressed_layer), mode="r:"
                )
            except tarfile.TarError as error:
                raise ValueError(
                    f"cannot read OCI layer {layer_index}: {error}"
                ) from error
            with layer_archive:
                layer_seen: set[str] = set()
                for member in layer_archive.getmembers():
                    name = _canonical_tar_name(member.name, f"OCI layer {layer_index}")
                    if name in layer_seen:
                        raise ValueError(f"OCI layer contains duplicate member {name}")
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
                    observed[label] = stream.read()
        missing = sorted(set(expected_paths.values()) - set(observed))
        if missing:
            raise ValueError(
                f"OCI image is missing required embedded evidence: {missing}"
            )
        context_recipe = load_json(context_dir / CONTEXT_RECIPE)
        context_metadata = load_json(context_dir / CONTEXT_METADATA)
        if _json_bytes(observed["recipe"], "embedded recipe") != context_recipe:
            raise ValueError("OCI embedded recipe does not match context")
        if _json_bytes(observed["metadata"], "embedded metadata") != context_metadata:
            raise ValueError("OCI embedded metadata does not match context")
        if observed["wheel"] != wheel_path.read_bytes():
            raise ValueError(
                "OCI embedded wheel does not match the authorized context bytes"
            )
        return {
            "schema_version": 1,
            "kind": "ucm-image-build-evidence",
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


def verify_oci(
    context_dir: Path,
    oci_path: Path,
    *,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    """Verify a Buildx local OCI output without caller-authored result summaries."""
    evidence = evidence_from_oci(context_dir, oci_path)
    return verify_image(context_dir, evidence, schema_dir=schema_dir)
