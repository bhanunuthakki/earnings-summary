"""Periodic garbage collection for data/portfolio.db.

Grounded in the 2026-07-30 consumer audit (3 parallel sweeps over every reader
of financial_facts, kpi_facts, and the telemetry tables). Four policies, each
independently selectable, all archive-then-delete — deleted rows are copied
into a sidecar SQLite archive (data/archive/portfolio_gc_archive.db) in their
original schema before removal, so any prune is reversible with one
INSERT ... SELECT.

Concurrency contract (post-incident hardening, 2026-08-01)
----------------------------------------------------------
The first production apply (2026-07-31) held ONE write transaction across the
whole run: the WAL grew past 115 MB, ``alembic upgrade head`` (no busy
timeout) failed instantly, and every other writer was starved for 10+ hours —
while WAL-mode reads kept the dashboard looking healthy. The rules that
prevent a recurrence:

* **Bounded batches.** Staging (the slow read/scan work) holds no main-DB
  write lock. Archiving writes only the attached sidecar. Deletes commit in
  ``--batch-size`` transactions, so any other writer waits at most one batch
  (seconds) behind its 30s busy_timeout. If an apply aborts between the
  archive pass and the last delete batch, a re-run may archive some rows a
  second time — duplicate sidecar rows are benign and logged per run in
  ``gc_manifest``.
* **Run lock.** ``--apply`` acquires the portfolio write-set run lock
  (src/run_lock.py) that the morning pipeline orchestrator also holds, per
  AGENTS.md's one-writer-owns-the-write-set rule. A held lock is a loud,
  immediate abort — never a queue.
* **Protected window.** The CLI refuses to start inside 03:00–05:00
  America/Los_Angeles (the 04:00 pipeline window) and aborts between batches
  if a long run reaches it. ``--ignore-protected-window`` overrides.
* **Wall-clock budget.** The whole run aborts loudly past
  ``--max-runtime-min`` (checked between batches); VACUUM additionally gets
  an exclusivity preflight and its own hard ``--vacuum-timeout-min`` cap
  enforced with ``Connection.interrupt()`` — a wedged VACUUM dies loudly
  instead of livelocking for hours.

Aborts are safe: every completed batch is committed and the run is idempotent,
so a re-run resumes from current state.

Policies
--------
1. ``validation-issues`` — collapse duplicate validation_issues rows onto their
   distinct defect key (source_doc_id, ticker, rule, raw_value, expected).
   The daily validation engine re-inserts ~10.2k identical open issues every
   morning because legacy rows carry NULL fingerprints (migration 0211
   deliberately did not backfill them), so the 0211 dedupe lifecycle never
   matches. The survivor (newest raised_at) gets a real fingerprint plus
   aggregated first_seen_at/last_seen_at/occurrence_count; duplicates are
   archived and deleted. Rows referenced by fact_selection_decisions
   (validation_issue_id, FK ON DELETE NO ACTION) are never deleted.

2. ``telemetry`` — age-based retention for the three tables the audit cleared
   as safe with zero code changes: stage_transitions (read only by run_id for
   --resume of a live run), source_calls (single aggregate reader), and
   ingestion_runs (widest reader window is 7 days). pipeline_attempts is
   deliberately NOT included: run_accounting.deduplicate_completed looks for
   any prior OK attempt unbounded in time — bound that lookup first.

3. ``facts-depth`` — window-prune financial_facts history per ticker tier.
   Active watchlist / evaluation / index_member tickers keep the newest
   --keep-quarters quarterly periods and --keep-fy fiscal years; tickers with
   facts but NO active tracked_companies row lose all rows (archived, not
   just deleted). Portfolio is untouched unless --include-portfolio.
   Always kept regardless of window: TTM rows (only ~52 exist and the cockpit
   fcf_margin path prefers them) and extracted_by='s1' rows (the
   recently-IPO'd anchor data). Both sources (fmp + sec_xbrl) are pruned by
   the SAME period window — the audit showed source-selective pruning flips
   fmp_backpop.sec_covers_well and blinds the tier-audit/source-disagreement
   tripwires, so we never prune by source. Floors are enforced:
   --keep-quarters >= 16 (report §3 needs 12 display + 4 TTM-1Y CAGR baseline
   quarters, src/report/sections/_common.py) and --keep-fy >= 12
   (ANNUAL_HISTORY_YEARS = 10 plus margin for 52/53-week filers).
   Cascades handled: metric_computation_attempts rows for pruned periods,
   dangling supersedes_id pointers (nulled BEFORE the delete batches so the
   self-referential FK holds across batch boundaries), and the 0225
   resolution-plane tables (fact_observation_revisions,
   legacy_fact_evidence_match_revisions, fact_selection_decisions) by
   (fact_table/target_table, row id).

4. ``maintenance`` — ANALYZE after any applied deletion; VACUUM only with
   --vacuum (the freelist alone held ~281 MB at audit time), under the
   preflight + hard timeout described above.

NOT touched, on purpose: kpi_facts (no reader has a wall-clock filter; as-of
replay and catalog HAVING counts need old rows — dedupe via the existing
pipeline.kpi_persistence.purge_duplicate_kpi_facts instead),
fmp_endpoint_status and metric_computation_attempts as tables (current-state
grids keyed by PRIMARY KEY / unique logical key, not logs), llm_calls (cost
ledger; sealed-Ask audit FKs and the all-time eval-coverage gate need it).

Dry-run by default; ``--apply`` writes. Idempotent: a second run over the same
DB finds nothing to do. Structured JSON events go to stderr; stdout is one
JSON report. Expected one-time side effect on first apply: the thesis
evaluator / segment-deriver db_snapshots fingerprints change for pruned
tickers (they hash SELECT *), forcing one full re-run each; re-baseline
credibility priors (execution/build_confidence_observations.py) BEFORE the
first facts-depth apply so measured priors are not rebuilt from a truncated
disagreement population.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite3

from clock import now_naive_utc
from pipeline.validation_issue_store import issue_fingerprint
from run_lock import RunLock, RunLockHeldError, acquire_run_lock
from schema_compat import require_current_for_write
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "portfolio.db"
ARCHIVE_NAME = "portfolio_gc_archive.db"

QUARTER_TYPES = ("Q1", "Q2", "Q3", "Q4", "quarterly")
ANNUAL_TYPES = ("FY", "annual")
# Report §3 renders 12 quarters but loads 16 (4 extra as the TTM-1Y CAGR
# baseline, src/report/sections/_common.py:19); 10 FY display + margin for
# 52/53-week filers whose fiscal year label can straddle the calendar cut.
MIN_KEEP_QUARTERS = 16
MIN_KEEP_FY = 12

# Tickers on these active lists get windowed; facts for tickers on NO active
# list are removed entirely (portfolio joins only with --include-portfolio).
WINDOWED_LIST_TYPES = ("watchlist", "evaluation", "index_member", "etf", "none")

POLICY_NAMES = ("validation-issues", "telemetry", "facts-depth", "maintenance")

# The 0225 cutover's append-only delete guard on financial_facts. db_gc drops
# and verbatim-recreates exactly this trigger inside each delete-batch
# transaction — see the maintenance-window comment in _delete_batches().
FACTS_DELETE_GUARD_TRIGGER = "trg_financial_facts_observation_delete"

# (table, timestamp column) pairs cleared for age-based retention by the
# consumer audit. pipeline_attempts is excluded — see module docstring.
TELEMETRY_TABLES: tuple[tuple[str, str], ...] = (
    ("stage_transitions", "started_at"),
    ("source_calls", "called_at"),
    ("ingestion_runs", "started_at"),
)

# Rows deleted (or survivor rows updated) per committed transaction. Sized so
# one batch holds the write lock for seconds, not hours; any concurrent
# writer's 30s busy_timeout comfortably outlasts it.
DEFAULT_BATCH_SIZE = 20_000
# Whole-run wall-clock budget. The 2026-07-31 incident run burned 10+ hours
# silently; a healthy full apply on the 1.7 GB prod DB is minutes.
DEFAULT_MAX_RUNTIME_MIN = 120.0
# VACUUM rebuilds the whole file; 1.7 GB measured in minutes on this disk.
DEFAULT_VACUUM_TIMEOUT_MIN = 30.0
# How long the VACUUM exclusivity preflight waits for the write lock before
# concluding the DB is contended and aborting loudly.
VACUUM_PREFLIGHT_TIMEOUT_S = 15.0
# How long --apply waits for the portfolio write-set run lock. Short on
# purpose: a GC pass should yield to whoever holds the write set, not queue.
DEFAULT_LOCK_TIMEOUT_S = 60.0

# The morning pipeline's protected write window (America/Los_Angeles), per
# AGENTS.md Scheduling & Quota Discipline / directives/llm_quota_scheduling.md.
PROTECTED_WINDOW_TZ = "America/Los_Angeles"
PROTECTED_WINDOW_START_HOUR = 3
PROTECTED_WINDOW_END_HOUR = 5


class GcAbortedError(RuntimeError):
    """Loud, deliberate mid-run abort (budget, window, or lock contention).

    Every committed batch stays committed; the run is idempotent, so a re-run
    resumes from current state.
    """


class PolicyReport(BaseModel):
    policy: str
    applied: bool
    rows_deleted: dict[str, int] = Field(default_factory=dict[str, int])
    rows_updated: dict[str, int] = Field(default_factory=dict[str, int])
    detail: dict[str, int] = Field(default_factory=dict[str, int])


class FactsDepthApplyPreflight(BaseModel):
    """Structural admission and diagnostic query-plan evidence for fact GC."""

    schema_version: Literal["gc-facts-depth-apply-preflight/v1"] = (
        "gc-facts-depth-apply-preflight/v1"
    )
    foreign_keys_enabled: bool
    self_fk_target_table: str
    self_fk_from_column: str
    self_fk_to_column: str
    lookup_index_name: str
    lookup_index_columns: tuple[str, ...]
    lookup_index_unique: bool
    lookup_index_origin: str
    lookup_index_partial: bool
    sqlite_version: str
    lookup_query_plan: tuple[str, ...]


class GcRunReport(BaseModel):
    run_at: str
    db_path: str
    archive_path: str
    apply: bool
    policies: list[PolicyReport] = Field(default_factory=list["PolicyReport"])
    facts_depth_apply_preflight: FactsDepthApplyPreflight | None = None


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), file=sys.stderr)


def in_protected_window(now: datetime | None = None) -> bool:
    """True inside the 03:00–05:00 America/Los_Angeles pipeline window."""
    current = now if now is not None else datetime.now(tz=ZoneInfo(PROTECTED_WINDOW_TZ))
    return PROTECTED_WINDOW_START_HOUR <= current.hour < PROTECTED_WINDOW_END_HOUR


@dataclass(slots=True)
class _RunControl:
    """Between-batch checkpoints: wall-clock budget + protected window.

    ``checkpoint()`` is called before every transaction (and per ticker during
    staging) so an over-budget or window-crossing run aborts at the next batch
    boundary — committed work stays committed.
    """

    max_runtime_s: float
    enforce_protected_window: bool = False
    started: float = field(default_factory=time.monotonic)

    def checkpoint(self) -> None:
        elapsed = time.monotonic() - self.started
        if elapsed > self.max_runtime_s:
            _log(
                "gc_runtime_budget_exceeded",
                elapsed_s=round(elapsed, 1),
                budget_s=self.max_runtime_s,
            )
            raise GcAbortedError(
                f"runtime budget exceeded ({elapsed / 60:.1f} min > "
                f"{self.max_runtime_s / 60:g} min); committed batches are kept"
            )
        if self.enforce_protected_window and in_protected_window():
            _log(
                "gc_protected_window_abort",
                window=f"{PROTECTED_WINDOW_START_HOUR:02d}:00-"
                f"{PROTECTED_WINDOW_END_HOUR:02d}:00 {PROTECTED_WINDOW_TZ}",
            )
            raise GcAbortedError(
                "inside the protected morning-pipeline window "
                f"({PROTECTED_WINDOW_START_HOUR:02d}:00-{PROTECTED_WINDOW_END_HOUR:02d}:00 "
                f"{PROTECTED_WINDOW_TZ}); pass --ignore-protected-window to override"
            )


@contextmanager
def _txn(conn: sqlite3.Connection) -> Generator[None]:
    """One bounded IMMEDIATE write transaction.

    BEGIN IMMEDIATE takes the write lock up front, where busy_timeout applies
    (a deferred transaction's first write is a snapshot upgrade that fails
    instantly under contention). Failure to acquire is a loud GcAbortedError, not a
    silent wait — the run lock should have prevented real contention.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise GcAbortedError(f"could not take the write lock for a batch: {exc}") from exc
    try:
        # Alembic does not participate in RunLock, so the revision must be
        # checked after SQLite itself has reserved the writer slot. Keeping
        # this in the shared batch boundary also catches a migration between
        # independently committed GC batches.
        require_current_for_write(conn)
        yield
    except BaseException:
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _chunks(
    rows: Sequence[tuple[object, ...]], size: int
) -> Iterator[Sequence[tuple[object, ...]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _facts_depth_apply_preflight(conn: sqlite3.Connection) -> FactsDepthApplyPreflight:
    """Refuse fact deletion without indexed enforcement of its self-FK.

    Runtime admission uses only SQLite's structural PRAGMA contracts. The raw
    EXPLAIN output is retained as version-stamped diagnostic evidence; its
    prose is intentionally not parsed because SQLite does not stabilize that
    text as an API.
    """
    foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if foreign_keys != 1:
        raise GcAbortedError("facts-depth apply requires PRAGMA foreign_keys=ON")

    fk_groups: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute("PRAGMA foreign_key_list('financial_facts')"):
        fk_groups.setdefault(int(row[0]), []).append(row)
    fk_candidates = [
        rows for rows in fk_groups.values() if any(str(row[3]) == "supersedes_id" for row in rows)
    ]
    self_fk = (
        fk_candidates[0][0] if len(fk_candidates) == 1 and len(fk_candidates[0]) == 1 else None
    )
    if (
        self_fk is None
        or int(self_fk[1]) != 0
        or str(self_fk[2]) != "financial_facts"
        or str(self_fk[3]) != "supersedes_id"
        or str(self_fk[4]) != "id"
        or str(self_fk[5]).upper() != "NO ACTION"
        or str(self_fk[6]).upper() != "NO ACTION"
        or str(self_fk[7]).upper() != "NONE"
    ):
        raise GcAbortedError(
            "facts-depth apply requires the exact single-column self-FK "
            "financial_facts.supersedes_id REFERENCES financial_facts(id) "
            "with ON UPDATE/DELETE NO ACTION and MATCH NONE"
        )

    expected_index = "ix_0270_financial_facts_supersedes_id"
    index_row = next(
        (
            row
            for row in conn.execute("PRAGMA index_list('financial_facts')")
            if str(row[1]) == expected_index
        ),
        None,
    )
    index_columns = (
        tuple(
            str(column[0])
            for column in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (expected_index,),
            )
        )
        if index_row is not None
        else ()
    )
    if (
        index_row is None
        or bool(index_row[2])
        or str(index_row[3]) != "c"
        or bool(index_row[4])
        or index_columns != ("supersedes_id",)
    ):
        raise GcAbortedError(
            "facts-depth apply requires the exact non-partial, non-unique index "
            "ix_0270_financial_facts_supersedes_id with leading supersedes_id; "
            "upgrade the database to Alembic head before retrying"
        )

    lookup_plan = tuple(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM financial_facts WHERE supersedes_id = ?",
            (0,),
        )
    )
    return FactsDepthApplyPreflight(
        foreign_keys_enabled=True,
        self_fk_target_table=str(self_fk[2]),
        self_fk_from_column=str(self_fk[3]),
        self_fk_to_column=str(self_fk[4]),
        lookup_index_name=expected_index,
        lookup_index_columns=index_columns,
        lookup_index_unique=bool(index_row[2]),
        lookup_index_origin=str(index_row[3]),
        lookup_index_partial=bool(index_row[4]),
        sqlite_version=sqlite3.sqlite_version,
        lookup_query_plan=lookup_plan,
    )


