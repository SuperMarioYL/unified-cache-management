"""Functional contract for the compact release plan."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

builders = importlib.import_module("ucm_release.builders")
compact = importlib.import_module("ucm_release.compact")
core = importlib.import_module("ucm_release.core")


def _builder_catalog() -> dict[str, object]:
    return builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=RELEASE_ROOT / "tests" / "fixtures" / "builders",
        owner="release-org",
    )


def _tag_lists() -> dict[str, list[str]]:
    fixture = json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        repository: sorted(payload["snapshots"])
        for repository, payload in fixture["repositories"].items()
    }


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_compact_plan_uses_stable_readable_build_matrices() -> None:
    catalog = core.load_catalog(version_override="0.7.59rc3")

    plan = compact.resolve_plan(
        catalog,
        builder_catalog=_builder_catalog(),
        route="release",
        tag_lists=_tag_lists(),
    )

    assert [item["id"] for item in plan["wheels"]] == [
        "cann900-a2-amd64",
        "cann900-a2-arm64",
        "cann900-a3-amd64",
        "cann900-a3-arm64",
        "cuda130-amd64",
        "cuda130-arm64",
    ]
    assert [item["id"] for item in plan["images"]] == [
        "vllm-ascend-a2-amd64",
        "vllm-ascend-a2-arm64",
        "vllm-ascend-a3-amd64",
        "vllm-ascend-a3-arm64",
        "vllm-default-amd64",
        "vllm-default-arm64",
    ]
    assert plan["wheel_matrix"]["include"][0] == {
        "id": "cann900-a2-amd64",
        "label": "CANN 9.0 A2 · amd64",
        "runner": "ubuntu-24.04",
    }
    assert plan["image_matrix"]["include"][0] == {
        "id": "vllm-ascend-a2-amd64",
        "label": "vLLM Ascend · CANN 9.0 A2 · amd64",
        "runner": "ubuntu-24.04",
        "wheel_id": "cann900-a2-amd64",
    }


def test_compact_plan_contains_no_audit_or_digest_fields() -> None:
    plan = compact.resolve_plan(
        core.load_catalog(version_override="0.7.59rc3"),
        builder_catalog=_builder_catalog(),
        route="release",
        tag_lists=_tag_lists(),
    )

    keys = _all_keys(plan)
    assert not {
        key
        for key in keys
        if "sha" in key
        or "digest" in key
        or "authority" in key
        or "evidence" in key
        or "seal" in key
    }
    assert all("@sha256:" not in json.dumps(item) for item in plan["wheels"])
    assert all("@sha256:" not in json.dumps(item) for item in plan["images"])


def test_compact_plan_keeps_release_yaml_channel_switches() -> None:
    catalog = core.load_catalog(version_override="0.7.59rc3")

    plan = compact.resolve_plan(
        catalog,
        builder_catalog=_builder_catalog(),
        route="daily",
        tag_lists=_tag_lists(),
    )

    assert plan["publish"] == catalog["publish"]
    assert plan["route"] == "daily"
    assert plan["version"] == "0.7.59rc3"


def test_compact_plan_can_build_only_a_requested_upstream_family() -> None:
    plan = compact.resolve_plan(
        core.load_catalog(version_override="0.7.59rc3"),
        builder_catalog=_builder_catalog(),
        route="pr",
        pinned_upstreams=["quay.io/ascend/vllm-ascend:v0.22.1rc1-a3"],
    )

    assert [item["id"] for item in plan["images"]] == [
        "vllm-ascend-a3-amd64",
        "vllm-ascend-a3-arm64",
    ]
    assert [item["id"] for item in plan["wheels"]] == [
        "cann900-a3-amd64",
        "cann900-a3-arm64",
    ]


def test_missing_builder_matrix_has_stable_ids_and_readable_labels() -> None:
    catalog = _builder_catalog()

    result = builders.compute_sync_plan(catalog, {})

    first = result["matrix"]["include"][0]
    assert set(first) == set(catalog["builders"][0]) | {"id", "label"}
    assert first["id"] == first["target_tag"]
    assert " · " in first["label"]
    assert first["cpu_arch"] in first["label"]


def test_compact_wheel_environment_controls_distribution_and_version(
    tmp_path: Path,
) -> None:
    shutil.copyfile(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    compact.prepare_wheel_source(tmp_path, "uc-manager-cuda")
    assert 'name = "uc-manager-cuda"' in (tmp_path / "pyproject.toml").read_text()

    env = {
        key: value
        for key, value in os.environ.items()
        if key != "UCM_BUILD_CONFIG" and not key.startswith("UCM_RELEASE_")
    }
    env.update(
        {
            "PLATFORM": "cuda",
            "UCM_BUILD_VERSION": "0.7.59rc3",
            "UCM_DIST_NAME": "uc-manager-cuda",
            "UCM_RUNTIME_REQUIREMENTS": '["packaging==24.2","wrapt==1.17.2"]',
        }
    )

    result = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.splitlines()[-1] == "0.7.59rc3"
