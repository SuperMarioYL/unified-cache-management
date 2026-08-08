#!/usr/bin/env python3
"""Install one exact UCM wheel with its ordinary Python dependency metadata."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any


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


def _wheel_metadata(path: Path) -> tuple[str, str, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1 or len(names) != len(set(names)):
                raise ValueError(
                    "wheel requires one METADATA member and unique members"
                )
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise ValueError(f"cannot inspect wheel metadata: {error}") from error
    return (
        metadata.get("Name", ""),
        metadata.get("Version", ""),
        sorted(metadata.get_all("Requires-Dist", [])),
    )


def install(recipe_path: Path, metadata_path: Path, wheel_path: Path) -> dict[str, Any]:
    recipe = _load(recipe_path)
    metadata = _load(metadata_path)
    payload = recipe.get("payload")
    if not isinstance(payload, dict) or recipe.get("payload_sha256") != (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    ):
        raise ValueError("recipe payload digest mismatch")
    wheel = payload.get("wheel")
    if not isinstance(wheel, dict):
        raise ValueError("recipe wheel must be an object")
    if (
        metadata.get("wheel_record")
        != metadata.get("source_case", {}).get("wheel_records", [None])[0]
    ):
        raise ValueError(
            "metadata does not retain the exact Task 2 wheel inspection record"
        )
    if wheel_path.name != wheel.get("filename"):
        raise ValueError("wheel filename does not match recipe")
    actual_sha256 = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    if actual_sha256 != wheel.get("sha256"):
        raise ValueError("wheel SHA256 does not match recipe")
    distribution, version, requires_dist = _wheel_metadata(wheel_path)
    if distribution != "uc-manager" or version != wheel.get("version"):
        raise ValueError("wheel distribution/version does not match recipe")
    if requires_dist != ["wrapt==1.17.2"] or requires_dist != wheel.get(
        "requires_dist"
    ):
        raise ValueError("wheel must declare ordinary Requires-Dist wrapt==1.17.2")

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--only-binary=:all:",
        str(wheel_path),
    ]
    subprocess.run(command, check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    ucm_distribution = importlib.metadata.distribution("uc-manager")
    wrapt_distribution = importlib.metadata.distribution("wrapt")
    direct_url_path = Path(ucm_distribution._path) / "direct_url.json"
    direct_url = _load(direct_url_path)
    archive_hash = direct_url.get("archive_info", {}).get("hash")
    if archive_hash != "sha256=" + actual_sha256.removeprefix("sha256:"):
        raise ValueError("installed direct_url archive hash does not match wheel")
    importlib.import_module("ucm")
    importlib.import_module("wrapt")
    if ucm_distribution.version != version or wrapt_distribution.version != "1.17.2":
        raise ValueError("installed package versions do not match recipe metadata")
    return {
        "schema_version": 1,
        "kind": "ucm-install-result",
        "wheel_filename": wheel_path.name,
        "wheel_sha256": actual_sha256,
        "version": version,
        "requires_dist": requires_dist,
        "pip_command": command,
        "pip_check": "passed",
        "direct_url": direct_url,
        "installed_packages": {
            "uc-manager": ucm_distribution.version,
            "wrapt": wrapt_distribution.version,
        },
        "imports": {"ucm": "passed", "wrapt": "passed"},
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = install(args.recipe, args.metadata, args.wheel)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
