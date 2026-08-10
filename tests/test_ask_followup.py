"""S7 agentic evidence loop (src/ask/followup.py + engine wiring): need-request
parsing, period-aware targeted retrieval, the bounded follow-up rounds, the
engine's stream gate and fail-closed paths.

All LLM seams are monkeypatched — the suite never spends:

  ask.narrative_transport.stream_llm_text      (portfolio pass 1)
  ask.followup.call_llm                        (follow-up rounds)
  ask.followup.should_skip_for_budget          (arming control)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from ask import engine as ask_engine
from ask import followup, narrative_transport
from ask.context import ContextPack
from ask.followup import (
    MAX_ROUNDS,
    FollowupOutcome,
    followup_armed,
    need_protocol_block,
    parse_need_request,
    run_followup_rounds,
)
from ask.grounding import (
    EvidenceItem,
    EvidenceNeed,
    _parse_period,  # pyright: ignore[reportPrivateUsage]
    gather_requested_evidence,
)

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL, source_url TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual'
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
    value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL, ticker TEXT NOT NULL,
    call_date TIMESTAMP, fiscal_period_type TEXT, period_end TIMESTAMP
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL, archived_at TEXT
);
"""

_NEW_CALL_LINES = [
    "TST Q1 2026 Earnings Call",
    "CFO: Credit quality improved this quarter, with NPL formation slowing"
    " across every cohort and coverage holding stable.",
]

_OLD_CALL_LINES = [
    "TST Q1 2024 Earnings Call",
    "CFO: Asset quality deteriorated this quarter and NPL coverage was thin,"
    " so we tightened underwriting across the riskiest cohorts.",
]

_FORM_10K_2023 = {
    "symbol": "TST",
    "period": "FY",
    "year": 2023,
    "Risk Factors": [
        {
            "Logistics": [
                "Heavy logistics capex may pressure free cash flow while the"
                " fulfillment network scales."
            ]
        }
    ],
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(_DDL)
    docs = [
        (3, "transcripts/processed/TST_Q1_2026.txt"),
        (4, "transcripts/processed/TST_Q4_2025.txt"),
        (5, "transcripts/processed/TST_Q1_2024.txt"),
    ]
    for doc_id, file_path in docs:
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, file_path,"
            " sha256, fetched_at, fetch_status) VALUES (?, 'TST', 'transcript_audio',"
            " 'earnings_call_transcript', ?, 'x', '2026-01-01 00:00:00', 'ok')",
            (doc_id, file_path),
        )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES (6, 'TST', 'fmp', 'fmp_10k_json',"
        " 'data/historical/fmp/TST_form_10k_2023.json', 'y', '2026-01-01 00:00:00', 'ok')"
    )
    for doc_id, fpt, pe in [
        (3, "Q1", "2026-03-31 00:00:00"),
        (4, "Q4", "2025-12-31 00:00:00"),
        (5, "Q1", "2024-03-31 00:00:00"),
    ]:
        conn.execute(
            "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, period_end)"
            " VALUES (?, 'TST', ?, ?)",
            (doc_id, fpt, pe),
        )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type)"
        " VALUES ('TST', 'Test Co', 'portfolio')"
    )
    conn.commit()
    conn.close()

    for name, lines in [
        ("TST_Q1_2026.txt", _NEW_CALL_LINES),
        ("TST_Q4_2025.txt", _NEW_CALL_LINES),
        ("TST_Q1_2024.txt", _OLD_CALL_LINES),
    ]:
        path = tmp_path / "transcripts" / "processed" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    filing = tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2023.json"
    filing.parent.mkdir(parents=True, exist_ok=True)
    filing.write_text(json.dumps(_FORM_10K_2023), encoding="utf-8")
    return tmp_path


def _db(repo: Path) -> Path:
    return repo / "data" / "portfolio.db"


_NEED_TRANSCRIPT = json.dumps(
    {
        "need": [
            {"kind": "transcript", "ticker": "TST", "period": "Q1 2024", "query": "asset quality"}
        ]
    }
)


# ----------------------------------------------------------------------------
# parse_need_request — the schema gate


