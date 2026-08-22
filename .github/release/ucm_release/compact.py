"""Compact, tag-based release planning for the active GitHub Actions lane."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packaging.version import Version

from . import builders, core, registry

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


def _runtime_name(value: str) -> str:
    name, _, version = value.partition("-")
    if name == "cuda":
        return f"CUDA {version}"
    if name == "cann":
        parts = version.split(".")
        return "CANN " + ".".join(parts[:2])
    return value


def _profile_label(profile: Mapping[str, Any]) -> str:
    runtime = _runtime_name(str(profile["accelerator_runtime"]))
    npu_arch = profile.get("npu_arch")
    if isinstance(npu_arch, list) and npu_arch and npu_arch[0] != "na":
        return f"{runtime} {str(npu_arch[0]).upper()}"
    return runtime


def _product_label(product_id: str) -> str:
    return "vLLM Ascend" if product_id == "vllm-ascend" else "vLLM"


def _live_tag_lists(catalog: Mapping[str, Any]) -> dict[str, list[str]]:
    limit = int(catalog["scan_limits"]["max_tags_per_repository"])
    crane = shutil.which("crane")
    if crane is None:
        raise ValueError("compact release planning requires crane on PATH")
    result: dict[str, list[str]] = {}
    for product in catalog["upstream_products"]:
        repository = product["repository"]
        completed = subprocess.run(
            [crane, "ls", repository],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"cannot list upstream tags for {repository}: "
                f"{completed.stderr.strip() or completed.returncode}"
            )
        tags = sorted(set(completed.stdout.splitlines()))
        if len(tags) > limit:
            raise ValueError(f"upstream tag limit {limit} exceeded for {repository}")
        result[repository] = tags
    return result


def _select_upstreams(
    catalog: dict[str, Any], tag_lists: Mapping[str, list[str]]
) -> list[dict[str, str]]:
    candidates, _ = registry.select_catalog_tags(catalog, dict(tag_lists))
    products = {item["id"]: item for item in catalog["upstream_products"]}
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for candidate in candidates:
        reason = core.candidate_exclusion_reason(
            catalog,
            products[candidate["product_id"]],
            candidate,
        )
        if reason is None:
            grouped.setdefault(
                (candidate["product_id"], candidate["variant"]), []
            ).append(candidate)

    selected: list[dict[str, str]] = []
    for coordinate, values in sorted(grouped.items()):
        chosen = max(values, key=lambda item: (Version(item["version"]), item["tag"]))
        product = products[coordinate[0]]
        selected.append(
            {
                **chosen,
                "target_repository": product["target_repository"],
                "target_tag": chosen["tag"] + product["target_tag_suffix"],
            }
        )
    return selected


def _inspect_pinned_variant(product: dict[str, Any], reference: str) -> str:
    if len(product["variants"]) == 1:
        return product["variants"][0]["id"]
    crane = shutil.which("crane")
    if crane is None:
        raise ValueError("pinned multi-variant images require crane on PATH")
    completed = subprocess.run(
        [crane, "config", "--platform", "linux/amd64", reference],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"cannot inspect pinned upstream {reference}: "
            f"{completed.stderr.strip() or completed.returncode}"
        )
    try:
        config = __import__("json").loads(completed.stdout)
    except ValueError as error:
        raise ValueError(
            f"pinned upstream {reference} returned invalid config"
        ) from error
    environment = ((config.get("config") or {}).get("Env")) or []
    soc = next(
        (
            entry.split("=", 1)[1]
            for entry in environment
            if entry.startswith("SOC_VERSION=")
        ),
        None,
    )
    variant = registry._variant_by_soc(product, soc) if soc else None
    if variant is None:
        raise ValueError(f"pinned upstream {reference} has no recognized variant")
    return variant


def _select_pinned_upstreams(
    catalog: dict[str, Any], references: list[str]
) -> list[dict[str, str]]:
    products = {item["repository"]: item for item in catalog["upstream_products"]}
    selected: list[dict[str, str]] = []
    for reference in references:
        repository, separator, tag = reference.rpartition(":")
        if not separator or repository not in products or not tag:
            raise ValueError(f"unsupported pinned upstream: {reference}")
        product = products[repository]
        try:
            parsed = registry._parse_product_tag(product, tag)
        except ValueError:
            loose = registry._loose_tag_version_channel(tag)
            version, channel = loose or (
                tag.removeprefix("v"),
                product["channels"][0],
            )
            parsed = {
                "tag": tag,
                "version": version,
                "channel": channel,
                "variant": _inspect_pinned_variant(product, reference),
            }
        selected.append(
            {
                "product_id": product["id"],
                "repository": repository,
                **parsed,
                "target_repository": product["target_repository"],
                "target_tag": tag + product["target_tag_suffix"],
            }
        )
    return selected


def _wheel_task(
    catalog: dict[str, Any], profile: dict[str, Any], architecture: str
) -> dict[str, Any]:
    wheel_id = f"{profile['id']}-{architecture}"
    label = f"{_profile_label(profile)} · {architecture}"
    root = profile["builders"][architecture]["root"]
    build_requirements = [
        item["requirement"]
        for item in core.build_tool_dependency_records(
            catalog, profile["python_abi"], architecture
        )
    ]
    return {
        "id": wheel_id,
        "label": label,
        "runner": catalog["runner_map"][architecture],
        "profile_id": profile["id"],
        "cpu_arch": architecture,
        "platform": f"linux/{architecture}",
        "python_version": profile["python_version"],
        "python_abi": profile["python_abi"],
        "wheel_version": profile["wheel_version"],
        "wheel_platform": profile["wheel_platform"],
        "dist_name": profile["dist_name"],
        "build": copy.deepcopy(profile["build"]),
        "builder": {"repository": root["repository"], "tag": root["tag"]},
        "build_requirements": build_requirements,
        "runtime_requirements": core.python_runtime_requirements(catalog),
        "required_native": copy.deepcopy(profile["required_native"]),
        "forbidden_native": copy.deepcopy(profile["forbidden_native"]),
        "external_required_dependencies": copy.deepcopy(
            profile["external_required_dependencies"]
        ),
    }


def resolve_plan(
    catalog: dict[str, Any],
    *,
    builder_catalog: dict[str, Any],
    route: str,
    tag_lists: Mapping[str, list[str]] | None = None,
    pinned_upstreams: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve one build plan without persisting commit, task, or OCI digests."""
    if route not in ROUTES:
        raise ValueError(f"unsupported release route: {route}")
    selection = builders.select_builders(builder_catalog, catalog)
    bound = builders.bind_selection(catalog, selection)
    upstreams = (
        _select_pinned_upstreams(bound, pinned_upstreams)
        if pinned_upstreams
        else _select_upstreams(bound, tag_lists or _live_tag_lists(bound))
    )
    relaxed = bool(pinned_upstreams)
    products = {item["id"]: item for item in bound["upstream_products"]}
    wheels_by_id: dict[str, dict[str, Any]] = {}
    images: list[dict[str, Any]] = []
    families: list[dict[str, Any]] = []
    for upstream in upstreams:
        product = products[upstream["product_id"]]
        family_id = f"{upstream['product_id']}-{upstream['variant']}"
        image_ids: list[str] = []
        for architecture in sorted(product["required_cpu_architectures"]):
            profile, rule = core._matching_profile(
                bound, product, upstream, architecture, relaxed=relaxed
            )
            wheel_id = f"{profile['id']}-{architecture}"
            wheels_by_id.setdefault(wheel_id, _wheel_task(bound, profile, architecture))
            image_id = f"{family_id}-{architecture}"
            image_ids.append(image_id)
            images.append(
                {
                    "id": image_id,
                    "label": (
                        f"{_product_label(upstream['product_id'])} · "
                        f"{_profile_label(profile)} · {architecture}"
                    ),
                    "runner": bound["runner_map"][architecture],
                    "wheel_id": wheel_id,
                    "family_id": family_id,
                    "profile_id": profile["id"],
                    "cpu_arch": architecture,
                    "platform": f"linux/{architecture}",
                    "compatibility_rule": rule["id"],
                    "runtime": {
                        key: upstream[key]
                        for key in (
                            "product_id",
                            "repository",
                            "tag",
                            "version",
                            "channel",
                            "variant",
                        )
                    },
                    "target_repository": upstream["target_repository"],
                    "target_tag": upstream["target_tag"],
                    "runtime_requirements": core.python_runtime_requirements(bound),
                }
            )
        families.append(
            {
                "id": family_id,
                "label": _product_label(upstream["product_id"]),
                "product_id": upstream["product_id"],
                "variant": upstream["variant"],
                "runtime": {
                    key: upstream[key]
                    for key in ("repository", "tag", "version", "channel")
                },
                "target_repository": upstream["target_repository"],
                "target_tag": upstream["target_tag"],
                "image_ids": image_ids,
            }
        )

    wheels = sorted(wheels_by_id.values(), key=lambda item: item["id"])
    images.sort(key=lambda item: item["id"])
    families.sort(key=lambda item: item["id"])
    if not wheels or not images or not families:
        raise ValueError("release plan resolved no build tasks")
    return {
        "kind": "ucm-release-plan",
        "route": route,
        "version": bound["ucm_version"],
        "release_tag": bound["source"]["release_tag"],
        "publish": copy.deepcopy(bound["publish"]),
        "chart": copy.deepcopy(bound["chart"]),
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
