"""EC Clustering — cognitive clusters emerge from ECU embeddings (§9.5).

The Canonical Brain has no pre-installed categories: density-based
clustering over canonical ECU embeddings discovers which beliefs group
into topics (design doc Section 4). HDBSCAN is used because it needs no
cluster count up front, finds varying-density clusters, and leaves ECUs
that belong nowhere as noise instead of forcing them in.

What is clustered:

- ONLY canonical ECUs whose status is in ``clustering.cluster_statuses``
  (default active/challenged/open_question). Superseded/deprecated/
  archived beliefs are not part of the living brain. Session Brain ECUs
  are never clustered (§4.4).
- Only ECUs that HAVE an embedding — a vectorless ECU cannot participate;
  it simply stays unclustered (no failure).

When it runs: as a Maintainer sub-task (``maintainer.task_clustering``),
gated by ``clustering.clustering_threshold`` new canonical ECUs since the
last clustering run (tracked in maintenance_state). At 50–100 ECU scale it
rarely fires — this is infrastructure for scale.

Storage: each run RECOMPUTES clusters wholesale. HDBSCAN label numbers are
not stable across runs, so ``brain.clear_cluster_memberships()`` wipes the
previous generation before the new one is written. Clusters below
``clustering.stability_threshold`` (persistence 0.6 default) and noise
points (label −1) get no membership rows.

Fallback (design doc §4.7): if the ``hdbscan`` package cannot be imported,
an agglomerative pass via ``scipy.cluster.hierarchy`` provides the same
interface (labels with −1 noise), selected explicitly by
``clustering.algorithm: "agglomerative"`` — or automatically when hdbscan
is unavailable, so a missing optional wheel degrades rather than breaks
maintenance.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("ec.clustering")

#: HDBSCAN's noise label — ECUs assigned it join no cluster.
NOISE_LABEL = -1


# ---------------------------------------------------------------------------
# algorithms — return (labels, per-cluster-stability); −1 = noise
# ---------------------------------------------------------------------------

def hdbscan_labels(embeddings: np.ndarray, min_cluster_size: int,
                   min_samples: int) -> tuple[np.ndarray, dict]:
    """HDBSCAN fit (design doc §4.4). Raises ImportError when hdbscan is
    unavailable — callers decide whether to fall back.

    Stability is HDBSCAN's cluster persistence score, keyed by label."""
    import hdbscan  # lazy: heavy import kept off the hot path

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=int(min_cluster_size),
        min_samples=int(min_samples),
    )
    clusterer.fit(embeddings)
    persistence = {
        int(label): float(score)
        for label, score in enumerate(clusterer.cluster_persistence_)
    }
    return clusterer.labels_, persistence


