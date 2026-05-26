"""ReportSpec → standalone HTML research doc.

Single self-contained HTML file with embedded CSS — opens in any browser,
prints to PDF cleanly, and renders identically across machines. Replaces
the (rendering-fragile) reportlab PDF path.

Design choices:
  - Inter font (matches frontend); system-fallback for offline use.
  - Light theme; print-friendly via @media print.
  - Sticky in-page nav at the top.
  - Wide tables scroll horizontally inside their containers (no overflow).
  - <details> collapsibles wrap long earnings cards so the doc stays
    skimmable; click to expand.
"""

from __future__ import annotations

import html
import re
from io import StringIO

from report.models import (
    AnnualLineItem,
    AppendixSection,
    BearCaseSection,
    BreakRuleEvaluation,
    CompanyDescriptionSection,
    DecisionBadge,
    EarningsSection,
    EvaluationSnapshotSection,
    FinancialsSection,
    IrDocsSection,
    KpiLedgerRow,
    ProvenanceSection,
    QuarterlyEarningsCard,
    QuarterlyLineItem,
    RecentDevelopmentsSection,
    ReportFlavor,
    ReportSpec,
    SayDoSection,
    SectionStatus,
    SegmentSecondaryExpansion,
    SegmentSeries,
    SegmentsSection,
    SegmentWeighting,
    SnapshotSection,
    SoftRuleEvaluation,
    SurpriseScorecardCard,
    ThesisSection,
    TranscriptEntry,
    ValuationSnapshot,
)
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.charts_v2 import (
    BarSpec,
    LineSeries,
    MatrixRow,
    bar_chart,
    paired_chart,
    stacked_area,
    yoy_heatmap_table,
)

_BOLD_RX = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RX = re.compile(r"`([^`]+)`")


def render(spec: ReportSpec) -> str:
    body = StringIO()
    _nav(body)
    _header(body, spec)
    if spec.portfolio_position is not None:
        _portfolio_position(body, spec)
    if spec.flavor == ReportFlavor.EVALUATION and spec.evaluation_snapshot is not None:
        _evaluation_snapshot(body, spec.evaluation_snapshot)
    else:
        _snapshot(body, spec.snapshot)
    _company_description(body, spec.company_description)
    _thesis(body, spec.thesis, spec.ticker)
    _financials(body, spec.financials)
    _segments(body, spec.segments)
    _earnings(body, spec.earnings)
    _saydo(body, spec.saydo)
    _ir_docs(body, spec.ir_docs)
    _recent_developments(body, spec.recent_developments)
    _bear_case(body, spec.bear_case)
    _provenance(body, spec.provenance)
    _appendix(body, spec.appendix)
    return _document(spec, body.getvalue())


# ---------------------------------------------------------------------------
# Document chrome
# ---------------------------------------------------------------------------


def _document(spec: ReportSpec, body: str) -> str:
    title = f"{spec.ticker} — research report"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_CSS}</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


_CSS = (
    """
:root {
  --bg: #ffffff;
  --fg: #111827;
  --muted: #6b7280;
  --border: #e5e7eb;
  --accent: #2563eb;
  --header-bg: #1f2937;
  --header-fg: #ffffff;
  --subheader-bg: #f3f4f6;
  --callout-bg: #fef3c7;
  --callout-border: #f59e0b;
  --green: #15803d;
  --yellow: #a16207;
  --red: #b91c1c;
  --gray: #6b7280;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
}
main {
  max-width: 1480px;
  margin: 0 auto;
  padding: 24px 32px 96px;
}
nav.toc {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 8px 0;
  margin-bottom: 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 13px;
}
nav.toc a {
  color: var(--muted);
  text-decoration: none;
  padding: 4px 8px;
  border-radius: 4px;
}
nav.toc a:hover { background: var(--subheader-bg); color: var(--fg); }
h1 { font-size: 28px; font-weight: 700; margin: 8px 0 4px; }
h2 {
  font-size: 20px;
  font-weight: 600;
  margin: 32px 0 12px;
  padding-bottom: 6px;
  border-bottom: 2px solid var(--border);
  display: flex;
  align-items: baseline;
  gap: 12px;
}
h3 { font-size: 16px; font-weight: 600; margin: 18px 0 8px; }
h4 { font-size: 14px; font-weight: 600; margin: 12px 0 6px; }
p { margin: 6px 0; }
ul, ol { margin: 6px 0; padding-left: 24px; }
li { margin: 2px 0; }
.meta { color: var(--muted); font-size: 12px; }
.status-badge {
  font-size: 11px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--subheader-bg);
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.status-ok { background: #dcfce7; color: var(--green); }
.status-partial { background: #fef9c3; color: var(--yellow); }
.status-missing_data, .status-llm_pending, .status-not_applicable {
  background: #fee2e2; color: var(--red);
}
.callout {
  background: var(--callout-bg);
  border-left: 3px solid var(--callout-border);
  padding: 10px 14px;
  margin: 12px 0;
  font-size: 13px;
  border-radius: 0 6px 6px 0;
}
.callout code {
  background: rgba(0,0,0,0.06);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
code, .mono {
  font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
  font-size: 0.92em;
}
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  margin: 8px 0;
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 12.5px;
}
thead th {
  background: var(--header-bg);
  color: var(--header-fg);
  font-weight: 600;
  text-align: left;
  padding: 6px 10px;
  position: sticky;
  top: 0;
  white-space: nowrap;
}
tbody td {
  padding: 5px 10px;
  border-top: 1px solid var(--border);
  vertical-align: top;
  white-space: nowrap;
}
tbody td:first-child, tbody td:nth-child(2) { white-space: normal; }
tbody tr:nth-child(odd) td { background: #fafbfc; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.kpi-table td:nth-child(2),
.kpi-table td:nth-child(3),
.kpi-table td:nth-child(4) { white-space: normal; max-width: 380px; }
details { margin: 6px 0; }
details summary {
  cursor: pointer;
  font-weight: 600;
  padding: 4px 0;
  list-style: none;
}
details summary::-webkit-details-marker { display: none; }
details summary::before { content: '▸ '; color: var(--muted); }
details[open] summary::before { content: '▾ '; }
details.earnings-card { border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; margin: 10px 0; }
details.earnings-card[open] { background: #fcfcfd; }
.earnings-title { font-size: 16px; }
.earnings-digest { font-weight: 400; margin-top: 6px; color: var(--fg); }
.earnings-digest p { margin: 4px 0; }
.earnings-full { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border); }
.seg-snapshot { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 8px 0 14px; }
.seg-card { border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; background: #fcfcfd; }
.seg-card-name { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
.seg-card-spark { color: var(--accent); margin: 4px 0 8px; }
.seg-card-row { display: flex; justify-content: space-between; font-size: 12px; padding: 2px 0; color: var(--muted); }
.seg-card-row strong { color: var(--fg); font-variant-numeric: tabular-nums; }
details.segment-details { margin-top: 6px; }
details.seg-card-def { margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--border); }
details.seg-card-def summary { font-size: 11px; color: var(--muted); }
details.seg-card-def p { font-size: 11.5px; line-height: 1.4; margin: 6px 0 0; color: var(--fg); }
.seg-def-mark { font-size: 11px; vertical-align: super; opacity: 0.6; cursor: help; }
.company-pitch {
  font-size: 15px;
  line-height: 1.55;
  margin: 8px 0 12px;
  padding: 12px 14px;
  background: var(--subheader-bg);
  border-left: 3px solid var(--accent);
  border-radius: 0 6px 6px 0;
}
details.company-details { margin: 10px 0 4px; }
details.company-details > h3 { margin-top: 16px; }
table.weighting-table td:nth-child(3) {
  white-space: nowrap;
  min-width: 140px;
}
.share-bar {
  display: inline-block;
  vertical-align: middle;
  width: 60px;
  height: 8px;
  background: var(--border);
  border-radius: 4px;
  margin-right: 6px;
  overflow: hidden;
}
.share-fill {
  display: block;
  height: 100%;
  background: var(--accent);
}
.decision-history {
  margin: 14px 0 18px;
  padding: 10px 14px;
  background: var(--subheader-bg);
  border-radius: 6px;
  border-left: 3px solid var(--accent);
}
.decision-history h3 {
  margin: 0 0 6px;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.decision-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  margin: 3px 0;
  border-radius: 4px;
  font-size: 12.5px;
  background: var(--bg);
  border: 1px solid var(--border);
  cursor: default;
}
.decision-badge .decision-date {
  font-variant-numeric: tabular-nums;
  color: var(--muted);
}
.decision-badge .decision-kind {
  font-weight: 600;
  letter-spacing: 0.3px;
}
.decision-badge .decision-outcome {
  margin-left: auto;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.decision-badge.outcome-correct .decision-outcome { background: #dcfce7; color: var(--green); }
.decision-badge.outcome-wrong .decision-outcome { background: #fee2e2; color: var(--red); }
.decision-badge.outcome-mixed .decision-outcome { background: #fef9c3; color: var(--yellow); }
.decision-badge.outcome-pending .decision-outcome { background: #f3f4f6; color: var(--gray); }
.decision-badge.outcome-unfalsifiable .decision-outcome {
  background: #f3f4f6;
  color: var(--gray);
  font-style: italic;
}
"""
    + CHARTS_V2_CSS
    + """
.summary-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin: 12px 0 24px;
}
.summary-card {
  background: var(--subheader-bg);
  border-radius: 8px;
  padding: 14px 18px;
}
.summary-card h4 {
  margin: 0 0 8px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted);
}
.summary-card .big { font-size: 22px; font-weight: 600; }
pre.transcript {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 16px 20px;
  margin: 8px 0 16px;
  white-space: pre-wrap;
  word-wrap: break-word;
  font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.5;
  max-height: 70vh;
  overflow-y: auto;
}
@media print {
  nav.toc { display: none; }
  body { font-size: 11px; }
  table { font-size: 10.5px; page-break-inside: auto; }
  details[open] summary::before, details:not([open]) summary::before { content: ''; }
  details { page-break-inside: avoid; }
  details > *:not(summary) { display: block !important; }
}
"""
)


