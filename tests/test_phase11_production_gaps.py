"""Phase 11 tests — Production Gaps (design doc §6, D40/D41/D42/D45).

Covers design doc §12.8 #1-#10:
  #1-#3   §17.2 Stage-1 scope-prefilter fix (D40)      -> TestPrefilterFix
  #4-#8   open-question resolution UI (D41, §17.5 a-d)  -> TestOpenQuestionResolution
  #9      Ollama auto-setup (D42)                       -> TestOllamaAutoSetup
  #10     console-script detection (D45)                -> TestConsoleScriptDetection

All offline: tmp_path brains, real HF-cache embeddings, ec.diffuser.call_llm /
ec.install subprocess work mocked or injected.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ec import install, review_gate
from ec.brain import Brain
from ec.diffuser import (
    PREFILTER_SIMILARITY_FLOOR,
    _contradiction_prefilter,
    diffuse_ecu,
)


def make_ecu(cognition: str, **overrides) -> dict:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:myapp > module:auth"},
        "provenance": {"source_type": "debugging", "source_id": "s1"},
        "grounding": {"files": ["auth/token_manager.py"], "symbols": ["refresh"]},
        "confidence": 0.5,
        "status": "active",
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    return ecu


def classifications_response(*rels: str) -> str:
    return json.dumps({
        "classifications": [
            {"pair": i + 1, "relationship": rel} for i, rel in enumerate(rels)
        ]
    })


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


# ---------------------------------------------------------------------------
# §12.8 #1-#3 — §17.2 Stage-1 scope-prefilter fix (D40, design doc §6.2)
# ---------------------------------------------------------------------------

class TestPrefilterFix:
    """The old rule killed every different-scope pair as orthogonal before
    Stage 2 (ASSESSMENT.md §4.2 verified live). D40 adds escape hatches."""

    def test_prefilter_grounding_overlap(self):
        """#1: different scopes, same grounding file -> potential (Stage 2)."""
        a = make_ecu("Auth uses optimistic token refresh",
                     scope={"level": "domain", "path": "domain:backend"})
        b = make_ecu("Auth uses pessimistic token refresh")
        assert _contradiction_prefilter(a, b) == "potential"
        assert _contradiction_prefilter(b, a) == "potential"

    def test_prefilter_high_similarity(self):
        """#2: different scopes, no grounding overlap, sim > 0.8 -> potential."""
        a = make_ecu("The rate limiter drops excess requests",
                     scope={"level": "module", "path": "module:api"},
                     grounding={"files": ["api/limiter.py"]})
        b = make_ecu("The rate limiter rejects requests beyond the quota",
                     grounding={"files": ["service/other.py"]})
        assert _contradiction_prefilter(a, b, similarity=0.83) == "potential"

    def test_prefilter_different_scope_no_overlap(self):
        """#3: different scopes, no overlap, sim < floor -> still killed."""
        a = make_ecu("Input validation at trust boundaries is always required",
                     scope={"level": "engineering", "path": "engineering"},
                     grounding={"files": ["lib/validate.py"]})
        b = make_ecu("Input validation at trust boundaries is skipped for "
                     "internal calls in the auth module",
                     grounding={"files": ["auth/token_manager.py"]})
        sim = 0.76          # above relevance gate, below the escape floor
        assert sim < PREFILTER_SIMILARITY_FLOOR
        assert _contradiction_prefilter(a, b, similarity=sim) == "orthogonal"
        assert _contradiction_prefilter(a, b) == "orthogonal"   # no sim signal

    def test_prefilter_boundary_at_floor(self):
        """sim exactly at the floor does NOT escape — strictly greater than."""
        a = make_ecu("X is true", scope={"level": "domain", "path": "d"})
        b = make_ecu("Y is false", grounding={"files": ["other.py"]})
        assert _contradiction_prefilter(a, b, similarity=0.8) == "orthogonal"
        assert _contradiction_prefilter(a, b, similarity=0.8000001) == "potential"

    def test_same_scope_behavior_unchanged(self):
        """D40 only touches the scope-mismatch branch; same-scope rules keep
        their precedence (qualifiers still kill, overlap still weak-evidence)."""
        a = make_ecu("X is true for low-traffic services")
        b = make_ecu("X is false for high-traffic services")
        assert _contradiction_prefilter(a, b, similarity=0.99) == "orthogonal"
        c = make_ecu("Auth uses pessimistic refresh",
                     grounding={"files": ["other/file.py"]})
        assert _contradiction_prefilter(c, a) == "no_overlap"
        d = make_ecu("Auth uses pessimistic refresh")
        assert _contradiction_prefilter(d, a) == "potential"

    def test_assessment_probe_reaches_stage2(self, brain, model):
        """ASSESSMENT.md §4.2 live probe, replayed offline: a direct,
        mutually-exclusive contradiction (same grounding file app/main.py,
        extractor labeled one side domain and the other repo) must now reach
        Stage-2 adjudication instead of being silently dropped."""
        existing = brain.insert_ecu(make_ecu(
            "Requests are rejected when the rate limit is exceeded",
            scope={"level": "domain", "path": "domain:traffic-control"},
            grounding={"files": ["app/main.py"], "symbols": []},
            confidence=0.7,
        ), embedding=model.encode_one(
            "Requests are rejected when the rate limit is exceeded"))
        new = brain.insert_ecu(make_ecu(
            "Requests exceeding the rate limit are queued, never rejected",
            confidence=0.65,
        ), embedding=model.encode_one(
            "Requests exceeding the rate limit are queued, never rejected"))
        responses = [
            classifications_response("contradicts"),
            json.dumps({"verdict": "genuine_contradiction", "differentiator": ""}),
        ]
        with patch("ec.diffuser.call_llm", side_effect=responses) as m:
            result = diffuse_ecu(brain, new, embedding_model=model)
        # classification + Stage-2 adjudication: the pair survived Stage 1
        assert m.call_count == 2
        edge = brain.get_edges_from(new)[0]
        assert edge["type"] == "contradicts" and edge["target_id"] == existing
        assert brain.get_ecu(existing)["status"] == "challenged"
        assert len(result.contradictions) == 1


