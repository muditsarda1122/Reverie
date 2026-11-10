"""Scoped team context: parsing, containment, conflict statuses, paths, authority."""

import subprocess
from pathlib import Path

import pytest

from reverie import context, context_check, deliver


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=t",
                    *a], check=True, capture_output=True)


def _repo(path: Path, email="dev@acme.com", name="Dev") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", email)
    _git(path, "config", "user.name", name)
    (path / ".reverie").mkdir(exist_ok=True)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "payments-api")
    org = tmp_path / "acme" / "org"
    team = tmp_path / "acme" / "payments"
    org.mkdir(parents=True)
    team.mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text(
        '[sources]\norg = "../acme/org"\nteam = "../acme/payments"\n')
    return repo, org, team


def _texts(items):
    return [i.text for i in items]


# --- format ---------------------------------------------------------------

def test_parse_rules_guidance_tags_headers():
    text = ("applies-to: payments-api, billing\napplies-to-paths: services/billing/**\n"
            "owner: Legal, ask Dana\n# Eng\n\n## Rules\n\n"
            "- Never log card numbers {id: no-pii} (locked)\n"
            "- Use the shared retry helper\n  for network calls\n"
            "- Ship via the gate {resolves: c-1234}\n\n"
            "## How we deploy\n\n- Deploys go through the staging gate\n")
    items = context.parse_document(text, "org", "eng.md", "acme/org")
    assert [i.kind for i in items] == ["rule", "rule", "rule", "guidance"]
    first = items[0]
    assert first.locked and first.tag == "no-pii" and first.text == "Never log card numbers"
    assert items[1].text == "Use the shared retry helper for network calls"
    assert items[2].resolves == "c-1234" and not items[2].locked
    assert first.applies_to == ("payments-api", "billing")
    assert first.paths == ("services/billing/**",)
    assert first.owner == "Legal, ask Dana"


def test_glob_matching():
    assert context.glob_match("services/billing/**", "services/billing/a/b.py")
    assert not context.glob_match("services/billing/**", "services/other/b.py")
    assert context.glob_match("*.md", "docs/readme.md")
    assert context.glob_match("/docs/", "docs/x/y.md")
    assert not context.glob_match("/docs/", "src/docs/y.md")


