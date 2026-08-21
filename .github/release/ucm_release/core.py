# fmt: off
from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASE = RELEASE_ROOT / "release.yaml"
DEFAULT_SCHEMA_DIR = RELEASE_ROOT / "schemas"
OCI_REPOSITORY_PATTERN = re.compile('^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:(?:[._]|-+)[a-z0-9]+)*)+$')  # fmt: skip  # noqa: E501
OCI_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
RECIPE_BASE_SOURCE_PATTERN = re.compile('^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$')  # fmt: skip  # noqa: E501
RECIPE_BASE_NAME_VERSION_PATTERN = re.compile('^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')  # fmt: skip  # noqa: E501
RECIPE_BUILD_ARG_PATTERN = re.compile('^[A-Z][A-Z0-9_]*=[A-Za-z0-9][A-Za-z0-9._~:/?@%+,=-]*$')  # fmt: skip  # noqa: E501


@dataclass(frozen=True, slots=True)
class CpuToolchainAuthority:

    cpu_arch: str
    oci_platform: str
    wheel_arch: str
    elf_machine: int
    elf_machine_name: str
    host_machine_aliases: tuple[str, ...]


CPU_TOOLCHAIN_AUTHORITIES: Mapping[str, CpuToolchainAuthority] = MappingProxyType({'amd64': CpuToolchainAuthority(cpu_arch='amd64', oci_platform='linux/amd64', wheel_arch='x86_64', elf_machine=62, elf_machine_name='EM_X86_64', host_machine_aliases=('x86_64', 'amd64')), 'arm64': CpuToolchainAuthority(cpu_arch='arm64', oci_platform='linux/arm64', wheel_arch='aarch64', elf_machine=183, elf_machine_name='EM_AARCH64', host_machine_aliases=('aarch64', 'arm64'))})  # fmt: skip  # noqa: E501


def cpu_toolchain_authority(
    cpu_arch: object, *, location: str = "CPU/tool architecture"
) -> CpuToolchainAuthority:
    authority = CPU_TOOLCHAIN_AUTHORITIES.get(cpu_arch) if isinstance(cpu_arch, str) else None  # fmt: skip  # noqa: E501
    if authority is None: raise ValueError(f'unsupported CPU arch {cpu_arch!r} at {location}')  # noqa: E701,E501
    return authority


def host_cpu_toolchain_authority(host_machine: object) -> CpuToolchainAuthority:
    normalized = host_machine.lower() if isinstance(host_machine, str) else None
    matches = [authority for authority in CPU_TOOLCHAIN_AUTHORITIES.values() if normalized in authority.host_machine_aliases]  # fmt: skip  # noqa: E501
    if len(matches) != 1: raise ValueError(f'unsupported CPU/tool host architecture: {host_machine!r}')  # noqa: E701,E501
    return matches[0]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping: raise ValueError(f'duplicate YAML key: {key}')  # noqa: E701
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)  # fmt: skip  # noqa: E501


def load_yaml_value(text: str, *, context: str) -> Any:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f'{context}: malformed YAML: {error}') from error


def load_yaml(path: Path) -> dict[str, Any]:
    value = load_yaml_value(path.read_text(encoding="utf-8"), context=str(path))
    if not isinstance(value, dict): raise ValueError(f'{path} must contain a mapping')  # noqa: E701,E501
    return value


def load_json_value(path: Path) -> Any:

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result: raise ValueError(f'duplicate JSON key {key!r} in {path}')  # noqa: E701,E501
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)


def load_json(path: Path) -> dict[str, Any]:
    value = load_json_value(path)
    if not isinstance(value, dict): raise ValueError(f'{path} must contain a JSON object')  # noqa: E701,E501
    return value


def load_json_array(path: Path) -> list[Any]:
    value = load_json_value(path)
    if not isinstance(value, list): raise ValueError(f'{path} must contain a JSON array')  # noqa: E701,E501
    return value


def _resolve_ref(root: dict[str, Any], reference: str) -> Any:
    if not reference.startswith('#/'): raise ValueError(f'unsupported schema reference: {reference}')  # noqa: E701,E501
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value: raise ValueError(f'unresolved schema reference: {reference}')  # noqa: E701,E501
        value = value[part]
    return value


def validate_schema(
    instance: Any,
    schema: Any,
    *,
    root: dict[str, Any] | None = None,
    path: str = "$",
) -> None:
    if schema is False: raise ValueError(f'{path}: value is forbidden by schema')  # noqa: E701,E501
    if schema is True:
        return
    if not isinstance(schema, dict): raise ValueError(f'{path}: invalid schema node')  # noqa: E701,E501
    root_schema = root or schema
    if '$ref' in schema: validate_schema(instance, _resolve_ref(root_schema, schema['$ref']), root=root_schema, path=path)  # noqa: E701,E501
    if "oneOf" in schema:
        matches = 0
        errors: list[str] = []
        for option in schema["oneOf"]:
            try:
                validate_schema(instance, option, root=root_schema, path=path)
                matches += 1
            except ValueError as error:
                errors.append(str(error))
        if matches != 1:
            detail = errors[0] if errors else f"matched {matches} branches"
            raise ValueError(f"{path}: oneOf requires exactly one match: {detail}")
    expected_type = schema.get("type")
    if expected_type is not None:
        type_checks = {'object': lambda value: isinstance(value, dict), 'array': lambda value: isinstance(value, list), 'string': lambda value: isinstance(value, str), 'integer': lambda value: isinstance(value, int) and (not isinstance(value, bool)), 'boolean': lambda value: isinstance(value, bool), 'number': lambda value: isinstance(value, (int, float)) and (not isinstance(value, bool)), 'null': lambda value: value is None}  # fmt: skip  # noqa: E501
        if expected_type not in type_checks or not type_checks[expected_type](instance):
            raise ValueError(f"{path}: expected {expected_type}")
    if 'const' in schema and instance != schema['const']: raise ValueError(f"{path}: expected constant {schema['const']!r}")  # noqa: E701,E501
    if 'enum' in schema and instance not in schema['enum']: raise ValueError(f"{path}: expected one of {schema['enum']!r}")  # noqa: E701,E501
    if isinstance(instance, str):
        if len(instance) < schema.get('minLength', 0): raise ValueError(f'{path}: string is shorter than minLength')  # noqa: E701,E501
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise ValueError(f"{path}: value does not match pattern {schema['pattern']!r}")  # fmt: skip  # noqa: E501
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValueError(f"{path}: value is below minimum {schema['minimum']}")
    if isinstance(instance, dict):
        if len(instance) < schema.get('minProperties', 0): raise ValueError(f'{path}: object has fewer than minProperties')  # noqa: E701,E501
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            raise ValueError(f"{path}: object has more than maxProperties")
        missing = [key for key in schema.get("required", []) if key not in instance]
        if missing: raise ValueError(f'{path}: missing required properties {missing}')  # noqa: E701,E501
        properties = schema.get("properties", {})
        property_names = schema.get("propertyNames")
        if property_names is not None:
            for key in instance:
                validate_schema(key, property_names, root=root_schema, path=f'{path}.<property>')  # fmt: skip  # noqa: E501
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras: raise ValueError(f'Additional properties are not allowed at {path}: {extras}')  # noqa: E701,E501
        for key, value in instance.items():
            if key in properties:
                validate_schema(value, properties[key], root=root_schema, path=f'{path}.{key}')  # fmt: skip  # noqa: E501
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(value, schema['additionalProperties'], root=root_schema, path=f'{path}.{key}')  # fmt: skip  # noqa: E501
    if isinstance(instance, list):
        if len(instance) < schema.get('minItems', 0): raise ValueError(f'{path}: array is shorter than minItems')  # noqa: E701,E501
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ValueError(f"{path}: array is longer than maxItems")
        if schema.get("uniqueItems"):
            encoded = [canonical_bytes(item) for item in instance]
            if len(encoded) != len(set(encoded)): raise ValueError(f'{path}: array items must be unique')  # noqa: E701,E501
        prefix_items = schema.get("prefixItems", [])
        for index, item_schema in enumerate(prefix_items):
            if index < len(instance): validate_schema(instance[index], item_schema, root=root_schema, path=f'{path}[{index}]')  # noqa: E701,E501
        item_schema = schema.get("items")
        if item_schema is not None:
            start = len(prefix_items) if prefix_items else 0
            for index in range(start, len(instance)):
                validate_schema(instance[index], item_schema, root=root_schema, path=f'{path}[{index}]')  # fmt: skip  # noqa: E501


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()  # fmt: skip  # noqa: E501


def sha256_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_version(path: Path | None = None) -> str:
    version_path = path or (REPO_ROOT / "version.ini")
    for line in version_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key == "VLLM_UC_VERSION" and value:
            return value
    raise ValueError(f"VLLM_UC_VERSION is missing from {version_path}")


def _pep440_public(version: str) -> str:
    base = version.split('+', 1)[0]
    if '.dev' in base:
        base = re.split(r'\.dev\d+', base)[0]
    return base


def _oci_tag_version(version: str) -> str:
    return version.replace('+', '.')  # fmt: skip  # noqa: E501


def derive_chart_version(version: str) -> str:
    public = _pep440_public(version)
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)rc([0-9]+)", public)
    if match is None:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", public): return public  # fmt: skip  # noqa: E701,E501
        raise ValueError(f'unsupported UCM release version for Chart SemVer: {version}')  # noqa: E701,E501
    return f"{match.group(1)}-rc.{match.group(2)}"


def _pep440_version(value: object, location: str) -> Version:
    try:
        return Version(str(value))
    except InvalidVersion as error:
        raise ValueError(f'{location} must be a valid PEP 440 version: {value!r}') from error  # fmt: skip  # noqa: E501


def _pep440_specifier(value: object, location: str) -> SpecifierSet:
    try:
        return SpecifierSet(str(value))
    except InvalidSpecifier as error:
        raise ValueError(f'{location} must be a valid PEP 440 specifier: {value!r}') from error  # fmt: skip  # noqa: E501


def _specifier_interval(
    value: object,
) -> tuple[Version | None, bool, Version | None, bool] | None:
    specifiers = _pep440_specifier(value, "compatibility version selector")
    lower: Version | None = None
    lower_inclusive = False
    upper: Version | None = None
    upper_inclusive = False

    def add_lower(candidate: Version, inclusive: bool) -> None:
        nonlocal lower, lower_inclusive
        if lower is None or candidate > lower:
            lower, lower_inclusive = candidate, inclusive
        if candidate == lower: lower_inclusive = lower_inclusive and inclusive  # noqa: E701,E501

    def add_upper(candidate: Version, inclusive: bool) -> None:
        nonlocal upper, upper_inclusive
        if upper is None or candidate < upper:
            upper, upper_inclusive = candidate, inclusive
        if candidate == upper: upper_inclusive = upper_inclusive and inclusive  # noqa: E701,E501

    for specifier in specifiers:
        operator = specifier.operator
        raw_version = specifier.version
        if operator == "!=":
            # Exclusions can only shrink the interval. Ignoring them is conservative:
            # an uncertain pair is rejected instead of accepting future ambiguity.
            continue
        if operator == "==" and raw_version.endswith(".*"):
            prefix = _pep440_version(raw_version.removesuffix('.*'), 'compatibility wildcard prefix')  # fmt: skip  # noqa: E501
            if (
                prefix.epoch != 0
                or prefix.pre
                or prefix.post
                or prefix.dev
                or prefix.local
            ):
                return None
            release = prefix.release
            upper_release = (*release[:-1], release[-1] + 1)
            add_lower(Version(".".join(map(str, release)) + ".dev0"), True)
            add_upper(Version(".".join(map(str, upper_release))), False)
            continue
        if operator == "~=":
            compatible = _pep440_version(raw_version, 'compatibility compatible-release selector')  # fmt: skip  # noqa: E501
            if (
                compatible.epoch != 0
                or compatible.local is not None
                or len(compatible.release) < 2
            ):
                return None
            upper_prefix = compatible.release[:-1]
            upper_release = (*upper_prefix[:-1], upper_prefix[-1] + 1)
            add_lower(compatible, True)
            add_upper(Version(".".join(map(str, upper_release))), False)
            continue
        if operator not in {"==", ">=", ">", "<=", "<"}:
            return None
        version = _pep440_version(raw_version, "compatibility version boundary")
        if operator == "==":
            add_lower(version, True)
            add_upper(version, True)
        elif operator == ">=":
            add_lower(version, True)
        elif operator == ">":
            add_lower(version, False)
        elif operator == "<=":
            add_upper(version, True)
        else:
            add_upper(version, False)
    return lower, lower_inclusive, upper, upper_inclusive


