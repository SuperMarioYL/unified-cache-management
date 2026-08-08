"""Strict configuration, version authority, and core release planning."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASE = RELEASE_ROOT / "release.yaml"
DEFAULT_COMPATIBILITY = RELEASE_ROOT / "compatibility.yaml"
DEFAULT_SCHEMA_DIR = RELEASE_ROOT / "schemas"


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def load_json(path: Path) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _resolve_ref(root: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise ValueError(f"unsupported schema reference: {reference}")
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"unresolved schema reference: {reference}")
        value = value[part]
    return value


def validate_schema(
    instance: Any,
    schema: Any,
    *,
    root: dict[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Validate the dependency-free JSON Schema subset used by shipped contracts."""
    if schema is False:
        raise ValueError(f"{path}: value is forbidden by schema")
    if schema is True:
        return
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: invalid schema node")
    root_schema = root or schema
    if "$ref" in schema:
        validate_schema(
            instance,
            _resolve_ref(root_schema, schema["$ref"]),
            root=root_schema,
            path=path,
        )
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
        type_checks = {
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
            "boolean": lambda value: isinstance(value, bool),
            "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
            "null": lambda value: value is None,
        }
        if expected_type not in type_checks or not type_checks[expected_type](instance):
            raise ValueError(f"{path}: expected {expected_type}")
    if "const" in schema and instance != schema["const"]:
        raise ValueError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise ValueError(f"{path}: expected one of {schema['enum']!r}")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            raise ValueError(f"{path}: string is shorter than minLength")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise ValueError(f"{path}: value does not match pattern {schema['pattern']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValueError(f"{path}: value is below minimum {schema['minimum']}")
    if isinstance(instance, dict):
        missing = [key for key in schema.get("required", []) if key not in instance]
        if missing:
            raise ValueError(f"{path}: missing required properties {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                raise ValueError(
                    f"Additional properties are not allowed at {path}: {extras}"
                )
        for key, value in instance.items():
            if key in properties:
                validate_schema(
                    value,
                    properties[key],
                    root=root_schema,
                    path=f"{path}.{key}",
                )
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            raise ValueError(f"{path}: array is shorter than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ValueError(f"{path}: array is longer than maxItems")
        if schema.get("uniqueItems"):
            encoded = [canonical_bytes(item) for item in instance]
            if len(encoded) != len(set(encoded)):
                raise ValueError(f"{path}: array items must be unique")
        prefix_items = schema.get("prefixItems", [])
        for index, item_schema in enumerate(prefix_items):
            if index < len(instance):
                validate_schema(
                    instance[index], item_schema, root=root_schema, path=f"{path}[{index}]"
                )
        item_schema = schema.get("items")
        if item_schema is not None:
            start = len(prefix_items) if prefix_items else 0
            for index in range(start, len(instance)):
                validate_schema(
                    instance[index], item_schema, root=root_schema, path=f"{path}[{index}]"
                )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_version(path: Path | None = None) -> str:
    version_path = path or (REPO_ROOT / "version.ini")
    for line in version_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key == "VLLM_UC_VERSION" and value:
            return value
    raise ValueError(f"VLLM_UC_VERSION is missing from {version_path}")


def derive_chart_version(version: str) -> str:
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)rc([0-9]+)", version)
    if match is None:
        raise ValueError(f"unsupported UCM release version for Chart SemVer: {version}")
    return f"{match.group(1)}-rc.{match.group(2)}"


def _strict_keys(value: dict[str, Any], allowed: set[str], location: str) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        raise ValueError(
            f"Additional properties are not allowed at {location}: {extras}"
        )


def _validate_release_shape(release: dict[str, Any]) -> None:
    _strict_keys(
        release,
        {
            "kind", "schema_version", "ucm_version", "version_file",
            "python_runtime_dependencies", "chart", "wheel_profiles",
        },
        "release.yaml",
    )
    if release.get("kind") != "release-config":
        raise ValueError("release.yaml kind must be release-config")
    if release.get("schema_version") != 1:
        raise ValueError("release.yaml schema_version must be 1")
    if release.get("version_file") != "version.ini":
        raise ValueError("release.yaml version_file must be version.ini")
    if release.get("python_runtime_dependencies") != ["wrapt==1.17.2"]:
        raise ValueError("release.yaml must keep wrapt==1.17.2 as an ordinary dependency")
    chart = release.get("chart")
    if not isinstance(chart, dict):
        raise ValueError("release.yaml chart must be a mapping")
    _strict_keys(
        chart,
        {"source", "name", "version", "app_version", "publication_target", "validation_cases"},
        "release.yaml.chart",
    )
    if chart.get("source") != "charts/ucm" or chart.get("name") != "unified-cache-pd":
        raise ValueError("release.yaml must bind the product Chart at charts/ucm")
    if chart.get("publication_target") != "github-release":
        raise ValueError("GitHub Release is the only Chart publication target")
    cases = chart.get("validation_cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError("release.yaml must define three Chart validation cases")
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError("Chart validation cases must be mappings")
        _strict_keys(
            case,
            {"name", "values", "image_repository", "image_digest", "expected_resource"},
            f"release.yaml.chart.validation_cases[{index}]",
        )
    expected_cases = {
        "cuda": ("registry.invalid/ucm/fixture-cuda", "nvidia.com/gpu"),
        "a2": ("registry.invalid/ucm/fixture-ascend-a2", "huawei.com/Ascend910"),
        "a3": ("registry.invalid/ucm/fixture-ascend-a3", "huawei.com/Ascend910"),
    }
    for case in cases:
        expected_repository, expected_resource = expected_cases.get(
            case.get("name"), (None, None)
        )
        if (
            case.get("image_repository") != expected_repository
            or case.get("expected_resource") != expected_resource
        ):
            raise ValueError(
                f"Chart case {case.get('name')} does not have its exact synthetic image/resource boundary"
            )
    if len({case.get("image_digest") for case in cases}) != 3:
        raise ValueError("CUDA, A2, and A3 Chart cases require distinct image digests")
    profiles = release.get("wheel_profiles")
    if not isinstance(profiles, list) or len(profiles) != 6:
        raise ValueError("release.yaml must define exactly six wheel profiles")
    profile_keys = {
        "id", "accelerator", "accelerator_runtime", "npu_arch", "os",
        "cpu_arch", "python_version", "python_abi", "binary_profile_id",
        "validation_targets", "locks", "runner",
    }
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            raise ValueError("wheel profiles must be mappings")
        _strict_keys(profile, profile_keys, f"release.yaml.wheel_profiles[{index}]")
        locks = profile.get("locks")
        if not isinstance(locks, list) or len(locks) < 2:
            raise ValueError("each wheel profile needs at least builder and toolchain locks")
        for lock_index, lock in enumerate(locks):
            if not isinstance(lock, dict):
                raise ValueError("wheel locks must be mappings")
            _strict_keys(lock, {"subject", "selector", "status", "identity"}, f"release.yaml.wheel_profiles[{index}].locks[{lock_index}]")
            if lock.get("status") not in {"unresolved", "resolved"}:
                raise ValueError("wheel lock status must be unresolved or resolved")
        runner = profile.get("runner")
        if not isinstance(runner, dict):
            raise ValueError("wheel runner must be a mapping")
        _strict_keys(runner, {"selector", "status", "identity"}, f"release.yaml.wheel_profiles[{index}].runner")
        if runner.get("status") not in {"unresolved", "resolved"}:
            raise ValueError("runner status must be unresolved or resolved")


def _validate_compatibility_shape(compatibility: dict[str, Any]) -> None:
    _strict_keys(
        compatibility,
        {"kind", "schema_version", "ucm_version", "rules", "excluded_upstream_patterns"},
        "compatibility.yaml",
    )
    if compatibility.get("kind") != "compatibility-config":
        raise ValueError("compatibility.yaml kind must be compatibility-config")
    if compatibility.get("schema_version") != 1:
        raise ValueError("compatibility.yaml schema_version must be 1")
    rules = compatibility.get("rules")
    if not isinstance(rules, list) or len(rules) != 2:
        raise ValueError("compatibility.yaml must define exactly two accelerator rules")
    rule_keys = {
        "id", "accelerator", "accelerator_runtimes", "npu_architectures",
        "operating_systems", "cpu_architectures", "python_abis", "upstream_channels",
    }
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError("compatibility rules must be mappings")
        _strict_keys(rule, rule_keys, f"compatibility.yaml.rules[{index}]")
    if compatibility.get("excluded_upstream_patterns") != [
        "nightly", "dev", "custom", "310p", "a5", "explicit-a2-suffix"
    ]:
        raise ValueError("compatibility exclusions must retain the reviewed fail-closed set")


def validate_config(
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config_schema = load_json(schema_dir / "config.schema.json")
    # Parse every shipped schema strictly so duplicate-key corruption cannot hide.
    load_json(schema_dir / "release-manifest.schema.json")
    load_json(schema_dir / "image-result.schema.json")
    release = load_yaml(release_path)
    compatibility = load_yaml(compatibility_path)
    validate_schema(release, config_schema)
    validate_schema(compatibility, config_schema)
    _validate_release_shape(release)
    _validate_compatibility_shape(compatibility)
    version = read_version(REPO_ROOT / release["version_file"])
    if release["ucm_version"] != version:
        raise ValueError(
            f"release.yaml version {release['ucm_version']} does not match version.ini {version}"
        )
    if compatibility["ucm_version"] != version:
        raise ValueError(
            "compatibility.yaml version "
            f"{compatibility['ucm_version']} does not match version.ini {version}"
        )
    if release["chart"]["app_version"] != version:
        raise ValueError("release.yaml Chart app_version does not match version.ini")
    expected_chart_version = derive_chart_version(version)
    if release["chart"]["version"] != expected_chart_version:
        raise ValueError(
            f"release.yaml Chart version must be derived as {expected_chart_version}"
        )
    chart = load_yaml(REPO_ROOT / release["chart"]["source"] / "Chart.yaml")
    if chart.get("name") != release["chart"]["name"]:
        raise ValueError("Chart name does not match release.yaml")
    if chart.get("version") != release["chart"]["version"]:
        raise ValueError("Chart version does not match release.yaml")
    if str(chart.get("appVersion")) != version:
        raise ValueError("Chart appVersion does not match version.ini")
    case_names = [item["name"] for item in release["chart"]["validation_cases"]]
    if case_names != ["cuda", "a2", "a3"]:
        raise ValueError("Chart validation cases must be exactly cuda, a2, a3")
    _validate_profile_semantics(release["wheel_profiles"])
    _validate_compatibility_semantics(release["wheel_profiles"], compatibility["rules"])
    return release, compatibility


def _validate_profile_semantics(profiles: list[dict[str, Any]]) -> None:
    if len({profile["id"] for profile in profiles}) != len(profiles):
        raise ValueError("wheel profile IDs must be unique")
    for profile in profiles:
        abi_version = {"cp311": "3.11", "cp312": "3.12"}[profile["python_abi"]]
        if profile["python_version"] != abi_version:
            raise ValueError(f"Python version/ABI mismatch in {profile['id']}")
        if profile["accelerator"] == "cuda":
            if not profile["accelerator_runtime"].startswith("cuda-"):
                raise ValueError(f"CUDA runtime mismatch in {profile['id']}")
            if profile["npu_arch"] != ["na"] or profile["os"] != ["ubuntu-22.04"]:
                raise ValueError(f"CUDA profile scope mismatch in {profile['id']}")
        else:
            if not profile["accelerator_runtime"].startswith("cann-"):
                raise ValueError(f"Ascend runtime mismatch in {profile['id']}")
            if profile["npu_arch"] != ["a2", "a3"]:
                raise ValueError(f"Ascend accepts exactly A2 and A3 in {profile['id']}")
        required_subjects = (
            {"builder", "toolchain"}
            if profile["accelerator"] == "cuda"
            else {"builder", "toolchain", "atb", "torch-npu"}
        )
        lock_subjects = [lock["subject"] for lock in profile["locks"]]
        if set(lock_subjects) != required_subjects or len(lock_subjects) != len(required_subjects):
            raise ValueError(
                f"{profile['id']} requires exact lock subjects {sorted(required_subjects)}"
            )
        identity_patterns = {
            "builder": r"^oci://[^@ ]+@sha256:[0-9a-f]{64}$",
            "toolchain": r"^toolchain://[^@ ]+@sha256:[0-9a-f]{64}$",
            "atb": r"^package://[^@ ]+@sha256:[0-9a-f]{64}$",
            "torch-npu": r"^package://[^@ ]+@sha256:[0-9a-f]{64}$",
        }
        for lock in profile["locks"]:
            identity = lock.get("identity")
            if lock["status"] == "resolved" and (
                not isinstance(identity, str)
                or re.fullmatch(identity_patterns[lock["subject"]], identity) is None
            ):
                scheme = identity_patterns[lock["subject"]].split(":", 1)[0].lstrip("^")
                raise ValueError(
                    f"resolved {lock['subject']} lock requires immutable {scheme} identity"
                )
            if lock["status"] == "unresolved" and identity is not None:
                raise ValueError(f"unresolved {lock['subject']} lock must not claim an identity")
        runner = profile["runner"]
        runner_identity = runner.get("identity")
        if runner["status"] == "resolved" and (
            not isinstance(runner_identity, str)
            or re.fullmatch(r"^runner://[^@ ]+@sha256:[0-9a-f]{64}$", runner_identity) is None
        ):
            raise ValueError("resolved runner requires immutable runner identity")
        if runner["status"] == "unresolved" and runner_identity is not None:
            raise ValueError("unresolved runner must not claim an identity")


def _validate_compatibility_semantics(
    profiles: list[dict[str, Any]], rules: list[dict[str, Any]]
) -> None:
    rule_by_accelerator = {rule["accelerator"]: rule for rule in rules}
    if set(rule_by_accelerator) != {"cuda", "ascend"} or len(rule_by_accelerator) != len(rules):
        raise ValueError("compatibility rules must contain one CUDA and one Ascend rule")
    field_mapping = {
        "accelerator_runtimes": "accelerator_runtime",
        "npu_architectures": "npu_arch",
        "operating_systems": "os",
        "cpu_architectures": "cpu_arch",
        "python_abis": "python_abi",
    }
    for accelerator, rule in rule_by_accelerator.items():
        matching = [profile for profile in profiles if profile["accelerator"] == accelerator]
        for rule_field, profile_field in field_mapping.items():
            expected: set[str] = set()
            for profile in matching:
                value = profile[profile_field]
                expected.update(value if isinstance(value, list) else [value])
            if set(rule[rule_field]) != expected:
                raise ValueError(
                    f"compatibility/profile drift for {accelerator}.{rule_field}: "
                    f"expected {sorted(expected)}, got {sorted(rule[rule_field])}"
                )


def _runtime_slug(runtime: str) -> str:
    family, version = runtime.split("-", 1)
    return ("cu" if family == "cuda" else "cann") + version.replace(".", "")


def _format_selector(value: str, *, npu_arch: str, operating_system: str, cpu_arch: str) -> str:
    return value.format(npu_arch=npu_arch, os=operating_system.replace("-", "").lower(), cpu_arch=cpu_arch)


def expand_wheel_specs(release: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for profile in release["wheel_profiles"]:
        for npu_arch, operating_system, cpu_arch in itertools.product(
            profile["npu_arch"], profile["os"], profile["cpu_arch"]
        ):
            parts = [profile["accelerator"], _runtime_slug(profile["accelerator_runtime"])]
            if profile["accelerator"] == "ascend":
                parts.append(npu_arch)
            parts.extend(
                [
                    operating_system.replace("-", "").replace(".", "").lower(),
                    cpu_arch,
                    profile["python_abi"],
                    profile["binary_profile_id"],
                ]
            )
            spec_id = "-".join(parts)
            if spec_id in seen:
                raise ValueError(f"duplicate wheel spec: {spec_id}")
            seen.add(spec_id)
            locks = [
                {
                    **lock,
                    "selector": _format_selector(
                        lock["selector"],
                        npu_arch=npu_arch,
                        operating_system=operating_system,
                        cpu_arch=cpu_arch,
                    ),
                }
                for lock in profile["locks"]
            ]
            runner = {
                **profile["runner"],
                "selector": _format_selector(
                    profile["runner"]["selector"],
                    npu_arch=npu_arch,
                    operating_system=operating_system,
                    cpu_arch=cpu_arch,
                ),
            }
            blockers = sorted(
                [
                    f"unresolved-lock:{lock['subject']}:{lock['selector']}"
                    for lock in locks
                    if lock["status"] != "resolved" or "identity" not in lock
                ]
                + (
                    [f"unresolved-runner:{runner['selector']}"]
                    if runner["status"] != "resolved" or "identity" not in runner
                    else []
                )
            )
            targets = [
                npu_arch if target == "npu-architecture" else target
                for target in profile["validation_targets"]
            ]
            spec: dict[str, Any] = {
                "spec_id": spec_id,
                "accelerator": profile["accelerator"],
                "accelerator_runtime": profile["accelerator_runtime"],
                "npu_arch_or_na": npu_arch,
                "os": operating_system,
                "cpu_arch": cpu_arch,
                "python_version": profile["python_version"],
                "python_abi": profile["python_abi"],
                "binary_profile_id": profile["binary_profile_id"],
                "validation_targets": targets,
                "locks": locks,
                "runner": runner,
                "build_eligible": not blockers,
                "blocked_reasons": blockers,
            }
            spec["declaration_sha256"] = sha256_value(spec)
            specs.append(spec)
    specs.sort(key=lambda item: item["spec_id"])
    if len(specs) != 36:
        raise ValueError(f"initial release must declare exactly 36 wheel specs, found {len(specs)}")
    return specs


def build_release_manifest(
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    release, compatibility = validate_config(release_path, compatibility_path, schema_dir)
    specs = expand_wheel_specs(release)
    eligible = [item for item in specs if item["build_eligible"]]
    assets = [
        {
            "id": f"wheel:{item['spec_id']}",
            "type": "wheel",
            "required": True,
            "status": "candidate" if item["build_eligible"] else "blocked",
        }
        for item in specs
    ]
    assets.append(
        {
            "id": f"chart:{release['chart']['name']}:{release['chart']['version']}",
            "type": "helm-chart",
            "required": True,
            "status": "candidate",
        }
    )
    blockers = sorted(
        {reason for item in specs for reason in item["blocked_reasons"]}
    )
    manifest = {
        "schema_version": 1,
        "kind": "ucm-core-release-manifest",
        "ucm_version": release["ucm_version"],
        "config_sha256": sha256_value(release),
        "compatibility_sha256": sha256_value(compatibility),
        "declared_wheel_count": len(specs),
        "eligible_wheel_count": len(eligible),
        "wheel_specs": specs,
        "blockers": blockers,
        "publication": {"target": "github-release", "assets": assets},
        "status": "candidate" if len(eligible) == len(specs) else "blocked",
    }
    validate_schema(manifest, load_json(schema_dir / "release-manifest.schema.json"))
    return manifest
