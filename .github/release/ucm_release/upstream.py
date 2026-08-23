"""Resolve formal upstream source tags, Wheel recipes, and runtime families."""

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
from typing import Any, Callable

from packaging.version import InvalidVersion, Version

from . import core

SELECTION_KIND = "ucm-upstream-selection"
SELECTION_SCHEMA_VERSION = 2
RELEASE_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_ARCHITECTURES = ("amd64", "arm64")

_WHEEL_BUILD_FIELDS = {
    "id",
    "product_id",
    "source_repository",
    "source_ref",
    "build_group",
    "backend",
    "accelerator",
    "accelerator_runtime",
    "variant",
    "soc_version",
    "runtime_variant",
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
    "recipe",
}
_RUNTIME_FIELDS = {
    "id",
    "product_id",
    "source_repository",
    "source_ref",
    "runtime_repository",
    "runtime_tag",
    "runtime_variant",
    "backend",
    "accelerator_runtime",
    "variant",
    "soc_version",
    "python_version",
    "python_abi",
    "os_id",
    "os_version",
    "architectures",
    "member_references",
    "wheel_build_ids",
    "version",
    "channel",
    "target_repository",
    "target_tag",
}
_PROBLEM_FIELDS = {"backend", "capability", "reason", "source", "runtime"}


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
        url = f"https://api.github.com/repos/{self.repository}/contents/{source_path}?ref={ref}"
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


def _source(repository: str, source_ref: str, snapshot_dir: Path | None):
    if snapshot_dir is not None:
        return _SnapshotSource(snapshot_dir, repository)
    return _GitHubSource(repository, source_ref)


def _github_commit(repository: str, source_ref: str) -> str:
    ref = urllib.parse.quote(source_ref, safe="")
    url = f"https://api.github.com/repos/{repository}/commits/{ref}"
    try:
        value = json.loads(_GitHubSource._request(url))
    except json.JSONDecodeError as error:
        raise ValueError(f"GitHub commit response is invalid JSON for {url}") from error
    commit = value.get("sha") if isinstance(value, dict) else None
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError(f"{repository}@{source_ref}: cannot resolve source commit")
    return commit


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
        match = re.fullmatch(
            r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?", line, re.IGNORECASE
        )
        if match and match.group(2) is not None:
            defaults[match.group(1)] = match.group(2).strip().strip("'\"")
    return defaults


def materialize_builder_recipe(text: str, stop_before: str) -> str:
    """Remove one upstream product-build RUN from a Builder Dockerfile."""
    if not stop_before or "\n" in stop_before or "\x00" in stop_before:
        raise ValueError("Builder recipe stop marker is invalid")
    lines = text.splitlines(keepends=True)
    matches: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        start = index
        logical = lines[index]
        while logical.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            logical += lines[index]
        end = index + 1
        if lines[start].lstrip().upper().startswith("RUN ") and stop_before in logical:
            matches.append((start, end))
        index = end
    if len(matches) != 1:
        raise ValueError(
            f"Builder recipe marker {stop_before!r} must match exactly one RUN"
        )
    start, end = matches[0]
    materialized = "".join(lines[:start] + lines[end:])
    return materialized if materialized.endswith("\n") else materialized + "\n"


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
    minimum = Version(_string(product, "minimum_version", "upstream product"))
    if product.get("channel_policy") != "stable-preferred-rc-fallback":
        raise ValueError(
            f"{product.get('id')}: channel_policy must be stable-preferred-rc-fallback"
        )
    candidates = [
        parsed
        for tag in tags
        if (parsed := _parse_base_tag(tag)) is not None and parsed[0] >= minimum
    ]
    stable = [item for item in candidates if not item[0].is_prerelease]
    selected = stable or [item for item in candidates if item[0].is_prerelease]
    if not selected:
        raise ValueError(
            f"{product['id']}: no stable or RC upstream tag meets minimum {minimum}"
        )
    version, source_ref = max(selected, key=lambda item: (item[0], item[1]))
    return str(version), source_ref


