"""Provider registry tests (feat/providers).

Offline: pure data + pure helpers. The wire-level request-shape tests live
in test_llm_request_shapes.py (also offline — requests.post is mocked).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from ec import providers
from ec.providers import MENU_KEYS, REGISTRY, Provider, llm_block


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
        }

    def test_zen_block_keeps_model_name_routing(self):
        block = llm_block("opencode_zen")
        assert block["wire_format"] is None or "wire_format" not in block

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
