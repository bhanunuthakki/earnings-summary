"""Manual bound draft body using the existing report master; no acceptance promotion."""

from __future__ import annotations

import hashlib
import sqlite3
from html import escape
from urllib.parse import quote

from report.artifacts import RenderedReportBody, ReportInteractionManifest, ReportSectionRef
from research.earnings_report_contract import BoundPostReport
from research.report_contract import BoundReport, ReportDraft, report_draft_sha256


def render_bound_draft(
    conn: sqlite3.Connection,
    draft: ReportDraft,
    binding: BoundReport,
    *,
    post: BoundPostReport | None = None,
) -> RenderedReportBody:
    if binding.draft_sha256 != report_draft_sha256(draft):
        raise ValueError("preview draft differs from binding receipt")
    if post is not None and post.report != binding:
        raise ValueError("preview POST baseline differs from binding receipt")
    receipt_sha = hashlib.sha256(binding.model_dump_json().encode()).hexdigest()
    body = [
        '<main class="l1-root" data-report-body="v1" '
        f'data-draft-sha256="{binding.draft_sha256}" data-binding-sha256="{receipt_sha}">',
        '<div class="l1-tabs-wrap">',
        f"<h1>{escape(draft.ticker)} · {escape(draft.family.upper())} draft preview</h1>",
        '<p class="k-well k-well-warn"><strong>Not decision-grade.</strong> '
        "Source and numerical binding passed. Source completeness has not been assessed. "
        "Semantic review is still required. This preview does not replace the current brief.</p>",
    ]
    if binding.temporal_status == "historical_reconstruction":
        body.append(
            '<p class="k-well k-well-warn">Historical reconstruction. This PRE was not bound before the event.</p>'
        )
    if post is not None:
        baseline_sha = hashlib.sha256(post.model_dump_json().encode()).hexdigest()
        label = (
            "No verified PRE baseline was selected."
            if post.baseline_status == "unavailable"
            else "A historical PRE was selected; its source binding has not been verified."
        )
        body.append(
            f'<p class="k-well k-well-warn" data-baseline-sha256="{baseline_sha}">{label}</p>'
        )
    sections: list[ReportSectionRef] = []
    for index, section in enumerate(draft.sections):
        section_id = f"draft-section-{index}"
        sections.append(
            ReportSectionRef(section_id=section_id, label=section.title, group_id="draft")
        )
        body.append(f'<section id="{section_id}"><h2>{escape(section.title)}</h2>')
        for paragraph in section.paragraphs:
            label = (
                "Reported source text"
                if paragraph.voice == "reported"
                else "Analyst inference — semantic review required"
            )
            body.append(
                f'<p class="muted">{escape(label)}</p><p id="{paragraph.paragraph_id}" data-voice="{paragraph.voice}">{escape(paragraph.text)}</p>'
            )
            body.append("<details><summary>Evidence and source locations</summary>")
            for excerpt in paragraph.excerpts:
                body.append(f"<blockquote>{escape(excerpt.text)}</blockquote>")
                for node_id in excerpt.evidence_node_ids:
                    row = conn.execute(
                        "SELECT run.document_version_id,node.locator_json FROM evidence_nodes node "
                        "JOIN evidence_extraction_runs run USING(extraction_run_id) WHERE node.node_id=?",
                        (node_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("preview source location is missing")
                    href = f"/source/version/{quote(str(row[0]), safe='')}?node={quote(node_id, safe='')}"
                    body.append(
                        f'<p><a href="{escape(href, quote=True)}">{escape(str(row[0]))} · {escape(node_id)}</a></p><pre>{escape(str(row[1]))}</pre>'
                    )
            for value in paragraph.values:
                body.append(
                    f"<p>Canonical financial reference</p><pre>{escape(value.reference.model_dump_json(indent=2))}</pre>"
                )
            body.append("</details>")
        body.append("</section>")
    body.append("</div></main>")
    return RenderedReportBody.from_html(
        ticker=draft.ticker,
        report_date=binding.bound_at.date(),
        body_html="".join(body),
        sections=tuple(sections),
        interaction_manifest=ReportInteractionManifest(),
    )
