"""Project-level builder discovery, synchronization, and release selection."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable

from . import core

RELEASE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = RELEASE_ROOT / "builders.yaml"
DEFAULT_RELEASE = RELEASE_ROOT / "release.yaml"
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

_BUILDER_LABEL_PREFIX = "io.ucm.builder."
_BUILDER_METADATA_FIELDS = (
    "id",
    "product_id",
    "source_repository",
    "source_ref",
    "build_group",
    "runtime_variant",
    "backend",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "soc_version",
    "python_version",
    "python_abi",
    "manylinux",
    "cpu_arch",
    "source_image",
    "source_image_digest",
    "build_mode",
    "mooncake_version",
    "recipe_revision",
    "sync_mode",
)
_IDENTITY_FIELDS = (
    "project",
    "accelerator",
    *CAPABILITY_FIELDS,
    "target_repository",
)


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


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, object]:
    """Load the adapter-only Builder discovery configuration."""
    config = _require_mapping(_read_yaml(path), str(path))
    if config.get("kind") != "builder-discovery-config":
        raise ValueError(f"{path}: kind must be builder-discovery-config")
    if config.get("schema_version") != 1:
        raise ValueError(f"{path}: schema_version must be 1")
    projects = config.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError(f"{path}: projects must be a non-empty list")
    product_ids: set[str] = set()
    for index, raw in enumerate(projects):
        context = f"{path}: projects[{index}]"
        project = _require_mapping(raw, context)
        product_id = _require_string(project, "product_id", context)
        if product_id in product_ids:
            raise ValueError(f"{context}: duplicate product_id {product_id!r}")
        product_ids.add(product_id)
        discovery = _require_string(project, "discovery", context)
        _require_string(project, "target_repository", context)
        if discovery not in {"vllm-buildkite", "vllm-ascend-actions"}:
            raise ValueError(f"{context}: unsupported discovery {discovery!r}")
        if discovery == "vllm-buildkite":
            _require_string(project, "pipeline_path", context)
            _require_string(project, "versions_path", context)
            _require_string(project, "dockerfile_path", context)
        else:
            _require_string(project, "wheel_workflow_path", context)
            _require_string(project, "image_workflow_path", context)
            _require_string(project, "dockerfile_directory", context)
            _require_string(project, "dockerfile_prefix", context)
            excluded = project.get("exclude_variants")
            if excluded != ["310p"]:
                raise ValueError(f"{context}: exclude_variants must contain only 310p")
            _require_string(project, "mooncake_version", context)
    if product_ids != {"vllm", "vllm-ascend"}:
        raise ValueError(f"{path}: vLLM and vLLM-Ascend discovery are both required")
    if set(config) != {"kind", "schema_version", "projects"}:
        raise ValueError(f"{path}: unsupported top-level fields")
    return config


class _SnapshotSource:
    def __init__(self, root: Path, project: str):
        self.root = root / project

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
        quoted_branch = urllib.parse.quote(self.branch, safe="")
        quoted_path = urllib.parse.quote(path, safe="/")
        url = f"https://raw.githubusercontent.com/{self.project}/{quoted_branch}/{quoted_path}"
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
            f"?ref={urllib.parse.quote(self.branch, safe='')}"
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
    project: dict[str, object], source: object, owner: str
) -> list[dict[str, str]]:
    project_name = str(project["project"])
    directory = str(project["dockerfile_directory"])
    prefix = str(project["dockerfile_prefix"])
    excluded = set(project["exclude_variants"])  # type: ignore[arg-type]
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
                    str(project["mooncake_version"]),
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
) -> dict[str, object]:
    """Discover current upstream builders and append explicit retained builders."""
    config = load_config(config_path)
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
            discovered.extend(_discover_ascend(project, source, resolved_owner))
    discovered.extend(_retained_items(config, resolved_owner))
    return {
        "kind": "ucm-builder-catalog",
        "schema_version": 1,
        "builders": _deduplicate(discovered),
    }


def _generated_target_tag(
    build_group: str,
    python_abi: str,
    manylinux: str,
    cpu_arch: str,
    recipe_revision: str,
) -> str:
    parts = [
        build_group,
        python_abi,
        manylinux.replace("manylinux_", "manylinux"),
        cpu_arch,
        f"r{recipe_revision}",
    ]
    tag = "-".join(parts)
    if core.OCI_TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"generated Builder tag is invalid: {tag!r}")
    return tag


def catalog_from_selection(
    selection: object,
    config_path: Path | None = None,
    *,
    owner: str | None = None,
    formal_policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Project one exact upstream selection into Builder synchronization tasks."""
    from . import policy, upstream

    resolved = upstream.validate_selection(selection)
    resolved_owner = _owner(owner)
    if formal_policy is None:
        bundle = policy.load(
            platforms_path=(
                config_path
                if config_path is not None and config_path.name == "platforms.yaml"
                else None
            )
        )
        platform_policy = bundle["platforms"]
    else:
        platform_policy = _require_mapping(formal_policy, "formal platform policy")
        if "platforms" in platform_policy:
            platform_policy = _require_mapping(
                platform_policy["platforms"], "formal platform policy.platforms"
            )
    families = _require_mapping(
        platform_policy.get("builder_families"), "builder_families"
    )
    backend_policies = _require_mapping(
        platform_policy.get("backends"), "platform backends"
    )
    items: list[dict[str, object]] = []
    for raw_group in resolved["wheel_builds"]:  # type: ignore[index]
        group = _require_mapping(raw_group, "Wheel build")
        _require_string(group, "product_id", "Wheel build")
        family_id = "cuda" if group.get("accelerator") == "cuda" else "ascend"
        family = _require_mapping(
            families.get(family_id), f"builder family {family_id}"
        )
        target_repository = _expand_owner(
            _require_string(family, "target_repository", f"builder family {family_id}"),
            resolved_owner,
        )
        build_group = _require_string(group, "build_group", "Wheel build")
        python_abi = _require_string(group, "python_abi", "Wheel build")
        manylinux = _require_string(group, "manylinux", "Wheel build")
        cpu_arch = _require_string(group, "cpu_arch", "Wheel build")
        recipe_revision = _require_string(group, "recipe_revision", "Wheel build")
        raw_backend_policy = backend_policies.get(str(group["backend"]))
        backend_policy = (
            _require_mapping(raw_backend_policy, f"platform backend {group['backend']}")
            if raw_backend_policy is not None
            else {"status": "blocked"}
        )
        item = {
            **copy.deepcopy(group),
            "target_repository": target_repository,
            "target_tag": _generated_target_tag(
                build_group, python_abi, manylinux, cpu_arch, recipe_revision
            ),
            "checks": {
                "commands": copy.deepcopy(family["required_commands"]),
                "mooncake": family_id == "ascend",
                "blocking": backend_policy.get("status") == "supported",
                "python_version": group["python_version"],
                "python_abi": python_abi,
                "accelerator_runtime": group["accelerator_runtime"],
            },
        }
        items.append(item)
    catalog = {
        "kind": "ucm-builder-catalog",
        "schema_version": 2,
        "builders": sorted(items, key=lambda item: str(item["id"])),
    }
    return validate_catalog(catalog)


