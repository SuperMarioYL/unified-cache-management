"""Deterministic production image recipes and OCI closure inspection."""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import (
    ProductionError,
    canonical_bytes,
    decode_json,
    require_exact_keys,
    require_lower_commit_sha,
    require_sha256_digest,
    sha256_envelope,
    verify_envelope,
)
from .config import validate_config

_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
_LAYER_PREFIX = "application/vnd.oci.image.layer.v1"


def image_recipe(task: dict[str, Any], image_tag: str) -> dict[str, Any]:
    """Bind one trusted wheel task to one immutable upstream runtime member."""

    require_lower_commit_sha(task.get("source_sha"), "image recipe source SHA")
    require_sha256_digest("sha256:" + str(task.get("sha256")), "image task SHA")
    runtime = task.get("runtime")
    if not isinstance(runtime, dict):
        raise ProductionError("image recipe runtime authority is missing")
    require_exact_keys(
        runtime,
        {"repository", "tag", "index_digest", "manifest_digest", "config_digest"},
        "image recipe runtime",
    )
    for name in ("index_digest", "manifest_digest", "config_digest"):
        require_sha256_digest(runtime[name], f"image runtime {name}")
    if not isinstance(image_tag, str) or not image_tag or "/" in image_tag:
        raise ProductionError("image recipe tag is invalid")
    return sha256_envelope(
        {
            "kind": "ucm-production-image-recipe",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "platform": task["platform"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "source_sha": task["source_sha"],
            "task_sha256": "sha256:" + task["sha256"],
            "image_tag": image_tag,
            "base": runtime,
        }
    )


def prepare_image_context(
    config_value: object,
    task_value: object,
    intent_value: object,
    wheel_record_value: object,
    wheel_path: Path,
    dockerfile: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Create a closed, offline image context from one verified wheel."""

    config = validate_config(config_value)
    task = verify_envelope(
        task_value,
        kind="ucm-production-wheel-build-task",
        schema_version=1,
    )
    intent = verify_envelope(
        intent_value,
        kind="ucm-production-tag-intent",
        schema_version=1,
    )
    wheel_record = verify_envelope(
        wheel_record_value,
        kind="ucm-production-wheel-record",
        schema_version=1,
    )
    if (
        wheel_record.get("spec_id") != task.get("spec_id")
        or wheel_record.get("task_sha256") != "sha256:" + str(task.get("sha256"))
        or wheel_record.get("source_sha") != task.get("source_sha")
        or wheel_record.get("version") != task.get("wheel_version")
        or wheel_record.get("distribution") != task.get("distribution")
    ):
        raise ProductionError("image wheel record differs from build task")
    wheel_path = Path(wheel_path)
    dockerfile = Path(dockerfile)
    if (
        not wheel_path.is_file()
        or wheel_path.is_symlink()
        or wheel_path.name != wheel_record.get("filename")
        or _digest(wheel_path.read_bytes()) != wheel_record.get("file_sha256")
    ):
        raise ProductionError("image wheel bytes differ from sealed record")
    if not dockerfile.is_file() or dockerfile.is_symlink():
        raise ProductionError("production image Dockerfile is invalid")
    architecture = task.get("cpu_arch")
    if architecture not in {"amd64", "arm64"}:
        raise ProductionError("image task architecture is invalid")
    wrapt = config["toolchain"]["wrapt"][architecture]
    wrapt_source = Path("/tmp") / wrapt["filename"]
    # Hosted callers materialize the pinned dependency at this exact sibling path.
    sibling = wheel_path.parent / wrapt["filename"]
    if sibling.is_file() and not sibling.is_symlink():
        wrapt_source = sibling
    if (
        not wrapt_source.is_file()
        or _digest(wrapt_source.read_bytes()) != "sha256:" + wrapt["sha256"]
    ):
        raise ProductionError("pinned wrapt wheel is absent or differs")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ProductionError("image context output already exists")
    output_dir.mkdir(parents=True)
    (output_dir / wheel_path.name).write_bytes(wheel_path.read_bytes())
    (output_dir / wrapt_source.name).write_bytes(wrapt_source.read_bytes())
    (output_dir / "Dockerfile").write_bytes(dockerfile.read_bytes())
    wheel_sha = wheel_record["file_sha256"]
    (output_dir / "requirements.lock").write_text(
        (
            f"{task['distribution']} @ file:///wheelhouse/{wheel_path.name} "
            f"--hash={wheel_sha}\n"
            f"wrapt @ file:///wheelhouse/{wrapt_source.name} "
            f"--hash=sha256:{wrapt['sha256']}\n"
        ),
        encoding="utf-8",
    )
    recipe = image_recipe(task, str(intent["image_tag"]))
    return {
        "recipe": recipe,
        "wheel_sha256": wheel_sha,
        "wheel_name": wheel_path.name,
        "wrapt_name": wrapt_source.name,
    }


def _safe_extract(archive_path: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:") as archive:
            names: set[str] = set()
            for member in archive.getmembers():
                path = Path(member.name)
                canonical_name = path.as_posix()
                if (
                    member.issym()
                    or member.islnk()
                    or path.is_absolute()
                    or ".." in path.parts
                    or canonical_name in {"", "."}
                    or canonical_name in names
                    or any(ord(char) < 32 or ord(char) == 127 for char in member.name)
                ):
                    raise ProductionError("OCI archive contains an unsafe member")
                names.add(canonical_name)
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ProductionError("OCI archive contains an unsafe member")
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ProductionError("OCI archive member cannot be read")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
    except tarfile.TarError:
        raise ProductionError("OCI archive is not a valid tar") from None


def extract_oci_archive(archive: Path, output_dir: Path) -> None:
    """Extract one BuildKit OCI archive using a closed regular-file policy."""

    archive = Path(archive)
    output_dir = Path(output_dir)
    if not archive.is_file() or archive.is_symlink():
        raise ProductionError("OCI archive must be one regular file")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ProductionError("OCI layout output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    _safe_extract(archive, output_dir)


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = decode_json(path.read_bytes(), label)
    except OSError:
        raise ProductionError(f"{label} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _blob(root: Path, descriptor: dict[str, Any], label: str) -> bytes:
    require_exact_keys(descriptor, {"mediaType", "digest", "size"}, label)
    digest = require_sha256_digest(descriptor["digest"], f"{label} digest")
    if type(descriptor["size"]) is not int or descriptor["size"] < 1:
        raise ProductionError(f"{label} size is invalid")
    path = root / "blobs" / "sha256" / digest.removeprefix("sha256:")
    if not path.is_file() or path.is_symlink():
        raise ProductionError(f"{label} blob is missing")
    raw = path.read_bytes()
    if len(raw) != descriptor["size"] or _digest(raw) != digest:
        raise ProductionError(f"{label} blob identity differs")
    return raw


def inspect_oci_layout(
    layout: Path,
    recipe_value: object,
    *,
    wheel_sha256: str,
) -> dict[str, Any]:
    """Reopen one exact OCI member and emit the candidate closure."""

    if not isinstance(recipe_value, dict):
        raise ProductionError("image recipe must be an object")
    recipe = dict(recipe_value)
    expected_recipe = dict(recipe)
    recipe_sha = expected_recipe.pop("sha256", None)
    if recipe_sha != hashlib.sha256(canonical_bytes(expected_recipe)).hexdigest():
        raise ProductionError("image recipe self-digest differs")
    require_sha256_digest(wheel_sha256, "image wheel SHA256")
    root = Path(layout)
    marker = _json(root / "oci-layout", "OCI layout marker")
    if marker != {"imageLayoutVersion": "1.0.0"}:
        raise ProductionError("OCI layout marker is invalid")
    index = _json(root / "index.json", "OCI layout index")
    manifests = index.get("manifests")
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != _OCI_INDEX
        or not isinstance(manifests, list)
        or len(manifests) != 1
    ):
        raise ProductionError("OCI layout index is invalid")
    descriptor = manifests[0]
    if not isinstance(descriptor, dict):
        raise ProductionError("OCI manifest descriptor is invalid")
    platform = descriptor.pop("platform", None)
    descriptor.pop("annotations", None)
    if platform != {
        "os": "linux",
        "architecture": recipe["platform"].split("/", 1)[1],
    }:
        raise ProductionError("OCI manifest platform differs from recipe")
    manifest_raw = _blob(root, descriptor, "OCI manifest")
    if descriptor["mediaType"] != _OCI_MANIFEST:
        raise ProductionError("OCI manifest media type differs")
    manifest = decode_json(manifest_raw, "OCI manifest")
    if not isinstance(manifest, dict):
        raise ProductionError("OCI manifest must be an object")
    config_descriptor = manifest.get("config")
    if not isinstance(config_descriptor, dict):
        raise ProductionError("OCI config descriptor is missing")
    config_raw = _blob(root, config_descriptor, "OCI config")
    if config_descriptor["mediaType"] != _OCI_CONFIG:
        raise ProductionError("OCI config media type differs")
    config = decode_json(config_raw, "OCI config")
    if not isinstance(config, dict):
        raise ProductionError("OCI config must be an object")
    labels = config.get("config", {}).get("Labels", {})
    expected_labels = {
        "org.opencontainers.image.revision": recipe["source_sha"],
        "org.opencontainers.image.version": recipe["image_tag"],
        "io.ucm.release.spec-id": recipe["spec_id"],
        "io.ucm.release.task-sha256": recipe["task_sha256"],
        "io.ucm.release.wheel-sha256": wheel_sha256,
        "io.ucm.release.recipe-sha256": "sha256:" + recipe["sha256"],
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise ProductionError("OCI config labels differ from production recipe")
    layers_value = manifest.get("layers")
    diff_ids = config.get("rootfs", {}).get("diff_ids")
    created = config.get("created")
    try:
        created_epoch = int(
            datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError, OverflowError):
        raise ProductionError("OCI config created time is invalid") from None
    if (
        not isinstance(layers_value, list)
        or not layers_value
        or not isinstance(diff_ids, list)
        or len(diff_ids) != len(layers_value)
    ):
        raise ProductionError("OCI layer/diff-ID closure is invalid")
    layers: list[dict[str, Any]] = []
    for position, item in enumerate(layers_value):
        if not isinstance(item, dict):
            raise ProductionError("OCI layer descriptor is invalid")
        descriptor = dict(item)
        annotations = descriptor.pop("annotations", None)
        diff_id = require_sha256_digest(diff_ids[position], f"OCI diff-ID {position}")
        if annotations is not None and annotations != {
            "buildkit/rewritten-timestamp": str(created_epoch)
        }:
            raise ProductionError("OCI layer annotations differ from config diff-ID")
        raw = _blob(root, descriptor, f"OCI layer {position}")
        if not descriptor["mediaType"].startswith(_LAYER_PREFIX):
            raise ProductionError("OCI layer media type differs")
        layers.append(
            {
                "digest": _digest(raw),
                "diff_id": diff_id,
                "size": len(raw),
            }
        )
    manifest_annotations = manifest.get("annotations", {})
    expected_annotations = {
        "org.opencontainers.image.revision": recipe["source_sha"],
        "org.opencontainers.image.version": recipe["image_tag"],
    }
    if any(
        manifest_annotations.get(key) != value
        for key, value in expected_annotations.items()
    ):
        raise ProductionError("OCI manifest annotations differ from production recipe")
    return sha256_envelope(
        {
            "kind": "ucm-production-image-member-closure",
            "schema_version": 1,
            "spec_id": recipe["spec_id"],
            "platform": recipe["platform"],
            "source_sha": recipe["source_sha"],
            "task_sha256": recipe["task_sha256"],
            "wheel_sha256": wheel_sha256,
            "recipe_sha256": "sha256:" + recipe["sha256"],
            "manifest_digest": _digest(manifest_raw),
            "manifest_size": len(manifest_raw),
            "config_digest": _digest(config_raw),
            "config_size": len(config_raw),
            "layers": layers,
            "annotations": expected_annotations,
        }
    )
