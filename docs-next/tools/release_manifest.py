"""Read and validate the published installation manifest for one documentation build."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

RELEASE_MANIFEST_FILENAME = "release-manifest.json"
RELEASE_MANIFEST_KIND = "ucm-release-manifest"
RELEASE_MANIFEST_SCHEMA_VERSION = 8
_PATH_COMPONENT = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ManifestError(RuntimeError):
    """A published installation manifest is invalid or cannot be read."""


class ReleasePending(ManifestError):
    """The tag exists before its release publication has completed."""


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ManifestError(
            f"{context} fields differ; missing={missing}, extra={extra}"
        )


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{context} must be an object")
    return value


def _nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ManifestError(f"{context} must be a non-empty string")
    return value


def _string_array(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ManifestError(f"{context} must be an array of non-empty strings")
    return value


def _sorted_string_array(value: object, context: str) -> list[str]:
    items = _string_array(value, context)
    if items != sorted(set(items)):
        raise ManifestError(f"{context} must be sorted and unique")
    return items


def _https_url(value: object, context: str, *, host: str | None = None) -> str:
    url = _nonempty_string(value, context)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or (host and parsed.netloc != host)
    ):
        raise ManifestError(f"{context} must be an HTTPS URL")
    return url


def _validate_wheel_filename(
    wheel: Mapping[str, Any], platform_tags: Sequence[str], context: str
) -> None:
    try:
        distribution, version, build, tags = parse_wheel_filename(wheel["filename"])
    except ValueError as error:
        raise ManifestError(f"{context} filename is not a valid Wheel") from error
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
        raise ManifestError(f"{context} filename and platform identity differ")


def _accelerator(value: object, context: str) -> dict[str, Any]:
    accelerator = _mapping(value, context)
    _exact_keys(accelerator, {"runtime", "variant", "soc_version"}, context)
    for field in ("runtime", "variant", "soc_version"):
        _nonempty_string(accelerator.get(field), f"{context} {field}")
    return accelerator


def _publication(value: object, context: str) -> dict[str, Any] | None:
    if value is None:
        return None
    publication = _mapping(value, context)
    _exact_keys(publication, {"pull", "multi_arch", "members"}, context)
    pull = _nonempty_string(publication.get("pull"), f"{context} pull")
    multi_arch = publication.get("multi_arch")
    if not isinstance(multi_arch, bool):
        raise ManifestError(f"{context} multi_arch must be a boolean")
    members = publication.get("members")
    if not isinstance(members, list) or not members:
        raise ManifestError(f"{context} members must be a non-empty array")
    architectures: set[str] = set()
    references: set[str] = set()
    for index, raw_member in enumerate(members):
        member_context = f"{context} members[{index}]"
        member = _mapping(raw_member, member_context)
        _exact_keys(member, {"architecture", "reference"}, member_context)
        architecture = _nonempty_string(
            member.get("architecture"), f"{member_context} architecture"
        )
        reference = _nonempty_string(
            member.get("reference"), f"{member_context} reference"
        )
        if architecture in architectures or reference in references:
            raise ManifestError(f"{context} members must be unique")
        architectures.add(architecture)
        references.add(reference)
    if multi_arch and len(members) < 2:
        raise ManifestError(f"{context} multi_arch requires at least two members")
    if not multi_arch and (len(members) != 1 or members[0]["reference"] != pull):
        raise ManifestError(
            f"{context} single-architecture pull must equal its only member"
        )
    return publication


def validate_manifest(value: object) -> dict[str, Any]:
    """Validate the exact public Schema 8 installation contract."""

    manifest = _mapping(value, "release manifest")
    if manifest.get("kind") != RELEASE_MANIFEST_KIND:
        raise ManifestError(f"release manifest kind must be {RELEASE_MANIFEST_KIND}")
    schema_version = manifest.get("schema_version")
    if schema_version != RELEASE_MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            "release manifest schema_version must be "
            f"{RELEASE_MANIFEST_SCHEMA_VERSION}"
        )
    _exact_keys(
        manifest,
        {
            "kind",
            "schema_version",
            "release",
            "python",
            "wheels",
            "images",
            "chart",
            "github_release_assets",
        },
        "release manifest",
    )

    release = _mapping(manifest.get("release"), "release manifest release")
    _exact_keys(
        release,
        {"tag", "type", "version", "url", "actions_run_id"},
        "release manifest release",
    )
    for field in ("tag", "type", "version", "url"):
        _nonempty_string(release.get(field), f"release manifest release {field}")
    if release["type"] not in {"stable", "prerelease", "draft", "nightly"}:
        raise ManifestError("release manifest release type is invalid")
    if _PATH_COMPONENT.fullmatch(release["version"]) is None:
        raise ManifestError("release manifest release version is not path-safe")
    actions_run_id = release.get("actions_run_id")
    if (
        not isinstance(actions_run_id, int)
        or isinstance(actions_run_id, bool)
        or actions_run_id < 1
    ):
        raise ManifestError(
            "release manifest actions_run_id must be a positive integer"
        )

    python_package = _mapping(manifest.get("python"), "release manifest python")
    _exact_keys(
        python_package,
        {
            "distribution",
            "version",
            "filename",
            "url",
            "sha256",
            "tags",
            "extras",
            "pypi",
        },
        "release manifest python",
    )
    for field in ("distribution", "version", "filename", "url", "sha256"):
        _nonempty_string(python_package.get(field), f"release manifest python {field}")
    meta_distribution = python_package["distribution"]
    if (
        re.fullmatch(r"(?:[a-z0-9]+-)*uc-manager", meta_distribution) is None
        or python_package["version"] != release["version"]
    ):
        raise ManifestError("release manifest Python package must match the release")
    python_filename = python_package["filename"]
    if Path(python_filename).name != python_filename:
        raise ManifestError("release manifest Python filename must be a filename")
    if _SHA256.fullmatch(python_package["sha256"]) is None:
        raise ManifestError("release manifest Python sha256 is invalid")
    _sorted_string_array(python_package.get("tags"), "release manifest Python tags")
    raw_extras = _mapping(
        python_package.get("extras"), "release manifest Python extras"
    )
    if not raw_extras:
        raise ManifestError("release manifest Python extras must not be empty")
    python_extras: dict[str, str] = {}
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
            raise ManifestError("release manifest Python extras are invalid")
        distributions.add(distribution)
        python_extras[extra] = distribution
    pypi = python_package.get("pypi")
    if pypi is not None:
        pypi = _mapping(pypi, "release manifest Python PyPI")
        _exact_keys(
            pypi,
            {"index_url", "project_url"},
            "release manifest Python PyPI",
        )
        _https_url(
            pypi.get("index_url"),
            "release manifest Python PyPI index",
        )
        pypi_host = urlparse(pypi["index_url"]).netloc
        if pypi_host not in {"pypi.org", "test.pypi.org"}:
            raise ManifestError("release manifest Python PyPI host is invalid")
        _https_url(
            pypi.get("project_url"),
            "release manifest Python PyPI project",
            host=pypi_host,
        )
        if (
            pypi["index_url"] != f"https://{pypi_host}/simple"
            or pypi["project_url"]
            != f"https://{pypi_host}/project/{meta_distribution}/"
            f"{quote(release['version'], safe='')}/"
        ):
            raise ManifestError("release manifest PyPI URLs differ from the release")

    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        raise ManifestError("release manifest wheels must be an array")
    wheel_keys = {
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
    wheel_ids: set[str] = set()
    wheel_filenames: set[str] = set()
    wheel_extras: set[str] = set()
    for index, raw_wheel in enumerate(wheels):
        context = f"release manifest wheels[{index}]"
        wheel = _mapping(raw_wheel, context)
        _exact_keys(wheel, wheel_keys, context)
        for field in wheel_keys - {"accelerator", "dependencies", "platform_tags"}:
            _nonempty_string(wheel.get(field), f"{context} {field}")
        wheel_id = wheel["id"]
        filename = wheel["filename"]
        if wheel_id in wheel_ids or filename in wheel_filenames:
            raise ManifestError(
                "release manifest Wheel IDs and filenames must be unique"
            )
        wheel_ids.add(wheel_id)
        wheel_filenames.add(filename)
        _accelerator(wheel.get("accelerator"), f"{context} accelerator")
        if _PATH_COMPONENT.fullmatch(wheel["extra"]) is None:
            raise ManifestError("Wheel extra is not path-safe")
        if (
            python_extras.get(wheel["extra"]) != wheel["distribution"]
            or wheel["version"] != manifest["python"]["version"]
        ):
            raise ManifestError("Wheel does not match its declared Python extra")
        platform_tags = _sorted_string_array(
            wheel.get("platform_tags"), f"{context} platform_tags"
        )
        if not platform_tags:
            raise ManifestError("Wheel platform tags must not be empty")
        _validate_wheel_filename(wheel, platform_tags, context)
        wheel_extras.add(wheel["extra"])
        if Path(filename).name != filename:
            raise ManifestError("Wheel filename must not contain a path")
        if _SHA256.fullmatch(wheel["sha256"]) is None:
            raise ManifestError("Wheel sha256 must contain 64 lowercase hex digits")
        dependencies = _string_array(
            wheel.get("dependencies"), f"{context} dependencies"
        )
        if dependencies != sorted(set(dependencies)):
            raise ManifestError(f"{context} dependencies must be sorted and unique")
    if wheel_extras != set(python_extras):
        raise ManifestError("release manifest Wheels must publish every Python extra")

    images = manifest.get("images")
    if not isinstance(images, list):
        raise ManifestError("release manifest images must be an array")
    image_ids: set[str] = set()
    for index, raw_image in enumerate(images):
        context = f"release manifest images[{index}]"
        image = _mapping(raw_image, context)
        _exact_keys(
            image,
            {"id", "product", "upstream", "accelerator", "os", "publications"},
            context,
        )
        image_id = _nonempty_string(image.get("id"), f"{context} id")
        _nonempty_string(image.get("product"), f"{context} product")
        if image_id in image_ids:
            raise ManifestError("release manifest Image IDs must be unique")
        image_ids.add(image_id)
        upstream = _mapping(image.get("upstream"), f"{context} upstream")
        _exact_keys(upstream, {"version", "channel"}, f"{context} upstream")
        for field in ("version", "channel"):
            _nonempty_string(upstream.get(field), f"{context} upstream {field}")
        _accelerator(image.get("accelerator"), f"{context} accelerator")
        operating_system = _mapping(image.get("os"), f"{context} os")
        _exact_keys(operating_system, {"id", "version"}, f"{context} os")
        for field in ("id", "version"):
            _nonempty_string(operating_system.get(field), f"{context} os {field}")
        publications = _mapping(image.get("publications"), f"{context} publications")
        _exact_keys(publications, {"ghcr", "dockerhub"}, f"{context} publications")
        published = [
            _publication(publications.get(channel), f"{context} publications {channel}")
            for channel in ("ghcr", "dockerhub")
        ]
        if all(publication is None for publication in published):
            raise ManifestError(f"{context} must have at least one publication")

    chart = _mapping(manifest.get("chart"), "release manifest chart")
    _exact_keys(
        chart, {"name", "version", "filename", "url", "oci"}, "release manifest chart"
    )
    for field in ("name", "version", "filename", "url"):
        _nonempty_string(chart.get(field), f"release manifest chart {field}")
    chart_filename = chart["filename"]
    if Path(chart_filename).name != chart_filename:
        raise ManifestError("release manifest Chart filename must not contain a path")
    if chart_filename in wheel_filenames:
        raise ManifestError("release manifest Chart and Wheel files must be unique")
    if python_filename in wheel_filenames:
        raise ManifestError("release manifest Python and Wheel files must be unique")
    if chart_filename == python_filename:
        raise ManifestError("release manifest Python and Chart files must be unique")
    if chart.get("oci") is not None:
        _nonempty_string(chart.get("oci"), "release manifest chart oci")

    assets = _string_array(
        manifest.get("github_release_assets"),
        "release manifest github_release_assets",
    )
    if len(assets) != len(set(assets)):
        raise ManifestError("release manifest github_release_assets must be unique")
    if RELEASE_MANIFEST_FILENAME not in assets:
        raise ManifestError(
            "release manifest must list itself as a GitHub Release asset"
        )
    if "install-catalog.json" in assets:
        raise ManifestError("release manifest must not list install-catalog.json")
    if (manifest["python"]["pypi"] is not None) != ("pypi-receipt.json" in assets):
        raise ManifestError(
            "release manifest PyPI receipt asset differs from publication"
        )
    required_assets = {chart_filename} | wheel_filenames
    required_assets.add(python_filename)
    missing_assets = sorted(required_assets - set(assets))
    if missing_assets:
        raise ManifestError(f"release manifest assets are missing {missing_assets}")
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(
            f"unable to read release manifest {path}: {error}"
        ) from error
    return validate_manifest(value)


def require_stable_manifest(manifest: Mapping[str, Any]) -> None:
    release = _mapping(manifest.get("release"), "release manifest release")
    if release.get("type") != "stable":
        raise ManifestError("Stable Manifest release type must be stable")
    version_text = _nonempty_string(release.get("version"), "Stable version")
    tag = _nonempty_string(release.get("tag"), "Stable Tag")
    try:
        version = Version(version_text)
    except InvalidVersion as error:
        raise ManifestError(
            f"Stable Manifest has invalid PEP 440 version {version_text!r}"
        ) from error
    if tag != f"v{version_text}":
        raise ManifestError(
            "Stable Manifest Tag must equal 'v' plus the public version"
        )
    if version.is_prerelease or version.is_devrelease or version.local is not None:
        raise ManifestError(
            "Stable Manifest version must not contain pre/dev/local segments"
        )


def fetch_json(url: str) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ucm-docs"}
    if urlparse(url).netloc == "api.github.com" and os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError:
        raise
    except (OSError, ValueError) as error:
        raise ManifestError(f"Unable to read {url}: {error}") from error


def _release_manifest(
    repository: str, release: Mapping[str, Any]
) -> dict[str, Any] | None:
    tag = release["tag_name"]
    assets = [
        a
        for a in release.get("assets", [])
        if a.get("name") == RELEASE_MANIFEST_FILENAME
    ]
    if not assets:
        return None
    if len(assets) != 1:
        raise ManifestError(f"Release {tag} has multiple installation manifests")
    value = fetch_json(assets[0]["browser_download_url"])
    if not isinstance(value, dict):
        raise ManifestError(f"Release {tag} manifest must be an object")
    # Earlier releases remain readable as historical documentation, but their
    # cleanup-only manifests cannot drive the installation selector.
    if value.get("schema_version") in {6, 7}:
        return None
    manifest = validate_manifest(value)
    if manifest["release"]["tag"] != tag:
        raise ManifestError(f"Release {tag} has a manifest for another tag")
    if tag != f"v{manifest['release']['version']}":
        raise ManifestError(f"Release {tag} tag and package version differ")
    expected_type = "prerelease" if release.get("prerelease") else "stable"
    if manifest["release"]["type"] != expected_type:
        raise ManifestError(f"Release {tag} publication type differs from its manifest")
    expected = f"https://github.com/{repository}/releases/tag/{quote(tag, safe='')}"
    if manifest["release"]["url"].casefold() != expected.casefold():
        raise ManifestError(f"Release {tag} manifest belongs to another repository")
    published_assets = {a.get("name") for a in release.get("assets", [])}
    if set(manifest["github_release_assets"]) != published_assets:
        raise ManifestError(f"Release {tag} assets differ from its completed manifest")
    urls = {a["name"]: a["browser_download_url"] for a in release["assets"]}
    for artifact in [manifest["python"], *manifest["wheels"], manifest["chart"]]:
        if artifact["url"] != urls[artifact["filename"]]:
            raise ManifestError(
                f"Release {tag} download URL differs for {artifact['filename']}"
            )
    return manifest


def resolve_manifest(
    repository: str, *, tag: str | None = None
) -> dict[str, Any] | None:
    """Resolve an exact release, or the highest completed Schema 8 stable release."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ManifestError("repository must be OWNER/REPO")
    base = f"https://api.github.com/repos/{repository}/releases"
    if tag:
        try:
            release = fetch_json(f"{base}/tags/{quote(tag, safe='')}")
        except HTTPError as error:
            if error.code == 404:
                raise ReleasePending(f"Release {tag} is not published yet") from error
            raise
        if release.get("draft"):
            raise ReleasePending(f"Release {tag} is still a draft")
        manifest = _release_manifest(repository, release)
        if manifest is None:
            raise ReleasePending(f"Release {tag} has no completed Schema 8 manifest")
        return manifest

    releases = []
    page = 1
    while True:
        batch = fetch_json(f"{base}?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ManifestError("GitHub releases response must be an array")
        for release in batch:
            if release.get("draft") or release.get("prerelease"):
                continue
            try:
                version = Version(release["tag_name"].removeprefix("v"))
            except (InvalidVersion, KeyError):
                continue
            if not (version.is_prerelease or version.is_devrelease or version.local):
                releases.append((version, release))
        if len(batch) < 100:
            break
        page += 1
    for _, release in sorted(releases, key=lambda item: item[0], reverse=True):
        manifest = _release_manifest(repository, release)
        if manifest is not None:
            require_stable_manifest(manifest)
            return manifest
    return None
