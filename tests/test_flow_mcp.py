"""feat/flow tests — MCP review tools (D57): ec_review_list token flow,
ec_review_decide scoping/refusals/channel, and the summary nudge line.

Run: .venv/bin/python -m pytest tests/test_flow_mcp.py -v
Offline; diffusion mocked where accepts occur.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ec.brain import Brain
from ec.mcp_server import ECServer
from ec.session import SessionManager

T0 = datetime.now(timezone.utc).replace(microsecond=0)
REPO, BRANCH = "/repos/flow-repo", "main"


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
def server(brain):
    manager = SessionManager(brain)
    s = ECServer(brain=brain, manager=manager)
    payload, _ = call_tool(s, "ec_set_repo_state", {"state": "enabled"})
    assert payload["status"] == "ok"
    return s


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result


def make_session(brain, repo=REPO, branch=BRANCH):
    return brain.create_session(repo, branch)


def make_candidate(brain, session_id, cognition, confidence=0.5,
                   review_status="pending", created_at=None, scope_path=None):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo",
                  "path": scope_path or f"repo:x > module:m{int(confidence*10)}"},
        "provenance": {"source_type": "debugging",
                       "created_at": (created_at or T0).isoformat()},
        "confidence": confidence,
    }
    cid = brain.insert_session_ecu(session_id, ecu)
    if review_status != "pending":
        brain.update_session_ecu_review_status(cid, review_status)
    return cid


# ---------------------------------------------------------------------------
# ec_review_list
# ---------------------------------------------------------------------------

def test_review_list_ordered_page_and_token(server, brain):
    s = make_session(brain)
    make_candidate(brain, s, "weak", confidence=0.2)
    make_candidate(brain, s, "strong", confidence=0.9)
    make_candidate(brain, s, "middle", confidence=0.5)
    payload, result = call_tool(server, "ec_review_list")
    assert result["isError"] is False
    assert payload["status"] == "ok"
    assert payload["total"] == 3
    assert [c["cognition"] for c in payload["candidates"]] == [
        "strong", "middle", "weak"]
    assert payload["review_token"]
    assert len(payload["candidates"][0]["id"]) >= 8
    # the message block is show-verbatim: full text, ids, one line per field
    assert "strong" in payload["message"]
    assert payload["candidates"][0]["id"] in payload["message"]
    assert "Never decide on the user's behalf" in payload["instructions"]


def test_review_list_scoped_to_repo_by_default(server, brain):
    s1 = make_session(brain)
    make_session(brain, repo="/repos/other")
    make_candidate(brain, s1, "mine", confidence=0.6)
    other = make_session(brain, repo="/repos/other")
    make_candidate(brain, other, "theirs", confidence=0.9)
    payload, _ = call_tool(server, "ec_review_list")
    assert [c["cognition"] for c in payload["candidates"]] == ["mine"]
    payload, _ = call_tool(server, "ec_review_list", {"all": True})
    assert sorted(c["cognition"] for c in payload["candidates"]) == \
        ["mine", "theirs"]


def test_review_list_by_topic_and_paging(server, brain):
    s = make_session(brain)
    for i in range(12):
        make_candidate(brain, s, f"c{i:02d}", confidence=i / 20)
    payload, _ = call_tool(server, "ec_review_list")
    assert payload["total"] == 12
    assert payload["page"] == 1
    assert payload["total_pages"] == 2
    assert len(payload["candidates"]) == 10
    assert "more available" in payload["message"]
    payload2, _ = call_tool(server, "ec_review_list", {"page": 2})
    assert len(payload2["candidates"]) == 2


def test_review_list_by_topic_groups(server, brain):
    s = make_session(brain)
    make_candidate(brain, s, "auth-1", confidence=0.5,
                   scope_path="repo:x > auth")
    make_candidate(brain, s, "db-1", confidence=0.9, scope_path="repo:x > db")
    make_candidate(brain, s, "auth-2", confidence=0.6,
                   scope_path="repo:x > auth")
    payload, _ = call_tool(server, "ec_review_list", {"by_topic": True})
    # topic groups in label order, strongest-first within each group
    assert [c["cognition"] for c in payload["candidates"]] == \
        ["auth-2", "auth-1", "db-1"]


def test_review_list_gated_on_undecided_repo(brain):
    manager = SessionManager(brain)
    s = ECServer(brain=brain, manager=manager)
    payload, result = call_tool(s, "ec_review_list")
    assert payload["status"] == "needs_consent"
    assert result["isError"] is True


# ---------------------------------------------------------------------------
# ec_review_decide
# ---------------------------------------------------------------------------

def test_review_decide_applies_with_agent_channel(server, brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "first", confidence=0.9)
    c2 = make_candidate(brain, s, "second", confidence=0.4)
    listing, _ = call_tool(server, "ec_review_list")
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": listing["review_token"],
            "decisions": [{"id": c1, "action": "accept"},
                          {"id": c2, "action": "reject"}],
        })
    assert payload["status"] == "ok"
    assert sorted(payload["decided"]) == sorted([c1, c2])
    audit = {r["candidate_id"]: r for r in brain.list_review_decisions()}
    assert audit[c1]["channel"] == "agent"
    assert audit[c2]["channel"] == "agent"
    assert brain.ecu_exists(c1) and brain.get_session_ecu(c2) is None


def test_review_decide_edited_cognition(server, brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "rough wording", confidence=0.7)
    listing, _ = call_tool(server, "ec_review_list")
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": listing["review_token"],
            "decisions": [{"id": c1, "action": "accept",
                           "edited_cognition": "polished wording"}],
        })
    assert payload["status"] == "ok"
    assert brain.get_ecu(c1)["cognition"] == "polished wording"


def test_review_decide_unknown_token(server):
    payload, result = call_tool(server, "ec_review_decide", {
        "review_token": "bogus",
        "decisions": [{"id": "x", "action": "accept"}]})
    assert payload["status"] == "error"
    assert result["isError"] is True
    assert "ec_review_list" in payload["error"]


def test_review_decide_ids_outside_token_refused(server, brain):
    s = make_session(brain)
    inside = make_candidate(brain, s, "inside", confidence=0.9)
    other = make_session(brain, repo="/repos/elsewhere")   # scoped out
    outside = make_candidate(brain, other, "outside", confidence=0.4)
    listing, _ = call_tool(server, "ec_review_list")        # only 'inside'
    payload, _ = call_tool(server, "ec_review_decide", {
        "review_token": listing["review_token"],
        "decisions": [{"id": outside, "action": "accept"}]})
    assert payload["status"] == "error"
    assert "not in this listing" in payload["error"]
    # nothing was decided
    assert brain.get_session_ecu(outside)["review_status"] == "pending"
    assert brain.get_session_ecu(inside)["review_status"] == "pending"


def test_review_decide_refuses_bulk_accept_all(server, brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "a", confidence=0.9)
    c2 = make_candidate(brain, s, "b", confidence=0.8)
    listing, _ = call_tool(server, "ec_review_list")
    payload, _ = call_tool(server, "ec_review_decide", {
        "review_token": listing["review_token"],
        "decisions": [{"id": c1, "action": "accept"},
                      {"id": c2, "action": "accept"}]})
    assert payload["status"] == "error"
    assert "bulk accept-all" in payload["error"]
    assert brain.get_session_ecu(c1)["review_status"] == "pending"


def test_review_decide_allows_full_accept_of_single_candidate(server, brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "only one", confidence=0.9)
    listing, _ = call_tool(server, "ec_review_list")
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": listing["review_token"],
            "decisions": [{"id": c1, "action": "accept"}]})
    assert payload["status"] == "ok"
    assert payload["decided"] == [c1]


def test_review_decide_token_not_reusable_after_expiry(server, brain):
    s = make_session(brain)
    make_candidate(brain, s, "one", confidence=0.9)
    listing, _ = call_tool(server, "ec_review_list")
    token = listing["review_token"]
    # simulate TTL expiry by backdating the token's creation
    entry = server._review_tokens[token]
    entry["created_at"] = (T0 - timedelta(hours=2)).isoformat()
    payload, _ = call_tool(server, "ec_review_decide", {
        "review_token": token,
        "decisions": [{"id": entry["ids"][0], "action": "skip"}]})
    assert payload["status"] == "error"


def test_review_decide_token_is_single_use(server, brain):
    """Fix 4: a token cannot decide the same listing twice — the second
    decide call with a consumed token is refused, so each batch needs a
    fresh ec_review_list (friction, not a security boundary)."""
    s = make_session(brain)
    c1 = make_candidate(brain, s, "first", confidence=0.9)
    c2 = make_candidate(brain, s, "second", confidence=0.4)
    listing, _ = call_tool(server, "ec_review_list")
    token = listing["review_token"]
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": token,
            "decisions": [{"id": c1, "action": "accept"}]})
    assert payload["status"] == "ok"
    payload, result = call_tool(server, "ec_review_decide", {
        "review_token": token,
        "decisions": [{"id": c2, "action": "reject"}]})
    assert payload["status"] == "error"
    assert result["isError"] is True
    assert "ec_review_list" in payload["error"]
    assert brain.get_session_ecu(c2)["review_status"] == "pending"


def test_review_decide_no_two_call_bulk_accept_bypass(server, brain):
    """Fix 4: accepting 9 of 10 candidates in one call and the 10th in a
    second call with the same token is refused — the "no bulk accept-all"
    guard cannot be split across calls."""
    s = make_session(brain)
    ids = [make_candidate(brain, s, f"c{i}", confidence=0.5)
           for i in range(10)]
    listing, _ = call_tool(server, "ec_review_list")
    token = listing["review_token"]
    first_nine = ids[:9]
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": token,
            "decisions": [{"id": cid, "action": "accept"}
                          for cid in first_nine]})
    assert payload["status"] == "ok"
    assert sorted(payload["decided"]) == sorted(first_nine)
    # the bypass attempt: same token, the last candidate
    payload, result = call_tool(server, "ec_review_decide", {
        "review_token": token,
        "decisions": [{"id": ids[9], "action": "accept"}]})
    assert payload["status"] == "error"
    assert result["isError"] is True
    assert brain.get_session_ecu(ids[9])["review_status"] == "pending"


def test_review_decide_token_survives_refusals(server, brain):
    """The token is consumed only by SUCCESS: refusals (bulk accept-all)
    leave it valid so the user can retry per candidate from the listing."""
    s = make_session(brain)
    c1 = make_candidate(brain, s, "a", confidence=0.9)
    c2 = make_candidate(brain, s, "b", confidence=0.8)
    listing, _ = call_tool(server, "ec_review_list")
    token = listing["review_token"]
    payload, _ = call_tool(server, "ec_review_decide", {
        "review_token": token,
        "decisions": [{"id": c1, "action": "accept"},
                      {"id": c2, "action": "accept"}]})
    assert payload["status"] == "error"          # bulk accept-all refused
    assert token in server._review_tokens
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": token,
            "decisions": [{"id": c1, "action": "accept"}]})
    assert payload["status"] == "ok"             # retry per candidate works


def test_review_decide_cross_session_batch(server, brain):
    s1 = make_session(brain)
    s2 = make_session(brain, branch="feat/x")
    c1 = make_candidate(brain, s1, "main's", confidence=0.9)
    c2 = make_candidate(brain, s2, "feat's", confidence=0.4)
    listing, _ = call_tool(server, "ec_review_list", {"all": True})
    with patch("ec.diffuser.diffuse_ecu"):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": listing["review_token"],
            "decisions": [{"id": c1, "action": "accept"},
                          {"id": c2, "action": "skip"}]})
    assert payload["status"] == "ok"
    assert brain.get_session(s1)["status"] == "active"
    assert brain.get_session(s2)["status"] == "active"


def test_review_decide_malformed_decisions(server, brain):
    listing, _ = call_tool(server, "ec_review_list", {"all": True})
    for bad in ({}, {"decisions": []},
                {"decisions": "accept everything"},
                {"decisions": [{"id": "x"}]},
                {"decisions": [{"action": "accept"}]}):
        payload, _ = call_tool(server, "ec_review_decide", {
            "review_token": listing["review_token"], **bad})
        assert payload["status"] == "error", bad


# ---------------------------------------------------------------------------
# summary nudge
# ---------------------------------------------------------------------------

def test_summary_nudge_line_when_backlog(server, brain):
    s = make_session(brain)
    for i in range(11):
        make_candidate(brain, s, f"c{i}", confidence=0.5,
                       created_at=T0 - timedelta(days=6))
    payload, result = call_tool(server, "ec_get_summary")
    assert payload["status"] == "ok"
    # fix 5: the count is split to match what ec_review_list shows for
    # this repo (all 11 candidates live in the session's repo here)
    assert "11 conclusions waiting for review " \
        "(11 in this repo, oldest 6 days)" in payload["review_nudge"]
    assert payload["review_nudge"] in payload["message"]


def test_summary_no_nudge_when_small_and_fresh(server, brain):
    s = make_session(brain)
    make_candidate(brain, s, "one", confidence=0.5)
    payload, _ = call_tool(server, "ec_get_summary")
    assert "review_nudge" not in payload


def test_summary_nudge_counts_only(server, brain):
    s = make_session(brain)
    for i in range(11):
        make_candidate(brain, s, f"secret-conclusion-{i}", confidence=0.5,
                       created_at=T0 - timedelta(days=6))
    payload, _ = call_tool(server, "ec_get_summary")
    assert "secret" not in payload["review_nudge"]


def test_summary_no_nudge_for_disabled_repo(brain):
    manager = SessionManager(brain)
    s = ECServer(brain=brain, manager=manager)
    call_tool(s, "ec_set_repo_state", {"state": "disabled"})
    payload, _ = call_tool(s, "ec_get_summary")
    assert payload["status"] == "disabled"