def _resolve_database_path(db_path: Path, *, apply: bool) -> Path:
    """Canonicalize symlinks and reject hardlink aliases for mutable runs."""
    resolved = db_path.resolve(strict=True)
    if not resolved.is_file():
        raise GcAbortedError(f"database path is not a regular file: {resolved}")
    if apply and resolved.stat().st_nlink != 1:
        raise GcAbortedError(
            "database has a hardlink alias; refuse apply until exactly one "
            f"directory entry owns the SQLite file ({resolved})"
        )
    return resolved


# ---------------------------------------------------------------------------
# Archive sidecar
# ---------------------------------------------------------------------------


def attach_archive(conn: sqlite3.Connection, archive_path: Path) -> None:
    """ATTACH the sidecar archive DB and ensure its manifest table exists."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("ATTACH DATABASE ? AS gcarc", (str(archive_path),))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gcarc.gc_manifest (
            run_at TEXT NOT NULL,
            policy TEXT NOT NULL,
            source_table TEXT NOT NULL,
            rows_archived INTEGER NOT NULL
        )
        """
    )


def _archive_doomed(
    conn: sqlite3.Connection,
    *,
    table: str,
    run_at: str,
    policy: str,
    id_col: str = "id",
    doomed: str = "_gc_doomed",
) -> int:
    """Copy rows whose ids sit in the doomed temp table into gcarc.<table>.

    The archive table is a schema mirror created lazily from the live table,
    so archived rows can be restored verbatim with INSERT ... SELECT.
    Writes touch only the attached sidecar (plus a read snapshot of main), so
    this never holds the main DB's write lock however large the doomed set is.
    """
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS gcarc."{table}" AS SELECT * FROM main."{table}" WHERE 0'  # nosec B608 -- internal registry table name; no user input
    )
    cur = conn.execute(
        f'INSERT INTO gcarc."{table}" SELECT t.* FROM main."{table}" t '  # nosec B608 -- internal registry identifiers; values remain bound
        f'JOIN "{doomed}" d ON d.id = t."{id_col}"'
    )
    n = cur.rowcount
    conn.execute(
        "INSERT INTO gcarc.gc_manifest (run_at, policy, source_table, rows_archived) "
        "VALUES (?, ?, ?, ?)",
        (run_at, policy, table, n),
    )
    return n