def _live_tag_lists(release: Mapping[str, object]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw_product in release["products"]:  # type: ignore[index]
        product = _mapping(raw_product, "upstream product")
        repository = _string(product, "runtime_repository", "upstream product")
        completed = subprocess.run(
            ["crane", "ls", repository], text=True, capture_output=True, check=False
        )
        if completed.returncode != 0:
            raise ValueError(
                f"cannot list upstream tags for {repository}: "
                f"{completed.stderr.strip() or completed.returncode}"
            )
        result[repository] = sorted(set(completed.stdout.splitlines()))
    return result


_BUILD_ARG = re.compile(r"--build-arg\s+([A-Za-z_][A-Za-z0-9_]*)=([^\s\\]+)")
_SHELL_VARIABLE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


def _command_build_args(
    commands: object, variables: Mapping[str, str] | None = None
) -> dict[str, str]:
    known = variables or {}
    arguments: dict[str, str] = {}
    for command in _strings(commands):
        for match in _BUILD_ARG.finditer(command):
            raw_value = match.group(2).strip("'\"")

            def replace(variable: re.Match[str]) -> str:
                name = str(variable.group("braced") or variable.group("plain"))
                if name not in known:
                    raise ValueError(f"unresolved build argument variable {name}")
                return known[name]

            arguments[match.group(1)] = _SHELL_VARIABLE.sub(replace, raw_value)
    return arguments


def _substitute(value: str, variables: Mapping[str, str], context: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = str(match.group("braced") or match.group("plain"))
        if name not in variables:
            raise ValueError(f"{context}: unresolved variable {name}")
        return variables[name]

    result = value
    for _ in range(len(variables) + 1):
        replaced = _SHELL_VARIABLE.sub(replace, result)
        if replaced == result:
            return replaced
        result = replaced
    if _SHELL_VARIABLE.search(result):
        raise ValueError(f"{context}: unresolved variable in {value!r}")
    return result


def _manylinux_from_vllm_image(
    source_image: str, runtime: str, cpu_arch: str, context: str
) -> str:
    match = re.fullmatch(
        r"docker\.io/pytorch/manylinux(?:(?P<arm>aarch64)|(?P<major>\d+)_(?P<minor>\d+))-builder:cuda(?P<runtime>\d+\.\d+)(?:-[A-Za-z0-9_][A-Za-z0-9_.-]*)?",
        source_image,
    )
    if match is None or match.group("runtime") != runtime:
        raise ValueError(f"{context}: malformed CUDA Builder image {source_image!r}")
    image_arch = "arm64" if match.group("arm") else "amd64"
    if image_arch != cpu_arch:
        raise ValueError(f"{context}: Builder architecture mismatch")
    return (
        "manylinux_2_28"
        if image_arch == "arm64"
        else f"manylinux_{match.group('major')}_{match.group('minor')}"
    )


def _vllm_runtime_suffix(commands: object) -> str:
    matches: set[str] = set()
    pattern = re.compile(
        r"\$BUILDKITE_COMMIT(?:-\$\(uname -m\))?((?:-[A-Za-z0-9_.]+)*)"
    )
    for command in _strings(commands):
        matches.update(found.group(1) for found in pattern.finditer(command))
    if not matches:
        raise ValueError("vLLM runtime task has no published BUILDKITE_COMMIT tag")
    return max(matches, key=lambda value: (len(value), value))


def _parse_vllm(
    product: Mapping[str, object], source: object
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    pipeline_path = ".buildkite/release-pipeline.yaml"
    versions_path = "docker/versions.json"
    dockerfile_path = "docker/Dockerfile"
    context = f"{product['source_repository']}@{product['source_ref']}"
    try:
        versions = _mapping(json.loads(source.read(versions_path)), versions_path)  # type: ignore[attr-defined]
    except json.JSONDecodeError as error:
        raise ValueError(f"{context}/{versions_path}: malformed JSON") from error
    variables = _mapping(versions.get("variable"), versions_path)
    defaults = {
        str(key): str(record["default"])
        for key, record in variables.items()
        if isinstance(record, dict) and isinstance(record.get("default"), str)
    }
    dockerfile = source.read(dockerfile_path)  # type: ignore[attr-defined]
    defaults.update(_dockerfile_arg_defaults(dockerfile))
    python_version = defaults.get("PYTHON_VERSION", "")
    python_abi = _python_abi(python_version, versions_path)
    pipeline = core.load_yaml_value(source.read(pipeline_path), context=f"{context}/{pipeline_path}")  # type: ignore[attr-defined]
    pipeline_mapping = _mapping(pipeline, f"{context}/{pipeline_path}")
    raw_env = pipeline_mapping.get("env", {})
    environment = (
        {str(key): str(value) for key, value in raw_env.items()}
        if isinstance(raw_env, dict)
        else {}
    )

    wheel_builds: list[dict[str, object]] = []
    wheel_pattern = re.compile(r"build-wheel-(x86|arm64)-cuda-(\d+)-(\d+)")
    for task in _walk_tasks(pipeline):
        task_id = str(task["id"])
        match = wheel_pattern.fullmatch(task_id)
        if match is None:
            continue
        cpu_arch = "arm64" if match.group(1) == "arm64" else "amd64"
        runtime = f"{match.group(2)}.{match.group(3)}"
        task_args = _command_build_args(task.get("commands"), environment)
        if task_args.get("USE_SCCACHE") == "1":
            task_args["USE_SCCACHE"] = "0"
        if "max_jobs" in task_args:
            task_args["max_jobs"] = "2"
            task_args["nvcc_threads"] = "2"
        effective = {**defaults, **environment, **task_args}
        raw_image = task_args.get("BUILD_BASE_IMAGE", defaults.get("BUILD_BASE_IMAGE"))
        if not raw_image:
            raise ValueError(
                f"{context}/{pipeline_path} task {task_id}: no Builder image"
            )
        source_image = _normalize_image(_substitute(raw_image, effective, task_id))
        manylinux = _manylinux_from_vllm_image(source_image, runtime, cpu_arch, task_id)
        build_group = f"cuda{runtime.replace('.', '')}"
        wheel_builds.append(
            {
                "id": f"{build_group}-{python_abi}-{cpu_arch}",
                "build_group": build_group,
                "backend": "cuda",
                "accelerator": "cuda",
                "accelerator_runtime": f"cuda-{runtime}",
                "variant": "default",
                "soc_version": "na",
                "runtime_variant": f"cu{runtime.replace('.', '')}",
                "python_version": python_version,
                "python_abi": python_abi,
                "manylinux": manylinux,
                "cpu_arch": cpu_arch,
                "source_image": source_image,
                "build_mode": "mirror",
                "mooncake_version": "",
                "recipe": {
                    "dockerfile": dockerfile_path,
                    "target": "base",
                    "build_args": task_args,
                },
                "_recipe_source": dockerfile,
            }
        )

    runtime_groups: dict[tuple[str, str], dict[str, object]] = {}
    runtime_pattern = re.compile(r"build-release-image-(x86|arm64)(?:-.+)?")
    for task in _walk_tasks(pipeline):
        task_id = str(task["id"])
        match = runtime_pattern.fullmatch(task_id)
        if match is None:
            continue
        task_args = _command_build_args(task.get("commands"), environment)
        cuda = task_args.get("CUDA_VERSION")
        if not cuda:
            continue
        runtime_match = re.match(r"(\d+\.\d+)", cuda)
        if runtime_match is None:
            raise ValueError(f"{task_id}: malformed CUDA")
        runtime = runtime_match.group(1)
        effective = {**defaults, **environment, **task_args}
        os_version = task_args.get("UBUNTU_VERSION", "")
        raw_base = task_args.get(
            "BUILD_BASE_IMAGE", defaults.get("BUILD_BASE_IMAGE", "")
        )
        base = _substitute(raw_base, effective, task_id) if raw_base else ""
        if not os_version:
            os_match = re.search(r"ubuntu(\d{2})\.(\d{2})", base)
            if os_match:
                os_version = f"{os_match.group(1)}.{os_match.group(2)}"
        if not os_version:
            os_version = "24.04" if "ubuntu2404" in task_id else "22.04"
        suffix = _vllm_runtime_suffix(task.get("commands"))
        key = (runtime, os_version)
        record = runtime_groups.setdefault(
            key, {"suffix": suffix, "architectures": set()}
        )
        if record["suffix"] != suffix:
            raise ValueError(f"{context}: conflicting runtime suffix for {key}")
        record["architectures"].add("arm64" if match.group(1) == "arm64" else "amd64")  # type: ignore[union-attr]

    if not wheel_builds or not runtime_groups:
        raise ValueError(f"{context}: vLLM Wheel or runtime matrix is empty")
    runtimes: list[dict[str, object]] = []
    for (runtime, os_version), record in sorted(runtime_groups.items()):
        runtime_variant = f"cu{runtime.replace('.', '')}"
        os_token = f"ubuntu{os_version.replace('.', '')}"
        runtimes.append(
            {
                "id": f"{runtime_variant}-{os_token}",
                "runtime_variant": runtime_variant,
                "backend": "cuda",
                "accelerator_runtime": f"cuda-{runtime}",
                "variant": "default",
                "soc_version": "na",
                "python_version": python_version,
                "python_abi": python_abi,
                "os_id": "ubuntu",
                "os_version": os_version,
                "runtime_suffix": str(record["suffix"]),
                "declared_architectures": sorted(record["architectures"]),  # type: ignore[arg-type]
            }
        )
    return wheel_builds, runtimes


def _ascend_wheel_matrix(
    workflow: object, context: str
) -> dict[str, dict[str, list[str]]]:
    jobs = _mapping(_mapping(workflow, context).get("jobs"), context)
    result: dict[str, dict[str, list[str]]] = {}
    for raw_job in jobs.values():
        job = _mapping(raw_job, context)
        strategy = job.get("strategy")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
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
        if match is not None:
            result[match.group(1)] = {
                "python_versions": [str(item) for item in python_versions],
                "operating_systems": [str(item) for item in operating_systems],
            }
    if not result:
        raise ValueError(f"{context}: missing Wheel job matrices")
    return result


def _runner_arch(operating_system: str, context: str) -> str:
    mapping = {"ubuntu-24.04": "amd64", "ubuntu-24.04-arm": "arm64"}
    try:
        return mapping[operating_system]
    except KeyError as error:
        raise ValueError(
            f"{context}: unsupported Wheel runner {operating_system!r}"
        ) from error


def _first_from(text: str, variables: Mapping[str, str], context: str) -> str:
    from_value = next(
        (
            line.strip()[5:].strip()
            for line in text.splitlines()
            if line.strip().upper().startswith("FROM ")
        ),
        None,
    )
    if from_value is None:
        raise ValueError(f"{context}: missing FROM")
    tokens = shlex.split(_substitute(from_value, variables, context))
    images = [token for token in tokens if not token.startswith("--")]
    if not images:
        raise ValueError(f"{context}: missing image in FROM")
    return _normalize_image(images[0])


def _ascend_builder_dockerfile(
    text: str, python_version: str, context: str
) -> tuple[str, str, str, str]:
    variables = _dockerfile_arg_defaults(text)
    variables["PY_VERSION"] = python_version
    source_image = _first_from(text, variables, context)
    match = re.fullmatch(
        r"quay\.io/ascend/manylinux:(?P<runtime>\d+\.\d+\.\d+)-(?P<soc>.+)-manylinux_(?P<major>\d+)_(?P<minor>\d+)-py(?P<python>\d+\.\d+)",
        source_image,
    )
    if match is None or match.group("python") != python_version:
        raise ValueError(f"{context}: malformed manylinux image {source_image!r}")
    return (
        source_image,
        match.group("runtime"),
        f"manylinux_{match.group('major')}_{match.group('minor')}",
        variables.get("SOC_VERSION", match.group("soc")).strip("'\""),
    )


def _ascend_runtime_matrix(workflow: object, context: str) -> list[dict[str, str]]:
    jobs = _mapping(_mapping(workflow, context).get("jobs"), context)
    image_build = _mapping(jobs.get("image_build"), context)
    strategy = _mapping(image_build.get("strategy"), context)
    matrix = _mapping(strategy.get("matrix"), context)
    values = matrix.get("include")
    nested = False
    if not isinstance(values, list) and isinstance(matrix.get("build_meta"), list):
        values = matrix["build_meta"]
        nested = True
    if not isinstance(values, list):
        raise ValueError(f"{context}: missing runtime matrix")
    result: list[dict[str, str]] = []
    for raw in values:
        record = _mapping(raw, context)
        metadata = record if nested else _mapping(record.get("build_meta"), context)
        dockerfile = _string(metadata, "dockerfile", context)
        suffix = metadata.get("suffix", "")
        if not isinstance(suffix, str):
            raise ValueError(f"{context}: runtime suffix must be a string")
        if "310p" not in dockerfile.lower():
            result.append({"dockerfile": dockerfile, "suffix": suffix})
    return result


def _ascend_runtime_dockerfile(text: str, context: str) -> dict[str, str]:
    variables = _dockerfile_arg_defaults(text)
    image = _first_from(text, variables, context)
    match = re.fullmatch(
        r"quay\.io/ascend/cann:(?P<runtime>\d+\.\d+\.\d+)-(?P<soc>.+)-(?P<os>ubuntu|openeuler)(?P<version>\d+\.\d+)-py(?P<python>\d+\.\d+)",
        image,
    )
    if match is None:
        raise ValueError(f"{context}: malformed runtime base image {image!r}")
    return {
        "accelerator_runtime": f"cann-{match.group('runtime')}",
        "soc_version": variables.get("SOC_VERSION", match.group("soc")).strip("'\""),
        "python_version": match.group("python"),
        "python_abi": _python_abi(match.group("python"), context),
        "os_id": match.group("os"),
        "os_version": match.group("version"),
        "mooncake_version": variables.get("MOONCAKE_TAG", "").removeprefix("v"),
    }


def _parse_ascend(
    product: Mapping[str, object], source: object
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    wheel_path = ".github/workflows/schedule_release_code_and_wheel.yml"
    image_path = ".github/workflows/schedule_image_build_and_push.yaml"
    directory = ".github/workflows/dockerfiles"
    prefix = "Dockerfile.buildwheel."
    context = f"{product['source_repository']}@{product['source_ref']}"
    wheel_workflow = core.load_yaml_value(source.read(wheel_path), context=f"{context}/{wheel_path}")  # type: ignore[attr-defined]
    image_workflow = core.load_yaml_value(source.read(image_path), context=f"{context}/{image_path}")  # type: ignore[attr-defined]
    wheel_matrix = _ascend_wheel_matrix(wheel_workflow, f"{context}/{wheel_path}")
    runtime_matrix = _ascend_runtime_matrix(image_workflow, f"{context}/{image_path}")

    runtimes: list[dict[str, object]] = []
    mooncake_by_variant: dict[str, set[str]] = {}
    for record in runtime_matrix:
        dockerfile = record["dockerfile"]
        filename = Path(dockerfile).name
        hardware_name = (
            filename[: -len(".openEuler")]
            if filename.lower().endswith(".openeuler")
            else filename
        )
        if hardware_name == "Dockerfile":
            variant = "a2"
        elif hardware_name.startswith("Dockerfile."):
            variant = hardware_name.removeprefix("Dockerfile.").lower()
        else:
            raise ValueError(
                f"{context}: unsupported runtime Dockerfile {dockerfile!r}"
            )
        parsed = _ascend_runtime_dockerfile(source.read(dockerfile), f"{context}/{dockerfile}")  # type: ignore[attr-defined]
        mooncake_by_variant.setdefault(variant, set()).add(parsed["mooncake_version"])
        runtime_version = parsed["accelerator_runtime"].removeprefix("cann-")
        build_group = f"cann{runtime_version.replace('.', '')}-{variant}"
        os_token = f"{parsed['os_id']}{parsed['os_version'].replace('.', '')}"
        runtimes.append(
            {
                "id": f"{build_group}-{os_token}",
                "runtime_variant": variant,
                "backend": f"cann-{variant}",
                "variant": variant,
                "runtime_suffix": f"-{record['suffix']}" if record["suffix"] else "",
                "declared_architectures": list(SUPPORTED_ARCHITECTURES),
                **{
                    key: value
                    for key, value in parsed.items()
                    if key != "mooncake_version"
                },
            }
        )

    wheel_builds: list[dict[str, object]] = []
    for filename in source.list(directory, prefix):  # type: ignore[attr-defined]
        variant = filename.removeprefix(prefix)
        if variant == "310p":
            continue
        matrix = wheel_matrix.get(variant)
        if matrix is None:
            raise ValueError(f"{context}: {variant} has no Wheel workflow matrix")
        mooncake_versions = mooncake_by_variant.get(variant, set())
        if len(mooncake_versions) != 1:
            raise ValueError(f"{context}: {variant} Mooncake version is not unique")
        mooncake_version = next(iter(mooncake_versions))
        dockerfile_path = f"{directory}/{filename}"
        dockerfile = source.read(dockerfile_path)  # type: ignore[attr-defined]
        for python_version in matrix["python_versions"]:
            source_image, runtime, manylinux, soc_version = _ascend_builder_dockerfile(
                dockerfile, python_version, f"{context}/{dockerfile_path}"
            )
            python_abi = _python_abi(python_version, dockerfile_path)
            build_group = f"cann{runtime.replace('.', '')}-{variant}"
            for operating_system in matrix["operating_systems"]:
                cpu_arch = _runner_arch(operating_system, wheel_path)
                wheel_builds.append(
                    {
                        "id": f"{build_group}-{python_abi}-{cpu_arch}",
                        "build_group": build_group,
                        "backend": f"cann-{variant}",
                        "accelerator": "ascend",
                        "accelerator_runtime": f"cann-{runtime}",
                        "variant": variant,
                        "soc_version": soc_version,
                        "runtime_variant": variant,
                        "python_version": python_version,
                        "python_abi": python_abi,
                        "manylinux": manylinux,
                        "cpu_arch": cpu_arch,
                        "source_image": source_image,
                        "build_mode": "recipe-extend",
                        "mooncake_version": mooncake_version,
                        "recipe": {
                            "dockerfile": dockerfile_path,
                            "target": "",
                            "build_args": {"PY_VERSION": python_version},
                            "strip_run_containing": "python3 setup.py bdist_wheel",
                        },
                        "_recipe_source": materialize_builder_recipe(
                            dockerfile, "python3 setup.py bdist_wheel"
                        ),
                    }
                )
    if not wheel_builds or not runtimes:
        raise ValueError(f"{context}: Ascend Wheel or runtime matrix is empty")
    return wheel_builds, runtimes


def _crane(operation: str, reference: str) -> str:
    completed = subprocess.run(
        ["crane", operation, reference], text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise ValueError(
            f"crane {operation} failed for {reference}: "
            f"{completed.stderr.strip() or completed.returncode}"
        )
    return completed.stdout


def _manifest_members(reference: str) -> dict[str, str]:
    try:
        manifest = json.loads(_crane("manifest", reference))
    except json.JSONDecodeError as error:
        raise ValueError(f"runtime manifest is invalid JSON for {reference}") from error
    repository = reference.rpartition(":")[0]
    members = {
        str(item.get("platform", {}).get("architecture")): (
            f"{repository}@{item['digest']}"
        )
        for item in manifest.get("manifests", [])
        if isinstance(item, dict)
        and isinstance(item.get("platform"), dict)
        and item["platform"].get("os") == "linux"
        and isinstance(item.get("digest"), str)
    }
    if not members:
        try:
            config = json.loads(_crane("config", reference))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"runtime config is invalid JSON for {reference}"
            ) from error
        if config.get("os", "linux") == "linux" and isinstance(
            config.get("architecture"), str
        ):
            digest = _crane("digest", reference).strip()
            members[config["architecture"]] = f"{repository}@{digest}"
    aliases = {"x86_64": "amd64", "aarch64": "arm64"}
    return {aliases.get(key, key): value for key, value in sorted(members.items())}


def _fixture_values(fixture: Mapping[str, object] | None, key: str) -> dict[str, Any]:
    if fixture is None:
        return {}
    value = fixture.get(key, {})
    return _mapping(value, f"tag fixture {key}") if isinstance(value, dict) else {}


def _tag_lists_from_fixture(fixture: Mapping[str, object]) -> dict[str, list[str]]:
    repositories = _mapping(fixture.get("repositories"), "tag fixture")
    result: dict[str, list[str]] = {}
    for repository, raw in repositories.items():
        payload = _mapping(raw, f"tag fixture {repository}")
        tags: set[str] = set()
        for raw_page in payload.get("pages", []):
            page = _mapping(raw_page, f"tag fixture {repository} page")
            tags.update(str(item) for item in page.get("tags", []))
        result[repository] = sorted(tags)
    return result


def _extension_material(accelerator: str) -> dict[str, str]:
    paths = (
        (RELEASE_ROOT / "docker" / "Dockerfile.builder-mirror",)
        if accelerator == "cuda"
        else (
            RELEASE_ROOT / "docker" / "Dockerfile.builder",
            RELEASE_ROOT / "docker" / "mooncake_installer.sh",
            RELEASE_ROOT / "docker" / "gflags-config.cmake",
        )
    )
    return {
        path.relative_to(RELEASE_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in paths
    }


def _finalize_wheel_builds(
    raw_builds: list[dict[str, object]],
    *,
    product: Mapping[str, object],
    digest_for: Callable[[str], str],
    builder_families: Mapping[str, object],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for raw in raw_builds:
        build = copy.deepcopy(raw)
        recipe_source = str(build.pop("_recipe_source"))
        source_image = str(build["source_image"])
        source_digest = digest_for(source_image)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", source_digest) is None:
            raise ValueError(f"invalid source image digest for {source_image}")
        recipe = _mapping(build["recipe"], "Wheel recipe")
        family_id = "cuda" if build["accelerator"] == "cuda" else "ascend"
        builder_family = _mapping(
            builder_families.get(family_id), f"Builder family {family_id}"
        )
        revision_input = {
            "source_ref": product["source_ref"],
            "source_commit": product.get("source_commit", product["source_ref"]),
            "materialized_dockerfile": recipe_source,
            "target": recipe.get("target", ""),
            "build_args": recipe.get("build_args", {}),
            "source_image": source_image,
            "source_image_digest": source_digest,
            "ucm_extension": _extension_material(str(build["accelerator"])),
            "builder_policy": builder_family,
            "mooncake_version": build["mooncake_version"],
        }
        revision = core.sha256_value(revision_input).removeprefix("sha256:")[:12]
        result.append(
            {
                **build,
                "product_id": product["id"],
                "source_repository": product["source_repository"],
                "source_ref": product["source_ref"],
                "source_image_digest": source_digest,
                "recipe_revision": revision,
                "sync_mode": "exact",
            }
        )
    return result


def _link_runtime_wheels(
    runtime: dict[str, object], wheel_builds: list[dict[str, object]]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for architecture in runtime["architectures"]:  # type: ignore[union-attr]
        matches = [
            item
            for item in wheel_builds
            if item["backend"] == runtime["backend"]
            and item["accelerator_runtime"] == runtime["accelerator_runtime"]
            and item["soc_version"] == runtime["soc_version"]
            and item["python_abi"] == runtime["python_abi"]
            and item["cpu_arch"] == architecture
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{runtime['id']}: {architecture} must resolve exactly one Wheel build"
            )
        result[str(architecture)] = str(matches[0]["id"])
    return result


def _blocked_problem(
    backend: str, runtime: Mapping[str, object], reason: str
) -> dict[str, object]:
    capability = "/".join(
        str(runtime[key])
        for key in (
            "accelerator_runtime",
            "soc_version",
            "python_abi",
            "os_id",
            "os_version",
        )
    )
    return {
        "backend": backend,
        "capability": capability,
        "reason": reason,
        "source": {
            "repository": runtime["source_repository"],
            "ref": runtime["source_ref"],
        },
        "runtime": {
            "repository": runtime["runtime_repository"],
            "tag": runtime["runtime_tag"],
        },
    }


def resolve_revision_wheel_builds(
    release: Mapping[str, object],
    *,
    product_id: str,
    source_ref: str,
    probes: Iterable[Mapping[str, object]],
    snapshot_dir: Path | None = None,
    image_digest_resolver: Callable[[str], str] | None = None,
) -> list[dict[str, object]]:
    """Resolve exact-source Builder recipes needed by inspected PR members."""
    product = next(
        (
            copy.deepcopy(_mapping(item, "upstream product"))
            for item in release.get("products", [])  # type: ignore[arg-type]
            if isinstance(item, dict) and item.get("id") == product_id
        ),
        None,
    )
    if product is None:
        raise ValueError(f"unknown upstream product {product_id!r}")
    if not source_ref:
        raise ValueError("exact-source Builder resolution requires a source revision")
    product["source_ref"] = source_ref
    source_repository = _string(product, "source_repository", product_id)
    source = _source(source_repository, source_ref, snapshot_dir)
    if product_id == "vllm":
        raw_builds, _ = _parse_vllm(product, source)
    elif product_id == "vllm-ascend":
        raw_builds, _ = _parse_ascend(product, source)
    else:
        raise ValueError(f"unsupported upstream product {product_id!r}")
    digest_for = image_digest_resolver or (
        lambda reference: _crane("digest", reference).strip()
    )
    product["source_commit"] = source_ref
    builds = _finalize_wheel_builds(
        raw_builds,
        product=product,
        digest_for=digest_for,
        builder_families=_mapping(release.get("builder_families"), "Builder families"),
    )
    selected: dict[str, dict[str, object]] = {}
    for probe in probes:
        capability = {
            field: str(probe[field])
            for field in (
                "backend",
                "accelerator_runtime",
                "soc_version",
                "python_abi",
                "cpu_arch",
            )
        }
        glibc_match = re.fullmatch(r"(\d+)\.(\d+)", str(probe["glibc_version"]))
        if glibc_match is None:
            raise ValueError("runtime probe glibc_version must be major.minor")
        runtime_floor = (int(glibc_match.group(1)), int(glibc_match.group(2)))
        candidates = []
        for build in builds:
            if not all(
                str(build[field]) == value for field, value in capability.items()
            ):
                continue
            floor_match = re.fullmatch(
                r"manylinux_(\d+)_(\d+)", str(build["manylinux"])
            )
            if floor_match is None:
                continue
            floor = (int(floor_match.group(1)), int(floor_match.group(2)))
            if floor <= runtime_floor:
                candidates.append((floor, build))
        if not candidates:
            detail = ", ".join(f"{key}={value}" for key, value in capability.items())
            raise ValueError(f"no exact-source Wheel recipe for {detail}")
        highest = max(floor for floor, _ in candidates)
        matches = [build for floor, build in candidates if floor == highest]
        if len(matches) != 1:
            raise ValueError("exact-source Wheel recipe is ambiguous")
        selected[str(matches[0]["id"])] = matches[0]
    return sorted(selected.values(), key=lambda item: str(item["id"]))


def resolve_upstreams(
    release: Mapping[str, object],
    _legacy_builder_config: Mapping[str, object] | None = None,
    *,
    tag_lists: Mapping[str, list[str]] | None = None,
    tag_fixture: Mapping[str, object] | None = None,
    snapshot_dir: Path | None = None,
    pinned_upstreams: list[str] | None = None,
    image_digest_resolver: Callable[[str], str] | None = None,
    architecture_resolver: Callable[[str], list[str]] | None = None,
    source_commit_resolver: Callable[[str, str], str] | None = None,
) -> dict[str, object]:
    """Resolve formal policy into independent Wheel, runtime, and problem sets."""
    if pinned_upstreams:
        raise ValueError("opaque pinned runtime tags require runtime inspection")
    if tag_lists is not None and tag_fixture is not None:
        raise ValueError("tag_lists and tag_fixture are mutually exclusive")
    if "products" not in release:
        raise ValueError("formal upstream resolution requires release policy schema 4")
    resolved_tags = (
        dict(tag_lists)
        if tag_lists is not None
        else (
            _tag_lists_from_fixture(tag_fixture)
            if tag_fixture is not None
            else _live_tag_lists(release)
        )
    )
    digest_fixture = _fixture_values(tag_fixture, "source_image_digests")
    architecture_fixture = _fixture_values(tag_fixture, "runtime_architectures")
    commit_fixture = _fixture_values(tag_fixture, "source_commits")
    digest_cache: dict[str, str] = {}

    def digest_for(reference: str) -> str:
        if reference not in digest_cache:
            value = digest_fixture.get(reference)
            digest_cache[reference] = (
                str(value)
                if value is not None
                else (
                    image_digest_resolver(reference)
                    if image_digest_resolver is not None
                    else _crane("digest", reference).strip()
                )
            )
        return digest_cache[reference]

    def members_for(reference: str) -> dict[str, str]:
        value = architecture_fixture.get(reference)
        if isinstance(value, list):
            return {str(item): reference for item in sorted(value)}
        if architecture_resolver is not None:
            return {
                str(item): reference
                for item in sorted(architecture_resolver(reference))
            }
        return _manifest_members(reference)

    image_suffix = f"-ucm-{core._oci_tag_version(str(release['ucm_version']))}-r1"
    expected_architectures = sorted(str(key) for key in release["runners"])  # type: ignore[index]
    backends = _mapping(release.get("backends"), "platform backends")
    wheel_builds: list[dict[str, object]] = []
    runtimes: list[dict[str, object]] = []
    problems: list[dict[str, object]] = []

    for raw_product in release["products"]:  # type: ignore[index]
        product = copy.deepcopy(_mapping(raw_product, "upstream product"))
        product_id = _string(product, "id", "upstream product")
        runtime_repository = _string(product, "runtime_repository", product_id)
        version, source_ref = _select_source_tag(
            product, resolved_tags.get(runtime_repository, [])
        )
        product["source_ref"] = source_ref
        source_repository = _string(product, "source_repository", product_id)
        fixture_commit = commit_fixture.get(f"{source_repository}@{source_ref}")
        product["source_commit"] = (
            str(fixture_commit)
            if fixture_commit is not None
            else (
                source_commit_resolver(source_repository, source_ref)
                if source_commit_resolver is not None
                else (
                    source_ref
                    if snapshot_dir is not None
                    else _github_commit(source_repository, source_ref)
                )
            )
        )
        source = _source(source_repository, source_ref, snapshot_dir)
        if product_id == "vllm":
            raw_builds, raw_runtimes = _parse_vllm(product, source)
        elif product_id == "vllm-ascend":
            raw_builds, raw_runtimes = _parse_ascend(product, source)
        else:
            raise ValueError(f"unsupported upstream product {product_id!r}")
        product_builds = _finalize_wheel_builds(
            raw_builds,
            product=product,
            digest_for=digest_for,
            builder_families=_mapping(
                release.get("builder_families"), "Builder families"
            ),
        )
        wheel_builds.extend(product_builds)

        for raw_runtime in raw_runtimes:
            runtime = copy.deepcopy(raw_runtime)
            runtime_suffix = str(runtime.pop("runtime_suffix"))
            declared_architectures = sorted(runtime.pop("declared_architectures"))
            runtime_tag = source_ref + runtime_suffix
            runtime_ref = f"{runtime_repository}:{runtime_tag}"
            backend = str(runtime["backend"])
            raw_backend_policy = backends.get(backend)
            backend_policy = (
                _mapping(raw_backend_policy, f"backend {backend}")
                if raw_backend_policy is not None
                else {
                    "status": "blocked",
                    "reason": f"{backend} has no UCM native backend policy",
                }
            )
            status = backend_policy.get("status")
            reason = str(backend_policy.get("reason", ""))
            published = runtime_tag in set(resolved_tags.get(runtime_repository, []))
            actual_members = members_for(runtime_ref) if published else {}
            actual_architectures = sorted(actual_members)
            complete = (
                declared_architectures == expected_architectures
                and actual_architectures == expected_architectures
            )
            completed_runtime = {
                **runtime,
                "product_id": product_id,
                "source_repository": source_repository,
                "source_ref": source_ref,
                "runtime_repository": runtime_repository,
                "runtime_tag": runtime_tag,
                "architectures": actual_architectures,
                "member_references": actual_members,
                "version": version,
                "channel": _channel(Version(version)),
                "target_repository": product["target_repository"],
                "target_tag": runtime_tag + image_suffix,
            }
            if status == "supported" and (not published or not complete):
                detail = (
                    "is not published"
                    if not published
                    else f"architectures={actual_architectures}, expected={expected_architectures}"
                )
                raise ValueError(f"{runtime_ref}: formal runtime {detail}")
            if not published or not complete:
                problems.append(
                    _blocked_problem(
                        backend,
                        completed_runtime,
                        "; ".join(filter(None, (reason, "runtime is incomplete"))),
                    )
                )
                continue
            completed_runtime["wheel_build_ids"] = _link_runtime_wheels(
                completed_runtime, product_builds
            )
            runtimes.append(completed_runtime)
            if status == "blocked":
                problems.append(_blocked_problem(backend, completed_runtime, reason))

    selection = {
        "kind": SELECTION_KIND,
        "schema_version": SELECTION_SCHEMA_VERSION,
        "wheel_builds": sorted(wheel_builds, key=lambda item: str(item["id"])),
        "runtimes": sorted(runtimes, key=lambda item: str(item["id"])),
        "problems": sorted(
            problems,
            key=lambda item: (
                str(item["backend"]),
                str(item["runtime"]["repository"]),
                str(item["runtime"]["tag"]),  # type: ignore[index]
            ),
        ),
    }
    return validate_selection(selection)


def validate_selection(value: object) -> dict[str, object]:
    selection = _mapping(value, "upstream selection")
    expected = {"kind", "schema_version", "wheel_builds", "runtimes", "problems"}
    if set(selection) != expected:
        raise ValueError("upstream selection fields must be exact")
    if selection.get("kind") != SELECTION_KIND:
        raise ValueError(f"upstream selection kind must be {SELECTION_KIND}")
    if selection.get("schema_version") != SELECTION_SCHEMA_VERSION:
        raise ValueError("upstream selection schema_version must be 2")
    builds = selection.get("wheel_builds")
    runtimes = selection.get("runtimes")
    problems = selection.get("problems")
    if not isinstance(builds, list) or not builds:
        raise ValueError("upstream selection wheel_builds must be non-empty")
    if not isinstance(runtimes, list) or not runtimes:
        raise ValueError("upstream selection runtimes must be non-empty")
    if not isinstance(problems, list):
        raise ValueError("upstream selection problems must be a list")
    build_ids: set[str] = set()
    for index, raw in enumerate(builds):
        build = _mapping(raw, f"wheel_builds[{index}]")
        if set(build) != _WHEEL_BUILD_FIELDS:
            raise ValueError(f"wheel_builds[{index}] fields must be exact")
        build_id = _string(build, "id", f"wheel_builds[{index}]")
        if build_id in build_ids:
            raise ValueError(f"duplicate Wheel build {build_id}")
        build_ids.add(build_id)
        if build.get("cpu_arch") not in SUPPORTED_ARCHITECTURES:
            raise ValueError(f"{build_id}: unsupported cpu_arch")
        if re.fullmatch(r"cp\d+", str(build.get("python_abi"))) is None:
            raise ValueError(f"{build_id}: malformed python_abi")
        if re.fullmatch(r"[0-9a-f]{12}", str(build.get("recipe_revision"))) is None:
            raise ValueError(f"{build_id}: malformed recipe_revision")
        if not isinstance(build.get("recipe"), dict):
            raise ValueError(f"{build_id}: recipe must be a mapping")
    runtime_ids: set[str] = set()
    for index, raw in enumerate(runtimes):
        runtime = _mapping(raw, f"runtimes[{index}]")
        if set(runtime) != _RUNTIME_FIELDS:
            raise ValueError(f"runtimes[{index}] fields must be exact")
        runtime_id = _string(runtime, "id", f"runtimes[{index}]")
        if runtime_id in runtime_ids:
            raise ValueError(f"duplicate runtime {runtime_id}")
        runtime_ids.add(runtime_id)
        architectures = runtime.get("architectures")
        wheel_ids = runtime.get("wheel_build_ids")
        member_references = runtime.get("member_references")
        if (
            not isinstance(architectures, list)
            or not architectures
            or not set(architectures) <= set(SUPPORTED_ARCHITECTURES)
        ):
            raise ValueError(f"{runtime_id}: invalid architectures")
        if not isinstance(wheel_ids, dict) or set(wheel_ids) != set(architectures):
            raise ValueError(f"{runtime_id}: wheel_build_ids must match architectures")
        if (
            not isinstance(member_references, dict)
            or set(member_references) != set(architectures)
            or not all(
                isinstance(reference, str) and reference
                for reference in member_references.values()
            )
        ):
            raise ValueError(
                f"{runtime_id}: member_references must match architectures"
            )
        unknown = set(str(item) for item in wheel_ids.values()) - build_ids
        if unknown:
            raise ValueError(f"{runtime_id}: unknown Wheel builds {sorted(unknown)}")
    seen_problems: set[bytes] = set()
    for index, raw in enumerate(problems):
        problem = _mapping(raw, f"problems[{index}]")
        if set(problem) != _PROBLEM_FIELDS:
            raise ValueError(f"problems[{index}] fields must be exact")
        for nested_key, nested_fields in (
            ("source", {"repository", "ref"}),
            ("runtime", {"repository", "tag"}),
        ):
            nested = _mapping(
                problem.get(nested_key), f"problems[{index}].{nested_key}"
            )
            if set(nested) != nested_fields or not all(
                isinstance(value, str) and value for value in nested.values()
            ):
                raise ValueError(f"problems[{index}].{nested_key} is malformed")
        identity = core.canonical_bytes(problem)
        if identity in seen_problems:
            raise ValueError(f"duplicate upstream problem at index {index}")
        seen_problems.add(identity)
    return selection
