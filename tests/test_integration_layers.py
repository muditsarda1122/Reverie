"""feat/integration-layers — core has no harness knowledge; integrations do.

Covers the four guarantees:

1. every ``reverie/*.py`` outside ``reverie/integrations/`` is harness-free
   and the core API exposes the same operations as the MCP tools;
2. SessionStart renders the canonical ``core_api.summary`` line, not a
   parallel formatter, and emits no conclusion text;
3. Claude's Stop hook captures one completed exchange, never blocks the
   agent (errors return 0), and keeps failed work in the pending queue;
4. every integration is installable/uninstallable in a sandboxed home.

All offline: LLM calls are mocked and embeddings use a deterministic fake.
"""

from __future__ import annotations

import ast
import json
import signal
import time
from pathlib import Path

import numpy as np
import pytest

import reverie
from reverie import cli, core_api, hooks, install, uninstall
from reverie.brain import Brain
from reverie.integrations import AGENTS, get_integration
from reverie.integrations.claude.capture import main as capture_main
from reverie.integrations.claude.capture import read_exchange
from reverie.mcp_server import ReverieServer
from reverie.session import SessionManager

ROOT = Path(reverie.__file__).parent
FIXTURE = Path(__file__).parent / "fixtures" / "claude_transcript.jsonl"
HARNESSES = ("claude", "opencode", "codex", "cursor", "gemini")
HOOK_EVENTS = ("SessionStart", "Stop", "UserPromptSubmit", "PreToolUse",
               "PostToolUse", "SessionEnd", "SubagentStop")
HARNESS_PATHS = ("~/.claude", ".claude/", "~/.cursor", ".cursor/mcp.json",
                 ".cursor/commands", ".config/opencode", "~/.codex",
                 ".codex/config.toml", ".codex/prompts", "~/.gemini",
                 ".gemini/settings.json", ".gemini/commands", "CLAUDE.md",
                 "GEMINI.md", "opencode.json")


class FakeModel:
    def encode_one(self, text):
        return np.zeros(8, dtype=np.float32)


def _payload(cognition: str = "refresh compares absolute expiries",
             files=("auth.py",)) -> str:
    return json.dumps({"ecus": [{
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:test"},
        "provenance": {"source_type": "debugging", "source_id": "s1"},
        "grounding": {"files": list(files), "symbols": ["refresh"]},
        "confidence": 0.6,
        "status": "active",
        "evidence_pointers": [],
    }]})


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("REVERIE_HOME", str(h / ".reverie"))
    return h


@pytest.fixture()
def reverie_home(home):
    return home / ".reverie"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "auth.py").write_text("def refresh(): ...\n")
    monkeypatch.setenv("REVERIE_REPO_PATH", str(r.resolve()))
    monkeypatch.setenv("REVERIE_BRANCH", "main")
    return r.resolve()


@pytest.fixture()
def brain(reverie_home):
    b = Brain(reverie_home / "reverie.db")
    yield b
    b.close()


@pytest.fixture()
def server(brain):
    return ReverieServer(brain=brain, manager=SessionManager(brain),
                         embedding_model=FakeModel())


@pytest.fixture(autouse=True)
def _reset_default_server():
    core_api._DEFAULT_SERVER = None
    yield
    core_api._DEFAULT_SERVER = None


@pytest.fixture()
def offline_extraction(monkeypatch):
    monkeypatch.setattr("reverie.embeddings.get_embedding_model",
                        lambda: FakeModel())
    monkeypatch.setattr("reverie.extractor.call_llm",
                        lambda *a, **k: _payload())
    monkeypatch.setattr("reverie.diffuser.diffuse_session_ecu",
                        lambda *a, **k: None)


def _enable(brain: Brain, repo: Path, state: str = "enabled") -> None:
    brain.upsert_repo(str(repo), path=str(repo), state=state)


# ---------------------------------------------------------------------------
# 1. core has no harness knowledge; core API matches the MCP tools
# ---------------------------------------------------------------------------

def _core_files() -> list[Path]:
    return sorted(ROOT.glob("*.py"))


def test_core_does_not_import_harness_packages():
    offenders = []
    for path in _core_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in HARNESSES or \
                            alias.name.startswith("reverie.integrations"):
                        offenders.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] in HARNESSES:
                    offenders.append((path.name, module))
                parts = module.split(".")
                if (len(parts) >= 2 and parts[0] == "integrations"
                        and parts[1] in HARNESSES) or \
                        (len(parts) >= 3 and parts[:2] ==
                         ["reverie", "integrations"]
                         and parts[2] in HARNESSES):
                    offenders.append((path.name, module))
    assert offenders == []


