"""Phase 9 tests — HDBSCAN Clustering (design doc Section 4).

Covers the §12.6 table (#1–#8): cluster creation on grouped embeddings,
min_cluster_size skipping, noise exclusion, stability threshold, re-clustering
clearing old memberships, status eligibility, the clustering_threshold
trigger, and review-gate cluster grouping (D36) — plus supporting coverage:
schema tables, brain cluster CRUD, agglomerative fallback, embedding-less
ECUs, maintenance_log/state wiring via run_maintenance.

Run: .venv/bin/python -m pytest tests/test_phase9_clustering.py -v
All offline: handcrafted synthetic embeddings (no model download), no LLM,
injectable `now`.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import copy
from datetime import datetime, timezone

import numpy as np
import pytest

from ec import clustering as clustering_mod
from ec import maintainer, review_gate
from ec.brain import Brain, BrainError
from ec.config import DEFAULT_CONFIG, AttrDict, _to_attrdict

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

def make_config(**overrides) -> AttrDict:
    """Deep-copied defaults; overrides apply to the clustering block unless
    prefixed 'review_gate.' (D36 tests tweak that block instead)."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in overrides.items():
        if key.startswith("review_gate."):
            cfg["review_gate"][key.split(".", 1)[1]] = value
        else:
            cfg["clustering"][key] = value
    return _to_attrdict(cfg)


@pytest.fixture()
def config():
    return make_config()


@pytest.fixture()
def lenient_config():
    """Default clustering config but no stability floor: HDBSCAN persistence
    is data-draw dependent (0.24–0.68 observed for the same geometry), so
    cluster-creation tests pin stability_threshold to 0 to stay
    deterministic. The threshold's own behavior is tested separately."""
    return make_config(stability_threshold=0.0)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def utcnow():
    return datetime.now(timezone.utc)


EMB_DIM = 384


def group_vectors(rng, basis_idx, n=4, dim=EMB_DIM, noise=0.02):
    """n unit vectors tightly clustered around a one-hot basis direction.

    Orthogonal bases keep groups far apart AFTER L2 normalization (raw
    gaussian offsets collapse onto each other once normalized — cosine
    geometry compresses scalar separation). noise=0.02 keeps intra-group
    cosine ≈ 0.86 — above the diffuser relevance floor (0.6) so review-gate
    cluster matching works, while cross-group stays ≈ 0."""
    vecs = rng.normal(0.0, noise, (n, dim)).astype(np.float32)
    vecs[:, basis_idx] += 1.0
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def insert_ecu(brain, cognition="Some engineering conclusion", status="active",
               confidence=0.7, embedding=None, **overrides):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "decision",
        "scope": {"level": "repo", "path": "repo:demo > repo:x"},
        "provenance": {"source_type": "debugging"},
        "confidence": confidence,
        "status": status,
    }
    ecu.update(overrides)
    ecu_id = brain.insert_ecu(ecu, embedding=embedding)
    if status != "active":  # insert always writes 'active'
        brain.update_ecu_status(ecu_id, status)
    return ecu_id


def seed_three_groups(brain, rng, per_group=4):
    """Three far-apart groups (basis coords 0/1/2) + their ids."""
    ids = []
    for basis_idx in (0, 1, 2):
        gvecs = group_vectors(rng, basis_idx, n=per_group)
        ids.append([insert_ecu(brain, embedding=v) for v in gvecs])
    return ids


# ---------------------------------------------------------------------------
# schema — additive tables exist on new AND pre-existing databases
# ---------------------------------------------------------------------------

