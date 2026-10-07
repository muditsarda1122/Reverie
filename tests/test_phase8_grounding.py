"""Phase 8 tests — Grounding Verification (design doc Section 3, D35).

Covers the §12.5 table (#1–#10): file existence checks with scope-dependent
deprecation rules (§3.4/§21.1), best-effort symbol checks, depends_on chain
flagging (§3.5), the 72h per-repo throttle, and repo-not-on-disk skipping —
plus supporting coverage: commit-staleness informational flags, frozen-status
skips, empty-grounding tolerance, brain helpers, maintenance_log integration
via run_maintenance, session-start hooking, and MCP-server repo resolution.

Run: .venv/bin/python -m pytest tests/test_phase8_grounding.py -v
All offline: real files in tmp_path repos (one git init for commit-distance),
no LLM, no embedding model (grounding never needs one), injectable `now`.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import copy
import io
import json
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ec import grounding, maintainer, mcp_server
from ec.brain import Brain
from ec.config import DEFAULT_CONFIG, AttrDict, _to_attrdict
from ec.grounding import (
    COMMIT_STALENESS_COMMITS,
    evaluate_ecu,
    should_deprecate,
)
from ec.session import SessionManager

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

def make_config(**maintainer_overrides) -> AttrDict:
    """Deep-copied defaults with optional maintainer-block overrides."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["maintainer"].update(maintainer_overrides)
    return _to_attrdict(cfg)


@pytest.fixture()
def config():
    return make_config()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture()
def repo(tmp_path):
    """A fake project directory with one source file."""
    r = tmp_path / "project"
    (r / "auth").mkdir(parents=True)
    (r / "auth" / "token.py").write_text(
        "class TokenManager:\n"
        "    def refresh(self):\n        pass\n"
    )
    return r


def utcnow():
    return datetime.now(timezone.utc)


def insert_grounded_ecu(brain, repo_path, files=("auth/token.py",),
                        symbols=(), level="repo", status="active",
                        cognition="Auth uses JWT with RS256",
                        commit_hash=None, **overrides):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "decision",
        "scope": {"level": level, "path": f"repo:demo > {level}:x"},
        "provenance": {"source_type": "debugging"},
        "grounding": {
            "repo_path": str(repo_path),
            "files": list(files),
            "symbols": list(symbols),
            **({"commit_hash": commit_hash} if commit_hash else {}),
        },
        "confidence": 0.8,
        "status": status,
    }
    ecu.update(overrides)
    ecu_id = brain.insert_ecu(ecu)
    if status != "active":  # insert always writes 'active'
        brain.update_ecu_status(ecu_id, status)
    return ecu_id


def run_grounding(brain, config, repo_path, now=None):
    result = maintainer.task_grounding(brain, config, repo_path=str(repo_path),
                                       now=now)
    brain.log_maintenance(result.action, json.dumps(result.details),
                          ecus_affected=result.ecus_affected)
    return result


# ---------------------------------------------------------------------------
# §12.5 #1 — file exists → no deprecation
# ---------------------------------------------------------------------------

