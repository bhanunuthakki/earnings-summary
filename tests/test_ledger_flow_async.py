"""Ledger flow mechanics (Phase A of the review/ledger un-stick):

* the capture answer tap runs on a background thread — the POST returns
  immediately with ``answering`` + a ``ledger_answer_pending`` note flag, and
  ``/api/onmymind/<id>/answer`` is the poll the panel swaps the card on;
* ``?fragment=card`` serves one feed card (card-level refresh — an action
  never repaints the whole list);
* the composite Ledger console renders ONE chrome (single nav band, no
  per-builder tab titles, no second jump toolbar);
* ``execution/expire_stale_research.py`` sweeps parked wonderings so pending
  counts stay honest.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from onmymind.feed import load_feed_item  # noqa: E402
from user_state.notes import create_note, get_note, patch_note_context  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FlaskClient, Path]:
    # Pin every capture-side tap deterministic: no wondering LLM, no answer
    # engine (each test patches the answer core itself).
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    monkeypatch.delenv("LEDGER_ANSWER_SYNC", raising=False)
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db


def _capture_note(db: Path, body: str, *, ticker: str | None = None) -> int:
    row = create_note(
        body=body,
        kind="musing",
        ticker=ticker,
        source="capture",
        context={"channel": "tray"},
        db_path=db,
    )
    return row.id


# ---------------------------------------------------------------------------
# Async answer tap
# ---------------------------------------------------------------------------


def test_capture_question_answers_on_background_thread(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = ctx
    import onmymind.respond as respond_mod

    ran = threading.Event()
    seen: list[int] = []

    def _fake_answer(note_id: int, **kw: object) -> str:
        seen.append(note_id)
        ran.set()
        return "42 shares at $18.20"

    monkeypatch.setattr(respond_mod, "answer_capture", _fake_answer)
    resp = client.post("/api/capture/text", json={"text": "What is my cost basis?"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "landed"
    assert body["answering"] is True
    assert body["answer"] is None
    assert ran.wait(timeout=5), "background answer thread never ran"
    assert seen == [body["note_id"]]
    # The pending flag was stamped synchronously, before the POST returned.
    note = get_note(body["note_id"], db_path=db)
    assert note is not None and (note.context or {}).get("ledger_answer_pending") is True


def test_capture_non_question_pends_then_triage_clears(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3: ``will_answer`` no longer pre-screens on question-shaped text — the
    thread spawns for every answerable-kind capture and ``capture_triage``
    decides INSIDE ``answer_capture``. A 'plain' verdict clears the pending
    flag with no stored answer (the card shows 'Answering…' only for the
    triage round-trip, never forever)."""
    client, db = ctx
    import onmymind.respond as respond_mod
    from capture.triage import TriageVerdict

    monkeypatch.setattr(
        respond_mod,
        "classify_capture_triage",
        lambda body, **kw: TriageVerdict(route="plain"),
    )
    resp = client.post("/api/capture/text", json={"text": "NU keeps compounding quietly."})
    body = resp.get_json()
    assert body["answering"] is True  # thread spawned; triage decides, not a regex
    note_id = body["note_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        note = get_note(note_id, db_path=db)
        assert note is not None
        if not (note.context or {}).get("ledger_answer_pending"):
            break
        time.sleep(0.05)
    ctx_json = (note.context or {}) if note else {}
    assert ctx_json.get("ledger_answer_pending") is False
    assert "ledger_answer" not in ctx_json  # plain -> no stored answer, no failure crumb


def test_capture_sync_env_restores_inline_answer(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _db = ctx
    monkeypatch.setenv("LEDGER_ANSWER_SYNC", "1")
    import onmymind.respond as respond_mod

    monkeypatch.setattr(respond_mod, "answer_capture", lambda nid, **kw: "inline answer")
    resp = client.post("/api/capture/text", json={"text": "What is my cost basis?"})
    body = resp.get_json()
    assert body["answer"] == "inline answer"
    assert body["answering"] is False


def test_answer_capture_clears_pending_on_empty_answer(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real answer core must clear ledger_answer_pending even when the
    engine produces nothing — a card must never say 'Answering…' forever."""
    _client, db = ctx
    from onmymind import respond

    note_id = _capture_note(db, "What is my cost basis?")
    patch_note_context(note_id, {"ledger_answer_pending": True}, db_path=db)
    monkeypatch.setattr(
        respond, "respond_turn", lambda *a, **kw: iter([{"type": "error", "error": "nope"}])
    )
    out = respond.answer_capture(note_id, repo_root=db.parents[1], db_path=db)
    assert out is None
    note = get_note(note_id, db_path=db)
    assert note is not None and (note.context or {}).get("ledger_answer_pending") is False


def test_answer_poll_endpoint_states(ctx: tuple[FlaskClient, Path]) -> None:
    client, db = ctx
    note_id = _capture_note(db, "What is my cost basis?")
    # No answer, not pending.
    body = client.get(f"/api/onmymind/{note_id}/answer").get_json()
    assert body == {"answer": None, "pending": False}
    # Pending.
    patch_note_context(note_id, {"ledger_answer_pending": True}, db_path=db)
    body = client.get(f"/api/onmymind/{note_id}/answer").get_json()
    assert body == {"answer": None, "pending": True}
    # Answered.
    patch_note_context(
        note_id,
        {"ledger_answer": {"text": "42", "status": "ok"}, "ledger_answer_pending": False},
        db_path=db,
    )
    body = client.get(f"/api/onmymind/{note_id}/answer").get_json()
    assert body == {"answer": "42", "pending": False}
    assert client.get("/api/onmymind/999999/answer").status_code == 404


def test_pending_card_renders_answering_state(ctx: tuple[FlaskClient, Path]) -> None:
    client, db = ctx
    note_id = _capture_note(db, "What is my cost basis?")
    patch_note_context(note_id, {"ledger_answer_pending": True}, db_path=db)
    frag = client.get(f"/api/panel/musings?fragment=card&note={note_id}").data.decode()
    assert "om-answer-pending" in frag and "Answering" in frag


# ---------------------------------------------------------------------------
# fragment=card (card-level refresh)
# ---------------------------------------------------------------------------


def test_fragment_card_serves_one_feed_card(ctx: tuple[FlaskClient, Path]) -> None:
    client, db = ctx
    note_id = _capture_note(db, "NU keeps compounding quietly.")
    resp = client.get(f"/api/panel/musings?fragment=card&note={note_id}")
    assert resp.status_code == 200
    frag = resp.data.decode()
    assert f'id="om-note-{note_id}"' in frag
    assert "NU keeps compounding quietly." in frag
    # One card, not the list wrapper.
    assert 'id="onmymind-list"' not in frag


def test_fragment_card_missing_and_bad_ids(ctx: tuple[FlaskClient, Path]) -> None:
    client, _db = ctx
    assert client.get("/api/panel/musings?fragment=card&note=999999").status_code == 404
    assert client.get("/api/panel/musings?fragment=card&note=abc").status_code == 400


def test_load_feed_item_skips_archived(ctx: tuple[FlaskClient, Path]) -> None:
    _client, db = ctx
    from user_state.notes import archive_note

    note_id = _capture_note(db, "a musing to archive")
    assert load_feed_item(note_id, db_path=db) is not None
    archive_note(note_id, db_path=db)
    assert load_feed_item(note_id, db_path=db) is None
    assert load_feed_item(999999, db_path=db) is None


# ---------------------------------------------------------------------------
# Console chrome merge
# ---------------------------------------------------------------------------


def test_ledger_console_renders_one_chrome(ctx: tuple[FlaskClient, Path]) -> None:
    client, _db = ctx
    html = client.get("/api/panel/musings").data.decode()
    # One nav band: the feed's jump chips merged into the console toolbar…
    assert html.count('<div class="k-toolbar">') == 1
    assert 'data-ledger-jump="ledger-jump-capture"' in html
    assert 'data-console-jump="csec-triage"' in html
    assert 'data-console-jump="csec-journal"' in html
    # …and no second chip row or per-builder tab titles below it. (The CSS
    # class definition remains in the style block; the DIV is what's gone.
    # The feed's 'Ledger' chip is excluded from the band per #876's merged-band
    # design; embedded it also sheds its own <h2>Ledger</h2> echo, since the
    # band is already titled 'Ledger' and its Capture chip names the section.)
    assert '<div class="ledger-jump-toolbar">' not in html
    assert "<h2>Ledger</h2>" not in html
    assert "<h2>Journal</h2>" not in html
    # Triage/Journal collapse to section-level h3 headings.
    assert 'class="tri-h"' in html
    assert 'class="jr-h"' in html


def test_standalone_builders_keep_their_chrome(ctx: tuple[FlaskClient, Path]) -> None:
    """The composite is the only embedded caller — the builders' own routes
    (still live for fragments/direct use) keep their tab-level chrome."""
    _client, db = ctx
    from pipeline.journal_panel import render_journal_panel
    from pipeline.ledger_panel import render_ledger_panel
    from pipeline.triage_panel import render_triage_panel

    ledger = render_ledger_panel(db)
    assert "ledger-jump-toolbar" in ledger
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


def test_expire_stale_research_dry_run_and_apply(ctx: tuple[FlaskClient, Path]) -> None:
    _client, db = ctx
    import sqlite3

    from expire_stale_research import expire_stale_tasks

    from research.proposals import create_task, get_task

    old_id = create_task(note_id=None, claim="stale wondering", ticker="NU", db_path=db)
    fresh_id = create_task(note_id=None, claim="fresh wondering", ticker="NU", db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE research_tasks SET created_at = '2026-01-01T00:00:00' WHERE id = ?", (old_id,)
    )
    conn.commit()
    conn.close()

    # Dry run: reports the stale one, changes nothing.
    ids = expire_stale_tasks(days=10, apply=False, db_path=db)
    assert ids == [old_id]
    task = get_task(old_id, db_path=db)
    assert task is not None and task.status == "proposed"

    # Apply: the stale one expires, the fresh one survives.
    ids = expire_stale_tasks(days=10, apply=True, db_path=db)
    assert ids == [old_id]
    task = get_task(old_id, db_path=db)
    assert task is not None and task.status == "expired"
    fresh = get_task(fresh_id, db_path=db)
    assert fresh is not None and fresh.status == "proposed"
    # Idempotent: an expired task never matches again.
    assert expire_stale_tasks(days=10, apply=True, db_path=db) == []


# ---------------------------------------------------------------------------
# Inline chat streams
# ---------------------------------------------------------------------------


def test_om_chat_uses_streaming_endpoint() -> None:
    from pipeline.ledger_panel import _OM_CHAT_JS  # pyright: ignore[reportPrivateUsage]

    assert "/api/ask/stream" in _OM_CHAT_JS
    assert "'/api/ask'" not in _OM_CHAT_JS  # the buffered sibling is gone


def test_onmymind_js_dropped_popup_branch() -> None:
    from pipeline.ledger_panel import _ONMYMIND_JS  # pyright: ignore[reportPrivateUsage]

    assert "window.open" not in _ONMYMIND_JS


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_background_answer_lands_on_the_note(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the route with the REAL answer core (engine stubbed):
    the answer lands on the note and clears pending, exactly what the panel's
    poll observes."""
    client, db = ctx
    from onmymind import respond

    monkeypatch.setattr(
        respond,
        "respond_turn",
        lambda *a, **kw: iter([{"type": "final", "text": "your basis is $18.20"}]),
    )
    resp = client.post("/api/capture/text", json={"text": "What is my cost basis?"})
    body = resp.get_json()
    assert body["answering"] is True
    note_id = body["note_id"]

    def _answered() -> bool:
        note = get_note(note_id, db_path=db)
        ctx_ = (note.context or {}) if note else {}
        return isinstance(ctx_.get("ledger_answer"), dict) and not ctx_.get("ledger_answer_pending")

    assert _wait_for(_answered), "background answer never landed on the note"
    poll = client.get(f"/api/onmymind/{note_id}/answer").get_json()
    assert poll == {"answer": "your basis is $18.20", "pending": False}
