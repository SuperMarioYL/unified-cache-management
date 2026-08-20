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
from unittest import mock

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
builders = importlib.import_module("ucm_release.builders")


def _registry_fixture() -> dict[str, object]:
    return json.loads(
        (RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json").read_text(
            encoding="utf-8"
        )
    )


def _builder_catalog() -> dict[str, object]:
    return builders.discover_builders(
        RELEASE_ROOT / "builders.yaml",
        snapshot_dir=RELEASE_ROOT / "tests" / "fixtures" / "builders",
        owner="release-org",
    )


def _resolved_builder_root() -> dict[str, object]:
    return {
        "index_digest": "sha256:" + "f" * 64,
        "manifest_digest": "sha256:" + "e" * 64,
        "config_digest": "sha256:" + "d" * 64,
        "operations": [],
    }


def _resolved_catalog(catalog: dict[str, object]) -> dict[str, object]:
    selection = builders.select_builders(_builder_catalog(), catalog)
    bound = builders.bind_selection(catalog, selection)
    root = _resolved_builder_root()
    for profile in bound["wheel_profiles"]:
        for requirement in profile["builders"].values():
            unresolved = requirement["root"]
            requirement["root"] = {
                "repository": unresolved["repository"],
                "tag": unresolved["tag"],
                "index_digest": root["index_digest"],
                "manifest_digest": root["manifest_digest"],
                "config_digest": root["config_digest"],
            }
    return bound


def _resolve_fixture(catalog: dict[str, object], *, source_sha: str):
    with mock.patch.object(
        registry, "resolve_builder_root", return_value=_resolved_builder_root()
    ):
        return registry.resolve_catalog(
            catalog,
            builder_catalog=_builder_catalog(),
            source_sha=source_sha,
            lane="feature-candidate",
            fixture=_registry_fixture(),
        )