class TestFileChecks:
    def test_file_exists_no_deprecation(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo)
        result = run_grounding(brain, config, repo)
        assert result.ecus_affected == 0
        assert result.details["deprecated"] == []
        assert brain.get_ecu(ecu_id)["status"] == "active"

    def test_file_deleted_repo_scope(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, level="repo")
        os.remove(repo / "auth" / "token.py")
        result = run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "deprecated"
        assert result.details["deprecated"][0]["missing_files"] == \
            ["auth/token.py"]

    def test_file_deleted_engineering_scope(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, level="engineering")
        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"

    def test_file_deleted_domain_scope(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, level="domain")
        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"

    def test_file_deleted_module_scope(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, level="module")
        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "deprecated"

    def test_file_deleted_subsystem_scope(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, level="subsystem")
        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "deprecated"

    def test_partial_file_deletion(self, brain, config, tmp_path, repo):
        """§12.5 #5: 2 grounding files, 1 deleted → repo scope deprecates,
        project scope does not (needs ALL files gone)."""
        (repo / "db").mkdir()
        (repo / "db" / "pool.py").write_text("pool = 10\n")
        repo_ecu = insert_grounded_ecu(
            brain, repo, files=("auth/token.py", "db/pool.py"),
            level="repo", cognition="repo-scoped")
        project_ecu = insert_grounded_ecu(
            brain, repo, files=("auth/token.py", "db/pool.py"),
            level="project", cognition="project-scoped")
        org_ecu = insert_grounded_ecu(
            brain, repo, files=("auth/token.py", "db/pool.py"),
            level="organization", cognition="org-scoped")
        os.remove(repo / "auth" / "token.py")

        result = run_grounding(brain, config, repo)
        assert brain.get_ecu(repo_ecu)["status"] == "deprecated"
        assert brain.get_ecu(project_ecu)["status"] == "active"
        assert brain.get_ecu(org_ecu)["status"] == "active"

        # Now ALL files gone → organization/project deprecate too.
        os.remove(repo / "db" / "pool.py")
        self.unthrottle(brain, repo)
        run_grounding(brain, config, repo)
        assert brain.get_ecu(repo_ecu)["status"] == "deprecated"
        assert brain.get_ecu(project_ecu)["status"] == "deprecated"
        assert brain.get_ecu(org_ecu)["status"] == "deprecated"
        assert result.action == "grounding"

    @staticmethod
    def unthrottle(brain, repo_path):
        brain.set_maintenance_state(
            f"grounding_last_check:{repo_path}", "2020-01-01T00:00:00+00:00")


# ---------------------------------------------------------------------------
# §12.5 #6/#7 — symbol checks (best-effort grep)
# ---------------------------------------------------------------------------

class TestSymbolChecks:
    def test_symbol_still_exists(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, symbols=["TokenManager"])
        result = run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"
        assert result.details["deprecated"] == []

    def test_symbol_renamed(self, brain, config, repo):
        """§12.5 #7: symbol gone from every remaining file → deprecated
        (best-effort; false negatives are accepted by design, §3.3)."""
        ecu_id = insert_grounded_ecu(
            brain, repo, symbols=["TokenCache"], level="module")
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "deprecated"

    def test_structured_symbol_found_via_fragment(self, brain, config, repo):
        """"TokenManager::refresh" never occurs verbatim; the longest
        separator-separated fragment is found instead → ECU kept."""
        ecu_id = insert_grounded_ecu(
            brain, repo, symbols=["TokenManager::refresh"])
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"

    def test_symbol_checked_across_existing_files(self, brain, config,
                                                  tmp_path, repo):
        """A symbol may live in any of the ECU's remaining files."""
        (repo / "cache.py").write_text("def evict(): ...\n")
        ecu_id = insert_grounded_ecu(
            brain, repo, files=("auth/token.py", "cache.py"),
            symbols=["evict"])
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"

    def test_unreadable_file_keeps_ecu(self, brain, config, repo):
        """Unreadable ≠ absent: cannot establish staleness → keep alive."""
        ecu_id = insert_grounded_ecu(brain, repo, symbols=["TokenManager"])
        target = repo / "auth" / "token.py"
        target.chmod(0o000)
        try:
            assert grounding.symbol_present(str(repo), "auth/token.py",
                                            "TokenManager") is True
        finally:
            target.chmod(0o644)
        run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"


# ---------------------------------------------------------------------------
# §12.5 #8 — depends_on chain flagging (§3.5 step 4)
# ---------------------------------------------------------------------------

