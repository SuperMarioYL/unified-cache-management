"""File-oriented CLI for the trusted production release controller."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .common import ProductionError
from .config import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ucm-release-production")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate")
    validate.add_argument("--config", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "config" and args.config_command == "validate":
            config = load_config(args.config)
            print(
                json.dumps(
                    {
                        "kind": config["kind"],
                        "schema_version": config["schema_version"],
                        "release_line": config["release_line"],
                        "valid": True,
                    },
                    sort_keys=True,
                )
            )
            return 0
        raise ProductionError("unsupported command")
    except ProductionError as error:
        print(f"production release validation failed: {error}", file=sys.stderr)
        return 2
