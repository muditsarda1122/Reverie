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

KEY_ENV = "EC_REQUEST_SHAPE_KEY"


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


ANTHROPIC_PROVIDERS = {"opencode_zen": ("claude-haiku-4-5", "https://opencode.ai/zen/v1"),
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
    assert body["temperature"] == cfg.llm.temperature
    assert body["messages"] == [{"role": "user", "content": "do the thing"}]


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
    else:
        assert body["temperature"] == cfg.llm.temperature
        assert body["max_tokens"] == cfg.llm.max_tokens
