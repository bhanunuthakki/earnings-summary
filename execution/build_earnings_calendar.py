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
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

UPCOMING_WINDOW_DAYS = 90
RECENT_WINDOW_DAYS = 45


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()

    rows = _load_tracked(repo_root)
    today = date.today()

    upcoming: list[dict] = []
    recent: list[dict] = []
    no_data: list[dict] = []

    for ticker, name, list_type in rows:
        events = _read_calendar(repo_root, ticker)
        latest_report = _latest_report(repo_root, ticker)
        base = {
            "ticker": ticker,
            "name": name,
            "list_type": list_type,
            "latest_report": latest_report,
        }
        if not events:
            no_data.append(base)
            continue

        next_event = _next_upcoming(events, today)
        last_event = _most_recent_past(events, today)

        if next_event and (next_event["date"] - today).days <= UPCOMING_WINDOW_DAYS:
            upcoming.append({**base, **next_event})
        elif last_event and (today - last_event["date"]).days <= RECENT_WINDOW_DAYS:
            recent.append({**base, **last_event})
        else:
            no_data.append(base)

    upcoming.sort(key=lambda r: (r["date"], _list_rank(r["list_type"]), r["ticker"]))
    recent.sort(key=lambda r: (r["date"], _list_rank(r["list_type"]), r["ticker"]), reverse=True)
    no_data.sort(key=lambda r: (_list_rank(r["list_type"]), r["ticker"]))

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
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute(
        f"SELECT ticker, name, list_type FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} AND archived_at IS NULL "
        f"ORDER BY ticker"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def _read_calendar(repo_root: Path, ticker: str) -> list[dict]:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_earnings_calendar.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("date") or row.get("reportDate") or row.get("earnings_date")
        if not raw_date:
            continue
        try:
            d = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append(
            {
                "date": d,
                "time": (row.get("time") or row.get("hour") or "").strip().lower(),
                "eps_est": row.get("epsEstimated") or row.get("eps_estimated"),
                "rev_est": row.get("revenueEstimated") or row.get("revenue_estimated"),
            }
        )
    return out


def _next_upcoming(events: list[dict], today: date) -> dict | None:
    future = [e for e in events if e["date"] >= today]
    if not future:
        return None
    return min(future, key=lambda e: e["date"])


def _most_recent_past(events: list[dict], today: date) -> dict | None:
    past = [e for e in events if e["date"] < today]
    if not past:
        return None
    return max(past, key=lambda e: e["date"])


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


def _render_row(row: dict, today: date, *, kind: str) -> str:
    delta = (row["date"] - today).days if kind == "upcoming" else (today - row["date"]).days
    delta_label = f"in {delta}d" if kind == "upcoming" else f"{delta}d ago"
    if kind == "upcoming" and delta == 0:
        delta_label = "today"
    if kind == "upcoming" and delta == 1:
        delta_label = "tomorrow"

    when = _format_time(row.get("time", ""))
    list_class, list_label = _list_class_and_label(row["list_type"])

    if row.get("latest_report"):
        rep_date, rep_rel = row["latest_report"]
        report_cell = f'<a href="{rep_rel}">{rep_date}</a>'
    else:
        report_cell = '<span class="muted">—</span>'

    return (
        f'<tr class="{list_class}">'
        f'<td class="date"><strong>{row["date"].isoformat()}</strong> '
        f'<span class="delta">{delta_label}</span></td>'
        f'<td class="ticker">{row["ticker"]}</td>'
        f'<td class="name">{row["name"]}</td>'
        f'<td><span class="badge {list_class}">{list_label}</span></td>'
        f'<td class="when">{when}</td>'
        f"<td>{report_cell}</td>"
        f"</tr>"
    )


def _render_no_data_row(row: dict) -> str:
    list_class, list_label = _list_class_and_label(row["list_type"])
    if row.get("latest_report"):
        rep_date, rep_rel = row["latest_report"]
        report_cell = f'<a href="{rep_rel}">{rep_date}</a>'
    else:
        report_cell = '<span class="muted">—</span>'
    return (
        f'<tr class="{list_class}">'
        f'<td class="ticker">{row["ticker"]}</td>'
        f'<td class="name">{row["name"]}</td>'
        f'<td><span class="badge {list_class}">{list_label}</span></td>'
        f"<td>{report_cell}</td>"
        f"</tr>"
    )


def _render_html(today: date, upcoming: list[dict], recent: list[dict], no_data: list[dict]) -> str:
    upcoming_rows = (
        "\n".join(_render_row(r, today, kind="upcoming") for r in upcoming)
        or '<tr><td colspan="6" class="muted">No upcoming earnings in the next 90 days.</td></tr>'
    )
    recent_rows = (
        "\n".join(_render_row(r, today, kind="recent") for r in recent)
        or '<tr><td colspan="6" class="muted">No earnings in the last 45 days.</td></tr>'
    )
    no_data_rows = (
        "\n".join(_render_no_data_row(r) for r in no_data)
        or '<tr><td colspan="4" class="muted">All tracked tickers have calendar data.</td></tr>'
    )

    portfolio_upcoming = sum(1 for r in upcoming if r["list_type"] == "portfolio")
    watchlist_upcoming = sum(1 for r in upcoming if r["list_type"] == "watchlist")
    evaluation_upcoming = sum(1 for r in upcoming if r["list_type"] == "evaluation")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Earnings Calendar — {today.isoformat()}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 0; padding: 24px 32px; background: #fafafa; color: #1a1a1a;
    max-width: 1200px;
  }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .meta strong {{ color: #1a1a1a; }}
  h2 {{ font-size: 16px; margin: 28px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #ddd; }}
  table {{ width: 100%; border-collapse: collapse; background: white; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }}
  th {{ background: #f5f5f5; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; color: #555; }}
  tr.portfolio td {{ background: #fffbe6; }}
  tr.portfolio td.ticker {{ font-weight: 700; }}
  tr.evaluation td {{ background: #eff6ff; }}
  tr.evaluation td.ticker {{ font-weight: 600; color: #1e40af; }}
  tr.watchlist td.ticker {{ font-weight: 500; color: #555; }}
  td.date {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  td.date strong {{ display: inline-block; min-width: 92px; }}
  span.delta {{ color: #666; font-size: 12px; margin-left: 4px; }}
  td.when {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #888; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
  .badge.portfolio {{ background: #fde68a; color: #92400e; }}
  .badge.evaluation {{ background: #bfdbfe; color: #1e3a8a; }}
  .badge.watchlist {{ background: #e5e7eb; color: #4b5563; }}
  a {{ color: #0366d6; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .muted {{ color: #999; font-style: italic; }}
  details {{ margin-top: 12px; }}
  details summary {{ cursor: pointer; color: #666; font-size: 13px; }}
</style>
</head>
<body>
<h1>Earnings Calendar</h1>
<p class="meta">Generated <strong>{today.isoformat()}</strong> · Upcoming next 90d: <strong>{len(upcoming)}</strong> ({portfolio_upcoming} portfolio, {evaluation_upcoming} evaluation, {watchlist_upcoming} watchlist) · Recently reported last 45d: <strong>{len(recent)}</strong> · No calendar data: <strong>{len(no_data)}</strong></p>

<h2>Upcoming (next 90 days)</h2>
<table>
  <thead><tr><th>Date</th><th>Ticker</th><th>Company</th><th>List</th><th>When</th><th>Latest report</th></tr></thead>
  <tbody>
{upcoming_rows}
  </tbody>
</table>

<h2>Recently reported (last 45 days)</h2>
<table>
  <thead><tr><th>Date</th><th>Ticker</th><th>Company</th><th>List</th><th>When</th><th>Latest report</th></tr></thead>
  <tbody>
{recent_rows}
  </tbody>
</table>

<details>
<summary>No calendar data ({len(no_data)} tickers)</summary>
<table style="margin-top: 10px;">
  <thead><tr><th>Ticker</th><th>Company</th><th>List</th><th>Latest report</th></tr></thead>
  <tbody>
{no_data_rows}
  </tbody>
</table>
</details>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
