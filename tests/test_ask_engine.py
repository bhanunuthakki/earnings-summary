"""The unified ask engine (src/ask/): deterministic routing, the three
response paths (command / data / narrative), context packs (ticker vs
portfolio), narrative fallback after a failed compile, report-thread
persistence for data turns, and the /api/ask JSON folding.

All LLM seams are monkeypatched — the suite never spends:
  viewspec.nl_compile.compile_nl_to_viewspec   (data path, lazy-imported)
  chat_session.stream_llm_text                 (portfolio narrative)
  chat_session.build_chat_response.stream_response  (ticker narrative)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import chat_session
from ask import engine as ask_engine
from ask.context import (
    ContextPack,
    build_portfolio_pack,
    build_ticker_pack,
    tracked_tickers,
)
from ask.engine import AskTurn, fold_events, respond_turn, route_turn, sanitize_history
from ask.grounding import EvidenceItem
from viewspec.nl_compile import NLCompileResult
from viewspec.spec import ViewSpec

RD = date(2026, 5, 1)

_SPEC = ViewSpec.from_dict(
    {
        "tickers": ["TST"],
        "metrics": ["fin:revenue"],
        "transform": "level",
        "cadence": "quarterly",
        "periods": 8,
    }
)

_TRACKED_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL
);
"""


@pytest.fixture
def missing_db(tmp_path: Path) -> Path:
    """A db_path that doesn't exist — catalog/tracked lookups degrade empty."""
    return tmp_path / "missing.db"


@pytest.fixture
def tracked_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(_TRACKED_DDL)
    conn.executemany(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type) VALUES (?, ?, ?, ?)",
        [
            ("bhanu", "TST", "Test Co", "portfolio"),
            ("bhanu", "EVA", "Eval Co", "evaluation"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _portfolio_pack() -> ContextPack:
    return ContextPack(
        scope="portfolio",
        default_tickers=["TST"],
        system_context="PORTFOLIO SYSTEM CONTEXT",
    )


# ----------------------------------------------------------------------------
# route_turn — the deterministic routing table
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # slash prefixes
        ("/view NU revenue", "data"),
        ("/view", "data"),  # bare → respond_turn yields the usage error
        ("/views of the world", "narrative"),  # prefix needs a word boundary
        ("/discovery list", "command"),
        ("/discovery build WDC", "command"),
        ("/help", "command"),
        ("/frobnicate now", "narrative"),  # unknown command → the assistant explains
        # narrative markers beat data markers
        ("why did margins compress?", "narrative"),
        ("what's the bear case on MELI?", "narrative"),
        ("tell me about NU", "narrative"),
        ("summarize the last transcript", "narrative"),
        ("should I be worried about NPLs?", "narrative"),
        # strong data signals
        ("NU vs MELI revenue growth, last 8 quarters", "data"),
        ("now annual", "data"),
        ("same but as margins", "data"),
        ("3-year CAGR for revenue", "data"),
        # default
        ("thanks", "narrative"),
        ("hello", "narrative"),
    ],
)
def test_route_turn_table(text: str, expected: str) -> None:
    assert route_turn(text) == expected


def test_route_turn_metric_label_grounding() -> None:
    """A bare metric question routes to data only when the catalog actually
    carries that label for the universe."""
    assert route_turn("TST revenue") == "narrative"  # no labels → no signal
    assert route_turn("TST revenue", metric_labels=["revenue"]) == "data"
    assert route_turn("TST revenue?", metric_labels=["revenue"]) == "data"
    # multi-word KPI labels match too
    assert route_turn("total customers for TST", metric_labels=["Total customers"]) == "data"
    # short/irrelevant labels don't false-positive
    assert route_turn("hi there", metric_labels=["revenue", "Total customers"]) == "narrative"


def test_route_turn_ticker_mention_only_counts_as_refinement() -> None:
    """ "add MELI" refines an existing view; standalone it's a narrative ask."""
    assert route_turn("add MELI", known_tickers={"MELI"}) == "narrative"
    assert route_turn("add MELI", has_context_spec=True, known_tickers={"MELI"}) == "data"
    # untracked symbols never count
    assert route_turn("add XYZ", has_context_spec=True, known_tickers={"MELI"}) == "narrative"


