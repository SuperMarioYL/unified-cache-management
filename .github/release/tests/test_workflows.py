"""RED workflow and staging-safety contract for the slim release lane."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_RELEASE_WORKFLOWS = {
    "_build-image.yml",
    "_publish-image-member.yml",
    "_build-wheel.yml",
    "release-ucm.yml",
    "release-vllm-images-protected.yml",
    "release-vllm-images.yml",
}
ALLOWED_NON_RELEASE_WORKFLOWS = {
    "lint-and-test.yml",
    "pull-request.yml",
    "push-check.yml",
}
V2_DRY_RUN_WORKFLOWS = {
    "draft-environment-dry-run.yml",
    "develop-release-dry-run.yml",
    "nightly-release-dry-run.yml",
    "pr-release-dry-run.yml",
    "release-lifecycle-dry-run.yml",
    "release-cleanup-dry-run.yml",
    "release-control-dry-run.yml",
    "repository-policy-audit-dry-run.yml",
}
PRODUCTION_WORKFLOWS = {
    "production-tag-candidate.yml",
    "_production-build-wheel.yml",
    "_production-build-image.yml",
    "production-release-controller.yml",
    "_production-release-controller.yml",
    "_production-publish-image-member.yml",
}
WORKFLOW_SUFFIXES = {".yml", ".yaml"}
SAFE_FORK_ACTIONS = {
    "actions/cache",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "jlumbroso/free-disk-space",
    "azure/setup-helm",
    "docker/setup-buildx-action",
    "docker/setup-qemu-action",
    "sigstore/cosign-installer",
}
CHANGED_WORKFLOWS = (
    EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS | V2_DRY_RUN_WORKFLOWS
)
FULL_ACTION_SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")
TRUSTED_V2_CONTROLLER = (
    "SuperMarioYL/unified-cache-management/"
    ".github/workflows/release-control-dry-run.yml@main"
)
FORBIDDEN_STAGED_PATHS = {
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.cc",
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.h",
    "ucm/store/compress/cc/compressor_action.cc",
}
FIXTURE_PROFILE = "cuda130-amd64"
WORKFLOW_FIXTURE_PROFILE = (
    "cuda-cu129-ubuntu2204-amd64-cp312-release-default-sm75-sm80-sm86-sm89-sm90"
)
REAL_SPEC_IDS = [
    "cuda130-amd64",
    "cuda130-arm64",
    "cann900-a2-amd64",
    "cann900-a2-arm64",
    "cann900-a3-amd64",
    "cann900-a3-arm64",
]
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
    image_workflow_text = "\n".join(_strings(workflows["_build-image.yml"]))
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
            '["toolchain"]["buildx_version"]',
            '["toolchain"]["buildx_linux_sha256"]',
            '["toolchain"]["buildkit_image"]',
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
        if "image real-authorities" not in image_workflow_text:
            violations.append(
                "_build-image.yml: real image toolchain-authority is missing"
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
            if step.get("name") in {"Install Helm", "Install checksum-pinned Helm"}
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
            if step.get("name") == "Install checksum-pinned Helm":
                if (
                    "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz"
                    not in command
                ):
                    violations.append(f"{filename}: Helm archive URL is not fixed")
                if (
                    not isinstance(environment, dict)
                    or environment.get("HELM_SHA256") != HELM_LINUX_SHA256["amd64"]
                ):
                    violations.append(f"{filename}: Helm amd64 checksum missing")
            else:
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
    expected = (
        EXPECTED_RELEASE_WORKFLOWS
        | ALLOWED_NON_RELEASE_WORKFLOWS
        | V2_DRY_RUN_WORKFLOWS
        | PRODUCTION_WORKFLOWS
    )
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
            or path.name not in ALLOWED_NON_RELEASE_WORKFLOWS | PRODUCTION_WORKFLOWS
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
    return all(
        fragment in condition
        for fragment in (
            "github.event_name == 'push'",
            "github.repository == 'SuperMarioYL/unified-cache-management'",
            "github.ref == 'refs/tags/v0.5.0rc1'",
            "github.ref_protected == true",
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
    if uses == TRUSTED_V2_CONTROLLER:
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
    candidate = jobs.get("build-wheels-feature") if isinstance(jobs, dict) else None
    if not isinstance(candidate, dict):
        violations.append(
            "release-ucm.yml must define a build-wheels-feature candidate job"
        )
    else:
        if candidate.get("permissions") != {"contents": "read"}:
            violations.append(
                "build-wheels-feature permissions must be exactly {'contents': 'read'}"
            )
        candidate_text = "\n".join(_strings(candidate)).lower()
        if "environment" in candidate:
            violations.append(
                "build-wheels-feature must not use protected environments"
            )
        banned_fragments = {
            "secrets.": "secrets",
            "self-hosted": "self-hosted runners",
        }
        for fragment, label in banned_fragments.items():
            if fragment in candidate_text:
                violations.append(f"build-wheels-feature must not use {label}")
        if re.search(r"\b(?:docker|crane)\s+(?:login|push)\b", candidate_text):
            violations.append(
                "build-wheels-feature must not log in to or push a container registry"
            )
        if re.search(r"\bgh\s+api\b.*\bdispatch", candidate_text):
            violations.append("build-wheels-feature must not dispatch workflows")

    documents = _release_workflow_documents(WORKFLOW_DIR)
    violations.extend(_fork_isolation_violations(documents))

    assert not violations, "release workflow safety contract failed:\n- " + "\n- ".join(
        violations
    )


def test_real_hosted_matrix_projects_the_reviewed_six_tasks_without_a_second_matrix() -> (
    None
):
    """A missing/wrong task, runner, target, or immutable builder breaks hosted builds."""
    core, _, verify_module = _release_modules()
    source_sha = "a" * 40
    source_epoch = 1_700_000_000
    hosted = verify_module.hosted_build_matrix(source_sha, source_epoch)
    reviewed = core.build_matrix("feature-candidate")

    assert [item["spec_id"] for item in hosted["tasks"]] == REAL_SPEC_IDS
    assert hosted["github_matrix"] == {
        "include": [{"spec_id": spec_id} for spec_id in REAL_SPEC_IDS]
    }
    assert [item["task_sha256"] for item in hosted["tasks"]] == [
        item["task_sha256"] for item in reviewed["tasks"]
    ]
    assert {
        item["runner"] for item in hosted["tasks"] if item["cpu_arch"] == "amd64"
    } == {"ubuntu-24.04"}
    assert {
        item["runner"] for item in hosted["tasks"] if item["cpu_arch"] == "arm64"
    } == {"ubuntu-24.04-arm"}
    assert {item["docker_target"] for item in hosted["tasks"]} == {
        "wheel-cuda",
        "wheel-ascend",
    }
    assert all(
        item["builder_coordinate"]
        == next(
            task for task in reviewed["tasks"] if task["spec_id"] == item["spec_id"]
        )["builder"]["root"]["repository"]
        + "@"
        + next(
            task for task in reviewed["tasks"] if task["spec_id"] == item["spec_id"]
        )["builder"]["root"]["manifest_digest"]
        for item in hosted["tasks"]
    )
    for item in hosted["tasks"]:
        assert item["wheel_artifact"] == f"ucm-wheel-{item['spec_id']}-{source_sha}"
        assert item["image_artifact"] == f"ucm-image-{item['spec_id']}-{source_sha}"
        assert item["build_args"]["UCM_RELEASE_BUILD_KEY"] == item["task_sha256"]
        assert item["build_args"]["SOURCE_DATE_EPOCH"] == str(source_epoch)
        expected_pyyaml = {
            "amd64": {
                "PYYAML_VERSION": "6.0.2",
                "PYYAML_FILENAME": "PyYAML-6.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                "PYYAML_SHA256": "sha256:80bab7bfc629882493af4aa31a4cfa43a4c57c83813253626916b8c7ada83476",
            },
            "arm64": {
                "PYYAML_VERSION": "6.0.2",
                "PYYAML_FILENAME": "PyYAML-6.0.2-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
                "PYYAML_SHA256": "sha256:1f71ea527786de97d1a0cc0eacd1defc0985dcf6b3f17bb77dcfc8c34bec4dc5",
            },
        }[item["cpu_arch"]]
        assert {
            key: item["build_args"][key] for key in expected_pyyaml
        } == expected_pyyaml
        expected_cmake = {
            "amd64": {
                "CMAKE_VERSION": "3.31.6",
                "CMAKE_FILENAME": "cmake-3.31.6-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                "CMAKE_SHA256": "sha256:1c8b05df0602365da91ee6a3336fe57525b137706c4ab5675498f662ae1dbcec",
            },
            "arm64": {
                "CMAKE_VERSION": "3.31.6",
                "CMAKE_FILENAME": "cmake-3.31.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
                "CMAKE_SHA256": "sha256:42d9883b8958da285d53d5f69d40d9650c2d1bcf922d82b3ebdceb2b3a7d4521",
            },
        }[item["cpu_arch"]]
        assert {
            key: item["build_args"][key] for key in expected_cmake
        } == expected_cmake

    dockerfile = (REPO_ROOT / ".github/release/docker/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "ARG PYYAML_VERSION" in dockerfile
    assert '"PyYAML==${PYYAML_VERSION}"' in dockerfile
    assert 'check_wheel "${PYYAML_FILENAME}" "${PYYAML_SHA256}"' in dockerfile
    assert "PyYAML==${PYYAML_VERSION} --hash=${PYYAML_SHA256}" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile


def test_hosted_matrix_cli_writes_the_canonical_workflow_authority(
    tmp_path: Path,
) -> None:
    """Workflows consume one tested record instead of reconstructing task authority."""
    output = tmp_path / "hosted-matrix.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ucm_release",
            "core",
            "hosted-matrix",
            "--source-sha",
            "c" * 40,
            "--source-date-epoch",
            "1700000000",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(REPO_ROOT / ".github" / "release"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    stdout = json.loads(completed.stdout)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert stdout == written
    assert [item["spec_id"] for item in written["tasks"]] == REAL_SPEC_IDS
    assert output.read_bytes() == (
        json.dumps(written, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


def test_four_workflows_run_real_six_wheel_and_six_image_native_jobs() -> None:
    """The feature push must execute six native wheels and six install-only OCIs."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_jobs = _jobs(entry)
    assert _trigger(entry)["push"] == {
        "branches": ["feature/**"],
        "tags": ["v0.5.0rc1"],
    }
    wheel_job = entry_jobs["build-wheels-feature"]
    assert wheel_job["strategy"]["fail-fast"] is False
    assert wheel_job["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.matrix) }}"
    )
    assert wheel_job["uses"] == "./.github/workflows/_build-wheel.yml"
    assert wheel_job["with"] == {
        "source_sha": "${{ needs.plan.outputs.source_sha }}",
        "spec_id": "${{ matrix.spec_id }}",
    }

    wheel = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    wheel_build = _jobs(wheel)["build"]
    assert wheel_build["timeout-minutes"] == 180
    assert "ubuntu-24.04-arm" in str(wheel_build["runs-on"])
    wheel_text = "\n".join(_strings(wheel_build))
    for required in (
        "wheel context",
        "core hosted-matrix",
        '--target "${docker_target}"',
        "--output type=local,dest=out/wheel",
        "wheel inspect",
        "Free disk for immutable native builder",
        "available_gib",
        "60",
    ):
        assert required in wheel_text
    assert "wheel fixture-build" not in wheel_text

    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_jobs = _jobs(images)
    image_matrix = image_jobs["build-images-feature"]
    assert image_matrix["strategy"]["fail-fast"] is False
    assert image_matrix["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.matrix) }}"
    )
    assert image_matrix["uses"] == "./.github/workflows/_build-image.yml"
    assert "feature-barrier" in image_jobs
    barrier = image_jobs["feature-barrier"]
    assert set(barrier["needs"]) == {"plan", "build-images-feature"}
    assert "always()" in str(barrier["if"])
    assert "needs.build-images-feature.result" in "\n".join(_strings(barrier))

    image_workflow = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    image_build = _jobs(image_workflow)["build"]
    assert image_build["timeout-minutes"] == 210
    assert "ubuntu-24.04-arm" in str(image_build["runs-on"])
    image_text = "\n".join(_strings(image_build))
    for required in (
        "image real-authorities",
        "image base-record-real",
        "crane config",
        "image prepare-real",
        "--target runtime-real",
        "UCM_RECIPE_SHA256",
        "manifest:io.ucm.release.recipe-sha256",
        "manifest:io.ucm.release.task-sha256",
        "rewrite-timestamp=true",
        "image verify",
        "rm -f out/image.oci.tar",
    ):
        assert required in image_text
    assert "crane blob" not in image_text
    assert "--push" not in image_text


