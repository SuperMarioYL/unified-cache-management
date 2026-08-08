"""Command-line interface for the compact UCM release package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import chart, core, wheel


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", type=Path, default=core.DEFAULT_RELEASE)
    parser.add_argument("--compatibility", type=Path, default=core.DEFAULT_COMPATIBILITY)
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
    inspect.add_argument("--source-kind", choices=("fixture", "production"), required=True)
    _paths(inspect)

    chart_parser = groups.add_parser("chart")
    chart_actions = chart_parser.add_subparsers(dest="action", required=True)
    package = chart_actions.add_parser("package")
    package.add_argument("--output-dir", type=Path, required=True)
    _paths(package)
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
            if args.require_publishable and result["eligible_wheel_count"] != result["declared_wheel_count"]:
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
