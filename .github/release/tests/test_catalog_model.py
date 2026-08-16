from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

core = importlib.import_module("ucm_release.core")
registry = importlib.import_module("ucm_release.registry")
verify = importlib.import_module("ucm_release.verify")


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _registry_fixture() -> dict[str, object]:
    return core.load_json(RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json")


def _builder(character: str) -> dict[str, object]:
    return {
        "root": {
            "repository": "docker.io/example/builder",
            "tag": "locked",
            "index_digest": _digest(character),
            "manifest_digest": _digest(character),
            "config_digest": _digest(character),
        },
        "sources": [],
        "copy_paths": [],
        "checks": [
            {"kind": "python", "version": "3.12", "abi": "cp312"},
            {"kind": "python-soabi", "prefix": "cpython"},
            {"kind": "command", "name": "g++"},
            {"kind": "file", "path": "/opt/toolkit/include/runtime.h"},
        ],
    }


def _profile(
    profile_id: str,
    architectures: list[str],
    *,
    accelerator: str = "cuda",
    npu_arch: str = "na",
) -> dict[str, object]:
    return {
        "id": profile_id,
        "accelerator": accelerator,
        "accelerator_runtime": "cuda-13.0" if accelerator == "cuda" else "cann-9.0.0",
        "npu_arch": [npu_arch],
        "os": ["ubuntu-22.04"],
        "cpu_arch": architectures,
        "python_version": "3.12",
        "python_abi": "cp312",
        "wheel_version": f"0.5.0rc1+{profile_id}",
        "wheel_platform": "manylinux_2_28" if accelerator == "cuda" else "linux",
        "binary_profile_id": f"release-{profile_id}",
        "allowed_dt_needed": ["libc.so.6"],
        "external_required_dependencies": [],
        "validation_targets": [profile_id],
        "required_native": ["ucmtrans"],
        "forbidden_native": ["forbidden"],
        "build": {
            "docker_target": "wheel",
            "platform_arg": accelerator,
        },
        "builders": {
            architecture: _builder(str(index + 1))
            for index, architecture in enumerate(architectures)
        },
    }


def _catalog() -> dict[str, object]:
    return {
        "kind": "release-config",
        "schema_version": 2,
        "ucm_version": "0.5.0rc1",
        "lanes": ["feature-candidate", "protected-tag"],
        "runner_map": {"amd64": "runner-x64", "arm64": "runner-arm64"},
        "python_runtime_dependencies": [
            {
                "requirement": "wrapt==1.17.2",
                "import_name": "wrapt",
                "wheel_artifacts": {
                    "cp312": {
                        "amd64": {
                            "filename": "wrapt-cp312-amd64.whl",
                            "sha256": _digest("a"),
                        },
                        "arm64": {
                            "filename": "wrapt-cp312-arm64.whl",
                            "sha256": _digest("b"),
                        },
                    }
                },
            },
            {"python_build_lock": "packaging", "import_name": "packaging"},
        ],
        "python_build_lock": {
            "packages": {
                "packaging": {
                    "version": "24.2",
                    "filename": "packaging-24.2-py3-none-any.whl",
                    "sha256": _digest("f"),
                }
            },
            "pyyaml": {
                "version": "6.0.2",
                "artifacts": {
                    "cp312": {
                        "amd64": {
                            "filename": "PyYAML-cp312-amd64.whl",
                            "sha256": _digest("c"),
                        },
                        "arm64": {
                            "filename": "PyYAML-cp312-arm64.whl",
                            "sha256": _digest("d"),
                        },
                    }
                },
            },
            "cmake": {
                "version": "3.31.6",
                "artifacts": {
                    "amd64": {
                        "filename": "cmake-amd64.whl",
                        "sha256": _digest("e"),
                    },
                    "arm64": {
                        "filename": "cmake-arm64.whl",
                        "sha256": _digest("f"),
                    },
                },
            },
        },
        "pr_smoke": {
            "image_selectors": [
                {
                    "product_id": "vllm",
                    "variant": "default",
                    "cpu_arch": "amd64",
                }
            ]
        },
        "wheel_profiles": [_profile("shared", ["amd64", "arm64"])],
        "upstream_products": [
            {
                "id": "vllm",
                "runtime_product": "vllm",
                "repository": "docker.io/vllm/vllm-openai",
                "version_specifier": ">=0.20,<0.23",
                "channels": ["stable"],
                "variants": [
                    {
                        "id": "default",
                        "tag_suffix": "",
                        "npu_arch": "na",
                        "runtime_patch_variants": {"vllm": "default"},
                    }
                ],
                "required_cpu_architectures": ["amd64", "arm64"],
            }
        ],
        "compatibility": {
            "rules": [
                {
                    "id": "cuda-stable",
                    "upstream_products": ["vllm"],
                    "version_specifier": ">=0.20,<0.23",
                    "variants": ["default"],
                    "accelerator": "cuda",
                    "accelerator_runtimes": ["cuda-13.0"],
                    "npu_architectures": ["na"],
                    "operating_systems": ["ubuntu-22.04"],
                    "cpu_architectures": ["amd64", "arm64"],
                    "python_abis": ["cp312"],
                    "upstream_channels": ["stable"],
                }
            ],
            "excluded_upstream_patterns": ["nightly", "dev"],
        },
        "runtime_patch_rules": [
            {
                "id": "vllm-current",
                "order": 10,
                "product": "vllm",
                "version_specifier": ">=0.20,<0.23",
                "channels": ["stable"],
                "variants": ["default"],
                "strategy": "imports",
                "imports": [
                    {
                        "module": "ucm.integration.vllm.patch.load_failure_patch",
                    }
                ],
            }
        ],
        "matrix_limits": {
            "max_wheel_tasks": 8,
            "max_image_tasks": 16,
            "max_family_tasks": 8,
        },
    }


def _complete_riscv64_catalog() -> dict[str, object]:
    """Return a shape-complete catalog that the finite toolchain cannot execute."""
    catalog = _catalog()
    catalog["runner_map"] = {"riscv64": "ubuntu-riscv64"}
    profile = catalog["wheel_profiles"][0]
    profile["cpu_arch"] = ["riscv64"]
    profile["builders"] = {"riscv64": copy.deepcopy(profile["builders"]["amd64"])}
    runtime_artifacts = catalog["python_runtime_dependencies"][0]["wheel_artifacts"]
    runtime_artifacts["cp312"] = {
        "riscv64": copy.deepcopy(runtime_artifacts["cp312"]["amd64"])
    }
    pyyaml_artifacts = catalog["python_build_lock"]["pyyaml"]["artifacts"]
    pyyaml_artifacts["cp312"] = {
        "riscv64": copy.deepcopy(pyyaml_artifacts["cp312"]["amd64"])
    }
    cmake_artifacts = catalog["python_build_lock"]["cmake"]["artifacts"]
    catalog["python_build_lock"]["cmake"]["artifacts"] = {
        "riscv64": copy.deepcopy(cmake_artifacts["amd64"])
    }
    catalog["upstream_products"][0]["required_cpu_architectures"] = ["riscv64"]
    catalog["compatibility"]["rules"][0]["cpu_architectures"] = ["riscv64"]
    catalog["pr_smoke"]["image_selectors"][0]["cpu_arch"] = "riscv64"
    return catalog


def _rebind_first_plan_member_architecture(
    plan: dict[str, object], architecture: str
) -> None:
    """Rehash a full resolved plan after changing one member architecture."""
    image_task = plan["image_tasks"][0]
    old_architecture = image_task["cpu_arch"]
    wheel_task = next(
        task
        for task in plan["wheel_tasks"]
        if task["task_id"] == image_task["wheel_task_id"]
    )
    family_task = next(
        task
        for task in plan["family_tasks"]
        if task["task_id"] == image_task["family_task_id"]
    )
    snapshot = next(
        item
        for item in plan["resolved_upstreams"]
        if core.sha256_value(item) == family_task["snapshot_sha256"]
    )
    snapshot["members"][architecture] = snapshot["members"].pop(old_architecture)

    for task in (wheel_task, image_task):
        task["cpu_arch"] = architecture
        task["platform"] = f"linux/{architecture}"
        task["task_sha256"] = core.sha256_value(
            {key: value for key, value in task.items() if key != "task_sha256"}
        )

    member_index = family_task["image_task_ids"].index(image_task["task_id"])
    family_task["cpu_arch"][member_index] = architecture
    family_task["platform"][member_index] = f"linux/{architecture}"
    family_task["wheel_task_ids"][architecture] = family_task["wheel_task_ids"].pop(
        old_architecture
    )
    family_task["snapshot_sha256"] = core.sha256_value(snapshot)
    if family_task["control_task_id"] == image_task["task_id"]:
        family_task["control_arch"] = architecture
        matrix_item = next(
            item
            for item in plan["github_family_matrix"]["include"]
            if item["task_id"] == family_task["task_id"]
        )
        matrix_item["control_arch"] = architecture
    linked_images = [
        next(task for task in plan["image_tasks"] if task["task_id"] == task_id)
        for task_id in family_task["image_task_ids"]
    ]
    family_task["member_set_sha256"] = core.sha256_value(
        [task["task_sha256"] for task in linked_images]
    )
    family_task["task_sha256"] = core.sha256_value(
        {key: value for key, value in family_task.items() if key != "task_sha256"}
    )
    plan["scan_sha256"] = core.sha256_value(
        {
            "resolved_upstreams": plan["resolved_upstreams"],
            "exclusions": plan["exclusions"],
            "operations": plan["operations"],
        }
    )
    plan["resolved_plan_sha256"] = core.sha256_value(
        {key: value for key, value in plan.items() if key != "resolved_plan_sha256"}
    )


def test_typed_builder_checks_and_patch_manifest_are_part_of_wheel_task_authority() -> (
    None
):
    catalog = _catalog()
    profile = catalog["wheel_profiles"][0]
    profile["id"] = "renamed-profile"
    profile["python_version"] = "3.11"
    profile["python_abi"] = "cp311"
    profile["accelerator_runtime"] = "cuda-14.2"
    profile["builders"]["amd64"]["checks"] = [
        {"kind": "python", "version": "3.11", "abi": "cp311"},
        {"kind": "python-soabi", "prefix": "cpython"},
        {
            "kind": "command-version",
            "name": "nvcc",
            "arguments": ["--version"],
            "contains": "release 14.2, V14.2.7",
        },
        {"kind": "file", "path": "/opt/cuda-14.2/include/cuda.h"},
    ]
    profile["builders"]["arm64"] = copy.deepcopy(profile["builders"]["amd64"])
    rule = catalog["compatibility"]["rules"][0]
    rule["accelerator_runtimes"] = ["cuda-14.2"]
    rule["python_abis"] = ["cp311"]
    catalog["python_build_lock"]["pyyaml"]["artifacts"]["cp311"] = {
        architecture: {
            "filename": f"PyYAML-cp311-{architecture}.whl",
            "sha256": _digest("e"),
        }
        for architecture in ("amd64", "arm64")
    }
    catalog["python_runtime_dependencies"][0]["wheel_artifacts"]["cp311"] = {
        architecture: {
            "filename": f"wrapt-cp311-{architecture}.whl",
            "sha256": _digest("f"),
        }
        for architecture in ("amd64", "arm64")
    }

    plan = core.expand_release_plan(
        catalog,
        [_snapshot("0.21.7", "v0.21.7", "d")],
        lane="feature-candidate",
    )

    task = plan["wheel_tasks"][0]
    assert task["profile_id"] == "renamed-profile"
    assert task["python_version"] == "3.11"
    assert task["python_abi"] == "cp311"
    assert task["builder"]["checks"][2]["contains"] == "release 14.2, V14.2.7"
    assert task["runtime_patch_manifest"]["kind"] == "ucm-runtime-patch-rules"
    assert task["runtime_patch_manifest_sha256"].startswith("sha256:")


def test_python_specific_artifacts_require_exact_abi_and_architecture_selector() -> (
    None
):
    catalog = _catalog()
    profile = catalog["wheel_profiles"][0]
    profile["python_version"] = "3.11"
    profile["python_abi"] = "cp311"
    for builder in profile["builders"].values():
        builder["checks"][0] = {
            "kind": "python",
            "version": "3.11",
            "abi": "cp311",
        }
    catalog["compatibility"]["rules"][0]["python_abis"] = ["cp311"]
    catalog["python_build_lock"]["pyyaml"] = {
        "version": "6.0.2",
        "artifacts": {
            "cp312": {
                "amd64": {
                    "filename": "PyYAML-cp312-amd64.whl",
                    "sha256": _digest("1"),
                },
                "arm64": {
                    "filename": "PyYAML-cp312-arm64.whl",
                    "sha256": _digest("2"),
                },
            }
        },
    }
    legacy_wrapt = copy.deepcopy(
        catalog["python_runtime_dependencies"][0]["wheel_artifacts"]["cp312"]
    )
    catalog["python_runtime_dependencies"][0]["wheel_artifacts"] = {
        "cp312": legacy_wrapt
    }

    with pytest.raises(ValueError, match="cp311.*amd64"):
        core.expand_release_plan(
            catalog,
            [_snapshot("0.21.7", "v0.21.7", "d")],
            lane="feature-candidate",
        )

    catalog["python_build_lock"]["pyyaml"]["artifacts"]["cp311"] = {
        "amd64": {
            "filename": "PyYAML-cp311-amd64.whl",
            "sha256": _digest("3"),
        },
        "arm64": {
            "filename": "PyYAML-cp311-arm64.whl",
            "sha256": _digest("4"),
        },
    }
    catalog["python_runtime_dependencies"][0]["wheel_artifacts"]["cp311"] = {
        "amd64": {
            "filename": "wrapt-cp311-amd64.whl",
            "sha256": _digest("5"),
        },
        "arm64": {
            "filename": "wrapt-cp311-arm64.whl",
            "sha256": _digest("6"),
        },
    }

    task = core.expand_release_plan(
        catalog,
        [_snapshot("0.21.7", "v0.21.7", "d")],
        lane="feature-candidate",
    )["wheel_tasks"][0]
    build_tools = {
        record["name"]: record for record in task["dependency_lock"]["build_tools"]
    }
    assert build_tools["pyyaml"]["filename"] == ("PyYAML-cp311-amd64.whl")
    runtime_wheels = {
        record["name"]: record
        for record in task["dependency_lock"]["runtime_dependencies"]
    }
    assert runtime_wheels["wrapt"]["filename"] == ("wrapt-cp311-amd64.whl")


@pytest.mark.parametrize(
    "checks, message",
    [
        ([{"kind": "shell", "command": "echo no"}], "unsupported builder check"),
        (
            [
                {"kind": "command", "name": "git"},
                {"kind": "command", "name": "git"},
            ],
            "duplicate builder check",
        ),
        (
            [
                {
                    "kind": "command-version",
                    "name": "nvcc",
                    "arguments": ["--version"],
                    "contains": "V14.2.7",
                },
                {
                    "kind": "command-version",
                    "name": "nvcc",
                    "arguments": ["--version"],
                    "contains": "V14.2.8",
                },
            ],
            "conflicting builder checks",
        ),
    ],
)
def test_catalog_rejects_unsupported_duplicate_or_conflicting_builder_checks(
    checks: list[dict[str, object]], message: str
) -> None:
    catalog = _catalog()
    catalog["wheel_profiles"][0]["builders"]["amd64"]["checks"] = checks

    with pytest.raises(ValueError, match=message):
        core.validate_catalog(catalog)


def test_catalog_rejects_file_and_directory_checks_for_same_normalized_path() -> None:
    """A path cannot be required to be both a regular file and a directory."""
    catalog = _catalog()
    checks = catalog["wheel_profiles"][0]["builders"]["amd64"]["checks"]
    checks.extend(
        [
            {"kind": "file", "path": "/opt/toolkit/lib/runtime.so"},
            {"kind": "directory", "path": "/opt/toolkit/lib/./runtime.so"},
        ]
    )

    with pytest.raises(ValueError, match="conflicting builder checks"):
        core.validate_catalog(catalog)


def test_catalog_rejects_duplicate_typed_paths_after_normalization() -> None:
    """Redundant separators cannot conceal a duplicate filesystem assertion."""
    catalog = _catalog()
    checks = catalog["wheel_profiles"][0]["builders"]["amd64"]["checks"]
    checks.extend(
        [
            {"kind": "file", "path": "/opt/toolkit/lib/runtime.so"},
            {"kind": "file", "path": "/opt/toolkit//lib/runtime.so"},
        ]
    )

    with pytest.raises(ValueError, match="duplicate builder check"):
        core.validate_catalog(catalog)


def test_catalog_allows_complementary_checks_for_one_file() -> None:
    """Existence and ELF dependency checks are complementary, not contradictory."""
    catalog = _catalog()
    checks = catalog["wheel_profiles"][0]["builders"]["amd64"]["checks"]
    checks.extend(
        [
            {"kind": "file", "path": "/opt/toolkit/lib/libucm.so"},
            {
                "kind": "shared-library-dependencies",
                "path": "/opt/toolkit/lib/./libucm.so",
            },
        ]
    )

    core.validate_catalog(catalog)


def test_patch_strategy_must_match_each_selected_target_exactly_once() -> None:
    catalog = _catalog()
    snapshot = _snapshot("0.21.7", "v0.21.7", "d")

    missing = copy.deepcopy(catalog)
    missing["runtime_patch_rules"][0]["version_specifier"] = ">=0.22,<0.23"
    with pytest.raises(ValueError, match="no runtime patch strategy"):
        core.expand_release_plan(missing, [snapshot], lane="feature-candidate")

    overlap = copy.deepcopy(catalog)
    duplicate = copy.deepcopy(overlap["runtime_patch_rules"][0])
    duplicate["id"] = "vllm-overlap"
    duplicate["order"] = 20
    overlap["runtime_patch_rules"].append(duplicate)
    with pytest.raises(ValueError, match="overlapping runtime patch strategies"):
        core.expand_release_plan(overlap, [snapshot], lane="feature-candidate")

    explicit_none = copy.deepcopy(catalog)
    explicit_none["runtime_patch_rules"][0].update({"strategy": "none", "imports": []})
    plan = core.expand_release_plan(explicit_none, [snapshot], lane="feature-candidate")
    assert plan["image_tasks"][0]["runtime_patch_rule_id"] == "vllm-current"
    assert plan["image_tasks"][0]["runtime_patch_strategy"] == "none"


def test_yaml_only_variant_split_binds_each_image_to_one_persisted_variant() -> None:
    """Splitting A2/A3 rules in YAML must not require Python dispatch changes."""
    catalog = core.load_catalog()
    original = next(
        rule
        for rule in catalog["runtime_patch_rules"]
        if rule["id"] == "vllm-ascend-0221"
    )
    original["variants"] = ["a2"]
    a3_rule = copy.deepcopy(original)
    a3_rule.update({"id": "vllm-ascend-0221-a3", "order": 155, "variants": ["a3"]})
    catalog["runtime_patch_rules"].append(a3_rule)

    plan = registry.resolve_catalog(
        catalog,
        source_sha="1" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    ascend = [
        task
        for task in plan["image_tasks"]
        if task["runtime_patch_product"] == "vllm-ascend"
    ]

    assert {
        (
            tuple(sorted(task["runtime_patch_variants"].items())),
            task["runtime_patch_rule_id"],
            task["runtime"]["variant"],
        )
        for task in ascend
    } == {
        (
            (("vllm", "default"), ("vllm-ascend", "a2")),
            "vllm-ascend-0221",
            "a2",
        ),
        (
            (("vllm", "default"), ("vllm-ascend", "a3")),
            "vllm-ascend-0221-a3",
            "a3",
        ),
    }
    assert all("runtime_patch_variant" not in task for task in plan["image_tasks"])


@pytest.mark.parametrize(
    "runtime_patch_variants",
    [
        {"vllm-ascend": "a2"},
        {"foreign": "default", "vllm": "default", "vllm-ascend": "a2"},
        {"vllm": "a2", "vllm-ascend": "a2"},
    ],
)
def test_catalog_rejects_missing_foreign_or_unknown_product_variant_maps(
    runtime_patch_variants: dict[str, str],
) -> None:
    """An upstream variant names exactly its installed products and rule variants."""
    catalog = core.load_catalog()
    ascend = next(
        product
        for product in catalog["upstream_products"]
        if product["runtime_product"] == "vllm-ascend"
    )
    next(item for item in ascend["variants"] if item["id"] == "a2")[
        "runtime_patch_variants"
    ] = runtime_patch_variants

    with pytest.raises(ValueError, match="runtime patch variant"):
        core.validate_catalog(catalog)


def _snapshot(version: str, tag: str, character: str) -> dict[str, object]:
    return {
        "product_id": "vllm",
        "repository": "docker.io/vllm/vllm-openai",
        "tag": tag,
        "version": version,
        "channel": "stable",
        "variant": "default",
        "index_digest": _digest(character),
        "members": {
            "amd64": {
                "manifest_digest": _digest(character),
                "config_digest": _digest(character),
            },
            "arm64": {
                "manifest_digest": _digest(character),
                "config_digest": _digest(character),
            },
        },
        "target_repository": "ghcr.io/example/vllm",
        "target_tag": f"{tag}-ucm",
    }


def test_catalog_rejects_invalid_pep440_specifier() -> None:
    catalog = _catalog()
    catalog["compatibility"]["rules"][0]["version_specifier"] = ">0.18*"

    with pytest.raises(ValueError, match="PEP 440"):
        core.validate_catalog(catalog)


def test_plan_reuses_one_wheel_across_multiple_upstream_versions() -> None:
    catalog = _catalog()
    snapshots = [
        _snapshot("0.21.0", "v0.21.0", "2"),
        _snapshot("0.20.1", "v0.20.1", "1"),
    ]

    plan = core.expand_release_plan(catalog, snapshots, lane="feature-candidate")

    assert len(plan["wheel_tasks"]) == 2
    assert len(plan["image_tasks"]) == 4
    assert len(plan["family_tasks"]) == 2
    wheel_by_arch = {task["cpu_arch"]: task["task_id"] for task in plan["wheel_tasks"]}
    assert [task["runtime"]["tag"] for task in plan["image_tasks"]] == [
        "v0.20.1",
        "v0.20.1",
        "v0.21.0",
        "v0.21.0",
    ]
    assert [task["wheel_task_id"] for task in plan["image_tasks"]] == [
        wheel_by_arch["amd64"],
        wheel_by_arch["arm64"],
        wheel_by_arch["amd64"],
        wheel_by_arch["arm64"],
    ]
    assert all(task["runner"] for task in plan["wheel_tasks"])
    assert all(task["platform"].startswith("linux/") for task in plan["image_tasks"])
    assert all(
        task["task_sha256"].startswith("sha256:") for task in plan["wheel_tasks"]
    )
    assert plan == core.expand_release_plan(
        catalog, list(reversed(snapshots)), lane="feature-candidate"
    )


def test_family_can_select_architecture_specific_wheel_profiles() -> None:
    catalog = _catalog()
    catalog["wheel_profiles"] = [
        _profile("x64-profile", ["amd64"]),
        _profile("arm-profile", ["arm64"]),
    ]

    plan = core.expand_release_plan(
        catalog,
        [_snapshot("0.21.0", "v0.21.0", "3")],
        lane="feature-candidate",
    )

    assert [task["profile_id"] for task in plan["image_tasks"]] == [
        "x64-profile",
        "arm-profile",
    ]
    wheel_ids = {task["cpu_arch"]: task["task_id"] for task in plan["wheel_tasks"]}
    assert plan["family_tasks"][0]["wheel_task_ids"] == wheel_ids


def test_runtime_member_with_no_matching_profile_fails_closed() -> None:
    catalog = _catalog()
    catalog["compatibility"]["rules"][0]["version_specifier"] = "<0.21"

    with pytest.raises(ValueError, match="no compatible wheel profile"):
        core.expand_release_plan(
            catalog,
            [_snapshot("0.21.0", "v0.21.0", "4")],
            lane="feature-candidate",
        )


def test_overlapping_profile_matches_fail_with_deterministic_diagnostics() -> None:
    catalog = _catalog()
    catalog["wheel_profiles"] = [
        _profile("zeta", ["amd64", "arm64"]),
        _profile("alpha", ["amd64", "arm64"]),
    ]

    with pytest.raises(ValueError) as rejected:
        core.expand_release_plan(
            catalog,
            [_snapshot("0.21.0", "v0.21.0", "5")],
            lane="feature-candidate",
        )

    assert "overlapping wheel profiles" in str(rejected.value)
    assert "matches=['alpha via cuda-stable', 'zeta via cuda-stable']" in str(
        rejected.value
    )


def test_duplicate_catalog_ids_fail_closed() -> None:
    catalog = _catalog()
    catalog["wheel_profiles"].append(copy.deepcopy(catalog["wheel_profiles"][0]))

    with pytest.raises(ValueError, match="duplicate wheel profile id 'shared'"):
        core.validate_catalog(catalog)


def test_missing_required_architecture_fails_before_expansion() -> None:
    snapshot = _snapshot("0.21.0", "v0.21.0", "6")
    del snapshot["members"]["arm64"]

    with pytest.raises(
        ValueError, match=r"missing required CPU architectures: \['arm64'\]"
    ):
        core.expand_release_plan(_catalog(), [snapshot], lane="feature-candidate")


def test_dynamic_cardinality_overflow_fails_without_truncation() -> None:
    catalog = _catalog()
    catalog["matrix_limits"]["max_image_tasks"] = 3

    with pytest.raises(
        ValueError,
        match="matrix limit max_image_tasks=3 exceeded by exact generated set of 4",
    ):
        core.expand_release_plan(
            catalog,
            [
                _snapshot("0.20.1", "v0.20.1", "7"),
                _snapshot("0.21.0", "v0.21.0", "8"),
            ],
            lane="feature-candidate",
        )


def test_checked_in_current_catalog_fixture_preserves_six_member_regression() -> None:
    """The current 6/3 shape is fixture evidence, never production cardinality."""
    catalog = core.load_catalog()

    assert catalog["schema_version"] == 2
    assert catalog["compatibility"]["rules"]
    plan = registry.resolve_catalog(
        catalog,
        source_sha="2" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )

    assert plan["fixture_only"] is True
    assert {
        key: plan["counts"][key]
        for key in ("wheel_tasks", "image_tasks", "family_tasks")
    } == {"wheel_tasks": 6, "image_tasks": 6, "family_tasks": 3}
    assert [task["spec_id"] for task in plan["image_tasks"]] == [
        "cuda130-amd64",
        "cuda130-arm64",
        "cann900-a2-amd64",
        "cann900-a2-arm64",
        "cann900-a3-amd64",
        "cann900-a3-arm64",
    ]


def test_packaging_runtime_requirement_reuses_the_canonical_build_lock() -> None:
    """Packaging's version and artifact must have one maintained lock source."""
    catalog = core.load_catalog()

    assert catalog["python_runtime_dependencies"] == [
        {
            "requirement": "wrapt==1.17.2",
            "import_name": "wrapt",
            "wheel_artifacts": catalog["python_runtime_dependencies"][0][
                "wheel_artifacts"
            ],
        },
        {"python_build_lock": "packaging", "import_name": "packaging"},
    ]
    assert core.python_runtime_requirements(catalog) == [
        "packaging==24.2",
        "wrapt==1.17.2",
    ]
    task = registry.resolve_catalog(
        catalog,
        source_sha="3" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )["wheel_tasks"][0]
    assert task["runtime_requirements"] == ["packaging==24.2", "wrapt==1.17.2"]
    packaging = next(
        record
        for record in task["dependency_lock"]["runtime_dependencies"]
        if record["name"] == "packaging"
    )
    assert packaging == {
        "name": "packaging",
        "version": "24.2",
        "requirement": "packaging==24.2",
        "import_name": "packaging",
        **catalog["python_build_lock"]["packages"]["packaging"],
    }

    missing = copy.deepcopy(catalog)
    del missing["python_build_lock"]["packages"]["packaging"]
    with pytest.raises(ValueError, match="packaging"):
        core.validate_catalog(missing)


def test_runtime_channel_must_explicitly_match_stable_or_rc_version() -> None:
    catalog = _catalog()
    catalog["upstream_products"][0]["version_specifier"] = ">=0.21.0rc1,<0.22"
    catalog["compatibility"]["rules"][0]["version_specifier"] = ">=0.21.0rc1,<0.22"
    snapshot = _snapshot("0.21.0rc1", "v0.21.0rc1", "9")

    with pytest.raises(ValueError, match="channel stable requires a final version"):
        core.expand_release_plan(catalog, [snapshot], lane="feature-candidate")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda snapshot: snapshot.update(index_digest="sha256:not-a-digest"),
        lambda snapshot: snapshot["members"]["amd64"].update(unexpected="field"),
    ],
)
def test_resolved_snapshot_structure_is_validated_before_expansion(mutation) -> None:
    snapshot = _snapshot("0.21.0", "v0.21.0", "a")
    mutation(snapshot)

    with pytest.raises(ValueError, match=r"resolved_upstreams\[0\]"):
        core.expand_release_plan(_catalog(), [snapshot], lane="feature-candidate")


