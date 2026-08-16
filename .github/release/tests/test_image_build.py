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
import runpy
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

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


def _fixture_resolved_plan() -> dict[str, object]:
    core, _, registry, _ = _modules()
    catalog = core.load_catalog()
    fixture = json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )
    return registry.resolve_catalog(
        catalog,
        source_sha="0" * 40,
        lane="feature-candidate",
        fixture=fixture,
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
                "Requires-Dist: packaging==24.2",
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


def _install_module():
    return SimpleNamespace(
        **runpy.run_path(
            str(DOCKER_ROOT / "install_ucm.py"),
            run_name="ucm_release_install_test",
        )
    )


def _inspect_module():
    return SimpleNamespace(
        **runpy.run_path(
            str(DOCKER_ROOT / "inspect_runtime.py"),
            run_name="ucm_release_inspect_test",
        )
    )


def _write_metadata_wheel(
    path: Path, *, distribution: str, version: str, requires_dist: list[str]
) -> None:
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
        *(f"Requires-Dist: {requirement}" for requirement in requires_dist),
        "",
    ]
    dist_info = distribution.replace("-", "_")
    module = {"uc-manager": "ucm"}.get(distribution, distribution.replace("-", "_"))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}-{version}.dist-info/METADATA", "\n".join(metadata)
        )
        archive.writestr(
            f"{dist_info}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{module}/__init__.py", f"__version__ = {version!r}\n")
        archive.writestr(f"{dist_info}-{version}.dist-info/RECORD", "")


def _real_install_case(
    tmp_path: Path,
    profile_id: str = "cuda130-amd64",
    *,
    runtime_dependencies: list[dict[str, str]] | None = None,
):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    version = "3.1.0"
    ucm_path = wheelhouse / "uc_manager-3.1.0-cp312-cp312-linux_x86_64.whl"
    declarations = runtime_dependencies or [
        {
            "name": "packaging",
            "version": "24.2",
            "requirement": "packaging==24.2",
            "import_name": "packaging",
        },
        {
            "name": "wrapt",
            "version": "1.17.2",
            "requirement": "wrapt==1.17.2",
            "import_name": "wrapt",
        },
    ]
    declarations = sorted(copy.deepcopy(declarations), key=lambda item: item["name"])
    requirements = [item["requirement"] for item in declarations]
    _write_metadata_wheel(
        ucm_path,
        distribution="uc-manager",
        version=version,
        requires_dist=requirements,
    )

    def digest(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    dependency_paths: dict[str, Path] = {}
    resolved_dependencies: list[dict[str, str]] = []
    for declaration in declarations:
        normalized = declaration["name"].replace("-", "_")
        path = wheelhouse / f"{normalized}-{declaration['version']}-py3-none-any.whl"
        _write_metadata_wheel(
            path,
            distribution=declaration["name"],
            version=declaration["version"],
            requires_dist=[],
        )
        dependency_paths[declaration["name"]] = path
        resolved_dependencies.append(
            {
                **declaration,
                "filename": path.name,
                "sha256": digest(path),
            }
        )

    lock_path = wheelhouse / "requirements.lock"
    ucm_sha256 = digest(ucm_path)
    lock_path.write_text(
        f"uc-manager @ file:///wheelhouse/{ucm_path.name} --hash={ucm_sha256}\n"
        + "".join(
            f"{record['name']} @ file:///wheelhouse/{record['filename']} "
            f"--hash={record['sha256']}\n"
            for record in resolved_dependencies
        ),
        encoding="utf-8",
    )
    authority_payload = {
        "kind": "ucm-real-image-source-authority",
        "candidate_kind": "real-candidate",
        "fixture_only": False,
    }
    authority_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                authority_payload, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    authority = {**authority_payload, "authority_sha256": authority_sha256}
    authority_path = tmp_path / "image-authority.json"
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    payload = {
        "candidate_kind": "real-candidate",
        "profile_id": profile_id,
        "authority_sha256": authority_sha256,
        "wheel": {
            "filename": ucm_path.name,
            "sha256": ucm_sha256,
            "version": version,
        },
        "runtime_dependencies": resolved_dependencies,
        "context_files": [
            ucm_path.name,
            *(record["filename"] for record in resolved_dependencies),
            lock_path.name,
        ],
        "dependency_lock": {
            "sha256": digest(lock_path),
            "preinstall_command": [
                "python",
                "-m",
                "pip",
                "uninstall",
                "--yes",
                "uc-manager",
                *(record["name"] for record in resolved_dependencies),
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
        },
    }
    recipe = {
        "payload": payload,
        "payload_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    recipe_path = tmp_path / "image-recipe.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")

    distributions: dict[str, SimpleNamespace] = {}
    distributions_to_write = [
        ("uc-manager", version, requirements, ucm_path.name, ucm_sha256),
        *[
            (
                record["name"],
                record["version"],
                [],
                record["filename"],
                record["sha256"],
            )
            for record in resolved_dependencies
        ],
    ]
    for name, installed_version, requires, filename, sha256 in distributions_to_write:
        dist_info = tmp_path / "installed" / name
        dist_info.mkdir(parents=True)
        (dist_info / "direct_url.json").write_text(
            json.dumps(
                {
                    "url": f"file:///wheelhouse/{filename}",
                    "archive_info": {
                        "hash": "sha256=" + sha256.removeprefix("sha256:")
                    },
                }
            ),
            encoding="utf-8",
        )
        distributions[name] = SimpleNamespace(
            version=installed_version, requires=requires, _path=dist_info
        )
    return {
        "recipe_path": recipe_path,
        "authority_path": authority_path,
        "wheelhouse": wheelhouse,
        "lock_path": lock_path,
        "version": version,
        "runtime_dependencies": resolved_dependencies,
        "distributions": distributions,
    }


def _stub_real_install_environment(
    installer,
    case: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    reject_global_check: bool = False,
) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str], *, check: bool):
        assert check is True
        commands.append(command)
        if reject_global_check and command[1:] == ["-m", "pip", "check"]:
            raise subprocess.CalledProcessError(1, command, stderr="base conflict")
        return subprocess.CompletedProcess(command, 0)

    def distribution(name: str):
        value = case["distributions"].get(name)
        if value is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return value

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(installer.importlib.metadata, "distribution", distribution)
    monkeypatch.setattr(installer.importlib, "import_module", lambda _name: object())
    return commands


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
    value["inventory_sha256"] = registry.fixture_inventory_digest(value)
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
    manifest = core._build_fixture_release_manifest()
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
    catalog = core.load_catalog()
    source_case = {
        "release_manifest": manifest,
        "wheel_records": [wheel_record],
        "spec_id": spec["spec_id"],
        "upstream_snapshot": _snapshot(),
        "catalog": catalog,
        "compatibility_rule_id": "ascend-supported",
        "implementation_digest": image.implementation_digests()["aggregate_sha256"],
    }
    candidate = registry.build_fixture_candidate(**source_case, fixture_mode=True)
    inventory = _inventory(registry)
    task = registry.reconcile_fixture_candidate(candidate, inventory)["tasks"][0]
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
            "requires_dist": ["packaging==24.2", "wrapt==1.17.2"],
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
                "packaging": "24.2",
                "wrapt": "1.17.2",
            },
            "imports": {
                "ucm": "passed",
                "packaging": "passed",
                "wrapt": "passed",
            },
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
    extra_layer_members: tuple[tuple[str, str], ...] = (),
    layer_member_types: dict[str, str] | None = None,
    outer_extra_members: tuple[tuple[str, bytes], ...] = (),
) -> None:
    """Write a minimal standard OCI layout containing the observable image files."""
    member_types = layer_member_types or {}

    def add_member(
        archive: tarfile.TarFile, name: str, content: bytes, member_type: str
    ) -> None:
        info = tarfile.TarInfo(name)
        info.mode = 0o644
        info.mtime = 0
        if member_type == "file":
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        elif member_type == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
            archive.addfile(info)
        elif member_type == "character-device":
            info.type = tarfile.CHRTYPE
            info.devmajor = 1
            info.devminor = 3
            archive.addfile(info)
        else:
            raise AssertionError(f"unknown test tar member type {member_type}")

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
                add_member(layer, name, content, member_types.get(name, "file"))
            if layer_index == 0:
                for name, member_type in extra_layer_members:
                    add_member(layer, name, b"extra layer member", member_type)
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
    if rootfs_mutation == "wrong-layer-descriptor-digest":
        layer_descriptors[0]["digest"] = "sha256:" + "c" * 64
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
        for name, content in [*sorted(files.items()), *outer_extra_members]:
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


