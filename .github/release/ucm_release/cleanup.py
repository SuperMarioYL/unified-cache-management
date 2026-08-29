"""Retain and remove one UCM Tag release from its schema 6, 7, or 8 manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from packaging.utils import canonicalize_name, parse_wheel_filename

MANIFEST_KIND = "ucm-release-manifest"
MANIFEST_SCHEMA_VERSION = 8
SCHEMA7_MANIFEST_VERSION = 7
LEGACY_MANIFEST_SCHEMA_VERSION = 6
MANIFEST_FILENAME = "release-manifest.json"
RELEASE_TYPES = frozenset({"stable", "prerelease", "draft", "nightly"})
RETRY_DELAYS_SECONDS = (0.0, 5.0, 15.0)

_SCHEMA6_MANIFEST_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "tag",
        "release_type",
        "actions_run_id",
        "chart_oci",
        "runtime_images",
        "github_release_assets",
    }
)
_SCHEMA7_MANIFEST_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "release",
        "wheels",
        "images",
        "chart",
        "github_release_assets",
    }
)
_SCHEMA8_MANIFEST_KEYS = _SCHEMA7_MANIFEST_KEYS | {"python"}
_RELEASE_KEYS = frozenset({"tag", "type", "version", "url", "actions_run_id"})
_SCHEMA7_WHEEL_KEYS = frozenset(
    {
        "id",
        "product",
        "channel",
        "accelerator",
        "distribution",
        "version",
        "python_abi",
        "architecture",
        "filename",
        "url",
        "sha256",
        "dependencies",
    }
)
_SCHEMA8_WHEEL_KEYS = frozenset(
    {
        "id",
        "product",
        "extra",
        "accelerator",
        "distribution",
        "version",
        "python_abi",
        "architecture",
        "platform_tags",
        "filename",
        "url",
        "sha256",
        "dependencies",
    }
)
_PYTHON_KEYS = frozenset(
    {"distribution", "version", "filename", "url", "sha256", "tags", "extras", "pypi"}
)
_PYPI_KEYS = frozenset({"index_url", "project_url"})
_IMAGE_KEYS = frozenset(
    {"id", "product", "upstream", "accelerator", "os", "publications"}
)
_ACCELERATOR_KEYS = frozenset({"runtime", "variant", "soc_version"})
_UPSTREAM_KEYS = frozenset({"version", "channel"})
_OS_KEYS = frozenset({"id", "version"})
_PUBLICATION_CHANNELS = frozenset({"ghcr", "dockerhub"})
_PUBLICATION_KEYS = frozenset({"pull", "multi_arch", "members"})
_MEMBER_KEYS = frozenset({"architecture", "reference"})
_CHART_KEYS = frozenset({"name", "version", "filename", "url", "oci"})
_RUNTIME_CHANNELS = ("ghcr", "dockerhub")
_RUNTIME_IMAGE_KEYS = frozenset({"members", "indexes"})
_OCI_REFERENCE = re.compile(
    r"(?P<repository>(?:ghcr\.io|docker\.io)/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
)
_REPOSITORY = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]+)"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PATH_COMPONENT = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_MISSING_MARKERS = (
    "404",
    "manifest unknown",
    "manifest_unknown",
    "name unknown",
    "name_unknown",
    "not found",
)
_TRANSPORT_MARKERS = (
    "connection reset",
    "connection refused",
    "context deadline exceeded",
    "i/o timeout",
    "network is unreachable",
    "temporary failure",
    "timed out",
    "timeout",
    "tls handshake timeout",
)


class CleanupError(ValueError):
    """A local contract or permanent remote cleanup error."""


class RemoteError(CleanupError):
    """A structured remote failure used by the retry policy."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_missing(self) -> bool:
        return self.status == 404

    @property
    def is_retryable(self) -> bool:
        return (
            self.status is None
            or self.status in {409, 429}
            or (self.status is not None and self.status >= 500)
        )


class UnsafePackageVersion(RemoteError):
    """A GHCR package version has Tags outside the requested resource."""

    def __init__(self, reference: str, tags: Sequence[str]) -> None:
        rendered = ", ".join(sorted(tags))
        super().__init__(
            f"refusing to delete {reference}: package version also has Tags [{rendered}]",
            status=422,
        )


@dataclass(frozen=True)
class Resource:
    kind: str
    reference: str
    identifier: str | int | tuple[str, ...] | None = None
    holds_manifest: bool = False


@dataclass(frozen=True)
class ResourceFailure:
    resource: Resource
    attempts: int
    final_error: str


@dataclass(frozen=True)
class CleanupReport:
    tag: str
    completed: bool
    stopped_phase: int | None
    failures: tuple[ResourceFailure, ...]


@dataclass(frozen=True)
class ManifestRecord:
    manifest: dict[str, Any]
    created_at: str
    release_id: int
    draft: bool
    prerelease: bool


@dataclass(frozen=True)
class RetentionSelection:
    candidates: tuple[ManifestRecord, ...]
    skipped_reason: str | None = None


class CleanupRemote(Protocol):
    repository: str

    def probe(self, resource: Resource) -> object | None: ...

    def delete(self, resource: Resource, state: object) -> None: ...

    def release_resources(self, tag: str) -> list[Resource]: ...


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CleanupError(f"{context} must be an object")
    return value


