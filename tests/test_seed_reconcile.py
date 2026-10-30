"""Seed-corpus reconciliation — verdicts, falsifier ratification, panel render.

The freshness pass from the owner's 2026-07-02 callout: every seed item gets a
one-tap verdict; '(inferred)' falsifiers must be ratified (or rewritten in the
owner's words) before the coach may quote them."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from synthesis.reconcile import (
    falsifier_action,
    list_unreconciled,
    reconcile_note,
)


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[[Path], Path]) -> Path:
    db = migrated_db(tmp_path / "ledger.db")
    with sqlite3.connect(db) as conn:
        # A seeded musing note + a current theme, as seed_themes would land them
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, source_ref, created_at, updated_at) "
            "VALUES ('musing','I sell winners too early','capture','seed:musing:1',"
            "'2026-07-01','2026-07-01')"
        )
    return db


@pytest.fixture
def seeded(db_path: Path) -> Path:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO analyst_notes(kind,body,source,source_ref,created_at,updated_at) "
            "VALUES ('musing','Synthetic rejected intent','manual','seed:intent:fixture',"
            "'2026-07-01','2026-07-01')"
        )
        conn.executemany(
            "INSERT INTO decisions(ticker,recommendation_kind,decided_by,scope,"
            "falsifier,made_at,created_at) VALUES (?,'buy','owner','ticker',?,"
            "'2026-07-01','2026-07-01')",
            [
                ("MU", "Memory pricing cycle rolls over. (inferred)"),
                ("NU", "15-90d NPL >5% for 2Q"),
            ],
        )
    return db_path


def _kinds(db: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in list_unreconciled(db):
        out[item.kind] = out.get(item.kind, 0) + 1
    return out


def test_note_verdicts_stamp_and_drop_off(seeded: Path) -> None:
    items = [i for i in list_unreconciled(seeded) if i.kind == "note"]
    musing = next(i for i in items if (i.source_ref or "").startswith("seed:musing"))
    intent = next(i for i in items if (i.source_ref or "").startswith("seed:intent"))

    assert reconcile_note(musing.item_id, "live", db_path=seeded)
    assert reconcile_note(intent.item_id, "resolved-rejected", db_path=seeded)
    assert _kinds(seeded).get("note", 0) == 0

    conn = sqlite3.connect(str(seeded))
    try:
        status, ctx = conn.execute(
            "SELECT status, context_json FROM analyst_notes WHERE id=?", (musing.item_id,)
        ).fetchone()
        assert status == "open"  # live keeps the note open
        assert json.loads(ctx)["reconcile"] == "live"
        status2 = conn.execute(
            "SELECT status FROM analyst_notes WHERE id=?", (intent.item_id,)
        ).fetchone()[0]
        assert status2 == "resolved"
    finally:
        conn.close()


def test_falsifier_ratify_edit_drop(seeded: Path) -> None:
    fals = next(i for i in list_unreconciled(seeded) if i.kind == "falsifier")

    assert falsifier_action(fals.item_id, "ratify", db_path=seeded)
    conn = sqlite3.connect(str(seeded))
    try:
        value = conn.execute(
            "SELECT falsifier FROM decisions WHERE id=?", (fals.item_id,)
        ).fetchone()[0]
        assert value == "Memory pricing cycle rolls over."  # marker stripped
    finally:
        conn.close()
    assert _kinds(seeded).get("falsifier", 0) == 0  # ratified → off the list

    assert falsifier_action(fals.item_id, "edit", text="Cycle rolls over 2Q", db_path=seeded)
    assert falsifier_action(fals.item_id, "drop", db_path=seeded)
    with pytest.raises(ValueError):
        falsifier_action(fals.item_id, "edit", db_path=seeded)


def test_render_reconcile_list_smoke(seeded: Path) -> None:
    from pipeline.ledger_panel import render_reconcile_list

    html = render_reconcile_list(seeded)
    assert 'id="ledger-reconcile"' in html
    assert "data-rec-verdict" in html and "data-falsifier-action" in html
    for item in list(list_unreconciled(seeded)):
        if item.kind == "note":
            reconcile_note(item.item_id, "live", db_path=seeded)
        else:
            falsifier_action(item.item_id, "ratify", db_path=seeded)
    assert "Corpus reconciled" in render_reconcile_list(seeded)
