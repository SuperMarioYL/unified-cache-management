#!/usr/bin/env python3
"""Verify that Docker's FROM input is the exact authorized platform subject."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


DIGEST = r"sha256:[0-9a-f]{64}"
SUBJECT_RE = re.compile(
    rf"[a-z0-9]+(?:[._:-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@{DIGEST}"
)


def _load(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r} in {path}")
            value[key] = item
        return value

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def verify(recipe_path: Path, base_image: str, target_platform: str) -> dict[str, Any]:
    recipe = _load(recipe_path)
    if set(recipe) != {"payload", "payload_sha256"}:
        raise ValueError(
            "recipe envelope must contain exactly payload and payload_sha256"
        )
    actual_recipe_sha256 = (
        "sha256:" + hashlib.sha256(_canonical(recipe["payload"])).hexdigest()
    )
    if recipe["payload_sha256"] != actual_recipe_sha256:
        raise ValueError("recipe payload digest mismatch")
    payload = recipe["payload"]
    base = payload.get("base") if isinstance(payload, dict) else None
    if not isinstance(base, dict):
        raise ValueError("recipe base must be an object")
    required = {
        "schema_version",
        "kind",
        "fixture_only",
        "repository",
        "index_digest",
        "platform",
        "subject",
    }
    if set(base) != required:
        raise ValueError("recipe base record has noncanonical fields")
    platform = base["platform"]
    if not isinstance(platform, dict) or set(platform) != {
        "os",
        "architecture",
        "manifest_digest",
        "config_digest",
    }:
        raise ValueError("recipe base platform has noncanonical fields")
    subject = f"{base['repository']}@{platform['manifest_digest']}"
    if SUBJECT_RE.fullmatch(subject) is None or base["subject"] != subject:
        raise ValueError(
            "authorized base subject is not an immutable repository digest"
        )
    expected_platform = f"{platform['os']}/{platform['architecture']}"
    if (
        target_platform != payload.get("target_platform")
        or target_platform != expected_platform
    ):
        raise ValueError("Docker target platform does not match the authorized recipe")
    if SUBJECT_RE.fullmatch(base_image) is None:
        raise ValueError("BASE_IMAGE must be an immutable repository@sha256 subject")
    if base_image != subject:
        raise ValueError("BASE_IMAGE does not match the authorized recipe subject")
    return {
        "schema_version": 1,
        "kind": "ucm-base-verification",
        "base_subject": subject,
        "target_platform": target_platform,
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--target-platform", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.recipe, args.base_image, args.target_platform)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
