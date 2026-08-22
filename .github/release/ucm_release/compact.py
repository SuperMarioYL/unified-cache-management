"""Compact upstream-driven release planning for the active Actions lane."""

from __future__ import annotations

import copy
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from . import builders, core, upstream

ROUTES = frozenset({"pr", "daily", "release"})


def prepare_wheel_source(source_root: Path, distribution: str) -> dict[str, str]:
    """Set the PEP 621 distribution name for one compact wheel build."""
    if re.fullmatch(r"uc-manager(?:-[a-z0-9]+)*", distribution) is None:
        raise ValueError("compact wheel distribution name is invalid")
    path = source_root / "pyproject.toml"
    raw = path.read_text(encoding="utf-8")
    document = tomllib.loads(raw)
    if document.get("project", {}).get("name") != "uc-manager":
        raise ValueError("compact wheel source must start with project.name=uc-manager")
    source = 'name = "uc-manager"'
    if raw.count(source) != 1:
        raise ValueError("compact wheel project.name is missing or ambiguous")
    updated = raw.replace(source, f'name = "{distribution}"')
    if tomllib.loads(updated).get("project", {}).get("name") != distribution:
        raise ValueError("compact wheel project.name update failed")
    path.write_text(updated, encoding="utf-8")
    return {"distribution": distribution, "project_file": "pyproject.toml"}


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected a mapping")
    return value


def _contract(catalog: Mapping[str, Any], backend: str) -> dict[str, Any]:
    contracts = catalog.get("backend_contracts")
    contract = contracts.get(backend) if isinstance(contracts, dict) else None
    if not isinstance(contract, dict):
        raise ValueError(f"upstream backend {backend!r} has no UCM native contract")
    return contract


def _distribution(contract: Mapping[str, Any], runtime_variant: str) -> str:
    exact = contract.get("distribution")
    prefix = contract.get("distribution_prefix")
    if isinstance(exact, str) and exact:
        return exact
    if isinstance(prefix, str) and prefix:
        value = prefix + runtime_variant
        if re.fullmatch(r"uc-manager(?:-[a-z0-9]+)*", value):
            return value
    raise ValueError("backend contract has no valid distribution rule")


def _runtime_label(runtime: str, variant: str) -> str:
    if runtime.startswith("cuda-"):
        return f"CUDA {runtime.removeprefix('cuda-')}"
    version = runtime.removeprefix("cann-")
    return f"CANN {version} {variant.upper()}"


def _builder_map(catalog: object) -> dict[str, dict[str, Any]]:
    validated = builders.validate_catalog(catalog)
    if validated.get("schema_version") != 2:
        raise ValueError("upstream-driven planning requires Builder Catalog schema 2")
    result: dict[str, dict[str, Any]] = {}
    for raw in validated["builders"]:  # type: ignore[index]
        item = _mapping(raw, "Builder Catalog item")
        result[str(item["id"])] = item
    return result


