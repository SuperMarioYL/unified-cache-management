"""Provider-free Markdown translation orchestration.

Co-op Translator owns Markdown chunking, placeholder protection, and
reconstruction. The configured HTTP API translates the prepared chunks; this
module never handles model credentials or provider protocol calls.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Mapping, Protocol, Sequence


class MarkdownTranslationError(RuntimeError):
    """Raised when a Markdown translation cannot be safely reconstructed."""


class TranslationDependencyError(MarkdownTranslationError):
    """Raised when the translation-only Co-op dependency is unavailable."""


@dataclass(frozen=True)
class AgentTranslationChunk:
    """One provider-free chunk with trusted instructions separated from text."""

    id: str
    text: str
    prompt: str


@dataclass(frozen=True)
class PreparedMarkdownTranslation:
    """Provider-free chunks prepared from one Markdown document."""

    chunks: tuple[AgentTranslationChunk, ...]


class AgentMarkdownBackend(Protocol):
    """Narrow seam around Co-op Translator's agent-assisted API."""

    def start(
        self,
        document: str,
        language_code: str,
        source_path: str,
    ) -> Mapping[str, object]: ...

    def finish(
        self,
        job: Mapping[str, object],
        translated_chunks: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]: ...


class CoopAgentMarkdownBackend:
    """Lazy adapter for Co-op Translator's stable public Python API."""

    @staticmethod
    def _api() -> tuple[object, object]:
        try:
            from co_op_translator.api import (  # type: ignore[import-not-found]
                finish_markdown_agent_translation,
                start_markdown_agent_translation,
            )
        except ImportError as error:  # pragma: no cover - exercised without dependency
            raise TranslationDependencyError(
                "Co-op Translator is required only for `site.py translate`; "
                "install docs-next/requirements-translation.txt before running "
                "translation."
            ) from error
        return start_markdown_agent_translation, finish_markdown_agent_translation

    def start(
        self,
        document: str,
        language_code: str,
        source_path: str,
    ) -> Mapping[str, object]:
        start, _ = self._api()
        return start(document, language_code, source_path=source_path)  # type: ignore[operator]

    def finish(
        self,
        job: Mapping[str, object],
        translated_chunks: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]:
        _, finish = self._api()
        return finish(job, translated_chunks)  # type: ignore[operator]


_COOP_PROMPT_BOUNDARY = "===SYSTEM_USER_SPLIT==="


def _trusted_coop_prompt(prompt: str, chunk_id: str) -> str:
    """Keep Co-op's rules while removing the untrusted source it appends."""

    trusted, boundary, _source = prompt.partition(_COOP_PROMPT_BOUNDARY)
    if not boundary or not trusted.strip():
        raise MarkdownTranslationError(
            f"Co-op Translator returned no prompt trust boundary for chunk {chunk_id}"
        )
    return trusted.strip()


def _job_chunks(job: Mapping[str, object]) -> tuple[AgentTranslationChunk, ...]:
    raw_chunks = job.get("chunks")
    if not isinstance(raw_chunks, list):
        raise MarkdownTranslationError("Co-op Translator returned an invalid chunk job")

    chunks: list[AgentTranslationChunk] = []
    seen: set[str] = set()
    for raw_chunk in raw_chunks:
        if not isinstance(raw_chunk, Mapping):
            raise MarkdownTranslationError(
                "Co-op Translator returned an invalid chunk job"
            )
        chunk_id = raw_chunk.get("id")
        source = raw_chunk.get("source")
        prompt = raw_chunk.get("prompt", "")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen:
            raise MarkdownTranslationError(
                "Co-op Translator returned an invalid or duplicate chunk id"
            )
        if not isinstance(source, str) or not isinstance(prompt, str):
            raise MarkdownTranslationError(
                f"Co-op Translator returned invalid content for chunk {chunk_id}"
            )
        seen.add(chunk_id)
        chunks.append(
            AgentTranslationChunk(
                chunk_id,
                source,
                _trusted_coop_prompt(prompt, chunk_id),
            )
        )
    return tuple(chunks)


