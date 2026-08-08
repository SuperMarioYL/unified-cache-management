"""Behavioral contract for read-only registry reconciliation and loop evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
PYTHON = sys.executable
DIGESTS = {
    name: "sha256:" + character * 64
    for name, character in {
        "index": "1",
        "amd64_manifest": "2",
        "amd64_config": "3",
        "arm64_manifest": "4",
        "arm64_config": "5",
        "wheel": "6",
        "implementation": "7",
        "observed_drift": "8",
    }.items()
}


def _modules():
    sys.path.insert(0, str(RELEASE_ROOT))
    return (
        importlib.import_module("ucm_release.registry"),
        importlib.import_module("ucm_release.verify"),
    )


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": "docker.io/vllm/vllm-ascend",
        "upstream_tag": "v0.10.2-a3-openeuler",
        "index_digest": DIGESTS["index"],
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


def _fixture_wheel(tmp_path: Path, spec: dict[str, object], version: str) -> Path:
    platform = {"amd64": "x86_64", "arm64": "aarch64"}[spec["cpu_arch"]]
    tag = f"{spec['python_abi']}-{spec['python_abi']}-linux_{platform}"
    path = tmp_path / f"uc_manager-{version}-{tag}.whl"
    dist_info = f"uc_manager-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        members = {
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
                    "Generator: task3-fixture",
                    "Root-Is-Purelib: false",
                    f"Tag: {tag}",
                    "",
                ]
            ),
        }
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return path


def _case(tmp_path: Path) -> dict[str, object]:
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    wheel = importlib.import_module("ucm_release.wheel")
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
    wheel_path = _fixture_wheel(tmp_path, spec, manifest["ucm_version"])
    wheel_sha256 = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    wheel_record = wheel.inspect_wheel(
        wheel_path,
        spec["spec_id"],
        wheel_sha256,
        "fixture",
    )
    _, compatibility = core.validate_config()
    return {
        "release_manifest": manifest,
        "wheel_records": [wheel_record],
        "spec_id": spec["spec_id"],
        "upstream_snapshot": _snapshot(),
        "compatibility": compatibility,
        "compatibility_rule_id": "ascend-supported",
        "implementation_digest": DIGESTS["implementation"],
    }


def _inventory(entries: list[dict[str, object]] | None = None) -> dict[str, object]:
    registry, _ = _modules()
    inventory = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": entries or [],
    }
    inventory["inventory_sha256"] = registry.inventory_digest(inventory)
    return inventory


def _entry(candidate: dict[str, object], *, drift: bool = False) -> dict[str, object]:
    return {
        "repository": candidate["target_repository"],
        "tag": candidate["tag_base"] + "-r1",
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": (DIGESTS["observed_drift"] if drift else DIGESTS["index"]),
        "evidence_digest": DIGESTS["index"],
    }


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


def test_loop_verify_aggregates_the_six_required_fixture_scenarios(
    tmp_path: Path,
) -> None:
    """Removing any reconcile transition or blocker must make the loop non-green."""
    _, verify = _modules()

    envelope = verify.verify_loop(
        _case(tmp_path),
        run={"id": "fixture-run-a", "attempt": 3, "started_at": "later"},
    )
    payload = envelope["payload"]

    assert payload["must_green"] is True
    assert [item["name"] for item in payload["scenarios"]] == [
        "new-input-one-task",
        "identical-input-zero-tasks",
        "tag-digest-drift-r2",
        "complete-digest-chain",
        "required-failures-block",
        "fixture-candidate-full-zero-reconcile",
    ]
    assert all(item["passed"] is True for item in payload["scenarios"])
    assert payload["scenarios"][0]["task_tags"] == [
        "v0.10.2-a3-openeuler-ucm-0.5.0rc1-r1"
    ]
    assert payload["scenarios"][1]["task_count"] == 0
    assert payload["scenarios"][2]["task_tags"] == [
        "v0.10.2-a3-openeuler-ucm-0.5.0rc1-r2"
    ]
    assert payload["expected_blockers"]["scenario_codes"] == [
        "duplicate-conflicting-inventory",
        "missing-linux-arm64",
        "production-wheel-unpublished",
    ]
    assert (
        payload["expected_blockers"]["production"]
        == _case(tmp_path)["release_manifest"]["blockers"]
    )
    assert payload["fixture_only"] is True
    assert payload["unpublished"] is True
    assert payload["publication_attempted"] is False
    assert payload["zero_write_audit"] == {
        "operation_count": 9,
        "operation_types": [
            "build-plan",
            "fixture-read",
            "registry-inventory-read",
        ],
        "write_capable_operations": [],
        "write_count": 0,
    }
    with pytest.raises(ValueError, match="unrelated verifier failure"):
        verify.expect_blocker(
            "missing-linux-arm64",
            lambda: (_ for _ in ()).throw(ValueError("unrelated verifier failure")),
        )
    with pytest.raises(ValueError, match="write-capable"):
        verify.audit_operations(
            [{"type": "registry-push", "capability": "write", "reference": "x"}]
        )


def test_reconcile_schedules_r1_skips_identity_and_preserves_drifted_r1(
    tmp_path: Path,
) -> None:
    """A reconciler that overwrites or rebuilds an identical input breaks idempotency."""
    registry, _ = _modules()
    candidate = registry.build_candidate(**_case(tmp_path), fixture_mode=True)

    first = registry.reconcile(candidate, _inventory())
    same = registry.reconcile(candidate, _inventory([_entry(candidate)]))
    drift_entry = _entry(candidate, drift=True)
    drifted = registry.reconcile(candidate, _inventory([drift_entry]))

    assert first["task_count"] == 1
    assert first["tasks"][0]["revision"] == 1
    assert same["task_count"] == 0
    assert same["decision"] == "already-present"
    assert drifted["task_count"] == 1
    assert drifted["tasks"][0]["revision"] == 2
    assert drifted["tasks"][0]["reason"] == "tag-digest-drift"
    assert drifted["inventory"] == _inventory([drift_entry])
    assert drifted["publication_attempted"] is False
    assert first["tasks"][0]["precondition"] == {
        "type": "tag-absent",
        "repository": candidate["target_repository"],
        "tag": candidate["tag_base"] + "-r1",
        "inventory_sha256": _inventory()["inventory_sha256"],
    }
    assert first["tasks"][0]["concurrency_key"] == candidate["tag_family_sha256"]

    stale_again = registry.reconcile(candidate, _inventory())
    changed_entry = _entry(candidate)
    changed_entry["tag"] = candidate["tag_base"] + "-r99"
    changed_entry["build_key_sha256"] = "sha256:" + "a" * 64
    changed = registry.reconcile(candidate, _inventory([changed_entry]))
    assert stale_again["tasks"][0]["precondition"] == first["tasks"][0]["precondition"]
    assert changed["tasks"][0]["precondition"] != first["tasks"][0]["precondition"]
    bad_inventory = _inventory()
    bad_inventory["inventory_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="inventory digest mismatch"):
        registry.reconcile(candidate, bad_inventory)
    second_entry = copy.deepcopy(changed_entry)
    second_entry["tag"] = candidate["tag_base"] + "-r98"
    second_entry["build_key_sha256"] = "sha256:" + "b" * 64
    assert (
        _inventory([changed_entry, second_entry])["inventory_sha256"]
        == _inventory([second_entry, changed_entry])["inventory_sha256"]
    )


@pytest.mark.parametrize(
    ("product", "tag", "arch", "operating_system"),
    [
        ("vllm-openai", "v0.10.2", "na", "ubuntu-22.04"),
        ("vllm-ascend", "v0.10.2", "a2", "ubuntu-22.04"),
        ("vllm-ascend", "v0.10.2-a3", "a3", "ubuntu-22.04"),
        ("vllm-ascend", "v0.10.2-openeuler", "a2", "openEuler-24.03"),
        ("vllm-ascend", "v0.10.2rc2-a3-openeuler", "a3", "openEuler-24.03"),
    ],
)
def test_upstream_parser_retains_only_canonical_device_and_os_tags(
    product: str, tag: str, arch: str, operating_system: str
) -> None:
    """Defaulting or suffix parsing must not lose the A2/A3/openEuler boundary."""
    registry, _ = _modules()

    parsed = registry.parse_upstream_tag(product, tag)

    assert parsed["exact_upstream_tag"] == tag
    assert parsed["npu_arch"] == arch
    assert parsed["operating_system"] == operating_system
    assert parsed["target_repository"] == (
        "ghcr.io/modelengine-group/vllm-openai"
        if product == "vllm-openai"
        else "ghcr.io/modelengine-group/vllm-ascend"
    )


@pytest.mark.parametrize(
    ("product", "tag"),
    [
        ("vllm-openai", "0.10.2"),
        ("vllm-openai", "v01.10.2"),
        ("vllm-openai", "v0.10.2rc0"),
        ("vllm-openai", "v0.10.2rc01"),
        ("vllm-openai", "v0.10.2-nightly"),
        ("vllm-openai", "v0.10.2-amd64"),
        ("vllm-ascend", "v0.10.2-a2"),
        ("vllm-ascend", "v0.10.2-310p"),
        ("vllm-ascend", "v0.10.2-a5"),
        ("vllm-ascend", "v0.10.2-dev"),
        ("vllm-ascend", "v0.10.2-a3-amd64"),
        ("vllm-ascend", "v0.10.2-openeuler-a3"),
    ],
)
def test_upstream_parser_rejects_noncanonical_and_excluded_tags(
    product: str, tag: str
) -> None:
    """Unsupported version, device, channel, and architecture extras fail closed."""
    registry, _ = _modules()

    with pytest.raises(ValueError):
        registry.parse_upstream_tag(product, tag)


def test_snapshot_and_build_identity_bind_every_immutable_input(tmp_path: Path) -> None:
    """Dropping any platform/config/wheel/rule/implementation input changes identity."""
    registry, _ = _modules()
    case = _case(tmp_path)
    baseline = registry.build_candidate(**case, fixture_mode=True)

    assert baseline["target_repository"] == ("ghcr.io/modelengine-group/vllm-ascend")
    assert baseline["tag_base"] == "v0.10.2-a3-openeuler-ucm-0.5.0rc1"
    assert baseline["tag_family_sha256"].startswith("sha256:")
    assert baseline["build_key_sha256"].startswith("sha256:")
    assert baseline["build_inputs"]["upstream"]["platforms"] == _snapshot()["platforms"]

    mutations = []
    changed_manifest = copy.deepcopy(case)
    changed_manifest["release_manifest"]["config_sha256"] = "sha256:" + "d" * 64
    mutations.append(changed_manifest)
    changed_wheel = copy.deepcopy(case)
    changed_wheel["wheel_records"][0]["sha256"] = "sha256:" + "a" * 64
    mutations.append(changed_wheel)
    changed_platform = copy.deepcopy(case)
    changed_platform["upstream_snapshot"]["platforms"][0]["config_digest"] = (
        "sha256:" + "b" * 64
    )
    mutations.append(changed_platform)
    changed_rule = copy.deepcopy(case)
    ascend_rule = next(
        item
        for item in changed_rule["compatibility"]["rules"]
        if item["id"] == "ascend-supported"
    )
    ascend_rule["id"] = "ascend-supported-v2"
    changed_rule["compatibility_rule_id"] = "ascend-supported-v2"
    changed_rule["release_manifest"]["compatibility_sha256"] = registry.sha256_value(
        changed_rule["compatibility"]
    )
    mutations.append(changed_rule)
    changed_implementation = copy.deepcopy(case)
    changed_implementation["implementation_digest"] = "sha256:" + "c" * 64
    mutations.append(changed_implementation)

    assert all(
        registry.build_candidate(**mutation, fixture_mode=True)["build_key_sha256"]
        != baseline["build_key_sha256"]
        for mutation in mutations
    )
    assert (
        registry.with_revision(baseline, 9)["build_key_sha256"]
        == baseline["build_key_sha256"]
    )
    assert baseline["build_inputs"]["compatibility_rule"]["id"] == ("ascend-supported")
    assert baseline["build_inputs"]["compatibility_rule_sha256"].startswith("sha256:")

    cross_pair = copy.deepcopy(case)
    cross_pair["compatibility_rule_id"] = "cuda-supported"
    with pytest.raises(ValueError, match="compatibility"):
        registry.build_candidate(**cross_pair, fixture_mode=True)
    semantic_mutation = copy.deepcopy(case)
    mutated_rule = next(
        item
        for item in semantic_mutation["compatibility"]["rules"]
        if item["id"] == "ascend-supported"
    )
    mutated_rule["accelerator_runtimes"].remove("cann-9.0.0")
    semantic_mutation["release_manifest"]["compatibility_sha256"] = (
        registry.sha256_value(semantic_mutation["compatibility"])
    )
    with pytest.raises(ValueError, match="compatibility"):
        registry.build_candidate(**semantic_mutation, fixture_mode=True)

    for mutation in (
        lambda value: value["platforms"].pop(),
        lambda value: value.update(index_digest="latest"),
    ):
        bad = _snapshot()
        mutation(bad)
        with pytest.raises(ValueError):
            registry.validate_snapshot(bad)


def test_inventory_tag_and_wheel_boundaries_fail_closed(tmp_path: Path) -> None:
    """Conflicting inventory, ambiguous wheels, and unpublished production never plan."""
    registry, _ = _modules()
    case = _case(tmp_path)
    candidate = registry.build_candidate(**case, fixture_mode=True)
    conflict = _entry(candidate)
    duplicate = copy.deepcopy(conflict)
    duplicate["observed_digest"] = DIGESTS["observed_drift"]

    with pytest.raises(ValueError, match="conflicting"):
        registry.reconcile(candidate, _inventory([conflict, duplicate]))
    with pytest.raises(ValueError):
        registry.with_revision(candidate, 0)
    with pytest.raises(ValueError):
        registry.validate_public_tag(candidate["tag_base"] + "-r01")
    with pytest.raises(ValueError):
        registry.build_candidate(
            **{**case, "wheel_records": case["wheel_records"] * 2},
            fixture_mode=True,
        )
    with pytest.raises(ValueError, match="unpublished"):
        registry.build_candidate(**case, fixture_mode=False)
    forged = copy.deepcopy(case["wheel_records"][0])
    forged.update(
        source_kind="builder-candidate",
        status="published",
        trust_level="registry-published",
        published=True,
        publication_eligible=True,
    )
    with pytest.raises(ValueError, match="Task 2"):
        registry.build_candidate(
            **{**case, "wheel_records": [forged]},
            fixture_mode=False,
        )
    forged_candidate = copy.deepcopy(candidate)
    forged_candidate["fixture_only"] = False
    with pytest.raises(ValueError, match="fixture-only"):
        registry.reconcile(forged_candidate, _inventory())
    mini_manifest = {
        "kind": "ucm-core-release-manifest",
        "ucm_version": "0.5.0rc1",
        "wheel_specs": [],
    }
    with pytest.raises(ValueError, match="release manifest"):
        registry.build_candidate(
            **{**case, "release_manifest": mini_manifest}, fixture_mode=True
        )
    mini_wheel = {
        "kind": "ucm-wheel-inspection",
        "spec_id": case["spec_id"],
        "sha256": case["wheel_records"][0]["sha256"],
        "declaration_sha256": case["wheel_records"][0]["declaration_sha256"],
        "source_kind": "fixture",
        "status": "fixture-only",
        "trust_level": "fixture-only",
        "published": False,
        "publication_eligible": False,
    }
    with pytest.raises(ValueError, match="wheel inspection"):
        registry.build_candidate(
            **{**case, "wheel_records": [mini_wheel]}, fixture_mode=True
        )


def test_crane_live_scan_cli_and_evidence_envelope_are_read_only_and_deterministic(
    tmp_path: Path,
) -> None:
    """Live discovery must need both child configs and expose no registry write verb."""
    registry, verify = _modules()
    crane = tmp_path / "crane-v0.20.3"
    crane.write_text(
        """#!/usr/bin/env python3
