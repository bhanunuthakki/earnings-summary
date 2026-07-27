"""§7.5 Filing Intelligence — read the cached buy-side 10-K narrative synthesis.

The heavy lifting (Focus Algorithm + LLM call + Pydantic validation + cache write)
lives in ``execution/analyze_filing_intelligence.py``. This section just reads
``data/filing_intelligence/<TICKER>.json`` and returns the structured slice the
renderers consume. When the cache is absent we surface a MISSING_DATA stub
pointing at the fix command; when it exists but can't be parsed we surface a
PARTIAL stub pointing at ``--refresh``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from report.models import (
    ExecutiveCompAlignmentDetail,
    FilingIntelligenceSection,
    InvestmentSignalDetail,
    MetricRedefinitionDetail,
    SectionStatus,
    SegmentChangeDetail,
)
from report.sections._common import missing


def _data_anchor(ticker: str, repo_root: Path) -> str:
    """Read `data_anchor` from holdings JSON; defaults to "10k"."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return "10k"
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return "10k"
    raw = payload.get("data_anchor")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return "10k"


def build(ticker: str, repo_root: Path) -> FilingIntelligenceSection:
    ticker = ticker.upper()
    cache_path = repo_root / "data" / "filing_intelligence" / f"{ticker}.json"

    # Recently-IPO'd issuers anchor on the S-1 and have no 10-K to extract
    # segment / metric / exec-comp signals from. Mark NOT_APPLICABLE so the
    # renderer doesn't surface a broken fix-command pointing at a script that
    # won't find any 10-K JSON.
    if _data_anchor(ticker, repo_root) == "s1":
        return FilingIntelligenceSection(
            status=SectionStatus.NOT_APPLICABLE,
            ticker=ticker,
            missing=missing(
                stage="SYNTHESIZE(analyze_filing_intelligence)",
                fix_command=f"# wait for first 10-K filing for {ticker}",
                detail=(
                    "Recently-IPO'd issuer — narrative is anchored on the S-1. "
                    "Filing-intelligence extraction (segment shifts, metric redefinitions, "
                    "exec-comp alignment, tail risks) requires a 10-K and a prior-year baseline."
                ),
            ),
        )

    if not cache_path.exists():
        return FilingIntelligenceSection(
            status=SectionStatus.MISSING_DATA,
            ticker=ticker,
            missing=missing(
                stage="SYNTHESIZE(analyze_filing_intelligence)",
                fix_command=f"python execution/analyze_filing_intelligence.py --ticker {ticker}",
                detail="Filing intelligence cache not found — run the synthesizer to populate it.",
            ),
        )

    try:
        data = cast("dict[str, object]", json.loads(cache_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        return FilingIntelligenceSection(
            status=SectionStatus.PARTIAL,
            ticker=ticker,
            missing=missing(
                stage="SYNTHESIZE(analyze_filing_intelligence)",
                fix_command=f"python execution/analyze_filing_intelligence.py --ticker {ticker} --refresh",
                detail=f"Cache read/parse failure: {exc}",
            ),
        )

    summary = cast("dict[str, object]", data.get("summary") or {})
    if not summary:
        return FilingIntelligenceSection(
            status=SectionStatus.PARTIAL,
            ticker=ticker,
            missing=missing(
                stage="SYNTHESIZE(analyze_filing_intelligence)",
                fix_command=f"python execution/analyze_filing_intelligence.py --ticker {ticker} --refresh",
                detail="Cache present but `summary` payload is missing — likely a skipped synthesis.",
            ),
        )

    seg_raw = cast("dict[str, object]", summary.get("segment_changes") or {})
    metric_raw = cast("dict[str, object]", summary.get("metric_redefinitions") or {})
    comp_raw = cast("dict[str, object]", summary.get("executive_comp") or {})
    signals_raw = cast("list[object]", summary.get("investment_signals") or [])

    fiscal_year = data.get("fiscal_year")
    analyzed_at = data.get("analyzed_at") or summary.get("analyzed_at")

    return FilingIntelligenceSection(
        status=SectionStatus.OK,
        ticker=ticker,
        fiscal_year=int(fiscal_year) if isinstance(fiscal_year, int) else None,
        analyzed_at=str(analyzed_at) if isinstance(analyzed_at, str) else None,
        segment_changes=SegmentChangeDetail(
            has_changes=bool(seg_raw.get("has_changes", False)),
            description=_str_or_none(seg_raw.get("description")),
        ),
        metric_redefinitions=MetricRedefinitionDetail(
            has_changes=bool(metric_raw.get("has_changes", False)),
            description=_str_or_none(metric_raw.get("description")),
        ),
        executive_comp=ExecutiveCompAlignmentDetail(
            metrics_used=[
                s
                for s in cast("list[object]", comp_raw.get("metrics_used") or [])
                if isinstance(s, str)
            ],
            targets_and_thresholds=_str_or_none(comp_raw.get("targets_and_thresholds")),
            alignment_verdict=_str_or_none(comp_raw.get("alignment_verdict")),
        ),
        investment_signals=[_coerce_signal(s) for s in signals_raw if isinstance(s, dict)],
        raw_synthesis_md=_str_or_none(summary.get("raw_synthesis_md")),
    )


def _coerce_signal(raw: object) -> InvestmentSignalDetail:
    obj = cast("dict[str, object]", raw)
    severity_raw = str(obj.get("severity") or "Low")
    severity = severity_raw if severity_raw in ("High", "Medium", "Low") else "Low"
    return InvestmentSignalDetail(
        signal_type=str(obj.get("signal_type") or "Unspecified"),
        severity=cast("Literal['High', 'Medium', 'Low']", severity),
        description=str(obj.get("description") or ""),
    )


def _str_or_none(v: object) -> str | None:
    return v if isinstance(v, str) and v.strip() else None
