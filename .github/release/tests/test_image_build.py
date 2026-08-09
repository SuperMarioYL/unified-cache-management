"""Behavioral contract for the fixture-only install image builder."""

from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
DOCKER_ROOT = RELEASE_ROOT / "docker"
PYTHON = sys.executable
DIGESTS = {
    name: "sha256:" + character * 64
    for name, character in {
        "upstream_index": "1",
        "amd64_manifest": "2",
        "amd64_config": "3",
        "arm64_manifest": "4",
        "arm64_config": "5",
        "base_index": "6",
        "base_manifest": "7",
        "base_config": "8",
        "oci": "9",
    }.items()
}


def _modules():
    sys.path.insert(0, str(RELEASE_ROOT))
    return (
        importlib.import_module("ucm_release.core"),
        importlib.import_module("ucm_release.wheel"),
        importlib.import_module("ucm_release.registry"),
        importlib.import_module("ucm_release.image"),
    )


def _write_fixture_wheel(tmp_path: Path, spec: dict[str, object], version: str) -> Path:
    platform = {"amd64": "x86_64", "arm64": "aarch64"}[spec["cpu_arch"]]
    tag = f"{spec['python_abi']}-{spec['python_abi']}-linux_{platform}"
    wheel_path = tmp_path / f"uc_manager-{version}-{tag}.whl"
    dist_info = f"uc_manager-{version}.dist-info"
    members = {
        "ucm/__init__.py": "__version__ = %r\n" % version,
        "ucm/_fixture_build.py": (
            f"SOURCE_SHA = {'0' * 40!r}\nPROFILE_ID = {spec['spec_id']!r}\n"
        ),
        f"{dist_info}/METADATA": "\n".join(
            [
                "Metadata-Version: 2.1",
                "Name: uc-manager",
                f"Version: {version}",
                "Requires-Dist: wrapt==1.17.2",
                "",
            ]
        ),
        f"{dist_info}/WHEEL": "\n".join(
            [
                "Wheel-Version: 1.0",
                "Generator: task4-fixture",
                "Root-Is-Purelib: false",
                f"Tag: {tag}",
                "",
            ]
        ),
    }
    record_rows: list[list[str]] = []
    for name, content in members.items():
        digest = hashlib.sha256(content.encode()).digest()
        import base64

        encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        record_rows.append([name, f"sha256={encoded}", str(len(content.encode()))])
    record_name = f"{dist_info}/RECORD"
    record_rows.append([record_name, "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(record_rows)
    members[record_name] = record_buffer.getvalue()
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return wheel_path


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": "quay.io/ascend/vllm-ascend",
        "upstream_tag": "v0.22.1rc1-a3",
        "index_digest": DIGESTS["upstream_index"],
        "platforms": [
            {
                "os": "linux",
                "architecture": "amd64",
                "manifest_digest": DIGESTS["amd64_manifest"],
                "config_digest": DIGESTS["amd64_config"],
            },
            {
                "os": "linux",
                "architecture": "arm64",
                "manifest_digest": DIGESTS["arm64_manifest"],
                "config_digest": DIGESTS["arm64_config"],
            },
        ],
    }


def _inventory(registry) -> dict[str, object]:
    value = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": [],
    }
    value["inventory_sha256"] = registry.inventory_digest(value)
    return value


def _base_record() -> dict[str, object]:
    return _base_chain_record()


def _base_chain_record() -> dict[str, object]:
    """Return exact, reopenable bytes for a synthetic OCI base descriptor chain."""

    def blob(value: dict[str, object], media_type: str) -> dict[str, object]:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        raw_bytes = raw.encode()
        return {
            "media_type": media_type,
            "digest": "sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
            "size": len(raw_bytes),
            "raw": raw,
        }

    config = blob(
        {"architecture": "arm64", "os": "linux", "variant": "v8"},
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = blob(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": config["media_type"],
                "digest": config["digest"],
                "size": config["size"],
            },
            "layers": [],
        },
        "application/vnd.oci.image.manifest.v1+json",
    )
    index = blob(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": manifest["media_type"],
                    "digest": manifest["digest"],
                    "size": manifest["size"],
                    "platform": {
                        "architecture": "arm64",
                        "os": "linux",
                        "variant": "v8",
                    },
                }
            ],
        },
        "application/vnd.oci.image.index.v1+json",
    )
    return {
        "schema_version": 1,
        "kind": "fixture-base-image-record",
        "fixture_only": True,
        "repository": "docker.io/library/python",
        "index": index,
        "manifest": manifest,
        "config": config,
    }


