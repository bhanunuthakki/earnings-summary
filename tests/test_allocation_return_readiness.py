"""Persisted valuations need shared readiness before they can rank a return."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

import pytest

from allocation.model import build_next_dollar_model
from dcf.latest import latest_dcf_row
from dcf.readiness import ValuationReadiness


class DcfUpside(Protocol):
    def __call__(
        self,
        db_path: Path,
        tickers: Sequence[str],
        *,
        as_of: datetime | None = None,
        unavailable: dict[str, str] | None = None,
    ) -> dict[str, tuple[float, str]]: ...


dcf_upside = cast(
    DcfUpside,
    getattr(import_module("allocation.model"), "_dcf_upside"),
)
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def _use_readiness(monkeypatch: pytest.MonkeyPatch, receipt: ValuationReadiness) -> None:
    def read(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        del conn, ticker, as_of
        return receipt

    monkeypatch.setattr("allocation.model.load_valuation_readiness", read)


@pytest.fixture()
def persisted_run(tmp_path: Path) -> Path:
    db = tmp_path / "synthetic.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                created_at TEXT,
                valuation_date TEXT,
                npv_per_share REAL,
                live_price REAL
            );
            INSERT INTO dcf_runs VALUES (
                1, 'AAA', '2026-10-01T12:00:00+00:00', '2026-10-01', 150.0, 100.0
            );
            """
        )
    return db


def test_unqualified_persisted_run_cannot_rank_return(persisted_run: Path) -> None:
    # A value and price alone do not establish input lineage or completeness.
    assert dcf_upside(persisted_run, ["AAA"]) == {}


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("degraded", "financial_input_completeness_unverified"),
        ("failed", "valuation_evidence_query_failed"),
        ("missing", "dcf_missing"),
    ],
)
def test_shared_readiness_rejections_remain_explicit(
    persisted_run: Path, monkeypatch: pytest.MonkeyPatch, status: str, reason: str
) -> None:
    receipt = ValuationReadiness.model_validate(
        {
            "ticker": "AAA",
            "evaluated_at": NOW.isoformat(),
            "status": status,
            "reason_codes": [reason],
            "run_id": 1,
        }
    )
    _use_readiness(monkeypatch, receipt)
    unavailable: dict[str, str] = {}
    assert dcf_upside(persisted_run, ["AAA"], as_of=NOW, unavailable=unavailable) == {}
    assert unavailable == {"AAA": reason}


def test_ready_exact_run_uses_aware_cutoff_and_cannot_write(
    persisted_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = persisted_run.read_bytes()
    evaluated: list[datetime] = []

    def qualify(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        assert conn.in_transaction
        evaluated.append(as_of)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE dcf_runs SET npv_per_share = 999.0")
        row = latest_dcf_row(conn, ticker)
        assert row is not None
        return ValuationReadiness(
            ticker=ticker, evaluated_at=as_of.isoformat(), ready=True, status="ready", run_id=row.id
        )

    monkeypatch.setattr("allocation.model.load_valuation_readiness", qualify)
    local_cutoff = NOW.astimezone(timezone(timedelta(hours=-7)))
    out = dcf_upside(persisted_run, ["AAA"], as_of=local_cutoff)
    assert out["AAA"][0] == pytest.approx(0.5)
    assert evaluated == [NOW]
    assert evaluated[0].tzinfo is UTC
    assert persisted_run.read_bytes() == before


@pytest.mark.parametrize("run_id", [None, 2])
def test_ready_receipt_for_another_run_cannot_rank(
    persisted_run: Path, monkeypatch: pytest.MonkeyPatch, run_id: int | None
) -> None:
    receipt = ValuationReadiness(
        ticker="AAA", evaluated_at=NOW.isoformat(), ready=True, status="ready", run_id=run_id
    )
    _use_readiness(monkeypatch, receipt)
    unavailable: dict[str, str] = {}
    assert dcf_upside(persisted_run, ["AAA"], as_of=NOW, unavailable=unavailable) == {}
    assert unavailable == {"AAA": "valuation_readiness_run_mismatch"}


def test_query_exception_is_a_rejection(
    persisted_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        del conn, ticker, as_of
        raise sqlite3.OperationalError("synthetic query failure")

    monkeypatch.setattr("allocation.model.load_valuation_readiness", fail)
    unavailable: dict[str, str] = {}
    assert dcf_upside(persisted_run, ["AAA"], as_of=NOW, unavailable=unavailable) == {}
    assert unavailable == {"AAA": "valuation_evidence_query_failed"}


def test_run_and_readiness_share_snapshot_during_concurrent_persistence(
    persisted_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(persisted_run) as writer:
        writer.execute("PRAGMA journal_mode=WAL")

    def qualify(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        with sqlite3.connect(persisted_run) as writer:
            writer.execute(
                "INSERT INTO dcf_runs VALUES "
                "(2, 'AAA', '2026-10-02T10:00:00+00:00', '2026-10-02', 999.0, 100.0)"
            )
        row = latest_dcf_row(conn, ticker)
        assert row is not None and row.id == 1
        return ValuationReadiness(
            ticker=ticker, evaluated_at=as_of.isoformat(), ready=True, status="ready", run_id=row.id
        )

    monkeypatch.setattr("allocation.model.load_valuation_readiness", qualify)
    assert dcf_upside(persisted_run, ["AAA"], as_of=NOW)["AAA"][0] == pytest.approx(0.5)
    with sqlite3.connect(persisted_run) as conn:
        row = latest_dcf_row(conn, "AAA")
        assert row is not None and row.id == 2


def test_naive_cutoff_rejected(persisted_run: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        dcf_upside(persisted_run, ["AAA"], as_of=NOW.replace(tzinfo=None))


def test_missing_database_remains_absent(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    unavailable: dict[str, str] = {}
    assert dcf_upside(missing, ["AAA"], as_of=NOW, unavailable=unavailable) == {}
    assert unavailable == {"AAA": "dcf_database_missing"}
    assert not missing.exists()


def test_rejected_return_reason_reaches_existing_model_state(
    persisted_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        del conn
        return ValuationReadiness(
            ticker=ticker,
            evaluated_at=as_of.isoformat(),
            status="failed",
            reason_codes=("valuation_evidence_query_failed",),
        )

    monkeypatch.setattr("allocation.model.load_valuation_readiness", reject)

    def macro(db_path: Path, tickers: Sequence[str]) -> dict[str, tuple[float, str]]:
        del db_path, tickers
        return {"AAA": (0.1, "synthetic"), "BBB": (0.2, "synthetic")}

    monkeypatch.setattr("allocation.model._macro_tilt", macro)
    model = build_next_dollar_model(persisted_run, tmp_path, ["AAA", "BBB"])
    assert model is not None
    assert "valuation_evidence_query_failed" in model.hidden_factors["ret"]
    assert model.notes == [
        "Valuation upside unavailable for AAA: valuation_evidence_query_failed",
        "Valuation upside unavailable for BBB: valuation_evidence_query_failed",
    ]
    assert all(row.ret is None for row in model.rows)
