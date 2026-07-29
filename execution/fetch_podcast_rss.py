"""execution/fetch_podcast_rss.py — curated podcast-RSS feed for diet-lane signals.

Polls seven curated show RSS feeds and writes a DIET-lane ``media_appearance``
signal when a tracked company name or a rostered investor firm name is found
(word-bounded, case-insensitive) in an episode title + description excerpt.

Matching rules
--------------
* **Company hit**: a tracked-company ``name`` (3+ chars) found word-bounded in
  the combined title + first 500 chars of description → signal ticker = company
  ticker.
* **Investor hit**: an active ``investor_13f`` source ``display_name`` (3+ chars)
  found word-bounded → signal ticker = that source's ``source_key`` (e.g.
  ``"appaloosa"``). The source_key is not a stock ticker, but the diet panel
  shows all types together so the row appears in the ingest stream regardless.

Each (ticker, episode_url) pair is written at most once — the
``ux_signals_media_url`` partial index (migration 0102) makes re-polling
idempotent. Source-call telemetry is logged for both feed-poll and per-signal
events so the Source Calls panel can show podcast_rss alongside other feeds.

Usage
-----
    python execution/fetch_podcast_rss.py
    python execution/fetch_podcast_rss.py --days 14
    python execution/fetch_podcast_rss.py --repo-root /path/to/earnings-summary
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from signals.store import record_media_appearance  # noqa: E402
from sources.registry import CallStatus, log_call  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# ---------------------------------------------------------------------------
# Curated show allowlist — (show_name, rss_url).
# Re-polling a feed after an episode is already in signals is a no-op
# (ux_signals_media_url dedup). Only the 7 shows in the directive allowlist.
# ---------------------------------------------------------------------------
_FEEDS: tuple[tuple[str, str], ...] = (
    ("BG2", "https://feeds.megaphone.fm/BG2POD"),
    ("Invest Like the Best", "https://invest-like-the-best.simplecast.com/episodes.rss"),
    ("Acquired", "https://feeds.megaphone.fm/acquired"),
    ("The Logan Bartlett Show", "https://theloganbartlettshow.libsyn.com/rss"),
    ("In Good Company", "https://feeds.megaphone.fm/ingoodcompany"),
    ("Odd Lots", "https://feeds.megaphone.fm/odd-lots"),
    ("Stratechery", "https://stratechery.com/feed/podcast/"),
)

_DEFAULT_DAYS = 7
_DESC_CHARS = 500
_HTTP_TIMEOUT = 20
_MIN_NAME_LEN = 3


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------


def _parse_pub_date(raw: str) -> tuple[float, str] | None:
    """Parse an RFC 2822 pubDate string; return (timestamp, iso_str) or None."""
    try:
        dt = parsedate_to_datetime(raw.strip())
        ts = dt.timestamp()
        iso = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ts, iso
    except Exception:
        return None


def parse_rss(xml_bytes: bytes, cutoff_ts: float) -> list[dict[str, str | None]]:
    """Parse an RSS feed XML; return episodes published after ``cutoff_ts``.

    Each dict: ``{title, url, description, pub_iso}`` where ``pub_iso`` may be
    ``None`` when the feed omits pubDate. Episodes with a parseable pubDate
    older than the cutoff are skipped. Episodes with no parseable pubDate are
    included (we cannot confirm they are old)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    episodes: list[dict[str, str | None]] = []
    for item in root.iter("item"):
        title_el = item.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue

        # Episode URL: prefer <link>, fall back to <enclosure url="...">.
        url: str | None = None
        link_el = item.find("link")
        if link_el is not None and link_el.text and link_el.text.strip():
            url = link_el.text.strip()
        else:
            enc = item.find("enclosure")
            if enc is not None:
                url = enc.get("url")
        if not url:
            continue

        desc_el = item.find("description")
        description: str = ""
        if desc_el is not None and desc_el.text:
            description = desc_el.text.strip()[:_DESC_CHARS]

        pub_iso: str | None = None
        pub_el = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            parsed = _parse_pub_date(pub_el.text)
            if parsed is not None:
                ts, pub_iso = parsed
                if ts < cutoff_ts:
                    continue  # older than lookback window, skip

        episodes.append(
            {
                "title": title,
                "url": url,
                "description": description or None,
                "pub_iso": pub_iso,
            }
        )
    return episodes


