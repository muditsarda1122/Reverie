"""Phase 7 tests — Controlled Forgetting / Reinforcement.

Covers (design doc Section 2, §12.4 test table):

- lazy confidence decay math in ec/confidence.py (effective_confidence,
  ecu_effective_confidence, reinforce_stored_confidence)
- effective confidence wired into retrieval ranking
- retrieval reinforcement bump + decay-clock reset (D17 write path)
- eager status transitions in the Maintainer's forgetting task:
  §17.5 persistence limits and §15.5 supersession eligibility

Run: .venv/bin/python -m pytest tests/test_phase7_forgetting.py -v
All offline: real embeddings from the HF cache where a fixture needs them,
no LLM calls, tmp_path brains, injectable `now`.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from ec import maintainer, retrieval
from ec.brain import Brain
from ec.confidence import (
    FROZEN_STATUSES,
    ecu_effective_confidence,
    effective_confidence,
    reinforce_stored_confidence,
    to_log_odds,
    to_probability,
)
from ec.config import DEFAULT_CONFIG, _to_attrdict

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

def make_config(**overrides) -> dict:
    """Deep-copied defaults with dotted-key overrides (e.g. **{'a.b': 1})."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for dotted, value in overrides.items():
        section, key = dotted.split(".", 1)
        cfg[section][key] = value
    return _to_attrdict(cfg)


@pytest.fixture()
def config():
    return make_config()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def days_before(n):
    return iso(NOW - timedelta(days=n))


def insert_ecu(brain, model=None, cognition="Cache invalidation must follow token refresh",
               created_at=None, last_reinforced=None, **overrides):
    """Insert a canonical ECU; embedding included when a model is given."""
    provenance = {"source_type": "debugging"}
    if created_at is not None:
        provenance["created_at"] = created_at if isinstance(created_at, str) \
            else iso(created_at)
    metadata = {}
    if last_reinforced is not None:
        metadata["last_reinforced"] = (
            last_reinforced if isinstance(last_reinforced, str) else iso(last_reinforced)
        )
    metadata.update(overrides.pop("metadata", {}))
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": provenance,
        "grounding": {"files": ["auth/token.py"]},
        "confidence": 0.8,
        "metadata": metadata or {},
    }
    ecu.update(overrides)
    embedding = model.encode_one(cognition) if model is not None else None
    return brain.insert_ecu(ecu, embedding=embedding), ecu


# ---------------------------------------------------------------------------
# §2.1/§2.6 — effective_confidence (lazy decay math)  [table #1–#5]
# ---------------------------------------------------------------------------

class TestEffectiveConfidenceMath:
    def test_effective_confidence_decay(self, config):
        """Module scope (lambda=0.02/day), 35 days elapsed (#1).

        Exact log-odds math: sigmoid(logit(0.75) - 0.02*35) ~= 0.598.
        (The design doc's '~0.5' glosses this as 'half-life'; lambda is the
        per-day LOG-ODDS decay rate, so half-life refers to L, not c.)
        """
        c_eff = effective_confidence(
            0.75, "module", days_before(35), status="active", now=NOW,
            config=config,
        )
        assert c_eff == pytest.approx(to_probability(to_log_odds(0.75) - 0.02 * 35))
        assert c_eff < 0.75                       # decay only goes down
        assert c_eff == pytest.approx(0.598, abs=0.01)

    def test_effective_confidence_engineering_slow(self, config):
        """Engineering scope (lambda=0.001), 100 days -> minimal decay (#2)."""
        c_eff = effective_confidence(
            0.75, "engineering", days_before(100), status="active", now=NOW,
            config=config,
        )
        assert c_eff == pytest.approx(to_probability(to_log_odds(0.75) - 0.001 * 100))
        assert 0.72 < c_eff < 0.75

    def test_effective_confidence_subsystem_fast(self, config):
        """Subsystem scope (lambda=0.03), 23 days -> significant decay (#3)."""
        c_eff = effective_confidence(
            0.75, "subsystem", days_before(23), status="active", now=NOW,
            config=config,
        )
        assert c_eff == pytest.approx(to_probability(to_log_odds(0.75) - 0.03 * 23))
        assert 0.55 < c_eff < 0.65

    def test_zero_elapsed_returns_stored(self, config):
        assert effective_confidence(
            0.75, "module", NOW, status="active", now=NOW, config=config,
        ) == 0.75

    def test_future_timestamp_clamped(self, config):
        """Elapsed time never negative — future clocks don't boost."""
        assert effective_confidence(
            0.75, "module", iso(NOW + timedelta(days=5)), status="active",
            now=NOW, config=config,
        ) == 0.75

    def test_never_reinforced_no_clock_returns_stored(self, config):
        """last_reinforced=None -> no decay here; callers resolve the
        created_at fallback via ecu_effective_confidence."""
        assert effective_confidence(
            0.75, "module", None, status="active", now=NOW, config=config,
        ) == 0.75

    @pytest.mark.parametrize("status", sorted(FROZEN_STATUSES))
    def test_frozen_statuses_never_decay(self, config, status):
        """open_question/superseded/deprecated/archived: effective == stored
        regardless of elapsed time (#4, #5)."""
        for days in (0, 30, 3650):
            assert effective_confidence(
                0.75, "module", days_before(days), status=status, now=NOW,
                config=config,
            ) == 0.75, status

    def test_unknown_scope_raises(self, config):
        with pytest.raises(ValueError, match="unknown scope_level"):
            effective_confidence(
                0.75, "galactic", days_before(1), now=NOW, config=config,
            )


