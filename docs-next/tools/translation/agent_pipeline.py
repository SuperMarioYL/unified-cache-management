"""Provider-free task, artifact, patch, and CAS hand-off contracts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping, Sequence

from .core import (
    REPOSITORY_PREFIX,
    STATE_FILENAME,
    TARGET_LANGUAGE,
    AgentMarkdownBackend,
    ChangeSet,
    Glossary,
    PageRequest,
    TranslationError,
    TranslationIdentity,
    TranslationLayout,
    TranslationPlan,
    TranslationState,
    _atomic_write_bytes,
    _git_root,
    _page_state,
    _resolve_base_ref,
    build_translation_plan,
    content_hash,
)
from .markdown import finalize_markdown_translation, prepare_markdown_translation

TASK_SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 2
TASK_FILENAME = "translation-task.json"
ARTIFACT_FILENAME = "translation-artifact.json"
PATCH_FILENAME = "translation.patch"
MAX_TASK_PAGES = 10
MAX_TASK_SOURCE_BYTES = 250 * 1024
MAX_AGENT_OUTPUT_BYTES = 1024 * 1024
MAX_CONTROL_BYTES = 2 * 1024 * 1024

_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CHUNK_ID_RE = re.compile(r"p[0-9]{3}-c[0-9]{3}\Z")


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranslationError(f"{name} must be a non-empty string")
    return value.strip()


def _sha(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _non_empty(value, name)
    if not _HASH_RE.fullmatch(text):
        raise TranslationError(f"{name} must be an exact sha256")
    return text


def _git_sha(value: object, name: str) -> str:
    text = _non_empty(value, name).lower()
    if not _GIT_SHA_RE.fullmatch(text):
        raise TranslationError(f"{name} must be an exact Git object id")
    return text


def _repo_id(value: object, name: str) -> str:
    text = str(value).strip() if not isinstance(value, bool) else ""
    if not text.isdigit() or int(text) <= 0:
        raise TranslationError(f"{name} must be a positive repository id")
    return text


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TranslationError(f"{name} must be a non-negative integer")
    return value


def _fields(payload: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(payload) != expected:
        raise TranslationError(
            f"{name} fields must be exactly: {', '.join(sorted(expected))}"
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TranslationError(f"JSON contains duplicate field {key!r}")
        result[key] = value
    return result


def _load_json(path: Path, name: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise TranslationError(f"{name} must be a regular file: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_CONTROL_BYTES:
        raise TranslationError(f"{name} is too large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranslationError(f"invalid {name} JSON: {error}") from error
    if not isinstance(value, Mapping):
        raise TranslationError(f"{name} root must be an object")
    return value


def _repo_source(value: object, name: str) -> PurePosixPath:
    path = PurePosixPath(_non_empty(value, name))
    prefix = REPOSITORY_PREFIX / "docs" / "en"
    try:
        relative = path.relative_to(prefix)
    except ValueError as error:
        raise TranslationError(f"unsafe {name} path must be below {prefix}") from error
    if (
        path.as_posix() != value
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".md"
    ):
        raise TranslationError(f"{name} is not a normalized Markdown path")
    return path


def _repo_target(value: object, name: str) -> PurePosixPath:
    path = PurePosixPath(_non_empty(value, name))
    prefix = REPOSITORY_PREFIX / "docs" / "zh"
    try:
        relative = path.relative_to(prefix)
    except ValueError as error:
        raise TranslationError(f"unsafe {name} path must be below {prefix}") from error
    if (
        path.as_posix() != value
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".md"
    ):
        raise TranslationError(f"{name} is not a normalized Markdown path")
    return path


def _artifact_write(value: object) -> PurePosixPath:
    path = PurePosixPath(_non_empty(value, "artifact write"))
    if path == REPOSITORY_PREFIX / STATE_FILENAME:
        return path
    return _repo_target(path.as_posix(), "artifact write")


def default_agent_instructions_path() -> Path:
    return Path(__file__).resolve().parent / "instructions.md"


def load_agent_instructions(path: Path | None = None) -> str:
    selected = (path or default_agent_instructions_path()).resolve()
    if selected.is_symlink() or not selected.is_file():
        raise TranslationError(f"agent instructions must be a regular file: {selected}")
    raw = selected.read_bytes()
    if not raw or len(raw) > 64 * 1024 or b"\0" in raw:
        raise TranslationError("agent instructions are empty, binary, or too large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TranslationError("agent instructions must be UTF-8") from error


def resolve_git_sha(docs_next_root: Path, ref: str) -> str:
    return _git_sha(
        _resolve_base_ref(_git_root(docs_next_root.resolve()), ref),
        "resolved Git sha",
    )


@dataclass(frozen=True)
class TranslationProvenance:
    mode: Literal["changed", "missing"]
    base_sha: str
    head_sha: str
    base_repository_id: str
    head_repository_id: str
    pr_number: int | None

    def __post_init__(self) -> None:
        if self.mode not in {"changed", "missing"}:
            raise TranslationError(f"unsupported translation mode: {self.mode}")
        object.__setattr__(self, "base_sha", _git_sha(self.base_sha, "base_sha"))
        object.__setattr__(self, "head_sha", _git_sha(self.head_sha, "head_sha"))
        object.__setattr__(
            self,
            "base_repository_id",
            _repo_id(self.base_repository_id, "base_repository_id"),
        )
        object.__setattr__(
            self,
            "head_repository_id",
            _repo_id(self.head_repository_id, "head_repository_id"),
        )
        if self.pr_number is not None and (
            isinstance(self.pr_number, bool)
            or not isinstance(self.pr_number, int)
            or self.pr_number <= 0
        ):
            raise TranslationError("pr_number must be a positive integer or null")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TranslationProvenance":
        expected = {
            "mode",
            "base_sha",
            "head_sha",
            "base_repository_id",
            "head_repository_id",
            "pr_number",
        }
        _fields(value, expected, "translation provenance")
        return cls(**value)  # type: ignore[arg-type]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "base_repository_id": self.base_repository_id,
            "head_repository_id": self.head_repository_id,
            "pr_number": self.pr_number,
        }


@dataclass(frozen=True)
class TaskPage:
    source_path: PurePosixPath
    target_path: PurePosixPath
    source_hash: str
    expected_target_hash: str | None
    chunk_ids: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], index: int) -> "TaskPage":
        expected = {
            "source_path",
            "target_path",
            "source_hash",
            "expected_target_hash",
            "chunk_ids",
        }
        _fields(value, expected, f"task page {index}")
        source = _repo_source(value["source_path"], "page.source_path")
        target = _repo_target(value["target_path"], "page.target_path")
        if source.relative_to(REPOSITORY_PREFIX / "docs" / "en") != target.relative_to(
            REPOSITORY_PREFIX / "docs" / "zh"
        ):
            raise TranslationError("task source and target mirror paths differ")
        raw_ids = value["chunk_ids"]
        if not isinstance(raw_ids, list) or not all(
            isinstance(item, str) for item in raw_ids
        ):
            raise TranslationError("page.chunk_ids must be a string list")
        chunk_ids = tuple(raw_ids)
        if len(chunk_ids) != len(set(chunk_ids)) or not all(
            _CHUNK_ID_RE.fullmatch(item) for item in chunk_ids
        ):
            raise TranslationError("page.chunk_ids are invalid or duplicated")
        return cls(
            source,
            target,
            _sha(value["source_hash"], "page.source_hash"),  # type: ignore[arg-type]
            _sha(
                value["expected_target_hash"],
                "page.expected_target_hash",
                nullable=True,
            ),
            chunk_ids,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path.as_posix(),
            "target_path": self.target_path.as_posix(),
            "source_hash": self.source_hash,
            "expected_target_hash": self.expected_target_hash,
            "chunk_ids": list(self.chunk_ids),
        }


@dataclass(frozen=True)
class TaskChunk:
    id: str
    page_index: int
    source_path: PurePosixPath
    input_path: PurePosixPath
    output_path: PurePosixPath

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], index: int) -> "TaskChunk":
        expected = {"id", "page_index", "source_path", "input_path", "output_path"}
        _fields(value, expected, f"task chunk {index}")
        chunk_id = _non_empty(value["id"], "chunk.id")
        if not _CHUNK_ID_RE.fullmatch(chunk_id):
            raise TranslationError(f"invalid chunk id: {chunk_id}")
        page_index = _count(value["page_index"], "chunk.page_index")
        source = _repo_source(value["source_path"], "chunk.source_path")
        input_path = PurePosixPath(_non_empty(value["input_path"], "chunk.input_path"))
        output_path = PurePosixPath(
            _non_empty(value["output_path"], "chunk.output_path")
        )
        if input_path != PurePosixPath("chunks") / f"{chunk_id}.md":
            raise TranslationError("chunk.input_path differs from its id")
        if output_path != PurePosixPath(f"{chunk_id}.md"):
            raise TranslationError("chunk.output_path differs from its id")
        return cls(chunk_id, page_index, source, input_path, output_path)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_index": self.page_index,
            "source_path": self.source_path.as_posix(),
            "input_path": self.input_path.as_posix(),
            "output_path": self.output_path.as_posix(),
        }


_TASK_SUMMARY_FIELDS = {
    "total_pages",
    "selected_pages",
    "remaining_pages",
    "selected_chunks",
    "source_bytes",
    "deletes",
    "state_changes",
    "page_limit",
}


@dataclass(frozen=True)
class TranslationTask:
    provenance: TranslationProvenance
    identity: TranslationIdentity
    pages: tuple[TaskPage, ...]
    chunks: tuple[TaskChunk, ...]
    deletes: tuple[PurePosixPath, ...]
    summary: Mapping[str, int]
    task_path: Path | None = field(default=None, compare=False)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], *, task_path: Path | None = None
    ) -> "TranslationTask":
        expected = {
            "schema_version",
            "provenance",
            "identity",
            "pages",
            "chunks",
            "deletes",
            "summary",
        }
        _fields(value, expected, "translation task")
        if value["schema_version"] != TASK_SCHEMA_VERSION:
            raise TranslationError("unsupported translation task schema")
        if not isinstance(value["provenance"], Mapping) or not isinstance(
            value["identity"], Mapping
        ):
            raise TranslationError("task provenance and identity must be objects")
        raw_pages, raw_chunks = value["pages"], value["chunks"]
        if not isinstance(raw_pages, list) or not all(
            isinstance(item, Mapping) for item in raw_pages
        ):
            raise TranslationError("task pages must be an object list")
        if not isinstance(raw_chunks, list) or not all(
            isinstance(item, Mapping) for item in raw_chunks
        ):
            raise TranslationError("task chunks must be an object list")
        raw_deletes, raw_summary = value["deletes"], value["summary"]
        if not isinstance(raw_deletes, list) or not isinstance(raw_summary, Mapping):
            raise TranslationError("task deletes/summary have invalid types")
        _fields(raw_summary, _TASK_SUMMARY_FIELDS, "task summary")
        task = cls(
            TranslationProvenance.from_mapping(value["provenance"]),
            TranslationIdentity.from_mapping(value["identity"]),
            tuple(
                TaskPage.from_mapping(item, index)
                for index, item in enumerate(raw_pages)
            ),
            tuple(
                TaskChunk.from_mapping(item, index)
                for index, item in enumerate(raw_chunks)
            ),
            tuple(_repo_target(item, "task delete") for item in raw_deletes),
            {
                key: _count(raw_summary[key], f"summary.{key}")
                for key in _TASK_SUMMARY_FIELDS
            },
            task_path,
        )
        task.validate()
        return task

    @classmethod
    def load(cls, task_path: Path) -> "TranslationTask":
        path = task_path.resolve()
        return cls.from_mapping(_load_json(path, "translation task"), task_path=path)

    def validate(self) -> None:
        if len(self.pages) > MAX_TASK_PAGES:
            raise TranslationError("translation task exceeds 10 pages")
        page_limit = self.summary["page_limit"]
        if not 1 <= page_limit <= MAX_TASK_PAGES or len(self.pages) > page_limit:
            raise TranslationError("translation task page_limit is inconsistent")
        ids = [chunk.id for chunk in self.chunks]
        if len(ids) != len(set(ids)):
            raise TranslationError("translation task contains duplicate chunk ids")
        if len(self.deletes) != len(set(self.deletes)):
            raise TranslationError("translation task contains duplicate deletes")
        by_page: dict[int, list[str]] = {index: [] for index in range(len(self.pages))}
        for chunk in self.chunks:
            if chunk.page_index not in by_page:
                raise TranslationError("chunk.page_index is out of range")
            if chunk.source_path != self.pages[chunk.page_index].source_path:
                raise TranslationError("chunk source differs from its page")
            by_page[chunk.page_index].append(chunk.id)
        for index, page in enumerate(self.pages):
            if tuple(by_page[index]) != page.chunk_ids:
                raise TranslationError("page chunk_ids differ from ordered chunks")
        if self.summary["selected_pages"] != len(self.pages):
            raise TranslationError("task selected_pages summary is inconsistent")
        if self.summary["selected_chunks"] != len(self.chunks):
            raise TranslationError("task selected_chunks summary is inconsistent")
        if self.summary["deletes"] != len(self.deletes):
            raise TranslationError("task deletes summary is inconsistent")
        if self.summary["total_pages"] != (
            self.summary["selected_pages"] + self.summary["remaining_pages"]
        ):
            raise TranslationError("task page summary is inconsistent")
        if self.summary["source_bytes"] > MAX_TASK_SOURCE_BYTES:
            raise TranslationError("task exceeds 250KiB")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "provenance": self.provenance.as_dict(),
            "identity": self.identity.as_dict(),
            "pages": [page.as_dict() for page in self.pages],
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "deletes": [path.as_posix() for path in self.deletes],
            "summary": dict(self.summary),
        }

    @property
    def task_dir(self) -> Path | None:
        return self.task_path.parent if self.task_path else None


@dataclass
class _TaskBuild:
    task: TranslationTask
    chunks: dict[str, str]
    plan: TranslationPlan
    selected: tuple[PageRequest, ...]


def _select_requests(
    requests: tuple[PageRequest, ...],
    mode: Literal["changed", "missing"],
    *,
    max_pages: int,
) -> tuple[tuple[PageRequest, ...], int]:
    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or not (1 <= max_pages <= MAX_TASK_PAGES)
    ):
        raise TranslationError("max_pages must be between 1 and 10")
    sizes = [len(request.source.encode("utf-8")) for request in requests]
    for request, size in zip(requests, sizes, strict=True):
        if size > MAX_TASK_SOURCE_BYTES:
            raise TranslationError(
                f"source page exceeds 250KiB: {request.relative_path}"
            )
    if mode == "changed":
        if len(requests) > max_pages or sum(sizes) > MAX_TASK_SOURCE_BYTES:
            raise TranslationError(
                f"changed translation exceeds {max_pages} pages or 250KiB"
            )
        return requests, 0
    selected: list[PageRequest] = []
    selected_bytes = 0
    for request, size in zip(requests, sizes, strict=True):
        if len(selected) >= max_pages or selected_bytes + size > MAX_TASK_SOURCE_BYTES:
            break
        selected.append(request)
        selected_bytes += size
    return tuple(selected), len(requests) - len(selected)


def _build_task(
    plan: TranslationPlan,
    provenance: TranslationProvenance,
    backend: AgentMarkdownBackend | None,
    *,
    max_pages: int = MAX_TASK_PAGES,
) -> _TaskBuild:
    selected, remaining = _select_requests(
        plan.requests,
        provenance.mode,
        max_pages=max_pages,
    )
    pages: list[TaskPage] = []
    chunks: list[TaskChunk] = []
    chunk_content: dict[str, str] = {}
    for page_index, request in enumerate(selected):
        source_path = plan.layout.source_repo_path(request.relative_path)
        target_path = plan.layout.target_repo_path(request.relative_path)
        prepared = prepare_markdown_translation(
            request.source,
            source_path=source_path.as_posix(),
            language_code=TARGET_LANGUAGE,
            backend=backend,
        )
        ids: list[str] = []
        for chunk_index, chunk in enumerate(prepared.chunks):
            if chunk_index > 999:
                raise TranslationError(f"too many chunks for {source_path}")
            chunk_id = f"p{page_index:03d}-c{chunk_index:03d}"
            ids.append(chunk_id)
            chunks.append(
                TaskChunk(
                    chunk_id,
                    page_index,
                    source_path,
                    PurePosixPath("chunks") / f"{chunk_id}.md",
                    PurePosixPath(f"{chunk_id}.md"),
                )
            )
            chunk_content[chunk_id] = chunk.text
        target_file = plan.layout.target_path(request.relative_path)
        target_hash = (
            content_hash(target_file.read_text(encoding="utf-8"))
            if target_file.exists()
            else None
        )
        pages.append(
            TaskPage(
                source_path,
                target_path,
                content_hash(request.source),
                target_hash,
                tuple(ids),
            )
        )
    task = TranslationTask(
        provenance,
        plan.identity,
        tuple(pages),
        tuple(chunks),
        plan.deletes,
        {
            "total_pages": len(plan.requests),
            "selected_pages": len(selected),
            "remaining_pages": remaining,
            "selected_chunks": len(chunks),
            "source_bytes": sum(len(item.source.encode("utf-8")) for item in selected),
            "deletes": len(plan.deletes),
            "state_changes": int(plan.state.pages != plan.initial_state.pages),
            "page_limit": max_pages,
        },
    )
    task.validate()
    return _TaskBuild(task, chunk_content, plan, selected)


def _new_output(output_dir: Path) -> tuple[Path, Path]:
    final = output_dir.resolve()
    if final.exists():
        raise TranslationError(f"output directory already exists: {final}")
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{final.name}.tmp-", dir=str(final.parent))
    )
    return staging, final


def prepare_translation(
    docs_next_root: Path,
    *,
    mode: Literal["changed", "missing"],
    output_dir: Path,
    provenance: TranslationProvenance,
    identity: TranslationIdentity,
    base_ref: str | None = None,
    glossary: Glossary | None = None,
    changes: ChangeSet | None = None,
    backend: AgentMarkdownBackend | None = None,
    agent_instructions_path: Path | None = None,
    max_pages: int = MAX_TASK_PAGES,
) -> TranslationTask:
    """Materialize a bounded translation task and raw chunk inputs."""

    layout = TranslationLayout(docs_next_root)
    if provenance.mode != mode:
        raise TranslationError("provenance mode differs from prepare mode")
    selected_glossary = glossary or Glossary.load(layout.glossary_path)
    instructions = load_agent_instructions(agent_instructions_path)
    if identity.agent_hash != content_hash(instructions):
        raise TranslationError("identity agent_hash differs from instructions")
    if identity.glossary_hash != selected_glossary.hash:
        raise TranslationError("identity glossary_hash differs from glossary")
    selected_base = base_ref or provenance.base_sha
    if resolve_git_sha(layout.docs_next_root, selected_base) != provenance.base_sha:
        raise TranslationError("base_ref differs from provenance.base_sha")
    plan = build_translation_plan(
        layout.docs_next_root,
        mode=mode,
        identity=identity,
        glossary=selected_glossary,
        changes=changes,
        base_ref=selected_base,
    )
    built = _build_task(plan, provenance, backend, max_pages=max_pages)
    staging, final = _new_output(output_dir)
    try:
        for chunk in built.task.chunks:
            target = staging.joinpath(*chunk.input_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(built.chunks[chunk.id], encoding="utf-8")
        (staging / TASK_FILENAME).write_text(
            json.dumps(
                built.task.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return replace(built.task, task_path=final / TASK_FILENAME)


def _read_text(path: Path, name: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise TranslationError(f"{name} must be a regular file: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_CONTROL_BYTES or b"\0" in raw:
        raise TranslationError(f"{name} must be textual and bounded: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TranslationError(f"{name} must be UTF-8: {path}") from error


def _validate_task_tree(task: TranslationTask) -> None:
    if task.task_dir is None:
        raise TranslationError("loaded task has no task directory")
    expected = {PurePosixPath(TASK_FILENAME)} | {
        chunk.input_path for chunk in task.chunks
    }
    actual: set[PurePosixPath] = set()
    for path in task.task_dir.rglob("*"):
        if path.is_symlink():
            raise TranslationError("translation task must not contain symlinks")
        if path.is_file():
            actual.add(PurePosixPath(path.relative_to(task.task_dir).as_posix()))
    if actual != expected:
        raise TranslationError("translation task files differ from declared inputs")


def _load_agent_outputs(task: TranslationTask, output_dir: Path) -> dict[str, str]:
    root = output_dir.resolve()
    # Artifact transport omits empty directories for deterministic-only tasks.
    if not task.chunks and not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise TranslationError(f"agent output directory is missing: {root}")
    expected = {chunk.output_path for chunk in task.chunks}
    actual: set[PurePosixPath] = set()
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise TranslationError("agent output must contain only regular root files")
        actual.add(PurePosixPath(path.name))
    missing = sorted(path.as_posix() for path in expected - actual)
    extra = sorted(path.as_posix() for path in actual - expected)
    if missing:
        raise TranslationError("agent output is missing chunks: " + ", ".join(missing))
    if extra:
        raise TranslationError("agent output has extra chunks: " + ", ".join(extra))
    outputs: dict[str, str] = {}
    total_bytes = 0
    for chunk in task.chunks:
        path = root / chunk.output_path.as_posix()
        raw = path.read_bytes()
        total_bytes += len(raw)
        if not raw or b"\0" in raw or total_bytes > MAX_AGENT_OUTPUT_BYTES:
            raise TranslationError("agent output is empty, binary, or too large")
        try:
            outputs[chunk.id] = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TranslationError(f"agent output is not UTF-8: {chunk.id}") from error
    return outputs


@dataclass(frozen=True)
class FileCAS:
    before_hash: str | None
    after_hash: str | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], path: str) -> "FileCAS":
        _fields(value, {"before_hash", "after_hash"}, f"artifact file {path}")
        return cls(
            _sha(value["before_hash"], f"files[{path}].before_hash", nullable=True),
            _sha(value["after_hash"], f"files[{path}].after_hash", nullable=True),
        )

    def as_dict(self) -> dict[str, str | None]:
        return {"before_hash": self.before_hash, "after_hash": self.after_hash}


_UNUSED_ARTIFACT_SUMMARY_FIELDS = {
    "translated_pages",
    "translated_chunks",
    "written_files",
    "deleted_files",
    "remaining_pages",
}


@dataclass(frozen=True)
class TranslationArtifact:
    provenance: TranslationProvenance
    identity: TranslationIdentity
    writes: tuple[PurePosixPath, ...]
    deletes: tuple[PurePosixPath, ...]
    files: Mapping[PurePosixPath, FileCAS]
    summary: Mapping[str, int]
    manifest_path: Path | None = field(default=None, compare=False)
    patch_path: Path | None = field(default=None, compare=False)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], *, manifest_path: Path | None = None
    ) -> "TranslationArtifact":
        expected = {
            "schema_version",
            "provenance",
            "identity",
            "write",
            "delete",
            "files",
            "summary",
        }
        _fields(value, expected, "translation artifact")
        if value["schema_version"] != ARTIFACT_SCHEMA_VERSION:
            raise TranslationError("unsupported translation artifact schema")
        if not isinstance(value["provenance"], Mapping) or not isinstance(
            value["identity"], Mapping
        ):
            raise TranslationError("artifact provenance and identity must be objects")
        raw_write, raw_delete = value["write"], value["delete"]
        if not isinstance(raw_write, list) or not isinstance(raw_delete, list):
            raise TranslationError("artifact write/delete must be path lists")
        writes = tuple(_artifact_write(item) for item in raw_write)
        deletes = tuple(_repo_target(item, "artifact delete") for item in raw_delete)
        raw_files, raw_summary = value["files"], value["summary"]
        if not isinstance(raw_files, Mapping) or not isinstance(raw_summary, Mapping):
            raise TranslationError("artifact files/summary must be objects")
        files: dict[PurePosixPath, FileCAS] = {}
        for raw_path, raw_cas in raw_files.items():
            path = PurePosixPath(_non_empty(raw_path, "artifact file path"))
            if path not in set(writes) | set(deletes) or not isinstance(
                raw_cas, Mapping
            ):
                raise TranslationError(f"invalid artifact file entry: {path}")
            files[path] = FileCAS.from_mapping(raw_cas, path.as_posix())
        _fields(raw_summary, _UNUSED_ARTIFACT_SUMMARY_FIELDS, "artifact summary")
        artifact = cls(
            TranslationProvenance.from_mapping(value["provenance"]),
            TranslationIdentity.from_mapping(value["identity"]),
            writes,
            deletes,
            files,
            {
                key: _count(raw_summary[key], f"summary.{key}")
                for key in _UNUSED_ARTIFACT_SUMMARY_FIELDS
            },
            manifest_path,
            manifest_path.parent / PATCH_FILENAME if manifest_path else None,
        )
        artifact.validate()
        return artifact

    @classmethod
    def load(cls, artifact_dir: Path) -> "TranslationArtifact":
        path = artifact_dir.resolve() / ARTIFACT_FILENAME
        return cls.from_mapping(
            _load_json(path, "translation artifact"), manifest_path=path
        )

    def validate(self) -> None:
        if list(self.writes) != sorted(self.writes) or len(self.writes) != len(
            set(self.writes)
        ):
            raise TranslationError("artifact write paths must be sorted and unique")
        if list(self.deletes) != sorted(self.deletes) or len(self.deletes) != len(
            set(self.deletes)
        ):
            raise TranslationError("artifact delete paths must be sorted and unique")
        if set(self.writes) & set(self.deletes):
            raise TranslationError("artifact writes and deletes overlap")
        if set(self.files) != set(self.writes) | set(self.deletes):
            raise TranslationError("artifact files map differs from write/delete")
        for path in self.writes:
            if self.files[path].after_hash is None:
                raise TranslationError(f"write has null after_hash: {path}")
        for path in self.deletes:
            if self.files[path].after_hash is not None:
                raise TranslationError(f"delete has non-null after_hash: {path}")
        if self.summary["written_files"] != len(self.writes):
            raise TranslationError("artifact written_files summary is inconsistent")
        if self.summary["deleted_files"] != len(self.deletes):
            raise TranslationError("artifact deleted_files summary is inconsistent")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "provenance": self.provenance.as_dict(),
            "identity": self.identity.as_dict(),
            "write": [path.as_posix() for path in self.writes],
            "delete": [path.as_posix() for path in self.deletes],
            "files": {
                path.as_posix(): self.files[path].as_dict()
                for path in sorted(self.files)
            },
            "summary": dict(self.summary),
        }

    @property
    def artifact_dir(self) -> Path | None:
        return self.manifest_path.parent if self.manifest_path else None


def _checkout_path(root: Path, repo_path: PurePosixPath) -> Path:
    try:
        relative = repo_path.relative_to(REPOSITORY_PREFIX)
    except ValueError as error:
        raise TranslationError(f"path is outside docs-next: {repo_path}") from error
    target = root.joinpath(*relative.parts)
    existing = target.parent
    while not existing.exists() and existing != root:
        existing = existing.parent
    try:
        existing.resolve().relative_to(root)
    except ValueError as error:
        raise TranslationError(f"path escapes docs-next: {repo_path}") from error
    return target


def _current_text(root: Path, repo_path: PurePosixPath) -> str | None:
    path = _checkout_path(root, repo_path)
    return _read_text(path, "artifact target") if path.exists() else None


def _build_patch(
    writes: Mapping[PurePosixPath, str],
    deletes: Sequence[PurePosixPath],
    before: Mapping[PurePosixPath, str | None],
) -> str:
    touched = sorted(set(writes) | set(deletes))
    if not touched:
        return ""
    with tempfile.TemporaryDirectory(prefix="ucm-translation-patch-") as temporary:
        root = Path(temporary)
        old, new = root / "old", root / "new"
        old.mkdir()
        new.mkdir()
        for path in touched:
            if before[path] is not None:
                target = old.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(before[path], encoding="utf-8")  # type: ignore[arg-type]
            if path in writes:
                target = new.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(writes[path], encoding="utf-8")
        process = subprocess.run(
            [
                "git",
                "diff",
                "--no-index",
                "--no-ext-diff",
                "--no-renames",
                "--text",
                "--",
                "old",
                "new",
            ],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode not in {0, 1}:
            raise TranslationError(
                "git patch generation failed: "
                + process.stderr.decode("utf-8", errors="replace").strip()
            )
        output = process.stdout.decode("utf-8")
    rewritten: list[str] = []
    for line in output.splitlines(keepends=True):
        if line.startswith("diff --git a/old/"):
            line = line.replace("diff --git a/old/", "diff --git a/", 1).replace(
                " b/new/", " b/", 1
            )
        elif line.startswith("--- a/old/"):
            line = line.replace("--- a/old/", "--- a/", 1)
        elif line.startswith("+++ b/new/"):
            line = line.replace("+++ b/new/", "+++ b/", 1)
        rewritten.append(line)
    patch = "".join(rewritten)
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise TranslationError("binary translation patch is forbidden")
    return patch


def _rebuild_task(
    layout: TranslationLayout,
    task: TranslationTask,
    backend: AgentMarkdownBackend | None,
    agent_instructions_path: Path | None,
) -> _TaskBuild:
    glossary = Glossary.load(layout.glossary_path)
    instructions = load_agent_instructions(agent_instructions_path)
    if task.identity.glossary_hash != glossary.hash:
        raise TranslationError("task glossary hash differs from checkout")
    if task.identity.agent_hash != content_hash(instructions):
        raise TranslationError("task agent hash differs from checkout")
    plan = build_translation_plan(
        layout.docs_next_root,
        mode=task.provenance.mode,
        identity=task.identity,
        glossary=glossary,
        base_ref=task.provenance.base_sha,
    )
    return _build_task(
        plan,
        task.provenance,
        backend,
        max_pages=task.summary["page_limit"],
    )


def finalize_translation(
    docs_next_root: Path,
    *,
    task_path: Path,
    agent_output_dir: Path,
    output_dir: Path,
    backend: AgentMarkdownBackend | None = None,
    agent_instructions_path: Path | None = None,
) -> TranslationArtifact:
    """Reconstruct agent chunks and emit a validated artifact plus Git patch."""

    layout = TranslationLayout(docs_next_root)
    task = TranslationTask.load(task_path)
    _validate_task_tree(task)
    rebuilt = _rebuild_task(layout, task, backend, agent_instructions_path)
    if rebuilt.task.as_dict() != task.as_dict():
        raise TranslationError("translation task no longer matches checkout")
    assert task.task_dir is not None
    for chunk in task.chunks:
        actual = _read_text(
            task.task_dir.joinpath(*chunk.input_path.parts), "task chunk"
        )
        if actual != rebuilt.chunks[chunk.id]:
            raise TranslationError(f"task chunk changed: {chunk.id}")
    outputs = _load_agent_outputs(task, agent_output_dir)
    state = rebuilt.plan.state.copy()
    writes: dict[PurePosixPath, str] = {}
    for page, request in zip(task.pages, rebuilt.selected, strict=True):
        content = finalize_markdown_translation(
            request.source,
            [outputs[chunk_id] for chunk_id in page.chunk_ids],
            source_path=page.source_path.as_posix(),
            language_code=TARGET_LANGUAGE,
            preserve_terms=rebuilt.plan.glossary.preserve,
            fixed_terms=rebuilt.plan.glossary.fixed,
            backend=backend,
        )
        writes[page.target_path] = content
        relative = page.source_path.relative_to(REPOSITORY_PREFIX / "docs" / "en")
        state.pages[relative] = _page_state(request.source, content, task.identity)
    # This also emits a state-only artifact for manual ownership transitions.
    if state.pages != rebuilt.plan.initial_state.pages:
        writes[layout.state_repo_path] = state.to_json()
    deletes = task.deletes
    before = {
        path: _current_text(layout.docs_next_root, path)
        for path in sorted(set(writes) | set(deletes))
    }
    files: dict[PurePosixPath, FileCAS] = {}
    for path, content in writes.items():
        files[path] = FileCAS(
            content_hash(before[path]) if before[path] is not None else None,
            content_hash(content),
        )
    for path in deletes:
        files[path] = FileCAS(
            content_hash(before[path]) if before[path] is not None else None,
            None,
        )
    artifact = TranslationArtifact(
        task.provenance,
        task.identity,
        tuple(sorted(writes)),
        tuple(sorted(deletes)),
        files,
        {
            "translated_pages": len(task.pages),
            "translated_chunks": len(task.chunks),
            "written_files": len(writes),
            "deleted_files": len(deletes),
            "remaining_pages": task.summary["remaining_pages"],
        },
    )
    artifact.validate()
    patch = _build_patch(writes, deletes, before)
    staging, final = _new_output(output_dir)
    try:
        for path, content in writes.items():
            target = staging.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (staging / PATCH_FILENAME).write_text(patch, encoding="utf-8")
        (staging / ARTIFACT_FILENAME).write_text(
            json.dumps(artifact.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return replace(
        artifact,
        manifest_path=final / ARTIFACT_FILENAME,
        patch_path=final / PATCH_FILENAME,
    )


def _validate_artifact_tree(
    artifact: TranslationArtifact,
) -> dict[PurePosixPath, str]:
    if artifact.artifact_dir is None:
        raise TranslationError("artifact has no directory")
    expected = {
        PurePosixPath(ARTIFACT_FILENAME),
        PurePosixPath(PATCH_FILENAME),
        *artifact.writes,
    }
    actual: set[PurePosixPath] = set()
    for path in artifact.artifact_dir.rglob("*"):
        if path.is_symlink():
            raise TranslationError("translation artifact must not contain symlinks")
        if path.is_file():
            actual.add(
                PurePosixPath(path.relative_to(artifact.artifact_dir).as_posix())
            )
    if actual != expected:
        raise TranslationError("artifact files differ from manifest.write")
    writes: dict[PurePosixPath, str] = {}
    for path in artifact.writes:
        text = _read_text(artifact.artifact_dir.joinpath(*path.parts), "artifact write")
        if content_hash(text) != artifact.files[path].after_hash:
            raise TranslationError(f"artifact after_hash mismatch: {path}")
        writes[path] = text
    if REPOSITORY_PREFIX / STATE_FILENAME in writes:
        raw = json.loads(
            writes[REPOSITORY_PREFIX / STATE_FILENAME],
            object_pairs_hook=_unique_object,
        )
        if not isinstance(raw, Mapping):
            raise TranslationError("artifact state root must be an object")
        TranslationState.from_mapping(raw)
    return writes


def apply_translation_artifact(
    docs_next_root: Path,
    *,
    artifact_dir: Path,
) -> tuple[Path, ...]:
    """Apply only when checkout HEAD and every before_hash still match."""

    layout = TranslationLayout(docs_next_root)
    artifact = TranslationArtifact.load(artifact_dir)
    writes = _validate_artifact_tree(artifact)
    current_head = resolve_git_sha(layout.docs_next_root, "HEAD")
    if current_head != artifact.provenance.head_sha:
        raise TranslationError(
            f"artifact head CAS failed: expected {artifact.provenance.head_sha}, got {current_head}"
        )
    before: dict[PurePosixPath, str | None] = {}
    for path in sorted(set(artifact.writes) | set(artifact.deletes)):
        current = _current_text(layout.docs_next_root, path)
        current_hash = content_hash(current) if current is not None else None
        if current_hash != artifact.files[path].before_hash:
            raise TranslationError(f"artifact before_hash CAS failed: {path}")
        before[path] = current
    expected_patch = _build_patch(writes, artifact.deletes, before)
    assert artifact.patch_path is not None
    if _read_text(artifact.patch_path, "translation patch") != expected_patch:
        raise TranslationError("translation.patch differs from artifact")
    return _apply(layout.docs_next_root, writes, artifact.deletes)


def _apply(
    root: Path,
    writes: Mapping[PurePosixPath, str],
    deletes: Sequence[PurePosixPath],
) -> tuple[Path, ...]:
    operations = [
        (_checkout_path(root, path), content)
        for path, content in sorted(writes.items())
    ] + [(_checkout_path(root, path), None) for path in sorted(deletes)]
    originals: dict[Path, tuple[bool, bytes, int]] = {}
    created_directories: set[Path] = set()
    for path, _ in operations:
        if path.is_symlink() or path.exists() and not path.is_file():
            raise TranslationError(f"unsafe apply destination: {path}")
        originals[path] = (
            path.exists(),
            path.read_bytes() if path.exists() else b"",
            path.stat().st_mode & 0o777 if path.exists() else 0o644,
        )
    applied: list[Path] = []
    try:
        for path, content in operations:
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                parent = path.parent
                missing: list[Path] = []
                while not parent.exists() and parent != root:
                    missing.append(parent)
                    parent = parent.parent
                path.parent.mkdir(parents=True, exist_ok=True)
                created_directories.update(missing)
                _atomic_write_bytes(path, content.encode("utf-8"), originals[path][2])
            applied.append(path)
    except BaseException as error:
        try:
            for path in reversed(applied):
                existed, content, mode = originals[path]
                if existed:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write_bytes(path, content, mode)
                elif path.exists():
                    path.unlink()
            for directory in sorted(
                created_directories,
                key=lambda value: len(value.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except BaseException as rollback_error:
            raise TranslationError(
                f"apply failed and rollback was incomplete: {rollback_error}"
            ) from error
        raise
    return tuple(path for path, _ in operations)