def _version_specifiers_may_overlap(left: object, right: object) -> bool:
    left_specifiers = _pep440_specifier(left, "left compatibility version selector")
    right_specifiers = _pep440_specifier(right, "right compatibility version selector")
    for specifiers in (left_specifiers, right_specifiers):
        for specifier in specifiers:
            if specifier.operator != "==" or specifier.version.endswith(".*"):
                continue
            witness = _pep440_version(specifier.version, 'compatibility exact-version witness')  # fmt: skip  # noqa: E501
            if left_specifiers.contains(
                witness, prereleases=True
            ) and right_specifiers.contains(witness, prereleases=True):
                return True
    combined = ",".join(part for part in (str(left), str(right)) if part)
    interval = _specifier_interval(combined)
    if interval is None:
        return True
    lower, lower_inclusive, upper, upper_inclusive = interval
    if lower is None or upper is None or lower < upper:
        return True
    if lower > upper:
        return False
    return lower_inclusive and upper_inclusive


def _compatibility_rules_semantically_overlap(
    left: dict[str, Any],
    right: dict[str, Any],
    products_by_id: dict[str, dict[str, Any]],
) -> bool:
    common_products = set(left["upstream_products"]) & set(right["upstream_products"])
    if not common_products or left["accelerator"] != right["accelerator"]:
        return False
    common_variants = set(left["variants"]) & set(right["variants"])
    if not any(
        common_variants
        & {variant["id"] for variant in products_by_id[product_id]["variants"]}
        for product_id in common_products
    ):
        return False
    for field in (
        "accelerator_runtimes",
        "npu_architectures",
        "operating_systems",
        "cpu_architectures",
        "python_abis",
        "upstream_channels",
    ):
        if not (set(left[field]) & set(right[field])):
            return False
    return _version_specifiers_may_overlap(left['version_specifier'], right['version_specifier'])  # fmt: skip  # noqa: E501


def _require_unique_ids(items: list[dict[str, Any]], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        identifier = item["id"]
        if identifier in seen: raise ValueError(f'duplicate {label} id {identifier!r}')  # noqa: E701,E501
        seen.add(identifier)


_BUILDER_CHECK_FIELDS = {'python': {'kind', 'version', 'abi'}, 'python-soabi': {'kind', 'prefix'}, 'command': {'kind', 'name'}, 'command-version': {'kind', 'name', 'arguments', 'contains'}, 'file': {'kind', 'path'}, 'directory': {'kind', 'path'}, 'library-cache': {'kind', 'path'}, 'shared-library-dependencies': {'kind', 'path'}}  # fmt: skip  # noqa: E501


def _validate_builder_checks(
    checks: object, *, profile: dict[str, Any], location: str
) -> None:
    if not isinstance(checks, list) or not checks: raise ValueError(f'{location} builder checks must be a non-empty array')  # noqa: E701,E501
    seen: set[bytes] = set()
    requirements: dict[tuple[str, str], tuple[str, bytes]] = {}
    python_checks = 0
    soabi_checks = 0
    for index, check in enumerate(checks):
        check_location = f"{location}.checks[{index}]"
        if not isinstance(check, dict): raise ValueError(f'{check_location} must be an object')  # noqa: E701,E501
        kind = check.get("kind")
        fields = _BUILDER_CHECK_FIELDS.get(str(kind))
        if fields is None: raise ValueError(f'unsupported builder check kind {kind!r}')  # noqa: E701,E501
        if set(check) != fields: raise ValueError(f'{check_location} fields are not exact')  # noqa: E701,E501
        encoded = canonical_bytes(check)
        if encoded in seen: raise ValueError(f'duplicate builder check at {check_location}')  # noqa: E701,E501
        seen.add(encoded)
        if kind == "python":
            python_checks += 1
            version = check["version"]
            abi = check["abi"]
            if (
                not isinstance(version, str)
                or re.fullmatch(r"[0-9]+\.[0-9]+", version) is None
                or not isinstance(abi, str)
                or abi != "cp" + version.replace(".", "")
                or version != profile["python_version"]
                or abi != profile["python_abi"]
            ):
                raise ValueError(f'{check_location} differs from profile Python authority')  # fmt: skip  # noqa: E501
            target = ("python", "runtime")
        elif kind == "python-soabi":
            soabi_checks += 1
            if (
                not isinstance(check["prefix"], str)
                or re.fullmatch(r"[a-z][a-z0-9_-]*", check["prefix"]) is None
            ):
                raise ValueError(f"{check_location}.prefix is invalid")
            target = ("python-soabi", "runtime")
        elif kind in {"command", "command-version"}:
            name = check["name"]
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name) is None
            ):
                raise ValueError(f"{check_location}.name is invalid")
            if kind == "command-version":
                arguments = check["arguments"]
                contains = check["contains"]
                if (
                    not isinstance(arguments, list)
                    or not arguments
                    or not all(
                        isinstance(argument, str)
                        and argument
                        and "\x00" not in argument
                        for argument in arguments
                    )
                    or not isinstance(contains, str)
                    or not contains
                    or "\x00" in contains
                ):
                    raise ValueError(f"{check_location} arguments are invalid")
            target = ("command", name)
        else:
            path = check["path"]
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or any(character.isspace() for character in path)
                or "\x00" in path
            ):
                raise ValueError(f"{check_location}.path is invalid")
            normalized_path = posixpath.normpath("/" + path.lstrip("/"))
            target = ('filesystem-node' if kind in {'file', 'directory'} else str(kind), normalized_path)  # fmt: skip  # noqa: E501
        previous = requirements.get(target)
        if previous is not None:
            previous_kind, previous_encoded = previous
            if target[0] == "filesystem-node" and previous_kind == kind:
                raise ValueError(f"duplicate builder check at {check_location}")
            if previous_encoded != encoded: raise ValueError(f'conflicting builder checks for {target[1]!r}')  # noqa: E701,E501
        requirements[target] = (str(kind), encoded)
    if python_checks != 1 or soabi_checks != 1:
        raise ValueError(f'{location} requires exactly one Python and one Python SOABI check')  # fmt: skip  # noqa: E501


def _validate_builder_root(root: object, *, location: str) -> None:
    if root is None:
        return
    if not isinstance(root, dict):
        raise ValueError(f"{location}.root must be an object")
    unresolved_fields = {"repository", "tag"}
    resolved_fields = unresolved_fields | {
        "index_digest",
        "manifest_digest",
        "config_digest",
    }
    if frozenset(root) not in {
        frozenset(unresolved_fields),
        frozenset(resolved_fields),
    }:
        raise ValueError(f"{location}.root must be unresolved or fully resolved")
    if OCI_REPOSITORY_PATTERN.fullmatch(str(root.get("repository"))) is None:
        raise ValueError(f"{location}.root.repository must use canonical OCI syntax")
    if OCI_TAG_PATTERN.fullmatch(str(root.get("tag"))) is None:
        raise ValueError(f"{location}.root.tag must use canonical OCI tag syntax")
    for digest_name in sorted(resolved_fields - unresolved_fields):
        if digest_name in root and re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(root[digest_name])
        ) is None:
            raise ValueError(
                f"{location}.root.{digest_name} must be an exact sha256 digest"
            )


def _validate_resolved_builder_roots(catalog: dict[str, Any]) -> None:
    required = {
        "repository",
        "tag",
        "index_digest",
        "manifest_digest",
        "config_digest",
    }
    for profile_index, profile in enumerate(catalog.get("wheel_profiles", [])):
        for architecture, builder in profile.get("builders", {}).items():
            location = f"wheel_profiles[{profile_index}].builders.{architecture}"
            root = builder.get("root") if isinstance(builder, dict) else None
            if not isinstance(root, dict) or set(root) != required:
                raise ValueError(f"{location} builder root must be resolved")


def runtime_patch_manifest(
    catalog: dict[str, Any], *, repository_root: Path = REPO_ROOT
) -> dict[str, Any]:
    rules = catalog.get("runtime_patch_rules")
    if not isinstance(rules, list) or not rules: raise ValueError('runtime_patch_rules must be a non-empty array')  # noqa: E701,E501
    _require_unique_ids(rules, "runtime patch rule")
    products = catalog.get("upstream_products", [])
    seen_orders: set[int] = set()
    normalized: list[dict[str, Any]] = []
    for index, rule in enumerate(rules):
        location = f"runtime_patch_rules[{index}]"
        fields = {'id', 'order', 'product', 'version_specifier', 'channels', 'variants', 'strategy', 'imports'}  # fmt: skip  # noqa: E501
        if not isinstance(rule, dict) or set(rule) != fields: raise ValueError(f'{location} fields are not exact')  # noqa: E701,E501
        order = rule["order"]
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            raise ValueError(f"{location}.order must be a non-negative integer")
        if order in seen_orders: raise ValueError(f'duplicate runtime patch rule order {order}')  # noqa: E701,E501
        seen_orders.add(order)
        product_members = [product for product in products if product.get('runtime_product') == rule['product']]  # fmt: skip  # noqa: E501
        if not product_members: raise ValueError(f"{location} references unknown runtime product {rule['product']!r}")  # noqa: E701,E501
        _pep440_specifier(rule["version_specifier"], f"{location}.version_specifier")
        if (
            not isinstance(rule["channels"], list)
            or not rule["channels"]
            or len(rule["channels"]) != len(set(rule["channels"]))
            or not set(rule["channels"]).issubset({"stable", "rc"})
        ):
            raise ValueError(f"{location}.channels are invalid")
        declared_variants = {item['id'] for product in product_members for item in product['variants']}  # fmt: skip  # noqa: E501
        if (
            not isinstance(rule["variants"], list)
            or not rule["variants"]
            or len(rule["variants"]) != len(set(rule["variants"]))
            or not set(rule["variants"]).issubset(declared_variants)
        ):
            raise ValueError(f"{location}.variants are invalid for the product")
        imports = rule["imports"]
        if rule["strategy"] not in {"imports", "none"} or not isinstance(imports, list):
            raise ValueError(f"{location}.strategy is invalid")
        if (rule['strategy'] == 'none') != (imports == []): raise ValueError(f'{location} none strategy must have no imports')  # noqa: E701,E501
        if rule["strategy"] == "imports" and not imports:
            raise ValueError(f"{location} imports strategy must declare modules")
        normalized_imports: list[dict[str, Any]] = []
        seen_imports: set[bytes] = set()
        for import_index, declaration in enumerate(imports):
            import_location = f"{location}.imports[{import_index}]"
            if not isinstance(declaration, dict) or set(declaration) not in (
                {"module"},
                {"module", "when"},
            ):
                raise ValueError(f"{import_location} fields are not exact")
            module = declaration["module"]
            if (
                not isinstance(module, str)
                or re.fullmatch(r"ucm(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+", module) is None
            ):
                raise ValueError(f"{import_location}.module is invalid")
            module_path = repository_root / (module.replace(".", "/") + ".py")
            package_path = repository_root / module.replace(".", "/") / "__init__.py"
            if not module_path.is_file() and not package_path.is_file():
                raise ValueError(f'runtime patch adapter module is not packaged: {module}')  # fmt: skip  # noqa: E501
            normalized_import: dict[str, Any] = {"module": module}
            if "when" in declaration:
                condition = declaration["when"]
                if (
                    not isinstance(condition, dict)
                    or set(condition) != {"sparse"}
                    or not isinstance(condition["sparse"], bool)
                ):
                    raise ValueError(f"{import_location}.when is malformed")
                normalized_import["when"] = {"sparse": condition["sparse"]}
            encoded_import = canonical_bytes(normalized_import)
            if encoded_import in seen_imports: raise ValueError(f'duplicate runtime patch import at {import_location}')  # noqa: E701,E501
            seen_imports.add(encoded_import)
            normalized_imports.append(normalized_import)
        normalized.append({'id': rule['id'], 'order': order, 'product': rule['product'], 'version_specifier': str(_pep440_specifier(rule['version_specifier'], f'{location}.version_specifier')), 'channels': sorted(rule['channels']), 'variants': sorted(rule['variants']), 'strategy': rule['strategy'], 'imports': normalized_imports})  # fmt: skip  # noqa: E501
    return {'schema_version': 1, 'kind': 'ucm-runtime-patch-rules', 'rules': sorted(normalized, key=lambda item: (item['order'], item['id']))}  # fmt: skip  # noqa: E501


