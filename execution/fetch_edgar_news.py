"""execution/fetch_edgar_news.py — additive EDGAR filings feed for the `news` table.

Maps recent SEC filings for tracked tickers into validated ``NewsRow``s:

  * 8-K / 8-K/A (with Reg-S-K item codes)          -> source_feed 'edgar_8k'
  * SC 13D / SCHEDULE 13D (+ amendments)           -> source_feed 'edgar_13d'
  * SC 13G / SCHEDULE 13G (+ amendments)           -> source_feed 'edgar_13g'

This is an ADDITIVE source (directives/news_sources_plan.md): EDGAR is canonical
primary-source disclosure the journalism feeds merely paraphrase, so the
dispatcher (execution/fetch_news.py) runs it every day alongside — never instead
of — the FMP→WebSearch ladder, deduped cross-feed by (ticker, normalized
headline, date) via ``drop_duplicate_stories``.

Everything comes from ONE request per ticker: ``data.sec.gov/submissions/
CIK##########.json`` carries form type, 8-K item codes, filing date and the UTC
acceptance timestamp in parallel arrays — no per-filing fetches. 13D/G filings
appear under the SUBJECT company's submissions (verified live 2026-06-11);
EDGAR's Dec-2024 renaming means new filings say 'SCHEDULE 13D/G' where legacy
ones say 'SC 13D/G' — both spellings are matched.

SEC fair access: a descriptive User-Agent with a contact email (override via
the ``EDGAR_USER_AGENT`` env var), a global >=0.15s spacing between requests
(policy ceiling is 10 req/s), and an on-disk cache under ``data/edgar/`` —
``company_tickers.json`` (ticker->CIK registry, 7-day TTL) and
``submissions/CIK##########.json`` per ticker (6h TTL: the daily morning run
refetches, same-day re-runs are free).

FPI caveat: 20-F/6-K filers (NU, ASML, NVO, TSM, ...) don't file 8-K and 6-K
carries no item codes, so those names get 13D/G coverage only.

Usage:
    python execution/fetch_edgar_news.py --tickers META GOOG
    python execution/fetch_edgar_news.py --days 5 --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import requests
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db import DB_PATH  # noqa: E402
from news.store import (  # noqa: E402
    SOURCE_FEED_EDGAR_8K,
    SOURCE_FEED_EDGAR_13D,
    SOURCE_FEED_EDGAR_13G,
    NewsRow,
    drop_duplicate_stories,
    upsert_news_rows,
)

EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SEC fair-access policy wants a descriptive UA with a real contact. The repo is
# private, so the default carries one; override via EDGAR_USER_AGENT.
USER_AGENT_ENV = "EDGAR_USER_AGENT"
DEFAULT_USER_AGENT = "earnings-summary/1.0 (personal research; bhanumufcpraneeth@gmail.com)"

DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "edgar"
TICKERS_CACHE_TTL_HOURS = 7 * 24.0
SUBMISSIONS_CACHE_TTL_HOURS = 6.0

# >=0.15s between SEC requests — comfortably under the 10 req/s ceiling, same
# spacing src/insider_transactions.py uses.
MIN_REQUEST_INTERVAL_S = 0.15
REQUEST_TIMEOUT = 30

DEFAULT_DAYS = 3  # filing-date window; wide enough that weekend filings overlap runs

_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Reg-S-K 8-K item codes -> short human descriptions (the headline's middle).
# Only items plausible for our operating-company book; an unknown code renders
# as 'other disclosure' rather than failing.
ITEM_DESCRIPTIONS: dict[str, str] = {
    "1.01": "entry into material agreement",
    "1.02": "termination of material agreement",
    "1.03": "bankruptcy or receivership",
    "1.05": "material cybersecurity incident",
    "2.01": "completed acquisition or disposition",
    "2.02": "results of operations",
    "2.03": "new financial obligation",
    "2.04": "triggering event on financial obligation",
    "2.05": "exit or disposal costs",
    "2.06": "material impairment",
    "3.01": "delisting or listing-standard notice",
    "3.02": "unregistered equity sale",
    "3.03": "modification of holder rights",
    "4.01": "auditor change",
    "4.02": "non-reliance on prior financials (restatement)",
    "5.01": "change in control",
    "5.02": "executive or director change",
    "5.03": "charter/bylaws or fiscal-year change",
    "5.07": "shareholder vote results",
    "5.08": "shareholder nominations",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}

# Items that are pure disclosure plumbing — never the story. Skipped when
# choosing which item names the filing (but still listed in the headline).
_BOILERPLATE_ITEMS = ("9.01",)

_8K_FORMS = ("8-K", "8-K/A")
_13D_FORMS = ("SC 13D", "SC 13D/A")
_13G_FORMS = ("SC 13G", "SC 13G/A")


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _user_agent() -> str:
    return os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------
# Throttled fetch + on-disk cache (SEC fair access)
# ---------------------------------------------------------------------------

_last_request_monotonic = 0.0


def _throttle() -> None:
    global _last_request_monotonic
    elapsed = time.monotonic() - _last_request_monotonic
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request_monotonic = time.monotonic()


def _get_json(url: str, *, user_agent: str) -> object:
    """One throttled SEC GET -> parsed JSON. Raises on HTTP/network/parse
    failure; callers degrade per ticker."""
    _throttle()
    resp = requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _cache_fresh(path: Path, ttl_hours: float) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < ttl_hours * 3600.0
    except OSError:
        return False


def _read_cached_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fetch_with_cache(
    url: str, cache_path: Path, *, ttl_hours: float, user_agent: str, force_refresh: bool = False
) -> object | None:
    """Cached SEC GET: a fresh cache file short-circuits the network entirely;
    otherwise fetch and overwrite. Network failure falls back to a STALE cache
    when one exists (old filings beat no filings), else None."""
    if not force_refresh and _cache_fresh(cache_path, ttl_hours):
        cached = _read_cached_json(cache_path)
        if cached is not None:
            return cached
    try:
        payload = _get_json(url, user_agent=user_agent)
    except (requests.RequestException, ValueError) as exc:
        _log("edgar_fetch_failed", url=url, error=str(exc)[:200])
        return _read_cached_json(cache_path)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # cache is an optimization; never fail the fetch on it
        _log("edgar_cache_write_failed", path=str(cache_path), error=str(exc)[:200])
    return payload


# ---------------------------------------------------------------------------
# Ticker -> CIK (company_tickers.json registry)
# ---------------------------------------------------------------------------


def resolve_cik(
    ticker: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    user_agent: str | None = None,
    force_refresh: bool = False,
) -> str | None:
    """Ticker -> 10-digit zero-padded CIK via the SEC registry (disk-cached,
    7-day TTL). None when the ticker isn't in the registry (non-EDGAR filer) or
    the registry is unreachable with no cache."""
    payload = _fetch_with_cache(
        EDGAR_TICKERS_URL,
        cache_dir / "company_tickers.json",
        ttl_hours=TICKERS_CACHE_TTL_HOURS,
        user_agent=user_agent or _user_agent(),
        force_refresh=force_refresh,
    )
    if not isinstance(payload, dict):
        return None
    # Registry shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    # Registry tickers use dashes for share classes (BRK-B), so normalize dots.
    wanted = ticker.upper().replace(".", "-")
    for entry in cast("dict[str, object]", payload).values():
        if not isinstance(entry, dict):
            continue
        rec = cast("dict[str, object]", entry)
        if str(rec.get("ticker", "")).upper() == wanted:
            try:
                return f"{int(cast('int | str', rec.get('cik_str'))):010d}"
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# Mapping (submissions JSON -> NewsRow)
# ---------------------------------------------------------------------------


def _to_published_at(acceptance: str, filing_date: str) -> str | None:
    """Canonical UTC 'YYYY-MM-DD HH:MM:SS' from a filing's timestamps.

    Prefers ``acceptanceDateTime`` (ISO-8601, already UTC, e.g.
    '2026-05-29T20:16:13.000Z'). Falls back to ``filingDate`` at midnight — the
    date is real and midnight is the conservative floor (sorts oldest within
    the day, never inflates recency). None when neither parses (caller drops
    the row; timestamps are never fabricated)."""
    if acceptance:
        try:
            parsed = datetime.fromisoformat(acceptance.replace("Z", "+00:00"))
            return parsed.astimezone(UTC).strftime(_DATETIME_FORMAT)
        except ValueError:
            pass
    if filing_date:
        try:
            return datetime.strptime(filing_date, "%Y-%m-%d").strftime(_DATETIME_FORMAT)
        except ValueError:
            pass
    return None


def _parse_items(items: str) -> list[str]:
    return [code.strip() for code in items.split(",") if code.strip()]


def _headline_8k(form: str, items: str, company: str) -> str:
    """E.g. '8-K 2.01, 9.01: completed acquisition or disposition — Acme, Inc.'.
    The first non-boilerplate item names the filing; all codes stay listed (the
    inbox categorizer reads them — inbox_rank._categorize)."""
    codes = _parse_items(items)
    if not codes:
        return f"{form}: filing — {company}"
    named = next((c for c in codes if c not in _BOILERPLATE_ITEMS), codes[0])
    description = ITEM_DESCRIPTIONS.get(named, "other disclosure")
    return f"{form} {', '.join(codes)}: {description} — {company}"


def _ownership_feed(form: str) -> str | None:
    """'edgar_13d' / 'edgar_13g' for a beneficial-ownership form (both the
    legacy 'SC 13D' and the post-Dec-2024 'SCHEDULE 13D' spellings), else None."""
    normalized = form.upper().replace("SCHEDULE ", "SC ").strip()
    if normalized in _13D_FORMS:
        return SOURCE_FEED_EDGAR_13D
    if normalized in _13G_FORMS:
        return SOURCE_FEED_EDGAR_13G
    return None


def _headline_ownership(form: str, feed: str, company: str) -> str:
    display = form.upper().replace("SCHEDULE ", "SC ").strip()
    amended = display.endswith("/A")
    if feed == SOURCE_FEED_EDGAR_13D:
        phrase = "activist stake (>5%) amended" if amended else "activist stake (>5%) disclosed"
    else:
        phrase = "passive stake (>5%) amended" if amended else "passive stake (>5%) disclosed"
    return f"{display}: {phrase} — {company}"


def _str_list(recent: dict[str, object], key: str) -> list[str]:
    raw = recent.get(key)
    if not isinstance(raw, list):
        return []
    return ["" if v is None else str(v) for v in cast("list[object]", raw)]


def map_filings_to_news_rows(
    payload: object, *, ticker: str, cik: str, since_date: str
) -> list[NewsRow]:
    """Pure mapping: one submissions JSON payload -> NewsRows for the 8-K and
    13D/G filings on/after ``since_date`` ('YYYY-MM-DD', lexical compare on
    filingDate). No network, no persistence — the unit tests feed fixtures here.

    URL is the filing's primary document under its EDGAR archive folder —
    unique per accession, so the table's (ticker, url) key keeps re-runs
    idempotent. A row whose timestamps don't parse or that fails the NewsRow
    gate is dropped (never fabricated), mirroring the FMP feed's discipline."""
    if not isinstance(payload, dict):
        return []
    root = cast("dict[str, object]", payload)
    company = str(root.get("name") or "").strip() or ticker.upper()
    filings = root.get("filings")
    if not isinstance(filings, dict):
        return []
    recent_obj = cast("dict[str, object]", filings).get("recent")
    if not isinstance(recent_obj, dict):
        return []
    recent = cast("dict[str, object]", recent_obj)

    forms = _str_list(recent, "form")
    accessions = _str_list(recent, "accessionNumber")
    filing_dates = _str_list(recent, "filingDate")
    acceptance_times = _str_list(recent, "acceptanceDateTime")
    items_list = _str_list(recent, "items")
    primary_docs = _str_list(recent, "primaryDocument")

    rows: list[NewsRow] = []
    for idx, form in enumerate(forms):
        filing_date = filing_dates[idx] if idx < len(filing_dates) else ""
        if not filing_date or filing_date < since_date:
            continue
        form_upper = form.upper().strip()
        items = items_list[idx] if idx < len(items_list) else ""
        if form_upper in _8K_FORMS:
            feed = SOURCE_FEED_EDGAR_8K
            headline = _headline_8k(form_upper, items, company)
        else:
            ownership = _ownership_feed(form_upper)
            if ownership is None:
                continue
            feed = ownership
            headline = _headline_ownership(form_upper, ownership, company)

        accession = accessions[idx] if idx < len(accessions) else ""
        acceptance = acceptance_times[idx] if idx < len(acceptance_times) else ""
        published_at = _to_published_at(acceptance, filing_date)
        if published_at is None:
            _log("edgar_unparseable_date", ticker=ticker, accession=accession, form=form_upper)
            continue

        archive_base = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
        )
        primary_doc = primary_docs[idx] if idx < len(primary_docs) else ""
        url = f"{archive_base}/{primary_doc}" if primary_doc else f"{archive_base}/"

        try:
            rows.append(
                NewsRow(
                    ticker=ticker.upper(),
                    headline=headline,
                    url=url,
                    published_at=published_at,
                    snippet=f"{form_upper} filed {filing_date}; accession {accession}",
                    source="SEC EDGAR",
                    source_feed=feed,
                )
            )
        except ValidationError:
            _log("edgar_row_rejected", ticker=ticker, accession=accession, form=form_upper)
    return rows


