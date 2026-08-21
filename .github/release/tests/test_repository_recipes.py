"""Repository recipe structure and output-injection safety contract.

Only the recipe-structure basics and output-injection safety invariants are
retained: the catalog registers every Dockerfile, base images are
catalog-owned, every FROM resolves through catalog arguments, and recipe
IDs/build-args/cache-scopes/base-images reject GitHub Actions expression
injection.  The Dockerfile parser edge-case and lane-matrix change-detector
suites were removed per the slimming plan.
"""

from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

core = importlib.import_module("ucm_release.core")


def test_unregistered_future_dockerfile_does_not_block_declared_recipes(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    docker_root = tmp_path / "docker"
    docker_root.mkdir()
    for recipe in catalog["docker_recipes"]:
        source = ROOT / recipe["path"]
        target = tmp_path / recipe["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (docker_root / "Dockerfile.ucm-vllm-ascend.a2-v0.99.0").write_text(
        "ARG IMAGE_SOURCE=\"quay.io/ascend\"\n"
        "ARG IMAGE_NAME_VERSION=\"vllm-ascend:v0.99.0\"\n"
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)


def test_repository_recipe_base_images_are_catalog_owned_and_contract_checked() -> None:
    catalog = core.load_catalog()

    assert all(recipe["base_image"] for recipe in catalog["docker_recipes"])
    for lane in ("pr-smoke", "hardware-e2e"):
        matrix = core.repository_recipe_matrix(catalog, lane=lane)
        for task in matrix["include"]:
            assert f"IMAGE_SOURCE={task['base_image']['source']}" in task["build_args"]
            assert (
                f"IMAGE_NAME_VERSION={task['base_image']['name_version']}"
                in task["build_args"]
            )
    mutated = copy.deepcopy(catalog)
    mutated["docker_recipes"][0]["base_image"] = "docker.io/foreign/image:latest"
    try:
        core.validate_repository_recipe_inventory(mutated)
    except ValueError as error:
        assert "base image" in str(error).lower()
    else:
        raise AssertionError("Dockerfile/catalog base-image drift was accepted")


def test_repository_recipe_rejects_any_non_catalog_from_instruction(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        (ROOT / recipe["path"]).read_text(encoding="utf-8")
        + "\nFROM docker.io/library/alpine:3.22 AS foreign\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "every FROM" in str(error)
    else:
        raise AssertionError("second hardcoded FROM instruction was accepted")


def test_repository_recipe_rejects_unsafe_base_image_output(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    original_source = recipe["base_image"]["source"]
    recipe["base_image"]["source"] = "${{github.token}}"
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        (ROOT / recipe["path"])
        .read_text(encoding="utf-8")
        .replace(original_source, recipe["base_image"]["source"], 1),
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "base image" in str(error).lower()
        assert "safe" in str(error).lower()
    else:
        raise AssertionError("unsafe base-image output was accepted")


def test_repository_recipe_rejects_unsafe_build_argument_output() -> None:
    catalog = core.load_catalog()
    catalog["docker_recipes"][0]["build_args"] = [
        "PIP_INDEX_URL=https://safe.invalid\nUCM_BUILD_ARGS\npath=foreign"
    ]

    try:
        core.validate_repository_recipe_inventory(catalog)
    except ValueError as error:
        assert "build_args" in str(error)
        assert "output-safe" in str(error)
    else:
        raise AssertionError("multiline build-argument output was accepted")


def test_repository_recipe_rejects_unsafe_cache_scope_output() -> None:
    catalog = core.load_catalog()
    catalog["docker_recipes"][0]["cache_scope"] = "safe\npath=foreign"

    try:
        core.validate_repository_recipe_inventory(catalog)
    except ValueError as error:
        assert "cache scope" in str(error)
        assert "output-safe" in str(error)
    else:
        raise AssertionError("multiline cache-scope output was accepted")


def test_recipe_ids_and_runner_labels_are_safe_for_actions_and_oci_tags() -> None:
    catalog = core.load_catalog()

    for invalid_id in ("Uppercase", "path/segment", "line\nbreak", "${{expr}}"):
        mutated = copy.deepcopy(catalog)
        mutated["docker_recipes"][0]["id"] = invalid_id
        try:
            core.validate_repository_recipe_inventory(mutated)
        except ValueError as error:
            assert "id" in str(error).lower()
        else:
            raise AssertionError(f"unsafe recipe ID was accepted: {invalid_id!r}")

    smoke = copy.deepcopy(catalog)
    smoke_recipe = next(
        recipe for recipe in smoke["docker_recipes"] if "pr-smoke" in recipe["lanes"]
    )
    smoke_recipe["runner"] = "self-hosted"
    try:
        core.validate_repository_recipe_inventory(smoke)
    except ValueError as error:
        assert "hosted runner" in str(error)
    else:
        raise AssertionError("self-hosted runner entered the pr-smoke lane")

    hardware = copy.deepcopy(catalog)
    hardware_recipe = next(
        recipe
        for recipe in hardware["docker_recipes"]
        if "hardware-e2e" in recipe["lanes"]
    )
    hardware_recipe["runner"] = ["self-hosted", "${{expr}}"]
    try:
        core.validate_repository_recipe_inventory(hardware)
    except ValueError as error:
        assert "runner label" in str(error)
    else:
        raise AssertionError("unsafe self-hosted runner label was accepted")
