"""File-oriented CLI for the trusted production release controller."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .common import ProductionError, load_json, write_json
from .config import load_config
from .tags import intent_document, parse_tag, reopen_intent, verify_ref_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ucm-release-production")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate")
    validate.add_argument("--config", required=True, type=Path)
    tag = commands.add_parser("tag")
    tag_commands = tag.add_subparsers(dest="tag_command", required=True)
    parse = tag_commands.add_parser("parse")
    parse.add_argument("--config", required=True, type=Path)
    parse.add_argument("--tag", required=True)
    parse.add_argument("--output", required=True, type=Path)
    refs = tag_commands.add_parser("verify-refs")
    refs.add_argument("--config", required=True, type=Path)
    refs.add_argument("--intent", required=True, type=Path)
    refs.add_argument("--snapshot", required=True, type=Path)
    refs.add_argument("--output", required=True, type=Path)
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
        if args.command == "tag" and args.tag_command == "parse":
            config = load_config(args.config)
            write_json(
                args.output,
                intent_document(parse_tag(args.tag, config)),
                "tag intent output",
            )
            return 0
        if args.command == "tag" and args.tag_command == "verify-refs":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            snapshot = load_json(args.snapshot, "ref snapshot")
            write_json(
                args.output,
                verify_ref_snapshot(intent, snapshot),
                "source identity output",
            )
            return 0
        raise ProductionError("unsupported command")
    except ProductionError as error:
        print(f"production release validation failed: {error}", file=sys.stderr)
        return 2