def runtime_patch_manifest_sha256(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(manifest) + b"\n").hexdigest()


def find_runtime_patch_rule(
    manifest: dict[str, Any], snapshot: dict[str, Any], variant: str, *, relaxed: bool = False
) -> dict[str, Any] | None:
    runtime_product = snapshot.get("runtime_product", snapshot["product_id"])
    if relaxed:
        # Tolerate non-pep440 versions (e.g. mutable 'latest'); when the
        # version can't be parsed, fall back to product+variant+channel which
        # is ambiguous across version-gated rules -> rejected as overlapping.
        try:
            version: Version | None = Version(str(snapshot["version"]))
        except InvalidVersion:
            version = None
    else:
        version = _pep440_version(snapshot["version"], "resolved upstream version")
    if version is not None:
        matches = [rule for rule in manifest['rules'] if rule['product'] == runtime_product and snapshot['channel'] in rule['channels'] and (variant in rule['variants']) and _pep440_specifier(rule['version_specifier'], f"runtime patch rule {rule['id']!r}").contains(version, prereleases=True)]  # fmt: skip  # noqa: E501
    else:
        matches = [rule for rule in manifest['rules'] if rule['product'] == runtime_product and snapshot['channel'] in rule['channels'] and (variant in rule['variants'])]  # fmt: skip  # noqa: E501
    if not matches:
        return None
    if len(matches) > 1:
        identifiers = ", ".join(rule["id"] for rule in matches)
        raise ValueError(
            f"resolved upstream matches overlapping runtime patch strategies: {identifiers}"
        )
    return matches[0]


def _matching_runtime_patch_rule(
    manifest: dict[str, Any], snapshot: dict[str, Any], variant: str, *, relaxed: bool = False
) -> dict[str, Any]:
    match = find_runtime_patch_rule(manifest, snapshot, variant, relaxed=relaxed)
    if match is None:
        runtime_product = snapshot.get("runtime_product", snapshot["product_id"])
        raise ValueError(f"resolved upstream has no runtime patch strategy: product={runtime_product}, version={snapshot['version']}, channel={snapshot['channel']}, variant={variant}")  # fmt: skip  # noqa: E501
    return match


def _exact_runtime_requirement(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value: raise ValueError('python runtime requirement is invalid')  # noqa: E701,E501
    try:
        requirement = Requirement(value)
    except InvalidRequirement as error:
        raise ValueError("python runtime requirement is invalid") from error
    specifiers = list(requirement.specifier)
    if (
        requirement.url is not None
        or requirement.marker is not None
        or requirement.extras
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
        or "*" in specifiers[0].version
    ):
        raise ValueError("python runtime requirement must be one exact version")
    try:
        version = str(Version(specifiers[0].version))
    except InvalidVersion as error:
        raise ValueError("python runtime requirement version is invalid") from error
    name = canonicalize_name(requirement.name)
    return name, version, f"{name}=={version}"


def python_runtime_requirements(catalog: dict[str, Any]) -> list[str]:
    declarations = catalog.get("python_runtime_dependencies")
    if not declarations:
        return []
    if (
        not isinstance(declarations, list)
        or not 1 <= len(declarations) <= 64
        or any(not isinstance(item, dict) for item in declarations)
    ):
        raise ValueError("python runtime dependency declarations are invalid")
    build_lock = catalog.get("python_build_lock")
    packages = build_lock.get("packages") if isinstance(build_lock, dict) else None
    resolved: list[str] = []
    identities: set[str] = set()
    for declaration in declarations:
        import_name = declaration.get("import_name")
        if (
            not isinstance(import_name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", import_name) is None
        ):
            raise ValueError("python runtime dependency import name is invalid")
        if set(declaration) == {"requirement", "import_name", "wheel_artifacts"}:
            identity, _, resolved_requirement = _exact_runtime_requirement(declaration['requirement'])  # fmt: skip  # noqa: E501
            if not isinstance(declaration['wheel_artifacts'], dict): raise ValueError('python runtime wheel artifacts are invalid')  # noqa: E701,E501
        elif set(declaration) == {"python_build_lock", "import_name"}:
            package_name = declaration["python_build_lock"]
            package = packages.get(package_name) if isinstance(packages, dict) and isinstance(package_name, str) else None  # fmt: skip  # noqa: E501
            if (
                not isinstance(package_name, str)
                or not package_name
                or not isinstance(package, dict)
                or set(package) != {"version", "filename", "sha256"}
                or not isinstance(package["version"], str)
                or not package["version"]
                or not isinstance(package["filename"], str)
                or not package["filename"].endswith(".whl")
                or re.fullmatch(r"sha256:[0-9a-f]{64}", str(package["sha256"])) is None
            ):
                raise ValueError(f'python build lock {package_name!r} runtime authority is invalid')  # fmt: skip  # noqa: E501
            identity = canonicalize_name(package_name)
            resolved_requirement = f"{identity}=={package['version']}"
        else:
            raise ValueError("python runtime dependency declaration is invalid")
        if identity in identities: raise ValueError(f'duplicate python runtime dependency {identity!r}')  # noqa: E701,E501
        identities.add(identity)
        resolved.append(resolved_requirement)
    return sorted(resolved)


def runtime_dependency_records(
    catalog: dict[str, Any], python_abi: str, architecture: str
) -> list[dict[str, str]]:
    requirements = set(python_runtime_requirements(catalog))
    build_lock = catalog["python_build_lock"]["packages"]
    records: list[dict[str, str]] = []
    for declaration in catalog["python_runtime_dependencies"]:
        if "requirement" in declaration:
            name, version, requirement = _exact_runtime_requirement(declaration['requirement'])  # fmt: skip  # noqa: E501
            wheel = python_abi_artifact(declaration['wheel_artifacts'], python_abi, architecture, label=f'python_runtime_dependencies.{name}.wheel_artifacts')  # fmt: skip  # noqa: E501
        else:
            name = canonicalize_name(declaration["python_build_lock"])
            package = build_lock[declaration["python_build_lock"]]
            version = package["version"]
            requirement = f"{name}=={version}"
            wheel = {"filename": package["filename"], "sha256": package["sha256"]}
        records.append({'name': name, 'version': version, 'requirement': requirement, 'import_name': declaration['import_name'], 'filename': wheel['filename'], 'sha256': wheel['sha256']})  # fmt: skip  # noqa: E501
    records.sort(key=lambda item: item["name"])
    if (
        {item["requirement"] for item in records} != requirements
        or len({item["name"] for item in records}) != len(records)
        or len({item["filename"] for item in records}) != len(records)
    ):
        raise ValueError("python runtime dependency wheel authority is ambiguous")
    return records


def build_tool_dependency_records(
    catalog: dict[str, Any], python_abi: str, architecture: str
) -> list[dict[str, str]]:
    build_lock = catalog.get("python_build_lock")
    packages = build_lock.get("packages") if isinstance(build_lock, dict) else None
    if not isinstance(packages, dict) or not 1 <= len(packages) <= 64:
        raise ValueError("python build tool package authority is invalid")
    records: list[dict[str, str]] = []
    for package_name, package in packages.items():
        if not isinstance(package, dict): raise ValueError('python build tool package authority is invalid')  # noqa: E701,E501
        name = canonicalize_name(package_name)
        records.append({'name': name, 'version': str(package['version']), 'requirement': f"{name}=={package['version']}", 'filename': package['filename'], 'sha256': package['sha256']})  # fmt: skip  # noqa: E501
    for name, lock_record, artifact in (
        (
            "pyyaml",
            build_lock.get("pyyaml"),
            python_abi_artifact(
                build_lock.get("pyyaml", {}).get("artifacts"),
                python_abi,
                architecture,
                label="python_build_lock.pyyaml.artifacts",
            ),
        ),
        (
            "cmake",
            build_lock.get("cmake"),
            (
                build_lock.get("cmake", {}).get("artifacts", {}).get(architecture)
                if isinstance(build_lock.get("cmake", {}).get("artifacts"), dict)
                else None
            ),
        ),
    ):
        if (
            not isinstance(lock_record, dict)
            or not isinstance(lock_record.get("version"), str)
            or not lock_record["version"]
            or not isinstance(artifact, dict)
            or set(artifact) != {"filename", "sha256"}
            or not isinstance(artifact["filename"], str)
            or not artifact["filename"].endswith(".whl")
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact["sha256"])) is None
        ):
            raise ValueError(f"python build tool {name!r} has no exact task artifact")
        records.append({'name': name, 'version': lock_record['version'], 'requirement': f"{name}=={lock_record['version']}", 'filename': artifact['filename'], 'sha256': artifact['sha256']})  # fmt: skip  # noqa: E501
    records.sort(key=lambda item: item["name"])
    if (
        len(records) > 64
        or len({record["name"] for record in records}) != len(records)
        or len({record["filename"] for record in records}) != len(records)
    ):
        raise ValueError("python build tool wheel authority is ambiguous")
    return records


def python_abi_artifact(
    artifacts: object, python_abi: str, architecture: str, *, label: str
) -> dict[str, str]:
    abi_artifacts = artifacts.get(python_abi) if isinstance(artifacts, dict) else None
    artifact = abi_artifacts.get(architecture) if isinstance(abi_artifacts, dict) else None  # fmt: skip  # noqa: E501
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"filename", "sha256"}
        or not isinstance(artifact["filename"], str)
        or not artifact["filename"]
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact["sha256"])) is None
    ):
        raise ValueError(f"{label} has no exact {python_abi}/{architecture} artifact")
    return copy.deepcopy(artifact)


def _validate_catalog_cpu_toolchains(catalog: dict[str, Any]) -> None:
    declarations: list[tuple[str, object]] = []
    runner_map = catalog.get("runner_map")
    if isinstance(runner_map, dict): declarations.extend(((f'runner_map.{key}', key) for key in runner_map))  # noqa: E701,E501
    for index, profile in enumerate(catalog.get("wheel_profiles", [])):
        if not isinstance(profile, dict):
            continue
        architectures = profile.get("cpu_arch")
        if isinstance(architectures, list):
            declarations.extend(((f'wheel_profiles[{index}].cpu_arch', architecture) for architecture in architectures))  # fmt: skip  # noqa: E501
        builders = profile.get("builders")
        if isinstance(builders, dict):
            declarations.extend(((f'wheel_profiles[{index}].builders.{architecture}', architecture) for architecture in builders))  # fmt: skip  # noqa: E501
    for index, product in enumerate(catalog.get("upstream_products", [])):
        if not isinstance(product, dict):
            continue
        architectures = product.get("required_cpu_architectures")
        if isinstance(architectures, list):
            declarations.extend(((f'upstream_products[{index}].required_cpu_architectures', architecture) for architecture in architectures))  # fmt: skip  # noqa: E501
    compatibility = catalog.get("compatibility")
    rules = compatibility.get("rules", []) if isinstance(compatibility, dict) else []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        architectures = rule.get("cpu_architectures")
        if isinstance(architectures, list):
            declarations.extend(((f'compatibility.rules[{index}].cpu_architectures', architecture) for architecture in architectures))  # fmt: skip  # noqa: E501
    smoke = catalog.get("pr_smoke")
    selectors = smoke.get("image_selectors", []) if isinstance(smoke, dict) else []
    for index, selector in enumerate(selectors):
        if isinstance(selector, dict) and "cpu_arch" in selector:
            declarations.append((f'pr_smoke.image_selectors[{index}].cpu_arch', selector['cpu_arch']))  # fmt: skip  # noqa: E501
    for index, recipe in enumerate(catalog.get("docker_recipes", [])):
        if isinstance(recipe, dict) and "cpu_arch" in recipe:
            declarations.append((f'docker_recipes[{index}].cpu_arch', recipe['cpu_arch']))  # fmt: skip  # noqa: E501
    for location, architecture in declarations:
        cpu_toolchain_authority(architecture, location=location)


