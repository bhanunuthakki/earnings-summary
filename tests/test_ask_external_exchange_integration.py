# pyright: reportPrivateUsage=false
"""Engine and event-boundary contracts for externally persisted Ask exchanges."""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable
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
from ask.store import append_turn, load_turns
from execution import comments_server
from viewspec.nl_compile import NLCompileResult
from viewspec.spec import ViewSpec


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
        ticker=None,
        report_date=None,
        default_tickers=["NU"],
        system_context="Portfolio context",
        narrative_purpose="ask_answer",
        persist=False,
    )


def test_orchestrator_holds_final_until_trailing_artifacts_are_durable(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    source = [
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
    list(
        orchestrate_exchange_events(
            iter(
                [
                    {"type": "final", "text": "Answer", "route": "narrative"},
                    {"type": "citations", "items": [{"href": "/source/7"}]},
                    {"type": "diff_proposal", "proposal_ref": "proposal:7", "diff": {}},
                ]
            ),
            exchange=exchange,
            db_path=db_path,
        )
    )
    completed = get_exchange("request-1", db_path=db_path)
    assert completed is not None

    replayed = list(replay_exchange_events(completed, db_path=db_path))

    assert [event["type"] for event in replayed] == [
        "artifacts",
        "citations",
        "proposal_ref",
        "final",
    ]
    assert replayed[-1]["text"] == "Answer"


def test_external_engine_mode_does_not_duplicate_narrative_or_shadow_turns(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _database(tmp_path, migrated_db)
    exchange = _begin(db_path)
    monkeypatch.setattr(ask_engine, "metric_catalog", lambda *_a, **_k: {})
    monkeypatch.setattr(ask_engine, "tracked_tickers", lambda *_a, **_k: [])
    monkeypatch.setattr(ask_engine, "gather_evidence", lambda *_a, **_k: [])
    monkeypatch.setattr(ask_engine, "followup_armed", lambda *_a, **_k: False)
    monkeypatch.setattr(ask_engine, "_shadow_retrieval", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ask_engine.chat_session,
        "stream_llm_text",
        lambda *_a, **_k: iter([{"type": "final", "text": "Answer"}]),
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
    monkeypatch.setattr(ask_engine, "metric_catalog", lambda *_a, **_k: {})
    monkeypatch.setattr(ask_engine, "tracked_tickers", lambda *_a, **_k: [])
    monkeypatch.setattr(
        ask_engine,
        "gather_evidence",
        lambda query, **_kwargs: retrieval_queries.append(query) or [],
    )
    monkeypatch.setattr(ask_engine, "followup_armed", lambda *_a, **_k: False)

    def _stream(prompt: str, **_kwargs: object):
        prompts.append(prompt)
        yield {"type": "final", "text": "Current answer"}

    monkeypatch.setattr(ask_engine.chat_session, "stream_llm_text", _stream)
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

    monkeypatch.setattr(
        nl_compile,
        "compile_nl_to_viewspec",
        lambda *_a, **_k: NLCompileResult(status="ok", spec=spec),
    )
    monkeypatch.setattr(
        ask_engine,
        "execute_view",
        lambda *_a, **_k: SimpleNamespace(
            spec=spec,
            period_labels=["Q1", "Q2", "Q3", "Q4"],
            rows=[SimpleNamespace(cells=[])],
        ),
    )
    monkeypatch.setattr(ask_engine, "render_view_fragment", lambda *_a, **_k: "<div />")
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
    monkeypatch.setattr(
        ask_engine,
        "_store_append_turn",
        lambda **_kwargs: pytest.fail("sealed mode appended a duplicate user turn"),
    )
    monkeypatch.setattr(
        ask_engine,
        "_store_append_assistant_cas",
        lambda **_kwargs: pytest.fail("sealed mode appended a duplicate assistant turn"),
    )

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

    def events():
        consumed.append("started")
        yield {"type": "stage"}
        consumed.append("completed")
        yield {"type": "final"}

    stop = threading.Event()
    stop.set()
    chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=1)

    comments_server._drain_durable_events(iter(events()), chunks, stop)

    assert consumed == ["started", "completed"]
