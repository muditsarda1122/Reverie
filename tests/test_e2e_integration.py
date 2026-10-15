"""End-to-end integration tests.

Two layers live in this module:

1. The LIVE full-pipeline test (test_e2e_full_pipeline), rewritten for the
   feat/flow product flow (SPEC §29): repo enablement, automatic sessions,
   the cross-session review queue, and sessions that are never closed.
   Skips without a provider key.

2. OFFLINE extended E2E tests (Phase 12 — design doc §11 "Integration"):
   the v2 components (Maintainer, grounding, reconsolidation, clustering,
   controlled forgetting) wired together across component boundaries —
   session + MCP server + brain + maintenance — with mocked/no LLM. These
   run everywhere; see TestE2EOffline below.

The LIVE test's flow (steps a–g):

  a. fresh brain (REVERIE_HOME=/tmp/ec_e2e_test); the repo is enabled via the
     real `reverie enable` CLI subprocess (repo /tmp/fastapi, branch main) —
     NO session is created by enabling
  b. the first reverie_observe auto-creates the session (D56)
  c. a realistic 2-paragraph FastAPI routing investigation through reverie_observe
     (extract -> store -> lightweight diffuse, all live LLM calls)
  d. ECUs extracted + stored in the session brain
  e. edges created (session edges / pending updates during observe — reported;
      firm supports/depends_on assertion on CANONICAL edges after promotion)
  f. reverie_query "How does FastAPI routing work?" — groups contain the
     extracted ECUs; the relevance gate filters a deliberately irrelevant
     session ECU; activation normalized; §11.8 formatted structure
  g. review through the review queue: reverie_review_list → reverie_review_decide,
     decisions applied, the agent channel recorded, tokens single-use;
     accepted ECUs promoted to the Canonical Brain (full diffuser)
  h. retrieval in a LATER conversation on the same branch: the SAME session
     is resumed (never closed) and the promoted canonical ECUs surface
  i. a branch switch picks the OTHER branch's session while the main
     session stays active and untouched
  j. cross-session persistence (ids survive promotion + a fresh DB connection)

The LIVE test runs against whichever backend is available — a hosted
provider when a key is set, otherwise a local Ollama serving
qwen2.5-coder:14b (a test config points the LLM at it; no key needed) —
and skips when neither is there.
Run live:   .venv/bin/python -m pytest tests/test_e2e_integration.py -v -s
Run offline: .venv/bin/python -m pytest tests/test_e2e_integration.py -v -k offline
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reverie.brain import Brain
from reverie.config import load_config
from reverie.llm import api_key
from reverie.mcp_server import ReverieServer
from reverie.session import SessionManager

ROOT = Path(__file__).resolve().parent.parent
REVERIE_HOME_DIR = Path("/tmp/ec_e2e_test")
REPO, BRANCH = "/tmp/fastapi", "main"
# detect_repo_branch() canonicalizes via Path.resolve() (macOS /tmp is a
# symlink to /private/tmp) — direct DB lookups must use the resolved path.
RESOLVED_REPO = str(Path(REPO).resolve())
QUERY = "How does FastAPI routing work?"

# --- the live backend -------------------------------------------------------
#
# The pipeline runs against whichever backend is available:
#   hosted — a provider key is set (any registry provider); every
#            model-quality assertion stays at full strength;
#   ollama — no key, but a local Ollama serves the qwen2.5-coder:14b
#            fallback (D42); a test config points the LLM at it, so the
#            whole pipeline is exercised with no API key at all;
#   none   — skip.
#
# Model-quality-sensitive assertions are guarded by STRICT (hosted only);
# in ollama mode each guarded step asserts what it really needs (the
# mechanism ran, data landed, the queue drained) — every such guard is
# listed in the PR description. Nothing else is relaxed.

OLLAMA_MODEL = "qwen2.5-coder:14b"
HOSTED = bool(api_key())


def _ollama_ready() -> bool:
    """True when a local Ollama serves the fallback model (no key needed)."""
    if HOSTED:
        return False
    import requests
    try:
        r = requests.get("http://localhost:11434/v1/models", timeout=3)
        models = r.json().get("data", [])
        return any(m.get("id") == OLLAMA_MODEL for m in models)
    except Exception:                    # not running / not installed
        return False


OLLAMA = _ollama_ready()
STRICT = HOSTED

requires_live_backend = pytest.mark.skipif(
    not HOSTED and not OLLAMA,
    reason="no provider key and no local Ollama serving "
           f"{OLLAMA_MODEL} — nothing to run the live pipeline against")

# --- fixtures ---------------------------------------------------------------

# A realistic 2-paragraph agent response about a FastAPI routing
# investigation. Written to contain several mutually-supporting engineering
# conclusions (verified MiniLM sims: conclusions ~0.8 vs the seed ECU).
USER_PROMPT = (
    "My GET /items/latest endpoint returns 422 Unprocessable Entity instead "
    "of the item named 'latest'. What's going on?"
)

REASONING_TRACE = (
    "I started by reading the route table in app/routers/items.py. The router "
    "declares @router.get('/items/{item_id}') at line 12 and "
    "@router.get('/items/latest') at line 48. FastAPI compiles each route "
    "path to a regex and matches incoming requests against routes in the "
    "exact order they were declared — the first match wins. Because the "
    "parameterized route /items/{item_id} is declared first, the request "
    "path '/items/latest' matches it, and FastAPI tries to parse 'latest' "
    "as item_id. The handler declares item_id: int, so validation fails "
    "with a 422 before the /items/latest handler is ever reached. I "
    "confirmed this by printing app.routes ordering and by swapping the "
    "declarations in a scratch app — /items/latest then resolved correctly.\n"
    "\n"
    "The root cause is route declaration ordering: in FastAPI (via "
    "Starlette), overlapping routes are shadowed by whichever matching route "
    "was declared earlier, so static paths must be declared before "
    "parameterized routes that can capture the same path segment. This is a "
    "structural property of the framework's sequential matching, not a bug "
    "in our validation code. The fix is to move @router.get('/items/latest') "
    "above @router.get('/items/{item_id}') in app/routers/items.py. A "
    "durable rule for this codebase: when adding a static path under a "
    "prefix that already has a parameterized route, always declare the "
    "static route first, and treat route order as part of the API contract."
)

FINAL_OUTPUT = (
    "Moved the /items/latest route above /items/{item_id} in "
    "app/routers/items.py. Verified: GET /items/latest now returns 200 and "
    "GET /items/42 still routes to the item handler."
)

# Prior reviewed knowledge (simulates a brain with history). Confidence 0.65
# sits above theta_supersede (0.3), so an LLM 'supersedes' proposal would
# downgrade to supports (D8) — the edge type assertion stays meaningful.
SEED_ECU = {
    "cognition": (
        "FastAPI (Starlette) matches routes sequentially in declaration "
        "order, so an earlier parameterized route can shadow a later static "
        "path."
    ),
    "conclusion_type": "invariant",
    "scope": {"level": "repo", "path": "repo:fastapi > module:routers"},
    "provenance": {"source_type": "debugging"},
    "grounding": {"files": ["app/routers/items.py"], "symbols": ["app.routes"]},
    "confidence": 0.65,
    "status": "active",
    "evidence_pointers": ["prior session review"],
}

# Deliberately irrelevant ECU for the relevance-gate check (verified sim to
# QUERY: 0.021 — far below the 0.3 gate).
OFFTOPIC_ECU = {
    "cognition": (
        "PostgreSQL autovacuum scale factors must be tuned per table based "
        "on churn rate to prevent table bloat."
    ),
    "conclusion_type": "invariant",
    "scope": {"level": "engineering", "path": "engineering"},
    "provenance": {"source_type": "session"},
    "grounding": {"files": ["db/maintenance.sql"]},
    "confidence": 0.5,
    "status": "active",
    "evidence_pointers": ["fixture"],
}


@pytest.fixture(scope="module")
def model():
    from reverie.embeddings import get_embedding_model
    return get_embedding_model()


def _cli_env() -> dict:
    env = dict(os.environ)
    env.update({
        "REVERIE_HOME": str(REVERIE_HOME_DIR),
        "REVERIE_REPO_PATH": REPO,
        "REVERIE_BRANCH": BRANCH,
        "PYTHONPATH": str(ROOT),
        "HF_HUB_OFFLINE": "1",
    })
    return env


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "reverie.session", *args],
        capture_output=True, text=True, env=_cli_env(), cwd=ROOT, timeout=300,
    )


def _run_ec_cli(*args: str) -> subprocess.CompletedProcess:
    """A real `ec <subcommand>` through the multi-command CLI."""
    return subprocess.run(
        [sys.executable, "-m", "reverie.cli", *args],
        capture_output=True, text=True, env=_cli_env(), cwd=ROOT, timeout=300,
    )


def _call_tool(server: ReverieServer, name: str, arguments: dict | None = None) -> dict:
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _group_ecu_ids(payload: dict) -> set[str]:
    ids = set()
    for group in payload.get("groups", []):
        ids.add(group["core_ecu"]["id"])
        for slot in ("supporting_evidence", "contradictions",
                     "dependencies", "superseded_by"):
            for item in group.get(slot, []):
                ids.add(item["id"])
    return ids


# --- the end-to-end flow ------------------------------------------------------

@requires_live_backend
def test_e2e_full_pipeline(model, monkeypatch, capsys):
    # (a) fresh brain at REVERIE_HOME=/tmp/ec_e2e_test; enable the repo via the
    # real `reverie enable` CLI subprocess --------------------------------------
    shutil.rmtree(REVERIE_HOME_DIR, ignore_errors=True)
    monkeypatch.setenv("REVERIE_HOME", str(REVERIE_HOME_DIR))
    monkeypatch.setenv("REVERIE_REPO_PATH", REPO)
    monkeypatch.setenv("REVERIE_BRANCH", BRANCH)

    # the LLM backend: hosted when a key is set, else the local Ollama
    # fallback via a test config written into REVERIE_HOME (picked up by every
    # component through load_config; passed explicitly to dodge the
    # process-wide config cache)
    if OLLAMA:
        import yaml
        from reverie.install import OLLAMA_LLM_BLOCK
        REVERIE_HOME_DIR.mkdir(parents=True, exist_ok=True)
        (REVERIE_HOME_DIR / "config.yaml").write_text(
            yaml.safe_dump({"llm": {**OLLAMA_LLM_BLOCK, "timeout": 300}}))
        print("[cfg] LLM backend: local Ollama "
              f"({OLLAMA_MODEL}) — no API key")
        cfg = load_config(REVERIE_HOME_DIR / "config.yaml")
    else:
        cfg = load_config()
        print(f"[cfg] LLM backend: hosted provider '{cfg.llm.provider}' "
              f"({cfg.llm.model})")

    brain = Brain()  # $REVERIE_HOME/reverie.db
    assert brain.count_ecus() == 0
    # one prior reviewed ECU so edge creation has a canonical target
    seed_id = brain.insert_ecu(
        SEED_ECU, embedding=model.encode_one(SEED_ECU["cognition"]))

    manager = SessionManager(brain, config=cfg, embedding_model=model)
    server = ReverieServer(brain=brain, manager=manager, config=cfg,
                      embedding_model=model)
    print("\n[a] fresh brain at", REVERIE_HOME_DIR, "| seed ECU:", seed_id)

    proc = _run_ec_cli("enable")
    assert proc.returncode == 0, proc.stderr
    assert "Reverie is on for" in proc.stdout
    assert "Sessions are automatic per branch" in proc.stdout
    # Enabling no longer creates a session — sessions are automatic per
    # branch and appear on the first tool call (D56).
    assert brain.get_active_session(RESOLVED_REPO, BRANCH) is None
    print("[a] repo enabled via `reverie enable`; no session yet (automatic)")

    # (b) the FIRST reverie_observe auto-creates the session (D56) ----------------
    # Ollama sampling variance (temperature 0.3): a 14B local model
    # sometimes returns an empty extraction for one trace. The product's
    # answer is to observe again (reverie_observe is cumulative by design), so
    # the test retries while nothing has been extracted. The ">= 1"
    # assertion itself is NOT relaxed.
    extracted_total = 0
    for attempt in range(3):
        observe = _call_tool(server, "reverie_observe", {
            "user_prompt": USER_PROMPT,
            "reasoning_trace": REASONING_TRACE,
            "final_output": FINAL_OUTPUT,
        })
        assert observe["status"] == "ok", observe
        print("[c] reverie_observe payload:", json.dumps(observe, indent=None))
        if attempt == 0:
            session_row = brain.get_active_session(RESOLVED_REPO, BRANCH)
            assert session_row is not None, \
                "the first tool call must create the session"
            session_id = session_row["id"]
            print("[b] session auto-created by the first tool call:",
                  session_id)
        extracted_total += observe["ecus_extracted"]
        if extracted_total >= 1:
            break
        print(f"[c] attempt {attempt + 1}: no ECUs extracted — "
              "observing again")

    # (c/d) ECUs extracted and stored in the session brain -------------------
    assert extracted_total >= 1, \
        "no ECUs extracted from a rich trace (3 attempts)"
    session_ecus = brain.list_session_ecus(session_id)
    assert len(session_ecus) == extracted_total
    assert observe["session_brain_count"] == len(session_ecus)
    extracted_ids = {e["id"] for e in session_ecus}
    for e in session_ecus:
        print("[d] session ECU: [%s|%s|%.2f] %s"
              % (e["conclusion_type"], e["scope"]["level"], e["confidence"],
                 e["cognition"][:100]))

    # (e) session-phase integration evidence (reported; firm check at (g)) ---
    session_edges = brain.list_session_edges(session_id)
    pending = brain.list_pending_updates(session_id=session_id)
    print("[e] session edges:", [(x["type"], x["target_type"]) for x in session_edges],
          "| pending updates:", [(p["relationship_type"]) for p in pending])

    # deliberately irrelevant session ECU for the gate check (f)
    offtopic_id = brain.insert_session_ecu(
        session_id, OFFTOPIC_ECU,
        embedding=model.encode_one(OFFTOPIC_ECU["cognition"]))
    off_sim = float(
        model.encode_one(QUERY) @ model.encode_one(OFFTOPIC_ECU["cognition"]))
    assert off_sim < 0.3, f"off-topic fixture too similar to the query: {off_sim}"

    # (f) reverie_query — relevant query via the MCP tool --------------------------
    result = _call_tool(server, "reverie_query", {"query": QUERY})
    print("[f] reverie_query: mode=%s groups=%d filtered_out=%d fallback=%s"
          % (result.get("mode"), result.get("groups_retrieved"),
             result.get("filtered_out"), result.get("fallback")))
    assert result["status"] == "ok", result
    assert result["groups_retrieved"] >= 1, "no groups retrieved for a relevant query"

    # retrieval groups contain extracted ECUs ---------------------------------
    surfaced = _group_ecu_ids(result)
    assert extracted_ids & surfaced, (
        f"no extracted ECU in retrieval output; extracted={extracted_ids}, "
        f"surfaced={surfaced}")
    print("[f1] extracted ECUs surfaced:", extracted_ids & surfaced)

    # relevance gate filtered the irrelevant ECU ------------------------------
    assert result["filtered_out"] >= 1, "gate filtered nothing (expected the off-topic ECU)"
    assert offtopic_id not in surfaced, "irrelevant ECU leaked past the relevance gate"
    assert result["fallback"] is False
    print("[f2] gate filtered %d ECU(s); off-topic ECU excluded"
          % result["filtered_out"])

    # activation scores normalized to [0, 1] ----------------------------------
    norm = manager.activation_for(session_id).normalized_scores()
    assert norm, "spreading activation produced no scores"
    assert all(0.0 <= s <= 1.0 for s in norm.values()), norm
    assert max(norm.values()) > 0.0
    print("[f3] normalized activation: %d entries, max %.3f, min %.3f"
          % (len(norm), max(norm.values()), min(norm.values())))

    # §11.8 formatted output structure ----------------------------------------
    formatted = result["formatted"]
    for marker in ("=== Engineering Cognition ===",
                   "=== End Engineering Cognition ===",
                   "CONCLUSION:", "CONFIDENCE:", "SCOPE:", "TYPE:", "SOURCE:"):
        assert marker in formatted, f"§11.8 marker missing: {marker}"
    assert ("This is past engineering understanding. "
            "Verify against current code before acting.") in formatted
    has_grounding = "GROUNDING:" in formatted or any(
        g["core_ecu"]["grounding"].get("files") for g in result["groups"])
    assert has_grounding, "no grounding surfaced in any retrieved group"
    print("[f4] §11.8 structure verified (CONCLUSION/CONFIDENCE/SCOPE/TYPE/"
          "SOURCE/GROUNDING + framing note)")

    # (g) review through the review queue (agent channel, D57) ----------------
    listing = _call_tool(server, "reverie_review_list", {"all": True})
    assert listing["status"] == "ok"
    candidates = [c["id"] for c in listing["candidates"]]
    assert set(candidates) == extracted_ids | {offtopic_id}
    token = listing["review_token"]

    # Hold ONE accepted-destiny ECU back for a second call — the two-call
    # split of the no-bulk-accept-all guard must fail on a consumed token.
    held = max(extracted_ids)
    decisions = [
        {"id": cid, "action": "reject" if cid == offtopic_id else "accept"}
        for cid in candidates if cid != held
    ]
    payload = _call_tool(server, "reverie_review_decide", {
        "review_token": token, "decisions": decisions})
    assert payload["status"] == "ok", payload
    # single-use: the token is consumed even though a valid id remains
    payload2 = _call_tool(server, "reverie_review_decide", {
        "review_token": token,
        "decisions": [{"id": held, "action": "accept"}]})
    assert payload2["status"] == "error"
    assert "Unknown or expired review_token" in payload2["error"]
    assert brain.get_session_ecu(held)["review_status"] == "pending", (
        "a refused second call must not decide anything")
    # the next batch needs a FRESH listing — and it holds exactly the
    # held-back candidate
    listing2 = _call_tool(server, "reverie_review_list", {"all": True})
    assert [c["id"] for c in listing2["candidates"]] == [held]
    payload3 = _call_tool(server, "reverie_review_decide", {
        "review_token": listing2["review_token"],
        "decisions": [{"id": held, "action": "accept"}]})
    assert payload3["status"] == "ok", payload3
    print("[g] %d candidates decided through the review queue in two "
          "batches (channel: agent; token single-use)" % len(candidates))

    # decisions recorded in the audit trail with the agent channel ----------
    audit = brain.list_review_decisions()
    audited = {r["candidate_id"]: r for r in audit}
    for cid in candidates:
        assert cid in audited, f"no audit row for {cid}"
        assert audited[cid]["channel"] == "agent"
    remaining = brain.list_session_ecus(session_id)
    assert all(e["review_status"] not in ("pending", "skipped")
               for e in remaining), "the queue must drain completely"

    # promotion to long-term memory: accepted extracted ECUs are canonical,
    # the rejected off-topic ECU is not -------------------------------
    canonical = {e["id"] for e in brain.list_ecus()}
    assert extracted_ids <= canonical, (
        f"accepted ECUs missing from the canonical brain: "
        f"{extracted_ids - canonical}")
    assert offtopic_id not in canonical, "a rejected ECU must not be promoted"
    promoted_ids = extracted_ids
    print("[g2] promoted to the Canonical Brain:", promoted_ids)

    # firm edge assertion: the full diffuser ran at promotion ---------------
    edge_types = []
    for ecu_id in canonical:
        edge_types.extend(e["type"] for e in brain.get_edges_for(ecu_id))
    print("[e-firm] canonical edges after promotion:", sorted(set(edge_types)))
    if STRICT:
        assert any(t in ("supports", "depends_on") for t in edge_types), (
            f"no supports/depends_on edge created by the full diffuser; "
            f"edge types present: {sorted(set(edge_types))}")
    else:
        # Ollama mode: the step really needs the full diffuser to RUN at
        # promotion and leave the brain consistent; whether the 14B
        # classifier proposes supports/depends_on (vs unrelated) is model
        # quality. STRICT (hosted) keeps the stronger form.
        assert brain.count_ecus() == len(promoted_ids) + 1
        print("[e-firm] (ollama) diffuser ran; edge types are model-"
              "quality-dependent:", sorted(set(edge_types)))

    # (h) retrieval in a LATER conversation on the same branch: the SAME
    # session resumes — nothing is closed (D56) -------------------------------
    manager2 = SessionManager(brain)  # fresh activation state
    server2 = ReverieServer(brain=brain, manager=manager2)
    result2 = _call_tool(server2, "reverie_query", {"query": QUERY})
    assert result2["status"] == "ok" and result2["groups_retrieved"] >= 1
    row = brain.get_active_session(RESOLVED_REPO, BRANCH)
    assert row is not None and row["id"] == session_id, (
        "the later conversation must resume the SAME session — sessions "
        "are never closed")
    assert row["status"] == "active"
    canonical_cores = {
        g["core_ecu"]["id"] for g in result2["groups"]
        if g["core_ecu"]["brain"] == "canonical"
    }
    assert canonical_cores & promoted_ids, (
        "promoted ECUs not retrievable in the later conversation")
    print("[h] same session resumed (%s); canonical ECUs surfaced: %s"
          % (row["id"], canonical_cores & promoted_ids))

    # (i) a branch switch picks the OTHER branch's session ---------------------
    monkeypatch.setenv("REVERIE_BRANCH", "feature")
    _call_tool(server2, "reverie_query", {"query": QUERY})
    feature_row = brain.get_active_session(RESOLVED_REPO, "feature")
    assert feature_row is not None, "the branch switch must auto-create ITS session"
    assert feature_row["id"] != session_id
    assert brain.get_session(session_id)["status"] == "active", (
        "switching branches must not close the main branch's session")
    print("[i] branch switch: session %s now serves 'feature'; %s stayed "
          "active" % (feature_row["id"], session_id))
    monkeypatch.setenv("REVERIE_BRANCH", BRANCH)

    # (j) cross-session persistence --------------------------------------------
    brain2 = Brain(REVERIE_HOME_DIR / "reverie.db")  # a brand-new connection
    try:
        for ecu_id in promoted_ids:
            ecu = brain2.get_ecu(ecu_id)
            assert ecu is not None, f"{ecu_id} lost across sessions"
            assert ecu["status"] in ("active", "challenged", "open_question")
            assert ecu["provenance"]["created_at"]
        assert brain2.count_ecus() == len(promoted_ids) + 1  # + seed
    finally:
        brain2.close()
    print("[j] %d promoted ECUs + seed persisted across sessions "
          "(verified via a fresh DB connection)" % len(promoted_ids))

    print("\nE2E PIPELINE: all steps a–j passed")
    brain.close()


# ============================================================================
# OFFLINE extended E2E — Phase 12 (design doc §11, "Integration and
# EC-Bench Run 3"): the v2 components wired together across boundaries.
#
# ~5 tests per §11: maintenance during a session, grounding deprecation,
# reconsolidation via the MCP tool, clustering table population, controlled
# forgetting in retrieval. All offline: no LLM (explicit modes / explicit
# relationships), real embeddings from the HF cache, tmp_path brains,
# hermetic DEFAULT_CONFIG (never ~/.reverie).
# ============================================================================

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from reverie import confidence as conf
from reverie import maintainer as maintainer_mod
from reverie.config import DEFAULT_CONFIG, AttrDict, _to_attrdict

OFFLINE_REPO, OFFLINE_BRANCH = "/repos/e2e-offline", "main"


def offline_config() -> AttrDict:
    return _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))


@pytest.fixture()
def offline_ecosystem(tmp_path, monkeypatch, model):
    """Brain + SessionManager + ReverieServer + an active session — the
    in-process equivalent of the live pipeline's CLI subprocess wiring,
    minus every LLM call."""
    monkeypatch.setenv("REVERIE_REPO_PATH", OFFLINE_REPO)
    monkeypatch.setenv("REVERIE_BRANCH", OFFLINE_BRANCH)
    cfg = offline_config()
    brain = Brain(tmp_path / "reverie.db")
    manager = SessionManager(brain, config=cfg, embedding_model=model)
    from reverie import repos
    repos.set_repo_state(brain, repos.resolve_repo(), "enabled")  # feat/flow
    server = ReverieServer(brain=brain, manager=manager, config=cfg,
                      embedding_model=model)
    session = manager.start_session()
    eco = SimpleNamespace(
        brain=brain, manager=manager, server=server, model=model, config=cfg,
        session_id=session["session_id"],
        repo=OFFLINE_REPO, branch=OFFLINE_BRANCH,
    )
    yield eco
    server.stop_maintenance()
    brain.close()


def seed_canonical(brain, model, cognition, **overrides):
    """Insert a canonical ECU (real embedding from ``model`` unless an
    explicit ``embedding`` override is given — synthetic-vector tests);
    non-active statuses are applied post-insert (insert always writes
    'active')."""
    status = overrides.pop("status", "active")
    embedding = overrides.pop("embedding", None)
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": 0.7,
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    if embedding is None and model is not None:
        embedding = model.encode_one(cognition)
    ecu_id = brain.insert_ecu(ecu, embedding=embedding)
    if status != "active":
        brain.update_ecu_status(ecu_id, status)
    return ecu_id


class TestE2EOffline:
    """Extended E2E over the assembled system — no mocks beyond the absent
    API key; components talk through their real interfaces."""

    def test_e2e_offline_maintenance_during_session(self, offline_ecosystem):
        # 1. A full maintenance run fires mid-session and the session
        #    survives it: four tasks in fixed order, per-task log rows,
        #    full_run summary + state baselines, tools still usable after.
        eco = offline_ecosystem
        brain = eco.brain
        auth_id = seed_canonical(
            eco.brain, eco.model,
            "Auth tokens must be refreshed before each request to avoid "
            "stale-credential 401 races")
        seed_canonical(eco.brain, eco.model,
                       "Route handlers validate their input schemas early")

        assert maintainer_mod.is_maintenance_overdue(brain, eco.config), \
            "a never-run brain is overdue"

        ran = eco.server.start_maintenance(repo_path=None)
        try:
            assert ran is not None, "startup check must run on a fresh brain"
            assert [t.action for t in ran.tasks] == list(
                maintainer_mod.TASK_ORDER)

            grounding = ran.tasks[1]
            assert grounding.details["skipped"] == "no_repo"
            clustering = ran.tasks[3]
            assert clustering.details["skipped"] == "too_few_ecus"
            assert clustering.details["min_cluster_size"] == 3

            logged = [r["action"]
                      for r in brain.list_maintenance_log(limit=10)]
            for action in (*maintainer_mod.TASK_ORDER, "full_run"):
                assert action in logged, f"{action} row missing"

            assert brain.get_maintenance_state(
                maintainer_mod.STATE_LAST_RUN_AT)
            assert int(brain.get_maintenance_state(
                maintainer_mod.STATE_LAST_RUN_ECU_COUNT)) == \
                brain.count_ecus() == 2
            assert not maintainer_mod.is_maintenance_overdue(
                brain, eco.config), "baseline just stamped"

            row = eco.manager.current_session(eco.repo, eco.branch)
            assert row is not None and row["id"] == eco.session_id, \
                "maintenance must not disturb the active session"
            assert brain.count_session_ecus(eco.session_id) == 0
        finally:
            eco.server.stop_maintenance()

        payload = _call_tool(eco.server, "reverie_query", {
            "query": "How does auth token refresh work?",
            "mode": "debugging"})
        assert payload["status"] == "ok", payload.get("error")
        assert payload["groups_retrieved"] >= 1
        assert auth_id in _group_ecu_ids(payload), \
            "canonical ECU retrievable after an inline maintenance run"

    def test_e2e_offline_grounding_deprecation_chain(self, offline_ecosystem,
                                                     tmp_path):
        # 2. Grounding verification against a live repo: a repo-scope ECU
        #    whose file vanished is deprecated, its dependent is challenged,
        #    engineering scope survives deletion, project scope needs ALL
        #    files gone — and the §3.6 review-gate notification surfaces the
        #    deprecation. A second run inside the throttle window skips.
        eco = offline_ecosystem
        brain = eco.brain

        repo = tmp_path / "demo-repo"
        (repo / "auth").mkdir(parents=True)
        (repo / "auth" / "token.py").write_text("def refresh(): ...\n")
        (repo / "auth" / "session.py").write_text(
            "class SessionManager: ...\n")
        grounding = lambda files: {  # noqa: E731 — fixture-local shorthand
            "files": files, "repo_path": str(repo)}

        stale_id = seed_canonical(                      # repo scope: ANY gone
            brain, eco.model,
            "Token refresh happens in auth/token.py refresh()",
            scope={"level": "repo", "path": "repo:demo > module:auth"},
            grounding=grounding(["auth/token.py"]))
        survivor_id = seed_canonical(                   # grounded, file exists
            brain, eco.model,
            "Session state is managed by SessionManager in auth/session.py",
            grounding=grounding(["auth/session.py"]))
        # dependent chain: survivor depends_on the soon-deprecated ECU
        brain.add_edge(survivor_id, stale_id, "depends_on")
        principle_id = seed_canonical(                  # never deprecated
            brain, eco.model,
            "Always validate inputs at the system boundary",
            scope={"level": "engineering", "path": "engineering"},
            grounding=grounding(["docs/deleted-guide.md"]))
        partial_id = seed_canonical(                    # needs ALL files gone
            brain, eco.model,
            "Auth configuration lives across session and token modules",
            scope={"level": "project", "path": "project:demo"},
            grounding=grounding(["auth/session.py", "docs/deleted-guide.md"]))

        (repo / "auth" / "token.py").unlink()           # the code change

        result = maintainer_mod.run_maintenance(brain, eco.config,
                                                repo_path=str(repo))
        by_action = {t.action: t for t in result.tasks}
        gtask = by_action["grounding"]
        assert "skipped" not in gtask.details
        assert gtask.details["checked"] == 4
        assert [d["ecu"] for d in gtask.details["deprecated"]] == [stale_id]
        assert gtask.details["deprecated"][0]["missing_files"] == \
            ["auth/token.py"]
        assert gtask.details["deprecated"][0]["challenged_dependents"] == \
            [survivor_id]

        assert brain.get_ecu(stale_id)["status"] == "deprecated"
        survivor = brain.get_ecu(survivor_id)
        assert survivor["status"] == "challenged"
        assert survivor["metadata"]["last_challenged"]
        assert brain.get_ecu(principle_id)["status"] == "active", \
            "engineering scope is never deprecated by code changes"
        assert brain.get_ecu(partial_id)["status"] == "active", \
            "project scope deprecates only when ALL grounding files are gone"

        # throttle stamped → an immediate second run skips this repo
        assert brain.get_maintenance_state(
            maintainer_mod.STATE_GROUNDING_CHECK_PREFIX + str(repo))
        again = maintainer_mod.run_maintenance(brain, eco.config,
                                               repo_path=str(repo))
        assert by_action["grounding"] .details["repo"] == str(repo)
        again_task = {t.action: t for t in again.tasks}["grounding"]
        assert again_task.details["skipped"] == "throttled"
        assert brain.get_ecu(stale_id)["status"] == "deprecated"

        # §3.6: the review gate surfaces what maintenance deprecated
        from reverie.review_gate import grounding_deprecation_notifications
        notifications = grounding_deprecation_notifications(brain)
        assert len(notifications) == 1
        note = notifications[0]
        assert note["kind"] == "grounding_deprecations"
        assert note["ecu_ids"] == [stale_id]
        assert "Token refresh happens in auth/token.py" in note["message"]
        assert "marked challenged" in note["message"]   # dependents mentioned

    def test_e2e_offline_clustering_table_population(self, tmp_path):
        # 4. The Maintainer's clustering task populates clusters +
        #    cluster_memberships from canonical ECU embeddings, excludes
        #    deprecated ECUs, stamps its baseline — and the 100-new-ECU
        #    trigger keeps subsequent runs from re-clustering until enough
        #    new work exists. Synthetic orthogonal-group embeddings keep the
        #    geometry deterministic (phase-9 pattern); agglomerative avoids
        #    the optional hdbscan dependency.
        brain = Brain(tmp_path / "reverie.db")
        try:
            cfg = offline_config()
            cfg["clustering"].update({
                "algorithm": "agglomerative",
                "stability_threshold": 0.0,
                "clustering_threshold": 1,
                "min_cluster_size": 3,
            })

            rng = np.random.default_rng(12)
            dim = 384
            group_ids = []
            for basis in range(3):                   # 3 far-apart groups × 4
                vecs = rng.normal(0.0, 0.02, (4, dim)).astype(np.float32)
                vecs[:, basis] += 1.0
                vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
                group_ids.append([
                    seed_canonical(brain, None,
                                   f"Group {basis} conclusion {i}",
                                   embedding=vecs[i])
                    for i in range(4)
                ])
            deprecated_id = seed_canonical(
                brain, None, "A stale conclusion whose code is gone",
                status="deprecated",
                embedding=rng.normal(0.0, 0.02, dim).astype(np.float32))

            result = maintainer_mod.run_maintenance(brain, cfg)
            ctask = {t.action: t for t in result.tasks}["clustering"]
            assert ctask.details["algorithm"] == "agglomerative"
            assert ctask.details["clusters_created"] == 3
            assert ctask.details["ecus_clustered"] == 12
            assert ctask.details["noise_count"] == 0

            clusters = brain.list_clusters()
            assert brain.count_clusters() == 3
            sizes = sorted(len(brain.memberships_for_cluster(c["id"]))
                           for c in clusters)
            assert sizes == [4, 4, 4]
            clustered_ids = {ecu_id
                             for c in clusters
                             for ecu_id in brain.memberships_for_cluster(
                                 c["id"])}
            all_active = {i for ids in group_ids for i in ids}
            assert clustered_ids == all_active
            assert deprecated_id not in clustered_ids, \
                "deprecated ECUs are never clustered"
            assert brain.clusters_for_ecu(deprecated_id) == []

            # baseline stamped; nothing new since → next run gates off
            assert brain.get_maintenance_state(
                maintainer_mod.STATE_CLUSTERING_ECU_COUNT) == \
                str(brain.count_ecus())
            again = maintainer_mod.run_maintenance(brain, cfg)
            again_ctask = {t.action: t for t in again.tasks}["clustering"]
            assert again_ctask.details["skipped"] == "below_threshold"
            assert again_ctask.details["new_ecus_since_last_run"] == 0
            assert brain.count_clusters() == 3, \
                "gated run must not touch existing clusters"
            assert len(brain.list_clusters()) == 3
        finally:
            brain.close()

    def test_e2e_offline_controlled_forgetting_retrieval(
            self, offline_ecosystem):
        # 5. Controlled forgetting, end to end (design doc Section 2):
        #    lazy decay decides RANKING at query time — a fresh ECU with
        #    LOWER stored confidence outranks an old one whose effective
        #    confidence decayed below it; retrieval then reinforces the
        #    stored values and resets the decay clock; the Maintainer's
        #    §17.5 persistence-limit pass parks a stale contradicts pair as
        #    open_question, where confidence is frozen yet still retrievable.
        eco = offline_ecosystem
        brain = eco.brain
        cfg = eco.config
        now = datetime.now(timezone.utc)
        module_scope = {"level": "module",
                        "path": "repo:demo > module:auth"}

        def days_ago(n: int) -> str:
            return (now - timedelta(days=n)).isoformat()

        # Same cognition text => identical embeddings => every ranking factor
        # equal except the confidence term. STORED says old(0.75) > fresh
        # (0.55); EFFECTIVE says old decays to ~0.52 (module λ=0.02/day × 50d)
        # and fresh stays 0.55 — the order must flip.
        old_id = seed_canonical(
            brain, eco.model,
            "The auth module refreshes tokens optimistically before each "
            "request", scope=module_scope, confidence=0.75,
            provenance={"source_type": "debugging",
                        "created_at": days_ago(50)},
            metadata={"last_reinforced": days_ago(50)})
        fresh_id = seed_canonical(
            brain, eco.model,
            "The auth module refreshes tokens optimistically before each "
            "request", scope=module_scope, confidence=0.55,
            provenance={"source_type": "debugging",
                        "created_at": now.isoformat()})

        # A stale §17.5 pair: challenged + contradicts, competing for 31
        # days against a module-scope limit of 30 → parked by maintenance.
        rate_scope = {"level": "module", "path": "repo:demo > module:api"}
        p1_id = seed_canonical(
            brain, eco.model,
            "The API rate limiter uses a fixed-window counter per user",
            scope=rate_scope, confidence=0.7, status="challenged",
            metadata={"competing_since": days_ago(31)})
        p2_id = seed_canonical(
            brain, eco.model,
            "The API rate limiter uses a sliding-window log per user",
            scope=rate_scope, confidence=0.7, status="challenged",
            metadata={"competing_since": days_ago(31)})
        brain.add_edge(p1_id, p2_id, "contradicts")

        payload = _call_tool(eco.server, "reverie_query", {
            "query": "How does auth token refresh work?",
            "mode": "debugging"})
        assert payload["status"] == "ok"
        core_ids = [g["core_ecu"]["id"] for g in payload["groups"]]
        assert set(core_ids) == {old_id, fresh_id}
        assert core_ids[0] == fresh_id and core_ids[1] == old_id, \
            "effective confidence must outrank stored confidence"

        # §2.1 reinforcement: both surfaced ECUs bumped on the STORED value,
        # decay clock reset to now (D17 write path).
        old_after = brain.get_ecu(old_id)
        fresh_after = brain.get_ecu(fresh_id)
        assert old_after["confidence"] == pytest.approx(
            conf.reinforce_stored_confidence(0.75, config=cfg))
        assert fresh_after["confidence"] == pytest.approx(
            conf.reinforce_stored_confidence(0.55, config=cfg))
        assert old_after["metadata"]["retrieval_count"] == 1
        assert old_after["metadata"]["last_reinforced"]
        # clock restarted: 35 MORE days of decay still beats what 50 days
        # had produced pre-retrieval (~0.52) — without the reset it would
        # sit near 0.35 after 85 total days.
        eff_later = conf.ecu_effective_confidence(
            old_after, now=now + timedelta(days=35), config=cfg)
        assert eff_later > 0.55

        # Eager half (D34): the Maintainer parks the expired pair…
        result = maintainer_mod.task_forgetting(brain, cfg, now=now)
        transitions = result.details["open_question_transitions"]
        assert len(transitions) == 1
        assert {transitions[0]["ecu"], *transitions[0]["pair"]} == \
            {p1_id, p2_id}
        assert brain.get_ecu(p1_id)["status"] == "open_question"
        assert brain.get_ecu(p2_id)["status"] == "open_question"
        assert not result.details["superseded"], \
            "decayed-but-replaced-less ECUs stay active"

        # …where their confidence is frozen (no decay on parked beliefs)…
        for pid in (p1_id, p2_id):
            parked = brain.get_ecu(pid)
            assert conf.ecu_effective_confidence(
                parked, now=now + timedelta(days=100), config=cfg) \
                == pytest.approx(parked["confidence"])

        # …and still retrievable, flagged as open questions (§17.5).
        oq_payload = _call_tool(eco.server, "reverie_query", {
            "query": "How does the API rate limiting work per user?",
            "mode": "debugging"})
        assert oq_payload["status"] == "ok"
        assert {p1_id, p2_id} <= _group_ecu_ids(oq_payload)
        assert "Open question" in oq_payload["formatted"]

    def test_e2e_offline_reconsolidation_via_mcp(self, offline_ecosystem):
        # 3. The full D37/D39 loop through the MCP tool surface:
        #    reverie_query surfaces a canonical ECU → it becomes labile AND gets
        #    the reinforcement bump → reverie_reconsolidate with contradicting
        #    evidence challenges it in the Canonical Brain immediately;
        #    a never-retrieved ECU is refused by the labile gate.
        eco = offline_ecosystem
        brain = eco.brain
        target_id = seed_canonical(
            brain, eco.model,
            "The auth module refreshes tokens optimistically before each "
            "request", confidence=0.75)
        unretrieved_id = seed_canonical(
            brain, eco.model,
            "PostgreSQL autovacuum scale factors must be tuned per table "
            "based on churn rate", confidence=0.6)

        payload = _call_tool(eco.server, "reverie_query", {
            "query": "How does the auth token refresh work?",
            "mode": "debugging"})
        assert payload["status"] == "ok"
        assert target_id in _group_ecu_ids(payload)
        assert unretrieved_id not in _group_ecu_ids(payload), \
            "irrelevant canonical ECU must not pass the relevance gate"

        # surfaced → labile (D39) + §2.1 reinforcement bump on STORED value
        sid = eco.session_id
        assert eco.manager.is_labile(sid, target_id)
        assert not eco.manager.is_labile(sid, unretrieved_id)
        expected_bump = conf.reinforce_stored_confidence(0.75,
                                                         config=eco.config)
        target = brain.get_ecu(target_id)
        assert target["confidence"] == pytest.approx(expected_bump)
        assert target["metadata"]["retrieval_count"] == 1
        assert target["metadata"]["last_reinforced"]

        # labile gate refuses what was never retrieved this session
        refusal = _call_tool(eco.server, "reverie_reconsolidate", {
            "ecu_id": unretrieved_id, "relationship": "contradicts",
            "evidence": "autovacuum tuning was verified differently"})
        assert refusal["status"] == "error"
        assert "not retrieved in this session" in refusal["error"]
        assert brain.get_ecu(unretrieved_id)["confidence"] == pytest.approx(
            0.6), "refused reconsolidation must write nothing"

        # contradicting evidence lands in the Canonical Brain immediately
        count_before = brain.count_ecus()
        result = _call_tool(eco.server, "reverie_reconsolidate", {
            "ecu_id": target_id, "relationship": "contradicts",
            "evidence": "auth/token.py now refreshes lazily only after a "
                        "401 — optimistic refresh no longer holds"})
        assert result["status"] == "ok", result.get("error")
        assert result["action_taken"] == "challenged"

        updated = brain.get_ecu(target_id)
        assert updated["status"] == "challenged"
        assert updated["confidence"] < expected_bump
        notes = updated["metadata"]["reconsolidations"]
        assert len(notes) == 1 and notes[0]["relationship"] == "contradicts"
        assert notes[0]["via"] == "reverie_reconsolidate"
        assert brain.count_ecus() == count_before, \
            "the pseudo-ECU must never be stored"
        assert brain.edge_count(target_id) == 0, \
            "contradiction path leaves no edge to an unstored pseudo-ECU"
