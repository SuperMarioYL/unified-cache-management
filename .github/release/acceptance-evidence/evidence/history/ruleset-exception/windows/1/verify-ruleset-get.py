#!/usr/bin/env python3
"""Read the ruleset and refuse a stale body or revision before a reviewed PUT.

This script performs GET only. It archives each response and never edits rules,
Tags, Releases, or workflow runs. An optional context gate compares both ETag and
updated_at. It is a preflight check, not an atomic conditional-write guarantee.
"""

import argparse
import datetime
import json
from pathlib import Path
import subprocess


WRITABLE_KEYS = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")
ENDPOINT = "repos/SuperMarioYL/unified-cache-management/rulesets/20626631"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-body", required=True, type=Path)
    parser.add_argument("--expected-context", type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    args = parser.parse_args()
    result = subprocess.run(
        ["gh", "api", "-i", ENDPOINT], capture_output=True, text=True, check=False
    )
    prefix = args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".response.txt").write_text(result.stdout)
    if result.returncode != 0:
        raise SystemExit("STOP: ruleset GET failed; no write is allowed")
    header, body = result.stdout.split("\n\n", 1)
    actual = json.loads(body)
    prefix.with_suffix(".full.json").write_text(json.dumps(actual, indent=2) + "\n")
    headers = {
        key.lower(): value.strip()
        for line in header.splitlines()[1:]
        if ":" in line
        for key, value in [line.split(":", 1)]
    }
    context = {
        "endpoint": ENDPOINT,
        "response_status": header.splitlines()[0],
        "etag": headers.get("etag"),
        "updated_at": actual.get("updated_at"),
        "created_at": actual.get("created_at"),
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    prefix.with_suffix(".context.json").write_text(json.dumps(context, indent=2) + "\n")
    if actual.get("id") != 20626631 or actual.get("source") != "SuperMarioYL/unified-cache-management":
        raise SystemExit("STOP: ruleset identity changed")
    if not all(key in actual for key in WRITABLE_KEYS):
        raise SystemExit("STOP: incomplete ruleset body or insufficient permission")
    expected = json.loads(args.expected_body.read_text())
    mutable = {key: actual[key] for key in WRITABLE_KEYS}
    if mutable != expected:
        raise SystemExit("STOP: writable body differs from the reviewed expected body")
    if args.expected_context:
        prior = json.loads(args.expected_context.read_text())
        if any(not prior.get(key) or prior[key] != context[key] for key in ("etag", "updated_at")):
            raise SystemExit("STOP: ETag or updated_at changed since the reviewed snapshot")
    print(json.dumps({"readback_matches": True, "context_matches": bool(args.expected_context), "writes": 0}))


if __name__ == "__main__":
    main()
