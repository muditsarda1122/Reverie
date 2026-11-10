"""`reverie ui` writes: add a rule, accept/reject a proposal. Every refusal changes nothing;
every write calls the CLI's own function and is exactly as strict as the CLI."""

import hashlib
import http.client
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest

from reverie import cli, context, ui

KEY = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HOSTILE = "<script>alert(1)</script> <img src=x onerror=alert(2)> \"q\" & more"


def _repo(path: Path, email: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", email), ("user.name", "Dana Dev")):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    (path / "CODEOWNERS").write_text("/.reverie/context/ lead@acme.com\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    return tmp_path, _repo(tmp_path / "svc", "dev@acme.com")     # dev is NOT an owner


def _owner(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "lead@acme.com"], check=True)


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


def _tree(*roots) -> dict:
    snap = {}
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            if p.is_file() and ".git/" not in str(p) and not p.name.endswith(("-shm", "-wal")):
                snap[str(p)] = hashlib.sha1(p.read_bytes()).hexdigest()
    return snap


def _http(s, method, path, fields=None, *, host=None, headers=None, body=None, same_origin=True):
    port = s.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    h = {}
    if method == "POST":
        data = body if body is not None else urlencode(fields or {}).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(data))}
        if same_origin:
            h["Origin"] = f"http://127.0.0.1:{port}"
    h.update(headers or {})
    for k, v in h.items():
        if v is not None:
            c.putheader(k, v)
    c.endheaders(data if method == "POST" else None)
    r = c.getresponse()
    out = (r.status, r.read().decode("utf-8"), dict(r.getheaders()))
    c.close()
    return out


def _form(s, path):
    """The real forms on a page: (token, project id) scraped as a browser would."""
    body = _http(s, "GET", path)[1]
    return (re.search(r'name=token value="([^"]+)"', body).group(1),
            re.search(r'name=p value="([0-9a-f]{8})"', body).group(1))


def _follow(s, resp):
    status, _, hdr = resp
    assert status == 303, resp[1]
    return _http(s, "GET", hdr["Location"])[1]


def _add(s, text, **extra):
    tok, pid = _form(s, "/add")
    return _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": text, "kind": "rule",
                                        "target": "repo", "why": "because", **extra})


def _propose_as_dev(repo, text="Prefer small PRs"):
    context.add(str(repo), text, why="reviews are faster")
    return context.proposals(str(repo))[0]


# --- the refusals: every one changes nothing ------------------------------------------------------

def test_forged_origin_and_fetch_site_are_refused(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/add")
    fields = {"token": tok, "p": pid, "text": "Never push to main", "target": "repo"}
    before = _tree(tmp_path)
    cases = [
        dict(headers={"Origin": "http://evil.example"}, same_origin=False),
        dict(headers={"Origin": "null"}, same_origin=False),
        dict(headers={"Origin": f"http://127.0.0.1:{s.server_address[1] + 1}"}, same_origin=False),
        dict(headers={"Sec-Fetch-Site": "cross-site"}, same_origin=True),          # right Origin, wrong site
        dict(headers={"Sec-Fetch-Site": "same-site"}, same_origin=False),
        dict(headers={"Sec-Fetch-Site": "none"}, same_origin=False),
        dict(headers={}, same_origin=False),                                       # neither header
    ]
    for kw in cases:
        status, body, _ = _http(s, "POST", "/do/add", fields, **kw)
        assert status == 403 and "Never push" not in body, kw
    assert _tree(tmp_path) == before


def test_sec_fetch_site_alone_is_enough_when_origin_is_absent(env, serve):
    _, repo = env
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/add")
    status, body, _ = _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "Rule A", "target": "repo"},
                            headers={"Sec-Fetch-Site": "same-origin"}, same_origin=False)
    assert status == 303, body


@pytest.mark.parametrize("token", [None, "", "wrong", "x" * 43])
def test_missing_or_wrong_token_is_refused(env, serve, token):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    _, pid = _form(s, "/add")
    before = _tree(tmp_path)
    fields = {"p": pid, "text": "Rule A", "target": "repo"}
    if token is not None:
        fields["token"] = token
    assert _http(s, "POST", "/do/add", fields)[0] == 403
    assert _tree(tmp_path) == before