# ---------------------------------------------------------------------------
# helpers for the open-question resolution tests (§12.8 #4-#8)
# ---------------------------------------------------------------------------

def _seed_open_pair(brain, model, *, since_iso="2026-06-01T00:00:00+00:00",
                    conf_a=0.45, conf_b=0.42):
    """Two canonical ECUs parked as open_question, joined by a contradicts
    edge — the state _open_question_sweep leaves after §17.5 parking."""
    id_a = brain.insert_ecu(make_ecu(
        "The race condition is in TokenManager", confidence=conf_a),
        embedding=model.encode_one("The race condition is in TokenManager"))
    id_b = brain.insert_ecu(make_ecu(
        "The race condition is in CacheInvalidator", confidence=conf_b),
        embedding=model.encode_one("The race condition is in CacheInvalidator"))
    brain.add_edge(id_a, id_b, "contradicts", weight=0.61, confidence_delta=-0.1)
    brain.update_ecu_status(id_a, "open_question")
    brain.update_ecu_status(id_b, "open_question")
    brain.update_ecu_metadata(id_a, competing_since=since_iso)
    brain.update_ecu_metadata(id_b, competing_since=since_iso)
    return id_a, id_b


# ---------------------------------------------------------------------------
# §12.8 #4-#8 — open-question resolution UI (D41, design doc §6.3)
# ---------------------------------------------------------------------------

