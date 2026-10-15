"""Provider registry tests (feat/providers).

Offline: pure data + pure helpers. The wire-level request-shape tests live
in test_llm_request_shapes.py (also offline — requests.post is mocked).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from reverie import providers
from reverie.config import load_config
from reverie.providers import MENU_KEYS, REGISTRY, Provider, llm_block


class TestRegistryShape:
    def test_all_brief_providers_exist(self):
        assert set(REGISTRY) == {
            "opencode_zen", "anthropic", "openai", "google",
            "openrouter", "ollama", "custom",
        }

    @pytest.mark.parametrize("key", list(REGISTRY))
    def test_entries_are_well_formed(self, key):
        entry = REGISTRY[key]
        assert isinstance(entry, Provider)
        assert entry.key == key
        assert entry.name
        if key not in ("custom", "opencode_zen"):
            # single-family providers pin their wire format
            assert entry.wire_format in ("anthropic", "openai")
        if entry.key_required:
            assert entry.api_key_env and entry.key_url
        if entry.default_model:
            assert entry.base_url

    def test_wire_format_values_are_valid(self):
        for entry in REGISTRY.values():
            assert entry.wire_format in (None, "anthropic", "openai")

    def test_menu_excludes_custom(self):
        assert "custom" not in MENU_KEYS
        assert set(MENU_KEYS) <= set(REGISTRY)


class TestKeyDetection:
    def test_detect_available_in_registry_order(self):
        env = {"ANTHROPIC_API_KEY": "a", "OPENCODE_ZEN_API_KEY": "z",
               "OPENROUTER_API_KEY": "o"}
        assert providers.detect_available(env) == [
            "opencode_zen", "anthropic", "openrouter",
        ]

    def test_google_aliases(self):
        assert providers.key_env_for(
            REGISTRY["google"], {"GOOGLE_API_KEY": "g"}) == "GOOGLE_API_KEY"
        assert providers.key_env_for(
            REGISTRY["google"], {"GEMINI_API_KEY": "g"}) == "GEMINI_API_KEY"
        assert providers.key_env_for(REGISTRY["google"], {}) is None

    def test_keyless_providers_never_detected(self):
        assert providers.detect_available({}) == []

    def test_detect_uses_real_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "x")
        assert "openrouter" in providers.detect_available()


class TestLlmBlock:
    def test_anthropic_block(self):
        assert llm_block("anthropic") == {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-haiku-5-5",
            "api_key_env": "ANTHROPIC_API_KEY",
            "wire_format": "anthropic",
        }

    def test_google_block(self):
        block = llm_block("google")
        assert block["provider"] == "google"
        assert block["api_key_env"] == "GEMINI_API_KEY"   # alias never written
        assert block["wire_format"] == "openai"

    def test_ollama_block_is_keyless(self):
        block = llm_block("ollama")
        assert block == {
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen2.5-coder:14b",
            "wire_format": "openai",
        }

    def test_zen_block_keeps_model_name_routing(self):
        block = llm_block("opencode_zen")
        assert "wire_format" not in block

    def test_custom_block_writes_nothing_but_the_name(self):
        assert llm_block("custom") == {"provider": "custom"}


class TestHelpers:
    def test_get_unknown(self):
        assert providers.get("nope") is None
        assert providers.get(None) is None
        assert providers.get("anthropic") is REGISTRY["anthropic"]

    def test_provider_names_includes_all(self):
        names = providers.provider_names()
        assert "custom" in names and "ollama" in names


class TestConfigResolution:
    """`provider` fills unset llm fields from the registry; explicit wins."""

    def _load(self, tmp_path, user_yaml: str):
        p = tmp_path / "config.yaml"
        p.write_text(user_yaml)
        return load_config(p)

    def test_provider_only_fills_registry_fields(self, tmp_path):
        cfg = self._load(tmp_path, "llm:\n  provider: anthropic\n")
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.base_url == "https://api.anthropic.com/v1"
        assert cfg.llm.model == "claude-haiku-5-5"
        assert cfg.llm.api_key_env == "ANTHROPIC_API_KEY"
        assert cfg.llm.wire_format == "anthropic"

    def test_explicit_fields_win(self, tmp_path):
        cfg = self._load(
            tmp_path,
            "llm:\n  provider: anthropic\n  model: claude-3-5-haiku\n"
            "  api_key_env: MY_ANTHROPIC_KEY\n")
        assert cfg.llm.model == "claude-3-5-haiku"
        assert cfg.llm.api_key_env == "MY_ANTHROPIC_KEY"
        assert cfg.llm.base_url == "https://api.anthropic.com/v1"  # filled

    def test_no_provider_key_uses_defaults(self, tmp_path):
        cfg = self._load(tmp_path, "llm:\n  model: gpt-4o-mini\n")
        assert cfg.llm.provider == "opencode_zen"       # default preserved
        assert cfg.llm.base_url == "https://opencode.ai/zen/v1"
        assert cfg.llm.api_key_env == "OPENCODE_ZEN_API_KEY"

    def test_default_config_is_unchanged_without_user_file(self, tmp_path):
        cfg = load_config(tmp_path / "missing.yaml")
        assert cfg.llm.model == "claude-haiku-4-5"
        assert cfg.llm.provider == "opencode_zen"
        assert cfg.llm.wire_format is None

    def test_custom_changes_nothing(self, tmp_path):
        cfg = self._load(tmp_path, "llm:\n  provider: custom\n")
        assert cfg.llm.provider == "custom"
        # fields stay as written in the defaults (today's behaviour)
        assert cfg.llm.model == "claude-haiku-4-5"
        assert cfg.llm.base_url == "https://opencode.ai/zen/v1"

    def test_unknown_provider_name_is_left_alone(self, tmp_path):
        cfg = self._load(tmp_path, "llm:\n  provider: vllm\n")
        assert cfg.llm.provider == "vllm"
        assert cfg.llm.model == "claude-haiku-4-5"      # merged defaults

    def test_ollama_provider_clears_key_env(self, tmp_path):
        cfg = self._load(tmp_path, "llm:\n  provider: ollama\n")
        assert cfg.llm.provider == "ollama"
        assert cfg.llm.base_url == "http://localhost:11434/v1"
        assert cfg.llm.model == "qwen2.5-coder:14b"
        assert cfg.llm.api_key_env is None
        assert cfg.llm.wire_format == "openai"

    def test_ollama_key_env_set_explicitly_is_kept(self, tmp_path):
        cfg = self._load(
            tmp_path, 'llm:\n  provider: ollama\n  api_key_env: "OLLAMA_KEY"\n')
        assert cfg.llm.api_key_env == "OLLAMA_KEY"

    def test_zen_provider_only_matches_defaults(self, tmp_path):
        cfg = self._load(tmp_path, "llm:\n  provider: opencode_zen\n")
        assert cfg.llm.model == "claude-haiku-4-5"
        assert cfg.llm.base_url == "https://opencode.ai/zen/v1"
        assert cfg.llm.api_key_env == "OPENCODE_ZEN_API_KEY"
        assert cfg.llm.wire_format is None              # multi-family: name rule

    def test_other_sections_unaffected(self, tmp_path):
        cfg = self._load(
            tmp_path, "ranking:\n  w_relevance: 0.99\nllm:\n  provider: openai\n")
        assert cfg.ranking.w_relevance == 0.99
        assert cfg.llm.provider == "openai"

    def test_new_llm_defaults_present(self, tmp_path):
        cfg = load_config(tmp_path / "missing.yaml")
        assert cfg.llm.retry_attempts == 3
        assert cfg.llm.retry_backoff == 0.5
        assert cfg.llm.retry_backoff_cap == 8.0
        assert cfg.llm.openai_reasoning is None
        assert cfg.llm.models.extraction is None
        assert cfg.llm.models.classification is None
        assert cfg.llm.models.mode_detection is None
