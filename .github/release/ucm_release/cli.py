"""Command-line interface for the compact UCM release package."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

from . import chart, core, image, registry, verify, wheel

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


def _gh_release_api(
    path: str,
    *,
    method: str | None = None,
    input_bytes: bytes | None = None,
    content_type: str | None = None,
    allow_missing: bool = False,
) -> dict[str, object] | None:
    """Invoke ``gh api`` and return the decoded JSON response."""
    cmd = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
    ]
    if content_type is not None:
        cmd += ["-H", f"Content-Type: {content_type}"]
    if method is not None:
        cmd += ["--method", method]
    cmd.append(path)
    if input_bytes is not None:
        cmd += ["--input", "-"]
    completed = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if completed.returncode != 0:
        message = completed.stderr.decode(encoding="utf-8", errors="replace").strip()
        if allow_missing and "not found" in message.lower():
            return None
        raise ValueError(f"gh api {path} failed: {message}")
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {}


def _publish_github_release(args) -> dict[str, object]:
    """Create-or-reuse a draft release (draft) then upload and publish it (finalize)."""
    plan = core.load_json(args.plan)
    if plan["resolved_plan_sha256"] != args.plan_sha256:
        raise ValueError("resolved plan hash differs from expected plan hash")
    repository = plan["source"]["repository"]
    if repository != args.repository:
        raise ValueError("plan repository differs from expected repository")
    tag = plan["source"]["release_tag"]
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    api_root = f"repos/{repository}"
    if args.stage == "draft":
        operations = [
            {
                "type": "github-release-read",
                "capability": "read",
                "reference": f"https://api.github.com/{api_root}/releases/tags/{tag}",
                "authenticated": True,
            }
        ]
        existing = _gh_release_api(
            f"{api_root}/releases/tags/{tag}", allow_missing=True
        )
        if existing is not None:
            release = existing
            just_created = False
        else:
            body = {
                "tag_name": tag,
                "target_commitish": args.source_sha,
                "name": f"UCM {tag}",
                "body": (
                    f"Protected UCM {tag} release from reviewed source commit "
                    f"{args.source_sha}. Frozen plan "
                    f"{plan['resolved_plan_sha256']}."
                ),
                "draft": True,
                "prerelease": True,
                "make_latest": "false",
            }
            release = _gh_release_api(
                f"{api_root}/releases",
                method="POST",
                input_bytes=core.canonical_bytes(body),
            )
            just_created = True
            operations.append(
                {
                    "type": "github-release-create",
                    "capability": "write",
                    "reference": f"https://api.github.com/{api_root}/releases",
                    "authenticated": True,
                }
            )
        state = {
            "release_id": release["id"],
            "tag": tag,
            "draft": True,
            "just_created": just_created,
        }
        _write(output_dir / "release-state.json", state)
        _write(output_dir / "operations.json", operations)
        return {"kind": "ucm-github-release-draft", "stage": "draft", **state}
    draft_state = core.load_json(output_dir / "release-state.json")
    release_id = draft_state["release_id"]
    operations = [
        {
            "type": "github-release-read",
            "capability": "read",
            "reference": f"https://api.github.com/{api_root}/releases/{release_id}",
            "authenticated": True,
        }
    ]
    artifacts_dir = output_dir / "artifacts"
    if artifacts_dir.is_dir():
        for asset in sorted(artifacts_dir.iterdir()):
            if not asset.is_file():
                continue
            encoded = urllib.parse.quote(asset.name, safe="")
            _gh_release_api(
                f"https://uploads.github.com/{api_root}/releases/"
                f"{release_id}/assets?name={encoded}",
                method="POST",
                input_bytes=asset.read_bytes(),
                content_type="application/octet-stream",
            )
            operations.append(
                {
                    "type": "github-release-asset-upload",
                    "capability": "write",
                    "reference": (
                        f"https://uploads.github.com/{api_root}/releases/"
                        f"{release_id}/assets"
                    ),
                    "authenticated": True,
                }
            )
    publish_body = {"draft": False, "prerelease": True, "make_latest": "false"}
    _gh_release_api(
        f"{api_root}/releases/{release_id}",
        method="PATCH",
        input_bytes=core.canonical_bytes(publish_body),
    )
    operations.append(
        {
            "type": "github-release-publish",
            "capability": "write",
            "reference": f"https://api.github.com/{api_root}/releases/{release_id}",
            "authenticated": True,
        }
    )
    final = {
        "release_id": release_id,
        "tag": tag,
        "draft": False,
        "just_created": False,
    }
    _write(output_dir / "release-state.json", final)
    _write(output_dir / "operations.json", operations)
    return {"kind": "ucm-github-release-finalize", "stage": "finalize", **final}


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

    publish_parser = groups.add_parser("publish")
    publish_actions = publish_parser.add_subparsers(dest="action", required=True)
    publish_plan = publish_actions.add_parser("plan")
    publish_plan.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    publish_plan.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    publish_plan.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    publish_plan.add_argument("--repository", default=None)
    publish_plan.add_argument(
        "--lane", choices=("feature-candidate", "protected-tag"), required=True
    )
    publish_plan.add_argument("--allow", required=True)
    publish_plan.add_argument("--request", default="")
    publish_plan.add_argument("--dry-run", default="true")
    publish_plan.add_argument("--output", type=Path, required=True)
    publish_github_release = publish_actions.add_parser("github-release")
    publish_github_release.add_argument("--plan", type=Path, required=True)
    publish_github_release.add_argument("--plan-sha256", required=True)
    publish_github_release.add_argument("--repository", required=True)
    publish_github_release.add_argument(
        "--stage", choices=("draft", "finalize"), required=True
    )
    publish_github_release.add_argument("--output-dir", type=Path, required=True)
    publish_github_release.add_argument("--source-sha", required=True)

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
    context = wheel_actions.add_parser("context")
    context.add_argument("--source-sha", required=True)
    context.add_argument("--output-dir", required=True, type=Path)

    chart_parser = groups.add_parser("chart")
    chart_actions = chart_parser.add_subparsers(dest="action", required=True)
    package = chart_actions.add_parser("package")
    package.add_argument("--output-dir", type=Path, required=True)
    package.add_argument("--resolved-plan", type=Path, required=True)
    package.add_argument("--expected-plan-sha256", required=True)
    _paths(package)

    registry_parser = groups.add_parser("registry")
    registry_actions = registry_parser.add_subparsers(dest="action", required=True)
    for action in (
        "verify-member",
        "plan-index",
        "prepare-index",
        "finalize-index",
        "aggregate-authenticated",
        "aggregate-protected",
        "audit-operations",
    ):
        command = registry_actions.add_parser(action)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    validate_member = registry_actions.add_parser("validate-member-schema")
    validate_member.add_argument("--input", type=Path, required=True)

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

    loop_parser = groups.add_parser("loop")
    loop_actions = loop_parser.add_subparsers(dest="action", required=True)
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
        elif (args.group, args.action) == ("publish", "plan"):
            release = core.load_catalog(
                args.catalog,
                args.schema_dir,
                repository_root=args.repository_root,
                repository=args.repository,
            )
            plan = core.compute_publish_plan(
                release,
                lane=args.lane,
                allow=json.loads(args.allow),
                request=args.request,
                dry_run=args.dry_run.strip().lower() == "true",
            )
            payload = json.dumps(plan, sort_keys=True, separators=(",", ":"))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
            result = {"kind": "ucm-publish-plan", "schema_version": 1, "publish": plan}
        elif (args.group, args.action) == ("publish", "github-release"):
            result = _publish_github_release(args)
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
        elif (args.group, args.action) == ("wheel", "context"):
            result = wheel.prepare_source_context(args.output_dir, args.source_sha)
        elif (args.group, args.action) == ("chart", "package"):
            result = chart.package_chart(
                args.output_dir,
                resolved_plan=core.load_json(args.resolved_plan),
                expected_plan_sha256=args.expected_plan_sha256,
            )
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
        elif (args.group, args.action) == ("registry", "validate-member-schema"):
            record = core.load_json(args.input)
            schema = core.load_json(
                core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json"
            )
            core.validate_schema(
                record, schema["$defs"]["registryMemberRecord"], root=schema
            )
            result = {"kind": "ucm-member-schema-validation", "valid": True}
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
