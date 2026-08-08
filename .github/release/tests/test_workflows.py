"""RED workflow and staging-safety contract for the slim release lane."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import importlib
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_RELEASE_WORKFLOWS = {
    "_build-image.yml",
    "_build-wheel.yml",
    "release-ucm.yml",
    "release-vllm-images.yml",
}
ALLOWED_NON_RELEASE_WORKFLOWS = {
    "lint-and-test.yml",
    "pull-request.yml",
    "push-check.yml",
}
WORKFLOW_SUFFIXES = {".yml", ".yaml"}
SAFE_FORK_ACTIONS = {
    "actions/cache",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "azure/setup-helm",
    "docker/setup-buildx-action",
    "docker/setup-qemu-action",
    "sigstore/cosign-installer",
}
CHANGED_WORKFLOWS = EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS
FULL_ACTION_SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")
FORBIDDEN_STAGED_PATHS = {
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.cc",
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.h",
    "ucm/store/compress/cc/compressor_action.cc",
}
FIXTURE_PROFILE = (
    "cuda-cu129-ubuntu2204-amd64-cp312-release-default-sm75-sm80-sm86-sm89-sm90"
)
BUILDKIT_IMAGE = (
    "moby/buildkit:v0.18.2@"
    "sha256:86c0ad9d1137c186e9d455912167df20e530bdf7f7c19de802e892bb8ca16552"
)
BUILDX_VERSION = "v0.19.2"
BUILDX_LINUX_SHA256 = {
    "amd64": "a5ff61c0b6d2c8ee20964a9d6dac7a7a6383c4a4a0ee8d354e983917578306ea",
    "arm64": "bd54f0e28c29789da1679bad2dd94c1923786ccd2cd80dd3a0a1d560a6baf10c",
}
DOCKERFILE_FRONTEND = (
    "# syntax=docker/dockerfile:1.12.1@"
    "sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25"
)
HELM_VERSION = "v3.15.3"
HELM_LINUX_SHA256 = {
    "amd64": "ad871aecb0c9fd96aa6702f6b79e87556c8998c2e714a4959bf71ee31282ac9c",
    "arm64": "bd57697305ba46fef3299b50168a34faa777dd2cf5b43b50df92cca7ed118cce",
}
BASE_AUTHORITY_FIXTURE = (
    REPO_ROOT
    / ".github"
    / "release"
    / "tests"
    / "fixtures"
    / "python-base-authority.json"
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8"
    )


def _release_modules() -> tuple[object, object, object]:
    release_root = REPO_ROOT / ".github" / "release"
    sys.path.insert(0, str(release_root))
    return (
        importlib.import_module("ucm_release.core"),
        importlib.import_module("ucm_release.wheel"),
        importlib.import_module("ucm_release.verify"),
    )


def _write_canonical(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _rewrite_fixture_marker(
    wheel_path: Path,
    content: bytes | None,
    *,
    duplicate: bool = False,
    extra_member: tuple[str, bytes] | None = None,
) -> None:
    """Rewrite a fixture marker and its RECORD for adversarial inspection tests."""
    marker = "ucm/_fixture_build.py"
    with zipfile.ZipFile(wheel_path) as archive:
        members = {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
            if not item.is_dir()
        }
    record_name = next(name for name in members if name.endswith(".dist-info/RECORD"))
    members.pop(record_name)
    if content is None:
        members.pop(marker)
    else:
        members[marker] = content
    if extra_member is not None:
        members[extra_member[0]] = extra_member[1]
    rows: list[list[str]] = []
    for name, raw in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode()
        rows.append([name, "sha256=" + digest.rstrip("="), str(len(raw))])
    rows.append([record_name, "", ""])
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    members[record_name] = record.getvalue().encode()
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, raw in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, raw)
        if duplicate and content is not None:
            archive.writestr(marker, content)


def _authoritative_base_record(image_module: object) -> dict[str, object]:
    """Reopen the checked-in Registry bytes for the single fixture base policy."""
    encoded = json.loads(BASE_AUTHORITY_FIXTURE.read_text(encoding="utf-8"))
    raw = {label: base64.b64decode(value) for label, value in encoded.items()}
    parsed = {label: json.loads(value) for label, value in raw.items()}
    authority = image_module.fixture_base_authority()
    record = {
        "schema_version": 1,
        "kind": "fixture-base-image-record",
        "fixture_only": True,
        "repository": authority["repository"],
        "index": {
            "media_type": parsed["index"]["mediaType"],
            "digest": authority["index_digest"],
            "size": len(raw["index"]),
            "raw": raw["index"].decode("utf-8"),
        },
        "manifest": {
            "media_type": parsed["manifest"]["mediaType"],
            "digest": authority["manifest_digest"],
            "size": len(raw["manifest"]),
            "raw": raw["manifest"].decode("utf-8"),
        },
        "config": {
            "media_type": parsed["manifest"]["config"]["mediaType"],
            "digest": authority["config_digest"],
            "size": len(raw["config"]),
            "raw": raw["config"].decode("utf-8"),
        },
    }
    return image_module._validate_base(record, authority["target_platform"])


def _valid_image_result(
    core: object,
    image_module: object,
    prepared: dict[str, object],
    wheel_record: dict[str, object],
    *,
    authoritative_base: bool = True,
) -> dict[str, object]:
    """Create a schema-valid result whose fields are derived from the real task."""

    def blob(value: dict[str, object], media_type: str) -> dict[str, object]:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        raw_bytes = raw.encode()
        return {
            "media_type": media_type,
            "digest": "sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
            "size": len(raw_bytes),
            "raw": raw,
        }

    if authoritative_base:
        base = _authoritative_base_record(image_module)
    else:
        config = blob(
            {"architecture": "amd64", "os": "linux"},
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
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
            },
            "application/vnd.oci.image.index.v1+json",
        )
        base = image_module._validate_base(  # noqa: SLF001 - negative fixture
            {
                "schema_version": 1,
                "kind": "fixture-base-image-record",
                "fixture_only": True,
                "repository": "docker.io/library/python",
                "index": index,
                "manifest": manifest,
                "config": config,
            },
            "linux/amd64",
        )
    candidate = prepared["candidate"]
    build_inputs = candidate["build_inputs"]
    wheel_input = build_inputs["wheel"]
    manifest_record = prepared["source_case"]["release_manifest"]
    upstream = build_inputs["upstream"]
    upstream_platform = next(
        item for item in upstream["platforms"] if item["architecture"] == "amd64"
    )
    result_payload = {
        "schema_version": 1,
        "kind": "ucm-image-result",
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": False,
        "recipe_sha256": "sha256:" + "a" * 64,
        "build_key_sha256": candidate["build_key_sha256"],
        "task_key": core.sha256_value(prepared["image_input"]["task"]),
        "ucm_version": candidate["ucm_version"],
        "source": {
            "release_manifest_sha256": build_inputs["release_manifest_sha256"],
            "config_sha256": manifest_record["config_sha256"],
            "compatibility_sha256": manifest_record["compatibility_sha256"],
            "compatibility_rule_id": build_inputs["compatibility_rule_id"],
            "compatibility_rule_sha256": build_inputs["compatibility_rule_sha256"],
            "upstream_repository": upstream["repository"],
            "upstream_index_digest": upstream["index_digest"],
            "upstream_platform_manifest_digest": upstream_platform["manifest_digest"],
            "upstream_platform_config_digest": upstream_platform["config_digest"],
        },
        "base": base,
        "target_platform": "linux/amd64",
        "wheel": {
            "filename": wheel_record["filename"],
            "sha256": wheel_record["sha256"],
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
        "implementation": image_module.implementation_digests(),
        "oci": {
            "output": "local-oci",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + "9" * 64,
            "platform": "linux/amd64",
            "published": False,
        },
        "gates": {
            "base_verified": "passed",
            "wheel_verified": "passed",
            "install": "passed",
            "pip_check": "passed",
            "direct_url": "passed",
            "ucm_import": "passed",
            "wrapt_import": "passed",
            "abi": "passed",
        },
        "runtime_validation": "external-required",
        "device_validation": "external-required",
        "status": "fixture-verified-unpublished",
    }
    return {**result_payload, "result_sha256": core.sha256_value(result_payload)}


def _release_closure(
    tmp_path: Path, *, attempt: int = 1, authoritative_base: bool = True
) -> dict[str, object]:
    core, wheel_module, verify_module = _release_modules()
    image_module = importlib.import_module("ucm_release.image")
    chart_module = importlib.import_module("ucm_release.chart")
    source_sha = "b" * 40
    wheel_dir = tmp_path / "wheel"
    fixture = wheel_module.build_fixture_wheel(wheel_dir, source_sha, FIXTURE_PROFILE)
    prepared = verify_module.prepare_candidate_loop(
        fixture["build_record"],
        fixture["inspection"],
        source_sha=source_sha,
        run={"id": "17", "attempt": attempt},
    )
    image_result = _valid_image_result(
        core,
        image_module,
        prepared,
        fixture["inspection"],
        authoritative_base=authoritative_base,
    )
    base_record = {
        key: copy.deepcopy(image_result["base"][key])
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
    image_context = tmp_path / "image-context"
    recipe = image_module.prepare_context(
        **prepared["image_input"],
        base_record=base_record,
        wheel_path=Path(fixture["wheel_path"]),
        output_dir=image_context,
    )
    layer_descriptor = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
        "digest": "sha256:" + "4" * 64,
        "size": 17,
    }
    diff_id = "sha256:" + "5" * 64

    def raw_json(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    config_raw = raw_json(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        }
    )
    config_descriptor = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": "sha256:" + hashlib.sha256(config_raw).hexdigest(),
        "size": len(config_raw),
    }
    manifest_raw = raw_json(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_descriptor,
            "layers": [layer_descriptor],
        }
    )
    manifest_descriptor = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:" + hashlib.sha256(manifest_raw).hexdigest(),
        "size": len(manifest_raw),
        "platform": {"architecture": "amd64", "os": "linux"},
    }
    index_raw = raw_json(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [manifest_descriptor],
        }
    )
    image_result["recipe_sha256"] = recipe["payload_sha256"]
    image_result["oci"]["digest"] = manifest_descriptor["digest"]
    image_result["result_sha256"] = core.sha256_value(
        {key: value for key, value in image_result.items() if key != "result_sha256"}
    )
    completed = verify_module.complete_candidate_loop(
        prepared,
        image_result,
        source_sha=source_sha,
        run={"id": "17", "attempt": attempt},
    )
    chart_dir = tmp_path / "chart"
    chart_result = chart_module.package_chart(chart_dir)
    paths = {
        "build_record": wheel_dir / "fixture-build.json",
        "wheel_inspection": wheel_dir / "wheel-inspection.json",
        "wheel": Path(fixture["wheel_path"]),
        "chart_result": tmp_path / "chart-result.json",
        "chart_package": chart_dir / chart_result["filename"],
        "image_result": tmp_path / "image-result.json",
        "oci_evidence": tmp_path / "oci-evidence",
        "image_recipe": tmp_path / "image-recipe.json",
        "image_metadata": tmp_path / "image-metadata.json",
        "image_prepare": tmp_path / "image-prepare-result.json",
        "buildkit_metadata": tmp_path / "buildkit-metadata.json",
        "image_archive_sha256": tmp_path / "image-archive.sha256",
        "completed_loop": tmp_path / "completed-loop.json",
        "second_reconcile": tmp_path / "second-reconcile.json",
        "image_loop": tmp_path / "vllm-loop-evidence.json",
    }
    _write_canonical(paths["chart_result"], chart_result)
    _write_canonical(paths["image_result"], image_result)
    paths["oci_evidence"].mkdir()
    (paths["oci_evidence"] / "oci-layout.json").write_bytes(
        raw_json({"imageLayoutVersion": "1.0.0"})
    )
    (paths["oci_evidence"] / "index.json").write_bytes(index_raw)
    (paths["oci_evidence"] / "manifest.json").write_bytes(manifest_raw)
    (paths["oci_evidence"] / "config.json").write_bytes(config_raw)
    archive_sha256 = "sha256:" + "6" * 64
    _write_canonical(
        paths["oci_evidence"] / "closure.json",
        {
            "schema_version": 1,
            "kind": "ucm-compact-oci-evidence",
            "target_platform": "linux/amd64",
            "manifest_descriptor": manifest_descriptor,
            "config_descriptor": config_descriptor,
            "layers": [layer_descriptor],
            "diff_ids": [diff_id],
            "recipe_payload_sha256": recipe["payload_sha256"],
            "metadata_sha256": recipe["payload"]["metadata_sha256"],
            "wheel_sha256": fixture["wheel_sha256"],
            "archive_sha256": archive_sha256,
            "archive_size": 123,
        },
    )
    paths["image_recipe"].write_bytes(
        (image_context / "image-recipe.json").read_bytes()
    )
    paths["image_metadata"].write_bytes(
        (image_context / "image-metadata.json").read_bytes()
    )
    paths["image_prepare"].write_bytes(
        (image_context / "image-recipe.json").read_bytes()
    )
    _write_canonical(
        paths["buildkit_metadata"],
        {
            "buildx.build.ref": f"builder/attempt-{attempt}",
            "containerimage.digest": manifest_descriptor["digest"],
            "containerimage.config.digest": config_descriptor["digest"],
            "containerimage.descriptor": manifest_descriptor,
        },
    )
    paths["image_archive_sha256"].write_text(
        archive_sha256.removeprefix("sha256:") + "  out/image.oci.tar\n",
        encoding="utf-8",
    )
    _write_canonical(paths["completed_loop"], completed)
    _write_canonical(paths["second_reconcile"], completed["second_reconcile"])
    _write_canonical(paths["image_loop"], completed["evidence"])
    return {
        "core": core,
        "verify": verify_module,
        "source_sha": source_sha,
        "fixture": fixture,
        "prepared": prepared,
        "image_result_value": image_result,
        "completed_value": completed,
        "paths": paths,
    }


def _aggregate(closure: dict[str, object], *, attempt: int = 1) -> dict[str, object]:
    paths = closure["paths"]
    return closure["verify"].aggregate_release_evidence(
        build_record_path=paths["build_record"],
        wheel_record_path=paths["wheel_inspection"],
        wheel_path=paths["wheel"],
        chart_result_path=paths["chart_result"],
        chart_package_path=paths["chart_package"],
        image_result_path=paths["image_result"],
        oci_evidence_dir=paths["oci_evidence"],
        image_recipe_path=paths["image_recipe"],
        image_metadata_path=paths["image_metadata"],
        image_prepare_path=paths["image_prepare"],
        buildkit_metadata_path=paths["buildkit_metadata"],
        image_archive_sha256_path=paths["image_archive_sha256"],
        completed_loop_path=paths["completed_loop"],
        second_reconcile_path=paths["second_reconcile"],
        image_loop_path=paths["image_loop"],
        repository="SuperMarioYL/unified-cache-management",
        ref="refs/heads/feature/cicd",
        source_sha=closure["source_sha"],
        run={"id": "17", "attempt": attempt},
    )


def _toolchain_pin_violations(
    workflows: dict[str, dict[str, object]], dockerfile: str
) -> list[str]:
    violations: list[str] = []
    for filename, document in workflows.items():
        for job_name, job in _jobs(document).items():
            for step in _steps(job):
                uses = str(step.get("uses", ""))
                if filename == "_build-image.yml" and uses.startswith(
                    "docker/setup-buildx-action@"
                ):
                    violations.append(
                        f"{filename}:{job_name}: Buildx setup action downloads an unchecked binary"
                    )
                if uses.startswith("azure/setup-helm@"):
                    violations.append(
                        f"{filename}:{job_name}: setup-helm has no checksum"
                    )
    image_steps = _steps(_jobs(workflows["_build-image.yml"])["build"])
    buildx_steps = [
        step
        for step in image_steps
        if step.get("name") == "Install checksum-pinned Buildx"
    ]
    if len(buildx_steps) != 1:
        violations.append(
            "_build-image.yml: checksum-pinned Buildx installer is missing"
        )
    else:
        buildx_step = buildx_steps[0]
        environment = buildx_step.get("env")
        command = str(buildx_step.get("run", ""))
        step_text = json.dumps(environment, sort_keys=True) + command
        for literal in (
            BUILDX_VERSION,
            *BUILDX_LINUX_SHA256.values(),
            BUILDKIT_IMAGE,
        ):
            if literal in step_text:
                violations.append(
                    "_build-image.yml: image toolchain authority is duplicated in YAML"
                )
        required_buildx_fragments = (
            "image toolchain-authority",
            '["buildx_version"]',
            '["buildx_linux_sha256"]',
            '["buildkit_image"]',
            "https://github.com/docker/buildx/releases/download/${buildx_version}/buildx-${buildx_version}.linux-${buildx_arch}",
            "sha256sum --check",
            '"${HOME}/.docker/cli-plugins/docker-buildx"',
            'test "$(docker buildx version | awk \'{print $2}\')" = "${buildx_version}"',
            "docker buildx create --name ucm-release-builder",
            "--driver docker-container",
            "--use",
            "docker buildx inspect ucm-release-builder --bootstrap",
        )
        for fragment in required_buildx_fragments:
            if fragment not in command:
                violations.append(
                    f"_build-image.yml: Buildx installer missing {fragment}"
                )
        if '--driver-opt "image=${buildkit_image}"' not in command:
            violations.append("_build-image.yml: mutable BuildKit")
    if dockerfile.splitlines()[0] != DOCKERFILE_FRONTEND:
        violations.append("release Dockerfile frontend is mutable")
    for filename in ("release-ucm.yml", "lint-and-test.yml"):
        install_steps = [
            step
            for job in _jobs(workflows[filename]).values()
            for step in _steps(job)
            if step.get("name") == "Install Helm"
        ]
        if not install_steps:
            violations.append(f"{filename}: Helm installer is missing")
        for step in install_steps:
            environment = step.get("env")
            command = str(step.get("run", ""))
            if (
                not isinstance(environment, dict)
                or environment.get("HELM_VERSION") != HELM_VERSION
            ):
                violations.append(f"{filename}: Helm version is not fixed")
            if (
                "https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz"
                not in command
            ):
                violations.append(f"{filename}: Helm archive URL is not fixed")
            for architecture, digest in HELM_LINUX_SHA256.items():
                variable = f"HELM_LINUX_{architecture.upper()}_SHA256"
                if (
                    not isinstance(environment, dict)
                    or environment.get(variable) != digest
                ):
                    violations.append(
                        f"{filename}: Helm {architecture} checksum missing"
                    )
            if "sha256sum --check" not in command or "version --short" not in command:
                violations.append(f"{filename}: Helm archive/version is not verified")
    return violations


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _strings(nested)]
    return [str(value)]


def _workflow_set_violations(workflow_dir: Path) -> list[str]:
    actual = {path.name for path in _workflow_paths(workflow_dir)}
    expected = EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS
    if actual == expected:
        return []
    return [
        f"workflow file set must be exactly {sorted(expected)}, found {sorted(actual)}"
    ]


def _workflow_paths(workflow_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in workflow_dir.iterdir()
        if path.is_file() and path.suffix in WORKFLOW_SUFFIXES
    )


def _release_workflow_documents(workflow_dir: Path) -> dict[str, object]:
    """Audit expected release files and any unallowlisted workflow extension."""
    documents: dict[str, object] = {}
    for path in _workflow_paths(workflow_dir):
        if (
            path.name in EXPECTED_RELEASE_WORKFLOWS
            or path.name not in ALLOWED_NON_RELEASE_WORKFLOWS
        ):
            documents[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return documents


def _load_workflow(path: Path) -> dict[str, object]:
    """Load Actions YAML without letting YAML 1.1 turn ``on`` into ``True``."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path.name} must contain a YAML object")
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _trigger(document: dict[str, object]) -> dict[str, object]:
    value = document.get("on")
    assert isinstance(value, dict)
    return value


