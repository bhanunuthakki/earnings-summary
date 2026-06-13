"""src/signals/store.py — read/write API + lane taxonomy for the `signals` table.

This module is the single home of the diet-vs-alert taxonomy. The CHECK
constraints in alembic 0095 are kept in LOCKSTEP with the constants here
(``test_signals_store`` asserts the two agree), so a typo can never land an
out-of-vocab ``signal_type`` / ``cadence``.

The lane model (design_language "Diet-vs-alert"):

  * A ``SignalRow`` is a DIET row. It feeds the pull-only diet panel and is
    NEVER turned into an ``InboxItem`` — so it never reaches
    ``inbox_rank.annotate_and_rank`` (the urgency-decay scorer) or the
    materiality veto. ``load_diet_signals`` / ``load_forward_agenda`` are the
    only readers, and both order by stored fields, never by a recency-decay of
    ``now`` (the NON-decaying property the guard pins).

  * Two NEWS-MIRRORED types — ``general_news`` and ``consensus_rating`` —
    additionally carry a ``news_id`` back-reference: the SAME story can also
    back a ``material_news`` alert through the existing news→trigger→alert
    pipeline. That escalation reads the ``news`` row, not the signal row, so
    the lanes stay disjoint. ``inbox_rank._categorize`` consults the stored
    ``signal_type`` (identity over source) to type such an alert, scoped to
    these mirrored types.

  * Three DIET-ONLY types never alert: ``investor_day`` (forward-dated, carries
    an ``event_date``), and the two disclosed fast-follow scaffolds
    ``buyside_rating`` / ``estimate_revision`` (no free data path yet — schema
    scaffolded, never promised).

``event_date`` makes a forward-dated investor day a queryable ROW (sorted by a
dedicated ASC index — the opposite sort from the recency lane), not LLM prose.
``weight`` decouples curation salience from urgency decay; ``cadence`` records
whether a signal recurs (``quarterly``/``scheduled``) or is a one-off
(``event``). ``firm`` is the free-text source entity (sell-side firm, investor,
publisher) — a dedicated ``source_entity`` table is a deliberate fast-follow;
a column is enough for v1.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

__all__ = [
    "CADENCES",
    "CADENCE_EVENT",
    "CADENCE_QUARTERLY",
    "CADENCE_SCHEDULED",
    "DEFAULT_WEIGHTS",
    "DIET_ONLY_TYPES",
    "NEWS_MIRRORED_TYPES",
    "SCAFFOLD_TYPES",
    "SIGNAL_BUYSIDE_RATING",
    "SIGNAL_CONSENSUS_RATING",
    "SIGNAL_ESTIMATE_REVISION",
    "SIGNAL_GENERAL_NEWS",
    "SIGNAL_INVESTOR_DAY",
    "SIGNAL_TYPES",
    "SignalRow",
    "is_diet_only",
    "is_news_mirrored",
    "load_diet_signals",
    "load_forward_agenda",
    "record_investor_day",
    "sync_news_to_signals",
]

# ---------------------------------------------------------------------------
# Taxonomy — kept in lockstep with the 0095 CHECK constraints
# ---------------------------------------------------------------------------

SIGNAL_GENERAL_NEWS = "general_news"
SIGNAL_CONSENSUS_RATING = "consensus_rating"
SIGNAL_INVESTOR_DAY = "investor_day"
SIGNAL_BUYSIDE_RATING = "buyside_rating"
SIGNAL_ESTIMATE_REVISION = "estimate_revision"

SIGNAL_TYPES: frozenset[str] = frozenset(
    {
        SIGNAL_GENERAL_NEWS,
        SIGNAL_CONSENSUS_RATING,
        SIGNAL_INVESTOR_DAY,
        SIGNAL_BUYSIDE_RATING,
        SIGNAL_ESTIMATE_REVISION,
    }
)

# NEWS-mirrored: carries a news_id and can ALSO back a material_news alert via
# the existing pipeline. The signal row itself still never enters the scorer —
# the alert is built from the news row. ``_categorize`` consults the stored
# signal_type only for these (the others have no news backing).
NEWS_MIRRORED_TYPES: frozenset[str] = frozenset({SIGNAL_GENERAL_NEWS, SIGNAL_CONSENSUS_RATING})
# DIET-only: never news-backed, never an alert — pull-lane exclusively.
DIET_ONLY_TYPES: frozenset[str] = SIGNAL_TYPES - NEWS_MIRRORED_TYPES

# Disclosed fast-follows: scaffolded so the panel can show "coming via paid
# feed" without inventing data. No live free path (FMP analyst is Ultimate-
# gated); the diet substrate must not promise data it can't fetch.
SCAFFOLD_TYPES: frozenset[str] = frozenset({SIGNAL_BUYSIDE_RATING, SIGNAL_ESTIMATE_REVISION})

CADENCE_QUARTERLY = "quarterly"
CADENCE_SCHEDULED = "scheduled"
CADENCE_EVENT = "event"
CADENCES: frozenset[str] = frozenset({CADENCE_QUARTERLY, CADENCE_SCHEDULED, CADENCE_EVENT})

# Curation salience per type (decoupled from urgency decay — a rating change is
# more worth-ingesting than a routine wire story, independent of how fresh it
# is). Used as the secondary sort + the salience pill; never a decay factor.
DEFAULT_WEIGHTS: dict[str, float] = {
    SIGNAL_CONSENSUS_RATING: 0.8,
    SIGNAL_INVESTOR_DAY: 0.7,
    SIGNAL_BUYSIDE_RATING: 0.85,
    SIGNAL_ESTIMATE_REVISION: 0.75,
    SIGNAL_GENERAL_NEWS: 0.5,
}

# The exact UTC stored shape of published_at / created_at — same as `news`
# (src/news/store.py) so a mirrored row keeps a chronological lexical sort.
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def is_diet_only(signal_type: str) -> bool:
    """True iff this type is pull-lane exclusive — it has no news backing and
    can never escalate to a decaying alert."""
    return signal_type in DIET_ONLY_TYPES


def is_news_mirrored(signal_type: str) -> bool:
    """True iff a story of this type can ALSO back a ``material_news`` alert via
    the news→trigger→alert pipeline (``_categorize`` consults the stored
    signal_type only for these). The signal row still never enters the scorer."""
    return signal_type in NEWS_MIRRORED_TYPES


# ---------------------------------------------------------------------------
# Row model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalRow:
    """One decoded `signals` row — a DIET row, never an ``InboxItem``."""

    id: int
    ticker: str
    signal_type: str
    title: str
    published_at: str
    weight: float
    cadence: str
    body: str | None = None
    url: str | None = None
    firm: str | None = None
    event_date: str | None = None
    source_feed: str | None = None
    news_id: int | None = None


_SELECT_COLS = (
    "id, ticker, signal_type, title, published_at, weight, cadence, "
    "body, url, firm, event_date, source_feed, news_id"
)


def _row_to_signal(row: Sequence[object]) -> SignalRow:
    return SignalRow(
        id=int(row[0]),  # type: ignore[arg-type]
        ticker=str(row[1]),
        signal_type=str(row[2]),
        title=str(row[3]),
        published_at=str(row[4]),
        weight=float(row[5]),  # type: ignore[arg-type]
        cadence=str(row[6]),
        body=str(row[7]) if row[7] is not None else None,
        url=str(row[8]) if row[8] is not None else None,
        firm=str(row[9]) if row[9] is not None else None,
        event_date=str(row[10]) if row[10] is not None else None,
        source_feed=str(row[11]) if row[11] is not None else None,
        news_id=int(row[12]) if row[12] is not None else None,  # type: ignore[arg-type]
    )


def _open_ro(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Read-only connection, or None when the DB is absent/unopenable — readers
    degrade to ``[]`` so a render path never breaks on a missing DB."""
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# Readers (the diet panel's two lenses — both NON-decaying)
# ---------------------------------------------------------------------------