class TestEcuEffectiveConfidenceWrapper:
    def test_last_reinforced_takes_precedence(self, brain, config):
        ecu_id, _ = insert_ecu(
            brain, created_at=days_before(100),
            last_reinforced=days_before(10),
        )
        ecu = brain.get_ecu(ecu_id)
        expected = to_probability(to_log_odds(0.8) - 0.01 * 10)   # repo lambda
        assert ecu_effective_confidence(ecu, now=NOW, config=config) == \
            pytest.approx(expected)

    def test_created_at_fallback(self, brain, config):
        """§2.6: an ECU that has NEVER been reinforced decays from its
        creation time."""
        ecu_id, _ = insert_ecu(brain, created_at=days_before(30))
        ecu = brain.get_ecu(ecu_id)
        assert (ecu["metadata"] or {}).get("last_reinforced") is None   # Phase 2 gap confirmed
        expected = to_probability(to_log_odds(0.8) - 0.01 * 30)
        assert ecu_effective_confidence(ecu, now=NOW, config=config) == \
            pytest.approx(expected)

    def test_frozen_ecu_dict_ignores_clock(self, brain, config):
        ecu_id, _ = insert_ecu(
            brain, created_at=days_before(400), status="deprecated",
            last_reinforced=days_before(400),
        )
        ecu = brain.get_ecu(ecu_id)
        assert ecu_effective_confidence(ecu, now=NOW, config=config) == 0.8


class TestReinforcementMath:
    def test_bump_is_log_odds_additive(self, config):
        bumped = reinforce_stored_confidence(0.5, config=config)
        assert bumped == pytest.approx(to_probability(0.05))     # logit(0.5)=0
        assert reinforce_stored_confidence(0.9, config=config) > 0.9
        # diminishing at high confidence (sigmoid saturation)
        assert reinforce_stored_confidence(0.9, config=config) - 0.9 < 0.05

    def test_idempotent_formula(self, config):
        """Bumping twice equals one bump of 2*alpha in log-odds space."""
        once = reinforce_stored_confidence(0.4, config=config)
        twice = reinforce_stored_confidence(once, config=config)
        assert twice == pytest.approx(to_probability(to_log_odds(0.4) + 0.1))


# ---------------------------------------------------------------------------
# §2.1 — retrieval ranking uses effective confidence  [table #6]
# ---------------------------------------------------------------------------