def _jobs(document: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    assert all(isinstance(job, dict) for job in jobs.values())
    return jobs  # type: ignore[return-value]


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps  # type: ignore[return-value]


def _uses_in(document: dict[str, object]) -> list[str]:
    uses: list[str] = []
    for job in _jobs(document).values():
        if isinstance(job.get("uses"), str):
            uses.append(str(job["uses"]))
        for step in _steps(job):
            if isinstance(step.get("uses"), str):
                uses.append(str(step["uses"]))
    return uses


def _artifact_uploads(document: dict[str, object]) -> list[dict[str, object]]:
    return [
        step
        for job in _jobs(document).values()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]


def _has_upstream_guard(job: dict[object, object]) -> bool:
    condition = str(job.get("if", ""))
    return bool(
        re.search(
            r"github\.repository\s*==\s*['\"]ModelEngine-Group/unified-cache-management['\"]",
            condition,
        )
    )


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() == "true"


def _effective_permissions(
    workflow_permissions: object, job: dict[object, object]
) -> tuple[object, bool]:
    """GitHub job permissions replace workflow permissions when explicitly set."""
    if "permissions" in job:
        return job["permissions"], False
    return workflow_permissions, True


def _permissions_grant_write(permissions: object) -> bool:
    if isinstance(permissions, dict):
        return any(str(value).lower() == "write" for value in permissions.values())
    if isinstance(permissions, str):
        normalized = permissions.lower().replace(" ", "")
        return normalized == "write-all" or bool(
            re.search(r"(?:^|,)\w+:write(?:,|$)", normalized)
        )
    return False


def _action_operation(uses: object, inputs: object) -> str | None:
    if not isinstance(uses, str) or not uses:
        return None
    action = uses.split("@", 1)[0].lower()
    if action in SAFE_FORK_ACTIONS:
        return None
    if action == "docker/build-push-action":
        if isinstance(inputs, dict) and _truthy(inputs.get("push")):
            return "container publishing action"
        return None
    if action == "docker/login-action":
        return "registry credential action"
    if action.startswith("./.github/workflows/"):
        workflow_name = Path(action).name
        if workflow_name in EXPECTED_RELEASE_WORKFLOWS:
            return None
    return f"unapproved action {action}"


def _dangerous_job_operations(
    workflow_permissions: object, job: dict[object, object]
) -> list[str]:
    """Return publication-capable operations that must be upstream-gated."""
    operations: list[str] = []
    if job.get("secrets") == "inherit":
        operations.append("secrets: inherit")
    permissions, inherited = _effective_permissions(workflow_permissions, job)
    if _permissions_grant_write(permissions):
        label = (
            "workflow-inherited write permission" if inherited else "write permission"
        )
        operations.append(label)
    if "environment" in job:
        operations.append("protected environment")

    job_text = "\n".join(_strings(job)).lower()
    if "self-hosted" in job_text:
        operations.append("self-hosted runner")
    command_patterns = {
        r"\b(?:docker|crane)\s+(?:login|push|copy)\b": "registry login or publication",
        r"\bbuildx\s+build\b[^\n]*--push\b": "Buildx publication",
        r"\bgh\s+workflow\s+run\b": "workflow dispatch",
        r"\bgh\s+api\b[^\n]*(?:/dispatches\b|workflow_dispatch\b)": "GitHub dispatch API",
        r"\b(?:curl|wget)\b[^\n]*(?:/dispatches\b|workflow_dispatch\b)": "HTTP dispatch",
    }
    for pattern, label in command_patterns.items():
        if re.search(pattern, job_text):
            operations.append(label)

    job_action = _action_operation(job.get("uses"), job.get("with"))
    if job_action:
        operations.append(job_action)
    for step in job.get("steps", []) if isinstance(job.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        action_operation = _action_operation(step.get("uses"), step.get("with"))
        if action_operation:
            operations.append(action_operation)
    return sorted(set(operations))


def _fork_isolation_violations(documents: dict[str, object]) -> list[str]:
    """Audit entry and locally reusable release workflows for a fork path escape."""
    violations: list[str] = []
    for filename, document in documents.items():
        if not isinstance(document, dict):
            continue
        jobs = document.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        workflow_permissions = document.get("permissions")
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            operations = _dangerous_job_operations(workflow_permissions, job)
            if operations and not _has_upstream_guard(job):
                violations.append(
                    f"{filename}:{job_name} exposes fork candidates to "
                    f"{', '.join(operations)} without an upstream repository guard"
                )
    return violations


def test_release_workflows_are_compact_and_fork_candidate_is_read_only() -> None:
    """Demand a closed workflow set and no fork-to-publish escape path."""
    violations = _workflow_set_violations(WORKFLOW_DIR)

    entrypoint = WORKFLOW_DIR / "release-ucm.yml"
    document = (
        yaml.safe_load(entrypoint.read_text(encoding="utf-8"))
        if entrypoint.exists()
        else {}
    )
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    candidate = jobs.get("fork-candidate") if isinstance(jobs, dict) else None
    if not isinstance(candidate, dict):
        violations.append("release-ucm.yml must define a fork-candidate job")
    else:
        if candidate.get("permissions") != {"contents": "read"}:
            violations.append(
                "fork-candidate permissions must be exactly {'contents': 'read'}"
            )
        candidate_text = "\n".join(_strings(candidate)).lower()
        if "environment" in candidate:
            violations.append("fork-candidate must not use protected environments")
        banned_fragments = {
            "secrets.": "secrets",
            "self-hosted": "self-hosted runners",
        }
        for fragment, label in banned_fragments.items():
            if fragment in candidate_text:
                violations.append(f"fork-candidate must not use {label}")
        if re.search(r"\b(?:docker|crane)\s+(?:login|push)\b", candidate_text):
            violations.append(
                "fork-candidate must not log in to or push a container registry"
            )
        if re.search(r"\bgh\s+api\b.*\bdispatch", candidate_text):
            violations.append("fork-candidate must not dispatch workflows")

    documents = _release_workflow_documents(WORKFLOW_DIR)
    violations.extend(_fork_isolation_violations(documents))

    assert not violations, "release workflow safety contract failed:\n- " + "\n- ".join(
        violations
    )


def test_existing_cpp_changes_are_explicitly_forbidden_from_the_stage() -> None:
    """Keep the three pre-existing C++ edits visible but outside this release commit."""
    assert all((REPO_ROOT / path).is_file() for path in FORBIDDEN_STAGED_PATHS)
    staged = set(filter(None, _git("diff", "--cached", "--name-only").splitlines()))
    assert not staged & FORBIDDEN_STAGED_PATHS, json.dumps(
        {"forbidden_staged_paths": sorted(staged & FORBIDDEN_STAGED_PATHS)}, indent=2
    )


def test_workflow_set_rejects_an_arbitrary_publish_workflow(tmp_path: Path) -> None:
    """An unrecognised YAML workflow cannot evade the four-workflow budget."""
    for filename in EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS:
        (tmp_path / filename).write_text("name: allowed\n")
    (tmp_path / "publish.yaml").write_text("name: bypass\n")

    violations = _workflow_set_violations(tmp_path)

    assert len(violations) == 1
    assert "publish.yaml" in violations[0]


def test_fork_isolation_rejects_reusable_workflow_publish_mutations() -> None:
    """Reusable workflow mutations must be rejected even when entry job is clean."""
    documents = {
        "release-ucm.yml": {
            "jobs": {
                "fork-candidate": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [{"run": "python -m ucm_release core plan"}],
                }
            }
        },
        "_build-image.yml": {
            "jobs": {
                "mutated-reusable": {
                    "secrets": "inherit",
                    "runs-on": "self-hosted",
                    "steps": [
                        {"uses": "docker/login-action@v3"},
                        {
                            "uses": "docker/build-push-action@v6",
                            "with": {"push": True},
                        },
                        {"uses": "softprops/action-gh-release@v2"},
                        {
                            "run": (
                                "docker buildx build --push .\n"
                                "crane copy source target\n"
                                "gh workflow run child.yml\n"
                                "gh api --method POST repos/x/dispatches\n"
                                "curl -X POST https://api.github.com/repos/x/dispatches"
                            )
                        },
                    ],
                }
            }
        },
    }

    violations = _fork_isolation_violations(documents)

    assert len(violations) == 1
    violation = violations[0]
    for operation in (
        "secrets: inherit",
        "self-hosted runner",
        "registry credential action",
        "container publishing action",
        "unapproved action softprops/action-gh-release",
        "Buildx publication",
        "registry login or publication",
        "workflow dispatch",
        "GitHub dispatch API",
        "HTTP dispatch",
    ):
        assert operation in violation


def test_fork_isolation_allows_a_read_only_reusable_build() -> None:
    """A normal hosted build and artifact upload remain valid fork operations."""
    documents = {
        "_build-wheel.yml": {
            "jobs": {
                "build": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [
                        {"uses": "actions/checkout@full-sha"},
                        {"run": "docker buildx build --output type=oci,dest=out.tar ."},
                        {"uses": "actions/upload-artifact@full-sha"},
                    ],
                }
            }
        }
    }

    assert _fork_isolation_violations(documents) == []


