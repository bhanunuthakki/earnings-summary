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
    SegmentSeries,
    SegmentsSection,
    SegmentWeighting,
    SnapshotSection,
    ThesisSection,
    TranscriptEntry,
    ValuationSnapshot,
)
from report.renderers.charts import CHART_CSS, sparkline
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.charts_v2 import (
    BarSpec,
    MatrixRow,
    bar_chart,
    paired_chart,
    yoy_heatmap_table,
)

_BOLD_RX = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RX = re.compile(r"`([^`]+)`")


def render(spec: ReportSpec) -> str:
    body = StringIO()
    _nav(body)
    _header(body, spec)
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
"""
    + CHART_CSS
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
    out.write('<div class="table-wrap"><table class="weighting-table">\n<thead><tr>')
    for h in (label, "Revenue (USD M)", "Share", "Description"):
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
            f"<td>{desc}</td></tr>"
        )
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

_COMPARATOR_SYMBOL: dict[str, str] = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥", "eq": "="}


def _break_rules_block(out: StringIO, s: ThesisSection, ticker: str) -> None:
    """Render the deterministic universal break-rules from thesis_evaluations."""
    if s.overall_breach_status == "unknown" and not s.break_rule_evaluations:
        out.write(
            "<h3>Universal break rules</h3>\n"
            f'<div class="callout"><strong>Not yet evaluated.</strong> '
            f"Run <code>python execution/run_thesis_evaluator.py --ticker {html.escape(ticker)}</code> "
            f"to populate <code>thesis_evaluations</code>.</div>\n"
        )
        return
    overall = s.overall_breach_status
    overall_class = _BREACH_STATUS_CLASS.get(overall, "not_applicable")
    out.write("<h3>Universal break rules</h3>\n")
    eval_when = (
        f' <span class="meta">— evaluated {html.escape(s.last_evaluated_at.isoformat(timespec="seconds"))}</span>'
        if s.last_evaluated_at
        else ""
    )
    out.write(
        f"<p><strong>Overall:</strong> "
        f'<span class="status-badge status-{overall_class}">{overall}</span>{eval_when}</p>\n'
    )
    if not s.break_rule_evaluations:
        return
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h_label in ("Status", "Rule", "Threshold", "Latest", "Detail"):
        out.write(f"<th>{h_label}</th>")
    out.write("</tr></thead>\n<tbody>")
    for ev in s.break_rule_evaluations:
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
        'Segments contributing &lt;1% of the bucket roll up into "Other". '
        "Click each bucket to expand the table; the snapshot panel is always visible."
    )
    if s.segment_definitions and s.segment_definitions_fiscal_year:
        out.write(
            f" Hover a segment name with the <code>📖</code> mark for the 10-K "
            f"definition (FY{s.segment_definitions_fiscal_year})."
        )
    out.write("</p>\n")
    for label, group, anchor in (
        ("Revenue by product", s.revenue_by_product, "rev-product"),
        ("Revenue by geography", s.revenue_by_geography, "rev-geo"),
        ("Operating income", s.operating_income, "op-income"),
    ):
        if not group:
            continue
        _segment_bucket(out, label, anchor, s.quarter_labels, group, s.segment_definitions)


def _segment_bucket(
    out: StringIO,
    label: str,
    anchor: str,
    quarters: list[str],
    rows: list[SegmentSeries],
    definitions: dict[str, str],
) -> None:
    """One bucket: snapshot panel (visible) + full table (in <details>)."""
    out.write(f'<h3 id="seg-{anchor}">{html.escape(label)}</h3>\n')
    _segment_snapshot_panel(out, quarters, rows, definitions)
    out.write(
        f'<details class="segment-details"><summary>Full quarterly table — {len(rows)} segments</summary>\n'
    )
    _segments_table(out, quarters, rows, definitions)
    out.write("</details>\n")


def _segment_snapshot_panel(
    out: StringIO,
    quarters: list[str],
    rows: list[SegmentSeries],
    definitions: dict[str, str],
) -> None:
    """Top-3 (by latest magnitude) cards + sparkline-per-segment summary list."""
    latest_label = quarters[-1] if quarters else ""
    out.write('<div class="seg-snapshot">')
    for r in rows[:3]:
        latest = _last_non_null(r.values)
        share_pct = _segment_share(r, rows)
        latest_str = _fmt_num(latest, 0)
        share_str = f"{share_pct * 100:.1f}%" if share_pct is not None else "—"
        yoy_str = _fmt_pct(r.growth.yoy)
        spark = sparkline(r.values, width=140, height=32)
        out.write(
            f'<div class="seg-card">'
            f'<div class="seg-card-name">{_segment_name_with_def(r.segment_name, definitions)}</div>'
            f'<div class="seg-card-spark">{spark}</div>'
            f'<div class="seg-card-row"><span>{html.escape(latest_label)}</span>'
            f"<strong>{latest_str}</strong></div>"
            f'<div class="seg-card-row"><span>Share</span><strong>{share_str}</strong></div>'
            f'<div class="seg-card-row"><span>YoY</span><strong>{yoy_str}</strong></div>'
        )
        definition = definitions.get(r.segment_name)
        if definition:
            out.write(
                f'<details class="seg-card-def"><summary>10-K definition</summary>'
                f"<p>{html.escape(definition)}</p></details>"
            )
        out.write("</div>")
    out.write("</div>\n")


def _segment_name_with_def(name: str, definitions: dict[str, str]) -> str:
    """Append a 📖 hover-mark when a definition exists. Tooltip via title attr."""
    if name in definitions:
        return (
            f'<span title="{html.escape(definitions[name])}">{html.escape(name)} '
            f'<span class="seg-def-mark">📖</span></span>'
        )
    return html.escape(name)


def _segment_share(r: SegmentSeries, rows: list[SegmentSeries]) -> float | None:
    latest = _last_non_null(r.values)
    if latest is None:
        return None
    total = sum(abs(_last_non_null(s.values) or 0.0) for s in rows)
    if total == 0:
        return None
    return abs(latest) / total


def _last_non_null(values: list[float | None]) -> float | None:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _segments_table(
    out: StringIO,
    quarters: list[str],
    rows: list[SegmentSeries],
    definitions: dict[str, str],
) -> None:
    headers = ["Segment", "Trend", *quarters, "QoQ", "YoY", "1Y CAGR", "3Y CAGR"]
    out.write('<div class="table-wrap"><table>\n<thead><tr>')
    for h in headers:
        out.write(f"<th>{html.escape(h)}</th>")
    out.write("</tr></thead>\n<tbody>")
    for r in rows:
        out.write(
            f"<tr><td>{_segment_name_with_def(r.segment_name, definitions)}</td>"
            f"<td>{sparkline(r.values, width=100, height=24)}</td>"
        )
        for v in r.values:
            out.write(f'<td class="num">{_fmt_num(v, 0)}</td>')
        for v in (r.growth.qoq, r.growth.yoy, r.growth.cagr_1y_ttm, r.growth.cagr_3y_ttm):
            out.write(f'<td class="num">{_fmt_pct(v)}</td>')
        out.write("</tr>")
    out.write("</tbody></table></div>\n")


def _earnings(out: StringIO, s: EarningsSection) -> None:
    _section_h2(out, "earnings", 6, "Earnings analysis", s.status)
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
