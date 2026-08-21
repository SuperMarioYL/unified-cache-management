"""Strict, repository-owned configuration loading for the dry-run planner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "release.yaml"


class ConfigError(ValueError):
    """Raised when the immutable dry-run configuration is malformed."""


_TOP_LEVEL = {
    "kind",
    "schema_version",
    "mode",
    "repositories",
    "branches",
    "version",
    "products",
    "environments",
    "retention_days",
    "repository_policy",
}
_BASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_WHEELS = [
    ("uc-manager-cuda", "cuda"),
    ("uc-manager-cann-a2", "cann-a2"),
    ("uc-manager-cann-a3", "cann-a3"),
]
_IMAGE_FAMILIES = {"cuda", "cann-a2", "cann-a3"}
_PLATFORMS = ["linux/amd64", "linux/arm64"]
_RETENTION_DAYS = {
    "develop": 14,
    "draft": 30,
    "hotfix": None,
    "nightly": 14,
    "pr": 7,
    "rc": None,
    "stable": None,
}
_DRY_RUN_WORKFLOWS = [
    "develop-release-dry-run.yml",
    "draft-environment-dry-run.yml",
    "nightly-release-dry-run.yml",
    "pr-release-dry-run.yml",
    "release-cleanup-dry-run.yml",
    "release-control-dry-run.yml",
    "release-lifecycle-dry-run.yml",
    "repository-policy-audit-dry-run.yml",
]


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _exact_mapping(value: object, name: str, keys: set[str]) -> dict[str, Any]:
    mapping = _mapping(value, name)
    unknown = set(mapping) - keys
    missing = keys - set(mapping)
    if unknown or missing:
        raise ConfigError(
            f"{name} keys mismatch: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    return mapping


def _validate_products(products: dict[str, Any]) -> None:
    if set(products) != {"wheels", "images", "chart"}:
        raise ConfigError("products must contain wheels, images, and chart")
    wheels = products["wheels"]
    if not isinstance(wheels, list) or len(wheels) != len(_WHEELS):
        raise ConfigError(
            "products.wheels must declare exactly three unique distributions"
        )
    for index, (distribution, backend) in enumerate(_WHEELS):
        wheel = _exact_mapping(
            wheels[index],
            f"products.wheels[{index}]",
            {"distribution", "import_name", "backend"},
        )
        if (
            wheel["distribution"] != distribution
            or wheel["backend"] != backend
            or wheel["import_name"] != "ucm"
        ):
            raise ConfigError(
                "products.wheels must declare the three UCM distributions"
            )
    images = products["images"]
    if not isinstance(images, list) or len(images) != len(_IMAGE_FAMILIES):
        raise ConfigError("products.images must declare exactly three unique families")
    families: list[str] = []
    for index, item in enumerate(images):
        image = _exact_mapping(
            item,
            f"products.images[{index}]",
            {"family", "repository", "platforms"},
        )
        family = _nonempty_string(image["family"], f"products.images[{index}].family")
        repository = _nonempty_string(
            image["repository"], f"products.images[{index}].repository"
        )
        if repository.count("{repository}") != 1:
            raise ConfigError(
                f"products.images[{index}].repository must contain one {{repository}} placeholder"
            )
        if image["platforms"] != _PLATFORMS:
            raise ConfigError(
                f"products.images[{index}].platforms must be exactly {_PLATFORMS}"
            )
        families.append(family)
    if len(set(families)) != len(families) or set(families) != _IMAGE_FAMILIES:
        raise ConfigError("products.images must declare exactly three unique families")
    chart = _exact_mapping(products["chart"], "products.chart", {"name", "source"})
    _nonempty_string(chart["name"], "products.chart.name")
    _nonempty_string(chart["source"], "products.chart.source")


def _validate_repository_policy(value: object) -> None:
    policy = _exact_mapping(
        value,
        "repository_policy",
        {
            "default_branch",
            "protected_branches",
            "tag_pattern",
            "production_environment",
            "dry_run_workflows",
        },
    )
    if policy["default_branch"] != "main":
        raise ConfigError("repository_policy.default_branch must be main")
    if policy["protected_branches"] != ["develop", "main"]:
        raise ConfigError(
            "repository_policy.protected_branches must be develop and main"
        )
    if policy["tag_pattern"] != "v[0-9]*":
        raise ConfigError("repository_policy.tag_pattern must be v[0-9]*")
    if policy["dry_run_workflows"] != _DRY_RUN_WORKFLOWS:
        raise ConfigError(
            "repository_policy.dry_run_workflows must match the v2 workflow set"
        )
    environment = _exact_mapping(
        policy["production_environment"],
        "repository_policy.production_environment",
        {"name", "minimum_required_reviewers", "deployment_branch_policy"},
    )
    if environment["name"] != "release-production":
        raise ConfigError(
            "repository_policy production environment must be release-production"
        )
    reviewers = environment["minimum_required_reviewers"]
    if not isinstance(reviewers, int) or isinstance(reviewers, bool) or reviewers < 1:
        raise ConfigError(
            "repository_policy minimum_required_reviewers must be at least 1"
        )
    branch_policy = _exact_mapping(
        environment["deployment_branch_policy"],
        "repository_policy.production_environment.deployment_branch_policy",
        {"protected_branches", "custom_branch_policies"},
    )
    if branch_policy != {
        "protected_branches": True,
        "custom_branch_policies": False,
    }:
        raise ConfigError(
            "repository_policy deployment branch policy must use protected branches only"
        )


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load and validate the complete v2 configuration without side effects."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"cannot read config: {path}") from error

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ConfigError(f"config contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as error:
            raise ConfigError("non-JSON config requires optional PyYAML") from error

        class RejectDuplicateSafeLoader(yaml.SafeLoader):
            pass

        def construct_unique_mapping(
            loader: Any, node: Any, deep: bool = False
        ) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in mapping
                except TypeError as error:
                    raise ConfigError(
                        "config YAML mapping keys must be scalar"
                    ) from error
                if duplicate:
                    raise ConfigError(f"config contains duplicate key: {key}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        RejectDuplicateSafeLoader.add_constructor(
            "tag:yaml.org,2002:map", construct_unique_mapping
        )
        try:
            value = yaml.load(raw, Loader=RejectDuplicateSafeLoader)
        except yaml.YAMLError as error:
            raise ConfigError(f"invalid YAML config: {error}") from error
    config = _mapping(value, "config")
    if config.get("mode") != "dry-run":
        raise ConfigError("mode must be dry-run")
    unknown = set(config) - _TOP_LEVEL
    missing = _TOP_LEVEL - set(config)
    if unknown or missing:
        raise ConfigError(
            f"config keys mismatch: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    if (
        config["kind"] != "ucm-release-lifecycle-config"
        or config["schema_version"] != 2
    ):
        raise ConfigError("unsupported config kind or schema_version")
    repositories = _exact_mapping(
        config["repositories"], "repositories", {"production", "validation"}
    )
    if set(repositories) != {"production", "validation"}:
        raise ConfigError("repositories must contain production and validation")
    for role, coordinate in repositories.items():
        coordinate = _nonempty_string(coordinate, f"repositories.{role}")
        if coordinate.count("/") != 1 or any(
            not item for item in coordinate.split("/")
        ):
            raise ConfigError(
                f"repositories.{role} must be an owner/repository coordinate"
            )
    branches = _exact_mapping(config["branches"], "branches", {"develop", "main"})
    if branches != {"develop": "develop", "main": "main"}:
        raise ConfigError("branches must define exactly develop and main")
    version = _nonempty_string(config["version"], "version")
    if not _BASE_VERSION.fullmatch(version):
        raise ConfigError("version must be a base x.y.z version")
    products = _mapping(config["products"], "products")
    _validate_products(products)
    environments = _exact_mapping(
        config["environments"], "environments", {"blue", "yellow"}
    )
    if any(
        _exact_mapping(item, f"environments.{name}", {"evidence_level"})[
            "evidence_level"
        ]
        != "simulated"
        for name, item in environments.items()
    ):
        raise ConfigError("environments must be blue/yellow simulated environments")
    retention = _exact_mapping(
        config["retention_days"], "retention_days", set(_RETENTION_DAYS)
    )
    if retention != _RETENTION_DAYS:
        raise ConfigError(
            "retention_days must be pr=7, develop/nightly=14, draft=30, "
            "and rc/stable/hotfix protected"
        )
    _validate_repository_policy(config["repository_policy"])
    return config


def retention_days(config: dict[str, Any], retention_class: str) -> int | None:
    """Return an explicit policy window; never silently choose a default."""
    try:
        return config["retention_days"][retention_class]
    except KeyError as error:
        raise ConfigError(f"unknown retention class: {retention_class}") from error
