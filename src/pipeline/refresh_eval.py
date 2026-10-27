"""Auto-trigger hook: derive KPIs and re-evaluate thesis after new data lands.

Called by extract_facts and extract_kpis_from_ir when --with-eval is set.
Sequence per ticker:
  1. fmp_derived_kpis.derive_for_ticker_outcome (idempotent — only inserts new rows)
  2. thesis_evaluator.evaluate_ticker_thesis + persist_verdict (appends history row)

Skips tickers whose holdings JSON is missing (e.g. tracked but not thesis-tracked).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from compute.fmp_derived_kpis import FmpDerivationOutcome, derive_for_ticker_outcome
from compute.thesis_evaluator import evaluate_ticker_thesis, persist_verdict
from models.kpis import BreachStatus


@dataclass(frozen=True)
class TickerRefreshResult:
    """Per-ticker outcome of the refresh chain."""

    ticker: str
    derived_kpi_rows_inserted: int
    eval_status: BreachStatus | None
    eval_error: str | None
    derivation: FmpDerivationOutcome | None = None


def refresh_for_tickers(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    holdings_dir: Path,
    run_id: str | None = None,
) -> list[TickerRefreshResult]:
    """Run derive + evaluate for each ticker. Failures are captured per ticker, not raised."""
    results: list[TickerRefreshResult] = []
    for ticker in tickers:
        derived_inserted = 0
        try:
            derivation = derive_for_ticker_outcome(conn, ticker)
            derived_inserted = derivation.rows_inserted
        except (ValueError, KeyError) as e:
            results.append(
                TickerRefreshResult(
                    ticker=ticker.upper(),
                    derived_kpi_rows_inserted=0,
                    eval_status=None,
                    eval_error=f"derive_for_ticker_outcome failed: {type(e).__name__}: {e}"[:300],
                )
            )
            continue

        try:
            verdict = evaluate_ticker_thesis(conn, ticker=ticker, holdings_dir=holdings_dir)
        except FileNotFoundError:
            results.append(
                TickerRefreshResult(
                    ticker=ticker.upper(),
                    derived_kpi_rows_inserted=derived_inserted,
                    derivation=derivation,
                    eval_status=None,
                    eval_error="holdings_spec_not_found",
                )
            )
            continue
        except (ValueError, KeyError) as e:
            results.append(
                TickerRefreshResult(
                    ticker=ticker.upper(),
                    derived_kpi_rows_inserted=derived_inserted,
                    derivation=derivation,
                    eval_status=None,
                    eval_error=f"evaluate failed: {type(e).__name__}: {e}"[:300],
                )
            )
            continue

        persist_verdict(conn, verdict, run_id=run_id, holdings_dir=holdings_dir)
        results.append(
            TickerRefreshResult(
                ticker=ticker.upper(),
                derived_kpi_rows_inserted=derived_inserted,
                derivation=derivation,
                eval_status=verdict.overall_status,
                eval_error=None,
            )
        )
    return results