class TestRetrievalUsesEffectiveConfidence:
    def test_decayed_ecu_ranks_lower_than_reinforced_twin(self, brain, model, config):
        """Two same-scope ECUs with identical stored confidence: the one
        reinforced recently must outrank the decayed one (#6)."""
        fresh_id, _ = insert_ecu(
            brain, model,
            cognition="Auth token refresh must rotate the refresh token",
            created_at=days_before(1), last_reinforced=days_before(1),
        )
        stale_id, _ = insert_ecu(
            brain, model,
            cognition="Auth token rotation invalidates the old access token",
            created_at=days_before(400), last_reinforced=days_before(400),
        )
        # sanity: identical stored confidence, identical scope
        assert brain.get_ecu(fresh_id)["confidence"] == \
            brain.get_ecu(stale_id)["confidence"] == 0.8

        result = retrieval.retrieve(
            brain, "auth token refresh and rotation", mode="investigation",
            config=config, embedding_model=model, now=NOW,
        )
        ranks = {
            g["core_ecu"]["id"]: g["rank"] for g in result["groups"]
        }
        assert ranks[fresh_id] > ranks[stale_id]

    def test_stored_confidence_unchanged_by_ranking(self, brain, model, config):
        """Lazy decay writes nothing: stored values are untouched by a query."""
        ecu_id, _ = insert_ecu(
            brain, model, created_at=days_before(400),
            last_reinforced=days_before(400),
        )
        before = brain.get_ecu(ecu_id)
        retrieval.retrieve(
            brain, "cache invalidation after token refresh", mode="investigation",
            config=config, embedding_model=model, now=NOW,
        )
        after = brain.get_ecu(ecu_id)
        assert after["confidence"] == before["confidence"]
        assert after["metadata"] == before["metadata"]     # decay clock untouched
        assert list_maintenance_rows(brain) == []          # no maintenance writes either

    def test_session_ecus_never_decay(self, brain, model, config):
        """Session-brain ECUs pass their stored confidence through (§2.6):
        rank_confidence == stored even after a long elapsed time."""
        session_id = brain.create_session("/tmp/repo7", "main")
        sess_ecu = {
            "cognition": "Session-scoped note about auth token refresh flows",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:demo > module:auth"},
            "provenance": {"source_type": "debugging", "created_at": days_before(400)},
            "grounding": {"files": ["auth/token.py"]},
            "confidence": 0.8,
        }
        brain.insert_session_ecu(
            session_id, sess_ecu, embedding=model.encode_one(sess_ecu["cognition"])
        )
        fetched = brain.get_session_ecu(
            brain.list_session_ecus(session_id)[0]["id"]
        )
        scored = retrieval._rank_candidates(
            brain,
            [{"ecu": {**fetched, "brain": "session"}, "similarity": 0.9,
              "brain": "session"}],
            activation=None, mode_params={"prioritize": [], "bias": "none"},
            session_id=session_id, config=config, now=NOW,
        )
        assert scored[0]["breakdown"]["rank_confidence"] == 0.8

    def test_canonical_breakdown_reports_both_confidences(self, brain, model, config):
        """Breakdown keeps stored AND effective (audit trail in the result)."""
        ecu_id, _ = insert_ecu(
            brain, model, created_at=days_before(100),
            last_reinforced=days_before(100),
        )
        ecu = brain.get_ecu(ecu_id)
        scored = retrieval._rank_candidates(
            brain,
            [{"ecu": ecu, "similarity": 0.9, "brain": "canonical"}],
            activation=None, mode_params={"prioritize": [], "bias": "none"},
            session_id=None, config=config, now=NOW,
        )
        b = scored[0]["breakdown"]
        assert b["confidence"] == 0.8
        assert b["rank_confidence"] == pytest.approx(
            to_probability(to_log_odds(0.8) - 0.01 * 100)
        )

    def test_frozen_status_ranks_on_stored(self, brain, model, config):
        """An open_question ECU's ranking factor is its frozen stored value."""
        ecu_id, _ = insert_ecu(
            brain, model, status="open_question",
            created_at=days_before(400), last_reinforced=days_before(400),
        )
        result = retrieval.retrieve(
            brain, "cache invalidation after token refresh", mode="investigation",
            config=config, embedding_model=model, now=NOW,
        )
        group = next(
            g for g in result["groups"] if g["core_ecu"]["id"] == ecu_id
        )
        # open_question weight applies; with no decay the factor-2 term is w_conf * 0.8
        assert group["core_ecu"]["confidence"] == 0.8


def list_maintenance_rows(brain):
    return brain.list_maintenance_log()


# ---------------------------------------------------------------------------
# §2.1/§2.3 — retrieval reinforcement bump (D17 write path)  [table #7, #8]
# ---------------------------------------------------------------------------

from ec.mcp_server import ECServer  # noqa: E402