def load_diet_signals(
    db_path: Path | str | None,
    *,
    types: Iterable[str] | None = None,
    limit: int = 60,
) -> list[SignalRow]:
    """The ingest stream: recent diet signals, newest first.

    Ordering is ``published_at DESC`` (display recency) with ``weight DESC`` to
    break ties toward higher curation salience — explicitly NOT a recency-DECAY
    of ``now``. This is the non-decaying property the guard pins: the order of a
    fixed set of rows is independent of the wall clock. Best-effort: a missing
    table / DB yields ``[]``.

    ``types`` defaults to the news-backed reading types (general news + sell-
    side ratings) — the lanes that have live free data. The forward-dated
    investor-day lane has its own reader (``load_forward_agenda``)."""
    wanted = tuple(types) if types is not None else (SIGNAL_GENERAL_NEWS, SIGNAL_CONSENSUS_RATING)
    if not wanted:
        return []
    conn = _open_ro(db_path)
    if conn is None:
        return []
    try:
        marks = ",".join("?" for _ in wanted)
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM signals "
            f"WHERE signal_type IN ({marks}) "
            "ORDER BY published_at DESC, weight DESC, id DESC LIMIT ?",
            (*wanted, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_signal(r) for r in rows]


def load_forward_agenda(
    db_path: Path | str | None,
    *,
    on_or_after: date,
    limit: int = 40,
) -> list[SignalRow]:
    """The forward agenda: upcoming forward-dated signals (investor days),
    soonest first.

    Reads the dedicated ``event_date`` index — ``event_date ASC`` is the OPPOSITE
    sort from the recency lane, which is exactly why the index is separate.
    Still non-decaying: the order depends on stored ``event_date``, never on a
    decay of ``now``."""
    conn = _open_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM signals "
            "WHERE event_date IS NOT NULL AND event_date >= ? "
            "ORDER BY event_date ASC, weight DESC, id ASC LIMIT ?",
            (on_or_after.isoformat(), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_signal(r) for r in rows]


