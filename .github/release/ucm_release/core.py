"""Strict configuration, immutable authority, and real release planning."""

from __future__ import annotations

import hashlib
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


def _construct_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, Any]:
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
            "integer": lambda value: isinstance(value, int)
            and not isinstance(value, bool),
            "boolean": lambda value: isinstance(value, bool),
            "number": lambda value: isinstance(value, (int, float))
            and not isinstance(value, bool),
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
            raise ValueError(
                f"{path}: value does not match pattern {schema['pattern']!r}"
            )
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
                    instance[index],
                    item_schema,
                    root=root_schema,
                    path=f"{path}[{index}]",
                )
        item_schema = schema.get("items")
        if item_schema is not None:
            start = len(prefix_items) if prefix_items else 0
            for index in range(start, len(instance)):
                validate_schema(
                    instance[index],
                    item_schema,
                    root=root_schema,
                    path=f"{path}[{index}]",
                )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


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


RELEASE_KEYS = {
    "kind",
    "schema_version",
    "ucm_version",
    "version_file",
    "source",
    "lanes",
    "runner_map",
    "python_runtime_dependencies",
    "python_build_lock",
    "wrapt_wheels",
    "chart",
    "wheel_profiles",
    "image_families",
}
COMPATIBILITY_KEYS = {
    "kind",
    "schema_version",
    "ucm_version",
    "rules",
    "excluded_upstream_patterns",
}
PROFILE_ORDER = ["cuda130", "cann900-a2", "cann900-a3"]
ARCHITECTURE_ORDER = ["amd64", "arm64"]
LANES = ("feature-candidate", "protected-tag")
COMMON_NATIVE = [
    "ucmtrans",
    "metrics",
    "ucmmetrics",
    "ucmlogger",
    "ucmnfsstore",
    "ucmpcstore",
    "posixstore",
    "compressor",
    "cachestore",
    "emptystore",
    "fakestore",
    "ucmpipelinestore",
]
FORBIDDEN_NATIVE = [
    "ds3fsstore",
    "uc_hash_ext",
    "ucm_custom_ops",
    "hash_retrieval_backend",
    "hamming",
    "gsa_prefetch",
    "kvstar_retrieve",
    "retrieval_backend",
    "gsa_offload_ops",
]
CANONICAL_RELEASE_SECTION_SHA256 = {
    "source": "sha256:d2d52dd28fa8307c6be94b8ed9e69db7c94d8f8a634ed7dc57032dfdb13ac5b1",
    "lanes": "sha256:8de0316a0d938870075c4865b6e9bf7beb969e3edcbbfd6a122a2862e8eeb7f1",
    "runner_map": "sha256:9ff3e8be59d3fc512967852b3c2e26e0e5474c7cf81ca500a255f10b13b84869",
    "python_runtime_dependencies": "sha256:a8667534906615d56bb80c5aef52014a6c099bf53b10e07305dd249dedc86b18",
    "python_build_lock": "sha256:367531ab722e53b1b3cd6283b7385ae0073c78f51ffedf8bb8658478fc593eb0",
    "wrapt_wheels": "sha256:2c674887f2c73e504ba0da5a83d93e2fc88391405e7ac29aa37d082e6184bfab",
    "chart": "sha256:a4d4da0020be293876a242a22910830cc2c600d308df650837c2ae6d53b67f7f",
    "wheel_profiles": "sha256:01e2129a06ebd5acbbd5107b7cb0490442db9d352d1716a8833b9437f496e8ac",
    "image_families": "sha256:e7200360dda58fd1d1caeaf0eb52bc4ca33157c521ef408fbc6a06c809c5819e",
}
CANONICAL_COMPATIBILITY_SHA256 = (
    "sha256:66ad8c060e79a80378ed27f29ced33af822c391da5e59da0f526613b796a23ee"
)


def _exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing or extras:
        raise ValueError(
            f"{location} requires exact key set; missing={missing}, extra={extras}"
        )