class TestSchema:
    def test_tables_exist(self, brain):
        names = brain.table_names()
        assert "clusters" in names
        assert "cluster_memberships" in names

    def test_migration_on_legacy_db(self, tmp_path):
        """A DB created before Phase 9 (no clustering DDL) gains the tables
        additively on the next connect."""
        db = tmp_path / "legacy.db"
        legacy = Brain(db)
        legacy._conn.execute("DROP TABLE clusters")
        legacy._conn.execute("DROP TABLE cluster_memberships")
        legacy._conn.commit()
        legacy.close()

        reopened = Brain(db)
        try:
            assert "clusters" in reopened.table_names()
            assert "cluster_memberships" in reopened.table_names()
        finally:
            reopened.close()


# ---------------------------------------------------------------------------
# brain cluster CRUD (design doc §4.4 pseudocode's brain calls)
# ---------------------------------------------------------------------------

class TestBrainClusterCRUD:
    def test_get_or_create_cluster_creates(self, brain):
        cid = brain.get_or_create_cluster(0, 0.75)
        rows = brain.list_clusters()
        assert len(rows) == 1
        assert rows[0]["id"] == cid
        assert rows[0]["label"] == 0
        assert rows[0]["stability"] == pytest.approx(0.75)

    def test_get_or_create_cluster_reuses_label(self, brain):
        cid = brain.get_or_create_cluster(3, 0.7)
        again = brain.get_or_create_cluster(3, 0.9)
        assert again == cid
        rows = brain.list_clusters()
        assert len(rows) == 1
        assert rows[0]["stability"] == pytest.approx(0.9)

    def test_add_membership(self, brain):
        ecu_id = insert_ecu(brain)
        cid = brain.get_or_create_cluster(1, 0.8)
        brain.add_cluster_membership(ecu_id, cid, weight=0.8)
        assert brain.memberships_for_cluster(cid) == [ecu_id]
        got = brain.clusters_for_ecu(ecu_id)
        assert len(got) == 1
        assert got[0]["label"] == 1
        assert got[0]["weight"] == pytest.approx(0.8)

    def test_add_membership_is_idempotent(self, brain):
        ecu_id = insert_ecu(brain)
        cid = brain.get_or_create_cluster(1, 0.8)
        brain.add_cluster_membership(ecu_id, cid, weight=0.8)
        brain.add_cluster_membership(ecu_id, cid, weight=0.8)
        assert len(brain.memberships_for_cluster(cid)) == 1

    def test_add_membership_unknown_ecu_raises(self, brain):
        cid = brain.get_or_create_cluster(1, 0.8)
        with pytest.raises(BrainError):
            brain.add_cluster_membership("no-such-ecu", cid, weight=0.5)

    def test_add_membership_unknown_cluster_raises(self, brain):
        ecu_id = insert_ecu(brain)
        with pytest.raises(BrainError):
            brain.add_cluster_membership(ecu_id, "no-such-cluster", weight=0.5)

    def test_clear_cluster_memberships_wipes_both_tables(self, brain):
        ids = [insert_ecu(brain) for _ in range(3)]
        cids = [brain.get_or_create_cluster(i, 0.9) for i in range(3)]
        for ecu_id, cid in zip(ids, cids):
            brain.add_cluster_membership(ecu_id, cid, weight=0.9)
        removed = brain.clear_cluster_memberships()
        assert removed == 3
        assert brain.list_clusters() == []
        assert brain.count_clusters() == 0
        assert brain.clusters_for_ecu(ids[0]) == []

    def test_list_clusters_ordered_by_label(self, brain):
        for label in (2, 0, 1):
            brain.get_or_create_cluster(label, 0.9)
        assert [c["label"] for c in brain.list_clusters()] == [0, 1, 2]

    def test_list_ecus_multi_status(self, brain):
        active = insert_ecu(brain, status="active")
        challenged = insert_ecu(brain, status="challenged")
        parked = insert_ecu(brain, status="open_question")
        insert_ecu(brain, status="superseded")
        got = brain.list_ecus(statuses=("active", "challenged",
                                        "open_question"))
        assert {e["id"] for e in got} == {active, challenged, parked}

    def test_list_ecus_single_status_still_works(self, brain):
        active = insert_ecu(brain, status="active")
        insert_ecu(brain, status="superseded")
        got = brain.list_ecus(status="active")
        assert [e["id"] for e in got] == [active]


