"""Command-line interface for the compact UCM release package."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

from . import core, image, registry, wheel

catalog_resolution = registry


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    parser.add_argument("--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR)


def _empty_output_dir(path: Path) -> Path:
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, value: object) -> None:
    path.write_bytes(core.canonical_bytes(value) + b"\n")


def _release_asset_manifest(
    request: dict[str, object], *, manifest_key: str = "manifest"
) -> dict[str, object]:
    manifest_path = request.get(manifest_key)
    allowed_root = request.get("allowed_root")
    source_sha = request.get("source_sha")
    resolved_plan_path = request.get("resolved_plan")
    expected_plan_sha256 = request.get("resolved_plan_sha256")
    if not all(
        isinstance(value, str)
        for value in (
            manifest_path,
            allowed_root,
            source_sha,
            resolved_plan_path,
            expected_plan_sha256,
        )
    ):
        raise ValueError("release asset manifest binding is malformed")
    resolved_plan = core.load_json(Path(resolved_plan_path))
    manifest = verify.validate_release_asset_manifest(
        core.load_json(Path(manifest_path)),
        allowed_root=Path(allowed_root),
        resolved_plan=resolved_plan,
        expected_plan_sha256=expected_plan_sha256,
    )
    if manifest["source_sha"] != source_sha:
        raise ValueError("release asset manifest source differs from live Release")
    return manifest


def _release_plan_binding(
    request: dict[str, object],
) -> tuple[dict[str, object], str]:
    path = request.get("resolved_plan")
    expected = request.get("resolved_plan_sha256")
    if not isinstance(path, str) or not isinstance(expected, str):
        raise ValueError("release frozen plan binding is malformed")
    plan = core.load_json(Path(path))
    registry.resolved_registry_contract(plan, expected_plan_sha256=expected)
    return plan, expected


def _release_asset_state(
    request: dict[str, object], *, release_key: str = "release"
) -> dict[str, object]:
    release_path = request.get(release_key)
    source_sha = request.get("source_sha")
    release_id = request.get("release_id")
    if not isinstance(release_path, str) or not isinstance(source_sha, str):
        raise ValueError("release asset state binding is malformed")
    _release_asset_manifest(request)
    resolved_plan, plan_sha256 = _release_plan_binding(request)
    state = verify.plan_github_release(
        core.load_json(Path(release_path)),
        source_sha,
        resolved_plan=resolved_plan,
        expected_plan_sha256=plan_sha256,
    )
    if state["release_id"] != release_id:
        raise ValueError("release asset state binding changed release id")
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ucm_release")
    groups = parser.add_subparsers(dest="group", required=True)

    config = groups.add_parser("config")
    config_actions = config.add_subparsers(dest="action", required=True)
    validate = config_actions.add_parser("validate")
    _paths(validate)

    catalog_parser = groups.add_parser("catalog")
    catalog_actions = catalog_parser.add_subparsers(dest="action", required=True)
    catalog_validate = catalog_actions.add_parser("validate")
    catalog_validate.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    catalog_validate.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    catalog_validate.add_argument(
        "--repository-root", type=Path, default=core.REPO_ROOT
    )
    catalog_resolve = catalog_actions.add_parser("resolve")
    catalog_resolve.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    catalog_resolve.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    catalog_resolve.add_argument(
        "--lane", choices=("feature-candidate", "protected-tag"), required=True
    )
    catalog_resolve.add_argument("--source-sha", required=True)
    catalog_resolve.add_argument("--fixture", type=Path)
    catalog_resolve.add_argument("--output", type=Path, required=True)
    catalog_select = catalog_actions.add_parser("select")
    catalog_select.add_argument("--plan", type=Path, required=True)
    catalog_select.add_argument(
        "--task-kind", choices=("wheel", "image", "family"), required=True
    )
    catalog_select.add_argument("--task-id", required=True)
    catalog_select.add_argument("--expected-plan-sha256", required=True)
    catalog_select.add_argument("--output", type=Path)
    catalog_drift = catalog_actions.add_parser("verify-drift")
    catalog_drift.add_argument("--plan", type=Path, required=True)
    catalog_drift.add_argument("--fixture", type=Path)
    catalog_drift.add_argument("--output", type=Path)
    recipe_matrix = catalog_actions.add_parser("recipe-matrix")
    recipe_matrix.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    recipe_matrix.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    recipe_matrix.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    recipe_matrix.add_argument(
        "--lane",
        choices=("pr-smoke", "hardware-e2e", "manual", "formal-release"),
        required=True,
    )
    recipe_matrix.add_argument("--output", type=Path, required=True)
    select_recipe = catalog_actions.add_parser("select-recipe")
    select_recipe.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    select_recipe.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    select_recipe.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    select_recipe.add_argument(
        "--lane",
        choices=("pr-smoke", "hardware-e2e", "manual", "formal-release"),
        required=True,
    )
    select_recipe.add_argument("--task-id", required=True)
    select_recipe.add_argument("--expected-catalog-sha256", required=True)
    select_recipe.add_argument("--expected-matrix-sha256", required=True)
    select_recipe.add_argument("--expected-task-sha256", required=True)
    select_recipe.add_argument("--output", type=Path, required=True)
    render_recipes = catalog_actions.add_parser("render-recipes")
    render_recipes.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    render_recipes.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    render_recipes.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    render_recipes.add_argument("--output", type=Path, required=True)

    core_parser = groups.add_parser("core")
    core_actions = core_parser.add_subparsers(dest="action", required=True)
    hosted_task = core_actions.add_parser("hosted-task")
    hosted_task.add_argument("--task", required=True, type=Path)
    hosted_task.add_argument("--source-sha", required=True)
    hosted_task.add_argument("--source-date-epoch", required=True, type=int)
    hosted_task.add_argument("--resolved-plan", required=True, type=Path)
    hosted_task.add_argument("--expected-plan-sha256", required=True)
    hosted_task.add_argument("--output", required=True, type=Path)
    hosted_image_task = core_actions.add_parser("hosted-image-task")
    hosted_image_task.add_argument("--task", required=True, type=Path)
    hosted_image_task.add_argument("--source-sha", required=True)
    hosted_image_task.add_argument("--source-date-epoch", required=True, type=int)
    hosted_image_task.add_argument("--resolved-plan", required=True, type=Path)
    hosted_image_task.add_argument("--expected-plan-sha256", required=True)
    hosted_image_task.add_argument("--output", required=True, type=Path)
    tag_preflight = core_actions.add_parser("tag-preflight")
    tag_preflight.add_argument(
        "--lane", choices=("feature-candidate", "protected-tag"), required=True
    )
    tag_preflight.add_argument("--resolved-plan", type=Path)
    tag_preflight.add_argument("--expected-plan-sha256")
    tag_preflight.add_argument(
        "--catalog-planner",
        action="store_true",
        help="resolve current catalog authority only in the initial planning job",
    )
    _paths(tag_preflight)

    wheel_parser = groups.add_parser("wheel")
    wheel_actions = wheel_parser.add_subparsers(dest="action", required=True)
    inspect = wheel_actions.add_parser("inspect")
    inspect.add_argument("wheel", type=Path)
    inspect.add_argument("--spec-id", required=True)
    inspect.add_argument("--expected-sha256", required=True)
    inspect.add_argument(
        "--source-kind", choices=("fixture", "builder-candidate"), required=True
    )
    inspect.add_argument("--task-file", type=Path)
    _paths(inspect)
    seal = wheel_actions.add_parser("seal")
    seal.add_argument("wheel", type=Path)
    seal.add_argument("--spec-id", required=True)
    seal.add_argument("--source-sha", required=True)
    seal.add_argument("--build-key", required=True)
    seal.add_argument("--source-date-epoch", required=True, type=int)
    seal.add_argument("--authority-file", required=True, type=Path)
    seal.add_argument("--dependency-closure", required=True, type=Path)
    seal.add_argument("--task-file", required=True, type=Path)
    seal.add_argument("--output-dir", required=True, type=Path)
    _paths(seal)
    authority = wheel_actions.add_parser("authority")
    authority.add_argument("--spec-id", required=True)
    authority.add_argument("--source-sha", required=True)
    authority.add_argument("--source-date-epoch", required=True, type=int)
    authority.add_argument("--builder-coordinate", required=True)
    authority.add_argument("--wheelhouse", required=True, type=Path)
    authority.add_argument("--source-archive", required=True, type=Path)
    authority.add_argument("--source-commit-payload", required=True, type=Path)
    authority.add_argument("--source-manifest", required=True, type=Path)
    authority.add_argument("--source-root", required=True, type=Path)
    authority.add_argument("--task-file", required=True, type=Path)
    authority.add_argument("--output", required=True, type=Path)
    _paths(authority)
    context = wheel_actions.add_parser("context")
    context.add_argument("--source-sha", required=True)
    context.add_argument("--output-dir", required=True, type=Path)
    verify_context = wheel_actions.add_parser("verify-context")
    verify_context.add_argument("--archive", required=True, type=Path)
    verify_context.add_argument("--commit-payload", required=True, type=Path)
    verify_context.add_argument("--expected-source-sha", required=True)
    verify_context.add_argument("--manifest", required=True, type=Path)
    verify_context.add_argument("--source-root", required=True, type=Path)
    closure = wheel_actions.add_parser("closure")
    closure.add_argument("wheel", type=Path)
    closure.add_argument("--spec-id", required=True)
    closure.add_argument("--authority-file", required=True, type=Path)
    closure.add_argument("--output", required=True, type=Path)
    closure.add_argument("--task-file", required=True, type=Path)
    _paths(closure)
    preflight_dependencies = wheel_actions.add_parser("preflight-dependencies")
    preflight_dependencies.add_argument("--binary", required=True, type=Path)
    preflight_dependencies.add_argument("--spec-id", required=True)
    preflight_dependencies.add_argument("--task-file", required=True, type=Path)
    _paths(preflight_dependencies)
    check_environment = wheel_actions.add_parser("check-environment")
    check_environment.add_argument("--task", required=True, type=Path)
    check_environment.add_argument("--python-executable", required=True, type=Path)
    fixture_build = wheel_actions.add_parser("fixture-build")
    fixture_build.add_argument("--output-dir", type=Path, required=True)
    fixture_build.add_argument("--source-sha", required=True)
    fixture_build.add_argument("--profile-id", required=True)
    _paths(fixture_build)

    chart_parser = groups.add_parser("chart")
    chart_actions = chart_parser.add_subparsers(dest="action", required=True)
    package = chart_actions.add_parser("package")
    package.add_argument("--output-dir", type=Path, required=True)
    package.add_argument("--resolved-plan", type=Path, required=True)
    package.add_argument("--expected-plan-sha256", required=True)
    _paths(package)

    registry_parser = groups.add_parser("registry")
    registry_actions = registry_parser.add_subparsers(dest="action", required=True)
    scan = registry_actions.add_parser("fixture-scan")
    scan.add_argument("--repository", required=True)
    scan.add_argument("--tag", required=True)
    scan.add_argument("--fixture", type=Path, required=True)
    for action in (
        "inventory",
        "verify-member",
        "plan-index",
        "verify-index",
        "prepare-index",
        "finalize-index",
        "aggregate-authenticated",
        "aggregate-protected",
        "audit-operations",
    ):
        command = registry_actions.add_parser(action)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)

    artifact_parser = groups.add_parser("artifact")
    artifact_actions = artifact_parser.add_subparsers(dest="action", required=True)
    for action in (
        "validate-image-bridge",
        "validate-index-parent",
        "collect-members",
        "collect-provisionals",
    ):
        command = artifact_actions.add_parser(action)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)

    release_parser = groups.add_parser("release")
    release_actions = release_parser.add_subparsers(dest="action", required=True)
    for action in (
        "assets-manifest",
        "plan-state",
        "plan-assets",
        "verify-assets",
        "select-pages",
        "plan-downloads",
        "complete-downloads",
        "refresh-assets",
        "verify-upload-prefix",
        "record-upload-response",
        "rebase-manifest",
        "operation-ledger",
        "publication-evidence",
    ):
        command = release_actions.add_parser(action)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)

    reconcile_parser = groups.add_parser("fixture-reconcile")
    reconcile_parser.set_defaults(action=None)
    reconcile_parser.add_argument("--input", type=Path, required=True)

    loop_parser = groups.add_parser("loop")
    loop_actions = loop_parser.add_subparsers(dest="action", required=True)
    loop_verify = loop_actions.add_parser("verify")
    loop_verify.add_argument("--input", type=Path, required=True)
    loop_verify.add_argument("--run-id", required=True)
    loop_verify.add_argument("--attempt", type=int, required=True)
    loop_prepare = loop_actions.add_parser("prepare")
    loop_prepare.add_argument("--build-record", type=Path, required=True)
    loop_prepare.add_argument("--wheel-inspection", type=Path, required=True)
    loop_prepare.add_argument("--source-sha", required=True)
    loop_prepare.add_argument("--output-dir", type=Path, required=True)
    loop_prepare.add_argument("--run-id", required=True)
    loop_prepare.add_argument("--attempt", type=int, required=True)
    loop_complete = loop_actions.add_parser("complete")
    loop_complete.add_argument("--prepared", type=Path, required=True)
    loop_complete.add_argument("--image-result", type=Path, required=True)
    loop_complete.add_argument("--source-sha", required=True)
    loop_complete.add_argument("--output-dir", type=Path, required=True)
    loop_complete.add_argument("--run-id", required=True)
    loop_complete.add_argument("--attempt", type=int, required=True)
    loop_aggregate = loop_actions.add_parser("aggregate")
    loop_aggregate.add_argument("--build-record", type=Path, required=True)
    loop_aggregate.add_argument("--wheel-inspection", type=Path, required=True)
    loop_aggregate.add_argument("--wheel", type=Path, required=True)
    loop_aggregate.add_argument("--chart-result", type=Path, required=True)
    loop_aggregate.add_argument("--chart-package", type=Path, required=True)
    loop_aggregate.add_argument("--image-result", type=Path, required=True)
    loop_aggregate.add_argument("--oci-evidence-dir", type=Path, required=True)
    loop_aggregate.add_argument("--image-recipe", type=Path, required=True)
    loop_aggregate.add_argument("--image-metadata", type=Path, required=True)
    loop_aggregate.add_argument("--image-prepare", type=Path, required=True)
    loop_aggregate.add_argument("--buildkit-metadata", type=Path, required=True)
    loop_aggregate.add_argument("--image-archive-sha256", type=Path, required=True)
    loop_aggregate.add_argument("--completed-loop", type=Path, required=True)
    loop_aggregate.add_argument("--second-reconcile", type=Path, required=True)
    loop_aggregate.add_argument("--image-loop", type=Path, required=True)
    loop_aggregate.add_argument("--repository", required=True)
    loop_aggregate.add_argument("--ref", required=True)
    loop_aggregate.add_argument("--source-sha", required=True)
    loop_aggregate.add_argument("--resolved-plan", type=Path, required=True)
    loop_aggregate.add_argument("--expected-plan-sha256", required=True)
    loop_aggregate.add_argument("--output", type=Path, required=True)
    loop_aggregate.add_argument("--run-id", required=True)
    loop_aggregate.add_argument("--attempt", type=int, required=True)
    loop_aggregate_real = loop_actions.add_parser("aggregate-real")
    loop_aggregate_real.add_argument("--wheel-dir", type=Path, required=True)
    loop_aggregate_real.add_argument("--image-dir", type=Path, required=True)
    loop_aggregate_real.add_argument("--chart-result", type=Path)
    loop_aggregate_real.add_argument("--chart-package", type=Path)
    loop_aggregate_real.add_argument("--repository", required=True)
    loop_aggregate_real.add_argument("--ref", required=True)
    loop_aggregate_real.add_argument("--source-sha", required=True)
    loop_aggregate_real.add_argument("--resolved-plan", type=Path, required=True)
    loop_aggregate_real.add_argument("--expected-plan-sha256", required=True)
    loop_aggregate_real.add_argument(
        "--selected-wheel-matrix", type=Path, required=True
    )
    loop_aggregate_real.add_argument(
        "--selected-image-matrix", type=Path, required=True
    )
    loop_aggregate_real.add_argument("--output", type=Path, required=True)
    loop_aggregate_real.add_argument("--output-dir", type=Path)
    loop_aggregate_real.add_argument("--run-id", required=True)
    loop_aggregate_real.add_argument("--attempt", type=int, required=True)

    image_parser = groups.add_parser("image")
    image_actions = image_parser.add_subparsers(dest="action", required=True)
    image_actions.add_parser("base-authority")
    image_actions.add_parser("toolchain-authority")
    task_toolchain = image_actions.add_parser("task-toolchain-authority")
    task_toolchain.add_argument("--resolved-plan", type=Path, required=True)
    task_toolchain.add_argument(
        "--task-kind", choices=("wheel", "image"), required=True
    )
    task_toolchain.add_argument("--task-id", required=True)
    task_toolchain.add_argument("--expected-plan-sha256", required=True)
    image_verify = image_actions.add_parser("verify", allow_abbrev=False)
    image_verify.add_argument("--context", type=Path, required=True)
    image_verify.add_argument("--oci", type=Path, required=True)
    image_verify.add_argument("--evidence-dir", type=Path, required=True)
    image_verify.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    image_verify.add_argument(
        "--output-mode", choices=("feature", "production"), default="feature"
    )
    image_verify.add_argument("--resolved-plan", type=Path)
    image_verify.add_argument("--task-id")
    image_verify.add_argument("--expected-plan-sha256")
    image_prepare = image_actions.add_parser("prepare")
    image_prepare.add_argument("--input", type=Path, required=True)
    image_prepare.add_argument("--wheel-dir", type=Path, required=True)
    image_prepare.add_argument("--expected-source-sha", required=True)
    image_prepare.add_argument("--base-authority", type=Path, required=True)
    image_prepare.add_argument("--base-index", type=Path, required=True)
    image_prepare.add_argument("--base-manifest", type=Path, required=True)
    image_prepare.add_argument("--base-config", type=Path, required=True)
    image_prepare.add_argument("--output-dir", type=Path, required=True)
    real_authorities = image_actions.add_parser("real-authorities")
    real_authorities.add_argument("--resolved-plan", type=Path, required=True)
    real_authorities.add_argument("--task-id", required=True)
    real_authorities.add_argument("--expected-plan-sha256", required=True)
    image_real_base = image_actions.add_parser("base-record-real")
    image_real_base.add_argument("--index", type=Path, required=True)
    image_real_base.add_argument("--manifest", type=Path, required=True)
    image_real_base.add_argument("--config", type=Path, required=True)
    image_real_base.add_argument("--task-authority", type=Path, required=True)
    image_prepare_real = image_actions.add_parser("prepare-real")
    image_prepare_real.add_argument("--wheel", type=Path, required=True)
    image_prepare_real.add_argument("--wheel-inspection", type=Path, required=True)
    image_prepare_real.add_argument("--base-record", type=Path, required=True)
    image_prepare_real.add_argument(
        "--runtime-wheel", type=Path, action="append", required=True
    )
    image_prepare_real.add_argument("--output-dir", type=Path, required=True)
    image_prepare_real.add_argument("--task-authority", type=Path, required=True)
    _paths(image_prepare_real)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if (args.group, args.action) == ("config", "validate"):
            release = core.load_catalog(args.release, args.schema_dir)
            result = {
                "schema_version": 1,
                "wheel_profiles": len(release["wheel_profiles"]),
                "compatibility_rules": len(release["compatibility"]["rules"]),
            }
        elif (args.group, args.action) == ("catalog", "validate"):
            release = core.load_catalog(
                args.catalog,
                args.schema_dir,
                repository_root=args.repository_root,
            )
            catalog_resolution.validate_catalog_tag_grammar(release)
            result = {
                "kind": "ucm-catalog-validation",
                "schema_version": 1,
                "config_sha256": core.sha256_value(release),
                "upstream_products": len(release["upstream_products"]),
                "compatibility_rules": len(release["compatibility"]["rules"]),
            }
        elif (args.group, args.action) == ("catalog", "resolve"):
            release = core.load_catalog(args.catalog, args.schema_dir)
            fixture = core.load_json(args.fixture) if args.fixture else None
            result = catalog_resolution.resolve_catalog(
                release,
                source_sha=args.source_sha,
                lane=args.lane,
                fixture=fixture,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, result)
        elif (args.group, args.action) == ("catalog", "select"):
            result = catalog_resolution.select_task(
                core.load_json(args.plan),
                task_kind=args.task_kind,
                task_id=args.task_id,
                expected_plan_sha256=args.expected_plan_sha256,
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                _write(args.output, result)
        elif (args.group, args.action) == ("catalog", "verify-drift"):
            fixture = core.load_json(args.fixture) if args.fixture else None
            result = catalog_resolution.verify_upstream_drift(
                core.load_json(args.plan), fixture=fixture
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                _write(args.output, result)
        elif (args.group, args.action) == ("catalog", "recipe-matrix"):
            release = core.load_catalog(
                args.catalog,
                args.schema_dir,
                repository_root=args.repository_root,
            )
            result = core.repository_recipe_matrix(
                release,
                lane=args.lane,
                repository_root=args.repository_root,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, result)
        elif (args.group, args.action) == ("catalog", "select-recipe"):
            release = core.load_catalog(
                args.catalog,
                args.schema_dir,
                repository_root=args.repository_root,
            )
            result = core.select_repository_recipe_task(
                release,
                lane=args.lane,
                task_id=args.task_id,
                expected_catalog_sha256=args.expected_catalog_sha256,
                expected_matrix_sha256=args.expected_matrix_sha256,
                expected_task_sha256=args.expected_task_sha256,
                repository_root=args.repository_root,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, result)
        elif (args.group, args.action) == ("catalog", "render-recipes"):
            release = core.load_catalog(
                args.catalog,
                args.schema_dir,
                repository_root=args.repository_root,
            )
            rendered = core.render_repository_recipe_markdown(
                release, repository_root=args.repository_root
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            result = {
                "kind": "ucm-repository-recipe-reference",
                "schema_version": 1,
                "catalog_sha256": core.sha256_value(release),
                "content_sha256": core.sha256_value(rendered),
            }
        elif (args.group, args.action) == ("core", "hosted-task"):
            result = verify.hosted_wheel_task(
                core.load_json(args.task),
                args.source_sha,
                args.source_date_epoch,
                resolved_plan=core.load_json(args.resolved_plan),
                expected_plan_sha256=args.expected_plan_sha256,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, result)
        elif (args.group, args.action) == ("core", "hosted-image-task"):
            result = verify.hosted_image_task(
                core.load_json(args.task),
                args.source_sha,
                args.source_date_epoch,
                resolved_plan=core.load_json(args.resolved_plan),
                expected_plan_sha256=args.expected_plan_sha256,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, result)
        elif (args.group, args.action) == ("core", "tag-preflight"):
            if args.catalog_planner:
                if (
                    args.resolved_plan is not None
                    or args.expected_plan_sha256 is not None
                ):
                    raise ValueError(
                        "catalog planner mode cannot consume a frozen plan binding"
                    )
                result = core.tag_preflight(
                    lane=args.lane,
                    release_path=args.release,
                    schema_dir=args.schema_dir,
                )
            else:
                if (
                    args.resolved_plan is None
                    or not isinstance(args.expected_plan_sha256, str)
                    or not args.expected_plan_sha256
                ):
                    raise ValueError(
                        "tag preflight requires an exact frozen plan and expected plan hash"
                    )
                resolved_plan = core.load_json(args.resolved_plan)
                registry.validate_resolved_plan(resolved_plan)
                if resolved_plan["resolved_plan_sha256"] != args.expected_plan_sha256:
                    raise ValueError(
                        "resolved plan hash differs from expected plan hash"
                    )
                if resolved_plan["lane"] != args.lane:
                    raise ValueError("tag preflight lane differs from frozen plan")
                result = core.tag_preflight(
                    lane=args.lane,
                    authority=resolved_plan["source"],
                )
        elif (args.group, args.action) == ("wheel", "inspect"):
            result = wheel.inspect_wheel(
                args.wheel,
                args.spec_id,
                args.expected_sha256,
                args.source_kind,
                task_path=args.task_file,
                release_path=args.release,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("wheel", "seal"):
            result = wheel.seal_wheel(
                args.wheel,
                args.output_dir,
                args.spec_id,
                args.source_sha,
                args.build_key,
                args.source_date_epoch,
                args.authority_file,
                args.dependency_closure,
                task_path=args.task_file,
            )
        elif (args.group, args.action) == ("wheel", "authority"):
            result = wheel.build_authority_record(
                args.output,
                args.spec_id,
                args.source_sha,
                args.source_date_epoch,
                args.builder_coordinate,
                args.wheelhouse,
                args.source_archive,
                args.source_commit_payload,
                args.source_manifest,
                args.source_root,
                args.task_file,
            )
        elif (args.group, args.action) == ("wheel", "context"):
            result = wheel.prepare_source_context(args.output_dir, args.source_sha)
        elif (args.group, args.action) == ("wheel", "verify-context"):
            result = wheel.verify_source_context(
                args.archive,
                args.manifest,
                args.source_root,
                args.commit_payload,
                args.expected_source_sha,
            )
        elif (args.group, args.action) == ("wheel", "closure"):
            result = wheel.audit_dependency_closure(
                args.wheel,
                args.output,
                args.spec_id,
                args.authority_file,
                task_path=args.task_file,
            )
        elif (args.group, args.action) == ("wheel", "preflight-dependencies"):
            result = wheel.preflight_dependencies(
                args.binary,
                args.spec_id,
                task_path=args.task_file,
            )
        elif (args.group, args.action) == ("wheel", "check-environment"):
            result = wheel.check_build_environment(
                core.load_json(args.task),
                python_executable=args.python_executable,
            )
        elif (args.group, args.action) == ("wheel", "fixture-build"):
            result = wheel.build_fixture_wheel(
                args.output_dir,
                args.source_sha,
                args.profile_id,
                release_path=args.release,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("chart", "package"):
            result = core.package_chart(
                args.output_dir,
                resolved_plan=core.load_json(args.resolved_plan),
                expected_plan_sha256=args.expected_plan_sha256,
            )
        elif (args.group, args.action) == ("registry", "fixture-scan"):
            fixture = core.load_json(args.fixture)
            result = registry.scan_fixture_registry(
                args.repository,
                args.tag,
                fixture=fixture,
            )
        elif (args.group, args.action) == ("registry", "inventory"):
            request = core.load_json(args.input)
            if set(request) != {"resolved_plan", "resolved_plan_sha256"} or any(
                not isinstance(request[key], str) for key in request
            ):
                raise ValueError("inventory input requires one frozen resolved plan")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            contract = registry.resolved_registry_contract(
                resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            result = registry.inventory_registry(
                targets=[
                    {
                        "repository": item["target_repository"],
                        "tag": item["target_tag"],
                    }
                    for item in contract["indexes"]
                ]
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "verify-member"):
            request = core.load_json(args.input)
            if set(request) != {
                "lane",
                "image_result",
                "oci_archive",
                "task_id",
                "resolved_plan",
                "resolved_plan_sha256",
            }:
                raise ValueError(
                    "verify-member input requires image/archive/task/frozen plan"
                )
            if any(
                not isinstance(request[key], str)
                for key in (
                    "lane",
                    "image_result",
                    "oci_archive",
                    "task_id",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("verify-member paths must be strings")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            selected_task = registry.select_task(
                resolved_plan,
                task_kind="image",
                task_id=request["task_id"],
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            result = registry.publish_member(
                Path(request["oci_archive"]),
                image_result=core.load_json(Path(request["image_result"])),
                lane=request["lane"],
                selected_task=selected_task,
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "plan-index"):
            request = core.load_json(args.input)
            if set(request) != {
                "lane",
                "members",
                "member_statuses",
                "resolved_plan",
                "resolved_plan_sha256",
            } or not all(
                isinstance(request[key], str)
                for key in ("lane", "resolved_plan", "resolved_plan_sha256")
            ):
                raise ValueError(
                    "plan-index input requires lane/members/statuses/frozen plan"
                )
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            registry.resolved_registry_contract(
                resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            inventory = registry.inventory_registry(
                targets=[
                    {
                        "repository": family["target_repository"],
                        "tag": family["target_tag"],
                    }
                    for family in resolved_plan["family_tasks"]
                ]
            )
            result = registry.plan_indexes(
                request["members"],
                inventory,
                member_statuses=request["member_statuses"],
                lane=request["lane"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "verify-index"):
            request = core.load_json(args.input)
            if set(request) != {
                "lane",
                "parent_plans",
                "family_task_id",
                "resolved_plan",
                "resolved_plan_sha256",
            } or not all(
                isinstance(request[key], str)
                for key in (
                    "lane",
                    "family_task_id",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError(
                    "verify-index input requires lane/parent/family task/frozen plan"
                )
            parent = request["parent_plans"]
            if not isinstance(parent, dict) or not isinstance(
                parent.get("plans"), list
            ):
                raise ValueError("verify-index parent_plans is malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            registry.validate_index_plans(
                parent,
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            matches = [
                item
                for item in parent["plans"]
                if isinstance(item, dict)
                and item.get("family_task_id") == request["family_task_id"]
            ]
            if len(matches) != 1:
                raise ValueError(
                    "verify-index family task does not resolve exactly once"
                )
            result = registry.create_index(
                matches[0],
                parent_plans=parent,
                lane=request["lane"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "prepare-index"):
            request = core.load_json(args.input)
            if set(request) != {
                "lane",
                "parent_plans",
                "family_task_id",
                "resolved_plan",
                "resolved_plan_sha256",
            } or not all(
                isinstance(request[key], str)
                for key in (
                    "lane",
                    "family_task_id",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError(
                    "prepare-index input requires parent/family task/frozen plan"
                )
            parent = request["parent_plans"]
            if not isinstance(parent, dict) or not isinstance(
                parent.get("plans"), list
            ):
                raise ValueError("prepare-index parent_plans is malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            registry.validate_index_plans(
                parent,
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            matches = [
                item
                for item in parent["plans"]
                if isinstance(item, dict)
                and item.get("family_task_id") == request["family_task_id"]
            ]
            if len(matches) != 1:
                raise ValueError(
                    "prepare-index family task does not resolve exactly once"
                )
            result = registry.prepare_index(
                matches[0],
                parent_plans=parent,
                lane=request["lane"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "finalize-index"):
            request = core.load_json(args.input)
            if set(request) != {
                "parent_plans",
                "provisional",
                "family_task_id",
                "resolved_plan",
                "resolved_plan_sha256",
            } or not all(
                isinstance(request[key], str)
                for key in (
                    "parent_plans",
                    "provisional",
                    "family_task_id",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError(
                    "finalize-index input requires parent/provisional/frozen family"
                )
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            family_task = registry.select_task(
                resolved_plan,
                task_kind="family",
                task_id=request["family_task_id"],
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            parent = core.load_json(Path(request["parent_plans"]))
            registry.validate_index_plans(
                parent,
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            if (
                len(
                    [
                        item
                        for item in parent["plans"]
                        if item.get("family_task_id") == family_task["task_id"]
                    ]
                )
                != 1
            ):
                raise ValueError("finalize-index family task differs from parent plans")
            result = registry.finalize_index(
                core.load_json(Path(request["provisional"])),
                parent_plans=parent,
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            _write(args.output, result)
        elif (args.group, args.action) in {
            ("registry", "aggregate-authenticated"),
            ("registry", "aggregate-protected"),
        }:
            request = core.load_json(args.input)
            record_key = (
                "provisional_indexes"
                if args.action == "aggregate-authenticated"
                else "finalized_indexes"
            )
            if set(request) != {
                "member_records",
                "member_collection",
                record_key,
                "provisional_collection",
                "parent_plans",
                "source_sha",
                "resolved_plan",
                "resolved_plan_sha256",
                "run",
            }:
                raise ValueError("registry aggregation input fields are noncanonical")
            if (
                not isinstance(request["member_records"], list)
                or not isinstance(request[record_key], list)
                or any(
                    not isinstance(path, str)
                    for path in [
                        *request["member_records"],
                        *request[record_key],
                    ]
                )
                or not isinstance(request["parent_plans"], str)
                or not isinstance(request["member_collection"], str)
                or not isinstance(request["provisional_collection"], str)
                or not isinstance(request["resolved_plan"], str)
                or not isinstance(request["resolved_plan_sha256"], str)
            ):
                raise ValueError("registry aggregation paths are malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            registry.resolved_registry_contract(
                resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            kwargs = {
                "member_records": [
                    core.load_json(Path(path)) for path in request["member_records"]
                ],
                "member_collection": core.load_json(Path(request["member_collection"])),
                record_key: [
                    core.load_json(Path(path)) for path in request[record_key]
                ],
                "provisional_collection": core.load_json(
                    Path(request["provisional_collection"])
                ),
                "parent_plans": core.load_json(Path(request["parent_plans"])),
                "source_sha": request["source_sha"],
                "resolved_plan": resolved_plan,
                "expected_plan_sha256": request["resolved_plan_sha256"],
                "run": request["run"],
            }
            function = (
                verify.authenticated_registry_publication_evidence
                if args.action == "aggregate-authenticated"
                else verify.protected_registry_publication_evidence
            )
            result = function(**kwargs)
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "audit-operations"):
            request = core.load_json(args.input)
            if set(request) != {
                "lane",
                "operations",
                "resolved_plan",
                "resolved_plan_sha256",
            }:
                raise ValueError(
                    "audit-operations input requires the exact frozen plan binding"
                )
            resolved_plan, _ = _release_plan_binding(request)
            audit = verify.audit_operations(
                request["operations"],
                lane=request["lane"],
                staging_repository=resolved_plan["source"]["staging_repository"],
            )
            payload = {
                "schema_version": 1,
                "kind": "ucm-registry-operation-audit",
                "lane": request["lane"],
                **audit,
            }
            result = {**payload, "audit_sha256": core.sha256_value(payload)}
            _write(args.output, result)
        elif (args.group, args.action) == ("artifact", "validate-image-bridge"):
            request = core.load_json(args.input)
            if set(request) != {
                "source_sha",
                "task_id",
                "oci_artifact",
                "image_artifact",
                "hosted_task",
                "resolved_plan",
                "resolved_plan_sha256",
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "source_sha",
                    "task_id",
                    "oci_artifact",
                    "image_artifact",
                    "hosted_task",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("image bridge artifact input is malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            selected_task = registry.select_task(
                resolved_plan,
                task_kind="image",
                task_id=request["task_id"],
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            task = core.load_json(Path(request["hosted_task"]))
            if (
                not isinstance(task, dict)
                or task.get("source_sha") != request["source_sha"]
                or task.get("task_id") != request["task_id"]
                or not isinstance(task.get("source_date_epoch"), int)
                or isinstance(task.get("source_date_epoch"), bool)
            ):
                raise ValueError("image bridge hosted task is malformed")
            expected = verify.hosted_image_task(
                selected_task,
                request["source_sha"],
                task["source_date_epoch"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            if task != expected:
                raise ValueError("image bridge hosted task differs from authority")
            result = {
                "schema_version": 1,
                "kind": "ucm-image-artifact-bridge-validation",
                "task_id": selected_task["task_id"],
                "image_task_sha256": selected_task["task_sha256"],
                "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
                "oci_artifact": verify.validate_run_bound_artifact_name(
                    request["oci_artifact"],
                    f"ucm-internal-oci-{request['task_id']}",
                    request["run"],
                ),
                "image_artifact": verify.validate_run_bound_artifact_name(
                    request["image_artifact"], task["image_artifact"], request["run"]
                ),
            }
            _write(args.output, result)
        elif (args.group, args.action) == ("artifact", "validate-index-parent"):
            request = core.load_json(args.input)
            if set(request) != {
                "parent_plans",
                "parent_artifact",
                "source_sha",
                "resolved_plan",
                "resolved_plan_sha256",
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "parent_plans",
                    "parent_artifact",
                    "source_sha",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("index parent artifact input is malformed")
            artifact = verify.validate_run_bound_artifact_name(
                request["parent_artifact"],
                f"ucm-index-parent-{request['source_sha']}",
                request["run"],
            )
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            parent = registry.validate_index_plans(
                core.load_json(Path(request["parent_plans"])),
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            if parent["source_sha"] != request["source_sha"]:
                raise ValueError("index parent source differs from protected tag")
            result = {
                "schema_version": 1,
                "kind": "ucm-index-parent-artifact-validation",
                "parent_artifact": artifact,
                "source_sha": request["source_sha"],
                "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
                "plans_sha256": parent["plans_sha256"],
            }
            _write(args.output, result)
        elif (args.group, args.action) == ("artifact", "collect-members"):
            request = core.load_json(args.input)
            if set(request) != {
                "root",
                "output_dir",
                "source_sha",
                "resolved_plan",
                "resolved_plan_sha256",
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "root",
                    "output_dir",
                    "source_sha",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("member artifact collection input is malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            registry.validate_resolved_plan(resolved_plan)
            if (
                resolved_plan["resolved_plan_sha256"] != request["resolved_plan_sha256"]
                or resolved_plan["source"]["commit"] != request["source_sha"]
            ):
                raise ValueError("member collection differs from frozen plan")
            image_tasks = resolved_plan["image_tasks"]
            logical = [f"ucm-member-{task['task_id']}" for task in image_tasks]
            directories = verify.resolve_run_bound_artifact_directories(
                Path(request["root"]), logical, run=request["run"], label="member"
            )
            output_dir = _empty_output_dir(Path(request["output_dir"]))
            paths: list[str] = []
            preflight_sha256s: dict[str, str] = {}
            for task, name in zip(image_tasks, logical, strict=True):
                spec = task["spec_id"]
                directory = directories[name]
                expected_files = {
                    "member-record.json",
                    "member-audit.json",
                    "member-preflight.json",
                    "member-mutation-preflight.json",
                    "selected-task.json",
                    "upstream-drift.json",
                }
                if {path.name for path in directory.iterdir()} != expected_files:
                    raise ValueError("member artifact file set is noncanonical")
                source = directory / "member-record.json"
                record = registry.validate_member_record(
                    core.load_json(source),
                    resolved_plan=resolved_plan,
                    expected_plan_sha256=request["resolved_plan_sha256"],
                )
                if (
                    record["spec_id"] != spec
                    or core.load_json(directory / "selected-task.json") != task
                ):
                    raise ValueError("member artifact/spec mismatch")
                early_preflight = core.load_json(directory / "member-preflight.json")
                mutation_preflight = core.load_json(
                    directory / "member-mutation-preflight.json"
                )
                for preflight in (early_preflight, mutation_preflight):
                    if (
                        preflight.get("schema_version") != 1
                        or isinstance(preflight.get("schema_version"), bool)
                        or preflight.get("kind") != "ucm-tag-preflight"
                        or preflight.get("lane") != "protected-tag"
                        or preflight.get("source_sha") != request["source_sha"]
                        or preflight.get("publication_allowed") is not True
                        or preflight.get("write_authority")
                        != [
                            "github-prerelease",
                            "ghcr-final-index",
                            "ghcr-private-staging",
                        ]
                        or not isinstance(preflight.get("checks"), dict)
                        or not preflight["checks"]
                        or any(
                            value is not True for value in preflight["checks"].values()
                        )
                        or preflight.get("preflight_sha256")
                        != core.sha256_value(
                            {
                                key: value
                                for key, value in preflight.items()
                                if key != "preflight_sha256"
                            }
                        )
                    ):
                        raise ValueError("member protected preflight is invalid")
                if early_preflight != mutation_preflight:
                    raise ValueError("member mutation preflight changed unexpectedly")
                target = output_dir / f"{task['task_id']}.json"
                shutil.copyfile(source, target)
                paths.append(str(target))
                preflight_sha256s[task["task_id"]] = mutation_preflight[
                    "preflight_sha256"
                ]
            result = {
                "schema_version": 1,
                "kind": "ucm-member-artifact-collection",
                "source_sha": request["source_sha"],
                "resolved_plan_sha256": resolved_plan["resolved_plan_sha256"],
                "member_records": paths,
                "member_record_sha256s": {
                    task["task_id"]: registry.validate_member_record(
                        core.load_json(output_dir / f"{task['task_id']}.json"),
                        resolved_plan=resolved_plan,
                        expected_plan_sha256=request["resolved_plan_sha256"],
                    )["record_sha256"]
                    for task in image_tasks
                },
                "member_preflight_sha256s": preflight_sha256s,
            }
            result["collection_sha256"] = core.sha256_value(
                {key: value for key, value in result.items() if key != "member_records"}
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("artifact", "collect-provisionals"):
            request = core.load_json(args.input)
            if set(request) != {
                "root",
                "output_dir",
                "source_sha",
                "parent_plans",
                "resolved_plan",
                "resolved_plan_sha256",
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "root",
                    "output_dir",
                    "source_sha",
                    "parent_plans",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("provisional artifact collection input is malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            contract = registry.resolved_registry_contract(
                resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            if contract["source_sha"] != request["source_sha"]:
                raise ValueError(
                    "provisional collection source differs from frozen plan"
                )
            families = resolved_plan["family_tasks"]
            logical = [
                f"ucm-index-provisional-{family['task_id']}" for family in families
            ]
            directories = verify.resolve_run_bound_artifact_directories(
                Path(request["root"]),
                logical,
                run=request["run"],
                label="provisional index",
            )
            parent = core.load_json(Path(request["parent_plans"]))
            registry.validate_index_plans(
                parent,
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            output_dir = _empty_output_dir(Path(request["output_dir"]))
            paths: list[str] = []
            provisional_sha256s: dict[str, str] = {}
            preflight_sha256s: dict[str, str] = {}
            for family, name in zip(families, logical, strict=True):
                family_task_id = family["task_id"]
                parent_matches = [
                    item
                    for item in parent["plans"]
                    if item.get("family_task_id") == family_task_id
                ]
                if len(parent_matches) != 1:
                    raise ValueError(
                        "provisional family task is absent from parent plans"
                    )
                directory = directories[name]
                if {path.name for path in directory.iterdir()} != {
                    "provisional.json",
                    "preflight.json",
                }:
                    raise ValueError("provisional artifact file set is noncanonical")
                source = directory / "provisional.json"
                provisional = registry.validate_provisional_index(
                    core.load_json(source),
                    parent_plans=parent,
                    resolved_plan=resolved_plan,
                    expected_plan_sha256=request["resolved_plan_sha256"],
                )
                if (
                    provisional["family_id"] != parent_matches[0]["family_id"]
                    or provisional.get("family_task_id", family_task_id)
                    != family_task_id
                ):
                    raise ValueError("provisional artifact/family mismatch")
                preflight = core.load_json(directory / "preflight.json")
                if (
                    preflight.get("schema_version") != 1
                    or isinstance(preflight.get("schema_version"), bool)
                    or preflight.get("kind") != "ucm-tag-preflight"
                    or preflight.get("lane") != "protected-tag"
                    or preflight.get("source_sha") != request["source_sha"]
                    or preflight.get("publication_allowed") is not True
                    or preflight.get("preflight_sha256")
                    != provisional["preflight_sha256"]
                    or preflight.get("preflight_sha256")
                    != core.sha256_value(
                        {
                            key: value
                            for key, value in preflight.items()
                            if key != "preflight_sha256"
                        }
                    )
                ):
                    raise ValueError("provisional protected preflight is invalid")
                target = output_dir / f"{family_task_id}.json"
                shutil.copyfile(source, target)
                paths.append(str(target))
                provisional_sha256s[family_task_id] = provisional["provisional_sha256"]
                preflight_sha256s[family_task_id] = preflight["preflight_sha256"]
            result = {
                "schema_version": 1,
                "kind": "ucm-provisional-artifact-collection",
                "source_sha": request["source_sha"],
                "resolved_plan_sha256": request["resolved_plan_sha256"],
                "parent_plans_sha256": parent["plans_sha256"],
                "provisional_indexes": paths,
                "provisional_sha256s": provisional_sha256s,
                "provisional_preflight_sha256s": preflight_sha256s,
            }
            result["collection_sha256"] = core.sha256_value(
                {
                    key: value
                    for key, value in result.items()
                    if key != "provisional_indexes"
                }
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "assets-manifest"):
            request = core.load_json(args.input)
            if set(request) != {
                "wheel_dir",
                "chart_result",
                "chart_package",
                "output_dir",
                "source_sha",
                "resolved_plan",
                "resolved_plan_sha256",
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "wheel_dir",
                    "chart_result",
                    "chart_package",
                    "output_dir",
                    "source_sha",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("release assets-manifest input is malformed")
            resolved_plan = core.load_json(Path(request["resolved_plan"]))
            registry.resolved_registry_contract(
                resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
            )
            result = verify.build_release_asset_manifest(
                wheel_dir=Path(request["wheel_dir"]),
                chart_result_path=Path(request["chart_result"]),
                chart_package_path=Path(request["chart_package"]),
                output_dir=Path(request["output_dir"]),
                source_sha=request["source_sha"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=request["resolved_plan_sha256"],
                run=request["run"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "plan-state"):
            request = core.load_json(args.input)
            if set(request) != {
                "remote",
                "source_sha",
                "just_created",
                "resolved_plan",
                "resolved_plan_sha256",
            }:
                raise ValueError("release plan-state input fields are noncanonical")
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.plan_github_release(
                request["remote"],
                request["source_sha"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
                just_created=request["just_created"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "select-pages"):
            request = core.load_json(args.input)
            if set(request) != {
                "pages",
                "source_sha",
                "resolved_plan",
                "resolved_plan_sha256",
            } or not all(isinstance(request[key], str) for key in request):
                raise ValueError("release select-pages input is malformed")
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.select_github_release_pages(
                core.load_json_array(Path(request["pages"])),
                request["source_sha"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "plan-downloads"):
            request = core.load_json(args.input)
            if set(request) != {
                "manifest",
                "raw_assets",
                "release",
                "source_sha",
                "release_id",
                "allowed_root",
                "require_complete",
                "resolved_plan",
                "resolved_plan_sha256",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "manifest",
                    "raw_assets",
                    "release",
                    "source_sha",
                    "allowed_root",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("release plan-downloads input is malformed")
            release_state = _release_asset_state(request)
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.plan_release_asset_downloads(
                core.load_json(Path(request["manifest"])),
                core.load_json_array(Path(request["raw_assets"])),
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
                require_complete=request["require_complete"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
                asset_download_slug=release_state["asset_download_slug"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "complete-downloads"):
            request = core.load_json(args.input)
            if set(request) != {"download_plan", "download_root"} or any(
                not isinstance(request[key], str) for key in request
            ):
                raise ValueError("release complete-downloads input is malformed")
            result = verify.complete_release_asset_downloads(
                core.load_json(Path(request["download_plan"])),
                Path(request["download_root"]),
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "refresh-assets"):
            request = core.load_json(args.input)
            if set(request) != {
                "manifest",
                "prior_assets",
                "raw_assets",
                "prior_release",
                "release",
                "source_sha",
                "release_id",
                "allowed_root",
                "resolved_plan",
                "resolved_plan_sha256",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "manifest",
                    "prior_assets",
                    "raw_assets",
                    "prior_release",
                    "release",
                    "source_sha",
                    "allowed_root",
                    "resolved_plan",
                    "resolved_plan_sha256",
                )
            ):
                raise ValueError("release refresh-assets input is malformed")
            prior_state = _release_asset_state(request, release_key="prior_release")
            release_state = _release_asset_state(request)
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.refresh_release_asset_metadata(
                core.load_json(Path(request["manifest"])),
                core.load_json_array(Path(request["prior_assets"])),
                core.load_json_array(Path(request["raw_assets"])),
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
                prior_asset_download_slug=prior_state["asset_download_slug"],
                asset_download_slug=release_state["asset_download_slug"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "verify-upload-prefix"):
            request = core.load_json(args.input)
            path_keys = {
                "manifest",
                "initial_asset_plan",
                "uploaded_assets",
                "current_assets",
                "allowed_root",
                "resolved_plan",
                "resolved_plan_sha256",
            }
            if set(request) != path_keys | {
                "next_name",
                "release_id",
                "release",
                "source_sha",
            } or any(
                not isinstance(request[key], str)
                for key in path_keys | {"next_name", "release", "source_sha"}
            ):
                raise ValueError("release verify-upload-prefix input is malformed")
            release_state = _release_asset_state(request)
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            if release_state["decision"] != "resume-draft":
                raise ValueError("release upload prefix requires a live draft")
            result = verify.verify_release_upload_prefix(
                core.load_json(Path(request["manifest"])),
                core.load_json(Path(request["initial_asset_plan"])),
                core.load_json_array(Path(request["uploaded_assets"])),
                core.load_json_array(Path(request["current_assets"])),
                next_name=request["next_name"],
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
                asset_download_slug=release_state["asset_download_slug"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "record-upload-response"):
            request = core.load_json(args.input)
            path_keys = {
                "manifest",
                "raw_response",
                "allowed_root",
                "resolved_plan",
                "resolved_plan_sha256",
            }
            if set(request) != path_keys | {
                "expected_name",
                "release_id",
                "release",
                "source_sha",
            } or any(
                not isinstance(request[key], str)
                for key in path_keys | {"expected_name", "release", "source_sha"}
            ):
                raise ValueError("release record-upload-response input is malformed")
            release_state = _release_asset_state(request)
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            if release_state["decision"] != "resume-draft":
                raise ValueError("release upload response requires a live draft")
            result = verify.record_release_upload_response(
                core.load_json(Path(request["manifest"])),
                core.load_json(Path(request["raw_response"])),
                expected_name=request["expected_name"],
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
                asset_download_slug=release_state["asset_download_slug"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "rebase-manifest"):
            request = core.load_json(args.input)
            if set(request) != {
                "manifest",
                "allowed_root",
                "resolved_plan",
                "resolved_plan_sha256",
            } or any(not isinstance(request[key], str) for key in request):
                raise ValueError("release rebase-manifest input is malformed")
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.rebase_release_asset_manifest(
                core.load_json(Path(request["manifest"])),
                allowed_root=Path(request["allowed_root"]),
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "operation-ledger"):
            request = core.load_json(args.input)
            path_keys = {
                "prepare_initial_plan",
                "initial_release",
                "initial_asset_plan",
                "authenticated_assets",
                "asset_manifest",
                "upload_transcript",
                "allowed_root",
                "resolved_plan",
                "resolved_plan_sha256",
            }
            if set(request) != path_keys | {"source_sha"} or any(
                not isinstance(request[key], str) for key in path_keys | {"source_sha"}
            ):
                raise ValueError("release operation-ledger input is malformed")
            asset_manifest = _release_asset_manifest(
                request, manifest_key="asset_manifest"
            )
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.build_github_release_operation_ledger(
                prepare_initial_plan=core.load_json(
                    Path(request["prepare_initial_plan"])
                ),
                initial_release=core.load_json(Path(request["initial_release"])),
                initial_asset_plan=core.load_json(Path(request["initial_asset_plan"])),
                authenticated_assets=core.load_json_array(
                    Path(request["authenticated_assets"])
                ),
                upload_transcript=verify.validate_release_upload_transcript(
                    asset_manifest,
                    core.load_json(Path(request["initial_asset_plan"])),
                    core.load_json_array(Path(request["upload_transcript"])),
                    source_sha=request["source_sha"],
                    release_id=core.load_json(Path(request["initial_asset_plan"]))[
                        "release_id"
                    ],
                    allowed_root=Path(request["allowed_root"]),
                    resolved_plan=resolved_plan,
                    expected_plan_sha256=plan_sha256,
                ),
                source_sha=request["source_sha"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
            )
            _write(args.output, result)
        elif (args.group, args.action) in {
            ("release", "plan-assets"),
            ("release", "verify-assets"),
        }:
            request = core.load_json(args.input)
            base_keys = {
                "manifest",
                "observed_assets",
                "release",
                "source_sha",
                "release_id",
                "allowed_root",
                "resolved_plan",
                "resolved_plan_sha256",
            }
            expected_keys = (
                base_keys | {"release_published"}
                if args.action == "plan-assets"
                else base_keys
            )
            if set(request) != expected_keys or any(
                not isinstance(request[key], str)
                for key in (
                    "manifest",
                    "observed_assets",
                    "release",
                    "source_sha",
                    "allowed_root",
                )
            ):
                raise ValueError(f"release {args.action} input fields are noncanonical")
            release_state = _release_asset_state(request)
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            kwargs = {
                "release_id": request["release_id"],
                "allowed_root": Path(request["allowed_root"]),
                "asset_download_slug": release_state["asset_download_slug"],
                "resolved_plan": resolved_plan,
                "expected_plan_sha256": plan_sha256,
            }
            if args.action == "plan-assets":
                if request["release_published"] != (
                    release_state["decision"] == "inspect-published-prerelease"
                ):
                    raise ValueError("release asset phase differs from live Release")
                result = verify.plan_release_assets(
                    core.load_json(Path(request["manifest"])),
                    core.load_json_array(Path(request["observed_assets"])),
                    release_published=request["release_published"],
                    **kwargs,
                )
            else:
                result = verify.verify_release_assets(
                    core.load_json(Path(request["manifest"])),
                    core.load_json_array(Path(request["observed_assets"])),
                    **kwargs,
                )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "publication-evidence"):
            request = core.load_json(args.input)
            path_keys = {
                "protected_registry",
                "asset_manifest",
                "allowed_root",
                "prepare_initial_plan",
                "prepare_release",
                "initial_release",
                "initial_assets",
                "initial_asset_plan",
                "upload_transcript",
                "prepublish_release",
                "prepublish_assets",
                "authenticated_release",
                "authenticated_assets",
                "anonymous_release",
                "anonymous_assets",
                "operations",
                "resolved_plan",
                "resolved_plan_sha256",
            }
            if set(request) != path_keys | {"source_sha", "run"} or any(
                not isinstance(request[key], str) for key in path_keys
            ):
                raise ValueError("release publication-evidence input is malformed")
            asset_manifest = _release_asset_manifest(
                request, manifest_key="asset_manifest"
            )
            resolved_plan, plan_sha256 = _release_plan_binding(request)
            result = verify.github_release_publication_evidence(
                protected_registry=core.load_json(Path(request["protected_registry"])),
                asset_manifest=asset_manifest,
                allowed_root=Path(request["allowed_root"]),
                prepare_initial_plan=core.load_json(
                    Path(request["prepare_initial_plan"])
                ),
                prepare_release=core.load_json(Path(request["prepare_release"])),
                initial_release=core.load_json(Path(request["initial_release"])),
                initial_assets=core.load_json_array(Path(request["initial_assets"])),
                initial_asset_plan=core.load_json(Path(request["initial_asset_plan"])),
                upload_transcript=core.load_json_array(
                    Path(request["upload_transcript"])
                ),
                prepublish_release=core.load_json(Path(request["prepublish_release"])),
                prepublish_assets=core.load_json_array(
                    Path(request["prepublish_assets"])
                ),
                authenticated_release=core.load_json(
                    Path(request["authenticated_release"])
                ),
                authenticated_assets=core.load_json_array(
                    Path(request["authenticated_assets"])
                ),
                anonymous_release=core.load_json(Path(request["anonymous_release"])),
                anonymous_assets=core.load_json_array(
                    Path(request["anonymous_assets"])
                ),
                operations=core.load_json_array(Path(request["operations"])),
                source_sha=request["source_sha"],
                resolved_plan=resolved_plan,
                expected_plan_sha256=plan_sha256,
                run=request["run"],
            )
            _write(args.output, result)
        elif args.group == "fixture-reconcile":
            request = core.load_json(args.input)
            if set(request) != {"candidate", "inventory"}:
                raise ValueError(
                    "reconcile input requires exactly candidate and inventory"
                )
            result = registry.reconcile_fixture_candidate(
                request["candidate"], request["inventory"]
            )
        elif (args.group, args.action) == ("loop", "verify"):
            result = verify.verify_loop(
                core.load_json(args.input),
                run={"id": args.run_id, "attempt": args.attempt},
            )
        elif (args.group, args.action) == ("loop", "prepare"):
            prepared = verify.prepare_candidate_loop(
                core.load_json(args.build_record),
                core.load_json(args.wheel_inspection),
                source_sha=args.source_sha,
                run={"id": args.run_id, "attempt": args.attempt},
            )
            output_dir = _empty_output_dir(args.output_dir)
            documents = {
                "prepared-loop.json": prepared,
                "image-input.json": prepared["image_input"],
                "candidate.json": prepared["candidate"],
                "first-reconcile.json": prepared["first_reconcile"],
                "loop-verification.json": prepared["loop_verification"],
            }
            for filename, value in documents.items():
                _write(output_dir / filename, value)
            first_sha256 = core.sha256_value(prepared["first_reconcile"])
            (output_dir / "first-reconcile.sha256").write_text(
                first_sha256 + "\n", encoding="utf-8"
            )
            result = {
                "image_input": str(output_dir / "image-input.json"),
                "candidate_build_key_sha256": prepared["candidate"]["build_key_sha256"],
                "first_reconcile_sha256": first_sha256,
                "upstream_index_digest": prepared["candidate"]["build_inputs"][
                    "upstream"
                ]["index_digest"],
                "loop_payload_sha256": prepared["loop_verification"]["payload_sha256"],
            }
        elif (args.group, args.action) == ("loop", "complete"):
            completed = verify.complete_candidate_loop(
                core.load_json(args.prepared),
                core.load_json(args.image_result),
                source_sha=args.source_sha,
                run={"id": args.run_id, "attempt": args.attempt},
            )
            output_dir = _empty_output_dir(args.output_dir)
            _write(output_dir / "completed-loop.json", completed)
            _write(output_dir / "second-reconcile.json", completed["second_reconcile"])
            _write(output_dir / "vllm-loop-evidence.json", completed["evidence"])
            result = {
                "loop_payload_sha256": completed["evidence"]["payload_sha256"],
                "image_result_sha256": completed["evidence"]["payload"][
                    "image_result_sha256"
                ],
                "oci_digest": completed["evidence"]["payload"]["oci_digest"],
                "second_task_count": completed["second_reconcile"]["task_count"],
            }
        elif (args.group, args.action) == ("loop", "aggregate"):
            evidence = verify.aggregate_release_evidence(
                build_record_path=args.build_record,
                wheel_record_path=args.wheel_inspection,
                wheel_path=args.wheel,
                chart_result_path=args.chart_result,
                chart_package_path=args.chart_package,
                image_result_path=args.image_result,
                oci_evidence_dir=args.oci_evidence_dir,
                image_recipe_path=args.image_recipe,
                image_metadata_path=args.image_metadata,
                image_prepare_path=args.image_prepare,
                buildkit_metadata_path=args.buildkit_metadata,
                image_archive_sha256_path=args.image_archive_sha256,
                completed_loop_path=args.completed_loop,
                second_reconcile_path=args.second_reconcile,
                image_loop_path=args.image_loop,
                repository=args.repository,
                ref=args.ref,
                source_sha=args.source_sha,
                resolved_plan=core.load_json(args.resolved_plan),
                expected_plan_sha256=args.expected_plan_sha256,
                run={"run_id": args.run_id, "run_attempt": args.attempt},
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, evidence)
            result = {
                "output": str(args.output),
                "payload_sha256": evidence["payload_sha256"],
            }
        elif (args.group, args.action) == ("loop", "aggregate-real"):
            evidence = verify.aggregate_real_hosted_evidence(
                wheel_dir=args.wheel_dir,
                image_dir=args.image_dir,
                chart_result_path=args.chart_result,
                chart_package_path=args.chart_package,
                repository=args.repository,
                ref=args.ref,
                source_sha=args.source_sha,
                resolved_plan=core.load_json(args.resolved_plan),
                expected_plan_sha256=args.expected_plan_sha256,
                wheel_matrix=core.load_json(args.selected_wheel_matrix),
                image_matrix=core.load_json(args.selected_image_matrix),
                run={"run_id": args.run_id, "run_attempt": args.attempt},
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, evidence)
            if args.output_dir is not None:
                output_dir = _empty_output_dir(args.output_dir)
                payload = evidence["payload"]
                _write(output_dir / "family-plans.json", payload["families"])
                _write(
                    output_dir / "candidate-inventory.json",
                    payload["candidate_inventory"],
                )
                _write(
                    output_dir / "second-reconcile.json",
                    payload["second_reconcile"],
                )
            result = {
                "output": str(args.output),
                "payload_sha256": evidence["payload_sha256"],
                "family_count": len(evidence["payload"]["families"]),
                "wheel_count": len(evidence["payload"]["wheels"]),
                "image_count": len(evidence["payload"]["images"]),
                "second_task_count": evidence["payload"]["second_reconcile"][
                    "task_count"
                ],
            }
        elif (args.group, args.action) == ("image", "base-authority"):
            result = image.fixture_base_authority()
        elif (args.group, args.action) == ("image", "toolchain-authority"):
            result = image.fixture_image_toolchain_authority()
        elif (args.group, args.action) == ("image", "task-toolchain-authority"):
            result = image.task_toolchain_authority(
                core.load_json(args.resolved_plan),
                task_kind=args.task_kind,
                task_id=args.task_id,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        elif (args.group, args.action) == ("image", "verify"):
            result = image.verify_oci(
                args.context,
                args.oci,
                schema_dir=args.schema_dir,
                evidence_dir=args.evidence_dir,
                output_mode=args.output_mode,
                resolved_plan=(
                    core.load_json(args.resolved_plan)
                    if args.resolved_plan is not None
                    else None
                ),
                expected_plan_sha256=args.expected_plan_sha256,
                task_id=args.task_id,
            )
        elif (args.group, args.action) == ("image", "prepare"):
            result = image.prepare_context_bundle(
                core.load_json(args.input),
                wheel_dir=args.wheel_dir,
                expected_source_sha=args.expected_source_sha,
                base_authority=core.load_json(args.base_authority),
                base_index_path=args.base_index,
                base_manifest_path=args.base_manifest,
                base_config_path=args.base_config,
                output_dir=args.output_dir,
            )
        elif (args.group, args.action) == ("image", "real-authorities"):
            result = image.real_image_authority_from_plan(
                core.load_json(args.resolved_plan),
                task_id=args.task_id,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        elif (args.group, args.action) == ("image", "base-record-real"):
            result = image.real_base_record_from_files(
                index_path=args.index,
                manifest_path=args.manifest,
                config_path=args.config,
                task_authority=core.load_json(args.task_authority),
            )
        elif (args.group, args.action) == ("image", "prepare-real"):
            result = image.prepare_real_context(
                wheel_path=args.wheel,
                wheel_inspection=core.load_json(args.wheel_inspection),
                base_record=core.load_json(args.base_record),
                runtime_dependency_paths=args.runtime_wheel,
                output_dir=args.output_dir,
                schema_dir=args.schema_dir,
                task_authority=core.load_json(args.task_authority),
            )
        else:  # pragma: no cover - argparse owns this branch.
            parser.error("unsupported command")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as error:
        parser.exit(2, f"error: {error}\n")
    print(_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
