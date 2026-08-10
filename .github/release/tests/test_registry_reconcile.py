"""Behavioral contract for read-only registry reconciliation and loop evidence."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import importlib
import io
import json
import os
import shutil
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
    manifest = core.build_release_manifest()
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


def test_registry_scan_rejects_same_product_name_on_an_unreviewed_host() -> None:
    """A matching final path segment cannot widen the exact upstream allowlist."""
    registry, _ = _modules()

    with pytest.raises(ValueError, match="exact upstream repository"):
        registry.scan_registry(
            "evil.example/vllm/vllm-openai",
            "v0.10.2",
            fixture={**_snapshot(), "repository": "evil.example/vllm/vllm-openai"},
        )


def test_snapshot_and_build_identity_bind_every_immutable_input(tmp_path: Path) -> None:
    """Dropping any platform/config/wheel/rule/implementation input changes identity."""
    registry, _ = _modules()
    case = _case(tmp_path)
    baseline = registry.build_candidate(**case, fixture_mode=True)

    assert baseline["target_repository"] == ("ghcr.io/modelengine-group/vllm-ascend")
    assert baseline["tag_base"] == "v0.22.1rc1-a3-ucm-0.5.0rc1"
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


def test_fixture_base_policy_drift_creates_a_new_build_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing only the authorized base identity must invalidate an old build key."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    case = _case(tmp_path)
    original_implementation = image.implementation_digests()
    case["implementation_digest"] = original_implementation["aggregate_sha256"]
    baseline = registry.build_candidate(**case, fixture_mode=True)

    changed_authority = copy.deepcopy(image.FIXTURE_BASE_AUTHORITY)
    changed_authority["manifest_digest"] = "sha256:" + "d" * 64
    monkeypatch.setattr(image, "FIXTURE_BASE_AUTHORITY", changed_authority)
    changed_implementation = image.implementation_digests()
    changed_case = copy.deepcopy(case)
    changed_case["implementation_digest"] = changed_implementation["aggregate_sha256"]
    changed = registry.build_candidate(**changed_case, fixture_mode=True)
    reconciled = registry.reconcile(changed, _inventory([_entry(baseline)]))

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
    baseline = registry.build_candidate(**case, fixture_mode=True)

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
    changed = registry.build_candidate(**changed_case, fixture_mode=True)
    reconciled = registry.reconcile(changed, _inventory([_entry(baseline)]))

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
        registry.build_candidate(
            **{**case, "wheel_records": [builder_record]}, fixture_mode=True
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


def _publication_members() -> list[dict[str, object]]:
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    candidate = core.build_matrix("feature-candidate")
    protected = core.build_matrix("protected-tag")
    protected_by_spec = {item["spec_id"]: item for item in protected["tasks"]}
    members: list[dict[str, object]] = []
    for index, task in enumerate(candidate["tasks"], start=1):
        digest = f"sha256:{index:064x}"
        config_digest = f"sha256:{index + 10:064x}"
        build_key = f"sha256:{index + 20:064x}"
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-member-publication",
            "status": "passed",
            "spec_id": task["spec_id"],
            "profile_id": task["profile_id"],
            "family_id": task["profile_id"],
            "platform": task["platform"],
            "target_repository": task["target_repository"],
            "target_tag": task["target_tag"],
            "staging_repository": "ghcr.io/supermarioyl/ucm-release-staging",
            "staging_visibility": "private",
            "staging_tag": f"staging-{build_key.removeprefix('sha256:')}",
            "candidate_task_sha256": task["task_sha256"],
            "publication_task_sha256": protected_by_spec[task["spec_id"]][
                "task_sha256"
            ],
            "build_key_sha256": build_key,
            "wheel_sha256": f"sha256:{index + 30:064x}",
            "member_digest": digest,
            "member_size": 1000 + index,
            "config_digest": config_digest,
            "annotations": {
                "io.ucm.release.build-key-sha256": build_key,
                "io.ucm.release.candidate-task-sha256": task["task_sha256"],
                "io.ucm.release.family-id": task["profile_id"],
                "io.ucm.release.platform": task["platform"],
                "io.ucm.release.spec-id": task["spec_id"],
                "io.ucm.release.wheel-sha256": f"sha256:{index + 30:064x}",
            },
        }
        payload["record_sha256"] = core.sha256_value(payload)
        members.append(payload)
    return members


def _protected_preflight() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ucm-tag-preflight",
        "lane": "protected-tag",
        "source_sha": "a" * 40,
        "publication_allowed": True,
        "write_authority": [
            "github-prerelease",
            "ghcr-final-index",
            "ghcr-private-staging",
        ],
    }


def test_canonical_registry_contract_has_six_members_and_three_exact_r1_targets() -> (
    None
):
    """Production coordinates come from the reviewed matrices, never legacy constants."""
    registry, _ = _modules()

    contract = registry.canonical_registry_contract()

    assert contract["staging_repository"] == (
        "ghcr.io/supermarioyl/ucm-release-staging"
    )
    assert [item["spec_id"] for item in contract["members"]] == [
        "cuda130-amd64",
        "cuda130-arm64",
        "cann900-a2-amd64",
        "cann900-a2-arm64",
        "cann900-a3-amd64",
        "cann900-a3-arm64",
    ]
    assert [item["platform"] for item in contract["members"]] == [
        "linux/amd64",
        "linux/arm64",
        "linux/amd64",
        "linux/arm64",
        "linux/amd64",
        "linux/arm64",
    ]
    assert [
        (item["target_repository"], item["target_tag"]) for item in contract["indexes"]
    ] == [
        (
            "ghcr.io/supermarioyl/vllm-ascend",
            "v0.22.1rc1-a3-ucm-0.5.0rc1-r1",
        ),
        (
            "ghcr.io/supermarioyl/vllm-ascend",
            "v0.22.1rc1-ucm-0.5.0rc1-r1",
        ),
        (
            "ghcr.io/supermarioyl/vllm-openai",
            "v0.21.0-ucm-0.5.0rc1-r1",
        ),
    ]
    assert all(
        item["candidate_task_sha256"] != item["publication_task_sha256"]
        for item in contract["members"]
    )


def test_staging_tag_and_exact_r1_reconciliation_are_collision_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent creates, identity reuses, and same-name drift cannot retag or roll r2."""
    registry, _ = _modules()
    members = _publication_members()
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    build_key = members[0]["build_key_sha256"]
    digest = members[0]["member_digest"]
    assert registry.plan_staging_tag(build_key, digest, None)["decision"] == "create"
    assert registry.plan_staging_tag(build_key, digest, digest)["decision"] == "reuse"
    with pytest.raises(ValueError, match="staging tag collision"):
        registry.plan_staging_tag(build_key, digest, DIGESTS["observed_drift"])
    public_staging = copy.deepcopy(members[0])
    public_staging["staging_visibility"] = "public"
    public_staging["record_sha256"] = registry.sha256_value(
        {key: value for key, value in public_staging.items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="private staging"):
        registry.validate_member_record(public_staging)

    absent = registry.plan_indexes(members, inventory=[])
    same_inventory = [
        {
            "repository": item["target_repository"],
            "tag": item["target_tag"],
            "digest": plan["expected_index_digest"],
            "build_key_sha256": plan["index_build_key_sha256"],
        }
        for item, plan in zip(absent["plans"], absent["plans"], strict=True)
    ]
    reused = registry.plan_indexes(members, inventory=same_inventory)
    assert [item["decision"] for item in absent["plans"]] == [
        "create",
        "create",
        "create",
    ]
    assert [item["decision"] for item in reused["plans"]] == [
        "reuse",
        "reuse",
        "reuse",
    ]
    assert [item["members"][0]["platform"] for item in absent["plans"]] == [
        "linux/amd64",
        "linux/amd64",
        "linux/amd64",
    ]
    assert [item["members"][1]["platform"] for item in absent["plans"]] == [
        "linux/arm64",
        "linux/arm64",
        "linux/arm64",
    ]
    conflict = copy.deepcopy(same_inventory)
    conflict[0]["digest"] = DIGESTS["observed_drift"]
    with pytest.raises(ValueError, match="r1 conflict"):
        registry.plan_indexes(members, inventory=conflict)
    with pytest.raises(ValueError, match="feature-candidate.*write-capable"):
        registry.plan_indexes(members, inventory=[], lane="feature-candidate")


@pytest.mark.parametrize("status", ["failed", "cancelled", "skipped", "missing"])
def test_exact_six_barrier_emits_zero_index_writes_for_any_unsuccessful_member(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """No partial family can open any of the three final-index write gates."""
    registry, _ = _modules()
    members = _publication_members()
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    statuses = {item["spec_id"]: "success" for item in members}
    statuses[members[2]["spec_id"]] = status

    with pytest.raises(ValueError, match="six-member barrier") as blocked:
        registry.plan_indexes(members, inventory=[], member_statuses=statuses)

    assert getattr(blocked.value, "operations", []) == []


def test_feature_and_protected_operation_audits_are_typed_and_allowlisted() -> None:
    """Feature rejects every write type; protected accepts only exact coordinates."""
    _, verify = _modules()
    staging = "ghcr.io/supermarioyl/ucm-release-staging"
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
            "reference": (
                "ghcr.io/supermarioyl/vllm-openai:" "v0.21.0-ucm-0.5.0rc1-r1"
            ),
        },
    ]

    with pytest.raises(ValueError, match="feature-candidate.*write"):
        verify.audit_operations(protected_operations, lane="feature-candidate")
    protected = verify.audit_operations(protected_operations, lane="protected-tag")
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
        verify.audit_operations(bad, lane="protected-tag")