@pytest.mark.parametrize("version", ["0.21.0a1", "0.21.0b2", "0.21.0.dev3"])
def test_rc_channel_accepts_only_actual_rc_versions(version: str) -> None:
    catalog = _catalog()
    catalog["upstream_products"][0]["version_specifier"] = ">=0.21.0.dev1,<0.22"
    catalog["upstream_products"][0]["channels"] = ["rc"]
    rule = catalog["compatibility"]["rules"][0]
    rule["version_specifier"] = ">=0.21.0.dev1,<0.22"
    rule["upstream_channels"] = ["rc"]
    catalog["runtime_patch_rules"][0]["version_specifier"] = ">=0.21.0.dev1,<0.22"
    catalog["runtime_patch_rules"][0]["channels"] = ["rc"]
    snapshot = _snapshot(version, f"v{version}", "b")
    snapshot["channel"] = "rc"

    with pytest.raises(ValueError, match="channel rc requires a plain rcN version"):
        core.expand_release_plan(catalog, [snapshot], lane="feature-candidate")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda catalog: catalog["upstream_products"][0]["variants"].append(
                {"id": "default", "npu_arch": "na"}
            ),
            "duplicate upstream variant id 'default'",
        ),
        (
            lambda catalog: catalog["compatibility"]["rules"][0].update(
                upstream_products=["missing-product"]
            ),
            "unknown upstream product 'missing-product'",
        ),
        (
            lambda catalog: catalog["compatibility"]["rules"][0].update(
                variants=["missing-variant"]
            ),
            "unknown variant 'missing-variant'",
        ),
    ],
)
def test_nested_variant_ids_and_catalog_references_are_validated(
    mutation, message: str
) -> None:
    catalog = _catalog()
    mutation(catalog)

    with pytest.raises(ValueError, match=message):
        core.validate_catalog(catalog)