def _refresh_base_blob(blob: dict[str, object], value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    raw_bytes = raw.encode()
    blob["raw"] = raw
    blob["digest"] = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    blob["size"] = len(raw_bytes)


def _rebind_base_config(record: dict[str, object], mutate: object) -> dict[str, object]:
    """Apply a valid config edit and refresh both upstream descriptors."""
    config = json.loads(record["config"]["raw"])
    mutate(config)
    _refresh_base_blob(record["config"], config)
    manifest = json.loads(record["manifest"]["raw"])
    manifest["config"] = {
        "mediaType": record["config"]["media_type"],
        "digest": record["config"]["digest"],
        "size": record["config"]["size"],
    }
    _refresh_base_blob(record["manifest"], manifest)
    index = json.loads(record["index"]["raw"])
    index["manifests"][0].update(
        {
            "mediaType": record["manifest"]["media_type"],
            "digest": record["manifest"]["digest"],
            "size": record["manifest"]["size"],
        }
    )
    _refresh_base_blob(record["index"], index)
    return record


def _actual_inputs(tmp_path: Path) -> dict[str, object]:
    core, wheel_module, registry, image = _modules()
    manifest = core.build_release_manifest()
    spec = next(
        item
        for item in manifest["wheel_specs"]
        if item["accelerator"] == "ascend"
        and item["accelerator_runtime"] == "cann-9.0.0"
        and item["npu_arch_or_na"] == "a3"
        and item["os"] == "ubuntu-22.04"
        and item["cpu_arch"] == "arm64"
        and item["python_abi"] == "cp312"
    )
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "fixture-wheel", "0" * 40, spec["spec_id"]
    )
    wheel_path = Path(fixture["wheel_path"])
    wheel_record = fixture["inspection"]
    _, compatibility = core.validate_config()
    source_case = {
        "release_manifest": manifest,
        "wheel_records": [wheel_record],
        "spec_id": spec["spec_id"],
        "upstream_snapshot": _snapshot(),
        "compatibility": compatibility,
        "compatibility_rule_id": "ascend-supported",
        "implementation_digest": image.implementation_digests()["aggregate_sha256"],
    }
    candidate = registry.build_candidate(**source_case, fixture_mode=True)
    inventory = _inventory(registry)
    task = registry.reconcile(candidate, inventory)["tasks"][0]
    return {
        "source_case": source_case,
        "candidate": candidate,
        "task": task,
        "inventory": inventory,
        "base_record": _base_record(),
        "target_platform": "linux/arm64",
        "wheel_path": wheel_path,
    }


def _prepare(tmp_path: Path, *, name: str = "context"):
    *_, image = _modules()
    values = _actual_inputs(tmp_path)
    context = tmp_path / name
    recipe = image.prepare_context(**values, output_dir=context)
    return image, values, context, recipe


def _evidence(recipe: dict[str, object]) -> dict[str, object]:
    wheel = recipe["payload"]["wheel"]
    base = recipe["payload"]["base"]
    return {
        "schema_version": 1,
        "kind": "ucm-image-build-evidence",
        "recipe_sha256": recipe["payload_sha256"],
        "build_key_sha256": recipe["payload"]["build_key_sha256"],
        "base_verification": {
            "schema_version": 1,
            "kind": "ucm-base-verification",
            "base_subject": base["subject"],
            "target_platform": recipe["payload"]["target_platform"],
            "status": "passed",
        },
        "install": {
            "schema_version": 1,
            "kind": "ucm-install-result",
            "wheel_filename": wheel["filename"],
            "wheel_sha256": wheel["sha256"],
            "version": wheel["version"],
            "requires_dist": ["wrapt==1.17.2"],
            "pip_command": [
                "/usr/local/bin/python",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--only-binary=:all:",
                f"/tmp/{wheel['filename']}",
            ],
            "pip_check": "passed",
            "direct_url": {
                "url": f"file:///tmp/{wheel['filename']}",
                "archive_info": {
                    "hash": "sha256=" + wheel["sha256"].removeprefix("sha256:")
                },
            },
            "installed_packages": {
                "uc-manager": wheel["version"],
                "wrapt": "1.17.2",
            },
            "imports": {"ucm": "passed", "wrapt": "passed"},
            "status": "passed",
        },
        "runtime": {
            "schema_version": 1,
            "kind": "ucm-runtime-inspection",
            "python_version": "3.12.11",
            "soabi": "cpython-312-aarch64-linux-gnu",
            "package_version": wheel["version"],
            "shared_objects": [],
            "abi": {
                "expected_python_abi": wheel["python_abi"],
                "observed_python_abi": "cp312",
                "status": "passed",
            },
            "accelerator_runtime": {
                "status": "external-required",
                "reason": "fixture image build cannot validate the accelerator runtime",
            },
            "device": {
                "status": "external-required",
                "reason": "fixture image build cannot validate accelerator hardware",
            },
            "hardware_passed": False,
            "status": "external-required",
        },
        "oci": {
            "output": "local-oci",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": DIGESTS["oci"],
            "platform": recipe["payload"]["target_platform"],
            "published": False,
        },
    }


