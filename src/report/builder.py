"""Top-level report builder.

`build_report(ticker, repo_root)` orchestrates the section builders, returning
a fully-typed ReportSpec. Renderers consume the ReportSpec — there is no
direct DB / filesystem coupling in the renderer layer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from report.models import ReportSpec
from report.sections import (
    appendix,
    bear_case,
    earnings,
    financials,
    ir_docs,
    provenance,
    saydo,
    segments,
    snapshot,
    thesis,
)


def build_report(
    ticker: str,
    repo_root: Path,
    model_link: str | None = None,
    enable_llm: bool = False,
) -> ReportSpec:
    """Build the unified ReportSpec for one ticker.

    `model_link` is the path-or-URL to the DCF workbook the snapshot card
    should point at. Caller passes it because the workbook isn't written
    until after `build_report()` returns.

    `enable_llm` opts the bear-case section into a real Gemini call. Default
    off so dev runs don't spend tokens or require GEMINI_API_KEY.
    """
    ticker = ticker.upper()
    snapshot_section = snapshot.build(ticker, repo_root, model_link)
    thesis_section = thesis.build(ticker, repo_root)
    financials_section = financials.build(ticker, repo_root)
    segments_section = segments.build(ticker, repo_root)
    earnings_section = earnings.build(ticker, repo_root)
    saydo_section = saydo.build(ticker, repo_root)
    ir_docs_section = ir_docs.build(ticker, repo_root)
    bear_case_section = bear_case.build(
        ticker=ticker,
        repo_root=repo_root,
        enable_llm=enable_llm,
        thesis=thesis_section,
        financials=financials_section,
        segments=segments_section,
        earnings=earnings_section,
    )
    provenance_section = provenance.build(ticker, repo_root)
    appendix_section = appendix.build(earnings_section)

    return ReportSpec(
        ticker=ticker,
        generation_date=date.today(),
        repo_root=str(repo_root),
        snapshot=snapshot_section,
        thesis=thesis_section,
        financials=financials_section,
        segments=segments_section,
        earnings=earnings_section,
        saydo=saydo_section,
        ir_docs=ir_docs_section,
        bear_case=bear_case_section,
        provenance=provenance_section,
        appendix=appendix_section,
    )
