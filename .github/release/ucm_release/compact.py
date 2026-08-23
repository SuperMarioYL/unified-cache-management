"""Compact planning shared by formal Release and inspected PR runtimes."""

from __future__ import annotations

import copy
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from . import builders, runtime as runtime_ops, upstream

ROUTES = frozenset({"pr", "daily", "release"})


def prepare_wheel_source(source_root: Path, distribution: str) -> dict[str, str]:
    """Set the PEP 621 distribution name for one compact Wheel build."""
    if re.fullmatch(r"uc-manager(?:-[a-z0-9]+)*", distribution) is None:
        raise ValueError("compact Wheel distribution name is invalid")
    path = source_root / "pyproject.toml"
    raw = path.read_text(encoding="utf-8")
    document = tomllib.loads(raw)
    if document.get("project", {}).get("name") != "uc-manager":
        raise ValueError("compact Wheel source must start with project.name=uc-manager")
    source = 'name = "uc-manager"'
    if raw.count(source) != 1:
        raise ValueError("compact Wheel project.name is missing or ambiguous")
    updated = raw.replace(source, f'name = "{distribution}"')
    if tomllib.loads(updated).get("project", {}).get("name") != distribution:
        raise ValueError("compact Wheel project.name update failed")
    path.write_text(updated, encoding="utf-8")
    return {"distribution": distribution, "project_file": "pyproject.toml"}


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected a mapping")
    return value


def _backend(catalog: Mapping[str, Any], backend: str) -> dict[str, Any]:
    backends = catalog.get("backends")
    value = backends.get(backend) if isinstance(backends, dict) else None
    if value is None:
        return {
            "status": "blocked",
            "reason": f"{backend} has no UCM native backend policy",
        }
    if not isinstance(value, dict):
        raise ValueError(f"upstream backend {backend!r} platform policy is malformed")
    return value


def _distribution(backend: Mapping[str, Any], runtime_variant: str) -> str:
    exact = backend.get("distribution")
    template = backend.get("distribution_template")
    if isinstance(exact, str) and exact:
        value = exact
    elif isinstance(template, str) and template.count("{runtime_variant}") == 1:
        value = template.replace("{runtime_variant}", runtime_variant)
    else:
        raise ValueError("backend policy has no valid distribution rule")
    if re.fullmatch(r"uc-manager(?:-[a-z0-9]+)*", value) is None:
        raise ValueError("backend policy generated an invalid distribution")
    return value


def _runtime_label(runtime: Mapping[str, Any]) -> str:
    accelerator = str(runtime["accelerator_runtime"])
    os_label = f"{runtime['os_id']} {runtime['os_version']}"
    if accelerator.startswith("cuda-"):
        return f"CUDA {accelerator.removeprefix('cuda-')} · {os_label}"
    return (
        f"CANN {accelerator.removeprefix('cann-')} "
        f"{str(runtime['variant']).upper()} · {os_label}"
    )


def _builder_map(catalog: object) -> dict[str, dict[str, Any]]:
    validated = builders.validate_catalog(catalog)
    if validated.get("schema_version") != 2:
        raise ValueError("upstream-driven planning requires Builder Catalog schema 2")
    result: dict[str, dict[str, Any]] = {}
    for raw in validated["builders"]:  # type: ignore[index]
        item = _mapping(raw, "Builder Catalog item")
        result[str(item["id"])] = item
    return result


