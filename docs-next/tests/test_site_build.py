"""Exercise one-language builds, source immutability and legacy browser routes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DOCS_ROOT / "tools"))
import site_build  # noqa: E402
from test_release_resolution import published_release  # noqa: E402


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.fixture
def small_docs(tmp_path):
    root = tmp_path / "source"
    files = {
        "docs/en/index.md": "# Home\n\n[Guide](guide.md)\n",
        "docs/en/guide.md": "# English guide\n\n![Diagram](../assets/logo.svg)\n",
        "docs/en/translated.md": "# Translated guide\n",
        "docs/zh/index.md": "# 首页\n\n[指南](guide.md)\n",
        "docs/zh/translated.md": "# 已翻译指南\n",
        "docs/assets/logo.svg": '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20"/></svg>',
        "docs/assets/manifest.js": (DOCS_ROOT / "docs/assets/manifest.js").read_text(),
        "translation-state.json": '{"schema_version":2,"pages":{}}\n',
        "redirects.json": json.dumps(
            {"legacy/guide.html": "guide/", "old-guide/": "guide/"}
        ),
        "tools/hooks.py": (DOCS_ROOT / "tools/hooks.py").read_text(),
        "mkdocs.yml": """site_name: Fixture documentation
site_url: https://docs.example.invalid/
repo_url: https://github.com/example/ucm
edit_uri: edit/develop/docs-next/docs/
theme:
  name: material
  favicon: assets/logo.svg
  features: [content.action.edit]
extra_javascript: [assets/manifest.js]
hooks: [tools/hooks.py]
plugins:
  - search
  - i18n:
      docs_structure: folder
      fallback_to_default: true
      reconfigure_material: true
      reconfigure_search: true
      languages:
        - locale: en
          name: English
          default: true
          build: true
        - locale: zh
          name: Chinese
          build: true
markdown_extensions: [admonition]
nav:
  - Home: index.md
  - Guide: guide.md
  - Translated: translated.md
