"""RED contracts for the unified, facts-first Capability Catalog."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
FIXTURE_PATH = RELEASE_ROOT / "tests" / "fixtures" / (
    "capability-catalog-discovery.json"
)
sys.path.insert(0, str(RELEASE_ROOT))

capabilities = importlib.import_module("ucm_release.capabilities")

CATALOG_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "upstream_reads",
    "builder_sync",
    "builder_capabilities",
    "builder_revisions",
    "runtime_candidates",
    "bindings",
    "entries",
    "exclusions",
    "catalog_sha256",
}
CAPABILITY_FIELDS = {
    "builder_capability_id",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "python_version",
    "python_abi",
    "mooncake_version",
    "builder_revision_ids",
}
REVISION_FIELDS = {
    "builder_revision_id",
    "builder_capability_id",
    "source_image_repository",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "revision_sha256",
}
RUNTIME_FIELDS = {
    "runtime_id",
    "product_id",
    "runtime_version",
    "channel",
    "variant",
    "cpu_architecture",
    "accelerator",
    "accelerator_runtime",
    "mooncake_version",
    "runtime_image",
    "git_tag",
    "git_commit",
}
ENTRY_FIELDS = {
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "python_version",
    "python_abi",
    "source_image",
    "target_image",
    "mooncake_version",
}


def _load_fixture() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _require_public_callable(name: str) -> Callable[..., dict[str, Any]]:
    function = getattr(capabilities, name, None)
    message = f"required public API ucm_release.capabilities.{name} is missing"
    assert callable(function), message
    return function


def _assemble() -> dict[str, Any]:
    fixture = _load_fixture()
    original = copy.deepcopy(fixture)
    assemble = _require_public_callable("assemble_capability_catalog")

    catalog = assemble(
        builder_discovery=fixture["builder_discovery"],
        runtime_discovery=fixture["runtime_discovery"],
        python_probes=fixture["python_probes"],
        mooncake_probes=fixture["mooncake_probes"],
        python_requires=fixture["python_requires"],
    )

    assert fixture == original, "Catalog assembly must not mutate discovery facts"
    assert isinstance(catalog, dict)
    return catalog


def _catalog_digest(catalog: dict[str, Any]) -> str:
    projection = copy.deepcopy(catalog)
    projection.pop("catalog_sha256", None)
    payload = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _reseal(catalog: dict[str, Any]) -> None:
    catalog["catalog_sha256"] = _catalog_digest(catalog)


def _assert_no_wall_clock_fields(value: object) -> None:
    if isinstance(value, dict):
        forbidden = {"created_at", "completed_at", "generated_at", "timestamp"}
        assert forbidden.isdisjoint(value)
        for nested in value.values():
            _assert_no_wall_clock_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_wall_clock_fields(nested)


def test_assembled_catalog_is_closed_digest_bound_and_valid() -> None:
    """A partial, open, or non-canonical object cannot be a Catalog artifact."""
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()

    assert set(catalog) == CATALOG_FIELDS
    assert catalog["kind"] == "ucm-capability-catalog"
    assert catalog["schema_version"] == 3
    assert catalog["source_sha"] == "1" * 40
    assert catalog["builder_sync"] == _load_fixture()["builder_discovery"][
        "builder_sync"
    ]
    assert catalog["catalog_sha256"] == _catalog_digest(catalog)
    assert validate(copy.deepcopy(catalog)) == catalog
    _assert_no_wall_clock_fields(catalog)


def test_catalog_entries_cover_discovered_dimensions_and_filter_python_requires(
) -> None:
    """Single-ABI or fixed-runtime assembly loses supported Builder products."""
    catalog = _assemble()
    capabilities_by_coordinate = {
        (
            item["accelerator_runtime"],
            item["variant"],
            item["cpu_architecture"],
            item["python_abi"],
        )
        for item in catalog["builder_capabilities"]
    }

    accelerator_runtimes = {
        item["accelerator_runtime"] for item in catalog["builder_capabilities"]
    }
    assert accelerator_runtimes >= {
        "cuda-12.9",
        "cuda-13.0",
        "cann-9.0",
        "cann-9.1",
    }
    assert {item["cpu_architecture"] for item in catalog["builder_capabilities"]} == {
        "amd64",
        "arm64",
    }
    assert {item["python_abi"] for item in catalog["builder_capabilities"]} == {
        "cp310",
        "cp311",
        "cp312",
    }
    assert ("cuda-12.9", "default", "amd64", "cp310") in capabilities_by_coordinate
    assert ("cuda-12.9", "default", "amd64", "cp311") in capabilities_by_coordinate
    assert ("cuda-13.0", "default", "arm64", "cp312") in capabilities_by_coordinate
    assert ("cann-9.0", "a2", "amd64", "cp310") in capabilities_by_coordinate
    assert ("cann-9.1", "a4", "arm64", "cp311") in capabilities_by_coordinate

    assert catalog["entries"]
    assert all(ENTRY_FIELDS <= set(entry) for entry in catalog["entries"])
    assert all("@sha256:" in entry["source_image"] for entry in catalog["entries"])
    assert all("@sha256:" in entry["target_image"] for entry in catalog["entries"])
    assert all(entry["python_abi"] != "cp39" for entry in catalog["entries"])
    rejected_cp39 = [
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "python-requires-mismatch"
    ]
    assert rejected_cp39
    assert {item["evidence"]["python_abi"] for item in rejected_cp39} == {"cp39"}
    assert all(
        item["evidence"]["python_requires"] == ">=3.10"
        for item in rejected_cp39
    )


def test_catalog_preserves_discovery_origins_and_filters_only_exact_310p() -> None:
    """Source provenance and future Variants must survive canonical assembly."""
    catalog = _assemble()
    reads = catalog["upstream_reads"]

    assert any(
        read["project"] == "vllm-project/vllm"
        and read["source_kind"] == "buildkite-build-base-image"
        and read["fact"] == "BUILD_BASE_IMAGE"
        for read in reads
    )
    ascend_paths = {
        read["source_path"]
        for read in reads
        if read["source_kind"] == "buildwheel-dockerfile"
    }
    assert ascend_paths == {
        ".github/workflows/dockerfiles/Dockerfile.buildwheel.310p",
        ".github/workflows/dockerfiles/Dockerfile.buildwheel.a2",
        ".github/workflows/dockerfiles/Dockerfile.buildwheel.a4",
    }
    assert any(item["variant"] == "a4" for item in catalog["builder_capabilities"])
    assert all(
        item["variant"] != "310p" for item in catalog["builder_capabilities"]
    )
    assert all(item["variant"] != "310p" for item in catalog["entries"])
    assert any(
        item["reason_code"] == "variant-filtered-310p"
        and item["evidence"]["variant"] == "310p"
        for item in catalog["exclusions"]
    )


def test_runtime_candidates_remain_multi_version_and_git_source_bound() -> None:
    """Latest-wins collapse destroys fallback and baseline runtime identities."""
    catalog = _assemble()
    runtimes = catalog["runtime_candidates"]

    assert all(RUNTIME_FIELDS <= set(runtime) for runtime in runtimes)
    assert {runtime["product_id"] for runtime in runtimes} == {"vllm", "vllm-ascend"}
    assert {
        runtime["runtime_version"]
        for runtime in runtimes
        if runtime["product_id"] == "vllm"
        and runtime["accelerator_runtime"] == "cuda-12.9"
        and runtime["cpu_architecture"] == "amd64"
    } == {"0.10.0", "0.10.1"}
    assert all("@sha256:" in runtime["runtime_image"] for runtime in runtimes)
    assert all(runtime["git_tag"].startswith("v") for runtime in runtimes)
    assert all(len(runtime["git_commit"]) == 40 for runtime in runtimes)


def test_mooncake_runtime_copy_is_version_exact_and_mismatch_is_local() -> None:
    """A fixed clone or global mismatch failure violates Ascend isolation."""
    catalog = _assemble()
    matched = next(
        entry
        for entry in catalog["entries"]
        if entry["accelerator_runtime"] == "cann-9.0" and entry["variant"] == "a2"
    )

    assert matched["mooncake_version"] == "0.3.11.post1"
    assert matched["mooncake_provenance"] == {
        "mode": "runtime-copy",
        "runtime_image": matched["runtime_image"],
        "declared_version": "0.3.11.post1",
        "installed_version": "0.3.11.post1",
        "headers_path": "/usr/local/include/mooncake",
        "libraries_path": "/usr/local/lib",
    }

    mismatched_runtime = next(
        runtime
        for runtime in catalog["runtime_candidates"]
        if runtime["accelerator_runtime"] == "cann-9.1"
        and runtime["variant"] == "a4"
    )
    mismatch = next(
        item
        for item in catalog["exclusions"]
        if item["reason_code"] == "mooncake-version-mismatch"
        and item["runtime_id"] == mismatched_runtime["runtime_id"]
    )
    assert mismatch["evidence"] == {
        "declared_version": "0.3.12",
        "installed_version": "0.3.11.post1",
    }
    assert all(
        entry["runtime_id"] != mismatched_runtime["runtime_id"]
        for entry in catalog["entries"]
    )
    assert any(entry["accelerator"] == "cuda" for entry in catalog["entries"])
    assert any(
        entry["accelerator_runtime"] == "cann-9.0"
        for entry in catalog["entries"]
    )
    encoded = json.dumps(catalog, sort_keys=True)
    assert "0.3.9" not in encoded
    assert "git-clone" not in encoded


def test_stable_capability_retains_two_immutable_builder_revisions() -> None:
    """Capability-level latest-wins replacement loses baseline revisions."""
    catalog = _assemble()
    capability = next(
        item
        for item in catalog["builder_capabilities"]
        if item["accelerator_runtime"] == "cuda-13.0"
        and item["variant"] == "default"
        and item["cpu_architecture"] == "amd64"
        and item["python_abi"] == "cp312"
    )
    revision_ids = capability["builder_revision_ids"]
    revisions = [
        revision
        for revision in catalog["builder_revisions"]
        if revision["builder_revision_id"] in revision_ids
    ]

    assert set(capability) == CAPABILITY_FIELDS
    assert len(revision_ids) == 2
    assert revision_ids == sorted(revision_ids)
    assert len(revisions) == 2
    assert all(REVISION_FIELDS <= set(revision) for revision in revisions)
    assert len({revision["source_image_digest"] for revision in revisions}) == 2
    assert len({revision["target_builder_digest"] for revision in revisions}) == 2
    assert {
        binding["builder_revision_id"]
        for binding in catalog["bindings"]
        if binding["builder_capability_id"] == capability["builder_capability_id"]
    } == set(revision_ids)


def _duplicate_id(catalog: dict[str, Any]) -> None:
    catalog["builder_capabilities"].append(
        copy.deepcopy(catalog["builder_capabilities"][0])
    )


def _unknown_revision_id(catalog: dict[str, Any]) -> None:
    ids = catalog["builder_capabilities"][0]["builder_revision_ids"]
    ids.append("sha256:" + "f" * 64)
    ids.sort()


def _malformed_digest(catalog: dict[str, Any]) -> None:
    catalog["builder_revisions"][0]["source_image_digest"] = "sha256:not-a-digest"


def _conflicting_revision(catalog: dict[str, Any]) -> None:
    conflict = copy.deepcopy(catalog["builder_revisions"][0])
    conflict["target_tag"] += "-conflict"
    catalog["builder_revisions"].append(conflict)


def _missing_binding_target(catalog: dict[str, Any]) -> None:
    catalog["bindings"][0]["runtime_id"] = "sha256:" + "e" * 64


def _duplicate_entry_coordinate(catalog: dict[str, Any]) -> None:
    catalog["entries"].append(copy.deepcopy(catalog["entries"][0]))


def _noncanonical_runtime_order(catalog: dict[str, Any]) -> None:
    catalog["runtime_candidates"].reverse()


def _unknown_field(catalog: dict[str, Any]) -> None:
    catalog["future_field"] = "must be rejected until the contract owns it"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_duplicate_id, id="duplicate-id"),
        pytest.param(_unknown_revision_id, id="unknown-id"),
        pytest.param(_malformed_digest, id="malformed-digest"),
        pytest.param(_conflicting_revision, id="conflicting-same-revision"),
        pytest.param(_missing_binding_target, id="missing-binding-target"),
        pytest.param(_duplicate_entry_coordinate, id="duplicate-entry-coordinate"),
        pytest.param(_noncanonical_runtime_order, id="noncanonical-array-order"),
        pytest.param(_unknown_field, id="unknown-field"),
    ],
)
def test_catalog_validation_rejects_noncanonical_or_dangling_objects(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    """Reject-all cannot pass because every mutation starts from a valid Catalog."""
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()
    mutation(catalog)
    _reseal(catalog)

    with pytest.raises(ValueError):
        validate(catalog)


def test_catalog_validation_rejects_malformed_self_digest() -> None:
    """The self digest must be a valid digest and match the canonical projection."""
    validate = _require_public_callable("validate_capability_catalog")
    catalog = _assemble()
    catalog["catalog_sha256"] = "sha256:not-a-digest"

    with pytest.raises(ValueError):
        validate(catalog)
