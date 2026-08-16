from __future__ import annotations

import importlib
import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

core = importlib.import_module("ucm_release.core")
cli = importlib.import_module("ucm_release.cli")
image = importlib.import_module("ucm_release.image")
registry = importlib.import_module("ucm_release.registry")


def test_catalog_registers_exact_repository_dockerfile_inventory() -> None:
    catalog = core.load_catalog()

    registered = [recipe["path"] for recipe in catalog["docker_recipes"]]
    discovered = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "docker").glob("Dockerfile.ucm-*")
        if path.is_file()
    )

    assert len(registered) == len(set(registered))
    assert sorted(registered) == discovered


def test_load_catalog_uses_one_explicit_alternate_checkout_root(
    tmp_path: Path,
) -> None:
    source_catalog = core.load_catalog()
    release_dir = tmp_path / ".github" / "release"
    release_dir.mkdir(parents=True)
    catalog_path = release_dir / "release.yaml"
    catalog_path.write_bytes((RELEASE_ROOT / "release.yaml").read_bytes())
    for recipe in source_catalog["docker_recipes"]:
        target = tmp_path / recipe["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / recipe["path"]).read_bytes())
    for rule in source_catalog["runtime_patch_rules"]:
        for declaration in rule["imports"]:
            module = declaration["module"].replace(".", "/")
            source = ROOT / f"{module}.py"
            target = tmp_path / f"{module}.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

    try:
        core.load_catalog(
            catalog_path,
            RELEASE_ROOT / "schemas",
            repository_root=tmp_path,
        )
    except FileNotFoundError as error:
        assert "version.ini" in str(error)
    else:
        raise AssertionError("alternate checkout without version.ini was accepted")

    (tmp_path / "version.ini").write_text(
        "VLLM_UC_VERSION=9.9.9rc9\n", encoding="utf-8"
    )
    try:
        core.load_catalog(
            catalog_path,
            RELEASE_ROOT / "schemas",
            repository_root=tmp_path,
        )
    except ValueError as error:
        assert "does not match version.ini" in str(error)
    else:
        raise AssertionError("alternate checkout version mismatch was accepted")

    (tmp_path / "version.ini").write_bytes((ROOT / "version.ini").read_bytes())
    try:
        core.load_catalog(
            catalog_path,
            RELEASE_ROOT / "schemas",
            repository_root=tmp_path,
        )
    except FileNotFoundError as error:
        assert "Chart.yaml" in str(error)
    else:
        raise AssertionError("alternate checkout without Chart.yaml was accepted")

    chart_path = tmp_path / source_catalog["chart"]["source"] / "Chart.yaml"
    chart_path.parent.mkdir(parents=True)
    chart_path.write_text(
        f"name: {source_catalog['chart']['name']}\n"
        "version: 9.9.9\nappVersion: 9.9.9\n",
        encoding="utf-8",
    )
    try:
        core.load_catalog(
            catalog_path,
            RELEASE_ROOT / "schemas",
            repository_root=tmp_path,
        )
    except ValueError as error:
        assert "Chart version" in str(error) or "Chart appVersion" in str(error)
    else:
        raise AssertionError("alternate checkout Chart mismatch was accepted")

    chart_path.write_bytes(
        (ROOT / source_catalog["chart"]["source"] / "Chart.yaml").read_bytes()
    )
    assert (
        core.load_catalog(
            catalog_path,
            RELEASE_ROOT / "schemas",
            repository_root=tmp_path,
        )
        == source_catalog
    )


def test_alternate_catalog_file_defaults_to_the_current_checkout(
    tmp_path: Path,
) -> None:
    alternate = tmp_path / "candidate-release.yaml"
    alternate.write_bytes((RELEASE_ROOT / "release.yaml").read_bytes())

    assert core.load_catalog(alternate, RELEASE_ROOT / "schemas") == core.load_catalog()


def test_catalog_cli_accepts_an_explicit_repository_root(tmp_path: Path) -> None:
    alternate = tmp_path / "candidate-release.yaml"
    alternate.write_bytes((RELEASE_ROOT / "release.yaml").read_bytes())

    assert (
        cli.main(
            [
                "catalog",
                "validate",
                "--catalog",
                str(alternate),
                "--repository-root",
                str(ROOT),
            ]
        )
        == 0
    )


def test_repository_recipe_matrix_projects_canonical_explicit_tasks() -> None:
    catalog = core.load_catalog()

    smoke = core.repository_recipe_matrix(catalog, lane="pr-smoke")
    hardware = core.repository_recipe_matrix(catalog, lane="hardware-e2e")

    assert set(smoke) == {
        "kind",
        "schema_version",
        "lane",
        "catalog_sha256",
        "protected_environment",
        "count",
        "include",
        "matrix_sha256",
    }
    assert smoke["catalog_sha256"] == core.sha256_value(catalog)
    assert smoke["protected_environment"] == catalog["source"]["protected_environment"]
    assert [task["task_id"] for task in smoke["include"]] == sorted(
        recipe["id"]
        for recipe in catalog["docker_recipes"]
        if "pr-smoke" in recipe["lanes"]
    )
    assert [task["task_id"] for task in hardware["include"]] == sorted(
        recipe["id"]
        for recipe in catalog["docker_recipes"]
        if "hardware-e2e" in recipe["lanes"]
    )
    assert smoke["count"] == len(smoke["include"])
    assert hardware["count"] == len(hardware["include"])
    for task in [*smoke["include"], *hardware["include"]]:
        assert set(task) == {
            "task_id",
            "task_sha256",
            "catalog_sha256",
            "dockerfile_sha256",
            "path",
            "lane",
            "runner",
            "cpu_arch",
            "platform",
            "product",
            "backend",
            "variant",
            "upstream_version",
            "base_image",
            "upstream_product_id",
            "upstream_variant",
            "status",
            "build_mode",
            "cache_scope",
            "build_args",
            "engine_type",
            "install_hook",
            "exclusion_reason",
        }
        assert task["task_sha256"] == core.sha256_value(
            {key: value for key, value in task.items() if key != "task_sha256"}
        )
        assert task["dockerfile_sha256"].startswith("sha256:")


def test_repository_recipe_selection_rejects_wrong_lane_and_tampered_matrix() -> None:
    catalog = core.load_catalog()
    hardware = core.repository_recipe_matrix(catalog, lane="hardware-e2e")
    task = hardware["include"][0]

    assert (
        core.select_repository_recipe_task(
            catalog,
            lane="hardware-e2e",
            task_id=task["task_id"],
            expected_catalog_sha256=hardware["catalog_sha256"],
            expected_matrix_sha256=hardware["matrix_sha256"],
            expected_task_sha256=task["task_sha256"],
        )
        == task
    )

    try:
        smoke = core.repository_recipe_matrix(catalog, lane="pr-smoke")
        core.select_repository_recipe_task(
            catalog,
            lane="pr-smoke",
            task_id=task["task_id"],
            expected_matrix_sha256=smoke["matrix_sha256"],
        )
    except ValueError as error:
        assert "exactly one" in str(error)
    else:
        raise AssertionError("wrong-lane recipe selection was accepted")

    try:
        core.select_repository_recipe_task(
            catalog,
            lane="hardware-e2e",
            task_id=task["task_id"],
            expected_matrix_sha256=core.sha256_value({}),
        )
    except ValueError as error:
        assert "matrix hash" in str(error)
    else:
        raise AssertionError("wrong expected recipe matrix hash was accepted")

    tampered = copy.deepcopy(hardware)
    tampered["include"][0]["path"] = "docker/Dockerfile.ucm-foreign"
    tampered["include"][0]["task_sha256"] = core.sha256_value(
        {
            key: value
            for key, value in tampered["include"][0].items()
            if key != "task_sha256"
        }
    )
    tampered["matrix_sha256"] = core.sha256_value(
        {key: value for key, value in tampered.items() if key != "matrix_sha256"}
    )
    try:
        core.validate_repository_recipe_matrix(catalog, tampered, lane="hardware-e2e")
    except ValueError as error:
        assert "differs from catalog" in str(error)
    else:
        raise AssertionError("self-hashed tampered recipe matrix was accepted")

    for label, changed in (
        ("missing", copy.deepcopy(hardware)),
        ("extra", copy.deepcopy(hardware)),
    ):
        if label == "missing":
            changed["include"].clear()
            changed["count"] = 0
        else:
            changed["include"].append(copy.deepcopy(task))
            changed["count"] = 2
        changed["matrix_sha256"] = core.sha256_value(
            {key: value for key, value in changed.items() if key != "matrix_sha256"}
        )
        try:
            core.validate_repository_recipe_matrix(
                catalog, changed, lane="hardware-e2e"
            )
        except ValueError as error:
            assert "differs from catalog" in str(error)
        else:
            raise AssertionError(f"{label} recipe matrix task was accepted")


def test_repository_recipe_semantics_reject_unknown_status_lane_and_mode() -> None:
    catalog = core.load_catalog()
    recipe = catalog["docker_recipes"][0]

    for field, invalid in (
        ("status", "retired"),
        ("build_mode", "container-copy"),
    ):
        mutated = copy.deepcopy(catalog)
        mutated["docker_recipes"][0][field] = invalid
        try:
            core.validate_repository_recipe_inventory(mutated)
        except ValueError as error:
            assert field.replace("_", " ") in str(error).lower()
        else:
            raise AssertionError(f"invalid {field} was accepted")

    mutated = copy.deepcopy(catalog)
    mutated_recipe = mutated["docker_recipes"][0]
    mutated_recipe["status"] = "legacy"
    mutated_recipe["build_mode"] = "legacy-source-build"
    mutated_recipe["lanes"] = ["formal-release"]
    try:
        core.validate_repository_recipe_inventory(mutated)
    except ValueError as error:
        assert "legacy-source-build" in str(error)
    else:
        raise AssertionError("legacy source build acquired formal release authority")

    assert recipe["build_mode"] == "legacy-source-build"


def test_repository_inventory_rejects_missing_extra_duplicate_and_non_recipe_paths() -> (
    None
):
    catalog = core.load_catalog()

    missing = copy.deepcopy(catalog)
    missing["docker_recipes"].pop()
    try:
        core.validate_repository_recipe_inventory(missing)
    except ValueError as error:
        assert "unregistered=" in str(error)
    else:
        raise AssertionError("unregistered repository Dockerfile was accepted")

    nonexistent = copy.deepcopy(catalog)
    nonexistent["docker_recipes"][0]["path"] = "docker/Dockerfile.ucm-future"
    try:
        core.validate_repository_recipe_inventory(nonexistent)
    except ValueError as error:
        assert "does not exist" in str(error)
    else:
        raise AssertionError("nonexistent registered Dockerfile was accepted")

    duplicate = copy.deepcopy(catalog)
    collision = copy.deepcopy(duplicate["docker_recipes"][0])
    collision["id"] = "unrelated-opaque-id"
    collision["cache_scope"] = "unrelated-cache-scope"
    duplicate["docker_recipes"].append(collision)
    try:
        core.validate_repository_recipe_inventory(duplicate)
    except ValueError as error:
        assert "path collision" in str(error)
    else:
        raise AssertionError("duplicate normalized recipe path was accepted")

    non_recipe = copy.deepcopy(catalog)
    non_recipe["docker_recipes"][0]["path"] = "docker/README.md"
    try:
        core.validate_repository_recipe_inventory(non_recipe)
    except ValueError as error:
        assert "Dockerfile.ucm-*" in str(error)
    else:
        raise AssertionError("non-recipe path was accepted")


def test_arbitrary_recipe_id_and_future_filename_expand_without_code_changes(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    for registered in catalog["docker_recipes"]:
        temporary_path = tmp_path / registered["path"]
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_bytes((ROOT / registered["path"]).read_bytes())
    before = core.repository_recipe_matrix(
        catalog, lane="pr-smoke", repository_root=tmp_path
    )

    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    recipe["id"] = "opaque-z9"
    recipe["path"] = "docker/Dockerfile.ucm-future.product-r42"
    recipe["cache_scope"] = "future-product-r42"
    catalog["docker_recipes"].append(recipe)
    (tmp_path / recipe["path"]).write_bytes(
        (ROOT / catalog["docker_recipes"][5]["path"]).read_bytes()
    )

    matrix = core.repository_recipe_matrix(
        catalog, lane="pr-smoke", repository_root=tmp_path
    )

    assert matrix["count"] == before["count"] + 1
    added = next(task for task in matrix["include"] if task["task_id"] == recipe["id"])
    assert added["path"] == recipe["path"]


def test_catalog_cli_emits_lane_matrix_and_selects_one_hash_bound_task(
    tmp_path: Path,
) -> None:
    matrix_path = tmp_path / "recipe-matrix.json"
    selected_path = tmp_path / "selected-recipe.json"

    assert (
        cli.main(
            [
                "catalog",
                "recipe-matrix",
                "--lane",
                "pr-smoke",
                "--output",
                str(matrix_path),
            ]
        )
        == 0
    )
    matrix = core.load_json(matrix_path)
    task = matrix["include"][0]
    assert (
        cli.main(
            [
                "catalog",
                "select-recipe",
                "--lane",
                "pr-smoke",
                "--task-id",
                task["task_id"],
                "--expected-catalog-sha256",
                matrix["catalog_sha256"],
                "--expected-matrix-sha256",
                matrix["matrix_sha256"],
                "--expected-task-sha256",
                task["task_sha256"],
                "--output",
                str(selected_path),
            ]
        )
        == 0
    )
    assert core.load_json(selected_path) == task


def test_repository_recipe_inventory_rejects_symlink_paths(tmp_path: Path) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    recipe["id"] = "future-source-build"
    recipe["path"] = "docker/Dockerfile.ucm-future"
    recipe["cache_scope"] = "future-source-build"
    catalog["docker_recipes"] = [recipe]
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    target = docker_dir / "shared.Dockerfile"
    target.write_text("FROM scratch\n", encoding="utf-8")
    (docker_dir / "Dockerfile.ucm-future").symlink_to(target.name)

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "symlink" in str(error)
    else:
        raise AssertionError("symlink Docker recipe path was accepted")


def test_repository_recipe_path_rejects_output_injection_characters(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    source_path = recipe["path"]
    unsafe_path = "docker/Dockerfile.ucm-safe\n${{github.token}}"
    recipe["path"] = unsafe_path
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / unsafe_path
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_bytes((ROOT / source_path).read_bytes())

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "safe filename" in str(error)
    else:
        raise AssertionError("output-injecting Docker recipe path was accepted")


def test_repository_recipe_inventory_rejects_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    real_docker = tmp_path / "real-docker"
    real_docker.mkdir()
    (tmp_path / "docker").symlink_to(real_docker.name, target_is_directory=True)
    (real_docker / Path(recipe["path"]).name).write_bytes(
        (ROOT / recipe["path"]).read_bytes()
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "symlink component" in str(error)
    else:
        raise AssertionError("symlinked docker ancestor was accepted")


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


def test_repository_recipe_rejects_duplicate_base_image_arguments(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    text = (ROOT / recipe["path"]).read_text(encoding="utf-8")
    for arg_name, declaration in (
        ("IMAGE_SOURCE", 'ARG IMAGE_SOURCE="quay.io/ascend"'),
        (
            "IMAGE_NAME_VERSION",
            'ARG IMAGE_NAME_VERSION="vllm-ascend:v0.20.2rc1"',
        ),
    ):
        repository_root = tmp_path / arg_name.lower()
        dockerfile = repository_root / recipe["path"]
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text(
            text.replace(declaration, f"{declaration}\n{declaration}", 1),
            encoding="utf-8",
        )
        try:
            core.validate_repository_recipe_inventory(
                catalog, repository_root=repository_root
            )
        except ValueError as error:
            assert "exactly one" in str(error)
            assert arg_name in str(error)
        else:
            raise AssertionError(f"duplicate {arg_name} authority was accepted")


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


def test_repository_recipe_accepts_typed_platform_and_stage_alias(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        (ROOT / recipe["path"])
        .read_text(encoding="utf-8")
        .replace(
            "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}",
            "from\t--platform=linux/arm64 "
            "${IMAGE_SOURCE}/${IMAGE_NAME_VERSION} as runtime",
            1,
        ),
        encoding="utf-8",
    )

    core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)


def test_repository_recipe_requires_base_args_before_first_from(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n"
        f'ARG IMAGE_SOURCE="{recipe["base_image"]["source"]}"\n'
        f'ARG IMAGE_NAME_VERSION="{recipe["base_image"]["name_version"]}"\n',
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "must precede the first FROM" in str(error)
    else:
        raise AssertionError("base-image ARG authority after FROM was accepted")


def test_repository_recipe_honors_backtick_escape_when_auditing_from(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        "# escape=`\n"
        'ARG IMAGE_SOURCE="quay.io/ascend"\n'
        'ARG IMAGE_NAME_VERSION="vllm-ascend:v0.20.2rc1"\n'
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n"
        "RUN printf ordinary-backslash \\\n"
        "FROM docker.io/library/alpine:3.22 AS unauthorized\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "every FROM" in str(error)
    else:
        raise AssertionError("backtick escape hid an unauthorized FROM instruction")


def test_repository_recipe_does_not_continue_default_escape_comments(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    source = recipe["base_image"]["source"]
    name_version = recipe["base_image"]["name_version"]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        f'ARG IMAGE_SOURCE="{source}"\n'
        f'ARG IMAGE_NAME_VERSION="{name_version}"\n'
        "# ordinary comment must end here \\\n"
        "FROM docker.io/library/alpine:3.22 AS unauthorized\n"
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "every FROM" in str(error)
    else:
        raise AssertionError("a default-escape comment hid an unauthorized FROM")


def test_repository_recipe_does_not_continue_backtick_escape_comments(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    source = recipe["base_image"]["source"]
    name_version = recipe["base_image"]["name_version"]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        "# escape=`\n"
        f'ARG IMAGE_SOURCE="{source}"\n'
        f'ARG IMAGE_NAME_VERSION="{name_version}"\n'
        "# ordinary comment must end here `\n"
        "FROM docker.io/library/alpine:3.22 AS unauthorized\n"
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "every FROM" in str(error)
    else:
        raise AssertionError("a backtick-escape comment hid an unauthorized FROM")


def test_repository_recipe_joins_default_escape_without_inserting_whitespace(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    source = recipe["base_image"]["source"]
    name_version = recipe["base_image"]["name_version"]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        f'ARG IMAGE_SOURCE="{source}"\n'
        f'ARG IMAGE_NAME_VERSION="{name_version}"\n'
        "FR\\\n"
        "OM docker.io/library/alpine:3.22 AS unauthorized\n"
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "every FROM" in str(error)
    else:
        raise AssertionError("default continuation split and hid a FROM instruction")


def test_repository_recipe_joins_backtick_escape_without_inserting_whitespace(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    source = recipe["base_image"]["source"]
    name_version = recipe["base_image"]["name_version"]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        "# escape=`\n"
        f'ARG IMAGE_SOURCE="{source}"\n'
        f'ARG IMAGE_NAME_VERSION="{name_version}"\n'
        "FR`\n"
        "OM docker.io/library/alpine:3.22 AS unauthorized\n"
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "every FROM" in str(error)
    else:
        raise AssertionError("backtick continuation split and hid a FROM instruction")


def test_repository_recipe_skips_comments_inside_split_keyword_continuations(
    tmp_path: Path,
) -> None:
    source_catalog = core.load_catalog()
    recipe = copy.deepcopy(source_catalog["docker_recipes"][5])
    source = recipe["base_image"]["source"]
    name_version = recipe["base_image"]["name_version"]
    accepted_cases: list[str] = []

    for case, parser_directive, escape_character in (
        ("default", "", "\\"),
        ("backtick", "# escape=`\n", "`"),
    ):
        catalog = copy.deepcopy(source_catalog)
        catalog["docker_recipes"] = [copy.deepcopy(recipe)]
        repository_root = tmp_path / case
        dockerfile = repository_root / recipe["path"]
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text(
            parser_directive
            + f'ARG IMAGE_SOURCE="{source}"\n'
            + f'ARG IMAGE_NAME_VERSION="{name_version}"\n'
            + f"FR{escape_character}\n"
            + "# ordinary comment inside continuation\n"
            + "OM docker.io/library/alpine:3.22 AS unauthorized\n"
            + "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
            encoding="utf-8",
        )

        try:
            core.validate_repository_recipe_inventory(
                catalog, repository_root=repository_root
            )
        except ValueError as error:
            assert "every FROM" in str(error)
        else:
            accepted_cases.append(case)

    assert accepted_cases == []


@pytest.mark.parametrize(
    ("case", "parser_directive", "escape_character", "blank_line"),
    [
        ("default-empty", "", "\\", ""),
        ("default-whitespace", "", "\\", "   \t"),
        ("backtick-empty", "# escape=`\n", "`", ""),
        ("backtick-whitespace", "# escape=`\n", "`", "   \t"),
    ],
)
def test_repository_recipe_rejects_blank_lines_inside_continuations(
    tmp_path: Path,
    case: str,
    parser_directive: str,
    escape_character: str,
    blank_line: str,
) -> None:
    """A physical blank cannot terminate parsing and hide a split FROM."""
    source_catalog = core.load_catalog()
    recipe = copy.deepcopy(source_catalog["docker_recipes"][5])
    catalog = copy.deepcopy(source_catalog)
    catalog["docker_recipes"] = [recipe]
    repository_root = tmp_path / case
    dockerfile = repository_root / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        parser_directive
        + f'ARG IMAGE_SOURCE="{recipe["base_image"]["source"]}"\n'
        + f'ARG IMAGE_NAME_VERSION="{recipe["base_image"]["name_version"]}"\n'
        + f"FR{escape_character}\n"
        + blank_line
        + "\nOM docker.io/library/alpine:3.22 AS unauthorized\n"
        + "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="blank.*continuation|continuation.*blank"):
        core.validate_repository_recipe_inventory(
            catalog, repository_root=repository_root
        )


def test_repository_recipe_ignores_base_args_inside_run_heredocs(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    source = recipe["base_image"]["source"]
    name_version = recipe["base_image"]["name_version"]

    for case, opener, terminator in (
        ("quoted", "RUN <<'PAYLOAD'\n", "PAYLOAD\n"),
        ("tab-stripped", 'RUN <<-"PAYLOAD"\n', "\tPAYLOAD\n"),
    ):
        repository_root = tmp_path / case
        catalog["docker_recipes"] = [copy.deepcopy(recipe)]
        dockerfile = repository_root / recipe["path"]
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text(
            opener
            + f'ARG IMAGE_SOURCE="{source}"\n'
            + f'ARG IMAGE_NAME_VERSION="{name_version}"\n'
            + terminator
            + "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
            encoding="utf-8",
        )

        try:
            core.validate_repository_recipe_inventory(
                catalog, repository_root=repository_root
            )
        except ValueError as error:
            assert "exactly one IMAGE_SOURCE" in str(error)
        else:
            raise AssertionError(f"{case} heredoc spoofed top-level base ARGs")


def test_repository_recipe_rejects_duplicate_or_conflicting_parser_directives(
    tmp_path: Path,
) -> None:
    source_catalog = core.load_catalog()
    recipe = copy.deepcopy(source_catalog["docker_recipes"][5])
    original = (ROOT / recipe["path"]).read_text(encoding="utf-8")

    for case, second_escape in (("duplicate", "`"), ("conflicting", "\\")):
        repository_root = tmp_path / case
        catalog = copy.deepcopy(source_catalog)
        catalog["docker_recipes"] = [copy.deepcopy(recipe)]
        dockerfile = repository_root / recipe["path"]
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text(
            f"# escape=`\n# escape={second_escape}\n{original}", encoding="utf-8"
        )

        try:
            core.validate_repository_recipe_inventory(
                catalog, repository_root=repository_root
            )
        except ValueError as error:
            assert "parser directive" in str(error)
        else:
            raise AssertionError(f"{case} parser directive was accepted")


def test_repository_recipe_rejects_unterminated_dockerfile_continuation(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        (ROOT / recipe["path"]).read_text(encoding="utf-8")
        + "\nRUN echo never-continued \\",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "unterminated Dockerfile continuation" in str(error)
    else:
        raise AssertionError("unterminated Dockerfile continuation was accepted")


def test_repository_recipe_rejects_unterminated_dockerfile_heredoc(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    recipe = copy.deepcopy(catalog["docker_recipes"][5])
    catalog["docker_recipes"] = [recipe]
    dockerfile = tmp_path / recipe["path"]
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        (ROOT / recipe["path"]).read_text(encoding="utf-8")
        + "\nRUN <<'PAYLOAD'\necho never-closed\n",
        encoding="utf-8",
    )

    try:
        core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
    except ValueError as error:
        assert "unterminated Dockerfile heredoc" in str(error)
    else:
        raise AssertionError("unterminated Dockerfile heredoc was accepted")


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


def test_repository_recipes_have_no_formal_authority_and_formal_plan_stays_generic() -> (
    None
):
    catalog = core.load_catalog()
    plan = registry.resolve_catalog(
        catalog,
        source_sha="0" * 40,
        lane="feature-candidate",
        fixture=core.load_json(
            RELEASE_ROOT / "tests" / "fixtures" / "catalog-registry.json"
        ),
    )

    assert plan["fixture_only"] is True
    assert not any(
        "formal-release" in recipe["lanes"] for recipe in catalog["docker_recipes"]
    )
    assert all("path" not in task for task in plan["image_tasks"])
    assert all("build_mode" not in task for task in plan["image_tasks"])
    assert image.DOCKER_ROOT == RELEASE_ROOT / "docker"
    assert (image.DOCKER_ROOT / "Dockerfile").is_file()
    assert image.implementation_digests()["files"]["Dockerfile"].startswith("sha256:")


def test_pull_request_uses_one_generated_hosted_router_and_no_hardware_execution() -> (
    None
):
    workflow_path = ROOT / ".github" / "workflows" / "pull-request.yml"
    workflow = core.load_yaml(workflow_path)
    jobs = workflow["jobs"]
    text = workflow_path.read_text(encoding="utf-8")

    assert "^\\.github/(workflows/|release/)" in text
    planner = jobs["repository-recipe-plan"]
    assert planner["outputs"]["recipe_matrix"]
    planner_text = str(planner)
    assert "catalog recipe-matrix" in planner_text
    assert "--lane pr-smoke" in planner_text
    assert "hardware-e2e" not in planner_text
    assert "recipe_matrix=$(jq -cS" in planner_text
    assert "{include: .include}" in planner_text

    hosted = jobs["repository-docker-smoke"]
    assert hosted["strategy"]["matrix"] == (
        "${{ fromJSON(needs.repository-recipe-plan.outputs.recipe_matrix) }}"
    )
    assert hosted["runs-on"] == "${{ matrix.runner }}"
    hosted_text = str(hosted)
    assert "catalog select-recipe" in hosted_text
    assert "--expected-catalog-sha256" in hosted_text
    assert "--expected-matrix-sha256" in hosted_text
    assert "--expected-task-sha256" in hosted_text
    assert "${{ steps.recipe.outputs.path }}" in hosted_text
    assert "${{ steps.recipe.outputs.build_args }}" in hosted_text
    assert "${{ steps.recipe.outputs.cache_scope }}" in hosted_text
    assert hosted["permissions"] == {"contents": "read"}
    assert (
        sum(
            str(step.get("uses", "")).startswith("docker/build-push-action@")
            for step in hosted["steps"]
        )
        == 1
    )
    assert not any(
        "login-action" in str(step.get("uses", "")) for step in hosted["steps"]
    )

    assert "test-e2e-pc-a2" not in jobs
    assert "self-hosted" not in text
    assert "docker run" not in text
    assert "--device" not in text
    assert "hardware-e2e" not in text

    for obsolete_job in (
        "test-build-vllm-ascend",
        "test-build-vllm-cuda-v0-20-2",
        "test-build-sglang",
        "test-build-mindie",
    ):
        assert obsolete_job not in jobs
    assert "Dockerfile.ucm-" not in text
    assert "v0.18.0" not in text
    assert "v0.20.2" not in text


def test_pull_request_workflow_keeps_actions_expressions_out_of_shell() -> None:
    workflow = core.load_yaml(ROOT / ".github" / "workflows" / "pull-request.yml")
    interpolated_steps = [
        f"{job_name}/{step.get('name', step.get('id', '<unnamed>'))}"
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if "${{" in str(step.get("run", ""))
    ]

    assert interpolated_steps == []


def test_pull_request_fetch_treats_hostile_branch_ref_as_one_data_argument(
    tmp_path: Path,
) -> None:
    workflow = core.load_yaml(ROOT / ".github" / "workflows" / "pull-request.yml")
    fetch_step = next(
        step
        for step in workflow["jobs"]["pre-check"]["steps"]
        if step.get("id") == "fetch_base_and_pr_branches"
    )
    hostile_ref = "feature/x$(touch${IFS}pwned)"
    subprocess.run(
        ["git", "check-ref-format", "--branch", hostile_ref],
        check=True,
        capture_output=True,
        text=True,
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$#" >> "${GIT_ARGS_LOG}"\n'
        'printf \'%s\\n\' "$@" >> "${GIT_ARGS_LOG}"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    git_log = tmp_path / "git-args.log"
    github_output = tmp_path / "github-output"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "GIT_ARGS_LOG": str(git_log),
            "GITHUB_OUTPUT": str(github_output),
            "BASE_REF": "main",
            "HEAD_REF": hostile_ref,
            "HEAD_REPOSITORY": "owner/repository",
            "BASE_REPOSITORY": "owner/repository",
            "HEAD_REPOSITORY_CLONE_URL": "https://example.invalid/repository.git",
        }
    )

    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", fetch_step["run"]],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert git_log.read_text(encoding="utf-8").splitlines() == [
        "4",
        "fetch",
        "--",
        "origin",
        "main",
        "4",
        "fetch",
        "--",
        "origin",
        hostile_ref,
    ]
    assert github_output.read_text(encoding="utf-8") == (
        f"HEAD_BRANCH_NAME=origin/{hostile_ref}\n"
    )
    assert not (tmp_path / "pwned").exists()


def test_hardware_e2e_workflow_is_dispatch_only_protected_and_catalog_routed() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "hardware-e2e.yml"
    workflow = core.load_yaml(workflow_path)
    text = workflow_path.read_text(encoding="utf-8")

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert "pull_request" not in text
    assert "pull_request_target" not in text
    assert workflow["permissions"] == {"contents": "read"}

    planner = workflow["jobs"]["repository-recipe-plan"]
    planner_text = str(planner)
    assert planner["runs-on"] == "ubuntu-24.04"
    assert "--lane hardware-e2e" in planner_text
    assert "hardware_matrix=$(jq -cS" in planner_text
    assert "{include: .include}" in planner_text
    assert "protected_environment" in planner_text
    assert ".protected_environment" in planner_text
    assert "core.load_catalog" not in planner_text
    assert "[0]" not in planner_text
    assert 'hardware["count"]' not in planner_text

    hardware = workflow["jobs"]["hardware-e2e"]
    hardware_text = str(hardware)
    assert hardware["strategy"]["matrix"] == (
        "${{ fromJSON(needs.repository-recipe-plan.outputs.hardware_matrix) }}"
    )
    assert hardware["runs-on"] == "${{ matrix.runner }}"
    assert hardware["environment"] == (
        "${{ needs.repository-recipe-plan.outputs.protected_environment }}"
    )
    assert "github.event_name == 'workflow_dispatch'" in hardware["if"]
    assert hardware["permissions"] == {"contents": "read"}
    assert "catalog select-recipe" in hardware_text
    assert "--lane hardware-e2e" in hardware_text
    assert "--expected-catalog-sha256" in hardware_text
    assert "--expected-matrix-sha256" in hardware_text
    assert "--expected-task-sha256" in hardware_text
    assert "steps.hardware-recipe.outputs.path" in hardware_text
    assert "selected-hardware-recipe.json" in hardware_text
    assert ".build_args" in hardware_text


def test_hardware_workflow_keeps_github_context_out_of_shell_and_bounds_tag(
    tmp_path: Path,
) -> None:
    workflow = core.load_yaml(ROOT / ".github" / "workflows" / "hardware-e2e.yml")
    interpolated_steps = [
        f"{job_name}/{step.get('name', step.get('id', '<unnamed>'))}"
        for job_name, job in workflow["jobs"].items()
        for step in job["steps"]
        if "${{ github." in str(step.get("run", ""))
    ]
    assert interpolated_steps == []

    version_step = next(
        step
        for step in workflow["jobs"]["hardware-e2e"]["steps"]
        if step.get("id") == "version"
    )
    assert version_step["env"]["SOURCE_SHA"] == "${{ github.sha }}"
    assert version_step["env"]["RUN_NUMBER"] == "${{ github.run_number }}"
    assert "github.ref_name" not in str(version_step)

    output = tmp_path / "github-output"
    sentinel = tmp_path / "must-not-exist"
    environment = os.environ.copy()
    environment.update(
        {
            "TASK_ID": "vllm-ascend-a2-0.18",
            "SOURCE_SHA": "a" * 40,
            "RUN_NUMBER": "42",
            "GITHUB_REF_NAME": f"release'$(touch {sentinel})'",
            "GITHUB_OUTPUT": str(output),
        }
    )
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", version_step["run"]],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    version = output.read_text(encoding="utf-8").removeprefix("version=").strip()
    assert version == "vllm-ascend-a2-0.18-42-aaaaaaa"
    assert len(version) <= 128
    assert not sentinel.exists()

    output.unlink()
    environment["TASK_ID"] = "a" * 128
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", version_step["run"]],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    long_version = output.read_text(encoding="utf-8").removeprefix("version=").strip()
    assert long_version == f"{'a' * 96}-42-aaaaaaa"
    assert len(long_version) <= 128

    output.unlink()
    environment["TASK_ID"] = "unsafe/task"
    rejected = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", version_step["run"]],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert not output.exists()


def test_checked_in_repository_recipe_table_is_rendered_from_catalog() -> None:
    catalog = core.load_catalog()
    generated = core.render_repository_recipe_markdown(catalog)
    path = ROOT / "docs" / "source" / "getting-started" / "docker-recipes.generated.md"

    assert generated.startswith("# Repository Docker recipes\n")
    assert sum(line.startswith("| `") for line in generated.splitlines()) == len(
        catalog["docker_recipes"]
    )
    assert path.read_text(encoding="utf-8") == generated


def test_catalog_cli_renders_recipe_reference(tmp_path: Path) -> None:
    output = tmp_path / "recipes.md"

    assert cli.main(["catalog", "render-recipes", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == core.render_repository_recipe_markdown(
        core.load_catalog()
    )


def test_active_quickstarts_link_to_catalog_generated_recipe_reference() -> None:
    for name in ("quickstart_vllm.md", "quickstart_vllm_ascend.md"):
        text = (ROOT / "docs" / "source" / "getting-started" / name).read_text(
            encoding="utf-8"
        )
        assert "docker-recipes.generated.md" in text
        assert (
            "Check the `docker/` directory for available Dockerfile versions"
            not in text
        )
    index = (ROOT / "docs" / "source" / "index.md").read_text(encoding="utf-8")
    assert "getting-started/docker-recipes.generated" in index


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