def _delete_batches(
    conn: sqlite3.Connection,
    *,
    table: str,
    policy: str,
    ctrl: _RunControl,
    batch_size: int,
    id_col: str = "id",
    doomed: str = "_gc_doomed",
    guard_trigger: bool = False,
) -> int:
    """Delete the staged doomed rows in bounded committed transactions.

    Each batch: BEGIN IMMEDIATE → delete ≤ batch_size rows → COMMIT, so no
    transaction ever holds the write lock longer than one batch. Rows must
    already be archived (``_archive_doomed``) — an abort between batches
    leaves a consistent, resumable state.

    ``guard_trigger`` opens the 0225 append-only maintenance window PER BATCH:
    financial_facts carries an unconditional BEFORE DELETE RAISE(ABORT)
    trigger, and db_gc is the one ratchet-registered delete surface
    (tests/test_core_fact_delete_architecture.py). The trigger is dropped and
    recreated verbatim from sqlite_master inside each batch transaction —
    SQLite DDL is transactional, so any failure rolls the trigger back along
    with the data, and between batches the guard is always live.
    """
    total = 0
    while True:
        ctrl.checkpoint()
        n = 0
        with _txn(conn):
            if table == "financial_facts":
                _facts_depth_apply_preflight(conn)
            conn.execute("DROP TABLE IF EXISTS temp._gc_batch")
            conn.execute(
                f'CREATE TEMP TABLE _gc_batch AS SELECT id FROM "{doomed}" '  # nosec B608 -- internal temp-table names; batch size is int()
                f"LIMIT {int(batch_size)}"
            )
            n = conn.execute("SELECT COUNT(*) FROM _gc_batch").fetchone()[0]
            if n:
                guard_sql: str | None = None
                if guard_trigger:
                    row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                        (FACTS_DELETE_GUARD_TRIGGER,),
                    ).fetchone()
                    if row is not None:
                        guard_sql = str(row[0])
                        conn.execute(f'DROP TRIGGER "{FACTS_DELETE_GUARD_TRIGGER}"')
                if table == "financial_facts":
                    # Literal statement on purpose: the core-fact delete
                    # ratchet (tests/test_core_fact_delete_architecture.py)
                    # keeps this surface greppable — a dynamic f-string here
                    # would blind it.
                    conn.execute(
                        "DELETE FROM financial_facts WHERE id IN (SELECT id FROM _gc_batch)"
                    )
                else:
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE "{id_col}" IN (SELECT id FROM _gc_batch)'  # nosec B608 -- internal registry identifiers; archive-first delete
                    )
                if guard_sql is not None:
                    conn.execute(guard_sql)
                conn.execute(f'DELETE FROM "{doomed}" WHERE id IN (SELECT id FROM _gc_batch)')  # nosec B608 -- hardcoded temp-table names
        if not n:
            return total
        total += n
        _log("gc_batch_commit", policy=policy, table=table, rows=n, rows_total=total)


