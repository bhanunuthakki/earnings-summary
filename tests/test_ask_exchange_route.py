"""Production portfolio Ask route wiring for durable client requests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient
from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from ask.exchange_store import (  # noqa: E402
    SessionContextV1,
    begin_exchange,
    hash_request_payload,
    put_session_context,
)
from ask.store import ensure_session  # noqa: E402

_JSON_OBJECT = TypeAdapter(dict[str, object])
_JSON_ARRAY = TypeAdapter(list[object])


def _json_object(value: object) -> dict[str, object]:
    return _JSON_OBJECT.validate_python(value)


def _empty_pack(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace()


def _legacy_mode(*_args: object, **_kwargs: object) -> str:
    return "legacy"


def _answer_events(*_args: object, **_kwargs: object) -> Iterator[dict[str, object]]:
    yield {"type": "final", "text": "Answer"}


def _unexpected_engine(*_args: object, **_kwargs: object) -> None:
    pytest.fail("pending conflict invoked the engine")


def _client(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> tuple[FlaskClient, Path]:
    db_path = migrated_db(tmp_path / "route.db", target="head")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    app = comments_server.create_app(repo_root, db_path=db_path)
    return app.test_client(), db_path


def _payload(*, request_id: str = "request-1", revision: int = 0) -> dict[str, object]:
    return {
        "query": "What changed?",
        "request_id": request_id,
        "expected_revision": revision,
        "session_context": {
            "company_ticker": "NU",
            "coverage_role_at_creation": "portfolio",
            "lifecycle_at_creation": "active",
            "category": "research",
        },
        "research_context": {
            "capability_id": "research.change_feed.chat",
            "card_key": "what_changed",
            "source_ids": ["source:7"],
        },
    }


def _sse_events(response_text: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def test_durable_stream_orders_terminal_final_and_replays_without_engine(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _client(tmp_path, migrated_db)
    calls = 0

    def _respond(*_args: object, **_kwargs: object) -> Iterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        yield {"type": "delta", "text": "Answer"}
        yield {"type": "final", "text": "Answer", "route": "narrative"}
        yield {"type": "citations", "items": [{"href": "/source/7"}]}
        yield {"type": "proposal_ref", "proposal_ref": "proposal:7"}

    monkeypatch.setattr(comments_server, "build_portfolio_pack", _empty_pack)
    monkeypatch.setattr(comments_server, "respond_turn", _respond)
    monkeypatch.setattr(comments_server, "ask_retrieval_mode", _legacy_mode)

    first = client.post("/api/ask/stream", json=_payload())
    first_events = _sse_events(first.get_data(as_text=True))
    session_id = str(first_events[0]["session_id"])

    assert [event["type"] for event in first_events] == [
        "session",
        "delta",
        "citations",
        "proposal_ref",
        "final",
    ]
    assert first_events[0]["session_revision"] == 1
    first_context = _json_object(first_events[0]["session_context"])
    assert first_context["company_ticker"] == "NU"
    assert first_events[-1]["session_revision"] == 2
    history_payload = _json_object(client.get("/api/ask/sessions").get_json())
    history = _JSON_ARRAY.validate_python(history_payload["sessions"])
    first_history = _json_object(history[0])
    assert first_history["session_revision"] == 2
    history_context = _json_object(first_history["session_context"])
    assert history_context["company_ticker"] == "NU"
    detail = _json_object(client.get(f"/api/ask/sessions/{session_id}").get_json())
    assert detail["session_revision"] == 2
    detail_context = _json_object(detail["session_context"])
    assert detail_context["category"] == "research"
    replay_payload = _payload(revision=2)
    replay_payload["session_id"] = session_id
    replay = client.post("/api/ask/stream", json=replay_payload)
    replay_events = _sse_events(replay.get_data(as_text=True))
    assert [event["type"] for event in replay_events] == [
        "session",
        "artifacts",
        "citations",
        "proposal_ref",
        "final",
    ]
    assert replay_events[0]["session_revision"] == 2
    assert replay_events[-1]["session_revision"] == 2
    assert calls == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ask_turns").fetchone() == (2,)


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ({"query": "Different payload", "expected_revision": 2}, 409),
        ({"request_id": "request-2", "expected_revision": 99}, 409),
    ],
)
def test_durable_request_conflicts_are_409(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
    expected_status: int,
) -> None:
    client, _db_path = _client(tmp_path, migrated_db)
    monkeypatch.setattr(comments_server, "build_portfolio_pack", _empty_pack)
    monkeypatch.setattr(comments_server, "respond_turn", _answer_events)
    monkeypatch.setattr(comments_server, "ask_retrieval_mode", _legacy_mode)
    first = client.post("/api/ask", json=_payload())
    assert first.status_code == 200
    first_payload = _json_object(first.get_json())
    session_id = str(first_payload["session_id"])
    conflict = _payload(revision=2)
    conflict["session_id"] = session_id
    conflict.update(mutation)

    response = client.post("/api/ask", json=conflict)

    assert response.status_code == expected_status
    response_payload = _json_object(response.get_json())
    assert response_payload["session_revision"] == 2


def test_legacy_request_without_request_id_keeps_engine_owned_path(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _db_path = _client(tmp_path, migrated_db)
    captured: list[comments_server.AskTurn] = []
    monkeypatch.setattr(comments_server, "build_portfolio_pack", _empty_pack)

    def _respond(
        turn: comments_server.AskTurn, *_a: object, **_k: object
    ) -> Iterator[dict[str, object]]:
        captured.append(turn)
        yield {"type": "final", "text": "legacy"}

    monkeypatch.setattr(comments_server, "respond_turn", _respond)
    response = client.post("/api/ask", json={"query": "legacy"})

    assert response.status_code == 200
    assert captured[0].persistence_mode == "engine"


def test_pending_durable_request_is_409_with_current_revision(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db_path = _client(tmp_path, migrated_db)
    session = ensure_session(None, scope="portfolio", db_path=db_path)
    context = SessionContextV1(
        company_ticker="NU",
        coverage_role_at_creation="portfolio",
        lifecycle_at_creation="active",
        category="research",
    )
    put_session_context(session.id, context, db_path=db_path)
    begin_exchange(
        session_id=session.id,
        request_id="pending-1",
        payload_sha256=hash_request_payload({"pending": True}),
        user_text="Pending",
        expected_revision=0,
        db_path=db_path,
    )
    monkeypatch.setattr(
        comments_server,
        "respond_turn",
        _unexpected_engine,
    )
    payload = _payload(request_id="request-2", revision=1)
    payload["session_id"] = session.id

    response = client.post("/api/ask", json=payload)

    assert response.status_code == 409
    response_payload = _json_object(response.get_json())
    assert response_payload["session_revision"] == 1


def test_session_context_uses_historical_thesis_hash_not_live_recomputation(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _db_path = _client(tmp_path, migrated_db)
    thesis = tmp_path / "repo" / "micro_thesis" / "holdings" / "NU.json"
    thesis.parent.mkdir(parents=True)
    thesis.write_bytes(b'{"version":"A"}')
    hash_a = hashlib.sha256(thesis.read_bytes()).hexdigest()
    monkeypatch.setattr(comments_server, "build_portfolio_pack", _empty_pack)
    monkeypatch.setattr(comments_server, "respond_turn", _answer_events)
    monkeypatch.setattr(comments_server, "ask_retrieval_mode", _legacy_mode)

    first = _json_object(client.post("/api/ask", json=_payload()).get_json())
    first_sid = str(first["session_id"])
    stored_payload = _json_object(client.get(f"/api/ask/sessions/{first_sid}").get_json())
    stored_a = _json_object(stored_payload["session_context"])
    assert stored_a["thesis_ref"] == "micro_thesis/holdings/NU.json"
    assert stored_a["thesis_version"] == hash_a

    thesis.write_bytes(b'{"version":"B"}')
    hash_b = hashlib.sha256(thesis.read_bytes()).hexdigest()
    second = _payload(request_id="request-2", revision=2)
    second["session_id"] = first_sid
    second["query"] = "Second turn"
    second["session_context"] = stored_a
    assert client.post("/api/ask", json=second).status_code == 200
    updated_payload = _json_object(client.get(f"/api/ask/sessions/{first_sid}").get_json())
    updated_context = _json_object(updated_payload["session_context"])
    assert updated_context["thesis_version"] == hash_a

    new_thread = _payload(request_id="request-3", revision=0)
    new_thread["query"] = "New thread"
    new_result = _json_object(client.post("/api/ask", json=new_thread).get_json())
    new_detail = _json_object(
        client.get(f"/api/ask/sessions/{new_result['session_id']}").get_json()
    )
    new_context = _json_object(new_detail["session_context"])
    assert new_context["thesis_version"] == hash_b


def test_legacy_session_remains_listable_with_null_validated_context(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    client, db_path = _client(tmp_path, migrated_db)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO ask_sessions(id,scope,title,created_at,updated_at) "
            "VALUES ('legacy','portfolio','Legacy','2026-08-09','2026-08-09')"
        )
        connection.commit()

    sessions_payload = _json_object(client.get("/api/ask/sessions").get_json())
    sessions = _JSON_ARRAY.validate_python(sessions_payload["sessions"])
    legacy = next(
        item for raw_item in sessions if (item := _json_object(raw_item))["id"] == "legacy"
    )
    assert legacy["session_context"] is None
    assert legacy["session_revision"] == 0
