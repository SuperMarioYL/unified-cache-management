"""Repository-level orchestration for English-to-Chinese documentation.

This module owns file selection, translation state, and freshness checks. It
does not read model credentials or call provider APIs. The API executor consumes
the deterministic hand-off defined in ``agent_pipeline``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal, Mapping, Sequence

from .markdown import (
    AgentMarkdownBackend,
    MarkdownTranslationError,
    validate_reconstructed_markdown,
)

STATE_SCHEMA_VERSION = 2
GLOSSARY_SCHEMA_VERSION = 1
TARGET_LANGUAGE = "zh-CN"

STATE_FILENAME = "translation-state.json"
GLOSSARY_FILENAME = "translation-glossary.json"
REPOSITORY_PREFIX = PurePosixPath("docs-next")
MAX_CONFIGURATION_BYTES = 2 * 1024 * 1024


class TranslationError(RuntimeError):
    """Raised when translation selection or state is invalid."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TranslationError(f"JSON contains duplicate field {key!r}")
        value[key] = item
    return value


def _load_control_json(path: Path, name: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise TranslationError(f"{name} must be a regular file: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_CONFIGURATION_BYTES:
        raise TranslationError(f"{name} is too large")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranslationError(f"invalid {name} JSON: {error}") from error
    if not isinstance(payload, Mapping):
        raise TranslationError(f"{name} root must be an object")
    return payload


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(content: str) -> str:
    """Return the canonical prefixed SHA-256 for UTF-8 content."""

    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def canonical_hash(value: object) -> str:
    """Hash a JSON-compatible value with stable ordering and encoding."""

    return content_hash(_canonical_json(value))


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranslationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _relative_markdown_path(raw_path: object, field_name: str) -> PurePosixPath:
    value = _non_empty_string(raw_path, field_name)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or path.suffix.lower() != ".md"
    ):
        raise TranslationError(f"{field_name} must be a normalized relative .md path")
    return path


@dataclass(frozen=True)
class TranslationIdentity:
    """Non-secret agent identity stored in task, state, and artifact."""

    provider: str
    model: str
    endpoint_hash: str
    agent_hash: str
    glossary_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider", _non_empty_string(self.provider, "provider")
        )
        object.__setattr__(self, "model", _non_empty_string(self.model, "model"))
        for name in ("endpoint_hash", "agent_hash", "glossary_hash"):
            value = _non_empty_string(getattr(self, name), name)
            if not re_full_sha256(value):
                raise TranslationError(f"{name} must be an exact sha256")
            object.__setattr__(self, name, value)

    @classmethod
    def from_runtime(
        cls,
        *,
        provider: str,
        model: str,
        base_url: str,
        agent_instructions: str,
        glossary: "Glossary",
        api_host: str | None = None,
    ) -> "TranslationIdentity":
        normalized_url = _validate_base_url(base_url, api_host=api_host)
        return cls(
            provider=provider,
            model=model,
            endpoint_hash=canonical_hash({"base_url": normalized_url}),
            agent_hash=content_hash(agent_instructions),
            glossary_hash=glossary.hash,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint_hash": self.endpoint_hash,
            "agent_hash": self.agent_hash,
            "glossary_hash": self.glossary_hash,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TranslationIdentity":
        expected = {
            "provider",
            "model",
            "endpoint_hash",
            "agent_hash",
            "glossary_hash",
        }
        if set(payload) != expected:
            raise TranslationError("translation identity fields differ")
        return cls(**payload)  # type: ignore[arg-type]


def _validate_base_url(value: object, *, api_host: str | None = None) -> str:
    base_url = _non_empty_string(value, "base_url").rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise TranslationError(
            "base_url must be http(s) without credentials, query, or fragment"
        )
    if api_host is not None:
        expected_host = _non_empty_string(api_host, "api_host").lower()
        if parsed.hostname.lower() != expected_host:
            raise TranslationError(
                f"base_url hostname {parsed.hostname!r} differs from api_host"
            )
    return base_url


@dataclass(frozen=True)
class Glossary:
    """Versioned terminology policy used in prompts and deterministic checks."""

    preserve: tuple[str, ...] = ()
    fixed: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        preserve: list[str] = []
        seen: set[str] = set()
        for index, raw_term in enumerate(self.preserve):
            term = _non_empty_string(raw_term, f"preserve[{index}]")
            if term in seen:
                raise TranslationError(f"duplicate preserved glossary term: {term}")
            seen.add(term)
            preserve.append(term)

        fixed: dict[str, str] = {}
        if not isinstance(self.fixed, Mapping):
            raise TranslationError("fixed glossary terms must be an object")
        for raw_source, raw_target in self.fixed.items():
            source = _non_empty_string(raw_source, "fixed glossary source")
            target = _non_empty_string(raw_target, f"fixed[{source}]")
            if source in fixed:
                raise TranslationError(f"duplicate fixed glossary term: {source}")
            if source in seen:
                raise TranslationError(
                    f"glossary term cannot be both preserved and fixed: {source}"
                )
            fixed[source] = target

        object.__setattr__(self, "preserve", tuple(sorted(preserve)))
        object.__setattr__(self, "fixed", dict(sorted(fixed.items())))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "Glossary":
        expected = {"schema_version", "preserve", "fixed"}
        if set(payload) != expected:
            raise TranslationError(
                "translation glossary fields must be exactly: "
                + ", ".join(sorted(expected))
            )
        if payload.get("schema_version") != GLOSSARY_SCHEMA_VERSION:
            raise TranslationError(
                f"unsupported translation glossary schema: {payload.get('schema_version')}"
            )
        preserve = payload.get("preserve")
        fixed = payload.get("fixed")
        if not isinstance(preserve, list) or not all(
            isinstance(term, str) for term in preserve
        ):
            raise TranslationError(
                "translation glossary preserve must be a string list"
            )
        if not isinstance(fixed, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in fixed.items()
        ):
            raise TranslationError(
                "translation glossary fixed must map strings to strings"
            )
        return cls(tuple(preserve), dict(fixed))

    @classmethod
    def load(cls, path: Path) -> "Glossary":
        if not path.exists():
            raise TranslationError(f"translation glossary not found: {path}")
        return cls.from_mapping(_load_control_json(path, "translation glossary"))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": GLOSSARY_SCHEMA_VERSION,
            "preserve": list(self.preserve),
            "fixed": dict(self.fixed),
        }

    @property
    def hash(self) -> str:
        return canonical_hash(self.as_dict())

    @property
    def instruction(self) -> str:
        lines = [
            "Apply these repository terminology rules exactly. "
            "Do not translate protected terms and use every fixed translation "
            "whenever its English source term appears in translatable prose."
        ]
        if self.preserve:
            lines.append("Preserve exactly: " + ", ".join(self.preserve))
        if self.fixed:
            lines.append("Fixed translations:")
            lines.extend(
                f"- {source} => {target}" for source, target in self.fixed.items()
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class PageState:
    source_hash: str
    target_hash: str
    provider: str
    model: str
    endpoint_hash: str
    agent_hash: str
    glossary_hash: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], path: str) -> "PageState":
        expected = {
            "source_hash",
            "target_hash",
            "provider",
            "model",
            "endpoint_hash",
            "agent_hash",
            "glossary_hash",
        }
        if set(payload) != expected:
            raise TranslationError(
                f"translation state entry {path} has unexpected fields"
            )
        values = {
            key: _non_empty_string(payload.get(key), f"state {path}.{key}")
            for key in expected
        }
        for key in (
            "source_hash",
            "target_hash",
            "endpoint_hash",
            "agent_hash",
            "glossary_hash",
        ):
            value = values[key]
            if not re_full_sha256(value):
                raise TranslationError(f"state {path}.{key} must be an exact sha256")
        return cls(**values)

    def as_dict(self) -> dict[str, str]:
        return {
            "source_hash": self.source_hash,
            "target_hash": self.target_hash,
            "provider": self.provider,
            "model": self.model,
            "endpoint_hash": self.endpoint_hash,
            "agent_hash": self.agent_hash,
            "glossary_hash": self.glossary_hash,
        }