""",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.mark.parametrize("language,slug", [("en", "en"), ("zh", "zh-cn")])
def test_single_language_build_preserves_sources_and_publishes_version_root(
    tmp_path, small_docs, language, slug
):
    before = snapshot(small_docs)
    output = tmp_path / "site"
    manifest = published_release()[1]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    canonical = f"https://docs.example.invalid/{slug}/v0.9.3/"
    site_build.build_site(
        language=language,
        site_dir=output,
        site_url=canonical,
        repository="example/ucm",
        ref="v0.9.3",
        manifest_path=manifest_path,
        docs_root=small_docs,
        strict=True,
    )
    assert snapshot(small_docs) == before
    assert (output / "index.html").is_file()
    assert not (output / "zh/index.html").exists()
    assert not (output / "en/index.html").exists()
    assert json.loads((output / "release-manifest.json").read_text()) == manifest
    assert (output / "assets/manifest.js").read_bytes() == (
        small_docs / "docs/assets/manifest.js"
    ).read_bytes()
    assert (output / "assets/logo.svg").is_file()
    home = (output / "index.html").read_text()
    guide = (output / "guide/index.html").read_text()
    translated = (output / "translated/index.html").read_text()
    assert f'<link rel="canonical" href="{canonical}">' in home
    assert (
        f"https://github.com/example/ucm/edit/v0.9.3/docs-next/docs/{language}/index.md"
        in home
    )
    assert "/en/en/" not in home and "/zh/zh/" not in home
    assert 'href="assets/logo.svg"' in home
    image_url = re.search(r'<img(?=[^>]*alt="Diagram")[^>]+src="([^"]+)"', guide)
    assert image_url is not None
    assert urljoin(canonical + "guide/", image_url[1]) == canonical + "assets/logo.svg"
    notice = "本页尚未提供中文翻译"
    assert (notice in guide) is (language == "zh")
    assert notice not in translated
    if language == "zh":
        assert 'href="https://docs.example.invalid/en/v0.9.3/guide/"' in guide
        assert "edit/v0.9.3/docs-next/docs/en/guide.md" in guide
        assert "edit/v0.9.3/docs-next/docs/zh/guide.md" not in guide
        assert "已翻译指南" in translated
    search = json.loads((output / "search/search_index.json").read_text())
    assert any(
        "English guide" in item["title"] + item["text"] for item in search["docs"]
    )


def test_pr_ref_is_commit_hash_instead_of_pr_number(monkeypatch):
    monkeypatch.setenv("READTHEDOCS_VERSION_TYPE", "external")
    monkeypatch.setenv("READTHEDOCS_GIT_IDENTIFIER", "123")
    monkeypatch.setenv("READTHEDOCS_GIT_COMMIT_HASH", "f" * 40)
    assert site_build.current_ref() == "f" * 40


def test_rtd_build_passes_exact_tag_and_canonical_environment(monkeypatch, tmp_path):
    environment = {
        "READTHEDOCS_LANGUAGE": "zh-cn",
        "READTHEDOCS_VERSION_TYPE": "tag",
        "READTHEDOCS_GIT_IDENTIFIER": "v0.9.4rc1",
        "READTHEDOCS_OUTPUT": str(tmp_path),
        "READTHEDOCS_GIT_CLONE_URL": "https://github.com/example/ucm.git",
        "READTHEDOCS_CANONICAL_URL": "https://docs.example.invalid/zh-cn/v0.9.4rc1/",
    }
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(
        site_build.subprocess, "check_output", lambda *a, **kw: "a" * 40
    )
    monkeypatch.setattr(site_build, "build_site", lambda **kwargs: calls.append(kwargs))
    site_build.build_readthedocs()
    assert calls == [
        {
            "language": "zh",
            "site_dir": tmp_path / "html",
            "site_url": environment["READTHEDOCS_CANONICAL_URL"],
            "repository": "example/ucm",
            "ref": "v0.9.4rc1",
            "release_tag": "v0.9.4rc1",
        }
    ]


def test_rtd_tag_rejects_a_checkout_from_another_commit(monkeypatch):
    monkeypatch.setenv("READTHEDOCS_LANGUAGE", "en")
    monkeypatch.setenv("READTHEDOCS_VERSION_TYPE", "tag")
    monkeypatch.setenv("READTHEDOCS_GIT_IDENTIFIER", "v0.9.3")
    commits = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(
        site_build.subprocess, "check_output", lambda *a, **kw: next(commits)
    )
    with pytest.raises(
        site_build.ManifestError, match="does not match the release tag"
    ):
        site_build.build_readthedocs()


@pytest.mark.parametrize(
    "language,source", [("en", "legacy/guide.html"), ("zh-cn", "old-guide/")]
)
def test_redirect_browser_keeps_language_version_query_and_fragment(
    tmp_path, language, source
):
    destination = tmp_path / "guide/index.html"
    destination.parent.mkdir(parents=True)
    destination.write_text("<h1>Existing page</h1>")
    site_build.write_redirects(tmp_path, {source: "guide/"})
    redirect = (
        tmp_path / source / "index.html" if source.endswith("/") else tmp_path / source
    )
    script = re.search(r"<script>(.*?)</script>", redirect.read_text(), re.S)[1]
    old_url = f"https://docs.example.invalid/{language}/v0.9.3/{source}?x=1#section"
    javascript = f"""
      const current = new URL({json.dumps(old_url)});
      globalThis.location = {{href: current.href, search: current.search, hash: current.hash,
                              replace: (url) => console.log(url)}};
      {script}
    """
    result = subprocess.run(
        ["node", "-e", javascript], text=True, capture_output=True, check=True
    )
    assert (
        result.stdout.strip()
        == f"https://docs.example.invalid/{language}/v0.9.3/guide/?x=1#section"
    )
    assert destination.read_text() == "<h1>Existing page</h1>"
    with pytest.raises(ValueError, match="replace a documentation page"):
        site_build.write_redirects(tmp_path, {"guide/": "guide/"})
    assert destination.read_text() == "<h1>Existing page</h1>"