def test_release_workflow_topology_runs_the_four_files_at_the_pushed_sha() -> None:
    """The feature push must reach wheel, Chart, image, and evidence jobs locally."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    triggers = _trigger(entry)
    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("branches") == ["feature/**"]
    assert push.get("tags") == ["v*"]
    assert "workflow_call" in triggers

    entry_jobs = _jobs(entry)
    assert entry_jobs["fork-candidate"]["permissions"] == {"contents": "read"}
    local_calls = {
        str(job["uses"])
        for job in entry_jobs.values()
        if isinstance(job.get("uses"), str)
    }
    assert local_calls == {
        "./.github/workflows/_build-wheel.yml",
        "./.github/workflows/release-vllm-images.yml",
    }
    assert all("@" not in reference for reference in local_calls)
    assert any("chart package" in value for value in _strings(entry))

    image_release = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_triggers = _trigger(image_release)
    assert set(image_triggers) == {
        "workflow_call",
        "schedule",
        "repository_dispatch",
        "workflow_dispatch",
    }
    image_calls = {
        str(job["uses"])
        for job in _jobs(image_release).values()
        if isinstance(job.get("uses"), str)
    }
    assert image_calls == {
        "./.github/workflows/_build-wheel.yml",
        "./.github/workflows/_build-image.yml",
    }
    image_jobs = _jobs(image_release)
    assert image_jobs["standalone-wheel"]["uses"] == (
        "./.github/workflows/_build-wheel.yml"
    )
    assert image_jobs["build-image"]["uses"] == ("./.github/workflows/_build-image.yml")
    assert set(image_jobs["final-reconcile"]["needs"]) == {
        "select-input",
        "reconcile-fixture",
        "build-image",
    }


def test_reusable_workflow_inputs_outputs_and_artifacts_are_exact() -> None:
    """Reusable boundaries must carry immutable identities, not implicit state."""
    wheel = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    wheel_call = _trigger(wheel)["workflow_call"]
    assert isinstance(wheel_call, dict)
    assert set(wheel_call.get("inputs", {})) == {
        "source_sha",
        "profile_id",
        "validation_lane",
    }
    assert {
        "wheel_artifact",
        "wheel_sha256",
        "inspection_sha256",
    } <= set(wheel_call.get("outputs", {}))
    wheel_text = "\n".join(_strings(wheel))
    assert "Requires-Dist wrapt==1.17.2" in wheel_text
    assert "wheel fixture-build" in wheel_text
    assert "fixture-only" in wheel_text

    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    image_call = _trigger(image)["workflow_call"]
    assert isinstance(image_call, dict)
    assert set(image_call.get("inputs", {})) == {
        "source_sha",
        "wheel_artifact",
        "image_input_artifact",
        "validation_lane",
    }
    assert {"image_artifact", "image_result_sha256", "oci_digest"} <= set(
        image_call.get("outputs", {})
    )
    image_text = "\n".join(_strings(image)).lower()
    assert "type=oci" in image_text
    assert "image verify" in image_text
    assert "--push" not in image_text
    assert not re.search(r"\b(?:cmake|ninja|gcc|g\+\+|clang|pip wheel)\b", image_text)

    for filename in EXPECTED_RELEASE_WORKFLOWS:
        document = _load_workflow(WORKFLOW_DIR / filename)
        uploads = _artifact_uploads(document)
        for upload in uploads:
            inputs = upload.get("with")
            assert isinstance(inputs, dict)
            assert inputs.get("retention-days") == 3


@pytest.mark.parametrize(
    ("filename", "valid_environment", "invalid_environment"),
    [
        (
            "_build-wheel.yml",
            {
                "SOURCE_SHA": "a" * 40,
                "PROFILE_ID": FIXTURE_PROFILE,
                "VALIDATION_LANE": "fork-candidate",
            },
            [
                {"SOURCE_SHA": "refs/heads/feature/cicd"},
                {"VALIDATION_LANE": "production"},
                {"PROFILE_ID": ""},
            ],
        ),
        (
            "_build-image.yml",
            {
                "SOURCE_SHA": "b" * 40,
                "WHEEL_ARTIFACT": "ucm-fixture-wheel-b",
                "IMAGE_INPUT_ARTIFACT": "ucm-image-input-b",
                "VALIDATION_LANE": "fork-candidate",
            },
            [
                {"SOURCE_SHA": "refs/heads/feature/cicd"},
                {"VALIDATION_LANE": "production"},
                {"WHEEL_ARTIFACT": ""},
                {"IMAGE_INPUT_ARTIFACT": ""},
            ],
        ),
    ],
)
def test_reusable_build_contract_gate_runs_before_checkout_or_untrusted_code(
    tmp_path: Path,
    filename: str,
    valid_environment: dict[str, str],
    invalid_environment: list[dict[str, str]],
) -> None:
    """Malformed calls must fail before checkout, Actions, Python, or network."""
    workflow = _load_workflow(WORKFLOW_DIR / filename)
    steps = _steps(_jobs(workflow)["build"])
    gate = steps[0]
    assert gate.get("name") == "Validate reusable build contract"
    assert gate.get("shell") == "bash"
    assert "uses" not in gate
    command = str(gate.get("run", ""))
    assert "set -euo pipefail" in command
    assert "python" not in command.lower()
    assert "curl" not in command

    checkout_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    setup_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    first_repo_or_network_index = next(
        index
        for index, step in enumerate(steps)
        if index > setup_index
        and any(
            marker in "\n".join(_strings(step))
            for marker in ("python", "ucm_release", "curl", "download-artifact")
        )
    )
    assert 0 < checkout_index < setup_index < first_repo_or_network_index

    base_environment = {**__import__("os").environ, **valid_environment}
    marker = tmp_path / "later-step-ran"
    wrapped = command + '\nprintf later >"${MARKER}"\n'
    valid = subprocess.run(
        ["bash", "-c", wrapped],
        env={**base_environment, "MARKER": str(marker)},
        check=False,
    )
    assert valid.returncode == 0
    assert marker.read_text(encoding="utf-8") == "later"

    for index, mutation in enumerate(invalid_environment):
        marker = tmp_path / f"invalid-{index}"
        rejected = subprocess.run(
            ["bash", "-c", wrapped],
            env={
                **base_environment,
                **mutation,
                "MARKER": str(marker),
            },
            check=False,
        )
        assert rejected.returncode == 2
        assert not marker.exists()


def test_reusable_entry_contracts_reject_empty_partial_and_illegal_calls() -> None:
    """A malformed workflow_call must run an explicit exit-2 job, never go green."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_inputs = _trigger(entry)["workflow_call"]["inputs"]
    assert entry_inputs["validation_lane"]["required"] is True
    assert entry_inputs["call_contract"]["default"] == "ucm-core-candidate-v1"
    entry_invalid = _jobs(entry)["invalid-call"]
    entry_condition = str(entry_invalid["if"])
    assert "inputs.call_contract != 'ucm-core-candidate-v1'" in entry_condition
    assert "inputs.validation_lane != 'fork-candidate'" in entry_condition
    assert "exit 2" in "\n".join(_strings(entry_invalid))
    assert "github.event_name != 'push'" in entry_condition
    assert "refs/heads/feature/" in entry_condition
    assert "refs/tags/v" in entry_condition

    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_inputs = _trigger(images)["workflow_call"]["inputs"]
    assert image_inputs["call_contract"]["default"] == "ucm-vllm-candidate-v1"
    assert all(
        image_inputs[name]["required"] is True
        for name in ("source_sha", "wheel_artifact", "validation_lane")
    )
    invalid = _jobs(images)["invalid-call"]
    condition = str(invalid["if"])
    for value in (
        "inputs.call_contract",
        "inputs.source_sha",
        "inputs.wheel_artifact",
        "inputs.validation_lane",
    ):
        assert value in condition
    assert "inputs.call_contract != 'ucm-vllm-candidate-v1'" in condition
    assert "inputs.source_sha == ''" in condition
    assert "inputs.wheel_artifact == ''" in condition
    assert "inputs.validation_lane != 'fork-candidate'" in condition
    assert "exit 2" in "\n".join(_strings(invalid))
    assert (
        'fromJSON(\'["schedule","repository_dispatch","workflow_dispatch"]\')'
        in condition
    )
    assert "github.event_name" in condition
    standalone = str(_jobs(images)["standalone-wheel"]["if"])
    assert all(
        f"inputs.{name} == ''" in standalone
        for name in (
            "call_contract",
            "source_sha",
            "wheel_artifact",
            "validation_lane",
        )
    )
    assert (
        'fromJSON(\'["schedule","repository_dispatch","workflow_dispatch"]\')'
        in standalone
    )