def agglomerative_labels(embeddings: np.ndarray, min_cluster_size: int,
                         max_clusters: int = 12) -> tuple[np.ndarray, dict]:
    """Agglomerative fallback on cosine distance (design doc §4.7).

    Cuts the dendrogram at k = 2..max_clusters (average linkage over cosine
    distance — embeddings are L2-normalized) and keeps the LARGEST cut where
    every cluster still has >= min_cluster_size members. There is no
    persistence concept here, so each cluster's "stability" is its mean
    intra-cluster pairwise cosine similarity — a coherence score in [0, 1]
    the stability threshold can filter on, exactly like HDBSCAN persistence.
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist

    n = len(embeddings)
    if n < min_cluster_size:
        return np.full(n, NOISE_LABEL, dtype=int), {}

    Z = linkage(pdist(embeddings, metric="cosine"), method="average")
    chosen: np.ndarray | None = None
    for k in range(2, min(max_clusters, n // min_cluster_size) + 1):
        raw = fcluster(Z, t=k, criterion="maxclust")
        sizes = np.bincount(raw)[1:]     # raw fcluster labels start at 1
        if not (sizes >= min_cluster_size).all():
            break
        chosen = raw

    if chosen is None:                   # no viable cut → everything noise
        return np.full(n, NOISE_LABEL, dtype=int), {}

    # Contiguous 0..C-1 output labels; sub-min_cluster_size groups → noise.
    labels = np.full(n, NOISE_LABEL, dtype=int)
    stabilities: dict = {}
    next_label = 0
    for label in sorted(set(chosen.tolist())):
        mask = chosen == label
        if int(mask.sum()) < min_cluster_size:
            continue
        labels[mask] = next_label
        stabilities[next_label] = _intra_coherence(embeddings, mask)
        next_label += 1
    return labels, stabilities


def _intra_coherence(embeddings: np.ndarray, mask: np.ndarray) -> float:
    """Mean pairwise cosine similarity within one cluster ([−1, 1])."""
    members = embeddings[mask]
    n = len(members)
    if n < 2:
        return 1.0
    sims = members @ members.T
    total = float(sims.sum() - np.trace(sims))
    return total / (n * (n - 1))


# ---------------------------------------------------------------------------
# the task body — one wholesale re-clustering of the canonical brain
# ---------------------------------------------------------------------------

def run_clustering(brain, config, repo_path=None, now=None):
    """Cluster canonical ECU embeddings; store clusters + memberships.

    Returns a ``maintainer.TaskResult`` whose details carry
    {clusters_created, noise_count, ecus_clustered, total_ecus, algorithm}.
    Skips (without writing anything) when there are too few eligible ECUs —
    below ``min_cluster_size`` there is nothing density-based clustering can
    discover. ``repo_path``/``now`` are accepted for Maintainer-signature
    compatibility and unused (clustering is repo-independent).
    """
    from .embeddings import from_blob
    from .maintainer import TaskResult

    cfg = config.clustering
    statuses = tuple(cfg.cluster_statuses)

    ecus = [
        e for e in brain.list_ecus(statuses=statuses)
        if e.get("embedding") is not None
    ]
    total_eligible = len(ecus)
    if total_eligible < int(cfg.min_cluster_size):
        return TaskResult("clustering", 0, {
            "skipped": "too_few_ecus",
            "eligible": total_eligible,
            "min_cluster_size": int(cfg.min_cluster_size),
        })

    embeddings = np.vstack([
        from_blob(bytes(e["embedding"]))
        if isinstance(e["embedding"], (bytes, bytearray))
        else np.asarray(e["embedding"], dtype=np.float32)
        for e in ecus
    ])

    labels, persistence, algorithm_used = None, {}, None
    if str(cfg.algorithm) == "hdbscan":
        try:
            labels, persistence = hdbscan_labels(
                embeddings, cfg.min_cluster_size, cfg.min_samples)
            algorithm_used = "hdbscan"
        except ImportError:
            log.warning(
                "EC Clustering: hdbscan unavailable, falling back to scipy "
                "agglomerative (design doc §4.7)")
    if algorithm_used is None:
        labels, persistence = agglomerative_labels(
            embeddings, int(cfg.min_cluster_size))
        algorithm_used = "agglomerative"

    # Wholesale replacement: old labels are meaningless after a re-fit.
    brain.clear_cluster_memberships()

    threshold = float(cfg.stability_threshold)
    created_ids: dict[int, str] = {}
    clustered = 0
    noise = 0
    for ecu, label in zip(ecus, labels.tolist()):
        if label == NOISE_LABEL:
            noise += 1
            continue
        stability = float(persistence.get(label, 0.0))
        if stability < threshold:
            noise += 1              # unstable cluster → treated as noise
            continue
        cluster_id = created_ids.get(label)
        if cluster_id is None:
            cluster_id = brain.get_or_create_cluster(label, stability)
            created_ids[label] = cluster_id
        brain.add_cluster_membership(ecu["id"], cluster_id, weight=stability)
        clustered += 1

    return TaskResult("clustering", clustered, {
        "clusters_created": len(created_ids),
        "noise_count": noise,
        "ecus_clustered": clustered,
        "total_ecus": total_eligible,
        "algorithm": algorithm_used,
    })