def test_registry_resolves_exactly_the_six_selected_builder_refs(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def resolve_builder(repository: str, tag: str, *, architecture: str):
        calls.append((repository, tag, architecture))
        return _resolved_builder_root()

    monkeypatch.setattr(registry, "resolve_builder_root", resolve_builder)

    plan = registry.resolve_catalog(
        core.load_catalog(),
        builder_catalog=_builder_catalog(),
        source_sha="a" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    assert len(calls) == 6
    assert {repository for repository, _, _ in calls} == {
        "ghcr.io/release-org/ucm-builder-vllm",
        "ghcr.io/release-org/ucm-builder-vllm-ascend",
    }
    assert all("12.9" not in tag and "9.1.0" not in tag for _, tag, _ in calls)
    assert {architecture for _, _, architecture in calls} == {"amd64", "arm64"}
    for task in plan["wheel_tasks"]:
        assert task["builder"]["root"] == {
            "repository": task["builder"]["root"]["repository"],
            "tag": task["builder"]["root"]["tag"],
            "index_digest": "sha256:" + "f" * 64,
            "manifest_digest": "sha256:" + "e" * 64,
            "config_digest": "sha256:" + "d" * 64,
        }


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

    with mock.patch.object(
        registry, "resolve_builder_root", return_value=_resolved_builder_root()
    ):
        plan = registry.resolve_catalog(
            catalog,
            builder_catalog=_builder_catalog(),
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
        _resolve_fixture(catalog, source_sha="1" * 40)

    catalog["scan_limits"]["max_selected_upstreams"] = 8
    catalog["matrix_limits"]["max_family_tasks"] = 2
    with pytest.raises(ValueError, match="max_family_tasks"):
        _resolve_fixture(catalog, source_sha="1" * 40)


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
    fake_config = json.dumps(
        {
            "config": {
                "Env": [
                    "ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-9.0.0",
                    "SOC_VERSION=ascend910_9391",
                    "PATH=/usr/local/python3.12.13/bin",
                ]
            }
        }
    )
    monkeypatch.setattr(registry, "_crane", lambda *_a, **_k: fake_config)
    variant, err = registry._inspect_upstream_variant(
        "crane", "quay.io/ascend/vllm-ascend", "sha256:dead", ascend
    )
    assert variant == "a3"
    assert err is None


def test_inspect_upstream_variant_cuda_default(monkeypatch) -> None:
    catalog = _catalog_with_soc_versions()
    vllm = next(p for p in catalog["upstream_products"] if p["id"] == "vllm")
    # cuda image: no ASCEND env, single-variant product -> default
    fake_config = json.dumps(
        {"config": {"Env": ["CUDA_VERSION=13.0.2", "NVARCH=x86_64"]}}
    )
    monkeypatch.setattr(registry, "_crane", lambda *_a, **_k: fake_config)
    variant, err = registry._inspect_upstream_variant(
        "crane", "docker.io/vllm/vllm-openai", "sha256:beef", vllm
    )
    assert variant == "default"
    assert err is None


def test_inspect_upstream_variant_ascend_missing_soc(monkeypatch) -> None:
    catalog = _catalog_with_soc_versions()
    ascend = next(p for p in catalog["upstream_products"] if p["id"] == "vllm-ascend")
    fake_config = json.dumps(
        {"config": {"Env": ["ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-9.0.0"]}}
    )
    monkeypatch.setattr(registry, "_crane", lambda *_a, **_k: fake_config)
    variant, err = registry._inspect_upstream_variant(
        "crane", "quay.io/ascend/vllm-ascend", "sha256:cafe", ascend
    )
    assert variant is None
    assert err is not None


# -- relaxed (PR/pinned) plan construction -----------------------------------


def _catalog_for_plan() -> dict:
    return core.load_catalog(
        RELEASE_ROOT / "release.yaml", RELEASE_ROOT / "schemas", repository_root=ROOT
    )


def _ascend_a3_snapshot(catalog: dict, version: str, tag: str) -> dict:
    ascend = next(p for p in catalog["upstream_products"] if p["id"] == "vllm-ascend")
    fake = "sha256:" + "0" * 64
    members = {
        arch: {"manifest_digest": fake, "config_digest": fake}
        for arch in ascend["required_cpu_architectures"]
    }
    return {
        "product_id": "vllm-ascend",
        "repository": ascend["repository"],
        "tag": tag,
        "version": version,
        "channel": "rc",
        "variant": "a3",
        "index_digest": fake,
        "members": members,
        "target_repository": ascend["target_repository"],
        "target_tag": tag + ascend["target_tag_suffix"],
    }


def test_release_plan_relaxed_accepts_out_of_specifier_tag() -> None:
    catalog = _resolved_catalog(_catalog_for_plan())
    snapshot = _ascend_a3_snapshot(
        catalog, "0.23.0", "v0.23.0-a3"
    )  # 0.23.0 is outside >=0.22.1rc1,<0.23
    plan = core.ReleasePlan.build(
        catalog,
        [snapshot],
        lane="feature-candidate",
        relaxed=True,
        repository_root=ROOT,
    )
    assert plan.wheel_tasks and plan.image_tasks
    with pytest.raises(ValueError):
        core.ReleasePlan.build(
            catalog, [snapshot], lane="feature-candidate", repository_root=ROOT
        )


def test_release_plan_rejects_unbound_and_unresolved_builder_requirements() -> None:
    catalog = _catalog_for_plan()
    snapshot = _ascend_a3_snapshot(catalog, "0.22.1rc1", "v0.22.1rc1-a3")

    with pytest.raises(ValueError, match="builder root must be resolved"):
        core.ReleasePlan.build(
            catalog,
            [snapshot],
            lane="feature-candidate",
            repository_root=ROOT,
        )

    bound = builders.bind_selection(
        catalog, builders.select_builders(_builder_catalog(), catalog)
    )
    core.validate_catalog(bound, repository_root=ROOT)
    with pytest.raises(ValueError, match="builder root must be resolved"):
        core.ReleasePlan.build(
            bound,
            [snapshot],
            lane="feature-candidate",
            repository_root=ROOT,
        )


def test_release_plan_relaxed_rejects_non_pep440_version() -> None:
    # Non-pep440 versions (e.g. mutable 'latest') can't disambiguate a
    # version-gated runtime-patch rule for ascend -> rejected. (cuda 'latest'
    # is fine in practice because its version comes from the image label.)
    catalog = _catalog_for_plan()
    snapshot = _ascend_a3_snapshot(catalog, "latest", "latest")
    with pytest.raises(ValueError):
        core.ReleasePlan.build(
            catalog,
            [snapshot],
            lane="feature-candidate",
            relaxed=True,
            repository_root=ROOT,
        )


def test_resolve_pinned_upstreams_inspect_variant(monkeypatch) -> None:
    catalog = _resolved_catalog(_catalog_for_plan())
    products_by_repo = {p["repository"]: p for p in catalog["upstream_products"]}
    fake = "sha256:" + "0" * 64

    def fake_resolve(repository, upstream_tag, required_architectures):
        return {
            "operations": [
                {
                    "type": "crane-digest",
                    "capability": "read",
                    "reference": f"{repository}:{upstream_tag}",
                }
            ],
            "snapshot": {
                "repository": repository,
                "tag": upstream_tag,
                "index_digest": fake,
                "members": {
                    a: {"manifest_digest": fake, "config_digest": fake}
                    for a in required_architectures
                },
            },
        }

    monkeypatch.setattr(registry, "resolve_repository_tag", fake_resolve)
    monkeypatch.setattr(
        registry,
        "_inspect_upstream_variant",
        lambda crane, repo, digest, product: (
            ("a3", None) if product["id"] == "vllm-ascend" else ("default", None)
        ),
    )

    operations: list = []
    ru = registry._resolve_pinned_upstreams(
        catalog,
        ["quay.io/ascend/vllm-ascend:v0.23.0-a3"],
        products_by_repo,
        "crane",
        operations,
    )
    assert len(ru) == 1
    snap = ru[0]
    assert snap["variant"] == "a3"  # inspect-determined, not the tag suffix
    assert snap["version"] == "0.23.0"  # grammar-extracted (v0.23.0-a3)
    assert snap["channel"] == "stable"  # 0.23.0 is not a prerelease
    assert any(op["type"] == "crane-config" for op in operations)
    # full relaxed plan builds (0.23.0 is outside the product specifier)
    plan = core.ReleasePlan.build(
        catalog, ru, lane="feature-candidate", relaxed=True, repository_root=ROOT
    )
    assert plan.image_tasks and plan.wheel_tasks
