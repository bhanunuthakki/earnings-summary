"""ReportSpec → Markdown long-form research doc.

Stub renderer: every section emits a header, status line, and either content
or a clear 'missing — fix command' block. The shape is what reviewers react
to first; content density grows as upstream sections fill in.
"""

from __future__ import annotations

from io import StringIO

from report.models import (
    AnnualLineItem,
    AppendixSection,
    BearCaseSection,
    EarningsSection,
    FinancialsSection,
    IrDocsSection,
    KpiLedgerRow,
    ProvenanceSection,
    QuarterlyEarningsCard,
    QuarterlyLineItem,
    ReportSpec,
    SayDoSection,
    SectionStatus,
    SegmentSeries,
    SegmentsSection,
    SnapshotSection,
    ThesisSection,
    TranscriptEntry,
)


def render(spec: ReportSpec) -> str:
    out = StringIO()
    _header(out, spec)
    _snapshot(out, spec.snapshot)
    _thesis(out, spec.thesis)
    _financials(out, spec.financials)
    _segments(out, spec.segments)
    _earnings(out, spec.earnings)
    _saydo(out, spec.saydo)
    _ir_docs(out, spec.ir_docs)
    _bear_case(out, spec.bear_case)
    _provenance(out, spec.provenance)
    _appendix(out, spec.appendix)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _header(out: StringIO, spec: ReportSpec) -> None:
    out.write(f"# {spec.ticker} — research report\n\n")
    out.write(f"_Generated: {spec.generation_date.isoformat()}_\n\n")
    out.write(f"_Repo root: `{spec.repo_root}`_\n\n")
    out.write("---\n\n")


def _section_header(out: StringIO, num: int, title: str, status: SectionStatus) -> None:
    out.write(f"## §{num} {title}\n\n")
    out.write(f"_Status: `{status.value}`_\n\n")


