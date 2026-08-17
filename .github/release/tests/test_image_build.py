"""OCI identity and ELF contract safety invariants for image build verification.

Only the two cross-cutting safety invariants are retained: the runtime
evidence gate that fails closed on every install/ABI/native/ELF/DT_NEEDED
drift, and the content-identity check that excludes run/signature bytes
while binding OCI labels and deterministic history.  The fixture-wheel,
install-audit, and OCI-archive shape change-detector tests were removed per
the slimming plan.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"


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


def _real_image_task(image, spec_id: str = "cuda130-amd64"):
    plan = _fixture_resolved_plan()
    task = next(item for item in plan["image_tasks"] if item["spec_id"] == spec_id)
    return image.real_image_authority_from_plan(
        plan,
        task_id=task["task_id"],
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )


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
                "repository": "release-org/unified-cache-management",
                "repository_url": (
                    "https://github.com/release-org/unified-cache-management"
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
                "https://github.com/release-org/unified-cache-management"
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
        "https://github.com/release-org/unified-cache-management"
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


@pytest.mark.parametrize("spec_id", ["cann900-a2-arm64", "cann900-a3-arm64"])
def test_cann900_builder_is_cann_ubuntu_and_runtime_closes_with_same_root_false(
    spec_id: str,
) -> None:
    """The Ascend wheel builder is the cann-ubuntu base (Mooncake built in-tree
    on the same CANN/Ubuntu lineage as vllm-ascend), while the runtime base
    stays vllm-ascend; the closure gate must still pass with the builder/base
    roots differing (same_root=False)."""
    *_, image = _modules()
    recipe, evidence = _real_runtime_probe(image, spec_id)
    builder_coordinate = recipe["payload"]["wheel"]["builder_evidence"][
        "builder_coordinate"
    ]
    base_subject = recipe["payload"]["base"]["subject"]
    assert builder_coordinate.startswith("quay.io/ascend/cann@sha256:")
    assert base_subject.startswith("quay.io/ascend/vllm-ascend@sha256:")
    assert builder_coordinate != base_subject
    assert image.verify_real_runtime_evidence(recipe, evidence) == {
        "install": "passed",
        "pip_check": "passed",
        "direct_url": "passed",
        "ucm_import": "passed",
        "runtime_dependency_imports": "passed",
        "abi": "passed",
        "native_members": "passed",
        "elf": "passed",
        "dependency_closure": "passed",
        "variant": "passed",
    }