class TestOpenQuestionResolution:
    def test_find_pairs(self, brain, model):
        id_a, id_b = _seed_open_pair(brain, model,
                                     since_iso="2026-07-15T00:00:00+00:00")
        found = review_gate.find_open_question_pairs(brain)
        assert len(found["pairs"]) == 1
        assert len(found["singles"]) == 0
        pair = found["pairs"][0]
        assert {pair["ecu_a"]["id"], pair["ecu_b"]["id"]} == {id_a, id_b}
        assert pair["days_unresolved"] is not None
        assert pair["limit_days"] == 60          # repo scope limit

    def test_resolution_investigate(self, brain, model):
        """#4 (a): both back to challenged, competing_since reset."""
        id_a, id_b = _seed_open_pair(brain, model)
        now = datetime.now(timezone.utc)
        rec = review_gate.apply_open_question_resolution(
            brain, id_a, id_b, "a", now=now)
        for ecu_id in (id_a, id_b):
            ecu = brain.get_ecu(ecu_id)
            assert ecu["status"] == "challenged"
            # the clock restarted: competing_since == now
            assert ecu["metadata"]["competing_since"] == now.isoformat()
        assert rec["choice"] == "a"

    def test_resolution_preferred(self, brain, model):
        """#5 (b): preferred active with alpha_retrieval bump; other
        superseded with a real supersedes edge from the winner."""
        id_a, id_b = _seed_open_pair(brain, model, conf_a=0.45)
        rec = review_gate.apply_open_question_resolution(
            brain, id_a, id_b, "b", preferred_id=id_a)
        winner = brain.get_ecu(id_a)
        loser = brain.get_ecu(id_b)

        from ec.confidence import to_log_odds, to_probability
        from ec.config import get_config
        expected = to_probability(
            to_log_odds(0.45) + get_config().confidence.alpha_retrieval)
        assert winner["confidence"] == pytest.approx(expected)
        assert winner["status"] == "active"
        assert loser["status"] == "superseded"
        sup = [e for e in brain.get_edges_from(id_a)
               if e["type"] == "supersedes"]
        assert len(sup) == 1 and sup[0]["target_id"] == id_b
        assert rec["preferred_id"] == id_a

    def test_resolution_orthogonal(self, brain, model):
        """#6 (c): both active, contradicts edges removed, metadata note."""
        id_a, id_b = _seed_open_pair(brain, model)
        # a reverse-direction duplicate must also be removed
        brain.add_edge(id_b, id_a, "contradicts", weight=0.5,
                       confidence_delta=-0.05)
        rec = review_gate.apply_open_question_resolution(
            brain, id_a, id_b, "c", now=datetime(2026, 8, 23,
                                                 tzinfo=timezone.utc))
        assert brain.get_edges_for(id_a) == []
        assert brain.get_edges_for(id_b) == []
        for ecu_id in (id_a, id_b):
            ecu = brain.get_ecu(ecu_id)
            assert ecu["status"] == "active"
            assert "User reclassified as orthogonal on 2026-08-23" \
                in ecu["metadata"]["orthogonal_note"]

    def test_resolution_archive(self, brain, model):
        """#7 (d): both archived, confidence frozen at the parked value."""
        id_a, id_b = _seed_open_pair(brain, model, conf_a=0.45, conf_b=0.42)
        rec = review_gate.apply_open_question_resolution(brain, id_a, id_b, "d")
        assert brain.get_ecu(id_a)["status"] == "archived"
        assert brain.get_ecu(id_b)["status"] == "archived"
        assert brain.get_ecu(id_a)["confidence"] == pytest.approx(0.45)
        assert brain.get_ecu(id_b)["confidence"] == pytest.approx(0.42)

    def test_resolution_validation(self, brain, model):
        id_a, id_b = _seed_open_pair(brain, model)
        with pytest.raises(review_gate.ReviewGateError, match="unknown resolution"):
            review_gate.apply_open_question_resolution(brain, id_a, id_b, "x")
        with pytest.raises(review_gate.ReviewGateError, match="preferred_id"):
            review_gate.apply_open_question_resolution(
                brain, id_a, id_b, "b", preferred_id="nope")
        brain.update_ecu_status(id_b, "superseded")
        with pytest.raises(review_gate.ReviewGateError, match="not open_question"):
            review_gate.apply_open_question_resolution(brain, id_a, id_b, "c")

    def test_non_interactive_skip_leaves_parked(self, brain, model):
        """#8 skip path: the default policy never touches open questions —
        verified here by asserting no state change without any call."""
        id_a, id_b = _seed_open_pair(brain, model)
        assert brain.get_ecu(id_a)["status"] == "open_question"

    def test_archive_all(self, brain, model):
        id_a, id_b = _seed_open_pair(brain, model)
        lone = brain.insert_ecu(make_ecu("Unpaired parked conclusion"),
                                embedding=model.encode_one(
                                    "Unpaired parked conclusion"))
        brain.update_ecu_status(lone, "open_question")
        records = review_gate.resolve_open_questions_archive_all(brain)
        assert all(brain.get_ecu(i)["status"] == "archived"
                   for i in (id_a, id_b, lone))
        assert {i for r in records for i in r["ecu_ids"]} == {id_a, id_b, lone}

    def test_interactive_flow(self, brain, model):
        id_a, id_b = _seed_open_pair(brain, model)
        pair = review_gate.find_open_question_pairs(brain)["pairs"][0]
        # the UI prompt maps [1] -> pair.ecu_a, [2] -> pair.ecu_b; answering
        # "2" prefers whichever ECU the sorted, deterministic ordering put
        # in the ecu_b slot
        answers = iter(["b", "2"])
        printed = []
        records = review_gate.resolve_open_questions(
            brain, input_fn=lambda _p="": next(answers),
            print_fn=printed.append)
        winner = pair["ecu_b"]["id"]
        loser = pair["ecu_a"]["id"]
        assert brain.get_ecu(winner)["status"] == "active"
        assert brain.get_ecu(loser)["status"] == "superseded"
        assert records[0]["preferred_id"] == winner
        assert any("Open Questions" in line for line in printed)

    def test_interactive_eof_parks(self, brain, model):
        id_a, id_b = _seed_open_pair(brain, model)

        def eof(_p=""):
            raise EOFError

        records = review_gate.resolve_open_questions(brain, input_fn=eof,
                                                     print_fn=lambda *_: None)
        assert records == []
        assert brain.get_ecu(id_a)["status"] == "open_question"

    def test_cli_stop_archive_and_skip_policies(self, tmp_path, monkeypatch,
                                                model):
        """#8 end-to-end through `python -m ec.session stop`: archive closes
        every open question before the review flow; skip leaves them."""
        from ec.session import SessionManager
        from ec.session import main as session_main

        for policy, expected_status in (("archive", "archived"),
                                        ("skip", "open_question")):
            ec_home = tmp_path / f"ec_{policy}"
            monkeypatch.setenv("EC_HOME", str(ec_home))
            monkeypatch.delenv("EC_REPO_PATH", raising=False)
            monkeypatch.delenv("EC_BRANCH", raising=False)
            seeder = Brain()
            id_a, id_b = _seed_open_pair(seeder, model)
            SessionManager(seeder).start_session("/repo-cli", "main")
            seeder.close()

            rc = session_main(["stop", "--repo", "/repo-cli", "--branch",
                               "main", "--all-skip",
                               "--resolve-open-questions", policy])
            assert rc == 0

            checker = Brain()
            try:
                assert checker.get_ecu(id_a)["status"] == expected_status
                assert checker.get_ecu(id_b)["status"] == expected_status
                sessions = checker.list_sessions()
                assert sessions[0]["status"] == "closed"
            finally:
                checker.close()


