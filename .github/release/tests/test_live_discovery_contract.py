"""RED contracts for typed live Builder and runtime discovery."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
FIXTURE_PATH = RELEASE_ROOT / "tests" / "fixtures" / "capability-catalog-discovery.json"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
capabilities = importlib.import_module("ucm_release.capabilities")

BUILDER_DISCOVERY_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "upstream_reads",
    "builders",
}
UPSTREAM_READ_FIELDS = {
    "project",
    "source_kind",
    "source_path",
    "source_commit",
    "fact",
}
RUNTIME_DISCOVERY_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "upstream_reads",
    "runtime_candidates",
    "runtime_probe_matrix",
}
RUNTIME_PROBE_MATRIX_ROW_FIELDS = {
    "id",
    "runtime_id",
    "runtime_image",
    "runtime_image_digest",
    "runtime_dockerfile",
    "cpu_architecture",
    "runner",
}
CANONICAL_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
ARTIFACT_SAFE_MATRIX_ID = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


def _fixture() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _require(module: object, name: str) -> Callable[..., dict[str, Any]]:
    function = getattr(module, name, None)
    assert callable(function), f"required public API {name} is missing"
    return function


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _runtime_id(value: dict[str, Any]) -> str:
    return _canonical_digest(
        {
            "product_id": value["product_id"],
            "runtime_repository": value["runtime_image_repository"],
            "runtime_tag": value["runtime_image_tag"],
            "variant": value["variant"],
            "cpu_architecture": value["cpu_architecture"],
        }
    )


def _builder_source_fixture() -> dict[str, Any]:
    fixture = _fixture()["builder_discovery"]
    return {field: copy.deepcopy(fixture[field]) for field in BUILDER_DISCOVERY_FIELDS}


def _runtime_source_fixture() -> dict[str, Any]:
    return {
        "source_sha": "1" * 40,
        "upstream_reads": [
            {
                "project": "vllm-project/vllm-ascend",
                "source_kind": "runtime-dockerfile-and-annotated-tag",
                "source_path": "docker/Dockerfile.runtime.a2",
                "source_commit": "c" + "1" * 39,
                "fact": "MOONCAKE_TAG",
            }
        ],
        "candidates": [
            {
                "product_id": "vllm-ascend",
                "runtime_version": "0.9.0",
                "channel": "stable",
                "accelerator": "ascend",
                "accelerator_runtime": "cann-9.0",
                "cpu_architecture": "amd64",
                "variant_candidates": ["a2"],
                "runtime_image_repository": "quay.io/ascend/vllm-ascend",
                "runtime_image_tag": "v0.9.0-a2",
                "runtime_image_digest": "sha256:" + "1c" + "1" * 62,
                "git_ref": {
                    "tag": "v0.9.0",
                    "object_type": "tag",
                    "target_commit": "c" + "1" * 39,
                },
                "runtime_dockerfiles": [
                    {
                        "variant": "a2",
                        "source_path": "docker/Dockerfile.runtime.a2",
                        "source_commit": "c" + "1" * 39,
                        "mooncake_version": "0.3.11.post1",
                    }
                ],
            }
        ],
    }


def test_builder_source_discovery_is_closed_and_provenance_complete() -> None:
    validate = _require(builders, "validate_builder_source_discovery")
    source = _builder_source_fixture()
    original = copy.deepcopy(source)
    discovered = validate(source)

    assert source == original
    assert discovered == source
    assert set(discovered) == BUILDER_DISCOVERY_FIELDS
    assert discovered["upstream_reads"]
    assert all(
        set(item) == UPSTREAM_READ_FIELDS for item in discovered["upstream_reads"]
    )
    assert all(
        len(item["source_commit"]) == 40 for item in discovered["upstream_reads"]
    )
    assert any(item["variant"] == "310p" for item in discovered["builders"])


def test_runtime_discovery_peels_tag_and_binds_matching_variant_dockerfile() -> None:
    discover = _require(capabilities, "discover_runtime_candidates")
    builders_input = _builder_source_fixture()
    runtime_sources = _runtime_source_fixture()
    original_builders = copy.deepcopy(builders_input)
    original_sources = copy.deepcopy(runtime_sources)
    result = discover(builders_input, runtime_sources)

    assert builders_input == original_builders
    assert runtime_sources == original_sources
    assert set(result) == RUNTIME_DISCOVERY_FIELDS
    assert result["kind"] == "ucm-runtime-discovery"
    assert result["schema_version"] == 3
    assert result["upstream_reads"] == runtime_sources["upstream_reads"]
    assert len(result["runtime_candidates"]) == len(runtime_sources["candidates"])
    candidate = result["runtime_candidates"][0]
    raw = runtime_sources["candidates"][0]
    assert candidate["variant"] == "a2"
    assert candidate["git_tag"] == raw["git_ref"]["tag"]
    assert candidate["git_commit"] == raw["git_ref"]["target_commit"]
    assert candidate["git_commit"] != candidate["git_tag"]
    assert candidate["mooncake_source_path"] == (
        raw["runtime_dockerfiles"][0]["source_path"]
    )
    assert candidate["mooncake_version"] == (
        raw["runtime_dockerfiles"][0]["mooncake_version"]
    )
    assert candidate["runtime_image_digest"] == raw["runtime_image_digest"]
    assert candidate["cpu_architecture"] == raw["cpu_architecture"]
    matrix = result["runtime_probe_matrix"]
    assert set(matrix) == {"include"}
    rows = matrix["include"]
    assert rows
    assert all(set(row) == RUNTIME_PROBE_MATRIX_ROW_FIELDS for row in rows)
    candidates_by_digest = {
        item["runtime_image_digest"]: item for item in result["runtime_candidates"]
    }
    for row in rows:
        expected_runtime_id = _runtime_id(
            candidates_by_digest[row["runtime_image_digest"]]
        )
        assert CANONICAL_SHA256_ID.fullmatch(row["runtime_id"])
        assert row["runtime_id"] == expected_runtime_id
        assert row["id"] == row["runtime_id"].removeprefix("sha256:")
        assert ARTIFACT_SAFE_MATRIX_ID.fullmatch(row["id"])
        assert ":" not in row["id"]


def _missing_variant(value: dict[str, Any]) -> None:
    value["candidates"][0]["variant_candidates"] = []


def _ambiguous_variant(value: dict[str, Any]) -> None:
    value["candidates"][0]["variant_candidates"] = ["a2", "a4"]


def _missing_matching_dockerfile(value: dict[str, Any]) -> None:
    value["candidates"][0]["runtime_dockerfiles"][0]["variant"] = "a4"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_missing_variant, id="missing-runtime-variant"),
        pytest.param(_ambiguous_variant, id="ambiguous-runtime-variant"),
        pytest.param(
            _missing_matching_dockerfile,
            id="missing-matching-runtime-dockerfile",
        ),
    ],
)
def test_runtime_discovery_rejects_ambiguous_or_unbound_variant(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    discover = _require(capabilities, "discover_runtime_candidates")
    builders_input = _builder_source_fixture()
    baseline = _runtime_source_fixture()
    assert discover(copy.deepcopy(builders_input), copy.deepcopy(baseline))[
        "runtime_candidates"
    ]
    mutation(baseline)

    with pytest.raises(ValueError):
        discover(builders_input, baseline)
