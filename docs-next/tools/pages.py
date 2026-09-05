#!/usr/bin/env python3
"""Publish the versioned UCM documentation site to ``gh-pages``.

This module is the only supported writer for the Pages branch.  Mike creates
the versioned documentation commits without pushing; this module then freezes
the public release manifest, preserves historical PEP 503 indexes and the
existing CNAME, and performs one ordinary push.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

DOCS_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = DOCS_ROOT.parent
PAGES_BRANCH = "gh-pages"
RELEASE_MANIFEST_FILENAME = "release-manifest.json"
RELEASE_MANIFEST_KIND = "ucm-release-manifest"
RELEASE_MANIFEST_SCHEMA_VERSION = 8
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PATH_COMPONENT = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PagesError(RuntimeError):
    """Raised when Pages input or branch state violates the publication contract."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path = REPOSITORY_ROOT,
    env: Mapping[str, str] | None = None,
    capture_output: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=merged_env,
        text=True,
        capture_output=capture_output,
        check=check,
    )


def _git(*arguments: str, capture_output: bool = True) -> str:
    completed = _run(
        ["git", *arguments], capture_output=capture_output, cwd=REPOSITORY_ROOT
    )
    return completed.stdout.strip() if capture_output else ""


def _parse_repository(repository: str) -> tuple[str, str]:
    if _REPOSITORY.fullmatch(repository) is None:
        raise PagesError("repository must use the OWNER/REPO form")
    owner, name = repository.split("/", 1)
    return owner, name


def _validate_repository(repository: str) -> tuple[str, str]:
    owner, name = _parse_repository(repository)
    event_repository = os.environ.get("GITHUB_REPOSITORY")
    if event_repository and event_repository.lower() != repository.lower():
        raise PagesError(
            f"repository {repository!r} differs from GITHUB_REPOSITORY "
            f"{event_repository!r}"
        )
    return owner, name


