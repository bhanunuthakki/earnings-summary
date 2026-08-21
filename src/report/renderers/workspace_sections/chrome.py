"""Page chrome: identity strip, thesis lede, open items, KPI strip, news tab, footer.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

from report.models import (
    BudgetSkip,
    FinancialsSection,
    InvestmentDecisionCardSection,
    KpiLedgerRow,
    RecentDevelopmentsSection,
    ReportSpec,
    SnapshotSection,
    SynthesisSection,
    ThesisSection,
)
from report.renderers.numfmt import fmt_date, fmt_reltime
from report.renderers.workspace_charts import SparklineSize, sparkline
from report.renderers.workspace_data import (
    KpiStripTile,
    NewsTile,
    select_kpi_strip,
    structure_news_by_section,
)
from report.renderers.workspace_sections._shared import (
    _esc,
    _fmt_price,
    _inline_md,
    _missing_panel,
    _panel_head,
    _render_markdown,
    _xlink_html,
)
from report.renderers.workspace_sections.earnings import _ws_period_sort_key
from report.renderers.workspace_sections.synthesis import _LENS_LABELS
from report.sections.p3_data import SayDoVerdictRow
from ui.controls import fact_anchor_attrs, pill_tone_class
from user_state.notes import AnalystNoteRow

__all__ = [
    "_decision_card_strip",
    "_footer",
    "_forgone_strip",
    "_identity",
    "_kpi_strip",
    "_kpi_tile",
    "_newest_quarter_ingested_at",
    "_news_tab",
    "_news_tile",
    "_open_items_strip",
    "_reread_strip",
    "_thesis_strip",
    "_val_stat",
    "_verdict_badge",
]

# suggested_disposition -> which verb button gets the primary treatment;
# every other verb renders quiet. All four verbs always render — the card's
# suggestion only styles the row, it never hides a verb (PRD §8.1: the owner
# picks; the model suggests).
_DISPOSITION_VERBS: tuple[tuple[str, str], ...] = (
    ("promote", "Promote"),
    ("watch", "Watch"),
    ("research_further", "Research further"),
    ("pass", "Pass"),
)


def _identity(
    body: StringIO,
    spec: ReportSpec,
    saydo_verdicts: list[SayDoVerdictRow] | None = None,
) -> None:
    snap = spec.snapshot
    val = snap.valuation
    body.write('<div class="l1-identity">')
    body.write('<div class="identity-left">')
    body.write(f'<div class="ticker-large">{_esc(spec.ticker)}</div>')
    body.write('<div class="company-row">')
    body.write(f'<span class="company-name">{_esc(snap.company_name or spec.ticker)}</span>')
    newest_quarter = spec.financials.quarter_labels[-1] if spec.financials.quarter_labels else None
    body.write(
        _verdict_badge(
            snap.verdict,
            snap.verdict_as_of,
            newest_quarter,
            _newest_quarter_ingested_at(spec.financials),
        )
    )
    body.write("</div>")
    body.write('<div class="company-meta">')
    body.write(f"<span>USD · Report dated {fmt_date(spec.generation_date.isoformat())}</span>")
    if val.valuation_date is not None:
        body.write('<span class="meta-pip">·</span>')
        body.write(f"<span>DCF dated {fmt_date(val.valuation_date.isoformat())}</span>")
    score_marker = _saydo_score_marker(saydo_verdicts or [])
    if score_marker:
        body.write('<span class="meta-pip">·</span>')
        body.write(score_marker)
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


def _saydo_score_marker(rows: list[SayDoVerdictRow]) -> str:
    """Compact Company Brief marker from the already-loaded verdict ledger."""
    graded = [row for row in rows if row.outcome]
    if not graded:
        return ""
    met_or_exceeded = sum(1 for row in graded if str(row.outcome).casefold() in {"met", "exceeded"})
    percent = round(100 * met_or_exceeded / len(graded))
    noun = "KPI" if len(graded) == 1 else "KPIs"
    label = f"{percent}% met/exceeded · {len(graded)} reported {noun}"
    return f'<span class="k-pill">{_esc(label)}</span>'


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


_QUARTER_END_MONTH_DAY = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _quarter_label_end_date(label: str) -> datetime | None:
    """Approximate calendar quarter-end for a 'Qx YYYY' / 'YYYY Qx' label, or
    None when the label doesn't parse (``_ws_period_sort_key`` returns its
    (9999, 9) sentinel in that case)."""
    year, q = _ws_period_sort_key(label)
    month_day = _QUARTER_END_MONTH_DAY.get(q)
    if year == 9999 or month_day is None:
        return None
    month, day = month_day
    return datetime(year, month, day)


def _newest_quarter_ingested_at(financials: FinancialsSection) -> datetime | None:
    """When the newest quarter's data actually LANDED in this build — the max
    ``documents.fetched_at`` across the levels table's newest-quarter cells
    (``QuarterlyLineItem.sources_full`` aligns to ``levels_full``, whose last
    entry is the newest quarter on the shared axis). None when no cell carries
    dated provenance (older built reports, legacy DBs) — the badge then falls
    back to the calendar quarter-end proxy."""
    newest: datetime | None = None
    for li in financials.line_items:
        if not li.sources_full or len(li.sources_full) != len(li.levels_full):
            continue
        src = li.sources_full[-1]
        if src is None or not src.fetched_at:
            continue
        try:
            stamp = datetime.fromisoformat(src.fetched_at)
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            # Naive-UTC convention (aware stamps crash comparisons downstream).
            stamp = stamp.astimezone(UTC).replace(tzinfo=None)
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def _verdict_badge(
    verdict: str,
    verdict_as_of: datetime | None = None,
    newest_quarter_label: str | None = None,
    newest_ingested_at: datetime | None = None,
) -> str:
    """Thesis-verdict badge, dated + honest-grey when stale.

    ``verdict_as_of`` is ``thesis_state.last_updated`` (persist_verdict writes
    ``verdict.evaluated_at`` there). When it predates the newest quarter's
    print this build has data for, the evaluation is stale — the badge renders
    the 'pending' muted dot instead of the verdict's own tone so a stale read
    never reads as fresh green (honest-grey, never fake-green), while the
    label text stays the actual verdict (still informative, just not implied
    current).

    Staleness compares against ``newest_ingested_at`` — when the newest
    quarter's facts were ingested (:func:`_newest_quarter_ingested_at`) — not
    the calendar quarter-end. Prints land 4-7 weeks after quarter end, so a
    verdict evaluated in that gap (after Mar-31, before the May print) has NOT
    seen the newest quarter and must grey; the calendar proxy would render it
    fake-fresh in exactly the window a new print most often flips a thesis.
    The proxy remains only as the fallback when no ingestion date is known.
    """
    mapping = {
        "intact": ("Thesis Intact", "ok"),
        "watch": ("Watch", "warn"),
        "broken": ("Broken", "bad"),
        "pending": ("Pending", "muted"),
    }
    label, dot = mapping.get(verdict, mapping["pending"])
    is_stale = False
    if verdict_as_of is not None:
        if newest_ingested_at is not None:
            is_stale = verdict_as_of < newest_ingested_at
        elif newest_quarter_label is not None:
            newest_end = _quarter_label_end_date(newest_quarter_label)
            if newest_end is not None and verdict_as_of < newest_end:
                is_stale = True
    if is_stale:
        dot = mapping["pending"][1]
    as_of_html = ""
    title_attr = ""
    if verdict_as_of is not None:
        as_of_display = verdict_as_of.strftime("%m-%d")
        as_of_html = f' <span class="verdict-asof muted">as of {_esc(as_of_display)}</span>'
        title_bits = [f"Evaluated {verdict_as_of.date().isoformat()}"]
        if is_stale:
            quarter_phrase = f"the {newest_quarter_label}" if newest_quarter_label else "the newest"
            stale_bit = f"predates {quarter_phrase} print"
            if newest_ingested_at is not None:
                stale_bit += f" (data ingested {newest_ingested_at.date().isoformat()})"
            title_bits.append(stale_bit)
        title_attr = f' title="{_esc(" — ".join(title_bits))}"'
    tone_cls = "" if is_stale or verdict == "pending" else f" k-pill-{dot}"
    return (
        f'<span class="k-pill{tone_cls}"{title_attr}>'
        f'<span class="k-dot k-dot-{dot}" aria-hidden="true"></span>'
        f"{_esc(label)}{as_of_html}</span>"
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


def _first_reread_line(content_md: str) -> str | None:
    """The first substantive paragraph/line of a lens's markdown body.

    Skips blank lines and pure markdown headings (``## 1. What changed``) so
    the strip leads with real content rather than a section title; a bullet
    marker is stripped so the line reads as prose. None when nothing
    substantive is found (the caller hides the strip in that case)."""
    for raw_line in content_md.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        return line.lstrip("-*").strip()
    return None


def _decision_card_strip(
    body: StringIO, card: InvestmentDecisionCardSection | None, *, ticker: str
) -> None:
    """The Investment Decision Card strip (PRD §8.1, P1.1) — renders before
    the tab bar, between the KPI strip and l1-tabs-wrap. Hidden entirely (not
    stubbed) when no card has been generated for this ticker yet; that build
    ran the LLM leg and hit a hard stop, or the artifact failed to decode
    (report.sections.investment_decision_card.build already degrades all
    three to ``None``)."""
    if card is None:
        return
    ready = bool(card.evidence_readiness and card.evidence_readiness.decision_ready)
    ready_tone = "ok" if ready else "warn"
    body.write('<div class="l1-decision-card" data-anchor-type="decision_card">')
    body.write('<div class="dc-head">')
    body.write('<span class="dc-label">Investment Decision Card</span>')
    body.write(
        f'<span class="k-pill{pill_tone_class(ready_tone)}">'
        f"{'Decision-ready' if ready else 'Not decision-ready'}</span>"
    )
    if card.dirty:
        body.write('<span class="k-pill k-pill-warn">stale — inputs changed</span>')
    elif card.is_stale:
        body.write('<span class="k-pill">aging</span>')
    if card.suggested_disposition:
        body.write(
            f'<span class="dc-suggested">suggests: {_esc(card.suggested_disposition)}</span>'
        )
    body.write("</div>")  # /dc-head

    body.write('<div class="dc-grid">')
    if card.company_hypothesis:
        body.write(
            '<div class="dc-cell"><h4>Company hypothesis</h4>'
            f"<p>{_esc(card.company_hypothesis.directional_thesis) or '—'}</p></div>"
        )
    if card.security_setup:
        body.write(
            '<div class="dc-cell"><h4>Security setup</h4>'
            f"<p>{_esc(card.security_setup.appears_priced_in) or '—'}</p></div>"
        )
    if card.portfolio_fit:
        body.write(
            '<div class="dc-cell"><h4>Portfolio fit</h4>'
            f"<p>{_esc(card.portfolio_fit.expected_role) or '—'}</p></div>"
        )
    if card.disconfirming_case:
        body.write(
            '<div class="dc-cell"><h4>Disconfirming case</h4>'
            f"<p>{_esc(card.disconfirming_case.bear_hypothesis) or '—'}</p></div>"
        )
    body.write("</div>")  # /dc-grid

    if card.evidence_readiness and card.evidence_readiness.blockers:
        blockers = "".join(f"<li>{_esc(b)}</li>" for b in card.evidence_readiness.blockers)
        body.write(
            f'<div class="dc-blockers"><strong>Blocked on:</strong><ul>{blockers}</ul></div>'
        )

    if card.uncertainty:
        body.write(
            '<div class="dc-uncertainty">'
            f"<strong>Confidence ({_esc(card.uncertainty.confidence_verbal)}):</strong> "
            f"{_esc(card.uncertainty.justification) or '—'}</div>"
        )

    artifact_id = card.artifact_id or 0
    body.write('<div class="dc-actions">')
    for verb, label in _DISPOSITION_VERBS:
        cls = "k-btn-primary" if verb == card.suggested_disposition else "k-btn-quiet"
        body.write(
            f'<button type="button" class="dc-act k-btn {cls} k-btn-sm" '
            f'data-verb="{_esc(verb)}" data-artifact-id="{artifact_id}">{_esc(label)}</button>'
        )
    body.write(
        f'<a class="panel-xlink" href="/?copilot=1&amp;ticker={_esc(ticker)}#screen-workspace" '
        'target="_top">Ask the Senior Partner &rarr;</a>'
    )
    body.write(_xlink_html("thesis", "Edit hypothesis →"))
    body.write("</div>")  # /dc-actions
    body.write(f'<div class="dc-status" id="dc-status-{artifact_id}" aria-live="polite"></div>')
    body.write("</div>")  # /l1-decision-card


def _reread_strip(body: StringIO, synthesis: SynthesisSection | None) -> None:
    """The "what changed" hoist — the first line of the cached
    five_min_reread lens, with a doorway into the full Synthesis pane. Hidden
    entirely (not stubbed) when no such lens is cached for this build."""
    if synthesis is None:
        return
    lens = next((lens for lens in synthesis.lenses if lens.name == "five_min_reread"), None)
    if lens is None:
        return
    line = _first_reread_line(lens.content_md)
    if not line:
        return
    label, _sub = _LENS_LABELS.get("five_min_reread", ("5-min reread", ""))
    body.write('<div class="l1-reread">')
    body.write(f'<span class="reread-label">{_esc(label)}</span>')
    body.write(f"<p>{_inline_md(line)}</p>")
    body.write(_xlink_html("synthesis", "Full reread →"))
    body.write("</div>")


def _open_items_strip(body: StringIO, notes: list[AnalystNoteRow]) -> None:
    """The owner's open watch-items lead every build (P4.4) — a compact
    expanded-by-default strip under the thesis lede, fed by analyst_notes
    at render time. Hidden entirely when the journal has nothing open."""
    if not notes:
        return
    body.write(
        '<details class="panel l1-open-items" open data-persist="open-items">'
        '<summary class="panel-head">'
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


def _kpi_strip(body: StringIO, rows: list[KpiLedgerRow], ticker: str) -> None:
    tiles = select_kpi_strip(rows, n=4)
    if not tiles:
        return
    body.write('<div class="kpi-strip">')
    for t in tiles:
        _kpi_tile(body, t, ticker)
    # Pad with empty cells to keep the 4-up grid when fewer tiles available.
    for _ in range(4 - len(tiles)):
        body.write('<div class="kpi-tile" aria-hidden="true"></div>')
    body.write("</div>")


def _kpi_tile(body: StringIO, t: KpiStripTile, ticker: str) -> None:
    """One tier-1 KPI tile. When the row resolves to a definition id the tile
    becomes a ``.fact-doorway`` button carrying the ``kpi:{ticker}:{def_id}``
    fact_ref (Law 2 — every datum with depth is a doorway; the ledger-row
    idiom, ``workspace_sections/thesis_risk.py``/``workspace_chat.py``'s
    click delegate). Without a resolvable definition it stays today's inert
    div — never a dead button."""
    fact_ref = f"kpi:{ticker.upper()}:{t.kpi_definition_id}" if t.kpi_definition_id else None
    cls = "kpi-tile fact-doorway" if fact_ref else "kpi-tile"
    type_attr = ' type="button"' if fact_ref else ""
    attrs = f" {fact_anchor_attrs(fact_ref, t.name)}" if fact_ref else ""
    if fact_ref:
        body.write(f'<button class="{cls}"{type_attr}{attrs}>')
    else:
        body.write(f'<div class="{cls}">')
    body.write(f'<div class="kpi-name">{_esc(t.name)}</div>')
    body.write('<div class="kpi-row">')
    body.write(f'<div class="kpi-value">{_esc(t.latest_display)}</div>')
    if t.delta_display:
        cls_delta = f"kpi-delta {t.delta_sign}"
        body.write(f'<div class="{cls_delta}">{_esc(t.delta_display)}</div>')
    body.write("</div>")
    body.write(f'<div class="kpi-spark">{sparkline(t.values, size=SparklineSize.KPI)}</div>')
    body.write('<div class="kpi-axis">')
    body.write(f"<span>{_esc(t.labels[0][2:7])}</span>")
    body.write(f'<span class="kpi-trail">{len(t.values)}q trailing</span>')
    body.write(f"<span>{_esc(t.labels[-1][2:7])}</span>")
    body.write("</div></button>" if fact_ref else "</div></div>")


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
        '<div class="forgone-strip k-well k-well-warn">'
        f"⏭ <strong>{n} {word} forgone to stay under budget:</strong> {names}. "
        "Raise the cap or override, then rebuild."
        "</div>"
    )
