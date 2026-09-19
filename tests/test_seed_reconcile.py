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
    reconcile_theme,
)
from synthesis.seed_decisions import backfill_seed_decisions

_SEED = {
    "decisions": [
        {
            "ticker": "MU",
            "action": "buy",
            "approx_date": "2025-07",
            "conviction": "high",
            "rationale": "Memory upcycle.",
            "falsifier": "Memory pricing cycle rolls over. (inferred)",
        },
        {
            "ticker": "NU",
            "action": "add",
            "approx_date": "2026-03",
            "conviction": "high",
            "rationale": "Credit book holding.",
            "falsifier": "15-90d NPL >5% for 2Q",
        },
    ],
    "musings": [],
    "themes": [],
}


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
        conn.execute(
            "INSERT INTO insight_notes (scope_key, kind, body_md, provenance, as_of, created_at, updated_at) "
            "VALUES ('theme:sell-winners-too-early','theme','Sells winners too early','owner','2026-07-01','2026-07-01','2026-07-01')"
        )
    return db


@pytest.fixture
def seeded(db_path: Path, tmp_path: Path) -> Path:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(_SEED), encoding="utf-8")
    backfill_seed_decisions(db_path, seed)
    return db_path


def _kinds(db: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in list_unreconciled(db):
        out[item.kind] = out.get(item.kind, 0) + 1
    return out


def test_list_unreconciled_covers_notes_themes_falsifiers(seeded: Path) -> None:
    # 1 seeded musing + 1 LEAP intent note, 1 theme, 1 inferred falsifier (MU
    # only — NU's falsifier carries no marker and needs no ratification)
    assert _kinds(seeded) == {"note": 2, "theme": 1, "falsifier": 1}


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


def test_theme_verdict_supersedes(seeded: Path) -> None:
    theme = next(i for i in list_unreconciled(seeded) if i.kind == "theme")
    assert reconcile_theme(theme.item_id, "superseded", db_path=seeded)
    assert _kinds(seeded).get("theme", 0) == 0
    conn = sqlite3.connect(str(seeded))
    try:
        assert (
            conn.execute(
                "SELECT status FROM insight_notes WHERE id=?", (theme.item_id,)
            ).fetchone()[0]
            == "superseded"
        )
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
        elif item.kind == "theme":
            reconcile_theme(item.item_id, "live", db_path=seeded)
        else:
            falsifier_action(item.item_id, "ratify", db_path=seeded)
    assert "Corpus reconciled" in render_reconcile_list(seeded)
