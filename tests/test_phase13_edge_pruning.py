"""Phase 13 tests — Edge Pruning (design doc §2, D52; SPEC §14.5/§15.6).

task_edge_pruning removes edges whose target ECU is deprecated/superseded
(or orphaned) and reverses their stored confidence deltas via
confidence.reverse_update — §15.6's first and only caller. supersedes
edges are never pruned: they are the audit trail.

Run: .venv/bin/python -m pytest tests/test_phase13_edge_pruning.py -v
All offline: no LLM calls, tmp_path brains, real confidence math.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json

import pytest

from ec import maintainer
from ec.brain import Brain
from ec.confidence import contradiction_update, support_update
from ec.config import DEFAULT_CONFIG, _to_attrdict
from ec.maintainer import run_maintenance, task_edge_pruning

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def config():
    return _to_attrdict(
        {**DEFAULT_CONFIG, "maintainer": {"enabled": False}})


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def _ecu(brain, cognition, **overrides):
    body = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": 0.6,
    }
    body.update(overrides)
    return brain.insert_ecu(body)


def _pair(brain):
    """(source, target): an active source holding evidence about a live
    target. Returns (a_id, b_id)."""
    a = _ecu(brain, "Token refresh must invalidate the cached session scope")
    b = _ecu(brain, "Session cache is keyed by user id, not by token")
    return a, b


def _apply_support(brain, a_id, b_id, c_a=0.75, relevance=0.9):
    """Create the edge the way the diffuser does: update the target's
    confidence AND store the delta on the edge (§8.3 steps 4+5)."""
    target = brain.get_ecu(b_id)
    new_c, delta = support_update(target["confidence"], c_a, relevance,
                                  config=None)
    brain.update_ecu_confidence(b_id, new_c)
    brain.add_edge(a_id, b_id, "supports", weight=relevance,
                   confidence_delta=delta)
    return delta


def _apply_contradiction(brain, a_id, b_id, c_a=0.75, relevance=0.9):
    target = brain.get_ecu(b_id)
    new_c, delta = contradiction_update(target["confidence"], c_a,
                                        relevance, config=None)
    brain.update_ecu_confidence(b_id, new_c)
    brain.add_edge(a_id, b_id, "contradicts", weight=relevance,
                   confidence_delta=delta)
    return delta


def _orphan_edge(brain, source_id):
    """Insert an edge row whose target does not exist (add_edge refuses
    dangling edges by design — pruning must still clean up historical
    orphans, e.g. from pre-FK databases)."""
    brain._conn.execute("PRAGMA foreign_keys = OFF")
    brain._conn.execute(
        "INSERT INTO edges (id, source_id, target_id, type, weight, "
        "confidence_delta, created_at) VALUES (?, ?, ?, 'supports', 0.5, "
        "0.1, '2026-01-01T00:00:00+00:00')",
        ("orphan-edge-1", source_id, "deleted-ecu-id"),
    )
    brain._conn.commit()
    brain._conn.execute("PRAGMA foreign_keys = ON")


# ---------------------------------------------------------------------------
# pruning triggers + reversal rules (§14.5)
# ---------------------------------------------------------------------------

class TestPruningTriggers:
    def test_deprecated_target_edge_pruned(self, brain, config):
        a, b = _pair(brain)
        _apply_support(brain, a, b)
        brain.update_ecu_status(b, "deprecated")

        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 1
        assert brain.get_edges_for(b) == []
        assert brain.get_edges_for(a) == []      # the same single edge

    def test_superseded_target_edge_pruned(self, brain, config):
        a, b = _pair(brain)
        _apply_contradiction(brain, a, b)
        brain.update_ecu_status(b, "superseded")

        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 1
        assert brain.get_edges_to(b) == []

    def test_active_target_edges_untouched(self, brain, config):
        a, b = _pair(brain)
        _apply_support(brain, a, b)
        before = brain.get_ecu(b)["confidence"]

        task_edge_pruning(brain, config)

        assert len(brain.get_edges_for(b)) == 1
        assert brain.get_ecu(b)["confidence"] == pytest.approx(before)

    def test_orphaned_edge_removed(self, brain, config):
        a, _b = _pair(brain)
        _orphan_edge(brain, a)

        result = task_edge_pruning(brain, config)

        assert result.details["orphans_removed"] == 1
        assert result.details["edges_pruned"] == 1
        assert all(e["id"] != "orphan-edge-1" for e in brain.list_all_edges())

    def test_supersedes_never_pruned_even_with_dead_target(self, brain,
                                                           config):
        """§14.5: the audit trail survives its endpoints' deaths."""
        a, b = _pair(brain)
        brain.update_ecu_status(b, "superseded")
        brain.add_edge(a, b, "supersedes", weight=1.0,
                       supersession_type="semantic")

        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 0
        remaining = brain.get_edges_between(a, b, "supersedes")
        assert len(remaining) == 1
        assert remaining[0]["supersession_type"] == "semantic"

    def test_depends_on_pruned_without_delta_reversal(self, brain, config):
        a, b = _pair(brain)
        brain.add_edge(a, b, "depends_on", weight=0.7, confidence_delta=0.0)
        conf_before = brain.get_ecu(b)["confidence"]
        brain.update_ecu_status(b, "deprecated")

        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 1
        assert result.details["deltas_reversed"] == 0
        assert brain.get_edges_for(b) == []
        # depends_on has no confidence effect to reverse (§14.5 rule 3)
        assert brain.get_ecu(b)["confidence"] == pytest.approx(conf_before)


