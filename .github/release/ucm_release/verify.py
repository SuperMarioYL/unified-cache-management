"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import hashlib
import re
from types import MappingProxyType
from typing import Any, Callable

from . import image
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
    if not isinstance(image_result, dict) or "result_sha256" not in image_result:
        raise ValueError("image result is missing its canonical identity")
    result_payload = {
        key: copy.deepcopy(value)
        for key, value in image_result.items()
        if key != "result_sha256"
    }
    if image_result["result_sha256"] != sha256_value(result_payload):
        raise ValueError("image result digest does not match its payload")
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    if (
        image_result.get("build_key_sha256") != candidate["build_key_sha256"]
        or image_result.get("wheel", {}).get("sha256")
        != candidate["build_inputs"]["wheel"]["sha256"]
    ):
        raise ValueError("image result does not bind the candidate build key and wheel")
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
    scenarios = loop.get("payload", {}).get("scenarios", [])
    if [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS or loop.get(
        "payload", {}
    ).get("must_green") is not True:
        raise ValueError("prepared deterministic scenario evidence is incomplete")
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
        "publication": {"status": "blocked", "attempted": False},
        "write_audit": copy.deepcopy(loop["payload"]["zero_write_audit"]),
    }
    return {"second_reconcile": second, "evidence": _envelope(payload, run)}


def aggregate_release_evidence(
    build_record: dict[str, Any],
    wheel_record: dict[str, Any],
    chart_result: dict[str, Any],
    image_loop: dict[str, Any],
    *,
    repository: str,
    ref: str,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind wheel, Chart, OCI, reconciliation, blockers, and zero writes."""
    source_sha = _source_sha(source_sha)
    if build_record.get("source_sha") != source_sha:
        raise ValueError("wheel build record source does not match aggregate source")
    if (
        build_record.get("wheel_sha256") != wheel_record.get("sha256")
        or wheel_record.get("status") != "fixture-only"
        or wheel_record.get("published") is not False
    ):
        raise ValueError("aggregate wheel is not the exact fixture artifact")
    if chart_result.get("status") != "candidate-verified" or chart_result.get(
        "rendered_cases"
    ) != ["cuda", "a2", "a3"]:
        raise ValueError("aggregate Chart did not pass CUDA/A2/A3")
    if not isinstance(image_loop, dict) or set(image_loop) != {
        "payload",
        "payload_sha256",
        "github",
    }:
        raise ValueError("image loop envelope fields are noncanonical")
    image_payload = image_loop["payload"]
    if (
        image_loop["payload_sha256"] != sha256_value(image_payload)
        or image_payload.get("source_sha") != source_sha
        or image_payload.get("second_task_count") != 0
        or [item.get("name") for item in image_payload.get("scenarios", [])]
        != REQUIRED_SCENARIOS
    ):
        raise ValueError("image loop does not bind the completed source closure")
    payload = {
        "mode": "fork-dry-run",
        "repository": repository,
        "ref": ref,
        "source_sha": source_sha,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "must_green": {
            "fixture_wheel": True,
            "helm_cuda_a2_a3": True,
            "install_only_image": image_payload["image_result_sha256"].startswith(
                "sha256:"
            ),
            "second_reconcile_zero": True,
        },
        "scenarios": copy.deepcopy(image_payload["scenarios"]),
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
        "publication": {"status": "blocked", "attempted": False},
        "write_audit": {
            "pull_request": False,
            "tag": False,
            "release": False,
            "package": False,
            "upstream": False,
        },
        "operation_audit": copy.deepcopy(image_payload["write_audit"]),
    }
    if not all(payload["must_green"].values()):
        raise ValueError("aggregate candidate gates did not all pass")
    return _envelope(payload, run)


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
        "zero_write_audit": zero_write_audit,
    }
    return {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-envelope",
        "run": copy.deepcopy(run or {}),
        "payload": payload,
        "payload_sha256": sha256_value(payload),
    }
