"""Confidence on remembered conclusions: shown where the brain has one, never invented where it has none."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from test_ui_stage3 import env, get, serve  # noqa: F401  (fixtures)

from reverie import cli, deliver, items
from reverie.brain import Brain


def _conclusion(repo, text, confidence, **extra):
    b = Brain(deliver.db_path())
    cid = b.insert_ecu({"cognition": text, "conclusion_type": "decision", "confidence": confidence,
                        "scope": {"level": "repo", "path": f"repo:{repo.name}"},
                        "grounding": {"repo_path": str(repo)}, **extra})
    b.close()
    return cid


def _plain_item(repo, text):
    conn = items.connect(deliver.db_path())
    iid = items.add(conn, text=text, kind="decision", source="user_said", repo_key=deliver.repo_key(str(repo)))
    conn.close()
    return iid


def test_confidence_words_give_the_number_and_the_band():
    assert deliver.confidence_words(0.9) == "0.90 (high)"
    assert deliver.confidence_words(0.5) == "0.50 (medium)"
    assert deliver.confidence_words(0.2) == "0.20 (low)"
    assert deliver.confidence_words(None) == ""


def test_a_conclusion_shows_its_confidence_on_the_card_the_item_page_and_in_show(env, serve, capsys):  # noqa: F811
    _, repo = env
    cid = _conclusion(repo, "Settlement runs on the ledger clock", 0.82)
    s = serve(repo)
    page = get(s, "/memory")[1]
    assert "Confidence 0.82 (high)" in page
    detail = get(s, f"/rule?id={cid[:8]}&p=" + re.search(r"/rule\?p=([0-9a-f]{8})", page).group(1))[1]
    assert "Confidence" in detail and "0.82 (high)" in detail
    assert cli.main(["show", cid[:8]]) == 0
    assert "confidence: 0.82 (high)" in capsys.readouterr().out


def test_an_item_with_no_conclusion_behind_it_shows_no_confidence_anywhere(env, serve, capsys):  # noqa: F811
    _, repo = env
    iid = _plain_item(repo, "Use integer cents for money")
    page = get(serve(repo), "/memory")[1]
    assert "Use integer cents for money" in page and "onfidence" not in page
    assert cli.main(["show", iid[:8]]) == 0
    assert "confidence" not in capsys.readouterr().out


def test_the_shown_number_is_the_decayed_one_not_the_stored_one(env):  # noqa: F811
    _, repo = env
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    cid = _conclusion(repo, "An old belief", 0.8, provenance={"created_at": old})
    conn = items.connect(deliver.db_path())
    shown = next(m for m in deliver.memory(conn, deliver.repo_key(str(repo)), str(repo)) if m["id"] == cid)
    conn.close()
    assert 0 < shown["confidence"] < 0.8           # decays at read time; never written back