def test_empty_reusable_inputs_are_routed_only_for_explicit_direct_events() -> None:
    """Inherited events with all-empty inputs must fail instead of starting a lane."""

    direct_vllm_events = {"schedule", "repository_dispatch", "workflow_dispatch"}

    def vllm_route(event: str, values: tuple[str, str, str, str]) -> str:
        contract, source_sha, artifact, lane = values
        any_input = any(values)
        valid_call = (
            contract == "ucm-vllm-candidate-v1"
            and bool(source_sha)
            and bool(artifact)
            and lane == "fork-candidate"
        )
        if any_input:
            return "callable" if valid_call else "invalid"
        return "standalone" if event in direct_vllm_events else "invalid"

    empty = ("", "", "", "")
    exact = ("ucm-vllm-candidate-v1", "b" * 40, "wheel", "fork-candidate")
    for event in direct_vllm_events:
        assert vllm_route(event, empty) == "standalone"
    for inherited_event in ("push", "pull_request", "workflow_call", "merge_group"):
        assert vllm_route(inherited_event, empty) == "invalid"
        assert vllm_route(inherited_event, exact) == "callable"
    assert vllm_route("push", (exact[0], "", exact[2], exact[3])) == "invalid"
    assert vllm_route("push", (*exact[:3], "production")) == "invalid"

    def core_route(event: str, ref: str, contract: str, lane: str) -> str:
        if contract or lane:
            return (
                "callable"
                if (contract, lane) == ("ucm-core-candidate-v1", "fork-candidate")
                else "invalid"
            )
        allowed_ref = ref.startswith("refs/heads/feature/") or ref.startswith(
            "refs/tags/v"
        )
        return "direct" if event == "push" and allowed_ref else "invalid"

    assert core_route("push", "refs/heads/feature/cicd", "", "") == "direct"
    assert core_route("push", "refs/tags/v0.5.0", "", "") == "direct"
    assert core_route("push", "refs/heads/main", "", "") == "invalid"
    for inherited_event in ("workflow_call", "pull_request", "schedule"):
        assert core_route(inherited_event, "refs/heads/main", "", "") == "invalid"
        assert (
            core_route(
                inherited_event,
                "refs/heads/main",
                "ucm-core-candidate-v1",
                "fork-candidate",
            )
            == "callable"
        )


def test_candidate_evidence_binds_the_real_two_reconcile_closure() -> None:
    """The final artifact must bind actual build output and all required scenarios."""
    entry_text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    image_text = (WORKFLOW_DIR / "release-vllm-images.yml").read_text(encoding="utf-8")
    policy_text = (REPO_ROOT / ".github/release/ucm_release/verify.py").read_text(
        encoding="utf-8"
    )
    combined = entry_text + "\n" + image_text + "\n" + policy_text
    required_fields = {
        "release-loop-evidence.json",
        "payload_sha256",
        "workflow_refs",
        "source_sha",
        "wheel_sha256",
        "chart_sha256",
        "chart_tree_sha256",
        "upstream_index_digest",
        "oci_digest",
        "image_result_sha256",
        "first_reconcile_sha256",
        "second_reconcile_sha256",
        "publication",
        "write_audit",
    }
    assert not {field for field in required_fields if field not in combined}
    for scenario in (
        "new-input-one-task",
        "identical-input-zero-tasks",
        "tag-digest-drift-r2",
        "complete-digest-chain",
        "required-failures-block",
        "fixture-candidate-full-zero-reconcile",
    ):
        assert scenario in combined
    for accepted in ("a2", "a3"):
        assert f'"{accepted}"' in combined
    for rejected in ("310p", "a5"):
        assert rejected in combined
    assert "loop complete" in image_text
    assert "loop aggregate" in entry_text


