"""Inspect a wheel and bind its bytes and metadata to one declared wheel spec."""

from __future__ import annotations

import ast
import base64
import copy
import csv
import email.parser
import hashlib
import io
import json
import re
import struct
import subprocess
import time
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from .core import (
    DEFAULT_RELEASE,
    DEFAULT_SCHEMA_DIR,
    REPO_ROOT,
    _fixture_wheel_specs,
    canonical_bytes,
    cpu_toolchain_authority,
    load_catalog,
    python_runtime_requirements,
    runtime_patch_manifest_sha256,
    sha256_value,
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIXTURE_MARKER = "ucm/_fixture_build.py"
COMPONENT_MANIFEST = "ucm/ucm-native-components.json"
RUNTIME_PATCH_MANIFEST = "ucm/integration/vllm/patch/runtime_patch_rules.json"
AUTHORITY_KIND = "ucm-native-build-authority"
CLOSURE_KIND = "ucm-linux-dependency-closure"
SOURCE_CONTEXT_KIND = "ucm-canonical-source-context"
SOURCE_CONTEXT_PREFIX = b"ucm-build-context-v2\0"
HOST_PATH_MARKERS = (
    b"/Users/",
    b"/home/runner/",
    b"/private/var/",
    b"/var/folders/",
    b"/tmp/",
)
NATIVE_MEMBER_DIRECTORIES = {
    "ucmtrans": "ucm/shared/trans",
    "metrics": "ucm/shared/metrics",
    "ucmmetrics": "ucm/shared/metrics",
    "ucmlogger": "ucm/shared/infra",
    "ucmnfsstore": "ucm/store/nfsstore",
    "ucmpcstore": "ucm/store/pcstore",
    "posixstore": "ucm/store/posix",
    "compressor": "ucm/store/compress",
    "cachestore": "ucm/store/cache",
    "emptystore": "ucm/store/empty",
    "fakestore": "ucm/store/fake",
    "ucmpipelinestore": "ucm/store/pipeline",
    "mooncakestore": "ucm/store/mooncakestore",
    "ds3fsstore": "ucm/store/ds3fs",
}
SHARED_LIBRARY_COMPONENTS = {
    "metrics",
    "posixstore",
    "compressor",
    "cachestore",
    "emptystore",
    "fakestore",
    "mooncakestore",
    "ds3fsstore",
}
EXTERNAL_REQUIRED_FIELDS = {
    "dependency",
    "provider",
    "expected_mount_root",
    "relation",
    "required_at",
}


_WHEEL_DECLARATION_FIELDS = (
    "spec_id",
    "profile_id",
    "accelerator",
    "accelerator_runtime",
    "npu_arch_or_na",
    "os",
    "cpu_arch",
    "python_version",
    "python_abi",
    "wheel_version",
    "wheel_platform",
    "binary_profile_id",
    "validation_targets",
    "required_native",
    "forbidden_native",
    "allowed_dt_needed",
    "external_required_dependencies",
)


def _validate_wheel_task(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("selected wheel task must be an object")
    task = copy.deepcopy(value)
    task_payload = {key: item for key, item in task.items() if key != "task_sha256"}
    if re.fullmatch(
        r"wheel-[0-9a-f]{64}", str(task.get("task_id"))
    ) is None or task.get("task_sha256") != sha256_value(task_payload):
        raise ValueError("wheel task hash mismatch")
    missing = [field for field in _WHEEL_DECLARATION_FIELDS if field not in task]
    if missing:
        raise ValueError(f"wheel task declaration fields are missing: {missing}")
    declaration = {
        field: copy.deepcopy(task[field]) for field in _WHEEL_DECLARATION_FIELDS
    }
    if task.get("declaration_sha256") != sha256_value(declaration):
        raise ValueError("wheel task declaration hash mismatch")
    dependency_lock = task.get("dependency_lock")
    if (
        not isinstance(dependency_lock, dict)
        or task.get("dependency_lock_sha256") != sha256_value(dependency_lock)
        or task.get("runtime_patch_manifest_sha256")
        != runtime_patch_manifest_sha256(task.get("runtime_patch_manifest"))
    ):
        raise ValueError("wheel task dependency authority is invalid")
    return task


def _selected_wheel_task(
    spec_id: str,
    *,
    task: dict[str, Any] | None = None,
    task_path: Path,
) -> dict[str, Any]:
    if (task is None) == (task_path is None):
        raise ValueError(
            "real wheel operation requires exactly one selected wheel task"
        )
    selected = (
        _validate_wheel_task(task)
        if task is not None
        else _validate_wheel_task(_canonical_record(task_path, "selected wheel task"))
    )
    if selected["spec_id"] != spec_id:
        raise ValueError("selected wheel task spec differs from requested spec")
    return selected


def _wheel_spec_from_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(task[field])
        for field in (*_WHEEL_DECLARATION_FIELDS, "declaration_sha256")
    } | {"build_eligible": task["build_eligible"]}


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_canonical(path: Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def _external_required_by_dependency(
    declarations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for declaration in declarations:
        if (
            not isinstance(declaration, dict)
            or set(declaration) != EXTERNAL_REQUIRED_FIELDS
        ):
            raise ValueError("external-required dependency declaration is invalid")
        dependency = declaration.get("dependency")
        if not isinstance(dependency, str) or dependency in result:
            raise ValueError("external-required dependency declarations are not unique")
        if (
            declaration.get("relation") != "transitive"
            or declaration.get("required_at") != "device-runtime"
            or not str(declaration.get("expected_mount_root", "")).startswith("/")
        ):
            raise ValueError("external-required dependency declaration is invalid")
        result[dependency] = declaration
    return result


def _external_required_resolution(declaration: dict[str, Any]) -> dict[str, Any]:
    return {
        **declaration,
        "direct": False,
        "kind": "external-required",
    }


def build_fixture_wheel(
    output_dir: Path,
    source_sha: str,
    profile_id: str,
    *,
    release_path: Path = DEFAULT_RELEASE,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    """Build one deterministic, source-bound wheel for the fork candidate lane."""
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("fixture wheel source SHA must be a full lowercase Git commit")
    release = load_catalog(release_path, schema_dir)
    specs = {item["spec_id"]: item for item in _fixture_wheel_specs(release)}
    if profile_id not in specs:
        raise ValueError(f"unknown fixture wheel profile: {profile_id}")
    spec = specs[profile_id]
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("fixture wheel output directory must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    version = release["ucm_version"]
    platform = cpu_toolchain_authority(spec["cpu_arch"]).wheel_arch
    tag = f"{spec['python_abi']}-{spec['python_abi']}-linux_{platform}"
    filename = f"uc_manager-{version}-{tag}.whl"
    dist_info = f"uc_manager-{version}.dist-info"
    members = {
        "ucm/__init__.py": f"__version__ = {version!r}\n",
        "ucm/_fixture_build.py": (
            f"SOURCE_SHA = {source_sha!r}\nPROFILE_ID = {profile_id!r}\n"
        ),
        f"{dist_info}/METADATA": "\n".join(
            [
                "Metadata-Version: 2.1",
                "Name: uc-manager",
                f"Version: {version}",
                *(
                    f"Requires-Dist: {requirement}"
                    for requirement in python_runtime_requirements(release)
                ),
                "",
            ]
        ),
        f"{dist_info}/WHEEL": "\n".join(
            [
                "Wheel-Version: 1.0",
                "Generator: ucm-fork-fixture-only",
                "Root-Is-Purelib: false",
                f"Tag: {tag}",
                "",
            ]
        ),
    }
    record_rows: list[list[str]] = []
    for name, content in members.items():
        raw = content.encode("utf-8")
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
            .decode("ascii")
            .rstrip("=")
        )
        record_rows.append([name, f"sha256={digest}", str(len(raw))])
    record_name = f"{dist_info}/RECORD"
    record_rows.append([record_name, "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(record_rows)
    members[record_name] = record_buffer.getvalue()

    wheel_path = output_dir / filename
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name])
    wheel_sha256 = _sha256(wheel_path)
    inspection = inspect_wheel(
        wheel_path,
        profile_id,
        wheel_sha256,
        "fixture",
        release_path=release_path,
        schema_dir=schema_dir,
    )
    inspection_path = output_dir / "wheel-inspection.json"
    _write_canonical(inspection_path, inspection)
    inspection_sha256 = _sha256(inspection_path)
    build_record = {
        "schema_version": 1,
        "kind": "ucm-fixture-wheel-build",
        "fixture_only": True,
        "publication_status": "unpublished",
        "publication_eligible": False,
        "source_sha": source_sha,
        "profile_id": profile_id,
        "wheel_sha256": wheel_sha256,
        "inspection_sha256": inspection_sha256,
    }
    _write_canonical(output_dir / "fixture-build.json", build_record)
    (output_dir / "wheel.sha256").write_text(wheel_sha256 + "\n", encoding="utf-8")
    return {
        "wheel_path": str(wheel_path),
        "wheel_sha256": wheel_sha256,
        "inspection_sha256": inspection_sha256,
        "inspection": inspection,
        "build_record": build_record,
    }


def _safe_wheel_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and "\\" not in name
        and all(part not in {"", ".", ".."} for part in name.split("/"))
        and path.as_posix() == name
    )


def _verify_record(archive: zipfile.ZipFile, record_name: str) -> None:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if len(names) != len(set(names)) or any(
        not _safe_wheel_name(name) for name in names
    ):
        raise ValueError("wheel contains duplicate, unsafe, or noncanonical members")
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    if any(len(row) != 3 for row in rows):
        raise ValueError("wheel RECORD must contain exactly three columns")
    by_name = {row[0]: row for row in rows}
    if len(by_name) != len(rows) or set(by_name) != set(names):
        raise ValueError("wheel RECORD does not exactly cover archive files")
    for name in names:
        _, encoded_digest, encoded_size = by_name[name]
        if name == record_name:
            if encoded_digest or encoded_size:
                raise ValueError(
                    "wheel RECORD self-entry must have empty digest and size"
                )
            continue
        if not encoded_digest.startswith("sha256="):
            raise ValueError(f"wheel RECORD entry lacks SHA256: {name}")
        encoded = encoded_digest.partition("=")[2]
        try:
            expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise ValueError(f"wheel RECORD has invalid SHA256: {name}") from error
        content = archive.read(name)
        if hashlib.sha256(content).digest() != expected:
            raise ValueError(f"wheel RECORD SHA256 mismatch: {name}")
        if not encoded_size.isdecimal() or int(encoded_size) != len(content):
            raise ValueError(f"wheel RECORD size mismatch: {name}")


def _unique_json(data: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _canonical_record(path: Path, label: str) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    value = _unique_json(raw, label)
    if raw != canonical_bytes(value) + b"\n":
        raise ValueError(f"{label} is noncanonical")
    return value


def _tool_wheel_authority(task: dict[str, Any]) -> dict[str, str]:
    records = task["dependency_lock"]["build_tools"]
    if (
        not isinstance(records, list)
        or not records
        or any(not isinstance(record, dict) for record in records)
    ):
        raise ValueError("build tool wheel authority is invalid")
    wheels = {record["filename"]: record["sha256"] for record in records}
    if len(wheels) != len(records):
        raise ValueError("build tool wheel authority is ambiguous")
    return dict(sorted(wheels.items()))


def _git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_object_digest(kind: str, data: bytes) -> bytes:
    header = f"{kind} {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).digest()  # noqa: S324 - Git SHA-1 object ID


def _source_context_digest(archive: bytes, commit_payload: bytes) -> str:
    material = (
        SOURCE_CONTEXT_PREFIX
        + len(archive).to_bytes(8, byteorder="big")
        + archive
        + commit_payload
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _commit_tree(commit_payload: bytes) -> str:
    """Strictly parse the one tree identity carried by a raw Git commit payload."""
    if b"\0" in commit_payload or b"\r" in commit_payload:
        raise ValueError("source commit payload contains invalid control bytes")
    header_block, separator, _ = commit_payload.partition(b"\n\n")
    if not separator or not header_block:
        raise ValueError("source commit payload has no complete header block")

    headers: list[tuple[bytes, bytes]] = []
    has_header = False
    for line in header_block.split(b"\n"):
        if line.startswith(b" "):
            if not has_header:
                raise ValueError("source commit payload has an orphan continuation")
            continue
        match = re.fullmatch(rb"([a-z][a-z0-9-]*) (.+)", line)
        if match is None:
            raise ValueError("source commit payload has a malformed header")
        headers.append((match.group(1), match.group(2)))
        has_header = True

    tree_headers = [value for name, value in headers if name == b"tree"]
    if not headers or headers[0][0] != b"tree" or len(tree_headers) != 1:
        raise ValueError(
            "source commit payload must have exactly one leading tree header"
        )
    tree = tree_headers[0]
    if re.fullmatch(rb"[0-9a-f]{40}", tree) is None:
        raise ValueError("source commit payload tree is invalid")
    return tree.decode("ascii")


def prepare_source_context(output_dir: Path, source_sha: str) -> dict[str, Any]:
    """Create the only accepted wheel-build context from exact Git objects."""
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("source context requires a full lowercase Git commit")
    if _git_value("rev-parse", "HEAD") != source_sha:
        raise ValueError("source context SHA does not match checked HEAD")
    source_tree = _git_value("rev-parse", f"{source_sha}^{{tree}}")
    if source_tree is None:
        raise ValueError("source context tree cannot be resolved")
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", source_sha],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("git archive failed for source context")
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("source context output directory must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "ucm-source.tar"
    archive_path.write_bytes(completed.stdout)
    commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "commit", source_sha],
        capture_output=True,
        check=False,
    )
    if commit.returncode != 0:
        raise ValueError("Git commit payload cannot be exported for source context")
    if _git_object_digest("commit", commit.stdout).hex() != source_sha:
        raise ValueError("exported Git commit payload differs from source SHA")
    if _commit_tree(commit.stdout) != source_tree:
        raise ValueError("exported Git commit tree differs from source tree")
    commit_payload_path = output_dir / "source-commit.payload"
    commit_payload_path.write_bytes(commit.stdout)
    manifest = {
        "schema_version": 1,
        "kind": SOURCE_CONTEXT_KIND,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "source_object_type": "commit",
        "source_commit_payload_sha256": "sha256:"
        + hashlib.sha256(commit.stdout).hexdigest(),
        "source_archive_sha256": "sha256:"
        + hashlib.sha256(completed.stdout).hexdigest(),
        "build_context_sha256": _source_context_digest(completed.stdout, commit.stdout),
    }
    manifest_path = output_dir / "source-context.json"
    _write_canonical(manifest_path, manifest)
    return {
        **manifest,
        "source_archive_path": str(archive_path),
        "source_commit_payload_path": str(commit_payload_path),
        "source_manifest_path": str(manifest_path),
    }


def _validate_build_authority(
    authority: dict[str, Any],
    spec_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "task_id",
        "spec_id",
        "profile_id",
        "cpu_arch",
        "platform",
        "build",
        "python_version",
        "python_abi",
        "wheel_version",
        "wheel_platform",
        "source_sha",
        "source_tree",
        "source_archive_sha256",
        "source_date_epoch",
        "task_sha256",
        "builder_coordinate",
        "builder_config_digest",
        "dependency_lock_sha256",
        "tool_wheels",
        "required_native",
        "forbidden_native",
        "runtime_patch_manifest_sha256",
        "runtime_requirements",
        "build_context_sha256",
    }
    if set(authority) != fields:
        raise ValueError("build authority fields are not exact")
    if DIGEST_RE.fullmatch(str(authority["build_context_sha256"])) is None:
        raise ValueError("build authority context digest is invalid")
    if DIGEST_RE.fullmatch(str(authority["source_archive_sha256"])) is None:
        raise ValueError("build authority source archive digest is invalid")
    root = task["builder"]["root"]
    expected = {
        "schema_version": 1,
        "kind": AUTHORITY_KIND,
        "task_id": task["task_id"],
        "spec_id": spec_id,
        "profile_id": task["profile_id"],
        "cpu_arch": task["cpu_arch"],
        "platform": task["platform"],
        "build": task["build"],
        "python_version": task["python_version"],
        "python_abi": task["python_abi"],
        "wheel_version": task["wheel_version"],
        "wheel_platform": task["wheel_platform"],
        "task_sha256": task["task_sha256"],
        "builder_coordinate": f"{root['repository']}@{root['manifest_digest']}",
        "builder_config_digest": root["config_digest"],
        "dependency_lock_sha256": task["dependency_lock_sha256"],
        "tool_wheels": _tool_wheel_authority(task),
        "required_native": task["required_native"],
        "forbidden_native": task["forbidden_native"],
        "runtime_patch_manifest_sha256": task["runtime_patch_manifest_sha256"],
        "runtime_requirements": task["runtime_requirements"],
    }
    for name, value in expected.items():
        if authority[name] != value:
            raise ValueError(f"build authority {name} differs from reviewed task")
    source_sha = authority["source_sha"]
    if re.fullmatch(r"[0-9a-f]{40}", str(source_sha)) is None:
        raise ValueError("build source authority SHA is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", str(authority["source_tree"])) is None:
        raise ValueError("build source authority tree is invalid")
    checked_head = _git_value("rev-parse", "HEAD")
    if checked_head is not None:
        if checked_head != source_sha:
            raise ValueError(
                "build source authority does not match checked source HEAD"
            )
        if (
            _git_value("rev-parse", f"{source_sha}^{{tree}}")
            != authority["source_tree"]
        ):
            raise ValueError(
                "build source authority tree does not match checked source"
            )
    _zip_timestamp(authority["source_date_epoch"])
    return authority


def _validate_dependency_closure(
    closure: dict[str, Any],
    raw_wheel_sha256: str,
    authority: dict[str, Any],
    native: dict[str, Any],
    archive: zipfile.ZipFile,
    *,
    external_required_dependencies: list[dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "spec_id",
        "raw_wheel_sha256",
        "build_context_sha256",
        "native_members",
        "unresolved_dependencies",
        "closure_sha256",
    }
    if set(closure) != fields:
        raise ValueError("dependency closure fields are not exact")
    digest_input = dict(closure)
    closure_digest = digest_input.pop("closure_sha256")
    if (
        closure_digest
        != "sha256:" + hashlib.sha256(canonical_bytes(digest_input)).hexdigest()
    ):
        raise ValueError("dependency closure digest is invalid")
    if (
        closure["schema_version"] != 1
        or closure["kind"] != CLOSURE_KIND
        or closure["spec_id"] != authority["spec_id"]
        or closure["raw_wheel_sha256"] != raw_wheel_sha256
        or closure["build_context_sha256"] != authority["build_context_sha256"]
    ):
        raise ValueError("dependency closure authority binding is invalid")
    if closure["unresolved_dependencies"] != []:
        raise ValueError(
            f"dependency closure has unresolved dependencies: {closure['unresolved_dependencies']}"
        )
    expected_names = set(native["native_artifacts"])
    records = closure["native_members"]
    if not isinstance(records, dict) or set(records) != expected_names:
        raise ValueError("dependency closure native member set is not exact")
    member_digests = {
        name: "sha256:" + hashlib.sha256(archive.read(name)).hexdigest()
        for name in expected_names
    }
    member_by_basename = {PurePosixPath(name).name: name for name in expected_names}
    declared_external = _external_required_by_dependency(external_required_dependencies)
    observed_external: set[str] = set()
    for name in sorted(expected_names):
        record = records[name]
        if not isinstance(record, dict) or set(record) != {
            "dt_needed",
            "resolved_dependencies",
            "unresolved_dependencies",
        }:
            raise ValueError(f"dependency closure record is invalid: {name}")
        if record["unresolved_dependencies"] != []:
            raise ValueError(
                f"dependency closure has unresolved dependencies for {name}"
            )
        needed = native["dt_needed"][name]
        if record["dt_needed"] != needed:
            missing = sorted(set(needed) - set(record.get("dt_needed", [])))
            if missing:
                raise ValueError(
                    f"dependency closure has unresolved entries for {name}: {missing}"
                )
            raise ValueError(f"dependency closure DT_NEEDED differs for {name}")
        resolutions = record["resolved_dependencies"]
        if not isinstance(resolutions, list) or not all(
            isinstance(resolution, dict) for resolution in resolutions
        ):
            raise ValueError(f"dependency closure resolutions are invalid: {name}")
        dependencies = [resolution.get("dependency") for resolution in resolutions]
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"dependency closure has duplicate resolutions: {name}")
        direct_resolutions = {
            resolution.get("dependency")
            for resolution in resolutions
            if resolution.get("direct") is True
        }
        if direct_resolutions != set(needed):
            missing = sorted(set(needed) - direct_resolutions)
            raise ValueError(f"dependency closure has unresolved entries: {missing}")
        for resolution in resolutions:
            dependency = resolution.get("dependency")
            if not isinstance(dependency, str) or resolution.get("direct") not in {
                True,
                False,
            }:
                raise ValueError(
                    f"dependency closure resolution identity is invalid: {name}"
                )
            internal_member = member_by_basename.get(PurePosixPath(dependency).name)
            if internal_member is not None:
                expected_resolution = {
                    "dependency": dependency,
                    "direct": dependency in needed,
                    "kind": "wheel-member",
                    "member": internal_member,
                    "sha256": member_digests[internal_member],
                }
                if resolution != expected_resolution:
                    raise ValueError(
                        f"dependency closure must resolve internal {dependency} "
                        "from the exact wheel member"
                    )
            elif resolution.get("kind") == "external-required":
                declaration = declared_external.get(str(dependency))
                if declaration is None or resolution != _external_required_resolution(
                    declaration
                ):
                    raise ValueError(
                        "dependency closure external-required resolution is invalid: "
                        f"{dependency}"
                    )
                observed_external.add(str(dependency))
            elif resolution.get("kind") == "virtual":
                if resolution != {
                    "dependency": "linux-vdso.so.1",
                    "direct": dependency in needed,
                    "kind": "virtual",
                }:
                    raise ValueError(
                        f"dependency closure virtual resolution is invalid: {dependency}"
                    )
            elif (
                not isinstance(resolution, dict)
                or set(resolution) != {"dependency", "direct", "kind", "path", "sha256"}
                or resolution["kind"] != "external"
                or not isinstance(resolution["path"], str)
                or not resolution["path"].startswith("/")
                or DIGEST_RE.fullmatch(str(resolution["sha256"])) is None
            ):
                raise ValueError(
                    f"dependency closure external resolution is invalid: {dependency}"
                )
    if observed_external != set(declared_external):
        raise ValueError(
            "dependency closure external-required set differs from declaration: "
            f"expected={sorted(declared_external)}, observed={sorted(observed_external)}"
        )
    return closure


def _python_extension_suffix(spec: dict[str, Any], task: dict[str, Any]) -> str:
    architecture = cpu_toolchain_authority(spec["cpu_arch"]).wheel_arch
    checks = task.get("builder", {}).get("checks", [])
    soabi_checks = [item for item in checks if item.get("kind") == "python-soabi"]
    if len(soabi_checks) != 1:
        raise ValueError("wheel task requires one Python SOABI authority")
    prefix = soabi_checks[0].get("prefix")
    version = task.get("python_version")
    abi = task.get("python_abi")
    if (
        not isinstance(prefix, str)
        or not isinstance(version, str)
        or not isinstance(abi, str)
        or abi != "cp" + version.replace(".", "")
        or abi != spec.get("python_abi")
    ):
        raise ValueError("wheel task Python ABI/SOABI authority is inconsistent")
    return f".{prefix}-{abi.removeprefix('cp')}-{architecture}-linux-gnu.so"


def _expected_native_members(
    spec: dict[str, Any], task: dict[str, Any]
) -> dict[str, str]:
    suffix = _python_extension_suffix(spec, task)
    known = set(spec["required_native"]) | set(spec["forbidden_native"])
    missing = sorted(set(spec["required_native"]) - set(NATIVE_MEMBER_DIRECTORIES))
    if missing:
        raise ValueError(f"native component archive paths are undeclared: {missing}")
    return {
        component: (
            f"{NATIVE_MEMBER_DIRECTORIES[component]}/lib{component}.so"
            if component in SHARED_LIBRARY_COMPONENTS
            else f"{NATIVE_MEMBER_DIRECTORIES[component]}/{component}{suffix}"
        )
        for component in known & set(NATIVE_MEMBER_DIRECTORIES)
    }


def _elf_string(data: bytes, offset: int, size: int, label: str) -> str:
    if offset < 0 or offset >= len(data):
        raise ValueError(f"ELF string offset is invalid in {label}")
    limit = min(len(data), offset + size)
    end = data.find(b"\0", offset, limit)
    if end < 0:
        raise ValueError(f"ELF string is unterminated in {label}")
    try:
        return data[offset:end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"ELF dynamic string is not UTF-8 in {label}") from error


def _inspect_elf(data: bytes, label: str) -> dict[str, Any]:
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ValueError(f"native member is not ELF: {label}")
    if data[4] != 2 or data[5] != 1 or data[6] != 1:
        raise ValueError(f"ELF must be 64-bit little-endian version 1: {label}")
    try:
        header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    except struct.error as error:
        raise ValueError(f"ELF header is truncated: {label}") from error
    machine = header[1]
    program_offset = header[4]
    program_size = header[8]
    program_count = header[9]
    if program_size != 56 or program_count < 1 or program_count > 1024:
        raise ValueError(f"ELF program header table is invalid: {label}")
    if program_offset + program_size * program_count > len(data):
        raise ValueError(f"ELF program header table is truncated: {label}")
    loads: list[tuple[int, int, int]] = []
    dynamics: list[tuple[int, int]] = []
    for index in range(program_count):
        values = struct.unpack_from(
            "<IIQQQQQQ", data, program_offset + index * program_size
        )
        kind, _, file_offset, virtual_address, _, file_size, _, _ = values
        if file_offset + file_size > len(data):
            raise ValueError(f"ELF program segment is truncated: {label}")
        if kind == 1:
            loads.append((virtual_address, file_offset, file_size))
        elif kind == 2:
            dynamics.append((file_offset, file_size))
    if len(dynamics) != 1:
        raise ValueError(f"ELF requires exactly one PT_DYNAMIC segment: {label}")
    dynamic_offset, dynamic_size = dynamics[0]
    if dynamic_size % 16 or dynamic_size > 1024 * 1024:
        raise ValueError(f"ELF dynamic segment is invalid: {label}")
    entries: list[tuple[int, int]] = []
    for offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
        tag, value = struct.unpack_from("<QQ", data, offset)
        entries.append((tag, value))
        if tag == 0:
            break
    if not entries or entries[-1][0] != 0:
        raise ValueError(f"ELF dynamic segment lacks DT_NULL: {label}")
    string_addresses = [value for tag, value in entries if tag == 5]
    string_sizes = [value for tag, value in entries if tag == 10]
    if len(string_addresses) != 1 or len(string_sizes) != 1:
        raise ValueError(f"ELF dynamic string table is ambiguous: {label}")
    string_address = string_addresses[0]
    string_size = string_sizes[0]
    string_offset: int | None = None
    for virtual_address, file_offset, file_size in loads:
        if virtual_address <= string_address < virtual_address + file_size:
            string_offset = file_offset + string_address - virtual_address
            break
    if string_offset is None or string_offset + string_size > len(data):
        raise ValueError(f"ELF dynamic string table is outside PT_LOAD: {label}")
    needed = sorted(
        _elf_string(data, string_offset + value, string_size - value, label)
        for tag, value in entries
        if tag == 1
    )
    runpaths = [
        _elf_string(data, string_offset + value, string_size - value, label)
        for tag, value in entries
        if tag in {15, 29}
    ]
    if runpaths:
        raise ValueError(f"ELF RPATH/RUNPATH is forbidden in {label}: {runpaths}")
    if any(marker in data for marker in HOST_PATH_MARKERS):
        raise ValueError(f"ELF source/path leakage detected in {label}")
    return {"machine": machine, "needed": needed}


def _verify_native_evidence(
    archive: zipfile.ZipFile,
    spec: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    required = spec["required_native"]
    forbidden = spec["forbidden_native"]
    expected_members = _expected_native_members(spec, task)
    extension_suffix = _python_extension_suffix(spec, task)
    components_by_member = {
        member: component for component, member in expected_members.items()
    }
    native_members: dict[str, str] = {}
    elf_evidence: dict[str, dict[str, Any]] = {}
    for item in archive.infolist():
        name = item.filename
        if item.is_dir() or not name.endswith(".so"):
            continue
        data = archive.read(name)
        if not data.startswith(b"\x7fELF"):
            continue
        component = components_by_member.get(name)
        if component is None:
            basename = PurePosixPath(name).name
            if any(
                basename == PurePosixPath(member).name
                or basename == component_name
                or basename == f"{component_name}.so"
                for component_name, member in expected_members.items()
            ):
                raise ValueError(f"native component archive path is not exact: {name}")
            component = next(
                (
                    forbidden_component
                    for forbidden_component in forbidden
                    if re.fullmatch(
                        rf"(?:lib)?{re.escape(forbidden_component)}(?:{re.escape(extension_suffix.removesuffix('.so'))})?\.so",
                        basename,
                    )
                ),
                None,
            )
        if component is None:
            raise ValueError(f"unclassified native wheel member: {name}")
        if component in native_members:
            raise ValueError(f"duplicate native component {component!r}")
        native_members[component] = name
        elf_evidence[name] = _inspect_elf(data, name)
    actual = set(native_members)
    missing = [item for item in required if item not in actual]
    present_forbidden = [item for item in forbidden if item in actual]
    extras = sorted(actual - set(required) - set(forbidden))
    if missing:
        raise ValueError(f"required native components are missing: {missing}")
    if present_forbidden:
        raise ValueError(
            f"forbidden native components are present: {present_forbidden}"
        )
    if extras or len(actual) != len(required):
        raise ValueError(f"native component set is not exact: extras={extras}")
    cpu_authority = cpu_toolchain_authority(spec["cpu_arch"])
    expected_machine = cpu_authority.elf_machine
    machine_name = cpu_authority.elf_machine_name
    wrong_machines = {
        name: evidence["machine"]
        for name, evidence in elf_evidence.items()
        if evidence["machine"] != expected_machine
    }
    if wrong_machines:
        raise ValueError(
            f"ELF machine does not match {spec['cpu_arch']}: {wrong_machines}"
        )
    allowed_needed = set(spec["allowed_dt_needed"])
    unapproved = {
        name: sorted(set(evidence["needed"]) - allowed_needed)
        for name, evidence in elf_evidence.items()
        if set(evidence["needed"]) - allowed_needed
    }
    if unapproved:
        raise ValueError(f"unapproved ELF DT_NEEDED entries: {unapproved}")
    ordered_members = {component: native_members[component] for component in required}
    return {
        "native_components": list(required),
        "native_members": ordered_members,
        "native_artifacts": list(ordered_members.values()),
        "elf_machines": [machine_name],
        "dt_needed": {
            name: elf_evidence[name]["needed"] for name in sorted(elf_evidence)
        },
    }


def _verify_component_manifest(
    archive: zipfile.ZipFile,
    spec: dict[str, Any],
    profile_id: str,
    source_sha: str,
    build_key: str,
) -> tuple[dict[str, Any], str]:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if names.count(COMPONENT_MANIFEST) != 1:
        raise ValueError("wheel requires exactly one native component manifest")
    raw = archive.read(COMPONENT_MANIFEST)
    manifest = _unique_json(raw, COMPONENT_MANIFEST)
    expected = {
        "schema_version": 1,
        "kind": "ucm-native-components",
        "profile_id": profile_id,
        "spec_id": spec["spec_id"],
        "source_sha": source_sha,
        "build_key": build_key,
        "version": spec["wheel_version"],
        "cpu_arch": spec["cpu_arch"],
        "required_native": spec["required_native"],
        "forbidden_native": spec["forbidden_native"],
        "installed_targets": spec["required_native"],
    }
    if manifest != expected:
        missing_native = sorted(
            set(spec["required_native"]) - set(manifest.get("installed_targets", []))
        )
        raise ValueError(
            "native component manifest does not match source/profile/architecture/"
            f"required native authority; missing={missing_native}"
        )
    if raw != canonical_bytes(manifest) + b"\n":
        raise ValueError("native component manifest is noncanonical")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return manifest, digest


def _verify_builder_candidate_evidence(
    archive: zipfile.ZipFile,
    wheel_metadata: email.message.Message,
    spec: dict[str, Any],
    profile_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if FIXTURE_MARKER in names:
        raise ValueError("builder candidate must not contain a fixture binding marker")
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise ValueError("builder candidate requires exactly one RECORD")
    _verify_record(archive, record_names[0])
    if wheel_metadata.get("Root-Is-Purelib", "").lower() != "false":
        raise ValueError("builder candidate must declare Root-Is-Purelib: false")
    build_names = [name for name in names if name.endswith(".dist-info/ucm-build.json")]
    if len(build_names) != 1:
        raise ValueError(
            "builder candidate requires exactly one embedded ucm-build.json"
        )
    binding = _unique_json(archive.read(build_names[0]), build_names[0])
    authority_names = [
        name for name in names if name.endswith(".dist-info/ucm-build-authority.json")
    ]
    closure_names = [
        name
        for name in names
        if name.endswith(".dist-info/ucm-dependency-closure.json")
    ]
    if len(authority_names) != 1 or len(closure_names) != 1:
        raise ValueError(
            "builder candidate requires one build authority and dependency closure"
        )
    authority = _validate_build_authority(
        _unique_json(archive.read(authority_names[0]), authority_names[0]),
        spec["spec_id"],
        task,
    )
    if archive.read(authority_names[0]) != canonical_bytes(authority) + b"\n":
        raise ValueError("embedded build authority is noncanonical")
    patch_names = [name for name in names if name == RUNTIME_PATCH_MANIFEST]
    if len(patch_names) != 1:
        raise ValueError(
            "builder candidate requires exactly one runtime patch manifest"
        )
    patch_raw = archive.read(patch_names[0])
    patch_value = _unique_json(patch_raw, patch_names[0])
    expected_patch = task["runtime_patch_manifest"]
    if (
        patch_value != expected_patch
        or patch_raw != canonical_bytes(expected_patch) + b"\n"
    ):
        raise ValueError("runtime patch manifest is malformed or differs from catalog")
    for rule in expected_patch["rules"]:
        for declaration in rule["imports"]:
            module_path = declaration["module"].replace(".", "/")
            if (
                f"{module_path}.py" not in names
                and f"{module_path}/__init__.py" not in names
            ):
                raise ValueError(
                    "runtime patch adapter is not packaged: " + declaration["module"]
                )
    patch_digest = "sha256:" + hashlib.sha256(patch_raw).hexdigest()
    if (
        patch_digest != authority["runtime_patch_manifest_sha256"]
        or patch_digest != task["runtime_patch_manifest_sha256"]
        or patch_digest != runtime_patch_manifest_sha256(expected_patch)
    ):
        raise ValueError("runtime patch manifest hash differs from build authority")
    required = {
        "schema_version",
        "task_id",
        "spec_id",
        "kind",
        "source_kind",
        "profile_id",
        "build",
        "source_sha",
        "build_key",
        "build_context_sha256",
        "source_tree",
        "source_archive_sha256",
        "builder_coordinate",
        "builder_config_digest",
        "dependency_lock_sha256",
        "tool_wheels",
        "source_date_epoch",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_abi",
        "python_version",
        "binary_profile_id",
        "wheel_version",
        "wheel_platform",
        "required_native",
        "forbidden_native",
        "allowed_dt_needed",
        "external_required_dependencies",
        "native_members",
        "component_manifest_sha256",
        "dependency_closure_sha256",
        "build_authority_sha256",
        "runtime_patch_manifest_sha256",
    }
    if set(binding) != required:
        raise ValueError(
            "embedded build binding fields mismatch: "
            f"missing={sorted(required - set(binding))}, extra={sorted(set(binding) - required)}"
        )
    if (
        binding["schema_version"] != 1
        or binding["kind"] != "ucm-native-wheel-build"
        or binding["source_kind"] != "builder-candidate"
    ):
        raise ValueError("embedded build binding identity is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", str(binding["source_sha"])) is None:
        raise ValueError("embedded build binding requires immutable source_commit")
    if binding["build_key"] != task["task_sha256"]:
        raise ValueError("embedded build binding build_key is not canonical")
    if (
        not isinstance(binding["source_date_epoch"], int)
        or isinstance(binding["source_date_epoch"], bool)
        or binding["source_date_epoch"] < 315532800
    ):
        raise ValueError("embedded build binding SOURCE_DATE_EPOCH is invalid")
    bound_fields = (
        "spec_id",
        "accelerator",
        "accelerator_runtime",
        "npu_arch_or_na",
        "os",
        "cpu_arch",
        "python_abi",
        "binary_profile_id",
        "wheel_version",
        "wheel_platform",
        "required_native",
        "forbidden_native",
        "allowed_dt_needed",
        "external_required_dependencies",
    )
    for field in bound_fields:
        if binding[field] != spec[field]:
            raise ValueError(
                f"embedded build binding {field} does not match planned spec: "
                f"{binding[field]!r} != {spec[field]!r}"
            )
    if binding["profile_id"] != profile_id:
        raise ValueError(
            "embedded build binding profile_id does not match planned spec"
        )
    for field in ("task_id", "build", "python_version"):
        if binding[field] != task[field]:
            raise ValueError(f"embedded build binding {field} differs from wheel task")
    native = _verify_native_evidence(archive, spec, task)
    closure_raw = archive.read(closure_names[0])
    closure_value = _unique_json(closure_raw, closure_names[0])
    if closure_raw != canonical_bytes(closure_value) + b"\n":
        raise ValueError("embedded dependency closure is noncanonical")
    closure = _validate_dependency_closure(
        closure_value,
        closure_value["raw_wheel_sha256"],
        authority,
        native,
        archive,
        external_required_dependencies=spec["external_required_dependencies"],
    )
    _, component_digest = _verify_component_manifest(
        archive,
        spec,
        profile_id,
        binding["source_sha"],
        binding["build_key"],
    )
    if binding["component_manifest_sha256"] != component_digest:
        raise ValueError("embedded component manifest digest mismatch")
    if binding["native_members"] != native["native_members"]:
        raise ValueError("embedded native member map does not match wheel bytes")
    authority_digest = (
        "sha256:" + hashlib.sha256(archive.read(authority_names[0])).hexdigest()
    )
    bound_authority = {
        "source_sha": authority["source_sha"],
        "build_key": authority["task_sha256"],
        "build_context_sha256": authority["build_context_sha256"],
        "source_tree": authority["source_tree"],
        "source_archive_sha256": authority["source_archive_sha256"],
        "builder_coordinate": authority["builder_coordinate"],
        "builder_config_digest": authority["builder_config_digest"],
        "dependency_lock_sha256": authority["dependency_lock_sha256"],
        "tool_wheels": authority["tool_wheels"],
        "build_authority_sha256": authority_digest,
        "dependency_closure_sha256": closure["closure_sha256"],
        "runtime_patch_manifest_sha256": patch_digest,
    }
    for field, value in bound_authority.items():
        if binding[field] != value:
            raise ValueError(f"embedded build binding {field} differs from authority")
    if archive.read(build_names[0]) != canonical_bytes(binding) + b"\n":
        raise ValueError("embedded build binding is noncanonical")
    return {
        "source_commit": binding["source_sha"],
        "build_context_digest": binding["build_context_sha256"],
        "build_key": binding["build_key"],
        "source_date_epoch": binding["source_date_epoch"],
        "runtime_patch_manifest_sha256": patch_digest,
        **{**native, "unresolved_dependencies": closure["unresolved_dependencies"]},
        "record_status": "passed",
    }


def _canonical_metadata(version: str, dependencies: list[str]) -> bytes:
    lines = [
        "Metadata-Version: 2.1",
        "Name: uc-manager",
        f"Version: {version}",
        "Summary: Unified Cache Management",
        "Requires-Python: >=3.10",
        *(f"Requires-Dist: {dependency}" for dependency in dependencies),
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _canonical_wheel_metadata(tag: str) -> bytes:
    return "\n".join(
        [
            "Wheel-Version: 1.0",
            "Generator: ucm-release-sealer-v1",
            "Root-Is-Purelib: false",
            f"Tag: {tag}",
            "",
        ]
    ).encode("utf-8")


def _record_bytes(members: dict[str, bytes], record_name: str) -> bytes:
    rows: list[list[str]] = []
    for name in sorted(members):
        content = members[name]
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .decode("ascii")
            .rstrip("=")
        )
        rows.append([name, f"sha256={digest}", str(len(content))])
    rows.append([record_name, "", ""])
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or not 315532800 <= source_date_epoch <= 4354819199
    ):
        raise ValueError("SOURCE_DATE_EPOCH must fit the canonical ZIP timestamp range")
    values = list(time.gmtime(source_date_epoch)[:6])
    values[5] -= values[5] % 2
    return tuple(values)  # type: ignore[return-value]


def _check_member_path_leakage(name: str, data: bytes) -> None:
    if any(marker in data for marker in HOST_PATH_MARKERS):
        raise ValueError(f"source/path leakage detected in wheel member: {name}")


def _expected_wheel_tag(spec: dict[str, Any]) -> str:
    architecture = cpu_toolchain_authority(spec["cpu_arch"]).wheel_arch
    return (
        f"{spec['python_abi']}-{spec['python_abi']}-"
        f"{spec['wheel_platform']}_{architecture}"
    )


def _verify_canonical_builder_archive(
    archive: zipfile.ZipFile,
    binding: dict[str, Any],
    metadata_name: str,
    wheel_name: str,
    record_name: str,
    dependencies: list[str],
) -> None:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if names != sorted(names):
        raise ValueError("sealed wheel member order is noncanonical")
    expected_timestamp = _zip_timestamp(binding["source_date_epoch"])
    for item in infos:
        if item.is_dir():
            raise ValueError("sealed wheel must not contain directory entries")
        if item.date_time != expected_timestamp:
            raise ValueError(f"sealed wheel timestamp is noncanonical: {item.filename}")
        if item.create_system != 3 or item.external_attr >> 16 != 0o644:
            raise ValueError(f"sealed wheel mode is noncanonical: {item.filename}")
        if item.compress_type != zipfile.ZIP_DEFLATED:
            raise ValueError(
                f"sealed wheel compression is noncanonical: {item.filename}"
            )
        if item.extra or item.comment:
            raise ValueError(
                f"sealed wheel ZIP metadata is noncanonical: {item.filename}"
            )
    if archive.comment:
        raise ValueError("sealed wheel ZIP comment is forbidden")
    tag = _expected_wheel_tag(binding)
    if archive.read(metadata_name) != _canonical_metadata(
        binding["wheel_version"], dependencies
    ):
        raise ValueError("sealed wheel METADATA bytes are noncanonical")
    if archive.read(wheel_name) != _canonical_wheel_metadata(tag):
        raise ValueError("sealed wheel WHEEL bytes are noncanonical")
    expected_record = _record_bytes(
        {name: archive.read(name) for name in names if name != record_name},
        record_name,
    )
    if archive.read(record_name) != expected_record:
        raise ValueError("sealed wheel RECORD bytes are noncanonical")


def _verify_fixture_binding(
    archive: zipfile.ZipFile, spec: dict[str, Any]
) -> dict[str, str]:
    """Parse the unique canonical fixture marker as literals without execution."""
    names = [item.filename for item in archive.infolist() if not item.is_dir()]
    if len(names) != len(set(names)) or any(
        not _safe_wheel_name(name) for name in names
    ):
        raise ValueError("fixture wheel contains duplicate or unsafe members")
    metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
    wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(metadata_names) != 1 or len(wheel_names) != 1 or len(record_names) != 1:
        raise ValueError("fixture wheel requires one METADATA, WHEEL, and RECORD")
    dist_info = record_names[0].removesuffix("/RECORD")
    expected_names = {
        "ucm/__init__.py",
        FIXTURE_MARKER,
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/RECORD",
    }
    if set(names) != expected_names or names.count(FIXTURE_MARKER) != 1:
        raise ValueError("fixture wheel requires exactly the canonical member set")
    _verify_record(archive, record_names[0])
    raw = archive.read(FIXTURE_MARKER)
    try:
        text = raw.decode("utf-8")
        module = ast.parse(text, filename=FIXTURE_MARKER, mode="exec")
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ValueError(f"fixture binding marker is invalid: {error}") from error
    values: dict[str, str] = {}
    for statement in module.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.Constant)
            or not isinstance(statement.value.value, str)
        ):
            raise ValueError("fixture binding accepts only literal assignments")
        name = statement.targets[0].id
        if name not in {"SOURCE_SHA", "PROFILE_ID"} or name in values:
            raise ValueError("fixture binding has duplicate or extra fields")
        values[name] = statement.value.value
    if set(values) != {"SOURCE_SHA", "PROFILE_ID"}:
        raise ValueError("fixture binding is missing required fields")
    source_sha = values["SOURCE_SHA"]
    profile_id = values["PROFILE_ID"]
    canonical = f"SOURCE_SHA = {source_sha!r}\nPROFILE_ID = {profile_id!r}\n"
    if text != canonical:
        raise ValueError("fixture binding marker bytes are noncanonical")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("fixture binding source SHA is invalid")
    if profile_id != spec["spec_id"]:
        raise ValueError("fixture binding profile does not match the planned spec")
    return {
        "source_commit": source_sha,
        "profile_id": profile_id,
        "marker_status": "passed",
    }


def inspect_wheel(
    path: Path,
    spec_id: str,
    expected_sha256: str,
    source_kind: str,
    *,
    task: dict[str, Any] | None = None,
    task_path: Path | None = None,
    release_path: Path = DEFAULT_RELEASE,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    if source_kind not in {"fixture", "builder-candidate"}:
        raise ValueError("source_kind must be fixture or builder-candidate")
    if DIGEST_RE.fullmatch(expected_sha256) is None:
        raise ValueError("expected SHA256 must be sha256:<64 lowercase hex>")
    release: dict[str, Any] | None = None
    selected_task: dict[str, Any] | None = None
    if source_kind == "builder-candidate":
        selected_task = _selected_wheel_task(spec_id, task=task, task_path=task_path)
        spec = _wheel_spec_from_task(selected_task)
        runtime_requirements = selected_task["runtime_requirements"]
        expected_version = selected_task["wheel_version"]
        profile_id = selected_task["profile_id"]
    else:
        if task is not None or task_path is not None:
            raise ValueError("fixture wheel inspection does not accept a real task")
        release = load_catalog(release_path, schema_dir)
        specs = {item["spec_id"]: item for item in _fixture_wheel_specs(release)}
        if spec_id not in specs:
            raise ValueError(f"unknown fixture wheel spec: {spec_id}")
        spec = specs[spec_id]
        runtime_requirements = python_runtime_requirements(release)
        expected_version = release["ucm_version"]
        profile_id = spec_id
    if source_kind == "builder-candidate" and not spec["build_eligible"]:
        raise ValueError(
            "builder candidate planned spec has unresolved locks or runner"
        )
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"wheel SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    raw_wheel = Path(path).read_bytes()
    end_record = raw_wheel.rfind(b"PK\x05\x06")
    if end_record < 0 or end_record + 22 > len(raw_wheel):
        raise ValueError("wheel ZIP end record is missing")
    comment_size = int.from_bytes(
        raw_wheel[end_record + 20 : end_record + 22], "little"
    )
    if end_record + 22 + comment_size != len(raw_wheel):
        raise ValueError("wheel contains trailing bytes after the ZIP end record")
    try:
        with zipfile.ZipFile(path) as integrity_archive:
            bad_member = integrity_archive.testzip()
    except (zipfile.BadZipFile, zlib.error) as error:
        raise ValueError(f"wheel ZIP is corrupt: {error}") from error
    if bad_member is not None:
        raise ValueError(f"wheel CRC is corrupt: {bad_member}")
    try:
        filename_name, filename_version, _, filename_tags = parse_wheel_filename(
            path.name
        )
    except Exception as error:
        raise ValueError(f"invalid wheel filename: {error}") from error
    builder_evidence: dict[str, Any] | None = None
    fixture_binding: dict[str, str] | None = None
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        wheel_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        ]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError(
                "wheel must contain exactly one METADATA and one WHEEL file"
            )
        parser = email.parser.Parser()
        metadata = parser.parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        wheel_metadata = parser.parsestr(archive.read(wheel_names[0]).decode("utf-8"))
        if source_kind == "builder-candidate":
            assert selected_task is not None
            builder_evidence = _verify_builder_candidate_evidence(
                archive, wheel_metadata, spec, profile_id, selected_task
            )
            build_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/ucm-build.json")
            )
            record_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/RECORD")
            )
            binding = _unique_json(archive.read(build_name), build_name)
            _verify_canonical_builder_archive(
                archive,
                binding,
                metadata_names[0],
                wheel_names[0],
                record_name,
                runtime_requirements,
            )
            for name in archive.namelist():
                if not name.endswith(".dist-info/RECORD"):
                    _check_member_path_leakage(name, archive.read(name))
        else:
            fixture_binding = _verify_fixture_binding(archive, spec)
    distribution = metadata.get("Name", "")
    version = metadata.get("Version", "")
    if canonicalize_name(distribution) != "uc-manager":
        raise ValueError(f"unexpected wheel distribution: {distribution}")
    if canonicalize_name(distribution) != canonicalize_name(str(filename_name)):
        raise ValueError("METADATA distribution does not match wheel filename")
    if version != str(filename_version):
        raise ValueError("METADATA version does not match wheel filename")
    if version != expected_version:
        raise ValueError(
            f"wheel version {version} does not match planned version {expected_version}"
        )
    filename_tag_strings = {str(tag) for tag in filename_tags}
    metadata_tags = set(wheel_metadata.get_all("Tag", []))
    if not metadata_tags or metadata_tags != filename_tag_strings:
        raise ValueError("WHEEL tags do not match wheel filename tags")
    if source_kind == "builder-candidate":
        expected_tags = {_expected_wheel_tag(spec)}
    else:
        architecture = cpu_toolchain_authority(spec["cpu_arch"]).wheel_arch
        expected_tags = {
            f"{spec['python_abi']}-{spec['python_abi']}-linux_{architecture}"
        }
    if filename_tag_strings != expected_tags:
        raise ValueError(
            "wheel tags do not match declared Python ABI and CPU architecture"
        )
    requires_dist = metadata.get_all("Requires-Dist", [])
    if requires_dist != runtime_requirements:
        raise ValueError("wheel runtime dependencies do not match selected authority")
    result = {
        "schema_version": 1,
        "kind": "ucm-wheel-inspection",
        "source_kind": source_kind,
        "spec_id": spec_id,
        "filename": path.name,
        "sha256": actual_sha256,
        "size": path.stat().st_size,
        "distribution": distribution,
        "version": version,
        "tags": sorted(filename_tag_strings),
        "requires_dist": requires_dist,
        "python_abi": spec["python_abi"],
        "cpu_arch": spec["cpu_arch"],
        "declaration_sha256": spec["declaration_sha256"],
        "status": "fixture-only" if source_kind == "fixture" else "candidate-inspected",
        "trust_level": (
            "fixture-only"
            if source_kind == "fixture"
            else "unpublished-builder-candidate"
        ),
        "published": False,
        "publication_eligible": False,
    }
    if builder_evidence is not None:
        result["builder_evidence"] = builder_evidence
        result["runtime_patch_manifest_sha256"] = builder_evidence[
            "runtime_patch_manifest_sha256"
        ]
    if fixture_binding is not None:
        result["fixture_binding"] = fixture_binding
    return result
