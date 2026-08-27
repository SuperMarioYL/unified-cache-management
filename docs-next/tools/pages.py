#!/usr/bin/env python3
"""Publish the versioned UCM documentation site to ``gh-pages``.

This module is the only supported writer for the Pages branch.  Mike creates
the versioned documentation commits without pushing; this module then freezes
the install catalog, rebuilds the cross-version PEP 503 indexes, preserves the
existing CNAME, and performs one ordinary push.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

DOCS_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = DOCS_ROOT.parent
PAGES_BRANCH = "gh-pages"
INSTALL_CATALOG_FILENAME = "install-catalog.json"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PATH_COMPONENT = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PagesError(RuntimeError):
    """Raised when Pages input or branch state violates the publication contract."""


@dataclass(frozen=True)
class PyPIWheel:
    filename: str
    url: str
    sha256: str


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
    if not isinstance(value, str) or not value:
        raise PagesError(f"{context} must be a non-empty string")
    return value


def validate_catalog(value: object) -> dict[str, Any]:
    catalog = _mapping(value, "install catalog")
    _exact_keys(
        catalog,
        {"kind", "schema_version", "release", "wheels", "images", "chart"},
        "install catalog",
    )
    if catalog.get("kind") != "ucm-install-catalog":
        raise PagesError("install catalog kind must be ucm-install-catalog")
    if catalog.get("schema_version") != 1:
        raise PagesError("install catalog schema_version must be 1")

    release = _mapping(catalog.get("release"), "install catalog release")
    _exact_keys(release, {"tag", "version", "url"}, "install catalog release")
    for field in ("tag", "version", "url"):
        _nonempty_string(release.get(field), f"install catalog release {field}")
    if _PATH_COMPONENT.fullmatch(release["version"]) is None:
        raise PagesError("install catalog release version is not path-safe")

    wheels = catalog.get("wheels")
    if not isinstance(wheels, list):
        raise PagesError("install catalog wheels must be an array")
    wheel_keys = {
        "channel",
        "distribution",
        "version",
        "python_abi",
        "cpu_arch",
        "filename",
        "url",
        "sha256",
        "dependencies",
    }
    for index, raw_wheel in enumerate(wheels):
        wheel = _mapping(raw_wheel, f"install catalog wheels[{index}]")
        _exact_keys(wheel, wheel_keys, f"install catalog wheels[{index}]")
        for field in wheel_keys - {"dependencies"}:
            _nonempty_string(
                wheel.get(field), f"install catalog wheels[{index}] {field}"
            )
        if wheel["distribution"] != "uc-manager":
            raise PagesError("Simple Index requires the uc-manager distribution")
        if _PATH_COMPONENT.fullmatch(wheel["channel"]) is None:
            raise PagesError("Wheel channel is not path-safe")
        if Path(wheel["filename"]).name != wheel["filename"]:
            raise PagesError("Wheel filename must not contain a path")
        if _SHA256.fullmatch(wheel["sha256"]) is None:
            raise PagesError("Wheel sha256 must contain 64 lowercase hex digits")
        dependencies = wheel.get("dependencies")
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item for item in dependencies
        ):
            raise PagesError("Wheel dependencies must be non-empty strings")

    images = catalog.get("images")
    if not isinstance(images, list):
        raise PagesError("install catalog images must be an array")
    image_keys = {
        "id",
        "product",
        "upstream_version",
        "upstream_channel",
        "accelerator_runtime",
        "variant",
        "soc_version",
        "os_id",
        "os_version",
        "architectures",
        "references",
    }
    for index, raw_image in enumerate(images):
        image = _mapping(raw_image, f"install catalog images[{index}]")
        _exact_keys(image, image_keys, f"install catalog images[{index}]")
        for field in image_keys - {"architectures", "references"}:
            _nonempty_string(
                image.get(field), f"install catalog images[{index}] {field}"
            )
        architectures = image.get("architectures")
        if not isinstance(architectures, list) or any(
            not isinstance(item, str) or not item for item in architectures
        ):
            raise PagesError("Image architectures must be strings")
        references = image.get("references")
        if not isinstance(references, dict) or any(
            not isinstance(channel, str)
            or not channel
            or not isinstance(reference, str)
            or not reference
            for channel, reference in references.items()
        ):
            raise PagesError("Image references must map channels to tagged references")

    chart = _mapping(catalog.get("chart"), "install catalog chart")
    _exact_keys(
        chart, {"name", "version", "filename", "url", "oci"}, "install catalog chart"
    )
    for field in ("name", "version", "filename", "url"):
        _nonempty_string(chart.get(field), f"install catalog chart {field}")
    if chart.get("oci") is not None and not isinstance(chart.get("oci"), str):
        raise PagesError("install catalog chart oci must be a string or null")
    return catalog


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PagesError(f"unable to read install catalog {path}: {error}") from error
    return validate_catalog(value)


def require_stable_catalog(catalog: Mapping[str, Any]) -> None:
    release = _mapping(catalog.get("release"), "install catalog release")
    version_text = _nonempty_string(release.get("version"), "Stable version")
    tag = _nonempty_string(release.get("tag"), "Stable Tag")
    try:
        version = Version(version_text)
    except InvalidVersion as error:
        raise PagesError(
            f"Stable Catalog has invalid PEP 440 version {version_text!r}"
        ) from error
    if tag != f"v{version_text}":
        raise PagesError("Stable Catalog Tag must equal 'v' plus the public version")
    if version.is_prerelease or version.is_devrelease or version.local is not None:
        raise PagesError(
            "Stable Catalog version must not contain pre/dev/local segments"
        )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def fetch_wrapt_wheels(version: str) -> list[PyPIWheel]:
    """Return the immutable bdist_wheel links for one exact wrapt version."""

    request = Request(
        f"https://pypi.org/pypi/wrapt/{quote(version, safe='')}/json",
        headers={"Accept": "application/json", "User-Agent": "ucm-pages/1"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as error:
        raise PagesError(
            f"unable to fetch wrapt=={version} metadata: {error}"
        ) from error
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        raise PagesError(f"PyPI metadata for wrapt=={version} has no urls array")
    result: list[PyPIWheel] = []
    for raw_file in urls:
        if (
            not isinstance(raw_file, dict)
            or raw_file.get("packagetype") != "bdist_wheel"
        ):
            continue
        filename = raw_file.get("filename")
        url = raw_file.get("url")
        digests = raw_file.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if (
            not isinstance(filename, str)
            or not filename.endswith(".whl")
            or Path(filename).name != filename
            or not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise PagesError(f"PyPI returned an invalid wrapt=={version} Wheel record")
        result.append(PyPIWheel(filename=filename, url=url, sha256=digest))
    if not result:
        raise PagesError(f"PyPI returned no bdist_wheel files for wrapt=={version}")
    return sorted(result, key=lambda item: item.filename)


def _exact_wrapt_versions(dependencies: Sequence[str]) -> set[str]:
    versions: set[str] = set()
    for declaration in dependencies:
        try:
            requirement = Requirement(declaration)
        except InvalidRequirement as error:
            raise PagesError(f"invalid Wheel dependency {declaration!r}") from error
        if canonicalize_name(requirement.name) != "wrapt":
            raise PagesError(
                f"Simple Index only mirrors wrapt; unsupported dependency {declaration!r}"
            )
        specifiers = list(requirement.specifier)
        if (
            requirement.extras
            or requirement.marker is not None
            or requirement.url is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise PagesError(f"wrapt dependency must be one exact pin: {declaration!r}")
        versions.add(specifiers[0].version)
    return versions


def _hash_link(url: str, digest: str) -> str:
    return f"{url.split('#', 1)[0]}#sha256={digest}"


def _write_index(path: Path, title: str, links: Sequence[tuple[str, str]]) -> None:
    anchors = "\n".join(
        f'    <a href="{html.escape(href, quote=True)}">{html.escape(label)}</a><br>'
        for href, label in links
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "<!doctype html>",
                '<html lang="en">',
                "  <head>",
                '    <meta charset="utf-8">',
                f"    <title>{html.escape(title)}</title>",
                "  </head>",
                "  <body>",
                anchors,
                "  </body>",
                "</html>",
                "",
            )
        ),
        encoding="utf-8",
    )


def _frozen_catalogs(pages_root: Path) -> list[dict[str, Any]]:
    catalog_root = pages_root / "catalogs"
    if not catalog_root.is_dir():
        return []
    catalogs: list[dict[str, Any]] = []
    for path in sorted(catalog_root.glob(f"*/{INSTALL_CATALOG_FILENAME}")):
        catalog = load_catalog(path)
        if path.parent.name != catalog["release"]["version"]:
            raise PagesError(f"frozen Catalog path does not match {path}")
        catalogs.append(catalog)
    return catalogs


def build_simple_indexes(
    pages_root: Path,
    *,
    fetch_wrapt_files: Callable[[str], Sequence[PyPIWheel]] = fetch_wrapt_wheels,
) -> None:
    """Rebuild every channel index from all frozen Stable Catalogs."""

    index_root = pages_root / "whl"
    if index_root.exists():
        shutil.rmtree(index_root)
    catalogs = _frozen_catalogs(pages_root)
    channels: dict[str, list[dict[str, Any]]] = {}
    for catalog in catalogs:
        for wheel in catalog["wheels"]:
            channels.setdefault(wheel["channel"], []).append(wheel)

    wrapt_cache: dict[str, Sequence[PyPIWheel]] = {}
    for channel in sorted(channels):
        wheels = channels[channel]
        project_root = index_root / channel
        _write_index(
            project_root / "index.html",
            f"UCM Simple Index: {channel}",
            (("uc-manager/", "uc-manager"), ("wrapt/", "wrapt")),
        )
        ucm_links = sorted(
            {
                (_hash_link(wheel["url"], wheel["sha256"]), wheel["filename"])
                for wheel in wheels
            },
            key=lambda item: (item[1], item[0]),
        )
        _write_index(
            project_root / "uc-manager" / "index.html",
            f"uc-manager: {channel}",
            ucm_links,
        )

        dependency_versions: set[str] = set()
        for wheel in wheels:
            dependency_versions.update(_exact_wrapt_versions(wheel["dependencies"]))
        if not dependency_versions:
            raise PagesError(f"channel {channel!r} has no exact wrapt dependency")
        dependency_links: set[tuple[str, str]] = set()
        for version in sorted(dependency_versions):
            if version not in wrapt_cache:
                wrapt_cache[version] = fetch_wrapt_files(version)
            files = wrapt_cache[version]
            for file in files:
                if (
                    Path(file.filename).name != file.filename
                    or _SHA256.fullmatch(file.sha256) is None
                ):
                    raise PagesError(f"invalid injected wrapt=={version} Wheel record")
                dependency_links.add((_hash_link(file.url, file.sha256), file.filename))
        _write_index(
            project_root / "wrapt" / "index.html",
            f"wrapt: {channel}",
            sorted(dependency_links, key=lambda item: (item[1], item[0])),
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


def inject_latest_catalog(pages_root: Path) -> str | None:
    """Copy the frozen current Stable Catalog into the mutable latest version."""

    latest_root = pages_root / "latest"
    if not latest_root.is_dir():
        print("[pages] latest documentation does not exist; skipping Catalog sync")
        return None
    destination = latest_root / INSTALL_CATALOG_FILENAME
    if destination.exists():
        destination.unlink()
    current_stable = stable_version(_load_versions(pages_root / "versions.json"))
    if current_stable is None:
        print("[pages] no stable alias; latest will show Catalog unavailable")
        return None
    source = pages_root / "catalogs" / current_stable / INSTALL_CATALOG_FILENAME
    if not source.is_file():
        print(
            f"[pages] stable alias {current_stable} has no frozen Catalog; "
            "latest will show Catalog unavailable"
        )
        return None
    catalog = load_catalog(source)
    _write_json(destination, catalog)
    return current_stable


def _freeze_stable_catalog(pages_root: Path, catalog: dict[str, Any]) -> None:
    version = catalog["release"]["version"]
    _write_json(pages_root / "catalogs" / version / INSTALL_CATALOG_FILENAME, catalog)
    _write_json(pages_root / version / INSTALL_CATALOG_FILENAME, catalog)


def _assert_cname_preserved(original: str | None) -> None:
    if _read_branch_file("CNAME") != original:
        raise PagesError("Pages publication changed the existing CNAME")


def publish_latest(repository: str) -> None:
    _prepare_pages_branch(repository)
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
    with _pages_worktree("Publish latest install Catalog") as worktree:
        inject_latest_catalog(worktree)
    _assert_cname_preserved(cname)
    _push_pages_branch()


def publish_stable(repository: str, catalog_path: Path) -> None:
    catalog = load_catalog(catalog_path)
    require_stable_catalog(catalog)
    version = catalog["release"]["version"]
    _prepare_pages_branch(repository)
    cname = _read_branch_file("CNAME")
    if not version_exists(_branch_versions(), version):
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
    with _pages_worktree(
        f"Freeze install Catalog and indexes for {version}"
    ) as worktree:
        _freeze_stable_catalog(worktree, catalog)
        inject_latest_catalog(worktree)
        build_simple_indexes(worktree)
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
    stable.add_argument("--catalog", type=Path, required=True)
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
            publish_stable(arguments.repository, arguments.catalog)
    except (PagesError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