# ---------------------------------------------------------------------------
# run_clustering — §12.6 #1–#6
# ---------------------------------------------------------------------------

class TestRunClustering:
    def _seed_and_run(self, brain, config, rng, per_group=4):
        ids = seed_three_groups(brain, rng, per_group=per_group)
        result = clustering_mod.run_clustering(brain, config)
        return ids, result

    def test_clustering_creates_clusters(self, brain, lenient_config):
        """§12.6 #1: three well-separated groups → 3 clusters with the
        correct memberships."""
        rng = np.random.default_rng(7)
        ids, result = self._seed_and_run(brain, lenient_config, rng)
        assert result.details["clusters_created"] == 3
        assert result.details["ecus_clustered"] == 12
        assert result.details["noise_count"] == 0
        clusters = brain.list_clusters()
        assert sorted(c["label"] for c in clusters) == [0, 1, 2]
        # every ECU is a member of exactly one cluster; groups stay intact
        seen = []
        for cluster in clusters:
            members = set(brain.memberships_for_cluster(cluster["id"]))
            assert len(members) == 4
            seen.extend(members)
            for ecu_id in members:
                assert brain.clusters_for_ecu(ecu_id)[0]["id"] == cluster["id"]
        assert seen and set(seen) == {i for group in ids for i in group}

    def test_min_cluster_size_skips(self, brain, config):
        """§12.6 #2: below min_cluster_size there is nothing to find."""
        rng = np.random.default_rng(7)
        for v in group_vectors(rng, 0, n=2):
            insert_ecu(brain, embedding=v)
        result = clustering_mod.run_clustering(brain, config)
        assert result.details["skipped"] == "too_few_ecus"
        assert result.ecus_affected == 0
        assert brain.list_clusters() == []

    def test_noise_ecus_excluded(self, brain, lenient_config):
        """§12.6 #3: an outlier joins no cluster."""
        rng = np.random.default_rng(7)
        ids, _ = self._seed_and_run(brain, lenient_config, rng)
        outlier_id = insert_ecu(
            brain, embedding=group_vectors(rng, 380, n=1)[0])
        result = clustering_mod.run_clustering(brain, lenient_config)
        all_members = {
            m
            for c in brain.list_clusters()
            for m in brain.memberships_for_cluster(c["id"])
        }
        assert outlier_id not in all_members
        assert result.details["noise_count"] >= 1

    def test_stability_threshold_drops_weak_clusters(self, brain):
        """§12.6 #4: clusters whose stability < stability_threshold get no
        storage (members count as noise)."""
        cfg = make_config(stability_threshold=0.999)   # nothing survives
        rng = np.random.default_rng(7)
        self._seed_and_run(brain, cfg, rng)
        assert brain.list_clusters() == []
        assert all(brain.clusters_for_ecu(e["id"]) == []
                   for e in brain.list_ecus())

    def test_reclustering_clears_old_memberships(self, brain, lenient_config):
        """§12.6 #5: a second run replaces the previous generation."""
        rng = np.random.default_rng(7)
        first_ids, first = self._seed_and_run(brain, lenient_config, rng)
        old_clusters = brain.list_clusters()
        assert old_clusters

        # grow one group so labels/memberships must change
        for v in group_vectors(rng, 1, n=2):
            insert_ecu(brain, embedding=v)
        second = clustering_mod.run_clustering(brain, lenient_config)

        new_ids = {c["id"] for c in brain.list_clusters()}
        assert new_ids.isdisjoint({c["id"] for c in old_clusters})
        total_members = sum(
            len(brain.memberships_for_cluster(c["id"]))
            for c in brain.list_clusters()
        )
        assert total_members == second.details["ecus_clustered"]
        assert second.details["total_ecus"] == 14

    def test_only_eligible_statuses_clustered(self, brain, lenient_config):
        """§12.6 #6: superseded/deprecated/archived ECUs are excluded from
        both the fit and the stored memberships."""
        rng = np.random.default_rng(7)
        dead_ids = [
            insert_ecu(brain, embedding=group_vectors(rng, 10, n=1)[0],
                       status="superseded"),
            insert_ecu(brain, embedding=group_vectors(rng, 10, n=1)[0],
                       status="deprecated"),
            insert_ecu(brain, embedding=group_vectors(rng, 10, n=1)[0],
                       status="archived"),
        ]
        live_vecs = group_vectors(rng, 0, n=4)
        live_ids = [
            insert_ecu(brain, embedding=live_vecs[0]),
            insert_ecu(brain, embedding=live_vecs[1], status="challenged"),
            insert_ecu(brain, embedding=live_vecs[2], status="open_question"),
            insert_ecu(brain, embedding=live_vecs[3]),
        ]
        result = clustering_mod.run_clustering(brain, lenient_config)
        assert result.details["total_ecus"] == 4
        members = {
            m
            for c in brain.list_clusters()
            for m in brain.memberships_for_cluster(c["id"])
        }
        assert members.isdisjoint(dead_ids)
        assert members <= set(live_ids)

    def test_ecus_without_embeddings_are_ignored(self, brain, lenient_config):
        """A vectorless ECU cannot participate — it stays unclustered while
        embedded siblings still cluster."""
        rng = np.random.default_rng(7)
        blank = insert_ecu(brain)                      # no embedding
        ids, result = self._seed_and_run(brain, lenient_config, rng)
        assert result.details["clusters_created"] == 3
        assert blank not in {
            m
            for c in brain.list_clusters()
            for m in brain.memberships_for_cluster(c["id"])
        }

    def test_agglomerative_fallback_algorithm(self, brain):
        """Design doc §4.7: algorithm='agglomerative' produces the same shape
        of result via scipy (no hdbscan call)."""
        cfg = make_config(algorithm="agglomerative",
                          stability_threshold=0.0)
        rng = np.random.default_rng(7)
        ids, result = self._seed_and_run(brain, cfg, rng)
        assert result.details["algorithm"] == "agglomerative"
        assert result.details["clusters_created"] == 3

    def test_hdbscan_import_error_falls_back(self, brain, monkeypatch):
        """If the hdbscan package is unavailable, maintenance degrades to the
        scipy fallback instead of crashing (design doc §4.7)."""
        import sys

        class StubModules:
            def __getattr__(self, name):
                raise ImportError("hdbscan missing")

        monkeypatch.setitem(sys.modules, "hdbscan", None)
        cfg = make_config(stability_threshold=0.0)
        rng = np.random.default_rng(7)
        _, result = self._seed_and_run(brain, cfg, rng)
        assert result.details["algorithm"] == "agglomerative"
        assert result.details["clusters_created"] == 3


