"""Earnings tab: scorecard, themes, per-quarter narrative, highlights, Q&A roster.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from report.models import (
    CellSource,
    EarningsSection,
    FinancialsSection,
    QAEntry,
    QARosterQuarter,
    QARosterSection,
    QuarterlyEarningsCard,
    QuarterlyLineItem,
    SurpriseScorecardCard,
    ThemeRollup,
)
from report.renderers.workspace_sections._shared import (
    _esc,
    _fmt_pct,
    _inline_md,
    _missing_panel,
    _panel_head,
    _quarter_selector,
    _render_markdown,
    _source_chip_html,
    _xlink_html,
)
from ui.earnings_audio import google_finance_earnings_url

__all__ = [
    "_beat_rate_scorecard_panel",
    "_earnings_narrative_panel",
    "_earnings_tab",
    "_earnings_themes_panel",
    "_financial_highlights_panel",
    "_financials_for_card",
    "_find_qa_quarter",
    "_fmt_line_value",
    "_growth_pair_cell",
    "_pos_of_card",
    "_qa_roster_panel",
    "_qa_row",
    "_source_for_display_index",
    "_surprise_tone",
    "_theme_list",
    "_ws_period_sort_key",
]


def _earnings_tab(
    body: StringIO,
    section: EarningsSection,
    financials: FinancialsSection,
    qa: QARosterSection | None,
    ticker: str,
    repo_root: str,
) -> None:
    cards = section.full_quarters + section.digest_quarters
    body.write('<div class="tab-body">')

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Earnings calls</div>')
    # (Removed the oversized "N quarters on file" section-title — the quarter
    # selector below already conveys how many quarters are on file.)
    body.write("</div>")
    if cards:
        _quarter_selector(body, [c.quarter + " " + str(c.year) for c in cards], group="earnings")
    body.write("</div>")

    # Beat-rate scorecard — small panel above the quarter cards summarizing
    # how the analyst consensus has fared over the lookback window.
    if section.surprise_scorecard is not None:
        _beat_rate_scorecard_panel(body, section.surprise_scorecard)

    # 4Q cross-quarter theme rollup — what management said vs what analysts
    # pressed on. Only renders when --enable-llm produced theme data; offline
    # builds skip silently.
    _earnings_themes_panel(body, section)

    # Per-quarter blocks — show one at a time; the quarter selector swaps
    # which is visible. Each block carries: the FMP financial-highlights
    # table for the quarter, the LLM-summarized prepared remarks / press
    # release narrative, and the parsed Q&A roster for that same quarter.
    if not cards:
        _missing_panel(body, section.status, section.missing, title="Earnings calls")
    for i, card in enumerate(cards):
        display = "" if i == 0 else "display:none"
        qid = f"{card.quarter} {card.year}"
        body.write(
            f'<div data-quarter-card data-quarter-group="earnings" '
            f'data-quarter="{_esc(qid)}" style="{display}">'
        )
        _financial_highlights_panel(body, card, _financials_for_card(card, financials))
        _earnings_narrative_panel(body, card, ticker, repo_root)
        body.write("</div>")

    # design_language §6.2 — the "Analyst Q&A" title is constant metadata; it
    # used to restate a full panel-head per quarter card (N identical titles
    # stacked in the DOM even though only one quarter is visible at a time).
    # ONE panel-head now, with the per-quarter roster swapped by the SAME
    # quarter-toggle the highlights/narrative panels above use.
    _qa_roster_panel(body, qa, cards)

    body.write("</div>")


def _beat_rate_scorecard_panel(body: StringIO, scs: SurpriseScorecardCard) -> None:
    """Two-row table: EPS / Revenue × beat rate / avg surprise / latest /
    sample size. Rows whose side has no data at all are skipped entirely
    (e.g. revenue after the FMP coverage lapse) instead of showing
    misleading zeroes.
    """
    body.write(
        _panel_head(
            "Analyst beat-rate scorecard",
            sub=f"last {scs.total_quarters} reported quarters",
        )
        + '<table class="tbl"><thead><tr>'
        "<th>Series</th>"
        '<th class="num">Beat rate</th>'
        '<th class="num">Avg surprise</th>'
        '<th class="num">Latest surprise</th>'
        '<th class="num">Beats / Misses / N/A</th>'
        "</tr></thead><tbody>"
    )
    if scs.eps_beats + scs.eps_misses > 0:
        body.write(
            f"<tr><td>EPS</td>"
            f'<td class="num">{_fmt_pct(scs.eps_beat_rate_pct)}</td>'
            f'<td class="num">{_fmt_pct(scs.eps_avg_surprise_pct)}</td>'
            f'<td class="num{_surprise_tone(scs.eps_latest_surprise_pct)}">'
            f"{_fmt_pct(scs.eps_latest_surprise_pct)}</td>"
            f'<td class="num">{scs.eps_beats} / {scs.eps_misses} / {scs.eps_no_data}</td>'
            "</tr>"
        )
    if scs.revenue_beats + scs.revenue_misses > 0:
        body.write(
            f"<tr><td>Revenue</td>"
            f'<td class="num">{_fmt_pct(scs.revenue_beat_rate_pct)}</td>'
            f'<td class="num">{_fmt_pct(scs.revenue_avg_surprise_pct)}</td>'
            f'<td class="num{_surprise_tone(scs.revenue_latest_surprise_pct)}">'
            f"{_fmt_pct(scs.revenue_latest_surprise_pct)}</td>"
            f'<td class="num">{scs.revenue_beats} / {scs.revenue_misses}'
            f" / {scs.revenue_no_data}</td>"
            "</tr>"
        )
    body.write("</tbody></table></div>")


def _surprise_tone(v: float | None) -> str:
    if v is None:
        return ""
    if v > 0.5:
        return " pos"
    if v < -0.5:
        return " neg"
    return ""


def _earnings_themes_panel(body: StringIO, section: EarningsSection) -> None:
    """4Q rolling theme rollup, split prepared vs Q&A.

    Skips when both sides are empty AND no themes_note exists (offline
    builds). When a side has no source material across the window, the
    builder leaves its list empty and sets themes_note so we surface the
    explanation rather than silently hiding the half.
    """
    has_any = bool(section.prepared_remarks_themes) or bool(section.qa_themes)
    if not has_any and not section.themes_note:
        return
    body.write(
        _panel_head(
            "Cross-quarter themes",
            sub="last 4 quarters · what management said vs what analysts pressed on",
            links=_xlink_html("bear", "bear case ↔", "panel-failure-modes"),
            panel_id="panel-themes",
        )
    )
    if section.themes_note:
        body.write(f'<p class="muted theme-note">{_esc(section.themes_note)}</p>')
    if section.prepared_remarks_themes:
        body.write(
            '<div class="theme-bucket"><h4 class="theme-bucket-title">Prepared remarks themes</h4>'
        )
        _theme_list(body, section.prepared_remarks_themes)
        body.write("</div>")
    if section.qa_themes:
        body.write('<div class="theme-bucket"><h4 class="theme-bucket-title">Q&amp;A themes</h4>')
        _theme_list(body, section.qa_themes)
        body.write("</div>")
    body.write("</div>")


def _theme_list(body: StringIO, themes: list[ThemeRollup]) -> None:
    body.write('<ul class="theme-rollup-list">')
    for theme in themes:
        body.write('<li class="theme-row">')
        body.write(
            f'<div class="theme-head"><strong>{_inline_md(theme.theme_name)}</strong>'
            f' <span class="muted">({theme.last_4q_count} mentions)</span></div>'
        )
        if theme.mentions_per_quarter:
            ordered = sorted(
                theme.mentions_per_quarter.items(),
                key=lambda kv: _ws_period_sort_key(kv[0]),
            )
            body.write('<div class="theme-spark">')
            for q, n in ordered:
                body.write(
                    f'<span class="theme-spark-cell">{_esc(q)}<span class="theme-spark-n">{n}</span></span>'
                )
            body.write("</div>")
        if theme.evidence:
            body.write('<ul class="theme-evidence">')
            for ev in theme.evidence:
                speaker = f"{_esc(ev.speaker)} · " if ev.speaker else ""
                body.write(
                    f"<li><em>&ldquo;{_esc(ev.text)}&rdquo;</em>"
                    f' <span class="muted">— {speaker}{_esc(ev.period)}</span></li>'
                )
            body.write("</ul>")
        body.write("</li>")
    body.write("</ul>")


def _ws_period_sort_key(period: str) -> tuple[int, int]:
    """Chronological sort key for 'Qx YYYY' (or 'YYYY Qx') period labels."""
    import re as _re

    y_match = _re.search(r"(20\d{2})", period)
    q_match = _re.search(r"Q([1-4])", period)
    if y_match and q_match:
        return (int(y_match.group(1)), int(q_match.group(1)))
    return (9999, 9)


def _earnings_narrative_panel(
    body: StringIO, card: QuarterlyEarningsCard, ticker: str, repo_root: str
) -> None:
    qid = f"{card.quarter} {card.year}"
    sub_parts = ["full" if card.is_recent else "digest"]
    if card.transcript_path:
        as_uri = card.transcript_path.replace("\\", "/")
        if not as_uri.startswith(("file://", "http://", "https://")):
            as_uri = "file:///" + as_uri.lstrip("/")
        sub_parts.append(
            f'<a href="{_esc(as_uri)}" target="_blank" rel="noopener" class="muted">'
            "transcript ↗</a>"
        )
    audio_url = google_finance_earnings_url(Path(repo_root), ticker)
    if audio_url:
        sub_parts.append(
            f'<a href="{_esc(audio_url)}" target="_blank" rel="noopener" class="muted" '
            'title="Opens Google Finance in a new tab — recorded call audio + transcript, '
            'hosted by Google, not this platform.">'
            "Google Finance audio/transcript ↗</a>"
        )
    body.write(
        _panel_head(
            f"{qid} — prepared remarks & key takeaways",
            sub_html=f'<span class="panel-sub">{" · ".join(sub_parts)}</span>',
        )
    )
    md = card.summary_md or card.digest_md or ""
    body.write(f'<div class="prose-pad">{_render_markdown(md)}</div>')
    body.write("</div>")


def _source_for_display_index(li: QuarterlyLineItem, idx: int) -> CellSource | None:
    """CellSource for a position in ``li.values`` (the display window).

    ``sources_full`` aligns to ``levels_full``; ``values`` is its tail —
    translate the display index into full-series coordinates.
    """
    if not li.sources_full or idx < 0:
        return None
    base = len(li.levels_full) - len(li.values) if li.levels_full else 0
    j = base + idx
    if 0 <= j < len(li.sources_full):
        return li.sources_full[j]
    return None


def _financial_highlights_panel(
    body: StringIO,
    card: QuarterlyEarningsCard,
    line_items: list[tuple[QuarterlyLineItem, float | None, float | None]],
) -> None:
    """Per-quarter FMP highlights table: revenue, op-inc, NI, EPS, FCF, capex.

    Each row carries the value FOR THIS quarter plus the prior-q and
    year-ago-q values for inline QoQ / YoY. Sourced from
    ``FinancialsSection.line_items`` — the same FMP-derived series the
    legacy renderer uses, but sliced per-quarter instead of always-latest.
    """
    qid = f"{card.quarter} {card.year}"
    if not line_items:
        # P4.2 hide-don't-stub: no fundamentals aligned to this quarter →
        # no panel; the Financials tab carries the full series.
        return
    # Panel-level source chip (P4.1 header anatomy): the quarter's primary
    # provenance, taken from the first line item that carries a source for
    # this card's quarter.
    head_chip: CellSource | None = None
    for li, _prev, _ya in line_items:
        head_chip = _source_for_display_index(li, _pos_of_card(li, card))
        if head_chip is not None:
            break
    body.write(_panel_head(f"{qid} — financial highlights", sub="QoQ / YoY", chip=head_chip))
    body.write(
        '<table class="tbl"><thead><tr>'
        "<th>Metric</th>"
        f'<th class="num">{_esc(qid)}</th>'
        '<th class="num">QoQ</th>'
        '<th class="num">YoY</th>'
        "</tr></thead><tbody>"
    )
    for li, prev, year_ago in line_items:
        if li.values and li.values[_pos_of_card(li, card)] is None:
            continue
        body.write(f"<tr><td>{_esc(li.line_item)}</td>")
        idx = _pos_of_card(li, card)
        v = li.values[idx]
        src = _source_for_display_index(li, idx)
        chip = f" {_source_chip_html(src)}" if src is not None else ""
        body.write(f'<td class="num">{_fmt_line_value(li, v)}{chip}</td>')
        body.write(_growth_pair_cell(v, prev))
        body.write(_growth_pair_cell(v, year_ago))
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _pos_of_card(li: QuarterlyLineItem, card: QuarterlyEarningsCard) -> int:
    """Position of the card's quarter in the line item's ``values`` list. -1 if not present."""
    label = f"{card.year} {card.quarter}"
    if label in li.quarters:
        return li.quarters.index(label)
    return -1


def _financials_for_card(
    card: QuarterlyEarningsCard, financials: FinancialsSection
) -> list[tuple[QuarterlyLineItem, float | None, float | None]]:
    """For each headline line item, return (item, prior_q_value, year_ago_q_value)
    when the card's quarter is present in the financials series; else []."""
    headline = {
        "revenue",
        "operating income",
        "net income",
        "eps (diluted)",
        "free cash flow",
        "capex",
        "gross profit",
        "operating cash flow",
    }
    out: list[tuple[QuarterlyLineItem, float | None, float | None]] = []
    for li in financials.line_items:
        if li.line_item.lower() not in headline:
            continue
        idx = _pos_of_card(li, card)
        if idx < 0:
            continue
        prev = li.values[idx - 1] if idx >= 1 else None
        year_ago = li.values[idx - 4] if idx >= 4 else None
        out.append((li, prev, year_ago))
    return out