def test_registry_cli_commands_use_canonical_json_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every Task 4 command has deterministic file input and file output."""
    registry, _ = _modules()
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    crane, transport_log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(transport_log))
    members = _publication_members()
    requests = {
        "inventory": {"crane": str(crane)},
        "verify-member": {"member": members[0]},
        "plan-index": {
            "lane": "protected-tag",
            "members": members,
            "inventory": [],
        },
        "verify-index": {
            "plan": registry.plan_indexes(members, inventory=[])["plans"][0]
        },
        "audit-operations": {"lane": "feature-candidate", "operations": []},
    }
    for action, request in requests.items():
        input_path = tmp_path / f"{action}.input.json"
        output_path = tmp_path / f"{action}.output.json"
        input_path.write_bytes(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        result = _cli(
            "registry",
            action,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        )
        assert output_path.read_text(encoding="utf-8") == result.stdout
        assert json.loads(result.stdout)["kind"].startswith("ucm-registry-")


def test_release_manifest_schema_accepts_optional_registry_publication() -> None:
    """Publication evidence is separate from still-unpublished image results."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
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
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

operation = sys.argv[1]
reference = sys.argv[2]
with Path(os.environ["UCM_TRANSPORT_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "docker_config": os.environ.get("DOCKER_CONFIG")}, sort_keys=True) + "\\n")
if os.environ.get("UCM_EXPECT_EMPTY_AUTH") == "1":
    config = Path(os.environ["DOCKER_CONFIG"]) / "config.json"
    if config.read_bytes() != b'{"auths":{}}\\n':
        raise SystemExit(78)
    if os.environ["DOCKER_CONFIG"] == os.environ.get("UCM_CALLER_DOCKER_CONFIG"):
        raise SystemExit(79)
if operation == "digest":
    if reference.endswith("v0.21.0-ucm-0.5.0rc1-r1") or "staging-" in reference:
        print("sha256:" + "9" * 64)
    else:
        print("MANIFEST_UNKNOWN", file=sys.stderr)
        raise SystemExit(1)
elif operation == "manifest":
    print(json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "annotations": {"io.ucm.release.index-build-key-sha256": "sha256:" + "8" * 64},
        "manifests": []
    }, sort_keys=True, separators=(",", ":")))
