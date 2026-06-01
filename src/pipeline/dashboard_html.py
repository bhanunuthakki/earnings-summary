"""Server-side HTML renderer for the dashboard.

Pure function: takes the rows dict produced by `dashboard_status.build_dashboard_rows`
and returns a full HTML document. Carries one piece of client JS — the "Refresh
IR KPIs" control, which POSTs to `/actions/refresh-ir` and streams the job's
output back via an EventSource on `/actions/stream/<job_id>`. Style is minimal
and self-contained (no external CSS / fonts) so the page renders identically
whether the user has the workspace report open elsewhere or not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from pipeline.dashboard_status import DashboardRow, TranscriptStatus

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
        actions_block=_ACTIONS_BLOCK,
        maintenance_block=_MAINTENANCE_BLOCK,
        portfolio_section=_render_section(
            "Portfolio", portfolio_rows, empty_msg="No portfolio tickers."
        ),
        evaluation_section=_render_section(
            "Evaluation", evaluation_rows, empty_msg="No evaluation tickers."
        ),
        generated_at=escape(stamp),
    )


def render_status_overview(rows_by_list: dict[str, list[DashboardRow]]) -> str:
    """Overview-tab body for the command-center shell: the IR-KPI + maintenance
    action blocks followed by the portfolio + evaluation status tables.

    A public seam over the same private renderers the standalone dashboard page
    uses, so the shell's inlined Overview and ``GET /``'s old page share one code
    path (the privates stay internal — no cross-module private access)."""
    portfolio_rows = rows_by_list.get("portfolio", [])
    evaluation_rows = rows_by_list.get("evaluation", [])
    return "".join(
        [
            _ACTIONS_BLOCK,
            _MAINTENANCE_BLOCK,
            _render_section("Portfolio", portfolio_rows, empty_msg="No portfolio tickers."),
            _render_section("Evaluation", evaluation_rows, empty_msg="No evaluation tickers."),
        ]
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
        f"<td class='ticker'><a href='/ticker/{escape(row.ticker)}'>{escape(row.ticker)}</a></td>"
        f"<td>{fmp_cell}</td>"
        f"<td>{transcript_cell}</td>"
        f"<td>{build_cell}</td>"
        f"<td>{comments_cell}</td>"
        f"<td>{breach_cell}</td>"
        f"<td>{open_cell}</td>"
        "</tr>"
    )


def _format_transcript(t: TranscriptStatus | None) -> str:
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
    return f"<span class='breach-badge' style='background:{escape(color)}'>{escape(status)}</span>"


# The "Refresh IR KPIs" control. Rendered into the page via a `{actions_block}`
# placeholder, so it is a `.format()` *argument* — its literal `{`/`}` (CSS rules,
# JS object literals) pass through untouched and need no double-brace escaping.
# In the JS, newlines appended to the log are written `\\n` in this Python source
# so they reach the browser as a JS `\n` escape, not a raw line break (which would
# be a syntax error inside a single-quoted JS string).
_ACTIONS_BLOCK = """
<section class="actions-section" aria-labelledby="actions-h2">
  <h2 id="actions-h2">Refresh IR KPIs</h2>
  <p class="actions-help">
    Pull the issuer's official historical-data spreadsheet (a headless browser
    discovers the current URL), parse it, and ingest KPI facts at the IR-doc
    tier &mdash; superseding LLM-brief values. The ticker needs a parser config
    in <code>micro_thesis/ir_config/</code> (e.g. NU).
  </p>
  <form id="refresh-ir-form" class="actions-form" autocomplete="off">
    <input type="text" id="ir-ticker" name="ticker" class="ir-ticker"
           placeholder="Ticker (e.g. NU)" required aria-label="Ticker">
    <label class="ir-quarters-label">quarters
      <input type="number" id="ir-quarters" name="quarters" class="ir-quarters"
             value="8" min="1" max="40">
    </label>
    <button type="submit" id="ir-submit">Refresh IR KPIs</button>
    <span id="ir-status" class="actions-status" role="status" aria-live="polite"></span>
  </form>
  <pre id="ir-output" class="actions-output" hidden></pre>
</section>
<style>
.actions-section { margin: 0 0 28px; padding: 16px 18px; background: var(--bg-card);
  border: 1px solid var(--border); border-radius: 4px; }
