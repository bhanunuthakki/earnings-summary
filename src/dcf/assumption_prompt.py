"""Prompt, guardrail and call for the ``dcf_assumptions`` purpose (prompt v3).

``execution/refresh_dcf_assumptions.py`` builds the company context from the
cached statements, the admitted KPI facts and consensus, then calls
:func:`call_dcf_assumptions`. The eval golden set
(``evals/golden/dcf_assumptions.json``) calls the same function with pinned
synthetic contexts, so the production prompt and the evaluated prompt are one
text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from llm.contracts import DCF_ASSUMPTIONS_SCHEMA, DcfAssumptionsPayload
from llm.structured import call_llm_structured

PURPOSE = "dcf_assumptions"
SCOPE = "dcf_assumptions_redesign"

Guard = Callable[[object], tuple[bool, str]]


def _example_records(seg_keys: Sequence[str]) -> str:
    fields = [f"segments.{s}.near_term_growth" for s in seg_keys] + [
        "near_term_op_margin",
        "terminal_op_margin",
        "exit_multiple",
    ]
    return ", ".join(
        json.dumps(
            {
                "field": field,
                "anchor_class": "reported_fact",
                "anchor": "FY2025 value and the three prior reported years.",
                "counter_argument": "The strongest reason this is wrong, and its direction.",
                "confidence": "medium",
                "observable": "Quarterly release, segment table.",
            }
        )
        for field in fields
    )


def build_prompt(ticker: str, context: str, seg_keys: Sequence[str]) -> str:
    """The complete v3 prompt for one company context."""
    example_records = _example_records(seg_keys)
    return f"""You are a valuation analyst building a 10-year FCFF DCF in the style of Aswath \
Damodaran. Set realistic, defensible drivers for {ticker} grounded in the data below. Fade growth \
toward a mature terminal rate; let margins normalize via operating leverage; converge capex \
toward maintenance (capex/D&A -> ~1.0x) in the terminal year.

METHOD. Form your own view from the drivers first; compare with consensus afterwards.
- Derive near-term growth from the reported history and the company-reported KPIs above \
(volume, price, users, units, retention). Prefer the driver that explains revenue over a \
headline growth rate.
- Compare the first forecast year with the latest reported year. A sharp change in growth or \
margin needs a named cause.
- Do not extend a recent margin gain when the growth that produced it slows.
- After you set the values, compare them with consensus in "consensus_comparison": the gap \
and its reason. Do not move a value to close the gap without a reason.
- Give one record in "assumption_records" for each of these fields (FCFF only): \
"segments.<name>.near_term_growth" for every segment, "near_term_op_margin", \
"terminal_op_margin", and "exit_multiple" (exit-multiple method) or "terminal_growth_g" \
(perpetuity). You may add records for the other numeric fields.
- Each record: anchor_class is "reported_fact" or "calculation" ONLY when the value or its \
inputs appear in the data above (name them, with the reported values beside it); \
"management_statement" needs the speaker and date; "external_estimate" needs the publisher, \
date and what was measured; anything else is "judgment" and cannot have high confidence. \
"counter_argument" is the strongest reason the value is wrong and its direction. \
"observable" is the disclosure and cadence that will test it, or "none". A capture gap \
listed above is not a reported fact.

{context}

VALUATION MODEL — FIRST decide which archetype fits {ticker}. The pipeline has these templates:
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
  "segments": {{ {", ".join(f'"{s}": {{"near_term_growth": 0.10, "terminal_growth": 0.03}}' for s in seg_keys)} }},
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
  "reasoning": "The path anchors to actual margins and a mature multiple.",
  "consensus_comparison": "Near-term growth is 1 pt below consensus because ...",
  "assumption_records": [{example_records}]
}}
terminal_method is exactly "Exit multiple" or "Perpetuity". Always fill exit_basis and \
exit_multiple (the cross-check multiple), also when terminal_method is "Perpetuity".
Growth must fade toward the terminal rate: near-term > terminal, except a declining \
segment (negative near-term), whose decline eases toward a terminal rate at or above it. \
For software/SaaS prefer EV/Sales or EV/EBITDA; for \
mature cash generators EV/FCF or EV/EBIT; pick the basis the market actually uses for {ticker}."""


def segment_guard(seg_keys: Sequence[str]) -> Guard:
    """Reject a result whose segments differ from the context or whose records are incomplete."""
    expected = frozenset(seg_keys)

    def guard(value: object) -> tuple[bool, str]:
        if not isinstance(value, DcfAssumptionsPayload):  # pragma: no cover - schema runs first
            return (False, "DCF assumptions are not the expected schema")
        actual = frozenset(value.segments)
        if actual != expected:
            return (False, f"expected segments {sorted(expected)}; got {sorted(actual)}")
        issues = value.assumption_record_issues()
        if issues:
            return (False, "; ".join(issues))
        return (True, "")

    return guard


def call_dcf_assumptions(
    ticker: str,
    context: str,
    seg_keys: Sequence[str],
    *,
    call: Callable[..., object] = call_llm_structured,
) -> DcfAssumptionsPayload:
    decoded = call(
        build_prompt(ticker, context, seg_keys),
        purpose=PURPOSE,
        ticker=ticker,
        scope=SCOPE,
        expect="object",
        schema=DCF_ASSUMPTIONS_SCHEMA,
        domain_guardrail=segment_guard(seg_keys),
        max_escalation_tier=0,
    )
    if not isinstance(decoded, DcfAssumptionsPayload):  # pragma: no cover
        raise TypeError("dcf_assumptions schema returned an unexpected value")
    return decoded
