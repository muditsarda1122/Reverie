"""feat/flow tests — cross-session review queue (D57): ordering, filters,
expiry, decisions through the gate (channel recorded), revive, nudge.

Run: .venv/bin/python -m pytest tests/test_flow_review_queue.py -v
Offline; diffusion is mocked-unrelated where accepts occur.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ec.brain import Brain
from ec.review_queue import (
    QueueError,
    apply_decisions,
    candidates,
    expire_stale,
    group_of,
    nudge_line,
    page,
    queue_stats,
    revive,
    topic_groups,
)

T0 = datetime.now(timezone.utc).replace(microsecond=0)


def mk_cfg(**review):
    """Config stub with every review key review_queue reads."""
    review.setdefault("expire_after_days", 21)
    review.setdefault("nudge_threshold", 10)
    review.setdefault("nudge_age_days", 5)
    review.setdefault("page_size", 10)
    return types.SimpleNamespace(review=types.SimpleNamespace(**review))


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def make_session(brain, repo="/repos/a", branch="main", status="active"):
    sid = brain.create_session(repo, branch)
    if status != "active":
        brain._conn.execute(
            "UPDATE sessions SET status = ? WHERE id = ?", (status, sid))
        brain._conn.commit()
    return sid


def make_candidate(brain, session_id, cognition="C", confidence=0.5,
                   review_status="pending", created_at=None, scope_path=None):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo",
                  "path": scope_path or "repo:a > module:auth"},
        "provenance": {"source_type": "debugging",
                       "created_at": (created_at or T0).isoformat()},
        "confidence": confidence,
    }
    cid = brain.insert_session_ecu(session_id, ecu)
    if review_status != "pending":
        brain.update_session_ecu_review_status(cid, review_status)
    return cid


# ---------------------------------------------------------------------------
# ordering + filters
# ---------------------------------------------------------------------------

def test_queue_strongest_first(brain):
    s = make_session(brain)
    make_candidate(brain, s, "weak", confidence=0.2)
    make_candidate(brain, s, "strong", confidence=0.9)
    make_candidate(brain, s, "middle", confidence=0.5)
    assert [c["cognition"] for c in candidates(brain)] == [
        "strong", "middle", "weak"]


def test_queue_default_current_repo_semantics_at_caller(brain):
    """The CLI/MCP layer decides default scope; the module only filters."""
    s1 = make_session(brain, repo="/repos/a")
    make_session(brain, repo="/repos/b")
    make_candidate(brain, s1, "A1")
    make_candidate(brain, s1, "A2", scope_path="repo:a > module:db")
    all_rows = candidates(brain)
    assert len(all_rows) == 2
    assert len(candidates(brain, repo="/repos/b")) == 0


def test_topic_groups_option(brain):
    s = make_session(brain)
    make_candidate(brain, s, "auth-1", scope_path="repo:a > module:auth")
    make_candidate(brain, s, "db-1", confidence=0.9,
                   scope_path="repo:a > module:db")
    make_candidate(brain, s, "auth-2", confidence=0.6,
                   scope_path="repo:a > module:auth")
    groups = topic_groups(candidates(brain))
    assert [label for label, _ in groups] == [
        "repo:a > module:auth", "repo:a > module:db"]
    auth = dict(groups)["repo:a > module:auth"]
    assert [c["cognition"] for c in auth] == ["auth-2", "auth-1"]


def test_group_of_finds_label(brain):
    s = make_session(brain)
    make_candidate(brain, s, "auth-1", scope_path="repo:a > auth")
    make_candidate(brain, s, "db-1", confidence=0.9, scope_path="repo:a > db")
    queue = candidates(brain)
    groups = topic_groups(queue)
    assert group_of(queue[0], groups) == "repo:a > db"
    assert group_of(queue[1], groups) == "repo:a > auth"


def test_page_never_discards(brain):
    s = make_session(brain)
    for i in range(13):
        make_candidate(brain, s, f"c{i:02d}", confidence=i / 20)
    cfg = mk_cfg(page_size=10)
    first, total = page(candidates(brain), 1, config=cfg)
    assert len(first) == 10
    second, total = page(candidates(brain), 2, config=cfg)
    assert len(second) == 3
    assert total == 2
    everything = candidates(brain)
    assert len(everything) == 13                 # nothing discarded


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------

def test_expiry_transitions_old_candidates(brain):
    s = make_session(brain)
    old = T0 - timedelta(days=30)
    fresh = T0
    old_id = make_candidate(brain, s, "old-one", created_at=old)
    make_candidate(brain, s, "fresh-one", created_at=fresh)
    expired = expire_stale(brain, now=T0)
    assert expired == [old_id]
    assert brain.get_session_ecu(old_id)["review_status"] == "expired"
    assert brain.get_session_ecu(old_id) is not None          # kept, never deleted
    queue = candidates(brain, now=T0)
    assert [c["cognition"] for c in queue] == ["fresh-one"]


def test_expiry_disabled_at_zero(brain):
    s = make_session(brain)
    make_candidate(brain, s, "ancient",
                   created_at=T0 - timedelta(days=400))
    cfg = mk_cfg(expire_after_days=0)
    assert expire_stale(brain, config=cfg, now=T0) == []
    assert brain.get_session_ecu(
        brain.list_session_ecus(s)[0]["id"])["review_status"] == "pending"


def test_expired_usable_in_session_memory(brain):
    """Expired candidates stay in the Session Brain for retrieval."""
    s = make_session(brain)
    cid = make_candidate(brain, s, "old-one",
                         created_at=T0 - timedelta(days=30))
    expire_stale(brain, now=T0)
    assert brain.get_session_ecu(cid) is not None
    assert brain.count_session_ecus(s) == 1


def test_revive(brain):
    s = make_session(brain)
    cid = make_candidate(brain, s, "old-one",
                         created_at=T0 - timedelta(days=30))
    expire_stale(brain, now=T0)
    result = revive(brain, cid, channel="tui", now=T0)
    assert result["status"] == "pending"
    assert brain.get_session_ecu(cid)["review_status"] == "pending"
    assert candidates(brain, now=T0)[0]["cognition"] == "old-one"
    audit = brain.list_review_decisions()
    assert audit[0]["decision"] == "revive" and audit[0]["channel"] == "tui"


def test_revive_non_expired_rejected(brain):
    s = make_session(brain)
    cid = make_candidate(brain, s, "fresh")
    with pytest.raises(QueueError, match="not expired"):
        revive(brain, cid)
    with pytest.raises(QueueError, match="no candidate"):
        revive(brain, "missing-id")


# ---------------------------------------------------------------------------
# decisions through the gate (close=False, undecided left, channel audit)
# ---------------------------------------------------------------------------

def test_apply_decisions_cross_session_channel_recorded(brain):
    s1 = make_session(brain, repo="/repos/a", branch="main")
    s2 = make_session(brain, repo="/repos/b", branch="dev")
    c1 = make_candidate(brain, s1, "A1", confidence=0.9)
    c2 = make_candidate(brain, s2, "B1", confidence=0.4)
    with patch("ec.diffuser.diffuse_ecu") as mock_diffuse:
        out = apply_decisions(brain, {c1: "accept", c2: "reject"},
                              channel="agent", now=T0)
    assert sorted(out["decided"]) == sorted([c1, c2])
    assert out["unknown"] == []
    assert mock_diffuse.called
    # sessions stay open — review is independent of sessions
    assert brain.get_session(s1)["status"] == "active"
    assert brain.get_session(s2)["status"] == "active"
    audit = {r["candidate_id"]: r for r in brain.list_review_decisions()}
    assert audit[c1]["decision"] == "accept"
    assert audit[c1]["channel"] == "agent"
    assert audit[c2]["decision"] == "reject"
    assert audit[c2]["channel"] == "agent"
    assert audit[c1]["session_id"] == s1


def test_apply_decisions_undecided_left_untouched(brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "decided")
    c2 = make_candidate(brain, s, "undecided")
    with patch("ec.diffuser.diffuse_ecu"):
        out = apply_decisions(brain, {c1: "skip"}, channel="tui", now=T0)
    assert out["decided"] == [c1]
    assert brain.get_session_ecu(c2)["review_status"] == "pending"


def test_apply_decisions_accept_promotes(brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "promote me", confidence=0.8)
    with patch("ec.diffuser.diffuse_ecu"):
        out = apply_decisions(brain, {c1: "accept"}, channel="tui", now=T0)
    assert out["results"][0].accepted == [c1]
    assert brain.ecu_exists(c1)                  # same id, D21
    assert brain.get_session_ecu(c1) is None     # removed from session brain


def test_apply_decisions_edited_cognition_recorded(brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "original text")
    with patch("ec.diffuser.diffuse_ecu"):
        out = apply_decisions(
            brain, {c1: {"action": "accept",
                         "edited_cognition": "edited conclusion"}},
            channel="tui", now=T0)
    assert out["decided"] == [c1]
    assert brain.get_ecu(c1)["cognition"] == "edited conclusion"
    audit = brain.list_review_decisions()[0]
    assert audit["edited_cognition"] == "edited conclusion"


def test_apply_decisions_unknown_ids_reported(brain):
    s = make_session(brain)
    c1 = make_candidate(brain, s, "A1")
    out = apply_decisions(brain, {"no-such-id": "accept"}, channel="agent")
    assert out["unknown"] == ["no-such-id"]
    assert out["decided"] == []
    assert brain.get_session_ecu(c1)["review_status"] == "pending"


def test_apply_decisions_archived_session_reviewable(brain):
    s = make_session(brain, status="archived")
    c1 = make_candidate(brain, s, "orphan")
    with patch("ec.diffuser.diffuse_ecu"):
        out = apply_decisions(brain, {c1: "reject"}, channel="tui", now=T0)
    assert out["decided"] == [c1]
    assert brain.get_session_ecu(c1) is None


# ---------------------------------------------------------------------------
# nudge — counts only, never contents
# ---------------------------------------------------------------------------

def test_nudge_silent_below_threshold(brain):
    s = make_session(brain)
    for i in range(5):
        make_candidate(brain, s, f"c{i}", confidence=0.5, created_at=T0)
    cfg = mk_cfg()
    assert nudge_line(brain, config=cfg, now=T0) is None


def test_nudge_fires_on_threshold(brain):
    s = make_session(brain)
    for i in range(11):
        make_candidate(brain, s, f"c{i}", confidence=0.5, created_at=T0)
    cfg = mk_cfg()
    line = nudge_line(brain, config=cfg, now=T0)
    assert line == "11 conclusions waiting for review (oldest 0 days)"


def test_nudge_fires_on_age(brain):
    s = make_session(brain)
    make_candidate(brain, s, "one", created_at=T0 - timedelta(days=6))
    cfg = mk_cfg()
    line = nudge_line(brain, config=cfg, now=T0)
    assert line == "1 conclusions waiting for review (oldest 6 days)"


def test_nudge_counts_only(brain):
    s = make_session(brain)
    make_candidate(brain, s, "secret-conclusion-text",
                   created_at=T0 - timedelta(days=6))
    cfg = mk_cfg()
    assert "secret" not in (nudge_line(brain, config=cfg, now=T0) or "")


def test_nudge_ignores_expired(brain):
    s = make_session(brain)
    make_candidate(brain, s, "old", created_at=T0 - timedelta(days=30))
    expire_stale(brain, now=T0)
    cfg = mk_cfg()
    assert nudge_line(brain, config=cfg, now=T0) is None


def test_queue_stats(brain):
    s = make_session(brain)
    make_candidate(brain, s, "a", created_at=T0 - timedelta(days=2))
    make_candidate(brain, s, "b", created_at=T0)
    stats = queue_stats(brain, now=T0)
    assert stats == {"count": 2, "oldest_age_days": 2}