def test_duplicate_logical_upstreams_are_rejected_independent_of_caller_order() -> None:
    first = _snapshot("0.21.0", "v0.21.0", "c")
    duplicate = _snapshot("0.21.0", "v0.21.0", "d")
    duplicate["target_tag"] = "v0.21.0-ucm-second-coordinate"

    with pytest.raises(ValueError, match="duplicate logical upstream identity"):
        core.expand_release_plan(
            _catalog(), [duplicate, first], lane="feature-candidate"
        )


def test_family_runtime_and_snapshot_hashes_name_their_exact_values() -> None:
    snapshot = _snapshot("0.21.0", "v0.21.0", "e")

    family = core.expand_release_plan(_catalog(), [snapshot], lane="feature-candidate")[
        "family_tasks"
    ][0]

    assert family["runtime_sha256"] == core.sha256_value(family["runtime"])
    assert family["snapshot_sha256"] == core.sha256_value(snapshot)


@pytest.mark.parametrize("version", ["0.21.0rc1.dev1", "0.21.0rc1.post1"])
def test_rc_channel_rejects_development_or_post_rc_versions(version: str) -> None:
    catalog = _catalog()
    catalog["upstream_products"][0]["version_specifier"] = ">=0.21.0.dev1,<0.22"
    catalog["upstream_products"][0]["channels"] = ["rc"]
    rule = catalog["compatibility"]["rules"][0]
    rule["version_specifier"] = ">=0.21.0.dev1,<0.22"
    rule["upstream_channels"] = ["rc"]
    catalog["runtime_patch_rules"][0]["version_specifier"] = ">=0.21.0.dev1,<0.22"
    catalog["runtime_patch_rules"][0]["channels"] = ["rc"]
    snapshot = _snapshot(version, f"v{version}", "1")
    snapshot["channel"] = "rc"

    with pytest.raises(ValueError, match="channel rc requires a plain rcN version"):
        core.expand_release_plan(catalog, [snapshot], lane="feature-candidate")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_repository", "GHCR.io/example/vllm"),
        ("target_repository", "ghcr.io/example//vllm"),
        ("target_tag", "-not-an-oci-tag"),
        ("target_tag", "a" * 129),
    ],
)
def test_resolved_target_coordinate_uses_strict_oci_syntax(
    field: str, value: str
) -> None:
    snapshot = _snapshot("0.21.0", "v0.21.0", "2")
    snapshot[field] = value

    with pytest.raises(ValueError, match=field):
        core.expand_release_plan(_catalog(), [snapshot], lane="feature-candidate")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda snapshot: snapshot.update(index_digest=None),
        lambda snapshot: snapshot["members"]["amd64"].update(manifest_digest=123),
    ],
)
def test_non_string_resolved_digests_raise_value_error(mutation) -> None:
    snapshot = _snapshot("0.21.0", "v0.21.0", "3")
    mutation(snapshot)

    with pytest.raises(ValueError, match="exact sha256 digest"):
        core.expand_release_plan(_catalog(), [snapshot], lane="feature-candidate")


