"""Today's Senior Partner Brief doorway (P2.2, PRD §9.1 frontend/§12.2
loading-and-failure states).

Leads ``render_overview_panel``'s composition (PRD: "the brief card leads
render_overview_panel's composition — absorb/replace the standup band, keep
a doorway"). The band composed here is a compact summary + the highest-
priority item + an effort estimate + an active-week explanation when
applicable, with Why? / Correct context / Record-confirm decision / Defer /
Dismiss reachable through the SAME action cores Telegram and the mobile
Inbox use (governor pings / decision drafts / card dispositions) — this
panel itself only renders a read-only summary + a doorway link; it never
defines its own duplicate action wiring.

§12.2 states, kept distinct (never collapsed into the same blank card):
  - loading: N/A — this is a server-rendered synchronous read, no async load.
  - not generated: no ``senior_partner_brief`` artifact exists yet.
  - failed: DB unreachable / artifact content failed to parse.
  - stale: the artifact's ISO week is not the current one.
  - last-valid: a stale artifact still renders (labeled stale), never blanked.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from html import escape
from pathlib import Path
from typing import cast

import llm_artifact_store
from ui.time import stamp_html

PURPOSE = "senior_partner_brief"

# Task 2 (wave3b — navigation_ia.md D1 band consolidation): the doorway chip
# + timestamp shape both this module's :func:`render_brief_today_card` and
# ``allocation_recommendation_panel.render_allocation_today_card`` emit —
# `<a class="k-chip k-chip-btn" href="...">...</a><span class="muted">...</span>`.
# Matching on this shape (rather than importing a private helper from the
# allocation module, which this PR does not own — a sibling PR does) lets
# :func:`render_today_doorways_card` fold BOTH today-cards' chips into one
# shared well while calling each module's PUBLIC, unmodified render function.
_CHIP_FRAGMENT_RE = re.compile(
    r'<a class="k-chip k-chip-btn".*?</a>\s*<span class="muted">.*?</span>', re.DOTALL
)

_STYLE = """<style>
.cc-spb-today { display: flex; align-items: baseline; gap: var(--sp-2); flex-wrap: wrap;
  margin: 0 0 var(--sp-2); }
.cc-spb-today .muted { color: var(--muted); font-size: var(--fs-caption); }
</style>"""


def _current_iso_week(now: datetime | None = None) -> tuple[int, int]:
    stamp = now or datetime.now()
    y, w, _ = stamp.isocalendar()
    return int(y), int(w)


def render_brief_today_card(
    db_path: Path | str | None,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """The Today compact doorway: highest-priority item (or a "what changed"
    headline) + effort estimate + stale/failed states, deep-linked to
    ``/mobile/inbox`` (the full brief surface — PRD §9.1 keeps ONE reading
    surface; this card is a glance, not a second copy of the brief). Renders
    nothing when there is no current artifact at all (Today stays quiet, like
    the sibling ``allocation_recommendation_panel.render_allocation_today_card``)
    — never a bare "not generated" stub on the landing page. Never raises."""
    if db_path is None:
        return ""
    try:
        artifact = llm_artifact_store.read_current(
            ticker=None,
            purpose=PURPOSE,
            scope="portfolio",
            db_path=Path(db_path),
            conn=conn,
        )
    except Exception:
        return '<div class="cc-spb-today k-well k-well-bad">Senior Partner Brief unavailable.</div>'
    if artifact is None:
        return ""
    if not isinstance(artifact.content_json, dict):
        return '<div class="cc-spb-today k-well k-well-bad">Senior Partner Brief failed to read back.</div>'

    content = cast("dict[str, object]", artifact.content_json)
    iso_year = content.get("iso_year")
    iso_week = content.get("iso_week")
    cur_year, cur_week = _current_iso_week(now)
    is_stale = not (iso_year == cur_year and iso_week == cur_week)

    headline = "Senior Partner Brief"
    highest = content.get("highest_priority_decision")
    if isinstance(highest, dict):
        title = str(cast("dict[str, object]", highest).get("title") or "")
        if title:
            headline = title
    elif content.get("what_changed"):
        changed = content["what_changed"]
        if isinstance(changed, list) and changed and isinstance(changed[0], dict):
            title = str(cast("dict[str, object]", changed[0]).get("title") or "")
            if title:
                headline = title

    mode = str(content.get("selection_mode") or "llm")
    mode_note = " (mechanical digest)" if mode == "deterministic_fallback" else ""
    stale_note = " · stale" if is_stale else ""
    stamp = stamp_html(artifact.generated_at, mode="rel")
    return (
        f"{_STYLE}"
        f'<div class="cc-spb-today k-well" data-artifact-id="{artifact.id}">'
        f'<a class="k-chip k-chip-btn" href="/mobile/inbox">{escape(headline)}{escape(mode_note)}</a>'
        f'<span class="muted">{stamp}{escape(stale_note)}</span></div>'
    )


def _extract_chip_fragment(card_html: str) -> str | None:
    """Pull just the doorway chip + timestamp out of a self-contained Today
    card (STYLE + one ``.k-well`` div wrapping exactly one chip+timestamp
    pair), so two such cards can be recomposed into one shared well. Returns
    ``None`` for ``""`` (nothing to show) OR a card that isn't shaped like a
    plain chip — a ``k-well-bad`` failure banner, say — which stays its own
    full, visible card rather than being squeezed into a shared well (a DB
    failure deserves to stay legible, not shrunk into chip text)."""
    if not card_html:
        return None
    m = _CHIP_FRAGMENT_RE.search(card_html)
    return m.group(0) if m else None


def render_today_doorways_card(
    db_path: Path | str | None,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Task 2 (navigation_ia.md D1): fold the allocation-today card into the
    Senior Partner Brief Today card — ONE ``.k-well`` with up to two doorway
    chips (brief first, allocation second) instead of two separate wells.
    Both chips' hrefs are preserved EXACTLY — this calls each module's own
    unmodified render function and only re-wraps the already-rendered chip
    fragment; it never rebuilds the anchor markup itself. A failure/stale
    state on either side (or the allocation module simply not existing on an
    old checkout) stays a full, separately visible card rather than being
    squeezed into the shared well. Never raises: an import or render failure
    on the allocation side degrades to brief-only, matching this band's
    sibling try/except discipline in ``execution/comments_server.py``."""
    brief_html = render_brief_today_card(db_path, now=now, conn=conn)
    try:
        from pipeline.allocation_recommendation_panel import render_allocation_today_card

        alloc_html = render_allocation_today_card(db_path, conn=conn)
    except Exception:
        alloc_html = ""

    brief_chip = _extract_chip_fragment(brief_html)
    alloc_chip = _extract_chip_fragment(alloc_html)

    parts: list[str] = []
    chips = [c for c in (brief_chip, alloc_chip) if c is not None]
    if chips:
        parts.append(f'{_STYLE}<div class="cc-spb-today k-well">{"".join(chips)}</div>')
    if brief_chip is None and brief_html:
        parts.append(brief_html)
    if alloc_chip is None and alloc_html:
        parts.append(alloc_html)
    return "".join(parts)


__all__ = ["render_brief_today_card", "render_today_doorways_card"]