def test_image_and_chart_artifacts_preserve_real_compact_evidence_layout(
    tmp_path: Path,
) -> None:
    """Artifact v4 download layout must expose flat Chart and OCI evidence paths."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    image_build = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    entry_text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    image_text = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    reconcile_text = (WORKFLOW_DIR / "release-vllm-images.yml").read_text(
        encoding="utf-8"
    )
    policy_text = (REPO_ROOT / ".github/release/ucm_release/verify.py").read_text(
        encoding="utf-8"
    )

    assert "--evidence-dir out/oci-evidence" in image_text
    assert "out/oci-evidence/" in image_text
    for filename in (
        "oci-layout.json",
        "index.json",
        "manifest.json",
        "config.json",
        "closure.json",
    ):
        assert filename in image_text or "out/oci-evidence/" in image_text
    aggregate_arguments = {
        "image-recipe.json": "--image-recipe",
        "image-metadata.json": "--image-metadata",
        "image-prepare-result.json": "--image-prepare",
        "buildkit-metadata.json": "--buildkit-metadata",
    }
    for filename, argument in aggregate_arguments.items():
        assert filename in reconcile_text
        assert argument in entry_text

    chart_upload = next(
        upload
        for upload in _artifact_uploads(entry)
        if "chart-artifact" in str(upload.get("with", {}).get("path", ""))
    )
    chart_path = str(chart_upload["with"]["path"])
    assert chart_path.strip() == "out/chart-artifact/"
    package_job = _jobs(entry)["package-chart"]
    package_commands = "\n".join(
        str(step.get("run", "")) for step in _steps(package_job)
    )
    assert "out/chart-artifact" in package_commands

    # Simulate upload-artifact v4's least-common-ancestor preservation and download.
    staging = tmp_path / "out" / "chart-artifact"
    staging.mkdir(parents=True)
    (staging / "ucm-1.0.0.tgz").write_bytes(b"chart")
    (staging / "chart-result.json").write_text("{}\n", encoding="utf-8")
    download = tmp_path / "input" / "chart"
    download.mkdir(parents=True)
    for source in staging.iterdir():
        (download / source.name).write_bytes(source.read_bytes())
    assert [path.name for path in download.glob("*.tgz")] == ["ucm-1.0.0.tgz"]
    assert (download / "chart-result.json").is_file()

    assert "out/oci-evidence" in str(_artifact_uploads(image_build))
    assert "out/oci-evidence" in str(_artifact_uploads(images))
    for argument in (
        "--wheel ",
        "--chart-package ",
        "--image-result ",
        "--completed-loop ",
        "--second-reconcile ",
    ):
        assert argument in entry_text
    assert "out/image-result.json" in image_text
    assert '"status": "blocked"' in policy_text
    assert '"attempted": False' in policy_text


def test_workflows_only_orchestrate_tested_cli_and_standalone_runs_full_closure() -> (
    None
):
    """Rules stay in Python while every public entry reaches the same fixture loop."""
    release_documents = {
        name: _load_workflow(WORKFLOW_DIR / name) for name in EXPECTED_RELEASE_WORKFLOWS
    }
    for name, document in release_documents.items():
        text = "\n".join(_strings(document))
        assert "python - <<" not in text, name
        assert "python3 - <<" not in text, name
    wheel_text = (WORKFLOW_DIR / "_build-wheel.yml").read_text(encoding="utf-8")
    image_text = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    reconcile_text = (WORKFLOW_DIR / "release-vllm-images.yml").read_text(
        encoding="utf-8"
    )
    entry_text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    assert "wheel fixture-build" in wheel_text
    assert "image prepare" in image_text and "image verify" in image_text
    assert "loop prepare" in reconcile_text and "loop complete" in reconcile_text
    assert "loop aggregate" in entry_text
    assert "standalone-wheel:" in reconcile_text
    assert "live-probes.json" in image_text
    assert 'identity_input":false' in image_text
    assert "CRANE_VERSION: v0.20.3" in image_text
    assert "image toolchain-authority" in image_text
    assert BUILDX_VERSION not in image_text
    assert BUILDX_LINUX_SHA256["amd64"] not in image_text
    assert BUILDX_LINUX_SHA256["arm64"] not in image_text
    assert BUILDKIT_IMAGE not in image_text
    assert re.search(r"CRANE_ARCHIVE_SHA256: [0-9a-f]{64}", image_text)
    assert "crane digest docker.io/vllm/vllm-openai:v0.10.2" in image_text
    assert "crane digest quay.io/ascend/vllm-ascend:v0.9.1" in image_text


def test_release_toolchains_are_immutable_and_checksum_verified() -> None:
    """Buildx, BuildKit, Dockerfile frontend, and Helm are all byte identities."""
    workflows = {
        path.name: _load_workflow(path) for path in _workflow_paths(WORKFLOW_DIR)
    }
    dockerfile = (REPO_ROOT / ".github/release/docker/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert _toolchain_pin_violations(workflows, dockerfile) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("buildx-authority", "toolchain-authority"),
        ("buildkit", "mutable BuildKit"),
        ("frontend", "frontend is mutable"),
        ("helm-version", "Helm version is not fixed"),
        ("helm-checksum", "Helm amd64 checksum missing"),
    ],
)
def test_toolchain_pin_audit_rejects_each_mutable_or_unverified_input(
    mutation: str, expected: str
) -> None:
    """Refreshing a version, image, frontend, or archive cannot pass the audit."""
    workflows = {
        path.name: _load_workflow(path) for path in _workflow_paths(WORKFLOW_DIR)
    }
    dockerfile = (REPO_ROOT / ".github/release/docker/Dockerfile").read_text(
        encoding="utf-8"
    )
    if mutation in {"buildx-authority", "buildkit"}:
        step = next(
            step
            for job in _jobs(workflows["_build-image.yml"]).values()
            for step in _steps(job)
            if step.get("name") == "Install checksum-pinned Buildx"
        )
        if mutation == "buildx-authority":
            step["run"] = str(step["run"]).replace(
                "image toolchain-authority", "image base-authority"
            )
        else:
            step["run"] = str(step["run"]).replace(
                '--driver-opt "image=${buildkit_image}"',
                '--driver-opt "image=moby/buildkit:latest"',
            )
    elif mutation == "frontend":
        dockerfile = dockerfile.replace(
            DOCKERFILE_FRONTEND, "# syntax=docker/dockerfile:1"
        )
    elif mutation == "helm-version":
        steps = _jobs(workflows["release-ucm.yml"])["package-chart"]["steps"]
        install = next(step for step in steps if step.get("name") == "Install Helm")
        install["env"]["HELM_VERSION"] = "latest"
    else:
        text = json.dumps(workflows["release-ucm.yml"])
        workflows["release-ucm.yml"] = json.loads(
            text.replace(HELM_LINUX_SHA256["amd64"], "0" * 64)
        )

    assert any(
        expected in violation
        for violation in _toolchain_pin_violations(workflows, dockerfile)
    )


def test_clean_image_build_rewrites_timestamps_without_disabling_dependencies() -> None:
    """Fresh runners must emit the same OCI bytes while pip still resolves wrapt."""
    workflow = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    installer = (REPO_ROOT / ".github/release/docker/install_ucm.py").read_text(
        encoding="utf-8"
    )
    verifier = (REPO_ROOT / ".github/release/ucm_release/image.py").read_text(
        encoding="utf-8"
    )

    assert "type=oci,dest=out/image.oci.tar,rewrite-timestamp=true" in workflow
    assert '"--no-cache-dir"' in installer
    assert '"--disable-pip-version-check"' in installer
    assert '"--only-binary=:all:"' in installer
    assert '"--no-deps"' not in installer
    assert '"--disable-pip-version-check"' in verifier
    assert '"--no-cache-dir"' in verifier


def test_every_hosted_cli_job_installs_the_locked_runtime_dependencies_first() -> None:
    """setup-python provides Python and pip, not PyYAML or packaging."""
    expected_jobs = {
        ("_build-wheel.yml", "build"),
        ("_build-image.yml", "build"),
        ("release-ucm.yml", "package-chart"),
        ("release-ucm.yml", "aggregate-evidence"),
        ("release-vllm-images.yml", "reconcile-fixture"),
        ("release-vllm-images.yml", "final-reconcile"),
    }
    observed_jobs: set[tuple[str, str]] = set()
    for filename in EXPECTED_RELEASE_WORKFLOWS:
        document = _load_workflow(WORKFLOW_DIR / filename)
        for job_name, job in _jobs(document).items():
            steps = _steps(job)
            cli_indexes = [
                index
                for index, step in enumerate(steps)
                if "python -m ucm_release" in str(step.get("run", ""))
            ]
            if not cli_indexes:
                continue
            observed_jobs.add((filename, job_name))
            prior_commands = "\n".join(
                str(step.get("run", "")) for step in steps[: min(cli_indexes)]
            )
            for required in (
                "python -m pip install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--only-binary=:all:",
                "PyYAML==6.0.2",
                "packaging==24.2",
            ):
                assert required in prior_commands, (filename, job_name, required)
    assert observed_jobs == expected_jobs


def test_reusable_image_router_uses_inputs_not_inherited_event_name() -> None:
    """A nested reusable workflow inherits the caller event (for example push)."""
    document = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    jobs = _jobs(document)
    standalone_condition = str(jobs["standalone-wheel"]["if"])
    select_condition = str(jobs["select-input"]["if"])
    invalid_manual_condition = str(jobs["invalid-manual-ref"]["if"])
    select_environment = jobs["select-input"]["steps"][0]["env"]

    assert "inputs.source_sha == ''" in standalone_condition
    assert "inputs.source_sha != ''" in select_condition
    assert "github.event_name == 'workflow_call'" not in standalone_condition
    assert "github.event_name == 'workflow_call'" not in select_condition
    assert "inputs.source_sha != ''" in str(select_environment["SOURCE_SHA"])
    assert "inputs.wheel_artifact != ''" in str(select_environment["WHEEL_ARTIFACT"])
    for name in (
        "call_contract",
        "source_sha",
        "wheel_artifact",
        "validation_lane",
    ):
        assert f"inputs.{name} == ''" in invalid_manual_condition

    def invalid_manual_route(
        event: str,
        ref_name: str,
        default_branch: str,
        values: tuple[str, str, str, str],
    ) -> bool:
        return (
            not any(values)
            and event == "workflow_dispatch"
            and ref_name != default_branch
        )

    exact = ("ucm-vllm-candidate-v1", "b" * 40, "wheel", "fork-candidate")
    assert (
        invalid_manual_route("workflow_dispatch", "feature/cicd", "main", exact)
        is False
    )
    assert (
        invalid_manual_route(
            "workflow_dispatch", "feature/cicd", "main", ("", "", "", "")
        )
        is True
    )


def test_reusable_release_entry_uses_input_lane_not_inherited_event_name() -> None:
    """The core reusable entry must recognize a call even when the event is push."""
    document = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    condition = str(_jobs(document)["fork-candidate"]["if"])

    assert "inputs.validation_lane == 'fork-candidate'" in condition
    assert "github.event_name == 'workflow_call'" not in condition


def test_fork_v_tag_runs_the_read_only_candidate_instead_of_green_noop() -> None:
    """A fork tag must prove blocked candidate behavior without entering production."""
    document = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    condition = str(_jobs(document)["fork-candidate"]["if"])

    assert "refs/tags/v" in condition
    assert (
        "github.repository != 'ModelEngine-Group/unified-cache-management'" in condition
    )


def test_production_and_unsupported_callable_lanes_fail_closed() -> None:
    """A caller must get an explicit failure, never a green no-op production run."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_jobs = _jobs(entry)
    blocked = entry_jobs["production-external-required"]
    blocked_condition = str(blocked["if"])
    assert "ModelEngine-Group/unified-cache-management" in blocked_condition
    assert "refs/tags/v" in blocked_condition
    assert blocked["environment"] == "ucm-production-release"
    assert "exit 2" in "\n".join(_strings(blocked))

    for filename in ("release-ucm.yml", "release-vllm-images.yml"):
        document = _load_workflow(WORKFLOW_DIR / filename)
        rejected = _jobs(document)["invalid-call"]
        condition = str(rejected["if"])
        assert "inputs.validation_lane != 'fork-candidate'" in condition
        assert rejected["permissions"] == {"contents": "read"}
        assert "exit 2" in "\n".join(_strings(rejected))


