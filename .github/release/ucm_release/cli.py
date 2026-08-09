"""Command-line interface for the compact UCM release package."""

from __future__ import annotations

import argparse
import json
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
    scan.add_argument("--crane", default="crane")

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
    image_prepare = image_actions.add_parser("prepare")
    image_prepare.add_argument("--input", type=Path, required=True)
    image_prepare.add_argument("--wheel-dir", type=Path, required=True)
    image_prepare.add_argument("--expected-source-sha", required=True)
    image_prepare.add_argument("--base-authority", type=Path, required=True)
    image_prepare.add_argument("--base-index", type=Path, required=True)
    image_prepare.add_argument("--base-manifest", type=Path, required=True)
    image_prepare.add_argument("--base-config", type=Path, required=True)
    image_prepare.add_argument("--output-dir", type=Path, required=True)
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
                crane_binary=args.crane,
            )
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
