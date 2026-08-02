# pyright: reportPrivateUsage=false
"""Tests for execution/db_gc.py — the periodic DB garbage collector.

Fixture DBs deliberately omit alembic_version so schema_compat's write
preflight no-ops (its documented fixture contract)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "execution"))

import db_gc  # noqa: E402

import run_lock  # noqa: E402


@pytest.fixture()
def gc_db(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            ticker TEXT, list_type TEXT, archived_at TIMESTAMP
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
            fiscal_period_type TEXT, line_item TEXT, value NUMERIC,
            extracted_by TEXT, supersedes_id INTEGER REFERENCES financial_facts(id)
        );
        CREATE INDEX ix_0270_financial_facts_supersedes_id
            ON financial_facts(supersedes_id);
        CREATE TABLE metric_computation_attempts (
            id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
            fiscal_period_type TEXT, formula_id INTEGER
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY, run_id TEXT, source_doc_id INTEGER,
            ticker TEXT, severity TEXT, rule TEXT, raw_value TEXT,
            expected TEXT, raised_at TEXT, resolved_at TEXT,
            resolved_by TEXT, resolution_note TEXT, fingerprint TEXT,
            first_seen_at TEXT, last_seen_at TEXT, occurrence_count INTEGER
        );
        CREATE UNIQUE INDEX uq_vi_fp ON validation_issues(fingerprint)
            WHERE fingerprint IS NOT NULL;
        CREATE TABLE fact_selection_decisions (
            decision_id INTEGER PRIMARY KEY, target_table TEXT,
            target_row_id INTEGER, validation_issue_id INTEGER
        );
        CREATE TABLE fact_observation_revisions (
            fact_table TEXT, fact_row_id INTEGER
        );
        CREATE TABLE legacy_fact_evidence_match_revisions (
            match_revision_id INTEGER PRIMARY KEY, fact_table TEXT, fact_row_id INTEGER
        );
        CREATE TABLE stage_transitions (id INTEGER PRIMARY KEY, started_at TEXT);
        CREATE TABLE source_calls (id INTEGER PRIMARY KEY, called_at TEXT);
        CREATE TABLE ingestion_runs (run_id TEXT, started_at TEXT);
        """
    )
    conn.commit()
    conn.close()
    return db


def _run(
    db: Path,
    *,
    apply: bool = False,
    policies: list[str] | None = None,
    retention_days: int = 90,
    keep_quarters: int = 16,
    keep_fy: int = 12,
    include_portfolio: bool = False,
    vacuum: bool = False,
    batch_size: int = db_gc.DEFAULT_BATCH_SIZE,
    max_runtime_min: float = db_gc.DEFAULT_MAX_RUNTIME_MIN,
    lock_timeout_s: float = 0.0,
    enforce_protected_window: bool = False,
) -> db_gc.GcRunReport:
    return db_gc.run_gc(
        db,
        apply=apply,
        policies=policies if policies is not None else list(db_gc.POLICY_NAMES),
        retention_days=retention_days,
        keep_quarters=keep_quarters,
        keep_fy=keep_fy,
        include_portfolio=include_portfolio,
        vacuum=vacuum,
        batch_size=batch_size,
        max_runtime_min=max_runtime_min,
        lock_timeout_s=lock_timeout_s,
        enforce_protected_window=enforce_protected_window,
    )


def _seed_quarters(conn: sqlite3.Connection, ticker: str, n: int) -> None:
    """n quarterly periods (newest 2026-03-31 going back), 2 line items each."""
    from datetime import date

    y, m = 2026, 3
    for _ in range(n):
        pe = f"{y:04d}-{m:02d}-{date(y, m, 28).day + (3 if m != 2 else 0):02d}"
        pe = f"{y:04d}-{m:02d}-31" if m in (3, 12) else f"{y:04d}-{m:02d}-30"
        q = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}[m]
        for item in ("revenue", "net_income"):
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
                " line_item, value, extracted_by) VALUES (?, ?, ?, ?, 1, 'fmp')",
                (ticker, pe, q, item),
            )
        m -= 3
        if m == 0:
            y, m = y - 1, 12


def _seed_fy(conn: sqlite3.Connection, ticker: str, n: int) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES (?, ?, 'FY', 'revenue', 1, 'fmp')",
            (
                ticker,
                f"{2025 - i}-12-31",
            ),
        )