def test_changed_workflows_pin_actions_and_keep_fork_jobs_read_only() -> None:
    """Every edited action is immutable and every edited workflow defaults read-only."""
    violations: list[str] = []
    for filename in sorted(CHANGED_WORKFLOWS):
        document = _load_workflow(WORKFLOW_DIR / filename)
        if document.get("permissions") != {"contents": "read"}:
            violations.append(
                f"{filename}: workflow permissions are not contents: read"
            )
        for job_name, job in _jobs(document).items():
            if job.get("permissions") != {"contents": "read"}:
                violations.append(
                    f"{filename}:{job_name} permissions are not explicit contents: read"
                )
        for uses in _uses_in(document):
            if uses.startswith("./.github/workflows/"):
                if "@" in uses:
                    violations.append(
                        f"{filename}: local workflow is not same-SHA: {uses}"
                    )
            elif FULL_ACTION_SHA.fullmatch(uses) is None:
                violations.append(f"{filename}: action is not SHA-pinned: {uses}")
    assert violations == []


def test_push_and_pull_request_callers_are_explicitly_read_only() -> None:
    """Normal fork validation callers must not inherit the repository token default."""
    push = _load_workflow(WORKFLOW_DIR / "push-check.yml")
    pull_request = _load_workflow(WORKFLOW_DIR / "pull-request.yml")
    assert push["permissions"] == {"contents": "read"}
    assert _jobs(push)["lint-and-unit-tests"]["permissions"] == {"contents": "read"}
    assert pull_request["permissions"] == {"contents": "read"}
    for job_name, job in _jobs(pull_request).items():
        permissions, _ = _effective_permissions(pull_request["permissions"], job)
        assert permissions == {"contents": "read"}, job_name


def test_lint_workflow_explicitly_runs_compact_release_tests() -> None:
    """The normal push checks must execute the focused release suite on GitHub."""
    lint = _load_workflow(WORKFLOW_DIR / "lint-and-test.yml")
    text = "\n".join(_strings(lint))
    assert "pytest -q .github/release/tests" in text


def test_release_test_job_installs_locked_setup_runtime_in_clean_python() -> None:
    """Python 3.12 setup-python does not provide setuptools for setup.py tests."""
    lint = _load_workflow(WORKFLOW_DIR / "lint-and-test.yml")
    steps = _steps(_jobs(lint)["release-tests"])
    install = next(
        step
        for step in steps
        if step.get("name") == "Install compact release test dependencies"
    )
    command = str(install.get("run", ""))
    for required in (
        "python -m pip install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--only-binary=:all:",
        "pytest==8.3.5",
        "PyYAML==6.0.2",
        "packaging==24.2",
        "setuptools==83.0.0",
    ):
        assert required in command


def test_pin_audit_rejects_a_nested_unpinned_action() -> None:
    """An unpinned action in a reusable workflow must not pass the SHA audit."""
    mutated = {
        "permissions": {"contents": "read"},
        "jobs": {
            "build": {
                "runs-on": "ubuntu-24.04",
                "steps": [{"uses": "actions/checkout@v4"}],
            }
        },
    }
    assert any(
        not uses.startswith("./.github/workflows/")
        and FULL_ACTION_SHA.fullmatch(uses) is None
        for uses in _uses_in(mutated)
    )


def test_fixture_wheel_builder_is_deterministic_unpublished_and_source_bound(
    tmp_path: Path,
) -> None:
    """Workflow wheel generation belongs in tested Python, not inline YAML."""
    _, wheel_module, _ = _release_modules()
    source_sha = "a" * 40

    first = wheel_module.build_fixture_wheel(
        tmp_path / "first", source_sha, FIXTURE_PROFILE
    )
    second = wheel_module.build_fixture_wheel(
        tmp_path / "second", source_sha, FIXTURE_PROFILE
    )

    assert (
        Path(first["wheel_path"]).read_bytes()
        == Path(second["wheel_path"]).read_bytes()
    )
    assert first["wheel_sha256"] == second["wheel_sha256"]
    assert first["inspection_sha256"] == second["inspection_sha256"]
    assert first["build_record"] == second["build_record"]
    assert first["inspection"]["requires_dist"] == ["wrapt==1.17.2"]
    assert first["inspection"]["status"] == "fixture-only"
    assert first["inspection"]["published"] is False
    assert first["build_record"]["source_sha"] == source_sha
    assert first["build_record"]["publication_status"] == "unpublished"


@pytest.mark.parametrize(
    ("marker", "duplicate"),
    [
        (None, False),
        (b"SOURCE_SHA = get_sha()\nPROFILE_ID = 'x'\n", False),
        (b"SOURCE_SHA = 'b' * 40\nPROFILE_ID = 'x'\n", False),
        (b"SOURCE_SHA = 'b'\n", False),
        (b"SOURCE_SHA = 'b'\nPROFILE_ID = 'x'\nEXTRA = True\n", False),
        (
            b"SOURCE_SHA = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'\n"
            b"PROFILE_ID = 'x'\n",
            True,
        ),
    ],
)
def test_fixture_wheel_rejects_missing_duplicate_extra_or_nonliteral_binding(
    tmp_path: Path, marker: bytes | None, duplicate: bool
) -> None:
    """Fixture provenance is parsed as exact data and is never executed."""
    _, wheel_module, _ = _release_modules()
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", "b" * 40, FIXTURE_PROFILE
    )
    wheel_path = Path(fixture["wheel_path"])
    if duplicate:
        with pytest.warns(UserWarning, match="Duplicate name"):
            _rewrite_fixture_marker(wheel_path, marker, duplicate=True)
    else:
        _rewrite_fixture_marker(wheel_path, marker)
    actual_sha = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="(fixture|binding|marker|duplicate)"):
        wheel_module.inspect_wheel(wheel_path, FIXTURE_PROFILE, actual_sha, "fixture")


@pytest.mark.parametrize("mutation", ["profile", "extra-member", "trailing-bytes"])
def test_fixture_wheel_cannot_be_coherently_relabelled_or_extended(
    tmp_path: Path, mutation: str
) -> None:
    """Actual deterministic wheel bytes own profile and exact ZIP membership."""
    _, wheel_module, _ = _release_modules()
    source_sha = "b" * 40
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", source_sha, FIXTURE_PROFILE
    )
    wheel_path = Path(fixture["wheel_path"])
    inspected_profile = FIXTURE_PROFILE
    if mutation == "profile":
        inspected_profile = FIXTURE_PROFILE.replace("cu129", "cu130")
    elif mutation == "extra-member":
        marker = (
            f"SOURCE_SHA = {source_sha!r}\nPROFILE_ID = {FIXTURE_PROFILE!r}\n"
        ).encode()
        _rewrite_fixture_marker(
            wheel_path,
            marker,
            extra_member=("UNTRACKED-TRAILER", b"coherently-recorded"),
        )
    else:
        wheel_path.write_bytes(wheel_path.read_bytes() + b"trailing-bytes")
    actual_sha = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="(fixture|profile|member|trailing)"):
        wheel_module.inspect_wheel(wheel_path, inspected_profile, actual_sha, "fixture")


def test_loop_orchestration_prepares_completes_and_aggregates_canonical_evidence(
    tmp_path: Path,
) -> None:
    """Aggregate must reopen every artifact and recompute the whole closure."""
    closure = _release_closure(tmp_path)
    completed = closure["completed_value"]
    assert completed["second_reconcile"]["task_count"] == 0
    assert completed["evidence"]["payload"]["compatibility"] == {
        "accepted": ["a2", "a3"],
        "rejected": ["310p", "a5"],
    }
    assert completed["evidence"]["payload"]["publication"] == {
        "status": "blocked",
        "attempted": False,
    }
    aggregate = _aggregate(closure)
    assert aggregate["payload"]["source_sha"] == closure["source_sha"]
    assert aggregate["payload"]["must_green"] == {
        "fixture_wheel": True,
        "helm_cuda_a2_a3": True,
        "install_only_image": True,
        "second_reconcile_zero": True,
    }
    assert (
        aggregate["payload"]["artifact_digests"]["wheel_sha256"]
        == closure["fixture"]["wheel_sha256"]
    )
    assert aggregate["payload"]["write_audit"]["write_count"] == 0
    assert aggregate["payload"]["write_audit"]["ledger_sha256"].startswith("sha256:")
    rerun_closure = _release_closure(tmp_path / "attempt-2", attempt=2)
    rerun = _aggregate(rerun_closure, attempt=2)
    assert aggregate["payload_sha256"] == rerun["payload_sha256"]
    assert (
        aggregate["payload"]["artifact_digests"] == rerun["payload"]["artifact_digests"]
    )
    assert (
        aggregate["github"]["non_deterministic_artifact_file_sha256"]
        != rerun["github"]["non_deterministic_artifact_file_sha256"]
    )


def test_aggregate_rejects_self_consistent_but_unauthorized_base(
    tmp_path: Path,
) -> None:
    """Internal descriptor consistency cannot replace the release base policy."""
    closure = _release_closure(tmp_path, authoritative_base=False)

    with pytest.raises(ValueError, match="base.*authority"):
        _aggregate(closure)


