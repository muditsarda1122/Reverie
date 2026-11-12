"""Command blocks hold one action each, and personal-layer paths read ~/.reverie/... whatever the folder is called."""

import re
import subprocess
import threading
from pathlib import Path

import pytest
from test_ui_writes import _http

from reverie import cli, context, deliver, items, ui

OPPOSITE_VERBS = [{"accept", "reject"}, {"enable", "disable"}, {"install", "uninstall"}, {"promote", "demote"}]


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", "lead@acme.com"), ("user.name", "Lee Lead")):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "elsewhere" / "my reverie"))        # not literally ~/.reverie, not named "home"
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    repo = _repo(tmp_path / "svc")
    monkeypatch.chdir(repo)
    assert cli.main(["enable"]) == 0
    return tmp_path, repo


@pytest.fixture
def serve():
    started = []

    def go(root):
        server = ui.make_server(str(root), 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server
    yield go
    for s in started:
        s.shutdown()
        s.server_close()


def _blocks(body: str) -> list[str]:
    """The text of every command block: a <pre> that is not the verbatim start text, and the single-line code.cmd boxes."""
    out = [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<pre(?![^>]*id=\"start\")[^>]*>(.*?)</pre>", body, re.S)]
    out += re.findall(r'<code class="cmd[^"]*">(.*?)</code>', body, re.S)
    return [b.replace("&amp;", "&").replace("&quot;", '"') for b in out]


def _reverse_pair(block: str) -> str:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("reverie ")]
    for a in lines:
        for flag in ("--restore", "--demote"):
            if a.endswith(" " + flag) and a[: -len(flag)].strip() in lines:
                return f"{a!r} undoes another line of the same block"
    verbs = [w for ln in lines for w in [next((x for x in ln.split()[1:] if x != "context"), "")] if w]
    for pair in OPPOSITE_VERBS:
        if pair <= set(verbs):
            return f"holds {sorted(pair)} together"
    return ""


def _all_pages(s, repo, tmp_path):
    pid = ui._pid(str(repo))
    mem = {}
    conn = items.connect(deliver.db_path())
    key = deliver.repo_key(str(repo))
    mem["rule"] = items.add(conn, repo_key=key, text="Never run migrations on the replica", kind="rule", source="user_said")
    mem["dec"] = items.add(conn, repo_key=key, text="Refunds need approval", kind="decision", source="user_said",
                           meta={"stale": {"commit": "fedcba9876", "message": "", "reason": "gone"}})
    conn.close()
    context.add(str(repo), "Prefer small PRs", why="faster reviews")                         # a proposal (lead is no owner here)
    pages = ["/project", "/proposals", "/context", "/memory", "/start", "/add", "/connect", "/new", "/"]
    bodies = {u: _http(s, "GET", u if u == "/" else f"{u}?p={pid}")[1] for u in pages}
    for k, i in mem.items():
        bodies[f"rule-{k}"] = _http(s, "GET", f"/rule?p={pid}&id={i[:8]}")[1]
    return bodies


def test_no_command_block_holds_a_command_and_its_reverse(env, serve):
    tmp_path, repo = env
    (repo / "CODEOWNERS").write_text("/.reverie/context/ lead@acme.com\n")                  # the viewer owns it: forms and commands show
    s = serve(repo)
    bodies = _all_pages(s, repo, tmp_path)
    seen = 0
    for url, body in bodies.items():
        for block in _blocks(body):
            seen += 1
            assert not _reverse_pair(block), (url, block)
    assert seen >= 8
    rid = next(b for u, b in bodies.items() if u == "rule-rule")
    assert re.search(r'<pre class="block-cmd">reverie undo [0-9a-f]{8}</pre>', rid)
    assert re.search(r"Forget it. Bring it back with <code>reverie undo [0-9a-f]{8} --restore</code>.", rid)
    assert re.search(r'<pre class="block-cmd">reverie promote [0-9a-f]{8}</pre>', rid)
    assert re.search(r"Take it back with <code>reverie promote [0-9a-f]{8} --demote</code>", rid)


def test_the_checker_catches_a_block_that_combines_opposites():
    assert _reverse_pair("reverie undo ab12\nreverie undo ab12 --restore")
    assert _reverse_pair("reverie promote ab12\nreverie promote ab12 --demote")
    assert _reverse_pair("reverie enable\nreverie disable")
    assert _reverse_pair("reverie context accept s\nreverie context reject s --why x")
    assert not _reverse_pair("reverie undo ab12")


def test_personal_layer_paths_read_tilde_reverie_whatever_the_home_folder_is_called(env, serve):
    tmp_path, repo = env
    (repo / ".reverie" / "context").mkdir(parents=True)
    (repo / ".reverie" / "context" / "rules.md").write_text("## Rules\n- Money is integer cents\n")
    context.add(str(repo), "Money is integer cents", layer="personal")                      # an exact personal copy: hidden
    s = serve(repo)
    pid = ui._pid(str(repo))
    page = _http(s, "GET", f"/context?p={pid}")[1]
    assert 'title="~/.reverie/context/rules.md"' in page
    assert "home/context" not in page and "my reverie" not in page and str(tmp_path) not in page
    hidden_id = re.search(r"Hidden: same as a team rule.*?/rule\?p=[0-9a-f]{8}&amp;id=([0-9A-Za-z_-]+)", page, re.S)
    ids = re.findall(r'/rule\?p=[0-9a-f]{8}&amp;id=([0-9A-Za-z_-]+)', page)
    seen_personal = False
    for rid in dict.fromkeys(ids):
        body = _http(s, "GET", f"/rule?p={pid}&id={rid}")[1]
        assert "my reverie" not in body and "home/context" not in body and str(tmp_path) not in body, rid
        if "~/.reverie/context/rules.md" in body:
            seen_personal = True
    assert seen_personal and hidden_id is not None
    assert ui._under_home(Path(ui.spool.reverie_home()) / "rules.md") == "~/.reverie/rules.md"
    assert ui._under_home(tmp_path / "svc" / "x.md") == ""