def _reset_doomed(conn: sqlite3.Connection, name: str = "_gc_doomed") -> None:
    conn.execute(f'CREATE TEMP TABLE IF NOT EXISTS "{name}" (id INTEGER PRIMARY KEY)')
    conn.execute(f'DELETE FROM "{name}"')  # nosec B608 -- hardcoded temp-table name


# ---------------------------------------------------------------------------
# Policy 1: validation_issues collapse
# ---------------------------------------------------------------------------


def collapse_validation_issues(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    run_at: str,
    ctrl: _RunControl,
    batch_size: int,
) -> PolicyReport:
    """Collapse duplicate open/resolved issues onto one row per defect key.

    Survivor = newest raised_at (ties: highest id) per distinct
    (source_doc_id, ticker, rule, raw_value, expected). Survivors get a real
    fingerprint (so the 0211 lifecycle can finally match future re-detections)
    and first_seen/last_seen/occurrence_count aggregated over the group.
    Rows referenced by fact_selection_decisions.validation_issue_id are
    protected: a referenced duplicate is simply left in place, never deleted.
    """
    report = PolicyReport(policy="validation-issues", applied=apply)

    # One set-based pass: rank every row inside its defect group. rn=1 is the
    # survivor (prefer an already-fingerprinted row so the partial unique
    # index never collides, then newest raised_at).
    conn.execute("DROP TABLE IF EXISTS temp._gc_vi")
    conn.execute(
        """
        CREATE TEMP TABLE _gc_vi AS
        SELECT id,
               ROW_NUMBER() OVER w_ord AS rn,
               COUNT(*)   OVER w_all AS n,
               MIN(raised_at) OVER w_all AS first_seen,
               MAX(raised_at) OVER w_all AS last_seen
        FROM validation_issues
        WINDOW
          w_ord AS (
            PARTITION BY COALESCE(source_doc_id, -1), COALESCE(ticker, ''), rule,
                         COALESCE(raw_value, ''), COALESCE(expected, '')
            ORDER BY (fingerprint IS NOT NULL) DESC, raised_at DESC, id DESC
          ),
          w_all AS (
            PARTITION BY COALESCE(source_doc_id, -1), COALESCE(ticker, ''), rule,
                         COALESCE(raw_value, ''), COALESCE(expected, '')
          )
        """
    )
    _reset_doomed(conn)
    conn.execute(
        """
        INSERT INTO _gc_doomed (id)
        SELECT id FROM _gc_vi
        WHERE rn > 1
          AND id NOT IN (
            SELECT validation_issue_id FROM fact_selection_decisions
            WHERE validation_issue_id IS NOT NULL
          )
        """
    )
    doomed_total = conn.execute("SELECT COUNT(*) FROM _gc_doomed").fetchone()[0]
    groups = conn.execute("SELECT COUNT(*) FROM _gc_vi WHERE rn = 1 AND n > 1").fetchone()[0]
    report.detail["duplicate_groups"] = groups
    report.rows_deleted["validation_issues"] = doomed_total
    report.rows_updated["validation_issues"] = groups

    if apply and doomed_total:
        # Aggregate lifecycle fields + a real fingerprint onto each survivor,
        # committing every batch_size updates so the write lock is never held
        # for the whole survivor set.
        survivors = conn.execute(
            """
            SELECT v.id, v.source_doc_id, v.ticker, v.rule, v.raw_value, v.expected,
                   g.n, g.first_seen, g.last_seen
            FROM validation_issues v JOIN _gc_vi g ON g.id = v.id
            WHERE g.rn = 1 AND g.n > 1
            """
        ).fetchall()
        updates = [
            (
                issue_fingerprint(
                    source_doc_id=row[1],
                    ticker=row[2],
                    rule=str(row[3]),
                    raw_value=row[4],
                    expected=row[5],
                ),
                row[7],
                row[8],
                int(row[6]),
                row[0],
            )
            for row in survivors
        ]
        for chunk in _chunks(updates, batch_size):
            ctrl.checkpoint()
            with _txn(conn):
                conn.executemany(
                    "UPDATE validation_issues SET fingerprint = ?, first_seen_at = ?, "
                    "last_seen_at = ?, occurrence_count = ? WHERE id = ?",
                    chunk,
                )
        _archive_doomed(conn, table="validation_issues", run_at=run_at, policy="validation-issues")
        _delete_batches(
            conn,
            table="validation_issues",
            policy="validation-issues",
            ctrl=ctrl,
            batch_size=batch_size,
        )
    _log(
        "gc_validation_issues",
        groups=groups,
        duplicates=doomed_total,
        action="deleted" if apply else "would_delete",
    )
    return report


