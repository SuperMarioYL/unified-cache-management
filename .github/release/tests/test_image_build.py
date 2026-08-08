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
        "repository": "docker.io/vllm/vllm-ascend",
        "upstream_tag": "v0.10.2-a3-openeuler",
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
    return {
        "schema_version": 1,
        "kind": "fixture-base-image-record",
        "fixture_only": True,
        "repository": "docker.io/library/python",
        "index_digest": DIGESTS["base_index"],
        "platform": {
            "os": "linux",
            "architecture": "arm64",
            "manifest_digest": DIGESTS["base_manifest"],
            "config_digest": DIGESTS["base_config"],
        },
    }


def _actual_inputs(tmp_path: Path) -> dict[str, object]:
    core, wheel_module, registry, image = _modules()
    manifest = core.build_release_manifest()
    spec = next(
        item
        for item in manifest["wheel_specs"]
        if item["accelerator"] == "ascend"
        and item["accelerator_runtime"] == "cann-9.0.0"
        and item["npu_arch_or_na"] == "a3"
        and item["os"] == "openEuler-24.03"
        and item["cpu_arch"] == "arm64"
        and item["python_abi"] == "cp312"
    )
    wheel_path = _write_fixture_wheel(tmp_path, spec, manifest["ucm_version"])
    wheel_sha256 = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    wheel_record = wheel_module.inspect_wheel(
        wheel_path, spec["spec_id"], wheel_sha256, "fixture"
    )
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
) -> None:
    """Write a minimal standard OCI layout containing the observable image files."""
    layer_buffer = io.BytesIO()
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
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        for name, content in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            layer.addfile(info, io.BytesIO(content))
    compressed_buffer = io.BytesIO()
    with gzip.GzipFile(
        fileobj=compressed_buffer, mode="wb", filename="", mtime=0
    ) as stream:
        stream.write(layer_buffer.getvalue())
    layer_bytes = compressed_buffer.getvalue()
    config = json.dumps(
        {"architecture": "arm64", "os": "linux"},
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
    layer_descriptor = descriptor(
        layer_bytes, "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_descriptor,
            "layers": [layer_descriptor],
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
        layer_descriptor["digest"]: layer_bytes,
        manifest_descriptor["digest"]: manifest,
    }
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
    assert payload["base"] == {
        **_base_record(),
        "subject": f"docker.io/library/python@{DIGESTS['base_manifest']}",
    }
    assert payload["wheel"]["spec_id"] == values["source_case"]["spec_id"]
    assert payload["wheel"]["declaration_sha256"].startswith("sha256:")
    assert payload["implementation"] == image.implementation_digests()
    changed = copy.deepcopy(values)
    changed["base_record"]["platform"]["config_digest"] = "sha256:" + "a" * 64
    changed_recipe = image.prepare_context(
        **changed, output_dir=tmp_path / "changed-context"
    )
    assert changed_recipe["payload_sha256"] != recipe["payload_sha256"]


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
        expect=2,
    )
    assert "metadata" in failed.stderr.lower()


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
