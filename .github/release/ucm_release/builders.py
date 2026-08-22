"""Project-level builder discovery, synchronization, and release selection."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from . import capabilities, core

RELEASE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = RELEASE_ROOT / "builders.yaml"
DEFAULT_RELEASE = RELEASE_ROOT / "release.yaml"
CURRENT_BUILDER_RECIPE_PATHS = (
    ".github/release/builders.yaml",
    ".github/release/docker/Dockerfile.builder",
)
CURRENT_BUILDER_TOOLCHAIN_PATH = ".github/release/toolchain.lock.yaml"
CATALOG_FIELDS = (
    "project",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "python_abi",
    "manylinux",
    "cpu_arch",
    "source_image",
    "target_repository",
    "target_tag",
    "build_mode",
)
CAPABILITY_FIELDS = (
    "accelerator_runtime",
    "variant",
    "python_abi",
    "manylinux",
    "cpu_arch",
)
_IDENTITY_FIELDS = (
    "project",
    "accelerator",
    *CAPABILITY_FIELDS,
    "target_repository",
)

BUILDER_FACT_PLAN_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "source_sha",
        "upstream_reads",
        "builders",
        "builder_plans",
        "failures",
        "matrix",
    }
)
BUILDER_PLAN_FIELDS = frozenset(
    {
        "builder_plan_id",
        "project",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "cpu_architecture",
        "manylinux",
        "source_kind",
        "source_path",
        "source_image_repository",
        "source_image_tag",
        "source_image_digest",
        "recipe_path",
        "recipe_source_commit",
        "recipe_sha256",
        "toolchain_sha256",
        "target_repository",
        "target_tag",
        "build_mode",
        "runner",
        "mooncake_source_runtime_id",
        "mooncake_source_runtime_image",
        "mooncake_version",
    }
)
BUILDER_PLAN_IDENTITY_FIELDS = (
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "source_image_repository",
    "source_image_digest",
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_repository",
    "target_tag",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
)
BUILDER_PLAN_FAILURE_FIELDS = frozenset(
    {
        "reason_code",
        "source_kind",
        "source_id",
        "builder_plan_id",
        "runtime_id",
        "evidence",
    }
)
BUILDER_RESULT_FIELDS = frozenset(
    {
        "builder_plan_id",
        "status",
        "target_repository",
        "target_tag",
        "target_builder_digest",
        "digest_readback",
        "evidence",
    }
)
COLLECTED_BUILDER_FACT_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "source_sha",
        "upstream_reads",
        "builders",
        "builder_sync",
        "builder_facts",
        "failures",
        "python_probe_matrix",
    }
)
COLLECTED_FAILURE_FIELDS = frozenset(
    {
        "builder_plan_id",
        "status",
        "reason_code",
        "source_kind",
        "source_id",
        "target_repository",
        "target_tag",
        "target_builder_digest",
        "digest_readback",
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
        "evidence",
    }
)
PROBE_MATRIX_ROW_FIELDS = frozenset(
    {
        "id",
        "builder_fact_id",
        "builder_image",
        "target_builder_digest",
        "runner",
        "cpu_architecture",
        "manylinux",
    }
)
_SOURCE_BUILDER_FIELDS = frozenset(
    {
        "project",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "cpu_architecture",
        "manylinux",
        "source_kind",
        "source_path",
        "source_image_repository",
        "source_image_tag",
        "source_image_digest",
        "recipe_path",
        "recipe_source_commit",
        "recipe_sha256",
        "toolchain_sha256",
        "target_repository",
        "target_tag",
    }
)
BUILDER_SOURCE_DISCOVERY_FIELDS = frozenset(
    {"kind", "schema_version", "source_sha", "upstream_reads", "builders"}
)
UPSTREAM_READ_FIELDS = frozenset(
    {"project", "source_kind", "source_path", "source_commit", "fact"}
)
_MOONCAKE_PROBE_FIELDS = frozenset(
    {
        "runtime_image_digest",
        "cpu_architecture",
        "runner",
        "declared_version",
        "installed_version",
        "headers_path",
        "libraries_path",
    }
)
_ASCEND_TARGET_SEPARATOR = "-rt-"
_ASCEND_TARGET_PREFIX_BUDGET = 60
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_COMMIT = re.compile(r"^[0-9a-f]{40}$", re.ASCII)


def _read_yaml(path: Path) -> object:
    return core.load_yaml(path)


def _owner(explicit: str | None) -> str:
    if explicit:
        value = explicit
    else:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" not in repository:
            raise ValueError(
                "builder target owner requires GITHUB_REPOSITORY or --owner"
            )
        value = repository.split("/", 1)[0]
    normalized = value.lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", normalized) is None:
        raise ValueError(f"invalid builder target owner {value!r}")
    return normalized


def _expand_owner(value: str, owner: str) -> str:
    return value.replace("{owner}", owner)


def _require_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected a mapping")
    return value


def _require_string(mapping: dict[str, object], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: {key} must be a non-empty string")
    return value


def _require_digest(value: object, context: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{context}: expected sha256:<64 lowercase hex>")
    return value


def _require_commit(value: object, context: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{context}: expected a 40-character lowercase Git commit")
    return value


def load_config(
    path: Path = DEFAULT_CONFIG, *, require_legacy_mooncake: bool = True
) -> dict[str, object]:
    """Load and structurally validate the sole builder discovery config."""
    config = _require_mapping(_read_yaml(path), str(path))
    if config.get("kind") != "builder-discovery-config":
        raise ValueError(f"{path}: kind must be builder-discovery-config")
    if config.get("schema_version") != 1:
        raise ValueError(f"{path}: schema_version must be 1")
    projects = config.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError(f"{path}: projects must be a non-empty list")
    discoveries: set[str] = set()
    for index, raw in enumerate(projects):
        context = f"{path}: projects[{index}]"
        project = _require_mapping(raw, context)
        _require_string(project, "project", context)
        discovery = _require_string(project, "discovery", context)
        _require_string(project, "target_repository", context)
        build_mode = _require_string(project, "build_mode", context)
        if discovery not in {"vllm-buildkite", "vllm-ascend-dockerfiles"}:
            raise ValueError(f"{context}: unsupported discovery {discovery!r}")
        if discovery in discoveries:
            raise ValueError(f"{context}: duplicate discovery {discovery!r}")
        discoveries.add(discovery)
        if discovery == "vllm-buildkite":
            if build_mode != "mirror":
                raise ValueError(f"{context}: vLLM build_mode must be mirror")
            _require_string(project, "pipeline_path", context)
            _require_string(project, "versions_path", context)
        else:
            if build_mode != "extend":
                raise ValueError(f"{context}: vLLM-Ascend build_mode must be extend")
            _require_string(project, "dockerfile_directory", context)
            _require_string(project, "dockerfile_prefix", context)
            arches = project.get("cpu_architectures")
            if arches != ["amd64", "arm64"]:
                raise ValueError(f"{context}: cpu_architectures must be amd64, arm64")
            excluded = project.get("exclude_variants")
            if excluded != ["310p"]:
                raise ValueError(f"{context}: exclude_variants must contain only 310p")
            if require_legacy_mooncake:
                _require_string(project, "mooncake_version", context)
    if discoveries != {"vllm-buildkite", "vllm-ascend-dockerfiles"}:
        raise ValueError(f"{path}: vLLM and vLLM-Ascend discovery are both required")
    if require_legacy_mooncake:
        retained = config.get("retained_builders")
        if not isinstance(retained, list) or not retained:
            raise ValueError(f"{path}: retained_builders must be a non-empty list")
        for index, raw in enumerate(retained):
            context = f"{path}: retained_builders[{index}]"
            item = _require_mapping(raw, context)
            for key in (
                "project",
                "accelerator",
                "accelerator_runtime",
                "variant",
                "python_abi",
                "manylinux",
                "cpu_arch",
                "source_image",
                "target_repository",
                "build_mode",
                "mooncake_version",
            ):
                _require_string(item, key, context)
            if item["build_mode"] != "copy":
                raise ValueError(f"{context}: retained build_mode must be copy")
    return config


class _SnapshotSource:
    def __init__(self, root: Path, project: str):
        self.root = root / project
        self.commit = os.environ.get("GITHUB_SHA", "0" * 40)

    def freeze_commit(self) -> str:
        return _require_commit(self.commit, "snapshot commit")

    def read(self, path: str) -> str:
        source = self.root / path
        if not source.is_file():
            raise ValueError(f"snapshot missing {source}")
        return source.read_text(encoding="utf-8")

    def list(self, directory: str, prefix: str) -> list[str]:
        root = self.root / directory
        if not root.is_dir():
            raise ValueError(f"snapshot missing {root}")
        return sorted(
            path.name for path in root.iterdir() if path.name.startswith(prefix)
        )


class _GitHubSource:
    def __init__(self, project: str):
        self.project = project
        metadata = self._json(f"https://api.github.com/repos/{project}")
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError(f"{project}: GitHub response has no default_branch")
        self.branch = branch
        self.ref = branch
        self.commit: str | None = None

    def freeze_commit(self) -> str:
        commit = self._json(
            f"https://api.github.com/repos/{self.project}/commits/"
            f"{urllib.parse.quote(self.branch, safe='')}"
        ).get("sha")
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ValueError(f"{self.project}: GitHub response has no branch commit")
        self.commit = commit
        self.ref = commit
        return commit

    @staticmethod
    def _request(url: str) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ucm-builder-discovery",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers)
        ) as response:
            return response.read()

    @classmethod
    def _json(cls, url: str) -> dict[str, object]:
        try:
            value = json.loads(cls._request(url))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"GitHub request failed for {url}: {error}") from error
        return _require_mapping(value, url)

    def read(self, path: str) -> str:
        quoted_ref = urllib.parse.quote(self.ref, safe="")
        quoted_path = urllib.parse.quote(path, safe="/")
        url = (
            f"https://raw.githubusercontent.com/{self.project}/"
            f"{quoted_ref}/{quoted_path}"
        )
        try:
            return self._request(url).decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(
                f"{self.project}/{path}: GitHub raw read failed: {error}"
            ) from error

    def list(self, directory: str, prefix: str) -> list[str]:
        quoted = urllib.parse.quote(directory, safe="/")
        url = (
            f"https://api.github.com/repos/{self.project}/contents/{quoted}"
            f"?ref={urllib.parse.quote(self.ref, safe='')}"
        )
        try:
            value = json.loads(self._request(url))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"{self.project}/{directory}: GitHub listing failed: {error}"
            ) from error
        if not isinstance(value, list):
            raise ValueError(
                f"{self.project}/{directory}: GitHub listing is not a list"
            )
        return sorted(
            item["name"]
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].startswith(prefix)
        )


def _normalize_image(image: str) -> str:
    image = image.strip()
    first = image.split("/", 1)[0]
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{image}"
    return image


def _validate_oci_repository(value: str, context: str) -> None:
    if core.OCI_REPOSITORY_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{context}: invalid OCI repository {value!r}")


def _validate_oci_image(value: str, context: str) -> None:
    separator = value.rfind(":")
    if separator <= value.rfind("/"):
        raise ValueError(f"{context}: OCI image must include a tag: {value!r}")
    repository, tag = value[:separator], value[separator + 1 :]
    _validate_oci_repository(repository, context)
    if core.OCI_TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"{context}: invalid OCI tag {tag!r}")


def _python_abi(version: str, context: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version.strip())
    if match is None:
        raise ValueError(f"{context}: malformed Python version {version!r}")
    return f"cp{match.group(1)}{match.group(2)}"


def _target_tag(item: dict[str, str], mooncake_version: str | None = None) -> str:
    runtime = (
        item["accelerator_runtime"].replace("cuda-", "cuda").replace("cann-", "cann")
    )
    manylinux = item["manylinux"].replace("manylinux_", "manylinux")
    parts = [runtime]
    if item["accelerator"] == "ascend":
        parts.append(item["variant"])
    parts.extend([item["python_abi"], manylinux])
    if mooncake_version is not None:
        parts.append(f"mooncake{mooncake_version}")
    parts.extend([item["cpu_arch"], "r1"])
    return "-".join(parts)


def _catalog_item(
    values: dict[str, str], mooncake_version: str | None = None
) -> dict[str, str]:
    item = dict(values)
    item["target_tag"] = _target_tag(item, mooncake_version)
    _validate_catalog_item(item, "builder")
    return {key: item[key] for key in CATALOG_FIELDS}


def _validate_catalog_item(item: object, context: str) -> dict[str, str]:
    mapping = _require_mapping(item, context)
    for field in CATALOG_FIELDS:
        _require_string(mapping, field, context)
    unknown = set(mapping) - set(CATALOG_FIELDS)
    if unknown:
        raise ValueError(f"{context}: unsupported fields {sorted(unknown)}")
    if mapping["cpu_arch"] not in {"amd64", "arm64"}:
        raise ValueError(f"{context}: unsupported cpu_arch {mapping['cpu_arch']!r}")
    if mapping["accelerator"] not in {"cuda", "ascend"}:
        raise ValueError(
            f"{context}: unsupported accelerator {mapping['accelerator']!r}"
        )
    runtime_pattern = (
        r"cuda-\d+\.\d+" if mapping["accelerator"] == "cuda" else r"cann-\d+\.\d+\.\d+"
    )
    if re.fullmatch(runtime_pattern, mapping["accelerator_runtime"]) is None:
        raise ValueError(
            f"{context}: malformed accelerator_runtime {mapping['accelerator_runtime']!r}"
        )
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", mapping["variant"]) is None:
        raise ValueError(f"{context}: malformed variant {mapping['variant']!r}")
    if re.fullmatch(r"cp\d+", mapping["python_abi"]) is None:
        raise ValueError(f"{context}: malformed python_abi {mapping['python_abi']!r}")
    if re.fullmatch(r"manylinux_\d+_\d+", mapping["manylinux"]) is None:
        raise ValueError(f"{context}: malformed manylinux {mapping['manylinux']!r}")
    if mapping["build_mode"] not in {"mirror", "extend", "copy"}:
        raise ValueError(f"{context}: unsupported build_mode {mapping['build_mode']!r}")
    _validate_oci_image(mapping["source_image"], f"{context} source_image")
    _validate_oci_repository(
        mapping["target_repository"], f"{context} target_repository"
    )
    return mapping  # type: ignore[return-value]


def _walk_tasks(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            yield value
        for nested in value.values():
            yield from _walk_tasks(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_tasks(nested)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def _discover_vllm(
    project: dict[str, object], source: object, owner: str
) -> list[dict[str, str]]:
    project_name = str(project["project"])
    pipeline_path = str(project["pipeline_path"])
    versions_path = str(project["versions_path"])
    context = f"{project_name}/{versions_path}"
    try:
        versions = _require_mapping(json.loads(source.read(versions_path)), context)  # type: ignore[attr-defined]
        variables = _require_mapping(versions.get("variable"), context)
        python_authority = variables.get("PYTHON_VERSION")
        python = (
            python_authority.get("default")
            if isinstance(python_authority, dict)
            else None
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"{context}: malformed JSON: {error}") from error
    if not isinstance(python, str) or not python:
        raise ValueError(
            f"{context}: missing Python version at variable.PYTHON_VERSION.default"
        )
    python_abi = _python_abi(python, context)
    pipeline_context = f"{project_name}/{pipeline_path}"
    pipeline = core.load_yaml_value(  # type: ignore[attr-defined]
        source.read(pipeline_path), context=pipeline_context
    )
    if pipeline is None:
        raise ValueError(f"{pipeline_context}: document is empty")
    items: list[dict[str, str]] = []
    task_pattern = re.compile(r"build-wheel-(x86|arm64)-cuda-(\d+)-(\d+)")
    image_pattern = re.compile(r"(?:^|\s)BUILD_BASE_IMAGE=(?:['\"])?([^\s'\"\\]+)")
    for task in _walk_tasks(pipeline):
        task_id = str(task["id"])
        match = task_pattern.fullmatch(task_id)
        if match is None:
            continue
        task_context = f"{pipeline_context} task {task_id}"
        images = {
            found.group(1)
            for command in _strings(task.get("commands"))
            for found in image_pattern.finditer(command)
        }
        if not images:
            raise ValueError(f"{task_context}: missing BUILD_BASE_IMAGE")
        if len(images) != 1:
            raise ValueError(
                f"{task_context}: BUILD_BASE_IMAGE must be unique, found {sorted(images)}"
            )
        source_image = _normalize_image(next(iter(images)))
        image_match = re.fullmatch(
            r"docker\.io/pytorch/manylinux(?:(?P<arm>aarch64)|(?P<major>\d+)_(?P<minor>\d+))-builder:cuda(?P<runtime>\d+\.\d+)(?:-[A-Za-z0-9_][A-Za-z0-9_.-]*)?",
            source_image,
        )
        if image_match is None:
            raise ValueError(
                f"{task_context}: malformed BUILD_BASE_IMAGE {source_image!r}"
            )
        cpu_arch = "arm64" if match.group(1) == "arm64" else "amd64"
        image_arch = "arm64" if image_match.group("arm") else "amd64"
        if image_arch != cpu_arch:
            raise ValueError(
                f"{task_context}: BUILD_BASE_IMAGE architecture is {image_arch}, expected {cpu_arch}"
            )
        requested_runtime = f"{match.group(2)}.{match.group(3)}"
        if image_match.group("runtime") != requested_runtime:
            raise ValueError(
                f"{task_context}: BUILD_BASE_IMAGE CUDA {image_match.group('runtime')} "
                f"does not match task CUDA {requested_runtime}"
            )
        manylinux = (
            "manylinux_2_28"
            if image_match.group("arm")
            else f"manylinux_{image_match.group('major')}_{image_match.group('minor')}"
        )
        items.append(
            _catalog_item(
                {
                    "project": project_name,
                    "accelerator": "cuda",
                    "accelerator_runtime": f"cuda-{requested_runtime}",
                    "variant": "default",
                    "python_abi": python_abi,
                    "manylinux": manylinux,
                    "cpu_arch": cpu_arch,
                    "source_image": source_image,
                    "target_repository": _expand_owner(
                        str(project["target_repository"]), owner
                    ),
                    "build_mode": str(project["build_mode"]),
                }
            )
        )
    if not items:
        raise ValueError(f"{pipeline_context}: missing build-wheel-*-cuda-* matrix")
    return items


_VARIABLE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


def _substitute(value: str, variables: dict[str, str], context: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain")
        replacement = variables.get(name)
        if replacement is None:
            raise ValueError(f"{context}: unresolved ARG {name} in FROM")
        return replacement

    result = value
    for _ in range(len(variables) + 1):
        replaced = _VARIABLE.sub(replace, result)
        if replaced == result:
            return replaced
        result = replaced
    if _VARIABLE.search(result):
        raise ValueError(f"{context}: unresolved ARG in FROM {result!r}")
    return result


def _parse_ascend_dockerfile(text: str, context: str) -> tuple[str, str, str]:
    variables: dict[str, str] = {}
    from_value: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        arg_match = re.match(
            r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?$", line, re.IGNORECASE
        )
        if arg_match and from_value is None:
            if arg_match.group(2) is not None:
                variables[arg_match.group(1)] = arg_match.group(2).strip().strip("'\"")
            continue
        from_match = re.match(r"FROM\s+(.+)$", line, re.IGNORECASE)
        if from_match:
            from_value = from_match.group(1).strip()
            break
    if from_value is None:
        raise ValueError(f"{context}: missing FROM")
    python = variables.get("PY_VERSION")
    if not python:
        raise ValueError(f"{context}: missing ARG PY_VERSION")
    substituted = _substitute(from_value, variables, context)
    try:
        tokens = shlex.split(substituted)
    except ValueError as error:
        raise ValueError(f"{context}: malformed FROM: {error}") from error
    images = [token for token in tokens if not token.startswith("--")]
    if not images:
        raise ValueError(f"{context}: missing image in FROM")
    source_image = _normalize_image(images[0])
    match = re.fullmatch(
        r"quay\.io/ascend/manylinux:(?P<runtime>\d+\.\d+\.\d+)-.+-manylinux_(?P<major>\d+)_(?P<minor>\d+)-py(?P<python>\d+\.\d+)",
        source_image,
    )
    if match is None:
        raise ValueError(f"{context}: malformed FROM image {source_image!r}")
    if match.group("python") != python:
        raise ValueError(
            f"{context}: FROM Python {match.group('python')} does not match ARG PY_VERSION {python}"
        )
    return (
        source_image,
        match.group("runtime"),
        f"manylinux_{match.group('major')}_{match.group('minor')}",
    )


def _discover_ascend(
    project: dict[str, object],
    source: object,
    owner: str,
    *,
    include_mooncake_tag: bool = True,
    apply_variant_exclusions: bool = True,
) -> list[dict[str, str]]:
    project_name = str(project["project"])
    directory = str(project["dockerfile_directory"])
    prefix = str(project["dockerfile_prefix"])
    excluded = (
        set(project["exclude_variants"])  # type: ignore[arg-type]
        if apply_variant_exclusions
        else set()
    )
    filenames = source.list(directory, prefix)  # type: ignore[attr-defined]
    if not filenames:
        raise ValueError(f"{project_name}/{directory}: missing {prefix}* matrix")
    items: list[dict[str, str]] = []
    for filename in filenames:
        context = f"{project_name}/{directory}/{filename}"
        variant = filename.removeprefix(prefix)
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", variant) is None:
            raise ValueError(f"{context}: malformed variant {variant!r}")
        if variant in excluded:
            continue
        source_image, runtime, manylinux = _parse_ascend_dockerfile(source.read(f"{directory}/{filename}"), context)  # type: ignore[attr-defined]
        python_match = re.search(r"-py(\d+\.\d+)$", source_image)
        assert python_match is not None
        for cpu_arch in project["cpu_architectures"]:  # type: ignore[union-attr]
            items.append(
                _catalog_item(
                    {
                        "project": project_name,
                        "accelerator": "ascend",
                        "accelerator_runtime": f"cann-{runtime}",
                        "variant": variant,
                        "python_abi": _python_abi(python_match.group(1), context),
                        "manylinux": manylinux,
                        "cpu_arch": str(cpu_arch),
                        "source_image": source_image,
                        "target_repository": _expand_owner(
                            str(project["target_repository"]), owner
                        ),
                        "build_mode": str(project["build_mode"]),
                    },
                    str(project["mooncake_version"]) if include_mooncake_tag else None,
                )
            )
    return items


def _retained_items(config: dict[str, object], owner: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for raw in config["retained_builders"]:  # type: ignore[union-attr]
        retained = dict(raw)
        mooncake = str(retained.pop("mooncake_version"))
        retained["source_image"] = _expand_owner(str(retained["source_image"]), owner)
        retained["target_repository"] = _expand_owner(
            str(retained["target_repository"]), owner
        )
        items.append(
            _catalog_item(
                {key: str(value) for key, value in retained.items()}, mooncake
            )
        )
    return items


def _deduplicate(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[str, ...], dict[str, str]] = {}
    for item in items:
        identity = tuple(item[field] for field in _IDENTITY_FIELDS)
        previous = unique.get(identity)
        if previous is not None and previous != item:
            raise ValueError(
                "conflicting builders for capability "
                + ", ".join(f"{field}={item[field]}" for field in _IDENTITY_FIELDS)
            )
        unique[identity] = item
    return sorted(
        unique.values(), key=lambda item: tuple(item[field] for field in CATALOG_FIELDS)
    )


def discover_builders(
    config_path: Path = DEFAULT_CONFIG,
    *,
    snapshot_dir: Path | None = None,
    owner: str | None = None,
    source_only: bool = False,
) -> dict[str, object]:
    """Discover current upstream builders and append explicit retained builders."""
    config = load_config(config_path, require_legacy_mooncake=not source_only)
    resolved_owner = _owner(owner)
    discovered: list[dict[str, str]] = []
    for raw in config["projects"]:  # type: ignore[union-attr]
        project = _require_mapping(raw, str(config_path))
        project_name = str(project["project"])
        source = (
            _SnapshotSource(snapshot_dir, project_name)
            if snapshot_dir is not None
            else _GitHubSource(project_name)
        )
        if project["discovery"] == "vllm-buildkite":
            discovered.extend(_discover_vllm(project, source, resolved_owner))
        else:
            discovered.extend(
                _discover_ascend(
                    project,
                    source,
                    resolved_owner,
                    include_mooncake_tag=not source_only,
                    apply_variant_exclusions=not source_only,
                )
            )
    if not source_only:
        discovered.extend(_retained_items(config, resolved_owner))
    return {
        "kind": "ucm-builder-catalog",
        "schema_version": 1,
        "builders": _deduplicate(discovered),
    }


def _resolve_image_digest(image: str) -> str:
    try:
        output = subprocess.run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                "--format",
                "{{json .Manifest.Digest}}",
                image,
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(
            f"cannot resolve immutable digest for {image}: {error}"
        ) from error
    try:
        digest = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid digest readback for {image}") from error
    return _require_digest(digest, f"source image {image}")


def discover_builder_sources(
    config_path: Path = DEFAULT_CONFIG,
    *,
    snapshot_dir: Path | None = None,
    owner: str | None = None,
    source_sha: str | None = None,
) -> dict[str, Any]:
    """Discover typed Builder source facts with immutable provenance."""
    config = load_config(config_path, require_legacy_mooncake=False)
    resolved_owner = _owner(owner)
    discovered: list[dict[str, str]] = []
    upstream_reads: list[dict[str, str]] = []
    for raw in config["projects"]:  # type: ignore[union-attr]
        project = _require_mapping(raw, str(config_path))
        project_name = str(project["project"])
        source = (
            _SnapshotSource(snapshot_dir, project_name)
            if snapshot_dir is not None
            else _GitHubSource(project_name)
        )
        source_commit = source.freeze_commit()
        if project["discovery"] == "vllm-buildkite":
            rows = _discover_vllm(project, source, resolved_owner)
            source_kind = "buildkite-build-base-image"
            source_paths = [str(project["pipeline_path"])]
            fact = "BUILD_BASE_IMAGE"
        else:
            rows = _discover_ascend(
                project,
                source,
                resolved_owner,
                include_mooncake_tag=False,
                apply_variant_exclusions=False,
            )
            source_kind = "buildwheel-dockerfile"
            directory = str(project["dockerfile_directory"])
            source_paths = sorted(
                {
                    f"{directory}/{str(project['dockerfile_prefix'])}{row['variant']}"
                    for row in rows
                }
            )
            fact = "FROM"
        discovered.extend(rows)
        upstream_reads.extend(
            {
                "project": project_name,
                "source_kind": source_kind,
                "source_path": source_path,
                "source_commit": source_commit,
                "fact": fact,
            }
            for source_path in source_paths
        )

    release_commit = source_sha or os.environ.get("GITHUB_SHA")
    _require_commit(release_commit, "Builder source discovery source_sha")
    toolchain = RELEASE_ROOT / "toolchain.lock.yaml"
    toolchain_sha = "sha256:" + hashlib.sha256(toolchain.read_bytes()).hexdigest()
    typed_rows: list[dict[str, Any]] = []
    source_digests: dict[str, str] = {}
    for item in _deduplicate(discovered):
        source_image = item["source_image"]
        if source_image not in source_digests:
            source_digests[source_image] = _resolve_image_digest(source_image)
        source_repository, source_tag = item["source_image"].rsplit(":", 1)
        accelerator = item["accelerator"]
        recipe_path = (
            CURRENT_BUILDER_RECIPE_PATHS[1]
            if accelerator == "ascend"
            else CURRENT_BUILDER_RECIPE_PATHS[0]
        )
        recipe = core.REPO_ROOT / recipe_path
        source_path = (
            f".github/workflows/dockerfiles/Dockerfile.buildwheel.{item['variant']}"
            if accelerator == "ascend"
            else str(
                next(
                    project["pipeline_path"]
                    for project in config["projects"]  # type: ignore[union-attr]
                    if project["project"] == item["project"]
                )
            )
        )
        target_tag = re.sub(
            r"-cp[0-9]+-", "-cp-all-", item["target_tag"], flags=re.IGNORECASE
        )
        typed_rows.append(
            {
                "project": item["project"],
                "accelerator": accelerator,
                "accelerator_runtime": item["accelerator_runtime"],
                "variant": item["variant"],
                "cpu_architecture": item["cpu_arch"],
                "manylinux": item["manylinux"],
                "source_kind": (
                    "buildkite-build-base-image"
                    if accelerator == "cuda"
                    else "buildwheel-dockerfile"
                ),
                "source_path": source_path,
                "source_image_repository": source_repository,
                "source_image_tag": source_tag,
                "source_image_digest": source_digests[source_image],
                "recipe_path": recipe_path,
                "recipe_source_commit": release_commit,
                "recipe_sha256": "sha256:"
                + hashlib.sha256(recipe.read_bytes()).hexdigest(),
                "toolchain_sha256": toolchain_sha,
                "target_repository": item["target_repository"],
                "target_tag": target_tag,
            }
        )
    value = {
        "kind": "ucm-builder-discovery",
        "schema_version": 3,
        "source_sha": release_commit,
        "upstream_reads": sorted(
            upstream_reads,
            key=lambda row: (row["project"], row["source_path"]),
        ),
        "builders": sorted(
            typed_rows,
            key=lambda row: core.canonical_bytes(row),
        ),
    }
    return validate_builder_source_discovery(value)


def validate_catalog(catalog: object) -> dict[str, object]:
    mapping = _require_mapping(catalog, "builder catalog")
    if mapping.get("kind") != "ucm-builder-catalog":
        raise ValueError("builder catalog: kind must be ucm-builder-catalog")
    if mapping.get("schema_version") != 1:
        raise ValueError("builder catalog: schema_version must be 1")
    values = mapping.get("builders")
    if not isinstance(values, list):
        raise ValueError("builder catalog: builders must be a list")
    for index, item in enumerate(values):
        _validate_catalog_item(item, f"builder catalog builders[{index}]")
    return mapping


def compute_sync_plan(catalog: object, existing_tags: object) -> dict[str, object]:
    """Return only catalog entries whose exact target tag is absent."""
    validated = validate_catalog(catalog)
    existing = _require_mapping(existing_tags, "existing builder tags")
    normalized: dict[str, set[str]] = {}
    for repository, raw_tags in existing.items():
        if not isinstance(repository, str) or not repository:
            raise ValueError(
                "existing builder tags: repository names must be non-empty strings"
            )
        if not isinstance(raw_tags, list) or not all(
            isinstance(tag, str) for tag in raw_tags
        ):
            raise ValueError(
                f"existing builder tags {repository}: tags must be a string list"
            )
        normalized[repository] = set(raw_tags)
    missing = sorted(
        (
            item
            for item in validated["builders"]  # type: ignore[union-attr]
            if item["target_tag"]
            not in normalized.get(item["target_repository"], set())
        ),
        key=lambda item: tuple(item[field] for field in CATALOG_FIELDS),
    )
    matrix = []
    for item in missing:
        runtime_name, _, runtime_version = item["accelerator_runtime"].partition("-")
        runtime_label = f"{runtime_name.upper()} {runtime_version}"
        variant = "" if item["variant"] == "default" else f" {item['variant'].upper()}"
        matrix.append(
            {
                **item,
                "id": item["target_tag"],
                "label": f"{runtime_label}{variant} · {item['cpu_arch']}",
            }
        )
    return {
        "kind": "ucm-builder-sync-plan",
        "schema_version": 1,
        "builders": missing,
        "matrix": {"include": matrix},
    }


def _closed_fields(
    value: dict[str, object], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{label} fields mismatch: missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def validate_builder_source_discovery(value: object) -> dict[str, Any]:
    """Validate and return one closed, provenance-complete source discovery."""
    discovery = _require_mapping(value, "Builder source discovery")
    _closed_fields(
        discovery,
        BUILDER_SOURCE_DISCOVERY_FIELDS,
        "Builder source discovery",
    )
    if discovery.get("kind") != "ucm-builder-discovery":
        raise ValueError("Builder source discovery kind is invalid")
    if discovery.get("schema_version") != 3:
        raise ValueError("Builder source discovery schema_version must be 3")
    _require_commit(discovery.get("source_sha"), "Builder source discovery source_sha")
    reads = discovery.get("upstream_reads")
    if not isinstance(reads, list) or not reads:
        raise ValueError("Builder source discovery upstream_reads must be non-empty")
    for index, raw in enumerate(reads):
        item = _require_mapping(raw, f"Builder upstream reads[{index}]")
        _closed_fields(item, UPSTREAM_READ_FIELDS, f"Builder upstream reads[{index}]")
        for field in ("project", "source_kind", "source_path", "fact"):
            _require_string(item, field, f"Builder upstream reads[{index}]")
        _require_commit(
            item.get("source_commit"),
            f"Builder upstream reads[{index}] source_commit",
        )
    sources = discovery.get("builders")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Builder source discovery builders must be non-empty")
    for index, raw in enumerate(sources):
        item = _require_mapping(raw, f"Builder sources[{index}]")
        _closed_fields(item, _SOURCE_BUILDER_FIELDS, f"Builder sources[{index}]")
        for field in _SOURCE_BUILDER_FIELDS - {
            "source_image_digest",
            "recipe_source_commit",
            "recipe_sha256",
            "toolchain_sha256",
        }:
            _require_string(item, field, f"Builder sources[{index}]")
        capabilities.compact_accelerator_runtime(item["accelerator_runtime"])
        if capabilities.normalize_variant(item["variant"]) != item["variant"]:
            raise ValueError(f"Builder sources[{index}] variant is not canonical")
        if item["accelerator"] not in {"cuda", "ascend"}:
            raise ValueError(f"Builder sources[{index}] accelerator is unsupported")
        if item["cpu_architecture"] not in {"amd64", "arm64"}:
            raise ValueError(f"Builder sources[{index}] architecture is unsupported")
        _require_digest(
            item["source_image_digest"],
            f"Builder sources[{index}] source image digest",
        )
        _require_commit(
            item["recipe_source_commit"],
            f"Builder sources[{index}] recipe source commit",
        )
        _require_digest(
            item["recipe_sha256"], f"Builder sources[{index}] recipe digest"
        )
        _require_digest(
            item["toolchain_sha256"], f"Builder sources[{index}] toolchain digest"
        )
    return copy.deepcopy(discovery)


def _owned_file_bytes(repository_root: Path, relative_path: str) -> bytes:
    try:
        root = repository_root.resolve(strict=True)
        candidate = repository_root / relative_path
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(
            f"Builder authority source is missing: {relative_path}"
        ) from error
    if not resolved.is_relative_to(root) or not candidate.is_file():
        raise ValueError(
            f"Builder authority source escapes repository: {relative_path}"
        )
    return candidate.read_bytes()


def validate_current_builder_authority(value: object) -> dict[str, Any]:
    """Validate one closed current-checkout Builder authority record."""
    authority = _require_mapping(value, "current Builder authority")
    expected_fields = {
        "kind",
        "schema_version",
        "source_sha",
        "toolchain_sha256",
        "recipes",
        "authority_sha256",
    }
    if set(authority) != expected_fields:
        raise ValueError("current Builder authority fields must be exact")
    if authority.get("kind") != "ucm-current-builder-authority":
        raise ValueError("current Builder authority kind is invalid")
    if authority.get("schema_version") != 3:
        raise ValueError("current Builder authority schema_version must be 3")
    source_sha = _require_commit(
        authority.get("source_sha"), "current Builder authority source_sha"
    )
    _require_digest(
        authority.get("toolchain_sha256"),
        "current Builder authority toolchain digest",
    )
    raw_recipes = authority.get("recipes")
    if not isinstance(raw_recipes, list):
        raise ValueError("current Builder authority recipes must be an array")
    recipes: list[dict[str, object]] = []
    for index, raw in enumerate(raw_recipes):
        recipe = _require_mapping(raw, f"current Builder recipes[{index}]")
        if set(recipe) != {
            "recipe_path",
            "recipe_source_commit",
            "recipe_sha256",
        }:
            raise ValueError("current Builder recipe fields must be exact")
        path = _require_string(
            recipe, "recipe_path", f"current Builder recipes[{index}]"
        )
        commit = _require_commit(
            recipe.get("recipe_source_commit"),
            f"current Builder recipes[{index}] commit",
        )
        if commit != source_sha:
            raise ValueError("current Builder recipe commit differs from source_sha")
        _require_digest(
            recipe.get("recipe_sha256"),
            f"current Builder recipes[{index}] digest",
        )
        recipes.append(recipe)
    expected_paths = tuple(sorted(CURRENT_BUILDER_RECIPE_PATHS))
    actual_paths = tuple(str(item["recipe_path"]) for item in recipes)
    if actual_paths != expected_paths:
        raise ValueError("current Builder recipes are incomplete or noncanonical")
    authority_digest = _require_digest(
        authority.get("authority_sha256"), "current Builder authority digest"
    )
    projection = copy.deepcopy(authority)
    projection.pop("authority_sha256")
    if authority_digest != core.sha256_value(projection):
        raise ValueError("current Builder authority digest differs from contents")
    return copy.deepcopy(authority)


def freeze_current_builder_authority(
    *,
    source_sha: str,
    repository_root: Path = core.REPO_ROOT,
) -> dict[str, Any]:
    """Freeze current recipe/toolchain bytes as the planner authority."""
    commit = _require_commit(source_sha, "current Builder authority source_sha")
    recipes = []
    for recipe_path in CURRENT_BUILDER_RECIPE_PATHS:
        contents = _owned_file_bytes(repository_root, recipe_path)
        recipes.append(
            {
                "recipe_path": recipe_path,
                "recipe_source_commit": commit,
                "recipe_sha256": "sha256:"
                + hashlib.sha256(contents).hexdigest(),
            }
        )
    recipes.sort(key=lambda item: item["recipe_path"])
    toolchain = _owned_file_bytes(repository_root, CURRENT_BUILDER_TOOLCHAIN_PATH)
    authority = {
        "kind": "ucm-current-builder-authority",
        "schema_version": 3,
        "source_sha": commit,
        "toolchain_sha256": "sha256:" + hashlib.sha256(toolchain).hexdigest(),
        "recipes": recipes,
        "authority_sha256": "",
    }
    authority["authority_sha256"] = core.sha256_value(
        {key: item for key, item in authority.items() if key != "authority_sha256"}
    )
    return validate_current_builder_authority(authority)


def _fact_digest(value: dict[str, object], fields: tuple[str, ...]) -> str:
    return core.sha256_value({field: value[field] for field in fields})


def _ascend_runtime_target_tag(
    source_target_tag: str, runtime_id: str, runtime_image_digest: str
) -> str:
    suffix = core.sha256_value(
        {
            "source_target_tag": source_target_tag,
            "runtime_id": runtime_id,
            "runtime_image_digest": runtime_image_digest,
        }
    ).split(":", 1)[1]
    prefix = re.sub(r"[^A-Za-z0-9._-]", "-", source_target_tag)
    prefix = prefix[:_ASCEND_TARGET_PREFIX_BUDGET].rstrip(".-")
    if not prefix or re.match(r"^[A-Za-z0-9_]", prefix) is None:
        prefix = "_" + prefix[1:]
    return f"{prefix}{_ASCEND_TARGET_SEPARATOR}{suffix}"


def _builder_plan(
    source: dict[str, object],
    *,
    runtime_id: str | None,
    runtime_image: str | None,
    mooncake_version: str | None,
    target_tag: str,
) -> dict[str, object]:
    plan = {
        "builder_plan_id": "",
        **{field: copy.deepcopy(source[field]) for field in _SOURCE_BUILDER_FIELDS},
        "target_tag": target_tag,
        "build_mode": "mirror" if source["accelerator"] == "cuda" else "extend",
        "runner": (
            "ubuntu-24.04-arm"
            if source["cpu_architecture"] == "arm64"
            else "ubuntu-24.04"
        ),
        "mooncake_source_runtime_id": runtime_id,
        "mooncake_source_runtime_image": runtime_image,
        "mooncake_version": mooncake_version,
    }
    plan["builder_plan_id"] = _fact_digest(plan, BUILDER_PLAN_IDENTITY_FIELDS)
    return plan


def plan_builder_facts(
    builder_discovery: object,
    runtime_discovery: object,
    mooncake_probes: object,
) -> dict[str, object]:
    """Plan ABI-independent physical Builder targets from verified runtime facts."""
    discovery = validate_builder_source_discovery(builder_discovery)
    runtimes_input = _require_mapping(runtime_discovery, "runtime discovery")
    probes_input = _require_mapping(mooncake_probes, "Mooncake probes")
    source_sha = _require_string(discovery, "source_sha", "Builder discovery")
    sources = discovery.get("builders")
    if not isinstance(sources, list):
        raise ValueError("Builder discovery builders must be an array")
    runtime_values = runtimes_input.get("runtime_candidates")
    if not isinstance(runtime_values, list):
        raise ValueError("runtime discovery candidates must be an array")
    probe_values = probes_input.get("probes")
    if not isinstance(probe_values, list):
        raise ValueError("Mooncake probes must be an array")
    probe_failures = probes_input.get("failures", [])
    if not isinstance(probe_failures, list):
        raise ValueError("Mooncake probe failures must be an array")

    runtimes: list[tuple[dict[str, object], dict[str, Any]]] = []
    for index, raw in enumerate(runtime_values):
        runtime = _require_mapping(raw, f"runtime candidates[{index}]")
        record = capabilities.normalize_runtime_candidate(runtime)
        runtimes.append((runtime, record))

    probes: dict[tuple[str, str], dict[str, object]] = {}
    for index, raw in enumerate(probe_values):
        probe = _require_mapping(raw, f"Mooncake probes[{index}]")
        _closed_fields(probe, _MOONCAKE_PROBE_FIELDS, f"Mooncake probes[{index}]")
        key = (
            _require_digest(
                probe.get("runtime_image_digest"), "Mooncake runtime digest"
            ),
            _require_string(probe, "cpu_architecture", "Mooncake probe"),
        )
        previous = probes.get(key)
        if previous is not None and previous != probe:
            raise ValueError("conflicting Mooncake probe for one runtime")
        probes[key] = probe

    failed_runtime_ids: set[str] = set()
    for index, raw in enumerate(probe_failures):
        failure = _require_mapping(raw, f"Mooncake probe failures[{index}]")
        _closed_fields(
            failure,
            capabilities.MOONCAKE_PROBE_FAILURE_FIELDS,
            f"Mooncake probe failures[{index}]",
        )
        if failure.get("status") != "failed" or failure.get("reason_code") != (
            "mooncake-probe-failed"
        ):
            raise ValueError("Mooncake probe failure status/reason is invalid")
        failed_runtime_ids.add(
            _require_digest(
                failure.get("runtime_id"),
                f"Mooncake probe failures[{index}] runtime ID",
            )
        )

    plans_by_id: dict[str, dict[str, object]] = {}
    failures: list[dict[str, object]] = []
    for index, raw in enumerate(sources):
        source = _require_mapping(raw, f"Builder sources[{index}]")
        _closed_fields(source, _SOURCE_BUILDER_FIELDS, f"Builder sources[{index}]")
        variant = capabilities.normalize_variant(source.get("variant"))
        if variant == "310p":
            continue
        accelerator = _require_string(source, "accelerator", "Builder source")
        if accelerator == "cuda":
            planned = _builder_plan(
                source,
                runtime_id=None,
                runtime_image=None,
                mooncake_version=None,
                target_tag=_require_string(source, "target_tag", "Builder source"),
            )
            plans_by_id[planned["builder_plan_id"]] = planned
            continue
        compatible = [
            (runtime, record)
            for runtime, record in runtimes
            if all(
                runtime[field] == source[field]
                for field in (
                    "accelerator",
                    "accelerator_runtime",
                    "variant",
                    "cpu_architecture",
                )
            )
        ]
        for runtime, record in compatible:
            runtime_id = record["runtime_id"]
            probe = probes.get(
                (runtime["runtime_image_digest"], runtime["cpu_architecture"])
            )
            if probe is None and runtime_id in failed_runtime_ids:
                continue
            if probe is None or not (
                probe["declared_version"]
                == probe["installed_version"]
                == runtime["mooncake_version"]
            ):
                evidence = {
                    "declared_version": (
                        None if probe is None else probe["declared_version"]
                    ),
                    "installed_version": (
                        None if probe is None else probe["installed_version"]
                    ),
                }
                failures.append(
                    {
                        "reason_code": "mooncake-version-mismatch",
                        "source_kind": "mooncake-probe",
                        "source_id": runtime_id,
                        "builder_plan_id": None,
                        "runtime_id": runtime_id,
                        "evidence": evidence,
                    }
                )
                continue
            target_tag = _ascend_runtime_target_tag(
                _require_string(source, "target_tag", "Builder source"),
                runtime_id,
                runtime["runtime_image_digest"],
            )
            planned = _builder_plan(
                source,
                runtime_id=runtime_id,
                runtime_image=record["runtime_image"],
                mooncake_version=runtime["mooncake_version"],
                target_tag=target_tag,
            )
            previous = plans_by_id.get(planned["builder_plan_id"])
            if previous is not None and previous != planned:
                raise ValueError("conflicting physical Builder plan identity")
            plans_by_id[planned["builder_plan_id"]] = planned

    plans = sorted(plans_by_id.values(), key=lambda item: item["builder_plan_id"])
    failures.sort(
        key=lambda item: (
            str(item["reason_code"]),
            str(item["source_id"]),
            str(item["runtime_id"] or ""),
        )
    )
    return {
        "kind": "ucm-builder-fact-plan",
        "schema_version": 3,
        "source_sha": source_sha,
        "upstream_reads": copy.deepcopy(discovery["upstream_reads"]),
        "builders": copy.deepcopy(discovery["builders"]),
        "builder_plans": plans,
        "failures": failures,
        "matrix": {
            "include": [
                {
                    "id": item["builder_plan_id"].removeprefix("sha256:"),
                    "label": (
                        f"{str(item['accelerator_runtime']).upper()} · "
                        f"{item['variant']} · {item['cpu_architecture']}"
                    ),
                    **copy.deepcopy(item),
                }
                for item in plans
            ]
        },
    }


def collect_builder_facts(plan: object, builder_results: object) -> dict[str, object]:
    """Reconcile one exact Result per planned Builder and freeze target facts."""
    planned = _require_mapping(plan, "Builder fact plan")
    _closed_fields(planned, BUILDER_FACT_PLAN_FIELDS, "Builder fact plan")
    results_value = _require_mapping(builder_results, "Builder Results")
    if results_value.get("kind") != "ucm-builder-results":
        raise ValueError("Builder Results kind is invalid")
    if results_value.get("schema_version") != 3:
        raise ValueError("Builder Results schema_version must be 3")
    result_items = results_value.get("results")
    if not isinstance(result_items, list):
        raise ValueError("Builder Results results must be an array")
    plan_items = planned.get("builder_plans")
    if not isinstance(plan_items, list):
        raise ValueError("Builder plan rows must be an array")
    plans_by_id: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(plan_items):
        item = _require_mapping(raw, f"Builder plans[{index}]")
        _closed_fields(item, BUILDER_PLAN_FIELDS, f"Builder plans[{index}]")
        plan_id = _require_digest(item["builder_plan_id"], "Builder plan ID")
        if plan_id in plans_by_id:
            raise ValueError("duplicate Builder plan ID")
        plans_by_id[plan_id] = item
    results_by_id: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(result_items):
        item = _require_mapping(raw, f"Builder Results[{index}]")
        _closed_fields(item, BUILDER_RESULT_FIELDS, f"Builder Results[{index}]")
        plan_id = _require_digest(item["builder_plan_id"], "Builder Result ID")
        if plan_id in results_by_id:
            raise ValueError("duplicate Builder Result ID")
        results_by_id[plan_id] = item
    if set(results_by_id) != set(plans_by_id):
        raise ValueError("Builder Result IDs do not exactly match planned IDs")

    facts: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    plan_failures = planned.get("failures")
    if not isinstance(plan_failures, list):
        raise ValueError("Builder plan failures must be an array")
    for index, raw in enumerate(plan_failures):
        failure = _require_mapping(raw, f"Builder plan failures[{index}]")
        _closed_fields(
            failure,
            BUILDER_PLAN_FAILURE_FIELDS,
            f"Builder plan failures[{index}]",
        )
        reason_code = _require_string(
            failure, "reason_code", f"Builder plan failures[{index}]"
        )
        if reason_code != "mooncake-version-mismatch":
            raise ValueError(f"unsupported Builder plan failure {reason_code!r}")
        runtime_id = _require_digest(
            failure.get("runtime_id"),
            f"Builder plan failures[{index}] runtime ID",
        )
        if failure.get("builder_plan_id") is not None:
            raise ValueError("Mooncake plan failure cannot claim a Builder plan")
        evidence = copy.deepcopy(
            _require_mapping(
                failure.get("evidence"), f"Builder plan failures[{index}] evidence"
            )
        )
        failures.append(
            {
                "builder_plan_id": None,
                "status": "failed",
                "reason_code": reason_code,
                "source_kind": _require_string(
                    failure, "source_kind", f"Builder plan failures[{index}]"
                ),
                "source_id": _require_string(
                    failure, "source_id", f"Builder plan failures[{index}]"
                ),
                "target_repository": None,
                "target_tag": None,
                "target_builder_digest": None,
                "digest_readback": False,
                "builder_capability_id": None,
                "builder_revision_id": None,
                "runtime_id": runtime_id,
                "evidence": evidence,
            }
        )
    rows: list[dict[str, object]] = []
    for plan_id in sorted(plans_by_id):
        planned_item = plans_by_id[plan_id]
        result = results_by_id[plan_id]
        if any(
            result[field] != planned_item[field]
            for field in ("target_repository", "target_tag")
        ):
            raise ValueError("Builder Result target differs from its plan")
        status = result.get("status")
        if status in {"existing", "built"}:
            if result.get("digest_readback") is not True:
                raise ValueError("resolved Builder Result requires digest readback")
            target_digest = _require_digest(
                result.get("target_builder_digest"), "Builder target digest"
            )
            fact = {
                "builder_fact_id": "",
                **{
                    field: copy.deepcopy(planned_item[field])
                    for field in capabilities.BUILDER_FACT_FIELDS
                    if field not in {"builder_fact_id", "target_builder_digest"}
                },
                "target_builder_digest": target_digest,
            }
            fact["builder_fact_id"] = _fact_digest(
                fact, capabilities.BUILDER_FACT_IDENTITY_FIELDS
            )
            facts.append(fact)
            rows.append(
                {
                    "id": fact["builder_fact_id"].removeprefix("sha256:"),
                    "builder_fact_id": fact["builder_fact_id"],
                    "builder_image": (f"{fact['target_repository']}@{target_digest}"),
                    "target_builder_digest": target_digest,
                    "runner": planned_item["runner"],
                    "cpu_architecture": planned_item["cpu_architecture"],
                    "manylinux": planned_item["manylinux"],
                }
            )
        elif status == "failed":
            if result.get("digest_readback") is not False:
                raise ValueError("failed Builder Result cannot verify a digest")
            evidence = copy.deepcopy(result["evidence"])
            if not isinstance(evidence, dict):
                raise ValueError("failed Builder Result evidence must be an object")
            evidence.setdefault(
                "plan",
                {
                    field: copy.deepcopy(planned_item[field])
                    for field in BUILDER_PLAN_IDENTITY_FIELDS
                },
            )
            failures.append(
                {
                    "builder_plan_id": plan_id,
                    "status": "failed",
                    "reason_code": "builder-sync-failed",
                    "source_kind": "builder-plan",
                    "source_id": plan_id,
                    "target_repository": planned_item["target_repository"],
                    "target_tag": planned_item["target_tag"],
                    "target_builder_digest": result.get("target_builder_digest"),
                    "digest_readback": False,
                    "builder_capability_id": None,
                    "builder_revision_id": None,
                    "runtime_id": None,
                    "evidence": evidence,
                }
            )
        else:
            raise ValueError(f"unsupported Builder Result status {status!r}")
    facts.sort(key=lambda item: item["builder_fact_id"])
    failures.sort(
        key=lambda item: (
            str(item["reason_code"]),
            str(item["source_id"]),
            str(item["runtime_id"] or ""),
        )
    )
    rows.sort(key=lambda item: item["builder_fact_id"])
    return {
        "kind": "ucm-collected-builder-facts",
        "schema_version": 3,
        "source_sha": planned["source_sha"],
        "upstream_reads": copy.deepcopy(planned["upstream_reads"]),
        "builders": copy.deepcopy(planned["builders"]),
        "builder_sync": {
            "mode": "append-only",
            "target_digests_verified": True,
            "deletions": [],
        },
        "builder_facts": facts,
        "failures": failures,
        "python_probe_matrix": {"include": rows},
    }


def _capability_text(capability: dict[str, str]) -> str:
    return ", ".join(f"{field}={capability[field]}" for field in CAPABILITY_FIELDS)


def _nearest_candidates(
    available: list[dict[str, str]], capability: dict[str, str], accelerator: str
) -> list[str]:
    candidates = [item for item in available if item["accelerator"] == accelerator]
    if not candidates:
        candidates = available
    ranked = sorted(
        candidates,
        key=lambda item: (
            sum(item[field] != capability[field] for field in CAPABILITY_FIELDS),
            item["target_tag"],
        ),
    )
    return [
        f"{item['target_repository']}:{item['target_tag']} "
        f"({_capability_text(item)})"
        for item in ranked[:5]
    ]


def select_builders(catalog: object, release: object) -> dict[str, object]:
    """Select one builder for every derived build profile and architecture."""
    validated = validate_catalog(catalog)
    available = list(validated["builders"])  # type: ignore[arg-type]
    release_config = _require_mapping(release, "release config")
    profiles = release_config.get("build_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("release config: build_profiles must be a non-empty list")
    selected: list[dict[str, str]] = []
    profile_ids: set[str] = set()
    for index, raw_profile in enumerate(profiles):
        context = f"release config build_profiles[{index}]"
        profile = _require_mapping(raw_profile, context)
        profile_id = _require_string(profile, "id", context)
        if profile_id in profile_ids:
            raise ValueError(f"duplicate release profile id: {profile_id}")
        profile_ids.add(profile_id)
        accelerator = _require_string(profile, "accelerator", context)
        runtime = _require_string(profile, "accelerator_runtime", context)
        python_abi = _require_string(profile, "python_abi", context)
        builder_manylinux = _require_string(profile, "builder_manylinux", context)
        if re.fullmatch(r"manylinux_\d+_\d+", builder_manylinux) is None:
            raise ValueError(
                f"{context}: malformed builder_manylinux {builder_manylinux!r}"
            )
        arches = profile.get("cpu_arch")
        if (
            not isinstance(arches, list)
            or not arches
            or not all(arch in {"amd64", "arm64"} for arch in arches)
        ):
            raise ValueError(
                f"{context}: cpu_arch must be a non-empty amd64/arm64 list"
            )
        if len(set(arches)) != len(arches):
            raise ValueError(f"{context}: cpu_arch contains duplicates")
        if accelerator not in {"cuda", "ascend"}:
            raise ValueError(f"{context}: unsupported accelerator {accelerator!r}")
        variant = _require_string(profile, "variant", context)
        for cpu_arch in arches:
            partial = {
                "accelerator_runtime": runtime,
                "variant": variant,
                "python_abi": python_abi,
                "cpu_arch": str(cpu_arch),
            }
            capability = {
                **partial,
                "manylinux": builder_manylinux,
            }
            matches = [
                item
                for item in available
                if item["accelerator"] == accelerator
                and all(item[field] == capability[field] for field in CAPABILITY_FIELDS)
            ]
            if len(matches) != 1:
                nearest = _nearest_candidates(available, capability, accelerator)
                reason = "missing" if not matches else f"multiple ({len(matches)})"
                raise ValueError(
                    f"release profile {profile_id}: {reason} builder for requested capability "
                    f"{_capability_text(capability)}; nearest candidates: {nearest}"
                )
            selected.append({"profile_id": profile_id, **matches[0]})
    return {
        "kind": "ucm-builder-selection",
        "schema_version": 1,
        "builders": selected,
        "matrix": {"include": selected},
    }


def bind_selection(catalog: object, selection: object) -> dict[str, object]:
    """Bind one selected project builder to every derived profile architecture."""
    release = copy.deepcopy(_require_mapping(catalog, "release catalog"))
    profiles = release.get("build_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("release catalog: build_profiles must be a non-empty list")

    selected = _require_mapping(selection, "builder selection")
    if set(selected) != {"kind", "schema_version", "builders", "matrix"}:
        raise ValueError("builder selection: fields must be exact")
    if selected.get("kind") != "ucm-builder-selection":
        raise ValueError("builder selection: kind must be ucm-builder-selection")
    if selected.get("schema_version") != 1:
        raise ValueError("builder selection: schema_version must be 1")
    items = selected.get("builders")
    matrix = selected.get("matrix")
    if not isinstance(items, list):
        raise ValueError("builder selection: builders must be a list")
    if (
        not isinstance(matrix, dict)
        or set(matrix) != {"include"}
        or matrix["include"] != items
    ):
        raise ValueError(
            "builder selection: matrix.include must exactly match builders"
        )

    profiles_by_id: dict[str, dict[str, object]] = {}
    expected_coordinates: set[tuple[str, str]] = set()
    for index, raw_profile in enumerate(profiles):
        context = f"release catalog build_profiles[{index}]"
        profile = _require_mapping(raw_profile, context)
        profile_id = _require_string(profile, "id", context)
        if profile_id in profiles_by_id:
            raise ValueError(f"duplicate release profile id: {profile_id}")
        requirements = profile.get("builders")
        architectures = profile.get("cpu_arch")
        if not isinstance(requirements, dict) or not isinstance(architectures, list):
            raise ValueError(
                f"release profile {profile_id}: builder requirements are invalid"
            )
        if set(requirements) != set(architectures):
            raise ValueError(
                f"release profile {profile_id}: builder architectures do not match cpu_arch"
            )
        profiles_by_id[profile_id] = profile
        expected_coordinates.update((profile_id, str(arch)) for arch in architectures)

    seen: set[tuple[str, str]] = set()
    for index, raw_item in enumerate(items):
        context = f"builder selection builders[{index}]"
        item = _require_mapping(raw_item, context)
        profile_id = _require_string(item, "profile_id", context)
        profile = profiles_by_id.get(profile_id)
        if profile is None:
            raise ValueError(f"{context}: unknown release profile {profile_id!r}")
        architecture = _require_string(item, "cpu_arch", context)
        requirements = profile["builders"]
        if not isinstance(requirements, dict) or architecture not in requirements:
            raise ValueError(
                f"{context}: undeclared architecture {architecture!r} for release profile {profile_id!r}"
            )
        catalog_item = {
            key: value for key, value in item.items() if key != "profile_id"
        }
        validated_item = _validate_catalog_item(catalog_item, context)
        coordinate = (profile_id, architecture)
        if coordinate in seen:
            raise ValueError(
                f"duplicate builder selection for release profile {profile_id!r} architecture {architecture!r}"
            )
        seen.add(coordinate)
        expected_capability = {
            "accelerator": profile.get("accelerator"),
            "accelerator_runtime": profile.get("accelerator_runtime"),
            "variant": profile.get("variant"),
            "python_abi": profile.get("python_abi"),
            "manylinux": profile.get("builder_manylinux"),
            "cpu_arch": architecture,
        }
        mismatches = {
            key: (expected, validated_item[key])
            for key, expected in expected_capability.items()
            if validated_item[key] != expected
        }
        if mismatches:
            raise ValueError(
                f"{context}: selected capability does not match release profile {profile_id!r}: {mismatches}"
            )
        requirement = requirements[architecture]
        if not isinstance(requirement, dict):
            raise ValueError(
                f"release profile {profile_id}: builder requirement {architecture!r} must be a mapping"
            )
        requirement["root"] = {
            "repository": validated_item["target_repository"],
            "tag": validated_item["target_tag"],
        }

    missing = sorted(expected_coordinates - seen)
    if missing:
        raise ValueError(
            f"missing builder selection for release profile architectures: {missing}"
        )
    extras = sorted(seen - expected_coordinates)
    if extras:
        raise ValueError(
            f"extra builder selection for release profile architectures: {extras}"
        )
    return release
