"""LLM client — provider-abstracted (D48, Phase 13; registry-driven feat/providers).

Two wire formats cover every supported backend:

- **Anthropic Messages API** — Claude models via OpenCode Zen or the direct
  Anthropic API (``POST {base_url}/messages``).
- **OpenAI Chat Completions API** — GPT models, Ollama's OpenAI-compatible
  endpoint, and most third-party providers: vLLM, LM Studio, Together, Groq
  (``POST {base_url}/chat/completions``).

The format is derived from the model name (``_api_format``) unless
``llm.wire_format`` is set, in which case the config wins — this is what
allows ``claude-*`` models on OpenAI-compatible gateways. The ``provider``
config key selects a registry entry (``ec/providers.py``): unset ``llm``
fields are filled from it, and the registry tells the client whether the
provider needs a key (so a missing key surfaces as a clear message instead
of a raw 401).

Retries: transient failures (429, 5xx, timeouts, connection errors) are
retried with capped exponential backoff and jitter, honouring ``Retry-After``
when the server sends one. Other 4xx errors are never retried. Policy keys:
``llm.retry_attempts`` / ``retry_backoff`` / ``retry_backoff_cap``. Tests
inject time by patching ``ec.llm._sleep`` / ``ec.llm._jitter``.

Temperature: 0.3 for extraction/diffusion, 0.0 for mode detection
(config keys llm.temperature / llm.temperature_mode_detection).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from urllib.parse import urlparse

import requests

from . import providers
from .config import get_config

log = logging.getLogger("ec.llm")

#: Injectable so tests stay fast and deterministic (monkeypatch these).
_sleep = time.sleep


def _jitter(delay: float) -> float:
    """±10% jitter on a computed backoff delay (identity-patchable in tests)."""
    return delay * random.uniform(0.9, 1.1)


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
    """Return the configured API key, or None (unset env var / keyless provider)."""
    cfg = config or get_config()
    env = cfg.llm.api_key_env
    return os.environ.get(env) if env else None


def model_for(config, task: str | None = None) -> str:
    """The effective model for one call: per-task override, else ``llm.model``.

    Tasks are ``extraction``, ``classification``, ``mode_detection`` (config
    ``llm.models.<task>``); every unset override falls back to ``llm.model``.
    """
    if task is not None:
        models = getattr(config.llm, "models", None)
        override = getattr(models, task, None) if models is not None else None
        if override:
            return override
    return config.llm.model


def _api_format(model: str, config=None) -> str:
    """Determine the API format (D48 + the ``llm.wire_format`` override).

    A set ``llm.wire_format`` beats the model-name rule (this is what lets
    a ``claude-*`` model run on an OpenAI-compatible gateway). Without it:
    Claude models speak Anthropic Messages; everything else — GPT, Qwen,
    Llama, Mistral — speaks OpenAI Chat Completions.
    """
    cfg = config or get_config()
    fmt = getattr(cfg.llm, "wire_format", None)
    if fmt in ("anthropic", "openai"):
        return fmt
    return "anthropic" if model.startswith("claude") else "openai"


# ---------------------------------------------------------------------------
# reasoning-model-safe OpenAI parameters (feat/providers)
# ---------------------------------------------------------------------------

#: Conservative name rule for OpenAI reasoning-family models, which reject
#: ``temperature`` and require ``max_completion_tokens``. Vendor prefixes
#: (OpenRouter-style ``openai/gpt-5-mini``) are stripped first. Matches
#: o1/o3/o4 families and the gpt-5 family; deliberately does NOT match
#: gpt-4o/gpt-4.1. Users can force either shape with ``llm.openai_reasoning``.
_REASONING_MODEL_RE = re.compile(r"^(?:o[134](?:[-._]|$)|gpt-5)")


def _is_openai_reasoning_model(model: str) -> bool:
    name = model.rsplit("/", 1)[-1]          # strip a vendor/ prefix
    return bool(_REASONING_MODEL_RE.match(name))


def _reasoning_params(cfg, model: str) -> bool:
    """Whether the OpenAI-format request must use the reasoning shape.

    Config ``llm.openai_reasoning``: True/False force; None = auto by name.
    """
    flag = getattr(cfg.llm, "openai_reasoning", None)
    if flag is None:
        return _is_openai_reasoning_model(model)
    return bool(flag)


# ---------------------------------------------------------------------------
# key handling — provider-aware, up-front (feat/providers)
# ---------------------------------------------------------------------------

def missing_key_message(cfg) -> str:
    """Actionable message for a missing key, naming the configured provider."""
    env = cfg.llm.api_key_env
    entry = providers.get(cfg.llm.provider)
    if entry is not None and entry.key_url:
        return (
            f"{env} is not set. EC is configured to use {entry.name} "
            f"({cfg.llm.model}) for extraction, relationship classification, "
            f"and mode detection. Get a key at {entry.key_url} — then: "
            f"export {env}='your-key'"
        )
    return (
        f"{env} is not set. EC needs an API key for extraction, relationship "
        f"classification, and mode detection. Then: export {env}='your-key'"
    )


def key_missing(config=None) -> str | None:
    """The env var a required key would come from, or None when a call may proceed.

    Rules: the Anthropic format always needs a key (its auth is the
    ``x-api-key`` header); the OpenAI format needs one only when the
    configured provider's registry entry says so (Ollama and ``custom``
    work keyless). A loopback base URL never needs a key — this keeps
    hand-edited local configs working even when the provider name still
    says something else. None means "fine to send".
    """
    cfg = config or get_config()
    if api_key(cfg):
        return None
    if _is_local_base_url(cfg.llm.base_url):
        return None
    if _api_format(cfg.llm.model, cfg) == "anthropic":
        return cfg.llm.api_key_env or "EC_LLM_API_KEY"
    entry = providers.get(cfg.llm.provider)
    if entry is not None and not entry.key_required:
        return None
    return cfg.llm.api_key_env or "EC_LLM_API_KEY"


def _is_local_base_url(base_url: str | None) -> bool:
    """True when the endpoint is loopback (local models need no key)."""
    if not base_url:
        return False
    try:
        host = urlparse(str(base_url)).hostname or ""
    except ValueError:
        return False
    return host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


# ---------------------------------------------------------------------------
# retry with backoff (feat/providers)
# ---------------------------------------------------------------------------

def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header; date-form values are ignored."""
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _backoff_delay(cfg, attempt: int, retry_after: str | None = None) -> float:
    """Capped exponential backoff, lifted to Retry-After when larger."""
    base = float(getattr(cfg.llm, "retry_backoff", 0.5))
    cap = float(getattr(cfg.llm, "retry_backoff_cap", 8.0))
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    parsed = _parse_retry_after(retry_after)
    if parsed is not None:
        delay = max(delay, min(cap, parsed))
    return _jitter(delay)


