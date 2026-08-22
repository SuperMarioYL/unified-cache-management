"""Canonical normalization for release capability coordinates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_ACCELERATOR_RUNTIME = re.compile(r"^(cuda|cann)-(.+)$", re.ASCII)
_COMPACT = re.compile(r"^[a-z0-9]+$", re.ASCII)
_VARIANT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$", re.ASCII)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_COMMIT = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_MANYLINUX = re.compile(r"^manylinux_[0-9]+_[0-9]+$", re.ASCII)

CATALOG_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "source_sha",
        "upstream_reads",
        "builder_sync",
        "builder_capabilities",
        "builder_revisions",
        "runtime_candidates",
        "bindings",
        "entries",
        "exclusions",
        "catalog_sha256",
    }
)
CAPABILITY_FIELDS = frozenset(
    {
        "builder_capability_id",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "cpu_architecture",
        "manylinux",
        "python_version",
        "python_abi",
        "mooncake_version",
        "builder_revision_ids",
    }
)
REVISION_FIELDS = frozenset(
    {
        "builder_revision_id",
        "builder_capability_id",
        "source_image_repository",
        "source_image_digest",
        "recipe_path",
        "recipe_source_commit",
        "recipe_sha256",
        "toolchain_sha256",
        "target_repository",
        "target_tag",
        "target_builder_digest",
        "revision_sha256",
    }
)
RUNTIME_FIELDS = frozenset(
    {
        "runtime_id",
        "product_id",
        "runtime_repository",
        "runtime_tag",
        "runtime_version",
        "channel",
        "variant",
        "cpu_architecture",
        "accelerator",
        "accelerator_runtime",
        "mooncake_version",
        "runtime_image",
        "git_tag",
        "git_commit",
    }
)
BINDING_FIELDS = frozenset(
    {
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "cpu_architecture",
        "manylinux",
        "python_version",
        "python_abi",
        "source_image",
        "target_image",
        "mooncake_version",
        "recipe_path",
        "recipe_source_commit",
        "recipe_sha256",
        "toolchain_sha256",
        "target_builder_digest",
        "mooncake_copy_mode",
        "runtime_image",
    }
)
ENTRY_FIELDS = frozenset(
    {
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
        "accelerator",
        "accelerator_runtime",
        "variant",
        "cpu_architecture",
        "manylinux",
        "python_version",
        "python_abi",
        "source_image",
        "target_image",
        "mooncake_version",
        "mooncake_copy_mode",
        "runtime_image",
    }
)
EXCLUSION_FIELDS = frozenset(
    {
        "reason_code",
        "source_kind",
        "source_id",
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
        "evidence",
    }
)
BUILDER_SYNC_FIELDS = frozenset({"mode", "target_digests_verified", "deletions"})
BUILDER_FACT_FIELDS = frozenset(
    {
        "builder_fact_id",
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
        "target_builder_digest",
        "mooncake_source_runtime_id",
        "mooncake_source_runtime_image",
        "mooncake_version",
    }
)
BUILDER_FACT_IDENTITY_FIELDS = (
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
    "target_builder_digest",
    "mooncake_source_runtime_id",
    "mooncake_source_runtime_image",
    "mooncake_version",
)
PYTHON_PROBE_FIELDS = frozenset(
    {
        "builder_fact_id",
        "builder_image",
        "target_builder_digest",
        "cpu_architecture",
        "manylinux",
        "runner",
        "interpreter_path",
        "python_version",
        "python_abi",
        "soabi",
        "wheel_tag",
    }
)
BUILDER_FAILURE_FIELDS = frozenset(
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
PYTHON_PROBE_FAILURE_FIELDS = frozenset(
    {
        "status",
        "reason_code",
        "source_kind",
        "source_id",
        "builder_fact_id",
        "builder_image",
        "target_builder_digest",
        "cpu_architecture",
        "manylinux",
        "runner",
        "interpreter_path",
        "builder_capability_id",
        "builder_revision_id",
        "runtime_id",
        "evidence",
    }
)
MOONCAKE_PROBE_FAILURE_FIELDS = frozenset(
    {
        "status",
        "reason_code",
        "source_kind",
        "source_id",
        "runtime_id",
        "runtime_image",
        "runtime_image_digest",
        "cpu_architecture",
        "runner",
        "builder_capability_id",
        "builder_revision_id",
        "evidence",
    }
)
MOONCAKE_PROBE_FIELDS = frozenset(
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
RUNTIME_DISCOVERY_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "source_sha",
        "upstream_reads",
        "runtime_candidates",
        "runtime_probe_matrix",
    }
)
UPSTREAM_READ_FIELDS = frozenset(
    {"project", "source_kind", "source_path", "source_commit", "fact"}
)

_CAPABILITY_IDENTITY_FIELDS = (
    "accelerator",
    "accelerator_runtime",
    "variant",
    "python_version",
    "python_abi",
    "cpu_architecture",
    "manylinux",
    "mooncake_version",
)
_REVISION_IDENTITY_FIELDS = (
    "builder_capability_id",
    "source_image_digest",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_builder_digest",
)
_RUNTIME_IDENTITY_FIELDS = (
    "product_id",
    "runtime_repository",
    "runtime_tag",
    "variant",
    "cpu_architecture",
)
_CAPABILITY_PROJECTION_FIELDS = (
    "accelerator",
    "accelerator_runtime",
    "variant",
    "cpu_architecture",
    "manylinux",
    "python_version",
    "python_abi",
    "mooncake_version",
)
_REVISION_PROJECTION_FIELDS = (
    "recipe_path",
    "recipe_source_commit",
    "recipe_sha256",
    "toolchain_sha256",
    "target_builder_digest",
)
_RUNTIME_EXACT_COMPATIBILITY_FIELDS = (
    "accelerator",
    "variant",
    "cpu_architecture",
    "mooncake_version",
)


def _parse_version(value: object, label: str) -> Version:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty version string")
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"{label} is not a valid version") from exc
    if parsed.epoch or parsed.local:
        raise ValueError(f"{label} must not use epochs or local versions")
    return parsed


def _compact_version(parsed: Version, label: str) -> str:
    compact = "".join(str(part) for part in parsed.release)
    if parsed.pre is not None:
        compact += f"{parsed.pre[0]}{parsed.pre[1]}"
    if parsed.post is not None:
        compact += f"post{parsed.post}"
    if parsed.dev is not None:
        compact += f"dev{parsed.dev}"
    if _COMPACT.fullmatch(compact) is None:
        raise ValueError(f"{label} does not have a canonical compact form")
    return compact


def compact_accelerator_runtime(value: object) -> str:
    """Return the canonical compact token for a CUDA or CANN runtime."""
    if not isinstance(value, str):
        raise ValueError("accelerator runtime must be a string")
    match = _ACCELERATOR_RUNTIME.fullmatch(value)
    if match is None:
        raise ValueError(
            "accelerator runtime must use cuda-<version> or cann-<version>"
        )
    parsed = _parse_version(match.group(2), "accelerator runtime version")
    return _compact_version(parsed, "accelerator runtime version")


def compact_mooncake_version(value: object) -> str:
    """Return the canonical compact token for a Mooncake version."""
    return _compact_version(
        _parse_version(value, "Mooncake version"), "Mooncake version"
    )


def _runtime_compatible(capability: dict[str, Any], runtime: dict[str, Any]) -> bool:
    if any(
        capability[field] != runtime[field]
        for field in _RUNTIME_EXACT_COMPATIBILITY_FIELDS
    ):
        return False
    capability_runtime = _string(
        capability.get("accelerator_runtime"), "Builder accelerator runtime"
    )
    runtime_runtime = _string(
        runtime.get("accelerator_runtime"), "runtime accelerator runtime"
    )
    if capability["accelerator"] != "cuda":
        return capability_runtime == runtime_runtime
    capability_match = _ACCELERATOR_RUNTIME.fullmatch(capability_runtime)
    runtime_match = _ACCELERATOR_RUNTIME.fullmatch(runtime_runtime)
    if (
        capability_match is None
        or capability_match.group(1) != "cuda"
        or runtime_match is None
        or runtime_match.group(1) != "cuda"
    ):
        return False
    capability_version = _parse_version(
        capability_match.group(2), "Builder CUDA runtime version"
    )
    runtime_version = _parse_version(runtime_match.group(2), "runtime CUDA version")
    if len(capability_version.release) < 2 or len(runtime_version.release) < 2:
        return False
    return capability_version.release[:2] == runtime_version.release[:2]


def normalize_variant(value: object) -> str:
    """Normalize one non-empty OCI/PEP 503-safe variant token."""
    if not isinstance(value, str) or _VARIANT.fullmatch(value) is None:
        raise ValueError("variant must be a non-empty OCI/PEP 503-safe token")
    normalized = canonicalize_name(value)
    if not normalized or _COMPACT.fullmatch(normalized.replace("-", "")) is None:
        raise ValueError("variant has no canonical token")
    return normalized


def python_version_from_abi(value: object) -> str:
    """Derive the dotted CPython version represented by a cpXY[t] ABI."""
    if not isinstance(value, str):
        raise ValueError("Python ABI must be a string")
    match = re.fullmatch(r"cp([0-9])([0-9]{1,2})t?", value, re.ASCII)
    if match is None:
        raise ValueError("Python ABI must use canonical cpXY or cpXYt form")
    return f"{match.group(1)}.{int(match.group(2))}"


def compile_python_coordinate(validated_fields: object) -> dict[str, str]:
    """Compile one closed, validated CPython build coordinate."""
    fields = _mapping(validated_fields, "Python coordinate fields")
    expected_fields = {
        "python_version",
        "python_abi",
        "cpu_architecture",
        "manylinux",
    }
    if set(fields) != expected_fields:
        raise ValueError("Python coordinate input fields must be exact")
    version = _string(fields["python_version"], "Python coordinate version")
    abi = _string(fields["python_abi"], "Python coordinate ABI")
    if python_version_from_abi(abi) != version:
        raise ValueError("Python coordinate version and ABI differ")
    python_tag = "cp" + version.replace(".", "")
    if abi not in {python_tag, python_tag + "t"}:
        raise ValueError("Python coordinate ABI is not canonical")
    architecture = _string(fields["cpu_architecture"], "Python coordinate architecture")
    platform_architecture = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }.get(architecture)
    if platform_architecture is None:
        raise ValueError("Python coordinate architecture is unsupported")
    manylinux = _string(fields["manylinux"], "Python coordinate manylinux")
    if _MANYLINUX.fullmatch(manylinux) is None:
        raise ValueError("Python coordinate manylinux is invalid")
    return {
        "python_tag": python_tag,
        "interpreter_path": f"/opt/python/{python_tag}-{abi}/bin/python",
        "expected_soabi": (
            f"cpython-{abi.removeprefix('cp')}-{platform_architecture}-linux-gnu"
        ),
        "expected_wheel_tag": (
            f"{python_tag}-{abi}-{manylinux}_{platform_architecture}"
        ),
    }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _commit(value: object, label: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 40-character lowercase Git commit")
    return value


def _exact_fields(value: dict[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        missing = sorted(fields - set(value))
        unknown = sorted(set(value) - fields)
        raise ValueError(
            f"{label} fields are not closed: missing={missing}, extra={unknown}"
        )


def _identity(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: value[field] for field in fields}


def _unique_records(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {_canonical_bytes(value): value for value in values}
    return [copy.deepcopy(unique[key]) for key in sorted(unique)]


def _exclusion_key(value: dict[str, Any]) -> tuple[str, ...]:
    return (
        value["reason_code"],
        value["source_kind"],
        value["source_id"],
        value["builder_capability_id"] or "",
        value["builder_revision_id"] or "",
        value["runtime_id"] or "",
    )


def _entry_key(value: dict[str, Any]) -> tuple[str, ...]:
    return (
        value["accelerator"],
        value["accelerator_runtime"],
        value["variant"],
        value["python_abi"],
        value["cpu_architecture"],
        value["builder_revision_id"],
        value["runtime_id"],
    )


def _exclusion(
    *,
    reason_code: str,
    source_kind: str,
    source_id: str,
    evidence: dict[str, Any],
    builder_capability_id: str | None = None,
    builder_revision_id: str | None = None,
    runtime_id: str | None = None,
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "source_kind": source_kind,
        "source_id": source_id,
        "builder_capability_id": builder_capability_id,
        "builder_revision_id": builder_revision_id,
        "runtime_id": runtime_id,
        "evidence": copy.deepcopy(evidence),
    }


def normalize_runtime_candidate(raw: object) -> dict[str, Any]:
    """Normalize one raw runtime discovery fact into its closed public record."""
    raw = _mapping(raw, "runtime discovery fact")
    repository = _string(raw.get("runtime_image_repository"), "runtime repository")
    tag = _string(raw.get("runtime_image_tag"), "runtime tag")
    image_digest = _digest(raw.get("runtime_image_digest"), "runtime image digest")
    record = {
        "runtime_id": "",
        "product_id": _string(raw.get("product_id"), "runtime product_id"),
        "runtime_repository": repository,
        "runtime_tag": tag,
        "runtime_version": _string(
            raw.get("runtime_version"), "runtime runtime_version"
        ),
        "channel": _string(raw.get("channel"), "runtime channel"),
        "variant": normalize_variant(raw.get("variant")),
        "cpu_architecture": _string(
            raw.get("cpu_architecture"), "runtime cpu_architecture"
        ),
        "accelerator": _string(raw.get("accelerator"), "runtime accelerator"),
        "accelerator_runtime": _string(
            raw.get("accelerator_runtime"), "runtime accelerator_runtime"
        ),
        "mooncake_version": raw.get("mooncake_version"),
        "runtime_image": f"{repository}@{image_digest}",
        "git_tag": _string(raw.get("git_tag"), "runtime git_tag"),
        "git_commit": _commit(raw.get("git_commit"), "runtime git_commit"),
    }
    compact_accelerator_runtime(record["accelerator_runtime"])
    if record["accelerator"] not in {"cuda", "ascend"}:
        raise ValueError("runtime accelerator must be cuda or ascend")
    if record["cpu_architecture"] not in {"amd64", "arm64"}:
        raise ValueError("runtime cpu_architecture must be amd64 or arm64")
    if record["accelerator"] == "cuda":
        if record["mooncake_version"] is not None:
            raise ValueError("CUDA runtime Mooncake version must be null")
    else:
        compact_mooncake_version(record["mooncake_version"])
    record["runtime_id"] = _canonical_digest(
        _identity(record, _RUNTIME_IDENTITY_FIELDS)
    )
    return record


def _validate_upstream_reads(value: object, label: str) -> list[dict[str, Any]]:
    reads = _array(value, label)
    if not reads:
        raise ValueError(f"{label} must be non-empty")
    for index, raw in enumerate(reads):
        read = _mapping(raw, f"{label}[{index}]")
        _exact_fields(read, UPSTREAM_READ_FIELDS, f"{label}[{index}]")
        for field in ("project", "source_kind", "source_path", "fact"):
            _string(read[field], f"{label}[{index}] {field}")
        _commit(read["source_commit"], f"{label}[{index}] source_commit")
    return reads


def discover_runtime_candidates(
    builder_discovery: object, runtime_sources: object
) -> dict[str, Any]:
    """Bind immutable runtime images to one unambiguous variant Dockerfile."""
    builders_input = _mapping(builder_discovery, "Builder source discovery")
    sources_input = _mapping(runtime_sources, "runtime sources")
    source_sha = _commit(
        builders_input.get("source_sha"), "Builder source discovery source_sha"
    )
    runtime_source_sha = _commit(
        sources_input.get("source_sha"), "runtime sources source_sha"
    )
    if runtime_source_sha != source_sha:
        raise ValueError("Builder and runtime discovery source_sha differ")
    builders = _array(builders_input.get("builders"), "Builder sources")
    reads = _validate_upstream_reads(
        sources_input.get("upstream_reads"), "runtime upstream reads"
    )

    records: list[dict[str, Any]] = []
    matrix: list[dict[str, Any]] = []
    for index, raw_value in enumerate(
        _array(sources_input.get("candidates"), "runtime source candidates")
    ):
        raw = _mapping(raw_value, f"runtime source candidates[{index}]")
        candidates = _array(
            raw.get("variant_candidates"),
            f"runtime source candidates[{index}] variant_candidates",
        )
        normalized_variants = sorted({normalize_variant(value) for value in candidates})
        if len(normalized_variants) != 1:
            raise ValueError("runtime candidate variant must be unique")
        variant = normalized_variants[0]
        accelerator = _string(
            raw.get("accelerator"), f"runtime source candidates[{index}] accelerator"
        )
        architecture = _string(
            raw.get("cpu_architecture"),
            f"runtime source candidates[{index}] cpu_architecture",
        )
        if not any(
            isinstance(builder, dict)
            and builder.get("accelerator") == accelerator
            and builder.get("variant") == variant
            and builder.get("cpu_architecture") == architecture
            for builder in builders
        ):
            raise ValueError("runtime candidate has no matching Builder platform")

        git_ref = _mapping(
            raw.get("git_ref"), f"runtime source candidates[{index}] git_ref"
        )
        git_tag = _string(
            git_ref.get("tag"), f"runtime source candidates[{index}] git tag"
        )
        object_type = _string(
            git_ref.get("object_type"),
            f"runtime source candidates[{index}] git object type",
        )
        if object_type not in {"tag", "commit"}:
            raise ValueError("runtime Git ref must resolve to a tag or commit")
        git_commit = _commit(
            git_ref.get("target_commit"),
            f"runtime source candidates[{index}] target commit",
        )

        runtime_dockerfiles = _array(
            raw.get("runtime_dockerfiles"),
            f"runtime source candidates[{index}] runtime_dockerfiles",
        )
        matching = [
            _mapping(item, "runtime Dockerfile")
            for item in runtime_dockerfiles
            if isinstance(item, dict)
            and normalize_variant(item.get("variant")) == variant
        ]
        if accelerator == "ascend" and len(matching) != 1:
            raise ValueError("runtime candidate must bind one matching Dockerfile")
        dockerfile = matching[0] if matching else None
        mooncake_version = None
        mooncake_source_path = None
        if dockerfile is not None:
            mooncake_source_path = _string(
                dockerfile.get("source_path"), "runtime Dockerfile source_path"
            )
            if (
                _commit(
                    dockerfile.get("source_commit"), "runtime Dockerfile source_commit"
                )
                != git_commit
            ):
                raise ValueError("runtime Dockerfile commit differs from peeled tag")
            mooncake_version = _string(
                dockerfile.get("mooncake_version"),
                "runtime Dockerfile Mooncake version",
            )

        record = {
            "product_id": _string(raw.get("product_id"), "runtime product_id"),
            "runtime_version": _string(raw.get("runtime_version"), "runtime version"),
            "channel": _string(raw.get("channel"), "runtime channel"),
            "variant": variant,
            "cpu_architecture": architecture,
            "accelerator": accelerator,
            "accelerator_runtime": _string(
                raw.get("accelerator_runtime"), "accelerator runtime"
            ),
            "mooncake_version": mooncake_version,
            "runtime_image_repository": _string(
                raw.get("runtime_image_repository"), "runtime image repository"
            ),
            "runtime_image_tag": _string(
                raw.get("runtime_image_tag"), "runtime image tag"
            ),
            "runtime_image_digest": _digest(
                raw.get("runtime_image_digest"), "runtime image digest"
            ),
            "git_tag": git_tag,
            "git_commit": git_commit,
        }
        if mooncake_source_path is not None:
            record["mooncake_source_path"] = mooncake_source_path
        normalized = normalize_runtime_candidate(record)
        records.append(record)
        if accelerator == "ascend":
            read = next(
                (
                    item
                    for item in reads
                    if item["source_path"] == mooncake_source_path
                    and item["source_commit"] == git_commit
                ),
                None,
            )
            if read is None:
                raise ValueError("runtime Dockerfile has no matching upstream read")
            raw_url = (
                "https://raw.githubusercontent.com/"
                f"{read['project']}/{git_commit}/{mooncake_source_path}"
            )
            matrix.append(
                {
                    "id": normalized["runtime_id"].removeprefix("sha256:"),
                    "runtime_id": normalized["runtime_id"],
                    "runtime_image": normalized["runtime_image"],
                    "runtime_image_digest": record["runtime_image_digest"],
                    "runtime_dockerfile": raw_url,
                    "mooncake_version": normalized["mooncake_version"],
                    "cpu_architecture": architecture,
                    "runner": (
                        "ubuntu-24.04-arm"
                        if architecture == "arm64"
                        else "ubuntu-24.04"
                    ),
                }
            )

    records.sort(key=lambda item: normalize_runtime_candidate(item)["runtime_id"])
    matrix.sort(key=lambda item: item["id"])
    result = {
        "kind": "ucm-runtime-discovery",
        "schema_version": 3,
        "source_sha": source_sha,
        "upstream_reads": copy.deepcopy(reads),
        "runtime_candidates": records,
        "runtime_probe_matrix": {"include": matrix},
    }
    _exact_fields(result, RUNTIME_DISCOVERY_FIELDS, "runtime discovery")
    return result


def _request_bytes(url: str) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ucm-runtime-discovery",
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
        raise ValueError(
            f"runtime discovery request failed for {url}: {error}"
        ) from error


def _request_json(url: str) -> Any:
    try:
        return json.loads(_request_bytes(url))
    except json.JSONDecodeError as error:
        raise ValueError(f"runtime discovery response is invalid for {url}") from error


def _crane(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["crane", *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"crane {' '.join(arguments)} failed: {error}") from error


def _peel_git_tag(project: str, tag: str) -> tuple[str, str]:
    quoted_tag = urllib.parse.quote(tag, safe="")
    ref = _mapping(
        _request_json(
            f"https://api.github.com/repos/{project}/git/ref/tags/{quoted_tag}"
        ),
        f"{project} tag {tag}",
    )
    target = _mapping(ref.get("object"), f"{project} tag {tag} object")
    original_type = _string(target.get("type"), f"{project} tag {tag} object type")
    object_type = original_type
    sha = _string(target.get("sha"), f"{project} tag {tag} object sha")
    for _ in range(8):
        if object_type == "commit":
            return original_type, _commit(sha, f"{project} tag {tag} commit")
        if object_type != "tag":
            raise ValueError(f"{project} tag {tag} resolves to {object_type!r}")
        annotated = _mapping(
            _request_json(f"https://api.github.com/repos/{project}/git/tags/{sha}"),
            f"{project} annotated tag {tag}",
        )
        target = _mapping(
            annotated.get("object"), f"{project} annotated tag {tag} object"
        )
        object_type = _string(
            target.get("type"), f"{project} annotated tag {tag} object type"
        )
        sha = _string(target.get("sha"), f"{project} annotated tag {tag} object sha")
    raise ValueError(f"{project} tag {tag} has excessive annotation depth")


def _ascend_hardware_variants(builders: list[Any], project: str) -> dict[str, str]:
    variants: dict[str, str] = {}
    for raw in builders:
        if not isinstance(raw, dict) or raw.get("project") != project:
            continue
        if raw.get("accelerator") != "ascend":
            continue
        runtime = _string(
            raw.get("accelerator_runtime"), "Ascend Builder accelerator runtime"
        ).removeprefix("cann-")
        source_tag = _string(
            raw.get("source_image_tag"), "Ascend Builder source image tag"
        ).lower()
        match = re.match(rf"^{re.escape(runtime)}-([a-z0-9]+)(?:-|$)", source_tag)
        if match is None:
            raise ValueError("Ascend Builder source tag has no hardware token")
        hardware = match.group(1)
        variant = normalize_variant(raw.get("variant"))
        previous = variants.get(hardware)
        if previous is not None and previous != variant:
            raise ValueError("Ascend hardware token maps to multiple variants")
        variants[hardware] = variant
    if not variants:
        raise ValueError("Ascend runtime discovery has no Builder hardware mapping")
    return variants


def _literal_mooncake_version(text: str, context: str) -> str:
    values = re.findall(r"(?m)^\s*ARG\s+MOONCAKE_TAG\s*=\s*([^\s#]+)", text)
    if len(values) != 1:
        raise ValueError(f"{context}: expected one literal MOONCAKE_TAG")
    raw = values[0]
    if raw[0] in {"'", '"'}:
        quote = raw[0]
        if len(raw) < 2 or raw[-1] != quote:
            raise ValueError(f"{context}: MOONCAKE_TAG has mismatched quotes")
        value = raw[1:-1]
    elif raw[-1] in {"'", '"'}:
        raise ValueError(f"{context}: MOONCAKE_TAG has mismatched quotes")
    else:
        value = raw
    if not value or "$" in value or "`" in value:
        raise ValueError(f"{context}: MOONCAKE_TAG must be a literal version")
    if value.startswith("v"):
        value = value[1:]
    parsed = _parse_version(value, f"{context} Mooncake version")
    _compact_version(parsed, f"{context} Mooncake version")
    return str(parsed)


def _declared_runtime_variant(
    tag_suffix: str,
    known_variants: frozenset[str],
    context: str,
) -> str | None:
    if not tag_suffix:
        return None
    leading = re.match(r"^(a[0-9]+|[0-9]+p)(?:-|$)", tag_suffix)
    if leading is not None:
        return normalize_variant(leading.group(1))
    matches = sorted(
        variant
        for variant in known_variants
        if re.search(rf"(?:^|-){re.escape(variant)}(?:-|$)", tag_suffix)
    )
    if len(matches) > 1:
        raise ValueError(f"{context}: filename declares multiple runtime variants")
    return matches[0] if matches else None


def _cann_base_fact(
    text: str,
    known_hardware: frozenset[str],
    declared_variant: str | None,
    context: str,
) -> tuple[str, str] | None:
    tags = re.findall(
        r"(?mi)^\s*FROM(?:\s+--platform=\S+)?\s+" r"quay\.io/ascend/cann:([^\s@]+)",
        text,
    )
    if not tags:
        return None
    facts: set[tuple[str, str]] = set()
    for tag in tags:
        parts = tag.lower().split("-")
        boundaries = [
            index for index, part in enumerate(parts) if part in known_hardware
        ]
        if len(boundaries) > 1 or any(boundary == 0 for boundary in boundaries):
            raise ValueError(f"{context}: CANN base hardware boundary is ambiguous")
        if boundaries:
            boundary = boundaries[0]
        elif declared_variant is None:
            raise ValueError(f"{context}: CANN base hardware boundary is unknown")
        else:
            candidates: list[int] = []
            for index in range(1, len(parts)):
                if (
                    re.fullmatch(r"(?:a[0-9]+|[0-9]{3,}[a-z]?|[0-9]+p)", parts[index])
                    is None
                ):
                    continue
                runtime_prefix = "-".join(parts[:index])
                try:
                    parsed = _parse_version(runtime_prefix, f"{context} CANN version")
                    _compact_version(parsed, f"{context} CANN version")
                except ValueError:
                    continue
                candidates.append(index)
            if not candidates:
                raise ValueError(f"{context}: CANN base hardware boundary is unknown")
            boundary = max(candidates)
        runtime = "-".join(parts[:boundary])
        parsed = _parse_version(runtime, f"{context} CANN version")
        _compact_version(parsed, f"{context} CANN version")
        facts.add((runtime, parts[boundary]))
    if len(facts) != 1:
        raise ValueError(f"{context}: CANN base facts are ambiguous")
    return facts.pop()


def _runtime_dockerfiles(
    project: str,
    commit: str,
    variant_by_hardware: dict[str, str],
    excluded_variants: frozenset[str],
) -> list[dict[str, Any]]:
    tree = _mapping(
        _request_json(
            f"https://api.github.com/repos/{project}/git/trees/{commit}?recursive=1"
        ),
        f"{project} tree {commit}",
    )
    paths = sorted(
        item["path"]
        for item in _array(tree.get("tree"), f"{project} tree entries")
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and isinstance(item.get("path"), str)
        and re.fullmatch(r"Dockerfile(?:\.[^/]+)*", item["path"])
    )
    values: list[dict[str, Any]] = []
    known_hardware = frozenset(variant_by_hardware)
    known_variants = frozenset(variant_by_hardware.values())
    for path in paths:
        quoted_path = urllib.parse.quote(path, safe="/")
        text = _request_bytes(
            f"https://raw.githubusercontent.com/{project}/{commit}/{quoted_path}"
        ).decode("utf-8")
        suffix = path.removeprefix("Dockerfile").lstrip(".")
        tag_suffix = normalize_variant(suffix) if suffix else ""
        declared_variant = _declared_runtime_variant(
            tag_suffix,
            known_variants,
            f"{project}/{path}",
        )
        if declared_variant in excluded_variants:
            values.append(
                {
                    "variant": declared_variant,
                    "source_path": path,
                    "source_commit": commit,
                    "mooncake_version": None,
                    "accelerator_runtime": None,
                    "hardware_token": None,
                    "tag_suffix": tag_suffix,
                    "filtered": True,
                }
            )
            continue
        base = _cann_base_fact(
            text,
            known_hardware,
            declared_variant,
            f"{project}/{path}",
        )
        if base is None:
            continue
        runtime, hardware = base
        mapped_variant = variant_by_hardware.get(hardware)
        if mapped_variant is not None:
            if declared_variant is not None and declared_variant != mapped_variant:
                raise ValueError(
                    f"{project}/{path}: filename variant and CANN hardware disagree"
                )
            variant = mapped_variant
        else:
            if declared_variant is None:
                raise ValueError(
                    f"{project}/{path}: CANN hardware has no Builder variant"
                )
            if declared_variant in known_variants:
                raise ValueError(
                    f"{project}/{path}: known variant requires mapped CANN hardware"
                )
            variant = declared_variant
        filtered = variant in excluded_variants
        mooncake_version = (
            None if filtered else _literal_mooncake_version(text, f"{project}/{path}")
        )
        values.append(
            {
                "variant": variant,
                "source_path": path,
                "source_commit": commit,
                "mooncake_version": mooncake_version,
                "accelerator_runtime": f"cann-{runtime}",
                "hardware_token": hardware,
                "tag_suffix": tag_suffix,
                "filtered": filtered,
            }
        )
    return values


def discover_live_runtime_candidates(
    builder_discovery: object, release: object
) -> dict[str, Any]:
    """Discover current compatible runtime tags, digests, commits, and Dockerfiles."""
    builders_input = _mapping(builder_discovery, "Builder source discovery")
    release_input = _mapping(release, "release config")
    products = _array(release_input.get("upstream_products"), "upstream products")
    discovery = _mapping(release_input.get("discovery"), "release discovery")
    limits = _mapping(discovery.get("scan_limits"), "release scan limits")
    excluded_values = _array(
        discovery.get("exclude_variants"), "release excluded variants"
    )
    excluded_variants = frozenset(normalize_variant(item) for item in excluded_values)
    if len(excluded_variants) != len(excluded_values):
        raise ValueError("release excluded variants must be unique")
    selected_limit = limits.get("max_selected_upstreams")
    if not isinstance(selected_limit, int) or selected_limit < 1:
        raise ValueError("max_selected_upstreams must be positive")
    builders = _array(builders_input.get("builders"), "Builder sources")
    source_sha = _commit(
        builders_input.get("source_sha"), "Builder source discovery source_sha"
    )
    source_candidates: list[dict[str, Any]] = []
    upstream_reads: list[dict[str, Any]] = []

    for product_value in products:
        product = _mapping(product_value, "upstream product")
        product_id = _string(product.get("id"), "upstream product ID")
        runtime_product = _string(
            product.get("runtime_product"), "upstream runtime product"
        )
        repository = _string(product.get("repository"), "upstream repository")
        try:
            specifier = SpecifierSet(
                _string(product.get("version_specifier"), "version specifier")
            )
        except InvalidSpecifier as error:
            raise ValueError("upstream version specifier is invalid") from error
        projects = sorted(
            {
                str(item["project"])
                for item in builders
                if isinstance(item, dict)
                and isinstance(item.get("project"), str)
                and str(item["project"]).rsplit("/", 1)[-1] == runtime_product
            }
        )
        if len(projects) != 1:
            raise ValueError(f"runtime product {runtime_product} has ambiguous project")
        project = projects[0]
        accelerator_values = {
            str(item["accelerator"])
            for item in builders
            if isinstance(item, dict) and item.get("project") == project
        }
        if len(accelerator_values) != 1:
            raise ValueError(
                f"runtime product {runtime_product} has ambiguous accelerator"
            )
        accelerator = accelerator_values.pop()
        variant_by_hardware = (
            _ascend_hardware_variants(builders, project)
            if accelerator == "ascend"
            else {}
        )
        architectures_by_variant: dict[str, set[str]] = {}
        for item in builders:
            if not isinstance(item, dict) or item.get("project") != project:
                continue
            variant = normalize_variant(item.get("variant"))
            architecture = _string(
                item.get("cpu_architecture"), "Builder source architecture"
            )
            architectures_by_variant.setdefault(variant, set()).add(architecture)
        selected: list[tuple[Version, str]] = []
        for tag in _crane("ls", repository).splitlines():
            version_text = tag.removeprefix("v").split("-", 1)[0]
            try:
                version = Version(version_text)
            except InvalidVersion:
                continue
            channel = "rc" if version.is_prerelease else "stable"
            if channel not in product.get("channels", []):
                continue
            if specifier.contains(version, prereleases=True):
                selected.append((version, tag))
        for version, tag in sorted(selected, reverse=True)[:selected_limit]:
            reference = f"{repository}:{tag}"
            digest = _digest(_crane("digest", reference).strip(), reference)
            config = _mapping(
                json.loads(_crane("config", f"{repository}@{digest}")),
                f"{reference} config",
            )
            config_value = _mapping(config.get("config", {}), f"{reference} config")
            env = config_value.get("Env", [])
            env_map = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for item in env
                if isinstance(item, str) and "=" in item
            }
            git_tag = "v" + str(version)
            original_type, commit = _peel_git_tag(project, git_tag)
            if accelerator == "cuda":
                runtime = "cuda-" + _string(
                    env_map.get("CUDA_VERSION"), f"{reference} CUDA_VERSION"
                )
                variant_candidates = ["default"]
                dockerfiles: list[dict[str, Any]] = []
            else:
                discovered_dockerfiles = _runtime_dockerfiles(
                    project,
                    commit,
                    variant_by_hardware,
                    excluded_variants,
                )
                suffix = tag.removeprefix(git_tag).lstrip("-")
                normalized_suffix = normalize_variant(suffix) if suffix else ""
                dockerfiles = [
                    item
                    for item in discovered_dockerfiles
                    if item["tag_suffix"] == normalized_suffix
                ]
                if len(dockerfiles) != 1:
                    raise ValueError(
                        f"{reference}: runtime Dockerfile suffix is not unique"
                    )
                selected_dockerfile = dockerfiles[0]
                upstream_reads.extend(
                    {
                        "project": project,
                        "source_kind": "runtime-dockerfile-and-annotated-tag",
                        "source_path": item["source_path"],
                        "source_commit": commit,
                        "fact": "FROM" if item["filtered"] else "MOONCAKE_TAG",
                    }
                    for item in discovered_dockerfiles
                )
                if selected_dockerfile["filtered"] is True:
                    continue
                runtime = selected_dockerfile["accelerator_runtime"]
                variant_candidates = [selected_dockerfile["variant"]]

            candidate_architectures = sorted(
                architectures_by_variant.get(variant_candidates[0], set())
            )
            if not candidate_architectures:
                raise ValueError(
                    f"{reference}: runtime variant has no Builder platform"
                )

            manifest = json.loads(_crane("manifest", f"{repository}@{digest}"))
            manifests = (
                manifest.get("manifests") if isinstance(manifest, dict) else None
            )
            if isinstance(manifests, list):
                image_architectures = {
                    item.get("platform", {}).get("architecture")
                    for item in manifests
                    if isinstance(item, dict) and isinstance(item.get("platform"), dict)
                }
            else:
                image_architectures = {config.get("architecture")}
            for architecture in candidate_architectures:
                if architecture not in image_architectures:
                    continue
                source_candidates.append(
                    {
                        "product_id": product_id,
                        "runtime_version": str(version),
                        "channel": ("rc" if version.is_prerelease else "stable"),
                        "accelerator": accelerator,
                        "accelerator_runtime": runtime,
                        "cpu_architecture": architecture,
                        "variant_candidates": variant_candidates,
                        "runtime_image_repository": repository,
                        "runtime_image_tag": tag,
                        "runtime_image_digest": digest,
                        "git_ref": {
                            "tag": git_tag,
                            "object_type": original_type,
                            "target_commit": commit,
                        },
                        "runtime_dockerfiles": copy.deepcopy(dockerfiles),
                    }
                )
            if accelerator == "cuda":
                upstream_reads.append(
                    {
                        "project": project,
                        "source_kind": "runtime-image-and-git-tag",
                        "source_path": repository,
                        "source_commit": commit,
                        "fact": "runtime-image",
                    }
                )
    runtime_sources = {
        "source_sha": source_sha,
        "upstream_reads": _unique_records(upstream_reads),
        "candidates": source_candidates,
    }
    return discover_runtime_candidates(builders_input, runtime_sources)


def _validate_builder_sync(value: object) -> dict[str, Any]:
    sync = _mapping(value, "builder_sync")
    _exact_fields(sync, BUILDER_SYNC_FIELDS, "builder_sync")
    if sync["mode"] != "append-only":
        raise ValueError("builder_sync mode must be append-only")
    if sync["target_digests_verified"] is not True:
        raise ValueError("builder_sync target digests must be verified")
    if sync["deletions"] != []:
        raise ValueError("builder_sync must not delete Builder revisions")
    return sync


def _validate_builder_fact(value: object, label: str) -> dict[str, Any]:
    fact = _mapping(value, label)
    _exact_fields(fact, BUILDER_FACT_FIELDS, label)
    fact_id = _digest(fact["builder_fact_id"], f"{label} ID")
    for field in (
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
        "recipe_path",
        "target_repository",
        "target_tag",
    ):
        _string(fact[field], f"{label} {field}")
    compact_accelerator_runtime(fact["accelerator_runtime"])
    if normalize_variant(fact["variant"]) != fact["variant"]:
        raise ValueError(f"{label} variant is not canonical")
    if fact["accelerator"] not in {"cuda", "ascend"}:
        raise ValueError(f"{label} accelerator is unsupported")
    if fact["cpu_architecture"] not in {"amd64", "arm64"}:
        raise ValueError(f"{label} architecture is unsupported")
    if _MANYLINUX.fullmatch(fact["manylinux"]) is None:
        raise ValueError(f"{label} manylinux is invalid")
    _digest(fact["source_image_digest"], f"{label} source digest")
    _commit(fact["recipe_source_commit"], f"{label} recipe commit")
    _digest(fact["recipe_sha256"], f"{label} recipe digest")
    _digest(fact["toolchain_sha256"], f"{label} toolchain digest")
    _digest(fact["target_builder_digest"], f"{label} target digest")
    if fact_id != _canonical_digest(_identity(fact, BUILDER_FACT_IDENTITY_FIELDS)):
        raise ValueError(f"{label} ID is not canonical")
    if fact["accelerator"] == "cuda":
        if any(
            fact[field] is not None
            for field in (
                "mooncake_source_runtime_id",
                "mooncake_source_runtime_image",
                "mooncake_version",
            )
        ):
            raise ValueError(f"{label} CUDA Mooncake provenance must be null")
    else:
        _digest(fact["mooncake_source_runtime_id"], f"{label} Mooncake runtime ID")
        _string(
            fact["mooncake_source_runtime_image"],
            f"{label} Mooncake runtime image",
        )
        compact_mooncake_version(fact["mooncake_version"])
    return fact


def assemble_capability_catalog(
    *,
    builder_discovery: object,
    runtime_discovery: object,
    python_probes: object,
    mooncake_probes: object,
    python_requires: object,
) -> dict[str, Any]:
    """Assemble one deterministic Catalog from parser- and probe-owned facts."""
    builders_input = _mapping(builder_discovery, "builder_discovery")
    runtimes_input = _mapping(runtime_discovery, "runtime_discovery")
    python_input = _mapping(python_probes, "python_probes")
    mooncake_input = _mapping(mooncake_probes, "mooncake_probes")
    source_sha = _commit(builders_input.get("source_sha"), "Catalog source_sha")
    runtime_source_sha = runtimes_input.get("source_sha")
    if runtime_source_sha is not None and runtime_source_sha != source_sha:
        raise ValueError("Builder and runtime discovery source_sha differ")
    if not isinstance(python_requires, str):
        raise ValueError("python_requires must be a PEP 440 specifier string")
    try:
        requires = SpecifierSet(python_requires)
    except InvalidSpecifier as error:
        raise ValueError("python_requires must be a valid PEP 440 specifier") from error

    exclusions: list[dict[str, Any]] = []
    raw_runtime_by_id: dict[str, dict[str, Any]] = {}
    runtime_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_value in enumerate(
        _array(runtimes_input.get("runtime_candidates"), "runtime candidates")
    ):
        raw = _mapping(raw_value, f"runtime candidates[{index}]")
        record = normalize_runtime_candidate(raw)
        previous = runtime_by_id.get(record["runtime_id"])
        if previous is not None and previous != record:
            raise ValueError(f"conflicting runtime identity {record['runtime_id']}")
        runtime_by_id[record["runtime_id"]] = record
        raw_runtime_by_id[record["runtime_id"]] = raw
    runtime_candidates = sorted(
        runtime_by_id.values(), key=lambda item: item["runtime_id"]
    )

    facts_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_value in enumerate(
        _array(builders_input.get("builder_facts"), "Builder facts")
    ):
        fact = _validate_builder_fact(raw_value, f"Builder facts[{index}]")
        fact_id = fact["builder_fact_id"]
        previous = facts_by_id.get(fact_id)
        if previous is not None and previous != fact:
            raise ValueError(f"conflicting Builder fact {fact_id}")
        facts_by_id[fact_id] = fact

    probes_by_fact: dict[str, list[dict[str, Any]]] = {}
    probes_by_coordinate: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw_value in enumerate(
        _array(python_input.get("probes"), "Python probes")
    ):
        probe = _mapping(raw_value, f"Python probes[{index}]")
        _exact_fields(probe, PYTHON_PROBE_FIELDS, f"Python probes[{index}]")
        fact_id = _digest(probe.get("builder_fact_id"), "Python probe Builder ID")
        fact = facts_by_id.get(fact_id)
        if fact is None:
            raise ValueError("Python probe references an unknown Builder fact")
        digest = _digest(
            probe.get("target_builder_digest"), "Python probe target digest"
        )
        if digest != fact["target_builder_digest"]:
            raise ValueError("Python probe target digest differs from Builder fact")
        expected_image = f"{fact['target_repository']}@{digest}"
        if probe.get("builder_image") != expected_image:
            raise ValueError("Python probe target image differs from Builder fact")
        architecture = _string(
            probe.get("cpu_architecture"), "Python probe cpu_architecture"
        )
        if architecture != fact["cpu_architecture"]:
            raise ValueError("Python probe architecture differs from Builder fact")
        manylinux = _string(probe.get("manylinux"), "Python probe manylinux")
        if manylinux != fact["manylinux"]:
            raise ValueError("Python probe manylinux differs from Builder fact")
        if _MANYLINUX.fullmatch(manylinux) is None:
            raise ValueError("Python probe manylinux is invalid")
        _string(probe.get("runner"), "Python probe runner")
        abi = _string(probe.get("python_abi"), "Python probe python_abi")
        version = _string(probe.get("python_version"), "Python probe python_version")
        if python_version_from_abi(abi) != version:
            raise ValueError("Python probe version and ABI differ")
        python_coordinate = compile_python_coordinate(
            {
                "python_version": version,
                "python_abi": abi,
                "cpu_architecture": architecture,
                "manylinux": manylinux,
            }
        )
        interpreter = _string(
            probe.get("interpreter_path"), "Python probe interpreter_path"
        )
        if interpreter != python_coordinate["interpreter_path"]:
            raise ValueError("Python probe path coordinate differs from its ABI")
        soabi = _string(probe.get("soabi"), "Python probe SOABI")
        wheel_tag = _string(probe.get("wheel_tag"), "Python probe wheel_tag")
        if soabi != python_coordinate["expected_soabi"]:
            raise ValueError("Python probe SOABI differs from its ABI")
        if wheel_tag != python_coordinate["expected_wheel_tag"]:
            raise ValueError("Python probe wheel tag is not canonical")
        coordinate = (fact_id, abi)
        if coordinate in probes_by_coordinate:
            raise ValueError("duplicate Python probe for one Builder ABI")
        probes_by_coordinate[coordinate] = probe
        probes_by_fact.setdefault(fact_id, []).append(probe)

    failed_python_facts: set[str] = set()
    for index, raw_value in enumerate(
        _array(python_input.get("failures", []), "Python probe failures")
    ):
        failure = _mapping(raw_value, f"Python probe failures[{index}]")
        _exact_fields(
            failure,
            PYTHON_PROBE_FAILURE_FIELDS,
            f"Python probe failures[{index}]",
        )
        if failure["status"] != "failed" or failure["reason_code"] != (
            "python-probe-failed"
        ):
            raise ValueError("Python probe failure status/reason is invalid")
        if any(
            failure[field] is not None
            for field in (
                "builder_capability_id",
                "builder_revision_id",
                "runtime_id",
            )
        ):
            raise ValueError("Python probe failure cannot claim Catalog references")
        fact_id = _digest(
            failure["builder_fact_id"], "Python probe failure Builder fact ID"
        )
        fact = facts_by_id.get(fact_id)
        if fact is None:
            raise ValueError("Python probe failure references an unknown Builder fact")
        digest = _digest(
            failure["target_builder_digest"],
            "Python probe failure target digest",
        )
        if digest != fact["target_builder_digest"]:
            raise ValueError("Python probe failure target digest differs")
        expected_image = f"{fact['target_repository']}@{digest}"
        if failure["builder_image"] != expected_image:
            raise ValueError("Python probe failure target image differs")
        architecture = _string(
            failure["cpu_architecture"], "Python probe failure architecture"
        )
        if architecture != fact["cpu_architecture"]:
            raise ValueError("Python probe failure architecture differs")
        manylinux = _string(failure["manylinux"], "Python probe failure manylinux")
        if manylinux != fact["manylinux"]:
            raise ValueError("Python probe failure manylinux differs")
        failed_python_facts.add(fact_id)
        exclusions.append(
            _exclusion(
                reason_code="python-probe-failed",
                source_kind=_string(
                    failure["source_kind"], "Python probe failure source_kind"
                ),
                source_id=_string(
                    failure["source_id"], "Python probe failure source_id"
                ),
                evidence={
                    "builder_fact_id": fact_id,
                    "builder_image": _string(
                        failure["builder_image"],
                        "Python probe failure builder image",
                    ),
                    "target_builder_digest": digest,
                    "cpu_architecture": _string(
                        failure["cpu_architecture"],
                        "Python probe failure architecture",
                    ),
                    "manylinux": manylinux,
                    "runner": _string(failure["runner"], "Python probe failure runner"),
                    "interpreter_path": _string(
                        failure["interpreter_path"],
                        "Python probe failure interpreter",
                    ),
                    "failure": copy.deepcopy(
                        _mapping(
                            failure["evidence"],
                            "Python probe failure evidence",
                        )
                    ),
                },
            )
        )

    mooncake_by_runtime: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw_value in enumerate(
        _array(mooncake_input.get("probes"), "Mooncake probes")
    ):
        probe = _mapping(raw_value, f"Mooncake probes[{index}]")
        _exact_fields(probe, MOONCAKE_PROBE_FIELDS, f"Mooncake probes[{index}]")
        key = (
            _digest(probe.get("runtime_image_digest"), "Mooncake runtime digest"),
            _string(probe.get("cpu_architecture"), "Mooncake probe cpu_architecture"),
        )
        if key in mooncake_by_runtime and mooncake_by_runtime[key] != probe:
            raise ValueError("conflicting Mooncake probe for one runtime image")
        mooncake_by_runtime[key] = probe

    for index, raw_value in enumerate(
        _array(mooncake_input.get("failures", []), "Mooncake probe failures")
    ):
        failure = _mapping(raw_value, f"Mooncake probe failures[{index}]")
        _exact_fields(
            failure,
            MOONCAKE_PROBE_FAILURE_FIELDS,
            f"Mooncake probe failures[{index}]",
        )
        if failure["status"] != "failed" or failure["reason_code"] != (
            "mooncake-probe-failed"
        ):
            raise ValueError("Mooncake probe failure status/reason is invalid")
        if any(
            failure[field] is not None
            for field in ("builder_capability_id", "builder_revision_id")
        ):
            raise ValueError("Mooncake probe failure cannot claim Builders")
        runtime_id = _digest(failure["runtime_id"], "Mooncake probe failure runtime ID")
        runtime = runtime_by_id.get(runtime_id)
        if runtime is None:
            raise ValueError("Mooncake probe failure references an unknown runtime")
        digest = _digest(
            failure["runtime_image_digest"],
            "Mooncake probe failure runtime digest",
        )
        if failure["runtime_image"] != runtime["runtime_image"] or not runtime[
            "runtime_image"
        ].endswith("@" + digest):
            raise ValueError("Mooncake probe failure runtime image differs")
        exclusions.append(
            _exclusion(
                reason_code="mooncake-probe-failed",
                source_kind=_string(
                    failure["source_kind"], "Mooncake probe failure source_kind"
                ),
                source_id=_string(
                    failure["source_id"], "Mooncake probe failure source_id"
                ),
                runtime_id=runtime_id,
                evidence={
                    "runtime_image": runtime["runtime_image"],
                    "runtime_image_digest": digest,
                    "cpu_architecture": _string(
                        failure["cpu_architecture"],
                        "Mooncake probe failure architecture",
                    ),
                    "runner": _string(
                        failure["runner"], "Mooncake probe failure runner"
                    ),
                    "failure": copy.deepcopy(
                        _mapping(
                            failure["evidence"],
                            "Mooncake probe failure evidence",
                        )
                    ),
                },
            )
        )

    capability_by_id: dict[str, dict[str, Any]] = {}
    revision_by_id: dict[str, dict[str, Any]] = {}
    fact_by_revision_id: dict[str, dict[str, Any]] = {}
    for index, raw_value in enumerate(
        _array(builders_input.get("builders", []), "discovered builders")
    ):
        builder = _mapping(raw_value, f"discovered builders[{index}]")
        variant = normalize_variant(builder.get("variant"))
        if variant == "310p":
            exclusions.append(
                _exclusion(
                    reason_code="variant-filtered-310p",
                    source_kind=_string(
                        builder.get("source_kind"), "Builder source_kind"
                    ),
                    source_id=_string(
                        builder.get("source_path"), "Builder source_path"
                    ),
                    evidence={"variant": "310p"},
                )
            )
    for raw_failure in _array(builders_input.get("failures", []), "Builder failures"):
        failure = _mapping(raw_failure, "Builder failure")
        _exact_fields(failure, BUILDER_FAILURE_FIELDS, "Builder failure")
        if failure["status"] != "failed":
            raise ValueError("Builder failure status is invalid")
        if failure["target_builder_digest"] is not None:
            raise ValueError("Failed Builder must not claim a target digest")
        if failure["digest_readback"] is not False:
            raise ValueError("Failed Builder must record failed digest readback")
        reason_code = _string(failure["reason_code"], "Builder failure reason")
        if reason_code == "builder-sync-failed":
            if any(
                failure[field] is not None
                for field in (
                    "builder_capability_id",
                    "builder_revision_id",
                    "runtime_id",
                )
            ):
                raise ValueError("Builder sync failure cannot claim Catalog references")
            evidence = {
                "builder_plan_id": _digest(
                    failure["builder_plan_id"], "Builder failure plan ID"
                ),
                "status": "failed",
                "target_repository": _string(
                    failure["target_repository"],
                    "Builder failure target repository",
                ),
                "target_tag": _string(
                    failure["target_tag"], "Builder failure target tag"
                ),
                "target_builder_digest": None,
                "digest_readback": False,
                "failure": copy.deepcopy(
                    _mapping(failure["evidence"], "Builder failure evidence")
                ),
            }
            runtime_id = None
        elif reason_code == "mooncake-version-mismatch":
            if failure["builder_plan_id"] is not None or any(
                failure[field] is not None
                for field in ("builder_capability_id", "builder_revision_id")
            ):
                raise ValueError("Mooncake mismatch cannot claim Builder references")
            runtime_id = _digest(failure["runtime_id"], "Mooncake mismatch runtime ID")
            if runtime_id not in runtime_by_id:
                raise ValueError("Mooncake mismatch references an unknown runtime")
            if any(
                failure[field] is not None
                for field in ("target_repository", "target_tag")
            ):
                raise ValueError("Mooncake mismatch cannot claim a Builder target")
            evidence = copy.deepcopy(
                _mapping(failure["evidence"], "Mooncake mismatch evidence")
            )
        else:
            raise ValueError(f"unsupported Builder failure {reason_code!r}")
        exclusions.append(
            _exclusion(
                reason_code=reason_code,
                source_kind=_string(
                    failure["source_kind"], "Builder failure source_kind"
                ),
                source_id=_string(failure["source_id"], "Builder failure source_id"),
                runtime_id=runtime_id,
                evidence=evidence,
            )
        )

    for fact_id, fact in facts_by_id.items():
        builder = fact
        variant = fact["variant"]
        architecture = fact["cpu_architecture"]
        source_digest = fact["source_image_digest"]
        probes = probes_by_fact.get(fact_id)
        if not probes:
            if fact_id in failed_python_facts:
                continue
            raise ValueError("Builder has no matching native Python probe")
        accelerator = fact["accelerator"]
        accelerator_runtime = fact["accelerator_runtime"]
        if accelerator == "ascend":
            runtime = runtime_by_id.get(fact["mooncake_source_runtime_id"])
            if runtime is None:
                raise ValueError("Ascend Builder fact references an unknown runtime")
            if fact["mooncake_source_runtime_image"] != runtime["runtime_image"]:
                raise ValueError("Ascend Builder fact runtime image differs")
            if fact["mooncake_version"] != runtime["mooncake_version"]:
                raise ValueError("Ascend Builder fact Mooncake version differs")
            runtime_digest = runtime["runtime_image"].rsplit("@", 1)[1]
            probe = mooncake_by_runtime.get((runtime_digest, architecture))
            if probe is None or not (
                probe["declared_version"]
                == probe["installed_version"]
                == fact["mooncake_version"]
            ):
                raise ValueError("Ascend Builder fact lacks verified copy provenance")
        for probe in probes:
            python_version = _string(
                probe.get("python_version"), "Python probe python_version"
            )
            python_abi = _string(probe.get("python_abi"), "Python probe python_abi")
            if not requires.contains(Version(python_version), prereleases=True):
                exclusions.append(
                    _exclusion(
                        reason_code="python-requires-mismatch",
                        source_kind="python-probe",
                        source_id=f"{fact_id}:{probe['interpreter_path']}",
                        evidence={
                            "python_abi": python_abi,
                            "python_requires": python_requires,
                        },
                    )
                )
                continue
            for mooncake_version in {fact["mooncake_version"]}:
                capability = {
                    "builder_capability_id": "",
                    "accelerator": accelerator,
                    "accelerator_runtime": accelerator_runtime,
                    "variant": variant,
                    "cpu_architecture": architecture,
                    "manylinux": _string(builder.get("manylinux"), "Builder manylinux"),
                    "python_version": python_version,
                    "python_abi": python_abi,
                    "mooncake_version": mooncake_version,
                    "builder_revision_ids": [],
                }
                capability["builder_capability_id"] = _canonical_digest(
                    _identity(capability, _CAPABILITY_IDENTITY_FIELDS)
                )
                revision = {
                    "builder_revision_id": "",
                    "builder_capability_id": capability["builder_capability_id"],
                    "source_image_repository": _string(
                        builder.get("source_image_repository"),
                        "Builder source image repository",
                    ),
                    "source_image_digest": source_digest,
                    "recipe_path": _string(
                        builder.get("recipe_path"), "Builder recipe_path"
                    ),
                    "recipe_source_commit": _commit(
                        builder.get("recipe_source_commit"),
                        "Builder recipe source commit",
                    ),
                    "recipe_sha256": _digest(
                        builder.get("recipe_sha256"), "Builder recipe digest"
                    ),
                    "toolchain_sha256": _digest(
                        builder.get("toolchain_sha256"), "Builder toolchain digest"
                    ),
                    "target_repository": _string(
                        builder.get("target_repository"), "Builder target repository"
                    ),
                    "target_tag": _string(
                        builder.get("target_tag"), "Builder target tag"
                    ),
                    "target_builder_digest": _digest(
                        builder.get("target_builder_digest"),
                        "Builder target digest",
                    ),
                    "revision_sha256": "",
                }
                revision["builder_revision_id"] = _canonical_digest(
                    _identity(revision, _REVISION_IDENTITY_FIELDS)
                )
                revision_projection = copy.deepcopy(revision)
                revision_projection.pop("revision_sha256")
                revision["revision_sha256"] = _canonical_digest(revision_projection)
                previous_revision = revision_by_id.get(revision["builder_revision_id"])
                if previous_revision is not None and previous_revision != revision:
                    raise ValueError(
                        "conflicting Builder revision "
                        f"{revision['builder_revision_id']}"
                    )
                revision_by_id[revision["builder_revision_id"]] = revision
                fact_by_revision_id[revision["builder_revision_id"]] = fact
                existing = capability_by_id.get(capability["builder_capability_id"])
                if existing is None:
                    existing = capability
                    capability_by_id[capability["builder_capability_id"]] = existing
                revision_ids = existing["builder_revision_ids"]
                if revision["builder_revision_id"] not in revision_ids:
                    revision_ids.append(revision["builder_revision_id"])

    if facts_by_id and not capability_by_id and failed_python_facts == set(facts_by_id):
        raise ValueError("all Builder facts failed native Python probing")

    for capability in capability_by_id.values():
        capability["builder_revision_ids"].sort()
    builder_capabilities = sorted(
        capability_by_id.values(), key=lambda item: item["builder_capability_id"]
    )
    builder_revisions = sorted(
        revision_by_id.values(), key=lambda item: item["builder_revision_id"]
    )

    bindings: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for capability in builder_capabilities:
        for runtime in runtime_candidates:
            if not _runtime_compatible(capability, runtime):
                continue
            raw_runtime = raw_runtime_by_id[runtime["runtime_id"]]
            mooncake_probe = None
            if capability["accelerator"] == "ascend":
                runtime_digest = runtime["runtime_image"].rsplit("@", 1)[1]
                mooncake_probe = mooncake_by_runtime.get(
                    (runtime_digest, runtime["cpu_architecture"])
                )
                if mooncake_probe is None:
                    raise ValueError("Ascend runtime has no matching Mooncake probe")
                declared = raw_runtime.get("mooncake_version")
                if mooncake_probe.get("declared_version") != declared:
                    raise ValueError(
                        "Mooncake probe declaration differs from runtime source"
                    )
            for revision_id in capability["builder_revision_ids"]:
                revision = revision_by_id[revision_id]
                fact = fact_by_revision_id[revision_id]
                if (
                    capability["accelerator"] == "ascend"
                    and runtime["runtime_id"] != fact["mooncake_source_runtime_id"]
                ):
                    continue
                if mooncake_probe is not None and (
                    mooncake_probe.get("installed_version")
                    != mooncake_probe.get("declared_version")
                ):
                    raise ValueError(
                        "Builder fact names an unverified Mooncake runtime"
                    )
                binding = {
                    "builder_capability_id": capability["builder_capability_id"],
                    "builder_revision_id": revision_id,
                    "runtime_id": runtime["runtime_id"],
                    **{
                        field: capability[field]
                        for field in _CAPABILITY_PROJECTION_FIELDS
                    },
                    "source_image": (
                        f"{revision['source_image_repository']}@"
                        f"{revision['source_image_digest']}"
                    ),
                    "target_image": (
                        f"{revision['target_repository']}@"
                        f"{revision['target_builder_digest']}"
                    ),
                    **{field: revision[field] for field in _REVISION_PROJECTION_FIELDS},
                    "mooncake_copy_mode": (
                        "none"
                        if capability["accelerator"] == "cuda"
                        else "runtime-copy"
                    ),
                    "runtime_image": runtime["runtime_image"],
                }
                bindings.append(binding)
                entries.append({field: binding[field] for field in ENTRY_FIELDS})

    bindings.sort(key=lambda item: (item["builder_revision_id"], item["runtime_id"]))
    entries.sort(key=_entry_key)
    exclusions = _unique_records(exclusions)
    exclusions.sort(key=_exclusion_key)
    upstream_reads = _unique_records(
        [
            *_array(builders_input.get("upstream_reads", []), "Builder upstream reads"),
            *_array(runtimes_input.get("upstream_reads", []), "Runtime upstream reads"),
        ]
    )
    catalog = {
        "kind": "ucm-capability-catalog",
        "schema_version": 3,
        "source_sha": source_sha,
        "upstream_reads": upstream_reads,
        "builder_sync": copy.deepcopy(
            _validate_builder_sync(builders_input.get("builder_sync"))
        ),
        "builder_capabilities": builder_capabilities,
        "builder_revisions": builder_revisions,
        "runtime_candidates": runtime_candidates,
        "bindings": bindings,
        "entries": entries,
        "exclusions": exclusions,
        "catalog_sha256": "",
    }
    catalog["catalog_sha256"] = _canonical_digest(
        {key: value for key, value in catalog.items() if key != "catalog_sha256"}
    )
    return validate_capability_catalog(catalog)


def _validate_capability_semantics(value: dict[str, Any], label: str) -> None:
    if value["accelerator"] not in {"cuda", "ascend"}:
        raise ValueError(f"{label}: unsupported accelerator")
    compact_accelerator_runtime(value["accelerator_runtime"])
    if normalize_variant(value["variant"]) != value["variant"]:
        raise ValueError(f"{label}: variant is not canonical")
    if value["cpu_architecture"] not in {"amd64", "arm64"}:
        raise ValueError(f"{label}: unsupported cpu_architecture")
    if (
        not isinstance(value["manylinux"], str)
        or _MANYLINUX.fullmatch(value["manylinux"]) is None
    ):
        raise ValueError(f"{label}: malformed manylinux")
    if python_version_from_abi(value["python_abi"]) != value["python_version"]:
        raise ValueError(f"{label}: Python version and ABI differ")
    if value["accelerator"] == "cuda":
        if value["mooncake_version"] is not None:
            raise ValueError(f"{label}: CUDA Mooncake version must be null")
    else:
        compact_mooncake_version(value["mooncake_version"])


def validate_selected_capability_evidence(value: object) -> dict[str, Any]:
    """Validate a closed selected Catalog subgraph through Catalog semantics."""
    evidence = _mapping(value, "selected capability evidence")
    expected_fields = {
        "builder_capabilities",
        "builder_revisions",
        "runtime_candidates",
        "bindings",
    }
    if set(evidence) != expected_fields:
        raise ValueError("selected capability evidence fields must be exact")
    for field in expected_fields:
        _array(evidence[field], f"selected capability evidence {field}")
    revision_ids_by_capability: dict[str, list[str]] = {}
    for raw in evidence["builder_revisions"]:
        revision = _mapping(raw, "selected Builder revision")
        capability_id = _string(
            revision.get("builder_capability_id"),
            "selected Builder revision capability ID",
        )
        revision_id = _string(
            revision.get("builder_revision_id"), "selected Builder revision ID"
        )
        revision_ids_by_capability.setdefault(capability_id, []).append(revision_id)
    capabilities = []
    for raw in evidence["builder_capabilities"]:
        capability = _mapping(raw, "selected Builder capability")
        capability_id = _string(
            capability.get("builder_capability_id"),
            "selected Builder capability ID",
        )
        selected_revision_ids = sorted(
            revision_ids_by_capability.get(capability_id, [])
        )
        if (
            not selected_revision_ids
            or capability.get("builder_revision_ids") != selected_revision_ids
        ):
            raise ValueError("selected capability revision evidence is incomplete")
        capabilities.append(copy.deepcopy(capability))
    entries = [
        {field: binding[field] for field in ENTRY_FIELDS}
        for binding in evidence["bindings"]
    ]
    entries.sort(key=_entry_key)
    catalog = {
        "kind": "ucm-capability-catalog",
        "schema_version": 3,
        "source_sha": "0" * 40,
        "upstream_reads": [],
        "builder_sync": {
            "mode": "append-only",
            "target_digests_verified": True,
            "deletions": [],
        },
        "builder_capabilities": capabilities,
        "builder_revisions": copy.deepcopy(evidence["builder_revisions"]),
        "runtime_candidates": copy.deepcopy(evidence["runtime_candidates"]),
        "bindings": copy.deepcopy(evidence["bindings"]),
        "entries": entries,
        "exclusions": [],
        "catalog_sha256": "",
    }
    catalog["catalog_sha256"] = _canonical_digest(
        {key: item for key, item in catalog.items() if key != "catalog_sha256"}
    )
    validate_capability_catalog(catalog)
    return copy.deepcopy(evidence)


def validate_capability_catalog(value: object) -> dict[str, Any]:
    """Validate and return one closed canonical Capability Catalog."""
    catalog = _mapping(value, "Capability Catalog")
    _exact_fields(catalog, CATALOG_FIELDS, "Capability Catalog")
    if catalog["kind"] != "ucm-capability-catalog":
        raise ValueError("Capability Catalog kind is invalid")
    if catalog["schema_version"] != 3:
        raise ValueError("Capability Catalog schema_version must be 3")
    _commit(catalog["source_sha"], "Capability Catalog source_sha")
    _array(catalog["upstream_reads"], "Capability Catalog upstream_reads")
    _validate_builder_sync(catalog["builder_sync"])
    catalog_digest = _digest(catalog["catalog_sha256"], "Catalog digest")
    projection = {key: item for key, item in catalog.items() if key != "catalog_sha256"}
    if catalog_digest != _canonical_digest(projection):
        raise ValueError("Capability Catalog digest does not match its contents")

    capability_values = _array(
        catalog["builder_capabilities"], "Catalog builder_capabilities"
    )
    revision_values = _array(catalog["builder_revisions"], "Catalog builder_revisions")
    runtime_values = _array(catalog["runtime_candidates"], "Catalog runtime_candidates")
    binding_values = _array(catalog["bindings"], "Catalog bindings")
    entry_values = _array(catalog["entries"], "Catalog entries")
    exclusion_values = _array(catalog["exclusions"], "Catalog exclusions")

    capabilities: list[dict[str, Any]] = []
    capabilities_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(capability_values):
        capability = _mapping(raw, f"builder_capabilities[{index}]")
        _exact_fields(capability, CAPABILITY_FIELDS, f"builder_capabilities[{index}]")
        capability_id = _digest(
            capability["builder_capability_id"],
            f"builder_capabilities[{index}] ID",
        )
        _validate_capability_semantics(capability, f"builder_capabilities[{index}]")
        if capability_id != _canonical_digest(
            _identity(capability, _CAPABILITY_IDENTITY_FIELDS)
        ):
            raise ValueError("Builder capability ID is not canonical")
        revision_ids = _array(
            capability["builder_revision_ids"],
            f"builder_capabilities[{index}] revision IDs",
        )
        if not revision_ids or revision_ids != sorted(revision_ids):
            raise ValueError("Builder revision IDs are empty or noncanonical")
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("Builder revision IDs contain duplicates")
        for revision_id in revision_ids:
            _digest(revision_id, "Builder revision reference")
        if capability_id in capabilities_by_id:
            raise ValueError(f"duplicate Builder capability ID {capability_id}")
        capabilities_by_id[capability_id] = capability
        capabilities.append(capability)
    if capabilities != sorted(
        capabilities, key=lambda item: item["builder_capability_id"]
    ):
        raise ValueError("Builder capabilities are not canonically ordered")

    revisions: list[dict[str, Any]] = []
    revisions_by_id: dict[str, dict[str, Any]] = {}
    revisions_by_capability: dict[str, list[str]] = {}
    for index, raw in enumerate(revision_values):
        revision = _mapping(raw, f"builder_revisions[{index}]")
        _exact_fields(revision, REVISION_FIELDS, f"builder_revisions[{index}]")
        revision_id = _digest(
            revision["builder_revision_id"], f"builder_revisions[{index}] ID"
        )
        capability_id = _digest(
            revision["builder_capability_id"],
            f"builder_revisions[{index}] capability ID",
        )
        if capability_id not in capabilities_by_id:
            raise ValueError("Builder revision references an unknown capability")
        _string(revision["source_image_repository"], "Builder source image repository")
        _digest(revision["source_image_digest"], "Builder source image digest")
        _string(revision["recipe_path"], "Builder recipe path")
        _commit(revision["recipe_source_commit"], "Builder recipe source commit")
        _digest(revision["recipe_sha256"], "Builder recipe digest")
        _digest(revision["toolchain_sha256"], "Builder toolchain digest")
        _string(revision["target_repository"], "Builder target repository")
        _string(revision["target_tag"], "Builder target tag")
        _digest(revision["target_builder_digest"], "Builder target digest")
        if revision_id != _canonical_digest(
            _identity(revision, _REVISION_IDENTITY_FIELDS)
        ):
            raise ValueError("Builder revision ID is not canonical")
        revision_projection = copy.deepcopy(revision)
        revision_sha256 = _digest(
            revision_projection.pop("revision_sha256"), "Builder revision digest"
        )
        if revision_sha256 != _canonical_digest(revision_projection):
            raise ValueError("Builder revision digest does not match its contents")
        if revision_id in revisions_by_id:
            raise ValueError(f"duplicate Builder revision ID {revision_id}")
        revisions_by_id[revision_id] = revision
        revisions_by_capability.setdefault(capability_id, []).append(revision_id)
        revisions.append(revision)
    if revisions != sorted(revisions, key=lambda item: item["builder_revision_id"]):
        raise ValueError("Builder revisions are not canonically ordered")
    for capability_id, capability in capabilities_by_id.items():
        if capability["builder_revision_ids"] != sorted(
            revisions_by_capability.get(capability_id, [])
        ):
            raise ValueError("Capability revision references are incomplete")

    runtimes: list[dict[str, Any]] = []
    runtimes_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(runtime_values):
        runtime = _mapping(raw, f"runtime_candidates[{index}]")
        _exact_fields(runtime, RUNTIME_FIELDS, f"runtime_candidates[{index}]")
        runtime_id = _digest(runtime["runtime_id"], f"runtime_candidates[{index}] ID")
        _string(runtime["product_id"], "runtime product_id")
        repository = _string(runtime["runtime_repository"], "runtime repository")
        _string(runtime["runtime_tag"], "runtime tag")
        _string(runtime["runtime_version"], "runtime version")
        _string(runtime["channel"], "runtime channel")
        _string(runtime["git_tag"], "runtime Git tag")
        _commit(runtime["git_commit"], "runtime Git commit")
        _validate_capability_semantics(
            {
                **runtime,
                "manylinux": "manylinux_2_28",
                "python_version": "3.10",
                "python_abi": "cp310",
            },
            f"runtime_candidates[{index}]",
        )
        expected_prefix = f"{repository}@"
        if not isinstance(runtime["runtime_image"], str) or not runtime[
            "runtime_image"
        ].startswith(expected_prefix):
            raise ValueError("Runtime image repository does not match")
        _digest(
            runtime["runtime_image"][len(expected_prefix) :], "runtime image digest"
        )
        if runtime_id != _canonical_digest(
            _identity(runtime, _RUNTIME_IDENTITY_FIELDS)
        ):
            raise ValueError("Runtime ID is not canonical")
        if runtime_id in runtimes_by_id:
            raise ValueError(f"duplicate runtime ID {runtime_id}")
        runtimes_by_id[runtime_id] = runtime
        runtimes.append(runtime)
    if runtimes != sorted(runtimes, key=lambda item: item["runtime_id"]):
        raise ValueError("Runtime candidates are not canonically ordered")

    bindings: list[dict[str, Any]] = []
    bindings_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(binding_values):
        binding = _mapping(raw, f"bindings[{index}]")
        _exact_fields(binding, BINDING_FIELDS, f"bindings[{index}]")
        revision_id = _digest(
            binding["builder_revision_id"], f"bindings[{index}] revision ID"
        )
        runtime_id = _digest(binding["runtime_id"], f"bindings[{index}] runtime ID")
        capability_id = _digest(
            binding["builder_capability_id"], f"bindings[{index}] capability ID"
        )
        if revision_id not in revisions_by_id:
            raise ValueError("Binding references an unknown Builder revision")
        if runtime_id not in runtimes_by_id:
            raise ValueError("Binding references an unknown runtime")
        if capability_id not in capabilities_by_id:
            raise ValueError("Binding references an unknown capability")
        revision = revisions_by_id[revision_id]
        capability = capabilities_by_id[capability_id]
        runtime = runtimes_by_id[runtime_id]
        if revision["builder_capability_id"] != capability_id:
            raise ValueError("Binding revision and capability differ")
        for field in _CAPABILITY_PROJECTION_FIELDS:
            if binding[field] != capability[field]:
                raise ValueError(f"Binding capability projection differs at {field}")
        for field in _REVISION_PROJECTION_FIELDS:
            if binding[field] != revision[field]:
                raise ValueError(f"Binding revision projection differs at {field}")
        if not _runtime_compatible(capability, runtime):
            raise ValueError("Binding capability is incompatible with runtime")
        if binding["source_image"] != (
            f"{revision['source_image_repository']}@{revision['source_image_digest']}"
        ):
            raise ValueError("Binding source image differs from revision")
        if binding["target_image"] != (
            f"{revision['target_repository']}@{revision['target_builder_digest']}"
        ):
            raise ValueError("Binding target image differs from revision")
        if binding["runtime_image"] != runtime["runtime_image"]:
            raise ValueError("Binding runtime image differs from runtime")
        expected_copy_mode = (
            "none" if binding["accelerator"] == "cuda" else "runtime-copy"
        )
        if binding["mooncake_copy_mode"] != expected_copy_mode:
            raise ValueError("Binding Mooncake copy mode is invalid")
        pair = (revision_id, runtime_id)
        if pair in bindings_by_pair:
            raise ValueError(f"duplicate Binding pair {pair}")
        bindings_by_pair[pair] = binding
        bindings.append(binding)
    if bindings != sorted(
        bindings,
        key=lambda item: (item["builder_revision_id"], item["runtime_id"]),
    ):
        raise ValueError("Bindings are not canonically ordered")

    entries: list[dict[str, Any]] = []
    entry_keys: set[tuple[str, ...]] = set()
    for index, raw in enumerate(entry_values):
        entry = _mapping(raw, f"entries[{index}]")
        _exact_fields(entry, ENTRY_FIELDS, f"entries[{index}]")
        pair = (
            _digest(entry["builder_revision_id"], "Entry Builder revision ID"),
            _digest(entry["runtime_id"], "Entry runtime ID"),
        )
        binding = bindings_by_pair.get(pair)
        if binding is None:
            raise ValueError("Entry references an unknown Binding")
        if any(entry[field] != binding[field] for field in ENTRY_FIELDS):
            raise ValueError("Entry projection differs from Binding")
        key = _entry_key(entry)
        if key in entry_keys:
            raise ValueError(f"duplicate Entry coordinate {key}")
        entry_keys.add(key)
        entries.append(entry)
    if entries != sorted(entries, key=_entry_key):
        raise ValueError("Entries are not canonically ordered")

    exclusions: list[dict[str, Any]] = []
    exclusion_keys: set[tuple[str, ...]] = set()
    for index, raw in enumerate(exclusion_values):
        exclusion = _mapping(raw, f"exclusions[{index}]")
        _exact_fields(exclusion, EXCLUSION_FIELDS, f"exclusions[{index}]")
        reason = _string(exclusion["reason_code"], "Exclusion reason_code")
        _string(exclusion["source_kind"], "Exclusion source_kind")
        _string(exclusion["source_id"], "Exclusion source_id")
        _mapping(exclusion["evidence"], "Exclusion evidence")
        capability_id = exclusion["builder_capability_id"]
        revision_id = exclusion["builder_revision_id"]
        runtime_id = exclusion["runtime_id"]
        for reference, known, label in (
            (capability_id, capabilities_by_id, "capability"),
            (revision_id, revisions_by_id, "revision"),
            (runtime_id, runtimes_by_id, "runtime"),
        ):
            if reference is not None:
                _digest(reference, f"Exclusion {label} ID")
                if reference not in known:
                    raise ValueError(f"Exclusion references an unknown {label}")
        if reason in {
            "python-requires-mismatch",
            "variant-filtered-310p",
            "builder-sync-failed",
            "python-probe-failed",
        }:
            if any(
                item is not None for item in (capability_id, revision_id, runtime_id)
            ):
                raise ValueError("Source exclusion must not invent Catalog references")
        elif reason in {"mooncake-version-mismatch", "mooncake-probe-failed"}:
            if capability_id is not None or revision_id is not None:
                raise ValueError("Runtime probe exclusion must not invent Builders")
            if runtime_id is None:
                raise ValueError("Runtime probe exclusion must identify its runtime")
        else:
            raise ValueError(f"unsupported exclusion reason {reason!r}")
        key = _exclusion_key(exclusion)
        if key in exclusion_keys:
            raise ValueError(f"duplicate Exclusion coordinate {key}")
        exclusion_keys.add(key)
        exclusions.append(exclusion)
    if exclusions != sorted(exclusions, key=_exclusion_key):
        raise ValueError("Exclusions are not canonically ordered")
    return catalog
