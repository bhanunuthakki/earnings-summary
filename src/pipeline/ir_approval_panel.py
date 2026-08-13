"""Read-only Settings projection of immutable IR candidates and owner decisions."""

from __future__ import annotations

import sqlite3
from datetime import date
from enum import StrEnum
from html import escape
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from models.documents import DocType
from pipeline.approved_ir_catalog import CatalogDisposition
from pipeline.ir_approval_store import DecisionAction
from pipeline.source_policy import issuer_policy
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import ticker_label


class IrApprovalPanelState(StrEnum):
    AVAILABLE = "available"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


class IrCandidateReviewState(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    SELECTED = "selected"


class IrCandidatePolicyState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class IrCandidateReview(BaseModel):
    """One candidate joined to its latest immutable owner decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    issuer_id: str
    ticker: str
    authority_url: str
    quarter_end: date
    title: str
    canonical_url: str
    disposition: CatalogDisposition
    doc_type: DocType
    observation_content_sha256: str
    policy_state: IrCandidatePolicyState
    state: IrCandidateReviewState
    current_decision_action: DecisionAction | None = None
    revision: int | None = None
    owner_actor: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None
    selected_url: str | None = None
    selected_doc_type: DocType | None = None
    selected_content_sha256: str | None = None

    @field_validator("candidate_id", "observation_content_sha256")
    @classmethod
    def _required_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("selected_content_sha256")
    @classmethod
    def _optional_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._required_sha256(value)

    @model_validator(mode="after")
    def _decision_state_is_coherent(self) -> IrCandidateReview:
        if self.current_decision_action is None:
            if any(
                value is not None
                for value in (
                    self.revision,
                    self.owner_actor,
                    self.decided_at,
                    self.decision_reason,
                    self.selected_url,
                    self.selected_doc_type,
                    self.selected_content_sha256,
                )
            ):
                raise ValueError("a missing decision cannot carry decision fields")
        elif self.revision is None or self.owner_actor is None or self.decided_at is None:
            raise ValueError("a current decision requires revision, owner, and timestamp")
        if self.state is IrCandidateReviewState.REJECTED:
            if self.current_decision_action is not DecisionAction.REJECT:
                raise ValueError("rejected review state requires a reject decision")
        elif self.state is IrCandidateReviewState.SELECTED:
            if (
                self.current_decision_action is not DecisionAction.SELECT_EXACT
                or self.selected_url is None
                or self.selected_doc_type is None
                or self.selected_content_sha256 is None
            ):
                raise ValueError("selected review state requires one exact selection")
        elif self.current_decision_action not in (None, DecisionAction.APPROVE):
            raise ValueError("pending review state accepts only no decision or approval")
        return self


class IrApprovalReviewView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: IrApprovalPanelState
    candidates: tuple[IrCandidateReview, ...] = ()

    @model_validator(mode="after")
    def _state_matches_candidates(self) -> IrApprovalReviewView:
        if self.state is IrApprovalPanelState.AVAILABLE and not self.candidates:
            raise ValueError("available approval view requires candidates")
        if self.state is not IrApprovalPanelState.AVAILABLE and self.candidates:
            raise ValueError("empty or unavailable approval view cannot carry candidates")
        return self


_LATEST_DECISION_QUERY = """
WITH latest_decision AS (
    SELECT
        decision_id,
        candidate_id,
        action,
        revision,
        owner_actor,
        decided_at,
        reason,
        selected_url,
        selected_doc_type,
        selected_content_sha256,
        ROW_NUMBER() OVER (
            PARTITION BY candidate_id
            ORDER BY revision DESC, decision_id DESC
        ) AS row_number
    FROM ir_approval_decisions
)
SELECT
    candidate.candidate_id,
    candidate.issuer_id,
    candidate.ticker,
    candidate.authority_url,
    candidate.quarter_end,
    candidate.title,
    candidate.candidate_url,
    candidate.disposition,
    candidate.doc_type,
    candidate.observation_raw_sha256,
    candidate.issuer_policy_sha256,
    decision.action,
    decision.revision,
    decision.owner_actor,
    decision.decided_at,
    decision.reason,
    decision.selected_url,
    decision.selected_doc_type,
    decision.selected_content_sha256
FROM ir_approval_candidates AS candidate
LEFT JOIN latest_decision AS decision
    ON decision.candidate_id = candidate.candidate_id
    AND decision.row_number = 1
ORDER BY candidate.recorded_at DESC, candidate.ticker, candidate.candidate_id
"""


def _unavailable_view() -> IrApprovalReviewView:
    return IrApprovalReviewView(state=IrApprovalPanelState.UNAVAILABLE)


def _candidate_from_row(row: sqlite3.Row) -> IrCandidateReview:
    raw_action = row["action"]
    action = None if raw_action is None else DecisionAction(str(raw_action))
    state = {
        None: IrCandidateReviewState.PENDING,
        DecisionAction.APPROVE: IrCandidateReviewState.PENDING,
        DecisionAction.REJECT: IrCandidateReviewState.REJECTED,
        DecisionAction.SELECT_EXACT: IrCandidateReviewState.SELECTED,
    }[action]
    issuer_id = str(row["issuer_id"])
    ticker = str(row["ticker"])
    try:
        current_policy = issuer_policy(issuer_id)
        policy_is_current = (
            ticker.casefold() in {alias.casefold() for alias in current_policy.ticker_aliases}
            and str(row["issuer_policy_sha256"]) == current_policy.policy_sha256
        )
    except ValueError:
        policy_is_current = False
    return IrCandidateReview(
        candidate_id=str(row["candidate_id"]),
        issuer_id=issuer_id,
        ticker=ticker,
        authority_url=str(row["authority_url"]),
        quarter_end=date.fromisoformat(str(row["quarter_end"])),
        title=str(row["title"]),
        canonical_url=str(row["candidate_url"]),
        disposition=CatalogDisposition(str(row["disposition"])),
        doc_type=DocType(str(row["doc_type"])),
        observation_content_sha256=str(row["observation_raw_sha256"]),
        policy_state=(
            IrCandidatePolicyState.CURRENT if policy_is_current else IrCandidatePolicyState.STALE
        ),
        state=state,
        current_decision_action=action,
        revision=None if row["revision"] is None else int(row["revision"]),
        owner_actor=None if row["owner_actor"] is None else str(row["owner_actor"]),
        decided_at=None if row["decided_at"] is None else str(row["decided_at"]),
        decision_reason=None if row["reason"] is None else str(row["reason"]),
        selected_url=None if row["selected_url"] is None else str(row["selected_url"]),
        selected_doc_type=(
            None if row["selected_doc_type"] is None else DocType(str(row["selected_doc_type"]))
        ),
        selected_content_sha256=(
            None if row["selected_content_sha256"] is None else str(row["selected_content_sha256"])
        ),
    )


def read_ir_approval_review(db_path: Path | None) -> IrApprovalReviewView:
    """Read candidates and latest decisions without creating or mutating a database."""

    if db_path is None or not db_path.is_file():
        return _unavailable_view()
    try:
        connection = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            candidates = tuple(
                _candidate_from_row(row) for row in connection.execute(_LATEST_DECISION_QUERY)
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValidationError, ValueError):
        return _unavailable_view()
    if not candidates:
        return IrApprovalReviewView(state=IrApprovalPanelState.EMPTY)
    return IrApprovalReviewView(
        state=IrApprovalPanelState.AVAILABLE,
        candidates=candidates,
    )


_DOC_TYPE_LABELS: dict[DocType, str] = {
    DocType.IR_PRESS_RELEASE: "IR press release",
    DocType.IR_PRESENTATION: "IR presentation",
    DocType.IR_SUPPLEMENT: "IR supplement",
    DocType.IR_INVESTOR_UPDATE: "IR investor update",
    DocType.IR_TRANSCRIPT: "IR transcript",
    DocType.IR_EVENT: "IR event",
}

_DISPOSITION_LABELS: dict[CatalogDisposition, str] = {
    CatalogDisposition.IR_DOCUMENT: "IR document",
    CatalogDisposition.SEC_HANDOFF: "SEC handoff",
    CatalogDisposition.TRANSCRIPT_CANDIDATE: "Transcript candidate",
    CatalogDisposition.WEBCAST_EXCLUDED: "Webcast excluded",
}

_STATE_LABELS: dict[IrCandidateReviewState, str] = {
    IrCandidateReviewState.PENDING: "Pending owner decision",
    IrCandidateReviewState.REJECTED: "Rejected",
    IrCandidateReviewState.SELECTED: "Exact document selected",
}

_STATE_TONES: dict[IrCandidateReviewState, str] = {
    IrCandidateReviewState.PENDING: "k-pill-warn",
    IrCandidateReviewState.REJECTED: "k-pill-bad",
    IrCandidateReviewState.SELECTED: "k-pill-ok",
}


def _decision_label(candidate: IrCandidateReview) -> str:
    if candidate.current_decision_action is None:
        return "None recorded"
    if candidate.current_decision_action is DecisionAction.APPROVE:
        return "Approved / exact selection pending"
    return candidate.current_decision_action.value.replace("_", " ")


def _render_candidate(candidate: IrCandidateReview) -> str:
    revision = "Not recorded" if candidate.revision is None else f"Revision {candidate.revision}"
    owner = candidate.owner_actor or "Not recorded"
    decided_at = candidate.decided_at or "Not recorded"
    reason = candidate.decision_reason or "No owner reason recorded"
    selected_url = candidate.selected_url or "Not selected"
    selected_doc_type = (
        "Not selected"
        if candidate.selected_doc_type is None
        else _DOC_TYPE_LABELS[candidate.selected_doc_type]
    )
    selected_hash = candidate.selected_content_sha256 or "Not selected"
    policy_label = (
        "Policy current"
        if candidate.policy_state is IrCandidatePolicyState.CURRENT
        else "STALE policy binding"
    )
    policy_tone = (
        "k-pill-ok" if candidate.policy_state is IrCandidatePolicyState.CURRENT else "k-pill-bad"
    )
    candidate_id = escape(candidate.candidate_id, quote=True)
    action_disabled = candidate.policy_state is IrCandidatePolicyState.STALE
    approve_disabled = action_disabled or candidate.current_decision_action in {
        DecisionAction.APPROVE,
        DecisionAction.SELECT_EXACT,
    }
    reject_disabled = action_disabled or candidate.current_decision_action is DecisionAction.REJECT
    disabled_reason = (
        "Current issuer policy no longer authorizes this candidate"
        if action_disabled
        else "This is already the current owner decision"
    )

    def action_button(
        action: DecisionAction,
        label: str,
        classes: str,
        *,
        disabled: bool,
        title: str,
    ) -> str:
        disabled_attribute = " disabled" if disabled else ""
        return (
            f'<button type="button" class="k-btn {classes} k-btn-sm" '
            f'data-ir-approval-action="{action.value}" '
            f'data-ir-candidate-id="{candidate_id}" title="{escape(title, quote=True)}"'
            f"{disabled_attribute}>{escape(label)}</button>"
        )

    approve_button = action_button(
        DecisionAction.APPROVE,
        "Approve",
        "k-btn-quiet",
        disabled=approve_disabled,
        title=disabled_reason if approve_disabled else "Approve this policy-current candidate",
    )
    reject_button = action_button(
        DecisionAction.REJECT,
        "Reject",
        "k-btn-danger",
        disabled=reject_disabled,
        title=disabled_reason if reject_disabled else "Reject this candidate with an owner reason",
    )
    selection_button = action_button(
        DecisionAction.SELECT_EXACT,
        "Select exact",
        "k-btn-quiet",
        disabled=True,
        title="Unavailable until captured document bytes have a server-owned hash",
    )
    return (
        '<article class="k-well k-card-stack" style="min-width:0;" '
        f'data-ir-approval-candidate="{candidate_id}">'
        '<div class="k-toolbar">'
        f"<div>{ticker_label(candidate.ticker, candidate.title)}"
        f'<div class="k-card-meta">Reporting period {candidate.quarter_end.isoformat()}</div></div>'
        f'<span class="k-pill {_STATE_TONES[candidate.state]}">'
        f"{escape(_STATE_LABELS[candidate.state])}</span></div>"
        '<div class="k-card-meta">'
        f'<span class="k-pill {policy_tone}">{escape(policy_label)}</span> '
        f'<span class="k-chip">{escape(_DOC_TYPE_LABELS[candidate.doc_type])}</span> '
        f'<span class="k-chip">{escape(_DISPOSITION_LABELS[candidate.disposition])}</span> '
        f'<span class="k-chip k-chip-mono">{escape(revision)}</span></div>'
        "<dl>"
        '<dt class="k-label">Candidate canonical URL</dt>'
        '<dd style="min-width:0;overflow-wrap:anywhere;">'
        f"<code>{escape(candidate.canonical_url)}</code></dd>"
        '<dt class="k-label">Approved issuer / authority surface</dt>'
        '<dd style="min-width:0;overflow-wrap:anywhere;">'
        f"<code>{escape(candidate.issuer_id)}</code> &middot; "
        f"<code>{escape(candidate.authority_url)}</code></dd>"
        '<dt class="k-label">Observed source hash</dt>'
        '<dd style="min-width:0;overflow-wrap:anywhere;">'
        f"<code>{escape(candidate.observation_content_sha256)}</code></dd>"
        '<dt class="k-label">Current owner decision</dt>'
        f"<dd>{escape(_decision_label(candidate))}</dd>"
        '<dt class="k-label">Owner / decided at</dt>'
        f"<dd>{escape(owner)} · {escape(decided_at)}</dd>"
        '<dt class="k-label">Owner reason</dt>'
        f"<dd>{escape(reason)}</dd>"
        '<dt class="k-label">Selected exact URL</dt>'
        '<dd style="min-width:0;overflow-wrap:anywhere;">'
        f"<code>{escape(selected_url)}</code></dd>"
        '<dt class="k-label">Selected document type</dt>'
        f"<dd>{escape(selected_doc_type)}</dd>"
        '<dt class="k-label">Selected content hash</dt>'
        '<dd style="min-width:0;overflow-wrap:anywhere;">'
        f"<code>{escape(selected_hash)}</code></dd>"
        "</dl>"
        f'<label class="k-label" for="ir-approval-reason-{candidate_id}">Owner reason</label>'
        f'<textarea id="ir-approval-reason-{candidate_id}" data-ir-approval-reason '
        'rows="2" maxlength="4096" placeholder="Required for every owner decision"></textarea>'
        '<div class="k-toolbar-controls">'
        f"{approve_button}{reject_button}{selection_button}</div>"
        '<div class="k-card-meta" role="status" aria-live="polite" '
        "data-ir-approval-receipt></div>"
        "</article>"
    )


_IR_APPROVAL_ACTIONS_SCRIPT = r"""
<script>
(function () {
  if (window.__irApprovalActionsBound) return;
  window.__irApprovalActionsBound = true;
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-ir-approval-action]');
    if (!button || button.disabled) return;
    var card = button.closest('[data-ir-approval-candidate]');
    if (!card || card.getAttribute('aria-busy') === 'true') return;
    var reasonField = card.querySelector('[data-ir-approval-reason]');
    var receipt = card.querySelector('[data-ir-approval-receipt]');
    var reason = reasonField ? reasonField.value.trim() : '';
    if (!reason) {
      if (receipt) receipt.textContent = 'Owner reason is required.';
      if (reasonField) reasonField.focus();
      return;
    }
    var candidate = button.getAttribute('data-ir-candidate-id');
    var action = button.getAttribute('data-ir-approval-action');
    var actionButtons = Array.prototype.slice.call(
      card.querySelectorAll('[data-ir-approval-action]')
    );
    var previouslyEnabled = actionButtons.filter(function (control) { return !control.disabled; });
    card.setAttribute('aria-busy', 'true');
    actionButtons.forEach(function (control) { control.disabled = true; });
    if (receipt) receipt.textContent = 'Recording owner decision…';
    fetch('/api/ir-approval/candidates/' + encodeURIComponent(candidate) + '/' +
          encodeURIComponent(action), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason})
    }).then(function (response) {
      return response.json().then(function (payload) {
        return {ok: response.ok, payload: payload};
      });
    }).then(function (result) {
      if (!result.ok) throw new Error(result.payload.error || 'Owner decision was not recorded');
      var panel = document.querySelector('[data-ir-approval-panel="review-queue"]');
      if (panel && result.payload.panel_html) panel.outerHTML = result.payload.panel_html;
      var refreshed = document.querySelector('[data-ir-approval-candidate="' + candidate + '"]');
      var refreshedReceipt = refreshed && refreshed.querySelector('[data-ir-approval-receipt]');
      if (refreshedReceipt) refreshedReceipt.textContent = result.payload.receipt;
    }).catch(function (error) {
      card.removeAttribute('aria-busy');
      previouslyEnabled.forEach(function (control) { control.disabled = false; });
      if (receipt) receipt.textContent = error.message;
    });
  });
}());
</script>
"""


def render_ir_approval_panel(view: IrApprovalReviewView) -> str:
    """Render the owner-review queue and policy-gated decision controls."""

    if view.state is IrApprovalPanelState.UNAVAILABLE:
        content = (
            '<div class="k-well k-well-warn">'
            '<div class="k-card-row-title">Approval store unavailable</div>'
            "<p>The immutable IR approval schema is not present or could not be read. "
            "No approval-state claim is inferred.</p></div>"
        )
    elif view.state is IrApprovalPanelState.EMPTY:
        content = (
            '<div class="k-well">'
            '<div class="k-card-row-title">No IR candidates awaiting review</div>'
            "<p>The approval store is available and currently contains no candidates.</p></div>"
        )
    else:
        content = "".join(_render_candidate(candidate) for candidate in view.candidates)
    return (
        '<section class="k-card-stack" data-ir-approval-panel="review-queue" '
        'aria-labelledby="ir-approval-review-title">'
        '<div class="k-toolbar"><div>'
        '<h3 class="k-card-title" id="ir-approval-review-title">IR document review queue</h3>'
        '<div class="k-card-meta">Owner-governed · immutable decisions</div>'
        "</div></div>"
        "<p>Approve or reject policy-current candidates with a reason. Exact selection remains "
        "unavailable until captured document bytes have a server-owned hash; the catalog "
        "observation hash is not a document-byte identity.</p>"
        f"{content}</section>{_IR_APPROVAL_ACTIONS_SCRIPT}"
    )


__all__ = [
    "IrApprovalPanelState",
    "IrApprovalReviewView",
    "IrCandidatePolicyState",
    "IrCandidateReview",
    "IrCandidateReviewState",
    "read_ir_approval_review",
    "render_ir_approval_panel",
]
