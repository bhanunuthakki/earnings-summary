# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _pack() -> ContextPack:
    return ContextPack(
        scope="portfolio",
        default_tickers=["ACME"],
        system_context="context",
    )


def test_shadow_preserves_the_exact_legacy_event_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[object] = []
    expected: list[dict[str, object]] = [
        {"type": "delta", "text": "legacy"},
        {"type": "final", "text": "legacy", "route": "narrative"},
    ]

    def fake_shadow(
        text: str,
        turn: AskTurn,
        pack: ContextPack,
        *,
        db_path: Path,
    ) -> None:
        observed.extend((text, turn, pack, db_path))

    def fake_legacy(
        text: str,
        turn: AskTurn,
        pack: ContextPack,
        *,
        repo_root: Path,
        db_path: Path,
        emit_stage: bool,
    ) -> Iterator[dict[str, object]]:
        observed.extend((text, turn, pack, repo_root, db_path, emit_stage))
        yield from expected

    monkeypatch.setattr(engine, "_shadow_retrieval", fake_shadow)
    monkeypatch.setattr(engine, "_narrative_events", fake_legacy)
    turn = AskTurn(text="question")
    pack = _pack()
    actual = list(
        engine._sealed_or_shadow_narrative_events(
            "question",
            turn,
            pack,
            repo_root=tmp_path,
            db_path=tmp_path / "db.sqlite",
            mode="shadow",
        )
    )
    assert actual == expected
    assert observed[0] == "question"
    assert observed[4] == "question"


def test_sealed_mode_fails_before_any_llm_without_authoritative_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("LLM must not run before sealed readiness")

    monkeypatch.setattr(engine, "call_llm", forbidden)
    events = list(
        engine._sealed_or_shadow_narrative_events(
            "question",
            AskTurn(text="question"),
            _pack(),
            repo_root=tmp_path,
            db_path=tmp_path / "missing.sqlite",
            mode="sealed",
        )
    )
    assert events == [
        {
            "type": "error",
            "error": "sealed Ask requires an authoritative portfolio session",
        }
    ]


def test_invalid_mode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASK_RETRIEVAL_MODE", "permissive")
    with pytest.raises(ValueError, match="grounded, legacy, shadow, or sealed"):
        engine.ask_retrieval_mode()


def test_sealed_policy_gates_before_router_catalog_or_compiler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sealed policy must gate before routing or LLM work")

    monkeypatch.setattr(engine, "route_turn", forbidden)
    monkeypatch.setattr(engine, "metric_catalog", forbidden)
    monkeypatch.setattr(engine, "call_llm", forbidden)
    events = list(
        engine.respond_turn(
            AskTurn(text="/view revenue", session_id="session-1"),
            _pack(),
            db_path=tmp_path / "missing.sqlite",
            repo_root=tmp_path,
            retrieval_mode="sealed",
        )
    )
    assert events == [
        {
            "type": "error",
            "error": "sealed Ask accepts narrative questions, not commands or view directives",
        }
    ]


def test_final_assistant_append_is_exact_tail_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ask.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE ask_sessions (
          id TEXT PRIMARY KEY, updated_at TEXT NOT NULL
        );
        CREATE TABLE ask_turns (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
          citations_json TEXT, model TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO ask_sessions VALUES ('session-1','2026-01-01');
        INSERT INTO ask_turns(session_id,role,text,created_at)
          VALUES ('session-1','user','first','2026-01-01');
        INSERT INTO ask_turns(session_id,role,text,created_at)
          VALUES ('session-1','user','concurrent','2026-01-02');
        """
    )
    conn.commit()
    conn.close()

    def open_test(path: Path) -> sqlite3.Connection:
        opened = sqlite3.connect(path)
        opened.row_factory = sqlite3.Row
        return opened

    monkeypatch.setattr(store, "_open", open_test)
    with pytest.raises(ValueError, match="session advanced"):
        store.append_assistant_turn_if_user_tail(
            session_id="session-1",
            user_turn_id=1,
            user_text="first",
            text="stale answer",
            db_path=db_path,
        )
    assert [item.text for item in store.load_turns("session-1", db_path=db_path)] == [
        "first",
        "concurrent",
    ]


def test_claim_gate_rejects_omitted_clause_and_injected_citation_number() -> None:
    answer = "Revenue grew [1]. Margin fell [2]."
    first = engine._AuditClaim(
        char_start=0,
        char_end=17,
        quote="Revenue grew [1].",
        cites=(1,),
        supported=True,
    )
    with pytest.raises(ValueError, match="every substantive clause"):
        engine._validate_claim_audit_output(
            answer,
            {1, 2},
            engine._ClaimAuditOutput(claims=(first,)),
        )
    injected = engine._AuditClaim(
        char_start=0,
        char_end=len("Answer cites [999]."),
        quote="Answer cites [999].",
        cites=(999,),
        supported=True,
    )
    with pytest.raises(ValueError, match="outside the sealed prompt"):
        engine._validate_claim_audit_output(
            "Answer cites [999].",
            {1},
            engine._ClaimAuditOutput(claims=(injected,)),
        )


def test_only_exact_fail_closed_no_answer_is_claim_exempt() -> None:
    exact = "I don't have enough sealed evidence to answer that."
    engine._validate_claim_audit_output(
        exact,
        set(),
        engine._ClaimAuditOutput(claims=()),
    )
    with pytest.raises(ValueError, match="every substantive clause"):
        engine._validate_claim_audit_output(
            exact + " Probably.",
            set(),
            engine._ClaimAuditOutput(claims=()),
        )


def test_sealed_answer_prompt_matches_the_exact_span_delivery_gate() -> None:
    rendered = engine._SEALED_ANSWER_TEMPLATE.render(
        system_context="context",
        thread_text="thread",
        evidence_block="[1] evidence",
        question="question",
    )

    assert "Every non-empty clause or sentence" in rendered
    assert "directly supported" in rendered
    assert "matching visible" in rendered
    assert "Omit greetings, headings, questions, pure" in rendered
    assert "I don't have enough sealed evidence to answer that." in rendered


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
        prompt: str, *, purpose: str, run_id: str, **_kwargs: object
    ) -> StructuredCallResult[engine._ClaimAuditOutput]:
        payload = engine._ClaimAuditOutput(
            claims=tuple(
                engine._AuditClaim(
                    char_start=start,
                    char_end=end,
                    quote=quote,
                    cites=(),
                    supported=False,
                )
                for start, end, quote in required_claim_spans(answer)
            )
        )
        raw = payload.model_dump_json()
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