# ---------------------------------------------------------------------------
# Policy 2: telemetry retention
# ---------------------------------------------------------------------------


def telemetry_retention(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    run_at: str,
    retention_days: int,
    ctrl: _RunControl,
    batch_size: int,
) -> PolicyReport:
    """Archive + delete telemetry rows older than the retention cutoff."""
    from datetime import timedelta

    report = PolicyReport(policy="telemetry", applied=apply)
    cutoff = (now_naive_utc() - timedelta(days=retention_days)).isoformat()
    report.detail["retention_days"] = retention_days

    for table, ts_col in TELEMETRY_TABLES:
        ctrl.checkpoint()
        # ingestion_runs keys on run_id, not id; resolve the rowid column.
        id_col = "id"
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if "id" not in cols:
            id_col = "rowid"
        _reset_doomed(conn)
        conn.execute(
            f'INSERT INTO _gc_doomed (id) SELECT "{id_col}" FROM "{table}" WHERE "{ts_col}" < ?',  # nosec B608 -- identifiers from the internal policy registry; cutoff bound
            (cutoff,),
        )
        n = conn.execute("SELECT COUNT(*) FROM _gc_doomed").fetchone()[0]
        if apply and n:
            _archive_doomed(conn, table=table, run_at=run_at, policy="telemetry", id_col=id_col)
            _delete_batches(
                conn,
                table=table,
                policy="telemetry",
                ctrl=ctrl,
                batch_size=batch_size,
                id_col=id_col,
            )
        report.rows_deleted[table] = n
        _log(
            "gc_telemetry",
            table=table,
            cutoff=cutoff,
            rows=n,
            action="deleted" if apply else "would_delete",
        )
    return report


# ---------------------------------------------------------------------------
# Policy 3: financial_facts depth windows
# ---------------------------------------------------------------------------


def _tier_of(conn: sqlite3.Connection) -> dict[str, str]:
    """Ticker -> active list_type; tickers absent from the map are orphans."""
    return {
        str(r[0]).upper(): str(r[1])
        for r in conn.execute(
            "SELECT ticker, list_type FROM tracked_companies WHERE archived_at IS NULL"
        )
    }


