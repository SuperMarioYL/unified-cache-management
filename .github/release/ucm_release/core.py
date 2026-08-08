"""Strict configuration, version authority, and core release planning."""

from __future__ import annotations

import hashlib
import itertools
import json
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
            "schema_version", "ucm_version", "version_file",
            "python_runtime_dependencies", "chart", "wheel_profiles",
        },
        "release.yaml",
    )
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
        _strict_keys(case, {"name", "values"}, f"release.yaml.chart.validation_cases[{index}]")
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
            _strict_keys(lock, {"subject", "selector", "status"}, f"release.yaml.wheel_profiles[{index}].locks[{lock_index}]")
            if lock.get("status") not in {"unresolved", "resolved"}:
                raise ValueError("wheel lock status must be unresolved or resolved")
        runner = profile.get("runner")
        if not isinstance(runner, dict):
            raise ValueError("wheel runner must be a mapping")
        _strict_keys(runner, {"selector", "status"}, f"release.yaml.wheel_profiles[{index}].runner")
        if runner.get("status") not in {"unresolved", "resolved"}:
            raise ValueError("runner status must be unresolved or resolved")


def _validate_compatibility_shape(compatibility: dict[str, Any]) -> None:
    _strict_keys(
        compatibility,
        {"schema_version", "ucm_version", "rules", "excluded_upstream_patterns"},
        "compatibility.yaml",
    )
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
    load_json(schema_dir / "config.schema.json")
    # Parse every shipped schema strictly so duplicate-key corruption cannot hide.
    load_json(schema_dir / "release-manifest.schema.json")
    load_json(schema_dir / "image-result.schema.json")
    release = load_yaml(release_path)
    compatibility = load_yaml(compatibility_path)
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
        lock_subjects = {lock["subject"] for lock in profile["locks"]}
        if not {"builder", "toolchain"}.issubset(lock_subjects):
            raise ValueError(f"builder/toolchain locks are required in {profile['id']}")


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
                    if lock["status"] != "resolved"
                ]
                + (
                    [f"unresolved-runner:{runner['selector']}"]
                    if runner["status"] != "resolved"
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
    load_json(schema_dir / "release-manifest.schema.json")
    return manifest