def test_native_build_and_runtime_preserve_mooncake_loader_path() -> None:
    """Non-interactive builds and runtime inspection must see /usr/local/lib."""
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    inspector = (RELEASE_ROOT / "docker/inspect_runtime.py").read_text(encoding="utf-8")
    inherited_loader_path = (
        'ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"'
    )
    assert dockerfile.count("ARG LD_LIBRARY_PATH") == 3
    assert dockerfile.count(inherited_loader_path) == 3
    assert dockerfile.count("RUN ldconfig /usr/local/lib") == 2
    assert "wheel check-environment" in dockerfile
    assert "libmooncake_store.so" not in dockerfile
    assert "grep -F 'not found'" not in dockerfile
    assert '[*directories, os.environ.get("LD_LIBRARY_PATH", "")]' in inspector


def test_wheel_base_uses_catalog_library_caches_without_backend_branches() -> None:
    """Builder library cache roots come only from each selected wheel task."""
    core, *_ = _modules()
    catalog = core.load_catalog()
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    wheel_base = dockerfile.split(
        "FROM ${UCM_BUILDER_IMAGE} AS wheel-base", maxsplit=1
    )[1].split("FROM wheel-base AS wheel-build", maxsplit=1)[0]
    runtime_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-install", maxsplit=1
    )[1].split("FROM runtime-install AS runtime", maxsplit=1)[0]
    runtime_real_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-real-install", maxsplit=1
    )[1].split("FROM runtime-real-install AS runtime-real", maxsplit=1)[0]

    cache_paths: dict[str, set[str]] = {}
    for profile in catalog["wheel_profiles"]:
        checks = profile["builders"]["amd64"]["checks"]
        cache_paths[profile["accelerator"]] = {
            check["path"] for check in checks if check["kind"] == "library-cache"
        }
    assert cache_paths["cuda"] == {"/usr/local/lib", "/usr/local/cuda/lib64"}
    assert cache_paths["ascend"] == {"/usr/local/lib"}
    assert 'if [[ "${PLATFORM}" == "cuda" ]]; then' not in wheel_base
    assert "/usr/local/cuda/lib64" not in wheel_base
    assert "RUN ldconfig /usr/local/lib\n" not in wheel_base
    for runtime_stage in (runtime_install, runtime_real_install):
        assert runtime_stage.count("RUN ldconfig /usr/local/lib") == 1
        assert "/usr/local/cuda/lib64" not in runtime_stage


def test_runtime_stages_remove_ldconfig_aux_cache_in_the_same_layer() -> None:
    """Runtime loader refreshes must not persist host-specific ldconfig metadata."""
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    wheel_base = dockerfile.split(
        "FROM ${UCM_BUILDER_IMAGE} AS wheel-base", maxsplit=1
    )[1].split("FROM wheel-base AS wheel-build", maxsplit=1)[0]
    runtime_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-install", maxsplit=1
    )[1].split("FROM runtime-install AS runtime", maxsplit=1)[0]
    runtime_real_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-real-install", maxsplit=1
    )[1].split("FROM runtime-real-install AS runtime-real", maxsplit=1)[0]
    deterministic_refresh = (
        "RUN ldconfig /usr/local/lib && rm -f /var/cache/ldconfig/aux-cache"
    )

    assert "/var/cache/ldconfig/aux-cache" not in wheel_base
    assert dockerfile.count(deterministic_refresh) == 2
    for runtime_stage in (runtime_install, runtime_real_install):
        assert runtime_stage.count(deterministic_refresh) == 1
        assert "RUN ldconfig /usr/local/lib\n" not in runtime_stage
        assert "RUN rm -f /var/cache/ldconfig/aux-cache" not in runtime_stage


def test_real_runtime_oci_labels_bind_the_planned_source_repository() -> None:
    """GHCR package linkage comes from the protected resolved plan."""
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    runtime_real_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-real-install", maxsplit=1
    )[1].split("FROM runtime-real-install AS runtime-real", maxsplit=1)[0]

    assert runtime_real_install.count("ARG UCM_SOURCE_REPOSITORY_URL") == 1
    assert (
        runtime_real_install.count(
            'org.opencontainers.image.source="${UCM_SOURCE_REPOSITORY_URL}"'
        )
        == 1
    )
    assert "SuperMarioYL/unified-cache-management" not in runtime_real_install


def test_runtime_stages_invoke_all_python_helpers_through_python3() -> None:
    """A runtime base with python3 but no bare python must reach every helper."""
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    runtime_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-install", maxsplit=1
    )[1].split("FROM runtime-install AS runtime", maxsplit=1)[0]
    runtime_real_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-real-install", maxsplit=1
    )[1].split("FROM runtime-real-install AS runtime-real", maxsplit=1)[0]

    for runtime_stage in (runtime_install, runtime_real_install):
        assert runtime_stage.count("python3 /usr/local/bin/") == 3
        assert "python /usr/local/bin/" not in runtime_stage


@pytest.mark.parametrize(
    "profile_id", ["cuda130-amd64", "cann900-a2-amd64", "cann900-a3-arm64"]
)
def test_real_install_ignores_unrelated_base_dependency_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_id: str
) -> None:
    """Only the offline UCM package scope, not the base image, is this gate."""
    installer = _install_module()
    case = _real_install_case(tmp_path, profile_id)
    commands = _stub_real_install_environment(
        installer, case, monkeypatch, reject_global_check=True
    )

    result = installer.install_real(
        case["recipe_path"],
        case["authority_path"],
        case["wheelhouse"],
        case["lock_path"],
    )

    assert len(commands) == 2
    assert result["pip_check"] == "passed"


