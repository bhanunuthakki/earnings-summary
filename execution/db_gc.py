"""Periodic garbage collection for data/portfolio.db.

Grounded in the 2026-07-30 consumer audit (3 parallel sweeps over every reader
of financial_facts, kpi_facts, and the telemetry tables). Four policies, each
independently selectable, all archive-then-delete — deleted rows are copied
into a sidecar SQLite archive (data/archive/portfolio_gc_archive.db) in their
original schema before removal, so any prune is reversible with one
INSERT ... SELECT.

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
   dangling supersedes_id pointers on surviving facts, and the 0225
   resolution-plane tables (fact_observation_revisions,
   legacy_fact_evidence_match_revisions, fact_selection_decisions) by
   (fact_table/target_table, row id).

4. ``maintenance`` — ANALYZE after any applied deletion; VACUUM only with
   --vacuum (the freelist alone held ~281 MB at audit time).

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
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clock import now_naive_utc  # noqa: E402
from pipeline.validation_issue_store import issue_fingerprint  # noqa: E402
from schema_compat import require_current_for_write  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

import sqlite3  # noqa: E402

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

# (table, timestamp column) pairs cleared for age-based retention by the
# consumer audit. pipeline_attempts is excluded — see module docstring.
TELEMETRY_TABLES: tuple[tuple[str, str], ...] = (
    ("stage_transitions", "started_at"),
    ("source_calls", "called_at"),
    ("ingestion_runs", "started_at"),
)


class PolicyReport(BaseModel):
    policy: str
    applied: bool
    rows_deleted: dict[str, int] = Field(default_factory=dict[str, int])
    rows_updated: dict[str, int] = Field(default_factory=dict[str, int])
    detail: dict[str, int] = Field(default_factory=dict[str, int])


class GcRunReport(BaseModel):
    run_at: str
    db_path: str
    archive_path: str
    apply: bool
    policies: list[PolicyReport] = Field(default_factory=list["PolicyReport"])


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), file=sys.stderr)


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
    """
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS gcarc."{table}" AS SELECT * FROM main."{table}" WHERE 0'
    )
    cur = conn.execute(
        f'INSERT INTO gcarc."{table}" SELECT t.* FROM main."{table}" t '
        f'JOIN "{doomed}" d ON d.id = t."{id_col}"'
    )
    n = cur.rowcount
    conn.execute(
        "INSERT INTO gcarc.gc_manifest (run_at, policy, source_table, rows_archived) "
        "VALUES (?, ?, ?, ?)",
        (run_at, policy, table, n),
    )
    return n


def _reset_doomed(conn: sqlite3.Connection, name: str = "_gc_doomed") -> None:
    conn.execute(f'CREATE TEMP TABLE IF NOT EXISTS "{name}" (id INTEGER PRIMARY KEY)')
    conn.execute(f'DELETE FROM "{name}"')


# ---------------------------------------------------------------------------
# Policy 1: validation_issues collapse
# ---------------------------------------------------------------------------


