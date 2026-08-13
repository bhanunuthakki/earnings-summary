"""Trusted owner-action seam for immutable IR review candidates."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.approved_ir_catalog import ApprovedIrCatalog, build_catalog
from pipeline.approved_ir_rubrik import load_rubrik_row_observations, parse_rubrik_quarter_rows
from pipeline.ir_approval_actions import (
    IrApprovalActionInput,
    IrApprovalActionUnauthorizedError,
    IrApprovalUiAction,
    IrExactSelectionUnavailableError,
    execute_ir_approval_action,
)
from pipeline.ir_approval_store import EvidenceReference, IrCandidateRequest, persist_candidate
from pipeline.source_policy import issuer_policy

FIXTURE = Path(__file__).parent / "fixtures" / "approved_ir" / "rubrik_rows_sanitized.json"
NOW = datetime(2026, 8, 12, 15, 0, 0)
EVIDENCE = (
    EvidenceReference(
        evidence_id="catalog-review-1",
        locator="review://catalog/rbrk",
        content_sha256="f" * 64,
    ),
)


def _catalog() -> ApprovedIrCatalog:
    policy = issuer_policy("RBRK")
    observations = load_rubrik_row_observations(FIXTURE.read_text(encoding="utf-8"))
    return build_catalog(policy, parse_rubrik_quarter_rows(observations, policy=policy))


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, str, str]:
    path = migrated_db(tmp_path / "ir-actions.db")
    catalog = _catalog()
    candidate_url = next(
        entry.url for entry in catalog.entries if entry.disposition.value == "ir_document"
    )
    request = IrCandidateRequest(
        request_id="candidate-rbrk-ui-actions",
        ticker="RBRK",
        catalog=catalog,
        candidate_url=candidate_url,
        recorded_by="pipeline:approved-ir-catalog",
        recorded_at=NOW,
        reason="Owner review required",
        evidence=EVIDENCE,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        candidate = persist_candidate(connection, request).candidate
    return path, candidate.candidate_id, candidate.observation_raw_sha256


def test_approve_derives_actor_time_evidence_revision_and_replay_identity_server_side(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path, candidate_id, observation_hash = _database(tmp_path, migrated_db)
    action = IrApprovalActionInput(
        candidate_id=candidate_id,
        action=IrApprovalUiAction.APPROVE,
        reason="Visible reporting-period evidence confirmed",
    )

    first = execute_ir_approval_action(path, action, owner_actor="bhanu", now=lambda: NOW)
    replay = execute_ir_approval_action(
        path,
        action,
        owner_actor="bhanu",
        now=lambda: datetime(2026, 8, 12, 15, 5, 0),
    )

    assert first.outcome == "appended"
    assert replay.outcome == "exact_replay"
    assert replay.revision == first.revision == 1
    assert replay.decided_at == first.decided_at == NOW
    assert first.owner_actor == "bhanu"
    assert first.evidence_count == 1
    assert first.selected_content_sha256 is None
    assert observation_hash not in first.model_dump_json()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT request_id,expected_revision,owner_actor,decided_at,evidence_json,"
            "selected_content_sha256 FROM ir_approval_decisions"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0]
    assert count == 1
    assert str(row[0]).startswith("owner-ui:")
    assert row[1:] == (
        0,
        "bhanu",
        NOW.isoformat(),
        '[{"content_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","evidence_id":"catalog-review-1","locator":"review://catalog/rbrk"}]',
        None,
    )


def test_reject_supersedes_approval_using_current_server_revision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path, candidate_id, _observation_hash = _database(tmp_path, migrated_db)
    approve = IrApprovalActionInput(
        candidate_id=candidate_id,
        action=IrApprovalUiAction.APPROVE,
        reason="Initial review passed",
    )
    reject = IrApprovalActionInput(
        candidate_id=candidate_id,
        action=IrApprovalUiAction.REJECT,
        reason="Later evidence shows stale scope",
    )

    execute_ir_approval_action(path, approve, owner_actor="bhanu", now=lambda: NOW)
    receipt = execute_ir_approval_action(path, reject, owner_actor="bhanu", now=lambda: NOW)

    assert receipt.action is IrApprovalUiAction.REJECT
    assert receipt.revision == 2
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT action,expected_revision,revision FROM ir_approval_decisions ORDER BY revision"
        ).fetchall()
    assert rows == [("approve", 0, 1), ("reject", 1, 2)]


def test_exact_replay_rechecks_current_policy_and_fails_closed_when_binding_is_stale(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path, candidate_id, _ = _database(tmp_path, migrated_db)
    action = IrApprovalActionInput(
        candidate_id=candidate_id,
        action=IrApprovalUiAction.APPROVE,
        reason="Visible reporting-period evidence confirmed",
    )
    execute_ir_approval_action(path, action, owner_actor="bhanu", now=lambda: NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER trg_ir_approval_candidates_no_update")
        connection.execute(
            "UPDATE ir_approval_candidates SET issuer_policy_sha256=? WHERE candidate_id=?",
            ("9" * 64, candidate_id),
        )

    with pytest.raises(IrApprovalActionUnauthorizedError, match="policy hash is stale"):
        execute_ir_approval_action(path, action, owner_actor="bhanu", now=lambda: NOW)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 1


def test_exact_selection_fails_closed_without_server_owned_document_byte_hash(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path, candidate_id, observation_hash = _database(tmp_path, migrated_db)
    select = IrApprovalActionInput(
        candidate_id=candidate_id,
        action=IrApprovalUiAction.SELECT_EXACT,
        reason="Select exact bytes",
    )

    with pytest.raises(IrExactSelectionUnavailableError, match="server-owned hash"):
        execute_ir_approval_action(path, select, owner_actor="bhanu", now=lambda: NOW)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_decisions").fetchone()[0] == 0
    assert observation_hash  # proves a catalog-observation hash existed but was not used


def test_browser_payload_rejects_server_owned_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IrApprovalActionInput.model_validate(
            {
                "candidate_id": "a" * 64,
                "action": "approve",
                "reason": "Owner reason",
                "owner_actor": "attacker",
                "expected_revision": 99,
                "decided_at": "2020-01-01T00:00:00",
                "selected_url": "https://attacker.test/file.pdf",
                "selected_doc_type": "ir_press_release",
                "selected_content_sha256": "b" * 64,
                "evidence": [],
                "request_id": "attacker-replay-key",
            }
        )


@pytest.mark.parametrize("reason", ["", "   "])
def test_reason_is_required(reason: str) -> None:
    with pytest.raises(ValidationError, match="reason"):
        IrApprovalActionInput(
            candidate_id="a" * 64,
            action=IrApprovalUiAction.REJECT,
            reason=reason,
        )