def make_server(brain, config):
    return ECServer(brain=brain, config=config)


def core_result(ecu_ids_by_brain):
    """Minimal ec_query-shaped result for _record_retrieval_metadata."""
    return {
        "groups": [
            {"core_ecu": {"id": ecu_id, "brain": brain_name},
             "supporting_evidence": [], "contradictions": [],
             "dependencies": [], "superseded_by": []}
            for brain_name, ecu_id in ecu_ids_by_brain
        ],
    }


class TestRetrievalReinforcementBump:
    def test_bump_and_clock_reset(self, brain, model, config):
        """After the query path: stored confidence bumped by alpha_retrieval,
        last_reinforced reset to now (#7)."""
        ecu_id, _ = insert_ecu(
            brain, model, created_at=days_before(30),
            last_reinforced=days_before(30),
        )
        server = make_server(brain, config)
        n = server._record_retrieval_metadata(core_result([("canonical", ecu_id)]))
        assert n == 1

        after = brain.get_ecu(ecu_id)
        expected = to_probability(to_log_odds(0.8) + config.confidence.alpha_retrieval)
        assert after["confidence"] == pytest.approx(expected)
        assert after["metadata"]["last_reinforced"] is not None
        # the decay clock restarted: elapsed since reinforcement ~ 0
        assert ecu_effective_confidence(after, now=datetime.now(timezone.utc)) == \
            pytest.approx(after["confidence"])

    def test_full_pipeline_reinforces(self, brain, model, config):
        """End-to-end: retrieve() -> _record_retrieval_metadata bumps the
        surfaced canonical ECU exactly once per query."""
        ecu_id, _ = insert_ecu(
            brain, model, created_at=days_before(30),
            last_reinforced=days_before(30),
        )
        result = retrieval.retrieve(
            brain, "cache invalidation after token refresh", mode="investigation",
            config=config, embedding_model=model, now=NOW,
        )
        server = make_server(brain, config)
        server._record_retrieval_metadata(result)
        bumped = brain.get_ecu(ecu_id)
        assert bumped["confidence"] == pytest.approx(
            to_probability(to_log_odds(0.8) + config.confidence.alpha_retrieval)
        )
        assert bumped["metadata"]["retrieval_count"] == 1

    def test_reinforcement_resets_decay(self, brain, model, config):
        """An ECU unreinforced for 30 days is retrieved: its effective
        confidence jumps back to ~stored+alpha because the clock reset (#8)."""
        ecu_id, _ = insert_ecu(
            brain, model, created_at=days_before(30),
            last_reinforced=days_before(30),
        )
        decayed_before = ecu_effective_confidence(
            brain.get_ecu(ecu_id), now=NOW, config=config)

        result = retrieval.retrieve(
            brain, "cache invalidation after token refresh", mode="investigation",
            config=config, embedding_model=model, now=NOW,
        )
        make_server(brain, config)._record_retrieval_metadata(result)

        refreshed = brain.get_ecu(ecu_id)
        eff_after = ecu_effective_confidence(
            refreshed, now=datetime.now(timezone.utc), config=config)
        assert eff_after > decayed_before
        assert eff_after == pytest.approx(refreshed["confidence"], abs=1e-6)

    @pytest.mark.parametrize("status", sorted(FROZEN_STATUSES))
    def test_frozen_statuses_stamped_but_not_bumped(self, brain, model, config, status):
        """Bookkeeping (last_retrieved/count/last_reinforced) still lands on
        frozen-status ECUs, but the confidence bump does NOT — §17.5 freeze
        must survive being retrieved."""
        ecu_id, _ = insert_ecu(
            brain, model, status=status,
            created_at=days_before(10), last_reinforced=days_before(10),
        )
        server = make_server(brain, config)
        server._record_retrieval_metadata(core_result([("canonical", ecu_id)]))

        after = brain.get_ecu(ecu_id)
        assert after["confidence"] == 0.8                       # frozen value intact
        assert after["metadata"]["retrieval_count"] == 1
        assert after["metadata"]["last_retrieved"] is not None

    def test_session_ecus_not_stamped(self, brain, model, config):
        session_id = brain.create_session("/tmp/repo7b", "main")
        sess_ecu = {
            "cognition": "Session-only observation about token caching",
            "conclusion_type": "observation",
            "scope": {"level": "repo", "path": "repo:demo"},
            "provenance": {"source_type": "debugging"},
            "confidence": 0.5,
        }
        sid = brain.insert_session_ecu(
            session_id, sess_ecu, embedding=model.encode_one(sess_ecu["cognition"])
        )
        server = make_server(brain, config)
        assert server._record_retrieval_metadata(
            core_result([("session", sid)])) == 0
        untouched = brain.get_session_ecu(sid)
        assert untouched["confidence"] == 0.5
        assert (untouched.get("metadata") or {}).get("retrieval_count") in (None, 0)