def _array(value: object, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise CleanupError(f"{context} must be an array")
    return value


def _exact_keys(value: dict[str, Any], expected: frozenset[str], context: str) -> None:
    if set(value) != set(expected):
        missing = sorted(expected - value.keys())
        extra = sorted(value.keys() - expected)
        raise CleanupError(
            f"{context} fields must be exact; missing={missing}, extra={extra}"
        )


def _tagged_oci_reference(value: object, context: str, *, registry: str) -> str:
    if not isinstance(value, str):
        raise CleanupError(f"{context} must be a tagged OCI reference")
    match = _OCI_REFERENCE.fullmatch(value)
    if match is None or not match.group("repository").startswith(registry + "/"):
        raise CleanupError(f"{context} must be a tagged {registry} reference")
    return value


def _validate_schema6(
    manifest: dict[str, Any], *, expected_tag: str | None = None
) -> dict[str, Any]:
    """Validate the historical cleanup-only Schema 6 contract."""
    _exact_keys(manifest, _SCHEMA6_MANIFEST_KEYS, "release manifest schema 6")
    tag = manifest["tag"]
    if not isinstance(tag, str) or not tag or tag.strip() != tag:
        raise CleanupError("release manifest Tag must be a non-empty exact string")
    if expected_tag is not None and tag != expected_tag:
        raise CleanupError("release manifest Tag differs from the requested Tag")
    if (
        not isinstance(manifest["release_type"], str)
        or manifest["release_type"] not in RELEASE_TYPES
    ):
        raise CleanupError("release manifest release type is invalid")
    run_id = manifest["actions_run_id"]
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise CleanupError("release manifest Actions run ID must be a positive integer")

    chart = manifest["chart_oci"]
    if chart is not None:
        _tagged_oci_reference(chart, "release manifest Chart OCI", registry="ghcr.io")

    runtime_images = _mapping(
        manifest["runtime_images"], "release manifest Runtime Images"
    )
    _exact_keys(
        runtime_images, frozenset(_RUNTIME_CHANNELS), "release manifest Runtime Images"
    )
    for channel in _RUNTIME_CHANNELS:
        channel_value = _mapping(
            runtime_images[channel], f"release manifest {channel} images"
        )
        _exact_keys(
            channel_value, _RUNTIME_IMAGE_KEYS, f"release manifest {channel} images"
        )
        registry = "ghcr.io" if channel == "ghcr" else "docker.io"
        seen: set[str] = set()
        for image_kind in ("members", "indexes"):
            references = _array(
                channel_value[image_kind],
                f"release manifest {channel} {image_kind}",
            )
            for index, reference in enumerate(references):
                normalized = _tagged_oci_reference(
                    reference,
                    f"release manifest {channel} {image_kind}[{index}]",
                    registry=registry,
                )
                if normalized in seen:
                    raise CleanupError(
                        f"release manifest {channel} image references must be unique"
                    )
                seen.add(normalized)

    assets = _array(
        manifest["github_release_assets"], "release manifest GitHub Release assets"
    )
    seen_assets: set[str] = set()
    for asset in assets:
        if (
            not isinstance(asset, str)
            or not asset
            or asset in {".", ".."}
            or Path(asset).name != asset
        ):
            raise CleanupError(
                "release manifest has an invalid GitHub Release asset name"
            )
        if asset in seen_assets:
            raise CleanupError("release manifest GitHub Release assets must be unique")
        seen_assets.add(asset)
    if MANIFEST_FILENAME not in seen_assets:
        raise CleanupError(
            "release manifest must list itself as a GitHub Release asset"
        )
    return manifest


def _nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CleanupError(f"{context} must be a non-empty exact string")
    return value


def _filename(value: object, context: str) -> str:
    name = _nonempty_string(value, context)
    if name in {".", ".."} or Path(name).name != name:
        raise CleanupError(f"{context} must be a filename")
    return name


def _github_url(value: object, context: str) -> str:
    url = _nonempty_string(value, context)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not parsed.path:
        raise CleanupError(f"{context} must be an https://github.com URL")
    return url


def _validate_assets(value: object) -> set[str]:
    assets = _array(value, "release manifest GitHub Release assets")
    seen: set[str] = set()
    for index, asset in enumerate(assets):
        name = _filename(asset, f"release manifest GitHub Release assets[{index}]")
        if name in seen:
            raise CleanupError("release manifest GitHub Release assets must be unique")
        seen.add(name)
    if MANIFEST_FILENAME not in seen:
        raise CleanupError(
            "release manifest must list itself as a GitHub Release asset"
        )
    return seen


def _validate_accelerator(value: object, context: str) -> None:
    accelerator = _mapping(value, context)
    _exact_keys(accelerator, _ACCELERATOR_KEYS, context)
    for field in sorted(_ACCELERATOR_KEYS):
        _nonempty_string(accelerator[field], f"{context} {field}")


def _validate_wheel_filename(
    wheel: dict[str, Any], platform_tags: Sequence[str], context: str
) -> None:
    try:
        distribution, version, build, tags = parse_wheel_filename(wheel["filename"])
    except ValueError as error:
        raise CleanupError(f"{context} filename is not a valid Wheel") from error
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        wheel["architecture"], wheel["architecture"]
    )
    if (
        canonicalize_name(str(distribution)) != canonicalize_name(wheel["distribution"])
        or str(version) != wheel["version"]
        or build
        or {tag.interpreter for tag in tags} != {wheel["python_abi"]}
        or {tag.abi for tag in tags} != {wheel["python_abi"]}
        or {tag.platform for tag in tags} != set(platform_tags)
        or len(platform_tags) != 1
        or not platform_tags[0].endswith(f"_{architecture}")
    ):
        raise CleanupError(f"{context} filename and platform identity differ")


def _validate_publication(value: object, *, channel: str, context: str) -> None:
    publication = _mapping(value, context)
    _exact_keys(publication, _PUBLICATION_KEYS, context)
    registry = "ghcr.io" if channel == "ghcr" else "docker.io"
    pull = _tagged_oci_reference(
        publication["pull"], f"{context} pull", registry=registry
    )
    multi_arch = publication["multi_arch"]
    if not isinstance(multi_arch, bool):
        raise CleanupError(f"{context} multi_arch must be boolean")
    members = _array(publication["members"], f"{context} members")
    if not members:
        raise CleanupError(f"{context} members must not be empty")
    architectures: set[str] = set()
    references: set[str] = set()
    for index, raw_member in enumerate(members):
        member_context = f"{context} members[{index}]"
        member = _mapping(raw_member, member_context)
        _exact_keys(member, _MEMBER_KEYS, member_context)
        architecture = _nonempty_string(
            member["architecture"], f"{member_context} architecture"
        )
        reference = _tagged_oci_reference(
            member["reference"], f"{member_context} reference", registry=registry
        )
        if architecture in architectures or reference in references:
            raise CleanupError(f"{context} members must be unique")
        architectures.add(architecture)
        references.add(reference)
    if multi_arch and len(members) < 2:
        raise CleanupError(f"{context} multi_arch requires at least two members")
    if not multi_arch and (len(members) != 1 or pull not in references):
        raise CleanupError(
            f"{context} single-architecture pull must equal its only member"
        )


