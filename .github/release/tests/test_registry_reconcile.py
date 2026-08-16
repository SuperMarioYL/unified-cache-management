"""Behavioral contract for read-only registry reconciliation and loop evidence."""

from __future__ import annotations

import ast
import base64
import copy
import csv
import functools
import hashlib
import importlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
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
        "repository": "quay.io/ascend/vllm-ascend",
        "upstream_tag": "v0.22.1rc1-a3",
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
    members = {
        "ucm/__init__.py": f"__version__ = {version!r}\n",
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
                "Generator: task3-fixture",
                "Root-Is-Purelib: false",
                f"Tag: {tag}",
                "",
            ]
        ),
    }
    rows: list[list[str]] = []
    for name, content in sorted(members.items()):
        raw = content.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode()
        rows.append([name, "sha256=" + digest.rstrip("="), str(len(raw))])
    record_name = f"{dist_info}/RECORD"
    rows.append([record_name, "", ""])
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    members[record_name] = record.getvalue()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return path


def _case(tmp_path: Path) -> dict[str, object]:
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    wheel = importlib.import_module("ucm_release.wheel")
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
    fixture = wheel.build_fixture_wheel(
        tmp_path / "fixture-wheel", "0" * 40, spec["spec_id"]
    )
    wheel_record = fixture["inspection"]
    catalog = core.load_catalog()
    return {
        "release_manifest": manifest,
        "wheel_records": [wheel_record],
        "spec_id": spec["spec_id"],
        "upstream_snapshot": _snapshot(),
        "catalog": catalog,
        "compatibility_rule_id": "ascend-supported",
        "implementation_digest": DIGESTS["implementation"],
    }


def _inventory(entries: list[dict[str, object]] | None = None) -> dict[str, object]:
    registry, verify = _modules()
    inventory = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": entries or [],
    }
    inventory["inventory_sha256"] = registry.fixture_inventory_digest(inventory)
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
    case = _case(tmp_path)

    envelope = verify.verify_loop(
        case,
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
    assert payload["scenarios"][0]["task_tags"] == ["v0.22.1rc1-a3-ucm-0.5.0rc1-r1"]
    assert payload["scenarios"][1]["task_count"] == 0
    assert payload["scenarios"][2]["task_tags"] == ["v0.22.1rc1-a3-ucm-0.5.0rc1-r2"]
    assert payload["expected_blockers"]["scenario_codes"] == [
        "duplicate-conflicting-inventory",
        "missing-linux-arm64",
        "production-wheel-unpublished",
    ]
    assert (
        payload["expected_blockers"]["production"]
        == case["release_manifest"]["blockers"]
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
    registry, verify = _modules()
    candidate = registry.build_fixture_candidate(**_case(tmp_path), fixture_mode=True)

    first = registry.reconcile_fixture_candidate(candidate, _inventory())
    same = registry.reconcile_fixture_candidate(
        candidate, _inventory([_entry(candidate)])
    )
    drift_entry = _entry(candidate, drift=True)
    drifted = registry.reconcile_fixture_candidate(candidate, _inventory([drift_entry]))

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

    stale_again = registry.reconcile_fixture_candidate(candidate, _inventory())
    changed_entry = _entry(candidate)
    changed_entry["tag"] = candidate["tag_base"] + "-r99"
    changed_entry["build_key_sha256"] = "sha256:" + "a" * 64
    changed = registry.reconcile_fixture_candidate(
        candidate, _inventory([changed_entry])
    )
    assert stale_again["tasks"][0]["precondition"] == first["tasks"][0]["precondition"]
    assert changed["tasks"][0]["precondition"] != first["tasks"][0]["precondition"]
    bad_inventory = _inventory()
    bad_inventory["inventory_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="inventory digest mismatch"):
        registry.reconcile_fixture_candidate(candidate, bad_inventory)
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

    parsed = registry.parse_fixture_upstream_tag(product, tag)

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
        registry.parse_fixture_upstream_tag(product, tag)


def test_registry_scan_rejects_same_product_name_on_an_unreviewed_host() -> None:
    """A matching final path segment cannot widen the exact upstream allowlist."""
    registry, _ = _modules()

    with pytest.raises(ValueError, match="exact upstream repository"):
        registry.scan_fixture_registry(
            "evil.example/vllm/vllm-openai",
            "v0.10.2",
            fixture={**_snapshot(), "repository": "evil.example/vllm/vllm-openai"},
        )


def test_snapshot_and_build_identity_bind_every_immutable_input(tmp_path: Path) -> None:
    """Dropping any platform/config/wheel/rule/implementation input changes identity."""
    registry, _ = _modules()
    case = _case(tmp_path)
    baseline = registry.build_fixture_candidate(**case, fixture_mode=True)

    assert baseline["target_repository"] == ("ghcr.io/modelengine-group/vllm-ascend")
    assert baseline["tag_base"] == "v0.22.1rc1-a3-ucm-0.5.0rc1"
    assert baseline["tag_family_sha256"].startswith("sha256:")
    assert baseline["build_key_sha256"].startswith("sha256:")
    assert baseline["build_inputs"]["upstream"]["platforms"] == _snapshot()["platforms"]

    mutations = []
    changed_manifest = copy.deepcopy(case)
    changed_manifest["catalog"]["compatibility"]["excluded_upstream_patterns"].append(
        "future"
    )
    changed_manifest["release_manifest"]["config_sha256"] = registry.sha256_value(
        changed_manifest["catalog"]
    )
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
        for item in changed_rule["catalog"]["compatibility"]["rules"]
        if item["id"] == "ascend-supported"
    )
    ascend_rule["id"] = "ascend-supported-v2"
    changed_rule["compatibility_rule_id"] = "ascend-supported-v2"
    changed_rule["release_manifest"]["config_sha256"] = registry.sha256_value(
        changed_rule["catalog"]
    )
    mutations.append(changed_rule)
    changed_implementation = copy.deepcopy(case)
    changed_implementation["implementation_digest"] = "sha256:" + "c" * 64
    mutations.append(changed_implementation)

    assert all(
        registry.build_fixture_candidate(**mutation, fixture_mode=True)[
            "build_key_sha256"
        ]
        != baseline["build_key_sha256"]
        for mutation in mutations
    )
    assert (
        registry.with_fixture_revision(baseline, 9)["build_key_sha256"]
        == baseline["build_key_sha256"]
    )
    assert baseline["build_inputs"]["compatibility_rule"]["id"] == ("ascend-supported")
    assert baseline["build_inputs"]["compatibility_rule_sha256"].startswith("sha256:")

    cross_pair = copy.deepcopy(case)
    cross_pair["compatibility_rule_id"] = "cuda-supported"
    with pytest.raises(ValueError, match="compatibility"):
        registry.build_fixture_candidate(**cross_pair, fixture_mode=True)
    semantic_mutation = copy.deepcopy(case)
    mutated_rule = next(
        item
        for item in semantic_mutation["catalog"]["compatibility"]["rules"]
        if item["id"] == "ascend-supported"
    )
    mutated_rule["accelerator_runtimes"].remove("cann-9.0.0")
    semantic_mutation["release_manifest"]["config_sha256"] = registry.sha256_value(
        semantic_mutation["catalog"]
    )
    with pytest.raises(ValueError, match="compatibility"):
        registry.build_fixture_candidate(**semantic_mutation, fixture_mode=True)

    for mutation in (
        lambda value: value["platforms"].pop(),
        lambda value: value.update(index_digest="latest"),
    ):
        bad = _snapshot()
        mutation(bad)
        with pytest.raises(ValueError):
            registry.validate_fixture_snapshot(bad)


def test_fixture_base_policy_drift_creates_a_new_build_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing only the authorized base identity must invalidate an old build key."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    case = _case(tmp_path)
    original_implementation = image.implementation_digests()
    case["implementation_digest"] = original_implementation["aggregate_sha256"]
    baseline = registry.build_fixture_candidate(**case, fixture_mode=True)

    changed_authority = copy.deepcopy(image.FIXTURE_BASE_AUTHORITY)
    changed_authority["manifest_digest"] = "sha256:" + "d" * 64
    monkeypatch.setattr(image, "FIXTURE_BASE_AUTHORITY", changed_authority)
    changed_implementation = image.implementation_digests()
    changed_case = copy.deepcopy(case)
    changed_case["implementation_digest"] = changed_implementation["aggregate_sha256"]
    changed = registry.build_fixture_candidate(**changed_case, fixture_mode=True)
    reconciled = registry.reconcile_fixture_candidate(
        changed, _inventory([_entry(baseline)])
    )

    assert (
        changed_implementation["base_authority_sha256"]
        != original_implementation["base_authority_sha256"]
    )
    assert changed["build_key_sha256"] != baseline["build_key_sha256"]
    assert reconciled["task_count"] == 1
    assert reconciled["tasks"][0]["revision"] == 2


@pytest.mark.parametrize("mutation", ["version", "binary-sha", "buildkit"])
def test_fixture_image_toolchain_policy_drift_creates_a_new_build_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Every authorized image toolchain byte identity participates in build_key."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    case = _case(tmp_path)
    original_implementation = image.implementation_digests()
    case["implementation_digest"] = original_implementation["aggregate_sha256"]
    baseline = registry.build_fixture_candidate(**case, fixture_mode=True)

    changed_authority = copy.deepcopy(image.FIXTURE_IMAGE_TOOLCHAIN_AUTHORITY)
    if mutation == "version":
        changed_authority["buildx_version"] = "v0.19.3"
    elif mutation == "binary-sha":
        changed_authority["buildx_linux_sha256"]["amd64"] = "sha256:" + "c" * 64
    else:
        changed_authority["buildkit_image"] = "moby/buildkit:v0.18.3@sha256:" + "d" * 64
    monkeypatch.setattr(image, "FIXTURE_IMAGE_TOOLCHAIN_AUTHORITY", changed_authority)
    changed_implementation = image.implementation_digests()
    changed_case = copy.deepcopy(case)
    changed_case["implementation_digest"] = changed_implementation["aggregate_sha256"]
    changed = registry.build_fixture_candidate(**changed_case, fixture_mode=True)
    reconciled = registry.reconcile_fixture_candidate(
        changed, _inventory([_entry(baseline)])
    )

    assert (
        changed_implementation["image_toolchain_authority_sha256"]
        != original_implementation["image_toolchain_authority_sha256"]
    )
    assert changed["build_key_sha256"] != baseline["build_key_sha256"]
    assert reconciled["tasks"][0]["revision"] == 2


def test_inventory_tag_and_wheel_boundaries_fail_closed(tmp_path: Path) -> None:
    """Conflicting inventory, ambiguous wheels, and unpublished production never plan."""
    registry, _ = _modules()
    case = _case(tmp_path)
    candidate = registry.build_fixture_candidate(**case, fixture_mode=True)
    conflict = _entry(candidate)
    duplicate = copy.deepcopy(conflict)
    duplicate["observed_digest"] = DIGESTS["observed_drift"]

    with pytest.raises(ValueError, match="conflicting"):
        registry.reconcile_fixture_candidate(
            candidate, _inventory([conflict, duplicate])
        )
    with pytest.raises(ValueError):
        registry.with_fixture_revision(candidate, 0)
    with pytest.raises(ValueError):
        registry.validate_public_tag(candidate["tag_base"] + "-r01")
    with pytest.raises(ValueError):
        registry.build_fixture_candidate(
            **{**case, "wheel_records": case["wheel_records"] * 2},
            fixture_mode=True,
        )
    with pytest.raises(ValueError, match="unpublished"):
        registry.build_fixture_candidate(**case, fixture_mode=False)
    forged = copy.deepcopy(case["wheel_records"][0])
    forged.update(
        source_kind="builder-candidate",
        status="published",
        trust_level="registry-published",
        published=True,
        publication_eligible=True,
    )
    with pytest.raises(ValueError, match="Task 2"):
        registry.build_fixture_candidate(
            **{**case, "wheel_records": [forged]},
            fixture_mode=False,
        )
    builder_record = copy.deepcopy(case["wheel_records"][0])
    builder_record.pop("fixture_binding")
    builder_record["builder_evidence"] = {
        "source_commit": "a" * 40,
        "build_context_digest": "sha256:" + "b" * 64,
        "native_artifacts": ["ucm/ucm_custom_ops.so"],
        "record_status": "passed",
    }
    builder_record.update(
        source_kind="builder-candidate",
        status="candidate-inspected",
        trust_level="unpublished-builder-candidate",
    )
    with pytest.raises(ValueError, match="fixture-only"):
        registry.build_fixture_candidate(
            **{**case, "wheel_records": [builder_record]}, fixture_mode=True
        )
    forged_candidate = copy.deepcopy(candidate)
    forged_candidate["fixture_only"] = False
    with pytest.raises(ValueError, match="fixture-only"):
        registry.reconcile_fixture_candidate(forged_candidate, _inventory())
    mini_manifest = {
        "kind": "ucm-core-release-manifest",
        "ucm_version": "0.5.0rc1",
        "wheel_specs": [],
    }
    with pytest.raises(ValueError, match="release manifest"):
        registry.build_fixture_candidate(
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
        registry.build_fixture_candidate(
            **{**case, "wheel_records": [mini_wheel]}, fixture_mode=True
        )


def test_fixture_scan_cli_and_evidence_envelope_are_local_and_deterministic(
    tmp_path: Path,
) -> None:
    """The legacy Task 3 scanner accepts only an explicit local snapshot fixture."""
    registry, verify = _modules()
    snapshot = {
        **_snapshot(),
        "repository": "docker.io/vllm/vllm-openai",
        "upstream_tag": "v0.10.2",
    }
    fixture_path = tmp_path / "snapshot.json"
    fixture_path.write_text(json.dumps(snapshot), encoding="utf-8")
    scanned = json.loads(
        _cli(
            "registry",
            "fixture-scan",
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
    candidate = registry.build_fixture_candidate(**case, fixture_mode=True)
    reconcile_path = tmp_path / "reconcile.json"
    reconcile_path.write_text(
        json.dumps({"candidate": candidate, "inventory": _inventory()}),
        encoding="utf-8",
    )
    reconciled = json.loads(
        _cli("fixture-reconcile", "--input", str(reconcile_path)).stdout
    )
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
    scan = registry.scan_fixture_registry(
        case["upstream_snapshot"]["repository"],
        case["upstream_snapshot"]["upstream_tag"],
        fixture=case["upstream_snapshot"],
    )
    candidate = registry.build_fixture_candidate(**case, fixture_mode=True)
    planned = registry.reconcile_fixture_candidate(candidate, _inventory())

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


@functools.lru_cache(maxsize=1)
def _publication_fixture_authorities() -> tuple[dict[str, object], dict[str, object]]:
    """Resolve the local registry fixture once, then derive both lane authorities."""
    registry, _ = _modules()
    catalog = registry.core.load_catalog()
    fixture = json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=fixture,
    )
    protected = registry.core.expand_release_plan(
        catalog,
        candidate["resolved_upstreams"],
        lane="protected-tag",
    )
    return candidate, protected


def _publication_members() -> list[dict[str, object]]:
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    candidate, protected = _publication_fixture_authorities()
    protected_by_task_id = {item["task_id"]: item for item in protected["image_tasks"]}
    members: list[dict[str, object]] = []
    for index, task in enumerate(candidate["image_tasks"], start=1):
        digest = f"sha256:{index:064x}"
        config_digest = f"sha256:{index + 10:064x}"
        build_key = f"sha256:{index + 20:064x}"
        wheel_digest = f"sha256:{index + 30:064x}"
        recipe_digest = f"sha256:{index + 40:064x}"
        image_result_digest = f"sha256:{index + 60:064x}"
        layer_digest = f"sha256:{index + 70:064x}"
        source_tree = f"{index + 80:040x}"
        source_archive_digest = f"sha256:{index + 90:064x}"
        source_context_digest = f"sha256:{index + 100:064x}"
        manifest = {
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "size": 1000 + index,
            "annotations": {
                "io.ucm.release.recipe-sha256": recipe_digest,
                "io.ucm.release.task-sha256": task["task_sha256"],
            },
        }
        config_labels = {
            "base.label": f"preserved-{index}",
            "org.opencontainers.image.source": (
                "https://github.com/release-org/unified-cache-management"
            ),
            "org.opencontainers.image.revision": "a" * 40,
            "io.ucm.release.source-tree": source_tree,
            "io.ucm.release.source-context-sha256": source_context_digest,
            "io.ucm.release.task-sha256": task["task_sha256"],
            "io.ucm.release.build-key-sha256": build_key,
            "io.ucm.release.wheel-sha256": wheel_digest,
            "io.ucm.release.recipe-sha256": recipe_digest,
        }
        config = {
            "media_type": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": 200 + index,
            "blob_sha256": config_digest,
            "labels": config_labels,
        }
        layers = [
            {
                "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": layer_digest,
                "size": 300 + index,
                "blob_sha256": layer_digest,
            }
        ]
        content_identity_payload = {
            "manifest_digest": digest,
            "config_digest": config_digest,
            "layers": [
                {
                    "mediaType": layers[0]["media_type"],
                    "digest": layer_digest,
                    "size": layers[0]["size"],
                }
            ],
            "diff_ids": [f"sha256:{index + 110:064x}"],
            "annotations": copy.deepcopy(manifest["annotations"]),
            "labels": copy.deepcopy(config_labels),
            "created": "2026-08-09T00:00:00Z",
            "history": [
                {
                    "created": "2025-01-01T00:00:00Z",
                    "created_by": "base-layer",
                },
                {
                    "created": "2026-08-09T00:00:00Z",
                    "created_by": "ucm-install-only-v1",
                },
            ],
            "source": {
                "repository": "release-org/unified-cache-management",
                "repository_url": (
                    "https://github.com/release-org/unified-cache-management"
                ),
                "commit": "a" * 40,
                "tree": source_tree,
                "archive_sha256": source_archive_digest,
                "context_sha256": source_context_digest,
            },
            "task_sha256": task["task_sha256"],
            "build_key_sha256": build_key,
            "wheel_sha256": wheel_digest,
            "recipe_sha256": recipe_digest,
        }
        content_identity = {
            **content_identity_payload,
            "content_identity_sha256": core.sha256_value(content_identity_payload),
        }
        member_reference = "ghcr.io/release-org/ucm-release-staging@" + digest
        readback_operations = [
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": member_reference,
            },
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": member_reference,
            },
            {
                "type": "registry-authenticated-config-blob-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/release-org/ucm-release-staging@" + config_digest
                ),
            },
            {
                "type": "registry-authenticated-layer-blob-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/release-org/ucm-release-staging@" + layer_digest
                ),
            },
        ]
        readback_payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": "ghcr.io/release-org/ucm-release-staging@" + digest,
            "digest": digest,
            "manifest": manifest,
            "config": config,
            "layers": layers,
            "children": [],
            "authenticated": True,
            "operations": readback_operations,
        }
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-member-publication",
            "status": "passed",
            "spec_id": task["spec_id"],
            "profile_id": task["profile_id"],
            "family_id": task["family_task_id"],
            "platform": task["platform"],
            "target_repository": task["target_repository"],
            "target_tag": task["target_tag"],
            "staging_repository": "ghcr.io/release-org/ucm-release-staging",
            "staging_visibility": "private",
            "staging_tag": f"staging-{build_key.removeprefix('sha256:')}",
            "candidate_task_sha256": task["task_sha256"],
            "publication_task_sha256": protected_by_task_id[task["task_id"]][
                "task_sha256"
            ],
            "build_key_sha256": build_key,
            "wheel_sha256": wheel_digest,
            "member_digest": digest,
            "member_size": 1000 + index,
            "config_digest": config_digest,
            "annotations": {
                "io.ucm.release.build-key-sha256": build_key,
                "io.ucm.release.candidate-task-sha256": task["task_sha256"],
                "io.ucm.release.family-id": task["family_task_id"],
                "io.ucm.release.platform": task["platform"],
                "io.ucm.release.spec-id": task["spec_id"],
                "io.ucm.release.wheel-sha256": wheel_digest,
            },
            "source_sha": "a" * 40,
            "image_result_sha256": image_result_digest,
            "recipe_sha256": recipe_digest,
            "content_identity_sha256": content_identity["content_identity_sha256"],
            "content_identity": content_identity,
            "manifest": manifest,
            "config": config,
            "layers": layers,
            "readback_sha256": core.sha256_value(readback_payload),
            "prewrite_visibility_evidence_sha256": f"sha256:{index + 73:064x}",
            "visibility_evidence_sha256": f"sha256:{index + 75:064x}",
            "collision_model": {
                "model": "observed-state-fail-closed",
                "in_system_serialization": "repository-concurrency",
                "fresh_prewrite_read": True,
                "exact_postwrite_readback": True,
                "external_admin_atomicity": "unavailable",
            },
            "operations": [
                {
                    "type": "registry-anonymous-prewrite-visibility-read",
                    "capability": "read",
                    "reference": (
                        "ghcr.io/release-org/ucm-release-staging:"
                        f"staging-{build_key.removeprefix('sha256:')}"
                    ),
                },
                {
                    "type": "registry-authenticated-staging-prewrite-read",
                    "capability": "read",
                    "reference": (
                        "ghcr.io/release-org/ucm-release-staging:"
                        f"staging-{build_key.removeprefix('sha256:')}"
                    ),
                },
                *readback_operations,
                {
                    "type": "registry-anonymous-visibility-read",
                    "capability": "read",
                    "reference": (
                        "ghcr.io/release-org/ucm-release-staging:"
                        f"staging-{build_key.removeprefix('sha256:')}"
                    ),
                },
            ],
        }
        payload["record_sha256"] = core.sha256_value(payload)
        members.append(payload)
    return members


def _protected_resolved_plan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_product: bool = False,
    split_cuda_family_profiles: bool = False,
    source_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    """Resolve the catalog as a live protected plan without network access."""
    registry, _ = _modules()
    # Warm the immutable fixture authority before this helper installs its live-read
    # stand-ins on the shared registry module.
    _publication_fixture_authorities()
    catalog = registry.core.load_catalog()
    if source_overrides is not None:
        catalog["source"].update(source_overrides)
    if split_cuda_family_profiles:
        cuda_profile = next(
            item for item in catalog["wheel_profiles"] if item["id"] == "cuda130"
        )
        arm_profile = copy.deepcopy(cuda_profile)
        arm_profile.update(
            {
                "id": "cuda130-arm-only",
                "cpu_arch": ["arm64"],
                "builders": {"arm64": arm_profile["builders"]["arm64"]},
            }
        )
        cuda_profile["cpu_arch"] = ["amd64"]
        cuda_profile["builders"] = {"amd64": cuda_profile["builders"]["amd64"]}
        catalog["wheel_profiles"].append(arm_profile)
    if extra_product:
        product = copy.deepcopy(
            next(item for item in catalog["upstream_products"] if item["id"] == "vllm")
        )
        product.update(
            {
                "id": "vllm-extra",
                "repository": "docker.io/vllm/vllm-openai-extra",
                "target_repository": "ghcr.io/release-org/vllm-openai-extra",
                "target_tag_suffix": "-extra-ucm-0.5.0rc1-r1",
            }
        )
        catalog["upstream_products"].append(product)
        rule = copy.deepcopy(
            next(
                item
                for item in catalog["compatibility"]["rules"]
                if item["id"] == "cuda-supported"
            )
        )
        rule.update({"id": "cuda-extra-supported", "upstream_products": ["vllm-extra"]})
        catalog["compatibility"]["rules"].append(rule)
    fixture = json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )
    repositories = fixture["repositories"]
    if extra_product:
        extra_repository = copy.deepcopy(repositories["docker.io/vllm/vllm-openai"])
        for snapshot in extra_repository["snapshots"].values():
            snapshot["repository"] = "docker.io/vllm/vllm-openai-extra"
        repositories["docker.io/vllm/vllm-openai-extra"] = extra_repository
    enumerate_tags = registry.enumerate_repository_tags
    resolve_tag = registry.resolve_repository_tag

    def live_tags(
        repository: str, *, fixture: object, max_tags: int
    ) -> dict[str, object]:
        assert fixture is None
        result = enumerate_tags(
            repository, fixture=repositories[repository], max_tags=max_tags
        )
        result["operations"] = [
            {
                "type": "crane-tag-list",
                "capability": "read",
                "reference": repository,
            }
        ]
        return result

    def live_snapshot(
        repository: str,
        upstream_tag: str,
        *,
        required_architectures: list[str],
        fixture: object,
    ) -> dict[str, object]:
        assert fixture is None
        snapshot_fixture = repositories[repository]["snapshots"][upstream_tag]
        result = resolve_tag(
            repository,
            upstream_tag,
            required_architectures=required_architectures,
            fixture=snapshot_fixture,
        )
        snapshot = result["snapshot"]
        result["fixture_only"] = False
        result["operations"] = [
            {
                "type": "crane-digest",
                "capability": "read",
                "reference": f"{repository}:{upstream_tag}",
            },
            {
                "type": "crane-manifest",
                "capability": "read",
                "reference": f"{repository}@{snapshot['index_digest']}",
            },
            *[
                {
                    "type": "crane-manifest",
                    "capability": "read",
                    "reference": f"{repository}@{member['manifest_digest']}",
                }
                for member in snapshot["members"].values()
            ],
        ]
        return result

    monkeypatch.setattr(registry, "enumerate_repository_tags", live_tags)
    monkeypatch.setattr(registry, "resolve_repository_tag", live_snapshot)
    plan = registry.resolve_catalog(
        catalog,
        source_sha="a" * 40,
        lane="protected-tag",
    )
    registry.validate_resolved_plan(plan)
    return plan


