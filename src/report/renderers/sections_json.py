"""ReportSpec → frontend sections JSON.

The shape is keyed by the existing UI tab id in static/index.html plus two
new tabs we propose adding ('financials', 'valuation'). Frontend code maps
each key directly to a panel; nothing else is needed.
"""

from __future__ import annotations

import json
from typing import Any

from report.models import ReportSpec


def render(spec: ReportSpec) -> str:
    payload: dict[str, Any] = {
        "ticker": spec.ticker,
        "generation_date": spec.generation_date.isoformat(),
        "run_id": spec.run_id,
        "summary_card": _summary_card(spec),
        "tabs": {
            "earnings": _earnings_tab(spec),
            "saydo": _saydo_tab(spec),
            "transcripts": _transcripts_tab(spec),
            "coverage": _coverage_tab(spec),
            "thesis_details": _thesis_tab(spec),
            "nuance": _nuance_tab(spec),
            "news": _news_tab(spec),
            "financials": _financials_tab(spec),
            "valuation": _valuation_tab(spec),
        },
        "report_links": {
            "html": "report.html",
            "markdown": "report.md",
            "workbook": "dcf.xlsx",
        },
    }
    return json.dumps(payload, indent=2, default=str)


def _summary_card(spec: ReportSpec) -> dict[str, Any]:
    s = spec.snapshot
    return {
        "ticker": s.ticker,
        "company_name": s.company_name,
        "verdict": s.verdict,
        "thesis_one_liner": s.thesis_one_liner,
        "valuation": s.valuation.model_dump(mode="json"),
        "tier_1_kpi_strip": [k.model_dump() for k in s.tier_1_kpi_row],
    }


def _earnings_tab(spec: ReportSpec) -> dict[str, Any]:
    return {
        "status": spec.earnings.status.value,
        "missing": spec.earnings.missing.model_dump() if spec.earnings.missing else None,
        "full_quarters": [c.model_dump() for c in spec.earnings.full_quarters],
        "digest_quarters": [c.model_dump() for c in spec.earnings.digest_quarters],
    }


def _saydo_tab(spec: ReportSpec) -> dict[str, Any]:
    s = spec.saydo
    return {
        "status": s.status.value,
        "missing": s.missing.model_dump() if s.missing else None,
        "cards": [c.model_dump() for c in s.cards],
    }


def _transcripts_tab(spec: ReportSpec) -> dict[str, Any]:
    """Per-quarter transcript metadata only — full text is in the HTML doc, not the JSON payload."""
    return {
        "status": spec.appendix.status.value,
        "quarters": [
            {
                "quarter": e.quarter,
                "year": e.year,
                "source_path": e.source_path,
                "char_count": len(e.text),
            }
            for e in spec.appendix.transcripts
        ],
    }


def _coverage_tab(spec: ReportSpec) -> dict[str, Any]:
    return {
        "status": spec.provenance.status.value,
        "rows": [r.model_dump() for r in spec.provenance.coverage],
    }


def _thesis_tab(spec: ReportSpec) -> dict[str, Any]:
    return {
        "status": spec.thesis.status.value,
        "thesis": spec.thesis.thesis_full,
        "last_updated": spec.thesis.last_updated.isoformat() if spec.thesis.last_updated else None,
        "break_conditions": spec.thesis.break_conditions,
        "kpi_ledger": [r.model_dump() for r in spec.thesis.kpi_ledger],
    }


def _nuance_tab(spec: ReportSpec) -> dict[str, Any]:
    """Map §9 (bear case) onto the existing 'Nuance' tab."""
    s = spec.bear_case
    return {
        "status": s.status.value,
        "missing": s.missing.model_dump() if s.missing else None,
        "failure_modes": [fm.model_dump() for fm in s.failure_modes],
        "most_underweighted": s.most_underweighted,
        "out_of_scope_flags": s.out_of_scope_flags,
    }


def _news_tab(spec: ReportSpec) -> dict[str, Any]:
    """News tab is fed by §8 Recent developments (WebSearch-driven brief)."""
    s = spec.recent_developments
    return {
        "status": s.status.value,
        "missing": s.missing.model_dump() if s.missing else None,
        "cached_at": s.cached_at.isoformat() if s.cached_at else None,
        "news_days_window": s.news_days_window,
        "content_md": s.content_md,
    }


def _financials_tab(spec: ReportSpec) -> dict[str, Any]:
    """New tab — proposed addition. Pre-shaped for a single-table render."""
    f = spec.financials
    return {
        "status": f.status.value,
        "missing": f.missing.model_dump() if f.missing else None,
        "quarter_labels": f.quarter_labels,
        "line_items": [li.model_dump() for li in f.line_items],
    }


def _valuation_tab(spec: ReportSpec) -> dict[str, Any]:
    """New tab — proposed addition. Snapshot lives in the summary card; the
    tab includes the segments grid + a link to the model file."""
    return {
        "valuation": spec.snapshot.valuation.model_dump(mode="json"),
        "segments": {
            "status": spec.segments.status.value,
            "quarter_labels": spec.segments.quarter_labels,
            "revenue_by_product": [s.model_dump() for s in spec.segments.revenue_by_product],
            "revenue_by_geography": [s.model_dump() for s in spec.segments.revenue_by_geography],
            "operating_income": [s.model_dump() for s in spec.segments.operating_income],
        },
    }
