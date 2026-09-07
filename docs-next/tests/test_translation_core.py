from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

DOCS_NEXT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = DOCS_NEXT_ROOT / "tools"
AGENT_INSTRUCTIONS_PATH = TOOLS_ROOT / "translation" / "instructions.md"
sys.path.insert(0, str(TOOLS_ROOT))

from translation import agent_pipeline as pipeline  # noqa: E402
from translation import core  # noqa: E402
from translation import markdown  # noqa: E402


class FakeMarkdownBackend:
    """Small Co-op-shaped fake; production imports remain lazy in tests."""

    split_marker = "\n<!-- split -->\n"

    def start(self, document: str, language_code: str, source_path: str):
        parts = document.split(self.split_marker)
        fenced = tuple(
            match.group(0)
            for match in re.finditer(
                r"(?ms)^ {0,3}(```+|~~~+).*?^ {0,3}\1[ \t]*(?:\n|$)", document
            )
        )
        destinations = tuple(
            match.group(1) for match in re.finditer(r"\]\(([^)#][^)]*)\)", document)
        )
        return {
            "job_type": "markdown_agent_translation",
            "chunks": [
                {
                    "id": f"body:{index}",
                    "source": part,
                    "prompt": (
                        f"Translate {source_path} part {index}\n\n"
                        "===SYSTEM_USER_SPLIT===\n\n"
                        f"{part}"
                    ),
                }
                for index, part in enumerate(parts, start=1)
            ],
            "state": {
                "placeholder_map": {
                    f"@@CODE_BLOCK_{index}@@": value
                    for index, value in enumerate(fenced)
                },
                "link_destination_map": {
                    f"@@LINK_DESTINATION_{index}@@": value
                    for index, value in enumerate(destinations)
                },
                "frontmatter_link_destination_map": {},
            },
        }

    def finish(self, job, translated_chunks):
        translated = {
            item["chunk_id"]: item["translated_text"] for item in translated_chunks
        }
        return {
            "content": self.split_marker.join(
                translated[chunk["id"]] for chunk in job["chunks"]
            ),
            "warnings": [],
        }


def _run(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _new_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    docs_next = repo / "docs-next"
    docs_next.mkdir(parents=True)
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "translation@example.invalid")
    _run(repo, "config", "user.name", "Translation Test")
    _write(
        docs_next / core.GLOSSARY_FILENAME,
        json.dumps(
            {
                "schema_version": 1,
                "preserve": ["UCM"],
                "fixed": {"Prefix Cache": "前缀缓存"},
            }
        ),
    )
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps({"schema_version": 2, "pages": {}}),
    )
    return repo, docs_next


def _translated_markdown(source: str, *, corrupt=None) -> str:
    backend = FakeMarkdownBackend()
    prepared = markdown.prepare_markdown_translation(
        source,
        source_path="docs-next/docs/en/guide.md",
        language_code="zh-CN",
        backend=backend,
    )
    translated = []
    for chunk in prepared.chunks:
        text = (
            chunk.text.replace("Prefix Cache", "前缀缓存")
            .replace("Hello", "你好")
            .replace("Guide", "指南")
            .replace("Description", "说明")
            .replace("Use", "使用")
            .replace("Item", "项目")
            .replace("Value", "值")
        )
        translated.append(corrupt(text) if corrupt is not None else text)
    return markdown.finalize_markdown_translation(
        source,
        translated,
        source_path="docs-next/docs/en/guide.md",
        language_code="zh-CN",
        preserve_terms=("UCM",),
        fixed_terms={"Prefix Cache": "前缀缓存"},
        backend=backend,
    )


def test_markdown_prepare_and_finalize_are_provider_free():
    result = _translated_markdown("# Hello UCM\n<!-- split -->\nUse Prefix Cache.\n")
    assert "你好 UCM" in result
    assert "前缀缓存" in result