class TestValidationIssuesCollapse:
    def _seed(self, db: Path) -> None:
        conn = sqlite3.connect(db)
        # Defect A: 3 duplicate detections across 3 runs.
        for i, day in enumerate(("2026-07-24", "2026-07-25", "2026-07-26")):
            conn.execute(
                "INSERT INTO validation_issues (run_id, source_doc_id, ticker,"
                " severity, rule, raw_value, expected, raised_at, occurrence_count)"
                " VALUES (?, 7, 'NU', 'warn', 'PLAUSIBLE_RANGE', 'x=1', 'x<1', ?, 1)",
                (f"run{i}", f"{day} 04:00:00"),
            )
        # Defect B: singleton — untouched.
        conn.execute(
            "INSERT INTO validation_issues (run_id, source_doc_id, ticker, severity,"
            " rule, raw_value, expected, raised_at, occurrence_count)"
            " VALUES ('run0', 8, 'WIX', 'warn', 'MAGNITUDE_JUMP', 'y', 'z',"
            " '2026-07-20 04:00:00', 1)"
        )
        conn.commit()
        conn.close()

    def test_collapse_keeps_newest_and_aggregates(self, gc_db: Path) -> None:
        self._seed(gc_db)
        report = _run(gc_db, apply=True, policies=["validation-issues"])
        (pol,) = report.policies
        assert pol.rows_deleted["validation_issues"] == 2
        conn = sqlite3.connect(gc_db)
        rows = conn.execute(
            "SELECT raised_at, first_seen_at, last_seen_at, occurrence_count,"
            " fingerprint FROM validation_issues WHERE ticker = 'NU'"
        ).fetchall()
        assert len(rows) == 1
        raised, first, last, count, fp = rows[0]
        assert raised.startswith("2026-07-26")
        assert first.startswith("2026-07-24") and last.startswith("2026-07-26")
        assert count == 3 and fp is not None
        # Singleton kept as one row — but fingerprinted, not stranded.
        assert (
            conn.execute("SELECT COUNT(*) FROM validation_issues WHERE ticker = 'WIX'").fetchone()[
                0
            ]
            == 1
        )
        # Duplicates archived, restorable.
        arc = gc_db.parent / "archive" / db_gc.ARCHIVE_NAME
        aconn = sqlite3.connect(arc)
        assert aconn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 2

    def test_fk_referenced_duplicate_is_protected(self, gc_db: Path) -> None:
        self._seed(gc_db)
        conn = sqlite3.connect(gc_db)
        oldest_id = conn.execute(
            "SELECT id FROM validation_issues WHERE ticker='NU' ORDER BY raised_at LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO fact_selection_decisions (target_table, target_row_id,"
            " validation_issue_id) VALUES ('kpi_facts', 1, ?)",
            (oldest_id,),
        )
        conn.commit()
        conn.close()
        _run(gc_db, apply=True, policies=["validation-issues"])
        conn = sqlite3.connect(gc_db)
        surviving = {
            r[0] for r in conn.execute("SELECT id FROM validation_issues WHERE ticker='NU'")
        }
        assert oldest_id in surviving  # protected row kept
        assert len(surviving) == 2  # survivor + protected; middle dup deleted

    def test_dry_run_writes_nothing(self, gc_db: Path) -> None:
        self._seed(gc_db)
        report = _run(gc_db, apply=False, policies=["validation-issues"])
        (pol,) = report.policies
        assert pol.rows_deleted["validation_issues"] == 2
        # Both survivors are planned for a fingerprint (dup group + singleton).
        assert pol.detail["fingerprint_backfilled"] == 2
        conn = sqlite3.connect(gc_db)
        assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 4
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM validation_issues WHERE fingerprint IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


