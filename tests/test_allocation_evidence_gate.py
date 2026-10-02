"""Real readiness composition must block scalar-only valuation evidence."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from advisor.context import TickerValuation, screen_swap_candidates
from allocation.eligibility import CHECK_USABLE_DCF, assess_eligibility, cash_assessment
from execution.valuation_preflight import main


def test_recent_quote_does_not_admit_scalar_only_model(tmp_path: Path) -> None:
    db_path = tmp_path / "explicit.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, "
            "valuation_date TEXT, npv_per_share REAL, live_price REAL, live_price_at TEXT)"
        )
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO dcf_runs VALUES (1, 'MELI', ?, ?, 120, 100, ?)", (now, now[:10], now)
        )
    result = assess_eligibility(db_path, tmp_path, "MELI", list_type="portfolio")
    assert not result.eligible
    check = result.checks[CHECK_USABLE_DCF]
    assert not check.passed
    assert "dcf_evidence_invalid" in check.reason
    assert cash_assessment().eligible


def test_swap_requires_evidence_for_both_comparison_legs() -> None:
    today = datetime.now(UTC)
    holding = TickerValuation("HELD", 10, today.date().isoformat(), "portfolio", "ok")
    candidate = TickerValuation("NEW", 50, today.date().isoformat(), "evaluation", "ok")
    assert screen_swap_candidates({"HELD": holding}, {"NEW": candidate}, now=today) == []
    candidate = replace(candidate, evidence_ready=True, evidence_reasons=())
    assert screen_swap_candidates({"HELD": holding}, {"NEW": candidate}, now=today) == []
    holding = replace(holding, evidence_ready=True, evidence_reasons=())
    assert screen_swap_candidates({"HELD": holding}, {"NEW": candidate}, now=today)[0].cleared


def test_preflight_missing_authority_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.db"
    assert main(["--db-path", str(missing), "--ticker", "MELI"]) == 3
    assert not missing.exists()
    assert "database_authority_unavailable" in capsys.readouterr().out


def test_preflight_reports_missing_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "explicit.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
    original = db_path.read_bytes()
    assert main(["--db-path", str(db_path), "--ticker", "MELI"]) == 2
    assert "dcf_evidence_invalid" in capsys.readouterr().out
    assert original == db_path.read_bytes()


def test_preflight_connection_failure_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "explicit.db"
    db_path.touch()

    def unavailable(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("sensitive diagnostic must not escape")

    monkeypatch.setattr("execution.valuation_preflight.connect_sqlite", unavailable)
    assert main(["--db-path", str(db_path), "--ticker", "MELI"]) == 3
    output = capsys.readouterr().out
    assert "database_open_failed" in output
    assert "sensitive" not in output


def test_advisor_values_and_evidence_share_read_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from advisor.context import load_valuations
    from dcf.readiness import ValuationReadiness

    db_path = tmp_path / "snapshot.db"
    with sqlite3.connect(db_path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT);"
            "INSERT INTO tracked_companies VALUES ('MELI', 'portfolio', NULL);"
            "CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, "
            "valuation_date TEXT, npv_per_share REAL, live_price REAL);"
            "INSERT INTO dcf_runs VALUES (1, 'MELI', '2026-10-01T12:00:00+00:00', "
            "'2026-10-01', 120, 100);"
        )
    observed: list[float] = []

    def evidence(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        with sqlite3.connect(db_path) as writer:
            writer.execute("UPDATE dcf_runs SET npv_per_share=200 WHERE id=1")
        observed.append(float(conn.execute("SELECT npv_per_share FROM dcf_runs").fetchone()[0]))
        return ValuationReadiness(
            ticker=ticker, evaluated_at=as_of.isoformat(), ready=True, status="ready"
        )

    monkeypatch.setattr("advisor.context.load_valuation_readiness", evidence)
    with sqlite3.connect(db_path) as conn:
        assert not conn.in_transaction
        holdings, _ = load_valuations(conn)
        assert not conn.in_transaction
    assert holdings["MELI"].upside_pct == pytest.approx(20)
    assert observed == [120]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT npv_per_share FROM dcf_runs").fetchone()[0] == 200


def test_risk_snapshot_reads_do_not_change_database_journal(tmp_path: Path) -> None:
    from portfolio_risk_snapshot_store import history_has_sha, read_history, read_latest_snapshot

    db_path = tmp_path / "read-only.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    original = db_path.read_bytes()
    assert read_latest_snapshot(db_path=db_path) is None
    assert read_history(db_path=db_path) == []
    assert not history_has_sha("missing", db_path=db_path)
    assert db_path.read_bytes() == original
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_allocation_closes_snapshot_on_reader_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(tmp_path / "snapshot.db")
    conn.execute("BEGIN")

    def opened(path: Path) -> sqlite3.Connection:
        del path
        return conn

    monkeypatch.setattr("allocation.eligibility._ro_conn", opened)

    def failed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("reader failed")

    monkeypatch.setattr("allocation.eligibility._check_disconfirmers", failed)
    with pytest.raises(RuntimeError, match="reader failed"):
        assess_eligibility(tmp_path / "snapshot.db", tmp_path, "MELI", list_type="portfolio")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
