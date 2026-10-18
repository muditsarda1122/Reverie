"""feat/providers — per-provider request shapes.

For every registry provider, the config sets ONLY `provider` (exercising the
registry fill in reverie.config) and these tests assert the exact request
call_llm issues: URL, headers and body. All offline — requests.post is
mocked; keys are test-only env vars.
"""

import json
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from unittest.mock import patch

import pytest

from reverie.config import load_config

KEY_ENV = "REVERIE_REQUEST_SHAPE_KEY"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("reverie.llm._sleep", lambda _s: None)
    monkeypatch.setattr("reverie.llm._jitter", lambda delay: delay)


def make_response(fmt: str):
    payload = (
        {"content": [{"type": "text", "text": "ok"}]}
        if fmt == "anthropic" else
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    )
    return payload


def provider_config(tmp_path, provider: str):
    """A config.yaml that sets only `provider` (+ test key env), resolved."""
    p = tmp_path / f"config-{provider}.yaml"
    p.write_text(f"llm:\n  provider: {provider}\n  api_key_env: {KEY_ENV}\n")
    cfg = load_config(p)
    return cfg


class FakePost:
    """Configurable stand-in capturing the request; returns a format payload."""

    def __init__(self, wire_format: str):
        self.wire_format = wire_format
        self.calls: list = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.wire_format == "anthropic":
            payload = {"content": [{"type": "text", "text": "ok"}]}
        else:
            payload = {"choices": [{"message": {"role": "assistant",
                                                "content": "ok"}}]}
        return FakeResponse(payload)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


ANTHROPIC_PROVIDERS = {"opencode_zen": ("claude-haiku-5-5", "https://opencode.ai/zen/v1"),
                       "anthropic": ("claude-haiku-5-5", "https://api.anthropic.com/v1")}
OPENAI_PROVIDERS = {
    "openai": ("gpt-5-mini", "https://api.openai.com/v1"),
    "google": ("gemini-3.8-flash",
               "https://generativelanguage.googleapis.com/v1beta/openai"),
    "openrouter": ("anthropic/claude-haiku-5.5", "https://openrouter.ai/api/v1"),
    "ollama": ("qwen2.5-coder:14b", "http://localhost:11434/v1"),
}


@pytest.mark.parametrize("provider", sorted(ANTHROPIC_PROVIDERS))
def test_anthropic_format_providers(provider, tmp_path, monkeypatch):
    model, base_url = ANTHROPIC_PROVIDERS[provider]
    cfg = provider_config(tmp_path, provider)
    assert cfg.llm.model == model            # registry default filled
    assert cfg.llm.base_url == base_url
    monkeypatch.setenv(KEY_ENV, "test-key")
    fake = FakePost("anthropic")
    with patch("reverie.llm.requests.post", fake):
        from reverie.llm import call_llm
        assert call_llm("do the thing", system="be brief", config=cfg) == "ok"
    url, kwargs = fake.calls[0]
    assert url == f"{base_url}/messages"
    headers = kwargs["headers"]
    assert headers["x-api-key"] == "test-key"
    assert headers["anthropic-version"] == "2023-06-01"
    body = kwargs["json"]
    assert body["model"] == model
    assert body["system"] == "be brief"
    assert body["max_tokens"] == cfg.llm.max_tokens
    assert body["messages"] == [{"role": "user", "content": "do the thing"}]
    # claude-haiku-5-5 (post-Opus-4.6; both registry defaults): temperature
    # is rejected with a 400 — the request omits it and carries the
    # cheapest thinking config
    assert "temperature" not in body
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("provider", sorted(OPENAI_PROVIDERS))
def test_openai_format_providers(provider, tmp_path, monkeypatch):
    model, base_url = OPENAI_PROVIDERS[provider]
    cfg = provider_config(tmp_path, provider)
    assert cfg.llm.model == model
    assert cfg.llm.base_url == base_url
    if provider == "ollama":
        monkeypatch.delenv(KEY_ENV, raising=False)   # keyless provider
    else:
        monkeypatch.setenv(KEY_ENV, "test-key")
    fake = FakePost("openai")
    with patch("reverie.llm.requests.post", fake):
        from reverie.llm import call_llm
        assert call_llm("do the thing", system="be brief", config=cfg) == "ok"
    url, kwargs = fake.calls[0]
    assert url == f"{base_url}/chat/completions"
    headers = kwargs["headers"]
    if provider == "ollama":
        assert "Authorization" not in headers        # keyless
    else:
        assert headers["Authorization"] == "Bearer test-key"
    body = kwargs["json"]
    assert body["model"] == model
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "do the thing"},
    ]
    if provider == "openai":                          # gpt-5-mini: reasoning shape
        assert "max_completion_tokens" in body
        assert "temperature" not in body
        assert "max_tokens" not in body
    elif provider == "openrouter":
        # anthropic/claude-haiku-5.5: post-Opus-4.6 Claude — no temperature,
        # forwarded through the OpenAI-compatible gateway as-is
        assert "temperature" not in body
        assert body["max_tokens"] == cfg.llm.max_tokens
    else:
        assert body["temperature"] == cfg.llm.temperature
        assert body["max_tokens"] == cfg.llm.max_tokens