@pytest.mark.parametrize(
    "mutation",
    [
        "build-record-extra",
        "build-record-missing",
        "wheel-inspection-extra",
        "wheel-inspection-missing",
        "wheel-bytes",
        "wheel-source-binding-coherent",
        "chart-result-extra",
        "chart-result-missing",
        "chart-bytes",
        "image-result-extra",
        "image-result-missing",
        "image-result-whitespace",
        "image-oci-digest-rehashed",
        "oci-raw-evidence-bypass-rehashed",
        "image-base-bytes-rehashed",
        "image-implementation-rehashed",
        "image-wheel-sha-rehashed",
        "oci-empty-layers-coherent",
        "oci-layer-media-coherent",
        "recipe-source-date-coherent",
        "scenario-rehashed",
        "gate-rehashed",
        "publication-rehashed",
        "write-ledger-rehashed",
        "second-reconcile-nonzero",
        "completed-extra",
        "completed-missing",
        "second-extra",
        "second-missing",
        "image-loop-extra",
        "image-loop-missing",
    ],
)
def test_aggregate_rejects_mutated_artifacts_even_after_envelope_rehash(
    tmp_path: Path, mutation: str
) -> None:
    """Changing bytes or rehashing a forged summary never authorizes publication."""
    closure = _release_closure(tmp_path)
    core = closure["core"]
    paths = closure["paths"]
    completed = copy.deepcopy(closure["completed_value"])

    def rewrite_evidence() -> None:
        completed["evidence"]["payload_sha256"] = core.sha256_value(
            completed["evidence"]["payload"]
        )
        _write_canonical(paths["completed_loop"], completed)
        _write_canonical(paths["image_loop"], completed["evidence"])

    if mutation == "wheel-source-binding-coherent":
        changed_source = "c" * 40
        build_record = json.loads(paths["build_record"].read_text(encoding="utf-8"))
        build_record["source_sha"] = changed_source
        _write_canonical(paths["build_record"], build_record)
        closure["source_sha"] = changed_source
        closure["prepared"] = copy.deepcopy(closure["prepared"])
        closure["prepared"]["source_sha"] = changed_source
        forged = closure["verify"].complete_candidate_loop(
            closure["prepared"],
            closure["image_result_value"],
            source_sha=changed_source,
            run=completed["evidence"]["github"],
        )
        _write_canonical(paths["completed_loop"], forged)
        _write_canonical(paths["second_reconcile"], forged["second_reconcile"])
        _write_canonical(paths["image_loop"], forged["evidence"])
    elif mutation == "recipe-source-date-coherent":
        recipe = json.loads(paths["image_recipe"].read_text(encoding="utf-8"))
        recipe["payload"]["source_date_epoch"] = 123
        recipe["payload_sha256"] = core.sha256_value(recipe["payload"])
        _write_canonical(paths["image_recipe"], recipe)
        _write_canonical(paths["image_prepare"], recipe)
        compact = json.loads(
            (paths["oci_evidence"] / "closure.json").read_text(encoding="utf-8")
        )
        compact["recipe_payload_sha256"] = recipe["payload_sha256"]
        _write_canonical(paths["oci_evidence"] / "closure.json", compact)
        result = copy.deepcopy(closure["image_result_value"])
        result["recipe_sha256"] = recipe["payload_sha256"]
        result["result_sha256"] = core.sha256_value(
            {key: value for key, value in result.items() if key != "result_sha256"}
        )
        _write_canonical(paths["image_result"], result)
        forged = closure["verify"].complete_candidate_loop(
            closure["prepared"],
            result,
            source_sha=closure["source_sha"],
            run=completed["evidence"]["github"],
        )
        _write_canonical(paths["completed_loop"], forged)
        _write_canonical(paths["second_reconcile"], forged["second_reconcile"])
        _write_canonical(paths["image_loop"], forged["evidence"])
    elif mutation in {"oci-empty-layers-coherent", "oci-layer-media-coherent"}:
        evidence_dir = paths["oci_evidence"]
        manifest = json.loads(
            (evidence_dir / "manifest.json").read_text(encoding="utf-8")
        )
        config = json.loads((evidence_dir / "config.json").read_text(encoding="utf-8"))
        index = json.loads((evidence_dir / "index.json").read_text(encoding="utf-8"))
        compact = json.loads(
            (evidence_dir / "closure.json").read_text(encoding="utf-8")
        )
        if mutation == "oci-empty-layers-coherent":
            manifest["layers"] = []
            config["rootfs"]["diff_ids"] = []
        else:
            manifest["layers"][0]["mediaType"] = "application/vnd.example.layer"

        def descriptor_bytes(value: object) -> tuple[bytes, str]:
            content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            return content, "sha256:" + hashlib.sha256(content).hexdigest()

        config_raw, config_digest = descriptor_bytes(config)
        manifest["config"] = {
            **manifest["config"],
            "digest": config_digest,
            "size": len(config_raw),
        }
        manifest_raw, manifest_digest = descriptor_bytes(manifest)
        index["manifests"][0] = {
            **index["manifests"][0],
            "digest": manifest_digest,
            "size": len(manifest_raw),
        }
        (evidence_dir / "config.json").write_bytes(config_raw)
        (evidence_dir / "manifest.json").write_bytes(manifest_raw)
        (evidence_dir / "index.json").write_bytes(
            json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
        )
        compact["manifest_descriptor"] = index["manifests"][0]
        compact["config_descriptor"] = manifest["config"]
        compact["layers"] = manifest["layers"]
        compact["diff_ids"] = config["rootfs"]["diff_ids"]
        _write_canonical(evidence_dir / "closure.json", compact)
        result = copy.deepcopy(closure["image_result_value"])
        result["oci"]["digest"] = manifest_digest
        result["result_sha256"] = core.sha256_value(
            {key: value for key, value in result.items() if key != "result_sha256"}
        )
        _write_canonical(paths["image_result"], result)
        buildkit = json.loads(paths["buildkit_metadata"].read_text(encoding="utf-8"))
        buildkit["containerimage.digest"] = manifest_digest
        buildkit["containerimage.config.digest"] = config_digest
        buildkit["containerimage.descriptor"] = index["manifests"][0]
        _write_canonical(paths["buildkit_metadata"], buildkit)
        forged = closure["verify"].complete_candidate_loop(
            closure["prepared"],
            result,
            source_sha=closure["source_sha"],
            run=completed["evidence"]["github"],
        )
        _write_canonical(paths["completed_loop"], forged)
        _write_canonical(paths["second_reconcile"], forged["second_reconcile"])
        _write_canonical(paths["image_loop"], forged["evidence"])
    elif mutation in {"build-record-extra", "build-record-missing"}:
        value = json.loads(paths["build_record"].read_text(encoding="utf-8"))
        if mutation.endswith("extra"):
            value["extra"] = True
        else:
            del value["profile_id"]
        _write_canonical(paths["build_record"], value)
    elif mutation in {"wheel-inspection-extra", "wheel-inspection-missing"}:
        value = json.loads(paths["wheel_inspection"].read_text(encoding="utf-8"))
        if mutation.endswith("extra"):
            value["extra"] = True
        else:
            del value["status"]
        _write_canonical(paths["wheel_inspection"], value)
    elif mutation == "wheel-bytes":
        paths["wheel"].write_bytes(paths["wheel"].read_bytes() + b"changed")
    elif mutation in {"chart-result-extra", "chart-result-missing"}:
        value = json.loads(paths["chart_result"].read_text(encoding="utf-8"))
        if mutation.endswith("extra"):
            value["extra"] = True
        else:
            del value["status"]
        _write_canonical(paths["chart_result"], value)
    elif mutation == "chart-bytes":
        paths["chart_package"].write_bytes(
            paths["chart_package"].read_bytes() + b"changed"
        )
    elif mutation in {
        "image-result-extra",
        "image-result-missing",
        "image-oci-digest-rehashed",
        "oci-raw-evidence-bypass-rehashed",
        "image-base-bytes-rehashed",
        "image-implementation-rehashed",
        "image-wheel-sha-rehashed",
    }:
        value = copy.deepcopy(closure["image_result_value"])
        if mutation == "image-result-extra":
            value["extra"] = True
        elif mutation == "image-result-missing":
            del value["status"]
        elif mutation in {
            "image-oci-digest-rehashed",
            "oci-raw-evidence-bypass-rehashed",
        }:
            value["oci"]["digest"] = "sha256:" + "8" * 64
        elif mutation == "image-base-bytes-rehashed":
            value["base"]["config"]["raw"] += " "
        elif mutation == "image-implementation-rehashed":
            value["implementation"]["aggregate_sha256"] = "sha256:" + "8" * 64
        else:
            value["wheel"]["sha256"] = "sha256:" + "8" * 64
        payload = {key: item for key, item in value.items() if key != "result_sha256"}
        value["result_sha256"] = core.sha256_value(payload)
        _write_canonical(paths["image_result"], value)
        if mutation == "oci-raw-evidence-bypass-rehashed":
            forged = closure["verify"].complete_candidate_loop(
                closure["prepared"],
                value,
                source_sha=closure["source_sha"],
                run=completed["evidence"]["github"],
            )
            _write_canonical(paths["completed_loop"], forged)
            _write_canonical(paths["second_reconcile"], forged["second_reconcile"])
            _write_canonical(paths["image_loop"], forged["evidence"])
    elif mutation == "image-result-whitespace":
        paths["image_result"].write_text(
            json.dumps(closure["image_result_value"], indent=2) + "\n",
            encoding="utf-8",
        )
    elif mutation in {
        "scenario-rehashed",
        "gate-rehashed",
        "publication-rehashed",
        "write-ledger-rehashed",
    }:
        payload = completed["evidence"]["payload"]
        if mutation == "scenario-rehashed":
            payload["scenarios"][0]["passed"] = False
        elif mutation == "gate-rehashed":
            payload["required_gates"]["abi"] = "failed"
        elif mutation == "publication-rehashed":
            payload["publication"] = {"status": "published", "attempted": True}
        else:
            payload["operation_batches"][0][0]["capability"] = "write"
            payload["write_audit"]["write_count"] = 0
        rewrite_evidence()
    elif mutation == "second-reconcile-nonzero":
        completed["second_reconcile"]["task_count"] = 1
        completed["evidence"]["payload"]["second_task_count"] = 1
        completed["evidence"]["payload"]["second_reconcile_sha256"] = core.sha256_value(
            completed["second_reconcile"]
        )
        _write_canonical(paths["second_reconcile"], completed["second_reconcile"])
        rewrite_evidence()
    elif mutation in {"completed-extra", "completed-missing"}:
        if mutation.endswith("extra"):
            completed["extra"] = True
        else:
            del completed["evidence"]
        _write_canonical(paths["completed_loop"], completed)
    elif mutation in {"second-extra", "second-missing"}:
        value = copy.deepcopy(completed["second_reconcile"])
        if mutation.endswith("extra"):
            value["extra"] = True
        else:
            del value["decision"]
        _write_canonical(paths["second_reconcile"], value)
    else:
        value = copy.deepcopy(completed["evidence"])
        if mutation.endswith("extra"):
            value["extra"] = True
        else:
            del value["github"]
        _write_canonical(paths["image_loop"], value)

    with pytest.raises(ValueError):
        _aggregate(closure)


