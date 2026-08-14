# pyright: reportPrivateUsage=false
"""Engine and event-boundary contracts for externally persisted Ask exchanges."""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import ask.engine as ask_engine
from ask.context import ContextPack
from ask.engine import AskTurn, respond_turn
from ask.exchange_store import (
    ExchangeArtifactsV1,
    ResearchContextV1,
    SessionContextV1,
    begin_exchange,
    get_exchange,
    hash_request_payload,
    orchestrate_exchange_events,
    put_session_context,
    replay_exchange_events,
)
from ask.grounding_trace import persist_grounding_trace
from ask.store import append_turn, load_turns
from execution import comments_server
from viewspec.nl_compile import NLCompileResult
from viewspec.spec import ViewSpec


def _empty_mapping(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {}


def _empty_strings(*_args: object, **_kwargs: object) -> list[str]:
    return []


def _empty_evidence(*_args: object, **_kwargs: object) -> list[object]:
    return []


def _false(*_args: object, **_kwargs: object) -> bool:
    return False


def _none(*_args: object, **_kwargs: object) -> None:
    return None


def _final_llm_events(*_args: object, **_kwargs: object) -> Iterator[dict[str, object]]:
    yield {"type": "final", "text": "Answer"}


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_path = migrated_db(tmp_path / "external-exchange.db", target="head")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO ask_sessions(id,scope,title,created_at,updated_at) "
            "VALUES ('session-1','portfolio','','2026-08-09T12:00:00',"
            "'2026-08-09T12:00:00')"
        )
        connection.commit()
    put_session_context(
        "session-1",
        SessionContextV1(
            company_ticker="NU",
            coverage_role_at_creation="portfolio",
            lifecycle_at_creation="active",
            category="research",
        ),
        db_path=db_path,
    )
    return db_path


def _begin(db_path: Path):
    return begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=hash_request_payload({"query": "What changed?"}),
        user_text="What changed?",
        expected_revision=0,
        db_path=db_path,
    ).exchange


def _portfolio_pack() -> ContextPack:
    return ContextPack(
        scope="portfolio",
        default_tickers=["NU"],
        system_context="Portfolio context",
        narrative_purpose="ask_answer",
    )


