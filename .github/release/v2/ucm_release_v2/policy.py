"""Offline repository-policy snapshot validation and deterministic gap reporting."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .common import canonical_json, sha256_envelope


class PolicyError(ValueError):
    """Raised when a repository policy snapshot is structurally ambiguous."""


_TOP_KEYS = {
    "kind",
    "schema_version",
    "mode",
    "repository",
    "default_branch",
    "branches",
    "rulesets",
    "environments",
    "workflows",
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{name} must be a mapping")
    return value


def _exact(value: object, name: str, keys: set[str]) -> dict[str, Any]:
    mapping = _mapping(value, name)
    if set(mapping) != keys:
        raise PolicyError(
            f"{name} keys mismatch: missing={sorted(keys - set(mapping))} "
            f"unknown={sorted(set(mapping) - keys)}"
        )
    return mapping


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise PolicyError(f"{name} must be a list")
    return value


def _string(value: object, name: str, *, coordinate: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not _NAME.fullmatch(value)
    ):
        raise PolicyError(f"{name} must be a non-empty safe string")
    if coordinate and (
        value.count("/") != 1 or any(not part for part in value.split("/"))
    ):
        raise PolicyError(f"{name} must be an owner/repository coordinate")
    return value


def _pattern(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PolicyError(f"{name} must be a non-empty safe string")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{name} must be a boolean")
    return value


def _unique(items: list[dict[str, Any]], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        name = item["name"]
        if name in seen:
            raise PolicyError(f"duplicate {label}: {name}")
        seen.add(name)


def _check(check_id: str, passed: bool, evidence: str) -> dict[str, str]:
    return {
        "id": check_id,
        "status": "passed" if passed else "gap",
        "evidence": evidence,
    }


def audit_repository_policy(
    config: dict[str, Any], snapshot_value: object, repository_role: str
) -> dict[str, Any]:
    """Return evidence-rich gaps without querying or changing repository settings."""
    if repository_role not in {"validation", "production"}:
        raise PolicyError("repository role must be validation or production")
    try:
        expected_repository = config["repositories"][repository_role]
        requirements = config["repository_policy"]
    except (KeyError, TypeError) as error:
        raise PolicyError("repository policy config is incomplete") from error

    snapshot = _exact(snapshot_value, "repository policy snapshot", _TOP_KEYS)
    if (
        snapshot["kind"] != "repository-policy-snapshot"
        or snapshot["schema_version"] != 2
        or snapshot["mode"] != "read-only"
    ):
        raise PolicyError(
            "repository policy snapshot kind, schema_version, or mode is unsupported"
        )
    observed_repository = _string(snapshot["repository"], "repository", coordinate=True)
    default_branch = _string(snapshot["default_branch"], "default_branch")

    branches: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(snapshot["branches"], "branches")):
        item = _exact(raw, f"branches[{index}]", {"name", "protected"})
        branches.append(
            {
                "name": _string(item["name"], f"branches[{index}].name"),
                "protected": _boolean(
                    item["protected"], f"branches[{index}].protected"
                ),
            }
        )
    _unique(branches, "branch")
    branches_by_name = {item["name"]: item for item in branches}

    rulesets: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(snapshot["rulesets"], "rulesets")):
        item = _exact(
            raw,
            f"rulesets[{index}]",
            {"name", "target", "enforcement", "tag_pattern"},
        )
        target = item["target"]
        enforcement = item["enforcement"]
        if target not in {"branch", "tag"}:
            raise PolicyError(f"rulesets[{index}].target is unsupported")
        if enforcement not in {"active", "evaluate", "disabled"}:
            raise PolicyError(f"rulesets[{index}].enforcement is unsupported")
        rulesets.append(
            {
                "name": _string(item["name"], f"rulesets[{index}].name"),
                "target": target,
                "enforcement": enforcement,
                "tag_pattern": _pattern(
                    item["tag_pattern"], f"rulesets[{index}].tag_pattern"
                ),
            }
        )
    _unique(rulesets, "ruleset")

    environments: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(snapshot["environments"], "environments")):
        item = _exact(
            raw,
            f"environments[{index}]",
            {"name", "required_reviewers", "deployment_branch_policy"},
        )
        reviewers = item["required_reviewers"]
        if (
            not isinstance(reviewers, int)
            or isinstance(reviewers, bool)
            or reviewers < 0
        ):
            raise PolicyError(
                f"environments[{index}].required_reviewers must be a nonnegative integer"
            )
        branch_policy = _exact(
            item["deployment_branch_policy"],
            f"environments[{index}].deployment_branch_policy",
            {"protected_branches", "custom_branch_policies"},
        )
        environments.append(
            {
                "name": _string(item["name"], f"environments[{index}].name"),
                "required_reviewers": reviewers,
                "deployment_branch_policy": {
                    "protected_branches": _boolean(
                        branch_policy["protected_branches"],
                        f"environments[{index}].deployment_branch_policy.protected_branches",
                    ),
                    "custom_branch_policies": _boolean(
                        branch_policy["custom_branch_policies"],
                        f"environments[{index}].deployment_branch_policy.custom_branch_policies",
                    ),
                },
            }
        )
    _unique(environments, "environment")
    environments_by_name = {item["name"]: item for item in environments}

    workflows: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(snapshot["workflows"], "workflows")):
        item = _exact(raw, f"workflows[{index}]", {"name", "permissions"})
        permissions = _mapping(item["permissions"], f"workflows[{index}].permissions")
        if not permissions:
            raise PolicyError(f"workflows[{index}].permissions must not be empty")
        normalized_permissions: dict[str, str] = {}
        for key, value in permissions.items():
            permission = _string(key, f"workflows[{index}].permissions key")
            if value not in {"read", "write", "none"}:
                raise PolicyError(
                    f"workflows[{index}].permissions.{permission} is unsupported"
                )
            normalized_permissions[permission] = value
        workflows.append(
            {
                "name": _string(item["name"], f"workflows[{index}].name"),
                "permissions": normalized_permissions,
            }
        )
    _unique(workflows, "workflow")

    checks: list[dict[str, str]] = []
    checks.append(
        _check(
            "repository.coordinate",
            observed_repository == expected_repository,
            f"expected={expected_repository}; observed={observed_repository}",
        )
    )
    checks.append(
        _check(
            "default-branch.main",
            default_branch == requirements["default_branch"],
            f"expected={requirements['default_branch']}; observed={default_branch}",
        )
    )
    for branch_name in requirements["protected_branches"]:
        branch = branches_by_name.get(branch_name)
        checks.append(
            _check(
                f"branch.{branch_name}.exists",
                branch is not None,
                f"branch {branch_name} {'present' if branch is not None else 'missing'}",
            )
        )
        checks.append(
            _check(
                f"branch.{branch_name}.protected",
                branch is not None and branch["protected"] is True,
                f"protected={branch['protected'] if branch is not None else 'missing'}",
            )
        )
    tag_pattern = requirements["tag_pattern"]
    tag_rule = any(
        item["target"] == "tag"
        and item["enforcement"] == "active"
        and item["tag_pattern"] == tag_pattern
        for item in rulesets
    )
    observed_patterns = sorted(
        item["tag_pattern"]
        for item in rulesets
        if item["target"] == "tag" and item["enforcement"] == "active"
    )
    checks.append(
        _check(
            "ruleset.release-tags.active-pattern",
            tag_rule,
            f"expected={tag_pattern}; active={observed_patterns}",
        )
    )

    environment_requirement = requirements["production_environment"]
    environment_name = environment_requirement["name"]
    environment = environments_by_name.get(environment_name)
    checks.append(
        _check(
            "environment.release-production.exists",
            environment is not None,
            f"environment {environment_name} {'present' if environment is not None else 'missing'}",
        )
    )
    minimum_reviewers = environment_requirement["minimum_required_reviewers"]
    observed_reviewers = (
        environment["required_reviewers"] if environment is not None else "missing"
    )
    checks.append(
        _check(
            "environment.release-production.reviewers",
            environment is not None
            and environment["required_reviewers"] >= minimum_reviewers,
            f"minimum={minimum_reviewers}; observed={observed_reviewers}",
        )
    )
    expected_branch_policy = environment_requirement["deployment_branch_policy"]
    observed_branch_policy = (
        environment["deployment_branch_policy"]
        if environment is not None
        else "missing"
    )
    checks.append(
        _check(
            "environment.release-production.branch-policy",
            observed_branch_policy == expected_branch_policy,
            f"expected={canonical_json(expected_branch_policy)}; observed={canonical_json(observed_branch_policy)}",
        )
    )

    expected_workflows = requirements["dry_run_workflows"]
    observed_workflows = sorted(item["name"] for item in workflows)
    checks.append(
        _check(
            "workflows.expected-set",
            observed_workflows == expected_workflows,
            f"expected={expected_workflows}; observed={observed_workflows}",
        )
    )
    permission_gaps = sorted(
        item["name"]
        for item in workflows
        if item["permissions"] != {"contents": "read"}
    )
    checks.append(
        _check(
            "workflows.permissions.read-only",
            not permission_gaps,
            f"non_read_only={permission_gaps}",
        )
    )

    checks.sort(key=lambda item: item["id"])
    unsigned = {
        "checks": checks,
        "compliant": all(item["status"] == "passed" for item in checks),
        "expected_repository": expected_repository,
        "kind": "repository-policy-report",
        "mode": "read-only",
        "observed_repository": observed_repository,
        "repository_role": repository_role,
        "schema_version": 2,
        "snapshot_sha256": hashlib.sha256(
            canonical_json(snapshot).encode()
        ).hexdigest(),
    }
    return sha256_envelope(unsigned)