def test_parse_need_request_accepts_valid_and_fenced() -> None:
    needs = parse_need_request(_NEED_TRANSCRIPT)
    assert needs == [
        EvidenceNeed(kind="transcript", ticker="TST", period="Q1 2024", query="asset quality")
    ]
    fenced = "```json\n" + _NEED_TRANSCRIPT + "\n```"
    assert parse_need_request(fenced) == needs


def test_parse_need_request_rejects_prose_and_other_json() -> None:
    assert parse_need_request("NPLs improved this quarter [1].") is None
    assert parse_need_request('{"answer": "ok"}') is None  # JSON but no need key
    assert parse_need_request("") is None


def test_parse_need_request_fails_closed_on_unusable_entries() -> None:
    # Wrong-shape need value / unknown kinds → [] (a need that retrieves
    # nothing and forces the answer round), never a crash, never None.
    assert parse_need_request('{"need": "more evidence"}') == []
    assert parse_need_request('{"need": [{"kind": "bogus"}, "x"]}') == []


def test_parse_need_request_normalizes_and_caps_entries() -> None:
    raw = {
        "need": [
            {"kind": "Transcript", "ticker": "tst", "query": "  margins   trend  "},
            {"kind": "dcf"},
            {"kind": "fact", "ticker": "not a ticker"},
            {"kind": "journal"},  # 4th entry — beyond the per-round cap
        ]
    }
    needs = parse_need_request(json.dumps(raw))
    assert needs is not None and len(needs) == 3
    assert needs[0] == EvidenceNeed(
        kind="transcript", ticker="TST", period=None, query="margins trend"
    )
    assert needs[1].kind == "dcf"
    assert needs[2].ticker is None  # malformed ticker dropped, need kept


def test_parse_period_shapes() -> None:
    assert _parse_period("Q1 2025") == (2025, 1)
    assert _parse_period("Q1'25") == (2025, 1)
    assert _parse_period("1Q25") == (2025, 1)
    assert _parse_period("FY2024") == (2024, None)
    assert _parse_period("2023") == (2023, None)
    assert _parse_period("latest") == (None, None)
    assert _parse_period(None) == (None, None)


# ----------------------------------------------------------------------------
# followup_armed — the budget/structural gate


def _over_cap(*_a: object, **_k: object) -> object:
    """A stand-in for the failing BudgetCheck should_skip_for_budget returns."""
    return object()


