"""Product-template compilation and deterministic Candidate selection."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import string
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from . import capabilities
from .capabilities import (
    compact_accelerator_runtime,
    compact_mooncake_version,
    compile_python_coordinate,
    normalize_variant,
    python_version_from_abi,
)

_TEMPLATE_FIELDS = {
    "cuda": frozenset({"runtime.compact"}),
    "cann": frozenset({"runtime.compact", "variant", "mooncake.compact"}),
}
_FAMILY_ACCELERATOR = {"cuda": "cuda", "cann": "ascend"}
_DISTRIBUTION = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", re.ASCII)
_OCI_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", re.ASCII)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_COMMIT = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_RUNTIME_SELECTOR_FIELDS = frozenset(
    {"version", "variant", "runtime.major_minor.compact"}
)
_RUNTIME_SELECTOR_TEMPLATES = frozenset(
    {
        "v{version}",
        "v{version}-cu{runtime.major_minor.compact}",
        "v{version}-{variant}",
    }
)
_RUNTIME_SELECTORS_BY_PRODUCT = {
    "vllm": (
        "v{version}",
        "v{version}-cu{runtime.major_minor.compact}",
    ),
    "vllm-ascend": (
        "v{version}",
        "v{version}-{variant}",
    ),
}


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


@dataclass(frozen=True, slots=True)
class CompiledRuntimeTagSelector:
    """One validated, ordered exact runtime-tag selector."""

    template: str
    fields: frozenset[str]

    def render(
        self,
        *,
        version: str,
        variant: str,
        runtime_major_minor_compact: str,
    ) -> str:
        try:
            parsed = Version(version)
        except InvalidVersion as error:
            raise ValueError("runtime selector version is invalid") from error
        if str(parsed) != version or version.startswith("v"):
            raise ValueError("runtime selector version is not canonical")
        normalized_variant = normalize_variant(variant)
        if (
            not isinstance(runtime_major_minor_compact, str)
            or re.fullmatch(r"[a-z0-9]+", runtime_major_minor_compact) is None
        ):
            raise ValueError("runtime selector compact runtime is invalid")
        values = {
            "version": version,
            "variant": normalized_variant,
            "runtime.major_minor.compact": runtime_major_minor_compact,
        }
        rendered = self.template
        for field in self.fields:
            rendered = rendered.replace("{" + field + "}", values[field])
        if _OCI_TAG.fullmatch(rendered) is None:
            raise ValueError("runtime selector expanded to an invalid OCI tag")
        return rendered


def compile_runtime_tag_selectors(
    value: object,
) -> tuple[CompiledRuntimeTagSelector, ...]:
    """Compile ordered runtime-tag policy without sorting or external reads."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("runtime_tag_selectors must be a non-empty sequence")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError("runtime_tag_selectors must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError("runtime_tag_selectors contains duplicates")
    compiled = []
    for template in value:
        if template not in _RUNTIME_SELECTOR_TEMPLATES:
            raise ValueError("runtime tag selector template is unsupported")
        fields: set[str] = set()
        try:
            parsed = tuple(string.Formatter().parse(template))
        except ValueError as error:
            raise ValueError("runtime tag selector is malformed") from error
        for _, field, format_spec, conversion in parsed:
            if field is None:
                continue
            if field not in _RUNTIME_SELECTOR_FIELDS:
                raise ValueError(f"unknown runtime selector variable {field}")
            if format_spec or conversion:
                raise ValueError("runtime selector formatting is not allowed")
            fields.add(field)
        selector = CompiledRuntimeTagSelector(template, frozenset(fields))
        selector.render(
            version="1.2.3",
            variant="future5",
            runtime_major_minor_compact="149",
        )
        compiled.append(selector)
    return tuple(compiled)


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


def _normalize_variant_tokens(value: object, *, location: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"{location} must be a non-empty sequence")
    variants = tuple(normalize_variant(item) for item in value)
    if len(variants) != len(set(variants)):
        raise ValueError(f"{location} contains duplicate normalized variants")
    return variants


