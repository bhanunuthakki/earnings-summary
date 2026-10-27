from __future__ import annotations

import json
from pathlib import Path

import pytest

import ask.claims as claims
import llm.structured as structured
from ask.grounding import EvidenceItem


@pytest.fixture
def evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            n=1,
            kind="transcript",
            label="ACME · Q2 transcript",
            text="ACME reported revenue of $12 million for Q2. Management expects margins to rise.",
            doc_id=7,
            href="/source/7",
        )
    ]


def audit_record(
    span_id: str,
    quote: str,
    *,
    kind: str = "reported_fact",
    supported: bool = True,
    excerpt: str = "ACME reported revenue of $12 million for Q2.",
    context_value: str = "ACME",
    reason: str = "Exact source wording supports the line.",
) -> dict[str, object]:
    return {
        "span_id": span_id,
        "quote": quote,
        "kind": kind,
        "supported": supported,
        "context": {
            "issuer": context_value,
            "metric": "revenue" if context_value != "unknown" else "unknown",
            "period": "Q2" if context_value != "unknown" else "unknown",
            "unit": "USD millions" if context_value != "unknown" else "unknown",
            "scope": "consolidated" if context_value != "unknown" else "unknown",
            "basis": "unknown",
            "actual_vs_forecast": "actual" if context_value != "unknown" else "unknown",
            "comparison": "unknown",
            "attribution": "company reported" if context_value != "unknown" else "unknown",
        },
        "support": [{"evidence_n": 1, "excerpt": excerpt}]
        if supported and kind != "presentation"
        else [],
        "reason": reason,
    }


def presentation_record(span_id: str, quote: str) -> dict[str, object]:
    return audit_record(
        span_id,
        quote,
        kind="presentation",
        supported=True,
        context_value="unknown",
        reason="Fixed transcript formatting label; no factual claim.",
    )


def test_generation_uses_shared_structured_owner_with_repair_and_db_authority(
    monkeypatch: pytest.MonkeyPatch,
    evidence: list[EvidenceItem],
    tmp_path: Path,
) -> None:
    body = (
        "### What accelerated\n"
        "| Metric | Q2 |\n"
        "| Revenue | $12 million |\n"
        "ACME reported revenue of $12 million for Q2.\n"
        "ACME reported revenue of $12 million for Q2.\n"
        "Café has no support in this fixture.\n"
        "**Quotes worth keeping**\n"
    )
    output = {
        "spans": [
            audit_record("s2", "| Metric | Q2 |", supported=False),
            audit_record(
                "s3",
                "| Revenue | $12 million |",
                excerpt="ACME reported revenue of $12 million for Q2.",
            ),
            audit_record("s4", "ACME reported revenue of $12 million for Q2."),
            audit_record("s5", "ACME reported revenue of $12 million for Q2."),
            audit_record("s6", "Café has no support in this fixture.", supported=False),
        ]
    }
    physical_calls: list[tuple[str, Path | str | None, str]] = []

    def physical_call(prompt: str, **kwargs: object) -> str:
        raw_db_path = kwargs.get("db_path")
        if not isinstance(raw_db_path, (str, Path)):
            raise AssertionError("structured owner did not forward db_path")
        response = "{}" if not physical_calls else json.dumps(output)
        physical_calls.append((prompt, raw_db_path, response))
        return response

    def budget_gate(purpose: str, *, db_path: Path) -> None:
        assert purpose == "transcript_summary_claim_audit"
        assert db_path == tmp_path / "audit.db"
        return

    monkeypatch.setattr(claims, "call_llm_structured", structured.call_llm_structured)
    monkeypatch.setattr(structured, "call_llm", physical_call)
    monkeypatch.setattr(claims, "should_skip_for_budget", budget_gate)
    audit = claims.audit_document_narrative(
        body,
        evidence,
        db_path=tmp_path / "audit.db",
    )

    assert len(physical_calls) == 2
    assert all(db_path == tmp_path / "audit.db" for _, db_path, _ in physical_calls)
    assert "REQUIRED JSON SCHEMA" in physical_calls[0][0]
    assert "evidence_n" in physical_calls[0][0]
    assert "spans" in physical_calls[1][0]
    assert [record.span_id for record in audit.records] == [
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
    ]
    assert audit.spans[3].text == audit.spans[4].text
    assert audit.spans[3].start != audit.spans[4].start
    encoded_body = body.encode("utf-8")
    assert all(
        encoded_body[span.byte_start : span.byte_end] == span.raw_bytes for span in audit.spans
    )
    assert audit.spans[5].byte_end > audit.spans[5].end
    assert audit.spans[6].byte_start > audit.spans[6].start
    assert audit.records[0].kind == "presentation"
    assert audit.records[0].supported and not audit.records[0].support
    assert audit.records[-1].kind == "presentation"
    assert audit.qualified is False
    replay = claims.replay_document_narrative_audit(audit.raw_payload(), body, evidence)
    assert replay == audit
    saved_calls = [(prompt, response) for prompt, _, response in physical_calls]
    call_count = len(physical_calls)

    def forbidden_transport(prompt: str, **kwargs: object) -> str:
        raise AssertionError("replay must not call a model transport")

    monkeypatch.setattr(structured, "call_llm", forbidden_transport)
    replayed_calls = claims.replay_document_narrative_calls(body, evidence, saved_calls)
    assert replayed_calls == audit
    assert len(physical_calls) == call_count

    changed_prompt = [(saved_calls[0][0] + " tampered", saved_calls[0][1]), *saved_calls[1:]]
    with pytest.raises(claims.DocumentNarrativeAuditError, match="prompt differs"):
        claims.replay_document_narrative_calls(body, evidence, changed_prompt)
    changed_repair = [
        saved_calls[0],
        (saved_calls[1][0] + " tampered", saved_calls[1][1]),
    ]
    with pytest.raises(claims.DocumentNarrativeAuditError, match="repair prompt differs"):
        claims.replay_document_narrative_calls(body, evidence, changed_repair)
    with pytest.raises(claims.DocumentNarrativeAuditError, match="prompt differs"):
        claims.replay_document_narrative_calls(body + "Changed body.\n", evidence, saved_calls)
    changed_evidence = [
        EvidenceItem(
            n=evidence[0].n,
            kind=evidence[0].kind,
            label=evidence[0].label,
            text=evidence[0].text + " Changed corpus.",
            doc_id=evidence[0].doc_id,
            href=evidence[0].href,
        )
    ]
    with pytest.raises(claims.DocumentNarrativeAuditError, match="prompt differs"):
        claims.replay_document_narrative_calls(body, changed_evidence, saved_calls)
    with pytest.raises(claims.DocumentNarrativeAuditError, match="extra calls"):
        claims.replay_document_narrative_calls(body, evidence, [*saved_calls, ("extra", "{}")])
    with pytest.raises(claims.DocumentNarrativeAuditError, match="required repair"):
        claims.replay_document_narrative_calls(body, evidence, saved_calls[:1])
    invalid_repair = [saved_calls[0], (saved_calls[1][0], '{"spans":[]}')]
    with pytest.raises(
        claims.DocumentNarrativeAuditError, match="saved repair response is invalid"
    ) as error:
        claims.replay_document_narrative_calls(body, evidence, invalid_repair)
    assert "population" in str(error.value.__cause__)
    valid_initial_with_extra_repair = [
        (saved_calls[0][0], saved_calls[1][1]),
        saved_calls[1],
    ]
    with pytest.raises(claims.DocumentNarrativeAuditError, match="extra calls"):
        claims.replay_document_narrative_calls(body, evidence, valid_initial_with_extra_repair)


