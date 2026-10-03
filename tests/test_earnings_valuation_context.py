"""Valuation context cannot turn stored, unaccepted estimates into signals."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import earnings_brief
from dcf.readiness import ValuationReadiness

NOW = datetime(2026, 10, 2, 20, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(migrated_db(tmp_path / "valuation-context.db"))
    try:
        yield connection
    finally:
        connection.close()


def seed(
    conn: sqlite3.Connection,
    *,
    value: float = 120,
    latest: int = 1,
    segment: str | None = None,
    calculated_at: str = "2026-10-02T18:00:00+00:00",
) -> int:
    digest = hashlib.sha256(b"synthetic valuation context").hexdigest()
    cursor = conn.execute(
        """INSERT INTO dcf_runs (
        ticker, created_at, valuation_date, engine_version, input_sha256,
        workbook_sha256, inputs_as_of, live_price, live_price_at, npv_per_share,
        assumption_snapshot_json, provenance_json, is_latest, segment_name,
        horizon_years, revenue_growths_json, fcf_margin, wacc, terminal_growth, npv
        ) VALUES ('MELI',?,'2026-10-02','synthetic',?,?,?,100,?,?, '{}',?,?,?,
                  10,'[]',0.2,0.1,0.03,?)""",
        (
            calculated_at,
            digest,
            digest,
            NOW.isoformat(),
            NOW.isoformat(),
            value,
            "{}",
            latest,
            segment,
            value,
        ),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_superseded_and_segment_estimates_cannot_win(conn: sqlite3.Connection) -> None:
    seed(conn)
    seed(conn, value=999, latest=0, calculated_at="2026-10-02T19:00:00+00:00")
    seed(conn, value=888, segment="credit", calculated_at="2026-10-02T19:30:00+00:00")
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "120.00" in text
    assert "999.00" not in text
    assert "888.00" not in text


def test_unaccepted_estimate_has_no_gap_signal(conn: sqlite3.Connection) -> None:
    seed(conn)
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "stored quote" in text.lower()
    assert "live $" not in text
    assert "unaccepted" in text.lower()
    assert "vs fair" not in text
    assert "financial_input" in text


def test_ready_estimate_requires_matching_run(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = seed(conn)

    def ready(_conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        return ValuationReadiness(
            ticker=ticker,
            evaluated_at=as_of.isoformat(),
            ready=True,
            status="ready",
            run_id=run_id,
            financial_period_end="2026-06-30",
        )

    monkeypatch.setattr(earnings_brief, "load_valuation_readiness", ready)
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "accepted" in text
    assert "+20%" in text  # Recomputed from value/quote, not the stored convention.
    assert "2026-06-30" in text


def test_mismatched_readiness_is_not_a_signal(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(conn)

    def wrong(_conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        return ValuationReadiness(
            ticker=ticker, evaluated_at=as_of.isoformat(), ready=True, status="ready", run_id=9999
        )

    monkeypatch.setattr(earnings_brief, "load_valuation_readiness", wrong)
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "run_mismatch" in text
    assert "%" not in text


def test_future_model_is_not_historical_context(conn: sqlite3.Connection) -> None:
    seed(conn, calculated_at="2026-10-02T20:00:00.900000+00:00")
    cutoff = NOW.replace(microsecond=100000)
    text = earnings_brief.valuation_text(conn, "MELI", as_of=cutoff)
    assert "unavailable at cutoff" in text.lower()
    assert "120.00" not in text


def test_preserves_caller_snapshot_and_factory(conn: sqlite3.Connection) -> None:
    seed(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    changes = conn.total_changes
    earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert conn.in_transaction
    assert conn.row_factory is sqlite3.Row
    assert conn.total_changes == changes
    conn.rollback()


def test_closes_owned_snapshot(conn: sqlite3.Connection) -> None:
    seed(conn)
    earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert not conn.in_transaction
    assert conn.row_factory is None


def test_naive_cutoff_is_rejected(conn: sqlite3.Connection) -> None:
    seed(conn)
    with pytest.raises(ValueError, match="timezone"):
        earnings_brief.valuation_text(conn, "MELI", as_of=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("quote_at", "reason", "show_quote"),
    [
        (None, "market_price_or_timestamp_missing", False),
        ("2025-01-01T00:00:00+00:00", "market_price_stale", True),
        ("2026-10-02T20:00:00.900000+00:00", "market_timestamp_after_cutoff", False),
        ("2026-10-02T20:00:00", "market_timestamp_invalid", False),
    ],
)
def test_quote_limits_are_visible_without_return_signal(
    conn: sqlite3.Connection, quote_at: str | None, reason: str, show_quote: bool
) -> None:
    seed(conn)
    conn.execute("UPDATE dcf_runs SET live_price_at = ?", (quote_at,))
    conn.commit()
    text = earnings_brief.valuation_text(conn, "meli", as_of=NOW)
    assert reason in text
    assert ("stored quote" in text) is show_quote
    assert "%" not in text


def test_invalid_model_is_explicit(conn: sqlite3.Connection) -> None:
    seed(conn)
    conn.execute("UPDATE dcf_runs SET npv_per_share = 'bad'")
    conn.commit()
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "invalid" in text
    assert "%" not in text


def test_absent_model_is_explicit(conn: sqlite3.Connection) -> None:
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "dcf_missing" in text


@pytest.mark.parametrize("column", ["provenance_json", "assumption_snapshot_json"])
def test_failed_model_evidence_cannot_leak_future_quote(
    conn: sqlite3.Connection, column: str
) -> None:
    seed(conn)
    # The identifier is selected only from this fixed test-owned parameter set.
    assert column in {"provenance_json", "assumption_snapshot_json"}
    conn.execute(f"UPDATE dcf_runs SET {column} = 'bad'")  # nosec B608
    conn.execute("UPDATE dcf_runs SET live_price_at = '2026-10-03T00:00:00+00:00'")
    conn.commit()
    text = earnings_brief.valuation_text(conn, "MELI", as_of=NOW)
    assert "100.00" not in text
    assert "market_timestamp_after_cutoff" in text
    assert "%" not in text
