"""One request asks git once per answer; nothing is remembered across requests."""

import subprocess

import pytest

from reverie import cli, spool, ui


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    r = tmp_path / "svc"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    for k, v in (("user.email", "dev@acme.com"), ("user.name", "Dana Dev")):
        subprocess.run(["git", "-C", str(r), "config", k, v], check=True)
    monkeypatch.chdir(r)
    assert cli.main(["enable"]) == 0
    return r


def _count(monkeypatch):
    calls = []
    real = subprocess.run

    def counting(cmd, *a, **k):
        if cmd and cmd[0] == "git":
            calls.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", counting)
    return calls


def test_git_is_remembered_only_inside_a_block(repo, monkeypatch):
    calls = _count(monkeypatch)
    spool._git(["config", "user.name"], repo)
    spool._git(["config", "user.name"], repo)
    assert len(calls) == 2                                        # outside a block: asked every time
    calls.clear()
    with spool.git_memo():
        assert spool._git(["config", "user.name"], repo) == "Dana Dev"
        spool._git(["config", "user.name"], repo)
        spool._git(["config", "user.email"], repo)
        assert len(calls) == 2                                    # one per distinct question
    spool._git(["config", "user.name"], repo)
    assert len(calls) == 3                                        # the block's answers are gone


def test_a_page_asks_git_far_less_and_a_changed_identity_shows_on_the_next_load(repo, monkeypatch):
    calls = _count(monkeypatch)
    page = ui.render(str(repo), "/add", {})[1]
    first = len(calls)
    assert 'title="dev@acme.com">Dana Dev' in page
    assert first <= 12, calls                                     # was 29 for this page before the per-request memory
    assert len(set(map(tuple, calls))) == first                   # nothing asked twice in one request
    for k, v in (("user.email", "lee@acme.com"), ("user.name", "Lee Lead")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    assert 'title="lee@acme.com">Lee' in ui.render(str(repo), "/add", {})[1]      # the next request sees the change
    assert spool._git_memo.get() is None                          # and nothing outlives a request
