"""F-3: migrate the 13 evaluation-list holdings JSONs to two-tier break rules.

Eval candidates are pre-diligence. The framing is "2-3 disqualifiers that would
stop diligence cold," not full thesis-breakers. Universals are the same
catastrophic 3 (Revenue / FCF / OCF). Business-model rules are lighter — 3
per ticker, focused on the signals that matter for the eval grain.

For 6 names without a holdings JSON (CGEH, FIGR, NTDOY, NTRA, TEM, WGS) we
create minimal schema-v2 stubs — no thesis paragraph, no DCF params; just
the rule contract so the eval brief surfaces real disqualifiers. The user
fills in the rest if/when the candidate graduates to portfolio diligence.

Run once from the worktree root.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


# Identical universals to F-2; included verbatim so this script is self-contained.
UNIVERSAL_REVENUE_DECLINE = {
    "rule_id": "universal_revenue_decline",
    "kpi_name": "Revenue YoY Growth (USD)",
    "comparator": "lt",
    "threshold": 0,
    "unit": "percent",
    "consecutive_periods": 2,
    "narrative": (
        "Revenue declining YoY for 2 consecutive quarters — outright top-line "
        "contraction."
    ),
}

UNIVERSAL_FCF_MARGIN_COLLAPSE = {
    "rule_id": "universal_fcf_margin_collapse",
    "kpi_name": "FCF Margin (GAAP)",
    "comparator": "lt",
    "threshold": 5,
    "unit": "percent",
    "consecutive_periods": 2,
    "narrative": (
        "FCF margin below 5% for 2 consecutive Qs — cash conversion broken. "
        "(Universal floor; per-ticker rules below are stricter.)"
    ),
}

UNIVERSAL_OCF_DECLINE = {
    "rule_id": "universal_ocf_decline_yoy",
    "kpi_name": "Operating Cash Flow YoY Growth (USD)",
    "comparator": "lt",
    "threshold": -10,
    "unit": "percent",
    "consecutive_periods": 2,
    "narrative": "Operating cash flow declining >10% YoY for 2 consecutive Qs.",
}

STANDARD_UNIVERSALS = [
    UNIVERSAL_REVENUE_DECLINE,
    UNIVERSAL_FCF_MARGIN_COLLAPSE,
    UNIVERSAL_OCF_DECLINE,
]

# SOFI and similar transition-bank evaluation names: drop FCF/OCF universals.
BANK_UNIVERSALS = [UNIVERSAL_REVENUE_DECLINE]


# Per-ticker business_model_rules. Lighter set (3 each) — eval grain, not
# portfolio grain.
PER_TICKER_RULES: dict[str, list[dict]] = {
    "ABNB": [
        {
            "rule_id": "abnb_nights_booked_below_5",
            "kpi_name": "Nights & experiences booked YoY growth",
            "comparator": "lt", "threshold": 5, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Nights & experiences booked YoY < 5% for 2Q — core volume "
                "metric stalling; the take-rate compounding thesis fails."
            ),
        },
        {
            "rule_id": "abnb_fcf_margin_below_35",
            "kpi_name": "Free cash flow margin",
            "comparator": "lt", "threshold": 35, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 35% for 2Q — ABNB's structural floor is high-30s; "
                "sub-35 means take-rate or fixed-cost leverage is compressing."
            ),
        },
        {
            "rule_id": "abnb_adj_ebitda_margin_below_30",
            "kpi_name": "Adjusted EBITDA margin",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Adjusted EBITDA margin < 30% for 2Q — host-supply marketing "
                "or compliance costs eroding the structural margin."
            ),
        },
    ],
    "BHP": [
        # Commodity miner — most signals (realized price vs cost curve, net debt /
        # EBITDA) require KPI extractors we don't have. Lean on what's in DB.
        {
            "rule_id": "bhp_fcf_margin_below_15",
            "kpi_name": "FCF Margin (GAAP)",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 15% for 2Q — BHP runs 25%+ in cycle peaks; "
                "sub-15 means realized prices are near or below all-in "
                "sustaining cost."
            ),
        },
        {
            "rule_id": "bhp_capex_intensity_above_22",
            "kpi_name": "Capex / Revenue (GAAP)",
            "comparator": "gt", "threshold": 22, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Capex/Revenue > 22% for 2Q — capex bloat (Jansen potash + "
                "Escondida sustaining) not being absorbed into commodity "
                "price realization."
            ),
        },
        {
            "rule_id": "bhp_gross_margin_below_50",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 50, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 50% for 2Q — proxy for unit-cost-vs-price compression. "
                "BHP's iron ore + copper portfolio normally sustains high-50s "
                "to 60s; sub-50 is a unit-economics break."
            ),
        },
    ],
    "BKNG": [
        {
            "rule_id": "bkng_room_nights_below_5",
            "kpi_name": "Room nights booked YoY growth",
            "comparator": "lt", "threshold": 5, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Room nights YoY < 5% for 2Q — core volume metric stalling. "
                "BKNG runs 8-12% in normal cycles."
            ),
        },
        {
            "rule_id": "bkng_gross_bookings_fxn_below_6",
            "kpi_name": "Gross bookings YoY growth (FX-neutral)",
            "comparator": "lt", "threshold": 6, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Gross bookings FX-neutral < 6% for 2Q — take-rate × volume "
                "compounding thesis breaks; this is the cleanest top-line "
                "signal for an OTA."
            ),
        },
        {
            "rule_id": "bkng_fcf_margin_below_30",
            "kpi_name": "Free cash flow margin",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 30% for 2Q — BKNG's structural floor is 35%+. "
                "Sub-30 means marketing spend or merchant-model investments "
                "are eroding cash generation."
            ),
        },
    ],
    "DLO": [
        # LatAm payments — most signals (TPV, take-rate breakdown) need KPI
        # extractors that don't exist yet. Forward-declare them.
        {
            "rule_id": "dlo_tpv_yoy_below_30",
            "kpi_name": "TPV YoY Growth (FXN)",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "TPV FX-neutral YoY < 30% for 2Q — cross-border payments "
                "scale thesis fails. DLocal runs 50%+ in normal periods."
            ),
        },
        {
            "rule_id": "dlo_take_rate_below_1_pct",
            "kpi_name": "Take Rate",
            "comparator": "lt", "threshold": 1, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Take rate < 1.0% for 2Q — pricing power degrading; the "
                "differentiated cross-border product loses its premium."
            ),
        },
        {
            "rule_id": "dlo_gross_margin_below_35",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 35, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 35% for 2Q — mix shifting to lower-margin large-merchant "
                "deals or local-rails costs rising; net take rate eroding."
            ),
        },
    ],
    "FCX": [
        {
            "rule_id": "fcx_fcf_margin_below_10",
            "kpi_name": "FCF Margin (GAAP)",
            "comparator": "lt", "threshold": 10, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 10% for 2Q — copper realized price below "
                "all-in sustaining cost. Single-commodity exposure makes this "
                "FCF floor critical."
            ),
        },
        {
            "rule_id": "fcx_capex_intensity_above_30",
            "kpi_name": "Capex / Revenue (GAAP)",
            "comparator": "gt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Capex/Revenue > 30% for 2Q — Indonesia smelter + "
                "brownfield expansion overrun is the standard mining risk."
            ),
        },
        {
            "rule_id": "fcx_op_margin_below_18",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": 18, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < 18% for 2Q — proxy for realized copper price "
                "in cost-curve terms. FCX clears low-20s in normal cycles."
            ),
        },
    ],
    "SOFI": [
        # Transition fintech-to-bank. NIM is in DB. Members YoY too.
        {
            "rule_id": "sofi_nim_below_5_5",
            "kpi_name": "Net interest margin (NIM)",
            "comparator": "lt", "threshold": 5.5, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "NIM < 5.5% for 2Q — SOFI's high-yield-deposit + personal-loan "
                "spread thesis breaks. They run 5.7-6.0% structurally."
            ),
        },
        {
            "rule_id": "sofi_members_yoy_below_25",
            "kpi_name": "Members YoY growth",
            "comparator": "lt", "threshold": 25, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Members YoY < 25% for 2Q — cross-sell flywheel stalling; "
                "the one-stop financial-services thesis depends on continued "
                "20%+ member-base compounding."
            ),
        },
        {
            "rule_id": "sofi_adj_ebitda_margin_below_25",
            "kpi_name": "Adjusted EBITDA margin",
            "comparator": "lt", "threshold": 25, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Adjusted EBITDA margin < 25% for 2Q — SOFI's path to "
                "sustainable GAAP-profit breaks; 27-30% has been the recent "
                "operating cadence."
            ),
        },
    ],
    "TMO": [
        # Thermo Fisher — life sciences. Mostly universal KPIs in DB; segment
        # data not extracted. Forward-declare organic growth (will fire once
        # extractor lands).
        {
            "rule_id": "tmo_organic_growth_below_3",
            "kpi_name": "Organic Revenue Growth",
            "comparator": "lt", "threshold": 3, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Organic revenue growth < 3% for 2Q — TMO's pricing + "
                "Bioproduction segment compounding thesis fails; this is the "
                "diagnostic signal for an end-to-end life-sciences platform."
            ),
        },
        {
            "rule_id": "tmo_op_margin_below_22",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": 22, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < 22% for 2Q — TMO clears 23-25% in normal cycles; "
                "sub-22 means M&A synergies are not materializing or pricing "
                "power weakening."
            ),
        },
        {
            "rule_id": "tmo_gross_margin_below_40",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 40, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 40% for 2Q — pricing or mix degradation; the moat "
                "thesis (instrument + consumable razor-blade economics) "
                "depends on sustained 41-43%."
            ),
        },
    ],

    # ------ stub names — no existing holdings JSON; create minimal v2 stubs ----
    "CGEH": [
        # Care.com / Caregiver Health (verify) — placeholder rules
        {
            "rule_id": "cgeh_revenue_yoy_below_8",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 8, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < 8% for 2Q — STUB threshold; refine when "
                "user-authored thesis lands."
            ),
        },
        {
            "rule_id": "cgeh_gross_margin_below_30",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": "GM < 30% — STUB; refine post-diligence.",
        },
        {
            "rule_id": "cgeh_op_margin_below_5",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": 5, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": "Op margin < 5% — STUB; refine post-diligence.",
        },
    ],
    "FIGR": [
        # Figure Technologies / Figure AI — placeholder rules
        {
            "rule_id": "figr_revenue_yoy_below_15",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": "Revenue YoY < 15% — STUB threshold; needs diligence.",
        },
        {
            "rule_id": "figr_gross_margin_below_25",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 25, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": "GM < 25% — STUB.",
        },
        {
            "rule_id": "figr_op_margin_above_neg_20",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": -20, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < -20% — STUB; early-stage tolerance, refine "
                "based on cash runway model."
            ),
        },
    ],
    "NTDOY": [
        # Nintendo — console + IP
        {
            "rule_id": "ntdoy_revenue_yoy_below_neg_15",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": -15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < -15% for 2Q — console lifecycle valley too "
                "deep; mid-cycle Switch transition tolerance is -10% to +10%."
            ),
        },
        {
            "rule_id": "ntdoy_fcf_margin_below_15",
            "kpi_name": "FCF Margin (GAAP)",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 15% for 2Q — Nintendo's structural floor "
                "even in console valleys is 20%+; sub-15 indicates "
                "first-party software tie-ratio breaking."
            ),
        },
        {
            "rule_id": "ntdoy_op_margin_below_15",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < 15% — IP-monetization profit pool breaking "
                "(theme parks, mobile, films should be absorbing console "
                "cyclicality)."
            ),
        },
    ],
    "NTRA": [
        # Natera — clinical genomics
        {
            "rule_id": "ntra_revenue_yoy_below_20",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 20, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < 20% for 2Q — STUB; high-growth genomics "
                "platform should sustain 25%+ during product expansion."
            ),
        },
        {
            "rule_id": "ntra_gross_margin_below_50",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 50, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 50% for 2Q — test reagent costs + reimbursement mix "
                "deteriorating; the consumable-economics thesis fails."
            ),
        },
        {
            "rule_id": "ntra_op_margin_above_neg_10",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": -10, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < -10% for 2Q — scaling-to-breakeven path "
                "breaks; clinical genomics scale should drive toward "
                "breakeven not deeper losses."
            ),
        },
    ],
    "TEM": [
        # Tempus AI — clinical AI / labs
        {
            "rule_id": "tem_revenue_yoy_below_30",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < 30% for 2Q — STUB; AI-clinical compounder "
                "thesis requires sustained 35%+ at current scale."
            ),
        },
        {
            "rule_id": "tem_gross_margin_below_35",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 35, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 35% for 2Q — STUB; data + AI inferencing platform "
                "should drive GM expansion as data licensing share rises."
            ),
        },
        {
            "rule_id": "tem_op_margin_above_neg_30",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": -30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < -30% for 2Q — STUB cash-burn floor; below "
                "this the runway thesis breaks at current liquidity."
            ),
        },
    ],
    "WGS": [
        # GeneDx — genetic testing
        {
            "rule_id": "wgs_revenue_yoy_below_30",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < 30% for 2Q — STUB; whole-genome testing "
                "volume + reimbursement-rate compounding thesis."
            ),
        },
        {
            "rule_id": "wgs_gross_margin_below_50",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 50, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 50% for 2Q — STUB; reimbursed-rate-per-test and "
                "scale economics need to compound; sub-50 means the "
                "structural margin story is broken."
            ),
        },
        {
            "rule_id": "wgs_op_margin_below_neg_20",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": -20, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < -20% for 2Q — STUB cash-burn floor; refine "
                "based on runway model."
            ),
        },
    ],
}

# Tickers where the bank-treatment applies (drop FCF + OCF universals).
BANKS = {"SOFI"}

# Tickers that don't have a holdings JSON yet — create minimal v2 stub on the fly.
STUB_NAMES: dict[str, str] = {
    "CGEH": "Care.com",
    "FIGR": "Figure",
    "NTDOY": "Nintendo Co. Ltd.",
    "NTRA": "Natera",
    "TEM": "Tempus AI",
    "WGS": "GeneDx",
}


def make_stub(ticker: str, name: str) -> dict:
    """Minimal schema-v2 stub. No thesis paragraph, no DCF params — just the
    rule contract so the eval brief surfaces real disqualifiers. The user
    fills in the rest if this candidate graduates to portfolio diligence."""
    return {
        "ticker": ticker,
        "name": name,
        "last_updated": date.today().isoformat(),
        "thesis": (
            f"STUB: {name} ({ticker}) is an evaluation candidate. Holdings JSON "
            "scaffolded by Phase F-3 to carry per-ticker break_rules for the "
            "eval brief — needs user-authored thesis + tier_1_kpis + DCF "
            "params if/when it graduates to portfolio."
        ),
        "verdict": "Evaluating",
        "verdict_color": "blue",
        "chart_priorities": [],
        "tier_1_kpis": [],
        "tier_2_kpis": [],
        "nuance": {},
        "competitive_watchlist": [],
        "thesis_breakers_qualitative": [],
        "schema_version": 2,
        "wacc": None,
        "mos_bar": None,
        "dcf_defaults": {"forecast_years": 5, "terminal_multiple": 18.0},
        "segments": [],
        "operational_kpis": [],
        "break_rules_soft": [],
        "_status": "evaluation_stub_phase_f3",
    }


def migrate(ticker: str, holdings_dir: Path) -> None:
    path = holdings_dir / f"{ticker}.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    elif ticker in STUB_NAMES:
        payload = make_stub(ticker, STUB_NAMES[ticker])
    else:
        print(f"  [skip] {ticker}: no JSON and not in stub registry")
        return
    payload["break_rules"] = (
        BANK_UNIVERSALS if ticker in BANKS else STANDARD_UNIVERSALS
    )
    payload["business_model_rules"] = PER_TICKER_RULES[ticker]
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    created = "+stub" if path.exists() and ticker in STUB_NAMES and "_status" in payload else ""
    print(f"  [ok] {ticker}{created}: {len(payload['break_rules'])} universals "
          f"+ {len(payload['business_model_rules'])} business-model rules")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    holdings_dir = root / "micro_thesis" / "holdings"
    print(f"Migrating against {holdings_dir}")
    for ticker in sorted(PER_TICKER_RULES.keys()):
        migrate(ticker, holdings_dir)
    print("Done.")


if __name__ == "__main__":
    main()