class TestDependents:
    def test_depends_on_chain_flagged(self, brain, config, repo):
        dep_id = insert_grounded_ecu(
            brain, repo, cognition="depends on auth",
            files=("auth/token.py",))
        (repo / "auth" / "flow.py").write_text("def refresh_flow(): ...\n")
        dependent_id = insert_grounded_ecu(
            brain, repo, cognition="refresh flow relies on auth",
            files=("auth/flow.py",))
        brain.add_edge(dependent_id, dep_id, "depends_on")
        before = utcnow() - timedelta(minutes=5)

        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo, now=before)

        assert brain.get_ecu(dep_id)["status"] == "deprecated"
        dependent = brain.get_ecu(dependent_id)
        assert dependent["status"] == "challenged"
        assert dependent["metadata"]["last_challenged"] == before.isoformat()

    def test_terminal_dependents_untouched(self, brain, config, repo):
        dep_id = insert_grounded_ecu(brain, repo)
        terminal_ids = [
            insert_grounded_ecu(brain, repo, cognition=f"d{i}",
                                files=("auth/x.py",), status=status)
            for i, status in enumerate(
                ("superseded", "deprecated", "archived", "open_question"))
        ]
        for tid in terminal_ids:
            brain.add_edge(tid, dep_id, "depends_on")
        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)
        for i, tid in enumerate(terminal_ids):
            assert brain.get_ecu(tid)["status"] == (
                "open_question" if i == 3 else
                ["superseded", "deprecated", "archived"][i])

    def test_already_challenged_not_restamped(self, brain, config, repo):
        dep_id = insert_grounded_ecu(brain, repo)
        dependent_id = insert_grounded_ecu(
            brain, repo, cognition="dependent", files=("auth/f.py",),
            status="challenged")
        brain.add_edge(dependent_id, dep_id, "depends_on")
        old_stamp = "2026-01-01T00:00:00+00:00"
        brain.update_ecu_metadata(dependent_id, last_challenged=old_stamp)

        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)

        meta = brain.get_ecu(dependent_id)["metadata"]
        assert meta["last_challenged"] == old_stamp  # untouched


# ---------------------------------------------------------------------------
# §12.5 #9/#10 — throttle + repo-not-on-disk + skip paths
# ---------------------------------------------------------------------------