def test_replay_rejects_missing_duplicate_and_unexpected_occurrences(
    evidence: list[EvidenceItem],
) -> None:
    body = "Revenue rose.\nRevenue rose."
    first = audit_record("s1", "Revenue rose.")
    with pytest.raises(claims.DocumentNarrativeAuditError, match="population"):
        claims.replay_document_narrative_audit({"records": [first]}, body, evidence)
    with pytest.raises(claims.DocumentNarrativeAuditError, match="duplicate"):
        claims.replay_document_narrative_audit({"records": [first, first]}, body, evidence)
    unexpected = audit_record("s3", "Revenue rose.")
    with pytest.raises(claims.DocumentNarrativeAuditError, match="unexpected"):
        claims.replay_document_narrative_audit({"records": [first, unexpected]}, body, evidence)


def test_replay_checks_exact_quote_support_and_closed_presentation_labels(
    evidence: list[EvidenceItem],
) -> None:
    body = "ACME reported revenue of $12 million for Q2."
    mismatch = audit_record("s1", body, excerpt="Revenue fell")
    with pytest.raises(claims.DocumentNarrativeAuditError, match="substring"):
        claims.replay_document_narrative_audit({"records": [mismatch]}, body, evidence)

    unsupported = audit_record("s1", body, supported=False)
    wrong_quote = dict(unsupported)
    wrong_quote["quote"] = "ACME revenue was $12 million."
    with pytest.raises(claims.DocumentNarrativeAuditError, match="exact candidate"):
        claims.replay_document_narrative_audit({"records": [wrong_quote]}, body, evidence)

    label = presentation_record("s1", "### What accelerated")
    fact_quote = "ACME reported revenue of $12 million for Q2."
    fact = audit_record("s2", fact_quote)
    result = claims.replay_document_narrative_audit(
        {"records": [label, fact]}, f"### What accelerated\n{fact_quote}", evidence
    )
    assert result.records[0].supported
    assert result.records[0].support == ()

    added_claim = presentation_record("s1", "### What accelerated: revenue rose")
    with pytest.raises(claims.DocumentNarrativeAuditError, match="fixed transcript label"):
        claims.replay_document_narrative_audit(
            {"records": [added_claim]}, "### What accelerated: revenue rose", evidence
        )


