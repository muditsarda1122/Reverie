import json
import sqlite3
import time

import pytest

from reverie import importers, spool
from reverie.importers import claude, codex, cursor, opencode

SECRET = "sk-ant-api03-Qw8ErTy2UiOp4AsDf6GhJk9LzXc1VbNm"


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "cursor"))
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "oc.db"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    queued: list = []
    monkeypatch.setattr(importers, "_extract", lambda cwd, ex: queued.extend(ex))
    return repo, tmp_path, queued


def _claude_file(tmp, repo, lines, name="s1"):
    d = tmp / "claude" / "projects" / importers.slug_of(str(repo))
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    with open(f, "a") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")
    return f


def _u(text, sid="s1", ts="2026-11-01T10:00:00.000Z"):
    return {"type": "user", "sessionId": sid, "timestamp": ts, "gitBranch": "feat/x",
            "message": {"role": "user", "content": text}}


def _a(text, sid="s1", ts="2026-11-01T10:00:05.000Z"):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _run(repo, **kw):
    return importers.run(repo, adapters_=[claude.ADAPTER, codex.ADAPTER,
                                          opencode.ADAPTER, cursor.ADAPTER],
                         now=time.time(), **kw)


def test_claude_import_redacts_and_cursor_prevents_rereads(env):
    repo, tmp, queued = env
    f = _claude_file(tmp, repo, [
        _u(f"use key {SECRET} and never retry 429"), _a("ok, noted"),
        _u("now add the test"), _a("added")])
    # file is old: the sessions are over
    old = time.time() - 3 * 86400
    import os
    os.utime(f, (old, old))
    r = _run(repo)
    assert (r.prompts, r.answers) == (2, 2)
    raw = (spool.repo_dir(repo) / "log.jsonl").read_text()
    assert SECRET not in raw and "[REDACTED:anthropic-key]" in raw
    assert all(rec["harness"] == "claude" and rec["branch"] == "feat/x"
               for rec in spool.read(repo))
    r2 = _run(repo)
    assert (r2.prompts, r2.answers, r2.sources) == (0, 0, 0)   # nothing read twice
    assert len(spool.read(repo)) == 4


def test_only_new_bytes_are_read(env):
    repo, tmp, _ = env
    f = _claude_file(tmp, repo, [_u("first"), _a("done")])
    import os
    old = time.time() - 3 * 86400
    os.utime(f, (old, old))
    _run(repo)
    _claude_file(tmp, repo, [_u("second", ts="2026-11-01T11:00:00.000Z"), _a("done 2")])
    os.utime(f, (old + 5, old + 5))
    r = _run(repo)
    assert (r.prompts, r.answers) == (1, 1)


def test_hook_written_records_are_not_duplicated(env):
    repo, tmp, _ = env
    spool.append(repo, "prompt", "never retry 429", session="s1", harness="claude")
    spool.append(repo, "answer", "ok", session="s1", harness="claude")
    f = _claude_file(tmp, repo, [_u("never retry 429"), _a("ok")])
    import os
    old = time.time() - 86400
    os.utime(f, (old, old))
    r = _run(repo)
    assert (r.prompts, r.answers) == (0, 0)
    assert len(spool.read(repo)) == 2


def test_killed_session_keeps_its_last_prompt_and_queues_it(env):
    repo, tmp, queued = env
    f = _claude_file(tmp, repo, [_u("add retry_on"), _a("done"),
                                 _u("also: DEBUG only and test both transports")])
    import os
    old = time.time() - 3600
    os.utime(f, (old, old))
    r = _run(repo)
    assert r.prompts == 2
    texts = [x["text"] for x in spool.read(repo) if x["kind"] == "prompt"]
    assert "also: DEBUG only and test both transports" in texts
    unanswered = [q for q in queued if q["reply"] == importers.NO_REPLY]
    assert len(unanswered) == 1 and "DEBUG only" in unanswered[0]["prompt"]


def test_live_session_pairs_answer_that_arrives_later(env):
    repo, tmp, queued = env
    _claude_file(tmp, repo, [_u("do the thing")])            # file is fresh: live
    r = _run(repo)
    assert r.prompts == 1 and not queued                      # not cut off yet
    _claude_file(tmp, repo, [_a("done it")])
    r = _run(repo)
    assert r.answers == 0                                     # not final yet
    _claude_file(tmp, repo, [_u("next", ts="2026-11-01T12:00:00.000Z")])
    _run(repo)
    assert any(x["kind"] == "answer" and x["text"] == "done it" for x in spool.read(repo))
    assert len(queued) == 1 and queued[0]["reply"] == "done it"


def test_claude_noise_and_other_repos_ignored(env):
    repo, tmp, _ = env
    f = _claude_file(tmp, repo, [
        _u("<command-name>/clear</command-name>"),
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x"}]}},
        _u("<system-reminder>ctx</system-reminder>real words"), _a("fine")])
    other = tmp / "elsewhere"
    other.mkdir()
    _claude_file(tmp, other, [_u("not mine"), _a("no")], name="s9")
    import os
    os.utime(f, (time.time() - 99999,) * 2)
    _run(repo)
    prompts = [x["text"] for x in spool.read(repo) if x["kind"] == "prompt"]
    assert prompts == ["real words"]