def _normalize_builder_variants(
    value: object, *, location: str
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"{location} must be a non-empty sequence")
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_variant in enumerate(value):
        variant_location = f"{location}[{index}]"
        if not isinstance(raw_variant, Mapping) or set(raw_variant) not in (
            {"id", "tag_suffix", "npu_arch"},
            {"id", "tag_suffix", "npu_arch", "soc_versions"},
        ):
            raise ValueError(f"{variant_location} fields must be exact")
        variant_id = normalize_variant(raw_variant["id"])
        if variant_id in seen:
            raise ValueError(f"{location} contains duplicate normalized variants")
        seen.add(variant_id)
        tag_suffix = raw_variant["tag_suffix"]
        if (
            not isinstance(tag_suffix, str)
            or re.fullmatch(r"(?:|-[a-z0-9][a-z0-9.-]*)", tag_suffix) is None
        ):
            raise ValueError(f"{variant_location}.tag_suffix is invalid")
        variant = {
            "id": variant_id,
            "tag_suffix": tag_suffix,
            "npu_arch": normalize_variant(raw_variant["npu_arch"]),
        }
        if "soc_versions" in raw_variant:
            soc_versions = raw_variant["soc_versions"]
            if (
                not isinstance(soc_versions, Sequence)
                or isinstance(soc_versions, (str, bytes))
                or not soc_versions
                or not all(isinstance(item, str) and item for item in soc_versions)
                or len(soc_versions) != len(set(soc_versions))
            ):
                raise ValueError(f"{variant_location}.soc_versions is invalid")
            variant["soc_versions"] = sorted(soc_versions)
        variants.append(variant)
    return tuple(variants)


def _compatibility_policy_for_product(
    rules: Sequence[Mapping[str, Any]], product_id: str
) -> tuple[str, list[str]]:
    matches = [
        rule for rule in rules if product_id in rule.get("upstream_products", [])
    ]
    if not matches:
        raise ValueError(f"upstream product {product_id!r} has no compatibility policy")
    accelerators = {str(rule.get("accelerator")) for rule in matches}
    if len(accelerators) != 1:
        raise ValueError(
            f"upstream product {product_id!r} has conflicting accelerator policy"
        )
    operating_systems = sorted(
        {
            operating_system
            for rule in matches
            for operating_system in rule["operating_systems"]
        }
    )
    return accelerators.pop(), operating_systems


def _excluded_variants(config: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        normalize_variant(value) for value in config["discovery"]["exclude_variants"]
    )


def _runtime_patch_variants(
    product: Mapping[str, Any], variant: Mapping[str, Any]
) -> dict[str, str]:
    runtime_product = product.get("runtime_product")
    if runtime_product == "vllm":
        return {"vllm": "default"}
    if runtime_product == "vllm-ascend":
        return {
            "vllm": "default",
            "vllm-ascend": str(variant["npu_arch"]),
        }
    raise ValueError(
        f"upstream product {product.get('id')!r} has unsupported runtime product"
    )