def test_protected_registry_contract_uses_family_task_not_member_profile_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One target family may select different wheel profiles by architecture."""
    registry, _ = _modules()
    plan = _protected_resolved_plan(monkeypatch, split_cuda_family_profiles=True)
    family = next(
        item
        for item in plan["family_tasks"]
        if item["target_repository"].endswith("/vllm-openai")
    )
    image_tasks = [
        next(item for item in plan["image_tasks"] if item["task_id"] == task_id)
        for task_id in family["image_task_ids"]
    ]
    assert [item["profile_id"] for item in image_tasks] == [
        "cuda130",
        "cuda130-arm-only",
    ]

    contract = registry.resolved_registry_contract(
        plan, expected_plan_sha256=plan["resolved_plan_sha256"]
    )
    family_members = [
        item
        for item in contract["members"]
        if item["family_task_id"] == family["task_id"]
    ]
    family_index = next(
        item
        for item in contract["indexes"]
        if item["family_task_id"] == family["task_id"]
    )
    assert {item["profile_id"] for item in family_members} == {
        "cuda130",
        "cuda130-arm-only",
    }
    assert {item["family_id"] for item in family_members} == {family["task_id"]}
    assert family_index["family_id"] == family["task_id"]

    member_records = _publication_members_for_plan(plan)
    forged = copy.deepcopy(
        next(
            record
            for record in member_records
            if record["profile_id"] == "cuda130-arm-only"
        )
    )
    forged["family_id"] = forged["profile_id"]
    forged["annotations"]["io.ucm.release.family-id"] = forged["family_id"]
    forged["record_sha256"] = registry.sha256_value(
        {key: value for key, value in forged.items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="differs from feature/protected task"):
        registry.validate_member_record(
            forged,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )


def _publication_members_for_plan(
    plan: dict[str, object],
) -> list[dict[str, object]]:
    """Rebind valid publication fixtures to exact frozen image tasks."""
    registry, _ = _modules()
    fixture_records = {
        (
            item["target_repository"],
            item["target_tag"],
            item["platform"],
        ): item
        for item in _publication_members()
    }
    fixture_records_by_build_shape = {
        (item["spec_id"], item["platform"]): item for item in _publication_members()
    }
    members: list[dict[str, object]] = []
    for task in plan["image_tasks"]:
        template = fixture_records.get(
            (
                task["target_repository"],
                task["target_tag"],
                task["platform"],
            )
        )
        if template is None:
            template = fixture_records_by_build_shape[
                (task["spec_id"], task["platform"])
            ]
        record = copy.deepcopy(template)
        prior_staging_repository = record["staging_repository"]
        staging_repository = plan["source"]["staging_repository"]
        record.update(
            {
                "spec_id": task["spec_id"],
                "profile_id": task["profile_id"],
                "family_id": task["family_task_id"],
                "platform": task["platform"],
                "target_repository": task["target_repository"],
                "target_tag": task["target_tag"],
                "candidate_task_sha256": task["task_sha256"],
                "publication_task_sha256": task["task_sha256"],
                "source_sha": plan["source"]["commit"],
                "staging_repository": staging_repository,
            }
        )
        for operation in record["operations"]:
            operation["reference"] = operation["reference"].replace(
                prior_staging_repository, staging_repository, 1
            )
        record["annotations"]["io.ucm.release.candidate-task-sha256"] = task[
            "task_sha256"
        ]
        record["annotations"]["io.ucm.release.family-id"] = task["family_task_id"]
        record["annotations"]["io.ucm.release.spec-id"] = task["spec_id"]
        record["manifest"]["annotations"]["io.ucm.release.task-sha256"] = task[
            "task_sha256"
        ]
        record["config"]["labels"]["io.ucm.release.task-sha256"] = task["task_sha256"]
        source_repository = plan["source"]["repository"]
        source_repository_url = f"https://github.com/{source_repository}"
        record["config"]["labels"][
            "org.opencontainers.image.source"
        ] = source_repository_url
        identity = record["content_identity"]
        identity["annotations"] = copy.deepcopy(record["manifest"]["annotations"])
        identity["labels"] = copy.deepcopy(record["config"]["labels"])
        identity["task_sha256"] = task["task_sha256"]
        identity["source"].update(
            {
                "repository": source_repository,
                "repository_url": source_repository_url,
                "commit": plan["source"]["commit"],
            }
        )
        identity["content_identity_sha256"] = registry.sha256_value(
            {
                key: value
                for key, value in identity.items()
                if key != "content_identity_sha256"
            }
        )
        record["content_identity_sha256"] = identity["content_identity_sha256"]
        readback_payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": record["staging_repository"] + "@" + record["member_digest"],
            "digest": record["member_digest"],
            "manifest": copy.deepcopy(record["manifest"]),
            "config": copy.deepcopy(record["config"]),
            "layers": copy.deepcopy(record["layers"]),
            "children": [],
            "authenticated": True,
            "operations": copy.deepcopy(record["operations"][2:6]),
        }
        record["readback_sha256"] = registry.sha256_value(readback_payload)
        record["record_sha256"] = registry.sha256_value(
            {key: value for key, value in record.items() if key != "record_sha256"}
        )
        members.append(record)
    return members


def test_registry_staging_repository_is_bound_to_the_frozen_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-current staging package must flow through every member reference."""
    registry, verify = _modules()
    staging_repository = "ghcr.io/future-org/frozen-private-staging"
    plan = _protected_resolved_plan(
        monkeypatch,
        source_overrides={"staging_repository": staging_repository},
    )
    members = _publication_members_for_plan(plan)
    contract = registry.resolved_registry_contract(
        plan, expected_plan_sha256=plan["resolved_plan_sha256"]
    )

    assert plan["source"]["staging_repository"] == staging_repository
    assert contract["staging_repository"] == staging_repository
    for member in members:
        validated = registry.validate_member_record(
            member,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
        assert validated["staging_repository"] == staging_repository
        assert all(
            staging_repository in operation["reference"]
            for operation in validated["operations"]
        )
        assert verify.audit_operations(
            validated["operations"],
            lane="protected-tag",
            staging_repository=staging_repository,
        )["operation_count"] == len(validated["operations"])


def test_release_asset_builder_rejects_wrong_plan_hash_before_output_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An independently wrong plan hash cannot create the staging directory."""
    _, verify = _modules()
    plan = _protected_resolved_plan(monkeypatch)
    output_dir = tmp_path / "release-assets"

    with pytest.raises(ValueError, match="exact live protected plan"):
        verify.build_release_asset_manifest(
            wheel_dir=tmp_path / "missing-wheels",
            chart_result_path=tmp_path / "missing-chart-result.json",
            chart_package_path=tmp_path / "missing-chart.tgz",
            output_dir=output_dir,
            source_sha=plan["source"]["commit"],
            resolved_plan=plan,
            expected_plan_sha256="sha256:" + "f" * 64,
            run={"run_id": "17", "run_attempt": 2},
        )

    assert not output_dir.exists()


def _absent_inventory_for_plan(
    plan: dict[str, object],
) -> dict[str, object]:
    """Build the canonical all-absent result emitted for frozen family targets."""
    registry, _ = _modules()
    absent = [
        {"repository": task["target_repository"], "tag": task["target_tag"]}
        for task in plan["family_tasks"]
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-inventory",
        "entries": [],
        "absent": absent,
        "operations": [
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": f"{item['repository']}:{item['tag']}",
            }
            for item in absent
        ],
    }
    return {**payload, "inventory_sha256": registry.sha256_value(payload)}


def _resolved_publication_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    dict[str, object],
    dict[str, object],
]:
    """Build one fully local protected fixture bound to its resolved plan."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    parent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(resolved_plan),
        member_statuses={
            item["task_id"]: "success" for item in resolved_plan["image_tasks"]
        },
        lane="protected-tag",
        **binding,
    )
    return resolved_plan, members, parent, binding


def test_collect_members_cli_output_is_consumed_without_plan_field_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real CLI collection is directly valid for the real aggregate consumer."""
    registry, verify = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    run = {"run_id": "42", "run_attempt": 3}
    root = tmp_path / "artifacts"
    root.mkdir()
    for task, member in zip(plan["image_tasks"], members, strict=True):
        directory = root / verify.run_bound_artifact_name(
            f"ucm-member-{task['task_id']}", run["run_id"], run["run_attempt"]
        )
        directory.mkdir()
        preflight_payload = {
            "schema_version": 1,
            "kind": "ucm-tag-preflight",
            "lane": "protected-tag",
            "source_sha": plan["source"]["commit"],
            "publication_allowed": True,
            "write_authority": [
                "github-prerelease",
                "ghcr-final-index",
                "ghcr-private-staging",
            ],
            "checks": {"release_tag_matches": True},
        }
        preflight = {
            **preflight_payload,
            "preflight_sha256": registry.sha256_value(preflight_payload),
        }
        files = {
            "member-record.json": member,
            "member-audit.json": {"kind": "test-member-audit"},
            "member-preflight.json": preflight,
            "member-mutation-preflight.json": preflight,
            "selected-task.json": task,
            "upstream-drift.json": {"kind": "test-upstream-drift"},
        }
        for name, value in files.items():
            (directory / name).write_bytes(registry.canonical_bytes(value) + b"\n")
    plan_path = tmp_path / "resolved-plan.json"
    request_path = tmp_path / "collect-members.input.json"
    output_path = tmp_path / "member-collection.json"
    collected_dir = tmp_path / "collected"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    request_path.write_bytes(
        registry.canonical_bytes(
            {
                "root": str(root),
                "output_dir": str(collected_dir),
                "source_sha": plan["source"]["commit"],
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
                "run": run,
            }
        )
        + b"\n"
    )

    assert (
        cli.main(
            [
                "artifact",
                "collect-members",
                "--input",
                str(request_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    collection = json.loads(capsys.readouterr().out)
    contract = registry.resolved_registry_contract(
        plan, expected_plan_sha256=plan["resolved_plan_sha256"]
    )
    consumed = verify._validate_member_artifact_collection(
        collection,
        members,
        source_sha=plan["source"]["commit"],
        contract=contract,
    )

    assert consumed["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert consumed["collection_sha256"] == collection["collection_sha256"]


def test_dynamic_member_publish_uses_catalog_added_plan_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-six protected member write is authorized only by its frozen task."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    plan = _protected_resolved_plan(monkeypatch, extra_product=True)
    selected = next(
        task
        for task in plan["image_tasks"]
        if task["runtime"]["product_id"] == "vllm-extra" and task["cpu_arch"] == "amd64"
    )
    member = next(
        item
        for task, item in zip(
            plan["image_tasks"], _publication_members_for_plan(plan), strict=True
        )
        if task["task_id"] == selected["task_id"]
    )
    archive, expected = _valid_oci_archive(tmp_path, member)
    image_result = {
        "candidate_kind": "real-candidate",
        "unpublished": True,
        "spec_id": expected["spec_id"],
        "profile_id": expected["profile_id"],
        "family_id": expected["family_id"],
        "target_platform": expected["platform"],
        "target_repository": expected["target_repository"],
        "target_tag": expected["target_tag"],
        "task_key": expected["candidate_task_sha256"],
        "build_key_sha256": expected["build_key_sha256"],
        "recipe_sha256": expected["recipe_sha256"],
        "content_identity_sha256": expected["content_identity_sha256"],
        "result_sha256": expected["image_result_sha256"],
        "source": {
            **copy.deepcopy(expected["content_identity"]["source"]),
            "task_sha256": expected["candidate_task_sha256"],
            "wheel_build_key": "sha256:" + "e" * 64,
        },
        "wheel": {"sha256": expected["wheel_sha256"]},
        "oci": {"digest": expected["member_digest"], "published": False},
        "content_identity": copy.deepcopy(expected["content_identity"]),
    }
    member_reference = (
        plan["source"]["staging_repository"] + "@" + expected["member_digest"]
    )
    read_operations = [
        {
            "type": "registry-authenticated-digest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-manifest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-config-blob-read",
            "capability": "read",
            "reference": plan["source"]["staging_repository"]
            + "@"
            + expected["config_digest"],
        },
        {
            "type": "registry-authenticated-layer-blob-read",
            "capability": "read",
            "reference": (
                plan["source"]["staging_repository"]
                + "@"
                + expected["layers"][0]["digest"]
            ),
        },
    ]
    readback_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-readback",
        "reference": member_reference,
        "digest": expected["member_digest"],
        "manifest": copy.deepcopy(expected["manifest"]),
        "config": copy.deepcopy(expected["config"]),
        "layers": copy.deepcopy(expected["layers"]),
        "children": [],
        "authenticated": True,
        "operations": read_operations,
    }
    readback = {
        **readback_payload,
        "readback_sha256": registry.sha256_value(readback_payload),
    }

    def visibility(
        reference: str,
        *,
        staging_repository: str,
        phase: str = "postwrite",
    ) -> dict[str, object]:
        assert staging_repository == plan["source"]["staging_repository"]
        operation = {
            "type": (
                "registry-anonymous-prewrite-visibility-read"
                if phase == "prewrite"
                else "registry-anonymous-visibility-read"
            ),
            "capability": "read",
            "reference": reference,
        }
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-private-visibility-evidence",
            "status": "anonymous-denied",
            "phase": phase,
            "returncode": 1,
            "stdout_sha256": "sha256:" + ("1" if phase == "prewrite" else "3") * 64,
            "stderr_sha256": "sha256:" + ("2" if phase == "prewrite" else "4") * 64,
            "operation": operation,
        }
        return {
            **payload,
            "visibility_evidence_sha256": registry.sha256_value(payload),
        }

    validation_calls: list[dict[str, object]] = []

    def validate_image_result(
        value: object,
        *,
        resolved_plan: dict[str, object],
        expected_plan_sha256: str,
        task_id: str,
    ) -> object:
        validation_calls.append(
            {
                "resolved_plan": resolved_plan,
                "expected_plan_sha256": expected_plan_sha256,
                "task_id": task_id,
            }
        )
        return copy.deepcopy(value)

    monkeypatch.setattr(image, "validate_image_result", validate_image_result)
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    assert not hasattr(registry.core, "build_matrix")
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")
    monkeypatch.setattr(registry, "verify_private_staging", visibility)
    monkeypatch.setattr(registry, "_fresh_transport_digest", lambda *_a, **_k: None)
    monkeypatch.setattr(
        registry,
        "_push_materialized_member",
        lambda *_a, **_k: {
            "digest": expected["member_digest"],
            "operations": [
                {
                    "type": "registry-member-push-by-digest",
                    "capability": "write",
                    "reference": member_reference,
                }
            ],
        },
    )
    staging_reference = (
        plan["source"]["staging_repository"]
        + ":staging-"
        + expected["build_key_sha256"][7:]
    )
    monkeypatch.setattr(
        registry,
        "_apply_digest_tag",
        lambda **_kwargs: {
            "digest": expected["member_digest"],
            "decision": "create",
            "collision_model": registry._collision_model_evidence(),
            "operations": [
                {
                    "type": "registry-staging-tag-create",
                    "capability": "write",
                    "reference": staging_reference,
                }
            ],
        },
    )
    monkeypatch.setattr(
        registry, "readback_reference", lambda *_a, **_k: copy.deepcopy(readback)
    )

    published = registry.publish_member(
        archive,
        image_result=image_result,
        lane="protected-tag",
        selected_task=selected,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    assert len(plan["image_tasks"]) == 8
    assert validation_calls == [
        {
            "resolved_plan": plan,
            "expected_plan_sha256": plan["resolved_plan_sha256"],
            "task_id": selected["task_id"],
        }
    ]
    assert published["candidate_task_sha256"] == selected["task_sha256"]
    foreign = copy.deepcopy(selected)
    foreign["task_id"] = "image-" + "f" * 64
    with pytest.raises(ValueError, match="selected|task"):
        registry.publish_member(
            archive,
            image_result=image_result,
            lane="protected-tag",
            selected_task=foreign,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )


def test_production_registry_and_release_boundaries_require_frozen_plan_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing plan authority fails before payload parsing or protected preflight."""
    registry, verify = _modules()
    monkeypatch.setattr(
        registry.core,
        "tag_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("protected preflight ran before plan validation")
        ),
    )

    with pytest.raises(ValueError, match="frozen resolved plan"):
        registry.validate_member_record(
            {}, resolved_plan=None, expected_plan_sha256=None
        )
    with pytest.raises(ValueError, match="frozen resolved plan"):
        registry.plan_indexes(
            [],
            {},
            member_statuses={},
            lane="protected-tag",
            resolved_plan=None,
            expected_plan_sha256=None,
        )
    with pytest.raises(ValueError, match="frozen resolved plan"):
        registry._fresh_write_authority(
            "protected-tag",
            resolved_plan=None,
            expected_plan_sha256=None,
            task_kind=None,
            task_id=None,
        )
    with pytest.raises(ValueError, match="frozen resolved plan"):
        verify.validate_index_readbacks(
            [],
            [],
            parent_plans={},
            anonymous=True,
            resolved_plan=None,
            expected_plan_sha256=None,
        )
    with pytest.raises(ValueError, match="frozen resolved plan"):
        verify.validate_release_asset_manifest(
            {},
            allowed_root=tmp_path,
            resolved_plan=None,
            expected_plan_sha256=None,
        )


def test_registry_production_source_has_no_fixed_catalog_fallback() -> None:
    """Current fixture cardinality must not remain callable production authority."""
    source = (RELEASE_ROOT / "ucm_release" / "registry.py").read_text(encoding="utf-8")
    for forbidden in (
        "CANONICAL_MEMBER_SPEC_IDS",
        "def canonical_registry_contract",
        'core.build_matrix("protected-tag")',
        '"matrix_sha256"',
        'len(record["member_digests"]) != 2',
        "len(member_records) != 6",
    ):
        assert forbidden not in source

    cli_source = (RELEASE_ROOT / "ucm_release" / "cli.py").read_text(encoding="utf-8")
    for forbidden in (
        "registry.inventory_registry()",
        'set(request) != {"lane", "parent_plans", "family_id"}',
    ):
        assert forbidden not in cli_source

    verify_source = (RELEASE_ROOT / "ucm_release" / "verify.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "contract: dict[str, Any] | None = None",
        "member artifact collection differs from six records",
        "provisional artifact collection differs from three indexes",
        "Aggregate 6/3 authenticated state",
    ):
        assert forbidden not in verify_source