class TestNullFingerprintBackfill:
    """Regression: every NULL fingerprint is backfilled, not just dup survivors.

    Before this, ``collapse_validation_issues`` backfilled only rows matching
    ``rn = 1 AND n > 1``. A row that was always a singleton kept its NULL
    fingerprint, and ``record_issue`` matches on ``WHERE fingerprint = ?``,
    which never matches NULL — so the row could never be re-opened, updated,
    or resolved and sat in the open-issue count forever (196 such rows
    survived the 2026-08-02 production apply).
    """

    def _seed_singleton(self, db: Path, *, ticker: str = "NU") -> None:
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO validation_issues (run_id, source_doc_id, ticker, severity,"
            " rule, raw_value, expected, raised_at, fingerprint, occurrence_count)"
            " VALUES ('run0', 7, ?, 'warn', 'PLAUSIBLE_RANGE', 'x=1', 'x<1',"
            " '2026-07-24 04:00:00', NULL, 1)",
            (ticker,),
        )
        conn.commit()
        conn.close()

    def _record_with_rule(
        self, db: Path, *, ticker: str = "NU", rule: str = "PLAUSIBLE_RANGE"
    ) -> int:
        """Re-detect the seeded defect through the real lifecycle writer."""
        from pipeline.validation_issue_store import record_issue

        conn = sqlite3.connect(db)
        try:
            issue_id = record_issue(
                conn,
                run_id="run-after-gc",
                source_doc_id=7,
                ticker=ticker,
                severity="warn",
                rule=rule,
                raw_value="x=1",
                expected="x<1",
            )
            conn.commit()
        finally:
            conn.close()
        return issue_id

    def test_singleton_gets_fingerprint_and_record_issue_updates_it(self, gc_db: Path) -> None:
        self._seed_singleton(gc_db)
        report = _run(gc_db, apply=True, policies=["validation-issues"])
        (pol,) = report.policies
        # No duplicates at all — the old `if apply and doomed_total` gate also
        # skipped the backfill entirely on exactly this shape.
        assert pol.rows_deleted["validation_issues"] == 0
        assert pol.detail["duplicate_groups"] == 0
        assert pol.detail["fingerprint_backfilled"] == 1

        conn = sqlite3.connect(gc_db)
        seeded_id, fingerprint, first, last, count = conn.execute(
            "SELECT id, fingerprint, first_seen_at, last_seen_at, occurrence_count"
            " FROM validation_issues"
        ).fetchone()
        conn.close()
        assert fingerprint is not None
        assert (
            fingerprint == hashlib.sha256(b"7\x1fNU\x1fPLAUSIBLE_RANGE\x1fx=1\x1fx<1").hexdigest()
        )
        assert first.startswith("2026-07-24") and last.startswith("2026-07-24")
        assert count == 1

        # The point of the fingerprint: the next detection UPDATEs this row
        # rather than inserting a second one.
        assert self._record_with_rule(gc_db) == seeded_id
        conn = sqlite3.connect(gc_db)
        rows = conn.execute("SELECT id, run_id, occurrence_count FROM validation_issues").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0] == (seeded_id, "run-after-gc", 2)

    def test_second_apply_is_a_noop(self, gc_db: Path) -> None:
        self._seed_singleton(gc_db)
        _run(gc_db, apply=True, policies=["validation-issues"])
        report = _run(gc_db, apply=True, policies=["validation-issues"])
        (pol,) = report.policies
        assert pol.rows_deleted["validation_issues"] == 0
        assert pol.rows_updated["validation_issues"] == 0
        assert pol.detail["fingerprint_backfilled"] == 0

    def test_ticker_case_variants_collapse_as_one_defect(self, gc_db: Path) -> None:
        """The defect-group key mirrors issue_fingerprint's normalization.

        ``issue_fingerprint`` upper-cases the ticker, so 'nu' and 'NU' are one
        defect. The group key used to partition on the raw ticker, which put
        them in two groups that computed one fingerprint — the second UPDATE
        would hit uq_validation_issues_fingerprint. Aligned, they are simply
        one duplicate group.
        """
        self._seed_singleton(gc_db, ticker="NU")
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO validation_issues (run_id, source_doc_id, ticker, severity,"
            " rule, raw_value, expected, raised_at, fingerprint, occurrence_count)"
            " VALUES ('run-legacy', 7, 'nu', 'warn', 'PLAUSIBLE_RANGE', 'x=1', 'x<1',"
            " '2026-07-20 04:00:00', NULL, 1)"
        )
        conn.commit()
        conn.close()

        report = _run(gc_db, apply=True, policies=["validation-issues"])
        (pol,) = report.policies
        assert pol.detail["duplicate_groups"] == 1
        assert pol.detail["fingerprint_collisions"] == 0
        assert pol.rows_deleted["validation_issues"] == 1

        conn = sqlite3.connect(gc_db)
        rows = conn.execute("SELECT ticker, fingerprint FROM validation_issues").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "NU" and rows[0][1] is not None

    def test_collision_with_fingerprinted_row_is_archived_not_written(self, gc_db: Path) -> None:
        """A NULL row whose fingerprint is already owned is a true duplicate.

        The one normalization the group key cannot mirror in SQL: a NULL
        ``rule`` reaches ``issue_fingerprint`` as the literal string "None", so
        a NULL rule and the rule 'None' are two groups with one fingerprint.
        The loser goes through archive+delete, never an UPDATE the partial
        unique index would reject mid-batch.
        """
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO validation_issues (run_id, source_doc_id, ticker, severity,"
            " rule, raw_value, expected, raised_at, fingerprint, occurrence_count)"
            " VALUES ('run-legacy', 7, 'NU', 'warn', NULL, 'x=1', 'x<1',"
            " '2026-07-20 04:00:00', NULL, 1)"
        )
        conn.commit()
        conn.close()
        kept_id = self._record_with_rule(gc_db, rule="None")

        report = _run(gc_db, apply=True, policies=["validation-issues"])
        (pol,) = report.policies
        assert pol.detail["fingerprint_collisions"] == 1
        assert pol.detail["fingerprint_backfilled"] == 0
        assert pol.rows_deleted["validation_issues"] == 1

        conn = sqlite3.connect(gc_db)
        rows = conn.execute("SELECT id FROM validation_issues").fetchall()
        conn.close()
        assert rows == [(kept_id,)]
        arc = gc_db.parent / "archive" / db_gc.ARCHIVE_NAME
        aconn = sqlite3.connect(arc)
        archived = aconn.execute("SELECT run_id FROM validation_issues").fetchall()
        aconn.close()
        assert archived == [("run-legacy",)]

    def test_referenced_collision_is_stranded_loudly_not_deleted(self, gc_db: Path) -> None:
        """An FK-referenced collision can be neither fingerprinted nor deleted."""
        conn = sqlite3.connect(gc_db)
        cur = conn.execute(
            "INSERT INTO validation_issues (run_id, source_doc_id, ticker, severity,"
            " rule, raw_value, expected, raised_at, fingerprint, occurrence_count)"
            " VALUES ('run-legacy', 7, 'NU', 'warn', NULL, 'x=1', 'x<1',"
            " '2026-07-20 04:00:00', NULL, 1)"
        )
        conn.execute(
            "INSERT INTO fact_selection_decisions (target_table, target_row_id,"
            " validation_issue_id) VALUES ('kpi_facts', 1, ?)",
            (cur.lastrowid,),
        )
        conn.commit()
        conn.close()
        self._record_with_rule(gc_db, rule="None")

        report = _run(gc_db, apply=True, policies=["validation-issues"])
        (pol,) = report.policies
        assert pol.detail["fingerprint_blocked_referenced"] == 1
        assert pol.detail["fingerprint_collisions"] == 0
        assert pol.rows_deleted["validation_issues"] == 0

        conn = sqlite3.connect(gc_db)
        assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 2
        # Stranded, but reported — never silently "fixed".
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM validation_issues WHERE fingerprint IS NULL"
            ).fetchone()[0]
            == 1
        )
        conn.close()