def _nav(out: StringIO) -> None:
    sections = [
        ("snapshot", "§1 Snapshot"),
        ("company-description", "§2 Company"),
        ("thesis", "§3 Thesis"),
        ("financials", "§4 Financials"),
        ("segments", "§5 Segments"),
        ("earnings", "§6 Earnings"),
        ("saydo", "§7 Say-Do"),
        ("ir-docs", "§8 IR docs"),
        ("recent-developments", "§9 Recent developments"),
        ("bear-case", "§10 Bear case"),
        ("provenance", "§11 Provenance"),
        ("appendix", "§12 Transcripts"),
    ]
    out.write('<nav class="toc">')
    for anchor, label in sections:
        out.write(f'<a href="#{anchor}">{label}</a>')
    out.write("</nav>\n")


def _header(out: StringIO, spec: ReportSpec) -> None:
    out.write(f"<h1>{html.escape(spec.ticker)} — research report</h1>\n")
    out.write(
        f'<p class="meta">Generated {html.escape(spec.generation_date.isoformat())} · '
        f"repo <code>{html.escape(spec.repo_root)}</code></p>\n"
    )


def _portfolio_position(out: StringIO, spec: ReportSpec) -> None:
    pp = spec.portfolio_position
    if pp is None or pp.status == SectionStatus.NOT_APPLICABLE:
        return
    out.write('<section class="portfolio-position" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;margin:12px 0;">\n')
    out.write(f"<h2 style=\"margin-top:0;font-size:16px;\">Your position in {html.escape(spec.ticker)}</h2>\n")
    if pp.held and pp.total_market_value is not None:
        pnl = pp.total_unrealized_pnl or 0.0
        pct = pp.total_unrealized_pct
        pnl_color = "#16a34a" if pnl >= 0 else "#dc2626"
        pct_str = f"{pct * 100:+.1f}%" if pct is not None else "—"
        out.write(
            '<p style="margin:4px 0;">'
            f"<strong>{pp.total_quantity:,.4f} sh</strong> · "
            f"cost <strong>${(pp.total_cost_basis or 0):,.0f}</strong> · "
            f"value <strong>${pp.total_market_value:,.0f}</strong> · "
            f'unrealized <strong style="color:{pnl_color};">${pnl:+,.0f} ({pct_str})</strong>'
            "</p>\n"
        )
    elif pp.held:
        out.write(f'<p style="margin:4px 0;"><strong>{pp.total_quantity:,.4f} sh</strong> held (cost basis unknown)</p>\n')
    if pp.accounts:
        out.write('<table style="width:100%;margin-top:8px;font-size:13px;border-collapse:collapse;">\n')
        out.write('<thead><tr style="text-align:left;border-bottom:1px solid #e2e8f0;"><th>Account</th><th style="text-align:right;">Qty</th><th style="text-align:right;">Cost</th><th style="text-align:right;">Value</th><th style="text-align:right;">Unrealized</th><th>Source</th></tr></thead>\n<tbody>\n')
        for a in pp.accounts:
            cost_s = f"${a.cost_basis:,.0f}" if a.cost_basis is not None else "—"
            value_s = f"${a.market_value:,.0f}" if a.market_value is not None else "—"
            if a.unrealized_pnl is not None and a.unrealized_pct is not None:
                color = "#16a34a" if a.unrealized_pnl >= 0 else "#dc2626"
                pnl_s = f'<span style="color:{color};">${a.unrealized_pnl:+,.0f} ({a.unrealized_pct * 100:+.1f}%)</span>'
            else:
                pnl_s = "—"
            src = a.cost_basis_source or "broker"
            out.write(
                f"<tr><td>{html.escape(a.account_name)}</td>"
                f"<td style=\"text-align:right;\">{a.quantity:,.4f}</td>"
                f"<td style=\"text-align:right;\">{cost_s}</td>"
                f"<td style=\"text-align:right;\">{value_s}</td>"
                f"<td style=\"text-align:right;\">{pnl_s}</td>"
                f"<td>{html.escape(src)}</td></tr>\n"
            )
        out.write("</tbody></table>\n")
    if pp.recent_transactions:
        out.write('<details style="margin-top:8px;"><summary><strong>Recent activity</strong></summary>\n<ul style="font-size:13px;">\n')
        for t in pp.recent_transactions:
            out.write(
                "<li>"
                f"{t.date.isoformat()} · {html.escape(t.account_name)} · {t.type} "
                f"{abs(t.quantity):,.4f} sh · ${abs(t.amount):,.0f}"
                "</li>\n"
            )
        out.write("</ul></details>\n")
    if pp.open_decisions:
        out.write('<div style="margin-top:8px;"><strong>Your open thesis on this name</strong>\n<ul style="font-size:13px;">\n')
        for d in pp.open_decisions:
            conf = f" ({d.confidence})" if d.confidence else ""
            thesis_truncated = d.thesis[:240] + ("…" if len(d.thesis) > 240 else "")
            brief_link = (
                f' <code style="font-size:11px;">linked: {html.escape(d.linked_brief_path)}</code>'
                if d.linked_brief_path
                else ""
            )
            out.write(
                f"<li>{d.decision_date.isoformat()} · <strong>{html.escape(d.action)}</strong>{conf}: "
                f"{html.escape(thesis_truncated)}{brief_link}</li>\n"
            )
        out.write("</ul></div>\n")
    if pp.closed_decisions:
        out.write('<details style="margin-top:8px;"><summary><strong>Track record on this name</strong> ({} closed)</summary>\n<ul style="font-size:13px;">\n'.format(len(pp.closed_decisions)))
        for d in pp.closed_decisions:
            conf = f" ({d.confidence})" if d.confidence else ""
            outcome_color = {
                "validated": "#16a34a",
                "invalidated": "#dc2626",
                "partial": "#ca8a04",
            }.get(d.outcome_status or "", "#475569")
            outcome_label = {
                "validated": "validated",
                "invalidated": "invalidated",
                "partial": "partial",
            }.get(d.outcome_status or "", d.outcome_status or "")
            outcome_when = (
                f"{d.outcome_date.isoformat()} " if d.outcome_date else ""
            )
            thesis_truncated = d.thesis[:160] + ("…" if len(d.thesis) > 160 else "")
            notes = (
                f' <em style="color:#64748b;">— {html.escape(d.outcome_notes[:160])}{("…" if len(d.outcome_notes) > 160 else "")}</em>'
                if d.outcome_notes
                else ""
            )
            out.write(
                f"<li>{d.decision_date.isoformat()} · <strong>{html.escape(d.action)}</strong>{conf}: "
                f"{html.escape(thesis_truncated)} → "
                f'{outcome_when}<strong style="color:{outcome_color};">{outcome_label}</strong>'
                f"{notes}</li>\n"
            )
        out.write("</ul></details>\n")
    out.write("</section>\n")


