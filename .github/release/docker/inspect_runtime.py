#!/usr/bin/env python3
"""Record installed Python/package/ABI facts without claiming hardware success."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import struct
import subprocess
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
    if (
        not isinstance(expected, dict)
        or not expected
        or observed != expected
        or observed_raw
        != json.dumps(
            expected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    ):
        raise ValueError("runtime patch variant map differs from image recipe")
    return observed


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _elf_string(data: bytes, offset: int, size: int, label: str) -> str:
    limit = min(len(data), offset + size)
    end = data.find(b"\0", offset, limit)
    if offset < 0 or offset >= len(data) or end < 0:
        raise ValueError(f"installed ELF string table is invalid: {label}")
    return data[offset:end].decode("utf-8")


def _inspect_elf(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    label = str(path)
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError(f"installed native member is not ELF64 little-endian: {label}")
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    machine = header[1]
    names = {62: "EM_X86_64", 183: "EM_AARCH64"}
    if machine not in names:
        raise ValueError(f"installed ELF machine is unsupported: {label}")
    program_offset, program_size, program_count = header[4], header[8], header[9]
    if program_size != 56 or program_count < 1:
        raise ValueError(f"installed ELF program headers are invalid: {label}")
    loads: list[tuple[int, int, int]] = []
    dynamics: list[tuple[int, int]] = []
    for index in range(program_count):
        values = struct.unpack_from(
            "<IIQQQQQQ", data, program_offset + index * program_size
        )
        kind, _, file_offset, virtual_address, _, file_size, _, _ = values
        if file_offset + file_size > len(data):
            raise ValueError(f"installed ELF segment is truncated: {label}")
        if kind == 1:
            loads.append((virtual_address, file_offset, file_size))
        elif kind == 2:
            dynamics.append((file_offset, file_size))
    if len(dynamics) != 1:
        raise ValueError(f"installed ELF dynamic segment is ambiguous: {label}")
    dynamic_offset, dynamic_size = dynamics[0]
    entries: list[tuple[int, int]] = []
    for offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
        tag, value = struct.unpack_from("<QQ", data, offset)
        entries.append((tag, value))
        if tag == 0:
            break
    strings = [value for tag, value in entries if tag == 5]
    sizes = [value for tag, value in entries if tag == 10]
    if len(strings) != 1 or len(sizes) != 1:
        raise ValueError(f"installed ELF string table is ambiguous: {label}")
    string_offset = None
    for address, file_offset, file_size in loads:
        if address <= strings[0] < address + file_size:
            string_offset = file_offset + strings[0] - address
            break
    if string_offset is None:
        raise ValueError(f"installed ELF string table is outside PT_LOAD: {label}")
    needed = sorted(
        _elf_string(data, string_offset + value, sizes[0] - value, label)
        for tag, value in entries
        if tag == 1
    )
    return {"machine": names[machine], "needed": needed}


def _parse_ldd(
    member: str,
    direct_needed: list[str],
    output: str,
    installed_paths: dict[Path, str],
    *,
    external_required_dependencies: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    direct = set(direct_needed)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing: set[str] = set()
    declaration_fields = {
        "dependency",
        "provider",
        "expected_mount_root",
        "relation",
        "required_at",
    }
    declared_external: dict[str, dict[str, Any]] = {}
    for declaration in external_required_dependencies or []:
        if (
            not isinstance(declaration, dict)
            or set(declaration) != declaration_fields
            or declaration.get("relation") != "transitive"
            or declaration.get("required_at") != "device-runtime"
        ):
            raise ValueError("installed external-required declaration is invalid")
        dependency = declaration.get("dependency")
        if not isinstance(dependency, str) or dependency in declared_external:
            raise ValueError("installed external-required declarations are not unique")
        declared_external[dependency] = declaration
    patterns = (
        re.compile(r"^(\S+)\s+=>\s+not found$"),
        re.compile(r"^(\S+)\s+=>\s+(\S+)(?:\s+\(0x[0-9a-fA-F]+\))?$"),
        re.compile(r"^(\S+)\s+\(0x[0-9a-fA-F]+\)$"),
    )
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        missing_match = patterns[0].fullmatch(line)
        if missing_match is not None:
            missing.add(missing_match.group(1))
            continue
        arrow = patterns[1].fullmatch(line)
        located = patterns[2].fullmatch(line)
        if arrow:
            dependency, raw_path = arrow.groups()
        elif located:
            raw_path = located.group(1)
            if raw_path == "linux-vdso.so.1":
                dependency = raw_path
                if dependency in seen:
                    raise ValueError(
                        f"installed ldd output has duplicate dependency for {member}"
                    )
                seen.add(dependency)
                result.append(
                    {
                        "dependency": dependency,
                        "direct": dependency in direct,
                        "kind": "virtual",
                    }
                )
                continue
            basename = Path(raw_path).name
            dependency = basename if basename in direct else raw_path
        else:
            raise ValueError(f"installed ldd output is malformed for {member}: {line}")
        if dependency in seen:
            raise ValueError(
                f"installed ldd output has duplicate dependency for {member}"
            )
        seen.add(dependency)
        path = Path(raw_path).resolve()
        internal_member = installed_paths.get(path)
        if internal_member is not None:
            result.append(
                {
                    "dependency": dependency,
                    "direct": dependency in direct,
                    "kind": "wheel-member",
                    "member": internal_member,
                    "sha256": _sha256(path),
                }
            )
        elif path.is_file():
            result.append(
                {
                    "dependency": dependency,
                    "direct": dependency in direct,
                    "kind": "external",
                    "path": str(path),
                    "sha256": _sha256(path),
                }
            )
        else:
            raise ValueError(f"installed dependency path is not a file: {line}")
    unexpected = sorted(missing - set(declared_external))
    if unexpected:
        raise ValueError(
            f"installed dependency has unexpected unresolved entries for {member}: "
            f"{unexpected}"
        )
    for dependency in sorted(missing):
        declaration = declared_external[dependency]
        if dependency in direct:
            raise ValueError(
                f"installed direct dependency {dependency} cannot be external-required"
            )
        if dependency in seen:
            raise ValueError(
                f"installed dependency is both resolved and external-required: {dependency}"
            )
        seen.add(dependency)
        result.append(
            {
                **declaration,
                "direct": False,
                "kind": "external-required",
            }
        )
    observed_direct = {
        item["dependency"] for item in result if item["dependency"] in direct
    }
    missing_direct = sorted(direct - observed_direct)
    if missing_direct:
        raise ValueError(
            f"installed direct dependencies are not found for {member}: {missing_direct}"
        )
    return sorted(
        result, key=lambda item: (str(item["dependency"]), str(item.get("path", "")))
    )


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
    distribution = importlib.metadata.distribution("uc-manager")
    expected_members = expected.get("native_members")
    if not isinstance(expected_members, dict) or not expected_members:
        raise ValueError("real recipe native member map is empty")
    installed: dict[str, Path] = {}
    for component, member in expected_members.items():
        path = Path(distribution.locate_file(member)).resolve()
        if not path.is_file():
            raise ValueError(f"installed native member is missing: {member}")
        installed[component] = path
    installed_paths = {
        path: expected_members[component] for component, path in installed.items()
    }
    elf = {
        expected_members[component]: _inspect_elf(path)
        for component, path in installed.items()
    }
    machines = sorted({value["machine"] for value in elf.values()})
    dt_needed = {member: elf[member]["needed"] for member in sorted(elf)}
    directories = sorted({str(path.parent) for path in installed.values()})
    environment = {
        **os.environ,
        "LD_LIBRARY_PATH": ":".join(
            [*directories, os.environ.get("LD_LIBRARY_PATH", "")]
        ).rstrip(":"),
    }
    closure: dict[str, Any] = {}
    for component, path in installed.items():
        member = expected_members[component]
        expected_closure = expected.get("dependency_closure", {}).get(member, {})
        expected_resolutions = expected_closure.get("resolved_dependencies", [])
        external_required = [
            {
                key: resolution[key]
                for key in (
                    "dependency",
                    "provider",
                    "expected_mount_root",
                    "relation",
                    "required_at",
                )
            }
            for resolution in expected_resolutions
            if resolution.get("kind") == "external-required"
        ]
        completed = subprocess.run(
            ["ldd", str(path)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(f"ldd failed for installed native member: {member}")
        closure[member] = {
            "dt_needed": dt_needed[member],
            "resolved_dependencies": _parse_ldd(
                member,
                dt_needed[member],
                completed.stdout,
                installed_paths,
                external_required_dependencies=external_required,
            ),
            "unresolved_dependencies": [],
        }
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
        "elf_machines": machines,
        "dt_needed": dt_needed,
        "dependency_closure": closure,
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