class TestTelemetryRetention:
    def test_cutoff_split(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        conn.execute("INSERT INTO stage_transitions (started_at) VALUES ('2026-01-01')")
        conn.execute("INSERT INTO stage_transitions (started_at) VALUES ('2026-07-29')")
        conn.execute("INSERT INTO source_calls (called_at) VALUES ('2025-12-01')")
        conn.execute("INSERT INTO ingestion_runs (run_id, started_at) VALUES ('r1', '2026-01-01')")
        conn.execute("INSERT INTO ingestion_runs (run_id, started_at) VALUES ('r2', '2026-07-29')")
        conn.commit()
        conn.close()
        report = _run(gc_db, apply=True, policies=["telemetry"], retention_days=90)
        (pol,) = report.policies
        assert pol.rows_deleted["stage_transitions"] == 1
        assert pol.rows_deleted["source_calls"] == 1
        assert pol.rows_deleted["ingestion_runs"] == 1
        conn = sqlite3.connect(gc_db)
        assert conn.execute("SELECT COUNT(*) FROM stage_transitions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1
        arc = sqlite3.connect(gc_db.parent / "archive" / db_gc.ARCHIVE_NAME)
        assert arc.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 1


class TestFactsDepth:
    def _seed(self, db: Path) -> None:
        conn = sqlite3.connect(db)
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, ?)",
            [("EVAL", "evaluation"), ("PORT", "portfolio"), ("PEER", "index_member")],
        )
        for t in ("EVAL", "PORT"):
            _seed_quarters(conn, t, 30)
            _seed_fy(conn, t, 15)
        _seed_quarters(conn, "PEER", 20)
        _seed_quarters(conn, "GONE", 8)  # facts but no tracked_companies row
        # TTM + s1 rows on EVAL must always survive.
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by)"
            " VALUES ('EVAL', '2010-03-31', 'TTM', 'revenue', 1, 'fmp')"
        )
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by)"
            " VALUES ('EVAL', '2011-03-31', 'Q1', 'revenue', 1, 's1')"
        )
        # Attempts grid rows for EVAL's oldest pruned quarter and newest quarter.
        conn.execute(
            "INSERT INTO metric_computation_attempts (ticker, period_end,"
            " fiscal_period_type, formula_id) VALUES ('EVAL', '2018-12-31', 'Q4', 1)"
        )
        conn.execute(
            "INSERT INTO metric_computation_attempts (ticker, period_end,"
            " fiscal_period_type, formula_id) VALUES ('EVAL', '2026-03-31', 'Q1', 1)"
        )
        conn.commit()
        conn.close()

    def test_windows_tiers_and_cascades(self, gc_db: Path) -> None:
        self._seed(gc_db)
        report = _run(gc_db, apply=True, policies=["facts-depth"])
        (pol,) = report.policies
        conn = sqlite3.connect(gc_db)

        def q(sql: str, *args: object) -> int:
            return conn.execute(sql, args).fetchone()[0]

        # EVAL windowed: 16 quarterly periods x 2 items, 12 FY, TTM + s1 kept.
        assert (
            q(
                "SELECT COUNT(DISTINCT period_end) FROM financial_facts"
                " WHERE ticker='EVAL' AND fiscal_period_type LIKE 'Q%'"
                " AND extracted_by != 's1'"
            )
            == 16
        )
        assert (
            q(
                "SELECT COUNT(*) FROM financial_facts WHERE ticker='EVAL'"
                " AND fiscal_period_type='FY'"
            )
            == 12
        )
        assert (
            q(
                "SELECT COUNT(*) FROM financial_facts WHERE ticker='EVAL'"
                " AND fiscal_period_type='TTM'"
            )
            == 1
        )
        assert q("SELECT COUNT(*) FROM financial_facts WHERE extracted_by='s1'") == 1
        # Portfolio untouched without the flag.
        assert (
            q(
                "SELECT COUNT(DISTINCT period_end) FROM financial_facts"
                " WHERE ticker='PORT' AND fiscal_period_type LIKE 'Q%'"
            )
            == 30
        )
        # index_member windowed; orphan removed entirely.
        assert (
            q(
                "SELECT COUNT(DISTINCT period_end) FROM financial_facts"
                " WHERE ticker='PEER' AND fiscal_period_type LIKE 'Q%'"
            )
            == 16
        )
        assert q("SELECT COUNT(*) FROM financial_facts WHERE ticker='GONE'") == 0
        # Attempts cascade: pruned-period attempt gone, live-period attempt kept.
        assert (
            q("SELECT COUNT(*) FROM metric_computation_attempts WHERE period_end='2018-12-31'") == 0
        )
        assert (
            q("SELECT COUNT(*) FROM metric_computation_attempts WHERE period_end='2026-03-31'") == 1
        )
        assert pol.rows_deleted["metric_computation_attempts"] == 1
        # Everything deleted is archived.
        arc = sqlite3.connect(gc_db.parent / "archive" / db_gc.ARCHIVE_NAME)
        archived = arc.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]
        assert archived == pol.rows_deleted["financial_facts"] > 0

    def test_supersedes_chain_pruned_whole_and_dangling_nulled(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 20)
        # Old-period chain: both rows fall outside the window together.
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES"
            " (9001, 'EVAL', '2018-12-31', 'Q4', 'revenue', 1, 'fmp')"
        )
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by, supersedes_id) VALUES"
            " (9002, 'EVAL', '2018-12-31', 'Q4', 'revenue', 2, 'sec_xbrl', 9001)"
        )
        # Survivor (newest quarter) pointing at a doomed old row — must be nulled.
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by, supersedes_id) VALUES"
            " (9003, 'EVAL', '2026-03-31', 'Q1', 'special_item', 3, 'fmp', 9001)"
        )
        conn.commit()
        conn.close()
        _run(gc_db, apply=True, policies=["facts-depth"])
        conn = sqlite3.connect(gc_db)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE id IN (9001, 9002)"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT supersedes_id FROM financial_facts WHERE id = 9003").fetchone()[0]
            is None
        )

    def test_dry_run_counts_but_does_not_delete(self, gc_db: Path) -> None:
        self._seed(gc_db)
        before = (
            sqlite3.connect(gc_db).execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]
        )
        report = _run(gc_db, apply=False, policies=["facts-depth"])
        (pol,) = report.policies
        assert pol.rows_deleted["financial_facts"] > 0
        after = sqlite3.connect(gc_db).execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]
        assert after == before

    def test_include_portfolio_windows_portfolio(self, gc_db: Path) -> None:
        self._seed(gc_db)
        _run(gc_db, apply=True, policies=["facts-depth"], include_portfolio=True)
        conn = sqlite3.connect(gc_db)
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT period_end) FROM financial_facts"
                " WHERE ticker='PORT' AND fiscal_period_type LIKE 'Q%'"
            ).fetchone()[0]
            == 16
        )


