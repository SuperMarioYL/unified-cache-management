from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from conftest import PRODUCTION_ROOT
from ucm_release_production.build import project_build_task
from ucm_release_production.common import (
    ProductionError,
    canonical_bytes,
    sha256_envelope,
)
from ucm_release_production.config import load_config
from ucm_release_production.images import (
    extract_oci_archive,
    image_recipe,
    inspect_oci_layout,
    prepare_image_context,
)
from ucm_release_production.tags import intent_document, parse_tag

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE = "1" * 40


def test_production_image_dockerfile_starts_cleanup_as_a_new_instruction() -> None:
    dockerfile = (PRODUCTION_ROOT / "docker" / "Dockerfile.image").read_text(
        encoding="utf-8"
    )

    assert "\nPY\nRUN rm -rf /wheelhouse\n" in dockerfile


def test_production_image_dockerfile_copies_complete_runtime_wheelhouse() -> None:
    source = (PRODUCTION_ROOT / "docker" / "Dockerfile.image").read_text(
        encoding="utf-8"
    )

    assert "ARG PACKAGING_WHEEL" in source
    assert "ARG WRAPT_WHEEL" in source
    assert "COPY ${PACKAGING_WHEEL} /wheelhouse/${PACKAGING_WHEEL}" in source
    assert "COPY ${WRAPT_WHEEL} /wheelhouse/${WRAPT_WHEEL}" in source
    assert "FROM ${BASE_IMAGE} AS production-runtime" in source


def test_extract_oci_archive_accepts_buildkit_directory_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "image.oci"
    blob_name = "blobs/sha256/" + "1" * 64
    blob = b"sealed-oci-blob"
    with tarfile.open(archive_path, "w:") as archive:
        for name in ("blobs", "blobs/sha256"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        member = tarfile.TarInfo(blob_name)
        member.size = len(blob)
        archive.addfile(member, io.BytesIO(blob))

    output = tmp_path / "layout"
    extract_oci_archive(archive_path, output)

    assert (output / blob_name).read_bytes() == blob


def test_inspect_oci_layout_accepts_buildkit_rewritten_timestamp_annotation(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    recipe = image_recipe(task, intent.image_tag)
    wheel_sha256 = "sha256:" + "6" * 64
    layer = b"production-layer"
    layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    diff_id = "sha256:" + "7" * 64
    source_date_epoch = 1786633566
    image_config = {
        "created": "2026-08-13T15:06:06Z",
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": recipe["source_sha"],
                "org.opencontainers.image.version": recipe["image_tag"],
                "io.ucm.release.spec-id": recipe["spec_id"],
                "io.ucm.release.task-sha256": recipe["task_sha256"],
                "io.ucm.release.wheel-sha256": wheel_sha256,
                "io.ucm.release.recipe-sha256": "sha256:" + recipe["sha256"],
            }
        },
    }
    config_raw = canonical_bytes(image_config)
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_raw),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": layer_digest,
                "size": len(layer),
                "annotations": {"buildkit/rewritten-timestamp": str(source_date_epoch)},
            }
        ],
        "annotations": {
            "org.opencontainers.image.revision": recipe["source_sha"],
            "org.opencontainers.image.version": recipe["image_tag"],
        },
    }
    manifest_raw = canonical_bytes(manifest)
    manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": manifest["mediaType"],
                "digest": manifest_digest,
                "size": len(manifest_raw),
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ],
    }
    blobs = tmp_path / "layout" / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (tmp_path / "layout" / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8"
    )
    (tmp_path / "layout" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    for digest, raw in (
        (layer_digest, layer),
        (config_digest, config_raw),
        (manifest_digest, manifest_raw),
    ):
        (blobs / digest.removeprefix("sha256:")).write_bytes(raw)

    closure = inspect_oci_layout(tmp_path / "layout", recipe, wheel_sha256=wheel_sha256)

    assert closure["layers"] == [
        {"digest": layer_digest, "diff_id": diff_id, "size": len(layer)}
    ]


def _source() -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-source-identity",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": "rc",
            "tag_name": "v0.6.0rc1",
            "tag_object_sha": "2" * 40,
            "source_commit_sha": SOURCE,
            "source_branch": "0.6.0-release",
            "tagger": "Octo Cat <octo@example.invalid>",
            "tagged_at": "2026-08-13T00:00:00Z",
            "tag_message_sha256": "3" * 64,
            "control_default_branch": "develop",
            "control_sha": "4" * 40,
            "lineage": {
                "accepted": True,
                "stage": "draft",
                "version": "0.6.0",
                "tag_name": "draft/v0.6.0-1",
                "source_commit_sha": SOURCE,
                "evidence_sha256": "5" * 64,
            },
        }
    )


