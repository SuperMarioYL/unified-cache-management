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
    """Project capability contexts to unique, deterministically sorted names."""
    compiled = _compile_product_rules(product_rules)

    names = [
        _render_distribution_name(
            product_rules=product_rules,
            compiled=compiled,
            context=context,
        )
        for context in capability_contexts
    ]
    ordered = tuple(sorted(names))
    if len(ordered) != len(set(ordered)):
        raise ValueError("duplicate expanded Distribution coordinate")
    return ordered


def _matching_fact(
    facts: Sequence[Mapping[str, Any]],
    *,
    accelerator: str,
    runtime: str,
    variant: str,
    python_abi: str | None = None,
) -> Mapping[str, Any]:
    matches = []
    for fact in facts:
        if fact.get("accelerator") != accelerator:
            continue
        if fact.get("accelerator_runtime") not in {None, runtime}:
            continue
        variants = fact.get("variants")
        if variants is not None and variant not in variants:
            continue
        if python_abi is not None and fact.get("python_abi") != python_abi:
            continue
        matches.append(fact)
    if len(matches) != 1:
        raise ValueError(
            "capability must match exactly one builder/native fact: "
            f"{accelerator}/{runtime}/{variant}/{python_abi or '-'}"
        )
    return matches[0]


def derive_build_profiles(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive the current compact build projection from v3 capability facts."""
    rules = config["compatibility"]["rules"]
    product_rules = config["products"]
    builder_facts = config["builder_requirements"]
    native_facts = config["native_contracts"]
    version = config["ucm_version"]
    pending: list[
        tuple[
            Mapping[str, Any],
            Mapping[str, Any],
            Mapping[str, Any],
            dict[str, object],
            str,
            str,
            str,
            str,
            str,
        ]
    ] = []
    compiled = _compile_product_rules(product_rules)
    distributions: set[str] = set()
    for rule in rules:
        accelerator = rule["accelerator"]
        for runtime in rule["accelerator_runtimes"]:
            for variant in rule["variants"]:
                for python_abi in rule["python_abis"]:
                    builder = _matching_fact(
                        builder_facts,
                        accelerator=accelerator,
                        runtime=runtime,
                        variant=variant,
                        python_abi=python_abi,
                    )
                    native = _matching_fact(
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
                    runtime_token = compact_accelerator_runtime(runtime)
                    profile_id = (
                        f"{accelerator}{runtime_token}-{variant}-{python_abi}"
                    )
                    distribution = _render_distribution_name(
                        product_rules=product_rules,
                        compiled=compiled,
                        context=context,
                    )
                    if distribution in distributions:
                        raise ValueError("duplicate expanded Distribution coordinate")
                    distributions.add(distribution)
                    pending.append(
                        (
                            rule,
                            builder,
                            native,
                            context,
                            profile_id,
                            accelerator,
                            runtime,
                            python_abi,
                            distribution,
                        )
                    )
    profiles: list[dict[str, Any]] = []
    for values in pending:
        (
            rule,
            builder,
            native,
            context,
            profile_id,
            accelerator,
            runtime,
            python_abi,
            distribution,
        ) = values
        variant = str(context["variant"])
        architectures = builder["architectures"]
        cpu_arch = sorted(set(rule["cpu_architectures"]) & set(architectures))
        if not cpu_arch:
            raise ValueError(f"build profile {profile_id} has no architecture")
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
                "os": copy.deepcopy(rule["operating_systems"]),
                "cpu_arch": cpu_arch,
                "python_version": builder.get(
                    "python_version", python_version_from_abi(python_abi)
                ),
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
                "allowed_dt_needed": copy.deepcopy(native["allowed_dt_needed"]),
                "builders": copy.deepcopy(architectures),
            }
        )
    profiles.sort(key=lambda item: item["id"])
    return profiles