def test_real_install_records_exact_ucm_package_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw evidence must say precisely which installed dependencies were checked."""
    installer = _install_module()
    case = _real_install_case(tmp_path)
    _stub_real_install_environment(installer, case, monkeypatch)

    result = installer.install_real(
        case["recipe_path"],
        case["authority_path"],
        case["wheelhouse"],
        case["lock_path"],
    )

    assert result["dependency_check"] == {
        "kind": "ucm-package-scope",
        "scope": ["uc-manager", "packaging", "wrapt"],
        "packages": {
            "uc-manager": {
                "version": case["version"],
                "requires_dist": ["packaging==24.2", "wrapt==1.17.2"],
            },
            "packaging": {"version": "24.2", "requires_dist": []},
            "wrapt": {"version": "1.17.2", "requires_dist": []},
        },
        "requirements": [
            {
                "owner": "uc-manager",
                "requirement": "packaging==24.2",
                "dependency": "packaging",
                "installed_version": "24.2",
                "status": "passed",
            },
            {
                "owner": "uc-manager",
                "requirement": "wrapt==1.17.2",
                "dependency": "wrapt",
                "installed_version": "1.17.2",
                "status": "passed",
            },
        ],
        "status": "passed",
    }
    assert result["installed_packages"] == {
        "uc-manager": case["version"],
        "packaging": "24.2",
        "wrapt": "1.17.2",
    }
    assert result["imports"] == {
        "ucm": "passed",
        "packaging": "passed",
        "wrapt": "passed",
    }


def test_real_install_accepts_task_bound_noncurrent_and_third_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime installation must consume an arbitrary exact task dependency set."""
    installer = _install_module()
    dependencies = [
        {
            "name": "packaging",
            "version": "24.2",
            "requirement": "packaging==24.2",
            "import_name": "packaging",
        },
        {
            "name": "wrapt",
            "version": "1.18.0",
            "requirement": "wrapt==1.18.0",
            "import_name": "wrapt",
        },
        {
            "name": "alpha-runtime",
            "version": "2.0",
            "requirement": "alpha-runtime==2.0",
            "import_name": "alpha_runtime",
        },
    ]
    case = _real_install_case(tmp_path, runtime_dependencies=dependencies)
    commands = _stub_real_install_environment(installer, case, monkeypatch)

    result = installer.install_real(
        case["recipe_path"],
        case["authority_path"],
        case["wheelhouse"],
        case["lock_path"],
    )

    assert result["installed_packages"] == {
        "alpha-runtime": "2.0",
        "packaging": "24.2",
        "uc-manager": case["version"],
        "wrapt": "1.18.0",
    }
    assert result["imports"] == {
        "alpha_runtime": "passed",
        "packaging": "passed",
        "ucm": "passed",
        "wrapt": "passed",
    }
    assert result["runtime_dependencies"] == sorted(
        case["runtime_dependencies"], key=lambda item: item["name"]
    )
    assert len(commands) == 2


def test_dependency_lock_producer_installs_and_imports_from_isolated_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The locked packaging wheel, not a base-site package, satisfies runtime import."""
    *_, image = _modules()
    installer = _install_module()
    case = _real_install_case(tmp_path)
    recipe = json.loads(case["recipe_path"].read_text(encoding="utf-8"))
    payload = recipe["payload"]
    task = _real_image_task(image)
    task["runtime_dependencies"] = copy.deepcopy(payload["runtime_dependencies"])
    task["authority_sha256"] = image.sha256_value(
        {key: value for key, value in task.items() if key != "authority_sha256"}
    )
    produced = image.build_real_dependency_lock(
        task,
        case["wheelhouse"] / payload["wheel"]["filename"],
        [
            case["wheelhouse"] / dependency["filename"]
            for dependency in payload["runtime_dependencies"]
        ],
    )
    assert produced["requirements"] == case["lock_path"].read_text(encoding="utf-8")

    isolated_site = tmp_path / "isolated-site"
    isolated_site.mkdir()
    real_run = subprocess.run

    def install_into_isolated_site(command: list[str], *, check: bool):
        assert check is True
        if command[3] == "install":
            for wheel_path in sorted(case["wheelhouse"].glob("*.whl")):
                with zipfile.ZipFile(wheel_path) as archive:
                    archive.extractall(isolated_site)
                _, _, _ = installer._wheel_metadata(wheel_path)
                dist_info = next(
                    path
                    for path in isolated_site.glob("*.dist-info")
                    if path.name.startswith(
                        wheel_path.name.split("-", 1)[0].replace("-", "_")
                    )
                )
                (dist_info / "direct_url.json").write_text(
                    json.dumps(
                        {
                            "url": f"file:///wheelhouse/{wheel_path.name}",
                            "archive_info": {
                                "hash": "sha256="
                                + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
                            },
                        }
                    ),
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(command, 0)

    def isolated_distribution(name: str):
        distributions = {
            distribution.metadata["Name"]: distribution
            for distribution in importlib.metadata.distributions(path=[isolated_site])
        }
        value = distributions.get(name)
        if value is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return value

    def isolated_import(name: str):
        code = (
            "import importlib,pathlib,sys;"
            f"sys.path.insert(0,{str(isolated_site)!r});"
            f"m=importlib.import_module({name!r});"
            f"assert pathlib.Path(m.__file__).is_relative_to(pathlib.Path({str(isolated_site)!r}))"
        )
        completed = real_run(
            [sys.executable, "-I", "-c", code],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return object()

    monkeypatch.setattr(installer.subprocess, "run", install_into_isolated_site)
    monkeypatch.setattr(
        installer.importlib.metadata, "distribution", isolated_distribution
    )
    monkeypatch.setattr(installer.importlib, "import_module", isolated_import)

    result = installer.install_real(
        case["recipe_path"],
        case["authority_path"],
        case["wheelhouse"],
        case["lock_path"],
    )

    assert result["imports"]["packaging"] == "passed"


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_real_install_rejects_missing_or_tampered_runtime_lock(
    tmp_path: Path, mutation: str
) -> None:
    installer = _install_module()
    case = _real_install_case(tmp_path)
    if mutation == "missing":
        case["lock_path"].unlink()
    else:
        case["lock_path"].write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="lock|wheelhouse"):
        installer.install_real(
            case["recipe_path"],
            case["authority_path"],
            case["wheelhouse"],
            case["lock_path"],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-wrapt",
        "wrong-wrapt-version",
        "wrong-ucm-version",
        "ucm-requires-drift",
        "wrapt-requires-dependency",
    ],
)
def test_real_install_rejects_installed_package_scope_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Missing, wrong, or dependency-bearing installed packages fail closed."""
    installer = _install_module()
    case = _real_install_case(tmp_path)
    distributions = case["distributions"]
    if mutation == "missing-wrapt":
        distributions.pop("wrapt")
    elif mutation == "wrong-wrapt-version":
        distributions["wrapt"].version = "1.17.1"
    elif mutation == "wrong-ucm-version":
        distributions["uc-manager"].version = "3.1.1"
    elif mutation == "ucm-requires-drift":
        distributions["uc-manager"].requires = ["wrapt>=1.17.2"]
    else:
        distributions["wrapt"].requires = ["typing-extensions"]
    _stub_real_install_environment(installer, case, monkeypatch)

    with pytest.raises(ValueError, match="installed UCM package scope"):
        installer.install_real(
            case["recipe_path"],
            case["authority_path"],
            case["wheelhouse"],
            case["lock_path"],
        )


def test_native_build_runs_the_locked_cmake_from_cpython_scripts() -> None:
    """The build must not fall back to an older CMake already on the base PATH."""
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    locked_cmake = (
        "locked_cmake=\"$(ucm-python -c 'import sysconfig; "
        'print(sysconfig.get_path("scripts"))\')/cmake"'
    )
    locked_scripts_path = (
        "PATH=\"$(ucm-python -c 'import sysconfig; "
        'print(sysconfig.get_path("scripts"))\'):${PATH}"'
    )
    assert locked_cmake in dockerfile
    assert 'test -x "${locked_cmake}"' in dockerfile
    assert f"export {locked_scripts_path}" in dockerfile
    assert "wheel check-environment" in dockerfile
    assert "command -v cmake" not in dockerfile


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


