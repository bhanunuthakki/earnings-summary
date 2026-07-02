"""Tests for the Slice 3 /review chat command + deterministic rendering.

The command is the instant, no-LLM surface: it routes through the ask engine's
command path and returns the grounded pre-analysis + a mechanical trim/hold read.
Tests cover the mechanical-read branches, the chat rendering, and the command
dispatch + at-price parsing (through the public run_chat_command, so no private
symbols are imported). No LLM; build_pre_analysis is stubbed where a DB would be.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from advisor import position_review as pr
from advisor.position_review import (
    PreAnalysis,
    mechanical_read,
    render_pre_analysis_chat,
    render_tax_lines,
)
from advisor.position_tax import PositionTaxView, TrimTaxEstimate, unavailable_tax_view
from ask.commands import COMMAND_PREFIXES, run_chat_command
from dispatch_registry import Registry

_PRE_DEFAULT = PreAnalysis(
    ticker="RBRK",
    weight_pct=10.0,
    weight_source="materialized",
    market_value_usd=None,
    unrealized_pnl_usd=None,
    target_weight_pct=None,
    target_band=None,
    weight_vs_band="no_band",
    conviction_1_5=None,
    concentration_flag=False,
    thesis_present=True,
    verdict_label="Intact",
    key_driver="Net new subscription ARR cadence",
    break_rule_status="intact",
    tripped_rules=(),
    dcf_gap_pct=-10.0,
    npv_per_share=91.09,
    dcf_live_price=80.35,
    dcf_date="2026-06-15",
    at_price=None,
    mos_bar=0.30,
    valuation_verdict="fair",
    conviction_encoded=True,
    has_stance=True,
    has_decision_note=True,
    is_index_instrument=False,
)


def _pre(**overrides: object) -> PreAnalysis:
    return replace(_PRE_DEFAULT, **overrides)


# --------------------------------------------------------------------------- #
# mechanical_read — the deterministic verdict hint (mirrors the guard)
# --------------------------------------------------------------------------- #


def test_mechanical_read_hold_on_intact_fair_non_oversized() -> None:
    assert mechanical_read(_pre()).startswith("HOLD")


def test_mechanical_read_flags_breach() -> None:
    assert "BREACHED" in mechanical_read(_pre(break_rule_status="breach"))


def test_mechanical_read_flags_overvalued() -> None:
    assert "Over-valued" in mechanical_read(_pre(valuation_verdict="sell"))


def test_mechanical_read_flags_oversized_concentration() -> None:
    assert "OVERSIZED" in mechanical_read(_pre(concentration_flag=True))


def test_mechanical_read_encode_first_for_index() -> None:
    read = mechanical_read(_pre(thesis_present=False, is_index_instrument=True))
    assert "encode" in read.lower() and "sleeve" in read.lower()


# --------------------------------------------------------------------------- #
# render_pre_analysis_chat
# --------------------------------------------------------------------------- #


def test_render_chat_contains_grounded_facts_and_cli_pointer() -> None:
    out = render_pre_analysis_chat(_pre(at_price=82.0))
    assert "**RBRK**" in out
    assert "Mechanical read:" in out
    assert "ladder: fair" in out
    assert "review_position.py RBRK --at-price 82" in out


# --------------------------------------------------------------------------- #
# render_tax_lines — the tax block shared by chat, CLI, and memo
# --------------------------------------------------------------------------- #

_TAX_TRIM = TrimTaxEstimate(
    trim_fraction=0.2,
    trim_rationale="to top of band (4.8%)",
    trim_shares=100.0,
    trim_usd=30_000.0,
    taxable_shares_consumed=100.0,
    realized_gain_usd=15_000.0,
    st_gain_usd=4_000.0,
    lt_gain_usd=11_000.0,
    tax_low_usd=4_895.0,
    tax_high_usd=4_895.0,
    days_until_lt=62,
    wait_savings_usd=680.0,
    wash_sale_risk=True,
)
_TAX_VIEW = PositionTaxView(
    available=True,
    reason=None,
    approximate=False,
    approx_reasons=(),
    eval_price=300.0,
    taxable_mv_usd=90_000.0,
    taxable_pct_of_position=60.0,
    sheltered_mv_usd=60_000.0,
    sheltered_note="40% of the position sits in Roth/HSA — trim there first",
    st_unrealized_usd=12_000.0,
    lt_unrealized_usd=48_000.0,
    trim=_TAX_TRIM,
    footnote="Tax estimate assumes 2026 MFJ, $450-500k MAGI, CA resident.",
)


def test_render_tax_lines_exact_mode_is_dense_and_complete() -> None:
    text = "\n".join(render_tax_lines(_TAX_VIEW))
    assert "- Tax: taxable 60% of position" in text
    assert "embedded gain ST $12,000 / LT $48,000" in text
    assert "Proposed trim (to top of band (4.8%)): ~$30,000" in text
    assert "est. tax ~$4,895" in text
    assert "waiting 62d (ST→LT) saves ~$680" in text
    assert "wash-sale risk" in text
    assert "Placement: 40% of the position sits in Roth/HSA" in text
    # The MAGI assumption footnote keeps the estimate auditable.
    assert "$450-500k MAGI" in text


def test_render_tax_lines_unavailable_is_one_honest_line() -> None:
    lines = render_tax_lines(unavailable_tax_view("tracker offline (ConnectionError)"))
    assert lines == ["- Tax: unavailable (tracker offline (ConnectionError))"]


def test_render_tax_lines_approximate_shows_range_and_reasons() -> None:
    from dataclasses import replace as dc_replace

    view = dc_replace(
        _TAX_VIEW,
        approximate=True,
        approx_reasons=("transaction history unavailable from the tracker",),
        st_unrealized_usd=None,
        lt_unrealized_usd=None,
        trim=dc_replace(
            _TAX_TRIM,
            st_gain_usd=None,
            lt_gain_usd=None,
            tax_low_usd=1_200.0,
            tax_high_usd=2_000.0,
            days_until_lt=None,
            wait_savings_usd=None,
            wash_sale_risk=False,
        ),
    )
    text = "\n".join(render_tax_lines(view))
    assert "- Tax (approx):" in text
    assert "term split unknown" in text
    assert "est. tax ~$1,200-$2,000 (term unknown)" in text
    assert "Tax approx because: transaction history unavailable" in text
    assert "waiting" not in text


def test_render_tax_lines_none_is_empty_and_chat_still_renders() -> None:
    assert render_tax_lines(None) == []
    assert "Tax" not in render_pre_analysis_chat(_pre())  # pre-tax-stage object


def test_render_chat_includes_tax_block_when_present() -> None:
    out = render_pre_analysis_chat(_pre(tax=_TAX_VIEW))
    assert "- Tax: taxable 60% of position" in out
    # Tax lines sit above the mechanical read, before the CLI pointer.
    assert out.index("- Tax:") < out.index("- Mechanical read:")


# --------------------------------------------------------------------------- #
# command routing + dispatch (public run_chat_command)
# --------------------------------------------------------------------------- #


def test_review_is_a_routed_command_prefix() -> None:
    assert "/review" in COMMAND_PREFIXES
    # mirrors ask.engine.route_turn's exact check
    assert "/review RBRK at $70".lower().startswith(COMMAND_PREFIXES)


def test_review_command_usage_without_ticker() -> None:
    reply = run_chat_command(Path("."), "/review", cast("Registry", None))
    assert reply is not None and "Usage:" in reply


def test_review_command_dispatches_and_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = run_chat_command(Path("."), "/review RBRK at $82", cast("Registry", None))
    assert reply is not None
    assert "**RBRK**" in reply and "Mechanical read:" in reply


def test_review_command_parses_at_price_through_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake(*_a: object, **kwargs: object) -> PreAnalysis:
        seen["at_price"] = kwargs.get("at_price")
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    run_chat_command(Path("."), "/review FLKR at $70", cast("Registry", None))
    assert seen["at_price"] == 70.0
    run_chat_command(Path("."), "/review RBRK", cast("Registry", None))
    assert seen["at_price"] is None
