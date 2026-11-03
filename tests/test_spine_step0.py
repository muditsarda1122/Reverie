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