@pytest.mark.parametrize(
    "forbidden_instruction",
    (
        'COPY ["setup.py", "/workspace/ucm/setup.py"]',
        'COPY ["ucm", "/workspace/ucm/ucm"]',
        'COPY [".", "/workspace/ucm"]',
        'COPY --chown=0:0 ["setup.py", "/workspace/ucm/setup.py"]',
        'COPY ["/setup.py", "/workspace/ucm/setup.py"]',
        'COPY ["././setup.py", "/workspace/ucm/setup.py"]',
        'COPY ["safe/../setup.py", "/workspace/ucm/setup.py"]',
        'RUN ["python", "-m", "build", "--wheel"]',
    ),
)
def test_install_only_audit_rejects_json_source_and_build_instructions(
    tmp_path: Path, forbidden_instruction: str
) -> None:
    """JSON-form Docker instructions cannot bypass the runtime source-build gate."""
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


def test_install_only_audit_accepts_legal_multiline_shell_run(tmp_path: Path) -> None:
    """A legal backslash-continued runtime command remains accepted."""
    *_, image = _modules()
    dockerfile = f"""\
ARG BASE_IMAGE=registry.invalid/base@sha256:{'0' * 64}
FROM ${{BASE_IMAGE}} AS runtime-install
RUN python -c 'print("runtime")' \\
 && python -c 'print("still runtime")'
COPY ${{UCM_WHEEL}} /tmp/${{UCM_WHEEL}}
FROM runtime-install AS runtime
"""
    mutated = _mutated_docker_root(tmp_path, dockerfile)

    result = image.implementation_digests(mutated)
    assert result["files"]["Dockerfile"].startswith("sha256:")


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


def test_fixture_context_uses_its_embedded_catalog_without_current_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen fixture case supplies its own dependency authority."""
    *_, image = _modules()
    values = _actual_inputs(tmp_path)
    expected = image.release_core.python_runtime_requirements(
        values["source_case"]["catalog"]
    )
    monkeypatch.setattr(
        image.release_core,
        "load_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fixture context reopened the current catalog")
        ),
    )

    recipe = image.prepare_context(
        **values,
        output_dir=tmp_path / "frozen-fixture-context",
    )

    assert recipe["payload"]["wheel"]["requires_dist"] == expected


def test_fixture_evidence_uses_recipe_dependencies_without_current_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed dependency names and versions remain recipe-bound."""
    image, _, _, recipe = _prepare(tmp_path)
    evidence = _evidence(recipe)
    recipe = copy.deepcopy(recipe)
    evidence = copy.deepcopy(evidence)
    recipe["payload"]["wheel"]["requires_dist"] = ["futuredep==7.4"]
    recipe["payload_sha256"] = image.sha256_value(recipe["payload"])
    evidence["recipe_sha256"] = recipe["payload_sha256"]
    evidence["install"]["requires_dist"] = ["futuredep==7.4"]
    evidence["install"]["installed_packages"] = {
        "uc-manager": recipe["payload"]["wheel"]["version"],
        "futuredep": "7.4",
    }
    evidence["install"]["imports"] = {
        "ucm": "passed",
        "futuredep": "passed",
    }
    monkeypatch.setattr(
        image.release_core,
        "load_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fixture verifier reopened the current catalog")
        ),
    )

    assert image._verify_evidence(recipe, evidence)["install"] == "passed"


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
            "wrong-layer-descriptor-digest",
            ("application/vnd.oci.image.layer.v1.tar+gzip",),
        ),
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


def test_verify_oci_accepts_literal_backslash_in_linux_layer_member(
    tmp_path: Path,
) -> None:
    """A POSIX layer filename may contain a literal backslash without extraction."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "posix-backslash.oci.tar"
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        extra_layer_members=(
            (r"usr/lib/systemd/system/system-systemd\x2dcryptsetup.slice", "file"),
        ),
    )

    result = image.verify_oci(context, oci_path)
    assert result["status"] == "fixture-verified-unpublished"


@pytest.mark.parametrize("member_name", ["/absolute", "a/../b", "a/./b", "a//b"])
def test_verify_oci_rejects_noncanonical_linux_layer_member(
    tmp_path: Path, member_name: str
) -> None:
    """Allowing POSIX backslashes must not admit absolute or normalized paths."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "noncanonical-layer.oci.tar"
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        extra_layer_members=((member_name, "file"),),
    )

    with pytest.raises(ValueError, match="noncanonical member"):
        image.verify_oci(context, oci_path)


def test_verify_oci_keeps_outer_layout_backslash_strict(tmp_path: Path) -> None:
    """The host-opened OCI layout must not inherit Linux layer filename rules."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "outer-backslash.oci.tar"
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        outer_extra_members=((r"blobs\sha256\not-a-layout-path", b"extra"),),
    )

    with pytest.raises(ValueError, match="OCI layout contains noncanonical member"):
        image.verify_oci(context, oci_path)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("duplicate", "duplicate member"),
        ("symlink", "duplicate/non-file recipe evidence"),
        ("character-device", "duplicate/non-file recipe evidence"),
    ],
)
def test_verify_oci_still_rejects_duplicate_or_nonfile_layer_evidence(
    tmp_path: Path, mutation: str, match: str
) -> None:
    """Streaming a layer must preserve duplicate and evidence-type checks."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / f"{mutation}.oci.tar"
    recipe_member = "usr/local/share/ucm-release/image-recipe.json"
    if mutation == "duplicate":
        _write_oci(
            oci_path,
            context,
            recipe,
            _evidence(recipe),
            extra_layer_members=(
                (r"usr/lib/systemd/system/system-systemd\x2dcryptsetup.slice", "file"),
                (r"usr/lib/systemd/system/system-systemd\x2dcryptsetup.slice", "file"),
            ),
        )
    else:
        _write_oci(
            oci_path,
            context,
            recipe,
            _evidence(recipe),
            layer_member_types={recipe_member: mutation},
        )

    with pytest.raises(ValueError, match=match):
        image.verify_oci(context, oci_path)


