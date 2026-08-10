"""P4.5 journal UI: the /api/notes REST surface, the Research → Journal
panel, and the report-side "save to journal" capture markup.

The notes substrate (analyst_notes, alembic 0074) already had full CRUD in
src/user_state/notes.py; these tests pin the in-app lifecycle on top of it.
The DB is built via alembic (stamp a pre-0074 head, upgrade to head),
mirroring test_comments_server_alerting_routes.py.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from pipeline.journal_panel import render_journal_list, render_journal_panel
from user_state.notes import create_note, get_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> FlaskClient:
    assert db_path.exists()
    return comments_server.create_app(tmp_path).test_client()


# ----------------------------------------------------------------------------
# /api/notes REST
# ----------------------------------------------------------------------------


def test_create_then_list_notes(client: FlaskClient) -> None:
    created = client.post(
        "/api/notes",
        json={
            "ticker": "nu",
            "kind": "watch",
            "body": "Watch NIM trajectory next print.",
            "anchor_type": "kpi_ledger_row",
            "anchor_key": "Risk-adjusted NIM",
            "context": {"report_date": "2026-06-10", "tab": "thesis"},
        },
    )
    assert created.status_code == 201
    note = created.get_json()["note"]
    assert note["ticker"] == "NU"
    assert note["kind"] == "watch"
    assert note["status"] == "open"
    assert note["source"] == "manual"
    assert note["anchor_type"] == "kpi_ledger_row"

    listed = client.get("/api/notes?ticker=NU")
    assert listed.status_code == 200
    notes = listed.get_json()["notes"]
    assert len(notes) == 1
    assert notes[0]["body"] == "Watch NIM trajectory next print."


def test_create_note_validates(client: FlaskClient) -> None:
    assert client.post("/api/notes", json={"kind": "watch", "body": "  "}).status_code == 400
    assert client.post("/api/notes", json={"kind": "nope", "body": "x"}).status_code == 400
    assert client.get("/api/notes?status=bogus").status_code == 400


def test_lifecycle_resolve_reclassify_supersede_archive(client: FlaskClient, db_path: Path) -> None:
    a = create_note(ticker="NU", kind="question", body="What drove the NIM dip?", db_path=db_path)
    b = create_note(ticker="NU", kind="observation", body="Funding mix shifted.", db_path=db_path)
    c = create_note(ticker="NU", kind="observation", body="Old framing.", db_path=db_path)
    d = create_note(ticker="NU", kind="watch", body="Stale item.", db_path=db_path)

    resolved = client.post(
        f"/api/notes/{a.id}/resolve", json={"resolution_note": "Mix shift, per the call."}
    )
    assert resolved.status_code == 200
    assert resolved.get_json()["note"]["status"] == "resolved"
    assert resolved.get_json()["note"]["resolution_note"] == "Mix shift, per the call."

    reclassified = client.post(f"/api/notes/{b.id}/reclassify", json={"kind": "assumption"})
    assert reclassified.status_code == 200
    assert reclassified.get_json()["note"]["kind"] == "assumption"

    superseded = client.post(
        f"/api/notes/{c.id}/supersede", json={"body": "New framing: secured mix is the driver."}
    )
    assert superseded.status_code == 200
    replacement = superseded.get_json()["note"]
    assert replacement["supersedes_id"] == c.id
    assert replacement["status"] == "open"
    old = get_note(c.id, db_path=db_path)
    assert old is not None and old.status == "superseded"

    archived = client.post(f"/api/notes/{d.id}/archive", json={})
    assert archived.status_code == 200
    assert archived.get_json()["note"]["status"] == "archived"


def test_lifecycle_error_paths(client: FlaskClient, db_path: Path) -> None:
    n = create_note(ticker="NU", kind="watch", body="x", db_path=db_path)
    assert client.post(f"/api/notes/{n.id}/reclassify", json={"kind": "bogus"}).status_code == 400
    assert client.post(f"/api/notes/{n.id}/supersede", json={}).status_code == 400
    assert client.post(f"/api/notes/{n.id}/frobnicate", json={}).status_code == 404
    assert client.post("/api/notes/99999/resolve", json={}).status_code == 404
    assert client.post("/api/notes/99999/supersede", json={"body": "y"}).status_code == 404


def test_supersede_rejects_stale_note_revision(client: FlaskClient, db_path: Path) -> None:
    original = create_note(ticker="NU", kind="question", body="Old question", db_path=db_path)
    stale = client.post(
        f"/api/notes/{original.id}/supersede",
        json={"body": "Stale edit", "expected_revision": "2000-01-01T00:00:00"},
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "revision_conflict"

    current_revision = client.get("/api/notes?ticker=NU").get_json()["notes"][0]["updated_at"]
    accepted = client.post(
        f"/api/notes/{original.id}/supersede",
        json={"body": "Current edit", "expected_revision": current_revision},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["note"]["body"] == "Current edit"


# ----------------------------------------------------------------------------
# Journal panel fragment
# ----------------------------------------------------------------------------


def test_panel_renders_filters_capture_and_actions(db_path: Path) -> None:
    create_note(ticker="NU", kind="watch", body="Watch NIM next print.", db_path=db_path)
    html = render_journal_panel(db_path)
    assert "Journal" in html
    assert 'id="jr-new"' in html  # capture form
    assert 'id="jr-filters"' in html
    assert "Watch NIM next print." in html
    # Lifecycle actions on the open note
    assert 'data-act="resolve"' in html
    assert 'data-act="supersede"' in html
    assert 'data-act="archive"' in html
    assert 'data-act="reclassify"' in html
    assert "/api/notes" in html  # action wiring


def test_panel_list_fragment_filters(db_path: Path) -> None:
    create_note(ticker="NU", kind="watch", body="NU watch item.", db_path=db_path)
    create_note(ticker="MELI", kind="question", body="MELI question.", db_path=db_path)
    only_nu = render_journal_list(db_path, ticker="NU")
    assert "NU watch item." in only_nu
    assert "MELI question." not in only_nu
    only_questions = render_journal_list(db_path, kind="question", status="all")
    assert "MELI question." in only_questions
    assert "NU watch item." not in only_questions
    # Bogus filter values are dropped, not 500s.
    assert "NU watch item." in render_journal_list(db_path, kind="bogus", status="weird")


def test_journal_batches_link_targets_for_visible_tickers(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.journal_panel as journal_panel

    create_note(ticker="NU", kind="watch", body="NU watch item.", db_path=db_path)
    create_note(ticker="MELI", kind="question", body="MELI question.", db_path=db_path)
    calls: list[set[str]] = []

    def _batch(*, tickers: set[str], **_kwargs: object) -> dict[str, list[object]]:
        calls.append(tickers)
        return {}

    monkeypatch.setattr(
        journal_panel,
        "linkable_targets_for_tickers",
        _batch,
        raising=False,
    )
    render_journal_list(db_path)
    assert calls == [{"MELI", "NU"}]


def test_journal_silos_separate_owner_from_advisor(db_path: Path) -> None:
    """S11: machine-authored advisor memos demote into a collapsed synthesis
    silo BELOW the owner's own journal — split by identity, not source table."""
    create_note(ticker="NU", kind="watch", body="OWNER watch item.", db_path=db_path)
    create_note(
        ticker=None,
        kind="observation",
        body="ADVISOR next-dollar synthesis.",
        source="advisor",
        source_ref="advisor_memo:7",
        context={"memo_id": 7, "kind": "next_dollar"},
        db_path=db_path,
    )
    html = render_journal_list(db_path)
    assert "jr-synthesis" in html
    assert "OWNER watch item." in html
    assert "ADVISOR next-dollar synthesis." in html
    # Owner note renders before the silo; the advisor memo inside it.
    assert html.index("OWNER watch item.") < html.index("jr-synthesis")
    assert html.index("ADVISOR next-dollar synthesis.") > html.index("jr-synthesis")
    # The advisor card is demoted + read-oriented: Open-in-Memos + archive only,
    # never the owner-thinking lifecycle (supersede / reclassify / resolve).
    assert "jr-synth-note" in html
    assert "Open in Memos" in html
    synth = html[html.index("jr-synthesis") :]
    assert 'data-act="archive"' in synth
    assert 'data-act="supersede"' not in synth
    assert 'data-act="resolve"' not in synth
    assert 'data-act="reclassify"' not in synth


