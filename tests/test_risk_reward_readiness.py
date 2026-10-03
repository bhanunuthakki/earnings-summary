"""Risk and supporting valuation factors must retain real model readiness gates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

import pytest

from allocation.book_risk import BookRisk
from dcf.input_evidence import ModelInputRequest
from dcf.latest import latest_dcf_row
from dcf.meli_inputs import model_output, prepare_meli_inputs
from dcf.readiness import ValuationReadiness, load_valuation_readiness
from risk_reward import Reward, build_gap_rows
from tests.test_meli_input_evidence import real_inputs

__all__ = ["real_inputs"]  # Reuse the public current-schema fixture; readiness is never mocked.


class RewardLegs(Protocol):
    def __call__(
        self, db_path: Path, tickers: Sequence[str], today: date, *, as_of: datetime | None = None
    ) -> dict[str, Reward]: ...


reward_legs = cast(RewardLegs, getattr(import_module("risk_reward"), "_dcf_reward_legs"))


class DcfUpside(Protocol):
    def __call__(
        self,
        db_path: Path,
        tickers: Sequence[str],
        *,
        as_of: datetime | None = None,
        unavailable: dict[str, str] | None = None,
    ) -> dict[str, tuple[float, str]]: ...


valuation_factor = cast(DcfUpside, getattr(import_module("allocation.model"), "_dcf_upside"))
NOW = datetime(2026, 10, 1, 23, 59, 59, 999999, tzinfo=UTC)


def _persist(
    conn: sqlite3.Connection,
    *,
    ticker: str = "META",
    provenance: object = None,
    snapshot: object = None,
    npv: float = 150,
    total: float = 1000,
) -> Path:
    conn.execute(
        """INSERT INTO dcf_runs (ticker,valuation_date,horizon_years,revenue_growths_json,
        fcf_margin,wacc,terminal_growth,npv,npv_per_share,created_at,live_price,live_price_at,
        input_sha256,workbook_sha256,engine_version,inputs_as_of,assumption_snapshot_json,provenance_json)
        VALUES (?,?,10,'[]',0,.135,.045,?,?,?,100,?,?,?,'meli_platform_sotp_v1',?,?,?)""",
        (
            ticker,
            NOW.date().isoformat(),
            total,
            npv,
            NOW.isoformat(),
            NOW.isoformat(),
            "a" * 64,
            "b" * 64,
            NOW.isoformat(),
            json.dumps(snapshot or {}),
            json.dumps(provenance or {}),
        ),
    )
    conn.commit()
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def test_current_quote_and_scalar_value_do_not_establish_readiness(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db = migrated_db(tmp_path / "risk-readiness.db")
    with sqlite3.connect(db) as conn:
        _persist(conn)
    before = db.read_bytes()
    leg = reward_legs(db, ["META"], NOW.date())["META"]
    assert leg.expected_return is None
    assert leg.low_confidence
    assert "financial_input_lineage_missing" in (leg.confidence_reason or "")
    assert "financial_input_completeness_unverified" in (leg.confidence_reason or "")
    assert valuation_factor(db, ["META"], as_of=NOW) == {}
    assert db.read_bytes() == before
    book = BookRisk(
        tickers=["META"],
        weights={"META": 1},
        marginal_vol_ann={"META": 0.3},
        risk_contribution_ann={"META": 0.3},
        risk_share={"META": 1},
        corr_to_book={"META": 1},
        portfolio_vol_ann=0.3,
        prices_through=NOW.date(),
        cov_obs=252,
        shrinkage=0.1,
    )
    rows, valued = build_gap_rows(book, {"META": leg}, {"META": 2})
    assert valued == 0
    assert rows[0].mismatch_score == 2
    assert rows[0].reward_share_pct is None
    assert any("conviction 2/5" in reason for reason in rows[0].mismatch_reasons)


@pytest.mark.parametrize(
    ("price_at", "reason"),
    [
        ((NOW + timedelta(days=1)).isoformat(), "market_timestamp_after_cutoff"),
        ("invalid", "row_decode_failed"),
        ((NOW - timedelta(days=30)).isoformat(), "market_price_stale"),
    ],
)
def test_quote_rejections_remain_explicit(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    price_at: str,
    reason: str,
) -> None:
    db = migrated_db(tmp_path / "risk-quote.db")
    with sqlite3.connect(db) as conn:
        _persist(conn)
        conn.execute("UPDATE dcf_runs SET live_price_at=?", (price_at,))
    leg = reward_legs(db, ["META"], NOW.date())["META"]
    assert leg.expected_return is None
    assert reason in (leg.confidence_reason or "")


def test_replayed_reported_base_remains_unavailable_without_scenario_acceptance(
    real_inputs: tuple[sqlite3.Connection, ModelInputRequest],
) -> None:
    conn, request = real_inputs
    assert request.assumption_review is not None
    cutoff = request.assumption_review.reviewed_at
    inputs, receipt = prepare_meli_inputs(
        conn,
        request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=cutoff,
    )
    output = model_output(inputs)
    vps = output["vps"]
    equity_value = output["equity_value"]
    assert isinstance(vps, (float, int)) and isinstance(equity_value, (float, int))
    db = _persist(
        conn,
        ticker="MELI",
        npv=vps,
        total=equity_value,
        snapshot={
            "model": "meli_platform_sotp",
            "effective_model_inputs": inputs,
            "value_per_share": output["vps"],
            "equity_value_m": output["equity_value"],
            "operating_ev_m": output["operating_ev"],
            "credit_equity_value_m": output["credit_equity_value"],
        },
        provenance={"model_input_receipt": receipt.model_dump(mode="json")},
    )
    readiness = load_valuation_readiness(conn, "MELI", as_of=NOW)
    assert readiness.financial_input_completeness == "verified", readiness.reason_codes
    assert readiness.reason_codes == ("scenario_acceptance_unverified",)
    leg = reward_legs(db, ["MELI"], NOW.date())["MELI"]
    assert leg.expected_return is None
    assert leg.confidence_reason == "scenario_acceptance_unverified"
    unavailable: dict[str, str] = {}
    assert valuation_factor(db, ["MELI"], as_of=NOW, unavailable=unavailable) == {}
    assert unavailable == {"MELI": "scenario_acceptance_unverified"}
    conn.execute("UPDATE dcf_runs SET npv=npv+1000")
    conn.commit()
    assert "persisted_model_output_replay_mismatch" in (
        reward_legs(db, ["MELI"], NOW.date())["MELI"].confidence_reason or ""
    )


def test_exact_run_and_cutoff_share_a_read_only_snapshot(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = migrated_db(tmp_path / "risk-snapshot.db")
    with sqlite3.connect(db) as writer:
        _persist(writer)
        writer.execute("PRAGMA journal_mode=WAL")
    evaluated: list[datetime] = []

    def assess(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        assert conn.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE dcf_runs SET npv_per_share=888")
        row = latest_dcf_row(conn, ticker)
        assert row is not None and row.npv_per_share == 150
        with sqlite3.connect(db) as writer:
            writer.execute("UPDATE dcf_runs SET npv_per_share=999")
        receipt = load_valuation_readiness(conn, ticker, as_of=as_of)
        same_row = latest_dcf_row(conn, ticker)
        assert same_row is not None and same_row.npv_per_share == 150
        assert receipt.run_id == same_row.id
        assert not receipt.ready  # Actual readiness is retained; this is no acceptance seam.
        evaluated.append(as_of)
        return receipt

    monkeypatch.setattr("risk_reward.load_valuation_readiness", assess)
    local_cutoff = NOW.astimezone(timezone(timedelta(hours=-7)))
    leg = reward_legs(db, ["META"], NOW.date(), as_of=local_cutoff)["META"]
    assert leg.expected_return is None
    assert evaluated == [NOW]
    assert evaluated[0].tzinfo is UTC
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT npv_per_share FROM dcf_runs").fetchone()[0] == 999


def test_missing_database_has_a_precise_reason(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    leg = reward_legs(absent, ["META"], NOW.date())["META"]
    assert leg.expected_return is None
    assert leg.confidence_reason == "dcf_database_missing"
    assert not absent.exists()


def test_naive_cutoff_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        reward_legs(tmp_path / "absent.db", ["META"], NOW.date(), as_of=NOW.replace(tzinfo=None))