import json
import sys
if sys.argv[1] != "manifest":
    if sys.argv[1] == "digest" and sys.argv[2].endswith(":v0.10.2"):
        print("sha256:" + "1" * 64)
        raise SystemExit(0)
    raise SystemExit(91)
ref = sys.argv[2]
if ref.endswith(":v0.10.2"):
    raise SystemExit(93)
elif ref.endswith("@sha256:" + "1" * 64):
    print(json.dumps({
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "4" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
        ]
    }))
elif ref.endswith("2" * 64):
    print(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {"digest": "sha256:" + "3" * 64}}))
elif ref.endswith("4" * 64):
    print(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {"digest": "sha256:" + "5" * 64}}))
else:
    raise SystemExit(92)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)

    live_scan = registry.scan_registry(
        "docker.io/vllm/vllm-openai",
        "v0.10.2",
        crane_binary=str(crane),
    )
    snapshot = live_scan["snapshot"]
    assert snapshot["platforms"] == _snapshot()["platforms"]
    assert live_scan["operations"][1]["reference"] == (
        "docker.io/vllm/vllm-openai@" + DIGESTS["index"]
    )
    assert all(item["capability"] == "read" for item in live_scan["operations"])
    assert verify.audit_operations(live_scan["operations"]) == {
        "operation_count": 4,
        "operation_types": ["crane-digest", "crane-manifest"],
        "write_capable_operations": [],
        "write_count": 0,
    }

    fixture_path = tmp_path / "snapshot.json"
    fixture_path.write_text(json.dumps(snapshot), encoding="utf-8")
    scanned = json.loads(
        _cli(
            "registry",
            "scan",
            "--repository",
            "docker.io/vllm/vllm-openai",
            "--tag",
            "v0.10.2",
            "--fixture",
            str(fixture_path),
        ).stdout
    )
    assert scanned["snapshot"] == snapshot
    assert scanned["operations"] == [
        {
            "type": "fixture-read",
            "capability": "read",
            "reference": "docker.io/vllm/vllm-openai:v0.10.2",
        }
    ]

    case = _case(tmp_path)
    candidate = registry.build_candidate(**case, fixture_mode=True)
    reconcile_path = tmp_path / "reconcile.json"
    reconcile_path.write_text(
        json.dumps({"candidate": candidate, "inventory": _inventory()}),
        encoding="utf-8",
    )
    reconciled = json.loads(_cli("reconcile", "--input", str(reconcile_path)).stdout)
    assert reconciled["task_count"] == 1

    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps(case), encoding="utf-8")
    cli_envelope = json.loads(
        _cli(
            "loop",
            "verify",
            "--input",
            str(case_path),
            "--run-id",
            "cli-run",
            "--attempt",
            "7",
        ).stdout
    )
    first = verify.verify_loop(case, run={"id": "a", "attempt": 1})
    second = verify.verify_loop(
        case, run={"id": "b", "attempt": 99, "finished_at": "tomorrow"}
    )
    assert first["payload"] == second["payload"] == cli_envelope["payload"]
    assert (
        first["payload_sha256"]
        == second["payload_sha256"]
        == (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    first["payload"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
    )


@pytest.mark.parametrize(
    ("operations", "message"),
    [
        (
            [{"type": "registry-push", "capability": "read", "reference": "x"}],
            "write-capable operation type",
        ),
        (
            [{"type": "unknown-read", "capability": "read", "reference": "x"}],
            "unknown operation type",
        ),
        (
            [
                {
                    "type": "fixture-read",
                    "capability": "write",
                    "reference": "docker.io/vllm/vllm-openai:v0.10.2",
                }
            ],
            "capability mismatch",
        ),
        (
            [
                {
                    "type": "fixture-read",
                    "capability": "read",
                    "reference": "not-an-oci-reference",
                }
            ],
            "malformed reference",
        ),
        (
            [
                {
                    "type": "registry-inventory-read",
                    "capability": "read",
                    "reference": DIGESTS["index"],
                },
                {
                    "type": "registry-inventory-read",
                    "capability": "read",
                    "reference": DIGESTS["index"],
                },
            ],
            "duplicate operation identity",
        ),
        (
            [{"type": "fixture-read", "capability": "read"}],
            "malformed ledger entry",
        ),
        (
            [
                {
                    "type": "fixture-read",
                    "capability": "read",
                    "reference": "docker.io/vllm/vllm-openai:v0.10.2",
                    "note": "extra",
                }
            ],
            "malformed ledger entry",
        ),
    ],
)
def test_operation_ledger_rejects_type_capability_and_identity_mutations(
    operations: list[dict[str, str]], message: str
) -> None:
    """Caller labels cannot turn a write or unknown operation into read evidence."""
    _, verify = _modules()

    with pytest.raises(ValueError, match=message):
        verify.audit_operations(operations)


def test_operation_ledger_accepts_only_exact_producer_operations(
    tmp_path: Path,
) -> None:
    """The verifier accepts emitted scan/reconcile batches without a permissive fallback."""
    registry, verify = _modules()
    case = _case(tmp_path)
    scan = registry.scan_registry(
        case["upstream_snapshot"]["repository"],
        case["upstream_snapshot"]["upstream_tag"],
        fixture=case["upstream_snapshot"],
    )
    candidate = registry.build_candidate(**case, fixture_mode=True)
    planned = registry.reconcile(candidate, _inventory())

    audit = verify.audit_operation_batches([scan["operations"], planned["operations"]])

    assert audit == {
        "operation_count": 3,
        "operation_types": [
            "build-plan",
            "fixture-read",
            "registry-inventory-read",
        ],
        "write_capable_operations": [],
        "write_count": 0,
    }
