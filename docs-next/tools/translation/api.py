"""Four explicit HTTP protocols behind one synchronous Markdown chunk call."""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .agent_pipeline import (
    MAX_AGENT_OUTPUT_BYTES,
    TranslationTask,
    _new_output,
    _read_text,
    _rebuild_task,
    _validate_task_tree,
    load_agent_instructions,
)
from .core import (
    TranslationError,
    TranslationIdentity,
    TranslationLayout,
    _validate_base_url,
)
from .markdown import AgentMarkdownBackend, prepare_markdown_translation

API_FORMATS = (
    "openai-chat-completions",
    "openai-responses",
    "anthropic-messages",
    "gemini-generate-content",
)
MAX_OUTPUT_TOKENS = 16384


@dataclass(frozen=True, repr=False)
class TranslationAPI:
    """Configuration stays in memory; only its non-secret identity is persisted."""

    api_format: str
    base_url: str
    model: str
    api_key: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "TranslationAPI":
        env = os.environ if environment is None else environment
        values = {}
        for name in ("API_FORMAT", "BASE_URL", "MODEL", "API_KEY"):
            key = f"DOCS_TRANSLATION_{name}"
            value = env.get(key, "").strip()
            if not value:
                raise TranslationError(
                    f"{key} is required when translation chunks exist"
                )
            values[name] = value
        if values["API_FORMAT"] not in API_FORMATS:
            raise TranslationError(
                f"DOCS_TRANSLATION_API_FORMAT must be one of {', '.join(API_FORMATS)}"
            )
        return cls(
            values["API_FORMAT"],
            _validate_base_url(values["BASE_URL"]),
            values["MODEL"],
            values["API_KEY"],
        )

    def translate(self, instructions: str, source: str) -> str:
        """Return one complete text response, or fail without retry or fallback."""

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_format == "openai-chat-completions":
            suffix = "/chat/completions"
            headers["Authorization"] = f"Bearer {self.api_key}"
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": source},
                ],
                "max_completion_tokens": MAX_OUTPUT_TOKENS,
                "stream": False,
            }
        elif self.api_format == "openai-responses":
            suffix = "/responses"
            headers["Authorization"] = f"Bearer {self.api_key}"
            payload = {
                "model": self.model,
                "instructions": instructions,
                "input": source,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "stream": False,
                "store": False,
            }
        elif self.api_format == "anthropic-messages":
            suffix = "/messages"
            headers.update(
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
            )
            payload = {
                "model": self.model,
                "system": instructions,
                "messages": [{"role": "user", "content": source}],
                "max_tokens": MAX_OUTPUT_TOKENS,
                "stream": False,
            }
        elif self.api_format == "gemini-generate-content":
            model = urllib.parse.quote(self.model.removeprefix("models/"), safe="")
            suffix = f"/models/{model}:generateContent"
            headers["x-goog-api-key"] = self.api_key
            payload = {
                "systemInstruction": {"parts": [{"text": instructions}]},
                "contents": [{"role": "user", "parts": [{"text": source}]}],
                "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
            }
        else:
            raise TranslationError(
                f"unsupported translation API format: {self.api_format}"
            )
        request = urllib.request.Request(
            self.base_url + suffix,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        # A configured endpoint is the credential boundary. Redirects must not
        # forward its Authorization or API-key header to another destination.
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=120) as response:
                raw = response.read(MAX_AGENT_OUTPUT_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise TranslationError(
                f"translation API returned HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TranslationError("translation API request failed") from None
        if len(raw) > MAX_AGENT_OUTPUT_BYTES:
            raise TranslationError("translation API response exceeds the output limit")
        try:
            result = json.loads(raw.decode("utf-8"))
            return _response_text(self.api_format, result)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            ValueError,
        ):
            raise TranslationError(
                "translation API returned an invalid response"
            ) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _response_text(api_format: str, result: object) -> str:
    if not isinstance(result, dict) or result.get("error"):
        raise TranslationError("translation API returned an error or invalid response")
    if api_format == "openai-chat-completions":
        choices = result["choices"]
        if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
            raise TranslationError("translation API did not complete the text response")
        message = choices[0]["message"]
        if message.get("refusal") or message.get("tool_calls"):
            raise TranslationError("translation API refused or requested a tool")
        text = message["content"]
    elif api_format == "openai-responses":
        if result.get("status") != "completed":
            raise TranslationError("translation API did not complete the text response")
        parts = []
        for item in result["output"]:
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
            ):
                raise TranslationError("translation API returned unexpected output")
            for part in item["content"]:
                if part.get("type") != "output_text":
                    raise TranslationError("translation API returned non-text output")
                parts.append(part["text"])
        text = "".join(parts)
    elif api_format == "anthropic-messages":
        if result.get("stop_reason") != "end_turn":
            raise TranslationError("translation API did not complete the text response")
        parts = result["content"]
        if any(part.get("type") != "text" for part in parts):
            raise TranslationError("translation API returned non-text output")
        text = "".join(part["text"] for part in parts)
    else:
        candidates = result["candidates"]
        if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
            raise TranslationError("translation API did not complete the text response")
        parts = candidates[0]["content"]["parts"]
        if any("text" not in part for part in parts):
            raise TranslationError("translation API returned non-text output")
        text = "".join(part["text"] for part in parts if not part.get("thought"))
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise TranslationError("translation API returned empty or invalid text")
    return text


def generate_translation(
    docs_next_root: Path,
    *,
    task_path: Path,
    output_dir: Path,
    environment: Mapping[str, str] | None = None,
    backend: AgentMarkdownBackend | None = None,
    instructions_path: Path | None = None,
) -> int:
    """Translate a verified task atomically; empty tasks need no API configuration."""

    task = TranslationTask.load(task_path)
    _validate_task_tree(task)
    built = _rebuild_task(
        TranslationLayout(docs_next_root), task, backend, instructions_path
    )
    if built.task.as_dict() != task.as_dict():
        raise TranslationError("translation task no longer matches checkout")
    assert task.task_dir is not None
    for chunk in task.chunks:
        if (
            _read_text(task.task_dir.joinpath(*chunk.input_path.parts), "task chunk")
            != built.chunks[chunk.id]
        ):
            raise TranslationError(f"task chunk changed: {chunk.id}")
    instructions = load_agent_instructions(instructions_path)
    api = None
    if task.chunks:
        api = TranslationAPI.from_environment(environment)
        identity = TranslationIdentity.from_runtime(
            provider=api.api_format,
            model=api.model,
            base_url=api.base_url,
            agent_instructions=instructions,
            glossary=built.plan.glossary,
        )
        if identity != task.identity:
            raise TranslationError(
                "translation API configuration differs from prepared task"
            )
    staging, final = _new_output(output_dir)
    try:
        total_bytes = 0
        for page, request in zip(task.pages, built.selected, strict=True):
            prepared = prepare_markdown_translation(
                request.source,
                source_path=page.source_path.as_posix(),
                language_code="zh-CN",
                backend=backend,
            )
            for chunk_id, chunk in zip(page.chunk_ids, prepared.chunks, strict=True):
                assert api is not None
                prompt = "\n\n".join(
                    (instructions, built.plan.glossary.instruction, chunk.prompt)
                )
                translated = api.translate(prompt, chunk.text)
                total_bytes += len(translated.encode("utf-8"))
                if total_bytes > MAX_AGENT_OUTPUT_BYTES:
                    raise TranslationError(
                        "translation output exceeds the task output limit"
                    )
                (staging / f"{chunk_id}.md").write_text(translated, encoding="utf-8")
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return len(task.chunks)