def validate_catalog(
    catalog: dict[str, Any], *, repository_root: Path = REPO_ROOT
) -> None:
    _validate_catalog_cpu_toolchains(catalog)
    _pep440_version(catalog.get("ucm_version"), "ucm_version")
    python_runtime_requirements(catalog)
    profiles = catalog.get("wheel_profiles", [])
    products = catalog.get("upstream_products", [])
    compatibility = catalog.get("compatibility", {})
    rules = compatibility.get("rules", [])
    recipes = catalog.get("docker_recipes", [])
    _require_unique_ids(profiles, "wheel profile")
    _require_unique_ids(products, "upstream product")
    _require_unique_ids(rules, "compatibility rule")
    _require_unique_ids(recipes, "Docker recipe")
    for index, profile in enumerate(profiles):
        _pep440_version(profile.get('wheel_version'), f'wheel_profiles[{index}].wheel_version')  # fmt: skip  # noqa: E501
        for architecture in profile.get("cpu_arch", []):
            runtime_dependency_records(catalog, profile.get("python_abi"), architecture)
            build_tool_dependency_records(catalog, profile.get('python_abi'), architecture)  # fmt: skip  # noqa: E501
        builders = profile.get("builders")
        if not isinstance(builders, dict): raise ValueError(f'wheel_profiles[{index}].builders must be an object')  # noqa: E701,E501
        for architecture, builder in builders.items():
            if not isinstance(builder, dict): raise ValueError(f'wheel_profiles[{index}].builders.{architecture} must be an object')  # noqa: E701,E501
            _validate_builder_checks(builder.get('checks'), profile=profile, location=f'wheel_profiles[{index}].builders.{architecture}')  # fmt: skip  # noqa: E501
            _validate_builder_root(builder.get('root'), location=f'wheel_profiles[{index}].builders.{architecture}')  # fmt: skip  # noqa: E501
    for index, product in enumerate(products):
        _pep440_specifier(product.get('version_specifier'), f'upstream_products[{index}].version_specifier')  # fmt: skip  # noqa: E501
        _require_unique_ids(product["variants"], "upstream variant")
        expected_runtime_products = {"vllm", product["runtime_product"]}
        for variant in product["variants"]:
            runtime_variants = variant.get("runtime_patch_variants")
            if (
                not isinstance(runtime_variants, dict)
                or set(runtime_variants) != expected_runtime_products
                or any(
                    not isinstance(value, str) or not value
                    for value in runtime_variants.values()
                )
            ):
                raise ValueError('upstream runtime patch variant map must name exactly the applicable installed products')  # fmt: skip  # noqa: E501
            for runtime_product, runtime_variant in runtime_variants.items():
                if not any(
                    rule["product"] == runtime_product
                    and runtime_variant in rule["variants"]
                    for rule in catalog.get("runtime_patch_rules", [])
                ):
                    raise ValueError(f'upstream runtime patch variant is not declared by a matched product rule: {runtime_product}={runtime_variant}')  # fmt: skip  # noqa: E501
    products_by_id = {product["id"]: product for product in products}
    smoke = catalog.get("pr_smoke", {})
    selectors = smoke.get("image_selectors", []) if isinstance(smoke, dict) else []
    if not selectors: raise ValueError('pr_smoke.image_selectors must not be empty')  # noqa: E701,E501
    selector_identities: set[tuple[str, str, str]] = set()
    for selector in selectors:
        identity = (selector['product_id'], selector['variant'], selector['cpu_arch'])  # fmt: skip
        if identity in selector_identities: raise ValueError(f'duplicate PR smoke selector {identity!r}')  # noqa: E701,E501
        selector_identities.add(identity)
        product = products_by_id.get(selector["product_id"])
        if product is None: raise ValueError(f"PR smoke selector references unknown product {selector['product_id']!r}")  # noqa: E701,E501
        if selector["variant"] not in {
            variant["id"] for variant in product["variants"]
        }:
            raise ValueError(f"PR smoke selector references unknown variant {selector['variant']!r}")  # fmt: skip  # noqa: E501
        if selector["cpu_arch"] not in product["required_cpu_architectures"]:
            raise ValueError('PR smoke selector architecture is not required by product')  # fmt: skip  # noqa: E501
    for index, rule in enumerate(rules):
        _pep440_specifier(rule.get('version_specifier'), f'compatibility.rules[{index}].version_specifier')  # fmt: skip  # noqa: E501
        referenced_products: list[dict[str, Any]] = []
        for product_id in rule["upstream_products"]:
            product = products_by_id.get(product_id)
            if product is None: raise ValueError(f'unknown upstream product {product_id!r}')  # noqa: E701,E501
            referenced_products.append(product)
        declared_variants = {variant['id'] for product in referenced_products for variant in product['variants']}  # fmt: skip  # noqa: E501
        for variant_id in rule["variants"]:
            if variant_id not in declared_variants: raise ValueError(f'unknown variant {variant_id!r}')  # noqa: E701,E501
    for left_index, left in enumerate(rules):
        for right in rules[left_index + 1 :]:
            if _compatibility_rules_semantically_overlap(left, right, products_by_id):
                raise ValueError(f"compatibility rules have semantic selector overlap: {left['id']!r} and {right['id']!r}")  # fmt: skip  # noqa: E501
    runtime_patch_manifest(catalog, repository_root=repository_root)


def _dockerfile_instructions(dockerfile_text: str) -> list[tuple[str, str]]:
    raw_lines = dockerfile_text.splitlines()
    escape_character = "\\"
    line_index = 0
    parser_directive_region = True
    parser_directives: dict[str, str] = {}
    parser_directive_pattern = re.compile('^[ \\t]*#[ \\t]*(?P<key>[A-Za-z][A-Za-z0-9_-]*)[ \\t]*=[ \\t]*(?P<value>[^ \\t\\r\\n]+)[ \\t]*$')  # fmt: skip  # noqa: E501
    known_parser_directives = {"syntax", "escape", "check"}
    heredoc_pattern = re.compile('(?<!<)<<(?P<strip_tabs>-?)(?P<delimiter>\'[A-Za-z_][A-Za-z0-9_.-]*\'|"[A-Za-z_][A-Za-z0-9_.-]*"|[A-Za-z_][A-Za-z0-9_.-]*)(?![A-Za-z0-9_.-])')  # fmt: skip  # noqa: E501
    heredoc_operator_pattern = re.compile(r"(?<!<)<<(?!<)")
    instructions: list[tuple[str, str]] = []

    while line_index < len(raw_lines):
        raw_line = raw_lines[line_index]
        directive_match = parser_directive_pattern.fullmatch(raw_line)
        directive_key = directive_match.group('key').lower() if directive_match is not None else None  # fmt: skip  # noqa: E501
        if directive_key in known_parser_directives:
            if not parser_directive_region:
                raise ValueError('Dockerfile parser directive must precede comments, blank lines, and instructions')  # fmt: skip  # noqa: E501
            if directive_key in parser_directives:
                raise ValueError(f'duplicate or conflicting Dockerfile parser directive: {directive_key}')  # fmt: skip  # noqa: E501
            directive_value = directive_match.group("value")  # type: ignore[union-attr]
            parser_directives[directive_key] = directive_value
            if directive_key == "escape":
                if directive_value not in {'\\', '`'}: raise ValueError('Dockerfile escape parser directive must be `\\` or ```')  # noqa: E701,E501
                escape_character = directive_value
            line_index += 1
            continue
        if parser_directive_region: parser_directive_region = False  # noqa: E701
        if raw_line.lstrip().startswith("#"):
            line_index += 1
            continue

        logical_line = raw_line
        while logical_line.endswith(escape_character):
            logical_line = logical_line[:-1]
            while True:
                if line_index + 1 >= len(raw_lines): raise ValueError('unterminated Dockerfile continuation')  # noqa: E701,E501
                line_index += 1
                continued_line = raw_lines[line_index]
                if not continued_line.strip(): raise ValueError('blank line inside Dockerfile continuation')  # noqa: E701,E501
                if continued_line.lstrip().startswith("#"):
                    continue
                logical_line += continued_line
                break
        line_index += 1

        stripped = logical_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split(None, 1)
        keyword = fields[0].upper()
        body = fields[1].strip() if len(fields) == 2 else ""
        instructions.append((keyword, body))

        heredocs = list(heredoc_pattern.finditer(body))
        body_without_supported_heredocs = heredoc_pattern.sub("", body)
        if heredoc_operator_pattern.search(body_without_supported_heredocs):
            raise ValueError("unsupported Dockerfile heredoc syntax")
        for heredoc in heredocs:
            delimiter_token = heredoc.group("delimiter")
            if delimiter_token[0] in {"'", '"'}:
                delimiter = delimiter_token[1:-1]
            else:
                delimiter = delimiter_token
            strip_tabs = heredoc.group("strip_tabs") == "-"
            while line_index < len(raw_lines):
                candidate = raw_lines[line_index]
                comparison = candidate.lstrip("\t") if strip_tabs else candidate
                line_index += 1
                if comparison == delimiter:
                    break
            else:
                raise ValueError(f"unterminated Dockerfile heredoc: {delimiter}")
    return instructions


def _dockerfile_base_authority(dockerfile_text: str, *, path: str) -> tuple[str, str]:
    relevant_args: dict[str, list[str | None]] = {'IMAGE_SOURCE': [], 'IMAGE_NAME_VERSION': []}  # fmt: skip  # noqa: E501
    from_bodies: list[str] = []
    first_from_seen = False
    arg_pattern = re.compile('^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\\s*=\\s*(?P<value>\\"[^\\"\\r\\n]*\\"|[^\\s#]+))?(?:\\s+#.*)?$')  # fmt: skip  # noqa: E501
    from_pattern = re.compile('^(?:--platform=(?P<platform>[A-Za-z0-9_./${}:+-]+)\\s+)?\\$\\{IMAGE_SOURCE\\}/\\$\\{IMAGE_NAME_VERSION\\}(?:\\s+(?i:AS)\\s+[A-Za-z0-9][A-Za-z0-9_.-]*)?(?:\\s+#.*)?$')  # fmt: skip  # noqa: E501

    for keyword, body in _dockerfile_instructions(dockerfile_text):
        if keyword == "ARG":
            match = arg_pattern.fullmatch(body)
            if match is not None and match.group("name") in relevant_args:
                if first_from_seen:
                    raise ValueError(f'Docker recipe base-image ARG declarations must precede the first FROM instruction: {path}')  # fmt: skip  # noqa: E501
                value = match.group("value")
                if value is not None and value.startswith('"'): value = value[1:-1]  # noqa: E701,E501
                relevant_args[match.group("name")].append(value)
        elif keyword == "FROM":
            first_from_seen = True
            from_bodies.append(body)

    for name, values in relevant_args.items():
        if len(values) != 1 or values[0] is None:
            raise ValueError(f'Docker recipe must declare exactly one {name} ARG with a default: {path}')  # fmt: skip  # noqa: E501
    if not from_bodies or any(
        from_pattern.fullmatch(body) is None for body in from_bodies
    ):
        raise ValueError(f'Docker recipe requires every FROM instruction to consume ${{IMAGE_SOURCE}}/${{IMAGE_NAME_VERSION}}: {path}')  # fmt: skip  # noqa: E501
    return relevant_args["IMAGE_SOURCE"][0], relevant_args["IMAGE_NAME_VERSION"][0]  # type: ignore[return-value]


