"""Cron-health dashboard panel for the command-center shell.

Leads with two failure modes that a green timeline cannot express, then shows
a KPI strip (today's morning-pipeline verdict + consecutive-clean-day streak)
and a last-7-day per-job timeline read from ``ingestion_runs``.  Jobs are
ordered by criticality: the two expected-daily jobs (backup_db,
run_morning_pipeline) appear first, then any other directive seen in the past
week (alphabetical).

Each day in the timeline renders as one coloured dot:

  green  — at least one OK run that day
  red    — ran but the most recent run was FAILED or still IN_PROGRESS
  grey   — no run recorded that day

The two banners above it exist because that timeline reports whether a job
RAN, not whether it did its work.  On 2026-08-02 the database sat one Alembic
revision behind ``main`` for hours: every guarded writer refused, the LLM cost
ledger swallowed the refusal by design, and this panel stayed green while
seven cost rows were lost.  Schema drift and dropped ledger writes are
therefore read directly — the first live, the second from the durable counter
``telemetry_health`` keeps beside the database.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from html import escape
from pathlib import Path

from pipeline.operational_health import (
    archive_backup_covers_local_sidecar,
    latest_backup,
    latest_eval,
    wal_size,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

_PANEL_STYLE = """<style>
.ch-dir { font-weight:600; white-space:nowrap; }
.ch-dots { white-space:nowrap; }
.ch-dot { --k-dot-size:11px; margin:0 1px; }
/* the unique faint "no run" shade — the ok/fail/prog tones ride the kit .k-dot */
.ch-dot-miss { color:var(--border); }
.ch-status-ok   { color:var(--ok);   font-weight:600; }
.ch-status-fail { color:var(--bad);  font-weight:600; }
.ch-status-miss { color:var(--muted); }
.ch-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius);
  font-size:var(--fs-body); line-height:1.55; }
.ch-note code { background:var(--surface); padding:1px 5px; border-radius:var(--radius); }
.ch-alarm { margin-bottom:var(--sp-3); line-height:1.55; }
.ch-alarm strong { display:block; margin-bottom:var(--sp-1); }
.ch-alarm code { background:var(--surface); padding:1px 5px; border-radius:var(--radius); }
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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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


def _schema_drift_banner(db_path: Path) -> str:
    """The loud one: this checkout and this database disagree, right now.

    Rendered live rather than from a job record, so it appears the moment the
    revisions diverge and clears itself on the next 60s poll once the upgrade
    lands — no job has to run and fail first for the operator to see it.
    """
    try:
        from runtime.job_runtime import SCHEMA_DRIFT_EXIT_CODE
        from schema_compat import describe_drift

        drift = describe_drift(db_path)
    except Exception:  # a health panel must never be the thing that breaks
        log.warning({"event": "cron_health_drift_probe_failed"}, exc_info=True)
        return ""
    if drift is None:
        return ""
    observed = ",".join(drift.db_revisions) or "(none)"
    return (
        '<div class="k-well k-well-bad ch-alarm">'
        "<strong>Schema drift — scheduled jobs are blocked</strong>"
        f"{escape(drift.detail)}. "
        f"Database is at <code>{escape(observed)}</code>, "
        f"this checkout expects <code>{escape(drift.expected_revision or 'a single head')}</code>. "
        "Every job outside the backup/restore/audit set now exits "
        f"<code>{SCHEMA_DRIFT_EXIT_CODE}</code> instead of running. "
        f"Fix: <code>{escape(drift.fix_command)}</code>."
        "</div>"
    )


def _dropped_ledger_banner(db_path: Path, since: datetime) -> str:
    """Cost rows that were written best-effort and lost anyway.

    ``llm_call_ledger`` cannot raise — the LLM call it describes has already
    been paid for — so this counter is the only place a lost row is countable.
    """
    try:
        from telemetry_health import DROPPED_LLM_LEDGER_WRITE, dropped_writes_since

        dropped = dropped_writes_since(DROPPED_LLM_LEDGER_WRITE, db_path=db_path, since=since)
    except Exception:
        log.warning({"event": "cron_health_dropped_writes_read_failed"}, exc_info=True)
        return ""
    if dropped is None:
        return ""
    plural = "s" if dropped.count != 1 else ""
    return (
        '<div class="k-well k-well-warn ch-alarm">'
        f"<strong>{dropped.count} LLM cost row{plural} lost in the last 7 days</strong>"
        "These are spend records the ledger could not persist. They are gone — "
        "the ledger is best-effort by design and does not retry. "
        f"Most recent {escape(dropped.last_at.strftime('%Y-%m-%d %H:%M UTC'))}: "
        f"<code>{escape(dropped.last_error[:180])}</code>"
        "</div>"
    )