def test_followup_armed_requires_db_and_budget(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert followup_armed(_db(repo)) is True  # no llm_budgets table → fail open
    assert followup_armed(repo / "nope.db") is False

    monkeypatch.setattr(followup, "should_skip_for_budget", _over_cap)
    assert followup_armed(_db(repo)) is False  # over a skip-mode cap → disarmed


# ----------------------------------------------------------------------------
# gather_requested_evidence — period-aware targeted retrieval


def test_requested_transcript_reaches_older_quarter(repo: Path) -> None:
    items = gather_requested_evidence(
        [EvidenceNeed(kind="transcript", ticker="TST", period="Q1 2024", query="asset quality")],
        question="how did management's asset quality framing change?",
        repo_root=repo,
        db_path=_db(repo),
        scope_tickers=["TST"],
        existing=[],
    )
    assert items, "expected the Q1'24 call line"
    assert items[0].label.startswith("TST Q1'24 call")
    assert "deteriorated" in items[0].text
    assert items[0].href == "/source/5#L2"


def test_requested_filing_targets_specific_year(repo: Path) -> None:
    items = gather_requested_evidence(
        [EvidenceNeed(kind="filing", ticker="TST", period="FY2023", query="logistics capex")],
        question="what did the older 10-K say?",
        repo_root=repo,
        db_path=_db(repo),
        scope_tickers=["TST"],
        existing=[],
    )
    assert items, "expected the FY2023 risk-factor section"
    assert "FY2023" in items[0].label
    assert "logistics capex" in items[0].text.lower()


def test_requested_items_continue_numbering_and_dedupe(repo: Path) -> None:
    existing = [
        EvidenceItem(
            n=1,
            kind="fact",
            label="TST · Revenue",
            text="TST Revenue ...",
            doc_id=1,
            href="/source/1",
        ),
        EvidenceItem(
            n=2,
            kind="transcript",
            label="TST Q1'24 call · L2",
            text="already shown",
            doc_id=5,
            href="/source/5#L2",
        ),
    ]
    need = EvidenceNeed(kind="transcript", ticker="TST", period="Q1 2024", query="asset quality")
    items = gather_requested_evidence(
        [need],
        question="asset quality",
        repo_root=repo,
        db_path=_db(repo),
        scope_tickers=["TST"],
        existing=existing,
    )
    # The only matching line is already presented → nothing new.
    assert items == []

    items2 = gather_requested_evidence(
        [need],
        question="asset quality",
        repo_root=repo,
        db_path=_db(repo),
        scope_tickers=["TST"],
        existing=[existing[0]],
    )
    assert items2 and items2[0].n == 2  # numbering continues after existing


def test_requested_pack_loads_with_focus(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_load_packs(
        keys: tuple[str, ...], *, db_path: Path, focus_tickers: list[str]
    ) -> list[dict[str, object]]:
        seen["keys"] = keys
        seen["focus"] = focus_tickers
        return [
            {
                "kind": "dcf",
                "label": "DCF runs · fair value",
                "text": "TST: fair $50 vs live $40",
                "doc_id": None,
                "href": "/#decisions_record",
                "source_url": None,
            }
        ]

    monkeypatch.setattr("ask.grounding.load_packs", fake_load_packs)
    items = gather_requested_evidence(
        [EvidenceNeed(kind="dcf", ticker="TST")],
        question="is it cheap?",
        repo_root=repo,
        db_path=_db(repo),
        scope_tickers=["TST"],
        existing=[],
    )
    assert seen == {"keys": ("dcf",), "focus": ["TST"]}
    assert items and items[0].kind == "dcf" and items[0].n == 1


def test_requested_evidence_never_raises(tmp_path: Path) -> None:
    assert (
        gather_requested_evidence(
            [EvidenceNeed(kind="transcript", ticker="TST")],
            question="anything",
            repo_root=tmp_path,
            db_path=tmp_path / "missing.db",
            scope_tickers=[],
            existing=[],
        )
        == []
    )


# ----------------------------------------------------------------------------
# run_followup_rounds — the bounded loop


def _drain(
    gen: Generator[dict[str, object], None, FollowupOutcome],
) -> tuple[list[dict[str, object]], FollowupOutcome]:
    events: list[dict[str, object]] = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, cast("FollowupOutcome", stop.value)


def test_followup_round_answers_and_attributes_ledger(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_call_llm(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return "Asset quality worsened in 2024, then recovered [1]."

    monkeypatch.setattr(followup, "call_llm", fake_call_llm)
    events, outcome = _drain(
        run_followup_rounds(
            question="how did asset quality framing change?",
            needs=[
                EvidenceNeed(
                    kind="transcript", ticker="TST", period="Q1 2024", query="asset quality"
                )
            ],
            items=[],
            base_context="SYSTEM CONTEXT",
            thread_text="",
            repo_root=repo,
            db_path=_db(repo),
            scope_tickers=["TST"],
            ledger_ticker="TST",
        )
    )
    assert outcome.final_text == "Asset quality worsened in 2024, then recovered [1]."
    assert outcome.rounds == 1 and outcome.error is None
    assert outcome.new_items == 1 and len(outcome.items) == 1
    stages = [(e.get("stage"), str(e.get("note") or "")) for e in events if e["type"] == "stage"]
    assert stages[0][0] == "retrieving"
    assert "fetching more evidence" in stages[0][1]
    assert stages[1][0] == "answering" and "1 new" in stages[1][1]
    # Ledger attribution: a real call_llm with purpose + run_id + bounded latency.
    call = calls[0]
    assert call["purpose"] == followup.PURPOSE
    assert call["ticker"] == "TST" and call["scope"] == "ask"
    assert call["run_id"] == outcome.run_id
    assert call["timeout_seconds"] == followup.ROUND_TIMEOUT_SECONDS
    prompt = str(call["prompt"])
    assert prompt.startswith("SYSTEM CONTEXT")
    assert "EVIDENCE FOLLOW-UP" in prompt and "[1]" in prompt
    assert "NEED MORE EVIDENCE?" in prompt  # one round left → may request again


def test_followup_caps_at_two_rounds_then_forces_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []
    responses = [_NEED_TRANSCRIPT, "Final answer with what exists."]

    def fake_call_llm(prompt: str, **_k: object) -> str:
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    monkeypatch.setattr(followup, "call_llm", fake_call_llm)
    _events, outcome = _drain(
        run_followup_rounds(
            question="q",
            needs=[EvidenceNeed(kind="transcript", ticker="TST", query="credit quality")],
            items=[],
            base_context="CTX",
            thread_text="",
            repo_root=repo,
            db_path=_db(repo),
            scope_tickers=["TST"],
            ledger_ticker=None,
        )
    )
    assert outcome.rounds == MAX_ROUNDS
    assert outcome.final_text == "Final answer with what exists."
    assert "NEED MORE EVIDENCE?" in prompts[0]
    assert "NEED MORE EVIDENCE?" not in prompts[1]  # round cap → forced
    assert "Do NOT request more evidence" in prompts[1]


def test_followup_errors_when_model_keeps_requesting(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_need(*_a: object, **_k: object) -> str:
        return _NEED_TRANSCRIPT

    monkeypatch.setattr(followup, "call_llm", always_need)
    _events, outcome = _drain(
        run_followup_rounds(
            question="q",
            needs=[EvidenceNeed(kind="transcript", ticker="TST", query="credit quality")],
            items=[],
            base_context="CTX",
            thread_text="",
            repo_root=repo,
            db_path=_db(repo),
            scope_tickers=["TST"],
            ledger_ticker=None,
        )
    )
    assert outcome.final_text is None
    assert outcome.rounds == MAX_ROUNDS
    assert outcome.error is not None and "round cap" in outcome.error


def test_followup_call_failure_degrades_to_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("transport down")

    monkeypatch.setattr(followup, "call_llm", boom)
    _events, outcome = _drain(
        run_followup_rounds(
            question="q",
            needs=[EvidenceNeed(kind="journal")],
            items=[],
            base_context="CTX",
            thread_text="",
            repo_root=repo,
            db_path=_db(repo),
            scope_tickers=["TST"],
            ledger_ticker=None,
        )
    )
    assert outcome.final_text is None
    assert outcome.error is not None and "RuntimeError" in outcome.error


def test_followup_unusable_needs_force_single_pass_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_call_llm(prompt: str, **_k: object) -> str:
        prompts.append(prompt)
        return "Answering with what exists."

    monkeypatch.setattr(followup, "call_llm", fake_call_llm)
    _events, outcome = _drain(
        run_followup_rounds(
            question="q",
            needs=[],  # parse said "need request" but no entry validated
            items=[],
            base_context="CTX",
            thread_text="",
            repo_root=repo,
            db_path=_db(repo),
            scope_tickers=["TST"],
            ledger_ticker=None,
        )
    )
    assert outcome.final_text == "Answering with what exists."
    assert "could not be parsed" in prompts[0]
    assert "Do NOT request more evidence" in prompts[0]
    assert "NEED MORE EVIDENCE?" not in prompts[0]


# ----------------------------------------------------------------------------
# Engine wiring — portfolio scope


def _portfolio_pack() -> ContextPack:
    return ContextPack(scope="portfolio", default_tickers=["TST"], system_context="SYS")


def _narrative(repo: Path, text: str) -> list[dict[str, object]]:
    turn = ask_engine.AskTurn(text=text)
    return list(
        ask_engine._narrative_events(  # pyright: ignore[reportPrivateUsage]
            text,
            turn,
            _portfolio_pack(),
            repo_root=repo,
            db_path=_db(repo),
            emit_stage=True,
        )
    )


def test_engine_portfolio_loop_end_to_end(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    prompts: list[str] = []

    def fake_stream(prompt: str, *, purpose: str = "ask_answer"):
        prompts.append(prompt)
        yield {"type": "delta", "text": _NEED_TRANSCRIPT}
        yield {"type": "final", "text": _NEED_TRANSCRIPT}

    def fake_call_llm(prompt: str, **_k: object) -> str:
        # Cite every evidence item the augmented prompt presents ("[n] ..."
        # lines), so the citations event must include the round-2 item.
        markers = sorted({int(m.group(1)) for m in re.finditer(r"^\[(\d+)\]", prompt, re.M)})
        return "Coverage was thin in Q1 2024 " + "".join(f"[{n}]" for n in markers) + "."

    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
    monkeypatch.setattr(followup, "call_llm", fake_call_llm)
    events = _narrative(repo, "did management's framing of asset quality change?")
    kinds = [e["type"] for e in events]
    # The need-request never leaks as a delta; the loop's stages show instead.
    assert '{"need"' not in "".join(
        str(e.get("text") or "") for e in events if e["type"] == "delta"
    )
    assert "retrieving" in [e.get("stage") for e in events if e["type"] == "stage"]
    final = next(e for e in events if e["type"] == "final")
    assert str(final["text"]).startswith("Coverage was thin in Q1 2024")
    grounding = next(e for e in events if e["type"] == "grounding")
    assert grounding["rounds"] == 1
    assert grounding["evidence_new"] == 1
    citations = next(e for e in events if e["type"] == "citations")
    items = cast("list[dict[str, object]]", citations["items"])
    # Round-2 evidence (the Q1'24 call, beyond one-shot's latest-two reach)
    # entered the same numbered system and carries the highest n.
    assert "Q1'24" in str(items[-1]["label"])
    assert items[-1]["n"] == cast("int", grounding["evidence_total"])
    assert kinds.index("final") < kinds.index("grounding") < kinds.index("citations")
    # Pass 1 was offered the protocol.
    assert "NEED MORE EVIDENCE?" in prompts[0]


def test_engine_portfolio_prose_answer_streams_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(_prompt: str, *, purpose: str = "ask_answer"):
        yield {"type": "delta", "text": "Plain "}
        yield {"type": "delta", "text": "answer."}
        yield {"type": "final", "text": "Plain answer."}

    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
    events = _narrative(repo, "tell me about the quarter")
    kinds = [e["type"] for e in events]
    assert kinds == ["stage", "delta", "delta", "final", "grounding"]
    grounding = next(e for e in events if e["type"] == "grounding")
    assert grounding["rounds"] == 0


def test_engine_portfolio_followup_failure_yields_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(_prompt: str, *, purpose: str = "ask_answer"):
        yield {"type": "final", "text": _NEED_TRANSCRIPT}

    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("down")

    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
    monkeypatch.setattr(followup, "call_llm", boom)
    events = _narrative(repo, "did management's framing change?")
    assert events[-1]["type"] == "error"
    assert "follow-up" in str(events[-1]["error"])


def test_engine_unarmed_turn_has_no_protocol_and_passes_json_through(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_stream(prompt: str, *, purpose: str = "ask_answer"):
        prompts.append(prompt)
        yield {"type": "delta", "text": _NEED_TRANSCRIPT}
        yield {"type": "final", "text": _NEED_TRANSCRIPT}

    monkeypatch.setattr(narrative_transport, "stream_llm_text", fake_stream)
    monkeypatch.setattr(followup, "should_skip_for_budget", _over_cap)
    events = _narrative(repo, "tell me about the quarter")
    # Disarmed = pre-S7 behavior: no protocol offered, the response (whatever
    # it is) is the final answer, no grounding frame.
    assert "NEED MORE EVIDENCE?" not in prompts[0]
    assert [e["type"] for e in events] == ["stage", "delta", "final"]
    assert next(e for e in events if e["type"] == "final")["text"] == _NEED_TRANSCRIPT


def test_need_protocol_block_names_kinds_and_rounds() -> None:
    block = need_protocol_block()
    assert "transcript" in block and "holdings" in block and "dcf" in block
    assert f"{MAX_ROUNDS} retrieval round(s)" in block
    assert '{"need":' in block
