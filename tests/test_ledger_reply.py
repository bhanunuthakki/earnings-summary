"""Phase B — the universal reply box + the Triage second pass.

* ``onmymind.reply.classify_reply``: closed enum, fail-open to 'question'.
* ``POST /api/onmymind/<id>/reply``: action intents flow through the ONE
  ``act_on_feed_item`` core; 'question' hands back chat mode; 'note' appends
  to the card's owner-reply thread.
* ``user_state.triage_suggest``: fail-closed to park; the sweep auto-routes
  high-confidence verdicts, stores one-tap suggestions for low, and is
  idempotent via the context stamps.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from onmymind.reply import ReplyVerdict, classify_reply  # noqa: E402
from user_state.notes import (  # noqa: E402
    TRIAGE_INTENT,
    create_note,
    get_note,
    list_triage_notes,
    patch_note_context,
)
from user_state.triage_suggest import suggest_route, sweep_unsuggested  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FlaskClient, Path]:
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.delenv("TRIAGE_SUGGEST", raising=False)
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


def _triage_note(db: Path, body: str, *, ticker: str | None = None) -> int:
    row = create_note(
        body=body,
        kind="question",
        ticker=ticker,
        source="comment",
        source_ref=f"T/{body[:8]}",
        context={"intent": TRIAGE_INTENT},
        db_path=db,
    )
    return row.id


# ---------------------------------------------------------------------------
# classify_reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_intent", "expected"),
    [
        ("research", "research"),
        ("SAVE", "save"),
        ("worldview", "worldview"),
        ("dismiss", "dismiss"),
        ("question", "question"),
        ("note", "note"),
        ("banana", "question"),  # unknown → fail open to conversation
        (None, "question"),
    ],
)
def test_classify_reply_enum_and_fail_open(model_intent: object, expected: str) -> None:
    verdict = classify_reply("card", "reply", call=lambda c, r: {"intent": model_intent})
    assert verdict.intent == expected


def test_classify_reply_action_property() -> None:
    assert ReplyVerdict(intent="research").is_action
    assert not ReplyVerdict(intent="question").is_action
    assert not ReplyVerdict(intent="note").is_action


# ---------------------------------------------------------------------------
# POST /api/onmymind/<id>/reply
# ---------------------------------------------------------------------------


def _patch_verdict(monkeypatch: pytest.MonkeyPatch, intent: str) -> None:
    import onmymind.reply as reply_mod

    monkeypatch.setattr(reply_mod, "classify_reply", lambda c, r, **kw: ReplyVerdict(intent=intent))


def test_reply_research_routes_through_action_core(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = ctx
    nid = _capture_note(db, "RBRK monetizes data volume")
    _patch_verdict(monkeypatch, "research")
    resp = client.post(f"/api/onmymind/{nid}/reply", json={"text": "dig into this"})
    body = resp.get_json()
    assert body["mode"] == "acted" and body["intent"] == "research"
    assert body["ladder_label"] == "in research"
    note = get_note(nid, db_path=db)
    assert note is not None and (note.context or {}).get("ladder") == "incorporated"


def test_reply_dismiss_removes_card(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = ctx
    nid = _capture_note(db, "old thought")
    _patch_verdict(monkeypatch, "dismiss")
    body = client.post(f"/api/onmymind/{nid}/reply", json={"text": "drop this"}).get_json()
    assert body["mode"] == "acted" and body["removed"] is True
    note = get_note(nid, db_path=db)
    assert note is not None and note.status == "archived"


def test_reply_question_hands_back_chat_mode(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = ctx
    nid = _capture_note(db, "NU keeps compounding")
    _patch_verdict(monkeypatch, "question")
    body = client.post(f"/api/onmymind/{nid}/reply", json={"text": "what changed?"}).get_json()
    assert body == {"ok": True, "mode": "chat", "intent": "question"}


def test_reply_note_appends_owner_thread(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = ctx
    nid = _capture_note(db, "NU keeps compounding")
    _patch_verdict(monkeypatch, "note")
    body = client.post(f"/api/onmymind/{nid}/reply", json={"text": "context: earnings 8/12"})
    assert body.get_json()["receipt"] == "Noted."
    note = get_note(nid, db_path=db)
    assert note is not None and (note.context or {}).get("owner_replies") == [
        "context: earnings 8/12"
    ]


def test_reply_classifier_failure_degrades_to_chat(
    ctx: tuple[FlaskClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = ctx
    nid = _capture_note(db, "NU keeps compounding")
    import onmymind.reply as reply_mod

    def _boom(*a: object, **kw: object) -> ReplyVerdict:
        raise RuntimeError("model down")

    monkeypatch.setattr(reply_mod, "classify_reply", _boom)
    body = client.post(f"/api/onmymind/{nid}/reply", json={"text": "hm"}).get_json()
    assert body["mode"] == "chat"


def test_reply_validation(ctx: tuple[FlaskClient, Path]) -> None:
    client, db = ctx
    nid = _capture_note(db, "x y z")
    assert client.post(f"/api/onmymind/{nid}/reply", json={}).status_code == 400
    assert client.post("/api/onmymind/999999/reply", json={"text": "hi"}).status_code == 404


# ---------------------------------------------------------------------------
# suggest_route + the sweep
# ---------------------------------------------------------------------------


def test_suggest_route_fail_closed_to_park() -> None:
    assert suggest_route("c", call=lambda t, c: {"intent": "park"}).intent is None
    assert suggest_route("c", call=lambda t, c: {"intent": "banana"}).intent is None
    s = suggest_route("c", call=lambda t, c: {"intent": "fix_data", "confidence": "weird"})
    assert s.intent == "fix_data" and s.confidence == "low"


def test_sweep_auto_routes_high_and_suggests_low(ctx: tuple[FlaskClient, Path]) -> None:
    _client, db = ctx
    hi = _triage_note(db, "revenue figure is wrong, should be 4.2B")
    lo = _triage_note(db, "hmm what about the peer set here")

    def _call(text: str, _ctx: str) -> dict[str, object]:
        if "revenue figure" in text:
            return {"intent": "fix_data", "confidence": "high", "reason": "clear data fix"}
        return {"intent": "curate_peers", "confidence": "low", "reason": "maybe peers"}

    stats = sweep_unsuggested(db_path=db, limit=10, call=_call)
    assert stats["auto_routed"] == 1 and stats["suggested"] == 1
    # High: routed out of the queue, receipt stamped.
    routed = get_note(hi, db_path=db)
    assert routed is not None and (routed.context or {}).get("intent") == "fix_data"
    assert isinstance((routed.context or {}).get("auto_routed"), dict)
    assert all(n.id != hi for n in list_triage_notes(db_path=db))
    # Low: still parked, suggestion stored.
    parked = get_note(lo, db_path=db)
    assert parked is not None and (parked.context or {}).get("route_suggestion") == {
        "intent": "curate_peers",
        "reason": "maybe peers",
    }
    # Idempotent: nothing left to classify.
    again = sweep_unsuggested(db_path=db, limit=10, call=_call)
    assert again == {"suggested": 0, "auto_routed": 0, "parked": 0, "skipped": 0, "errors": 0}


def test_sweep_per_item_degrade(ctx: tuple[FlaskClient, Path]) -> None:
    _client, db = ctx
    _triage_note(db, "first comment")
    ok_id = _triage_note(db, "second comment routes fine")
    calls = {"n": 0}

    def _flaky(text: str, _ctx: str) -> dict[str, object]:
        calls["n"] += 1
        if "first" in text:
            raise RuntimeError("transient")
        return {"intent": "ask_question", "confidence": "low", "reason": "q"}

    stats = sweep_unsuggested(db_path=db, limit=10, call=_flaky)
    assert stats["errors"] == 1 and stats["suggested"] == 1
    ok_note = get_note(ok_id, db_path=db)
    assert ok_note is not None and "route_suggestion" in (ok_note.context or {})


def test_sweep_park_stamps_and_skips_forever(ctx: tuple[FlaskClient, Path]) -> None:
    _client, db = ctx
    nid = _triage_note(db, "unroutable rambling")
    stats = sweep_unsuggested(db_path=db, limit=10, call=lambda t, c: {"intent": "park"})
    assert stats["parked"] == 1
    note = get_note(nid, db_path=db)
    assert note is not None and "route_parked" in (note.context or {})
    assert sweep_unsuggested(db_path=db, limit=10, call=lambda t, c: {"intent": "park"}) == {
        "suggested": 0,
        "auto_routed": 0,
        "parked": 0,
        "skipped": 0,
        "errors": 0,
    }


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


def test_triage_row_leads_with_suggestion_button(ctx: tuple[FlaskClient, Path]) -> None:
    _client, db = ctx
    nid = _triage_note(db, "the peer set feels off")
    patch_note_context(
        nid, {"route_suggestion": {"intent": "curate_peers", "reason": "peers"}}, db_path=db
    )
    from pipeline.triage_panel import render_triage_list

    html = render_triage_list(db)
    assert 'data-act="route-suggested"' in html
    assert 'data-intent="curate_peers"' in html
    assert "Route to Curate peers" in html


def test_feed_card_reply_row_markup(ctx: tuple[FlaskClient, Path]) -> None:
    client, db = ctx
    nid = _capture_note(db, "NU keeps compounding quietly.")
    frag = client.get(f"/api/panel/musings?fragment=card&note={nid}").data.decode()
    assert "om-reply-input" in frag and "data-om-reply" in frag
    assert 'data-om-verb="dismiss"' in frag
    assert "data-om-ask" not in frag


def test_reply_purposes_registered() -> None:
    from llm.cli import FAST_CLASSIFIER_MODEL, LLM_MODELS
    from llm.prompt_versions import prompt_version_for, registered_purposes

    assert LLM_MODELS["ledger_reply_intent"] == FAST_CLASSIFIER_MODEL
    assert LLM_MODELS["triage_route_suggest"] == FAST_CLASSIFIER_MODEL
    assert prompt_version_for("ledger_reply_intent") == "v1"
    assert prompt_version_for("triage_route_suggest") == "v1"
    assert {"ledger_reply_intent", "triage_route_suggest"} <= registered_purposes()
