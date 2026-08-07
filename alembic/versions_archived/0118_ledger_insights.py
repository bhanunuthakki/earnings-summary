"""insight_notes + FTS5 — The Ledger's longitudinal synthesis layer (Wave B).

The semantic-memory tier over the episodic ``analyst_notes`` musings: one
``insight_notes`` row is a consolidated, source-grounded artifact —

  * kind='theme'  scope_key='theme:<slug>'  — a cluster of musings (seed/synth)
  * kind='stance' scope_key='<TICKER>'      — the standing view on a holding
  * kind='digest' scope_key='portfolio'     — the weekly cross-portfolio rollup

``source_note_ids`` (JSON array of ``analyst_notes.id``) is the grounding edge —
every synthesized claim cites the musings it came from (the NotebookLM closed-
corpus rule). ``watermark_id`` (max consolidated ``analyst_notes.id``) makes
synthesis INCREMENTAL: a scope with no new musings reuses its cached insight with
zero LLM cost. ``esi`` (Evidence Sensitivity Index) ships nullable here and is
populated by the Phase-3 conviction-drift detector. ``meta_json`` carries theme
display metadata (title, member tickers). NO real FK REFERENCES (code-level RI).

``analyst_notes_fts`` is a best-effort FTS5 index over ``analyst_notes.body`` for
BM25 search (retrieval-failure beats hallucination; no vector store). Guarded on
``ENABLE_FTS5`` — absent → skipped, and the reader falls back to LIKE.

Revision ID: 0118_ledger_insights
Revises: 0117_capture_audit_log
Create Date: 2026-06-29
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0118_ledger_insights"
down_revision: str | Sequence[str] | None = "0117_capture_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fts5_available(bind: sa.Connection) -> bool:
    try:
        rows = bind.execute(sa.text("PRAGMA compile_options")).fetchall()
    except sa.exc.SQLAlchemyError:
        return False
    return any("ENABLE_FTS5" in str(r[0]) for r in rows)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = set(inspector.get_table_names())

    if "insight_notes" not in names:
        op.create_table(
            "insight_notes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scope_key", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("body_md", sa.Text(), nullable=False),
            sa.Column("source_note_ids", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("meta_json", sa.Text(), nullable=True),
            sa.Column("as_of", sa.Text(), nullable=False),
            sa.Column("window_start", sa.Text(), nullable=True),
            sa.Column("window_end", sa.Text(), nullable=True),
            sa.Column("watermark_id", sa.Integer(), nullable=True),
            sa.Column("supersedes_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'current'")),
            sa.Column("esi", sa.Float(), nullable=True),
            sa.Column("provenance", sa.Text(), nullable=False, server_default=sa.text("'derived'")),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
        op.create_index("ix_insight_scope_status", "insight_notes", ["scope_key", "status"])
        op.create_index("ix_insight_kind", "insight_notes", ["kind"])

    # Best-effort FTS5 over analyst_notes.body (external-content table + sync
    # triggers). Skipped when FTS5 is not compiled in — the reader uses LIKE.
    if "analyst_notes" in names and "analyst_notes_fts" not in names and _fts5_available(bind):
        op.execute(
            "CREATE VIRTUAL TABLE analyst_notes_fts USING fts5("
            "body, content='analyst_notes', content_rowid='id')"
        )
        op.execute("INSERT INTO analyst_notes_fts(rowid, body) SELECT id, body FROM analyst_notes")
        op.execute(
            "CREATE TRIGGER analyst_notes_fts_ai AFTER INSERT ON analyst_notes BEGIN "
            "INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END"
        )
        op.execute(
            "CREATE TRIGGER analyst_notes_fts_ad AFTER DELETE ON analyst_notes BEGIN "
            "INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body) "
            "VALUES ('delete', old.id, old.body); END"
        )
        op.execute(
            "CREATE TRIGGER analyst_notes_fts_au AFTER UPDATE ON analyst_notes BEGIN "
            "INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body) "
            "VALUES ('delete', old.id, old.body); "
            "INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = set(inspector.get_table_names())
    for trig in ("analyst_notes_fts_au", "analyst_notes_fts_ad", "analyst_notes_fts_ai"):
        with contextlib.suppress(sa.exc.SQLAlchemyError):
            op.execute(f"DROP TRIGGER IF EXISTS {trig}")
    if "analyst_notes_fts" in names:
        op.execute("DROP TABLE IF EXISTS analyst_notes_fts")
    if "insight_notes" in names:
        existing = {idx["name"] for idx in inspector.get_indexes("insight_notes")}
        for name in ("ix_insight_scope_status", "ix_insight_kind"):
            if name in existing:
                op.drop_index(name, table_name="insight_notes")
        op.drop_table("insight_notes")
