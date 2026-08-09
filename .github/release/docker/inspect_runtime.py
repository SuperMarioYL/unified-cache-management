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
) -> list[dict[str, Any]]:
    direct = set(direct_needed)
    result: list[dict[str, Any]] = []
    patterns = (
        re.compile(r"^(\S+)\s+=>\s+(\S+)(?:\s+\(0x[0-9a-fA-F]+\))?$"),
        re.compile(r"^(\S+)\s+\(0x[0-9a-fA-F]+\)$"),
    )
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("=> not found"):
            raise ValueError(f"installed dependency is unresolved for {member}: {line}")
        arrow = patterns[0].fullmatch(line)
        located = patterns[1].fullmatch(line)
        if arrow:
            dependency, raw_path = arrow.groups()
        elif located:
            dependency = located.group(1)
            if dependency == "linux-vdso.so.1":
                result.append(
                    {
                        "dependency": dependency,
                        "direct": dependency in direct,
                        "kind": "virtual",
                    }
                )
                continue
            raw_path = dependency
        else:
            raise ValueError(f"installed ldd output is malformed for {member}: {line}")
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
    environment = {**os.environ, "LD_LIBRARY_PATH": ":".join(directories)}
    closure: dict[str, Any] = {}
    for component, path in installed.items():
        member = expected_members[component]
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
                member, dt_needed[member], completed.stdout, installed_paths
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