elif operation == "push":
    print(sys.argv[3].rsplit("@", 1)[-1])
elif operation == "tag":
    print("tagged")
else:
    raise SystemExit(77)
""",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool, log


def test_inventory_and_anonymous_readback_use_real_subprocess_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anonymous reads get a fresh empty auth config and inventory records absence."""
    registry, verify = _modules()
    crane, log = _fake_registry_tool(tmp_path)
    caller_config = tmp_path / "caller-docker-config"
    caller_config.mkdir()
    (caller_config / "config.json").write_text(
        '{"auths":{"secret.example":{"auth":"do-not-use"}}}\n', encoding="utf-8"
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(caller_config))
    monkeypatch.setenv("UCM_CALLER_DOCKER_CONFIG", str(caller_config))
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))

    inventory = registry.inventory_registry(crane_binary=str(crane))
    assert [(item["repository"], item["tag"]) for item in inventory["entries"]] == [
        (
            "ghcr.io/supermarioyl/vllm-openai",
            "v0.21.0-ucm-0.5.0rc1-r1",
        )
    ]
    assert len(inventory["absent"]) == 2
    assert verify.audit_operations(inventory["operations"])["write_count"] == 0

    monkeypatch.setenv("UCM_EXPECT_EMPTY_AUTH", "1")
    readback = registry.readback_reference(
        "ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1",
        crane_binary=str(crane),
        anonymous=True,
    )
    assert readback["digest"] == "sha256:" + "9" * 64
    assert readback["authenticated"] is False
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    anonymous_configs = [
        item["docker_config"]
        for item in events[-2:]
        if item["docker_config"] != str(caller_config)
    ]
    assert len(set(anonymous_configs)) == 1
    assert not Path(anonymous_configs[0]).exists()