def test_a_token_from_another_process_is_refused(env, serve):
    tmp_path, repo = env
    _owner(repo)
    a, b = serve(repo), serve(repo)
    tok_a, pid = _form(a, "/add")
    tok_b, _ = _form(b, "/add")
    assert tok_a != tok_b
    before = _tree(tmp_path)
    assert _http(b, "POST", "/do/add", {"token": tok_a, "p": pid, "text": "Rule", "target": "repo"})[0] == 403
    assert _tree(tmp_path) == before


def test_wrong_host_is_refused_on_post(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    for host in ("evil.example", "127.0.0.1:1", "localhost"):
        assert _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "R", "target": "repo"}, host=host)[0] == 403
    assert _tree(tmp_path) == before


def test_get_on_write_paths_has_no_side_effect(env, serve):
    tmp_path, repo = env
    prop = _propose_as_dev(repo)
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    q = urlencode({"token": tok, "p": pid, "text": "Sneaky", "target": "repo", "slug": prop["slug"], "why": "x"})
    for path in WRITE:
        for m in ("GET", "HEAD"):
            assert _http(s, m, f"{path}?{q}")[0] == 404, (m, path)
    assert _tree(tmp_path) == before


WRITE = ("/do/add", "/do/accept", "/do/reject")


def test_bad_bodies_are_refused(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    good = urlencode({"token": tok, "p": pid, "text": "R", "target": "repo"}).encode()
    assert _http(s, "POST", "/do/add", body=good, headers={"Content-Type": "text/plain"})[0] == 415
    assert _http(s, "POST", "/do/add", body=good, headers={"Content-Type": "application/json"})[0] == 415
    assert _http(s, "POST", "/do/add", body=b"x" * 20000)[0] == 413
    assert _http(s, "POST", "/do/add", body=good, headers={"Content-Length": None})[0] == 411
    assert _http(s, "POST", "/do/nothing", body=good)[0] == 405
    assert _http(s, "POST", "/", body=good)[0] == 405
    for m in ("PUT", "DELETE", "PATCH"):
        assert _http(s, m, "/do/add")[0] == 405
    assert _tree(tmp_path) == before


def test_unknown_project_id_writes_nothing(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    tok, _ = _form(s, "/add")
    before = _tree(tmp_path)
    for pid in ("deadbeef", str(repo), "../../etc"):
        r = _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "R", "target": "repo"})
        assert r[0] == 303
    assert _tree(tmp_path) == before


def test_a_target_the_project_does_not_offer_is_refused(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    before = _tree(tmp_path)
    for target in ("personal", "org|../../etc", "team|nope", "../x"):
        page = _follow(s, _add(s, "Rule A", target=target))
        assert "Refused" in page, target
    assert _tree(tmp_path) == before


def test_a_disabled_project_refuses_writes(env, serve, monkeypatch):
    tmp_path, repo = env
    _owner(repo)
    monkeypatch.chdir(repo)
    assert cli.main(["enable"]) == 0
    s = serve(repo)
    tok, pid = _form(s, "/add")
    assert cli.main(["disable"]) == 0
    before = _tree(tmp_path)
    r = _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "R", "target": "repo"})
    assert r[0] == 303 and _tree(tmp_path) == before
    assert "turned off" in _http(s, "GET", r[2]["Location"])[1]


# --- secrets ------------------------------------------------------------------------------------

def test_a_secret_is_refused_and_never_echoed(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    before = _tree(tmp_path)
    resp = _add(s, f"Use token {KEY} for CI")
    assert KEY not in resp[2]["Location"] and KEY not in resp[1]
    page = _follow(s, resp)
    assert "secret" in page and KEY not in page and "sk-ant" not in page
    assert re.search(r"<textarea[^>]*></textarea>", page)               # the field comes back EMPTY
    resp = _add(s, "Fine rule", why=f"key {KEY}")
    page = _follow(s, resp)
    assert "secret" in page and KEY not in page
    assert _tree(tmp_path) == before


def test_a_secret_in_a_rejection_reason_is_refused(env, serve):
    tmp_path, repo = env
    prop = _propose_as_dev(repo)
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/proposals")
    before = _tree(tmp_path)
    page = _follow(s, _http(s, "POST", "/do/reject", {"token": tok, "p": pid, "slug": prop["slug"], "why": f"k {KEY}"}))
    assert "secret" in page and KEY not in page
    assert _tree(tmp_path) == before


# --- the writes: through the CLI's functions, exactly one thing changes ----------------------------

def test_owner_add_changes_exactly_one_file(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/add")                       # (a GET has side effects on nothing: baseline after it)
    before = _tree(tmp_path)
    page = _follow(s, _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "  Always   squash merge ",
                                                   "kind": "rule", "target": "repo", "why": "linear"}))
    after = _tree(tmp_path)
    changed = sorted(k for k in set(after) | set(before) if after.get(k) != before.get(k))
    assert changed == [str(repo / ".reverie" / "context" / "rules.md")]
    assert "Added to rules.md" in page
    assert "- Always squash merge" in (repo / ".reverie/context/rules.md").read_text()


