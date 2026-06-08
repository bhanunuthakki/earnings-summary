"""Opus per-name DCF assumption pass (new redesign schema).

For a ticker, gather FMP + thesis context and ask claude-opus-4-8 to set the
10-year DCF drivers the redesigned workbook consumes: per-segment growth
(near-term + terminal), the operating-margin path, capex intensity, terminal
basis/multiple, business-model applicability, and a narrative. Cache the result
under data/dcf_assumptions/<T>.json (key "redesign") so the renderer can read it.

Usage:  DCF_TICKER=AMZN python scratch/opus_dcf_assumptions.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
FMP = REPO / "data" / "historical" / "fmp"
sys.path.insert(0, str(REPO / "src"))
from llm.cli import call_llm  # noqa: E402

T = os.environ.get("DCF_TICKER", "AMZN")


def loadj(name):
    p = FMP / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def m(v):
    return v / 1e6 if isinstance(v, (int, float)) else 0.0


inc = loadj(f"{T}_income_statement_quarterly.json")
cf = loadj(f"{T}_cash_flow_quarterly.json")
pseg = loadj(f"{T}_product_segments_quarterly.json")
est = loadj(f"{T}_analyst_estimates_annual.json")
prof = loadj(f"{T}_profile.json")
prof = prof[0] if isinstance(prof, list) and prof else (prof if isinstance(prof, dict) else {})


def idx(records, seg=False):
    out = {}
    for r in records:
        per, fy = r.get("period"), r.get("fiscalYear")
        try:
            fy = int(fy)
        except (TypeError, ValueError):
            continue
        if isinstance(per, str) and per.startswith("Q"):
            out[(fy, per)] = r.get("data") if seg else r
    return out


def detect_fy_periods(
    records_i: dict[tuple[int, str], object],
    default: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4"),
) -> tuple[str, ...]:
    """Canonical period labels that make up ONE fiscal year for this issuer.

    Generalises the "four quarters per year" assumption to any consistent cadence
    — e.g. a semi-annual filer (BHP) that reports only H1/H2 as Q2/Q4, which sum
    to the fiscal year exactly as four quarters do. The cadence is the LARGEST
    period-set that recurs across >=2 fiscal years, so a single partial year (the
    current in-progress year, or an IPO mid-ramp) is never mistaken for it; with
    too little history to establish one, fall back to quarterly so short-history
    names self-skip below instead of keying off a one-off period set.
    """
    by_fy: dict[int, set[str]] = defaultdict(set)
    for y, p in records_i:
        by_fy[y].add(p)
    counts: dict[frozenset[str], int] = defaultdict(int)
    for s in by_fy.values():
        if s:
            counts[frozenset(s)] += 1
    recurring = [s for s, n in counts.items() if n >= 2]
    return tuple(sorted(max(recurring, key=len))) if recurring else default


inc_i, cf_i, pseg_i = idx(inc), idx(cf), idx(pseg, True)
PERIODS = detect_fy_periods(inc_i)  # ("Q1".."Q4") quarterly · ("Q2","Q4") semi-annual (BHP)
fys = sorted({y for (y, p) in inc_i})
full = [y for y in fys if all((y, p) in inc_i for p in PERIODS)]
if not full:
    # No issuer-complete fiscal year (e.g. an IPO with < 2 years of history): the
    # redesign builder will SKIP this name, so don't spend an Opus call on it.
    print(f"SKIP\t{T}\tno complete fiscal year yet\t(insufficient history for a DCF)")
    raise SystemExit(0)


def fy(recs, fld, y):
    return sum(m(recs.get((y, p), {}).get(fld)) for p in PERIODS)


# --- context ---
lines = [
    f"Company: {prof.get('companyName', T)} ({T})",
    f"Sector / industry: {prof.get('sector', '?')} / {prof.get('industry', '?')}",
    f"Reported currency: {(inc_i.get((full[-1], PERIODS[-1]) or {}) or inc[0] if inc else {}).get('reportedCurrency', '?') if inc else '?'}",
    f"Country: {prof.get('country', '?')}  |  Beta: {prof.get('beta', '?')}  |  Current price (USD): {prof.get('price', '?')}",
    "",
]
lines.append("Recent fiscal-year actuals (reporting currency, $M):")
for y in full[-3:]:
    rev = fy(inc_i, "revenue", y)
    if not rev:
        continue
    oi = fy(inc_i, "operatingIncome", y)
    ni = fy(inc_i, "netIncome", y)
    cap = abs(fy(cf_i, "capitalExpenditure", y))
    da = fy(cf_i, "depreciationAndAmortization", y)
    lines.append(
        f"  FY{y}: revenue {rev:,.0f}  op-margin {oi / rev:.1%}  net-margin {ni / rev:.1%}  "
        f"capex {cap:,.0f} ({cap / rev:.1%} of rev)  D&A {da:,.0f}"
    )

# segments
seg_ann = defaultdict(lambda: defaultdict(float))
for (y, p), d in pseg_i.items():
    for k, v in (d or {}).items():
        if isinstance(v, (int, float)):
            seg_ann[y][k] += v / 1e6
seg_names = []
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
ebY = {}
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
            f"  {y}: revenue {m(e.get('revenueAvg')):,.0f}  net-income {m(e.get('netIncomeAvg')):,.0f}  EPS {e.get('epsAvg', '?')}"
        )

# thesis
for d in ("holdings", "evaluation"):
    tp = REPO / "micro_thesis" / d / f"{T}.json"
    if tp.exists():
        th = json.loads(tp.read_text(encoding="utf-8"))
        blurb = th.get("thesis") or th.get("micro_thesis") or ""
        if blurb:
            lines.append(f"\nUser's investment thesis:\n  {str(blurb)[:600]}")
        break

CONTEXT = "\n".join(lines)
SEG_KEYS = seg_names if seg_names else ["Total company"]

PROMPT = f"""You are a valuation analyst building a 10-year FCFF DCF in the style of Aswath \
Damodaran. Set realistic, defensible drivers for {T} grounded in the data below. Fade growth \
toward a mature terminal rate; let margins normalize via operating leverage; converge capex \
toward maintenance (capex/D&A -> ~1.0x) in the terminal year.