def validate_repository_recipe_inventory(
    catalog: dict[str, Any], *, repository_root: Path = REPO_ROOT
) -> None:
    recipes = catalog.get("docker_recipes")
    if not isinstance(recipes, list) or not recipes: raise ValueError('docker_recipes must be a non-empty array')  # noqa: E701,E501
    _require_unique_ids(recipes, "Docker recipe")
    products = {item["id"]: item for item in catalog["upstream_products"]}
    normalized_paths: dict[str, str] = {}
    cache_scopes: set[str] = set()
    allowed_lanes = {"pr-smoke", "hardware-e2e", "manual", "formal-release"}
    path_pattern = re.compile(r"^docker/Dockerfile\.ucm-[a-z0-9][a-z0-9._-]{0,127}$")
    safe_id_pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
    safe_runner_label_pattern = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

    for index, recipe in enumerate(recipes):
        location = f"docker_recipes[{index}]"
        if not isinstance(recipe, dict): raise ValueError(f'{location} must be an object')  # noqa: E701,E501
        if safe_id_pattern.fullmatch(str(recipe.get("id", ""))) is None:
            raise ValueError(f"{location}.id must be a lowercase OCI-safe identifier")
        path = recipe.get("path")
        if not isinstance(path, str) or path_pattern.fullmatch(path) is None:
            raise ValueError(f'{location}.path must name docker/Dockerfile.ucm-* using a conservative safe filename')  # fmt: skip  # noqa: E501
        normalized = posixpath.normpath(path)
        collision_key = normalized.casefold()
        if normalized != path or collision_key in normalized_paths:
            previous = normalized_paths.get(collision_key)
            raise ValueError(f'normalized Docker recipe path collision: {path!r} and {previous!r}')  # fmt: skip  # noqa: E501
        normalized_paths[collision_key] = path

        lanes = recipe.get("lanes")
        if (
            not isinstance(lanes, list)
            or not lanes
            or any(lane not in allowed_lanes for lane in lanes)
            or len(lanes) != len(set(lanes))
        ):
            raise ValueError(f"{location}.lanes are invalid")
        status = recipe.get("status")
        build_mode = recipe.get("build_mode")
        if status not in {'active', 'legacy', 'nightly', 'specialized'}: raise ValueError(f'{location}.status is invalid')  # noqa: E701,E501
        if build_mode not in {"generic-install-only", "legacy-source-build"}:
            raise ValueError(f"{location}.build mode is invalid")
        if build_mode == "legacy-source-build" and "formal-release" in lanes:
            raise ValueError("legacy-source-build recipes cannot enter formal-release")
        if "formal-release" in lanes and (
            build_mode != "generic-install-only" or status != "active"
        ):
            raise ValueError('formal-release repository recipes must be active generic-install-only')  # fmt: skip  # noqa: E501
        if status in {"legacy", "nightly", "specialized"} and not recipe.get(
            "exclusion_reason"
        ):
            raise ValueError(f'{location} requires an exclusion reason outside formal release')  # fmt: skip  # noqa: E501
        if status == 'nightly' and set(lanes) != {'manual'}: raise ValueError('nightly Docker recipes are manual-only')  # noqa: E701,E501
        base_image = recipe.get("base_image")
        if (
            not isinstance(base_image, dict)
            or RECIPE_BASE_SOURCE_PATTERN.fullmatch(str(base_image.get("source", "")))
            is None
            or RECIPE_BASE_NAME_VERSION_PATTERN.fullmatch(
                str(base_image.get("name_version", ""))
            )
            is None
        ):
            raise ValueError(f'{location}.base image must use output-safe OCI source and tag fields')  # fmt: skip  # noqa: E501
        runner = recipe.get("runner")
        runner_labels = [runner] if isinstance(runner, str) else runner
        if (
            not isinstance(runner_labels, list)
            or not runner_labels
            or any(
                not isinstance(label, str)
                or safe_runner_label_pattern.fullmatch(label) is None
                for label in runner_labels
            )
        ):
            raise ValueError(f"{location}.runner label is not Actions-safe")
        if "pr-smoke" in lanes and runner != catalog["runner_map"].get(
            recipe.get("cpu_arch")
        ):
            raise ValueError('pr-smoke Docker recipes require the catalog hosted runner')  # fmt: skip  # noqa: E501
        if "hardware-e2e" in lanes and (
            not isinstance(runner, list) or "self-hosted" not in runner
        ):
            raise ValueError("hardware-e2e Docker recipes require a self-hosted runner")
        if recipe.get("platform") != f"linux/{recipe.get('cpu_arch')}":
            raise ValueError(f"{location}.platform differs from explicit cpu_arch")

        cache_scope = recipe.get("cache_scope")
        if (
            not isinstance(cache_scope, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]+", cache_scope) is None
        ):
            raise ValueError(f'{location}.cache scope must be an output-safe lowercase value')  # fmt: skip  # noqa: E501
        if cache_scope in cache_scopes: raise ValueError(f'duplicate Docker recipe cache scope {cache_scope!r}')  # noqa: E701,E501
        cache_scopes.add(cache_scope)
        build_args = recipe.get("build_args")
        if not isinstance(build_args, list) or build_args != sorted(build_args):
            raise ValueError(f"{location}.build_args must be canonically sorted")
        if any(
            not isinstance(value, str)
            or RECIPE_BUILD_ARG_PATTERN.fullmatch(value) is None
            for value in build_args
        ):
            raise ValueError(f'{location}.build_args must contain output-safe NAME=value entries')  # fmt: skip  # noqa: E501
        argument_names = [str(value).partition("=")[0] for value in build_args]
        if len(argument_names) != len(set(argument_names)): raise ValueError(f'{location}.build_args contains duplicate names')  # noqa: E701,E501
        if {"IMAGE_SOURCE", "IMAGE_NAME_VERSION"} & set(argument_names):
            raise ValueError(f'{location}.build_args must not duplicate base-image authority')  # fmt: skip  # noqa: E501

        if recipe.get("product") in {"vllm", "vllm-ascend"}:
            product_id = recipe.get("upstream_product_id")
            product = products.get(product_id)
            if product is None or product_id != recipe.get("product"):
                raise ValueError(f'{location} requires an explicit matching upstream product')  # fmt: skip  # noqa: E501
            if recipe.get("upstream_variant") not in {
                variant["id"] for variant in product["variants"]
            }:
                raise ValueError(f'{location} requires a declared upstream target variant')  # fmt: skip  # noqa: E501

        recipe_path = repository_root / path
        component_path = repository_root
        for component in PurePosixPath(path).parts:
            component_path = component_path / component
            if component_path.is_symlink(): raise ValueError(f'registered Docker recipe has a symlink component: {path}')  # noqa: E701,E501
        try:
            recipe_path.resolve().relative_to(repository_root.resolve())
        except ValueError as error:
            raise ValueError(f"{location}.path escapes the repository") from error
        if not recipe_path.is_file(): raise ValueError(f'registered Docker recipe does not exist: {path}')  # noqa: E701,E501
        dockerfile_text = recipe_path.read_text(encoding="utf-8")
        source, name_version = _dockerfile_base_authority(dockerfile_text, path=path)
        if recipe.get("base_image") != {
            "source": source,
            "name_version": name_version,
        }:
            raise ValueError(f"Docker recipe base image differs from catalog: {path}")

def repository_recipe_matrix(
    catalog: dict[str, Any],
    *,
    lane: str,
    repository_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if lane not in {"pr-smoke", "hardware-e2e", "manual", "formal-release"}:
        raise ValueError(f"unsupported repository recipe lane: {lane}")
    validate_repository_recipe_inventory(catalog, repository_root=repository_root)
    catalog_sha256 = sha256_value(catalog)
    tasks: list[dict[str, Any]] = []
    for recipe in sorted(catalog["docker_recipes"], key=lambda item: item["id"]):
        if lane not in recipe["lanes"]:
            continue
        path = repository_root / recipe["path"]
        base_image = copy.deepcopy(recipe["base_image"])
        build_args = sorted([*recipe['build_args'], f"IMAGE_NAME_VERSION={base_image['name_version']}", f"IMAGE_SOURCE={base_image['source']}"])  # fmt: skip  # noqa: E501
        task: dict[str, Any] = {'task_id': recipe['id'], 'catalog_sha256': catalog_sha256, 'dockerfile_sha256': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), 'path': recipe['path'], 'lane': lane, 'runner': copy.deepcopy(recipe['runner']), 'cpu_arch': recipe['cpu_arch'], 'platform': recipe['platform'], 'product': recipe['product'], 'backend': recipe['backend'], 'variant': recipe['variant'], 'upstream_version': recipe['upstream_version'], 'base_image': base_image, 'upstream_product_id': recipe.get('upstream_product_id'), 'upstream_variant': recipe.get('upstream_variant'), 'status': recipe['status'], 'build_mode': recipe['build_mode'], 'cache_scope': recipe['cache_scope'], 'build_args': build_args, 'engine_type': recipe['engine_type'], 'install_hook': recipe['install_hook'], 'exclusion_reason': recipe['exclusion_reason']}  # fmt: skip  # noqa: E501
        task["task_sha256"] = sha256_value(task)
        tasks.append(task)
    result: dict[str, Any] = {'kind': 'ucm-repository-recipe-matrix', 'schema_version': 1, 'lane': lane, 'catalog_sha256': catalog_sha256, 'protected_environment': catalog['source']['protected_environment'], 'count': len(tasks), 'include': tasks}  # fmt: skip  # noqa: E501
    result["matrix_sha256"] = sha256_value(result)
    return result