def _build_map(selection: Mapping[str, object]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in selection["wheel_builds"]:  # type: ignore[index]
        item = _mapping(raw, "Wheel build")
        result[str(item["id"])] = item
    return result


def _validate_builder_matches_build(
    build: Mapping[str, Any], builder: Mapping[str, Any]
) -> None:
    for field in (
        "source_repository",
        "source_ref",
        "build_group",
        "runtime_variant",
        "backend",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "soc_version",
        "python_version",
        "python_abi",
        "manylinux",
        "cpu_arch",
        "source_image",
        "source_image_digest",
        "build_mode",
        "mooncake_version",
        "recipe_revision",
        "sync_mode",
    ):
        if builder.get(field) != build.get(field):
            raise ValueError(
                f"{build['id']}: Builder {field} does not match Wheel recipe"
            )


def _wheel_task(
    catalog: Mapping[str, Any],
    build: Mapping[str, Any],
    builder: Mapping[str, Any],
    backend: Mapping[str, Any],
) -> dict[str, Any]:
    architecture = str(build["cpu_arch"])
    requirements = _mapping(catalog.get("requirements"), "formal requirements")
    return {
        "id": str(build["id"]),
        "label": (f"{build['build_group']} · {build['python_abi']} · {architecture}"),
        "runner": catalog["runners"][architecture],  # type: ignore[index]
        "profile_id": str(build["build_group"]),
        "group_id": str(build["build_group"]),
        "backend": str(build["backend"]),
        "runtime_variant": str(build["runtime_variant"]),
        "cpu_arch": architecture,
        "platform": f"linux/{architecture}",
        "python_version": str(build["python_version"]),
        "python_abi": str(build["python_abi"]),
        "manylinux": str(build["manylinux"]),
        "wheel_version": catalog["ucm_version"],
        "dist_name": _distribution(backend, str(build["runtime_variant"])),
        "build": {"docker_target": "wheel", "platform_arg": backend["platform"]},
        "builder": {
            "repository": str(builder["target_repository"]),
            "tag": str(builder["target_tag"]),
            "recipe_revision": str(builder["recipe_revision"]),
        },
        "build_requirements": copy.deepcopy(requirements["wheel_build"]),
        "runtime_requirements": copy.deepcopy(requirements["wheel_runtime"]),
    }


def _selected_runtimes(
    selection: Mapping[str, object], pinned_upstreams: list[str] | None
) -> list[dict[str, Any]]:
    values = [
        _mapping(item, "upstream runtime")
        for item in selection["runtimes"]  # type: ignore[index]
    ]
    if not pinned_upstreams:
        return values
    pinned = set(pinned_upstreams)
    selected = [
        item
        for item in values
        if f"{item['runtime_repository']}:{item['runtime_tag']}" in pinned
    ]
    if len(
        {f"{item['runtime_repository']}:{item['runtime_tag']}" for item in selected}
    ) != len(pinned):
        raise ValueError("pinned runtime references are not in the inspected selection")
    return selected


def resolve_plan(
    catalog: dict[str, Any],
    *,
    builder_catalog: dict[str, Any],
    upstream_selection: dict[str, Any],
    route: str,
    pinned_upstreams: list[str] | None = None,
) -> dict[str, Any]:
    """Generate Wheel/Image tasks from one normalized upstream selection."""
    if route not in ROUTES:
        raise ValueError(f"unsupported release route: {route}")
    selection = upstream.validate_selection(upstream_selection)
    builds = _build_map(selection)
    builder_by_id = _builder_map(builder_catalog)
    wheels_by_id: dict[str, dict[str, Any]] = {}
    images: list[dict[str, Any]] = []
    families: list[dict[str, Any]] = []
    formal_selection = all(
        runtime.get("channel") in {"stable", "rc"}
        for runtime in selection["runtimes"]  # type: ignore[index]
        if isinstance(runtime, dict)
    )
    if formal_selection:
        for build_id, build in builds.items():
            backend = _backend(catalog, str(build["backend"]))
            if backend.get("status") == "blocked":
                continue
            builder = builder_by_id.get(build_id)
            if builder is None:
                raise ValueError(f"matching Builder {build_id!r} is missing")
            _validate_builder_matches_build(build, builder)
            wheels_by_id[build_id] = _wheel_task(catalog, build, builder, backend)

    for runtime in _selected_runtimes(selection, pinned_upstreams):
        backend = _backend(catalog, str(runtime["backend"]))
        if backend.get("status") == "blocked":
            continue
        if backend.get("status") != "supported":
            raise ValueError(f"backend {runtime['backend']!r} has an invalid status")
        wheel_ids = _mapping(runtime["wheel_build_ids"], f"{runtime['id']} Wheel links")
        image_ids: list[str] = []
        member_records: list[dict[str, str]] = []
        for architecture in runtime["architectures"]:
            build_id = str(wheel_ids[str(architecture)])
            build = builds.get(build_id)
            builder = builder_by_id.get(build_id)
            if build is None or builder is None:
                raise ValueError(
                    f"{runtime['id']}: matching Wheel/Builder {build_id!r} is missing"
                )
            _validate_builder_matches_build(build, builder)
            if build_id not in wheels_by_id:
                wheels_by_id[build_id] = _wheel_task(catalog, build, builder, backend)
            image_id = f"{runtime['id']}-{architecture}"
            image_ids.append(image_id)
            member_reference = (
                f"{runtime['target_repository']}:{runtime['target_tag']}-{architecture}"
            )
            member_records.append(
                {
                    "image_id": image_id,
                    "cpu_arch": str(architecture),
                    "reference": member_reference,
                }
            )
            images.append(
                {
                    "id": image_id,
                    "label": f"{_runtime_label(runtime)} · {architecture}",
                    "runner": catalog["runners"][architecture],
                    "wheel_id": build_id,
                    "family_id": str(runtime["id"]),
                    "profile_id": str(runtime["id"]),
                    "cpu_arch": str(architecture),
                    "platform": f"linux/{architecture}",
                    "runtime": {
                        "product_id": runtime["product_id"],
                        "repository": runtime["runtime_repository"],
                        "tag": runtime["runtime_tag"],
                        "image_reference": runtime["member_references"][architecture],
                        "version": runtime["version"],
                        "channel": runtime["channel"],
                        "variant": runtime["variant"],
                        "accelerator_runtime": runtime["accelerator_runtime"],
                        "soc_version": runtime["soc_version"],
                        "python_version": runtime["python_version"],
                        "python_abi": runtime["python_abi"],
                        "os_id": runtime["os_id"],
                        "os_version": runtime["os_version"],
                    },
                    "target_repository": runtime["target_repository"],
                    "target_tag": runtime["target_tag"],
                    "build_requirements": copy.deepcopy(
                        catalog["requirements"]["wheel_runtime"]
                    ),
                }
            )
        creates_index = len(member_records) > 1
        published_reference = (
            f"{runtime['target_repository']}:{runtime['target_tag']}"
            if creates_index
            else member_records[0]["reference"]
        )
        families.append(
            {
                "id": str(runtime["id"]),
                "label": _runtime_label(runtime),
                "product_id": runtime["product_id"],
                "variant": runtime["variant"],
                "runtime": {
                    "repository": runtime["runtime_repository"],
                    "tag": runtime["runtime_tag"],
                    "version": runtime["version"],
                    "channel": runtime["channel"],
                    "python_abi": runtime["python_abi"],
                    "os_id": runtime["os_id"],
                    "os_version": runtime["os_version"],
                },
                "target_repository": runtime["target_repository"],
                "target_tag": runtime["target_tag"],
                "image_ids": sorted(image_ids),
                "members": sorted(member_records, key=lambda item: item["cpu_arch"]),
                "create_index": creates_index,
                "published_reference": published_reference,
            }
        )

    wheels = sorted(wheels_by_id.values(), key=lambda item: item["id"])
    images.sort(key=lambda item: item["id"])
    families.sort(key=lambda item: item["id"])
    if not wheels or not images or not families:
        raise ValueError("release plan has no supported Wheel/runtime families")
    limits = catalog["matrix_limits"]
    for key, values, limit_key in (
        ("wheels", wheels, "max_wheel_tasks"),
        ("images", images, "max_image_tasks"),
        ("families", families, "max_family_tasks"),
    ):
        if len(values) > int(limits[limit_key]):
            raise ValueError(f"release plan {key} count exceeds configured limit")
    distributions = sorted({str(item["dist_name"]) for item in wheels})
    publish = copy.deepcopy(catalog["publish"])
    publish["pypi"]["distributions"] = distributions
    return {
        "kind": "ucm-release-plan",
        "route": route,
        "version": catalog["ucm_version"],
        "release_tag": catalog["release_tag"],
        "publish": publish,
        "chart": copy.deepcopy(catalog["chart"]),
        "wheels": wheels,
        "images": images,
        "families": families,
        "wheel_matrix": {
            "include": [
                {key: task[key] for key in ("id", "label", "runner")} for task in wheels
            ]
        },
        "image_matrix": {
            "include": [
                {key: task[key] for key in ("id", "label", "runner", "wheel_id")}
                for task in images
            ]
        },
    }


def retag_pr_plan(
    plan: Mapping[str, Any],
    *,
    pr_number: int | str,
    author: str,
    run_id: int | str,
) -> dict[str, Any]:
    """Project collision-resistant PR targets without changing build tasks."""
    result = copy.deepcopy(_mapping(plan, "release plan"))
    if result.get("kind") != "ucm-release-plan" or result.get("route") != "pr":
        raise ValueError("PR retagging requires a compact PR plan")
    families = {
        str(item["id"]): item
        for item in result.get("families", [])
        if isinstance(item, dict)
    }
    for family in families.values():
        base_tag = runtime_ops.project_pr_tag(
            str(family["runtime"]["tag"]),
            pr_number=pr_number,
            author=author,
            run_id=run_id,
        )
        family["target_tag"] = base_tag
        for member in family["members"]:
            member["reference"] = (
                f"{family['target_repository']}:{base_tag}-{member['cpu_arch']}"
            )
        family["published_reference"] = (
            f"{family['target_repository']}:{base_tag}"
            if family["create_index"]
            else family["members"][0]["reference"]
        )
    for image in result.get("images", []):
        if not isinstance(image, dict):
            raise ValueError("release plan images must be mappings")
        family = families.get(str(image["family_id"]))
        if family is None:
            raise ValueError(f"image {image['id']} references an unknown family")
        image["target_tag"] = family["target_tag"]
    return result


def select_task(plan: Mapping[str, Any], kind: str, task_id: str) -> dict[str, Any]:
    collection = {"wheel": "wheels", "image": "images"}.get(kind)
    if collection is None:
        raise ValueError(f"unsupported task kind: {kind}")
    matches = [item for item in plan.get(collection, []) if item.get("id") == task_id]
    if len(matches) != 1:
        raise ValueError(f"{kind} task {task_id!r} does not resolve exactly once")
    return copy.deepcopy(matches[0])


def record_wheel_result(task: Mapping[str, Any], wheel_path: Path) -> dict[str, Any]:
    """Validate one built Wheel and record its auditwheel-visible coordinates."""
    if not wheel_path.is_file():
        raise ValueError(f"Wheel result is missing: {wheel_path}")
    try:
        distribution, version, _build, tags = parse_wheel_filename(wheel_path.name)
    except ValueError as error:
        raise ValueError(f"invalid Wheel filename {wheel_path.name!r}") from error
    if canonicalize_name(str(distribution)) != canonicalize_name(
        str(task["dist_name"])
    ):
        raise ValueError("built Wheel distribution does not match its task")
    if str(version) != str(task["wheel_version"]):
        raise ValueError("built Wheel version does not match its task")
    python_abi = str(task["python_abi"])
    if not any(tag.interpreter == python_abi and tag.abi == python_abi for tag in tags):
        raise ValueError("built Wheel ABI does not match its task")
    architecture = str(task["cpu_arch"])
    tokens = {"amd64": ("x86_64", "amd64"), "arm64": ("aarch64", "arm64")}[architecture]
    if not any(any(token in tag.platform for token in tokens) for tag in tags):
        raise ValueError("built Wheel architecture does not match its task")
    return {
        "kind": "ucm-wheel-result",
        "schema_version": 1,
        "task_id": task["id"],
        "distribution": task["dist_name"],
        "version": task["wheel_version"],
        "python_abi": python_abi,
        "cpu_arch": architecture,
        "filename": wheel_path.name,
        "platform_tags": sorted({tag.platform for tag in tags}),
    }