class TestAppendOnlyGuardWindow:
    """The 0225 cutover's BEFORE DELETE RAISE(ABORT) trigger on
    financial_facts must be dropped for the prune and recreated verbatim —
    and still block ad-hoc deletes afterward."""

    GUARD_SQL = (
        f"CREATE TRIGGER {db_gc.FACTS_DELETE_GUARD_TRIGGER} "
        "BEFORE DELETE ON financial_facts BEGIN "
        "SELECT RAISE(ABORT, 'financial fact history is append-only after cutover'); END"
    )

    def test_prune_succeeds_and_guard_survives(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 30)
        conn.execute(self.GUARD_SQL)
        conn.commit()
        conn.close()
        report = _run(gc_db, apply=True, policies=["facts-depth"])
        assert report.policies[0].rows_deleted["financial_facts"] > 0
        conn = sqlite3.connect(gc_db)
        # Trigger recreated verbatim…
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (db_gc.FACTS_DELETE_GUARD_TRIGGER,),
        ).fetchone()
        assert sql is not None and "append-only after cutover" in sql[0]
        # …and still functional against ad-hoc deletes.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM financial_facts")

    def test_prune_works_without_guard_present(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 30)
        conn.commit()
        conn.close()
        report = _run(gc_db, apply=True, policies=["facts-depth"])
        assert report.policies[0].rows_deleted["financial_facts"] > 0


