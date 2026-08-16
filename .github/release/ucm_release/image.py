from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from . import core as release_core
from . import registry
from . import wheel as wheel_artifact
from .core import sha256_value

DOCKER_ROOT = Path(__file__).resolve().parents[1] / "docker"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY_RE = re.compile('[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+')  # fmt: skip  # noqa: E501
FROM_RE = re.compile('(?im)^\\s*FROM(?:\\s+--[^\\s]+)*\\s+(?P<base>[^\\s]+)(?:\\s+AS\\s+(?P<alias>[A-Za-z0-9_.-]+))?\\s*$')  # fmt: skip  # noqa: E501
COPY_FROM_RE = re.compile('(?im)^\\s*COPY\\s+(?:--[^\\s]+\\s+)*--from=(?P<stage>[^\\s]+)\\s+')  # fmt: skip  # noqa: E501
REAL_INSTALL_TARGET = "runtime-real"
OCI_INDEX_MEDIA_TYPES = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}  # fmt: skip  # noqa: E501
OCI_MANIFEST_MEDIA_TYPES = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}  # fmt: skip  # noqa: E501
FIXTURE_IMAGE_TOOLCHAIN_AUTHORITY = {'schema_version': 1, 'kind': 'ucm-fixture-image-toolchain-authority', 'buildx_version': 'v0.19.2', 'buildx_linux_sha256': {'amd64': 'sha256:a5ff61c0b6d2c8ee20964a9d6dac7a7a6383c4a4a0ee8d354e983917578306ea', 'arm64': 'sha256:bd54f0e28c29789da1679bad2dd94c1923786ccd2cd80dd3a0a1d560a6baf10c'}, 'buildkit_image': 'moby/buildkit:v0.18.2@sha256:86c0ad9d1137c186e9d455912167df20e530bdf7f7c19de802e892bb8ca16552'}  # fmt: skip  # noqa: E501
REAL_DETERMINISTIC_FLAGS = ['--provenance=false', '--sbom=false', 'oci-mediatypes=true', 'rewrite-timestamp=true']  # fmt: skip  # noqa: E501


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) != keys:
        raise ValueError(f'{label} fields mismatch: missing={sorted(keys - set(value))}, extra={sorted(set(value) - keys)}')  # fmt: skip  # noqa: E501
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase sha256:<64 hex>")
    return value


def validate_image_toolchain_authority(value: object) -> dict[str, Any]:
    authority = _exact(value, {'schema_version', 'kind', 'buildx_version', 'buildx_linux_sha256', 'buildkit_image'}, 'fixture image toolchain authority')  # fmt: skip  # noqa: E501
    if (
        authority["schema_version"] != 1
        or authority["kind"] != "ucm-fixture-image-toolchain-authority"
        or not isinstance(authority["buildx_version"], str)
        or re.fullmatch(
            r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            authority["buildx_version"],
        )
        is None
    ):
        raise ValueError("fixture image Buildx version is invalid")
    binary_sha256 = _exact(authority['buildx_linux_sha256'], set(release_core.CPU_TOOLCHAIN_AUTHORITIES), 'fixture image Buildx binary digests')  # fmt: skip  # noqa: E501
    for architecture, digest in binary_sha256.items():
        _digest(digest, f"fixture image Buildx {architecture} digest")
    buildkit_image = authority["buildkit_image"]
    if (
        not isinstance(buildkit_image, str)
        or re.fullmatch(
            r"moby/buildkit:v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)@sha256:[0-9a-f]{64}",
            buildkit_image,
        )
        is None
    ):
        raise ValueError("fixture image BuildKit image is not digest-pinned")
    return copy.deepcopy(authority)


def fixture_image_toolchain_authority() -> dict[str, Any]:
    return validate_image_toolchain_authority(FIXTURE_IMAGE_TOOLCHAIN_AUTHORITY)


