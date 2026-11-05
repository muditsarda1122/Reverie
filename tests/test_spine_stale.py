"""Step 4: staleness worker."""

import json
import subprocess

import pytest

from reverie import deliver, digest, items, staleness
from reverie.brain import Brain
from reverie.config import DEFAULT_CONFIG, _to_attrdict


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def cfg(**stale):
    import copy
    c = _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))
    c.stale["run_tests"] = False
    for k, v in stale.items():
        c.stale[k] = v
    return c


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "pkg" / "lock.py").write_text("def acquire():\n    return RLock()\n")
    (repo / "pkg" / "util.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    brain = Brain(tmp_path / "home" / "reverie.db")
    yield brain, repo
    brain.close()


def memory(brain, repo, text, files=(), symbols=()):
    return items.add(brain._conn, text=text, kind="decision", source="user_said",
                     repo_key=deliver.repo_key(repo),
                     anchor_=items.anchor(files, symbols),
                     grounding_commit=git(repo, "rev-parse", "HEAD"))


def commit(repo, path, content, msg):
    (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


class Judge:
    def __init__(self, verdicts=None, default="STILL_TRUE: ok"):
        self.calls, self.verdicts, self.default = [], verdicts or {}, default

    def __call__(self, prompt):
        self.calls.append(prompt)
        for needle, v in self.verdicts.items():
            if needle in prompt:
                return v
        return self.default


def baseline(brain, repo):
    staleness.check(brain._conn, str(repo), cfg(), Judge())


def test_first_check_only_baselines(env):
    brain, repo = env
    memory(brain, repo, "Locking uses RLock", files=["pkg/lock.py"])
    j = Judge()
    rep = staleness.check(brain._conn, str(repo), cfg(), j)
    assert rep.commits == 0 and j.calls == []
    assert staleness.get_state(brain._conn, deliver.repo_key(repo)) == git(repo, "rev-parse", "HEAD")


def test_only_memories_the_commit_touched_reach_the_judge(env):
    brain, repo = env
    memory(brain, repo, "Locking uses RLock", files=["pkg/lock.py"])
    memory(brain, repo, "Util is trivial", files=["pkg/util.py"])
    baseline(brain, repo)
    commit(repo, "pkg/util.py", "x = 2\n", "tweak util")
    j = Judge()
    rep = staleness.check(brain._conn, str(repo), cfg(), j)
    assert len(j.calls) == 1 and "Util is trivial" in j.calls[0]
    assert rep.triggered == 1 and rep.possible == 2 and rep.flags == []


def test_symbol_in_the_diff_triggers_without_a_file_anchor(env):
    brain, repo = env
    memory(brain, repo, "Never swap RLock for Lock", symbols=["RLock"])
    baseline(brain, repo)
    commit(repo, "pkg/other.py", "from threading import Lock\n# RLock removed\n", "drop RLock")
    j = Judge()
    staleness.check(brain._conn, str(repo), cfg(), j)
    assert len(j.calls) == 1
    commit(repo, "pkg/unrelated.py", "y = 1\n", "unrelated")
    staleness.check(brain._conn, str(repo), cfg(), j)
    assert len(j.calls) == 1                      # nothing new reached the judge


def test_contradicted_memory_is_flagged_with_a_candidate_never_silently_changed(env):
    brain, repo = env
    mid = memory(brain, repo, "Locking uses RLock", files=["pkg/lock.py"])
    baseline(brain, repo)
    commit(repo, "pkg/lock.py", "def acquire():\n    return Lock()\n", "S1 switch to Lock")
    j = Judge({"Locking uses RLock":
               "CONTRADICTED: the commit replaces RLock with Lock\nUPDATED: Locking uses a plain Lock."})
    rep = staleness.check(brain._conn, str(repo), cfg(), j)
    assert [f.memory_id for f in rep.flags] == [mid]
    old = items.get(brain._conn, mid)
    assert old["status"] == "active" and old["text"] == "Locking uses RLock"   # untouched
    assert old["meta"]["stale"]["commit"]
    cand = items.get(brain._conn, rep.flags[0].candidate_id)
    assert cand["status"] == "candidate" and cand["text"] == "Locking uses a plain Lock."
    # a candidate is never delivered or indexed
    active_ids = {m["id"] for m in items.active(brain._conn, deliver.repo_key(repo))}
    assert cand["id"] not in active_ids
    # the stale memory carries a warning wherever it is delivered
    mem = deliver.memory(brain._conn, deliver.repo_key(repo), str(repo))
    assert "[MAY BE STALE]" in deliver.headline_index(mem)
    assert "WARNING: may be stale" in deliver.render_point(
        [(next(m for m in mem if m["id"] == mid), "file anchor")], "pkg/lock.py", str(repo))
    assert "stale" in digest.digest(brain._conn, deliver.repo_key(repo), mark=False)


def test_accept_supersedes_and_keep_restores(env):
    brain, repo = env
    mid = memory(brain, repo, "Locking uses RLock", files=["pkg/lock.py"])
    baseline(brain, repo)
    commit(repo, "pkg/lock.py", "def acquire():\n    return Lock()\n", "switch")
    rep = staleness.check(brain._conn, str(repo), cfg(), Judge(
        default="CONTRADICTED: changed\nUPDATED: Uses Lock."))
    cand = rep.flags[0].candidate_id
    assert "Accepted" in staleness.accept(brain._conn, cand)
    assert items.get(brain._conn, mid)["status"] == "superseded"
    assert items.get(brain._conn, cand)["status"] == "active"

    mid2 = memory(brain, repo, "Util is trivial", files=["pkg/util.py"])
    commit(repo, "pkg/util.py", "x = 3\n", "touch")
    rep = staleness.check(brain._conn, str(repo), cfg(), Judge(
        {"Util is trivial": "CONTRADICTED: no"}))
    assert rep.flags
    assert "Kept" in staleness.keep(brain._conn, mid2)
    assert "stale" not in items.get(brain._conn, mid2)["meta"]
    assert items.get(brain._conn, rep.flags[0].candidate_id)["status"] == "undone"


def test_judge_error_never_flags_and_unrelated_is_ignored(env):
    brain, repo = env
    memory(brain, repo, "Locking uses RLock", files=["pkg/lock.py"])
    baseline(brain, repo)
    commit(repo, "pkg/lock.py", "def acquire():\n    return 1\n", "c")

    def boom(_):
        raise RuntimeError("down")
    assert staleness.check(brain._conn, str(repo), cfg(), boom).flags == []


def test_failing_tests_are_passed_to_the_judge_as_evidence(env):
    brain, repo = env
    memory(brain, repo, "Locking uses RLock", files=["pkg/lock.py"])
    baseline(brain, repo)
    commit(repo, "pkg/lock.py", "def acquire():\n    return 1\n", "c")
    j = Judge()
    staleness.check(brain._conn, str(repo), cfg(), j, tests="failed: 2 failed | test_lock")
    assert "TESTS AT HEAD: failed" in j.calls[0]
    j2 = Judge()
    commit(repo, "pkg/lock.py", "def acquire():\n    return 2\n", "d")
    staleness.check(brain._conn, str(repo), cfg(), j2, tests="passed")
    assert "TESTS AT HEAD" not in j2.calls[0]


def test_run_tests_reports_pass_fail_and_skips_dirty_trees(env):
    brain, repo = env
    c = cfg(run_tests=True, test_command="python -c pass")
    assert staleness.run_tests(str(repo), c) == "passed"
    assert staleness.run_tests(str(repo), cfg(run_tests=True, test_command="python -c 1/0")
                               ).startswith("failed")
    (repo / "dirty.txt").write_text("x")
    assert staleness.run_tests(str(repo), c).startswith("skipped")


def test_legacy_conclusion_is_flagged_in_its_own_metadata(env):
    brain, repo = env
    ecu = brain.insert_ecu({
        "cognition": "RLock is required for re-entrant acquire", "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": f"repo:{repo.name}"},
        "grounding": {"files": ["pkg/lock.py"], "repo_path": str(repo)}})
    baseline(brain, repo)
    commit(repo, "pkg/lock.py", "def acquire():\n    return Lock()\n", "switch")
    rep = staleness.check(brain._conn, str(repo), cfg(), Judge(default="CONTRADICTED: switched"))
    assert rep.flags and rep.flags[0].memory_id == ecu
    row = brain._conn.execute("SELECT status, metadata_json FROM ecus WHERE id=?", (ecu,)).fetchone()
    assert row["status"] == "active" and "stale" in json.loads(row["metadata_json"])
    assert "Accepted" in staleness.accept(brain._conn, rep.flags[0].candidate_id)
    assert brain._conn.execute("SELECT status FROM ecus WHERE id=?", (ecu,)).fetchone()[0] == "superseded"


def test_tests_are_off_by_default_and_the_judge_is_the_evidence(env):
    from reverie import config as cfgmod
    assert cfgmod.get_config().stale.run_tests is False
    assert staleness.run_tests("/nonexistent", cfgmod.get_config()) == "skipped"