def test_core_has_no_harness_config_paths_or_hook_events():
    offenders = []
    for path in _core_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                for needle in (*HOOK_EVENTS, *HARNESS_PATHS):
                    if needle in node.value:
                        offenders.append((path.name, needle))
    assert offenders == []


def test_core_summary_matches_mcp_tool(server, brain, repo):
    _enable(brain, repo)
    from_mcp = server._tool_summary({"repo_path": str(repo)})
    from_api = core_api.summary(str(repo), _server=server)
    assert from_api == from_mcp


def test_read_only_summary_creates_no_session(server, brain, repo):
    _enable(brain, repo)
    payload = core_api.summary(str(repo), branch="main", read_only=True,
                               _server=server)
    assert payload["status"] == "ok"
    assert brain.get_active_session(str(repo), "main") is None
    assert brain.list_sessions(repo_path=str(repo)) == []


def test_core_api_wrappers_round_trip(server, brain, repo, monkeypatch):
    _enable(brain, repo)
    monkeypatch.setattr("reverie.extractor.call_llm",
                        lambda *a, **k: _payload())
    monkeypatch.setattr("reverie.diffuser.diffuse_session_ecu",
                        lambda *a, **k: None)
    observed = core_api.observe("prompt", "trace", repo_path=str(repo),
                                _server=server)
    assert observed["status"] == "ok"
    found = core_api.query("refresh", repo_path=str(repo), _server=server)
    assert found["status"] == "ok"
    listed = core_api.review_list(repo_path=str(repo), _server=server)
    assert listed["status"] == "ok"
    state = core_api.set_repo_state("enabled", repo_path=str(repo),
                                    _server=server)
    assert state["state"] == "enabled"


def test_every_integration_implements_the_contract(tmp_path):
    for integration in AGENTS:
        assert integration.key and integration.name
        assert integration.detect(tmp_path, which=lambda _b: None) is False
        marker = tmp_path / (integration.marker or "")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}" if marker.suffix == ".json" else "")
        assert integration.detect(tmp_path, which=lambda _b: None) is True
        plans = integration.install(tmp_path, None, True,
                                    reverie_home=tmp_path / ".reverie")
        assert plans
        removals = integration.uninstall(
            tmp_path, True, reverie_home=tmp_path / ".reverie")
        assert removals


@pytest.mark.parametrize("key", ["claude_code", "cursor", "opencode",
                                 "codex", "gemini_cli"])
def test_sandboxed_install_uninstall_round_trip(key, tmp_path):
    home = tmp_path / key
    home.mkdir()
    reverie_home = home / ".reverie"
    def quiet(_s=""):
        return None
    rc = install.run(
        home=home, reverie_home=reverie_home, only=[key], no_verify=True,
        which=lambda _b: None, input_fn=lambda _p="": "", print_fn=quiet,
        environ={}, check_fn=lambda *a, **k: (True, "stub"))
    assert rc == 0
    before = sorted(
        str(p.relative_to(home)) for p in home.rglob("*")
        if p.is_file() and not p.is_relative_to(reverie_home))
    assert before
    rc = uninstall.run(home=home, reverie_home=reverie_home, yes=True,
                       print_fn=quiet)
    assert rc == 0
    after = sorted(
        str(p.relative_to(home)) for p in home.rglob("*")
        if p.is_file() and not p.is_relative_to(reverie_home))
    assert after == []


# ---------------------------------------------------------------------------
# 2. SessionStart uses core_api.summary
# ---------------------------------------------------------------------------

def test_session_start_uses_canonical_summary(home, reverie_home, repo,
                                              monkeypatch):
    Brain(reverie_home / "reverie.db").close()
    monkeypatch.setattr(core_api, "summary",
                        lambda *a, **k: {"status": "ok",
                                         "message": "CANONICAL SUMMARY LINE"})
    out = []
    assert hooks.session_start(print_fn=out.append) == 0
    text = "\n".join(out)
    assert "CANONICAL SUMMARY LINE" in text
    assert "secret conclusion text" not in text


def test_session_start_omits_conclusion_text(home, reverie_home, repo):
    brain = Brain(reverie_home / "reverie.db")
    brain.upsert_repo(str(repo), path=str(repo), state="enabled")
    brain.insert_ecu({"cognition": "a hidden conclusion",
                      "conclusion_type": "invariant",
                      "scope": {"level": "repo", "path": f"repo:{repo}"}})
    brain.close()
    out = []
    hooks.session_start(print_fn=out.append)
    text = "\n".join(out)
    assert "1 conclusion" in text
    assert "a hidden conclusion" not in text
    assert "When to call each command" not in text