# ---------------------------------------------------------------------------
# task_clustering — threshold gate (§12.6 #7), state stamping, log wiring
# ---------------------------------------------------------------------------

class TestTaskClustering:
    def test_below_threshold_skips(self, brain, config):
        """§12.6 #7: fewer than 100 new ECUs since the last run → skip."""
        rng = np.random.default_rng(7)
        seed_three_groups(brain, rng)
        brain.set_maintenance_state(maintainer.STATE_CLUSTERING_ECU_COUNT,
                                    str(brain.count_ecus()))
        result = maintainer.task_clustering(brain, config)
        assert result.details["skipped"] == "below_threshold"
        assert brain.list_clusters() == []

    def test_at_threshold_runs(self, brain, config):
        cfg = make_config(clustering_threshold=12,
                          stability_threshold=0.0)
        rng = np.random.default_rng(7)
        seed_three_groups(brain, rng, per_group=4)
        brain.set_maintenance_state(maintainer.STATE_CLUSTERING_ECU_COUNT,
                                    str(brain.count_ecus() - 12))
        result = maintainer.task_clustering(brain, cfg)
        assert "skipped" not in result.details
        assert result.details["clusters_created"] == 3

    def test_never_clustered_counts_from_zero(self, brain):
        """Missing baseline key → every canonical ECU counts as new; a brain
        under min_cluster_size still skips inside run_clustering."""
        rng = np.random.default_rng(7)
        for v in group_vectors(rng, 0, n=2):
            insert_ecu(brain, embedding=v)             # 2 ECUs < min size 3
        result = maintainer.task_clustering(brain, make_config())
        assert result.details["skipped"] == "too_few_ecus"

    def test_baseline_stamped_on_run(self, brain):
        lenient = make_config(stability_threshold=0.0)
        rng = np.random.default_rng(7)
        seed_three_groups(brain, rng)
        maintainer.task_clustering(brain, lenient)
        stamped = int(brain.get_maintenance_state(
            maintainer.STATE_CLUSTERING_ECU_COUNT))
        assert stamped == brain.count_ecus()

    def test_baseline_stamped_even_when_too_few(self, brain):
        """The examination happened — the baseline updates even on a
        too_few_ecus outcome (mirrors grounding's clock-stamping)."""
        rng = np.random.default_rng(7)
        seed_three_groups(brain, rng, per_group=1)
        maintainer.task_clustering(brain, make_config())
        assert brain.get_maintenance_state(
            maintainer.STATE_CLUSTERING_ECU_COUNT) is not None

    def test_run_maintenance_logs_clustering_row(self, brain):
        """Full-run wiring: the clustering task logs its own maintenance_log
        row exactly like grounding/forgetting do."""
        cfg = make_config(time_threshold_hours=0)      # force overdue
        rng = np.random.default_rng(7)
        seed_three_groups(brain, rng)
        result = maintainer.run_maintenance(brain, cfg, now=utcnow())
        actions = [t.action for t in result.tasks]
        assert actions[-1] == "clustering"
        rows = brain.list_maintenance_log(limit=5)
        assert any(r["action"] == "clustering" for r in rows)

    def test_maintenance_failure_does_not_abort_run(self, brain, monkeypatch):
        """A crashing clustering step is logged with its error; earlier task
        results survive (run_maintenance resilience contract)."""
        cfg = make_config(time_threshold_hours=0)
        rng = np.random.default_rng(7)
        seed_three_groups(brain, rng)
        monkeypatch.setattr(clustering_mod, "run_clustering",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        result = maintainer.run_maintenance(brain, cfg, now=utcnow())
        by_action = {t.action: t for t in result.tasks}
        assert by_action["clustering"].details.get("error") == "boom"
        assert by_action["forgetting"].details.get("error") is None


# ---------------------------------------------------------------------------
# review gate grouping — §12.6 #8 + fallbacks (D36)
# ---------------------------------------------------------------------------

def insert_session_candidate(brain, session_id, embedding,
                             confidence=0.7, path="repo:demo > module:m"):
    return brain.insert_session_ecu(
        session_id,
        {
            "cognition": "A candidate engineering conclusion",
            "conclusion_type": "decision",
            "scope": {"level": "repo", "path": path},
            "provenance": {"source_type": "debugging"},
            "confidence": confidence,
            "status": "active",
        },
        embedding=embedding,
    )


class TestReviewGateClusterGrouping:
    @pytest.fixture()
    def d36_config(self):
        """D36 cluster strategy + no stability floor (deterministic fit)."""
        return make_config(**{
            "review_gate.grouping_strategy": "cluster",
            "stability_threshold": 0.0,
        })

    @pytest.fixture()
    def clustered_brain(self, brain, d36_config):
        """Brain with two stored clusters over canonical ECUs."""
        rng = np.random.default_rng(11)
        for basis_idx in (0, 1):
            for v in group_vectors(rng, basis_idx, n=3):
                insert_ecu(brain, embedding=v)
        clustering_mod.run_clustering(brain, d36_config)
        assert brain.count_clusters() == 2
        session_id = brain.create_session("/repos/demo", "main")
        return brain, session_id

    def test_cluster_strategy_groups_by_nearest_cluster(
        self, clustered_brain, d36_config
    ):
        """§12.6 #8: strategy=cluster groups candidates by the cluster of
        their most-similar canonical ECU, regardless of scope_path."""
        brain, session_id = clustered_brain
        rng = np.random.default_rng(11)
        near_a = group_vectors(rng, 0, n=1)[0]
        near_b = group_vectors(rng, 1, n=1)[0]
        # different scope paths on purpose — cluster grouping ignores them
        a_id = insert_session_candidate(brain, session_id, near_a,
                                        confidence=0.9)
        b_id = insert_session_candidate(brain, session_id, near_b,
                                        confidence=0.6)

        plan = review_gate.group_for_review(brain, session_id,
                                            config=d36_config)
        assert plan.total == 2
        labels = [g.label for g in plan.groups]
        assert all(label.startswith("cluster:") for label in labels)
        by_label = {g.label: [e["id"] for e in g.ecus] for g in plan.groups}
        assert sorted(by_label[labels[0]] + by_label[labels[1]]) == \
            sorted([a_id, b_id])
        # each candidate landed in a DIFFERENT cluster group
        assert len(by_label) == 2

    def test_unmatched_candidate_keeps_scope_group(self, clustered_brain,
                                                   d36_config):
        """A candidate unrelated to every canonical belief is not forced into
        a topic — it falls back to its own scope-path group."""
        brain, session_id = clustered_brain
        rng = np.random.default_rng(11)
        loner_id = insert_session_candidate(
            brain, session_id, group_vectors(rng, 300, n=1)[0],
            confidence=0.5)

        plan = review_gate.group_for_review(brain, session_id,
                                            config=d36_config)
        assert plan.total == 1
        assert plan.groups[0].label == "repo:demo > module:m"
        assert plan.groups[0].ecus[0]["id"] == loner_id

    def test_falls_back_to_scope_when_no_clusters(self, brain, d36_config):
        """D36: cluster strategy without any stored clusters → full scope
        grouping fallback."""
        rng = np.random.default_rng(11)
        for v in group_vectors(rng, 0, n=3):     # canonical ECUs exist but
            insert_ecu(brain, embedding=v)       # were never clustered
        session_id = brain.create_session("/repos/demo", "main")
        cand = insert_session_candidate(brain, session_id,
                                        group_vectors(rng, 0, n=1)[0])

        plan = review_gate.group_for_review(brain, session_id,
                                            config=d36_config)
        assert [g.label for g in plan.groups] == ["repo:demo > module:m"]
        assert plan.groups[0].ecus[0]["id"] == cand

    def test_default_strategy_stays_scope(self, brain, d36_config):
        """Default config keeps D18 behavior even when clusters exist."""
        rng = np.random.default_rng(11)
        for basis_idx in (0, 1):
            for v in group_vectors(rng, basis_idx, n=3):
                insert_ecu(brain, embedding=v)
        clustering_mod.run_clustering(brain, d36_config)
        assert brain.count_clusters() == 2
        session_id = brain.create_session("/repos/demo", "main")
        insert_session_candidate(brain, session_id,
                                 group_vectors(rng, 0, n=1)[0])

        default_cfg = make_config()   # grouping_strategy: "scope"
        plan = review_gate.group_for_review(brain, session_id,
                                            config=default_cfg)
        assert [g.label for g in plan.groups] == ["repo:demo > module:m"]

    def test_unknown_session_raises(self, brain):
        from ec.review_gate import ReviewGateError
        with pytest.raises(ReviewGateError):
            review_gate.group_for_review(brain, "no-such-session")