def test_non_owner_add_becomes_one_proposal_file(env, serve):
    tmp_path, repo = env
    s = serve(repo)
    _form(s, "/add")
    before = _tree(tmp_path)
    page = _follow(s, _add(s, "Prefer small PRs"))
    after = _tree(tmp_path)
    new = sorted(set(after) - set(before))
    assert len(new) == 1 and "/proposals/" in new[0] and set(before) <= set(after)
    assert all(after[k] == before[k] for k in before)
    assert "proposal" in page and "lead@acme.com" in page
    assert not (repo / ".reverie/context/rules.md").exists()


def test_guidance_and_hostile_text_are_stored_and_rendered_escaped(env, serve):
    tmp_path, repo = env
    _owner(repo)
    s = serve(repo)
    page = _follow(s, _add(s, HOSTILE, kind="guidance", why=HOSTILE))
    assert "<script" not in page.lower() and "<img src=x" not in page
    stored = (repo / ".reverie/context/guidance.md").read_text()
    assert "<script>alert(1)</script>" in stored          # stored as the CLI would store it
    for path in ("/context", "/add", "/project", "/proposals"):
        body = _http(s, "GET", path)[1]
        assert "<script" not in body.lower() and "<img src=x" not in body
    assert "&lt;script&gt;" in _http(s, "GET", "/context")[1]


def test_a_refused_add_keeps_a_non_secret_text_for_editing(env, serve):
    _, repo = env
    _owner(repo)
    s = serve(repo)
    page = _follow(s, _add(s, "Keep me", target="personal"))
    assert "Refused" in page and ">Keep me</textarea>" in page


def test_owner_accept_moves_the_proposal_into_the_document(env, serve):
    tmp_path, repo = env
    prop = _propose_as_dev(repo)
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/proposals")
    before = _tree(tmp_path)
    page = _follow(s, _http(s, "POST", "/do/accept", {"token": tok, "p": pid, "slug": prop["slug"]}))
    after = _tree(tmp_path)
    changed = sorted(k for k in set(after) | set(before) if after.get(k) != before.get(k))
    assert changed == sorted([str(prop["path"]), str(repo / ".reverie/context/rules.md")])
    assert str(prop["path"]) not in after and "Accepted into rules.md" in page
    assert "Prefer small PRs" in (repo / ".reverie/context/rules.md").read_text()
    assert context.proposals(str(repo)) == []


def test_non_owner_cannot_accept_or_reject(env, serve):
    tmp_path, repo = env
    prop = _propose_as_dev(repo)                         # dev is not an owner
    s = serve(repo)
    tok, pid = _form(s, "/proposals")
    before = _tree(tmp_path)
    page = _follow(s, _http(s, "POST", "/do/accept", {"token": tok, "p": pid, "slug": prop["slug"]}))
    assert "Refused" in page and "only the owners of rules.md can accept" in page and "lead@acme.com" in page
    page = _follow(s, _http(s, "POST", "/do/reject", {"token": tok, "p": pid, "slug": prop["slug"], "why": "no"}))
    assert "Refused" in page and "only the owners of rules.md can reject" in page
    assert _tree(tmp_path) == before and context.proposals(str(repo))
    assert not (repo / ".reverie/context/rules.md").exists()


def test_reject_needs_a_reason_and_keeps_it(env, serve):
    tmp_path, repo = env
    prop = _propose_as_dev(repo)
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/proposals")
    before = _tree(tmp_path)
    for why in ("", "   "):
        page = _follow(s, _http(s, "POST", "/do/reject", {"token": tok, "p": pid, "slug": prop["slug"], "why": why}))
        assert "Refused" in page and "say why" in page
    assert _tree(tmp_path) == before
    page = _follow(s, _http(s, "POST", "/do/reject", {"token": tok, "p": pid, "slug": prop["slug"],
                                                      "why": "we squash already"}))
    assert "Rejected" in page
    assert context.proposals(str(repo)) == []
    assert [r.get("reason") for r in context.rejected(str(repo))] == ["we squash already"]
    assert "we squash already" in _http(s, "GET", "/proposals")[1]


