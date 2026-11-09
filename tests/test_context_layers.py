"""Team context layers: parsing, precedence, conflicts, delivery, adding."""

import subprocess
from pathlib import Path

import pytest

from reverie import context, deliver


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "payments-api"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    org = tmp_path / "acme-context" / "org"
    team = tmp_path / "acme-context" / "payments"
    org.mkdir(parents=True)
    team.mkdir(parents=True)
    (repo / ".reverie").mkdir()
    (repo / ".reverie" / "context.toml").write_text(
        '[sources]\norg = "../acme-context/org"\nteam = "../acme-context/payments"\n')
    return repo, org, team


def test_parse_rules_guidance_locked_tag():
    text = ("applies-to: payments-api, billing\n# Eng\n\n## Rules\n\n"
            "- Never log card numbers {id: no-pii} (locked)\n"
            "- Use the shared retry helper\n  for network calls\n\n"
            "## How we deploy\n\n- Deploys go through the staging gate\n")
    items = context.parse_document(text, "org", "eng.md")
    assert [i.kind for i in items] == ["rule", "rule", "guidance"]
    first = items[0]
    assert first.locked and first.tag == "no-pii" and first.text == "Never log card numbers"
    assert items[1].text == "Use the shared retry helper for network calls"
    assert first.applies_to == ("payments-api", "billing")
    assert first.id == context.item_id("org", "eng.md", first.text)


def test_layers_load_and_applies_to(env):
    repo, org, team = env
    (org / "all.md").write_text("## Rules\n- Org rule\n")
    (org / "other.md").write_text("applies-to: billing\n## Rules\n- Not for us\n")
    (team / "t.md").write_text("applies-to: payments-api\n## Rules\n- Team rule\n")
    context.add(str(repo), "Repo rule")
    texts = [(i.layer, i.text) for i in context.load_items(str(repo))]
    assert texts == [("org", "Org rule"), ("team", "Team rule"), ("repo", "Repo rule")]


def test_same_tag_conflict_is_shown_not_dropped(env):
    repo, org, team = env
    (org / "r.md").write_text("## Rules\n- Use tabs {id: indent}\n")
    context.add(str(repo), "Use spaces", tag="indent")
    res = context.resolve(str(repo))
    assert [c.winner.text for c in res.conflicts] == ["Use tabs"]
    assert [i.text for i in res.effective] == ["Use tabs"]
    assert len(res.items) == 2                       # nothing dropped
    text = context.render(str(repo))
    assert "overrides" in text and "Use spaces" in text and "Use tabs" in text


def test_locked_org_rule_wins_and_is_flagged(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Never log PII {id: pii} (locked)\n")
    context.add(str(repo), "Logging PII is fine for debugging", tag="pii")
    res = context.resolve(str(repo))
    assert res.conflicts[0].locked
    assert "do not follow the lower one" in context.render(str(repo))


def test_judge_conflict_from_state(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Always squash merge\n")
    context.add(str(repo), "Never squash merge")
    items = context.load_items(str(repo))
    state = {"conflicts": [{"a": items[0].id, "b": items[1].id, "reason": "opposite"}]}
    res = context.resolve(str(repo), items, state)
    assert len(res.conflicts) == 1 and res.conflicts[0].reason == "opposite"
    assert [i.text for i in res.effective] == ["Always squash merge"]


def test_stale_flag_moves_item_to_outdated_group(env):
    repo, _, _ = env
    context.add(str(repo), "Use `old_helper` for retries")
    it = context.load_items(str(repo))[0]
    context.save_state({"stale": {it.id: {"commit": "abc123456789", "reason": "removed"}}})
    text = context.render(str(repo))
    assert "Possibly outdated team context" in text and "abc12345" in text
    assert "always apply" not in text


def test_guidance_is_a_pointer_and_show_works(env):
    repo, _, _ = env
    context.add(str(repo), "Deploys go through staging", rule=False)
    it = context.load_items(str(repo))[0]
    assert it.kind == "guidance"
    assert f"reverie show {'<id>'}" in context.render(str(repo))
    assert "document:" in context.show(str(repo), it.id[:6])


def test_add_idempotent_and_sections(env):
    repo, _, _ = env
    p1 = context.add(str(repo), "Rule one")
    context.add(str(repo), "Rule one")
    context.add(str(repo), "A guide", rule=False, doc="rules")
    context.add(str(repo), "Rule two")
    kinds = [(i.kind, i.text) for i in context.load_items(str(repo))]
    assert kinds == [("rule", "Rule one"), ("guidance", "A guide"), ("rule", "Rule two")]
    assert p1.read_text().count("Rule one") == 1


def test_add_org_without_source_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(ValueError, match="no org source"):
        context.add(str(repo), "x", layer="org")


def test_startup_text_includes_team_rules_first(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Org rule alpha\n")
    out = deliver.startup_text(str(repo))
    assert "Org rule alpha" in out
    assert Path(context.documents(str(repo))[0][1]).exists()


def test_render_is_bounded(env):
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n" + "\n".join(f"- rule number {i} " + "x" * 200
                                                       for i in range(80)))
    assert len(context.render(str(repo), budget=1500)) < 1700


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=t",
                    *a], check=True, capture_output=True)


def test_conflict_judge_flags_and_caches(env):
    from reverie import context_check
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Always squash merge\n")
    context.add(str(repo), "Never squash merge")
    calls = []

    def judge(prompt):
        calls.append(prompt)
        return "1: opposite"

    out = context_check.find_conflicts(str(repo), judge)
    assert len(out) == 1 and out[0]["reason"] == "opposite"
    context_check.find_conflicts(str(repo), judge)
    assert len(calls) == 1                      # cached by content
    res = context.resolve(str(repo))
    assert res.conflicts and res.conflicts[0].winner.layer == "org"
    assert [i.text for i in res.effective] == ["Always squash merge"]


def test_conflict_judge_none_and_error(env):
    from reverie import context_check
    repo, org, _ = env
    (org / "r.md").write_text("## Rules\n- Use black\n")
    context.add(str(repo), "Line length 100")
    assert context_check.find_conflicts(str(repo), lambda p: "NONE") == []

    def boom(p):
        raise RuntimeError("x")
    context.add(str(repo), "Another rule")
    assert context_check.find_conflicts(str(repo), boom) == []


def test_shared_doc_staleness_names_file_and_commit(env):
    from reverie import context_check
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