def validate_catalog(catalog: object) -> dict[str, object]:
    mapping = _require_mapping(catalog, "builder catalog")
    if mapping.get("kind") != "ucm-builder-catalog":
        raise ValueError("builder catalog: kind must be ucm-builder-catalog")
    schema_version = mapping.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("builder catalog: schema_version must be 1 or 2")
    values = mapping.get("builders")
    if not isinstance(values, list):
        raise ValueError("builder catalog: builders must be a list")
    if schema_version == 1:
        for index, item in enumerate(values):
            _validate_catalog_item(item, f"builder catalog builders[{index}]")
        return mapping
    ids: set[str] = set()
    target_coordinates: set[tuple[str, str]] = set()
    required = {
        "id",
        "product_id",
        "source_repository",
        "source_ref",
        "build_group",
        "runtime_variant",
        "backend",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "soc_version",
        "python_version",
        "python_abi",
        "manylinux",
        "cpu_arch",
        "source_image",
        "source_image_digest",
        "target_repository",
        "target_tag",
        "build_mode",
        "mooncake_version",
        "recipe_revision",
        "sync_mode",
        "recipe",
        "checks",
    }
    for index, raw_item in enumerate(values):
        context = f"builder catalog builders[{index}]"
        item = _require_mapping(raw_item, context)
        if set(item) != required:
            raise ValueError(f"{context}: fields must be exact")
        item_id = _require_string(item, "id", context)
        if item_id in ids:
            raise ValueError(f"duplicate Builder id {item_id!r}")
        ids.add(item_id)
        if item.get("cpu_arch") not in {"amd64", "arm64"}:
            raise ValueError(f"{context}: unsupported cpu_arch")
        if item.get("build_mode") not in {"mirror", "recipe", "recipe-extend"}:
            raise ValueError(f"{context}: unsupported build_mode")
        if item.get("sync_mode") not in {"exact", "registry-only"}:
            raise ValueError(f"{context}: unsupported sync_mode")
        if not isinstance(item.get("recipe"), dict):
            raise ValueError(f"{context}: recipe must be a mapping")
        if not isinstance(item.get("checks"), dict):
            raise ValueError(f"{context}: checks must be a mapping")
        _validate_oci_image(str(item.get("source_image")), f"{context} source_image")
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("source_image_digest")))
            is None
        ):
            raise ValueError(f"{context}: source_image_digest is invalid")
        if re.fullmatch(r"[0-9a-f]{12}", str(item.get("recipe_revision"))) is None:
            raise ValueError(f"{context}: recipe_revision is invalid")
        _validate_oci_repository(
            str(item.get("target_repository")), f"{context} target_repository"
        )
        if core.OCI_TAG_PATTERN.fullmatch(str(item.get("target_tag"))) is None:
            raise ValueError(f"{context}: target_tag is invalid")
        coordinate = (str(item["target_repository"]), str(item["target_tag"]))
        if coordinate in target_coordinates:
            raise ValueError(f"duplicate Builder target {coordinate!r}")
        target_coordinates.add(coordinate)
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
    sort_fields = ("id",) if validated["schema_version"] == 2 else CATALOG_FIELDS
    missing = sorted(
        (
            item
            for item in validated["builders"]  # type: ignore[union-attr]
            if item["target_tag"]
            not in normalized.get(item["target_repository"], set())
        ),
        key=lambda item: tuple(item[field] for field in sort_fields),
    )
    registry_only_missing = [
        item for item in missing if item.get("sync_mode") == "registry-only"
    ]
    if registry_only_missing:
        coordinates = sorted(
            f"{item['target_repository']}:{item['target_tag']}"
            for item in registry_only_missing
        )
        raise ValueError(
            "Registry-only Builders disappeared after capability matching: "
            f"{coordinates}"
        )
    matrix = []
    for item in missing:
        if validated["schema_version"] == 2:
            matrix.append(
                {
                    **item,
                    "label": (
                        f"{item['build_group']} · {item['python_abi']} · "
                        f"{item['cpu_arch']}"
                    ),
                }
            )
            continue
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


