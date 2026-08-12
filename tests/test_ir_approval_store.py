"""Owner approval and exact-document selection for approved IR catalogs."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import DocType  # noqa: E402
from pipeline.approved_ir_catalog import ApprovedIrCatalog, build_catalog  # noqa: E402
from pipeline.approved_ir_rubrik import (  # noqa: E402
    load_rubrik_row_observations,
    parse_rubrik_quarter_rows,
)
from pipeline.approved_ir_wix import (  # noqa: E402
    load_wix_rendered_observations,
    parse_wix_visible_quarters,
)
from pipeline.ir_approval_store import (  # noqa: E402
    DecisionAction,
    EvidenceReference,
    IrAdmissionProof,
    IrApprovalConflictError,
    IrAuthorizationError,
    IrCandidateRequest,
    IrDecisionRequest,
    append_decision,
    get_current_decision,
    persist_candidate,
    verify_admission,
)
from pipeline.source_policy import issuer_policy  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "approved_ir"
RUBRIK_FIXTURE = FIXTURES / "rubrik_rows_sanitized.json"
WIX_FIXTURE = FIXTURES / "wix_rendered_sequence_sanitized.json"
NOW = datetime(2026, 8, 12, 10, 0, 0)
EVIDENCE = (
    EvidenceReference(
        evidence_id="owner-review-note-1",
        locator="review://owner/ir/rbrk-q2",
        content_sha256="f" * 64,
    ),
)
SELECTED_CONTENT_SHA256 = "1" * 64


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ir-approval.db", target="0009_add_ir_approval_store")


def _rubrik_catalog() -> ApprovedIrCatalog:
    policy = issuer_policy("RBRK")
    observations = load_rubrik_row_observations(RUBRIK_FIXTURE.read_text(encoding="utf-8"))
    return build_catalog(policy, parse_rubrik_quarter_rows(observations, policy=policy))


def _wix_catalog() -> ApprovedIrCatalog:
    policy = issuer_policy("WIX")
    observations = load_wix_rendered_observations(WIX_FIXTURE.read_text(encoding="utf-8"))
    return build_catalog(policy, parse_wix_visible_quarters(observations, policy=policy))


def _first_document_url(catalog: ApprovedIrCatalog) -> str:
    return next(entry.url for entry in catalog.entries if entry.disposition.value == "ir_document")


@pytest.mark.parametrize(
    "ticker,catalog_factory", [("RBRK", _rubrik_catalog), ("WIX", _wix_catalog)]
)
def test_persist_candidate_accepts_approved_catalogs_and_exact_replay(
    ticker: str,
    catalog_factory: Callable[[], ApprovedIrCatalog],
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = _database(tmp_path, migrated_db)
    catalog = catalog_factory()
    request = IrCandidateRequest(
        request_id=f"candidate-{ticker.lower()}-1",
        ticker=ticker,
        catalog=catalog,
        candidate_url=_first_document_url(catalog),
        recorded_by="owner@example.test",
        recorded_at=NOW,
        reason="Review this exact issuer document",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        first = persist_candidate(connection, request)
        replay = persist_candidate(connection, request)
        assert first.outcome == "created"
        assert replay.outcome == "exact_replay"
        assert replay.candidate == first.candidate
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_candidates").fetchone()[0] == 1


def test_owner_approval_then_exact_selection_is_cas_guarded_and_auditable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = _database(tmp_path, migrated_db)
    catalog = _rubrik_catalog()
    candidate_url = _first_document_url(catalog)
    candidate_request = IrCandidateRequest(
        request_id="candidate-rbrk-approval",
        ticker="RBRK",
        catalog=catalog,
        candidate_url=candidate_url,
        recorded_by="pipeline:approved-ir-catalog",
        recorded_at=NOW,
        reason="Catalog candidate requires owner review",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        candidate = persist_candidate(connection, candidate_request).candidate
        approval_request = IrDecisionRequest(
            request_id="decision-rbrk-approve",
            candidate_id=candidate.candidate_id,
            action=DecisionAction.APPROVE,
            expected_revision=0,
            owner_actor="owner@example.test",
            decided_at=NOW,
            reason="Issuer evidence and reporting period confirmed",
            evidence=EVIDENCE,
        )
        approved = append_decision(connection, approval_request)
        replay = append_decision(connection, approval_request)
        assert approved.outcome == "appended"
        assert replay.outcome == "exact_replay"
        assert approved.decision.revision == 1
        assert approved.decision.supersedes_decision_id is None

        selection_request = IrDecisionRequest(
            request_id="decision-rbrk-select",
            candidate_id=candidate.candidate_id,
            action=DecisionAction.SELECT_EXACT,
            expected_revision=1,
            owner_actor="owner@example.test",
            decided_at=datetime(2026, 8, 12, 10, 1, 0),
            reason="Select the reviewed presentation URL",
            evidence=EVIDENCE,
            selected_url=candidate_url,
            selected_doc_type=candidate.doc_type,
            selected_content_sha256=SELECTED_CONTENT_SHA256,
        )
        selected = append_decision(connection, selection_request)
        assert selected.decision.revision == 2
        assert selected.decision.supersedes_decision_id == approved.decision.decision_id
        assert get_current_decision(connection, candidate.candidate_id) == selected.decision
        rows = connection.execute(
            "SELECT action,owner_actor,reason,evidence_json,revision "
            "FROM ir_approval_decisions ORDER BY revision"
        ).fetchall()
        assert [tuple(row[:3]) for row in rows] == [
            ("approve", "owner@example.test", "Issuer evidence and reporting period confirmed"),
            ("select_exact", "owner@example.test", "Select the reviewed presentation URL"),
        ]
        assert all(str(row[3]).startswith("[") for row in rows)
        assert [int(row[4]) for row in rows] == [1, 2]

        stale = selection_request.model_copy(
            update={"request_id": "decision-rbrk-stale", "expected_revision": 1}
        )
        with pytest.raises(IrApprovalConflictError, match="revision"):
            append_decision(connection, stale)


def test_reject_can_supersede_approval_but_replay_identity_is_immutable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = _database(tmp_path, migrated_db)
    catalog = _wix_catalog()
    request = IrCandidateRequest(
        request_id="candidate-wix-reject",
        ticker="WIX",
        catalog=catalog,
        candidate_url=_first_document_url(catalog),
        recorded_by="pipeline:approved-ir-catalog",
        recorded_at=NOW,
        reason="Review Wix document",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        candidate = persist_candidate(connection, request).candidate
        approve = IrDecisionRequest(
            request_id="decision-wix-1",
            candidate_id=candidate.candidate_id,
            action=DecisionAction.APPROVE,
            expected_revision=0,
            owner_actor="owner@example.test",
            decided_at=NOW,
            reason="Initial approval",
            evidence=EVIDENCE,
        )
        append_decision(connection, approve)
        rejected = append_decision(
            connection,
            IrDecisionRequest(
                request_id="decision-wix-2",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.REJECT,
                expected_revision=1,
                owner_actor="owner@example.test",
                decided_at=datetime(2026, 8, 12, 10, 2, 0),
                reason="Hidden stale panel makes this capture ambiguous",
                evidence=EVIDENCE,
            ),
        )
        assert rejected.decision.action is DecisionAction.REJECT
        with pytest.raises(IrApprovalConflictError, match="replay conflict"):
            append_decision(
                connection,
                approve.model_copy(update={"reason": "changed replay payload"}),
            )


def _change_policy_hash(request: IrCandidateRequest) -> IrCandidateRequest:
    return request.model_copy(
        update={"catalog": request.catalog.model_copy(update={"issuer_policy_sha256": "0" * 64})}
    )


def _escape_catalog_url(request: IrCandidateRequest) -> IrCandidateRequest:
    return request.model_copy(update={"candidate_url": "https://evil.example.test/report.pdf"})


@pytest.mark.parametrize(
    "mutator,error",
    [(_change_policy_hash, "policy"), (_escape_catalog_url, "catalog")],
)
def test_candidate_persistence_fails_closed_on_policy_or_url_escape(
    mutator: Callable[[IrCandidateRequest], IrCandidateRequest],
    error: str,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = _database(tmp_path, migrated_db)
    catalog = _rubrik_catalog()
    valid = IrCandidateRequest(
        request_id="candidate-rbrk-closed",
        ticker="RBRK",
        catalog=catalog,
        candidate_url=_first_document_url(catalog),
        recorded_by="pipeline:approved-ir-catalog",
        recorded_at=NOW,
        reason="Review candidate",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(IrAuthorizationError, match=error):
            persist_candidate(connection, mutator(valid))
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_candidates").fetchone()[0] == 0


def test_exact_selection_accepts_owner_url_inside_policy_and_rejects_escapes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = _database(tmp_path, migrated_db)
    catalog = _rubrik_catalog()
    request = IrCandidateRequest(
        request_id="candidate-rbrk-exact",
        ticker="RBRK",
        catalog=catalog,
        candidate_url=_first_document_url(catalog),
        recorded_by="pipeline:approved-ir-catalog",
        recorded_at=NOW,
        reason="Review candidate",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        candidate = persist_candidate(connection, request).candidate
        append_decision(
            connection,
            IrDecisionRequest(
                request_id="decision-rbrk-exact-approve",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.APPROVE,
                expected_revision=0,
                owner_actor="owner@example.test",
                decided_at=NOW,
                reason="Approve candidate",
                evidence=EVIDENCE,
            ),
        )
        owner_selected_url = "https://ir.rubrik.com/static-files/owner-confirmed-report.pdf"
        selected = append_decision(
            connection,
            IrDecisionRequest(
                request_id="decision-rbrk-exact-owner-url",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.SELECT_EXACT,
                expected_revision=1,
                owner_actor="owner@example.test",
                decided_at=datetime(2026, 8, 12, 10, 2, 0),
                reason="Owner confirmed a different exact document URL",
                evidence=EVIDENCE,
                selected_url=owner_selected_url,
                selected_doc_type=candidate.doc_type,
                selected_content_sha256=SELECTED_CONTENT_SHA256,
            ),
        )
        assert selected.decision.selected_url == owner_selected_url

        append_decision(
            connection,
            IrDecisionRequest(
                request_id="decision-rbrk-exact-reapprove",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.APPROVE,
                expected_revision=2,
                owner_actor="owner@example.test",
                decided_at=datetime(2026, 8, 12, 10, 3, 0),
                reason="Re-open exact selection after reviewing the redirect",
                evidence=EVIDENCE,
            ),
        )
        with pytest.raises(IrAuthorizationError, match="approved endpoint policy"):
            append_decision(
                connection,
                IrDecisionRequest(
                    request_id="decision-rbrk-exact-wrong",
                    candidate_id=candidate.candidate_id,
                    action=DecisionAction.SELECT_EXACT,
                    expected_revision=3,
                    owner_actor="owner@example.test",
                    decided_at=datetime(2026, 8, 12, 10, 3, 0),
                    reason="Wrong exact URL",
                    evidence=EVIDENCE,
                    selected_url="https://evil.example.test/not-the-candidate.pdf",
                    selected_doc_type=candidate.doc_type,
                    selected_content_sha256=SELECTED_CONTENT_SHA256,
                ),
            )
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 3


def test_admission_requires_current_selection_and_revalidates_bytes_and_redirects(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = _database(tmp_path, migrated_db)
    catalog = _wix_catalog()
    candidate_url = _first_document_url(catalog)
    request = IrCandidateRequest(
        request_id="candidate-wix-admission",
        ticker="WIX",
        catalog=catalog,
        candidate_url=candidate_url,
        recorded_by="pipeline:approved-ir-catalog",
        recorded_at=NOW,
        reason="Review candidate for later admission",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        candidate = persist_candidate(connection, request).candidate
        approval = append_decision(
            connection,
            IrDecisionRequest(
                request_id="decision-wix-admission-approve",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.APPROVE,
                expected_revision=0,
                owner_actor="owner@example.test",
                decided_at=NOW,
                reason="Approve the catalog evidence",
                evidence=EVIDENCE,
            ),
        ).decision
        owner_selected_url = "https://static.wixstatic.com/media/owner-confirmed-report.pdf"
        selected = append_decision(
            connection,
            IrDecisionRequest(
                request_id="decision-wix-admission-select",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.SELECT_EXACT,
                expected_revision=approval.revision,
                owner_actor="owner@example.test",
                decided_at=datetime(2026, 8, 12, 10, 4, 0),
                reason="Select the exact observed Wix document",
                evidence=EVIDENCE,
                selected_url=owner_selected_url,
                selected_doc_type=candidate.doc_type,
                selected_content_sha256=SELECTED_CONTENT_SHA256,
            ),
        ).decision
        proof = IrAdmissionProof(
            candidate_id=candidate.candidate_id,
            selection_decision_id=selected.decision_id,
            issuer_id=candidate.issuer_id,
            ticker=candidate.ticker,
            quarter_end=candidate.quarter_end,
            doc_type=DocType(candidate.doc_type),
            canonical_url=owner_selected_url,
            final_url=owner_selected_url,
            issuer_policy_sha256=candidate.issuer_policy_sha256,
            catalog_sha256=candidate.catalog_sha256,
            observation_key=candidate.observation_key,
            observation_raw_sha256=candidate.observation_raw_sha256,
            captured_content_sha256=SELECTED_CONTENT_SHA256,
            captured_at=datetime(2026, 8, 12, 10, 5, 0),
        )
        verified = verify_admission(connection, proof)
        assert verified.candidate_id == candidate.candidate_id
        assert verified.captured_content_sha256 == SELECTED_CONTENT_SHA256

        with pytest.raises(IrAuthorizationError, match="captured bytes"):
            verify_admission(
                connection,
                proof.model_copy(update={"captured_content_sha256": "2" * 64}),
            )

        with pytest.raises(IrAuthorizationError, match="redirect"):
            verify_admission(
                connection,
                proof.model_copy(update={"final_url": "https://evil.example.test/report.pdf"}),
            )
        with pytest.raises(IrAuthorizationError, match="observation hash"):
            verify_admission(
                connection,
                proof.model_copy(update={"observation_raw_sha256": "2" * 64}),
            )
        with pytest.raises(IrAuthorizationError, match="catalog hash"):
            verify_admission(
                connection,
                proof.model_copy(update={"catalog_sha256": "3" * 64}),
            )