def test_markdown_rejects_coop_prompt_without_trust_boundary():
    class UnsafePromptBackend(FakeMarkdownBackend):
        def start(self, document: str, language_code: str, source_path: str):
            job = super().start(document, language_code, source_path)
            job["chunks"][0]["prompt"] = "Translate trusted rules and source together"
            return job

    with pytest.raises(markdown.MarkdownTranslationError, match="trust boundary"):
        markdown.prepare_markdown_translation(
            "Hello UCM",
            source_path="docs-next/docs/en/guide.md",
            language_code="zh-CN",
            backend=UnsafePromptBackend(),
        )


MARKDOWN_FIXTURE = """---
title: Guide
---

# Guide for UCM

!!! note "Guide"

    Use `ucm --help`.

=== "Guide"

    Description.

| Item | Value |
| --- | --- |
| Prefix Cache | UCM |

<div class="hero" data-kind="docs" title="Guide">Description</div>

--8<-- "includes/example.md"

[Guide](https://example.com/guide)

```mermaid
graph TD
    A[UCM] --> B[Cache]
```
"""


def test_markdown_preserves_material_html_tables_links_and_mermaid():
    result = _translated_markdown(MARKDOWN_FIXTURE)

    assert '<div class="hero" data-kind="docs" title="指南">说明</div>' in result
    assert '!!! note "指南"' in result
    assert "| 前缀缓存 | UCM |" in result
    assert '--8<-- "includes/example.md"' in result
    assert "https://example.com/guide" in result
    assert "graph TD\n    A[UCM] --> B[Cache]" in result


@pytest.mark.parametrize(
    "corrupt, message",
    [
        (lambda text: text.replace("`ucm --help`", "`ucm help`"), "inline code"),
        (lambda text: text.replace('class="hero"', 'class="banner"'), "HTML"),
        (lambda text: text.replace("!!! note", "!!! warning"), "Material"),
        (lambda text: text.replace("| --- | --- |", "| --- |"), "table"),
        (lambda text: text.replace("A[UCM] --> B[Cache]", "A --> B"), "code"),
        (
            lambda text: text.replace(
                "https://example.com/guide", "https://evil.invalid"
            ),
            "link",
        ),
        (lambda text: text + "\n[Injected](javascript:alert(1))\n", "link"),
    ],
)
def test_markdown_rejects_structural_corruption(corrupt, message):
    with pytest.raises(markdown.MarkdownTranslationError, match=message):
        _translated_markdown(MARKDOWN_FIXTURE, corrupt=corrupt)


def test_collect_git_changes_covers_add_modify_rename_delete_and_chinese(tmp_path):
    repo, docs_next = _new_repo(tmp_path)
    for name in ("modify.md", "delete.md", "rename.md"):
        _write(docs_next / "docs" / "en" / name, f"# {name}\n")
    _write(docs_next / "docs" / "zh" / "modify.md", "# 原文\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "baseline")

    _write(docs_next / "docs" / "en" / "modify.md", "# modified\n")
    (docs_next / "docs" / "en" / "delete.md").unlink()
    _run(
        repo,
        "mv",
        "docs-next/docs/en/rename.md",
        "docs-next/docs/en/renamed.md",
    )
    _write(docs_next / "docs" / "en" / "added.md", "# added\n")
    _write(docs_next / "docs" / "zh" / "modify.md", "# 人工修改\n")

    changes = core.collect_git_changes(docs_next, base_ref="HEAD")

    assert changes.source_changed == {
        PurePosixPath("modify.md"),
        PurePosixPath("renamed.md"),
        PurePosixPath("added.md"),
    }
    assert changes.source_deleted == {
        PurePosixPath("delete.md"),
        PurePosixPath("rename.md"),
    }
    assert changes.source_renames == {
        PurePosixPath("renamed.md"): PurePosixPath("rename.md")
    }
    assert changes.target_changed == {PurePosixPath("modify.md")}


def _new_v2_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo-v2"
    docs_next = repo / "docs-next"
    docs_next.mkdir(parents=True)
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "translation@example.invalid")
    _run(repo, "config", "user.name", "Translation Test")
    _write(
        docs_next / core.GLOSSARY_FILENAME,
        json.dumps(
            {
                "schema_version": 1,
                "preserve": ["UCM"],
                "fixed": {"Prefix Cache": "前缀缓存"},
            }
        )
        + "\n",
    )
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps({"schema_version": 2, "pages": {}}) + "\n",
    )
    return repo, docs_next


