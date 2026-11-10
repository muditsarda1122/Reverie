"""The promotion path: a rule a user taught their agent, proposed into a team document."""

import subprocess
from pathlib import Path

import pytest

from reverie import cli, context, context_propose, deliver, items, rules


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=t", *a],
                   check=True, capture_output=True)


def _repo(path: Path, email: str, name="Dev") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", email)
    _git(path, "config", "user.name", name)
    (path / ".reverie").mkdir(exist_ok=True)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A repo whose team folder is owned by lead@acme.com; the current user is a non-owner
    with one personal rule."""
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    repo = _repo(tmp_path / "svc", "dev@acme.com")
    team = _repo(tmp_path / "acme" / "payments", "dev@acme.com")   # the shared team repo
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = "../acme/payments"\n')
    (team / "CODEOWNERS").write_text("* lead@acme.com\n")
    rules.add("Money amounts are integer cents, never floats", level="user")
    return repo, team


def _become(team, email):
    _git(team, "config", "user.email", email)


def test_non_owner_propose_is_a_proposal_with_provenance_and_not_in_force(env):
    repo, team = env
    got = context_propose.propose(str(repo), "integer cents", why="bit us twice")
    assert got.result.proposal and got.source.origin == "your rules.md"
    props = context.proposals(str(repo))
    assert len(props) == 1
    p = props[0]
    assert p["from"] == "personal rules.md" and p["proposed-via"] == "reverie context propose"
    assert "bit us twice" in p["why"] and "dev@acme.com" in p["proposed-by"]
    assert "integer cents" not in deliver.startup_text(str(repo)).split("Reverie rules")[0]
    assert [i for i in context.load_items(str(repo)) if i.layer == "team"] == []


def test_owner_accept_puts_rule_in_force_and_hides_the_personal_copy_at_delivery(env):
    repo, team = env
    context_propose.propose(str(repo), "integer cents")
    personal = rules.personal_path().read_text()
    _become(team, "lead@acme.com")
    context.accept(str(repo), context.proposals(str(repo))[0]["slug"])
    text = deliver.startup_text(str(repo))
    assert text.count("integer cents") == 1                 # shown once, as the team rule
    assert "Team context rules" in text
    assert rules.personal_path().read_text() == personal    # never edited or deleted
    assert "integer cents" in rules.load(str(repo))["user"]  # still listed for the user
    assert "integer cents" not in rules.load(str(repo), hide_shared=True)["user"]


def test_personal_rule_comes_back_when_the_team_rule_goes_away(env):
    repo, team = env
    context_propose.propose(str(repo), "integer cents")
    _become(team, "lead@acme.com")
    context.accept(str(repo), context.proposals(str(repo))[0]["slug"])
    doc = team / "rules.md"
    body = doc.read_text()
    assert deliver.startup_text(str(repo)).count("integer cents") == 1
    doc.write_text("## Rules\n")                             # the team rule is removed
    after = deliver.startup_text(str(repo))
    assert after.count("integer cents") == 1 and "Team context rules" not in after
    assert "Your personal rules" in after
    doc.write_text(body)                                    # restored: hidden again
    assert "Your personal rules" not in deliver.startup_text(str(repo))


def test_personal_rule_comes_back_when_the_team_rule_is_superseded_or_stale(env):
    repo, team = env
    context_propose.propose(str(repo), "integer cents")
    _become(team, "lead@acme.com")
    context.accept(str(repo), context.proposals(str(repo))[0]["slug"])
    text = (team / "rules.md").read_text().replace(
        "Money amounts are integer cents, never floats",
        "Money amounts are integer cents, never floats {id: money}")
    (team / "rules.md").write_text(text)
    # a narrower repo rule answers it (same tag): the team rule is overridden
    (repo / ".reverie" / "context").mkdir(parents=True)
    (repo / ".reverie" / "context" / "rules.md").write_text(
        "## Rules\n- Money is a Decimal here {id: money}\n")
    assert "Your personal rules" in deliver.startup_text(str(repo))
    (repo / ".reverie" / "context" / "rules.md").unlink()
    assert "Your personal rules" not in deliver.startup_text(str(repo))
    state = context.load_state()                            # a stale flag brings it back too
    item = next(i for i in context.load_items(str(repo)) if i.layer == "team")
    state["stale"] = {item.id: {"commit": "abc", "reason": "x", "file": item.file}}
    context.save_state(state)
    assert "Your personal rules" in deliver.startup_text(str(repo))


def test_personal_context_rule_is_hidden_in_render_not_in_list(env):
    repo, team = env
    context.add(str(repo), "Prefer small PRs", layer="personal")
    (team / "rules.md").write_text("## Rules\n- Prefer small PRs\n")
    res = context.resolve(str(repo))
    assert len(res.hidden) == 1 and any(i.layer == "personal" for i in res.items)
    assert context.render(str(repo)).count("Prefer small PRs") == 1


def test_memory_item_source_needs_user_said(env):
    repo, team = env
    conn = items.connect(deliver.db_path())
    key = deliver.repo_key(str(repo))
    said = items.add(conn, text="Always paginate list endpoints", kind="rule",
                     source="user_said", repo_key=key)
    inferred = items.add(conn, text="The code seems to paginate", kind="rule",
                         source="agent_inferred", repo_key=key, evidence="read api.py")
    conn.close()
    got = context_propose.propose(str(repo), said[:8])
    assert got.source.origin.startswith("memory item") and got.result.proposal
    assert context.proposals(str(repo))[0]["from"].startswith("memory item")
    with pytest.raises(ValueError, match="inferred by an agent"):
        context_propose.propose(str(repo), inferred[:8])
    with pytest.raises(ValueError, match="no rule of yours"):
        context_propose.propose(str(repo), "deadbeef")


def test_secret_is_refused_not_stripped(env):
    repo, _ = env
    with pytest.raises(ValueError, match="secret"):
        context_propose.propose(str(repo), "Use token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA for CI")
    assert context.proposals(str(repo)) == []


def test_duplicates_are_refused(env):
    repo, team = env
    context_propose.propose(str(repo), "integer cents")
    with pytest.raises(ValueError, match="already proposed"):
        context_propose.propose(str(repo), "integer cents")
    (team / "rules.md").write_text("## Rules\n- Prefer small PRs\n")
    rules.add("prefer small prs.", level="user")
    with pytest.raises(ValueError, match="already a rule"):
        context_propose.propose(str(repo), "prefer small prs")


def test_owner_proposal_writes_directly(env):
    repo, team = env
    _become(team, "lead@acme.com")
    got = context_propose.propose(str(repo), "integer cents")
    assert not got.result.proposal
    assert "integer cents" in (team / "rules.md").read_text()


def test_reject_keeps_the_reason_in_the_proposals_folder_and_never_delivers_it(env):
    repo, team = env
    context_propose.propose(str(repo), "integer cents")
    slug = context.proposals(str(repo))[0]["slug"]
    with pytest.raises(ValueError, match="only the owners"):
        context.reject(str(repo), slug, "no")
    _become(team, "lead@acme.com")
    with pytest.raises(ValueError, match="say why"):
        context.reject(str(repo), slug, "  ")
    dest = context.reject(str(repo), slug, "we use Decimal for ledgers, floats are banned elsewhere")
    assert dest.parent.name == "rejected" and dest.parent.parent.name == "proposals"
    assert "Decimal for ledgers" in dest.read_text() and "rejected-by" in dest.read_text()
    assert context.proposals(str(repo)) == []
    assert context.rejected(str(repo))[0]["reason"].startswith("we use Decimal")
    out = deliver.startup_text(str(repo))
    assert "Decimal for ledgers" not in out
    assert "Team context rules" not in out                  # not in force either
    assert "integer cents" in out                           # the personal rule is untouched
    assert "Decimal for ledgers" not in context.render(str(repo))
    assert "Decimal for ledgers" not in context.point_text(str(repo), "a.py")[0]


def test_propose_warns_about_a_prior_rejection_and_needs_again_and_why(env):
    repo, team = env
    context_propose.propose(str(repo), "integer cents")
    _become(team, "lead@acme.com")
    context.reject(str(repo), context.proposals(str(repo))[0]["slug"], "not for ledgers")
    _become(team, "dev@acme.com")
    with pytest.raises(ValueError, match=r"(?s)rejected on .*not for ledgers.*Not proposing it again"):
        context_propose.propose(str(repo), "integer cents")
    assert context.proposals(str(repo)) == []               # silently re-proposed: no
    with pytest.raises(ValueError, match="needs --why"):
        context_propose.propose(str(repo), "integer cents", again=True)
    got = context_propose.propose(str(repo), "integer cents", again=True,
                                  why="ledgers now use integer cents too")
    assert got.prior_rejection and context.proposals(str(repo))[0]["re-proposal-of"]


def test_cli_propose_reject_accept_round_trip(env, monkeypatch, capsys):
    repo, team = env
    monkeypatch.chdir(repo)
    assert cli.main(["context", "propose", "integer cents", "--why", "bit us"]) == 0
    assert "Proposed" in capsys.readouterr().out
    assert cli.main(["context", "propose", "integer cents"]) == 2          # already proposed
    slug = context.proposals(str(repo))[0]["slug"]
    assert cli.main(["context", "reject", slug]) == 2                      # --why missing
    _become(team, "lead@acme.com")
    assert cli.main(["context", "reject", slug, "--why", "not now"]) == 0
    assert cli.main(["context", "proposals"]) == 0
    assert "not now" in capsys.readouterr().out
    _become(team, "dev@acme.com")
    assert cli.main(["context", "propose", "integer cents"]) == 2
    assert "rejected on" in capsys.readouterr().err
    assert cli.main(["context", "propose", "integer cents", "--again", "--why", "changed"]) == 0
    _become(team, "lead@acme.com")
    assert cli.main(["context", "accept", context.proposals(str(repo))[0]["slug"]]) == 0
    assert "Team context rules" in deliver.startup_text(str(repo))


def test_propose_refuses_the_personal_layer(env):
    repo, _ = env
    with pytest.raises(ValueError, match="OUT of your personal scope"):
        context_propose.propose(str(repo), "integer cents", layer="personal")
