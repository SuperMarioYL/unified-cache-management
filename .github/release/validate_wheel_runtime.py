#!/usr/bin/env python3
"""Validate every native member of an installed UCM backend Wheel."""

from __future__ import annotations

import ctypes
import importlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_LDD_DEPENDENCY = re.compile(r"^\s*(?P<soname>\S+)\s+=>\s+(?P<path>\S+)", re.MULTILINE)


def parse_ldd_dependencies(output: str) -> dict[str, set[str]]:
    dependencies: dict[str, set[str]] = {}
    for match in _LDD_DEPENDENCY.finditer(output):
        dependencies.setdefault(match.group("soname"), set()).add(match.group("path"))
    return dependencies


def distribution_native_members(
    distribution: importlib.metadata.Distribution,
) -> list[Path]:
    files = distribution.files
    if files is None:
        raise RuntimeError("installed UCM backend has no RECORD file list")
    members: set[Path] = set()
    for file in files:
        path = Path(distribution.locate_file(file)).resolve()
        if path.is_file():
            with path.open("rb") as source:
                if source.read(4) == b"\x7fELF":
                    members.add(path)
    return sorted(members)


def extension_module_name(package_root: Path, path: Path) -> str | None:
    if ".cpython-" not in path.name or not path.name.endswith(".so"):
        return None
    module = path.name.split(".cpython-", 1)[0]
    relative = path.relative_to(package_root).parent
    return ".".join(("ucm", *relative.parts, module))


def validate_external_resolution(
    resolved: dict[str, set[str]],
    expected_external: list[str],
    owned_members: set[Path],
) -> None:
    for soname in expected_external:
        paths = resolved.get(soname)
        if not paths:
            raise RuntimeError(
                f"expected external Runtime library was not resolved: {soname}"
            )
        for raw_path in paths:
            if Path(raw_path).resolve() in owned_members:
                raise RuntimeError(
                    f"external Runtime library unexpectedly resolves inside the Wheel: {soname}"
                )


def validate_runtime(
    package_root: Path,
    expected_external: list[str],
    members: list[Path],
) -> None:
    members = sorted(members)
    if not members:
        raise RuntimeError("installed UCM backend contains no native members")

    resolved: dict[str, set[str]] = {}
    for member in members:
        completed = subprocess.run(
            ["ldd", str(member)],
            check=False,
            capture_output=True,
            text=True,
        )
        output = completed.stdout + completed.stderr
        if completed.returncode != 0 or "not found" in output:
            raise RuntimeError(
                f"native dependency resolution failed for {member}:\n{output}"
            )
        for soname, paths in parse_ldd_dependencies(output).items():
            resolved.setdefault(soname, set()).update(paths)

    owned_members = {member.resolve() for member in members}
    validate_external_resolution(resolved, expected_external, owned_members)

    for member in members:
        module = (
            extension_module_name(package_root, member)
            if member.is_relative_to(package_root)
            else None
        )
        if module is not None:
            importlib.import_module(module)
        elif member.name.startswith("lib"):
            ctypes.CDLL(str(member), mode=os.RTLD_NOW | os.RTLD_LOCAL)

    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)


def validate_installed_distributions(
    expected_backend: str, expected_version: str, expected_meta_version: str | None
) -> importlib.metadata.Distribution:
    try:
        backend = importlib.metadata.distribution(expected_backend)
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError(
            "expected UCM backend distribution is not installed"
        ) from error
    if backend.version != expected_version:
        raise RuntimeError("installed UCM backend version does not match")
    if expected_meta_version is None:
        try:
            importlib.metadata.version("uc-manager")
        except importlib.metadata.PackageNotFoundError:
            return backend
        raise RuntimeError(
            "local backend validation found an existing uc-manager distribution"
        )
    if importlib.metadata.version("uc-manager") != expected_meta_version:
        raise RuntimeError("installed uc-manager meta version does not match")
    return backend


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def main() -> int:
    expected_backend = required_environment("EXPECTED_BACKEND_DISTRIBUTION")
    expected_version = required_environment("EXPECTED_UCM_VERSION")
    expected_meta_version = os.environ.get("EXPECTED_META_VERSION")
    expected = json.loads(os.environ.get("EXPECTED_EXTERNAL_LIBRARIES", "[]"))
    if not isinstance(expected, list) or any(
        not isinstance(value, str) or not value for value in expected
    ):
        raise RuntimeError("EXPECTED_EXTERNAL_LIBRARIES must be a JSON string list")
    backend = validate_installed_distributions(
        expected_backend, expected_version, expected_meta_version
    )
    package = importlib.import_module("ucm")
    package_paths = getattr(package, "__path__", None)
    if not package_paths:
        raise RuntimeError("installed UCM backend package cannot be located")
    validate_runtime(
        Path(next(iter(package_paths))),
        sorted(expected),
        distribution_native_members(backend),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