# ---------------------------------------------------------------------------
# §17.5 / §15.5 — the Maintainer's eager forgetting task  [table #9–#12]
# ---------------------------------------------------------------------------

def make_competing_pair(brain, scope="module", since_days=40,
                        conf_a=0.45, conf_b=0.50):
    """Two challenged canonical ECUs joined by a contradicts edge, both with
    metadata.competing_since set (as review_gate._mark_competing does)."""
    id_a, _ = insert_ecu(
        brain,
        cognition="The race condition lives in TokenManager's refresh path",
        scope={"level": scope, "path": f"repo:demo > {scope}:auth"},
        confidence=conf_a, status="challenged", created_at=days_before(since_days),
        last_reinforced=days_before(since_days),
        metadata={"competing_since": days_before(since_days)},
    )
    id_b, _ = insert_ecu(
        brain,
        cognition="The race condition lives in CacheInvalidator's eviction path",
        scope={"level": scope, "path": f"repo:demo > {scope}:auth"},
        confidence=conf_b, status="challenged", created_at=days_before(since_days),
        last_reinforced=days_before(since_days),
        metadata={"competing_since": days_before(since_days)},
    )
    brain.add_edge(id_a, id_b, "contradicts", weight=0.8)
    return id_a, id_b


class TestPersistenceLimitTransitions:
    def test_persistence_limit_transition(self, brain, config):
        """challenged + competing_since past the module limit (30d) -> BOTH
        sides become open_question (#9)."""
        id_a, id_b = make_competing_pair(brain, scope="module", since_days=40)
        result = maintainer.task_forgetting(brain, config, now=NOW)

        assert brain.get_ecu(id_a)["status"] == "open_question"
        assert brain.get_ecu(id_b)["status"] == "open_question"
        assert len(result.details["open_question_transitions"]) == 1
        t = result.details["open_question_transitions"][0]
        assert t["ecu"] in (id_a, id_b)
        assert set(t["pair"]) == {id_a, id_b} - {t["ecu"]}
        assert result.ecus_affected == 2

    def test_transition_via_run_maintenance_logs_row(self, brain, config):
        """The full run logs the forgetting task with its transitions and
        ecus_affected (harness integration)."""
        id_a, id_b = make_competing_pair(brain, scope="module", since_days=60)
        maintainer.run_maintenance(brain, config, now=NOW)

        row = next(r for r in brain.list_maintenance_log()
                   if r["action"] == "forgetting")
        details = json.loads(row["details"])
        assert len(details["open_question_transitions"]) == 1
        assert row["ecus_affected"] == 2
        assert brain.get_ecu(id_a)["status"] == "open_question"

    def test_persistence_limit_engineering_indefinite(self, brain, config):
        """engineering scope: null limit = indefinite — never parked (#10)."""
        id_a, id_b = make_competing_pair(brain, scope="engineering",
                                         since_days=3650)
        result = maintainer.task_forgetting(brain, config, now=NOW)

        assert brain.get_ecu(id_a)["status"] == "challenged"
        assert brain.get_ecu(id_b)["status"] == "challenged"
        assert result.details["open_question_transitions"] == []
        assert result.ecus_affected == 0

    def test_domain_indefinite_too(self, brain, config):
        id_a, _ = make_competing_pair(brain, scope="domain", since_days=3650)
        maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(id_a)["status"] == "challenged"

    def test_below_limit_not_transitioned(self, brain, config):
        """competing_since within the limit -> untouched."""
        id_a, _ = make_competing_pair(brain, scope="module", since_days=10)
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(id_a)["status"] == "challenged"
        assert result.details["open_question_transitions"] == []

    def test_exact_limit_day_transitions(self, brain, config):
        """Elapsed == limit triggers (>= semantics, matching review_gate)."""
        id_a, _ = make_competing_pair(brain, scope="module", since_days=30)
        maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(id_a)["status"] == "open_question"

    def test_missing_competing_since_skipped(self, brain, config):
        """A challenged ECU without the §17.5 timestamp has no clock — left
        alone (it may be a plain contradiction, not a tracked competition)."""
        ecu_id, _ = insert_ecu(brain, status="challenged",
                               created_at=days_before(400))
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(ecu_id)["status"] == "challenged"
        assert result.ecus_affected == 0

    def test_terminal_pair_member_left_alone(self, brain, config):
        """An already-superseded pair member is not dragged into
        open_question — only active/challenged sides transition."""
        id_a, id_b = make_competing_pair(brain, scope="subsystem",
                                         since_days=30)
        brain.update_ecu_status(id_b, "superseded")
        maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(id_a)["status"] == "open_question"
        assert brain.get_ecu(id_b)["status"] == "superseded"

    def test_frozen_confidence_after_parking(self, brain, model, config):
        """Parked ECUs stop decaying: effective == stored afterwards."""
        id_a, _ = make_competing_pair(brain, scope="module", since_days=40)
        maintainer.task_forgetting(brain, config, now=NOW)
        parked = brain.get_ecu(id_a)
        assert ecu_effective_confidence(
            parked, now=datetime.now(timezone.utc), config=config
        ) == parked["confidence"]


