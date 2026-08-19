"""Matrix expansion and compatibility filtering contract for the planner.

Only pure input/output tests of the planner are retained: matrix expansion
(arch-bound family control tasks and matrix-overflow fail-closed) and
compatibility-rule ambiguity validation (overlapping selectors, semantic
range overlap, public/local exact overlap, disjoint dimensions).  The
frozen-plan authority, real-image-task, and CLI projection change-detector
suites were removed per the slimming plan.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

core = importlib.import_module("ucm_release.core")
registry = importlib.import_module("ucm_release.registry")


def _registry_fixture() -> dict[str, object]:
    return json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )


def test_arm64_only_family_binds_control_runner_and_tool_arch_in_plan() -> None:
    """An arch-restricted family binds its control task, runner, and arch."""
    catalog = core.load_catalog()
    catalog["upstream_products"] = [
        product for product in catalog["upstream_products"] if product["id"] == "vllm"
    ]
    catalog["upstream_products"][0]["required_cpu_architectures"] = ["arm64"]
    catalog["compatibility"]["rules"] = [
        rule
        for rule in catalog["compatibility"]["rules"]
        if rule["accelerator"] == "cuda"
    ]
    catalog["compatibility"]["rules"][0]["cpu_architectures"] = ["arm64"]
    catalog["runtime_patch_rules"] = [
        rule for rule in catalog["runtime_patch_rules"] if rule["product"] == "vllm"
    ]
    catalog["chart"]["validation_cases"] = [
        case
        for case in catalog["chart"]["validation_cases"]
        if case["product_id"] == "vllm"
    ]
    catalog["pr_smoke"]["image_selectors"] = [
        {"product_id": "vllm", "variant": "default", "cpu_arch": "arm64"}
    ]
    fixture = _registry_fixture()
    fixture["repositories"] = {
        "docker.io/vllm/vllm-openai": fixture["repositories"][
            "docker.io/vllm/vllm-openai"
        ]
    }
    selected_snapshot = fixture["repositories"]["docker.io/vllm/vllm-openai"][
        "snapshots"
    ]["v0.21.0"]
    selected_snapshot["platforms"] = [
        member
        for member in selected_snapshot["platforms"]
        if member["architecture"] == "arm64"
    ]

    plan = registry.resolve_catalog(
        catalog,
        source_sha="b" * 40,
        lane="feature-candidate",
        fixture=fixture,
    )

    assert len(plan["image_tasks"]) == len(plan["family_tasks"]) == 1
    image_task = plan["image_tasks"][0]
    family_task = plan["family_tasks"][0]
    matrix = plan["github_family_matrix"]["include"][0]
    assert family_task["control_task_id"] == image_task["task_id"]
    assert family_task["control_arch"] == "arm64"
    assert family_task["control_runner"] == image_task["runner"]
    assert matrix == {
        "task_id": family_task["task_id"],
        "family_task_id": family_task["task_id"],
        "runner": image_task["runner"],
        "control_task_id": image_task["task_id"],
        "control_arch": "arm64",
    }


def test_scan_and_matrix_overflow_fail_without_truncation() -> None:
    """Selecting or generating more than the configured max must fail closed."""
    resolver = registry
    catalog = core.load_catalog()
    catalog["scan_limits"]["max_selected_upstreams"] = 2
    with pytest.raises(ValueError, match="max_selected_upstreams"):
        resolver.resolve_catalog(
            catalog,
            source_sha="1" * 40,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )

    catalog["scan_limits"]["max_selected_upstreams"] = 8
    catalog["matrix_limits"]["max_family_tasks"] = 2
    with pytest.raises(ValueError, match="max_family_tasks"):
        resolver.resolve_catalog(
            catalog,
            source_sha="1" * 40,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )


def test_v2_catalog_still_rejects_overlapping_compatibility_rules(
    tmp_path: Path,
) -> None:
    """Deleting the adapter must not weaken v2 rule ambiguity validation."""
    catalog = core.load_catalog()
    duplicate = copy.deepcopy(catalog["compatibility"]["rules"][0])
    duplicate["id"] = "overlapping-copy"
    catalog["compatibility"]["rules"].append(duplicate)
    catalog_path = tmp_path / "release.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="overlap"):
        core.load_catalog(catalog_path)


def test_v2_catalog_rejects_semantic_range_overlap_without_a_current_target() -> None:
    """Future targets cannot make two previously accepted rule selectors ambiguous."""
    catalog = core.load_catalog()
    product = next(
        item for item in catalog["upstream_products"] if item["id"] == "vllm"
    )
    product["version_specifier"] = ">=0.30,<0.31"
    broad = next(
        item
        for item in catalog["compatibility"]["rules"]
        if item["id"] == "cuda-supported"
    )
    broad["version_specifier"] = ">=0.18,<0.23"
    subset = copy.deepcopy(broad)
    subset.update(
        {
            "id": "cuda-021-subset",
            "version_specifier": "==0.21.*",
            "cpu_architectures": ["amd64"],
            "upstream_channels": ["stable", "rc"],
        }
    )
    catalog["compatibility"]["rules"].append(subset)

    with pytest.raises(ValueError, match="semantic.*overlap|overlap.*semantic"):
        core.validate_catalog(catalog)


def _catalog_with_cuda_version_rules(
    left_specifier: str, right_specifier: str
) -> dict[str, object]:
    catalog = core.load_catalog()
    left = next(
        item
        for item in catalog["compatibility"]["rules"]
        if item["id"] == "cuda-supported"
    )
    left["version_specifier"] = left_specifier
    right = copy.deepcopy(left)
    right.update(
        {
            "id": "cuda-version-peer",
            "version_specifier": right_specifier,
        }
    )
    catalog["compatibility"]["rules"].append(right)
    return catalog


def test_v2_catalog_rejects_public_exact_and_local_exact_overlap() -> None:
    """A public equality includes local builds of the same public version."""
    witness = Version("1.0+foo")
    assert SpecifierSet("==1.0").contains(witness, prereleases=True)
    assert SpecifierSet("==1.0+foo").contains(witness, prereleases=True)

    with pytest.raises(ValueError, match="semantic.*overlap|overlap.*semantic"):
        core.validate_catalog(_catalog_with_cuda_version_rules("==1.0", "==1.0+foo"))


def test_v2_catalog_rejects_local_exact_at_inclusive_public_upper_bound() -> None:
    """Inclusive public bounds include a local build at that boundary."""
    witness = Version("1.0+foo")
    assert SpecifierSet("==1.0+foo").contains(witness, prereleases=True)
    assert SpecifierSet("<=1.0").contains(witness, prereleases=True)

    with pytest.raises(ValueError, match="semantic.*overlap|overlap.*semantic"):
        core.validate_catalog(_catalog_with_cuda_version_rules("==1.0+foo", "<=1.0"))


@pytest.mark.parametrize(
    ("left_specifier", "right_specifier"),
    [
        ("==1.0+foo", "==1.0+bar"),
        ("==1.0", "==2.0"),
    ],
)
def test_v2_catalog_allows_disjoint_exact_local_versions(
    left_specifier: str,
    right_specifier: str,
) -> None:
    """Distinct exact versions remain provably disjoint under PEP 440."""
    core.validate_catalog(
        _catalog_with_cuda_version_rules(left_specifier, right_specifier)
    )


@pytest.mark.parametrize(
    "dimension",
    ["version", "channel", "variant", "cpu-architecture"],
)
def test_v2_catalog_allows_compatibility_rules_with_a_disjoint_dimension(
    dimension: str,
) -> None:
    """Two rules remain unambiguous when at least one selector cannot intersect."""
    catalog = core.load_catalog()
    if dimension == "variant":
        rule = next(
            item
            for item in catalog["compatibility"]["rules"]
            if item["id"] == "ascend-supported"
        )
        rule["variants"] = ["a2"]
        other = copy.deepcopy(rule)
        other.update({"id": "ascend-a3-only", "variants": ["a3"]})
    else:
        rule = next(
            item
            for item in catalog["compatibility"]["rules"]
            if item["id"] == "cuda-supported"
        )
        other = copy.deepcopy(rule)
        other["id"] = f"cuda-disjoint-{dimension}"
        if dimension == "version":
            rule["version_specifier"] = ">=0.18,<0.21"
            other["version_specifier"] = ">=0.21,<0.23"
        elif dimension == "channel":
            rule["upstream_channels"] = ["stable"]
            other["upstream_channels"] = ["rc"]
        else:
            rule["cpu_architectures"] = ["amd64"]
            other["cpu_architectures"] = ["arm64"]
    catalog["compatibility"]["rules"].append(other)

    core.validate_catalog(catalog)


# -- inspect-based variant detection (soc_versions) -------------------------

def _catalog_with_soc_versions() -> dict:
    return core.load_catalog(RELEASE_ROOT / "release.yaml", RELEASE_ROOT / "schemas")


def test_variant_by_soc_matches_ascend_variants() -> None:
    catalog = _catalog_with_soc_versions()
    ascend = next(p for p in catalog["upstream_products"] if p["id"] == "vllm-ascend")
    assert registry._variant_by_soc(ascend, "ascend910b1") == "a2"
    assert registry._variant_by_soc(ascend, "ascend910_9391") == "a3"
    assert registry._variant_by_soc(ascend, "ascend910b3") is None


def test_inspect_upstream_variant_ascend(monkeypatch) -> None:
    catalog = _catalog_with_soc_versions()
    ascend = next(p for p in catalog["upstream_products"] if p["id"] == "vllm-ascend")
    fake_config = json.dumps({"config": {"Env": [
        "ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-9.0.0",
        "SOC_VERSION=ascend910_9391",
        "PATH=/usr/local/python3.12.13/bin",
    ]}})
    monkeypatch.setattr(registry, "_crane", lambda *_a, **_k: fake_config)
    variant, err = registry._inspect_upstream_variant("crane", "quay.io/ascend/vllm-ascend", "sha256:dead", ascend)
    assert variant == "a3"
    assert err is None


def test_inspect_upstream_variant_cuda_default(monkeypatch) -> None:
    catalog = _catalog_with_soc_versions()
    vllm = next(p for p in catalog["upstream_products"] if p["id"] == "vllm")
    # cuda image: no ASCEND env, single-variant product -> default
    fake_config = json.dumps({"config": {"Env": ["CUDA_VERSION=13.0.2", "NVARCH=x86_64"]}})
    monkeypatch.setattr(registry, "_crane", lambda *_a, **_k: fake_config)
    variant, err = registry._inspect_upstream_variant("crane", "docker.io/vllm/vllm-openai", "sha256:beef", vllm)
    assert variant == "default"
    assert err is None


def test_inspect_upstream_variant_ascend_missing_soc(monkeypatch) -> None:
    catalog = _catalog_with_soc_versions()
    ascend = next(p for p in catalog["upstream_products"] if p["id"] == "vllm-ascend")
    fake_config = json.dumps({"config": {"Env": ["ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-9.0.0"]}})
    monkeypatch.setattr(registry, "_crane", lambda *_a, **_k: fake_config)
    variant, err = registry._inspect_upstream_variant("crane", "quay.io/ascend/vllm-ascend", "sha256:cafe", ascend)
    assert variant is None
    assert err is not None
