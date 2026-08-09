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


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _direct_url(distribution: importlib.metadata.Distribution) -> dict[str, Any]:
    direct_url_path = Path(distribution._path) / "direct_url.json"
    return _load(direct_url_path)


def install_real(
    recipe_path: Path,
    authority_path: Path,
    wheelhouse: Path,
    lock_path: Path,
) -> dict[str, Any]:
    recipe = _load(recipe_path)
    authority = _load(authority_path)
    payload = recipe.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("candidate_kind") != "real-candidate"
        or recipe.get("payload_sha256")
        != "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    ):
        raise ValueError("real recipe payload digest mismatch")
    authority_payload = {
        key: value for key, value in authority.items() if key != "authority_sha256"
    }
    authority_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                authority_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    if (
        authority.get("kind") != "ucm-real-image-source-authority"
        or authority.get("candidate_kind") != "real-candidate"
        or authority.get("fixture_only") is not False
        or authority.get("authority_sha256") != authority_sha256
        or payload.get("authority_sha256") != authority_sha256
    ):
        raise ValueError("real image authority does not match recipe")
    wheel = payload.get("wheel")
    wrapt = payload.get("wrapt_wheel")
    dependency_lock = payload.get("dependency_lock")
    if not all(isinstance(value, dict) for value in (wheel, wrapt, dependency_lock)):
        raise ValueError("real recipe dependency authority is missing")
    ucm_path = wheelhouse / wheel["filename"]
    wrapt_path = wheelhouse / wrapt["filename"]
    expected_files = {ucm_path.name, wrapt_path.name, lock_path.name}
    if (
        not wheelhouse.is_dir()
        or {path.name for path in wheelhouse.iterdir() if path.is_file()}
        != expected_files
        or any(path.is_symlink() for path in wheelhouse.iterdir())
    ):
        raise ValueError("real wheelhouse must contain exactly two wheels and one lock")
    if _sha256(ucm_path) != wheel.get("sha256"):
        raise ValueError("UCM wheel SHA256 does not match real recipe")
    if _sha256(wrapt_path) != wrapt.get("sha256"):
        raise ValueError("wrapt wheel SHA256 does not match real recipe")
    if _sha256(lock_path) != dependency_lock.get("sha256"):
        raise ValueError("runtime dependency lock SHA256 does not match recipe")
    ucm_name, ucm_version, ucm_requires = _wheel_metadata(ucm_path)
    wrapt_name, wrapt_version, wrapt_requires = _wheel_metadata(wrapt_path)
    if (
        ucm_name != "uc-manager"
        or ucm_version != wheel.get("version")
        or ucm_requires != ["wrapt==1.17.2"]
        or wrapt_name != "wrapt"
        or wrapt_version != "1.17.2"
        or wrapt_requires
    ):
        raise ValueError("runtime wheel metadata differs from real recipe")
    expected_lock = (
        f"uc-manager @ file:///wheelhouse/{ucm_path.name} "
        f"--hash={wheel['sha256']}\n"
        f"wrapt @ file:///wheelhouse/{wrapt_path.name} "
        f"--hash={wrapt['sha256']}\n"
    )
    if lock_path.read_text(encoding="utf-8") != expected_lock:
        raise ValueError("runtime dependency lock bytes are noncanonical")
    preinstall_command = [
        sys.executable,
        "-m",
        "pip",
        "uninstall",
        "--yes",
        "uc-manager",
        "wrapt",
    ]
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links=/wheelhouse",
        "--require-hashes",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "-r",
        "/wheelhouse/requirements.lock",
    ]
    if (
        dependency_lock.get("preinstall_command", [])[1:] != preinstall_command[1:]
        or dependency_lock.get("pip_command", [])[1:] != command[1:]
    ):
        raise ValueError("real dependency commands differ from reviewed recipe")
    subprocess.run(preinstall_command, check=True)
    subprocess.run(command, check=True)
    try:
        ucm_distribution = importlib.metadata.distribution("uc-manager")
        wrapt_distribution = importlib.metadata.distribution("wrapt")
    except importlib.metadata.PackageNotFoundError as error:
        raise ValueError("installed UCM package scope is incomplete") from error
    packages = {
        "uc-manager": {
            "version": ucm_distribution.version,
            "requires_dist": list(ucm_distribution.requires or []),
        },
        "wrapt": {
            "version": wrapt_distribution.version,
            "requires_dist": list(wrapt_distribution.requires or []),
        },
    }
    if packages != {
        "uc-manager": {
            "version": ucm_version,
            "requires_dist": ["wrapt==1.17.2"],
        },
        "wrapt": {"version": "1.17.2", "requires_dist": []},
    }:
        raise ValueError("installed UCM package scope differs from mounted wheels")
    dependency_check = {
        "kind": "ucm-package-scope",
        "scope": ["uc-manager", "wrapt"],
        "packages": packages,
        "requirements": [
            {
                "owner": "uc-manager",
                "requirement": "wrapt==1.17.2",
                "dependency": "wrapt",
                "installed_version": "1.17.2",
                "status": "passed",
            }
        ],
        "status": "passed",
    }
    direct_urls = {
        "uc-manager": _direct_url(ucm_distribution),
        "wrapt": _direct_url(wrapt_distribution),
    }
    expected_direct = {
        "uc-manager": (ucm_path.name, wheel["sha256"]),
        "wrapt": (wrapt_path.name, wrapt["sha256"]),
    }
    for distribution, (filename, digest) in expected_direct.items():
        direct = direct_urls[distribution]
        if direct.get("url") != f"file:///wheelhouse/{filename}" or direct.get(
            "archive_info", {}
        ).get("hash") != "sha256=" + digest.removeprefix("sha256:"):
            raise ValueError(f"{distribution} direct_url does not bind mounted wheel")
    importlib.import_module("ucm")
    importlib.import_module("wrapt")
    return {
        "schema_version": 1,
        "kind": "ucm-real-install-result",
        "wheel_filename": ucm_path.name,
        "wheel_sha256": wheel["sha256"],
        "wrapt_filename": wrapt_path.name,
        "wrapt_sha256": wrapt["sha256"],
        "version": ucm_version,
        "preinstall_command": preinstall_command,
        "pip_command": command,
        "pip_check": "passed",
        "dependency_check": dependency_check,
        "direct_urls": direct_urls,
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
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fixture_arguments = (args.metadata, args.wheel)
        real_arguments = (args.authority, args.wheelhouse, args.lock)
        if all(value is not None for value in fixture_arguments) and all(
            value is None for value in real_arguments
        ):
            result = install(args.recipe, args.metadata, args.wheel)
        elif all(value is not None for value in real_arguments) and all(
            value is None for value in fixture_arguments
        ):
            result = install_real(
                args.recipe, args.authority, args.wheelhouse, args.lock
            )
        else:
            raise ValueError(
                "choose exactly fixture metadata/wheel or real authority/wheelhouse/lock"
            )
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