_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


def _post_with_retries(url, headers, body, timeout, cfg):
    """POST with retry on 429/5xx/timeouts; other 4xx fail immediately.

    Returns the 200 ``requests.Response``. Raises ``LLMError`` otherwise.
    """
    attempts = max(1, int(getattr(cfg.llm, "retry_attempts", 3)))
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == attempts:
                raise LLMError(
                    f"LLM API request failed after {attempt} attempt(s): {exc}"
                ) from exc
            delay = _backoff_delay(cfg, attempt)
            log.warning(
                "LLM request failed (%s); retrying in %.1fs (attempt %d/%d)",
                exc, delay, attempt + 1, attempts,
            )
            _sleep(delay)
            continue
        if resp.status_code == 200:
            return resp
        retryable = resp.status_code in _RETRYABLE_STATUSES
        if not retryable or attempt == attempts:
            log.error("LLM API returned %s: %s", resp.status_code, resp.text[:500])
            raise LLMError(
                f"LLM API returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        delay = _backoff_delay(cfg, attempt, resp.headers.get("Retry-After"))
        log.warning(
            "LLM API returned %d; retrying in %.1fs (attempt %d/%d)",
            resp.status_code, delay, attempt + 1, attempts,
        )
        _sleep(delay)
    raise LLMError("unreachable: retry loop exhausted")   # pragma: no cover


# ---------------------------------------------------------------------------
# the call
# ---------------------------------------------------------------------------

def call_llm(
    prompt: str,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
    config=None,
) -> str:
    """Single-turn call to the configured LLM. Returns the text content.

    ``model`` (optional) overrides the configured model for this one call —
    the per-task overrides in ``ec.llm.model_for`` feed through here. The
    contract stays string-in, string-out.

    Routes to the correct wire format based on the effective model (D48),
    unless ``cfg.llm.wire_format`` overrides. base_url and api_key_env come
    from config. Raises LLMError (never crashes) on: missing key where one
    is required, non-200 response after retries, network failure, or
    malformed response body.

    OpenAI-format providers such as Ollama require no API key — an unset
    key env var simply omits the Authorization header.
    """
    cfg = config or get_config()
    model = model or model_for(cfg)
    fmt = _api_format(model, cfg)

    if key_missing(cfg) is not None:
        raise LLMError(missing_key_message(cfg))

    call_args = dict(
        base_url=cfg.llm.base_url.rstrip("/"),
        api_key=api_key(cfg) or "",
        model=model,
        prompt=prompt,
        system=system,
        temperature=cfg.llm.temperature if temperature is None else temperature,
        max_tokens=max_tokens or cfg.llm.max_tokens,
        timeout=cfg.llm.timeout,
        config=cfg,
    )
    if fmt == "anthropic":
        return _call_anthropic(**call_args)
    return _call_openai(**call_args,
                        reasoning=_reasoning_params(cfg, model))


def _call_anthropic(base_url, api_key, model, prompt, system,
                    temperature, max_tokens, timeout, config=None) -> str:
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

    resp = _post_with_retries(
        url, headers, body, timeout, config or get_config())
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
                 temperature, max_tokens, timeout, reasoning=False,
                 config=None) -> str:
    """OpenAI Chat Completions API format (GPT, Ollama, and others).

    Ollama does not require an API key — the Authorization header is only
    sent when a key is present (empty string skips it).

    Reasoning-family models (o-series, gpt-5) reject ``temperature`` and
    want ``max_completion_tokens`` instead of ``max_tokens`` — those params
    are simply not sent (``reasoning=True``).
    """
    url = f"{base_url}/chat/completions"
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict = {"model": model, "messages": messages}
    if reasoning:
        body["max_completion_tokens"] = max_tokens
    else:
        body["temperature"] = temperature
        body["max_tokens"] = max_tokens

    resp = _post_with_retries(
        url, headers, body, timeout, config or get_config())
    try:
        payload = resp.json()
        return payload["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, AttributeError, KeyError, IndexError) as exc:
        log.error("Malformed LLM API response: %s", resp.text[:500])
        raise LLMError(f"Malformed LLM API response: {exc}") from exc