def operational_alarms(db_path: Path, *, now: datetime | None = None) -> str:
    """Backup, WAL, and eval tripwires; metadata-only except one indexed eval read."""
    observed_now = now or datetime.now(UTC)
    alarms: list[str] = []
    try:
        backup = latest_backup()
        if backup.is_stale(now=observed_now):
            detail = (
                "no encrypted snapshot was found"
                if backup.observed_at is None
                else f"newest snapshot is {observed_now - backup.observed_at} old"
            )
            alarms.append(
                '<div class="k-well k-well-bad ch-alarm">'
                "<strong>Database backup is stale</strong>"
                f"{escape(detail)}; the required maximum age is 48 hours."
                "</div>"
            )
        repo_root = db_path.parent.parent
        if not archive_backup_covers_local_sidecar(repo_root):
            alarms.append(
                '<div class="k-well k-well-bad ch-alarm">'
                "<strong>GC archive is not backed up</strong>"
                "The latest encrypted archive snapshot predates the local reversible-prune store."
                "</div>"
            )
    except (OSError, ValueError) as exc:
        log.warning({"event": "cron_health_backup_probe_failed", "error": str(exc)})

    try:
        wal_bytes, wal_threshold = wal_size(db_path)
        if wal_bytes >= wal_threshold:
            alarms.append(
                '<div class="k-well k-well-warn ch-alarm">'
                "<strong>SQLite WAL is oversized</strong>"
                f"Current WAL size is {wal_bytes / (1024 * 1024):.1f} MiB; "
                f"the alert threshold is {wal_threshold / (1024 * 1024):.1f} MiB. "
                "Inspect long-lived readers; this probe never checkpoints the live database."
                "</div>"
            )
    except (OSError, ValueError) as exc:
        log.warning({"event": "cron_health_wal_probe_failed", "error": str(exc)})

    try:
        evaluation = latest_eval(db_path)
        if evaluation.is_stale(now=observed_now):
            detail = (
                "no eval receipt was found"
                if evaluation.observed_at is None
                else f"newest eval receipt is {observed_now - evaluation.observed_at} old"
            )
            alarms.append(
                '<div class="k-well k-well-warn ch-alarm">'
                "<strong>LLM evaluations are stale</strong>"
                f"{escape(detail)}; weekly cohorts must finish at least every 8 days."
                "</div>"
            )
    except (sqlite3.Error, ValueError) as exc:
        log.warning({"event": "cron_health_eval_probe_failed", "error": str(exc)})
        alarms.append(
            '<div class="k-well k-well-warn ch-alarm">'
            "<strong>LLM evaluation freshness is unavailable</strong>"
            "The eval ledger could not be read; this is not treated as fresh."
            "</div>"
        )
    return "".join(alarms)


def _dot(status: str | None) -> str:
    if status is None:
        return '<span class="ch-dot k-dot ch-dot-miss" title="no run"></span>'
    cls = {
        "ok": "k-dot-ok",
        "failed": "k-dot-bad",
        "abandoned": "k-dot-bad",
        "in_progress": "k-dot-warn",
    }.get(status, "ch-dot-miss")
    return f'<span class="ch-dot k-dot {cls}" title="{escape(status)}"></span>'


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
            "abandoned": "ch-status-fail",
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

    # Both alarms lead the body, and both survive the no-rows early return: a
    # drifted database with no run history is the WORST case, not a reason to
    # say nothing.
    alarms = (
        _schema_drift_banner(db_path)
        + _dropped_ledger_banner(db_path, since.replace(tzinfo=UTC))
        + operational_alarms(db_path)
    )

    all_runs = _query_runs(db_path, since)
    if not all_runs:
        return alarms + (
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

    return alarms + _kpi_strip(today_verdict, streak) + _timeline_table(all_runs, dates)


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
