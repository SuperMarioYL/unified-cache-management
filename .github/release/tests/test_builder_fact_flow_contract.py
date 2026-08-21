"""RED contracts for same-run Builder planning and fact collection."""

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
FIXTURE_PATH = (
    RELEASE_ROOT / "tests" / "fixtures" / "capability-catalog-discovery.json"
)
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")

PLAN_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "builder_plans",
    "failures",
    "matrix",
}
PLAN_IDENTITY_FIELDS = (
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "source_image_repository",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
)
BUILDER_PLAN_FIELDS = {
    "builder_plan_id",
    "project",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "source_kind",
    "source_path",
    "source_image_repository",
    "source_image_tag",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "build_mode",
    "runner",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
}
PLAN_FAILURE_FIELDS = {
    "reason_code",
    "source_kind",
    "source_id",
    "builder_plan_id",
    "runtime_id",
    "evidence",
}
RESULT_FIELDS = {
    "builder_plan_id",
    "status",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "digest_readback",
    "evidence",
}
BUILDER_FACT_FIELDS = {
    "builder_fact_id",
    "project",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "source_kind",
    "source_path",
    "source_image_repository",
    "source_image_tag",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
}
COLLECTED_FAILURE_FIELDS = {
    "builder_plan_id",
    "status",
    "reason_code",
    "source_kind",
    "source_id",
    "target_repository",
    "target_tag",
    "target_builder_digest",
    "digest_readback",
    "evidence",
}
PROBE_MATRIX_ROW_FIELDS = {
    "id",
    "builder_fact_id",
    "builder_image",
    "target_builder_digest",
    "runner",
    "cpu_architecture",
}
COLLECTED_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "builder_sync",
    "builder_facts",
    "failures",
    "python_probe_matrix",
}


def _load_fixture() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_public_callable(name: str) -> Callable[..., dict[str, Any]]:
    function = getattr(builders, name, None)
    message = f"required public API ucm_release.builders.{name} is missing"
    assert callable(function), message
    return function