def _runtime_identity(docs_next: Path):
    glossary = core.Glossary.load(docs_next / core.GLOSSARY_FILENAME)
    return core.TranslationIdentity.from_runtime(
        provider="openai",
        model="test-model",
        base_url="https://models.example.invalid/v1",
        api_host="models.example.invalid",
        agent_instructions=pipeline.load_agent_instructions(AGENT_INSTRUCTIONS_PATH),
        glossary=glossary,
    )


def test_runtime_identity_requires_base_url_host_to_match_api_host(
    tmp_path: Path,
) -> None:
    _, docs_next = _new_v2_repo(tmp_path)
    glossary = core.Glossary.load(docs_next / core.GLOSSARY_FILENAME)

    with pytest.raises(core.TranslationError, match="differs from api_host"):
        core.TranslationIdentity.from_runtime(
            provider="openai",
            model="test-model",
            base_url="https://models.example.invalid/v1",
            api_host="other.example.invalid",
            agent_instructions=pipeline.load_agent_instructions(
                AGENT_INSTRUCTIONS_PATH
            ),
            glossary=glossary,
        )


def _provenance(*, mode: str, base_sha: str, head_sha: str):
    return pipeline.TranslationProvenance(
        mode=mode,
        base_sha=base_sha,
        head_sha=head_sha,
        base_repository_id="100",
        head_repository_id="200",
        pr_number=7,
    )


