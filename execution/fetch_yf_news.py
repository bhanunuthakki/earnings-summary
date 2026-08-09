"""execution/fetch_yf_news.py — free per-ticker journalism feed (yfinance).

Replaces the WebSearch+LLM ingester as the DEFAULT news source. The measured
case for the swap (2026-07-25, lifetime figures from `llm_calls` + `news`):

    websearch_opus : $453.39 spent, 1,081 successful calls, 79 rows stored
                     => $5.74 per stored row, 0.073 rows per call, ~55s each
    yfinance .news : $0.00, ~0.3s per ticker, 10 items per ticker (measured live)

93% of the paid calls stored nothing: the job re-searched the same window
daily, the model re-found the same URLs, and `(ticker, url)` dedup discarded
them. The root cause of the fallback becoming primary is upstream — FMP's
stock-news endpoint returns **402 Payment Required** for every ticker
(verified 2026-07-03, `fetch_news.py` header) — so `--source auto` fell
through to the LLM for the entire book instead of for the rare refusal it was
designed for.

The general rule this encodes: **deterministic feeds do INGESTION; the LLM does
JUDGEMENT over already-known items.** Scoring materiality on a known headline
(`material_news_classification`) costs a fraction of a cent; discovering the
headline with an agentic web loop cost ~$0.40 and ~206K cache-read tokens.

Shape notes (verified live 2026-07-25 against yfinance's current schema): items
arrive nested under ``content`` with ``title``, ``pubDate`` (ISO-8601 Zulu),
``provider.displayName``, and a URL under ``canonicalUrl.url`` or
``clickThroughUrl.url``. Older yfinance builds returned a FLAT dict with
``title``/``link``/``publisher``/``providerPublishTime`` (epoch seconds), so
both shapes are read — an unofficial API that drifts must degrade to [] rather
than take the morning pipeline down with it (the `fetch_yf_grades` contract).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from news.store import (  # noqa: E402
    SOURCE_FEED_YF_NEWS,
    NewsRow,
    upsert_news_rows,
)
from pipeline.row_validation import RowValidationDriftError, validate_provider_rows  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# yfinance is HTTP-bound; the same worker count the grades feed uses.
_WORKERS = 8
DEFAULT_DAYS = 7
_MAX_SNIPPET = 400


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr, flush=True)


def _utc_stamp(value: object) -> str | None:
    """Normalize either yfinance timestamp shape to the store's exact
    ``YYYY-MM-DD HH:MM:SS`` UTC contract. None when unparseable — the row is
    then DROPPED, never dated by guess (the same 'never fabricate a date' rule
    the LLM ingester was given)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        dt = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return None


def _first_url(*candidates: object) -> str | None:
    for cand in candidates:
        if isinstance(cand, dict):
            url = cast("dict[str, object]", cand).get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        elif isinstance(cand, str) and cand.strip():
            return cand.strip()
    return None


def rows_for_ticker(ticker: str, items: list[object], *, days: int) -> list[NewsRow]:
    """Map raw yfinance news items onto validated NewsRows.

    Every per-item failure is skipped individually: one malformed story must
    not cost the ticker its other nine.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    candidates: list[dict[str, object]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        inner = item.get("content")
        content = cast("dict[str, object]", inner) if isinstance(inner, dict) else item

        title = content.get("title")
        if not isinstance(title, str) or not title.strip():
            continue

        url = _first_url(
            content.get("canonicalUrl"), content.get("clickThroughUrl"), content.get("link")
        )
        if not url:
            continue  # url is the dedup key — a story without one is unusable

        published = _utc_stamp(content.get("pubDate") or content.get("providerPublishTime"))
        if published is None or published < cutoff:
            continue  # undateable or outside the window

        provider = content.get("provider")
        source = (
            str(cast("dict[str, object]", provider).get("displayName") or "").strip()
            if isinstance(provider, dict)
            else str(content.get("publisher") or "").strip()
        )
        summary = content.get("summary") or content.get("description")
        snippet = str(summary).strip()[:_MAX_SNIPPET] if isinstance(summary, str) else None

        candidates.append(
            {
                "ticker": ticker.upper(),
                "headline": title.strip(),
                "url": url,
                "published_at": published,
                "snippet": snippet or None,
                "source": source or None,
                "source_feed": SOURCE_FEED_YF_NEWS,
            }
        )
    return validate_provider_rows(
        candidates,
        TypeAdapter(NewsRow),
        source="yf_news",
        context={"ticker": ticker.upper()},
    )


def fetch_news_for_ticker(ticker: str, *, days: int = DEFAULT_DAYS) -> list[NewsRow]:
    """One ticker's free journalism. Degrades to [] on ANY failure — yfinance
    is an unofficial API and this feed must never block the pipeline."""
    try:
        import yfinance as yf

        raw = cast("list[object]", yf.Ticker(ticker).news or [])
    except Exception as exc:
        _log("yf_news_fetch_failed", ticker=ticker, error=f"{type(exc).__name__}: {exc}"[:200])
        return []
    return rows_for_ticker(ticker, list(raw), days=days)


def fetch_many(
    tickers: list[str],
    *,
    days: int = DEFAULT_DAYS,
    fetcher: Callable[..., list[NewsRow]] | None = None,
) -> list[NewsRow]:
    """Threaded fan-out over tickers (yfinance is HTTP-bound)."""
    call = fetcher or fetch_news_for_ticker
    out: list[NewsRow] = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {pool.submit(call, t, days=days): t for t in tickers}
        for fut in as_completed(futures):
            try:
                out.extend(fut.result())
            except RowValidationDriftError:
                raise
            except Exception as exc:
                _log("yf_news_worker_failed", ticker=futures[fut], error=str(exc)[:200])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cli_tickers = cast("list[str]", args.tickers or [])
    tickers: list[str] = [t.upper() for t in cli_tickers]
    if not tickers:
        from db import DB_PATH

        db = args.db_path or DB_PATH
        conn = connect_sqlite(str(db), role=SQLiteConnectionRole.READ_ONLY)
        try:
            rows_raw = cast(
                "list[tuple[object, ...]]",
                conn.execute("SELECT DISTINCT ticker FROM tracked_companies").fetchall(),
            )
            tickers = [str(r[0]) for r in rows_raw]
        finally:
            conn.close()
    if not tickers:
        _log("yf_news_no_tickers")
        return 0

    rows = fetch_many(tickers, days=args.days)
    _log("yf_news_fetched", tickers=len(tickers), rows=len(rows))
    if args.dry_run:
        for row in rows[:10]:
            print(f"{row.ticker:6s} {row.published_at}  {row.headline[:70]}")
        return 0

    from db import DB_PATH as _DB_PATH

    conn = connect_sqlite(
        str(args.db_path or _DB_PATH), role=SQLiteConnectionRole.WRITER, schema_preflight=True
    )
    try:
        inserted, deduped = upsert_news_rows(conn, rows)
        conn.commit()
    finally:
        conn.close()
    _log("yf_news_persisted", rows=len(rows), inserted=inserted, deduped=deduped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
