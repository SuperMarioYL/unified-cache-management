"""Fail-closed lifecycle routing and deterministic release-plan construction."""

from __future__ import annotations

import re
from datetime import date as calendar_date
from typing import Any

from .common import canonical_json, sha256_envelope


class LifecycleError(ValueError):
    """Raised for an ambiguous, unsafe, or contradictory lifecycle request."""


_SHA = re.compile(r"^[0-9a-f]{40}$")
_PR_REF = re.compile(r"^refs/pull/([1-9][0-9]*)/(?:head|merge)$")
_RC_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+rc[1-9][0-9]*$")
_STABLE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ROUTES = {
    "pr": ("pull_request", "validation"),
    "develop": ("push", "validation"),
    "nightly": ("schedule", "validation"),
    "draft": ("workflow_dispatch", "production"),
    "rc": ("workflow_dispatch", "production"),
    "stable": ("workflow_dispatch", "production"),
    "hotfix": ("workflow_dispatch", "production"),
}
_PROTECTED = frozenset({"draft", "rc", "stable", "hotfix"})
_PLAN_KEYS = {
    "channel",
    "gates",
    "kind",
    "mode",
    "operations",
    "products",
    "ref",
    "repository",
    "repository_role",
    "retention_days",
    "schema_version",
    "sha256",
    "source_sha",
    "stage",
    "trigger",
    "version",
}


def _require_sha(source_sha: object) -> None:
    if not isinstance(source_sha, str) or not _SHA.fullmatch(source_sha):
        raise LifecycleError(
            "source_sha must be exactly 40 lowercase hexadecimal characters"
        )


def verify_envelope(value: object, *, kind: str) -> dict[str, Any]:
    """Verify a canonical, self-independent dry-run document envelope."""
    if not isinstance(value, dict):
        raise LifecycleError(f"{kind} envelope must be a JSON object")
    if value.get("kind") != kind:
        raise LifecycleError(f"{kind} envelope has an unexpected kind")
    if value.get("schema_version") != 2 or value.get("mode") != "dry-run":
        raise LifecycleError(f"{kind} envelope must be schema_version 2 dry-run")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise LifecycleError(f"{kind} envelope has an invalid sha256")
    unsigned = dict(value)
    unsigned.pop("sha256")
    if sha256_envelope(unsigned)["sha256"] != digest:
        raise LifecycleError(f"{kind} envelope sha256 does not match its content")
    return value


def _route(
    config: dict[str, Any],
    stage: str,
    trigger: str,
    ref: str,
    repository_role: str,
    pr_number: int | None,
) -> None:
    if stage not in _ROUTES:
        raise LifecycleError(f"unsupported lifecycle stage: {stage}")
    expected_trigger, expected_role = _ROUTES[stage]
    if trigger != expected_trigger:
        raise LifecycleError(f"stage {stage} requires trigger {expected_trigger}")
    if repository_role not in config["repositories"]:
        raise LifecycleError(f"unknown repository role: {repository_role}")
    if repository_role != expected_role:
        raise LifecycleError(f"stage {stage} requires repository role {expected_role}")
    develop_ref = f"refs/heads/{config['branches']['develop']}"
    main_ref = f"refs/heads/{config['branches']['main']}"
    if stage == "pr":
        match = _PR_REF.fullmatch(ref)
        if not match:
            raise LifecycleError(
                "pr stage requires a refs/pull/<number>/head or merge ref"
            )
        if pr_number is not None and int(match.group(1)) != pr_number:
            raise LifecycleError("pr_number does not match the pull-request ref")
    elif stage in {"develop", "nightly"} and ref != develop_ref:
        raise LifecycleError(f"stage {stage} requires ref {develop_ref}")
    elif stage in _PROTECTED and ref != main_ref:
        raise LifecycleError(f"stage {stage} requires ref {main_ref}")


def _intent(stage: str, intent: dict[str, Any] | None, source_sha: str) -> str | None:
    if stage not in _PROTECTED:
        if intent is not None:
            raise LifecycleError(f"stage {stage} must not include release intent")
        return None
    if not isinstance(intent, dict):
        raise LifecycleError(f"stage {stage} requires release intent")
    if set(intent) != {"stage", "version", "source_sha"}:
        raise LifecycleError(
            "release intent must contain exactly stage, version, and source_sha"
        )
    if intent["stage"] != stage:
        raise LifecycleError("release intent stage does not match requested stage")
    if intent["source_sha"] != source_sha:
        raise LifecycleError(
            "release intent source_sha does not match requested source_sha"
        )
    version = intent["version"]
    if not isinstance(version, str):
        raise LifecycleError("release intent version must be a string")
    if stage in {"draft", "rc"} and not _RC_VERSION.fullmatch(version):
        raise LifecycleError(f"stage {stage} requires an rc version")
    if stage in {"stable", "hotfix"} and not _STABLE_VERSION.fullmatch(version):
        raise LifecycleError(f"stage {stage} requires a stable x.y.z version")
    return version


