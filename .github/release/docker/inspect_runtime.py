#!/usr/bin/env python3
"""Record installed Python/package/ABI facts without claiming hardware success."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
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


def _runtime_patch_variants(payload: dict[str, Any]) -> dict[str, str]:
    expected = payload.get("runtime_patch_variants")
    observed_raw = os.environ.get("UCM_RUNTIME_PATCH_VARIANTS")
    try:
        observed = json.loads(observed_raw) if observed_raw is not None else None
    except json.JSONDecodeError as error:
        raise ValueError("runtime patch variant map is invalid JSON") from error
    if not isinstance(expected, dict) or not expected or observed != expected:
        raise ValueError("runtime patch variant map differs from image recipe")
    return observed


def _inspect_real(
    payload: dict[str, Any], install_result: dict[str, Any]
) -> dict[str, Any]:
    if (
        install_result.get("kind") != "ucm-real-install-result"
        or install_result.get("status") != "passed"
    ):
        raise ValueError(
            "real runtime inspection requires a successful offline install"
        )
    wheel = payload.get("wheel")
    expected = wheel.get("builder_evidence") if isinstance(wheel, dict) else None
    if not isinstance(expected, dict):
        raise ValueError("real recipe lacks builder native evidence")
    observed_variants = _runtime_patch_variants(payload)
    dist_name = wheel.get("dist_name") if isinstance(wheel, dict) else None
    if not isinstance(dist_name, str) or not dist_name:
        raise ValueError("real recipe wheel lacks dist_name")
    distribution = importlib.metadata.distribution(dist_name)
    expected_members = expected.get("native_members")
    if not isinstance(expected_members, dict) or not expected_members:
        raise ValueError("real recipe native member map is empty")
    for member_name in expected_members:
        path = Path(distribution.locate_file(member_name)).resolve()
        if not path.is_file():
            raise ValueError(f"installed native member is missing: {member_name}")
    expected_abi = wheel.get("python_abi")
    observed_abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if expected_abi != observed_abi:
        raise ValueError(
            f"Python ABI mismatch: expected {expected_abi}, observed {observed_abi}"
        )
    return {
        "schema_version": 1,
        "kind": "ucm-real-runtime-inspection",
        "python_version": platform.python_version(),
        "soabi": sysconfig.get_config_var("SOABI") or "unknown",
        "package_version": distribution.version,
        "runtime_patch_variants": observed_variants,
        "native_members": expected_members,
        "abi": {
            "expected_python_abi": expected_abi,
            "observed_python_abi": observed_abi,
            "status": "passed",
        },
        "accelerator_runtime": {
            "status": "external-required",
            "reason": "install-only image build cannot validate accelerator runtime",
        },
        "device": {
            "status": "external-required",
            "reason": "install-only image build cannot validate accelerator hardware",
        },
        "hardware_passed": False,
        "status": "external-required",
    }


def inspect(recipe_path: Path, install_path: Path) -> dict[str, Any]:
    recipe = _load(recipe_path)
    install_result = _load(install_path)
    payload = recipe.get("payload")
    if not isinstance(payload, dict) or install_result.get("status") != "passed":
        raise ValueError("runtime inspection requires a successful canonical install")
    if payload.get("candidate_kind") == "real-candidate":
        return _inspect_real(payload, install_result)
    expected_abi = payload.get("wheel", {}).get("python_abi")
    observed_abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if expected_abi != observed_abi:
        raise ValueError(
            f"Python ABI mismatch: expected {expected_abi}, observed {observed_abi}"
        )
    dist_name = payload.get("wheel", {}).get("dist_name") or "uc-manager"
    distribution = importlib.metadata.distribution(dist_name)
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