def _prepare_changed_page(tmp_path: Path):
    repo, docs_next = _new_v2_repo(tmp_path)
    _write(docs_next / "docs" / "en" / "guide.md", "# Old guide UCM\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "baseline")
    base_sha = _run(repo, "rev-parse", "HEAD")
    _write(docs_next / "docs" / "en" / "guide.md", "# Hello UCM\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "change English guide")
    head_sha = _run(repo, "rev-parse", "HEAD")
    task_dir = tmp_path / "translation-task"
    pipeline.prepare_translation(
        docs_next,
        mode="changed",
        output_dir=task_dir,
        provenance=_provenance(
            mode="changed",
            base_sha=base_sha,
            head_sha=head_sha,
        ),
        identity=_runtime_identity(docs_next),
        base_ref=base_sha,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    return repo, docs_next, base_sha, head_sha, task_dir


def _task_payload(task_dir: Path) -> dict[str, object]:
    return json.loads((task_dir / "translation-task.json").read_text(encoding="utf-8"))


def _write_agent_outputs(
    task_dir: Path,
    output_dir: Path,
    *,
    transform=lambda value: value.replace("Hello", "你好"),
) -> None:
    task = _task_payload(task_dir)
    output_dir.mkdir(parents=True)
    for chunk in task["chunks"]:
        source = (task_dir / chunk["input_path"]).read_text(encoding="utf-8")
        (output_dir / chunk["output_path"]).write_text(
            transform(source), encoding="utf-8"
        )


def test_state_v2_has_exact_non_secret_page_identity(tmp_path: Path) -> None:
    _, docs_next = _new_v2_repo(tmp_path)
    page = {
        "source_hash": core.content_hash("# Hello UCM\n"),
        "target_hash": core.content_hash("# 你好 UCM\n"),
        "provider": "openai",
        "model": "test-model",
        "endpoint_hash": core.content_hash("endpoint"),
        "agent_hash": core.content_hash("agent"),
        "glossary_hash": core.content_hash("glossary"),
    }
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps({"schema_version": 2, "pages": {"guide.md": page}}) + "\n",
    )

    state = core.TranslationState.load(docs_next / core.STATE_FILENAME)
    serialized = state.as_dict()

    assert serialized["schema_version"] == 2
    assert serialized["pages"]["guide.md"] == page
    assert set(serialized["pages"]["guide.md"]) == {
        "source_hash",
        "target_hash",
        "provider",
        "model",
        "endpoint_hash",
        "agent_hash",
        "glossary_hash",
    }
    assert "models.example.invalid" not in state.to_json()
    assert "API_KEY" not in state.to_json()


def test_state_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    _, docs_next = _new_v2_repo(tmp_path)
    (docs_next / core.STATE_FILENAME).write_text(
        '{"schema_version":2,"pages":{},"pages":{}}\n',
        encoding="utf-8",
    )

    with pytest.raises(core.TranslationError, match="duplicate"):
        core.TranslationState.load(docs_next / core.STATE_FILENAME)


def _managed_page_record(
    docs_next: Path,
    source: str,
    target: str,
) -> dict[str, str]:
    identity = _runtime_identity(docs_next)
    return {
        "source_hash": core.content_hash(source),
        "target_hash": core.content_hash(target),
        **identity.as_dict(),
    }


def test_matching_touched_state_remains_managed_and_is_a_noop(tmp_path: Path) -> None:
    _, docs_next = _new_v2_repo(tmp_path)
    source = "# Hello UCM\n"
    target = "# 你好 UCM\n"
    _write(docs_next / "docs" / "en" / "guide.md", source)
    _write(docs_next / "docs" / "zh" / "guide.md", target)
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps(
            {
                "schema_version": 2,
                "pages": {"guide.md": _managed_page_record(docs_next, source, target)},
            }
        )
        + "\n",
    )

    plan = core.build_translation_plan(
        docs_next,
        mode="changed",
        identity=_runtime_identity(docs_next),
        glossary=core.Glossary.load(docs_next / core.GLOSSARY_FILENAME),
        changes=core.ChangeSet(
            source_changed=frozenset({PurePosixPath("guide.md")}),
            target_changed=frozenset({PurePosixPath("guide.md")}),
        ),
    )

    assert plan.requests == ()
    assert plan.deletes == ()
    assert plan.skipped_manual == ()
    assert PurePosixPath("guide.md") in plan.state.pages
    assert plan.state.pages == plan.initial_state.pages


def test_hash_diverged_touched_target_becomes_manual_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    _, docs_next = _new_v2_repo(tmp_path)
    source = "# Hello UCM\n"
    managed_target = "# 你好 UCM\n"
    _write(docs_next / "docs" / "en" / "guide.md", source)
    _write(docs_next / "docs" / "zh" / "guide.md", "# 人工说明 UCM\n")
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps(
            {
                "schema_version": 2,
                "pages": {
                    "guide.md": _managed_page_record(docs_next, source, managed_target)
                },
            }
        )
        + "\n",
    )

    plan = core.build_translation_plan(
        docs_next,
        mode="changed",
        identity=_runtime_identity(docs_next),
        glossary=core.Glossary.load(docs_next / core.GLOSSARY_FILENAME),
        changes=core.ChangeSet(
            source_changed=frozenset({PurePosixPath("guide.md")}),
            target_changed=frozenset({PurePosixPath("guide.md")}),
        ),
    )

    assert plan.requests == ()
    assert plan.deletes == ()
    assert plan.skipped_manual == (PurePosixPath("guide.md"),)
    assert PurePosixPath("guide.md") not in plan.state.pages


def test_manual_takeover_must_remove_the_stale_managed_state(tmp_path: Path) -> None:
    _, docs_next = _new_v2_repo(tmp_path)
    source = "# Hello UCM\n"
    managed_target = "# 你好 UCM\n"
    _write(docs_next / "docs" / "en" / "guide.md", source)
    _write(docs_next / "docs" / "zh" / "guide.md", "# 人工说明 UCM\n")
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps(
            {
                "schema_version": 2,
                "pages": {
                    "guide.md": _managed_page_record(docs_next, source, managed_target)
                },
            }
        )
        + "\n",
    )
    changes = core.ChangeSet(
        source_changed=frozenset({PurePosixPath("guide.md")}),
        target_changed=frozenset({PurePosixPath("guide.md")}),
    )

    result = core.check_translations(docs_next, changes=changes)
    assert [issue.code for issue in result.issues] == ["manual-ownership-state"]

    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps({"schema_version": 2, "pages": {}}) + "\n",
    )
    assert core.check_translations(docs_next, changes=changes).ok


