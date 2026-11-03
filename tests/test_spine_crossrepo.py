"""Step 5: cross-repo learning. A preference learned in repo A is delivered in
repo B; a repo-specific decision is not."""

import copy
import json
import subprocess
from unittest.mock import patch

import pytest
from fake_embed import FakeEmbed

from reverie import deliver, hooks, items, typed_extractor
from reverie.brain import Brain
from reverie.config import DEFAULT_CONFIG, _to_attrdict

PREF = "Keep diffs minimal: do not add tests or docs unless asked."
DECISION = "The storage layer uses an RLock because ids are allocated per table."


def cfg():
    return _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))


def mkrepo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=repo, check=True)
    (repo / "store.py").write_text("class Storage:\n    pass\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    brain = Brain(tmp_path / "home" / "reverie.db")
    yield brain, {n: mkrepo(tmp_path, n) for n in "ABC"}
    brain.close()


def learn(brain, repo, objs, prompt):
    """Run one exchange through the real pipeline with a stubbed LLM."""
    with patch("reverie.typed_extractor.call_llm", return_value=json.dumps(objs)):
        return typed_extractor.extract_and_store_items(
            brain, {"prompt": prompt, "output": "done"}, repo_path=str(repo),
            embedding_model=FakeEmbed(), config=cfg(),
            judge=lambda p: "NEW_INFO=NO")


def inferred(kind, text, evidence, anchor="repo", symbols=()):
    return {"kind": kind, "text": text, "source": "agent", "evidence": evidence,
            "anchor": anchor, "symbols": list(symbols)}


def start(repo):
    out = []
    hooks.session_start(repo, print_fn=out.append)
    return "\n".join(out)


PROMPT_A = "Please fix the cache bug. Keep diffs minimal and skip tests unless I ask. We use an RLock in storage."


def test_preference_seen_in_two_repos_is_promoted_and_delivered_in_a_third(world):
    brain, r = world
    ev_pref, ev_dec = "Keep diffs minimal", "We use an RLock in storage"
    objs = [inferred("preference", PREF, ev_pref),
            inferred("decision", DECISION, ev_dec, anchor="store.py", symbols=["Storage"])]
    a = learn(brain, r["A"], objs, PROMPT_A)
    assert len(a.stored) == 2 and not a.promoted
    # seen in A only: still repo-scoped, nowhere else
    assert PREF not in start(r["B"])

    b = learn(brain, r["B"], objs, PROMPT_A)
    assert len(b.promoted) == 1                       # the preference, not the decision
    pref = items.get(brain._conn, b.promoted[0])
    assert pref["kind"] == "preference" and pref["repo_key"] is None
    assert pref["level"] == "user" and pref["anchor"]["type"] == "user"
    assert deliver.repo_key(r["A"]) in pref["meta"]["seen_repos"]
    assert deliver.repo_key(r["B"]) in pref["meta"]["seen_repos"]

    # delivered in a repo that never saw it
    assert PREF in start(r["C"])
    assert "across all repos" in start(r["C"])
    # the repo-specific decision is NOT delivered there, in any lane
    assert DECISION not in start(r["C"])
    assert "RLock" not in start(r["C"])
    assert deliver.point_of_action(str(r["C"]), "s", "store.py") == ""
    # and it still is delivered where it belongs
    assert "RLock" in deliver.point_of_action(str(r["A"]), "s", "store.py")
    # the decision never became user-wide, even though it was seen in two repos
    rows = brain._conn.execute(
        "SELECT repo_key, kind FROM items WHERE kind='decision'").fetchall()
    assert rows and all(row["repo_key"] is not None for row in rows)


def test_user_stated_always_is_user_wide_immediately(world):
    brain, r = world
    prompt = "From now on I always want minimal diffs in every project. Fix the bug."
    res = learn(brain, r["A"], [{
        "kind": "preference", "text": "The user always wants minimal diffs, in every project.",
        "source": "user", "quote": "I always want minimal diffs in every project",
        "scope": "user"}], prompt)
    it = items.get(brain._conn, res.stored[0])
    assert it["repo_key"] is None and it["level"] == "user" and it["source"] == "user_said"
    assert "minimal diffs" in start(r["B"])


def test_a_rule_about_this_codebase_stays_repo_scoped_even_if_marked_user(world):
    brain, r = world
    prompt = "In this codebase we never use print() for diagnostics."
    res = learn(brain, r["A"], [{
        "kind": "directive", "text": "Never use print() for diagnostics in this codebase.",
        "source": "user", "quote": "In this codebase we never use print() for diagnostics",
        "scope": "user"}], prompt)
    it = items.get(brain._conn, res.stored[0])
    assert it["repo_key"] == deliver.repo_key(r["A"])
    assert "print()" not in start(r["B"])


def test_scope_user_needs_the_users_own_global_wording(world):
    brain, r = world
    res = learn(brain, r["A"], [{
        "kind": "preference", "text": "The user prefers short answers always please.",
        "source": "user", "quote": "short answers", "scope": "user"}],
        "Give me short answers when you summarise.")
    assert items.get(brain._conn, res.stored[0])["repo_key"] is not None


def test_user_user_said_items_in_two_repos_do_not_auto_promote(world):
    """Auto-promotion is for what the AGENT inferred. A rule the user stated
    for one repo does not leak because it was stated twice."""
    brain, r = world
    obj = {"kind": "preference", "text": "Prefer small commits with clear messages please.",
           "source": "user", "quote": "small commits with clear messages"}
    p = "Prefer small commits with clear messages."
    learn(brain, r["A"], [obj], p)
    b = learn(brain, r["B"], [obj], p)
    assert b.promoted == []
    assert "small commits" not in start(r["C"])


def test_promotion_is_reversible_and_undoable(world):
    brain, r = world
    objs = [inferred("preference", PREF, "Keep diffs minimal")]
    learn(brain, r["A"], objs, PROMPT_A)
    pid = learn(brain, r["B"], objs, PROMPT_A).promoted[0]
    assert items.demote(brain._conn, pid)
    assert items.get(brain._conn, pid)["repo_key"] == deliver.repo_key(r["A"])
    assert PREF not in start(r["C"])
    assert items.promote(brain._conn, pid)             # the user marks it always
    assert PREF in start(r["C"])
    from reverie import digest
    digest.undo(brain._conn, pid[:8])
    assert PREF not in start(r["C"])


def test_min_repos_is_configurable(world):
    brain, r = world
    c = cfg()
    c.promotion["min_repos"] = 3
    objs = [inferred("preference", PREF, "Keep diffs minimal")]
    with patch("reverie.typed_extractor.call_llm", return_value=json.dumps(objs)):
        for name in "AB":
            res = typed_extractor.extract_and_store_items(
                brain, {"prompt": PROMPT_A, "output": "d"}, repo_path=str(r[name]),
                embedding_model=FakeEmbed(), config=c, judge=lambda p: "NEW_INFO=NO")
    assert res.promoted == [] and PREF not in start(r["C"])