def _validate_canonical_authorities(
    release: dict[str, Any], compatibility: dict[str, Any]
) -> None:
    for name, expected in CANONICAL_RELEASE_SECTION_SHA256.items():
        actual = sha256_value(release[name])
        if actual != expected:
            raise ValueError(
                f"canonical release authority mismatch for {name}: "
                f"expected {expected}, got {actual}"
            )
    actual_compatibility = sha256_value(compatibility)
    if actual_compatibility != CANONICAL_COMPATIBILITY_SHA256:
        raise ValueError(
            "canonical compatibility authority mismatch: "
            f"expected {CANONICAL_COMPATIBILITY_SHA256}, got {actual_compatibility}"
        )


def _validate_cross_config(
    release: dict[str, Any], compatibility: dict[str, Any]
) -> None:
    profiles = release["wheel_profiles"]
    families = release["image_families"]
    if [item["id"] for item in profiles] != PROFILE_ORDER:
        raise ValueError(f"exact production profile set/order is {PROFILE_ORDER}")
    if [item["id"] for item in families] != PROFILE_ORDER:
        raise ValueError(f"exact image family set/order is {PROFILE_ORDER}")
    if any(item["profile_id"] != item["id"] for item in families):
        raise ValueError("each image family must bind the same-named wheel profile")
    if any(item["cpu_arch"] != ARCHITECTURE_ORDER for item in profiles):
        raise ValueError("every production profile requires amd64 then arm64")

    coordinates = [
        f"{family['target_repository']}:{family['target_tag']}" for family in families
    ]
    if len(coordinates) != len(set(coordinates)):
        raise ValueError("public image coordinates must be unique")
    if len({item["target_repository"] for item in families}) != 2:
        raise ValueError(
            "three image families must use exactly two target repositories"
        )

    family_by_id = {item["id"]: item for item in families}
    for profile in profiles:
        family = family_by_id[profile["id"]]
        expected_required = COMMON_NATIVE + (
            [] if profile["accelerator"] == "cuda" else ["mooncakestore"]
        )
        expected_forbidden = (
            ["mooncakestore"] if profile["accelerator"] == "cuda" else []
        ) + FORBIDDEN_NATIVE
        if profile["required_native"] != expected_required:
            raise ValueError(f"{profile['id']} required native allowlist drifted")
        if profile["forbidden_native"] != expected_forbidden:
            raise ValueError(f"{profile['id']} forbidden native allowlist drifted")
        for architecture in ARCHITECTURE_ORDER:
            builder = profile["builders"][architecture]
            root = builder["root"]
            root_coordinate = f"{root['repository']}@{root['manifest_digest']}"
            if re.fullmatch(r"[^@ ]+@sha256:[0-9a-f]{64}", root_coordinate) is None:
                raise ValueError("builder roots must resolve to repository@sha256")
            if profile["accelerator"] == "cuda":
                if builder["sources"] or builder["copy_paths"]:
                    raise ValueError("CUDA builder must be a complete pinned root")
            else:
                if len(builder["sources"]) != 1:
                    raise ValueError(
                        "Ascend builder requires one immutable Mooncake donor"
                    )
                donor = builder["sources"][0]
                member = family["runtime"]["members"][architecture]
                expected_donor = {
                    "repository": family["runtime"]["repository"],
                    "tag": family["runtime"]["tag"],
                    "index_digest": family["runtime"]["index_digest"],
                    "manifest_digest": member["manifest_digest"],
                    "config_digest": member["config_digest"],
                }
                if donor != expected_donor:
                    raise ValueError(
                        f"{profile['id']}/{architecture} Mooncake donor/runtime drift"
                    )

    rules = compatibility["rules"]
    rule_by_accelerator = {rule["accelerator"]: rule for rule in rules}
    if set(rule_by_accelerator) != {"cuda", "ascend"} or len(rules) != 2:
        raise ValueError("compatibility requires exactly CUDA and Ascend rules")
    field_mapping = {
        "accelerator_runtimes": "accelerator_runtime",
        "npu_architectures": "npu_arch",
        "operating_systems": "os",
        "cpu_architectures": "cpu_arch",
        "python_abis": "python_abi",
    }
    for accelerator, rule in rule_by_accelerator.items():
        matching = [item for item in profiles if item["accelerator"] == accelerator]
        for rule_field, profile_field in field_mapping.items():
            expected: list[str] = []
            for profile in matching:
                value = profile[profile_field]
                candidates = value if isinstance(value, list) else [value]
                expected.extend(item for item in candidates if item not in expected)
            if rule[rule_field] != expected:
                raise ValueError(
                    f"compatibility/profile drift for {accelerator}.{rule_field}: "
                    f"expected {expected}, got {rule[rule_field]}"
                )

    cases = release["chart"]["validation_cases"]
    if [item["name"] for item in cases] != ["cuda", "a2", "a3"]:
        raise ValueError("Chart validation cases must be exactly cuda, a2, a3")
    for case, family_id in zip(cases, PROFILE_ORDER, strict=True):
        runtime = family_by_id[family_id]["runtime"]
        if (
            case["image_repository"] != runtime["repository"]
            or case["image_digest"] != runtime["index_digest"]
        ):
            raise ValueError(
                "Chart validation must use the exact final repository@sha256 runtime"
            )


