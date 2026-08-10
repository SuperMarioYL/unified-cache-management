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
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))

    live_scan = registry.scan_registry(
        "docker.io/vllm/vllm-openai",
        "v0.10.2",
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
                "https://github.com/SuperMarioYL/unified-cache-management"
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
        member_reference = "ghcr.io/supermarioyl/ucm-release-staging@" + digest
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
                    "ghcr.io/supermarioyl/ucm-release-staging@" + config_digest
                ),
            },
            {
                "type": "registry-authenticated-layer-blob-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/supermarioyl/ucm-release-staging@" + layer_digest
                ),
            },
        ]
        readback_payload = {
            "schema_version": 1,
            "kind": "ucm-registry-readback",
            "reference": "ghcr.io/supermarioyl/ucm-release-staging@" + digest,
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
            "wheel_sha256": wheel_digest,
            "member_digest": digest,
            "member_size": 1000 + index,
            "config_digest": config_digest,
            "annotations": {
                "io.ucm.release.build-key-sha256": build_key,
                "io.ucm.release.candidate-task-sha256": task["task_sha256"],
                "io.ucm.release.family-id": task["profile_id"],
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
                        "ghcr.io/supermarioyl/ucm-release-staging:"
                        f"staging-{build_key.removeprefix('sha256:')}"
                    ),
                },
                {
                    "type": "registry-authenticated-staging-prewrite-read",
                    "capability": "read",
                    "reference": (
                        "ghcr.io/supermarioyl/ucm-release-staging:"
                        f"staging-{build_key.removeprefix('sha256:')}"
                    ),
                },
                *readback_operations,
                {
                    "type": "registry-anonymous-visibility-read",
                    "capability": "read",
                    "reference": (
                        "ghcr.io/supermarioyl/ucm-release-staging:"
                        f"staging-{build_key.removeprefix('sha256:')}"
                    ),
                },
            ],
        }
        payload["record_sha256"] = core.sha256_value(payload)
        members.append(payload)
    return members


