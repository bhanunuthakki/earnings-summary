"""Per-name DCF assumption pass (new redesign schema).

For a ticker, gather FMP + thesis context and ask the configured DCF model to set the
10-year DCF drivers the redesigned workbook consumes: per-segment growth
(near-term + terminal), the operating-margin path, capex intensity, terminal
basis/multiple, business-model applicability, and a narrative. Cache the result
under data/dcf_assumptions/<T>.json (key "redesign") so the renderer can read it.

Usage:  DCF_TICKER=AMZN python execution/dcf_opus_assumptions.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from compute.segment_cache import apply_overrides
from db_paths import require_db_path
from dcf.fiscal_periods import detect_fy_periods
from llm.contracts import DCF_ASSUMPTIONS_SCHEMA, DcfAssumptionsPayload
from llm.structured import call_llm_structured

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
FMP = REPO / "data" / "historical" / "fmp"

DATABASE_PATH = require_db_path()
TICKER = os.environ.get("DCF_TICKER", "AMZN")


def load_cache_records(name: str) -> list[dict[str, object]]:
    path = FMP / name
    if not path.exists():
        return []
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    # Some profile cache versions contain one object instead of an array.
    return TypeAdapter(list[dict[str, object]]).validate_python(
        [payload] if isinstance(payload, dict) else payload
    )


def to_millions(value: object) -> float:
    return value / 1e6 if isinstance(value, (int, float)) else 0.0


income_records = load_cache_records(f"{TICKER}_income_statement_quarterly.json")
cashflow_records = load_cache_records(f"{TICKER}_cash_flow_quarterly.json")
segment_records = apply_overrides(
    load_cache_records(f"{TICKER}_product_segments_quarterly.json"),
    ticker=TICKER,
    dim_type="product",
    db_path=str(DATABASE_PATH),
)
est = load_cache_records(f"{TICKER}_analyst_estimates_annual.json")
profile_records = load_cache_records(f"{TICKER}_profile.json")
company_profile: dict[str, object] = profile_records[0] if profile_records else {}


def index_fiscal_records(
    records: Sequence[dict[str, object]], *, segments: bool = False
) -> dict[tuple[int, str], dict[str, object]]:
    indexed: dict[tuple[int, str], dict[str, object]] = {}
    for record in records:
        period, fiscal_year = record.get("period"), record.get("fiscalYear")
        if not isinstance(fiscal_year, (str, int, float)):
            continue
        try:
            year = int(fiscal_year)
        except (TypeError, ValueError):
            continue
        if isinstance(period, str) and period.startswith("Q"):
            indexed[(year, period)] = (
                TypeAdapter(dict[str, object]).validate_python(record.get("data") or {})
                if segments
                else record
            )
    return indexed


income_by_period, cashflow_by_period, segments_by_period = (
    index_fiscal_records(income_records),
    index_fiscal_records(cashflow_records),
    index_fiscal_records(segment_records, segments=True),
)
PERIODS = detect_fy_periods(
    income_by_period
)  # ("Q1".."Q4") quarterly · ("Q2","Q4") semi-annual (BHP)
fys = sorted({year for year, _period in income_by_period})
full = [y for y in fys if all((y, p) in income_by_period for p in PERIODS)]
if not full:
    # No issuer-complete fiscal year (e.g. an IPO with < 2 years of history): the
    # redesign builder will SKIP this name, so don't spend an Opus call on it.
    print(f"SKIP\t{TICKER}\tno complete fiscal year yet\t(insufficient history for a DCF)")
    raise SystemExit(0)


def fiscal_year_total(
    records: Mapping[tuple[int, str], Mapping[str, object]], field: str, year: int
) -> float:
    return sum(to_millions(records.get((year, period), {}).get(field)) for period in PERIODS)


# --- context ---
lines = [
    f"Company: {company_profile.get('companyName', TICKER)} ({TICKER})",
    f"Sector / industry: {company_profile.get('sector', '?')} / {company_profile.get('industry', '?')}",
    f"Reported currency: {(income_by_period.get((full[-1], PERIODS[-1])) or income_records[0] if income_records else {}).get('reportedCurrency', '?') if income_records else '?'}",
    f"Country: {company_profile.get('country', '?')}  |  Beta: {company_profile.get('beta', '?')}  |  Current price (USD): {company_profile.get('price', '?')}",
    "",
]
lines.append("Recent fiscal-year actuals (reporting currency, $M):")
for y in full[-3:]:
    rev = fiscal_year_total(income_by_period, "revenue", y)
    if not rev:
        continue
    oi = fiscal_year_total(income_by_period, "operatingIncome", y)
    ni = fiscal_year_total(income_by_period, "netIncome", y)
    cap = abs(fiscal_year_total(cashflow_by_period, "capitalExpenditure", y))
    da = fiscal_year_total(cashflow_by_period, "depreciationAndAmortization", y)
    lines.append(
        f"  FY{y}: revenue {rev:,.0f}  op-margin {oi / rev:.1%}  net-margin {ni / rev:.1%}  "
        f"capex {cap:,.0f} ({cap / rev:.1%} of rev)  D&A {da:,.0f}"
    )

# segments
seg_ann: defaultdict[int, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
for (y, p), d in segments_by_period.items():
    for k, v in (d or {}).items():
        if isinstance(v, (int, float)):
            seg_ann[y][k] += v / 1e6
seg_names: list[str] = []
if full and seg_ann.get(full[-1]):
    seg_names = sorted(seg_ann[full[-1]], key=lambda s: -seg_ann[full[-1]][s])
    lines.append("\nProduct segments (latest FY revenue, $M, and YoY growth):")
    for s in seg_names:
        cur, prev = seg_ann[full[-1]].get(s, 0), seg_ann[full[-2]].get(s, 0) if len(full) > 1 else 0
        g = (cur / prev - 1) if prev else None
        lines.append(f"  {s}: {cur:,.0f}" + (f"  ({g:+.1%} YoY)" if g is not None else ""))
else:
    lines.append(
        "\nNo usable product-segment data — model a single revenue line ('Total company')."
    )

# consensus
ebY: dict[int, dict[str, object]] = {}
for e in est:
    try:
        ebY[int(str(e.get("date"))[:4])] = e
    except (TypeError, ValueError):
        pass
fwd = [y for y in sorted(ebY) if y >= (full[-1] + 1 if full else 2026)][:5]
if fwd:
    lines.append("\nStreet consensus (FMP) forward estimates ($M, EPS per share):")
    for y in fwd:
        e = ebY[y]
        lines.append(
            f"  {y}: revenue {to_millions(e.get('revenueAvg')):,.0f}  net-income {to_millions(e.get('netIncomeAvg')):,.0f}  EPS {e.get('epsAvg', '?')}"
        )

# thesis
for d in ("holdings", "evaluation"):
    tp = REPO / "micro_thesis" / d / f"{TICKER}.json"
    if tp.exists():
        th = json.loads(tp.read_text(encoding="utf-8"))
        blurb = th.get("thesis") or th.get("micro_thesis") or ""
        if blurb:
            lines.append(f"\nUser's investment thesis:\n  {str(blurb)[:600]}")
        break

CONTEXT = "\n".join(lines)
SEG_KEYS = seg_names if seg_names else ["Total company"]

PROMPT = f"""You are a valuation analyst building a 10-year FCFF DCF in the style of Aswath \
Damodaran. Set realistic, defensible drivers for {TICKER} grounded in the data below. Fade growth \
toward a mature terminal rate; let margins normalize via operating leverage; converge capex \
toward maintenance (capex/D&A -> ~1.0x) in the terminal year.