def validate_config(
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config_schema = load_json(schema_dir / "config.schema.json")
    load_json(schema_dir / "release-manifest.schema.json")
    load_json(schema_dir / "image-result.schema.json")
    release = load_yaml(release_path)
    compatibility = load_yaml(compatibility_path)
    validate_schema(release, config_schema)
    validate_schema(compatibility, config_schema)
    _exact_keys(release, RELEASE_KEYS, "release.yaml")
    _exact_keys(compatibility, COMPATIBILITY_KEYS, "compatibility.yaml")

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
    if release["source"]["release_tag"] != f"v{version}":
        raise ValueError("release tag must be derived from version.ini")
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
    _validate_cross_config(release, compatibility)
    _validate_canonical_authorities(release, compatibility)
    return release, compatibility


def _resolved_locks(
    release: dict[str, Any], profile: dict[str, Any], architecture: str
) -> list[dict[str, Any]]:
    builder = profile["builders"][architecture]["root"]
    dependency = {
        "python_build_lock": release["python_build_lock"],
        "wrapt_wheel": release["wrapt_wheels"][architecture],
    }
    return [
        {
            "subject": "builder",
            "selector": f"builder://{profile['id']}/{architecture}",
            "status": "resolved",
            "identity": f"oci://{builder['repository']}@{builder['manifest_digest']}",
        },
        {
            "subject": "python-build",
            "selector": f"package-lock://{profile['id']}/{architecture}",
            "status": "resolved",
            "identity": f"package://pypi/ucm-build@{sha256_value(dependency)}",
        },
    ]


def expand_wheel_specs(release: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for profile in release["wheel_profiles"]:
        npu_arch = profile["npu_arch"][0]
        operating_system = profile["os"][0]
        for architecture in profile["cpu_arch"]:
            spec: dict[str, Any] = {
                "spec_id": f"{profile['id']}-{architecture}",
                "accelerator": profile["accelerator"],
                "accelerator_runtime": profile["accelerator_runtime"],
                "npu_arch_or_na": npu_arch,
                "os": operating_system,
                "cpu_arch": architecture,
                "python_version": profile["python_version"],
                "python_abi": profile["python_abi"],
                "binary_profile_id": profile["binary_profile_id"],
                "validation_targets": profile["validation_targets"],
                "locks": _resolved_locks(release, profile, architecture),
                "runner": {
                    "selector": f"runner-map://{architecture}",
                    "status": "resolved",
                    "identity": (
                        f"runner://github-hosted/{release['runner_map'][architecture]}@"
                        f"{sha256_value({'architecture': architecture, 'label': release['runner_map'][architecture]})}"
                    ),
                },
                "build_eligible": True,
                "blocked_reasons": [],
            }
            spec["declaration_sha256"] = sha256_value(spec)
            specs.append(spec)
    expected_ids = [
        f"{profile}-{architecture}"
        for profile in PROFILE_ORDER
        for architecture in ARCHITECTURE_ORDER
    ]
    if [item["spec_id"] for item in specs] != expected_ids:
        raise ValueError("wheel specification order or membership is noncanonical")
    return specs


def build_matrix(
    lane: str,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    if lane not in LANES:
        raise ValueError(f"unsupported validation lane: {lane}")
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    profiles = {item["id"]: item for item in release["wheel_profiles"]}
    families = {item["profile_id"]: item for item in release["image_families"]}
    write_authority = (
        []
        if lane == "feature-candidate"
        else ["github-prerelease", "ghcr-final-index", "ghcr-private-staging"]
    )
    tasks: list[dict[str, Any]] = []
    for spec in expand_wheel_specs(release):
        architecture = spec["cpu_arch"]
        profile_id = spec["spec_id"].removesuffix(f"-{architecture}")
        profile = profiles[profile_id]
        family = families[profile_id]
        runtime_member = family["runtime"]["members"][architecture]
        dependency_lock = {
            "python_build_lock": release["python_build_lock"],
            "wrapt_wheel": release["wrapt_wheels"][architecture],
        }
        task: dict[str, Any] = {
            "spec_id": spec["spec_id"],
            "profile_id": profile_id,
            "cpu_arch": architecture,
            "platform": f"linux/{architecture}",
            "runner": release["runner_map"][architecture],
            "python_abi": profile["python_abi"],
            "wheel_version": profile["wheel_version"],
            "builder": profile["builders"][architecture],
            "runtime": {
                "repository": family["runtime"]["repository"],
                "tag": family["runtime"]["tag"],
                "index_digest": family["runtime"]["index_digest"],
                **runtime_member,
            },
            "target_repository": family["target_repository"],
            "target_tag": family["target_tag"],
            "required_native": profile["required_native"],
            "forbidden_native": profile["forbidden_native"],
            "dependency_lock_sha256": sha256_value(dependency_lock),
            "write_authority": write_authority,
            "build_eligible": True,
        }
        task["task_sha256"] = sha256_value(task)
        tasks.append(task)
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ucm-real-wheel-matrix",
        "lane": lane,
        "source_repository": release["source"]["repository"],
        "release_tag": release["source"]["release_tag"],
        "tasks": tasks,
    }
    result["matrix_sha256"] = sha256_value(result)
    return result


def tag_preflight(
    *,
    lane: str,
    repository: str,
    repository_owner: str,
    ref_name: str,
    source_sha: str,
    default_branch: str,
    ref_protected: bool,
    policy: str | None = None,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    if lane not in LANES:
        raise ValueError(f"unsupported validation lane: {lane}")
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    authority = release["source"]
    checks = {
        "repository": repository == authority["repository"],
        "owner": repository_owner == authority["owner"],
        "source_sha": re.fullmatch(r"[0-9a-f]{40}", source_sha) is not None,
        "default_branch": default_branch == authority["default_branch"],
        "version_file": read_version(REPO_ROOT / release["version_file"])
        == release["ucm_version"],
    }
    if lane == "protected-tag":
        checks.update(
            {
                "tag": ref_name == authority["release_tag"],
                "ref_protected": ref_protected is True,
                "release_policy": policy == authority["release_policy"],
            }
        )
    elif not ref_name or ref_name == authority["release_tag"]:
        checks["feature_ref"] = False
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ValueError(f"release preflight failed: {failed}")
    publication_allowed = lane == "protected-tag"
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ucm-tag-preflight",
        "lane": lane,
        "repository": repository,
        "repository_owner": repository_owner,
        "ref_name": ref_name,
        "source_sha": source_sha,
        "default_branch": default_branch,
        "checks": checks,
        "publication_allowed": publication_allowed,
        "write_authority": (
            ["github-prerelease", "ghcr-final-index", "ghcr-private-staging"]
            if publication_allowed
            else []
        ),
    }
    result["preflight_sha256"] = sha256_value(result)
    return result


def build_release_manifest(
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    release, compatibility = validate_config(
        release_path, compatibility_path, schema_dir
    )
    specs = expand_wheel_specs(release)
    assets = [
        {
            "id": f"wheel:{item['spec_id']}",
            "type": "wheel",
            "required": True,
            "status": "candidate",
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
    manifest = {
        "schema_version": 1,
        "kind": "ucm-core-release-manifest",
        "ucm_version": release["ucm_version"],
        "config_sha256": sha256_value(release),
        "compatibility_sha256": sha256_value(compatibility),
        "declared_wheel_count": len(specs),
        "eligible_wheel_count": len(specs),
        "wheel_specs": specs,
        "blockers": [],
        "publication": {"target": "github-release", "assets": assets},
        "status": "candidate",
    }
    validate_schema(manifest, load_json(schema_dir / "release-manifest.schema.json"))
    return manifest
