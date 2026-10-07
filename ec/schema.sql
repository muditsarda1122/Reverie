-- EC brain schema — one global SQLite database for all projects (§28.1).
--
-- Two brains, one database:
--   Canonical Brain  -> ecus + edges              (durable, human-reviewed)
--   Session Brain    -> sessions + session_ecus + session_edges (ephemeral)
--
-- Deferred tables (review_queue) are NOT created. The Maintainer's
-- maintenance_log + maintenance_state tables exist (Phase 6); the
-- clustering tables exist (Phase 9).
-- review status lives on session_ecus.review_status (§28.5).

-- ---------------------------------------------------------------------------
-- Canonical Brain: ECUs (Spec Section 1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ecus (
    id                      TEXT PRIMARY KEY,          -- uuid4
    cognition               TEXT NOT NULL,             -- IMMUTABLE (§13.2)
    conclusion_type         TEXT NOT NULL,             -- 8 types (§1.2)
    scope_level             TEXT NOT NULL,             -- 7 levels (§1.2)
    scope_path              TEXT NOT NULL,             -- e.g. "engineering > repo:fastapi"
    source_type             TEXT NOT NULL,             -- provenance.source_type
    source_id               TEXT,                      -- session id / commit / PR
    origin_agent            TEXT,                      -- model name + version
    origin_engineer         TEXT,                      -- future team attribution
    created_at              TEXT NOT NULL,             -- ISO 8601
    grounding_json          TEXT NOT NULL DEFAULT '{}',-- repo_path, files, symbols, commit, snapshot
    confidence              REAL NOT NULL,             -- [0,1], Bayesian-updated (§11)
    status                  TEXT NOT NULL DEFAULT 'active',
    evidence_pointers_json  TEXT NOT NULL DEFAULT '[]',
    metadata_json           TEXT NOT NULL DEFAULT '{}',-- last_reinforced, retrieval_count, ...
    embedding               BLOB,                      -- float32 x 384 (all-MiniLM-L6-v2)
    document                TEXT NOT NULL              -- full ECU JSON (audit fidelity)
);
CREATE INDEX IF NOT EXISTS idx_ecus_status      ON ecus(status);
CREATE INDEX IF NOT EXISTS idx_ecus_scope_level ON ecus(scope_level);
CREATE INDEX IF NOT EXISTS idx_ecus_created     ON ecus(created_at);

-- ---------------------------------------------------------------------------
-- Canonical Brain: edges (Spec Section 10.2)
-- One edge per (source, target, type) — weight accumulates (§14.3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edges (
    id                  TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES ecus(id),
    target_id           TEXT NOT NULL REFERENCES ecus(id),
    type                TEXT NOT NULL,              -- supports | contradicts | depends_on | supersedes
    weight              REAL NOT NULL DEFAULT 1.0,
    confidence_delta    REAL NOT NULL DEFAULT 0.0,  -- log-odds space, for reversible updates
    supersession_type   TEXT,                       -- cosmetic | semantic (only when type=supersedes)
    created_at          TEXT NOT NULL,
    UNIQUE (source_id, target_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type   ON edges(type);

-- ---------------------------------------------------------------------------
-- Sessions (§28.5)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    repo_path       TEXT NOT NULL,
    branch          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | closed
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    ecu_count       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_repo   ON sessions(repo_path);
CREATE INDEX IF NOT EXISTS idx_sessions_branch ON sessions(branch);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

-- ---------------------------------------------------------------------------
-- Session Brain: ECUs (§28.5)
-- embedding BLOB added (deliberate deviation from §28.5 DDL — the lightweight
-- diffuser needs session embeddings; §28.5 omits the column).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_ecus (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    cognition       TEXT NOT NULL,
    conclusion_type TEXT,
    scope_level     TEXT,
    scope_path      TEXT,
    confidence      REAL,
    source_type     TEXT,
    origin_agent    TEXT,
    created_at      TEXT,
    embedding       BLOB,
    document        TEXT NOT NULL,              -- full ECU JSON
    review_status   TEXT NOT NULL DEFAULT 'pending'  -- pending | accepted | rejected | skipped
);
CREATE INDEX IF NOT EXISTS idx_session_ecus_session ON session_ecus(session_id);
CREATE INDEX IF NOT EXISTS idx_session_ecus_review  ON session_ecus(review_status);

-- ---------------------------------------------------------------------------
-- Session Brain: lightweight edges (§28.5)
-- Supports cross-brain targets: a session ECU may point at a canonical ECU.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_edges (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    source_id       TEXT NOT NULL REFERENCES session_ecus(id),
    target_type     TEXT NOT NULL,              -- 'session_ecu' | 'canonical_ecu'
    target_id       TEXT NOT NULL,              -- session_ecus.id or ecus.id
    type            TEXT NOT NULL,              -- 'supports' | 'contradicts'
    weight          REAL,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_edges_session ON session_edges(session_id);
CREATE INDEX IF NOT EXISTS idx_session_edges_source  ON session_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_session_edges_target  ON session_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_session_edges_type    ON session_edges(type);

-- ---------------------------------------------------------------------------
-- Pending updates (§28.5): session evidence relating to canonical ECUs,
-- held until the review gate (no Session->Canonical writes, §6.7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_updates (
    id                          TEXT PRIMARY KEY,
    canonical_ecu_id            TEXT NOT NULL REFERENCES ecus(id),
    session_ecu_id              TEXT REFERENCES session_ecus(id),
    session_id                  TEXT REFERENCES sessions(id),
    relationship_type           TEXT NOT NULL,      -- 'supports' | 'contradicts'
    proposed_confidence_delta   REAL,               -- log-odds space
    timestamp                   TEXT,
    status                      TEXT NOT NULL DEFAULT 'pending'  -- pending | applied | discarded
);
CREATE INDEX IF NOT EXISTS idx_pending_canonical ON pending_updates(canonical_ecu_id);
CREATE INDEX IF NOT EXISTS idx_pending_session   ON pending_updates(session_id);
CREATE INDEX IF NOT EXISTS idx_pending_status    ON pending_updates(status);

-- ---------------------------------------------------------------------------
-- Session activation (§6.6): spreading-activation scores persist with their
-- last-updated timestamps so a resumed session can apply time-based decay.
-- Additive table (not in §28.5's DDL) — see handoffs/HANDOFF_PHASE_3_PLAN.md
-- decision D13.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_activation (
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    ecu_id          TEXT NOT NULL,              -- session or canonical ECU id
    score           REAL NOT NULL DEFAULT 0.0,
    updated_at      TEXT NOT NULL,              -- ISO 8601, per-ECU decay clock
    PRIMARY KEY (session_id, ecu_id)
);
CREATE INDEX IF NOT EXISTS idx_session_activation_session
    ON session_activation(session_id);

-- ---------------------------------------------------------------------------
-- Maintenance log — powers ec_get_summary.last_maintenance_run (§16.3).
-- The Maintainer (Phase 6) writes one row per task plus a 'full_run'
-- summary; the review gate also logs here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_log (
    id              TEXT PRIMARY KEY,
    run_at          TEXT NOT NULL,          -- ISO 8601
    action          TEXT NOT NULL,          -- 'full_run' | 'forgetting' | 'grounding' | 'clustering' | 'edge_pruning' | 'review_gate'
    details         TEXT,                   -- JSON summary of what was done
    ecus_affected   INTEGER DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Maintenance state (Phase 6): persistent key/value tracking for the
-- Maintainer's trigger checks. Rows: 'last_run_at', 'last_run_ecu_count',
-- 'last_grounding_check_at'. last_run_ecu_count is compared against
-- COUNT(*) FROM ecus to detect ecu_threshold new canonical ECUs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Clusters (Phase 9, design doc §4.5): cognitive clusters discovered by
-- HDBSCAN over canonical ECU embeddings (§9.5 — no pre-installed categories).
-- Clusters are EPHEMERAL: recomputed wholesale on each clustering run (the
-- label numbers are not stable across runs), so a run clears both tables
-- before repopulating them. Session Brain ECUs are never clustered (§4.4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clusters (
    id          TEXT PRIMARY KEY,
    label       INTEGER NOT NULL,       -- HDBSCAN cluster label
    stability   REAL NOT NULL,          -- HDBSCAN persistence score
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL           -- updated when re-clustered
);

CREATE TABLE IF NOT EXISTS cluster_memberships (
    ecu_id      TEXT NOT NULL REFERENCES ecus(id),
    cluster_id  TEXT NOT NULL REFERENCES clusters(id),
    weight      REAL NOT NULL,          -- cluster stability score
    PRIMARY KEY (ecu_id, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_memberships_cluster
    ON cluster_memberships(cluster_id);
