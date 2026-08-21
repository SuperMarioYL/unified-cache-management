"""Product-template compilation and Task 2 build-name projections."""

from __future__ import annotations

import copy
import re
import string
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from packaging.utils import canonicalize_name

from .capabilities import (
    compact_accelerator_runtime,
    compact_mooncake_version,
    normalize_variant,
    python_version_from_abi,
)

_TEMPLATE_FIELDS = {
    "cuda": frozenset({"runtime.compact"}),
    "cann": frozenset({"runtime.compact", "variant", "mooncake.compact"}),
}
_FAMILY_ACCELERATOR = {"cuda": "cuda", "cann": "ascend"}
_DISTRIBUTION = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", re.ASCII)


@dataclass(frozen=True, slots=True)
class CompiledDistributionTemplate:
    """A validated family-specific Distribution template."""

    family: str
    template: str
    fields: frozenset[str]

    def render(self, values: Mapping[str, str]) -> str:
        rendered = self.template
        for field in self.fields:
            value = values.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"template variable {field} expanded empty")
            rendered = rendered.replace("{" + field + "}", value)
        if (
            _DISTRIBUTION.fullmatch(rendered) is None
            or canonicalize_name(rendered) != rendered
        ):
            raise ValueError("expanded Distribution is not a canonical PEP 503 name")
        return rendered


def compile_distribution_template(
    family: str, template: str
) -> CompiledDistributionTemplate:
    """Compile one closed CUDA or CANN Distribution template."""
    allowed = _TEMPLATE_FIELDS.get(family)
    if allowed is None:
        raise ValueError("Distribution template family must be cuda or cann")
    if not isinstance(template, str) or not template:
        raise ValueError("Distribution template must be a non-empty string")
    fields: set[str] = set()
    try:
        parsed = tuple(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError("Distribution template is malformed") from exc
    for _, field, format_spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed:
            raise ValueError(f"unknown Distribution template variable {field}")
        if format_spec or conversion:
            raise ValueError("Distribution template formatting is not allowed")
        fields.add(field)
    missing = sorted(allowed - fields)
    if missing:
        raise ValueError(
            f"Distribution template is missing required variables {missing}"
        )
    return CompiledDistributionTemplate(family, template, frozenset(fields))


def _product_family(
    product_rules: Mapping[str, object], accelerator: object
) -> tuple[str, Mapping[str, object]]:
    matches: list[tuple[str, Mapping[str, object]]] = []
    for family, value in product_rules.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"products.{family} must be an object")
        if value.get("accelerator") == accelerator:
            matches.append((family, value))
    if len(matches) != 1:
        raise ValueError("capability accelerator must select exactly one product rule")
    return matches[0]


def _compile_product_rules(
    product_rules: Mapping[str, object],
) -> dict[str, CompiledDistributionTemplate]:
    compiled: dict[str, CompiledDistributionTemplate] = {}
    for family, raw_rule in product_rules.items():
        if not isinstance(raw_rule, Mapping):
            raise ValueError(f"products.{family} must be an object")
        if set(raw_rule) != {"accelerator", "distribution"}:
            raise ValueError(f"products.{family} fields must be exact")
        if raw_rule["accelerator"] != _FAMILY_ACCELERATOR.get(family):
            raise ValueError(f"products.{family}.accelerator is invalid")
        compiled[family] = compile_distribution_template(
            family, raw_rule["distribution"]
        )
    return compiled


def _render_distribution_name(
    *,
    product_rules: Mapping[str, object],
    compiled: Mapping[str, CompiledDistributionTemplate],
    context: Mapping[str, object],
) -> str:
    family, _ = _product_family(product_rules, context.get("accelerator"))
    values = {
        "runtime.compact": compact_accelerator_runtime(
            context.get("accelerator_runtime")
        )
    }
    if family == "cann":
        values["variant"] = normalize_variant(context.get("variant"))
        values["mooncake.compact"] = compact_mooncake_version(
            context.get("mooncake_version")
        )
    return compiled[family].render(values)