def test_catalog_schema_accepts_future_catalog_owned_values_and_cardinalities() -> None:
    """The schema checks shape; catalog semantics bind the selected plan later."""
    catalog = core.load_catalog()
    catalog["source"] = {
        "repository": "FutureOrg/unified-cache-next",
        "staging_repository": "registry.example/future/private-staging",
        "default_branch": "next",
        "release_tag": "v1.2.3",
        "release_policy": "two-reviewers-v2",
        "protected_environment": "future-production",
    }
    catalog["runner_map"] = {"riscv64": "ubuntu-riscv64"}
    locked_wheel = copy.deepcopy(
        catalog["python_runtime_dependencies"][0]["wheel_artifacts"]["cp312"]["amd64"]
    )
    future_artifacts = {"cp313": {"riscv64": locked_wheel}}
    catalog["python_runtime_dependencies"] = [
        {
            "requirement": "alpha-runtime>=1",
            "import_name": "alpha_runtime",
            "wheel_artifacts": copy.deepcopy(future_artifacts),
        },
        {"python_build_lock": "packaging", "import_name": "packaging"},
        {
            "requirement": "wrapt==1.18.0",
            "import_name": "wrapt",
            "wheel_artifacts": copy.deepcopy(future_artifacts),
        },
    ]
    catalog["python_build_lock"]["pyyaml"].update(
        {
            "version": "7.1.0",
            "artifacts": {"cp313": {"riscv64": copy.deepcopy(locked_wheel)}},
        }
    )
    catalog["python_build_lock"]["cmake"].update(
        {
            "version": "4.2.0",
            "artifacts": {"riscv64": copy.deepcopy(locked_wheel)},
        }
    )
    catalog["chart"]["validation_cases"] = [
        {
            "name": "future-device",
            "values": "charts/ucm/models/future/values.yaml",
            "product_id": "future-runtime",
            "variant": "x1",
            "expected_resource": "future.example/device",
        }
    ]
    profile = catalog["wheel_profiles"][0]
    profile.update(
        {
            "id": "future-profile",
            "accelerator": "future-accelerator",
            "accelerator_runtime": "future-runtime-2.0",
            "npu_arch": ["x1"],
            "os": ["future-linux-1"],
            "cpu_arch": ["riscv64", "s390x", "loong64"],
            "python_version": "3.13",
            "python_abi": "cp313",
            "wheel_version": "1.2.3+future",
            "wheel_platform": "futurelinux_1_0",
            "binary_profile_id": "future-binary-profile",
            "external_required_dependencies": [
                {
                    "dependency": "libfuture.so",
                    "provider": "future-driver",
                    "expected_mount_root": "/opt/future/driver",
                    "relation": "transitive",
                    "required_at": "device-runtime",
                },
                {
                    "dependency": "libfuture-helper.so",
                    "provider": "future-driver",
                    "expected_mount_root": "/opt/future/driver",
                    "relation": "transitive",
                    "required_at": "device-runtime",
                },
            ],
            "builders": {
                architecture: copy.deepcopy(next(iter(profile["builders"].values())))
                for architecture in ("riscv64", "s390x", "loong64")
            },
        }
    )
    for builder in profile["builders"].values():
        builder["sources"] = [
            {
                **copy.deepcopy(builder["root"]),
                "tag": f"future-source-{index}",
                "index_digest": _digest(str(index + 1)),
                "manifest_digest": _digest(str(index + 4)),
                "config_digest": _digest(str(index + 7)),
            }
            for index in range(3)
        ]
        builder["copy_paths"] = [
            "/opt/future/runtime",
            "/opt/future/toolkit",
            "/opt/future/plugins",
        ]
    catalog["docker_recipes"][0].update(
        {
            "cpu_arch": "riscv64",
            "platform": "futureos/riscv64",
            "runner": "ubuntu-riscv64",
        }
    )
    catalog["upstream_products"][0]["required_cpu_architectures"] = [
        "riscv64",
        "s390x",
        "loong64",
    ]
    catalog["compatibility"]["rules"][0]["cpu_architectures"] = [
        "riscv64",
        "s390x",
        "loong64",
    ]
    catalog["pr_smoke"]["image_selectors"][0]["cpu_arch"] = "riscv64"

    core.validate_schema(
        catalog,
        core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json"),
    )