def _mapping_values(job: Mapping[str, object], key: str) -> tuple[str, ...]:
    state = job.get("state")
    if not isinstance(state, Mapping):
        raise MarkdownTranslationError("Co-op Translator returned invalid job state")
    raw_mapping = state.get(key, {})
    if not isinstance(raw_mapping, Mapping):
        raise MarkdownTranslationError("Co-op Translator returned invalid job state")
    values: list[str] = []
    for value in raw_mapping.values():
        if not isinstance(value, str):
            raise MarkdownTranslationError(
                "Co-op Translator returned invalid job state"
            )
        values.append(value)
    return tuple(values)


def _inline_code_spans(document: str) -> tuple[str, ...]:
    """Extract inline-code spans after fenced blocks have been ignored.

    Co-op Translator protects fenced code itself.  Inline spans are prompted as
    immutable but are checked here as a second, deterministic boundary.
    """

    fenced = _fenced_ranges(document)
    spans: list[str] = []
    index = 0
    range_index = 0
    while index < len(document):
        if range_index < len(fenced) and index >= fenced[range_index][1]:
            range_index += 1
            continue
        if range_index < len(fenced) and fenced[range_index][0] <= index:
            index = fenced[range_index][1]
            continue
        if document[index] != "`":
            index += 1
            continue
        run_end = index + 1
        while run_end < len(document) and document[run_end] == "`":
            run_end += 1
        marker = document[index:run_end]
        closing = document.find(marker, run_end)
        if closing < 0:
            index = run_end
            continue
        spans.append(document[index : closing + len(marker)])
        index = closing + len(marker)
    return tuple(spans)


_FENCE_RE = re.compile(r"(?m)^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,}).*$")


def _fenced_ranges(document: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    search_at = 0
    while match := _FENCE_RE.search(document, search_at):
        marker = match.group("marker")
        close_re = re.compile(
            rf"(?m)^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$"
        )
        closing = close_re.search(document, match.end())
        if closing is None:
            break
        end = closing.end()
        if end < len(document) and document[end] == "\n":
            end += 1
        ranges.append((match.start(), end))
        search_at = end
    return tuple(ranges)


_RAW_URL_RE = re.compile(r"(?:https?://|mailto:)[^\s<>\"'`]+")


def _raw_urls(document: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).rstrip(".,;:!?") for match in _RAW_URL_RE.finditer(document)
    )


def _assert_same_multiset(
    *, source: Sequence[str], translated: Sequence[str], label: str
) -> None:
    if Counter(source) != Counter(translated):
        raise MarkdownTranslationError(f"translation changed protected {label}")


