"""One-shot migration: tighten universal break_rules + add business_model_rules
across the 10 remaining portfolio holdings.

Universals get reduced from 7 -> 3 catastrophic tripwires:
  - universal_revenue_decline   (Revenue YoY < 0 for 2Q)
  - universal_fcf_margin_collapse (FCF Margin < 5% for 2Q) -- dropped on NU (bank)
  - universal_ocf_decline_yoy   (OCF YoY < -10% for 2Q)  -- dropped on NU (bank)

The 4 noisy universals are removed entirely (universal_operating_loss,
universal_net_loss, universal_gross_margin_collapse, capex_intensity_too_high).
For SBC-heavy software, asset-managers, banks, and capex-cycle pharma these
universals were wrong calibrations.

Per-ticker business_model_rules are added with KPI names that already exist in
kpi_facts where possible. Where the right signal requires a KPI that hasn't been
extracted yet, the rule still goes in -- the evaluator returns OK / "no
observations" until the KPI extractor lands, but the contract is in place.

Run this once from the worktree root; review the diffs; commit.
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Common tripwires.
# ---------------------------------------------------------------------------

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

# Banks: FCF / OCF metrics are dominated by deposit and loan-portfolio flows,
# not operating cash generation. Drop both universal cash-flow tripwires.
BANK_UNIVERSALS = [UNIVERSAL_REVENUE_DECLINE]


# ---------------------------------------------------------------------------
# Per-ticker business_model_rules.
# ---------------------------------------------------------------------------

PER_TICKER_RULES: dict[str, list[dict]] = {
    # AMZN — AWS profit engine + retail OM inflection + ads option
    "AMZN": [
        {
            "rule_id": "amzn_aws_growth_below_15",
            "kpi_name": "AWS Revenue YoY Growth",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "AWS revenue YoY < 15% for 2Q — the cloud-growth thesis is the "
                "AMZN multiple driver; sustained sub-15 breaks it."
            ),
        },
        {
            "rule_id": "amzn_aws_op_margin_below_28",
            "kpi_name": "AWS Operating Margin",
            "comparator": "lt", "threshold": 28, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "AWS op margin < 28% for 2Q — high-30s is the recent run rate; "
                "sub-28 means AI infra capex is no longer absorbing into "
                "operating leverage."
            ),
        },
        {
            "rule_id": "amzn_na_retail_op_margin_below_4",
            "kpi_name": "North America Retail Operating Margin",
            "comparator": "lt", "threshold": 4, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "NA Retail op margin < 4% for 2Q — retail flywheel margin "
                "thesis depends on continued levering past 5%."
            ),
        },
        {
            "rule_id": "amzn_ads_growth_below_18",
            "kpi_name": "Advertising Revenue YoY Growth",
            "comparator": "lt", "threshold": 18, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Ads revenue YoY < 18% for 2Q — third profit-pool thesis "
                "(after AWS and Prime) fails."
            ),
        },
    ],

    # BN — distributable earnings + FRE drive the equity; GAAP consolidated noise
    # masks owner-economics. Drop the catastrophic FCF universal too (BN's
    # consolidated cash flow includes partner-owned infrastructure).
    "BN": [
        {
            "rule_id": "bn_de_per_share_below_15",
            "kpi_name": "Distributable Earnings (DE) per share growth YoY (TTM)",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "DE/share growth < 15% YoY (TTM) — the owner-economics floor; "
                "this is what compounds equity value."
            ),
        },
        {
            "rule_id": "bn_fre_growth_below_15",
            "kpi_name": "Fee-Related Earnings (FRE) growth (BAM segment)",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "BAM-segment FRE growth < 15% YoY — asset manager scale "
                "thesis breaks here."
            ),
        },
        {
            "rule_id": "bn_fee_bearing_capital_below_10",
            "kpi_name": "Fee-bearing capital growth",
            "comparator": "lt", "threshold": 10, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Fee-bearing capital growth < 10% — the leading indicator of "
                "future FRE; if AUM raise stalls, FRE follows."
            ),
        },
        {
            "rule_id": "bn_wealth_solutions_roe_below_15",
            "kpi_name": "Brookfield Wealth Solutions ROE",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "BWS ROE < 15% for 2Q — the insurance leg's spread economics "
                "are degrading."
            ),
        },
    ],

    # GOOG — Search ad floor + Cloud margins + ad-share mix
    "GOOG": [
        {
            "rule_id": "goog_search_revenue_below_8",
            "kpi_name": "Search & Other revenue growth (CC)",
            "comparator": "lt", "threshold": 8, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Search ads CC growth < 8% YoY for 2Q — AI-disruption "
                "thesis-breaker; Search remains the core profit pool."
            ),
        },
        {
            "rule_id": "goog_gcp_revenue_below_25",
            "kpi_name": "GCP revenue growth (YoY)",
            "comparator": "lt", "threshold": 25, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GCP revenue YoY < 25% for 2Q — Cloud catch-up vs MSFT/AWS "
                "thesis breaks."
            ),
        },
        {
            "rule_id": "goog_gcp_margin_below_15",
            "kpi_name": "GCP operating margin trajectory",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GCP op margin < 15% — Cloud profitability inflection reverses."
            ),
        },
        {
            "rule_id": "goog_consolidated_op_margin_below_28",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": 28, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Consolidated op margin < 28% — TAC pressure or capex bloat "
                "compressing the profit engine (Q1-Q3 run rate ~33%)."
            ),
        },
    ],

    # MELI — GMV growth + payments take rate + credit-portfolio risk
    "MELI": [
        {
            "rule_id": "meli_gmv_growth_below_25",
            "kpi_name": "GMV growth FX-neutral (consolidated)",
            "comparator": "lt", "threshold": 25, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GMV FX-neutral < 25% for 2Q — marketplace scale thesis breaks; "
                "MELI runs 30-50% historically."
            ),
        },
        {
            "rule_id": "meli_consolidated_op_margin_below_8",
            "kpi_name": "Operating margin (consolidated)",
            "comparator": "lt", "threshold": 8, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Consolidated op margin < 8% for 2Q — credit + fintech margin "
                "investment cycle breaks owner economics."
            ),
        },
        {
            "rule_id": "meli_tpv_growth_below_30",
            "kpi_name": "TPV Growth (FXN)",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "MercadoPago TPV FX-neutral < 30% — fintech leg loses momentum."
            ),
        },
        {
            "rule_id": "meli_credit_npl_above_25",
            "kpi_name": "Credit portfolio NPL ratio",
            "comparator": "gt", "threshold": 25, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Credit-portfolio NPL > 25% for 2Q — Mercado Crédito risk "
                "model breaks; credit losses dominate fintech contribution."
            ),
        },
    ],

    # META — already had well-calibrated rules; refine + add Reality Labs cap
    "META": [
        {
            "rule_id": "meta_foa_revenue_below_10",
            "kpi_name": "Family of Apps revenue growth (CC)",
            "comparator": "lt", "threshold": 10, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FoA revenue CC growth < 10% for 2Q — the ad business is the "
                "entire profit pool; sub-10 is a structural growth break."
            ),
        },
        {
            "rule_id": "meta_dap_below_3",
            "kpi_name": "Family DAP (daily active people) growth",
            "comparator": "lt", "threshold": 3, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Family DAP growth < 3% YoY for 2Q — engagement saturation; "
                "ad-impression volume floor breaks."
            ),
        },
        {
            "rule_id": "meta_consolidated_op_margin_below_35",
            "kpi_name": "Consolidated operating margin",
            "comparator": "lt", "threshold": 35, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Consolidated op margin < 35% for 2Q — would need ad price + "
                "RL bleed combined; the profit-pool integrity check."
            ),
        },
        {
            "rule_id": "meta_reality_labs_loss_below_neg_5b",
            "kpi_name": "Reality Labs operating loss",
            "comparator": "lt", "threshold": -5000000000, "unit": "actual",
            "consecutive_periods": 2,
            "narrative": (
                "Reality Labs op loss > $5B/quarter for 2Q — the lid is off "
                "on the metaverse bet; consolidated margin gets compressed "
                "regardless of FoA performance. (Threshold is in raw dollars "
                "to match kpi_facts.unit='actual' for this KPI.)"
            ),
        },
    ],

    # NOW — cRPO, NRR (not in kpi_facts yet), Now Assist cohort growth
    "NOW": [
        {
            "rule_id": "now_crpo_growth_below_18",
            "kpi_name": "cRPO Growth (CC)",
            "comparator": "lt", "threshold": 18, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "cRPO CC growth < 18% YoY for 2Q — the forward-revenue "
                "indicator that matters most for SaaS retention thesis."
            ),
        },
        {
            "rule_id": "now_assist_acv_growth_below_60",
            "kpi_name": "Now Assist >$1M ACV YoY Growth",
            "comparator": "lt", "threshold": 60, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Now Assist >$1M ACV cohort growth < 60% YoY for 2Q — AI "
                "attach thesis breaks; this is the next-gen ARR driver."
            ),
        },
        {
            "rule_id": "now_large_customer_growth_below_15",
            "kpi_name": "Customers >$5M ACV YoY Growth",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Customers >$5M ACV YoY < 15% for 2Q — large-deal motion "
                "stalls; the enterprise-scale moat weakens."
            ),
        },
        {
            "rule_id": "now_fcf_margin_below_28",
            "kpi_name": "FCF Margin (GAAP)",
            "comparator": "lt", "threshold": 28, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 28% for 2Q — NOW's structural floor is ~30%; "
                "sub-28 means SBC efficiency or capex absorption broke."
            ),
        },
    ],

    # NU — bank: drop FCF/OCF universals, use NIM/NPL/efficiency
    "NU": [
        {
            "rule_id": "nu_npl_above_7",
            "kpi_name": "NPL 90d+",
            "comparator": "gt", "threshold": 7, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "90d+ NPL > 7% for 2Q — credit-quality break; their underwriting "
                "thesis depends on the 5-6% historical range holding."
            ),
        },
        {
            "rule_id": "nu_arpac_below_10",
            "kpi_name": "Monthly ARPAC",
            "comparator": "lt", "threshold": 10, "unit": "actual",
            "consecutive_periods": 2,
            "narrative": (
                "Monthly ARPAC < $10 for 2Q — cross-sell stalling; expanding "
                "ARPAC is the unit-economics flywheel."
            ),
        },
        {
            "rule_id": "nu_activity_rate_below_82",
            "kpi_name": "Activity Rate",
            "comparator": "lt", "threshold": 82, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Activity rate < 82% for 2Q — customers becoming dormant; "
                "engagement-flywheel signal."
            ),
        },
        {
            "rule_id": "nu_early_npl_above_5",
            "kpi_name": "NPL 15-90d",
            "comparator": "gt", "threshold": 5, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "15-90d NPL > 5% for 2Q — leading indicator of cohort "
                "credit deterioration; fires 1-2Q before 90d+ trips."
            ),
        },
    ],

    # NVO — Wegovy/Ozempic franchise + diabetes legacy + R&D yield
    "NVO": [
        {
            "rule_id": "nvo_revenue_cc_below_12",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 12, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < 12% for 2Q — sub-cycle floor for a name with "
                "Wegovy supply ramp; sub-12 means GLP-1 share is being lost."
            ),
        },
        {
            "rule_id": "nvo_op_margin_below_30",
            "kpi_name": "Operating Margin (GAAP)",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Op margin < 30% for 2Q — NVO runs 35-45% historically; "
                "sub-30 means Wegovy capacity capex is no longer being "
                "absorbed into operating leverage."
            ),
        },
        {
            "rule_id": "nvo_gross_margin_below_75",
            "kpi_name": "Gross Margin (GAAP)",
            "comparator": "lt", "threshold": 75, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "GM < 75% for 2Q — pricing/cost mix degrading; biologics "
                "should sustain low-80s."
            ),
        },
        {
            "rule_id": "nvo_capex_intensity_above_30",
            "kpi_name": "Capex / Revenue (GAAP)",
            "comparator": "gt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Capex/Revenue > 30% for 2Q — Wegovy capacity buildout was "
                "the thesis through FY26; sustained >30% past FY27 means "
                "capex is no longer cycling down with capacity in place."
            ),
        },
    ],

    # VEEV — segment growth + sub gross margin + subscription compounding
    "VEEV": [
        {
            "rule_id": "veev_total_rev_below_12",
            "kpi_name": "Total revenue YoY growth",
            "comparator": "lt", "threshold": 12, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Total revenue YoY < 12% for 2Q — base case for compounding "
                "vertical-SaaS; sub-12 means the durable-growth thesis breaks."
            ),
        },
        {
            "rule_id": "veev_rd_solutions_below_15",
            "kpi_name": "R&D Solutions revenue YoY growth",
            "comparator": "lt", "threshold": 15, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "R&D Solutions YoY < 15% for 2Q — Vault platform expansion "
                "thesis (the higher-growth segment) fails."
            ),
        },
        {
            "rule_id": "veev_subscription_growth_below_12",
            "kpi_name": "Subscription revenue YoY growth",
            "comparator": "lt", "threshold": 12, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Subscription revenue YoY < 12% for 2Q — the high-quality "
                "compounding revenue stream stalls."
            ),
        },
        {
            "rule_id": "veev_non_gaap_op_margin_below_38",
            "kpi_name": "Non-GAAP operating margin",
            "comparator": "lt", "threshold": 38, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Non-GAAP op margin < 38% for 2Q — VEEV's structural floor "
                "is ~40%; SBC-adjusted profitability is the right metric "
                "vs GAAP."
            ),
        },
    ],

    # WIX — top-line + FCF margin floor + Studio (high-growth segment)
    "WIX": [
        {
            "rule_id": "wix_revenue_growth_below_10",
            "kpi_name": "Revenue YoY Growth (USD)",
            "comparator": "lt", "threshold": 10, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Revenue YoY < 10% for 2Q — durable creator-tooling growth "
                "thesis breaks; consumer SaaS floor."
            ),
        },
        {
            "rule_id": "wix_fcf_margin_below_18",
            "kpi_name": "FCF Margin (GAAP)",
            "comparator": "lt", "threshold": 18, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "FCF margin < 18% for 2Q — WIX's structural floor is ~20%; "
                "the FCF-compounder thesis depends on this holding."
            ),
        },
        {
            "rule_id": "wix_studio_arr_growth_below_30",
            "kpi_name": "Wix Studio ARR YoY Growth",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Studio ARR YoY < 30% — Studio is the agency-tier growth "
                "lever; sub-30 means the upmarket expansion stalls."
            ),
        },
        {
            "rule_id": "wix_partners_revenue_share_below_30",
            "kpi_name": "Partners Revenue Share",
            "comparator": "lt", "threshold": 30, "unit": "percent",
            "consecutive_periods": 2,
            "narrative": (
                "Partners (agency / pro creator) revenue share < 30% for 2Q "
                "— the high-AOV monetization mix-shift thesis reverses."
            ),
        },
    ],
}

# Tickers where the bank-treatment applies (drop FCF + OCF universals).
# NU is a literal bank. BN is a diversified holdco: consolidated GAAP cash
# flows include partner-owned infrastructure (renewable, infra funds), so
# headline FCF/OCF doesn't reflect parent-company economics. Use DE/share +
# FRE growth (already in business_model_rules) as the real cash-engine signal.
BANKS = {"NU", "BN"}


def migrate(ticker: str, holdings_dir: Path) -> None:
    """Rewrite one holdings JSON in place. Idempotent — re-running produces the
    same output. Preserves all non-break-rules fields verbatim."""
    path = holdings_dir / f"{ticker}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["break_rules"] = (
        BANK_UNIVERSALS if ticker in BANKS else STANDARD_UNIVERSALS
    )
    payload["business_model_rules"] = PER_TICKER_RULES[ticker]
    # Stable ordering: keep break_rules + business_model_rules adjacent in the
    # serialized output by re-inserting them in the same spot. json.dumps with
    # sort_keys=False respects insertion order; we just need to ensure the new
    # business_model_rules key is positioned right after break_rules. Since
    # both keys are already set on the payload dict in that order on Py 3.7+,
    # the output is fine without further work.
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  [ok] {ticker}: {len(payload['break_rules'])} universals + "
          f"{len(payload['business_model_rules'])} business-model rules")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    holdings_dir = root / "micro_thesis" / "holdings"
    print(f"Migrating against {holdings_dir}")
    for ticker in sorted(PER_TICKER_RULES.keys()):
        migrate(ticker, holdings_dir)
    print("Done.")


if __name__ == "__main__":
    main()
