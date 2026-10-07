"""LLM client — provider-abstracted (D48, Phase 13).

Two wire formats cover every supported backend:

- **Anthropic Messages API** — Claude models via OpenCode Zen or the direct
  Anthropic API (``POST {base_url}/messages``).
- **OpenAI Chat Completions API** — GPT models, Ollama's OpenAI-compatible
  endpoint, and most third-party providers: vLLM, LM Studio, Together, Groq
  (``POST {base_url}/chat/completions``).

The format is derived from the model name (``_api_format``), not from the
``provider`` config key — ``provider`` stays informational (installer
detection). Adding a provider that speaks OpenAI Chat is a pure config
change; a new wire format needs one more ``_call_*`` function and one more
branch in ``_api_format``.

Temperature: 0.3 for extraction/diffusion, 0.0 for mode detection
(config keys llm.temperature / llm.temperature_mode_detection).
"""

from __future__ import annotations

import json
import logging
import os

import requests

from .config import get_config

log = logging.getLogger("ec.llm")


class LLMError(RuntimeError):
    """Raised on any LLM failure — callers get a clear message, never a crash."""


def extract_json(text: str) -> str:
    """Strip markdown code fences Claude wraps around JSON output.

    The JSON content itself is always valid once extracted (§15 JSON
    post-processing block). Exact implementation from the spec.
    """
    if "```" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            return text[start:end]
    return text.strip()


def api_key(config=None) -> str | None:
    """Return the configured API key, or None if the env var is unset."""
    cfg = config or get_config()
    return os.environ.get(cfg.llm.api_key_env)


def _api_format(model: str) -> str:
    """Determine the API format from the model name (D48).

    Claude models speak Anthropic Messages; everything else — GPT, Qwen,
    Llama, Mistral — speaks OpenAI Chat Completions.
    """
    return "anthropic" if model.startswith("claude") else "openai"


def call_llm(
    prompt: str,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    config=None,
) -> str:
    """Single-turn call to the configured LLM. Returns the text content.

    Routes to the correct wire format based on ``cfg.llm.model`` (D48).
    base_url and api_key_env come from config. Raises LLMError (never
    crashes) on: missing key where one is required, non-200 response,
    network failure, or malformed response body.

    OpenAI-format providers such as Ollama require no API key — an unset
    key env var simply omits the Authorization header.
    """
    cfg = config or get_config()
    fmt = _api_format(cfg.llm.model)
    key = api_key(cfg) or ""

    if fmt == "anthropic" and not key:
        raise LLMError(
            f"{cfg.llm.api_key_env} is not set. EC needs an OpenCode Zen API key "
            "for extraction, relationship classification, and mode detection. "
            "Get one at https://opencode.ai/auth — then: "
            f"export {cfg.llm.api_key_env}='your-key'"
        )

    args = (
        cfg.llm.base_url.rstrip("/"),
        key,
        cfg.llm.model,
        prompt,
        system,
        cfg.llm.temperature if temperature is None else temperature,
        max_tokens or cfg.llm.max_tokens,
        cfg.llm.timeout,
    )
    if fmt == "anthropic":
        return _call_anthropic(*args)
    return _call_openai(*args)


def _call_anthropic(base_url, api_key, model, prompt, system,
                    temperature, max_tokens, timeout) -> str:
    """Anthropic Messages API format (Claude models via Zen or direct)."""
    url = f"{base_url}/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        log.error("LLM API request failed: %s", exc)
        raise LLMError(f"LLM API request failed: {exc}") from exc

    if resp.status_code != 200:
        log.error("LLM API returned %s: %s", resp.status_code, resp.text[:500])
        raise LLMError(
            f"LLM API returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        payload = resp.json()
        return "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
    except (json.JSONDecodeError, AttributeError, KeyError) as exc:
        log.error("Malformed LLM API response: %s", resp.text[:500])
        raise LLMError(f"Malformed LLM API response: {exc}") from exc


def _call_openai(base_url, api_key, model, prompt, system,
                 temperature, max_tokens, timeout) -> str:
    """OpenAI Chat Completions API format (GPT, Ollama, and others).

    Ollama does not require an API key — the Authorization header is only
    sent when a key is present (empty string skips it).
    """
    url = f"{base_url}/chat/completions"
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        log.error("LLM API request failed: %s", exc)
        raise LLMError(f"LLM API request failed: {exc}") from exc

    if resp.status_code != 200:
        log.error("LLM API returned %s: %s", resp.status_code, resp.text[:500])
        raise LLMError(
            f"LLM API returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        payload = resp.json()
        return payload["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, AttributeError, KeyError, IndexError) as exc:
        log.error("Malformed LLM API response: %s", resp.text[:500])
        raise LLMError(f"Malformed LLM API response: {exc}") from exc