def test_release_closure_internal_calls_always_forward_the_frozen_plan() -> None:
    """Every nested production asset reopen stays on its originating plan/hash."""
    source = (RELEASE_ROOT / "ucm_release" / "verify.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    protected_calls = {
        "validate_release_asset_manifest",
        "plan_release_assets",
        "verify_release_assets",
        "plan_release_asset_downloads",
        "refresh_release_asset_metadata",
        "rebase_release_asset_manifest",
        "verify_release_upload_prefix",
        "record_release_upload_response",
        "validate_release_upload_transcript",
    }
    missing: list[tuple[str, int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in protected_calls:
            continue
        keywords = {item.arg for item in node.keywords}
        absent = sorted({"resolved_plan", "expected_plan_sha256"} - keywords)
        if absent:
            missing.append((node.func.id, node.lineno, absent))
    assert missing == []


def test_production_registry_and_release_plan_parameters_are_required() -> None:
    """Production API signatures cannot silently default back to current state."""
    registry, verify = _modules()
    functions = [
        registry.resolved_registry_contract,
        registry.validate_member_record,
        registry.validate_index_record,
        registry.verify_member_readback,
        registry.plan_indexes,
        registry._validate_parent_plans,
        registry.validate_index_plans,
        registry.verify_index,
        registry._fresh_write_authority,
        registry.publish_member,
        registry.prepare_index,
        registry.validate_provisional_index,
        registry.finalize_index,
        registry.validate_finalized_index,
        verify.validate_release_asset_manifest,
        verify.plan_release_assets,
        verify.verify_release_assets,
        verify.plan_release_asset_downloads,
        verify.refresh_release_asset_metadata,
        verify.rebase_release_asset_manifest,
        verify.verify_release_upload_prefix,
        verify.record_release_upload_response,
        verify.validate_release_upload_transcript,
        verify.github_release_publication_evidence,
        verify.validate_index_readbacks,
        verify.authenticated_registry_publication_evidence,
        verify.protected_registry_publication_evidence,
    ]
    optional: list[tuple[str, str]] = []
    for function in functions:
        parameters = inspect.signature(function).parameters
        for name in ("resolved_plan", "expected_plan_sha256"):
            if parameters[name].default is not inspect.Parameter.empty:
                optional.append((function.__name__, name))
    if (
        inspect.signature(registry.select_task)
        .parameters["expected_plan_sha256"]
        .default
        is not inspect.Parameter.empty
    ):
        optional.append(("select_task", "expected_plan_sha256"))
    assert optional == []


def test_release_asset_set_reopens_only_against_its_frozen_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact plan-derived asset order has a positive local reopen path."""
    registry, verify = _modules()
    plan = _protected_resolved_plan(monkeypatch)
    authorities = verify._canonical_release_asset_authorities(
        plan["wheel_tasks"],
        include_plan=True,
        chart_authority=plan["chart"],
    )
    assets: list[dict[str, object]] = []
    for index, authority in enumerate(authorities):
        path = tmp_path / authority["name"]
        path.write_bytes(f"asset-{index}".encode())
        assets.append(
            {
                **authority,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "path": str(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets",
        "source_sha": plan["source"]["commit"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "assets": assets,
    }
    manifest["assets_sha256"] = verify.sha256_value(
        verify._release_asset_identity_payload(manifest)
    )

    assert (
        verify.validate_release_asset_manifest(
            manifest,
            allowed_root=tmp_path,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
        == manifest
    )
    with pytest.raises(ValueError, match="exact live protected plan"):
        verify.validate_release_asset_manifest(
            manifest,
            allowed_root=tmp_path,
            resolved_plan=plan,
            expected_plan_sha256="sha256:" + "f" * 64,
        )


def test_dynamic_index_prepare_binds_catalog_added_family_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare/provisional validation use the exact non-six frozen family task."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch, extra_product=True)
    members = _publication_members_for_plan(resolved_plan)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")
    monkeypatch.setattr(
        registry,
        "_run_registry_tool",
        lambda _executable, arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 1, stdout="", stderr="MANIFEST_UNKNOWN"
        ),
    )
    inventory = registry.inventory_registry(
        targets=[
            {"repository": task["target_repository"], "tag": task["target_tag"]}
            for task in resolved_plan["family_tasks"]
        ]
    )
    parent = registry.plan_indexes(
        members,
        inventory=inventory,
        member_statuses={
            task["task_id"]: "success" for task in resolved_plan["image_tasks"]
        },
        lane="protected-tag",
        resolved_plan=resolved_plan,
        expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
    )
    family = next(
        task
        for task in resolved_plan["family_tasks"]
        if task["product_id"] == "vllm-extra"
    )
    index_plan = next(
        item for item in parent["plans"] if item["family_task_id"] == family["task_id"]
    )
    digest = index_plan["expected_index_digest"]
    target = index_plan["target_repository"] + ":" + index_plan["target_tag"]
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    assert not hasattr(registry.core, "build_matrix")
    monkeypatch.setattr(registry, "resolve_pinned_buildx", lambda: "/pinned/buildx")
    monkeypatch.setattr(
        registry,
        "_create_index_transport",
        lambda **_: {
            "rendered": copy.deepcopy(index_plan["index_manifest"]),
            "raw_manifest": registry.canonical_bytes(index_plan["index_manifest"]),
            "index_digest": digest,
            "decision": "create",
            "collision_model": registry._collision_model_evidence(),
            "operations": [
                {
                    "type": "registry-index-create",
                    "capability": "write",
                    "reference": target,
                }
            ],
            "postwrite_manifest_sha256": digest,
        },
    )
    readback_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-readback",
        "reference": target,
        "digest": digest,
        "manifest": {
            "media_type": "application/vnd.oci.image.index.v1+json",
            "digest": digest,
            "size": len(registry.canonical_bytes(index_plan["index_manifest"])),
            "annotations": copy.deepcopy(index_plan["index_manifest"]["annotations"]),
        },
        "config": None,
        "layers": [],
        "children": copy.deepcopy(index_plan["index_manifest"]["manifests"]),
        "authenticated": True,
        "operations": [
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": target,
            },
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": index_plan["target_repository"] + "@" + digest,
            },
        ],
    }
    readback = {
        **readback_payload,
        "readback_sha256": registry.sha256_value(readback_payload),
    }
    monkeypatch.setattr(
        registry, "readback_reference", lambda *_a, **_k: copy.deepcopy(readback)
    )
    monkeypatch.setattr(
        registry,
        "_validate_remote_index_closure",
        lambda plan, *, index_digest, anonymous=False: _index_closure_evidence(
            registry, plan, index_digest, authenticated=not anonymous
        ),
    )

    provisional = registry.prepare_index(
        index_plan,
        parent_plans=parent,
        lane="protected-tag",
        resolved_plan=resolved_plan,
        expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
    )
    assert provisional["resolved_plan_sha256"] == resolved_plan["resolved_plan_sha256"]
    assert provisional["family_task_id"] == family["task_id"]
    assert provisional["family_task_sha256"] == family["task_sha256"]
    assert (
        registry.validate_provisional_index(
            provisional,
            parent_plans=parent,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )
        == provisional
    )

    foreign = copy.deepcopy(index_plan)
    foreign["family_task_id"] = "family-" + "f" * 64
    with pytest.raises(ValueError, match="task|family"):
        registry.prepare_index(
            foreign,
            parent_plans=parent,
            lane="protected-tag",
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )


def test_plan_index_cli_binds_dynamic_members_to_frozen_protected_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The protected planner accepts task IDs only from one validated plan."""
    registry, _ = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    plan_path = tmp_path / "resolved-plan.json"
    input_path = tmp_path / "plan-index.input.json"
    output_path = tmp_path / "parent-plans.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    input_path.write_bytes(
        registry.canonical_bytes(
            {
                "lane": "protected-tag",
                "members": members,
                "member_statuses": statuses,
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
            }
        )
        + b"\n"
    )
    monkeypatch.setattr(
        registry,
        "inventory_registry",
        lambda *, targets: _absent_inventory_for_plan(plan),
    )

    assert (
        cli.main(
            [
                "registry",
                "plan-index",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    parent = json.loads(capsys.readouterr().out)
    assert parent["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert parent["member_statuses"] == statuses
    assert {item["family_task_id"] for item in parent["plans"]} == {
        item["task_id"] for item in plan["family_tasks"]
    }


def test_plan_index_cli_inventories_only_frozen_family_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dynamic inventory has exact plan coverage and rejects missing/extra scans."""
    registry, _ = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch, extra_product=True)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    plan_path = tmp_path / "resolved-plan.json"
    request_path = tmp_path / "plan-index.input.json"
    output_path = tmp_path / "parent-plans.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    request_path.write_bytes(
        registry.canonical_bytes(
            {
                "lane": "protected-tag",
                "members": members,
                "member_statuses": statuses,
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
            }
        )
        + b"\n"
    )
    queried: list[str] = []

    def missing_registry_read(
        _executable: str, arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[0] == "digest"
        queried.append(arguments[1])
        return subprocess.CompletedProcess(
            arguments, 1, stdout="", stderr="MANIFEST_UNKNOWN"
        )

    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")
    monkeypatch.setattr(registry, "_run_registry_tool", missing_registry_read)

    assert (
        cli.main(
            [
                "registry",
                "plan-index",
                "--input",
                str(request_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    parent = json.loads(capsys.readouterr().out)
    targets = [
        {"repository": family["target_repository"], "tag": family["target_tag"]}
        for family in plan["family_tasks"]
    ]
    assert queried == [f"{item['repository']}:{item['tag']}" for item in targets]
    assert parent["inventory"]["absent"] == targets

    missing = registry.inventory_registry(targets=targets[:-1])
    with pytest.raises(ValueError, match="inventory|target|coverage"):
        registry.plan_indexes(
            members,
            missing,
            member_statuses=statuses,
            lane="protected-tag",
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
    extra = registry.inventory_registry(
        targets=[
            *targets,
            {
                "repository": "ghcr.io/release-org/foreign-image",
                "tag": plan["family_tasks"][0]["target_tag"],
            },
        ]
    )
    with pytest.raises(ValueError, match="inventory|target|coverage"):
        registry.plan_indexes(
            members,
            extra,
            member_statuses=statuses,
            lane="protected-tag",
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )


def test_prepare_index_cli_selects_exact_frozen_family_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A family writer receives no profile/family allowlist outside the plan."""
    registry, _ = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses=statuses,
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    family_task = plan["family_tasks"][0]
    plan_path = tmp_path / "resolved-plan.json"
    input_path = tmp_path / "prepare-index.input.json"
    output_path = tmp_path / "provisional.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    input_path.write_bytes(
        registry.canonical_bytes(
            {
                "lane": "protected-tag",
                "parent_plans": parent,
                "family_task_id": family_task["task_id"],
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
            }
        )
        + b"\n"
    )
    selected: dict[str, object] = {}

    def prepare(
        index_plan: dict[str, object],
        *,
        parent_plans: dict[str, object],
        lane: str,
        resolved_plan: dict[str, object],
        expected_plan_sha256: str,
    ) -> dict[str, object]:
        selected.update(index_plan)
        assert parent_plans == parent
        assert lane == "protected-tag"
        assert resolved_plan == plan
        assert expected_plan_sha256 == plan["resolved_plan_sha256"]
        return {
            "schema_version": 1,
            "kind": "ucm-registry-index-provisional",
            "family_task_id": index_plan["family_task_id"],
        }

    monkeypatch.setattr(registry, "prepare_index", prepare)

    assert (
        cli.main(
            [
                "registry",
                "prepare-index",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["family_task_id"] == family_task["task_id"]
    assert selected["family_task_id"] == family_task["task_id"]


def test_collect_provisionals_cli_uses_frozen_family_task_artifact_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provisional collection closes over the plan's opaque family task IDs."""
    registry, verify = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses=statuses,
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    plan_path = tmp_path / "resolved-plan.json"
    parent_path = tmp_path / "parent-plans.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    run = {"run_id": "42", "run_attempt": 3}
    root = tmp_path / "artifacts"
    root.mkdir()
    provisionals: dict[str, dict[str, object]] = {}
    for family in plan["family_tasks"]:
        family_task_id = family["task_id"]
        physical = verify.run_bound_artifact_name(
            f"ucm-index-provisional-{family_task_id}",
            run["run_id"],
            run["run_attempt"],
        )
        directory = root / physical
        directory.mkdir()
        preflight_payload = {
            "schema_version": 1,
            "kind": "ucm-tag-preflight",
            "lane": "protected-tag",
            "source_sha": plan["source"]["commit"],
            "publication_allowed": True,
        }
        preflight = {
            **preflight_payload,
            "preflight_sha256": registry.sha256_value(preflight_payload),
        }
        provisional = {
            "schema_version": 1,
            "kind": "ucm-registry-index-provisional",
            "family_task_id": family_task_id,
            "family_id": next(
                item["family_id"]
                for item in parent["plans"]
                if item["family_task_id"] == family_task_id
            ),
            "preflight_sha256": preflight["preflight_sha256"],
            "provisional_sha256": "sha256:" + family_task_id[-64:],
        }
        provisionals[family_task_id] = provisional
        (directory / "provisional.json").write_bytes(
            registry.canonical_bytes(provisional) + b"\n"
        )
        (directory / "preflight.json").write_bytes(
            registry.canonical_bytes(preflight) + b"\n"
        )
    monkeypatch.setattr(
        registry,
        "validate_provisional_index",
        lambda value, **kwargs: copy.deepcopy(value),
    )
    input_path = tmp_path / "collect.input.json"
    output_path = tmp_path / "collection.json"
    output_dir = tmp_path / "collected"
    input_path.write_bytes(
        registry.canonical_bytes(
            {
                "root": str(root),
                "output_dir": str(output_dir),
                "source_sha": plan["source"]["commit"],
                "parent_plans": str(parent_path),
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
                "run": run,
            }
        )
        + b"\n"
    )

    assert (
        cli.main(
            [
                "artifact",
                "collect-provisionals",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    collection = json.loads(capsys.readouterr().out)
    assert {Path(path).name for path in collection["provisional_indexes"]} == {
        f"{task['task_id']}.json" for task in plan["family_tasks"]
    }
    assert set(collection["provisional_sha256s"]) == {
        task["task_id"] for task in plan["family_tasks"]
    }


def test_authenticated_aggregate_cli_passes_exact_frozen_plan_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Authenticated aggregation receives the plan object and expected hash."""
    registry, verify = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses=statuses,
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    plan_path = tmp_path / "resolved-plan.json"
    parent_path = tmp_path / "parent-plans.json"
    collection_path = tmp_path / "member-collection.json"
    provisional_collection_path = tmp_path / "provisional-collection.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    collection_path.write_text("{}\n", encoding="utf-8")
    provisional_collection_path.write_text("{}\n", encoding="utf-8")
    member_paths: list[str] = []
    for task, member in zip(plan["image_tasks"], members, strict=True):
        path = tmp_path / f"{task['task_id']}.member.json"
        path.write_bytes(registry.canonical_bytes(member) + b"\n")
        member_paths.append(str(path))
    provisional_paths: list[str] = []
    for family in plan["family_tasks"]:
        path = tmp_path / f"{family['task_id']}.provisional.json"
        path.write_text('{"kind":"test-provisional"}\n', encoding="utf-8")
        provisional_paths.append(str(path))
    observed: dict[str, object] = {}

    def aggregate(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "schema_version": 1,
            "kind": "ucm-authenticated-registry-publication-payload",
            "resolved_plan_sha256": kwargs["resolved_plan"]["resolved_plan_sha256"],
        }

    monkeypatch.setattr(
        verify, "authenticated_registry_publication_evidence", aggregate
    )
    request_path = tmp_path / "aggregate.input.json"
    output_path = tmp_path / "authenticated.json"
    request_path.write_bytes(
        registry.canonical_bytes(
            {
                "member_records": member_paths,
                "member_collection": str(collection_path),
                "provisional_indexes": provisional_paths,
                "provisional_collection": str(provisional_collection_path),
                "parent_plans": str(parent_path),
                "source_sha": plan["source"]["commit"],
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
                "run": {"run_id": "42", "run_attempt": 3},
            }
        )
        + b"\n"
    )

    assert (
        cli.main(
            [
                "registry",
                "aggregate-authenticated",
                "--input",
                str(request_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert observed["resolved_plan"] == plan
    assert observed["expected_plan_sha256"] == plan["resolved_plan_sha256"]


def test_protected_aggregate_cli_passes_exact_frozen_plan_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Protected aggregation executes with the same frozen plan/hash authority."""
    registry, verify = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses={task["task_id"]: "success" for task in plan["image_tasks"]},
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    plan_path = tmp_path / "resolved-plan.json"
    parent_path = tmp_path / "parent-plans.json"
    member_collection_path = tmp_path / "member-collection.json"
    provisional_collection_path = tmp_path / "provisional-collection.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    member_collection_path.write_text("{}\n", encoding="utf-8")
    provisional_collection_path.write_text("{}\n", encoding="utf-8")
    member_paths: list[str] = []
    for task, member in zip(plan["image_tasks"], members, strict=True):
        path = tmp_path / f"{task['task_id']}.member.json"
        path.write_bytes(registry.canonical_bytes(member) + b"\n")
        member_paths.append(str(path))
    finalized_paths: list[str] = []
    for family in plan["family_tasks"]:
        path = tmp_path / f"{family['task_id']}.finalized.json"
        path.write_text('{"kind":"test-finalized"}\n', encoding="utf-8")
        finalized_paths.append(str(path))
    observed: dict[str, object] = {}

    def aggregate(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "schema_version": 1,
            "kind": "ucm-protected-registry-publication-payload",
            "resolved_plan_sha256": kwargs["resolved_plan"]["resolved_plan_sha256"],
        }

    monkeypatch.setattr(verify, "protected_registry_publication_evidence", aggregate)
    request_path = tmp_path / "aggregate-protected.input.json"
    output_path = tmp_path / "protected.json"
    request_path.write_bytes(
        registry.canonical_bytes(
            {
                "member_records": member_paths,
                "member_collection": str(member_collection_path),
                "finalized_indexes": finalized_paths,
                "provisional_collection": str(provisional_collection_path),
                "parent_plans": str(parent_path),
                "source_sha": plan["source"]["commit"],
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
                "run": {"run_id": "42", "run_attempt": 3},
            }
        )
        + b"\n"
    )

    assert (
        cli.main(
            [
                "registry",
                "aggregate-protected",
                "--input",
                str(request_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert observed["resolved_plan"] == plan
    assert observed["expected_plan_sha256"] == plan["resolved_plan_sha256"]


def test_image_bridge_cli_reopens_hosted_image_task_from_frozen_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The member bridge compares image evidence to its image task, not a wheel."""
    registry, verify = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    task = plan["image_tasks"][0]
    epoch = 1_800_000_000
    hosted = verify.hosted_image_task(
        task,
        plan["source"]["commit"],
        epoch,
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    plan_path = tmp_path / "resolved-plan.json"
    hosted_path = tmp_path / "hosted-task.json"
    request_path = tmp_path / "bridge.input.json"
    output_path = tmp_path / "bridge.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    hosted_path.write_bytes(registry.canonical_bytes(hosted) + b"\n")
    run = {"run_id": "42", "run_attempt": 3}
    request_path.write_bytes(
        registry.canonical_bytes(
            {
                "source_sha": plan["source"]["commit"],
                "task_id": task["task_id"],
                "oci_artifact": verify.run_bound_artifact_name(
                    f"ucm-internal-oci-{task['task_id']}", "42", 3
                ),
                "image_artifact": verify.run_bound_artifact_name(
                    task["artifact_name"], "42", 3
                ),
                "hosted_task": str(hosted_path),
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
                "run": run,
            }
        )
        + b"\n"
    )

    assert (
        cli.main(
            [
                "artifact",
                "validate-image-bridge",
                "--input",
                str(request_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["task_id"] == task["task_id"]
    assert result["image_task_sha256"] == task["task_sha256"]
    assert result["resolved_plan_sha256"] == plan["resolved_plan_sha256"]


def test_verify_member_cli_passes_exact_selected_image_task_to_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mutating member command cannot fall back to an embedded six-ID map."""
    registry, _ = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    task = plan["image_tasks"][0]
    plan_path = tmp_path / "resolved-plan.json"
    result_path = tmp_path / "image-result.json"
    archive_path = tmp_path / "image.oci.tar"
    input_path = tmp_path / "verify-member.input.json"
    output_path = tmp_path / "member-record.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    result_path.write_text('{"kind":"test-image-result"}\n', encoding="utf-8")
    archive_path.write_bytes(b"oci")
    input_path.write_bytes(
        registry.canonical_bytes(
            {
                "lane": "protected-tag",
                "image_result": str(result_path),
                "oci_archive": str(archive_path),
                "task_id": task["task_id"],
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
            }
        )
        + b"\n"
    )
    observed: dict[str, object] = {}

    def publish(archive: Path, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        assert archive == archive_path
        return {
            "schema_version": 1,
            "kind": "ucm-registry-member-publication",
            "task_id": kwargs["selected_task"]["task_id"],
        }

    monkeypatch.setattr(registry, "publish_member", publish)

    assert (
        cli.main(
            [
                "registry",
                "verify-member",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["task_id"] == task["task_id"]
    assert observed["selected_task"] == task
    assert observed["resolved_plan"] == plan
    assert observed["expected_plan_sha256"] == plan["resolved_plan_sha256"]


def test_finalize_index_cli_binds_anonymous_readback_to_frozen_family_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Anonymous finalization reopens the same plan and opaque family task ID."""
    registry, _ = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses=statuses,
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    family_task_id = plan["family_tasks"][0]["task_id"]
    provisional = {
        "kind": "test-provisional",
        "family_task_id": family_task_id,
    }
    plan_path = tmp_path / "resolved-plan.json"
    parent_path = tmp_path / "parent-plans.json"
    provisional_path = tmp_path / f"{family_task_id}.json"
    input_path = tmp_path / "finalize.input.json"
    output_path = tmp_path / "finalized.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    provisional_path.write_bytes(registry.canonical_bytes(provisional) + b"\n")
    input_path.write_bytes(
        registry.canonical_bytes(
            {
                "parent_plans": str(parent_path),
                "provisional": str(provisional_path),
                "family_task_id": family_task_id,
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
            }
        )
        + b"\n"
    )
    observed: dict[str, object] = {}

    def finalize(value: object, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        assert value == provisional
        return {
            "schema_version": 1,
            "kind": "ucm-registry-index-finalization",
            "family_task_id": family_task_id,
        }

    monkeypatch.setattr(registry, "finalize_index", finalize)

    assert (
        cli.main(
            [
                "registry",
                "finalize-index",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["family_task_id"] == family_task_id
    assert observed["resolved_plan"] == plan
    assert observed["expected_plan_sha256"] == plan["resolved_plan_sha256"]


def test_validate_index_parent_cli_reopens_exact_frozen_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The parent artifact gate binds its dynamic families to the plan hash."""
    registry, verify = _modules()
    cli = importlib.import_module("ucm_release.cli")
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses={task["task_id"]: "success" for task in plan["image_tasks"]},
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    plan_path = tmp_path / "resolved-plan.json"
    parent_path = tmp_path / "parent-plans.json"
    input_path = tmp_path / "parent-validation.input.json"
    output_path = tmp_path / "parent-validation.json"
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    input_path.write_bytes(
        registry.canonical_bytes(
            {
                "parent_plans": str(parent_path),
                "parent_artifact": verify.run_bound_artifact_name(
                    f"ucm-index-parent-{plan['source']['commit']}", "42", 3
                ),
                "source_sha": plan["source"]["commit"],
                "resolved_plan": str(plan_path),
                "resolved_plan_sha256": plan["resolved_plan_sha256"],
                "run": {"run_id": "42", "run_attempt": 3},
            }
        )
        + b"\n"
    )

    assert (
        cli.main(
            [
                "artifact",
                "validate-index-parent",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert result["plans_sha256"] == parent["plans_sha256"]


def test_protected_member_workflow_executes_postpublish_record_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted member bridge must reopen its JSON record through a Path."""
    registry, _ = _modules()
    workflow = registry.core.load_yaml(
        REPO_ROOT / ".github" / "workflows" / "_publish-image-member.yml"
    )
    record_step = next(
        step
        for step in workflow["jobs"]["publish-member"]["steps"]
        if step.get("id") == "record"
    )
    run_lines = record_step["run"].splitlines()
    redirect_idx = next(
        i for i, line in enumerate(run_lines) if "member-audit-request.json" in line
    )
    start_idx = redirect_idx
    while start_idx > 0 and run_lines[start_idx - 1].strip().endswith("\\"):
        start_idx -= 1
    jq_lines = [
        run_lines[i].strip().rstrip("\\") for i in range(start_idx, redirect_idx + 1)
    ]
    audit_command = " ".join(jq_lines)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    plan = _protected_resolved_plan(monkeypatch)
    member = _publication_members_for_plan(plan)[0]
    (out_dir / "member-record.json").write_text(
        json.dumps(member, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    plan_dir = tmp_path / "input" / "plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "resolved-plan.json").write_bytes(
        registry.canonical_bytes(plan) + b"\n"
    )
    release_link = tmp_path / ".github" / "release"
    release_link.parent.mkdir(parents=True)
    release_link.symlink_to(RELEASE_ROOT, target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(PYTHON)

    result = subprocess.run(
        ["bash", "-c", audit_command],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RESOLVED_PLAN_SHA256": plan["resolved_plan_sha256"],
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(
        (out_dir / "member-audit-request.json").read_text(encoding="utf-8")
    ) == {
        "lane": "protected-tag",
        "operations": member["operations"],
        "resolved_plan": "input/plan/resolved-plan.json",
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
    }
    cli = importlib.import_module("ucm_release.cli")
    monkeypatch.chdir(tmp_path)
    audit_output = out_dir / "member-audit.json"
    assert (
        cli.main(
            [
                "registry",
                "audit-operations",
                "--input",
                str(out_dir / "member-audit-request.json"),
                "--output",
                str(audit_output),
            ]
        )
        == 0
    )
    missing_plan = out_dir / "member-audit-missing-plan.json"
    missing_plan.write_bytes(
        registry.canonical_bytes(
            {"lane": "protected-tag", "operations": member["operations"]}
        )
        + b"\n"
    )
    rejected_output = out_dir / "member-audit-rejected.json"
    with pytest.raises(SystemExit) as rejected:
        cli.main(
            [
                "registry",
                "audit-operations",
                "--input",
                str(missing_plan),
                "--output",
                str(rejected_output),
            ]
        )
    assert rejected.value.code == 2
    assert not rejected_output.exists()


def test_protected_index_workflow_embeds_reopened_parent_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted index request must carry the validated parent object, not its path."""
    registry, _ = _modules()
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    parent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(plan),
        member_statuses={task["task_id"]: "success" for task in plan["image_tasks"]},
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    workflow = registry.core.load_yaml(
        REPO_ROOT / ".github" / "workflows" / "release-vllm-images-protected.yml"
    )
    publish_step = next(
        step
        for step in workflow["jobs"]["publish-indexes"]["steps"]
        if step.get("id") == "publish"
    )
    request_command = next(
        line.strip()
        for line in publish_step["run"].splitlines()
        if "jq -cn" in line and "> out/request.json" in line
    )

    parent_path = tmp_path / "input" / "parent" / "parent-plans.json"
    parent_path.parent.mkdir(parents=True)
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    plan_path = tmp_path / "input" / "plan" / "resolved-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(registry.canonical_bytes(plan) + b"\n")
    (tmp_path / "out").mkdir()
    release_link = tmp_path / ".github" / "release"
    release_link.parent.mkdir(parents=True)
    release_link.symlink_to(RELEASE_ROOT, target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(PYTHON)
    family_task_id = parent["plans"][0]["family_task_id"]

    result = subprocess.run(
        ["bash", "-c", request_command],
        cwd=tmp_path,
        env={
            **os.environ,
            "FAMILY_TASK_ID": family_task_id,
            "RESOLVED_PLAN_SHA256": plan["resolved_plan_sha256"],
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert registry.core.load_json(tmp_path / "out" / "request.json") == {
        "lane": "protected-tag",
        "parent_plans": parent,
        "family_task_id": family_task_id,
        "resolved_plan": "input/plan/resolved-plan.json",
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
    }


def _protected_preflight() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ucm-tag-preflight",
        "lane": "protected-tag",
        "source_sha": "a" * 40,
        "default_branch_sha": "a" * 40,
        "publication_allowed": True,
        "write_authority": [
            "github-prerelease",
            "ghcr-final-index",
            "ghcr-private-staging",
        ],
    }


def _index_closure_evidence(
    registry: object,
    plan: dict[str, object],
    digest: str,
    *,
    authenticated: bool,
) -> dict[str, object]:
    mode = "authenticated" if authenticated else "anonymous"
    reference = str(plan["target_repository"]) + "@" + digest
    operation = {
        "type": f"registry-{mode}-recursive-validate",
        "capability": "read",
        "reference": reference,
    }
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-remote-validation",
        "source_sha": plan["source_sha"],
        "family_id": plan["family_id"],
        "reference": reference,
        "member_digests": [item["member_digest"] for item in plan["members"]],
        "authenticated": authenticated,
        "tool": {"name": "crane", "version": "0.20.3"},
        "command": ["validate", "--remote", reference, "--fast"],
        "returncode": 0,
        "stdout_sha256": "sha256:" + "1" * 64,
        "stderr_sha256": "sha256:" + "2" * 64,
        "operation": operation,
    }
    return {**payload, "validation_sha256": registry.sha256_value(payload)}


def _index_readback_evidence(
    registry: object,
    plan: dict[str, object],
    digest: str,
    *,
    authenticated: bool,
) -> dict[str, object]:
    mode = "authenticated" if authenticated else "anonymous"
    tag_reference = plan["target_repository"] + ":" + plan["target_tag"]
    digest_reference = plan["target_repository"] + "@" + digest
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-readback",
        "reference": tag_reference,
        "digest": digest,
        "manifest": {
            "media_type": "application/vnd.oci.image.index.v1+json",
            "digest": digest,
            "size": 123,
            "annotations": copy.deepcopy(plan["index_manifest"]["annotations"]),
        },
        "config": None,
        "layers": [],
        "children": copy.deepcopy(plan["index_manifest"]["manifests"]),
        "authenticated": authenticated,
        "operations": [
            {
                "type": f"registry-{mode}-digest-read",
                "capability": "read",
                "reference": tag_reference,
            },
            {
                "type": f"registry-{mode}-manifest-read",
                "capability": "read",
                "reference": digest_reference,
            },
        ],
    }
    return {**payload, "readback_sha256": registry.sha256_value(payload)}


def _provisional_index_evidence(
    registry: object,
    parent: dict[str, object],
    plan: dict[str, object],
    *,
    resolved_plan: dict[str, object],
) -> dict[str, object]:
    digest = plan["expected_index_digest"]
    target = plan["target_repository"] + ":" + plan["target_tag"]
    operations = (
        [{"type": "registry-index-create", "capability": "write", "reference": target}]
        if plan["decision"] == "create"
        else []
    )
    family_task = registry.select_task(
        resolved_plan,
        task_kind="family",
        task_id=plan["family_task_id"],
        expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
    )
    payload = {
        "schema_version": 1,
        "kind": "ucm-registry-index-provisional",
        "status": "authenticated-passed",
        "source_sha": plan["source_sha"],
        "family_id": plan["family_id"],
        "target_repository": plan["target_repository"],
        "target_tag": plan["target_tag"],
        "index_build_key_sha256": plan["index_build_key_sha256"],
        "index_digest": digest,
        "manifest_sha256": digest,
        "member_digests": [item["member_digest"] for item in plan["members"]],
        "authenticated_readback": _index_readback_evidence(
            registry, plan, digest, authenticated=True
        ),
        "authenticated_closure": _index_closure_evidence(
            registry, plan, digest, authenticated=True
        ),
        "collision_model": {
            "model": "observed-state-fail-closed",
            "in_system_serialization": "repository-concurrency",
            "fresh_prewrite_read": True,
            "exact_postwrite_readback": True,
            "external_admin_atomicity": "unavailable",
        },
        "operations": operations,
        "decision": plan["decision"],
        "postwrite_manifest_sha256": digest,
        "preflight_sha256": "sha256:" + "a" * 64,
        "verification_sha256": registry.verify_index(
            plan,
            parent_plans=parent,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )["verification_sha256"],
        "parent_plans_sha256": parent["plans_sha256"],
        "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
        "family_task_id": family_task["task_id"],
        "family_task_sha256": family_task["task_sha256"],
    }
    return {**payload, "provisional_sha256": registry.sha256_value(payload)}


def test_authenticated_evidence_aggregates_exact_dynamic_plan_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authenticated barrier derives N/M solely from the frozen plan."""
    registry, verify = _modules()
    plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(plan)
    statuses = {task["task_id"]: "success" for task in plan["image_tasks"]}
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses=statuses,
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    provisionals = [
        _provisional_index_evidence(registry, parent, family_plan, resolved_plan=plan)
        for family_plan in parent["plans"]
    ]
    member_collection_payload = {
        "schema_version": 1,
        "kind": "ucm-member-artifact-collection",
        "source_sha": plan["source"]["commit"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "member_record_sha256s": {
            task["task_id"]: member["record_sha256"]
            for task, member in zip(plan["image_tasks"], members, strict=True)
        },
        "member_preflight_sha256s": {
            task["task_id"]: "sha256:" + "a" * 64 for task in plan["image_tasks"]
        },
    }
    member_collection = {
        **member_collection_payload,
        "collection_sha256": registry.sha256_value(member_collection_payload),
    }
    provisional_collection_payload = {
        "schema_version": 1,
        "kind": "ucm-provisional-artifact-collection",
        "source_sha": plan["source"]["commit"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "parent_plans_sha256": parent["plans_sha256"],
        "provisional_sha256s": {
            family_plan["family_task_id"]: provisional["provisional_sha256"]
            for family_plan, provisional in zip(
                parent["plans"], provisionals, strict=True
            )
        },
        "provisional_preflight_sha256s": {
            family_plan["family_task_id"]: provisional["preflight_sha256"]
            for family_plan, provisional in zip(
                parent["plans"], provisionals, strict=True
            )
        },
    }
    provisional_collection = {
        **provisional_collection_payload,
        "collection_sha256": registry.sha256_value(provisional_collection_payload),
    }

    evidence = verify.authenticated_registry_publication_evidence(
        member_records=members,
        member_collection=member_collection,
        provisional_indexes=provisionals,
        provisional_collection=provisional_collection,
        parent_plans=parent,
        source_sha=plan["source"]["commit"],
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
        run={"run_id": "42", "run_attempt": 3},
    )

    assert evidence["payload"]["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert len(evidence["payload"]["member_records"]) == len(plan["image_tasks"])
    assert len(evidence["payload"]["provisional_indexes"]) == len(plan["family_tasks"])


@pytest.mark.parametrize(
    ("extra_product", "split_cuda_family_profiles"),
    [(True, False), (False, True)],
)
def test_protected_evidence_accepts_dynamic_family_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    extra_product: bool,
    split_cuda_family_profiles: bool,
) -> None:
    """Dynamic family counts and per-arch profiles survive protected Release E2E."""
    registry, verify = _modules()
    plan = _protected_resolved_plan(
        monkeypatch,
        extra_product=extra_product,
        split_cuda_family_profiles=split_cuda_family_profiles,
    )
    members = _publication_members_for_plan(plan)
    expected_images, expected_families = (8, 4) if extra_product else (6, 3)
    assert len(plan["image_tasks"]) == expected_images
    assert len(plan["family_tasks"]) == expected_families
    if split_cuda_family_profiles:
        cuda_family = next(
            item
            for item in plan["family_tasks"]
            if item["target_repository"].endswith("/vllm-openai")
        )
        cuda_members = [
            next(item for item in plan["image_tasks"] if item["task_id"] == task_id)
            for task_id in cuda_family["image_task_ids"]
        ]
        assert {item["profile_id"] for item in cuda_members} == {
            "cuda130",
            "cuda130-arm-only",
        }
    parent = registry.plan_indexes(
        members,
        _absent_inventory_for_plan(plan),
        member_statuses={task["task_id"]: "success" for task in plan["image_tasks"]},
        lane="protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
    )
    provisionals = [
        _provisional_index_evidence(registry, parent, family_plan, resolved_plan=plan)
        for family_plan in parent["plans"]
    ]
    plans_by_reference = {
        item["target_repository"] + ":" + item["target_tag"]: item
        for item in parent["plans"]
    }

    def anonymous_readback(
        reference: str,
        *,
        anonymous: bool = False,
        public_targets: set[str] | None = None,
    ) -> dict[str, object]:
        assert anonymous is True
        assert public_targets == {reference}
        selected = plans_by_reference[reference]
        return _index_readback_evidence(
            registry,
            selected,
            selected["expected_index_digest"],
            authenticated=False,
        )

    def anonymous_closure(
        selected: dict[str, object],
        *,
        index_digest: str,
        anonymous: bool = False,
    ) -> dict[str, object]:
        assert anonymous is True
        return _index_closure_evidence(
            registry, selected, index_digest, authenticated=False
        )

    monkeypatch.setattr(registry, "readback_reference", anonymous_readback)
    monkeypatch.setattr(registry, "_validate_remote_index_closure", anonymous_closure)
    finalized = [
        registry.finalize_index(
            provisional,
            parent_plans=parent,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )
        for provisional in provisionals
    ]
    member_payload = {
        "schema_version": 1,
        "kind": "ucm-member-artifact-collection",
        "source_sha": plan["source"]["commit"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "member_record_sha256s": {
            task["task_id"]: member["record_sha256"]
            for task, member in zip(plan["image_tasks"], members, strict=True)
        },
        "member_preflight_sha256s": {
            task["task_id"]: "sha256:" + "a" * 64 for task in plan["image_tasks"]
        },
    }
    member_collection = {
        **member_payload,
        "collection_sha256": registry.sha256_value(member_payload),
    }
    provisional_payload = {
        "schema_version": 1,
        "kind": "ucm-provisional-artifact-collection",
        "source_sha": plan["source"]["commit"],
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        "parent_plans_sha256": parent["plans_sha256"],
        "provisional_sha256s": {
            task["task_id"]: provisional["provisional_sha256"]
            for task, provisional in zip(
                plan["family_tasks"], provisionals, strict=True
            )
        },
        "provisional_preflight_sha256s": {
            task["task_id"]: provisional["preflight_sha256"]
            for task, provisional in zip(
                plan["family_tasks"], provisionals, strict=True
            )
        },
    }
    provisional_collection = {
        **provisional_payload,
        "collection_sha256": registry.sha256_value(provisional_payload),
    }
    monkeypatch.setattr(
        verify,
        "_build_fixture_release_manifest",
        lambda: (_ for _ in ()).throw(
            AssertionError("protected evidence reopened the current release manifest")
        ),
    )

    evidence = verify.protected_registry_publication_evidence(
        member_records=members,
        member_collection=member_collection,
        finalized_indexes=finalized,
        provisional_collection=provisional_collection,
        parent_plans=parent,
        source_sha=plan["source"]["commit"],
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
        run={"run_id": "42", "run_attempt": 3},
    )

    assert evidence["payload"]["resolved_plan_sha256"] == plan["resolved_plan_sha256"]
    assert len(evidence["payload"]["members"]) == len(plan["image_tasks"])
    assert len(evidence["payload"]["indexes"]) == len(plan["family_tasks"])
    release_schema = registry.core.load_json(
        RELEASE_ROOT / "schemas" / "release-manifest.schema.json"
    )
    for record in evidence["payload"]["member_records"]:
        registry.core.validate_schema(
            record,
            release_schema["$defs"]["registryMemberRecord"],
            root=release_schema,
        )
    for record in evidence["payload"]["index_records"]:
        registry.core.validate_schema(
            record,
            release_schema["$defs"]["registryIndexRecord"],
            root=release_schema,
        )


def test_staging_tag_and_exact_r1_reconciliation_are_collision_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent creates, identity reuses, and same-name drift cannot retag or roll r2."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    plan_binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }

    build_key = members[0]["build_key_sha256"]
    digest = members[0]["member_digest"]
    staging_repository = resolved_plan["source"]["staging_repository"]
    assert (
        registry.plan_staging_tag(
            build_key,
            digest,
            None,
            staging_repository=staging_repository,
        )["decision"]
        == "create"
    )
    assert (
        registry.plan_staging_tag(
            build_key,
            digest,
            digest,
            staging_repository=staging_repository,
        )["decision"]
        == "reuse"
    )
    with pytest.raises(ValueError, match="staging tag collision"):
        registry.plan_staging_tag(
            build_key,
            digest,
            DIGESTS["observed_drift"],
            staging_repository=staging_repository,
        )
    public_staging = copy.deepcopy(members[0])
    public_staging["staging_visibility"] = "public"
    public_staging["record_sha256"] = registry.sha256_value(
        {key: value for key, value in public_staging.items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="private staging"):
        registry.validate_member_record(public_staging, **plan_binding)

    statuses = {item["task_id"]: "success" for item in resolved_plan["image_tasks"]}
    absent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(resolved_plan),
        member_statuses=statuses,
        lane="protected-tag",
        **plan_binding,
    )
    same_inventory_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-inventory",
        "entries": [
            {
                "repository": item["target_repository"],
                "tag": item["target_tag"],
                "digest": item["expected_index_digest"],
                "build_key_sha256": item["index_build_key_sha256"],
            }
            for item in absent["plans"]
        ],
        "absent": [],
        "operations": [
            operation
            for item in absent["plans"]
            for operation in (
                {
                    "type": "registry-authenticated-digest-read",
                    "capability": "read",
                    "reference": f"{item['target_repository']}:{item['target_tag']}",
                },
                {
                    "type": "registry-authenticated-manifest-read",
                    "capability": "read",
                    "reference": (
                        f"{item['target_repository']}@{item['expected_index_digest']}"
                    ),
                },
            )
        ],
    }
    same_inventory = {
        **same_inventory_payload,
        "inventory_sha256": registry.sha256_value(same_inventory_payload),
    }
    reused = registry.plan_indexes(
        members,
        inventory=same_inventory,
        member_statuses=statuses,
        lane="protected-tag",
        **plan_binding,
    )
    assert [item["decision"] for item in absent["plans"]] == ["create"] * len(
        resolved_plan["family_tasks"]
    )
    assert [item["decision"] for item in reused["plans"]] == ["reuse"] * len(
        resolved_plan["family_tasks"]
    )
    assert all(
        item["index_manifest"]["annotations"]["org.opencontainers.image.source"]
        == f"https://github.com/{resolved_plan['source']['repository']}"
        for item in absent["plans"]
    )
    conflict = copy.deepcopy(same_inventory)
    conflict["entries"][0]["digest"] = DIGESTS["observed_drift"]
    conflict["entries"][0]["build_key_sha256"] = DIGESTS["observed_drift"]
    conflict["operations"][1][
        "reference"
    ] = f"{conflict['entries'][0]['repository']}@{DIGESTS['observed_drift']}"
    conflict["inventory_sha256"] = registry.sha256_value(
        {key: value for key, value in conflict.items() if key != "inventory_sha256"}
    )
    with pytest.raises(ValueError, match="r1 conflict"):
        registry.plan_indexes(
            members,
            inventory=conflict,
            member_statuses=statuses,
            lane="protected-tag",
            **plan_binding,
        )
    with pytest.raises(ValueError, match="feature-candidate.*write-capable"):
        registry.plan_indexes(
            members,
            inventory=_absent_inventory_for_plan(resolved_plan),
            member_statuses=statuses,
            lane="feature-candidate",
            **plan_binding,
        )


@pytest.mark.parametrize("status", ["failed", "cancelled", "skipped", "missing"])
def test_planned_member_barrier_emits_zero_index_writes_for_any_unsuccessful_member(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """No partial family can open any plan-derived final-index write gate."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    statuses = {item["task_id"]: "success" for item in resolved_plan["image_tasks"]}
    statuses[resolved_plan["image_tasks"][0]["task_id"]] = status

    with pytest.raises(ValueError, match="unsuccessful planned tasks") as blocked:
        registry.plan_indexes(
            members,
            inventory=_absent_inventory_for_plan(resolved_plan),
            member_statuses=statuses,
            lane="protected-tag",
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )

    assert getattr(blocked.value, "operations", []) == []


def test_feature_and_protected_operation_audits_are_typed_and_allowlisted() -> None:
    """Feature rejects every write type; protected accepts only exact coordinates."""
    _, verify = _modules()
    staging = "ghcr.io/release-org/ucm-release-staging"
    member_digest = "sha256:" + "1" * 64
    protected_operations = [
        {
            "type": "registry-member-push-by-digest",
            "capability": "write",
            "reference": f"{staging}@{member_digest}",
        },
        {
            "type": "registry-staging-tag-create",
            "capability": "write",
            "reference": f"{staging}:staging-{'2' * 64}",
        },
        {
            "type": "registry-index-create",
            "capability": "write",
            "reference": ("ghcr.io/release-org/vllm-openai:" "v0.21.0-ucm-0.5.0rc1-r1"),
        },
    ]

    with pytest.raises(ValueError, match="feature-candidate.*write"):
        verify.audit_operations(protected_operations, lane="feature-candidate")
    public_targets = {protected_operations[-1]["reference"]}
    protected = verify.audit_operations(
        protected_operations,
        lane="protected-tag",
        staging_repository=staging,
        public_targets=public_targets,
    )
    assert protected == {
        "operation_count": 3,
        "operation_types": [
            "registry-index-create",
            "registry-member-push-by-digest",
            "registry-staging-tag-create",
        ],
        "write_capable_operations": protected_operations,
        "write_count": 3,
    }
    bad = copy.deepcopy(protected_operations)
    bad[-1]["reference"] = "ghcr.io/attacker/vllm-openai:latest"
    with pytest.raises(ValueError, match="allowlist"):
        verify.audit_operations(
            bad,
            lane="protected-tag",
            staging_repository=staging,
            public_targets=public_targets,
        )


def test_extended_registry_read_operations_audit_exact_roles() -> None:
    """Config/layer/visibility evidence has exact typed reference contracts."""
    _, verify = _modules()
    staging = "ghcr.io/release-org/ucm-release-staging"
    operations = [
        {
            "type": "registry-authenticated-config-blob-read",
            "capability": "read",
            "reference": f"{staging}@sha256:{'1' * 64}",
        },
        {
            "type": "registry-authenticated-layer-blob-read",
            "capability": "read",
            "reference": f"{staging}@sha256:{'2' * 64}",
        },
        {
            "type": "registry-anonymous-config-blob-read",
            "capability": "read",
            "reference": f"{staging}@sha256:{'3' * 64}",
        },
        {
            "type": "registry-anonymous-layer-blob-read",
            "capability": "read",
            "reference": f"{staging}@sha256:{'4' * 64}",
        },
        {
            "type": "registry-anonymous-visibility-read",
            "capability": "read",
            "reference": f"{staging}:staging-{'5' * 64}",
        },
    ]

    assert verify.audit_operations(
        operations,
        lane="protected-tag",
        staging_repository=staging,
    ) == {
        "operation_count": 5,
        "operation_types": [
            "registry-anonymous-config-blob-read",
            "registry-anonymous-layer-blob-read",
            "registry-anonymous-visibility-read",
            "registry-authenticated-config-blob-read",
            "registry-authenticated-layer-blob-read",
        ],
        "write_capable_operations": [],
        "write_count": 0,
    }
    invalid = []
    wrong_config_reference = copy.deepcopy(operations)
    wrong_config_reference[0]["reference"] = f"{staging}:staging-{'5' * 64}"
    invalid.append(wrong_config_reference)
    wrong_layer_capability = copy.deepcopy(operations)
    wrong_layer_capability[1]["capability"] = "write"
    invalid.append(wrong_layer_capability)
    wrong_visibility_reference = copy.deepcopy(operations)
    wrong_visibility_reference[-1][
        "reference"
    ] = "ghcr.io/release-org/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1"
    invalid.append(wrong_visibility_reference)
    duplicate = copy.deepcopy(operations)
    duplicate.append(copy.deepcopy(duplicate[0]))
    invalid.append(duplicate)
    for ledger in invalid:
        with pytest.raises(ValueError):
            verify.audit_operations(
                ledger,
                lane="protected-tag",
                staging_repository=staging,
            )


def test_release_manifest_schema_accepts_optional_registry_publication() -> None:
    """Publication evidence is separate from still-unpublished image results."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    manifest["publication"]["registry"] = {
        "status": "candidate",
        "candidate_task_sha256": "sha256:" + "1" * 64,
        "publication_task_sha256": "sha256:" + "2" * 64,
        "member_records": [],
        "index_records": [],
    }

    core.validate_schema(
        manifest,
        core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
    )


def _fake_registry_tool(tmp_path: Path) -> tuple[Path, Path]:
    tool = tmp_path / "crane-v0.20.3"
    log = tmp_path / "transport.log"
    tool.write_text(
        f"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

operation = sys.argv[1]
reference = sys.argv[2]
content_marker = Path({str(log.with_name('content-pushed'))!r})
with Path({str(log)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({{"args": sys.argv[1:], "docker_config": os.environ.get("DOCKER_CONFIG")}}, sort_keys=True) + "\\n")
if operation == "digest":
    if "@sha256:" in reference:
        if not content_marker.exists():
            print("MANIFEST_UNKNOWN", file=sys.stderr)
            raise SystemExit(1)
        print(reference.rsplit("@", 1)[-1])
    elif reference.endswith("v0.21.0-ucm-0.5.0rc1-r1") or "staging-" in reference:
        print("sha256:" + "9" * 64)
    else:
        print("MANIFEST_UNKNOWN", file=sys.stderr)
        raise SystemExit(1)
elif operation == "manifest":
    print(json.dumps({{
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "annotations": {{"io.ucm.release.index-build-key-sha256": "sha256:" + "8" * 64}},
        "manifests": []
    }}, sort_keys=True, separators=(",", ":")))
elif operation == "push":
    content_marker.write_text("pushed", encoding="utf-8")
    print(sys.argv[3])
elif operation == "tag":
    print("tagged")
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool, log


def _valid_oci_archive(
    tmp_path: Path, record: dict[str, object]
) -> tuple[Path, dict[str, object]]:
    registry, _ = _modules()
    layout = tmp_path / "oci-layout-directory"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    config = json.dumps(
        {
            "architecture": record["platform"].split("/", 1)[1],
            "os": "linux",
            "created": record["content_identity"]["created"],
            "config": {"Labels": record["config"]["labels"]},
            "rootfs": {
                "type": "layers",
                "diff_ids": record["content_identity"]["diff_ids"],
            },
            "history": record["content_identity"]["history"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config_hex = hashlib.sha256(config).hexdigest()
    (blobs / config_hex).write_bytes(config)
    layer = b"tiny canonical member layer"
    layer_hex = hashlib.sha256(layer).hexdigest()
    (blobs / layer_hex).write_bytes(layer)
    source_layer = record["content_identity"]["layers"][0]
    manifest_layer = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
        "digest": "sha256:" + layer_hex,
        "size": len(layer),
    }
    if "annotations" in source_layer:
        manifest_layer["annotations"] = copy.deepcopy(source_layer["annotations"])
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": "sha256:" + config_hex,
            "size": len(config),
        },
        "layers": [manifest_layer],
        "annotations": copy.deepcopy(record["manifest"]["annotations"]),
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_hex = hashlib.sha256(manifest_raw).hexdigest()
    (blobs / manifest_hex).write_bytes(manifest_raw)
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + manifest_hex,
                "size": len(manifest_raw),
                "platform": {
                    "os": "linux",
                    "architecture": record["platform"].split("/", 1)[1],
                },
            }
        ],
    }
    (layout / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}', encoding="utf-8"
    )
    (layout / "index.json").write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    archive = tmp_path / "member.oci.tar"
    with tarfile.open(archive, "w") as bundle:
        for path in sorted(layout.rglob("*")):
            bundle.add(
                path,
                arcname=path.relative_to(layout).as_posix(),
                recursive=False,
            )
    updated = copy.deepcopy(record)
    updated["member_digest"] = "sha256:" + manifest_hex
    updated["member_size"] = len(manifest_raw)
    updated["config_digest"] = "sha256:" + config_hex
    updated["manifest"] = {
        "media_type": "application/vnd.oci.image.manifest.v1+json",
        "digest": updated["member_digest"],
        "size": updated["member_size"],
        "annotations": copy.deepcopy(manifest["annotations"]),
    }
    updated["config"] = {
        "media_type": "application/vnd.oci.image.config.v1+json",
        "digest": updated["config_digest"],
        "size": len(config),
        "blob_sha256": updated["config_digest"],
        "labels": copy.deepcopy(record["config"]["labels"]),
    }
    layer_record = {
        "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
        "digest": "sha256:" + layer_hex,
        "size": len(layer),
        "blob_sha256": "sha256:" + layer_hex,
    }
    if "annotations" in manifest_layer:
        layer_record["annotations"] = copy.deepcopy(manifest_layer["annotations"])
    updated["layers"] = [layer_record]
    updated["content_identity"]["manifest_digest"] = updated["member_digest"]
    updated["content_identity"]["config_digest"] = updated["config_digest"]
    updated["content_identity"]["annotations"] = copy.deepcopy(
        updated["manifest"]["annotations"]
    )
    updated["content_identity"]["labels"] = copy.deepcopy(updated["config"]["labels"])
    updated["content_identity"]["layers"] = [copy.deepcopy(manifest_layer)]
    identity_payload = {
        key: value
        for key, value in updated["content_identity"].items()
        if key != "content_identity_sha256"
    }
    updated["content_identity"]["content_identity_sha256"] = registry.sha256_value(
        identity_payload
    )
    updated["content_identity_sha256"] = updated["content_identity"][
        "content_identity_sha256"
    ]
    member_reference = (
        "ghcr.io/release-org/ucm-release-staging@" + updated["member_digest"]
    )
    updated["operations"] = [
        {
            "type": "registry-anonymous-prewrite-visibility-read",
            "capability": "read",
            "reference": (
                "ghcr.io/release-org/ucm-release-staging:" + updated["staging_tag"]
            ),
        },
        {
            "type": "registry-authenticated-staging-prewrite-read",
            "capability": "read",
            "reference": (
                "ghcr.io/release-org/ucm-release-staging:" + updated["staging_tag"]
            ),
        },
        {
            "type": "registry-authenticated-digest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-manifest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-config-blob-read",
            "capability": "read",
            "reference": (
                "ghcr.io/release-org/ucm-release-staging@" + updated["config_digest"]
            ),
        },
        {
            "type": "registry-authenticated-layer-blob-read",
            "capability": "read",
            "reference": (
                "ghcr.io/release-org/ucm-release-staging@"
                + updated["layers"][0]["digest"]
            ),
        },
        {
            "type": "registry-anonymous-visibility-read",
            "capability": "read",
            "reference": (
                "ghcr.io/release-org/ucm-release-staging:" + updated["staging_tag"]
            ),
        },
    ]
    updated["record_sha256"] = registry.sha256_value(
        {key: value for key, value in updated.items() if key != "record_sha256"}
    )
    return archive, updated


def _member_with_buildkit_rewritten_timestamp(
    member: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project the descriptor annotation emitted by BuildKit v0.19.2."""
    registry, _ = _modules()
    record = copy.deepcopy(member or _publication_members()[0])
    identity = record["content_identity"]
    identity["created"] = "2026-08-10T09:22:50Z"
    identity["history"][-1]["created"] = identity["created"]
    identity["layers"][0]["annotations"] = {
        "buildkit/rewritten-timestamp": "1786353770"
    }
    identity["content_identity_sha256"] = registry.sha256_value(
        {
            key: value
            for key, value in identity.items()
            if key != "content_identity_sha256"
        }
    )
    record["content_identity_sha256"] = identity["content_identity_sha256"]
    record["record_sha256"] = registry.sha256_value(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )
    return record


def _rehash_member_content_identity(record: dict[str, object]) -> None:
    registry, _ = _modules()
    identity = record["content_identity"]
    identity["content_identity_sha256"] = registry.sha256_value(
        {
            key: value
            for key, value in identity.items()
            if key != "content_identity_sha256"
        }
    )
    record["content_identity_sha256"] = identity["content_identity_sha256"]
    record["record_sha256"] = registry.sha256_value(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )


def test_inventory_and_anonymous_readback_use_real_subprocess_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current local transport fixture records plan-scoped reads and absence."""
    registry, verify = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    targets = [
        {
            "repository": item["target_repository"],
            "tag": item["target_tag"],
        }
        for item in resolved_plan["family_tasks"]
    ]
    crane, log = _fake_registry_tool(tmp_path)
    caller_config = tmp_path / "caller-docker-config"
    caller_config.mkdir()
    (caller_config / "config.json").write_text(
        '{"auths":{"secret.example":{"auth":"do-not-use"}}}\n', encoding="utf-8"
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(caller_config))
    monkeypatch.setenv("UCM_CALLER_DOCKER_CONFIG", str(caller_config))
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))

    inventory = registry.inventory_registry(targets=targets)
    public_target_refs = {f"{item['repository']}:{item['tag']}" for item in targets}
    assert [(item["repository"], item["tag"]) for item in inventory["entries"]] == [
        (
            "ghcr.io/release-org/vllm-openai",
            "v0.21.0-ucm-0.5.0rc1-r1",
        )
    ]
    assert len(inventory["entries"]) + len(inventory["absent"]) == len(targets)
    assert (
        verify.audit_operations(
            inventory["operations"],
            public_targets=public_target_refs,
        )["write_count"]
        == 0
    )

    anonymous_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "annotations": {
                "io.ucm.release.index-build-key-sha256": "sha256:" + "8" * 64
            },
            "manifests": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    anonymous_digest = "sha256:" + hashlib.sha256(anonymous_manifest).hexdigest()
    anonymous_tool = tmp_path / "anonymous-crane"
    anonymous_log = tmp_path / "anonymous-config.log"
    anonymous_tool.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import os
import sys
config = Path(os.environ["DOCKER_CONFIG"])
if config == Path({str(caller_config)!r}) or (config / "config.json").read_bytes() != b'{{"auths":{{}}}}\\n':
    raise SystemExit(79)
Path({str(anonymous_log)!r}).write_text(str(config), encoding="utf-8")
if sys.argv[1] == "digest":
    print({anonymous_digest!r})
elif sys.argv[1] == "manifest":
    sys.stdout.buffer.write(bytes.fromhex({anonymous_manifest.hex()!r}))
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    anonymous_tool.chmod(0o755)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(anonymous_tool))
    readback = registry.readback_reference(
        "ghcr.io/release-org/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1",
        anonymous=True,
        public_targets=public_target_refs,
    )
    assert readback["digest"] == anonymous_digest
    assert readback["authenticated"] is False
    anonymous_config = Path(anonymous_log.read_text(encoding="utf-8"))
    assert anonymous_config != caller_config
    assert not anonymous_config.exists()


@pytest.mark.parametrize("lane", ["feature-candidate", "manual", "head-marker"])
def test_write_transport_fails_closed_before_subprocess_for_nonprotected_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    """A caller label cannot reach push/tag/index transport outside protected Tag."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    selected_task = resolved_plan["image_tasks"][0]
    plan_binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    archive = tmp_path / "member.tar"
    archive.write_bytes(b"rejected before this input is opened")

    with pytest.raises(ValueError, match="protected-tag"):
        registry.push_member_by_digest(
            archive,
            members[0],
            lane=lane,
            task_id=selected_task["task_id"],
            **plan_binding,
        )
    with pytest.raises(ValueError, match="protected-tag"):
        registry.apply_staging_tag(
            members[0],
            lane=lane,
            task_id=selected_task["task_id"],
            **plan_binding,
        )
    parent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(resolved_plan),
        member_statuses={
            item["task_id"]: "success" for item in resolved_plan["image_tasks"]
        },
        lane="protected-tag",
        **plan_binding,
    )
    with pytest.raises(ValueError, match="protected-tag"):
        registry.create_index(
            parent["plans"][0],
            parent_plans=parent,
            lane=lane,
            **plan_binding,
        )

    assert not log.exists()


def test_protected_write_transport_rechecks_authority_and_exact_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write rechecks protected preflight without reopening catalog state."""
    registry, verify = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    selected_task = resolved_plan["image_tasks"][0]
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    calls = {"preflight": 0}

    def preflight(**_: object) -> dict[str, object]:
        calls["preflight"] += 1
        return _protected_preflight()

    monkeypatch.setattr(registry.core, "tag_preflight", preflight)
    monkeypatch.setattr(
        registry.core,
        "load_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("write transport reopened the current catalog")
        ),
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    archive, member = _valid_oci_archive(tmp_path, members[0])
    result = registry.push_member_by_digest(
        archive,
        member,
        lane="protected-tag",
        resolved_plan=resolved_plan,
        expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        task_id=selected_task["task_id"],
    )

    assert calls["preflight"] == 1
    assert result["digest"] == member["member_digest"]
    assert (
        verify.audit_operations(
            result["operations"],
            lane="protected-tag",
            staging_repository=resolved_plan["source"]["staging_repository"],
        )["write_count"]
        == 1
    )
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [item["args"][0] for item in events] == ["digest", "push", "digest"]
    assert Path(events[1]["args"][1]).name.startswith("ucm-oci-layout-")


def test_fresh_write_preflight_uses_non_current_frozen_source_without_catalog_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _ = _modules()
    plan = _protected_resolved_plan(
        monkeypatch,
        source_overrides={
            "repository": "FutureOrg/unified-cache-next",
            "default_branch": "next",
            "release_tag": "v0.5.0rc1",
            "release_policy": "future-owner-reviewed-v2",
        },
    )
    selected = plan["image_tasks"][0]
    observed: list[dict[str, object]] = []

    def live_preflight(
        *, lane: str, authority: dict[str, object], repository_root: Path
    ) -> dict[str, object]:
        observed.append(copy.deepcopy(authority))
        assert lane == "protected-tag"
        assert repository_root == registry.core.REPO_ROOT
        return _protected_preflight()

    monkeypatch.setattr(
        registry.core,
        "_tag_preflight_live",
        live_preflight,
        raising=False,
    )
    monkeypatch.setattr(
        registry.core,
        "load_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CURRENT_CATALOG_REOPEN")
        ),
    )

    authority = registry._fresh_write_authority(
        "protected-tag",
        resolved_plan=plan,
        expected_plan_sha256=plan["resolved_plan_sha256"],
        task_kind="image",
        task_id=selected["task_id"],
    )

    assert observed == [plan["source"]]
    assert authority["selected_task"] == selected


@pytest.mark.parametrize("entrypoint", ["push", "tag"])
def test_member_write_binds_record_to_selected_task_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    registry, _ = _modules()
    plan = _protected_resolved_plan(monkeypatch)
    selected = plan["image_tasks"][0]
    foreign_record = _publication_members_for_plan(plan)[1]
    transport_calls = 0

    def transport_forbidden() -> str:
        nonlocal transport_calls
        transport_calls += 1
        raise AssertionError("registry transport resolved before task binding")

    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", transport_forbidden)
    arguments = {
        "lane": "protected-tag",
        "resolved_plan": plan,
        "expected_plan_sha256": plan["resolved_plan_sha256"],
        "task_id": selected["task_id"],
    }

    with pytest.raises(ValueError, match="selected image task"):
        if entrypoint == "push":
            registry.push_member_by_digest(
                tmp_path / "must-not-be-opened.tar",
                foreign_record,
                **arguments,
            )
        else:
            registry.apply_staging_tag(foreign_record, **arguments)

    assert transport_calls == 0


def test_buildkit_rewritten_timestamp_annotation_survives_member_push_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact BuildKit timestamp descriptor remains byte-bound through push."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    member_fixture = _publication_members_for_plan(resolved_plan)[0]
    selected_task = resolved_plan["image_tasks"][0]
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    archive, member = _valid_oci_archive(
        tmp_path, _member_with_buildkit_rewritten_timestamp(member_fixture)
    )
    release_schema = registry.load_json(
        registry.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"
    )
    registry.validate_schema(
        member,
        {
            "$schema": release_schema["$schema"],
            "$defs": release_schema["$defs"],
            "$ref": "#/$defs/registryMemberRecord",
        },
    )

    result = registry.push_member_by_digest(
        archive,
        member,
        lane="protected-tag",
        resolved_plan=resolved_plan,
        expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        task_id=selected_task["task_id"],
    )

    expected_annotations = {"buildkit/rewritten-timestamp": "1786353770"}
    assert member["content_identity"]["layers"][0]["annotations"] == (
        expected_annotations
    )
    assert member["layers"][0]["annotations"] == expected_annotations
    assert result["digest"] == member["member_digest"]
    assert [
        json.loads(line)["args"][0]
        for line in log.read_text(encoding="utf-8").splitlines()
    ] == ["digest", "push", "digest"]


def test_annotated_registry_readback_preserves_exact_member_layer_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authenticated readback retains the producer annotation for exact reopen."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    member_fixture = _publication_members_for_plan(resolved_plan)[0]
    archive, member = _valid_oci_archive(
        tmp_path, _member_with_buildkit_rewritten_timestamp(member_fixture)
    )
    with registry.materialize_oci_layout(archive) as materialized:
        manifest_raw = json.dumps(
            materialized["manifest"], sort_keys=True, separators=(",", ":")
        ).encode()
        config_raw = json.dumps(
            materialized["config"], sort_keys=True, separators=(",", ":")
        ).encode()
        layer_raw = (
            materialized["layout_dir"]
            / "blobs"
            / "sha256"
            / member["layers"][0]["digest"].removeprefix("sha256:")
        ).read_bytes()
    crane = tmp_path / "crane"
    crane.write_text(
        f"""#!/usr/bin/env python3
import sys
operation = sys.argv[1]
reference = sys.argv[-1]
if operation == "digest":
    print({member['member_digest']!r})
elif operation == "manifest":
    sys.stdout.buffer.write(bytes.fromhex({manifest_raw.hex()!r}))
elif operation == "blob":
    raw = bytes.fromhex({config_raw.hex()!r}) if reference.endswith({member['config_digest']!r}) else bytes.fromhex({layer_raw.hex()!r})
    sys.stdout.buffer.write(raw)
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))

    reference = registry.FIXTURE_STAGING_REPOSITORY + "@" + member["member_digest"]
    readback = registry.readback_reference(
        reference, staging_repository=resolved_plan["source"]["staging_repository"]
    )
    member["readback_sha256"] = readback["readback_sha256"]
    _rehash_publication_record(member)

    assert readback["layers"][0]["annotations"] == {
        "buildkit/rewritten-timestamp": "1786353770"
    }
    assert (
        registry.verify_member_readback(
            member,
            readback,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )
        == member
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown-descriptor-key", "descriptor fields"),
        ("unknown-annotation", "descriptor annotations"),
        ("non-string", "rewritten timestamp annotation"),
        ("non-decimal", "rewritten timestamp annotation"),
        ("created-mismatch", "differs from created"),
    ],
)
def test_buildkit_rewritten_timestamp_annotation_rejects_noncanonical_variants(
    monkeypatch: pytest.MonkeyPatch, mutation: str, message: str
) -> None:
    """Only the one exact decimal annotation matching config.created is accepted."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    member = _member_with_buildkit_rewritten_timestamp(
        _publication_members_for_plan(resolved_plan)[0]
    )
    identity_layer = member["content_identity"]["layers"][0]
    record_layer = member["layers"][0]
    if mutation == "unknown-descriptor-key":
        identity_layer["urls"] = ["https://example.invalid/layer"]
    elif mutation == "unknown-annotation":
        identity_layer["annotations"]["buildkit/unknown"] = "1786353770"
        record_layer["annotations"] = copy.deepcopy(identity_layer["annotations"])
    elif mutation == "non-string":
        identity_layer["annotations"]["buildkit/rewritten-timestamp"] = 1786353770
        record_layer["annotations"] = copy.deepcopy(identity_layer["annotations"])
    elif mutation == "non-decimal":
        identity_layer["annotations"]["buildkit/rewritten-timestamp"] = "0x6a78b5aa"
        record_layer["annotations"] = copy.deepcopy(identity_layer["annotations"])
    else:
        identity_layer["annotations"]["buildkit/rewritten-timestamp"] = "1786353771"
        record_layer["annotations"] = copy.deepcopy(identity_layer["annotations"])
    _rehash_member_content_identity(member)

    with pytest.raises(ValueError, match=message):
        registry.validate_member_record(
            member,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )


def test_loopback_registry_contract_uses_tiny_scratch_manifests_only() -> None:
    """The disposable publisher never needs a UCM wheel or image build."""
    registry, _ = _modules()
    docker = shutil.which("docker")
    crane = shutil.which("crane")
    if docker is None or crane is None:
        pytest.skip(
            f"loopback prerequisites unavailable: docker={docker}, crane={crane}"
        )

    result = registry.run_loopback_registry_contract(
        docker_binary=docker,
        crane_binary=crane,
    )

    assert result["status"] == "passed"
    assert result["member_count"] == 2
    assert result["registry_member_closure_count"] == 2
    assert result["final_repository_child_closure_count"] == 2
    assert result["cross_repository_copy"] is True
    assert result["negative_mutation"] == "blocked"
    assert all("ghcr.io" not in item["reference"] for item in result["operations"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_repository", "ghcr.io/attacker/forged"),
        ("target_tag", "latest"),
        ("family_id", "forged-family"),
        ("decision", "reuse"),
    ],
)
def test_index_verification_rejects_forged_authority_and_parent_plan(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    """A self-consistent caller rewrite cannot bypass the frozen-plan barrier."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    plan_binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    parent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(resolved_plan),
        member_statuses={
            item["task_id"]: "success" for item in resolved_plan["image_tasks"]
        },
        lane="protected-tag",
        **plan_binding,
    )
    forged_parent = copy.deepcopy(parent)
    forged = forged_parent["plans"][0]
    forged[field] = value
    if field in {"target_repository", "target_tag", "family_id"}:
        manifest, build_key, digest = registry._index_manifest(
            forged["family_id"],
            forged["target_repository"],
            forged["target_tag"],
            forged["members"],
        )
        forged["index_manifest"] = manifest
        forged["index_build_key_sha256"] = build_key
        forged["expected_index_digest"] = digest
    payload = {
        key: copy.deepcopy(item)
        for key, item in forged_parent.items()
        if key != "plans_sha256"
    }
    forged_parent["plans_sha256"] = registry.sha256_value(payload)

    with pytest.raises(ValueError, match="canonical|parent|authority|decision"):
        registry.verify_index(
            forged,
            parent_plans=forged_parent,
            **plan_binding,
        )


def test_index_create_rejects_forged_parent_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a rehashed forged parent cannot reach the Docker subprocess boundary."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    plan_binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    parent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(resolved_plan),
        member_statuses={
            item["task_id"]: "success" for item in resolved_plan["image_tasks"]
        },
        lane="protected-tag",
        **plan_binding,
    )
    forged = copy.deepcopy(parent)
    forged["plans"][0]["target_repository"] = "ghcr.io/attacker/forged"
    forged_payload = {
        key: copy.deepcopy(item)
        for key, item in forged.items()
        if key != "plans_sha256"
    }
    forged["plans_sha256"] = registry.sha256_value(forged_payload)
    docker = tmp_path / "docker"
    marker = tmp_path / "docker-ran"
    docker.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    with pytest.raises(ValueError, match="canonical|parent|authority"):
        registry.create_index(
            forged["plans"][0],
            parent_plans=forged,
            lane="protected-tag",
            **plan_binding,
        )

    assert not marker.exists()


def _fresh_drift_crane(tmp_path: Path, digest: str) -> tuple[Path, Path]:
    crane = tmp_path / "crane"
    mutation_marker = tmp_path / "registry-mutated"
    crane.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import sys

if sys.argv[1] == "version":
    print("0.20.3")
elif sys.argv[1] == "digest":
    print({digest!r})
elif sys.argv[1] in {"tag", "push"}:
    Path({str(mutation_marker)!r}).write_text("mutated", encoding="utf-8")
    print({digest!r})
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    return crane, mutation_marker


def test_staging_tag_rereads_fresh_drift_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caller-claimed absence cannot overwrite a tag that appeared before write."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    record = _publication_members_for_plan(resolved_plan)[0]
    selected_task = resolved_plan["image_tasks"][0]
    crane, marker = _fresh_drift_crane(tmp_path, DIGESTS["observed_drift"])
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    monkeypatch.setenv("UCM_FRESH_DIGEST", DIGESTS["observed_drift"])
    monkeypatch.setenv("UCM_MUTATION_MARKER", str(marker))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    with pytest.raises(ValueError, match="collision|fresh|drift"):
        registry.apply_staging_tag(
            record,
            lane="protected-tag",
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
            task_id=selected_task["task_id"],
        )

    assert not marker.exists()


def test_staging_tag_reports_observed_state_collision_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence must disclose repository serialization and unavailable global CAS."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    record = _publication_members_for_plan(resolved_plan)[0]
    selected_task = resolved_plan["image_tasks"][0]
    crane, marker = _fresh_drift_crane(tmp_path, record["member_digest"])
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    result = registry.apply_staging_tag(
        record,
        lane="protected-tag",
        resolved_plan=resolved_plan,
        expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        task_id=selected_task["task_id"],
    )

    assert result["collision_model"] == {
        "model": "observed-state-fail-closed",
        "in_system_serialization": "repository-concurrency",
        "fresh_prewrite_read": True,
        "exact_postwrite_readback": True,
        "external_admin_atomicity": "unavailable",
    }
    assert result["decision"] == "reuse"
    assert not marker.exists()


def test_index_create_rereads_fresh_drift_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale absent inventory cannot overwrite a final r1 that appeared later."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    members = _publication_members_for_plan(resolved_plan)
    plan_binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    parent = registry.plan_indexes(
        members,
        inventory=_absent_inventory_for_plan(resolved_plan),
        member_statuses={
            item["task_id"]: "success" for item in resolved_plan["image_tasks"]
        },
        lane="protected-tag",
        **plan_binding,
    )
    plan = parent["plans"][0]
    crane, registry_marker = _fresh_drift_crane(tmp_path, DIGESTS["observed_drift"])
    docker = tmp_path / "docker"
    docker_marker = tmp_path / "docker-ran"
    dry_raw = json.dumps(
        plan["index_manifest"], sort_keys=True, separators=(",", ":")
    ).encode()
    docker.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import sys
if "--dry-run" in sys.argv:
    sys.stdout.buffer.write(bytes.fromhex({dry_raw.hex()!r}) + b"\\n")
else:
    Path({str(docker_marker)!r}).write_text("write", encoding="utf-8")
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setenv("UCM_FRESH_DIGEST", DIGESTS["observed_drift"])
    monkeypatch.setenv("UCM_MUTATION_MARKER", str(registry_marker))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    monkeypatch.setattr(registry, "resolve_pinned_buildx", lambda: str(docker))

    with pytest.raises(ValueError, match="conflict|fresh|drift"):
        registry.create_index(
            plan,
            parent_plans=parent,
            lane="protected-tag",
            **plan_binding,
        )

    assert not docker_marker.exists()
    assert not registry_marker.exists()


def test_real_buildx_oci_layout_is_materialized_as_a_crane_directory(
    tmp_path: Path,
) -> None:
    """Production accepts Buildx OCI output without pretending it is Docker-save."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    buildkit_image = image.fixture_image_toolchain_authority()["buildkit_image"]
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable for the real Buildx OCI transport test")
    context = tmp_path / "context"
    context.mkdir()
    (context / "payload.txt").write_text("tiny real Buildx layer\n", encoding="utf-8")
    (context / "Dockerfile").write_text(
        (
            "FROM scratch\n"
            "COPY payload.txt /payload.txt\n"
            "LABEL io.ucm.release.transport=buildx-oci\n"
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "member.oci.tar"
    builder = "ucm-transport-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    absent = subprocess.run(
        [docker, "buildx", "inspect", builder],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert absent.returncode != 0, "test builder must not preexist"
    created = subprocess.run(
        [
            docker,
            "buildx",
            "create",
            "--name",
            builder,
            "--driver",
            "docker-container",
            "--driver-opt",
            f"image={buildkit_image}",
            "--use",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    try:
        bootstrapped = None
        bootstrap_logs: list[str] = []
        for _attempt in range(3):
            bootstrapped = subprocess.run(
                [docker, "buildx", "inspect", builder, "--bootstrap"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            bootstrap_logs.append(bootstrapped.stdout + bootstrapped.stderr)
            if bootstrapped.returncode == 0:
                break
        assert bootstrapped is not None and bootstrapped.returncode == 0, "\n".join(
            bootstrap_logs
        )
        built = subprocess.run(
            [
                docker,
                "buildx",
                "build",
                "--builder",
                builder,
                "--platform",
                "linux/arm64",
                "--provenance=false",
                "--sbom=false",
                "--output",
                f"type=oci,dest={archive}",
                str(context),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        subprocess.run(
            [docker, "buildx", "rm", "--force", builder],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    assert built.returncode == 0, built.stderr
    with tarfile.open(archive) as bundle:
        assert "index.json" in bundle.getnames()
        assert "oci-layout" in bundle.getnames()
        assert "manifest.json" not in bundle.getnames()

    with registry.materialize_oci_layout(archive) as materialized:
        assert materialized["layout_dir"].is_dir()
        assert materialized["manifest"]["mediaType"] == (
            "application/vnd.oci.image.manifest.v1+json"
        )
        assert materialized["config"]["architecture"] == "arm64"
        assert materialized["config"]["os"] == "linux"


def _expanded_member_and_readback(
    member: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    registry, _ = _modules()
    base = copy.deepcopy(member or _publication_members()[0])
    manifest_annotations = copy.deepcopy(base["manifest"]["annotations"])
    config_labels = copy.deepcopy(base["config"]["labels"])
    layer = {
        "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
        "digest": "sha256:" + "5" * 64,
        "size": 51,
        "blob_sha256": "sha256:" + "5" * 64,
    }
    manifest = {
        "media_type": "application/vnd.oci.image.manifest.v1+json",
        "digest": base["member_digest"],
        "size": base["member_size"],
        "annotations": manifest_annotations,
    }
    config = {
        "media_type": "application/vnd.oci.image.config.v1+json",
        "digest": base["config_digest"],
        "size": 73,
        "blob_sha256": base["config_digest"],
        "labels": config_labels,
    }
    member_reference = (
        "ghcr.io/release-org/ucm-release-staging@" + base["member_digest"]
    )
    readback_operations = [
        {
            "type": "registry-authenticated-digest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-manifest-read",
            "capability": "read",
            "reference": member_reference,
        },
        {
            "type": "registry-authenticated-config-blob-read",
            "capability": "read",
            "reference": (
                "ghcr.io/release-org/ucm-release-staging@" + base["config_digest"]
            ),
        },
        {
            "type": "registry-authenticated-layer-blob-read",
            "capability": "read",
            "reference": "ghcr.io/release-org/ucm-release-staging@" + layer["digest"],
        },
    ]
    readback_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-readback",
        "reference": (
            "ghcr.io/release-org/ucm-release-staging@" + base["member_digest"]
        ),
        "digest": base["member_digest"],
        "manifest": manifest,
        "config": config,
        "layers": [layer],
        "children": [],
        "authenticated": True,
        "operations": readback_operations,
    }
    readback = {
        **readback_payload,
        "readback_sha256": registry.sha256_value(readback_payload),
    }
    content_identity = copy.deepcopy(base["content_identity"])
    content_identity.update(
        {
            "manifest_digest": manifest["digest"],
            "config_digest": config["digest"],
            "annotations": copy.deepcopy(manifest_annotations),
            "labels": copy.deepcopy(config_labels),
            "layers": [
                {
                    "mediaType": layer["media_type"],
                    "digest": layer["digest"],
                    "size": layer["size"],
                }
            ],
        }
    )
    content_identity["content_identity_sha256"] = registry.sha256_value(
        {
            key: value
            for key, value in content_identity.items()
            if key != "content_identity_sha256"
        }
    )
    expanded_payload = {
        **{
            key: copy.deepcopy(value)
            for key, value in base.items()
            if key != "record_sha256"
        },
        "content_identity_sha256": content_identity["content_identity_sha256"],
        "content_identity": content_identity,
        "manifest": manifest,
        "config": config,
        "layers": [layer],
        "readback_sha256": readback["readback_sha256"],
        "operations": [
            {
                "type": "registry-anonymous-prewrite-visibility-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/release-org/ucm-release-staging:" + base["staging_tag"]
                ),
            },
            {
                "type": "registry-authenticated-staging-prewrite-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/release-org/ucm-release-staging:" + base["staging_tag"]
                ),
            },
            *copy.deepcopy(readback["operations"]),
            {
                "type": "registry-anonymous-visibility-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/release-org/ucm-release-staging:" + base["staging_tag"]
                ),
            },
        ],
    }
    expanded = {
        **expanded_payload,
        "record_sha256": registry.sha256_value(expanded_payload),
    }
    return expanded, readback


@pytest.mark.parametrize(
    "mutation",
    ["config", "layer", "annotations", "build-key"],
)
def test_member_publication_rejects_readback_closure_mutations(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Config, layer, annotations, and build key all remain byte-bound."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    member, readback = _expanded_member_and_readback(
        _publication_members_for_plan(resolved_plan)[0]
    )
    mutated = copy.deepcopy(readback)
    if mutation == "config":
        mutated["config"]["digest"] = DIGESTS["observed_drift"]
    elif mutation == "layer":
        mutated["layers"][0]["digest"] = DIGESTS["observed_drift"]
    elif mutation == "annotations":
        mutated["manifest"]["annotations"]["io.ucm.release.recipe-sha256"] = DIGESTS[
            "observed_drift"
        ]
    else:
        mutated["config"]["labels"]["io.ucm.release.build-key-sha256"] = DIGESTS[
            "observed_drift"
        ]
    payload = {
        key: copy.deepcopy(value)
        for key, value in mutated.items()
        if key != "readback_sha256"
    }
    mutated["readback_sha256"] = registry.sha256_value(payload)

    with pytest.raises(ValueError, match="config|layer|annotation|build key|closure"):
        registry.verify_member_readback(
            member,
            mutated,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )


@pytest.mark.parametrize(
    ("version", "binary_digest"),
    [("0.20.2", None), ("0.20.3", DIGESTS["observed_drift"])],
)
def test_registry_transport_rejects_unpinned_or_wrong_crane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    binary_digest: str | None,
) -> None:
    """Production resolves crane internally and verifies version plus bytes."""
    registry, _ = _modules()
    crane = tmp_path / "crane"
    crane.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    crane.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    if binary_digest is not None:
        monkeypatch.setattr(
            registry,
            "CRANE_BINARY_SHA256",
            {registry._host_platform_key(): binary_digest},
            raising=False,
        )

    with pytest.raises(ValueError, match="crane v0.20.3|binary digest"):
        registry.resolve_pinned_crane()


def test_registry_readback_fetches_config_and_layer_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production readback traverses the manifest/config/layer byte closure."""
    registry, _ = _modules()
    member, _ = _expanded_member_and_readback()
    config_bytes = b'{"config":{"Labels":{}}}'
    layer_bytes = b"real-compressed-layer-bytes"
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    layer_digest = "sha256:" + hashlib.sha256(layer_bytes).hexdigest()
    raw_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": layer_digest,
                    "size": len(layer_bytes),
                }
            ],
            "annotations": member["manifest"]["annotations"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
    crane = tmp_path / "crane"
    crane.write_text(
        f"""#!/usr/bin/env python3
import sys

op = sys.argv[1]
if op == "digest":
    print({manifest_digest!r})
elif op == "manifest":
    sys.stdout.buffer.write(bytes.fromhex({raw_manifest.hex()!r}))
elif op == "blob":
    digest = sys.argv[2].rsplit("@", 1)[1]
    raw = bytes.fromhex({config_bytes.hex()!r}) if digest == {config_digest!r} else bytes.fromhex({layer_bytes.hex()!r})
    sys.stdout.buffer.write(raw)
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    original_bytes_runner = registry._run_registry_tool_bytes

    def bounded_runner(
        binary: str, arguments: list[str], *, environment: dict[str, str] | None = None
    ) -> bytes:
        if arguments[0] == "blob" and arguments[1].endswith(layer_digest):
            raise AssertionError(
                "registry layer was captured into one stdout bytes object"
            )
        return original_bytes_runner(binary, arguments, environment=environment)

    monkeypatch.setattr(registry, "_run_registry_tool_bytes", bounded_runner)
    result = registry.readback_reference(
        "ghcr.io/release-org/ucm-release-staging@" + manifest_digest,
        staging_repository=registry.FIXTURE_STAGING_REPOSITORY,
    )

    assert result["config"]["blob_sha256"] == config_digest
    assert result["layers"] == [
        {
            "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": layer_digest,
            "size": len(layer_bytes),
            "blob_sha256": layer_digest,
        }
    ]


SECONDARY_RATE_LIMIT_ERRORS = [
    (
        "DENIED: permission_denied: Error from intermediary with HTTP status code "
        '403 "Forbidden" - with-body: {"documentation_url":'
        '"https://docs.github.com/free-pro-team@latest/rest/overview/'
        'rate-limits-for-the-rest-api#about-secondary-rate-limits",'
        '"message":"You have exceeded a secondary rate limit. Please wait a few '
        'minutes before you try again."}'
    ),
    (
        "You have triggered an abuse detection mechanism. Please wait a few "
        "minutes before you try again."
    ),
]


def _flaky_read_crane(
    tmp_path: Path,
    *,
    operation: str,
    error: str,
    failures: int,
) -> tuple[Path, Path, bytes]:
    crane = tmp_path / f"crane-{operation}"
    attempts = tmp_path / f"{operation}-attempts"
    payload = {
        "digest": ("sha256:" + "a" * 64 + "\n").encode(),
        "manifest": b'{"schemaVersion":2}',
        "blob": b"complete-registry-blob",
        "push": b"unused-mutating-output",
        "tag": b"unused-mutating-output",
    }[operation]
    crane.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import sys

attempts = Path({str(attempts)!r})
count = int(attempts.read_text(encoding="utf-8")) + 1 if attempts.exists() else 1
attempts.write_text(str(count), encoding="utf-8")
if sys.argv[1] != {operation!r}:
    raise SystemExit(77)
if count <= {failures}:
    sys.stdout.buffer.write(b"partial-failed-attempt")
    print({error!r}, file=sys.stderr)
    raise SystemExit(1)
sys.stdout.buffer.write(bytes.fromhex({payload.hex()!r}))
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    return crane, attempts, payload


@pytest.mark.parametrize("error", SECONDARY_RATE_LIMIT_ERRORS)
@pytest.mark.parametrize(
    "transport", ["text-digest", "bytes-manifest", "bytes-blob", "stream-blob"]
)
def test_registry_reads_retry_only_explicit_github_secondary_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    transport: str,
) -> None:
    """Every read transport retries GitHub's explicit secondary-limit signal."""
    registry, _ = _modules()
    operation = {
        "text-digest": "digest",
        "bytes-manifest": "manifest",
        "bytes-blob": "blob",
        "stream-blob": "blob",
    }[transport]
    crane, attempts, payload = _flaky_read_crane(
        tmp_path, operation=operation, error=error, failures=1
    )
    sleeps: list[float] = []
    monkeypatch.setattr(registry.time, "sleep", sleeps.append)
    reference = registry.FIXTURE_STAGING_REPOSITORY + "@sha256:" + "b" * 64

    if transport == "text-digest":
        result = registry._run_registry_tool(
            str(crane), ["digest", reference], missing_ok=True
        )
        assert result.stdout == payload.decode()
    elif transport in {"bytes-manifest", "bytes-blob"}:
        assert (
            registry._run_registry_tool_bytes(str(crane), [operation, reference])
            == payload
        )
    else:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        closure, raw = registry._descriptor_closure(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": digest,
                "size": len(payload),
            },
            label="registry layer",
            repository=registry.FIXTURE_STAGING_REPOSITORY,
            crane_binary=str(crane),
            environment=None,
            retain_raw=False,
        )
        assert closure["blob_sha256"] == digest
        assert raw is None

    assert attempts.read_text(encoding="utf-8") == "2"
    assert sleeps == [60.0]


@pytest.mark.parametrize(
    "error",
    [
        "DENIED: denied",
        "UNAUTHORIZED: authentication required",
        (
            "DENIED: permission_denied: Error from intermediary with HTTP status "
            'code 403 "Forbidden"'
        ),
        "proxy CONNECT returned HTTP 403 Forbidden",
    ],
)
@pytest.mark.parametrize("transport", ["text", "bytes", "stream"])
def test_registry_reads_do_not_retry_authorization_or_generic_403_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    transport: str,
) -> None:
    """A2-amd64's plain token denial and proxy failures remain immediate errors."""
    registry, _ = _modules()
    operation = "digest" if transport == "text" else "blob"
    crane, attempts, payload = _flaky_read_crane(
        tmp_path, operation=operation, error=error, failures=99
    )
    sleeps: list[float] = []
    monkeypatch.setattr(registry.time, "sleep", sleeps.append)
    reference = registry.FIXTURE_STAGING_REPOSITORY + ":staging-" + "c" * 64

    if transport == "text":
        with pytest.raises(ValueError, match="fresh Registry read failed"):
            registry._fresh_transport_digest(reference, str(crane))
    elif transport == "bytes":
        with pytest.raises(ValueError, match="registry tool blob failed"):
            registry._run_registry_tool_bytes(str(crane), ["blob", reference])
    else:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        with pytest.raises(ValueError, match="registry tool blob failed"):
            registry._descriptor_closure(
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": digest,
                    "size": len(payload),
                },
                label="registry layer",
                repository=registry.FIXTURE_STAGING_REPOSITORY,
                crane_binary=str(crane),
                environment=None,
                retain_raw=False,
            )

    assert attempts.read_text(encoding="utf-8") == "1"
    assert sleeps == []


@pytest.mark.parametrize("operation", ["push", "tag"])
def test_registry_mutations_never_retry_even_explicit_secondary_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Ambiguous push/tag outcomes never gain an automatic second side effect."""
    registry, _ = _modules()
    crane, attempts, _ = _flaky_read_crane(
        tmp_path,
        operation=operation,
        error=SECONDARY_RATE_LIMIT_ERRORS[0],
        failures=99,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(registry.time, "sleep", sleeps.append)

    with pytest.raises(ValueError, match=f"registry tool {operation} failed"):
        registry._run_registry_tool(
            str(crane),
            [operation, str(tmp_path / "source"), registry.FIXTURE_STAGING_REPOSITORY],
        )

    assert attempts.read_text(encoding="utf-8") == "1"
    assert sleeps == []


def test_registry_secondary_limit_exhaustion_is_never_missing_or_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """missing_ok cannot turn a rate-limit DENIED into absence/privacy evidence."""
    registry, _ = _modules()
    crane, attempts, _ = _flaky_read_crane(
        tmp_path,
        operation="digest",
        error=SECONDARY_RATE_LIMIT_ERRORS[0],
        failures=99,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(registry.time, "sleep", sleeps.append)
    reference = registry.FIXTURE_STAGING_REPOSITORY + ":staging-" + "d" * 64

    with pytest.raises(ValueError) as failure:
        registry._run_registry_tool(str(crane), ["digest", reference], missing_ok=True)

    assert str(failure.value) == (
        "registry tool digest failed: " + SECONDARY_RATE_LIMIT_ERRORS[0]
    )
    assert attempts.read_text(encoding="utf-8") == "4"
    assert sleeps == [60.0, 120.0, 240.0]


def test_registry_secondary_limit_retry_is_bounded_and_discards_partial_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent throttling stops after two retries without mixing partial bytes."""
    registry, _ = _modules()
    crane, attempts, payload = _flaky_read_crane(
        tmp_path,
        operation="blob",
        error=SECONDARY_RATE_LIMIT_ERRORS[0],
        failures=99,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(registry.time, "sleep", sleeps.append)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    with pytest.raises(ValueError, match="secondary rate limit") as failure:
        registry._descriptor_closure(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": digest,
                "size": len(payload),
            },
            label="registry layer",
            repository=registry.FIXTURE_STAGING_REPOSITORY,
            crane_binary=str(crane),
            environment=None,
            retain_raw=False,
        )

    assert str(failure.value) == (
        "registry tool blob failed: " + SECONDARY_RATE_LIMIT_ERRORS[0]
    )
    assert attempts.read_text(encoding="utf-8") == "4"
    assert sleeps == [60.0, 120.0, 240.0]


def _published_registry_evidence() -> dict[str, object]:
    registry, _ = _modules()
    members = _publication_members()
    resolved_plan, _ = _publication_fixture_authorities()
    indexes = []
    for position, authority in enumerate(resolved_plan["family_tasks"], start=1):
        family_id = authority["task_id"]
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-index-publication",
            "status": "passed",
            "source_sha": "a" * 40,
            "family_id": family_id,
            "target_repository": authority["target_repository"],
            "target_tag": authority["target_tag"],
            "index_build_key_sha256": f"sha256:{position + 80:064x}",
            "index_digest": f"sha256:{position + 90:064x}",
            "manifest_sha256": f"sha256:{position + 90:064x}",
            "member_digests": [
                item["member_digest"]
                for item in members
                if item["family_id"] == family_id
            ],
            "authenticated_readback_sha256": f"sha256:{position + 100:064x}",
            "authenticated_closure_sha256": f"sha256:{position + 105:064x}",
            "anonymous_readback_sha256": f"sha256:{position + 110:064x}",
            "anonymous_closure_sha256": f"sha256:{position + 115:064x}",
            "collision_model": {
                "model": "observed-state-fail-closed",
                "in_system_serialization": "repository-concurrency",
                "fresh_prewrite_read": True,
                "exact_postwrite_readback": True,
                "external_admin_atomicity": "unavailable",
            },
            "operations": [
                {
                    "type": "registry-index-create",
                    "capability": "write",
                    "reference": (
                        authority["target_repository"] + ":" + authority["target_tag"]
                    ),
                }
            ],
        }
        payload["record_sha256"] = registry.sha256_value(payload)
        indexes.append(payload)
    return {
        "status": "published",
        "candidate_task_sha256": members[0]["candidate_task_sha256"],
        "publication_task_sha256": members[0]["publication_task_sha256"],
        "member_records": members,
        "index_records": indexes,
    }


def _rehash_publication_record(record: dict[str, object]) -> None:
    registry, _ = _modules()
    payload = {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key != "record_sha256"
    }
    record["record_sha256"] = registry.sha256_value(payload)


@pytest.mark.parametrize(
    ("record_kind", "mutation"),
    [
        ("member_records", "empty"),
        ("member_records", "duplicate"),
        ("index_records", "empty"),
        ("index_records", "duplicate"),
    ],
)
def test_published_registry_schema_requires_nonempty_unique_record_arrays(
    record_kind: str, mutation: str
) -> None:
    """Published evidence record arrays are nonempty and unique at any size."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    originals = evidence[record_kind]
    evidence[record_kind] = (
        []
        if mutation == "empty"
        else [*copy.deepcopy(originals), copy.deepcopy(originals[0])]
    )
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


def test_published_registry_schema_rejects_arbitrary_record_items() -> None:
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    evidence["member_records"][0] = {}
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


def test_published_registry_schema_rejects_malformed_member_records() -> None:
    """Structural schema still rejects members missing their typed OCI payload."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    for position, record in enumerate(evidence["member_records"]):
        record.update(
            {
                "spec_id": f"attacker-spec-{position}",
                "profile_id": "attacker-profile",
                "family_id": "attacker-family",
                "target_repository": "evil.invalid/attacker/repo",
                "target_tag": "latest",
                "annotations": {},
                "manifest": {},
                "config": {},
            }
        )
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


def test_published_registry_schema_defers_index_identity_to_frozen_plan() -> None:
    """Structurally valid index coordinates are authorized by the frozen plan."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    for position, record in enumerate(evidence["index_records"]):
        record.update(
            {
                "family_id": f"attacker-family-{position}",
                "target_repository": "evil.invalid/attacker/repo",
                "target_tag": "latest",
                "operations": [],
            }
        )
    manifest["publication"]["registry"] = evidence

    core.validate_schema(
        manifest,
        core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
    )


@pytest.mark.parametrize(
    ("field", "wrong_canonical_value"),
    [
        ("spec_id", "cuda130-arm64"),
        ("profile_id", "cann900-a2"),
        ("family_id", "cann900-a2"),
        ("platform", "linux/arm64"),
        ("target_repository", "ghcr.io/release-org/vllm-ascend"),
        ("target_tag", "v0.22.1rc1-ucm-0.5.0rc1-r1"),
    ],
)
def test_published_registry_schema_defers_member_identity_to_frozen_plan(
    field: str, wrong_canonical_value: str
) -> None:
    """Allowed identity strings cannot be recombined into a forged member."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    evidence["member_records"][0][field] = wrong_canonical_value
    manifest["publication"]["registry"] = evidence

    core.validate_schema(
        manifest,
        core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
    )


@pytest.mark.parametrize(
    ("annotation", "wrong_canonical_value"),
    [
        ("io.ucm.release.spec-id", "cuda130-arm64"),
        ("io.ucm.release.family-id", "cann900-a2"),
        ("io.ucm.release.platform", "linux/arm64"),
    ],
)
def test_published_registry_schema_defers_nested_identity_to_frozen_plan(
    annotation: str, wrong_canonical_value: str
) -> None:
    """Nested allowed values must still agree with the member's canonical slot."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    evidence["member_records"][0]["annotations"][annotation] = wrong_canonical_value
    manifest["publication"]["registry"] = evidence

    core.validate_schema(
        manifest,
        core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
    )


@pytest.mark.parametrize(
    ("record_kind", "count"), [("member_records", 6), ("index_records", 3)]
)
def test_published_registry_schema_defers_identity_uniqueness_to_frozen_plan(
    record_kind: str, count: int
) -> None:
    """Changing hashes cannot bypass uniqueness of canonical member/family identity."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    original = evidence[record_kind][0]
    duplicates = []
    for position in range(count):
        duplicate = copy.deepcopy(original)
        duplicate["record_sha256"] = f"sha256:{position + 201:064x}"
        duplicates.append(duplicate)
    evidence[record_kind] = duplicates
    manifest["publication"]["registry"] = evidence

    core.validate_schema(
        manifest,
        core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "layer-media-type",
        "operation-type",
        "operation-capability",
        "operation-reference",
    ],
)
def test_published_registry_schema_rejects_noncanonical_content_and_operation_shape(
    mutation: str,
) -> None:
    """Supported blob media and typed canonical coordinates are schema boundaries."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    member = evidence["member_records"][0]
    if mutation == "layer-media-type":
        member["layers"][0]["media_type"] = "evil/type"
    elif mutation == "operation-type":
        member["operations"][0]["type"] = "attacker-write"
    elif mutation == "operation-capability":
        member["operations"][0]["capability"] = "write"
    else:
        member["operations"][0]["reference"] = "evil.invalid/attacker:latest"
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


def test_published_registry_schema_accepts_index_reuse_with_empty_write_ledger() -> (
    None
):
    """An exact r1 reuse emits no index write operation and remains publishable."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    for record in evidence["index_records"]:
        record["operations"] = []
    manifest["publication"]["registry"] = evidence

    core.validate_schema(
        manifest,
        core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
    )


@pytest.mark.parametrize(
    ("record_kind", "mutation"),
    [
        ("member_records", "schema-version-bool"),
        ("member_records", "collision-flag-int"),
        ("index_records", "schema-version-bool"),
        ("index_records", "collision-flag-int"),
    ],
)
def test_registry_schema_rejects_bool_int_equality_masquerades(
    record_kind: str, mutation: str
) -> None:
    """JSON true/1 equality cannot bypass integer and boolean type identity."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    record = evidence[record_kind][0]
    if mutation == "schema-version-bool":
        record["schema_version"] = True
    else:
        record["collision_model"]["fresh_prewrite_read"] = 1
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


@pytest.mark.parametrize("mutation", ["schema-version-bool", "collision-flag-int"])
def test_member_python_validator_rejects_bool_int_equality_masquerades(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Canonical member reopening applies strict Python scalar types too."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    record = _publication_members_for_plan(resolved_plan)[0]
    if mutation == "schema-version-bool":
        record["schema_version"] = True
    else:
        record["collision_model"]["exact_postwrite_readback"] = 1
    _rehash_publication_record(record)

    with pytest.raises(ValueError, match="schema|collision|boolean"):
        registry.validate_member_record(
            record,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )


def test_member_record_binds_full_real_content_identity_and_source_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inherited labels survive, while every canonical release label is authoritative."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    record = _publication_members_for_plan(resolved_plan)[0]
    plan_binding = {
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }

    assert record["config"]["labels"]["base.label"] == "preserved-1"
    assert record["config"]["labels"] == record["content_identity"]["labels"]
    assert registry.validate_member_record(record, **plan_binding) == record

    for label, forged_value in (
        ("org.opencontainers.image.source", "https://example.invalid/attacker"),
        ("org.opencontainers.image.revision", "b" * 40),
        ("io.ucm.release.source-tree", "c" * 40),
        ("io.ucm.release.source-context-sha256", "sha256:" + "d" * 64),
        ("io.ucm.release.task-sha256", "sha256:" + "e" * 64),
        ("io.ucm.release.build-key-sha256", "sha256:" + "f" * 64),
        ("io.ucm.release.wheel-sha256", "sha256:" + "0" * 64),
        ("io.ucm.release.recipe-sha256", "sha256:" + "1" * 64),
    ):
        forged = copy.deepcopy(record)
        forged["config"]["labels"][label] = forged_value
        forged["content_identity"]["labels"][label] = forged_value
        identity_payload = {
            key: value
            for key, value in forged["content_identity"].items()
            if key != "content_identity_sha256"
        }
        forged["content_identity"]["content_identity_sha256"] = registry.sha256_value(
            identity_payload
        )
        forged["content_identity_sha256"] = forged["content_identity"][
            "content_identity_sha256"
        ]
        _rehash_publication_record(forged)
        with pytest.raises(ValueError, match="content|label|source|identity"):
            registry.validate_member_record(forged, **plan_binding)

    inherited_drift = copy.deepcopy(record)
    inherited_drift["config"]["labels"]["base.label"] = "changed-after-build"
    _rehash_publication_record(inherited_drift)
    with pytest.raises(ValueError, match="content|label|identity"):
        registry.validate_member_record(inherited_drift, **plan_binding)


@pytest.mark.parametrize(
    ("record_kind", "mutation"),
    [
        ("member_records", "index-create"),
        ("member_records", "public-visibility"),
        ("member_records", "duplicate"),
        ("index_records", "member-read"),
        ("index_records", "duplicate-create"),
    ],
)
def test_registry_schema_rejects_cross_role_and_duplicate_operations(
    record_kind: str, mutation: str
) -> None:
    """Member and index ledgers accept only operations belonging to their roles."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core._build_fixture_release_manifest()
    evidence = _published_registry_evidence()
    record = evidence[record_kind][0]
    staging_digest = "ghcr.io/release-org/ucm-release-staging@" + "sha256:" + "1" * 64
    public_target = "ghcr.io/release-org/vllm-ascend:" "v0.22.1rc1-a3-ucm-0.5.0rc1-r1"
    if mutation == "index-create":
        record["operations"] = [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": public_target,
            }
        ]
    elif mutation == "public-visibility":
        record["operations"] = [
            {
                "type": "registry-anonymous-visibility-read",
                "capability": "read",
                "reference": public_target,
            }
        ]
    elif mutation == "duplicate":
        record["operations"] = [
            copy.deepcopy(record["operations"][0]),
            copy.deepcopy(record["operations"][0]),
        ]
    elif mutation == "member-read":
        record["operations"] = [
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": staging_digest,
            }
        ]
    else:
        record["operations"] = [
            copy.deepcopy(record["operations"][0]),
            copy.deepcopy(record["operations"][0]),
        ]
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "index-create",
        "public-visibility",
        "duplicate",
        "config-wrong-reference",
        "layer-wrong-capability",
    ],
)
def test_member_python_validator_rejects_cross_role_and_duplicate_operations(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Self-hashed member ledgers cannot smuggle index/public/duplicate operations."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    record = _publication_members_for_plan(resolved_plan)[0]
    public_target = "ghcr.io/release-org/vllm-ascend:" "v0.22.1rc1-a3-ucm-0.5.0rc1-r1"
    if mutation == "index-create":
        record["operations"] = [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": public_target,
            }
        ]
    elif mutation == "public-visibility":
        record["operations"] = [
            {
                "type": "registry-anonymous-visibility-read",
                "capability": "read",
                "reference": public_target,
            }
        ]
    elif mutation == "duplicate":
        record["operations"] = [
            copy.deepcopy(record["operations"][0]),
            copy.deepcopy(record["operations"][0]),
        ]
    elif mutation == "config-wrong-reference":
        operation = next(
            item
            for item in record["operations"]
            if item["type"] == "registry-authenticated-config-blob-read"
        )
        operation["reference"] = (
            "ghcr.io/release-org/ucm-release-staging@" + record["member_digest"]
        )
    else:
        operation = next(
            item
            for item in record["operations"]
            if item["type"] == "registry-authenticated-layer-blob-read"
        )
        operation["capability"] = "write"
    _rehash_publication_record(record)

    with pytest.raises(ValueError, match="operation|duplicate|reference|role"):
        registry.validate_member_record(
            record,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )


def test_validate_index_record_reopens_only_canonical_role_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index evidence reopens hash, identity, booleans, and exact write role."""
    registry, _ = _modules()
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    _, _, parent_plans, plan_binding = _resolved_publication_context(monkeypatch)
    plan = parent_plans["plans"][0]
    record = _published_registry_evidence()["index_records"][0]
    record.update(
        {
            "family_id": plan["family_id"],
            "target_repository": plan["target_repository"],
            "target_tag": plan["target_tag"],
            "index_build_key_sha256": plan["index_build_key_sha256"],
            "member_digests": [item["member_digest"] for item in plan["members"]],
            "operations": [
                {
                    "type": "registry-index-create",
                    "capability": "write",
                    "reference": (plan["target_repository"] + ":" + plan["target_tag"]),
                }
            ],
        }
    )
    _rehash_publication_record(record)
    assert (
        registry.validate_index_record(
            record,
            parent_plans=parent_plans,
            **plan_binding,
        )
        == record
    )

    mutations = []
    schema_bool = copy.deepcopy(record)
    schema_bool["schema_version"] = True
    mutations.append(schema_bool)
    collision_int = copy.deepcopy(record)
    collision_int["collision_model"]["fresh_prewrite_read"] = 1
    mutations.append(collision_int)
    member_read = copy.deepcopy(record)
    member_read["operations"] = [
        {
            "type": "registry-authenticated-manifest-read",
            "capability": "read",
            "reference": ("ghcr.io/release-org/ucm-release-staging@sha256:" + "1" * 64),
        }
    ]
    mutations.append(member_read)
    duplicate = copy.deepcopy(record)
    duplicate["operations"] = [
        copy.deepcopy(record["operations"][0]),
        copy.deepcopy(record["operations"][0]),
    ]
    mutations.append(duplicate)
    wrong_reference = copy.deepcopy(record)
    wrong_reference["operations"][0][
        "reference"
    ] = "ghcr.io/attacker/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1"
    mutations.append(wrong_reference)
    for mutation in mutations:
        _rehash_publication_record(mutation)
        with pytest.raises(ValueError):
            registry.validate_index_record(
                mutation,
                parent_plans=parent_plans,
                **plan_binding,
            )


def test_index_create_uses_exact_dry_run_bytes_and_postwrite_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write inputs, expected digest, and post-read bytes are one exact object."""
    registry, _ = _modules()
    resolved_plan, _, parent, plan_binding = _resolved_publication_context(monkeypatch)
    plan = parent["plans"][0]
    descriptors = []
    for member in plan["members"]:
        descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": member["member_digest"],
                "size": member["member_size"],
                "platform": {
                    "os": "linux",
                    "architecture": member["platform"].split("/", 1)[1],
                },
                "annotations": {
                    "io.ucm.release.build-key-sha256": member["build_key_sha256"],
                    "io.ucm.release.spec-id": member["spec_id"],
                },
            }
        )
    dry_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": descriptors,
        "annotations": {
            "org.opencontainers.image.source": (
                f"https://github.com/{resolved_plan['source']['repository']}"
            ),
            "io.ucm.release.family-id": plan["family_id"],
            "io.ucm.release.index-build-key-sha256": plan["index_build_key_sha256"],
            "io.ucm.release.source-sha": plan["source_sha"],
        },
    }
    raw = json.dumps(dry_manifest, indent=2).encode()
    expected = "sha256:" + hashlib.sha256(raw).hexdigest()
    marker = tmp_path / "written"
    invocation_log = tmp_path / "buildx-invocations.jsonl"
    buildx = tmp_path / "docker-buildx"
    buildx.write_text(
        f"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
files = [Path(args[index + 1]).read_bytes().decode() for index, value in enumerate(args) if value == "--file"]
with Path({str(invocation_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({{"args": args, "environment": dict(os.environ), "files": files}}, sort_keys=True) + "\\n")
if "--dry-run" in args:
    sys.stdout.buffer.write(bytes.fromhex({raw.hex()!r}) + b"\\n")
else:
    Path({str(marker)!r}).write_text("written", encoding="utf-8")
""",
        encoding="utf-8",
    )
    buildx.chmod(0o755)
    attacker_marker = tmp_path / "path-docker-ran"
    attacker = tmp_path / "docker"
    attacker.write_text(
        f"#!/bin/sh\ntouch {attacker_marker}\nexit 91\n", encoding="utf-8"
    )
    attacker.chmod(0o755)
    crane = tmp_path / "crane"
    crane.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import sys

if sys.argv[1] == "digest":
    if not Path({str(marker)!r}).exists():
        print("MANIFEST_UNKNOWN", file=sys.stderr)
        raise SystemExit(1)
    print({expected!r})
elif sys.argv[1] == "manifest":
    sys.stdout.buffer.write(bytes.fromhex({raw.hex()!r}))
elif sys.argv[1] == "validate":
    pass
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    monkeypatch.setattr(
        registry, "resolve_pinned_buildx", lambda: str(buildx), raising=False
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/runner/home")
    monkeypatch.setenv("DOCKER_CONFIG", "/runner/docker-config")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "must-not-leak")

    result = registry.create_index(
        plan,
        parent_plans=parent,
        lane="protected-tag",
        **plan_binding,
    )

    assert result["index_digest"] == expected
    assert result["manifest_sha256"] == expected
    assert result["postwrite_manifest_sha256"] == expected
    assert set(result) == registry.INDEX_RECORD_KEYS | {
        "decision",
        "postwrite_manifest_sha256",
        "preflight_sha256",
        "verification_sha256",
    }
    strict_record = {key: result[key] for key in registry.INDEX_RECORD_KEYS}
    assert (
        registry.validate_index_record(
            strict_record,
            parent_plans=parent,
            **plan_binding,
        )
        == strict_record
    )
    assert result["decision"] == "create"
    assert result["operations"] == [
        {
            "type": "registry-index-create",
            "capability": "write",
            "reference": plan["target_repository"] + ":" + plan["target_tag"],
        }
    ]
    invocations = [json.loads(line) for line in invocation_log.read_text().splitlines()]
    assert len(invocations) == 2
    assert invocations[0]["args"][:2] == ["imagetools", "create"]
    assert invocations[0]["files"] == [
        f"{registry.FIXTURE_STAGING_REPOSITORY}@{member['member_digest']}"
        for member in plan["members"]
    ]
    assert "GITHUB_TOKEN" not in invocations[0]["environment"]
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in invocations[0]["environment"]
    assert "PATH" not in invocations[0]["environment"]
    assert "--dry-run" in invocations[0]["args"]
    assert "--dry-run" not in invocations[1]["args"]
    assert not attacker_marker.exists()


def test_index_prepare_defers_anonymous_readback_until_strict_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty draft can be created between authenticated prepare and anonymous close."""
    registry, verify = _modules()
    _, _, parent, plan_binding = _resolved_publication_context(monkeypatch)
    plan = parent["plans"][0]
    target = plan["target_repository"] + ":" + plan["target_tag"]
    digest = plan["expected_index_digest"]
    collision_model = {
        "model": "observed-state-fail-closed",
        "in_system_serialization": "repository-concurrency",
        "fresh_prewrite_read": True,
        "exact_postwrite_readback": True,
        "external_admin_atomicity": "unavailable",
    }
    create_operation = {
        "type": "registry-index-create",
        "capability": "write",
        "reference": target,
    }
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")
    monkeypatch.setattr(registry, "resolve_pinned_buildx", lambda: "/pinned/buildx")
    monkeypatch.setattr(
        registry,
        "_create_index_transport",
        lambda **_: {
            "rendered": copy.deepcopy(plan["index_manifest"]),
            "raw_manifest": registry.canonical_bytes(plan["index_manifest"]),
            "index_digest": digest,
            "decision": "create",
            "collision_model": collision_model,
            "operations": [create_operation],
            "postwrite_manifest_sha256": digest,
        },
    )
    calls: list[bool] = []

    def fake_readback(
        reference: str, *, anonymous: bool = False, **_kwargs: object
    ) -> dict[str, object]:
        calls.append(anonymous)
        prefix = "registry-anonymous" if anonymous else "registry-authenticated"
        operations = [
            {
                "type": f"{prefix}-digest-read",
                "capability": "read",
                "reference": target,
            },
            {
                "type": f"{prefix}-manifest-read",
                "capability": "read",
                "reference": plan["target_repository"] + "@" + digest,
            },
        ]
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": reference,
            "digest": digest,
            "manifest": {
                "media_type": "application/vnd.oci.image.index.v1+json",
                "digest": digest,
                "size": len(registry.canonical_bytes(plan["index_manifest"])),
                "annotations": copy.deepcopy(plan["index_manifest"]["annotations"]),
            },
            "config": None,
            "layers": [],
            "children": copy.deepcopy(plan["index_manifest"]["manifests"]),
            "authenticated": not anonymous,
            "operations": operations,
        }
        return {**payload, "readback_sha256": registry.sha256_value(payload)}

    monkeypatch.setattr(registry, "readback_reference", fake_readback)
    closure_calls: list[bool] = []

    def fake_closure(
        closure_plan: dict[str, object],
        *,
        index_digest: str,
        anonymous: bool = False,
    ) -> dict[str, object]:
        closure_calls.append(anonymous)
        mode = "anonymous" if anonymous else "authenticated"
        reference = closure_plan["target_repository"] + "@" + index_digest
        operation = {
            "type": f"registry-{mode}-recursive-validate",
            "capability": "read",
            "reference": reference,
        }
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-index-remote-validation",
            "source_sha": closure_plan["source_sha"],
            "family_id": closure_plan["family_id"],
            "reference": reference,
            "member_digests": [
                item["member_digest"] for item in closure_plan["members"]
            ],
            "authenticated": not anonymous,
            "tool": {"name": "crane", "version": "0.20.3"},
            "command": ["validate", "--remote", reference, "--fast"],
            "returncode": 0,
            "stdout_sha256": "sha256:" + "1" * 64,
            "stderr_sha256": "sha256:" + "2" * 64,
            "operation": operation,
        }
        return {
            **payload,
            "validation_sha256": registry.sha256_value(payload),
        }

    monkeypatch.setattr(
        registry, "_validate_remote_index_closure", fake_closure, raising=False
    )

    provisional = registry.prepare_index(
        plan,
        parent_plans=parent,
        lane="protected-tag",
        **plan_binding,
    )

    assert calls == [False]
    assert closure_calls == [False]
    assert provisional["kind"] == "ucm-registry-index-provisional"
    assert "anonymous_readback_sha256" not in provisional
    assert (
        registry.validate_provisional_index(
            provisional,
            parent_plans=parent,
            **plan_binding,
        )
        == provisional
    )
    assert (
        verify.audit_operations(
            provisional["operations"],
            lane="protected-tag",
            public_targets={target},
        )["write_count"]
        == 1
    )

    finalized = registry.finalize_index(
        provisional,
        parent_plans=parent,
        **plan_binding,
    )
    final = finalized["record"]

    assert calls == [False, True]
    assert closure_calls == [False, True]
    assert finalized["provisional"] == provisional
    assert finalized["provisional_sha256"] == provisional["provisional_sha256"]
    assert finalized["authenticated_readback"] == provisional["authenticated_readback"]
    assert finalized["anonymous_readback"]["authenticated"] is False
    assert finalized["anonymous_closure"]["authenticated"] is False
    assert finalized["operation_audit"]["anonymous"]["write_count"] == 0
    assert set(final) == registry.INDEX_RECORD_KEYS
    assert final["anonymous_readback_sha256"] != final["authenticated_readback_sha256"]
    assert (
        registry.validate_index_record(
            final,
            parent_plans=parent,
            **plan_binding,
        )
        == final
    )
    assert final["source_sha"] == "a" * 40
    forged_finalized = copy.deepcopy(finalized)
    forged_finalized["provisional"]["preflight_sha256"] = "sha256:" + "f" * 64
    forged_finalized["finalization_sha256"] = registry.sha256_value(
        {
            key: value
            for key, value in forged_finalized.items()
            if key != "finalization_sha256"
        }
    )
    with pytest.raises(ValueError):
        registry.validate_finalized_index(
            forged_finalized,
            parent_plans=parent,
            **plan_binding,
        )


def test_index_plans_reject_mixed_sources_and_writer_rechecks_live_tag_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No final r1 transport may begin for mixed or stale protected sources."""
    registry, _ = _modules()
    resolved_plan, members, parent, plan_binding = _resolved_publication_context(
        monkeypatch
    )
    mixed = copy.deepcopy(members)
    mixed[0]["source_sha"] = "b" * 40
    mixed[0]["record_sha256"] = registry.sha256_value(
        {key: value for key, value in mixed[0].items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="source"):
        registry.plan_indexes(
            mixed,
            inventory=_absent_inventory_for_plan(resolved_plan),
            member_statuses={
                item["task_id"]: "success" for item in resolved_plan["image_tasks"]
            },
            lane="protected-tag",
            **plan_binding,
        )

    stale_preflight = _protected_preflight()
    stale_preflight["source_sha"] = "b" * 40
    monkeypatch.setattr(registry.core, "tag_preflight", lambda **_: stale_preflight)
    called = False

    def transport(**_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("transport must not run for a stale tag source")

    monkeypatch.setattr(registry, "_create_index_transport", transport)
    with pytest.raises(ValueError, match="source"):
        registry.prepare_index(
            parent["plans"][0],
            parent_plans=parent,
            lane="protected-tag",
            **plan_binding,
        )
    assert called is False

    head_mismatch = _protected_preflight()
    head_mismatch["default_branch_sha"] = "b" * 40
    monkeypatch.setattr(registry.core, "tag_preflight", lambda **_: head_mismatch)
    with pytest.raises(ValueError, match="default branch|first publication"):
        registry.prepare_index(
            parent["plans"][0],
            parent_plans=parent,
            lane="protected-tag",
            **plan_binding,
        )
    assert called is False


def test_index_prepare_fails_when_child_manifest_is_missing_from_final_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index descriptor alone cannot prove its child is pullable cross-repository."""
    registry, _ = _modules()
    _, _, parent, plan_binding = _resolved_publication_context(monkeypatch)
    plan = parent["plans"][0]
    digest = plan["expected_index_digest"]
    target = plan["target_repository"] + ":" + plan["target_tag"]
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")
    monkeypatch.setattr(registry, "resolve_pinned_buildx", lambda: "/pinned/buildx")
    monkeypatch.setattr(
        registry,
        "_create_index_transport",
        lambda **_: {
            "rendered": copy.deepcopy(plan["index_manifest"]),
            "raw_manifest": registry.canonical_bytes(plan["index_manifest"]),
            "index_digest": digest,
            "decision": "create",
            "collision_model": {
                "model": "observed-state-fail-closed",
                "in_system_serialization": "repository-concurrency",
                "fresh_prewrite_read": True,
                "exact_postwrite_readback": True,
                "external_admin_atomicity": "unavailable",
            },
            "operations": [
                {
                    "type": "registry-index-create",
                    "capability": "write",
                    "reference": target,
                }
            ],
            "postwrite_manifest_sha256": digest,
        },
    )
    index_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-readback",
        "reference": target,
        "digest": digest,
        "manifest": {
            "media_type": "application/vnd.oci.image.index.v1+json",
            "digest": digest,
            "size": 1,
            "annotations": copy.deepcopy(plan["index_manifest"]["annotations"]),
        },
        "config": None,
        "layers": [],
        "children": copy.deepcopy(plan["index_manifest"]["manifests"]),
        "authenticated": True,
        "operations": [
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": target,
            },
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": plan["target_repository"] + "@" + digest,
            },
        ],
    }
    monkeypatch.setattr(
        registry,
        "readback_reference",
        lambda *_args, **_kwargs: {
            **index_payload,
            "readback_sha256": registry.sha256_value(index_payload),
        },
    )
    monkeypatch.setattr(
        registry,
        "_validate_remote_index_closure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("final repository child manifest is missing")
        ),
        raising=False,
    )
    with pytest.raises(ValueError, match="child manifest"):
        registry.prepare_index(
            plan,
            parent_plans=parent,
            lane="protected-tag",
            **plan_binding,
        )