def test_journal_no_silo_when_only_owner(db_path: Path) -> None:
    create_note(ticker="NU", kind="watch", body="Just an owner note.", db_path=db_path)
    html = render_journal_list(db_path)
    assert "Just an owner note." in html
    assert "jr-synthesis" not in html  # silo only appears when there are memos


def test_journal_owner_empty_hint_when_only_advisor(db_path: Path) -> None:
    create_note(
        ticker="NU",
        kind="observation",
        body="Only advisor here.",
        source="advisor",
        source_ref="advisor_memo:1",
        context={"memo_id": 1, "kind": "swap_checks"},
        db_path=db_path,
    )
    html = render_journal_list(db_path)
    assert "No notes of your own match this filter." in html
    assert "jr-synthesis" in html
    assert "Only advisor here." in html


def test_panel_route_serves_fragment(client: FlaskClient, db_path: Path) -> None:
    create_note(ticker="NU", kind="watch", body="Route-served note.", db_path=db_path)
    full = client.get("/api/panel/journal")
    assert full.status_code == 200
    assert b"Route-served note." in full.data
    assert b"jr-filters" in full.data
    frag = client.get("/api/panel/journal?fragment=list&ticker=NU")
    assert frag.status_code == 200
    assert b"Route-served note." in frag.data
    assert b"jr-filters" not in frag.data  # list-only fragment


# ----------------------------------------------------------------------------
# Report-side capture (workspace comment sidebar)
# ----------------------------------------------------------------------------


def test_workspace_sidebar_carries_save_to_journal() -> None:
    from io import StringIO

    from report.renderers.workspace_comments import JS as COMMENTS_JS
    from report.renderers.workspace_html import (
        _comment_sidebar_shell,  # pyright: ignore[reportPrivateUsage]
    )

    out = StringIO()
    _comment_sidebar_shell(out)
    html = out.getvalue()
    assert 'id="cmt-save-note"' in html
    assert 'name="note_kind"' in html
    assert '<option value="watch">' in html
    assert "/api/notes" in COMMENTS_JS
    assert "Saved to journal" in COMMENTS_JS


def test_journal_js_has_no_window_prompt_and_uses_in_card_editor() -> None:
    """Red-team wave B (B9): a native window.prompt is a blocking, unstyled OS
    modal that hides the card being edited. The Journal's resolve / rec-resolve
    / supersede actions must be the in-card textarea editor (the Ledger's
    Rewrite/Steer idiom); the POST contracts are unchanged. Comments may
    mention the old idiom; live calls may not."""
    import inspect

    import pipeline.journal_panel as jp

    src = inspect.getsource(jp)
    assert "window.prompt(" not in src
    # The in-card editor: textarea + kit Save/Cancel appended to the card.
    assert "beginEdit" in src
    assert "jr-edit-ta" in src
    # POST contracts unchanged: resolve / supersede still hit /api/notes/<id>/<act>.
    assert "'/api/notes/' + id + '/' + act" in src