# ---------------------------------------------------------------------------
# Inline / shared helpers
# ---------------------------------------------------------------------------


def _md_inline(text: str) -> str:
    """Escape, then enable **bold** and `code` from markdown source."""
    safe = html.escape(text)
    safe = _BOLD_RX.sub(r"<strong>\1</strong>", safe)
    safe = _INLINE_CODE_RX.sub(r"<code>\1</code>", safe)
    return safe


def _md_block(text: str) -> str:
    """Render a multi-line markdown-ish block (paragraphs + bullets + tables).

    We preserve structure but don't re-implement a full markdown parser. Tables
    in upstream LLM output are pipe-delimited; we render those as <table>; bullet
    lines starting with `* ` or `- ` become <ul><li>; everything else paragraphs.
    """
    lines = text.split("\n")
    out = StringIO()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            block_end = i
            while block_end < len(lines) and lines[block_end].strip().startswith("|"):
                block_end += 1
            _emit_pipe_table(out, lines[i:block_end])
            i = block_end
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            block_end = i
            while block_end < len(lines):
                ls = lines[block_end].strip()
                if not (ls.startswith("- ") or ls.startswith("* ")):
                    break
                block_end += 1
            out.write("<ul>")
            for li in lines[i:block_end]:
                out.write(f"<li>{_md_inline(li.strip()[2:])}</li>")
            out.write("</ul>\n")
            i = block_end
            continue
        if stripped.startswith("####"):
            out.write(f"<h5>{_md_inline(stripped.lstrip('#').strip())}</h5>\n")
        elif stripped.startswith("###") or stripped.startswith("##") or stripped.startswith("#"):
            out.write(f"<h4>{_md_inline(stripped.lstrip('#').strip())}</h4>\n")
        else:
            out.write(f"<p>{_md_inline(stripped)}</p>\n")
        i += 1
    return out.getvalue()


def _emit_pipe_table(out: StringIO, rows: list[str]) -> None:
    parsed = [_split_pipe_row(r) for r in rows if r.strip()]
    if not parsed:
        return
    if len(parsed) >= 2 and all(set(c) <= set("-: ") for c in parsed[1]):
        header = parsed[0]
        body = parsed[2:]
    else:
        header = parsed[0]
        body = parsed[1:]
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for cell in header:
        out.write(f"<th>{_md_inline(cell)}</th>")
    out.write("</tr></thead>\n<tbody>")
    for row in body:
        out.write("<tr>")
        for cell in row:
            out.write(f"<td>{_md_inline(cell)}</td>")
        out.write("</tr>")
    out.write("</tbody></table></div>\n")


def _split_pipe_row(line: str) -> list[str]:
    parts = line.strip().strip("|").split("|")
    return [p.strip() for p in parts]


def _section_h2(out: StringIO, anchor: str, num: int, title: str, status: SectionStatus) -> None:
    out.write(
        f'<h2 id="{anchor}">§{num} {html.escape(title)}'
        f'<span class="status-badge status-{status.value}">{status.value.replace("_", " ")}</span></h2>\n'
    )