def real_image_toolchain_authority(
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    fixture = fixture_image_toolchain_authority()
    dockerfile = (Path(docker_root) / "Dockerfile").read_text(encoding="utf-8")
    first_line = dockerfile.splitlines()[0] if dockerfile.splitlines() else ""
    prefix = "# syntax="
    if (
        not first_line.startswith(prefix)
        or re.fullmatch(
            r"docker/dockerfile:v?[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}",
            first_line.removeprefix(prefix),
        )
        is None
    ):
        raise ValueError("Dockerfile frontend must be versioned and digest-pinned")
    if REAL_INSTALL_TARGET not in _docker_stages(dockerfile):
        raise ValueError("Dockerfile is missing real install-only runtime target")
    result = {'schema_version': 1, 'kind': 'ucm-real-image-toolchain-authority', 'buildx_version': fixture['buildx_version'], 'buildx_linux_sha256': fixture['buildx_linux_sha256'], 'buildkit_image': fixture['buildkit_image'], 'dockerfile_frontend': first_line.removeprefix(prefix), 'deterministic_flags': list(REAL_DETERMINISTIC_FLAGS)}  # fmt: skip  # noqa: E501
    result["authority_sha256"] = sha256_value(result)
    return result


def _real_image_authority_from_selected_tasks(
    task: dict[str, Any],
    wheel_task: dict[str, Any],
    *,
    resolved_plan_sha256: str,
    source_repository: str,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise ValueError("real image task must be an object")
    payload = {key: value for key, value in task.items() if key != "task_sha256"}
    dependency_lock = task.get("dependency_lock")
    runtime = task.get("runtime")
    runtime_patch_product = task.get("runtime_patch_product")
    runtime_patch_variants = task.get("runtime_patch_variants")
    runtime_requirements = task.get("runtime_requirements")
    expected_runtime_products = {"vllm", runtime_patch_product}
    if (
        re.fullmatch(r"image-[0-9a-f]{64}", str(task.get("task_id"))) is None
        or task.get("task_sha256") != sha256_value(payload)
        or not isinstance(dependency_lock, dict)
        or set(dependency_lock) != {"build_tools", "runtime_dependencies"}
        or task.get("dependency_lock_sha256") != sha256_value(dependency_lock)
        or not isinstance(runtime_requirements, list)
        or not runtime_requirements
        or not isinstance(runtime, dict)
        or runtime_patch_product not in {"vllm", "vllm-ascend"}
        or not isinstance(runtime_patch_variants, dict)
        or set(runtime_patch_variants) != expected_runtime_products
        or any(
            not isinstance(value, str) or not value
            for value in runtime_patch_variants.values()
        )
        or runtime.get("variant") != runtime_patch_variants.get(runtime_patch_product)
    ):
        raise ValueError("real image task identity or runtime variant is invalid")
    wheel_task = wheel_artifact._validate_wheel_task(wheel_task)
    if (
        task.get("wheel_task_id") != wheel_task["task_id"]
        or task.get("spec_id") != wheel_task["spec_id"]
        or task.get("profile_id") != wheel_task["profile_id"]
        or task.get("cpu_arch") != wheel_task["cpu_arch"]
    ):
        raise ValueError("real image task does not bind the selected wheel task")
    if (
        not isinstance(resolved_plan_sha256, str)
        or DIGEST_RE.fullmatch(resolved_plan_sha256) is None
    ):
        raise ValueError("real image resolved plan hash is invalid")
    if (
        not isinstance(source_repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repository) is None
    ):
        raise ValueError("real image source repository is invalid")
    runtime_versions: dict[str, str] = {}
    for raw_requirement in runtime_requirements:
        try:
            requirement = Requirement(raw_requirement)
        except (InvalidRequirement, TypeError) as error:
            raise ValueError("real image runtime requirement is invalid") from error
        name = canonicalize_name(requirement.name)
        specifiers = list(requirement.specifier)
        if name in runtime_versions or len(specifiers) != 1:
            raise ValueError("real image runtime requirement is not exact")
        specifier = specifiers[0]
        if specifier.operator != "==" or "*" in specifier.version:
            raise ValueError("real image runtime requirement is not exact")
        try:
            runtime_versions[name] = str(Version(specifier.version))
        except InvalidVersion as error:
            raise ValueError('real image runtime requirement version is invalid') from error  # fmt: skip  # noqa: E501
    runtime_dependencies = copy.deepcopy(dependency_lock["runtime_dependencies"])
    if (
        not isinstance(runtime_dependencies, list)
        or not runtime_dependencies
        or len(runtime_dependencies) != len(runtime_versions)
        or len({record.get("name") for record in runtime_dependencies})
        != len(runtime_dependencies)
    ):
        raise ValueError("real image runtime dependency authority is invalid")
    for record in runtime_dependencies:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "name",
                "version",
                "requirement",
                "import_name",
                "filename",
                "sha256",
            }
            or runtime_versions.get(record["name"]) != record["version"]
            or record["requirement"] != f"{record['name']}=={record['version']}"
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", record["import_name"]) is None
            or not isinstance(record["filename"], str)
            or not record["filename"].endswith(".whl")
            or DIGEST_RE.fullmatch(str(record["sha256"])) is None
        ):
            raise ValueError('real image dependency wheels differ from runtime requirements')  # fmt: skip  # noqa: E501
    authority: dict[str, Any] = {'schema_version': 1, 'kind': 'ucm-real-image-task-authority', 'candidate_kind': 'real-candidate', 'fixture_only': False, 'unpublished': True, 'publication_attempted': False, 'source_repository': source_repository, 'source_repository_url': f'https://github.com/{source_repository}', 'task_id': task['task_id'], 'family_task_id': task['family_task_id'], 'wheel_task_id': task['wheel_task_id'], 'wheel_task': copy.deepcopy(wheel_task), 'spec_id': task['spec_id'], 'family_id': task['family_task_id'], 'profile_id': task['profile_id'], 'cpu_arch': task['cpu_arch'], 'platform': task['platform'], 'python_abi': task['python_abi'], 'wheel_version': task['wheel_version'], 'builder': copy.deepcopy(task['builder']), 'runtime': copy.deepcopy(task['runtime']), 'runtime_patch_variants': copy.deepcopy(runtime_patch_variants), 'target_repository': task['target_repository'], 'target_tag': task['target_tag'], 'required_native': copy.deepcopy(task['required_native']), 'forbidden_native': copy.deepcopy(task['forbidden_native']), 'allowed_dt_needed': copy.deepcopy(task['allowed_dt_needed']), 'external_required_dependencies': copy.deepcopy(task['external_required_dependencies']), 'dependency_lock_sha256': task['dependency_lock_sha256'], 'runtime_requirements': copy.deepcopy(runtime_requirements), 'runtime_dependencies': runtime_dependencies, 'task_sha256': task['task_sha256'], 'resolved_plan_sha256': resolved_plan_sha256, 'toolchain': real_image_toolchain_authority(docker_root)}  # fmt: skip  # noqa: E501
    authority["authority_sha256"] = sha256_value(authority)
    return authority


