"""``ec repair`` — brain integrity check and safe recovery (§28.4, D51).

The §28.4 database-locked error has told users to "run ``ec repair``" since
Phase 1; this module makes that command real. It checks structural health and
performs only SAFE, NON-DESTRUCTIVE recovery:

  1. the database file exists and is valid SQLite
  2. every required table exists (missing ones are created — schema.sql is
     all CREATE TABLE IF NOT EXISTS, so re-executing it is additive-only)
  3. the WAL is checkpointed into the main DB (consolidates ``ec.db-wal``)
  4. orphan edges (edges whose source/target ECU no longer exists) are
     removed — they reference deleted ECUs and can never be traversed
  5. orphan session ECUs (session_ecus rows pointing at a deleted session)
     are REPORTED but not touched
  6. every table is queryable (COUNT(*) smoke test, maintenance_log included)

It NEVER modifies ECU data — no cognition, confidence, status, metadata or
session-ECU changes; no diffusion or maintenance is triggered. Data-level
inconsistencies are left to the Maintainer and the review gate.

CLI: ``ec-repair [--dry-run] [--db PATH]`` (console script in pyproject.toml;
``--db`` exists for tests/ops against a non-default brain).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .config import default_db_path

#: Every table schema.sql creates. Repair verifies presence (+ queryability);
#: missing tables are created by re-applying schema.sql (additive-only rule).
REQUIRED_TABLES = (
    "cluster_memberships",
    "clusters",
    "edges",
    "ecus",
    "maintenance_log",
    "maintenance_state",
    "pending_updates",
    "session_activation",
    "session_ecus",
    "session_edges",
    "sessions",
)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


#: Substrings that mark an UNRESOLVED problem in a check value. Everything
#: else ("ok", "checkpointed", "repaired", "removed N") is a healthy or
#: fixed state.
_BAD_MARKERS = (
    "missing", "corrupt", "integrity:", "still", "unqueryable",
    "busy", "orphaned", "reference missing", "dry run", "pending",
)


def repair(brain_path=None, dry_run: bool = False) -> dict:
    """Check brain integrity and perform safe recovery (D51).

    Returns a report dict::

        {
            "brain_path": str,
            "dry_run": bool,
            "checks": {name: "ok" | "warning" | <description>, ...},
            "fixes": ["<human-readable fix>", ...],
            "healthy": bool,          # True when nothing needs fixing
        }

    Check keys: ``database_file``, ``schema_tables``, ``wal_checkpoint``,
    ``orphan_edges``, ``orphan_session_ecus``, ``table_queryability``.
    A missing or corrupt database short-circuits: nothing else can run.
    """
    path = Path(brain_path) if brain_path else default_db_path()
    report: dict = {
        "brain_path": str(path),
        "dry_run": bool(dry_run),
        "checks": {},
        "fixes": [],
        "healthy": False,
    }
    checks = report["checks"]

    # -- check 1: file exists + is a readable SQLite database ---------------
    if not path.exists():
        checks["database_file"] = f"missing ({path})"
        return report
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        checks["database_file"] = f"corrupt ({exc})"
        return report
    if quick != "ok":
        checks["database_file"] = f"integrity: {quick}"
        return report
    checks["database_file"] = "ok"

    try:
        # -- check 2: required tables ----------------------------------------
        def _tables() -> set[str]:
            return {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }

        missing = [t for t in REQUIRED_TABLES if t not in _tables()]
        if not missing:
            checks["schema_tables"] = "ok"
        elif dry_run:
            checks["schema_tables"] = f"missing {missing} (dry run — not created)"
        else:
            with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
                conn.executescript(fh.read())
            conn.commit()
            still_missing = [t for t in REQUIRED_TABLES if t not in _tables()]
            if still_missing:
                checks["schema_tables"] = f"still missing after repair: {still_missing}"
            else:
                checks["schema_tables"] = "repaired"
                report["fixes"].append(
                    f"created missing table(s): {', '.join(missing)}")

        # -- check 3: WAL checkpoint ------------------------------------------
        wal = Path(str(path) + "-wal")
        if not wal.exists() or wal.stat().st_size == 0:
            checks["wal_checkpoint"] = "ok"
        elif dry_run:
            checks["wal_checkpoint"] = f"pending ({wal.stat().st_size} bytes in -wal)"
        else:
            result_row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # (busy, log-pages, checkpointed-pages): busy=0 means it ran clean.
            if result_row[0] == 0:
                checks["wal_checkpoint"] = "checkpointed"
                report["fixes"].append("checkpointed WAL into the main database")
            else:
                checks["wal_checkpoint"] = (
                    "busy — another process holds the database; try again "
                    "after closing other EC instances")
        if not dry_run:
            conn.execute("PRAGMA journal_mode=WAL")   # keep Brain's default
            conn.commit()

        # -- check 4: orphan edges (source or target ECU gone) ----------------
        orphan_edges = [
            r["id"] for r in conn.execute(
                "SELECT e.id FROM edges e WHERE "
                "NOT EXISTS (SELECT 1 FROM ecus WHERE id = e.source_id) OR "
                "NOT EXISTS (SELECT 1 FROM ecus WHERE id = e.target_id)")
        ]
        if not orphan_edges:
            checks["orphan_edges"] = "ok"
        elif dry_run:
            checks["orphan_edges"] = f"{len(orphan_edges)} orphaned (dry run)"
        else:
            ph = ",".join("?" for _ in orphan_edges)
            conn.execute(f"DELETE FROM edges WHERE id IN ({ph})", orphan_edges)
            conn.commit()
            checks["orphan_edges"] = f"removed {len(orphan_edges)}"
            report["fixes"].append(
                f"removed {len(orphan_edges)} orphan edge(s) referencing "
                "deleted ECUs")

        # -- check 5: orphan session ECUs (report only — data rows) -----------
        orphan_sessions = [
            r["id"] for r in conn.execute(
                "SELECT se.id FROM session_ecus se WHERE NOT EXISTS "
                "(SELECT 1 FROM sessions WHERE id = se.session_id)")
        ]
        checks["orphan_session_ecus"] = (
            "ok" if not orphan_sessions
            else f"{len(orphan_sessions)} reference missing sessions "
                 "(reported, not modified)")

        # -- check 6: every table queryable ------------------------------------
        broken = []
        for table in REQUIRED_TABLES:
            try:
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            except sqlite3.DatabaseError:
                broken.append(table)
        checks["table_queryability"] = (
            "ok" if not broken else f"unqueryable: {broken}")
    finally:
        conn.close()

    report["healthy"] = not any(
        any(m in str(v) for m in _BAD_MARKERS) for v in checks.values()
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ec-repair",
        description="Check EC brain integrity and perform safe, "
                    "non-destructive recovery (§28.4).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report problems without applying any fixes",
    )
    parser.add_argument(
        "--db", default=None,
        help="brain path (default: $EC_HOME/ec.db)",
    )
    args = parser.parse_args(argv)

    report = repair(brain_path=args.db, dry_run=args.dry_run)
    for check, status in report["checks"].items():
        if status == "ok":
            mark = "✓"
        elif any(m in status for m in
                 ("missing", "corrupt", "integrity:", "still", "unqueryable",
                  "busy")):
            mark = "✗"
        else:
            mark = "⚠️"
        print(f"  {mark} {check}: {status}")
    for fix in report["fixes"]:
        print(f"  → Fixed: {fix}")
    suffix = " (dry run — nothing changed)" if args.dry_run else ""
    print(f"\nRepair complete. Brain at {report['brain_path']}.{suffix}")
    return 0 if report["healthy"] or report["fixes"] else 1


if __name__ == "__main__":
    sys.exit(main())