def test_image_recipe_is_complete_and_context_reopens_pinned_wheels(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    wheel = tmp_path / "uc_manager_cuda-0.6.0rc1-cp312-cp312-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"sealed-production-wheel")
    wheel_record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": wheel.name,
            "file_sha256": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "task_sha256": "sha256:" + task["sha256"],
            "source_sha": SOURCE,
            "python_abi": task["python_abi"],
            "wheel_platform": task["wheel_platform"],
            "runtime_requirements": task["runtime_requirements"],
        }
    )
    wrapt = config["toolchain"]["wrapt"]["amd64"]
    wrapt_path = tmp_path / wrapt["filename"]
    wrapt_path.write_bytes(b"pinned-wrapt-wheel")
    packaging = config["toolchain"]["python_build"]["packaging"]
    packaging_path = tmp_path / packaging["filename"]
    packaging_path.write_bytes(b"pinned-packaging-wheel")
    mutable = copy.deepcopy(config)
    mutable["toolchain"]["wrapt"]["amd64"]["sha256"] = hashlib.sha256(
        wrapt_path.read_bytes()
    ).hexdigest()
    mutable["toolchain"]["python_build"]["packaging"]["sha256"] = hashlib.sha256(
        packaging_path.read_bytes()
    ).hexdigest()
    dockerfile = tmp_path / "Dockerfile.image"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    recipe = image_recipe(task, intent.image_tag)
    result = prepare_image_context(
        mutable,
        task,
        intent_document(intent),
        wheel_record,
        wheel,
        dockerfile,
        tmp_path / "context",
    )

    assert recipe["base"] == task["runtime"]
    assert result["recipe"] == recipe
    assert (tmp_path / "context" / wheel.name).read_bytes() == wheel.read_bytes()
    assert (tmp_path / "context" / packaging_path.name).read_bytes() == (
        packaging_path.read_bytes()
    )
    assert (tmp_path / "context" / wrapt_path.name).read_bytes() == (
        wrapt_path.read_bytes()
    )
    assert (tmp_path / "context" / "requirements.lock").read_text() == (
        f"uc-manager-cuda130 @ file:///wheelhouse/{wheel.name} "
        f"--hash={wheel_record['file_sha256']}\n"
        f"packaging @ file:///wheelhouse/{packaging_path.name} "
        f"--hash=sha256:{mutable['toolchain']['python_build']['packaging']['sha256']}\n"
        f"wrapt @ file:///wheelhouse/{wrapt_path.name} "
        f"--hash=sha256:{mutable['toolchain']['wrapt']['amd64']['sha256']}\n"
    )
    assert result["runtime_wheel_names"] == [packaging_path.name, wrapt_path.name]


@pytest.mark.parametrize(
    ("requirement", "failure"),
    [
        ("packaging", "missing"),
        ("packaging", "wrong"),
        ("wrapt", "missing"),
        ("wrapt", "wrong"),
    ],
)
def test_image_context_rejects_missing_or_wrong_runtime_requirement_wheels(
    tmp_path: Path, requirement: str, failure: str
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    wheel = tmp_path / "uc_manager_cuda-0.6.0rc1-cp312-cp312-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"sealed-production-wheel")
    wheel_record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": wheel.name,
            "file_sha256": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "task_sha256": "sha256:" + task["sha256"],
            "source_sha": SOURCE,
            "python_abi": task["python_abi"],
            "wheel_platform": task["wheel_platform"],
            "runtime_requirements": task["runtime_requirements"],
        }
    )
    mutable = copy.deepcopy(config)
    records = {
        "packaging": mutable["toolchain"]["python_build"]["packaging"],
        "wrapt": mutable["toolchain"]["wrapt"]["amd64"],
    }
    paths: dict[str, Path] = {}
    for name, record in records.items():
        path = tmp_path / record["filename"]
        path.write_bytes(f"pinned-{name}-wheel".encode())
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        paths[name] = path
    if failure == "missing":
        paths[requirement].unlink()
    else:
        paths[requirement].write_bytes(b"wrong-bytes")
    dockerfile = tmp_path / "Dockerfile.image"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    with pytest.raises(ProductionError, match=requirement):
        prepare_image_context(
            mutable,
            task,
            intent_document(intent),
            wheel_record,
            wheel,
            dockerfile,
            tmp_path / "context",
        )

    assert not (tmp_path / "context").exists()


def test_image_context_rejects_unknown_task_runtime_requirement(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    task.pop("sha256")
    task["runtime_requirements"] = [*task["runtime_requirements"], "unknown==1"]
    task = sha256_envelope(task)
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"sealed-production-wheel")
    record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": wheel.name,
            "file_sha256": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "task_sha256": "sha256:" + task["sha256"],
            "source_sha": SOURCE,
            "python_abi": task["python_abi"],
            "wheel_platform": task["wheel_platform"],
            "runtime_requirements": task["runtime_requirements"],
        }
    )
    dockerfile = tmp_path / "Dockerfile.image"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    with pytest.raises(ProductionError, match="unknown"):
        prepare_image_context(
            config,
            task,
            intent_document(intent),
            record,
            wheel,
            dockerfile,
            tmp_path / "context",
        )


def test_image_context_rejects_wheel_record_drift(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"bytes")
    record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": wheel.name,
            "file_sha256": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "task_sha256": "sha256:" + "9" * 64,
            "source_sha": SOURCE,
            "python_abi": task["python_abi"],
            "wheel_platform": task["wheel_platform"],
            "runtime_requirements": task["runtime_requirements"],
        }
    )

    with pytest.raises(ProductionError, match="differs"):
        prepare_image_context(
            config,
            task,
            intent_document(intent),
            record,
            wheel,
            tmp_path / "Dockerfile",
            tmp_path / "context",
        )