# ---------------------------------------------------------------------------
# Phase 3 — post-Opus-4.6 Claude models reject sampling parameters
# ---------------------------------------------------------------------------
# Anthropic docs (checked 2026-10-15):
#   https://platform.claude.com/docs/en/build-with-claude/thinking
#   https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide
# "On Claude Opus 5.5 … Claude Sonnet 5.5 … and Claude Haiku 5.5, non-default
#  temperature, top_p, or top_k values return a 400 error on every request."
# The fix: omit temperature entirely; carry the cheapest valid thinking
# configuration (adaptive thinking is on by default and is billed as output).

CLAUDE_55_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


@pytest.mark.parametrize("model", CLAUDE_55_MODELS)
def test_claude_55_no_sampling_params_sent(model, tmp_path, monkeypatch):
    """The default anthropic provider (claude-haiku-5-5) and the other 5.5
    models must not get temperature/top_p/top_k — they 400 on it."""
    cfg = load_config(write_llm_yaml(tmp_path, model))
    monkeypatch.setenv(KEY_ENV, "test-key")
    fake = FakePost("anthropic")
    with patch("reverie.llm.requests.post", fake):
        from reverie.llm import call_llm
        assert call_llm("classify", config=cfg, temperature=0.0) == "ok"
    body = fake.calls[0][1]["json"]
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body
    assert body["max_tokens"] == cfg.llm.max_tokens   # still required


def write_llm_yaml(tmp_path, model: str):
    p = tmp_path / f"config-{model}.yaml"
    p.write_text("llm:\n"
                 "  provider: anthropic\n"
                 f"  model: {model}\n"
                 f"  api_key_env: {KEY_ENV}\n")
    return p


@pytest.mark.parametrize("model, expected", [
    ("claude-haiku-5-5",
     {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}),
    ("claude-sonnet-5-5",
     {"thinking": {"type": "between_tools"},
      "output_config": {"effort": "low"}}),
    ("claude-opus-5-5",
     {"output_config": {"effort": "low"}}),
])
def test_claude_55_cheapest_thinking_config(model, expected, tmp_path,
                                            monkeypatch):
    """Haiku disables thinking (allowed at effort ≤ high); Sonnet uses the
    documented between_tools alternative; Opus cannot disable — effort only.
    Thinking tokens are billed as output, so 'low' is the cheapest valid
    configuration for Reverie's short calls."""
    cfg = load_config(write_llm_yaml(tmp_path, model))
    monkeypatch.setenv(KEY_ENV, "test-key")
    fake = FakePost("anthropic")
    with patch("reverie.llm.requests.post", fake):
        from reverie.llm import call_llm
        call_llm("classify", config=cfg, temperature=0.0)
    body = fake.calls[0][1]["json"]
    for key, value in expected.items():
        assert body[key] == value


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-5"])
def test_pre_46_claude_models_keep_temperature(model, tmp_path, monkeypatch):
    """Older Claude models accept temperature — the default Zen model must
    not lose it."""
    cfg = load_config(write_llm_yaml(tmp_path, model))
    monkeypatch.setenv(KEY_ENV, "test-key")
    fake = FakePost("anthropic")
    with patch("reverie.llm.requests.post", fake):
        from reverie.llm import call_llm
        call_llm("classify", config=cfg, temperature=0.0)
    body = fake.calls[0][1]["json"]
    assert body["temperature"] == 0.0
    assert "thinking" not in body
    assert "output_config" not in body


@pytest.mark.parametrize("model, expected", [
    ("claude-haiku-5-5", True),
    ("claude-sonnet-5-5", True),
    ("claude-opus-5-5", True),
    ("anthropic/claude-haiku-5.5", True),          # OpenRouter slug
    ("claude-sonnet-5-5-20261007", True),          # dated suffix
    ("claude-opus-5", True),
    ("claude-sonnet-5", True),
    ("claude-opus-4-7", True),
    ("claude-opus-4-8", True),
    ("claude-haiku-4-5", False),                   # accepts temperature
    ("claude-sonnet-4-5", False),
    ("qwen2.5-coder:14b", False),
    ("gpt-5-mini", False),
])
def test_claude_rejects_sampling_rule(model, expected):
    from reverie.llm import _claude_rejects_sampling
    assert _claude_rejects_sampling(model) is expected


def test_claude_no_sampling_config_override(tmp_path, monkeypatch):
    """llm.claude_no_sampling forces the behaviour either way, like
    llm.openai_reasoning."""
    p = tmp_path / "config-force.yaml"
    p.write_text("llm:\n"
                 "  provider: anthropic\n"
                 "  model: claude-haiku-4-5\n"
                 f"  api_key_env: {KEY_ENV}\n"
                 "  claude_no_sampling: true\n")
    cfg = load_config(p)
    monkeypatch.setenv(KEY_ENV, "test-key")
    fake = FakePost("anthropic")
    with patch("reverie.llm.requests.post", fake):
        from reverie.llm import call_llm
        call_llm("classify", config=cfg, temperature=0.0)
    body = fake.calls[0][1]["json"]
    assert "temperature" not in body               # forced on a 4-5 model
    assert "thinking" not in body                  # thinking config is
    assert "output_config" not in body             # name-gated to the 5.5 trio
