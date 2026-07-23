"""The Worldview review surface (P2) — the section renderer + the /api/tenets routes.

The section is behind LEDGER_WORLDVIEW; the routes delegate to the (separately
tested) synthesis.tenets / tenet_distill cores. The distill route is exercised only
on the $0-triage path (nothing flagged ⇒ no LLM), so CI needs no CLI.
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

from pipeline.worldview_panel import render_worldview_body, render_worldview_section  # noqa: E402
from synthesis.tenets import ensure_tenet_for_note, record_tenet  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return db


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
    assert "wv-distill-btn" in html  # the distill tap


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
# "Adopted this week" strip (B4)
# ---------------------------------------------------------------------------


def test_adopted_strip_shows_recent_session_distill_tenet(db_file: Path) -> None:
    from synthesis.insights import record_insight

    record_insight(
        scope_key="tenet:exit-discipline",
        kind="tenet",
        body_md="Let a working thesis run — distilled from this week's session.",
        source_note_ids=[1],
        watermark_id=1,
        provenance="session_distill",
        db_path=db_file,
    )
    html = render_worldview_body(db_file)
    assert "Adopted this week" in html
    assert "Let a working thesis run" in html
    assert 'data-tenet-action="revert"' in html
    assert "from session" in html


def test_adopted_strip_excludes_old_adoption(db_file: Path) -> None:
    import sqlite3

    from synthesis.insights import record_insight

    # kind='stance' (not 'tenet') so an un-excluded row would show ONLY in the
    # strip, not also in the tenet-only "Your Worldview" list below it — an
    # unambiguous probe of the 7-day cutoff.
    insight_id = record_insight(
        scope_key="NU",
        kind="stance",
        body_md="An old auto-adopted stance.",
        source_note_ids=[1],
        watermark_id=1,
        provenance="session_distill",
        db_path=db_file,
    )
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "UPDATE insight_notes SET as_of = '2026-01-01T00:00:00' WHERE id = ?",
            (insight_id,),
        )
        conn.commit()
    finally:
        conn.close()
    html = render_worldview_body(db_file)
    assert "Adopted this week" not in html
    assert "An old auto-adopted stance." not in html


def test_adopted_strip_excludes_owner_provenance(db_file: Path) -> None:
    record_tenet(body_md="I state my own beliefs.", provenance="owner", db_path=db_file)
    html = render_worldview_body(db_file)
    assert "Adopted this week" not in html


def test_adopted_strip_empty_renders_nothing(db_file: Path) -> None:
    html = render_worldview_body(db_file)
    assert "Adopted this week" not in html


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


def test_distill_route_zero_triage_no_llm(client: FlaskClient) -> None:
    # Nothing flagged ⇒ the deterministic $0 triage short-circuits before any LLM,
    # so the route is safe to exercise end-to-end in CI.
    r = client.post("/api/tenets/distill")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["candidates"] == 0 and j["proposed"] == 0
