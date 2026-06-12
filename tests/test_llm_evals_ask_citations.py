"""Citation-accuracy eval grader (src/evals/ask_citations.py, S8 PR2):
golden-file validation (including the checked-in set, whose map quotes must
anchor under PRODUCTION semantics), precision/recall map grading, the judge-
graded answer mode, and the abort semantics. Extract/generate/judge are
injected fakes everywhere — no LLM, no prod DB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ask.claims import Claim, normalize_text, split_sentences
from evals.ask_citations import (
    DEFAULT_GOLDEN_RELPATH,
    CitationCase,
    ExpectedClaim,
    compose_answer_prompt,
    grade_answer_case,
    grade_map_case,
    load_ask_citations_golden,
    parse_answer_verdict,
    run_ask_citations_eval,
)
from evals.harness import EvalAbortError
from llm.cli import LLMBudgetExceeded

REPO_ROOT = Path(__file__).resolve().parents[1]

_EVIDENCE = [
    {"n": 1, "kind": "fact", "label": "TST · Revenue", "text": "TST Revenue: Q1'26 3.2B"},
    {"n": 2, "kind": "fact", "label": "TST · Margin", "text": "TST Gross Margin: Q1'26 46.1%"},
]


def _golden(tmp_path: Path, cases: list[dict[str, object]]) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(
        json.dumps({"purpose": "ask_claim_grounding", "version": 1, "cases": cases}),
        encoding="utf-8",
    )
    return path


def _map_case(
    answer: str,
    expected: list[ExpectedClaim],
    evidence_ns: tuple[int, ...] = (1, 2),
) -> CitationCase:
    from ask.grounding import EvidenceItem

    return CitationCase(
        case_id="map-x",
        mode="map",
        evidence=tuple(
            EvidenceItem(
                n=n, kind="fact", label=f"E{n}", text=f"evidence {n}", doc_id=None, href=None
            )
            for n in evidence_ns
        ),
        answer=answer,
        expected_claims=tuple(expected),
    )


def _extract_returning(claims: list[Claim] | None):
    def _fn(answer: str, items: object) -> list[Claim] | None:
        return claims

    return _fn


# ---------------------------------------------------------------------------
# golden loading — including authoring guards on the checked-in set
# ---------------------------------------------------------------------------


def test_checked_in_golden_set_loads_and_is_well_formed() -> None:
    cases = load_ask_citations_golden(REPO_ROOT / DEFAULT_GOLDEN_RELPATH)
    map_cases = [c for c in cases if c.mode == "map"]
    answer_cases = [c for c in cases if c.mode == "answer"]
    assert len(map_cases) >= 10
    assert len(answer_cases) >= 4
    for c in cases:
        valid = {item.n for item in c.evidence}
        assert valid, c.case_id
        for e in c.expected_claims:
            assert e.cites <= valid, c.case_id
        assert c.must_cite <= valid, c.case_id
        assert c.forbid_cites <= valid, c.case_id


def test_checked_in_map_quotes_anchor_under_production_semantics() -> None:
    """Authoring guard: every expected quote must anchor to a sentence of its
    case's answer exactly the way ask.claims anchors — a quote that can't
    anchor would silently make the case unwinnable."""
    cases = load_ask_citations_golden(REPO_ROOT / DEFAULT_GOLDEN_RELPATH)
    for c in cases:
        if c.mode != "map":
            continue
        sentences = [normalize_text(s) for s in split_sentences(c.answer)]
        for e in c.expected_claims:
            q = normalize_text(e.quote)
            assert any(q in s or (len(s) >= 12 and s in q) for s in sentences), (
                f"{c.case_id}: quote does not anchor: {e.quote!r}"
            )


def test_checked_in_set_has_adversarial_cases() -> None:
    """The directive's core requirement: tempting-but-unsupported claims must
    be present in BOTH modes (map traps pin supported=false; answer traps cap
    the unsupported rate at 0 with partial/poisoned evidence)."""
    cases = load_ask_citations_golden(REPO_ROOT / DEFAULT_GOLDEN_RELPATH)
    map_traps = [
        c for c in cases if c.mode == "map" and any(not e.supported for e in c.expected_claims)
    ]
    assert len(map_traps) >= 3
    assert any(c.mode == "answer" and c.forbid_cites for c in cases)


def test_golden_validation_collects_all_problems(tmp_path: Path) -> None:
    path = _golden(
        tmp_path,
        [
            {"id": "a", "mode": "map", "evidence": _EVIDENCE, "answer": ""},
            {"id": "a", "mode": "nope", "evidence": [], "answer": "x"},
            {
                "id": "b",
                "mode": "map",
                "evidence": _EVIDENCE,
                "answer": "Revenue grew a lot this quarter.",
                "expected_claims": [
                    {"quote": "Revenue grew a lot this quarter", "cites": [9], "supported": True},
                    {"quote": "short", "cites": [], "supported": False},
                ],
            },
            {
                "id": "c",
                "mode": "answer",
                "evidence": _EVIDENCE,
                "question": "q?",
                "max_unsupported_rate": 2.0,
                "must_cite": [1],
                "forbid_cites": [1],
            },
        ],
    )
    with pytest.raises(ValueError) as exc:
        load_ask_citations_golden(path)
    message = str(exc.value)
    assert "duplicate id" in message
    assert "mode must be" in message
    assert "non-empty `evidence`" in message
    assert "non-empty `answer`" in message
    assert "not among the case's evidence numbers" in message
    assert "supported=true requires cites" in message or "quote too short" in message
    assert "outside [0, 1]" in message
    assert "overlap" in message


# ---------------------------------------------------------------------------
# map grading
# ---------------------------------------------------------------------------

_TWO_CLAIM_ANSWER = "Revenue grew 24% this quarter [1]. Margins expanded to 46.1%."
_TWO_CLAIM_EXPECTED = [
    ExpectedClaim(quote="Revenue grew 24% this quarter", cites=frozenset({1}), supported=True),
    ExpectedClaim(quote="Margins expanded to 46.1%", cites=frozenset({2}), supported=True),
]


def test_map_exact_match_passes() -> None:
    case = _map_case(_TWO_CLAIM_ANSWER, _TWO_CLAIM_EXPECTED)
    claims = [
        Claim(text="Revenue grew 24% this quarter [1].", cites=(1,), supported=True),
        Claim(text="Margins expanded to 46.1%.", cites=(2,), supported=True),
    ]
    result = grade_map_case(case, extract_fn=_extract_returning(claims))
    assert result.passed
    assert result.score == 1.0


def test_map_missed_recovery_costs_recall() -> None:
    case = _map_case(_TWO_CLAIM_ANSWER, _TWO_CLAIM_EXPECTED)
    claims = [
        Claim(text="Revenue grew 24% this quarter [1].", cites=(1,), supported=True),
        Claim(text="Margins expanded to 46.1%.", cites=(), supported=False),
    ]
    result = grade_map_case(case, extract_fn=_extract_returning(claims))
    assert not result.passed
    assert result.failure_stage == "mismatch"
    # tp=1 fp=0 fn=1 → P=1, R=0.5, F1=2/3; flags 1/2 → score (2/3 + 1/2)/2
    assert result.score == pytest.approx((2 / 3 + 0.5) / 2)
    assert result.judge_rationale is not None
    assert "recall" in result.judge_rationale


def test_map_tempting_cite_costs_precision_and_flag() -> None:
    """The adversarial contract: the audit cited the tempting item for an
    unsupported claim AND endorsed it."""
    case = _map_case(
        "Deposits grew 9% sequentially in Q1'26.",
        [
            ExpectedClaim(
                quote="Deposits grew 9% sequentially in Q1'26", cites=frozenset(), supported=False
            )
        ],
        evidence_ns=(1,),
    )
    claims = [Claim(text="Deposits grew 9% sequentially in Q1'26.", cites=(1,), supported=True)]
    result = grade_map_case(case, extract_fn=_extract_returning(claims))
    assert not result.passed
    # tp=0 fp=1 fn=0 → P=0, R=1, F1=0; flags 0/1 → score 0
    assert result.score == 0.0
    assert result.judge_rationale is not None
    assert "wrong supported flags" in result.judge_rationale


def test_map_extra_zero_cite_claim_is_tolerated() -> None:
    """A conservative audit flagging a hedge sentence as an unsupported claim
    costs nothing — only unsanctioned CITES are precision failures."""
    case = _map_case(_TWO_CLAIM_ANSWER, _TWO_CLAIM_EXPECTED)
    claims = [
        Claim(text="Revenue grew 24% this quarter [1].", cites=(1,), supported=True),
        Claim(text="Margins expanded to 46.1%.", cites=(2,), supported=True),
        Claim(text="Treat the churn figure as unverified.", cites=(), supported=False),
    ]
    result = grade_map_case(case, extract_fn=_extract_returning(claims))
    assert result.passed


def test_map_no_claims_case_requires_empty_map() -> None:
    case = _map_case("What horizon do you care about most here?", [])
    assert grade_map_case(case, extract_fn=_extract_returning([])).passed
    invented = [Claim(text="What horizon do you care about most here?", cites=(1,), supported=True)]
    result = grade_map_case(case, extract_fn=_extract_returning(invented))
    assert not result.passed
    assert result.judge_rationale is not None
    assert "invented" in result.judge_rationale


def test_map_unanchored_and_call_failures_score_zero() -> None:
    case = _map_case(_TWO_CLAIM_ANSWER, _TWO_CLAIM_EXPECTED)
    unanchored = grade_map_case(case, extract_fn=_extract_returning(None))
    assert unanchored.failure_stage == "unanchored"
    assert unanchored.score == 0.0

    def _boom(answer: str, items: object) -> list[Claim] | None:
        raise RuntimeError("transport down")

    failed = grade_map_case(case, extract_fn=_boom)
    assert failed.failure_stage == "call"
    assert failed.score == 0.0


def test_map_hard_stop_aborts() -> None:
    case = _map_case(_TWO_CLAIM_ANSWER, _TWO_CLAIM_EXPECTED)

    def _cap(answer: str, items: object) -> list[Claim] | None:
        raise LLMBudgetExceeded("ask_claim_grounding over cap")

    with pytest.raises(EvalAbortError):
        grade_map_case(case, extract_fn=_cap)


# ---------------------------------------------------------------------------
# answer grading
# ---------------------------------------------------------------------------


def _answer_case(
    *,
    must_cite: frozenset[int] = frozenset(),
    forbid: frozenset[int] = frozenset(),
    max_rate: float = 0.0,
) -> CitationCase:
    from ask.grounding import EvidenceItem

    return CitationCase(
        case_id="ans-x",
        mode="answer",
        evidence=(
            EvidenceItem(
                n=1, kind="fact", label="E1", text="NU Revenue: Q1'26 3.2B", doc_id=None, href=None
            ),
            EvidenceItem(
                n=2,
                kind="fact",
                label="E2",
                text="MELI Revenue: Q1'26 6.1B",
                doc_id=None,
                href=None,
            ),
        ),
        question="How fast did NU grow?",
        max_unsupported_rate=max_rate,
        must_cite=must_cite,
        forbid_cites=forbid,
    )


def _verdict(*claims: tuple[str, list[int], bool]) -> str:
    return json.dumps(
        {
            "claims": [
                {"quote": q, "cites": c, "supported_by_cited_evidence": s} for q, c, s in claims
            ],
            "rationale": "graded",
        }
    )


def _judge_returning(raw: str):
    def _fn(prompt: str, **kwargs: object) -> str:
        return raw

    return _fn


def test_answer_clean_pass_uses_production_prompt_contract() -> None:
    case = _answer_case(must_cite=frozenset({1}), forbid=frozenset({2}))
    prompts: list[str] = []

    def _gen(prompt: str) -> str:
        prompts.append(prompt)
        return "NU revenue grew 24% year over year [1]."

    result = grade_answer_case(
        case,
        generate_fn=_gen,
        judge_caller=_judge_returning(_verdict(("NU revenue grew 24% year over year", [1], True))),
        judge_model=None,
        run_id="r",
    )
    assert result.passed
    assert result.score == 1.0
    # The generation prompt is the engine's portfolio shape: evidence block
    # with the per-claim contract + the question.
    assert "CITE-OR-SAY-UNSURE" in prompts[0]
    assert "Cite PER SENTENCE" in prompts[0]
    assert prompts[0].rstrip().endswith("How fast did NU grow?")
    assert compose_answer_prompt(case) == prompts[0]


def test_answer_unsupported_rate_fails_and_scores_fractionally() -> None:
    case = _answer_case()
    result = grade_answer_case(
        case,
        generate_fn=lambda p: "NU grew 24% [1]. Churn halved this quarter.",
        judge_caller=_judge_returning(
            _verdict(("NU grew 24%", [1], True), ("Churn halved this quarter", [], False))
        ),
        judge_model=None,
        run_id="r",
    )
    assert not result.passed
    assert result.failure_stage == "discipline"
    # rate 0.5 → components (0.5, 1.0, 1.0) → score 5/6
    assert result.score == pytest.approx(5 / 6)
    assert result.judge_rationale is not None
    assert "unsupported-claim rate" in result.judge_rationale


def test_answer_must_cite_and_forbidden_markers_are_deterministic() -> None:
    case = _answer_case(must_cite=frozenset({1}), forbid=frozenset({2}))
    result = grade_answer_case(
        case,
        generate_fn=lambda p: "NU grew fast — MELI grew 30% [2].",
        judge_caller=_judge_returning(_verdict(("MELI grew 30%", [2], True))),
        judge_model=None,
        run_id="r",
    )
    assert not result.passed
    assert result.judge_rationale is not None
    assert "missing must-cite" in result.judge_rationale
    assert "forbidden" in result.judge_rationale
    # components: rate 0 → 1.0; must_cite 0/1 → 0.0; forbidden → 0.0
    assert result.score == pytest.approx(1 / 3)


def test_answer_judge_failures_fail_closed_and_hard_stops_abort() -> None:
    case = _answer_case()
    unparseable = grade_answer_case(
        case,
        generate_fn=lambda p: "answer [1]",
        judge_caller=_judge_returning("not json at all"),
        judge_model=None,
        run_id="r",
    )
    assert unparseable.failure_stage == "judge"
    assert unparseable.score == 0.0

    def _cap(prompt: str, **kwargs: object) -> str:
        raise LLMBudgetExceeded("eval_judge over cap")

    with pytest.raises(EvalAbortError):
        grade_answer_case(
            case, generate_fn=lambda p: "answer", judge_caller=_cap, judge_model=None, run_id="r"
        )


def test_answer_generation_error_scores_at_call_stage() -> None:
    case = _answer_case()

    def _gen(prompt: str) -> str:
        raise RuntimeError("CLI not reachable")

    result = grade_answer_case(
        case, generate_fn=_gen, judge_caller=_judge_returning("{}"), judge_model=None, run_id="r"
    )
    assert result.failure_stage == "call"
    assert result.score == 0.0


def test_parse_answer_verdict_is_strict() -> None:
    assert parse_answer_verdict("nope") is None
    assert parse_answer_verdict('{"claims": "x", "rationale": "r"}') is None
    assert parse_answer_verdict('{"claims": [], "rationale": 7}') is None
    # An empty rationale is valid — the judge writes "" when every claim is
    # supported (observed on the first live run).
    assert parse_answer_verdict('{"claims": [], "rationale": ""}') == ([], "")
    ok = parse_answer_verdict(
        '```json\n{"claims": [{"quote": "q", "cites": [1], '
        '"supported_by_cited_evidence": false}], "rationale": "r"}\n```'
    )
    assert ok is not None
    judged, rationale = ok
    assert judged[0].cites == (1,)
    assert judged[0].supported is False
    assert rationale == "r"


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def _mixed_golden(tmp_path: Path) -> Path:
    return _golden(
        tmp_path,
        [
            {
                "id": "m1",
                "mode": "map",
                "evidence": _EVIDENCE,
                "answer": "Revenue grew 24% this quarter [1].",
                "expected_claims": [
                    {"quote": "Revenue grew 24% this quarter", "cites": [1], "supported": True}
                ],
            },
            {
                "id": "a1",
                "mode": "answer",
                "evidence": _EVIDENCE,
                "question": "How fast did revenue grow?",
                "must_cite": [1],
            },
        ],
    )


def test_run_summary_mixed_modes(tmp_path: Path) -> None:
    golden = _mixed_golden(tmp_path)
    claims = [Claim(text="Revenue grew 24% this quarter [1].", cites=(1,), supported=True)]
    summary = run_ask_citations_eval(
        db_path=tmp_path / "x.db",
        golden_path=golden,
        code_root=REPO_ROOT,
        extract_fn=_extract_returning(claims),
        generate_fn=lambda p: "Revenue grew 24% [1].",
        judge_caller=_judge_returning(_verdict(("Revenue grew 24%", [1], True))),
    )
    assert summary.purpose == "ask_claim_grounding"
    assert summary.n_cases == 2
    assert summary.n_pass == 2
    assert summary.avg_score == 1.0
    assert summary.judge_model  # answer cases present → judge pin recorded
    assert summary.notes == "map=1 answer=1"
    assert summary.golden_set_sha


def test_run_without_answer_cases_skips_judge_spend(tmp_path: Path) -> None:
    golden = _mixed_golden(tmp_path)
    claims = [Claim(text="Revenue grew 24% this quarter [1].", cites=(1,), supported=True)]

    def _no_gen(prompt: str) -> str:
        raise AssertionError("generation must not run with include_answer_cases=False")

    summary = run_ask_citations_eval(
        db_path=tmp_path / "x.db",
        golden_path=golden,
        code_root=REPO_ROOT,
        include_answer_cases=False,
        extract_fn=_extract_returning(claims),
        generate_fn=_no_gen,
        judge_caller=_no_gen,
    )
    assert summary.n_cases == 1
    assert summary.judge_model is None
    assert summary.notes == "map=1 answer=0"


def test_run_aborts_when_first_case_errors_twice(tmp_path: Path) -> None:
    golden = _mixed_golden(tmp_path)

    def _boom(answer: str, items: object) -> list[Claim] | None:
        raise RuntimeError("transport down")

    with pytest.raises(EvalAbortError, match="transport"):
        run_ask_citations_eval(
            db_path=tmp_path / "x.db",
            golden_path=golden,
            code_root=REPO_ROOT,
            include_answer_cases=False,
            extract_fn=_boom,
        )


def test_run_pre_gates_on_budget_for_production_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _mixed_golden(tmp_path)

    class _Check:
        cap = 5.0

    def _skip(*_a: object, **_k: object) -> object:
        return _Check()

    monkeypatch.setattr("evals.ask_citations.should_skip_for_budget", _skip)
    with pytest.raises(EvalAbortError, match="budget"):
        run_ask_citations_eval(
            db_path=tmp_path / "x.db",
            golden_path=golden,
            code_root=REPO_ROOT,
            include_answer_cases=False,
        )
