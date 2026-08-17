# fmt: off
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

from . import core, registry, wheel

catalog_resolution = registry


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    parser.add_argument("--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR)


def _write(path: Path, value: object) -> None:
    path.write_bytes(core.canonical_bytes(value) + b"\n")


def _gh_release_api(
    path: str,
    *,
    method: str | None = None,
    input_bytes: bytes | None = None,
    content_type: str | None = None,
    allow_missing: bool = False,
) -> dict[str, object] | None:
    cmd = ['gh', 'api', '-H', 'Accept: application/vnd.github+json', '-H', 'X-GitHub-Api-Version: 2022-11-28']  # fmt: skip  # noqa: E501
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
    plan = core.load_json(args.plan)
    if plan["resolved_plan_sha256"] != args.plan_sha256:
        raise ValueError("resolved plan hash differs from expected plan hash")
    repository = plan["source"]["repository"]
    if repository != args.repository: raise ValueError('plan repository differs from expected repository')  # noqa: E701,E501
    tag = plan["source"]["release_tag"]
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    api_root = f"repos/{repository}"
    if args.stage == "draft":
        operations = [{'type': 'github-release-read', 'capability': 'read', 'reference': f'https://api.github.com/{api_root}/releases/tags/{tag}', 'authenticated': True}]  # fmt: skip  # noqa: E501
        existing = _gh_release_api(f'{api_root}/releases/tags/{tag}', allow_missing=True)  # fmt: skip  # noqa: E501
        if existing is not None:
            release = existing
            just_created = False
        else:
            body = {'tag_name': tag, 'target_commitish': args.source_sha, 'name': f'UCM {tag}', 'body': f"Protected UCM {tag} release from reviewed source commit {args.source_sha}. Frozen plan {plan['resolved_plan_sha256']}.", 'draft': True, 'prerelease': True, 'make_latest': 'false'}  # fmt: skip  # noqa: E501
            release = _gh_release_api(f'{api_root}/releases', method='POST', input_bytes=core.canonical_bytes(body))  # fmt: skip  # noqa: E501
            just_created = True
            operations.append({'type': 'github-release-create', 'capability': 'write', 'reference': f'https://api.github.com/{api_root}/releases', 'authenticated': True})  # fmt: skip  # noqa: E501
        state = {'release_id': release['id'], 'tag': tag, 'draft': True, 'just_created': just_created}  # fmt: skip  # noqa: E501
        _write(output_dir / "release-state.json", state)
        _write(output_dir / "operations.json", operations)
        return {"kind": "ucm-github-release-draft", "stage": "draft", **state}
    draft_state = core.load_json(output_dir / "release-state.json")
    release_id = draft_state["release_id"]
    operations = [{'type': 'github-release-read', 'capability': 'read', 'reference': f'https://api.github.com/{api_root}/releases/{release_id}', 'authenticated': True}]  # fmt: skip  # noqa: E501
    artifacts_dir = output_dir / "artifacts"
    if artifacts_dir.is_dir():
        for asset in sorted(artifacts_dir.iterdir()):
            if not asset.is_file():
                continue
            encoded = urllib.parse.quote(asset.name, safe="")
            _gh_release_api(f'https://uploads.github.com/{api_root}/releases/{release_id}/assets?name={encoded}', method='POST', input_bytes=asset.read_bytes(), content_type='application/octet-stream')  # fmt: skip  # noqa: E501
            operations.append({'type': 'github-release-asset-upload', 'capability': 'write', 'reference': f'https://uploads.github.com/{api_root}/releases/{release_id}/assets', 'authenticated': True})  # fmt: skip  # noqa: E501
    publish_body = {"draft": False, "prerelease": True, "make_latest": "false"}
    _gh_release_api(f'{api_root}/releases/{release_id}', method='PATCH', input_bytes=core.canonical_bytes(publish_body))  # fmt: skip  # noqa: E501
    operations.append({'type': 'github-release-publish', 'capability': 'write', 'reference': f'https://api.github.com/{api_root}/releases/{release_id}', 'authenticated': True})  # fmt: skip  # noqa: E501
    final = {'release_id': release_id, 'tag': tag, 'draft': False, 'just_created': False}  # fmt: skip  # noqa: E501
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
    validate.set_defaults(func=lambda a: {'schema_version': 1, 'wheel_profiles': len(core.load_catalog(a.release, a.schema_dir)['wheel_profiles']), 'compatibility_rules': len(core.load_catalog(a.release, a.schema_dir)['compatibility']['rules'])})  # fmt: skip  # noqa: E501

    catalog_parser = groups.add_parser("catalog")
    catalog_actions = catalog_parser.add_subparsers(dest="action", required=True)
    catalog_validate = catalog_actions.add_parser("validate")
    catalog_validate.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    catalog_validate.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501
    catalog_validate.add_argument('--repository-root', type=Path, default=core.REPO_ROOT)  # fmt: skip  # noqa: E501
    catalog_validate.set_defaults(func=lambda a: {'kind': 'ucm-catalog-validation', 'schema_version': 1, 'config_sha256': core.sha256_value((lambda r: (catalog_resolution.validate_catalog_tag_grammar(r), r)[1])(core.load_catalog(a.catalog, a.schema_dir, repository_root=a.repository_root))), 'upstream_products': len(core.load_catalog(a.catalog, a.schema_dir, repository_root=a.repository_root)['upstream_products']), 'compatibility_rules': len(core.load_catalog(a.catalog, a.schema_dir, repository_root=a.repository_root)['compatibility']['rules'])})  # fmt: skip  # noqa: E501

    catalog_resolve = catalog_actions.add_parser("resolve")
    catalog_resolve.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    catalog_resolve.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501
    catalog_resolve.add_argument('--lane', choices=('feature-candidate', 'protected-tag'), required=True)  # fmt: skip  # noqa: E501
    catalog_resolve.add_argument("--source-sha", required=True)
    catalog_resolve.add_argument("--fixture", type=Path)
    catalog_resolve.add_argument("--output", type=Path, required=True)

    def _cmd_resolve(a):
        release = core.load_catalog(a.catalog, a.schema_dir)
        fixture = core.load_json(a.fixture) if a.fixture else None
        result = catalog_resolution.resolve_catalog(release, source_sha=a.source_sha, lane=a.lane, fixture=fixture)  # fmt: skip  # noqa: E501
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result
    catalog_resolve.set_defaults(func=_cmd_resolve)

    recipe_matrix = catalog_actions.add_parser("recipe-matrix")
    recipe_matrix.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    recipe_matrix.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501
    recipe_matrix.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    recipe_matrix.add_argument('--lane', choices=('pr-smoke', 'hardware-e2e', 'manual', 'formal-release'), required=True)  # fmt: skip  # noqa: E501
    recipe_matrix.add_argument("--output", type=Path, required=True)

    def _cmd_recipe_matrix(a):
        release = core.load_catalog(a.catalog, a.schema_dir, repository_root=a.repository_root)  # fmt: skip  # noqa: E501
        result = core.repository_recipe_matrix(release, lane=a.lane, repository_root=a.repository_root)  # fmt: skip  # noqa: E501
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result
    recipe_matrix.set_defaults(func=_cmd_recipe_matrix)

    select_recipe = catalog_actions.add_parser("select-recipe")
    select_recipe.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    select_recipe.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501
    select_recipe.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    select_recipe.add_argument('--lane', choices=('pr-smoke', 'hardware-e2e', 'manual', 'formal-release'), required=True)  # fmt: skip  # noqa: E501
    select_recipe.add_argument("--task-id", required=True)
    select_recipe.add_argument("--expected-catalog-sha256", required=True)
    select_recipe.add_argument("--expected-matrix-sha256", required=True)
    select_recipe.add_argument("--expected-task-sha256", required=True)
    select_recipe.add_argument("--output", type=Path, required=True)

    def _cmd_select_recipe(a):
        release = core.load_catalog(a.catalog, a.schema_dir, repository_root=a.repository_root)  # fmt: skip  # noqa: E501
        result = core.select_repository_recipe_task(release, lane=a.lane, task_id=a.task_id, expected_catalog_sha256=a.expected_catalog_sha256, expected_matrix_sha256=a.expected_matrix_sha256, expected_task_sha256=a.expected_task_sha256, repository_root=a.repository_root)  # fmt: skip  # noqa: E501
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result
    select_recipe.set_defaults(func=_cmd_select_recipe)

    publish_parser = groups.add_parser("publish")
    publish_actions = publish_parser.add_subparsers(dest="action", required=True)
    publish_plan = publish_actions.add_parser("plan")
    publish_plan.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    publish_plan.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501
    publish_plan.add_argument("--repository-root", type=Path, default=core.REPO_ROOT)
    publish_plan.add_argument("--repository", default=None)
    publish_plan.add_argument('--lane', choices=('feature-candidate', 'protected-tag'), required=True)  # fmt: skip  # noqa: E501
    publish_plan.add_argument("--allow", required=True)
    publish_plan.add_argument("--request", default="")
    publish_plan.add_argument("--dry-run", default="true")
    publish_plan.add_argument("--output", type=Path, required=True)

    def _cmd_publish_plan(a):
        release = core.load_catalog(a.catalog, a.schema_dir, repository_root=a.repository_root, repository=a.repository)  # fmt: skip  # noqa: E501
        plan = core.compute_publish_plan(release, lane=a.lane, allow=json.loads(a.allow), request=a.request, dry_run=a.dry_run.strip().lower() == 'true')  # fmt: skip  # noqa: E501
        payload = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(payload + "\n", encoding="utf-8")
        return {"kind": "ucm-publish-plan", "schema_version": 1, "publish": plan}
    publish_plan.set_defaults(func=_cmd_publish_plan)

    publish_github_release = publish_actions.add_parser("github-release")
    publish_github_release.add_argument("--plan", type=Path, required=True)
    publish_github_release.add_argument("--plan-sha256", required=True)
    publish_github_release.add_argument("--repository", required=True)
    publish_github_release.add_argument('--stage', choices=('draft', 'finalize'), required=True)  # fmt: skip  # noqa: E501
    publish_github_release.add_argument("--output-dir", type=Path, required=True)
    publish_github_release.add_argument("--source-sha", required=True)
    publish_github_release.set_defaults(func=_publish_github_release)

    core_parser = groups.add_parser("core")
    core_actions = core_parser.add_subparsers(dest="action", required=True)
    tag_preflight = core_actions.add_parser("tag-preflight")
    tag_preflight.add_argument('--lane', choices=('feature-candidate', 'protected-tag'), required=True)  # fmt: skip  # noqa: E501
    tag_preflight.add_argument("--resolved-plan", type=Path)
    tag_preflight.add_argument("--expected-plan-sha256")
    tag_preflight.add_argument('--catalog-planner', action='store_true', help='resolve current catalog authority only in the initial planning job')  # fmt: skip  # noqa: E501
    _paths(tag_preflight)

    def _cmd_tag_preflight(a):
        if a.catalog_planner:
            if a.resolved_plan is not None or a.expected_plan_sha256 is not None:
                raise ValueError('catalog planner mode cannot consume a frozen plan binding')  # fmt: skip  # noqa: E501
            return core.tag_preflight(lane=a.lane, release_path=a.release, schema_dir=a.schema_dir)  # fmt: skip  # noqa: E501
        if a.resolved_plan is None or not isinstance(a.expected_plan_sha256, str) or not a.expected_plan_sha256:
            raise ValueError('tag preflight requires an exact frozen plan and expected plan hash')  # fmt: skip  # noqa: E501
        resolved_plan = core.load_json(a.resolved_plan)
        registry.validate_resolved_plan(resolved_plan)
        if resolved_plan["resolved_plan_sha256"] != a.expected_plan_sha256:
            raise ValueError('resolved plan hash differs from expected plan hash')  # fmt: skip  # noqa: E501
        if resolved_plan['lane'] != a.lane: raise ValueError('tag preflight lane differs from frozen plan')  # noqa: E701,E501
        return core.tag_preflight(lane=a.lane, authority=resolved_plan['source'])  # fmt: skip  # noqa: E501
    tag_preflight.set_defaults(func=_cmd_tag_preflight)

    registry_parser = groups.add_parser("registry")
    registry_actions = registry_parser.add_subparsers(dest="action", required=True)
    validate_member = registry_actions.add_parser("validate-member-schema")
    validate_member.add_argument("--input", type=Path, required=True)

    def _cmd_validate_member_schema(a):
        record = core.load_json(a.input)
        schema = core.load_json(core.DEFAULT_SCHEMA_DIR / 'release-manifest.schema.json')  # fmt: skip  # noqa: E501
        core.validate_schema(record, schema['$defs']['registryMemberRecord'], root=schema)  # fmt: skip  # noqa: E501
        return {"kind": "ucm-member-schema-validation", "valid": True}
    validate_member.set_defaults(func=_cmd_validate_member_schema)

    wheel_parser = groups.add_parser("wheel")
    wheel_actions = wheel_parser.add_subparsers(dest="action", required=True)

    wc_env = wheel_actions.add_parser("check-environment")
    wc_env.add_argument("--task", type=Path, required=True)
    wc_env.add_argument("--python-executable", type=Path, required=True)

    def _cmd_wheel_check_env(a):
        task = core.load_json(a.task)
        return wheel.check_build_environment(task, python_executable=a.python_executable)  # fmt: skip  # noqa: E501
    wc_env.set_defaults(func=_cmd_wheel_check_env)

    wc_ctx = wheel_actions.add_parser("verify-context")
    wc_ctx.add_argument("--archive", type=Path, required=True)
    wc_ctx.add_argument("--manifest", type=Path, required=True)
    wc_ctx.add_argument("--source-root", type=Path, required=True)
    wc_ctx.add_argument("--commit-payload", type=Path)
    wc_ctx.add_argument("--expected-source-sha")

    def _cmd_wheel_verify_ctx(a):
        return wheel.verify_source_context(a.archive, a.manifest, a.source_root, a.commit_payload, a.expected_source_sha)  # fmt: skip  # noqa: E501
    wc_ctx.set_defaults(func=_cmd_wheel_verify_ctx)

    wc_auth = wheel_actions.add_parser("authority")
    wc_auth.add_argument("--spec-id", required=True)
    wc_auth.add_argument("--source-sha", required=True)
    wc_auth.add_argument("--source-date-epoch", required=True)
    wc_auth.add_argument("--builder-coordinate", required=True)
    wc_auth.add_argument("--wheelhouse", type=Path, required=True)
    wc_auth.add_argument("--source-archive", type=Path, required=True)
    wc_auth.add_argument("--source-commit-payload", type=Path, required=True)
    wc_auth.add_argument("--source-manifest", type=Path, required=True)
    wc_auth.add_argument("--source-root", type=Path, required=True)
    wc_auth.add_argument("--task-file", type=Path, required=True)
    wc_auth.add_argument("--output", type=Path, required=True)

    def _cmd_wheel_authority(a):
        return wheel.build_authority_record(a.output, a.spec_id, a.source_sha, int(a.source_date_epoch), a.builder_coordinate, a.wheelhouse, a.source_archive, a.source_commit_payload, a.source_manifest, a.source_root, a.task_file)  # fmt: skip  # noqa: E501
    wc_auth.set_defaults(func=_cmd_wheel_authority)

    wc_closure = wheel_actions.add_parser("closure")
    wc_closure.add_argument("path", type=Path)
    wc_closure.add_argument("--spec-id", required=True)
    wc_closure.add_argument("--authority-file", type=Path, required=True)
    wc_closure.add_argument("--task-file", type=Path)
    wc_closure.add_argument("--output", type=Path, required=True)

    def _cmd_wheel_closure(a):
        return wheel.audit_dependency_closure(a.path, a.output, a.spec_id, a.authority_file, task_path=a.task_file)  # fmt: skip  # noqa: E501
    wc_closure.set_defaults(func=_cmd_wheel_closure)

    wc_seal = wheel_actions.add_parser("seal")
    wc_seal.add_argument("path", type=Path)
    wc_seal.add_argument("--spec-id", required=True)
    wc_seal.add_argument("--source-sha", required=True)
    wc_seal.add_argument("--build-key", required=True)
    wc_seal.add_argument("--source-date-epoch", required=True)
    wc_seal.add_argument("--authority-file", type=Path, required=True)
    wc_seal.add_argument("--dependency-closure", type=Path, required=True)
    wc_seal.add_argument("--task-file", type=Path)
    wc_seal.add_argument("--output-dir", type=Path, required=True)

    def _cmd_wheel_seal(a):
        return wheel.seal_wheel(a.path, a.output_dir, a.spec_id, a.source_sha, a.build_key, int(a.source_date_epoch), a.authority_file, a.dependency_closure, task_path=a.task_file)  # fmt: skip  # noqa: E501
    wc_seal.set_defaults(func=_cmd_wheel_seal)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, yaml.YAMLError) as error:
        parser.exit(2, f"error: {error}\n")
    print(_json(result))
    return 0


if __name__ == '__main__': sys.exit(main())  # noqa: E701
# fmt: on