def test_protected_member_workflow_executes_postpublish_record_audit(
    tmp_path: Path,
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
    audit_command = next(
        line.strip()
        for line in record_step["run"].splitlines()
        if "registry.validate_member_record" in line
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    member = _publication_members()[0]
    (out_dir / "member-record.json").write_text(
        json.dumps(member, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
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
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(
        (out_dir / "member-audit-request.json").read_text(encoding="utf-8")
    ) == {"lane": "protected-tag", "operations": member["operations"]}


def test_protected_index_workflow_embeds_reopened_parent_plan(
    tmp_path: Path,
) -> None:
    """The hosted index request must carry the validated parent object, not its path."""
    registry, _ = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={member["spec_id"]: "success" for member in members},
        lane="protected-tag",
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
        if 'open("out/request.json"' in line
    )

    parent_path = tmp_path / "input" / "parent" / "parent-plans.json"
    parent_path.parent.mkdir(parents=True)
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    (tmp_path / "out").mkdir()
    release_link = tmp_path / ".github" / "release"
    release_link.parent.mkdir(parents=True)
    release_link.symlink_to(RELEASE_ROOT, target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(PYTHON)
    family_id = parent["plans"][0]["family_id"]

    result = subprocess.run(
        ["bash", "-c", request_command],
        cwd=tmp_path,
        env={
            **os.environ,
            "FAMILY_ID": family_id,
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
        "family_id": family_id,
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
) -> dict[str, object]:
    digest = plan["expected_index_digest"]
    target = plan["target_repository"] + ":" + plan["target_tag"]
    operations = (
        [{"type": "registry-index-create", "capability": "write", "reference": target}]
        if plan["decision"] == "create"
        else []
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
        "matrix_sha256": registry.core.build_matrix("protected-tag")["matrix_sha256"],
        "verification_sha256": registry.verify_index(plan, parent_plans=parent)[
            "verification_sha256"
        ],
        "parent_plans_sha256": parent["plans_sha256"],
    }
    return {**payload, "provisional_sha256": registry.sha256_value(payload)}


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

    statuses = {item["spec_id"]: "success" for item in members}
    absent = registry.plan_indexes(members, inventory=[], member_statuses=statuses)
    same_inventory = [
        {
            "repository": item["target_repository"],
            "tag": item["target_tag"],
            "digest": plan["expected_index_digest"],
            "build_key_sha256": plan["index_build_key_sha256"],
        }
        for item, plan in zip(absent["plans"], absent["plans"], strict=True)
    ]
    reused = registry.plan_indexes(
        members, inventory=same_inventory, member_statuses=statuses
    )
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
    assert all(
        item["index_manifest"]["annotations"]["org.opencontainers.image.source"]
        == "https://github.com/SuperMarioYL/unified-cache-management"
        for item in absent["plans"]
    )
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
    conflict[0]["build_key_sha256"] = DIGESTS["observed_drift"]
    with pytest.raises(ValueError, match="r1 conflict"):
        registry.plan_indexes(members, inventory=conflict, member_statuses=statuses)
    with pytest.raises(ValueError, match="feature-candidate.*write-capable"):
        registry.plan_indexes(
            members,
            inventory=[],
            member_statuses=statuses,
            lane="feature-candidate",
        )


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


def test_extended_registry_read_operations_audit_exact_roles() -> None:
    """Config/layer/visibility evidence has exact typed reference contracts."""
    _, verify = _modules()
    staging = "ghcr.io/supermarioyl/ucm-release-staging"
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

    assert verify.audit_operations(operations, lane="protected-tag") == {
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
    ] = "ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1"
    invalid.append(wrong_visibility_reference)
    duplicate = copy.deepcopy(operations)
    duplicate.append(copy.deepcopy(duplicate[0]))
    invalid.append(duplicate)
    for ledger in invalid:
        with pytest.raises(ValueError):
            verify.audit_operations(ledger, lane="protected-tag")


def test_registry_cli_commands_use_canonical_json_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """File commands split authenticated prepare from anonymous finalization."""
    registry, _ = _modules()
    cli = importlib.import_module("ucm_release.cli")
    members = _publication_members()
    statuses = {item["spec_id"]: "success" for item in members}
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses=statuses,
        lane="protected-tag",
    )
    image_result_path = tmp_path / "image-result.json"
    image_result_path.write_text('{"kind":"test-image-result"}\n', encoding="utf-8")
    oci_archive = tmp_path / "member.oci.tar"
    oci_archive.write_bytes(b"test transport boundary")
    inventory_result = {
        "schema_version": 1,
        "kind": "ucm-registry-inventory",
        "entries": [],
        "absent": [
            {
                "repository": item["target_repository"],
                "tag": item["target_tag"],
            }
            for item in registry.canonical_registry_contract()["indexes"]
        ],
        "operations": [],
        "inventory_sha256": "sha256:" + "1" * 64,
    }
    monkeypatch.setattr(
        registry, "inventory_registry", lambda: copy.deepcopy(inventory_result)
    )
    monkeypatch.setattr(
        registry,
        "publish_member",
        lambda archive, *, image_result, lane: {
            "schema_version": 1,
            "kind": "ucm-registry-member-publication",
            "status": "passed",
            "record_sha256": "sha256:" + "2" * 64,
        },
    )
    monkeypatch.setattr(
        registry,
        "create_index",
        lambda plan, *, parent_plans, lane: {
            "schema_version": 1,
            "kind": "ucm-registry-index-publication",
            "status": "passed",
            "source_sha": "a" * 40,
            "record_sha256": "sha256:" + "3" * 64,
        },
    )
    monkeypatch.setattr(
        registry,
        "prepare_index",
        lambda plan, *, parent_plans, lane: {
            "schema_version": 1,
            "kind": "ucm-registry-index-provisional",
            "status": "authenticated-passed",
            "provisional_sha256": "sha256:" + "4" * 64,
        },
    )
    monkeypatch.setattr(
        registry,
        "finalize_index",
        lambda provisional, *, parent_plans: {
            "schema_version": 1,
            "kind": "ucm-registry-index-finalization",
            "status": "anonymous-passed",
            "finalization_sha256": "sha256:" + "5" * 64,
        },
    )
    parent_path = tmp_path / "parent-plans.json"
    provisional_path = tmp_path / "index-provisional.json"
    parent_path.write_bytes(registry.canonical_bytes(parent) + b"\n")
    provisional_path.write_text(
        '{"kind":"ucm-registry-index-provisional"}\n', encoding="utf-8"
    )
    requests = {
        "inventory": {},
        "verify-member": {
            "lane": "protected-tag",
            "image_result": str(image_result_path),
            "oci_archive": str(oci_archive),
        },
        "plan-index": {
            "lane": "protected-tag",
            "members": members,
            "member_statuses": statuses,
        },
        "verify-index": {
            "lane": "protected-tag",
            "parent_plans": parent,
            "family_id": parent["plans"][0]["family_id"],
        },
        "prepare-index": {
            "lane": "protected-tag",
            "parent_plans": parent,
            "family_id": parent["plans"][0]["family_id"],
        },
        "finalize-index": {
            "parent_plans": str(parent_path),
            "provisional": str(provisional_path),
        },
        "audit-operations": {"lane": "feature-candidate", "operations": []},
    }
    for action, request in requests.items():
        input_path = tmp_path / f"{action}.input.json"
        output_path = tmp_path / f"{action}.output.json"
        input_path.write_bytes(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        assert "crane" not in request and "docker" not in request
        assert (
            cli.main(
                [
                    "registry",
                    action,
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                ]
            )
            == 0
        )
        stdout = capsys.readouterr().out
        assert output_path.read_text(encoding="utf-8") == stdout
        assert json.loads(stdout)["kind"].startswith("ucm-registry-")

    bad_input = tmp_path / "bad-inventory.json"
    bad_output = tmp_path / "bad-output.json"
    bad_input.write_text('{"crane":"/tmp/attacker"}\n', encoding="utf-8")
    with pytest.raises(SystemExit) as rejected:
        cli.main(
            [
                "registry",
                "inventory",
                "--input",
                str(bad_input),
                "--output",
                str(bad_output),
            ]
        )
    assert rejected.value.code == 2


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
        "ghcr.io/supermarioyl/ucm-release-staging@" + updated["member_digest"]
    )
    updated["operations"] = [
        {
            "type": "registry-anonymous-prewrite-visibility-read",
            "capability": "read",
            "reference": (
                "ghcr.io/supermarioyl/ucm-release-staging:" + updated["staging_tag"]
            ),
        },
        {
            "type": "registry-authenticated-staging-prewrite-read",
            "capability": "read",
            "reference": (
                "ghcr.io/supermarioyl/ucm-release-staging:" + updated["staging_tag"]
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
                "ghcr.io/supermarioyl/ucm-release-staging@" + updated["config_digest"]
            ),
        },
        {
            "type": "registry-authenticated-layer-blob-read",
            "capability": "read",
            "reference": (
                "ghcr.io/supermarioyl/ucm-release-staging@"
                + updated["layers"][0]["digest"]
            ),
        },
        {
            "type": "registry-anonymous-visibility-read",
            "capability": "read",
            "reference": (
                "ghcr.io/supermarioyl/ucm-release-staging:" + updated["staging_tag"]
            ),
        },
    ]
    updated["record_sha256"] = registry.sha256_value(
        {key: value for key, value in updated.items() if key != "record_sha256"}
    )
    return archive, updated


def _member_with_buildkit_rewritten_timestamp() -> dict[str, object]:
    """Project the descriptor annotation emitted by BuildKit v0.19.2."""
    registry, _ = _modules()
    record = copy.deepcopy(_publication_members()[0])
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
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))

    inventory = registry.inventory_registry()
    assert [(item["repository"], item["tag"]) for item in inventory["entries"]] == [
        (
            "ghcr.io/supermarioyl/vllm-openai",
            "v0.21.0-ucm-0.5.0rc1-r1",
        )
    ]
    assert len(inventory["absent"]) == 2
    assert verify.audit_operations(inventory["operations"])["write_count"] == 0

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
        "ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1",
        anonymous=True,
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
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    archive = tmp_path / "member.tar"
    archive.write_bytes(b"rejected before this input is opened")

    with pytest.raises(ValueError, match="protected-tag"):
        registry.push_member_by_digest(
            archive,
            _publication_members()[0],
            lane=lane,
        )
    with pytest.raises(ValueError, match="protected-tag"):
        registry.apply_staging_tag(_publication_members()[0], lane=lane)
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
    with pytest.raises(ValueError, match="protected-tag"):
        registry.create_index(
            parent["plans"][0],
            parent_plans=parent,
            lane=lane,
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
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    archive, member = _valid_oci_archive(tmp_path, _publication_members()[0])
    result = registry.push_member_by_digest(
        archive,
        member,
        lane="protected-tag",
    )

    assert calls["preflight"] == 1
    assert calls["matrix"] >= 1
    assert result["digest"] == member["member_digest"]
    assert (
        verify.audit_operations(result["operations"], lane="protected-tag")[
            "write_count"
        ]
        == 1
    )
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [item["args"][0] for item in events] == ["digest", "push", "digest"]
    assert Path(events[1]["args"][1]).name.startswith("ucm-oci-layout-")


def test_buildkit_rewritten_timestamp_annotation_survives_member_push_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact BuildKit timestamp descriptor remains byte-bound through push."""
    registry, _ = _modules()
    crane, log = _fake_registry_tool(tmp_path)
    monkeypatch.setenv("UCM_TRANSPORT_LOG", str(log))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    archive, member = _valid_oci_archive(
        tmp_path, _member_with_buildkit_rewritten_timestamp()
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
    archive, member = _valid_oci_archive(
        tmp_path, _member_with_buildkit_rewritten_timestamp()
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

    reference = registry.STAGING_REPOSITORY + "@" + member["member_digest"]
    readback = registry.readback_reference(reference)
    member["readback_sha256"] = readback["readback_sha256"]
    _rehash_publication_record(member)

    assert readback["layers"][0]["annotations"] == {
        "buildkit/rewritten-timestamp": "1786353770"
    }
    assert registry.verify_member_readback(member, readback) == member


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
    mutation: str, message: str
) -> None:
    """Only the one exact decimal annotation matching config.created is accepted."""
    registry, _ = _modules()
    member = _member_with_buildkit_rewritten_timestamp()
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
        registry.validate_member_record(member)


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
    field: str, value: str
) -> None:
    """A self-consistent caller rewrite cannot bypass the exact-six parent barrier."""
    registry, _ = _modules()
    parent = registry.plan_indexes(
        _publication_members(),
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in _publication_members()},
        lane="protected-tag",
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
        registry.verify_index(forged, parent_plans=forged_parent)


def test_index_create_rejects_forged_parent_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a rehashed forged parent cannot reach the Docker subprocess boundary."""
    registry, _ = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
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
    record = _publication_members()[0]
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
        )

    assert not marker.exists()


def test_staging_tag_reports_observed_state_collision_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence must disclose repository serialization and unavailable global CAS."""
    registry, _ = _modules()
    record = _publication_members()[0]
    crane, marker = _fresh_drift_crane(tmp_path, record["member_digest"])
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: str(crane))
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )

    result = registry.apply_staging_tag(record, lane="protected-tag")

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
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
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
        )

    assert not docker_marker.exists()
    assert not registry_marker.exists()


def test_real_buildx_oci_layout_is_materialized_as_a_crane_directory(
    tmp_path: Path,
) -> None:
    """Production accepts Buildx OCI output without pretending it is Docker-save."""
    registry, _ = _modules()
    image = importlib.import_module("ucm_release.image")
    buildkit_image = image.real_image_authorities()[0]["toolchain"]["buildkit_image"]
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


def _expanded_member_and_readback() -> tuple[dict[str, object], dict[str, object]]:
    registry, _ = _modules()
    base = _publication_members()[0]
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
        "ghcr.io/supermarioyl/ucm-release-staging@" + base["member_digest"]
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
                "ghcr.io/supermarioyl/ucm-release-staging@" + base["config_digest"]
            ),
        },
        {
            "type": "registry-authenticated-layer-blob-read",
            "capability": "read",
            "reference": "ghcr.io/supermarioyl/ucm-release-staging@" + layer["digest"],
        },
    ]
    readback_payload = {
        "schema_version": 1,
        "kind": "ucm-registry-readback",
        "reference": (
            "ghcr.io/supermarioyl/ucm-release-staging@" + base["member_digest"]
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
                    "ghcr.io/supermarioyl/ucm-release-staging:" + base["staging_tag"]
                ),
            },
            {
                "type": "registry-authenticated-staging-prewrite-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/supermarioyl/ucm-release-staging:" + base["staging_tag"]
                ),
            },
            *copy.deepcopy(readback["operations"]),
            {
                "type": "registry-anonymous-visibility-read",
                "capability": "read",
                "reference": (
                    "ghcr.io/supermarioyl/ucm-release-staging:" + base["staging_tag"]
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
    mutation: str,
) -> None:
    """Config, layer, annotations, and build key all remain byte-bound."""
    registry, _ = _modules()
    member, readback = _expanded_member_and_readback()
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
        registry.verify_member_readback(member, mutated)


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
        "ghcr.io/supermarioyl/ucm-release-staging@" + manifest_digest
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
    reference = registry.STAGING_REPOSITORY + "@sha256:" + "b" * 64

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
            repository=registry.STAGING_REPOSITORY,
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
    reference = registry.STAGING_REPOSITORY + ":staging-" + "c" * 64

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
                repository=registry.STAGING_REPOSITORY,
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
            [operation, str(tmp_path / "source"), registry.STAGING_REPOSITORY],
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
    reference = registry.STAGING_REPOSITORY + ":staging-" + "d" * 64

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
            repository=registry.STAGING_REPOSITORY,
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
    indexes = []
    for position, authority in enumerate(
        registry.canonical_registry_contract()["indexes"], start=1
    ):
        payload = {
            "schema_version": 1,
            "kind": "ucm-registry-index-publication",
            "status": "passed",
            "source_sha": "a" * 40,
            "family_id": authority["family_id"],
            "target_repository": authority["target_repository"],
            "target_tag": authority["target_tag"],
            "index_build_key_sha256": f"sha256:{position + 80:064x}",
            "index_digest": f"sha256:{position + 90:064x}",
            "manifest_sha256": f"sha256:{position + 90:064x}",
            "member_digests": [
                item["member_digest"]
                for item in members
                if item["family_id"] == authority["family_id"]
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
    ("record_kind", "count"),
    [
        ("member_records", 0),
        ("member_records", 5),
        ("member_records", 7),
        ("index_records", 0),
        ("index_records", 2),
        ("index_records", 4),
    ],
)
def test_published_registry_schema_requires_exact_six_members_and_three_indexes(
    record_kind: str, count: int
) -> None:
    """Published evidence cannot be empty, partial, duplicated, or oversized."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    originals = evidence[record_kind]
    evidence[record_kind] = [
        copy.deepcopy(originals[index % len(originals)]) for index in range(count)
    ]
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


def test_published_registry_schema_rejects_arbitrary_record_items() -> None:
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    evidence["member_records"][0] = {}
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


@pytest.mark.parametrize("record_kind", ["member_records", "index_records"])
def test_published_registry_schema_rejects_exact_count_arbitrary_identities(
    record_kind: str,
) -> None:
    """Exact 6/3 cardinality cannot legitimize attacker-controlled identities."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    if record_kind == "member_records":
        for position, record in enumerate(evidence[record_kind]):
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
    else:
        for position, record in enumerate(evidence[record_kind]):
            record.update(
                {
                    "family_id": f"attacker-family-{position}",
                    "target_repository": "evil.invalid/attacker/repo",
                    "target_tag": "latest",
                    "operations": [],
                }
            )
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
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
        ("target_repository", "ghcr.io/supermarioyl/vllm-ascend"),
        ("target_tag", "v0.22.1rc1-ucm-0.5.0rc1-r1"),
    ],
)
def test_published_registry_schema_rejects_member_identity_mismatch(
    field: str, wrong_canonical_value: str
) -> None:
    """Allowed identity strings cannot be recombined into a forged member."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    evidence["member_records"][0][field] = wrong_canonical_value
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
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
def test_published_registry_schema_rejects_nested_member_identity_mismatch(
    annotation: str, wrong_canonical_value: str
) -> None:
    """Nested allowed values must still agree with the member's canonical slot."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    evidence["member_records"][0]["annotations"][annotation] = wrong_canonical_value
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
        core.validate_schema(
            manifest,
            core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"),
        )


@pytest.mark.parametrize(
    ("record_kind", "count"), [("member_records", 6), ("index_records", 3)]
)
def test_published_registry_schema_rejects_unique_objects_with_duplicate_identity(
    record_kind: str, count: int
) -> None:
    """Changing hashes cannot bypass uniqueness of canonical member/family identity."""
    sys.path.insert(0, str(RELEASE_ROOT))
    core = importlib.import_module("ucm_release.core")
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    original = evidence[record_kind][0]
    duplicates = []
    for position in range(count):
        duplicate = copy.deepcopy(original)
        duplicate["record_sha256"] = f"sha256:{position + 201:064x}"
        duplicates.append(duplicate)
    evidence[record_kind] = duplicates
    manifest["publication"]["registry"] = evidence

    with pytest.raises(ValueError):
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
    manifest = core.build_release_manifest()
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
    manifest = core.build_release_manifest()
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
    manifest = core.build_release_manifest()
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
    mutation: str,
) -> None:
    """Canonical member reopening applies strict Python scalar types too."""
    registry, _ = _modules()
    record = _publication_members()[0]
    if mutation == "schema-version-bool":
        record["schema_version"] = True
    else:
        record["collision_model"]["exact_postwrite_readback"] = 1
    _rehash_publication_record(record)

    with pytest.raises(ValueError, match="schema|collision|boolean"):
        registry.validate_member_record(record)


def test_member_record_binds_full_real_content_identity_and_source_labels() -> None:
    """Inherited labels survive, while every canonical release label is authoritative."""
    registry, _ = _modules()
    record = _publication_members()[0]

    assert record["config"]["labels"]["base.label"] == "preserved-1"
    assert record["config"]["labels"] == record["content_identity"]["labels"]
    assert registry.validate_member_record(record) == record

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
            registry.validate_member_record(forged)

    inherited_drift = copy.deepcopy(record)
    inherited_drift["config"]["labels"]["base.label"] = "changed-after-build"
    _rehash_publication_record(inherited_drift)
    with pytest.raises(ValueError, match="content|label|identity"):
        registry.validate_member_record(inherited_drift)


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
    manifest = core.build_release_manifest()
    evidence = _published_registry_evidence()
    record = evidence[record_kind][0]
    staging_digest = "ghcr.io/supermarioyl/ucm-release-staging@" + "sha256:" + "1" * 64
    public_target = "ghcr.io/supermarioyl/vllm-ascend:" "v0.22.1rc1-a3-ucm-0.5.0rc1-r1"
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
    mutation: str,
) -> None:
    """Self-hashed member ledgers cannot smuggle index/public/duplicate operations."""
    registry, _ = _modules()
    record = _publication_members()[0]
    public_target = "ghcr.io/supermarioyl/vllm-ascend:" "v0.22.1rc1-a3-ucm-0.5.0rc1-r1"
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
            "ghcr.io/supermarioyl/ucm-release-staging@" + record["member_digest"]
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
        registry.validate_member_record(record)


def test_validate_index_record_reopens_only_canonical_role_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index evidence reopens hash, identity, booleans, and exact write role."""
    registry, _ = _modules()
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    members = _publication_members()
    parent_plans = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
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
    assert registry.validate_index_record(record, parent_plans=parent_plans) == record

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
            "reference": (
                "ghcr.io/supermarioyl/ucm-release-staging@sha256:" + "1" * 64
            ),
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
    ] = "ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1"
    mutations.append(wrong_reference)
    for mutation in mutations:
        _rehash_publication_record(mutation)
        with pytest.raises(ValueError):
            registry.validate_index_record(mutation, parent_plans=parent_plans)


def test_index_create_uses_exact_dry_run_bytes_and_postwrite_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write inputs, expected digest, and post-read bytes are one exact object."""
    registry, _ = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
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
                "https://github.com/SuperMarioYL/unified-cache-management"
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
    )

    assert result["index_digest"] == expected
    assert result["manifest_sha256"] == expected
    assert result["postwrite_manifest_sha256"] == expected
    assert set(result) == registry.INDEX_RECORD_KEYS | {
        "decision",
        "postwrite_manifest_sha256",
        "preflight_sha256",
        "matrix_sha256",
        "verification_sha256",
    }
    strict_record = {key: result[key] for key in registry.INDEX_RECORD_KEYS}
    assert registry.validate_index_record(strict_record, parent_plans=parent) == (
        strict_record
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
        "ghcr.io/supermarioyl/ucm-release-staging@"
        + plan["members"][0]["member_digest"],
        "ghcr.io/supermarioyl/ucm-release-staging@"
        + plan["members"][1]["member_digest"],
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
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
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

    def fake_readback(reference: str, *, anonymous: bool = False) -> dict[str, object]:
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
        plan, parent_plans=parent, lane="protected-tag"
    )

    assert calls == [False]
    assert closure_calls == [False]
    assert provisional["kind"] == "ucm-registry-index-provisional"
    assert "anonymous_readback_sha256" not in provisional
    assert (
        registry.validate_provisional_index(provisional, parent_plans=parent)
        == provisional
    )
    assert (
        verify.audit_operations(provisional["operations"], lane="protected-tag")[
            "write_count"
        ]
        == 1
    )

    finalized = registry.finalize_index(provisional, parent_plans=parent)
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
    assert registry.validate_index_record(final, parent_plans=parent) == final
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
        registry.validate_finalized_index(forged_finalized, parent_plans=parent)


def test_index_plans_reject_mixed_sources_and_writer_rechecks_live_tag_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No final r1 transport may begin for mixed or stale protected sources."""
    registry, _ = _modules()
    mixed = _publication_members()
    mixed[0]["source_sha"] = "b" * 40
    mixed[0]["record_sha256"] = registry.sha256_value(
        {key: value for key, value in mixed[0].items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="source"):
        registry.plan_indexes(
            mixed,
            inventory=[],
            member_statuses={item["spec_id"]: "success" for item in mixed},
            lane="protected-tag",
        )

    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
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
            parent["plans"][0], parent_plans=parent, lane="protected-tag"
        )
    assert called is False

    head_mismatch = _protected_preflight()
    head_mismatch["default_branch_sha"] = "b" * 40
    monkeypatch.setattr(registry.core, "tag_preflight", lambda **_: head_mismatch)
    with pytest.raises(ValueError, match="default branch|first publication"):
        registry.prepare_index(
            parent["plans"][0], parent_plans=parent, lane="protected-tag"
        )
    assert called is False


def test_index_prepare_fails_when_child_manifest_is_missing_from_final_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index descriptor alone cannot prove its child is pullable cross-repository."""
    registry, _ = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
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
        registry.prepare_index(plan, parent_plans=parent, lane="protected-tag")


def test_index_provisional_cannot_forge_parent_or_finalize_with_authenticated_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize derives the target from the parent and requires an anonymous readback."""
    registry, _ = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
    plan = parent["plans"][0]
    target = plan["target_repository"] + ":" + plan["target_tag"]
    digest = plan["expected_index_digest"]
    authenticated_payload = {
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
    authenticated = {
        **authenticated_payload,
        "readback_sha256": registry.sha256_value(authenticated_payload),
    }
    provisional_payload = {
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
        "authenticated_readback": authenticated,
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
        "operations": [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": target,
            }
        ],
        "decision": "create",
        "postwrite_manifest_sha256": digest,
        "preflight_sha256": "sha256:" + "a" * 64,
        "matrix_sha256": registry.core.build_matrix("protected-tag")["matrix_sha256"],
        "verification_sha256": registry.verify_index(plan, parent_plans=parent)[
            "verification_sha256"
        ],
        "parent_plans_sha256": parent["plans_sha256"],
    }
    provisional = {
        **provisional_payload,
        "provisional_sha256": registry.sha256_value(provisional_payload),
    }
    assert (
        registry.validate_provisional_index(provisional, parent_plans=parent)
        == provisional
    )

    forged = copy.deepcopy(provisional)
    forged["target_tag"] = "attacker"
    forged["provisional_sha256"] = registry.sha256_value(
        {key: value for key, value in forged.items() if key != "provisional_sha256"}
    )
    with pytest.raises(ValueError):
        registry.validate_provisional_index(forged, parent_plans=parent)

    monkeypatch.setattr(
        registry, "readback_reference", lambda *_args, **_kwargs: authenticated
    )
    with pytest.raises(ValueError, match="anonymous"):
        registry.finalize_index(provisional, parent_plans=parent)


def test_provisional_and_finalized_index_readbacks_are_strict_and_mode_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-job evidence rejects loose types, extra keys, and mixed auth ledgers."""
    registry, _ = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
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

    auth = readback(authenticated=True)
    provisional_payload = {
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
        "authenticated_readback": auth,
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
        "operations": [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": target,
            }
        ],
        "decision": "create",
        "postwrite_manifest_sha256": digest,
        "preflight_sha256": "sha256:" + "a" * 64,
        "matrix_sha256": registry.core.build_matrix("protected-tag")["matrix_sha256"],
        "verification_sha256": registry.verify_index(plan, parent_plans=parent)[
            "verification_sha256"
        ],
        "parent_plans_sha256": parent["plans_sha256"],
    }
    provisional = {
        **provisional_payload,
        "provisional_sha256": registry.sha256_value(provisional_payload),
    }
    assert (
        registry.validate_provisional_index(provisional, parent_plans=parent)
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
            registry.validate_provisional_index(forged, parent_plans=parent)

    mixed = readback(authenticated=False)
    mixed["operations"] = copy.deepcopy(auth["operations"])
    mixed["readback_sha256"] = registry.sha256_value(
        {key: item for key, item in mixed.items() if key != "readback_sha256"}
    )
    monkeypatch.setattr(registry, "readback_reference", lambda *_args, **_kwargs: mixed)
    with pytest.raises(ValueError):
        registry.finalize_index(provisional, parent_plans=parent)


def test_index_rerun_defers_same_build_key_digest_to_exact_dry_run() -> None:
    """Buildx formatting may change bytes without changing parsed index intent."""
    registry, _ = _modules()
    members = _publication_members()
    statuses = {item["spec_id"]: "success" for item in members}
    absent = registry.plan_indexes(
        members, inventory=[], member_statuses=statuses, lane="protected-tag"
    )
    plan = absent["plans"][0]
    buildx_raw = json.dumps(plan["index_manifest"], indent=2).encode()
    actual_digest = "sha256:" + hashlib.sha256(buildx_raw).hexdigest()
    assert actual_digest != plan["expected_index_digest"]
    inventory = [
        {
            "repository": plan["target_repository"],
            "tag": plan["target_tag"],
            "digest": actual_digest,
            "build_key_sha256": plan["index_build_key_sha256"],
        }
    ]

    rerun = registry.plan_indexes(
        members,
        inventory=inventory,
        member_statuses=statuses,
        lane="protected-tag",
    )

    assert rerun["plans"][0]["decision"] == "reuse"


def test_registry_aggregate_keeps_authenticated_and_anonymous_states_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated provisional evidence cannot claim final public publication."""
    registry, verify = _modules()
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
    provisionals = [
        _provisional_index_evidence(registry, parent, plan) for plan in parent["plans"]
    ]
    member_collection = {
        "schema_version": 1,
        "kind": "ucm-member-artifact-collection",
        "source_sha": "a" * 40,
        "member_records": [
            f"out/member-records/{item['spec_id']}.json" for item in members
        ],
        "member_record_sha256s": {
            item["spec_id"]: item["record_sha256"] for item in members
        },
        "member_preflight_sha256s": {
            item["spec_id"]: "sha256:" + "e" * 64 for item in members
        },
    }
    member_collection["collection_sha256"] = verify.sha256_value(
        {
            key: value
            for key, value in member_collection.items()
            if key != "member_records"
        }
    )
    provisional_collection = {
        "schema_version": 1,
        "kind": "ucm-provisional-artifact-collection",
        "source_sha": "a" * 40,
        "parent_plans_sha256": parent["plans_sha256"],
        "provisional_indexes": [
            f"out/provisionals/{item['family_id']}.json" for item in provisionals
        ],
        "provisional_sha256s": {
            item["family_id"]: item["provisional_sha256"] for item in provisionals
        },
        "provisional_preflight_sha256s": {
            item["family_id"]: item["preflight_sha256"] for item in provisionals
        },
    }
    provisional_collection["collection_sha256"] = verify.sha256_value(
        {
            key: value
            for key, value in provisional_collection.items()
            if key != "provisional_indexes"
        }
    )

    authenticated = verify.authenticated_registry_publication_evidence(
        member_records=members,
        member_collection=member_collection,
        provisional_indexes=provisionals,
        provisional_collection=provisional_collection,
        parent_plans=parent,
        source_sha="a" * 40,
        run={"run_id": "123", "run_attempt": 4},
    )

    assert authenticated["payload"]["kind"] == (
        "ucm-authenticated-registry-publication-payload"
    )
    assert authenticated["payload"]["publication"] == {
        "registry": "authenticated-passed",
        "anonymous": "pending",
        "github_release": "pending",
    }
    assert "release_manifest_sha256" not in authenticated["payload"]
    assert authenticated["payload"]["member_records"] == parent["member_records"]
    assert authenticated["payload"]["provisional_indexes"] == provisionals

    plans_by_reference = {
        plan["target_repository"] + ":" + plan["target_tag"]: plan
        for plan in parent["plans"]
    }

    def anonymous_readback(
        reference: str, *, anonymous: bool = False
    ) -> dict[str, object]:
        assert anonymous is True
        plan = plans_by_reference[reference]
        return _index_readback_evidence(
            registry,
            plan,
            plan["expected_index_digest"],
            authenticated=False,
        )

    def anonymous_closure(
        plan: dict[str, object],
        *,
        index_digest: str,
        anonymous: bool = False,
    ) -> dict[str, object]:
        assert anonymous is True
        return _index_closure_evidence(
            registry, plan, index_digest, authenticated=False
        )

    monkeypatch.setattr(registry, "readback_reference", anonymous_readback)
    monkeypatch.setattr(registry, "_validate_remote_index_closure", anonymous_closure)
    finalized = [
        registry.finalize_index(provisional, parent_plans=parent)
        for provisional in provisionals
    ]
    published = verify.protected_registry_publication_evidence(
        member_records=members,
        member_collection=member_collection,
        finalized_indexes=finalized,
        provisional_collection=provisional_collection,
        parent_plans=parent,
        source_sha="a" * 40,
        run={"run_id": "123", "run_attempt": 4},
    )

    assert published["payload"]["publication"] == {
        "registry": "published",
        "anonymous": "passed",
        "github_release": "pending",
    }
    assert published["payload"]["release_manifest_sha256"].startswith("sha256:")
    assert published["payload"]["member_records"] == parent["member_records"]
    assert published["payload"]["finalized_indexes"] == finalized
    assert len(published["payload"]["index_records"]) == 3
    assert (
        verify.authenticated_registry_publication_evidence(
            member_records=authenticated["payload"]["member_records"],
            member_collection=authenticated["payload"]["member_collection"],
            provisional_indexes=authenticated["payload"]["provisional_indexes"],
            provisional_collection=authenticated["payload"]["provisional_collection"],
            parent_plans=authenticated["payload"]["parent_plans"],
            source_sha="a" * 40,
            run={"run_id": "123", "run_attempt": 4},
        )
        == authenticated
    )
    assert (
        verify.protected_registry_publication_evidence(
            member_records=published["payload"]["member_records"],
            member_collection=published["payload"]["member_collection"],
            finalized_indexes=published["payload"]["finalized_indexes"],
            provisional_collection=published["payload"]["provisional_collection"],
            parent_plans=published["payload"]["parent_plans"],
            source_sha="a" * 40,
            run={"run_id": "123", "run_attempt": 4},
        )
        == published
    )

    asset_root = tmp_path / "release-assets"
    asset_root.mkdir()
    assets: list[dict[str, object]] = []
    for authority in verify._canonical_release_asset_authorities():
        path = asset_root / authority["name"]
        path.write_bytes(str(authority["spec_id"]).encode())
        assets.append(
            {
                **copy.deepcopy(authority),
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "path": str(path),
            }
        )
    asset_manifest = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets",
        "source_sha": "a" * 40,
        "assets": assets,
    }
    asset_manifest["assets_sha256"] = verify.sha256_value(
        {
            **asset_manifest,
            "assets": [
                {key: value for key, value in asset.items() if key != "path"}
                for asset in assets
            ],
        }
    )
    authority = verify.github_release_authority("a" * 40)
    release = {
        "id": 41,
        "tag_name": authority["tag_name"],
        "target_commitish": "develop",
        "name": authority["name"],
        "body": authority["body"],
        "draft": False,
        "prerelease": True,
        "assets": [{"id": 600 + index} for index in range(7)],
        "author": {"login": "github-actions[bot]", "type": "Bot"},
        "upload_url": (
            "https://uploads.github.com/repos/SuperMarioYL/"
            "unified-cache-management/releases/41/assets{?name,label}"
        ),
        "url": (
            "https://api.github.com/repos/SuperMarioYL/"
            "unified-cache-management/releases/41"
        ),
        "assets_url": (
            "https://api.github.com/repos/SuperMarioYL/"
            "unified-cache-management/releases/41/assets"
        ),
    }
    remote_assets = [
        {
            "release_id": 41,
            "asset_id": 600 + index,
            "name": asset["name"],
            "size": asset["size"],
            "state": "uploaded",
            "digest": asset["sha256"],
            "api_url": (
                "https://api.github.com/repos/SuperMarioYL/"
                f"unified-cache-management/releases/assets/{600 + index}"
            ),
            "browser_download_url": (
                "https://github.com/SuperMarioYL/unified-cache-management/"
                f"releases/download/v0.5.0rc1/{asset['name']}"
            ),
            "uploader": {"login": "github-actions[bot]", "type": "Bot"},
            "download_sha256": asset["sha256"],
            "download_size": asset["size"],
        }
        for index, asset in enumerate(assets)
    ]
    release_plan = verify.plan_github_release(release, "a" * 40)
    asset_plan = verify.plan_release_assets(
        asset_manifest,
        remote_assets,
        release_id=41,
        allowed_root=asset_root,
        release_published=True,
    )
    operations = verify.build_github_release_operation_ledger(
        prepare_initial_plan=release_plan,
        initial_release=release,
        initial_asset_plan=asset_plan,
        authenticated_assets=remote_assets,
        upload_transcript=[],
        source_sha="a" * 40,
    )
    github = verify.github_release_publication_evidence(
        protected_registry=published,
        asset_manifest=asset_manifest,
        allowed_root=asset_root,
        prepare_initial_plan=release_plan,
        prepare_release=release,
        initial_release=release,
        initial_assets=remote_assets,
        initial_asset_plan=asset_plan,
        upload_transcript=[],
        prepublish_release=release,
        prepublish_assets=remote_assets,
        authenticated_release=release,
        authenticated_assets=remote_assets,
        anonymous_release=release,
        anonymous_assets=remote_assets,
        operations=operations,
        source_sha="a" * 40,
        run={"run_id": "123", "run_attempt": 4},
    )
    assert (
        github["payload"]["protected_registry_payload_sha256"]
        == published["payload_sha256"]
    )
    assert github["payload"]["publication"] == "published-prerelease"

    split_brain_members = copy.deepcopy(members)
    split_brain_members[0]["image_result_sha256"] = "sha256:" + "f" * 64
    _rehash_publication_record(split_brain_members[0])
    with pytest.raises(ValueError, match="parent|member"):
        verify.authenticated_registry_publication_evidence(
            member_records=split_brain_members,
            member_collection=member_collection,
            provisional_indexes=provisionals,
            provisional_collection=provisional_collection,
            parent_plans=parent,
            source_sha="a" * 40,
        )
    with pytest.raises(ValueError, match="final|provisional|index"):
        verify.protected_registry_publication_evidence(
            member_records=members,
            member_collection=member_collection,
            finalized_indexes=provisionals,
            provisional_collection=provisional_collection,
            parent_plans=parent,
            source_sha="a" * 40,
        )


