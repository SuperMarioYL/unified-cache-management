"""Command-line interface for the compact UCM release package."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

from . import core, registry, verify

catalog_resolution = registry


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    parser.add_argument("--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR)


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

    registry_parser = groups.add_parser("registry")
    registry_actions = registry_parser.add_subparsers(dest="action", required=True)
    for action in (
        "verify-member",
        "audit-operations",
    ):
        command = registry_actions.add_parser(action)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    validate_member = registry_actions.add_parser("validate-member-schema")
    validate_member.add_argument("--input", type=Path, required=True)
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
