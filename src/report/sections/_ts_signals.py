"""Shared accessor + markdown formatter for the timeseries_signals table.

Section builders (§2 thesis, §4 segments, §5 earnings, §9 bear case) pull
pre-computed signals out of this table (populated by
``src.timeseries.signal_writer``) and inject them into their LLM prompts —
or into the section's rendered output when the section has no LLM call.

The narrative on each signal row is already formatted by the writer (e.g.
``Revenue: decelerating trend (slope -2.3%/q, significant)``) — this
module's job is just to load + group + emit a markdown block. No
re-rendering of value_json.

Public API:
    SignalRow                       — flat view of one timeseries_signals row
    load_signals_for_metrics(...)   — load by metric_name list, grouped
    load_all_signals(...)           — load every signal for the ticker
    load_segment_signals(...)       — load metric_kind='segment' rows only
    format_signals_as_prompt_block(signals, heading) — emit markdown
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from report.sections._common import has_table, open_repo_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalRow:
    """Flat projection of one ``timeseries_signals`` row.

    Only the columns prompt-injection cares about. ``value_json`` is kept
    as the raw string — the renderer's narrative is the authoritative
    English; the JSON is here for callers that want to drill in.
    """

    metric_name: str
    metric_kind: str  # 'financial' | 'kpi' | 'segment'
    signal_type: str  # 'trend' | 'inflection' | 'anomaly' | 'yoy_acceleration' | 'seasonal' | 'correlation'
    value_json: str
    severity: str  # 'green' | 'yellow' | 'red'
    narrative: str | None


def load_signals_for_metrics(
    ticker: str, metric_names: list[str], *, repo_root: Path
) -> dict[str, list[SignalRow]]:
    """Return signals for each requested metric, grouped by metric_name.

    Missing metrics (no rows in the table) yield no key in the dict. The
    typical caller passes ~3-10 metrics; we issue one SQL with an IN
    clause and bucket in Python.

    Returns an empty dict when:
      - the DB is unreachable (fresh checkout)
      - the timeseries_signals table is absent (migration 0053 not run)
      - none of the requested metrics have rows
    """
    if not metric_names:
        return {}

    conn = open_repo_db(repo_root)
    if conn is None:
        return {}
    try:
        if not has_table(conn, "timeseries_signals"):
            return {}
        placeholders = ",".join("?" * len(metric_names))
        rows = conn.execute(
            f"""
            SELECT metric_name, metric_kind, signal_type, value_json,
                   severity, narrative
            FROM timeseries_signals
            WHERE ticker = ? AND metric_name IN ({placeholders})
            ORDER BY metric_name, signal_type
            """,
            (ticker.upper(), *metric_names),
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[SignalRow]] = defaultdict(list)
    for r in rows:
        grouped[str(r["metric_name"])].append(
            SignalRow(
                metric_name=str(r["metric_name"]),
                metric_kind=str(r["metric_kind"]),
                signal_type=str(r["signal_type"]),
                value_json=str(r["value_json"]),
                severity=str(r["severity"]),
                narrative=r["narrative"],
            )
        )
    return dict(grouped)


def load_all_signals(ticker: str, *, repo_root: Path) -> list[SignalRow]:
    """Return every signal for the ticker in (metric_name, signal_type) order.

    Bear case wants the broadest coverage (any disconfirming pattern is
    worth surfacing), so it pulls everything rather than enumerating the
    metric list it cares about.
    """
    conn = open_repo_db(repo_root)
    if conn is None:
        return []
    try:
        if not has_table(conn, "timeseries_signals"):
            return []
        rows = conn.execute(
            """
            SELECT metric_name, metric_kind, signal_type, value_json,
                   severity, narrative
            FROM timeseries_signals
            WHERE ticker = ?
            ORDER BY metric_kind, metric_name, signal_type
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()

    return [
        SignalRow(
            metric_name=str(r["metric_name"]),
            metric_kind=str(r["metric_kind"]),
            signal_type=str(r["signal_type"]),
            value_json=str(r["value_json"]),
            severity=str(r["severity"]),
            narrative=r["narrative"],
        )
        for r in rows
    ]


def load_segment_signals(ticker: str, *, repo_root: Path) -> list[SignalRow]:
    """Return only ``metric_kind='segment'`` rows for the ticker.

    Segment signals are written with ``metric_name='<segment>:<metric>'``
    (e.g. ``Google Cloud:revenue``) so the row is self-describing. The
    segments section wants every signal that pertains to a per-segment
    series, not the consolidated financial baseline.
    """
    conn = open_repo_db(repo_root)
    if conn is None:
        return []
    try:
        if not has_table(conn, "timeseries_signals"):
            return []
        rows = conn.execute(
            """
            SELECT metric_name, metric_kind, signal_type, value_json,
                   severity, narrative
            FROM timeseries_signals
            WHERE ticker = ? AND metric_kind = 'segment'
            ORDER BY metric_name, signal_type
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()

    return [
        SignalRow(
            metric_name=str(r["metric_name"]),
            metric_kind=str(r["metric_kind"]),
            signal_type=str(r["signal_type"]),
            value_json=str(r["value_json"]),
            severity=str(r["severity"]),
            narrative=r["narrative"],
        )
        for r in rows
    ]


def format_signals_as_prompt_block(
    signals: list[SignalRow], heading: str
) -> str:
    """Render signals as a markdown block with the supplied H2 heading.

    Each line is one signal's pre-rendered narrative — the writer already
    formats them as ``{metric}: {description}`` so the block reads as a
    flat bullet list grouped by metric (signals arrive pre-sorted by the
    loader). Severity is appended as a chip when the signal is yellow/red
    so the LLM knows which signals are calibrated as material vs.
    informational.

    Returns the empty string for an empty signals list so callers can
    unconditionally interpolate without producing a dangling heading.
    Signals whose narrative is None are skipped (correlation signals
    carry no English; they render in the UI as a heatmap).
    """
    lines: list[str] = []
    for s in signals:
        if not s.narrative:
            continue
        chip = ""
        if s.severity == "red":
            chip = " [RED]"
        elif s.severity == "yellow":
            chip = " [yellow]"
        lines.append(f"- {s.narrative}{chip}")

    if not lines:
        return ""

    return "## " + heading + "\n" + "\n".join(lines)
