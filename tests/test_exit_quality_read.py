"""advisor.exit_quality — the Phase-2 tracker exit-quality contract-formalization
layer (docs/design/owner_context_federation.md §3.2 "Tracker realized-gain /
exit-quality payloads"). Typed, units-explicit narrowing of the tracker
client's ``ExitQuality`` payload to ONE ticker, plus the one-line render for
the capacity block.

Covers: found / not-found / tracker-unavailable / never-raises degrade, and
the render's signed-regret phrasing + partial-exit flag.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from advisor.exit_quality import (  # noqa: E402
    TickerExitQuality,
    read_ticker_exit_quality,
    render_exit_quality_note,
)
from integrations.portfolio_tracker_client import (  # noqa: E402
    ExitQuality,
    ExitQualityRow,
)


def _payload(rows: list[ExitQualityRow]) -> ExitQuality:
    return ExitQuality(
        start_date="2025-06-10",
        end_date="2026-06-10",
        total_sold_proceeds=None,
        total_value_if_held=None,
        total_regret_vs_hold=None,
        total_spy_value_if_reinvested=None,
        total_exit_alpha_vs_spy=None,
        rows=rows,
    )


_META_ROW = ExitQualityRow(
    ticker="META",
    name="Meta Platforms",
    sold_shares=10.0,
    sold_proceeds=5000.0,
    avg_sell_price=500.0,
    price_now=600.0,
    value_if_held=6000.0,
    regret_vs_hold=1000.0,  # positive — selling cost the owner money
    spy_value_if_reinvested=5200.0,
    exit_alpha_vs_spy=-200.0,
    still_held=False,
)


def _fetch_meta_only(**kwargs: object) -> ExitQuality:
    return _payload([_META_ROW])


def _fetch_unavailable(**kwargs: object) -> ExitQuality | None:
    return None


def test_read_ticker_exit_quality_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "integrations.portfolio_tracker_client.fetch_exit_quality", _fetch_meta_only
    )
    eq = read_ticker_exit_quality("meta")  # lowercase — case-insensitive match
    assert eq is not None
    assert eq.ticker == "META"
    assert eq.sold_shares == pytest.approx(10.0)
    assert eq.sold_proceeds_usd == pytest.approx(5000.0)
    assert eq.value_if_held_usd == pytest.approx(6000.0)
    assert eq.regret_vs_hold_usd == pytest.approx(1000.0)
    assert eq.exit_alpha_vs_spy_usd == pytest.approx(-200.0)
    assert eq.still_held is False


def test_read_ticker_exit_quality_ticker_not_in_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "integrations.portfolio_tracker_client.fetch_exit_quality", _fetch_meta_only
    )
    assert read_ticker_exit_quality("RBRK") is None


def test_read_ticker_exit_quality_tracker_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "integrations.portfolio_tracker_client.fetch_exit_quality", _fetch_unavailable
    )
    assert read_ticker_exit_quality("META") is None


def test_read_ticker_exit_quality_never_raises_on_client_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**kw: object) -> ExitQuality:
        raise RuntimeError("tracker client blew up")

    monkeypatch.setattr("integrations.portfolio_tracker_client.fetch_exit_quality", _boom)
    assert read_ticker_exit_quality("META") is None


def test_ticker_exit_quality_model_units_are_absolute_usd() -> None:
    """Sanity check the contract itself: every dollar field is described as
    absolute USD in its Field description — never a percent/fraction."""
    for name in (
        "sold_proceeds_usd",
        "value_if_held_usd",
        "regret_vs_hold_usd",
        "exit_alpha_vs_spy_usd",
    ):
        field = TickerExitQuality.model_fields[name]
        assert "usd" in (field.description or "").lower()


# --------------------------------------------------------------------------- #
# render_exit_quality_note
# --------------------------------------------------------------------------- #


def _eq(**overrides: object) -> TickerExitQuality:
    base: dict[str, object] = {
        "ticker": "META",
        "sold_shares": 10.0,
        "sold_proceeds_usd": 5000.0,
        "value_if_held_usd": 6000.0,
        "regret_vs_hold_usd": 1000.0,
        "exit_alpha_vs_spy_usd": -200.0,
        "still_held": False,
    }
    base.update(overrides)
    return TickerExitQuality.model_validate(base)


def test_render_positive_regret_reads_as_cost() -> None:
    note = render_exit_quality_note(_eq(regret_vs_hold_usd=1000.0))
    assert "cost you" in note
    assert "$1,000" in note


def test_render_negative_regret_reads_as_beat_holding() -> None:
    note = render_exit_quality_note(_eq(regret_vs_hold_usd=-500.0))
    assert "beat holding" in note
    assert "$500" in note


def test_render_zero_regret_reads_as_matched() -> None:
    note = render_exit_quality_note(_eq(regret_vs_hold_usd=0.0))
    assert "matched holding" in note


def test_render_unknown_regret_when_none() -> None:
    note = render_exit_quality_note(_eq(regret_vs_hold_usd=None))
    assert "unknown" in note


def test_render_flags_partial_exit() -> None:
    note = render_exit_quality_note(_eq(still_held=True))
    assert "partial exit" in note
    note_full = render_exit_quality_note(_eq(still_held=False))
    assert "partial exit" not in note_full