def _fmt_line_value(li: QuarterlyLineItem, v: float | None) -> str:
    if v is None:
        return "—"
    if li.line_item.lower() == "eps (diluted)":
        return f"{v:.2f}"
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}B"
    return f"{v:.0f}M"


def _growth_pair_cell(curr: float | None, base: float | None) -> str:
    if curr is None or base is None or base == 0:
        return '<td class="num muted">—</td>'
    pct = (curr / base - 1) * 100
    cls = "num pos" if pct >= 0 else "num neg"
    return f'<td class="{cls}">{pct:+.1f}%</td>'


def _qa_roster_panel(
    body: StringIO,
    qa: QARosterSection | None,
    cards: list[QuarterlyEarningsCard],
) -> None:
    """ONE "Analyst Q&A" panel shared across every quarter (design_language
    §6.2 — constant metadata rides an existing frame, stated once; the
    quarter is the per-item label, not a restated title). Previously each
    quarter card carried its own full ``.panel`` with an identical
    "Analyst Q&A" ``.panel-title`` — N boxed, bordered panel-heads restating
    the same constant text, one per quarter on file. The per-quarter roster
    now swaps inside ONE panel body via the SAME quarter-toggle the
    highlights/narrative panels use — ``workspace_script``'s toggle queries
    ``[data-quarter-card][data-quarter-group]`` globally (not scoped to a
    parent), so these toggle divs don't need to nest under the per-quarter
    wrapper in ``_earnings_tab``."""
    if qa is None or not cards:
        return
    body.write(_panel_head("Analyst Q&A", sub="questions from the analyst call"))
    for i, card in enumerate(cards):
        display = "" if i == 0 else "display:none"
        qid = f"{card.quarter} {card.year}"
        body.write(
            f'<div data-quarter-card data-quarter-group="earnings" '
            f'data-quarter="{_esc(qid)}" style="{display}">'
        )
        matching = _find_qa_quarter(qa, card.quarter, card.year)
        if matching is None:
            body.write(
                '<div class="panel-empty-body">No parsed transcript on file for '
                "this quarter&rsquo;s Q&amp;A. Older calls often drop out of the "
                "transcript window, and some quarters publish without a Q&amp;A "
                "session.</div>"
            )
        else:
            n = len(matching.entries)
            body.write(
                f'<div class="panel-sub qa-quarter-sub">{_esc(matching.quarter)} '
                f"{matching.year} call &middot; {n} question{'s' if n != 1 else ''}</div>"
            )
            body.write('<div class="qa-list">')
            for j, entry in enumerate(matching.entries):
                _qa_row(body, entry, is_first=j == 0)
            body.write("</div>")
        body.write("</div>")
    body.write("</div>")