def test_orchestrator_holds_final_until_trailing_artifacts_are_durable(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    source: list[dict[str, object]] = [
        {"type": "delta", "text": "Answer"},
        {"type": "fragment", "html": "<div>chart</div>", "spec": {"tickers": ["NU"]}},
        {"type": "final", "text": "Answer", "route": "data"},
        {"type": "grounding", "rounds": 1},
        {
            "type": "citations",
            "items": [{"href": "/source/7", "candidate_kind": "fact", "candidate_id": "fact:7"}],
        },
        {
            "type": "diff_proposal",
            "proposal_ref": "proposal:7",
            "diff": {"mutable": "body must not become durable authority"},
        },
    ]

    output = list(orchestrate_exchange_events(iter(source), exchange=exchange, db_path=db_path))

    assert [event["type"] for event in output] == [
        "delta",
        "fragment",
        "grounding",
        "citations",
        "diff_proposal",
        "final",
    ]
    assert sum(event["type"] == "final" for event in output) == 1
    completed = get_exchange("request-1", db_path=db_path)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.artifacts == ExchangeArtifactsV1(
        route="data",
        view_spec={"tickers": ["NU"]},
        proposal_ref="proposal:7",
        source_links=["/source/7"],
        fact_links=["fact:7"],
    )
    assert [(turn.role, turn.text) for turn in load_turns("session-1", db_path=db_path)] == [
        ("user", "What changed?"),
        ("assistant", "Answer"),
    ]


def test_replay_reconstructs_artifact_events_and_one_terminal_final(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    trace = persist_grounding_trace(
        db_path,
        question="What changed?",
        scope_tickers=("NU",),
        route="narrative",
        strategy="sql_facts_and_lexical_documents",
        outcome="no_evidence",
        items=(),
        session_id="session-1",
    )
    source: list[dict[str, object]] = [
        {
            "type": "retrieval",
            "trace_id": trace.trace_id,
            "route": "narrative",
            "strategy": "sql_facts_and_lexical_documents",
            "outcome": "no_evidence",
            "item_count": 0,
        },
        {"type": "final", "text": "Answer", "route": "narrative"},
        {"type": "citations", "items": [{"href": "/source/7"}]},
        {"type": "diff_proposal", "proposal_ref": "proposal:7", "diff": {}},
    ]
    list(
        orchestrate_exchange_events(
            iter(source),
            exchange=exchange,
            db_path=db_path,
        )
    )
    completed = get_exchange("request-1", db_path=db_path)
    assert completed is not None

    replayed = list(replay_exchange_events(completed, db_path=db_path))

    assert [event["type"] for event in replayed] == [
        "artifacts",
        "retrieval",
        "citations",
        "proposal_ref",
        "final",
    ]
    assert replayed[1]["trace_id"] == trace.trace_id
    assert load_turns("session-1", db_path=db_path)[-1].grounding_trace_id == trace.trace_id
    assert replayed[-1]["text"] == "Answer"


def test_grounded_exchange_releases_no_answer_when_trace_binding_fails(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    source: list[dict[str, object]] = [
        {
            "type": "retrieval",
            "trace_id": "ask-grounding:" + ("f" * 64),
            "route": "narrative",
            "strategy": "sql_facts_and_lexical_documents",
            "outcome": "ready",
            "item_count": 1,
        },
        {"type": "delta", "text": "Uncommitted answer"},
        {"type": "final", "text": "Uncommitted answer", "route": "narrative"},
    ]

    output = list(orchestrate_exchange_events(iter(source), exchange=exchange, db_path=db_path))

    assert [event["type"] for event in output] == ["retrieval", "error"]
    assert all(event.get("text") != "Uncommitted answer" for event in output)


def test_external_engine_mode_does_not_duplicate_narrative_or_shadow_turns(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    monkeypatch.setattr(ask_engine, "metric_catalog", _empty_mapping)
    monkeypatch.setattr(ask_engine, "tracked_tickers", _empty_strings)
    monkeypatch.setattr(ask_engine, "gather_evidence", _empty_evidence)
    monkeypatch.setattr(ask_engine, "followup_armed", _false)
    monkeypatch.setattr(ask_engine, "_shadow_retrieval", _none)
    monkeypatch.setattr(
        ask_engine.narrative_transport,
        "stream_llm_text",
        _final_llm_events,
    )
    turn = AskTurn(
        text="What changed?",
        session_id="session-1",
        persistence_mode="external_exchange",
        authoritative_user_turn_id=exchange.user_turn_id,
    )

    for mode in ("legacy", "shadow"):
        events = list(
            respond_turn(
                turn,
                _portfolio_pack(),
                db_path=db_path,
                repo_root=tmp_path,
                retrieval_mode=mode,
            )
        )
        assert any(event["type"] == "final" for event in events)
        assert [(row.role, row.text) for row in load_turns("session-1", db_path=db_path)] == [
            ("user", "What changed?")
        ]


def test_external_multi_turn_history_excludes_current_user_and_metadata_from_prompt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _database(tmp_path, migrated_db)
    append_turn(session_id="session-1", role="user", text="Prior question", db_path=db_path)
    append_turn(session_id="session-1", role="assistant", text="Prior answer", db_path=db_path)
    exchange = _begin(db_path)
    retrieval_queries: list[str] = []
    prompts: list[str] = []
    monkeypatch.setattr(ask_engine, "metric_catalog", _empty_mapping)
    monkeypatch.setattr(ask_engine, "tracked_tickers", _empty_strings)

    def _gather_evidence(query: str, **_kwargs: object) -> list[object]:
        retrieval_queries.append(query)
        return []

    monkeypatch.setattr(
        ask_engine,
        "gather_evidence",
        _gather_evidence,
    )
    monkeypatch.setattr(ask_engine, "followup_armed", _false)

    def _stream(prompt: str, **_kwargs: object) -> Iterator[dict[str, object]]:
        prompts.append(prompt)
        yield {"type": "final", "text": "Current answer"}

    monkeypatch.setattr(ask_engine.narrative_transport, "stream_llm_text", _stream)
    turn = AskTurn(
        text="What changed?",
        session_id="session-1",
        persistence_mode="external_exchange",
        authoritative_user_turn_id=exchange.user_turn_id,
        research_context={
            "schema_version": "research_context.v1",
            "fact_ref": "fin:NU:revenue:2026Q2",
            "capability_id": "research.change_feed.chat",
            "card_key": "what_changed",
            "source_ids": [],
        },
    )
    engine_events = respond_turn(
        turn,
        _portfolio_pack(),
        db_path=db_path,
        repo_root=tmp_path,
    )
    list(orchestrate_exchange_events(engine_events, exchange=exchange, db_path=db_path))

    prompt = prompts[0]
    prior_thread, current_user = prompt.split("\n\n---\n\nUSER:\n", maxsplit=1)
    assert prior_thread.count("Prior question") == 1
    assert prior_thread.count("Prior answer") == 1
    assert "What changed?" not in prior_thread
    assert current_user == "What changed?"
    assert "research.change_feed.chat" not in prompt
    assert "what_changed" not in prompt
    assert retrieval_queries == ["What changed?\nEvidence handles: fin:NU:revenue:2026Q2"]
    assert [(row.role, row.text) for row in load_turns("session-1", db_path=db_path)] == [
        ("user", "Prior question"),
        ("assistant", "Prior answer"),
        ("user", "What changed?"),
        ("assistant", "Current answer"),
    ]


@pytest.mark.parametrize("fact_ref", ["kpi:NU:123", "fin:NU:revenue:2026-06-30"])
def test_canonical_fact_refs_reach_retrieval_and_malformed_refs_reject(fact_ref: str) -> None:
    context = ResearchContextV1(fact_ref=fact_ref)
    turn = AskTurn(text="Question", research_context=context.model_dump(mode="json"))

    assert ask_engine._retrieval_text("Question", turn) == (
        f"Question\nEvidence handles: {fact_ref}"
    )
    with pytest.raises(ValidationError):
        ResearchContextV1(fact_ref="../../prompt injection")
    source_only = ResearchContextV1(source_ref="source:7", source_ids=["source:8"])
    assert (
        ask_engine._retrieval_text(
            "Question",
            AskTurn(text="Question", research_context=source_only.model_dump(mode="json")),
        )
        == "Question"
    )


def test_external_command_and_data_routes_leave_turn_persistence_to_exchange(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    command = AskTurn(
        text="/help",
        session_id="session-1",
        persistence_mode="external_exchange",
        authoritative_user_turn_id=exchange.user_turn_id,
    )
    command_events = list(
        respond_turn(command, _portfolio_pack(), db_path=db_path, repo_root=tmp_path)
    )
    assert command_events[-1]["route"] == "command"
    assert len(load_turns("session-1", db_path=db_path)) == 1

    spec = ViewSpec.from_dict(
        {
            "tickers": ["NU"],
            "metrics": ["fin:revenue"],
            "transform": "level",
            "cadence": "quarterly",
            "periods": 4,
        }
    )
    import viewspec.nl_compile as nl_compile

    def _compile_view(*_args: object, **_kwargs: object) -> NLCompileResult:
        return NLCompileResult(status="ok", spec=spec)

    def _execute_view(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            spec=spec,
            period_labels=["Q1", "Q2", "Q3", "Q4"],
            rows=[SimpleNamespace(cells=[])],
        )

    def _render_view(*_args: object, **_kwargs: object) -> str:
        return "<div />"

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", _compile_view)
    monkeypatch.setattr(ask_engine, "execute_view", _execute_view)
    monkeypatch.setattr(ask_engine, "render_view_fragment", _render_view)
    data = AskTurn(
        text="/view NU revenue",
        session_id="session-1",
        persistence_mode="external_exchange",
        authoritative_user_turn_id=exchange.user_turn_id,
    )
    data_events = list(respond_turn(data, _portfolio_pack(), db_path=db_path, repo_root=tmp_path))
    assert data_events[-1]["route"] == "data"
    assert len(load_turns("session-1", db_path=db_path)) == 1


def test_sealed_external_mode_reuses_authoritative_user_and_skips_assistant_cas(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    turn = AskTurn(
        text="What changed?",
        session_id="session-1",
        persistence_mode="external_exchange",
        authoritative_user_turn_id=exchange.user_turn_id,
    )

    def _fail_user_turn(**_kwargs: object) -> None:
        pytest.fail("sealed mode appended a duplicate user turn")

    def _fail_assistant_turn(**_kwargs: object) -> None:
        pytest.fail("sealed mode appended a duplicate assistant turn")

    monkeypatch.setattr(ask_engine, "_store_append_turn", _fail_user_turn)
    monkeypatch.setattr(ask_engine, "_store_append_assistant_cas", _fail_assistant_turn)

    assert ask_engine._bind_sealed_user_turn(turn, "What changed?", db_path=db_path) == (
        exchange.user_turn_id
    )
    ask_engine._persist_sealed_assistant(
        turn,
        user_turn_id=exchange.user_turn_id,
        user_text="What changed?",
        text="Answer",
        citations=[],
        model="test-model",
        db_path=db_path,
    )
    assert len(load_turns("session-1", db_path=db_path)) == 1


def test_disconnect_safe_drain_consumes_durable_generator_after_stop() -> None:
    consumed: list[str] = []

    def events() -> Iterator[dict[str, object]]:
        consumed.append("started")
        yield {"type": "stage"}
        consumed.append("completed")
        yield {"type": "final"}

    stop = threading.Event()
    stop.set()
    chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=1)

    comments_server._drain_durable_events(iter(events()), chunks, stop)

    assert consumed == ["started", "completed"]