def test_full_oci_delivery_opt_in_is_resolved_then_explicitly_forwarded() -> None:
    """Only the entry resolver output may reach the full-archive upload step."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_triggers = _trigger(entry)
    assert entry_triggers["push"] == {
        "branches": ["feature/**"],
        "tags": ["v0.5.0rc1"],
    }
    dispatch = entry_triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    dispatch_inputs = dispatch.get("inputs")
    assert isinstance(dispatch_inputs, dict)
    assert set(dispatch_inputs) == {"deliver_full_oci"}
    assert dispatch_inputs["deliver_full_oci"] == {
        "description": "Upload complete feature OCI archives for this attempt",
        "type": "boolean",
        "required": False,
        "default": False,
    }

    entry_jobs = _jobs(entry)
    plan_outputs = entry_jobs["plan"]["outputs"]
    assert plan_outputs["deliver_full_oci"] == (
        "${{ steps.plan.outputs.deliver_full_oci }}"
    )
    plan_steps = {str(step.get("name")): step for step in _steps(entry_jobs["plan"])}
    resolver = plan_steps["Route only the feature lane or exact protected tag"]
    assert resolver["env"] == {
        "EVENT_NAME": "${{ github.event_name }}",
        "REPOSITORY": "${{ github.repository }}",
        "REF": "${{ github.ref }}",
        "REF_PROTECTED": "${{ github.ref_protected }}",
        "HEAD_COMMIT_MESSAGE": "${{ github.event.head_commit.message }}",
        "MANUAL_DELIVERY": "${{ inputs.deliver_full_oci }}",
    }
    assert "[ucm-deliver-full-oci]" in str(resolver["run"])
    assert "release invocation is outside feature/protected authority" in str(
        resolver["run"]
    )

    image_call = entry_jobs["reconcile-images-feature"]["with"]
    assert image_call == {
        "source_sha": "${{ needs.plan.outputs.source_sha }}",
        "deliver_full_oci": "${{ needs.plan.outputs.deliver_full_oci == 'true' }}",
    }

    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_inputs = _trigger(images)["workflow_call"]["inputs"]
    assert image_inputs["deliver_full_oci"] == {
        "type": "boolean",
        "required": True,
    }
    assert _jobs(images)["build-images-feature"]["with"]["deliver_full_oci"] == (
        "${{ inputs.deliver_full_oci }}"
    )

    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    unit_inputs = _trigger(image)["workflow_call"]["inputs"]
    assert unit_inputs["deliver_full_oci"] == {
        "type": "boolean",
        "required": True,
    }
    steps = _steps(_jobs(image)["build"])
    named_steps = {str(step.get("name")): step for step in steps}
    for step_name in (
        "Preserve full OCI archive for manual delivery",
        "Stage verified full OCI artifact",
    ):
        condition = str(named_steps[step_name]["if"])
        assert "inputs.deliver_full_oci" in condition
        assert "refs/tags/v0.5.0rc1" in condition
    assert named_steps["Upload manually requested full OCI artifact"]["if"] == (
        "${{ inputs.deliver_full_oci }}"
    )

    full_upload = named_steps["Upload manually requested full OCI artifact"]
    assert str(full_upload["uses"]).startswith("actions/upload-artifact@")
    assert full_upload["with"] == {
        "name": "ucm-oci-${{ inputs.spec_id }}-${{ inputs.source_sha }}-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}",
        "path": "out/full-oci-artifact/",
        "if-no-files-found": "error",
        "compression-level": 0,
        "overwrite": False,
        "retention-days": 1,
    }
    compact_upload = next(
        upload
        for upload in _artifact_uploads(image)
        if "steps.task.outputs.image_artifact" in str(upload["with"]["name"])
    )
    assert "if" not in compact_upload
    assert compact_upload["with"]["name"] == (
        "${{ steps.task.outputs.image_artifact }}"
    )
    aggregate_downloads = "\n".join(_strings(_jobs(images)["aggregate-feature"]))
    assert "ucm-image-*" in aggregate_downloads
    assert "ucm-oci-*" not in aggregate_downloads


@pytest.mark.parametrize(
    ("failed_attempts", "expected_returncode", "expected_attempts", "cleaned"),
    [(1, 0, 2, False), (99, 2, 3, True)],
)
def test_buildx_bootstrap_retry_is_bounded_and_cleans_terminal_failure(
    tmp_path: Path,
    failed_attempts: int,
    expected_returncode: int,
    expected_attempts: int,
    cleaned: bool,
) -> None:
    """A transient BuildKit pull retries, while three failures remove the builder."""
    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    install = next(
        step
        for step in _steps(_jobs(image)["build"])
        if step.get("name") == "Install checksum-pinned Buildx"
    )
    command = str(install["run"])
    retry = command[command.index("bootstrap_ok=false") :]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "attempts"
    cleanup = tmp_path / "cleanup"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == "buildx inspect ucm-release-builder --bootstrap" ]]; then
  count=0
  if [[ -f "${COUNT_FILE}" ]]; then count="$(cat "${COUNT_FILE}")"; fi
  count="$((count + 1))"
  printf '%s' "${count}" >"${COUNT_FILE}"
  if (( count <= FAIL_COUNT )); then
    echo "context deadline exceeded" >&2
    exit 1
  fi
  echo "bootstrap ready"
  exit 0
fi
if [[ "$*" == "buildx rm --force ucm-release-builder" ]]; then
  : >"${CLEANUP_FILE}"
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail; mkdir -p out; " + retry],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COUNT_FILE": str(counter),
            "CLEANUP_FILE": str(cleanup),
            "FAIL_COUNT": str(failed_attempts),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_returncode, result.stderr
    assert int(counter.read_text(encoding="utf-8")) == expected_attempts
    assert cleanup.exists() is cleaned
    assert len(list((tmp_path / "out").glob("buildx-bootstrap-attempt-*.log"))) == (
        expected_attempts
    )


@pytest.mark.parametrize(
    ("event_name", "head_commit_message", "manual_opt_in", "expected"),
    [
        ("push", "ordinary feature change", "false", "false"),
        (
            "push",
            "release candidate [ucm-deliver-full-oci]",
            "false",
            "true",
        ),
        ("workflow_dispatch", "", "true", "true"),
    ],
)
def test_full_oci_delivery_resolver_routes_each_explicit_opt_in(
    tmp_path: Path,
    event_name: str,
    head_commit_message: str,
    manual_opt_in: str,
    expected: str,
) -> None:
    """Run the real resolver for ordinary push, marker push, and manual true."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    steps = {str(step.get("name")): step for step in _steps(_jobs(entry)["plan"])}
    assert "Route only the feature lane or exact protected tag" in steps
    resolver = steps["Route only the feature lane or exact protected tag"]
    resolver_script = str(resolver["run"]).split("source_date_epoch=", 1)[0]
    resolver_script = re.sub(
        r"(?m)^\s*PYTHONPATH=.*feature-preflight\.json\s*$", ":", resolver_script
    )
    resolver_script += '\necho "deliver_full_oci=${delivery}" >>"${GITHUB_OUTPUT}"\n'
    github_output = tmp_path / "github-output"
    completed = subprocess.run(
        ["bash", "-c", resolver_script],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "EVENT_NAME": event_name,
            "REPOSITORY": "SuperMarioYL/unified-cache-management",
            "REF": "refs/heads/feature/cicd",
            "REF_PROTECTED": "false",
            "HEAD_COMMIT_MESSAGE": head_commit_message,
            "MANUAL_DELIVERY": manual_opt_in,
            "GITHUB_SHA": _git("rev-parse", "HEAD").strip(),
            "GITHUB_OUTPUT": str(github_output),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert github_output.read_text(encoding="utf-8") == (
        f"deliver_full_oci={expected}\n"
    )


def test_full_oci_preserve_and_stage_shells_keep_verified_bytes_flat(
    tmp_path: Path,
) -> None:
    """A hardlink must survive verifier unlink and stage three verified flat files."""
    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    named_steps = {
        str(step.get("name")): step for step in _steps(_jobs(image)["build"])
    }
    required = {
        "Preserve full OCI archive for manual delivery",
        "Stage verified full OCI artifact",
    }
    assert required <= set(named_steps), "full OCI preserve/stage steps are missing"

    out = tmp_path / "out"
    out.mkdir()
    archive = out / "image.oci.tar"
    archive_bytes = b"complete OCI archive\x00with layer bytes\n"
    archive.write_bytes(archive_bytes)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    (out / "image-archive.sha256").write_text(
        f"{digest}  out/image.oci.tar\n", encoding="utf-8"
    )

    preserved = subprocess.run(
        [
            "bash",
            "-c",
            str(named_steps["Preserve full OCI archive for manual delivery"]["run"]),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert preserved.returncode == 0, preserved.stderr
    retained = out / "full-oci-preserved" / "image.oci.tar"
    assert retained.read_bytes() == archive_bytes
    assert retained.stat().st_dev == archive.stat().st_dev
    assert retained.stat().st_ino == archive.stat().st_ino
    retained_device = retained.stat().st_dev
    retained_inode = retained.stat().st_ino

    evidence = out / "oci-evidence"
    evidence.mkdir()
    _write_canonical(
        evidence / "closure.json",
        {
            "archive_sha256": f"sha256:{digest}",
            "archive_size": len(archive_bytes),
        },
    )
    image_result = b'{"verified":true}\n'
    (out / "image-result.json").write_bytes(image_result)
    archive.unlink()  # The real verifier owns and removes this original path.
    assert not archive.exists() and retained.is_file()

    staged = subprocess.run(
        ["bash", "-c", str(named_steps["Stage verified full OCI artifact"]["run"])],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert staged.returncode == 0, staged.stderr
    delivery = out / "full-oci-artifact"
    assert {path.name for path in delivery.iterdir()} == {
        "image.oci.tar",
        "image-archive.sha256",
        "image-result.json",
    }
    delivered_archive = delivery / "image.oci.tar"
    assert delivered_archive.read_bytes() == archive_bytes
    assert delivered_archive.stat().st_dev == retained_device
    assert delivered_archive.stat().st_ino == retained_inode
    assert not retained.exists()
    assert (delivery / "image-archive.sha256").read_text(encoding="utf-8") == (
        f"{digest}  image.oci.tar\n"
    )
    assert (delivery / "image-result.json").read_bytes() == image_result


@pytest.mark.parametrize("mutation", ["sha256", "size"])
def test_full_oci_stage_shell_rejects_compact_closure_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    """Neither altered archive identity field may reach upload staging."""
    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    named_steps = {
        str(step.get("name")): step for step in _steps(_jobs(image)["build"])
    }
    assert "Stage verified full OCI artifact" in named_steps
    out = tmp_path / "out"
    retained_dir = out / "full-oci-preserved"
    retained_dir.mkdir(parents=True)
    archive_bytes = b"complete OCI archive\n"
    (retained_dir / "image.oci.tar").write_bytes(archive_bytes)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    closure = {
        "archive_sha256": f"sha256:{digest}",
        "archive_size": len(archive_bytes),
    }
    if mutation == "sha256":
        closure["archive_sha256"] = "sha256:" + "0" * 64
    else:
        closure["archive_size"] = len(archive_bytes) + 1
    evidence = out / "oci-evidence"
    evidence.mkdir()
    _write_canonical(evidence / "closure.json", closure)
    (out / "image-result.json").write_text("{}\n", encoding="utf-8")

    staged = subprocess.run(
        ["bash", "-c", str(named_steps["Stage verified full OCI artifact"]["run"])],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert staged.returncode != 0
    assert not (out / "full-oci-artifact").exists()


@pytest.mark.parametrize("success_on", [3, 0])
def test_native_wheel_build_retries_on_one_builder_and_cleans_partial_output(
    tmp_path: Path, success_on: int
) -> None:
    """Transient Buildx failures retain logs but never leak a partial wheel tree."""
    workflow = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    steps = _steps(_jobs(workflow)["build"])
    install = next(
        step for step in steps if step.get("name") == "Install checksum-pinned Buildx"
    )
    build = next(
        step
        for step in steps
        if step.get("name") == "Build, seal, and export the real native wheel"
    )
    install_command = str(install["run"])
    build_command = str(build["run"])

    assert install_command.count("docker buildx create --name ucm-release-builder") == 1
    assert "docker buildx create" not in build_command
    assert "--builder ucm-release-builder" in build_command
    assert "max_build_attempts=3" in build_command
    assert 'attempt_log="out/build-attempt-${build_attempt}.log"' in build_command
    assert 'tee -a "${attempt_log}" out/build.log' in build_command
    assert build_command.count("rm -rf out/wheel") >= 2

    hosted_task = {
        "docker_target": "wheel-cuda",
        "platform": "linux/amd64",
        "build_args": {"SOURCE_DATE_EPOCH": "0"},
    }
    (tmp_path / "out/source-context").mkdir(parents=True)
    (tmp_path / "out/hosted-task.json").write_text(
        json.dumps(hosted_task), encoding="utf-8"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
count="$(cat "${UCM_TEST_DOCKER_COUNT}" 2>/dev/null || printf 0)"
count="$((count + 1))"
printf '%s\n' "${count}" >"${UCM_TEST_DOCKER_COUNT}"
printf '%s\n' "$*" >>"${UCM_TEST_DOCKER_ARGS}"
if [[ "${UCM_TEST_SUCCESS_ON}" -gt 0 && "${count}" -eq "${UCM_TEST_SUCCESS_ON}" ]]; then
  test ! -e out/wheel/partial.whl
  printf 'sealed\n' >out/wheel/final.whl
  printf 'success attempt %s\n' "${count}"
  exit 0
fi
printf 'partial\n' >out/wheel/partial.whl
printf 'unexpected EOF attempt %s\n' "${count}" >&2
exit 19
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    counter = tmp_path / "docker-count.txt"
    arguments = tmp_path / "docker-args.txt"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "UCM_TEST_DOCKER_COUNT": str(counter),
        "UCM_TEST_DOCKER_ARGS": str(arguments),
        "UCM_TEST_SUCCESS_ON": str(success_on),
    }

    completed = subprocess.run(
        ["bash", "-c", build_command],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    expected_returncode = 0 if success_on else 19
    assert completed.returncode == expected_returncode, completed.stderr
    assert counter.read_text(encoding="utf-8").strip() == "3"
    invocations = arguments.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 3
    assert all(
        invocation.startswith("buildx build --builder ucm-release-builder")
        for invocation in invocations
    )
    merged_log = (tmp_path / "out/build.log").read_text(encoding="utf-8")
    assert merged_log.count("unexpected EOF attempt") == (2 if success_on else 3)
    for attempt in range(1, 4):
        attempt_log = tmp_path / f"out/build-attempt-{attempt}.log"
        assert attempt_log.is_file()
        assert f"attempt {attempt}" in attempt_log.read_text(encoding="utf-8")
    if success_on:
        assert (tmp_path / "out/wheel/final.whl").read_text(encoding="utf-8") == (
            "sealed\n"
        )
        assert "success attempt 3" in merged_log
    else:
        assert not (tmp_path / "out/wheel").exists()


def test_real_family_plans_are_three_sorted_dual_arch_candidates_and_reconcile_zero() -> (
    None
):
    """Missing, duplicated, or cross-family members cannot produce a green aggregate."""
    core, _, verify_module = _release_modules()
    source_sha = "b" * 40
    matrix = core.build_matrix("feature-candidate")
    results = []
    for index, task in enumerate(matrix["tasks"]):
        results.append(
            {
                "candidate_kind": "real-candidate",
                "fixture_only": False,
                "unpublished": True,
                "publication_attempted": False,
                "status": "real-verified-unpublished",
                "spec_id": task["spec_id"],
                "family_id": task["profile_id"],
                "profile_id": task["profile_id"],
                "target_platform": task["platform"],
                "target_repository": task["target_repository"],
                "target_tag": task["target_tag"],
                "task_key": task["task_sha256"],
                "build_key_sha256": "sha256:" + f"{index + 1:064x}",
                "result_sha256": "sha256:" + f"{index + 11:064x}",
                "content_identity_sha256": "sha256:" + f"{index + 21:064x}",
                "source": {"commit": source_sha},
                "oci": {
                    "digest": "sha256:" + f"{index + 31:064x}",
                    "platform": task["platform"],
                    "published": False,
                },
            }
        )

    first = verify_module.build_real_family_plans(results, source_sha=source_sha)
    second = verify_module.build_real_family_plans(
        list(reversed(results)), source_sha=source_sha
    )

    assert first == second
    assert [item["family_id"] for item in first["families"]] == [
        "cann900-a2",
        "cann900-a3",
        "cuda130",
    ]
    assert all(
        [member["platform"] for member in family["members"]]
        == ["linux/amd64", "linux/arm64"]
        for family in first["families"]
    )
    assert first["second_reconcile"] == {
        "decision": "already-present",
        "task_count": 0,
        "tasks": [],
    }
    with pytest.raises(ValueError, match="exactly six"):
        verify_module.build_real_family_plans(results[:-1], source_sha=source_sha)


def test_existing_cpp_changes_are_explicitly_forbidden_from_the_stage() -> None:
    """Keep the three pre-existing C++ edits visible but outside this release commit."""
    assert all((REPO_ROOT / path).is_file() for path in FORBIDDEN_STAGED_PATHS)
    staged = set(filter(None, _git("diff", "--cached", "--name-only").splitlines()))
    assert not staged & FORBIDDEN_STAGED_PATHS, json.dumps(
        {"forbidden_staged_paths": sorted(staged & FORBIDDEN_STAGED_PATHS)}, indent=2
    )


def test_workflow_set_rejects_an_arbitrary_publish_workflow(tmp_path: Path) -> None:
    """The exact v2 lane is accepted, but an unrecognised workflow is not."""
    expected = (
        EXPECTED_RELEASE_WORKFLOWS
        | ALLOWED_NON_RELEASE_WORKFLOWS
        | V2_DRY_RUN_WORKFLOWS
        | PRODUCTION_WORKFLOWS
    )
    for filename in expected:
        (tmp_path / filename).write_text("name: allowed\n")

    assert _workflow_set_violations(tmp_path) == []

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


def test_release_workflow_topology_runs_split_files_at_the_pushed_sha() -> None:
    """The feature push must reach six wheels, Chart, six images, and aggregate."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    triggers = _trigger(entry)
    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("branches") == ["feature/**"]
    assert push.get("tags") == ["v0.5.0rc1"]
    assert set(triggers) == {"push", "workflow_dispatch"}

    entry_jobs = _jobs(entry)
    assert entry_jobs["build-wheels-feature"]["permissions"] == {"contents": "read"}
    assert entry_jobs["build-wheels-protected"]["permissions"] == {"contents": "read"}
    local_calls = {
        str(job["uses"])
        for job in entry_jobs.values()
        if isinstance(job.get("uses"), str)
    }
    assert local_calls == {
        "./.github/workflows/_build-wheel.yml",
        "./.github/workflows/release-vllm-images-protected.yml",
        "./.github/workflows/release-vllm-images.yml",
    }
    assert all("@" not in reference for reference in local_calls)
    assert any("chart package" in value for value in _strings(entry))
    assert "loop aggregate-real" in "\n".join(_strings(entry))
    assert set(entry_jobs["feature-barrier"]["needs"]) == {
        "plan",
        "build-wheels-feature",
        "package-chart",
        "reconcile-images-feature",
    }

    image_release = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_triggers = _trigger(image_release)
    assert set(image_triggers) == {"workflow_call"}
    image_calls = {
        str(job["uses"])
        for job in _jobs(image_release).values()
        if isinstance(job.get("uses"), str)
    }
    assert image_calls == {"./.github/workflows/_build-image.yml"}
    image_jobs = _jobs(image_release)
    assert image_jobs["build-images-feature"]["uses"] == (
        "./.github/workflows/_build-image.yml"
    )
    assert set(image_jobs["aggregate-feature"]["needs"]) == {
        "plan",
        "build-images-feature",
        "feature-barrier",
    }
    protected_release = _load_workflow(
        WORKFLOW_DIR / "release-vllm-images-protected.yml"
    )
    assert _jobs(protected_release)["build-images-protected"]["uses"] == (
        "./.github/workflows/_publish-image-member.yml"
    )
    publisher = _load_workflow(WORKFLOW_DIR / "_publish-image-member.yml")
    assert _jobs(publisher)["build"]["uses"] == ("./.github/workflows/_build-image.yml")


def test_reusable_workflow_inputs_outputs_and_artifacts_are_exact() -> None:
    """Reusable boundaries must carry immutable identities, not implicit state."""
    wheel = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    wheel_call = _trigger(wheel)["workflow_call"]
    assert isinstance(wheel_call, dict)
    assert set(wheel_call.get("inputs", {})) == {"source_sha", "spec_id"}
    assert {
        "wheel_artifact",
        "wheel_sha256",
        "inspection_sha256",
    } <= set(wheel_call.get("outputs", {}))
    wheel_text = "\n".join(_strings(wheel))
    assert "wheel context" in wheel_text
    assert "wheel inspect" in wheel_text
    assert "builder-candidate" in wheel_text
    assert "wheel fixture-build" not in wheel_text

    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    image_call = _trigger(image)["workflow_call"]
    assert isinstance(image_call, dict)
    assert set(image_call.get("inputs", {})) == {
        "source_sha",
        "spec_id",
        "deliver_full_oci",
    }
    assert {"image_artifact", "image_result_sha256", "oci_digest"} <= set(
        image_call.get("outputs", {})
    )
    image_text = "\n".join(_strings(image)).lower()
    assert "type=oci" in image_text
    assert "image prepare-real" in image_text and "image verify" in image_text
    assert "--push" not in image_text
    assert not re.search(r"\b(?:cmake|ninja|gcc|g\+\+|clang|pip wheel)\b", image_text)

    for filename in EXPECTED_RELEASE_WORKFLOWS:
        document = _load_workflow(WORKFLOW_DIR / filename)
        uploads = _artifact_uploads(document)
        for upload in uploads:
            inputs = upload.get("with")
            assert isinstance(inputs, dict)
            retention = inputs.get("retention-days")
            if upload.get("if") == "${{ inputs.deliver_full_oci }}":
                assert inputs.get("retention-days") == 1
                assert inputs.get("compression-level") == 0
            elif isinstance(retention, str):
                for fragment in (
                    "github.event_name == 'push'",
                    "SuperMarioYL/unified-cache-management",
                    "refs/tags/v0.5.0rc1",
                    "github.ref_protected == true",
                    "90 || 3",
                ):
                    assert fragment in retention
            else:
                assert retention in {3, 90}


@pytest.mark.parametrize(
    ("filename", "valid_environment", "invalid_environment"),
    [
        (
            "_build-wheel.yml",
            {
                "SOURCE_SHA": "a" * 40,
                "SPEC_ID": "cuda130-amd64",
            },
            [
                {"SOURCE_SHA": "refs/heads/feature/cicd"},
                {"SPEC_ID": ""},
                {"SPEC_ID": "cuda130-riscv64"},
            ],
        ),
        (
            "_build-image.yml",
            {
                "SOURCE_SHA": "b" * 40,
                "SPEC_ID": "cann900-a3-arm64",
            },
            [
                {"SOURCE_SHA": "refs/heads/feature/cicd"},
                {"SPEC_ID": ""},
                {"SPEC_ID": "cann900-a5-arm64"},
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
    """Only fork feature pushes execute; image calls accept one exact source SHA."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    plan_text = "\n".join(_strings(_jobs(entry)["plan"]))
    for fragment in (
        "SuperMarioYL/unified-cache-management",
        "refs/heads/feature/",
        "refs/tags/v0.5.0rc1",
        "REF_PROTECTED",
        "release invocation is outside feature/protected authority",
        "exit 2",
    ):
        assert fragment in plan_text

    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_inputs = _trigger(images)["workflow_call"]["inputs"]
    assert set(image_inputs) == {"source_sha", "deliver_full_oci"}
    assert image_inputs["source_sha"]["required"] is True
    assert image_inputs["deliver_full_oci"] == {
        "type": "boolean",
        "required": True,
    }
    contract = next(
        step
        for step in _steps(_jobs(images)["plan"])
        if step.get("name") == "Fail closed and project the exact six feature members"
    )
    assert "inputs.source_sha" in str(contract["env"]["SOURCE_SHA"])
    assert "exit 2" in str(contract["run"])


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


def test_candidate_evidence_binds_six_real_members_three_plans_and_chart() -> None:
    """The deterministic payload binds every real hosted output without publication."""
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
        "image_result_sha256",
        "manifest_digest",
        "build_key_sha256",
        "families",
        "candidate_inventory",
        "second_reconcile",
        "release_tree_sha256",
        "publication",
    }
    assert not {field for field in required_fields if field not in combined}
    assert "loop aggregate-real" in image_text
    assert "loop aggregate-real" in entry_text
    assert '"wheels": wheel_summaries' in policy_text
    assert '"images": image_summaries' in policy_text
    assert '"families": planned["families"]' in policy_text
    assert "len(task_records) != 6" in policy_text
    assert "len(image_results) != 6" in policy_text
    assert "--chart-result input/chart/chart-result.json" in entry_text


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
    for filename in (
        "image-result.json",
        "image-recipe.json",
        "image-authority.json",
        "image-prepare-result.json",
        "buildkit-metadata.json",
    ):
        assert filename in image_text
    assert "--wheel-dir input/wheels" in reconcile_text
    assert "--image-dir input/images" in reconcile_text
    assert "--chart-result input/chart/chart-result.json" in entry_text

    chart_uploads = [
        upload
        for upload in _artifact_uploads(entry)
        if str(upload.get("with", {}).get("path", "")).strip() == "out/artifact/"
    ]
    assert len(chart_uploads) == 2
    package_job = _jobs(entry)["package-chart"]
    package_commands = "\n".join(
        str(step.get("run", "")) for step in _steps(package_job)
    )
    assert "out/artifact" in package_commands

    # Simulate upload-artifact v4's least-common-ancestor preservation and download.
    staging = tmp_path / "out" / "artifact"
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
    assert "real-image-loop-evidence.json" in "\n".join(_strings(images))
    assert "protected-publication.json" in entry_text
    assert "out/image-result.json" in image_text
    assert '"status": "blocked"' in policy_text
    assert '"attempted": False' in policy_text


def test_wheel_artifact_is_flat_for_same_run_image_and_aggregate_consumers(
    tmp_path: Path,
) -> None:
    """upload-artifact's LCA must not hide wheel records under a wheel/ subdir."""
    wheel = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    upload = _artifact_uploads(wheel)[0]
    assert str(upload["with"]["path"]).strip() == "out/wheel-artifact/"
    commands = "\n".join(_strings(_jobs(wheel)["build"]))
    for filename in (
        "*.whl",
        "wheel-inspection.json",
        "wheel-seal.json",
        "hosted-task.json",
        "source-context.json",
    ):
        assert filename in commands

    staging = tmp_path / "out" / "wheel-artifact"
    staging.mkdir(parents=True)
    for filename in (
        "uc_manager.whl",
        "wheel-inspection.json",
        "wheel-seal.json",
        "hosted-task.json",
        "source-context.json",
    ):
        (staging / filename).write_text("artifact\n", encoding="utf-8")
    downloaded = tmp_path / "input" / "wheel"
    downloaded.mkdir(parents=True)
    for source in staging.iterdir():
        (downloaded / source.name).write_bytes(source.read_bytes())
    assert len(list(downloaded.glob("*.whl"))) == 1
    assert (downloaded / "wheel-inspection.json").is_file()
    assert (downloaded / "source-context.json").is_file()


def test_native_wheel_and_image_jobs_clean_disk_and_gate_sixty_gib_before_checkout() -> (
    None
):
    """Both multi-gigabyte hosted builds need recoverable pre-checkout disk evidence."""
    for filename in ("_build-wheel.yml", "_build-image.yml"):
        workflow = _load_workflow(WORKFLOW_DIR / filename)
        steps = _steps(_jobs(workflow)["build"])
        checkout_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        cleanup_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("jlumbroso/free-disk-space@")
        )
        assert cleanup_index < checkout_index
        assert steps[cleanup_index]["uses"] == (
            "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be"
        )
        precheckout = "\n".join(_strings(steps[:checkout_index]))
        assert "RUNNER_TEMP" in precheckout
        assert "available_gib" in precheckout
        assert "60" in precheckout


def test_workflows_only_orchestrate_tested_cli_and_real_runs_full_closure() -> None:
    """Rules stay in Python while feature push reaches the reviewed real closure."""
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
    assert "core hosted-matrix" in wheel_text and "wheel context" in wheel_text
    assert "wheel fixture-build" not in wheel_text
    assert "image prepare-real" in image_text and "image verify" in image_text
    assert "loop aggregate-real" in reconcile_text
    assert "loop aggregate-real" in entry_text
    assert "standalone-wheel:" not in reconcile_text
    assert "image real-authorities" in image_text
    assert "CRANE_VERSION: v0.20.3" in image_text
    assert "buildx_linux_sha256" in image_text
    assert BUILDX_VERSION not in image_text
    assert BUILDX_LINUX_SHA256["amd64"] not in image_text
    assert BUILDX_LINUX_SHA256["arm64"] not in image_text
    assert BUILDKIT_IMAGE not in image_text
    assert re.search(r"CRANE_LINUX_AMD64_SHA256: [0-9a-f]{64}", image_text)
    assert re.search(r"CRANE_LINUX_ARM64_SHA256: [0-9a-f]{64}", image_text)
    assert "crane manifest" in image_text and "crane config" in image_text
    assert "crane blob" not in image_text
    assert 'export SOURCE_DATE_EPOCH="$(' not in image_text
    assert 'SOURCE_DATE_EPOCH="$(' in image_text
    assert "export SOURCE_DATE_EPOCH" in image_text


def test_release_toolchains_are_immutable_and_checksum_verified() -> None:
    """Buildx, BuildKit, Dockerfile frontend, and Helm are all byte identities."""
    workflows = {
        path.name: _load_workflow(path)
        for path in _workflow_paths(WORKFLOW_DIR)
        if path.name not in PRODUCTION_WORKFLOWS
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
        if mutation == "buildx-authority":
            step = next(
                step
                for job in _jobs(workflows["_build-image.yml"]).values()
                for step in _steps(job)
                if "image real-authorities" in str(step.get("run", ""))
            )
            step["run"] = str(step["run"]).replace(
                "image real-authorities", "image base-authority"
            )
        else:
            step = next(
                step
                for job in _jobs(workflows["_build-image.yml"]).values()
                for step in _steps(job)
                if step.get("name") == "Install checksum-pinned Buildx"
            )
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
        install = next(
            step for step in steps if step.get("name") == "Install checksum-pinned Helm"
        )
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

    assert (
        "type=oci,dest=out/image.oci.tar,oci-mediatypes=true,rewrite-timestamp=true"
        in workflow
    )
    assert 'export SOURCE_DATE_EPOCH="$(python' not in workflow
    assert 'SOURCE_DATE_EPOCH="$(python' in workflow
    assert "export SOURCE_DATE_EPOCH" in workflow
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
        ("_publish-image-member.yml", "publish-member"),
        ("release-ucm.yml", "plan"),
        ("release-ucm.yml", "package-chart"),
        ("release-ucm.yml", "aggregate-evidence"),
        ("release-ucm.yml", "prepare-release-draft"),
        ("release-ucm.yml", "anonymous-registry-readback"),
        ("release-ucm.yml", "publish-release"),
        ("release-ucm.yml", "anonymous-release-readback"),
        ("release-vllm-images.yml", "plan"),
        ("release-vllm-images.yml", "aggregate-feature"),
        ("release-vllm-images-protected.yml", "plan"),
        ("release-vllm-images-protected.yml", "aggregate-members"),
        ("release-vllm-images-protected.yml", "publish-indexes"),
        ("release-vllm-images-protected.yml", "authenticated-readback"),
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


def test_reusable_image_routers_split_feature_and_protected_authority() -> None:
    """The callable source is explicit and feature cannot inherit protected jobs."""
    feature = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    feature_jobs = _jobs(feature)
    assert set(_trigger(feature)) == {"workflow_call"}
    assert set(_trigger(feature)["workflow_call"]["inputs"]) == {
        "source_sha",
        "deliver_full_oci",
    }
    feature_plan = "\n".join(_strings(feature_jobs["plan"]))
    assert "inputs.source_sha" in feature_plan
    for authority in (
        "github.event_name",
        "github.repository",
        "github.ref",
        "refs/heads/feature/",
    ):
        assert authority in feature_plan
    assert "github.ref_protected" not in feature_plan
    assert "standalone-wheel" not in feature_jobs

    protected = _load_workflow(WORKFLOW_DIR / "release-vllm-images-protected.yml")
    assert set(_trigger(protected)["workflow_call"]["inputs"]) == {"source_sha"}
    protected_plan = "\n".join(_strings(_jobs(protected)["plan"]))
    for authority in (
        "github.event_name",
        "github.repository",
        "github.ref",
        "github.ref_protected",
        "refs/tags/v0.5.0rc1",
    ):
        assert authority in protected_plan


def test_callable_image_chain_overrides_skipped_standalone_ancestor() -> None:
    """Every image aggregate dependency has explicit success and cancellation gates."""
    feature_jobs = _jobs(_load_workflow(WORKFLOW_DIR / "release-vllm-images.yml"))
    protected_jobs = _jobs(
        _load_workflow(WORKFLOW_DIR / "release-vllm-images-protected.yml")
    )
    dependencies = {
        "build-images-feature": ["plan"],
        "aggregate-feature": ["plan", "build-images-feature", "feature-barrier"],
    }
    protected_dependencies = {
        "build-images-protected": ["plan"],
        "aggregate-members": [
            "plan",
            "build-images-protected",
            "member-barrier",
        ],
        "authenticated-readback": [
            "aggregate-members",
            "publish-indexes",
            "index-barrier",
        ],
    }
    for job_name, direct_needs in dependencies.items():
        condition = str(feature_jobs[job_name]["if"])
        assert "!cancelled()" in condition, job_name
        for dependency in direct_needs:
            assert f"needs.{dependency}.result == 'success'" in condition
    for job_name, direct_needs in protected_dependencies.items():
        condition = str(protected_jobs[job_name]["if"])
        assert "!cancelled()" in condition, job_name
        for dependency in direct_needs:
            assert f"needs.{dependency}.result == 'success'" in condition


def test_callable_image_chain_fails_closed_for_direct_needs_and_cancellation() -> None:
    """The always barrier converts failed, skipped, or cancelled matrix state to failure."""
    feature_jobs = _jobs(_load_workflow(WORKFLOW_DIR / "release-vllm-images.yml"))
    protected_jobs = _jobs(
        _load_workflow(WORKFLOW_DIR / "release-vllm-images-protected.yml")
    )
    barriers = (
        (
            feature_jobs,
            "feature-barrier",
            {"plan", "build-images-feature"},
            {"PLAN_RESULT", "IMAGE_RESULT"},
        ),
        (
            protected_jobs,
            "member-barrier",
            {"plan", "build-images-protected"},
            {"PLAN_RESULT", "MEMBERS_RESULT"},
        ),
        (
            protected_jobs,
            "index-barrier",
            {"plan", "aggregate-members", "publish-indexes"},
            {"MEMBERS_RESULT", "INDEXES_RESULT"},
        ),
    )
    for jobs, barrier_name, expected_needs, result_names in barriers:
        barrier = jobs[barrier_name]
        assert set(barrier["needs"]) == expected_needs
        assert "always()" in str(barrier["if"])
        command = "\n".join(_strings(barrier))
        assert result_names <= set(re.findall(r"[A-Z_]+_RESULT", command))
        for result in ("skipped", "failure", "cancelled"):
            assert result in command
        assert "exit 2" in command


def test_multi_output_steps_use_one_grouped_github_output_append() -> None:
    """ShellCheck SC2129: write each multi-value output file only once."""
    targets = {
        ("_build-wheel.yml", "build", "record"): 2,
        ("_build-image.yml", "build", "task"): 3,
        ("_build-image.yml", "build", "result"): 2,
        ("release-ucm.yml", "plan", "plan"): 5,
        ("release-vllm-images.yml", "plan", "plan"): 3,
        ("release-vllm-images-protected.yml", "plan", "plan"): 3,
    }
    for (filename, job_name, step_id), output_count in targets.items():
        document = _load_workflow(WORKFLOW_DIR / filename)
        step = next(
            item
            for item in _steps(_jobs(document)[job_name])
            if item.get("id") == step_id
        )
        command = str(step.get("run", ""))
        assert command.count('>>"${GITHUB_OUTPUT}"') == 1, (
            filename,
            job_name,
            step_id,
        )
        assert re.search(r"(?m)^\s*\{\s*$", command), (filename, step_id)
        assert re.search(r'(?m)^\s*\}\s*>>"\$\{GITHUB_OUTPUT\}"\s*$', command), (
            filename,
            step_id,
        )
        grouped = re.search(
            r'(?ms)^\s*\{\s*$\n(?P<body>.*?)^\s*\}\s*>>"\$\{GITHUB_OUTPUT\}"\s*$',
            command,
        )
        assert grouped is not None, (filename, step_id)
        assert (
            len(re.findall(r"(?m)^\s*echo ", grouped.group("body"))) == output_count
        ), (
            filename,
            step_id,
        )


def test_release_entry_router_fails_closed_inside_the_always_run_plan_job() -> None:
    """The plan job classifies only feature pushes or the exact protected tag."""
    document = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    plan = _jobs(document)["plan"]
    assert "if" not in plan
    command = "\n".join(_strings(plan))
    for fragment in (
        "EVENT_NAME",
        "REPOSITORY",
        "REF_PROTECTED",
        "SuperMarioYL/unified-cache-management",
        "refs/heads/feature/",
        "refs/tags/v0.5.0rc1",
        "release invocation is outside feature/protected authority",
        "exit 2",
    ):
        assert fragment in command
    assert "refs/tags/v*" not in command


def test_only_the_exact_protected_v_tag_can_enter_the_publication_route() -> None:
    """Foreign, unprotected, or wildcard tags fail in the plan before any writer."""
    document = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    assert _trigger(document)["push"]["tags"] == ["v0.5.0rc1"]
    plan_text = "\n".join(_strings(_jobs(document)["plan"]))
    for fragment in (
        '"${EVENT_NAME}" == push',
        '"${REPOSITORY}" == SuperMarioYL/unified-cache-management',
        '"${REF}" == refs/tags/v0.5.0rc1',
        '"${REF_PROTECTED}" == true',
    ):
        assert fragment in plan_text
    assert "refs/tags/v*" not in plan_text
    protected_jobs = {
        "reconcile-images-protected",
        "prepare-release-draft",
        "publish-release",
    }
    for job_name in protected_jobs:
        condition = str(_jobs(document)[job_name]["if"])
        assert "refs/tags/v0.5.0rc1" in condition
        assert "github.ref_protected == true" in condition


def test_production_and_unsupported_callable_lanes_fail_closed() -> None:
    """Only exact protected concrete writers receive an Environment or write token."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    assert "release invocation is outside feature/protected authority" in "\n".join(
        _strings(_jobs(entry)["plan"])
    )
    environments = {
        (filename, job_name)
        for filename in EXPECTED_RELEASE_WORKFLOWS
        for job_name, job in _jobs(_load_workflow(WORKFLOW_DIR / filename)).items()
        if "environment" in job
    }
    assert environments == {
        ("_publish-image-member.yml", "publish-member"),
        ("release-vllm-images-protected.yml", "publish-indexes"),
        ("release-ucm.yml", "prepare-release-draft"),
        ("release-ucm.yml", "publish-release"),
    }
    for filename in EXPECTED_RELEASE_WORKFLOWS:
        document = _load_workflow(WORKFLOW_DIR / filename)
        assert document["permissions"] == {"contents": "read"}
        assert "secrets." not in "\n".join(_strings(document)).lower()


def test_changed_workflows_pin_actions_and_keep_fork_jobs_read_only() -> None:
    """Action steps are immutable; the reviewed reusable controller is explicit."""
    permission_exceptions = {
        ("_publish-image-member.yml", "publish-member"): {
            "contents": "read",
            "packages": "write",
        },
        ("release-vllm-images-protected.yml", "build-images-protected"): {
            "contents": "read",
            "packages": "write",
        },
        ("release-vllm-images-protected.yml", "aggregate-members"): {
            "contents": "read",
            "packages": "read",
        },
        ("release-vllm-images-protected.yml", "publish-indexes"): {
            "contents": "read",
            "packages": "write",
        },
        ("release-ucm.yml", "reconcile-images-protected"): {
            "contents": "read",
            "packages": "write",
        },
        ("release-ucm.yml", "prepare-release-draft"): {"contents": "write"},
        ("release-ucm.yml", "publish-release"): {"contents": "write"},
    }
    violations: list[str] = []
    for filename in sorted(CHANGED_WORKFLOWS):
        document = _load_workflow(WORKFLOW_DIR / filename)
        if document.get("permissions") != {"contents": "read"}:
            violations.append(
                f"{filename}: workflow permissions are not contents: read"
            )
        for job_name, job in _jobs(document).items():
            expected = permission_exceptions.get(
                (filename, job_name), {"contents": "read"}
            )
            if job.get("permissions") != expected:
                violations.append(
                    f"{filename}:{job_name} permissions differ from exact authority"
                )
        for uses in _uses_in(document):
            if uses == TRUSTED_V2_CONTROLLER:
                continue
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


def test_release_test_checkout_fetches_the_parent_commit() -> None:
    """Source-context tests inspect HEAD^ and cannot run from a depth-one checkout."""
    lint = _load_workflow(WORKFLOW_DIR / "lint-and-test.yml")
    steps = _steps(_jobs(lint)["release-tests"])
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"] == {
        "persist-credentials": False,
        "fetch-depth": 2,
    }


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
        inspected_profile = FIXTURE_PROFILE.replace("amd64", "arm64")
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
    """Workflow derives all six real base chains instead of duplicating coordinates."""
    image_module = importlib.import_module("ucm_release.image")
    authority = image_module.real_image_authorities()
    environment = {
        **__import__("os").environ,
        "PYTHONPATH": str(REPO_ROOT / ".github" / "release"),
    }
    command = subprocess.run(
        [sys.executable, "-m", "ucm_release", "image", "real-authorities"],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(command.stdout)["members"] == authority

    workflow = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    assert "image real-authorities" in workflow
    assert "image base-record-real" in workflow
    for item in authority:
        for field in ("index_digest", "manifest_digest", "config_digest"):
            assert str(item["runtime"][field]) not in workflow


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


def test_task5_protected_route_has_static_permission_and_environment_boundaries() -> (
    None
):
    """Only the exact protected tag may reach GHCR or GitHub Release mutation jobs."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_jobs = _jobs(entry)
    assert _trigger(entry)["push"] == {
        "branches": ["feature/**"],
        "tags": ["v0.5.0rc1"],
    }
    assert entry["concurrency"] == {
        "group": "ucm-tag-${{ github.repository_id }}-${{ github.ref_name }}",
        "cancel-in-progress": False,
    }
    assert entry["permissions"] == {"contents": "read"}

    assert entry_jobs["build-wheels-feature"]["permissions"] == {"contents": "read"}
    assert entry_jobs["reconcile-images-feature"]["permissions"] == {"contents": "read"}
    assert "environment" not in entry_jobs["reconcile-images-feature"]
    assert entry_jobs["reconcile-images-protected"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    protected_call_if = str(entry_jobs["reconcile-images-protected"]["if"])
    for fragment in (
        "github.event_name == 'push'",
        "github.repository == 'SuperMarioYL/unified-cache-management'",
        "github.ref == 'refs/tags/v0.5.0rc1'",
        "github.ref_protected == true",
    ):
        assert fragment in protected_call_if

    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    publisher_workflow = _load_workflow(WORKFLOW_DIR / "_publish-image-member.yml")
    publisher = _jobs(publisher_workflow)["publish-member"]
    assert publisher["permissions"] == {"contents": "read", "packages": "write"}
    assert publisher["environment"] == "release-production"
    assert "UCM_RELEASE_POLICY" not in publisher.get("env", {})
    policy_steps = [
        step
        for step in _steps(publisher)
        if "core tag-preflight --lane protected-tag" in str(step.get("run", ""))
        or "registry verify-member" in str(step.get("run", ""))
    ]
    assert policy_steps
    assert all(
        step.get("env", {}).get("UCM_RELEASE_POLICY")
        == "${{ vars.UCM_RELEASE_POLICY }}"
        for step in policy_steps
    )
    publisher_text = "\n".join(_strings(publisher))
    for fragment in (
        "core tag-preflight --lane protected-tag",
        "registry verify-member",
        "registry.validate_member_record",
        "registry audit-operations",
        "member-mutation-preflight.json",
        "GITHUB_TOKEN",
    ):
        assert fragment in publisher_text
    assert _jobs(image)["build"]["permissions"] == {"contents": "read"}
    publisher_steps = _steps(publisher)
    publisher_auth = next(
        index
        for index, step in enumerate(publisher_steps)
        if step.get("name") == "Authenticate the pinned publisher to GHCR"
    )
    publisher_mutation = next(
        index
        for index, step in enumerate(publisher_steps)
        if step.get("id") == "record"
    )
    publisher_cleanup = next(
        index
        for index, step in enumerate(publisher_steps)
        if step.get("name") == "Remove Registry credentials before evidence upload"
    )
    publisher_upload = next(
        index
        for index, step in enumerate(publisher_steps)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert "out/member-mutation-preflight.json" in str(
        publisher_steps[publisher_upload]["with"]["path"]
    )
    publisher_downloads = [
        index
        for index, step in enumerate(publisher_steps)
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert publisher_downloads and max(publisher_downloads) < publisher_auth
    assert (
        publisher_steps[publisher_auth - 1]["name"]
        == "Validate exact current-attempt bridge names"
    )
    assert publisher_auth < publisher_mutation < publisher_cleanup < publisher_upload
    assert publisher_steps[publisher_cleanup]["if"] == "${{ always() }}"
    assert "crane auth logout" in str(publisher_steps[publisher_cleanup]["run"])
    assert publisher_steps[publisher_auth]["env"]["DOCKER_CONFIG"] == (
        publisher_steps[publisher_mutation]["env"]["DOCKER_CONFIG"]
    )

    for job_name in ("prepare-release-draft", "publish-release"):
        release_job = entry_jobs[job_name]
        assert release_job["permissions"] == {"contents": "write"}
        assert release_job["environment"] == "release-production"
        assert "UCM_RELEASE_POLICY" not in release_job.get("env", {})
        mutation_steps = [
            step
            for step in _steps(release_job)
            if "core tag-preflight --lane protected-tag" in str(step.get("run", ""))
        ]
        assert mutation_steps
        assert all(
            step.get("env", {}).get("UCM_RELEASE_POLICY")
            == "${{ vars.UCM_RELEASE_POLICY }}"
            for step in mutation_steps
        )

    all_text = "\n".join(
        "\n".join(_strings(_load_workflow(WORKFLOW_DIR / filename)))
        for filename in EXPECTED_RELEASE_WORKFLOWS
    )
    for forbidden in (
        "secrets.GHCR_TOKEN",
        "secrets.PAT",
        "id-token: write",
        "pull_request_target",
    ):
        assert forbidden not in all_text


def test_feature_reusable_call_graph_never_requests_package_authority() -> None:
    """GitHub validates nested reusable permissions before evaluating job conditions."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    feature_call = _jobs(entry)["reconcile-images-feature"]
    assert feature_call["permissions"] == {"contents": "read"}
    pending = [str(feature_call["uses"]).removeprefix("./.github/workflows/")]
    visited: set[str] = set()
    while pending:
        filename = pending.pop()
        if filename in visited:
            continue
        visited.add(filename)
        document = _load_workflow(WORKFLOW_DIR / filename)
        assert document["permissions"] == {"contents": "read"}
        for job_name, job in _jobs(document).items():
            assert "packages" not in job.get("permissions", {}), (
                filename,
                job_name,
                "feature nested reusable call requests package authority",
            )
            uses = str(job.get("uses", ""))
            if uses.startswith("./.github/workflows/"):
                pending.append(uses.removeprefix("./.github/workflows/"))
    assert visited == {"release-vllm-images.yml", "_build-image.yml"}


def test_protected_reusable_call_graph_is_split_from_feature_authority() -> None:
    """Every write-capable reusable edge is reachable only from the protected caller."""
    entry_jobs = _jobs(_load_workflow(WORKFLOW_DIR / "release-ucm.yml"))
    assert entry_jobs["reconcile-images-feature"]["uses"] == (
        "./.github/workflows/release-vllm-images.yml"
    )
    assert entry_jobs["reconcile-images-protected"]["uses"] == (
        "./.github/workflows/release-vllm-images-protected.yml"
    )
    protected = _load_workflow(WORKFLOW_DIR / "release-vllm-images-protected.yml")
    protected_build = _jobs(protected)["build-images-protected"]
    assert protected_build["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert protected_build["uses"] == ("./.github/workflows/_publish-image-member.yml")
    publisher = _load_workflow(WORKFLOW_DIR / "_publish-image-member.yml")
    publisher_jobs = _jobs(publisher)
    assert publisher_jobs["build"]["permissions"] == {"contents": "read"}
    assert publisher_jobs["build"]["uses"] == ("./.github/workflows/_build-image.yml")
    assert publisher_jobs["publish-member"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert publisher_jobs["publish-member"]["environment"] == "release-production"


def test_hosted_shellcheck_false_positives_are_narrowly_resolved() -> None:
    """The hosted actionlint shellcheck binary must accept protected workflow scripts."""
    image = _load_workflow(WORKFLOW_DIR / "_publish-image-member.yml")
    member = _jobs(image)["publish-member"]
    member_run = "\n".join(str(step.get("run", "")) for step in _steps(member))
    assert 'schema["$defs"]' in member_run
    assert "# shellcheck disable=SC2016" in member_run
    entry_text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    assert "origin/develop^{commit}" not in entry_text


def test_task5_artifacts_are_run_bound_and_barriers_are_transitive() -> None:
    """A stale artifact or skipped/cancelled matrix cannot open an index write gate."""
    for filename in ("_build-wheel.yml", "_build-image.yml"):
        text = "\n".join(_strings(_load_workflow(WORKFLOW_DIR / filename)))
        assert "GITHUB_RUN_ID" in text
        assert "GITHUB_RUN_ATTEMPT" in text
        assert "run_bound_artifact_name" in text

    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    feature_jobs = _jobs(images)
    protected_images = _load_workflow(
        WORKFLOW_DIR / "release-vllm-images-protected.yml"
    )
    jobs = _jobs(protected_images)
    assert feature_jobs["build-images-feature"]["strategy"]["fail-fast"] is False
    assert jobs["build-images-protected"]["strategy"]["fail-fast"] is False
    assert feature_jobs["build-images-feature"]["permissions"] == {"contents": "read"}
    assert jobs["build-images-protected"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert jobs["publish-indexes"]["strategy"] == {
        "fail-fast": False,
        "matrix": {"family_id": ["cann900-a3", "cann900-a2", "cuda130"]},
    }
    assert jobs["publish-indexes"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert jobs["publish-indexes"]["environment"] == "release-production"
    assert "UCM_RELEASE_POLICY" not in jobs["publish-indexes"].get("env", {})
    index_policy_steps = [
        step
        for step in _steps(jobs["publish-indexes"])
        if "core tag-preflight --lane protected-tag" in str(step.get("run", ""))
    ]
    assert index_policy_steps
    assert all(
        step.get("env", {}).get("UCM_RELEASE_POLICY")
        == "${{ vars.UCM_RELEASE_POLICY }}"
        for step in index_policy_steps
    )
    index_steps = _steps(jobs["publish-indexes"])
    index_auth = next(
        index
        for index, step in enumerate(index_steps)
        if step.get("name") == "Authenticate GHCR publisher"
    )
    index_mutation = next(
        index for index, step in enumerate(index_steps) if step.get("id") == "publish"
    )
    index_cleanup = next(
        index
        for index, step in enumerate(index_steps)
        if step.get("name") == "Remove Registry credentials before evidence upload"
    )
    index_upload = next(
        index
        for index, step in enumerate(index_steps)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    index_downloads = [
        index
        for index, step in enumerate(index_steps)
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert index_downloads and max(index_downloads) < index_auth
    assert index_steps[index_auth - 1]["name"] == (
        "Reopen exact current-attempt parent before authentication"
    )
    assert index_auth < index_mutation < index_cleanup < index_upload
    assert index_steps[index_cleanup]["if"] == "${{ always() }}"
    assert "crane auth logout" in str(index_steps[index_cleanup]["run"])
    assert index_steps[index_auth]["env"]["DOCKER_CONFIG"] == (
        index_steps[index_mutation]["env"]["DOCKER_CONFIG"]
    )

    index_install_index = next(
        index
        for index, step in enumerate(index_steps)
        if step.get("name") == "Install checksum-pinned Registry tools"
    )
    index_install = index_steps[index_install_index]
    index_install_command = str(index_install["run"])
    fixed_buildx = "${HOME}/.docker/cli-plugins/docker-buildx"
    assert index_install_index < index_auth
    assert fixed_buildx in index_install_command
    assert "${docker_config}/cli-plugins/docker-buildx" not in index_install_command
    assert f'"{fixed_buildx}" version' in index_install_command
    assert f'rm -f "{fixed_buildx}"' in str(index_steps[index_cleanup]["run"])

    for barrier in (
        feature_jobs["feature-barrier"],
        jobs["member-barrier"],
        jobs["index-barrier"],
    ):
        barrier_text = "\n".join(_strings(barrier))
        for status in ("success", "skipped", "failure", "cancelled"):
            assert status in barrier_text

    protected_chain = {
        "aggregate-members": {"plan", "build-images-protected", "member-barrier"},
        "publish-indexes": {"plan", "aggregate-members"},
        "index-barrier": {"plan", "aggregate-members", "publish-indexes"},
        "authenticated-readback": {
            "plan",
            "aggregate-members",
            "publish-indexes",
            "index-barrier",
        },
    }
    for job_name, direct_needs in protected_chain.items():
        job = jobs[job_name]
        assert set(job["needs"]) == direct_needs
        condition = str(job["if"])
        if job_name not in {"index-barrier"}:
            assert "!cancelled()" in condition
        if "always()" not in condition:
            for dependency in direct_needs - {"plan"}:
                assert f"needs.{dependency}.result == 'success'" in condition

    workflow_text = "\n".join(_strings(protected_images))
    for required in (
        "run-${{ github.run_id }}-attempt-${{ github.run_attempt }}",
        "artifact collect-members",
        "artifact collect-provisionals",
        "member_collection",
        "provisional_collection",
        "registry inventory",
        "registry plan-index",
        "registry prepare-index",
        "registry aggregate-authenticated",
    ):
        assert required in workflow_text
    assert "registry audit-operations" in "\n".join(
        _strings(_load_workflow(WORKFLOW_DIR / "_publish-image-member.yml"))
    )
    assert "registry verify-index" not in workflow_text


def test_task5_public_visibility_and_release_order_are_fail_closed() -> None:
    """Anonymous package closure and seven rehashed assets precede prerelease publish."""
    images = _load_workflow(WORKFLOW_DIR / "release-vllm-images-protected.yml")
    jobs = _jobs(images)
    assert jobs["authenticated-readback"]["permissions"] == {"contents": "read"}
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_jobs = _jobs(entry)
    draft = entry_jobs["prepare-release-draft"]
    assert set(draft["needs"]) == {
        "plan",
        "build-wheels-protected",
        "package-chart",
        "reconcile-images-protected",
    }
    assert "Fresh preflight and create empty draft or reopen exact state" in {
        str(step.get("name", "")) for step in _steps(draft)
    }

    anonymous = entry_jobs["anonymous-registry-readback"]
    assert set(anonymous["needs"]) == {
        "plan",
        "reconcile-images-protected",
        "prepare-release-draft",
    }
    assert anonymous["permissions"] == {"contents": "read"}
    anonymous_text = "\n".join(_strings(anonymous))
    assert "registry finalize-index" in anonymous_text
    assert "DOCKER_CONFIG" not in anonymous.get("env", {})
    assert "ucm-release-staging" not in anonymous_text

    release_job = entry_jobs["publish-release"]
    assert set(release_job["needs"]) == {
        "plan",
        "build-wheels-protected",
        "package-chart",
        "prepare-release-draft",
        "anonymous-registry-readback",
    }
    release_text = "\n".join(_strings(release_job))
    release_transport_text = release_text + "\n" + "\n".join(_strings(draft))
    assert release_text.index("release plan-assets") < release_text.index(
        "gh api --method POST"
    )
    patch_index = release_text.index("gh api --method PATCH")
    assert release_text.index("release verify-assets") < patch_index
    assert release_text.index("release plan-state", patch_index) > patch_index
    authenticated_list_index = release_text.index(
        ">out/authenticated-assets-pages.json"
    )
    assert patch_index < authenticated_list_index
    assert authenticated_list_index < release_text.index(
        "release refresh-assets --input out/refresh-authenticated-assets-request.json"
    )
    assert release_text.index(
        "release refresh-assets --input out/refresh-authenticated-assets-request.json"
    ) < release_text.index(
        "release verify-assets --input out/verify-authenticated-assets-request.json"
    )
    request_lines = {
        line.strip().split('open("', 1)[1].split('"', 1)[0]: line
        for line in release_text.splitlines()
        if "json.dump(" in line and 'open("out/' in line
    }
    for request_name in (
        "out/initial-download-request.json",
        "out/asset-plan-request.json",
        "out/upload-prefix-request.json",
        "out/upload-response-request.json",
        "out/final-download-request.json",
        "out/verify-prepublish-assets-request.json",
        "out/refresh-assets-request.json",
        "out/refresh-authenticated-assets-request.json",
        "out/verify-authenticated-assets-request.json",
    ):
        request_line = request_lines[request_name]
        assert '"release"' in request_line
        assert '"source_sha"' in request_line
    for request_name in (
        "out/refresh-assets-request.json",
        "out/refresh-authenticated-assets-request.json",
    ):
        assert '"prior_release"' in request_lines[request_name]
    for fragment in (
        "release assets-manifest",
        "release plan-state",
        "release plan-assets",
        "release verify-assets",
        "+refs/heads/develop:refs/remotes/origin/develop",
        "HEAD:.github/workflows",
        "origin/develop:.github/workflows",
        "HEAD:.github/release",
        "origin/develop:.github/release",
        "releases?per_page=100",
        "--paginate",
        '"draft":false',
        '"prerelease":true',
        '"make_latest":"false"',
    ):
        assert fragment in release_transport_text
    assert "--clobber" not in release_text
    assert "DELETE" not in release_text
    public_job = entry_jobs["anonymous-release-readback"]
    assert set(public_job["needs"]) == {
        "plan",
        "prepare-release-draft",
        "publish-release",
    }
    assert public_job["permissions"] == {"contents": "read"}
    assert "environment" not in public_job
    assert "GH_TOKEN" not in "\n".join(_strings(public_job))
    assert "release publication-evidence" in "\n".join(_strings(public_job))
    public_text = "\n".join(_strings(public_job))
    anonymous_request_line = next(
        line
        for line in public_text.splitlines()
        if 'open("out/anonymous-download-request.json"' in line
    )
    assert '"release"' in anonymous_request_line
    assert '"source_sha"' in anonymous_request_line


def test_task5_run_bound_artifact_identity_rejects_cross_attempt_reuse() -> None:
    """Physical artifact identity binds run/attempt while logical identity stays stable."""
    _, _, verify_module = _release_modules()
    logical = "ucm-wheel-cuda130-amd64-" + "a" * 40
    assert verify_module.run_bound_artifact_name(logical, "17", 2) == (
        logical + "-run-17-attempt-2"
    )
    assert (
        verify_module.validate_run_bound_artifact_name(
            logical + "-run-17-attempt-2", logical, {"run_id": "17", "run_attempt": 2}
        )
        == logical + "-run-17-attempt-2"
    )
    for forged in (
        logical,
        logical + "-run-18-attempt-2",
        logical + "-run-17-attempt-1",
        logical + "-run-17-attempt-2-extra",
    ):
        with pytest.raises(ValueError, match="artifact"):
            verify_module.validate_run_bound_artifact_name(
                forged, logical, {"run_id": "17", "run_attempt": 2}
            )


def test_task5_run_bound_artifact_directories_reject_stale_or_logical_dirs(
    tmp_path: Path,
) -> None:
    """Downloaded task directories must be the exact six from this attempt."""
    _, _, verify_module = _release_modules()
    logical_names = [
        "ucm-wheel-cuda130-amd64-" + "a" * 40,
        "ucm-wheel-cuda130-arm64-" + "a" * 40,
    ]
    run = {"run_id": "17", "run_attempt": 2}
    for logical_name in logical_names:
        (
            tmp_path / verify_module.run_bound_artifact_name(logical_name, "17", 2)
        ).mkdir()

    resolved = verify_module.resolve_run_bound_artifact_directories(
        tmp_path, logical_names, run=run, label="wheel"
    )
    assert set(resolved) == set(logical_names)
    assert all(path.is_dir() for path in resolved.values())

    stale = tmp_path / verify_module.run_bound_artifact_name(logical_names[0], "17", 1)
    stale.mkdir()
    with pytest.raises(ValueError, match="artifact|attempt|extra"):
        verify_module.resolve_run_bound_artifact_directories(
            tmp_path, logical_names, run=run, label="wheel"
        )
    stale.rmdir()
    physical = tmp_path / verify_module.run_bound_artifact_name(
        logical_names[0], "17", 2
    )
    physical.rename(tmp_path / logical_names[0])
    with pytest.raises(ValueError, match="artifact|attempt|missing"):
        verify_module.resolve_run_bound_artifact_directories(
            tmp_path, logical_names, run=run, label="wheel"
        )


def test_task5_release_state_is_create_or_exact_idempotent_reuse() -> None:
    """Reruns reject foreign releases while accepting the exact draft/prerelease."""
    _, _, verify_module = _release_modules()
    source_sha = "a" * 40
    authority = verify_module.github_release_authority(source_sha)
    assert authority == {
        "tag_name": "v0.5.0rc1",
        "target_commitish": source_sha,
        "name": "UCM v0.5.0rc1",
        "body": (
            "Protected UCM v0.5.0rc1 release from reviewed source commit "
            + source_sha
            + "."
        ),
        "draft": True,
        "prerelease": True,
        "make_latest": "false",
    }
    assert verify_module.plan_github_release(None, source_sha)["decision"] == "create"

    remote = {
        "id": 41,
        "tag_name": authority["tag_name"],
        "target_commitish": authority["target_commitish"],
        "name": authority["name"],
        "body": authority["body"],
        "draft": True,
        "prerelease": True,
        "assets": [],
        "author": {"login": "github-actions[bot]", "type": "Bot"},
        "upload_url": "https://uploads.github.com/repos/SuperMarioYL/unified-cache-management/releases/41/assets{?name,label}",
        "url": "https://api.github.com/repos/SuperMarioYL/unified-cache-management/releases/41",
        "assets_url": "https://api.github.com/repos/SuperMarioYL/unified-cache-management/releases/41/assets",
        "html_url": (
            "https://github.com/SuperMarioYL/unified-cache-management/"
            "releases/tag/untagged-a2d19fd21f8e2f4f9847"
        ),
    }
    created = verify_module.plan_github_release(remote, source_sha, just_created=True)
    assert created["decision"] == "reuse-draft"
    assert created["asset_count"] == 0
    assert created["asset_download_slug"] == "untagged-a2d19fd21f8e2f4f9847"

    published = copy.deepcopy(remote)
    published["draft"] = False
    published["html_url"] = (
        "https://github.com/SuperMarioYL/unified-cache-management/"
        "releases/tag/v0.5.0rc1"
    )
    assert verify_module.plan_github_release(published, source_sha)["decision"] == (
        "inspect-published-prerelease"
    )
    different_unused_target = copy.deepcopy(remote)
    different_unused_target["target_commitish"] = "develop"
    assert (
        verify_module.plan_github_release(different_unused_target, source_sha)[
            "decision"
        ]
        == "resume-draft"
    )

    for mutation in (
        {"tag_name": "v0.5.0"},
        {"name": "foreign"},
        {"body": "foreign"},
        {"prerelease": False},
        {"prerelease": 1},
        {"draft": 1},
    ):
        with pytest.raises(ValueError, match="release"):
            verify_module.plan_github_release({**remote, **mutation}, source_sha)
    partial_create = copy.deepcopy(remote)
    partial_create["assets"] = [{"id": 1, "name": "foreign.whl"}]
    with pytest.raises(ValueError, match="empty"):
        verify_module.plan_github_release(partial_create, source_sha, just_created=True)
    existing_partial = verify_module.plan_github_release(partial_create, source_sha)
    assert existing_partial["decision"] == "resume-draft"
    assert existing_partial["asset_count"] == 1

    mismatched_endpoint = copy.deepcopy(remote)
    mismatched_endpoint["upload_url"] = mismatched_endpoint["upload_url"].replace(
        "/41/", "/42/"
    )
    with pytest.raises(ValueError, match="transport"):
        verify_module.plan_github_release(mismatched_endpoint, source_sha)
    mismatched_assets_endpoint = copy.deepcopy(remote)
    mismatched_assets_endpoint["assets_url"] += "/foreign"
    with pytest.raises(ValueError, match="transport"):
        verify_module.plan_github_release(mismatched_assets_endpoint, source_sha)
    malformed_author = copy.deepcopy(remote)
    malformed_author["author"] = {"login": ""}
    with pytest.raises(ValueError, match="author"):
        verify_module.plan_github_release(malformed_author, source_sha)
    foreign_author = copy.deepcopy(remote)
    foreign_author["author"] = {"login": "attacker[bot]", "type": "Bot"}
    with pytest.raises(ValueError, match="author"):
        verify_module.plan_github_release(foreign_author, source_sha)

    for bad_html_url in (
        "http://github.com/SuperMarioYL/unified-cache-management/releases/tag/untagged-a2d19fd21f8e2f4f9847",
        "https://example.invalid/SuperMarioYL/unified-cache-management/releases/tag/untagged-a2d19fd21f8e2f4f9847",
        "https://github.com/SuperMarioYL/unified-cache-management/releases/tag/untagged-a2d19fd21f8e2f4f9847?foreign=1",
        "https://github.com/SuperMarioYL/unified-cache-management/releases/tag/untagged-a2d19fd21f8e2f4f9847#foreign",
        "https://github.com/SuperMarioYL/unified-cache-management/releases/tag/untagged-a2d19fd21f8e2f4f9847/foreign",
        "https://github.com/SuperMarioYL/unified-cache-management/releases/tag/v0.5.0rc1",
    ):
        with pytest.raises(ValueError, match="HTML transport"):
            verify_module.plan_github_release(
                {**remote, "html_url": bad_html_url}, source_sha
            )
    with pytest.raises(ValueError, match="HTML transport"):
        verify_module.plan_github_release(
            {
                **published,
                "html_url": (
                    "https://github.com/SuperMarioYL/unified-cache-management/"
                    "releases/tag/untagged-a2d19fd21f8e2f4f9847"
                ),
            },
            source_sha,
        )


def test_task5_release_asset_plan_never_overwrites_or_ignores_conflicts(
    tmp_path: Path,
) -> None:
    """Only missing canonical assets upload; existing names must match exact bytes."""
    core_module, _, verify_module = _release_modules()
    asset_root = tmp_path / "release-assets"
    asset_root.mkdir()
    tasks = core_module.build_matrix("protected-tag")["tasks"]
    assets = []
    for index, task in enumerate(tasks):
        architecture = "x86_64" if task["cpu_arch"] == "amd64" else "aarch64"
        name = (
            f"uc_manager-{task['wheel_version']}-{task['python_abi']}-"
            f"{task['python_abi']}-{task['wheel_platform']}_{architecture}.whl"
        )
        path = asset_root / name
        path.write_bytes(f"wheel-{task['spec_id']}".encode())
        assets.append(
            {
                "spec_id": task["spec_id"],
                "profile_id": task["profile_id"],
                "platform": task["platform"],
                "name": name,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "type": "wheel",
                "path": str(path),
            }
        )
    chart_name = "unified-cache-pd-0.5.0-rc.1.tgz"
    chart_path = asset_root / chart_name
    chart_path.write_bytes(b"real-chart")
    assets.append(
        {
            "spec_id": "helm-chart",
            "profile_id": None,
            "platform": None,
            "name": chart_name,
            "sha256": "sha256:" + hashlib.sha256(chart_path.read_bytes()).hexdigest(),
            "size": chart_path.stat().st_size,
            "type": "helm-chart",
            "path": str(chart_path),
        }
    )
    expected = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets",
        "source_sha": "a" * 40,
        "assets": assets,
    }
    expected["assets_sha256"] = verify_module.sha256_value(
        {
            "schema_version": 1,
            "kind": expected["kind"],
            "source_sha": expected["source_sha"],
            "assets": [
                {key: value for key, value in asset.items() if key != "path"}
                for asset in assets
            ],
        }
    )

    assert (
        verify_module.validate_release_asset_manifest(expected, allowed_root=asset_root)
        == expected
    )

    def remote(asset: dict[str, object], asset_id: int) -> dict[str, object]:
        return {
            "release_id": 41,
            "asset_id": asset_id,
            "name": asset["name"],
            "size": asset["size"],
            "state": "uploaded",
            "digest": asset["sha256"],
            "api_url": (
                "https://api.github.com/repos/SuperMarioYL/"
                f"unified-cache-management/releases/assets/{asset_id}"
            ),
            "browser_download_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/download/v0.5.0rc1/{asset['name']}"
            ),
            "uploader": {"login": "github-actions[bot]", "type": "Bot"},
            "download_sha256": asset["sha256"],
            "download_size": asset["size"],
        }

    reused = [remote(expected["assets"][0], 501)]
    plan = verify_module.plan_release_assets(
        expected, reused, release_id=41, allowed_root=asset_root
    )
    assert plan["asset_count"] == 7
    assert plan["reuse_names"] == [expected["assets"][0]["name"]]
    assert len(plan["upload_names"]) == 6

    draft_slug = "untagged-a2d19fd21f8e2f4f9847"
    draft_reused = copy.deepcopy(reused)
    draft_reused[0]["browser_download_url"] = draft_reused[0][
        "browser_download_url"
    ].replace("v0.5.0rc1", draft_slug)
    draft_plan = verify_module.plan_release_assets(
        expected,
        draft_reused,
        release_id=41,
        allowed_root=asset_root,
        asset_download_slug=draft_slug,
    )
    assert draft_plan["reuse_names"] == [expected["assets"][0]["name"]]
    with pytest.raises(ValueError, match="transport"):
        verify_module.plan_release_assets(
            expected,
            draft_reused,
            release_id=41,
            allowed_root=asset_root,
            asset_download_slug="untagged-00000000000000000000",
        )

    conflict = copy.deepcopy(reused)
    conflict[0]["download_sha256"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError, match="conflict"):
        verify_module.plan_release_assets(
            expected, conflict, release_id=41, allowed_root=asset_root
        )
    with pytest.raises(ValueError, match="foreign"):
        verify_module.plan_release_assets(
            expected,
            [{**remote(expected["assets"][0], 502), "name": "foreign.bin"}],
            release_id=41,
            allowed_root=asset_root,
        )
    full = [remote(asset, 600 + index) for index, asset in enumerate(assets)]
    verified = verify_module.verify_release_assets(
        expected, full, release_id=41, allowed_root=asset_root
    )
    assert verified["verified_names"] == [asset["name"] for asset in assets]

    raw_assets = [
        {
            "id": item["asset_id"],
            "name": item["name"],
            "size": item["size"],
            "state": item["state"],
            "digest": item["digest"],
            "url": item["api_url"],
            "browser_download_url": item["browser_download_url"],
            "uploader": {
                **copy.deepcopy(item["uploader"]),
                "id": 41898282,
                "node_id": "MDM6Qm90NDE4OTgyODI=",
                "avatar_url": "https://avatars.githubusercontent.com/in/15368?v=4",
            },
        }
        for item in reversed(full)
    ]
    draft_raw_assets = copy.deepcopy(raw_assets[:1])
    draft_raw_assets[0]["browser_download_url"] = draft_raw_assets[0][
        "browser_download_url"
    ].replace("v0.5.0rc1", draft_slug)
    draft_download_plan = verify_module.plan_release_asset_downloads(
        expected,
        draft_raw_assets,
        release_id=41,
        allowed_root=asset_root,
        require_complete=False,
        asset_download_slug=draft_slug,
    )
    assert draft_download_plan["asset_download_slug"] == draft_slug
    assert (
        verify_module.record_release_upload_response(
            expected,
            draft_raw_assets[0],
            expected_name=draft_raw_assets[0]["name"],
            release_id=41,
            allowed_root=asset_root,
            asset_download_slug=draft_slug,
        )["asset_download_slug"]
        == draft_slug
    )
    download_plan = verify_module.plan_release_asset_downloads(
        expected,
        [raw_assets[:2], raw_assets[2:]],
        release_id=41,
        allowed_root=asset_root,
        require_complete=True,
    )
    assert [item["name"] for item in download_plan["downloads"]] == [
        item["name"] for item in assets
    ]
    download_root = tmp_path / "downloaded-assets"
    download_root.mkdir()
    for asset in assets:
        (download_root / asset["name"]).write_bytes(Path(asset["path"]).read_bytes())
    normalized = verify_module.complete_release_asset_downloads(
        download_plan, download_root
    )
    assert normalized == full
    foreign_symlink = download_root / "foreign-link"
    foreign_symlink.symlink_to(download_root / str(assets[0]["name"]))
    with pytest.raises(ValueError, match="directory|foreign|regular|symlink"):
        verify_module.complete_release_asset_downloads(download_plan, download_root)
    foreign_symlink.unlink()
    foreign_directory = download_root / "foreign-directory"
    foreign_directory.mkdir()
    with pytest.raises(ValueError, match="directory|foreign|regular"):
        verify_module.complete_release_asset_downloads(download_plan, download_root)
    foreign_directory.rmdir()
    assert (
        verify_module.refresh_release_asset_metadata(
            expected,
            full,
            list(reversed(raw_assets)),
            release_id=41,
            allowed_root=asset_root,
        )
        == full
    )
    draft_full = copy.deepcopy(full)
    for item in draft_full:
        item["browser_download_url"] = item["browser_download_url"].replace(
            "v0.5.0rc1", draft_slug
        )
    assert (
        verify_module.refresh_release_asset_metadata(
            expected,
            draft_full,
            list(reversed(raw_assets)),
            release_id=41,
            allowed_root=asset_root,
            prior_asset_download_slug=draft_slug,
            asset_download_slug="v0.5.0rc1",
        )
        == full
    )
    with pytest.raises(ValueError, match="phase transition"):
        verify_module.refresh_release_asset_metadata(
            expected,
            draft_full,
            list(reversed(raw_assets)),
            release_id=41,
            allowed_root=asset_root,
            prior_asset_download_slug=draft_slug,
            asset_download_slug="untagged-00000000000000000000",
        )
    for field, bad in (
        ("id", 999),
        ("name", "foreign.whl"),
        ("size", 999),
        ("digest", "sha256:" + "c" * 64),
        ("url", "https://api.github.com/foreign"),
        ("uploader", {"login": "attacker", "type": "User"}),
    ):
        changed = copy.deepcopy(raw_assets)
        changed[0][field] = bad
        with pytest.raises(ValueError, match="asset|metadata|foreign|conflict"):
            verify_module.refresh_release_asset_metadata(
                expected,
                draft_full,
                changed,
                release_id=41,
                allowed_root=asset_root,
                prior_asset_download_slug=draft_slug,
                asset_download_slug="v0.5.0rc1",
            )
    changed_prior = copy.deepcopy(draft_full)
    changed_prior[0]["download_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="asset|conflict"):
        verify_module.refresh_release_asset_metadata(
            expected,
            changed_prior,
            list(reversed(raw_assets)),
            release_id=41,
            allowed_root=asset_root,
            prior_asset_download_slug=draft_slug,
            asset_download_slug="v0.5.0rc1",
        )
    drifted_raw = copy.deepcopy(raw_assets)
    drifted_raw[0]["digest"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="asset|metadata|conflict"):
        verify_module.refresh_release_asset_metadata(
            expected,
            full,
            drifted_raw,
            release_id=41,
            allowed_root=asset_root,
        )
    current_reused = [
        {
            "id": item["asset_id"],
            "name": item["name"],
            "size": item["size"],
            "state": item["state"],
            "digest": item["digest"],
            "url": item["api_url"],
            "browser_download_url": item["browser_download_url"],
            "uploader": copy.deepcopy(item["uploader"]),
        }
        for item in reused
    ]
    prefix = verify_module.verify_release_upload_prefix(
        expected,
        plan,
        [],
        current_reused,
        next_name=plan["upload_names"][0],
        release_id=41,
        allowed_root=asset_root,
    )
    assert prefix["completed_upload_names"] == []
    raced = copy.deepcopy(current_reused)
    raced.append({**raw_assets[0], "name": "foreign.bin"})
    with pytest.raises(ValueError, match="foreign|changed|upload"):
        verify_module.verify_release_upload_prefix(
            expected,
            plan,
            [],
            raced,
            next_name=plan["upload_names"][0],
            release_id=41,
            allowed_root=asset_root,
        )
    with pytest.raises(ValueError, match="published|seven"):
        verify_module.plan_release_assets(
            expected,
            reused,
            release_id=41,
            release_published=True,
            allowed_root=asset_root,
        )

    for field, bad in (
        ("asset_id", True),
        ("release_id", 42),
        ("state", "starter"),
        ("digest", "sha256:" + "d" * 64),
        ("api_url", "https://api.github.com/attacker"),
        ("browser_download_url", "https://example.invalid/attacker"),
        ("uploader", {"login": "attacker", "type": "User"}),
    ):
        forged = copy.deepcopy(reused)
        forged[0][field] = bad
        with pytest.raises(ValueError, match="asset|conflict|release|transport"):
            verify_module.plan_release_assets(
                expected, forged, release_id=41, allowed_root=asset_root
            )

    boolean_schema = copy.deepcopy(expected)
    boolean_schema["schema_version"] = True
    with pytest.raises(ValueError, match="identity|schema"):
        verify_module.validate_release_asset_manifest(
            boolean_schema, allowed_root=asset_root
        )
    duplicate_spec = copy.deepcopy(expected)
    duplicate_spec["assets"][1]["spec_id"] = duplicate_spec["assets"][0]["spec_id"]
    duplicate_spec["assets_sha256"] = verify_module.sha256_value(
        {
            "schema_version": 1,
            "kind": duplicate_spec["kind"],
            "source_sha": duplicate_spec["source_sha"],
            "assets": [
                {key: value for key, value in asset.items() if key != "path"}
                for asset in duplicate_spec["assets"]
            ],
        }
    )
    with pytest.raises(ValueError, match="spec|canonical"):
        verify_module.validate_release_asset_manifest(
            duplicate_spec, allowed_root=asset_root
        )
    assets[0]["path"] = str(asset_root / ".." / assets[0]["name"])
    with pytest.raises(ValueError, match="path|root|regular"):
        verify_module.validate_release_asset_manifest(expected, allowed_root=asset_root)


def test_task5_release_asset_manifest_reopens_exact_current_attempt_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seven public assets derive from six sealed wheels and one Chart result."""
    core_module, wheel_module, verify_module = _release_modules()
    source_sha = "a" * 40
    run = {"run_id": "17", "run_attempt": 2}
    matrix = verify_module.hosted_build_matrix(source_sha, 1_700_000_000)
    wheel_root = tmp_path / "wheel-downloads"
    wheel_root.mkdir()
    inspections: dict[str, dict[str, object]] = {}
    for task in matrix["tasks"]:
        artifact = wheel_root / verify_module.run_bound_artifact_name(
            task["wheel_artifact"], "17", 2
        )
        artifact.mkdir()
        _write_canonical(artifact / "hosted-task.json", task)
        reviewed = next(
            item
            for item in core_module.build_matrix("protected-tag")["tasks"]
            if item["spec_id"] == task["spec_id"]
        )
        architecture = "x86_64" if reviewed["cpu_arch"] == "amd64" else "aarch64"
        filename = (
            f"uc_manager-{reviewed['wheel_version']}-{reviewed['python_abi']}-"
            f"{reviewed['python_abi']}-{reviewed['wheel_platform']}_{architecture}.whl"
        )
        wheel_path = artifact / filename
        wheel_path.write_bytes(task["spec_id"].encode())
        wheel_sha256 = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        inspection = {
            "filename": filename,
            "sha256": wheel_sha256,
            "spec_id": task["spec_id"],
            "builder_evidence": {
                "source_commit": source_sha,
                "build_key": task["task_sha256"],
                "source_date_epoch": matrix["source_date_epoch"],
                "build_context_digest": "sha256:" + "b" * 64,
            },
        }
        inspections[str(wheel_path)] = inspection
        _write_canonical(artifact / "wheel-inspection.json", inspection)
        inspection_sha = (
            "sha256:"
            + hashlib.sha256(
                (artifact / "wheel-inspection.json").read_bytes()
            ).hexdigest()
        )
        _write_canonical(
            artifact / "wheel-seal.json",
            {
                "source_kind": "builder-candidate",
                "publication_status": "unpublished",
                "publication_eligible": False,
                "spec_id": task["spec_id"],
                "source_sha": source_sha,
                "build_key": task["task_sha256"],
                "wheel_sha256": wheel_sha256,
                "inspection_sha256": inspection_sha,
            },
        )
        _write_canonical(
            artifact / "source-context.json",
            {
                "source_sha": source_sha,
                "source_tree": "c" * 40,
                "build_context_sha256": "sha256:" + "b" * 64,
            },
        )

    def inspect_wheel(
        path: Path, spec_id: str, wheel_sha256: str, source_kind: str
    ) -> dict[str, object]:
        assert source_kind == "builder-candidate"
        value = inspections[str(path)]
        assert value["spec_id"] == spec_id
        assert value["sha256"] == wheel_sha256
        return copy.deepcopy(value)

    monkeypatch.setattr(wheel_module, "inspect_wheel", inspect_wheel)
    chart_result = tmp_path / "chart-result.json"
    chart_package = tmp_path / "unified-cache-pd-0.5.0-rc.1.tgz"
    chart_package.write_bytes(b"chart")
    chart_sha256 = "sha256:" + hashlib.sha256(chart_package.read_bytes()).hexdigest()
    _write_canonical(
        chart_result,
        {
            "filename": chart_package.name,
            "sha256": chart_sha256,
            "release_tree_sha256": "sha256:" + "d" * 64,
            "rendered_cases": ["cuda", "ascend"],
            "status": "passed",
        },
    )
    monkeypatch.setattr(
        verify_module,
        "_real_chart_summary",
        lambda result_path, package_path: {
            **json.loads(result_path.read_text()),
            "sha256": "sha256:" + hashlib.sha256(package_path.read_bytes()).hexdigest(),
        },
    )

    output_root = tmp_path / "release-assets"
    manifest = verify_module.build_release_asset_manifest(
        wheel_dir=wheel_root,
        chart_result_path=chart_result,
        chart_package_path=chart_package,
        output_dir=output_root,
        source_sha=source_sha,
        run=run,
    )
    assert [item["spec_id"] for item in manifest["assets"]] == [
        *REAL_SPEC_IDS,
        "helm-chart",
    ]
    assert manifest["assets"][-1]["name"] == ("unified-cache-pd-0.5.0-rc.1.tgz")
    assert all(Path(item["path"]).parent == output_root for item in manifest["assets"])
    assert (
        verify_module.validate_release_asset_manifest(
            manifest, allowed_root=output_root
        )
        == manifest
    )

    first_task = matrix["tasks"][0]
    first_artifact = wheel_root / verify_module.run_bound_artifact_name(
        first_task["wheel_artifact"], "17", 2
    )
    seal = json.loads((first_artifact / "wheel-seal.json").read_text())
    seal["source_sha"] = "b" * 40
    _write_canonical(first_artifact / "wheel-seal.json", seal)
    with pytest.raises(ValueError, match="wheel|seal|source"):
        verify_module.build_release_asset_manifest(
            wheel_dir=wheel_root,
            chart_result_path=chart_result,
            chart_package_path=chart_package,
            output_dir=tmp_path / "forged-release-assets",
            source_sha=source_sha,
            run=run,
        )


@pytest.mark.parametrize("release_branch", ["published", "create", "resume"])
def test_task5_final_release_evidence_reopens_authenticated_and_anonymous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_branch: str,
) -> None:
    """Final evidence binds protected Registry closure to one public seven-asset release."""
    core_module, _, verify_module = _release_modules()
    source_sha = "a" * 40
    asset_root = tmp_path / "release-assets"
    asset_root.mkdir()
    assets: list[dict[str, object]] = []
    for task in core_module.build_matrix("protected-tag")["tasks"]:
        architecture = "x86_64" if task["cpu_arch"] == "amd64" else "aarch64"
        name = (
            f"uc_manager-{task['wheel_version']}-{task['python_abi']}-"
            f"{task['python_abi']}-{task['wheel_platform']}_{architecture}.whl"
        )
        path = asset_root / name
        path.write_bytes(task["spec_id"].encode())
        assets.append(
            {
                "spec_id": task["spec_id"],
                "profile_id": task["profile_id"],
                "platform": task["platform"],
                "name": name,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "type": "wheel",
                "path": str(path),
            }
        )
    chart_path = asset_root / "unified-cache-pd-0.5.0-rc.1.tgz"
    chart_path.write_bytes(b"chart")
    assets.append(
        {
            "spec_id": "helm-chart",
            "profile_id": None,
            "platform": None,
            "name": chart_path.name,
            "sha256": "sha256:" + hashlib.sha256(chart_path.read_bytes()).hexdigest(),
            "size": chart_path.stat().st_size,
            "type": "helm-chart",
            "path": str(chart_path),
        }
    )
    asset_manifest = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets",
        "source_sha": source_sha,
        "assets": assets,
    }
    asset_manifest["assets_sha256"] = verify_module.sha256_value(
        {
            "schema_version": 1,
            "kind": asset_manifest["kind"],
            "source_sha": source_sha,
            "assets": [
                {key: value for key, value in asset.items() if key != "path"}
                for asset in assets
            ],
        }
    )
    authority = verify_module.github_release_authority(source_sha)
    published_release = {
        "id": 41,
        "tag_name": authority["tag_name"],
        "target_commitish": "develop",
        "name": authority["name"],
        "body": authority["body"],
        "draft": False,
        "prerelease": True,
        "assets": [{"id": 600 + index} for index in range(7)],
        "author": {"login": "github-actions[bot]", "type": "Bot"},
        "upload_url": "https://uploads.github.com/repos/SuperMarioYL/unified-cache-management/releases/41/assets{?name,label}",
        "url": "https://api.github.com/repos/SuperMarioYL/unified-cache-management/releases/41",
        "assets_url": "https://api.github.com/repos/SuperMarioYL/unified-cache-management/releases/41/assets",
        "html_url": (
            "https://github.com/SuperMarioYL/unified-cache-management/"
            "releases/tag/v0.5.0rc1"
        ),
    }
    draft_slug = "untagged-a2d19fd21f8e2f4f9847"

    def remote(asset: dict[str, object], asset_id: int) -> dict[str, object]:
        return {
            "release_id": 41,
            "asset_id": asset_id,
            "name": asset["name"],
            "size": asset["size"],
            "state": "uploaded",
            "digest": asset["sha256"],
            "api_url": (
                "https://api.github.com/repos/SuperMarioYL/"
                f"unified-cache-management/releases/assets/{asset_id}"
            ),
            "browser_download_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/download/v0.5.0rc1/{asset['name']}"
            ),
            "uploader": {"login": "github-actions[bot]", "type": "Bot"},
            "download_sha256": asset["sha256"],
            "download_size": asset["size"],
        }

    remote_assets = [remote(asset, 600 + index) for index, asset in enumerate(assets)]
    draft_remote_assets = copy.deepcopy(remote_assets)
    for item in draft_remote_assets:
        item["browser_download_url"] = item["browser_download_url"].replace(
            "v0.5.0rc1", draft_slug
        )
    if release_branch == "published":
        prepare_release = copy.deepcopy(published_release)
        initial_release = copy.deepcopy(published_release)
        initial_assets = copy.deepcopy(remote_assets)
        prepare_initial_plan = verify_module.plan_github_release(
            prepare_release, source_sha
        )
        prepublish_release = copy.deepcopy(published_release)
    elif release_branch == "create":
        prepare_release = {
            **copy.deepcopy(published_release),
            "draft": True,
            "assets": [],
            "html_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/tag/{draft_slug}"
            ),
        }
        initial_release = copy.deepcopy(prepare_release)
        initial_assets = []
        prepare_initial_plan = verify_module.plan_github_release(None, source_sha)
        prepublish_release = {
            **copy.deepcopy(published_release),
            "draft": True,
            "html_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/tag/{draft_slug}"
            ),
        }
    else:
        prepare_release = {
            **copy.deepcopy(published_release),
            "draft": True,
            "assets": copy.deepcopy(published_release["assets"][:3]),
            "html_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/tag/{draft_slug}"
            ),
        }
        initial_release = copy.deepcopy(prepare_release)
        initial_assets = copy.deepcopy(draft_remote_assets[:3])
        prepare_initial_plan = verify_module.plan_github_release(
            prepare_release, source_sha
        )
        prepublish_release = {
            **copy.deepcopy(published_release),
            "draft": True,
            "html_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/tag/{draft_slug}"
            ),
        }
    initial_state = verify_module.plan_github_release(initial_release, source_sha)
    initial_asset_plan = verify_module.plan_release_assets(
        asset_manifest,
        initial_assets,
        release_id=41,
        allowed_root=asset_root,
        release_published=release_branch == "published",
        asset_download_slug=initial_state["asset_download_slug"],
    )

    def raw_asset(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item["asset_id"],
            "name": item["name"],
            "size": item["size"],
            "state": item["state"],
            "digest": item["digest"],
            "url": item["api_url"],
            "browser_download_url": item["browser_download_url"],
            "uploader": copy.deepcopy(item["uploader"]),
        }

    upload_assets = (
        remote_assets if release_branch == "published" else draft_remote_assets
    )
    final_by_name = {item["name"]: item for item in upload_assets}
    current_assets = copy.deepcopy(initial_assets)
    uploaded_raw: list[dict[str, object]] = []
    upload_transcript: list[dict[str, object]] = []
    for ordinal, name in enumerate(initial_asset_plan["upload_names"]):
        live_release = {
            **copy.deepcopy(prepare_release),
            "assets": [{"id": item["asset_id"]} for item in current_assets],
        }
        raw_current = [raw_asset(item) for item in current_assets]
        prefix = verify_module.verify_release_upload_prefix(
            asset_manifest,
            initial_asset_plan,
            uploaded_raw,
            raw_current,
            next_name=name,
            release_id=41,
            allowed_root=asset_root,
            asset_download_slug=initial_state["asset_download_slug"],
        )
        raw_response = raw_asset(final_by_name[name])
        response = verify_module.record_release_upload_response(
            asset_manifest,
            raw_response,
            expected_name=name,
            release_id=41,
            allowed_root=asset_root,
            asset_download_slug=initial_state["asset_download_slug"],
        )
        upload_transcript.append(
            {
                "ordinal": ordinal,
                "name": name,
                "release": live_release,
                "prefix": prefix,
                "response": response,
            }
        )
        uploaded_raw.append(raw_response)
        current_assets.append(copy.deepcopy(final_by_name[name]))
    if upload_transcript:
        forged_transcripts: list[list[dict[str, object]]] = []
        forged_prefix = copy.deepcopy(upload_transcript)
        forged_prefix[0]["prefix"]["current_asset_ids"] = [999]
        forged_transcripts.append(forged_prefix)
        forged_release = copy.deepcopy(upload_transcript)
        forged_release[0]["release"]["assets"] = [{"id": 999}]
        forged_transcripts.append(forged_release)
        forged_response = copy.deepcopy(upload_transcript)
        forged_response[0]["response"]["asset"]["digest"] = "sha256:" + "f" * 64
        forged_response[0]["response"]["response_sha256"] = verify_module.sha256_value(
            {
                key: value
                for key, value in forged_response[0]["response"].items()
                if key != "response_sha256"
            }
        )
        forged_transcripts.append(forged_response)
        forged_final_browser = copy.deepcopy(upload_transcript)
        forged_final_browser[-1]["response"]["asset"][
            "browser_download_url"
        ] = "https://example.invalid/forged-final-asset"
        forged_final_browser[-1]["response"]["response_sha256"] = (
            verify_module.sha256_value(
                {
                    key: value
                    for key, value in forged_final_browser[-1]["response"].items()
                    if key != "response_sha256"
                }
            )
        )
        forged_transcripts.append(forged_final_browser)
    else:
        forged_transcripts = [[{}]]
    for forged_transcript in forged_transcripts:
        with pytest.raises(ValueError, match="transcript|prefix|Release|response"):
            verify_module.validate_release_upload_transcript(
                asset_manifest,
                initial_asset_plan,
                forged_transcript,
                source_sha=source_sha,
                release_id=41,
                allowed_root=asset_root,
            )
    protected_payload = {
        "kind": "ucm-protected-registry-publication-payload",
        "source_sha": source_sha,
        "publication": {
            "registry": "published",
            "anonymous": "passed",
            "github_release": "pending",
        },
    }
    protected = {
        "payload": protected_payload,
        "payload_sha256": verify_module.sha256_value(protected_payload),
        "github": {"run_id": "17", "run_attempt": 2},
    }
    monkeypatch.setattr(
        verify_module,
        "protected_registry_publication_evidence",
        lambda **_kwargs: copy.deepcopy(protected),
    )
    api_root = "https://api.github.com/repos/SuperMarioYL/unified-cache-management"
    operations: list[dict[str, object]] = [
        {
            "type": "github-release-list",
            "capability": "read",
            "reference": api_root + "/releases",
            "authenticated": True,
        },
    ]
    if release_branch == "create":
        operations.extend(
            [
                {
                    "type": "github-release-create",
                    "capability": "write",
                    "reference": api_root + "/releases",
                    "authenticated": True,
                },
                {
                    "type": "github-release-read",
                    "capability": "read",
                    "reference": api_root + "/releases/41",
                    "authenticated": True,
                },
            ]
        )
    operations.extend(
        [
            {
                "type": "github-release-read",
                "capability": "read",
                "reference": api_root + "/releases/41",
                "authenticated": True,
            },
            {
                "type": "github-release-assets-list",
                "capability": "read",
                "reference": api_root + "/releases/41/assets",
                "authenticated": True,
            },
        ]
    )
    operations.extend(
        [
            {
                "type": "github-release-asset-download",
                "capability": "read",
                "reference": item["api_url"],
                "authenticated": True,
            }
            for item in initial_asset_plan["reuse_assets"]
        ]
    )
    for name in initial_asset_plan["upload_names"]:
        operations.extend(
            [
                {
                    "type": "github-release-read",
                    "capability": "read",
                    "reference": api_root + "/releases/41",
                    "authenticated": True,
                },
                {
                    "type": "github-release-assets-list",
                    "capability": "read",
                    "reference": api_root + "/releases/41/assets",
                    "authenticated": True,
                },
                {
                    "type": "github-release-asset-upload",
                    "capability": "write",
                    "reference": (
                        "https://uploads.github.com/repos/SuperMarioYL/"
                        "unified-cache-management/releases/41/assets?name="
                        + urllib.parse.quote(str(name), safe="")
                    ),
                    "authenticated": True,
                },
            ]
        )
    operations.extend(
        [
            {
                "type": "github-release-assets-list",
                "capability": "read",
                "reference": api_root + "/releases/41/assets",
                "authenticated": True,
            },
        ]
    )
    operations.extend(
        [
            {
                "type": "github-release-asset-download",
                "capability": "read",
                "reference": item["api_url"],
                "authenticated": True,
            }
            for item in remote_assets
        ]
    )
    if release_branch in {"create", "resume"}:
        operations.extend(
            [
                {
                    "type": "github-release-read",
                    "capability": "read",
                    "reference": api_root + "/releases/41",
                    "authenticated": True,
                },
                {
                    "type": "github-release-assets-list",
                    "capability": "read",
                    "reference": api_root + "/releases/41/assets",
                    "authenticated": True,
                },
                {
                    "type": "github-release-publish",
                    "capability": "write",
                    "reference": api_root + "/releases/41",
                    "authenticated": True,
                },
            ]
        )
    operations.extend(
        [
            {
                "type": "github-release-read",
                "capability": "read",
                "reference": api_root + "/releases/41",
                "authenticated": True,
            },
            {
                "type": "github-release-assets-list",
                "capability": "read",
                "reference": api_root + "/releases/41/assets",
                "authenticated": True,
            },
            {
                "type": "github-release-tag-read",
                "capability": "read",
                "reference": api_root + "/releases/tags/v0.5.0rc1",
                "authenticated": False,
            },
            {
                "type": "github-release-assets-list",
                "capability": "read",
                "reference": api_root + "/releases/41/assets",
                "authenticated": False,
            },
        ]
    )
    operations.extend(
        [
            {
                "type": "github-release-asset-download",
                "capability": "read",
                "reference": item["api_url"],
                "authenticated": False,
            }
            for item in remote_assets
        ]
    )
    prepublish_assets = (
        remote_assets if release_branch == "published" else draft_remote_assets
    )
    evidence = verify_module.github_release_publication_evidence(
        protected_registry=protected,
        asset_manifest=asset_manifest,
        allowed_root=asset_root,
        prepare_initial_plan=prepare_initial_plan,
        prepare_release=prepare_release,
        initial_release=initial_release,
        initial_assets=initial_assets,
        initial_asset_plan=initial_asset_plan,
        upload_transcript=upload_transcript,
        prepublish_release=prepublish_release,
        prepublish_assets=prepublish_assets,
        authenticated_release=published_release,
        authenticated_assets=remote_assets,
        anonymous_release=copy.deepcopy(published_release),
        anonymous_assets=copy.deepcopy(remote_assets),
        operations=operations,
        source_sha=source_sha,
        run={"run_id": "17", "run_attempt": 2},
    )
    assert evidence["payload"]["kind"] == "ucm-github-release-publication"
    assert evidence["payload"]["publication"] == "published-prerelease"
    assert evidence["payload"]["asset_count"] == 7

    forged_assets = copy.deepcopy(remote_assets)
    forged_assets[0]["download_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="asset|conflict"):
        verify_module.github_release_publication_evidence(
            protected_registry=protected,
            asset_manifest=asset_manifest,
            allowed_root=asset_root,
            prepare_initial_plan=prepare_initial_plan,
            prepare_release=prepare_release,
            initial_release=initial_release,
            initial_assets=initial_assets,
            initial_asset_plan=initial_asset_plan,
            upload_transcript=upload_transcript,
            prepublish_release=prepublish_release,
            prepublish_assets=prepublish_assets,
            authenticated_release=published_release,
            authenticated_assets=remote_assets,
            anonymous_release=published_release,
            anonymous_assets=forged_assets,
            operations=operations,
            source_sha=source_sha,
            run={"run_id": "17", "run_attempt": 2},
        )

    forged_operations = copy.deepcopy(operations)
    if release_branch == "published":
        forged_operations.insert(
            -1,
            {
                "type": "github-release-asset-upload",
                "capability": "write",
                "reference": (
                    "https://uploads.github.com/repos/SuperMarioYL/"
                    "unified-cache-management/releases/41/assets?name="
                    + urllib.parse.quote(str(assets[0]["name"]), safe="")
                ),
                "authenticated": True,
            },
        )
    elif release_branch == "create":
        forged_operations.pop(1)
    else:
        forged_operations = [
            item
            for item in forged_operations
            if item["type"] != "github-release-publish"
        ]
    with pytest.raises(ValueError, match="ledger|operation|branch|order"):
        verify_module.github_release_publication_evidence(
            protected_registry=protected,
            asset_manifest=asset_manifest,
            allowed_root=asset_root,
            prepare_initial_plan=prepare_initial_plan,
            prepare_release=prepare_release,
            initial_release=initial_release,
            initial_assets=initial_assets,
            initial_asset_plan=initial_asset_plan,
            upload_transcript=upload_transcript,
            prepublish_release=prepublish_release,
            prepublish_assets=prepublish_assets,
            authenticated_release=published_release,
            authenticated_assets=remote_assets,
            anonymous_release=published_release,
            anonymous_assets=remote_assets,
            operations=forged_operations,
            source_sha=source_sha,
            run={"run_id": "17", "run_attempt": 2},
        )
