"""Trusted production release configuration and repository projection."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .common import (
    ProductionError,
    canonical_bytes,
    load_json,
    require_exact_keys,
    require_lower_sha256,
    require_posix_path,
    require_string,
)

_CONFIG_KEYS = {
    "kind",
    "schema_version",
    "release_line",
    "base_version",
    "release_branch",
    "environment",
    "products",
    "build_profiles",
    "channels",
    "retention_days",
    "external_channels",
    "toolchain",
}
_PROFILE_IDS = ["cuda130", "cann900-a2", "cann900-a3"]
_DISTRIBUTIONS = ["uc-manager-cuda", "uc-manager-cann-a2", "uc-manager-cann-a3"]
_IMAGE_BASENAMES = ["ucm-cuda", "ucm-cann-a2", "ucm-cann-a3"]
_REPOSITORY = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<name>[A-Za-z0-9._-]{1,100})",
    re.ASCII,
)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", re.ASCII)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProductionError(f"{label} must be an array")
    return value


def _validate_digest_tree(value: object, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("_digest"):
                if not isinstance(child, str) or not child.startswith("sha256:"):
                    raise ProductionError(f"{label}.{key} must use sha256:<digest>")
                require_lower_sha256(child.removeprefix("sha256:"), f"{label}.{key}")
            else:
                _validate_digest_tree(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_digest_tree(child, f"{label}[{index}]")


def _validate_products(config: dict[str, Any]) -> None:
    products = _object(config["products"], "products")
    require_exact_keys(products, {"wheels", "images", "chart"}, "products")
    wheels = _array(products["wheels"], "products.wheels")
    images = _array(products["images"], "products.images")
    if len(wheels) != 3 or len(images) != 3:
        raise ProductionError("products must contain exactly three wheels and images")
    expected_wheels = list(zip(_PROFILE_IDS, _DISTRIBUTIONS, strict=True))
    for index, (wheel, expected) in enumerate(
        zip(wheels, expected_wheels, strict=True)
    ):
        item = _object(wheel, f"products.wheels[{index}]")
        require_exact_keys(
            item,
            {"profile_id", "distribution", "import_name"},
            f"products.wheels[{index}]",
        )
        if (item["profile_id"], item["distribution"]) != expected:
            raise ProductionError("wheel product order or profile mapping is invalid")
        if item["import_name"] != "ucm":
            raise ProductionError("every production wheel must provide import ucm")
    for index, (image, profile_id, basename) in enumerate(
        zip(images, _PROFILE_IDS, _IMAGE_BASENAMES, strict=True)
    ):
        item = _object(image, f"products.images[{index}]")
        require_exact_keys(
            item,
            {"profile_id", "basename", "draft_basename"},
            f"products.images[{index}]",
        )
        if item != {
            "profile_id": profile_id,
            "basename": basename,
            "draft_basename": f"{basename}-private",
        }:
            raise ProductionError("image product order or naming is invalid")
    chart = _object(products["chart"], "products.chart")
    require_exact_keys(
        chart, {"name", "source", "repository_basename"}, "products.chart"
    )
    if chart["name"] != "unified-cache-pd":
        raise ProductionError("products.chart.name must be unified-cache-pd")
    require_posix_path(chart["source"], "products.chart.source")
    if chart["repository_basename"] != "charts/unified-cache-pd":
        raise ProductionError(
            "products.chart.repository_basename must be charts/unified-cache-pd"
        )


def _validate_profiles(config: dict[str, Any]) -> None:
    profiles = _array(config["build_profiles"], "build_profiles")
    if [item.get("id") for item in profiles if isinstance(item, dict)] != _PROFILE_IDS:
        raise ProductionError("build profile order must be CUDA, CANN A2, CANN A3")
    for index, profile in enumerate(profiles):
        item = _object(profile, f"build_profiles[{index}]")
        require_exact_keys(
            item,
            {
                "id",
                "distribution",
                "build_platform",
                "cpu_arch",
                "python_version",
                "python_abi",
                "wheel_platform",
                "builders",
                "runtime",
            },
            f"build_profiles[{index}]",
        )
        if item["distribution"] != _DISTRIBUTIONS[index]:
            raise ProductionError("build profile distribution mapping is invalid")
        if item["build_platform"] not in {"cuda", "ascend", "ascend-a3"}:
            raise ProductionError("build profile build_platform is invalid")
        if item["cpu_arch"] != ["amd64", "arm64"]:
            raise ProductionError("every build profile must target amd64 and arm64")
        if item["python_version"] != "3.12" or item["python_abi"] != "cp312":
            raise ProductionError("production Python ABI must be CPython 3.12")
        if item["wheel_platform"] not in {"manylinux_2_28", "linux"}:
            raise ProductionError("build profile wheel_platform is invalid")
        for lock_name in ("builders", "runtime"):
            locks = _object(item[lock_name], f"build_profiles[{index}].{lock_name}")
            require_exact_keys(locks, {"amd64", "arm64"}, lock_name)
            for arch in ("amd64", "arm64"):
                lock = _object(locks[arch], f"{lock_name}.{arch}")
                require_exact_keys(
                    lock,
                    {
                        "repository",
                        "tag",
                        "index_digest",
                        "manifest_digest",
                        "config_digest",
                    },
                    f"{lock_name}.{arch}",
                )
                require_string(lock["repository"], f"{lock_name}.{arch}.repository")
                require_string(lock["tag"], f"{lock_name}.{arch}.tag")
                _validate_digest_tree(lock, f"{lock_name}.{arch}")


def _validate_channels(config: dict[str, Any]) -> None:
    channels = _object(config["channels"], "channels")
    require_exact_keys(channels, {"draft", "rc", "stable", "hotfix"}, "channels")
    expected_visibility = {
        "draft": "private",
        "rc": "public",
        "stable": "public",
        "hotfix": "public",
    }
    for name, expected in expected_visibility.items():
        channel = _object(channels[name], f"channels.{name}")
        require_exact_keys(
            channel,
            {
                "github_release",
                "image_visibility",
                "publish_chart",
                "environment_test",
            },
            f"channels.{name}",
        )
        if channel["image_visibility"] != expected:
            raise ProductionError(f"channels.{name}.image_visibility is invalid")
        if channel["github_release"] not in {"draft", "prerelease", "release"}:
            raise ProductionError(f"channels.{name}.github_release is invalid")
        if type(channel["publish_chart"]) is not bool:
            raise ProductionError(f"channels.{name}.publish_chart must be boolean")
        modes = _array(channel["environment_test"], f"channels.{name}.environment_test")
        allowed = (
            ["passed", "waived-for-preview"] if name in {"draft", "rc"} else ["passed"]
        )
        if modes != allowed:
            raise ProductionError(f"channels.{name}.environment_test is invalid")


def _validate_external_channels(config: dict[str, Any]) -> None:
    external = _object(config["external_channels"], "external_channels")
    require_exact_keys(external, {"docker_hub", "pypi"}, "external_channels")
    pypi = external["pypi"]
    if pypi is not False:
        item = _object(pypi, "external_channels.pypi")
        require_exact_keys(
            item,
            {"repository", "trusted_publisher"},
            "external_channels.pypi",
        )
        if item != {
            "repository": "https://upload.pypi.org/legacy/",
            "trusted_publisher": "github-oidc",
        }:
            raise ProductionError("PyPI external channel must use GitHub OIDC")
    docker = external["docker_hub"]
    if docker is not False:
        item = _object(docker, "external_channels.docker_hub")
        require_exact_keys(
            item,
            {"namespace", "repositories"},
            "external_channels.docker_hub",
        )
        namespace = require_string(item["namespace"], "Docker Hub namespace")
        if re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", namespace, re.ASCII) is None:
            raise ProductionError("Docker Hub namespace is invalid")
        repositories = _object(
            item["repositories"], "external_channels.docker_hub.repositories"
        )
        require_exact_keys(repositories, set(_PROFILE_IDS), "Docker Hub repositories")
        expected = {
            "cuda130": "ucm-cuda",
            "cann900-a2": "ucm-cann-a2",
            "cann900-a3": "ucm-cann-a3",
        }
        if repositories != expected:
            raise ProductionError("Docker Hub repository mapping is invalid")


def validate_config(value: object) -> dict[str, Any]:
    config = _object(value, "production release config")
    require_exact_keys(config, _CONFIG_KEYS, "production release config")
    if config["kind"] != "ucm-production-release-config":
        raise ProductionError("config kind must be ucm-production-release-config")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ProductionError("config schema_version must be 1")
    if config["release_line"] != "0.6" or config["base_version"] != "0.6.0":
        raise ProductionError("trusted configuration must describe release line 0.6")
    if config["release_branch"] != "0.6.0-release":
        raise ProductionError("release_branch must be 0.6.0-release")
    if config["environment"] != "release-production":
        raise ProductionError("environment must be release-production")
    _validate_products(config)
    _validate_profiles(config)
    _validate_channels(config)
    retention = _object(config["retention_days"], "retention_days")
    require_exact_keys(retention, {"candidate", "evidence"}, "retention_days")
    if any(type(value) is not int or value < 1 for value in retention.values()):
        raise ProductionError("retention_days values must be positive integers")
    _validate_external_channels(config)
    toolchain = _object(config["toolchain"], "toolchain")
    require_exact_keys(
        toolchain,
        {
            "legacy_release_config_sha256",
            "runtime_requirements",
            "python_build",
            "pyyaml",
            "cmake",
            "wrapt",
        },
        "toolchain",
    )
    require_lower_sha256(
        toolchain["legacy_release_config_sha256"],
        "toolchain.legacy_release_config_sha256",
    )
    runtime_requirements = _array(
        toolchain["runtime_requirements"], "toolchain.runtime_requirements"
    )
    if (
        not runtime_requirements
        or runtime_requirements != sorted(runtime_requirements)
        or len(runtime_requirements) != len(set(runtime_requirements))
    ):
        raise ProductionError(
            "toolchain.runtime_requirements must be unique and sorted"
        )
    python_build = _object(toolchain["python_build"], "toolchain.python_build")
    for requirement in runtime_requirements:
        if not isinstance(requirement, str):
            raise ProductionError("toolchain runtime requirement is invalid")
        name, separator, version = requirement.partition("==")
        record = python_build.get(name, toolchain.get(name))
        if (
            separator != "=="
            or not name
            or not version
            or not isinstance(record, dict)
            or record.get("version") != version
        ):
            raise ProductionError(
                f"toolchain runtime requirement is not pinned by toolchain: {requirement!r}"
            )
    _validate_digest_tree(toolchain, "toolchain")
    lowered = canonical_bytes(config).lower()
    if b"supermarioyl" in lowered or b"modelengine-group" in lowered:
        raise ProductionError("production config must not contain a repository owner")
    return config


def load_config(path: Path) -> dict[str, Any]:
    return validate_config(load_json(path, "production release config"))


def _validate_branch(value: object) -> str:
    branch = require_string(value, "default_branch")
    if (
        _BRANCH.fullmatch(branch) is None
        or branch.startswith("refs/")
        or branch.endswith(".")
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise ProductionError("default_branch is not a canonical branch name")
    return branch


def derive_repository(
    config: dict[str, Any],
    *,
    repository: str,
    repository_id: int,
    default_branch: str,
) -> dict[str, Any]:
    """Derive all same-repository coordinates from verified GitHub identity."""

    validated = validate_config(config)
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise ProductionError("repository must be one canonical owner/name pair")
    if type(repository_id) is not int or repository_id < 1:
        raise ProductionError("repository_id must be a positive integer")
    branch = _validate_branch(default_branch)
    owner, name = repository.split("/", 1)
    namespace = owner.lower()
    images = [
        f"ghcr.io/{namespace}/{item['basename']}"
        for item in validated["products"]["images"]
    ]
    drafts = [
        f"ghcr.io/{namespace}/{item['draft_basename']}"
        for item in validated["products"]["images"]
    ]
    return {
        "repository": repository,
        "repository_id": repository_id,
        "repository_name": name,
        "default_branch": branch,
        "ghcr_namespace": namespace,
        "release_branch": validated["release_branch"],
        "image_repositories": images,
        "draft_image_repositories": drafts,
        "chart_repository": (
            f"oci://ghcr.io/{namespace}/"
            f"{validated['products']['chart']['repository_basename']}"
        ),
        "config_sha256": hashlib.sha256(canonical_bytes(validated)).hexdigest(),
    }
