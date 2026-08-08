"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from . import chart, image, wheel
from .core import build_release_manifest, canonical_bytes, sha256_value, validate_config
from .registry import (
    TARGET_REPOSITORIES,
    RegistryBlocker,
    build_candidate,
    inventory_digest,
    parse_upstream_tag,
    reconcile,
    scan_registry,
    validate_public_tag,
    validate_snapshot,
)

EXPECTED_BLOCKERS = [
    "duplicate-conflicting-inventory",
    "missing-linux-arm64",
    "production-wheel-unpublished",
]
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[._:-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
OPERATION_CONTRACTS = MappingProxyType(
    {
        "fixture-read": ("read", "upstream-tag"),
        "crane-digest": ("read", "upstream-tag"),
        "crane-manifest": ("read", "upstream-digest"),
        "registry-inventory-read": ("read", "digest"),
        "build-plan": ("plan", "target-tag"),
    }
)
KNOWN_WRITE_OPERATION_TYPES = frozenset(
    {
        "registry-push",
        "registry-copy",
        "registry-tag",
        "crane-push",
        "crane-copy",
        "crane-tag",
    }
)
WORKFLOW_REFS = [
    "release-ucm.yml",
    "_build-wheel.yml",
    "release-vllm-images.yml",
    "_build-image.yml",
]
REQUIRED_SCENARIOS = [
    "new-input-one-task",
    "identical-input-zero-tasks",
    "tag-digest-drift-r2",
    "complete-digest-chain",
    "required-failures-block",
    "fixture-candidate-full-zero-reconcile",
]
REQUIRED_IMAGE_GATES = {
    "base_verified",
    "wheel_verified",
    "install",
    "pip_check",
    "direct_url",
    "ucm_import",
    "wrapt_import",
    "abi",
}


def _source_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source SHA must be a full lowercase Git commit")
    return value


def _envelope(payload: dict[str, Any], run: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "payload": payload,
        "payload_sha256": sha256_value(payload),
        "github": copy.deepcopy(run or {}),
    }


def _file_sha256(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"release artifact is not a regular file: {path}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    if raw != canonical_bytes(value) + b"\n":
        raise ValueError(f"{label} must use canonical JSON bytes")
    return value


def prepare_candidate_loop(
    build_record: dict[str, Any],
    wheel_record: dict[str, Any],
    *,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the actual fixture candidate, first task, and six-scenario proof."""
    source_sha = _source_sha(source_sha)
    required_build = {
        "schema_version",
        "kind",
        "fixture_only",
        "publication_status",
        "publication_eligible",
        "source_sha",
        "profile_id",
        "wheel_sha256",
        "inspection_sha256",
    }
    if not isinstance(build_record, dict) or set(build_record) != required_build:
        raise ValueError("fixture wheel build record fields are noncanonical")
    if (
        build_record["schema_version"] != 1
        or build_record["kind"] != "ucm-fixture-wheel-build"
        or build_record["fixture_only"] is not True
        or build_record["publication_status"] != "unpublished"
        or build_record["publication_eligible"] is not False
        or build_record["source_sha"] != source_sha
    ):
        raise ValueError("fixture wheel build record does not bind the source")
    inspection_sha256 = (
        "sha256:" + hashlib.sha256(canonical_bytes(wheel_record) + b"\n").hexdigest()
    )
    if (
        build_record["wheel_sha256"] != wheel_record.get("sha256")
        or build_record["inspection_sha256"] != inspection_sha256
        or build_record["profile_id"] != wheel_record.get("spec_id")
        or wheel_record.get("fixture_binding")
        != {
            "source_commit": source_sha,
            "profile_id": build_record["profile_id"],
            "marker_status": "passed",
        }
        or wheel_record.get("status") != "fixture-only"
        or wheel_record.get("trust_level") != "fixture-only"
        or wheel_record.get("published") is not False
        or wheel_record.get("publication_eligible") is not False
    ):
        raise ValueError("fixture wheel inspection does not match its build record")

    manifest = build_release_manifest()
    _, compatibility = validate_config()
    snapshot = {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": "docker.io/vllm/vllm-openai",
        "upstream_tag": "v0.10.2",
        "index_digest": "sha256:" + "1" * 64,
        "platforms": [
            {
                "os": "linux",
                "architecture": "amd64",
                "manifest_digest": "sha256:" + "2" * 64,
                "config_digest": "sha256:" + "3" * 64,
            },
            {
                "os": "linux",
                "architecture": "arm64",
                "manifest_digest": "sha256:" + "4" * 64,
                "config_digest": "sha256:" + "5" * 64,
            },
        ],
    }
    source_case = {
        "release_manifest": manifest,
        "wheel_records": [copy.deepcopy(wheel_record)],
        "spec_id": wheel_record["spec_id"],
        "upstream_snapshot": snapshot,
        "compatibility": compatibility,
        "compatibility_rule_id": "cuda-supported",
        "implementation_digest": image.implementation_digests()["aggregate_sha256"],
    }
    candidate = build_candidate(**source_case, fixture_mode=True)
    inventory = _inventory()
    first = reconcile(candidate, inventory)
    if first["task_count"] != 1 or first["tasks"][0]["revision"] != 1:
        raise ValueError("new fixture input must schedule exactly one r1 task")
    loop = verify_loop(source_case, run=run)
    scenarios = loop["payload"]["scenarios"]
    if (
        [item["name"] for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item["passed"] is True for item in scenarios)
        or loop["payload"]["publication_attempted"] is not False
    ):
        raise ValueError("fixture loop did not pass all deterministic scenarios")
    return {
        "source_sha": source_sha,
        "source_case": source_case,
        "candidate": candidate,
        "inventory": inventory,
        "first_reconcile": first,
        "image_input": {
            "source_case": source_case,
            "candidate": candidate,
            "task": first["tasks"][0],
            "inventory": inventory,
            "target_platform": "linux/amd64",
        },
        "loop_verification": loop,
    }


def complete_candidate_loop(
    prepared: dict[str, Any],
    image_result: dict[str, Any],
    *,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize a verified local result and require the second reconcile to be zero."""
    source_sha = _source_sha(source_sha)
    required_prepared = {
        "source_sha",
        "source_case",
        "candidate",
        "inventory",
        "first_reconcile",
        "image_input",
        "loop_verification",
    }
    if not isinstance(prepared, dict) or set(prepared) != required_prepared:
        raise ValueError("prepared loop fields are noncanonical")
    if prepared["source_sha"] != source_sha:
        raise ValueError("prepared loop source SHA mismatch")
    candidate = prepared["candidate"]
    first = prepared["first_reconcile"]
    image_input = prepared["image_input"]
    if (
        image_input.get("candidate") != candidate
        or image_input.get("inventory") != prepared["inventory"]
        or first.get("tasks") != [image_input.get("task")]
    ):
        raise ValueError("prepared image input is not the exact first reconcile task")
    if not isinstance(image_result, dict):
        raise ValueError("image result must be an object")
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    image_result = image.validate_image_result(image_result)
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    build_inputs = candidate["build_inputs"]
    wheel_input = build_inputs["wheel"]
    wheel_records = prepared["source_case"].get("wheel_records")
    if not isinstance(wheel_records, list) or len(wheel_records) != 1:
        raise ValueError("prepared loop must retain one exact wheel inspection")
    wheel_record = wheel_records[0]
    expected_wheel = {
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
    }
    target_platform = image_input["target_platform"]
    target_architecture = target_platform.split("/", 1)[1]
    upstream = build_inputs["upstream"]
    upstream_platforms = [
        item
        for item in upstream["platforms"]
        if item["architecture"] == target_architecture
    ]
    if len(upstream_platforms) != 1:
        raise ValueError("candidate does not have one exact target platform")
    upstream_platform = upstream_platforms[0]
    manifest = prepared["source_case"]["release_manifest"]
    expected_source = {
        "release_manifest_sha256": build_inputs["release_manifest_sha256"],
        "config_sha256": manifest["config_sha256"],
        "compatibility_sha256": manifest["compatibility_sha256"],
        "compatibility_rule_id": build_inputs["compatibility_rule_id"],
        "compatibility_rule_sha256": build_inputs["compatibility_rule_sha256"],
        "upstream_repository": upstream["repository"],
        "upstream_index_digest": upstream["index_digest"],
        "upstream_platform_manifest_digest": upstream_platform["manifest_digest"],
        "upstream_platform_config_digest": upstream_platform["config_digest"],
    }
    if (
        image_result["build_key_sha256"] != candidate["build_key_sha256"]
        or image_result["task_key"] != sha256_value(image_input["task"])
        or image_result["ucm_version"] != candidate["ucm_version"]
        or image_result["target_platform"] != target_platform
        or image_result["wheel"] != expected_wheel
        or image_result["source"] != expected_source
        or image_result["implementation"]["aggregate_sha256"]
        != build_inputs["implementation_digest"]
    ):
        raise ValueError("image result does not bind the exact candidate input closure")
    gates = image_result.get("gates")
    if (
        not isinstance(gates, dict)
        or set(gates) != REQUIRED_IMAGE_GATES
        or any(value != "passed" for value in gates.values())
    ):
        raise ValueError("image result required gates did not all pass")
    if (
        image_result.get("runtime_validation") != "external-required"
        or image_result.get("device_validation") != "external-required"
    ):
        raise ValueError(
            "fixture runtime and device validation must remain external-required"
        )
    oci_digest = image_result.get("oci", {}).get("digest")
    if not isinstance(oci_digest, str) or DIGEST_RE.fullmatch(oci_digest) is None:
        raise ValueError("image result OCI digest is invalid")

    task = image_input["task"]
    entry = {
        "repository": candidate["target_repository"],
        "tag": task["tag"],
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": oci_digest,
        "evidence_digest": oci_digest,
    }
    inventory = _inventory([entry])
    second = reconcile(candidate, inventory)
    if second["task_count"] != 0 or second["decision"] != "already-present":
        raise ValueError("completed fixture candidate did not reconcile to zero")

    accepted = {
        "a2": parse_upstream_tag("vllm-ascend", "v0.10.2")["npu_arch"],
        "a3": parse_upstream_tag("vllm-ascend", "v0.10.2-a3")["npu_arch"],
    }
    rejected: list[str] = []
    for suffix in ("310p", "a5"):
        try:
            parse_upstream_tag("vllm-ascend", f"v0.10.2-{suffix}")
        except ValueError:
            rejected.append(suffix)
    if accepted != {"a2": "a2", "a3": "a3"} or rejected != ["310p", "a5"]:
        raise ValueError("Ascend compatibility boundary is not A2/A3 only")
    loop = prepared["loop_verification"]
    if not isinstance(loop, dict) or set(loop) != {
        "schema_version",
        "kind",
        "run",
        "payload",
        "payload_sha256",
    }:
        raise ValueError("prepared loop verification envelope is noncanonical")
    recomputed_loop = verify_loop(prepared["source_case"], run=loop["run"])
    if loop != recomputed_loop:
        raise ValueError("prepared loop verification does not match recomputation")
    scenarios = loop.get("payload", {}).get("scenarios", [])
    if (
        [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item.get("passed") is True for item in scenarios)
        or loop.get("payload", {}).get("must_green") is not True
    ):
        raise ValueError("prepared deterministic scenario evidence is incomplete")
    source_batches = loop["payload"].get("operation_batches")
    if not isinstance(source_batches, list):
        raise ValueError("prepared loop is missing its operation ledger")
    if audit_operation_batches(source_batches) != loop["payload"].get(
        "zero_write_audit"
    ):
        raise ValueError("prepared zero-write audit does not match its ledger")
    operation_batches = copy.deepcopy(source_batches) + [
        copy.deepcopy(second["operations"])
    ]
    operation_audit = audit_operation_batches(operation_batches)
    if operation_audit["write_count"] != 0:
        raise ValueError("completed candidate attempted a write")
    write_audit = {
        **operation_audit,
        "ledger_sha256": sha256_value(operation_batches),
    }
    publication_attempted = (
        image_result["publication_attempted"] or write_audit["write_count"] != 0
    )
    if publication_attempted or image_result["unpublished"] is not True:
        raise ValueError("completed fixture candidate must remain unpublished")
    payload = {
        "schema_version": 1,
        "kind": "ucm-vllm-candidate-loop-payload",
        "source_sha": source_sha,
        "candidate_identity": {
            "repository": candidate["target_repository"],
            "tag": task["tag"],
            "build_key_sha256": candidate["build_key_sha256"],
        },
        "upstream_index_digest": candidate["build_inputs"]["upstream"]["index_digest"],
        "first_reconcile_sha256": sha256_value(first),
        "second_reconcile_sha256": sha256_value(second),
        "first_task_count": first["task_count"],
        "second_task_count": second["task_count"],
        "image_result_sha256": image_result["result_sha256"],
        "oci_digest": oci_digest,
        "loop_payload_sha256": loop["payload_sha256"],
        "scenarios": copy.deepcopy(scenarios),
        "compatibility": {"accepted": ["a2", "a3"], "rejected": ["310p", "a5"]},
        "required_gates": copy.deepcopy(gates),
        "runtime_validation": image_result["runtime_validation"],
        "device_validation": image_result["device_validation"],
        "expected_blocked": copy.deepcopy(loop["payload"]["expected_blockers"]),
        "publication": {
            "status": "blocked" if image_result["unpublished"] else "invalid",
            "attempted": publication_attempted,
        },
        "operation_batches": operation_batches,
        "write_audit": write_audit,
    }
    return {"second_reconcile": second, "evidence": _envelope(payload, run)}


def aggregate_release_evidence(
    *,
    build_record_path: Path,
    wheel_record_path: Path,
    wheel_path: Path,
    chart_result_path: Path,
    chart_package_path: Path,
    image_result_path: Path,
    oci_evidence_dir: Path,
    image_recipe_path: Path,
    image_metadata_path: Path,
    image_prepare_path: Path,
    buildkit_metadata_path: Path,
    image_archive_sha256_path: Path,
    completed_loop_path: Path,
    second_reconcile_path: Path,
    image_loop_path: Path,
    repository: str,
    ref: str,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reopen every release artifact and recompute the exact candidate closure."""
    source_sha = _source_sha(source_sha)
    build_record_path = Path(build_record_path)
    wheel_record_path = Path(wheel_record_path)
    wheel_path = Path(wheel_path)
    chart_result_path = Path(chart_result_path)
    chart_package_path = Path(chart_package_path)
    image_result_path = Path(image_result_path)
    oci_evidence_dir = Path(oci_evidence_dir)
    image_recipe_path = Path(image_recipe_path)
    image_metadata_path = Path(image_metadata_path)
    image_prepare_path = Path(image_prepare_path)
    buildkit_metadata_path = Path(buildkit_metadata_path)
    image_archive_sha256_path = Path(image_archive_sha256_path)
    completed_loop_path = Path(completed_loop_path)
    second_reconcile_path = Path(second_reconcile_path)
    image_loop_path = Path(image_loop_path)

    build_record = _load_canonical_json(build_record_path, "wheel build record")
    wheel_record = _load_canonical_json(wheel_record_path, "wheel inspection")
    actual_wheel_sha256 = _file_sha256(wheel_path)
    with tempfile.TemporaryDirectory() as temporary:
        expected_fixture = wheel.build_fixture_wheel(
            Path(temporary) / "wheel",
            source_sha,
            build_record.get("profile_id"),
        )
        expected_wheel_path = Path(expected_fixture["wheel_path"])
        if (
            wheel_path.read_bytes() != expected_wheel_path.read_bytes()
            or wheel_record != expected_fixture["inspection"]
            or build_record != expected_fixture["build_record"]
        ):
            raise ValueError(
                "actual fixture wheel/build/inspection differs from authoritative rebuild"
            )
    if wheel_path.name != wheel_record.get("filename"):
        raise ValueError("wheel filename does not match its inspection")
    inspected = wheel.inspect_wheel(
        wheel_path,
        build_record.get("profile_id"),
        actual_wheel_sha256,
        "fixture",
    )
    if inspected != wheel_record:
        raise ValueError("actual wheel does not match its canonical inspection")
    if build_record.get("inspection_sha256") != _file_sha256(wheel_record_path):
        raise ValueError("wheel build record does not bind inspection bytes")
    prepared = prepare_candidate_loop(
        build_record,
        wheel_record,
        source_sha=source_sha,
        run={},
    )

    chart_result = _load_canonical_json(chart_result_path, "Chart result")
    with tempfile.TemporaryDirectory() as temporary:
        expected_chart_dir = Path(temporary) / "chart"
        expected_chart_result = chart.package_chart(expected_chart_dir)
        expected_chart_package = expected_chart_dir / expected_chart_result["filename"]
        if chart_result != expected_chart_result:
            raise ValueError("Chart result does not match fresh validation")
        if chart_package_path.name != expected_chart_result["filename"]:
            raise ValueError("Chart package filename is noncanonical")
        if not chart_package_path.is_file():
            raise ValueError("Chart package is not a regular file")
        if chart_package_path.read_bytes() != expected_chart_package.read_bytes():
            raise ValueError("Chart package bytes do not match fresh validation")

    image_result = image.validate_image_result(
        _load_canonical_json(image_result_path, "image result")
    )
    if (
        image.require_fixture_base_authority(
            image_result["base"], image_result["target_platform"]
        )
        != image_result["base"]
    ):
        raise ValueError("image result base is not the authoritative fixture base")
    if not buildkit_metadata_path.is_file():
        raise ValueError("BuildKit metadata is not a regular file")
    try:
        buildkit_metadata = json.loads(buildkit_metadata_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"BuildKit metadata is invalid JSON: {error}") from error
    compact_oci = image.validate_compact_oci_evidence(
        oci_evidence_dir,
        image_result=image_result,
        image_recipe_path=image_recipe_path,
        image_metadata_path=image_metadata_path,
        image_prepare_path=image_prepare_path,
        wheel_path=wheel_path,
        buildkit_metadata=buildkit_metadata,
    )
    if compact_oci["wheel_sha256"] != actual_wheel_sha256:
        raise ValueError("compact OCI evidence does not bind the actual wheel")
    if not image_archive_sha256_path.is_file():
        raise ValueError("OCI archive digest record is not a regular file")
    archive_record = image_archive_sha256_path.read_text(encoding="utf-8").strip()
    archive_parts = archive_record.split()
    if (
        len(archive_parts) != 2
        or "sha256:" + archive_parts[0] != compact_oci["archive_sha256"]
        or Path(archive_parts[1]).name != "image.oci.tar"
    ):
        raise ValueError("OCI archive digest record does not match compact evidence")
    completed = _load_canonical_json(completed_loop_path, "completed loop")
    second_reconcile = _load_canonical_json(second_reconcile_path, "second reconcile")
    image_loop = _load_canonical_json(image_loop_path, "image loop evidence")
    if set(completed) != {"second_reconcile", "evidence"}:
        raise ValueError("completed loop fields are noncanonical")
    completed_evidence = completed.get("evidence")
    if not isinstance(completed_evidence, dict) or set(completed_evidence) != {
        "payload",
        "payload_sha256",
        "github",
    }:
        raise ValueError("completed loop evidence fields are noncanonical")
    recomputed = complete_candidate_loop(
        prepared,
        image_result,
        source_sha=source_sha,
        run=completed_evidence["github"],
    )
    if completed != recomputed:
        raise ValueError("completed loop does not match full recomputation")
    if second_reconcile != recomputed["second_reconcile"]:
        raise ValueError("standalone second reconcile disagrees with completed loop")
    if image_loop != recomputed["evidence"]:
        raise ValueError("image loop envelope disagrees with completed loop")

    image_payload = recomputed["evidence"]["payload"]
    scenarios = image_payload["scenarios"]
    operation_batches = image_payload["operation_batches"]
    derived_operation_audit = audit_operation_batches(operation_batches)
    derived_write_audit = {
        **derived_operation_audit,
        "ledger_sha256": sha256_value(operation_batches),
    }
    if image_payload["write_audit"] != derived_write_audit:
        raise ValueError("completed write audit does not match its operation ledger")
    must_green = {
        "fixture_wheel": (
            build_record["wheel_sha256"] == actual_wheel_sha256
            and inspected["status"] == "fixture-only"
            and inspected["published"] is False
        ),
        "helm_cuda_a2_a3": (
            chart_result["rendered_cases"] == ["cuda", "a2", "a3"]
            and set(chart_result["checks"].values()) == {"passed"}
            and chart_result["status"] == "candidate-verified"
        ),
        "install_only_image": (
            set(image_result["gates"]) == REQUIRED_IMAGE_GATES
            and set(image_result["gates"].values()) == {"passed"}
            and image_result["status"] == "fixture-verified-unpublished"
        ),
        "second_reconcile_zero": (
            second_reconcile["task_count"] == 0
            and second_reconcile["decision"] == "already-present"
        ),
    }
    if (
        not all(must_green.values())
        or [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item.get("passed") is True for item in scenarios)
        or image_payload["publication"] != {"status": "blocked", "attempted": False}
        or derived_write_audit["write_count"] != 0
    ):
        raise ValueError("aggregate candidate closure did not pass every required gate")
    payload = {
        "mode": "fork-dry-run",
        "repository": repository,
        "ref": ref,
        "source_sha": source_sha,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "must_green": must_green,
        "scenarios": copy.deepcopy(scenarios),
        "compatibility": copy.deepcopy(image_payload["compatibility"]),
        "candidate_identity": copy.deepcopy(image_payload["candidate_identity"]),
        "artifact_digests": {
            "wheel_sha256": wheel_record["sha256"],
            "wheel_inspection_sha256": build_record["inspection_sha256"],
            "chart_sha256": chart_result["sha256"],
            "chart_tree_sha256": chart_result["release_tree_sha256"],
            "upstream_index_digest": image_payload["upstream_index_digest"],
            "oci_digest": image_payload["oci_digest"],
            "image_result_sha256": image_payload["image_result_sha256"],
            "first_reconcile_sha256": image_payload["first_reconcile_sha256"],
            "second_reconcile_sha256": image_payload["second_reconcile_sha256"],
            "image_loop_payload_sha256": image_loop["payload_sha256"],
            "oci_evidence_closure_sha256": compact_oci["closure_sha256"],
            "oci_manifest_digest": compact_oci["oci_digest"],
            "oci_config_digest": compact_oci["config_digest"],
            "oci_archive_sha256": compact_oci["archive_sha256"],
            "build_record_file_sha256": _file_sha256(build_record_path),
            "image_result_file_sha256": _file_sha256(image_result_path),
            "second_reconcile_file_sha256": _file_sha256(second_reconcile_path),
        },
        "required_gates": copy.deepcopy(image_payload["required_gates"]),
        "expected_blocked": [
            "production-wheel-builders",
            "accelerator-runtime",
            "cuda-device",
            "ascend-a2-device",
            "ascend-a3-device",
            "protected-environment",
            "registry-publication-and-readback",
        ],
        "publication": copy.deepcopy(image_payload["publication"]),
        "operation_batches": copy.deepcopy(operation_batches),
        "write_audit": copy.deepcopy(derived_write_audit),
    }
    evidence = _envelope(payload, run)
    evidence["github"]["non_deterministic_artifact_file_sha256"] = {
        "completed_loop": _file_sha256(completed_loop_path),
        "image_loop": _file_sha256(image_loop_path),
        "buildkit_metadata": _file_sha256(buildkit_metadata_path),
    }
    return evidence


def _inventory(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    inventory = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": entries or [],
    }
    inventory["inventory_sha256"] = inventory_digest(inventory)
    return inventory


def _entry(
    candidate: dict[str, Any],
    digest: str,
    *,
    revision: int = 1,
    observed_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "repository": candidate["target_repository"],
        "tag": f"{candidate['tag_base']}-r{revision}",
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": observed_digest or digest,
        "evidence_digest": digest,
    }


def expect_blocker(code: str, operation: Callable[[], object]) -> str:
    """Accept only the exact typed blocker expected by a verification scenario."""
    try:
        operation()
    except RegistryBlocker as error:
        if error.code != code:
            raise ValueError(f"expected blocker {code}, got {error.code}") from error
        return error.code
    raise ValueError(f"expected blocker {code} was not raised")


def _validate_operation_reference(reference_kind: str, reference: object) -> None:
    if not isinstance(reference, str):
        raise ValueError("operation has malformed reference")
    if reference_kind == "digest":
        valid = DIGEST_RE.fullmatch(reference) is not None
    elif reference_kind == "upstream-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and REPOSITORY_RE.fullmatch(repository) is not None
            and repository.rsplit("/", 1)[-1] in TARGET_REPOSITORIES
            and DIGEST_RE.fullmatch(digest) is not None
        )
    elif reference_kind == "upstream-tag":
        repository, separator, tag = reference.rpartition(":")
        valid = separator == ":" and REPOSITORY_RE.fullmatch(repository) is not None
        if valid:
            try:
                parse_upstream_tag(repository.rsplit("/", 1)[-1], tag)
            except ValueError:
                valid = False
    elif reference_kind == "target-tag":
        matching = [
            repository
            for repository in TARGET_REPOSITORIES.values()
            if reference.startswith(repository + ":")
        ]
        valid = len(matching) == 1
        if valid:
            try:
                validate_public_tag(reference.removeprefix(matching[0] + ":"))
            except ValueError:
                valid = False
    else:  # pragma: no cover - immutable mapping owns this branch.
        raise ValueError(f"unknown operation reference contract: {reference_kind}")
    if not valid:
        raise ValueError(
            f"operation has malformed reference for {reference_kind}: {reference}"
        )


def audit_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive zero-write evidence from emitted operation ledgers."""
    if not isinstance(operations, list):
        raise ValueError("operation ledger must be an array")
    operation_types: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {
            "type",
            "capability",
            "reference",
        }:
            raise ValueError(
                "malformed ledger entry: expected exactly type/capability/reference"
            )
        operation_type = operation["type"]
        if operation_type in KNOWN_WRITE_OPERATION_TYPES:
            raise ValueError(
                f"write-capable operation type is forbidden: {operation_type}"
            )
        if operation_type not in OPERATION_CONTRACTS:
            raise ValueError(f"unknown operation type: {operation_type}")
        expected_capability, reference_kind = OPERATION_CONTRACTS[operation_type]
        if operation["capability"] != expected_capability:
            raise ValueError(
                f"operation capability mismatch for {operation_type}: "
                f"expected {expected_capability}, got {operation['capability']}"
            )
        _validate_operation_reference(reference_kind, operation["reference"])
        identity = (operation_type, operation["reference"])
        if identity in identities:
            raise ValueError(f"duplicate operation identity: {identity}")
        identities.add(identity)
        operation_types.add(operation_type)
    return {
        "operation_count": len(operations),
        "operation_types": sorted(operation_types),
        "write_capable_operations": [],
        "write_count": 0,
    }


def audit_operation_batches(
    operation_batches: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Audit each producer ledger independently, then aggregate proven summaries."""
    if not isinstance(operation_batches, list):
        raise ValueError("operation ledger batches must be an array")
    audits = [audit_operations(batch) for batch in operation_batches]
    return {
        "operation_count": sum(audit["operation_count"] for audit in audits),
        "operation_types": sorted(
            {
                operation_type
                for audit in audits
                for operation_type in audit["operation_types"]
            }
        ),
        "write_capable_operations": [],
        "write_count": 0,
    }


def _required_blockers(case: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    snapshot = copy.deepcopy(case["upstream_snapshot"])
    snapshot["platforms"] = [
        item for item in snapshot["platforms"] if item["architecture"] != "arm64"
    ]
    stable = _entry(candidate, case["upstream_snapshot"]["index_digest"])
    conflicting = copy.deepcopy(stable)
    conflicting["observed_digest"] = "sha256:" + "f" * 64
    production_case = copy.deepcopy(case)
    results = [
        expect_blocker(
            "duplicate-conflicting-inventory",
            lambda: reconcile(candidate, _inventory([stable, conflicting])),
        ),
        expect_blocker("missing-linux-arm64", lambda: validate_snapshot(snapshot)),
        expect_blocker(
            "production-wheel-unpublished",
            lambda: build_candidate(**production_case, fixture_mode=False),
        ),
    ]
    return sorted(results)


def _artifact_digests(
    candidate: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    return {
        "release_manifest_sha256": candidate["build_inputs"]["release_manifest_sha256"],
        "wheel": copy.deepcopy(candidate["build_inputs"]["wheel"]),
        "upstream": {
            "index_digest": snapshot["index_digest"],
            "platforms": copy.deepcopy(snapshot["platforms"]),
        },
        "implementation_digest": candidate["build_inputs"]["implementation_digest"],
        "compatibility_rule_sha256": candidate["build_inputs"][
            "compatibility_rule_sha256"
        ],
        "build_key_sha256": candidate["build_key_sha256"],
        "tag_family_sha256": candidate["tag_family_sha256"],
    }


def verify_loop(
    case: dict[str, Any], *, run: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Exercise six fixture scenarios and hash only their deterministic payload."""
    if not isinstance(case, dict):
        raise ValueError("loop verification input must be an object")
    required = {
        "release_manifest",
        "wheel_records",
        "spec_id",
        "upstream_snapshot",
        "compatibility",
        "compatibility_rule_id",
        "implementation_digest",
    }
    if set(case) != required:
        raise ValueError(
            "loop verification fields mismatch: "
            f"missing={sorted(required - set(case))}, extra={sorted(set(case) - required)}"
        )
    requested_snapshot = validate_snapshot(case["upstream_snapshot"])
    scan_result = scan_registry(
        requested_snapshot["repository"],
        requested_snapshot["upstream_tag"],
        fixture=requested_snapshot,
    )
    fixture_case = {**case, "upstream_snapshot": scan_result["snapshot"]}
    candidate = build_candidate(**fixture_case, fixture_mode=True)
    snapshot = scan_result["snapshot"]
    digest = snapshot["index_digest"]

    new_result = reconcile(candidate, _inventory())
    stable_inventory = _inventory([_entry(candidate, digest)])
    same_result = reconcile(candidate, stable_inventory)
    drift_inventory = _inventory(
        [
            _entry(
                candidate,
                digest,
                observed_digest="sha256:" + "f" * 64,
            )
        ]
    )
    drift_result = reconcile(candidate, drift_inventory)
    blockers = _required_blockers(fixture_case, candidate)

    first_fixture_result = reconcile(candidate, _inventory())
    completed_entry = _entry(candidate, digest)
    final_fixture_result = reconcile(candidate, _inventory([completed_entry]))
    operation_batches = [
        scan_result["operations"],
        *[
            result["operations"]
            for result in (
                new_result,
                same_result,
                drift_result,
                first_fixture_result,
                final_fixture_result,
            )
        ],
    ]
    zero_write_audit = audit_operation_batches(operation_batches)
    digest_chain = _artifact_digests(candidate, snapshot)
    platforms = digest_chain["upstream"]["platforms"]
    complete_chain = (
        len(platforms) == 2
        and {item["architecture"] for item in platforms} == {"amd64", "arm64"}
        and all(item["manifest_digest"] and item["config_digest"] for item in platforms)
    )

    scenarios = [
        {
            "name": "new-input-one-task",
            "passed": new_result["task_count"] == 1
            and new_result["tasks"][0]["revision"] == 1,
            "task_count": new_result["task_count"],
            "task_tags": [item["tag"] for item in new_result["tasks"]],
        },
        {
            "name": "identical-input-zero-tasks",
            "passed": same_result["task_count"] == 0,
            "task_count": same_result["task_count"],
            "task_tags": [],
        },
        {
            "name": "tag-digest-drift-r2",
            "passed": drift_result["task_count"] == 1
            and drift_result["tasks"][0]["revision"] == 2
            and drift_result["inventory"] == drift_inventory,
            "task_count": drift_result["task_count"],
            "task_tags": [item["tag"] for item in drift_result["tasks"]],
        },
        {
            "name": "complete-digest-chain",
            "passed": complete_chain,
            "platform_count": len(platforms),
        },
        {
            "name": "required-failures-block",
            "passed": blockers == EXPECTED_BLOCKERS,
            "blockers": blockers,
        },
        {
            "name": "fixture-candidate-full-zero-reconcile",
            "passed": first_fixture_result["task_count"] == 1
            and final_fixture_result["task_count"] == 0,
            "initial_task_count": first_fixture_result["task_count"],
            "final_task_count": final_fixture_result["task_count"],
        },
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-payload",
        "must_green": all(item["passed"] for item in scenarios),
        "scenarios": scenarios,
        "artifact_digests": digest_chain,
        "compatibility_rule_id": case["compatibility_rule_id"],
        "expected_blockers": {
            "scenario_codes": copy.deepcopy(EXPECTED_BLOCKERS),
            "production": copy.deepcopy(case["release_manifest"]["blockers"]),
        },
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": zero_write_audit["write_count"] != 0,
        "operation_batches": copy.deepcopy(operation_batches),
        "zero_write_audit": zero_write_audit,
    }
    return {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-envelope",
        "run": copy.deepcopy(run or {}),
        "payload": payload,
        "payload_sha256": sha256_value(payload),
    }