def test_index_provisional_cannot_forge_parent_or_finalize_with_authenticated_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize derives the target from the parent and requires an anonymous readback."""
    registry, _ = _modules()
    resolved_plan, _, parent, plan_binding = _resolved_publication_context(monkeypatch)
    plan = parent["plans"][0]
    provisional = _provisional_index_evidence(
        registry,
        parent,
        plan,
        resolved_plan=resolved_plan,
    )
    assert (
        registry.validate_provisional_index(
            provisional,
            parent_plans=parent,
            **plan_binding,
        )
        == provisional
    )

    forged = copy.deepcopy(provisional)
    forged["target_tag"] = "attacker"
    forged["provisional_sha256"] = registry.sha256_value(
        {key: value for key, value in forged.items() if key != "provisional_sha256"}
    )
    with pytest.raises(ValueError):
        registry.validate_provisional_index(
            forged,
            parent_plans=parent,
            **plan_binding,
        )

    monkeypatch.setattr(
        registry,
        "readback_reference",
        lambda *_args, **_kwargs: provisional["authenticated_readback"],
    )
    with pytest.raises(ValueError, match="anonymous"):
        registry.finalize_index(
            provisional,
            parent_plans=parent,
            **plan_binding,
        )


def test_provisional_and_finalized_index_readbacks_are_strict_and_mode_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-job evidence rejects loose types, extra keys, and mixed auth ledgers."""
    registry, _ = _modules()
    resolved_plan, _, parent, plan_binding = _resolved_publication_context(monkeypatch)
    plan = parent["plans"][0]
    target = plan["target_repository"] + ":" + plan["target_tag"]
    digest = plan["expected_index_digest"]

    def readback(*, authenticated: bool) -> dict[str, object]:
        mode = "authenticated" if authenticated else "anonymous"
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": target,
            "digest": digest,
            "manifest": {
                "media_type": "application/vnd.oci.image.index.v1+json",
                "digest": digest,
                "size": 123,
                "annotations": copy.deepcopy(plan["index_manifest"]["annotations"]),
            },
            "config": None,
            "layers": [],
            "children": copy.deepcopy(plan["index_manifest"]["manifests"]),
            "authenticated": authenticated,
            "operations": [
                {
                    "type": f"registry-{mode}-digest-read",
                    "capability": "read",
                    "reference": target,
                },
                {
                    "type": f"registry-{mode}-manifest-read",
                    "capability": "read",
                    "reference": plan["target_repository"] + "@" + digest,
                },
            ],
        }
        return {**payload, "readback_sha256": registry.sha256_value(payload)}

    provisional = _provisional_index_evidence(
        registry,
        parent,
        plan,
        resolved_plan=resolved_plan,
    )
    auth = provisional["authenticated_readback"]
    assert (
        registry.validate_provisional_index(
            provisional,
            parent_plans=parent,
            **plan_binding,
        )
        == provisional
    )

    for mutate in (
        lambda value: value.update(schema_version=True),
        lambda value: value["authenticated_readback"].update(extra="forged"),
        lambda value: value["authenticated_readback"].update(authenticated=1),
        lambda value: value["authenticated_readback"]["manifest"].update(
            extra="forged"
        ),
    ):
        forged = copy.deepcopy(provisional)
        mutate(forged)
        readback_payload = forged["authenticated_readback"]
        readback_payload["readback_sha256"] = registry.sha256_value(
            {
                key: item
                for key, item in readback_payload.items()
                if key != "readback_sha256"
            }
        )
        forged["provisional_sha256"] = registry.sha256_value(
            {key: item for key, item in forged.items() if key != "provisional_sha256"}
        )
        with pytest.raises(ValueError):
            registry.validate_provisional_index(
                forged,
                parent_plans=parent,
                **plan_binding,
            )

    mixed = readback(authenticated=False)
    mixed["operations"] = copy.deepcopy(auth["operations"])
    mixed["readback_sha256"] = registry.sha256_value(
        {key: item for key, item in mixed.items() if key != "readback_sha256"}
    )
    monkeypatch.setattr(registry, "readback_reference", lambda *_args, **_kwargs: mixed)
    with pytest.raises(ValueError):
        registry.finalize_index(
            provisional,
            parent_plans=parent,
            **plan_binding,
        )