def test_catalog_and_planner_reject_unsupported_cpu_toolchain_architecture() -> None:
    """Generic schema values still fail closed at the finite toolchain boundary."""
    catalog = _complete_riscv64_catalog()
    snapshot = _snapshot("0.21.0", "v0.21.0", "9")
    snapshot["members"] = {"riscv64": copy.deepcopy(snapshot["members"]["amd64"])}

    with pytest.raises(ValueError, match="unsupported CPU/tool architecture.*riscv64"):
        core.validate_catalog(catalog)
    with pytest.raises(ValueError, match="unsupported CPU/tool architecture.*riscv64"):
        core.expand_release_plan(catalog, [snapshot], lane="feature-candidate")


def test_self_consistent_unsupported_cpu_plan_fails_before_hosted_downstream() -> None:
    """A rehashed foreign plan cannot defer an unsupported arch to wheel.py."""
    plan = registry.resolve_catalog(
        core.load_catalog(),
        source_sha="8" * 40,
        lane="feature-candidate",
        fixture=_registry_fixture(),
    )
    _rebind_first_plan_member_architecture(plan, "riscv64")

    with pytest.raises(ValueError, match="unsupported CPU/tool architecture.*riscv64"):
        registry.validate_resolved_plan(plan)
    with pytest.raises(ValueError, match="unsupported CPU/tool architecture.*riscv64"):
        verify.hosted_wheel_task(
            plan["wheel_tasks"][0],
            plan["source"]["commit"],
            1_700_000_000,
            resolved_plan=plan,
            expected_plan_sha256=plan["resolved_plan_sha256"],
        )


