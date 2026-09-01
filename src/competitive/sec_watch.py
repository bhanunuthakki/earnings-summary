"""Piece 3 — competitor IPO S-1 watch via EDGAR full-text search.

The RBRK-vs-Cohesity net-new-ARR-share metric is a FUTURE metric: Cohesity is
private and discloses ARR only sporadically. The data-unlock is Cohesity's
2026 IPO S-1. This module watches EDGAR full-text search for that filing and,
when it lands, writes a ``news`` row attributed to RBRK (it is material to the
RBRK thesis, not the filer's — the filer isn't in our book) tagged
``source_feed='edgar_s1_watch'``, so it surfaces in RBRK's feed and flips the
competitive KPI in ``RBRK.json`` from "not yet filed" to filed (date + link).

Correctness guard: a full-text search for "Cohesity" + form S-1 ALSO returns
Rubrik's OWN 2024 S-1 (it names Cohesity as a competitor). So the watch matches
on the FILER's ``display_names`` / ``ciks`` — only a filing BY the watched
entity counts, never one that merely mentions it.

Network is injectable (``fetch_fn``) so the parse/emit logic is fully testable
offline; the default fetch uses the SEC fair-access User-Agent + request spacing
the rest of the EDGAR code uses.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import requests
from pydantic import BaseModel, ConfigDict, ValidationError

from news.store import (
    SOURCE_FEED_EDGAR_S1_WATCH,
    NewsRow,
    drop_duplicate_stories,
    upsert_news_rows,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# SEC fair access — descriptive UA with a contact (override via EDGAR_USER_AGENT),
# same default + env var the rest of the EDGAR code uses.
_USER_AGENT_ENV = "EDGAR_USER_AGENT"
_DEFAULT_USER_AGENT = "earnings-summary/1.0 (+https://github.com/bhanunuthakki/earnings-summary)"
_MIN_REQUEST_INTERVAL_S = 0.15
_REQUEST_TIMEOUT = 30

FetchFn = Callable[[str], object]


class SecWatch(BaseModel):
    """One watched competitor + the holding the filing is material to."""

    model_config = ConfigDict(extra="forbid")

    entity_name: str
    attributed_ticker: str
    fulltext_query: str
    forms: list[str]
    cik: str | None = None
    min_file_date: str | None = None  # ISO 'YYYY-MM-DD'; ignore filings before it


class SecWatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readme: str | None = None
    watches: list[SecWatch]


@dataclass(slots=True)
class S1Status:
    """Resolved watch state for one competitor, read from the ``news`` table."""

    entity: str
    filed: bool
    filed_date: str | None = None
    url: str | None = None


def watch_config_path(repo_root: Path) -> Path:
    return repo_root / "micro_thesis" / "competitive" / "sec_watch.json"


def load_watches(repo_root: Path) -> list[SecWatch]:
    """Load and validate the committed watch config; [] when absent."""
    path = watch_config_path(repo_root)
    if not path.exists():
        return []
    return SecWatchConfig.model_validate_json(path.read_text(encoding="utf-8")).watches


# --------------------------------------------------------------------------- #
# Default network fetch (injectable)
# --------------------------------------------------------------------------- #

_last_request_monotonic = 0.0


def _user_agent() -> str:
    return os.environ.get(_USER_AGENT_ENV, "").strip() or _DEFAULT_USER_AGENT


def _default_fetch(url: str) -> object:
    """One throttled SEC GET -> parsed JSON. Raises on failure; the caller
    degrades to [] per watch."""
    global _last_request_monotonic
    elapsed = time.monotonic() - _last_request_monotonic
    if elapsed < _MIN_REQUEST_INTERVAL_S:
        time.sleep(_MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request_monotonic = time.monotonic()
    resp = requests.get(
        url,
        headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _efts_url(query: str, form: str) -> str:
    # Quote the query for a phrase match; `forms` is the EDGAR root-form filter.
    quoted_phrase = '"' + query + '"'
    return f"{EFTS_SEARCH_URL}?{urlencode({'q': quoted_phrase, 'forms': form})}"


# --------------------------------------------------------------------------- #
# Pure parse: EFTS payload -> NewsRows (the unit tests feed fixtures here)
# --------------------------------------------------------------------------- #


def _filer_is_entity(source: dict[str, object], watch: SecWatch) -> bool:
    """True iff the FILER is the watched entity (not merely a document that
    mentions it). Matches the entity name inside any ``display_names`` entry, or
    a configured CIK against the hit's ``ciks``."""
    if watch.cik is not None:
        ciks = source.get("ciks")
        if isinstance(ciks, list):
            wanted = watch.cik.lstrip("0")
            if any(str(c).lstrip("0") == wanted for c in cast("list[object]", ciks)):
                return True
    names = source.get("display_names")
    if isinstance(names, list):
        needle = watch.entity_name.lower()
        return any(needle in str(n).lower() for n in cast("list[object]", names))
    return False


def _form_matches(form: str, watch_forms: list[str]) -> bool:
    """Prefix match so a watched ``S-1`` also catches ``S-1/A`` amendments."""
    f = form.upper().strip()
    return any(f.startswith(wf.upper().strip()) for wf in watch_forms)


