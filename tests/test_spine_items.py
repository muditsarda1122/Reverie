"""Step 3: typed extraction, two-stage dedupe, auto-accept, undo, digest."""

import copy
import json
import subprocess
from unittest.mock import patch

import pytest
from fake_embed import FakeEmbed

from reverie import dedupe, deliver, digest, items, typed_extractor
from reverie.brain import Brain
from reverie.config import DEFAULT_CONFIG, _to_attrdict
from reverie.extractor import ExtractionError


def cfg(**dedupe_over):
    c = _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))
    for k, v in dedupe_over.items():
        c.dedupe[k] = v
    return c


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proj"
    repo.mkdir()
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=repo, check=True)
    (repo / "a.py").write_text("x=1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)
    brain = Brain(tmp_path / "home" / "reverie.db")
    yield brain, repo
    brain.close()


USER = ("Before you add the retry helper: in this codebase we never use print() "
        "for diagnostics, always the logging module. Go ahead.")
ASSISTANT = "Added retry_with_backoff in utils.py with a module-level logger."
INTER = {"prompt": USER, "output": ASSISTANT}


# ---------------------------------------------------------------- validation

def test_user_item_with_verified_quote_is_kept_as_user_said():
    cand, why = typed_extractor.validate(
        {"kind": "directive", "text": "Never use print() for diagnostics; use logging.",
         "source": "user", "quote": "we never use print() for diagnostics",
         "anchor": "repo"}, INTER, None)
    assert why == "ok" and cand["source"] == "user_said" and cand["kind"] == "rule"
    assert cand["meta"]["quote"] == "we never use print() for diagnostics"


def test_claimed_user_item_with_a_wrong_quote_is_downgraded_then_needs_evidence():
    cand, why = typed_extractor.validate(
        {"kind": "directive", "text": "Always use tabs everywhere please",
         "source": "user", "quote": "we always use tabs everywhere"}, INTER, None)
    assert cand is None and "evidence" in why


def test_agent_inferred_needs_a_quote_that_appears_in_the_exchange():
    ok, _ = typed_extractor.validate(
        {"kind": "decision", "text": "Retries use a module-level logger.",
         "source": "agent", "evidence": "with a module-level logger"}, INTER, None)
    assert ok["source"] == "agent_inferred"
    bad, why = typed_extractor.validate(
        {"kind": "decision", "text": "Retries use jittered backoff always.",
         "source": "agent", "evidence": "uses decorrelated jitter"}, INTER, None)
    assert bad is None and "evidence" in why


def test_kind_mapping_anchor_and_symbols():
    cand, _ = typed_extractor.validate(
        {"kind": "plan", "text": "CLI part: get and ls come last.", "source": "user",
         "quote": "we never use print() for diagnostics", "anchor": "tinydb/cli.py",
         "symbols": ["Storage.close", "not an identifier!", "x"]}, INTER, None)
    assert cand["kind"] == "work_state"
    assert cand["anchor"]["files"] == ["tinydb/cli.py"]
    assert cand["anchor"]["symbols"] == ["Storage.close"]
    assert typed_extractor.validate({"kind": "gossip", "text": "x" * 20}, INTER, None)[0] is None


def test_non_json_reply_is_a_terminal_extraction_error():
    with patch("reverie.typed_extractor.call_llm", return_value="I cannot help."):
        with pytest.raises(ExtractionError):
            typed_extractor.extract_candidates(INTER, cfg())


# -------------------------------------------------------------------- dedupe

def vec(*parts):
    v = FakeEmbed().encode_one(" ".join(parts))
    return v


def test_screen_bands():
    c = cfg()
    emb = FakeEmbed()
    existing = [("a", "cache invalidation uses mtime", emb.encode_one("cache invalidation uses mtime"))]
    calls = []
    judge = lambda p: calls.append(p) or "NEW_INFO=NO"       # noqa: E731
    far = dedupe.screen("zebra quantum banana", emb.encode_one("zebra quantum banana"),
                        existing, c, judge)
    assert far.keep and far.stage == "low" and not calls
    same = dedupe.screen("cache invalidation uses mtime",
                         emb.encode_one("cache invalidation uses mtime"), existing, c, judge)
    assert not same.keep and same.stage == "high" and not calls and same.nearest_id == "a"
    mid_text = "cache invalidation uses mtime and inode and size"
    mid = dedupe.screen(mid_text, emb.encode_one(mid_text), existing,
                        cfg(low=0.3, high=0.99), judge)
    assert mid.stage == "judge" and len(calls) == 1 and not mid.keep
    yes = dedupe.screen(mid_text, emb.encode_one(mid_text), existing,
                        cfg(low=0.3, high=0.99), lambda p: "NEW_INFO=YES")
    assert yes.keep


def test_judge_failure_fails_open_and_judge_off_keeps():
    emb = FakeEmbed()
    existing = [("a", "t one two three", emb.encode_one("t one two three"))]
    text = "t one two three four"

    def boom(_):
        raise RuntimeError("down")
    r = dedupe.screen(text, emb.encode_one(text), existing, cfg(low=0.3, high=0.99), boom)
    assert r.keep and r.stage == "judge_error"
    r = dedupe.screen(text, emb.encode_one(text), existing,
                      cfg(low=0.3, high=0.99, judge=False), boom)
    assert r.keep and r.stage == "judge_off"


def test_sandbox_trivia_question_is_optional():
    assert dedupe.parse_verdict("NEW_INFO=YES SANDBOX_TRIVIA=YES", True) is False
    assert dedupe.parse_verdict("NEW_INFO=YES SANDBOX_TRIVIA=YES", False) is True


# ------------------------------------------------------------------ pipeline

def reply(*objs):
    return json.dumps(list(objs))


RULE = {"kind": "directive", "text": "Never use print() for diagnostics; use the logging module.",
        "source": "user", "quote": "we never use print() for diagnostics",
        "anchor": "repo", "symbols": ["retry_with_backoff"]}


def run(brain, repo, llm_reply, judge=None, interaction=INTER, config=None):
    with patch("reverie.typed_extractor.call_llm", return_value=llm_reply):
        return typed_extractor.extract_and_store_items(
            brain, interaction, repo_path=str(repo), embedding_model=FakeEmbed(),
            config=config or cfg(), commit_hash="abc123", judge=judge or (lambda p: "NEW_INFO=YES"))


def test_pipeline_auto_accepts_and_stores_typed_item(env):
    brain, repo = env
    res = run(brain, repo, reply(RULE))
    assert len(res.stored) == 1
    it = items.get(brain._conn, res.stored[0])
    assert (it["status"], it["kind"], it["source"], it["author"], it["level"]) == \
        ("active", "rule", "user_said", "user", "repo")
    assert it["grounding_commit"] == "abc123" and it["last_verified"]
    assert it["anchor"]["symbols"] == ["retry_with_backoff"]
    assert it["repo_key"] == deliver.repo_key(repo)


def test_second_identical_item_is_deduped_and_corroborates(env):
    brain, repo = env
    first = run(brain, repo, reply(RULE)).stored[0]
    res = run(brain, repo, reply(RULE))
    assert res.stored == [] and res.deduped == 1
    meta = items.get(brain._conn, first)["meta"]
    assert meta["seen"] == 2


def test_noise_and_unverifiable_items_store_nothing(env):
    brain, repo = env
    assert run(brain, repo, "[]").stored == []
    res = run(brain, repo, reply({"kind": "decision", "text": "Use jitter everywhere in backoff",
                                  "source": "agent", "evidence": "never said this"}))
    assert res.stored == [] and res.rejected_count == 1


def test_undo_and_restore_are_reversible_and_hide_the_item(env):
    brain, repo = env
    iid = run(brain, repo, reply(RULE)).stored[0]
    c = brain._conn
    assert "Undone" in digest.undo(c, iid[:8])
    assert items.active(c, deliver.repo_key(repo)) == []
    # an undone item no longer blocks the same fact being learned again
    assert len(run(brain, repo, reply(RULE)).stored) == 1
    assert "Restored" in digest.undo(c, iid[:8], restore=True)
    assert "No single item" in digest.undo(c, "zzzz")


def test_digest_lists_new_items_and_marks_seen(env):
    brain, repo = env
    run(brain, repo, reply(RULE))
    key = deliver.repo_key(repo)
    out = digest.digest(brain._conn, key, mark=False)
    assert "you said" in out and "reverie undo" in out and "print()" in out
    assert "Nothing new" not in out
    digest.digest(brain._conn, key, mark=True)
    # items created before the mark are no longer new
    assert "Nothing new" in digest.digest(brain._conn, key, mark=False)


def test_nudge_fires_only_when_enough_is_unseen(env):
    brain, repo = env
    key = deliver.repo_key(repo)
    run(brain, repo, reply(RULE))
    assert digest.nudge(brain._conn, key, min_new=2) == ""
    assert "1 new item" in digest.nudge(brain._conn, key, min_new=1)


def test_agent_inferred_items_need_evidence_in_the_store_too(env):
    brain, _ = env
    with pytest.raises(ValueError):
        items.add(brain._conn, text="inferred thing", kind="decision",
                  source="agent_inferred", repo_key="k")
    assert items.add(brain._conn, text="said thing", kind="decision",
                     source="user_said", repo_key="k")


def test_capture_turn_end_to_end_default_mode(env, monkeypatch):
    from reverie import core_api
    from reverie.mcp_server import ReverieServer
    from reverie.session import SessionManager
    brain, repo = env
    server = ReverieServer(brain=brain, manager=SessionManager(brain),
                           embedding_model=FakeEmbed())
    monkeypatch.setattr("reverie.dedupe.llm_judge", lambda c: (lambda p: "NEW_INFO=YES"))
    with patch("reverie.typed_extractor.call_llm", return_value=reply(RULE)):
        out = core_api.capture_turn(str(repo), "main", USER, ASSISTANT, _server=server)
    assert out["status"] == "ok" and out["ecus_extracted"] == 1
    assert "reverie undo" in out["message"]
    assert brain.count_pending_extractions() == 0


def test_digest_watermark_is_per_repo(env):
    brain, repo = env
    c = brain._conn
    items.add(c, text="repo A fact here", kind="decision", source="user_said", repo_key="A")
    items.add(c, text="repo B fact here", kind="decision", source="user_said", repo_key="B")
    digest.digest(c, "A")
    assert "repo B fact" in digest.digest(c, "B", mark=False)
    assert "Nothing new" in digest.digest(c, "A", mark=False)


@pytest.mark.parametrize("raw", [
    '```json\n[{"kind": "decision", "text": "x" }]\n```',
    'Here you go:\n[{"kind": "decision", "text": "x"}]\nHope that helps [1].',
    '[{"kind": "decision", "text": "braces } and ] inside strings"}]',
])
def test_parse_items_tolerates_fences_prose_and_brackets(raw):
    out = typed_extractor.parse_items(raw)
    assert out and out[0]["kind"] == "decision"


@pytest.mark.parametrize("raw", ["no array here", "[not json", '{"a": 1}'])
def test_parse_items_rejects_non_arrays(raw):
    with pytest.raises(typed_extractor.TypedExtractionError):
        typed_extractor.parse_items(raw)
