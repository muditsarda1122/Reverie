"""Step 0 of the continuity spine — fixes found in the usefulness lab.

Offline. Each section is one lab finding (see the commit that introduced it).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from unittest.mock import patch

import pytest

from reverie.brain import Brain
from reverie.llm import LLMError
from reverie.mcp_server import ReverieServer
from reverie.session import SessionManager

REPO, BRANCH = "/repos/spine-repo", "main"


@pytest.fixture(autouse=True)
def _repo_env(monkeypatch):
    monkeypatch.setenv("REVERIE_REPO_PATH", REPO)
    monkeypatch.setenv("REVERIE_BRANCH", BRANCH)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "reverie.db")
    yield b
    b.close()


@pytest.fixture()
def server(brain):
    return ReverieServer(brain=brain, manager=SessionManager(brain))


def call_tool(server, name, arguments=None):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result


# ---------------------------------------------------------------------------
# 0a. observe wording — the API reasoning-extraction safeguard
# ---------------------------------------------------------------------------

def _tools(server):
    r = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return r["result"]["tools"]


def test_no_tool_text_asks_for_a_reasoning_trace(server):
    """Sonnet/Opus 5.5 reject sessions whose tool text asks the model to emit
    its chain of thought (`reasoning_extraction`). No tool name, description or
    parameter may ask for a reasoning trace."""
    blob = json.dumps(_tools(server)).lower()
    assert "reasoning trace" not in blob
    assert "reasoning_trace" not in blob
    assert "your reasoning" not in blob
    instructions = server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "t"}}})["result"]["instructions"]
    assert "reasoning trace" not in instructions.lower()


def test_installed_agent_instructions_do_not_ask_for_reasoning():
    from reverie.install import AGENTS_MD
    assert "your reasoning" not in AGENTS_MD.lower().replace(
        "your reasoning about", "")  # reconsolidate evidence phrase is allowed
    assert "reasoning trace" not in AGENTS_MD.lower()


def test_observe_accepts_work_summary_and_legacy_alias(server):
    seen = []

    def fake_extract(row, interaction, **kw):
        seen.append(interaction["reasoning_trace"])
        return {"status": "ok", "ecus_extracted": 0}

    server._extract_interaction = fake_extract
    call_tool(server, "set_repo_state", {"state": "enabled"})
    p, _ = call_tool(server, "observe",
                     {"user_prompt": "p", "work_summary": "new name"})
    assert p["status"] == "ok"
    p, _ = call_tool(server, "observe",
                     {"user_prompt": "p", "reasoning_trace": "old name"})
    assert p["status"] == "ok"
    assert seen == ["new name", "old name"]
    p, _ = call_tool(server, "observe", {"user_prompt": "p"})
    assert p["status"] == "error" and "work_summary" in p["error"]


# ---------------------------------------------------------------------------
# 0b. capture: only real conversation turns are exchanges
# ---------------------------------------------------------------------------

def test_read_exchange_ignores_non_turn_records(tmp_path):
    from reverie.integrations.claude.capture import read_exchange
    log = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "fix the cache"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "done"}]}},
        # written by newer Claude Code after ToolSearch; looks like a user turn
        {"type": "api-request-blob",
         "message": {"role": "user", "content": [{"type": "text", "text": "Tool loaded."}]}},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    ex = read_exchange(log)
    assert ex.user_request == "fix the cache"
    assert ex.assistant_reply == "done"


# ---------------------------------------------------------------------------
# 0c. the stuck pending queue
# ---------------------------------------------------------------------------

def _enabled_server(brain):
    from reverie import repos
    s = ReverieServer(brain=brain, manager=SessionManager(brain))
    repos.set_repo_state(brain, repos.resolve_repo(), "enabled")
    return s


def test_prose_reply_is_terminal_not_queued(brain):
    """A model answer that is not JSON must not sit in the retry queue."""
    s = _enabled_server(brain)
    row = s._enabled_session(__import__("reverie.repos", fromlist=["x"]).resolve_repo())
    with patch("reverie.extractor.call_llm",
               return_value="I can't produce the extraction you asked for."):
        out = s._extract_interaction(
            row, {"prompt": "p", "output": "o"}, prequeue=True)
    assert out["status"] == "skipped" and out["unextractable"] is True
    assert brain.count_pending_extractions() == 0


def test_provider_failure_still_queues_for_retry(brain):
    s = _enabled_server(brain)
    row = s._enabled_session(__import__("reverie.repos", fromlist=["x"]).resolve_repo())
    with patch("reverie.extractor.call_llm", side_effect=LLMError("503")):
        out = s._extract_interaction(
            row, {"prompt": "p", "output": "o"}, prequeue=True)
    assert out["status"] == "error"
    assert brain.count_pending_extractions() == 1


def test_retry_cap_drops_exhausted_items(brain):
    from reverie import pending, repos
    s = _enabled_server(brain)
    row = s._enabled_session(repos.resolve_repo())
    item = pending.save_pending_extraction(
        brain, row, {"prompt": "p", "output": "o"}, "boom")
    for _ in range(5):
        brain.mark_pending_extraction_attempt(item, "boom")
    with patch("reverie.extractor.call_llm", side_effect=LLMError("503")) as llm:
        summary = pending.retry_pending(brain, config=s.config)
    assert summary["dropped"] == 1 and llm.call_count == 0
    assert brain.count_pending_extractions() == 0


def test_retry_drops_unparseable_replies(brain):
    from reverie import pending, repos
    s = _enabled_server(brain)
    row = s._enabled_session(repos.resolve_repo())
    pending.save_pending_extraction(brain, row, {"prompt": "p", "output": "o"}, "x")
    with patch("reverie.extractor.call_llm", return_value="Sorry, no."):
        summary = pending.retry_pending(brain, config=s.config)
    assert summary["dropped"] == 1 and brain.count_pending_extractions() == 0


@pytest.mark.parametrize("user,reply", [
    ("/reverie-review", "Here are your candidates"),
    ("show me my candidates", "Calling review_list ... token abc"),
    ("<command-name>/reverie-review</command-name>", "ok"),
])
def test_reverie_housekeeping_is_not_captured(brain, user, reply):
    from reverie import core_api
    s = _enabled_server(brain)
    with patch("reverie.extractor.call_llm") as llm:
        out = core_api.capture_turn(REPO, BRANCH, user, reply, _server=s)
    assert out["status"] == "skipped" and llm.call_count == 0
    assert brain.count_pending_extractions() == 0