# ---------------------------------------------------------------------------
# 3. Stop hook: install, capture, never block
# ---------------------------------------------------------------------------

def test_stop_hook_is_installed_and_uninstalled(home, reverie_home):
    def quiet(_s=""):
        return None
    install.run(home=home, reverie_home=reverie_home,
                only=["claude_code"], no_verify=True,
                which=lambda _b: None, input_fn=lambda _p="": "",
                print_fn=quiet, environ={},
                check_fn=lambda *a, **k: (True, "stub"))
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == {"SessionStart", "Stop"}
    stop = settings["hooks"]["Stop"][0]["hooks"][0]
    assert stop["type"] == "command"
    assert stop["timeout"] == 20
    assert stop["async"] is True
    command = " ".join([stop.get("command", ""), *stop.get("args", [])])
    assert "capture-turn" in command

    uninstall.run(home=home, reverie_home=reverie_home, yes=True,
                  print_fn=quiet)
    assert not (home / ".claude" / "settings.json").exists()


def test_stop_hook_captures_one_exchange(home, reverie_home, repo,
                                         offline_extraction):
    brain = Brain(reverie_home / "reverie.db")
    _enable(brain, repo)
    brain.close()
    assert capture_main(["--transcript", str(FIXTURE), "--repo", str(repo),
                         "--branch", "main"]) == 0
    brain = Brain(reverie_home / "reverie.db")
    try:
        session = brain.get_active_session(str(repo), "main")
        assert session is not None
        assert brain.count_session_ecus(session["id"]) == 1
        assert brain.count_pending_extractions(str(repo), "main") == 0
    finally:
        brain.close()


def test_stop_hook_disabled_repo_captures_nothing(home, reverie_home, repo,
                                                  offline_extraction):
    brain = Brain(reverie_home / "reverie.db")
    _enable(brain, repo, "disabled")
    brain.close()
    assert capture_main(["--transcript", str(FIXTURE), "--repo", str(repo),
                         "--branch", "main"]) == 0
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert brain.list_sessions(repo_path=str(repo)) == []
        assert brain.count_pending_extractions() == 0
    finally:
        brain.close()


def test_stop_hook_broken_transcript_exits_zero_and_writes_nothing(
        home, repo, tmp_path):
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n{\"type\":\"user\"\n")
    assert capture_main(["--transcript", str(broken), "--repo", str(repo),
                         "--branch", "main"]) == 0
    assert not (home / ".reverie").exists()


def test_stop_hook_failed_extraction_goes_to_pending(home, reverie_home,
                                                     repo, monkeypatch):
    brain = Brain(reverie_home / "reverie.db")
    _enable(brain, repo)
    brain.close()
    from reverie.llm import LLMError
    monkeypatch.setattr("reverie.extractor.call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(
                            LLMError("no key")))
    result = core_api.capture_turn(
        str(repo), "main", "question", "answer", timeout=5)
    assert result["status"] == "error"
    assert result["captured"] is False
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert brain.count_pending_extractions(str(repo), "main") == 1
    finally:
        brain.close()


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="needs SIGALRM")
def test_stop_hook_timeout_leaves_pending(home, reverie_home, repo,
                                          monkeypatch):
    brain = Brain(reverie_home / "reverie.db")
    _enable(brain, repo)
    brain.close()

    def _slow(*args, **kwargs):
        time.sleep(2)

    monkeypatch.setattr("reverie.extractor.extract_and_store_session", _slow)
    result = core_api.capture_turn(
        str(repo), "main", "question", "answer", timeout=0.05)
    assert result["status"] == "pending"
    assert result["captured"] is False
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert brain.count_pending_extractions(str(repo), "main") == 1
    finally:
        brain.close()


def test_stop_hook_fixture_parsing():
    exchange = read_exchange(FIXTURE)
    assert exchange is not None
    assert "refresh token" in exchange.user_request
    assert "absolute expiries" in exchange.assistant_reply
    assert "def refresh" not in exchange.assistant_reply
    assert "trace" not in exchange.assistant_reply


def test_capture_turn_cli_routes_through_integrations(home, reverie_home,
                                                      repo, offline_extraction):
    brain = Brain(reverie_home / "reverie.db")
    _enable(brain, repo)
    brain.close()
    assert cli.main(["capture-turn", "--transcript", str(FIXTURE),
                     "--repo", str(repo), "--branch", "main"]) == 0
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert brain.count_session_ecus(
            brain.get_active_session(str(repo), "main")["id"]) == 1
    finally:
        brain.close()


def test_harness_hook_names_stay_with_integrations():
    assert get_integration("claude_code").name == "Claude Code"
    assert get_integration("gemini_cli").name == "Gemini CLI"
