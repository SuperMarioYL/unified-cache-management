"""Exercise the installed Co-op public API rather than the unit-test fake."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DOCS_ROOT / "tools"))
from translation import markdown  # noqa: E402


def test_real_coop_roundtrips_the_28_required_english_pages_without_a_model():
    paths = [
        line.strip()
        for line in (DOCS_ROOT / "translation-required.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(paths) == 28
    for relative in paths:
        source_path = DOCS_ROOT / "docs/en" / relative
        source = source_path.read_text(encoding="utf-8")
        prepared = markdown.prepare_markdown_translation(
            source, source_path=source_path.as_posix(), language_code="zh-CN"
        )
        assert prepared.chunks, relative
        assert all(
            chunk.prompt and "===SYSTEM_USER_SPLIT===" not in chunk.prompt
            for chunk in prepared.chunks
        )
        reconstructed = markdown.finalize_markdown_translation(
            source,
            [chunk.text for chunk in prepared.chunks],
            source_path=source_path.as_posix(),
            language_code="zh-CN",
        )
        assert reconstructed.strip(), relative


def test_real_coop_cannot_change_a_relative_link_destination():
    source = "# Guide\n\n[Usage](../guide.md)\n"
    with pytest.raises(markdown.MarkdownTranslationError, match="link destinations"):
        markdown.validate_reconstructed_markdown(
            source,
            source.replace("../guide.md", "../wrong.md"),
            source_path="docs-next/docs/en/test.md",
            language_code="zh-CN",
        )


def test_real_coop_preserves_same_link_path_with_and_without_fragment():
    source = "# Guide\n\n[Build](../build.md) and [Options](../build.md#options).\n"
    prepared = markdown.prepare_markdown_translation(
        source, source_path="docs-next/docs/en/test.md", language_code="zh-CN"
    )
    reconstructed = markdown.finalize_markdown_translation(
        source,
        [chunk.text for chunk in prepared.chunks],
        source_path="docs-next/docs/en/test.md",
        language_code="zh-CN",
    )
    assert reconstructed == source
