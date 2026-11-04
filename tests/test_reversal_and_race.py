"""Memory-write defects found on a real repo (httpx): a user reversal left the
old rule active next to the new one (both delivered as "always apply"), and
concurrent writers duplicated a rule (screening read a stale snapshot)."""

import copy
import json
import subprocess
from unittest.mock import patch

import pytest
from fake_embed import FakeEmbed

from reverie import dedupe, deliver, digest, items, typed_extractor
from reverie.brain import Brain
from reverie.config import DEFAULT_CONFIG, _to_attrdict


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "pkg" / "store.py").write_text("class Storage:\n    pass\n")
    (repo / "pkg" / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    brain = Brain(tmp_path / "home" / "reverie.db")
    yield brain, repo
    brain.close()


def add(brain, repo, text, kind="rule", files=(), symbols=(), source="user_said",
        emb=None, **kw):
    return items.add(
        brain._conn, text=text, kind=kind, source=source, evidence="e",
        repo_key=deliver.repo_key(repo), anchor_=items.anchor(files, symbols),
        grounding_commit=git(repo, "rev-parse", "HEAD"), author="user",
        embedding=emb, **kw)


def flag_stale(brain, item_id):
    items.set_meta(brain._conn, item_id, {"stale": {
        "commit": "deadbeef1234", "message": "changed it", "reason": "now allows X"}})



# ------------------------------------------------------------ reversals

def cfg():
    return _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))


def run(brain, repo, objs, judge):
    with patch("reverie.typed_extractor.call_llm", return_value=json.dumps(objs)):
        return typed_extractor.extract_and_store_items(
            brain, {"prompt": PROMPT, "output": "ok"}, repo_path=str(repo),
            embedding_model=FakeEmbed(), config=cfg(), commit_hash="abc", judge=judge)


PROMPT = ("Change of plan: we DO retry 429 now and honour Retry-After, forget what I said "
          "before about 429 and Retry-After.")
NEW = {"kind": "directive", "text": "Retry 429 responses and honour the Retry-After header.",
       "source": "user", "quote": "we DO retry 429 now and honour Retry-After", "anchor": "repo"}


def _old(brain, repo):
    text = "Do not retry on 429 and do not handle Retry-After."
    return add(brain, repo, text, emb=FakeEmbed().encode_one(text).tobytes())


def judge_says(replaces):
    def judge(prompt):
        if "REPLACES=" in prompt:
            return f"REPLACES={replaces}"
        return "NEW_INFO=YES"
    return judge


def test_user_reversal_supersedes_the_old_rule_and_undo_brings_it_back(env):
    brain, repo = env
    old = _old(brain, repo)
    res = run(brain, repo, [NEW], judge_says("1"))
    assert len(res.stored) == 1 and res.superseded == [old]
    assert items.get(brain._conn, old)["status"] == "superseded"
    text = deliver.startup_text(str(repo))
    assert res.stored[0][:8] in text and old[:8] not in text   # no contradictory pair
    msg = digest.undo(brain._conn, res.stored[0][:8])
    assert "brought back" in msg and items.get(brain._conn, old)["status"] == "active"
    again = digest.undo(brain._conn, res.stored[0][:8], restore=True)
    assert "superseded again" in again and items.get(brain._conn, old)["status"] == "superseded"


def test_a_judge_that_says_none_or_fails_replaces_nothing(env):
    brain, repo = env
    old = _old(brain, repo)
    res = run(brain, repo, [NEW], judge_says("NONE"))
    assert res.superseded == [] and items.get(brain._conn, old)["status"] == "active"

    def boom(prompt):
        if "REPLACES=" in prompt:
            raise RuntimeError("llm down")
        return "NEW_INFO=YES"
    brain2_old = items.get(brain._conn, old)
    res = run(brain, repo, [{**NEW, "text": NEW["text"] + " Really."}], boom)
    assert res.superseded == [] and items.get(brain._conn, brain2_old["id"])["status"] == "active"


def test_only_user_said_reversible_kinds_can_supersede(env):
    brain, repo = env
    old = _old(brain, repo)
    inferred = {"kind": "decision", "text": "Retry 429 responses and honour Retry-After.",
                "source": "agent", "evidence": "we DO retry 429 now and honour Retry-After",
                "anchor": "repo"}
    res = run(brain, repo, [inferred], judge_says("1"))
    assert res.superseded == []                            # an agent cannot retire a user rule
    assert items.get(brain._conn, old)["status"] == "active"


@pytest.mark.parametrize("raw,count,want", [
    ("REPLACES=1", 3, [0]), ("REPLACES=1, 3", 3, [0, 2]), ("replaces = none", 3, []),
    ("REPLACES=NONE", 3, []), ("REPLACES=7", 3, []), ("garbage", 3, []), ("", 3, [])])
def test_parse_replaces(raw, count, want):
    assert dedupe.parse_replaces(raw, count) == want


# ------------------------------------------------------------ dedupe race

def test_a_fact_stored_by_another_writer_during_the_judge_is_caught(env):
    """Two agents share one memory. A's candidate passes the screen; before it
    is written, B stores the same fact. Without the re-screen both are kept."""
    brain, repo = env
    text = "Never retry a ReadTimeout, whatever the method."
    obj = {"kind": "directive", "text": text, "source": "user",
           "quote": "never retry a ReadTimeout", "anchor": "repo"}
    emb = FakeEmbed()
    # a neighbour in the judge band so the judge actually runs
    add(brain, repo, "Retry only idempotent methods.",
        emb=emb.encode_one("Retry only idempotent methods.").tobytes())
    calls = {"n": 0}

    def judge(prompt):
        calls["n"] += 1
        if calls["n"] == 1:      # B writes the same fact while A's judge runs
            add(brain, repo, text, emb=emb.encode_one(text).tobytes())
        return "NEW_INFO=YES"
    c = cfg()
    c.dedupe.low, c.dedupe.high = 0.0, 0.9    # the neighbour hits the judge; an exact copy drops
    with patch("reverie.typed_extractor.call_llm", return_value=json.dumps([obj])):
        res = typed_extractor.extract_and_store_items(
            brain, {"prompt": "never retry a ReadTimeout, whatever", "output": "ok"},
            repo_path=str(repo), embedding_model=emb, config=c, commit_hash="abc", judge=judge)
    same = [r for r in brain._conn.execute("SELECT text FROM items WHERE status='active'")
            if "ReadTimeout" in r["text"]]
    assert len(same) == 1 and res.stored == [] and res.deduped == 1