def _doom_facts_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    keep_quarters: int,
    keep_fy: int,
    drop_all: bool,
) -> int:
    """Stage prunable financial_facts ids for one ticker into _gc_doomed.

    TTM rows and extracted_by='s1' rows are never doomed (see module
    docstring); both sources prune by the same period window.
    """
    if drop_all:
        cur = conn.execute(
            "INSERT INTO _gc_doomed (id) SELECT id FROM financial_facts "
            "WHERE ticker = ? AND fiscal_period_type != 'TTM' "
            "AND COALESCE(extracted_by, '') != 's1'",
            (ticker,),
        )
        return cur.rowcount

    qmarks = ", ".join("?" for _ in QUARTER_TYPES)
    keep_periods = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT period_end FROM financial_facts "  # nosec B608 -- only qmark placeholders interpolated
            f"WHERE ticker = ? AND fiscal_period_type IN ({qmarks}) "
            f"ORDER BY period_end DESC LIMIT ?",
            (ticker, *QUARTER_TYPES, keep_quarters),
        )
    ]
    amarks = ", ".join("?" for _ in ANNUAL_TYPES)
    keep_years = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT substr(period_end, 1, 4) FROM financial_facts "  # nosec B608 -- only qmark placeholders interpolated
            f"WHERE ticker = ? AND fiscal_period_type IN ({amarks}) "
            f"ORDER BY 1 DESC LIMIT ?",
            (ticker, *ANNUAL_TYPES, keep_fy),
        )
    ]
    pmarks = ", ".join("?" for _ in keep_periods) or "''"
    ymarks = ", ".join("?" for _ in keep_years) or "''"
    cur = conn.execute(
        f"""
        INSERT INTO _gc_doomed (id)
        SELECT id FROM financial_facts
        WHERE ticker = ?
          AND COALESCE(extracted_by, '') != 's1'
          AND (
                (fiscal_period_type IN ({qmarks}) AND period_end NOT IN ({pmarks}))
             OR (fiscal_period_type IN ({amarks}) AND substr(period_end, 1, 4) NOT IN ({ymarks}))
          )
        """,  # nosec B608 -- only qmark placeholder lists interpolated; every value bound
        (ticker, *QUARTER_TYPES, *keep_periods, *ANNUAL_TYPES, *keep_years),
    )
    return cur.rowcount


def facts_depth(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    run_at: str,
    keep_quarters: int,
    keep_fy: int,
    include_portfolio: bool,
    ctrl: _RunControl,
    batch_size: int,
    tickers: list[str] | None = None,
) -> PolicyReport:
    """Window-prune financial_facts + cascades per ticker tier."""
    report = PolicyReport(policy="facts-depth", applied=apply)
    tier = _tier_of(conn)

    fact_tickers = [str(r[0]) for r in conn.execute("SELECT DISTINCT ticker FROM financial_facts")]
    if tickers:
        wanted = {t.upper() for t in tickers}
        fact_tickers = [t for t in fact_tickers if t.upper() in wanted]

    # Staging holds no main-DB write lock (reads + TEMP-table writes only), so
    # this loop — the multi-hour leg of the incident run — no longer starves
    # any other writer.
    facts_doomed = 0
    attempts_doomed = 0
    per_tier: dict[str, int] = {}
    _reset_doomed(conn)
    for t in sorted(fact_tickers):
        ctrl.checkpoint()
        lt = tier.get(t.upper())
        if lt == "portfolio" and not include_portfolio:
            continue
        if lt is not None and lt not in WINDOWED_LIST_TYPES and lt != "portfolio":
            continue
        drop_all = lt is None  # no active tracking row -> orphan, remove fully
        n = _doom_facts_for_ticker(
            conn, t, keep_quarters=keep_quarters, keep_fy=keep_fy, drop_all=drop_all
        )
        if n:
            key = lt or "orphan"
            per_tier[key] = per_tier.get(key, 0) + n
            facts_doomed += n
            _log(
                "gc_facts_depth_ticker",
                ticker=t,
                list_type=lt or "orphan",
                rows=n,
                action="deleted" if apply else "would_delete",
            )

    report.rows_deleted["financial_facts"] = facts_doomed
    for key, n in sorted(per_tier.items()):
        report.detail[f"facts_{key}"] = n

    # Stage the metric_computation_attempts cascade into its own temp table.
    # The engine discovers periods from financial_facts, so a pruned period is
    # never re-attempted; its grid rows are dead weight once the facts go.
    _reset_doomed(conn, "_gc_doomed_mca")
    conn.execute(
        """
        INSERT OR IGNORE INTO _gc_doomed_mca (id)
        SELECT a.id FROM metric_computation_attempts a
        WHERE EXISTS (
            SELECT 1 FROM financial_facts f JOIN _gc_doomed d ON d.id = f.id
            WHERE f.ticker = a.ticker AND f.period_end = a.period_end
        )
        """
    )
    attempts_doomed = conn.execute("SELECT COUNT(*) FROM _gc_doomed_mca").fetchone()[0]
    report.rows_deleted["metric_computation_attempts"] = attempts_doomed

    if not (apply and facts_doomed):
        return report

    # --- apply path: archive first, then cascades, then bounded deletes -----
    # Archive both tables BEFORE any mutation so the sidecar copies carry the
    # original values (including supersedes_id, which is nulled below).
    _archive_doomed(conn, table="financial_facts", run_at=run_at, policy="facts-depth")
    if attempts_doomed:
        _archive_doomed(
            conn,
            table="metric_computation_attempts",
            run_at=run_at,
            policy="facts-depth",
            doomed="_gc_doomed_mca",
        )
        _delete_batches(
            conn,
            table="metric_computation_attempts",
            policy="facts-depth",
            ctrl=ctrl,
            batch_size=batch_size,
            doomed="_gc_doomed_mca",
        )

    # Cascade 2: 0225 resolution-plane rows for the doomed facts (empty on
    # prod today; defensive so the integrity audit never sees dangling refs).
    # Must run before the facts delete batches consume _gc_doomed.
    for tbl, table_col, id_col in (
        ("fact_observation_revisions", "fact_table", "fact_row_id"),
        ("legacy_fact_evidence_match_revisions", "fact_table", "fact_row_id"),
        ("fact_selection_decisions", "target_table", "target_row_id"),
    ):
        try:
            with _txn(conn):
                cur = conn.execute(
                    f'DELETE FROM "{tbl}" WHERE "{table_col}" = ? '  # nosec B608 -- identifiers from the literal tuple above; values bound
                    f'AND "{id_col}" IN (SELECT id FROM _gc_doomed)',
                    ("financial_facts",),
                )
                if cur.rowcount:
                    report.rows_deleted[tbl] = cur.rowcount
        except GcAbortedError:
            raise
        except sqlite3.OperationalError:
            pass  # table absent on older schemas — nothing to cascade

    # Cascade 3: null every supersedes_id pointing into the doomed set BEFORE
    # the delete batches. Chains share one logical (ticker, period_end, fpt,
    # line_item) key so a period prune removes whole chains — but batching can
    # split a chain across transactions, and the self-referential
    # fk_financial_facts_supersedes must hold at every batch COMMIT. Nulling
    # first (survivor stragglers and doomed rows alike) removes every FK edge
    # into the doomed set; the archive above already preserved the originals.
    with _txn(conn):
        _facts_depth_apply_preflight(conn)
        cur = conn.execute(
            "UPDATE financial_facts SET supersedes_id = NULL "
            "WHERE supersedes_id IN (SELECT id FROM _gc_doomed)"
        )
        if cur.rowcount:
            report.rows_updated["financial_facts_supersedes_nulled"] = cur.rowcount

    _log(
        "gc_append_only_guard_window",
        trigger=FACTS_DELETE_GUARD_TRIGGER,
        action="per_batch_drop_and_recreate",
    )
    _delete_batches(
        conn,
        table="financial_facts",
        policy="facts-depth",
        ctrl=ctrl,
        batch_size=batch_size,
        guard_trigger=True,
    )
    return report


