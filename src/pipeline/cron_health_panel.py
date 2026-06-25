"""Cron-health dashboard panel for the command-center shell.

Shows a KPI strip (today's morning-pipeline verdict + consecutive-clean-day
streak) and a last-7-day per-job timeline read from ``ingestion_runs``.  Jobs
are ordered by criticality: the two expected-daily jobs (backup_db,
run_morning_pipeline) appear first, then any other directive seen in the
past week (alphabetical).

Each day in the timeline renders as one coloured dot:

  green  — at least one OK run that day
  red    — ran but the most recent run was FAILED or still IN_PROGRESS
  grey   — no run recorded that day
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

log = logging.getLogger(__name__)

_PANEL_STYLE = """<style>
.ch-dir { font-weight:600; white-space:nowrap; }
.ch-dots { white-space:nowrap; }
.ch-dot { display:inline-block; width:11px; height:11px; border-radius:var(--radius-full);
  margin:0 1px; vertical-align:middle; }
.ch-dot-ok   { background:var(--ok); }
.ch-dot-fail { background:var(--bad); }
.ch-dot-miss { background:var(--border); }
.ch-dot-prog { background:var(--warn); }
.ch-status-ok   { color:var(--ok);   font-weight:600; }
.ch-status-fail { color:var(--bad);  font-weight:600; }
.ch-status-miss { color:var(--muted); }
.ch-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius);
  font-size:var(--fs-body); line-height:1.55; }
