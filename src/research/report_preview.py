"""Explicit manual preview through the existing report artifact owner."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from earnings_brief import BriefCandidate
from earnings_readout import ReportedQuarter
from provenance.immutable_artifact import publish_text_no_clobber
from report.artifacts import (
    ReportArtifactRef,
    persist_report_artifact,
    validate_report_artifact_path,
)
from report.renderers.bound_draft import render_bound_draft
from report.renderers.offline_document import render_offline_document
from research.earnings_report_contract import bind_post_earnings_draft, bind_pre_earnings_draft
from research.report_contract import ReportBindingError, ReportDraft, report_read_snapshot


def save_report_preview(
    conn: sqlite3.Connection,
    draft: ReportDraft,
    *,
    repo_root: Path,
    quarter: ReportedQuarter | None = None,
) -> ReportArtifactRef:
    post = None
    with report_read_snapshot(conn):
        if draft.family == "pre" and draft.event_date is not None:
            binding = bind_pre_earnings_draft(
                conn,
                draft,
                BriefCandidate(ticker=draft.ticker, er_date=draft.event_date, days_until=0),
            )
        elif draft.family == "post" and quarter is not None:
            post = bind_post_earnings_draft(conn, draft, quarter)
            binding = post.report
        else:
            raise ReportBindingError("manual_preview_requires_pre_or_explicit_post_quarter")
        body = render_bound_draft(conn, draft, binding, post=post)
    title = f"{draft.ticker} {draft.family.upper()} draft preview — not decision-grade"
    artifact_root = (
        repo_root / "output" / "research" / draft.ticker / "artifacts" / body.artifact_id
    )
    validate_report_artifact_path(repo_root, artifact_root / "draft.json")
    publish_text_no_clobber(artifact_root / "draft.json", draft.model_dump_json())
    publish_text_no_clobber(artifact_root / "binding.json", binding.model_dump_json())
    if post is not None:
        publish_text_no_clobber(artifact_root / "post-baseline.json", post.model_dump_json())
    staging = repo_root / ".tmp"
    staging.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="report-preview-", dir=staging) as directory:
        standalone = Path(directory) / "preview.html"
        standalone.write_text(
            render_offline_document(body.body_html, title=title), encoding="utf-8"
        )
        return persist_report_artifact(
            repo_root=repo_root,
            body=body,
            standalone_path=standalone,
            generated_at=binding.bound_at,
            coverage_role="evaluation",
            title=title,
            publication_status="draft_preview",
        )
