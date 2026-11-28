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
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import ask.claims as claims_module
import ask.engine as engine
from ask.claims import STRICT_NO_ANSWER, Claim, required_claim_spans
from ask.context import ContextPack
from ask.engine import AskRetrievalMode, AskTurn
from ask.grounding import EvidenceItem
from ask.grounding_trace import GroundingOutcome, GroundingTrace

QUESTION = "How did revenue grow?"
ANSWER = "Revenue grew 12 percent on subscription demand [1]."
TWO_CLAUSE_ANSWER = (
    "Revenue grew 12 percent on subscription demand [1]. "
    "Gross margin narrowed on freight costs [2]."
)
_FIRST_CLAUSE = "Revenue grew 12 percent on subscription demand [1]."
_ANSWER_CHUNKS = ("Revenue grew 12 percent ", "on subscription demand ", "[1].")

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