@pytest.mark.parametrize("lane", ["feature-candidate", "manual", "head-marker"])
def test_write_transport_fails_closed_before_subprocess_for_nonprotected_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    """A caller label cannot reach push/tag/index transport outside protected Tag."""
    registry, _ = _modules()
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    archive = tmp_path / "member.tar"
    archive.write_bytes(b"tiny scratch archive")

    with pytest.raises(ValueError, match="protected-tag"):
        registry.push_member_by_digest(
            archive,
            _publication_members()[0],
            lane=lane,
            crane_binary=str(crane),
        )

    assert not log.exists()


def test_protected_write_transport_rechecks_authority_and_exact_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write reloads protected preflight/matrix and returns a typed ledger."""
    registry, verify = _modules()
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    calls = {"preflight": 0, "matrix": 0}
    real_matrix = registry.core.build_matrix

    def preflight(**_: object) -> dict[str, object]:
        calls["preflight"] += 1
        return _protected_preflight()

    def matrix(lane: str) -> dict[str, object]:
        calls["matrix"] += 1
        return real_matrix(lane)

    monkeypatch.setattr(registry.core, "tag_preflight", preflight)
    monkeypatch.setattr(registry.core, "build_matrix", matrix)
    archive = tmp_path / "member.tar"
    archive.write_bytes(b"tiny scratch archive")
    result = registry.push_member_by_digest(
        archive,
        _publication_members()[0],
        lane="protected-tag",
        crane_binary=str(crane),
    )

    assert calls["preflight"] == 1
    assert calls["matrix"] >= 1
    assert result["digest"] == _publication_members()[0]["member_digest"]
    assert (
        verify.audit_operations(result["operations"], lane="protected-tag")[
            "write_count"
        ]
        == 1
    )
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


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
    assert result["negative_mutation"] == "blocked"
    assert all("ghcr.io" not in item["reference"] for item in result["operations"])