class _HtmlShapeParser(HTMLParser):
    """Collect structural HTML without retaining translatable display text."""

    _TRANSLATABLE_ATTRIBUTES = {"alt", "aria-label", "placeholder", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.shape: list[tuple[str, str, tuple[tuple[str, str | None], ...]]] = []

    def _attributes(
        self, attributes: list[tuple[str, str | None]]
    ) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (
                name,
                None if name.lower() in self._TRANSLATABLE_ATTRIBUTES else value,
            )
            for name, value in attributes
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.shape.append(("start", tag, self._attributes(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.shape.append(("empty", tag, self._attributes(attrs)))

    def handle_endtag(self, tag: str) -> None:
        self.shape.append(("end", tag, ()))


def _html_shape(document: str) -> tuple[object, ...]:
    parser = _HtmlShapeParser()
    try:
        parser.feed(document)
        parser.close()
    except (TypeError, ValueError) as error:
        raise MarkdownTranslationError(f"invalid embedded HTML: {error}") from error
    return tuple(parser.shape)


_MATERIAL_BLOCK_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<marker>!!!|\?\?\?\+?)[ \t]+"
    r'(?P<kind>[A-Za-z0-9_-]+)(?:[ \t]+["\'].*)?$'
)
_MATERIAL_TAB_RE = re.compile(r'(?m)^(?P<indent>[ \t]*)===[ \t]+["\'].*["\'][ \t]*$')
_SNIPPET_RE = re.compile(r"(?m)^[ \t]*--8<--.*$")


def _material_shape(document: str) -> tuple[object, ...]:
    blocks = tuple(
        (match.group("indent"), match.group("marker"), match.group("kind"))
        for match in _MATERIAL_BLOCK_RE.finditer(document)
    )
    tabs = tuple(match.group("indent") for match in _MATERIAL_TAB_RE.finditer(document))
    snippets = tuple(match.group(0).strip() for match in _SNIPPET_RE.finditer(document))
    return blocks, tabs, snippets


def _unescaped_pipe_count(line: str) -> int:
    count = 0
    backslashes = 0
    for character in line:
        if character == "\\":
            backslashes += 1
            continue
        if character == "|" and backslashes % 2 == 0:
            count += 1
        backslashes = 0
    return count


def _table_shape(document: str) -> tuple[int, ...]:
    fenced = _fenced_ranges(document)
    range_index = 0
    offset = 0
    shape: list[int] = []
    for line in document.splitlines(keepends=True):
        while range_index < len(fenced) and offset >= fenced[range_index][1]:
            range_index += 1
        inside_fence = (
            range_index < len(fenced)
            and fenced[range_index][0] <= offset < fenced[range_index][1]
        )
        if not inside_fence:
            pipe_count = _unescaped_pipe_count(line)
            if pipe_count >= 2:
                shape.append(pipe_count)
        offset += len(line)
    return tuple(shape)


def _validate_markdown_structure(source: str, translated: str) -> None:
    if _html_shape(source) != _html_shape(translated):
        raise MarkdownTranslationError(
            "translation changed embedded HTML tags or structural attributes"
        )
    if _material_shape(source) != _material_shape(translated):
        raise MarkdownTranslationError(
            "translation changed Material admonition, tab, or snippet syntax"
        )
    if _table_shape(source) != _table_shape(translated):
        raise MarkdownTranslationError("translation changed Markdown table columns")


def _validate_job_protection(
    source: str,
    translated: str,
    source_job: Mapping[str, object],
    translated_job: Mapping[str, object],
) -> tuple[str, ...]:
    source_code = _mapping_values(source_job, "placeholder_map")
    translated_code = _mapping_values(translated_job, "placeholder_map")
    source_links = (
        *_mapping_values(source_job, "link_destination_map"),
        *_mapping_values(source_job, "frontmatter_link_destination_map"),
    )
    translated_links = (
        *_mapping_values(translated_job, "link_destination_map"),
        *_mapping_values(translated_job, "frontmatter_link_destination_map"),
    )
    _assert_same_multiset(
        source=source_code,
        translated=translated_code,
        label="fenced code",
    )
    _assert_same_multiset(
        source=source_links,
        translated=translated_links,
        label="link destinations",
    )
    protected_values = (*source_code, *source_links)

    if "@@CODE_BLOCK_" in translated or "@@LINK_DESTINATION_" in translated:
        raise MarkdownTranslationError("translation contains unresolved placeholders")

    _assert_same_multiset(
        source=_inline_code_spans(source),
        translated=_inline_code_spans(translated),
        label="inline code",
    )
    _assert_same_multiset(
        source=_raw_urls(source),
        translated=_raw_urls(translated),
        label="URLs",
    )
    _validate_markdown_structure(source, translated)
    return protected_values


def _without_protected(document: str, protected_values: Sequence[str]) -> str:
    text = document
    for value in sorted(set(protected_values), key=len, reverse=True):
        text = text.replace(value, "")
    for value in sorted(set(_inline_code_spans(text)), key=len, reverse=True):
        text = text.replace(value, "")
    for value in sorted(set(_raw_urls(text)), key=len, reverse=True):
        text = text.replace(value, "")
    return text


def _validate_glossary(
    source: str,
    translated: str,
    protected_values: Sequence[str],
    preserve_terms: Sequence[str],
    fixed_terms: Mapping[str, str],
) -> None:
    source_text = _without_protected(source, protected_values)
    translated_text = _without_protected(translated, protected_values)

    for term in preserve_terms:
        expected = source_text.count(term)
        if expected and translated_text.count(term) < expected:
            raise MarkdownTranslationError(
                f"translation did not preserve glossary term {term!r}"
            )
    for source_term, target_term in fixed_terms.items():
        expected = source_text.count(source_term)
        if expected and translated_text.count(target_term) < expected:
            raise MarkdownTranslationError(
                f"translation did not use fixed glossary term {target_term!r}"
            )


def validate_reconstructed_markdown(
    source: str,
    translated: str,
    *,
    source_path: str,
    language_code: str,
    preserve_terms: Sequence[str] = (),
    fixed_terms: Mapping[str, str] | None = None,
    backend: AgentMarkdownBackend | None = None,
) -> None:
    """Validate an existing translation without calling a model provider."""

    selected_backend = backend or CoopAgentMarkdownBackend()
    try:
        source_job = selected_backend.start(source, language_code, source_path)
        translated_job = selected_backend.start(translated, language_code, source_path)
        protected_values = _validate_job_protection(
            source, translated, source_job, translated_job
        )
        _validate_glossary(
            source,
            translated,
            protected_values,
            preserve_terms,
            fixed_terms or {},
        )
    except MarkdownTranslationError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MarkdownTranslationError(
            f"cannot validate reconstructed Markdown for {source_path}: {error}"
        ) from error


def prepare_markdown_translation(
    source: str,
    *,
    source_path: str,
    language_code: str,
    backend: AgentMarkdownBackend | None = None,
) -> PreparedMarkdownTranslation:
    """Prepare Co-op chunks without calling or configuring a model provider."""

    selected_backend = backend or CoopAgentMarkdownBackend()
    try:
        job = selected_backend.start(source, language_code, source_path)
        return PreparedMarkdownTranslation(chunks=_job_chunks(job))
    except MarkdownTranslationError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MarkdownTranslationError(
            f"cannot prepare Markdown translation for {source_path}: {error}"
        ) from error


def finalize_markdown_translation(
    source: str,
    translated_texts: Sequence[str],
    *,
    source_path: str,
    language_code: str,
    preserve_terms: Sequence[str] = (),
    fixed_terms: Mapping[str, str] | None = None,
    backend: AgentMarkdownBackend | None = None,
) -> str:
    """Reconstruct and validate one document from ordered agent chunk outputs."""

    selected_backend = backend or CoopAgentMarkdownBackend()
    try:
        job = selected_backend.start(source, language_code, source_path)
        chunks = _job_chunks(job)
        translations = tuple(translated_texts)
        if len(translations) != len(chunks) or not all(
            isinstance(text, str) and text for text in translations
        ):
            raise MarkdownTranslationError(
                f"agent output count differs for {source_path}: "
                f"expected {len(chunks)}, got {len(translations)}"
            )
        reconstructed = selected_backend.finish(
            job,
            [
                {"chunk_id": chunk.id, "translated_text": translated}
                for chunk, translated in zip(chunks, translations, strict=True)
            ],
        )
        content = reconstructed.get("content")
        warnings = reconstructed.get("warnings", [])
        if not isinstance(content, str):
            raise MarkdownTranslationError(
                "Co-op Translator returned no reconstructed Markdown"
            )
        if not isinstance(warnings, list) or warnings:
            raise MarkdownTranslationError(
                "Co-op Translator reported reconstruction warnings"
            )
        translated_job = selected_backend.start(content, language_code, source_path)
        protected_values = _validate_job_protection(
            source, content, job, translated_job
        )
        _validate_glossary(
            source,
            content,
            protected_values,
            preserve_terms,
            fixed_terms or {},
        )
        return content
    except MarkdownTranslationError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MarkdownTranslationError(
            f"cannot finalize Markdown translation for {source_path}: {error}"
        ) from error