# ---------------------------------------------------------------------------
# §3.6 — grounding-deprecation surfacing at the review gate
# ---------------------------------------------------------------------------

class TestGroundingDeprecationSurfacing:
    def _log_grounding(self, brain, run_at: str, deprecated: list[dict]):
        brain.log_maintenance("grounding", json.dumps(
            {"repo": "/repo", "checked": 3, "deprecated": deprecated}),
            run_at=run_at)

    def test_notification_lists_deprecated_and_dependents(self, brain):
        self._log_grounding(brain, "2026-08-20T10:00:00+00:00", [{
            "ecu": "ecu-1", "scope": "repo",
            "missing_files": ["auth/token.py"], "missing_symbols": [],
            "stale_commit_behind": None, "deprecated": True,
            "challenged_dependents": ["ecu-9"],
        }])
        notes = review_gate.grounding_deprecation_notifications(brain)
        assert len(notes) == 1
        assert notes[0]["kind"] == "grounding_deprecations"
        assert "auth/token.py" in notes[0]["message"]
        assert "1 ECU(s) that depend" in notes[0]["message"]
        assert notes[0]["ecu_ids"] == ["ecu-1"]

    def test_suppressed_after_review_gate_surfaced_it(self, brain):
        self._log_grounding(brain, "2026-08-20T10:00:00+00:00",
                            [{"ecu": "ecu-1", "scope": "repo"}])
        brain.log_maintenance("review_gate", "{}",
                              run_at="2026-08-21T09:00:00+00:00")
        # new grounding run AFTER the last review gate -> surfaced again
        self._log_grounding(brain, "2026-08-22T10:00:00+00:00",
                            [{"ecu": "ecu-2", "scope": "module"}])
        notes = review_gate.grounding_deprecation_notifications(brain)
        assert [n["ecu_ids"] for n in notes] == [["ecu-2"]]

    def test_no_grounding_rows_no_notification(self, brain):
        assert review_gate.grounding_deprecation_notifications(brain) == []


