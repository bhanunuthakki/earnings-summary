from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import ask.claims as claims
import llm.structured as structured
from ask.grounding import EvidenceItem
from evals.ask_citations import document_support_expectations, grade_document_narrative_case


@pytest.fixture
def context_payload() -> dict[str, object]:
    path = (
        Path(__file__).resolve().parents[1]
        / "evals/golden/transcript_summary_grounding_context.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _wire_for_prompt(
    prompt: str,
    expected: list[dict[str, object]],
    source: str,
    case_id: str,
) -> str:
    ids = re.findall(r"(?m)^(s\d+) \[\d+:\d+\]", prompt)
    spans: list[dict[str, object]] = []
    for span_id in ids:
        index = int(span_id[1:]) - 1
        item = expected[index]
        quote = cast(str, item["quote"])
        kind_value = item.get("kind", "reported_fact")
        kind = cast(str, kind_value[0] if isinstance(kind_value, list) else kind_value)
        supported = cast(bool, item["supported"])
        presentation = kind == "presentation"
        context = {
            key: "unknown"
            for key in (
                "issuer",
                "metric",
                "period",
                "unit",
                "scope",
                "basis",
                "actual_vs_forecast",
                "comparison",
                "attribution",
            )
        }
        context.update(cast(dict[str, str], item.get("context", {})))
        occurrence = sum(1 for prior in expected[:index] if prior.get("quote") == quote) + 1
        support_excerpts = cast(
            list[str],
            item.get(
                "support_excerpts",
                document_support_expectations(case_id, quote, occurrence) or [source[:80]],
            ),
        )
        spans.append(
            {
                "span_id": span_id,
                "quote": quote,
                "kind": kind,
                "supported": supported,
                "context": context,
                "support": []
                if not supported or presentation
                else [{"evidence_n": 1, "excerpt": excerpt} for excerpt in support_excerpts],
                "reason": (
                    "Fixed transcript formatting label; no factual claim."
                    if presentation
                    else "Synthetic fixture response."
                ),
            }
        )
    return json.dumps({"spans": spans})


def test_full_format_bundle_checks_second_batch_mutation_and_database_path(
    monkeypatch: pytest.MonkeyPatch,
    context_payload: dict[str, object],
    tmp_path: Path,
) -> None:
    fixtures = cast(list[dict[str, object]], context_payload["cases"])
    fixture = fixtures[0]
    expected = cast(list[dict[str, object]], fixture["lines"])
    source = cast(str, cast(list[dict[str, object]], fixture["items"])[0]["text"])
    body = "\n".join(cast(str, item["quote"]) for item in expected)
    evidence = [
        EvidenceItem(
            n=1,
            kind="transcript",
            label="SYNTHCTX1 source",
            text=source,
            doc_id=None,
            href=None,
        )
    ]
    database = tmp_path / "isolated-eval.db"
    budget_paths: list[Path] = []

    def budget_gate(purpose: str, *, db_path: Path) -> None:
        assert purpose == "transcript_summary_claim_audit"
        budget_paths.append(db_path)

    monkeypatch.setattr(claims, "should_skip_for_budget", budget_gate)
    calls: list[str] = []
    structured_calls: list[dict[str, object]] = []
    original_structured_call = cast(Callable[..., object], structured.call_llm_structured)

    def structured_call(prompt: str, **kwargs: object) -> object:
        assert kwargs.get("purpose") == "transcript_summary_claim_audit"
        assert kwargs.get("db_path") == database
        structured_calls.append(kwargs)
        return original_structured_call(prompt, **kwargs)

    monkeypatch.setattr(claims, "call_llm_structured", structured_call)

    def transport(prompt: str, **kwargs: object) -> str:
        assert kwargs.get("purpose") == "transcript_summary_claim_audit"
        assert kwargs.get("scope") == "eval:document_summary:full_format_cross_batch"
        calls.append(prompt)
        return _wire_for_prompt(prompt, expected, source, "full_format_cross_batch")

    result = grade_document_narrative_case(
        case_id="full_format_cross_batch",
        body=body,
        items=evidence,
        expected=expected,
        db_path=database,
        transport=transport,
        exchanges=[],
    )
    assert result.passed
    assert len(calls) == 2
    assert len(structured_calls) == 2
    assert budget_paths == [database]
    assert "s25" in calls[1] and "s28" in calls[1]

    mutation = cast(list[dict[str, object]], fixture["mutations"])[0]
    changed = list(expected)
    changed[cast(int, mutation["line_index"])] = {
        "quote": mutation["quote"],
        "supported": mutation["supported"],
    }
    changed_body = "\n".join(cast(str, item["quote"]) for item in changed)

    def mutation_transport(prompt: str, **kwargs: object) -> str:
        return _wire_for_prompt(prompt, changed, source, "full_format_false_after_batch20")

    mutation_result = grade_document_narrative_case(
        case_id="full_format_false_after_batch20",
        body=changed_body,
        items=evidence,
        expected=changed,
        db_path=database,
        transport=mutation_transport,
        exchanges=[],
    )
    assert mutation_result.passed
    assert cast(int, mutation["line_index"]) >= 20


