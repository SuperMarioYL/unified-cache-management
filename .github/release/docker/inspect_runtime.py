#!/usr/bin/env python3
"""Record installed Python/package/ABI facts without claiming hardware success."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
import sysconfig
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def inspect(recipe_path: Path, install_path: Path) -> dict[str, Any]:
    recipe = _load(recipe_path)
    install_result = _load(install_path)
    payload = recipe.get("payload")
    if not isinstance(payload, dict) or install_result.get("status") != "passed":
        raise ValueError("runtime inspection requires a successful canonical install")
    expected_abi = payload.get("wheel", {}).get("python_abi")
    observed_abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if expected_abi != observed_abi:
        raise ValueError(
            f"Python ABI mismatch: expected {expected_abi}, observed {observed_abi}"
        )
    distribution = importlib.metadata.distribution("uc-manager")
    files = distribution.files or []
    shared_objects = sorted(
        str(file) for file in files if str(file).endswith((".so", ".pyd"))
    )
    if distribution.version != payload["wheel"]["version"]:
        raise ValueError("installed UCM version does not match recipe")
    return {
        "schema_version": 1,
        "kind": "ucm-runtime-inspection",
        "python_version": platform.python_version(),
        "soabi": sysconfig.get_config_var("SOABI") or "unknown",
        "package_version": distribution.version,
        "shared_objects": shared_objects,
        "abi": {
            "expected_python_abi": expected_abi,
            "observed_python_abi": observed_abi,
            "status": "passed",
        },
        "accelerator_runtime": {
            "status": "external-required",
            "reason": "fixture image build cannot validate the accelerator runtime",
        },
        "device": {
            "status": "external-required",
            "reason": "fixture image build cannot validate accelerator hardware",
        },
        "hardware_passed": False,
        "status": "external-required",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--install-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect(args.recipe, args.install_result)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
