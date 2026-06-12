"""IR-document coverage panel for the command-center shell.

Surfaces, per portfolio + evaluation name, whether the headless IR auto-fetch has
landed any documents — so a *gap* (a name with ZERO auto-fetched IR docs) is
visible as a manual-pull to-do, with the last crawl outcome explaining why (a
bot-protected 403, a headless load-stall, or simply "not yet crawled").

Coverage is read live from the document store (``documents.source_type='ir_doc'``)
so a manual upload shows up immediately; crawl health is read from
``ir_fetch_status``. Both come from ``src/ir_fetch_status.py``. Reuses the shell's
dark panel / kpi-strip / table CSS vocabulary, with a small scoped ``<style>`` for
the status pills.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from ir_fetch_status import IrCoverageRow, briefed_roster, coverage_rows

_PANEL_STYLE = """<style>
.ir-cov-table td.tk { font-weight:600; font-family:var(--mono); }
.ir-ok { background:color-mix(in srgb, var(--ok) 16%, transparent); color:var(--ok); }
.ir-gap { background:color-mix(in srgb, var(--bad) 16%, transparent); color:var(--bad); }
tr.ir-gap-row td { background:color-mix(in srgb, var(--bad) 7%, transparent); }
.ir-reason { font-size:var(--fs-caption); color:var(--muted); margin-top:3px; }
.ir-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius); font-size:var(--fs-body);
  line-height:1.55; }
.ir-note code { background:var(--surface); padding:1px 5px; border-radius:4px; }
</style>"""


def render_ir_coverage_panel(db_path: Path) -> str:
    """The IR Docs tab fragment: a coverage KPI strip + a gaps-first table of every
    portfolio/evaluation name's auto-fetch status, plus the manual-pull how-to."""
    rows = coverage_rows(db_path, briefed_roster(db_path))
    if not rows:
        return (
            '<section class="panel"><h2>IR document coverage</h2>'
            '<p class="muted">No portfolio or evaluation names are tracked yet.</p></section>'
        )
    gaps = [r for r in rows if not r.has_docs]
    covered = len(rows) - len(gaps)
    total_docs = sum(r.doc_count for r in rows)
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>IR document coverage</h2>',
            '<p class="sub">Headless auto-fetch of investor-relations documents across the '
            "portfolio + evaluation list. A <strong>gap</strong> is a name with zero "
            "auto-fetched IR docs — pull it manually (see below). Coverage is live from the "
            "document store; failing names are retried automatically each week.</p>",
            _kpi_strip(covered, len(gaps), total_docs),
            _coverage_table(rows),
            _manual_note(),
            "</section>",
        ]
    )


def _kpi_strip(covered: int, gaps: int, total_docs: int) -> str:
    cov_tone = "tone-good" if gaps == 0 else "tone-warn"
    gap_tone = "tone-bad" if gaps else "tone-good"
    return (
        '<div class="kpi-strip">'
        f'<div class="kpi-card {cov_tone}"><div class="kpi-label">Auto-fetched</div>'
        f'<div class="kpi-value">{covered}/{covered + gaps}</div>'
        '<div class="kpi-sub">names with IR docs</div></div>'
        f'<div class="kpi-card {gap_tone}"><div class="kpi-label">Manual pull needed</div>'
        f'<div class="kpi-value">{gaps}</div>'
        '<div class="kpi-sub">names with a gap</div></div>'
        '<div class="kpi-card"><div class="kpi-label">IR documents</div>'
        f'<div class="kpi-value">{total_docs}</div>'
        '<div class="kpi-sub">registered total</div></div>'
        "</div>"
    )


def _coverage_table(rows: list[IrCoverageRow]) -> str:
    body = "".join(_row(r) for r in rows)
    return (
        '<table class="p-table ir-cov-table"><thead><tr>'
        "<th>Ticker</th><th>Name</th><th>List</th>"
        '<th class="num">IR docs</th><th>Latest period</th><th>Last fetched</th>'
        "<th>Last crawl</th><th>Status</th>"
        "</tr></thead><tbody>"
        f"{body}</tbody></table>"
    )


def _row(r: IrCoverageRow) -> str:
    if r.has_docs:
        plural = "s" if r.doc_count != 1 else ""
        status_cell = f'<span class="p-pill ir-ok">&#10003; {r.doc_count} doc{plural}</span>'
        docs_cell = f'<td class="num">{r.doc_count}</td>'
        period = escape(r.latest_period or "—")
        fetched = _fmt_date(r.last_doc_at)
        row_attr = ""
    else:
        status_cell = (
            '<span class="p-pill ir-gap">manual pull</span>'
            f'<div class="ir-reason">{escape(_gap_reason(r))}</div>'
        )
        docs_cell = '<td class="num muted">0</td>'
        period = "—"
        fetched = "—"
        row_attr = ' class="ir-gap-row"'
    return (
        f"<tr{row_attr}>"
        f'<td class="tk">{escape(r.ticker)}</td>'
        f"<td>{escape(r.name or '—')}</td>"
        f"<td>{escape(r.list_type)}</td>"
        f"{docs_cell}"
        f"<td>{period}</td>"
        f"<td>{fetched}</td>"
        f"<td>{_crawl_cell(r)}</td>"
        f"<td>{status_cell}</td>"
        "</tr>"
    )


def _crawl_cell(r: IrCoverageRow) -> str:
    st = r.status
    if st is None:
        return '<span class="muted">never</span>'
    return f'{escape(st.last_status)} <span class="muted">· {_fmt_date(st.last_attempt_at)}</span>'


def _gap_reason(r: IrCoverageRow) -> str:
    st = r.status
    if st is None:
        return "not yet crawled — runs on the next weekly sweep"
    if st.reason:
        return st.reason
    return f"last crawl: {st.last_status}, no documents found"


def _fmt_date(iso: str | None) -> str:
    return escape(iso[:10]) if iso else "—"


def _manual_note() -> str:
    return (
        '<div class="ir-note">'
        "<strong>Filling a gap manually:</strong> download the IR document (press release, "
        "deck, transcript, supplement, or IR update), drop it into <code>ir_documents/</code>, "
        "and register it &mdash;"
        '<pre class="cli-hint">python execution/categorize_ir_uploads.py --ticker &lt;TICKER&gt;</pre>'
        "It content-classifies the period + type, files it under "
        "<code>ir_documents/&lt;TICKER&gt;/&lt;period&gt;/</code>, and feeds it into the next "
        "<code>--enable-llm</code> brief. Bot-protected names (403 / headless block) are "
        "re-attempted automatically by the weekly failing-crawler rescan, in case the site "
        "starts cooperating."
        "</div>"
    )
