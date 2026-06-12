"""S15 PR1 — journal ↔ decision/position links + reconciliation.

Covers the whole chain:

  * migration 0093 round-trip (columns + partial indexes, downgrade clean),
  * notes substrate: create with links, set_note_links, supersede inherits
    the chain's links, defensive decode on a pre-0093 row shape,
  * journal_links: target labeling, link validation (dangling → LookupError),
    pending_reconciliation across both conclusion paths (decision graded /
    position exited) + the both-linked dedupe, reconcile_linked_notes
    (auto-resolve opt-in vs pending),
  * REST: /api/notes/<id>/link + /unlink + create-with-link, error paths,
  * journal panel: link controls + chips, the pending-reconciliation strip,
    the ?fragment=reconcile route,
  * inbox resurfacing: a reconciliation-pending note carries the reconcile
    title + 'pending' status.

DB built via alembic (stamp pre-0074, upgrade head) like test_journal_panel;
``decisions`` is hand-created in the post-0086 shape (its 0046 migration sits
before the stamp point), matching test_decision_conditions.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command
from dashboard.inbox import collect_inbox
from journal_links import (
    link_note,
    linkable_targets,
    pending_reconciliation,
    reconcile_linked_notes,
    unlink_note,
)
from pipeline.journal_panel import (
    render_journal_list,
    render_journal_panel,
    render_reconciliation_list,
)
from user_state.notes import create_note, get_note, set_note_links, supersede_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"

# decisions in the post-0086 shape (0046 predates the stamp point) — the
# columns journal_links reads; mirrors test_decision_conditions._SCHEMA.
_DECISIONS_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_lens VARCHAR(64),
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    user_notes TEXT,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    outcome_notes TEXT,
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    created_at DATETIME NOT NULL
);
"""


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DECISIONS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


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


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


_ARTIFACT_SEQ = iter(range(900, 100_000))