# ---------------------------------------------------------------------------
# Entity name indexes
# ---------------------------------------------------------------------------


def _load_company_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{lower_name: ticker}`` for non-archived tracked companies.
    Short names (< _MIN_NAME_LEN chars) are excluded to avoid false positives."""
    try:
        rows = conn.execute(
            "SELECT ticker, name FROM tracked_companies WHERE archived_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        # archived_at column not present in old schema → fall back without filter.
        try:
            rows = conn.execute("SELECT ticker, name FROM tracked_companies").fetchall()
        except sqlite3.Error:
            return {}
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        ticker = str(row[0]).upper()
        name = row[1]
        if isinstance(name, str) and len(name) >= _MIN_NAME_LEN:
            out[name.lower()] = ticker
    return out


_PAREN_SUFFIX = re.compile(r"\s*\(.*\)\s*$")


def _base_name(display_name: str) -> str:
    """Strip trailing parenthetical disambiguators from investor display names.
    'Appaloosa (Tepper)' → 'appaloosa'; 'Tybourne Capital (wind-down)' → 'tybourne capital'."""
    return _PAREN_SUFFIX.sub("", display_name).strip().lower()


def _load_investor_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{lower_base_name: source_key}`` for active ``investor_13f``
    sources. Trailing parenthetical qualifiers (fund manager name, status note)
    are stripped so 'Appaloosa (Tepper)' matches an episode mentioning 'Appaloosa'.
    Degrades to ``{}`` on a missing ``discovery_sources`` table."""
    try:
        rows = conn.execute(
            "SELECT source_key, display_name FROM discovery_sources "
            "WHERE signal_class = 'investor_13f' AND active = 1"
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        source_key = str(row[0])
        display_name = row[1]
        if isinstance(display_name, str):
            base = _base_name(display_name)
            if len(base) >= _MIN_NAME_LEN:
                out[base] = source_key
    return out


# ---------------------------------------------------------------------------
# Entity matching
# ---------------------------------------------------------------------------


def match_entities(
    text: str,
    company_names: dict[str, str],
    investor_names: dict[str, str],
) -> list[tuple[str, str]]:
    """Return ``[(ticker_or_source_key, match_type)]`` for entities found in
    ``text``. ``match_type`` is ``'company'`` or ``'investor'``.

    Uses word-boundary regex (case-insensitive). Each entity is yielded at most
    once per call even if its name appears multiple times in the text."""
    text_lower = text.lower()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for name, ticker in company_names.items():
        if ticker in seen:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            found.append((ticker, "company"))
            seen.add(ticker)

    for name, source_key in investor_names.items():
        if source_key in seen:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            found.append((source_key, "investor"))
            seen.add(source_key)

    return found


# ---------------------------------------------------------------------------
# HTTP seam (injectable for tests)
# ---------------------------------------------------------------------------


def _default_fetch_url(url: str) -> bytes:
    """Fetch ``url``; raises ``urllib.error.URLError`` / ``OSError`` on failure."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "earnings-summary/1.0 podcast-rss-fetcher"}
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------


def run(
    *,
    db_path: Path,
    days: int = _DEFAULT_DAYS,
    fetch_url: Callable[[str], bytes] = _default_fetch_url,
    feeds: tuple[tuple[str, str], ...] = _FEEDS,
    now: datetime | None = None,
) -> dict[str, int]:
    """Poll all feeds, match entities, write signals. Return a summary dict.

    Injecting ``fetch_url`` lets tests supply canned XML bytes without network
    access. Injecting ``feeds`` lets tests use a single synthetic entry."""
    _now = now or datetime.now(UTC)
    cutoff_ts = _now.timestamp() - days * 86400.0

    try:
        conn = connect_sqlite(
            str(db_path),
            role=SQLiteConnectionRole.WRITER,
            # The feed adapter intentionally supports its historical
            # signals-only schema for deterministic replay.
            schema_preflight=False,
        )
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as exc:
        _log("db_open_failed", db_path=str(db_path), error=str(exc))
        return {"feeds": 0, "episodes": 0, "written": 0}

    try:
        company_names = _load_company_names(conn)
        investor_names = _load_investor_names(conn)
    except Exception:
        conn.close()
        raise

    feeds_ok = 0
    episodes_total = 0
    written_total = 0

    for show_name, feed_url in feeds:
        started = time.monotonic()
        try:
            xml_bytes = fetch_url(feed_url)
            latency_ms = int((time.monotonic() - started) * 1000)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            _log("feed_error", show=show_name, error=str(exc)[:120])
            log_call(
                source_name="podcast_rss",
                kind="feed_poll",
                ticker=None,
                status=CallStatus.ERROR,
                latency_ms=latency_ms,
                notes=f"{show_name}: {str(exc)[:50]}",
            )
            continue

        episodes = parse_rss(xml_bytes, cutoff_ts)
        feeds_ok += 1
        episodes_total += len(episodes)
        log_call(
            source_name="podcast_rss",
            kind="feed_poll",
            ticker=None,
            status=CallStatus.OK,
            latency_ms=latency_ms,
            record_count=len(episodes),
            notes=show_name[:64],
        )

        for ep in episodes:
            search_text = f"{ep['title']} {ep['description'] or ''}"
            matches = match_entities(search_text, company_names, investor_names)
            for entity_key, match_type in matches:
                pub_dt: datetime | None = None
                if ep["pub_iso"]:
                    with contextlib.suppress(ValueError):
                        pub_dt = datetime.fromisoformat(str(ep["pub_iso"]).replace("Z", "+00:00"))
                try:
                    inserted = record_media_appearance(
                        conn,
                        entity_key,
                        str(ep["title"]),
                        url=str(ep["url"]),
                        firm=show_name,
                        body=ep["description"],
                        published_at=pub_dt,
                        now=_now,
                    )
                    status = CallStatus.OK if inserted else CallStatus.SKIPPED
                    if inserted:
                        written_total += 1
                        _log(
                            "signal_written",
                            ticker=entity_key,
                            match_type=match_type,
                            show=show_name,
                            title=str(ep["title"])[:60],
                        )
                    log_call(
                        source_name="podcast_rss",
                        kind="media_appearance",
                        ticker=entity_key if match_type == "company" else None,
                        status=status,
                        notes=f"{show_name}: {str(ep['title'])[:50]}",
                    )
                except sqlite3.Error as exc:
                    _log("write_error", entity_key=entity_key, error=str(exc)[:120])
                    log_call(
                        source_name="podcast_rss",
                        kind="media_appearance",
                        ticker=entity_key if match_type == "company" else None,
                        status=CallStatus.ERROR,
                        notes=str(exc)[:100],
                    )

    conn.close()
    _log("done", feeds_ok=feeds_ok, episodes=episodes_total, written=written_total)
    return {"feeds": feeds_ok, "episodes": episodes_total, "written": written_total}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Podcast RSS → diet media_appearance signals")
    p.add_argument("--days", type=int, default=_DEFAULT_DAYS, help="Lookback window in days")
    p.add_argument("--repo-root", default=None, help="Project root (overrides auto-detection)")
    p.add_argument("--db-path", default=None, help="DB path (overrides DB_PATH default)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.db_path:
        db_path = Path(args.db_path)
    else:
        if args.repo_root:
            root = Path(args.repo_root)
            for _p in (str(root), str(root / "src")):
                if _p not in sys.path:
                    sys.path.insert(0, _p)
        from db import DB_PATH

        db_path = Path(DB_PATH)

    summary = run(db_path=db_path, days=args.days)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