def test_an_unknown_or_ambiguous_slug_changes_nothing(env, serve):
    tmp_path, repo = env
    _propose_as_dev(repo)
    _owner(repo)
    s = serve(repo)
    tok, pid = _form(s, "/proposals")
    before = _tree(tmp_path)
    for slug in ("nope", "../../etc/passwd", ""):
        page = _follow(s, _http(s, "POST", "/do/accept", {"token": tok, "p": pid, "slug": slug}))
        assert "Refused" in page
    assert _tree(tmp_path) == before


# --- post/redirect/get ---------------------------------------------------------------------------

def test_every_write_redirects_and_the_message_is_shown_once(env, serve):
    _, repo = env
    _owner(repo)
    s = serve(repo)
    resp = _add(s, "Rule once")
    assert resp[0] == 303 and urlsplit(resp[2]["Location"]).path == "/add"
    first = _http(s, "GET", resp[2]["Location"])[1]
    again = _http(s, "GET", resp[2]["Location"])[1]
    assert "Added to rules.md" in first and "Added to rules.md" not in again   # one-shot
    for _ in range(3):                                                         # a refresh repeats nothing
        _http(s, "GET", resp[2]["Location"])
    assert (repo / ".reverie/context/rules.md").read_text().count("Rule once") == 1


# --- identity, parity, judge ---------------------------------------------------------------------

def test_screens_show_the_git_identity_the_action_will_use(env, serve):
    _, repo = env
    prop = _propose_as_dev(repo)
    s = serve(repo)
    add = _http(s, "GET", "/add")[1]
    assert "Dana Dev &lt;dev@acme.com&gt;" in add
    props = _http(s, "GET", "/proposals")[1]
    assert "Dana Dev &lt;dev@acme.com&gt;" in props and "Not an owner" in props and prop["slug"] in props
    _owner(repo)
    props = _http(s, "GET", "/proposals")[1]
    assert "lead@acme.com" in props and "You can decide this" in props


def test_ui_and_cli_write_identical_files(tmp_path, monkeypatch, serve):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    for owner in (True, False):
        a = _repo(tmp_path / f"ui{owner}", "lead@acme.com" if owner else "dev@acme.com")
        b = _repo(tmp_path / f"cli{owner}", "lead@acme.com" if owner else "dev@acme.com")
        _add(serve(a), "Always squash merge", why="linear")
        monkeypatch.chdir(b)
        assert cli.main(["context", "add", "Always squash merge", "--why", "because"]) == 0
        ra = sorted((a / ".reverie/context").rglob("*.md"))
        rb = sorted((b / ".reverie/context").rglob("*.md"))
        assert [p.relative_to(a) for p in ra] == [p.relative_to(b) for p in rb]
        for x, y in zip(ra, rb):
            assert x.read_text().replace("linear", "because") == y.read_text()


def test_the_ui_never_runs_the_judge(env, serve, monkeypatch):
    _, repo = env
    prop = _propose_as_dev(repo)
    _owner(repo)
    monkeypatch.delenv("REVERIE_NO_BACKGROUND", raising=False)
    from reverie import context_check

    def boom(*a, **k):
        raise AssertionError("the UI ran the judge")
    monkeypatch.setattr(context_check, "auto_check", boom)
    monkeypatch.setattr(context_check, "_cfg_judge", boom)
    monkeypatch.setattr(cli, "_context_detect", boom)
    s = serve(repo)
    assert "Added" in _follow(s, _add(s, "Rule judged later"))
    tok, pid = _form(s, "/proposals")
    assert "Accepted" in _follow(s, _http(s, "POST", "/do/accept", {"token": tok, "p": pid, "slug": prop["slug"]}))


def test_headers_forbid_framing_and_cross_origin_forms(env, serve):
    _, repo = env
    s = serve(repo)
    _, body, hdr = _http(s, "GET", "/add")
    assert "form-action 'self'" in hdr["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in hdr["Content-Security-Policy"] and hdr["X-Frame-Options"] == "DENY"
    assert "<script" not in body.lower() and "cookie" not in {k.lower() for k in hdr}
    for m in re.findall(r'<form[^>]*method=post[^>]*>', body):
        assert re.search(r'action="/do/\w+"', m)
