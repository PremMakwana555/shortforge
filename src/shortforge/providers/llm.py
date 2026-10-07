"""LLM providers. Free / open-weight first:

* ``groq``       - Llama 3.3 70B (open weights) on Groq's free tier, OpenAI-compatible API
* ``openrouter`` - ``:free`` open models (Llama / Qwen / DeepSeek), OpenAI-compatible API
* ``gemini``     - Gemini free tier (AI Studio key) - matches the GCP production setup
* ``ollama``     - any open model running locally (llama3.1, qwen2.5, ...), no key, no network
* ``offline``    - deterministic rule-based writer so the pipeline never hard-stops

``generate_json`` wraps a chain with JSON extraction, schema validation and one repair round-trip
per provider - LLM output is untrusted input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from ..core.errors import ProviderError, ValidationFailed
from ..core.resilience import ChainResult, ProviderChain, http_client, request_with_retry
from . import offline_writer


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider:
    name = "base"

    def available(self) -> bool:
        return True

    async def complete(self, system: str, user: str, *, task: str, context: dict[str, Any],
                       temperature: float = 0.8) -> LLMResponse:
        raise NotImplementedError


class OpenAICompatible(LLMProvider):
    def __init__(self, name: str, base_url: str, model: str, api_key: str | None, settings: Settings,
                 extra_headers: dict[str, str] | None = None, needs_key: bool = True):
        self.name, self.base_url, self.model, self.api_key = name, base_url.rstrip("/"), model, api_key
        self.settings, self.extra_headers, self.needs_key = settings, extra_headers or {}, needs_key

    def available(self) -> bool:
        return bool(self.api_key) or not self.needs_key

    async def complete(self, system: str, user: str, *, task: str, context: dict[str, Any],
                       temperature: float = 0.8) -> LLMResponse:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        async with http_client(self.settings.http_timeout_seconds,
                               self.settings.http_connect_timeout_seconds) as c:
            r = await request_with_retry(c, "POST", f"{self.base_url}/chat/completions", json=body,
                                         headers=headers)
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected response shape: {str(data)[:200]}") from e
        u = data.get("usage") or {}
        return LLMResponse(text=text, model=self.model, usage={
            "prompt_tokens": int(u.get("prompt_tokens", 0)),
            "completion_tokens": int(u.get("completion_tokens", 0)),
        })


class Gemini(LLMProvider):
    name = "gemini"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return bool(self.settings.gemini_api_key)

    async def complete(self, system: str, user: str, *, task: str, context: dict[str, Any],
                       temperature: float = 0.8) -> LLMResponse:
        model = self.settings.gemini_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        async with http_client(self.settings.http_timeout_seconds,
                               self.settings.http_connect_timeout_seconds) as c:
            r = await request_with_retry(c, "POST", url, json=body,
                                         headers={"x-goog-api-key": self.settings.gemini_api_key})
        data = r.json()
        try:
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"])
        except (KeyError, IndexError, TypeError) as e:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or str(data)[:200]
            raise ProviderError(f"no candidate (blocked?): {reason}") from e
        u = data.get("usageMetadata") or {}
        return LLMResponse(text=text, model=model, usage={
            "prompt_tokens": int(u.get("promptTokenCount", 0)),
            "completion_tokens": int(u.get("candidatesTokenCount", 0)),
        })


class OfflineLLM(LLMProvider):
    """Rule-based fallback. Not a language model - a deterministic, seeded template writer that
    produces schema-valid output for each task, so the pipeline degrades instead of failing."""

    name = "offline"

    async def complete(self, system: str, user: str, *, task: str, context: dict[str, Any],
                       temperature: float = 0.8) -> LLMResponse:
        fn = offline_writer.TASKS.get(task)
        if fn is None:
            raise ProviderError(f"offline writer has no handler for task '{task}'")
        return LLMResponse(text=json.dumps(fn(context)), model="offline-template-v1")


def build_llm_chain(settings: Settings) -> ProviderChain[LLMProvider]:
    catalog: dict[str, Callable[[], LLMProvider]] = {
        "groq": lambda: OpenAICompatible("groq", "https://api.groq.com/openai/v1", settings.groq_model,
                                         settings.groq_api_key, settings),
        "openrouter": lambda: OpenAICompatible(
            "openrouter", "https://openrouter.ai/api/v1", settings.openrouter_model,
            settings.openrouter_api_key, settings,
            extra_headers={"HTTP-Referer": "https://github.com/PremMakwana555/shortforge",
                           "X-Title": "shortforge"}),
        "gemini": lambda: Gemini(settings),
        "ollama": lambda: _Ollama(settings),
        "offline": OfflineLLM,
    }
    return ProviderChain("llm", [catalog[n]() for n in settings.llm_providers if n in catalog])


class _Ollama(OpenAICompatible):
    """Ollama exposes an OpenAI-compatible endpoint. Only enabled when OLLAMA_BASE_URL is set
    explicitly or the default localhost port answers - probed lazily on first use."""

    def __init__(self, settings: Settings):
        super().__init__("ollama", f"{settings.ollama_base_url}/v1", settings.ollama_model, None, settings,
                         needs_key=False)
        self._probed: bool | None = None

    def available(self) -> bool:
        if self._probed is None:
            try:
                httpx.get(f"{self.settings.ollama_base_url}/api/tags", timeout=1.0)
                self._probed = True
            except httpx.HTTPError:
                self._probed = False
        return self._probed


# ----------------------------------------------------------------------------- JSON helpers
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text`` - tolerant of code fences and chatty preambles."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise ValidationFailed("no JSON object in model output") from None
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(cleaned[start:], start):
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(cleaned[start:i + 1])
                        break
                    except json.JSONDecodeError as e:
                        raise ValidationFailed(f"malformed JSON: {e}") from e
        else:
            raise ValidationFailed("unterminated JSON object in model output")
    if not isinstance(obj, dict):
        raise ValidationFailed("top-level JSON must be an object")
    return obj


@dataclass
class JsonResult:
    data: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, int]
    attempts: list[dict[str, Any]]
    degraded: bool


async def generate_json(
    chain: ProviderChain[LLMProvider],
    *,
    task: str,
    system: str,
    user: str,
    context: dict[str, Any],
    validate: Callable[[dict[str, Any]], dict[str, Any]],
    temperature: float = 0.8,
) -> JsonResult:
    """Run the chain; each provider gets one repair attempt if its JSON fails validation."""

    async def call(p: LLMProvider) -> tuple[dict[str, Any], LLMResponse]:
        resp = await p.complete(system, user, task=task, context=context, temperature=temperature)
        try:
            return validate(extract_json(resp.text)), resp
        except ValidationFailed as first:
            repair = (f"{user}\n\nYour previous answer was rejected: {first}.\n"
                      "Return ONLY a corrected JSON object that satisfies every constraint.")
            resp2 = await p.complete(system, repair, task=task, context=context, temperature=0.4)
            resp2.usage = {k: resp.usage.get(k, 0) + resp2.usage.get(k, 0)
                           for k in {*resp.usage, *resp2.usage}}
            return validate(extract_json(resp2.text)), resp2

    res: ChainResult[tuple[dict[str, Any], LLMResponse]] = await chain.run(call)
    data, resp = res.value
    return JsonResult(data=data, provider=res.provider, model=resp.model, usage=resp.usage,
                      attempts=res.attempts, degraded=res.degraded or res.provider == "offline")