def test_several_orgs_and_teams_are_separate_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "shared-repo")
    for d in ("acme/org", "globex/org", "acme/a", "acme/b"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text(
        '[sources]\norg = ["../acme/org", "../globex/org"]\nteam = ["../acme/a", "../acme/b"]\n')
    for d in ("acme/org", "globex/org", "acme/a", "acme/b"):
        (tmp_path / d / "r.md").write_text(f"## Rules\n- rule from {d}\n")
    items = context.load_items(str(repo))
    assert len(items) == 4 and len({i.scope for i in items}) == 4


# --- nested scopes: the narrower answers the wider -------------------------

def test_nested_narrower_overrides_wider_and_both_are_shown(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Use tabs {id: indent}\n")
    context.add(str(repo), "Use spaces", tag="indent")
    res = context.resolve(str(repo))
    c = res.conflicts[0]
    assert c.status == "overridden" and c.winner and c.winner.text == "Use spaces"
    assert _texts(res.effective) == ["Use spaces"] and len(res.items) == 2
    text = context.render(str(repo))
    assert "overridden" in text and "Use tabs" in text and "Use spaces" in text


def test_wider_lock_beats_narrower(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Never log PII {id: pii} (locked)\n")
    context.add(str(repo), "Logging PII is fine", tag="pii")
    c = context.resolve(str(repo)).conflicts[0]
    assert c.status == "locked" and c.winner and c.winner.layer == "org"
    assert "do not follow the lower one" in context.render(str(repo))


# --- sibling scopes: no order, unresolved, agent asks ----------------------

def test_two_teams_in_one_repo_unresolved_and_agent_told_to_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r")
    for d in ("co/a", "co/b"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text(
        '[sources]\nteam = ["../co/a", "../co/b"]\n')
    (tmp_path / "co/a/r.md").write_text("owner: Team A lead\n## Rules\n- Use tabs {id: indent}\n")
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Use spaces {id: indent}\n")
    res = context.resolve(str(repo))
    c = res.conflicts[0]
    assert c.status == "unresolved" and c.winner is None
    assert res.effective == []                        # neither applies silently
    text = context.render(str(repo))
    assert "UNRESOLVED" in text and "ask the user" in text
    assert "Team A lead" in text and "reverie context resolve" in text


def test_two_orgs_with_conflicting_locks_are_unresolved(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r")
    for d in ("acme/org", "globex/org"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text(
        '[sources]\norg = ["../acme/org", "../globex/org"]\n')
    (tmp_path / "acme/org/r.md").write_text("## Rules\n- Always use REST {id: api} (locked)\n")
    (tmp_path / "globex/org/r.md").write_text("## Rules\n- Always use gRPC {id: api} (locked)\n")
    c = context.resolve(str(repo)).conflicts[0]
    assert c.status == "unresolved" and "two locks" in c.reason
    assert "ask the user" in context.render(str(repo))


def test_resolution_is_remembered_and_settles_the_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r")
    for d in ("acme/org", "globex/org"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text(
        '[sources]\norg = ["../acme/org", "../globex/org"]\n')
    (tmp_path / "acme/org/r.md").write_text("## Rules\n- Always use REST {id: api} (locked)\n")
    (tmp_path / "globex/org/r.md").write_text("## Rules\n- Always use gRPC {id: api} (locked)\n")
    key = context.resolve(str(repo)).conflicts[0].key
    context.add(str(repo), "Public endpoints use REST, internal ones gRPC", resolves=key)
    res = context.resolve(str(repo))          # a fresh read: the decision lives in the repo
    c = res.conflicts[0]
    assert c.status == "overridden" and c.winner and "internal ones gRPC" in c.winner.text
    assert "resolved by" in c.reason
    assert _texts(res.effective) == ["Public endpoints use REST, internal ones gRPC"]
    assert "UNRESOLVED" not in context.render(str(repo))


# --- path scoping ----------------------------------------------------------

def test_path_scoped_rule_applies_only_under_its_path(env):
    repo, _, _ = env
    context.add(str(repo), "Amounts are integer cents", paths=("services/billing/**",))
    items = context.load_items(str(repo))
    assert items[0].paths == ("services/billing/**",)
    assert _texts(context.resolve(str(repo), path="services/billing/x.py").effective) \
        == ["Amounts are integer cents"]
    assert context.resolve(str(repo), path="services/other/x.py").effective == []
    text, ids = context.point_text(str(repo), "services/billing/x.py")
    assert "integer cents" in text and ids
    assert context.point_text(str(repo), "docs/a.md")[0] == ""
    # the start block lists it as path-scoped, never as a repo-wide rule
    start = context.render(str(repo))
    assert "ONLY to files under the path" in start and "services/billing/**" in start


def test_point_of_action_delivers_path_rules(env):
    repo, _, _ = env
    context.add(str(repo), "Amounts are integer cents", paths=("services/billing/**",))
    (repo / "services" / "billing").mkdir(parents=True)
    f = repo / "services" / "billing" / "a.py"
    f.write_text("x = 1\n")
    out = deliver.point_of_action(str(repo), "s1", str(f))
    assert "integer cents" in out
    assert "integer cents" not in deliver.point_of_action(str(repo), "s1", str(f))  # once


def test_lock_does_not_leak_outside_its_scope(env):
    repo, _, _ = env
    # a lock on billing paths neither binds nor conflicts with a rule on other paths
    # (a sibling team's lock is covered by test_sibling_team_lock_is_not_a_winner)
    context.add(str(repo), "Use Decimal {id: money}", locked=True, paths=("services/billing/**",))
    context.add(str(repo), "Use float {id: money}", paths=("services/analytics/**",))
    res = context.resolve(str(repo))
    assert res.conflicts == [] and len(res.effective) == 2


def test_sibling_team_lock_is_not_a_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r")
    for d in ("co/a", "co/b"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/a", "../co/b"]\n')
    (tmp_path / "co/a/r.md").write_text("## Rules\n- Use tabs {id: indent} (locked)\n")
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Use spaces {id: indent}\n")
    c = context.resolve(str(repo)).conflicts[0]
    assert c.status == "unresolved"             # A's lock binds only A's scope


# --- authority: anyone contributes, owners accept --------------------------

def test_non_owner_writes_a_proposal_owner_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r", email="pm@acme.com", name="Pat")
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text(
        "# owners\n/.reverie/context/ lead@acme.com @eng-leads\n")
    added = context.add(str(repo), "Error messages never blame the user", why="support asked")
    assert added.proposal and "proposals" in str(added.path)
    assert context.load_items(str(repo)) == []          # not in force yet
    props = context.proposals(str(repo))
    assert len(props) == 1 and "support asked" in props[0]["why"]
    assert "pm@acme.com" in props[0]["proposed-by"]
    with pytest.raises(ValueError, match="only the owners"):
        context.accept(str(repo), props[0]["slug"])
    _git(repo, "config", "user.email", "lead@acme.com")
    doc = context.accept(str(repo), props[0]["slug"])
    assert _texts(context.load_items(str(repo))) == ["Error messages never blame the user"]
    assert doc.name == "rules.md" and context.proposals(str(repo)) == []


def test_owner_edits_directly_and_unowned_folders_are_open(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r", email="lead@acme.com")
    (repo / "CODEOWNERS").write_text(".reverie/context/ lead@acme.com\n")
    assert not context.add(str(repo), "Owner rule").proposal
    repo2 = _repo(tmp_path / "r2", email="anyone@x.com")
    assert not context.add(str(repo2), "Open rule").proposal      # nobody owns it


def test_authority_labels_are_display_only(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r", email="x@x.com")
    (repo / "CODEOWNERS").write_text(".reverie/context/ @dana\n")
    (repo / ".reverie" / "authority").write_text("legal = @dana\n")
    context.add(str(repo), "Rule")   # not an owner: proposal; add the doc directly for display
    folder = repo / ".reverie" / "context"
    (folder / "rules.md").write_text("## Rules\n- A rule\n")
    it = context.load_items(str(repo))[0]
    assert context.owner_text(it) == "legal (@dana)"


# --- judge-backed checks ---------------------------------------------------

def test_conflict_judge_flags_siblings_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r")
    for d in ("co/a", "co/b"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/a", "../co/b"]\n')
    (tmp_path / "co/a/r.md").write_text("## Rules\n- Always squash merge\n")
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Never squash merge\n")
    calls = []

    def judge(prompt):
        calls.append(prompt)
        return "1: opposite"

    out = context_check.find_conflicts(str(repo), judge)
    assert len(out) == 1
    n = len(calls)
    context_check.find_conflicts(str(repo), judge)
    assert len(calls) == n                              # cached by content
    res = context.resolve(str(repo))
    assert len(res.conflicts) == 1 and res.conflicts[0].status == "unresolved"
    assert "opposite" in res.conflicts[0].reason


def test_conflict_judge_none_error_and_disjoint_paths(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Use black\n")
    context.add(str(repo), "Line length 100")
    assert context_check.find_conflicts(str(repo), lambda p: "NONE") == []

    def boom(p):
        raise RuntimeError("x")
    assert context_check.find_conflicts(str(repo), boom) == []
    # rules on disjoint paths are never even compared
    context.add(str(repo), "A", paths=("a/**",))
    context.add(str(repo), "B", paths=("b/**",))
    a, b = [i for i in context.load_items(str(repo)) if i.paths]
    assert context.relation(a, b) == "disjoint"


def test_shared_doc_staleness_names_file_and_commit(env):
    repo, _, _ = env
    (repo / "retry.py").write_text("def old_helper():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    context.add(str(repo), "Use `old_helper` in retry.py for all retries")
    assert context_check.check_stale(str(repo), lambda p: "x") == []   # baseline
    (repo / "retry.py").write_text("def new_helper():\n    return 2\n")
    _git(repo, "commit", "-qam", "replace helper")
    flags = context_check.check_stale(
        str(repo), lambda p: "CONTRADICTED: old_helper was removed")
    assert len(flags) == 1 and flags[0]["file"].endswith("rules.md")
    text = context.render(str(repo))
    assert "Possibly outdated team context" in text and "old_helper was removed" in text


# --- delivery --------------------------------------------------------------

def test_guidance_is_a_pointer_and_show_works(env):
    repo, _, _ = env
    context.add(str(repo), "Deploys go through staging", rule=False)
    it = context.load_items(str(repo))[0]
    assert it.kind == "guidance"
    assert "reverie show <id>" in context.render(str(repo))
    assert "document:" in context.show(str(repo), it.id[:6])


def test_add_idempotent_sections_and_errors(env, tmp_path):
    repo, _, _ = env
    p1 = context.add(str(repo), "Rule one").path
    context.add(str(repo), "Rule one")
    context.add(str(repo), "A guide", rule=False, doc="rules")
    context.add(str(repo), "Rule two")
    assert [(i.kind, i.text) for i in context.load_items(str(repo))] == [
        ("rule", "Rule one"), ("guidance", "A guide"), ("rule", "Rule two")]
    assert p1.read_text().count("Rule one") == 1
    with pytest.raises(ValueError, match="no org source"):
        context.add(str(_repo(tmp_path / "bare")), "x", layer="org")


def test_startup_text_includes_team_rules_and_is_bounded(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n" + "\n".join(f"- rule number {i} " + "x" * 200
                                                       for i in range(80)))
    assert "rule number 0" in deliver.startup_text(str(repo))
    assert len(context.render(str(repo), budget=1500)) < 1700


def test_disabled_repo_gets_no_team_context_anywhere(env, monkeypatch):
    """Regression (found before the lab run): the edit hook delivered path-scoped
    team rules even when Reverie was disabled for the repo, which would have
    leaked into every Reverie-off control arm."""
    from reverie import cli
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Org rule alpha\n")
    context.add(str(repo), "Amounts are integer cents", paths=("services/billing/**",))
    (repo / "services" / "billing").mkdir(parents=True)
    f = repo / "services" / "billing" / "a.py"
    f.write_text("x = 1\n")
    assert "integer cents" in deliver.point_of_action(str(repo), "s0", str(f))
    monkeypatch.chdir(repo)
    assert cli.main(["disable"]) == 0
    assert deliver.point_of_action(str(repo), "s2", str(f)) == ""
    assert deliver.startup_text(str(repo)) == ""


def test_cli_context_flags_are_on_the_context_parser(env, monkeypatch, capsys):
    """Regression (found by the lab driver): --scope/--paths/--why had been added to
    the `stale` parser, so `reverie context add --why ...` failed to parse and
    `reverie context add` crashed with a missing `paths` attribute."""
    from reverie import cli
    repo, org, _ = env
    monkeypatch.chdir(repo)
    assert cli.main(["context", "add", "--why", "because", "Rule one"]) == 0
    assert cli.main(["context", "add", "Rule two", "--paths", "src/a/**"]) == 0
    assert cli.main(["context", "add", "--layer", "org", "--scope", "acme/org", "--locked",
                     "--tag", "t", "Org rule"]) == 0
    assert cli.main(["context", "proposals"]) == 0
    assert cli.main(["context", "list"]) == 0
    out = capsys.readouterr().out
    assert "Rule one" in out and "Org rule" in out and "only under src/a/**" in out


# --- detection runs off the hook path; the hint is a separate mitigation ---

def _two_teams(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "r")
    for d in ("co/a", "co/b"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/a", "../co/b"]\n')
    (tmp_path / "co/a/r.md").write_text("## Rules\n- Always squash merge\n")
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Never squash merge\n")
    _git(repo, "commit", "--allow-empty", "-qm", "init")
    return repo


def _capture_spawns(monkeypatch):
    """Record detached `reverie context ...` spawns; real git calls pass through."""
    import subprocess
    real, spawned = subprocess.Popen, []

    def fake(*a, **k):
        if "context" in " ".join(map(str, a[0] if isinstance(a[0], list) else [a[0]])):
            spawned.append(a[0])
            return None
        return real(*a, **k)
    monkeypatch.setattr(subprocess, "Popen", fake)
    return spawned


def test_hint_only_when_more_than_one_org_or_team_source(tmp_path, monkeypatch, env):
    repo, org, team = env                                  # one org and one team: two sources
    (org / "r.md").write_text("## Rules\n- Org rule\n")
    (team / "r.md").write_text("## Rules\n- Team rule\n")
    assert context.CONTRADICTION_HINT in context.render(str(repo))
    (team / "r.md").unlink()                               # one source left: no hint, no cost
    assert context.CONTRADICTION_HINT not in context.render(str(repo))
    (org / "r.md").unlink()
    context.add(str(repo), "Repo rule")                    # repo/personal alone never trigger it
    assert context.CONTRADICTION_HINT not in context.render(str(repo))


def test_hint_does_not_detect_anything(tmp_path, monkeypatch):
    """The hint is a nudge: it appears for sibling teams whether or not they contradict
    and never creates a conflict record."""
    repo = _two_teams(tmp_path, monkeypatch)
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Use black\n")
    assert context.CONTRADICTION_HINT in context.render(str(repo))
    assert context.resolve(str(repo)).conflicts == []


def test_auto_check_judges_on_change_only_and_survives_a_failing_judge(tmp_path, monkeypatch):
    repo = _two_teams(tmp_path, monkeypatch)
    calls = []

    def judge(p):
        calls.append(p)
        return "1: opposite"
    first = context_check.auto_check(str(repo), judge)
    assert first and first["conflicts"] == 1 and calls
    n = len(calls)
    assert context_check.auto_check(str(repo), judge) is None        # nothing changed: free
    assert len(calls) == n
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Never squash merge\n- Tag releases\n")
    assert context_check.auto_check(str(repo), judge) is not None    # a document changed
    assert len(calls) > n
    assert context.resolve(str(repo)).conflicts[0].status == "unresolved"


def test_failing_judge_changes_nothing_and_is_retried(tmp_path, monkeypatch):
    repo = _two_teams(tmp_path, monkeypatch)
    before = deliver.startup_text(str(repo))

    def boom(p):
        raise RuntimeError("provider down")
    res = context_check.auto_check(str(repo), boom)
    assert res and res["judge_errors"] > 0
    assert deliver.startup_text(str(repo)) == before                  # delivery unchanged
    assert context.load_state().get("doc_sig", {}).get(str(repo)) is None   # will retry
    assert context_check.auto_check(str(repo), lambda p: "1: opposite")["conflicts"] == 1


def test_failing_judge_does_not_skip_commits_for_staleness(env):
    repo, _, _ = env
    (repo / "retry.py").write_text("def old_helper():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    context.add(str(repo), "Use `old_helper` in retry.py for all retries")
    context_check.auto_check(str(repo), lambda p: "NONE")             # baseline at HEAD
    (repo / "retry.py").write_text("def new_helper():\n    return 2\n")
    _git(repo, "commit", "-qam", "replace helper")

    def boom(p):
        raise RuntimeError("down")
    context_check.auto_check(str(repo), boom)
    out = context_check.auto_check(str(repo), lambda p: "CONTRADICTED: removed")
    assert out and out["stale"] == 1                                   # the commit was not lost


def test_session_start_never_calls_a_judge(tmp_path, monkeypatch):
    """The hook only hashes files and spawns a detached worker; a judge that explodes
    is never reached and the printed block is identical."""
    from reverie import hooks
    repo = _two_teams(tmp_path, monkeypatch)
    monkeypatch.delenv("REVERIE_NO_BACKGROUND", raising=False)
    spawned = _capture_spawns(monkeypatch)

    def forbidden(*a, **k):
        raise AssertionError("a model call inside a hook")
    monkeypatch.setattr(context_check, "_cfg_judge", forbidden)
    monkeypatch.setattr(context_check, "find_conflicts", forbidden)
    monkeypatch.setattr(context_check, "check_stale", forbidden)
    out = []
    hooks.session_start(str(repo), print_fn=out.append)
    assert len(spawned) == 1                                  # a detached worker, not a call
    text = "\n".join(out)
    assert "Always squash merge" in text and context.CONTRADICTION_HINT in text
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    out2 = []
    hooks.session_start(str(repo), print_fn=out2.append)
    assert "\n".join(out2) == text                                    # spawning changes nothing


def test_spawn_skips_when_nothing_changed(tmp_path, monkeypatch):
    from reverie import hooks
    repo = _two_teams(tmp_path, monkeypatch)
    monkeypatch.delenv("REVERIE_NO_BACKGROUND", raising=False)
    context_check.auto_check(str(repo), lambda p: "NONE")
    spawned = _capture_spawns(monkeypatch)
    hooks._spawn_context_check(str(repo))
    assert len(spawned) == 1                    # first time: the daily full recheck
    (Path(context.spool.repo_dir(str(repo))) / "context.lock").unlink()
    hooks._spawn_context_check(str(repo))
    assert len(spawned) == 1                    # unchanged and checked today: nothing


def test_cli_add_and_accept_run_detection(tmp_path, monkeypatch, capsys):
    from reverie import cli
    repo = _two_teams(tmp_path, monkeypatch)
    monkeypatch.delenv("REVERIE_NO_BACKGROUND", raising=False)
    monkeypatch.chdir(repo)
    seen = []
    monkeypatch.setattr(context_check, "auto_check",
                        lambda root, *a, **k: seen.append(root) or {"conflicts": 1})
    assert cli.main(["context", "add", "Rule three"]) == 0
    assert seen and "conflict(s) between scopes" in capsys.readouterr().out
