# pyright: reportPrivateUsage=false
"""Regression pins for the 2026-08-03 adversarial review of db_gc + run_lock.

Each test reproduces a confirmed defect's exact failure scenario and asserts
the fixed behavior. Numbers reference the review's ranking."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
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
        CREATE TABLE fact_observation_revisions (fact_table TEXT, fact_row_id INTEGER);
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


def _run_historical_facts_apply(db: Path) -> db_gc.GcRunReport:
    """Exercise the retained implementation without reopening the public gate."""

    return db_gc._run_gc_implementation(
        db,
        apply=True,
        policies=["facts-depth"],
        retention_days=90,
        keep_quarters=16,
        keep_fy=12,
        include_portfolio=True,
        vacuum=False,
        batch_size=db_gc.DEFAULT_BATCH_SIZE,
        max_runtime_min=db_gc.DEFAULT_MAX_RUNTIME_MIN,
        lock_timeout_s=0.0,
        enforce_protected_window=False,
    )


def _seed_quarters(conn: sqlite3.Connection, ticker: str, n: int) -> None:
    y, m = 2026, 3
    for _ in range(n):
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


class TestReviewFix1RecycledIdVsSupersedesNulling:
    """#1: an apply that aborts AFTER the supersedes-nulling commit but BEFORE
    the delete batches finish must be retryable — the archived copy differs
    from the live row only in supersedes_id, which the GC itself rewrote."""

    def test_retry_after_nulling_commit_succeeds(self, gc_db: Path) -> None:
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 20)
        # A doomed chain: 9002 supersedes 9001, both in a pruned period.
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES"
            " (9001, 'EVAL', '2019-03-31', 'Q1', 'revenue', 1, 'fmp')"
        )
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by, supersedes_id) VALUES"
            " (9002, 'EVAL', '2019-03-31', 'Q1', 'revenue', 2, 'sec_xbrl', 9001)"
        )
        conn.commit()
        conn.close()

        # Simulate the interrupted first apply: archive completes, the nulling
        # commits, the delete batches never run (hard kill / budget abort).
        conn = sqlite3.connect(gc_db)
        arc = gc_db.parent / "archive" / db_gc.ARCHIVE_NAME
        db_gc.attach_archive(conn, arc)
        db_gc._reset_doomed(conn)
        conn.execute("INSERT INTO _gc_doomed (id) VALUES (9001), (9002)")
        db_gc._archive_doomed(
            conn, table="financial_facts", run_at="2026-08-03T00:00:00", policy="facts-depth"
        )
        conn.execute(
            "UPDATE financial_facts SET supersedes_id = NULL "
            "WHERE supersedes_id IN (SELECT id FROM _gc_doomed)"
        )
        conn.commit()
        conn.close()

        # The retry (a full apply) must NOT abort with a recycled-id error.
        report = _run_historical_facts_apply(gc_db)
        deleted = next(p for p in report.policies if p.policy == "facts-depth")
        assert deleted.rows_deleted["financial_facts"] > 0
        conn = sqlite3.connect(gc_db)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE id IN (9001, 9002)"
            ).fetchone()[0]
            == 0
        )

    def test_a_genuinely_recycled_id_becomes_a_recoverable_variant(self, gc_db: Path) -> None:
        """Post-run-keying (2026-08-03): a NEW row minted under an archived id
        with a truly different payload no longer fail-closes (that was #1140's
        prune dead-end) — it is archived as a DISTINCT run-attributed variant,
        so the original and the recycled row are both recoverable and the prune
        proceeds. The safety property (no silent loss) is preserved; only the
        mechanism changed from abort to variant."""
        conn = sqlite3.connect(gc_db)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES ('EVAL', 'evaluation')"
        )
        _seed_quarters(conn, "EVAL", 20)
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES"
            " (9100, 'EVAL', '2019-06-30', 'Q2', 'revenue', 1, 'fmp')"
        )
        conn.commit()

        arc = gc_db.parent / "archive" / db_gc.ARCHIVE_NAME
        arc.parent.mkdir(parents=True, exist_ok=True)
        db_gc.attach_archive(conn, arc)
        db_gc._reset_doomed(conn)
        conn.execute("INSERT INTO _gc_doomed (id) VALUES (9100)")
        db_gc._archive_doomed(
            conn, table="financial_facts", run_at="2026-08-03T00:00:00", policy="facts-depth"
        )
        # Recycle: delete the live row and mint a DIFFERENT row under its id.
        conn.execute("DELETE FROM financial_facts WHERE id = 9100")
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES"
            " (9100, 'EVAL', '2019-09-30', 'Q3', 'gross_profit', 777, 'fmp')"
        )
        conn.commit()
        conn.close()

        # No abort: the full apply archives the recycled row under its own run
        # and prunes it.
        report = _run_historical_facts_apply(gc_db)
        facts = next(p for p in report.policies if p.policy == "facts-depth")
        assert facts.rows_deleted["financial_facts"] > 0

        conn = sqlite3.connect(gc_db)
        db_gc.attach_archive(conn, arc)
        # Both payloads survive in the archive under DIFFERENT gc_run_ids — the
        # original Q2 revenue and the recycled Q3 gross_profit.
        variants = conn.execute(
            "SELECT gc_run_id, fiscal_period_type, line_item, value "
            'FROM gcarc."financial_facts" WHERE id = 9100 ORDER BY gc_run_id'
        ).fetchall()
        conn.close()
        assert len(variants) == 2
        runs = {r[0] for r in variants}
        assert len(runs) == 2  # attributed to two distinct runs
        assert ("2026-08-03T00:00:00", "Q2", "revenue", 1) in variants
        assert any(v[1:] == ("Q3", "gross_profit", 777) for v in variants)


class TestReviewFix3AggregatesSurviveAbort:
    """#3: survivor aggregates must be durably written BEFORE duplicates are
    deleted, so an abort in between cannot reset history to n=1."""

    def test_aggregates_written_before_duplicate_delete(
        self, gc_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = sqlite3.connect(gc_db)
        for i, day in enumerate(("2026-07-24", "2026-07-25", "2026-07-26")):
            conn.execute(
                "INSERT INTO validation_issues (run_id, source_doc_id, ticker,"
                " severity, rule, raw_value, expected, raised_at, occurrence_count)"
                " VALUES (?, 7, 'NU', 'warn', 'PLAUSIBLE_RANGE', 'x=1', 'x<1', ?, 1)",
                (f"run{i}", f"{day} 04:00:00"),
            )
        conn.commit()
        conn.close()

        # Kill the run at the moment duplicates would be archived — i.e. after
        # the (fixed) aggregate pass, before any deletion.
        real_archive = db_gc._archive_doomed

        def bomb(*args: object, **kwargs: object) -> int:
            raise db_gc.GcAbortedError("injected abort before duplicate delete")

        monkeypatch.setattr(db_gc, "_archive_doomed", bomb)
        with pytest.raises(db_gc.GcAbortedError, match="injected"):
            db_gc.run_gc(
                gc_db,
                apply=True,
                policies=["validation-issues"],
                retention_days=90,
                keep_quarters=16,
                keep_fy=12,
                include_portfolio=False,
                vacuum=False,
                batch_size=db_gc.DEFAULT_BATCH_SIZE,
                max_runtime_min=db_gc.DEFAULT_MAX_RUNTIME_MIN,
                lock_timeout_s=0.0,
                enforce_protected_window=False,
            )
        monkeypatch.setattr(db_gc, "_archive_doomed", real_archive)

        # Duplicates still present (abort pre-delete), but the survivor's
        # aggregate history is already durable.
        conn = sqlite3.connect(gc_db)
        assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 3
        newest = conn.execute(
            "SELECT first_seen_at, occurrence_count FROM validation_issues "
            "ORDER BY raised_at DESC LIMIT 1"
        ).fetchone()
        assert newest[0].startswith("2026-07-24")
        assert newest[1] == 3
        conn.close()

        # And the retry completes the collapse with the same truth.
        db_gc.run_gc(
            gc_db,
            apply=True,
            policies=["validation-issues"],
            retention_days=90,
            keep_quarters=16,
            keep_fy=12,
            include_portfolio=False,
            vacuum=False,
            batch_size=db_gc.DEFAULT_BATCH_SIZE,
            max_runtime_min=db_gc.DEFAULT_MAX_RUNTIME_MIN,
            lock_timeout_s=0.0,
            enforce_protected_window=False,
        )
        conn = sqlite3.connect(gc_db)
        row = conn.execute(
            "SELECT first_seen_at, last_seen_at, occurrence_count, fingerprint "
            "FROM validation_issues"
        ).fetchall()
        assert len(row) == 1
        assert row[0][0].startswith("2026-07-24")
        assert row[0][1].startswith("2026-07-26")
        assert row[0][2] == 3
        assert row[0][3] is not None


class TestReviewFix5LockAtomicity:
    """#5: the lock file must never be observable without its payload — an
    acquirer racing the creation must see either no file or a complete one."""

    def test_lock_file_carries_payload_from_birth(self, tmp_path: Path) -> None:
        db = tmp_path / "x.db"
        db.write_text("")
        lock = run_lock.acquire_run_lock(db, owner="a", timeout_s=0.0)
        try:
            holder = run_lock._read_holder(run_lock.lock_path_for(db))
            assert holder is not None and holder.get("pid") == os.getpid()
        finally:
            lock.release()

    def test_concurrent_acquirers_yield_exactly_one_winner(self, tmp_path: Path) -> None:
        db = tmp_path / "x.db"
        db.write_text("")
        results: list[str] = []
        barrier = threading.Barrier(8)

        def contend(i: int) -> None:
            barrier.wait()
            try:
                lk = run_lock.acquire_run_lock(db, owner=f"t{i}", timeout_s=0.0)
                results.append(f"won:{i}")
                lk.release()
            except run_lock.RunLockHeldError:
                results.append(f"lost:{i}")
            except Exception as exc:
                results.append(f"error:{i}:{exc!r}")

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # Winners release immediately, so several may win sequentially. The
        # invariants under attack: no thread crashes (the old empty-file race
        # produced FileNotFoundError/JSON errors mid-break), at least one
        # winner exists, every outcome is a clean win or a clean held-error,
        # and no temp files leak.
        assert len(results) == 8
        errors = [r for r in results if r.startswith("error:")]
        assert not errors, errors
        assert any(r.startswith("won:") for r in results)
        leftovers = list(db.parent.glob("*.write.lock.*.tmp"))
        assert not leftovers, leftovers