.actions-section h2 { margin: 0 0 6px; }
.actions-help { font-size: 12px; color: var(--fg-muted); margin: 0 0 12px; max-width: 760px; }
.actions-help code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.actions-form { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ir-ticker { width: 170px; padding: 6px 9px; font-size: 13px; border: 1px solid var(--border);
  border-radius: 4px; text-transform: uppercase;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ir-quarters-label { font-size: 12px; color: var(--fg-muted); display: inline-flex;
  align-items: center; gap: 6px; }
.ir-quarters { width: 60px; padding: 6px 8px; font-size: 13px; border: 1px solid var(--border);
  border-radius: 4px; }
#ir-submit { padding: 7px 14px; font-size: 13px; font-weight: 600; color: #fff;
  background: var(--link); border: none; border-radius: 4px; cursor: pointer; }
#ir-submit:disabled { opacity: 0.5; cursor: progress; }
.actions-status { font-size: 12px; font-weight: 500; }
.actions-status.running { color: #b88a1f; }
.actions-status.ok { color: #3a8a3a; }
.actions-status.error { color: #b04040; }
.actions-output { margin: 12px 0 0; padding: 10px 12px; max-height: 320px; overflow-y: auto;
  background: #1c1c1c; color: #e6e6e6; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
</style>
<script>
(function () {
  var form = document.getElementById('refresh-ir-form');
  if (!form) return;
  var tickerEl = document.getElementById('ir-ticker');
  var quartersEl = document.getElementById('ir-quarters');
  var submitEl = document.getElementById('ir-submit');
  var statusEl = document.getElementById('ir-status');
  var outputEl = document.getElementById('ir-output');
  var es = null;
  var finished = false;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = 'actions-status' + (cls ? ' ' + cls : '');
  }
  function appendLine(line) {
    outputEl.hidden = false;
    outputEl.textContent += line + '\\n';
    outputEl.scrollTop = outputEl.scrollHeight;
  }
  function closeStream() {
    if (es) { es.close(); es = null; }
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var ticker = (tickerEl.value || '').trim().toUpperCase();
    if (!ticker) { setStatus('Enter a ticker.', 'error'); return; }
    var quarters = parseInt(quartersEl.value, 10);
    if (!(quarters > 0)) quarters = 8;
    closeStream();
    finished = false;
    outputEl.textContent = '';
    outputEl.hidden = true;
    submitEl.disabled = true;
    setStatus('Starting...', 'running');

    fetch('/actions/refresh-ir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker, quarters: quarters })
    }).then(function (resp) {
      return resp.json().then(function (body) {
        return { ok: resp.ok, status: resp.status, body: body };
      });
    }).then(function (r) {
      if (!r.ok) {
        var msg = (r.body && r.body.error) ? r.body.error : ('HTTP ' + r.status);
        setStatus('Error: ' + msg, 'error');
        submitEl.disabled = false;
        return;
      }
      setStatus('Running ' + r.body.ticker + ' (' + r.body.kind + ')...', 'running');
      es = new EventSource(r.body.stream_url);
      es.onmessage = function (e) {
        var m;
        try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'start') {
          appendLine('> job ' + m.job_id + ' started for ' + m.ticker + ' (' + m.kind + ')');
        } else if (m.event === 'log') {
          appendLine(m.line);
        } else if (m.event === 'done') {
          finished = true;
          appendLine('# exit code ' + m.exit_code);
          if (m.exit_code === 0) {
            setStatus('Done. KPIs ingested at IR-doc tier.', 'ok');
          } else {
            setStatus('Failed (exit ' + m.exit_code + '). See log above.', 'error');
          }
          submitEl.disabled = false;
          closeStream();
        }
      };
      es.onerror = function () {
        if (finished) return;
        setStatus('Stream interrupted - is the server still running?', 'error');
        submitEl.disabled = false;
        closeStream();
      };
    }).catch(function (err) {
      setStatus('Request failed: ' + err.message, 'error');
      submitEl.disabled = false;
    });
  });
})();
</script>
""".strip()


# Repo-wide maintenance controls. Like _ACTIONS_BLOCK this is a `.format()`
# argument, so its literal {/} (JS object literals, CSS) pass through untouched.
# Reuses the .actions-section / .actions-output / .actions-status styles above.
_MAINTENANCE_BLOCK = """
<section class="actions-section" aria-labelledby="maint-h2">
  <h2 id="maint-h2">Maintenance</h2>
  <p class="actions-help">
    Repo-wide chores, streamed live — the same CLIs the crons run.
  </p>
  <div class="actions-form">
    <button type="button" class="maint-btn" data-action="seed_kpis">Seed KPI defs</button>
    <button type="button" class="maint-btn" data-action="process_inbox">Process dropped docs</button>
    <button type="button" class="maint-btn" data-action="sweep_history">Sweep output history</button>
    <button type="button" class="maint-btn" data-action="onboard_pending">Onboard pending</button>
    <span class="maint-sep">|</span>
    <input type="text" id="maint-onboard-ticker" class="ir-ticker" placeholder="Ticker" aria-label="Ticker to onboard">
    <button type="button" class="maint-btn" data-action="onboard" data-needs-ticker="1">Onboard ticker</button>
    <span id="maint-status" class="actions-status" role="status" aria-live="polite"></span>
  </div>
  <pre id="maint-output" class="actions-output" hidden></pre>
</section>
<style>
.maint-btn { padding: 6px 11px; font-size: 12px; font-weight: 600; color: #fff;
  background: var(--link); border: none; border-radius: 4px; cursor: pointer; }
.maint-btn:disabled { opacity: 0.5; cursor: progress; }
.maint-sep { color: #ccc; margin: 0 4px; }
</style>
<script>
(function () {
  var btns = document.querySelectorAll('.maint-btn');
  if (!btns.length) return;
  var statusEl = document.getElementById('maint-status');
  var outputEl = document.getElementById('maint-output');
  var tickerEl = document.getElementById('maint-onboard-ticker');
  var es = null, finished = false;
  function setStatus(t, c) { statusEl.textContent = t; statusEl.className = 'actions-status' + (c ? ' ' + c : ''); }
  function appendLine(l) { outputEl.hidden = false; outputEl.textContent += l + '\\n'; outputEl.scrollTop = outputEl.scrollHeight; }
  function enable() { btns.forEach(function (b) { b.disabled = false; }); }
  function run(action, ticker) {
    if (es) { es.close(); es = null; }
    finished = false; outputEl.textContent = ''; outputEl.hidden = true;
    btns.forEach(function (b) { b.disabled = true; });
    setStatus('Starting ' + action + '...', 'running');
    var payload = { action: action };
    if (ticker) payload.ticker = ticker;
    fetch('/actions/maintenance', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().then(function (body) { return { ok: resp.ok, status: resp.status, body: body }; });
    }).then(function (r) {
      if (!r.ok) { setStatus('Error: ' + ((r.body && r.body.error) || ('HTTP ' + r.status)), 'error'); enable(); return; }
      setStatus('Running ' + r.body.kind + '...', 'running');
      es = new EventSource(r.body.stream_url);
      es.onmessage = function (e) {
        var m; try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'start') { appendLine('> ' + m.kind + ' started (job ' + m.job_id + ')'); }
        else if (m.event === 'log') { appendLine(m.line); }
        else if (m.event === 'done') {
          finished = true; appendLine('# exit code ' + m.exit_code);
          setStatus(m.exit_code === 0 ? 'Done.' : 'Failed (exit ' + m.exit_code + ').', m.exit_code === 0 ? 'ok' : 'error');
          enable(); if (es) { es.close(); es = null; }
        }
      };
      es.onerror = function () { if (finished) return; setStatus('Stream interrupted.', 'error'); enable(); if (es) { es.close(); es = null; } };
    }).catch(function (err) { setStatus('Request failed: ' + err.message, 'error'); enable(); });
  }
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      var needsTicker = b.getAttribute('data-needs-ticker');
      var ticker = needsTicker ? (tickerEl.value || '').trim().toUpperCase() : '';
      if (needsTicker && !ticker) { setStatus('Enter a ticker to onboard.', 'error'); return; }
      run(b.getAttribute('data-action'), ticker);
    });
  });
})();
</script>
""".strip()


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
.title-block {{ display: flex; flex-direction: column; gap: 2px; }}
.top-nav a {{ font-size: 12px; color: var(--link); text-decoration: none; }}
.top-nav a:hover {{ text-decoration: underline; }}
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
td.ticker a {{ color: var(--link); text-decoration: none; }}
td.ticker a:hover {{ text-decoration: underline; }}
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
  <div class="title-block">
    <h1>Earnings Summary — Dashboard</h1>
    <nav class="top-nav"><a href="/analytical">Analytical overview ↗</a></nav>
  </div>
  <span class="generated-at">generated {generated_at}</span>
</header>
{actions_block}
{maintenance_block}
{portfolio_section}
{evaluation_section}
<div class="banner">
  Workspace report links open in a new tab. The "Refresh IR KPIs" panel above
  ingests issuer-spreadsheet KPIs live; broader comments processing and bulk
  refreshes land in later PRs.
</div>
</body>
</html>
"""
