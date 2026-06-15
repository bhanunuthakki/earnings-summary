"""Sources tab: coverage matrix, source docs, validation issues, prompt quality.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from llm.calibration import VersionSummary, daily_avg_scores, summarize_by_prompt_version
from report.models import AppendixSection, ProvenanceSection
from report.renderers.workspace_charts import sparkline
from report.renderers.workspace_sections._shared import _esc, _panel_head
from ui.controls import prov_severity_tick

# The research server the report deep-links into. The report opens via file://,
# so console links (the in-app /source/<doc_id> viewers, the live data-quality
# console) must be absolute — same hardcoded base the chat/socratic links use
# (workspace_sections/boot.py). The viewers 302 to the original URL when a doc
# isn't in-app-viewable, so the link is never a dead end.
_CONSOLE_BASE = "http://localhost:7421"

__all__ = [
    "_prompt_quality_panel",
    "_sources_tab",
]


def _sources_tab(
    body: StringIO,
    prov: ProvenanceSection,
    app: AppendixSection,
    repo_root: str,
) -> None:
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Provenance &amp; sources</div>')
    body.write(
        '<h2 class="section-title">'
        f"{len(prov.coverage)} quarter{'s' if len(prov.coverage) != 1 else ''} of coverage · "
        f"{len(prov.source_docs)} source documents · "
        f"{len(app.transcripts)} transcript{'s' if len(app.transcripts) != 1 else ''} inline."
        "</h2>"
    )
    body.write("</div>")
    # Deep-link into the running command-center's consolidated data-quality
    # console (System → Provenance) — resolve / refresh / diagnose live there.
    body.write(
        '<div class="row-actions">'
        f'<a class="k-btn k-btn-quiet k-btn-sm" href="{_CONSOLE_BASE}/#provenance" '
        'target="_blank" rel="noopener">Open the live data-quality console &rarr;</a>'
        "</div>"
    )
    body.write("</div>")

    # Transcripts first — most-used scroll target per user request.
    if app.transcripts:
        body.write(
            _panel_head(
                "Earnings-call transcripts",
                sub=f"{len(app.transcripts)} on file · click to expand",
            )
        )
        for t in app.transcripts:
            body.write('<details class="transcript-block">')
            body.write(f"<summary>{_esc(t.quarter)} {t.year} — {len(t.text):,} chars</summary>")
            body.write(f'<div class="transcript-text">{_esc(t.text)}</div>')
            body.write("</details>")
        body.write("</div>")

    # Surface open validation issues as a dedicated panel above the matrix
    # when there are any — the count alone (previously a small subhead) was
    # easy to miss. Severity-sorted (errors first), capped server-side at 50.
    if prov.open_issues_detail:
        body.write(
            _panel_head(
                "Open validation issues",
                sub_html=f'<span class="k-chip k-chip-mono k-chip-warn">{prov.open_validation_issues} open</span>',
            )
            + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
            "<th>Severity</th><th>Rule</th><th>Raw value</th><th>Expected</th><th>Raised</th>"
            "</tr></thead><tbody>"
        )
        for v in prov.open_issues_detail:
            # Severity tick via the shared kit (prov_severity_tick) so the tone
            # derives from the live Severity enum — the writer emits halt/warn,
            # and the prior hand-typed {error,warning} map rendered every real
            # issue (incl. HALT) muted grey. See test_provenance_severity_contract.
            body.write(
                f"<tr>"
                f"<td>{prov_severity_tick(v.severity)}</td>"
                f"<td>{_esc(v.rule)}</td>"
                f"<td>{_esc(v.raw_value or '—')}</td>"
                f"<td>{_esc(v.expected or '—')}</td>"
                f"<td>{_esc((v.raised_at or '')[:10])}</td></tr>"
            )
        body.write("</tbody></table></div></div>")

    if prov.coverage:
        body.write(
            _panel_head(
                "Coverage matrix",
                sub=f"{prov.open_validation_issues} open validation issues",
            )
            + '<div class="table-scroll"><table class="tbl coverage-table"><thead><tr>'
            "<th>Quarter</th>"
            '<th class="cov-cell">Audio</th>'
            '<th class="cov-cell">Transcript</th>'
            '<th class="cov-cell">Release</th>'
            '<th class="cov-cell">Slides</th>'
            '<th class="cov-cell">SayDo</th>'
            '<th class="cov-cell">Summary</th></tr></thead><tbody>'
        )
        for c in prov.coverage:
            body.write(f"<tr><td>{_esc(c.quarter)} {c.year}</td>")
            for present in (
                c.has_audio_file,
                c.has_transcript_file,
                c.has_release_file,
                c.has_slides_file,
                c.step_saydo_analyzed,
                c.step_llm_summarized,
            ):
                mark = "●" if present else "○"
                cls = "cov-yes" if present else "cov-no"
                body.write(f'<td class="cov-cell {cls}">{mark}</td>')
            body.write("</tr>")
        body.write("</tbody></table></div></div>")

    if prov.source_docs:
        body.write(
            _panel_head("Source documents", sub=f"{len(prov.source_docs)} files")
            + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
            "<th>Type</th><th>Period</th><th>Path</th><th>Fetched</th>"
            "</tr></thead><tbody>"
        )
        for d in prov.source_docs:
            # Path deep-links the in-app /source/<doc_id> viewer (numbered
            # transcript reader / section-keyed 10-K reader / metadata card with
            # the outbound link) when the row carries a doc_id; older built
            # reports without it fall back to the plain path text.
            if d.doc_id is not None:
                path_cell = (
                    f'<a href="{_CONSOLE_BASE}/source/{d.doc_id}" target="_blank" '
                    f'rel="noopener">{_esc(d.file_path)}</a>'
                )
            else:
                path_cell = _esc(d.file_path)
            body.write(
                f"<tr><td>{_esc(d.doc_type)}</td>"
                f"<td>{_esc(d.period_end or '—')}</td>"
                f"<td>{path_cell}</td>"
                f"<td>{_esc(d.fetched_at or '—')}</td></tr>"
            )
        body.write("</tbody></table></div></div>")

    _prompt_quality_panel(body, Path(repo_root) / "data" / "portfolio.db")
    body.write("</div>")


def _prompt_quality_panel(body: StringIO, db_path: Path) -> None:
    """Surface the prompt_calibration_scores table — grouped by (purpose,
    prompt_version) so the analyst can see whether each prompt version is
    improving over time without spelunking the per-call ledger.

    The graders (grade_bear_cases, grade_decisions) write the rows; this
    panel is the consumer side. Best-effort: missing DB / empty table
    degrades to a small "no data yet" stub rather than breaking the page.
    """
    window_days = 30
    # Stored scored_at is naive UTC ISO; compare with a naive cutoff so the
    # SQL string comparison is well-defined.
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)
    summaries: list[VersionSummary] = summarize_by_prompt_version(db_path=db_path, since=since)

    if not summaries:
        # P4.2 hide-don't-stub: no graded outputs in the window → no panel.
        return

    body.write(
        _panel_head(
            "Prompt quality",
            sub=f"last {window_days} days · grader-scored, grouped by prompt version",
        )
    )

    daily = daily_avg_scores(db_path=db_path, since=since)
    body.write(
        '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
        "<th>Purpose</th><th>Version</th>"
        '<th class="num">n</th>'
        '<th class="num">avg</th>'
        '<th class="num">p25</th>'
        '<th class="num">p50</th>'
        '<th class="num">p75</th>'
        "<th>Last scored</th>"
        "<th>30d trend</th>"
        "</tr></thead><tbody>"
    )
    for s in summaries:
        spark_vals = [v for _, v in daily.get((s.purpose, s.prompt_version), [])]
        spark = sparkline(spark_vals, width=120, height=24) if spark_vals else "—"
        last = (s.last_scored_at or "—")[:19].replace("T", " ")
        body.write(
            "<tr>"
            f"<td>{_esc(s.purpose)}</td>"
            f"<td>{_esc(s.prompt_version)}</td>"
            f'<td class="num">{s.score_count}</td>'
            f'<td class="num">{s.avg_score:.3f}</td>'
            f'<td class="num">{s.p25:.3f}</td>'
            f'<td class="num">{s.p50:.3f}</td>'
            f'<td class="num">{s.p75:.3f}</td>'
            f"<td>{_esc(last)}</td>"
            f"<td>{spark}</td>"
            "</tr>"
        )
    body.write("</tbody></table></div></div>")