{CONTEXT}

VALUATION MODEL — FIRST decide which archetype fits {T}. The pipeline has these templates:
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
  "dcf_applicable": true | false,   // false for banks/insurers/asset-managers where an FCFF DCF is the wrong tool
  "business_model": "operating | bank | insurer | asset_manager | royalty | reit | other",
  "valuation_model": "fcff_dcf | bank_excess_return | holdco_sotp | new",
  "valuation_model_suggestion": "",  // when "new": <archetype name> - <one-sentence spec>; else ""
  "segments": {{ {", ".join(f'"{s}": {{"near_term_growth": <0-1 decimal>, "terminal_growth": <0-1 decimal>}}' for s in SEG_KEYS)} }},
  "near_term_op_margin": <0-1 decimal>,    // operating margin in the next 1-3 years
  "terminal_op_margin": <0-1 decimal>,     // mature operating margin by year 10
  "tax_rate": <0-1 decimal>,
  "capex_pct_revenue_2026": <0-1 decimal>, // 2026 capex as a fraction of 2026 revenue
  "terminal_capex_da": <number ~1.0-1.2>,  // capex/D&A in the terminal year
  "terminal_method": "Exit multiple | Perpetuity",
  "exit_basis": "EV/EBITDA | EV/Sales | EV/EBIT | EV/FCF",
  "exit_multiple": <number>,               // a defensible MATURE multiple for the chosen basis
  "terminal_growth_g": <0-1 decimal>,      // ~risk-free rate; <= 0.05
  "narrative": "<2-3 sentence valuation story>",
  "reasoning": "<2-3 sentences on the key judgement calls>"
}}
Growth must fade (near-term > terminal). For software/SaaS prefer EV/Sales or EV/EBITDA; for \
mature cash generators EV/FCF or EV/EBIT; pick the basis the market actually uses for {T}. \
Be conservative: anchor near-term to consensus where shown."""


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        return json.loads(t[a : b + 1])
    raise ValueError("no JSON object found")


resp = call_llm(PROMPT, model="claude-opus-4-8", ticker=T, scope="dcf_assumptions_redesign")
data = parse_json(resp)
data["_segment_keys"] = SEG_KEYS

cache_path = REPO / "data" / "dcf_assumptions" / f"{T}.json"
existing = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
existing["redesign"] = data
if data.get("narrative"):
    existing.setdefault("narrative", data["narrative"])
cache_path.parent.mkdir(parents=True, exist_ok=True)
cache_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
_vm = data.get("valuation_model") or ("fcff_dcf" if data.get("dcf_applicable") else "?")
_sugg = data.get("valuation_model_suggestion")
print(
    f"OK\t{T}\tvaluation_model={_vm}\tbusiness={data.get('business_model')}\t"
    f"basis={data.get('exit_basis')}@{data.get('exit_multiple')}\tterm_margin={data.get('terminal_op_margin')}"
    + (f"\tSUGGESTS: {_sugg}" if _vm == "new" and _sugg else "")
)