class TestDeltaReversal:
    def test_support_delta_reversed_confidence_drops_back(self, brain, config):
        """§15.2 then §15.6: the target was bumped up when the supports edge
        was created; pruning subtracts exactly that delta."""
        a, b = _pair(brain)
        original = brain.get_ecu(b)["confidence"]
        delta = _apply_support(brain, a, b)
        assert brain.get_ecu(b)["confidence"] > original   # update applied

        brain.update_ecu_status(b, "deprecated")
        result = task_edge_pruning(brain, config)

        assert result.details["deltas_reversed"] == 1
        after = brain.get_ecu(b)["confidence"]
        assert after == pytest.approx(original, abs=1e-9)
        reversal = result.details["reversals"][0]
        assert reversal["edge_type"] == "supports"
        assert reversal["delta"] == pytest.approx(delta, abs=1e-9)
        assert reversal["old_confidence"] > reversal["new_confidence"]

    def test_contradict_delta_reversed_confidence_rises_back(self, brain,
                                                             config):
        """The contradicts edge's stored delta is negative; reversing it
        adds the magnitude back (§14.5 rule 2 / §15.6)."""
        a, b = _pair(brain)
        original = brain.get_ecu(b)["confidence"]
        _apply_contradiction(brain, a, b)
        lowered = brain.get_ecu(b)["confidence"]
        assert lowered < original                          # challenge applied

        brain.update_ecu_status(b, "deprecated")           # frozen status:
        # effective == stored for deprecated ECUs, so the reversal math runs
        # against the stored value exactly as §15.6 specifies.
        result = task_edge_pruning(brain, config)

        assert result.details["deltas_reversed"] == 1
        assert brain.get_ecu(b)["confidence"] == pytest.approx(original,
                                                              abs=1e-9)

    def test_zero_delta_edge_pruned_without_reversal(self, brain, config):
        """A delta-less supports edge (weight-only accumulation) is pruned
        without touching confidence."""
        a, b = _pair(brain)
        brain.add_edge(a, b, "supports", weight=0.5, confidence_delta=0.0)
        conf_before = brain.get_ecu(b)["confidence"]
        brain.update_ecu_status(b, "deprecated")

        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 1
        assert result.details["deltas_reversed"] == 0
        assert brain.get_ecu(b)["confidence"] == pytest.approx(conf_before)

    def test_multiple_edges_reverse_independently(self, brain, config):
        """Two edges onto one target each contribute their own delta; the
        sequential reversals compose back to the original value."""
        a, b = _pair(brain)
        original = brain.get_ecu(b)["confidence"]
        d1 = _apply_support(brain, a, b, c_a=0.7)
        d2 = _apply_contradiction(brain, a, b, c_a=0.5)
        assert brain.get_ecu(b)["confidence"] != original

        brain.update_ecu_status(b, "deprecated")
        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 2
        assert result.details["deltas_reversed"] == 2
        assert brain.get_ecu(b)["confidence"] == pytest.approx(original,
                                                              abs=1e-9)
        reversed_deltas = sorted(r["delta"]
                                 for r in result.details["reversals"])
        assert reversed_deltas == pytest.approx(sorted([d1, d2]))


# ---------------------------------------------------------------------------
# integration: the maintenance run + maintenance_log
# ---------------------------------------------------------------------------

class TestMaintenanceIntegration:
    def test_run_maintenance_logs_real_pruning_details(self, brain, config):
        """The old stub logged {'deferred': ...}; the live task logs its
        counts through the normal run_maintenance pipeline."""
        a, b = _pair(brain)
        _apply_support(brain, a, b)
        brain.update_ecu_status(b, "deprecated")

        run = run_maintenance(brain, config)

        task = next(t for t in run.tasks if t.action == "edge_pruning")
        assert task.details["edges_pruned"] == 1
        assert task.ecus_affected == 1
        row = next(r for r in brain.list_maintenance_log()
                   if r["action"] == "edge_pruning")
        assert json.loads(row["details"])["edges_pruned"] == 1
        # TASK_ORDER position preserved: pruning ran after grounding
        actions = [t.action for t in run.tasks]
        assert actions.index("grounding") < actions.index("edge_pruning") \
            < actions.index("clustering")

    def test_healthy_brain_is_a_noop(self, brain, config):
        a, b = _pair(brain)
        _apply_support(brain, a, b)

        result = task_edge_pruning(brain, config)

        assert result.details == {
            "edges_pruned": 0,
            "deltas_reversed": 0,
            "orphans_removed": 0,
            "reversals": [],
        }
        assert len(brain.get_edges_for(b)) == 1

    def test_grounding_then_pruning_same_pass(self, brain, config, tmp_path):
        """TASK_ORDER rationale, end-to-end: grounding deprecates an ECU
        whose file vanished; pruning removes the stale edge in the SAME
        run (fresh brain → no 72h throttle entry for this repo)."""
        repo = tmp_path / "repo"
        (repo / "auth").mkdir(parents=True)
        (repo / "auth" / "token.py").write_text("TOKEN = 1\n")
        a, b = _pair(brain)
        _apply_support(brain, a, b)
        # re-point b's grounding at the repo that will lose its file
        brain._conn.execute(
            "UPDATE ecus SET grounding_json = ? WHERE id = ?",
            (json.dumps({"repo_path": str(repo),
                         "files": ["auth/token.py"]}), b),
        )
        brain._conn.commit()

        import os
        os.remove(repo / "auth" / "token.py")   # repo dir stays; file gone
        maintainer.task_grounding(brain, config, repo_path=str(repo))
        assert brain.get_ecu(b)["status"] == "deprecated"

        result = task_edge_pruning(brain, config)

        assert result.details["edges_pruned"] == 1
        assert result.details["deltas_reversed"] == 1
