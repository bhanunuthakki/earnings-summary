"""Tests for execution/db_gc.py — the periodic DB garbage collector.

Fixture DBs deliberately omit alembic_version so schema_compat's write
preflight no-ops (its documented fixture contract)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "execution"))

import db_gc  # noqa: E402


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
        # Singleton untouched.
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
        conn = sqlite3.connect(gc_db)
        assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 4
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM validation_issues WHERE fingerprint IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


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
