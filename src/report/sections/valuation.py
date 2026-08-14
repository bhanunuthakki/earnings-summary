"""§Valuation tab section builder. Pure adapter: pulls the cached compute
output from `compute.valuation_basis` and shapes it into the
`ValuationBasisSection` Pydantic model the renderer consumes. No display
logic here; no analytical logic here either — that's all in the compute
layer."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from compute import valuation_basis as compute_valuation
from llm_client import is_hard_stop
from report.models import (
    SectionStatus,
    ValuationBasisHistoricalPoint,
    ValuationBasisSection,
)
from report.sections._common import budget_gate, budget_skip_missing, missing, open_repo_db


def build(
    ticker: str,
    repo_root: Path,
    enable_llm: bool,
    force_budget_bypass: bool = False,
    force_refresh: bool = False,
    conn: sqlite3.Connection | None = None,
) -> ValuationBasisSection:
    if not enable_llm:
        cached = compute_valuation.load(repo_root, ticker)
        if cached is None or cached.multiple_name is None:
            return ValuationBasisSection(
                status=SectionStatus.LLM_PENDING,
                missing=missing(
                    stage="SYNTHESIZE(valuation_basis_llm)",
                    fix_command=(
                        f"python execution/build_artifacts.py --ticker {ticker.upper()} "
                        f"--enable-llm"
                    ),
                    detail=(
                        "Valuation tab needs Opus to pick the right multiple for this "
                        "ticker. Re-run with --enable-llm to populate."
                    ),
                ),
            )
        return _to_section(cached)

    skip = budget_gate(
        "valuation_basis", "Valuation (multiple)", repo_root, bypass=force_budget_bypass
    )
    if skip is not None:
        # A previously-cached multiple is free to render — prefer it over forgoing.
        cached = compute_valuation.load(repo_root, ticker)
        if cached is not None and cached.multiple_name is not None:
            return _to_section(cached)
        return ValuationBasisSection(
            status=SectionStatus.BUDGET_SKIPPED,
            budget_skip=skip,
            missing=budget_skip_missing(ticker, "valuation_basis", skip),
        )

    db_conn = open_repo_db(repo_root, conn)
    try:
        if db_conn is None:
            return ValuationBasisSection(
                status=SectionStatus.MISSING_DATA,
                missing=missing(
                    stage="INGEST(portfolio_db)",
                    fix_command=f"python execution/backfill_fmp.py --ticker {ticker.upper()}",
                    detail="No portfolio.db on disk; cannot compute valuation basis.",
                ),
            )
        result = compute_valuation.extract_for_ticker(
            ticker,
            repo_root,
            db_conn,
            refresh=force_refresh,
        )
    except Exception as e:
        # Hard stops (monthly budget cap, CLI not installed) propagate — the
        # LLM multiple-picker runs through call_llm inside the compute layer,
        # so a budget cap surfaces here and must not be masked as MISSING_DATA.
        if is_hard_stop(e):
            raise
        return ValuationBasisSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(valuation_basis_llm)",
                fix_command=(
                    f"python execution/build_artifacts.py --ticker {ticker.upper()} "
                    f"--enable-llm --refresh"
                ),
                detail=f"Compute layer raised: {type(e).__name__}: {e}",
            ),
        )
    finally:
        if db_conn is not None and conn is None:
            db_conn.close()

    if result.skipped_reason or result.multiple_name is None:
        return ValuationBasisSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(fmp_key_metrics)",
                fix_command=(f"python execution/backfill_fmp.py --ticker {ticker.upper()}"),
                detail=(result.skipped_reason or "compute layer returned no valuation multiple."),
            ),
        )
    return _to_section(result)


def _to_section(r: compute_valuation.ValuationBasisResult) -> ValuationBasisSection:
    history = [
        ValuationBasisHistoricalPoint(
            period_end=period_end,
            value=h.value,
        )
        for h in r.history
        if h.period_end and (period_end := _as_date(h.period_end)) is not None
    ]
    return ValuationBasisSection(
        status=SectionStatus.OK,
        multiple_name=r.multiple_name,
        rationale=r.rationale,
        current_value=r.current_value,
        current_value_display=r.current_value_display,
        peg_ratio=r.peg_ratio,
        peg_growth_pct=r.peg_growth_pct,
        current_period_end=_as_date(r.current_period_end),
        history=history,
        historical_min=r.historical_min,
        historical_max=r.historical_max,
        historical_median=r.historical_median,
        rich_cheap_verdict=r.rich_cheap_verdict,
        notes=r.target_band or r.notes,
    )


def _as_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None
