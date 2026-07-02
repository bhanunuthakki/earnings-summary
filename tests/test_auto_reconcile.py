"""Auto-reconciliation — derive verdicts, never queue the derivable.

The 2026-07-02 owner correction: 27 of 28 queued reconcile items were
derivable from holdings/timeline/kind. This pins the derivations and that the
residual queue holds ONLY inferred falsifiers on held positions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from synthesis.auto_reconcile import auto_reconcile, auto_reconciled_summary
from synthesis.reconcile import list_unreconciled

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0129_commitment_scan_log"
HEAD = "0131_coach_pings"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_dismissal_id INTEGER,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid')
);
CREATE TABLE tenants (id TEXT PRIMARY KEY);
INSERT INTO tenants (id) VALUES ('bhanu');
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'bhanu' NOT NULL,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'open' NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    context_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT ck_analyst_notes_kind CHECK (kind IN
        ('question','decision','watch','assumption','observation','musing'))
);
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
    body, content='analyst_notes', content_rowid='id');
CREATE TABLE insight_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    body_md TEXT NOT NULL,
    meta_json TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE tracked_companies (ticker TEXT PRIMARY KEY, list_type TEXT NOT NULL);
INSERT INTO tracked_companies VALUES ('VEEV','portfolio');
INSERT INTO tracked_companies VALUES ('MU','watchlist');
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "auto.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    # Post-0130 seed shapes: a decision note, a musing, a theme, and two
    # inferred falsifiers — one held (VEEV), one not (MU).
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, source_ref, created_at, "
            "updated_at) VALUES ('decision','sold MU','capture','seed:decision:1',"
            "'2026-07-01','2026-07-01')"
        )
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, source_ref, created_at, "
            "updated_at) VALUES ('musing','I sell winners too early','capture',"
            "'seed:musing:1','2026-07-01','2026-07-01')"
        )
        conn.execute(
            "INSERT INTO insight_notes (scope_key, kind, body_md) VALUES "
            "('theme:x','theme','Sells winners too early')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('VEEV','add','owner',"
            "'Seat contraction 2Q. (inferred)','2026-05-01','2026-05-01')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('MU','sell','owner',"
            "'Memory cycle rolls over. (inferred)','2025-12-15','2025-12-15')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_derivables_never_reach_the_queue(db: Path) -> None:
    before = list_unreconciled(db)
    assert len(before) == 5  # the rote queue the owner rejected

    tally = auto_reconcile(db)
    assert tally == {
        "decisions_done": 1,
        "musings_live": 1,
        "themes_live": 1,
        "already_closed": 0,
        "falsifiers_dropped_unheld": 1,
    }

    residue = list_unreconciled(db)
    assert len(residue) == 1  # ONLY the held-name inferred falsifier
    assert residue[0].kind == "falsifier" and residue[0].label == "VEEV"

    # MU's moot falsifier is gone (never quotable, never extractable)
    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute("SELECT falsifier FROM decisions WHERE ticker='MU'").fetchone()[0] is None
        )
    finally:
        conn.close()

    summary = auto_reconciled_summary(db)
    assert summary == {"auto_done": 1, "auto_live": 2, "auto_dropped": 1}

    # Idempotent — a second pass resolves nothing new
    assert sum(auto_reconcile(db).values()) == 0


def test_missing_falsifier_on_held_position_surfaces(db: Path) -> None:
    """The gap class behind decision id=52 (VEEV): a live owner decision on a
    HELD name with an empty/NULL falsifier has no tripwire coverage — an
    irreducible owner-only ask that must survive the auto pass."""
    from synthesis.reconcile import falsifier_action, list_missing_falsifiers

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio')")
        # held + empty falsifier → the ask MUST surface
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('NU','initiate','owner','','2026-06-01','2026-06-01')"
        )
        # unheld/closed + empty falsifier → never re-ask (moot-drop behaviour)
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('MU','sell','owner',NULL,'2025-12-20','2025-12-20')"
        )
        conn.commit()
    finally:
        conn.close()

    auto_reconcile(db)
    gaps = list_missing_falsifiers(db)
    # NU asks. MU is unheld — silent. VEEV's '(inferred)' falsifier is already
    # pending in the ratify queue — no double-ask for the same position.
    assert [(g.kind, g.label) for g in gaps] == [("falsifier-missing", "NU")]

    # held + owner-supplied falsifier (the existing edit action) → ask clears
    assert falsifier_action(gaps[0].item_id, "edit", text="15-90d NPL >5% for 2Q", db_path=db)
    assert list_missing_falsifiers(db) == []


def test_missing_falsifier_ask_renders_on_reconcile_card(db: Path) -> None:
    """The ask is one dense line inside #ledger-reconcile wired to the existing
    falsifier edit action — and the empty state never paints over it."""
    from pipeline.ledger_panel import render_reconcile_list
    from synthesis.reconcile import falsifier_action

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio')")
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('NU','initiate','owner','','2026-06-01','2026-06-01')"
        )
        nu_id = int(conn.execute("SELECT id FROM decisions WHERE ticker='NU'").fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    auto_reconcile(db)
    # Clear the seed residue (VEEV's inferred falsifier) so the ask stands alone.
    for item in list_unreconciled(db):
        assert item.kind == "falsifier"
        falsifier_action(item.item_id, "ratify", db_path=db)

    html = render_reconcile_list(db)
    assert "Corpus reconciled" not in html  # the ask blocks the empty state
    assert "1 live decision needs a falsifier" in html and "NU" in html
    assert f'data-falsifier-action="edit" data-rec-id="{nu_id}"' in html

    # Owner supplies the falsifier → the ask clears, the empty state returns.
    falsifier_action(nu_id, "edit", text="15-90d NPL >5% for 2Q", db_path=db)
    assert "Corpus reconciled" in render_reconcile_list(db)


def test_owner_verdicts_are_never_overwritten(db: Path) -> None:
    from synthesis.reconcile import reconcile_note

    note_id = int(
        sqlite3.connect(str(db))
        .execute("SELECT id FROM analyst_notes WHERE kind='musing'")
        .fetchone()[0]
    )
    reconcile_note(note_id, "superseded", db_path=db)  # the owner spoke first
    auto_reconcile(db)
    conn = sqlite3.connect(str(db))
    try:
        ctx = conn.execute(
            "SELECT json_extract(context_json,'$.reconcile') FROM analyst_notes WHERE id=?",
            (note_id,),
        ).fetchone()[0]
        assert ctx == "superseded"  # auto never touches an owner verdict
    finally:
        conn.close()
