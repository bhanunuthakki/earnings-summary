"""Top-level report builder.

`build_report(ticker, repo_root)` orchestrates the section builders, returning
a fully-typed ReportSpec. Renderers consume the ReportSpec — there is no
direct DB / filesystem coupling in the renderer layer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from report.models import ReportFlavor, ReportSpec
from report.sections import (
    appendix,
    bear_case,
    company_description,
    earnings,
    evaluation_snapshot,
    financials,
    ir_docs,
    portfolio_position,
    provenance,
    recent_developments,
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
    news_days: int = 7,
    news_cache_ttl_days: int = 7,
    refresh_news: bool = False,
    flavor: ReportFlavor = ReportFlavor.PORTFOLIO,
) -> ReportSpec:
    """Build the unified ReportSpec for one ticker.

    `model_link` is the path-or-URL to the DCF workbook the snapshot card
    should point at. Caller passes it because the workbook isn't written
    until after `build_report()` returns.

    `enable_llm` opts the bear-case AND recent-developments sections into real
    LLM calls (Claude Sonnet 4.6 via the Code CLI, Gemini Flash as automatic
    fallback). Default off so dev runs don't burn subscription quota or
    wall-time on the synthesis step.

    `news_days` is the WebSearch lookback window for the §9 recent-developments
    section. `news_cache_ttl_days` controls how long that section's on-disk
    cache stays fresh between regenerations; `refresh_news=True` bypasses
    the cache for this build.

    `flavor` controls the §1 shape. PORTFOLIO renders the full Snapshot
    (thesis verdict, KPI strip). EVALUATION builds an additional
    EvaluationSnapshot (3y quick-categorization data table) which renderers
    use in §1 instead of Snapshot — for new-name screening.
    """
    ticker = ticker.upper()
    portfolio_position_section = portfolio_position.build(ticker, repo_root)
    snapshot_section = snapshot.build(ticker, repo_root, model_link)
    evaluation_snapshot_section = (
        evaluation_snapshot.build(ticker, repo_root) if flavor == ReportFlavor.EVALUATION else None
    )
    company_description_section = company_description.build(ticker, repo_root)
    thesis_section = thesis.build(ticker, repo_root)
    financials_section = financials.build(ticker, repo_root)
    segments_section = segments.build(ticker, repo_root)
    earnings_section = earnings.build(ticker, repo_root)
    saydo_section = saydo.build(ticker, repo_root)
    ir_docs_section = ir_docs.build(ticker, repo_root)
    recent_developments_section = recent_developments.build(
        ticker=ticker,
        repo_root=repo_root,
        enable_llm=enable_llm,
        news_days=news_days,
        cache_ttl_days=news_cache_ttl_days,
        force_refresh=refresh_news,
    )
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
        flavor=flavor,
        portfolio_position=portfolio_position_section,
        snapshot=snapshot_section,
        evaluation_snapshot=evaluation_snapshot_section,
        company_description=company_description_section,
        thesis=thesis_section,
        financials=financials_section,
        segments=segments_section,
        earnings=earnings_section,
        saydo=saydo_section,
        ir_docs=ir_docs_section,
        recent_developments=recent_developments_section,
        bear_case=bear_case_section,
        provenance=provenance_section,
        appendix=appendix_section,
    )
