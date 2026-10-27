"""Retained Notes logging, refresh surfaces and no-automatic-advice boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import comments_server
import pytest
from flask.testing import FlaskClient

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from user_state.notes import create_note, get_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ctx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> tuple[FlaskClient, Path]:
    # Pin every capture-side tap deterministic: no wondering LLM, no answer
    # engine (each test patches the answer core itself).
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    monkeypatch.delenv("LEDGER_ANSWER_SYNC", raising=False)
    db = tmp_path / "data" / "portfolio.db"
    migrated_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db


# ---------------------------------------------------------------------------
# Async answer tap
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# fragment=card (card-level refresh)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Console chrome merge
# ---------------------------------------------------------------------------


def test_notes_page_keeps_logging_without_the_retired_console(
    ctx: tuple[FlaskClient, Path],
) -> None:
    client, _db = ctx
    html = client.get("/api/panel/musings").data.decode()
    assert "<h2>Notes</h2>" in html
    assert 'id="ledger-cap-text"' in html
    assert "/api/capture/text" in html
    assert 'id="ledger-list"' in html
    assert "data-console-jump" not in html
    from bs4 import BeautifulSoup

    assert BeautifulSoup(html, "html.parser").select(".ledger-packet") == []
    assert 'id="ledger-queues"' not in html


def test_standalone_builders_keep_their_chrome(ctx: tuple[FlaskClient, Path]) -> None:
    """The composite is the only embedded caller — the builders' own routes
    (still live for fragments/direct use) keep their tab-level chrome."""
    _client, db = ctx
    from pipeline.journal_panel import render_journal_panel
    from pipeline.ledger_panel import render_ledger_panel
    from pipeline.triage_panel import render_triage_panel

    ledger = render_ledger_panel(db)
    assert "<h2>Notes</h2>" in ledger and 'id="ledger-cap-text"' in ledger
    triage = render_triage_panel(db)
    assert "k-toolbar" in triage
    journal = render_journal_panel(db)
    assert "<h2>Journal</h2>" in journal


def test_no_window_prompt_anywhere_in_review_surfaces(ctx: tuple[FlaskClient, Path]) -> None:
    """window.prompt was banned from the ledger in PR9 — the composite console
    (feed + triage + journal) must not reintroduce it anywhere."""
    client, _db = ctx
    html = client.get("/api/panel/musings").data.decode()
    # The call form — comments referencing the ban are fine.
    assert "window.prompt(" not in html


# ---------------------------------------------------------------------------
# Stale-queue expiry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inline chat streams
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["What is my cost basis?", "NU keeps compounding quietly."])
@pytest.mark.parametrize("sync_env", ["0", "1"])
def test_basic_capture_logs_without_automatic_advice(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch, text: str, sync_env: str
) -> None:
    client, db = ctx
    monkeypatch.setenv("LEDGER_ANSWER_SYNC", sync_env)
    answer = Mock(side_effect=AssertionError("Basic logging must not start advice"))
    monkeypatch.setattr("llm.cli.call_llm", answer)
    response = client.post("/api/capture/text", json={"text": text})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "landed"
    assert "answering" not in body and "answer" not in body
    answer.assert_not_called()
    note = get_note(body["note_id"], db_path=db)
    assert note is not None and note.body == text
    assert not (note.context or {}).get("ledger_answer_pending")


@pytest.mark.parametrize(
    "text", ["Buying NU ~$30k", "Selling NU", "high conviction, falsifier: NPL above 5%"]
)
def test_plain_note_cannot_create_or_annotate_a_decision(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    from research.decision_feed import persist_owner_decision

    client, database = ctx
    decision_id = persist_owner_decision(ticker="NU", direction="buy", db_path=database)
    with connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY) as connection:
        before = connection.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    provider = Mock(side_effect=AssertionError("Notes must not invoke a provider"))
    monkeypatch.setattr("llm.cli.call_llm", provider)
    monkeypatch.setattr("llm.cli.execute_prepared_call", provider)
    monkeypatch.setattr("llm.structured.call_llm_structured", provider)
    response = client.post("/api/capture/text", json={"text": text})
    assert response.status_code == 200
    receipt = response.get_json()
    assert receipt["status"] == "landed" and "annotated_decision_id" not in receipt
    note = get_note(receipt["note_id"], db_path=database)
    assert note is not None and note.body == text and note.decision_id is None
    provider.assert_not_called()
    with connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY) as connection:
        assert connection.execute("SELECT * FROM decisions ORDER BY id").fetchall() == before
        assert connection.execute("SELECT id FROM decisions").fetchone()[0] == decision_id


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/onmymind/{id}/reply"),
        ("GET", "/api/onmymind/{id}/answer"),
        ("POST", "/api/onmymind/{id}/save"),
        ("POST", "/api/onmymind/{id}/discuss"),
        ("POST", "/api/onmymind/{id}/incorporate"),
    ],
)
def test_retired_feed_actions_cannot_mutate_retained_owner_notes(
    ctx: tuple[FlaskClient, Path],
    method: str,
    path: str,
) -> None:
    client, db = ctx
    note = create_note(
        ticker=None,
        kind="musing",
        body="Retained owner note.",
        source="capture",
        db_path=db,
    )
    response = client.open(
        path.format(id=note.id), method=method, json={} if method == "POST" else None
    )
    assert response.status_code == 404
    assert get_note(note.id, db_path=db) == note


@pytest.mark.parametrize("query", ["fragment=onmymind", "fragment=card&note=1"])
def test_retired_feed_fragments_do_not_render_a_second_notes_interface(
    ctx: tuple[FlaskClient, Path],
    query: str,
) -> None:
    client, _db = ctx
    assert client.get("/api/panel/musings?" + query).status_code == 400