class TestGuards:
    def test_window_floors_enforced(self, gc_db: Path) -> None:
        with pytest.raises(ValueError, match="keep-quarters"):
            _run(gc_db, keep_quarters=12)
        with pytest.raises(ValueError, match="keep-fy"):
            _run(gc_db, keep_fy=10)

    def test_batch_size_floor_enforced(self, gc_db: Path) -> None:
        with pytest.raises(ValueError, match="batch-size"):
            _run(gc_db, batch_size=0)

    def test_idempotent_second_apply_is_noop(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 30)
        conn.commit()
        conn.close()
        first = _run(gc_db, apply=True, policies=["facts-depth"])
        second = _run(gc_db, apply=True, policies=["facts-depth"])
        assert first.policies[0].rows_deleted["financial_facts"] > 0
        assert second.policies[0].rows_deleted["financial_facts"] == 0


def _window_always(value: bool) -> Callable[[datetime | None], bool]:
    def check(now: datetime | None = None) -> bool:
        return value

    return check


class TestIncidentHardening:
    """The 2026-07-31 lock-starvation incident fixes: bounded batches, run
    lock, protected window, runtime budget, VACUUM preflight."""

    def _seed_facts(self, db: Path) -> None:
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 30)
        _seed_quarters(conn, "GONE", 8)
        conn.commit()
        conn.close()

    def test_small_batches_reach_same_final_state(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        report = _run(gc_db, apply=True, policies=["facts-depth"], batch_size=7)
        (pol,) = report.policies
        deleted = pol.rows_deleted["financial_facts"]
        assert deleted > 7  # actually exercised more than one batch
        conn = sqlite3.connect(gc_db)
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT period_end) FROM financial_facts"
                " WHERE ticker='EVAL' AND fiscal_period_type LIKE 'Q%'"
            ).fetchone()[0]
            == 16
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM financial_facts WHERE ticker='GONE'").fetchone()[0]
            == 0
        )
        # Archive complete and manifest total matches despite batching.
        arc = sqlite3.connect(gc_db.parent / "archive" / db_gc.ARCHIVE_NAME)
        assert arc.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == deleted
        assert (
            arc.execute(
                "SELECT SUM(rows_archived) FROM gc_manifest WHERE source_table='financial_facts'"
            ).fetchone()[0]
            == deleted
        )
        preflight = report.facts_depth_apply_preflight
        assert preflight is not None
        assert preflight.schema_version == "gc-facts-depth-apply-preflight/v1"
        assert preflight.foreign_keys_enabled is True
        assert preflight.self_fk_target_table == "financial_facts"
        assert preflight.self_fk_from_column == "supersedes_id"
        assert preflight.self_fk_to_column == "id"
        assert preflight.lookup_index_name == "ix_0270_financial_facts_supersedes_id"
        assert preflight.lookup_index_columns == ("supersedes_id",)
        assert preflight.lookup_index_unique is False
        assert preflight.lookup_index_origin == "c"
        assert preflight.lookup_index_partial is False
        assert preflight.sqlite_version == sqlite3.sqlite_version
        assert preflight.lookup_query_plan

    def test_facts_apply_refuses_missing_leading_index_before_archive_or_mutation(
        self, gc_db: Path
    ) -> None:
        self._seed_facts(gc_db)
        with sqlite3.connect(gc_db) as conn:
            conn.execute("DROP INDEX ix_0270_financial_facts_supersedes_id")
            conn.execute(
                "CREATE INDEX ix_bad_facts_ticker_supersedes "
                "ON financial_facts(supersedes_id, ticker)"
            )
            before = conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]

        archive = gc_db.parent / "archive" / db_gc.ARCHIVE_NAME
        with pytest.raises(db_gc.GcAbortedError, match="leading supersedes_id"):
            _run(gc_db, apply=True, policies=["facts-depth"])

        assert not archive.exists()
        with sqlite3.connect(gc_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == before

    def test_facts_dry_run_without_index_is_logically_read_only(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        with sqlite3.connect(gc_db) as conn:
            conn.execute("DROP INDEX ix_0270_financial_facts_supersedes_id")
        before_sha = hashlib.sha256(gc_db.read_bytes()).hexdigest()

        report = _run(gc_db, apply=False, policies=["facts-depth"])

        assert report.policies[0].rows_deleted["financial_facts"] > 0
        assert report.facts_depth_apply_preflight is None
        assert hashlib.sha256(gc_db.read_bytes()).hexdigest() == before_sha
        assert not (gc_db.parent / "archive" / db_gc.ARCHIVE_NAME).exists()

    def test_facts_apply_refuses_missing_self_fk_before_archive(self, gc_db: Path) -> None:
        with sqlite3.connect(gc_db) as conn:
            conn.executescript(
                """
                DROP INDEX ix_0270_financial_facts_supersedes_id;
                ALTER TABLE financial_facts RENAME TO financial_facts_old;
                CREATE TABLE financial_facts (
                    id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
                    fiscal_period_type TEXT, line_item TEXT, value NUMERIC,
                    extracted_by TEXT, supersedes_id INTEGER
                );
                DROP TABLE financial_facts_old;
                CREATE INDEX ix_0270_financial_facts_supersedes_id
                    ON financial_facts(supersedes_id);
                """
            )
        self._seed_facts(gc_db)

        with pytest.raises(db_gc.GcAbortedError, match="REFERENCES financial_facts"):
            _run(gc_db, apply=True, policies=["facts-depth"])

        assert not (gc_db.parent / "archive" / db_gc.ARCHIVE_NAME).exists()

    @pytest.mark.parametrize(
        "table_definition",
        [
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
                fiscal_period_type TEXT, line_item TEXT, value NUMERIC,
                extracted_by TEXT,
                supersedes_id INTEGER REFERENCES financial_facts(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
                fiscal_period_type TEXT, line_item TEXT, value NUMERIC,
                extracted_by TEXT, supersedes_id INTEGER, scope_id INTEGER,
                UNIQUE(id, scope_id),
                FOREIGN KEY(supersedes_id, scope_id)
                    REFERENCES financial_facts(id, scope_id)
            )
            """,
        ],
        ids=["cascade", "composite"],
    )
    def test_facts_apply_refuses_non_exact_self_fk_semantics(
        self,
        gc_db: Path,
        table_definition: str,
    ) -> None:
        with sqlite3.connect(gc_db) as conn:
            conn.executescript(
                "DROP INDEX ix_0270_financial_facts_supersedes_id;"
                "ALTER TABLE financial_facts RENAME TO financial_facts_old;"
                f"{table_definition};"
                "DROP TABLE financial_facts_old;"
                "CREATE INDEX ix_0270_financial_facts_supersedes_id "
                "ON financial_facts(supersedes_id);"
            )
        self._seed_facts(gc_db)

        with pytest.raises(db_gc.GcAbortedError, match="exact single-column self-FK"):
            _run(gc_db, apply=True, policies=["facts-depth"])

        assert not (gc_db.parent / "archive" / db_gc.ARCHIVE_NAME).exists()

    def test_facts_apply_rechecks_index_under_writer_lock_before_mutation(
        self,
        gc_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_facts(gc_db)
        with sqlite3.connect(gc_db) as conn:
            before = conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]
        original = db_gc._archive_doomed

        def archive_then_drift(
            conn: sqlite3.Connection,
            *,
            table: str,
            run_at: str,
            policy: str,
            id_col: str = "id",
            doomed: str = "_gc_doomed",
        ) -> int:
            archived = original(
                conn,
                table=table,
                run_at=run_at,
                policy=policy,
                id_col=id_col,
                doomed=doomed,
            )
            if table == "financial_facts":
                conn.execute("DROP INDEX ix_0270_financial_facts_supersedes_id")
            return archived

        monkeypatch.setattr(db_gc, "_archive_doomed", archive_then_drift)

        with pytest.raises(db_gc.GcAbortedError, match="leading supersedes_id"):
            _run(gc_db, apply=True, policies=["facts-depth"])

        with sqlite3.connect(gc_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == before
        archive = gc_db.parent / "archive" / db_gc.ARCHIVE_NAME
        with sqlite3.connect(archive) as conn:
            assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] > 0

    def test_schema_guard_runs_only_inside_each_immediate_transaction(
        self,
        gc_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_facts(gc_db)
        calls = 0
        original = db_gc.require_current_for_write

        def guarded(conn: sqlite3.Connection) -> None:
            nonlocal calls
            assert conn.in_transaction
            calls += 1
            original(conn)

        monkeypatch.setattr(db_gc, "require_current_for_write", guarded)

        _run(gc_db, apply=True, policies=["facts-depth"], batch_size=7)

        assert calls >= 4

    def test_apply_refuses_hardlink_database_alias_before_lock_or_archive(
        self,
        gc_db: Path,
    ) -> None:
        self._seed_facts(gc_db)
        alias = gc_db.with_name("portfolio-alias.db")
        os.link(gc_db, alias)
        with sqlite3.connect(gc_db) as conn:
            before = conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]

        with pytest.raises(db_gc.GcAbortedError, match="hardlink alias"):
            _run(alias, apply=True, policies=["facts-depth"])

        with sqlite3.connect(gc_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == before
        assert not (gc_db.parent / "archive" / db_gc.ARCHIVE_NAME).exists()
        assert not run_lock.lock_path_for(alias).exists()

    def test_guard_trigger_live_after_batched_prune(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        conn = sqlite3.connect(gc_db)
        conn.execute(TestAppendOnlyGuardWindow.GUARD_SQL)
        conn.commit()
        conn.close()
        report = _run(gc_db, apply=True, policies=["facts-depth"], batch_size=5)
        assert report.policies[0].rows_deleted["financial_facts"] > 5
        conn = sqlite3.connect(gc_db)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM financial_facts")

    def test_validation_collapse_with_small_batches(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        for group in range(4):
            for i in range(3):
                conn.execute(
                    "INSERT INTO validation_issues (run_id, source_doc_id, ticker,"
                    " severity, rule, raw_value, expected, raised_at, occurrence_count)"
                    " VALUES (?, ?, 'NU', 'warn', 'PLAUSIBLE_RANGE', ?, 'x<1', ?, 1)",
                    (f"run{i}", group, f"x={group}", f"2026-07-2{4 + i} 04:00:00"),
                )
        conn.commit()
        conn.close()
        report = _run(gc_db, apply=True, policies=["validation-issues"], batch_size=3)
        (pol,) = report.policies
        assert pol.rows_deleted["validation_issues"] == 8  # 4 groups x 2 dups
        conn = sqlite3.connect(gc_db)
        assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 4
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM validation_issues WHERE fingerprint IS NOT NULL"
            ).fetchone()[0]
            == 4
        )

    def test_apply_yields_when_run_lock_held(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        lock = run_lock.acquire_run_lock(gc_db, owner="run_morning_pipeline", timeout_s=0)
        try:
            with pytest.raises(run_lock.RunLockHeldError):
                _run(gc_db, apply=True, policies=["facts-depth"])
        finally:
            lock.release()
        # Nothing was mutated while the lock was held.
        conn = sqlite3.connect(gc_db)
        assert (
            conn.execute("SELECT COUNT(*) FROM financial_facts WHERE ticker='GONE'").fetchone()[0]
            > 0
        )

    def test_dry_run_takes_no_lock(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        lock = run_lock.acquire_run_lock(gc_db, owner="run_morning_pipeline", timeout_s=0)
        try:
            report = _run(gc_db, apply=False, policies=["facts-depth"])
            assert report.policies[0].rows_deleted["financial_facts"] > 0
        finally:
            lock.release()

    def test_protected_window_pure_function(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        la = ZoneInfo(db_gc.PROTECTED_WINDOW_TZ)
        assert db_gc.in_protected_window(datetime(2026, 8, 1, 3, 0, tzinfo=la)) is True
        assert db_gc.in_protected_window(datetime(2026, 8, 1, 4, 59, tzinfo=la)) is True
        assert db_gc.in_protected_window(datetime(2026, 8, 1, 5, 0, tzinfo=la)) is False
        assert db_gc.in_protected_window(datetime(2026, 8, 1, 2, 59, tzinfo=la)) is False

    def test_protected_window_refuses_apply(
        self, gc_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_facts(gc_db)
        monkeypatch.setattr(db_gc, "in_protected_window", _window_always(True))
        with pytest.raises(db_gc.GcAbortedError, match="protected"):
            _run(gc_db, apply=True, policies=["facts-depth"], enforce_protected_window=True)
        # Dry runs and out-of-window applies are unaffected.
        _run(gc_db, apply=False, policies=["facts-depth"], enforce_protected_window=True)
        monkeypatch.setattr(db_gc, "in_protected_window", _window_always(False))
        _run(gc_db, apply=True, policies=["facts-depth"], enforce_protected_window=True)

    def test_cli_protected_window_exit_code(
        self, gc_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_gc, "in_protected_window", _window_always(True))
        rc = db_gc.main(["--db-path", str(gc_db), "--apply", "--policies", "telemetry"])
        assert rc == 2
        rc = db_gc.main(
            [
                "--db-path",
                str(gc_db),
                "--apply",
                "--policies",
                "telemetry",
                "--ignore-protected-window",
            ]
        )
        assert rc == 0

    def test_runtime_budget_aborts_loudly(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        with pytest.raises(db_gc.GcAbortedError, match="budget"):
            _run(gc_db, apply=True, policies=["facts-depth"], max_runtime_min=0.0)

    def test_vacuum_preflight_aborts_when_write_locked(
        self, gc_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-convert to WAL so the writer policy's journal_mode pragma is a
        # no-op under contention, then hold the write lock from a second
        # connection: the VACUUM exclusivity preflight must abort loudly
        # within its short timeout instead of livelocking.
        conn = sqlite3.connect(gc_db)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.close()
        monkeypatch.setattr(db_gc, "VACUUM_PREFLIGHT_TIMEOUT_S", 0.3)
        blocker = sqlite3.connect(gc_db)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(db_gc.GcAbortedError, match="write lock"):
                _run(gc_db, apply=True, policies=["maintenance"], vacuum=True)
        finally:
            blocker.rollback()
            blocker.close()

    def test_vacuum_runs_under_guards_when_uncontended(self, gc_db: Path) -> None:
        self._seed_facts(gc_db)
        report = _run(gc_db, apply=True, policies=["facts-depth", "maintenance"], vacuum=True)
        maint = report.policies[-1]
        assert maint.detail.get("vacuum") == 1
