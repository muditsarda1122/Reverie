"""feat/flow tests — the `reverie review` TUI (D57): rendering snapshots kept
separate from decision logic; single-keypress loop via scripted keys;
undo semantics; non-interactive flags.

Run: .venv/bin/python -m pytest tests/test_flow_tui.py -v
Offline; diffusion mocked where accepts occur.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from reverie.brain import Brain
from reverie.diffuser import DiffuserError
from reverie.review_cli import (
    Quit,
    ReviewRun,
    build_queue,
    candidate_detail,
    decide_candidate,
    make_key_reader,
    ordered_by_topic,
    render_screen,
    run_tui,
)
from reverie.review_cli import main as cli_main

T0 = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "reverie.db")
    yield b
    b.close()


def make_session(brain, repo="/repos/a", branch="main"):
    return brain.create_session(repo, branch)


def make_candidate(brain, session_id, cognition, confidence=0.5,
                   review_status="pending", scope_path="repo:a > auth",
                   grounding=None, evidence=None):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": scope_path},
        "provenance": {"source_type": "debugging",
                       "created_at": T0.isoformat()},
        "grounding": grounding or {"files": ["auth/token.py"]},
        "evidence_pointers": evidence or ["reasoning step 3"],
        "confidence": confidence,
    }
    cid = brain.insert_session_ecu(session_id, ecu)
    if review_status != "pending":
        brain.update_session_ecu_review_status(cid, review_status)
    return cid


def candidate(brain, cid):
    return brain.get_queue_candidate(cid)


# ---------------------------------------------------------------------------
# rendering — snapshots (pure, no I/O)
# ---------------------------------------------------------------------------

def test_render_screen_snapshot(brain):
    sid = make_session(brain)
    cid = make_candidate(
        brain, sid, "Token refresh must be optimistic to avoid the retry "
        "double-charge.", confidence=0.72,
        scope_path="repo:demo > module:auth")
    screen = render_screen(
        candidate(brain, cid), candidate_detail(brain, candidate(brain, cid)),
        index=3, total=12,
        progress={"accepted": 2, "rejected": 1, "skipped": 1},
    )
    assert screen == (
        "Reverie Review — candidate 3 of 12 "
        "(2 accepted · 1 rejected · 1 skipped so far)\n"
        "\n"
        "Token refresh must be optimistic to avoid the retry double-charge.\n"
        "\n"
        "    type: invariant\n"
        "    scope: repo:demo > module:auth\n"
        "    confidence: 0.72\n"
        "    grounded: auth/token.py  symbols: —\n"
        "    evidence: reasoning step 3\n"
        "    repo: /repos/a (branch main)\n"
        "\n"
        "a accept · r reject · s skip · e edit · g accept group · "
        "u undo · q quit"
    )


def test_render_screen_snapshot_contradiction(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "The new cache is always correct.",
                         confidence=0.55)
    partner = {
        "cognition": "Cache must be invalidated before every refresh.",
        "confidence": 0.81,
    }
    screen = render_screen(
        candidate(brain, cid), {}, index=1, total=2,
        progress={"accepted": 0, "rejected": 0, "skipped": 0},
        partner=partner,
    )
    assert '    contradicts: "Cache must be invalidated before every ' \
        'refresh." (0.81)' in screen
    # both conclusions appear — side by side as separate lines, no boxes
    assert "The new cache is always correct." in screen
    assert "┌" not in screen and "│" not in screen and "╭" not in screen


def test_render_screen_snapshot_expired(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "Old conclusion.", confidence=0.4)
    screen = render_screen(
        candidate(brain, cid), {}, index=1, total=1,
        progress={"accepted": 0, "rejected": 0, "skipped": 0},
        expired=True,
    )
    assert screen.endswith(
        "v revive (back to pending) · a accept · r reject · q quit")


# ---------------------------------------------------------------------------
# decision logic (ReviewRun) — no rendering
# ---------------------------------------------------------------------------

def test_decide_candidate_accept_promotes_and_diffuses(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "accept me", confidence=0.8)
    with patch("reverie.diffuser.diffuse_ecu"):
        outcome = decide_candidate(brain, candidate(brain, cid), "accept")
    assert outcome == {"id": cid, "action": "accept", "diffused": True,
                       "warning": None}
    assert brain.ecu_exists(cid) and brain.get_session_ecu(cid) is None
    audit = brain.list_review_decisions()[0]
    assert audit["decision"] == "accept" and audit["channel"] == "tui"


def test_decide_candidate_diffusion_failure_reports(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "undiffused", confidence=0.8)
    with patch("reverie.diffuser.diffuse_ecu",
               side_effect=DiffuserError("classification failed")):
        outcome = decide_candidate(brain, candidate(brain, cid), "accept")
    assert outcome["diffused"] is False
    assert outcome["warning"] and "promoted without diffusion" \
        in outcome["warning"]
    assert brain.ecu_exists(cid)                 # promotion stands


def test_decide_candidate_invalid_action(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "x")
    with pytest.raises(ValueError, match="unknown review action"):
        decide_candidate(brain, candidate(brain, cid), "maybe")


def test_run_undo_skip(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "skipped")
    run = ReviewRun(brain)
    run.apply(candidate(brain, cid), "skip")
    assert brain.get_session_ecu(cid)["review_status"] == "skipped"
    message = run.undo()
    assert "Undone" in message
    assert brain.get_session_ecu(cid)["review_status"] == "pending"
    assert any(r["decision"] == "undo"
               for r in brain.list_review_decisions())


def test_run_undo_reject_restores_snapshot(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "rejected")
    run = ReviewRun(brain)
    snapshot = run.snapshot(cid)
    run.apply(candidate(brain, cid), "reject")
    assert brain.get_session_ecu(cid) is None
    message = run.undo()
    assert "Undone" in message
    restored = brain.get_session_ecu(cid)
    assert restored is not None
    assert restored["cognition"] == "rejected"
    assert restored["review_status"] == "pending"
    assert snapshot["id"] == cid


def test_run_undo_accept_refused_plainly(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "accepted", confidence=0.8)
    run = ReviewRun(brain)
    with patch("reverie.diffuser.diffuse_ecu"):
        run.apply(candidate(brain, cid), "accept")
    message = run.undo()
    assert "already diffused" in message
    assert "cannot be undone" in message
    assert brain.ecu_exists(cid)                 # stays promoted


def test_run_undo_empty_stack(brain):
    run = ReviewRun(brain)
    assert "Nothing to undo" in run.undo()


# ---------------------------------------------------------------------------
# the loop — scripted keys
# ---------------------------------------------------------------------------

def scripted_reader(keys):
    it = iter(keys)

    def reader():
        value = next(it, "q")
        if value == "q":
            raise Quit()
        return value
    return reader


def test_run_tui_skips_through_and_quits(brain):
    sid = make_session(brain)
    c1 = make_candidate(brain, sid, "first", confidence=0.9)
    c2 = make_candidate(brain, sid, "second", confidence=0.5)
    printed: list[str] = []
    outcome = run_tui(brain, build_queue(brain),
                      key_reader=scripted_reader(["s", "q"]),
                      print_fn=printed.append)
    assert outcome["progress"]["skipped"] == 1
    assert outcome["remaining"] == 1
    assert brain.get_session_ecu(c1)["review_status"] == "skipped"
    assert brain.get_session_ecu(c2)["review_status"] == "pending"
    screens = [p for p in printed if "Reverie Review" in p]
    assert len(screens) == 2                     # q shows the next screen first


def test_run_tui_q_key_quits_cleanly(brain):
    """Item 18: 'q' RETURNED by the reader (a real terminal, not the
    scripted reader's Quit exception) must exit the loop — never crash."""
    sid = make_session(brain)
    c1 = make_candidate(brain, sid, "first", confidence=0.9)
    c2 = make_candidate(brain, sid, "second", confidence=0.5)
    printed: list[str] = []
    outcome = run_tui(brain, build_queue(brain),
                      key_reader=lambda: "q", print_fn=printed.append)
    assert outcome["progress"]["accepted"] == 0
    assert outcome["remaining"] == 2             # c1 was NOT decided either
    assert brain.get_session_ecu(c1)["review_status"] == "pending"
    assert brain.get_session_ecu(c2)["review_status"] == "pending"


def test_run_tui_q_after_skip_reports_true_undecided(brain):
    sid = make_session(brain)
    make_candidate(brain, sid, "first", confidence=0.9)
    c2 = make_candidate(brain, sid, "second", confidence=0.5)
    outcome = run_tui(brain, build_queue(brain),
                      key_reader=scripted_reader(["s", "q"]))
    assert outcome["progress"]["skipped"] == 1
    assert outcome["remaining"] == 1             # only c2 left
    assert brain.get_session_ecu(c2)["review_status"] == "pending"


def test_run_tui_page_flow_shows_more(brain):
    sid = make_session(brain)
    for i in range(11):
        make_candidate(brain, sid, f"c{i:02d}", confidence=i / 20)
    printed: list[str] = []
    outcome = run_tui(brain, build_queue(brain), page_size=10,
                      key_reader=scripted_reader(["s"] * 11),
                      print_fn=printed.append)
    assert outcome["progress"]["skipped"] == 11
    more = [p for p in printed if "more available" in p]
    assert more and "1 candidate(s) remain" in more[0]


def test_run_tui_accept_flow(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "take it", confidence=0.9)
    with patch("reverie.diffuser.diffuse_ecu"):
        outcome = run_tui(brain, build_queue(brain),
                          key_reader=scripted_reader(["a"]))
    assert outcome["progress"]["accepted"] == 1
    assert brain.ecu_exists(cid)


def test_run_tui_edit_via_injected_editor(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "rough", confidence=0.8)
    with patch("reverie.diffuser.diffuse_ecu"):
        outcome = run_tui(
            brain, build_queue(brain), key_reader=scripted_reader(["e"]),
            edit_fn=lambda text: "polished")
    assert outcome["progress"]["accepted"] == 1
    assert brain.get_ecu(cid)["cognition"] == "polished"
    audit = brain.list_review_decisions()[0]
    assert audit["edited_cognition"] == "polished"


def test_run_tui_edit_declined_leaves_open(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "rough", confidence=0.8)
    outcome = run_tui(brain, build_queue(brain),
                      key_reader=scripted_reader(["e"]),
                      edit_fn=lambda text: None)
    assert outcome["progress"]["accepted"] == 0
    assert brain.get_session_ecu(cid)["review_status"] == "pending"


def test_run_tui_group_accept(brain):
    sid = make_session(brain)
    a1 = make_candidate(brain, sid, "a1", confidence=0.9,
                        scope_path="repo:a > auth")
    a2 = make_candidate(brain, sid, "a2", confidence=0.8,
                        scope_path="repo:a > auth")
    db1 = make_candidate(brain, sid, "db1", confidence=0.7,
                         scope_path="repo:a > db")
    printed: list[str] = []
    with patch("reverie.diffuser.diffuse_ecu"):
        # 'g' accepts the whole topic group (including the candidate on
        # screen); already-accepted screens are skipped silently
        outcome = run_tui(brain, build_queue(brain),
                          key_reader=scripted_reader(["g", "g"]),
                          print_fn=printed.append)
    assert outcome["progress"]["accepted"] == 3
    for cid in (a1, a2, db1):
        assert brain.ecu_exists(cid)
    assert any("accepted 2 candidate(s) in group repo:a > auth" in p
               for p in printed)


def test_run_tui_undo_key(brain):
    sid = make_session(brain)
    make_candidate(brain, sid, "first", confidence=0.9)
    second = make_candidate(brain, sid, "second", confidence=0.5)
    printed: list[str] = []
    outcome = run_tui(brain, build_queue(brain),
                      key_reader=scripted_reader(["s", "r", "u", "q"]),
                      print_fn=printed.append)
    assert outcome["progress"]["rejected"] == 0     # undone
    assert brain.get_session_ecu(second) is not None
    assert any("Undone" in p for p in printed)


def test_run_tui_unknown_key_hints(brain):
    sid = make_session(brain)
    make_candidate(brain, sid, "only", confidence=0.5)
    printed: list[str] = []
    run_tui(brain, build_queue(brain), key_reader=scripted_reader(["z", "q"]),
            print_fn=printed.append)
    hints = [p for p in printed if p.startswith("  · (a accept")]
    assert hints


def test_run_tui_expired_revive_only(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "stale", confidence=0.6,
                         review_status="expired")
    printed: list[str] = []
    outcome = run_tui(brain, build_queue(brain, include_expired=True),
                      key_reader=scripted_reader(["v", "q"]),
                      print_fn=printed.append)
    assert outcome["progress"]["revived"] == 1
    assert brain.get_session_ecu(cid)["review_status"] == "pending"


def test_run_tui_expired_accept_revives_and_applies(brain):
    sid = make_session(brain)
    cid = make_candidate(brain, sid, "stale but good", confidence=0.6,
                         review_status="expired")
    with patch("reverie.diffuser.diffuse_ecu"):
        outcome = run_tui(brain, build_queue(brain, include_expired=True),
                          key_reader=scripted_reader(["a"]))
    assert outcome["progress"]["accepted"] == 1
    assert brain.ecu_exists(cid) and brain.get_session_ecu(cid) is None


# ---------------------------------------------------------------------------
# key reader fallback (line input when stdin is not a TTY)
# ---------------------------------------------------------------------------

def test_key_reader_line_fallback_maps_words():
    answers = iter(["accept", "REJECT", "quit"])
    reader = make_key_reader(input_fn=lambda _p="": next(answers))
    assert reader() == "accept"
    assert reader() == "reject"
    with pytest.raises(Quit):
        reader()


def test_key_reader_line_fallback_eof_quits():
    def eof(_p=""):
        raise EOFError
    reader = make_key_reader(input_fn=eof)
    with pytest.raises(Quit):
        reader()


# ---------------------------------------------------------------------------
# non-interactive CLI
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reverie_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "reverie_home"))
    monkeypatch.setenv("REVERIE_REPO_PATH", "/repos/a")
    monkeypatch.setenv("REVERIE_BRANCH", "main")
    return tmp_path / "reverie_home"


