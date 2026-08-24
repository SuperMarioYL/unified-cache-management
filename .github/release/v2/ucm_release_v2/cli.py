"""Command-line boundary for the entirely read-only v2 planner."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, collect_artifacts, validate_artifacts
from .cleanup import CleanupError, build_cleanup_plan
from .commands import CommandError, parse_command
from .common import canonical_json
from .config import DEFAULT_CONFIG, ConfigError, load_config, retention_days
from .environment import (
    EnvironmentError,
    export_request,
    simulate_result,
    verify_result,
)
from .lifecycle import LifecycleError, build_plan, validate_plan
from .policy import PolicyError, audit_repository_policy
from .reconcile import ReconcileError, build_reconcile_plan
from .render import RenderError, render_release_preview
from .wheels import WheelError, build_wheel_plan, check_environment


def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ucm_release_v2")
    groups = parser.add_subparsers(dest="group", required=True)

    config = groups.add_parser("config")
    config_actions = config.add_subparsers(dest="action", required=True)
    validate = config_actions.add_parser("validate")
    _config_argument(validate)
    retention = config_actions.add_parser("retention")
    retention.add_argument("retention_class")
    _config_argument(retention)

    lifecycle = groups.add_parser("lifecycle")
    lifecycle_actions = lifecycle.add_subparsers(dest="action", required=True)
    plan = lifecycle_actions.add_parser("plan")
    plan.add_argument(
        "--stage",
        required=True,
        choices=("pr", "develop", "nightly", "draft", "rc", "stable", "hotfix"),
    )
    plan.add_argument("--trigger", required=True)
    plan.add_argument("--ref", required=True)
    plan.add_argument("--source-sha", required=True)
    plan.add_argument(
        "--repository-role", required=True, choices=("production", "validation")
    )
    plan.add_argument("--intent-json")
    plan.add_argument("--intent", type=Path)
    plan.add_argument("--pr-number", type=int)
    plan.add_argument("--run-number", type=int)
    plan.add_argument("--date")
    plan.add_argument("--output", type=Path)
    _config_argument(plan)
    validate_plan_parser = lifecycle_actions.add_parser("validate")
    validate_plan_parser.add_argument("--plan", type=Path, required=True)
    _config_argument(validate_plan_parser)

    command = groups.add_parser("command")
    command_actions = command.add_subparsers(dest="action", required=True)
    parse = command_actions.add_parser("parse")
    body = parse.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)
    body.add_argument("--body-env", metavar="NAME")
    parse.add_argument("--actor", required=True)
    parse.add_argument("--author-association", required=True)
    parse.add_argument("--observed-source-sha")
    parse.add_argument("--current-source-sha")

    wheel = groups.add_parser("wheel")
    wheel_actions = wheel.add_subparsers(dest="action", required=True)
    wheel_plan = wheel_actions.add_parser("plan")
    wheel_plan.add_argument("--lifecycle-plan", type=Path, required=True)
    wheel_plan.add_argument("--output", type=Path)
    _config_argument(wheel_plan)
    environment = wheel_actions.add_parser("check-environment")
    environment.add_argument("--installed-json", type=Path)
    _config_argument(environment)

    artifacts = groups.add_parser("artifacts")
    artifact_actions = artifacts.add_subparsers(dest="action", required=True)
    collect = artifact_actions.add_parser("collect")
    collect.add_argument("--lifecycle-plan", type=Path, required=True)
    collect.add_argument("--records-json", type=Path, required=True)
    collect.add_argument("--base-dir", type=Path, required=True)
    collect.add_argument("--output", type=Path)
    _config_argument(collect)
    validate_artifact = artifact_actions.add_parser("validate")
    validate_artifact.add_argument("--lifecycle-plan", type=Path, required=True)
    validate_artifact.add_argument("--manifest", type=Path, required=True)
    validate_artifact.add_argument("--base-dir", type=Path, required=True)
    _config_argument(validate_artifact)

    environment_group = groups.add_parser("environment")
    environment_actions = environment_group.add_subparsers(dest="action", required=True)
    export = environment_actions.add_parser("export")
    export.add_argument("--lifecycle-plan", type=Path, required=True)
    export.add_argument("--manifest", type=Path, required=True)
    export.add_argument("--environment", required=True, choices=("blue", "yellow"))
    export.add_argument("--nonce", required=True)
    export.add_argument("--output", type=Path)
    _config_argument(export)
    simulate = environment_actions.add_parser("simulate")
    simulate.add_argument("--request", type=Path, required=True)
    simulate.add_argument("--verdict", required=True, choices=("passed", "failed"))
    simulate.add_argument("--fail-check")
    simulate.add_argument("--output", type=Path)
    _config_argument(simulate)
    verify = environment_actions.add_parser("verify")
    verify.add_argument("--request", type=Path, required=True)
    verify.add_argument("--result", type=Path, required=True)
    _config_argument(verify)

    reconcile = groups.add_parser("reconcile")
    reconcile_actions = reconcile.add_subparsers(dest="action", required=True)
    reconcile_plan = reconcile_actions.add_parser("plan")
    reconcile_plan.add_argument("--lifecycle-plan", type=Path, required=True)
    reconcile_plan.add_argument("--manifest", type=Path, required=True)
    reconcile_plan.add_argument("--inventory", type=Path, required=True)
    reconcile_plan.add_argument("--promotion", type=Path)
    reconcile_plan.add_argument("--promotion-source-lifecycle-plan", type=Path)
    reconcile_plan.add_argument("--promotion-source-manifest", type=Path)
    reconcile_plan.add_argument("--environment-lifecycle-plan", type=Path)
    reconcile_plan.add_argument("--environment-manifest", type=Path)
    reconcile_plan.add_argument("--environment-request", type=Path)
    reconcile_plan.add_argument("--environment-result", type=Path)
    reconcile_plan.add_argument("--output", type=Path)
    _config_argument(reconcile_plan)

    release = groups.add_parser("release")
    release_actions = release.add_subparsers(dest="action", required=True)
    render = release_actions.add_parser("render")
    render.add_argument("--lifecycle-plan", type=Path, required=True)
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--inventory", type=Path, required=True)
    render.add_argument("--reconcile-plan", type=Path, required=True)
    render.add_argument("--promotion", type=Path)
    render.add_argument("--promotion-source-lifecycle-plan", type=Path)
    render.add_argument("--promotion-source-manifest", type=Path)
    render.add_argument("--environment-lifecycle-plan", type=Path)
    render.add_argument("--environment-manifest", type=Path)
    render.add_argument("--environment-request", type=Path)
    render.add_argument("--environment-result", type=Path)
    render.add_argument("--known-issues-json", type=Path)
    render.add_argument("--output", type=Path)
    _config_argument(render)

    cleanup = groups.add_parser("cleanup")
    cleanup_actions = cleanup.add_subparsers(dest="action", required=True)
    cleanup_plan = cleanup_actions.add_parser("plan")
    cleanup_plan.add_argument("--inventory", type=Path, required=True)
    cleanup_plan.add_argument("--as-of", required=True)
    cleanup_plan.add_argument("--output", type=Path)
    _config_argument(cleanup_plan)

    repo_policy = groups.add_parser("repo-policy")
    repo_policy_actions = repo_policy.add_subparsers(dest="action", required=True)
    audit = repo_policy_actions.add_parser("audit")
    audit.add_argument("--snapshot", type=Path, required=True)
    audit.add_argument(
        "--repository-role", required=True, choices=("validation", "production")
    )
    audit.add_argument("--output", type=Path)
    _config_argument(audit)
    return parser


def _intent(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.intent_json is not None and args.intent is not None:
        raise LifecycleError("provide only one of --intent-json or --intent")
    raw: str | None = args.intent_json
    if args.intent is not None:
        try:
            raw = args.intent.read_text(encoding="utf-8")
        except OSError as error:
            raise LifecycleError(
                f"cannot read release intent: {args.intent}"
            ) from error
    if raw is None:
        return None

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LifecycleError(f"release intent contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise LifecycleError("release intent must be valid JSON") from error
    if not isinstance(value, dict):
        raise LifecycleError("release intent must be a JSON object")
    return value


def _emit(value: object, output: Path | None = None) -> None:
    content = canonical_json(value)
    if output is None:
        print(content)
        return
    if output.exists():
        raise LifecycleError(f"refusing to overwrite output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content + "\n", encoding="utf-8")


def _read_json_object(path: Path, name: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LifecycleError(f"{name} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LifecycleError(f"cannot read {name}: {path}") from error
    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise LifecycleError(f"{name} must be valid JSON") from error
    if not isinstance(value, dict):
        raise LifecycleError(f"{name} must be a JSON object")
    return value


def _command_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        try:
            return args.body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CommandError(f"cannot read command body: {args.body_file}") from error
    assert args.body_env is not None
    try:
        return os.environ[args.body_env]
    except KeyError as error:
        raise CommandError(
            f"command body environment variable is not set: {args.body_env}"
        ) from error


def run(args: argparse.Namespace) -> None:
    if args.group == "command" and args.action == "parse":
        _emit(
            parse_command(
                _command_body(args),
                actor=args.actor,
                author_association=args.author_association,
                observed_source_sha=args.observed_source_sha,
                current_source_sha=args.current_source_sha,
            )
        )
        return
    config = load_config(args.config)
    if args.group == "config":
        if args.action == "validate":
            _emit(config)
        else:
            _emit(
                {
                    "days": retention_days(config, args.retention_class),
                    "kind": "retention-policy",
                    "mode": "dry-run",
                    "retention_class": args.retention_class,
                    "schema_version": 2,
                }
            )
        return
    if args.group == "lifecycle" and args.action == "plan":
        _emit(
            build_plan(
                config,
                stage=args.stage,
                trigger=args.trigger,
                ref=args.ref,
                source_sha=args.source_sha,
                repository_role=args.repository_role,
                intent=_intent(args),
                pr_number=args.pr_number,
                run_number=args.run_number,
                date=args.date,
            ),
            args.output,
        )
        return
    if args.group == "lifecycle" and args.action == "validate":
        plan = validate_plan(config, _read_json_object(args.plan, "lifecycle plan"))
        _emit(
            {
                "kind": "lifecycle-plan-validation",
                "mode": "dry-run",
                "plan_sha256": plan["sha256"],
                "schema_version": 2,
                "semantic_gates": [
                    {"name": "canonical-self-digest", "status": "passed"},
                    {"name": "configured-route", "status": "passed"},
                    {"name": "configured-product-closure", "status": "passed"},
                    {"name": "source-version-binding", "status": "passed"},
                    {"name": "release-intent-binding", "status": "passed"},
                ],
                "source_sha": plan["source_sha"],
                "stage": plan["stage"],
                "status": "passed",
            }
        )
        return
    if args.group == "wheel" and args.action == "plan":
        _emit(build_wheel_plan(config, args.lifecycle_plan), args.output)
        return
    if args.group == "wheel" and args.action == "check-environment":
        _emit(check_environment(args.installed_json))
        return
    if args.group == "artifacts" and args.action == "collect":
        _emit(
            collect_artifacts(
                config, args.lifecycle_plan, args.records_json, args.base_dir
            ),
            args.output,
        )
        return
    if args.group == "artifacts" and args.action == "validate":
        _emit(
            validate_artifacts(
                config, args.lifecycle_plan, args.manifest, args.base_dir
            )
        )
        return
    if args.group == "environment" and args.action == "export":
        _emit(
            export_request(
                config, args.lifecycle_plan, args.manifest, args.environment, args.nonce
            ),
            args.output,
        )
        return
    if args.group == "environment" and args.action == "simulate":
        _emit(
            simulate_result(config, args.request, args.verdict, args.fail_check),
            args.output,
        )
        return
    if args.group == "environment" and args.action == "verify":
        _emit(verify_result(config, args.request, args.result))
        return
    if args.group == "reconcile" and args.action == "plan":
        _emit(
            build_reconcile_plan(
                config,
                args.lifecycle_plan,
                args.manifest,
                args.inventory,
                args.promotion,
                args.promotion_source_lifecycle_plan,
                args.promotion_source_manifest,
                args.environment_lifecycle_plan,
                args.environment_manifest,
                args.environment_request,
                args.environment_result,
            ),
            args.output,
        )
        return
    if args.group == "release" and args.action == "render":
        content = render_release_preview(
            config,
            args.lifecycle_plan,
            args.manifest,
            args.inventory,
            args.reconcile_plan,
            args.known_issues_json,
            args.promotion,
            args.promotion_source_lifecycle_plan,
            args.promotion_source_manifest,
            args.environment_lifecycle_plan,
            args.environment_manifest,
            args.environment_request,
            args.environment_result,
        )
        if args.output is None:
            print(content, end="")
        else:
            if args.output.exists():
                raise RenderError(f"refusing to overwrite output: {args.output}")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        return
    if args.group == "cleanup" and args.action == "plan":
        _emit(
            build_cleanup_plan(
                config,
                _read_json_object(args.inventory, "cleanup inventory"),
                args.as_of,
            ),
            args.output,
        )
        return
    if args.group == "repo-policy" and args.action == "audit":
        _emit(
            audit_repository_policy(
                config,
                _read_json_object(args.snapshot, "repository policy snapshot"),
                args.repository_role,
            ),
            args.output,
        )
        return
    raise LifecycleError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except (
        ArtifactError,
        CleanupError,
        CommandError,
        ConfigError,
        EnvironmentError,
        LifecycleError,
        PolicyError,
        ReconcileError,
        RenderError,
        WheelError,
    ) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