def collapse_validation_issues(
    conn: sqlite3.Connection, *, apply: bool, run_at: str
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
        # Aggregate lifecycle fields + a real fingerprint onto each survivor.
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
        conn.executemany(
            "UPDATE validation_issues SET fingerprint = ?, first_seen_at = ?, "
            "last_seen_at = ?, occurrence_count = ? WHERE id = ?",
            updates,
        )
        _archive_doomed(
            conn, table="validation_issues", run_at=run_at, policy="validation-issues"
        )
        conn.execute(
            "DELETE FROM validation_issues WHERE id IN (SELECT id FROM _gc_doomed)"
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
    conn: sqlite3.Connection, *, apply: bool, run_at: str, retention_days: int
) -> PolicyReport:
    """Archive + delete telemetry rows older than the retention cutoff."""
    from datetime import timedelta

    report = PolicyReport(policy="telemetry", applied=apply)
    cutoff = (now_naive_utc() - timedelta(days=retention_days)).isoformat()
    report.detail["retention_days"] = retention_days

    for table, ts_col in TELEMETRY_TABLES:
        # ingestion_runs keys on run_id, not id; resolve the rowid column.
        id_col = "id"
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if "id" not in cols:
            id_col = "rowid"
        _reset_doomed(conn)
        conn.execute(
            f'INSERT INTO _gc_doomed (id) SELECT "{id_col}" FROM "{table}" '
            f'WHERE "{ts_col}" < ?',
            (cutoff,),
        )
        n = conn.execute("SELECT COUNT(*) FROM _gc_doomed").fetchone()[0]
        if apply and n:
            _archive_doomed(conn, table=table, run_at=run_at, policy="telemetry", id_col=id_col)
            conn.execute(
                f'DELETE FROM "{table}" WHERE "{id_col}" IN (SELECT id FROM _gc_doomed)'
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
            f"SELECT DISTINCT period_end FROM financial_facts "
            f"WHERE ticker = ? AND fiscal_period_type IN ({qmarks}) "
            f"ORDER BY period_end DESC LIMIT ?",
            (ticker, *QUARTER_TYPES, keep_quarters),
        )
    ]
    amarks = ", ".join("?" for _ in ANNUAL_TYPES)
    keep_years = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT substr(period_end, 1, 4) FROM financial_facts "
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
        """,
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
    tickers: list[str] | None = None,
) -> PolicyReport:
    """Window-prune financial_facts + cascades per ticker tier."""
    report = PolicyReport(policy="facts-depth", applied=apply)
    tier = _tier_of(conn)

    fact_tickers = [
        str(r[0]) for r in conn.execute("SELECT DISTINCT ticker FROM financial_facts")
    ]
    if tickers:
        wanted = {t.upper() for t in tickers}
        fact_tickers = [t for t in fact_tickers if t.upper() in wanted]

    facts_doomed = 0
    attempts_doomed = 0
    per_tier: dict[str, int] = {}
    _reset_doomed(conn)
    for t in sorted(fact_tickers):
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

    # --- apply path: archive + delete + cascades ---------------------------
    # Supersede chains live inside one logical key, so parent and child are
    # usually doomed together; defer FK checks to commit so the self-
    # referential fk_financial_facts_supersedes doesn't reject mid-statement.
    conn.execute("PRAGMA defer_foreign_keys = ON")
    _archive_doomed(conn, table="financial_facts", run_at=run_at, policy="facts-depth")
    if attempts_doomed:
        _archive_doomed(
            conn,
            table="metric_computation_attempts",
            run_at=run_at,
            policy="facts-depth",
            doomed="_gc_doomed_mca",
        )
        conn.execute(
            "DELETE FROM metric_computation_attempts "
            "WHERE id IN (SELECT id FROM _gc_doomed_mca)"
        )

    # Cascade 2: 0225 resolution-plane rows for the doomed facts (empty on
    # prod today; defensive so the integrity audit never sees dangling refs).
    for tbl, table_col, id_col in (
        ("fact_observation_revisions", "fact_table", "fact_row_id"),
        ("legacy_fact_evidence_match_revisions", "fact_table", "fact_row_id"),
        ("fact_selection_decisions", "target_table", "target_row_id"),
    ):
        try:
            cur = conn.execute(
                f'DELETE FROM "{tbl}" WHERE "{table_col}" = ? '
                f'AND "{id_col}" IN (SELECT id FROM _gc_doomed)',
                ("financial_facts",),
            )
            if cur.rowcount:
                report.rows_deleted[tbl] = cur.rowcount
        except sqlite3.OperationalError:
            pass  # table absent on older schemas — nothing to cascade

    # Cascade 3: null dangling supersedes_id on survivors. Chains share one
    # logical (ticker, period_end, fpt, line_item) key so a period prune
    # removes whole chains; this catches any cross-period stragglers.
    cur = conn.execute(
        "UPDATE financial_facts SET supersedes_id = NULL "
        "WHERE supersedes_id IN (SELECT id FROM _gc_doomed)"
    )
    if cur.rowcount:
        report.rows_updated["financial_facts_supersedes_nulled"] = cur.rowcount

    conn.execute("DELETE FROM financial_facts WHERE id IN (SELECT id FROM _gc_doomed)")
    return report


# ---------------------------------------------------------------------------
# Policy 4: maintenance
# ---------------------------------------------------------------------------


def maintenance(conn: sqlite3.Connection, *, apply: bool, vacuum: bool) -> PolicyReport:
    report = PolicyReport(policy="maintenance", applied=apply)
    if not apply:
        report.detail["vacuum_requested"] = int(vacuum)
        return report
    conn.commit()
    conn.execute("ANALYZE")
    report.detail["analyze"] = 1
    if vacuum:
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        conn.execute("VACUUM")
        report.detail["freelist_pages_before_vacuum"] = before
        report.detail["vacuum"] = 1
        _log("gc_vacuum", freelist_pages_before=before)
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
) -> GcRunReport:
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

    run_at = now_naive_utc().isoformat()
    archive = archive_path or (db_path.parent / "archive" / ARCHIVE_NAME)
    # Dry runs mutate nothing, so they skip the alembic-revision preflight and
    # never create the archive sidecar; --apply enforces both.
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=apply)
    report = GcRunReport(
        run_at=run_at, db_path=str(db_path), archive_path=str(archive), apply=apply
    )
    try:
        if apply:
            require_current_for_write(conn)
            attach_archive(conn, archive)
        _reset_doomed(conn)

        if "validation-issues" in policies:
            report.policies.append(
                collapse_validation_issues(conn, apply=apply, run_at=run_at)
            )
        if "telemetry" in policies:
            report.policies.append(
                telemetry_retention(
                    conn, apply=apply, run_at=run_at, retention_days=retention_days
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
                    tickers=tickers,
                )
            )
        if apply:
            conn.commit()
        if "maintenance" in policies:
            report.policies.append(maintenance(conn, apply=apply, vacuum=vacuum))
        if apply:
            conn.commit()
    finally:
        conn.close()
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
        "--vacuum", action="store_true", help="run VACUUM after deletions (slow, big win)"
    )
    args = parser.parse_args(argv)

    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    unknown = [p for p in policies if p not in POLICY_NAMES]
    if unknown:
        parser.error(f"unknown policies: {unknown}; expected subset of {POLICY_NAMES}")

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
    )
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
