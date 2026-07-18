"""Weekly Telegram packet — the ``profile_fact_expiring`` item kind (tenet-2
Phase 3, §4 delivery seam 5 / §7 decision 6 "both"): expiring AFFIRMED
owner-profile facts ride the SAME assemble/sync/verdict machinery as every
other packet item, with [Still true / Rewrite / Drop / Defer] verbs.

Uses a REAL, fully-migrated DB (mirrors test_capacity_moments.py's fixture)
rather than test_weekly_packet.py's hand-rolled schema, because this item
kind needs the real ``owner_profile_facts`` table (0159) ALONGSIDE the real
``weekly_packet_runs``/``items`` tables (0149) and the pre-alembic
``decisions`` table the other item kinds read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db as dbmod  # noqa: E402
from owner_profile.store import append_fact  # noqa: E402
from pipeline import weekly_packet as wp  # noqa: E402


def _cfg(db_file: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "portfolio.db"
    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db_file))
    dbmod.init_db()
    cfg = _cfg(db_file)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    try:
        yield db_file
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


def _seed_expired_fact(db_path: Path, *, key: str = "dry_powder_policy") -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        fid = append_fact(
            conn,
            category="appetite",
            key=key,
            value={"months": 3.0},
            narrative="Dry-powder policy: keep 3 months uninvested.",
            provenance="owner",
            status="affirmed",
            review_horizon_days=90,
        )
        conn.execute(
            "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", fid),
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def test_assemble_packet_includes_expiring_profile_facts(db: Path) -> None:
    _seed_expired_fact(db)
    plans = wp.assemble_packet(db_path=db)
    matches = [p for p in plans if p.item_kind == "profile_fact_expiring"]
    assert len(matches) == 1
    assert "Dry-powder policy" in matches[0].title
    assert matches[0].ticker is None


def test_assemble_packet_excludes_facts_still_within_horizon(db: Path) -> None:
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
        )  # affirmed_at defaults to now
        conn.commit()
    finally:
        conn.close()
    plans = wp.assemble_packet(db_path=db)
    assert not [p for p in plans if p.item_kind == "profile_fact_expiring"]


def test_item_keyboard_uses_profile_fact_labels(db: Path) -> None:
    fid = _seed_expired_fact(db)
    run = wp.ensure_run(now=datetime(2026, 7, 17), db_path=db)
    plans = wp.assemble_packet(db_path=db)
    items = wp.sync_items(run.id, plans, db_path=db)
    (item,) = [it for it in items if it.item_kind == "profile_fact_expiring"]
    kb = wp.item_keyboard(item)
    rows = cast("list[list[dict[str, str]]]", kb["inline_keyboard"])
    labels = [btn["text"] for row in rows for btn in row]
    assert labels == ["Still true", "Rewrite", "Drop", "Defer"]
    _ = fid


def test_verdict_accept_reaffirms_the_fact(db: Path) -> None:
    fid = _seed_expired_fact(db)
    run = wp.ensure_run(now=datetime(2026, 7, 17), db_path=db)
    items = wp.sync_items(run.id, wp.assemble_packet(db_path=db), db_path=db)
    (item,) = [it for it in items if it.item_kind == "profile_fact_expiring"]

    verdict = wp.apply_verdict(item.id, "accept", chat_id=None, db_path=db)
    assert verdict == "accept"

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, affirmed_at FROM owner_profile_facts WHERE id = ?", (fid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "affirmed"
    assert row["affirmed_at"] != "2020-01-01T00:00:00"  # refreshed

    # Re-affirmed -> no longer expiring -> gone from a fresh assemble.
    assert not [p for p in wp.assemble_packet(db_path=db) if p.item_kind == "profile_fact_expiring"]


def test_verdict_drop_retires_the_fact(db: Path) -> None:
    fid = _seed_expired_fact(db)
    run = wp.ensure_run(now=datetime(2026, 7, 17), db_path=db)
    items = wp.sync_items(run.id, wp.assemble_packet(db_path=db), db_path=db)
    (item,) = [it for it in items if it.item_kind == "profile_fact_expiring"]

    verdict = wp.apply_verdict(item.id, "drop", chat_id=None, db_path=db)
    assert verdict == "drop"

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM owner_profile_facts WHERE id = ?", (fid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "rejected"


def test_verdict_rewrite_stashes_reply_then_supersedes_on_apply(db: Path) -> None:
    fid = _seed_expired_fact(db)
    run = wp.ensure_run(now=datetime(2026, 7, 17), db_path=db)
    items = wp.sync_items(run.id, wp.assemble_packet(db_path=db), db_path=db)
    (item,) = [it for it in items if it.item_kind == "profile_fact_expiring"]

    verdict = wp.apply_verdict(item.id, "rewrite", chat_id=555, db_path=db)
    assert verdict == "awaiting"

    from capture import pending_replies

    pending = pending_replies.peek(555, db_path=db)
    assert pending is not None and pending.kind == wp.REWRITE_KIND and pending.ref_id == item.id

    updated_item, wrote = wp.handle_awaited_reply(
        pending, "Dry-powder policy: keep 4 months now.", db_path=db
    )
    assert wrote is True
    assert updated_item is not None and updated_item.verdict == "rewrite"

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        old_row = conn.execute(
            "SELECT status, is_latest FROM owner_profile_facts WHERE id = ?", (fid,)
        ).fetchone()
        new_row = conn.execute(
            "SELECT status, is_latest, narrative, value_json FROM owner_profile_facts "
            "WHERE narrative = ?",
            ("Dry-powder policy: keep 4 months now.",),
        ).fetchone()
    finally:
        conn.close()
    assert old_row["status"] == "affirmed" and old_row["is_latest"] == 0
    assert new_row is not None
    assert new_row["status"] == "proposed" and new_row["is_latest"] == 1
    assert json.loads(new_row["value_json"]) == {"months": 3.0}


def test_verdict_defer_is_bookkeeping_only(db: Path) -> None:
    fid = _seed_expired_fact(db)
    run = wp.ensure_run(now=datetime(2026, 7, 17), db_path=db)
    items = wp.sync_items(run.id, wp.assemble_packet(db_path=db), db_path=db)
    (item,) = [it for it in items if it.item_kind == "profile_fact_expiring"]

    verdict = wp.apply_verdict(item.id, "defer", chat_id=None, db_path=db)
    assert verdict == "defer"

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM owner_profile_facts WHERE id = ?", (fid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "affirmed"  # untouched

    # Deferred items reappear next assemble (still-expiring, unresolved).
    matches = [p for p in wp.assemble_packet(db_path=db) if p.item_kind == "profile_fact_expiring"]
    assert len(matches) == 1