def test_aggregate_cli_reopens_files_and_exits_two_for_mutation(
    tmp_path: Path,
) -> None:
    """The public command exposes the same fail-closed file contract."""
    closure = _release_closure(tmp_path)
    paths = closure["paths"]
    environment = {**__import__("os").environ, "PYTHONPATH": ".github/release"}

    def invoke(output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ucm_release",
                "loop",
                "aggregate",
                "--build-record",
                str(paths["build_record"]),
                "--wheel-inspection",
                str(paths["wheel_inspection"]),
                "--wheel",
                str(paths["wheel"]),
                "--chart-result",
                str(paths["chart_result"]),
                "--chart-package",
                str(paths["chart_package"]),
                "--image-result",
                str(paths["image_result"]),
                "--oci-evidence-dir",
                str(paths["oci_evidence"]),
                "--image-recipe",
                str(paths["image_recipe"]),
                "--image-metadata",
                str(paths["image_metadata"]),
                "--image-prepare",
                str(paths["image_prepare"]),
                "--buildkit-metadata",
                str(paths["buildkit_metadata"]),
                "--image-archive-sha256",
                str(paths["image_archive_sha256"]),
                "--completed-loop",
                str(paths["completed_loop"]),
                "--second-reconcile",
                str(paths["second_reconcile"]),
                "--image-loop",
                str(paths["image_loop"]),
                "--repository",
                "SuperMarioYL/unified-cache-management",
                "--ref",
                "refs/heads/feature/cicd",
                "--source-sha",
                str(closure["source_sha"]),
                "--run-id",
                "17",
                "--attempt",
                "1",
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    passed = invoke(tmp_path / "evidence.json")
    assert passed.returncode == 0, passed.stderr
    paths["image_result"].write_text(
        json.dumps(closure["image_result_value"], indent=2) + "\n",
        encoding="utf-8",
    )
    rejected = invoke(tmp_path / "mutated-evidence.json")
    assert rejected.returncode == 2
    assert "canonical JSON bytes" in rejected.stderr


def test_loop_orchestration_rejects_wrong_source_or_published_image(
    tmp_path: Path,
) -> None:
    """A workflow caller cannot relabel bytes or a publication as fixture evidence."""
    core, wheel_module, verify_module = _release_modules()
    source_sha = "c" * 40
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", source_sha, FIXTURE_PROFILE
    )
    with __import__("pytest").raises(ValueError, match="source"):
        verify_module.prepare_candidate_loop(
            fixture["build_record"], fixture["inspection"], source_sha="d" * 40, run={}
        )
    prepared = verify_module.prepare_candidate_loop(
        fixture["build_record"], fixture["inspection"], source_sha=source_sha, run={}
    )
    payload = {
        "fixture_only": True,
        "unpublished": False,
        "publication_attempted": True,
        "status": "published",
        "build_key_sha256": prepared["candidate"]["build_key_sha256"],
        "wheel": {"sha256": fixture["wheel_sha256"]},
        "oci": {"digest": "sha256:" + "9" * 64},
        "gates": {},
        "runtime_validation": "external-required",
        "device_validation": "external-required",
    }
    published = {**payload, "result_sha256": core.sha256_value(payload)}
    with __import__("pytest").raises(ValueError, match="unpublished"):
        verify_module.complete_candidate_loop(
            prepared, published, source_sha=source_sha, run={}
        )


def test_image_context_bundle_reopens_fixed_base_descriptor_bytes(
    tmp_path: Path,
) -> None:
    """Base fetching stays orchestration; descriptor validation stays tested Python."""
    _, wheel_module, verify_module = _release_modules()
    image_module = importlib.import_module("ucm_release.image")
    source_sha = "e" * 40
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", source_sha, FIXTURE_PROFILE
    )
    prepared = verify_module.prepare_candidate_loop(
        fixture["build_record"],
        fixture["inspection"],
        source_sha=source_sha,
        run={},
    )
    encoded = json.loads(BASE_AUTHORITY_FIXTURE.read_text(encoding="utf-8"))
    raw = {label: base64.b64decode(value) for label, value in encoded.items()}
    authority = image_module.fixture_base_authority()
    blobs = {}
    for label, content in raw.items():
        path = tmp_path / f"{label}.json"
        path.write_bytes(content)
        blobs[label] = path
    context = tmp_path / "context"
    recipe = image_module.prepare_context_bundle(
        prepared["image_input"],
        wheel_dir=tmp_path / "wheel",
        expected_source_sha=source_sha,
        base_authority=authority,
        base_index_path=blobs["index"],
        base_manifest_path=blobs["manifest"],
        base_config_path=blobs["config"],
        output_dir=context,
    )
    assert recipe["payload"]["base"]["subject"] == (
        authority["repository"] + "@" + authority["manifest_digest"]
    )
    assert (
        sorted(path.name for path in context.iterdir())
        == recipe["payload"]["context_files"]
    )
    with pytest.raises(ValueError, match="source"):
        image_module.prepare_context_bundle(
            prepared["image_input"],
            wheel_dir=tmp_path / "wheel",
            expected_source_sha="f" * 40,
            base_authority=authority,
            base_index_path=blobs["index"],
            base_manifest_path=blobs["manifest"],
            base_config_path=blobs["config"],
            output_dir=tmp_path / "wrong-source-context",
        )
    blobs["config"].write_bytes(raw["config"] + b"\n")
    with __import__("pytest").raises(ValueError, match="config digest"):
        image_module.prepare_context_bundle(
            prepared["image_input"],
            wheel_dir=tmp_path / "wheel",
            expected_source_sha=source_sha,
            base_authority=authority,
            base_index_path=blobs["index"],
            base_manifest_path=blobs["manifest"],
            base_config_path=blobs["config"],
            output_dir=tmp_path / "bad-context",
        )


def test_base_authority_is_owned_by_python_and_consumed_once_by_workflow() -> None:
    """Workflow orchestration reads one canonical policy instead of duplicating pins."""
    image_module = importlib.import_module("ucm_release.image")
    authority = image_module.fixture_base_authority()
    environment = {
        **__import__("os").environ,
        "PYTHONPATH": str(REPO_ROOT / ".github" / "release"),
    }
    command = subprocess.run(
        [sys.executable, "-m", "ucm_release", "image", "base-authority"],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(command.stdout) == authority

    workflow = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    assert "image base-authority" in workflow
    assert "--base-authority out/base-authority.json" in workflow
    assert '--expected-source-sha "${SOURCE_SHA}"' in workflow
    for field in (
        "repository",
        "index_digest",
        "manifest_digest",
        "config_digest",
    ):
        assert str(authority[field]) not in workflow
    assert "BASE_REPOSITORY" not in workflow
    assert "BASE_INDEX_DIGEST" not in workflow
    assert "BASE_MANIFEST_DIGEST" not in workflow
    assert "BASE_CONFIG_DIGEST" not in workflow


def test_image_toolchain_authority_is_python_owned_and_strict() -> None:
    """Buildx bytes and BuildKit image must share one candidate identity source."""
    image_module = importlib.import_module("ucm_release.image")
    authority = image_module.fixture_image_toolchain_authority()
    environment = {
        **__import__("os").environ,
        "PYTHONPATH": str(REPO_ROOT / ".github" / "release"),
    }
    command = subprocess.run(
        [sys.executable, "-m", "ucm_release", "image", "toolchain-authority"],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(command.stdout) == authority
    assert authority == {
        "schema_version": 1,
        "kind": "ucm-fixture-image-toolchain-authority",
        "buildx_version": BUILDX_VERSION,
        "buildx_linux_sha256": {
            architecture: "sha256:" + digest
            for architecture, digest in BUILDX_LINUX_SHA256.items()
        },
        "buildkit_image": BUILDKIT_IMAGE,
    }

    workflow = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    for literal in (
        BUILDX_VERSION,
        *BUILDX_LINUX_SHA256.values(),
        BUILDKIT_IMAGE,
    ):
        assert literal not in workflow
    malformed = copy.deepcopy(authority)
    malformed["buildx_linux_sha256"]["amd64"] = "not-a-digest"
    with pytest.raises(ValueError, match="Buildx.*digest"):
        image_module.validate_image_toolchain_authority(malformed)


def test_compact_cli_owns_fixture_build_and_loop_preparation(tmp_path: Path) -> None:
    """Hosted jobs should invoke small CLI commands instead of inline policy code."""
    release_root = REPO_ROOT / ".github" / "release"
    environment = {**__import__("os").environ, "PYTHONPATH": str(release_root)}
    source_sha = "f" * 40
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "ucm_release",
            "wheel",
            "fixture-build",
            "--output-dir",
            str(wheel_dir),
            "--source-sha",
            source_sha,
            "--profile-id",
            FIXTURE_PROFILE,
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    prepared_dir = tmp_path / "prepared"
    prepared = subprocess.run(
        [
            sys.executable,
            "-m",
            "ucm_release",
            "loop",
            "prepare",
            "--build-record",
            str(wheel_dir / "fixture-build.json"),
            "--wheel-inspection",
            str(wheel_dir / "wheel-inspection.json"),
            "--source-sha",
            source_sha,
            "--output-dir",
            str(prepared_dir),
            "--run-id",
            "local",
            "--attempt",
            "1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    assert {
        "prepared-loop.json",
        "image-input.json",
        "candidate.json",
        "first-reconcile.json",
        "first-reconcile.sha256",
        "loop-verification.json",
    } == {path.name for path in prepared_dir.iterdir()}


def test_yaml_workflow_inherits_write_permissions_and_rejects_unknown_actions(
    tmp_path: Path,
) -> None:
    """Both permission inheritance and unknown action capability apply to .yaml."""
    (tmp_path / "publish.yaml").write_text(
        """
permissions: write-all
jobs:
  inherited-permission:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/create-release@v1
  job-permission:
    permissions:
      contents: write
    runs-on: ubuntu-24.04
    steps:
      - run: echo publish
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "copy.yaml").write_text(
        """
permissions:
  contents: write
jobs:
  inherited-map-permission:
    runs-on: ubuntu-24.04
    steps:
      - run: echo publish
""".lstrip(),
        encoding="utf-8",
    )

    violations = _fork_isolation_violations(_release_workflow_documents(tmp_path))

    assert len(violations) == 3
    assert any(
        "publish.yaml:inherited-permission" in violation
        and "workflow-inherited write permission" in violation
        and "unapproved action actions/create-release" in violation
        for violation in violations
    )
    assert any(
        "publish.yaml:job-permission" in violation and "write permission" in violation
        for violation in violations
    )
    assert any(
        "copy.yaml:inherited-map-permission" in violation
        and "workflow-inherited write permission" in violation
        for violation in violations
    )