def _missing_callout(out: StringIO, status: SectionStatus, missing: object) -> bool:
    if status == SectionStatus.OK or missing is None:
        return False
    stage = html.escape(str(getattr(missing, "stage", "unknown")))
    fix = html.escape(str(getattr(missing, "fix_command", "")))
    detail = getattr(missing, "detail", None)
    out.write(f'<div class="callout"><strong>Pending stage:</strong> <code>{stage}</code><br>')
    out.write(f"<strong>Fix:</strong> <code>{fix}</code>")
    if detail:
        out.write(f"<br>{html.escape(str(detail))}")
    out.write("</div>\n")
    return status not in (SectionStatus.PARTIAL,)


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_num(v: float | None, digits: int) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _snapshot(out: StringIO, s: SnapshotSection) -> None:
    _section_h2(out, "snapshot", 1, "Executive snapshot", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    out.write(
        f"<p><strong>{html.escape(s.ticker)}</strong> — {html.escape(s.company_name or '—')}</p>\n"
    )
    out.write(
        f"<p><strong>Verdict:</strong> "
        f'<span class="status-badge status-{_verdict_class(s.verdict)}">{s.verdict}</span></p>\n'
    )
    if s.thesis_one_liner:
        out.write(f"<p><strong>Thesis:</strong> {html.escape(s.thesis_one_liner)}</p>\n")
    _valuation_card(out, s.valuation)
    if s.valuation.model_link:
        out.write(
            f'<p class="meta">DCF workbook: <code>{html.escape(s.valuation.model_link)}</code></p>\n'
        )
    _decision_sidebar(out, s.recent_decisions)


def _decision_sidebar(out: StringIO, decisions: list[DecisionBadge]) -> None:
    """Last 3 LLM recommendations from the decisions ledger.

    Hidden entirely when there are no rows — no placeholder. Each badge shows
    date · recommendation · outcome chip, with the rationale excerpt attached
    as a `title` tooltip so hovering reveals the LLM's verdict text.
    """
    if not decisions:
        return
    out.write('<div class="decision-history">\n')
    out.write(
        '<h3>Recent decisions <span class="meta">— last 3 LLM recommendations</span></h3>\n'
    )
    for d in decisions:
        cls = f"decision-badge outcome-{html.escape(d.outcome_label)}"
        tooltip = f' title="{html.escape(d.rationale_short)}"' if d.rationale_short else ""
        kind_label = html.escape(d.recommendation_kind.upper())
        outcome_label = html.escape(d.outcome_label)
        out.write(
            f'<div class="{cls}"{tooltip}>'
            f'<span class="decision-date">{html.escape(d.date_short)}</span>'
            f' · <span class="decision-kind">{kind_label}</span>'
            f' · <span class="decision-outcome">{outcome_label}</span>'
            "</div>\n"
        )
    out.write("</div>\n")


_TRIGGER_LABEL: dict[str, str] = {
    "sell": "SELL — DCF says >20% over fair value",
    "trim": "TRIM — DCF says >10% over fair value",
    "hold": "HOLD — within trim/sell band",
    "initiate_candidate": "INITIATE candidate — beyond MoS bar",
    "unknown": "DCF not yet computed",
}

_TRIGGER_CLASS: dict[str, str] = {
    "sell": "missing_data",
    "trim": "partial",
    "hold": "ok",
    "initiate_candidate": "ok",
    "unknown": "not_applicable",
}


def _valuation_card(out: StringIO, v: ValuationSnapshot) -> None:
    """Render the §1 valuation card. Empty section if DCF hasn't been computed yet."""
    if v.consolidated_npv_per_share is None and v.current_price is None:
        out.write(
            '<div class="callout"><strong>DCF not yet computed.</strong><br>'
            "Run <code>python execution/refresh_dcf.py --ticker &lt;TICKER&gt;</code> "
            "after the canonical workbook (<code>dcf/&lt;TICKER&gt;.xlsx</code>) is in place.</div>\n"
        )
        return
    out.write('<div class="table-wrap"><table class="kpi-table">\n<tbody>')
    if v.consolidated_npv_per_share is not None:
        out.write(
            f'<tr><th>Fair value / share</th><td class="num">'
            f"${v.consolidated_npv_per_share:,.2f}</td></tr>"
        )
    if v.current_price is not None:
        suffix = ""
        if v.live_price_at is not None:
            suffix = (
                f' <span class="meta">(as of '
                f"{html.escape(v.live_price_at.date().isoformat())})</span>"
            )
        out.write(
            f'<tr><th>Live price</th><td class="num">${v.current_price:,.2f}{suffix}</td></tr>'
        )
    if v.over_under_pct is not None:
        trigger_label = _TRIGGER_LABEL.get(v.trigger_status, v.trigger_status)
        trigger_cls = _TRIGGER_CLASS.get(v.trigger_status, "not_applicable")
        out.write(
            f'<tr><th>Over/under</th><td class="num">{v.over_under_pct * 100:+.1f}% '
            f'<span class="status-badge status-{trigger_cls}">{html.escape(trigger_label)}</span>'
            f"</td></tr>"
        )
    out.write("</tbody></table></div>\n")
    meta_parts: list[str] = []
    if v.wacc is not None:
        meta_parts.append(f"WACC {v.wacc * 100:.1f}%")
    if v.mos_bar is not None:
        meta_parts.append(f"MoS bar {v.mos_bar * 100:.0f}%")
    if v.valuation_date is not None:
        meta_parts.append(f"Valued {v.valuation_date}")
    if meta_parts:
        out.write(f'<p class="meta">{html.escape(" · ".join(meta_parts))}</p>\n')


def _verdict_class(v: str) -> str:
    return {"intact": "ok", "watch": "partial", "broken": "missing_data"}.get(v, "not_applicable")


def _evaluation_snapshot(out: StringIO, s: EvaluationSnapshotSection) -> None:
    """§1 for eval flavor: 3y quick-categorization data table."""
    _section_h2(out, "snapshot", 1, "Evaluation snapshot", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    out.write(f"<p><strong>{html.escape(s.ticker)}</strong>")
    if s.company_name:
        out.write(f" — {html.escape(s.company_name)}")
    out.write("</p>\n")
    chips: list[str] = []
    if s.sector:
        chips.append(f"Sector: {html.escape(s.sector)}")
    if s.market_cap is not None:
        chips.append(f"Market cap: ${_fmt_compact_usd(s.market_cap)}")
    if s.current_price is not None:
        chips.append(f"Current price: ${s.current_price:,.2f}")
    if chips:
        out.write(f'<p class="meta">{" · ".join(chips)}</p>\n')
    if not s.rows:
        out.write("<p><em>No metric rows available.</em></p>\n")
        return
    out.write(
        '<p class="meta">3-year quick categorization '
        "(LFY-2 → LFY columns are fiscal-year actuals; TTM is rolling).</p>\n"
    )
    year_labels = [str(y) for y in s.fiscal_years]
    while len(year_labels) < 3:
        year_labels.insert(0, "—")
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h_label in (
        "Metric",
        "Unit",
        year_labels[0],
        year_labels[1],
        year_labels[2],
        "TTM",
        "3y CAGR",
    ):
        out.write(f"<th>{html.escape(h_label)}</th>")
    out.write("</tr></thead>\n<tbody>")
    for r in s.rows:
        out.write("<tr>")
        out.write(f"<td><strong>{html.escape(r.metric)}</strong></td>")
        out.write(f"<td>{html.escape(r.unit)}</td>")
        for v in (r.lfy_minus_2, r.lfy_minus_1, r.lfy, r.ttm):
            out.write(f'<td class="num">{_fmt_metric(v, r.unit, r.digits)}</td>')
        out.write(f'<td class="num">{_fmt_pct(r.cagr_3y)}</td>')
        out.write("</tr>\n")
    out.write("</tbody></table></div>\n")


def _fmt_metric(v: float | None, unit: str, digits: int) -> str:
    """Format a cell value: ratios as %, absolute numbers with thousands separator."""
    if v is None:
        return "—"
    if unit == "%":
        return f"{v * 100:.{digits}f}%"
    return f"{v:,.{digits}f}"


def _fmt_compact_usd(v: float) -> str:
    """Compact USD format: $123B / $45M / $678K."""
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.0f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:,.0f}"


def _company_description(out: StringIO, s: CompanyDescriptionSection) -> None:
    _section_h2(out, "company-description", 2, "Company description", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    chips: list[str] = []
    if s.sector:
        chips.append(f"Sector: {html.escape(s.sector)}")
    if s.industry:
        chips.append(f"Industry: {html.escape(s.industry)}")
    if s.source_fiscal_year is not None:
        chips.append(f"Source: 10-K FY{s.source_fiscal_year}")
    if chips:
        out.write(f'<p class="meta">{" · ".join(chips)}</p>\n')
    if s.elevator_pitch:
        out.write(f'<p class="company-pitch">{html.escape(s.elevator_pitch)}</p>\n')

    if not _has_expandable_content(s):
        return

    out.write(
        '<details class="company-details"><summary>What businesses does the company operate in, '
        "how does it make money, and what's the relative weighting</summary>\n"
    )
    if s.business_overview:
        out.write('<h3>Lines of business</h3>\n')
        out.write(_paragraphs_html(s.business_overview))
    if s.revenue_model:
        out.write('<h3>How it makes money</h3>\n')
        out.write(_paragraphs_html(s.revenue_model))
    if s.segment_breakdown:
        out.write('<h3>Segment weighting (latest quarter)</h3>\n')
        _weighting_table(out, s.segment_breakdown, label="Segment")
    if s.geographic_breakdown:
        out.write('<h3>Geographic weighting (latest quarter)</h3>\n')
        _weighting_table(out, s.geographic_breakdown, label="Geography")
    out.write("</details>\n")


def _has_expandable_content(s: CompanyDescriptionSection) -> bool:
    return bool(
        s.business_overview
        or s.revenue_model
        or s.segment_breakdown
        or s.geographic_breakdown
    )


def _paragraphs_html(text: str) -> str:
    """Render LLM-supplied multi-paragraph text. Splits on blank lines."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not parts:
        return ""
    return "".join(f"<p>{_md_inline(p)}</p>\n" for p in parts)


def _weighting_table(out: StringIO, rows: list[SegmentWeighting], label: str) -> None:
    # Show OI columns only when ≥ 2 rows carry an OI value — a lone OI row
    # surrounded by "—" reads as missing data, not as the table's actual
    # shape. See workspace_html._segment_breakdown_panel for the same rule.
    has_oi = sum(1 for r in rows if r.operating_income_usd_m is not None) >= 2

    out.write('<div class="table-wrap"><table class="weighting-table">\n<thead><tr>')
    headers: tuple[str, ...] = (label, "Revenue (USD M)", "Share")
    if has_oi:
        headers = (*headers, "Op income (USD M)", "OI share")
    headers = (*headers, "Description")
    for h in headers:
        out.write(f"<th>{html.escape(h)}</th>")
    out.write("</tr></thead>\n<tbody>")
    for r in rows:
        rev = _fmt_num(r.revenue_usd_m, 0)
        share = "—" if r.share_pct is None else f"{r.share_pct * 100:.1f}%"
        desc = html.escape(r.description) if r.description else "—"
        out.write(
            f"<tr><td><strong>{html.escape(r.name)}</strong></td>"
            f'<td class="num">{rev}</td>'
            f'<td class="num">{_share_bar(r.share_pct)}{share}</td>'
        )
        if has_oi:
            oi = _fmt_num(r.operating_income_usd_m, 0)
            oi_share = "—" if r.oi_share_pct is None else f"{r.oi_share_pct * 100:.1f}%"
            out.write(f'<td class="num">{oi}</td><td class="num">{oi_share}</td>')
        out.write(f"<td>{desc}</td></tr>")
    out.write("</tbody></table></div>\n")


def _share_bar(share: float | None) -> str:
    """Tiny inline bar so the table reads at a glance.

    Width clamped to 0..100% on the share fraction. Empty span when share is
    unknown so the cell still aligns.
    """
    if share is None:
        return ""
    pct = max(0.0, min(1.0, share)) * 100.0
    return (
        f'<span class="share-bar"><span class="share-fill" '
        f'style="width:{pct:.1f}%"></span></span>'
    )


def _thesis(out: StringIO, s: ThesisSection, ticker: str) -> None:
    _section_h2(out, "thesis", 3, "Thesis & tier-1 KPIs", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    if s.stub_warning:
        out.write(
            f'<div class="callout callout-warn"><strong>Stub thesis — needs user review.</strong> '
            f"{html.escape(s.stub_warning)}</div>\n"
        )
    if s.thesis_full:
        out.write(f"<p>{html.escape(s.thesis_full)}</p>\n")
    if s.last_updated:
        out.write(f'<p class="meta">Last updated: {s.last_updated.isoformat()}</p>\n')
    if s.break_conditions:
        out.write("<h3>Break conditions</h3>\n<ul>\n")
        for bc in s.break_conditions:
            out.write(f"<li>{html.escape(bc)}</li>\n")
        out.write("</ul>\n")
    if s.qualitative_breakers:
        out.write("<h3>Qualitative thesis breakers</h3>\n<ul>\n")
        for q in s.qualitative_breakers:
            out.write(f"<li>{html.escape(q)}</li>\n")
        out.write("</ul>\n")
    if s.competitive_watchlist:
        out.write(
            f"<h3>Competitive watchlist</h3><p>{html.escape(', '.join(s.competitive_watchlist))}</p>\n"
        )
    _break_rules_block(out, s, ticker)
    if s.kpi_ledger:
        _kpi_ledger(out, s.kpi_ledger)


_BREACH_STATUS_CLASS: dict[str, str] = {
    "ok": "ok",
    "warn": "partial",
    "breach": "missing_data",
    "unknown": "not_applicable",
}

# Soft rules map to the same badge palette as hard rules so the §2 reader sees
# one consistent set of colors: green = no signal, partial (yellow) = fired.
_SOFT_STATUS_CLASS: dict[str, str] = {"green": "ok", "yellow": "partial"}

_COMPARATOR_SYMBOL: dict[str, str] = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥", "eq": "="}


def _break_rules_block(out: StringIO, s: ThesisSection, ticker: str) -> None:
    """Render break-rule evaluations split into two tables by tier.

    Tier 1 ("Catastrophic tripwires"): a narrow set of universal rules
    (revenue YoY < 0, FCF margin collapse, OCF YoY collapse) that should fire
    rarely but indicate a fundamental break. Rendered first because they're the
    backstop — if any of these are red, the rest is academic.

    Tier 2 ("Thesis breakers"): per-ticker rules calibrated to the actual unit
    economics (sub-ARR contribution margin, NIM, FRE growth, etc.). These are
    where the thesis usually breaks first.

    Empty-tier tables are suppressed entirely (the heading is also omitted) so
    holdings without per-ticker rules don't carry an empty placeholder.
    """
    if s.overall_breach_status == "unknown" and not s.break_rule_evaluations:
        out.write(
            "<h3>Break rules</h3>\n"
            f'<div class="callout"><strong>Not yet evaluated.</strong> '
            f"Run <code>python execution/run_thesis_evaluator.py --ticker {html.escape(ticker)}</code> "
            f"to populate <code>thesis_evaluations</code>.</div>\n"
        )
        return
    overall = s.overall_breach_status
    overall_class = _BREACH_STATUS_CLASS.get(overall, "not_applicable")
    out.write("<h3>Break rules</h3>\n")
    eval_when = (
        f' <span class="meta">— evaluated {html.escape(s.last_evaluated_at.isoformat(timespec="seconds"))}</span>'
        if s.last_evaluated_at
        else ""
    )
    out.write(
        f"<p><strong>Overall:</strong> "
        f'<span class="status-badge status-{overall_class}">{overall}</span>{eval_when}</p>\n'
    )
    if not s.break_rule_evaluations and not s.soft_rule_evaluations:
        return
    universal = [ev for ev in s.break_rule_evaluations if ev.tier == "universal"]
    business = [ev for ev in s.break_rule_evaluations if ev.tier == "business_model"]
    if universal:
        out.write("<h4>Catastrophic tripwires</h4>\n")
        out.write(
            '<p class="meta">Narrow universal set — should fire only on a '
            "fundamental break. Wrong calibrations were removed (GAAP op margin "
            "and net margin are SBC-distorted; capex/revenue is wrong for "
            "capex-cycle businesses).</p>\n"
        )
        _break_rule_table(out, universal)
    if business:
        out.write("<h4>Thesis breakers</h4>\n")
        out.write(
            '<p class="meta">Per-ticker rules calibrated to this business\'s '
            "unit economics — usually the first place the thesis breaks.</p>\n"
        )
        _break_rule_table(out, business)
    if s.soft_rule_evaluations:
        out.write("<h4>Soft signals</h4>\n")
        out.write(
            '<p class="meta">Predicate-style watch signals — '
            "deceleration, ratio drift, multi-period thresholds. Fire as "
            "YELLOW only (never escalate to RED on their own).</p>\n"
        )
        _soft_rule_table(out, s.soft_rule_evaluations)


def _break_rule_table(out: StringIO, evaluations: list[BreakRuleEvaluation]) -> None:
    """One table body shared by both tiers — keeps the column contract identical
    so a reader scanning across the page sees the same columns in the same order.
    """
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h_label in ("Status", "Rule", "Threshold", "Latest", "Detail"):
        out.write(f"<th>{h_label}</th>")
    out.write("</tr></thead>\n<tbody>")
    for ev in evaluations:
        out.write("<tr>")
        rule_class = _BREACH_STATUS_CLASS.get(ev.status, "not_applicable")
        out.write(f'<td><span class="status-badge status-{rule_class}">{ev.status}</span></td>')
        out.write(
            f"<td><strong>{html.escape(ev.kpi_name)}</strong>"
            f'<br><span class="meta">{html.escape(ev.narrative)}</span></td>'
        )
        comp = _COMPARATOR_SYMBOL.get(ev.comparator, ev.comparator)
        threshold_label = (
            f"{comp} {ev.threshold:g} for {ev.consecutive_periods} consecutive periods"
        )
        out.write(f"<td>{html.escape(threshold_label)}</td>")
        out.write(f"<td>{_format_observations(ev)}</td>")
        out.write(f"<td>{html.escape(ev.detail)}</td>")
        out.write("</tr>\n")
    out.write("</tbody></table></div>\n")


def _soft_rule_table(out: StringIO, evaluations: list[SoftRuleEvaluation]) -> None:
    """Three-column table: status badge, rule name, evidence string."""
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h_label in ("Status", "Rule", "Evidence"):
        out.write(f"<th>{h_label}</th>")
    out.write("</tr></thead>\n<tbody>")
    for ev in evaluations:
        out.write("<tr>")
        status_cls = _SOFT_STATUS_CLASS.get(ev.status, "not_applicable")
        out.write(
            f'<td><span class="status-badge status-{status_cls}">{ev.status}</span></td>'
        )
        out.write(f"<td><strong>{html.escape(ev.rule_name)}</strong></td>")
        out.write(f"<td>{html.escape(ev.evidence)}</td>")
        out.write("</tr>\n")
    out.write("</tbody></table></div>\n")


def _format_observations(ev: BreakRuleEvaluation) -> str:
    if not ev.observations:
        return "—"
    cells = []
    for o in ev.observations[: max(2, ev.consecutive_periods)]:
        unit = "%" if o.unit == "percent" else f" {html.escape(o.unit)}" if o.unit else ""
        cells.append(f"{html.escape(o.period_end)}: <strong>{o.value:.2f}{unit}</strong>")
    return "<br>".join(cells)


def _kpi_ledger(out: StringIO, rows: list[KpiLedgerRow]) -> None:
    tier_1 = [r for r in rows if r.tier == "tier_1"]
    lower = [r for r in rows if r.tier != "tier_1"]
    out.write("<h3>Tier-1 KPIs (thesis breakers)</h3>\n")
    out.write(
        '<p class="meta">Custom per-thesis KPIs from the holdings JSON. Status fires once '
        "<code>kpi_facts</code> rows are populated for each name "
        "(<code>extract_kpis_from_ir.py</code> / <code>derive_kpis_from_fmp.py</code>). "
        "Universal financial break rules — those that always apply — are evaluated above.</p>\n"
    )
    _kpi_table(out, tier_1)
    if lower:
        out.write(f"<details><summary>Lower-tier KPIs ({len(lower)} hidden by default)</summary>\n")
        _kpi_table(out, lower)
        out.write("</details>\n")


def _kpi_table(out: StringIO, rows: list[KpiLedgerRow]) -> None:
    out.write('<div class="table-wrap"><table class="kpi-table">\n')
    out.write(
        "<thead><tr><th>Tier</th><th>KPI</th><th>Source</th><th>Break</th><th>Status</th></tr></thead>\n<tbody>"
    )
    for r in rows:
        out.write(
            f"<tr><td>{html.escape(r.tier)}</td><td>{html.escape(r.name)}</td>"
            f"<td>{html.escape(r.source_hint or '—')}</td><td>{html.escape(r.break_condition or '—')}</td>"
            f'<td><span class="status-badge status-{_status_class(r.current_status)}">{r.current_status}</span></td></tr>\n'
        )
    out.write("</tbody></table></div>\n")


def _status_class(s: str) -> str:
    return {"green": "ok", "yellow": "partial", "red": "missing_data"}.get(s, "not_applicable")


def _financials(out: StringIO, s: FinancialsSection) -> None:
    _section_h2(out, "financials", 4, "Financials — last 12 quarters", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    # YoY% growth matrix — dense scannable view with heat shading + trailing
    # CAGR columns. Lead with this; analysts read it first.
    if s.line_items and s.quarter_labels_full and any(li.levels_full for li in s.line_items):
        _yoy_matrix(out, s)
    if s.line_items and s.chart_priorities:
        _financial_charts(
            out, s.quarter_labels, s.line_items, s.chart_priorities, list(s.kpi_chart_series)
        )
    if s.annual_line_items:
        out.write(f"<details><summary>Annual reference (last {len(s.annual_years)} FY)</summary>\n")
        _annual_table(out, s.annual_years, s.annual_line_items)
        out.write("</details>\n")


_QUARTER_LABEL_RX = re.compile(r"^(\d{4}) Q(\d)$")


def _compact_q(label: str) -> str:
    """'2025 Q3' → 'Q3'25' for axis-label density."""
    m = _QUARTER_LABEL_RX.match(label)
    if m:
        return f"Q{m.group(2)}'{m.group(1)[2:]}"
    return label


def _compute_yoy(levels: list[float | None]) -> list[float | None]:
    """YoY% per position: 100 × (curr / value_4q_ago − 1). None for insufficient history."""
    out: list[float | None] = []
    for i, v in enumerate(levels):
        prior = levels[i - 4] if i >= 4 else None
        if v is None or prior is None or prior == 0:
            out.append(None)
        else:
            out.append((v / prior - 1) * 100)
    return out


def _yoy_matrix(out: StringIO, s: FinancialsSection) -> None:
    rows = [
        MatrixRow(name=li.line_item, levels=list(li.levels_full), unit=li.unit)
        for li in s.line_items
        if li.levels_full
    ]
    if not rows:
        return
    out.write(yoy_heatmap_table(
        rows=rows,
        periods=[_compact_q(q) for q in s.quarter_labels_full],
        title="YoY % growth matrix — heat-shaded, with trailing CAGR",
        display_quarters=12,
        cagr_periods=(4, 8, 12),
    ))


def _infer_level_fmt(unit: str) -> str:
    """Map a unit hint to the BarSpec value_fmt key."""
    u = unit.lower()
    if "%" in u or "percent" in u:
        return "pct"
    if "usd" in u or "$" in u:
        return "dollar"
    return "compact"


def _financial_charts(
    out: StringIO,
    quarters: list[str],
    line_items: list[QuarterlyLineItem],
    priorities: list[str],
    kpi_series: list[object],
) -> None:
    """Render N paired charts (level + YoY%) — one per priority, stacked vertically.

    Each priority resolves first to a financials line_item, then falls back to
    a kpi_facts series (e.g. ARPAC, GMV growth, NIM). When a series has 4+
    quarters of prior history, the right panel shows YoY% bars; otherwise it
    silently falls back to a single level bar chart.
    """
    by_line_item = {li.line_item: li for li in line_items}
    by_kpi_name = {s.name: s for s in kpi_series}
    compact_labels = [_compact_q(q) for q in quarters]
    n_display = len(quarters)
    out.write('<div class="chart-grid-1col">\n')
    for name in priorities:
        if name in by_line_item:
            item = by_line_item[name]
            title = item.line_item
            full = list(item.levels_full) if item.levels_full else list(item.values)
            level_fmt = _infer_level_fmt(item.unit)
        elif name in by_kpi_name:
            ks = by_kpi_name[name]
            title = name
            full = list(getattr(ks, "levels_full", []) or getattr(ks, "values", []))
            level_fmt = _infer_level_fmt(str(getattr(ks, "unit", "")))
        else:
            continue
        display_levels = full[-n_display:]
        yoy = _compute_yoy(full)[-n_display:]
        n_level_obs = sum(1 for v in display_levels if v is not None)
        n_yoy_obs = sum(1 for v in yoy if v is not None)
        out.write('<div class="chart-cell">')
        if n_yoy_obs >= 4:
            out.write(paired_chart(
                level_values=display_levels,
                yoy_values=yoy,
                labels=compact_labels,
                title=title,
                level_fmt=level_fmt,
                panel_width=540,
                height=220,
            ))
        elif n_level_obs >= 4:
            out.write(bar_chart(BarSpec(
                values=display_levels,
                labels=compact_labels,
                title=title,
                width=1080,
                height=240,
                value_fmt=level_fmt,
                signed_color=False,
            )))
        else:
            # Too sparse for a chart — caller is likely a newly-tracked KPI
            # with only 1-3 quarters of data. Render a compact stat row.
            latest_label, latest_val = "", None
            for i in range(len(display_levels) - 1, -1, -1):
                if display_levels[i] is not None:
                    latest_label = compact_labels[i] if i < len(compact_labels) else ""
                    latest_val = display_levels[i]
                    break
            out.write(
                f'<div class="chart-empty"><strong>{html.escape(title)}</strong> '
                f'<span class="meta">— sparse series ({n_level_obs} obs). Latest '
                f'{html.escape(latest_label)}: {_fmt_num(latest_val, 2)}</span></div>'
            )
        out.write("</div>\n")
    out.write("</div>\n")


def _annual_table(out: StringIO, years: list[int], rows: list[AnnualLineItem]) -> None:
    headers = ["Line item", "Unit", *[str(y) for y in years]]
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h in headers:
        out.write(f"<th>{html.escape(h)}</th>")
    out.write("</tr></thead>\n<tbody>")
    for r in rows:
        out.write(f"<tr><td>{html.escape(r.line_item)}</td><td>{html.escape(r.unit)}</td>")
        for v in r.values:
            out.write(f'<td class="num">{_fmt_num(v, r.digits)}</td>')
        out.write("</tr>")
    out.write("</tbody></table></div>\n")


def _segments(out: StringIO, s: SegmentsSection) -> None:
    _section_h2(out, "segments", 5, "Segments — last 12 quarters", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    out.write(
        '<p class="meta">Sorted by latest-quarter magnitude, descending. '
        'Segments contributing &lt;1% of the bucket roll up into "Other".'
    )
    if s.segment_definitions and s.segment_definitions_fiscal_year:
        out.write(
            f" Hover a segment name with the <code>📖</code> mark for the 10-K "
            f"definition (FY{s.segment_definitions_fiscal_year})."
        )
    out.write("</p>\n")
    quarters_full = s.quarter_labels_full or s.quarter_labels
    for label, group, anchor in (
        ("Revenue by product", s.revenue_by_product, "rev-product"),
        ("Revenue by geography", s.revenue_by_geography, "rev-geo"),
        ("Operating income", s.operating_income, "op-income"),
        ("Capex by segment", s.capex_by_segment, "capex-seg"),
        ("Headcount by segment", s.headcount_by_segment, "headcount-seg"),
    ):
        if not group:
            continue
        _segment_bucket(out, label, anchor, quarters_full, group, s.segment_definitions)

    if s.secondary_expansions:
        _segment_secondary_expansions(out, quarters_full, s.secondary_expansions)


def _segment_secondary_expansions(
    out: StringIO,
    quarters_full: list[str],
    expansions: list[SegmentSecondaryExpansion],
) -> None:
    """Render junction-derived secondary breakdowns as collapsible subtables."""
    out.write('<h3 id="seg-secondary">Cross-tab breakdowns</h3>\n')
    out.write(
        '<p class="meta">Optional secondary-axis splits sourced from the segment '
        "junction tables (geography / channel / customer breakdowns of the rows above).</p>\n"
    )
    for exp in expansions:
        if not exp.rows:
            continue
        axis_label = exp.dim_type.replace("_", " ").title()
        parent = f" — under {html.escape(exp.parent_label)}" if exp.parent_label else ""
        out.write('<details class="segments-expander" open>\n')
        out.write(f"<summary>By {html.escape(axis_label)}{parent}</summary>\n")
        if any(r.levels_full for r in exp.rows) and quarters_full:
            matrix_rows = [
                MatrixRow(
                    name=r.segment_name,
                    levels=list(r.levels_full or r.values),
                    unit=r.unit,
                    tooltip="",
                )
                for r in exp.rows
                if (r.levels_full or any(v is not None for v in r.values))
            ]
            if matrix_rows:
                out.write(yoy_heatmap_table(
                    rows=matrix_rows,
                    periods=[_compact_q(q) for q in quarters_full],
                    title=f"By {axis_label} — YoY % with trailing CAGR",
                    display_quarters=12,
                    cagr_periods=(4, 8, 12),
                ))
        out.write("</details>\n")


_STACKED_AREA_DOMINANT_SHARE = 0.90  # skip stacked area when one segment dwarfs the rest


def _segment_bucket(
    out: StringIO,
    label: str,
    anchor: str,
    quarters_full: list[str],
    rows: list[SegmentSeries],
    definitions: dict[str, str],
) -> None:
    """One bucket: stacked-area mix-shift chart + YoY% matrix with CAGR cols."""
    out.write(f'<h3 id="seg-{anchor}">{html.escape(label)}</h3>\n')
    # Stacked area — only when 2+ segments AND no one segment dwarfs the rest.
    if len(rows) >= 2 and _top_segment_share(rows) < _STACKED_AREA_DOMINANT_SHARE:
        _segment_stacked_area(out, quarters_full, rows, label)
    # YoY% matrix — every segment as a row, full quarter history, CAGR cols.
    if any(r.levels_full for r in rows) and quarters_full:
        matrix_rows = [
            MatrixRow(
                name=r.segment_name,
                levels=list(r.levels_full),
                unit=r.unit,
                tooltip=definitions.get(r.segment_name, ""),
            )
            for r in rows
            if r.levels_full
        ]
        if matrix_rows:
            out.write(yoy_heatmap_table(
                rows=matrix_rows,
                periods=[_compact_q(q) for q in quarters_full],
                title=f"{label} — YoY % with trailing CAGR",
                display_quarters=12,
                cagr_periods=(4, 8, 12),
            ))


def _segment_stacked_area(
    out: StringIO,
    quarters_full: list[str],
    rows: list[SegmentSeries],
    label: str,
) -> None:
    """Render a stacked area chart of $ levels over the full quarter axis."""
    compact = [_compact_q(q) for q in quarters_full]
    series = [
        LineSeries(name=r.segment_name, values=list(r.levels_full))
        for r in rows
        if r.levels_full
    ]
    if not series:
        return
    out.write(stacked_area(
        series=series,
        labels=compact,
        title=f"{label} — $ levels",
        width=1080,
        height=280,
    ))


def _top_segment_share(rows: list[SegmentSeries]) -> float:
    """What fraction of the latest-quarter total does the largest segment take?"""
    latest_vals = []
    for r in rows:
        for v in reversed(r.values):
            if v is not None:
                latest_vals.append(abs(v))
                break
    if not latest_vals:
        return 0.0
    total = sum(latest_vals)
    return max(latest_vals) / total if total else 0.0


def _earnings(out: StringIO, s: EarningsSection) -> None:
    _section_h2(out, "earnings", 6, "Earnings analysis", s.status)
    # Scorecard renders BEFORE the missing-data check (see markdown renderer
    # for the rationale) — a ticker can have populated surprise data while
    # still missing LLM summaries.
    _surprise_scorecard_block(out, s.surprise_scorecard)
    _themes_block(out, s)
    if _missing_callout(out, s.status, s.missing):
        return
    out.write(
        '<p class="meta">Each quarter shows its executive-summary digest as the visible '
        "header; click to expand the full LLM analysis. Newest first. Full transcripts in §12.</p>\n"
    )
    cards = list(s.full_quarters) + list(s.digest_quarters)
    cards.sort(key=lambda c: (c.year, c.quarter), reverse=True)
    for q in cards:
        _earnings_card(out, q)


def _themes_block(out: StringIO, s: EarningsSection) -> None:
    """4Q cross-quarter theme rollup, split prepared vs Q&A.

    Skips entirely when both sides are empty AND no themes_note exists
    (offline / dev builds without --enable-llm). Each theme renders as a
    line item with a sparkline showing per-quarter mention counts plus the
    LLM-selected supporting quote(s).
    """
    has_any = bool(s.prepared_remarks_themes) or bool(s.qa_themes)
    if not has_any and not s.themes_note:
        return
    out.write('<h3>Cross-quarter themes (last 4Q)</h3>\n')
    if s.themes_note:
        out.write(f'<p class="meta">{html.escape(s.themes_note)}</p>\n')
    if s.prepared_remarks_themes:
        out.write('<h4>Prepared remarks themes</h4>\n<ul class="theme-rollup">\n')
        for theme in s.prepared_remarks_themes:
            _theme_li(out, theme)
        out.write("</ul>\n")
    if s.qa_themes:
        out.write('<h4>Q&amp;A themes</h4>\n<ul class="theme-rollup">\n')
        for theme in s.qa_themes:
            _theme_li(out, theme)
        out.write("</ul>\n")


def _theme_li(out: StringIO, theme) -> None:
    sparkline = ""
    if theme.mentions_per_quarter:
        ordered = sorted(
            theme.mentions_per_quarter.items(),
            key=lambda kv: _theme_period_sort_key(kv[0]),
        )
        parts = [
            f'<span class="theme-spark-cell">{html.escape(q)}:<strong>{n}</strong></span>'
            for q, n in ordered
        ]
        sparkline = ' <span class="theme-spark">' + " ".join(parts) + "</span>"
    out.write(
        f'<li><strong>{html.escape(theme.theme_name)}</strong> '
        f'<span class="meta">({theme.last_4q_count} mentions)</span>{sparkline}'
    )
    if theme.evidence:
        out.write('<ul class="theme-evidence">\n')
        for ev in theme.evidence:
            speaker = f"{html.escape(ev.speaker)} · " if ev.speaker else ""
            out.write(
                f'<li><em>"{html.escape(ev.text)}"</em> — '
                f'<span class="meta">{speaker}{html.escape(ev.period)}</span></li>\n'
            )
        out.write("</ul>\n")
    out.write("</li>\n")


def _theme_period_sort_key(period: str) -> tuple[int, int]:
    """Chronological sort key for 'Qx YYYY' (or 'YYYY Qx') labels."""
    import re as _re

    y_match = _re.search(r"(20\d{2})", period)
    q_match = _re.search(r"Q([1-4])", period)
    if y_match and q_match:
        return (int(y_match.group(1)), int(q_match.group(1)))
    return (9999, 9)


def _surprise_scorecard_block(out: StringIO, c: SurpriseScorecardCard | None) -> None:
    """Header table: last N quarters' EPS/Revenue beat-rate vs street.

    Renders nothing when c is None or empty. When revenue side has no source
    coverage (post-FMP-lapse), the revenue row shows '—' across the board
    with a meta note instead of zero values.
    """
    if c is None or c.total_quarters == 0:
        return
    out.write(
        f'<p class="meta"><strong>Analyst surprise — last {c.total_quarters} '
        f"reported quarters</strong></p>\n"
    )
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h in ("Metric", "Beats", "Misses", "Beat rate", "Avg surprise", "Latest"):
        out.write(f"<th>{h}</th>")
    out.write("</tr></thead>\n<tbody>\n")
    out.write(
        f"<tr><td>EPS</td>"
        f"<td>{c.eps_beats}</td><td>{c.eps_misses}</td>"
        f"<td>{_fmt_surprise_pct(c.eps_beat_rate_pct, 1)}</td>"
        f"<td>{_fmt_surprise_pct(c.eps_avg_surprise_pct, 2)}</td>"
        f"<td>{_fmt_surprise_pct(c.eps_latest_surprise_pct, 2)}</td></tr>\n"
    )
    if c.revenue_no_data >= c.total_quarters:
        out.write(
            "<tr><td>Revenue</td><td>—</td><td>—</td><td>—</td><td>—</td>"
            '<td>— <span class="meta">(source coverage absent)</span></td></tr>\n'
        )
    else:
        out.write(
            f"<tr><td>Revenue</td>"
            f"<td>{c.revenue_beats}</td><td>{c.revenue_misses}</td>"
            f"<td>{_fmt_surprise_pct(c.revenue_beat_rate_pct, 1)}</td>"
            f"<td>{_fmt_surprise_pct(c.revenue_avg_surprise_pct, 2)}</td>"
            f"<td>{_fmt_surprise_pct(c.revenue_latest_surprise_pct, 2)}</td></tr>\n"
        )
    out.write("</tbody></table></div>\n")


def _fmt_surprise_pct(v: float | None, places: int) -> str:
    """Sign-aware percentage formatter. None → '—'. Positive values get '+'
    so beats and misses read at a glance."""
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{places}f}%"


def _earnings_card(out: StringIO, q: QuarterlyEarningsCard) -> None:
    """Uniform card: digest is always visible inside <summary>, full body expands."""
    digest_html = _md_block(q.digest_md) if q.digest_md else ""
    summary_html = (
        _md_block(q.summary_md) if q.summary_md else "<p><em>No LLM summary cached.</em></p>"
    )
    transcript_link = (
        f'<p class="meta">Transcript source: <code>{html.escape(q.transcript_path)}</code></p>'
        if q.transcript_path
        else ""
    )
    open_attr = " open" if q.is_recent else ""
    out.write(f'<details class="earnings-card"{open_attr}>\n')
    out.write(f'<summary><span class="earnings-title">{html.escape(q.quarter)} {q.year}</span>')
    if digest_html:
        out.write(f'<div class="earnings-digest">{digest_html}</div>')
    out.write("</summary>\n")
    out.write(f'<div class="earnings-full">{summary_html}{transcript_link}</div>\n')
    out.write("</details>\n")


def _saydo(out: StringIO, s: SayDoSection) -> None:
    _section_h2(out, "saydo", 7, "Say-Do analysis", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    if not s.cards:
        return

    # Summary table — quantitative grade per pair, visible by default.
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h in ("Pair", "Rating", "Attribution", "Thesis view"):
        out.write(f"<th>{h}</th>")
    out.write("</tr></thead>\n<tbody>")
    for c in s.cards:
        title = f"{c.current_quarter} {c.current_year} vs {c.prior_quarter} {c.prior_year}"
        out.write(
            f"<tr><td>{html.escape(title)}</td>"
            f'<td><span class="status-badge status-{_rating_class(c.rating)}">{c.rating}</span></td>'
            f"<td>{html.escape(c.attribution or '—')}</td>"
            f"<td>{html.escape(c.thesis_view or '—')}</td></tr>\n"
        )
    out.write("</tbody></table></div>\n")

    # Per-pair detail — collapsed by default.
    out.write('<p class="meta">Per-pair detail (click to expand):</p>\n')
    for c in s.cards:
        title = f"{c.current_quarter} {c.current_year} vs {c.prior_quarter} {c.prior_year}"
        out.write(
            f"<details><summary>{html.escape(title)} "
            f'<span class="meta">— {c.rating}</span></summary>\n{_md_block(c.saydo_md)}</details>\n'
        )


def _rating_class(rating: str) -> str:
    return {
        "EXCEEDED": "ok",
        "MET": "ok",
        "MIXED": "partial",
        "MISSED": "missing_data",
    }.get(rating, "not_applicable")


def _ir_docs(out: StringIO, s: IrDocsSection) -> None:
    _section_h2(out, "ir-docs", 8, "IR documents", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    for c in s.cards:
        out.write(f"<h3>{html.escape(c.quarter)} {c.year} — {html.escape(c.doc_type)}</h3>\n")
        if c.source_url:
            out.write(
                f'<p class="meta">Source: <a href="{html.escape(c.source_url)}">{html.escape(c.source_url)}</a></p>\n'
            )
        if c.summary_md:
            out.write(_md_block(c.summary_md))


def _recent_developments(out: StringIO, s: RecentDevelopmentsSection) -> None:
    _section_h2(out, "recent-developments", 9, "Recent developments", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    if s.cached_at is not None:
        out.write(
            f'<p class="meta">Window: last {s.news_days_window} days. '
            f"Cached at {html.escape(s.cached_at.isoformat(timespec='seconds'))}.</p>\n"
        )
    if s.content_md:
        out.write(_md_block(s.content_md))
    else:
        out.write("<p><em>No content available.</em></p>\n")


def _bear_case(out: StringIO, s: BearCaseSection) -> None:
    _section_h2(out, "bear-case", 10, "Bear case", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    for i, fm in enumerate(s.failure_modes, 1):
        out.write(f"<h3>Failure mode {i}: {html.escape(fm.hypothesis)}</h3>\n<ul>")
        out.write(f"<li><strong>Evidence in data:</strong> {html.escape(fm.evidence_in_data)}</li>")
        out.write(
            f"<li><strong>Leading indicator:</strong> {html.escape(fm.leading_indicator)}</li>"
        )
        out.write(
            f"<li><strong>Quantitative impact:</strong> {html.escape(fm.quantitative_impact)}</li>"
        )
        out.write(
            f"<li><strong>Refutation:</strong> {html.escape(fm.refutation_criteria)}</li></ul>\n"
        )
    if s.most_underweighted:
        out.write(
            f"<h3>Most underweighted by consensus</h3><p>{html.escape(s.most_underweighted)}</p>\n"
        )
    if s.out_of_scope_flags:
        out.write("<h3>Flagged for manual review</h3>\n<ul>")
        for f in s.out_of_scope_flags:
            out.write(f"<li>{html.escape(f)}</li>")
        out.write("</ul>\n")


def _provenance(out: StringIO, s: ProvenanceSection) -> None:
    _section_h2(out, "provenance", 11, "Provenance & data quality", s.status)
    if _missing_callout(out, s.status, s.missing):
        return
    if s.coverage:
        out.write("<h3>Coverage matrix</h3>\n")
        out.write('<div class="table-wrap"><table>\n<thead><tr>')
        for h in ("Quarter", "Audio", "Transcript", "Release", "Slides", "Say-Do", "LLM summary"):
            out.write(f"<th>{h}</th>")
        out.write("</tr></thead>\n<tbody>")
        for c in s.coverage:
            out.write(
                f"<tr><td>{c.quarter} {c.year}</td>"
                f"<td>{_chk(c.has_audio_file)}</td><td>{_chk(c.has_transcript_file)}</td>"
                f"<td>{_chk(c.has_release_file)}</td><td>{_chk(c.has_slides_file)}</td>"
                f"<td>{_chk(c.step_saydo_analyzed)}</td><td>{_chk(c.step_llm_summarized)}</td></tr>"
            )
        out.write("</tbody></table></div>\n")
    out.write(f"<p>Open validation issues: <strong>{s.open_validation_issues}</strong></p>\n")
    out.write(
        f'<p class="meta">Source documents in workbook (Provenance tab): {len(s.source_docs)}.</p>\n'
    )


def _chk(b: bool) -> str:
    return "✅" if b else "—"


def _appendix(out: StringIO, s: AppendixSection) -> None:
    _section_h2(out, "appendix", 12, "Appendix — full earnings-call transcripts", s.status)
    if s.status != SectionStatus.OK or not s.transcripts:
        out.write("<p>No transcripts available.</p>\n")
        return
    out.write('<p class="meta">Click each quarter to expand the raw transcript text.</p>\n')
    for entry in s.transcripts:
        _transcript_block(out, entry)


def _transcript_block(out: StringIO, entry: TranscriptEntry) -> None:
    char_count = f"{len(entry.text):,} chars"
    out.write(
        f"<details><summary>{html.escape(entry.quarter)} {entry.year}"
        f' <span class="meta">— {char_count}, source <code>{html.escape(entry.source_path)}</code></span>'
        f'</summary>\n<pre class="transcript">{html.escape(entry.text)}</pre>\n</details>\n'
    )