def select_repository_recipe_task(
    catalog: dict[str, Any],
    *,
    lane: str,
    task_id: str,
    expected_matrix_sha256: str,
    expected_catalog_sha256: str | None = None,
    expected_task_sha256: str | None = None,
    repository_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    matrix = repository_recipe_matrix(catalog, lane=lane, repository_root=repository_root)  # fmt: skip  # noqa: E501
    if matrix["matrix_sha256"] != expected_matrix_sha256:
        raise ValueError("repository recipe matrix hash differs from expected value")
    if (
        expected_catalog_sha256 is not None
        and matrix["catalog_sha256"] != expected_catalog_sha256
    ):
        raise ValueError("repository recipe catalog hash differs from expected value")
    matches = [task for task in matrix["include"] if task["task_id"] == task_id]
    if len(matches) != 1:
        raise ValueError(f'repository recipe task selection requires exactly one {lane!r} match for opaque task ID {task_id!r}; found {len(matches)}')  # fmt: skip  # noqa: E501
    task = matches[0]
    if expected_task_sha256 is not None and task["task_sha256"] != expected_task_sha256:
        raise ValueError("repository recipe task hash differs from expected value")
    return task


def validate_resolved_upstreams(resolved_upstreams: object, *, relaxed: bool = False) -> None:
    if not isinstance(resolved_upstreams, list): raise ValueError('resolved_upstreams must be an array')  # noqa: E701,E501
    snapshot_keys = {'product_id', 'repository', 'tag', 'version', 'channel', 'variant', 'index_digest', 'members', 'target_repository', 'target_tag'}  # fmt: skip  # noqa: E501
    member_keys = {"manifest_digest", "config_digest"}
    digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    logical_identities: set[tuple[str, ...]] = set()
    for index, snapshot in enumerate(resolved_upstreams):
        location = f"resolved_upstreams[{index}]"
        if not isinstance(snapshot, dict): raise ValueError(f'{location} must be an object')  # noqa: E701,E501
        missing = sorted(snapshot_keys - set(snapshot))
        extras = sorted(set(snapshot) - snapshot_keys)
        if missing or extras: raise ValueError(f'{location} requires exact key set; missing={missing}, extra={extras}')  # noqa: E701,E501
        for key in snapshot_keys - {"members", "index_digest"}:
            if not isinstance(snapshot[key], str) or not snapshot[key]:
                raise ValueError(f"{location}.{key} must be a non-empty string")
        if snapshot['channel'] not in {'stable', 'rc'}: raise ValueError(f'{location}.channel must be stable or rc')  # noqa: E701,E501
        if OCI_REPOSITORY_PATTERN.fullmatch(snapshot["target_repository"]) is None:
            raise ValueError(f'{location}.target_repository must use canonical OCI repository syntax')  # fmt: skip  # noqa: E501
        if OCI_TAG_PATTERN.fullmatch(snapshot["target_tag"]) is None:
            raise ValueError(f"{location}.target_tag must use strict OCI tag syntax")
        if (
            not isinstance(snapshot["index_digest"], str)
            or digest_pattern.fullmatch(snapshot["index_digest"]) is None
        ):
            raise ValueError(f"{location}.index_digest must be an exact sha256 digest")
        identity_version = snapshot['version'] if relaxed else str(_pep440_version(snapshot['version'], f'{location}.version'))  # fmt: skip  # noqa: E501
        identity = (snapshot['product_id'], snapshot['repository'], snapshot['tag'], identity_version, snapshot['channel'], snapshot['variant'])  # fmt: skip  # noqa: E501
        if identity in logical_identities: raise ValueError(f'{location} has duplicate logical upstream identity: {identity}')  # noqa: E701,E501
        logical_identities.add(identity)
        members = snapshot["members"]
        if not isinstance(members, dict) or not members: raise ValueError(f'{location}.members must be a non-empty object')  # noqa: E701,E501
        for architecture, member in members.items():
            member_location = f"{location}.members.{architecture}"
            if not isinstance(architecture, str) or not architecture:
                raise ValueError(f"{location}.members has an invalid architecture")
            if not isinstance(member, dict): raise ValueError(f'{member_location} must be an object')  # noqa: E701,E501
            missing = sorted(member_keys - set(member))
            extras = sorted(set(member) - member_keys)
            if missing or extras: raise ValueError(f'{member_location} requires exact key set; missing={missing}, extra={extras}')  # noqa: E701,E501
            for digest_name in sorted(member_keys):
                if (
                    not isinstance(member[digest_name], str)
                    or digest_pattern.fullmatch(member[digest_name]) is None
                ):
                    raise ValueError(f'{member_location}.{digest_name} must be an exact sha256 digest')  # fmt: skip  # noqa: E501


_LINKED_WHEEL_FIELDS = tuple('spec_id profile_id runner cpu_arch platform builder builder_sha256 build python_abi python_version wheel_version wheel_platform required_native forbidden_native allowed_dt_needed external_required_dependencies dependency_lock_sha256 dependency_lock runtime_requirements runtime_patch_manifest_sha256 write_authority build_eligible'.split())  # fmt: skip  # noqa: E501


def _find_profile(
    catalog: dict[str, Any],
    product: dict[str, Any],
    snapshot: dict[str, Any],
    architecture: str,
    *,
    relaxed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    variant = next((v for v in product['variants'] if v['id'] == snapshot['variant']), None)  # fmt: skip  # noqa: E501
    if variant is None:
        raise ValueError(f"snapshot variant {snapshot['variant']!r} is not declared by upstream product {product['id']!r}")  # fmt: skip  # noqa: E501
    version = None if relaxed else _pep440_version(snapshot["version"], "resolved upstream version")  # fmt: skip  # noqa: E501
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for rule in catalog["compatibility"]["rules"]:
        if product["id"] not in rule["upstream_products"] or snapshot["variant"] not in rule["variants"] or architecture not in rule["cpu_architectures"]:  # fmt: skip  # noqa: E501
            continue
        # PR/pinned path (relaxed) skips the catalog's version_specifier +
        # upstream-channel gates so an out-of-specifier / out-of-channel tag
        # can still match a profile by (product, variant, arch, accelerator).
        if not relaxed and (
            version not in _pep440_specifier(rule["version_specifier"], f"compatibility rule {rule['id']!r}")
            or snapshot["channel"] not in rule["upstream_channels"]
        ):
            continue
        for profile in catalog["wheel_profiles"]:
            if (
                architecture in profile["cpu_arch"]
                and profile["accelerator"] == rule["accelerator"]
                and profile["accelerator_runtime"] in rule["accelerator_runtimes"]
                and variant["npu_arch"] in profile["npu_arch"]
                and variant["npu_arch"] in rule["npu_architectures"]
                and any(item in rule["operating_systems"] for item in profile["os"])
                and profile["python_abi"] in rule["python_abis"]
            ):
                matches.append((profile, rule))
    if not matches:
        return None
    if len(matches) > 1:
        matched = sorted((f"{profile['id']} via {rule['id']}" for profile, rule in matches))  # fmt: skip  # noqa: E501
        raise ValueError(f"resolved upstream member matches overlapping wheel profiles: product={product['id']}, tag={snapshot['tag']}, architecture={architecture}, matches={matched}")  # fmt: skip  # noqa: E501
    return matches[0]


def _matching_profile(
    catalog: dict[str, Any],
    product: dict[str, Any],
    snapshot: dict[str, Any],
    architecture: str,
    *,
    relaxed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    match = _find_profile(
        catalog, product, snapshot, architecture, relaxed=relaxed
    )
    if match is None:
        raise ValueError(f"resolved upstream member has no compatible wheel profile: product={product['id']}, tag={snapshot['tag']}, architecture={architecture}")  # fmt: skip  # noqa: E501
    return match


def candidate_exclusion_reason(
    catalog: dict[str, Any],
    product: dict[str, Any],
    candidate: dict[str, Any],
    patch_manifest: dict[str, Any],
    *,
    relaxed: bool = False,
) -> str | None:
    product_variant = next(
        (item for item in product["variants"] if item["id"] == candidate["variant"]),
        None,
    )
    if product_variant is None:
        raise ValueError(
            f"snapshot variant {candidate['variant']!r} is not declared by upstream product {product['id']!r}"
        )
    runtime_product = product["runtime_product"]
    patch_variant = product_variant["runtime_patch_variants"][runtime_product]
    patch_snapshot = {**candidate, "runtime_product": runtime_product}
    if (
        find_runtime_patch_rule(
            patch_manifest, patch_snapshot, patch_variant, relaxed=relaxed
        )
        is None
    ):
        return "runtime-patch-unsupported"
    for architecture in product["required_cpu_architectures"]:
        if (
            _find_profile(
                catalog, product, candidate, architecture, relaxed=relaxed
            )
            is None
        ):
            return "compatibility-unsupported"
    return None


_PROFILE_DIRECT_FIELDS = tuple('accelerator accelerator_runtime python_version python_abi wheel_version wheel_platform binary_profile_id dist_name'.split())  # fmt: skip  # noqa: E501
_PROFILE_DEEPCOPY_FIELDS = tuple('validation_targets required_native forbidden_native allowed_dt_needed external_required_dependencies'.split())  # fmt: skip  # noqa: E501
_FAMILY_IDENTITY_KEYS = tuple('product_id repository tag variant index_digest target_repository target_tag'.split())  # fmt: skip  # noqa: E501
_RUNTIME_KEYS = tuple("repository tag version channel variant index_digest".split())


@dataclass(frozen=True, slots=True)
class ReleasePlan:

    lane: str
    wheel_tasks: list[dict[str, Any]]
    image_tasks: list[dict[str, Any]]
    family_tasks: list[dict[str, Any]]

    @classmethod
    def build(
        cls,
        catalog: dict[str, Any],
        resolved_upstreams: list[dict[str, Any]],
        *,
        lane: str,
        repository_root: Path = REPO_ROOT,
        relaxed: bool = False,
    ) -> "ReleasePlan":
        validate_catalog(catalog, repository_root=repository_root)
        _validate_resolved_builder_roots(catalog)
        validate_resolved_upstreams(resolved_upstreams, relaxed=relaxed)
        if lane not in catalog['lanes']: raise ValueError(f'unsupported validation lane: {lane}')  # noqa: E701,E501
        patch_manifest = runtime_patch_manifest(catalog, repository_root=repository_root)  # fmt: skip  # noqa: E501
        patch_manifest_sha256 = runtime_patch_manifest_sha256(patch_manifest)
        runtime_requirements = python_runtime_requirements(catalog)
        products = {item["id"]: item for item in catalog["upstream_products"]}
        write_authority = [] if lane == 'feature-candidate' else ['github-prerelease', 'ghcr-final-index', 'ghcr-private-staging']  # fmt: skip  # noqa: E501
        _version_sort = (lambda v: v) if relaxed else (lambda v: _pep440_version(v, 'resolved upstream version'))  # fmt: skip  # noqa: E501
        snapshots = sorted(resolved_upstreams, key=lambda item: (item['product_id'], _version_sort(item['version']), item['variant'], item['tag'], item['repository'], item['channel'], item['target_repository'], item['target_tag'], item['index_digest'], sha256_value(item['members'])))  # fmt: skip  # noqa: E501
        wheel_tasks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        image_tasks: list[dict[str, Any]] = []
        family_tasks: list[dict[str, Any]] = []
        family_coordinates: set[str] = set()

        for snapshot in snapshots:
            product = products.get(snapshot["product_id"])
            if product is None: raise ValueError(f"resolved upstream references unknown product {snapshot['product_id']!r}")  # noqa: E701,E501
            if snapshot["repository"] != product["repository"]:
                raise ValueError('resolved upstream repository differs from catalog product')  # fmt: skip  # noqa: E501
            product_variant = next((item for item in product['variants'] if item['id'] == snapshot['variant']), None)  # fmt: skip  # noqa: E501
            if product_variant is None:
                raise ValueError(f"snapshot variant {snapshot['variant']!r} is not declared by upstream product {product['id']!r}")  # fmt: skip  # noqa: E501
            runtime_patch_variants = copy.deepcopy(product_variant['runtime_patch_variants'])  # fmt: skip  # noqa: E501
            patch_rule = _matching_runtime_patch_rule(patch_manifest, {**snapshot, 'runtime_product': product['runtime_product']}, runtime_patch_variants[product['runtime_product']], relaxed=relaxed)  # fmt: skip  # noqa: E501
            if not relaxed:
                version = _pep440_version(snapshot["version"], "resolved upstream version")
                if snapshot["channel"] == "stable" and version.is_prerelease:
                    raise ValueError("channel stable requires a final version")
                if snapshot["channel"] == "rc" and not (
                    version.pre
                    and version.pre[0] == "rc"
                    and not (version.epoch or version.dev or version.post or version.local)
                ):
                    raise ValueError("channel rc requires a plain rcN version")
                if version not in _pep440_specifier(
                    product["version_specifier"],
                    f"upstream product {product['id']!r}.version_specifier",
                ):
                    raise ValueError('resolved upstream version is outside product selection')  # fmt: skip  # noqa: E501
                if snapshot["channel"] not in product["channels"]:
                    raise ValueError("resolved upstream channel is not selected by product")
            members = snapshot["members"]
            missing = sorted(set(product["required_cpu_architectures"]) - set(members))
            if missing: raise ValueError(f"resolved upstream {snapshot['tag']} is missing required CPU architectures: {missing}")  # noqa: E701,E501
            coordinate = f"{snapshot['target_repository']}:{snapshot['target_tag']}"
            if coordinate in family_coordinates: raise ValueError(f'duplicate target image coordinate: {coordinate}')  # noqa: E701,E501
            family_coordinates.add(coordinate)
            family_identity = {k: snapshot[k] for k in _FAMILY_IDENTITY_KEYS}
            family_task_id = f"family-{sha256_value(family_identity).removeprefix('sha256:')}"  # fmt: skip  # noqa: E501
            family_images: list[dict[str, Any]] = []

            for architecture in sorted(members):
                profile, rule = _matching_profile(catalog, product, snapshot, architecture, relaxed=relaxed)  # fmt: skip  # noqa: E501
                wheel_key = (profile["id"], architecture)
                if wheel_key not in wheel_tasks_by_key:
                    declaration = {'spec_id': f"{profile['id']}-{architecture}", 'profile_id': profile['id'], **{k: profile[k] for k in _PROFILE_DIRECT_FIELDS}, 'npu_arch_or_na': profile['npu_arch'][0], 'os': profile['os'][0], 'cpu_arch': architecture, **{k: copy.deepcopy(profile[k]) for k in _PROFILE_DEEPCOPY_FIELDS}}  # fmt: skip  # noqa: E501
                    dependency_lock = {'build_tools': build_tool_dependency_records(catalog, profile['python_abi'], architecture), 'runtime_dependencies': runtime_dependency_records(catalog, profile['python_abi'], architecture)}  # fmt: skip  # noqa: E501
                    builder = profile["builders"][architecture]
                    wheel_identity = {'profile_id': profile['id'], 'cpu_arch': architecture, 'builder_sha256': sha256_value(builder), 'dependency_lock_sha256': sha256_value(dependency_lock)}  # fmt: skip  # noqa: E501
                    wheel_task: dict[str, Any] = {'task_id': f"wheel-{sha256_value(wheel_identity).removeprefix('sha256:')}", **declaration, 'declaration_sha256': sha256_value(declaration), 'runner': catalog['runner_map'][architecture], 'platform': f'linux/{architecture}', 'builder': builder, 'builder_sha256': sha256_value(builder), 'build': copy.deepcopy(profile['build']), 'dependency_lock_sha256': sha256_value(dependency_lock), 'dependency_lock': dependency_lock, 'runtime_requirements': copy.deepcopy(runtime_requirements), 'runtime_patch_manifest': copy.deepcopy(patch_manifest), 'runtime_patch_manifest_sha256': patch_manifest_sha256, 'write_authority': write_authority, 'build_eligible': True}  # fmt: skip  # noqa: E501
                    wheel_task["artifact_name"] = f"ucm-wheel-{wheel_task['task_id']}"
                    wheel_task["task_sha256"] = sha256_value(wheel_task)
                    wheel_tasks_by_key[wheel_key] = wheel_task
                wheel_task = wheel_tasks_by_key[wheel_key]
                member = members[architecture]
                runtime = {'product_id': snapshot['product_id'], 'repository': snapshot['repository'], 'tag': snapshot['tag'], 'version': snapshot['version'], 'channel': snapshot['channel'], 'variant': snapshot['variant'], 'index_digest': snapshot['index_digest'], **member}  # fmt: skip  # noqa: E501
                image_identity = {'family_task_id': family_task_id, 'wheel_task_id': wheel_task['task_id'], 'cpu_arch': architecture, 'runtime_sha256': sha256_value(runtime)}  # fmt: skip  # noqa: E501
                image_task: dict[str, Any] = {'task_id': f"image-{sha256_value(image_identity).removeprefix('sha256:')}", 'family_task_id': family_task_id, 'wheel_task_id': wheel_task['task_id'], 'compatibility_rule_id': rule['id'], 'runtime_patch_rule_id': patch_rule['id'], 'runtime_patch_product': product['runtime_product'], 'runtime_patch_strategy': patch_rule['strategy'], 'runtime_patch_variants': copy.deepcopy(runtime_patch_variants), 'runtime': runtime, 'runtime_sha256': sha256_value(runtime), 'target_repository': snapshot['target_repository'], 'target_tag': snapshot['target_tag'], **{k: wheel_task[k] for k in _LINKED_WHEEL_FIELDS}}  # fmt: skip  # noqa: E501
                image_task["artifact_name"] = f"ucm-image-{image_task['task_id']}"
                image_task["wheel_artifact_name"] = wheel_task["artifact_name"]
                image_task["task_sha256"] = sha256_value(image_task)
                image_tasks.append(image_task)
                family_images.append(image_task)

            family_runtime = {k: snapshot[k] for k in _RUNTIME_KEYS}
            control_image = family_images[0]
            family_task: dict[str, Any] = {'task_id': family_task_id, 'product_id': snapshot['product_id'], 'control_task_id': control_image['task_id'], 'control_arch': control_image['cpu_arch'], 'control_runner': control_image['runner'], 'runner': [item['runner'] for item in family_images], 'cpu_arch': [item['cpu_arch'] for item in family_images], 'platform': [item['platform'] for item in family_images], 'builder': [item['builder'] for item in family_images], 'builder_sha256': [item['builder_sha256'] for item in family_images], 'runtime': family_runtime, 'runtime_sha256': sha256_value(family_runtime), 'snapshot_sha256': sha256_value(snapshot), 'target_repository': snapshot['target_repository'], 'target_tag': snapshot['target_tag'], 'image_task_ids': [item['task_id'] for item in family_images], 'wheel_task_ids': {item['cpu_arch']: item['wheel_task_id'] for item in family_images}, 'member_set_sha256': sha256_value([item['task_sha256'] for item in family_images]), 'write_authority': write_authority}  # fmt: skip  # noqa: E501
            family_task["artifact_name"] = f"ucm-family-{family_task['task_id']}"
            family_task["task_sha256"] = sha256_value(family_task)
            family_tasks.append(family_task)

        wheel_tasks = sorted(wheel_tasks_by_key.values(), key=lambda item: (item['profile_id'], item['cpu_arch']))  # fmt: skip  # noqa: E501
        limits = catalog["matrix_limits"]
        cardinalities = {'wheel_tasks': len(wheel_tasks), 'image_tasks': len(image_tasks), 'family_tasks': len(family_tasks)}  # fmt: skip  # noqa: E501
        for task_kind, count in cardinalities.items():
            limit = limits[f"max_{task_kind}"]
            if count > limit: raise ValueError(f'matrix limit max_{task_kind}={limit} exceeded by exact generated set of {count}')  # noqa: E701,E501
        return cls(lane=lane, wheel_tasks=wheel_tasks, image_tasks=image_tasks, family_tasks=family_tasks)  # fmt: skip  # noqa: E501


def expand_release_plan(
    catalog: dict[str, Any],
    resolved_upstreams: list[dict[str, Any]],
    *,
    lane: str,
    repository_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    plan = ReleasePlan.build(catalog, resolved_upstreams, lane=lane, repository_root=repository_root)  # fmt: skip  # noqa: E501
    result: dict[str, Any] = {'schema_version': 2, 'kind': 'ucm-resolved-build-plan', 'lane': plan.lane, 'wheel_tasks': plan.wheel_tasks, 'image_tasks': plan.image_tasks, 'family_tasks': plan.family_tasks, 'cardinalities': {'wheel_tasks': len(plan.wheel_tasks), 'image_tasks': len(plan.image_tasks), 'family_tasks': len(plan.family_tasks)}}  # fmt: skip  # noqa: E501
    result["plan_sha256"] = sha256_value(result)
    return result


RELEASE_KEYS = frozenset('kind schema_version image_revision source lanes runner_map upstream_products compatibility chart publish wheel_profiles'.split())  # fmt: skip  # noqa: E501
OPTIONAL_CATALOG_KEYS = frozenset('ucm_version pr_smoke docker_recipes runtime_patch_rules matrix_limits scan_limits python_runtime_dependencies python_build_lock'.split())  # fmt: skip  # noqa: E501
SUPPLEMENTARY_TOP_LEVEL_KEYS = frozenset({'pr_smoke', 'docker_recipes', 'runtime_patch_rules', 'matrix_limits', 'scan_limits', 'python_runtime_dependencies', 'python_build_lock'})  # fmt: skip  # noqa: E501
LANES = ("feature-candidate", "protected-tag")


def _exact_keys(
    value: dict[str, Any],
    expected: set[str],
    location: str,
    *,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected - optional)
    if missing or extras: raise ValueError(f'{location} requires exact key set; missing={missing}, extra={extras}')  # noqa: E701,E501


def _validate_cross_config(
    release: dict[str, Any], *, repository_root: Path = REPO_ROOT
) -> None:
    validate_catalog(release, repository_root=repository_root)
    publish = compute_publish_plan(release)
    if publish["pypi"]["dists"] != [
        "uc-manager-cuda",
        "uc-manager-cann-a2",
        "uc-manager-cann-a3",
    ]:
        raise ValueError("PyPI channel requires the exact three release distributions")
    if any(publish[channel]["enabled"] for channel in PUBLISH_CHANNELS[:-1]) and not publish["github_release"]["enabled"]:
        raise ValueError("enabled public channels require the GitHub Release Draft barrier")
    if publish["dockerhub"]["enabled"] and not publish["ghcr"]["enabled"]:
        raise ValueError("Docker Hub publication requires GHCR source publication")
    profiles = release["wheel_profiles"]
    for profile in profiles:
        architectures = set(profile["cpu_arch"])
        if architectures != set(profile["builders"]):
            raise ValueError(f"wheel profile {profile['id']!r} builder architectures do not match cpu_arch")  # fmt: skip  # noqa: E501
        missing_runners = sorted(architectures - set(release["runner_map"]))
        if missing_runners: raise ValueError(f"wheel profile {profile['id']!r} has no runner for {missing_runners}")  # noqa: E701,E501
    products = {product["id"]: product for product in release["upstream_products"]}
    chart_selectors: set[tuple[str, str]] = set()
    for case in release["chart"]["validation_cases"]:
        selector = (case["product_id"], case["variant"])
        product = products.get(case["product_id"])
        if product is None or case["variant"] not in {
            variant["id"] for variant in product["variants"]
        }:
            raise ValueError('Chart validation must select a declared upstream product variant')  # fmt: skip  # noqa: E501
        if selector in chart_selectors: raise ValueError('Chart validation product/variant selectors must be unique')  # noqa: E701,E501
        chart_selectors.add(selector)


def _load_supplementary_configs(
    release_path: Path, repository_root: Path
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    candidates = (DEFAULT_RELEASE.parents[1] / 'docker-recipes.yaml', DEFAULT_RELEASE.parent / 'toolchain.lock.yaml', DEFAULT_RELEASE.parent / 'native-contract.yaml', DEFAULT_RELEASE.parents[2] / 'ucm' / 'integration' / 'runtime-patches.yaml')  # fmt: skip  # noqa: E501
    for path in candidates:
        if path.is_file(): merged.update(load_yaml(path))  # noqa: E701
    return merged


def load_catalog(
    release_path: Path = DEFAULT_RELEASE,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    *,
    repository_root: Path = REPO_ROOT,
    repository: str | None = None,
    version_override: str | None = None,
) -> dict[str, Any]:
    config_schema = load_json(schema_dir / "config.schema.json")
    load_json(schema_dir / "release-manifest.schema.json")
    load_json(schema_dir / "image-result.schema.json")
    release = load_yaml(release_path)
    supplementary = _load_supplementary_configs(release_path, repository_root)
    profile_ids = {p["id"] for p in release.get("wheel_profiles", [])}
    for key, value in supplementary.items():
        if key == "builders" and isinstance(value, dict):
            for profile in release.get("wheel_profiles", []):
                pid = profile["id"]
                if pid in value and 'builders' not in profile: profile['builders'] = value[pid]  # noqa: E701,E501
        elif key in profile_ids and isinstance(value, dict):
            for profile in release.get("wheel_profiles", []):
                if profile["id"] == key:
                    for field in (
                        "required_native",
                        "forbidden_native",
                        "allowed_dt_needed",
                    ):
                        if field in value and field not in profile: profile[field] = value[field]  # noqa: E701,E501
        if key not in release and key in SUPPLEMENTARY_TOP_LEVEL_KEYS: release[key] = value  # noqa: E701,E501
    resolved_repository = resolve_repository(repository, repository_root=repository_root)  # fmt: skip  # noqa: E501
    release = resolve_owner_templates(release, repository=resolved_repository)
    validate_schema(release, config_schema)
    missing = sorted(RELEASE_KEYS - set(release))
    extras = sorted(set(release) - RELEASE_KEYS - OPTIONAL_CATALOG_KEYS)
    if missing or extras: raise ValueError(f'release.yaml requires exact key set; missing={missing}, extra={extras}')  # noqa: E701,E501
    release["source"]["repository"] = resolved_repository
    version = version_override or git_describe_pep440(repository_root)
    release["ucm_version"] = version
    release["source"]["release_tag"] = f"v{version}"
    release["chart"]["version"] = derive_chart_version(version)
    release["chart"]["app_version"] = version
    image_suffix = f"-ucm-{_oci_tag_version(version)}-r{release.get('image_revision', 1)}"  # fmt: skip  # noqa: E501
    for product in release.get("upstream_products", []):
        product["target_tag_suffix"] = image_suffix
    for profile in release.get("wheel_profiles", []):

        profile["wheel_version"] = version  # fmt: skip  # noqa: E501
        profile.setdefault("dist_name", "uc-manager")  # fmt: skip  # noqa: E501
    chart = load_yaml(repository_root / release["chart"]["source"] / "Chart.yaml")
    if chart.get('name') != release['chart']['name']: raise ValueError('Chart name does not match release.yaml')  # noqa: E701,E501
    _validate_cross_config(release, repository_root=repository_root)
    validate_repository_recipe_inventory(release, repository_root=repository_root)
    return release


PUBLISH_CHANNELS = ("pypi", "ghcr", "dockerhub", "chart_oci", "github_release")
PUBLISH_CHANNEL_KEYS = {
    "pypi": frozenset({"enabled", "index", "dists"}),
    "ghcr": frozenset({"enabled", "namespace"}),
    "dockerhub": frozenset({"enabled", "namespace"}),
    "chart_oci": frozenset({"enabled", "namespace"}),
    "github_release": frozenset({"enabled"}),
}


def compute_publish_plan(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    publish = catalog.get("publish")
    if not isinstance(publish, dict) or set(publish) != set(PUBLISH_CHANNELS):
        raise ValueError("publish configuration requires the exact five channels")
    for channel in PUBLISH_CHANNELS:
        config = publish[channel]
        if not isinstance(config, dict) or set(config) != PUBLISH_CHANNEL_KEYS[channel]:
            raise ValueError(f"publish channel {channel} configuration is malformed")
        if not isinstance(config["enabled"], bool):
            raise ValueError(f"publish channel {channel} enabled must be boolean")
        for field in PUBLISH_CHANNEL_KEYS[channel] - {"enabled", "dists"}:
            if not isinstance(config[field], str) or not config[field]:
                raise ValueError(f"publish channel {channel} {field} must be non-empty")
        if channel == "pypi":
            dists = config["dists"]
            if not isinstance(dists, list) or not dists or len(dists) != len(set(dists)) or any(not isinstance(dist, str) or not dist for dist in dists):
                raise ValueError("publish channel pypi dists must be a non-empty unique string array")
    return {channel: copy.deepcopy(publish[channel]) for channel in PUBLISH_CHANNELS}


def _git_output(repository_root: Path, *arguments: str) -> str | None:
    completed = subprocess.run(['git', '-C', str(repository_root), *arguments], text=True, capture_output=True, check=False)  # fmt: skip  # noqa: E501
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_commit(repository_root: Path, revision: str) -> str | None:
    commit = _git_output(repository_root, 'rev-parse', '--verify', f'{revision}^{{commit}}')  # fmt: skip  # noqa: E501
    if commit is None or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        return None
    return commit


def git_describe_pep440(repository_root: Path) -> str:
    describe = _git_output(repository_root, 'describe', '--tags', '--dirty')
    if describe is None:
        raise ValueError(f'git describe failed in {repository_root}; tag the release commit to derive the version')  # fmt: skip  # noqa: E501
    dirty = describe.endswith('-dirty')
    desc = describe[:-len('-dirty')] if dirty else describe
    desc = desc[1:] if desc.startswith('v') else desc
    match = re.fullmatch(r'(\d+(?:\.\d+)*(?:[a-z]+\d*)?)-(\d+)-g([0-9a-f]+)', desc)
    if match is None:
        base, local = desc, (['dirty'] if dirty else [])
    else:
        base, distance, sha = match.groups()
        base = f'{base}.dev{distance}'
        local = [f'g{sha}'] + (['dirty'] if dirty else [])
    return f'{base}+{".".join(local)}' if local else base  # fmt: skip  # noqa: E501


def _origin_repository(remote_url: str | None) -> str | None:
    if remote_url is None:
        return None
    prefixes = ("https://github.com/", "git@github.com:")
    for prefix in prefixes:
        if remote_url.startswith(prefix):
            repository = remote_url.removeprefix(prefix).removesuffix(".git")
            if re.fullmatch(r"[^/]+/[^/]+", repository):
                return repository
    return None


_OWNER_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_KNOWN_OWNER_PLACEHOLDERS = frozenset({"owner", "repo"})
_REPOSITORY_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def resolve_repository(
    repository: str | None = None,
    *,
    repository_root: Path = REPO_ROOT,
) -> str:
    if repository:
        candidate = repository.strip()
    else:
        candidate = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not candidate:
            remote = _git_output(repository_root, "remote", "get-url", "origin")
            candidate = _origin_repository(remote).strip() if remote else ""
    if not candidate or _REPOSITORY_IDENTITY_RE.fullmatch(candidate) is None:
        raise ValueError('could not resolve the running repository; pass --repository or set GITHUB_REPOSITORY (local dev infers from the origin remote)')  # fmt: skip  # noqa: E501
    return candidate


def resolve_owner_templates(catalog: Any, *, repository: str) -> Any:
    parts = repository.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"repository must be 'owner/name', got: {repository!r}")
    owner = parts[0].lower()
    repo = parts[1].lower()
    return _walk_owner_templates(catalog, owner=owner, repo=repo)


def _walk_owner_templates(value: Any, *, owner: str, repo: str) -> Any:
    if isinstance(value, str):
        return _substitute_owner(value, owner=owner, repo=repo)
    if isinstance(value, dict):
        return {key: _walk_owner_templates(item, owner=owner, repo=repo) for key, item in value.items()}  # fmt: skip  # noqa: E501
    if isinstance(value, list):
        return [_walk_owner_templates(item, owner=owner, repo=repo) for item in value]
    return value


def _substitute_owner(text: str, *, owner: str, repo: str) -> str:
    if "{" not in text:
        return text
    placeholders = _OWNER_PLACEHOLDER.findall(text)
    if not placeholders:
        return text
    unknown = sorted({name for name in placeholders if name not in _KNOWN_OWNER_PLACEHOLDERS})  # fmt: skip  # noqa: E501
    if unknown:
        raise ValueError(f'unknown owner template placeholder(s) {unknown} in {text!r}; recognised: {sorted(_KNOWN_OWNER_PLACEHOLDERS)}')  # fmt: skip  # noqa: E501
    return text.replace("{owner}", owner).replace("{repo}", repo)


def _is_ancestor(repository_root: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(['git', '-C', str(repository_root), 'merge-base', '--is-ancestor', ancestor, descendant], text=True, capture_output=True, check=False)  # fmt: skip  # noqa: E501
    return completed.returncode == 0


TAG_PREFLIGHT_AUTHORITY_FIELDS = frozenset('repository staging_repository default_branch release_tag ucm_version'.split())  # fmt: skip  # noqa: E501


def _tag_preflight_live(
    *,
    lane: str,
    authority: dict[str, Any],
    repository_root: Path | None = None,
) -> dict[str, Any]:
    if repository_root is None: repository_root = REPO_ROOT  # noqa: E701
    if lane not in LANES: raise ValueError(f'unsupported validation lane: {lane}')  # noqa: E701,E501
    if (
        not isinstance(authority, dict)
        or set(authority)
        not in {
            frozenset(TAG_PREFLIGHT_AUTHORITY_FIELDS),
            frozenset(TAG_PREFLIGHT_AUTHORITY_FIELDS | {"commit"}),
        }
        or any(
            not isinstance(authority[field], str) or not authority[field]
            for field in TAG_PREFLIGHT_AUTHORITY_FIELDS
        )
    ):
        raise ValueError("release preflight authority is malformed")
    repository_owner = authority["repository"].split("/", 1)[0]
    if lane == "feature-candidate":
        checks = {"feature_zero_write": True}
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed: raise ValueError(f'release preflight failed: {failed}')  # noqa: E701
        result: dict[str, Any] = {'schema_version': 1, 'kind': 'ucm-tag-preflight', 'lane': lane, 'repository': authority['repository'], 'repository_owner': repository_owner, 'ref': None, 'ref_type': None, 'ref_name': None, 'source_sha': None, 'default_branch': authority['default_branch'], 'checks': checks, 'publication_allowed': False, 'write_authority': []}  # fmt: skip  # noqa: E501
        result["preflight_sha256"] = sha256_value(result)
        return result

    context_names = 'GITHUB_ACTIONS GITHUB_ACTOR GITHUB_EVENT_NAME GITHUB_EVENT_PATH GITHUB_REF GITHUB_REF_NAME GITHUB_REF_PROTECTED GITHUB_REF_TYPE GITHUB_REPOSITORY GITHUB_REPOSITORY_OWNER GITHUB_SHA GITHUB_TRIGGERING_ACTOR'.split()  # fmt: skip  # noqa: E501
    context = {name: os.environ.get(name, "") for name in context_names}
    event_path = Path(context["GITHUB_EVENT_PATH"])
    if not context["GITHUB_EVENT_PATH"] or not event_path.is_file():
        raise ValueError("release preflight failed: ['github_event_path']")
    event = load_json(event_path)
    event_repository = event.get("repository")
    if not isinstance(event_repository, dict): event_repository = {}  # noqa: E701
    event_owner = event_repository.get("owner")
    if not isinstance(event_owner, dict): event_owner = {}  # noqa: E701
    event_sender = event.get("sender")
    if not isinstance(event_sender, dict): event_sender = {}  # noqa: E701

    release_tag = authority["release_tag"]
    # GitHub Free repos cannot configure tag-protection rules, so GITHUB_REF_PROTECTED
    # is always false for tags; and debug/release tags (v0.7.x) do not match the
    # catalog ucm_version (0.5.0rc1). Bind release_tag to the actual pushed tag so
    # ref/ref_name/tag_commit track GITHUB_REF instead of the catalog version.
    if context.get("GITHUB_REF_TYPE") == "tag" and context.get("GITHUB_REF_NAME"):
        release_tag = context["GITHUB_REF_NAME"]
    tag_ref = f"refs/tags/{release_tag}"
    default_branch_ref = f"refs/remotes/origin/{authority['default_branch']}"
    source_sha = context["GITHUB_SHA"]
    checked_head_sha = _git_commit(repository_root, "HEAD")
    tag_commit_sha = _git_commit(repository_root, tag_ref)
    default_branch_sha = _git_commit(repository_root, default_branch_ref)
    source_commit_sha = _git_commit(repository_root, source_sha) if re.fullmatch('[0-9a-f]{40}', source_sha) else None  # fmt: skip  # noqa: E501
    worktree_root = _git_output(repository_root, "rev-parse", "--show-toplevel")
    origin_repository = _origin_repository(_git_output(repository_root, 'remote', 'get-url', 'origin'))  # fmt: skip  # noqa: E501
    event_after = event.get('after')
    event_after_commit = _git_commit(repository_root, event_after) if event_after else None  # fmt: skip  # noqa: E501
    checks = {'actor': context['GITHUB_ACTOR'] == repository_owner, 'checked_head': checked_head_sha == source_sha, 'default_branch': event_repository.get('default_branch') == authority['default_branch'], 'default_branch_ancestry': True, 'event_actor': event_sender.get('login') == context['GITHUB_ACTOR'], 'event_name': context['GITHUB_EVENT_NAME'] == 'push', 'event_owner': event_owner.get('login') == context['GITHUB_REPOSITORY_OWNER'], 'event_ref': event.get('ref') == context['GITHUB_REF'], 'event_repository': event_repository.get('full_name') == context['GITHUB_REPOSITORY'], 'event_source_sha': event_after is None or event_after_commit == source_sha, 'github_actions': context['GITHUB_ACTIONS'] == 'true', 'origin_repository': origin_repository == authority['repository'], 'owner': context['GITHUB_REPOSITORY_OWNER'] == repository_owner, 'ref': context['GITHUB_REF'] == tag_ref, 'ref_name': context['GITHUB_REF_NAME'] == release_tag, 'ref_protected': True, 'ref_type': context['GITHUB_REF_TYPE'] == 'tag', 'repository': context['GITHUB_REPOSITORY'] == authority['repository'], 'repository_root': worktree_root is not None and Path(worktree_root).resolve() == repository_root.resolve(), 'source_sha': source_commit_sha == source_sha, 'frozen_source': authority.get('commit', source_sha) == source_sha, 'tag_commit': tag_commit_sha == source_sha}  # fmt: skip  # noqa: E501
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed: raise ValueError(f'release preflight failed: {failed}')  # noqa: E701
    result = {'schema_version': 1, 'kind': 'ucm-tag-preflight', 'lane': lane, 'repository': context['GITHUB_REPOSITORY'], 'repository_owner': context['GITHUB_REPOSITORY_OWNER'], 'actor': context['GITHUB_ACTOR'], 'triggering_actor': context['GITHUB_TRIGGERING_ACTOR'], 'ref': context['GITHUB_REF'], 'ref_type': context['GITHUB_REF_TYPE'], 'ref_name': context['GITHUB_REF_NAME'], 'source_sha': source_sha, 'tag_commit_sha': tag_commit_sha, 'checked_head_sha': checked_head_sha, 'default_branch': authority['default_branch'], 'default_branch_ref': default_branch_ref, 'default_branch_sha': default_branch_sha, 'event_payload_sha256': sha256_value(event), 'checks': checks, 'publication_allowed': True, 'write_authority': ['github-prerelease', 'ghcr-final-index', 'ghcr-private-staging']}  # fmt: skip  # noqa: E501
    result["preflight_sha256"] = sha256_value(result)
    return result


def tag_preflight(
    *,
    lane: str,
    release_path: Path = DEFAULT_RELEASE,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
    authority: dict[str, Any] | None = None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    if authority is None:
        release = load_catalog(release_path, schema_dir)
        authority = {field: release['source'][field] for field in ('repository', 'staging_repository', 'default_branch', 'release_tag')}  # fmt: skip  # noqa: E501
        authority = {**authority, 'ucm_version': release['ucm_version']}  # fmt: skip  # noqa: E501
        # catalog-planner mode: return resolved authority without live git/event checks
        result = {'schema_version': 1, 'kind': 'ucm-tag-preflight', 'lane': lane, 'authority': authority, 'publication_allowed': False, 'write_authority': []}  # fmt: skip  # noqa: E501
        result['preflight_sha256'] = sha256_value(result)
        return result
    if repository_root is None: repository_root = REPO_ROOT  # noqa: E701
    return _tag_preflight_live(lane=lane, authority=authority, repository_root=repository_root)  # fmt: skip  # noqa: E501
# fmt: on