.ch-note code { background:var(--surface); padding:1px 5px; border-radius:var(--radius); }
</style>"""

# Directives shown first, in criticality order, with friendly display names.
_PRIORITY_JOBS: list[tuple[str, str]] = [
    ("backup_db", "DB backup"),
    ("run_morning_pipeline", "Morning pipeline"),
]
_PRIORITY_DIRECTIVES: frozenset[str] = frozenset(d for d, _ in _PRIORITY_JOBS)


def _query_runs(db_path: Path, since: datetime) -> dict[tuple[str, str], str]:
    """Return {(directive, "YYYY-MM-DD"): latest_status} for runs since *since*."""
    result: dict[tuple[str, str], str] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT directive, date(started_at) as run_date, status "
                "FROM ingestion_runs "
                "WHERE started_at >= ? "
                "ORDER BY started_at ASC",
                (since,),
            )
            for row in cur.fetchall():
                key: tuple[str, str] = (str(row["directive"]), str(row["run_date"]))
                result[key] = str(row["status"])
        finally:
            conn.close()
    except sqlite3.Error:
        # DB/table not ready (e.g. ingestion_runs absent on a fresh setup) —
        # render whatever rows were gathered rather than failing the panel.
        log.warning({"event": "cron_health_query_failed"}, exc_info=True)
    return result


def _dot(status: str | None) -> str:
    if status is None:
        return '<span class="ch-dot ch-dot-miss" title="no run"></span>'
    cls = {
        "ok": "ch-dot-ok",
        "failed": "ch-dot-fail",
        "in_progress": "ch-dot-prog",
    }.get(status, "ch-dot-miss")
    return f'<span class="ch-dot {cls}" title="{escape(status)}"></span>'


def _day_label(d: date) -> str:
    return d.strftime("%a") + " " + str(d.day)


def _kpi_strip(today_verdict: str, streak: int) -> str:
    verdict_label = {
        "ok": "OK",
        "failed": "FAILED",
        "missing": "Not run yet",
    }.get(today_verdict, "—")
    verdict_tone = {
        "ok": "tone-good",
        "failed": "tone-bad",
    }.get(today_verdict, "")
    streak_tone = "tone-good" if streak >= 3 else ("tone-warn" if streak >= 1 else "")
    return (
        '<div class="kpi-strip">'
        f'<div class="kpi-card {verdict_tone}">'
        '<div class="kpi-label">Today\'s pipeline</div>'
        f'<div class="kpi-value">{verdict_label}</div>'
        '<div class="kpi-sub">run_morning_pipeline</div>'
        "</div>"
        f'<div class="kpi-card {streak_tone}">'
        '<div class="kpi-label">Clean streak</div>'
        f'<div class="kpi-value">{streak}d</div>'
        '<div class="kpi-sub">consecutive OK days</div>'
        "</div>"
        "</div>"
    )


def _timeline_table(
    all_runs: dict[tuple[str, str], str],
    dates: list[date],
) -> str:
    seen: set[str] = {directive for directive, _ in all_runs}
    extra = sorted(seen - _PRIORITY_DIRECTIVES)
    display: list[tuple[str, str]] = [
        *((d, n) for d, n in _PRIORITY_JOBS if d in seen or d in _PRIORITY_DIRECTIVES),
        *((d, d) for d in extra),
    ]

    date_strs = [d.isoformat() for d in dates]
    header = "".join(
        f'<th class="num" title="{ds}">{_day_label(d)}</th>'
        for d, ds in zip(dates, date_strs, strict=True)
    )
    rows = ""
    for directive, label in display:
        dots = "".join(_dot(all_runs.get((directive, ds))) for ds in date_strs)
        latest: str | None = next(
            (
                all_runs[(directive, ds)]
                for ds in reversed(date_strs)
                if (directive, ds) in all_runs
            ),
            None,
        )
        status_cls = {
            "ok": "ch-status-ok",
            "failed": "ch-status-fail",
        }.get(latest or "", "ch-status-miss")
        status_txt = latest or "—"
        rows += (
            "<tr>"
            f'<td class="ch-dir">{escape(label)}</td>'
            f'<td class="ch-dots">{dots}</td>'
            f'<td class="{status_cls}">{escape(status_txt)}</td>'
            "</tr>"
        )

    return (
        '<table class="p-table"><thead><tr>'
        "<th>Job</th>"
        f"{header}"
        "<th>Last status</th>"
        "</tr></thead><tbody>"
        f"{rows}"
        "</tbody></table>"
    )


def render_cron_health_live_body(db_path: Path) -> str:
    """The time-varying part of the panel (KPI strip + 7-day timeline) — the
    fragment the HTMX poller re-fetches via ``GET /api/cron-health`` so today's
    pipeline verdict flips from "Not run yet" to OK/FAILED in place, without a
    manual reload. Returned alone so the wrapper's chrome + CLI note never
    re-render on a poll."""
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    since = datetime.combine(dates[0], datetime.min.time())

    all_runs = _query_runs(db_path, since)
    if not all_runs:
        return (
            '<p class="muted">No pipeline run rows yet. '
            "The morning pipeline writes to <code>ingestion_runs</code> after each "
            "run; this panel fills as the daily jobs execute.</p>"
        )

    today_str = today.isoformat()
    today_status = all_runs.get(("run_morning_pipeline", today_str))
    today_verdict = today_status or "missing"

    # Streak: consecutive days ending yesterday where run_morning_pipeline=ok.
    streak = 0
    for i in range(1, 8):
        ds = (today - timedelta(days=i)).isoformat()
        if all_runs.get(("run_morning_pipeline", ds)) == "ok":
            streak += 1
        else:
            break

    return _kpi_strip(today_verdict, streak) + _timeline_table(all_runs, dates)


def render_cron_health_panel(db_path: Path) -> str:
    """The Cron Health tab fragment: a KPI strip + 7-day per-job timeline.

    The live body self-refreshes every 60s via HTMX (the Wave 3 live-tile
    pattern the Overview cockpit uses): the ``#cc-cron-live`` wrapper re-fetches
    ``GET /api/cron-health`` so today's verdict updates in place while the
    morning pipeline runs. Degrades cleanly with JS off — the body is
    server-rendered, the poll is pure enhancement."""
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Cron health</h2>',
            '<p class="sub">Last 7 days of pipeline run history from '
            "<code>ingestion_runs</code>. "
            "Green = OK · Red = failed · Grey = no run recorded. "
            '<span class="muted">Auto-refreshes every 60s.</span></p>',
            '<div id="cc-cron-live" hx-get="/api/cron-health" '
            'hx-trigger="every 60s" hx-swap="innerHTML">',
            render_cron_health_live_body(db_path),
            "</div>",
            '<div class="ch-note">Run '
            "<code>python execution/verify_cron_registration.py</code> to audit "
            "the Windows Task Scheduler registration, or "
            "<code>python execution/verify_daily_chain.py</code> to check "
            "whether today's morning pipeline completed successfully.</div>",
            "</section>",
        ]
    )
