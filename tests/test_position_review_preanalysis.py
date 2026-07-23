"""Pure-core tests for the position-review pre-analysis (Slice 1).

These exercise the deterministic composition + the valuation ladder without any
DB or network — the I/O orchestrator (`build_pre_analysis`) is covered by the
CLI smoke path against real data, not here. The RBRK / FLKR / breach cases are
the design's worked examples encoded as assertions.
"""

from __future__ import annotations

from advisor.position_review import (
    SELL_OVER_UNDER,
    TRIM_OVER_UNDER,
    assemble_pre_analysis,
    classify_valuation,
    classify_weight_band,
    summarize_verdict,
)

# --------------------------------------------------------------------------- #
# classify_valuation — the trim/sell ladder
# --------------------------------------------------------------------------- #


def test_classify_valuation_ladder() -> None:
    assert classify_valuation(None, 0.30) == "n/a"  # no DCF on file
    assert classify_valuation(0.25, 0.30) == "sell"  # > 0.20
    assert classify_valuation(0.15, 0.30) == "trim"  # > 0.10
    assert classify_valuation(0.05, 0.30) == "fair"  # in between
    assert classify_valuation(-0.40, 0.30) == "buy"  # < -mos_bar
    assert classify_valuation(-0.10, 0.30) == "fair"  # cheap but not initiation-grade


def test_classify_valuation_falls_back_to_default_mos_bar() -> None:
    # No holding mos_bar -> DEFAULT_MOS_BAR (0.30).
    assert classify_valuation(-0.40, None) == "buy"
    assert classify_valuation(-0.25, None) == "fair"


def test_valuation_ladder_matches_dcf_valuation_docstring() -> None:
    """Guard: our ladder constants + the at-price recompute must not drift from
    the canonical `dcf.valuation.over_under_pct` (whose docstring is the ladder's
    source). Asserts the real recompute path, not a private helper."""
    from dcf.valuation import over_under_pct

    assert TRIM_OVER_UNDER == 0.10
    assert SELL_OVER_UNDER == 0.20
    # assemble's at-price recompute must equal the canonical over/under formula.
    pre = assemble_pre_analysis(
        "T",
        weight_pct=None,
        weight_source="unknown",
        market_value_usd=None,
        unrealized_pnl_usd=None,
        target_weight_pct=None,
        conviction_1_5=None,
        break_rule_status="intact",
        tripped_rules=(),
        holdings_json=None,
        dcf_tuple=(None, 100.0, None, None),
        has_stance=False,
        has_decision_note=False,
        is_index_instrument=False,
        at_price=110.0,
    )
    assert pre.dcf_gap_pct == over_under_pct(110.0, 100.0) * 100.0


# --------------------------------------------------------------------------- #
# classify_weight_band
# --------------------------------------------------------------------------- #


def test_classify_weight_band() -> None:
    assert classify_weight_band(10.0, None) == ("no_band", None)
    label, band = classify_weight_band(5.0, 4.0)  # band 3.2-4.8; 5.0 above
    assert label == "above_band" and band == (3.2, 4.8)
    assert classify_weight_band(4.0, 4.0)[0] == "in_band"
    assert classify_weight_band(3.0, 4.0)[0] == "below_band"
    # target known but live weight missing -> no_band, band still returned
    assert classify_weight_band(None, 4.0) == ("no_band", (3.2, 4.8))


# --------------------------------------------------------------------------- #
# summarize_verdict
# --------------------------------------------------------------------------- #


def test_summarize_verdict_none_is_no_thesis() -> None:
    assert summarize_verdict(None) == ("no_thesis", ())


# --------------------------------------------------------------------------- #
# assemble_pre_analysis — the worked examples
# --------------------------------------------------------------------------- #


def test_rbrk_intact_undervalued_is_a_sizing_only_decision() -> None:
    """RBRK at ~10% weight, $82 vs DCF fair $91.09, thesis intact (soft WARN
    only): valuation gives no trim signal, so any trim is sizing, not thesis."""
    pre = assemble_pre_analysis(
        "RBRK",
        weight_pct=10.0,
        weight_source="live",
        market_value_usd=150_000.0,
        unrealized_pnl_usd=60_000.0,
        target_weight_pct=None,
        conviction_1_5=None,
        break_rule_status="warn",
        tripped_rules=("rbrk_growth_endurance (warn): ~76% < 85%",),
        holdings_json={
            "verdict": "Intact",
            "key_driver": "Net new subscription ARR cadence",
            "mos_bar": 0.30,
        },
        dcf_tuple=(-10.0, 91.09, 80.35, "2026-06-15"),
        has_stance=False,
        has_decision_note=True,
        is_index_instrument=False,
        at_price=82.0,
    )
    assert pre.thesis_present is True
    assert pre.verdict_label == "Intact"
    assert pre.break_rule_status == "warn"
    # 10% is the "meaningful" zone (PRD §7.2) — below the 12% trim-assessment
    # threshold, so concentration_flag (now "zone >= concentrated") is False.
    assert pre.concentration_flag is False
    assert pre.concentration_zone == "meaningful"
    assert pre.weight_vs_band == "no_band"  # no target recorded
    assert pre.conviction_encoded is True  # JSON + a decision note
    assert pre.valuation_verdict == "fair"  # ~-10% vs fair: no trim/sell, not initiation-grade
    assert pre.dcf_gap_pct is not None and pre.dcf_gap_pct < 0


