"""Transcript audit routing and budget failures cannot silently use generic Ask."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from ask import claims
from ask.claims import DOCUMENT_AUDIT_PURPOSE, PURPOSE
from ask.grounding import EvidenceItem
from llm.cli import LLM_MODELS, model_for_subscription_backend, prepare_llm_call
from llm.prompt_versions import prompt_version_for
from llm.resolver import structured_capability_profile
from llm_artifact_store import (
    DocumentProductEnvelope,
    DocumentProductGeneration,
    DocumentProductKey,
    DocumentProductVerificationReceipt,
)
from llm_budget import require_explicit_blocking_budget
from pipeline import transcript_summary_verification as verification
from pipeline.transcript_summary_verification import (
    TranscriptAuditEvaluation,
    TranscriptAuditPhysicalCall,
    TranscriptAuditQualification,
    TranscriptAuditQualificationSet,
    TranscriptSummaryVerificationError,
    TranscriptSummaryVerifier,
)


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


@pytest.fixture(autouse=True)
def synthetic_implementation_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # These are route-unit fixtures, not proof of an installed fleet implementation.
    monkeypatch.setattr(verification, "transcript_audit_code_sha256", lambda: "c" * 64)


def _qualified_profile(
    *,
    model: str,
    backend: str,
    route_sha: str,
    expires_at: datetime | None = None,
    launch_profile: Literal["manual", "cron"] = "manual",
) -> TranscriptAuditQualification:
    now = datetime.now(UTC)
    return TranscriptAuditQualification(
        launch_profile=launch_profile,
        schema_version="transcript-summary-audit-qualification@1",
        status="qualified",
        purpose=DOCUMENT_AUDIT_PURPOSE,
        model=model,
        backend=backend,
        routing_config_sha256=route_sha,
        audit_code_sha256=verification.transcript_audit_code_sha256(),
        corpus_sha256=verification.transcript_audit_corpus_sha256(),
        proof_path="evals/qualification/synthetic-route-test.json",
        proof_sha256="0" * 64,
        qualified_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(expires_at or now + timedelta(days=1)).isoformat(),
        scope="registered_original_transcript_full_context@1",
        max_source_characters=120_000,
        max_candidate_characters=80_000,
        limitations=["Synthetic route fixture; proof validation is stubbed."],
    )


def _install_two_profile_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: Path | None = None,
    selected_model: str = "manual-model",
    selected_backend: str = "manual-backend",
    selected_route_sha: str = "b" * 64,
) -> tuple[TranscriptSummaryVerifier, TranscriptAuditQualification, TranscriptAuditQualification]:
    cron = _qualified_profile(
        model="cron-model", backend="cron-backend", route_sha="a" * 64, launch_profile="cron"
    )
    manual = _qualified_profile(model="manual-model", backend="manual-backend", route_sha="b" * 64)
    bundle = TranscriptAuditQualificationSet(
        schema_version="transcript-summary-audit-qualification-set@1",
        profiles=[cron, manual],
    )
    qualification_path = tmp_path / "synthetic-qualification.json"
    qualification_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        "pipeline.transcript_summary_verification._QUALIFICATION", qualification_path
    )

    def skip_proof_validation(
        _self: TranscriptSummaryVerifier,
        _profile: TranscriptAuditQualification,
        *,
        runs: set[str],
        generations: set[str],
    ) -> None:
        # Route-unit tests verify profile selection, not qualification evidence.
        return None

    monkeypatch.setattr(TranscriptSummaryVerifier, "_validate_profile_proof", skip_proof_validation)

    def prepared_route(_prompt: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            purpose=kwargs["purpose"],
            model=selected_model,
            backend=selected_backend,
            routing_config_sha256=selected_route_sha,
        )

    def identity_model(model: str, _backend: str) -> str:
        return model

    def selected_route_hash(_purpose: str) -> str:
        return selected_route_sha

    monkeypatch.setattr(verification, "prepare_llm_call", prepared_route)
    monkeypatch.setattr(verification, "model_for_subscription_backend", identity_model)
    monkeypatch.setattr(verification, "routing_config_sha256", selected_route_hash)
    authority = database or (tmp_path / "authority.sqlite")
    if not authority.exists():
        authority.touch()
    verifier = TranscriptSummaryVerifier(authority, tmp_path)
    return verifier, cron, manual


def _stored_cron_audit(
    verifier: TranscriptSummaryVerifier,
    cron: TranscriptAuditQualification,
    *,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DocumentProductEnvelope, list[EvidenceItem]]:
    body = "Revenue was $10 million."
    source = "Revenue was $10 million."
    items = [
        EvidenceItem(
            n=1,
            kind="transcript",
            label="TEST Q1 2026 transcript",
            text=source,
            doc_id=None,
            href=None,
            source_url="https://example.test/transcript",
            ticker="TEST",
            doc_type="transcript",
            period="2026-03-31",
        )
    ]
    response = json.dumps(
        {
            "spans": [
                {
                    "span_id": "s1",
                    "quote": body,
                    "kind": "reported_fact",
                    "supported": True,
                    "context": {
                        "issuer": "TEST",
                        "metric": "revenue",
                        "period": "Q1 2026",
                        "unit": "USD millions",
                        "scope": "consolidated",
                        "basis": "reported",
                        "actual_vs_forecast": "actual",
                        "comparison": "unknown",
                        "attribution": "management",
                    },
                    "support": [{"evidence_n": 1, "excerpt": source}],
                    "reason": "The exact transcript excerpt supports the claim.",
                }
            ]
        },
        separators=(",", ":"),
    )
    prompts: list[str] = []

    def offline_structured_call(prompt: str, **kwargs: object) -> object:
        schema = kwargs.get("schema")
        if not isinstance(schema, TypeAdapter):
            raise AssertionError("the audit must use its supplied structured schema")
        adapter = cast(TypeAdapter[object], schema)
        prompts.append(prompt)
        return adapter.validate_json(response)

    monkeypatch.setattr(claims, "call_llm_structured", offline_structured_call)

    audited = claims.audit_document_narrative(body, items, db_path=database)
    assert len(prompts) == 1
    call = TranscriptAuditPhysicalCall(
        purpose=DOCUMENT_AUDIT_PURPOSE,
        prompt=prompts[0],
        response=response,
        generation_id="00000000-0000-0000-0000-000000000001",
        generated_at=datetime.now(UTC).isoformat(),
        prompt_sha256=_sha(prompts[0].encode()),
        response_sha256=_sha(response.encode()),
        model=cron.model,
        backend=cron.backend,
        routing_config_sha256=cron.routing_config_sha256,
    )
    key = DocumentProductKey(
        issuer_id="issuer-test",
        document_version_id="document-test",
        period_start=None,
        period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
        period_type="Q1",
        fiscal_year=2026,
        fiscal_quarter=1,
        product="transcript_summary",
    )
    generation = DocumentProductGeneration(
        input_manifest_json="{}",
        input_sha256="c" * 64,
        generation_id="summary-generation-test",
        generated_at=datetime.now(UTC).isoformat(),
        model="summary-model",
        backend="summary-backend",
        prompt_version="summary-test-v1",
        prompt_sha256="d" * 64,
    )
    body_bytes = body.encode()
    serialized_corpus = json.dumps(
        [asdict(item) for item in items],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    verification_payload = {
        "purpose": DOCUMENT_AUDIT_PURPOSE,
        "schema_version": "transcript-summary-verification@1",
        "body_sha256": _sha(body_bytes),
        "input_sha256": generation.input_sha256,
        "audit_code_sha256": verification.transcript_audit_code_sha256(),
        "qualification_sha256": _sha(verifier.qualification_bytes),
        "corpus_sha256": _sha(serialized_corpus.encode()),
        "records": audited.raw_payload(),
        "calls": [call.model_dump(mode="json")],
    }
    receipt_json = json.dumps(
        verification_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    envelope = DocumentProductEnvelope(
        key=key,
        generation=generation,
        body_bytes=body_bytes,
        body_sha256=_sha(body_bytes),
        verification=DocumentProductVerificationReceipt(
            receipt_json=receipt_json,
            receipt_sha256=_sha(receipt_json.encode()),
        ),
    )

    def source_corpus(
        _conn: sqlite3.Connection, _key: DocumentProductKey, _manifest: str
    ) -> list[EvidenceItem]:
        return items

    monkeypatch.setattr(verifier, "_corpus", source_corpus)
    return envelope, items


def test_default_document_audit_route_is_separate_from_generic_ask(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    monkeypatch.setenv("AGENT_INSTRUCTIONS_HOME", str(tmp_path / "absent-fleet"))
    monkeypatch.setenv("LLM_PRIMARY_SUBSCRIPTION_BACKEND", "codex")
    monkeypatch.setenv("LLM_SUBSCRIPTION_FALLBACK_DISABLED", "1")
    monkeypatch.delenv("LLM_EXPLICIT_MODEL_POLICY", raising=False)
    require_explicit_blocking_budget(DOCUMENT_AUDIT_PURPOSE, db_path=database)
    prepared = prepare_llm_call(
        "Synthetic route preflight.",
        purpose=DOCUMENT_AUDIT_PURPOSE,
        scope="document_summary",
        capability_profile=structured_capability_profile(),
        db_path=database,
    )
    assert prepared.purpose == DOCUMENT_AUDIT_PURPOSE
    assert prepared.backend == "codex"
    assert model_for_subscription_backend(prepared.model, prepared.backend) == "gpt-6.1-sol"
    assert prompt_version_for(DOCUMENT_AUDIT_PURPOSE) == "v1"
    assert PURPOSE == "ask_claim_grounding"
    assert LLM_MODELS[PURPOSE] != LLM_MODELS[DOCUMENT_AUDIT_PURPOSE]


@pytest.mark.parametrize("case", ["missing", "zero", "negative", "infinite", "warn", "skip"])
def test_document_audit_requires_its_own_positive_blocking_budget(
    tmp_path: Path, migrated_db: Callable[..., Path], case: str
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        if case == "missing":
            conn.execute("DELETE FROM llm_budgets WHERE purpose=?", (DOCUMENT_AUDIT_PURPOSE,))
        else:
            cap = {"zero": 0, "negative": -1, "infinite": float("inf")}.get(case, 5)
            mode = case if case in {"warn", "skip"} else "block"
            conn.execute(
                "UPDATE llm_budgets SET monthly_cap_usd=?,on_exceed=? WHERE purpose=?",
                (cap, mode, DOCUMENT_AUDIT_PURPOSE),
            )
    with pytest.raises(ValueError, match="budget"):
        require_explicit_blocking_budget(DOCUMENT_AUDIT_PURPOSE, db_path=database)


@pytest.mark.parametrize(
    "schema", [TranscriptAuditQualification, TranscriptAuditEvaluation, TranscriptAuditPhysicalCall]
)
def test_qualification_and_physical_proofs_require_the_document_purpose(
    schema: type[TranscriptAuditQualification]
    | type[TranscriptAuditEvaluation]
    | type[TranscriptAuditPhysicalCall],
) -> None:
    for data in ({}, {"purpose": PURPOSE}):
        with pytest.raises(ValidationError) as error:
            schema.model_validate(data)
        assert any(item["loc"] == ("purpose",) for item in error.value.errors())


def test_qualification_set_requires_two_distinct_routes_with_one_scope() -> None:
    cron = _qualified_profile(
        model="cron-model", backend="cron", route_sha="a" * 64, launch_profile="cron"
    )
    manual = _qualified_profile(model="manual-model", backend="manual", route_sha="b" * 64)

    duplicate_route = manual.model_copy(
        update={
            "model": cron.model,
            "backend": cron.backend,
            "routing_config_sha256": cron.routing_config_sha256,
        }
    )
    with pytest.raises(ValidationError, match="distinct routes"):
        TranscriptAuditQualificationSet.model_validate(
            {
                "schema_version": "transcript-summary-audit-qualification-set@1",
                "profiles": [cron.model_dump(), duplicate_route.model_dump()],
            }
        )

    mixed_scope = manual.model_copy(update={"max_source_characters": 60_000})
    with pytest.raises(ValidationError, match="one complete scope"):
        TranscriptAuditQualificationSet.model_validate(
            {
                "schema_version": "transcript-summary-audit-qualification-set@1",
                "profiles": [cron.model_dump(), mixed_scope.model_dump()],
            }
        )

    third = _qualified_profile(model="third-model", backend="third", route_sha="c" * 64)
    with pytest.raises(ValidationError):
        TranscriptAuditQualificationSet.model_validate(
            {
                "schema_version": "transcript-summary-audit-qualification-set@1",
                "profiles": [cron.model_dump(), manual.model_dump(), third.model_dump()],
            }
        )


def test_unknown_current_prepared_route_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(TranscriptSummaryVerificationError, match="no current qualified route"):
        _install_two_profile_verifier(
            tmp_path,
            monkeypatch,
            selected_model="unqualified-model",
            selected_backend="unqualified-backend",
            selected_route_sha="e" * 64,
        )


def test_cron_profile_call_replays_under_manual_profile_without_model_calls(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    verifier, cron, manual = _install_two_profile_verifier(tmp_path, monkeypatch, database=database)
    envelope, _items = _stored_cron_audit(
        verifier, cron, database=database, monkeypatch=monkeypatch
    )
    model_calls: list[bool] = []

    def blocked_call(*_args: object, **_kwargs: object) -> object:
        model_calls.append(True)
        raise AssertionError("audit replay must not call a model")

    monkeypatch.setattr(verification, "execute_prepared_call", blocked_call)
    assert verifier.qualification == manual
    assert verifier.qualification.model != cron.model
    with sqlite3.connect(":memory:") as conn:
        verifier(conn, envelope)
    assert model_calls == []


def _change_saved_call(
    envelope: DocumentProductEnvelope, field: str, value: object
) -> DocumentProductEnvelope:
    payload = json.loads(envelope.verification.receipt_json)
    payload["calls"][0][field] = value
    receipt_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return replace(
        envelope,
        verification=DocumentProductVerificationReceipt(
            receipt_json=receipt_json,
            receipt_sha256=_sha(receipt_json.encode()),
        ),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "unqualified-model"),
        ("routing_config_sha256", "f" * 64),
    ],
)
def test_saved_call_rejects_wrong_model_or_route_hash(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    verifier, cron, _manual = _install_two_profile_verifier(
        tmp_path, monkeypatch, database=database
    )
    envelope, _items = _stored_cron_audit(
        verifier, cron, database=database, monkeypatch=monkeypatch
    )
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(TranscriptSummaryVerificationError, match="unqualified route"),
    ):
        verifier(conn, _change_saved_call(envelope, field, value))


def test_saved_call_rejects_wrong_purpose_and_prompt_hash(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    verifier, cron, _manual = _install_two_profile_verifier(
        tmp_path, monkeypatch, database=database
    )
    envelope, _items = _stored_cron_audit(
        verifier, cron, database=database, monkeypatch=monkeypatch
    )
    wrong_purpose = _change_saved_call(envelope, "purpose", "ask_claim_grounding")
    with sqlite3.connect(":memory:") as conn, pytest.raises(ValidationError):
        verifier(conn, wrong_purpose)
    wrong_prompt_hash = _change_saved_call(envelope, "prompt_sha256", "f" * 64)
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(TranscriptSummaryVerificationError, match="physical call seal"),
    ):
        verifier(conn, wrong_prompt_hash)


def test_saved_call_rejects_expired_cron_profile(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    verifier, cron, manual = _install_two_profile_verifier(tmp_path, monkeypatch, database=database)
    envelope, _items = _stored_cron_audit(
        verifier, cron, database=database, monkeypatch=monkeypatch
    )
    expired = cron.model_copy(
        update={"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    )
    verifier.qualifications = (expired, manual)
    assert verifier.qualification == manual
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(TranscriptSummaryVerificationError, match="qualification expired"),
    ):
        verifier(conn, envelope)