def expand_distribution_names(
    *,
    product_rules: Mapping[str, object],
    capability_contexts: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Project contexts in deterministic Distribution/Python-ABI order."""
    compiled = _compile_product_rules(product_rules)

    coordinates: list[tuple[str, str | None]] = []
    for context in capability_contexts:
        distribution = _render_distribution_name(
            product_rules=product_rules,
            compiled=compiled,
            context=context,
        )
        python_abi = context.get("python_abi")
        if python_abi is not None:
            python_version_from_abi(python_abi)
        coordinates.append((distribution, python_abi))
    ordered = sorted(coordinates, key=lambda item: (item[0], item[1] or ""))
    if len(ordered) != len(set(ordered)):
        raise ValueError("duplicate expanded Distribution coordinate")
    return tuple(distribution for distribution, _ in ordered)


def _normalize_variants(value: object, *, location: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
    ):
        raise ValueError(f"{location} must be a non-empty sequence")
    variants = tuple(normalize_variant(item) for item in value)
    if len(variants) != len(set(variants)):
        raise ValueError(f"{location} contains duplicate normalized variants")
    return variants


def _operating_systems_for_accelerator(
    rules: Sequence[Mapping[str, Any]], accelerator: str
) -> list[str]:
    matches = [rule for rule in rules if rule.get("accelerator") == accelerator]
    if not matches:
        raise ValueError(
            f"capability has no compatibility policy for accelerator {accelerator}"
        )
    return sorted(
        {
            operating_system
            for rule in matches
            for operating_system in rule["operating_systems"]
        }
    )


def _matching_native_contract(
    contracts: Sequence[Mapping[str, Any]],
    *,
    accelerator: str,
    runtime: str,
    variant: str,
) -> Mapping[str, Any]:
    matches = []
    for contract in contracts:
        if contract.get("accelerator") != accelerator:
            continue
        if contract.get("accelerator_runtime") not in {None, runtime}:
            continue
        variants = contract.get("variants")
        if variants is not None and variant not in _normalize_variants(
            variants, location="native contract variants"
        ):
            continue
        matches.append(contract)
    if len(matches) != 1:
        raise ValueError(
            "capability must match exactly one builder/native fact: "
            f"{accelerator}/{runtime}/{variant}"
        )
    return matches[0]


def derive_build_profiles(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive the compact build projection from frozen Builder facts."""
    rules = config["compatibility"]["rules"]
    product_rules = config["products"]
    builder_facts = config["builder_requirements"]
    native_facts = config["native_contracts"]
    version = config["ucm_version"]
    compiled = _compile_product_rules(product_rules)
    distribution_coordinates: set[tuple[str, str]] = set()
    profiles: list[dict[str, Any]] = []
    for index, builder in enumerate(builder_facts):
        if not isinstance(builder, Mapping):
            raise ValueError(f"builder_requirements[{index}] must be an object")
        accelerator = str(builder.get("accelerator"))
        runtime = str(builder.get("accelerator_runtime"))
        python_abi = str(builder.get("python_abi"))
        python_version = python_version_from_abi(python_abi)
        if builder.get("python_version") != python_version:
            raise ValueError(
                f"builder_requirements[{index}] Python version differs from ABI"
            )
        operating_systems = _operating_systems_for_accelerator(rules, accelerator)
        architectures = builder["architectures"]
        if not isinstance(architectures, Mapping) or not architectures:
            raise ValueError(
                f"builder_requirements[{index}].architectures must be an object"
            )
        variants = _normalize_variants(
            builder.get("variants"),
            location=f"builder_requirements[{index}].variants",
        )
        for variant in variants:
            native = _matching_native_contract(
                native_facts,
                accelerator=accelerator,
                runtime=runtime,
                variant=variant,
            )
            context = {
                "accelerator": accelerator,
                "accelerator_runtime": runtime,
                "variant": variant,
                "mooncake_version": builder.get("mooncake_version"),
            }
            distribution = _render_distribution_name(
                product_rules=product_rules,
                compiled=compiled,
                context=context,
            )
            coordinate = (distribution, python_abi)
            if coordinate in distribution_coordinates:
                raise ValueError(
                    "duplicate Distribution and Python ABI coordinate: "
                    f"{distribution}/{python_abi}"
                )
            distribution_coordinates.add(coordinate)
            runtime_token = compact_accelerator_runtime(runtime)
            profile_id = f"{accelerator}{runtime_token}-{variant}-{python_abi}"
            profiles.append(
                {
                    "id": profile_id,
                    "build": {
                        "docker_target": "wheel",
                        "platform_arg": builder["build_platform"],
                    },
                    "accelerator": accelerator,
                    "accelerator_runtime": runtime,
                    "npu_arch": ["na" if variant == "default" else variant],
                    "os": copy.deepcopy(operating_systems),
                    "cpu_arch": sorted(architectures),
                    "python_version": python_version,
                    "python_abi": python_abi,
                    "wheel_version": version,
                    "dist_name": distribution,
                    "wheel_platform": builder["wheel_platform"],
                    "builder_manylinux": builder["manylinux"],
                    "binary_profile_id": f"release-{profile_id}",
                    "external_required_dependencies": copy.deepcopy(
                        native.get("external_required_dependencies", [])
                    ),
                    "validation_targets": [
                        runtime if accelerator == "cuda" else variant
                    ],
                    "required_native": copy.deepcopy(native["required_native"]),
                    "forbidden_native": copy.deepcopy(native["forbidden_native"]),
                    "allowed_dt_needed": copy.deepcopy(
                        native["allowed_dt_needed"]
                    ),
                    "builders": copy.deepcopy(architectures),
                }
            )
    profiles.sort(key=lambda item: item["id"])
    return profiles
