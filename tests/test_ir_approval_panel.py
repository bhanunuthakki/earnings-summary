"""Read-only owner review projection for exact IR document candidates."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

from pipeline.data_policy_settings_panel import (
    render_data_policy_settings_panel,
    render_operations_settings_shell,
)
from pipeline.ir_approval_panel import (
    IrApprovalPanelState,
    IrCandidatePolicyState,
    IrCandidateReviewState,
    read_ir_approval_review,
    render_ir_approval_panel,
)
from pipeline.source_policy import issuer_policy


def _create_store(path: Path, migrated_db: Callable[..., Path]) -> None:
    migrated_db(path, target="0009_add_ir_approval_store")


def _insert_candidate(
    path: Path,
    *,
    candidate_id: str,
    ticker: str,
    title: str,
    url: str,
    period: str,
    disposition: str = "ir_document",
    doc_type: str = "ir_press_release",
    issuer_id: str | None = None,
    policy_sha256: str | None = None,
) -> None:
    resolved_issuer_id = issuer_id or ticker
    resolved_policy_hash = policy_sha256 or issuer_policy(resolved_issuer_id).policy_sha256
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO ir_approval_candidates (
                candidate_id,request_id,request_sha256,issuer_id,ticker,catalog_sha256,
                issuer_policy_sha256,authority_url,quarter_end,title,candidate_url,disposition,
                doc_type,observation_key,observation_raw_sha256,evidence_locator,recorded_by,
                recorded_at,reason,evidence_json,evidence_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate_id,
                f"request-{candidate_id[:8]}",
                "c" * 64,
                resolved_issuer_id,
                ticker,
                "d" * 64,
                resolved_policy_hash,
                "https://example.test/authority",
                period,
                title,
                url,
                disposition,
                doc_type,
                f"observation-{candidate_id[:8]}",
                "a" * 64,
                "source:fixture",
                "test:fixture",
                "2026-08-12T10:00:00",
                "Review candidate",
                "[]",
                "e" * 64,
            ),
        )


def _insert_decision(
    path: Path,
    *,
    decision_id: str,
    candidate_id: str,
    action: str,
    revision: int,
    reason: str,
    selected_url: str | None = None,
    selected_doc_type: str | None = None,
    selected_content_sha256: str | None = None,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO ir_approval_decisions (
                decision_id,request_id,request_sha256,candidate_id,action,expected_revision,
                revision,supersedes_decision_id,owner_actor,decided_at,reason,evidence_json,
                evidence_sha256,selected_url,selected_doc_type,selected_content_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id,
                f"decision-request-{decision_id[:8]}",
                "f" * 64,
                candidate_id,
                action,
                revision - 1,
                revision,
                None if revision == 1 else "5" * 64,
                "owner@example.test",
                f"2026-08-12T10:0{revision}:00",
                reason,
                "[]",
                "0" * 64,
                selected_url,
                selected_doc_type,
                selected_content_sha256,
            ),
        )


def test_missing_store_is_explicitly_unavailable_and_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    view = read_ir_approval_review(missing)
    html = render_ir_approval_panel(view)

    assert view.state is IrApprovalPanelState.UNAVAILABLE
    assert not missing.exists()
    assert "Approval store unavailable" in html
    assert "No approval-state claim is inferred" in html
    assert "<form" not in html
    assert "<button" not in html


def test_empty_store_is_distinct_from_unavailable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = tmp_path / "empty.db"
    _create_store(db, migrated_db)

    view = read_ir_approval_review(db)
    html = render_ir_approval_panel(view)

    assert view.state is IrApprovalPanelState.EMPTY
    assert view.candidates == ()
    assert "No IR candidates awaiting review" in html
    assert "Approval store unavailable" not in html


def test_projection_shows_pending_rejected_and_selected_candidates_without_writing(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db = tmp_path / "approval.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="1" * 64,
        ticker="RBRK",
        title="Pending release",
        url="https://ir.rubrik.com/pending.pdf",
        period="2026-04-30",
    )
    _insert_candidate(
        db,
        candidate_id="2" * 64,
        ticker="WIX",
        title="Rejected slides",
        url="https://static.wixstatic.com/rejected.pdf",
        period="2026-06-30",
        doc_type="ir_presentation",
    )
    _insert_decision(
        db,
        decision_id="3" * 64,
        candidate_id="2" * 64,
        action="reject",
        revision=1,
        reason="Stale hidden panel",
    )
    _insert_candidate(
        db,
        candidate_id="4" * 64,
        ticker="WIX",
        title="Selected update",
        url="https://static.wixstatic.com/catalog.pdf",
        period="2026-03-31",
        doc_type="ir_investor_update",
    )
    _insert_decision(
        db,
        decision_id="5" * 64,
        candidate_id="4" * 64,
        action="approve",
        revision=1,
        reason="Candidate is in visible scope",
    )
    _insert_decision(
        db,
        decision_id="6" * 64,
        candidate_id="4" * 64,
        action="select_exact",
        revision=2,
        reason="Exact bytes confirmed",
        selected_url="https://static.wixstatic.com/selected.pdf",
        selected_doc_type="ir_investor_update",
        selected_content_sha256="b" * 64,
    )
    before = db.stat().st_mtime_ns

    view = read_ir_approval_review(db)
    html = render_ir_approval_panel(view)

    assert db.stat().st_mtime_ns == before
    assert view.state is IrApprovalPanelState.AVAILABLE
    states = {candidate.ticker + candidate.title: candidate.state for candidate in view.candidates}
    assert states == {
        "RBRKPending release": IrCandidateReviewState.PENDING,
        "WIXRejected slides": IrCandidateReviewState.REJECTED,
        "WIXSelected update": IrCandidateReviewState.SELECTED,
    }
    assert "Pending owner decision" in html
    assert "Rejected" in html
    assert "Exact document selected" in html
    assert "2026-04-30" in html
    assert "IR press release" in html
    assert "IR document" in html
    assert "https://ir.rubrik.com/pending.pdf" in html
    assert "Approved issuer / authority surface" in html
    assert "https://example.test/authority" in html
    assert "https://static.wixstatic.com/selected.pdf" in html
    assert "b" * 64 in html
    assert "Current owner decision" in html
    assert "select exact" in html
    assert "Revision 2" in html
    assert "Approve or reject policy-current candidates with a reason" in html
    assert "observation hash is not a document-byte identity" in html
    assert "overflow-wrap:anywhere" in html
    assert "<form" not in html
    assert 'data-ir-approval-action="approve"' in html
    assert 'data-ir-approval-action="reject"' in html
    assert 'data-ir-approval-action="select_exact"' in html
    assert "Unavailable until captured document bytes have a server-owned hash" in html
    assert "data-ir-approval-reason" in html
    assert "JSON.stringify({reason: reason})" in html
    assert "selected_content_sha256" not in html
    assert "k-btn-primary" not in html


def test_approved_candidate_remains_pending_until_exact_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = tmp_path / "approved.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="7" * 64,
        ticker="RBRK",
        title="Approved candidate",
        url="https://ir.rubrik.com/approved.pdf",
        period="2026-04-30",
    )
    _insert_decision(
        db,
        decision_id="8" * 64,
        candidate_id="7" * 64,
        action="approve",
        revision=1,
        reason="Candidate approved; exact bytes not selected",
    )

    view = read_ir_approval_review(db)
    html = render_ir_approval_panel(view)

    assert view.candidates[0].state is IrCandidateReviewState.PENDING
    assert view.candidates[0].current_decision_action == "approve"
    assert "Approved / exact selection pending" in html
    assert "Selected content hash" in html
    assert "Not selected" in html


def test_candidate_policy_binding_is_stale_only_on_hash_mismatch(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = tmp_path / "stale.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="8" * 64,
        ticker="RBRK",
        title="Policy changed after capture",
        url="https://ir.rubrik.com/stale.pdf",
        period="2026-04-30",
        policy_sha256="9" * 64,
    )
    _insert_decision(
        db,
        decision_id="9" * 64,
        candidate_id="8" * 64,
        action="reject",
        revision=1,
        reason="Rejected independently of policy drift",
    )
    _insert_candidate(
        db,
        candidate_id="a" * 64,
        ticker="UNKNOWN",
        issuer_id="issuer-without-policy",
        title="Policy cannot be resolved",
        url="https://example.test/unresolved.pdf",
        period="2026-03-31",
        policy_sha256="7" * 64,
    )

    view = read_ir_approval_review(db)
    html = render_ir_approval_panel(view)

    candidates = {candidate.ticker: candidate for candidate in view.candidates}
    assert candidates["RBRK"].policy_state is IrCandidatePolicyState.STALE
    assert candidates["RBRK"].state is IrCandidateReviewState.REJECTED
    assert candidates["UNKNOWN"].policy_state is IrCandidatePolicyState.STALE
    assert candidates["UNKNOWN"].state is IrCandidateReviewState.PENDING
    assert "STALE policy binding" in html
    assert "Rejected" in html
    assert "2026-04-30" in html
    stale_card = html.split('data-ir-approval-candidate="' + "8" * 64 + '"', 1)[1].split(
        "</article>", 1
    )[0]
    assert stale_card.count(" disabled") == 3


def test_untrusted_candidate_and_decision_text_is_escaped(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = tmp_path / "escaping.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="9" * 64,
        ticker="WIX",
        title='<script>alert("title")</script>',
        url='https://example.test/report.pdf?x=" onmouseover="alert(1)',
        period="2026-06-30",
    )
    _insert_decision(
        db,
        decision_id="a" * 64,
        candidate_id="9" * 64,
        action="reject",
        revision=1,
        reason="<img src=x onerror=alert(1)>",
    )

    html = render_ir_approval_panel(read_ir_approval_review(db))

    assert '<script>alert("title")</script>' not in html
    assert "<img" not in html
    assert 'onmouseover="alert(1)' not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_data_policy_settings_includes_read_only_ir_review_queue(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = tmp_path / "settings.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="b" * 64,
        ticker="RBRK",
        title="Quarterly results",
        url="https://ir.rubrik.com/results.pdf",
        period="2026-04-30",
    )

    html = render_data_policy_settings_panel(db_path=db)

    assert 'data-ir-approval-panel="review-queue"' in html
    assert "IR document review queue" in html
    assert "Quarterly results" in html
    assert 'data-ir-approval-action="approve"' in html


def test_work_os_shell_binds_one_delegated_action_listener_that_survives_panel_refresh(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = tmp_path / "shell.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="c" * 64,
        ticker="RBRK",
        title="Shell hydration candidate",
        url="https://ir.rubrik.com/shell.pdf",
        period="2026-04-30",
    )

    html = render_operations_settings_shell(db_path=db)

    assert html.count("window.__irApprovalActionsBound") == 2  # guard read + write
    assert "document.addEventListener('click'" in html
    assert "button.closest('[data-ir-approval-candidate]')" in html
    assert "panel.outerHTML = result.payload.panel_html" in html
    assert "card.querySelectorAll('[data-ir-approval-action]')" in html
    assert "card.getAttribute('aria-busy') === 'true'" in html
    assert "card.setAttribute('aria-busy', 'true')" in html
    assert "card.removeAttribute('aria-busy')" in html
    assert "actionButtons.forEach(function (control) { control.disabled = true; })" in html
    assert "previouslyEnabled.forEach(function (control) { control.disabled = false; })" in html
    assert "card.addEventListener" not in html
    assert "button.addEventListener" not in html


def test_ir_action_script_parses_in_node(tmp_path: Path, migrated_db: Callable[..., Path]) -> None:
    node = shutil.which("node")
    if node is None:
        return
    db = tmp_path / "node.db"
    _create_store(db, migrated_db)
    _insert_candidate(
        db,
        candidate_id="d" * 64,
        ticker="RBRK",
        title="Node parse candidate",
        url="https://ir.rubrik.com/node.pdf",
        period="2026-04-30",
    )
    html = render_ir_approval_panel(read_ir_approval_review(db))
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]

    result = subprocess.run(
        [node, "--check", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
