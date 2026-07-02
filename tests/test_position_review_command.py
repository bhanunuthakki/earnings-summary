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
from advisor.position_review import PreAnalysis, mechanical_read, render_pre_analysis_chat
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