def test_image_result_schema_accepts_plan_owned_future_values_and_dependencies() -> (
    None
):
    schema = core.load_json(RELEASE_ROOT / "schemas" / "image-result.schema.json")
    digest = _digest("a")
    wheel = {
        "filename": "uc_manager-1.2.3-cp313-future.whl",
        "sha256": digest,
        "size": 1,
        "spec_id": "future-spec",
        "declaration_sha256": digest,
        "version": "1.2.3+future",
        "python_abi": "cp313",
        "cpu_arch": "riscv64",
        "accelerator": "future-accelerator",
        "accelerator_runtime": "future-runtime-2.0",
        "npu_arch_or_na": "x1",
        "os": "future-linux-1",
        "binary_profile_id": "future-profile",
        "requires_dist": [
            "alpha-runtime>=1",
            "packaging==25.1",
            "wrapt==1.18.0",
        ],
    }
    core.validate_schema(wheel, schema["properties"]["wheel"], root=schema)
    core.validate_schema(
        "futureos/riscv64", schema["properties"]["target_platform"], root=schema
    )
    core.validate_schema(
        {
            "output": "local-oci",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "platform": "futureos/riscv64",
            "published": False,
        },
        schema["properties"]["oci"],
        root=schema,
    )
    core.validate_schema(
        [
            {
                "name": "alpha-runtime",
                "version": "2.0",
                "requirement": "alpha-runtime==2.0",
                "import_name": "alpha_runtime",
                "filename": "alpha_runtime-2.0-py3-none-any.whl",
                "sha256": digest,
            },
            {
                "name": "packaging",
                "version": "25.1",
                "requirement": "packaging==25.1",
                "import_name": "packaging",
                "filename": "packaging-25.1-py3-none-any.whl",
                "sha256": digest,
            },
            {
                "name": "wrapt",
                "version": "1.18.0",
                "requirement": "wrapt==1.18.0",
                "import_name": "wrapt",
                "filename": "wrapt-1.18.0-cp313-riscv64.whl",
                "sha256": digest,
            },
        ],
        schema["properties"]["runtime_dependencies"],
        root=schema,
    )


