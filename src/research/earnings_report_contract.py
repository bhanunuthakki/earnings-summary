"""Read-only PRE/POST binding adapters; scheduled generation is not switched here."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from earnings_brief import SECTION_TITLES as PRE_TITLES
from earnings_brief import BriefCandidate
from earnings_readout import SECTION_TITLES as POST_TITLES
from earnings_readout import ReportedQuarter, pre_call_baseline
from provenance.source_read_context import SourceReadContext
from research.report_contract import (
    BoundReport,
    ReportBindingError,
    ReportDraft,
    bind_report_draft,
    report_read_snapshot,
)


class BoundPostReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report: BoundReport
    baseline_status: Literal["selected_legacy", "unavailable"]
    baseline_selection_sha256: str
    baseline_artifact_id: int | None
    baseline_reason: str | None
    # Existing PRE selection validates timing and identity, not this new binding contract.
    baseline_binding: Literal["not_verified"] = "not_verified"


def bind_pre_earnings_draft(
    conn: sqlite3.Connection,
    draft: ReportDraft,
    candidate: BriefCandidate,
    *,
    source_context: SourceReadContext | None = None,
) -> BoundReport:
    if (
        draft.family != "pre"
        or draft.ticker != candidate.ticker
        or draft.event_date != candidate.er_date
    ):
        raise ReportBindingError("pre_report_event_mismatch")
    return bind_report_draft(conn, draft, required_titles=PRE_TITLES, source_context=source_context)


def bind_post_earnings_draft(
    conn: sqlite3.Connection,
    draft: ReportDraft,
    quarter: ReportedQuarter,
    *,
    source_context: SourceReadContext | None = None,
) -> BoundPostReport:
    if draft.family != "post" or draft.ticker != quarter.ticker:
        raise ReportBindingError("post_report_identity_mismatch")
    if not quarter.call_date:
        raise ReportBindingError("post_report_event_unknown")
    if draft.event_date != date.fromisoformat(quarter.call_date[:10]):
        raise ReportBindingError("post_report_event_mismatch")
    with report_read_snapshot(conn):
        baseline = pre_call_baseline(conn, quarter)
        report = bind_report_draft(
            conn, draft, required_titles=POST_TITLES, source_context=source_context
        )
        selected = baseline.get("status") == "selected"
        artifact_id = baseline.get("artifact_id")
        if selected and (type(artifact_id) is not int or artifact_id <= 0):
            raise ReportBindingError("post_baseline_identity_invalid")
        reason = baseline.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ReportBindingError("post_baseline_reason_invalid")
        return BoundPostReport(
            report=report,
            baseline_status="selected_legacy" if selected else "unavailable",
            baseline_selection_sha256=hashlib.sha256(
                json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            baseline_artifact_id=artifact_id if isinstance(artifact_id, int) and selected else None,
            baseline_reason=reason,
        )
