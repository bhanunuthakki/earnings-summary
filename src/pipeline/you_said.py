""" "You said" strip -- the owner's own last decision on a ticker, ambient.

Owner-ratified design review (2026-08-02): the most personalized data the
platform holds is the owner's own ``decisions`` table (~103 rows, 48 tickers,
``decision_conditions`` populated on all, ~39 graded owner rows). This module
turns that into ONE dense line -- last decision verb + date, a short excerpt
of the owner's own rationale (their words), conviction, the nearest still-live
falsifiable condition, and the current tracking (grading) state -- rendered
above the ticker peek's mini-card facts, near the Holding tab header, and on
the workspace report's position header.

Deterministic, zero LLM calls, no new queues: a plain read over ``decisions``
(+ the already-stamped ``decision_conditions`` JSON column, alembic 0086/0130)
and :func:`decision_conditions.conditions_from_json`. Degrades to the D4 empty
primitive (``ui.controls.k_empty``) -- one line + a doorway chip into the
capture tray -- when no owner decision is on file for the ticker, including on
a pre-0130 schema (no ``decided_by`` column) where "owner decision" cannot
even be asked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from decision_conditions import DecisionCondition, conditions_from_json
from report.renderers.numfmt import fmt_date
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import k_empty

__all__ = [
    "YouSaid",
    "excerpt_text",
    "load_you_said",
    "render_you_said_line",
    "render_you_said_strip",
    "render_you_said_strip_for_path",
]

_EXCERPT_MAX = 90
_COND_MAX = 80

# Comparator vocabulary (decision_conditions.CONDITION_OPS) -> display symbol.
# Mirrors pipeline.ledger_panel._condition_text's mapping -- duplicated, not
# imported, per the repo's duplicate-simple-shared-logic convention
# (feedback_duplicate_simple_shared_logic.md): a 4-entry dict is cheaper to
# keep in sync by inspection than to couple two panel modules over.
_OP_SYMBOL: dict[str, str] = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥"}

_OUTCOME_TONE: dict[str, str] = {"correct": " k-pill-ok", "wrong": " k-pill-bad"}


@dataclass(frozen=True, slots=True)
class YouSaid:
    """One rendered "you said" line's worth of data -- the owner's own most
    recent decision on a ticker, plus its nearest live falsifier."""

    ticker: str
    verb: str
    made_at: str
    excerpt: str | None
    rationale_full: str | None
    conviction: str | None
    condition_text: str | None
    outcome_label: str | None


def excerpt_text(raw: object, *, max_len: int = _EXCERPT_MAX) -> str | None:
    """The owner's rationale, truncated to ~``max_len`` chars on a word
    boundary (never mid-word), whitespace-collapsed. ``None``/blank -> None."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    if not text:
        return None
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    cut = cut.rstrip(" ,.;:-")
    return f"{cut}…" if cut else None


def _condition_text(cond: DecisionCondition) -> str:
    """One condition's display text -- the owner's own ``note`` when the
    extractor kept one, else the structured metric/op/threshold shape."""
    if cond.note:
        return cond.note
    op = _OP_SYMBOL.get(cond.op, cond.op)
    return f"{cond.metric} {op} {cond.threshold:g} {cond.unit}"


def nearest_live_condition(
    conditions: tuple[DecisionCondition, ...], *, today: str | None = None
) -> DecisionCondition | None:
    """The nearest ARMED/live condition: the first one (extraction order,
    stable) that was NOT already breached at attach time and whose milestone
    window (``not_before``), if any, has opened. Zero extra reads -- only the
    watermark fields ``attach_conditions`` already stamped on the row."""
    ref = today or datetime.now(UTC).date().isoformat()
    for cond in conditions:
        if cond.breached_at_attach:
            continue
        if cond.not_before and cond.not_before > ref:
            continue
        return cond
    return None