def derive_upstream_products(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project registry-facing Variant and architecture facts at load time."""
    raw_products = config["upstream_products"]
    builder_facts = config["builder_requirements"]
    rules = config["compatibility"]["rules"]
    excluded = _excluded_variants(config)
    products_by_id: dict[str, Mapping[str, Any]] = {}
    for index, product in enumerate(raw_products):
        if not isinstance(product, Mapping):
            raise ValueError(f"upstream_products[{index}] must be an object")
        product_id = str(product.get("id"))
        if product_id in products_by_id:
            raise ValueError(f"duplicate upstream product id: {product_id!r}")
        products_by_id[product_id] = product

    facts_by_product: dict[str, list[tuple[int, Mapping[str, Any]]]] = {
        product_id: [] for product_id in products_by_id
    }
    for index, builder in enumerate(builder_facts):
        if not isinstance(builder, Mapping):
            raise ValueError(f"builder_requirements[{index}] must be an object")
        product_id = builder.get("upstream_product_id")
        if product_id not in products_by_id:
            raise ValueError(
                f"builder_requirements[{index}] references unknown upstream product "
                f"{product_id!r}"
            )
        facts_by_product[str(product_id)].append((index, builder))

    projected: list[dict[str, Any]] = []
    for product_id, product in products_by_id.items():
        accelerator, _ = _compatibility_policy_for_product(rules, product_id)
        variant_facts: dict[str, dict[str, Any]] = {}
        architectures: set[str] = set()
        for index, builder in facts_by_product[product_id]:
            if builder.get("accelerator") != accelerator:
                raise ValueError(
                    f"builder_requirements[{index}] accelerator differs from "
                    f"upstream product {product_id!r} policy"
                )
            variants = _normalize_builder_variants(
                builder.get("variants"),
                location=f"builder_requirements[{index}].variants",
            )
            included = [
                variant for variant in variants if variant["id"] not in excluded
            ]
            if not included:
                continue
            builder_architectures = builder.get("architectures")
            if (
                not isinstance(builder_architectures, Mapping)
                or not builder_architectures
            ):
                raise ValueError(
                    f"builder_requirements[{index}].architectures must be an object"
                )
            architectures.update(str(value) for value in builder_architectures)
            for variant in included:
                previous = variant_facts.get(variant["id"])
                if previous is not None and previous != variant:
                    raise ValueError(
                        f"conflicting Builder metadata for upstream variant "
                        f"{product_id}/{variant['id']}"
                    )
                variant_facts[variant["id"]] = variant
        if not variant_facts or not architectures:
            raise ValueError(
                f"upstream product {product_id!r} has no non-excluded Builder facts"
            )
        variants = []
        for variant_id in sorted(variant_facts):
            variant = copy.deepcopy(variant_facts[variant_id])
            variant["runtime_patch_variants"] = _runtime_patch_variants(
                product, variant
            )
            variants.append(variant)
        projected.append(
            {
                **copy.deepcopy(dict(product)),
                "variants": variants,
                "required_cpu_architectures": sorted(architectures),
            }
        )
    return projected


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
        if variants is not None and variant not in _normalize_variant_tokens(
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
    projected_products = {
        str(product["id"]): product for product in config["upstream_products"]
    }
    excluded = _excluded_variants(config)
    distribution_coordinates: set[tuple[str, str]] = set()
    profiles: list[dict[str, Any]] = []
    for index, builder in enumerate(builder_facts):
        if not isinstance(builder, Mapping):
            raise ValueError(f"builder_requirements[{index}] must be an object")
        accelerator = str(builder.get("accelerator"))
        product_id = str(builder.get("upstream_product_id"))
        product = projected_products.get(product_id)
        if product is None:
            raise ValueError(
                f"builder_requirements[{index}] references unknown upstream product "
                f"{product_id!r}"
            )
        policy_accelerator, operating_systems = _compatibility_policy_for_product(
            rules, product_id
        )
        if accelerator != policy_accelerator:
            raise ValueError(
                f"builder_requirements[{index}] accelerator differs from "
                f"upstream product {product_id!r} policy"
            )
        runtime = str(builder.get("accelerator_runtime"))
        python_abi = str(builder.get("python_abi"))
        python_version = python_version_from_abi(python_abi)
        if builder.get("python_version") != python_version:
            raise ValueError(
                f"builder_requirements[{index}] Python version differs from ABI"
            )
        architectures = builder["architectures"]
        if not isinstance(architectures, Mapping) or not architectures:
            raise ValueError(
                f"builder_requirements[{index}].architectures must be an object"
            )
        variants = _normalize_builder_variants(
            builder.get("variants"),
            location=f"builder_requirements[{index}].variants",
        )
        for variant in variants:
            variant_id = variant["id"]
            if variant_id in excluded:
                continue
            projected_variant = next(
                (value for value in product["variants"] if value["id"] == variant_id),
                None,
            )
            if projected_variant is None or any(
                projected_variant.get(field) != variant.get(field)
                for field in ("id", "tag_suffix", "npu_arch", "soc_versions")
                if field in projected_variant or field in variant
            ):
                raise ValueError(
                    f"builder_requirements[{index}] Variant metadata differs from "
                    f"upstream product projection"
                )
            npu_arch = variant["npu_arch"]
            native = _matching_native_contract(
                native_facts,
                accelerator=accelerator,
                runtime=runtime,
                variant=npu_arch,
            )
            context = {
                "accelerator": accelerator,
                "accelerator_runtime": runtime,
                "variant": variant_id,
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
            profile_id = f"{accelerator}{runtime_token}-{variant_id}-{python_abi}"
            profiles.append(
                {
                    "id": profile_id,
                    "build": {
                        "docker_target": "wheel",
                        "platform_arg": builder["build_platform"],
                    },
                    "accelerator": accelerator,
                    "accelerator_runtime": runtime,
                    "upstream_product_id": product_id,
                    "variant": variant_id,
                    "npu_arch": [npu_arch],
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
                        runtime if accelerator == "cuda" else npu_arch
                    ],
                    "required_native": copy.deepcopy(native["required_native"]),
                    "forbidden_native": copy.deepcopy(native["forbidden_native"]),
                    "allowed_dt_needed": copy.deepcopy(native["allowed_dt_needed"]),
                    "builders": copy.deepcopy(architectures),
                }
            )
    profiles.sort(key=lambda item: item["id"])
    return profiles


_SELECTION_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "route",
        "source_sha",
        "ucm_version",
        "release_tag",
        "config_sha256",
        "catalog_sha256",
        "current_builder_authority_sha256",
        "baseline_manifest_sha256",
        "builder_capabilities",
        "builder_revisions",
        "runtime_candidates",
        "bindings",
        "baseline_selections",
        "discovered_selections",
        "exclusions",
        "blockers",
        "dependency_requests",
        "selection_sha256",
    }
)
_DISCOVERED_SELECTION_FIELDS = frozenset(
    {
        "product_id",
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
    }
)
_EXCLUSION_FIELDS = frozenset(
    {
        "reason_code",
        "product_id",
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
        "evidence",
    }
)
_DEPENDENCY_REQUEST_FIELDS = frozenset({"request_id", "coordinate", "requirements"})
_REQUEST_COORDINATE_FIELDS = frozenset(
    {"python_tag", "python_abi", "cpu_architecture", "manylinux"}
)
_REQUIREMENT_FIELDS = frozenset({"requirement_id", "scope", "name", "version"})


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical SHA256 digest")
    return value


def _require_commit(value: object, label: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical Git commit")
    return value


def _validate_dependency_identity(
    scope: object, name: object, version: object
) -> dict[str, str]:
    if scope not in {"build", "runtime"}:
        raise ValueError("dependency scope must be build or runtime")
    if (
        not isinstance(name, str)
        or not name
        or canonicalize_name(name) != name
    ):
        raise ValueError("dependency name must be canonical for a public index")
    if not isinstance(version, str):
        raise ValueError("dependency version must be a string")
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise ValueError("dependency pin is not PEP 440") from error
    if str(parsed) != version or parsed.epoch or parsed.local:
        raise ValueError("dependency pin is not an exact public PEP 440 version")
    return {"scope": scope, "name": name, "version": version}


def _dependency_requirements(config: Mapping[str, Any]) -> list[dict[str, str]]:
    dependencies = config.get("dependencies")
    if not isinstance(dependencies, Mapping) or set(dependencies) != {
        "build",
        "runtime",
    }:
        raise ValueError("Candidate dependency policy fields must be exact")
    requirements = []
    for scope in ("build", "runtime"):
        values = dependencies[scope]
        if not isinstance(values, Mapping):
            raise ValueError(f"dependencies.{scope} must be an object")
        for name, version in values.items():
            identity = _validate_dependency_identity(scope, name, version)
            requirements.append(
                {"requirement_id": _canonical_digest(identity), **identity}
            )
    return sorted(requirements, key=lambda item: item["requirement_id"])


def _dependency_request(
    capability: Mapping[str, Any], requirements: list[dict[str, str]]
) -> dict[str, Any]:
    compiled = compile_python_coordinate(
        {
            "python_version": capability["python_version"],
            "python_abi": capability["python_abi"],
            "cpu_architecture": capability["cpu_architecture"],
            "manylinux": capability["manylinux"],
        }
    )
    coordinate = {
        "python_tag": compiled["python_tag"],
        "python_abi": capability["python_abi"],
        "cpu_architecture": capability["cpu_architecture"],
        "manylinux": capability["manylinux"],
    }
    projection = {"coordinate": coordinate, "requirements": requirements}
    return {"request_id": _canonical_digest(projection), **projection}


def _selection_exclusion(
    *,
    reason_code: str,
    product_id: str,
    evidence: dict[str, Any],
    builder_capability_id: str | None = None,
    builder_revision_id: str | None = None,
    runtime_id: str | None = None,
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "product_id": product_id,
        "builder_capability_id": builder_capability_id,
        "builder_revision_id": builder_revision_id,
        "runtime_id": runtime_id,
        "evidence": copy.deepcopy(evidence),
    }


def _exclusion_key(value: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(value["reason_code"]),
        str(value["product_id"]),
        str(value["builder_capability_id"] or ""),
        str(value["builder_revision_id"] or ""),
        str(value["runtime_id"] or ""),
    )


def _selector_runtime_token(accelerator_runtime: str) -> str:
    name, separator, version = accelerator_runtime.partition("-")
    if separator != "-":
        raise ValueError("binding accelerator runtime is malformed")
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise ValueError("binding accelerator runtime version is invalid") from error
    if len(parsed.release) < 2:
        raise ValueError("binding accelerator runtime lacks major/minor")
    return compact_accelerator_runtime(
        f"{name}-{parsed.release[0]}.{parsed.release[1]}"
    )


def prepare_candidate_selection(
    config: object,
    catalog: object,
    current_builder_authority: object,
    *,
    route: str,
    source_sha: str,
    baseline_manifest: object | None = None,
) -> dict[str, Any]:
    """Prepare the deterministic Task 4A1 selection without external reads."""
    from . import builders

    if route not in {"pr", "daily", "release"}:
        raise ValueError("Candidate selection route is invalid")
    requested_source_sha = _require_commit(source_sha, "Candidate source_sha")
    if baseline_manifest is not None:
        raise ValueError("Task 4A1 does not yet accept a baseline Manifest")
    if not isinstance(config, Mapping):
        raise ValueError("Candidate config must be an object")
    validated_catalog = capabilities.validate_capability_catalog(copy.deepcopy(catalog))
    authority = builders.validate_current_builder_authority(
        current_builder_authority
    )
    if not (
        requested_source_sha
        == validated_catalog["source_sha"]
        == authority["source_sha"]
    ):
        raise ValueError("Candidate/Catalog/Builder authority source_sha differ")

    products_by_id: dict[str, Mapping[str, Any]] = {}
    selectors_by_product: dict[str, tuple[CompiledRuntimeTagSelector, ...]] = {}
    raw_products = config.get("upstream_products")
    if not isinstance(raw_products, Sequence) or isinstance(raw_products, (str, bytes)):
        raise ValueError("upstream_products must be an array")
    for product in raw_products:
        if not isinstance(product, Mapping):
            raise ValueError("upstream product must be an object")
        product_id = product.get("id")
        if (
            not isinstance(product_id, str)
            or not product_id
            or product_id in products_by_id
        ):
            raise ValueError("upstream product ID is missing or duplicate")
        products_by_id[product_id] = product
        runtime_product = product.get("runtime_product")
        expected_selectors = _RUNTIME_SELECTORS_BY_PRODUCT.get(runtime_product)
        if expected_selectors is None or tuple(
            product.get("runtime_tag_selectors", [])
        ) != expected_selectors:
            raise ValueError("runtime selector policy differs from runtime product")
        selectors_by_product[product_id] = compile_runtime_tag_selectors(
            product.get("runtime_tag_selectors")
        )

    capabilities_by_id = {
        item["builder_capability_id"]: item
        for item in validated_catalog["builder_capabilities"]
    }
    revisions_by_id = {
        item["builder_revision_id"]: item
        for item in validated_catalog["builder_revisions"]
    }
    runtimes_by_id = {
        item["runtime_id"]: item for item in validated_catalog["runtime_candidates"]
    }
    recipes_by_path = {item["recipe_path"]: item for item in authority["recipes"]}
    grouped_bindings: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for binding in validated_catalog["bindings"]:
        capability = capabilities_by_id[binding["builder_capability_id"]]
        runtime = runtimes_by_id[binding["runtime_id"]]
        product_id = runtime["product_id"]
        product = products_by_id.get(product_id)
        if product is None:
            raise ValueError("Catalog runtime references an unknown product")
        if runtime["runtime_repository"] != product.get("repository"):
            raise ValueError("Catalog runtime repository differs from product config")
        key = (
            product_id,
            capability["accelerator_runtime"],
            capability["variant"],
            capability["cpu_architecture"],
        )
        grouped_bindings.setdefault(key, []).append(binding)

    selected_capabilities: dict[str, dict[str, Any]] = {}
    selected_revisions: dict[str, dict[str, Any]] = {}
    selected_runtimes: dict[str, dict[str, Any]] = {}
    selected_bindings: dict[tuple[str, str], dict[str, Any]] = {}
    discovered: list[dict[str, str]] = []
    exclusions: list[dict[str, Any]] = []
    for group in sorted(grouped_bindings):
        product_id, accelerator_runtime, variant, architecture = group
        bindings = grouped_bindings[group]
        runtime_ids = sorted({item["runtime_id"] for item in bindings})
        versions: dict[Version, list[dict[str, Any]]] = {}
        for runtime_id in runtime_ids:
            runtime = runtimes_by_id[runtime_id]
            try:
                version = Version(runtime["runtime_version"])
            except InvalidVersion as error:
                raise ValueError("Catalog runtime_version is invalid") from error
            if str(version) != runtime["runtime_version"]:
                raise ValueError("Catalog runtime_version is not canonical")
            versions.setdefault(version, []).append(runtime)
        selected_runtime: dict[str, Any] | None = None
        for version in sorted(versions, reverse=True):
            candidates = versions[version]
            for selector in selectors_by_product[product_id]:
                tag = selector.render(
                    version=str(version),
                    variant=variant,
                    runtime_major_minor_compact=_selector_runtime_token(
                        accelerator_runtime
                    ),
                )
                matches = [item for item in candidates if item["runtime_tag"] == tag]
                if len(matches) > 1:
                    raise ValueError("runtime selector matched multiple candidates")
                if matches:
                    selected_runtime = matches[0]
                    break
            if selected_runtime is not None:
                break
            exclusions.append(
                _selection_exclusion(
                    reason_code="runtime-flavor-unsupported",
                    product_id=product_id,
                    evidence={
                        "accelerator_runtime": accelerator_runtime,
                        "variant": variant,
                        "cpu_architecture": architecture,
                        "runtime_version": str(version),
                        "selectors": [
                            item.template for item in selectors_by_product[product_id]
                        ],
                    },
                )
            )
        if selected_runtime is None:
            continue

        selected_runtime_id = selected_runtime["runtime_id"]
        runtime_bindings = [
            item for item in bindings if item["runtime_id"] == selected_runtime_id
        ]
        capability_ids = sorted(
            {item["builder_capability_id"] for item in runtime_bindings}
        )
        for capability_id in capability_ids:
            capability_bindings = [
                item
                for item in runtime_bindings
                if item["builder_capability_id"] == capability_id
            ]
            current = []
            for binding in capability_bindings:
                revision = revisions_by_id[binding["builder_revision_id"]]
                recipe = recipes_by_path.get(revision["recipe_path"])
                if recipe is None:
                    continue
                if (
                    revision["recipe_source_commit"] == recipe["recipe_source_commit"]
                    and revision["recipe_sha256"] == recipe["recipe_sha256"]
                    and revision["toolchain_sha256"] == authority["toolchain_sha256"]
                ):
                    current.append((binding, revision))
            if len(current) > 1:
                raise ValueError("multiple current Builder revisions match selection")
            if not current:
                exclusions.append(
                    _selection_exclusion(
                        reason_code="current-builder-revision-unavailable",
                        product_id=product_id,
                        builder_capability_id=capability_id,
                        runtime_id=selected_runtime_id,
                        evidence={
                            "current_builder_authority_sha256": authority[
                                "authority_sha256"
                            ],
                            "recipe_paths": sorted(recipes_by_path),
                        },
                    )
                )
                continue
            binding, revision = current[0]
            selected_capabilities[capability_id] = capabilities_by_id[capability_id]
            selected_revisions[revision["builder_revision_id"]] = revision
            selected_runtimes[selected_runtime_id] = selected_runtime
            selected_bindings[
                (revision["builder_revision_id"], selected_runtime_id)
            ] = binding
            discovered.append(
                {
                    "product_id": product_id,
                    "builder_capability_id": capability_id,
                    "builder_revision_id": revision["builder_revision_id"],
                    "runtime_id": selected_runtime_id,
                }
            )

    requirements = _dependency_requirements(config)
    requests_by_id = {}
    for capability in selected_capabilities.values():
        request = _dependency_request(capability, requirements)
        requests_by_id[request["request_id"]] = request
    discovered.sort(
        key=lambda item: (
            item["product_id"],
            item["builder_capability_id"],
            item["builder_revision_id"],
            item["runtime_id"],
        )
    )
    exclusions.sort(key=_exclusion_key)
    selection = {
        "kind": "ucm-candidate-selection",
        "schema_version": 3,
        "route": route,
        "source_sha": requested_source_sha,
        "ucm_version": config.get("ucm_version"),
        "release_tag": config.get("source", {}).get("release_tag"),
        "config_sha256": _canonical_digest(config),
        "catalog_sha256": validated_catalog["catalog_sha256"],
        "current_builder_authority_sha256": authority["authority_sha256"],
        "baseline_manifest_sha256": None,
        "builder_capabilities": sorted(
            (copy.deepcopy(item) for item in selected_capabilities.values()),
            key=lambda item: item["builder_capability_id"],
        ),
        "builder_revisions": sorted(
            (copy.deepcopy(item) for item in selected_revisions.values()),
            key=lambda item: item["builder_revision_id"],
        ),
        "runtime_candidates": sorted(
            (copy.deepcopy(item) for item in selected_runtimes.values()),
            key=lambda item: item["runtime_id"],
        ),
        "bindings": sorted(
            (copy.deepcopy(item) for item in selected_bindings.values()),
            key=lambda item: (item["builder_revision_id"], item["runtime_id"]),
        ),
        "baseline_selections": [],
        "discovered_selections": discovered,
        "exclusions": exclusions,
        "blockers": [],
        "dependency_requests": [requests_by_id[key] for key in sorted(requests_by_id)],
        "selection_sha256": "",
    }
    if not isinstance(selection["ucm_version"], str) or not selection["ucm_version"]:
        raise ValueError("Candidate config ucm_version is missing")
    if not isinstance(selection["release_tag"], str) or not selection["release_tag"]:
        raise ValueError("Candidate config release_tag is missing")
    selection["selection_sha256"] = _canonical_digest(
        {key: item for key, item in selection.items() if key != "selection_sha256"}
    )
    return validate_candidate_selection(selection)


def validate_candidate_selection(value: object) -> dict[str, Any]:
    """Validate the frozen A1 selection without rerunning selectors."""
    if not isinstance(value, dict) or set(value) != _SELECTION_FIELDS:
        raise ValueError("Candidate selection fields must be exact")
    if value["kind"] != "ucm-candidate-selection" or value["schema_version"] != 3:
        raise ValueError("Candidate selection identity is invalid")
    if value["route"] not in {"pr", "daily", "release"}:
        raise ValueError("Candidate selection route is invalid")
    _require_commit(value["source_sha"], "Candidate selection source_sha")
    for field in ("ucm_version", "release_tag"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"Candidate selection {field} must be non-empty")
    for field in (
        "config_sha256",
        "catalog_sha256",
        "current_builder_authority_sha256",
        "selection_sha256",
    ):
        _require_digest(value[field], f"Candidate selection {field}")
    if value["baseline_manifest_sha256"] is not None:
        raise ValueError("Task 4A1 baseline selection must be null")
    if value["baseline_selections"] != [] or value["blockers"] != []:
        raise ValueError("Task 4A1 baseline selections/blockers must be empty")

    arrays = capabilities.validate_selected_capability_evidence(
        {
            "builder_capabilities": value["builder_capabilities"],
            "builder_revisions": value["builder_revisions"],
            "runtime_candidates": value["runtime_candidates"],
            "bindings": value["bindings"],
        }
    )

    capability_ids = {
        item["builder_capability_id"] for item in arrays["builder_capabilities"]
    }
    revision_ids = {item["builder_revision_id"] for item in arrays["builder_revisions"]}
    runtime_ids = {item["runtime_id"] for item in arrays["runtime_candidates"]}
    runtime_products = {
        item["runtime_id"]: item["product_id"]
        for item in arrays["runtime_candidates"]
    }
    binding_coordinates = {
        (
            item["builder_capability_id"],
            item["builder_revision_id"],
            item["runtime_id"],
        )
        for item in arrays["bindings"]
    }
    if len(binding_coordinates) != len(arrays["bindings"]):
        raise ValueError("Candidate selection bindings contain duplicates")
    selections = value["discovered_selections"]
    if not isinstance(selections, list) or not all(
        isinstance(item, dict) and set(item) == _DISCOVERED_SELECTION_FIELDS
        for item in selections
    ):
        raise ValueError("discovered selections are not closed")
    expected_selection_order = sorted(
        selections,
        key=lambda item: (
            item["product_id"],
            item["builder_capability_id"],
            item["builder_revision_id"],
            item["runtime_id"],
        ),
    )
    if selections != expected_selection_order or len(selections) != len(
        {tuple(item.values()) for item in selections}
    ):
        raise ValueError("discovered selections are duplicate or noncanonical")
    for item in selections:
        if (
            item["builder_capability_id"] not in capability_ids
            or item["builder_revision_id"] not in revision_ids
            or item["runtime_id"] not in runtime_ids
            or (
                item["builder_capability_id"],
                item["builder_revision_id"],
                item["runtime_id"],
            )
            not in binding_coordinates
        ):
            raise ValueError("discovered selection references are incomplete")
        if item["product_id"] != runtime_products[item["runtime_id"]]:
            raise ValueError("discovered selection product differs from runtime")
    selection_coordinates = {
        (
            item["builder_capability_id"],
            item["builder_revision_id"],
            item["runtime_id"],
        )
        for item in selections
    }
    if selection_coordinates != binding_coordinates:
        raise ValueError("discovered selections do not close selected bindings")
    if capability_ids != {item[0] for item in selection_coordinates}:
        raise ValueError("selected capabilities are not closed")
    if revision_ids != {item[1] for item in selection_coordinates}:
        raise ValueError("selected revisions are not closed")
    if runtime_ids != {item[2] for item in selection_coordinates}:
        raise ValueError("selected runtimes are not closed")

    exclusions = value["exclusions"]
    if not isinstance(exclusions, list) or not all(
        isinstance(item, dict)
        and set(item) == _EXCLUSION_FIELDS
        and isinstance(item["evidence"], dict)
        for item in exclusions
    ):
        raise ValueError("Candidate selection exclusions are not closed")
    if exclusions != sorted(exclusions, key=_exclusion_key):
        raise ValueError("Candidate selection exclusions are noncanonical")
    if len(exclusions) != len({_canonical_bytes(item) for item in exclusions}):
        raise ValueError("Candidate selection exclusions contain duplicates")

    requests = value["dependency_requests"]
    if not isinstance(requests, list) or requests != sorted(
        requests, key=lambda item: item.get("request_id", "")
    ):
        raise ValueError("dependency requests are noncanonical")
    request_ids = []
    request_coordinates = []
    for request in requests:
        if not isinstance(request, dict) or set(request) != _DEPENDENCY_REQUEST_FIELDS:
            raise ValueError("dependency request fields must be exact")
        coordinate = request["coordinate"]
        requirements = request["requirements"]
        if (
            not isinstance(coordinate, dict)
            or set(coordinate) != _REQUEST_COORDINATE_FIELDS
        ):
            raise ValueError("dependency request coordinate fields must be exact")
        compiled = compile_python_coordinate(
            {
                "python_version": python_version_from_abi(coordinate["python_abi"]),
                "python_abi": coordinate["python_abi"],
                "cpu_architecture": coordinate["cpu_architecture"],
                "manylinux": coordinate["manylinux"],
            }
        )
        if compiled["python_tag"] != coordinate["python_tag"]:
            raise ValueError("dependency request Python tag differs")
        if not isinstance(requirements, list) or requirements != sorted(
            requirements, key=lambda item: item.get("requirement_id", "")
        ):
            raise ValueError("dependency requirements are noncanonical")
        for requirement in requirements:
            if (
                not isinstance(requirement, dict)
                or set(requirement) != _REQUIREMENT_FIELDS
            ):
                raise ValueError("dependency requirement fields must be exact")
            identity = _validate_dependency_identity(
                requirement["scope"],
                requirement["name"],
                requirement["version"],
            )
            if requirement["requirement_id"] != _canonical_digest(identity):
                raise ValueError("dependency requirement ID is not canonical")
        projection = {"coordinate": coordinate, "requirements": requirements}
        if request["request_id"] != _canonical_digest(projection):
            raise ValueError("dependency request ID is not canonical")
        request_ids.append(request["request_id"])
        request_coordinates.append(coordinate)
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("dependency requests contain duplicates")
    expected_coordinates = set()
    for capability in arrays["builder_capabilities"]:
        compiled = capabilities.compile_python_coordinate(
            {
                "python_version": capability["python_version"],
                "python_abi": capability["python_abi"],
                "cpu_architecture": capability["cpu_architecture"],
                "manylinux": capability["manylinux"],
            }
        )
        expected_coordinates.add(
            (
                compiled["python_tag"],
                capability["python_abi"],
                capability["cpu_architecture"],
                capability["manylinux"],
            )
        )
    actual_coordinates = {
        (
            item["python_tag"],
            item["python_abi"],
            item["cpu_architecture"],
            item["manylinux"],
        )
        for item in request_coordinates
    }
    if actual_coordinates != expected_coordinates:
        raise ValueError("dependency requests do not close selected capabilities")
    digest = value["selection_sha256"]
    projection = copy.deepcopy(value)
    projection.pop("selection_sha256")
    if digest != _canonical_digest(projection):
        raise ValueError("Candidate selection digest differs from contents")
    return copy.deepcopy(value)
