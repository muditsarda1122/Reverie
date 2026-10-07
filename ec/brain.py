"""EC brain — SQLite storage for ECUs, edges, and sessions.

One global database for all projects (§28.1), default ``~/.ec/ec.db``
(override the directory with the ``EC_HOME`` env var). Two brains in one
database (§4): the Canonical Brain (``ecus`` + ``edges`` tables) and the
Session Brain (``session_ecus`` + ``session_edges`` tables).

ECU mutability follows §13: cognition/scope/provenance are immutable (change
requires a new ECU with a supersedes edge); confidence/status/edges/metadata
are mutable in place (§13.3 — this module deliberately exposes no way to
mutate cognition).

Schema lives in ``schema.sql`` next to this module and is applied on first
open. WAL mode is enabled for concurrent reads (§ technical constraints).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import (
    CONCLUSION_TYPES,
    ECU_STATUSES,
    EDGE_TYPES,
    RETRIEVABLE_STATUSES,
    SCOPE_LEVELS,
    default_db_path,
)
from .embeddings import from_blob, to_blob

log = logging.getLogger("ec.brain")

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_DEFAULT_METADATA = {
    "last_reinforced": None,
    "last_challenged": None,
    "last_retrieved": None,
    "retrieval_count": 0,
    "cluster_memberships": [],
    "has_pending_updates": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrainError(RuntimeError):
    """Storage-level failure with an actionable message (§16.4)."""


class Brain:
    """SQLite-backed Engineering Brain.

    Parameters
    ----------
    db_path:
        Path to the SQLite file. Defaults to ``$EC_HOME/ec.db`` (``~/.ec/ec.db``).
        The database (and parent directory) are created automatically with the
        full schema if they do not exist.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._apply_schema()
        except sqlite3.OperationalError as exc:
            raise BrainError(
                f"EC brain could not be opened at {self.db_path}: {exc}. "
                "If it is locked by another process, close other EC instances "
                "and try again."
            ) from exc

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            self._conn.executescript(fh.read())
        # Additive migration for pre-existing databases (Phase 6): SQLite has
        # no "ADD COLUMN IF NOT EXISTS", so guard the ALTER with a PRAGMA
        # check — this runs on every connect.
        cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(maintenance_log)")
        }
        if cols and "ecus_affected" not in cols:
            self._conn.execute(
                "ALTER TABLE maintenance_log "
                "ADD COLUMN ecus_affected INTEGER DEFAULT 0"
            )
        self._conn.commit()

    def table_names(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    @property
    def journal_mode(self) -> str:
        return self._conn.execute("PRAGMA journal_mode").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Brain":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_ecu_fields(
        cognition: str,
        conclusion_type: str,
        scope_level: str,
        status: str,
        confidence: float,
    ) -> None:
        if not cognition or not cognition.strip():
            raise ValueError("ECU cognition must be a non-empty string")
        if conclusion_type not in CONCLUSION_TYPES:
            raise ValueError(
                f"invalid conclusion_type {conclusion_type!r}; "
                f"expected one of {CONCLUSION_TYPES}"
            )
        if scope_level not in SCOPE_LEVELS:
            raise ValueError(
                f"invalid scope level {scope_level!r}; expected one of {SCOPE_LEVELS}"
            )
        if status not in ECU_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; expected one of {ECU_STATUSES}"
            )
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")

    @staticmethod
    def _embedding_to_bytes(embedding) -> bytes | None:
        if embedding is None:
            return None
        if isinstance(embedding, (bytes, bytearray)):
            return bytes(embedding)
        if isinstance(embedding, np.ndarray):
            return to_blob(embedding)
        raise TypeError(f"embedding must be bytes or np.ndarray, got {type(embedding)}")

    # ------------------------------------------------------------------
    # ECU CRUD (Canonical Brain)
    # ------------------------------------------------------------------

    def insert_ecu(self, ecu: dict, embedding=None) -> str:
        """Insert an ECU into the Canonical Brain. Returns the ECU id.

        ``ecu`` follows the Section 1 structure. Nested sub-objects may be
        omitted; defaults are applied (uuid4 id, current timestamp, empty
        grounding/metadata, status 'active'). ``embedding`` is a float32
        np.ndarray or bytes (384-dim).
        """
        scope = ecu.get("scope", {})
        provenance = ecu.get("provenance", {})
        ecu_id = ecu.get("id") or str(uuid.uuid4())
        created_at = provenance.get("created_at") or _now_iso()
        confidence = float(ecu.get("confidence", 0.5))
        status = ecu.get("status", "active")
        cognition = ecu.get("cognition", "")
        conclusion_type = ecu.get("conclusion_type", "")
        scope_level = scope.get("level", "")
        self._validate_ecu_fields(
            cognition, conclusion_type, scope_level, status, confidence
        )

        grounding = ecu.get("grounding", {}) or {}
        evidence = ecu.get("evidence_pointers", []) or []
        metadata = {**_DEFAULT_METADATA, **(ecu.get("metadata") or {})}
        embedding_bytes = self._embedding_to_bytes(embedding)

        document = {
            "id": ecu_id,
            "cognition": cognition,
            "conclusion_type": conclusion_type,
            "scope": {"level": scope_level, "path": scope.get("path", scope_level)},
            "provenance": {
                "source_type": provenance.get("source_type", "session"),
                "source_id": provenance.get("source_id"),
                "origin_agent": provenance.get("origin_agent"),
                "origin_engineer": provenance.get("origin_engineer"),
                "created_at": created_at,
            },
            "grounding": grounding,
            "confidence": confidence,
            "status": status,
            "evidence_pointers": evidence,
            "metadata": metadata,
        }

        try:
            self._conn.execute(
                """
                INSERT INTO ecus (
                    id, cognition, conclusion_type, scope_level, scope_path,
                    source_type, source_id, origin_agent, origin_engineer,
                    created_at, grounding_json, confidence, status,
                    evidence_pointers_json, metadata_json, embedding, document
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ecu_id,
                    cognition,
                    conclusion_type,
                    scope_level,
                    document["scope"]["path"],
                    document["provenance"]["source_type"],
                    document["provenance"]["source_id"],
                    document["provenance"]["origin_agent"],
                    document["provenance"]["origin_engineer"],
                    created_at,
                    json.dumps(grounding),
                    confidence,
                    status,
                    json.dumps(evidence),
                    json.dumps(metadata),
                    embedding_bytes,
                    json.dumps(document),
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise BrainError(f"could not insert ECU {ecu_id}: {exc}") from exc
        return ecu_id

    @staticmethod
    def _row_to_ecu(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "cognition": row["cognition"],
            "conclusion_type": row["conclusion_type"],
            "scope": {"level": row["scope_level"], "path": row["scope_path"]},
            "provenance": {
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "origin_agent": row["origin_agent"],
                "origin_engineer": row["origin_engineer"],
                "created_at": row["created_at"],
            },
            "grounding": json.loads(row["grounding_json"]),
            "confidence": row["confidence"],
            "status": row["status"],
            "evidence_pointers": json.loads(row["evidence_pointers_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "embedding": row["embedding"],  # bytes | None
        }

    def get_ecu(self, ecu_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM ecus WHERE id = ?", (ecu_id,)
        ).fetchone()
        return self._row_to_ecu(row) if row else None

    def ecu_exists(self, ecu_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM ecus WHERE id = ?", (ecu_id,)
            ).fetchone()
            is not None
        )

    def list_ecus(
        self,
        status: str | None = None,
        scope_level: str | None = None,
        statuses: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict]:
        """List canonical ECUs, optionally filtered.

        ``status`` narrows to one status; ``statuses`` (clustering, design
        doc §4.4) accepts several at once and wins when both are given.
        Ordered by created_at.
        """
        query = "SELECT * FROM ecus"
        clauses, params = [], []
        if statuses:
            clauses.append(
                f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        elif status is not None:
            clauses.append("status = ?")
            params.append(status)
        if scope_level is not None:
            clauses.append("scope_level = ?")
            params.append(scope_level)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        return [self._row_to_ecu(r) for r in self._conn.execute(query, params)]

    def count_ecus(self, status: str | None = None) -> int:
        if status is None:
            return self._conn.execute("SELECT COUNT(*) FROM ecus").fetchone()[0]
        return self._conn.execute(
            "SELECT COUNT(*) FROM ecus WHERE status = ?", (status,)
        ).fetchone()[0]

    def list_ecus_for_repo(
        self,
        repo_path: str,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        """Canonical ECUs grounded in ``repo_path`` (grounding verification,
        design doc §3.2), optionally narrowed to ``statuses``.

        grounding_json is a JSON document column; the repo filter runs
        Python-side (brain sizes are small — 50–100 ECUs at current scale).
        Ordered by created_at like list_ecus."""
        query = "SELECT * FROM ecus"
        clauses, params = [], []
        if statuses:
            clauses.append(
                f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(query, params)
        return [
            self._row_to_ecu(r)
            for r in rows
            if json.loads(r["grounding_json"]).get("repo_path") == repo_path
        ]

    # -- mutable fields only (§13.3) ------------------------------------

    def _rewrite_document(self, ecu_id: str) -> None:
        """Regenerate the audit document from the authoritative columns."""
        ecu = self.get_ecu(ecu_id)
        if ecu is None:
            return
        doc = {k: v for k, v in ecu.items() if k != "embedding"}
        self._conn.execute(
            "UPDATE ecus SET document = ? WHERE id = ?",
            (json.dumps(doc), ecu_id),
        )

    def update_ecu_confidence(self, ecu_id: str, new_confidence: float) -> None:
        """In-place Bayesian-updated confidence (§13.3)."""
        if not 0.0 <= new_confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {new_confidence}")
        cur = self._conn.execute(
            "UPDATE ecus SET confidence = ? WHERE id = ?", (new_confidence, ecu_id)
        )
        if cur.rowcount == 0:
            raise BrainError(f"no ECU with id {ecu_id}")
        self._rewrite_document(ecu_id)
        self._conn.commit()

    def update_ecu_status(self, ecu_id: str, new_status: str) -> None:
        """Status lifecycle transitions (§13.3)."""
        if new_status not in ECU_STATUSES:
            raise ValueError(
                f"invalid status {new_status!r}; expected one of {ECU_STATUSES}"
            )
        cur = self._conn.execute(
            "UPDATE ecus SET status = ? WHERE id = ?", (new_status, ecu_id)
        )
        if cur.rowcount == 0:
            raise BrainError(f"no ECU with id {ecu_id}")
        self._rewrite_document(ecu_id)
        self._conn.commit()

    def update_ecu_metadata(self, ecu_id: str, **fields) -> None:
        """Merge keys into the ECU's metadata (last_retrieved, retrieval_count…)."""
        row = self._conn.execute(
            "SELECT metadata_json FROM ecus WHERE id = ?", (ecu_id,)
        ).fetchone()
        if row is None:
            raise BrainError(f"no ECU with id {ecu_id}")
        metadata = json.loads(row["metadata_json"])
        metadata.update(fields)
        self._conn.execute(
            "UPDATE ecus SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), ecu_id),
        )
        self._rewrite_document(ecu_id)
        self._conn.commit()

    def update_ecu_grounding(self, ecu_id: str, new_grounding: dict) -> None:
        """§20.3: grounding is mutable. File paths can be updated when code
        was moved (a refactor should not deprecate a valid conclusion);
        ``commit_hash`` / ``code_snapshot`` remain as historical references.

        Keys in ``new_grounding`` overwrite their old values; keys not
        mentioned are kept (so a file-path refresh never silently drops the
        commit anchor). Cognition stays immutable — this updates provenance
        of the evidence, not the conclusion.
        """
        if not isinstance(new_grounding, dict):
            raise TypeError("new_grounding must be a dict")
        row = self._conn.execute(
            "SELECT grounding_json FROM ecus WHERE id = ?", (ecu_id,)
        ).fetchone()
        if row is None:
            raise BrainError(f"no ECU with id {ecu_id}")
        merged = {**json.loads(row["grounding_json"]), **new_grounding}
        self._conn.execute(
            "UPDATE ecus SET grounding_json = ? WHERE id = ?",
            (json.dumps(merged), ecu_id),
        )
        self._rewrite_document(ecu_id)
        self._conn.commit()

    def add_evidence_pointer(self, ecu_id: str, pointer: str) -> None:
        """§20.3: evidence can gain new supporting references (reinforcement,
        not a change to the conclusion).

        Appends to ``evidence_pointers`` without replacing existing entries;
        adding an identical pointer twice is a no-op.
        """
        if not pointer or not str(pointer).strip():
            raise ValueError("evidence pointer must be a non-empty string")
        row = self._conn.execute(
            "SELECT evidence_pointers_json FROM ecus WHERE id = ?", (ecu_id,)
        ).fetchone()
        if row is None:
            raise BrainError(f"no ECU with id {ecu_id}")
        pointers = json.loads(row["evidence_pointers_json"])
        if pointer not in pointers:
            pointers.append(pointer)
            self._conn.execute(
                "UPDATE ecus SET evidence_pointers_json = ? WHERE id = ?",
                (json.dumps(pointers), ecu_id),
            )
            self._rewrite_document(ecu_id)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Edges (Canonical Brain)
    # ------------------------------------------------------------------

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        type: str,
        weight: float = 1.0,
        confidence_delta: float = 0.0,
        supersession_type: str | None = None,
    ) -> str:
        """Create an edge, or accumulate into an existing one.

        §14.3: one edge per (source, target, type) — repeated evidence for the
        same relationship increases the existing edge's weight (capped at 1.0)
        and adds to its confidence_delta rather than creating duplicates.
        """
        if type not in EDGE_TYPES:
            raise ValueError(f"invalid edge type {type!r}; expected one of {EDGE_TYPES}")
        for role, ecu_id in (("source", source_id), ("target", target_id)):
            if not self.ecu_exists(ecu_id):
                raise BrainError(f"{role} ECU {ecu_id} does not exist")
        if type == "supersedes":
            if supersession_type not in ("cosmetic", "semantic"):
                supersession_type = "semantic"
        else:
            supersession_type = None

        existing = self._conn.execute(
            "SELECT id, weight, confidence_delta FROM edges "
            "WHERE source_id = ? AND target_id = ? AND type = ?",
            (source_id, target_id, type),
        ).fetchone()
        if existing:
            new_weight = min(1.0, existing["weight"] + weight)
            new_delta = existing["confidence_delta"] + confidence_delta
            self._conn.execute(
                "UPDATE edges SET weight = ?, confidence_delta = ? WHERE id = ?",
                (new_weight, new_delta, existing["id"]),
            )
            self._conn.commit()
            return existing["id"]

        edge_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO edges (
                id, source_id, target_id, type, weight,
                confidence_delta, supersession_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id, source_id, target_id, type, weight,
                confidence_delta, supersession_type, _now_iso(),
            ),
        )
        self._conn.commit()
        return edge_id

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "type": row["type"],
            "weight": row["weight"],
            "confidence_delta": row["confidence_delta"],
            "supersession_type": row["supersession_type"],
            "created_at": row["created_at"],
        }

    def get_edges_from(self, source_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE source_id = ?", (source_id,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_to(self, target_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE target_id = ?", (target_id,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_for(self, ecu_id: str) -> list[dict]:
        """All edges in both directions (traversal needs inbound edges too)."""
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE source_id = ? OR target_id = ?",
            (ecu_id, ecu_id),
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def list_all_edges(self) -> list[dict]:
        """Every canonical edge — the §14.5 pruning scan's input."""
        rows = self._conn.execute("SELECT * FROM edges").fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_between(self, source_id: str, target_id: str,
                          edge_type: str | None = None) -> list[dict]:
        """Edges from one specific ECU to another, optionally by type.

        Directional: only source→target rows are returned. The §14.3
        opposing-edges check uses this to detect a supports+contradicts
        pair on the same directed edge.
        """
        sql = ("SELECT * FROM edges WHERE source_id = ? AND target_id = ?")
        params: list[str] = [source_id, target_id]
        if edge_type:
            sql += " AND type = ?"
            params.append(edge_type)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def edge_count(self, ecu_id: str) -> int:
        """Degree (in + out) — feeds network_richness in ranking (§11.5)."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (ecu_id, ecu_id),
        ).fetchone()[0]

    def delete_edge(self, edge_id: str) -> None:
        self._conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Sessions — minimal storage helpers.
    # Lifecycle logic (resume, carry-over of skipped ECUs, /ec-stop cleanup)
    # is Phase 4 (ec/session.py, ec/review_gate.py).
    # ------------------------------------------------------------------

    def create_session(
        self, repo_path: str, branch: str, session_id: str | None = None
    ) -> str:
        sid = session_id or str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO sessions (id, repo_path, branch, status, started_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (sid, repo_path, branch, _now_iso()),
        )
        self._conn.commit()
        return sid

    def get_active_session(self, repo_path: str, branch: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE repo_path = ? AND branch = ? "
            "AND status = 'active'",
            (repo_path, branch),
        ).fetchone()
        return dict(row) if row else None

    def most_recent_active_session_repo(self) -> str | None:
        """Repo of the most recently started active session — fallback repo
        resolution for the Maintainer when the MCP server process runs
        outside any work tree (design doc §3.2/D35)."""
        row = self._conn.execute(
            "SELECT repo_path FROM sessions WHERE status = 'active' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["repo_path"] if row else None

    def get_session(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_session_ecu(self, session_id: str, ecu: dict, embedding=None) -> str:
        """Insert an ECU into the Session Brain (no review required, §6.2)."""
        if self.get_session(session_id) is None:
            raise BrainError(f"no session with id {session_id}")
        scope = ecu.get("scope", {})
        provenance = ecu.get("provenance", {})
        ecu_id = ecu.get("id") or str(uuid.uuid4())
        conclusion_type = ecu.get("conclusion_type", "")
        scope_level = scope.get("level", "")
        confidence = float(ecu.get("confidence", 0.5))
        status = ecu.get("status", "active")
        cognition = ecu.get("cognition", "")
        self._validate_ecu_fields(
            cognition, conclusion_type, scope_level, status, confidence
        )
        document = {
            "id": ecu_id,
            "cognition": cognition,
            "conclusion_type": conclusion_type,
            "scope": {"level": scope_level, "path": scope.get("path", scope_level)},
            "provenance": {
                "source_type": provenance.get("source_type", "session"),
                "source_id": provenance.get("source_id", session_id),
                "origin_agent": provenance.get("origin_agent"),
                "origin_engineer": provenance.get("origin_engineer"),
                "created_at": provenance.get("created_at") or _now_iso(),
            },
            "grounding": ecu.get("grounding", {}) or {},
            "confidence": confidence,
            "status": status,
            "evidence_pointers": ecu.get("evidence_pointers", []) or [],
            "metadata": {**_DEFAULT_METADATA, **(ecu.get("metadata") or {})},
        }
        self._conn.execute(
            """
            INSERT INTO session_ecus (
                id, session_id, cognition, conclusion_type, scope_level,
                scope_path, confidence, source_type, origin_agent, created_at,
                embedding, document, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                ecu_id,
                session_id,
                cognition,
                conclusion_type,
                scope_level,
                document["scope"]["path"],
                confidence,
                document["provenance"]["source_type"],
                document["provenance"]["origin_agent"],
                document["provenance"]["created_at"],
                self._embedding_to_bytes(embedding),
                json.dumps(document),
            ),
        )
        self._conn.execute(
            "UPDATE sessions SET ecu_count = ecu_count + 1 WHERE id = ?",
            (session_id,),
        )
        self._conn.commit()
        return ecu_id

    @staticmethod
    def _session_row_to_ecu(row: sqlite3.Row) -> dict:
        ecu = json.loads(row["document"])
        ecu["embedding"] = row["embedding"]
        ecu["session_id"] = row["session_id"]
        ecu["review_status"] = row["review_status"]
        return ecu

    def get_session_ecu(self, ecu_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM session_ecus WHERE id = ?", (ecu_id,)
        ).fetchone()
        return self._session_row_to_ecu(row) if row else None

    def session_ecu_exists(self, ecu_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM session_ecus WHERE id = ?", (ecu_id,)
            ).fetchone()
            is not None
        )

    def update_session_ecu_confidence(self, ecu_id: str, new_confidence: float) -> None:
        """Lightweight-diffusion confidence bump on a session ECU (§4.4)."""
        if not 0.0 <= new_confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {new_confidence}")
        row = self._conn.execute(
            "SELECT document FROM session_ecus WHERE id = ?", (ecu_id,)
        ).fetchone()
        if row is None:
            raise BrainError(f"no session ECU with id {ecu_id}")
        doc = json.loads(row["document"])
        doc["confidence"] = new_confidence
        self._conn.execute(
            "UPDATE session_ecus SET confidence = ?, document = ? WHERE id = ?",
            (new_confidence, json.dumps(doc), ecu_id),
        )
        self._conn.commit()

    def list_session_ecus(
        self, session_id: str, review_status: str | None = None
    ) -> list[dict]:
        query = "SELECT * FROM session_ecus WHERE session_id = ?"
        params: list = [session_id]
        if review_status is not None:
            query += " AND review_status = ?"
            params.append(review_status)
        rows = self._conn.execute(query, params).fetchall()
        return [self._session_row_to_ecu(row) for row in rows]

    # ------------------------------------------------------------------
    # Session Brain: lightweight edges (§28.5, §4.4). Sources are always
    # session ECUs; targets may be session or canonical ECUs.
    # ------------------------------------------------------------------

    def add_session_edge(
        self,
        session_id: str,
        source_id: str,
        target_id: str,
        type: str,
        weight: float = 1.0,
        target_type: str = "session_ecu",
    ) -> str:
        """Create a lightweight session edge (supports/contradicts only).

        §28.5 defines no uniqueness constraint on session_edges, but
        duplicate (source, target, type) rows are pointless — repeated
        evidence accumulates weight (capped at 1.0) like canonical edges.
        """
        if type not in ("supports", "contradicts"):
            raise ValueError(
                f"session edges are supports|contradicts only (§4.4), got {type!r}"
            )
        if target_type not in ("session_ecu", "canonical_ecu"):
            raise ValueError(f"invalid target_type {target_type!r}")
        if not self.session_ecu_exists(source_id):
            raise BrainError(f"source session ECU {source_id} does not exist")
        if target_type == "session_ecu":
            if not self.session_ecu_exists(target_id):
                raise BrainError(f"target session ECU {target_id} does not exist")
        elif not self.ecu_exists(target_id):
            raise BrainError(f"target canonical ECU {target_id} does not exist")

        existing = self._conn.execute(
            "SELECT id, weight FROM session_edges "
            "WHERE source_id = ? AND target_id = ? AND type = ?",
            (source_id, target_id, type),
        ).fetchone()
        if existing:
            new_weight = min(1.0, existing["weight"] + weight)
            self._conn.execute(
                "UPDATE session_edges SET weight = ? WHERE id = ?",
                (new_weight, existing["id"]),
            )
            self._conn.commit()
            return existing["id"]

        edge_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO session_edges (
                id, session_id, source_id, target_type, target_id,
                type, weight, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id, session_id, source_id, target_type, target_id,
                type, weight, _now_iso(),
            ),
        )
        self._conn.commit()
        return edge_id

    def list_session_edges(self, session_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM session_edges WHERE session_id = ?", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_edges_for(self, ecu_id: str) -> list[dict]:
        """All session edges touching an ECU (either direction, both brains)."""
        rows = self._conn.execute(
            "SELECT * FROM session_edges WHERE source_id = ? OR target_id = ?",
            (ecu_id, ecu_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_neighbourhood(self, ecu_id: str, session_id: str | None = None) -> list[dict]:
        """All edges touching an ECU across BOTH brains, direction-agnostic.

        Unions canonical edges with session edges (§11.4 cross-brain
        traversal, §12.1 spreading activation). ``session_id`` restricts the
        session-edge side to one session; ``None`` leaves it unfiltered
        (same convention as ``find_similar``). Each entry:
        ``{"other_id", "type", "weight", "edge_brain"}``.
        """
        out = []
        for e in self.get_edges_for(ecu_id):
            other = e["target_id"] if e["source_id"] == ecu_id else e["source_id"]
            out.append({
                "other_id": other,
                "type": e["type"],
                "weight": e["weight"],
                "edge_brain": "canonical",
            })
        for e in self.get_session_edges_for(ecu_id):
            if session_id is not None and e["session_id"] != session_id:
                continue
            other = e["target_id"] if e["source_id"] == ecu_id else e["source_id"]
            out.append({
                "other_id": other,
                "type": e["type"],
                "weight": e["weight"] or 1.0,
                "edge_brain": "session",
            })
        return out

    # ------------------------------------------------------------------
    # Pending updates (§28.5): session evidence about canonical ECUs, held
    # until the review gate. The Session Brain NEVER writes to the
    # Canonical Brain (§6.7) — it records intent here instead.
    # ------------------------------------------------------------------

    def add_pending_update(
        self,
        canonical_ecu_id: str,
        session_ecu_id: str,
        session_id: str,
        relationship_type: str,
        proposed_confidence_delta: float,
    ) -> str:
        """Record cross-brain evidence for review-gate application (§28.6)."""
        if relationship_type not in ("supports", "contradicts"):
            raise ValueError(
                f"pending update relationship must be supports|contradicts, "
                f"got {relationship_type!r}"
            )
        if not self.ecu_exists(canonical_ecu_id):
            raise BrainError(f"canonical ECU {canonical_ecu_id} does not exist")
        update_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO pending_updates (
                id, canonical_ecu_id, session_ecu_id, session_id,
                relationship_type, proposed_confidence_delta, timestamp, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                update_id, canonical_ecu_id, session_ecu_id, session_id,
                relationship_type, proposed_confidence_delta, _now_iso(),
            ),
        )
        self._conn.commit()
        return update_id

    def list_pending_updates(
        self,
        session_id: str | None = None,
        canonical_ecu_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM pending_updates"
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if canonical_ecu_id is not None:
            clauses.append("canonical_ecu_id = ?")
            params.append(canonical_ecu_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Session activation persistence (§6.6). Storage only — decay and
    # resume semantics live in ec/activation.py (Phase 3).
    # ------------------------------------------------------------------

    def save_activation(self, session_id: str, scores: dict) -> None:
        """Persist a session's activation scores with timestamps (§6.6).

        ``scores`` maps ecu_id -> (score, updated_at_iso). Rows for this
        session whose ECU id is absent are pruned, so the table mirrors the
        in-memory state exactly.
        """
        self._conn.execute(
            "DELETE FROM session_activation WHERE session_id = ?", (session_id,)
        )
        self._conn.executemany(
            "INSERT INTO session_activation (session_id, ecu_id, score, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (session_id, ecu_id, float(score), updated_at)
                for ecu_id, (score, updated_at) in scores.items()
            ],
        )
        self._conn.commit()

    def load_activation(self, session_id: str) -> dict:
        """Load persisted activation scores: ecu_id -> (score, updated_at_iso)."""
        rows = self._conn.execute(
            "SELECT ecu_id, score, updated_at FROM session_activation "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {r["ecu_id"]: (r["score"], r["updated_at"]) for r in rows}

    # ------------------------------------------------------------------
    # Session lifecycle helpers (§28.5/§28.6) — storage only; the lifecycle
    # logic lives in ec/session.py and ec/review_gate.py (Phase 4).
    # ------------------------------------------------------------------

    _REVIEW_STATUSES = ("pending", "accepted", "rejected", "skipped")

    def list_sessions(
        self,
        repo_path: str | None = None,
        branch: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM sessions"
        clauses, params = [], []
        if repo_path is not None:
            clauses.append("repo_path = ?")
            params.append(repo_path)
        if branch is not None:
            clauses.append("branch = ?")
            params.append(branch)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def close_session(self, session_id: str, ended_at: str | None = None) -> None:
        """Mark a session closed (§28.6)."""
        cur = self._conn.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ? WHERE id = ?",
            (ended_at or _now_iso(), session_id),
        )
        if cur.rowcount == 0:
            raise BrainError(f"no session with id {session_id}")
        self._conn.commit()

    def count_session_ecus(self, session_id: str) -> int:
        """Live count of ECUs currently in the Session Brain."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM session_ecus WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

    def pending_review_count(
        self, repo_path: str | None = None, branch: str | None = None
    ) -> int:
        """Session ECUs still awaiting review (§28.10 step 3).

        With ``repo_path`` + ``branch``, counts pending rows across ALL
        sessions of that working context; without them, across the whole
        brain. Rejected rows are deleted at stop and accepted ones removed,
        so what remains 'pending' is genuinely unreviewed material
        (pending + carried-over skipped ECUs are counted by their own
        statuses — this counts only ``review_status='pending'``)."""
        if repo_path is not None and branch is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM session_ecus se "
                "JOIN sessions s ON se.session_id = s.id "
                "WHERE s.repo_path = ? AND s.branch = ? "
                "AND se.review_status = 'pending'",
                (repo_path, branch),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM session_ecus WHERE review_status = 'pending'"
            ).fetchone()
        return row[0]

    def update_session_ecu_review_status(self, ecu_id: str, status: str) -> None:
        """§28.5 review_status: pending | accepted | rejected | skipped."""
        if status not in self._REVIEW_STATUSES:
            raise ValueError(
                f"invalid review status {status!r}; "
                f"expected one of {self._REVIEW_STATUSES}"
            )
        cur = self._conn.execute(
            "UPDATE session_ecus SET review_status = ? WHERE id = ?",
            (status, ecu_id),
        )
        if cur.rowcount == 0:
            raise BrainError(f"no session ECU with id {ecu_id}")
        self._conn.commit()

    def delete_session_edges_for(self, ecu_ids: list[str]) -> int:
        """Delete session edges touching any of the given ECUs (either
        direction). §28.6 cleanup for accepted/rejected ECUs."""
        if not ecu_ids:
            return 0
        placeholders = ",".join("?" for _ in ecu_ids)
        cur = self._conn.execute(
            f"DELETE FROM session_edges WHERE source_id IN ({placeholders}) "
            f"OR target_id IN ({placeholders})",
            (*ecu_ids, *ecu_ids),
        )
        self._conn.commit()
        return cur.rowcount

    def delete_session_ecu(self, ecu_id: str) -> None:
        """Remove a session ECU row (§28.6: accepted/rejected cleanup)."""
        self._conn.execute("DELETE FROM session_ecus WHERE id = ?", (ecu_id,))
        self._conn.commit()

    def set_pending_update_status(self, update_id: str, status: str) -> None:
        """§28.5 pending_updates status: pending | applied | discarded."""
        if status not in ("pending", "applied", "discarded"):
            raise ValueError(f"invalid pending-update status {status!r}")
        cur = self._conn.execute(
            "UPDATE pending_updates SET status = ? WHERE id = ?",
            (status, update_id),
        )
        if cur.rowcount == 0:
            raise BrainError(f"no pending update with id {update_id}")
        self._conn.commit()

    def delete_pending_update(self, update_id: str) -> None:
        """§28.6: applied/discarded pending-update records are then deleted."""
        self._conn.execute(
            "DELETE FROM pending_updates WHERE id = ?", (update_id,)
        )
        self._conn.commit()

    def reassign_session_rows(
        self, from_session_ids: list[str], to_session_id: str
    ) -> list[str]:
        """Carry unreviewed ECUs over to a new session (§28.5 /ec-start step 5).

        Moves session_ecus with review_status pending|skipped from the given
        (closed) sessions into ``to_session_id``, along with their session
        edges and pending updates. Returns the moved ECU ids.
        """
        if not from_session_ids:
            return []
        if self.get_session(to_session_id) is None:
            raise BrainError(f"no session with id {to_session_id}")
        from_ph = ",".join("?" for _ in from_session_ids)
        rows = self._conn.execute(
            f"SELECT id FROM session_ecus WHERE session_id IN ({from_ph}) "
            "AND review_status IN ('pending', 'skipped')",
            from_session_ids,
        ).fetchall()
        moved = [r["id"] for r in rows]
        if not moved:
            return []
        id_ph = ",".join("?" for _ in moved)
        self._conn.execute(
            f"UPDATE session_ecus SET session_id = ? WHERE id IN ({id_ph})",
            (to_session_id, *moved),
        )
        self._conn.execute(
            f"UPDATE session_edges SET session_id = ? "
            f"WHERE session_id IN ({from_ph}) "
            f"AND (source_id IN ({id_ph}) OR target_id IN ({id_ph}))",
            (to_session_id, *from_session_ids, *moved, *moved),
        )
        self._conn.execute(
            f"UPDATE pending_updates SET session_id = ? "
            f"WHERE session_id IN ({from_ph}) AND session_ecu_id IN ({id_ph})",
            (to_session_id, *from_session_ids, *moved),
        )
        self._conn.execute(
            "UPDATE sessions SET ecu_count = ecu_count + ? WHERE id = ?",
            (len(moved), to_session_id),
        )
        self._conn.commit()
        return moved

    def delete_session_activation(self, session_id: str) -> None:
        """Drop a session's persisted activation scores (session is closed —
        a new session starts with a fresh slate, §12.2)."""
        self._conn.execute(
            "DELETE FROM session_activation WHERE session_id = ?", (session_id,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Brain statistics — ec_get_summary (§16.3) + maintenance log.
    # ------------------------------------------------------------------

    def count_ecus_by_scope(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT scope_level, COUNT(*) AS n FROM ecus GROUP BY scope_level"
        ).fetchall()
        return {r["scope_level"]: r["n"] for r in rows}

    def last_ecu_added(self) -> str | None:
        return self._conn.execute("SELECT MAX(created_at) FROM ecus").fetchone()[0]

    def last_maintenance_run(self) -> str | None:
        return self._conn.execute(
            "SELECT MAX(run_at) FROM maintenance_log"
        ).fetchone()[0]

    def log_maintenance(
        self, action: str, details: str | None = None, ecus_affected: int = 0,
        run_at: str | None = None,
    ) -> str:
        """Record a maintenance-log row (review gate, Maintainer tasks).

        ``run_at`` defaults to now; injectable for time-dependent tests.
        """
        row_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO maintenance_log (id, run_at, action, details, ecus_affected) "
            "VALUES (?, ?, ?, ?, ?)",
            (row_id, run_at or _now_iso(), action, details, int(ecus_affected)),
        )
        self._conn.commit()
        return row_id

    def list_maintenance_log(self, limit: int | None = None) -> list[dict]:
        """Maintenance-log rows, newest first (testing / status reporting)."""
        query = (
            "SELECT id, run_at, action, details, ecus_affected "
            "FROM maintenance_log ORDER BY run_at DESC"
        )
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(query)]

    # -- maintenance_state (Phase 6): trigger-check bookkeeping -----------

    def get_maintenance_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM maintenance_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_maintenance_state(self, key: str, value: str) -> None:
        """Upsert a persistent maintenance-state value (stored as TEXT)."""
        self._conn.execute(
            "INSERT INTO maintenance_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Clusters + cluster_memberships (Phase 9, design doc §4.4–§4.5).
    # Cognitive clusters are ephemeral and recomputed wholesale by each
    # clustering run — clear_cluster_memberships wipes both tables before
    # they are repopulated (HDBSCAN labels are not stable across runs).
    # ------------------------------------------------------------------

    def get_or_create_cluster(self, label: int, stability: float,
                              now: str | None = None) -> str:
        """Return the id of the cluster with this HDBSCAN label, creating it
        if absent. ``stability`` (the HDBSCAN persistence score) is refreshed
        on existing rows and ``updated_at`` re-stamped."""
        ts = now or _now_iso()
        row = self._conn.execute(
            "SELECT id FROM clusters WHERE label = ?", (int(label),)
        ).fetchone()
        if row is not None:
            self._conn.execute(
                "UPDATE clusters SET stability = ?, updated_at = ? WHERE id = ?",
                (float(stability), ts, row["id"]),
            )
            self._conn.commit()
            return row["id"]
        cluster_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO clusters (id, label, stability, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cluster_id, int(label), float(stability), ts, ts),
        )
        self._conn.commit()
        return cluster_id

    def add_cluster_membership(self, ecu_id: str, cluster_id: str,
                               weight: float) -> None:
        """Attach an ECU to a cluster (weight = cluster stability). Idempotent
        per (ecu_id, cluster_id); unknown ids raise BrainError."""
        if not self.ecu_exists(ecu_id):
            raise BrainError(f"no ECU with id {ecu_id}")
        if self._conn.execute(
            "SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone() is None:
            raise BrainError(f"no cluster with id {cluster_id}")
        self._conn.execute(
            "INSERT INTO cluster_memberships (ecu_id, cluster_id, weight) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (ecu_id, cluster_id, float(weight)),
        )
        self._conn.commit()

    def clear_cluster_memberships(self) -> int:
        """Drop every membership and cluster (design doc §4.4 step 3).

        Clusters are recomputed from scratch on each run — old rows would
        dangle once HDBSCAN's new labels replace the old ones. Returns the
        number of memberships removed."""
        cur = self._conn.execute("DELETE FROM cluster_memberships")
        self._conn.execute("DELETE FROM clusters")
        self._conn.commit()
        return cur.rowcount

    def list_clusters(self) -> list[dict]:
        """All stored clusters, ordered by HDBSCAN label."""
        rows = self._conn.execute(
            "SELECT id, label, stability, created_at, updated_at "
            "FROM clusters ORDER BY label"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_clusters(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]

    def memberships_for_cluster(self, cluster_id: str) -> list[str]:
        """ECU ids belonging to a cluster."""
        return [
            r["ecu_id"]
            for r in self._conn.execute(
                "SELECT ecu_id FROM cluster_memberships WHERE cluster_id = ?",
                (cluster_id,),
            )
        ]

    def clusters_for_ecu(self, ecu_id: str) -> list[dict]:
        """Clusters an ECU belongs to (usually zero or one), label-ordered."""
        rows = self._conn.execute(
            "SELECT c.id, c.label, c.stability, m.weight "
            "FROM cluster_memberships m JOIN clusters c ON c.id = m.cluster_id "
            "WHERE m.ecu_id = ? ORDER BY c.label",
            (ecu_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Similarity search — over BOTH brains (§4.2: 'Both the Session Brain
    # and Canonical Brain participate in retrieval'). No vector DB (§28.14):
    # numpy dot products over float32 BLOBs are fast at 50–1000 ECU scale.
    # ------------------------------------------------------------------

    def find_similar(
        self,
        query_embedding,
        threshold: float = 0.0,
        top_k: int | None = None,
        include_session: bool = True,
        session_id: str | None = None,
        statuses: tuple[str, ...] = RETRIEVABLE_STATUSES,
    ) -> list[dict]:
        """Cosine-similarity search over canonical + session ECUs.

        Returns a list of ``{"id", "similarity", "brain"}`` sorted by
        similarity descending. Vectors are L2-normalized, so cosine
        similarity is a dot product. ``statuses`` filters canonical ECUs
        (session ECUs are always live working memory). When ``session_id``
        is given, only that session's ECUs are searched.
        """
        if isinstance(query_embedding, (bytes, bytearray)):
            qvec = from_blob(bytes(query_embedding))
        else:
            qvec = np.asarray(query_embedding, dtype=np.float32)

        rows: list[tuple[str, bytes, str]] = []

        placeholders = ",".join("?" for _ in statuses)
        for r in self._conn.execute(
            f"SELECT id, embedding FROM ecus "
            f"WHERE embedding IS NOT NULL AND status IN ({placeholders})",
            statuses,
        ):
            rows.append((r["id"], r["embedding"], "canonical"))

        if include_session:
            if session_id is not None:
                sess_rows = self._conn.execute(
                    "SELECT id, embedding FROM session_ecus "
                    "WHERE embedding IS NOT NULL AND session_id = ?",
                    (session_id,),
                ).fetchall()
            else:
                sess_rows = self._conn.execute(
                    "SELECT id, embedding FROM session_ecus "
                    "WHERE embedding IS NOT NULL"
                ).fetchall()
            for r in sess_rows:
                rows.append((r["id"], r["embedding"], "session"))

        results = []
        for ecu_id, blob, brain in rows:
            sim = float(np.dot(qvec, from_blob(blob)))
            if sim >= threshold:
                results.append({"id": ecu_id, "similarity": sim, "brain": brain})
        results.sort(key=lambda r: r["similarity"], reverse=True)
        if top_k is not None:
            results = results[:top_k]
        return results
