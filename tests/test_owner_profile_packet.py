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

import json
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
    # Ledger UX overhaul (requirement B): the route itself carries the
    # consequence receipt — the UI never has to guess what registering.
    assert body["receipt"] == "Affirmed — the coach may now cite this when reviewing your trades"

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
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Dropped — never used, won't be re-proposed"

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


# ---------------------------------------------------------------------------
# tenet-2 Phase 3 — expiring AFFIRMED facts as packet items
# [Still true / Update / Drop], distinct from the Phase 1 proposed-fact card.
# ---------------------------------------------------------------------------


def _seed_expired_fact(db: Path, *, key: str = "dry_powder_policy", horizon_days: int = 90) -> int:
    """An AFFIRMED fact whose review horizon has already elapsed."""
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="appetite",
            key=key,
            value={"months": 3.0},
            narrative="Dry-powder policy: keep 3 months uninvested.",
            provenance="owner",
            status="affirmed",
            review_horizon_days=horizon_days,
        )
        conn.execute(
            "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", fid),
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def test_expiring_fact_appears_in_packet_with_still_true_update_drop(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    _seed_expired_fact(db)
    html = render_ledger_panel(db)
    assert 'id="ledger-packet"' in html
    assert "1 needs you" in html
    assert "Dry-powder policy: keep 3 months uninvested." in html
    assert 'data-profile-action="reaffirm"' in html
    assert 'data-profile-action="retire"' in html
    assert "data-profile-update-toggle" in html
    assert "Still true" in html and "Update" in html and "Drop" in html


def test_reaffirm_route_bumps_affirmed_at_and_clears_the_packet(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    fid = _seed_expired_fact(db)
    resp = client.post(f"/api/profile/fact/{fid}/reaffirm")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["status"] == "affirmed"
    assert body["receipt"] == "Confirmed — good for another review cycle"

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT SUM(count) FROM panel_activation_counts WHERE panel_id = ?",
            ("act:profile:reaffirm",),
        ).fetchone()
        affirmed_at = conn.execute(
            "SELECT affirmed_at FROM owner_profile_facts WHERE id = ?", (fid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(row[0] or 0) == 1
    assert affirmed_at != "2020-01-01T00:00:00"  # refreshed, no longer expired

    html = render_ledger_panel(db)
    assert "ledger-packet" not in html


def test_reaffirm_stale_id_is_404(ctx: tuple[FlaskClient, Path, Path]) -> None:
    """Re-tapping a fact that isn't (still) affirmed is a stale no-op, not a
    silent 200 — mirrors the affirm route's own stale-tap contract."""
    client, db, _root = ctx
    fid = _seed_fact(db)  # proposed, never affirmed
    resp = client.post(f"/api/profile/fact/{fid}/reaffirm")
    assert resp.status_code == 404


def test_retire_route_retires_and_bumps_activation(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    client, db, _root = ctx
    fid = _seed_expired_fact(db)
    resp = client.post(f"/api/profile/fact/{fid}/retire")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Dropped — the coach will stop citing this fact"

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        status = conn.execute(
            "SELECT status FROM owner_profile_facts WHERE id = ?", (fid,)
        ).fetchone()["status"]
        row = conn.execute(
            "SELECT SUM(count) FROM panel_activation_counts WHERE panel_id = ?",
            ("act:profile:retire",),
        ).fetchone()
    finally:
        conn.close()
    assert status == "rejected"
    assert int(row[0] or 0) == 1
    html = render_ledger_panel(db)
    assert "ledger-packet" not in html


def test_retire_route_cannot_retire_a_proposed_fact(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    """``retire`` is the AFFIRMED-only sibling of ``reject`` — a proposed fact
    must go through ``reject``, never ``retire``."""
    client, db, _root = ctx
    fid = _seed_fact(db)  # proposed
    resp = client.post(f"/api/profile/fact/{fid}/retire")
    assert resp.status_code == 404


def test_update_route_lands_a_new_proposed_fact_superseding_the_old(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    """Gated assertion holds even on the owner's own edit (§7.1): Update never
    auto-promotes — the rewritten text lands as a NEW ``proposed`` row, which
    resurfaces via the SAME Phase 1 proposed-facts packet source."""
    client, db, _root = ctx
    fid = _seed_expired_fact(db)
    resp = client.post(
        f"/api/profile/fact/{fid}/update", json={"narrative": "Dry-powder policy: 4 months now."}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["receipt"] == "Saved — your edit awaits your affirm next walk"
    new_id = body["new_fact_id"]
    assert new_id != fid

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        old_status = conn.execute(
            "SELECT status, is_latest FROM owner_profile_facts WHERE id = ?", (fid,)
        ).fetchone()
        new_row = conn.execute(
            "SELECT status, is_latest, narrative, value_json FROM owner_profile_facts WHERE id = ?",
            (new_id,),
        ).fetchone()
    finally:
        conn.close()
    assert old_status["status"] == "affirmed"  # untouched
    assert old_status["is_latest"] == 0  # superseded
    assert new_row["status"] == "proposed"
    assert new_row["is_latest"] == 1
    assert new_row["narrative"] == "Dry-powder policy: 4 months now."
    assert json.loads(new_row["value_json"]) == {"months": 3.0}  # value unchanged (narrative-only)

    # The expiring-fact item is gone (old row superseded); the NEW proposed
    # fact appears via the Phase 1 source instead, with its own Affirm/Reject.
    html = render_ledger_panel(db)
    assert 'data-profile-action="affirm"' in html
    assert "Dry-powder policy: 4 months now." in html


def test_update_route_requires_a_narrative(ctx: tuple[FlaskClient, Path, Path]) -> None:
    client, db, _root = ctx
    fid = _seed_expired_fact(db)
    resp = client.post(f"/api/profile/fact/{fid}/update", json={})
    assert resp.status_code == 400
    resp2 = client.post(f"/api/profile/fact/{fid}/update", json={"narrative": "   "})
    assert resp2.status_code == 400


def test_expiring_fact_not_shown_before_horizon_elapses(
    ctx: tuple[FlaskClient, Path, Path],
) -> None:
    _client, db, _root = ctx
    conn = sqlite3.connect(str(db))
    try:
        append_fact(
            conn,
            category="appetite",
            key="dry_powder_policy",
            value={"months": 3.0},
            narrative="Dry-powder policy: keep 3 months uninvested.",
            provenance="owner",
            status="affirmed",
            review_horizon_days=90,
        )  # affirmed_at defaults to now — nowhere near 90 days stale
        conn.commit()
    finally:
        conn.close()
    html = render_ledger_panel(db)
    assert "ledger-packet" not in html