def test_prepare_emits_exact_task_schema_and_content_addressed_chunks(
    tmp_path: Path,
) -> None:
    _, _, base_sha, head_sha, task_dir = _prepare_changed_page(tmp_path)
    task = _task_payload(task_dir)

    assert set(task) == {
        "schema_version",
        "provenance",
        "identity",
        "pages",
        "chunks",
        "deletes",
        "summary",
    }
    assert task["schema_version"] == 1
    assert task["provenance"] == {
        "mode": "changed",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "base_repository_id": "100",
        "head_repository_id": "200",
        "pr_number": 7,
    }
    assert set(task["identity"]) == {
        "provider",
        "model",
        "endpoint_hash",
        "agent_hash",
        "glossary_hash",
    }
    assert task["identity"]["provider"] == "openai"
    assert task["identity"]["model"] == "test-model"
    assert len(task["pages"]) == 1
    page = task["pages"][0]
    assert set(page) == {
        "source_path",
        "target_path",
        "source_hash",
        "expected_target_hash",
        "chunk_ids",
    }
    assert page["source_path"] == "docs-next/docs/en/guide.md"
    assert page["target_path"] == "docs-next/docs/zh/guide.md"
    assert page["expected_target_hash"] is None
    assert page["chunk_ids"]
    assert len(task["chunks"]) == len(page["chunk_ids"])
    for index, chunk in enumerate(task["chunks"]):
        assert set(chunk) == {
            "id",
            "page_index",
            "source_path",
            "input_path",
            "output_path",
        }
        assert chunk["id"] in page["chunk_ids"]
        assert chunk["page_index"] == index
        assert chunk["source_path"] == page["source_path"]
        assert chunk["input_path"] == f"chunks/{chunk['id']}.md"
        assert chunk["output_path"] == f"{chunk['id']}.md"
        assert (task_dir / chunk["input_path"]).is_file()


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_finalize_requires_the_exact_agent_chunk_set(
    tmp_path: Path,
    mutation: str,
) -> None:
    _, docs_next, _, _, task_dir = _prepare_changed_page(tmp_path)
    agent_output = tmp_path / "agent-output"
    _write_agent_outputs(task_dir, agent_output)
    task = _task_payload(task_dir)
    if mutation == "missing":
        (agent_output / task["chunks"][0]["output_path"]).unlink()
    else:
        _write(agent_output / "unexpected.md", "额外输出\n")
    artifact_dir = tmp_path / "artifact"

    with pytest.raises(core.TranslationError, match=mutation):
        pipeline.finalize_translation(
            docs_next,
            task_path=task_dir / "translation-task.json",
            agent_output_dir=agent_output,
            output_dir=artifact_dir,
            backend=FakeMarkdownBackend(),
            agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
        )

    assert not artifact_dir.exists()


def test_finalize_rejects_binary_agent_output_without_an_artifact(
    tmp_path: Path,
) -> None:
    _, docs_next, _, _, task_dir = _prepare_changed_page(tmp_path)
    agent_output = tmp_path / "agent-output"
    _write_agent_outputs(
        task_dir,
        agent_output,
        transform=lambda value: value.replace("Hello", "你好") + "\0",
    )
    artifact_dir = tmp_path / "artifact"

    with pytest.raises(core.TranslationError, match="binary"):
        pipeline.finalize_translation(
            docs_next,
            task_path=task_dir / "translation-task.json",
            agent_output_dir=agent_output,
            output_dir=artifact_dir,
            backend=FakeMarkdownBackend(),
            agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
        )

    assert not artifact_dir.exists()


