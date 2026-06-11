"""P4.5 journal UI: the /api/notes REST surface, the Research → Journal
panel, and the report-side "save to journal" capture markup.

The notes substrate (analyst_notes, alembic 0074) already had full CRUD in
src/user_state/notes.py; these tests pin the in-app lifecycle on top of it.
The DB is built via alembic (stamp a pre-0074 head, upgrade to head),
mirroring test_comments_server_alerting_routes.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command
from pipeline.command_center_shell import render_shell
from pipeline.journal_panel import render_journal_list, render_journal_panel
from user_state.notes import create_note, get_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return db


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


def test_shell_carries_journal_subtab() -> None:
    html = render_shell(overview_html="<p>x</p>")
    assert 'data-panel="journal"' in html or "journal" in html
    assert "Journal" in html


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
