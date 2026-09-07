"""HTTP wire contracts and deterministic hand-off behavior without real API spend."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from test_translation_core import FakeMarkdownBackend, _new_repo, _run, _write
from translation import agent_pipeline as pipeline
from translation import api, core

REPLIES = {
    "openai-chat-completions": {
        "choices": [{"finish_reason": "stop", "message": {"content": "你好 UCM"}}]
    },
    "openai-responses": {
        "status": "completed",
        "output": [
            {"type": "reasoning"},
            {
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "你好 UCM"}],
            },
        ],
    },
    "anthropic-messages": {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "你好 UCM"}],
    },
    "gemini-generate-content": {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"text": "private thought", "thought": True},
                        {"text": "你好 UCM"},
                    ]
                },
            }
        ]
    },
}


def stub_http(monkeypatch, reply):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 120
            if isinstance(reply, BaseException):
                raise reply
            return io.BytesIO(
                reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            )

    monkeypatch.setattr(api.urllib.request, "build_opener", lambda *_: Opener())
    return requests


@pytest.mark.parametrize("api_format", api.API_FORMATS)
def test_four_http_protocol_contracts(monkeypatch, api_format):
    requests = stub_http(monkeypatch, REPLIES[api_format])
    client = api.TranslationAPI(
        api_format, "https://configured.example/v1", "chosen-model", "private-key"
    )
    assert client.translate("trusted rules", "untrusted Markdown") == "你好 UCM"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    body = json.loads(request.data)
    headers = {key.lower(): value for key, value in request.header_items()}
    if api_format.startswith("openai-"):
        assert headers["authorization"] == "Bearer private-key"
        assert body["model"] == "chosen-model"
        assert body["stream"] is False
    if api_format == "openai-chat-completions":
        assert request.full_url.endswith("/v1/chat/completions")
        assert body["messages"] == [
            {"role": "system", "content": "trusted rules"},
            {"role": "user", "content": "untrusted Markdown"},
        ]
    elif api_format == "openai-responses":
        assert request.full_url.endswith("/v1/responses")
        assert (
            body["instructions"] == "trusted rules"
            and body["input"] == "untrusted Markdown"
        )
    elif api_format == "anthropic-messages":
        assert request.full_url.endswith("/v1/messages")
        assert (
            headers["x-api-key"] == "private-key"
            and headers["anthropic-version"] == "2023-06-01"
        )
        assert body["system"] == "trusted rules"
        assert body["messages"] == [{"role": "user", "content": "untrusted Markdown"}]
    else:
        assert request.full_url.endswith("/v1/models/chosen-model:generateContent")
        assert headers["x-goog-api-key"] == "private-key"
        assert body["systemInstruction"] == {"parts": [{"text": "trusted rules"}]}
        assert body["contents"] == [
            {"role": "user", "parts": [{"text": "untrusted Markdown"}]}
        ]
    assert "private-key" not in repr(client)


@pytest.mark.parametrize(
    "api_format,reply",
    [
        (
            "openai-chat-completions",
            {
                "choices": [
                    {"finish_reason": "length", "message": {"content": "partial"}}
                ]
            },
        ),
        ("openai-responses", {"status": "incomplete", "output": []}),
        (
            "anthropic-messages",
            {
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": "partial"}],
            },
        ),
        ("gemini-generate-content", {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
        ("openai-responses", b"invalid JSON"),
        ("openai-responses", {"status": "completed", "output": []}),
        ("openai-responses", {"status": "completed", "output": ["invalid"]}),
        (
            "anthropic-messages",
            {"stop_reason": "end_turn", "content": [{"type": "tool_use"}]},
        ),
        (
            "openai-responses",
            HTTPError("https://configured.example", 401, "private-key", {}, None),
        ),
        ("openai-responses", URLError("private-key")),
    ],
)
def test_incomplete_invalid_and_http_failures_never_retry_or_leak_key(
    monkeypatch, api_format, reply
):
    requests = stub_http(monkeypatch, reply)
    with pytest.raises(core.TranslationError) as caught:
        api.TranslationAPI(
            api_format, "https://configured.example/v1", "chosen-model", "private-key"
        ).translate("rules", "source")
    assert len(requests) == 1
    assert "private-key" not in str(caught.value)


def prepared_task(
    tmp_path, *, source="# Hello UCM\n", translated=False, instructions_path=None
):
    repo, docs = _new_repo(tmp_path)
    _write(docs / "docs/en/guide.md", source)
    if translated:
        _write(docs / "docs/zh/guide.md", "# 你好 UCM\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "seed")
    sha = _run(repo, "rev-parse", "HEAD")
    glossary = core.Glossary.load(docs / core.GLOSSARY_FILENAME)
    identity = core.TranslationIdentity.from_runtime(
        provider="openai-responses",
        model="chosen-model",
        base_url="https://configured.example/v1",
        agent_instructions=pipeline.load_agent_instructions(instructions_path),
        glossary=glossary,
    )
    task = pipeline.prepare_translation(
        docs,
        mode="missing",
        output_dir=tmp_path / "task",
        provenance=pipeline.TranslationProvenance("missing", sha, sha, "1", "1", None),
        identity=identity,
        backend=FakeMarkdownBackend(),
        agent_instructions_path=instructions_path,
    )
    env = {
        "DOCS_TRANSLATION_API_FORMAT": "openai-responses",
        "DOCS_TRANSLATION_BASE_URL": "https://configured.example/v1",
        "DOCS_TRANSLATION_MODEL": "chosen-model",
        "DOCS_TRANSLATION_API_KEY": "private-key",
    }
    return docs, task, env


def test_noop_needs_no_config_and_makes_no_request(tmp_path, monkeypatch):
    docs, task, _ = prepared_task(tmp_path, translated=True)
    monkeypatch.setattr(
        api.TranslationAPI,
        "from_environment",
        lambda *_: pytest.fail("no-op read API config"),
    )
    output = tmp_path / "output"
    assert (
        api.generate_translation(
            docs,
            task_path=task.task_path,
            output_dir=output,
            environment={},
            backend=FakeMarkdownBackend(),
        )
        == 0
    )
    assert list(output.iterdir()) == []


def test_prompt_receives_terminology_and_coop_rules_separate_from_source(
    tmp_path, monkeypatch
):
    docs, task, env = prepared_task(tmp_path, source="# Hello UCM Prefix Cache\n")
    calls = []

    def translate(self, prompt, source):
        calls.append((prompt, source))
        return "# 你好 UCM 前缀缓存\n"

    monkeypatch.setattr(api.TranslationAPI, "translate", translate)
    output = tmp_path / "output"
    assert (
        api.generate_translation(
            docs,
            task_path=task.task_path,
            output_dir=output,
            environment=env,
            backend=FakeMarkdownBackend(),
        )
        == 1
    )
    prompt, source = calls[0]
    assert "Preserve exactly: UCM" in prompt
    assert "Prefix Cache => 前缀缓存" in prompt
    assert "Translate docs-next/docs/en/guide.md part 1" in prompt
    assert "# Hello UCM Prefix Cache" not in prompt
    assert source == "# Hello UCM Prefix Cache\n"
    artifact = pipeline.finalize_translation(
        docs,
        task_path=task.task_path,
        agent_output_dir=output,
        output_dir=tmp_path / "artifact",
        backend=FakeMarkdownBackend(),
    )
    assert artifact.summary["translated_pages"] == 1


def test_failed_later_chunk_discards_all_model_output(tmp_path, monkeypatch):
    docs, task, env = prepared_task(
        tmp_path, source="# Hello UCM\n<!-- split -->\nDescription\n"
    )
    calls = []

    def translate(self, prompt, source):
        calls.append(source)
        if len(calls) == 2:
            raise core.TranslationError("translation API failed")
        return "# 你好 UCM\n"

    monkeypatch.setattr(api.TranslationAPI, "translate", translate)
    output = tmp_path / "output"
    with pytest.raises(core.TranslationError, match="failed"):
        api.generate_translation(
            docs,
            task_path=task.task_path,
            output_dir=output,
            environment=env,
            backend=FakeMarkdownBackend(),
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".output.tmp-*"))
    assert not (docs / "docs/zh/guide.md").exists()


def test_explicit_config_has_no_provider_model_or_endpoint_defaults():
    with pytest.raises(core.TranslationError, match="DOCS_TRANSLATION_API_FORMAT"):
        api.TranslationAPI.from_environment({})


def test_custom_instructions_preserve_identity_across_prepare_generate_finalize(
    tmp_path, monkeypatch
):
    instructions_path = tmp_path / "custom-instructions.md"
    instructions_path.write_text(
        pipeline.load_agent_instructions() + "\nPrefer concise technical wording.\n"
    )
    docs, task, env = prepared_task(tmp_path, instructions_path=instructions_path)
    prompts = []

    def translate(self, prompt, source):
        prompts.append(prompt)
        return "# 你好 UCM\n"

    monkeypatch.setattr(api.TranslationAPI, "translate", translate)
    output = tmp_path / "output"
    assert (
        api.generate_translation(
            docs,
            task_path=task.task_path,
            output_dir=output,
            environment=env,
            backend=FakeMarkdownBackend(),
            instructions_path=instructions_path,
        )
        == 1
    )
    assert "Prefer concise technical wording." in prompts[0]
    artifact = pipeline.finalize_translation(
        docs,
        task_path=task.task_path,
        agent_output_dir=output,
        output_dir=tmp_path / "artifact",
        backend=FakeMarkdownBackend(),
        agent_instructions_path=instructions_path,
    )
    assert artifact.identity == task.identity