class TestThrottleAndSkips:
    def test_72h_throttle(self, brain, config, repo):
        """Grounding ran 10h ago on same repo → skipped; 80h ago → runs."""
        now = utcnow()
        first = run_grounding(brain, config, repo, now=now)
        assert "skipped" not in first.details

        recent = run_grounding(brain, config, repo,
                               now=now + timedelta(hours=10))
        assert recent.details["skipped"] == "throttled"
        assert recent.ecus_affected == 0

        later = run_grounding(brain, config, repo,
                              now=now + timedelta(hours=80))
        assert "skipped" not in later.details

    def test_throttle_is_per_repo(self, brain, config, tmp_path, repo):
        other = tmp_path / "other-project"
        other.mkdir()
        run_grounding(brain, config, repo)
        result = maintainer.task_grounding(brain, config,
                                           repo_path=str(other))
        assert "skipped" not in result.details   # different key → runs

    def test_throttle_stamped_even_with_zero_ecus(self, brain, config, repo):
        now = utcnow()
        run_grounding(brain, config, repo, now=now)          # nothing grounded
        again = maintainer.task_grounding(brain, config,
                                          repo_path=str(repo),
                                          now=now + timedelta(hours=1))
        assert again.details["skipped"] == "throttled"

    def test_repo_not_on_disk_skipped(self, brain, config, tmp_path):
        ghost = tmp_path / "ghost-repo"
        insert_grounded_ecu(brain, ghost)
        result = maintainer.task_grounding(brain, config,
                                           repo_path=str(ghost))
        assert result.details["skipped"] == "repo_not_on_disk"
        assert result.ecus_affected == 0
        # The throttle clock must NOT be stamped for a skipped repo.
        assert brain.get_maintenance_state(
            f"grounding_last_check:{ghost}") is None

    def test_no_repo_path_skipped(self, brain, config):
        result = maintainer.task_grounding(brain, config, repo_path=None)
        assert result.details["skipped"] == "no_repo"

    def test_other_repo_ecus_not_verified(self, brain, config, tmp_path,
                                          repo):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        foreign_id = insert_grounded_ecu(brain, elsewhere)
        os.remove(repo / "auth" / "token.py")     # irrelevant to foreign ECU
        result = run_grounding(brain, config, repo)
        assert result.details["checked"] == 0
        assert brain.get_ecu(foreign_id)["status"] == "active"

    def test_frozen_statuses_skipped(self, brain, config, repo):
        """Terminal/parked ECUs are not re-litigated by hygiene."""
        ids = {
            status: insert_grounded_ecu(brain, repo, cognition=cognition,
                                        status=status)
            for status, cognition in (
                ("superseded", "s"), ("deprecated", "d"),
                ("open_question", "oq"))
        }
        os.remove(repo / "auth" / "token.py")
        result = run_grounding(brain, config, repo)
        assert result.details["checked"] == 0
        for status, ecu_id in ids.items():
            assert brain.get_ecu(ecu_id)["status"] == status
        assert all(d["ecu"] not in set(ids.values())
                   for d in result.details["deprecated"])

    def test_empty_grounding_kept_alive(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(brain, repo, files=[], symbols=())
        result = run_grounding(brain, config, repo)
        assert brain.get_ecu(ecu_id)["status"] == "active"
        assert result.details["checked"] == 1


# ---------------------------------------------------------------------------
# commit-hash staleness — informational only (§3.3)
# ---------------------------------------------------------------------------

class TestCommitStaleness:
    @pytest.fixture()
    def git_repo(self, repo):
        subprocess.run(["git", "init"], cwd=str(repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(repo),
                       check=True, capture_output=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True,
            capture_output=True, text=True).stdout.strip()
        return repo, base

    def _advance(self, repo, n):
        for i in range(n):
            (repo / f"f{i}.txt").write_text("x\n")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=str(repo),
                           check=True, capture_output=True)

    def test_old_commit_flagged_in_metadata(self, brain, config, git_repo):
        repo, base = git_repo
        self._advance(repo, COMMIT_STALENESS_COMMITS + 5)
        ecu_id = insert_grounded_ecu(brain, repo, commit_hash=base)
        result = run_grounding(brain, config, repo)
        flagged = result.details["flagged_stale_commits"]
        assert flagged and flagged[0]["ecu"] == ecu_id
        assert brain.get_ecu(ecu_id)["metadata"]["stale_commit"] > \
            COMMIT_STALENESS_COMMITS
        assert brain.get_ecu(ecu_id)["status"] == "active"   # NEVER deprecated

    def test_recent_commit_not_flagged(self, brain, config, git_repo):
        repo, base = git_repo
        self._advance(repo, 2)
        ecu_id = insert_grounded_ecu(brain, repo, commit_hash=base)
        result = run_grounding(brain, config, repo)
        assert result.details["flagged_stale_commits"] == []
        assert "stale_commit" not in brain.get_ecu(ecu_id)["metadata"]

    def test_unknown_commit_hash_ignored(self, brain, config, repo):
        ecu_id = insert_grounded_ecu(
            brain, repo, commit_hash="deadbeefnotacommit")
        result = run_grounding(brain, config, repo)
        assert result.details["flagged_stale_commits"] == []

    def test_should_deprecate_scope_matrix(self):
        gone, total = ["a.py"], 2
        assert should_deprecate("engineering", gone, [], 2) is False
        assert should_deprecate("domain", gone, [], 2) is False
        assert should_deprecate("project", gone, [], 2) is False
        assert should_deprecate("organization", gone, [], 2) is False
        assert should_deprecate("repo", gone, [], 2) is True
        assert should_deprecate("module", gone, [], 2) is True
        assert should_deprecate("subsystem", [], [], 2) is False
        assert should_deprecate("subsystem", [], ["sym"], 1) is True
        assert should_deprecate("project", ["a.py", "b.py"], [], 2) is True
        with pytest.raises(ValueError):
            should_deprecate("galaxy", gone, [], 2)


# ---------------------------------------------------------------------------
# deprecation semantics (§21.3) + maintenance_log integration
# ---------------------------------------------------------------------------

class TestDeprecationSemantics:
    def test_edges_preserved_and_confidence_frozen(self, brain, config, repo):
        a_id = insert_grounded_ecu(brain, repo, cognition="a")
        b_id = insert_grounded_ecu(brain, repo, cognition="b",
                                   files=("auth/other.py",))
        brain.add_edge(a_id, b_id, "supports", confidence_delta=0.1)
        conf_before = brain.get_ecu(a_id)["confidence"]

        os.remove(repo / "auth" / "token.py")
        run_grounding(brain, config, repo)

        dead = brain.get_ecu(a_id)
        assert dead["status"] == "deprecated"
        assert dead["confidence"] == conf_before       # frozen at transition
        types = [e["type"] for e in brain.get_edges_for(a_id)]
        assert types == ["supports"]                   # audit trail intact

    def test_run_maintenance_logs_grounding_row(self, brain, config, repo):
        insert_grounded_ecu(brain, repo)
        os.remove(repo / "auth" / "token.py")
        maintainer.run_maintenance(brain, config, repo_path=str(repo))
        rows = brain.list_maintenance_log()
        grounding_rows = [r for r in rows if r["action"] == "grounding"]
        assert grounding_rows, "grounding task must log its own row"
        details = json.loads(grounding_rows[0]["details"])
        assert details["deprecated"][0]["missing_files"] == ["auth/token.py"]
        assert grounding_rows[0]["ecus_affected"] == 1
        state_key = f"grounding_last_check:{repo}"
        assert brain.get_maintenance_state(state_key) is not None

    def test_verdict_details_shape(self, brain, repo):
        ecu_id = insert_grounded_ecu(brain, repo, symbols=["GoneSymbol"])
        ecu = brain.get_ecu(ecu_id)
        verdict = evaluate_ecu(ecu, str(repo))
        details = verdict.to_details()
        assert details == {
            "ecu": ecu_id,
            "scope": "repo",
            "missing_files": [],
            "missing_symbols": ["GoneSymbol"],
            "stale_commit_behind": None,
            "deprecated": True,
            "challenged_dependents": [],
        }


# ---------------------------------------------------------------------------
# brain helpers
# ---------------------------------------------------------------------------

class TestBrainHelpers:
    def test_list_ecus_for_repo_filters_by_status_and_repo(
            self, brain, tmp_path, repo):
        here = insert_grounded_ecu(brain, repo)
        there = insert_grounded_ecu(brain, tmp_path / "elsewhere")
        superseded_here = insert_grounded_ecu(
            brain, repo, cognition="old", status="superseded")

        all_here = brain.list_ecus_for_repo(str(repo))
        assert {e["id"] for e in all_here} == {here, superseded_here}
        active_only = brain.list_ecus_for_repo(
            str(repo), statuses=["active"])
        assert [e["id"] for e in active_only] == [here]
        assert brain.list_ecus_for_repo(str(tmp_path / "elsewhere"))[0][
            "id"] == there

    def test_most_recent_active_session_repo(self, brain):
        assert brain.most_recent_active_session_repo() is None
        brain.create_session("/repos/a", "main")
        brain.create_session("/repos/b", "dev")
        assert brain.most_recent_active_session_repo() == "/repos/b"
        row = brain.get_active_session("/repos/b", "dev")
        brain.close_session(row["id"])
        assert brain.most_recent_active_session_repo() == "/repos/a"


# ---------------------------------------------------------------------------
# D35 wiring: session start + MCP server resolution
# ---------------------------------------------------------------------------

class TestSessionStartWiring:
    def test_start_session_runs_grounding_once(self, brain, config, repo):
        manager = SessionManager(brain, config=config)
        insert_grounded_ecu(brain, repo)
        manager.start_session(repo_path=str(repo), branch="main")

        rows = [r for r in brain.list_maintenance_log()
                if r["action"] == "grounding"]
        assert len(rows) == 1
        assert json.loads(rows[0]["details"])["checked"] == 1

        # Resuming within the window re-checks but the throttle skips.
        manager.start_session(repo_path=str(repo), branch="main")
        rows = [r for r in brain.list_maintenance_log()
                if r["action"] == "grounding"]
        assert len(rows) == 2
        assert json.loads(rows[0]["details"])["skipped"] == "throttled"  # newest

    def test_start_session_grounding_failure_never_blocks(self, brain,
                                                          config, repo):
        from unittest.mock import patch
        manager = SessionManager(brain, config=config)
        with patch.object(maintainer, "task_grounding",
                          side_effect=RuntimeError("boom")):
            payload = manager.start_session(repo_path=str(repo),
                                            branch="main")
        assert payload["status"] == "ok"

    def test_disabled_maintainer_no_grounding(self, brain, config, repo):
        cfg = make_config(enabled=False)
        manager = SessionManager(brain, config=cfg)
        manager.start_session(repo_path=str(repo), branch="main")
        assert [r for r in brain.list_maintenance_log()
                if r["action"] == "grounding"] == []


class TestMCPServerRepoResolution:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EC_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EC_BRANCH", "main")
        monkeypatch.chdir(tmp_path)
        assert mcp_server.resolve_maintainer_repo_path(None) == \
            str(tmp_path.resolve())

    def test_cwd_git_toplevel(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EC_REPO_PATH", raising=False)
        monkeypatch.delenv("EC_BRANCH", raising=False)
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True,
                       capture_output=True)
        monkeypatch.chdir(tmp_path)
        resolved = mcp_server.resolve_maintainer_repo_path(None)
        assert resolved == str(tmp_path.resolve())

    def test_falls_back_to_last_active_session(self, monkeypatch, tmp_path,
                                               brain):
        monkeypatch.delenv("EC_REPO_PATH", raising=False)
        monkeypatch.delenv("EC_BRANCH", raising=False)
        outside = tmp_path / "plain-dir"
        outside.mkdir()
        monkeypatch.chdir(outside)              # not a git repo
        brain.create_session("/repos/session-repo", "main")
        assert mcp_server.resolve_maintainer_repo_path(brain) == \
            "/repos/session-repo"

    def test_none_when_nothing_resolves(self, monkeypatch, tmp_path, brain):
        monkeypatch.delenv("EC_REPO_PATH", raising=False)
        monkeypatch.delenv("EC_BRANCH", raising=False)
        outside = tmp_path / "plain-dir-2"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert mcp_server.resolve_maintainer_repo_path(brain) is None

    def test_main_passes_repo_to_start_maintenance(self, monkeypatch,
                                                   tmp_path):
        """The startup path threads the resolved repo into
        start_maintenance (D35 end-to-end)."""
        monkeypatch.setenv("EC_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("EC_BRANCH", "main")
        captured = {}

        class FakeServer:
            def __init__(self):
                self._brain = None
                self._manager = None
                self._embedding_model = None
                self._maintainer_thread = None
                self._config = make_config(enabled=False)

            def _ensure_brain(self):
                return None

            def start_maintenance(self, repo_path=None):
                captured["repo_path"] = repo_path
                return None

            def stop_maintenance(self):
                pass

        fake = FakeServer()
        with patch.object(mcp_server, "ECServer", lambda: fake), \
                patch.object(mcp_server.sys, "stdin", io.StringIO("")), \
                patch.object(mcp_server.sys, "stdout", io.StringIO()):
            mcp_server.main()
        assert captured["repo_path"] == str(tmp_path.resolve())