def test_flkr_unencoded_conviction_degrades_cleanly() -> None:
    """FLKR: real 7.6% position, NO holdings JSON. thesis_present False,
    no DCF, and conviction_encoded stays False even with a decision note —
    the encode-first / instrument-aware path."""
    pre = assemble_pre_analysis(
        "FLKR",
        weight_pct=7.6,
        weight_source="materialized",
        market_value_usd=None,
        unrealized_pnl_usd=None,
        target_weight_pct=None,
        conviction_1_5=None,
        break_rule_status="no_thesis",
        tripped_rules=(),
        holdings_json=None,
        dcf_tuple=None,
        has_stance=False,
        has_decision_note=True,  # FLKR has a seeded "macro ETF, low conviction" note
        is_index_instrument=True,
        at_price=70.0,
    )
    assert pre.thesis_present is False
    assert pre.break_rule_status == "no_thesis"
    assert pre.valuation_verdict == "n/a"
    assert pre.conviction_encoded is False  # no JSON -> not encoded, note notwithstanding
    assert pre.is_index_instrument is True
    assert pre.concentration_flag is False  # 7.6% < 12% trim-assessment threshold
    assert pre.concentration_zone == "ordinary"  # 7.6% < 10%
    assert pre.verdict_label is None


def test_breach_overvalued_name_sells_and_flags_band() -> None:
    pre = assemble_pre_analysis(
        "XYZ",
        weight_pct=5.0,
        weight_source="live",
        market_value_usd=1.0,
        unrealized_pnl_usd=0.0,
        target_weight_pct=4.0,
        conviction_1_5=3,
        break_rule_status="breach",
        tripped_rules=("xyz_nrr_below_115 (breach): NRR 112 < 115",),
        holdings_json={"mos_bar": 0.30},
        dcf_tuple=(25.0, 100.0, 125.0, "2026-06-01"),
        has_stance=True,
        has_decision_note=False,
        is_index_instrument=False,
        at_price=None,
    )
    assert pre.valuation_verdict == "sell"  # +25% over fair
    assert pre.weight_vs_band == "above_band"  # 5.0 > 4.8
    assert pre.tripped_rules == ("xyz_nrr_below_115 (breach): NRR 112 < 115",)
    assert pre.conviction_encoded is True


def test_at_price_recompute_overrides_stored_gap() -> None:
    """Asking 'at $110' must recompute against NPV, not reuse the stored close's
    gap — 110 vs fair 91.09 is +20.8% -> sell, flipping the stored -10%."""
    pre = assemble_pre_analysis(
        "RBRK",
        weight_pct=10.0,
        weight_source="live",
        market_value_usd=None,
        unrealized_pnl_usd=None,
        target_weight_pct=None,
        conviction_1_5=None,
        break_rule_status="intact",
        tripped_rules=(),
        holdings_json={"mos_bar": 0.30},
        dcf_tuple=(-10.0, 91.09, 80.35, "2026-06-15"),
        has_stance=False,
        has_decision_note=False,
        is_index_instrument=False,
        at_price=110.0,
    )
    assert pre.valuation_verdict == "sell"
    assert pre.dcf_gap_pct is not None and pre.dcf_gap_pct > 20


# --------------------------------------------------------------------------- #
# Concentration zones (PRD §7.2, P0.2) — assemble_pre_analysis's threading
# --------------------------------------------------------------------------- #


def test_assemble_computes_zone_and_threads_entry_method() -> None:
    """A 13.4% weight is the "concentrated" zone (>= 12%, < 15%):
    concentration_flag flips True, zone_treatment renders, and the caller-
    supplied entry_method passes through untouched (assemble is pure — it
    never derives entry_method itself, only classifies the zone)."""
    pre = assemble_pre_analysis(
        "RBRK",
        weight_pct=13.4,
        weight_source="live",
        market_value_usd=None,
        unrealized_pnl_usd=None,
        target_weight_pct=None,
        conviction_1_5=None,
        break_rule_status="intact",
        tripped_rules=(),
        holdings_json=None,
        dcf_tuple=None,
        has_stance=False,
        has_decision_note=False,
        is_index_instrument=False,
        at_price=None,
        entry_method="intentional_add",
    )
    assert pre.concentration_zone == "concentrated"
    assert pre.concentration_flag is True
    assert pre.zone_treatment == "triggers an explicit hold-versus-trim assessment"
    assert pre.entry_method == "intentional_add"


def test_assemble_zone_and_entry_method_none_with_no_weight() -> None:
    pre = assemble_pre_analysis(
        "RBRK",
        weight_pct=None,
        weight_source="unknown",
        market_value_usd=None,
        unrealized_pnl_usd=None,
        target_weight_pct=None,
        conviction_1_5=None,
        break_rule_status="no_thesis",
        tripped_rules=(),
        holdings_json=None,
        dcf_tuple=None,
        has_stance=False,
        has_decision_note=False,
        is_index_instrument=False,
        at_price=None,
    )
    assert pre.concentration_zone is None
    assert pre.concentration_flag is False
    assert pre.zone_treatment is None
    assert pre.entry_method is None
