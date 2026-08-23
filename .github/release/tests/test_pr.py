"""PR probe aggregation into the shared formal selection contract."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "builders"
TAG_FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
core = importlib.import_module("ucm_release.core")
policy = importlib.import_module("ucm_release.policy")
pr = importlib.import_module("ucm_release.pr")
upstream = importlib.import_module("ucm_release.upstream")


def _formal_inputs():
    formal = policy.resolve(
        repository="release-org/unified-cache-management",
        version_override="0.7.60rc1",
    )
    selection = upstream.resolve_upstreams(
        formal,
        tag_fixture=core.load_json(TAG_FIXTURE),
        snapshot_dir=FIXTURE,
    )
    catalog = builders.catalog_from_selection(
        selection, owner="release-org", formal_policy=formal
    )
    return formal, selection, catalog


def _probe(*, revision: str = "", tag: str = "nightly-weird") -> dict[str, object]:
    return {
        "kind": "ucm-runtime-probe",
        "schema_version": 1,
        "probes": [
            {
                "probe_id": "runtime-001-amd64",
                "request_id": "runtime-001",
                "product_id": "vllm",
                "runtime_ref": f"docker.io/vllm/vllm-openai:{tag}",
                "repository": "docker.io/vllm/vllm-openai",
                "tag": tag,
                "target_repository": "ghcr.io/release-org/vllm-openai",
                "configured_source_repository": "vllm-project/vllm",
                "cpu_arch": "amd64",
                "platform": "linux/amd64",
                "runner": "ubuntu-24.04",
                "image_reference": "docker.io/vllm/vllm-openai@sha256:" + "1" * 64,
                "backend": "cuda",
                "accelerator_runtime": "cuda-12.9",
                "soc_version": "na",
                "python_version": "3.12",
                "python_abi": "cp312",
                "os_id": "ubuntu",
                "os_version": "24.04",
                "glibc_version": "2.39",
                "oci_labels": {},
                "oci_source": "https://github.com/vllm-project/vllm",
                "oci_revision": revision,
            }
        ],
    }


def _registry_record(selection, catalog):
    build = next(
        item
        for item in selection["wheel_builds"]
        if item["id"] == "cuda129-cp312-amd64"
    )
    builder = next(
        item for item in catalog["builders"] if item["id"] == "cuda129-cp312-amd64"
    )
    return {
        **build,
        "target_repository": builder["target_repository"],
        "target_tag": builder["target_tag"],
        "checks": builder["checks"],
        "created": "2026-08-23T12:00:00Z",
        "checked": True,
    }


def test_no_revision_uses_checked_registry_builder_and_opaque_tag() -> None:
    formal, selection, catalog = _formal_inputs()
    inventory = {
        "kind": "ucm-builder-registry",
        "schema_version": 1,
        "builders": [_registry_record(selection, catalog)],
    }

    result = pr.resolve_pr_request(
        formal,
        _probe(),
        inventory,
        pr_number=42,
        author="Release-Author",
        run_id=998877,
    )

    assert result["ok"] is True
    selected = result["selection"]
    assert [item["id"] for item in selected["wheel_builds"]] == ["cuda129-cp312-amd64"]
    runtime = selected["runtimes"][0]
    assert runtime["runtime_tag"] == "nightly-weird"
    assert runtime["channel"] == "pinned"
    assert runtime["architectures"] == ["amd64"]
    assert runtime["member_references"]["amd64"].startswith(
        "docker.io/vllm/vllm-openai@sha256:"
    )
    assert runtime["target_tag"].startswith(
        "pr-42-release-author-run-998877-nightly-weird"
    )
    assert result["publication"]["families"][0]["has_index"] is False
    assert selected["wheel_builds"][0]["sync_mode"] == "registry-only"
    with pytest.raises(ValueError, match="Registry-only Builders disappeared"):
        builders.compute_sync_plan(result["builder_catalog"], {})


def test_revision_prefers_exact_recipe_and_can_sync_missing_builder() -> None:
    formal, selection, _catalog = _formal_inputs()
    exact = next(
        item
        for item in selection["wheel_builds"]
        if item["id"] == "cuda129-cp312-amd64"
    )
    exact = dict(exact)
    exact["source_ref"] = "a" * 40
    calls = []

    def resolve(product_id, revision, probes):
        calls.append((product_id, revision, len(probes)))
        return [exact]

    result = pr.resolve_pr_request(
        formal,
        _probe(revision="a" * 40, tag="cu129-nightly-a"),
        {"kind": "ucm-builder-registry", "schema_version": 1, "builders": []},
        pr_number=7,
        author="builder",
        run_id=123,
        exact_build_resolver=resolve,
    )

    assert result["ok"] is True
    assert calls == [("vllm", "a" * 40, 1)]
    assert result["selection"]["wheel_builds"][0]["source_ref"] == "a" * 40
    assert result["builder_catalog"]["builders"][0]["target_tag"].endswith(
        "-r" + exact["recipe_revision"]
    )


def test_revision_failure_never_silently_falls_back_to_registry() -> None:
    formal, selection, catalog = _formal_inputs()
    inventory = {
        "kind": "ucm-builder-registry",
        "schema_version": 1,
        "builders": [_registry_record(selection, catalog)],
    }

    result = pr.resolve_pr_request(
        formal,
        _probe(revision="b" * 40),
        inventory,
        pr_number=1,
        author="builder",
        run_id=2,
        exact_build_resolver=lambda *_args: (_ for _ in ()).throw(
            ValueError("upstream Workflow changed")
        ),
    )

    assert result["ok"] is False
    assert result["problems"][0]["stage"] == "exact-source"
    assert result["problems"][0]["reason"] == "unresolvable-exact-recipe"
    assert "upstream Workflow changed" in result["problems"][0]["detail"]


def test_multiple_exact_tags_with_same_capability_reuse_one_wheel() -> None:
    formal, selection, _catalog = _formal_inputs()
    base = _probe(revision="a" * 40, tag="nightly-a")["probes"][0]
    second = dict(base)
    second.update(
        {
            "probe_id": "runtime-002-amd64",
            "request_id": "runtime-002",
            "runtime_ref": "docker.io/vllm/vllm-openai:nightly-b",
            "tag": "nightly-b",
            "oci_revision": "b" * 40,
        }
    )
    probe = {"kind": "ucm-runtime-probe", "schema_version": 1, "probes": [base, second]}
    template = next(
        item
        for item in selection["wheel_builds"]
        if item["id"] == "cuda129-cp312-amd64"
    )

    def resolve(_product_id, revision, _probes):
        item = dict(template)
        item["source_ref"] = revision
        item["recipe_revision"] = revision[0] * 12
        return [item]

    result = pr.resolve_pr_request(
        formal,
        probe,
        {"kind": "ucm-builder-registry", "schema_version": 1, "builders": []},
        pr_number=4,
        author="builder",
        run_id=5,
        exact_build_resolver=resolve,
    )

    assert result["ok"] is True
    assert len(result["selection"]["wheel_builds"]) == 1
    assert len(result["selection"]["runtimes"]) == 2
    assert {
        runtime["wheel_build_ids"]["amd64"]
        for runtime in result["selection"]["runtimes"]
    } == {"cuda129-cp312-amd64"}


def test_blocked_a5_reports_platform_reason_before_builder_matching() -> None:
    formal, _selection, _catalog = _formal_inputs()
    probe = _probe()["probes"][0]
    probe.update(
        {
            "product_id": "vllm-ascend",
            "runtime_ref": "quay.io/ascend/vllm-ascend:nightly-a5",
            "repository": "quay.io/ascend/vllm-ascend",
            "tag": "nightly-a5",
            "target_repository": "ghcr.io/release-org/vllm-ascend",
            "configured_source_repository": "vllm-project/vllm-ascend",
            "backend": "cann-a5",
            "accelerator_runtime": "cann-9.1.0",
            "soc_version": "ascend950dt_9582",
            "oci_source": "",
        }
    )

    result = pr.resolve_pr_request(
        formal,
        {"kind": "ucm-runtime-probe", "schema_version": 1, "probes": [probe]},
        {"kind": "ucm-builder-registry", "schema_version": 1, "builders": []},
        pr_number=1,
        author="builder",
        run_id=2,
    )

    assert result["ok"] is False
    assert result["problems"][0]["stage"] == "platform-policy"
    assert result["problems"][0]["reason"] == "blocked-backend"
    assert "dedicated UCM native implementation" in result["problems"][0]["detail"]


def test_no_revision_dual_arch_can_use_builders_from_different_source_refs() -> None:
    formal, selection, catalog = _formal_inputs()
    amd64 = _registry_record(selection, catalog)
    arm_build = next(
        item
        for item in selection["wheel_builds"]
        if item["id"] == "cuda129-cp312-arm64"
    )
    arm_builder = next(
        item for item in catalog["builders"] if item["id"] == "cuda129-cp312-arm64"
    )
    arm64 = {
        **arm_build,
        "source_ref": "different-compatible-revision",
        "target_repository": arm_builder["target_repository"],
        "target_tag": arm_builder["target_tag"],
        "checks": arm_builder["checks"],
        "created": "2026-08-23T13:00:00Z",
        "checked": True,
    }
    first = _probe()["probes"][0]
    second = dict(first)
    second.update(
        {
            "probe_id": "runtime-001-arm64",
            "cpu_arch": "arm64",
            "platform": "linux/arm64",
            "runner": "ubuntu-24.04-arm",
            "image_reference": "docker.io/vllm/vllm-openai@sha256:" + "2" * 64,
        }
    )
    result = pr.resolve_pr_request(
        formal,
        {
            "kind": "ucm-runtime-probe",
            "schema_version": 1,
            "probes": [first, second],
        },
        {
            "kind": "ucm-builder-registry",
            "schema_version": 1,
            "builders": [amd64, arm64],
        },
        pr_number=3,
        author="builder",
        run_id=4,
    )

    assert result["ok"] is True
    selected_runtime = result["selection"]["runtimes"][0]
    assert selected_runtime["source_ref"] == "registry-capability"
    assert selected_runtime["wheel_build_ids"] == {
        "amd64": "cuda129-cp312-amd64",
        "arm64": "cuda129-cp312-arm64",
    }
