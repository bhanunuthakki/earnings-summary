"""Page chrome: identity strip, thesis lede, open items, KPI strip, news tab, footer.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import (
    BudgetSkip,
    KpiLedgerRow,
    RecentDevelopmentsSection,
    ReportSpec,
    SnapshotSection,
    ThesisSection,
)
from report.renderers.numfmt import fmt_date, fmt_reltime
from report.renderers.workspace_charts import sparkline
from report.renderers.workspace_data import (
    KpiStripTile,
    NewsTile,
    select_kpi_strip,
    structure_news_by_section,
)
from report.renderers.workspace_sections._shared import (
    _esc,
    _fmt_price,
    _missing_panel,
    _panel_head,
    _render_markdown,
)
from user_state.notes import AnalystNoteRow

__all__ = [
    "_footer",
    "_forgone_strip",
    "_identity",
    "_kpi_strip",
    "_kpi_tile",
    "_news_tab",
    "_news_tile",
    "_open_items_strip",
    "_thesis_strip",
    "_val_stat",
    "_verdict_badge",
]


def _identity(body: StringIO, spec: ReportSpec) -> None:
    snap = spec.snapshot
    val = snap.valuation
    body.write('<div class="l1-identity">')
    body.write('<div class="identity-left">')
    body.write(f'<div class="ticker-large">{_esc(spec.ticker)}</div>')
    body.write('<div class="company-row">')
    body.write(f'<span class="company-name">{_esc(snap.company_name or spec.ticker)}</span>')
    body.write(_verdict_badge(snap.verdict))
    body.write("</div>")
    body.write('<div class="company-meta">')
    body.write(f"<span>USD · Report dated {fmt_date(spec.generation_date.isoformat())}</span>")
    if val.valuation_date is not None:
        body.write('<span class="meta-pip">·</span>')
        body.write(f"<span>DCF dated {fmt_date(val.valuation_date.isoformat())}</span>")
    body.write("</div></div>")  # /identity-left

    # Valuation strip on the right.
    body.write('<div class="identity-right">')
    _val_stat(body, "Last price", _fmt_price(val.current_price))
    body.write('<div class="val-divider"></div>')
    _val_stat(body, "DCF / share", _fmt_price(val.consolidated_npv_per_share))
    body.write('<div class="val-divider"></div>')
    if val.current_price and val.consolidated_npv_per_share:
        upside = (val.consolidated_npv_per_share - val.current_price) / val.current_price * 100
        tone = "pos" if upside >= 0 else "neg"
        _val_stat(body, "Implied", f"{upside:+.0f}%", tone=tone)
    else:
        _val_stat(body, "Implied", "—")
    if val.mos_bar is not None:
        body.write('<div class="val-divider"></div>')
        _val_stat(body, "MoS bar", f"{val.mos_bar * 100:.0f}%", mono_sm=True)
    if val.trigger_status and val.trigger_status != "unknown":
        body.write('<div class="val-divider"></div>')
        _val_stat(body, "Trigger", val.trigger_status.upper(), mono_sm=True)
    body.write("</div></div>")  # /identity-right, /l1-identity


def _val_stat(
    body: StringIO,
    label: str,
    value: str,
    *,
    tone: str | None = None,
    mono_sm: bool = False,
) -> None:
    cls = "val-stat-value"
    if mono_sm:
        cls += " mono-sm"
    if tone:
        cls += f" {tone}"
    body.write('<div class="val-stat">')
    body.write(f'<div class="val-stat-label">{_esc(label)}</div>')
    body.write(f'<div class="{cls}">{_esc(value)}</div>')
    body.write("</div>")


def _verdict_badge(verdict: str) -> str:
    mapping = {
        "intact": ("Thesis Intact", "var(--ok)"),
        "watch": ("Watch", "var(--warn)"),
        "broken": ("Broken", "var(--bad)"),
        "pending": ("Pending", "var(--muted-2)"),
    }
    label, dot = mapping.get(verdict, mapping["pending"])
    return (
        f'<span class="badge"><span class="dot" style="background:{dot}"></span>'
        f"{_esc(label)}</span>"
    )


def _thesis_strip(body: StringIO, snap: SnapshotSection, thesis: ThesisSection) -> None:
    text = snap.thesis_one_liner or thesis.thesis_full
    if not text:
        return
    body.write(
        '<div class="l1-thesis" data-commentable="true" '
        'data-anchor-type="thesis_lede" data-anchor-key="thesis_lede" '
        'data-anchor-tab="thesis">'
    )
    body.write('<span class="thesis-label">Thesis</span>')
    body.write(f"<p>{_esc(text)}</p>")
    body.write("</div>")


def _open_items_strip(body: StringIO, notes: list[AnalystNoteRow]) -> None:
    """The owner's open watch-items lead every build (P4.4) — a compact
    expanded-by-default strip under the thesis lede, fed by analyst_notes
    at render time. Hidden entirely when the journal has nothing open."""
    if not notes:
        return
    body.write(
        '<details class="panel l1-open-items" open><summary class="panel-head">'
        '<span class="panel-title">Open watch-items</span>'
        '<span class="panel-meta"><span class="panel-sub">'
        f"{len(notes)} open · from the analyst journal</span></span></summary>"
        '<ul class="oi-strip-list">'
    )
    for n in notes:
        when = _esc(n.created_at.date().isoformat())
        body.write(
            f'<li><span class="oi-kind">{_esc(n.kind)}</span>'
            f'<span class="oi-body">{_esc(n.body)}</span>'
            f'<span class="oi-when">{when}</span></li>'
        )
    body.write("</ul></details>")


def _kpi_strip(body: StringIO, rows: list[KpiLedgerRow]) -> None:
    tiles = select_kpi_strip(rows, n=4)
    if not tiles:
        return
    body.write('<div class="kpi-strip">')
    for t in tiles:
        _kpi_tile(body, t)
    # Pad with empty cells to keep the 4-up grid when fewer tiles available.
    for _ in range(4 - len(tiles)):
        body.write('<div class="kpi-tile" aria-hidden="true"></div>')
    body.write("</div>")


def _kpi_tile(body: StringIO, t: KpiStripTile) -> None:
    body.write('<div class="kpi-tile">')
    body.write(f'<div class="kpi-name">{_esc(t.name)}</div>')
    body.write('<div class="kpi-row">')
    body.write(f'<div class="kpi-value">{_esc(t.latest_display)}</div>')
    if t.delta_display:
        cls = f"kpi-delta {t.delta_sign}"
        body.write(f'<div class="{cls}">{_esc(t.delta_display)}</div>')
    body.write("</div>")
    body.write(f'<div class="kpi-spark">{sparkline(t.values, width=230, height=36)}</div>')
    body.write('<div class="kpi-axis">')
    body.write(f"<span>{_esc(t.labels[0][2:7])}</span>")
    body.write(f'<span class="kpi-trail">{len(t.values)}q trailing</span>')
    body.write(f"<span>{_esc(t.labels[-1][2:7])}</span>")
    body.write("</div></div>")


def _news_tab(body: StringIO, section: RecentDevelopmentsSection) -> None:
    """News tab — splits the recent-developments markdown into its source
    sections (Material events / Sector & regulatory context / Watch this
    week / …) and renders each as its own panel of tone-coded tiles.

    Falls back to a stub + raw markdown when the section is empty or the
    parser can't extract bullets in the expected shape.
    """
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    eyebrow_bits = ["News & market context"]
    if section.cached_at is not None:
        eyebrow_bits.append(f"cached {fmt_reltime(section.cached_at.isoformat())}")
    body.write(f'<div class="eyebrow">{_esc(" · ".join(eyebrow_bits))}</div>')
    by_section = structure_news_by_section(section, limit_per_section=12)
    total = sum(len(v) for v in by_section.values())
    title = f"{total} item{'s' if total != 1 else ''}"
    if section.news_days_window:
        title += f" · last {section.news_days_window} days"
    body.write(f'<h2 class="section-title">{_esc(title)}</h2>')
    body.write("</div></div>")
    if not by_section:
        _missing_panel(body, section.status, section.missing, title="News & market context")
        if section.content_md:
            body.write(_panel_head("News brief", sub="full text"))
            body.write(f'<div class="prose-pad">{_render_markdown(section.content_md)}</div>')
            body.write("</div>")
        body.write("</div>")
        return
    for sec_title, tiles in by_section.items():
        body.write(
            _panel_head(sec_title, sub=f"{len(tiles)} item{'s' if len(tiles) != 1 else ''}")
            + '<div class="news-grid news-grid-tab">'
        )
        for t in tiles:
            _news_tile(body, t)
        body.write("</div></div>")
    body.write("</div>")


def _news_tile(body: StringIO, t: NewsTile) -> None:
    anchor_key = _esc(t.headline[:80])
    body.write(
        f'<article class="news-item tone-{_esc(t.tone)}" '
        f'data-commentable="true" data-anchor-type="news_item" '
        f'data-anchor-key="{anchor_key}" data-anchor-tab="news">'
    )
    body.write('<div class="news-meta">')
    if t.tag:
        body.write(f'<span class="news-tag">{_esc(t.tag)}</span>')
    if t.date:
        body.write(f'<span class="news-date mono">{_esc(t.date)}</span>')
    if t.source:
        body.write(f'<span class="news-src">{_esc(t.source)}</span>')
    body.write("</div>")
    headline = _esc(t.headline)
    if t.url:
        headline = f'<a href="{_esc(t.url)}" target="_blank" rel="noopener">{headline}</a>'
    body.write(f'<h4 class="news-headline">{headline}</h4>')
    if t.gloss:
        body.write(f'<p class="news-gloss">{_esc(t.gloss)}</p>')
    body.write("</article>")


def _footer(body: StringIO, spec: ReportSpec) -> None:
    body.write('<div class="l1-footer">')
    body.write(
        f'<div class="footer-left">'
        f"<span>Research package · {_esc(spec.ticker)} · {spec.generation_date.isoformat()}</span>"
        "</div>"
    )
    body.write(
        '<div class="footer-mid">'
        '<span class="muted">DCF · earnings transcripts · Say-Do · KPI ledger · break rules</span>'
        "</div>"
    )
    body.write(
        '<div class="footer-right">'
        '<span class="muted mono">renderer · workspace · v0.1</span>'
        "</div>"
    )
    body.write("</div>")


def _forgone_strip(body: StringIO, forgone: list[BudgetSkip]) -> None:
    """Brief-wide banner listing the analyses forgone to stay under budget."""
    if not forgone:
        return
    n = len(forgone)
    word = "analysis" if n == 1 else "analyses"
    names = _esc(", ".join(f.section for f in forgone))
    body.write(
        '<div class="forgone-strip k-well k-well-warn" style="margin:8px 0;">'
        f"⏭ <strong>{n} {word} forgone to stay under budget:</strong> {names}. "
        "Raise the cap or override, then rebuild."
        "</div>"
    )
