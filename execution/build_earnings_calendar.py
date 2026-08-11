"""Build a simple earnings calendar HTML for the active universe.

Reads earnings dates from data/historical/fmp/{TICKER}_earnings_calendar.json
for every row in tracked_companies (portfolio + watchlist + evaluation,
non-archived) and emits output/earnings_calendar.html — a single
self-contained page showing:

  - Upcoming earnings (next 90 days), sorted by date, portfolio rows pinned first
  - Recently reported (last 45 days), for context
  - Tickers without calendar data

Each row links to the latest output/research/{TICKER}/{DATE}_workspace.html if
one exists. Re-run any time; the file is overwritten in place.

Usage:
    python execution/build_earnings_calendar.py
    python execution/build_earnings_calendar.py --repo-root /abs/path
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from calendar_clock import calendar_today  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from ui.controls import controls_css, icon_svg, ticker_label  # noqa: E402
from ui.tokens import FAVICON_LINK, palette_css  # noqa: E402

UPCOMING_WINDOW_DAYS = 90
RECENT_WINDOW_DAYS = 45


@dataclass(frozen=True)
class CalendarEvent:
    date: date
    time: str
    eps_est: object | None
    rev_est: object | None


@dataclass(frozen=True)
class CalendarRow:
    ticker: str
    name: str
    list_type: str
    latest_report: tuple[str, str] | None
    event: CalendarEvent | None = None


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()

    rows = _load_tracked(repo_root)
    today = calendar_today()

    upcoming: list[CalendarRow] = []
    recent: list[CalendarRow] = []
    no_data: list[CalendarRow] = []

    for ticker, name, list_type in rows:
        events = read_calendar_events(repo_root, ticker)
        latest_report = _latest_report(repo_root, ticker)
        base = CalendarRow(ticker, name, list_type, latest_report)
        if not events:
            no_data.append(base)
            continue

        next_event = _next_upcoming(events, today)
        last_event = _most_recent_past(events, today)

        if next_event and (next_event.date - today).days <= UPCOMING_WINDOW_DAYS:
            upcoming.append(CalendarRow(ticker, name, list_type, latest_report, next_event))
        elif last_event and (today - last_event.date).days <= RECENT_WINDOW_DAYS:
            recent.append(CalendarRow(ticker, name, list_type, latest_report, last_event))
        else:
            no_data.append(base)

    upcoming.sort(key=lambda r: (_event_date(r), _list_rank(r.list_type), r.ticker))
    recent.sort(key=lambda r: (_event_date(r), _list_rank(r.list_type), r.ticker), reverse=True)
    no_data.sort(key=lambda r: (_list_rank(r.list_type), r.ticker))

    html = _render_html(today, upcoming, recent, no_data)
    out_path = repo_root / "output" / "earnings_calendar.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(
        json.dumps(
            {
                "path": str(out_path),
                "upcoming": len(upcoming),
                "recent": len(recent),
                "no_data": len(no_data),
            },
            indent=2,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/ and output/. Default: this repo.",
    )
    return parser.parse_args()


def _load_tracked(repo_root: Path) -> list[tuple[str, str, str]]:
    db_path = repo_root / "data" / "portfolio.db"
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    c = conn.cursor()
    c.execute(
        f"SELECT ticker, name, list_type FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} AND archived_at IS NULL "
        f"ORDER BY ticker"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def read_calendar_events(repo_root: Path, ticker: str) -> list[CalendarEvent]:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_earnings_calendar.json"
    if not path.exists():
        return []
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(payload, dict):
        mapping = cast("dict[str, object]", payload)
        payload = mapping.get("data", mapping)
    if not isinstance(payload, list):
        return []
    out: list[CalendarEvent] = []
    for raw_row in cast("list[object]", payload):
        if not isinstance(raw_row, dict):
            continue
        row = cast("dict[str, object]", raw_row)
        # A cache file is named for the tracked ticker, but the payload is an
        # external boundary. If the provider supplies identity, require it to
        # agree so a crossed/stale cache cannot put another issuer's event on
        # this company's calendar. Older cache shapes without identity remain
        # supported.
        raw_ticker = row.get("symbol") or row.get("ticker")
        if isinstance(raw_ticker, str) and raw_ticker.strip().upper() != ticker.upper():
            continue
        raw_date = row.get("date") or row.get("reportDate") or row.get("earnings_date")
        if not isinstance(raw_date, str) or not raw_date:
            continue
        try:
            d = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        raw_time = row.get("time") or row.get("hour") or ""
        out.append(
            CalendarEvent(
                date=d,
                time=raw_time.strip().lower() if isinstance(raw_time, str) else "",
                eps_est=row.get("epsEstimated") or row.get("eps_estimated"),
                rev_est=row.get("revenueEstimated") or row.get("revenue_estimated"),
            )
        )
    return out


def _next_upcoming(events: list[CalendarEvent], today: date) -> CalendarEvent | None:
    future = [event for event in events if event.date >= today]
    if not future:
        return None
    return min(future, key=lambda event: event.date)


def _most_recent_past(events: list[CalendarEvent], today: date) -> CalendarEvent | None:
    past = [event for event in events if event.date < today]
    if not past:
        return None
    return max(past, key=lambda event: event.date)


def _latest_report(repo_root: Path, ticker: str) -> tuple[str, str] | None:
    """Return (date_iso, relative_path_from_output_dir) for the latest report.

    Returns None if no report exists. The relative path is relative to
    output/ so the calendar HTML can link to
    research/{TICKER}/{DATE}_workspace.html.
    """
    research_dir = repo_root / "output" / "research" / ticker
    if not research_dir.exists():
        return None
    reports: list[tuple[date, Path]] = []
    for p in research_dir.iterdir():
        if not p.is_file() or not p.name.endswith("_workspace.html"):
            continue
        stem = p.name.replace("_workspace.html", "")
        try:
            d = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        reports.append((d, p))
    if not reports:
        return None
    d, p = max(reports, key=lambda x: x[0])
    rel = f"research/{ticker}/{p.name}"
    return d.isoformat(), rel


_LIST_CLASS_LABELS: dict[str, tuple[str, str]] = {
    "portfolio": ("portfolio", "Portfolio"),
    "watchlist": ("watchlist", "Watchlist"),
    "evaluation": ("evaluation", "Evaluation"),
}


def _list_class_and_label(list_type: str) -> tuple[str, str]:
    """CSS class + human label for a row. Unknown list_types fall back to watchlist styling."""
    return _LIST_CLASS_LABELS.get(list_type, ("watchlist", "Watchlist"))


def _list_rank(list_type: str) -> int:
    """Sort key: portfolio first, then evaluation, then watchlist."""
    return {"portfolio": 0, "evaluation": 1, "watchlist": 2}.get(list_type, 3)


def _format_time(time_str: str) -> str:
    t = time_str.lower()
    if "bmo" in t or "before" in t or "pre" in t:
        return "BMO"
    if "amc" in t or "after" in t or "post" in t:
        return "AMC"
    return ""


def _event_date(row: CalendarRow) -> date:
    if row.event is None:
        raise ValueError(f"calendar row for {row.ticker} has no event")
    return row.event.date


def render_calendar_row(row: CalendarRow, today: date, *, kind: str) -> str:
    event = row.event
    if event is None:
        raise ValueError(f"calendar row for {row.ticker} has no event")
    delta = (event.date - today).days if kind == "upcoming" else (today - event.date).days
    delta_label = f"in {delta}d" if kind == "upcoming" else f"{delta}d ago"
    if kind == "upcoming" and delta == 0:
        delta_label = "today"
    if kind == "upcoming" and delta == 1:
        delta_label = "tomorrow"

    when = _format_time(event.time)
    list_class, list_label = _list_class_and_label(row.list_type)

    if row.latest_report:
        rep_date, rep_rel = row.latest_report
        report_cell = f'<a href="{escape(rep_rel, quote=True)}">{escape(rep_date)}</a>'
    else:
        report_cell = (
            '<span class="muted" aria-label="No report available">No report available</span>'
        )

    company = ticker_label(row.ticker, row.name)

    return (
        f'<tr class="calendar-row {escape(list_class)}">'
        f'<td class="date"><strong>{event.date.isoformat()}</strong> '
        f'<span class="delta">{delta_label}</span></td>'
        f'<td class="company">{company}</td>'
        f'<td><span class="k-chip k-chip-mono calendar-kind {escape(list_class)}">'
        f"{escape(list_label)}</span></td>"
        f'<td class="when">{escape(when)}</td>'
        f"<td>{report_cell}</td>"
        f"</tr>"
    )


def _render_no_data_row(row: CalendarRow) -> str:
    list_class, list_label = _list_class_and_label(row.list_type)
    if row.latest_report:
        rep_date, rep_rel = row.latest_report
        report_cell = f'<a href="{escape(rep_rel, quote=True)}">{escape(rep_date)}</a>'
    else:
        report_cell = (
            '<span class="muted" aria-label="No report available">No report available</span>'
        )
    company = ticker_label(row.ticker, row.name)
    return (
        f'<tr class="calendar-row {escape(list_class)}">'
        f'<td class="company">{company}</td>'
        f'<td><span class="k-chip k-chip-mono calendar-kind {escape(list_class)}">'
        f"{escape(list_label)}</span></td>"
        f"<td>{report_cell}</td>"
        f"</tr>"
    )


def _render_html(
    today: date,
    upcoming: list[CalendarRow],
    recent: list[CalendarRow],
    no_data: list[CalendarRow],
) -> str:
    upcoming_rows = (
        "\n".join(render_calendar_row(r, today, kind="upcoming") for r in upcoming)
        or '<tr><td colspan="5" class="muted">No upcoming earnings in the next 90 days.</td></tr>'
    )
    recent_rows = (
        "\n".join(render_calendar_row(r, today, kind="recent") for r in recent)
        or '<tr><td colspan="5" class="muted">No earnings in the last 45 days.</td></tr>'
    )
    no_data_rows = (
        "\n".join(_render_no_data_row(r) for r in no_data)
        or '<tr><td colspan="3" class="muted">All tracked tickers have calendar data.</td></tr>'
    )

    portfolio_upcoming = sum(1 for row in upcoming if row.list_type == "portfolio")
    watchlist_upcoming = sum(1 for row in upcoming if row.list_type == "watchlist")
    evaluation_upcoming = sum(1 for row in upcoming if row.list_type == "evaluation")

    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Earnings Calendar — {today.isoformat()}</title>
{FAVICON_LINK}
<style>
{palette_css("dark")}
{controls_css("dark")}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font-family: var(--sans); }}
.calendar-shell {{ min-height: 100dvh; display: grid;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr); }}
.calendar-sidebar {{ position: sticky; top: 0; height: 100dvh; padding: var(--sp-4) var(--sp-3);
  display: flex; flex-direction: column; gap: var(--sp-4); }}
.calendar-brand {{ min-height: var(--header-height); display: flex; align-items: center;
  padding: 0 var(--sp-2); font-size: var(--fs-title); font-weight: 600; }}
.calendar-layer {{ display: flex; flex-direction: column; gap: var(--sp-half); }}
.calendar-layer-title {{ padding: 0 var(--sp-2); color: var(--muted);
  font-size: var(--fs-caption); font-weight: 600; text-transform: uppercase; }}
.calendar-content {{ min-width: 0; max-width: var(--main-max-width); width: 100%;
  margin: 0 auto; padding: var(--sp-5) var(--sp-4); }}
.calendar-head {{ margin-bottom: var(--sp-5); }}
.calendar-page-title {{ margin: 0 0 var(--sp-1); font-size: var(--fs-display); }}
.calendar-meta strong {{ color: var(--fg); }}
.calendar-section {{ margin-top: var(--sp-4); }}
.calendar-row.portfolio td {{ background: color-mix(in srgb, var(--accent) 5%, var(--surface)); }}
.calendar-row.evaluation td {{ background: color-mix(in srgb, var(--mark) 5%, var(--surface)); }}
.calendar-kind.portfolio {{ color: var(--accent); border-color: var(--accent); }}
.calendar-kind.evaluation {{ color: var(--mark); border-color: var(--mark); }}
td.date {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
td.date strong {{ display: inline-block; min-width: var(--ticker-width); font-family: var(--mono); }}
.delta {{ color: var(--muted); font-size: var(--fs-caption); margin-left: var(--sp-1); }}
td.when {{ font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.muted {{ color: var(--muted); font-style: italic; }}
details summary {{ cursor: pointer; }}
details .calendar-table {{ margin-top: var(--sp-2); }}
.calendar-table-wrap {{ width: 100%; max-width: 100%; overflow-x: auto; }}
@media (max-width: 900px) {{
  .calendar-shell {{ grid-template-columns: var(--sidebar-collapsed-width) minmax(0, 1fr); }}
  .calendar-sidebar {{ width: var(--sidebar-collapsed-width); padding: var(--sp-3) var(--sp-2); }}
  .calendar-brand, .calendar-layer-title, .calendar-sidebar .k-nav-item span {{ display: none; }}
  .calendar-sidebar .k-nav-item {{ width: var(--touch-target-size);
    min-height: var(--touch-target-size); padding: 0;
    justify-content: center; align-self: center; }}
}}
</style>
</head>
<body>
<div class="calendar-shell">
<aside class="calendar-sidebar k-sidebar">
  <div class="calendar-brand">Earnings OS</div>
  <section class="calendar-layer">
    <div class="calendar-layer-title">L1 · Portfolio Intelligence</div>
    <a class="k-btn k-nav-item active" href="earnings_calendar.html"
      aria-current="page" aria-label="Earnings calendar">
      {icon_svg("cockpit")}<span>Earnings calendar</span></a>
    <a class="k-btn k-nav-item" href="http://127.0.0.1:7421/" aria-label="Open command center">
      {icon_svg("portfolio")}<span>Command center</span></a>
  </section>
  <section class="calendar-layer">
    <div class="calendar-layer-title">L2 · Research Engine</div>
    <a class="k-btn k-nav-item" href="research/" aria-label="Research briefs">
      {icon_svg("company")}<span>Research briefs</span></a>
  </section>
</aside>
<main class="calendar-content">
<header class="calendar-head">
  <h1 class="calendar-page-title">Earnings Calendar</h1>
  <p class="calendar-meta k-card-meta">Generated <strong>{today.isoformat()}</strong> · Upcoming next 90d: <strong>{len(upcoming)}</strong> ({portfolio_upcoming} portfolio, {evaluation_upcoming} evaluation, {watchlist_upcoming} watchlist) · Recently reported last 45d: <strong>{len(recent)}</strong> · No calendar data: <strong>{len(no_data)}</strong></p>
</header>

<section class="calendar-section k-card k-card-stack" aria-labelledby="upcomingEarningsTitle">
<h2 class="k-card-title" id="upcomingEarningsTitle">Upcoming (next 90 days)</h2>
<div class="calendar-table-wrap" role="region" aria-label="Upcoming earnings" tabindex="0">
<table class="p-table calendar-table">
  <thead><tr><th>Date</th><th>Company</th><th>List</th><th>When</th><th>Latest report</th></tr></thead>
  <tbody>
{upcoming_rows}
  </tbody>
</table>
</div>
</section>

<section class="calendar-section k-card k-card-stack" aria-labelledby="recentEarningsTitle">
<h2 class="k-card-title" id="recentEarningsTitle">Recently reported (last 45 days)</h2>
<div class="calendar-table-wrap" role="region" aria-label="Recently reported earnings" tabindex="0">
<table class="p-table calendar-table">
  <thead><tr><th>Date</th><th>Company</th><th>List</th><th>When</th><th>Latest report</th></tr></thead>
  <tbody>
{recent_rows}
  </tbody>
</table>
</div>
</section>

<details class="calendar-section k-card">
<summary class="k-card-row-title">No calendar data ({len(no_data)} tickers)</summary>
<div class="calendar-table-wrap" role="region" aria-label="Tickers without calendar data" tabindex="0">
<table class="p-table calendar-table">
  <thead><tr><th>Company</th><th>List</th><th>Latest report</th></tr></thead>
  <tbody>
{no_data_rows}
  </tbody>
</table>
</div>
</details>
</main>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
