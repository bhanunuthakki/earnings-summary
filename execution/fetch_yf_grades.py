"""execution/fetch_yf_grades.py — additive sell-side rating-changes feed (yfinance).

Maps ``yfinance.Ticker(t).upgrades_downgrades`` rows — keyless, free — into
validated ``NewsRow``s tagged ``source_feed='yf_grades'``, the dedicated grades
feed the inbox categorizer routes to "Rating changes" (the forward-compat hook
src/dashboard/inbox_rank.py shipped for ``fmp_grades`` extends to this tag).

Verified live 2026-06-11 (directives/news_sources_plan.md §1a): the frame is
indexed by ``GradeDate`` (UTC) with columns Firm / ToGrade / FromGrade / Action
(``up|down|init|main|reit``) / priceTargetAction / currentPriceTarget /
priorPriceTarget. Known upstream gaps exist per ticker (META frozen at
2024-09), so this feed is ADDITIVE coverage — never proof of "no rating
activity"; the FMP headline-regex leg keeps catching what Yahoo misses.

Headlines are deliberately shaped like the wire convention the categorizer's
``_RATING_HEADLINE_RX`` already matches ("X upgrades T to Buy from Hold;
PT $570 → $620"). Yahoo provides no per-event URL, so a stable synthetic
fragment on the ticker's analysis page supplies the uniqueness the table's
``(ticker, url)`` key needs while still being clickable.

yfinance is an unofficial API: every failure path (not installed, transport,
shape drift) degrades to ``[]`` — this feed must never block the primary feeds
or the morning pipeline's trigger stage.

Usage:
    python execution/fetch_yf_grades.py --tickers META NOW
    python execution/fetch_yf_grades.py --days 5 --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db import DB_PATH  # noqa: E402
from news.store import (  # noqa: E402
    SOURCE_FEED_YF_GRADES,
    NewsRow,
    drop_duplicate_stories,
    upsert_news_rows,
)
from pipeline.row_validation import validate_provider_rows  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DEFAULT_DAYS = 3
MAX_WORKERS = 8  # yfinance is HTTP-bound; same threading idiom as the FMP feed

_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_SLUG_RX = re.compile(r"[^a-z0-9]+")

# yfinance Action codes -> headline verb phrases. Unknown codes fall back to a
# neutral "rates" so a Yahoo vocabulary addition degrades, not drops.
_ACTION_UP = "up"
_ACTION_DOWN = "down"
_ACTION_INIT = "init"
_ACTION_MAIN = "main"
_ACTION_REIT = "reit"


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


# ---------------------------------------------------------------------------
# yfinance access (duck-typed; tests inject records and never import yfinance)
# ---------------------------------------------------------------------------


def _frame_to_records(frame: object) -> list[dict[str, object]]:
    """DataFrame -> plain record dicts with the GradeDate index restored as a
    column. Duck-typed via getattr so the module imports (and tests run)
    without pandas in play; any shape surprise degrades to []."""
    if frame is None:
        return []
    try:
        reset_index = getattr(frame, "reset_index", None)
        if not callable(reset_index):
            return []
        to_dict = getattr(reset_index(), "to_dict", None)
        if not callable(to_dict):
            return []
        records: object = to_dict("records")
    except Exception:
        return []
    if not isinstance(records, list):
        return []
    out: list[dict[str, object]] = []
    for rec in cast("list[object]", records):
        if isinstance(rec, dict):
            out.append({str(k): v for k, v in cast("dict[object, object]", rec).items()})
    return out


def _load_grade_records(ticker: str) -> list[dict[str, object]]:
    """Live yfinance pull -> record dicts. Degrades to [] on ANY failure —
    yfinance not installed, Yahoo transport error, attribute/shape drift."""
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        _log("yf_grades_unavailable", ticker=ticker, error="yfinance not installed")
        return []
    try:
        frame = cast("object", yf.Ticker(ticker).upgrades_downgrades)
    except Exception as exc:
        _log("yf_grades_fetch_failed", ticker=ticker, error=f"{type(exc).__name__}: {exc}"[:200])
        return []
    return _frame_to_records(frame)


# ---------------------------------------------------------------------------
# Mapping (grade record -> NewsRow)
# ---------------------------------------------------------------------------


def _grade_datetime(value: object) -> datetime | None:
    """GradeDate cell -> naive-UTC datetime. pandas Timestamps subclass
    datetime; tz-aware values convert, naive values are already UTC (the column
    derives from Yahoo's UTC epochGradeDate)."""
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            value = to_pydatetime()
        except Exception:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _money(value: float) -> str:
    return f"${value:,.0f}" if value == int(value) else f"${value:,.2f}"


def _target_price(value: object) -> float | None:
    """A positive finite price target or None (NaN-safe: NaN != NaN)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    return price if price == price and price > 0 else None


def _pt_suffix(record: dict[str, object]) -> str:
    current = _target_price(record.get("currentPriceTarget"))
    prior = _target_price(record.get("priorPriceTarget"))
    if current is None:
        return ""
    if prior is not None and prior != current:
        return f"; PT {_money(prior)} → {_money(current)}"
    return f"; PT {_money(current)}"


def _headline(record: dict[str, object], ticker: str) -> str | None:
    """Wire-convention headline, or None when the record lacks the essentials
    (firm + to-grade) — such rows are dropped, never padded."""
    firm = str(record.get("Firm") or "").strip()
    to_grade = str(record.get("ToGrade") or "").strip()
    if not firm or not to_grade:
        return None
    from_grade = str(record.get("FromGrade") or "").strip()
    action = str(record.get("Action") or "").strip().lower()
    from_part = f" from {from_grade}" if from_grade and from_grade != to_grade else ""
    if action == _ACTION_UP:
        body = f"{firm} upgrades {ticker} to {to_grade}{from_part}"
    elif action == _ACTION_DOWN:
        body = f"{firm} downgrades {ticker} to {to_grade}{from_part}"
    elif action == _ACTION_INIT:
        body = f"{firm} initiates coverage on {ticker} at {to_grade}"
    elif action == _ACTION_REIT:
        body = f"{firm} reiterates {to_grade} on {ticker}"
    elif action == _ACTION_MAIN:
        body = f"{firm} maintains {to_grade} on {ticker}"
    else:
        body = f"{firm} rates {ticker} {to_grade}"
    return body + _pt_suffix(record)


def _event_url(ticker: str, published_at: str, firm: str) -> str:
    """Stable synthetic per-event URL: clickable (the ticker's Yahoo analysis
    page) and unique per (timestamp, firm) for the (ticker, url) dedup key."""
    stamp = published_at.replace("-", "").replace(":", "").replace(" ", "T")
    slug = _SLUG_RX.sub("-", firm.lower()).strip("-") or "firm"
    return f"https://finance.yahoo.com/quote/{ticker}/analysis#grade-{stamp}-{slug}"


def rows_from_records(
    records: list[dict[str, object]], *, ticker: str, since: datetime
) -> list[NewsRow]:
    """Pure mapping: grade records -> NewsRows within the window (GradeDate >=
    ``since``, naive UTC). Rows missing a parseable date or the firm/grade
    essentials are dropped; a record that fails the NewsRow gate is dropped
    individually (degraded, never a halt)."""
    ticker = ticker.upper()
    candidates: list[dict[str, object]] = []
    for record in records:
        when = _grade_datetime(record.get("GradeDate"))
        if when is None or when < since:
            continue
        headline = _headline(record, ticker)
        if headline is None:
            continue
        published_at = when.strftime(_DATETIME_FORMAT)
        firm = str(record.get("Firm") or "").strip()
        candidates.append(
            {
                "ticker": ticker,
                "headline": headline,
                "url": _event_url(ticker, published_at, firm),
                "published_at": published_at,
                "snippet": None,
                "source": firm,
                "source_feed": SOURCE_FEED_YF_GRADES,
            }
        )
    return validate_provider_rows(
        candidates,
        TypeAdapter(NewsRow),
        source="yf_grades",
        context={"ticker": ticker},
    )


def fetch_grades_for_ticker(
    ticker: str,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    records_loader: Callable[[str], list[dict[str, object]]] | None = None,
) -> list[NewsRow]:
    """Fetch + map one ticker's recent rating changes (does NOT persist).
    ``records_loader`` is the test seam — fixtures in, no yfinance import."""
    ticker = ticker.upper()
    loader = records_loader or _load_grade_records
    since = (now or datetime.now(UTC).replace(tzinfo=None)) - timedelta(days=days)
    return rows_from_records(loader(ticker), ticker=ticker, since=since)


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------


def run(tickers: list[str], *, db_path: str, days: int) -> int:
    """Fetch every ticker (threaded), dedupe cross-feed, persist. Exit 1 only
    on a structural failure (`news` table absent)."""
    if not tickers:
        _log("yf_grades_no_tickers")
        return 0
    all_rows: list[NewsRow] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures: dict[Future[list[NewsRow]], str] = {
            executor.submit(fetch_grades_for_ticker, ticker, days=days): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            all_rows.extend(future.result())
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        fresh = drop_duplicate_stories(conn, all_rows)
        inserted, deduped = upsert_news_rows(conn, fresh)
    except sqlite3.OperationalError as exc:
        _log("yf_grades_table_missing", error=str(exc))
        return 1
    finally:
        conn.close()
    _log(
        "yf_grades_done",
        tickers=len(tickers),
        fetched_rows=len(all_rows),
        inserted=inserted,
        deduped=deduped + (len(all_rows) - len(fresh)),
    )
    return 0


def default_tickers(db_path: str) -> list[str]:
    """The active tracked book — same selection rule as the other news feeds."""
    import execution.fetch_fmp_news as fmpnews

    return fmpnews.default_tickers(db_path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", nargs="*", help="Whitespace-separated tickers (default: active tracked book)."
    )
    parser.add_argument("--db-path", default=None, help="Override the portfolio DB path.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Grade-date window in days.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = cast("str", args.db_path) if args.db_path else DB_PATH
    tickers = (
        [t.upper() for t in cast("list[str]", args.tickers)]
        if args.tickers
        else default_tickers(db_path)
    )
    return run(tickers, db_path=db_path, days=cast("int", args.days))


if __name__ == "__main__":
    sys.exit(main())