def builder_labels(builder: Mapping[str, object]) -> dict[str, str]:
    """Project one catalog entry into OCI labels used by PR inventory scans."""
    item = _require_mapping(dict(builder), "Builder label source")
    missing = [field for field in _BUILDER_METADATA_FIELDS if field not in item]
    if missing:
        raise ValueError(f"Builder label source is missing {missing}")
    labels = {
        f"{_BUILDER_LABEL_PREFIX}schema": "1",
        f"{_BUILDER_LABEL_PREFIX}checked": "true",
        f"{_BUILDER_LABEL_PREFIX}target_tag": _require_string(
            item, "target_tag", "Builder label source"
        ),
    }
    for field in _BUILDER_METADATA_FIELDS:
        value = item[field]
        if not isinstance(value, str) or (not value and field != "mooncake_version"):
            raise ValueError(f"Builder label field {field} must be a non-empty string")
        labels[f"{_BUILDER_LABEL_PREFIX}{field}"] = value
    return labels


def registry_builder_record(
    repository: str, tag: str, config: object
) -> dict[str, object] | None:
    """Read one checked final Builder record from its OCI config labels."""
    if core.OCI_REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError(f"invalid Builder repository {repository!r}")
    if core.OCI_TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"invalid Builder tag {tag!r}")
    document = _require_mapping(config, f"Builder config {repository}:{tag}")
    nested = document.get("config", {})
    nested = _require_mapping(nested, f"Builder config {repository}:{tag}.config")
    raw_labels = nested.get("Labels") or {}
    labels = _require_mapping(
        raw_labels, f"Builder config {repository}:{tag}.config.Labels"
    )
    if labels.get(f"{_BUILDER_LABEL_PREFIX}schema") != "1":
        return None
    if labels.get(f"{_BUILDER_LABEL_PREFIX}checked") != "true":
        return None
    if labels.get(f"{_BUILDER_LABEL_PREFIX}target_tag") != tag:
        return None
    metadata: dict[str, object] = {}
    for field in _BUILDER_METADATA_FIELDS:
        value = labels.get(f"{_BUILDER_LABEL_PREFIX}{field}")
        if not isinstance(value, str) or (not value and field != "mooncake_version"):
            raise ValueError(f"Builder {repository}:{tag} label {field} is missing")
        metadata[field] = value
    created = labels.get(f"{_BUILDER_LABEL_PREFIX}created", document.get("created"))
    if not isinstance(created, str) or not created:
        raise ValueError(f"Builder {repository}:{tag} created timestamp is missing")
    return {
        **metadata,
        "target_repository": repository,
        "target_tag": tag,
        "created": created,
        "checked": True,
        "recipe": {},
        "checks": {},
    }


