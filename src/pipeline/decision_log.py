"""The owner's decision log: what was decided, why, and how it turned out.

``decisions`` also holds advisor recommendations, broker-fill imports and a
one-time hindsight import. This module separates those records so the audit
page leads with decisions the owner actually made. Every row stays reachable:
nothing here deletes, rewrites, or re-grades a stored decision.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path

from pipeline.portfolio_styles import decisions_css
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.prose import prose_headline, render_prose

_ACTION_LABELS = {
    "initiate": "Start",
    "buy": "Buy",
    "add": "Add",
    "trim": "Trim",
    "sell": "Sell",
    "hold": "Hold",
    "watch": "Watch",
    "pass": "Pass",
    "avoid": "Pass",
}
_OUTCOME_LABELS = {"correct": "Correct", "wrong": "Wrong", "mixed": "Mixed"}
_OUTCOME_TONES = {"correct": "ok", "wrong": "bad", "mixed": "warn"}
_NOT_TESTABLE = "unfalsifiable"
_STUB_RE = re.compile(r"\bSTUB\b|system-drafted stub", re.IGNORECASE)
_PURPOSE_RE = re.compile(r"\bPurpose:\s*(.+)", re.IGNORECASE | re.DOTALL)
_WHY_LIMIT = 150


@dataclass(frozen=True, slots=True)
class DecisionLeg:
    """One ticker-level part of a decision."""

    action: str
    ticker: str


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    """One owner decision, which can span several linked tickers."""

    when: date
    legs: tuple[DecisionLeg, ...]
    headline: str | None
    why: str
    detail: tuple[tuple[str, str], ...]
    outcome: str | None
    outcome_pct: float | None
    date_text: str


@dataclass(frozen=True, slots=True)
class DecisionLog:
    """The decision record split by how each row entered the database."""

    recorded: tuple[DecisionEntry, ...]
    hindsight: tuple[DecisionEntry, ...]
    without_reason: tuple[tuple[str, str, int], ...]
    advisor_count: int


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        cursor = conn.execute(sql)
    except sqlite3.OperationalError:
        return []
    cursor.row_factory = sqlite3.Row
    return cursor.fetchall()


def _day(value: object) -> date:
    return date.fromisoformat(str(value)[:10])


def _action(row: sqlite3.Row) -> str:
    kind = str(row["recommendation_kind"])
    if "covered_call" in str(row["instrument"] or "").lower():
        return "Wrote covered calls on"
    return _ACTION_LABELS.get(kind, kind.replace("_", " ").capitalize())


def _why(text: str) -> str:
    purpose = _PURPOSE_RE.search(text)
    line = prose_headline(purpose.group(1) if purpose else text, limit=_WHY_LIMIT)
    return line[:1].upper() + line[1:]


def _exact_date(value: object) -> str:
    return _day(value).strftime("%b %-d, %Y")


def _hindsight_date(row: sqlite3.Row) -> str:
    """Hindsight rows carry a month at best. Never show a day the record lacks."""
    if _day(row["made_at"]) < _day(row["created_at"]):
        return _day(row["made_at"]).strftime("%b %Y")
    return "Date unknown"


def _entry(rows: list[sqlite3.Row], notes: dict[int, str], *, date_text: str) -> DecisionEntry:
    first = rows[0]
    outcomes = {
        str(row["outcome_label"])
        for row in rows
        if row["outcome_label"] and row["outcome_label"] != "pending"
    }
    outcome = next(iter(outcomes)) if len(outcomes) == 1 else None
    percents = [row["outcome_pct"] for row in rows if row["outcome_label"] == outcome]
    return DecisionEntry(
        when=_day(first["made_at"]),
        legs=tuple(DecisionLeg(_action(row), str(row["ticker"])) for row in rows),
        headline=None,
        why=_why(str(first["rationale_excerpt"])),
        detail=tuple(
            (str(row["ticker"]), notes.get(int(row["id"]), str(row["rationale_excerpt"])))
            for row in rows
        ),
        outcome=outcome,
        outcome_pct=(float(percents[0]) if len(rows) == 1 and percents and percents[0] else None),
        date_text=date_text,
    )


def _recorded_groups(
    rows: list[sqlite3.Row], checkpoints: dict[int, int]
) -> list[list[sqlite3.Row]]:
    """Merge legs of one decision: a shared checkpoint, or one same-day action."""
    groups: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    for row in rows:
        checkpoint = checkpoints.get(int(row["id"]))
        key: tuple[object, ...] = (
            ("checkpoint", checkpoint)
            if checkpoint is not None
            else (
                "action",
                _day(row["made_at"]),
                row["recommendation_kind"],
                row["instrument"],
            )
        )
        groups.setdefault(key, []).append(row)
    return [
        sorted(group, key=lambda row: (str(row["made_at"]), str(row["ticker"])))
        for group in groups.values()
    ]


def _note_entry(row: sqlite3.Row) -> DecisionEntry:
    body = str(row["body"])
    return DecisionEntry(
        when=_day(row["created_at"]),
        legs=(),
        headline=prose_headline(body, limit=_WHY_LIMIT),
        why=prose_headline(body, limit=_WHY_LIMIT, skip_title=True),
        detail=((str(row["ticker"] or "Portfolio"), body),),
        outcome=None,
        outcome_pct=None,
        date_text=_exact_date(row["created_at"]),
    )


def build_decision_log(conn: sqlite3.Connection) -> DecisionLog:
    """Classify stored decisions without changing or re-grading them."""
    decisions = _rows(
        conn,
        "SELECT id, ticker, recommendation_kind, rationale_excerpt, instrument, made_at, "
        "created_at, outcome_label, outcome_pct, decided_by, source_lens "
        "FROM decisions ORDER BY made_at DESC, id DESC",
    )
    open_notes = _rows(
        conn,
        "SELECT id, ticker, body, created_at, decision_id FROM analyst_notes "
        "WHERE kind = 'decision' AND status = 'open' ORDER BY created_at DESC, id DESC",
    )
    checkpoints = {
        int(row["decision_id"]): int(row["checkpoint_id"])
        for row in _rows(
            conn, "SELECT checkpoint_id, decision_id FROM owner_decision_checkpoint_decisions"
        )
    }
    owner = [
        row
        for row in decisions
        if row["ticker"] and (row["decided_by"] == "owner" or row["source_lens"] == "manual_pass")
    ]
    advisor_count = len(decisions) - len(owner)

    without_reason = Counter(
        (str(row["ticker"]), _action(row))
        for row in owner
        if not row["rationale_excerpt"] or _STUB_RE.search(str(row["rationale_excerpt"]))
    )
    explained = [
        row
        for row in owner
        if row["rationale_excerpt"] and not _STUB_RE.search(str(row["rationale_excerpt"]))
    ]
    # One import stamped its whole batch in the same second. A row in that
    # batch is hindsight even when the import had no original date to preserve.
    import_seconds = {
        str(row["created_at"])[:19]
        for row in explained
        if _day(row["made_at"]) < _day(row["created_at"])
    }
    hindsight = [row for row in explained if str(row["created_at"])[:19] in import_seconds]
    recorded = [row for row in explained if str(row["created_at"])[:19] not in import_seconds]

    note_by_decision: dict[int, str] = {}
    orphan_notes: list[sqlite3.Row] = []
    for note in open_notes:
        linked = note["decision_id"]
        same_day = next(
            (
                row
                for row in recorded
                if str(row["ticker"]).upper() == str(note["ticker"] or "").upper()
                and _day(row["made_at"]) == _day(note["created_at"])
            ),
            None,
        )
        if linked is not None:
            note_by_decision[int(linked)] = str(note["body"])
        elif same_day is not None:
            note_by_decision[int(same_day["id"])] = str(note["body"])
        else:
            orphan_notes.append(note)

    recorded_entries = [
        _entry(group, note_by_decision, date_text=_exact_date(group[0]["made_at"]))
        for group in _recorded_groups(recorded, checkpoints)
    ] + [_note_entry(note) for note in orphan_notes]
    recorded_entries.sort(key=lambda entry: entry.when, reverse=True)
    return DecisionLog(
        recorded=tuple(recorded_entries),
        hindsight=tuple(
            sorted(
                (
                    _entry([row], note_by_decision, date_text=_hindsight_date(row))
                    for row in hindsight
                ),
                key=lambda entry: (entry.date_text == "Date unknown", -entry.when.toordinal()),
            )
        ),
        without_reason=tuple(
            (ticker, action, count) for (ticker, action), count in sorted(without_reason.items())
        ),
        advisor_count=advisor_count,
    )


def _what(entry: DecisionEntry) -> str:
    if entry.headline is not None:
        return f"<strong>{escape(entry.headline)}</strong>"
    actions = {leg.action for leg in entry.legs}
    if len(actions) == 1:
        tickers = " · ".join(
            f'<span class="k-tick-sym">{escape(leg.ticker)}</span>' for leg in entry.legs
        )
        return f"<strong>{escape(entry.legs[0].action)}</strong> {tickers}"
    return " · ".join(
        f'<strong>{escape(leg.action)}</strong> <span class="k-tick-sym">{escape(leg.ticker)}</span>'
        for leg in entry.legs
    )


def _outcome(entry: DecisionEntry) -> str:
    if entry.outcome in _OUTCOME_LABELS:
        percent = ""
        if entry.outcome_pct is not None:
            sign = "pos" if entry.outcome_pct >= 0 else "neg"
            percent = f' <span class="k-num-{sign}">{entry.outcome_pct * 100:+.0f}%</span>'
        pill = f"k-pill k-pill-{_OUTCOME_TONES[entry.outcome]}"
        return f'<span class="{pill}">{_OUTCOME_LABELS[entry.outcome]}</span>{percent}'
    text = "Not testable" if entry.outcome == _NOT_TESTABLE else "Too early"
    return f'<span class="muted">{text}</span>'


def _row(entry: DecisionEntry) -> str:
    detail = "".join(
        (f'<h4 class="dl-leg">{escape(label)}</h4>' if len(entry.detail) > 1 else "")
        + f'<div class="dl-body">{render_prose(body)}</div>'
        for label, body in entry.detail
    )
    return (
        '<li class="dl-row"><details>'
        '<summary><span class="dl-line">'
        f'<span class="dl-date">{escape(entry.date_text)}</span>'
        f'<span class="dl-what">{_what(entry)}</span>'
        f'<span class="dl-why">{escape(entry.why)}</span>'
        f'<span class="dl-outcome">{_outcome(entry)}</span>'
        "</span></summary>"
        f'<div class="dl-detail">{detail}</div>'
        "</details></li>"
    )


def _list(entries: tuple[DecisionEntry, ...]) -> str:
    return f'<ol class="dl-list">{"".join(_row(entry) for entry in entries)}</ol>'


def render_decision_log(log: DecisionLog) -> str:
    """Render recorded decisions first and keep every other record one click away."""
    if not (log.recorded or log.hindsight or log.without_reason):
        return f'{decisions_css()}<p class="muted">No owner decisions are recorded yet.</p>'
    parts = [decisions_css()]
    if log.recorded:
        parts.append(_list(log.recorded))
    else:
        parts.append('<p class="muted">No decisions were recorded when they were made.</p>')
    if log.hindsight:
        parts.append(
            '<details class="dl-more"><summary>'
            f"Earlier decisions recorded in hindsight · {len(log.hindsight)}</summary>"
            f"{_list(log.hindsight)}</details>"
        )
    if log.without_reason:
        items = "".join(
            f'<li><span class="k-tick-sym">{escape(ticker)}</span> {escape(action)}'
            f"{f' · {count} rows' if count > 1 else ''}</li>"
            for ticker, action, count in log.without_reason
        )
        total = sum(count for _ticker, _action_label, count in log.without_reason)
        parts.append(
            '<details class="dl-more"><summary>'
            f"Imported rows with no recorded reason · {total}</summary>"
            f'<ul class="dl-imports">{items}</ul></details>'
        )
    if log.advisor_count:
        parts.append(
            f'<p class="muted dl-foot">{log.advisor_count} advisor recommendations stay '
            "stored and do not appear here.</p>"
        )
    return "".join(parts)


def render_decision_log_panel(db_path: Path) -> str:
    """Read the decision record and render the owner's decision log."""
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        return render_decision_log(build_decision_log(conn))
    finally:
        conn.close()


__all__ = [
    "DecisionEntry",
    "DecisionLeg",
    "DecisionLog",
    "build_decision_log",
    "render_decision_log",
    "render_decision_log_panel",
]