def _insert_decision(
    db_path: Path,
    *,
    ticker: str = "NU",
    kind: str = "trim",
    value: float | None = 20.0,
    conviction: str | None = "high",
    outcome_label: str | None = None,
    outcome_pct: float | None = None,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, "
            "conviction, source_artifact_id, made_at, outcome_at, outcome_label, "
            "outcome_pct, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ticker,
                kind,
                value,
                conviction,
                next(_ARTIFACT_SEQ),
                "2026-05-02T09:00:00",
                _now() if outcome_label and outcome_label != "pending" else None,
                outcome_label or "pending",
                outcome_pct,
                _now(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _insert_position(
    db_path: Path,
    *,
    ticker: str = "NU",
    entry_date: str | None = "2026-01-15",
    exit_date: str | None = None,
    outcome_vs_thesis: str | None = None,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO position_entries (user_id, ticker, entry_date, exit_date, "
            "outcome_vs_thesis, source, created_at, updated_at) "
            "VALUES ('bhanu', ?, ?, ?, ?, 'manual', ?, ?)",
            (ticker, entry_date, exit_date, outcome_vs_thesis, _now(), _now()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration 0093
# ---------------------------------------------------------------------------


def test_migration_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "round_trip.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_notes)")}
    assert {"decision_id", "position_entry_id", "link_auto_resolve"} <= cols
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(analyst_notes)")}
    assert "ix_analyst_notes_decision" in indexes
    assert "ix_analyst_notes_position_entry" in indexes
    conn.close()

    command.downgrade(cfg, "-1")
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_notes)")}
    assert not {"decision_id", "position_entry_id", "link_auto_resolve"} & cols
    # The 0074 partial unique must survive the batch recreate intact.
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_analyst_notes_source_ref'"
    ).fetchone()
    assert sql_row is not None and "WHERE source_ref IS NOT NULL" in str(sql_row[0])
    conn.close()

    command.upgrade(cfg, "head")  # idempotent re-upgrade


# ---------------------------------------------------------------------------
# Notes substrate
# ---------------------------------------------------------------------------


def test_create_note_with_links_and_supersede_inherits(db_path: Path) -> None:
    d_id = _insert_decision(db_path)
    n = create_note(
        ticker="NU",
        kind="assumption",
        body="Trim was about valuation, not thesis.",
        decision_id=d_id,
        link_auto_resolve=True,
        db_path=db_path,
    )
    assert n.decision_id == d_id
    assert n.position_entry_id is None
    assert n.link_auto_resolve is True

    replacement = supersede_note(
        n.id, body="Refined: trim was about concentration.", db_path=db_path
    )
    assert replacement.decision_id == d_id
    assert replacement.link_auto_resolve is True
    assert replacement.supersedes_id == n.id


def test_set_note_links_partial_updates(db_path: Path) -> None:
    p_id = _insert_position(db_path)
    n = create_note(ticker="NU", kind="watch", body="Watch NIM.", db_path=db_path)
    linked = set_note_links(n.id, position_entry_id=p_id, db_path=db_path)
    assert linked is not None and linked.position_entry_id == p_id
    assert linked.link_auto_resolve is False  # untouched
    cleared = set_note_links(n.id, position_entry_id=None, db_path=db_path)
    assert cleared is not None and cleared.position_entry_id is None
    assert set_note_links(99_999, decision_id=1, db_path=db_path) is None


def test_plain_create_still_works_on_pre_link_schema(tmp_path: Path) -> None:
    """The default write path must keep working against a hand-rolled 0074
    schema (no 0093 columns) — only link-carrying writes require 0093."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE analyst_notes (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, "
        "kind TEXT, status TEXT, body TEXT, anchor_type TEXT, anchor_key TEXT, source TEXT, "
        "source_ref TEXT, supersedes_id INTEGER, resolution_note TEXT, context_json TEXT, "
        "created_at TEXT, updated_at TEXT, resolved_at TEXT)"
    )
    conn.commit()
    conn.close()
    n = create_note(ticker="NU", kind="watch", body="plain", db_path=db)
    assert n.decision_id is None and n.link_auto_resolve is False
    fetched = get_note(n.id, db_path=db)
    assert fetched is not None and fetched.position_entry_id is None


# ---------------------------------------------------------------------------
# journal_links — targets, validation, reconciliation
# ---------------------------------------------------------------------------


def test_linkable_targets_labels(db_path: Path) -> None:
    _insert_decision(db_path, kind="trim", value=20.0, conviction="high")
    _insert_position(db_path, entry_date="2026-01-15")
    targets = linkable_targets(ticker="nu", db_path=db_path)
    kinds = [t.kind for t in targets]
    assert kinds == ["decision", "position"]
    assert "TRIM 20%" in targets[0].label and "high conviction" in targets[0].label
    assert targets[0].concluded is False
    assert targets[1].label.startswith("2026-01-15 → open")


def test_link_note_validates_targets(db_path: Path) -> None:
    n = create_note(ticker="NU", kind="watch", body="x", db_path=db_path)
    with pytest.raises(ValueError):
        link_note(n.id, db_path=db_path)
    with pytest.raises(LookupError):
        link_note(n.id, decision_id=12_345, db_path=db_path)
    with pytest.raises(LookupError):
        link_note(n.id, position_entry_id=12_345, db_path=db_path)
    d_id = _insert_decision(db_path)
    linked = link_note(n.id, decision_id=d_id, auto_resolve=True, db_path=db_path)
    assert linked is not None and linked.decision_id == d_id and linked.link_auto_resolve
    unlinked = unlink_note(n.id, db_path=db_path)
    assert unlinked is not None
    assert unlinked.decision_id is None and unlinked.link_auto_resolve is False


def test_pending_reconciliation_both_paths_and_dedupe(db_path: Path) -> None:
    graded = _insert_decision(db_path, outcome_label="correct", outcome_pct=18.2)
    open_d = _insert_decision(db_path, kind="add", value=5.0)
    exited = _insert_position(db_path, exit_date="2026-06-01", outcome_vs_thesis="played_out")
    open_p = _insert_position(db_path, ticker="MELI")

    n_decision = create_note(
        ticker="NU", kind="assumption", body="graded-decision note",
        decision_id=graded, db_path=db_path,
    )  # fmt: skip
    create_note(  # linked to a still-open decision → NOT pending
        ticker="NU", kind="watch", body="open-decision note",
        decision_id=open_d, db_path=db_path,
    )  # fmt: skip
    n_position = create_note(
        ticker="NU", kind="watch", body="exited-position note",
        position_entry_id=exited, db_path=db_path,
    )  # fmt: skip
    create_note(  # open position → NOT pending
        ticker="MELI", kind="watch", body="open-position note",
        position_entry_id=open_p, db_path=db_path,
    )  # fmt: skip
    n_both = create_note(  # both links concluded → surfaces ONCE, decision wins
        ticker="NU", kind="question", body="both-linked note",
        decision_id=graded, position_entry_id=exited, db_path=db_path,
    )  # fmt: skip

    items = pending_reconciliation(db_path=db_path)
    by_note = {item.note.id: item for item in items}
    assert set(by_note) == {n_decision.id, n_position.id, n_both.id}
    assert by_note[n_decision.id].target.kind == "decision"
    assert "graded correct (+18.2%)" in (by_note[n_decision.id].target.conclusion or "")
    assert by_note[n_position.id].target.kind == "position"
    assert "exited 2026-06-01, played_out" in (by_note[n_position.id].target.conclusion or "")
    assert by_note[n_both.id].target.kind == "decision"  # decision conclusion wins
    assert "Linked decision concluded" in by_note[n_decision.id].suggested_resolution

    # Ticker filter
    only_meli = pending_reconciliation(db_path=db_path, ticker="MELI")
    assert only_meli == []


def test_reconcile_linked_notes_auto_vs_pending(db_path: Path) -> None:
    graded = _insert_decision(db_path, outcome_label="wrong", outcome_pct=-7.0)
    n_auto = create_note(
        ticker="NU", kind="watch", body="auto note",
        decision_id=graded, link_auto_resolve=True, db_path=db_path,
    )  # fmt: skip
    n_manual = create_note(
        ticker="NU", kind="watch", body="manual note",
        decision_id=graded, db_path=db_path,
    )  # fmt: skip

    tally = reconcile_linked_notes(db_path=db_path)
    assert tally == {"auto_resolved": 1, "pending": 1, "db_unavailable": 0}

    resolved = get_note(n_auto.id, db_path=db_path)
    assert resolved is not None and resolved.status == "resolved"
    assert resolved.resolution_note is not None
    assert resolved.resolution_note.startswith("auto-resolved:")
    assert "graded wrong" in resolved.resolution_note

    still_open = get_note(n_manual.id, db_path=db_path)
    assert still_open is not None and still_open.status == "open"

    # Idempotent: the resolved note left the pending set; the manual one stays.
    assert reconcile_linked_notes(db_path=db_path) == {
        "auto_resolved": 0,
        "pending": 1,
        "db_unavailable": 0,
    }


def test_reconcile_db_unavailable(tmp_path: Path) -> None:
    assert reconcile_linked_notes(db_path=tmp_path / "missing.db") == {
        "auto_resolved": 0,
        "pending": 0,
        "db_unavailable": 1,
    }


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def test_link_unlink_rest(client: FlaskClient, db_path: Path) -> None:
    d_id = _insert_decision(db_path)
    n = create_note(ticker="NU", kind="watch", body="rest-linked", db_path=db_path)

    linked = client.post(
        f"/api/notes/{n.id}/link", json={"decision_id": d_id, "auto_resolve": True}
    )
    assert linked.status_code == 200
    note_json = linked.get_json()["note"]
    assert note_json["decision_id"] == d_id
    assert note_json["link_auto_resolve"] is True

    assert client.post(f"/api/notes/{n.id}/link", json={}).status_code == 400
    assert client.post(f"/api/notes/{n.id}/link", json={"decision_id": 999_999}).status_code == 404
    assert client.post("/api/notes/99999/link", json={"decision_id": d_id}).status_code == 404

    unlinked = client.post(f"/api/notes/{n.id}/unlink", json={})
    assert unlinked.status_code == 200
    assert unlinked.get_json()["note"]["decision_id"] is None


def test_create_note_with_link_rest(client: FlaskClient, db_path: Path) -> None:
    p_id = _insert_position(db_path)
    created = client.post(
        "/api/notes",
        json={
            "ticker": "NU",
            "kind": "watch",
            "body": "born linked",
            "position_entry_id": p_id,
            "auto_resolve": True,
        },
    )
    assert created.status_code == 201
    note_json = created.get_json()["note"]
    assert note_json["position_entry_id"] == p_id
    assert note_json["link_auto_resolve"] is True
    dangling = client.post(
        "/api/notes",
        json={"ticker": "NU", "kind": "watch", "body": "x", "decision_id": 777_777},
    )
    assert dangling.status_code == 404


# ---------------------------------------------------------------------------
# Journal panel
# ---------------------------------------------------------------------------


def test_panel_list_carries_link_controls_and_chip(db_path: Path) -> None:
    d_id = _insert_decision(db_path)
    create_note(ticker="NU", kind="watch", body="unlinked note", db_path=db_path)
    create_note(ticker="NU", kind="watch", body="linked note", decision_id=d_id, db_path=db_path)
    html = render_journal_list(db_path)
    # Unlinked open note offers the link dropdown; linked one shows chip + unlink.
    assert 'data-role="link-target"' in html
    assert 'data-act="link"' in html
    assert 'data-role="link-auto"' in html
    assert f"decision #{d_id}" in html
    assert 'data-act="unlink"' in html
    assert "TRIM 20%" in html  # dropdown option label


def test_reconciliation_strip_and_panel(db_path: Path) -> None:
    graded = _insert_decision(db_path, outcome_label="correct", outcome_pct=4.2)
    create_note(
        ticker="NU", kind="watch", body="needs reconciling",
        decision_id=graded, db_path=db_path,
    )  # fmt: skip
    strip = render_reconciliation_list(db_path)
    assert "pending reconciliation" in strip
    assert "needs reconciling" in strip
    assert "graded correct" in strip
    assert 'data-act="rec-resolve"' in strip
    assert 'data-act="unlink"' in strip
    assert "data-suggest=" in strip

    panel = render_journal_panel(db_path)
    assert 'id="jr-reconcile"' in panel
    assert "needs reconciling" in panel

    # Nothing pending → the strip vanishes entirely.
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE analyst_notes SET status='resolved'")
    conn.commit()
    conn.close()
    assert render_reconciliation_list(db_path) == ""


def test_reconcile_fragment_route(client: FlaskClient, db_path: Path) -> None:
    graded = _insert_decision(db_path, outcome_label="mixed")
    create_note(
        ticker="NU", kind="watch", body="route reconcile",
        decision_id=graded, db_path=db_path,
    )  # fmt: skip
    frag = client.get("/api/panel/journal?fragment=reconcile")
    assert frag.status_code == 200
    assert b"route reconcile" in frag.data
    assert b"jr-filters" not in frag.data  # strip-only fragment


# ---------------------------------------------------------------------------
# Inbox resurfacing
# ---------------------------------------------------------------------------


def test_inbox_marks_reconciliation_notes(db_path: Path) -> None:
    graded = _insert_decision(db_path, outcome_label="correct", outcome_pct=11.0)
    create_note(
        ticker="NU", kind="watch", body="reconcile me in the inbox",
        decision_id=graded, db_path=db_path,
    )  # fmt: skip
    create_note(ticker="NU", kind="watch", body="ordinary watch item", db_path=db_path)

    items = collect_inbox(db_path, kinds=("note",), position_weights={})
    by_body = {it.body.split(" — ")[0]: it for it in items}
    marked = by_body["reconcile me in the inbox"]
    assert marked.title == "reconcile · watch"
    assert marked.status == "pending"
    assert "graded correct" in marked.body
    plain = by_body["ordinary watch item"]
    assert plain.title == "watch"
    assert plain.status is None