def _filing_url(source: dict[str, object], hit_id: str) -> str:
    """Build the EDGAR archive URL for the filing's primary document."""
    adsh = str(source.get("adsh") or "").strip()
    ciks = source.get("ciks")
    cik_raw = ""
    if isinstance(ciks, list) and ciks:
        cik_raw = str(cast("list[object]", ciks)[0])
    # _id is "<accession>:<primary_doc>"; recover the doc name when present.
    primary_doc = hit_id.split(":", 1)[1] if ":" in hit_id else ""
    if not adsh or not cik_raw:
        return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_raw}"
    try:
        cik_int = int(cik_raw)
    except ValueError:
        cik_int = 0
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{adsh.replace('-', '')}"
    return f"{base}/{primary_doc}" if primary_doc else f"{base}/"


def parse_efts_hits(payload: object, watch: SecWatch) -> list[NewsRow]:
    """Map one EFTS response to NewsRows for filings BY the watched entity.

    Filters: filer == watched entity, form matches the watch, and file_date is
    on/after ``min_file_date``. No network, no persistence."""
    if not isinstance(payload, dict):
        return []
    hits_obj = cast("dict[str, object]", payload).get("hits")
    if not isinstance(hits_obj, dict):
        return []
    hits = cast("dict[str, object]", hits_obj).get("hits")
    if not isinstance(hits, list):
        return []

    rows: list[NewsRow] = []
    for hit in cast("list[object]", hits):
        if not isinstance(hit, dict):
            continue
        hit_d = cast("dict[str, object]", hit)
        source = hit_d.get("_source")
        if not isinstance(source, dict):
            continue
        src = cast("dict[str, object]", source)

        form = str(src.get("form") or "")
        if not _form_matches(form, watch.forms):
            continue
        if not _filer_is_entity(src, watch):
            continue
        file_date = str(src.get("file_date") or "")
        if not file_date:
            continue
        if watch.min_file_date and file_date < watch.min_file_date:
            continue
        try:
            published_at = datetime.strptime(file_date, "%Y-%m-%d").strftime(_DATETIME_FORMAT)
        except ValueError:
            continue

        url = _filing_url(src, str(hit_d.get("_id") or ""))
        headline = (
            f"{watch.entity_name} files {form} (IPO registration) — "
            f"unlocks {watch.attributed_ticker}-vs-{watch.entity_name} net-new-ARR share"
        )
        try:
            rows.append(
                NewsRow(
                    ticker=watch.attributed_ticker.upper(),
                    headline=headline,
                    url=url,
                    published_at=published_at,
                    snippet=(
                        f"{watch.entity_name} {form} filed {file_date} "
                        f"(accession {src.get('adsh')}); competitor IPO registration."
                    ),
                    source="SEC EDGAR (full-text search)",
                    source_feed=SOURCE_FEED_EDGAR_S1_WATCH,
                )
            )
        except ValidationError:
            continue
    return rows


def check_s1_watch(
    watches: list[SecWatch],
    *,
    fetch_fn: FetchFn | None = None,
    now: datetime | None = None,
) -> list[NewsRow]:
    """Query EFTS for every (watch, form) and return the emitted NewsRows.

    Degrades per watch to no rows on a network/parse failure (additive feed —
    never raises). ``now`` is accepted for symmetry/testing; filing recency is
    governed by each watch's ``min_file_date``."""
    _ = now
    fetch = fetch_fn or _default_fetch
    rows: list[NewsRow] = []
    seen_urls: set[str] = set()
    for watch in watches:
        for form in watch.forms:
            try:
                payload = fetch(_efts_url(watch.fulltext_query, form))
            except (requests.RequestException, ValueError, OSError) as exc:
                _log(
                    "s1_watch_fetch_failed",
                    entity=watch.entity_name,
                    form=form,
                    error=str(exc)[:200],
                )
                continue
            for row in parse_efts_hits(payload, watch):
                if row.url in seen_urls:
                    continue
                seen_urls.add(row.url)
                rows.append(row)
    return rows


def run(
    repo_root: Path,
    *,
    db_path: str,
    fetch_fn: FetchFn | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Load watches, query EFTS, persist new rows to ``news``. Returns
    (inserted, deduped). A structural failure (no `news` table) raises."""
    watches = load_watches(repo_root)
    if not watches:
        return (0, 0)
    rows = check_s1_watch(watches, fetch_fn=fetch_fn, now=now)
    if not rows:
        return (0, 0)
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        fresh = drop_duplicate_stories(conn, rows)
        inserted, deduped = upsert_news_rows(conn, fresh)
    finally:
        conn.close()
    return (inserted, deduped + (len(rows) - len(fresh)))


def s1_watch_status(
    conn: sqlite3.Connection, *, entity: str = "Cohesity", attributed_ticker: str = "RBRK"
) -> S1Status:
    """Resolve whether the watched entity's S-1 has been seen, from ``news``.

    The read path for the competitive KPI: returns the earliest watch row's date
    + link, or ``filed=False`` when none exists yet (the live state today)."""
    try:
        row = conn.execute(
            "SELECT published_at, url FROM news "
            "WHERE ticker = ? AND source_feed = ? AND headline LIKE ? "
            "ORDER BY published_at ASC LIMIT 1",
            (attributed_ticker.upper(), SOURCE_FEED_EDGAR_S1_WATCH, f"%{entity}%"),
        ).fetchone()
    except sqlite3.OperationalError:
        return S1Status(entity=entity, filed=False)
    if row is None:
        return S1Status(entity=entity, filed=False)
    published = str(row["published_at"] if hasattr(row, "keys") else row[0])
    url = str(row["url"] if hasattr(row, "keys") else row[1])
    return S1Status(entity=entity, filed=True, filed_date=published[:10], url=url)


def _log(event: str, **kwargs: object) -> None:
    import sys

    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)
