# fmt: off
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import builders, capabilities, compact, core, products, registry, wheel

catalog_resolution = registry


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    parser.add_argument("--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR)


def _write(path: Path, value: object) -> None:
    path.write_bytes(core.canonical_bytes(value) + b"\n")


def _load_result_records(path: Path, key: str, kind: str) -> dict[str, object]:
    paths = [path] if path.is_file() else sorted(path.rglob("result.json"))
    records: list[object] = []
    failures: list[object] = []
    statuses: list[str] = []
    for item_path in paths:
        value = core.load_json(item_path)
        status = value.get("status")
        if isinstance(status, str):
            statuses.append(status)
        nested = value.get(key)
        if isinstance(nested, list):
            records.extend(nested)
        else:
            records.append(value)
        nested_failures = value.get("failures")
        if isinstance(nested_failures, list):
            failures.extend(nested_failures)
    return {
        "kind": kind,
        "schema_version": 3,
        "status": "failed" if "failed" in statuses or failures else "success",
        key: records,
        "failures": failures,
    }


def _load_mooncake_probes(path: Path) -> dict[str, object]:
    collected = _load_result_records(
        path, "probes", "ucm-runtime-mooncake-probes"
    )
    raw_probes = collected.get("probes")
    if not isinstance(raw_probes, list):
        raise ValueError("Mooncake probe collection must contain probes")
    probes = []
    for raw in raw_probes:
        if not isinstance(raw, dict):
            raise ValueError("Mooncake probe Result must be an object")
        probes.append(
            {
                field: raw[field]
                for field in capabilities.MOONCAKE_PROBE_FIELDS
            }
        )
    collected["probes"] = probes
    return collected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ucm_release")
    groups = parser.add_subparsers(dest="group", required=True)

    builders_parser = groups.add_parser("builders")
    builders_actions = builders_parser.add_subparsers(dest="action", required=True)

    builders_discover = builders_actions.add_parser("discover")
    builders_discover.add_argument("--config", type=Path, default=builders.DEFAULT_CONFIG)
    builders_discover.add_argument("--snapshot", "--snapshot-dir", dest="snapshot", type=Path)
    builders_discover.add_argument("--owner")
    builders_discover.add_argument("--source-only", action="store_true")
    builders_discover.add_argument("--output", type=Path, required=True)

    def _cmd_builders_discover(a):
        result = builders.discover_builders(
            a.config,
            snapshot_dir=a.snapshot,
            owner=a.owner,
            source_only=a.source_only,
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    builders_discover.set_defaults(func=_cmd_builders_discover)

    builders_discover_sources = builders_actions.add_parser("discover-sources")
    builders_discover_sources.add_argument(
        "--config", type=Path, default=builders.DEFAULT_CONFIG
    )
    builders_discover_sources.add_argument(
        "--snapshot", "--snapshot-dir", dest="snapshot", type=Path
    )
    builders_discover_sources.add_argument("--owner")
    builders_discover_sources.add_argument("--legacy-output", type=Path, required=True)
    builders_discover_sources.add_argument("--output", type=Path, required=True)

    def _cmd_builders_discover_sources(a):
        legacy = builders.discover_builders(
            a.config,
            snapshot_dir=a.snapshot,
            owner=a.owner,
        )
        discovered = builders.discover_builder_sources(
            a.config,
            snapshot_dir=a.snapshot,
            owner=a.owner,
        )
        a.legacy_output.parent.mkdir(parents=True, exist_ok=True)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.legacy_output, legacy)
        _write(a.output, discovered)
        return discovered

    builders_discover_sources.set_defaults(func=_cmd_builders_discover_sources)

    builders_sync_plan = builders_actions.add_parser("sync-plan")
    builders_sync_plan.add_argument("--catalog", type=Path, required=True)
    builders_sync_plan.add_argument(
        "--existing", "--existing-tags", dest="existing", type=Path, required=True
    )
    builders_sync_plan.add_argument("--output", type=Path, required=True)

    def _cmd_builders_sync_plan(a):
        result = builders.compute_sync_plan(
            core.load_json(a.catalog), core.load_json(a.existing)
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    builders_sync_plan.set_defaults(func=_cmd_builders_sync_plan)

    builders_plan_facts = builders_actions.add_parser("plan-facts")
    builders_plan_facts.add_argument("--builder-catalog", type=Path, required=True)
    builders_plan_facts.add_argument("--runtime-discovery", type=Path, required=True)
    builders_plan_facts.add_argument("--mooncake-probes", type=Path, required=True)
    builders_plan_facts.add_argument("--output", type=Path, required=True)

    def _cmd_builders_plan_facts(a):
        result = builders.plan_builder_facts(
            core.load_json(a.builder_catalog),
            core.load_json(a.runtime_discovery),
            _load_mooncake_probes(a.mooncake_probes),
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    builders_plan_facts.set_defaults(func=_cmd_builders_plan_facts)

    builders_collect_facts = builders_actions.add_parser("collect-facts")
    builders_collect_facts.add_argument("--plan", type=Path, required=True)
    builders_collect_facts.add_argument("--results", type=Path, required=True)
    builders_collect_facts.add_argument("--output", type=Path, required=True)

    def _cmd_builders_collect_facts(a):
        result = builders.collect_builder_facts(
            core.load_json(a.plan),
            _load_result_records(a.results, "results", "ucm-builder-results"),
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    builders_collect_facts.set_defaults(func=_cmd_builders_collect_facts)

    builders_select = builders_actions.add_parser("select")
    builders_select.add_argument("--catalog", type=Path, required=True)
    builders_select.add_argument("--release", type=Path, default=builders.DEFAULT_RELEASE)
    builders_select.add_argument("--output", type=Path, required=True)

    def _cmd_builders_select(a):
        result = builders.select_builders(
            core.load_json(a.catalog), core.load_catalog(a.release)
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    builders_select.set_defaults(func=_cmd_builders_select)

    compact_parser = groups.add_parser("compact")
    compact_actions = compact_parser.add_subparsers(dest="action", required=True)

    compact_plan = compact_actions.add_parser("plan")
    compact_plan.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    compact_plan.add_argument("--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR)
    compact_plan.add_argument("--builder-catalog", type=Path, required=True)
    compact_plan.add_argument("--route", choices=tuple(sorted(compact.ROUTES)), required=True)
    compact_plan.add_argument("--pin-upstream", action="append", default=None)
    compact_plan.add_argument("--output", type=Path, required=True)

    def _cmd_compact_plan(a):
        result = compact.resolve_plan(
            core.load_catalog(a.catalog, a.schema_dir),
            builder_catalog=core.load_json(a.builder_catalog),
            route=a.route,
            pinned_upstreams=a.pin_upstream,
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    compact_plan.set_defaults(func=_cmd_compact_plan)

    compact_select = compact_actions.add_parser("select")
    compact_select.add_argument("--plan", type=Path, required=True)
    compact_select.add_argument("--kind", choices=("wheel", "image"), required=True)
    compact_select.add_argument("--id", required=True)
    compact_select.add_argument("--output", type=Path, required=True)

    def _cmd_compact_select(a):
        result = compact.select_task(core.load_json(a.plan), a.kind, a.id)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    compact_select.set_defaults(func=_cmd_compact_select)

    compact_prepare = compact_actions.add_parser("prepare-wheel-source")
    compact_prepare.add_argument("--source-root", type=Path, required=True)
    compact_prepare.add_argument("--distribution", required=True)
    compact_prepare.set_defaults(
        func=lambda a: compact.prepare_wheel_source(a.source_root, a.distribution)
    )

    plan_parser = groups.add_parser("plan")
    plan_actions = plan_parser.add_subparsers(dest="action", required=True)
    prepare_candidates = plan_actions.add_parser("prepare-candidates")
    prepare_candidates.add_argument(
        "--release", type=Path, default=core.DEFAULT_RELEASE
    )
    prepare_candidates.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
    prepare_candidates.add_argument(
        "--repository-root", type=Path, default=core.REPO_ROOT
    )
    prepare_candidates.add_argument("--capability-catalog", type=Path, required=True)
    prepare_candidates.add_argument(
        "--route", choices=("pr", "daily", "release"), required=True
    )
    prepare_candidates.add_argument("--source-sha", required=True)
    prepare_candidates.add_argument("--baseline-manifest", type=Path)
    prepare_candidates.add_argument("--output", type=Path, required=True)

    def _cmd_prepare_candidates(a):
        config = core.load_catalog(
            a.release,
            a.schema_dir,
            repository_root=a.repository_root,
        )
        catalog = capabilities.validate_capability_catalog(
            core.load_json(a.capability_catalog)
        )
        baseline = (
            core.load_json(a.baseline_manifest)
            if a.baseline_manifest is not None
            else None
        )
        authority = builders.freeze_current_builder_authority(
            source_sha=a.source_sha,
            repository_root=a.repository_root,
        )
        result = products.prepare_candidate_selection(
            config,
            catalog,
            authority,
            route=a.route,
            source_sha=a.source_sha,
            baseline_manifest=baseline,
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    prepare_candidates.set_defaults(func=_cmd_prepare_candidates)

    config = groups.add_parser("config")
    config_actions = config.add_subparsers(dest="action", required=True)
    validate = config_actions.add_parser("validate")
    _paths(validate)
    validate.set_defaults(func=lambda a: {'schema_version': 1, 'build_profiles': len(core.load_catalog(a.release, a.schema_dir)['build_profiles']), 'compatibility_rules': len(core.load_catalog(a.release, a.schema_dir)['compatibility']['rules'])})  # fmt: skip  # noqa: E501

    catalog_parser = groups.add_parser("catalog")
    catalog_actions = catalog_parser.add_subparsers(dest="action", required=True)
    discover_runtimes = catalog_actions.add_parser("discover-runtimes")
    discover_runtimes.add_argument("--builder-catalog", type=Path, required=True)
    discover_runtimes.add_argument("--output", type=Path, required=True)

    def _cmd_discover_runtimes(a):
        # Release-derived version fields are not runtime discovery authority.
        release = core.load_catalog(version_override="0.0.0")
        result = capabilities.discover_live_runtime_candidates(
            core.load_json(a.builder_catalog), release
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    discover_runtimes.set_defaults(func=_cmd_discover_runtimes)

    assemble_capabilities = catalog_actions.add_parser("assemble-capabilities")
    assemble_capabilities.add_argument("--builder-facts", type=Path, required=True)
    assemble_capabilities.add_argument("--python-probes", type=Path, required=True)
    assemble_capabilities.add_argument("--runtime-discovery", type=Path, required=True)
    assemble_capabilities.add_argument("--mooncake-probes", type=Path, required=True)
    assemble_capabilities.add_argument("--output", type=Path, required=True)

    def _cmd_assemble_capabilities(a):
        # Catalog assembly consumes fact-owned source_sha, not a release tag.
        release = core.load_catalog(version_override="0.0.0")
        result = capabilities.assemble_capability_catalog(
            builder_discovery=core.load_json(a.builder_facts),
            python_probes=_load_result_records(
                a.python_probes, "probes", "ucm-builder-python-probes"
            ),
            runtime_discovery=core.load_json(a.runtime_discovery),
            mooncake_probes=_load_mooncake_probes(a.mooncake_probes),
            python_requires=release["discovery"]["python_requires"],
        )
        capabilities.validate_capability_catalog(result)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result

    assemble_capabilities.set_defaults(func=_cmd_assemble_capabilities)
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
    catalog_resolve.add_argument("--builder-catalog", type=Path, required=True)
    catalog_resolve.add_argument("--fixture", type=Path)
    catalog_resolve.add_argument("--pin-upstream", action="append", default=None, metavar="REPO:TAG", help="pin a specific upstream image:tag (PR path; repeatable; skips the registry scan + catalog compatibility gates)")  # fmt: skip  # noqa: E501
    catalog_resolve.add_argument("--output", type=Path, required=True)

    def _cmd_resolve(a):
        release = core.load_catalog(a.catalog, a.schema_dir)
        builder_catalog = core.load_json(a.builder_catalog)
        fixture = core.load_json(a.fixture) if a.fixture else None
        result = catalog_resolution.resolve_catalog(release, builder_catalog=builder_catalog, source_sha=a.source_sha, lane=a.lane, fixture=fixture, pin_upstreams=a.pin_upstream)  # fmt: skip  # noqa: E501
        a.output.parent.mkdir(parents=True, exist_ok=True)
        _write(a.output, result)
        return result
    catalog_resolve.set_defaults(func=_cmd_resolve)

    validate_resolved_plan = catalog_actions.add_parser("validate-resolved-plan")
    validate_resolved_plan.add_argument("--plan", type=Path, required=True)

    def _cmd_validate_resolved_plan(a):
        plan = core.load_json(a.plan)
        registry.validate_resolved_plan(plan)
        return {"kind": "ucm-resolved-plan-validation", "schema_version": 1}

    validate_resolved_plan.set_defaults(func=_cmd_validate_resolved_plan)

    validate_main_loop = catalog_actions.add_parser("validate-main-loop")
    validate_main_loop.add_argument("--plan", type=Path, required=True)
    validate_main_loop.add_argument("--catalog", type=Path, default=core.DEFAULT_RELEASE)
    validate_main_loop.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501

    def _cmd_validate_main_loop(a):
        plan = core.load_json(a.plan)
        catalog = core.load_catalog(a.catalog, a.schema_dir)
        counts = registry.validate_main_full_loop_plan(plan, catalog)
        return {"kind": "ucm-main-full-loop-validation", "schema_version": 1, **counts}
    validate_main_loop.set_defaults(func=_cmd_validate_main_loop)

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

    core_parser = groups.add_parser("core")
    core_actions = core_parser.add_subparsers(dest="action", required=True)
    tag_preflight = core_actions.add_parser("tag-preflight")
    tag_preflight.add_argument('--lane', choices=('feature-candidate', 'protected-tag'), required=True)  # fmt: skip  # noqa: E501
    tag_preflight.add_argument("--resolved-plan", type=Path)
    tag_preflight.add_argument('--catalog-planner', action='store_true', help='resolve current catalog authority only in the initial planning job')  # fmt: skip  # noqa: E501
    _paths(tag_preflight)

    def _cmd_tag_preflight(a):
        if a.catalog_planner:
            if a.resolved_plan is not None:
                raise ValueError('catalog planner mode cannot consume a resolved plan binding')  # fmt: skip  # noqa: E501
            return core.tag_preflight(lane=a.lane, release_path=a.release, schema_dir=a.schema_dir)  # fmt: skip  # noqa: E501
        if a.resolved_plan is None:
            raise ValueError('tag preflight requires a resolved plan')  # fmt: skip  # noqa: E501
        resolved_plan = core.load_json(a.resolved_plan)
        registry.validate_resolved_plan(resolved_plan)
        if resolved_plan['lane'] != a.lane: raise ValueError('tag preflight lane differs from resolved plan')  # noqa: E701,E501
        return core.tag_preflight(lane=a.lane, authority=resolved_plan['source'])  # fmt: skip  # noqa: E501
    tag_preflight.set_defaults(func=_cmd_tag_preflight)

    wheel_parser = groups.add_parser("wheel")
    wheel_actions = wheel_parser.add_subparsers(dest="action", required=True)

    wc_build_config = wheel_actions.add_parser("build-config")
    wc_build_config.add_argument("--task-file", type=Path, required=True)
    wc_build_config.add_argument("--authority-file", type=Path, required=True)
    wc_build_config.add_argument("--output", type=Path, required=True)
    wc_build_config.set_defaults(
        func=lambda a: wheel.build_wheel_config(
            a.task_file, a.authority_file, a.output
        )
    )

    wc_prepare_source = wheel_actions.add_parser("prepare-source")
    wc_prepare_source.add_argument("--build-config", type=Path, required=True)
    wc_prepare_source.add_argument("--source-root", type=Path, required=True)
    wc_prepare_source.set_defaults(
        func=lambda a: wheel.prepare_wheel_source(a.build_config, a.source_root)
    )

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

    wc_inspect = wheel_actions.add_parser("inspect")
    wc_inspect.add_argument("path", type=Path)
    wc_inspect.add_argument("--spec-id", required=True)
    wc_inspect.add_argument("--expected-sha256", required=True)
    wc_inspect.add_argument('--source-kind', choices=('fixture', 'builder-candidate'), required=True)  # fmt: skip  # noqa: E501
    wc_inspect.add_argument("--task-file", type=Path)
    wc_inspect.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    wc_inspect.add_argument('--schema-dir', type=Path, default=core.DEFAULT_SCHEMA_DIR)  # fmt: skip  # noqa: E501
    wc_inspect.add_argument("--output", type=Path)

    def _cmd_wheel_inspect(a):
        result = wheel.inspect_wheel(a.path, a.spec_id, a.expected_sha256, a.source_kind, task_path=a.task_file, release_path=a.release, schema_dir=a.schema_dir)  # fmt: skip  # noqa: E501
        if a.output is not None:
            a.output.parent.mkdir(parents=True, exist_ok=True)
            _write(a.output, result)
        return result
    wc_inspect.set_defaults(func=_cmd_wheel_inspect)

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