def test_codex_skips_injected_context_and_reads_branch(env):
    repo, tmp, _ = env
    d = tmp / "codex" / "sessions" / "2026" / "11" / "01"
    d.mkdir(parents=True)
    f = d / "rollout-2026-11-01T10-00-00-abc.jsonl"

    def item(role, kind, text):
        return {"timestamp": "2026-11-01T04:30:00.000Z", "type": "response_item",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": kind, "text": text}]}}
    recs = [{"type": "session_meta", "payload": {"id": "cx-1", "cwd": str(repo),
                                                  "git": {"branch": "lab"}}},
            item("user", "input_text", "# AGENTS.md instructions\n..."),
            item("user", "input_text", "<environment_context>x</environment_context>"),
            item("user", "input_text", "finish the CLI, DEBUG only"),
            item("assistant", "output_text", "finished")]
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    import os
    os.utime(f, (time.time() - 99999,) * 2)
    r = _run(repo)
    recs = spool.read(repo)
    assert (r.prompts, r.answers) == (1, 1)
    assert recs[0]["session"] == "cx-1" and recs[0]["branch"] == "lab"
    assert recs[0]["harness"] == "codex"


def test_opencode_sqlite(env):
    repo, tmp, queued = env
    conn = sqlite3.connect(tmp / "oc.db")
    conn.executescript("""
      CREATE TABLE session (id text primary key, directory text, parent_id text, time_updated integer);
      CREATE TABLE message (id text primary key, session_id text, time_created integer, data text);
      CREATE TABLE part (id text primary key, message_id text, session_id text, time_created integer, data text);
    """)
    t0 = int((time.time() - 7200) * 1000)
    conn.execute("insert into session values ('ses_1', ?, null, ?)", (str(repo), t0 + 9))
    conn.execute("insert into session values ('ses_sub', ?, 'ses_1', ?)", (str(repo), t0 + 9))
    conn.execute("insert into message values ('m1','ses_1',?,?)", (t0, json.dumps({"role": "user"})))
    conn.execute("insert into message values ('m2','ses_1',?,?)", (t0 + 5, json.dumps({"role": "assistant"})))
    conn.execute("insert into part values ('p1','m1','ses_1',?,?)",
                 (t0, json.dumps({"type": "text", "text": '"never retry ReadTimeout"'})))
    conn.execute("insert into part values ('p2','m2','ses_1',?,?)",
                 (t0 + 5, json.dumps({"type": "reasoning", "text": "thinking"})))
    conn.execute("insert into part values ('p3','m2','ses_1',?,?)",
                 (t0 + 6, json.dumps({"type": "text", "text": "will do"})))
    conn.commit()
    conn.close()
    r = _run(repo)
    recs = spool.read(repo)
    assert (r.prompts, r.answers) == (1, 1)
    assert recs[0]["text"] == "never retry ReadTimeout" and recs[0]["harness"] == "opencode"
    assert [x["text"] for x in recs if x["kind"] == "answer"] == ["will do"]
    assert _run(repo).sources == 0


def test_cursor_user_query(env):
    repo, tmp, _ = env
    d = tmp / "cursor" / "projects" / importers.slug_of(str(repo)).lstrip("-") \
        / "agent-transcripts" / "c1"
    d.mkdir(parents=True)
    f = d / "c1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"role": "user", "message": {"content": [{"type": "text", "text":
            "<timestamp>Fri</timestamp>\n<user_query>\nuse hmac.compare_digest\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "turn_ended", "status": "success"}]) + "\n")
    import os
    os.utime(f, (time.time() - 99999,) * 2)
    r = _run(repo)
    assert (r.prompts, r.answers) == (1, 1)
    assert spool.read(repo)[0]["text"] == "use hmac.compare_digest"


def test_torn_last_line_is_left_for_next_read(env):
    repo, tmp, _ = env
    f = _claude_file(tmp, repo, [_u("complete")])
    with open(f, "a") as fh:
        fh.write('{"type": "user", "message": {"role": "user", "content": "tor')
    import os
    os.utime(f, (time.time() - 99999,) * 2)
    assert _run(repo).prompts == 1
    with open(f, "a") as fh:
        fh.write('n now"}}\n')
    os.utime(f, (time.time() - 99990,) * 2)
    assert _run(repo).prompts == 1
    assert [x["text"] for x in spool.read(repo)] == ["complete", "torn now"]


def test_resume_imports_a_codex_session_lazily(env, monkeypatch, capsys):
    """A session in a harness with no hooks is visible to `reverie resume`."""
    repo, tmp, _ = env
    monkeypatch.delenv("REVERIE_NO_IMPORT")
    d = tmp / "codex" / "sessions" / "2026" / "11" / "01"
    d.mkdir(parents=True)
    f = d / "rollout-x.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [
        {"type": "session_meta", "payload": {"id": "cx-9", "cwd": str(repo)}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "rule: never log the secret, OPENAI_API_KEY=abc123xyz789"}]}},
    ]) + "\n")
    import os
    os.utime(f, (time.time() - 99999,) * 2)
    from reverie import cli
    assert cli.main(["resume", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "never log the secret" in out and "abc123xyz789" not in out
    assert "OPENAI_API_KEY=[REDACTED]" in out


def test_import_off_switch(env, monkeypatch):
    repo, tmp, _ = env
    assert importers.lazy(repo) is None                  # REVERIE_NO_IMPORT=1 from conftest