def _validate_schema7_or_8(
    manifest: dict[str, Any], *, expected_tag: str | None, schema_version: int
) -> dict[str, Any]:
    manifest_keys = (
        _SCHEMA7_MANIFEST_KEYS
        if schema_version == SCHEMA7_MANIFEST_VERSION
        else _SCHEMA8_MANIFEST_KEYS
    )
    _exact_keys(manifest, manifest_keys, f"release manifest schema {schema_version}")
    release = _mapping(manifest["release"], "release manifest release")
    _exact_keys(release, _RELEASE_KEYS, "release manifest release")
    tag = _nonempty_string(release["tag"], "release manifest Tag")
    if expected_tag is not None and tag != expected_tag:
        raise CleanupError("release manifest Tag differs from the requested Tag")
    if not isinstance(release["type"], str) or release["type"] not in RELEASE_TYPES:
        raise CleanupError("release manifest release type is invalid")
    _nonempty_string(release["version"], "release manifest version")
    _github_url(release["url"], "release manifest Release URL")
    run_id = release["actions_run_id"]
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise CleanupError("release manifest Actions run ID must be a positive integer")

    python_extras: dict[str, str] | None = None
    asset_filenames: set[str] = set()
    if schema_version == MANIFEST_SCHEMA_VERSION:
        python_package = _mapping(manifest["python"], "release manifest Python")
        _exact_keys(python_package, _PYTHON_KEYS, "release manifest Python")
        meta_distribution = python_package.get("distribution")
        if (
            not isinstance(meta_distribution, str)
            or re.fullmatch(r"(?:[a-z0-9]+-)*uc-manager", meta_distribution) is None
            or python_package.get("version") != release["version"]
        ):
            raise CleanupError("release manifest Python identity is invalid")
        python_filename = _filename(
            python_package["filename"], "release manifest Python filename"
        )
        asset_filenames.add(python_filename)
        _github_url(python_package["url"], "release manifest Python URL")
        if (
            not isinstance(python_package["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", python_package["sha256"]) is None
        ):
            raise CleanupError("release manifest Python sha256 is invalid")
        tags = _array(python_package["tags"], "release manifest Python tags")
        if any(not isinstance(tag, str) or not tag for tag in tags) or tags != sorted(
            set(tags)
        ):
            raise CleanupError("release manifest Python tags are invalid")
        raw_extras = _mapping(
            python_package["extras"], "release manifest Python extras"
        )
        if not raw_extras:
            raise CleanupError("release manifest Python extras must not be empty")
        python_extras = {}
        distributions: set[str] = set()
        for extra in sorted(raw_extras):
            distribution = _nonempty_string(
                raw_extras[extra], f"release manifest Python extra {extra}"
            )
            if (
                _PATH_COMPONENT.fullmatch(extra) is None
                or not distribution.startswith(f"{meta_distribution}-")
                or distribution in distributions
            ):
                raise CleanupError("release manifest Python extras are invalid")
            distributions.add(distribution)
            python_extras[extra] = distribution
        if python_package["pypi"] is not None:
            pypi = _mapping(python_package["pypi"], "release manifest PyPI")
            _exact_keys(pypi, _PYPI_KEYS, "release manifest PyPI")
            for field in sorted(_PYPI_KEYS):
                url = _nonempty_string(pypi[field], f"release manifest PyPI {field}")
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme != "https" or parsed.netloc not in {
                    "pypi.org",
                    "test.pypi.org",
                }:
                    raise CleanupError("release manifest PyPI URL is invalid")
            pypi_host = urllib.parse.urlparse(pypi["index_url"]).netloc
            if (
                pypi["index_url"] != f"https://{pypi_host}/simple"
                or pypi["project_url"]
                != f"https://{pypi_host}/project/{meta_distribution}/"
                f"{urllib.parse.quote(str(release['version']), safe='')}/"
            ):
                raise CleanupError("release manifest PyPI URLs differ from the release")

    wheel_keys = (
        _SCHEMA7_WHEEL_KEYS
        if schema_version == SCHEMA7_MANIFEST_VERSION
        else _SCHEMA8_WHEEL_KEYS
    )
    wheel_ids: set[str] = set()
    wheel_extras: set[str] = set()
    for index, raw_wheel in enumerate(
        _array(manifest["wheels"], "release manifest Wheels")
    ):
        context = f"release manifest Wheels[{index}]"
        wheel = _mapping(raw_wheel, context)
        _exact_keys(wheel, wheel_keys, context)
        wheel_id = _nonempty_string(wheel["id"], f"{context} id")
        filename = _filename(wheel["filename"], f"{context} filename")
        if wheel_id in wheel_ids or filename in asset_filenames:
            raise CleanupError("release manifest Wheels must have unique IDs and files")
        wheel_ids.add(wheel_id)
        asset_filenames.add(filename)
        for field in (
            "product",
            "distribution",
            "version",
            "python_abi",
            "architecture",
        ):
            _nonempty_string(wheel[field], f"{context} {field}")
        _validate_accelerator(wheel["accelerator"], f"{context} accelerator")
        _github_url(wheel["url"], f"{context} URL")
        if (
            not isinstance(wheel["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"]) is None
        ):
            raise CleanupError(f"{context} sha256 is invalid")
        dependencies = _array(wheel["dependencies"], f"{context} dependencies")
        if any(
            not isinstance(item, str) or not item for item in dependencies
        ) or dependencies != sorted(set(dependencies)):
            raise CleanupError(f"{context} dependencies are invalid")
        if schema_version == SCHEMA7_MANIFEST_VERSION:
            channel = _nonempty_string(wheel["channel"], f"{context} channel")
            if (
                wheel["distribution"] != "uc-manager"
                or _PATH_COMPONENT.fullmatch(channel) is None
            ):
                raise CleanupError("release manifest Schema 7 Wheel is invalid")
        else:
            assert python_extras is not None
            extra = _nonempty_string(wheel["extra"], f"{context} extra")
            if (
                _PATH_COMPONENT.fullmatch(extra) is None
                or python_extras.get(extra) != wheel["distribution"]
                or wheel["version"] != release["version"]
            ):
                raise CleanupError("release manifest Wheel extra mapping is invalid")
            platform_tags = _array(wheel["platform_tags"], f"{context} platform tags")
            if (
                not platform_tags
                or any(not isinstance(item, str) or not item for item in platform_tags)
                or platform_tags != sorted(set(platform_tags))
            ):
                raise CleanupError(f"{context} platform tags are invalid")
            _validate_wheel_filename(wheel, platform_tags, context)
            wheel_extras.add(extra)
    if python_extras is not None and wheel_extras != set(python_extras):
        raise CleanupError("release manifest Wheels do not cover every Python extra")

    image_ids: set[str] = set()
    for index, raw_image in enumerate(
        _array(manifest["images"], "release manifest Images")
    ):
        context = f"release manifest Images[{index}]"
        image = _mapping(raw_image, context)
        _exact_keys(image, _IMAGE_KEYS, context)
        image_id = _nonempty_string(image["id"], f"{context} id")
        if image_id in image_ids:
            raise CleanupError("release manifest Image IDs must be unique")
        image_ids.add(image_id)
        _nonempty_string(image["product"], f"{context} product")
        upstream = _mapping(image["upstream"], f"{context} upstream")
        _exact_keys(upstream, _UPSTREAM_KEYS, f"{context} upstream")
        for field in sorted(_UPSTREAM_KEYS):
            _nonempty_string(upstream[field], f"{context} upstream {field}")
        _validate_accelerator(image["accelerator"], f"{context} accelerator")
        operating_system = _mapping(image["os"], f"{context} OS")
        _exact_keys(operating_system, _OS_KEYS, f"{context} OS")
        for field in sorted(_OS_KEYS):
            _nonempty_string(operating_system[field], f"{context} OS {field}")
        publications = _mapping(image["publications"], f"{context} publications")
        _exact_keys(publications, _PUBLICATION_CHANNELS, f"{context} publications")
        if all(publications[channel] is None for channel in _RUNTIME_CHANNELS):
            raise CleanupError(f"{context} has no publication")
        for channel in _RUNTIME_CHANNELS:
            if publications[channel] is not None:
                _validate_publication(
                    publications[channel],
                    channel=channel,
                    context=f"{context} publications {channel}",
                )

    chart = _mapping(manifest["chart"], "release manifest Chart")
    _exact_keys(chart, _CHART_KEYS, "release manifest Chart")
    for field in ("name", "version"):
        _nonempty_string(chart[field], f"release manifest Chart {field}")
    chart_filename = _filename(chart["filename"], "release manifest Chart filename")
    if chart_filename in asset_filenames:
        raise CleanupError("release manifest artifact filenames must be unique")
    asset_filenames.add(chart_filename)
    _github_url(chart["url"], "release manifest Chart URL")
    if chart["oci"] is not None:
        _tagged_oci_reference(
            chart["oci"], "release manifest Chart OCI", registry="ghcr.io"
        )
    assets = _validate_assets(manifest["github_release_assets"])
    if "install-catalog.json" in assets:
        raise CleanupError("release manifest must not list install-catalog.json")
    if schema_version == MANIFEST_SCHEMA_VERSION and (
        (manifest["python"]["pypi"] is not None) != ("pypi-receipt.json" in assets)
    ):
        raise CleanupError(
            "release manifest PyPI receipt asset differs from publication"
        )
    missing_assets = sorted(asset_filenames - assets)
    if missing_assets:
        raise CleanupError(f"release manifest assets are missing {missing_assets}")
    return manifest


def validate_manifest(
    value: object, *, expected_tag: str | None = None
) -> dict[str, Any]:
    """Validate an exact public Schema 6, 7, or 8 manifest."""
    manifest = _mapping(value, "release manifest")
    if manifest.get("kind") != MANIFEST_KIND:
        raise CleanupError("release manifest kind is invalid")
    schema_version = manifest.get("schema_version")
    if schema_version == LEGACY_MANIFEST_SCHEMA_VERSION:
        return _validate_schema6(manifest, expected_tag=expected_tag)
    if schema_version in {SCHEMA7_MANIFEST_VERSION, MANIFEST_SCHEMA_VERSION}:
        return _validate_schema7_or_8(
            manifest,
            expected_tag=expected_tag,
            schema_version=schema_version,
        )
    raise CleanupError("release manifest must use schema version 6, 7, or 8")


def _cleanup_model(value: object, *, expected_tag: str | None = None) -> dict[str, Any]:
    manifest = validate_manifest(value, expected_tag=expected_tag)
    if manifest["schema_version"] == LEGACY_MANIFEST_SCHEMA_VERSION:
        return manifest
    release = manifest["release"]
    runtime_images = {
        channel: {"members": set(), "indexes": set()} for channel in _RUNTIME_CHANNELS
    }
    for image in manifest["images"]:
        for channel in _RUNTIME_CHANNELS:
            publication = image["publications"][channel]
            if publication is None:
                continue
            destination = "indexes" if publication["multi_arch"] else "members"
            runtime_images[channel][destination].add(publication["pull"])
            runtime_images[channel]["members"].update(
                member["reference"] for member in publication["members"]
            )
    return {
        "kind": MANIFEST_KIND,
        "schema_version": LEGACY_MANIFEST_SCHEMA_VERSION,
        "tag": release["tag"],
        "release_type": release["type"],
        "actions_run_id": release["actions_run_id"],
        "chart_oci": manifest["chart"]["oci"],
        "runtime_images": {
            channel: {
                kind: sorted(references) for kind, references in channel_value.items()
            }
            for channel, channel_value in runtime_images.items()
        },
        "github_release_assets": list(manifest["github_release_assets"]),
    }


def registry_resources(manifest: object) -> list[Resource]:
    """Project phase-one resources in the required deletion order."""
    validated = _cleanup_model(manifest)
    ghcr_resources: list[tuple[str, str]] = []
    if validated["chart_oci"] is not None:
        ghcr_resources.append(("chart-oci", validated["chart_oci"]))
    images = validated["runtime_images"]
    ghcr_resources.extend(("ghcr-index", ref) for ref in images["ghcr"]["indexes"])
    ghcr_resources.extend(("ghcr-member", ref) for ref in images["ghcr"]["members"])

    allowed_tags_by_package: dict[str, set[str]] = {}
    for _, reference in ghcr_resources:
        match = _OCI_REFERENCE.fullmatch(reference)
        if match is None:
            raise AssertionError("validated GHCR reference no longer parses")
        allowed_tags_by_package.setdefault(match.group("repository"), set()).add(
            match.group("tag")
        )
    result = []
    for kind, reference in ghcr_resources:
        match = _OCI_REFERENCE.fullmatch(reference)
        if match is None:
            raise AssertionError("validated GHCR reference no longer parses")
        allowed_tags = tuple(sorted(allowed_tags_by_package[match.group("repository")]))
        result.append(Resource(kind, reference, allowed_tags))

    result.extend(
        Resource("dockerhub-index", ref) for ref in images["dockerhub"]["indexes"]
    )
    result.extend(
        Resource("dockerhub-member", ref) for ref in images["dockerhub"]["members"]
    )
    return result


def select_retention_candidates(
    records: Sequence[ManifestRecord],
    *,
    current_tag: str,
    release_type: str,
    max_count: int,
    pypi_enabled: bool,
) -> RetentionSelection:
    """Select the oldest excess same-type Tags without guessing old manifests."""
    if not isinstance(current_tag, str) or not current_tag:
        raise CleanupError("current Tag must be non-empty")
    if release_type not in RELEASE_TYPES:
        raise CleanupError("retention release type is invalid")
    if (
        not isinstance(max_count, int)
        or isinstance(max_count, bool)
        or max_count == 0
        or max_count < -1
    ):
        raise CleanupError("max_count must be -1 or an integer >= 1")
    if not isinstance(pypi_enabled, bool):
        raise CleanupError("pypi_enabled must be boolean")
    if max_count == -1:
        return RetentionSelection((), "retention skipped: max_count is unlimited")
    if pypi_enabled:
        return RetentionSelection(
            (),
            "retention skipped: PyPI is enabled for this release type",
        )

    grouped: dict[str, list[ManifestRecord]] = {}
    normalized_by_record: dict[int, dict[str, Any]] = {}
    for record in records:
        try:
            manifest = _cleanup_model(record.manifest)
        except CleanupError:
            continue
        if manifest["release_type"] != release_type or manifest["tag"] == current_tag:
            continue
        if (
            not isinstance(record.created_at, str)
            or not record.created_at
            or not isinstance(record.release_id, int)
            or isinstance(record.release_id, bool)
            or record.release_id < 1
            or not isinstance(record.draft, bool)
            or not isinstance(record.prerelease, bool)
        ):
            continue
        expected_visibility = {
            "stable": (False, False),
            "prerelease": (False, True),
            "draft": (True, True),
            "nightly": (False, True),
        }[release_type]
        if (record.draft, record.prerelease) != expected_visibility:
            continue
        normalized_by_record[id(record)] = manifest
        grouped.setdefault(manifest["tag"], []).append(record)

    unique_records: list[ManifestRecord] = []
    for tag_records in grouped.values():
        first = tag_records[0].manifest
        if any(record.manifest != first for record in tag_records[1:]):
            continue
        unique_records.append(
            min(tag_records, key=lambda item: (item.created_at, item.release_id))
        )
    unique_records.sort(
        key=lambda item: (
            item.created_at,
            item.release_id,
            normalized_by_record[id(item)]["tag"],
        )
    )
    allowed_other_tags = max_count - 1
    excess = max(0, len(unique_records) - allowed_other_tags)
    return RetentionSelection(tuple(unique_records[:excess]))


def _failure(
    resource: Resource, attempts: int, error: BaseException
) -> ResourceFailure:
    return ResourceFailure(resource, attempts, str(error) or type(error).__name__)


def delete_resource_with_retry(
    remote: CleanupRemote,
    resource: Resource,
    *,
    sleeper=time.sleep,
    fail_resource: str | None = None,
) -> ResourceFailure | None:
    """Probe and delete one resource with exactly three independent attempts."""
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        print(
            f"cleanup resource_type={resource.kind} reference={resource.reference} "
            f"attempt={attempt}/{len(RETRY_DELAYS_SECONDS)} delay={int(delay)}s",
            flush=True,
        )
        if delay:
            sleeper(delay)
        try:
            state = remote.probe(resource)
            if state is None:
                return None
            if fail_resource is not None and resource.reference == fail_resource:
                raise RemoteError(
                    f"synthetic HTTP 503 for {resource.reference}", status=503
                )
            remote.delete(resource, state)
            return None
        except RemoteError as error:
            if error.is_missing:
                return None
            if error.is_retryable and attempt < len(RETRY_DELAYS_SECONDS):
                continue
            return _failure(resource, attempt, error)
        except CleanupError as error:
            return _failure(resource, attempt, error)
    raise AssertionError("resource retry loop exhausted without a result")


def _run_phase(
    remote: CleanupRemote,
    resources: Sequence[Resource],
    *,
    sleeper,
    fail_resource: str | None,
) -> list[ResourceFailure]:
    failures: list[ResourceFailure] = []
    for resource in resources:
        failure = delete_resource_with_retry(
            remote,
            resource,
            sleeper=sleeper,
            fail_resource=fail_resource,
        )
        if failure is not None:
            failures.append(failure)
    return failures


def _release_resources_with_retry(
    remote: CleanupRemote, tag: str, *, sleeper
) -> tuple[list[Resource], ResourceFailure | None]:
    collection = Resource("github-releases", tag)
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        print(
            f"cleanup resource_type={collection.kind} reference={collection.reference} "
            f"attempt={attempt}/{len(RETRY_DELAYS_SECONDS)} delay={int(delay)}s",
            flush=True,
        )
        if delay:
            sleeper(delay)
        try:
            return remote.release_resources(tag), None
        except RemoteError as error:
            if error.is_missing:
                return [], None
            if error.is_retryable and attempt < len(RETRY_DELAYS_SECONDS):
                continue
            return [], _failure(collection, attempt, error)
        except CleanupError as error:
            return [], _failure(collection, attempt, error)
    raise AssertionError("Release discovery retry loop exhausted without a result")


def cleanup_manifest(
    manifest: object,
    remote: CleanupRemote,
    *,
    sleeper=time.sleep,
    fail_resource: str | None = None,
) -> CleanupReport:
    """Delete one Tag through the four recovery-preserving phases."""
    validated = _cleanup_model(manifest)
    tag = validated["tag"]
    phase_one_resources = registry_resources(validated)

    failures = _run_phase(
        remote,
        phase_one_resources,
        sleeper=sleeper,
        fail_resource=fail_resource,
    )
    if failures:
        return CleanupReport(tag, False, 1, tuple(failures))

    run_id = validated["actions_run_id"]
    actions = Resource(
        "actions-run",
        f"https://github.com/{remote.repository}/actions/runs/{run_id}",
        run_id,
    )
    failures = _run_phase(
        remote, [actions], sleeper=sleeper, fail_resource=fail_resource
    )
    if failures:
        return CleanupReport(tag, False, 2, tuple(failures))

    git_tag = Resource("git-tag", tag, tag)
    failures = _run_phase(
        remote, [git_tag], sleeper=sleeper, fail_resource=fail_resource
    )
    if failures:
        return CleanupReport(tag, False, 3, tuple(failures))

    releases, discovery_failure = _release_resources_with_retry(
        remote, tag, sleeper=sleeper
    )
    if discovery_failure is not None:
        return CleanupReport(tag, False, 4, (discovery_failure,))
    unbacked = [resource for resource in releases if not resource.holds_manifest]
    failures = _run_phase(
        remote, unbacked, sleeper=sleeper, fail_resource=fail_resource
    )
    if failures:
        return CleanupReport(tag, False, 4, tuple(failures))
    backed = [resource for resource in releases if resource.holds_manifest]
    failures = _run_phase(remote, backed, sleeper=sleeper, fail_resource=fail_resource)
    return CleanupReport(tag, not failures, 4 if failures else None, tuple(failures))


def render_failure_summary(failures: Sequence[ResourceFailure]) -> str:
    """Render only final failures; successful attempts intentionally stay out."""
    if not failures:
        return ""

    def cell(value: object) -> str:
        return " ".join(str(value).split()).replace("|", "\\|")

    lines = [
        "## UCM release cleanup final failures",
        "",
        "| Resource type | Reference | Final error |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {cell(item.resource.kind)} | {cell(item.resource.reference)} | "
        f"{cell(item.final_error)} |"
        for item in failures
    )
    return "\n".join(lines) + "\n"


def append_failure_summary(
    path: Path | None, failures: Sequence[ResourceFailure]
) -> None:
    summary = render_failure_summary(failures)
    if path is None or not summary:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(summary)


class ProductionRemote:
    """GitHub REST and Crane adapter for the cleanup domain Interface."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        crane: str = "crane",
        api_base: str = "https://api.github.com",
        opener: Any | None = None,
    ) -> None:
        match = _REPOSITORY.fullmatch(repository)
        if match is None:
            raise CleanupError("repository must use owner/name form")
        if not token:
            raise CleanupError("GH_TOKEN or GITHUB_TOKEN is required")
        self.repository = repository
        self.owner = match.group("owner")
        self.token = token
        self.crane = crane
        self.api_base = api_base.rstrip("/")
        self._opener = opener or urllib.request.build_opener()
        self._package_owner_prefix: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
    ) -> bytes:
        url = path if path.startswith("https://") else self.api_base + path
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "ucm-release-cleanup",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            raise RemoteError(
                f"GitHub API HTTP {error.code}: {detail or error.reason}",
                status=error.code,
            ) from error
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise RemoteError(f"GitHub API transport error: {error}") from error

    def _github_json(self, method: str, path: str) -> Any:
        raw = self._request(method, path)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CleanupError("GitHub API returned malformed JSON") from error

    def _all_pages(self, path: str) -> list[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        values: list[dict[str, Any]] = []
        for page in range(1, 1001):
            value = self._github_json(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value
            ):
                raise CleanupError("GitHub API paginated response must be an array")
            values.extend(value)
            if len(value) < 100:
                return values
        raise CleanupError("GitHub API pagination exceeded 1000 pages")

    def list_releases(self) -> list[dict[str, Any]]:
        owner_repo = urllib.parse.quote(self.repository, safe="/")
        return self._all_pages(f"/repos/{owner_repo}/releases")

    @staticmethod
    def _manifest_asset(release: dict[str, Any]) -> dict[str, Any] | None:
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise CleanupError("GitHub Release assets must be an array")
        matches = [
            asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name") == MANIFEST_FILENAME
        ]
        if len(matches) > 1:
            raise CleanupError("GitHub Release has duplicate release manifest assets")
        return matches[0] if matches else None

    def _download_manifest(
        self, release: dict[str, Any], *, expected_tag: str
    ) -> dict[str, Any] | None:
        asset = self._manifest_asset(release)
        if asset is None:
            return None
        url = asset.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CleanupError("release manifest asset has no API URL")
        raw = self._request("GET", url, accept="application/octet-stream")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CleanupError("release manifest asset is not valid JSON") from error
        return validate_manifest(value, expected_tag=expected_tag)

    def load_manifest_for_tag(self, tag: str) -> dict[str, Any]:
        releases = [
            release
            for release in self.list_releases()
            if release.get("tag_name") == tag
        ]
        manifests = [
            manifest
            for release in releases
            if (manifest := self._download_manifest(release, expected_tag=tag))
            is not None
        ]
        if not manifests:
            raise CleanupError(f"Tag {tag} has no exact schema 6, 7, or 8 manifest")
        if any(manifest != manifests[0] for manifest in manifests[1:]):
            raise CleanupError(f"Tag {tag} has conflicting release manifests")
        return manifests[0]

    def list_manifest_records(self) -> list[ManifestRecord]:
        records: list[ManifestRecord] = []
        for release in self.list_releases():
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not tag:
                continue
            try:
                manifest = self._download_manifest(release, expected_tag=tag)
            except RemoteError:
                raise
            except CleanupError:
                continue
            if manifest is None:
                continue
            created_at = release.get("created_at")
            release_id = release.get("id")
            draft = release.get("draft")
            prerelease = release.get("prerelease")
            if (
                not isinstance(created_at, str)
                or not created_at
                or not isinstance(release_id, int)
                or isinstance(release_id, bool)
                or release_id < 1
                or not isinstance(draft, bool)
                or not isinstance(prerelease, bool)
            ):
                continue
            records.append(
                ManifestRecord(
                    manifest,
                    created_at,
                    release_id,
                    draft,
                    prerelease,
                )
            )
        return records

    def _owner_package_prefix(self) -> str:
        if self._package_owner_prefix is not None:
            return self._package_owner_prefix
        owner = urllib.parse.quote(self.owner, safe="")
        value = self._github_json("GET", f"/users/{owner}")
        if not isinstance(value, dict) or value.get("type") not in {
            "Organization",
            "User",
        }:
            raise CleanupError("GitHub package owner type is invalid")
        prefix = "orgs" if value["type"] == "Organization" else "users"
        self._package_owner_prefix = f"/{prefix}/{owner}"
        return self._package_owner_prefix

    def _ghcr_version_state(
        self, reference: str, *, allowed_tags: Sequence[str]
    ) -> str | None:
        match = _OCI_REFERENCE.fullmatch(reference)
        if match is None or not reference.startswith("ghcr.io/"):
            raise CleanupError("GHCR resource reference is invalid")
        repository = match.group("repository").removeprefix("ghcr.io/")
        parts = repository.split("/")
        if len(parts) < 2 or parts[0].casefold() != self.owner.casefold():
            raise CleanupError("GHCR resource does not belong to the repository owner")
        package = urllib.parse.quote("/".join(parts[1:]), safe="")
        base = f"{self._owner_package_prefix()}/packages/container/{package}/versions"
        try:
            versions = self._all_pages(base)
        except RemoteError as error:
            if error.is_missing:
                return None
            raise
        target_tag = match.group("tag")
        matches: list[tuple[int, list[str]]] = []
        for version in versions:
            version_id = version.get("id")
            metadata = version.get("metadata")
            container = (
                metadata.get("container") if isinstance(metadata, dict) else None
            )
            tags = container.get("tags") if isinstance(container, dict) else None
            if not isinstance(tags, list) or any(
                not isinstance(tag, str) for tag in tags
            ):
                raise CleanupError("GHCR package version Tags are malformed")
            if target_tag in tags:
                if not isinstance(version_id, int) or isinstance(version_id, bool):
                    raise CleanupError("GHCR package version ID is malformed")
                matches.append((version_id, tags))
        if not matches:
            return None
        if len(matches) != 1:
            raise RemoteError(
                f"GHCR Tag {reference} resolves to multiple package versions",
                status=422,
            )
        version_id, tags = matches[0]
        allowed = set(allowed_tags)
        if target_tag not in allowed:
            raise CleanupError("GHCR resource allowed Tag set omits its target Tag")
        other_tags = sorted(set(tags) - allowed)
        if other_tags:
            raise UnsafePackageVersion(reference, other_tags)
        return f"{base}/{version_id}"

    @staticmethod
    def _crane_error(detail: str) -> RemoteError:
        normalized = " ".join(detail.casefold().split())
        if any(marker in normalized for marker in _MISSING_MARKERS):
            return RemoteError(f"Crane resource is absent: {detail}", status=404)
        if any(marker in normalized for marker in _TRANSPORT_MARKERS):
            return RemoteError(f"Crane transport error: {detail}")
        status_match = re.search(
            r"(?:http|status(?: code)?|response status)\D{0,8}([45][0-9]{2})",
            normalized,
        )
        if status_match is not None:
            status = int(status_match.group(1))
            return RemoteError(f"Crane HTTP {status}: {detail}", status=status)
        if "too many requests" in normalized:
            return RemoteError(f"Crane HTTP 429: {detail}", status=429)
        if "unauthorized" in normalized or "authentication required" in normalized:
            return RemoteError(f"Crane HTTP 401: {detail}", status=401)
        if "denied" in normalized or "forbidden" in normalized:
            return RemoteError(f"Crane HTTP 403: {detail}", status=403)
        return RemoteError(f"Crane permanent error: {detail}", status=422)

    def _run_crane(self, operation: str, reference: str) -> str:
        try:
            result = subprocess.run(
                [self.crane, operation, reference],
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            raise RemoteError(f"Crane {operation} timed out for {reference}") from error
        except OSError as error:
            raise RemoteError(f"Crane {operation} transport error: {error}") from error
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or str(result.returncode)
            )
            raise self._crane_error(detail)
        return result.stdout.strip()

    def _github_path_state(self, resource: Resource) -> str | None:
        if resource.kind == "actions-run":
            path = f"/repos/{self.repository}/actions/runs/{resource.identifier}"
        elif resource.kind == "git-tag":
            ref = urllib.parse.quote(f"tags/{resource.identifier}", safe="/")
            path = f"/repos/{self.repository}/git/ref/{ref}"
        elif resource.kind == "github-release":
            path = f"/repos/{self.repository}/releases/{resource.identifier}"
        else:
            raise CleanupError(f"unsupported GitHub resource kind: {resource.kind}")
        try:
            value = self._github_json("GET", path)
        except RemoteError as error:
            if error.is_missing:
                return None
            raise
        if resource.kind == "github-release" and (
            not isinstance(value, dict)
            or value.get("tag_name") != str(resource.reference).split("#", 1)[0]
        ):
            return None
        return path

    def probe(self, resource: Resource) -> object | None:
        if resource.kind in {"chart-oci", "ghcr-index", "ghcr-member"}:
            if not isinstance(resource.identifier, tuple) or any(
                not isinstance(tag, str) for tag in resource.identifier
            ):
                raise CleanupError("GHCR resource allowed Tag set is invalid")
            return self._ghcr_version_state(
                resource.reference, allowed_tags=resource.identifier
            )
        if resource.kind in {"dockerhub-index", "dockerhub-member"}:
            return self._run_crane("digest", resource.reference)
        return self._github_path_state(resource)

    def delete(self, resource: Resource, state: object) -> None:
        if resource.kind in {"chart-oci", "ghcr-index", "ghcr-member"}:
            if not isinstance(state, str):
                raise CleanupError("GHCR deletion state is invalid")
            self._github_json("DELETE", state)
            return
        if resource.kind in {"dockerhub-index", "dockerhub-member"}:
            if not isinstance(state, str) or _DIGEST.fullmatch(state) is None:
                raise CleanupError("DockerHub deletion state is not a manifest digest")
            match = _OCI_REFERENCE.fullmatch(resource.reference)
            if match is None:
                raise CleanupError("DockerHub deletion reference is invalid")
            self._run_crane("delete", f"{match.group('repository')}@{state}")
            return
        if not isinstance(state, str):
            raise CleanupError("GitHub deletion state is invalid")
        delete_path = state
        if resource.kind == "git-tag":
            ref = urllib.parse.quote(f"tags/{resource.identifier}", safe="/")
            delete_path = f"/repos/{self.repository}/git/refs/{ref}"
        self._github_json("DELETE", delete_path)

    def release_resources(self, tag: str) -> list[Resource]:
        resources: list[Resource] = []
        for release in self.list_releases():
            if release.get("tag_name") != tag:
                continue
            release_id = release.get("id")
            if not isinstance(release_id, int) or isinstance(release_id, bool):
                raise CleanupError("GitHub Release ID is malformed")
            resources.append(
                Resource(
                    "github-release",
                    f"{tag}#{release_id}",
                    release_id,
                    holds_manifest=self._manifest_asset(release) is not None,
                )
            )
        return sorted(resources, key=lambda item: int(item.identifier or 0))


def _boolean(value: str) -> bool:
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _summary_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _add_remote_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="GitHub owner/repository; defaults to GITHUB_REPOSITORY",
    )
    parser.add_argument("--crane", default="crane")
    parser.add_argument(
        "--api-base", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument("--fail-resource")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    tag = commands.add_parser("tag", help="clean one exact Tag")
    tag.add_argument("--tag", required=True)
    _add_remote_arguments(tag)

    retention = commands.add_parser(
        "retention", help="clean oldest excess same-type Tags"
    )
    retention.add_argument("--current-tag", required=True)
    retention.add_argument(
        "--release-type", choices=sorted(RELEASE_TYPES), required=True
    )
    retention.add_argument("--max-count", type=int, required=True)
    retention.add_argument("--pypi-enabled", type=_boolean, required=True)
    _add_remote_arguments(retention)
    return parser


def _production_remote(arguments: argparse.Namespace) -> ProductionRemote:
    repository = arguments.repository
    if not isinstance(repository, str) or not repository:
        raise CleanupError("--repository or GITHUB_REPOSITORY is required")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    return ProductionRemote(
        repository,
        token,
        crane=arguments.crane,
        api_base=arguments.api_base,
    )


def _run_tag(
    arguments: argparse.Namespace, remote: ProductionRemote
) -> list[ResourceFailure]:
    manifest = remote.load_manifest_for_tag(arguments.tag)
    report = cleanup_manifest(
        manifest,
        remote,
        fail_resource=arguments.fail_resource,
    )
    if report.completed:
        print(f"cleanup complete: {report.tag}")
    return list(report.failures)


def _run_retention(
    arguments: argparse.Namespace, remote: ProductionRemote
) -> list[ResourceFailure]:
    skip = select_retention_candidates(
        [],
        current_tag=arguments.current_tag,
        release_type=arguments.release_type,
        max_count=arguments.max_count,
        pypi_enabled=arguments.pypi_enabled,
    )
    if skip.skipped_reason is not None:
        print(skip.skipped_reason)
        return []
    selection = select_retention_candidates(
        remote.list_manifest_records(),
        current_tag=arguments.current_tag,
        release_type=arguments.release_type,
        max_count=arguments.max_count,
        pypi_enabled=arguments.pypi_enabled,
    )
    failures: list[ResourceFailure] = []
    for candidate in selection.candidates:
        report = cleanup_manifest(
            candidate.manifest,
            remote,
            fail_resource=arguments.fail_resource,
        )
        failures.extend(report.failures)
        if report.completed:
            print(f"retention cleanup complete: {report.tag}")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        remote = _production_remote(arguments)
        failures = (
            _run_tag(arguments, remote)
            if arguments.command == "tag"
            else _run_retention(arguments, remote)
        )
        append_failure_summary(_summary_path(arguments.summary), failures)
        if failures:
            for failure in failures:
                print(
                    f"{failure.resource.kind} {failure.resource.reference}: "
                    f"{failure.final_error}",
                    file=sys.stderr,
                )
            return 1
        return 0
    except CleanupError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
