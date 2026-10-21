"""Smoke-group-5 tests — one review queue, one order (items 19, 22).

- 19: `review_list` (the MCP tool) and `reverie review --list` (the CLI)
      show the same candidates in the same order — both go through
      review_queue.candidates(); this pins it with real outputs.
- 22: candidates sharing a conclusion type AND grounding signature are
      near-duplicates; the review_list message annotates them (grouping
      them for a single decision) — storage and the TUI are untouched.

Run: .venv/bin/python -m pytest tests/test_flow_review_consistency.py -v
Offline: fake embedding model, no LLM, temp REVERIE_HOME only.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import re
from datetime import datetime, timezone

import numpy as np
import pytest

from reverie.brain import Brain
from reverie.mcp_server import ReverieServer
from reverie.review_cli import (
    candidate_detail,
    render_screen,
)
from reverie.review_cli import (
    main as cli_main,
)
from reverie.review_queue import CANDIDATE_RULE, OPTIONS_LINE
from reverie.session import SessionManager

T0 = datetime.now(timezone.utc).replace(microsecond=0)
REPO_A, REPO_B = "/repos/alpha", "/repos/beta"


class FakeModel:
    """Deterministic 8-dim embeddings — no real model load."""

    def encode_one(self, text):
        return np.zeros(8, dtype=np.float32)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path))
    monkeypatch.setenv("REVERIE_REPO_PATH", REPO_A)
    monkeypatch.setenv("REVERIE_BRANCH", "main")
    return tmp_path


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result


def make_candidate(brain, session_id, cognition, confidence=0.5,
                   files=("auth/token.py",), conclusion_type="invariant",
                   metadata=None):
    ecu = {
        "cognition": cognition,
        "conclusion_type": conclusion_type,
        "scope": {"level": "repo", "path": "repo:x > auth"},
        "provenance": {"source_type": "debugging",
                       "created_at": T0.isoformat()},
        "grounding": {"files": list(files)},
        "evidence_pointers": ["reasoning step 1"],
        "confidence": confidence,
        "metadata": metadata or {},
    }
    return brain.insert_session_ecu(session_id, ecu)


def _block_containing(text: str, needle: str) -> str:
    lines = text.splitlines()
    hit = next(i for i, line in enumerate(lines) if needle in line)
    start = hit
    while start > 0 and lines[start] != CANDIDATE_RULE:
        start -= 1
    end = hit
    while end < len(lines) and lines[end] != CANDIDATE_RULE:
        end += 1
    return "\n".join(lines[start:end + 1])


def _enabled_server(brain):
    server = ReverieServer(brain=brain, manager=SessionManager(brain),
                           embedding_model=FakeModel())
    payload, _ = call_tool(server, "set_repo_state", {"state": "enabled"})
    assert payload["status"] == "ok"
    return server


def test_review_list_order_matches_cli_listing(home, capsys):
    """Item 19: the tool page and the CLI listing share one order."""
    brain = Brain()
    server = _enabled_server(brain)
    sid_a = brain.create_session(REPO_A, "main")
    sid_b = brain.create_session(REPO_B, "trunk")
    weakest = make_candidate(brain, sid_b, "weakest", confidence=0.4)
    strongest = make_candidate(brain, sid_a, "strongest", confidence=0.9)
    middle = make_candidate(brain, sid_a, "middle", confidence=0.6)

    listing, _ = call_tool(server, "review_list", {"all": True})
    tool_order = [c["id"] for c in listing["candidates"]]
    brain.close()

    rc = cli_main(["--list", "--all", "--resolve-open-questions", "skip"])
    assert rc == 0
    cli_order = re.findall(r"id: ([0-9a-f-]{36})", capsys.readouterr().out)
    assert tool_order == [strongest, middle, weakest]
    assert cli_order == tool_order


def test_review_list_annotates_near_duplicates(home):
    """Item 22: same type + grounding → a 'duplicates' line in the listing."""
    brain = Brain()
    server = _enabled_server(brain)
    sid = brain.create_session(REPO_A, "main")
    first = make_candidate(brain, sid, "token must refresh optimistically",
                           confidence=0.9)
    twin = make_candidate(brain, sid, "refresh must be optimistic",
                          confidence=0.8)
    unrelated_type = make_candidate(brain, sid, "a different kind of claim",
                                    confidence=0.7,
                                    conclusion_type="constraint")
    unrelated_files = make_candidate(brain, sid, "pool sizing invariant",
                                     confidence=0.6, files=("db/pool.py",))
    no_refs = make_candidate(brain, sid, "no refs at all", confidence=0.5,
                             files=())

    listing, _ = call_tool(server, "review_list", {})
    msg = listing["message"]
    brain.close()

    assert f"duplicates (same type + grounding): {twin[:8]}" in msg
    assert f"duplicates (same type + grounding): {first[:8]}" in msg
    for lonely in (unrelated_type, unrelated_files, no_refs):
        assert f"duplicates (same type + grounding): {lonely[:8]}" not in msg
    dup_lines = [ln for ln in msg.splitlines() if "duplicates (" in ln]
    assert len(dup_lines) == 2          # one per member of the pair only


# ---------------------------------------------------------------------------
# smoke group — MCP and terminal render the SAME candidate block
# ---------------------------------------------------------------------------

def test_mcp_and_terminal_candidate_blocks_match(home):
    brain = Brain()
    server = _enabled_server(brain)
    sid = brain.create_session(REPO_A, "main")
    cid = make_candidate(
        brain, sid, "Token refresh must be optimistic to avoid double charge",
        confidence=0.72, metadata={"evidence_status": "supported"},
    )

    listing, _ = call_tool(server, "review_list", {})
    mcp_block = _block_containing(listing["message"], f"ID: {cid}")
    candidate = brain.get_queue_candidate(cid)
    terminal = render_screen(
        candidate, candidate_detail(brain, candidate),
        index=1, total=1, progress={},
    )
    terminal_block = _block_containing(terminal, f"ID: {cid}")
    brain.close()

    assert mcp_block == terminal_block
    for fragment in (
        "Token refresh must be optimistic to avoid double charge",
        "evidence: SUPPORTED",
        "Grounded: auth/token.py",
        "Repo: /repos/alpha",
        OPTIONS_LINE,
    ):
        assert fragment in mcp_block
        assert fragment in terminal_block


def test_every_candidate_block_carries_options(home):
    brain = Brain()
    server = _enabled_server(brain)
    sid = brain.create_session(REPO_A, "main")
    ids = [
        make_candidate(brain, sid, f"conclusion number {n}",
                       confidence=0.5 + n / 10)
        for n in range(3)
    ]
    listing, _ = call_tool(server, "review_list", {})
    brain.close()

    blocks = [
        part for part in listing["message"].split(CANDIDATE_RULE)
        if "Candidate " in part
    ]
    assert len(blocks) == len(ids)
    for block in blocks:
        assert "Options:" in block
        assert OPTIONS_LINE in block