def _missing_block(out: StringIO, status: SectionStatus, missing: object) -> bool:
    """Render the 'missing data' block. Returns True if it wrote (and caller should skip content)."""
    if status == SectionStatus.OK:
        return False
    if missing is None:
        return False
    stage = getattr(missing, "stage", "unknown")
    fix = getattr(missing, "fix_command", "")
    detail = getattr(missing, "detail", None)
    out.write(f"> **Pending stage:** `{stage}`\n>\n")
    out.write(f"> **Fix:** `{fix}`\n")
    if detail:
        out.write(f">\n> {detail}\n")
    out.write("\n")
    return status not in (SectionStatus.PARTIAL,)


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_num(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _snapshot(out: StringIO, s: SnapshotSection) -> None:
    _section_header(out, 1, "Executive snapshot", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    out.write(f"**{s.ticker}** — {s.company_name or '—'}  \n")
    out.write(f"**Verdict:** `{s.verdict}`  \n")
    if s.thesis_one_liner:
        out.write(f"**Thesis:** {s.thesis_one_liner}\n\n")
    if s.valuation.model_link:
        out.write(
            f"_Valuation snapshot, DCF inputs, and segment NPVs live in the workbook: "
            f"`{s.valuation.model_link}`._\n\n"
        )


def _thesis(out: StringIO, s: ThesisSection) -> None:
    _section_header(out, 2, "Thesis & tier-1 KPIs", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if s.thesis_full:
        out.write(f"{s.thesis_full}\n\n")
    if s.last_updated:
        out.write(f"_Last updated: {s.last_updated.isoformat()}_\n\n")
    if s.break_conditions:
        out.write("### Break conditions\n\n")
        for bc in s.break_conditions:
            out.write(f"- {bc}\n")
        out.write("\n")
    if s.qualitative_breakers:
        out.write("### Qualitative thesis breakers\n\n")
        for q in s.qualitative_breakers:
            out.write(f"- {q}\n")
        out.write("\n")
    if s.competitive_watchlist:
        out.write("### Competitive watchlist\n\n")
        out.write(", ".join(s.competitive_watchlist) + "\n\n")
    _break_rules_block(out, s)
    if s.kpi_ledger:
        _kpi_ledger(out, s.kpi_ledger)


_COMPARATOR_SYMBOL_MD: dict[str, str] = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥", "eq": "="}


def _break_rules_block(out: StringIO, s: ThesisSection) -> None:
    if s.overall_breach_status == "unknown" and not s.break_rule_evaluations:
        out.write("### Universal break rules\n\n")
        out.write("_Not yet evaluated. Run `python execution/run_thesis_evaluator.py --ticker <T>` to populate `thesis_evaluations`._\n\n")
        return
    out.write("### Universal break rules\n\n")
    eval_when = (
        f" — evaluated {s.last_evaluated_at.isoformat(timespec='seconds')}"
        if s.last_evaluated_at
        else ""
    )
    out.write(f"**Overall:** `{s.overall_breach_status}`{eval_when}\n\n")
    if not s.break_rule_evaluations:
        return
    out.write("| Status | Rule | Threshold | Latest | Detail |\n|---|---|---|---|---|\n")
    for ev in s.break_rule_evaluations:
        comp = _COMPARATOR_SYMBOL_MD.get(ev.comparator, ev.comparator)
        threshold = f"{comp} {ev.threshold:g} for {ev.consecutive_periods} consecutive periods"
        if ev.observations:
            latest_cells = []
            for o in ev.observations[: max(2, ev.consecutive_periods)]:
                unit = "%" if o.unit == "percent" else (f" {o.unit}" if o.unit else "")
                latest_cells.append(f"{o.period_end}: **{o.value:.2f}{unit}**")
            latest = "<br>".join(latest_cells)
        else:
            latest = "—"
        out.write(f"| `{ev.status}` | **{ev.kpi_name}** — {ev.narrative} | {threshold} | {latest} | {ev.detail} |\n")
    out.write("\n")


def _kpi_ledger(out: StringIO, rows: list[KpiLedgerRow]) -> None:
    out.write("### KPI ledger\n\n")
    out.write("| Tier | KPI | Source | Break | Status |\n|---|---|---|---|---|\n")
    for r in rows:
        out.write(
            f"| {r.tier} | {r.name} | {r.source_hint or '—'} | {r.break_condition or '—'} | {r.current_status} |\n"
        )
    out.write("\n")


def _financials(out: StringIO, s: FinancialsSection) -> None:
    _section_header(out, 3, "Financials — last 12 quarters", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if not s.line_items:
        out.write("_No line items rendered._\n\n")
        return
    _quarterly_table(out, s.quarter_labels, s.line_items)


def _quarterly_table(out: StringIO, quarters: list[str], rows: list[QuarterlyLineItem]) -> None:
    headers = ["Line item", "Unit", *quarters, "QoQ", "YoY", "1Y CAGR", "3Y CAGR"]
    out.write("| " + " | ".join(headers) + " |\n")
    out.write("|" + "|".join(["---"] * len(headers)) + "|\n")
    for r in rows:
        cells: list[str] = [r.line_item, r.unit]
        cells.extend(_fmt_num(v, r.digits) for v in r.values)
        cells.extend(
            [
                _fmt_pct(r.growth.qoq),
                _fmt_pct(r.growth.yoy),
                _fmt_pct(r.growth.cagr_1y_ttm),
                _fmt_pct(r.growth.cagr_3y_ttm),
            ]
        )
        out.write("| " + " | ".join(cells) + " |\n")
    out.write("\n")


def _segments(out: StringIO, s: SegmentsSection) -> None:
    _section_header(out, 4, "Segments — last 12 quarters", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for label, group in (
        ("Revenue by product", s.revenue_by_product),
        ("Revenue by geography", s.revenue_by_geography),
        ("Operating income", s.operating_income),
    ):
        if not group:
            continue
        out.write(f"### {label}\n\n")
        _segments_table(out, s.quarter_labels, group)


def _segments_table(out: StringIO, quarters: list[str], rows: list[SegmentSeries]) -> None:
    headers = ["Segment", "Unit", *quarters, "QoQ", "YoY", "1Y CAGR", "3Y CAGR"]
    out.write("| " + " | ".join(headers) + " |\n")
    out.write("|" + "|".join(["---"] * len(headers)) + "|\n")
    for r in rows:
        cells: list[str] = [r.segment_name, r.unit]
        cells.extend(_fmt_num(v, 0) for v in r.values)
        cells.extend(
            [
                _fmt_pct(r.growth.qoq),
                _fmt_pct(r.growth.yoy),
                _fmt_pct(r.growth.cagr_1y_ttm),
                _fmt_pct(r.growth.cagr_3y_ttm),
            ]
        )
        out.write("| " + " | ".join(cells) + " |\n")
    out.write("\n")


def _earnings(out: StringIO, s: EarningsSection) -> None:
    _section_header(out, 5, "Earnings analysis", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    cards = list(s.full_quarters) + list(s.digest_quarters)
    cards.sort(key=lambda c: (c.year, c.quarter), reverse=True)
    for q in cards:
        if q.is_recent:
            _full_card(out, q)
        else:
            _digest_card(out, q)


def _digest_card(out: StringIO, q: QuarterlyEarningsCard) -> None:
    out.write(f"#### {q.quarter} {q.year} _(digest)_\n\n")
    if q.digest_md:
        out.write(q.digest_md.strip() + "\n\n")
    else:
        out.write("_(no digest available)_\n\n")


def _full_card(out: StringIO, q: QuarterlyEarningsCard) -> None:
    out.write(f"#### {q.quarter} {q.year}\n\n")
    if q.summary_md:
        out.write(q.summary_md.strip() + "\n\n")
    else:
        out.write("_(no LLM summary cached)_\n\n")
    if q.transcript_path:
        out.write(f"_Transcript source: `{q.transcript_path}`_\n\n")


def _saydo(out: StringIO, s: SayDoSection) -> None:
    _section_header(out, 6, "Say-Do analysis", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for c in s.cards:
        out.write(f"### {c.current_quarter} {c.current_year} vs {c.prior_quarter} {c.prior_year}\n\n")
        out.write(c.saydo_md.strip() + "\n\n")


def _ir_docs(out: StringIO, s: IrDocsSection) -> None:
    _section_header(out, 7, "IR documents", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for c in s.cards:
        out.write(f"### {c.quarter} {c.year} — {c.doc_type}\n\n")
        if c.source_url:
            out.write(f"_Source: <{c.source_url}>_\n\n")
        if c.summary_md:
            out.write(c.summary_md.strip() + "\n\n")


def _bear_case(out: StringIO, s: BearCaseSection) -> None:
    _section_header(out, 8, "Bear case", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for i, fm in enumerate(s.failure_modes, 1):
        out.write(f"### Failure mode {i}: {fm.hypothesis}\n\n")
        out.write(f"- **Evidence in data:** {fm.evidence_in_data}\n")
        out.write(f"- **Leading indicator:** {fm.leading_indicator}\n")
        out.write(f"- **Quantitative impact:** {fm.quantitative_impact}\n")
        out.write(f"- **Refutation:** {fm.refutation_criteria}\n\n")
    if s.most_underweighted:
        out.write(f"### Most underweighted by consensus\n\n{s.most_underweighted}\n\n")
    if s.out_of_scope_flags:
        out.write("### Flagged for manual review (out of scope)\n\n")
        for f in s.out_of_scope_flags:
            out.write(f"- {f}\n")
        out.write("\n")


def _provenance(out: StringIO, s: ProvenanceSection) -> None:
    _section_header(out, 9, "Provenance & data quality", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if s.coverage:
        out.write("### Coverage matrix\n\n")
        out.write("| Quarter | Audio | Transcript | Release | Slides | Say-Do | LLM summary |\n")
        out.write("|---|---|---|---|---|---|---|\n")
        for c in s.coverage:
            out.write(
                f"| {c.quarter} {c.year} | {_chk(c.has_audio_file)} | {_chk(c.has_transcript_file)} | "
                f"{_chk(c.has_release_file)} | {_chk(c.has_slides_file)} | "
                f"{_chk(c.step_saydo_analyzed)} | {_chk(c.step_llm_summarized)} |\n"
            )
        out.write("\n")
    if s.source_docs:
        out.write(f"### Source documents ({len(s.source_docs)})\n\n")
        out.write("| doc_type | period_end | file_path | sha256 |\n|---|---|---|---|\n")
        for d in s.source_docs[:50]:  # cap to keep the doc reviewable
            sha_prefix = (d.sha256 or "")[:10]
            out.write(f"| {d.doc_type} | {d.period_end or '—'} | `{d.file_path}` | `{sha_prefix}` |\n")
        if len(s.source_docs) > 50:
            out.write(f"\n_…and {len(s.source_docs) - 50} more (see workbook Provenance tab)._\n")
        out.write("\n")
    out.write(f"_Open validation issues: **{s.open_validation_issues}**_\n\n")


def _chk(b: bool) -> str:
    return "✅" if b else "⬜"


def _appendix(out: StringIO, s: AppendixSection) -> None:
    out.write(f"## §10 Appendix — full earnings-call transcripts\n\n_Status: `{s.status.value}`_\n\n")
    if s.status != SectionStatus.OK or not s.transcripts:
        out.write("_No transcripts available._\n\n")
        return
    for entry in s.transcripts:
        _transcript_block(out, entry)


def _transcript_block(out: StringIO, entry: TranscriptEntry) -> None:
    out.write(f"### {entry.quarter} {entry.year}\n\n")
    out.write(f"_Source: `{entry.source_path}` · {len(entry.text):,} chars_\n\n")
    out.write("```\n")
    out.write(entry.text)
    out.write("\n```\n\n")