# ---------------------------------------------------------------------------
# Per-ticker fetch (the unit the dispatcher reuses)
# ---------------------------------------------------------------------------


def fetch_edgar_news_for_ticker(
    ticker: str,
    *,
    days: int = DEFAULT_DAYS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    user_agent: str | None = None,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> list[NewsRow]:
    """Fetch + map one ticker's recent EDGAR filings (does NOT persist).

    Degrades to [] — no CIK (non-EDGAR filer), unreachable SEC with a cold
    cache — rather than raising; the dispatcher treats this feed as additive
    and must never let it block the primary feeds."""
    ua = user_agent or _user_agent()
    cik = resolve_cik(ticker, cache_dir=cache_dir, user_agent=ua, force_refresh=force_refresh)
    if cik is None:
        _log("edgar_no_cik", ticker=ticker.upper())
        return []
    payload = _fetch_with_cache(
        EDGAR_SUBMISSIONS_URL.format(cik=cik),
        cache_dir / "submissions" / f"CIK{cik}.json",
        ttl_hours=SUBMISSIONS_CACHE_TTL_HOURS,
        user_agent=ua,
        force_refresh=force_refresh,
    )
    if payload is None:
        _log("edgar_submissions_unavailable", ticker=ticker.upper(), cik=cik)
        return []
    reference = now or datetime.now(UTC)
    since_date = (reference - timedelta(days=days)).date().isoformat()
    return map_filings_to_news_rows(payload, ticker=ticker, cik=cik, since_date=since_date)


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------


def default_tickers(db_path: str) -> list[str]:
    """The active tracked book — same selection rule as the other news feeds."""
    import execution.fetch_fmp_news as fmpnews

    return fmpnews.default_tickers(db_path)


def run(
    tickers: list[str],
    *,
    db_path: str,
    days: int,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force_refresh: bool = False,
) -> int:
    """Fetch every ticker sequentially (the SEC throttle makes parallelism
    pointless), dedupe cross-feed, persist. Exit 1 only on a structural failure
    (`news` table absent)."""
    if not tickers:
        _log("edgar_news_no_tickers")
        return 0
    all_rows: list[NewsRow] = []
    for ticker in tickers:
        all_rows.extend(
            fetch_edgar_news_for_ticker(
                ticker, days=days, cache_dir=cache_dir, force_refresh=force_refresh
            )
        )
    conn = sqlite3.connect(db_path)
    try:
        fresh = drop_duplicate_stories(conn, all_rows)
        inserted, deduped = upsert_news_rows(conn, fresh)
    except sqlite3.OperationalError as exc:
        _log("edgar_news_table_missing", error=str(exc))
        return 1
    finally:
        conn.close()
    _log(
        "edgar_news_done",
        tickers=len(tickers),
        fetched_rows=len(all_rows),
        inserted=inserted,
        deduped=deduped + (len(all_rows) - len(fresh)),
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", nargs="*", help="Whitespace-separated tickers (default: active tracked book)."
    )
    parser.add_argument("--db-path", default=None, help="Override the portfolio DB path.")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="Filing-date window in days."
    )
    parser.add_argument(
        "--cache-dir", default=None, help=f"EDGAR cache directory (default: {DEFAULT_CACHE_DIR})."
    )
    parser.add_argument(
        "--force-refresh", action="store_true", help="Bypass cache TTLs (still writes the cache)."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = cast("str", args.db_path) if args.db_path else DB_PATH
    tickers = (
        [t.upper() for t in cast("list[str]", args.tickers)]
        if args.tickers
        else default_tickers(db_path)
    )
    cache_dir = Path(cast("str", args.cache_dir)) if args.cache_dir else DEFAULT_CACHE_DIR
    return run(
        tickers,
        db_path=db_path,
        days=cast("int", args.days),
        cache_dir=cache_dir,
        force_refresh=cast("bool", args.force_refresh),
    )


if __name__ == "__main__":
    sys.exit(main())