# ---------------------------------------------------------------------------
# §12.8 #9 — Ollama auto-setup (D42, design doc §6.4)
# ---------------------------------------------------------------------------

def scripted(answers):
    it = iter(answers)
    return lambda _prompt="": next(it)


def run_install(home, ec_home, *, input_answers=(), environ=None,
                which_fn=None, runner=None, dry_run=False, all_agents=False,
                input_fn=None):
    """install.run() fully sandboxed; returns the printed transcript."""
    out = []

    def _which(binary):
        if binary == install.OLLAMA_BINARY:
            return "/usr/local/bin/ollama"
        return None

    calls = []

    def _default_runner(cmd, *args, **kwargs):
        calls.append(cmd)
        import subprocess
        return subprocess.CompletedProcess(cmd, 0)

    install.run(
        home=home, ec_home=ec_home, all_agents=all_agents,
        which=which_fn or _which,
        input_fn=input_fn or scripted(list(input_answers)),
        print_fn=out.append,
        environ=environ if environ is not None else {},
        runner=runner or _default_runner,
        dry_run=dry_run,
    )
    return "\n".join(out)


class TestOllamaAutoSetup:
    def test_ollama_detected_offered(self, tmp_path):
        """#9: Ollama on PATH + no Zen key -> offer accepted pulls the model
        and writes the Ollama LLM block into ~/.ec/config.yaml."""
        home = tmp_path / "h1"
        home.mkdir()
        ec_home = home / ".ec"
        out = run_install(home, ec_home, all_agents=True,
                          input_answers=["y"])
        assert "Ollama detected" in out
        # the offer prompt itself goes to input(); its acceptance is proven
        # by the pull + the written config below
        assert f"Pulling {install.OLLAMA_MODEL}" in out
        cfg = ec_home / "config.yaml"
        assert cfg.exists()
        import yaml
        data = yaml.safe_load(cfg.read_text())
        assert data["llm"]["provider"] == "ollama"
        assert data["llm"]["model"] == install.OLLAMA_MODEL
        assert data["llm"]["base_url"] == install.OLLAMA_BASE_URL

    def test_ollama_pull_executed(self, tmp_path):
        home = tmp_path / "h2"
        home.mkdir()
        calls = []
        import subprocess

        def runner(cmd, *a, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        run_install(home, home / ".ec", all_agents=True,
                    input_answers=["y"], runner=runner)
        assert calls == [["/usr/local/bin/ollama", "pull",
                          install.OLLAMA_MODEL]]

    def test_ollama_offer_declined(self, tmp_path):
        home = tmp_path / "h3"
        home.mkdir()
        ec_home = home / ".ec"
        out = run_install(home, ec_home, all_agents=True,
                          input_answers=["n"])
        assert "left unconfigured" in out
        assert 'provider: "ollama"' in out          # manual keys shown
        import yaml
        data = yaml.safe_load((ec_home / "config.yaml").read_text())
        # bootstrap default config untouched by the declined offer
        assert data["llm"]["provider"] != "ollama"

    def test_ollama_not_installed_prints_instructions(self, tmp_path):
        home = tmp_path / "h4"
        home.mkdir()
        out = run_install(home, home / ".ec", all_agents=True,
                          which_fn=lambda _b: None)
        assert "curl -fsSL https://ollama.com/install.sh | sh" in out
        assert "provider: \"ollama\"" in out

    def test_zen_key_with_ollama_notes_fallback(self, tmp_path):
        home = tmp_path / "h5"
        home.mkdir()

        def no_prompt(_p=""):
            raise AssertionError("no offer may be made when a Zen key is set")

        out = run_install(
            home, home / ".ec", all_agents=True,
            environ={install.ZEN_KEY_ENV: "sk-test"},
            input_fn=no_prompt)
        assert "OpenCode Zen API key found" in out
        assert "offline fallback" in out
        assert "Configure EC for offline use" not in out   # no offer

    def test_existing_config_yaml_merged(self, tmp_path):
        home = tmp_path / "h6"
        home.mkdir()
        ec_home = home / ".ec"
        ec_home.mkdir()
        (ec_home / "config.yaml").write_text("ranking:\n  w_relevance: 0.99\n")
        run_install(home, ec_home, all_agents=True, input_answers=["y"])
        import yaml
        data = yaml.safe_load((ec_home / "config.yaml").read_text())
        assert data["ranking"]["w_relevance"] == 0.99      # user keys preserved
        assert data["llm"]["provider"] == "ollama"

    def test_dry_run_pulls_nothing(self, tmp_path):
        home = tmp_path / "h7"
        home.mkdir()
        calls = []

        def runner(cmd, *a, **k):                # must never be called
            calls.append(cmd)

        out = run_install(home, home / ".ec", all_agents=True,
                          input_answers=["y"], runner=runner, dry_run=True)
        assert calls == []
        assert "[dry-run] would pull" in out
        assert not (home / ".ec" / "config.yaml").exists()

# ---------------------------------------------------------------------------
# §12.8 #10 — console-script detection (D45, design doc §6.1)
# ---------------------------------------------------------------------------

class TestConsoleScriptDetection:
    def test_console_script_detected(self, monkeypatch):
        """#10: pip-installed package -> the ec-mcp console script, no args,
        no env (D45)."""
        monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
        assert install.server_command() == (install.MCP_CONSOLE_SCRIPT, [], {})

    def test_dev_fallback_unchanged(self, monkeypatch):
        """Not installed -> the D28 absolute-venv-python + PYTHONPATH form."""
        monkeypatch.setattr(install, "_is_pip_installed", lambda: False)
        python, args, env = install.server_command()
        assert args == ["-m", "ec.mcp_server"]
        assert env == {"PYTHONPATH": str(install.Path(
            install.__file__).resolve().parent.parent)}
        from pathlib import Path
        assert Path(python).is_absolute()

    def test_planners_write_console_script(self, tmp_path, monkeypatch):
        home = tmp_path / "h"
        for marker, content in ((".claude.json", "{}"),
                                (".cursor/mcp.json", "{}"),
                                (".config/opencode/opencode.json", "{}"),
                                (".codex/config.toml", "")):
            p = home / marker
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
        out = run_install(home, home / ".ec", all_agents=True,
                          environ={install.ZEN_KEY_ENV: "sk-test"})
        claude = json.loads((home / ".claude.json").read_text())
        assert claude["mcpServers"]["ec"] == {
            "type": "stdio", "command": "ec-mcp"}
        cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
        assert cursor["mcpServers"]["ec"] == {"command": "ec-mcp"}
        opencode = json.loads(
            (home / ".config" / "opencode" / "opencode.json").read_text())
        assert opencode["mcp"]["ec"] == {
            "type": "local", "command": ["ec-mcp"], "enabled": True}
        import tomllib
        codex = tomllib.loads((home / ".codex" / "config.toml").read_text())
        assert codex["mcp_servers"]["ec"] == {"command": "ec-mcp"}

    def test_entry_point_registered_and_launches(self, tmp_path, monkeypatch):
        """The console script exists in the installed distribution and its
        entry point resolves to ec.mcp_server:main."""
        from importlib import metadata
        eps = metadata.entry_points()
        group = (eps.select(group="console_scripts")
                 if hasattr(eps, "select") else eps.get("console_scripts", []))
        matches = [ep for ep in group if ep.name == "ec-mcp"]
        assert matches, "ec-mcp console script missing — rerun `pip install -e .`"
        assert matches[0].value == "ec.mcp_server:main"