def _version(
    stage: str,
    base_version: str,
    source_sha: str,
    *,
    pr_number: int | None,
    run_number: int | None,
    date: str | None,
    intent_version: str | None,
) -> str:
    short_sha = source_sha[:12]
    if stage == "pr":
        if not isinstance(pr_number, int) or pr_number < 1:
            raise LifecycleError("pr stage requires a positive pr_number")
        return f"{base_version}.dev0+pr.{pr_number}.g.{short_sha}"
    if stage == "develop":
        if not isinstance(run_number, int) or run_number < 1:
            raise LifecycleError("develop stage requires a positive run_number")
        return f"{base_version}.dev{run_number}+develop.g.{short_sha}"
    if stage == "nightly":
        if not isinstance(date, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date
        ):
            raise LifecycleError("nightly stage requires date formatted YYYY-MM-DD")
        try:
            calendar_date.fromisoformat(date)
        except ValueError as error:
            raise LifecycleError(
                "nightly stage requires a valid calendar date formatted YYYY-MM-DD"
            ) from error
        return f"{base_version}.dev{date.replace('-', '')}+nightly.g.{short_sha}"
    assert intent_version is not None
    if stage == "draft":
        return f"{intent_version}.dev0+draft.g.{short_sha}"
    return intent_version


def _products(
    config: dict[str, Any], repository: str, version: str
) -> list[dict[str, str]]:
    products: list[dict[str, str]] = []
    for item in config["products"]["wheels"]:
        products.append(
            {
                "kind": "wheel",
                "name": item["distribution"],
                "coordinate": f"{item['distribution']}=={version}",
            }
        )
    for item in config["products"]["images"]:
        products.append(
            {
                "kind": "image",
                "name": item["family"],
                "coordinate": item["repository"].replace("{repository}", repository)
                + f":{version}",
            }
        )
    chart = config["products"]["chart"]
    products.append(
        {
            "kind": "chart",
            "name": chart["name"],
            "coordinate": f"{chart['name']}@{version}",
        }
    )
    return sorted(products, key=lambda item: (item["kind"], item["name"]))


def _plan_gates(value: object) -> None:
    expected = [
        {"name": "source-sha", "status": "passed"},
        {
            "name": "publication",
            "status": "blocked",
            "reason": "dry-run does not publish",
        },
    ]
    if value != expected:
        raise LifecycleError("lifecycle plan gates must be the generated dry-run gates")


def _plan_operations(value: object) -> None:
    expected = [
        {"action": "build-preview", "executed": False},
        {"action": "publish-preview", "executed": False},
    ]
    if value != expected:
        raise LifecycleError(
            "lifecycle plan operations must be generated and executed=false"
        )


def _plan_version(
    config: dict[str, Any], plan: dict[str, Any], intent_version: str | None
) -> None:
    stage = plan["stage"]
    source_sha = plan["source_sha"]
    version = plan["version"]
    if not isinstance(version, str) or not version:
        raise LifecycleError("lifecycle plan version must be a non-empty string")
    if stage == "pr":
        match = _PR_REF.fullmatch(plan["ref"])
        assert match is not None
        expected = _version(
            stage,
            config["version"],
            source_sha,
            pr_number=int(match.group(1)),
            run_number=None,
            date=None,
            intent_version=None,
        )
        if version != expected:
            raise LifecycleError(
                "pr lifecycle plan version does not match ref number and source_sha"
            )
        return
    if stage == "develop":
        pattern = rf"^{re.escape(config['version'])}\.dev[1-9][0-9]*\+develop\.g\.{source_sha[:12]}$"
        if not re.fullmatch(pattern, version):
            raise LifecycleError(
                "develop lifecycle plan version does not match run number and source_sha"
            )
        return
    if stage == "nightly":
        pattern = rf"^{re.escape(config['version'])}\.dev([0-9]{{8}})\+nightly\.g\.{source_sha[:12]}$"
        match = re.fullmatch(pattern, version)
        if match is None:
            raise LifecycleError(
                "nightly lifecycle plan version does not match date and source_sha"
            )
        compact_date = match.group(1)
        try:
            calendar_date.fromisoformat(
                f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}"
            )
        except ValueError as error:
            raise LifecycleError(
                "nightly lifecycle plan version contains an invalid calendar date"
            ) from error
        return
    expected = _version(
        stage,
        config["version"],
        source_sha,
        pr_number=None,
        run_number=None,
        date=None,
        intent_version=intent_version,
    )
    if version != expected:
        raise LifecycleError(
            f"{stage} lifecycle plan version does not match release intent"
        )


