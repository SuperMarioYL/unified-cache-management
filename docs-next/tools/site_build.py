"""Build one language and version without modifying documentation sources."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from release_manifest import (
    RELEASE_MANIFEST_FILENAME,
    ManifestError,
    load_manifest,
    resolve_manifest,
)

DOCS_ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = {"en": "en", "zh": "zh-cn"}


def repository_name(url: str) -> str:
    match = re.fullmatch(
        r"(?:https://github.com/|git@github.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url
    )
    if not match:
        raise ManifestError("Documentation repository must be a GitHub OWNER/REPO URL")
    return match[1]


def current_repository() -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    clone_url = os.environ.get("READTHEDOCS_GIT_CLONE_URL")
    if not clone_url:
        clone_url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=DOCS_ROOT, text=True
        ).strip()
    return repository_name(clone_url)


def current_ref() -> str:
    if os.environ.get("READTHEDOCS_VERSION_TYPE") == "external":
        return (
            os.environ.get("READTHEDOCS_GIT_COMMIT_HASH")
            or subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=DOCS_ROOT, text=True
            ).strip()
        )
    return (
        os.environ.get("READTHEDOCS_GIT_IDENTIFIER")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=DOCS_ROOT, text=True
        ).strip()
    )


def write_redirects(site_dir: Path, redirects: dict[str, str]) -> None:
    """Keep legacy URLs in this language/version, preserving query and fragment."""
    for source, target in redirects.items():
        for path in (source, target):
            if (
                path.startswith("/")
                or ".." in Path(path).parts
                or not re.fullmatch(r"[\w./-]+", path)
            ):
                raise ValueError(
                    f"Redirect must use a relative documentation path: {path}"
                )
        destination = (
            site_dir / target / "index.html"
            if target.endswith("/")
            else site_dir / target
        )
        if not destination.is_file():
            raise ValueError(f"Redirect target does not exist: {target}")
        output = (
            site_dir / source / "index.html"
            if source.endswith("/")
            else site_dir / source
        )
        if output.exists():
            raise ValueError(f"Redirect would replace a documentation page: {source}")
        relative = os.path.relpath(site_dir / target, output.parent)
        if target.endswith("/"):
            relative += "/"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="robots" content="noindex">'
            f'<meta http-equiv="refresh" content="0; url={relative}">'
            f'<link rel="canonical" href="{relative}">'
            "<title>Documentation moved</title></head><body>"
            f'<a href="{relative}">Continue to the documentation</a>'
            "<script>const target = new URL("
            + json.dumps(relative)
            + ", location.href); target.search = location.search; "
            "target.hash = location.hash; location.replace(target.href);</script>"
            "</body></html>\n",
            encoding="utf-8",
        )


def build_site(
    *,
    language: str,
    site_dir: Path,
    site_url: str,
    strict: bool = True,
    repository: str | None = None,
    ref: str | None = None,
    release_tag: str | None = None,
    manifest_path: Path | None = None,
    docs_root: Path = DOCS_ROOT,
) -> None:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    if language not in LANGUAGES:
        raise ValueError(f"Unsupported documentation language: {language}")
    manifest = (
        load_manifest(manifest_path)
        if manifest_path
        else (resolve_manifest(repository, tag=release_tag) if repository else None)
    )
    with tempfile.TemporaryDirectory(prefix="ucm-docs-build-") as directory:
        sources = Path(directory) / "docs"
        shutil.copytree(docs_root / "docs", sources)
        fallback_pages = []
        if language == "zh":
            for source in (sources / "en").rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(sources / "en")
                target = sources / "zh" / relative
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    if source.suffix == ".md":
                        fallback_pages.append(relative.as_posix())
        config = load_config(
            config_file=str(docs_root / "mkdocs.yml"),
            docs_dir=str(sources),
            site_dir=str(site_dir.resolve()),
            site_url=site_url,
            strict=strict,
        )
        i18n = config.plugins["i18n"]
        settings = dict(i18n.config)
        settings["languages"] = [dict(item) for item in i18n.config.languages]
        settings["build_only_locale"] = language
        errors, warnings = i18n.load_config(settings)
        if errors or warnings:
            raise ValueError(f"Invalid language configuration: {errors or warnings}")
        config.extra["ucm_fallback_pages"] = fallback_pages
        config.extra["ucm_build_language"] = language
        config.extra["ucm_english_url"] = site_url.replace("/zh-cn/", "/en/", 1)
        if repository:
            config.repo_url = f"https://github.com/{repository}"
            config.repo_name = repository
            config.edit_uri = (
                f"edit/{quote(ref or current_ref(), safe='/')}/docs-next/docs/"
            )
        config.plugins.on_startup(command="build", dirty=False)
        try:
            build(config)
        finally:
            config.plugins.on_shutdown()
        mapping = docs_root / "redirects.json"
        if mapping.exists():
            write_redirects(site_dir, json.loads(mapping.read_text(encoding="utf-8")))
        if manifest is not None:
            (site_dir / RELEASE_MANIFEST_FILENAME).write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"[docs] Installation artifacts: {manifest['release']['url']}")
        else:
            print(
                "[docs] No completed Schema 8 release; installation links to source builds."
            )


def build_readthedocs() -> None:
    language = {"en": "en", "zh-cn": "zh"}[os.environ["READTHEDOCS_LANGUAGE"]]
    ref = current_ref()
    tag = ref if os.environ.get("READTHEDOCS_VERSION_TYPE") == "tag" else None
    if tag:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=DOCS_ROOT, text=True
        ).strip()
        tagged = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
            cwd=DOCS_ROOT,
            text=True,
        ).strip()
        if head != tagged:
            raise ManifestError("RTD checkout does not match the release tag")
    build_site(
        language=language,
        site_dir=Path(os.environ["READTHEDOCS_OUTPUT"]) / "html",
        site_url=os.environ["READTHEDOCS_CANONICAL_URL"],
        repository=current_repository(),
        ref=ref,
        release_tag=tag,
    )