def re_full_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != len("sha256:") + 64:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


@dataclass
class TranslationState:
    pages: dict[PurePosixPath, PageState] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TranslationState":
        if not isinstance(payload, Mapping):
            raise TranslationError("translation state root must be an object")
        if set(payload) != {"schema_version", "pages"}:
            raise TranslationError(
                "translation state fields must be exactly: pages, schema_version"
            )
        if payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise TranslationError(
                f"unsupported translation state schema: {payload.get('schema_version')}"
            )
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, Mapping):
            raise TranslationError("translation state pages must be an object")
        pages: dict[PurePosixPath, PageState] = {}
        for raw_path, raw_state in raw_pages.items():
            path_key = _relative_markdown_path(raw_path, "translation state page")
            if not isinstance(raw_state, Mapping):
                raise TranslationError(
                    f"translation state entry {path_key} must be an object"
                )
            pages[path_key] = PageState.from_mapping(raw_state, path_key.as_posix())
        return cls(pages)

    @classmethod
    def load(cls, path: Path) -> "TranslationState":
        if not path.exists():
            return cls()
        return cls.from_mapping(_load_control_json(path, "translation state"))

    def copy(self) -> "TranslationState":
        return TranslationState(dict(self.pages))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "pages": {
                path.as_posix(): self.pages[path].as_dict()
                for path in sorted(self.pages)
            },
        }

    def to_json(self) -> str:
        return (
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )


