"""Manual process Tenet controls and refusal of retired distillation requests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from flask.testing import FlaskClient

from pipeline.worldview_panel import render_worldview_body, render_worldview_section
from synthesis.tenets import ensure_tenet_for_note, record_tenet

_PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_file(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "data" / "portfolio.db", stamp=_PRIOR_HEAD)


@pytest.fixture
def client(tmp_path: Path, db_file: Path) -> FlaskClient:
    return comments_server.create_app(tmp_path).test_client()


# ---------------------------------------------------------------------------
# Section renderer (flag-gated)
# ---------------------------------------------------------------------------


def test_section_empty_when_flag_off(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_WORLDVIEW", raising=False)
    assert render_worldview_section(db_file) == ""


def test_section_renders_when_flag_on(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    html = render_worldview_section(db_file)
    assert "Worldview" in html
    assert "wv-add-btn" in html  # the add-a-Tenet box
    assert "wv-distill-btn" not in html


def test_body_shows_current_and_proposed_with_tension(db_file: Path) -> None:
    live = record_tenet(
        body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_file
    )
    record_tenet(
        body_md="Trim winners on a double.",
        scope_key="exit-discipline",
        status="proposed",
        source_note_ids=[1],
        tension_with=live.id,
        db_path=db_file,
    )
    html = render_worldview_body(db_file)
    assert "Let winning theses run." in html  # current
    assert "Trim winners on a double." in html  # proposed
    assert 'data-tenet-action="approve"' in html
    assert "tension" in html  # the overlap is surfaced


def test_body_empty_state(db_file: Path) -> None:
    assert "No Tenets yet" in render_worldview_body(db_file)


def test_note_staged_tenet_surfaces_in_panel(db_file: Path) -> None:
    # A Tenet staged straight from an On My Mind musing (the 'To Worldview' rung)
    # lands ``proposed`` and shows in the approval queue through the existing path.
    ensure_tenet_for_note(7, body_md="Let a working thesis run.", db_path=db_file)
    html = render_worldview_body(db_file)
    assert "Let a working thesis run." in html
    assert 'data-tenet-action="approve"' in html


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_create_tenet_route(client: FlaskClient) -> None:
    r = client.post("/api/tenets", json={"body_md": "I sell my winners too early."})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    body = client.get("/api/panel/musings?fragment=worldview").data.decode()
    assert "sell my winners too early" in body


def test_create_tenet_requires_body(client: FlaskClient) -> None:
    assert client.post("/api/tenets", json={"body_md": "  "}).status_code == 400


def test_approve_route(client: FlaskClient, db_file: Path) -> None:
    prop = record_tenet(
        body_md="Trim winners on a double.",
        scope_key="exit-discipline",
        status="proposed",
        source_note_ids=[1],
        db_path=db_file,
    )
    r = client.post(f"/api/tenets/{prop.id}/approve")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "current"
    # Ledger UX overhaul (requirement B): the route itself carries the
    # consequence receipt.
    assert body["receipt"] == "Adopted — now a standing Tenet in your decision prompts"


def test_reject_route(client: FlaskClient, db_file: Path) -> None:
    prop = record_tenet(
        body_md="Chase momentum.", status="proposed", source_note_ids=[1], db_path=db_file
    )
    r = client.post(f"/api/tenets/{prop.id}/reject")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_unknown_action_route(client: FlaskClient, db_file: Path) -> None:
    t = record_tenet(body_md="Circle of competence.", db_path=db_file)
    assert client.post(f"/api/tenets/{t.id}/bogus").status_code == 400


def test_revert_route_restores_predecessor(client: FlaskClient, db_file: Path) -> None:
    from synthesis.tenets import supersede_tenet

    old = record_tenet(body_md="Let winners run.", scope_key="exit-discipline", db_path=db_file)
    new = supersede_tenet(old.id, body_md="Trim on a double.", db_path=db_file)
    assert new is not None
    r = client.post(f"/api/tenets/{new.id}/revert")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "current"
    assert body["receipt"] == "Reverted — restores your prior belief"


def test_revert_route_brand_new_retires(client: FlaskClient, db_file: Path) -> None:
    t = record_tenet(body_md="Circle of competence.", db_path=db_file)
    r = client.post(f"/api/tenets/{t.id}/revert")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "superseded"
    assert body["receipt"] == "Reverted — retired, no longer live"


def test_revert_route_missing_is_404(client: FlaskClient) -> None:
    assert client.post("/api/tenets/999999/revert").status_code == 404


def test_retired_distill_route_is_absent(client: FlaskClient) -> None:
    assert client.post("/api/tenets/distill").status_code == 404
