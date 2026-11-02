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
    parse_review_command,
    render_pre_analysis_chat,
    render_tax_lines,
    review_reply_text,
)
from advisor.position_tax import PositionTaxView, TrimTaxEstimate, unavailable_tax_view
from ask.engine import COMMAND_PREFIXES, run_chat_command
from capture.matcher import DISTINCTIVE_ALIASES, RosterIndex, build_roster_index
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
    has_decision_note=True,
    is_index_instrument=False,
)


def _pre(**overrides: object) -> PreAnalysis:
    return replace(_PRE_DEFAULT, **overrides)


# --------------------------------------------------------------------------- #
# mechanical_read — the deterministic verdict hint (mirrors the guard)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# render_pre_analysis_chat
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# review_reply_text — deterministic Markdown dispatch
# --------------------------------------------------------------------------- #


def test_review_reply_text_default_stays_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "portfolio.sqlite"
    database.touch()

    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = review_reply_text(Path("."), "/review RBRK at $82", db_path=database)
    assert "**RBRK**" in reply


# --------------------------------------------------------------------------- #
# name/alias resolution — "/review Rubrik" must review RBRK, not "RUBRIK"
# --------------------------------------------------------------------------- #


def _roster() -> RosterIndex:
    """A roster with the tracked symbols + the distinctive-name alias seed —
    the same shape ``load_roster`` builds in prod."""
    return build_roster_index(symbols=["RBRK", "NU", "NVO"], phrases=DISTINCTIVE_ALIASES)


def _fake_roster(_repo_root: Path) -> RosterIndex:
    """A single-name roster for the loader seam (replaces
    ``_load_review_roster`` in monkeypatch tests)."""
    return build_roster_index(symbols=["RBRK"], phrases=DISTINCTIVE_ALIASES)


def test_parse_resolves_company_name_to_ticker() -> None:
    roster = _roster()
    assert parse_review_command("/review Rubrik", roster=roster) == ("RBRK", None)
    # case-insensitive, and the "at $X" clause still parses alongside the name
    assert parse_review_command("/review rubrik at $70", roster=roster) == ("RBRK", 70.0)
    assert parse_review_command("/review Nubank", roster=roster) == ("NU", None)


def test_parse_resolves_multiword_company_name() -> None:
    roster = build_roster_index(symbols=["NVO"], phrases=DISTINCTIVE_ALIASES)
    # a two-word name survives the "at $X" strip and resolves as a phrase
    assert parse_review_command("/review Novo Nordisk at $120", roster=roster) == ("NVO", 120.0)


def test_parse_bare_symbol_and_symbol_alias_still_resolve() -> None:
    roster = _roster()
    assert parse_review_command("/review RBRK", roster=roster) == ("RBRK", None)
    assert parse_review_command("/review $RBRK at $70", roster=roster) == ("RBRK", 70.0)
    # symbol-alias fallback still applies even when the roster has no name match
    assert parse_review_command("/review googl", roster=roster) == ("GOOG", None)


def test_parse_without_roster_uses_token_as_typed() -> None:
    # No roster (resolution unavailable) → the token is used as-typed, uppercased
    # — the pre-resolution behavior, so an un-rostered name still reviews.
    assert parse_review_command("/review RBRK at $82") == ("RBRK", 82.0)
    assert parse_review_command("/review Rubrik") == ("RUBRIK", None)


def test_review_reply_text_resolves_company_name_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complete chat command: '/review Rubrik' must build the review for
    RBRK, not the literal 'RUBRIK'."""
    seen: dict[str, object] = {}

    def _fake(_repo: object, ticker: str, **_k: object) -> PreAnalysis:
        seen["ticker"] = ticker
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    monkeypatch.setattr(
        pr,
        "_load_review_roster",
        _fake_roster,
    )
    database = tmp_path / "portfolio.sqlite"
    database.touch()
    out = review_reply_text(Path("."), "/review Rubrik", db_path=database)
    assert seen["ticker"] == "RBRK"
    assert "RBRK" in out


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
    assert "- Mechanical read:" not in out
    assert "Full calibrated verdict" not in out


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


def test_review_command_dispatches_and_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "portfolio.sqlite"
    database.touch()

    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = run_chat_command(
        Path("."), "/review RBRK at $82", cast("Registry", None), db_path=database
    )
    assert reply is not None
    assert "**RBRK**" in reply and "Mechanical read:" not in reply


def test_review_command_parses_at_price_through_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    database = tmp_path / "portfolio.sqlite"
    database.touch()

    def _fake(*_a: object, **kwargs: object) -> PreAnalysis:
        seen["at_price"] = kwargs.get("at_price")
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    run_chat_command(Path("."), "/review FLKR at $70", cast("Registry", None), db_path=database)
    assert seen["at_price"] == 70.0
    run_chat_command(Path("."), "/review RBRK", cast("Registry", None), db_path=database)
    assert seen["at_price"] is None


# --------------------------------------------------------------------------- #
# /review background full-verdict job (Bug 2: production caller wiring)
# --------------------------------------------------------------------------- #