def test_publish_member_rederives_record_from_image_result_and_registry_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot supply the publication record that authorizes its own write."""
    registry, verify = _modules()
    image = importlib.import_module("ucm_release.image")
    archive, expected = _valid_oci_archive(
        tmp_path, _member_with_buildkit_rewritten_timestamp()
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
        image, "validate_image_result", lambda value: copy.deepcopy(value)
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
            archive, image_result=image_result, lane="protected-tag"
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
        registry.publish_member(archive, image_result=stale, lane="protected-tag")
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
            archive, image_result=forged_identity, lane="protected-tag"
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
            )
        assert not invocation_log.exists()

    record = registry.publish_member(
        archive, image_result=image_result, lane="protected-tag"
    )

    assert record["member_digest"] == expected["member_digest"]
    assert record["image_result_sha256"] == expected["image_result_sha256"]
    assert record["layers"][0]["annotations"] == {
        "buildkit/rewritten-timestamp": "1786353770"
    }
    assert record["prewrite_visibility_evidence_sha256"].startswith("sha256:")
    assert registry.validate_member_record(record) == record
    assert {item["type"] for item in record["operations"]} >= {
        "registry-member-push-by-digest",
        "registry-staging-tag-create",
        "registry-authenticated-config-blob-read",
        "registry-authenticated-layer-blob-read",
    }
    assert verify.audit_operations(record["operations"], lane="protected-tag") == {
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
    staging_reference = "ghcr.io/supermarioyl/ucm-release-staging:staging-" + expected[
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
    archive, expected = _valid_oci_archive(tmp_path, _publication_members()[0])
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
        image, "validate_image_result", lambda value: copy.deepcopy(value)
    )
    monkeypatch.setattr(
        registry.core, "tag_preflight", lambda **_: _protected_preflight()
    )
    monkeypatch.setattr(registry, "resolve_pinned_crane", lambda: "/pinned/crane")

    def visibility(reference: str, *, phase: str = "postwrite") -> dict[str, object]:
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
        registry.STAGING_REPOSITORY
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
            archive, image_result=image_result, lane="protected-tag"
        )
    assert pushed == []


def test_member_prewrite_and_postwrite_visibility_evidence_must_be_distinct() -> None:
    """A record cannot collapse the two sides of the private staging write gate."""
    registry, _ = _modules()
    record = _publication_members()[0]
    record["visibility_evidence_sha256"] = record["prewrite_visibility_evidence_sha256"]
    record["record_sha256"] = registry.sha256_value(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )
    with pytest.raises(ValueError, match="visibility"):
        registry.validate_member_record(record)


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
  echo 'Error: GET https://ghcr.io/token?scope=repository%3Asupermarioyl%2Fucm-release-staging%3Apull&service=ghcr.io: DENIED: requested access to the resource is denied' >&2
  exit 1
fi
if [ {mode!r} = ghcr-manifest-denied ]; then
  echo 'Error: fetching manifest: GET https://ghcr.io/v2/supermarioyl/ucm-release-staging/manifests/staging-{'2' * 64}: UNAUTHORIZED: authentication required' >&2
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
    reference = "ghcr.io/supermarioyl/ucm-release-staging:staging-" + "2" * 64

    if accepted:
        evidence = registry.verify_private_staging(reference)
        assert evidence["status"] == "anonymous-denied"
        assert evidence["returncode"] == 1
        assert evidence["stdout_sha256"].startswith("sha256:")
        assert evidence["stderr_sha256"].startswith("sha256:")
        assert evidence["operation"] == {
            "type": "registry-anonymous-visibility-read",
            "capability": "read",
            "reference": reference,
        }
        prewrite = registry.verify_private_staging(reference, phase="prewrite")
        assert prewrite["operation"]["type"] == (
            "registry-anonymous-prewrite-visibility-read"
        )
    else:
        with pytest.raises(ValueError, match="public|anonymous|network|denial"):
            registry.verify_private_staging(reference)


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
    members = _publication_members()
    parent = registry.plan_indexes(
        members,
        inventory=[],
        member_statuses={item["spec_id"]: "success" for item in members},
        lane="protected-tag",
    )
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
                repository="ghcr.io/supermarioyl/ucm-release-staging",
                crane_binary="/pinned/crane",
            )
