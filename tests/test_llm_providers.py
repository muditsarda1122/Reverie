"""Phase 13 tests — LLM provider abstraction (design doc §1, D48).

call_llm routes to Anthropic Messages format for Claude models and OpenAI
Chat Completions format for everything else (GPT, Ollama's OpenAI-compatible
endpoint, vLLM, LM Studio, ...). The wire format is derived from the model
name, never from the informational ``provider`` config key.

Run: .venv/bin/python -m pytest tests/test_llm_providers.py -v
All offline: requests.post is mocked; no network, no API keys required
(the key env var is a per-test fixture name set/cleared by monkeypatch).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import copy
import json
from unittest.mock import patch

import pytest

from ec.config import DEFAULT_CONFIG, _to_attrdict
from ec.install import OLLAMA_BASE_URL, OLLAMA_LLM_BLOCK
from ec.llm import LLMError, _api_format, api_key, call_llm

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

KEY_ENV = "EC_TEST_LLM_KEY"


def make_config(model="claude-haiku-4-5", base_url="https://zen.example/v1"):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["llm"].update({
        "model": model,
        "base_url": base_url,
        "api_key_env": KEY_ENV,
        "timeout": 5,
    })
    return _to_attrdict(cfg)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


ANTHROPIC_PAYLOAD = {
    "content": [
        {"type": "text", "text": "hello "},
        {"type": "tool_use", "id": "x", "name": "f", "input": {}},
        {"type": "text", "text": "world"},
    ],
}
OPENAI_PAYLOAD = {
    "choices": [{"message": {"role": "assistant", "content": "hi there"}}],
}


@pytest.fixture()
def anthropic_cfg():
    return make_config(model="claude-haiku-4-5")


@pytest.fixture()
def openai_cfg():
    return make_config(model="gpt-4o-mini")


# ---------------------------------------------------------------------------
# model -> format routing (D48)
# ---------------------------------------------------------------------------

class TestApiFormatRouting:
    @pytest.mark.parametrize("model", [
        "claude-haiku-4-5",
        "claude-sonnet-4",
        "claude-opus-4-1",
        "claudev1",          # prefix match, per design doc §1.2
    ])
    def test_claude_models_use_anthropic_format(self, model):
        assert _api_format(model) == "anthropic"

    @pytest.mark.parametrize("model", [
        "gpt-4o-mini",
        "gpt-5",
        "qwen2.5-coder:14b",
        "llama3.1:70b",
        "mistral-small",
        "deepseek-coder-v2",
    ])
    def test_other_models_use_openai_format(self, model):
        assert _api_format(model) == "openai"

    def test_provider_key_is_informational_not_routing(self, openai_cfg):
        """A 'provider' value must not flip routing — the model name decides."""
        openai_cfg.llm.provider = "opencode_zen"
        assert _api_format(openai_cfg.llm.model) == "openai"


# ---------------------------------------------------------------------------
# Anthropic Messages format
# ---------------------------------------------------------------------------

class TestCallAnthropic:
    def test_anthropic_wire_format(self, anthropic_cfg, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-test")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=ANTHROPIC_PAYLOAD)) as m:
            out = call_llm("do the thing", system="be brief",
                           config=anthropic_cfg)

        assert out == "hello world"          # only text blocks are joined
        req = m.call_args
        assert req.args[0] == "https://zen.example/v1/messages"
        headers = req.kwargs["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"
        body = req.kwargs["json"]
        assert body["model"] == "claude-haiku-4-5"
        assert body["messages"] == [{"role": "user", "content": "do the thing"}]
        assert body["system"] == "be brief"
        assert body["temperature"] == anthropic_cfg.llm.temperature
        assert body["max_tokens"] == anthropic_cfg.llm.max_tokens
        assert req.kwargs["timeout"] == 5

    def test_trailing_slash_base_url_is_normalized(self, anthropic_cfg,
                                                   monkeypatch):
        anthropic_cfg.llm.base_url = "https://zen.example/v1/"
        monkeypatch.setenv(KEY_ENV, "sk-test")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=ANTHROPIC_PAYLOAD)) as m:
            call_llm("x", config=anthropic_cfg)
        assert m.call_args.args[0] == "https://zen.example/v1/messages"

    def test_temperature_and_max_tokens_overrides(self, anthropic_cfg,
                                                  monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-test")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=ANTHROPIC_PAYLOAD)) as m:
            call_llm("x", temperature=0.0, max_tokens=128,
                     config=anthropic_cfg)
        body = m.call_args.kwargs["json"]
        assert body["temperature"] == 0.0     # mode detection uses 0.0
        assert body["max_tokens"] == 128

    def test_missing_key_raises_with_zen_guidance(self, anthropic_cfg,
                                                  monkeypatch):
        monkeypatch.delenv(KEY_ENV, raising=False)
        with pytest.raises(LLMError) as excinfo:
            call_llm("x", config=anthropic_cfg)
        assert KEY_ENV in str(excinfo.value)
        assert "opencode.ai" in str(excinfo.value)

    def test_non_200_raises_llmerror(self, anthropic_cfg, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-test")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(status_code=503, payload={})):
            with pytest.raises(LLMError, match="HTTP 503"):
                call_llm("x", config=anthropic_cfg)

    def test_network_failure_raises_llmerror(self, anthropic_cfg, monkeypatch):
        import requests as _requests
        monkeypatch.setenv(KEY_ENV, "sk-test")
        with patch("ec.llm.requests.post",
                   side_effect=_requests.ConnectionError("refused")):
            with pytest.raises(LLMError, match="request failed"):
                call_llm("x", config=anthropic_cfg)

    def test_malformed_body_raises_llmerror(self, anthropic_cfg, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-test")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload={"content": "oops"})):
            with pytest.raises(LLMError, match="Malformed"):
                call_llm("x", config=anthropic_cfg)


# ---------------------------------------------------------------------------
# OpenAI Chat Completions format
# ---------------------------------------------------------------------------

class TestCallOpenai:
    def test_openai_wire_format(self, openai_cfg, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-oai")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            out = call_llm("do the thing", system="be brief",
                           config=openai_cfg)

        assert out == "hi there"
        req = m.call_args
        assert req.args[0] == "https://zen.example/v1/chat/completions"
        headers = req.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-oai"
        body = req.kwargs["json"]
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "do the thing"},
        ]
        assert body["temperature"] == openai_cfg.llm.temperature
        assert body["max_tokens"] == openai_cfg.llm.max_tokens

    def test_no_system_prompt_omits_system_message(self, openai_cfg,
                                                   monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-oai")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("just this", config=openai_cfg)
        assert m.call_args.kwargs["json"]["messages"] == [
            {"role": "user", "content": "just this"}
        ]

    def test_non_200_raises_llmerror(self, openai_cfg, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-oai")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(status_code=404, payload={})):
            with pytest.raises(LLMError, match="HTTP 404"):
                call_llm("x", config=openai_cfg)

    def test_malformed_body_raises_llmerror(self, openai_cfg, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-oai")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload={"nope": []})):
            with pytest.raises(LLMError, match="Malformed"):
                call_llm("x", config=openai_cfg)


# ---------------------------------------------------------------------------
# Ollama fallback (§15 llm block / §28.8) — configured-but-broken no longer
# ---------------------------------------------------------------------------

class TestOllamaNoApiKey:
    def test_ollama_works_without_any_api_key(self, monkeypatch):
        """The D42 offline fallback needs no key: no Authorization header,
        request still issued in OpenAI Chat format against /v1."""
        cfg = make_config(model="qwen2.5-coder:14b",
                          base_url="http://localhost:11434/v1")
        monkeypatch.delenv(KEY_ENV, raising=False)
        assert api_key(cfg) is None
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            out = call_llm("classify these pairs",
                           system="you are a classifier", config=cfg)
        assert out == "hi there"
        req = m.call_args
        assert req.args[0] == "http://localhost:11434/v1/chat/completions"
        assert "Authorization" not in req.kwargs["headers"]

    def test_installer_writes_v1_base_url(self):
        """Phase 13 fix: the D42 Ollama block points at the OpenAI-compatible
        endpoint (…/v1), which _call_openai appends /chat/completions to."""
        assert OLLAMA_BASE_URL == "http://localhost:11434/v1"
        assert OLLAMA_LLM_BLOCK["base_url"] == OLLAMA_BASE_URL
