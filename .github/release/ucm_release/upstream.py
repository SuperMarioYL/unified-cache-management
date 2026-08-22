"""Resolve upstream runtime tags and their authoritative wheel build recipes."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from . import core

SELECTION_KIND = "ucm-upstream-selection"
SELECTION_SCHEMA_VERSION = 1


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected a mapping")
    return value


def _string(mapping: Mapping[str, object], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}: {key} must be a non-empty string")
    return value


class _SnapshotSource:
    def __init__(self, root: Path, repository: str):
        self.root = root / repository

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
            item.name for item in root.iterdir() if item.name.startswith(prefix)
        )


class _GitHubSource:
    def __init__(self, repository: str, source_ref: str):
        self.repository = repository
        self.source_ref = source_ref

    @staticmethod
    def _request(url: str) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ucm-upstream-resolution",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers)
            ) as response:
                return response.read()
        except OSError as error:
            raise ValueError(f"GitHub request failed for {url}: {error}") from error

    def read(self, path: str) -> str:
        ref = urllib.parse.quote(self.source_ref, safe="")
        source_path = urllib.parse.quote(path, safe="/")
        url = f"https://raw.githubusercontent.com/{self.repository}/{ref}/{source_path}"
        try:
            return self._request(url).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(
                f"{self.repository}@{self.source_ref}/{path}: non-UTF-8 source"
            ) from error

    def list(self, directory: str, prefix: str) -> list[str]:
        source_path = urllib.parse.quote(directory, safe="/")
        ref = urllib.parse.quote(self.source_ref, safe="")
        url = (
            f"https://api.github.com/repos/{self.repository}/contents/{source_path}"
            f"?ref={ref}"
        )
        try:
            value = json.loads(self._request(url))
        except json.JSONDecodeError as error:
            raise ValueError(f"GitHub response is invalid JSON for {url}") from error
        if not isinstance(value, list):
            raise ValueError(f"{self.repository}/{directory}: listing is not a list")
        return sorted(
            item["name"]
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].startswith(prefix)
        )


def _source(
    repository: str, source_ref: str, snapshot_dir: Path | None
) -> _SnapshotSource | _GitHubSource:
    if snapshot_dir is not None:
        return _SnapshotSource(snapshot_dir, repository)
    return _GitHubSource(repository, source_ref)


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


def _python_abi(version: str, context: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version)
    if match is None:
        raise ValueError(f"{context}: malformed Python version {version!r}")
    return f"cp{match.group(1)}{match.group(2)}"


def _normalize_image(image: str) -> str:
    first = image.split("/", 1)[0]
    if "." not in first and ":" not in first and first != "localhost":
        return f"docker.io/{image}"
    return image


def _dockerfile_arg_defaults(text: str) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("FROM "):
            break
        match = re.fullmatch(
            r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?", line, re.IGNORECASE
        )
        if match and match.group(2) is not None:
            defaults[match.group(1)] = match.group(2).strip().strip("'\"")
    return defaults


def _channel(version: Version) -> str:
    return "rc" if version.is_prerelease else "stable"


def _parse_base_tag(tag: str) -> tuple[Version, str] | None:
    if re.fullmatch(r"v?\d+\.\d+\.\d+(?:rc\d+)?", tag) is None:
        return None
    try:
        return Version(tag.removeprefix("v")), tag
    except InvalidVersion:
        return None


def _select_source_tag(
    product: Mapping[str, object], tags: list[str]
) -> tuple[str, str]:
    specifier = SpecifierSet(_string(product, "version_specifier", "upstream product"))
    channels = product.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("upstream product channels must be a non-empty list")
    candidates: list[tuple[Version, str]] = []
    for tag in tags:
        parsed = _parse_base_tag(tag)
        if parsed is None:
            continue
        version, source_ref = parsed
        if version in specifier and _channel(version) in channels:
            candidates.append((version, source_ref))
    if not candidates:
        raise ValueError(
            f"{product['id']}: no upstream tag matches {specifier} and {channels}"
        )
    version, source_ref = max(candidates, key=lambda item: (item[0], item[1]))
    return str(version), source_ref


def _live_tag_lists(release: Mapping[str, object]) -> dict[str, list[str]]:
    limit = int(release["scan_limits"]["max_tags_per_repository"])  # type: ignore[index]
    result: dict[str, list[str]] = {}
    for raw_product in release["upstream_products"]:  # type: ignore[index]
        product = _mapping(raw_product, "upstream product")
        repository = _string(product, "runtime_repository", "upstream product")
        completed = subprocess.run(
            ["crane", "ls", repository],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"cannot list upstream tags for {repository}: "
                f"{completed.stderr.strip() or completed.returncode}"
            )
        tags = sorted(set(completed.stdout.splitlines()))
        if len(tags) > limit:
            raise ValueError(f"upstream tag limit {limit} exceeded for {repository}")
        result[repository] = tags
    return result


_BUILD_ARG = re.compile(r"--build-arg\s+([A-Za-z_][A-Za-z0-9_]*)=([^\s\\]+)")
_SHELL_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _command_build_args(
    commands: object, variables: Mapping[str, str] | None = None
) -> dict[str, str]:
    known = variables or {}
    args: dict[str, str] = {}
    for command in _strings(commands):
        for match in _BUILD_ARG.finditer(command):
            raw_value = match.group(2).strip("'\"")

            def replace(variable: re.Match[str]) -> str:
                name = variable.group(1)
                if name not in known:
                    raise ValueError(f"unresolved build argument variable {name}")
                return known[name]

            args[match.group(1)] = _SHELL_VARIABLE.sub(replace, raw_value)
    return args


def _vllm_runtime_suffixes(
    pipeline: object, context: str, variables: Mapping[str, str]
) -> dict[str, str]:
    suffixes: dict[str, str] = {}
    for task in _walk_tasks(pipeline):
        task_id = str(task["id"])
        if not task_id.startswith("build-release-image-") or "ubuntu2404" in task_id:
            continue
        commands = "\n".join(_strings(task.get("commands")))
        args = _command_build_args(task.get("commands"), variables)
        cuda = args.get("CUDA_VERSION")
        if not cuda:
            continue
        match = re.match(r"(\d+\.\d+)", cuda)
        if match is None:
            raise ValueError(f"{context} task {task_id}: malformed CUDA_VERSION")
        runtime = match.group(1)
        suffix_match = re.search(
            r"\$BUILDKITE_COMMIT(?:-\$\(uname -m\))?(-cu\d+)", commands
        )
        suffix = suffix_match.group(1) if suffix_match else ""
        previous = suffixes.setdefault(runtime, suffix)
        if previous != suffix:
            raise ValueError(
                f"{context}: conflicting runtime suffixes for CUDA {runtime}"
            )
    if not suffixes:
        raise ValueError(f"{context}: missing default-OS CUDA release image tasks")
    return suffixes


def _parse_vllm(
    product: Mapping[str, object], adapter: Mapping[str, object], source: object
) -> list[dict[str, object]]:
    pipeline_path = _string(adapter, "pipeline_path", "vLLM adapter")
    versions_path = _string(adapter, "versions_path", "vLLM adapter")
    dockerfile_path = _string(adapter, "dockerfile_path", "vLLM adapter")
    context = f"{product['source_repository']}@{product['source_ref']}"
    try:
        versions = _mapping(json.loads(source.read(versions_path)), versions_path)  # type: ignore[attr-defined]
    except json.JSONDecodeError as error:
        raise ValueError(f"{context}/{versions_path}: malformed JSON") from error
    variables = _mapping(versions.get("variable"), versions_path)
    python_record = _mapping(variables.get("PYTHON_VERSION"), versions_path)
    python_version = _string(python_record, "default", versions_path)
    python_abi = _python_abi(python_version, versions_path)
    pipeline = core.load_yaml_value(
        source.read(pipeline_path), context=f"{context}/{pipeline_path}"  # type: ignore[attr-defined]
    )
    pipeline_mapping = _mapping(pipeline, f"{context}/{pipeline_path}")
    pipeline_env = pipeline_mapping.get("env", {})
    env = (
        {str(key): str(value) for key, value in pipeline_env.items()}
        if isinstance(pipeline_env, dict)
        else {}
    )
    defaults = {
        str(key): str(value["default"])
        for key, value in variables.items()
        if isinstance(value, dict) and isinstance(value.get("default"), str)
    }
    defaults.update(_dockerfile_arg_defaults(source.read(dockerfile_path)))  # type: ignore[attr-defined]
    if "BUILD_BASE_IMAGE" not in defaults or "BUILD_OS" not in defaults:
        raise ValueError(
            f"{context}/{versions_path}: BUILD_BASE_IMAGE and BUILD_OS defaults are required"
        )
    suffixes = _vllm_runtime_suffixes(pipeline, f"{context}/{pipeline_path}", env)
    task_pattern = re.compile(r"build-wheel-(x86|arm64)-cuda-(\d+)-(\d+)")
    image_pattern = re.compile(r"(?:^|\s)BUILD_BASE_IMAGE=(?:['\"])?([^\s'\"\\]+)")
    groups: list[dict[str, object]] = []
    for task in _walk_tasks(pipeline):
        task_id = str(task["id"])
        match = task_pattern.fullmatch(task_id)
        if match is None:
            continue
        runtime = f"{match.group(2)}.{match.group(3)}"
        suffix = suffixes.get(runtime)
        if suffix is None:
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: no matching default-OS runtime"
            )
        task_args = _command_build_args(task.get("commands"), env)
        images = {
            found.group(1)
            for command in _strings(task.get("commands"))
            for found in image_pattern.finditer(command)
        }
        if len(images) > 1:
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: BUILD_BASE_IMAGE must resolve once"
            )
        effective = {**defaults, **env, **task_args}
        source_image_value = (
            next(iter(images))
            if images
            else _SHELL_VARIABLE.sub(
                lambda variable: effective[variable.group(1)],
                defaults["BUILD_BASE_IMAGE"],
            )
        )
        source_image = _normalize_image(source_image_value)
        image_match = re.fullmatch(
            r"docker\.io/pytorch/manylinux(?:(?P<arm>aarch64)|(?P<major>\d+)_(?P<minor>\d+))-builder:cuda(?P<runtime>\d+\.\d+)(?:-[A-Za-z0-9_][A-Za-z0-9_.-]*)?",
            source_image,
        )
        cuda_image_match = re.fullmatch(
            r"docker\.io/nvidia/cuda:(?P<runtime>\d+\.\d+)(?:\.\d+)?-devel-ubuntu\d+\.\d+",
            source_image,
        )
        if image_match is None and cuda_image_match is None:
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: malformed CUDA builder image"
            )
        build_os = task_args.get("BUILD_OS", defaults["BUILD_OS"])
        if image_match is not None and image_match.group("runtime") != runtime:
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: malformed CUDA builder image"
            )
        if (
            cuda_image_match is not None
            and cuda_image_match.group("runtime") != runtime
        ):
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: CUDA base image does not match task"
            )
        cpu_arch = "arm64" if match.group(1) == "arm64" else "amd64"
        image_arch = (
            "arm64" if image_match is not None and image_match.group("arm") else "amd64"
        )
        if image_match is not None and image_arch != cpu_arch:
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: builder architecture mismatch"
            )
        if build_os == "manylinux":
            if image_match is None:
                raise ValueError(
                    f"{context}/{pipeline_path} task {task_id}: manylinux build has no manylinux Builder image"
                )
            manylinux = (
                "manylinux_2_28"
                if image_match.group("arm")
                else f"manylinux_{image_match.group('major')}_{image_match.group('minor')}"
            )
        else:
            manylinux = "linux"
        runtime_variant = suffix.removeprefix("-") or f"cu{runtime.replace('.', '')}"
        build_group = f"cuda{runtime.replace('.', '')}"
        groups.append(
            {
                "id": f"{build_group}-{python_abi}-{cpu_arch}",
                "build_group": build_group,
                "backend": "cuda",
                "accelerator": "cuda",
                "accelerator_runtime": f"cuda-{runtime}",
                "variant": "default",
                "soc_version": "na",
                "runtime_variant": runtime_variant,
                "runtime_suffix": suffix,
                "python_version": python_version,
                "python_abi": python_abi,
                "manylinux": manylinux,
                "cpu_arch": cpu_arch,
                "source_image": source_image,
                "build_mode": (
                    "mirror" if images and build_os == "manylinux" else "recipe"
                ),
                "recipe": {
                    "dockerfile": dockerfile_path,
                    "target": "build",
                    "build_args": task_args,
                },
            }
        )
    if not groups:
        raise ValueError(f"{context}/{pipeline_path}: no CUDA wheel tasks found")
    return groups


_VARIABLE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


def _substitute(value: str, variables: Mapping[str, str], context: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain")
        replacement = variables.get(str(name))
        if replacement is None:
            raise ValueError(f"{context}: unresolved ARG {name} in FROM")
        return replacement

    return _VARIABLE.sub(replace, value)


def _ascend_dockerfile(
    text: str, python_version: str, context: str
) -> tuple[str, str, str, str]:
    variables: dict[str, str] = {"PY_VERSION": python_version}
    from_value: str | None = None
    soc_version = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        arg = re.fullmatch(
            r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?", line, re.IGNORECASE
        )
        if arg:
            value = arg.group(2)
            if value is not None and arg.group(1) != "PY_VERSION":
                variables[arg.group(1)] = value.strip().strip("'\"")
            if arg.group(1) == "SOC_VERSION" and value is not None:
                soc_version = value.strip().strip("'\"")
            continue
        match = re.fullmatch(r"FROM\s+(.+)", line, re.IGNORECASE)
        if match and from_value is None:
            from_value = match.group(1)
    if from_value is None:
        raise ValueError(f"{context}: missing FROM")
    substituted = _substitute(from_value, variables, context)
    try:
        tokens = shlex.split(substituted)
    except ValueError as error:
        raise ValueError(f"{context}: malformed FROM") from error
    images = [token for token in tokens if not token.startswith("--")]
    if not images:
        raise ValueError(f"{context}: missing image in FROM")
    image = _normalize_image(images[0])
    match = re.fullmatch(
        r"quay\.io/ascend/manylinux:(?P<runtime>\d+\.\d+\.\d+)-(?P<soc>.+)-manylinux_(?P<major>\d+)_(?P<minor>\d+)-py(?P<python>\d+\.\d+)",
        image,
    )
    if match is None or match.group("python") != python_version:
        raise ValueError(f"{context}: malformed manylinux image {image!r}")
    resolved_soc = soc_version or match.group("soc")
    return (
        image,
        match.group("runtime"),
        f"manylinux_{match.group('major')}_{match.group('minor')}",
        resolved_soc,
    )


def _ascend_runtime_suffixes(workflow: object, context: str) -> dict[str, str]:
    jobs = _mapping(_mapping(workflow, context).get("jobs"), context)
    image_build = _mapping(jobs.get("image_build"), context)
    strategy = _mapping(image_build.get("strategy"), context)
    matrix = _mapping(strategy.get("matrix"), context)
    includes = matrix.get("include")
    nested_build_meta = False
    if not isinstance(includes, list) and isinstance(matrix.get("build_meta"), list):
        includes = matrix["build_meta"]
        nested_build_meta = True
    if not isinstance(includes, list):
        raise ValueError(f"{context}: missing image build include matrix")
    suffixes: dict[str, str] = {}
    for raw in includes:
        record = _mapping(raw, context)
        build_meta = (
            record if nested_build_meta else _mapping(record.get("build_meta"), context)
        )
        dockerfile = _string(build_meta, "dockerfile", context)
        suffix = build_meta.get("suffix", "")
        if not isinstance(suffix, str):
            raise ValueError(f"{context}: image suffix must be a string")
        if "openEuler" in dockerfile or "310p" in dockerfile:
            continue
        if dockerfile == "Dockerfile":
            suffixes["a2"] = suffix
        elif dockerfile == "Dockerfile.a3":
            suffixes["a3"] = suffix
    if set(suffixes) != {"a2", "a3"}:
        raise ValueError(f"{context}: default A2/A3 runtime variants are required")
    return suffixes


def _ascend_wheel_matrix(
    workflow: object, context: str
) -> dict[str, dict[str, list[str]]]:
    jobs = _mapping(_mapping(workflow, context).get("jobs"), context)
    result: dict[str, dict[str, list[str]]] = {}
    for raw_job in jobs.values():
        job = _mapping(raw_job, context)
        strategy = job.get("strategy")
        if not isinstance(strategy, dict):
            continue
        matrix = strategy.get("matrix")
        if not isinstance(matrix, dict):
            continue
        python_versions = matrix.get("python-version")
        operating_systems = matrix.get("os")
        if not isinstance(python_versions, list) or not isinstance(
            operating_systems, list
        ):
            continue
        commands = "\n".join(
            _strings(
                [
                    step.get("run")
                    for step in job.get("steps", [])
                    if isinstance(step, dict)
                ]
            )
        )
        match = re.search(r"Dockerfile\.buildwheel\.([a-z0-9-]+)", commands)
        if match is None:
            continue
        variant = match.group(1)
        result[variant] = {
            "python_versions": [str(item) for item in python_versions],
            "operating_systems": [str(item) for item in operating_systems],
        }
    if not result:
        raise ValueError(f"{context}: missing wheel job matrices")
    return result


def _runner_arch(operating_system: str, context: str) -> str:
    mapping = {"ubuntu-24.04": "amd64", "ubuntu-24.04-arm": "arm64"}
    try:
        return mapping[operating_system]
    except KeyError as error:
        raise ValueError(
            f"{context}: unsupported default runner {operating_system!r}"
        ) from error


def _parse_ascend(
    product: Mapping[str, object], adapter: Mapping[str, object], source: object
) -> list[dict[str, object]]:
    wheel_workflow_path = _string(adapter, "wheel_workflow_path", "Ascend adapter")
    image_workflow_path = _string(adapter, "image_workflow_path", "Ascend adapter")
    directory = _string(adapter, "dockerfile_directory", "Ascend adapter")
    prefix = _string(adapter, "dockerfile_prefix", "Ascend adapter")
    excluded = adapter.get("exclude_variants")
    if excluded != ["310p"]:
        raise ValueError("Ascend adapter must exclude only 310p")
    context = f"{product['source_repository']}@{product['source_ref']}"
    wheel_workflow = core.load_yaml_value(
        source.read(wheel_workflow_path), context=f"{context}/{wheel_workflow_path}"  # type: ignore[attr-defined]
    )
    image_workflow = core.load_yaml_value(
        source.read(image_workflow_path), context=f"{context}/{image_workflow_path}"  # type: ignore[attr-defined]
    )
    matrix = _ascend_wheel_matrix(wheel_workflow, f"{context}/{wheel_workflow_path}")
    suffixes = _ascend_runtime_suffixes(
        image_workflow, f"{context}/{image_workflow_path}"
    )
    filenames = source.list(directory, prefix)  # type: ignore[attr-defined]
    groups: list[dict[str, object]] = []
    for filename in filenames:
        variant = filename.removeprefix(prefix)
        if variant in excluded:
            continue
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", variant) is None:
            raise ValueError(f"{context}/{directory}/{filename}: malformed variant")
        job_matrix = matrix.get(variant)
        runtime_suffix = suffixes.get(variant)
        if job_matrix is None or runtime_suffix is None:
            raise ValueError(
                f"{context}: variant {variant!r} has no wheel/runtime workflow support"
            )
        dockerfile_path = f"{directory}/{filename}"
        dockerfile = source.read(dockerfile_path)  # type: ignore[attr-defined]
        for python_version in job_matrix["python_versions"]:
            source_image, runtime, manylinux, soc_version = _ascend_dockerfile(
                dockerfile,
                python_version,
                f"{context}/{dockerfile_path}",
            )
            python_abi = _python_abi(python_version, dockerfile_path)
            build_group = f"cann{runtime.replace('.', '')}-{variant}"
            for operating_system in job_matrix["operating_systems"]:
                cpu_arch = _runner_arch(operating_system, wheel_workflow_path)
                groups.append(
                    {
                        "id": f"{build_group}-{python_abi}-{cpu_arch}",
                        "build_group": build_group,
                        "backend": f"cann-{variant}",
                        "accelerator": "ascend",
                        "accelerator_runtime": f"cann-{runtime}",
                        "variant": variant,
                        "soc_version": soc_version,
                        "runtime_variant": variant,
                        "runtime_suffix": (
                            f"-{runtime_suffix}" if runtime_suffix else ""
                        ),
                        "python_version": python_version,
                        "python_abi": python_abi,
                        "manylinux": manylinux,
                        "cpu_arch": cpu_arch,
                        "source_image": source_image,
                        "build_mode": "recipe-extend",
                        "recipe": {
                            "dockerfile": dockerfile_path,
                            "target": "",
                            "build_args": {"PY_VERSION": python_version},
                        },
                    }
                )
    if not groups:
        raise ValueError(f"{context}: no supported Ascend wheel groups found")
    return groups


def _adapter_by_product(
    builder_config: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    projects = builder_config.get("projects")
    if not isinstance(projects, list):
        raise ValueError("builder config projects must be a list")
    result: dict[str, dict[str, object]] = {}
    for raw in projects:
        project = _mapping(raw, "builder adapter")
        product_id = _string(project, "product_id", "builder adapter")
        if product_id in result:
            raise ValueError(f"duplicate builder adapter for {product_id}")
        result[product_id] = project
    return result


def _tag_lists_from_fixture(fixture: Mapping[str, object]) -> dict[str, list[str]]:
    repositories = _mapping(fixture.get("repositories"), "tag fixture")
    result: dict[str, list[str]] = {}
    for repository, raw in repositories.items():
        payload = _mapping(raw, f"tag fixture {repository}")
        tags: set[str] = set()
        pages = payload.get("pages", [])
        if isinstance(pages, list):
            for raw_page in pages:
                page = _mapping(raw_page, f"tag fixture {repository} page")
                values = page.get("tags", [])
                if isinstance(values, list):
                    tags.update(str(item) for item in values)
        snapshots = payload.get("snapshots", {})
        if isinstance(snapshots, dict):
            tags.update(str(item) for item in snapshots)
        result[repository] = sorted(tags)
    return result


def resolve_upstreams(
    release: Mapping[str, object],
    builder_config: Mapping[str, object],
    *,
    tag_lists: Mapping[str, list[str]] | None = None,
    tag_fixture: Mapping[str, object] | None = None,
    snapshot_dir: Path | None = None,
    pinned_upstreams: list[str] | None = None,
) -> dict[str, object]:
    """Select each upstream once, then parse recipes at that exact Git tag."""
    if tag_lists is not None and tag_fixture is not None:
        raise ValueError("tag_lists and tag_fixture are mutually exclusive")
    resolved_tags = (
        dict(tag_lists)
        if tag_lists is not None
        else (
            _tag_lists_from_fixture(tag_fixture)
            if tag_fixture is not None
            else _live_tag_lists(release)
        )
    )
    adapters = _adapter_by_product(builder_config)
    pinned_by_repository: dict[str, set[str]] = {}
    for reference in pinned_upstreams or []:
        repository, separator, tag = reference.rpartition(":")
        if not separator or not repository or not tag:
            raise ValueError(f"unsupported pinned upstream {reference!r}")
        pinned_by_repository.setdefault(repository, set()).add(tag)
    image_suffix = f"-ucm-{core._oci_tag_version(str(release['ucm_version']))}-r{release.get('image_revision', 1)}"
    upstreams: list[dict[str, object]] = []
    for raw_product in release["upstream_products"]:  # type: ignore[index]
        product = copy.deepcopy(_mapping(raw_product, "upstream product"))
        product_id = _string(product, "id", "upstream product")
        adapter = adapters.get(product_id)
        if adapter is None:
            raise ValueError(f"{product_id}: missing builder adapter")
        runtime_repository = _string(product, "runtime_repository", product_id)
        pinned_tags = pinned_by_repository.get(runtime_repository)
        if pinned_upstreams and not pinned_tags:
            continue
        if pinned_tags:
            source_refs = {
                re.sub(
                    r"-(?:cu\d+|a\d+)$",
                    "",
                    tag,
                )
                for tag in pinned_tags
            }
            if len(source_refs) != 1:
                raise ValueError(
                    f"{product_id}: pinned runtime variants must share one source Tag"
                )
            source_ref = next(iter(source_refs))
            parsed = _parse_base_tag(source_ref)
            if parsed is None:
                raise ValueError(f"{product_id}: pinned Tag is not a semantic version")
            version = str(parsed[0])
            missing_pins = pinned_tags - set(resolved_tags.get(runtime_repository, []))
            if missing_pins:
                raise ValueError(
                    f"{product_id}: pinned runtime tags are not published: {sorted(missing_pins)}"
                )
        else:
            version, source_ref = _select_source_tag(
                product, resolved_tags.get(runtime_repository, [])
            )
        product["source_ref"] = source_ref
        source_repository = _string(product, "source_repository", product_id)
        source = _source(source_repository, source_ref, snapshot_dir)
        discovery = _string(adapter, "discovery", f"{product_id} adapter")
        groups = (
            _parse_vllm(product, adapter, source)
            if discovery == "vllm-buildkite"
            else (
                _parse_ascend(product, adapter, source)
                if discovery == "vllm-ascend-actions"
                else None
            )
        )
        if groups is None:
            raise ValueError(f"{product_id}: unsupported discovery {discovery!r}")
        by_family: dict[str, list[dict[str, object]]] = {}
        for group in groups:
            by_family.setdefault(str(group["build_group"]), []).append(group)
        runtime_tags = set(resolved_tags.get(runtime_repository, []))
        for family_id, family_groups in sorted(by_family.items()):
            runtime_suffixes = {str(item["runtime_suffix"]) for item in family_groups}
            if len(runtime_suffixes) != 1:
                raise ValueError(f"{family_id}: runtime suffix must be unique")
            runtime_suffix = next(iter(runtime_suffixes))
            runtime_tag = source_ref + runtime_suffix
            if pinned_tags and runtime_tag not in pinned_tags:
                continue
            if runtime_tag not in runtime_tags:
                raise ValueError(
                    f"{product_id}: runtime variant {runtime_tag!r} is not published"
                )
            upstreams.append(
                {
                    "product_id": product_id,
                    "source_repository": source_repository,
                    "source_ref": source_ref,
                    "runtime_repository": runtime_repository,
                    "runtime_tag": runtime_tag,
                    "runtime_variant": str(family_groups[0]["runtime_variant"]),
                    "version": version,
                    "channel": _channel(Version(version)),
                    "family_id": family_id,
                    "target_repository": _string(
                        product, "target_repository", product_id
                    ),
                    "target_tag": (
                        f"{source_ref}-{family_groups[0]['runtime_variant']}"
                        if product_id == "vllm"
                        else runtime_tag
                    )
                    + image_suffix,
                    "integration_python_abi": _string(
                        product, "integration_python_abi", product_id
                    ),
                    "build_groups": sorted(
                        family_groups, key=lambda item: str(item["id"])
                    ),
                }
            )
    unknown_pin_repositories = set(pinned_by_repository) - {
        str(item["runtime_repository"])
        for item in release["upstream_products"]  # type: ignore[index]
    }
    if unknown_pin_repositories:
        raise ValueError(
            f"unsupported pinned upstream repositories: {sorted(unknown_pin_repositories)}"
        )
    selection = {
        "kind": SELECTION_KIND,
        "schema_version": SELECTION_SCHEMA_VERSION,
        "upstreams": sorted(upstreams, key=lambda item: str(item["family_id"])),
    }
    return validate_selection(selection)


def validate_selection(value: object) -> dict[str, object]:
    selection = _mapping(value, "upstream selection")
    if selection.get("kind") != SELECTION_KIND:
        raise ValueError(f"upstream selection kind must be {SELECTION_KIND}")
    if selection.get("schema_version") != SELECTION_SCHEMA_VERSION:
        raise ValueError("upstream selection schema_version must be 1")
    upstreams = selection.get("upstreams")
    if not isinstance(upstreams, list) or not upstreams:
        raise ValueError("upstream selection must contain upstreams")
    family_ids: set[str] = set()
    group_ids: set[str] = set()
    for raw in upstreams:
        item = _mapping(raw, "upstream selection item")
        family_id = _string(item, "family_id", "upstream selection item")
        if family_id in family_ids:
            raise ValueError(f"duplicate upstream family {family_id}")
        family_ids.add(family_id)
        groups = item.get("build_groups")
        if not isinstance(groups, list) or not groups:
            raise ValueError(f"{family_id}: build_groups must be non-empty")
        for raw_group in groups:
            group = _mapping(raw_group, f"{family_id} build group")
            group_id = _string(group, "id", f"{family_id} build group")
            if group_id in group_ids:
                raise ValueError(f"duplicate upstream build group {group_id}")
            group_ids.add(group_id)
            if group.get("cpu_arch") not in {"amd64", "arm64"}:
                raise ValueError(f"{group_id}: unsupported cpu_arch")
            if re.fullmatch(r"cp\d+", str(group.get("python_abi"))) is None:
                raise ValueError(f"{group_id}: malformed python_abi")
            if not isinstance(group.get("recipe"), dict):
                raise ValueError(f"{group_id}: recipe must be a mapping")
    return selection