{CONTEXT}

VALUATION MODEL — FIRST decide which archetype fits {TICKER}. The pipeline has these templates:
  - "fcff_dcf": the 10-year FCFF DCF below. DEFAULT for operating companies (software, \
consumer, industrial, healthcare, semis, energy, payments, etc.).
  - "bank_excess_return": equity-side excess-return for deposit/credit BANKS — value equity as \
book + PV[(ROE - cost of equity) x regulatory capital]. Use for spread-lending banks.
  - "holdco_sotp": sum-of-the-parts / NAV for capital-allocator HOLDING COMPANIES that \
consolidate non-recourse subsidiary debt + large minorities (NAV of the parts: fee business, \
carry, insurance, invested capital, less corporate). Use for Brookfield-style holdcos.
  - "new": NONE of the above fits well — e.g. a pure insurer needing embedded value, a REIT \
needing NAV + FFO, a pure asset manager needing an FRE multiple, a yieldco needing a DDM, a \
pre-revenue biotech needing risk-adjusted NPV, a commodity/royalty needing a NAV. Set \
valuation_model="new" and put the proposed archetype + a one-sentence spec in \
valuation_model_suggestion.
Keep dcf_applicable consistent: true if and only if valuation_model="fcff_dcf". The FCFF drivers \
below are only USED when valuation_model="fcff_dcf"; still fill them best-effort.

