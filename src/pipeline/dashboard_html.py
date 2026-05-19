"""Server-side HTML renderer for the dashboard.

Pure function: takes the rows dict produced by `dashboard_status.build_dashboard_rows`
and returns a full HTML document. No JS yet — PR 1 is read-only; action UI lands
in later PRs. Style is minimal and self-contained (no external CSS / fonts) so
the page renders identically whether the user has the workspace report open
elsewhere or not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from pipeline.dashboard_status import DashboardRow

_BREACH_BADGE_COLOR: dict[str, str] = {
    "intact": "#3a8a3a",
    "watch": "#b88a1f",
    "broken": "#b04040",
    "pending": "#7a7a7a",
}


def render_dashboard_html(
    rows_by_list: dict[str, list[DashboardRow]],
    *,
    generated_at: datetime | None = None,
) -> str:
    """Render the two-table dashboard page.

    `rows_by_list` is the return value of `build_dashboard_rows`. `generated_at`
    is an optional fixed timestamp (tests pass one for deterministic output).
    """
    portfolio_rows = rows_by_list.get("portfolio", [])
    evaluation_rows = rows_by_list.get("evaluation", [])
    stamp = (generated_at or datetime.now(UTC)).isoformat(timespec="seconds")

    return _PAGE_TEMPLATE.format(
        portfolio_section=_render_section(
            "Portfolio", portfolio_rows, empty_msg="No portfolio tickers."
        ),
        evaluation_section=_render_section(
            "Evaluation", evaluation_rows, empty_msg="No evaluation tickers."
        ),
        generated_at=escape(stamp),
    )


def _render_section(title: str, rows: list[DashboardRow], *, empty_msg: str) -> str:
    body = _render_table(rows) if rows else f"<p class='empty'>{escape(empty_msg)}</p>"
    return f"""
<section class="list-section">
  <h2>{escape(title)} <span class="count">({len(rows)})</span></h2>
  {body}
</section>
""".strip()


def _render_table(rows: list[DashboardRow]) -> str:
    head = (
        "<thead><tr>"
        "<th>Ticker</th>"
        "<th>Last FMP</th>"
        "<th>Last transcript</th>"
        "<th>Last build</th>"
        "<th>Comments</th>"
        "<th>Breach</th>"
        "<th>Open</th>"
        "</tr></thead>"
    )
    body_rows = "\n".join(_render_row(r) for r in rows)
    return f"<table>{head}<tbody>{body_rows}</tbody></table>"


def _render_row(row: DashboardRow) -> str:
    transcript_cell = _format_transcript(row.last_transcript)
    build_cell = _format_relative_time(row.last_build_at)
    comments_cell = _format_comments_count(row.open_comments_count)
    breach_cell = _format_breach(row.breach_status)
    fmp_cell = _format_relative_time(row.fmp_last_pulled)
    open_cell = (
        f"<a class='open-link' href='/reports/{escape(row.ticker)}'>Open↗</a>"
        if row.last_build_at
        else "<span class='muted'>—</span>"
    )
    return (
        "<tr>"
        f"<td class='ticker'>{escape(row.ticker)}</td>"
        f"<td>{fmp_cell}</td>"
        f"<td>{transcript_cell}</td>"
        f"<td>{build_cell}</td>"
        f"<td>{comments_cell}</td>"
        f"<td>{breach_cell}</td>"
        f"<td>{open_cell}</td>"
        "</tr>"
    )


def _format_transcript(t) -> str:
    if t is None or t.period_end is None:
        return "<span class='muted'>—</span>"
    qa_marker = ""
    if t.has_qa_section is True:
        qa_marker = " <span class='qa-yes' title='Has Q&amp;A section'>Q&amp;A</span>"
    elif t.has_qa_section is False:
        qa_marker = " <span class='qa-no' title='Prepared remarks only'>no Q&amp;A</span>"
    return f"{escape(t.period_end)}{qa_marker}"


def _format_relative_time(iso: str | None) -> str:
    if iso is None:
        return "<span class='muted'>—</span>"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return escape(iso)
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return escape(iso[:10])
    if seconds < 3600:
        label = f"{seconds // 60}m ago"
    elif seconds < 86400:
        label = f"{seconds // 3600}h ago"
    elif seconds < 86400 * 30:
        label = f"{seconds // 86400}d ago"
    else:
        label = f"{seconds // (86400 * 30)}mo ago"
    return f"<span title='{escape(iso)}'>{escape(label)}</span>"


def _format_comments_count(n: int) -> str:
    if n == 0:
        return "<span class='muted'>—</span>"
    plural = "" if n == 1 else "s"
    return f"<span class='comments-open'>{n} open comment{plural}</span>"


def _format_breach(status: str | None) -> str:
    if status is None:
        return "<span class='muted'>—</span>"
    color = _BREACH_BADGE_COLOR.get(status, "#7a7a7a")
    return (
        f"<span class='breach-badge' "
        f"style='background:{escape(color)}'>{escape(status)}</span>"
    )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Earnings Summary — Dashboard</title>
<style>
:root {{
  --fg: #1a1a1a;
  --fg-muted: #888;
  --border: #ddd;
  --row-hover: #f5f5f5;
  --bg: #fafafa;
  --bg-card: #fff;
  --link: #0066cc;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
  padding: 24px 32px 64px;
  max-width: 1200px;
  margin-left: auto;
  margin-right: auto;
}}
header {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 28px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}}
h1 {{ font-size: 20px; font-weight: 600; margin: 0; }}
h2 {{ font-size: 15px; font-weight: 600; margin: 28px 0 10px; text-transform: uppercase; letter-spacing: 0.5px; }}
h2 .count {{ font-weight: 400; color: var(--fg-muted); margin-left: 4px; }}
.generated-at {{ font-size: 12px; color: var(--fg-muted); font-variant-numeric: tabular-nums; }}
section.list-section {{ margin-bottom: 24px; }}
table {{
  width: 100%;
  border-collapse: collapse;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
}}
th, td {{
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
}}
th {{
  background: #f0f0f0;
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  color: #555;
}}
tbody tr:last-child td {{ border-bottom: none; }}
tbody tr:hover {{ background: var(--row-hover); }}
td.ticker {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; font-weight: 600; }}
.muted {{ color: var(--fg-muted); }}
.qa-yes, .qa-no {{
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  letter-spacing: 0.3px;
}}
.qa-yes {{ background: #e0f0e0; color: #2a6a2a; }}
.qa-no {{ background: #f0e0e0; color: #8a4040; }}
.comments-open {{ color: #b88a1f; font-weight: 500; }}
.breach-badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 3px;
  font-size: 11px;
  color: white;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  font-weight: 500;
}}
.open-link {{ color: var(--link); text-decoration: none; }}
.open-link:hover {{ text-decoration: underline; }}
.empty {{ color: var(--fg-muted); font-style: italic; padding: 12px; }}
.banner {{
  font-size: 12px;
  color: var(--fg-muted);
  margin-top: 32px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}}
</style>
</head>
<body>
<header>
  <h1>Earnings Summary — Dashboard</h1>
  <span class="generated-at">generated {generated_at}</span>
</header>
{portfolio_section}
{evaluation_section}
<div class="banner">
  Read-only view (PR 1). Per-ticker actions, comments processing, and bulk
  refreshes land in later PRs. Workspace report links open in a new tab.
</div>
</body>
</html>
"""