def load_you_said(conn: sqlite3.Connection, ticker: str) -> YouSaid | None:
    """The owner's own last decision on ``ticker``. ``None`` when the schema
    predates 0130 (no ``decided_by`` column -- "owner decision" isn't even a
    concept yet) or no owner decision exists for this ticker. Best-effort:
    any read failure degrades to ``None``, never raises."""
    t = ticker.strip().upper()
    if not t:
        return None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    except sqlite3.Error:
        return None
    if "decided_by" not in cols:
        return None
    try:
        row = conn.execute(
            "SELECT recommendation_kind, made_at, rationale_excerpt, conviction, "
            "decision_conditions, outcome_label FROM decisions "
            "WHERE UPPER(ticker) = ? AND decided_by = 'owner' "
            "ORDER BY made_at DESC LIMIT 1",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    conditions = conditions_from_json(row["decision_conditions"])
    live = nearest_live_condition(conditions)
    rationale_full = (
        " ".join(str(row["rationale_excerpt"]).split())
        if isinstance(row["rationale_excerpt"], str) and row["rationale_excerpt"].strip()
        else None
    )
    return YouSaid(
        ticker=t,
        verb=str(row["recommendation_kind"] or "").strip(),
        made_at=str(row["made_at"] or ""),
        excerpt=excerpt_text(row["rationale_excerpt"]),
        rationale_full=rationale_full,
        conviction=(str(row["conviction"]) if row["conviction"] else None),
        condition_text=(_condition_text(live) if live is not None else None),
        outcome_label=(str(row["outcome_label"]) if row["outcome_label"] else None),
    )


def render_you_said_line(you_said: YouSaid) -> str:
    """Pure render of one dense line from an already-loaded :class:`YouSaid`
    -- no DB, unit-testable on its own. Kit classes only (design_language
    Sec4): ``.k-pill`` for conviction/tracking, ``.k-chip`` for the verb; the
    date/excerpt/condition ride as plain escaped text so the line stays ONE
    reading unit rather than a chip pile. Title tooltips carry the overflow
    (full rationale, full condition text) per the D5 density rule."""
    bits: list[str] = [f'<span class="k-chip k-chip-mono">{escape(you_said.verb.upper())}</span>']
    date = fmt_date(you_said.made_at, include_year=False) if you_said.made_at else ""
    if date:
        bits.append(escape(date))
    if you_said.excerpt:
        truncated = you_said.excerpt.endswith("…")
        title = (
            f' title="{escape(you_said.rationale_full, quote=True)}"'
            if truncated and you_said.rationale_full
            else ""
        )
        bits.append(f"<span{title}>“{escape(you_said.excerpt)}”</span>")
    if you_said.conviction:
        bits.append(f'<span class="k-pill">{escape(you_said.conviction)} conviction</span>')
    if you_said.condition_text:
        text = you_said.condition_text
        shown = text if len(text) <= _COND_MAX else text[: _COND_MAX - 1].rstrip() + "…"
        title = f' title="{escape(text, quote=True)}"' if shown != text else ""
        bits.append(f"<span{title}>watching: {escape(shown)}</span>")
    if you_said.outcome_label:
        tone = _OUTCOME_TONE.get(you_said.outcome_label, "")
        bits.append(f'<span class="k-pill{tone}">{escape(you_said.outcome_label)}</span>')
    return '<div class="ys-line">' + " · ".join(bits) + "</div>"


def _capture_chip(ticker: str) -> str:
    """The D4 degraded-state doorway: a chip that opens the shell's capture
    tray, pre-filled with this ticker (the SAME tray Ctrl/Cmd+. opens --
    ``pipeline.command_center_shell``'s ``data-open-capture-tray`` rail,
    delegated once at the document level like every other doorway attribute,
    design_language Sec4.1)."""
    t = escape(ticker, quote=True)
    return (
        '<button type="button" class="k-chip k-chip-btn" data-open-capture-tray '
        f'data-capture-ticker="{t}">Capture a decision</button>'
    )


def render_you_said_strip(conn: sqlite3.Connection, ticker: str) -> str:
    """The "You said" strip: the owner's own last decision on ``ticker``, one
    line, meant to sit above the mini-card / holding-header facts. Degrades to
    the D4 empty primitive -- "No decision on file for T" + a capture-tray
    doorway chip -- when nothing is on file (including a pre-0130 schema)."""
    t = ticker.strip().upper()
    if not t:
        return ""
    you_said = load_you_said(conn, t)
    if you_said is None:
        return k_empty(f"No decision on file for {t}", _capture_chip(t))
    return render_you_said_line(you_said)


def render_you_said_strip_for_path(db_path: Path | str, ticker: str) -> str:
    """``db_path`` convenience wrapper for callers that don't already hold an
    open connection (the Holding tab header). Best-effort: a DB-open failure
    degrades to the same empty-state line an untracked/no-decision ticker
    gets -- never raises."""
    t = ticker.strip().upper()
    if not t:
        return ""
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return k_empty(f"No decision on file for {t}", _capture_chip(t))
    try:
        return render_you_said_strip(conn, t)
    finally:
        conn.close()
