""" "Since you last looked" — the Today headline band (navigation_ia §4 PR3).

The stamp is the cheap part (client-side ``ix-last-seen:overview``, the same
localStorage convention ``dashboard/inbox.py`` already ships); this module is
the honest hard part it deferred: the aggregation builder that turns
``[since, now]`` into a short, dense line-set of what actually happened —
new diet signals, decisions logged/graded, falsifiers armed, new source
documents, and newly-scheduled earnings. Every non-zero item is a doorway;
an empty window renders one explicit quiet line — never a blank box (the
``open_loops.py`` precedent this module follows verbatim: independently
guarded queries, so a missing table can never break Home).

Timestamp bridging: the columns this module reads were written by different
call sites in different (but all SQLite-legal) shapes — ``signals``/
``documents``/``expected_earnings`` in space-separated ``YYYY-MM-DD HH:MM:SS``,
``decisions`` in ``now_iso()``'s ``T``-separated isoformat with microseconds.
Comparing those as raw strings is wrong (``' ' < 'T'`` in ASCII, so a
space-stamped row would sort before a same-instant T-stamped bound
regardless of actual time). Every query instead wraps BOTH the column and
the bound in SQLite's own ``datetime()``, which accepts every one of the
above shapes per the documented time-value grammar and normalizes them to
one comparable form — no per-table format bookkeeping, no fragile string
math.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from clock import to_naive_utc
from user_state._db import open_conn

__all__ = ["SinceLastItem", "SinceLastStory", "build_since_last", "render_since_last_band"]

# Doorways are PANEL hashes (see open_loops.py's comment on the same idiom):
# interpolated rather than written as one literal so a future CSS-emitting
# rewrite of this module can't have a '#dec...'/'#die...' fragment misread as
# a raw hex color by the token guard's scan (tests/test_ui_controls.py).
_DIET_PANEL = "diet"
_DECISIONS_PANEL = "decisions_record"
_DIET_HASH = f"/#{_DIET_PANEL}"
_DECISIONS_HASH = f"/#{_DECISIONS_PANEL}"


@dataclass(frozen=True)
class SinceLastItem:
    """One doorway line: ``"{label}: {count}{suffix}"`` linking to ``href``."""

    href: str
    label: str
    count: int
    suffix: str = ""


@dataclass(frozen=True)
class SinceLastStory:
    since: datetime
    now: datetime
    items: tuple[SinceLastItem, ...]

    @property
    def is_quiet(self) -> bool:
        return not self.items


def _fmt(dt: datetime) -> str:
    """Naive-UTC ``datetime`` -> a SQLite time-value bound. Space-separated
    (matches the majority writer shape); ``datetime()`` on the query side
    normalizes it against every column shape regardless."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _tickers_suffix(raw: object, cap: int = 3) -> str:
    """``GROUP_CONCAT(DISTINCT ticker)`` -> ``" (MELI, NU, +2 more)"``; ``""``
    when there's nothing to show."""
    if not raw:
        return ""
    tickers = sorted({t.strip() for t in str(raw).split(",") if t.strip()})
    if not tickers:
        return ""
    shown = tickers[:cap]
    overflow = len(tickers) - len(shown)
    text = ", ".join(html.escape(t, quote=True) for t in shown)
    if overflow > 0:
        text += f", +{overflow} more"
    return f" ({text})"


def _first_ticker(raw: object) -> str | None:
    if not raw:
        return None
    tickers = sorted({t.strip() for t in str(raw).split(",") if t.strip()})
    return tickers[0] if tickers else None


# ---------------------------------------------------------------------------
# Per-substrate reads — each independently guarded by the caller (build_since_
# last), never by these helpers, matching open_loops.py's split.
# ---------------------------------------------------------------------------


