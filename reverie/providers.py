"""Provider registry — bring-your-own-model table (feat/providers).

Static, stdlib-only table describing every supported LLM provider: where to
reach it, which environment variable holds the key (plus accepted aliases),
which wire format it speaks, a sensible small/cheap default model, whether a
key is required, and where to get one.

``provider`` in ``~/.reverie/config.yaml`` selects an entry; reverie/config.py fills
any llm field the user did not set explicitly from this table (``custom``
and unknown names are left untouched — today's behaviour).

Wire formats:
- ``anthropic`` — Anthropic Messages API (``POST {base_url}/messages``,
  ``x-api-key`` header).
- ``openai``   — OpenAI Chat Completions API (``POST {base_url}/chat/completions``,
  ``Authorization: Bearer`` when a key is present).
- ``None``     — derive from the model name (D48): ``claude*`` -> anthropic,
  anything else -> openai. Used by multi-family endpoints (OpenCode Zen) and
  ``custom``.

Default models are small, cheap, current models that can produce the
extractor's JSON; the choice and its source are recorded in each comment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """One LLM provider's static connection details."""

    key: str                          # config value of llm.provider
    name: str                         # human-readable (installer output)
    base_url: str | None
    api_key_env: str | None
    key_aliases: tuple[str, ...] = ()  # alternative env var names
    # None = derive the wire format from the model name (D48).
    wire_format: str | None = None     # "anthropic" | "openai" | None
    default_model: str | None = None
    key_required: bool = True
    key_url: str | None = None


REGISTRY: dict[str, Provider] = {
    # claude-haiku-5-5: current cheap/fast default (review fix 2 — the old
    # claude-haiku-4-5 default was verified to be the model name the smoke
    # test tripped over). Zen serves several model families, so the wire
    # format stays model-name-derived (None), not pinned.
    "opencode_zen": Provider(
        key="opencode_zen",
        name="OpenCode Zen",
        base_url="https://opencode.ai/zen/v1",
        api_key_env="OPENCODE_ZEN_API_KEY",
        wire_format=None,
        default_model="claude-haiku-5-5",
        key_required=True,
        key_url="https://opencode.ai/auth",
    ),
    # claude-haiku-5-5: Anthropic's cheapest/fastest small model, released
    # 2026-10-07; $0.10/$0.50 per MTok for prompts up to 100K (the extractor
    # prompt is ~8.5K tokens, well inside that tier). Sources checked
    # 2026-10-14: https://www.anthropic.com/claude/haiku and
    # https://docs.anthropic.com/en/docs/about-claude/pricing
    "anthropic": Provider(
        key="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        wire_format="anthropic",
        default_model="claude-haiku-5-5",
        key_required=True,
        key_url="https://console.anthropic.com/settings/keys",
    ),
    # gpt-5-mini: $0.25/$2 per MTok; OpenAI's documented cheap model for
    # low-latency, high-volume workloads (successor to o4-mini). A reasoning
    # model — it rejects `temperature` and needs `max_completion_tokens`;
    # the auto rule in reverie.llm handles the request shape. Source checked
    # 2026-10-14: https://developers.openai.com/api/docs/models/gpt-5-mini
    # and https://developers.openai.com/api/docs/pricing
    "openai": Provider(
        key="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        wire_format="openai",
        default_model="gpt-5-mini",
        key_required=True,
        key_url="https://platform.openai.com/api-keys",
    ),
    # gemini-3.8-flash: current stable Gemini Flash ("for any new projects,
    # use ... 3.8 Flash" per the model docs). Google serves an OpenAI-
    # compatible endpoint at {base_url}/chat/completions with a Bearer
    # GEMINI_API_KEY. Sources checked 2026-10-14:
    # https://ai.google.dev/gemini-api/docs/models and
    # https://ai.google.dev/gemini-api/docs/openai
    "google": Provider(
        key="google",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        key_aliases=("GOOGLE_API_KEY",),
        wire_format="openai",
        default_model="gemini-3.8-flash",
        key_required=True,
        key_url="https://aistudio.google.com/apikey",
    ),
    # anthropic/claude-haiku-5.5: $0.10/$0.50 per MTok, added 2026-10-07.
    # OpenRouter is OpenAI-compatible only (its own docs: "OpenRouter's API
    # is OpenAI-compatible"), so the wire format is pinned to openai — which
    # also means the vendor-prefixed slug never trips the claude* name rule.
    # Source checked 2026-10-14:
    # https://openrouter.ai/anthropic/claude-haiku-5.5
    "openrouter": Provider(
        key="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        wire_format="openai",
        default_model="anthropic/claude-haiku-5.5",
        key_required=True,
        key_url="https://openrouter.ai/keys",
    ),
    # qwen2.5-coder:14b: the existing D42 offline fallback model (the
    # installer pulls it); keyless local inference. Sources: the repo's own
    # D42/D48 history (reverie/install.py); Ollama's OpenAI-compatible endpoint
    # lives at http://localhost:11434/v1.
    "ollama": Provider(
        key="ollama",
        name="Ollama (local)",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        wire_format="openai",
        default_model="qwen2.5-coder:14b",
        key_required=False,
        key_url=None,
    ),
    # No defaults: every llm field the user sets is used as-is and nothing
    # is filled in — exactly today's behaviour.
    "custom": Provider(
        key="custom",
        name="Custom",
        base_url=None,
        api_key_env=None,
        wire_format=None,
        default_model=None,
        key_required=False,
        key_url=None,
    ),
}

#: Providers the installer's interactive menu offers (custom is manual-only).
MENU_KEYS: tuple[str, ...] = (
    "anthropic", "openai", "google", "openrouter", "opencode_zen", "ollama",
)


def get(name: str | None) -> Provider | None:
    """Registry entry for a provider name, or None (custom/unknown)."""
    return REGISTRY.get(name) if name else None


def provider_names() -> list[str]:
    """Known provider names (for error messages and --provider validation)."""
    return list(REGISTRY)


def key_env_for(entry: Provider, environ=None) -> str | None:
    """The env var (primary or alias) currently providing this provider's key.

    Returns the NAME of the environment variable that is set, or None when
    none of the provider's key variables carry a value.
    """
    environ = os.environ if environ is None else environ
    for env in (entry.api_key_env, *entry.key_aliases):
        if env and environ.get(env):
            return env
    return None


def detect_available(environ=None) -> list[str]:
    """Registry keys whose key is present in the environment (registry order)."""
    environ = os.environ if environ is None else environ
    return [
        key for key, entry in REGISTRY.items()
        if entry.api_key_env and key_env_for(entry, environ)
    ]


def llm_block(key: str) -> dict:
    """The config ``llm`` block for a provider (fields the installer merges).

    Only non-None fields are written; ``custom`` therefore writes nothing
    beyond the provider name.
    """
    entry = REGISTRY[key]
    block: dict = {"provider": key}
    if entry.base_url is not None:
        block["base_url"] = entry.base_url
    if entry.default_model is not None:
        block["model"] = entry.default_model
    if entry.api_key_env is not None:
        block["api_key_env"] = entry.api_key_env
    if entry.wire_format is not None:
        block["wire_format"] = entry.wire_format
    return block