@dataclass(frozen=True)
class TranslationLayout:
    docs_next_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "docs_next_root", self.docs_next_root.resolve())

    @property
    def source_root(self) -> Path:
        return self.docs_next_root / "docs" / "en"

    @property
    def target_root(self) -> Path:
        return self.docs_next_root / "docs" / "zh"

    @property
    def state_path(self) -> Path:
        return self.docs_next_root / STATE_FILENAME

    @property
    def glossary_path(self) -> Path:
        return self.docs_next_root / GLOSSARY_FILENAME

    def source_path(self, relative: PurePosixPath) -> Path:
        return self.source_root.joinpath(*relative.parts)

    def source_repo_path(self, relative: PurePosixPath) -> PurePosixPath:
        return REPOSITORY_PREFIX / "docs" / "en" / relative

    def target_path(self, relative: PurePosixPath) -> Path:
        return self.target_root.joinpath(*relative.parts)

    def target_repo_path(self, relative: PurePosixPath) -> PurePosixPath:
        return REPOSITORY_PREFIX / "docs" / "zh" / relative

    @property
    def state_repo_path(self) -> PurePosixPath:
        return REPOSITORY_PREFIX / STATE_FILENAME

    def markdown_sources(self) -> frozenset[PurePosixPath]:
        return _markdown_relatives(self.source_root)

    def markdown_targets(self) -> frozenset[PurePosixPath]:
        return _markdown_relatives(self.target_root)


def _markdown_relatives(root: Path) -> frozenset[PurePosixPath]:
    if not root.exists():
        return frozenset()
    return frozenset(
        PurePosixPath(path.relative_to(root).as_posix())
        for path in root.rglob("*.md")
        if path.is_file()
    )


@dataclass(frozen=True)
class ChangeSet:
    source_changed: frozenset[PurePosixPath] = frozenset()
    source_deleted: frozenset[PurePosixPath] = frozenset()
    source_renames: Mapping[PurePosixPath, PurePosixPath] = field(default_factory=dict)
    target_changed: frozenset[PurePosixPath] = frozenset()
    target_deleted: frozenset[PurePosixPath] = frozenset()
    target_renames: Mapping[PurePosixPath, PurePosixPath] = field(default_factory=dict)

    @property
    def target_touched(self) -> frozenset[PurePosixPath]:
        return self.target_changed | self.target_deleted