@pytest.mark.parametrize(
    ("quote", "accepted"),
    [
        ("Inference: management expects margins to rise.", True),
        ("Analyst inference: management expects margins to rise.", True),
        ("Management did not infer any change.", False),
    ],
)
def test_inference_label_must_be_an_anchored_prefix(
    quote: str,
    accepted: bool,
    evidence: list[EvidenceItem],
) -> None:
    entry = audit_record(
        "s1",
        quote,
        kind="explicit_inference",
        excerpt="Management expects margins to rise.",
    )
    payload = {"records": [entry]}
    if accepted:
        audit = claims.replay_document_narrative_audit(payload, quote, evidence)
        assert audit.records[0].kind == "explicit_inference"
    else:
        with pytest.raises(claims.DocumentNarrativeAuditError, match="labeled"):
            claims.replay_document_narrative_audit(payload, quote, evidence)


def test_replay_rejects_empty_syntax_only_and_invalid_evidence_numbers(
    evidence: list[EvidenceItem],
) -> None:
    for body in ("", "   ", "---\n", "```\n~~~"):
        with pytest.raises(claims.DocumentNarrativeAuditError, match="candidate"):
            claims.replay_document_narrative_audit({"records": []}, body, evidence)

    for invalid_number in (0, -1, True):
        invalid = [
            EvidenceItem(
                n=invalid_number,
                kind="transcript",
                label="invalid",
                text="source",
                doc_id=1,
                href="/source/1",
            )
        ]
        with pytest.raises(claims.DocumentNarrativeAuditError, match="positive integers"):
            claims.replay_document_narrative_audit({"records": []}, "Candidate claim.", invalid)

    duplicate_numbers = [
        evidence[0],
        EvidenceItem(
            n=1,
            kind="transcript",
            label="duplicate",
            text="second source",
            doc_id=2,
            href="/source/2",
        ),
    ]
    with pytest.raises(claims.DocumentNarrativeAuditError, match="unique"):
        claims.replay_document_narrative_audit(
            {"records": []}, "Candidate claim.", duplicate_numbers
        )


def test_generation_preconditions_fail_before_budget_or_model(
    monkeypatch: pytest.MonkeyPatch,
    evidence: list[EvidenceItem],
    tmp_path: Path,
) -> None:
    budget_calls: list[str] = []
    model_calls: list[str] = []

    def budget_gate(purpose: str, *, db_path: Path) -> None:
        budget_calls.append(purpose)
        return

    def physical_call(prompt: str, **kwargs: object) -> str:
        model_calls.append(prompt)
        return "{}"

    monkeypatch.setattr(claims, "should_skip_for_budget", budget_gate)
    monkeypatch.setattr(claims, "call_llm_structured", structured.call_llm_structured)
    monkeypatch.setattr(structured, "call_llm", physical_call)
    for body, corpus in (
        ("---\n```", evidence),
        (
            "Candidate claim.",
            [
                EvidenceItem(
                    n=0,
                    kind="transcript",
                    label="invalid",
                    text="source",
                    doc_id=1,
                    href="/source/1",
                )
            ],
        ),
    ):
        with pytest.raises(claims.DocumentNarrativeAuditError):
            claims.audit_document_narrative(body, corpus, db_path=tmp_path / "audit.db")
    assert budget_calls == []
    assert model_calls == []


def test_invalid_source_excerpt_uses_one_shared_repair_and_replays_without_calls(
    evidence: list[EvidenceItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = "ACME reported revenue of $12 million for Q2."
    bad = json.dumps({"spans": [audit_record("s1", body, excerpt="ACME revenue was $12m.")]})
    good = json.dumps({"spans": [audit_record("s1", body)]})
    calls: list[tuple[str, str]] = []

    def transport(prompt: str, **_kwargs: object) -> str:
        response = bad if not calls else good
        calls.append((prompt, response))
        return response

    def no_budget(_purpose: str, *, db_path: Path) -> None:
        return None

    monkeypatch.setattr(claims, "call_llm_structured", structured.call_llm_structured)
    monkeypatch.setattr(claims, "should_skip_for_budget", no_budget)
    result = claims.audit_document_narrative(
        body, evidence, db_path=tmp_path / "audit.sqlite", transport=transport
    )
    assert len(calls) == 2
    assert "span s1, evidence 1" in calls[1][0]
    assert "Copy the excerpt exactly from that source" in calls[1][0]
    assert "ACME revenue was $12m." not in calls[1][0]
    assert claims.replay_document_narrative_calls(body, evidence, calls) == result
    assert len(calls) == 2
