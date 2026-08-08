"""Behavioral contract for read-only registry reconciliation and loop evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
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


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ucm-core-release-manifest",
        "ucm_version": "0.5.0rc1",
        "wheel_specs": [
            {
                "spec_id": "ascend-a3-openeuler-arm64-cp312",
                "declaration_sha256": "sha256:" + "9" * 64,
                "build_eligible": True,
                "blocked_reasons": [],
            }
        ],
        "publication": {
            "target": "github-release",
            "assets": [
                {
                    "id": "wheel:ascend-a3-openeuler-arm64-cp312",
                    "type": "wheel",
                    "required": True,
                    "status": "candidate",
                }
            ],
        },
        "status": "candidate",
    }


def _wheel() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ucm-wheel-inspection",
        "spec_id": "ascend-a3-openeuler-arm64-cp312",
        "sha256": DIGESTS["wheel"],
        "declaration_sha256": "sha256:" + "9" * 64,
        "source_kind": "fixture",
        "status": "fixture-only",
        "trust_level": "fixture-only",
        "published": False,
        "publication_eligible": False,
    }


def _case() -> dict[str, object]:
    return {
        "release_manifest": _manifest(),
        "wheel_records": [_wheel()],
        "spec_id": "ascend-a3-openeuler-arm64-cp312",
        "upstream_snapshot": _snapshot(),
        "compatibility_rule_id": "ascend-supported",
        "implementation_digest": DIGESTS["implementation"],
    }


def _inventory(entries: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "registry-inventory",
        "entries": entries or [],
    }


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


def test_loop_verify_aggregates_the_six_required_fixture_scenarios() -> None:
    """Removing any reconcile transition or blocker must make the loop non-green."""
    _, verify = _modules()

    envelope = verify.verify_loop(
        _case(),
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
    assert payload["expected_blockers"] == [
        "duplicate-conflicting-inventory",
        "missing-linux-arm64",
        "production-wheel-unpublished",
    ]
    assert payload["fixture_only"] is True
    assert payload["unpublished"] is True
    assert payload["publication_attempted"] is False
    assert payload["zero_write_audit"] == {
        "publication_actions": [],
        "registry_write_commands": [],
        "write_count": 0,
    }


def test_reconcile_schedules_r1_skips_identity_and_preserves_drifted_r1() -> None:
    """A reconciler that overwrites or rebuilds an identical input breaks idempotency."""
    registry, _ = _modules()
    candidate = registry.build_candidate(**_case(), fixture_mode=True)

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


@pytest.mark.parametrize(
    ("product", "tag", "arch", "operating_system"),
    [
        ("vllm-openai", "v0.10.2", "na", "linux"),
        ("vllm-openai", "v1.2.3rc1", "na", "linux"),
        ("vllm-ascend", "v0.10.2", "a2", "linux"),
        ("vllm-ascend", "v0.10.2-a3", "a3", "linux"),
        ("vllm-ascend", "v0.10.2-openeuler", "a2", "openEuler"),
        ("vllm-ascend", "v0.10.2rc2-a3-openeuler", "a3", "openEuler"),
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


def test_snapshot_and_build_identity_bind_every_immutable_input() -> None:
    """Dropping any platform/config/wheel/rule/implementation input changes identity."""
    registry, _ = _modules()
    case = _case()
    baseline = registry.build_candidate(**case, fixture_mode=True)

    assert baseline["target_repository"] == ("ghcr.io/modelengine-group/vllm-ascend")
    assert baseline["tag_base"] == "v0.10.2-a3-openeuler-ucm-0.5.0rc1"
    assert baseline["tag_family_sha256"].startswith("sha256:")
    assert baseline["build_key_sha256"].startswith("sha256:")
    assert baseline["build_inputs"]["upstream"]["platforms"] == _snapshot()["platforms"]

    mutations = []
    changed_manifest = copy.deepcopy(case)
    changed_manifest["release_manifest"]["note"] = "different manifest bytes"
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
    changed_rule["compatibility_rule_id"] = "ascend-supported-v2"
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

    for mutation in (
        lambda value: value["platforms"].pop(),
        lambda value: value["platforms"].append(copy.deepcopy(value["platforms"][0])),
        lambda value: value.update(index_digest="latest"),
    ):
        bad = _snapshot()
        mutation(bad)
        with pytest.raises(ValueError):
            registry.validate_snapshot(bad)


def test_inventory_tag_and_wheel_boundaries_fail_closed() -> None:
    """Conflicting inventory, ambiguous wheels, and unpublished production never plan."""
    registry, _ = _modules()
    candidate = registry.build_candidate(**_case(), fixture_mode=True)
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
            **{**_case(), "wheel_records": [_wheel(), _wheel()]},
            fixture_mode=True,
        )
    with pytest.raises(ValueError, match="unpublished"):
        registry.build_candidate(**_case(), fixture_mode=False)
    forged = _wheel()
    forged.update(
        source_kind="builder-candidate",
        status="published",
        trust_level="registry-published",
        published=True,
        publication_eligible=True,
    )
    with pytest.raises(ValueError, match="Task 2"):
        registry.build_candidate(
            **{**_case(), "wheel_records": [forged]},
            fixture_mode=False,
        )
    forged_candidate = copy.deepcopy(candidate)
    forged_candidate["fixture_only"] = False
    with pytest.raises(ValueError, match="fixture-only"):
        registry.reconcile(forged_candidate, _inventory())


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
    print(json.dumps({
        "manifests": [
            {"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "4" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
        ]
    }))
elif ref.endswith("2" * 64):
    print(json.dumps({"config": {"digest": "sha256:" + "3" * 64}}))
elif ref.endswith("4" * 64):
    print(json.dumps({"config": {"digest": "sha256:" + "5" * 64}}))
else:
    raise SystemExit(92)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)

    snapshot = registry.scan_registry(
        "docker.io/vllm/vllm-openai",
        "v0.10.2",
        crane_binary=str(crane),
    )
    assert snapshot["platforms"] == _snapshot()["platforms"]

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
    assert scanned == snapshot

    candidate = registry.build_candidate(**_case(), fixture_mode=True)
    reconcile_path = tmp_path / "reconcile.json"
    reconcile_path.write_text(
        json.dumps({"candidate": candidate, "inventory": _inventory()}),
        encoding="utf-8",
    )
    reconciled = json.loads(_cli("reconcile", "--input", str(reconcile_path)).stdout)
    assert reconciled["task_count"] == 1

    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps(_case()), encoding="utf-8")
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
    first = verify.verify_loop(_case(), run={"id": "a", "attempt": 1})
    second = verify.verify_loop(
        _case(), run={"id": "b", "attempt": 99, "finished_at": "tomorrow"}
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