def _wheel_task(
    catalog: dict[str, Any],
    group: Mapping[str, Any],
    builder: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    task_id = str(group["id"])
    architecture = str(group["cpu_arch"])
    python_abi = str(group["python_abi"])
    build_requirements = [
        item["requirement"]
        for item in core.build_tool_dependency_records(
            catalog, python_abi, architecture
        )
    ]
    return {
        "id": task_id,
        "label": (
            f"{_runtime_label(str(group['accelerator_runtime']), str(group['variant']))}"
            f" · {python_abi} · {architecture}"
        ),
        "runner": catalog["runner_map"][architecture],
        "profile_id": str(group["build_group"]),
        "group_id": str(group["build_group"]),
        "backend": str(group["backend"]),
        "runtime_variant": str(group["runtime_variant"]),
        "cpu_arch": architecture,
        "platform": f"linux/{architecture}",
        "python_version": str(group["python_version"]),
        "python_abi": python_abi,
        "wheel_version": catalog["ucm_version"],
        "dist_name": _distribution(contract, str(group["runtime_variant"])),
        "build": {"docker_target": "wheel", "platform_arg": contract["platform_arg"]},
        "builder": {
            "repository": str(builder["target_repository"]),
            "tag": str(builder["target_tag"]),
        },
        "build_requirements": build_requirements,
        "runtime_requirements": core.python_runtime_requirements(catalog),
        "required_native": copy.deepcopy(contract["required_native"]),
        "forbidden_native": copy.deepcopy(contract["forbidden_native"]),
        "external_required_dependencies": copy.deepcopy(
            contract["external_required_dependencies"]
        ),
    }


def _selected_upstreams(
    selection: Mapping[str, object], pinned_upstreams: list[str] | None
) -> list[dict[str, Any]]:
    values = [
        _mapping(item, "upstream selection item")
        for item in selection["upstreams"]  # type: ignore[index]
    ]
    if not pinned_upstreams:
        return values
    pinned = set(pinned_upstreams)
    selected = [
        item
        for item in values
        if f"{item['runtime_repository']}:{item['runtime_tag']}" in pinned
    ]
    if len(selected) != len(pinned):
        available = sorted(
            f"{item['runtime_repository']}:{item['runtime_tag']}" for item in values
        )
        raise ValueError(
            f"pinned upstreams are not in the resolved selection; available={available}"
        )
    return selected


def resolve_plan(
    catalog: dict[str, Any],
    *,
    builder_catalog: dict[str, Any],
    upstream_selection: dict[str, Any],
    route: str,
    pinned_upstreams: list[str] | None = None,
) -> dict[str, Any]:
    """Generate Wheel/Image tasks only from one immutable upstream selection."""
    if route not in ROUTES:
        raise ValueError(f"unsupported release route: {route}")
    selection = upstream.validate_selection(upstream_selection)
    builder_by_id = _builder_map(builder_catalog)
    wheels_by_id: dict[str, dict[str, Any]] = {}
    images: list[dict[str, Any]] = []
    families: list[dict[str, Any]] = []
    for selected in _selected_upstreams(selection, pinned_upstreams):
        family_id = str(selected["family_id"])
        integration_abi = str(selected["integration_python_abi"])
        groups = [
            _mapping(item, f"{family_id} build group")
            for item in selected["build_groups"]
        ]
        image_ids: list[str] = []
        for group in groups:
            builder = builder_by_id.get(str(group["id"]))
            if builder is None:
                raise ValueError(f"{group['id']}: matching Builder is missing")
            for field in (
                "source_ref",
                "build_group",
                "runtime_variant",
                "backend",
                "python_version",
                "python_abi",
                "manylinux",
                "cpu_arch",
            ):
                expected = (
                    selected["source_ref"] if field == "source_ref" else group[field]
                )
                if builder.get(field) != expected:
                    raise ValueError(
                        f"{group['id']}: Builder {field} does not match upstream recipe"
                    )
            contract = _contract(catalog, str(group["backend"]))
            wheel = _wheel_task(catalog, group, builder, contract)
            if wheel["id"] in wheels_by_id:
                raise ValueError(f"duplicate Wheel task {wheel['id']!r}")
            wheels_by_id[wheel["id"]] = wheel
        for architecture in sorted(catalog["runner_map"]):
            candidates = [
                group
                for group in groups
                if group["python_abi"] == integration_abi
                and group["cpu_arch"] == architecture
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"{family_id}: integration ABI {integration_abi}/{architecture} "
                    "must resolve exactly one Wheel"
                )
            group = candidates[0]
            image_id = f"{family_id}-{architecture}"
            image_ids.append(image_id)
            images.append(
                {
                    "id": image_id,
                    "label": (
                        f"{_runtime_label(str(group['accelerator_runtime']), str(group['variant']))}"
                        f" · {architecture}"
                    ),
                    "runner": catalog["runner_map"][architecture],
                    "wheel_id": str(group["id"]),
                    "family_id": family_id,
                    "profile_id": family_id,
                    "cpu_arch": architecture,
                    "platform": f"linux/{architecture}",
                    "runtime": {
                        "product_id": selected["product_id"],
                        "repository": selected["runtime_repository"],
                        "tag": selected["runtime_tag"],
                        "version": selected["version"],
                        "channel": selected["channel"],
                        "variant": selected["runtime_variant"],
                        "python_abi": integration_abi,
                    },
                    "target_repository": selected["target_repository"],
                    "target_tag": selected["target_tag"],
                    "runtime_requirements": core.python_runtime_requirements(catalog),
                }
            )
        families.append(
            {
                "id": family_id,
                "label": _runtime_label(
                    str(groups[0]["accelerator_runtime"]), str(groups[0]["variant"])
                ),
                "product_id": selected["product_id"],
                "variant": selected["runtime_variant"],
                "runtime": {
                    "repository": selected["runtime_repository"],
                    "tag": selected["runtime_tag"],
                    "version": selected["version"],
                    "channel": selected["channel"],
                    "python_abi": integration_abi,
                },
                "target_repository": selected["target_repository"],
                "target_tag": selected["target_tag"],
                "image_ids": sorted(image_ids),
            }
        )
    wheels = sorted(wheels_by_id.values(), key=lambda item: item["id"])
    images.sort(key=lambda item: item["id"])
    families.sort(key=lambda item: item["id"])
    limits = catalog["matrix_limits"]
    counts = {
        "wheels": len(wheels),
        "images": len(images),
        "families": len(families),
    }
    for key, limit_key in (
        ("wheels", "max_wheel_tasks"),
        ("images", "max_image_tasks"),
        ("families", "max_family_tasks"),
    ):
        if not counts[key] or counts[key] > int(limits[limit_key]):
            raise ValueError(f"release plan {key} count is outside configured limits")
    distributions = sorted({str(item["dist_name"]) for item in wheels})
    publish = copy.deepcopy(catalog["publish"])
    publish["pypi"]["distributions"] = distributions
    return {
        "kind": "ucm-release-plan",
        "route": route,
        "version": catalog["ucm_version"],
        "release_tag": catalog["source"]["release_tag"],
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
    architecture_tokens = {
        "amd64": ("x86_64", "amd64"),
        "arm64": ("aarch64", "arm64"),
    }[architecture]
    if not any(
        any(token in tag.platform for token in architecture_tokens) for tag in tags
    ):
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
