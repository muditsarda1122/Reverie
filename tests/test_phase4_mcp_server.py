"""Phase 4 tests — MCP server (§28.3/§28.4, §16.3 shapes, D14/D15/D17/D22).

Run: .venv/bin/python -m pytest tests/ -v
Offline: in-process dispatch with mocked LLM calls (ec.extractor.call_llm /
ec.diffuser.call_llm), plus one real-subprocess stdio test. A live test skips
unless OPENCODE_ZEN_API_KEY is set.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ec.brain import Brain
from ec.llm import LLMError, api_key
from ec.mcp_server import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NO_SESSION_ERROR,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    ECServer,
)
from ec.session import SessionManager

ROOT = Path(__file__).resolve().parent.parent
REPO, BRANCH = "/repos/mcp-repo", "main"

EXTRACT_PAYLOAD = json.dumps({
    "ecus": [
        {
            "cognition": "Token refresh must be optimistic to avoid race conditions",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:mcp-repo > module:auth"},
            "source_type": "debugging",
            "grounding": {"files": ["auth/token.py"], "symbols": ["refresh"]},
            "evidence_pointer": "reasoning step 3",
        },
        {
            "cognition": "Token refresh must be optimistic so retries do not race",
            "conclusion_type": "decision",
            "scope": {"level": "module", "path": "repo:mcp-repo > module:auth"},
            "source_type": "debugging",
            "grounding": {"files": ["auth/cache.py"]},
            "evidence_pointer": "reasoning step 5",
        },
    ],
    "rejected_count": 1,
    "rejection_summary": "1 raw fact, not a conclusion",
})


@pytest.fixture(autouse=True)
def _repo_env(monkeypatch):
    monkeypatch.setenv("EC_REPO_PATH", REPO)
    monkeypatch.setenv("EC_BRANCH", BRANCH)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture()
def manager(brain):
    return SessionManager(brain)


@pytest.fixture()
def server(brain, manager):
    return ECServer(brain=brain, manager=manager)


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def active_session(manager):
    """An active session for the env-detected (repo, branch)."""
    return manager.start_session()["session_id"]


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return payload, result


def make_canonical(brain, model, cognition, **overrides):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:mcp-repo > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": 0.8,
        "status": "active",
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    return brain.insert_ecu(ecu, embedding=model.encode_one(cognition))


# ---------------------------------------------------------------------------
# protocol (D14)
# ---------------------------------------------------------------------------

def test_initialize_handshake(server):
    r = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "test"}},
    })
    result = r["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "ec"
    assert r["id"] == 1


def test_ping(server):
    r = server.handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert r["result"] == {}


def test_tools_list(server):
    r = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r["result"]["tools"]
    # four tools since Phase 10 (design doc §5.2: ec_reconsolidate)
    assert [t["name"] for t in tools] == [
        "ec_observe", "ec_query", "ec_get_summary", "ec_reconsolidate"]
    observe = tools[0]
    assert observe["inputSchema"]["required"] == ["user_prompt", "reasoning_trace"]
    assert "Do NOT call ec_observe" in observe["description"]  # §28.12 verbatim
    query = tools[1]
    assert query["inputSchema"]["required"] == ["query"]
    assert set(query["inputSchema"]["properties"]["mode"]["enum"]) == {
        "debugging", "architecture", "implementation", "investigation", "planning",
    }
    summary = tools[2]
    assert summary["inputSchema"]["properties"] == {}
    assert "Works without an active session" in summary["description"]
    recons = tools[3]
    assert recons["inputSchema"]["required"] == ["ecu_id", "evidence"]
    assert recons["inputSchema"]["properties"]["relationship"]["enum"] == [
        "supports", "contradicts", "supersedes"]


def test_notifications_get_no_response(server):
    assert server.handle_message({
        "jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle_message({
        "jsonrpc": "2.0", "method": "notifications/cancelled",
        "params": {"requestId": 1}}) is None


def test_protocol_errors(server):
    r = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "bogus/method"})
    assert r["error"]["code"] == METHOD_NOT_FOUND
    r = server.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "ec_delete_everything", "arguments": {}}})
    assert r["error"]["code"] == INVALID_PARAMS
    assert "ec_observe" in r["error"]["message"]
    r = server.handle_line("this is not json {{{")
    assert r["error"]["code"] == PARSE_ERROR


# ---------------------------------------------------------------------------
# error contracts (§16.3 / §28.4)
# ---------------------------------------------------------------------------

def test_observe_no_active_session(server):
    payload, result = call_tool(server, "ec_observe", {
        "user_prompt": "fix the bug", "reasoning_trace": "..."})
    assert payload == {
        "status": "error", "session_active": False, "error": NO_SESSION_ERROR,
    }
    assert result["isError"] is True


def test_query_no_active_session(server):
    payload, result = call_tool(server, "ec_query", {"query": "auth refresh"})
    assert payload["status"] == "error"
    assert payload["session_active"] is False
    assert payload["error"] == NO_SESSION_ERROR
    assert result["isError"] is True


def test_summary_without_session(server):
    payload, result = call_tool(server, "ec_get_summary")
    assert payload["status"] == "ok"
    assert result["isError"] is False
    assert payload["session"]["active"] is False
    assert payload["canonical_brain"]["total_ecus"] == 0
    assert set(payload["canonical_brain"]["by_scope"]) == {
        "engineering", "domain", "organization", "project",
        "repo", "module", "subsystem",
    }


def test_observe_missing_params(server, active_session):
    payload, _ = call_tool(server, "ec_observe", {"user_prompt": "x"})
    assert payload["status"] == "error"
    assert payload["session_active"] is True
    assert "reasoning_trace" in payload["error"]


def test_query_invalid_params(server, active_session):
    payload, _ = call_tool(server, "ec_query", {"query": "  "})
    assert payload["status"] == "error" and "query" in payload["error"]
    payload, _ = call_tool(server, "ec_query",
                           {"query": "auth", "scope": "galactic"})
    assert payload["status"] == "error"
    assert "valid scopes" in payload["error"].lower()
    payload, _ = call_tool(server, "ec_query",
                           {"query": "auth", "mode": "lounging"})
    assert payload["status"] == "error"
    assert "Valid modes" in payload["error"]


# ---------------------------------------------------------------------------
# ec_observe (§28.13) + ec_query (§11.3) with an active session
# ---------------------------------------------------------------------------

def test_observe_extracts_into_session_brain(server, brain, active_session, model):
    with patch("ec.extractor.call_llm", return_value=EXTRACT_PAYLOAD), \
         patch("ec.diffuser.call_llm",
               return_value=json.dumps({"classifications": []})):
        payload, result = call_tool(server, "ec_observe", {
            "user_prompt": "why do sessions drop?",
            "reasoning_trace": "I traced it to the token refresh race...",
            "final_output": "fixed by making refresh optimistic",
        })
    assert result["isError"] is False
    assert payload["status"] == "ok"
    assert payload["session_active"] is True
    assert payload["ecus_extracted"] == 2
    assert payload["ecus_rejected"] == 1
    assert payload["session_brain_count"] == 2
    assert "2 ECUs" in payload["message"]
    assert len(brain.list_session_ecus(active_session)) == 2


def test_observe_extraction_failure_guidance(server, active_session):
    with patch("ec.extractor.call_llm", side_effect=LLMError("no key")):
        payload, result = call_tool(server, "ec_observe", {
            "user_prompt": "x", "reasoning_trace": "y"})
    assert payload["status"] == "error"
    assert result["isError"] is True
    assert payload["error"].startswith("Extraction failed:")
    assert "no key" in payload["error"]
    assert "not every response produces ECUs" in payload["error"]


def test_observe_lightweight_diffusion_failure_is_soft(server, brain,
                                                       active_session, model):
    with patch("ec.extractor.call_llm", return_value=EXTRACT_PAYLOAD), \
         patch("ec.diffuser.call_llm", side_effect=LLMError("offline")):
        payload, _ = call_tool(server, "ec_observe", {
            "user_prompt": "p", "reasoning_trace": "r"})
    assert payload["status"] == "ok"
    assert payload["ecus_extracted"] == 2
    assert payload["diffusion_failures"] == 2  # stored, just not diffused


def test_query_returns_groups_and_writes_metadata(server, brain, manager,
                                                  active_session, model):
    c1 = make_canonical(brain, model,
                        "Token refresh must be optimistic to avoid races")
    c2 = make_canonical(brain, model,
                        "The auth cache must be cleared after refresh failures")
    brain.add_edge(c1, c2, "supports", weight=0.9)

    payload, result = call_tool(server, "ec_query", {
        "query": "how does auth token refresh work?", "mode": "implementation"})
    assert result["isError"] is False
    assert payload["status"] == "ok"
    assert payload["session_active"] is True
    assert payload["groups_retrieved"] >= 1
    ids = {g["core_ecu"]["id"] for g in payload["groups"]}
    assert c1 in ids or c2 in ids
    # §16.3 shape keys
    for key in ("mode", "budget_used", "groups", "warnings", "brain_source",
                "message"):
        assert key in payload

    # D17: retrieval metadata written on the query path (canonical only)
    stamped = 0
    for ecu_id in (c1, c2):
        meta = brain.get_ecu(ecu_id)["metadata"]
        if meta["last_retrieved"] is not None:
            stamped += 1
            assert meta["retrieval_count"] == 1
    assert stamped >= 1
    # second query increments
    call_tool(server, "ec_query", {
        "query": "how does auth token refresh work?", "mode": "implementation"})
    for ecu_id in (c1, c2):
        meta = brain.get_ecu(ecu_id)["metadata"]
        if meta["last_retrieved"] is not None:
            assert meta["retrieval_count"] == 2

    # D22: activation persisted after the query (§6.6 resume source)
    assert len(brain.load_activation(active_session)) >= 1


def test_query_no_results_message(server, active_session):
    payload, _ = call_tool(server, "ec_query", {
        "query": "obscure topic with no cognition", "mode": "investigation"})
    assert payload["status"] == "ok"
    assert payload["groups_retrieved"] == 0
    assert "No ECUs found for query" in payload["message"]
    assert "broadening the scope" in payload["message"]


# ---------------------------------------------------------------------------
# stdio end-to-end (real subprocess, NDJSON)
# ---------------------------------------------------------------------------

def test_stdio_server_subprocess(tmp_path):
    env = dict(os.environ)
    env["EC_HOME"] = str(tmp_path / "ec_home")
    env["EC_REPO_PATH"] = REPO
    env["EC_BRANCH"] = BRANCH
    env["PYTHONPATH"] = str(ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "ec.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=ROOT,
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "test"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "ec_get_summary", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "ec_observe",
                    "arguments": {"user_prompt": "x", "reasoning_trace": "y"}}},
    ]
    try:
        for req in requests:
            proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()

        responses = {}
        for _ in range(4):  # the notification gets no response
            line = proc.stdout.readline()
            assert line, "server closed stdout unexpectedly"
            msg = json.loads(line)
            responses[msg["id"]] = msg

        assert responses[1]["result"]["serverInfo"]["name"] == "ec"
        assert len(responses[2]["result"]["tools"]) == 4  # +ec_reconsolidate
        summary = json.loads(responses[3]["result"]["content"][0]["text"])
        assert summary["status"] == "ok"
        assert summary["session"]["active"] is False
        observe = json.loads(responses[4]["result"]["content"][0]["text"])
        assert observe["error"] == NO_SESSION_ERROR
        assert responses[4]["result"]["isError"] is True
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# live (skipif-gated)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not api_key(), reason="OPENCODE_ZEN_API_KEY not set")
def test_live_observe_and_query(server, brain, active_session, model):
    payload, _ = call_tool(server, "ec_observe", {
        "user_prompt": "Why do webhook retries double-charge customers?",
        "reasoning_trace": (
            "I read src/webhook.py and found the handler charges the card "
            "before acknowledging the webhook. If the ack fails, the retry "
            "re-charges. The fix is to record 'charged' before acking — "
            "webhook processing must be idempotent."
        ),
        "final_output": "Added a charged-status transition before the ack.",
    })
    assert payload["status"] == "ok"
    assert payload["ecus_extracted"] >= 1

    payload, _ = call_tool(server, "ec_query", {
        "query": "webhook idempotency and retry handling"})
    assert payload["status"] == "ok"
    assert payload["groups_retrieved"] >= 1