Return ONLY a JSON object (no prose, no markdown fences) with EXACTLY these keys:
{{
  "dcf_applicable": true,
  "business_model": "operating",
  "valuation_model": "fcff_dcf",
  "valuation_model_suggestion": "",
  "segments": {{ {", ".join(f'"{s}": {{"near_term_growth": 0.10, "terminal_growth": 0.03}}' for s in SEG_KEYS)} }},
  "near_term_op_margin": 0.20,
  "terminal_op_margin": 0.25,
  "tax_rate": 0.21,
  "capex_pct_revenue_2026": 0.05,
  "terminal_capex_da": 1.05,
  "terminal_method": "Exit multiple",
  "exit_basis": "EV/EBITDA",
  "exit_multiple": 15.0,
  "terminal_growth_g": 0.03,
  "narrative": "Margins expand as growth fades toward maturity.",
  "reasoning": "The path anchors to actual margins and a mature multiple."
}}
Growth must fade (near-term > terminal). For software/SaaS prefer EV/Sales or EV/EBITDA; for \
mature cash generators EV/FCF or EV/EBIT; pick the basis the market actually uses for {TICKER}. \
Be conservative: anchor near-term to consensus where shown."""


def _segment_guard(value: object) -> tuple[bool, str]:
    if not isinstance(value, DcfAssumptionsPayload):  # pragma: no cover - schema runs first
        return (False, "DCF assumptions are not the expected schema")
    expected = frozenset(SEG_KEYS)
    actual = frozenset(value.segments)
    if actual != expected:
        return (False, f"expected segments {sorted(expected)}; got {sorted(actual)}")
    return (True, "")


def _call_dcf_assumptions() -> DcfAssumptionsPayload:
    decoded = call_llm_structured(
        PROMPT,
        purpose="dcf_assumptions",
        ticker=TICKER,
        scope="dcf_assumptions_redesign",
        expect="object",
        schema=DCF_ASSUMPTIONS_SCHEMA,
        domain_guardrail=_segment_guard,
        max_escalation_tier=0,
    )
    if not isinstance(decoded, DcfAssumptionsPayload):  # pragma: no cover
        raise TypeError("dcf_assumptions schema returned an unexpected value")
    return decoded


def main() -> int:
    result = _call_dcf_assumptions()
    data = cast("dict[str, object]", result.model_dump(mode="json"))
    data["_segment_keys"] = SEG_KEYS

    cache_path = REPO / "data" / "dcf_assumptions" / f"{TICKER}.json"
    existing_raw: object = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    if not isinstance(existing_raw, dict):
        raise ValueError(f"{cache_path} must contain a JSON object")
    existing = cast("dict[str, object]", existing_raw)
    prior_redesign = existing.get("redesign")
    if isinstance(prior_redesign, dict) and "dcf_debt_scope" in prior_redesign:
        # Deterministic owner/governance input: the LLM schema neither selects
        # nor rewrites the liability perimeter used by the equity bridge.
        data["dcf_debt_scope"] = prior_redesign["dcf_debt_scope"]
    existing["redesign"] = data
    existing.setdefault("narrative", result.narrative)

    from dcf.assumptions_doc import baseline_from_opus_pass

    existing["opus_baseline"] = baseline_from_opus_pass(data)
    existing.pop("assumption_overrides", None)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    suggestion = result.valuation_model_suggestion
    print(
        f"OK\t{TICKER}\tvaluation_model={result.valuation_model}\t"
        f"business={result.business_model}\tbasis={result.exit_basis}@{result.exit_multiple}\t"
        f"term_margin={result.terminal_op_margin}"
        + (f"\tSUGGESTS: {suggestion}" if result.valuation_model == "new" else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