def _prepare_pages_branch(repository: str) -> None:
    _validate_repository(repository)
    current = _run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
    )
    current_branch = current.stdout.strip() if current.returncode == 0 else ""
    if current_branch == PAGES_BRANCH:
        raise PagesError("run Pages publication from a source branch, not gh-pages")
    try:
        _run(
            [
                "git",
                "fetch",
                "origin",
                "+refs/heads/gh-pages:refs/remotes/origin/gh-pages",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise PagesError(
            "origin/gh-pages is unavailable; initialize the Pages branch first"
            + (f": {detail}" if detail else "")
        ) from error
    remote_head = _git("rev-parse", "refs/remotes/origin/gh-pages")
    _git("update-ref", "refs/heads/gh-pages", remote_head)


def _read_branch_file(path: str) -> str | None:
    completed = _run(
        ["git", "show", f"{PAGES_BRANCH}:{path}"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    if completed.returncode in {1, 128}:
        return None
    raise PagesError(f"unable to read {path!r} from {PAGES_BRANCH}")


def _site_url(repository: str, cname: str | None) -> str:
    owner, name = _parse_repository(repository)
    pages_host = f"{owner.lower()}.github.io"
    custom_domain = (cname or "").strip()
    if custom_domain:
        if "://" in custom_domain or "/" in custom_domain:
            raise PagesError("CNAME must contain one bare host name")
        return f"https://{custom_domain}/"
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{pages_host}/"
    return f"https://{pages_host}/{name}/"


def _mike(arguments: Sequence[str], *, site_url: str) -> None:
    executable = shutil.which("mike") or "mike"
    _run(
        [executable, *arguments],
        cwd=DOCS_ROOT,
        env={
            "UCM_DOCS_SITE_URL": site_url,
            "GIT_COMMITTER_NAME": BOT_NAME,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
        },
    )


@contextmanager
def _pages_worktree(message: str) -> Iterator[Path]:
    """Yield a detached worktree and advance gh-pages to its optional commit."""

    base = _git("rev-parse", PAGES_BRANCH)
    new_head = base
    with tempfile.TemporaryDirectory(prefix="ucm-pages-") as temporary_root:
        worktree = Path(temporary_root) / "tree"
        _run(
            ["git", "worktree", "add", "--detach", str(worktree), base],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
        )
        try:
            yield worktree
            status = _run(
                ["git", "status", "--porcelain"],
                cwd=worktree,
                capture_output=True,
            ).stdout
            if status.strip():
                _run(["git", "add", "--all"], cwd=worktree)
                _run(
                    [
                        "git",
                        "-c",
                        f"user.name={BOT_NAME}",
                        "-c",
                        f"user.email={BOT_EMAIL}",
                        "commit",
                        "--message",
                        message,
                    ],
                    cwd=worktree,
                    capture_output=True,
                )
                new_head = _run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    capture_output=True,
                ).stdout.strip()
        finally:
            _run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                check=False,
            )
            _run(
                ["git", "worktree", "prune"],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                check=False,
            )
    if new_head != base:
        _git("update-ref", "refs/heads/gh-pages", new_head, base)


def _push_pages_branch() -> None:
    _run(
        ["git", "push", "origin", "gh-pages:gh-pages"],
        cwd=REPOSITORY_ROOT,
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise PagesError(f"{context} fields differ; missing={missing}, extra={extra}")


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PagesError(f"{context} must be an object")
    return value


def _nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PagesError(f"{context} must be a non-empty string")
    return value


def _string_array(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise PagesError(f"{context} must be an array of non-empty strings")
    return value


def _sorted_string_array(value: object, context: str) -> list[str]:
    items = _string_array(value, context)
    if items != sorted(set(items)):
        raise PagesError(f"{context} must be sorted and unique")
    return items


def _https_url(value: object, context: str, *, host: str | None = None) -> str:
    url = _nonempty_string(value, context)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or (host and parsed.netloc != host)
    ):
        raise PagesError(f"{context} must be an HTTPS URL")
    return url


def _validate_wheel_filename(
    wheel: Mapping[str, Any], platform_tags: Sequence[str], context: str
) -> None:
    try:
        distribution, version, build, tags = parse_wheel_filename(wheel["filename"])
    except ValueError as error:
        raise PagesError(f"{context} filename is not a valid Wheel") from error
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
        raise PagesError(f"{context} filename and platform identity differ")


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
        raise PagesError(f"{context} multi_arch must be a boolean")
    members = publication.get("members")
    if not isinstance(members, list) or not members:
        raise PagesError(f"{context} members must be a non-empty array")
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
            raise PagesError(f"{context} members must be unique")
        architectures.add(architecture)
        references.add(reference)
    if multi_arch and len(members) < 2:
        raise PagesError(f"{context} multi_arch requires at least two members")
    if not multi_arch and (len(members) != 1 or members[0]["reference"] != pull):
        raise PagesError(
            f"{context} single-architecture pull must equal its only member"
        )
    return publication


def validate_manifest(value: object) -> dict[str, Any]:
    """Validate the exact public Schema 8 Pages contract."""

    manifest = _mapping(value, "release manifest")
    if manifest.get("kind") != RELEASE_MANIFEST_KIND:
        raise PagesError(f"release manifest kind must be {RELEASE_MANIFEST_KIND}")
    schema_version = manifest.get("schema_version")
    if schema_version != RELEASE_MANIFEST_SCHEMA_VERSION:
        raise PagesError(
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
        raise PagesError("release manifest release type is invalid")
    if _PATH_COMPONENT.fullmatch(release["version"]) is None:
        raise PagesError("release manifest release version is not path-safe")
    actions_run_id = release.get("actions_run_id")
    if (
        not isinstance(actions_run_id, int)
        or isinstance(actions_run_id, bool)
        or actions_run_id < 1
    ):
        raise PagesError("release manifest actions_run_id must be a positive integer")

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
        raise PagesError("release manifest Python package must match the release")
    python_filename = python_package["filename"]
    if Path(python_filename).name != python_filename:
        raise PagesError("release manifest Python filename must be a filename")
    if _SHA256.fullmatch(python_package["sha256"]) is None:
        raise PagesError("release manifest Python sha256 is invalid")
    _sorted_string_array(python_package.get("tags"), "release manifest Python tags")
    raw_extras = _mapping(
        python_package.get("extras"), "release manifest Python extras"
    )
    if not raw_extras:
        raise PagesError("release manifest Python extras must not be empty")
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
            raise PagesError("release manifest Python extras are invalid")
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
            raise PagesError("release manifest Python PyPI host is invalid")
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
            raise PagesError("release manifest PyPI URLs differ from the release")

    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        raise PagesError("release manifest wheels must be an array")
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
            raise PagesError("release manifest Wheel IDs and filenames must be unique")
        wheel_ids.add(wheel_id)
        wheel_filenames.add(filename)
        _accelerator(wheel.get("accelerator"), f"{context} accelerator")
        if _PATH_COMPONENT.fullmatch(wheel["extra"]) is None:
            raise PagesError("Wheel extra is not path-safe")
        if (
            python_extras.get(wheel["extra"]) != wheel["distribution"]
            or wheel["version"] != manifest["python"]["version"]
        ):
            raise PagesError("Wheel does not match its declared Python extra")
        platform_tags = _sorted_string_array(
            wheel.get("platform_tags"), f"{context} platform_tags"
        )
        if not platform_tags:
            raise PagesError("Wheel platform tags must not be empty")
        _validate_wheel_filename(wheel, platform_tags, context)
        wheel_extras.add(wheel["extra"])
        if Path(filename).name != filename:
            raise PagesError("Wheel filename must not contain a path")
        if _SHA256.fullmatch(wheel["sha256"]) is None:
            raise PagesError("Wheel sha256 must contain 64 lowercase hex digits")
        dependencies = _string_array(
            wheel.get("dependencies"), f"{context} dependencies"
        )
        if dependencies != sorted(set(dependencies)):
            raise PagesError(f"{context} dependencies must be sorted and unique")
    if wheel_extras != set(python_extras):
        raise PagesError("release manifest Wheels must publish every Python extra")

    images = manifest.get("images")
    if not isinstance(images, list):
        raise PagesError("release manifest images must be an array")
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
            raise PagesError("release manifest Image IDs must be unique")
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
            raise PagesError(f"{context} must have at least one publication")

    chart = _mapping(manifest.get("chart"), "release manifest chart")
    _exact_keys(
        chart, {"name", "version", "filename", "url", "oci"}, "release manifest chart"
    )
    for field in ("name", "version", "filename", "url"):
        _nonempty_string(chart.get(field), f"release manifest chart {field}")
    chart_filename = chart["filename"]
    if Path(chart_filename).name != chart_filename:
        raise PagesError("release manifest Chart filename must not contain a path")
    if chart_filename in wheel_filenames:
        raise PagesError("release manifest Chart and Wheel files must be unique")
    if python_filename in wheel_filenames:
        raise PagesError("release manifest Python and Wheel files must be unique")
    if chart_filename == python_filename:
        raise PagesError("release manifest Python and Chart files must be unique")
    if chart.get("oci") is not None:
        _nonempty_string(chart.get("oci"), "release manifest chart oci")

    assets = _string_array(
        manifest.get("github_release_assets"),
        "release manifest github_release_assets",
    )
    if len(assets) != len(set(assets)):
        raise PagesError("release manifest github_release_assets must be unique")
    if RELEASE_MANIFEST_FILENAME not in assets:
        raise PagesError("release manifest must list itself as a GitHub Release asset")
    if "install-catalog.json" in assets:
        raise PagesError("release manifest must not list install-catalog.json")
    if (manifest["python"]["pypi"] is not None) != ("pypi-receipt.json" in assets):
        raise PagesError("release manifest PyPI receipt asset differs from publication")
    required_assets = {chart_filename} | wheel_filenames
    required_assets.add(python_filename)
    missing_assets = sorted(required_assets - set(assets))
    if missing_assets:
        raise PagesError(f"release manifest assets are missing {missing_assets}")
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PagesError(f"unable to read release manifest {path}: {error}") from error
    return validate_manifest(value)


def require_stable_manifest(manifest: Mapping[str, Any]) -> None:
    release = _mapping(manifest.get("release"), "release manifest release")
    if release.get("type") != "stable":
        raise PagesError("Stable Manifest release type must be stable")
    version_text = _nonempty_string(release.get("version"), "Stable version")
    tag = _nonempty_string(release.get("tag"), "Stable Tag")
    try:
        version = Version(version_text)
    except InvalidVersion as error:
        raise PagesError(
            f"Stable Manifest has invalid PEP 440 version {version_text!r}"
        ) from error
    if tag != f"v{version_text}":
        raise PagesError("Stable Manifest Tag must equal 'v' plus the public version")
    if version.is_prerelease or version.is_devrelease or version.local is not None:
        raise PagesError(
            "Stable Manifest version must not contain pre/dev/local segments"
        )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _load_versions(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PagesError(f"invalid Mike versions file {path}") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PagesError(f"invalid Mike versions file {path}")
    return value


def _branch_versions() -> list[dict[str, Any]]:
    raw = _read_branch_file("versions.json")
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PagesError("gh-pages versions.json is invalid") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PagesError("gh-pages versions.json is invalid")
    return value


def _branch_stable_manifest() -> tuple[str, dict[str, Any]] | None:
    current_stable = stable_version(_branch_versions())
    if current_stable is None:
        print("[pages] no stable alias; skipping latest publication")
        return None
    path = f"manifests/{current_stable}/{RELEASE_MANIFEST_FILENAME}"
    raw = _read_branch_file(path)
    if raw is None:
        print(
            f"[pages] stable alias {current_stable} has no frozen release "
            "Manifest; skipping latest publication"
        )
        return None
    try:
        manifest = validate_manifest(json.loads(raw))
    except json.JSONDecodeError as error:
        raise PagesError(f"gh-pages {path} is invalid JSON") from error
    if manifest["release"]["version"] != current_stable:
        raise PagesError(f"gh-pages frozen Manifest path does not match {path}")
    return current_stable, manifest


def version_exists(versions: Sequence[Mapping[str, Any]], version: str) -> bool:
    return any(item.get("version") == version for item in versions)


def stable_version(versions: Sequence[Mapping[str, Any]]) -> str | None:
    matches = [
        item.get("version")
        for item in versions
        if isinstance(item.get("aliases"), list) and "stable" in item["aliases"]
    ]
    if len(matches) > 1 or any(not isinstance(item, str) for item in matches):
        raise PagesError("Mike versions.json has an ambiguous stable alias")
    return matches[0] if matches else None


def _remove_legacy_catalogs(pages_root: Path) -> None:
    """Remove every obsolete public Catalog location from the Pages tree."""

    _remove_path(pages_root / "catalogs")
    for path in pages_root.glob("*/install-catalog.json"):
        _remove_path(path)


def inject_latest_manifest(pages_root: Path) -> str | None:
    """Copy the frozen current Stable Manifest into the mutable latest version."""

    latest_root = pages_root / "latest"
    if not latest_root.is_dir():
        print("[pages] latest documentation does not exist; skipping Manifest sync")
        return None
    destination = latest_root / RELEASE_MANIFEST_FILENAME
    if destination.exists():
        destination.unlink()
    current_stable = stable_version(_load_versions(pages_root / "versions.json"))
    if current_stable is None:
        print("[pages] no stable alias; latest will show Manifest unavailable")
        return None
    source = pages_root / "manifests" / current_stable / RELEASE_MANIFEST_FILENAME
    if not source.is_file():
        print(
            f"[pages] stable alias {current_stable} has no frozen Manifest; "
            "latest will show Manifest unavailable"
        )
        return None
    manifest = load_manifest(source)
    _write_json(destination, manifest)
    return current_stable


def _freeze_stable_manifest(pages_root: Path, manifest: dict[str, Any]) -> None:
    version = manifest["release"]["version"]
    _write_json(
        pages_root / "manifests" / version / RELEASE_MANIFEST_FILENAME,
        manifest,
    )
    _write_json(pages_root / version / RELEASE_MANIFEST_FILENAME, manifest)


def _assert_cname_preserved(original: str | None) -> None:
    if _read_branch_file("CNAME") != original:
        raise PagesError("Pages publication changed the existing CNAME")


def publish_latest(repository: str) -> None:
    _prepare_pages_branch(repository)
    if _branch_stable_manifest() is None:
        return
    cname = _read_branch_file("CNAME")
    _mike(
        [
            "deploy",
            "latest",
            "--update-aliases",
            "--branch",
            PAGES_BRANCH,
            "--remote",
            "origin",
        ],
        site_url=_site_url(repository, cname),
    )
    with _pages_worktree("Publish latest release Manifest") as worktree:
        _remove_legacy_catalogs(worktree)
        inject_latest_manifest(worktree)
    _assert_cname_preserved(cname)
    _push_pages_branch()


def publish_stable(
    repository: str,
    manifest_path: Path,
    *,
    replace_existing: bool = False,
) -> None:
    manifest = load_manifest(manifest_path)
    require_stable_manifest(manifest)
    version = manifest["release"]["version"]
    _prepare_pages_branch(repository)
    cname = _read_branch_file("CNAME")
    exists = version_exists(_branch_versions(), version)
    if replace_existing and not exists:
        raise PagesError("--replace-existing requires an existing Stable version")
    if not exists or replace_existing:
        site_url = _site_url(repository, cname)
        _mike(
            [
                "deploy",
                version,
                "stable",
                "--update-aliases",
                "--alias-type",
                "redirect",
                "--branch",
                PAGES_BRANCH,
                "--remote",
                "origin",
            ],
            site_url=site_url,
        )
        _mike(
            [
                "set-default",
                "stable",
                "--branch",
                PAGES_BRANCH,
                "--remote",
                "origin",
            ],
            site_url=site_url,
        )
    else:
        print(f"[pages] Stable {version} already exists; skipping documentation body")
    with _pages_worktree(f"Freeze release Manifest for {version}") as worktree:
        _remove_legacy_catalogs(worktree)
        _freeze_stable_manifest(worktree, manifest)
        inject_latest_manifest(worktree)
    _assert_cname_preserved(cname)
    _push_pages_branch()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def initialize(repository: str) -> None:
    """Create one clean baseline commit while preserving CNAME and history."""

    _prepare_pages_branch(repository)
    cname = _read_branch_file("CNAME")
    with _pages_worktree("Initialize clean GitHub Pages baseline") as worktree:
        for child in worktree.iterdir():
            if child.name in {".git", "CNAME"}:
                continue
            _remove_path(child)
        if cname is not None:
            (worktree / "CNAME").write_text(cname, encoding="utf-8")
        (worktree / ".nojekyll").write_text("", encoding="utf-8")
    _assert_cname_preserved(cname)
    _push_pages_branch()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("initialize", "publish-latest"):
        command = commands.add_parser(name)
        command.add_argument("--repository", required=True)
    stable = commands.add_parser("publish-stable")
    stable.add_argument("--repository", required=True)
    stable.add_argument("--manifest", type=Path, required=True)
    stable.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace one already-published Stable documentation body",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "initialize":
            initialize(arguments.repository)
        elif arguments.command == "publish-latest":
            publish_latest(arguments.repository)
        else:
            publish_stable(
                arguments.repository,
                arguments.manifest,
                replace_existing=arguments.replace_existing,
            )
    except (PagesError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