def _write_oci(
    path: Path,
    context: Path,
    recipe: dict[str, object],
    evidence: dict[str, object],
    *,
    layer_media_types: tuple[str, ...] = (
        "application/vnd.oci.image.layer.v1.tar+gzip",
    ),
    rootfs_mutation: str | None = None,
) -> None:
    """Write a minimal standard OCI layout containing the observable image files."""
    members = {
        "usr/local/share/ucm-release/image-recipe.json": (
            context / "image-recipe.json"
        ).read_bytes(),
        "usr/local/share/ucm-release/image-metadata.json": (
            context / "image-metadata.json"
        ).read_bytes(),
        "usr/local/share/ucm-release/base-verification.json": json.dumps(
            evidence["base_verification"], sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
        "usr/local/share/ucm-release/install-result.json": json.dumps(
            evidence["install"], sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
        "usr/local/share/ucm-release/runtime-inspection.json": json.dumps(
            evidence["runtime"], sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
        f"tmp/{recipe['payload']['wheel']['filename']}": (
            context / recipe["payload"]["wheel"]["filename"]
        ).read_bytes(),
    }
    layer_blobs: list[bytes] = []
    diff_ids: list[str] = []
    for layer_index, media_type in enumerate(layer_media_types):
        layer_buffer = io.BytesIO()
        layer_members = (
            members
            if layer_index == 0
            else {
                f"usr/local/share/ucm-release/layer-{layer_index}.txt": str(
                    layer_index
                ).encode()
            }
        )
        with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
            for name, content in sorted(layer_members.items()):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                info.mtime = 0
                layer.addfile(info, io.BytesIO(content))
        uncompressed = layer_buffer.getvalue()
        diff_ids.append("sha256:" + hashlib.sha256(uncompressed).hexdigest())
        if media_type == "application/vnd.oci.image.layer.v1.tar":
            layer_blobs.append(uncompressed)
            continue
        compressed_buffer = io.BytesIO()
        with gzip.GzipFile(
            fileobj=compressed_buffer, mode="wb", filename="", mtime=0
        ) as stream:
            stream.write(uncompressed)
        layer_blobs.append(compressed_buffer.getvalue())

    rootfs = {"type": "layers", "diff_ids": diff_ids}
    if rootfs_mutation == "wrong-type":
        rootfs["type"] = "not-layers"
    elif rootfs_mutation == "wrong-diff-id":
        rootfs["diff_ids"][0] = "sha256:" + "a" * 64
    elif rootfs_mutation == "extra-diff-id":
        rootfs["diff_ids"].append("sha256:" + "b" * 64)
    elif rootfs_mutation == "missing-diff-id":
        rootfs["diff_ids"] = rootfs["diff_ids"][:-1]
    elif rootfs_mutation == "reversed-diff-ids":
        rootfs["diff_ids"] = list(reversed(rootfs["diff_ids"]))
    config_value = {"architecture": "arm64", "os": "linux", "rootfs": rootfs}
    if rootfs_mutation == "missing-rootfs":
        del config_value["rootfs"]
    config = json.dumps(
        config_value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    def descriptor(blob: bytes, media_type: str) -> dict[str, object]:
        return {
            "mediaType": media_type,
            "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
            "size": len(blob),
        }

    config_descriptor = descriptor(config, "application/vnd.oci.image.config.v1+json")
    layer_descriptors = [
        descriptor(blob, media_type)
        for blob, media_type in zip(layer_blobs, layer_media_types, strict=True)
    ]
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_descriptor,
            "layers": layer_descriptors,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_descriptor = {
        **descriptor(manifest, "application/vnd.oci.image.manifest.v1+json"),
        "platform": {"architecture": "arm64", "os": "linux"},
    }
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [manifest_descriptor],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    layout = b'{"imageLayoutVersion":"1.0.0"}'
    blobs = {
        config_descriptor["digest"]: config,
        manifest_descriptor["digest"]: manifest,
    }
    blobs.update(
        {
            descriptor["digest"]: blob
            for descriptor, blob in zip(layer_descriptors, layer_blobs, strict=True)
        }
    )
    with tarfile.open(path, "w") as archive:
        files = {"index.json": index, "oci-layout": layout}
        files.update(
            {
                "blobs/sha256/" + digest.removeprefix("sha256:"): blob
                for digest, blob in blobs.items()
            }
        )
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))


def _cli(*arguments: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(RELEASE_ROOT)
    result = subprocess.run(
        [PYTHON, "-m", "ucm_release", *arguments],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == expect, result.stderr
    return result


def test_prepare_context_consumes_actual_task3_task_and_task2_record(
    tmp_path: Path,
) -> None:
    """Replacing either full upstream artifact with a hand-made summary must fail."""
    _, values, context, recipe = _prepare(tmp_path)

    assert {path.name for path in context.iterdir()} == {
        "Dockerfile",
        "install_ucm.py",
        "inspect_runtime.py",
        "verify_base_image.py",
        values["wheel_path"].name,
        "image-recipe.json",
        "image-metadata.json",
    }
    assert (
        recipe["payload"]["build_key_sha256"] == values["candidate"]["build_key_sha256"]
    )
    assert recipe["payload"]["task_sha256"].startswith("sha256:")
    assert (
        recipe["payload"]["wheel"]["sha256"]
        == values["source_case"]["wheel_records"][0]["sha256"]
    )
    assert recipe["payload"]["fixture_only"] is True
    assert recipe["payload"]["unpublished"] is True
    assert recipe["payload"]["source_date_epoch"] == 0


@pytest.mark.parametrize("mutation", ["task", "candidate", "record", "wheel"])
def test_prepare_context_recomputes_instead_of_trusting_summaries(
    tmp_path: Path, mutation: str
) -> None:
    """A forged task, candidate, mini record, or changed wheel cannot enter context."""
    *_, image = _modules()
    values = _actual_inputs(tmp_path)
    if mutation == "task":
        values["task"]["build_key_sha256"] = "sha256:" + "a" * 64
    elif mutation == "candidate":
        values["candidate"]["fixture_only"] = False
    elif mutation == "record":
        values["source_case"]["wheel_records"] = [
            {
                "kind": "ucm-wheel-inspection",
                "spec_id": values["source_case"]["spec_id"],
            }
        ]
    else:
        values["wheel_path"].write_bytes(values["wheel_path"].read_bytes() + b"x")

    with pytest.raises(ValueError):
        image.prepare_context(**values, output_dir=tmp_path / "bad")


def test_recipe_identity_binds_base_source_wheel_and_implementation(
    tmp_path: Path,
) -> None:
    """Dropping a base/config/index/spec/declaration/helper input breaks closure."""
    image, values, _, recipe = _prepare(tmp_path)
    payload = recipe["payload"]

    assert (
        payload["source"]["config_sha256"]
        == values["source_case"]["release_manifest"]["config_sha256"]
    )
    assert payload["source"]["upstream_index_digest"] == DIGESTS["upstream_index"]
    assert (
        payload["source"]["upstream_platform_manifest_digest"]
        == DIGESTS["arm64_manifest"]
    )
    assert (
        payload["source"]["upstream_platform_config_digest"] == DIGESTS["arm64_config"]
    )
    assert payload["base"]["index"] == values["base_record"]["index"]
    assert payload["base"]["manifest"] == values["base_record"]["manifest"]
    assert payload["base"]["config"] == values["base_record"]["config"]
    assert payload["base"]["subject"] == (
        "docker.io/library/python@" + values["base_record"]["manifest"]["digest"]
    )
    assert payload["base"]["platform"] == {
        "os": "linux",
        "architecture": "arm64",
        "variant": "v8",
        "manifest_media_type": values["base_record"]["manifest"]["media_type"],
        "manifest_digest": values["base_record"]["manifest"]["digest"],
        "manifest_size": values["base_record"]["manifest"]["size"],
        "config_media_type": values["base_record"]["config"]["media_type"],
        "config_digest": values["base_record"]["config"]["digest"],
        "config_size": values["base_record"]["config"]["size"],
    }
    assert payload["wheel"]["spec_id"] == values["source_case"]["spec_id"]
    assert payload["wheel"]["declaration_sha256"].startswith("sha256:")
    assert payload["implementation"] == image.implementation_digests()
    changed = copy.deepcopy(values)
    _rebind_base_config(
        changed["base_record"],
        lambda config: config.update({"fixture_revision": 2}),
    )
    changed_recipe = image.prepare_context(
        **changed, output_dir=tmp_path / "changed-context"
    )
    assert changed_recipe["payload_sha256"] != recipe["payload_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        "index-bytes",
        "index-digest",
        "index-size",
        "manifest-bytes",
        "manifest-digest",
        "manifest-size",
        "config-bytes",
        "config-digest",
        "config-size",
        "index-platform",
        "index-variant",
        "index-manifest-digest",
        "index-manifest-size",
        "index-manifest-media-type",
        "manifest-config-digest",
        "manifest-config-size",
        "manifest-config-media-type",
        "config-media-type",
        "config-platform",
    ],
)
def test_base_descriptor_chain_is_reopened_and_every_link_is_verified(
    tmp_path: Path, mutation: str
) -> None:
    """Index, manifest, and config bytes must prove the authorized FROM subject."""
    *_, image = _modules()
    values = _actual_inputs(tmp_path)
    values["base_record"] = _base_chain_record()
    good = image.prepare_context(**values, output_dir=tmp_path / "base-chain-good")
    assert good["payload"]["base"]["subject"] == (
        "docker.io/library/python@" + values["base_record"]["manifest"]["digest"]
    )

    changed = copy.deepcopy(values)
    record = changed["base_record"]
    part, operation = mutation.split("-", 1)
    if operation == "bytes":
        record[part]["raw"] += " "
    elif operation == "digest":
        record[part]["digest"] = "sha256:" + "c" * 64
    elif operation == "size":
        record[part]["size"] += 1
    elif mutation == "index-platform":
        index = json.loads(record["index"]["raw"])
        index["manifests"][0]["platform"]["architecture"] = "amd64"
        _refresh_base_blob(record["index"], index)
    elif mutation == "index-variant":
        index = json.loads(record["index"]["raw"])
        index["manifests"][0]["platform"]["variant"] = "v9"
        _refresh_base_blob(record["index"], index)
    elif mutation in {"index-manifest-digest", "index-manifest-size"}:
        index = json.loads(record["index"]["raw"])
        field = mutation.removeprefix("index-manifest-")
        index["manifests"][0][field] = "sha256:" + "d" * 64 if field == "digest" else 1
        _refresh_base_blob(record["index"], index)
    elif mutation == "index-manifest-media-type":
        index = json.loads(record["index"]["raw"])
        index["manifests"][0]["mediaType"] = "application/octet-stream"
        _refresh_base_blob(record["index"], index)
    elif mutation in {
        "manifest-config-digest",
        "manifest-config-size",
        "manifest-config-media-type",
    }:
        manifest = json.loads(record["manifest"]["raw"])
        field = mutation.removeprefix("manifest-config-")
        field = "mediaType" if field == "media-type" else field
        manifest["config"][field] = {
            "digest": "sha256:" + "e" * 64,
            "size": 1,
            "mediaType": "application/octet-stream",
        }[field]
        _refresh_base_blob(record["manifest"], manifest)
        index = json.loads(record["index"]["raw"])
        index["manifests"][0].update(
            {
                "digest": record["manifest"]["digest"],
                "size": record["manifest"]["size"],
            }
        )
        _refresh_base_blob(record["index"], index)
    elif mutation == "config-media-type":
        record["config"]["media_type"] = "application/octet-stream"
    else:
        _rebind_base_config(
            record, lambda config: config.update({"architecture": "amd64"})
        )

    with pytest.raises(ValueError, match="base"):
        image.prepare_context(**changed, output_dir=tmp_path / f"base-chain-{mutation}")


def test_docker_recipe_rejects_compile_mutation_even_when_rehashed(
    tmp_path: Path,
) -> None:
    """Adding a compiler command cannot be authorized by refreshing its digest."""
    *_, image = _modules()
    mutated = tmp_path / "docker"
    mutated.mkdir()
    for filename in image.DOCKER_FILES:
        source = DOCKER_ROOT / filename
        (mutated / source.name).write_bytes(source.read_bytes())
    with (mutated / "Dockerfile").open("a", encoding="utf-8") as stream:
        stream.write("\nRUN cmake --build /src\n")

    with pytest.raises(ValueError, match="compile"):
        image.implementation_digests(mutated)


def _mutated_docker_root(tmp_path: Path, dockerfile: str) -> Path:
    """Copy the install helpers and replace only the Dockerfile under audit."""
    *_, image = _modules()
    mutated = tmp_path / "docker"
    mutated.mkdir()
    for filename in image.DOCKER_FILES:
        source = DOCKER_ROOT / filename
        (mutated / source.name).write_bytes(source.read_bytes())
    (mutated / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return mutated


def test_install_only_audit_allows_disconnected_wheel_build_stage(
    tmp_path: Path,
) -> None:
    """A native wheel authority may compile outside the runtime dependency graph."""
    *_, image = _modules()
    dockerfile = """\
ARG BASE_IMAGE=registry.invalid/base@sha256:0000000000000000000000000000000000000000000000000000000000000000
FROM registry.invalid/builder@sha256:1111111111111111111111111111111111111111111111111111111111111111 AS wheel-build
COPY . /src
RUN cmake -S /src -B /build && cmake --build /build
FROM ${BASE_IMAGE} AS runtime-install
COPY install_ucm.py /usr/local/bin/install_ucm.py
COPY ${UCM_WHEEL} /tmp/${UCM_WHEEL}
RUN python /usr/local/bin/install_ucm.py
FROM runtime-install AS runtime
"""
    mutated = _mutated_docker_root(tmp_path, dockerfile)

    assert image.implementation_digests(mutated)["aggregate_sha256"].startswith(
        "sha256:"
    )


@pytest.mark.parametrize(
    "runtime_dependency",
    (
        "FROM wheel-build AS runtime-install",
        "FROM ${BASE_IMAGE} AS runtime-install\nCOPY --from=wheel-build /out /tmp",
    ),
)
def test_install_only_audit_rejects_direct_or_copied_build_stage_dependency(
    tmp_path: Path, runtime_dependency: str
) -> None:
    """The runtime target may not inherit or copy from a native build stage."""
    *_, image = _modules()
    dockerfile = f"""\
ARG BASE_IMAGE=registry.invalid/base@sha256:{'0' * 64}
FROM registry.invalid/builder@sha256:{'1' * 64} AS wheel-build
COPY . /src
RUN cmake -S /src -B /build && cmake --build /build
{runtime_dependency}
COPY ${{UCM_WHEEL}} /tmp/${{UCM_WHEEL}}
FROM runtime-install AS runtime
"""
    mutated = _mutated_docker_root(tmp_path, dockerfile)

    with pytest.raises(ValueError, match="install-only.*compile"):
        image.implementation_digests(mutated)


@pytest.mark.parametrize(
    "forbidden_instruction",
    (
        "COPY . /workspace/ucm",
        "COPY setup.py /workspace/ucm/setup.py",
        "RUN python -m build --wheel",
    ),
)
def test_install_only_audit_rejects_source_or_compile_in_runtime_graph(
    tmp_path: Path, forbidden_instruction: str
) -> None:
    """The final runtime graph contains an exact wheel install and no source build."""
    *_, image = _modules()
    dockerfile = f"""\
ARG BASE_IMAGE=registry.invalid/base@sha256:{'0' * 64}
FROM ${{BASE_IMAGE}} AS runtime-install
{forbidden_instruction}
COPY ${{UCM_WHEEL}} /tmp/${{UCM_WHEEL}}
FROM runtime-install AS runtime
"""
    mutated = _mutated_docker_root(tmp_path, dockerfile)

    with pytest.raises(ValueError, match="install-only"):
        image.implementation_digests(mutated)


def test_base_helper_rejects_mutable_and_mismatched_subjects(tmp_path: Path) -> None:
    """The FROM argument cannot be a tag or differ from the authorized subject."""
    _, _, context, recipe = _prepare(tmp_path)
    recipe_path = context / "image-recipe.json"
    helper = context / "verify_base_image.py"
    expected = recipe["payload"]["base"]["subject"]
    good = subprocess.run(
        [
            PYTHON,
            helper,
            "--recipe",
            recipe_path,
            "--base-image",
            expected,
            "--target-platform",
            "linux/arm64",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert good.returncode == 0, good.stderr
    assert json.loads(good.stdout)["status"] == "passed"
    for bad in ("python:3.12", f"docker.io/library/python@{'sha256:' + 'a' * 64}"):
        result = subprocess.run(
            [
                PYTHON,
                helper,
                "--recipe",
                recipe_path,
                "--base-image",
                bad,
                "--target-platform",
                "linux/arm64",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode != 0


@pytest.mark.parametrize("part", ["index", "manifest", "config"])
def test_base_helper_reopens_descriptor_chain_in_recipe(
    tmp_path: Path, part: str
) -> None:
    """Refreshing the recipe envelope cannot bless changed base descriptor bytes."""
    _, _, context, recipe = _prepare(tmp_path)
    changed = copy.deepcopy(recipe)
    changed["payload"]["base"][part]["raw"] += " "
    payload_bytes = json.dumps(
        changed["payload"], sort_keys=True, separators=(",", ":")
    ).encode()
    changed["payload_sha256"] = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
    changed_path = tmp_path / "changed-recipe.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")

    result = subprocess.run(
        [
            PYTHON,
            context / "verify_base_image.py",
            "--recipe",
            changed_path,
            "--base-image",
            recipe["payload"]["base"]["subject"],
            "--target-platform",
            recipe["payload"]["target_platform"],
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0


def test_install_helper_rejects_wrong_wheel_before_pip(tmp_path: Path) -> None:
    """A changed wheel must fail byte verification before package installation."""
    _, values, context, _ = _prepare(tmp_path)
    wheel_path = context / values["wheel_path"].name
    wheel_path.write_bytes(wheel_path.read_bytes() + b"changed")
    result = subprocess.run(
        [
            PYTHON,
            context / "install_ucm.py",
            "--recipe",
            context / "image-recipe.json",
            "--metadata",
            context / "image-metadata.json",
            "--wheel",
            wheel_path,
            "--output",
            tmp_path / "install.json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert not (tmp_path / "install.json").exists()


def test_verify_recomputes_closure_and_emits_deterministic_fixture_result(
    tmp_path: Path,
) -> None:
    """Result identity cannot depend on run time, signature, or evidence refresh."""
    image, _, context, recipe = _prepare(tmp_path)
    evidence = _evidence(recipe)

    first = image.verify_image(context, evidence)
    second = image.verify_image(context, copy.deepcopy(evidence))

    assert first == second
    assert first["result_sha256"].startswith("sha256:")
    assert first["status"] == "fixture-verified-unpublished"
    assert first["fixture_only"] is True
    assert first["unpublished"] is True
    assert first["publication_attempted"] is False
    assert first["runtime_validation"] == "external-required"
    assert first["device_validation"] == "external-required"
    assert first["oci"]["output"] == "local-oci"
    assert "timestamp" not in json.dumps(first).lower()
    assert "signature" not in json.dumps(first).lower()


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("base_verification", "status"), "failed", "base"),
        (("install", "pip_check"), "failed", "pip"),
        (("install", "imports", "ucm"), "failed", "import"),
        (("runtime", "abi", "status"), "failed", "ABI"),
        (("runtime", "device", "status"), "passed", "hardware"),
        (("runtime", "hardware_passed"), True, "hardware"),
        (("oci", "published"), True, "published"),
    ],
)
def test_verify_blocks_failed_gates_and_hardware_or_publication_claims(
    tmp_path: Path, path: tuple[str, ...], value: object, match: str
) -> None:
    """No required failure or fixture hardware/publication assertion can verify."""
    image, _, context, recipe = _prepare(tmp_path)
    evidence = _evidence(recipe)
    cursor = evidence
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value

    with pytest.raises(ValueError, match=match):
        image.verify_image(context, evidence)


def test_image_verify_cli_validates_schema_and_source_closure(tmp_path: Path) -> None:
    """The public CLI must read context bytes and reject an altered source closure."""
    _, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "image.oci.tar"
    _write_oci(oci_path, context, recipe, _evidence(recipe))

    result = _cli(
        "image",
        "verify",
        "--context",
        str(context),
        "--oci",
        str(oci_path),
        "--evidence-dir",
        str(tmp_path / "verified-evidence"),
    )
    assert json.loads(result.stdout)["status"] == "fixture-verified-unpublished"
    metadata = json.loads((context / "image-metadata.json").read_text())
    metadata["task"]["build_key_sha256"] = "sha256:" + "f" * 64
    (context / "image-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    failed = _cli(
        "image",
        "verify",
        "--context",
        str(context),
        "--oci",
        str(oci_path),
        "--evidence-dir",
        str(tmp_path / "rejected-evidence"),
        expect=2,
    )
    assert "metadata" in failed.stderr.lower()


@pytest.mark.parametrize(
    ("rootfs_mutation", "layer_media_types"),
    [
        ("missing-rootfs", ("application/vnd.oci.image.layer.v1.tar+gzip",)),
        ("wrong-type", ("application/vnd.oci.image.layer.v1.tar+gzip",)),
        ("wrong-diff-id", ("application/vnd.oci.image.layer.v1.tar+gzip",)),
        ("extra-diff-id", ("application/vnd.oci.image.layer.v1.tar+gzip",)),
        ("missing-diff-id", ("application/vnd.oci.image.layer.v1.tar+gzip",)),
        (
            "reversed-diff-ids",
            (
                "application/vnd.oci.image.layer.v1.tar+gzip",
                "application/vnd.oci.image.layer.v1.tar",
            ),
        ),
        ("unknown-media-type", ("application/vnd.example.layer+gzip",)),
    ],
)
def test_verify_oci_rejects_invalid_rootfs_layer_chain(
    tmp_path: Path,
    rootfs_mutation: str,
    layer_media_types: tuple[str, ...],
) -> None:
    """Runnable config diff_ids must match every decompressed layer in order."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / f"{rootfs_mutation}.oci.tar"
    mutation = None if rootfs_mutation == "unknown-media-type" else rootfs_mutation
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        layer_media_types=layer_media_types,
        rootfs_mutation=mutation,
    )

    with pytest.raises(ValueError, match="(rootfs|layer|diff|media)"):
        image.verify_oci(context, oci_path)


def test_verify_oci_accepts_uncompressed_layer_with_matching_diff_id(
    tmp_path: Path,
) -> None:
    """OCI's standard uncompressed tar layer media type remains supported."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "uncompressed.oci.tar"
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        layer_media_types=("application/vnd.oci.image.layer.v1.tar",),
    )

    result = image.verify_oci(context, oci_path)
    assert result["status"] == "fixture-verified-unpublished"


def test_verify_oci_exports_and_revalidates_compact_raw_descriptor_evidence(
    tmp_path: Path,
) -> None:
    """The uploaded evidence must be derived from real verified OCI raw bytes."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "image.oci.tar"
    evidence_dir = tmp_path / "compact-evidence"
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        layer_media_types=(
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.oci.image.layer.v1.tar",
        ),
    )

    result = image.verify_oci(context, oci_path, evidence_dir=evidence_dir)
    assert {path.name for path in evidence_dir.iterdir()} == {
        "oci-layout.json",
        "index.json",
        "manifest.json",
        "config.json",
        "closure.json",
    }
    closure = image.validate_compact_oci_evidence(
        evidence_dir,
        image_result=result,
        image_recipe_path=context / "image-recipe.json",
        image_metadata_path=context / "image-metadata.json",
        image_prepare_path=context / "image-recipe.json",
        wheel_path=context / recipe["payload"]["wheel"]["filename"],
        buildkit_metadata={
            "containerimage.digest": result["oci"]["digest"],
            "containerimage.config.digest": json.loads(
                (evidence_dir / "closure.json").read_text(encoding="utf-8")
            )["config_descriptor"]["digest"],
            "containerimage.descriptor": json.dumps(
                json.loads((evidence_dir / "closure.json").read_text(encoding="utf-8"))[
                    "manifest_descriptor"
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )
    assert closure["oci_digest"] == result["oci"]["digest"]
    assert len(closure["layers"]) == len(closure["diff_ids"]) == 2

    changed = json.loads((evidence_dir / "manifest.json").read_text(encoding="utf-8"))
    changed["layers"] = []
    (evidence_dir / "manifest.json").write_text(
        json.dumps(changed, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="(manifest|digest|layer)"):
        image.validate_compact_oci_evidence(
            evidence_dir,
            image_result=result,
            image_recipe_path=context / "image-recipe.json",
            image_metadata_path=context / "image-metadata.json",
            image_prepare_path=context / "image-recipe.json",
            wheel_path=context / recipe["payload"]["wheel"]["filename"],
            buildkit_metadata={},
        )


def test_image_verify_cli_does_not_accept_caller_authored_evidence(
    tmp_path: Path,
) -> None:
    """A hand-made evidence summary cannot replace observation of the OCI bytes."""
    _, _, context, recipe = _prepare(tmp_path)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_evidence(recipe)), encoding="utf-8")

    result = _cli(
        "image",
        "verify",
        "--context",
        str(context),
        "--oci",
        str(evidence_path),
        "--evidence",
        str(evidence_path),
        expect=2,
    )

    assert "--evidence" in result.stderr


@pytest.mark.parametrize("tag", ["v0.10.2-310p", "v0.10.2-a5", "v0.10.2-a2"])
def test_image_builder_rejects_unsupported_or_mixed_ascend_tasks(
    tmp_path: Path, tag: str
) -> None:
    """310P/A5 and an A2 upstream paired with the selected A3 wheel must fail."""
    *_, image = _modules()
    values = _actual_inputs(tmp_path)
    values["source_case"]["upstream_snapshot"]["upstream_tag"] = tag

    with pytest.raises(ValueError):
        image.prepare_context(**values, output_dir=tmp_path / "bad-mixed")