def test_index_rerun_defers_same_build_key_digest_to_exact_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buildx formatting may change bytes without changing parsed index intent."""
    registry, _ = _modules()
    resolved_plan, members, absent, plan_binding = _resolved_publication_context(
        monkeypatch
    )
    statuses = {item["task_id"]: "success" for item in resolved_plan["image_tasks"]}
    plan = absent["plans"][0]
    buildx_raw = json.dumps(plan["index_manifest"], indent=2).encode()
    actual_digest = "sha256:" + hashlib.sha256(buildx_raw).hexdigest()
    assert actual_digest != plan["expected_index_digest"]
    inventory_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-inventory",
        "entries": [
            {
                "repository": plan["target_repository"],
                "tag": plan["target_tag"],
                "digest": actual_digest,
                "build_key_sha256": plan["index_build_key_sha256"],
            }
        ],
        "absent": [
            {
                "repository": item["target_repository"],
                "tag": item["target_tag"],
            }
            for item in absent["plans"][1:]
        ],
        "operations": [
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": f"{plan['target_repository']}:{plan['target_tag']}",
            },
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": f"{plan['target_repository']}@{actual_digest}",
            },
            *[
                {
                    "type": "registry-authenticated-digest-read",
                    "capability": "read",
                    "reference": (f"{item['target_repository']}:{item['target_tag']}"),
                }
                for item in absent["plans"][1:]
            ],
        ],
    }
    inventory = {
        **inventory_payload,
        "inventory_sha256": registry.sha256_value(inventory_payload),
    }

    rerun = registry.plan_indexes(
        members,
        inventory=inventory,
        member_statuses=statuses,
        lane="protected-tag",
        **plan_binding,
    )

    assert rerun["plans"][0]["decision"] == "reuse"


def test_publish_member_rederives_record_from_image_result_and_registry_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot supply the publication record that authorizes its own write."""
    registry, verify = _modules()
    image = importlib.import_module("ucm_release.image")
    resolved_plan = _protected_resolved_plan(monkeypatch)
    selected_task = resolved_plan["image_tasks"][0]
    member_fixture = _publication_members_for_plan(resolved_plan)[0]
    plan_binding = {
        "selected_task": selected_task,
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    archive, expected = _valid_oci_archive(
        tmp_path, _member_with_buildkit_rewritten_timestamp(member_fixture)
    )
    image_result = {
        "candidate_kind": "real-candidate",
        "unpublished": True,
        "spec_id": expected["spec_id"],
        "profile_id": expected["profile_id"],
        "family_id": expected["family_id"],
        "target_platform": expected["platform"],
        "target_repository": expected["target_repository"],
        "target_tag": expected["target_tag"],
        "task_key": expected["candidate_task_sha256"],
        "build_key_sha256": expected["build_key_sha256"],
        "recipe_sha256": expected["recipe_sha256"],
        "content_identity_sha256": expected["content_identity_sha256"],
        "result_sha256": expected["image_result_sha256"],
        "source": {
            **copy.deepcopy(expected["content_identity"]["source"]),
            "task_sha256": expected["candidate_task_sha256"],
            "wheel_build_key": "sha256:" + "e" * 64,
        },
        "wheel": {"sha256": expected["wheel_sha256"]},
        "oci": {"digest": expected["member_digest"], "published": False},
        "content_identity": copy.deepcopy(expected["content_identity"]),
    }
    monkeypatch.setattr(
        image,
        "validate_image_result",
        lambda value, **_kwargs: copy.deepcopy(value),
    )
    with registry.materialize_oci_layout(archive) as materialized:
        manifest_raw = json.dumps(
            materialized["manifest"], sort_keys=True, separators=(",", ":")
        ).encode()
        config_raw = json.dumps(
            materialized["config"], sort_keys=True, separators=(",", ":")
        ).encode()
        layer_path = next(
            path
            for path in (materialized["layout_dir"] / "blobs" / "sha256").iterdir()
            if path.name == expected["layers"][0]["digest"].split(":", 1)[1]
        )
        layer_raw = layer_path.read_bytes()
    content_marker = tmp_path / "content"
    tag_marker = tmp_path / "tag"
    invocation_log = tmp_path / "crane-invocations.log"
    crane = tmp_path / "crane"
    crane.write_text(
        f"""#!/usr/bin/env python3
from pathlib import Path
import os
import sys
op = sys.argv[1]
ref = sys.argv[-1]
with Path({str(invocation_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write(f"{{bool(os.environ.get('DOCKER_CONFIG'))}}:{{op}}:{{ref}}\\n")
if op == "digest":
    if os.environ.get("DOCKER_CONFIG") and os.environ.get("DOCKER_CONFIG") != "/runner/docker-config":
        print("UNAUTHORIZED: authentication required", file=sys.stderr)
        raise SystemExit(1)
    marker = Path({str(content_marker)!r}) if "@" in ref else Path({str(tag_marker)!r})
    if not marker.exists():
        print("MANIFEST_UNKNOWN", file=sys.stderr)
        raise SystemExit(1)
    print({expected['member_digest']!r})
elif op == "push":
    Path({str(content_marker)!r}).write_text("pushed", encoding="utf-8")
    print(ref)
elif op == "tag":
    Path({str(tag_marker)!r}).write_text("tagged", encoding="utf-8")
elif op == "manifest":
    sys.stdout.buffer.write(bytes.fromhex({manifest_raw.hex()!r}))
elif op == "blob":
    raw = bytes.fromhex({config_raw.hex()!r}) if ref.endswith({expected['config_digest']!r}) else bytes.fromhex({layer_raw.hex()!r})
    sys.stdout.buffer.write(raw)
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    ancestor_preflight = _protected_preflight()
    ancestor_preflight["default_branch_sha"] = "b" * 40
    monkeypatch.setattr(registry.core, "tag_preflight", lambda **_: ancestor_preflight)
    with pytest.raises(ValueError, match="default branch|first publication"):
        registry.publish_member(
            archive,
            image_result=image_result,
            lane="protected-tag",
            **plan_binding,
        )
    assert not any(
        marker in invocation_log.read_text(encoding="utf-8")
        for marker in (":push:", ":tag:")
    )
    invocation_log.unlink()
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    stale = copy.deepcopy(image_result)
    stale["source"]["commit"] = "b" * 40
    with pytest.raises(ValueError, match="source"):
        registry.publish_member(
            archive,
            image_result=stale,
            lane="protected-tag",
            **plan_binding,
        )
    assert not invocation_log.exists()

    forged_identity = copy.deepcopy(image_result)
    forged_identity["content_identity"]["task_sha256"] = "sha256:" + "f" * 64
    identity_payload = {
        key: value
        for key, value in forged_identity["content_identity"].items()
        if key != "content_identity_sha256"
    }
    forged_identity["content_identity"]["content_identity_sha256"] = (
        registry.sha256_value(identity_payload)
    )
    forged_identity["content_identity_sha256"] = forged_identity["content_identity"][
        "content_identity_sha256"
    ]
    with pytest.raises(ValueError, match="content identity|task"):
        registry.publish_member(
            archive,
            image_result=forged_identity,
            lane="protected-tag",
            **plan_binding,
        )
    assert not invocation_log.exists()

    for mutation in ("diff_ids", "created", "history"):
        forged_config_closure = copy.deepcopy(image_result)
        identity = forged_config_closure["content_identity"]
        if mutation == "diff_ids":
            identity["diff_ids"][0] = "sha256:" + "d" * 64
        elif mutation == "created":
            identity["created"] = "2026-08-10T00:00:00Z"
        else:
            identity["history"][-1]["created_by"] = "forged-install-command"
        identity["content_identity_sha256"] = registry.sha256_value(
            {
                key: value
                for key, value in identity.items()
                if key != "content_identity_sha256"
            }
        )
        forged_config_closure["content_identity_sha256"] = identity[
            "content_identity_sha256"
        ]
        with pytest.raises(ValueError, match="content identity|Buildx OCI"):
            registry.publish_member(
                archive,
                image_result=forged_config_closure,
                lane="protected-tag",
                **plan_binding,
            )
        assert not invocation_log.exists()

    record = registry.publish_member(
        archive,
        image_result=image_result,
        lane="protected-tag",
        **plan_binding,
    )

    assert record["member_digest"] == expected["member_digest"]
    assert record["image_result_sha256"] == expected["image_result_sha256"]
    assert record["layers"][0]["annotations"] == {
        "buildkit/rewritten-timestamp": "1786353770"
    }
    assert record["prewrite_visibility_evidence_sha256"].startswith("sha256:")
    assert (
        registry.validate_member_record(
            record,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )
        == record
    )
    assert {item["type"] for item in record["operations"]} >= {
        "registry-member-push-by-digest",
        "registry-staging-tag-create",
        "registry-authenticated-config-blob-read",
        "registry-authenticated-layer-blob-read",
    }
    assert verify.audit_operations(
        record["operations"],
        lane="protected-tag",
        staging_repository=resolved_plan["source"]["staging_repository"],
    ) == {
        "operation_count": 9,
        "operation_types": [
            "registry-anonymous-prewrite-visibility-read",
            "registry-anonymous-visibility-read",
            "registry-authenticated-config-blob-read",
            "registry-authenticated-digest-read",
            "registry-authenticated-layer-blob-read",
            "registry-authenticated-manifest-read",
            "registry-authenticated-staging-prewrite-read",
            "registry-member-push-by-digest",
            "registry-staging-tag-create",
        ],
        "write_capable_operations": record["operations"][2:4],
        "write_count": 2,
    }
    staging_reference = "ghcr.io/release-org/ucm-release-staging:staging-" + expected[
        "build_key_sha256"
    ].removeprefix("sha256:")
    assert invocation_log.read_text(encoding="utf-8").splitlines()[0] == (
        "True:digest:" + staging_reference
    )


def test_member_tag_collision_fails_before_any_digest_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing staging tag with different bytes cannot leave an orphan push."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    resolved_plan = _protected_resolved_plan(monkeypatch)
    selected_task = resolved_plan["image_tasks"][0]
    archive, expected = _valid_oci_archive(
        tmp_path,
        _publication_members_for_plan(resolved_plan)[0],
    )
    plan_binding = {
        "selected_task": selected_task,
        "resolved_plan": resolved_plan,
        "expected_plan_sha256": resolved_plan["resolved_plan_sha256"],
    }
    image_result = {
        "candidate_kind": "real-candidate",
        "unpublished": True,
        "spec_id": expected["spec_id"],
        "profile_id": expected["profile_id"],
        "family_id": expected["family_id"],
        "target_platform": expected["platform"],
        "target_repository": expected["target_repository"],
        "target_tag": expected["target_tag"],
        "task_key": expected["candidate_task_sha256"],
        "build_key_sha256": expected["build_key_sha256"],
        "recipe_sha256": expected["recipe_sha256"],
        "content_identity_sha256": expected["content_identity_sha256"],
        "result_sha256": expected["image_result_sha256"],
        "source": {
            **copy.deepcopy(expected["content_identity"]["source"]),
            "task_sha256": expected["candidate_task_sha256"],
            "wheel_build_key": "sha256:" + "e" * 64,
        },
        "wheel": {"sha256": expected["wheel_sha256"]},
        "oci": {"digest": expected["member_digest"], "published": False},
        "content_identity": copy.deepcopy(expected["content_identity"]),
    }
    monkeypatch.setattr(
        image,
        "validate_image_result",
        lambda value, **_kwargs: copy.deepcopy(value),
    )
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")

    def visibility(
        reference: str,
        *,
        staging_repository: str,
        phase: str = "postwrite",
    ) -> dict[str, object]:
        assert staging_repository == resolved_plan["source"]["staging_repository"]
        operation = {
            "type": (
                "registry-anonymous-prewrite-visibility-read"
                if phase == "prewrite"
                else "registry-anonymous-visibility-read"
            ),
            "capability": "read",
            "reference": reference,
        }
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-private-visibility-evidence",
            "status": "anonymous-denied",
            "phase": phase,
            "returncode": 1,
            "stdout_sha256": "sha256:" + "1" * 64,
            "stderr_sha256": "sha256:" + "2" * 64,
            "operation": operation,
        }
        return {
            **payload,
            "visibility_evidence_sha256": registry.sha256_value(payload),
        }

    monkeypatch.setattr(registry, "verify_private_staging", visibility)
    staging_reference = (
        resolved_plan["source"]["staging_repository"]
        + ":staging-"
        + expected["build_key_sha256"].removeprefix("sha256:")
    )

    def fresh(reference: str, *_args: object, **_kwargs: object) -> str | None:
        if reference == staging_reference:
            return "sha256:" + "f" * 64
        return None

    monkeypatch.setattr(registry, "_fresh_transport_digest", fresh)
    pushed: list[str] = []

    def push(*_args: object, **_kwargs: object) -> dict[str, object]:
        pushed.append("push")
        return {
            "decision": "create",
            "digest": expected["member_digest"],
            "operations": [],
        }

    monkeypatch.setattr(registry, "_push_materialized_member", push)

    with pytest.raises(ValueError, match="tag collision"):
        registry.publish_member(
            archive,
            image_result=image_result,
            lane="protected-tag",
            **plan_binding,
        )
    assert pushed == []


def test_member_prewrite_and_postwrite_visibility_evidence_must_be_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record cannot collapse the two sides of the private staging write gate."""
    registry, _ = _modules()
    resolved_plan = _protected_resolved_plan(monkeypatch)
    record = _publication_members_for_plan(resolved_plan)[0]
    record["visibility_evidence_sha256"] = record["prewrite_visibility_evidence_sha256"]
    record["record_sha256"] = registry.sha256_value(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="visibility"):
        registry.validate_member_record(
            record,
            resolved_plan=resolved_plan,
            expected_plan_sha256=resolved_plan["resolved_plan_sha256"],
        )


def test_materialize_oci_hashes_large_layers_without_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-GB layer verification must stay chunked on a 16GB hosted runner."""
    registry, _ = _modules()
    archive, record = _valid_oci_archive(tmp_path, _publication_members()[0])
    layer_hex = record["layers"][0]["digest"].split(":", 1)[1]
    original = Path.read_bytes

    def guarded(path: Path) -> bytes:
        if path.name == layer_hex:
            raise AssertionError("layer Path.read_bytes is an unbounded allocation")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)

    with registry.materialize_oci_layout(archive) as materialized:
        assert materialized["layers"][0]["digest"] == record["layers"][0]["digest"]


def test_materialize_oci_rejects_duplicate_archive_paths(tmp_path: Path) -> None:
    """A later tar entry cannot replace an already validated canonical path."""
    registry, _ = _modules()
    archive, _ = _valid_oci_archive(tmp_path, _publication_members()[0])
    with tarfile.open(archive, "a") as bundle:
        duplicate = tarfile.TarInfo("oci-layout")
        duplicate.size = len(b'{"imageLayoutVersion":"1.0.0"}')
        bundle.addfile(duplicate, io.BytesIO(b'{"imageLayoutVersion":"1.0.0"}'))

    with pytest.raises(ValueError, match="duplicate"):
        with registry.materialize_oci_layout(archive):
            pass


@pytest.mark.parametrize(
    ("mode", "accepted"),
    [
        ("unauthorized", True),
        ("ghcr-token-denied", True),
        ("ghcr-manifest-denied", True),
        ("network", False),
        ("permission", False),
        ("http403", False),
        ("proxy403", False),
        ("bareauth", False),
        ("missing", False),
        ("public", False),
    ],
)
def test_private_staging_requires_typed_anonymous_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    accepted: bool,
) -> None:
    """Only an explicit auth denial proves private; outages and public reads do not."""
    registry, _ = _modules()
    crane = tmp_path / "crane"
    crane.write_text(
        f"""#!/bin/sh
if [ {mode!r} = unauthorized ]; then
  echo 'UNAUTHORIZED: authentication required' >&2
  exit 1
fi
if [ {mode!r} = ghcr-token-denied ]; then
  echo 'Error: GET https://ghcr.io/token?scope=repository%3Arelease-org%2Fucm-release-staging%3Apull&service=ghcr.io: DENIED: requested access to the resource is denied' >&2
  exit 1
fi
if [ {mode!r} = ghcr-manifest-denied ]; then
  echo 'Error: fetching manifest: GET https://ghcr.io/v2/release-org/ucm-release-staging/manifests/staging-{'2' * 64}: UNAUTHORIZED: authentication required' >&2
  exit 1
fi
if [ {mode!r} = network ]; then
  echo 'dial tcp: network is unreachable' >&2
  exit 1
fi
if [ {mode!r} = permission ]; then
  echo 'dial tcp: connect: permission denied' >&2
  exit 1
fi
if [ {mode!r} = http403 ]; then
  echo 'HTTP/1.1 403 Forbidden' >&2
  exit 1
fi
if [ {mode!r} = proxy403 ]; then
  echo 'proxy CONNECT ghcr.io:443: status code 403' >&2
  exit 1
fi
if [ {mode!r} = bareauth ]; then
  echo 'authentication required' >&2
  exit 1
fi
if [ {mode!r} = missing ]; then
  echo 'MANIFEST_UNKNOWN' >&2
  exit 1
fi
echo 'sha256:{'1' * 64}'
""",
        encoding="utf-8",
    )
    crane.chmod(0o755)
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    reference = "ghcr.io/release-org/ucm-release-staging:staging-" + "2" * 64

    if accepted:
        evidence = registry.verify_private_staging(
            reference, staging_repository=registry.FIXTURE_STAGING_REPOSITORY
        )
        assert evidence["status"] == "anonymous-denied"
        assert evidence["returncode"] == 1
        assert evidence["stdout_sha256"].startswith("sha256:")
        assert evidence["stderr_sha256"].startswith("sha256:")
        assert evidence["operation"] == {
            "type": "registry-anonymous-visibility-read",
            "capability": "read",
            "reference": reference,
        }
        prewrite = registry.verify_private_staging(
            reference,
            staging_repository=registry.FIXTURE_STAGING_REPOSITORY,
            phase="prewrite",
        )
        assert prewrite["operation"]["type"] == (
            "registry-anonymous-prewrite-visibility-read"
        )
    else:
        with pytest.raises(ValueError, match="public|anonymous|network|denial"):
            registry.verify_private_staging(
                reference, staging_repository=registry.FIXTURE_STAGING_REPOSITORY
            )


def test_registry_subprocess_environment_is_minimal_and_keeps_login_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _ = _modules()
    values = {
        "HOME": "/runner/home",
        "DOCKER_CONFIG": "/runner/docker-config",
        "SSL_CERT_FILE": "/etc/certs.pem",
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "NO_PROXY": "127.0.0.1",
        "GITHUB_TOKEN": "must-not-leak",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "must-not-leak",
        "PATH": "/attacker/path",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    environment = registry._minimal_registry_environment()

    assert environment == {
        "HOME": "/runner/home",
        "DOCKER_CONFIG": "/runner/docker-config",
        "SSL_CERT_FILE": "/etc/certs.pem",
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "NO_PROXY": "127.0.0.1",
    }

    crane = tmp_path / "crane"
    crane.write_text("not executed", encoding="utf-8")
    invocation: dict[str, object] = {}

    def run(
        arguments: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        invocation.update({"arguments": arguments, **options})
        return subprocess.CompletedProcess(
            arguments, 0, stdout="sha256:" + "1" * 64 + "\n", stderr=""
        )

    monkeypatch.setattr(registry.subprocess, "run", run)
    reference = "docker.io/vllm/vllm-openai:v0.10.2"
    assert registry._crane(str(crane), "digest", reference) == "sha256:" + "1" * 64
    assert invocation["env"] == environment
    assert invocation["arguments"] == [str(crane), "digest", reference]


def test_buildx_subprocess_environment_is_minimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct Buildx plugin cannot inherit GitHub or Actions credentials."""
    registry, _ = _modules()
    environment_log = tmp_path / "buildx.environment"
    buildx = tmp_path / "docker-buildx"
    buildx.write_text(
        f"#!/bin/sh\n/usr/bin/env > {environment_log}\nprintf '{{}}\\n'\n",
        encoding="utf-8",
    )
    buildx.chmod(0o755)
    values = {
        "HOME": "/runner/home",
        "DOCKER_CONFIG": "/runner/docker-config",
        "SSL_CERT_FILE": "/etc/certs.pem",
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "NO_PROXY": "127.0.0.1",
        "GITHUB_TOKEN": "must-not-leak",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "must-not-leak",
        "PATH": "/attacker/path",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    assert (
        registry._run_imagetools(str(buildx), ["imagetools", "create", "--dry-run"])
        == b"{}\n"
    )
    observed = dict(
        line.split("=", 1)
        for line in environment_log.read_text(encoding="utf-8").splitlines()
    )

    assert observed["HOME"] == "/runner/home"
    assert observed["DOCKER_CONFIG"] == "/runner/docker-config"
    assert observed["SSL_CERT_FILE"] == "/etc/certs.pem"
    assert observed["HTTPS_PROXY"] == "http://proxy.internal:8080"
    assert observed["NO_PROXY"] == "127.0.0.1"
    assert "GITHUB_TOKEN" not in observed
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in observed
    assert "PATH" not in observed


def test_resolve_pinned_buildx_uses_image_toolchain_version_and_platform_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production resolves one fixed plugin path and verifies version plus bytes."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    plugin = tmp_path / ".docker" / "cli-plugins" / "docker-buildx"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(
        "#!/bin/sh\necho 'github.com/docker/buildx v0.19.2 deadbeef'\n",
        encoding="utf-8",
    )
    plugin.chmod(0o755)
    plugin_sha = "sha256:" + hashlib.sha256(plugin.read_bytes()).hexdigest()
    authority = image.real_image_toolchain_authority()
    authority["buildx_linux_sha256"]["amd64"] = plugin_sha
    monkeypatch.setattr(image, "real_image_toolchain_authority", lambda: authority)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")

    assert registry.resolve_pinned_buildx() == str(plugin.resolve())

    execution_marker = tmp_path / "wrong-buildx-executed"
    plugin.write_text(
        (
            "#!/bin/sh\n"
            f"touch {execution_marker}\n"
            "echo 'github.com/docker/buildx v0.19.2 deadbeef'\n"
        ),
        encoding="utf-8",
    )
    wrong = copy.deepcopy(authority)
    wrong["buildx_linux_sha256"]["amd64"] = "sha256:" + "f" * 64
    monkeypatch.setattr(image, "real_image_toolchain_authority", lambda: wrong)
    with pytest.raises(ValueError, match="Buildx binary digest mismatch"):
        registry.resolve_pinned_buildx()
    assert not execution_marker.exists()

    plugin.write_text(
        "#!/bin/sh\necho 'github.com/docker/buildx v0.19.3 deadbeef'\n",
        encoding="utf-8",
    )
    wrong_version = copy.deepcopy(authority)
    wrong_version["buildx_linux_sha256"]["amd64"] = (
        "sha256:" + hashlib.sha256(plugin.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(image, "real_image_toolchain_authority", lambda: wrong_version)
    with pytest.raises(ValueError, match="requires Buildx v0.19.2"):
        registry.resolve_pinned_buildx()


def test_production_index_api_rejects_caller_selected_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither Python callers nor the canonical CLI can replace Buildx."""
    registry, _ = _modules()
    _, _, parent, plan_binding = _resolved_publication_context(monkeypatch)
    marker = tmp_path / "caller-executor-ran"
    attacker = tmp_path / "attacker-docker"
    attacker.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8")
    attacker.chmod(0o755)
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    with pytest.raises(TypeError, match="docker_binary"):
        registry.create_index(
            parent["plans"][0],
            parent_plans=parent,
            lane="protected-tag",
            docker_binary=str(attacker),
            **plan_binding,
        )

    assert not marker.exists()


def test_member_push_requires_crane_full_reference_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crane v0.20.3 push must report the exact full repository@digest target."""
    registry, _ = _modules()
    archive, _ = _valid_oci_archive(tmp_path, _publication_members()[0])
    with registry.materialize_oci_layout(archive) as materialized:
        digest = materialized["manifest_digest"]
        observations = iter([None, digest])
        monkeypatch.setattr(
            registry,
            "_fresh_transport_digest",
            lambda *args, **kwargs: next(observations),
        )
        monkeypatch.setattr(
            registry,
            "_run_registry_tool",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=digest + "\n", stderr=""
            ),
        )

        with pytest.raises(ValueError, match="full reference|stdout|coordinate"):
            registry._push_materialized_member(
                materialized,
                repository="ghcr.io/release-org/ucm-release-staging",
                crane_binary="/pinned/crane",
            )
