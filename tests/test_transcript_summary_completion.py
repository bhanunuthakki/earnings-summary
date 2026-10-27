"""Structural tests for exact-document transcript summary completion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from llm.cli import LLMCallReceipt, LLMCallResult, PreparedLLMCall
from llm_artifact_store import (
    DocumentProductArtifact,
    DocumentProductGeneration,
    DocumentProductKey,
    DocumentProductStoreError,
    DocumentProductVerificationReceipt,
)
from pipeline import transcript_summary as summary


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass
class _CapabilityProfile:
    min_context_length: int = 0


@dataclass
class _FakeConnection:
    in_transaction: bool = False
    commits: int = 0
    rollbacks: int = 0

    def execute(self, sql: str, _params: object = ()) -> object:
        if sql == "BEGIN IMMEDIATE":
            self.in_transaction = True
        return object()

    def commit(self) -> None:
        assert self.in_transaction
        self.in_transaction = False
        self.commits += 1

    def rollback(self) -> None:
        self.in_transaction = False
        self.rollbacks += 1


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, *_exc: object) -> None:
        return None


class _Verifier:
    def __init__(self, receipt: DocumentProductVerificationReceipt | None = None) -> None:
        self.receipt = receipt or DocumentProductVerificationReceipt(
            receipt_json='{"schema_version":"test-only"}',
            receipt_sha256="0" * 64,
        )
        self.audit_calls = 0
        self.preflight_calls: list[tuple[DocumentProductKey, str]] = []

    def __call__(self, _conn: object, _envelope: object) -> None:
        return None

    def preflight(self, key: DocumentProductKey, input_manifest_json: str, /) -> None:
        self.preflight_calls.append((key, input_manifest_json))

    def audit(
        self,
        _key: DocumentProductKey,
        _generation: DocumentProductGeneration,
        _body_bytes: bytes,
    ) -> DocumentProductVerificationReceipt:
        self.audit_calls += 1
        return self.receipt


def _artifact() -> DocumentProductArtifact:
    key = DocumentProductKey(
        issuer_id="issuer-1",
        document_version_id="doc-ver-1",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        period_type="Q1",
        fiscal_year=2026,
        fiscal_quarter=1,
        product="transcript_summary",
    )
    body = b"Verified structure only; this is not a semantic qualification."
    generation = DocumentProductGeneration(
        input_manifest_json="{}",
        input_sha256="1" * 64,
        generation_id="generation-1",
        generated_at=datetime.now(UTC).isoformat(),
        model="test-model",
        backend="test-backend",
        prompt_version="test-v1",
        prompt_sha256="2" * 64,
    )
    return DocumentProductArtifact(
        artifact_id=41,
        key=key,
        generation=generation,
        body_bytes=body,
        body_sha256=_digest(body),
        input_sha256=generation.input_sha256,
        output_sha256="3" * 64,
    )


def _source_manifest() -> dict[str, object]:
    return {
        "document_version_id": "doc-ver-1",
        "issuer_id": "issuer-1",
        "ticker": "GOOG",
        "document_type": "transcript",
        "source_ref": "https://example.test/transcript.txt",
        "blob_sha256": "a" * 64,
        "raw_byte_size": 12,
        "binding_kind": "native",
        "recorded_issuer_id": "issuer-1",
        "binding_revision_id": None,
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "fiscal_selection_request": {
            "document_version_id": "doc-ver-1",
            "fiscal_year": None,
            "fiscal_quarter": None,
        },
        "fiscal_selection_proof": {"request": {"fiscal_year": 2026}},
        "extractor": {
            "name": "text",
            "code_version": "test-v1",
            "config_sha256": "e" * 64,
            "approved_extraction_run_id": None,
            "selection_cutoff": None,
        },
        "fulltext_sha256": "b" * 64,
        "consumed_fulltext_sha256": "c" * 64,
        "fulltext_characters": 12,
        "prompt_text_limit": 60_000,
    }


def _install_common(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source_changes: bool = False,
) -> tuple[_FakeConnection, list[object], list[object], list[object], _Verifier]:
    conn = _FakeConnection()

    # Production resolves this explicit authority path strictly. The fake
    # connection below isolates transaction ordering; this file satisfies only
    # the path boundary and does not claim to be a migrated integration DB.
    (tmp_path / "authority.sqlite").touch()

    def fake_connect(*_args: object, **_kwargs: object) -> _ConnectionContext:
        return _ConnectionContext(conn)

    monkeypatch.setattr(
        summary,
        "connect_sqlite",
        fake_connect,
    )
    source_calls = 0
    original_manifest = _source_manifest()

    def resolve_source(*_args: object, **_kwargs: object) -> tuple[dict[str, object], str, str]:
        nonlocal source_calls
        source_calls += 1
        manifest = dict(original_manifest)
        if source_changes and source_calls >= 2:
            manifest["blob_sha256"] = "d" * 64
        return manifest, "full registered transcript text", "https://example.test/transcript.txt"

    monkeypatch.setattr(summary, "resolve_transcript_summary_source", resolve_source)

    def anchor_text(*_args: object) -> str:
        return "actual test anchor input"

    def compose_anchors(*_args: object) -> str:
        return "composed test anchors"

    for name in ("load_thesis_anchor", "load_bear_anchor", "load_ir_anchor"):
        monkeypatch.setattr(summary, name, anchor_text)
    monkeypatch.setattr(summary, "compose_anchor_block", compose_anchors)
    profile = _CapabilityProfile()

    def prepare(prompt: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            prompt=prompt,
            purpose=kwargs["purpose"],
            scope=kwargs["scope"],
            model="test-model",
            backend="test-backend",
            requested_backend=None,
            capability_profile=profile,
            db_path=kwargs["db_path"],
            prompt_version="transcript-summary-test-v1",
            fallback_disabled=True,
            routing_config_sha256="route-fingerprint",
            prompt_sha256=_digest(prompt.encode("utf-8")),
        )

    monkeypatch.setattr(summary, "prepare_llm_call", prepare)
    key_inputs: list[object] = []

    def pipeline_key(_directive: str, _tickers: list[str], inputs: object) -> str:
        key_inputs.append(inputs)
        return "pipeline-exact-key"

    reservations: list[object] = []

    def start_run(*_args: object, **kwargs: object) -> str:
        reservations.append(kwargs)
        return "attempt-1"

    monkeypatch.setattr(summary, "make_pipeline_key", pipeline_key)
    monkeypatch.setattr(summary, "start_run", start_run)

    def ownership_ok(_conn: object, **_kwargs: object) -> None:
        return None

    def end_owned(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(summary, "assert_attempt_owned", ownership_ok)
    monkeypatch.setattr(summary, "end_run_owned", end_owned)
    stages: list[object] = []

    def stage(*_args: object, **kwargs: object) -> None:
        stages.append(kwargs)

    monkeypatch.setattr(summary, "record_artifact_stage_owned", stage)
    verifier = _Verifier()
    execute_calls: list[object] = []

    def execute(prepared: object, **_kwargs: object) -> LLMCallResult:
        execute_calls.append(conn.in_transaction)
        response = "A transcript summary with no qualification claim."
        prepared_call = cast(PreparedLLMCall, prepared)
        receipt = LLMCallReceipt(
            generation_id="00000000-0000-0000-0000-000000000001",
            purpose="transcript_summary",
            prompt_sha256=prepared_call.prompt_sha256,
            response_sha256=_digest(response.encode()),
            model="test-physical-model",
            backend="test-backend",
            generated_at=datetime.now(UTC),
            routing_config_sha256="route-fingerprint",
        )
        return LLMCallResult(response, receipt)

    monkeypatch.setattr(summary, "execute_prepared_call", execute)

    def no_current(*_args: object, **_kwargs: object) -> None:
        return None

    def persist_artifact(*_args: object, **_kwargs: object) -> DocumentProductArtifact:
        return _artifact()

    monkeypatch.setattr(summary, "read_document_current", no_current)
    monkeypatch.setattr(summary, "persist_document_generated_strict", persist_artifact)
    return conn, execute_calls, stages, [key_inputs, reservations], verifier


def _invoke(tmp_path: Path, verifier: _Verifier) -> summary.TranscriptSummaryResult:
    return summary.generate_transcript_summary(
        "doc-ver-1",
        db_path=tmp_path / "authority.sqlite",
        state_root=tmp_path,
        verifier=cast(summary.TranscriptSummaryVerifier, verifier),
        prepare_call=summary.prepare_llm_call,
        execute_call=summary.execute_prepared_call,
    )


def test_exact_source_identity_and_clean_generation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, executions, stages, captured, verifier = _install_common(monkeypatch, tmp_path)
    persisted: list[object] = []

    def persist(_conn: object, envelope: object, *, verifier: object) -> DocumentProductArtifact:
        persisted.append(envelope)
        return _artifact()

    monkeypatch.setattr(summary, "persist_document_generated_strict", persist)
    result = _invoke(tmp_path, verifier)

    assert result.document_version_id == "doc-ver-1"
    assert executions == [False]
    assert conn.commits == 2
    assert len(persisted) == 1
    envelope = cast(summary.DocumentProductEnvelope, persisted[0])
    assert envelope.key.document_version_id == "doc-ver-1"
    assert envelope.key.product == "transcript_summary"
    manifest = json.loads(envelope.generation.input_manifest_json)
    assert manifest["fiscal_period_selection"]["selection_request"]["fiscal_year"] is None
    assert (
        manifest["fiscal_period_selection"]["selection_proof"]["request"]["request"]["fiscal_year"]
        == 2026
    )
    assert manifest["document"]["raw_blob_sha256"] == "a" * 64
    assert manifest["extraction"]["fulltext_sha256"] == "b" * 64
    assert manifest["consumed_text_sha256"] == ["c" * 64]
    assert manifest["anchors_context"]["thesis"] == "actual test anchor input"
    key_inputs, reservations = cast(list[list[object]], captured)
    exact_key = cast(dict[str, object], key_inputs[0])
    reservation = cast(dict[str, object], reservations[0])
    assert exact_key["document_version_id"] == "doc-ver-1"
    assert exact_key["product"] == "transcript_summary"
    assert "input_sha256" not in exact_key
    assert reservation["invocation_inputs"] == exact_key
    assert len(stages) == 2
    assert len(verifier.preflight_calls) == 1
    assert verifier.preflight_calls[0][0] == envelope.key
    assert verifier.preflight_calls[0][1] == envelope.generation.input_manifest_json


def test_verified_artifact_repairs_immutable_projection_without_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, executions, stages, _captured, verifier = _install_common(monkeypatch, tmp_path)
    artifact = _artifact()

    def current_artifact(*_args: object, **_kwargs: object) -> DocumentProductArtifact:
        return artifact

    monkeypatch.setattr(summary, "read_document_current", current_artifact)
    result = _invoke(tmp_path, verifier)

    assert result.status == "repaired_projection"
    assert result.artifact_id == artifact.artifact_id
    assert executions == []
    assert stages == [
        {
            "run_id": "attempt-1",
            "pipeline_key": "pipeline-exact-key",
            "ticker": "GOOG",
            "stage": summary.StageName.PUBLISH,
            "artifact_id": artifact.artifact_id,
            "artifact_output_sha256": artifact.output_sha256,
            "projection_sha256": artifact.body_sha256,
            "period_end": datetime(2026, 3, 31),
        }
    ]
    assert conn.commits == 1
    assert result.projection_path is not None
    assert result.projection_path.read_bytes() == artifact.body_bytes


def test_source_drift_after_call_prevents_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conn, executions, _stages, _captured, verifier = _install_common(
        monkeypatch, tmp_path, source_changes=True
    )
    persisted: list[object] = []

    def should_not_persist(*_args: object, **_kwargs: object) -> None:
        persisted.append(True)

    monkeypatch.setattr(summary, "persist_document_generated_strict", should_not_persist)

    with pytest.raises(summary.TranscriptSummaryError, match="source_changed"):
        _invoke(tmp_path, verifier)
    assert executions == [False]
    assert verifier.audit_calls == 0
    assert persisted == []


def test_replaced_attempt_owner_cannot_persist_or_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _executions, _stages, _captured, verifier = _install_common(monkeypatch, tmp_path)
    persisted: list[object] = []

    def replaced(_conn: object, **_kwargs: object) -> None:
        raise RuntimeError("pipeline attempt is no longer owned by this writer")

    monkeypatch.setattr(summary, "assert_attempt_owned", replaced)

    def should_not_persist(*_args: object, **_kwargs: object) -> None:
        persisted.append(True)

    monkeypatch.setattr(summary, "persist_document_generated_strict", should_not_persist)
    with pytest.raises(RuntimeError, match="no longer owned"):
        _invoke(tmp_path, verifier)
    assert persisted == []
    assert conn.rollbacks == 1


def test_audit_failure_prevents_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conn, _executions, _stages, _captured, verifier = _install_common(monkeypatch, tmp_path)
    persisted: list[object] = []

    class FailedAuditVerifier(_Verifier):
        def audit(
            self,
            _key: DocumentProductKey,
            _generation: DocumentProductGeneration,
            _body_bytes: bytes,
        ) -> DocumentProductVerificationReceipt:
            self.audit_calls += 1
            raise DocumentProductStoreError("invalid audit receipt")

    verifier = FailedAuditVerifier()

    def no_current(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(summary, "read_document_current", no_current)

    def persist(_conn: object, _envelope: object, *, verifier: object) -> DocumentProductArtifact:
        persisted.append(True)
        return _artifact()

    monkeypatch.setattr(summary, "persist_document_generated_strict", persist)
    with pytest.raises(DocumentProductStoreError, match="invalid audit receipt"):
        _invoke(tmp_path, verifier)
    assert verifier.audit_calls == 1
    assert persisted == []


def test_corpus_preflight_failure_prevents_call_and_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conn, executions, stages, captured, _verifier = _install_common(monkeypatch, tmp_path)
    persisted: list[object] = []

    class FailedPreflightVerifier(_Verifier):
        def preflight(self, _key: DocumentProductKey, _manifest_json: str, /) -> None:
            self.preflight_calls.append((_key, _manifest_json))
            raise DocumentProductStoreError("corpus preflight failed")

    verifier = FailedPreflightVerifier()

    def should_not_persist(*_args: object, **_kwargs: object) -> None:
        persisted.append(True)

    monkeypatch.setattr(summary, "persist_document_generated_strict", should_not_persist)
    with pytest.raises(DocumentProductStoreError, match="corpus preflight failed"):
        _invoke(tmp_path, verifier)

    _key_inputs, reservations = cast(list[list[object]], captured)
    assert verifier.preflight_calls
    assert executions == []
    assert reservations == []
    assert stages == []
    assert persisted == []