def validate_plan(config: dict[str, Any], value: object) -> dict[str, Any]:
    """Validate every semantic field of an already-generated lifecycle plan.

    Unlike :func:`build_plan`, this deliberately derives request details from the
    content-addressed plan itself. It therefore does not depend on absent CLI-only inputs
    such as a develop run number or nightly date.
    """
    if config.get("mode") != "dry-run":
        raise LifecycleError("mode must be dry-run")
    plan = verify_envelope(value, kind="lifecycle-plan")
    stage = plan.get("stage")
    if not isinstance(stage, str) or stage not in _ROUTES:
        raise LifecycleError("unsupported lifecycle stage")
    expected_keys = _PLAN_KEYS | ({"release_intent"} if stage in _PROTECTED else set())
    if set(plan) != expected_keys:
        raise LifecycleError("lifecycle plan top-level keys do not match its stage")
    for key in (
        "channel",
        "ref",
        "repository",
        "repository_role",
        "trigger",
        "version",
    ):
        if (
            not isinstance(plan[key], str)
            or not plan[key]
            or plan[key] != plan[key].strip()
        ):
            raise LifecycleError(
                f"lifecycle plan {key} must be a non-empty trimmed string"
            )
    _require_sha(plan["source_sha"])
    if plan["channel"] != stage:
        raise LifecycleError("lifecycle plan channel does not match stage")
    role = plan["repository_role"]
    if (
        role not in config["repositories"]
        or plan["repository"] != config["repositories"][role]
    ):
        raise LifecycleError("lifecycle plan repository does not match repository_role")
    pr_number: int | None = None
    if stage == "pr":
        match = _PR_REF.fullmatch(plan["ref"])
        if match is not None:
            pr_number = int(match.group(1))
    _route(config, stage, plan["trigger"], plan["ref"], role, pr_number)
    intent = plan.get("release_intent") if stage in _PROTECTED else None
    intent_version = _intent(stage, intent, plan["source_sha"])
    _plan_version(config, plan, intent_version)
    if plan.get("retention_days") != config["retention_days"][stage]:
        raise LifecycleError("lifecycle plan retention_days does not match stage")
    if plan.get("products") != _products(config, plan["repository"], plan["version"]):
        raise LifecycleError(
            "lifecycle plan product closure does not match configured products"
        )
    _plan_gates(plan.get("gates"))
    _plan_operations(plan.get("operations"))
    return plan


def build_plan(
    config: dict[str, Any],
    *,
    stage: str,
    trigger: str,
    ref: str,
    source_sha: str,
    repository_role: str,
    intent: dict[str, Any] | None = None,
    pr_number: int | None = None,
    run_number: int | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Build a fully read-only plan from explicit request identity only."""
    if config.get("mode") != "dry-run":
        raise LifecycleError("mode must be dry-run")
    _require_sha(source_sha)
    _route(config, stage, trigger, ref, repository_role, pr_number)
    intent_version = _intent(stage, intent, source_sha)
    version = _version(
        stage,
        config["version"],
        source_sha,
        pr_number=pr_number,
        run_number=run_number,
        date=date,
        intent_version=intent_version,
    )
    repository = config["repositories"][repository_role]
    plan: dict[str, Any] = {
        "channel": stage,
        "gates": [
            {"name": "source-sha", "status": "passed"},
            {
                "name": "publication",
                "status": "blocked",
                "reason": "dry-run does not publish",
            },
        ],
        "kind": "lifecycle-plan",
        "mode": "dry-run",
        "operations": [
            {"action": "build-preview", "executed": False},
            {"action": "publish-preview", "executed": False},
        ],
        "products": _products(config, repository, version),
        "ref": ref,
        "repository": repository,
        "repository_role": repository_role,
        "retention_days": config["retention_days"][stage],
        "schema_version": 2,
        "source_sha": source_sha,
        "stage": stage,
        "trigger": trigger,
        "version": version,
    }
    if intent is not None:
        plan["release_intent"] = {key: intent[key] for key in sorted(intent)}
    return sha256_envelope(plan)


__all__ = [
    "LifecycleError",
    "build_plan",
    "canonical_json",
    "validate_plan",
    "verify_envelope",
]
