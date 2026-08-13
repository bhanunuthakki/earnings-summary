"""Exact-byte IR capture binds approval, bytes, selection, and evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from ir_pipeline import evidence_capture
from ir_pipeline.evidence_capture import SessionLike
from pipeline.approved_ir_catalog import build_catalog
from pipeline.approved_ir_rubrik import load_rubrik_row_observations, parse_rubrik_quarter_rows
from pipeline.ir_approval_capture import (
    ExactIrCaptureActionInput,
    ExactIrCaptureError,
    ExactIrCaptureHardStopError,
    ExactIrCaptureReceipt,
    capture_and_admit_exact_ir_document,
)
from pipeline.ir_approval_store import (
    DecisionAction,
    EvidenceReference,
    IrCandidateRequest,
    IrDecisionRequest,
    append_decision,
    get_current_decision,
    persist_candidate,
)
from pipeline.source_policy import issuer_policy

FIXTURE = Path(__file__).parent / "fixtures" / "approved_ir" / "rubrik_rows_sanitized.json"
URL = (
    "https://s203.q4cdn.com/667520861/files/doc_financials/2027/q1/"
    "RBRK-Q1-FY27-Investor-Presentation-FINAL.pdf"
)
NOW = datetime(2026, 8, 12, 15, 0, 0)
PDF = b"%PDF-1.7\nserver-owned exact bytes\n%%EOF\n"


class Response:
    def __init__(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.status_code, self.body, self.headers = status, body, headers

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.body

    def close(self) -> None:
        pass


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.responses, self.urls = responses, []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> Response:
        del headers, timeout, stream, allow_redirects
        self.urls.append(url)
        return self.responses.pop(0)


def _approved_candidate(path: Path) -> str:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload[0]["links"][0].update(url=URL, declared_kind="presentation")
    policy = issuer_policy("RBRK")
    observations = load_rubrik_row_observations(json.dumps(payload))
    catalog = build_catalog(policy, parse_rubrik_quarter_rows(observations, policy=policy))
    evidence = (EvidenceReference(evidence_id="owner-approved-url", locator=URL),)
    with sqlite3.connect(path) as conn:
        candidate = persist_candidate(
            conn,
            IrCandidateRequest(
                request_id="capture-candidate",
                ticker="RBRK",
                catalog=catalog,
                candidate_url=URL,
                recorded_by="test",
                recorded_at=NOW,
                reason="Owner supplied exact URL",
                evidence=evidence,
            ),
        ).candidate
        append_decision(
            conn,
            IrDecisionRequest(
                request_id="capture-approve",
                candidate_id=candidate.candidate_id,
                action=DecisionAction.APPROVE,
                expected_revision=0,
                owner_actor="owner@example.test",
                decided_at=NOW,
                reason="Approved exact scope",
                evidence=evidence,
            ),
        )
        conn.commit()
    return candidate.candidate_id


def _run(
    path: Path, root: Path, candidate_id: str, session: Session, task_id: str
) -> ExactIrCaptureReceipt:
    return capture_and_admit_exact_ir_document(
        path,
        ExactIrCaptureActionInput(candidate_id=candidate_id, reason="Capture approved bytes"),
        owner_actor="owner@example.test",
        checkpoint_root=root / "checkpoints",
        blob_root=root / "blobs",
        task_id=task_id,
        user_agent="earnings-summary-test/1.0",
        session=cast(SessionLike, session),
        robots_allows=lambda _url, _agent: True,
        now=lambda: datetime(2026, 8, 12, 15, 1, 0),
    )


def _fail_if_dns_resolves(_url: str) -> str:
    raise AssertionError("replay resolved DNS")


def test_capture_selects_server_hash_and_admits_evidence_atomically(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "capture.db")
    candidate_id = _approved_candidate(path)
    session = Session([Response(200, PDF, **{"Content-Type": "application/pdf"})])
    receipt = _run(path, tmp_path, candidate_id, session, "rbrk-exact")
    assert (receipt.outcome, receipt.network_fetched, receipt.final_url) == (
        "admitted",
        True,
        URL,
    )
    assert (receipt.media_type, receipt.byte_size, len(receipt.content_sha256)) == (
        "application/pdf",
        len(PDF),
        64,
    )
    assert session.urls == [URL]
    with sqlite3.connect(path) as conn:
        current = get_current_decision(conn, candidate_id)
        assert current is not None and current.action is DecisionAction.SELECT_EXACT
        assert current.selected_content_sha256 == receipt.content_sha256
        for table in (
            "evidence_content_blobs",
            "evidence_source_observations",
            "evidence_document_versions",
            "evidence_document_observation_links",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)  # nosec B608

    replay_session = Session([])
    monkeypatch.setattr(
        evidence_capture,
        "ensure_safe_public_url",
        _fail_if_dns_resolves,
    )
    replay = _run(path, tmp_path, candidate_id, replay_session, "rbrk-exact")
    assert (replay.outcome, replay.network_fetched, replay.content_sha256) == (
        "exact_replay",
        False,
        receipt.content_sha256,
    )
    assert replay_session.urls == []

    monkeypatch.undo()

    new_task_session = Session([Response(200, PDF, **{"Content-Type": "application/pdf"})])
    cross_task = _run(path, tmp_path, candidate_id, new_task_session, "rbrk-exact-new-task")
    assert (cross_task.outcome, cross_task.network_fetched) == ("exact_replay", True)
    assert cross_task.content_sha256 == receipt.content_sha256


@pytest.mark.parametrize(
    ("responses", "error_type", "message"),
    [
        (
            [Response(302, Location="https://evil.example.test/report.pdf")],
            ExactIrCaptureError,
            "redirect",
        ),
        ([Response(403)], ExactIrCaptureHardStopError, "authorization"),
        (
            [Response(200, b"<html>not a document</html>", **{"Content-Type": "application/pdf"})],
            ExactIrCaptureError,
            "not a PDF",
        ),
    ],
)
def test_capture_fails_closed_without_selection_or_evidence(
    responses: list[Response],
    error_type: type[Exception],
    message: str,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "failure.db")
    candidate_id = _approved_candidate(path)
    with pytest.raises(error_type, match=message):
        _run(path, tmp_path, candidate_id, Session(responses), "rbrk-failure")
    with sqlite3.connect(path) as conn:
        current = get_current_decision(conn, candidate_id)
        assert current is not None and current.action is DecisionAction.APPROVE
        assert conn.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone() == (0,)
    assert not any((tmp_path / "blobs").rglob("*"))


def test_exact_capture_never_dereferences_even_an_authorized_redirect(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "redirect.db")
    candidate_id = _approved_candidate(path)
    session = Session([Response(302, Location=URL.replace("FINAL.pdf", "v2.pdf"))])
    with pytest.raises(ExactIrCaptureError, match="redirect_budget_exhausted"):
        _run(path, tmp_path, candidate_id, session, "rbrk-no-redirect")
    assert session.urls == [URL]


def test_existing_blob_metadata_mismatch_rolls_back_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "metadata.db")
    candidate_id = _approved_candidate(path)
    digest = hashlib.sha256(PDF).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO evidence_content_blobs "
            "(sha256,byte_size,media_type,storage_uri,recorded_at) VALUES (?,?,?,?,?)",
            (digest, len(PDF) + 1, "application/pdf", "file:///corrupt", NOW),
        )
        conn.commit()
    with pytest.raises(ExactIrCaptureError, match="blob metadata conflicts"):
        _run(
            path,
            tmp_path,
            candidate_id,
            Session([Response(200, PDF, **{"Content-Type": "application/pdf"})]),
            "rbrk-metadata-conflict",
        )
    with sqlite3.connect(path) as conn:
        current = get_current_decision(conn, candidate_id)
        assert current is not None and current.action is DecisionAction.APPROVE


def test_browser_capture_input_rejects_identity_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        ExactIrCaptureActionInput.model_validate(
            {
                "candidate_id": "a" * 64,
                "reason": "capture",
                "selected_url": URL,
                "content_sha256": "b" * 64,
            }
        )
