"""
src/news/store.py — the single validated persistence gate for the `news` table.

Both ingestion feeds — the primary FMP stock-news fetcher and the
FMP-independent WebSearch+Opus fallback — map their source-specific payloads
into one canonical ``NewsRow`` and write through ``upsert_news_rows``.
Centralizing the write here means the table's contract — above all the UTC
``published_at`` format the material_news trigger's lexical recency compare
depends on — is enforced once, in code, regardless of which feed produced the
row.

The `news` table is created by Alembic migration ``0065_news``; this module
never creates schema (executors own data, migrations own schema). If the table
is absent, ``upsert_news_rows`` raises ``sqlite3.OperationalError`` and the
calling fetcher logs and exits non-zero rather than silently dropping news.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The exact stored shape of `published_at` (and `fetched_at`): UTC, naive, fixed
# width, space separator. The trigger compares ``published_at >= ?`` LEXICALLY
# (src/triggers/material_news.py _format_threshold), which is chronological only
# if every value is this canonical form — no 'T', no zone suffix, no fractional
# seconds. NewsRow's validator is the code-side enforcer the TEXT column cannot
# be.
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Plausibility bounds for ``published_at``. A feed that emits a garbage timestamp
# — an epoch-conversion overflow (1970 / a wild future year) or a genuinely
# future-dated story — passes the *format* check above and then skews the
# material_news trigger's lexical 24h recency window: a future date sorts at the
# top and fires the alert immediately. The floor rejects pre-2000 garbage; the
# ceiling rejects anything more than a small skew ahead of "now" (tolerating
# feed/zone clock drift), so a real just-published story still passes while
# 2050 does not. Bounds are intentionally wide — this is a sanity gate, not a
# recency filter (the trigger owns recency).
_MIN_PUBLISHED_YEAR = 2000
_FUTURE_SKEW_DAYS = 2

# `source_feed` provenance tags — which ingester wrote the row. Defined here so
# the feeds and the dedup tests reference one spelling, not scattered literals.
# SOURCE_FEED_FMP matches the column's server_default in migration 0065_news.
# The EDGAR/grades tags are the ADDITIVE feeds (directives/news_sources_plan.md):
# they supplement the FMP→WebSearch ladder, never replace it.
SOURCE_FEED_FMP = "fmp_stock_news"
SOURCE_FEED_WEBSEARCH = "websearch_opus"
SOURCE_FEED_EDGAR_8K = "edgar_8k"
SOURCE_FEED_EDGAR_13D = "edgar_13d"
SOURCE_FEED_EDGAR_13G = "edgar_13g"
SOURCE_FEED_YF_GRADES = "yf_grades"
# Competitor IPO S-1 watch (src/competitive/sec_watch.py): an EDGAR full-text
# search flags when a watched private competitor (Cohesity) files its IPO S-1.
# The row is attributed to the affected holding's ticker (RBRK) since it is
# material to THAT thesis, not the filer's (the filer isn't in our book).
SOURCE_FEED_EDGAR_S1_WATCH = "edgar_s1_watch"


class NewsRow(BaseModel):
    """A validated news row ready to persist — the single contract gate for the
    `news` table. Both the FMP and WebSearch+Opus feeds map into this.

    ``extra="forbid"`` so a feed that drifts an unexpected field in fails loud at
    construction rather than silently carrying it.
    """

    model_config = ConfigDict(extra="forbid")

    # min_length=1 on the identity + dedup-key fields: an empty ticker or url
    # silently degrades the (ticker, url) uniqueness key, and an empty headline
    # makes a material-news alert meaningless. Reject the row at the gate rather
    # than persisting a story with no source link (regrade memo gap #5).
    ticker: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    url: str = Field(min_length=1)
    published_at: str  # UTC 'YYYY-MM-DD HH:MM:SS' — enforced below
    snippet: str | None = None
    source: str | None = None
    source_feed: str  # SOURCE_FEED_FMP | SOURCE_FEED_WEBSEARCH

    @field_validator("published_at")
    @classmethod
    def _utc_fixed_format(cls, v: str) -> str:
        """Reject any value that isn't EXACTLY UTC 'YYYY-MM-DD HH:MM:SS' *and*
        within a plausible date range.

        Raises (rejecting the row) rather than coercing — a malformed timestamp
        is a feed bug, and a wrongly-shaped value would silently skew the
        trigger's lexical 24h window. ISO-with-'T', fractional seconds, a zone
        suffix, a date alone, and empty all fail ``strptime`` outright.

        The round-trip equality check then closes the one gap ``strptime`` leaves
        open: it parses non-zero-padded fields (``'2026-5-30 ...'``), which would
        be the wrong *width* and so sort incorrectly under the trigger's lexical
        compare. A value is canonical iff re-emitting it via the same format
        reproduces it byte-for-byte — the airtight form of the §2.3 invariant.

        Finally, a *plausibility* gate (see ``_MIN_PUBLISHED_YEAR`` /
        ``_FUTURE_SKEW_DAYS``): a correctly-shaped but absurd timestamp (a 1970
        epoch artifact, or a 2050 future-dated story that would fire
        material_news immediately) is rejected here, where every persisted row
        is funneled, rather than silently corrupting the recency window.
        """
        parsed = datetime.strptime(v, _DATETIME_FORMAT)
        if parsed.strftime(_DATETIME_FORMAT) != v:
            raise ValueError(f"published_at must be canonical UTC '{_DATETIME_FORMAT}': {v!r}")
        if parsed.year < _MIN_PUBLISHED_YEAR:
            raise ValueError(
                f"published_at year {parsed.year} predates {_MIN_PUBLISHED_YEAR} "
                f"(implausible / epoch artifact): {v!r}"
            )
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        if parsed > now_utc + timedelta(days=_FUTURE_SKEW_DAYS):
            raise ValueError(
                f"published_at {v!r} is more than {_FUTURE_SKEW_DAYS}d in the future "
                f"(feed clock bug; would skew the trigger's recency window)"
            )
        return v


def upsert_news_rows(conn: sqlite3.Connection, rows: Iterable[NewsRow]) -> tuple[int, int]:
    """INSERT OR IGNORE each row into `news`; return ``(inserted, deduped)``.

    Idempotent on the table's ``UNIQUE (ticker, url)`` constraint: a row whose
    (ticker, url) already exists is ignored (counted in ``deduped``), so both
    feeds — and same-day re-runs — converge without duplicating stories. A story
    seen by both feeds under the same ticker is stored once; a syndicated URL
    under two tickers is two rows (the compound key is the point).

    ``fetched_at`` is stamped here in UTC at write time (one value per call). The
    `news` table is assumed pre-created by migration ``0065_news``; if it is
    absent this raises ``sqlite3.OperationalError``, which the caller surfaces
    (exit non-zero) rather than dropping news silently.
    """
    fetched_at = datetime.now(UTC).strftime(_DATETIME_FORMAT)
    inserted = 0
    total = 0
    for row in rows:
        total += 1
        cur = conn.execute(
            "INSERT OR IGNORE INTO news "
            "(ticker, headline, url, published_at, snippet, source, source_feed, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.ticker,
                row.headline,
                row.url,
                row.published_at,
                row.snippet,
                row.source,
                row.source_feed,
                fetched_at,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    # Mirror the freshly-written stories into the typed diet substrate
    # (`signals`, alembic 0095) so the information-diet panel stays current
    # off the same fetch — no new cron. Best-effort and idempotent
    # (INSERT OR IGNORE on ux_signals_news_id): a pre-0095 DB with no `signals`
    # table degrades to a no-op, so the news write path is never blocked by the
    # mirror. The `news` table itself is untouched (non-destructive).
    if inserted:
        try:
            from signals.store import sync_news_to_signals

            sync_news_to_signals(conn)
        except Exception:  # pragma: no cover - the mirror never blocks news
            pass
    return inserted, total - inserted


# ---------------------------------------------------------------------------
# Cross-feed story dedup (the additive-feed guard)
# ---------------------------------------------------------------------------

_HEADLINE_NORMALIZE_RX = re.compile(r"[^a-z0-9]+")


def normalize_headline(headline: str) -> str:
    """Canonical headline form for cross-feed dedup: lowercase, every run of
    non-alphanumerics collapsed to one space, ends stripped — so punctuation,
    smart quotes, and spacing differences between two feeds' renderings of the
    same story compare equal."""
    return _HEADLINE_NORMALIZE_RX.sub(" ", headline.lower()).strip()


def _story_key(row: NewsRow) -> tuple[str, str, str]:
    """(ticker, normalized headline, published DATE) — same story, any feed."""
    return row.ticker, normalize_headline(row.headline), row.published_at[:10]


def drop_duplicate_stories(
    conn: sqlite3.Connection,
    candidates: Iterable[NewsRow],
    *,
    against: Iterable[NewsRow] = (),
) -> list[NewsRow]:
    """Filter ``candidates`` down to rows whose (ticker, normalized headline,
    published date) is not already present — in ``against`` (rows queued for the
    same write by another feed), earlier in ``candidates`` itself, or in the
    `news` table.

    This is the ADDITIVE-feed guard: ``upsert_news_rows``'s ``UNIQUE (ticker,
    url)`` key dedupes re-runs of one feed, but the same story arriving from two
    feeds carries two different urls. Additive feeds (EDGAR, grades) run their
    rows through here so they supplement — never duplicate — the primary feeds.

    Best-effort against the DB: if the `news` table is absent or unreadable the
    check degrades to batch-only dedup (the subsequent upsert still surfaces a
    missing table loudly)."""
    seen: set[tuple[str, str, str]] = {_story_key(row) for row in against}
    kept: list[NewsRow] = []
    for row in candidates:
        key = _story_key(row)
        if key in seen:
            continue
        ticker, normalized, day = key
        try:
            existing = conn.execute(
                "SELECT headline FROM news WHERE ticker = ? AND substr(published_at, 1, 10) = ?",
                (ticker, day),
            ).fetchall()
        except sqlite3.Error:
            existing = []
        if any(normalize_headline(str(h)) == normalized for (h,) in existing):
            seen.add(key)
            continue
        seen.add(key)
        kept.append(row)
    return kept