def test_context_gold_has_conflict_owner_and_long_full_source_fixtures(
    context_payload: dict[str, object],
) -> None:
    fixtures = cast(list[dict[str, object]], context_payload["cases"])
    by_id = {cast(str, fixture["case_id"]): fixture for fixture in fixtures}
    assert len(fixtures) == 4
    conflict = cast(list[dict[str, object]], by_id["period_conflict_issuer_and_qualifier"]["lines"])
    assert [item["supported"] for item in conflict] == [True, False, False, False]
    owner = cast(list[dict[str, object]], by_id["owner_anchor_not_company_fact"]["lines"])
    assert [item["supported"] for item in owner] == [True, False, True, False]
    assert cast(str, owner[2]["quote"]).startswith("Our view:")
    long_case = by_id["long_full_source_and_preflight"]
    assert cast(int, long_case["padding_chars"]) == 90_000
    assert cast(int, long_case["overlimit_preflight_chars"]) > 120_000


def test_overlimit_source_is_rejected_before_budget_or_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    items = [
        EvidenceItem(
            n=1,
            kind="transcript",
            label="oversized",
            text="x" * 120_001,
            doc_id=None,
            href=None,
        )
    ]

    def forbidden_budget(purpose: str, *, db_path: Path) -> None:
        raise AssertionError("input preflight must precede the budget and provider gates")

    def forbidden_transport(prompt: str, **kwargs: object) -> str:
        calls.append(prompt)
        raise AssertionError("input preflight must precede the provider call")

    monkeypatch.setattr(claims, "should_skip_for_budget", forbidden_budget)
    with pytest.raises(claims.DocumentNarrativeAuditError, match="size limit"):
        claims.audit_document_narrative(
            "This candidate has a substantive line.",
            items,
            db_path=tmp_path / "isolated-eval.db",
            transport=forbidden_transport,
        )
    assert calls == []


def test_context_wording_is_diagnostic_and_requires_semantic_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = "SYNTH01 reported revenue."
    expected: list[dict[str, object]] = [
        {
            "quote": body,
            "supported": True,
            "kind": "reported_fact",
            "context": {"attribution": "company reported"},
            "support_excerpts": [body],
        }
    ]
    actual_expected = [{**expected[0], "context": {"attribution": "SYNTH01 reported"}}]
    wire = _wire_for_prompt("s1 [0:25]", actual_expected, body, "wording")

    def respond(_prompt: str, **_kwargs: object) -> str:
        return wire

    def no_budget(_purpose: str, *, db_path: Path) -> None:
        return None

    monkeypatch.setattr(claims, "should_skip_for_budget", no_budget)
    monkeypatch.setattr(claims, "call_llm_structured", structured.call_llm_structured)
    result = grade_document_narrative_case(
        case_id="wording",
        body=body,
        items=[
            EvidenceItem(n=1, kind="transcript", label="source", text=body, doc_id=None, href=None)
        ],
        expected=expected,
        db_path=tmp_path / "test.sqlite",
        transport=respond,
        exchanges=[],
    )
    assert result.passed
    assert result.actual_json is not None
    checks = json.loads(result.actual_json)["checks"]
    assert checks[0]["context_text_matches"] is False
    assert checks[0]["semantic_review_required"] is True


def test_live_document_eval_rejects_missing_ledger_before_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import llm.cli as cli
    from evals.ask_citations import run_document_narrative_eval

    calls: list[str] = []

    def forbidden_prepare(prompt: str, **_kwargs: object) -> object:
        calls.append(prompt)
        raise AssertionError("provider preparation must follow the ledger preflight")

    monkeypatch.setattr(cli, "prepare_llm_call", forbidden_prepare)
    root = Path(__file__).resolve().parents[1]
    missing = tmp_path / "missing-ledger.db"
    with pytest.raises(FileNotFoundError, match="database is unavailable"):
        run_document_narrative_eval(
            db_path=missing,
            golden_path=root / "evals/golden/transcript_summary_grounding.json",
            context_golden_path=root / "evals/golden/transcript_summary_grounding_context.json",
            code_root=root,
        )
    assert calls == []
    assert not missing.exists()


def test_live_document_eval_rejects_stale_ledger_before_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import llm.cli as cli
    from evals.ask_citations import run_document_narrative_eval
    from schema_compat import SchemaRevisionMismatch

    calls: list[str] = []

    def forbidden_prepare(prompt: str, **_kwargs: object) -> object:
        calls.append(prompt)
        raise AssertionError("provider preparation must follow schema preflight")

    monkeypatch.setattr(cli, "prepare_llm_call", forbidden_prepare)
    root = Path(__file__).resolve().parents[1]
    database = migrated_db(
        tmp_path / "stale-ledger.db", target="0064_retired_capture_coaching_stores"
    )
    with pytest.raises(SchemaRevisionMismatch):
        run_document_narrative_eval(
            db_path=database,
            golden_path=root / "evals/golden/transcript_summary_grounding.json",
            context_golden_path=root / "evals/golden/transcript_summary_grounding_context.json",
            code_root=root,
        )
    assert calls == []
