import subprocess
import time

from reverie import deliver, items, usage


def _setup(tmp_path, monkeypatch, n=2):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("REVERIE_HOME", str(home))
    monkeypatch.delenv("REVERIE_NO_USAGE_LOG", raising=False)
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    conn = items.connect(home / "reverie.db")
    key = deliver.repo_key(repo)
    ids = [items.add(conn, text=t, kind="rule", source="user_said", repo_key=key)
           for t in ("Always use logging, never print()", "Ask before adding a dependency")[:n]]
    return repo, conn, ids


def test_deliveries_are_logged_by_every_channel(tmp_path, monkeypatch):
    repo, conn, (a, b) = _setup(tmp_path, monkeypatch)
    conn.execute("CREATE TABLE IF NOT EXISTS repos (repo_key TEXT PRIMARY KEY, state TEXT)")
    conn.commit()
    deliver.startup_text(str(repo))
    got = usage.delivered_between(repo, time.time() - 60, time.time() + 60)
    assert set(got) == {a, b}


def test_labels_are_parsed_and_counted_and_a_repeat_offender_is_challenged(tmp_path, monkeypatch):
    repo, conn, (a, b) = _setup(tmp_path, monkeypatch)
    usage.log_delivery(repo, "start", [a, b])
    def by_text(prompt):          # label by what the item says, not by its position
        out = []
        for line in prompt.splitlines():
            if line[:1].isdigit() and "logging" in line:
                out.append(f"{line.split('.')[0]}=CONTRADICTED")
            elif line[:1].isdigit() and "dependency" in line:
                out.append(f"{line.split('.')[0]}=IRRELEVANT")
        return " ".join(out)
    for _ in range(2):
        out = usage.judge_exchange(
            repo, conn, prompt="add a debug helper", reply="done", epoch=time.time(),
            judge=by_text)
    assert out == {a: "CONTRADICTED", b: "IRRELEVANT"}
    ia, ib = items.get(conn, a), items.get(conn, b)
    assert ia["meta"]["usage"] == {"CONTRADICTED": 2} and ia["meta"]["challenged"] is True
    assert ib["meta"]["usage"] == {"IRRELEVANT": 2} and "challenged" not in ib["meta"]
    assert "agents contradicted this repeatedly" in deliver._line({**ia, "kind": "rule"})
    rows = usage.report(conn, deliver.repo_key(repo))
    assert rows[0]["id"] in (a, b) and any(r["challenged"] for r in rows)


def test_used_outweighs_contradicted_and_overridden_is_not_a_strike(tmp_path, monkeypatch):
    repo, conn, (a, _b) = _setup(tmp_path, monkeypatch)
    usage.log_delivery(repo, "start", [a])
    for label in ("USED", "USED", "CONTRADICTED", "CONTRADICTED", "OVERRIDDEN"):
        usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=time.time(),
                             judge=lambda p, label=label: f"1={label}")
    assert "challenged" not in items.get(conn, a)["meta"]    # 2 contradicted is not > 2 used


def test_judge_failures_and_garbage_record_nothing(tmp_path, monkeypatch):
    repo, conn, (a, _b) = _setup(tmp_path, monkeypatch)
    usage.log_delivery(repo, "start", [a])
    boom = lambda p: (_ for _ in ()).throw(RuntimeError("down"))     # noqa: E731
    assert usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=time.time(), judge=boom) == {}
    assert usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=time.time(),
                                judge=lambda p: "no idea") == {}
    assert usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=time.time(), judge=None) == {}
    assert "usage" not in items.get(conn, a)["meta"]
    assert usage.parse_labels("1=used 9=USED 2=maybe", 2) == {1: "USED"}


def test_deliveries_outside_the_window_are_not_attributed(tmp_path, monkeypatch):
    repo, conn, (a, _b) = _setup(tmp_path, monkeypatch)
    usage.log_delivery(repo, "start", [a])
    assert usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=time.time() + 7200,
                                judge=lambda p: "1=USED") == {}


def test_a_past_exchange_only_gets_deliveries_made_during_it(tmp_path, monkeypatch):
    """Found on a real run: an imported exchange was credited with deliveries made up to
    30 minutes after it ended, so unrelated later deliveries inflated the counts."""
    repo, conn, (a, b) = _setup(tmp_path, monkeypatch)
    now = time.time()
    usage.log_delivery(repo, "start", [a, b])            # delivered NOW
    past = now - 1200                                    # an exchange that ended 20 min ago
    assert usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=past,
                                judge=lambda p: "1=USED 2=USED", epoch_end=past + 30) == {}
    got = usage.judge_exchange(repo, conn, prompt="p", reply="r", epoch=now - 5,
                               judge=lambda p: "1=USED 2=USED", epoch_end=now + 5)
    assert len(got) == 2