# ---------------------------------------------------------------------------
# Writers / reconcilers (own a connection + commit — never a render path)
# ---------------------------------------------------------------------------


def _now_stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).replace(tzinfo=None).strftime(_DATETIME_FORMAT)


def sync_news_to_signals(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Mirror `news` rows into `signals` as typed diet rows; return the count
    inserted this call.

    The mapping (per the substrate spec): a ``yf_grades`` story becomes a typed
    ``consensus_rating`` (routing the already-working free sell-side feed into
    the typed lane, replacing the render-time headline regex); every other story
    is ``general_news``. Idempotent — ``INSERT OR IGNORE`` on the
    ``ux_signals_news_id`` unique index, so re-running after each news fetch only
    adds genuinely-new stories and never duplicates. Best-effort on a pre-0095
    DB (no `signals` / `news` table) → returns 0 without raising, so the news
    write path is never blocked by the mirror.
    """
    stamp = _now_stamp(now)
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO signals
                (ticker, signal_type, title, body, url, firm,
                 event_date, published_at, weight, cadence, source_feed,
                 news_id, created_at)
            SELECT
                ticker,
                CASE WHEN source_feed = 'yf_grades' THEN ? ELSE ? END,
                headline, snippet, url, source,
                NULL, published_at,
                CASE WHEN source_feed = 'yf_grades' THEN ? ELSE ? END,
                ?, source_feed, id, ?
            FROM news
            """,
            (
                SIGNAL_CONSENSUS_RATING,
                SIGNAL_GENERAL_NEWS,
                DEFAULT_WEIGHTS[SIGNAL_CONSENSUS_RATING],
                DEFAULT_WEIGHTS[SIGNAL_GENERAL_NEWS],
                CADENCE_EVENT,
                stamp,
            ),
        )
    except sqlite3.Error:
        return 0
    conn.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def record_investor_day(
    conn: sqlite3.Connection,
    ticker: str,
    event_date: date,
    title: str,
    *,
    firm: str | None = None,
    url: str | None = None,
    body: str | None = None,
    source_feed: str = "ir_events",
    weight: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Record one forward-dated investor/analyst day; return True iff inserted.

    The writer the (fast-follow) IR-events scrape calls — an EXTENSION of the
    existing ``expected_earnings`` calendar machinery into the typed signal
    store, not a greenfield event table. Idempotent on ``ux_signals_event``
    (ticker, signal_type, event_date), so re-scraping the same calendar is a
    no-op. ``published_at`` is the discovery time (now); ``event_date`` is the
    forward agenda date. Caller commits via this function.
    """
    stamp = _now_stamp(now)
    w = weight if weight is not None else DEFAULT_WEIGHTS[SIGNAL_INVESTOR_DAY]
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO signals
            (ticker, signal_type, title, body, url, firm,
             event_date, published_at, weight, cadence, source_feed,
             news_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            ticker.upper(),
            SIGNAL_INVESTOR_DAY,
            title,
            body,
            url,
            firm,
            event_date.isoformat(),
            stamp,
            w,
            CADENCE_SCHEDULED,
            source_feed,
            stamp,
        ),
    )
    conn.commit()
    return bool(cur.rowcount and cur.rowcount > 0)