@pytest.mark.parametrize("member_count", [1, 3])
def test_release_schema_accepts_dynamic_registry_member_counts(
    member_count: int,
) -> None:
    schema = core.load_json(RELEASE_ROOT / "schemas" / "release-manifest.schema.json")
    digest_schema = schema["$defs"]["registryIndexRecord"]["properties"][
        "member_digests"
    ]

    core.validate_schema(
        [_digest(str(index + 1)) for index in range(member_count)],
        digest_schema,
        root=schema,
    )


def test_release_schema_accepts_future_plan_owned_wheel_and_asset_values() -> None:
    schema = core.load_json(RELEASE_ROOT / "schemas" / "release-manifest.schema.json")
    manifest = core._build_fixture_release_manifest()
    wheel = copy.deepcopy(manifest["wheel_specs"][0])
    wheel.update(
        {
            "spec_id": "future-spec",
            "accelerator": "future-accelerator",
            "accelerator_runtime": "future-runtime-2.0",
            "npu_arch_or_na": "x1",
            "os": "future-linux-1",
            "cpu_arch": "riscv64",
            "python_version": "3.13",
            "python_abi": "cp313",
            "wheel_version": "1.2.3+future",
            "wheel_platform": "futurelinux_1_0",
            "binary_profile_id": "future-profile",
            "external_required_dependencies": [
                {
                    "dependency": "libfuture.so",
                    "provider": "future-driver",
                    "expected_mount_root": "/opt/future/driver",
                    "relation": "transitive",
                    "required_at": "device-runtime",
                },
                {
                    "dependency": "libfuture-helper.so",
                    "provider": "future-driver",
                    "expected_mount_root": "/opt/future/driver",
                    "relation": "transitive",
                    "required_at": "device-runtime",
                },
            ],
            "locks": [wheel["locks"][0]],
        }
    )
    wheel_schema = schema["properties"]["wheel_specs"]["items"]
    core.validate_schema(wheel, wheel_schema, root=schema)
    core.validate_schema(
        {
            "target": "github-release",
            "assets": [
                {
                    "id": "future-sbom",
                    "type": "sbom",
                    "required": True,
                    "status": "candidate",
                }
            ],
        },
        schema["properties"]["publication"],
        root=schema,
    )
    core.validate_schema(
        "futureos/riscv64",
        schema["$defs"]["registryMemberRecord"]["properties"]["platform"],
        root=schema,
    )
    labels = {
        "org.opencontainers.image.source": "https://github.com/FutureOrg/unified-cache-next",
        "org.opencontainers.image.revision": "a" * 40,
        "io.ucm.release.source-tree": "b" * 40,
        "io.ucm.release.source-context-sha256": _digest("1"),
        "io.ucm.release.build-key-sha256": _digest("2"),
        "io.ucm.release.task-sha256": _digest("3"),
        "io.ucm.release.wheel-sha256": _digest("4"),
        "io.ucm.release.recipe-sha256": _digest("5"),
    }
    core.validate_schema(labels, schema["$defs"]["registryConfigLabels"], root=schema)
    core.validate_schema(
        {
            "type": "registry-member-push-by-digest",
            "capability": "write",
            "reference": "registry.example/future/staging@" + _digest("6"),
        },
        schema["$defs"]["registryMemberOperation"],
        root=schema,
    )


def test_runtime_requirements_are_resolved_from_catalog_declarations() -> None:
    catalog = _catalog()
    catalog["python_runtime_dependencies"] = [
        {
            "requirement": "alpha-runtime==2.0",
            "import_name": "alpha_runtime",
            "wheel_artifacts": copy.deepcopy(
                catalog["python_runtime_dependencies"][0]["wheel_artifacts"]
            ),
        },
        {"python_build_lock": "packaging", "import_name": "packaging"},
        {
            "requirement": "wrapt==1.18.0",
            "import_name": "wrapt",
            "wheel_artifacts": copy.deepcopy(
                catalog["python_runtime_dependencies"][0]["wheel_artifacts"]
            ),
        },
    ]

    assert core.python_runtime_requirements(catalog) == sorted(
        ["alpha-runtime==2.0", "packaging==24.2", "wrapt==1.18.0"]
    )


def test_dynamic_schema_maps_still_validate_each_catalog_owned_value() -> None:
    schema = core.load_json(RELEASE_ROOT / "schemas" / "config.schema.json")

    with pytest.raises(ValueError, match="missing required properties"):
        core.validate_schema(
            {"cp313": {"riscv64": {}}},
            schema["$defs"]["pythonAbiArtifacts"],
            root=schema,
        )