def test_sanitize_history_validates_and_caps() -> None:
    raw: object = [
        {"role": "user", "text": "q1"},
        {"role": "assistant", "text": "a1"},
        {"role": "system", "text": "nope"},  # bad role dropped
        {"role": "user", "text": "   "},  # empty dropped
        "garbage",
        {"role": "user", "text": "x" * 5000},  # truncated
    ]
    out = sanitize_history(raw)
    assert [h["role"] for h in out] == ["user", "assistant", "user"]
    assert len(out[-1]["text"]) == 1200
    assert sanitize_history("not a list") == []
    long = [{"role": "user", "text": f"q{i}"} for i in range(20)]
    assert len(sanitize_history(long)) == 8


# ----------------------------------------------------------------------------
# respond_turn — command route
# ----------------------------------------------------------------------------


def test_command_route_replies_without_llm(tmp_path: Path, missing_db: Path) -> None:
    from dispatch_registry import Registry

    events = list(
        respond_turn(
            AskTurn(text="/help"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
            registry=Registry(),
        )
    )
    assert [e["type"] for e in events] == ["delta", "final"]
    assert events[-1]["route"] == "command"
    assert "/discovery" in str(events[-1]["text"])
    assert "/view" in str(events[-1]["text"])


def test_command_route_without_registry_degrades(tmp_path: Path, missing_db: Path) -> None:
    events = list(
        respond_turn(
            AskTurn(text="/discovery list"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
            registry=None,
        )
    )
    assert events[-1]["type"] == "final"
    assert "aren't available" in str(events[-1]["text"])


# ----------------------------------------------------------------------------
# respond_turn — data route (compile + execute monkeypatched)
# ----------------------------------------------------------------------------


def _patch_data_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compile_result: NLCompileResult,
    n_rows: int = 2,
) -> dict[str, object]:
    """Stub the compile + execute + render seams; returns the capture dict."""
    import viewspec.nl_compile as nlc

    seen: dict[str, object] = {}

    def fake_compile(query: str, **kw: object) -> NLCompileResult:
        seen["query"] = query
        seen.update(kw)
        return compile_result

    def fake_execute(spec: ViewSpec, *, db_path: Path) -> SimpleNamespace:
        seen["executed_spec"] = spec
        return SimpleNamespace(rows=[object()] * n_rows)

    def fake_render(view: object, **_kw: object) -> str:
        return "<div>FAKE-FRAGMENT</div>"

    monkeypatch.setattr(nlc, "compile_nl_to_viewspec", fake_compile)
    monkeypatch.setattr(ask_engine, "execute_view", fake_execute)
    monkeypatch.setattr(ask_engine, "render_view_fragment", fake_render)
    return seen


def test_data_route_compiles_runs_and_renders(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _patch_data_path(monkeypatch, compile_result=NLCompileResult(status="ok", spec=_SPEC))
    turn = AskTurn(
        text="TST revenue growth, last 8 quarters",
        tickers=["TST"],
        context_spec={"tickers": ["TST"]},
    )
    events = list(respond_turn(turn, _portfolio_pack(), db_path=missing_db, repo_root=tmp_path))
    kinds = [e["type"] for e in events]
    assert kinds == ["stage", "stage", "fragment", "final"]
    assert [e.get("stage") for e in events[:2]] == ["compiling", "running"]
    assert seen["query"] == "TST revenue growth, last 8 quarters"
    assert seen["context_tickers"] == ["TST"]
    assert seen["context_spec"] == {"tickers": ["TST"]}
    frag = events[2]
    assert frag["html"] == "<div>FAKE-FRAGMENT</div>"
    assert frag["spec"] == _SPEC.to_dict()
    final = events[3]
    assert final["route"] == "data"
    assert "2 series" in str(final["text"])
    assert "(refined the previous view)" in str(final["text"])


def test_data_route_zero_series_carries_cadence_hint(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_data_path(monkeypatch, compile_result=NLCompileResult(status="ok", spec=_SPEC), n_rows=0)
    events = list(
        respond_turn(
            AskTurn(text="/view TST revenue"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    final = events[-1]
    assert final["type"] == "final"
    text = str(final["text"])
    assert text.startswith("0 series")
    assert "annual" in text  # spec is quarterly → suggest the other cadence


def test_forced_view_surfaces_compile_error_without_fallback(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_data_path(
        monkeypatch,
        compile_result=NLCompileResult(status="error", message="no matching metric token"),
    )

    def no_llm(prompt: str) -> object:
        raise AssertionError("narrative transport must not run on /view")

    monkeypatch.setattr(chat_session, "stream_llm_text", no_llm)
    events = list(
        respond_turn(
            AskTurn(text="/view garbage"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "error"
    assert "no matching metric token" in str(events[-1]["error"])


def test_forced_view_budget_skip_keeps_its_code(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_data_path(
        monkeypatch,
        compile_result=NLCompileResult(status="budget_skipped", message="over budget"),
    )
    events = list(
        respond_turn(
            AskTurn(text="/view TST revenue"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert events[-1] == {"type": "error", "error": "over budget", "code": "budget_skipped"}


def test_failed_compile_falls_back_to_narrative(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unforced data question whose compile fails still gets answered —
    through the narrative path, with a stage note saying so."""
    _patch_data_path(monkeypatch, compile_result=NLCompileResult(status="error", message="nope"))

    def fake_llm(prompt: str):
        yield {"type": "delta", "text": "prose "}
        yield {"type": "final", "text": "prose answer"}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    events = list(
        respond_turn(
            AskTurn(text="TST revenue growth, last 8 quarters"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    kinds = [e["type"] for e in events]
    assert kinds == ["stage", "stage", "delta", "final"]
    note_stage = events[1]
    assert note_stage["route"] == "narrative"
    assert "answering in prose" in str(note_stage.get("note"))
    assert events[-1] == {"type": "final", "text": "prose answer", "route": "narrative"}


def test_data_turn_persists_to_report_thread(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the report drawer (ticker pack), data turns keep the per-report
    thread continuous: user question + the message line are stored."""
    _patch_data_path(monkeypatch, compile_result=NLCompileResult(status="ok", spec=_SPEC))
    pack = build_ticker_pack("NU", RD)
    events = list(
        respond_turn(
            AskTurn(text="/view NU revenue"),
            pack,
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert events[-1]["type"] == "final"
    thread = chat_session.load_thread(tmp_path, "NU", RD)
    assert [t.role for t in thread] == ["user", "assistant"]
    assert thread[0].text == "/view NU revenue"
    assert "live data view" in thread[1].text


# ----------------------------------------------------------------------------
# respond_turn — narrative routes
# ----------------------------------------------------------------------------


def test_ticker_narrative_uses_chat_session_machinery(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticker scope delegates to the existing report-chat session (its own
    system prompt + persistence) — the same seam the server tests patch."""
    seen: dict[str, object] = {}

    def fake_stream_response(*, repo_root: Path, ticker: str, report_date: date, user_message: str):
        seen.update(
            repo_root=repo_root, ticker=ticker, report_date=report_date, message=user_message
        )
        yield {"type": "delta", "text": "hi"}
        yield {"type": "final", "text": "hi"}

    monkeypatch.setattr(chat_session.build_chat_response, "stream_response", fake_stream_response)
    pack = build_ticker_pack("NU", RD)
    events = list(
        respond_turn(
            AskTurn(text="why is NPL formation seasonal?"),
            pack,
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert seen == {
        "repo_root": tmp_path,
        "ticker": "NU",
        "report_date": RD,
        "message": "why is NPL formation seasonal?",
    }
    assert [e["type"] for e in events] == ["stage", "delta", "final"]
    assert events[0] == {"type": "stage", "stage": "answering", "route": "narrative"}


def test_portfolio_narrative_composes_pack_context_and_history(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_llm(prompt: str):
        prompts.append(prompt)
        yield {"type": "delta", "text": "answer"}
        yield {"type": "final", "text": "answer"}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    turn = AskTurn(
        text="how concentrated is the book?",
        history=[
            {"role": "user", "text": "earlier question"},
            {"role": "assistant", "text": "earlier answer"},
        ],
    )
    events = list(respond_turn(turn, _portfolio_pack(), db_path=missing_db, repo_root=tmp_path))
    assert events[-1] == {"type": "final", "text": "answer", "route": "narrative"}
    prompt = prompts[0]
    assert prompt.startswith("PORTFOLIO SYSTEM CONTEXT")
    assert "[USER] earlier question" in prompt
    assert "[ASSISTANT] earlier answer" in prompt
    assert prompt.rstrip().endswith("how concentrated is the book?")


def test_portfolio_narrative_extracts_diff_proposal(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = (
        "I'd tighten the thesis.\n\n"
        '```json\n{"diff": {"target_file": "micro_thesis/holdings/TST.json", '
        '"target_path": "thesis", "new_value": "new", "summary": "tighten"}}\n```'
    )

    def fake_llm(prompt: str):
        yield {"type": "final", "text": answer}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    events = list(
        respond_turn(
            AskTurn(text="tell me about the thesis edit"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert events[-1]["type"] == "diff_proposal"
    diff = events[-1]["diff"]
    assert isinstance(diff, dict)
    assert diff["target_path"] == "thesis"


def test_portfolio_narrative_transport_error_passthrough(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_llm(prompt: str):
        yield {"type": "error", "error": "claude CLI not found in PATH"}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    events = list(
        respond_turn(
            AskTurn(text="hello"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert events[-1]["type"] == "error"
    assert "claude CLI" in str(events[-1]["error"])


def test_empty_message_is_an_error(tmp_path: Path, missing_db: Path) -> None:
    events = list(
        respond_turn(AskTurn(text="   "), _portfolio_pack(), db_path=missing_db, repo_root=tmp_path)
    )
    assert events == [{"type": "error", "error": "empty message"}]
    usage = list(
        respond_turn(
            AskTurn(text="/view  "),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert usage[-1]["type"] == "error"
    assert "usage: /view" in str(usage[-1]["error"])


# ----------------------------------------------------------------------------
# grounded narrative (Ask v3) — evidence injection + citations event
# ----------------------------------------------------------------------------


def _evidence(n: int) -> EvidenceItem:
    return EvidenceItem(
        n=n,
        kind="fact",
        label=f"TST · Metric{n}",
        text=f"TST Metric{n} (newest first): Q1'26 5",
        doc_id=9,
        href=f"/source/9?n={n}",
        source_url=None,
    )


def _stub_gather(*items: EvidenceItem):
    def fake(
        question: str,
        *,
        repo_root: Path,
        db_path: Path,
        scope_tickers: list[str],
        cache_key: str | None = None,
    ) -> list[EvidenceItem]:
        del question, repo_root, db_path, scope_tickers, cache_key
        return list(items)

    return fake


def test_portfolio_narrative_grounds_prompt_and_emits_pruned_citations(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ask_engine, "gather_evidence", _stub_gather(_evidence(1), _evidence(2)))
    prompts: list[str] = []

    def fake_llm(prompt: str):
        prompts.append(prompt)
        yield {"type": "final", "text": "Growth held up well [1]."}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    events = list(
        respond_turn(
            AskTurn(text="why is growth holding up?"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert [e["type"] for e in events] == ["stage", "final", "citations"]
    # The answering stage announces the grounding.
    assert events[0]["note"] == "grounded on 2 sources"
    # The evidence block rode into the prompt between pack context and thread.
    prompt = prompts[0]
    assert prompt.startswith("PORTFOLIO SYSTEM CONTEXT")
    assert "EVIDENCE" in prompt
    assert "[1] TST Metric1" in prompt
    assert "CITE-OR-SAY-UNSURE" in prompt
    assert prompt.index("EVIDENCE") < prompt.index("PRIOR THREAD")
    # Only the marker the answer used survives into the citations event.
    raw_items = events[-1]["items"]
    assert isinstance(raw_items, list)
    items = cast("list[dict[str, object]]", raw_items)
    assert [c["n"] for c in items] == [1]
    assert items[0]["href"] == "/source/9?n=1"
    # The claim audit is blocked by the conftest seam here → the event fails
    # closed to the legacy answer-level shape (S8 contract).
    assert events[-1]["grounding"] == "answer_level"
    assert "claims" not in events[-1]


def test_portfolio_narrative_unused_evidence_emits_no_citations(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ask_engine, "gather_evidence", _stub_gather(_evidence(1)))

    def fake_llm(prompt: str):
        yield {"type": "final", "text": "An answer with no markers at all."}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    events = list(
        respond_turn(
            AskTurn(text="why is growth holding up?"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert [e["type"] for e in events] == ["stage", "final"]


def test_portfolio_narrative_per_claim_event_reconciles_and_recovers(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The S8 happy path: the claim audit succeeds, inline markers win over
    the map's cites, and an unmarked sentence RECOVERS its cite — so item
    [2] joins the event even though the answer text never wrote ``[2]``."""
    monkeypatch.setattr(ask_engine, "gather_evidence", _stub_gather(_evidence(1), _evidence(2)))

    def fake_llm(prompt: str):
        yield {"type": "final", "text": "Growth held up well [1]. Margins expanded by 300bps."}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)

    def fake_struct(prompt: str, **kwargs: object) -> object:
        assert kwargs.get("purpose") == "ask_claim_grounding"
        return {
            "claims": [
                {"quote": "Growth held up well", "cites": [2], "supported": True},
                {"quote": "Margins expanded by 300bps", "cites": [2], "supported": True},
            ]
        }

    monkeypatch.setattr("ask.claims.call_llm_structured", fake_struct)
    events = list(
        respond_turn(
            AskTurn(text="why is growth holding up?"),
            _portfolio_pack(),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    cit = events[-1]
    assert cit["type"] == "citations"
    assert cit["grounding"] == "per_claim"
    raw_items = cit["items"]
    assert isinstance(raw_items, list)
    items = cast("list[dict[str, object]]", raw_items)
    assert [c["n"] for c in items] == [1, 2]
    raw_claims = cit["claims"]
    assert isinstance(raw_claims, list)
    claims = cast("list[dict[str, object]]", raw_claims)
    # Inline [1] beats the map's [2] on the first sentence; the unmarked
    # second sentence takes the map's recovered cite.
    assert claims[0]["cites"] == [1]
    assert claims[1]["cites"] == [2]
    assert all(c["supported"] is True for c in claims)


def test_ticker_narrative_passes_evidence_as_extra_context(
    tmp_path: Path, missing_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticker scope threads the evidence block through stream_response's
    extra_context kwarg — passed ONLY when evidence exists (the legacy
    four-kwarg fakes elsewhere in this suite must keep working)."""
    monkeypatch.setattr(ask_engine, "gather_evidence", _stub_gather(_evidence(1)))
    seen: dict[str, object] = {}

    def fake_stream_response(
        *,
        repo_root: Path,
        ticker: str,
        report_date: date,
        user_message: str,
        extra_context: str = "",
    ):
        seen["extra_context"] = extra_context
        yield {"type": "delta", "text": "NPLs are stable [1]."}
        yield {"type": "final", "text": "NPLs are stable [1]."}

    monkeypatch.setattr(chat_session.build_chat_response, "stream_response", fake_stream_response)
    events = list(
        respond_turn(
            AskTurn(text="why are NPLs stable?"),
            build_ticker_pack("NU", RD),
            db_path=missing_db,
            repo_root=tmp_path,
        )
    )
    assert "EVIDENCE" in str(seen["extra_context"])
    assert "[1] TST Metric1" in str(seen["extra_context"])
    assert [e["type"] for e in events] == ["stage", "delta", "final", "citations"]


def test_stream_response_appends_extra_context_to_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_llm(prompt: str):
        prompts.append(prompt)
        yield {"type": "final", "text": "answer"}

    monkeypatch.setattr(chat_session, "stream_llm_text", fake_llm)
    events = list(
        chat_session.stream_response(tmp_path, "NU", RD, "question", extra_context="EXTRA-MARKER")
    )
    assert events[-1]["type"] == "final"
    assert "EXTRA-MARKER" in prompts[0]
    assert prompts[0].index("EXTRA-MARKER") < prompts[0].index("PRIOR THREAD")
    # Default call shape (no kwarg) is unchanged.
    prompts.clear()
    list(chat_session.stream_response(tmp_path, "NU", RD, "question"))
    assert "EXTRA-MARKER" not in prompts[0]


# ----------------------------------------------------------------------------
# fold_events — the /api/ask JSON contract
# ----------------------------------------------------------------------------


def test_fold_events_view_payload_is_backward_compatible() -> None:
    out = fold_events(
        [
            {"type": "stage", "stage": "compiling", "route": "data"},
            {"type": "fragment", "html": "<div>F</div>", "spec": {"tickers": ["TST"]}},
            {"type": "final", "text": "2 series · level · quarterly, 8 periods", "route": "data"},
        ]
    )
    assert out["status"] == "ok"
    assert out["kind"] == "view"
    assert out["fragment"] == "<div>F</div>"
    assert out["spec"] == {"tickers": ["TST"]}
    assert "series" in str(out["message"])


def test_fold_events_narrative_with_note_and_diff() -> None:
    out = fold_events(
        [
            {"type": "stage", "stage": "answering", "route": "narrative", "note": "in prose"},
            {"type": "delta", "text": "chunk"},
            {"type": "final", "text": "the answer", "route": "narrative"},
            {"type": "diff_proposal", "diff": {"target_path": "thesis"}},
        ]
    )
    assert out == {
        "status": "ok",
        "kind": "narrative",
        "text": "the answer",
        "note": "in prose",
        "diff": {"target_path": "thesis"},
    }


def test_fold_events_carries_citations() -> None:
    out = fold_events(
        [
            {"type": "stage", "stage": "answering", "route": "narrative"},
            {"type": "final", "text": "grounded answer [1]", "route": "narrative"},
            {"type": "citations", "items": [{"n": 1, "label": "L", "href": "/source/9"}]},
        ]
    )
    assert out["citations"] == [{"n": 1, "label": "L", "href": "/source/9"}]
    # Legacy answer-level event: no claims/grounding keys invented.
    assert "claims" not in out
    assert "grounding" not in out


def test_fold_events_carries_per_claim_grounding() -> None:
    claims = [{"text": "Revenue grew [1].", "cites": [1], "supported": True}]
    out = fold_events(
        [
            {"type": "final", "text": "Revenue grew [1].", "route": "narrative"},
            {
                "type": "citations",
                "items": [{"n": 1, "label": "L", "href": "/source/9"}],
                "claims": claims,
                "grounding": "per_claim",
            },
        ]
    )
    assert out["claims"] == claims
    assert out["grounding"] == "per_claim"


def test_fold_events_command_and_errors() -> None:
    cmd = fold_events(
        [
            {"type": "delta", "text": "reply"},
            {"type": "final", "text": "reply", "route": "command"},
        ]
    )
    assert cmd["kind"] == "command"
    err = fold_events([{"type": "error", "error": "boom"}])
    assert err == {"status": "error", "message": "boom"}
    budget = fold_events([{"type": "error", "error": "cap", "code": "budget_skipped"}])
    assert budget == {"status": "budget_skipped", "message": "cap"}
    assert fold_events([])["status"] == "error"


# ----------------------------------------------------------------------------
# context packs
# ----------------------------------------------------------------------------


def test_build_ticker_pack_shape() -> None:
    pack = build_ticker_pack("nu", RD)
    assert pack.scope == "ticker"
    assert pack.ticker == "NU"
    assert pack.report_date == RD
    assert pack.default_tickers == ["NU"]
    assert pack.persist is True
    assert pack.system_context is None  # chat_session owns the ticker prompt


def test_build_portfolio_pack_grounds_universe_and_theses(tmp_path: Path, tracked_db: Path) -> None:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "TST.json").write_text(
        json.dumps({"thesis": "Compounding test-ware monopoly with pricing power."}),
        encoding="utf-8",
    )
    pack = build_portfolio_pack(tmp_path, tracked_db)
    assert pack.scope == "portfolio"
    assert pack.persist is False
    assert pack.default_tickers == ["TST"]
    ctx = pack.system_context or ""
    assert "portfolio: TST" in ctx
    assert "evaluation: EVA" in ctx
    assert "TST: Compounding test-ware monopoly" in ctx
    assert "/discovery list" in ctx
    assert "Socratic" in ctx


def test_build_portfolio_pack_degrades_without_db(tmp_path: Path) -> None:
    pack = build_portfolio_pack(tmp_path, tmp_path / "missing.db")
    assert pack.default_tickers == []
    assert "no tracked companies" in (pack.system_context or "")


def test_tracked_tickers(tracked_db: Path, tmp_path: Path) -> None:
    assert tracked_tickers(tracked_db) == {"TST", "EVA"}
    assert tracked_tickers(tmp_path / "nope.db") == set()