def _source_builder_discovery(fixture: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(fixture["builder_discovery"])
    source.pop("builder_sync")
    source.pop("builder_facts")
    source.pop("failures")
    return source


def _plan_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    function = _require_public_callable("plan_builder_facts")
    original = copy.deepcopy(fixture)
    plan = function(
        _source_builder_discovery(fixture),
        fixture["runtime_discovery"],
        fixture["mooncake_probes"],
    )
    assert fixture == original
    assert isinstance(plan, dict)
    return plan


def _result_fixture(
    fixture: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    facts_by_target = {
        (item["target_repository"], item["target_tag"]): item
        for item in fixture["builder_discovery"]["builder_facts"]
    }
    failure_by_target = {
        (item["target_repository"], item["target_tag"]): item
        for item in fixture["builder_discovery"]["failures"]
    }
    results = []
    for item in plan["builder_plans"]:
        target = (item["target_repository"], item["target_tag"])
        fact = facts_by_target.get(target)
        if fact is not None:
            status = "built" if item["target_tag"].endswith("-r2") else "existing"
            results.append(
                {
                    "builder_plan_id": item["builder_plan_id"],
                    "status": status,
                    "target_repository": item["target_repository"],
                    "target_tag": item["target_tag"],
                    "target_builder_digest": fact["target_builder_digest"],
                    "digest_readback": True,
                    "evidence": {"source": "registry-readback"},
                }
            )
            continue
        failure = failure_by_target[target]
        results.append(
            {field: copy.deepcopy(failure[field]) for field in RESULT_FIELDS}
        )
    return {
        "kind": "ucm-builder-results",
        "schema_version": 3,
        "results": sorted(results, key=lambda item: item["builder_plan_id"]),
    }


def _collect_fixture(
    fixture: dict[str, Any],
    plan: dict[str, Any],
    results: dict[str, Any],
) -> dict[str, Any]:
    function = _require_public_callable("collect_builder_facts")
    original_plan = copy.deepcopy(plan)
    original_results = copy.deepcopy(results)
    collected = function(plan, results)
    assert plan == original_plan
    assert results == original_results
    assert isinstance(collected, dict)
    return collected


def _runtime_id(value: dict[str, Any]) -> str:
    return _canonical_digest(
        {
            field: value[field]
            for field in (
                "product_id",
                "runtime_image_repository",
                "runtime_image_tag",
                "variant",
                "cpu_architecture",
            )
        }
    )


def test_builder_fact_flow_public_apis_exist() -> None:
    assert _require_public_callable("plan_builder_facts")
    assert _require_public_callable("collect_builder_facts")


def test_plan_builder_facts_is_closed_canonical_and_abi_independent() -> None:
    fixture = _load_fixture()
    plan = _plan_fixture(fixture)

    assert set(plan) == PLAN_FIELDS
    assert plan["kind"] == "ucm-builder-fact-plan"
    assert plan["schema_version"] == 3
    assert plan["source_sha"] == fixture["builder_discovery"]["source_sha"]
    plans = plan["builder_plans"]
    assert plans == sorted(plans, key=lambda item: item["builder_plan_id"])
    expected_targets = {
        (item["target_repository"], item["target_tag"])
        for item in fixture["builder_discovery"]["builders"]
        if item["variant"] != "310p"
    }
    assert {
        (item["target_repository"], item["target_tag"]) for item in plans
    } == expected_targets

    runtimes = {
        _runtime_id(item): item
        for item in fixture["runtime_discovery"]["runtime_candidates"]
    }
    probes = {
        (item["runtime_image_digest"], item["cpu_architecture"]): item
        for item in fixture["mooncake_probes"]["probes"]
    }
    expected_fact_by_target = {
        (item["target_repository"], item["target_tag"]): item
        for item in fixture["builder_discovery"]["builder_facts"]
    }
    for item in plans:
        assert set(item) == BUILDER_PLAN_FIELDS
        identity = {field: item[field] for field in PLAN_IDENTITY_FIELDS}
        assert item["builder_plan_id"] == _canonical_digest(identity)
        assert "target_builder_digest" not in item
        assert "builder_fact_id" not in item
        assert "builder_revision_id" not in item
        assert "python_abi" not in item
        if item["accelerator"] == "cuda":
            assert item["mooncake_source_runtime_id"] is None
            assert item["mooncake_source_runtime_image"] is None
            assert item["mooncake_version"] is None
        else:
            runtime = runtimes[item["mooncake_source_runtime_id"]]
            probe = probes[
                (runtime["runtime_image_digest"], item["cpu_architecture"])
            ]
            assert item["mooncake_source_runtime_image"] == (
                f'{runtime["runtime_image_repository"]}@'
                f'{runtime["runtime_image_digest"]}'
            )
            assert probe["declared_version"] == item["mooncake_version"]
            assert probe["installed_version"] == item["mooncake_version"]
        expected_fact = expected_fact_by_target.get(
            (item["target_repository"], item["target_tag"])
        )
        if expected_fact is not None:
            for field in (
                "mooncake_source_runtime_id",
                "mooncake_source_runtime_image",
                "mooncake_version",
            ):
                assert item[field] == expected_fact[field]

    matrix = plan["matrix"]
    assert set(matrix) == {"include"}
    assert {
        item["builder_plan_id"] for item in matrix["include"]
    } == {item["builder_plan_id"] for item in plans}
    assert all(
        item["id"] == item["builder_plan_id"] for item in matrix["include"]
    )


def test_plan_builder_facts_keeps_mooncake_failure_local() -> None:
    fixture = _load_fixture()
    plan = _plan_fixture(fixture)
    mismatched_runtime = next(
        item
        for item in fixture["runtime_discovery"]["runtime_candidates"]
        if item["runtime_image_tag"] == "v0.10.0-a4"
    )
    failures = [
        item
        for item in plan["failures"]
        if item["reason_code"] == "mooncake-version-mismatch"
    ]

    assert failures
    assert all(set(item) == PLAN_FAILURE_FIELDS for item in failures)
    mismatch = next(
        item
        for item in failures
        if item["runtime_id"] == _runtime_id(mismatched_runtime)
    )
    assert mismatch["evidence"] == {
        "declared_version": "0.3.12",
        "installed_version": "0.3.11.post1",
    }
    assert any(
        item["target_tag"] == "cann9.1-a4-cp-all-manylinux2_34-arm64-r1"
        and item["mooncake_source_runtime_id"]
        != mismatch["runtime_id"]
        for item in plan["builder_plans"]
    )
    assert any(item["accelerator"] == "cuda" for item in plan["builder_plans"])


def _unknown_mooncake_probe_field(fixture: dict[str, Any]) -> None:
    fixture["mooncake_probes"]["probes"][0]["future_field"] = "unexpected"


def _conflicting_mooncake_probe(fixture: dict[str, Any]) -> None:
    conflict = copy.deepcopy(fixture["mooncake_probes"]["probes"][0])
    conflict["installed_version"] = "0.3.12"
    fixture["mooncake_probes"]["probes"].append(conflict)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            _unknown_mooncake_probe_field,
            id="unknown-mooncake-probe-field",
        ),
        pytest.param(
            _conflicting_mooncake_probe,
            id="conflicting-mooncake-probe",
        ),
    ],
)
def test_plan_builder_facts_rejects_ambiguous_probe_inputs(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    fixture = _load_fixture()
    baseline = _plan_fixture(fixture)
    assert baseline["builder_plans"]
    mutation(fixture)

    with pytest.raises(ValueError):
        _plan_fixture(fixture)


def test_collect_builder_facts_is_closed_and_matches_catalog_fixture() -> None:
    fixture = _load_fixture()
    plan = _plan_fixture(fixture)
    results = _result_fixture(fixture, plan)
    collected = _collect_fixture(fixture, plan, results)

    assert set(collected) == COLLECTED_FIELDS
    assert collected["kind"] == "ucm-collected-builder-facts"
    assert collected["schema_version"] == 3
    assert collected["source_sha"] == plan["source_sha"]
    assert collected["builder_sync"] == {
        "mode": "append-only",
        "target_digests_verified": True,
        "deletions": [],
    }
    assert collected["builder_facts"] == fixture["builder_discovery"][
        "builder_facts"
    ]
    assert collected["failures"] == fixture["builder_discovery"]["failures"]
    assert all(
        set(item) == BUILDER_FACT_FIELDS for item in collected["builder_facts"]
    )
    assert all(
        set(item) == COLLECTED_FAILURE_FIELDS for item in collected["failures"]
    )

    matrix = collected["python_probe_matrix"]
    assert set(matrix) == {"include"}
    rows = matrix["include"]
    assert all(set(item) == PROBE_MATRIX_ROW_FIELDS for item in rows)
    assert {item["builder_fact_id"] for item in rows} == {
        item["builder_fact_id"] for item in collected["builder_facts"]
    }
    facts = {
        item["builder_fact_id"]: item for item in collected["builder_facts"]
    }
    for row in rows:
        fact = facts[row["builder_fact_id"]]
        assert row["id"] == row["builder_fact_id"]
        assert row["builder_image"] == (
            f'{fact["target_repository"]}@{fact["target_builder_digest"]}'
        )
        assert row["target_builder_digest"] == fact["target_builder_digest"]
        assert row["cpu_architecture"] == fact["cpu_architecture"]


@pytest.mark.parametrize(
    ("status", "expected_collection"),
    [
        pytest.param("existing", "builder_facts", id="zero-new-existing"),
        pytest.param("built", "builder_facts", id="same-run-built"),
        pytest.param("failed", "failures", id="failed-new-local"),
    ],
)
def test_collect_builder_facts_routes_result_status_locally(
    status: str,
    expected_collection: str,
) -> None:
    fixture = _load_fixture()
    plan = _plan_fixture(fixture)
    results = _result_fixture(fixture, plan)
    collected = _collect_fixture(fixture, plan, results)
    result = next(item for item in results["results"] if item["status"] == status)
    output = collected[expected_collection]

    assert any(
        item.get("builder_plan_id") == result["builder_plan_id"]
        or item.get("target_tag") == result["target_tag"]
        for item in output
    )
    if status == "failed":
        assert result["digest_readback"] is False
        assert all(
            item["target_tag"] != result["target_tag"]
            for item in collected["builder_facts"]
        )
        assert collected["builder_facts"]
        assert collected["python_probe_matrix"]["include"]
    else:
        assert result["digest_readback"] is True
        assert any(
            item["target_tag"] == result["target_tag"]
            for item in collected["builder_facts"]
        )


def _missing_result(results: dict[str, Any]) -> None:
    results["results"].pop()


def _unexpected_result(results: dict[str, Any]) -> None:
    unexpected = copy.deepcopy(results["results"][0])
    unexpected["builder_plan_id"] = "sha256:" + "f" * 64
    results["results"].append(unexpected)


def _duplicate_result(results: dict[str, Any]) -> None:
    results["results"].append(copy.deepcopy(results["results"][0]))


def _unresolved_existing_result(results: dict[str, Any]) -> None:
    result = next(item for item in results["results"] if item["status"] == "existing")
    result["target_builder_digest"] = None
    result["digest_readback"] = False


def _unresolved_built_result(results: dict[str, Any]) -> None:
    result = next(item for item in results["results"] if item["status"] == "built")
    result["digest_readback"] = False


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_missing_result, id="missing-result-id"),
        pytest.param(_unexpected_result, id="unexpected-result-id"),
        pytest.param(_duplicate_result, id="duplicate-result-id"),
        pytest.param(_unresolved_existing_result, id="unresolved-existing"),
        pytest.param(_unresolved_built_result, id="unresolved-built"),
    ],
)
def test_collect_builder_facts_rejects_incomplete_or_unsafe_results(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    fixture = _load_fixture()
    plan = _plan_fixture(fixture)
    results = _result_fixture(fixture, plan)
    baseline = _collect_fixture(fixture, plan, results)
    assert baseline["builder_facts"]
    mutation(results)

    with pytest.raises(ValueError):
        _collect_fixture(fixture, plan, results)
