"""Item 7: no LLM on write; collapse at delivery; periodic merge; 'seen N times'."""

import copy

import pytest
from fake_embed import FakeEmbed

from reverie import collapse, deliver, items, merge
from reverie.config import DEFAULT_CONFIG, _to_attrdict
from reverie.embeddings import to_blob


def cfg(**over):
    c = _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))
    for k, v in over.items():
        c.dedupe[k] = v
    return c


def it(text, kind="rule", source="agent_inferred", seen=1, created="2026-01-01"):
    return {"id": text[:6], "text": text, "kind": kind, "source": source,
            "created_at": created, "meta": {"seen": seen}}


def test_restatements_collapse_with_a_count():
    a = it("Never retry ReadTimeout, even if someone asks", source="user_said")
    b = it("never retry ReadTimeout even if someone asks!", seen=2)
    c = it("Always use hmac.compare_digest for signatures")
    out = collapse.collapse([b, c, a])
    assert [x["text"] for x in out] == ["Always use hmac.compare_digest for signatures",
                                       "Never retry ReadTimeout, even if someone asks"]
    keeper = out[1]
    assert keeper["source"] == "user_said" and keeper["seen_n"] == 3


@pytest.mark.parametrize("a,b", [
    ("Do not retry on 429", "Retry on 429"),                       # opposite polarity
    ("Retry on 429 with backoff", "Retry on 503 with backoff"),    # different number
    ("Never log the webhook secret", "Never log the session cookie"),
])
def test_opposites_and_near_misses_are_not_collapsed(a, b):
    assert not collapse.similar(a, b)
    assert len(collapse.collapse([it(a), it(b)])) == 2


def test_kinds_do_not_cross():
    assert len(collapse.collapse([it("use cell_len for widths", kind="rule"),
                                  it("use cell_len for widths", kind="decision")])) == 2


def test_delivery_shows_a_duplicate_once_with_seen_count(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    conn = items.connect(tmp_path / "home" / "reverie.db")
    for t in ("Never retry ReadTimeout, even if someone asks",
              "never retry ReadTimeout even if someone asks"):
        items.add(conn, text=t, kind="rule", source="user_said", repo_key="k")
    mem = deliver.memory(conn, "k", str(tmp_path))
    assert len(mem) == 1 and mem[0]["seen_n"] == 2
    assert "(seen 2 times)" in deliver._line(mem[0])


def _db(tmp_path, texts, emb=None):
    emb = emb or FakeEmbed()
    conn = items.connect(tmp_path / "m.db")
    ids = []
    for t, src in texts:
        ids.append(items.add(conn, text=t, kind="rule", source=src, repo_key="k",
                             evidence="seen in the user's words" if src == "agent_inferred" else None,
                             embedding=to_blob(emb.encode_one(t))))
    return conn, ids


def test_merge_job_merges_near_duplicates_keeps_user_words_and_counts(tmp_path):
    conn, (a, b, c) = _db(tmp_path, [
        ("never retry ReadTimeout even when asked by the gateway team", "agent_inferred"),
        ("Never retry ReadTimeout even when asked by the gateway team.", "user_said"),
        ("Always use hmac.compare_digest to compare signatures", "user_said")])
    rep = merge.run(conn, "k", cfg(merge_sim=0.9))
    assert rep.merged == [(a, b)]
    rows = {r["id"]: r for r in (items.get(conn, x) for x in (a, b, c))}
    assert rows[a]["status"] == "superseded" and rows[a]["meta"]["merged_into"] == b
    assert rows[b]["status"] == "active" and rows[b]["meta"]["seen"] == 2
    assert rows[c]["status"] == "active"


def test_merge_job_never_merges_opposites(tmp_path):
    conn, (a, b) = _db(tmp_path, [
        ("do not retry on 429 responses from the gateway", "user_said"),
        ("retry on 429 responses from the gateway", "user_said")])
    assert merge.run(conn, "k", cfg(merge_sim=0.5)).merged == []
    assert items.get(conn, a)["status"] == items.get(conn, b)["status"] == "active"


def test_write_path_has_no_judge_and_keeps_the_middle_band(tmp_path):
    from reverie import dedupe
    emb = FakeEmbed()
    existing = [("a", "cache invalidation uses mtime", emb.encode_one("cache invalidation uses mtime"))]
    text = "cache invalidation uses mtime and inode and size"
    r = dedupe.screen(text, emb.encode_one(text), existing, cfg(low=0.3, high=0.99))
    assert r.keep and r.stage == "band"
    assert not hasattr(dedupe, "PROMPT")           # the NEW_INFO judge prompt is gone


def test_parse_groups():
    from reverie.merge import parse_groups
    assert parse_groups("GROUPS=1,3;2,5", 5) == [[0, 2], [1, 4]]
    assert parse_groups("GROUPS=NONE", 3) == [] and parse_groups("junk", 3) == []
    assert parse_groups("GROUPS=1,2;2,3", 3) == [[0, 1]]        # an item joins one group
    assert parse_groups("GROUPS=9,10", 3) == []


def test_paraphrases_merge_only_through_the_judge(tmp_path):
    """Embeddings cannot tell a paraphrase from an opposite; the periodic job asks a
    judge once per candidate cluster, and fails safe."""
    texts = [("Do not add any dependency without asking the user first.", "user_said"),
             ("Ask the user before adding any new dependency to the project.", "agent_inferred"),
             ("Use logging, not print", "user_said")]
    conn, (a, b, c) = _db(tmp_path, texts)
    low = cfg(merge_sim=0.99, merge_candidate_sim=0.1)
    assert merge.run(conn, "k", low).merged == []                           # no judge: nothing
    calls = []
    boom = lambda p: (_ for _ in ()).throw(RuntimeError("down"))             # noqa: E731
    assert merge.run(conn, "k", low, judge=boom).merged == []               # error: nothing
    judge = lambda p: calls.append(p) or "GROUPS=1,2"                       # noqa: E731
    rep = merge.run(conn, "k", low, judge=judge)
    assert rep.merged == [(b, a)] and len(calls) == 1
    assert items.get(conn, b)["status"] == "superseded" and items.get(conn, a)["meta"]["seen"] == 2
    assert items.get(conn, c)["status"] == "active"