# ---------------------------------------------------------------------------
# Policy 4: maintenance
# ---------------------------------------------------------------------------


def maintenance(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    vacuum: bool,
    ctrl: _RunControl | None = None,
    vacuum_timeout_min: float = DEFAULT_VACUUM_TIMEOUT_MIN,
) -> PolicyReport:
    report = PolicyReport(policy="maintenance", applied=apply)
    if not apply:
        report.detail["vacuum_requested"] = int(vacuum)
        return report
    if vacuum:
        # Exclusivity preflight BEFORE any maintenance work: prove the write
        # lock is attainable under a short timeout. The incident run sat on a
        # contended DB for hours; this turns that into a 15s loud abort.
        if ctrl is not None:
            ctrl.checkpoint()
        with suppress(sqlite3.OperationalError):  # not attached (maintenance-only)
            conn.execute("DETACH DATABASE gcarc")
        conn.execute(f"PRAGMA busy_timeout = {int(VACUUM_PREFLIGHT_TIMEOUT_S * 1000)}")
        try:
            with _txn(conn):
                pass
        except GcAbortedError:
            _log("gc_vacuum_preflight_failed", timeout_s=VACUUM_PREFLIGHT_TIMEOUT_S)
            raise
        finally:
            conn.execute("PRAGMA busy_timeout = 30000")
    if ctrl is not None:
        ctrl.checkpoint()
    conn.execute("ANALYZE")
    report.detail["analyze"] = 1
    if vacuum:
        if ctrl is not None:
            ctrl.checkpoint()
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        # Hard wall-clock cap: a timer thread interrupts the connection, so a
        # wedged VACUUM raises OperationalError('interrupted') instead of
        # livelocking for hours (Connection.interrupt is documented
        # thread-safe). SQLite rolls the aborted VACUUM back; the main DB is
        # untouched.
        timer = threading.Timer(vacuum_timeout_min * 60.0, conn.interrupt)
        timer.daemon = True
        timer.start()
        t0 = time.monotonic()
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            elapsed = round(time.monotonic() - t0, 1)
            if "interrupt" in str(exc).lower():
                _log(
                    "gc_vacuum_timed_out",
                    budget_min=vacuum_timeout_min,
                    elapsed_s=elapsed,
                    freelist_pages=before,
                )
                raise GcAbortedError(
                    f"VACUUM exceeded its {vacuum_timeout_min:g}-minute budget and was "
                    "interrupted (rolled back; DB intact). Raise --vacuum-timeout-min "
                    "or reclaim in a quieter window."
                ) from exc
            raise
        finally:
            timer.cancel()
        report.detail["freelist_pages_before_vacuum"] = before
        report.detail["vacuum"] = 1
        _log(
            "gc_vacuum",
            freelist_pages_before=before,
            elapsed_s=round(time.monotonic() - t0, 1),
        )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_gc(
    db_path: Path,
    *,
    apply: bool,
    policies: list[str],
    retention_days: int,
    keep_quarters: int,
    keep_fy: int,
    include_portfolio: bool,
    vacuum: bool,
    tickers: list[str] | None = None,
    archive_path: Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_runtime_min: float = DEFAULT_MAX_RUNTIME_MIN,
    vacuum_timeout_min: float = DEFAULT_VACUUM_TIMEOUT_MIN,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    enforce_protected_window: bool = False,
) -> GcRunReport:
    """Run the selected policies.

    ``enforce_protected_window`` defaults to False for library/test callers
    (a CI run at 10:00 UTC must not flake on Pacific wall-clock); the CLI
    passes True unless --ignore-protected-window is set.
    """
    if keep_quarters < MIN_KEEP_QUARTERS:
        raise ValueError(
            f"--keep-quarters {keep_quarters} < floor {MIN_KEEP_QUARTERS} "
            "(report §3 loads 16 quarters: 12 display + 4 CAGR baseline)"
        )
    if keep_fy < MIN_KEEP_FY:
        raise ValueError(
            f"--keep-fy {keep_fy} < floor {MIN_KEEP_FY} "
            "(10 rendered years + 52/53-week-filer margin)"
        )
    if batch_size < 1:
        raise ValueError(f"--batch-size {batch_size} < 1")

    ctrl = _RunControl(
        max_runtime_s=max_runtime_min * 60.0,
        enforce_protected_window=enforce_protected_window and apply,
    )
    ctrl.checkpoint()  # refuse to start inside the protected window

    run_at = now_naive_utc().isoformat()
    effective_db_path = _resolve_database_path(db_path, apply=apply)
    archive = archive_path or (effective_db_path.parent / "archive" / ARCHIVE_NAME)

    # AGENTS.md concurrency rule: one process owns the portfolio write set.
    # The morning pipeline orchestrator holds the same lock for its whole run,
    # so an apply can never interleave with the 04:00 write legs. Short
    # timeout on purpose — GC yields, it does not queue.
    lock: RunLock | None = None
    if apply:
        lock = acquire_run_lock(effective_db_path, owner="db_gc", timeout_s=lock_timeout_s)

    try:
        # Dry runs make no logical database writes, so they skip the Alembic
        # write preflight and never create the archive sidecar; --apply
        # enforces both. A WAL reader may still open SQLite-managed sidecars.
        role = SQLiteConnectionRole.WRITER if apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(
            effective_db_path,
            role=role,
            schema_preflight=False if apply else None,
        )
        # Explicit transaction control: the batching contract needs autocommit
        # between the bounded _txn() blocks, not the sqlite3 module's implicit
        # transaction management.
        conn.isolation_level = None
        report = GcRunReport(
            run_at=run_at,
            db_path=str(effective_db_path),
            archive_path=str(archive),
            apply=apply,
        )
        try:
            if apply:
                # The compatibility check must run while SQLite owns the
                # writer reservation, not merely before lock acquisition.
                with _txn(conn):
                    if "facts-depth" in policies:
                        report.facts_depth_apply_preflight = _facts_depth_apply_preflight(conn)
                attach_archive(conn, archive)
                # FK enforcement stays ON (sqlite_runtime default). The 0270
                # supersedes_id index makes the self-FK child check O(log n),
                # and _facts_depth_apply_preflight verifies via EXPLAIN QUERY
                # PLAN that the index is actually used — the historical
                # foreign_keys=OFF workaround for the pre-0270 quadratic scan
                # is obsolete and now rejected by the preflight.
            _reset_doomed(conn)

            if "validation-issues" in policies:
                report.policies.append(
                    collapse_validation_issues(
                        conn, apply=apply, run_at=run_at, ctrl=ctrl, batch_size=batch_size
                    )
                )
            if "telemetry" in policies:
                report.policies.append(
                    telemetry_retention(
                        conn,
                        apply=apply,
                        run_at=run_at,
                        retention_days=retention_days,
                        ctrl=ctrl,
                        batch_size=batch_size,
                    )
                )
            if "facts-depth" in policies:
                report.policies.append(
                    facts_depth(
                        conn,
                        apply=apply,
                        run_at=run_at,
                        keep_quarters=keep_quarters,
                        keep_fy=keep_fy,
                        include_portfolio=include_portfolio,
                        ctrl=ctrl,
                        batch_size=batch_size,
                        tickers=tickers,
                    )
                )
            if "maintenance" in policies:
                report.policies.append(
                    maintenance(
                        conn,
                        apply=apply,
                        vacuum=vacuum,
                        ctrl=ctrl,
                        vacuum_timeout_min=vacuum_timeout_min,
                    )
                )
        finally:
            conn.close()
    finally:
        if lock is not None:
            lock.release()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Periodic garbage collection for data/portfolio.db "
        "(archive-then-delete; dry-run by default)"
    )
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--policies",
        default=",".join(POLICY_NAMES),
        help=f"comma-separated subset of {POLICY_NAMES}",
    )
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--keep-quarters", type=int, default=20)
    parser.add_argument("--keep-fy", type=int, default=12)
    parser.add_argument(
        "--include-portfolio",
        action="store_true",
        help="apply the facts-depth window to portfolio tickers too",
    )
    parser.add_argument(
        "--tickers", default=None, help="comma-separated ticker allowlist for facts-depth"
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="run VACUUM after deletions (exclusivity preflight + hard timeout)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"rows deleted/updated per committed transaction (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-runtime-min",
        type=float,
        default=DEFAULT_MAX_RUNTIME_MIN,
        help=f"whole-run wall-clock budget in minutes; loud abort past it "
        f"(default {DEFAULT_MAX_RUNTIME_MIN:g})",
    )
    parser.add_argument(
        "--vacuum-timeout-min",
        type=float,
        default=DEFAULT_VACUUM_TIMEOUT_MIN,
        help=f"hard VACUUM cap in minutes, enforced via connection interrupt "
        f"(default {DEFAULT_VACUUM_TIMEOUT_MIN:g})",
    )
    parser.add_argument(
        "--lock-timeout-s",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_S,
        help=f"how long --apply waits for the portfolio run lock "
        f"(default {DEFAULT_LOCK_TIMEOUT_S:g}s)",
    )
    parser.add_argument(
        "--ignore-protected-window",
        action="store_true",
        help="run even inside the 03:00-05:00 America/Los_Angeles morning-pipeline window",
    )
    args = parser.parse_args(argv)

    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    unknown = [p for p in policies if p not in POLICY_NAMES]
    if unknown:
        parser.error(f"unknown policies: {unknown}; expected subset of {POLICY_NAMES}")

    try:
        report = run_gc(
            args.db_path,
            apply=args.apply,
            policies=policies,
            retention_days=args.retention_days,
            keep_quarters=args.keep_quarters,
            keep_fy=args.keep_fy,
            include_portfolio=args.include_portfolio,
            vacuum=args.vacuum,
            tickers=[t.strip() for t in args.tickers.split(",")] if args.tickers else None,
            batch_size=args.batch_size,
            max_runtime_min=args.max_runtime_min,
            vacuum_timeout_min=args.vacuum_timeout_min,
            lock_timeout_s=args.lock_timeout_s,
            enforce_protected_window=not args.ignore_protected_window,
        )
    except (GcAbortedError, RunLockHeldError) as exc:
        _log("gc_aborted", error=str(exc), kind=type(exc).__name__)
        print(f"db_gc aborted: {exc}", file=sys.stderr)
        return 2
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