def _find_qa_quarter(qa: QARosterSection, quarter: str, year: int) -> QARosterQuarter | None:
    for q in qa.quarters:
        if q.quarter == quarter and q.year == year:
            return q
    return None


def _qa_row(body: StringIO, entry: QAEntry, *, is_first: bool) -> None:
    # P4.1 canonical collapse idiom: <details> rows, first question open.
    # The +/- chevron is CSS-driven off the [open] state.
    open_attr = " open" if is_first else ""
    body.write(f'<details class="qa-row"{open_attr}>')
    body.write('<summary class="qa-head">')
    body.write('<span class="qa-chev"></span>')
    body.write(f'<span class="qa-tag">{_esc(entry.tag)}</span>')
    body.write(f'<span class="qa-topic">{_esc(entry.topic)}</span>')
    body.write(f'<span class="qa-analysts">{_esc(entry.analysts)}</span>')
    body.write("</summary>")
    body.write('<div class="qa-body">')
    body.write(
        f'<div class="qa-q"><span class="qa-label">Q</span>'
        f"<span>{_esc(entry.question)}</span></div>"
    )
    if entry.answers:
        for speaker, text in entry.answers:
            body.write(
                f'<div class="qa-a"><span class="qa-label">A</span>'
                f"<span><strong>{_esc(speaker)}:</strong> {_esc(text)}</span></div>"
            )
    else:
        body.write(
            '<div class="qa-a"><span class="qa-label">A</span>'
            "<span><em>No response captured.</em></span></div>"
        )
    if entry.follow_up:
        body.write(
            '<div class="qa-followup">'
            '<span class="qa-followup-label">Follow-up</span>'
            f"<span>{_esc(entry.follow_up)}</span></div>"
        )
    body.write("</div></details>")
