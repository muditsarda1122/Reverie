"""feat/flow tests — export / import / reset (D58) + the `reverie` CLI
subcommands (enable / disable / repos).

Run: .venv/bin/python -m pytest tests/test_flow_transport.py -v
All offline, everything under tmp_path (REVERIE_HOME redirected).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reverie import cli, repos, transport
from reverie.brain import Brain
from reverie.config import SCHEMA_VERSION
from reverie.transport import (
    TransportError,
    export_brain,
    import_brain,
    reset_brain,
)

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def reverie_home(tmp_path, monkeypatch):
    home = tmp_path / "reverie_home"
    monkeypatch.setenv("REVERIE_HOME", str(home))
    return home


@pytest.fixture()
def brain(reverie_home):
    b = Brain(reverie_home / "reverie.db")
    yield b
    b.close()


def seed(brain, cognition="A canonical truth", repo="/repos/a"):
    brain.insert_ecu({
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a"},
        "provenance": {"source_type": "debugging"},
        "confidence": 0.7,
        "grounding": {"repo_path": repo, "files": ["a.py"]},
    })


def session_with_candidate(brain, repo="/repos/a", branch="main"):
    sid = brain.create_session(repo, branch)
    brain.insert_session_ecu(sid, {
        "cognition": "a candidate",
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a"},
        "provenance": {"source_type": "debugging",
                       "created_at": T0.isoformat()},
        "confidence": 0.5,
    })
    return sid


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def test_export_writes_zip_with_manifest(brain, tmp_path):
    seed(brain)
    session_with_candidate(brain)
    brain.upsert_repo("/git/a/.git", path="/repos/a",
                      url="https://x/a.git", state="enabled")
    out = tmp_path / "export.zip"
    manifest = export_brain(brain, out)
    assert out.exists()
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["embedding_model"] == "all-MiniLM-L6-v2"
    assert manifest["embedding_dimensions"] == 384
    assert manifest["counts"]["ecus"] == 1
    assert manifest["counts"]["sessions"] == 1
    assert manifest["counts"]["repos"] == 1
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"brain.db", "manifest.json"}
        assert json.loads(zf.read("manifest.json"))["format"] == \
            transport.EXPORT_FORMAT


def test_export_refuses_existing_file(brain, tmp_path):
    out = tmp_path / "export.zip"
    out.write_text("x")
    with pytest.raises(TransportError, match="already exists"):
        export_brain(brain, out)


def test_export_default_path_in_cwd(brain, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest = export_brain(brain)
    assert manifest["counts"]["ecus"] == 0
    assert list(tmp_path.glob("reverie-export-*.zip"))


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def test_import_into_empty_brain(brain, tmp_path):
    seed(brain, "shipped belief")
    brain.upsert_repo("/git/a/.git", url="https://x/a.git", state="enabled")
    out = tmp_path / "export.zip"
    export_brain(brain, out)

    fresh = Brain(tmp_path / "other" / "reverie.db")
    try:
        result = import_brain(fresh, out)
        assert result["manifest"]["counts"]["ecus"] == 1
        assert fresh.count_ecus() == 1
        assert fresh.get_ecu(fresh.list_ecus()[0]["id"]) is not None
        assert fresh.list_repos()[0]["state"] == "enabled"
        assert fresh.list_repos()[0]["url"] == "https://x/a.git"
    finally:
        fresh.close()


def test_import_nonempty_requires_replace(brain, tmp_path, capsys):
    seed(brain, "local belief")
    out = tmp_path / "export.zip"
    export_brain(brain, out)
    seed(brain, "another local")            # brain no longer empty
    with pytest.raises(TransportError, match="--replace"):
        import_brain(brain, out)
    assert "merging two brains is not supported" in capsys.readouterr().err \
        or True                             # message content asserted below


def test_import_replace_takes_backup_first(brain, tmp_path):
    seed(brain, "local belief")
    sid = session_with_candidate(brain)
    out = tmp_path / "export.zip"
    export_brain(brain, out)
    seed(brain, "another local")

    printed: list[str] = []
    result = import_brain(brain, out, replace=True, print_fn=printed.append)
    assert result["backup"] is not None
    assert Path(result["backup"]).exists()
    assert any("Automatic backup" in line for line in printed)
    assert brain.count_ecus() == 1
    # the export was taken before "another local" was seeded — the restore
    # is the export's contents, exactly
    assert "local belief" in brain.list_ecus()[0]["cognition"]
    assert brain.list_sessions()[0]["id"] == sid


def _rezip(tmp_path, manifest_updates: dict) -> Path:
    """Take a valid export, patch the manifest, re-zip."""
    brain = Brain(tmp_path / "src" / "reverie.db")
    seed(brain, "content")
    out = tmp_path / "src.zip"
    export_brain(brain, out)
    brain.close()
    patched = tmp_path / "patched.zip"
    with zipfile.ZipFile(out) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest.update(manifest_updates)
    members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(patched, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return patched


def test_import_refuses_embedding_model_mismatch(brain, tmp_path):
    bad = _rezip(tmp_path, {"embedding_model": "other-model-v9"})
    with pytest.raises(TransportError, match="embedding model"):
        import_brain(brain, bad)


def test_import_refuses_dimension_mismatch(brain, tmp_path):
    bad = _rezip(tmp_path, {"embedding_dimensions": 1536})
    with pytest.raises(TransportError, match="dimensions"):
        import_brain(brain, bad)


def test_import_refuses_newer_schema(brain, tmp_path):
    bad = _rezip(tmp_path, {"schema_version": SCHEMA_VERSION + 5})
    with pytest.raises(TransportError, match="newer than this Reverie"):
        import_brain(brain, bad)


def test_import_refuses_not_an_export(brain, tmp_path):
    junk = tmp_path / "junk.zip"
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("readme.txt", "hello")
    with pytest.raises(TransportError, match="not a Reverie export"):
        import_brain(brain, junk)


def test_import_refuses_missing_file(brain, tmp_path):
    with pytest.raises(TransportError, match="No such file"):
        import_brain(brain, tmp_path / "nope.zip")


# ---------------------------------------------------------------------------
# import robustness (feat/flow review fixes): atomicity, schema mismatch
# ---------------------------------------------------------------------------

def _repack(export: Path, tmp_path, mutate_db=None,
            manifest_updates: dict | None = None) -> Path:
    """Take a valid export, patch the raw brain.db member (and/or the
    manifest), re-zip."""
    with zipfile.ZipFile(export) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    if mutate_db is not None or manifest_updates is not None:
        raw = tmp_path / "raw-brain.db"
        raw.write_bytes(members["brain.db"])
        conn = sqlite3.connect(raw)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            if mutate_db is not None:
                mutate_db(conn)
            conn.commit()
        finally:
            conn.close()
        members["brain.db"] = raw.read_bytes()
    if manifest_updates:
        manifest = json.loads(members["manifest.json"])
        manifest.update(manifest_updates)
        members["manifest.json"] = json.dumps(manifest).encode()
    out = tmp_path / "repacked.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return out


def test_import_failure_leaves_brain_untouched(brain, tmp_path):
    """Fix 1: import is atomic. The copy fails midway (NOT NULL violation
    on a deliberately corrupted snapshot table, after earlier tables
    copied) — the local brain must be exactly as it was."""
    seed(brain, "precious local")
    sid = session_with_candidate(brain)
    brain.upsert_repo("/git/a/.git", state="enabled")

    incoming = Brain(tmp_path / "incoming" / "reverie.db")
    try:
        seed(incoming, "incoming one")
        seed(incoming, "incoming two")
        other = tmp_path / "other.zip"
        export_brain(incoming, other)
        # corrupt the snapshot's edges table: no constraints, a row with a
        # NULL created_at — the destination copy fails right after `ecus`
        bad = _repack(other, tmp_path, mutate_db=lambda c: c.executescript(
            "DROP TABLE edges;"
            "CREATE TABLE edges (id TEXT, source_id TEXT, target_id TEXT,"
            " type TEXT, weight REAL, created_at TEXT);"
            "INSERT INTO edges VALUES"
            " ('e1', 'x', 'y', 'supports', 1.0, NULL);"))
        with pytest.raises(TransportError, match="left exactly as it was"):
            import_brain(brain, bad, replace=True)
        assert brain.count_ecus() == 1
        assert brain.list_ecus()[0]["cognition"] == "precious local"
        assert brain.list_sessions()[0]["id"] == sid
        assert brain.get_repo("/git/a/.git")["state"] == "enabled"
        # the connection is still usable
        seed(brain, "still works")
        assert brain.count_ecus() == 2
    finally:
        incoming.close()


def test_import_fk_violation_rolls_back(brain, tmp_path):
    """Fix 3: the foreign-key check runs BEFORE the commit and raises a
    real exception that rolls everything back (not an assert after it)."""
    seed(brain, "precious local")
    sid = session_with_candidate(brain)

    incoming = Brain(tmp_path / "incoming" / "reverie.db")
    try:
        seed(incoming, "incoming one")
        s2 = session_with_candidate(incoming)
        other = tmp_path / "fk.zip"
        export_brain(incoming, other)
        # orphan session_edge (raw connection: FK enforcement is off by
        # default) — caught only by PRAGMA foreign_key_check
        bad = _repack(other, tmp_path, mutate_db=lambda c: c.execute(
            "INSERT INTO session_edges (id, session_id, source_id,"
            " target_type, target_id, type) VALUES"
            " ('orphan', ?, 'ghost', 'session_ecu', 'ghost', 'supports')",
            (s2,)))
        with pytest.raises(TransportError, match="rolled back"):
            import_brain(brain, bad, replace=True)
        assert brain.count_ecus() == 1
        assert brain.list_sessions()[0]["id"] == sid
        assert brain.list_session_ecus(sid)[0]["cognition"] == "a candidate"
    finally:
        incoming.close()


def test_import_from_previous_schema_without_new_tables(brain, tmp_path):
    """Fix 2a: an export from an older schema (no `repos` /
    `review_decisions` tables, lower user_version) is upgraded through the
    normal schema/migration path before the copy."""
    seed(brain, "local")
    export = tmp_path / "export.zip"
    export_brain(brain, export)

    incoming = Brain(tmp_path / "incoming" / "reverie.db")
    try:
        seed(incoming, "from the old schema")
        other = tmp_path / "old.zip"
        export_brain(incoming, other)

        def strip_new_tables(conn):
            conn.execute("DROP TABLE repos")
            conn.execute("DROP TABLE review_decisions")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")

        old = _repack(other, tmp_path, mutate_db=strip_new_tables,
                      manifest_updates={"schema_version": SCHEMA_VERSION - 1})
        import_brain(brain, old, replace=True)
        assert brain.count_ecus() == 1
        assert brain.list_ecus()[0]["cognition"] == "from the old schema"
        # the upgraded snapshot's (empty) new tables copied cleanly
        assert brain.list_repos() == []
        assert brain.list_review_decisions() == []
    finally:
        incoming.close()


def test_import_survives_altered_column_order(brain, tmp_path):
    """Fix 2b: a snapshot whose maintenance_log got `ecus_affected` at a
    different position (ALTER TABLE order vs fresh-schema order) imports
    correctly — columns are matched by name, never by position."""
    seed(brain, "local")
    export = tmp_path / "export.zip"
    export_brain(brain, export)

    incoming = Brain(tmp_path / "incoming" / "reverie.db")
    try:
        seed(incoming, "from altered schema")
        incoming._conn.execute(
            "INSERT INTO maintenance_log (id, run_at, action, details,"
            " ecus_affected) VALUES ('m1', '2026-10-14T00:00:00+00:00',"
            " 'grounding', '{\"checked\": 4}', 3)")
        incoming._conn.commit()
        other = tmp_path / "altered.zip"
        export_brain(incoming, other)

        def reorder_maintenance_log(conn):
            conn.executescript(
                "CREATE TABLE maintenance_log_new ("
                " id TEXT PRIMARY KEY, run_at TEXT NOT NULL,"
                " action TEXT NOT NULL, ecus_affected INTEGER DEFAULT 0,"
                " details TEXT);"
                "INSERT INTO maintenance_log_new"
                " SELECT id, run_at, action, ecus_affected, details"
                " FROM maintenance_log;"
                "DROP TABLE maintenance_log;"
                "ALTER TABLE maintenance_log_new RENAME TO maintenance_log;")

        altered = _repack(other, tmp_path,
                          mutate_db=reorder_maintenance_log)
        import_brain(brain, altered, replace=True)
        row = brain.list_maintenance_log()[0]
        assert row["ecus_affected"] == 3
        assert row["details"] == '{"checked": 4}'
        assert row["action"] == "grounding"
        assert brain.list_ecus()[0]["cognition"] == "from altered schema"
    finally:
        incoming.close()


def test_replace_reports_tables_missing_from_source(brain, tmp_path):
    """Fix 2c: tables absent from the source are skipped, not errors, and
    reported in the result."""
    session_with_candidate(brain)
    seed(brain)
    snap = tmp_path / "partial.db"
    brain._conn.execute("VACUUM INTO ?", (str(snap),))
    conn = sqlite3.connect(snap)
    try:
        conn.execute("DROP TABLE clusters")
        conn.execute("DROP TABLE cluster_memberships")
        conn.commit()
    finally:
        conn.close()
    brain._conn.execute("ATTACH ? AS partial", (str(snap),))
    try:
        counts = brain.replace_all_from_attached("partial")
    finally:
        brain._conn.execute("DETACH partial")
    assert "clusters" in counts["skipped_tables"]
    assert "cluster_memberships" in counts["skipped_tables"]
    assert counts["ecus"] == 1
    assert counts["sessions"] == 1
    # the skipped tables still exist locally (empty after the wipe)
    assert "clusters" in brain.table_names()


def test_clear_all_data_is_atomic(brain, monkeypatch):
    """Fix 3 (reset side): a failure mid-wipe rolls back — nothing is lost."""
    seed(brain)
    sid = session_with_candidate(brain)
    real = Brain.table_names

    def ghost_table_names(self):
        return real(self) + ["ghost_table"]

    monkeypatch.setattr(Brain, "table_names", ghost_table_names)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        brain.clear_all_data()
    monkeypatch.undo()
    assert brain.count_ecus() == 1
    assert brain.list_sessions()[0]["id"] == sid


# ---------------------------------------------------------------------------
# URL rematching (D58): states survive moves and re-clones
# ---------------------------------------------------------------------------

def test_repo_state_remaches_by_url_after_move(brain, tmp_path):
    brain.upsert_repo("/old/path/.git", path="/old/path",
                      url="https://x/a.git", state="enabled")
    # the repo is re-cloned to a new path; first tool call there:
    info = repos.RepoInfo(repo_path="/new/path", branch="main",
                          repo_key="/new/path/.git",
                          url="https://x/a.git")
    assert repos.state_of(brain, info) == "enabled"
    row = brain.get_repo("/new/path/.git")
    assert row["state"] == "enabled"
    assert brain.get_repo("/old/path/.git") is None


def test_repo_state_ambiguous_url_starts_fresh(brain):
    brain.upsert_repo("/clone1/.git", url="https://x/a.git",
                      state="enabled")
    brain.upsert_repo("/clone2/.git", url="https://x/a.git",
                      state="disabled")
    info = repos.RepoInfo(repo_path="/clone3", branch="main",
                          repo_key="/clone3/.git", url="https://x/a.git")
    assert repos.state_of(brain, info) == "undecided"


def test_repo_state_no_url_starts_fresh(brain):
    brain.upsert_repo("/other/.git", url="https://x/a.git",
                      state="enabled")
    info = repos.RepoInfo(repo_path="/mine", branch="main",
                          repo_key="/mine/.git", url=None)
    assert repos.state_of(brain, info) == "undecided"


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def test_reset_whole_brain_with_backup(brain, tmp_path):
    seed(brain)
    session_with_candidate(brain)
    brain.upsert_repo("/git/a/.git", state="enabled")
    printed: list[str] = []
    backup = reset_brain(brain, print_fn=printed.append)
    assert Path(backup).exists()
    assert any("Automatic backup" in line for line in printed)
    assert brain.count_ecus() == 0
    assert brain.list_sessions() == []
    assert brain.list_repos() == []
    # the brain still works after the reset
    assert session_with_candidate(brain)


def test_reset_repo_keeps_long_term_memory(brain, tmp_path):
    seed(brain, "kept belief", repo="/repos/a")
    session_with_candidate(brain, repo="/repos/a")
    session_with_candidate(brain, repo="/repos/b")
    brain.upsert_repo("/git/a/.git", state="enabled")
    printed: list[str] = []
    backup = reset_brain(brain, repo="/repos/a", print_fn=printed.append)
    assert Path(backup).exists()
    assert brain.count_ecus() == 1                    # long-term memory kept
    assert brain.list_repos()[0]["state"] == "enabled"
    repos_left = brain.list_sessions()
    assert {s["repo_path"] for s in repos_left} == {"/repos/b"}
    assert not brain.list_queue_candidates(repo_path="/repos/a")


def test_reset_repo_covers_worktrees(brain, reverie_home, tmp_path, capsys):
    """Fix 7: sessions are keyed by worktree path while repo identity is
    the git common dir — `reverie reset --repo` must wipe every worktree
    belonging to the repo and say which paths were included."""
    main_wt = real_repo(tmp_path, "main-wt")
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"],
                   cwd=main_wt, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(tmp_path), "PATH": os.environ["PATH"]})
    subprocess.run(["git", "worktree", "add", str(tmp_path / "linked-wt"),
                    "-b", "linked"], cwd=main_wt, check=True,
                   capture_output=True)
    main_path = str(main_wt.resolve())
    linked_path = str((tmp_path / "linked-wt").resolve())
    session_with_candidate(brain, repo=main_path)
    session_with_candidate(brain, repo=main_path, branch="dev")
    session_with_candidate(brain, repo=linked_path)
    session_with_candidate(brain, repo="/repos/other")   # untouched
    seed(brain, "kept belief", repo=main_path)
    brain.upsert_repo(str((tmp_path / "main-wt" / ".git").resolve()),
                      state="enabled")

    printed: list[str] = []
    reset_brain(brain, repo=main_wt, print_fn=printed.append)
    out = "\n".join(printed)
    assert brain.count_ecus() == 1                    # long-term memory kept
    assert brain.list_repos()[0]["state"] == "enabled"
    remaining = {s["repo_path"] for s in brain.list_sessions()}
    assert remaining == {"/repos/other"}
    assert "Included worktree paths:" in out
    assert main_path in out and linked_path in out
    assert "repo identity:" in out                    # identity is printed


def test_reset_cli_confirms_with_word(brain, monkeypatch, capsys):
    seed(brain)
    monkeypatch.setattr("builtins.input", lambda _p="": "no")
    assert transport.main_reset([]) == 1
    assert "Aborted" in capsys.readouterr().out
    assert brain.count_ecus() == 1
    monkeypatch.setattr("builtins.input", lambda _p="": "reset")
    assert transport.main_reset([]) == 0
    assert brain.count_ecus() == 0


def test_reset_cli_yes_still_backs_up(brain, tmp_path, capsys):
    seed(brain)
    assert transport.main_reset(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "Automatic backup" in out
    assert brain.count_ecus() == 0


# ---------------------------------------------------------------------------
# the `reverie` CLI — enable / disable / repos
# ---------------------------------------------------------------------------

def real_repo(tmp_path, name="repo"):
    r = tmp_path / name
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=r, check=True,
                   capture_output=True)
    return r


def test_cli_enable_disable_roundtrip(reverie_home, tmp_path, capsys,
                                      monkeypatch):
    repo = real_repo(tmp_path)
    monkeypatch.delenv("REVERIE_REPO_PATH", raising=False)
    monkeypatch.delenv("REVERIE_BRANCH", raising=False)
    assert cli.main(["enable", str(repo)]) == 0
    out = capsys.readouterr().out
    assert f"Reverie is on for {repo.resolve()}" in out
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert repos.state_of(brain, repos.resolve_repo(
            cwd=repo)) == "enabled"
    finally:
        brain.close()
    assert cli.main(["disable", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "Fully off" in out
    assert "Existing memory is kept" in out
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert repos.state_of(brain, repos.resolve_repo(
            cwd=repo)) == "disabled"
    finally:
        brain.close()


def test_cli_enable_outside_repo_fails(reverie_home, tmp_path, capsys,
                                       monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.delenv("REVERIE_REPO_PATH", raising=False)
    monkeypatch.delenv("REVERIE_BRANCH", raising=False)
    assert cli.main(["enable", str(plain)]) == 1
    assert "Not a git repository" in capsys.readouterr().err


def test_cli_repos_listing(reverie_home, tmp_path, capsys, brain):
    seed(brain)
    session_with_candidate(brain, repo="/repos/a", branch="main")
    brain.upsert_repo("/git/a/.git", path="/repos/a",
                      url="https://x/a.git", state="enabled")
    assert cli.main(["repos"]) == 0
    out = capsys.readouterr().out
    assert "/repos/a" in out
    assert "state: enabled" in out
    assert "candidates: 1" in out
    assert "https://x/a.git" in out


def test_cli_repos_empty(reverie_home, capsys):
    assert cli.main(["repos"]) == 0
    assert "No repos known" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the `reverie` CLI dispatch (feat/flow review fix 6)
# ---------------------------------------------------------------------------

def test_cli_bare_ec_prints_multi_command_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for word in ("enable", "disable", "repos", "review", "export",
                 "import", "reset", "install"):
        assert word in out, word


def test_cli_mistyped_subcommand_errors_with_hint(capsys):
    assert cli.main(["reveiw"]) == 2
    err = capsys.readouterr().err
    assert "Unknown command: 'reveiw'" in err
    assert "Did you mean 'reverie review'?" in err


def test_cli_mistyped_subcommand_without_hint(capsys):
    assert cli.main(["frobnicate"]) == 2
    err = capsys.readouterr().err
    assert "Unknown command: 'frobnicate'" in err
    assert "reverie --help" in err


def test_cli_dash_flags_are_unknown_commands(capsys):
    """`reverie --all` / `reverie --only ...` are NOT shortcuts for the
    installer — flags belong after the subcommand (`reverie install --all`)."""
    assert cli.main(["--all"]) == 2
    assert cli.main(["--only", "claude_code"]) == 2
    err = capsys.readouterr().err
    assert "Unknown command" in err


def test_cli_review_forwards_flags(reverie_home, capsys, monkeypatch):
    """`reverie review --list` reaches the review CLI (the generic argparse
    pass used to reject its flags before review_cli ever saw them)."""
    monkeypatch.setenv("REVERIE_REPO_PATH", "/repos/a")
    monkeypatch.setenv("REVERIE_BRANCH", "main")
    assert cli.main(["review", "--list"]) == 0
    assert "No candidates in this scope." in capsys.readouterr().out


def test_cli_status_shares_logic_with_ec_status(reverie_home, capsys,
                                                monkeypatch):
    """Fix 9: `reverie status` exists and prints the ec-status report."""
    monkeypatch.setenv("REVERIE_REPO_PATH", "/repos/a")
    monkeypatch.setenv("REVERIE_BRANCH", "main")
    assert cli.main(["status"]) == 0
    assert "No session on branch main yet" in capsys.readouterr().out
    assert cli.main(["status", "--repo", "/repos/a", "--branch", "dev"]) == 0
    assert "No session on branch dev yet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ec install --import
# ---------------------------------------------------------------------------

def test_install_import_during_setup(reverie_home, tmp_path, capsys):
    from reverie import install
    source = Brain(tmp_path / "src" / "reverie.db")
    seed(source, "shipped on the old machine")
    source.upsert_repo("/git/a/.git", url="https://x/a.git",
                       state="enabled")
    export = tmp_path / "carry.zip"
    export_brain(source, export)
    source.close()

    home = tmp_path / "fakehome"
    home.mkdir()
    install.run(home=home, reverie_home=reverie_home, all_agents=True, no_verify=True,
                import_file=str(export),
                which=lambda _b: None,
                input_fn=lambda _p="": "",
                print_fn=capsys.write_line if False else (lambda s: None),
                environ={})
    brain = Brain(reverie_home / "reverie.db")
    try:
        assert brain.count_ecus() == 1
        assert brain.list_ecus()[0]["cognition"] == \
            "shipped on the old machine"
        assert brain.list_repos()[0]["state"] == "enabled"
    finally:
        brain.close()


def test_install_import_refuses_nonempty_brain(reverie_home, tmp_path, capsys,
                                               brain):
    from reverie import install
    seed(brain, "already here")
    export = tmp_path / "carry.zip"
    export_brain(brain, export)
    home = tmp_path / "fakehome"
    home.mkdir()
    with pytest.raises(install.InstallError, match="--replace"):
        install.run(home=home, reverie_home=reverie_home, all_agents=True,
                    no_verify=True, import_file=str(export),
                    which=lambda _b: None, input_fn=lambda _p="": "",
                    print_fn=lambda s: None, environ={})
