"""feat/providers — LLM client feature tests.

All offline: requests.post mocked, sleep/jitter injected. Covers the
wire_format override, reasoning-model-safe OpenAI params, per-task models,
provider-aware missing-key messages, and retry-with-backoff policy.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import copy
import json
from unittest.mock import patch

import pytest

from ec.config import DEFAULT_CONFIG, _to_attrdict
from ec.llm import (
    LLMError,
    _api_format,
    _backoff_delay,
    _is_openai_reasoning_model,
    key_missing,
    missing_key_message,
    model_for,
    call_llm,
)

KEY_ENV = "EC_TEST_LLM_KEY"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retry tests count attempts, never sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr("ec.llm._sleep", sleeps.append)
    monkeypatch.setattr("ec.llm._jitter", lambda delay: delay)
    return sleeps


def make_config(model="claude-haiku-4-5", base_url="https://zen.example/v1",
                **llm_overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["llm"].update({
        "model": model,
        "base_url": base_url,
        "api_key_env": KEY_ENV,
        "timeout": 5,
        "provider": "custom",          # no registry interference by default
    })
    cfg["llm"].update(llm_overrides)
    return _to_attrdict(cfg)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


ANTHROPIC_PAYLOAD = {"content": [{"type": "text", "text": "hi"}]}
OPENAI_PAYLOAD = {"choices": [{"message": {"role": "assistant",
                                           "content": "hi"}}]}


# ---------------------------------------------------------------------------
# wire_format override
# ---------------------------------------------------------------------------

class TestWireFormatOverride:
    def test_unset_uses_model_name_rule(self):
        cfg = make_config(model="claude-haiku-4-5", wire_format=None)
        assert _api_format(cfg.llm.model, cfg) == "anthropic"

    @pytest.mark.parametrize("fmt", ["anthropic", "openai"])
    def test_set_override_beats_model_name(self, fmt):
        cfg = make_config(model="gpt-4o-mini", wire_format=fmt)
        assert _api_format(cfg.llm.model, cfg) == fmt

    def test_override_ignores_model_name(self):
        cfg = make_config(model="claude-haiku-4-5", wire_format="openai")
        assert _api_format(cfg.llm.model, cfg) == "openai"

    def test_claude_model_on_openai_gateway_sends_chat_completions(
            self, monkeypatch):
        """The fix this override exists for: claude-* on /chat/completions."""
        cfg = make_config(model="claude-haiku-4-5",
                          base_url="https://gw.example/v1",
                          wire_format="openai")
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("x", config=cfg)
        assert m.call_args.args[0] == "https://gw.example/v1/chat/completions"
        assert m.call_args.kwargs["headers"]["Authorization"] == "Bearer k"

    def test_invalid_override_falls_back_to_name_rule(self):
        cfg = make_config(model="claude-haiku-4-5", wire_format="nonsense")
        assert _api_format(cfg.llm.model, cfg) == "anthropic"


# ---------------------------------------------------------------------------
# reasoning-model-safe OpenAI params
# ---------------------------------------------------------------------------

class TestReasoningParams:
    @pytest.mark.parametrize("model", [
        "o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o3-pro",
        "o4-mini", "gpt-5", "gpt-5-mini", "gpt-5-nano",
        "openai/gpt-5-mini", "openai/o3",
    ])
    def test_reasoning_models_detected(self, model):
        assert _is_openai_reasoning_model(model)

    @pytest.mark.parametrize("model", [
        "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
        "qwen2.5-coder:14b", "llama3.1:70b", "gemini-3.8-flash",
        "anthropic/claude-haiku-5.5",
    ])
    def test_non_reasoning_models_detected(self, model):
        assert not _is_openai_reasoning_model(model)

    def test_reasoning_body_uses_max_completion_tokens_and_no_temperature(
            self, monkeypatch):
        cfg = make_config(model="gpt-5-mini", base_url="https://api.example/v1")
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("x", config=cfg)
        body = m.call_args.kwargs["json"]
        assert body["max_completion_tokens"] == cfg.llm.max_tokens
        assert "temperature" not in body
        assert "max_tokens" not in body

    def test_normal_body_keeps_temperature_and_max_tokens(self, monkeypatch):
        cfg = make_config(model="gpt-4o-mini",
                          base_url="https://api.example/v1")
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("x", config=cfg)
        body = m.call_args.kwargs["json"]
        assert body["max_tokens"] == cfg.llm.max_tokens
        assert body["temperature"] == cfg.llm.temperature
        assert "max_completion_tokens" not in body

    def test_config_flag_forces_reasoning_shape(self, monkeypatch):
        cfg = make_config(model="gpt-4o-mini", openai_reasoning=True,
                          base_url="https://api.example/v1")
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("x", config=cfg)
        assert "max_completion_tokens" in m.call_args.kwargs["json"]

    def test_config_flag_false_disables_auto_detection(self, monkeypatch):
        cfg = make_config(model="gpt-5-mini", openai_reasoning=False,
                          base_url="https://api.example/v1")
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("x", config=cfg)
        body = m.call_args.kwargs["json"]
        assert "max_tokens" in body and "temperature" in body

    def test_anthropic_requests_are_never_reshaped(self, monkeypatch):
        cfg = make_config(model="claude-haiku-4-5")   # reasoning rule is openai-only
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=ANTHROPIC_PAYLOAD)) as m:
            call_llm("x", config=cfg)
        body = m.call_args.kwargs["json"]
        assert body["temperature"] == cfg.llm.temperature
        assert body["max_tokens"] == cfg.llm.max_tokens


# ---------------------------------------------------------------------------
# per-task models
# ---------------------------------------------------------------------------

class TestPerTaskModels:
    def test_fallback_to_llm_model(self):
        cfg = make_config(model="main-model")
        assert model_for(cfg) == "main-model"
        for task in ("extraction", "classification", "mode_detection"):
            assert model_for(cfg, task) == "main-model"

    def test_overrides_apply_per_task(self):
        cfg = make_config(model="main-model", models={
            "extraction": "strong-model",
            "classification": "cheap-model",
            "mode_detection": None,
        })
        assert model_for(cfg, "extraction") == "strong-model"
        assert model_for(cfg, "classification") == "cheap-model"
        assert model_for(cfg, "mode_detection") == "main-model"
        assert model_for(cfg) == "main-model"

    def test_call_llm_model_kwarg_reaches_the_body(self, monkeypatch):
        cfg = make_config(model="main-model",
                          base_url="https://api.example/v1")
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(payload=OPENAI_PAYLOAD)) as m:
            call_llm("x", config=cfg, model="other-model")
        assert m.call_args.kwargs["json"]["model"] == "other-model"

    def test_extractor_passes_extraction_model(self, monkeypatch):
        from ec import extractor
        cfg = make_config(model="main-model", models={
            "extraction": "strong-extractor",
            "classification": None,
            "mode_detection": None,
        })
        monkeypatch.setenv(KEY_ENV, "k")
        with patch.object(extractor, "load_extractor_prompt",
                          return_value="prompt"):
            with patch("ec.extractor.call_llm",
                       return_value='{"ecus": []}') as m:
                extractor.extract_ecus(
                    {"prompt": "p"}, config=cfg)
        assert m.call_args.kwargs.get("model") == "strong-extractor"

    def test_classifier_passes_classification_model(self, monkeypatch):
        from ec import diffuser
        cfg = make_config(model="main-model", models={
            "extraction": None,
            "classification": "cheap-classifier",
            "mode_detection": None,
        })
        monkeypatch.setenv(KEY_ENV, "k")
        pairs = [({"id": "a", "cognition": "x",
                   "conclusion_type": "pattern",
                   "scope": {"level": "repo", "path": "r"},
                   "confidence": 0.5},
                  {"id": "b", "cognition": "y",
                   "conclusion_type": "pattern",
                   "scope": {"level": "repo", "path": "r"},
                   "confidence": 0.5})]
        with patch("ec.diffuser.call_llm",
                   return_value='{"classifications": []}') as m:
            diffuser._classify_pairs(pairs, diffuser.CLASSIFICATION_SYSTEM,
                                     diffuser._RELATIONSHIPS, cfg)
        assert m.call_args.kwargs.get("model") == "cheap-classifier"


# ---------------------------------------------------------------------------
# provider-aware missing-key handling
# ---------------------------------------------------------------------------

class TestKeyGate:
    def test_anthropic_format_always_requires_key(self):
        cfg = make_config(model="claude-haiku-4-5", api_key_env=KEY_ENV)
        assert key_missing(cfg) == KEY_ENV

    def test_registry_provider_with_key_required(self):
        cfg = make_config(model="gpt-4o-mini", provider="openai",
                          api_key_env=KEY_ENV)
        assert key_missing(cfg) == KEY_ENV

    def test_keyless_providers_pass(self):
        cfg = make_config(model="qwen2.5-coder:14b", provider="ollama",
                          api_key_env=KEY_ENV, base_url="http://x/v1")
        assert key_missing(cfg) is None

    def test_custom_provider_openai_format_has_no_check(self):
        cfg = make_config(model="gpt-4o-mini", provider="custom",
                          api_key_env=KEY_ENV)
        assert key_missing(cfg) is None

    def test_missing_key_message_names_provider_and_url(self):
        cfg = make_config(model="gpt-4o-mini", provider="openai",
                          api_key_env="MY_OPENAI_KEY")
        msg = missing_key_message(cfg)
        assert "MY_OPENAI_KEY" in msg
        assert "OpenAI" in msg
        assert "https://platform.openai.com/api-keys" in msg
        assert "opencode" not in msg.lower()

    def test_missing_key_message_for_unknown_provider_is_generic(self):
        cfg = make_config(model="whatever", provider="vllm",
                          api_key_env="MY_KEY")
        msg = missing_key_message(cfg)
        assert "MY_KEY" in msg
        assert "opencode.ai" not in msg

    def test_missing_key_raises_before_any_http(self, monkeypatch):
        cfg = make_config(model="gpt-4o-mini", provider="openai",
                          api_key_env=KEY_ENV)
        monkeypatch.delenv(KEY_ENV, raising=False)
        with patch("ec.llm.requests.post") as post:
            with pytest.raises(LLMError) as exc:
                call_llm("x", config=cfg)
        post.assert_not_called()
        assert KEY_ENV in str(exc.value)
        assert "opencode" not in str(exc.value).lower()


# ---------------------------------------------------------------------------
# retry with backoff
# ---------------------------------------------------------------------------

class TestRetry:
    def _cfg(self, **overrides):
        cfg = make_config(model="gpt-4o-mini",
                          base_url="https://api.example/v1")
        cfg.llm.provider = "custom"
        for k, v in overrides.items():
            cfg.llm[k] = v
        return cfg

    def test_429_then_success(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "k")
        cfg = self._cfg()
        responses = [FakeResponse(429, {}, {"Retry-After": "2"}),
                     FakeResponse(payload=OPENAI_PAYLOAD)]
        with patch("ec.llm.requests.post", side_effect=responses) as m:
            assert call_llm("x", config=cfg) == "hi"
        assert m.call_count == 2

    def test_500_then_success(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   side_effect=[FakeResponse(503, {}),
                                FakeResponse(payload=OPENAI_PAYLOAD)]):
            assert call_llm("x", config=self._cfg()) == "hi"

    def test_retries_exhausted_raises(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(500, {})) as m:
            with pytest.raises(LLMError, match="HTTP 500"):
                call_llm("x", config=self._cfg())
        assert m.call_count == 3          # default retry_attempts

    def test_timeout_then_success(self, monkeypatch):
        import requests as _requests
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   side_effect=[_requests.Timeout("read timed out"),
                                FakeResponse(payload=OPENAI_PAYLOAD)]):
            assert call_llm("x", config=self._cfg()) == "hi"

    def test_no_retry_on_4xx(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(401, {})) as m:
            with pytest.raises(LLMError, match="HTTP 401"):
                call_llm("x", config=self._cfg())
        assert m.call_count == 1

    def test_attempts_configurable(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "k")
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(429, {})) as m:
            with pytest.raises(LLMError):
                call_llm("x", config=self._cfg(retry_attempts=1))
        assert m.call_count == 1

    def test_backoff_is_exponential_and_capped(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "k")
        cfg = self._cfg(retry_backoff=1.0, retry_backoff_cap=4.0)
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(429, {})) as m:
            with pytest.raises(LLMError):
                call_llm("x", config=cfg)
        assert m.call_count == 3
        assert _backoff_delay(cfg, 1) == 1.0      # base * 2^0
        assert _backoff_delay(cfg, 2) == 2.0      # base * 2^1
        assert _backoff_delay(cfg, 3) == 4.0      # capped
        assert _backoff_delay(cfg, 9) == 4.0      # still capped

    def test_retry_after_honoured_and_capped(self, monkeypatch):
        cfg = self._cfg(retry_backoff=0.1, retry_backoff_cap=8.0)
        assert _backoff_delay(cfg, 1, "30") == 8.0     # lifted to cap
        assert _backoff_delay(cfg, 1, "0.25") == 0.25  # larger than base
        assert _backoff_delay(cfg, 1, "not-a-date") == 0.1  # ignored

    def test_sleep_is_injected_not_real(self, monkeypatch):
        """The three exhausted retries produce exactly two sleeps, no real time."""
        monkeypatch.setenv(KEY_ENV, "k")
        sleeps: list[float] = []
        monkeypatch.setattr("ec.llm._sleep", sleeps.append)
        with patch("ec.llm.requests.post",
                   return_value=FakeResponse(429, {})):
            with pytest.raises(LLMError):
                call_llm("x", config=self._cfg())
        assert len(sleeps) == 2
        assert all(s >= 0 for s in sleeps)