class TestSupersessionFromDecay:
    def test_supersession_from_decay(self, brain, model, config):
        """Decayed below theta_supersede + replacement exists -> superseded:
        status change + supersedes edge from the replacement (#11)."""
        old_id, _ = insert_ecu(
            brain, model,
            cognition="Rate limiting uses a fixed window counter per API key",
            scope={"level": "module", "path": "repo:demo > module:api"},
            confidence=0.55,
            created_at=days_before(200), last_reinforced=days_before(200),
        )
        # effective = sigmoid(logit(0.55) - 0.02*200) ~= 0.096 < 0.3
        assert ecu_effective_confidence(
            brain.get_ecu(old_id), now=NOW, config=config) < 0.3

        new_id, _ = insert_ecu(
            brain, model,
            cognition="Rate limiting uses a sliding window counter per API key",
            scope={"level": "module", "path": "repo:demo > module:api"},
            confidence=0.75,
            created_at=days_before(5), last_reinforced=days_before(5),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)

        old = brain.get_ecu(old_id)
        assert old["status"] == "superseded"
        assert old["confidence"] == 0.55          # frozen at current value (§15.4)
        edges = [e for e in brain.get_edges_from(new_id) if e["type"] == "supersedes"]
        assert len(edges) == 1 and edges[0]["target_id"] == old_id
        s = result.details["superseded"][0]
        assert s["old"] == old_id and s["replacement"] == new_id
        assert s["similarity"] >= config.diffuser.relevance_threshold
        assert result.ecus_affected >= 1

    def test_no_supersession_without_replacement(self, brain, model, config):
        """Decayed below theta but no similar successor -> stays active at
        low confidence (#12). Decay alone never supersedes."""
        old_id, _ = insert_ecu(
            brain, model,
            cognition="Rate limiting uses a fixed window counter per API key",
            scope={"level": "module", "path": "repo:demo > module:api"},
            confidence=0.55,
            created_at=days_before(200), last_reinforced=days_before(200),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(old_id)["status"] == "active"
        assert result.details["superseded"] == []

    def test_reinforced_ecu_above_theta_untouched(self, brain, model, config):
        """Recent reinforcement keeps effective above theta — no supersession."""
        old_id, _ = insert_ecu(
            brain, model,
            cognition="Rate limiting uses a fixed window counter per API key",
            scope={"level": "module", "path": "repo:demo > module:api"},
            confidence=0.55,
            created_at=days_before(200), last_reinforced=days_before(2),
        )
        insert_ecu(
            brain, model,
            cognition="Rate limiting uses a sliding window counter per API key",
            scope={"level": "module", "path": "repo:demo > module:api"},
            confidence=0.75,
            created_at=days_before(1),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(old_id)["status"] != "superseded"
        assert result.details["superseded"] == []

    def test_old_or_weaker_candidate_not_a_replacement(self, brain, model, config):
        """Replacement must be NEWER and MORE CONFIDENT than the decayed ECU."""
        old_id, _ = insert_ecu(
            brain, model,
            cognition="Deployments roll out canary-first behind a feature flag",
            scope={"level": "module", "path": "repo:demo > module:deploy"},
            confidence=0.55,
            created_at=days_before(100), last_reinforced=days_before(100),
        )
        # effective = sigmoid(logit(0.55) - 0.02*100) ~= 0.14 < 0.3
        # older than old_ecu -> rejected despite similarity
        insert_ecu(
            brain, model,
            cognition="Deployments roll out canary-first behind feature flags",
            scope={"level": "module", "path": "repo:demo > module:deploy"},
            confidence=0.9,
            created_at=days_before(400), last_reinforced=days_before(400),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(old_id)["status"] == "active"

        # younger but WEAKER than the old EFFECTIVE value (~0.14) -> rejected
        insert_ecu(
            brain, model,
            cognition="Deploys use canary rollouts gated by flags",
            scope={"level": "module", "path": "repo:demo > module:deploy"},
            confidence=0.05,
            created_at=days_before(2),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(old_id)["status"] == "active"
        assert all(s["old"] != old_id for s in result.details["superseded"])

    def test_dependents_of_superseded_marked_challenged(self, brain, model, config):
        """§15.7 propagation: an ECU depending on the superseded belief is
        challenged (effective < theta_dep_reevaluate)."""
        old_id, _ = insert_ecu(
            brain, model,
            cognition="Auth sessions are stored in the shared Redis cluster",
            scope={"level": "module", "path": "repo:demo > module:auth"},
            confidence=0.55,
            created_at=days_before(200), last_reinforced=days_before(200),
        )
        dependent_id, _ = insert_ecu(
            brain, model,
            cognition="Session revocation propagates through Redis pub/sub channels",
            scope={"level": "module", "path": "repo:demo > module:auth"},
            confidence=0.8,
            created_at=days_before(100),
        )
        brain.add_edge(dependent_id, old_id, "depends_on", weight=0.9)
        insert_ecu(
            brain, model,
            cognition="Auth sessions live in Postgres with Redis as read cache",
            scope={"level": "module", "path": "repo:demo > module:auth"},
            confidence=0.85,
            created_at=days_before(3),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)
        assert brain.get_ecu(old_id)["status"] == "superseded"
        assert brain.get_ecu(dependent_id)["status"] == "challenged"
        assert dependent_id in result.details["challenged_dependents"]

    def test_supersession_pass_respects_parking(self, brain, model, config):
        """Order matters: an ECU just parked as open_question by the
        persistence pass is frozen — it is not then superseded in the same
        run even if its effective confidence is below theta."""
        old_id, _ = insert_ecu(
            brain, model,
            cognition="Retries wrap the payment charge call in an idempotency key",
            scope={"level": "module", "path": "repo:demo > module:pay"},
            confidence=0.25,                       # already below theta
            status="challenged",
            created_at=days_before(100), last_reinforced=days_before(100),
            metadata={"competing_since": days_before(100)},
        )
        other_id, _ = insert_ecu(
            brain, model,
            cognition="Retries skip the idempotency key for query endpoints",
            scope={"level": "module", "path": "repo:demo > module:pay"},
            confidence=0.25, status="challenged",
            created_at=days_before(100), last_reinforced=days_before(100),
            metadata={"competing_since": days_before(100)},
        )
        brain.add_edge(old_id, other_id, "contradicts", weight=0.8)
        insert_ecu(
            brain, model,
            cognition="Payment charge retries always carry an idempotency key header",
            scope={"level": "module", "path": "repo:demo > module:pay"},
            confidence=0.9,
            created_at=days_before(2),
        )
        result = maintainer.task_forgetting(brain, config, now=NOW)

        parked = brain.get_ecu(old_id)
        assert parked["status"] == "open_question"      # persistence pass won
        assert all(s["old"] != old_id for s in result.details["superseded"])
        assert ecu_effective_confidence(
            parked, now=datetime.now(timezone.utc), config=config) == 0.25
