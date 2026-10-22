from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest
from pydantic import TypeAdapter

import ask.engine as engine
import ask.store as store
from ask.audit_store import digest_text
from ask.claims import required_claim_spans
from ask.context import ContextPack
from ask.engine import AskTurn
from ask.sealed_retrieval import SealedEvidenceItem
from llm.prompt_registry import template_meta
from llm.structured import StructuredCallResult
from llm_call_ledger import LlmCallRecord, record_call
from search.embedding_runtime_artifact import (
    RuntimeArtifactSource,
    build_runtime_artifact,
    verify_runtime_artifact,
)

T = TypeVar("T")


def _pack() -> ContextPack:
    return ContextPack(
        scope="portfolio",
        default_tickers=["ACME"],
        system_context="context",
    )


def _fixture_scopes(*_args: object, **_kwargs: object) -> tuple[()]:
    return ()


def test_unsupported_auditor_verdict_never_reaches_delivery_or_assistant_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise delivery, not population admission: retrieval is a fixture boundary."""

    db_path = migrated_db(tmp_path / "delivery.db")
    session = store.create_session(scope="portfolio", title="Synthetic delivery", db_path=db_path)
    answer = "Synthetic revenue was 200 USD [1]."
    calls: list[str] = []
    evidence = SealedEvidenceItem(
        n=1,
        kind="narrative",
        label="Synthetic source",
        text="Synthetic revenue was 100 USD.",
        href="/source/version/synthetic-document",
        source_url="",
        ticker="ACME",
        doc_type="synthetic",
        period=None,
        value=None,
        trace_id="synthetic-trace",
        result_ordinal=0,
        candidate_id="synthetic-node",
        source_commitment_sha256="a" * 64,
        research_snapshot_id="synthetic-snapshot",
        as_of_at=datetime.now(UTC),
        document_version_id="synthetic-document",
        evidence_node_id="synthetic-node",
        node_kind="paragraph",
        locator={},
        score="1",
    )

    def ready(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(outcome="ready")

    def plan(*_args: object, **_kwargs: object) -> None:
        return None

    def execute(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(receipts=(SimpleNamespace(trace_id="synthetic-trace"),))

    def load_evidence(*_args: object, **_kwargs: object) -> list[SealedEvidenceItem]:
        return [evidence]

    monkeypatch.setattr(engine, "_sealed_runtime", lambda: None)
    monkeypatch.setattr(engine, "load_production_scopes", _fixture_scopes)
    monkeypatch.setattr(engine, "assess_retrieval_readiness", ready)
    monkeypatch.setattr(engine, "build_sealed_retrieval_plan", plan)
    monkeypatch.setattr(
        engine,
        "execute_sealed_retrieval_plan",
        execute,
    )
    monkeypatch.setattr(engine, "load_verified_trace_evidence", load_evidence)

    def record(prompt: str, response: str, purpose: str, run_id: str) -> None:
        template_id, template_version, variables_sha = template_meta(prompt)
        call_id = record_call(
            LlmCallRecord(
                called_at=datetime.now(UTC),
                model="deterministic-test",
                provider="fixture",
                transport="in-process",
                auth_class="none",
                prompt_sha256=digest_text(str(prompt)),
                prompt_chars=len(prompt),
                response_sha256=digest_text(response),
                response_chars=len(response),
                elapsed_ms=0,
                cost_estimate_usd=0,
                purpose=purpose,
                scope="portfolio",
                run_id=run_id,
                template_id=template_id,
                template_version=template_version,
                template_vars_sha256=variables_sha,
                outcome="success",
            ),
            db_path=db_path,
        )
        assert call_id is not None
        calls.append(purpose)

    def answer_call(prompt: str, *, purpose: str, run_id: str, **_kwargs: object) -> str:
        record(prompt, answer, purpose, run_id)
        return answer

    def audit_call(
        prompt: str, *, purpose: str, run_id: str, schema: TypeAdapter[T], **_kwargs: object
    ) -> StructuredCallResult[T]:
        raw = json.dumps(
            {
                "claims": [
                    {
                        "char_start": start,
                        "char_end": end,
                        "quote": quote,
                        "cites": [],
                        "supported": False,
                    }
                    for start, end, quote in required_claim_spans(answer)
                ]
            }
        )
        payload = schema.validate_json(raw)
        record(prompt, raw, purpose, run_id)
        return StructuredCallResult(value=payload, raw_response=raw, prompt=prompt)

    monkeypatch.setattr(engine, "call_llm", answer_call)
    monkeypatch.setattr(engine, "call_llm_structured_with_raw", audit_call)
    events = list(
        engine.respond_turn(
            AskTurn(text="What was synthetic revenue?", session_id=session.id),
            _pack(),
            db_path=db_path,
            repo_root=tmp_path,
            retrieval_mode="sealed",
        )
    )
    assert calls == ["ask_answer", "ask_claim_audit"]
    assert [item["type"] for item in events] == ["error"]
    assert "unsupported substantive claim" in capsys.readouterr().err
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM ask_turns WHERE role='assistant'").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM ask_answer_audit_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 2


@pytest.mark.parametrize("component", ["model", "tokenizer"])
def test_runtime_byte_drift_stops_request_before_any_provider_or_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
    component: str,
) -> None:
    """Wire the real byte verifier into a fixture readiness boundary, with no model load."""

    runtime_root = tmp_path / "synthetic-runtime"
    runtime_root.mkdir()
    sources = (
        RuntimeArtifactSource(logical_name="model.bin", role="model", relative_path=Path("model")),
        RuntimeArtifactSource(
            logical_name="tokenizer.json", role="tokenizer", relative_path=Path("tokenizer")
        ),
    )
    for name in ("model", "tokenizer"):
        (runtime_root / name).write_bytes(name.encode())
    artifact = build_runtime_artifact(
        runtime_root,
        sources,
        provider="fastembed",
        model="synthetic/no-model-load",
        dimensions=2,
        execution_provider="CPUExecutionProvider",
        execution_settings={},
        component_versions={"synthetic": "1"},
    )
    verify_runtime_artifact(artifact, runtime_root, sources)
    (runtime_root / component).write_bytes(b"changed")
    db_path = migrated_db(tmp_path / "runtime-drift.db")
    session = store.create_session(scope="portfolio", title="Synthetic runtime", db_path=db_path)
    verified: list[str] = []
    calls: list[str] = []

    def readiness(*_args: object, **_kwargs: object) -> None:
        verified.append(component)
        verify_runtime_artifact(artifact, runtime_root, sources)
        raise AssertionError("Changed runtime must not pass verification")

    def forbidden(*_args: object, **_kwargs: object) -> str:
        calls.append("provider")
        raise AssertionError("A provider must not run after runtime byte drift")

    monkeypatch.setattr(engine, "_sealed_runtime", lambda: None)
    monkeypatch.setattr(engine, "load_production_scopes", _fixture_scopes)
    monkeypatch.setattr(engine, "assess_retrieval_readiness", readiness)
    monkeypatch.setattr(engine, "call_llm", forbidden)
    monkeypatch.setattr(engine, "call_llm_structured_with_raw", forbidden)
    events = list(
        engine.respond_turn(
            AskTurn(text="What changed?", session_id=session.id),
            _pack(),
            db_path=db_path,
            repo_root=tmp_path,
            retrieval_mode="sealed",
        )
    )
    assert verified == [component]
    assert calls == []
    assert [item["type"] for item in events] == ["error"]
    assert "runtime artifact no longer matches local bytes" in capsys.readouterr().err
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM ask_turns WHERE role='assistant'").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM ask_answer_audit_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
