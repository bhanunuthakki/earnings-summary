"""Tests for the deterministic tax layer of the position-review service.

Pure and hermetic — lots, profiles, and views are built from in-memory
dataclasses; no DB, no network, no LLM. The date used everywhere is injected
(``today``) so nothing here can flake at a UTC midnight boundary.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from advisor.position_tax import (
    DEFAULT_TRIM_FRACTION,
    LotReconstruction,
    TaxProfile,
    build_position_tax_view,
    load_tax_profile,
    long_term_date,
    propose_trim_fraction,
    reconstruct_lots,
    unavailable_tax_view,
)
from integrations.portfolio_tracker_client import LivePosition, LiveTransaction, TaxLot

_TODAY = date(2026, 7, 2)  # injected everywhere; NOT read from the clock


def _txn(
    day: str,
    kind: str,
    qty: float,
    *,
    price: float | None = None,
    amount: float | None = None,
    fees: float | None = None,
    account_id: int = 6,
    account_name: str = "Fidelity Brokerage",
    ticker: str = "NU",
) -> LiveTransaction:
    return LiveTransaction(
        date=day,
        ticker=ticker,
        name=None,
        type=kind,
        subtype=None,
        quantity=qty,
        amount=amount,
        account_name=account_name,
        account_id=account_id,
        price=price,
        fees=fees,
    )


def _position(
    *,
    quantity: float,
    market_value: float,
    cost_basis: float | None,
    accounts: list[TaxLot],
    unrealized: float | None = None,
) -> LivePosition:
    return LivePosition(
        ticker="NU",
        name="Nu Holdings",
        quantity=quantity,
        market_value=market_value,
        cost_basis=cost_basis,
        unrealized_pnl=unrealized,
        percent_of_portfolio=None,
        accounts=accounts,
    )


# --------------------------------------------------------------------------- #
# TaxProfile + loader
# --------------------------------------------------------------------------- #


def test_default_profile_owner_approved_rates() -> None:
    profile = load_tax_profile(Path("Z:/nonexistent"))
    assert profile.source == "default"
    assert profile.st_rate == pytest.approx(0.32 + 0.038 + 0.093)
    assert profile.lt_rate == pytest.approx(0.15 + 0.038 + 0.093)
    assert "$450-500k MAGI" in profile.magi_assumption
    # The footnote makes the assumption auditable in every rendered block.
    assert profile.magi_assumption in profile.footnote()
    assert "tax_profile.json" in profile.footnote()


def test_profile_override_is_partial(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tax_profile.json").write_text(
        '{"federal_ordinary_rate": 0.35, "magi_assumption": "2027 MFJ, $600k MAGI, CA"}',
        encoding="utf-8",
    )
    profile = load_tax_profile(tmp_path)
    assert profile.federal_ordinary_rate == pytest.approx(0.35)
    assert profile.state_rate == pytest.approx(0.093)  # untouched default
    assert profile.magi_assumption == "2027 MFJ, $600k MAGI, CA"
    assert profile.source == "data/tax_profile.json"


def test_profile_malformed_file_degrades_to_default(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tax_profile.json").write_text("{not json", encoding="utf-8")
    profile = load_tax_profile(tmp_path)
    assert profile.st_rate == pytest.approx(0.451)
    assert "unreadable" in profile.source


# --------------------------------------------------------------------------- #
# Holding-period arithmetic
# --------------------------------------------------------------------------- #


def test_long_term_date_is_day_after_anniversary() -> None:
    assert long_term_date(date(2025, 7, 1)) == date(2026, 7, 2)
    # Feb 29 rolls to Mar 1, then +1 day.
    assert long_term_date(date(2024, 2, 29)) == date(2025, 3, 2)


def test_term_boundary_exactly_one_year_is_still_short_term() -> None:
    # Acquired exactly one year ago today: LT starts TOMORROW.
    assert long_term_date(date(2025, 7, 2)) > _TODAY
    assert long_term_date(date(2025, 7, 1)) <= _TODAY


# --------------------------------------------------------------------------- #
# FIFO lot reconstruction
# --------------------------------------------------------------------------- #

_TAXABLE = {6: "Fidelity Brokerage"}


def test_reconstruct_multi_lot_with_partial_sell() -> None:
    """Two buys, a partial sell washing through the first lot FIFO."""
    txns = [
        _txn("2025-01-10", "buy", 100, price=50.0),
        _txn("2025-06-01", "sell", 30, price=90.0),
        _txn("2025-09-01", "buy", 50, price=80.0, fees=5.0),
    ]
    recon = reconstruct_lots(
        txns, ticker="NU", taxable_accounts=_TAXABLE, live_quantities={6: 120.0}, today=_TODAY
    )
    assert not recon.approximate, recon.reasons
    assert [(lot.acquired, lot.quantity) for lot in recon.lots] == [
        (date(2025, 1, 10), 70.0),
        (date(2025, 9, 1), 50.0),
    ]
    # Buy fees roll into basis: (50*80 + 5)/50.
    assert recon.lots[1].cost_per_share == pytest.approx(80.10)


def test_reconstruct_sell_spanning_lots() -> None:
    txns = [
        _txn("2025-01-10", "buy", 100, price=50.0),
        _txn("2025-02-10", "buy", 100, price=60.0),
        _txn("2025-06-01", "sell", 150, price=90.0),
    ]
    recon = reconstruct_lots(
        txns, ticker="NU", taxable_accounts=_TAXABLE, live_quantities={6: 50.0}, today=_TODAY
    )
    assert not recon.approximate
    assert [(lot.acquired, lot.quantity) for lot in recon.lots] == [(date(2025, 2, 10), 50.0)]


def test_reconstruct_price_falls_back_to_amount_over_qty() -> None:
    txns = [_txn("2025-01-10", "buy", 100, amount=-5000.0)]
    recon = reconstruct_lots(
        txns, ticker="NU", taxable_accounts=_TAXABLE, live_quantities={6: 100.0}, today=_TODAY
    )
    assert not recon.approximate
    assert recon.lots[0].cost_per_share == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("txns", "live_qty", "reason_fragment"),
    [
        # Sell exceeding open lots: position predates the history window.
        ([_txn("2025-06-01", "sell", 30, price=90.0)], 0.0, "exceeds reconstructed"),
        # Share transfer carries no basis.
        (
            [_txn("2025-01-10", "buy", 100, price=50.0), _txn("2025-03-01", "transfer", 20)],
            120.0,
            "transfer",
        ),
        # Reconstruction disagrees with the live count.
        ([_txn("2025-01-10", "buy", 100, price=50.0)], 130.0, "reconstructed 100 vs live 130"),
        # A buy whose price can't be derived.
        ([_txn("2025-01-10", "buy", 100)], 100.0, "no derivable price"),
        # Unparseable fill date.
        (
            [_txn("2025-01-10", "buy", 100, price=50.0), _txn("garbage", "sell", 10, price=60.0)],
            100.0,
            "unparseable fill date",
        ),
    ],
)
def test_reconstruct_degrades_honestly(
    txns: list[LiveTransaction], live_qty: float, reason_fragment: str
) -> None:
    recon = reconstruct_lots(
        txns, ticker="NU", taxable_accounts=_TAXABLE, live_quantities={6: live_qty}, today=_TODAY
    )
    assert recon.approximate
    assert any(reason_fragment in r for r in recon.reasons), recon.reasons


def test_reconstruct_truncated_history_is_approximate() -> None:
    txns = [_txn("2025-01-10", "buy", 100, price=50.0)]
    recon = reconstruct_lots(
        txns,
        ticker="NU",
        taxable_accounts=_TAXABLE,
        live_quantities={6: 100.0},
        history_truncated=True,
        today=_TODAY,
    )
    assert recon.approximate
    assert any("truncated" in r for r in recon.reasons)


def test_recent_buys_span_all_accounts_for_wash_window() -> None:
    """The wash window watches buys in EVERY account (IRAs included), not just
    the taxable ones being reconstructed."""
    txns = [
        _txn("2025-01-10", "buy", 100, price=50.0),
        _txn("2026-06-20", "buy", 10, price=120.0, account_id=5, account_name="RH Roth IRA"),
    ]
    recon = reconstruct_lots(
        txns, ticker="NU", taxable_accounts=_TAXABLE, live_quantities={6: 100.0}, today=_TODAY
    )
    assert recon.recent_buy_dates == (date(2026, 6, 20),)
    assert not recon.approximate  # the Roth buy isn't part of taxable lot math


# --------------------------------------------------------------------------- #
# Trim proposal sizing
# --------------------------------------------------------------------------- #


def test_propose_trim_fraction_ladder() -> None:
    # Above the target band -> trim to the band top (unchanged by P0.2).
    frac, why = propose_trim_fraction(
        weight_pct=10.0,
        target_band=(3.2, 4.8),
        weight_vs_band="above_band",
        zone="meaningful",
    )
    assert frac == pytest.approx((10.0 - 4.8) / 10.0)
    assert "top of band" in why
    # No band, but the zone is concentrated-or-higher and the weight clears
    # the 12% trim-assessment threshold -> comparison-framed trim TO that
    # threshold, not the old flat 8% concentration bar.
    frac, why = propose_trim_fraction(
        weight_pct=13.4,
        target_band=None,
        weight_vs_band="no_band",
        zone="concentrated",
    )
    assert frac == pytest.approx((13.4 - 12.0) / 13.4)
    assert "12% trim-assessment threshold" in why
    assert "soft zone, not a required target" in why
    # No band, zone below "concentrated" (or weight under the threshold) ->
    # the illustrative default tranche.
    frac, why = propose_trim_fraction(
        weight_pct=5.0,
        target_band=None,
        weight_vs_band="no_band",
        zone="ordinary",
    )
    assert frac == pytest.approx(DEFAULT_TRIM_FRACTION)
    assert "illustrative" in why
    # zone=None (weight unknown) also falls through to the default tranche.
    frac, why = propose_trim_fraction(
        weight_pct=None,
        target_band=None,
        weight_vs_band="no_band",
        zone=None,
    )
    assert frac == pytest.approx(DEFAULT_TRIM_FRACTION)
    assert "illustrative" in why


# --------------------------------------------------------------------------- #
# build_position_tax_view — exact (lot-validated) mode
# --------------------------------------------------------------------------- #

_PROFILE = TaxProfile(
    federal_ordinary_rate=0.32,
    federal_lt_rate=0.15,
    niit_rate=0.038,
    state_rate=0.093,
    magi_assumption="2026 MFJ, $450-500k MAGI, CA resident",
)
_ST = _PROFILE.st_rate  # 0.451
_LT = _PROFILE.lt_rate  # 0.281

# Open lots after history: 70 @ $50 (2025-01-10, LT) + 50 @ $80 (2025-09-01, ST).
_HISTORY = [
    _txn("2025-01-10", "buy", 100, price=50.0),
    _txn("2025-06-01", "sell", 30, price=90.0),
    _txn("2025-09-01", "buy", 50, price=80.0),
]
_TAXABLE_POS = _position(
    quantity=120.0,
    market_value=12_000.0,
    cost_basis=7_500.0,
    accounts=[TaxLot(6, "Fidelity Brokerage", 120.0, 12_000.0, "taxable", 7_500.0)],
)


def test_exact_view_splits_st_lt_and_prices_the_trim() -> None:
    view = build_position_tax_view(
        _TAXABLE_POS,
        _HISTORY,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.5,
        trim_rationale="illustrative 50%",
        today=_TODAY,
    )
    assert view.available and not view.approximate
    assert view.st_unrealized_usd == pytest.approx((100 - 80) * 50)  # $1,000 ST
    assert view.lt_unrealized_usd == pytest.approx((100 - 50) * 70)  # $3,500 LT
    trim = view.trim
    assert trim is not None
    # 60 shares FIFO -> all from the LT $50 lot.
    assert trim.trim_shares == pytest.approx(60.0)
    assert trim.st_gain_usd == pytest.approx(0.0)
    assert trim.lt_gain_usd == pytest.approx(3_000.0)
    assert trim.tax_low_usd == trim.tax_high_usd == pytest.approx(3_000.0 * _LT)
    assert trim.days_until_lt is None  # no ST lots consumed -> nothing to wait for
    assert view.footnote == _PROFILE.footnote()


def test_exact_trim_crossing_into_st_lot_reports_wait_savings() -> None:
    view = build_position_tax_view(
        _TAXABLE_POS,
        _HISTORY,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.75,  # 90 shares: 70 LT + 20 from the ST lot
        trim_rationale="to top of band",
        today=_TODAY,
    )
    trim = view.trim
    assert trim is not None
    assert trim.st_gain_usd == pytest.approx((100 - 80) * 20)  # $400
    assert trim.lt_gain_usd == pytest.approx((100 - 50) * 70)  # $3,500
    assert trim.tax_low_usd == pytest.approx(400 * _ST + 3_500 * _LT)
    # The ST lot (2025-09-01) turns LT on 2026-09-02 -> 62 days from 2026-07-02,
    # saving the ST-LT spread on the $400 gain.
    assert trim.days_until_lt == 62
    assert trim.wait_savings_usd == pytest.approx(400 * (_ST - _LT))


def test_exact_trim_flags_wash_sale_risk_on_loss_lot_with_recent_buy() -> None:
    history = [
        _txn("2025-01-10", "buy", 100, price=50.0),
        _txn("2026-06-20", "buy", 20, price=120.0),  # 12 days ago, now underwater
    ]
    pos = _position(
        quantity=120.0,
        market_value=12_000.0,
        cost_basis=7_400.0,
        accounts=[TaxLot(6, "Fidelity Brokerage", 120.0, 12_000.0, "taxable", 7_400.0)],
    )
    view = build_position_tax_view(
        pos,
        history,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=1.0,  # consume everything, incl. the $120 loss lot
        trim_rationale="full exit",
        today=_TODAY,
    )
    assert view.trim is not None
    assert view.trim.wash_sale_risk is True


def test_sheltered_note_when_trim_fits_in_roth() -> None:
    pos = _position(
        quantity=120.0,
        market_value=12_000.0,
        cost_basis=7_500.0,
        accounts=[
            TaxLot(6, "Fidelity Brokerage", 60.0, 6_000.0, "taxable", 3_750.0),
            TaxLot(5, "RH Roth IRA", 60.0, 6_000.0, "tax_free", 3_750.0),
        ],
    )
    history = [_txn("2025-01-10", "buy", 60, price=62.5)]
    view = build_position_tax_view(
        pos,
        history,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.25,  # 30 shares; the Roth holds 60
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.sheltered_note is not None
    assert "$0 tax" in view.sheltered_note
    assert view.taxable_pct_of_position == pytest.approx(50.0)


def test_fully_sheltered_position_has_no_tax_math() -> None:
    pos = _position(
        quantity=100.0,
        market_value=10_000.0,
        cost_basis=6_000.0,
        accounts=[TaxLot(5, "RH Roth IRA", 100.0, 10_000.0, "tax_free", 6_000.0)],
    )
    view = build_position_tax_view(
        pos,
        [],
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.2,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.available and view.trim is None
    assert view.taxable_pct_of_position == pytest.approx(0.0)
    assert view.sheltered_note is not None and "$0 tax" in view.sheltered_note


# --------------------------------------------------------------------------- #
# build_position_tax_view — honest degrades
# --------------------------------------------------------------------------- #


def test_history_unavailable_degrades_to_position_level_range() -> None:
    """Tracker history fetch failed: average-basis math, term unknown, tax is
    an [all-LT, all-ST] range — and the view says why."""
    view = build_position_tax_view(
        _TAXABLE_POS,
        None,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.5,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.approximate
    assert any("history unavailable" in r for r in view.approx_reasons)
    assert view.st_unrealized_usd is None and view.lt_unrealized_usd is None
    trim = view.trim
    assert trim is not None
    # 60 of 120 shares at $100 vs $7,500 basis: realized gain = (12000-7500)/2.
    assert trim.realized_gain_usd == pytest.approx(2_250.0)
    assert trim.tax_low_usd == pytest.approx(2_250.0 * _LT)
    assert trim.tax_high_usd == pytest.approx(2_250.0 * _ST)
    assert trim.st_gain_usd is None and trim.days_until_lt is None


def test_failed_reconstruction_degrades_with_reasons() -> None:
    history = [_txn("2025-01-10", "buy", 100, price=50.0)]  # live says 120 -> mismatch
    view = build_position_tax_view(
        _TAXABLE_POS,
        history,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.5,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.approximate
    assert any("reconstructed" in r for r in view.approx_reasons)
    assert view.trim is not None and view.trim.st_gain_usd is None


def test_no_basis_anywhere_yields_no_trim_estimate() -> None:
    pos = _position(
        quantity=120.0,
        market_value=12_000.0,
        cost_basis=None,
        accounts=[TaxLot(6, "Fidelity Brokerage", 120.0, 12_000.0, "taxable", None)],
    )
    view = build_position_tax_view(
        pos,
        None,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.5,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.approximate and view.trim is None
    assert any("no cost basis" in r for r in view.approx_reasons)


def test_unknown_treatment_counts_as_taxable_but_flags() -> None:
    pos = _position(
        quantity=100.0,
        market_value=10_000.0,
        cost_basis=6_000.0,
        accounts=[TaxLot(9, "Mystery Acct", 100.0, 10_000.0, "unknown", 6_000.0)],
    )
    view = build_position_tax_view(
        pos,
        None,
        profile=_PROFILE,
        at_price=100.0,
        trim_fraction=0.2,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.approximate
    assert any("treatment unknown" in r for r in view.approx_reasons)
    assert view.taxable_mv_usd == pytest.approx(10_000.0)


def test_unavailable_view_never_crashes_renderers() -> None:
    view = unavailable_tax_view("tracker offline")
    assert view.available is False and view.reason == "tracker offline"
    assert view.trim is None and view.footnote == ""


def test_zero_quantity_position_is_unavailable() -> None:
    pos = _position(quantity=0.0, market_value=0.0, cost_basis=None, accounts=[])
    view = build_position_tax_view(
        pos,
        [],
        profile=_PROFILE,
        at_price=None,
        trim_fraction=0.2,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.available is False


def test_no_price_available_keeps_buckets_but_no_gains(monkeypatch: pytest.MonkeyPatch) -> None:
    pos = _position(
        quantity=100.0,
        market_value=0.0,  # no MV -> no implied price
        cost_basis=6_000.0,
        accounts=[TaxLot(6, "Fidelity Brokerage", 100.0, None, "taxable", 6_000.0)],
    )
    view = build_position_tax_view(
        pos,
        [],
        profile=_PROFILE,
        at_price=None,
        trim_fraction=0.2,
        trim_rationale="illustrative",
        today=_TODAY,
    )
    assert view.available and view.approximate
    assert view.eval_price is None and view.trim is None
    assert any("no price" in r for r in view.approx_reasons)


def test_lot_reconstruction_dataclass_shape() -> None:
    """LotReconstruction defaults: recent buys empty, reasons deduped."""
    recon = LotReconstruction(ticker="NU", lots=(), approximate=False, reasons=())
    assert recon.recent_buy_dates == ()
