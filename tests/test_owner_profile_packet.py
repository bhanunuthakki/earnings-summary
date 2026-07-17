"""owner_profile_facts ratification surface (tenet-2 Phase 1) — the packet-walk
item source + card, and the affirm/reject routes.

- ``_packet_items`` surfaces every ``proposed`` fact as its own card with
  Affirm ("Still true") / Reject ("Drop") buttons.
- ``/api/profile/fact/<id>/affirm`` and ``/reject`` call the store directly
  and bump ``act:profile:<verb>``.
- Gated assertion: a fact never appears once it is no longer ``proposed``
  (affirmed or rejected), and reads degrade to an empty packet on a
  pre-0159 DB (no owner_profile_facts table yet).
"""

from __future__ import annotations

import sqlite3
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

from owner_profile.store import append_fact, list_facts  # noqa: E402
from pipeline.ledger_panel import render_ledger_panel  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"  # matches tests/test_ledger_packet.py's baseline


def _build_db(db_path: Path, *, target: str = "head") -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, target)


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[FlaskClient, Path, Path]:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db, tmp_path


def _seed_fact(db: Path, *, key: str = "home_city") -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="capacity",
            key=key,
            value={"city": "San Francisco"},
            narrative="Home city: San Francisco.",
            provenance="wealthplan_import",
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def test_packet_absent_with_no_proposed_facts(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    html = render_ledger_panel(db)
    assert "ledger-packet" not in html


def test_proposed_fact_appears_in_packet(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    _seed_fact(db)
    html = render_ledger_panel(db)
    assert 'id="ledger-packet"' in html
    assert "1 needs you" in html
    assert "Home city: San Francisco." in html
    assert 'data-profile-action="affirm"' in html
    assert 'data-profile-action="reject"' in html
    assert "Still true" in html and "Drop" in html


def test_affirm_route_promotes_and_bumps_activation(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    fid = _seed_fact(db)
    resp = client.post(f"/api/profile/fact/{fid}/affirm")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["status"] == "affirmed"

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT SUM(count) FROM panel_activation_counts WHERE panel_id = ?",
            ("act:profile:affirm",),
        ).fetchone()
    finally:
        conn.close()
    assert int(row[0] or 0) == 1

    # Affirmed facts must not still be waiting on the owner.
    html = render_ledger_panel(db)
    assert "ledger-packet" not in html


def test_reject_route_retires_and_bumps_activation(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    fid = _seed_fact(db)
    resp = client.post(f"/api/profile/fact/{fid}/reject")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT SUM(count) FROM panel_activation_counts WHERE panel_id = ?",
            ("act:profile:reject",),
        ).fetchone()
    finally:
        conn.close()
    assert int(row[0] or 0) == 1
    html = render_ledger_panel(db)
    assert "ledger-packet" not in html


def test_affirm_stale_id_is_404(ctx: tuple[FlaskClient, Path, Path]) -> None:
    client, db, _root = ctx
    fid = _seed_fact(db)
    client.post(f"/api/profile/fact/{fid}/affirm")
    # Re-tapping an already-affirmed fact is a stale no-op, not a silent 200.
    resp = client.post(f"/api/profile/fact/{fid}/affirm")
    assert resp.status_code == 404


def test_packet_degrades_on_pre_0159_db(tmp_path: Path) -> None:
    """A DB migrated only to the prior head (no owner_profile_facts table)
    must not break the packet — the source degrades to zero items, same as
    every other _packet_items source."""
    db = tmp_path / "old.db"
    _build_db(db, target="0152_v_thesis_status_stub_substring")
    html = render_ledger_panel(db)
    assert "ledger-packet" not in html


def test_list_facts_excludes_affirmed_and_rejected(ctx: tuple[FlaskClient, Path, Path]) -> None:
    _client, db, _root = ctx
    fid = _seed_fact(db, key="a")
    _seed_fact(db, key="b")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        from owner_profile.store import affirm_fact

        affirm_fact(conn, fid)
        conn.commit()
        proposed = list_facts(conn, status="proposed")
    finally:
        conn.close()
    assert {f.key for f in proposed} == {"b"}
