"""§7 Bear case — strategically deep, structured, LLM-driven.

When `enable_llm` is False (default for dev runs), returns LLM_PENDING with
the prompt template embedded for review. When True, assembles inputs from
the upstream sections, calls llm_client.generate_bear_case, and parses the
JSON into FailureMode rows. Any LLM / parsing error surfaces — no silent
stub on failure (per repo's "fail loudly" rule).
"""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path
from typing import cast

from llm_client import generate_bear_case
from report.models import (
    BearCaseSection,
    EarningsSection,
    FailureMode,
    FinancialsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
)
from report.sections._common import missing


def build(
    ticker: str,
    repo_root: Path,
    enable_llm: bool,
    thesis: ThesisSection,
    financials: FinancialsSection,
    segments: SegmentsSection,
    earnings: EarningsSection,
) -> BearCaseSection:
    if not enable_llm:
        return BearCaseSection(
            status=SectionStatus.LLM_PENDING,
            missing=missing(
                stage="SYNTHESIZE(bear_case_llm)",
                fix_command=(
                    f"python execution/build_artifacts.py --ticker {ticker.upper()} --enable-llm"
                ),
                detail="Pass --enable-llm to populate this section (requires GEMINI_API_KEY).",
            ),
        )

    if thesis.status == SectionStatus.MISSING_DATA or not thesis.thesis_full:
        return BearCaseSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(holdings)",
                fix_command=f"create micro_thesis/holdings/{ticker.upper()}.json",
                detail="Bear case requires a thesis + break conditions to ground itself.",
            ),
        )

    response_text = generate_bear_case(
        ticker=ticker,
        thesis=thesis.thesis_full or "",
        break_conditions=thesis.break_conditions,
        last_quarter_summaries=_last_summaries(earnings, n=4),
        financials_table_md=_financials_md(financials),
        segments_table_md=_segments_md(segments),
        kpi_status_md=_kpi_status_md(thesis),
    )
    return _parse_response(response_text)


def _parse_response(text: str) -> BearCaseSection:
    """LLM occasionally wraps JSON in ``` fences; strip and parse."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError(f"Bear case response was not a JSON object: {type(payload).__name__}")

    failure_modes_raw = payload.get("failure_modes") or []
    failure_modes = [FailureMode(**_coerce_failure_mode(fm)) for fm in failure_modes_raw]
    return BearCaseSection(
        status=SectionStatus.OK,
        failure_modes=failure_modes,
        most_underweighted=_str_or_none(payload.get("most_underweighted")),
        out_of_scope_flags=[s for s in (payload.get("out_of_scope_flags") or []) if isinstance(s, str)],
    )


def _coerce_failure_mode(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"failure_mode must be an object; got {type(raw).__name__}")
    keys = ("hypothesis", "evidence_in_data", "leading_indicator", "quantitative_impact", "refutation_criteria")
    return {k: str(raw.get(k, "")) for k in keys}


def _str_or_none(v: object) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def _last_summaries(earnings: EarningsSection, n: int) -> list[str]:
    """Pull the most recent N full LLM summaries for the prompt."""
    cards = (list(earnings.digest_quarters) + list(earnings.full_quarters))[-n:]
    return [c.summary_md for c in cards if c.summary_md]


def _financials_md(financials: FinancialsSection) -> str:
    if financials.status == SectionStatus.MISSING_DATA:
        return "(not yet extracted)"
    out = StringIO()
    out.write("| Line item | " + " | ".join(financials.quarter_labels) + " | QoQ | YoY | 1Y CAGR | 3Y CAGR |\n")
    out.write("|" + "|".join(["---"] * (len(financials.quarter_labels) + 5)) + "|\n")
    for li in financials.line_items:
        cells = [li.line_item] + [_fmt(v, li.digits) for v in li.values]
        cells.extend(_pct(g) for g in (li.growth.qoq, li.growth.yoy, li.growth.cagr_1y_ttm, li.growth.cagr_3y_ttm))
        out.write("| " + " | ".join(cells) + " |\n")
    return out.getvalue()


def _segments_md(segments: SegmentsSection) -> str:
    if segments.status == SectionStatus.MISSING_DATA:
        return "(not yet extracted)"
    out = StringIO()
    for label, group in (
        ("Revenue by product", segments.revenue_by_product),
        ("Operating income", segments.operating_income),
    ):
        if not group:
            continue
        out.write(f"\n**{label}**\n")
        out.write("| Segment | " + " | ".join(segments.quarter_labels) + " | YoY |\n")
        out.write("|" + "|".join(["---"] * (len(segments.quarter_labels) + 2)) + "|\n")
        for s in group:
            cells = [s.segment_name] + [_fmt(v, 0) for v in s.values] + [_pct(s.growth.yoy)]
            out.write("| " + " | ".join(cells) + " |\n")
    return out.getvalue()


def _kpi_status_md(thesis: ThesisSection) -> str:
    out = StringIO()
    out.write("| Tier | KPI | Break condition | Current status |\n|---|---|---|---|\n")
    for k in thesis.kpi_ledger:
        out.write(f"| {k.tier} | {k.name} | {k.break_condition or '—'} | {k.current_status} |\n")
    return out.getvalue()


def _fmt(v: float | None, digits: int) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


# Kept for documentation parity with v0 — the real prompt is in llm_client.
PROMPT_TEMPLATE = cast(str, generate_bear_case.__doc__) or ""
