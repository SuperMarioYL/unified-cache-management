from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = DOCS_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
MODULE_PATH = TOOLS_ROOT / "site.py"
SPEC = importlib.util.spec_from_file_location("ucm_docs_site", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
site = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = site
SPEC.loader.exec_module(site)


class Delegated(RuntimeError):
    """Stop a CLI call immediately after it delegates to the translation core."""


def test_translate_requires_one_nested_operation() -> None:
    with pytest.raises(SystemExit) as error:
        site.main(["translate"])

    assert error.value.code == 2


@pytest.mark.parametrize("legacy_mode", ["--changed", "--missing", "--check"])
def test_translate_rejects_legacy_flat_modes(legacy_mode: str) -> None:
    with pytest.raises(SystemExit) as error:
        site.main(["translate", legacy_mode])

    assert error.value.code == 2


def test_prepare_requires_one_explicit_selection_mode(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        site.main(
            [
                "translate",
                "prepare",
                "--output-dir",
                str(tmp_path / "task"),
            ]
        )

    assert error.value.code == 2


def test_prepare_builds_non_secret_identity_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from translation import agent_pipeline as pipeline

    monkeypatch.delenv("DOCS_TRANSLATION_API_KEY", raising=False)
    agent_instructions = tmp_path / "agent.md"
    agent_instructions.write_text("Translate UCM documentation.\n", encoding="utf-8")
    received: dict[str, object] = {}

    def prepare_translation(root: Path, **kwargs: object) -> object:
        received["root"] = root
        received.update(kwargs)
        raise Delegated

    monkeypatch.setattr(pipeline, "prepare_translation", prepare_translation)
    monkeypatch.setattr(pipeline, "resolve_git_sha", lambda root, ref: "a" * 40)
    output = tmp_path / "task"

    with pytest.raises(Delegated):
        site.main(
            [
                "translate",
                "prepare",
                "--changed",
                "--base-ref",
                "base-sha",
                "--head-sha",
                "b" * 40,
                "--base-repository-id",
                "100",
                "--head-repository-id",
                "200",
                "--pr-number",
                "7",
                "--api-format",
                "openai-chat-completions",
                "--model",
                "configured-model",
                "--base-url",
                "https://models.example.invalid/v1",
                "--instructions",
                str(agent_instructions),
                "--output-dir",
                str(output),
            ]
        )

    assert received["root"] == DOCS_ROOT
    assert received["mode"] == "changed"
    assert received["base_ref"] == "base-sha"
    assert received["output_dir"] == output
    assert received["agent_instructions_path"] == agent_instructions
    provenance = received["provenance"]
    assert provenance.mode == "changed"
    assert provenance.base_sha == "a" * 40
    assert provenance.head_sha == "b" * 40
    assert provenance.base_repository_id == "100"
    assert provenance.head_repository_id == "200"
    assert provenance.pr_number == 7
    identity = received["identity"]
    assert identity.provider == "openai-chat-completions"
    assert identity.model == "configured-model"
    assert identity.endpoint_hash.startswith("sha256:")
    assert identity.agent_hash.startswith("sha256:")
    assert not hasattr(identity, "base_url")


def test_finalize_delegates_task_and_agent_output_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from translation import agent_pipeline as pipeline

    received: dict[str, object] = {}

    def finalize_translation(root: Path, **kwargs: object) -> object:
        received["root"] = root
        received.update(kwargs)
        raise Delegated

    monkeypatch.setattr(pipeline, "finalize_translation", finalize_translation)
    task_dir = tmp_path / "task"
    agent_output = tmp_path / "agent"
    output = tmp_path / "artifact"

    with pytest.raises(Delegated):
        site.main(
            [
                "translate",
                "finalize",
                "--task-dir",
                str(task_dir),
                "--agent-output-dir",
                str(agent_output),
                "--output-dir",
                str(output),
            ]
        )

    assert received == {
        "root": DOCS_ROOT,
        "task_path": task_dir / "translation-task.json",
        "agent_output_dir": agent_output,
        "output_dir": output,
        "agent_instructions_path": (
            DOCS_ROOT / "tools" / "translation" / "instructions.md"
        ),
    }


def test_check_is_read_only_and_does_not_require_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from translation import core

    for name in (
        "DOCS_TRANSLATION_API_FORMAT",
        "DOCS_TRANSLATION_BASE_URL",
        "DOCS_TRANSLATION_MODEL",
        "DOCS_TRANSLATION_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    received: dict[str, object] = {}

    def check_translations(root: Path, **kwargs: object) -> object:
        received["root"] = root
        received.update(kwargs)
        raise Delegated

    monkeypatch.setattr(core, "check_translations", check_translations)

    with pytest.raises(Delegated):
        site.main(["translate", "check", "--base-ref", "base-sha"])

    assert received == {"root": DOCS_ROOT, "base_ref": "base-sha"}


def test_apply_delegates_to_cas_artifact_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from translation import agent_pipeline as pipeline

    received: dict[str, object] = {}

    def apply_translation_artifact(root: Path, **kwargs: object) -> object:
        received["root"] = root
        received.update(kwargs)
        raise Delegated

    monkeypatch.setattr(
        pipeline,
        "apply_translation_artifact",
        apply_translation_artifact,
    )
    artifact_dir = tmp_path / "artifact"

    with pytest.raises(Delegated):
        site.main(
            [
                "translate",
                "apply",
                "--artifact-dir",
                str(artifact_dir),
            ]
        )

    assert received == {"root": DOCS_ROOT, "artifact_dir": artifact_dir}