def _crane_output(operation: str, reference: str) -> str:
    completed = subprocess.run(
        ["crane", operation, reference], text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise ValueError(
            f"crane {operation} failed for {reference}: "
            f"{completed.stderr.strip() or completed.returncode}"
        )
    return completed.stdout


def _live_builder_config(reference: str) -> object:
    try:
        manifest = json.loads(_crane_output("manifest", reference))
    except json.JSONDecodeError as error:
        raise ValueError(f"Builder manifest is malformed for {reference}") from error
    descriptors = manifest.get("manifests") if isinstance(manifest, dict) else None
    if not isinstance(descriptors, list):
        return json.loads(_crane_output("config", reference))
    repository = reference.rpartition(":")[0]
    configs = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        platform = descriptor.get("platform")
        digest = descriptor.get("digest")
        if (
            isinstance(platform, dict)
            and platform.get("os") == "linux"
            and platform.get("architecture") in {"amd64", "arm64"}
            and isinstance(digest, str)
        ):
            configs.append(
                json.loads(_crane_output("config", f"{repository}@{digest}"))
            )
    if len(configs) != 1:
        raise ValueError(f"Builder {reference} must contain exactly one Linux member")
    return configs[0]


def scan_registry_builders(
    formal_policy: Mapping[str, object],
    *,
    tag_loader=None,
    config_loader=None,
) -> dict[str, object]:
    """Scan only labelled, functionally promoted final Builder tags."""
    policy_mapping = _require_mapping(dict(formal_policy), "formal policy")
    if "platforms" in policy_mapping:
        policy_mapping = _require_mapping(
            policy_mapping["platforms"], "formal policy.platforms"
        )
    families = _require_mapping(
        policy_mapping.get("builder_families"), "builder_families"
    )
    repositories = sorted(
        {
            _require_string(
                _require_mapping(item, "builder family"),
                "target_repository",
                "builder family",
            )
            for item in families.values()
        }
    )
    load_tags = tag_loader or (
        lambda repository: _crane_output("ls", repository).splitlines()
    )
    load_config = config_loader or _live_builder_config
    records: list[dict[str, object]] = []
    for repository in repositories:
        for tag in sorted(set(str(item) for item in load_tags(repository))):
            if re.search(r"-r[0-9a-f]{12}$", tag) is None:
                continue
            record = registry_builder_record(
                repository, tag, load_config(f"{repository}:{tag}")
            )
            if record is not None:
                records.append(record)
    return {
        "kind": "ucm-builder-registry",
        "schema_version": 1,
        "builders": sorted(
            records,
            key=lambda item: (
                str(item["id"]),
                str(item["created"]),
                str(item["target_tag"]),
            ),
        ),
    }


def catalog_from_registry_records(
    records: Iterable[Mapping[str, object]]
) -> dict[str, object]:
    """Reopen selected Registry records as the compact Builder Catalog."""
    items: list[dict[str, object]] = []
    for raw in records:
        record = dict(raw)
        items.append(
            {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if key not in {"created", "checked"}
            }
        )
    return validate_catalog(
        {
            "kind": "ucm-builder-catalog",
            "schema_version": 2,
            "builders": sorted(items, key=lambda item: str(item["id"])),
        }
    )


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
    """Select one builder for every release wheel profile and architecture."""
    validated = validate_catalog(catalog)
    available = list(validated["builders"])  # type: ignore[arg-type]
    release_config = _require_mapping(release, "release config")
    profiles = release_config.get("wheel_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("release config: wheel_profiles must be a non-empty list")
    selected: list[dict[str, str]] = []
    profile_ids: set[str] = set()
    for index, raw_profile in enumerate(profiles):
        context = f"release config wheel_profiles[{index}]"
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
        if accelerator == "cuda":
            variant = "default"
        elif accelerator == "ascend":
            npu_arch = profile.get("npu_arch")
            if (
                not isinstance(npu_arch, list)
                or len(npu_arch) != 1
                or not isinstance(npu_arch[0], str)
            ):
                raise ValueError(f"{context}: Ascend selection requires one npu_arch")
            variant = npu_arch[0]
        else:
            raise ValueError(f"{context}: unsupported accelerator {accelerator!r}")
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
    """Bind one selected project builder to every release profile architecture."""
    release = copy.deepcopy(_require_mapping(catalog, "release catalog"))
    profiles = release.get("wheel_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("release catalog: wheel_profiles must be a non-empty list")

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
        context = f"release catalog wheel_profiles[{index}]"
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
        npu_arch = profile.get("npu_arch")
        expected_variant = (
            "default"
            if profile.get("accelerator") == "cuda"
            else npu_arch[0] if isinstance(npu_arch, list) and npu_arch else None
        )
        expected_capability = {
            "accelerator": profile.get("accelerator"),
            "accelerator_runtime": profile.get("accelerator_runtime"),
            "variant": expected_variant,
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
