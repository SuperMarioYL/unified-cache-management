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

    image_parser = groups.add_parser("image")
    image_actions = image_parser.add_subparsers(dest="action", required=True)
    image_verify = image_actions.add_parser("verify")
    image_verify.add_argument("--context", type=Path, required=True)
    image_verify.add_argument("--oci", type=Path, required=True)
    image_verify.add_argument(
        "--schema-dir", type=Path, default=core.DEFAULT_SCHEMA_DIR
    )
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
        elif (args.group, args.action) == ("image", "verify"):
            result = image.verify_oci(
                args.context,
                args.oci,
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