def _signals_activity(db_path: Path | str | None, since_s: str, now_s: str) -> tuple[int, str]:
    """New information-diet rows (``signals.created_at`` — the ingest write
    stamp, not ``published_at``, which can be backdated to the source's own
    publish time and would misrepresent "since you looked")."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), GROUP_CONCAT(DISTINCT ticker) FROM signals "
            "WHERE datetime(created_at) > datetime(?) AND datetime(created_at) <= datetime(?)",
            (since_s, now_s),
        ).fetchone()
        return int(row[0] or 0), _tickers_suffix(row[1])
    finally:
        conn.close()


def _falsifiers_armed_count(db_path: Path | str | None, since_s: str, now_s: str) -> int:
    """Owner decisions created in-window that already carry a falsifier.
    ``falsifier`` is write-once at the writer (0130) — there is no separate
    "armed at" stamp — so a newly-created decision with a non-NULL falsifier
    IS the arm event."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE falsifier IS NOT NULL "
            "AND datetime(created_at) > datetime(?) AND datetime(created_at) <= datetime(?)",
            (since_s, now_s),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _decisions_logged_count(db_path: Path | str | None, since_s: str, now_s: str) -> int:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE datetime(created_at) > datetime(?) AND datetime(created_at) <= datetime(?)",
            (since_s, now_s),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _decisions_graded_count(db_path: Path | str | None, since_s: str, now_s: str) -> int:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE outcome_at IS NOT NULL "
            "AND outcome_label IS NOT NULL AND outcome_label != 'pending' "
            "AND datetime(outcome_at) > datetime(?) AND datetime(outcome_at) <= datetime(?)",
            (since_s, now_s),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _documents_activity(
    db_path: Path | str | None, since_s: str, now_s: str
) -> tuple[int, str, str | None]:
    """New source documents (``documents.fetched_at``) — count, ticker
    suffix, and the first (alphabetical) ticker for the doorway target."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), GROUP_CONCAT(DISTINCT ticker) FROM documents "
            "WHERE datetime(fetched_at) > datetime(?) AND datetime(fetched_at) <= datetime(?)",
            (since_s, now_s),
        ).fetchone()
        return int(row[0] or 0), _tickers_suffix(row[1]), _first_ticker(row[1])
    finally:
        conn.close()


def _earnings_scheduled_activity(
    db_path: Path | str | None, since_s: str, now_s: str
) -> tuple[int, str, str | None]:
    """Newly-scheduled earnings dates — a fresh ``expected_earnings`` row
    (``first_seen_at``, not ``last_seen_at``, which just re-confirms an
    already-known date on every watcher run)."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), GROUP_CONCAT(DISTINCT ticker) FROM expected_earnings "
            "WHERE datetime(first_seen_at) > datetime(?) AND datetime(first_seen_at) <= datetime(?)",
            (since_s, now_s),
        ).fetchone()
        return int(row[0] or 0), _tickers_suffix(row[1]), _first_ticker(row[1])
    finally:
        conn.close()


def build_since_last(db_path: Path | str | None, since: datetime, now: datetime) -> SinceLastStory:
    """Assemble the story for ``(since, now]``. Every read is independently
    guarded — a missing table (a pre-migration DB, or a partial fixture)
    just skips that item; this function itself never raises."""
    since_n = to_naive_utc(since)
    now_n = to_naive_utc(now)
    since_s, now_s = _fmt(since_n), _fmt(now_n)
    items: list[SinceLastItem] = []

    try:
        n, suffix = _signals_activity(db_path, since_s, now_s)
        if n:
            items.append(SinceLastItem(_DIET_HASH, "Signals", n, suffix))
    except Exception:
        pass
    try:
        n = _falsifiers_armed_count(db_path, since_s, now_s)
        if n:
            items.append(SinceLastItem(_DECISIONS_HASH, "Falsifiers armed", n))
    except Exception:
        pass
    try:
        n = _decisions_logged_count(db_path, since_s, now_s)
        if n:
            items.append(SinceLastItem(_DECISIONS_HASH, "Decisions logged", n))
    except Exception:
        pass
    try:
        n = _decisions_graded_count(db_path, since_s, now_s)
        if n:
            items.append(SinceLastItem(_DECISIONS_HASH, "Decisions graded", n))
    except Exception:
        pass
    try:
        n, suffix, first_ticker = _documents_activity(db_path, since_s, now_s)
        if n:
            href = f"/#holding={first_ticker}" if first_ticker else _DIET_HASH
            items.append(SinceLastItem(href, "New documents", n, suffix))
    except Exception:
        pass
    try:
        n, suffix, first_ticker = _earnings_scheduled_activity(db_path, since_s, now_s)
        if n:
            href = f"/#holding={first_ticker}" if first_ticker else _DIET_HASH
            items.append(SinceLastItem(href, "Earnings scheduled", n, suffix))
    except Exception:
        pass

    return SinceLastStory(since=since_n, now=now_n, items=tuple(items))


def render_since_last_band(story: SinceLastStory) -> str:
    """One dense line per non-empty item; an explicit quiet line when
    nothing happened. Deliberately emits no CSS of its own — reuses
    ``open_loops.py``'s already-inlined ``.cc-open-loops``/``.cc-ol-*``
    classes verbatim (this fragment is only ever injected into a page that
    already rendered that band), so it is not a new design-language surface.
    Never raises: formatting only, over already-validated ``story`` data."""
    if not story.items:
        days = max((story.now - story.since).days, 0)
        return (
            '<div class="cc-open-loops">'
            f'<span class="cc-ol-clear">Quiet since your last look — {days}d ago.</span>'
            "</div>"
        )
    lines = "".join(
        f'<a class="cc-ol-line" href="{html.escape(it.href, quote=True)}">'
        f"{html.escape(it.label, quote=True)}: "
        f'<span class="cc-ol-count">{it.count}</span>{it.suffix}</a>'
        for it in story.items
    )
    return (
        '<div class="cc-open-loops"><span class="cc-ol-head">Since you last looked</span>'
        + lines
        + "</div>"
    )
