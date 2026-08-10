"""Command-line interface for the compact UCM release package."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

from . import chart, core, image, registry, verify, wheel


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    parser.add_argument(
        "--compatibility", type=Path, default=core.DEFAULT_COMPATIBILITY
    )
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
    if not all(
        isinstance(value, str) for value in (manifest_path, allowed_root, source_sha)
    ):
        raise ValueError("release asset manifest binding is malformed")
    manifest = verify.validate_release_asset_manifest(
        core.load_json(Path(manifest_path)), allowed_root=Path(allowed_root)
    )
    if manifest["source_sha"] != source_sha:
        raise ValueError("release asset manifest source differs from live Release")
    return manifest


def _release_asset_state(
    request: dict[str, object], *, release_key: str = "release"
) -> dict[str, object]:
    release_path = request.get(release_key)
    source_sha = request.get("source_sha")
    release_id = request.get("release_id")
    if not isinstance(release_path, str) or not isinstance(source_sha, str):
        raise ValueError("release asset state binding is malformed")
    _release_asset_manifest(request)
    state = verify.plan_github_release(core.load_json(Path(release_path)), source_sha)
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

    core_parser = groups.add_parser("core")
    core_actions = core_parser.add_subparsers(dest="action", required=True)
    plan = core_actions.add_parser("plan")
    _paths(plan)
    plan.add_argument("--output", type=Path)
    plan.add_argument("--require-publishable", action="store_true")
    matrix = core_actions.add_parser("matrix")
    matrix.add_argument(
        "--lane", choices=("feature-candidate", "protected-tag"), required=True
    )
    _paths(matrix)
    hosted_matrix = core_actions.add_parser("hosted-matrix")
    hosted_matrix.add_argument("--source-sha", required=True)
    hosted_matrix.add_argument("--source-date-epoch", required=True, type=int)
    hosted_matrix.add_argument("--spec-id")
    hosted_matrix.add_argument("--output", required=True, type=Path)
    tag_preflight = core_actions.add_parser("tag-preflight")
    tag_preflight.add_argument(
        "--lane", choices=("feature-candidate", "protected-tag"), required=True
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
    _paths(inspect)
    seal = wheel_actions.add_parser("seal")
    seal.add_argument("wheel", type=Path)
    seal.add_argument("--spec-id", required=True)
    seal.add_argument("--source-sha", required=True)
    seal.add_argument("--build-key", required=True)
    seal.add_argument("--source-date-epoch", required=True, type=int)
    seal.add_argument("--authority-file", required=True, type=Path)
    seal.add_argument("--dependency-closure", required=True, type=Path)
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
    _paths(closure)
    preflight_dependencies = wheel_actions.add_parser("preflight-dependencies")
    preflight_dependencies.add_argument("--binary", required=True, type=Path)
    preflight_dependencies.add_argument("--spec-id", required=True)
    _paths(preflight_dependencies)
    fixture_build = wheel_actions.add_parser("fixture-build")
    fixture_build.add_argument("--output-dir", type=Path, required=True)
    fixture_build.add_argument("--source-sha", required=True)
    fixture_build.add_argument("--profile-id", required=True)
    _paths(fixture_build)

    chart_parser = groups.add_parser("chart")
    chart_actions = chart_parser.add_subparsers(dest="action", required=True)
    package = chart_actions.add_parser("package")
    package.add_argument("--output-dir", type=Path, required=True)
    _paths(package)

    registry_parser = groups.add_parser("registry")
    registry_actions = registry_parser.add_subparsers(dest="action", required=True)
    scan = registry_actions.add_parser("scan")
    scan.add_argument("--repository", required=True)
    scan.add_argument("--tag", required=True)
    scan.add_argument("--fixture", type=Path)
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

    reconcile_parser = groups.add_parser("reconcile")
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
    loop_aggregate_real.add_argument("--output", type=Path, required=True)
    loop_aggregate_real.add_argument("--output-dir", type=Path)
    loop_aggregate_real.add_argument("--run-id", required=True)
    loop_aggregate_real.add_argument("--attempt", type=int, required=True)

    image_parser = groups.add_parser("image")
    image_actions = image_parser.add_subparsers(dest="action", required=True)
    image_actions.add_parser("base-authority")
    image_actions.add_parser("toolchain-authority")
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
    image_prepare = image_actions.add_parser("prepare")
    image_prepare.add_argument("--input", type=Path, required=True)
    image_prepare.add_argument("--wheel-dir", type=Path, required=True)
    image_prepare.add_argument("--expected-source-sha", required=True)
    image_prepare.add_argument("--base-authority", type=Path, required=True)
    image_prepare.add_argument("--base-index", type=Path, required=True)
    image_prepare.add_argument("--base-manifest", type=Path, required=True)
    image_prepare.add_argument("--base-config", type=Path, required=True)
    image_prepare.add_argument("--output-dir", type=Path, required=True)
    image_actions.add_parser("real-authorities")
    image_real_base = image_actions.add_parser("base-record-real")
    image_real_base.add_argument("--family-id", required=True)
    image_real_base.add_argument(
        "--architecture", choices=("amd64", "arm64"), required=True
    )
    image_real_base.add_argument("--index", type=Path, required=True)
    image_real_base.add_argument("--manifest", type=Path, required=True)
    image_real_base.add_argument("--config", type=Path, required=True)
    image_prepare_real = image_actions.add_parser("prepare-real")
    image_prepare_real.add_argument("--family-id", required=True)
    image_prepare_real.add_argument(
        "--architecture", choices=("amd64", "arm64"), required=True
    )
    image_prepare_real.add_argument("--wheel", type=Path, required=True)
    image_prepare_real.add_argument("--wheel-inspection", type=Path, required=True)
    image_prepare_real.add_argument("--base-record", type=Path, required=True)
    image_prepare_real.add_argument("--wrapt-wheel", type=Path, required=True)
    image_prepare_real.add_argument("--output-dir", type=Path, required=True)
    _paths(image_prepare_real)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if (args.group, args.action) == ("config", "validate"):
            release, compatibility = core.validate_config(
                args.release, args.compatibility, args.schema_dir
            )
            result = {
                "schema_version": 1,
                "wheel_profiles": len(release["wheel_profiles"]),
                "compatibility_rules": len(compatibility["rules"]),
            }
        elif (args.group, args.action) == ("core", "plan"):
            result = core.build_release_manifest(
                args.release, args.compatibility, args.schema_dir
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(_json(result) + "\n", encoding="utf-8")
            if (
                args.require_publishable
                and result["eligible_wheel_count"] != result["declared_wheel_count"]
            ):
                parser.error(
                    f"{result['eligible_wheel_count']} of {result['declared_wheel_count']} wheel specs are eligible"
                )
        elif (args.group, args.action) == ("core", "matrix"):
            result = core.build_matrix(
                args.lane, args.release, args.compatibility, args.schema_dir
            )
        elif (args.group, args.action) == ("core", "hosted-matrix"):
            result = verify.hosted_build_matrix(args.source_sha, args.source_date_epoch)
            if args.spec_id:
                matches = [
                    item for item in result["tasks"] if item["spec_id"] == args.spec_id
                ]
                if len(matches) != 1:
                    raise ValueError("hosted spec does not resolve exactly once")
                result = matches[0]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write(args.output, result)
        elif (args.group, args.action) == ("core", "tag-preflight"):
            result = core.tag_preflight(
                lane=args.lane,
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("wheel", "inspect"):
            result = wheel.inspect_wheel(
                args.wheel,
                args.spec_id,
                args.expected_sha256,
                args.source_kind,
                release_path=args.release,
                compatibility_path=args.compatibility,
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
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
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
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
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
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("wheel", "preflight-dependencies"):
            result = wheel.preflight_dependencies(
                args.binary,
                args.spec_id,
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("wheel", "fixture-build"):
            result = wheel.build_fixture_wheel(
                args.output_dir,
                args.source_sha,
                args.profile_id,
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("chart", "package"):
            result = chart.package_chart(
                args.output_dir,
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
            )
        elif (args.group, args.action) == ("registry", "scan"):
            fixture = core.load_json(args.fixture) if args.fixture else None
            result = registry.scan_registry(
                args.repository,
                args.tag,
                fixture=fixture,
            )
        elif (args.group, args.action) == ("registry", "inventory"):
            request = core.load_json(args.input)
            if request != {}:
                raise ValueError("inventory input must be an empty object")
            result = registry.inventory_registry()
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "verify-member"):
            request = core.load_json(args.input)
            if set(request) != {"lane", "image_result", "oci_archive"}:
                raise ValueError(
                    "verify-member input requires lane/image_result/oci_archive"
                )
            if not isinstance(request["image_result"], str) or not isinstance(
                request["oci_archive"], str
            ):
                raise ValueError("verify-member paths must be strings")
            result = registry.publish_member(
                Path(request["oci_archive"]),
                image_result=core.load_json(Path(request["image_result"])),
                lane=request["lane"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "plan-index"):
            request = core.load_json(args.input)
            if set(request) != {"lane", "members", "member_statuses"}:
                raise ValueError(
                    "plan-index input requires lane/members/member_statuses"
                )
            inventory = registry.inventory_registry()
            result = registry.plan_indexes(
                request["members"],
                inventory["entries"],
                member_statuses=request["member_statuses"],
                lane=request["lane"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "verify-index"):
            request = core.load_json(args.input)
            if set(request) != {"lane", "parent_plans", "family_id"}:
                raise ValueError(
                    "verify-index input requires lane/parent_plans/family_id"
                )
            parent = request["parent_plans"]
            if not isinstance(parent, dict) or not isinstance(
                parent.get("plans"), list
            ):
                raise ValueError("verify-index parent_plans is malformed")
            matches = [
                item
                for item in parent["plans"]
                if isinstance(item, dict)
                and item.get("family_id") == request["family_id"]
            ]
            if len(matches) != 1:
                raise ValueError("verify-index family does not resolve exactly once")
            result = registry.create_index(
                matches[0], parent_plans=parent, lane=request["lane"]
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "prepare-index"):
            request = core.load_json(args.input)
            if set(request) != {"lane", "parent_plans", "family_id"}:
                raise ValueError(
                    "prepare-index input requires lane/parent_plans/family_id"
                )
            parent = request["parent_plans"]
            if not isinstance(parent, dict) or not isinstance(
                parent.get("plans"), list
            ):
                raise ValueError("prepare-index parent_plans is malformed")
            matches = [
                item
                for item in parent["plans"]
                if isinstance(item, dict)
                and item.get("family_id") == request["family_id"]
            ]
            if len(matches) != 1:
                raise ValueError("prepare-index family does not resolve exactly once")
            result = registry.prepare_index(
                matches[0], parent_plans=parent, lane=request["lane"]
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("registry", "finalize-index"):
            request = core.load_json(args.input)
            if set(request) != {"parent_plans", "provisional"} or not all(
                isinstance(request[key], str) for key in ("parent_plans", "provisional")
            ):
                raise ValueError(
                    "finalize-index input requires exact parent/provisional paths"
                )
            result = registry.finalize_index(
                core.load_json(Path(request["provisional"])),
                parent_plans=core.load_json(Path(request["parent_plans"])),
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
            ):
                raise ValueError("registry aggregation paths are malformed")
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
            if set(request) != {"lane", "operations"}:
                raise ValueError(
                    "audit-operations input requires exactly lane and operations"
                )
            audit = verify.audit_operations(request["operations"], lane=request["lane"])
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
                "spec_id",
                "oci_artifact",
                "image_artifact",
                "hosted_task",
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "source_sha",
                    "spec_id",
                    "oci_artifact",
                    "image_artifact",
                    "hosted_task",
                )
            ):
                raise ValueError("image bridge artifact input is malformed")
            task = core.load_json(Path(request["hosted_task"]))
            if (
                not isinstance(task, dict)
                or task.get("source_sha") != request["source_sha"]
                or task.get("spec_id") != request["spec_id"]
                or not isinstance(task.get("source_date_epoch"), int)
                or isinstance(task.get("source_date_epoch"), bool)
            ):
                raise ValueError("image bridge hosted task is malformed")
            expected = verify.hosted_build_matrix(
                request["source_sha"], task["source_date_epoch"]
            )
            matches = [
                item
                for item in expected["tasks"]
                if item["spec_id"] == request["spec_id"]
            ]
            if len(matches) != 1 or task != matches[0]:
                raise ValueError("image bridge hosted task differs from authority")
            result = {
                "oci_artifact": verify.validate_run_bound_artifact_name(
                    request["oci_artifact"],
                    f"ucm-internal-oci-{request['spec_id']}-{request['source_sha']}",
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
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in ("parent_plans", "parent_artifact", "source_sha")
            ):
                raise ValueError("index parent artifact input is malformed")
            artifact = verify.validate_run_bound_artifact_name(
                request["parent_artifact"],
                f"ucm-index-parent-{request['source_sha']}",
                request["run"],
            )
            parent = registry.validate_index_plans(
                core.load_json(Path(request["parent_plans"]))
            )
            if parent["source_sha"] != request["source_sha"]:
                raise ValueError("index parent source differs from protected tag")
            result = {
                "schema_version": 1,
                "kind": "ucm-index-parent-artifact-validation",
                "parent_artifact": artifact,
                "source_sha": request["source_sha"],
                "plans_sha256": parent["plans_sha256"],
            }
            _write(args.output, result)
        elif (args.group, args.action) == ("artifact", "collect-members"):
            request = core.load_json(args.input)
            if set(request) != {"root", "output_dir", "source_sha", "run"} or any(
                not isinstance(request[key], str)
                for key in ("root", "output_dir", "source_sha")
            ):
                raise ValueError("member artifact collection input is malformed")
            specs = [
                item["spec_id"]
                for item in registry.canonical_registry_contract()["members"]
            ]
            logical = [f"ucm-member-{spec}-{request['source_sha']}" for spec in specs]
            directories = verify.resolve_run_bound_artifact_directories(
                Path(request["root"]), logical, run=request["run"], label="member"
            )
            output_dir = _empty_output_dir(Path(request["output_dir"]))
            paths: list[str] = []
            preflight_sha256s: dict[str, str] = {}
            for spec, name in zip(specs, logical, strict=True):
                directory = directories[name]
                expected_files = {
                    "member-record.json",
                    "member-audit.json",
                    "member-preflight.json",
                    "member-mutation-preflight.json",
                }
                if {path.name for path in directory.iterdir()} != expected_files:
                    raise ValueError("member artifact file set is noncanonical")
                source = directory / "member-record.json"
                record = registry.validate_member_record(core.load_json(source))
                if record["spec_id"] != spec:
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
                target = output_dir / f"{spec}.json"
                shutil.copyfile(source, target)
                paths.append(str(target))
                preflight_sha256s[spec] = mutation_preflight["preflight_sha256"]
            result = {
                "schema_version": 1,
                "kind": "ucm-member-artifact-collection",
                "source_sha": request["source_sha"],
                "member_records": paths,
                "member_record_sha256s": {
                    spec: registry.validate_member_record(
                        core.load_json(output_dir / f"{spec}.json")
                    )["record_sha256"]
                    for spec in specs
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
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in ("root", "output_dir", "source_sha", "parent_plans")
            ):
                raise ValueError("provisional artifact collection input is malformed")
            families = [
                item["family_id"]
                for item in registry.canonical_registry_contract()["indexes"]
            ]
            logical = [
                f"ucm-index-provisional-{family}-{request['source_sha']}"
                for family in families
            ]
            directories = verify.resolve_run_bound_artifact_directories(
                Path(request["root"]),
                logical,
                run=request["run"],
                label="provisional index",
            )
            parent = core.load_json(Path(request["parent_plans"]))
            output_dir = _empty_output_dir(Path(request["output_dir"]))
            paths: list[str] = []
            provisional_sha256s: dict[str, str] = {}
            preflight_sha256s: dict[str, str] = {}
            for family, name in zip(families, logical, strict=True):
                directory = directories[name]
                if {path.name for path in directory.iterdir()} != {
                    "provisional.json",
                    "preflight.json",
                }:
                    raise ValueError("provisional artifact file set is noncanonical")
                source = directory / "provisional.json"
                provisional = registry.validate_provisional_index(
                    core.load_json(source), parent_plans=parent
                )
                if provisional["family_id"] != family:
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
                target = output_dir / f"{family}.json"
                shutil.copyfile(source, target)
                paths.append(str(target))
                provisional_sha256s[family] = provisional["provisional_sha256"]
                preflight_sha256s[family] = preflight["preflight_sha256"]
            result = {
                "schema_version": 1,
                "kind": "ucm-provisional-artifact-collection",
                "source_sha": request["source_sha"],
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
                "run",
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "wheel_dir",
                    "chart_result",
                    "chart_package",
                    "output_dir",
                    "source_sha",
                )
            ):
                raise ValueError("release assets-manifest input is malformed")
            result = verify.build_release_asset_manifest(
                wheel_dir=Path(request["wheel_dir"]),
                chart_result_path=Path(request["chart_result"]),
                chart_package_path=Path(request["chart_package"]),
                output_dir=Path(request["output_dir"]),
                source_sha=request["source_sha"],
                run=request["run"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "plan-state"):
            request = core.load_json(args.input)
            if set(request) != {"remote", "source_sha", "just_created"}:
                raise ValueError("release plan-state input fields are noncanonical")
            result = verify.plan_github_release(
                request["remote"],
                request["source_sha"],
                just_created=request["just_created"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "select-pages"):
            request = core.load_json(args.input)
            if set(request) != {"pages", "source_sha"} or not all(
                isinstance(request[key], str) for key in request
            ):
                raise ValueError("release select-pages input is malformed")
            result = verify.select_github_release_pages(
                core.load_json_array(Path(request["pages"])), request["source_sha"]
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
            } or any(
                not isinstance(request[key], str)
                for key in (
                    "manifest",
                    "raw_assets",
                    "release",
                    "source_sha",
                    "allowed_root",
                )
            ):
                raise ValueError("release plan-downloads input is malformed")
            release_state = _release_asset_state(request)
            result = verify.plan_release_asset_downloads(
                core.load_json(Path(request["manifest"])),
                core.load_json_array(Path(request["raw_assets"])),
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
                require_complete=request["require_complete"],
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
                )
            ):
                raise ValueError("release refresh-assets input is malformed")
            prior_state = _release_asset_state(request, release_key="prior_release")
            release_state = _release_asset_state(request)
            result = verify.refresh_release_asset_metadata(
                core.load_json(Path(request["manifest"])),
                core.load_json_array(Path(request["prior_assets"])),
                core.load_json_array(Path(request["raw_assets"])),
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
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
                asset_download_slug=release_state["asset_download_slug"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "record-upload-response"):
            request = core.load_json(args.input)
            path_keys = {"manifest", "raw_response", "allowed_root"}
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
            if release_state["decision"] != "resume-draft":
                raise ValueError("release upload response requires a live draft")
            result = verify.record_release_upload_response(
                core.load_json(Path(request["manifest"])),
                core.load_json(Path(request["raw_response"])),
                expected_name=request["expected_name"],
                release_id=request["release_id"],
                allowed_root=Path(request["allowed_root"]),
                asset_download_slug=release_state["asset_download_slug"],
            )
            _write(args.output, result)
        elif (args.group, args.action) == ("release", "rebase-manifest"):
            request = core.load_json(args.input)
            if set(request) != {"manifest", "allowed_root"} or any(
                not isinstance(request[key], str) for key in request
            ):
                raise ValueError("release rebase-manifest input is malformed")
            result = verify.rebase_release_asset_manifest(
                core.load_json(Path(request["manifest"])),
                allowed_root=Path(request["allowed_root"]),
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
            }
            if set(request) != path_keys | {"source_sha"} or any(
                not isinstance(request[key], str) for key in path_keys | {"source_sha"}
            ):
                raise ValueError("release operation-ledger input is malformed")
            asset_manifest = _release_asset_manifest(
                request, manifest_key="asset_manifest"
            )
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
                ),
                source_sha=request["source_sha"],
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
            kwargs = {
                "release_id": request["release_id"],
                "allowed_root": Path(request["allowed_root"]),
                "asset_download_slug": release_state["asset_download_slug"],
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
            }
            if set(request) != path_keys | {"source_sha", "run"} or any(
                not isinstance(request[key], str) for key in path_keys
            ):
                raise ValueError("release publication-evidence input is malformed")
            asset_manifest = _release_asset_manifest(
                request, manifest_key="asset_manifest"
            )
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
                run=request["run"],
            )
            _write(args.output, result)
        elif args.group == "reconcile":
            request = core.load_json(args.input)
            if set(request) != {"candidate", "inventory"}:
                raise ValueError(
                    "reconcile input requires exactly candidate and inventory"
                )
            result = registry.reconcile(request["candidate"], request["inventory"])
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
        elif (args.group, args.action) == ("image", "verify"):
            result = image.verify_oci(
                args.context,
                args.oci,
                schema_dir=args.schema_dir,
                evidence_dir=args.evidence_dir,
                output_mode=args.output_mode,
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
            result = {
                "schema_version": 1,
                "kind": "ucm-real-image-authorities",
                "members": image.real_image_authorities(),
            }
            result["authorities_sha256"] = core.sha256_value(result)
        elif (args.group, args.action) == ("image", "base-record-real"):
            result = image.real_base_record_from_files(
                args.family_id,
                args.architecture,
                index_path=args.index,
                manifest_path=args.manifest,
                config_path=args.config,
            )
        elif (args.group, args.action) == ("image", "prepare-real"):
            result = image.prepare_real_context(
                family_id=args.family_id,
                architecture=args.architecture,
                wheel_path=args.wheel,
                wheel_inspection=core.load_json(args.wheel_inspection),
                base_record=core.load_json(args.base_record),
                wrapt_path=args.wrapt_wheel,
                output_dir=args.output_dir,
                release_path=args.release,
                compatibility_path=args.compatibility,
                schema_dir=args.schema_dir,
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