def test_finalize_emits_v2_cas_manifest_and_text_patch(tmp_path: Path) -> None:
    _, docs_next, base_sha, head_sha, task_dir = _prepare_changed_page(tmp_path)
    agent_output = tmp_path / "agent-output"
    _write_agent_outputs(task_dir, agent_output)
    artifact_dir = tmp_path / "artifact"

    pipeline.finalize_translation(
        docs_next,
        task_path=task_dir / "translation-task.json",
        agent_output_dir=agent_output,
        output_dir=artifact_dir,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    manifest = json.loads(
        (artifact_dir / "translation-artifact.json").read_text(encoding="utf-8")
    )

    assert set(manifest) == {
        "schema_version",
        "provenance",
        "identity",
        "write",
        "delete",
        "files",
        "summary",
    }
    assert manifest["schema_version"] == 2
    assert manifest["provenance"]["base_sha"] == base_sha
    assert manifest["provenance"]["head_sha"] == head_sha
    assert set(manifest["write"]) == {
        "docs-next/docs/zh/guide.md",
        "docs-next/translation-state.json",
    }
    assert manifest["delete"] == []
    assert set(manifest["files"]) == set(manifest["write"])
    for path, hashes in manifest["files"].items():
        assert set(hashes) == {"before_hash", "after_hash"}
        if path.endswith("guide.md"):
            assert hashes["before_hash"] is None
        assert hashes["after_hash"].startswith("sha256:")
    patch = (artifact_dir / "translation.patch").read_bytes()
    assert b"\0" not in patch
    assert b"diff --git" in patch
    assert b"docs-next/docs/zh/guide.md" in patch


def test_artifact_files_cover_writes_and_deletes_with_null_delete_hash(
    tmp_path: Path,
) -> None:
    repo, docs_next = _new_v2_repo(tmp_path)
    source = "# Hello UCM\n"
    target = "# 你好 UCM\n"
    _write(docs_next / "docs" / "en" / "obsolete.md", source)
    _write(docs_next / "docs" / "zh" / "obsolete.md", target)
    _write(
        docs_next / core.STATE_FILENAME,
        json.dumps(
            {
                "schema_version": 2,
                "pages": {
                    "obsolete.md": _managed_page_record(docs_next, source, target)
                },
            }
        )
        + "\n",
    )
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "baseline managed page")
    base_sha = _run(repo, "rev-parse", "HEAD")
    (docs_next / "docs" / "en" / "obsolete.md").unlink()
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "delete English page")
    head_sha = _run(repo, "rev-parse", "HEAD")
    task_dir = tmp_path / "task"
    pipeline.prepare_translation(
        docs_next,
        mode="changed",
        output_dir=task_dir,
        provenance=_provenance(
            mode="changed",
            base_sha=base_sha,
            head_sha=head_sha,
        ),
        identity=_runtime_identity(docs_next),
        base_ref=base_sha,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    agent_output = tmp_path / "agent-output"
    # Upload/download does not preserve an empty output directory.
    artifact_dir = tmp_path / "artifact"

    pipeline.finalize_translation(
        docs_next,
        task_path=task_dir / "translation-task.json",
        agent_output_dir=agent_output,
        output_dir=artifact_dir,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    manifest = json.loads(
        (artifact_dir / "translation-artifact.json").read_text(encoding="utf-8")
    )

    assert manifest["delete"] == ["docs-next/docs/zh/obsolete.md"]
    assert set(manifest["files"]) == set(manifest["write"]) | set(manifest["delete"])
    deleted = manifest["files"]["docs-next/docs/zh/obsolete.md"]
    assert deleted["before_hash"] == core.content_hash(target)
    assert deleted["after_hash"] is None


def test_apply_enforces_head_and_file_compare_and_swap_atomically(
    tmp_path: Path,
) -> None:
    repo, docs_next, _, _, task_dir = _prepare_changed_page(tmp_path)
    agent_output = tmp_path / "agent-output"
    _write_agent_outputs(task_dir, agent_output)
    artifact_dir = tmp_path / "artifact"
    pipeline.finalize_translation(
        docs_next,
        task_path=task_dir / "translation-task.json",
        agent_output_dir=agent_output,
        output_dir=artifact_dir,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    state_before = (docs_next / core.STATE_FILENAME).read_bytes()
    manual_target = docs_next / "docs" / "zh" / "guide.md"
    _write(manual_target, "# 人工中文 UCM\n")

    with pytest.raises(core.TranslationError, match="before_hash|compare|changed"):
        pipeline.apply_translation_artifact(docs_next, artifact_dir=artifact_dir)

    assert manual_target.read_text(encoding="utf-8") == "# 人工中文 UCM\n"
    assert (docs_next / core.STATE_FILENAME).read_bytes() == state_before

    manual_target.unlink()
    _run(repo, "commit", "--allow-empty", "-qm", "advance head")
    with pytest.raises(core.TranslationError, match="HEAD|head"):
        pipeline.apply_translation_artifact(docs_next, artifact_dir=artifact_dir)
    assert not manual_target.exists()
    assert (docs_next / core.STATE_FILENAME).read_bytes() == state_before


def test_apply_rejects_artifact_path_escape(tmp_path: Path) -> None:
    _, docs_next, _, _, task_dir = _prepare_changed_page(tmp_path)
    agent_output = tmp_path / "agent-output"
    _write_agent_outputs(task_dir, agent_output)
    artifact_dir = tmp_path / "artifact"
    pipeline.finalize_translation(
        docs_next,
        task_path=task_dir / "translation-task.json",
        agent_output_dir=agent_output,
        output_dir=artifact_dir,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    manifest_path = artifact_dir / "translation-artifact.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["write"].append("../escape.md")
    manifest["files"]["../escape.md"] = {
        "before_hash": None,
        "after_hash": core.content_hash("escaped\n"),
    }
    _write(artifact_dir.parent / "escape.md", "escaped\n")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(core.TranslationError, match="path|outside|unsafe|below"):
        pipeline.apply_translation_artifact(docs_next, artifact_dir=artifact_dir)

    assert not (docs_next.parent / "escape.md").exists()


def test_applied_managed_translation_makes_second_prepare_noop(tmp_path: Path) -> None:
    _, docs_next, base_sha, head_sha, task_dir = _prepare_changed_page(tmp_path)
    agent_output = tmp_path / "agent-output"
    _write_agent_outputs(task_dir, agent_output)
    artifact_dir = tmp_path / "artifact"
    pipeline.finalize_translation(
        docs_next,
        task_path=task_dir / "translation-task.json",
        agent_output_dir=agent_output,
        output_dir=artifact_dir,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    pipeline.apply_translation_artifact(docs_next, artifact_dir=artifact_dir)

    second_task = tmp_path / "second-task"
    pipeline.prepare_translation(
        docs_next,
        mode="changed",
        output_dir=second_task,
        provenance=_provenance(
            mode="changed",
            base_sha=base_sha,
            head_sha=head_sha,
        ),
        identity=_runtime_identity(docs_next),
        base_ref=base_sha,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
    )
    task = _task_payload(second_task)

    assert task["pages"] == []
    assert task["chunks"] == []
    assert task["deletes"] == []


@pytest.mark.parametrize(
    ("page_count", "page_size", "message"),
    [
        (11, 8, "10"),
        (1, 250 * 1024 + 1, "250"),
    ],
)
def test_prepare_enforces_bounded_pr_translation_input(
    tmp_path: Path,
    page_count: int,
    page_size: int,
    message: str,
) -> None:
    repo, docs_next = _new_v2_repo(tmp_path)
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "baseline")
    base_sha = _run(repo, "rev-parse", "HEAD")
    for index in range(page_count):
        _write(
            docs_next / "docs" / "en" / f"page-{index}.md",
            "x" * page_size,
        )
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "add English pages")
    head_sha = _run(repo, "rev-parse", "HEAD")

    with pytest.raises(core.TranslationError, match=message):
        pipeline.prepare_translation(
            docs_next,
            mode="changed",
            output_dir=tmp_path / "task",
            provenance=_provenance(
                mode="changed",
                base_sha=base_sha,
                head_sha=head_sha,
            ),
            identity=_runtime_identity(docs_next),
            base_ref=base_sha,
            backend=FakeMarkdownBackend(),
            agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
        )


def test_missing_prepare_is_bounded_and_reports_remaining_pages(tmp_path: Path) -> None:
    repo, docs_next = _new_v2_repo(tmp_path)
    for index in range(11):
        _write(
            docs_next / "docs" / "en" / f"page-{index:02d}.md",
            f"# Hello {index} UCM\n",
        )
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "add missing English pages")
    head_sha = _run(repo, "rev-parse", "HEAD")
    task_dir = tmp_path / "task"

    pipeline.prepare_translation(
        docs_next,
        mode="missing",
        output_dir=task_dir,
        provenance=_provenance(
            mode="missing",
            base_sha=head_sha,
            head_sha=head_sha,
        ),
        identity=_runtime_identity(docs_next),
        base_ref=head_sha,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=AGENT_INSTRUCTIONS_PATH,
        max_pages=5,
    )
    task = _task_payload(task_dir)

    assert len(task["pages"]) == 5
    assert task["summary"]["total_pages"] == 11
    assert task["summary"]["selected_pages"] == 5
    assert task["summary"]["remaining_pages"] == 6
    assert task["summary"]["page_limit"] == 5


def test_managed_source_staleness_survives_empty_diff_window(tmp_path: Path) -> None:
    repo, docs_next = _new_repo(tmp_path)
    source = "# Hello UCM\n"
    target = "# 你好 UCM\n"
    _write(docs_next / "docs/en/guide.md", source)
    _write(docs_next / "docs/zh/guide.md", target)
    glossary = core.Glossary.load(docs_next / core.GLOSSARY_FILENAME)
    identity = core.TranslationIdentity.from_runtime(
        provider="openai-responses",
        model="configured-model",
        base_url="https://api.example/v1",
        agent_instructions=pipeline.load_agent_instructions(),
        glossary=glossary,
    )
    state = core.TranslationState(
        pages={PurePosixPath("guide.md"): core._page_state(source, target, identity)}
    )
    _write(docs_next / core.STATE_FILENAME, state.to_json())
    _write(docs_next / "docs/en/guide.md", "# Hello UCM updated\n")
    result = core.check_translations(
        docs_next, changes=core.ChangeSet(), backend=FakeMarkdownBackend()
    )
    assert not result.ok
    assert [issue.code for issue in result.issues] == ["translation-stale"]
    plan = core.build_translation_plan(
        docs_next,
        mode="changed",
        identity=identity,
        glossary=glossary,
        changes=core.ChangeSet(target_changed=frozenset({PurePosixPath("guide.md")})),
    )
    assert [request.relative_path for request in plan.requests] == [
        PurePosixPath("guide.md")
    ]
    assert not plan.skipped_manual


def test_required_pages_block_missing_while_optional_pages_warn(tmp_path: Path) -> None:
    _, docs_next = _new_repo(tmp_path)
    _write(docs_next / "docs/en/guide.md", "# Hello UCM\n")
    _write(docs_next / "docs/en/advanced.md", "# Advanced\n")
    _write(docs_next / "translation-required.txt", "guide.md\n")
    result = core.check_translations(
        docs_next,
        changes=core.ChangeSet(
            source_changed=frozenset({PurePosixPath("advanced.md")})
        ),
    )
    assert [issue.path for issue in result.blocking_issues] == [
        PurePosixPath("guide.md")
    ]
    _write(docs_next / "docs/zh/guide.md", "# 你好 UCM\n")
    result = core.check_translations(docs_next)
    assert result.ok
    assert [issue.path for issue in result.issues] == [PurePosixPath("advanced.md")]
    assert result.issues[0].blocking is False