def test_verify_oci_streams_compressed_layers_without_gzip_decompress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full-layer gzip materialization must not be required to verify an OCI."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "compressed.oci.tar"
    _write_oci(oci_path, context, recipe, _evidence(recipe))

    def reject_full_decompression(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("gzip.decompress materializes the complete layer")

    monkeypatch.setattr(gzip, "decompress", reject_full_decompression)

    result = image.verify_oci(context, oci_path)
    assert result["status"] == "fixture-verified-unpublished"


def test_verify_oci_bounds_descriptor_and_archive_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-GiB descriptor/archive bytes must be consumed in bounded chunks."""
    image, _, context, recipe = _prepare(tmp_path)
    oci_path = tmp_path / "uncompressed.oci.tar"
    evidence_dir = tmp_path / "compact-evidence"
    _write_oci(
        oci_path,
        context,
        recipe,
        _evidence(recipe),
        layer_media_types=("application/vnd.oci.image.layer.v1.tar",),
    )
    with oci_path.open("rb") as stream:
        expected_archive_sha256 = (
            "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        )

    original_member_read = tarfile.ExFileObject.read

    def bounded_member_read(
        stream: tarfile.ExFileObject, size: int | None = None
    ) -> bytes:
        if size is None or size < 0 or size > 1024 * 1024:
            raise AssertionError("OCI member read must be bounded to one MiB")
        return original_member_read(stream, size)

    original_path_read_bytes = Path.read_bytes

    def reject_archive_read_bytes(path: Path) -> bytes:
        if path == oci_path:
            raise AssertionError("OCI archive must not be materialized with read_bytes")
        return original_path_read_bytes(path)

    monkeypatch.setattr(tarfile.ExFileObject, "read", bounded_member_read)
    monkeypatch.setattr(Path, "read_bytes", reject_archive_read_bytes)

    result = image.verify_oci(context, oci_path, evidence_dir=evidence_dir)
    closure = json.loads((evidence_dir / "closure.json").read_text(encoding="utf-8"))
    assert result["status"] == "fixture-verified-unpublished"
    assert closure["archive_sha256"] == expected_archive_sha256


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


def _real_image_task(image, spec_id: str = "cuda130-amd64"):
    plan = _fixture_resolved_plan()
    task = next(item for item in plan["image_tasks"] if item["spec_id"] == spec_id)
    return image.real_image_authority_from_plan(
        plan,
        task_id=task["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )


def test_current_catalog_fixture_real_authorities_match_the_resolved_plan() -> None:
    """The current member list is fixture evidence; the plan is the authority."""
    *_, image = _modules()
    plan = _fixture_resolved_plan()
    authorities = [
        image.real_image_authority_from_plan(
            plan,
            task_id=task["task_id"],
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
        for task in plan["image_tasks"]
    ]

    assert [item["spec_id"] for item in authorities] == [
        item["spec_id"] for item in plan["image_tasks"]
    ]
    assert [item["task_sha256"] for item in authorities] == [
        item["task_sha256"] for item in plan["image_tasks"]
    ]
    assert all(
        item["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
        for item in authorities
    )
    assert all(item["kind"] == "ucm-real-image-task-authority" for item in authorities)
    assert all(item["candidate_kind"] == "real-candidate" for item in authorities)
    assert all(item["fixture_only"] is False for item in authorities)
    assert all(item["unpublished"] is True for item in authorities)
    assert all(item["publication_attempted"] is False for item in authorities)
    assert len({item["authority_sha256"] for item in authorities}) == len(
        plan["image_tasks"]
    )
    assert all(
        item["external_required_dependencies"] == []
        for item in authorities
        if item["profile_id"] == "cuda130"
    )
    assert all(
        item["external_required_dependencies"]
        == [
            {
                "dependency": "libascend_hal.so",
                "provider": "host-ascend-driver",
                "expected_mount_root": "/usr/local/Ascend/driver/lib64",
                "relation": "transitive",
                "required_at": "device-runtime",
            }
        ]
        for item in authorities
        if item["profile_id"] != "cuda130"
    )
    assert {
        (item["runtime"]["repository"], item["target_repository"])
        for item in authorities
    } == {
        ("docker.io/vllm/vllm-openai", "ghcr.io/supermarioyl/vllm-openai"),
        ("quay.io/ascend/vllm-ascend", "ghcr.io/supermarioyl/vllm-ascend"),
    }
    assert {
        (
            item["profile_id"],
            tuple(sorted(item["runtime_patch_variants"].items())),
        )
        for item in authorities
    } == {
        ("cuda130", (("vllm", "default"),)),
        ("cann900-a2", (("vllm", "default"), ("vllm-ascend", "a2"))),
        ("cann900-a3", (("vllm", "default"), ("vllm-ascend", "a3"))),
    }


@pytest.mark.parametrize("mutation", ["missing", "tampered", "foreign"])
def test_real_image_authority_rejects_missing_tampered_or_foreign_variant_map(
    mutation: str,
) -> None:
    """The exact product map is part of the hashed image-task authority."""
    core, _, _, image = _modules()
    plan = _fixture_resolved_plan()
    task = next(
        item
        for item in plan["image_tasks"]
        if item["profile_id"] == "cann900-a2" and item["cpu_arch"] == "amd64"
    )
    wheel_task = next(
        item for item in plan["wheel_tasks"] if item["task_id"] == task["wheel_task_id"]
    )
    image._real_image_authority_from_selected_tasks(
        copy.deepcopy(task),
        copy.deepcopy(wheel_task),
        resolved_plan_sha256=plan["resolved_plan_sha256"],
        source_repository=plan["source"]["repository"],
    )
    if mutation == "missing":
        task["runtime_patch_variants"].pop("vllm")
    elif mutation == "tampered":
        task["runtime_patch_variants"]["vllm-ascend"] = "a3"
    else:
        task["runtime_patch_variants"]["foreign"] = "a2"
    task["task_sha256"] = core.sha256_value(
        {key: value for key, value in task.items() if key != "task_sha256"}
    )

    with pytest.raises(ValueError, match="variant"):
        image._real_image_authority_from_selected_tasks(
            task,
            wheel_task,
            resolved_plan_sha256=plan["resolved_plan_sha256"],
            source_repository=plan["source"]["repository"],
        )


def test_real_runtime_recipe_and_evidence_bind_the_product_variant_map() -> None:
    """Runtime inspection cannot attest a different product-specific map."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image, "cann900-a2-amd64")
    expected = {"vllm": "default", "vllm-ascend": "a2"}
    encoded = '{"vllm":"default","vllm-ascend":"a2"}'

    assert recipe["payload"]["runtime_patch_variants"] == expected
    assert (
        recipe["payload"]["build"]["build_args"]["UCM_RUNTIME_PATCH_VARIANTS"]
        == encoded
    )
    assert evidence["runtime"]["runtime_patch_variants"] == expected
    assert image.verify_real_runtime_evidence(recipe, evidence)["variant"] == "passed"

    evidence["runtime"]["runtime_patch_variants"]["vllm-ascend"] = "a3"
    with pytest.raises(ValueError, match="variant"):
        image.verify_real_runtime_evidence(recipe, evidence)


def test_runtime_real_docker_stage_persists_the_authorized_variant_map() -> None:
    """The built image must expose the canonical product-specific map."""
    dockerfile = (RELEASE_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    runtime_real_install = dockerfile.split(
        "FROM ${BASE_IMAGE} AS runtime-real-install", maxsplit=1
    )[1].split("FROM runtime-real-install AS runtime-real", maxsplit=1)[0]

    assert "ARG UCM_RUNTIME_PATCH_VARIANTS" in runtime_real_install
    assert (
        'ENV UCM_RUNTIME_PATCH_VARIANTS="${UCM_RUNTIME_PATCH_VARIANTS}"'
        in runtime_real_install
    )


@pytest.mark.parametrize(
    "observed",
    [
        None,
        '{"vllm":"default"}',
        '{"vllm":"default", "vllm-ascend":"a2"}',
        '{"foreign":"a2","vllm":"default","vllm-ascend":"a2"}',
    ],
)
def test_runtime_inspector_rejects_missing_tampered_or_noncanonical_variant_map(
    monkeypatch: pytest.MonkeyPatch, observed: str | None
) -> None:
    """Inspection reopens the exact canonical map persisted in the image."""
    inspector = _inspect_module()
    payload = {"runtime_patch_variants": {"vllm": "default", "vllm-ascend": "a2"}}
    if observed is None:
        monkeypatch.delenv("UCM_RUNTIME_PATCH_VARIANTS", raising=False)
    else:
        monkeypatch.setenv("UCM_RUNTIME_PATCH_VARIANTS", observed)

    with pytest.raises(ValueError, match="variant map"):
        inspector._runtime_patch_variants(payload)


def test_runtime_inspector_accepts_the_exact_canonical_variant_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = _inspect_module()
    expected = {"vllm": "default", "vllm-ascend": "a2"}
    monkeypatch.setenv(
        "UCM_RUNTIME_PATCH_VARIANTS",
        '{"vllm":"default","vllm-ascend":"a2"}',
    )

    assert (
        inspector._runtime_patch_variants({"runtime_patch_variants": expected})
        == expected
    )


@pytest.mark.parametrize("mutation", ["unknown", "malformed", "wrong-kind"])
def test_real_image_authority_rejects_caller_invented_task_id(
    mutation: str,
) -> None:
    """A caller-controlled task ID cannot select family/architecture authority."""
    *_, image = _modules()
    plan = _fixture_resolved_plan()
    task_id = {
        "unknown": "image-" + "f" * 64,
        "malformed": "../cuda130",
        "wrong-kind": plan["wheel_tasks"][0]["task_id"],
    }[mutation]

    with pytest.raises(ValueError, match="task"):
        image.real_image_authority_from_plan(
            plan,
            task_id=task_id,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )


def test_real_entry_reinspects_raw_wheel_and_rejects_fixture_relabeling(
    tmp_path: Path,
) -> None:
    """Changing summary labels cannot turn fixture bytes into a real wheel."""
    core, wheel, _, image = _modules()
    manifest = core._build_fixture_release_manifest()
    spec = next(
        item for item in manifest["wheel_specs"] if item["spec_id"] == "cuda130-amd64"
    )
    built = wheel.build_fixture_wheel(tmp_path / "fixture", "0" * 40, spec["spec_id"])
    relabeled = copy.deepcopy(built["inspection"])
    relabeled.update(
        {
            "source_kind": "builder-candidate",
            "status": "candidate-inspected",
            "trust_level": "unpublished-builder-candidate",
        }
    )

    with pytest.raises(ValueError, match="builder-candidate|fixture"):
        task = _real_image_task(image)
        image.inspect_real_wheel_candidate(
            Path(built["wheel_path"]),
            relabeled,
            task_authority=task,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("fixture-kind", "real"),
        ("repository", "repository"),
        ("index", "base"),
        ("manifest", "base"),
        ("config", "base"),
    ],
)
def test_real_base_authority_requires_the_exact_task_descriptor_chain(
    mutation: str, message: str
) -> None:
    """Basename matches and internally consistent wrong descriptor chains are invalid."""
    *_, image = _modules()
    task = _real_image_task(image)
    record = _base_chain_record()
    record["kind"] = "ucm-real-base-image-record"
    record["fixture_only"] = False
    record["repository"] = task["runtime"]["repository"]
    if mutation == "fixture-kind":
        record["kind"] = "fixture-base-image-record"
        record["fixture_only"] = True
    elif mutation == "repository":
        record["repository"] = "evil.example/vllm/vllm-openai"
    elif mutation == "index":
        record["index"]["digest"] = task["runtime"]["index_digest"]
    elif mutation == "manifest":
        record["manifest"]["digest"] = task["runtime"]["manifest_digest"]
    elif mutation == "config":
        record["config"]["digest"] = task["runtime"]["config_digest"]

    with pytest.raises(ValueError, match=message):
        image.validate_real_base_authority(record, task)


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        ("setup.py", "source|allowlist"),
        ("CMakeLists.txt", "source|allowlist"),
        ("compiler", "tool|allowlist"),
        ("second.whl", "wheel|allowlist"),
        ("nested/ucm.cc", "flat|directory|allowlist"),
        ("linked", "symlink"),
    ],
)
def test_real_context_recursive_allowlist_rejects_source_tools_and_extra_wheels(
    tmp_path: Path, artifact: str, message: str
) -> None:
    """A top-level-only file check must not hide nested source or symlink payloads."""
    *_, image = _modules()
    context = tmp_path / "context"
    context.mkdir()
    expected = {"Dockerfile", "image-recipe.json"}
    for name in expected:
        (context / name).write_text("{}\n", encoding="utf-8")
    target = context / artifact
    if artifact == "linked":
        target.symlink_to(context / "Dockerfile")
    elif "/" in artifact:
        target.parent.mkdir()
        target.write_text("source\n", encoding="utf-8")
    else:
        target.write_text("payload\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        image.audit_real_context(context, expected)


def _materialize_runtime_dependency_wheels(image, task, tmp_path: Path) -> list[Path]:
    paths = []
    for record in task["runtime_dependencies"]:
        path = tmp_path / record["filename"]
        path.write_bytes(f"reviewed {record['name']} wheel bytes".encode())
        record["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        paths.append(path)
    task["authority_sha256"] = image.sha256_value(
        {key: value for key, value in task.items() if key != "authority_sha256"}
    )
    return paths


def test_real_dependency_lock_rejects_missing_or_wrong_runtime_wheel_set(
    tmp_path: Path,
) -> None:
    """A network fallback or missing task dependency cannot satisfy the lock."""
    *_, image = _modules()
    task = _real_image_task(image, "cuda130-arm64")
    paths = _materialize_runtime_dependency_wheels(image, task, tmp_path)
    ucm = tmp_path / "ucm.whl"
    ucm.write_bytes(b"ucm")

    with pytest.raises(ValueError, match="wheel set|missing|ambiguous"):
        image.build_real_dependency_lock(task, ucm, paths[:-1])
    paths[-1].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA256"):
        image.build_real_dependency_lock(task, ucm, paths)


def test_real_dependency_lock_uses_exact_install_and_preinstall_purge(
    tmp_path: Path,
) -> None:
    """The locked install replaces rather than reuses packages present in the base."""
    *_, image = _modules()
    task = _real_image_task(image)
    ucm = tmp_path / "uc_manager.whl"
    ucm.write_bytes(b"reviewed UCM wheel bytes")
    runtime_paths = _materialize_runtime_dependency_wheels(image, task, tmp_path)

    lock = image.build_real_dependency_lock(task, ucm, runtime_paths)

    assert lock["preinstall_command"] == [
        "python",
        "-m",
        "pip",
        "uninstall",
        "--yes",
        "uc-manager",
        *sorted(record["name"] for record in task["runtime_dependencies"]),
    ]
    assert lock["pip_command"] == [
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
    ]


def _real_runtime_probe(image, spec_id: str = "cuda130-amd64"):
    task = _real_image_task(image, spec_id)
    builder = task["builder"]["root"]
    builder_coordinate = f"{builder['repository']}@{builder['manifest_digest']}"
    runtime_coordinate = (
        f"{task['runtime']['repository']}@{task['runtime']['manifest_digest']}"
    )
    machine = "EM_X86_64" if task["cpu_arch"] == "amd64" else "EM_AARCH64"
    wheel_sha256 = "sha256:" + "a" * 64
    runtime_dependencies = copy.deepcopy(task["runtime_dependencies"])
    native_members = {
        component: f"ucm/native/{component}.so" for component in task["required_native"]
    }
    dt_needed = {member: ["libc.so.6"] for member in native_members.values()}
    dependency_closure = {
        member: {
            "dt_needed": ["libc.so.6"],
            "resolved_dependencies": [
                {
                    "dependency": "libc.so.6",
                    "direct": True,
                    "kind": "external",
                    "path": "/lib/libc.so.6",
                    "sha256": "sha256:" + "b" * 64,
                }
            ],
            "unresolved_dependencies": [],
        }
        for member in native_members.values()
    }
    recipe = {
        "payload": {
            "candidate_kind": "real-candidate",
            "runtime_patch_variants": copy.deepcopy(task["runtime_patch_variants"]),
            "target_platform": task["platform"],
            "base": {"subject": runtime_coordinate},
            "wheel": {
                "filename": "uc_manager.whl",
                "sha256": wheel_sha256,
                "version": task["wheel_version"],
                "python_abi": task["python_abi"],
                "builder_evidence": {
                    "builder_coordinate": builder_coordinate,
                    "native_members": native_members,
                    "elf_machines": [machine],
                    "dt_needed": dt_needed,
                    "dependency_closure": copy.deepcopy(dependency_closure),
                },
            },
            "runtime_dependencies": runtime_dependencies,
            "dependency_lock": {
                "preinstall_command": [
                    "python",
                    "-m",
                    "pip",
                    "uninstall",
                    "--yes",
                    "uc-manager",
                    *(record["name"] for record in runtime_dependencies),
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
            },
            "build": {
                "build_args": {
                    "UCM_RUNTIME_PATCH_VARIANTS": json.dumps(
                        task["runtime_patch_variants"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            },
        }
    }
    recipe["payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                recipe["payload"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    evidence = {
        "install": {
            "preinstall_command": [
                "/usr/local/bin/python",
                "-m",
                "pip",
                "uninstall",
                "--yes",
                "uc-manager",
                *(record["name"] for record in runtime_dependencies),
            ],
            "pip_command": [
                "/usr/local/bin/python",
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
            "pip_check": "passed",
            "installed_packages": {
                "uc-manager": task["wheel_version"],
                **{
                    record["name"]: record["version"] for record in runtime_dependencies
                },
            },
            "imports": {
                "ucm": "passed",
                **{record["import_name"]: "passed" for record in runtime_dependencies},
            },
            "direct_urls": {
                "uc-manager": {
                    "url": "file:///wheelhouse/uc_manager.whl",
                    "archive_info": {
                        "hash": "sha256=" + wheel_sha256.removeprefix("sha256:")
                    },
                },
                **{
                    record["name"]: {
                        "url": f"file:///wheelhouse/{record['filename']}",
                        "archive_info": {
                            "hash": "sha256=" + record["sha256"].removeprefix("sha256:")
                        },
                    }
                    for record in runtime_dependencies
                },
            },
            "status": "passed",
        },
        "runtime": {
            "package_version": task["wheel_version"],
            "runtime_patch_variants": copy.deepcopy(task["runtime_patch_variants"]),
            "native_members": native_members,
            "elf_machines": [machine],
            "dt_needed": dt_needed,
            "dependency_closure": copy.deepcopy(dependency_closure),
            "abi": {
                "expected_python_abi": task["python_abi"],
                "observed_python_abi": task["python_abi"],
                "status": "passed",
            },
            "accelerator_runtime": {"status": "external-required"},
            "device": {"status": "external-required"},
            "hardware_passed": False,
            "status": "external-required",
        },
    }
    return recipe, evidence


def test_real_runtime_dependencies_come_from_selected_task_recipe() -> None:
    """Future versions and extra dependencies must not reopen current authority."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image)
    dependencies = recipe["payload"]["runtime_dependencies"]
    wrapt = next(record for record in dependencies if record["name"] == "wrapt")
    wrapt["version"] = "9.9.0"
    wrapt["requirement"] = "wrapt==9.9.0"
    dependencies.append(
        {
            "name": "alpha-runtime",
            "version": "2.0",
            "requirement": "alpha-runtime==2.0",
            "import_name": "alpha_runtime",
            "filename": "alpha_runtime-2.0-py3-none-any.whl",
            "sha256": "sha256:" + "d" * 64,
        }
    )
    dependencies.sort(key=lambda item: item["name"])
    dependency_names = [record["name"] for record in dependencies]
    recipe["payload"]["dependency_lock"]["preinstall_command"] = [
        "python",
        "-m",
        "pip",
        "uninstall",
        "--yes",
        "uc-manager",
        *dependency_names,
    ]
    recipe["payload_sha256"] = image.sha256_value(recipe["payload"])
    evidence["install"]["preinstall_command"] = [
        "/usr/local/bin/python",
        "-m",
        "pip",
        "uninstall",
        "--yes",
        "uc-manager",
        *dependency_names,
    ]
    evidence["install"]["runtime_dependencies"] = copy.deepcopy(dependencies)
    evidence["install"]["installed_packages"].update(
        {"alpha-runtime": "2.0", "wrapt": "9.9.0"}
    )
    evidence["install"]["imports"]["alpha_runtime"] = "passed"
    evidence["install"]["direct_urls"]["alpha-runtime"] = {
        "url": "file:///wheelhouse/alpha_runtime-2.0-py3-none-any.whl",
        "archive_info": {"hash": "sha256=" + "d" * 64},
    }

    assert image.verify_real_runtime_evidence(recipe, evidence)["install"] == "passed"


@pytest.mark.parametrize("spec_id", ["cuda130-amd64", "cuda130-arm64"])
def test_cross_root_runtime_closure_accepts_root_local_external_bytes(
    spec_id: str,
) -> None:
    """Immutable builder/runtime roots may supply different bytes for one SONAME."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image, spec_id)
    runtime_resolution = next(iter(evidence["runtime"]["dependency_closure"].values()))[
        "resolved_dependencies"
    ][0]
    runtime_resolution["path"] = "/usr/lib/runtime/libc.so.6"
    runtime_resolution["sha256"] = "sha256:" + "c" * 64

    assert (
        image.verify_real_runtime_evidence(recipe, evidence)["dependency_closure"]
        == "passed"
    )


def test_same_root_runtime_closure_rejects_external_byte_drift() -> None:
    """One immutable CANN root must retain literal dependency byte identity."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image, "cann900-a2-amd64")
    runtime_resolution = next(iter(evidence["runtime"]["dependency_closure"].values()))[
        "resolved_dependencies"
    ][0]
    runtime_resolution["path"] = "/usr/lib/runtime/libc.so.6"
    runtime_resolution["sha256"] = "sha256:" + "c" * 64

    with pytest.raises(ValueError, match="dependency closure"):
        image.verify_real_runtime_evidence(recipe, evidence)


def test_real_runtime_evidence_accepts_selected_non_cp312_python_command() -> None:
    """Offline install evidence must not encode Python 3.12 as the only ABI."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image)
    recipe["payload"]["wheel"]["python_abi"] = "cp311"
    recipe["payload_sha256"] = image.sha256_value(recipe["payload"])
    evidence["install"]["preinstall_command"][0] = "/usr/local/bin/python3.11"
    evidence["install"]["pip_command"][0] = "/usr/local/bin/python3.11"
    evidence["runtime"]["abi"] = {
        "expected_python_abi": "cp311",
        "observed_python_abi": "cp311",
        "status": "passed",
    }

    assert image.verify_real_runtime_evidence(recipe, evidence)["abi"] == "passed"


@pytest.mark.parametrize(
    "mutation",
    [
        "member",
        "dt-needed",
        "unresolved",
        "dependency",
        "direct",
        "kind",
        "wheel-member",
        "virtual",
        "external-required",
    ],
)
def test_cross_root_runtime_closure_rejects_dependency_identity_drift(
    mutation: str,
) -> None:
    """Cross-root normalization must preserve every non-location closure fact."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image)
    expected_closure = recipe["payload"]["wheel"]["builder_evidence"][
        "dependency_closure"
    ]
    runtime_closure = evidence["runtime"]["dependency_closure"]
    member = next(iter(expected_closure))
    expected_record = expected_closure[member]
    runtime_record = runtime_closure[member]
    internal_member = next(
        iter(recipe["payload"]["wheel"]["builder_evidence"]["native_members"].values())
    )
    shared_resolutions = [
        {
            "dependency": "libucm-internal.so",
            "direct": False,
            "kind": "wheel-member",
            "member": internal_member,
            "sha256": "sha256:" + "d" * 64,
        },
        {
            "dependency": "linux-vdso.so.1",
            "direct": False,
            "kind": "virtual",
        },
        {
            "dependency": "libascend_hal.so",
            "direct": False,
            "kind": "external-required",
            "provider": "host-ascend-driver",
            "expected_mount_root": "/usr/local/Ascend/driver/lib64",
            "relation": "transitive",
            "required_at": "device-runtime",
        },
    ]
    expected_record["resolved_dependencies"].extend(copy.deepcopy(shared_resolutions))
    runtime_record["resolved_dependencies"].extend(copy.deepcopy(shared_resolutions))
    recipe["payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                recipe["payload"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )

    if mutation == "member":
        runtime_closure["ucm/native/unexpected.so"] = runtime_closure.pop(member)
    elif mutation == "dt-needed":
        runtime_record["dt_needed"] = ["libm.so.6"]
    elif mutation == "unresolved":
        runtime_record["unresolved_dependencies"] = ["libmissing.so"]
    elif mutation == "dependency":
        runtime_record["resolved_dependencies"][0]["dependency"] = "libm.so.6"
    elif mutation == "direct":
        runtime_record["resolved_dependencies"][0]["direct"] = False
    elif mutation == "kind":
        runtime_record["resolved_dependencies"][0]["kind"] = "virtual"
    elif mutation == "wheel-member":
        runtime_record["resolved_dependencies"][1]["sha256"] = "sha256:" + "e" * 64
    elif mutation == "virtual":
        runtime_record["resolved_dependencies"][2]["dependency"] = "linux-gate.so.1"
    elif mutation == "external-required":
        runtime_record["resolved_dependencies"][3]["provider"] = "wrong-provider"

    with pytest.raises(ValueError, match="dependency closure"):
        image.verify_real_runtime_evidence(recipe, evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        "record-extra",
        "record-missing",
        "resolution-extra",
        "resolution-missing",
        "relative-path",
        "bad-digest",
        "duplicate-resolution",
        "direct-coverage",
        "direct-type",
    ],
)
def test_cross_root_runtime_closure_rejects_malformed_external_evidence(
    mutation: str,
) -> None:
    """Normalization cannot erase malformed or ambiguous external evidence."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image)
    expected_record = next(
        iter(
            recipe["payload"]["wheel"]["builder_evidence"][
                "dependency_closure"
            ].values()
        )
    )
    runtime_record = next(iter(evidence["runtime"]["dependency_closure"].values()))
    for record in (expected_record, runtime_record):
        resolution = record["resolved_dependencies"][0]
        if mutation == "record-extra":
            record["unexpected"] = "value"
        elif mutation == "record-missing":
            record.pop("unresolved_dependencies")
        elif mutation == "resolution-extra":
            resolution["unexpected"] = "value"
        elif mutation == "resolution-missing":
            resolution.pop("path")
        elif mutation == "relative-path":
            resolution["path"] = "usr/lib/libc.so.6"
        elif mutation == "bad-digest":
            resolution["sha256"] = "sha256:bad"
        elif mutation == "duplicate-resolution":
            record["resolved_dependencies"].append(copy.deepcopy(resolution))
        elif mutation == "direct-coverage":
            resolution["direct"] = False
        elif mutation == "direct-type":
            record["resolved_dependencies"].append(
                {
                    "dependency": "libtransitive.so",
                    "direct": 0,
                    "kind": "external",
                    "path": "/lib/libtransitive.so",
                    "sha256": "sha256:" + "f" * 64,
                }
            )
    recipe["payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                recipe["payload"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )

    with pytest.raises(ValueError, match="dependency closure"):
        image.verify_real_runtime_evidence(recipe, evidence)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("install", "preinstall_command"), [], "preinstall"),
        (("install", "pip_check"), "failed", "pip"),
        (
            ("install", "direct_urls", "uc-manager", "archive_info", "hash"),
            "sha256=bad",
            "direct_url",
        ),
        (
            ("install", "direct_urls", "wrapt", "archive_info", "hash"),
            "sha256=bad",
            "direct_url",
        ),
        (("runtime", "native_members"), {}, "native"),
        (("runtime", "elf_machines"), ["EM_AARCH64"], "ELF"),
        (("runtime", "dt_needed"), {}, "DT_NEEDED"),
        (("runtime", "dependency_closure"), {}, "dependency closure"),
        (("runtime", "abi", "status"), "failed", "ABI"),
        (("runtime", "device", "status"), "passed", "external-required"),
    ],
)
def test_real_runtime_verification_fails_closed_on_every_required_gate(
    path: tuple[str, ...], value: object, message: str
) -> None:
    """A real member cannot survive install, ABI, native, ELF, or runtime gate drift."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image)
    cursor = evidence
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ValueError, match=message):
        image.verify_real_runtime_evidence(recipe, evidence)


def test_real_content_identity_rejects_mutable_or_missing_oci_authority() -> None:
    """Run/signature bytes are excluded, while OCI labels and deterministic history bind."""
    *_, image = _modules()
    task = _real_image_task(image)
    epoch = 1_700_000_000
    base_history = [{"created": "2022-01-01T00:00:00Z", "created_by": "base-layer"}]
    recipe = {
        "payload": {
            "candidate_kind": "real-candidate",
            "source_date_epoch": epoch,
            "base": {
                "config": {
                    "raw": json.dumps(
                        {
                            "config": {"Labels": {"base.label": "preserved"}},
                            "history": base_history,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                }
            },
            "source": {
                "repository": "SuperMarioYL/unified-cache-management",
                "repository_url": (
                    "https://github.com/SuperMarioYL/unified-cache-management"
                ),
                "commit": "1" * 40,
                "tree": "2" * 40,
                "context_sha256": "sha256:" + "3" * 64,
            },
            "task_sha256": task["task_sha256"],
            "build_key_sha256": "sha256:" + "4" * 64,
            "wheel": {"sha256": "sha256:" + "5" * 64},
        }
    }
    recipe["payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                recipe["payload"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    created = "2023-11-14T22:13:20Z"
    closure = {
        "manifest_digest": "sha256:" + "6" * 64,
        "config_digest": "sha256:" + "7" * 64,
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "8" * 64,
                "size": 123,
                "annotations": {
                    "buildkit/rewritten-timestamp": str(epoch),
                },
            }
        ],
        "diff_ids": ["sha256:" + "9" * 64],
        "annotations": {
            "io.ucm.release.recipe-sha256": recipe["payload_sha256"],
            "io.ucm.release.task-sha256": task["task_sha256"],
        },
        "labels": {
            "base.label": "preserved",
            "org.opencontainers.image.source": (
                "https://github.com/SuperMarioYL/unified-cache-management"
            ),
            "org.opencontainers.image.revision": "1" * 40,
            "io.ucm.release.source-tree": "2" * 40,
            "io.ucm.release.source-context-sha256": "sha256:" + "3" * 64,
            "io.ucm.release.task-sha256": task["task_sha256"],
            "io.ucm.release.build-key-sha256": "sha256:" + "4" * 64,
            "io.ucm.release.wheel-sha256": "sha256:" + "5" * 64,
            "io.ucm.release.recipe-sha256": recipe["payload_sha256"],
        },
        "created": created,
        "history": [
            *base_history,
            {"created": created, "created_by": "ucm-install-only-v1"},
        ],
    }
    first = image.real_content_identity(recipe, closure)
    assert first["labels"]["org.opencontainers.image.source"] == (
        "https://github.com/SuperMarioYL/unified-cache-management"
    )
    assert first["layers"][0]["annotations"] == {
        "buildkit/rewritten-timestamp": "1700000000"
    }
    changed_envelope = copy.deepcopy(closure)
    changed_envelope["run_id"] = "different-run"
    changed_envelope["signature"] = "different-signature"
    assert image.real_content_identity(recipe, changed_envelope) == first

    missing_label = copy.deepcopy(closure)
    del missing_label["labels"]["io.ucm.release.task-sha256"]
    with pytest.raises(ValueError, match="label"):
        image.real_content_identity(recipe, missing_label)
    mutable_history = copy.deepcopy(closure)
    mutable_history["history"][0]["created"] = "2026-08-09T12:34:56Z"
    with pytest.raises(ValueError, match="created|history"):
        image.real_content_identity(recipe, mutable_history)