@pytest.fixture()
def home_brain(reverie_home):
    """The brain the CLI actually opens (REVERIE_HOME/reverie.db)."""
    b = Brain(reverie_home / "reverie.db")
    yield b
    b.close()


def test_cli_list_and_json(home_brain, capsys):
    sid = make_session(home_brain)
    make_candidate(home_brain, sid, "first", confidence=0.9)
    assert cli_main(["--list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["cognition"] == "first"
    assert cli_main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "first" in out


def test_cli_decide_flag(home_brain, capsys):
    sid = make_session(home_brain)
    c1 = make_candidate(home_brain, sid, "first", confidence=0.9)
    c2 = make_candidate(home_brain, sid, "second", confidence=0.4)
    with patch("reverie.diffuser.diffuse_ecu"):
        assert cli_main(["--decide", f"{c1}=accept", f"{c2}=reject"]) == 0
    out = capsys.readouterr().out
    assert "Applied 2 decision(s)." in out
    assert home_brain.ecu_exists(c1)
    assert home_brain.get_session_ecu(c2) is None


def test_cli_decide_unknown_id_fails(home_brain, capsys):
    sid = make_session(home_brain)
    make_candidate(home_brain, sid, "first")
    assert cli_main(["--decide", "nope=accept"]) == 1
    assert "Unknown candidate id" in capsys.readouterr().err


def test_cli_topic_ordering_option(home_brain, capsys):
    sid = make_session(home_brain)
    make_candidate(home_brain, sid, "auth-1", confidence=0.5,
                   scope_path="repo:a > auth")
    make_candidate(home_brain, sid, "db-1", confidence=0.9,
                   scope_path="repo:a > db")
    assert cli_main(["--list", "--by-topic"]) == 0
    out = capsys.readouterr().out
    assert out.index("auth-1") < out.index("db-1")


def test_cli_empty_queue(capsys):
    assert cli_main([]) == 0
    assert "No pending candidates" in capsys.readouterr().out


def test_cli_expired_listing(home_brain, capsys):
    sid = make_session(home_brain)
    make_candidate(home_brain, sid, "old", confidence=0.4,
                   review_status="expired")
    assert cli_main(["--expired", "--list"]) == 0
    assert "old" in capsys.readouterr().out
    # expired ones are not in the normal queue
    assert cli_main(["--list"]) == 0
    assert "old" not in capsys.readouterr().out


def test_topic_ordering_helper(brain):
    sid = make_session(brain)
    make_candidate(brain, sid, "auth-1", confidence=0.5,
                   scope_path="repo:a > auth")
    make_candidate(brain, sid, "db-1", confidence=0.9,
                   scope_path="repo:a > db")
    queue = ordered_by_topic(build_queue(brain))
    assert [c["cognition"] for c in queue] == ["auth-1", "db-1"]


def test_python_dash_m_review_cli_produces_output(tmp_path, monkeypatch,
                                                  capsys):
    """Regression: `python -m reverie.review_cli` must actually run main() —
    the missing __main__ guard once exited silently."""
    import subprocess
    import sys
    from pathlib import Path
    env = dict(os.environ)
    env["REVERIE_HOME"] = str(tmp_path / "reverie_home")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run([sys.executable, "-m", "reverie.review_cli", "--list"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert proc.stdout.strip(), proc.stderr
