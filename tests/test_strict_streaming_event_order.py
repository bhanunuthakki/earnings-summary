"""VAL-SEARCH-009: strict-mode streaming assessment on deterministic event-order fixtures.

Both strict Ask answer paths (strict-grounded and sealed) hold the finished
answer behind an exact-span claim-audit gate: no answer delta may reach the
consumer before the audit has accepted the answer, and an unsupported claim
still fails closed with zero delivered answer text. These tests record ONE
totally-ordered observable trace per turn — upstream transport deltas, the
claim audit's start/accept/reject, audit-package persistence, and every
consumer event as it is pulled from the generator — then assert the ordering
and the fail-closed outcomes on deterministic synthetic fixtures.

This is the M4 streaming ASSESSMENT, not a behavior change. The exact-span
claim-audit gates (the strict branch of ``ask.claims.build_citations_payload``
and ``ask.engine._validate_claim_audit_output``) run REAL on the fixtures;
nothing about delivery, buffering, or the gate is modified. The recorded
assessment concludes that earlier token streaming cannot coexist with the
retained exact-span gate (the gate is defined over the COMPLETE answer text,
so a streamed-prefix audit can never satisfy full-answer span coverage), and
recommends keeping buffered-until-audit-accepted delivery.

Zero real Claude/Ask turns: every LLM boundary is a stubbed transport. No
database outside pytest tmp, no SEC or source request, no network, no bench
or portfolio data. No timing is measured anywhere and nothing here is a
latency claim — the fixtures are event-order traces, not performance
evidence, and no streaming latency claim is made from them.

Setting ``SEARCH_VALIDATION_EVIDENCE_DIR`` records the trace artifacts under
that directory; ordinary runs write nothing outside pytest tmp.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import ask.claims as claims_module
import ask.engine as engine
from ask.audit_store import AnswerAuditPackage, AnswerContextTurn, digest_text
from ask.claims import STRICT_NO_ANSWER, Claim, required_claim_spans
from ask.context import ContextPack
from ask.engine import CLAIM_AUDIT_ADAPTER, AskRetrievalMode, AskTurn
from ask.grounding import EvidenceItem
from ask.grounding_trace import GroundingOutcome, GroundingTrace
from ask.sealed_retrieval import SealedEvidenceItem

QUESTION = "How did revenue grow?"
ANSWER = "Revenue grew 12 percent on subscription demand [1]."
TWO_CLAUSE_ANSWER = (
    "Revenue grew 12 percent on subscription demand [1]. "
    "Gross margin narrowed on freight costs [2]."
)
_FIRST_CLAUSE = "Revenue grew 12 percent on subscription demand [1]."
_ANSWER_CHUNKS = ("Revenue grew 12 percent ", "on subscription demand ", "[1].")
SEALED_NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
REQUEST_UUID = UUID(int=0x5EA1ED)
SESSION_ID = "session-1"

_EVIDENCE_DIR_ENV = "SEARCH_VALIDATION_EVIDENCE_DIR"
_SCOPE_NOTE = (
    "Deterministic synthetic event-order fixtures only: stubbed LLM transports, "
    "pytest-local artifacts, real claim-audit gate code. No live Ask or Claude turn, "
    "no SEC or source request, no portfolio or bench database, no network. No timing "
    "was measured; nothing here is a latency, retrieval-quality, or streaming-"
    "performance claim."
)


def _pack() -> ContextPack:
    return ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="context")


def _trace(outcome: GroundingOutcome, item_count: int) -> GroundingTrace:
    return GroundingTrace(
        trace_id=f"ask-grounding:{'a' * 64}",
        outcome=outcome,
        item_count=item_count,
    )


def _grounded_evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            n=1,
            kind="filing",
            label="ACME 10-K",
            text="Revenue grew 12 percent on subscription demand.",
            doc_id=3,
            href="/source/3",
        )
    ]


def _record_artifact(name: str, payload: dict[str, object]) -> None:
    root = os.environ.get(_EVIDENCE_DIR_ENV, "").strip()
    if not root:
        return
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _trace_payload(
    case: str,
    verdict: str,
    log: list[tuple[str, object]],
    event_types: list[str],
) -> dict[str, object]:
    return {
        "assertion": "VAL-SEARCH-009",
        "case": case,
        "scope_note": _SCOPE_NOTE,
        "ordering_verdict": verdict,
        "trace": [
            {"step": index, "observable": name, "value": value}
            for index, (name, value) in enumerate(log)
        ],
        "consumer_event_types": event_types,
    }


def _span_offset(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise AssertionError("claim span offsets must be integers")


def _first_consumer_index(log: list[tuple[str, object]], event_type: str) -> int:
    return next(
        index
        for index, (name, value) in enumerate(log)
        if name == "consumer_event" and value == event_type
    )


def _pull_events(
    turn: AskTurn,
    pack: ContextPack,
    *,
    db_path: Path,
    repo_root: Path,
    log: list[tuple[str, object]],
    retrieval_mode: AskRetrievalMode,
) -> list[dict[str, object]]:
    """Consume the turn one event at a time, recording each delivered frame.

    ``respond_turn`` is a generator, so engine work only advances as the
    consumer pulls; the interleaved log is therefore a faithful record of
    what the client could observe and when.
    """
    events: list[dict[str, object]] = []
    for event in engine.respond_turn(
        turn,
        pack,
        db_path=db_path,
        repo_root=repo_root,
        retrieval_mode=retrieval_mode,
    ):
        log.append(("consumer_event", event["type"]))
        events.append(event)
    return events


def _install_grounded_seams(
    monkeypatch: pytest.MonkeyPatch,
    log: list[tuple[str, object]],
    *,
    evidence: list[EvidenceItem],
    claim_supported: bool,
) -> None:
    def empty_catalog(*_args: object, **_kwargs: object) -> dict[str, list[dict[str, object]]]:
        return {}

    def known_tickers(*_args: object, **_kwargs: object) -> list[str]:
        return ["ACME"]

    def gathered(*_args: object, **_kwargs: object) -> list[EvidenceItem]:
        log.append(("evidence_gathered", len(evidence)))
        return evidence

    def recorded(*_args: object, **_kwargs: object) -> GroundingTrace:
        return _trace("ready" if evidence else "no_evidence", len(evidence))

    def stream(
        prompt: str, *, purpose: str, allow_read: bool = True
    ) -> Iterator[dict[str, object]]:
        assert purpose == "ask_answer"
        assert allow_read is False  # strict grounded runs tool-free
        for index, chunk in enumerate(_ANSWER_CHUNKS):
            log.append(("transport_delta_produced", index))
            yield {"type": "delta", "text": chunk}
        log.append(("transport_final", None))
        yield {"type": "final", "text": ANSWER}

    def claim_map(
        final_text: str,
        items: list[EvidenceItem],
        *,
        db_path: Path,
        strict: bool = False,
    ) -> list[Claim]:
        log.append(("claim_map_built", len(required_claim_spans(final_text))))
        return [
            Claim(
                text=span,
                cites=(1,) if claim_supported else (),
                supported=claim_supported,
            )
            for _start, _end, span in required_claim_spans(final_text)
        ]

    real_payload = claims_module.build_citations_payload

    def audit_spy(
        final_text: str,
        items: list[EvidenceItem],
        *,
        db_path: Path,
        strict: bool = False,
    ) -> dict[str, object] | None:
        log.append(("claim_audit_started", len(final_text)))
        try:
            payload = real_payload(final_text, items, db_path=db_path, strict=strict)
        except Exception as exc:
            log.append(("claim_audit_rejected", type(exc).__name__))
            raise
        log.append(("claim_audit_accepted", None))
        return payload

    monkeypatch.setattr(engine, "metric_catalog", empty_catalog)
    monkeypatch.setattr(engine, "tracked_tickers", known_tickers)
    monkeypatch.setattr(engine, "gather_evidence", gathered)
    monkeypatch.setattr(engine, "persist_grounding_trace", recorded)
    monkeypatch.setattr(engine.narrative_transport, "stream_llm_text", stream)
    monkeypatch.setattr(claims_module, "extract_claim_map", claim_map)
    monkeypatch.setattr(engine, "build_citations_payload", audit_spy)


def test_strict_grounded_delivers_answer_deltas_only_after_the_claim_audit_accepts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ASK_RETRIEVAL_MODE", raising=False)
    log: list[tuple[str, object]] = []
    _install_grounded_seams(monkeypatch, log, evidence=_grounded_evidence(), claim_supported=True)

    events = _pull_events(
        AskTurn(text=QUESTION),
        _pack(),
        db_path=tmp_path / "ask.db",
        repo_root=tmp_path,
        log=log,
        retrieval_mode="grounded",
    )

    names = [name for name, _value in log]
    audit_accepted_at = names.index("claim_audit_accepted")
    audit_started_at = names.index("claim_audit_started")
    transport_final_at = names.index("transport_final")
    first_delta_at = _first_consumer_index(log, "delta")

    # The audit must accept BEFORE the first answer delta reaches the consumer.
    assert first_delta_at > audit_accepted_at
    # The audit ran over the COMPLETE answer, never a streamed prefix.
    assert transport_final_at < audit_started_at
    assert cast("int", log[audit_started_at][1]) == len(ANSWER)
    # Tokens were already streamable upstream well before the audit: the
    # answer text existed, the gate is what withheld it.
    assert names.index("transport_delta_produced") < audit_started_at
    assert names.count("transport_delta_produced") == len(_ANSWER_CHUNKS)

    event_types = [str(event["type"]) for event in events]
    assert event_types == [
        "retrieval",
        "stage",
        "delta",
        "delta",
        "delta",
        "final",
        "citations",
    ]
    # The pre-answer frame carries progress text only — never answer text.
    assert "grounded on 1 source" in str(events[1].get("note"))
    deltas = [event for event in events if event["type"] == "delta"]
    assert "".join(str(event["text"]) for event in deltas) == ANSWER
    citations = events[-1]
    assert citations["grounding"] == "per_claim"
    claims = cast("list[dict[str, object]]", citations["claims"])
    assert [str(claim["text"]) for claim in claims] == [
        span for _start, _end, span in required_claim_spans(ANSWER)
    ]

    _record_artifact(
        "streaming/val-search-009-grounded-accepted-trace.json",
        _trace_payload(
            "strict-grounded turn, audit accepts",
            (
                "claim-audit acceptance precedes the first delivered answer delta; "
                "the buffered transport deltas flush only after the audit accepts"
            ),
            log,
            event_types,
        ),
    )


def test_strict_grounded_audit_rejection_fails_closed_with_zero_delivered_answer_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ASK_RETRIEVAL_MODE", raising=False)
    log: list[tuple[str, object]] = []
    _install_grounded_seams(monkeypatch, log, evidence=_grounded_evidence(), claim_supported=False)

    events = _pull_events(
        AskTurn(text=QUESTION),
        _pack(),
        db_path=tmp_path / "ask.db",
        repo_root=tmp_path,
        log=log,
        retrieval_mode="grounded",
    )

    names = [name for name, _value in log]
    # The complete answer existed upstream; the audit rejected it.
    assert names.index("transport_final") < names.index("claim_audit_started")
    rejected = [value for name, value in log if name == "claim_audit_rejected"]
    assert rejected == ["GroundedCitationError"]
    assert "claim_audit_accepted" not in names
    # Fail closed: not one answer delta is delivered, and there is no final.
    assert not any(name == "consumer_event" and value == "delta" for name, value in log)
    event_types = [str(event["type"]) for event in events]
    assert event_types == ["retrieval", "stage", "error"]
    assert events[-1] == {
        "type": "error",
        "error": "grounded answer failed citation verification",
        "code": "grounding_citation_failed",
    }
    assert not any(event["type"] == "final" for event in events)

    _record_artifact(
        "streaming/val-search-009-grounded-rejected-trace.json",
        _trace_payload(
            "strict-grounded turn, audit rejects an unsupported claim",
            (
                "unsupported claim: zero answer deltas delivered and the turn ends "
                "in the fail-closed error frame (nothing is streamed then retracted)"
            ),
            log,
            event_types,
        ),
    )


def test_strict_grounded_no_evidence_emits_only_the_deterministic_fail_closed_frame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ASK_RETRIEVAL_MODE", raising=False)
    log: list[tuple[str, object]] = []
    _install_grounded_seams(monkeypatch, log, evidence=[], claim_supported=True)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the no-evidence path must not touch an LLM boundary")

    monkeypatch.setattr(engine.narrative_transport, "stream_llm_text", forbidden)
    monkeypatch.setattr(engine, "build_citations_payload", forbidden)

    events = _pull_events(
        AskTurn(text=QUESTION),
        _pack(),
        db_path=tmp_path / "ask.db",
        repo_root=tmp_path,
        log=log,
        retrieval_mode="grounded",
    )

    event_types = [str(event["type"]) for event in events]
    assert event_types == ["retrieval", "stage", "delta", "final"]
    delta_text = "".join(str(event["text"]) for event in events if event["type"] == "delta")
    # The only pre-audit delta path is the exact deterministic no-answer
    # sentence — a fail-closed constant, never unaudited model output.
    assert delta_text == STRICT_NO_ANSWER
    assert cast("str", events[0]["outcome"]) == "no_evidence"

    _record_artifact(
        "streaming/val-search-009-grounded-no-evidence-trace.json",
        _trace_payload(
            "strict-grounded turn, zero evidence",
            (
                "the deterministic no-evidence frame is the only text emitted "
                "without an audit: it is the fixed fail-closed sentence, not "
                "unaudited answer prose, and no LLM boundary is touched"
            ),
            log,
            event_types,
        ),
    )


@dataclass(frozen=True)
class _FixturePromotion:
    promotion_id: str = "ask-promotion-1"


@dataclass(frozen=True)
class _FixtureReadiness:
    outcome: str = "ready"
    promotion: _FixturePromotion = field(default_factory=_FixturePromotion)


@dataclass(frozen=True)
class _FixturePlan:
    scopes: tuple[_FixtureReadiness, ...]


@dataclass(frozen=True)
class _FixtureReceipt:
    trace_id: str = "trace-1"
    trace_sha256: str = "b" * 64
    research_snapshot_sha256: str = "c" * 64


@dataclass(frozen=True)
class _FixtureExecution:
    plan: _FixturePlan
    receipts: tuple[_FixtureReceipt, ...]


@dataclass(frozen=True)
class _StructuredResult:
    value: object
    raw_response: str
    prompt: str


@dataclass(frozen=True)
class _CallIdentity:
    call_id: int
    run_id: str
    model: str
    provider: str
    transport: str
    prompt_sha256: str
    response_sha256: str
    template_id: str
    template_version: str
    template_vars_sha256: str


def _sealed_evidence() -> tuple[SealedEvidenceItem, ...]:
    return (
        SealedEvidenceItem(
            n=1,
            kind="narrative",
            label="ACME 10-K · MD&A",
            text="Revenue grew 12 percent on subscription demand.",
            href="/source/version/document-1?node=node-1",
            source_url="https://example.test/acme-10k",
            ticker="ACME",
            doc_type="annual_report",
            period="FY2025",
            value=None,
            trace_id="trace-1",
            result_ordinal=0,
            candidate_id="chunk-1",
            source_commitment_sha256="a" * 64,
            research_snapshot_id="research-1",
            as_of_at=SEALED_NOW,
            document_version_id="document-1",
            evidence_node_id="node-1",
            node_kind="paragraph",
            locator={"section": "MD&A"},
            score="0.500000",
        ),
    )


def _install_sealed_seams(
    monkeypatch: pytest.MonkeyPatch,
    log: list[tuple[str, object]],
    *,
    claim_supported: bool,
) -> None:
    prompts: dict[str, str] = {}

    def connect(path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(path)

    def scopes(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        return ()

    def readiness(*_args: object, **_kwargs: object) -> _FixtureReadiness:
        return _FixtureReadiness()

    def plan(*_args: object, **_kwargs: object) -> _FixturePlan:
        return _FixturePlan(scopes=(_FixtureReadiness(),))

    def execution(*_args: object, **_kwargs: object) -> _FixtureExecution:
        return _FixtureExecution(
            plan=_FixturePlan(scopes=(_FixtureReadiness(),)),
            receipts=(_FixtureReceipt(),),
        )

    def evidence(*_args: object, **_kwargs: object) -> tuple[SealedEvidenceItem, ...]:
        log.append(("sealed_evidence_loaded", 1))
        return _sealed_evidence()

    def bind(*_args: object, **_kwargs: object) -> int:
        return 1

    def context(*_args: object, **_kwargs: object) -> tuple[tuple[AnswerContextTurn, ...], str]:
        return (
            (
                AnswerContextTurn(
                    turn_id=1,
                    session_id=SESSION_ID,
                    role="user",
                    text_sha256=digest_text(QUESTION),
                    created_at=SEALED_NOW.isoformat(),
                ),
            ),
            "",
        )

    def noop(*_args: object, **_kwargs: object) -> None:
        return None

    def answer(prompt: str, **_kwargs: object) -> str:
        prompts["ask_answer"] = prompt
        log.append(("answer_call_completed", len(ANSWER)))
        return ANSWER

    def audit(prompt: str, **kwargs: object) -> _StructuredResult:
        assert kwargs.get("purpose") == "ask_claim_audit"
        prompts["ask_claim_audit"] = prompt
        log.append(("claim_audit_started", None))
        cites: list[int] = [1] if claim_supported else []
        raw_claims = [
            {
                "char_start": start,
                "char_end": end,
                "quote": quote,
                "cites": cites,
                "supported": claim_supported,
            }
            for start, end, quote in required_claim_spans(ANSWER)
        ]
        log.append(("claim_audit_returned", len(raw_claims)))
        return _StructuredResult(
            value=CLAIM_AUDIT_ADAPTER.validate_python({"claims": raw_claims}),
            raw_response=json.dumps({"claims": raw_claims}),
            prompt=prompt,
        )

    def identity(
        _db_path: Path,
        *,
        run_id: str,
        purpose: str,
        response_text: str,
    ) -> _CallIdentity:
        return _CallIdentity(
            call_id=1 if purpose == "ask_answer" else 2,
            run_id=run_id,
            model="fixture-model",
            provider="fixture",
            transport="fixture",
            prompt_sha256=digest_text(prompts[purpose]),
            response_sha256=digest_text(response_text),
            template_id=f"fixture.{purpose}",
            template_version="1",
            template_vars_sha256=digest_text(purpose),
        )

    def persist(_conn: sqlite3.Connection, package: AnswerAuditPackage) -> None:
        log.append(("audit_package_persisted", None))

    def persist_assistant(*_args: object, **_kwargs: object) -> None:
        log.append(("assistant_turn_persisted", None))

    monkeypatch.setattr(engine, "connect_sqlite", connect)
    monkeypatch.setattr(engine, "load_production_scopes", scopes)
    monkeypatch.setattr(engine, "assess_retrieval_readiness", readiness)
    monkeypatch.setattr(engine, "build_sealed_retrieval_plan", plan)
    monkeypatch.setattr(engine, "execute_sealed_retrieval_plan", execution)
    monkeypatch.setattr(engine, "load_verified_trace_evidence", evidence)
    monkeypatch.setattr(engine, "_bind_sealed_user_turn", bind)
    monkeypatch.setattr(engine, "_authoritative_context", context)
    monkeypatch.setattr(engine, "_store_assert_user_tail", noop)
    monkeypatch.setattr(engine, "call_llm", answer)
    monkeypatch.setattr(engine, "call_llm_structured_with_raw", audit)
    monkeypatch.setattr(engine, "_governed_call_identity", identity)
    monkeypatch.setattr(engine, "persist_answer_audit", persist)
    monkeypatch.setattr(engine, "_persist_sealed_assistant", persist_assistant)
    monkeypatch.setattr(engine, "uuid4", lambda: REQUEST_UUID)
    monkeypatch.delenv("ASK_RETRIEVAL_MODE", raising=False)


def test_sealed_turn_emits_its_single_answer_delta_only_after_the_exact_span_gate_accepts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log: list[tuple[str, object]] = []
    _install_sealed_seams(monkeypatch, log, claim_supported=True)

    events = _pull_events(
        AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
        _pack(),
        db_path=tmp_path / "sealed.sqlite",
        repo_root=tmp_path,
        log=log,
        retrieval_mode="sealed",
    )

    names = [name for name, _value in log]
    first_delta_at = _first_consumer_index(log, "delta")

    # The exact-span gate (running for real inside _claim_audit) plus the
    # durable audit package and the assistant persistence all precede the
    # first delivered answer delta.
    assert names.index("claim_audit_returned") < names.index("audit_package_persisted")
    assert names.index("audit_package_persisted") < first_delta_at
    assert names.index("assistant_turn_persisted") < first_delta_at
    assert names.index("answer_call_completed") < names.index("claim_audit_started")
    assert cast("int", log[names.index("answer_call_completed")][1]) == len(ANSWER)

    event_types = [str(event["type"]) for event in events]
    assert event_types == ["stage", "delta", "citations", "final"]
    deltas = [event for event in events if event["type"] == "delta"]
    assert len(deltas) == 1
    assert deltas[0]["text"] == ANSWER
    # The stage frame itself states the audit completed, before any text.
    assert "sealed and audited on 1 exact source result(s)" in str(events[0].get("note"))
    citations = events[2]
    assert citations["grounding"] == "sealed"
    claims = cast("list[dict[str, object]]", citations["claims"])
    assert [
        (_span_offset(claim["char_start"]), _span_offset(claim["char_end"]), str(claim["quote"]))
        for claim in claims
    ] == [(start, end, quote) for start, end, quote in required_claim_spans(ANSWER)]
    assert all(claim["supported"] is True for claim in claims)

    _record_artifact(
        "streaming/val-search-009-sealed-accepted-trace.json",
        _trace_payload(
            "sealed turn, exact-span gate accepts",
            (
                "the exact-span claim audit, durable audit package, and assistant "
                "persistence all complete before the single answer delta is emitted"
            ),
            log,
            event_types,
        ),
    )


def test_sealed_turn_fails_closed_when_the_exact_span_gate_rejects_an_unsupported_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log: list[tuple[str, object]] = []
    _install_sealed_seams(monkeypatch, log, claim_supported=False)

    events = _pull_events(
        AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
        _pack(),
        db_path=tmp_path / "sealed.sqlite",
        repo_root=tmp_path,
        log=log,
        retrieval_mode="sealed",
    )

    names = [name for name, _value in log]
    # The answer existed and the audit ran; the gate stopped the turn before
    # anything was persisted or delivered.
    assert "answer_call_completed" in names
    assert "claim_audit_returned" in names
    assert "audit_package_persisted" not in names
    assert "assistant_turn_persisted" not in names
    assert not any(name == "consumer_event" and value == "delta" for name, value in log)
    assert events == [
        {
            "type": "error",
            "error": "sealed Ask could not produce a fully audited answer",
        }
    ]

    _record_artifact(
        "streaming/val-search-009-sealed-rejected-trace.json",
        _trace_payload(
            "sealed turn, exact-span gate rejects an unsupported claim",
            (
                "unsupported claim: the single fail-closed error frame is the only "
                "event; zero answer deltas, no audit package, no assistant turn"
            ),
            log,
            [str(event["type"]) for event in events],
        ),
    )


def _gate() -> Callable[[str, set[int], object], None]:
    """The sealed deterministic delivery gate, resolved without private access."""
    return cast(
        "Callable[[str, set[int], object], None]",
        vars(engine)["_validate_claim_audit_output"],
    )


def _audited(claims: list[dict[str, object]]) -> object:
    """Decode a fixture auditor verdict through the governed schema adapter."""
    return CLAIM_AUDIT_ADAPTER.validate_python({"claims": claims})


def test_exact_span_gate_cannot_accept_an_audit_that_only_covered_a_streamed_prefix() -> None:
    """The structural reason earlier streaming cannot coexist with the gate.

    ``required_claim_spans`` partitions the COMPLETE delivered answer, and the
    gate demands exact-span coverage of that partition. A prefix that was
    streamed early has a different partition, so an audit performed over the
    prefix can never satisfy full-answer coverage equality — the gate is only
    decidable once the whole answer exists.
    """
    full = TWO_CLAUSE_ANSWER
    prefix = full[: len(_FIRST_CLAUSE)]
    prefix_spans = required_claim_spans(prefix)
    full_spans = required_claim_spans(full)
    assert len(prefix_spans) == 1
    assert len(full_spans) == 2
    assert prefix_spans != full_spans

    prefix_only = _audited(
        [
            {
                "char_start": prefix_spans[0][0],
                "char_end": prefix_spans[0][1],
                "quote": prefix_spans[0][2],
                "cites": [1],
                "supported": True,
            }
        ]
    )
    with pytest.raises(ValueError, match="every substantive clause"):
        _gate()(full, {1, 2}, prefix_only)

    complete = _audited(
        [
            {
                "char_start": start,
                "char_end": end,
                "quote": quote,
                "cites": [1] if "[1]" in quote else [2],
                "supported": True,
            }
            for start, end, quote in full_spans
        ]
    )
    _gate()(full, {1, 2}, complete)


def test_exact_span_gate_retention_quote_mismatch_and_unsupported_claims_fail_closed() -> None:
    """The retained gate still rejects quote tampering and unsupported claims."""
    start, end, quote = required_claim_spans(ANSWER)[0]

    with pytest.raises(ValueError, match="quote does not equal its exact answer span"):
        _gate()(
            ANSWER,
            {1},
            _audited(
                [
                    {
                        "char_start": start,
                        "char_end": end,
                        "quote": quote[:-1],
                        "cites": [1],
                        "supported": True,
                    }
                ]
            ),
        )

    with pytest.raises(ValueError, match="unsupported substantive claim"):
        _gate()(
            ANSWER,
            {1},
            _audited(
                [
                    {
                        "char_start": start,
                        "char_end": end,
                        "quote": quote,
                        "cites": [],
                        "supported": False,
                    }
                ]
            ),
        )

    _record_artifact(
        "streaming/val-search-009-gate-retention.json",
        {
            "assertion": "VAL-SEARCH-009",
            "scope_note": _SCOPE_NOTE,
            "gate": "the sealed deterministic delivery gate (claim-span-audit.v1)",
            "retained_checks": [
                "claim audit quote does not equal its exact answer span",
                "claim audit must cover every substantive clause exactly once without gaps",
                "sealed answer contains an unsupported substantive claim",
            ],
            "weakened": False,
        },
    )


def test_ordering_assessment_and_recommendation_are_recorded() -> None:
    """The recorded assessment: earlier streaming cannot coexist with the gate.

    Deterministic fixture facts established by the tests above (see the
    recorded traces): audit acceptance strictly precedes the first delivered
    answer delta in both strict modes, audit rejection delivers zero deltas
    and a single fail-closed error frame, and the exact-span gate is only
    decidable over the complete answer text.
    """
    full_spans = required_claim_spans(TWO_CLAUSE_ANSWER)
    prefix_spans = required_claim_spans(TWO_CLAUSE_ANSWER[: len(_FIRST_CLAUSE)])
    assert prefix_spans != full_spans

    _record_artifact(
        "streaming/val-search-009-ordering-assessment.json",
        {
            "assertion": "VAL-SEARCH-009",
            "scope_note": _SCOPE_NOTE,
            "question": "Can earlier streaming coexist with the exact-span claim-audit gate?",
            "verdict": (
                "No. Under the retained fail-closed contract, earlier token "
                "streaming cannot coexist with the exact-span claim-audit gate."
            ),
            "machine_checked_facts": {
                "audit_acceptance_precedes_first_delivered_answer_delta": True,
                "audit_rejection_delivers_zero_deltas_and_one_error_frame": True,
                "gate_defined_over_the_complete_answer_text": True,
                "streamed_prefix_partition_differs_from_full_answer_partition": True,
                "exact_no_answer_sentence_is_the_only_unaudited_delta_and_is_deterministic": True,
            },
            "traces": [
                "streaming/val-search-009-grounded-accepted-trace.json",
                "streaming/val-search-009-grounded-rejected-trace.json",
                "streaming/val-search-009-grounded-no-evidence-trace.json",
                "streaming/val-search-009-sealed-accepted-trace.json",
                "streaming/val-search-009-sealed-rejected-trace.json",
                "streaming/val-search-009-gate-retention.json",
            ],
            "rationale": (
                "The gate partitions the complete delivered answer into exact "
                "character spans and demands one audited, supported, correctly "
                "cited claim per span. It is therefore only decidable after the "
                "answer is complete: a streamed prefix has a different span "
                "partition (pinned above), so streaming tokens before acceptance "
                "would require either weakening the gate or delivering text that "
                "the audit may later reject — the deliver-then-retract design "
                "M4 explicitly declines. Both strict paths already hold the "
                "finished text (strict-grounded buffers all transport deltas via "
                "buffer_all; sealed is single-shot) and flush only on acceptance."
            ),
            "gate_retained": (
                "The exact-span claim-audit gate is preserved unchanged in both "
                "strict modes; no behavior change was implemented and none is "
                "required by the approved fixture contract."
            ),
            "recommendation": (
                "Retain buffered-until-audit-accepted delivery in both strict "
                "modes. Any future streaming design needs its own owner-approved "
                "feature and must preserve the invariant that no answer text is "
                "delivered before the claim audit accepts it. Progress frames "
                "that carry no answer text (stage events) already exist and "
                "remain the only latency-safe lever short of changing that owner "
                "decision. No latency claim is made from these deterministic "
                "traces; production streaming latency is unmeasured."
            ),
        },
    )
