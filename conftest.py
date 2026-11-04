# Ensures the repo root is on sys.path so `import reverie` works under pytest.


# Modules that test the legacy conclusion/candidate/review flow (capture.mode
# "legacy"); the typed items pipeline has its own tests (tests/test_spine_*).
_LEGACY_CAPTURE_MODULES = {
    "test_e2e_integration", "test_flow_candidate_quality", "test_integration_layers",
    "test_pending_queue", "test_phase13_commit_hash", "test_phase13_polish",
    "test_phase4_mcp_server",
}

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _legacy_capture_for_legacy_modules(request, monkeypatch):
    if request.module.__name__.split(".")[-1] in _LEGACY_CAPTURE_MODULES:
        monkeypatch.setenv("REVERIE_CAPTURE_MODE", "legacy")


@pytest.fixture(autouse=True)
def _no_user_claude_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_background_workers(monkeypatch):
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