def real_image_authority_from_plan(
    resolved_plan: dict[str, Any],
    *,
    task_id: str,
    expected_plan_sha256: str,
    docker_root: Path = DOCKER_ROOT,
) -> dict[str, Any]:
    task = registry.select_task(resolved_plan, task_kind='image', task_id=task_id, expected_plan_sha256=expected_plan_sha256)  # fmt: skip  # noqa: E501
    wheel_task = registry.select_task(resolved_plan, task_kind='wheel', task_id=task['wheel_task_id'], expected_plan_sha256=expected_plan_sha256)  # fmt: skip  # noqa: E501
    return _real_image_authority_from_selected_tasks(task, wheel_task, resolved_plan_sha256=resolved_plan['resolved_plan_sha256'], source_repository=resolved_plan['source']['repository'], docker_root=docker_root)  # fmt: skip  # noqa: E501


def _json_bytes(content: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            value[key] = item
        return value

    try:
        value = json.loads(content, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON in {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _docker_stages(dockerfile: str) -> dict[str, dict[str, Any]]:
    matches = list(FROM_RE.finditer(dockerfile))
    stages: dict[str, dict[str, Any]] = {}
    aliases_by_index: list[str | None] = []
    for index, match in enumerate(matches):
        alias_value = match.group("alias")
        alias = alias_value.lower() if alias_value is not None else None
        if alias is not None and alias in stages:
            raise ValueError(f"Dockerfile has duplicate stage alias {alias!r}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(dockerfile)  # fmt: skip  # noqa: E501
        body = dockerfile[match.end() : end]
        dependencies: set[str] = set()
        base = match.group("base").lower()
        if base in stages:
            dependencies.add(base)
        elif base.isdecimal() and int(base) < len(aliases_by_index):
            dependency = aliases_by_index[int(base)]
            if dependency is not None:
                dependencies.add(dependency)
        for copy_match in COPY_FROM_RE.finditer(body):
            reference = copy_match.group("stage").lower()
            if reference in stages:
                dependencies.add(reference)
            elif reference.isdecimal() and int(reference) < len(aliases_by_index):
                dependency = aliases_by_index[int(reference)]
                if dependency is not None:
                    dependencies.add(dependency)
        if alias is not None:
            stages[alias] = {'body': body, 'dependencies': dependencies}  # fmt: skip
        aliases_by_index.append(alias)
    return stages


def _project_dependency_closure(
    closure: object,
    native_members: object,
    dt_needed: object,
    *,
    normalize_external_locations: bool,
) -> dict[str, Any]:
    if (
        not isinstance(closure, dict)
        or not isinstance(native_members, dict)
        or not native_members
        or not isinstance(dt_needed, dict)
    ):
        raise ValueError("dependency closure must be a non-empty object")
    expected_members = set(native_members.values())
    if (
        not all(isinstance(member, str) and member for member in expected_members)
        or set(closure) != expected_members
        or set(dt_needed) != expected_members
    ):
        raise ValueError("dependency closure native member set is not exact")

    projected: dict[str, Any] = {}
    record_fields = {'dt_needed', 'resolved_dependencies', 'unresolved_dependencies'}  # fmt: skip
    external_fields = {"dependency", "direct", "kind", "path", "sha256"}
    wheel_member_fields = {'dependency', 'direct', 'kind', 'member', 'sha256'}  # fmt: skip
    external_required_fields = {'dependency', 'direct', 'kind', 'provider', 'expected_mount_root', 'relation', 'required_at'}  # fmt: skip  # noqa: E501
    for member in sorted(expected_members):
        record = closure[member]
        expected_needed = dt_needed[member]
        if (
            not isinstance(record, dict)
            or set(record) != record_fields
            or not isinstance(expected_needed, list)
            or not all(isinstance(item, str) and item for item in expected_needed)
            or len(expected_needed) != len(set(expected_needed))
            or record["dt_needed"] != expected_needed
            or record["unresolved_dependencies"] != []
        ):
            raise ValueError(f"dependency closure record is invalid: {member}")
        resolutions = record["resolved_dependencies"]
        if not isinstance(resolutions, list) or not all(
            isinstance(resolution, dict) for resolution in resolutions
        ):
            raise ValueError(f"dependency closure resolutions are invalid: {member}")
        dependencies = [resolution.get("dependency") for resolution in resolutions]
        if not all(
            isinstance(dependency, str) and dependency for dependency in dependencies
        ) or len(dependencies) != len(set(dependencies)):
            raise ValueError(f"dependency closure resolutions are not unique: {member}")
        direct_dependencies = {resolution['dependency'] for resolution in resolutions if resolution.get('direct') is True}  # fmt: skip  # noqa: E501
        if direct_dependencies != set(expected_needed):
            raise ValueError(f"dependency closure direct dependencies differ: {member}")

        projected_resolutions: list[dict[str, Any]] = []
        for resolution in resolutions:
            dependency = resolution["dependency"]
            direct = resolution.get("direct")
            kind = resolution.get("kind")
            if type(direct) is not bool:
                raise ValueError(f'dependency closure resolution direct flag is invalid: {member}')  # fmt: skip  # noqa: E501
            if kind == "external":
                path = resolution.get("path")
                if (
                    set(resolution) != external_fields
                    or not isinstance(path, str)
                    or not PurePosixPath(path).is_absolute()
                    or DIGEST_RE.fullmatch(str(resolution.get("sha256"))) is None
                ):
                    raise ValueError(f'dependency closure external resolution is invalid: {dependency}')  # fmt: skip  # noqa: E501
                if normalize_external_locations:
                    projected_resolutions.append({'dependency': dependency, 'direct': direct, 'kind': kind})  # fmt: skip  # noqa: E501
                else:
                    projected_resolutions.append(copy.deepcopy(resolution))
            elif kind == "wheel-member":
                wheel_member = resolution.get("member")
                if (
                    set(resolution) != wheel_member_fields
                    or wheel_member not in expected_members
                    or DIGEST_RE.fullmatch(str(resolution.get("sha256"))) is None
                ):
                    raise ValueError(f'dependency closure wheel-member resolution is invalid: {dependency}')  # fmt: skip  # noqa: E501
                projected_resolutions.append(copy.deepcopy(resolution))
            elif kind == "virtual":
                if set(resolution) != {"dependency", "direct", "kind"} or (
                    dependency != "linux-vdso.so.1"
                ):
                    raise ValueError(f'dependency closure virtual resolution is invalid: {dependency}')  # fmt: skip  # noqa: E501
                projected_resolutions.append(copy.deepcopy(resolution))
            elif kind == "external-required":
                mount_root = resolution.get("expected_mount_root")
                if (
                    set(resolution) != external_required_fields
                    or direct is not False
                    or not isinstance(resolution.get("provider"), str)
                    or not resolution["provider"]
                    or not isinstance(mount_root, str)
                    or not PurePosixPath(mount_root).is_absolute()
                    or resolution.get("relation") != "transitive"
                    or resolution.get("required_at") != "device-runtime"
                ):
                    raise ValueError(f'dependency closure external-required resolution is invalid: {dependency}')  # fmt: skip  # noqa: E501
                projected_resolutions.append(copy.deepcopy(resolution))
            else:
                raise ValueError(f'dependency closure resolution kind is invalid: {dependency}')  # fmt: skip  # noqa: E501
        projected[member] = {'dt_needed': copy.deepcopy(record['dt_needed']), 'resolved_dependencies': projected_resolutions, 'unresolved_dependencies': []}  # fmt: skip  # noqa: E501
    return projected


def _matches_python_command(value: object, python_abi: object) -> bool:
    if (
        not isinstance(value, str)
        or not isinstance(python_abi, str)
        or re.fullmatch(r"cp[0-9]{2,}", python_abi) is None
    ):
        return False
    digits = python_abi.removeprefix("cp")
    version = f"{digits[0]}.{digits[1:]}"
    return PurePosixPath(value).name in {"python", "python3", f"python{version}"}


def verify_real_runtime_evidence(recipe: object, evidence: object) -> dict[str, str]:
    if not isinstance(recipe, dict) or set(recipe) != {"payload", "payload_sha256"}:
        raise ValueError("real runtime recipe envelope is invalid")
    payload = recipe["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("candidate_kind") != "real-candidate"
        or recipe["payload_sha256"] != sha256_value(payload)
    ):
        raise ValueError("real runtime recipe digest is invalid")
    if not isinstance(evidence, dict):
        raise ValueError("real runtime evidence must be an object")
    install = evidence.get("install")
    runtime = evidence.get("runtime")
    if (
        not isinstance(install, dict)
        or install.get("kind") not in {None, "ucm-real-install-result"}
        or install.get("status") != "passed"
    ):
        raise ValueError("real install gate did not pass")
    if install.get("pip_check") != "passed":
        raise ValueError("real pip check gate did not pass")
    wheel = payload.get("wheel")
    runtime_dependencies = payload.get("runtime_dependencies")
    if (
        not isinstance(wheel, dict)
        or not isinstance(runtime_dependencies, list)
        or not runtime_dependencies
        or any(not isinstance(value, dict) for value in runtime_dependencies)
    ):
        raise ValueError("real recipe wheel authority is missing")
    if install.get("kind") == "ucm-real-install-result" and (
        install.get("wheel_filename") != wheel.get("filename")
        or install.get("wheel_sha256") != wheel.get("sha256")
        or install.get("runtime_dependencies") != runtime_dependencies
        or install.get("version") != wheel.get("version")
    ):
        raise ValueError("real install wheel identity differs from recipe")
    preinstall_command = install.get("preinstall_command")
    expected_preinstall_command = payload.get('dependency_lock', {}).get('preinstall_command')  # fmt: skip  # noqa: E501
    if (
        not isinstance(preinstall_command, list)
        or not isinstance(expected_preinstall_command, list)
        or preinstall_command[1:] != expected_preinstall_command[1:]
        or not _matches_python_command(preinstall_command[0], wheel.get("python_abi"))
    ):
        raise ValueError("real preinstall purge is not the exact reviewed command")
    command = install.get("pip_command")
    expected_command = payload.get("dependency_lock", {}).get("pip_command")
    if command is not None and (
        not isinstance(command, list)
        or not isinstance(expected_command, list)
        or command[1:] != expected_command[1:]
        or not _matches_python_command(command[0], wheel.get("python_abi"))
    ):
        raise ValueError("real pip command is not the exact offline hashed install")
    expected_packages = {"uc-manager": wheel.get("version")}
    expected_packages.update({record['name']: record.get('version') for record in runtime_dependencies})  # fmt: skip  # noqa: E501
    if install.get("installed_packages") != expected_packages:
        raise ValueError("real installed package versions do not match")
    expected_imports = {"ucm": "passed"}
    expected_imports.update({record['import_name']: 'passed' for record in runtime_dependencies})  # fmt: skip  # noqa: E501
    if install.get("imports") != expected_imports:
        raise ValueError("real import gate did not pass")
    direct_urls = install.get("direct_urls")
    if not isinstance(direct_urls, dict):
        raise ValueError("real direct_url evidence is missing")
    expected_direct = {'uc-manager': (wheel.get('filename'), wheel.get('sha256'))}  # fmt: skip
    expected_direct.update({record['name']: (record.get('filename'), record.get('sha256')) for record in runtime_dependencies})  # fmt: skip  # noqa: E501
    for distribution, (filename, digest) in expected_direct.items():
        direct = direct_urls.get(distribution)
        if (
            not isinstance(direct, dict)
            or direct.get("url") != f"file:///wheelhouse/{filename}"
            or direct.get("archive_info", {}).get("hash")
            != "sha256=" + str(digest).removeprefix("sha256:")
        ):
            raise ValueError(f'real {distribution} direct_url does not bind wheel bytes')  # fmt: skip  # noqa: E501
    if not isinstance(runtime, dict) or runtime.get("kind") not in {
        None,
        "ucm-real-runtime-inspection",
    }:
        raise ValueError("real runtime inspection is missing")
    expected_variants = payload.get("runtime_patch_variants")
    if (
        not isinstance(expected_variants, dict)
        or not expected_variants
        or runtime.get("runtime_patch_variants") != expected_variants
    ):
        raise ValueError("real runtime patch variant map differs from recipe")
    abi = runtime.get("abi")
    expected_abi = wheel.get("python_abi")
    if abi != {
        "expected_python_abi": expected_abi,
        "observed_python_abi": expected_abi,
        "status": "passed",
    }:
        raise ValueError("real runtime ABI gate did not pass")
    expected_native = wheel.get("builder_evidence")
    if not isinstance(expected_native, dict):
        raise ValueError("real recipe lacks Task 2 native evidence")
    if runtime.get("native_members") != expected_native.get("native_members"):
        raise ValueError("installed native member paths differ from Task 2 inspection")
    if runtime.get("elf_machines") != expected_native.get("elf_machines"):
        raise ValueError("installed ELF machine differs from Task 2 inspection")
    if runtime.get("dt_needed") != expected_native.get("dt_needed"):
        raise ValueError("installed ELF DT_NEEDED differs from Task 2 inspection")
    builder_coordinate = expected_native.get("builder_coordinate")
    base = payload.get("base")
    base_subject = base.get("subject") if isinstance(base, dict) else None
    if (
        not isinstance(builder_coordinate, str)
        or not isinstance(base_subject, str)
        or "@" not in builder_coordinate
        or "@" not in base_subject
        or DIGEST_RE.fullmatch(builder_coordinate.rsplit("@", 1)[1]) is None
        or DIGEST_RE.fullmatch(base_subject.rsplit("@", 1)[1]) is None
    ):
        raise ValueError("dependency closure immutable root authority is invalid")
    same_root = builder_coordinate == base_subject
    expected_closure = _project_dependency_closure(expected_native.get('dependency_closure'), expected_native.get('native_members'), expected_native.get('dt_needed'), normalize_external_locations=not same_root)  # fmt: skip  # noqa: E501
    runtime_closure = _project_dependency_closure(runtime.get('dependency_closure'), runtime.get('native_members'), runtime.get('dt_needed'), normalize_external_locations=not same_root)  # fmt: skip  # noqa: E501
    if runtime_closure != expected_closure:
        raise ValueError("installed dependency closure differs from Task 2 inspection")
    for label in ("accelerator_runtime", "device"):
        state = runtime.get(label)
        if not isinstance(state, dict) or state.get("status") != "external-required":
            raise ValueError(f"real {label} must remain external-required")
    if (
        runtime.get("hardware_passed") is not False
        or runtime.get("status") != "external-required"
        or runtime.get("package_version") != wheel.get("version")
    ):
        raise ValueError("real runtime/device evidence is noncanonical")
    return {'install': 'passed', 'pip_check': 'passed', 'direct_url': 'passed', 'ucm_import': 'passed', 'runtime_dependency_imports': 'passed', 'abi': 'passed', 'native_members': 'passed', 'elf': 'passed', 'dependency_closure': 'passed', 'variant': 'passed'}  # fmt: skip  # noqa: E501


def _epoch_timestamp(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 315532800:
        raise ValueError("real source epoch is invalid")
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')  # fmt: skip  # noqa: E501


def real_content_identity(recipe: object, closure: object) -> dict[str, Any]:
    if not isinstance(recipe, dict) or set(recipe) != {"payload", "payload_sha256"}:
        raise ValueError("real content recipe envelope is invalid")
    payload = recipe["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("candidate_kind") != "real-candidate"
        or recipe["payload_sha256"] != sha256_value(payload)
    ):
        raise ValueError("real content recipe digest is invalid")
    if not isinstance(closure, dict):
        raise ValueError("real OCI closure must be an object")
    source = payload.get("source")
    wheel = payload.get("wheel")
    base = payload.get("base")
    base_config_blob = base.get("config") if isinstance(base, dict) else None
    base_config_raw = base_config_blob.get('raw') if isinstance(base_config_blob, dict) else None  # fmt: skip  # noqa: E501
    if (
        not isinstance(source, dict)
        or not isinstance(wheel, dict)
        or not isinstance(base_config_raw, str)
        or re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            str(source.get("repository")),
        )
        is None
        or source.get("repository_url")
        != f"https://github.com/{source.get('repository')}"
    ):
        raise ValueError("real content recipe source/wheel/base authority is missing")
    base_config = _json_bytes(base_config_raw.encode(), "real recipe base config")
    config_value = base_config.get("config", {})
    base_labels = config_value.get('Labels', {}) if isinstance(config_value, dict) else {}  # fmt: skip  # noqa: E501
    base_history = base_config.get("history", [])
    if not isinstance(base_labels, dict) or not isinstance(base_history, list):
        raise ValueError("real recipe base labels/history are invalid")
    expected_labels = copy.deepcopy(base_labels)
    expected_labels.update({'org.opencontainers.image.source': source.get('repository_url'), 'org.opencontainers.image.revision': source.get('commit'), 'io.ucm.release.source-tree': source.get('tree'), 'io.ucm.release.source-context-sha256': source.get('context_sha256'), 'io.ucm.release.task-sha256': payload.get('task_sha256'), 'io.ucm.release.build-key-sha256': payload.get('build_key_sha256'), 'io.ucm.release.wheel-sha256': wheel.get('sha256'), 'io.ucm.release.recipe-sha256': recipe.get('payload_sha256')})  # fmt: skip  # noqa: E501
    labels = closure.get("labels")
    if labels != expected_labels:
        raise ValueError("real OCI config labels do not bind recipe authority")
    expected_annotations = {'io.ucm.release.recipe-sha256': recipe.get('payload_sha256'), 'io.ucm.release.task-sha256': payload.get('task_sha256')}  # fmt: skip  # noqa: E501
    if closure.get("annotations") != expected_annotations:
        raise ValueError("real OCI manifest annotations do not bind recipe authority")
    created = _epoch_timestamp(payload.get("source_date_epoch"))
    history = closure.get("history")
    if (
        closure.get("created") != created
        or not isinstance(history, list)
        or len(history) <= len(base_history)
        or history[: len(base_history)] != base_history
        or any(
            not isinstance(item, dict)
            or item.get("created") != created
            or not isinstance(item.get("created_by"), str)
            or not item["created_by"]
            for item in history[len(base_history) :]
        )
    ):
        raise ValueError("real OCI created/history is not source-epoch deterministic")
    layers = closure.get("layers")
    diff_ids = closure.get("diff_ids")
    if (
        not isinstance(layers, list)
        or not layers
        or not isinstance(diff_ids, list)
        or len(layers) != len(diff_ids)
    ):
        raise ValueError("real OCI layer/diff-id closure is invalid")
    for position, (layer, diff_id) in enumerate(zip(layers, diff_ids, strict=True)):
        if not isinstance(layer, dict):
            raise ValueError(f"real OCI layer {position} is invalid")
        registry._validate_layer_descriptor_annotations(layer, created=created, label=f'real OCI layer {position}')  # fmt: skip  # noqa: E501
        _digest(layer.get("digest"), f"real OCI layer {position}")
        if not isinstance(layer.get("size"), int) or layer["size"] < 1:
            raise ValueError(f"real OCI layer {position} size is invalid")
        _digest(diff_id, f"real OCI diff-id {position}")
    stable = {'manifest_digest': _digest(closure.get('manifest_digest'), 'real OCI manifest digest'), 'config_digest': _digest(closure.get('config_digest'), 'real OCI config digest'), 'layers': copy.deepcopy(layers), 'diff_ids': copy.deepcopy(diff_ids), 'annotations': copy.deepcopy(expected_annotations), 'labels': copy.deepcopy(expected_labels), 'created': created, 'history': copy.deepcopy(history), 'source': copy.deepcopy(source), 'task_sha256': payload.get('task_sha256'), 'build_key_sha256': payload.get('build_key_sha256'), 'wheel_sha256': wheel.get('sha256'), 'recipe_sha256': recipe.get('payload_sha256')}  # fmt: skip  # noqa: E501
    return {**stable, "content_identity_sha256": sha256_value(stable)}