def _run_git(git_root: Path, arguments: Sequence[str]) -> bytes:
    process = subprocess.run(
        ["git", *arguments],
        cwd=git_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise TranslationError(f"git command failed: {message}")
    return process.stdout


def _git_root(docs_next_root: Path) -> Path:
    output = _run_git(docs_next_root, ["rev-parse", "--show-toplevel"])
    return Path(os.fsdecode(output).strip()).resolve()


def _resolve_base_ref(git_root: Path, base_ref: str) -> str:
    reference = _non_empty_string(base_ref, "base_ref")
    output = _run_git(
        git_root,
        ["rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"],
    )
    return os.fsdecode(output).strip()


def _parse_name_status(output: bytes) -> tuple[tuple[str, str, str | None], ...]:
    tokens = output.rstrip(b"\0").split(b"\0") if output else []
    changes: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(tokens):
        status = os.fsdecode(tokens[index])
        index += 1
        if not status:
            raise TranslationError("git returned an empty change status")
        kind = status[0]
        if kind in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise TranslationError("git returned an incomplete rename record")
            old_path = os.fsdecode(tokens[index])
            new_path = os.fsdecode(tokens[index + 1])
            index += 2
            changes.append((kind, new_path, old_path))
        else:
            if index >= len(tokens):
                raise TranslationError("git returned an incomplete change record")
            path = os.fsdecode(tokens[index])
            index += 1
            changes.append((kind, path, None))
    return tuple(changes)


def collect_git_changes(
    docs_next_root: Path,
    *,
    base_ref: str = "HEAD",
) -> ChangeSet:
    """Collect committed, staged, unstaged, and untracked documentation changes."""

    layout = TranslationLayout(docs_next_root)
    git_root = _git_root(layout.docs_next_root)
    try:
        docs_prefix = PurePosixPath(
            layout.docs_next_root.relative_to(git_root).as_posix()
        )
    except ValueError as error:
        raise TranslationError("docs-next root is outside the git worktree") from error

    source_prefix = docs_prefix / "docs" / "en"
    target_prefix = docs_prefix / "docs" / "zh"
    commit = _resolve_base_ref(git_root, base_ref)
    pathspecs = [source_prefix.as_posix(), target_prefix.as_posix()]
    diff = _run_git(
        git_root,
        ["diff", "--name-status", "-z", "-M", commit, "--", *pathspecs],
    )
    untracked = _run_git(
        git_root,
        ["ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs],
    )

    source_changed: set[PurePosixPath] = set()
    source_deleted: set[PurePosixPath] = set()
    source_renames: dict[PurePosixPath, PurePosixPath] = {}
    target_changed: set[PurePosixPath] = set()
    target_deleted: set[PurePosixPath] = set()
    target_renames: dict[PurePosixPath, PurePosixPath] = {}

    def relative(path_string: str, prefix: PurePosixPath) -> PurePosixPath | None:
        path = PurePosixPath(path_string)
        try:
            candidate = path.relative_to(prefix)
        except ValueError:
            return None
        if candidate.suffix.lower() != ".md":
            return None
        return candidate

    def record(status: str, new_path: str, old_path: str | None) -> None:
        for prefix, changed, deleted, renames in (
            (source_prefix, source_changed, source_deleted, source_renames),
            (target_prefix, target_changed, target_deleted, target_renames),
        ):
            new_relative = relative(new_path, prefix)
            old_relative = relative(old_path, prefix) if old_path is not None else None
            if status == "R":
                if old_relative is not None:
                    deleted.add(old_relative)
                if new_relative is not None:
                    changed.add(new_relative)
                if old_relative is not None and new_relative is not None:
                    renames[new_relative] = old_relative
                continue
            if status == "C":
                if new_relative is not None:
                    changed.add(new_relative)
                continue
            if new_relative is not None and status == "D":
                deleted.add(new_relative)
            elif new_relative is not None:
                changed.add(new_relative)

    for status, new_path, old_path in _parse_name_status(diff):
        record(status, new_path, old_path)
    for raw_path in untracked.rstrip(b"\0").split(b"\0") if untracked else ():
        record("A", os.fsdecode(raw_path), None)

    return ChangeSet(
        source_changed=frozenset(source_changed),
        source_deleted=frozenset(source_deleted),
        source_renames=source_renames,
        target_changed=frozenset(target_changed),
        target_deleted=frozenset(target_deleted),
        target_renames=target_renames,
    )


@dataclass(frozen=True)
class PageRequest:
    relative_path: PurePosixPath
    source: str


@dataclass
class TranslationPlan:
    layout: TranslationLayout
    initial_state: TranslationState
    state: TranslationState
    requests: tuple[PageRequest, ...]
    deletes: tuple[PurePosixPath, ...]
    skipped_manual: tuple[PurePosixPath, ...]
    glossary: Glossary
    identity: TranslationIdentity


def _page_state(
    source: str,
    target: str,
    identity: TranslationIdentity,
) -> PageState:
    return PageState(
        source_hash=content_hash(source),
        target_hash=content_hash(target),
        provider=identity.provider,
        model=identity.model,
        endpoint_hash=identity.endpoint_hash,
        agent_hash=identity.agent_hash,
        glossary_hash=identity.glossary_hash,
    )


def _configuration_matches(
    page: PageState,
    identity: TranslationIdentity,
) -> bool:
    return (
        page.provider == identity.provider
        and page.model == identity.model
        and page.endpoint_hash == identity.endpoint_hash
        and page.agent_hash == identity.agent_hash
        and page.glossary_hash == identity.glossary_hash
    )


def build_translation_plan(
    docs_next_root: Path,
    *,
    mode: Literal["changed", "missing"],
    identity: TranslationIdentity,
    glossary: Glossary,
    changes: ChangeSet | None = None,
    base_ref: str = "HEAD",
) -> TranslationPlan:
    """Select pages while preserving manual Chinese ownership."""

    if mode not in {"changed", "missing"}:
        raise TranslationError(f"unsupported translation mode: {mode}")
    if identity.glossary_hash != glossary.hash:
        raise TranslationError("identity glossary_hash differs from glossary")
    layout = TranslationLayout(docs_next_root)
    selected_changes = (
        changes
        if changes is not None
        else (
            collect_git_changes(layout.docs_next_root, base_ref=base_ref)
            if mode == "changed"
            else ChangeSet()
        )
    )
    sources = layout.markdown_sources()
    initial_state = TranslationState.load(layout.state_path)
    state = initial_state.copy()

    deletes: set[PurePosixPath] = set()
    skipped_manual: set[PurePosixPath] = set()
    requests: dict[PurePosixPath, PageRequest] = {}

    # Remove only robot-owned targets when a managed source disappears.  A
    # target whose content no longer matches state is treated as human-owned.
    for relative_path, page in tuple(state.pages.items()):
        if relative_path in sources:
            continue
        state.pages.pop(relative_path)
        target_path = layout.target_path(relative_path)
        if target_path.exists():
            target = target_path.read_text(encoding="utf-8")
            if content_hash(target) != page.target_hash:
                skipped_manual.add(relative_path)
                continue
        deletes.add(layout.target_repo_path(relative_path))

    # A generated target and matching state are both touched when the patch is
    # applied to the PR. Retain that ownership so the next run is a true no-op.
    # Only a hash-diverged target becomes human-owned.
    for relative_path in sorted(selected_changes.target_touched & sources):
        page = state.pages.get(relative_path)
        source_path = layout.source_path(relative_path)
        target_path = layout.target_path(relative_path)
        if not target_path.exists():
            state.pages.pop(relative_path, None)
            skipped_manual.add(relative_path)
            continue
        source = source_path.read_text(encoding="utf-8")
        target = target_path.read_text(encoding="utf-8")
        if page is not None and (page.target_hash == content_hash(target)):
            continue
        state.pages.pop(relative_path, None)
        skipped_manual.add(relative_path)

    # Scan all managed state, not only the git diff.  This recovers interrupted
    # jobs and makes provider/model/endpoint/prompt/glossary changes effective.
    for relative_path, page in tuple(state.pages.items()):
        if relative_path not in sources:
            continue
        source = layout.source_path(relative_path).read_text(encoding="utf-8")
        target_path = layout.target_path(relative_path)
        if not target_path.exists():
            requests[relative_path] = PageRequest(relative_path, source)
            continue
        target = target_path.read_text(encoding="utf-8")
        if content_hash(target) != page.target_hash:
            state.pages.pop(relative_path)
            skipped_manual.add(relative_path)
            continue
        if mode == "changed" and (
            content_hash(source) != page.source_hash
            or not _configuration_matches(page, identity)
        ):
            requests[relative_path] = PageRequest(relative_path, source)

    if mode == "changed":
        candidates: Iterable[PurePosixPath] = selected_changes.source_changed
    else:
        candidates = sources

    for relative_path in sorted(candidates):
        if relative_path not in sources or relative_path in requests:
            continue
        if relative_path in state.pages:
            continue
        if mode == "changed" and relative_path in selected_changes.target_touched:
            continue
        source = layout.source_path(relative_path).read_text(encoding="utf-8")
        target_path = layout.target_path(relative_path)
        if target_path.exists():
            skipped_manual.add(relative_path)
            continue
        requests[relative_path] = PageRequest(relative_path, source)

    return TranslationPlan(
        layout=layout,
        initial_state=initial_state,
        state=state,
        requests=tuple(requests[path] for path in sorted(requests)),
        deletes=tuple(sorted(deletes)),
        skipped_manual=tuple(sorted(skipped_manual)),
        glossary=glossary,
        identity=identity,
    )


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class TranslationIssue:
    path: PurePosixPath
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class TranslationCheckResult:
    checked_files: int
    issues: tuple[TranslationIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.blocking_issues

    @property
    def blocking_issues(self) -> tuple[TranslationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)


def _required_pages(docs_next_root: Path) -> frozenset[PurePosixPath] | None:
    path = docs_next_root / "translation-required.txt"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise TranslationError("translation-required.txt must be a regular file")
    pages = [
        _relative_markdown_path(line.strip(), "required translation")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not pages or len(set(pages)) != len(pages):
        raise TranslationError("required translations must be non-empty and unique")
    return frozenset(pages)


def check_translations(
    docs_next_root: Path,
    *,
    glossary: Glossary | None = None,
    identity: TranslationIdentity | None = None,
    base_ref: str | None = None,
    changes: ChangeSet | None = None,
    backend: AgentMarkdownBackend | None = None,
) -> TranslationCheckResult:
    """Check missing, stale, and structurally unsafe translations read-only."""

    layout = TranslationLayout(docs_next_root)
    selected_glossary = glossary or Glossary.load(layout.glossary_path)
    state = TranslationState.load(layout.state_path)
    sources = layout.markdown_sources()
    targets = layout.markdown_targets()

    selected_changes: ChangeSet | None = None
    if base_ref is None and changes is None:
        scope = set(sources)
        scope.update(state.pages)
    else:
        selected_changes = changes or collect_git_changes(
            layout.docs_next_root, base_ref=base_ref or "HEAD"
        )
        scope = set(selected_changes.source_changed)
        scope.update(selected_changes.source_deleted)
        scope.update(selected_changes.target_touched)
        # Managed state is always audited so stale work cannot fall out of a
        # later git-diff window after an interrupted translation run.
        scope.update(state.pages)

    required = _required_pages(layout.docs_next_root)
    if required is not None:
        if missing := required - sources:
            raise TranslationError(
                f"required translation source is missing: {sorted(missing)}"
            )
        scope.update(required)

    issues: list[TranslationIssue] = []
    checked = 0
    for relative_path in sorted(scope):
        source_exists = relative_path in sources
        target_exists = relative_path in targets
        page = state.pages.get(relative_path)

        if not source_exists:
            if page is not None:
                issues.append(
                    TranslationIssue(
                        relative_path,
                        "managed-source-missing",
                        "managed source is missing; translation cleanup has not run",
                    )
                )
            elif target_exists and (
                selected_changes is not None
                and relative_path in selected_changes.source_deleted
                and relative_path not in selected_changes.target_deleted
            ):
                issues.append(
                    TranslationIssue(
                        relative_path,
                        "manual-source-missing",
                        "English source was deleted but the manual Chinese page remains",
                    )
                )
            continue
        if not target_exists:
            issues.append(
                TranslationIssue(
                    relative_path,
                    "translation-missing",
                    "Chinese mirror page is missing",
                )
            )
            continue

        checked += 1
        source = layout.source_path(relative_path).read_text(encoding="utf-8")
        target = layout.target_path(relative_path).read_text(encoding="utf-8")
        target_touched = selected_changes is not None and (
            relative_path in selected_changes.target_touched
        )
        source_matches = page is not None and page.source_hash == content_hash(source)
        target_matches = page is not None and page.target_hash == content_hash(target)
        # Ownership follows the target hash; freshness follows the source hash.
        # A stale source must not turn an unchanged machine translation manual.
        managed = page is not None and target_matches
        if not managed:
            if page is not None:
                issues.append(
                    TranslationIssue(
                        relative_path,
                        "manual-ownership-state",
                        "Chinese content was manually changed but its managed state remains",
                    )
                )
            if (
                selected_changes is not None
                and relative_path in selected_changes.source_changed
                and not target_touched
            ):
                issues.append(
                    TranslationIssue(
                        relative_path,
                        "manual-translation-stale",
                        "English source changed but the unmanaged Chinese page did not",
                    )
                )
            continue
        if not source_matches:
            issues.append(
                TranslationIssue(
                    relative_path,
                    "translation-stale",
                    "English source differs from the translated source",
                )
            )
        glossary_matches = page.glossary_hash == selected_glossary.hash
        if glossary_matches and source_matches:
            try:
                validate_reconstructed_markdown(
                    source,
                    target,
                    source_path=(
                        REPOSITORY_PREFIX / "docs" / "en" / relative_path
                    ).as_posix(),
                    language_code=TARGET_LANGUAGE,
                    preserve_terms=selected_glossary.preserve,
                    fixed_terms=selected_glossary.fixed,
                    backend=backend,
                )
            except MarkdownTranslationError as error:
                issues.append(
                    TranslationIssue(relative_path, "markdown-invalid", str(error))
                )

        if not glossary_matches:
            issues.append(
                TranslationIssue(
                    relative_path,
                    "glossary-stale",
                    "translation glossary changed",
                )
            )
        if identity is not None and not (
            page.provider == identity.provider
            and page.model == identity.model
            and page.endpoint_hash == identity.endpoint_hash
        ):
            issues.append(
                TranslationIssue(
                    relative_path,
                    "provider-stale",
                    "translation provider, model, or endpoint changed",
                )
            )
        if identity is not None and page.agent_hash != identity.agent_hash:
            issues.append(
                TranslationIssue(
                    relative_path,
                    "agent-stale",
                    "translation agent instructions changed",
                )
            )

    if required is not None:
        content_issues = {
            "translation-missing",
            "translation-stale",
            "manual-translation-stale",
            "glossary-stale",
            "provider-stale",
            "agent-stale",
        }
        issues = [
            (
                replace(issue, blocking=False)
                if issue.path not in required and issue.code in content_issues
                else issue
            )
            for issue in issues
        ]
    return TranslationCheckResult(checked, tuple(issues))
