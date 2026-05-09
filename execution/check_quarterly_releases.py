"""
execution/check_quarterly_releases.py
-------------------------------------
Layer 3 orchestrator. Designed to run on a monthly cron (Windows Task
Scheduler — see directives/check_quarterly_releases.md).

For every ticker in the portfolio:
  1. Pull the last N quarterly reports from FMP `/stable/income-statement`
     (authoritative source for (fiscalYear, period) labels).
  2. Augment with FMP `/stable/earnings` per-symbol — that gives us the
     announcement date so we can filter to "reported in the last --days".
  3. For each recent, actually-filed report: skip if we already have a
     transcript with qa=ok. Otherwise:
        a) Try `fetch_qa_transcript.py` (free aggregator chain — cheap).
        b) Optionally try `fetch_audio_transcripts.py` smart-search (only
           when `--with-audio-fallback` is set; off by default because
           ytsearch5 occasionally picks the wrong upload).
  4. Write a per-run report to `.tmp/cron_runs/<timestamp>.json`.

The script is idempotent — re-running with the same date window is safe;
already-have transcripts are skipped via the QA-aware index gate that lives
inside `fetch_qa_transcript.fetch_qa` and `fetch_audio_transcripts.fetch_and_transcribe`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))
sys.path.append(str(SCRIPT_DIR))  # so we can import sibling execution scripts

from alias_manager import resolve_ticker  # noqa: E402
import index_manager  # noqa: E402
from portfolio import PortfolioEntry, get_portfolio  # noqa: E402
from fetch_qa_transcript import FetchQaSpec, fetch_qa  # noqa: E402
from fetch_audio_transcripts import (  # noqa: E402
    FetchSpec as AudioFetchSpec,
    fetch_and_transcribe as audio_fetch_and_transcribe,
    _resolve_ffmpeg_location,  # type: ignore[attr-defined]
)

load_dotenv(PROJECT_ROOT / ".env")
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_BASE = "https://financialmodelingprep.com/stable"
HTTP_TIMEOUT = (10, 30)

CRON_RUNS_DIR = PROJECT_ROOT / ".tmp" / "cron_runs"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RecentReport(BaseModel):
    ticker: str
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    period_end: str           # YYYY-MM-DD (income-statement.date)
    filing_date: str          # YYYY-MM-DD (income-statement.filingDate)
    announce_date: str | None # YYYY-MM-DD (earnings.date) — for --days filter


class QuarterStatus(BaseModel):
    ticker: str
    fiscal_year: int
    fiscal_quarter: int
    period_end: str
    filing_date: str
    announce_date: str | None
    action: str          # "skipped_have_qa_ok" | "aggregator_ok" | "audio_ok" | "miss" | "error"
    source: str | None   # source label written to index, if any
    qa_status: str | None
    error: str | None = None


class RunReport(BaseModel):
    run_id: str
    started_at: str
    completed_at: str
    tickers_polled: int
    days_window: int
    audio_fallback_enabled: bool
    quarters_examined: list[QuarterStatus]


# ---------------------------------------------------------------------------
# FMP fetchers
# ---------------------------------------------------------------------------


def _fmp_get(endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if not FMP_API_KEY:
        raise RuntimeError("FMP_API_KEY not set in .env")
    url = f"{FMP_BASE}/{endpoint}"
    full_params = {**params, "apikey": FMP_API_KEY}
    r = requests.get(url, params=full_params, timeout=HTTP_TIMEOUT)
    if r.status_code in (401, 403):
        raise RuntimeError(f"FMP {endpoint} auth error {r.status_code}: {r.text[:120]}")
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"FMP {endpoint} returned non-list payload")
    return payload


def fmp_quarterly_reports(ticker: str, limit: int = 8) -> list[RecentReport]:
    rows = _fmp_get("income-statement", {"symbol": ticker, "period": "quarter", "limit": limit})
    earnings_rows = _fmp_get("earnings", {"symbol": ticker, "limit": max(limit, 12)})
    # earnings rows are forward+historical; index by date for cheap lookup.
    earn_by_date: dict[str, str] = {row["date"]: row["date"] for row in earnings_rows if row.get("date")}

    out: list[RecentReport] = []
    for row in rows:
        period_end = row.get("date")
        period_label = row.get("period")
        fiscal_year = row.get("fiscalYear")
        filing_date = row.get("filingDate")
        if not (period_end and period_label and fiscal_year and filing_date):
            continue
        if not (isinstance(period_label, str) and period_label.startswith("Q")):
            continue
        try:
            quarter_num = int(period_label[1:])
        except ValueError:
            continue
        # Match earnings calendar by filing-date proximity; FMP frequently
        # publishes the announcement on the same day as the filing.
        announce_date = earn_by_date.get(filing_date)
        out.append(
            RecentReport(
                ticker=ticker.upper(),
                fiscal_year=int(fiscal_year),
                fiscal_quarter=quarter_num,
                period_end=str(period_end),
                filing_date=str(filing_date),
                announce_date=announce_date,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Decision + dispatch
# ---------------------------------------------------------------------------


def _qa_status_in_index(canonical: str, year: int, quarter: int) -> str | None:
    qlabel = f"Q{quarter}"
    entry = index_manager.has_transcript(canonical, year, qlabel)
    if entry is None:
        return None
    return entry.get("qa_status")


def _try_aggregator(report: RecentReport) -> tuple[bool, str | None, str | None]:
    """Returns (success, source_label, qa_status)."""
    spec = FetchQaSpec(
        ticker=report.ticker, year=report.fiscal_year, quarter=report.fiscal_quarter
    )
    try:
        result = fetch_qa(spec, force=False)
    except Exception as e:
        return (False, None, f"aggregator-error: {type(e).__name__}: {e}")
    if result is None:
        return (False, None, None)
    canonical = resolve_ticker(report.ticker)
    qa_status = _qa_status_in_index(canonical, report.fiscal_year, report.fiscal_quarter)
    return (qa_status == "ok", f"aggregator_{result.source_name}", qa_status)


def _try_audio(report: RecentReport, ffmpeg_location: Path | None) -> tuple[bool, str | None, str | None]:
    """Returns (success, source_label, qa_status)."""
    spec = AudioFetchSpec(
        ticker=report.ticker, year=report.fiscal_year, quarter=report.fiscal_quarter,
    )
    try:
        result = audio_fetch_and_transcribe(spec, ffmpeg_location)
    except Exception as e:
        return (False, None, f"audio-error: {type(e).__name__}: {e}")
    if result is None:
        # already-exists path or smart-search miss
        canonical = resolve_ticker(report.ticker)
        qa_status = _qa_status_in_index(canonical, report.fiscal_year, report.fiscal_quarter)
        if qa_status == "ok":
            return (True, "audio_skip_existing", qa_status)
        return (False, None, qa_status)
    canonical = resolve_ticker(report.ticker)
    qa_status = _qa_status_in_index(canonical, report.fiscal_year, report.fiscal_quarter)
    return (qa_status == "ok", result.source.value, qa_status)


def _within_window(report: RecentReport, today: _dt.date, days_window: int) -> bool:
    ref = report.announce_date or report.filing_date
    try:
        ref_date = _dt.date.fromisoformat(ref)
    except Exception:
        return False
    if ref_date > today:
        return False  # not yet reported
    return (today - ref_date).days <= days_window


def _examine(
    report: RecentReport,
    ffmpeg_location: Path | None,
    audio_fallback: bool,
) -> QuarterStatus:
    canonical = resolve_ticker(report.ticker)
    existing_status = _qa_status_in_index(canonical, report.fiscal_year, report.fiscal_quarter)
    base = dict(
        ticker=canonical,
        fiscal_year=report.fiscal_year,
        fiscal_quarter=report.fiscal_quarter,
        period_end=report.period_end,
        filing_date=report.filing_date,
        announce_date=report.announce_date,
    )
    if existing_status == "ok":
        return QuarterStatus(**base, action="skipped_have_qa_ok", source=None, qa_status="ok")

    print(f"[{canonical} Q{report.fiscal_quarter} FY{report.fiscal_year}] aggregator chain…")
    ok, source, qa = _try_aggregator(report)
    if ok:
        return QuarterStatus(**base, action="aggregator_ok", source=source, qa_status=qa)

    if audio_fallback:
        print(f"[{canonical} Q{report.fiscal_quarter} FY{report.fiscal_year}] aggregator missed — audio fallback…")
        ok, source, qa = _try_audio(report, ffmpeg_location)
        if ok:
            return QuarterStatus(**base, action="audio_ok", source=source, qa_status=qa)

    print(f"[{canonical} Q{report.fiscal_quarter} FY{report.fiscal_year}] MISS — flagged for review")
    return QuarterStatus(**base, action="miss", source=source, qa_status=qa)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Monthly check for new portfolio earnings releases.")
    parser.add_argument(
        "--days", type=int, default=45,
        help="Window (in days) of recent reports to consider. Default 45 — covers a monthly cron with a 2-week safety overlap.",
    )
    parser.add_argument(
        "--limit-quarters", type=int, default=4,
        help="How many quarterly reports per ticker to pull from FMP (filter is then applied).",
    )
    parser.add_argument(
        "--with-audio-fallback", action="store_true",
        help="On aggregator miss, also try fetch_audio_transcripts smart-search. OFF by default.",
    )
    parser.add_argument(
        "--ticker", help="Restrict the run to one ticker (debug / manual reruns).",
    )
    parser.add_argument(
        "--include-watchlist", action="store_true",
        help="Include watchlist tickers in addition to portfolio (default: portfolio only).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without calling any fetcher.",
    )
    args = parser.parse_args()

    started_at = _dt.datetime.now(_dt.UTC)
    today = started_at.date()
    run_id = f"check_quarterly_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    print(f"[{run_id}] window={args.days}d audio_fallback={args.with_audio_fallback} dry_run={args.dry_run}")

    portfolio: list[PortfolioEntry]
    if args.ticker:
        from portfolio import lookup
        entry = lookup(args.ticker)
        if entry is None:
            parser.error(f"--ticker {args.ticker} not in portfolio")
        portfolio = [entry]
    else:
        portfolio = get_portfolio(include_watchlist=args.include_watchlist)
    print(
        f"[scope] {len(portfolio)} ticker(s): "
        f"{', '.join(e.ticker for e in portfolio)}"
    )

    ffmpeg_location = _resolve_ffmpeg_location(None)

    results: list[QuarterStatus] = []
    for entry in portfolio:
        try:
            recent = fmp_quarterly_reports(entry.ticker, limit=args.limit_quarters)
        except Exception as e:
            print(f"[{entry.ticker}] FMP fetch failed: {type(e).__name__}: {e}")
            results.append(QuarterStatus(
                ticker=entry.ticker, fiscal_year=0, fiscal_quarter=1,
                period_end="", filing_date="", announce_date=None,
                action="error", source=None, qa_status=None,
                error=f"fmp-fetch-failed: {type(e).__name__}: {e}",
            ))
            continue

        recent_in_window = [r for r in recent if _within_window(r, today, args.days)]
        if not recent_in_window:
            print(f"[{entry.ticker}] no reports filed in last {args.days}d (newest: "
                  f"{recent[0].filing_date if recent else 'none'})")
            continue

        for report in recent_in_window:
            if args.dry_run:
                qa = _qa_status_in_index(resolve_ticker(report.ticker), report.fiscal_year, report.fiscal_quarter)
                action = "skipped_have_qa_ok" if qa == "ok" else "would_fetch"
                print(f"  [dry] {report.ticker} Q{report.fiscal_quarter} FY{report.fiscal_year} "
                      f"period_end={report.period_end} filing={report.filing_date} -> {action}")
                results.append(QuarterStatus(
                    ticker=resolve_ticker(report.ticker),
                    fiscal_year=report.fiscal_year, fiscal_quarter=report.fiscal_quarter,
                    period_end=report.period_end, filing_date=report.filing_date,
                    announce_date=report.announce_date,
                    action=action, source=None, qa_status=qa,
                ))
                continue
            try:
                status = _examine(report, ffmpeg_location, args.with_audio_fallback)
            except Exception as e:
                print(f"  [error] {report.ticker} Q{report.fiscal_quarter} FY{report.fiscal_year}: {type(e).__name__}: {e}")
                traceback.print_exc(file=sys.stderr)
                status = QuarterStatus(
                    ticker=resolve_ticker(report.ticker),
                    fiscal_year=report.fiscal_year, fiscal_quarter=report.fiscal_quarter,
                    period_end=report.period_end, filing_date=report.filing_date,
                    announce_date=report.announce_date,
                    action="error", source=None, qa_status=None,
                    error=f"{type(e).__name__}: {e}",
                )
            results.append(status)

    completed_at = _dt.datetime.now(_dt.UTC)
    report = RunReport(
        run_id=run_id,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        tickers_polled=len(portfolio),
        days_window=args.days,
        audio_fallback_enabled=args.with_audio_fallback,
        quarters_examined=results,
    )
    CRON_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CRON_RUNS_DIR / f"{run_id}.json"
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    summary = {"skipped_have_qa_ok": 0, "aggregator_ok": 0, "audio_ok": 0, "miss": 0, "error": 0, "would_fetch": 0}
    for s in results:
        summary[s.action] = summary.get(s.action, 0) + 1
    print(f"\n[summary] " + "  ".join(f"{k}={v}" for k, v in summary.items() if v))
    print(f"[report]  {out_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
