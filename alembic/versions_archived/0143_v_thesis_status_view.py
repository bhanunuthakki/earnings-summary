"""v_thesis_status — a per-ticker read contract over the research substrate.

2026-07-05: the companion portfolio-tracker builds its monthly CIO brief from
its OWN tables (``TradeDecision`` etc.), so the brief says things like "the
decision log is empty" and "FLKR is held without a written thesis" even when
this repo holds the real research history. The tracker already reads this DB
read-only (``EARNINGS_SUMMARY_DB_PATH``, ``mode=ro``) for ``thesis_state``;
this view gives it ONE stable, earnings-summary-owned surface to read the rest
of the picture per ticker, so the brief can be grounded in the actual ledger /
notes rather than the tracker's empty native log.

Being a VIEW (not a table), it auto-updates as the ledger / notes / thesis_state
tables change — no cron, no denormalized copy to keep in sync. earnings-summary
owns the join logic here, so its table shapes can evolve without breaking the
tracker (the tracker just ``SELECT``s from the view).

Columns, one row per ticker that appears in ANY of the three source tables:
- ``ticker``               — canonical UPPER-cased ticker.
- ``has_written_thesis``   — 1 when the latest ``thesis_state`` row carries a
                             non-empty thesis, else 0. A ticker with only a
                             ledger/notes footprint (no thesis) reads 0; a
                             thesis-only ticker (WIX: thesis, zero ledger) reads 1.
- ``breach_status``        — the latest ``thesis_state`` breach flag ('ok'/'warn'/…).
- ``thesis_updated_at``    — when that thesis_state row was last refreshed.
- ``ledger_entry_count``   — total accepted ``thesis_ledger_entries`` for the ticker.
- ``last_ledger_at``       — most-recent ledger ``created_at`` (the "last decision"
                             signal that refutes an "empty decision log").
- ``open_notes_count``     — live (status='open') ``analyst_notes`` of any kind.
- ``open_questions_count`` — the subset of those whose kind is 'question'.

Single-user platform (the substrate carries one user_id, 'bhanu'), so the grain
is the ticker — ledger/notes are aggregated across user_id rather than keyed by
it. The ``thesis_state`` sub-select leans on SQLite's documented bare-column /
MAX() behaviour: with ``MAX(last_updated)`` the bare ``breach_status`` / ``thesis``
come from that same latest row, so a ticker with multiple thesis_state rows
resolves to its current state deterministically.

Revision ID: 0143_v_thesis_status_view
Revises: 0142_alerts_dismiss_reason
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0143_v_thesis_status_view"
down_revision: str | None = "0142_alerts_dismiss_reason"
branch_labels = None
depends_on = None

# Every table the view SELECTs from. The view is only created when ALL are
# present — see upgrade() for why a dangling view is a live landmine.
_SOURCE_TABLES = ("thesis_state", "thesis_ledger_entries", "analyst_notes")

_VIEW_SQL = """
CREATE VIEW v_thesis_status AS
WITH tk(ticker) AS (
    SELECT UPPER(ticker) FROM thesis_state
        WHERE ticker IS NOT NULL AND TRIM(ticker) <> ''
    UNION
    SELECT UPPER(ticker) FROM thesis_ledger_entries
        WHERE ticker IS NOT NULL AND TRIM(ticker) <> ''
    UNION
    SELECT UPPER(ticker) FROM analyst_notes
        WHERE ticker IS NOT NULL AND TRIM(ticker) <> ''
)
SELECT
    tk.ticker                          AS ticker,
    COALESCE(ts.has_thesis, 0)         AS has_written_thesis,
    ts.breach_status                   AS breach_status,
    ts.thesis_updated_at               AS thesis_updated_at,
    COALESCE(le.entry_count, 0)        AS ledger_entry_count,
    le.last_ledger_at                  AS last_ledger_at,
    COALESCE(an.open_notes, 0)         AS open_notes_count,
    COALESCE(an.open_questions, 0)     AS open_questions_count
FROM tk
LEFT JOIN (
    SELECT UPPER(ticker)                                        AS ticker,
           MAX(last_updated)                                    AS thesis_updated_at,
           breach_status                                        AS breach_status,
           CASE WHEN TRIM(COALESCE(thesis, '')) <> '' THEN 1 ELSE 0 END AS has_thesis
    FROM thesis_state
    GROUP BY UPPER(ticker)
) ts ON ts.ticker = tk.ticker
LEFT JOIN (
    SELECT UPPER(ticker)   AS ticker,
           COUNT(*)        AS entry_count,
           MAX(created_at) AS last_ledger_at
    FROM thesis_ledger_entries
    GROUP BY UPPER(ticker)
) le ON le.ticker = tk.ticker
LEFT JOIN (
    SELECT UPPER(ticker) AS ticker,
           SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END)                        AS open_notes,
           SUM(CASE WHEN status = 'open' AND kind = 'question' THEN 1 ELSE 0 END)  AS open_questions
    FROM analyst_notes
    GROUP BY UPPER(ticker)
) an ON an.ticker = tk.ticker
"""


def upgrade() -> None:
    # Guarded CREATE VIEW. The original revision created the view
    # unconditionally, on the theory that SQLite binds a view's tables lazily
    # (CREATE VIEW succeeds without thesis_state/analyst_notes; the error only
    # surfaces on SELECT, which readers catch). That held until 2026-07-16:
    # SQLite validates EVERY view in the schema during ALTER TABLE ... RENAME —
    # the rename step of any batch_alter_table — so a dangling view turns every
    # later batch migration into "error in view v_thesis_status" on DBs built
    # by stamp-then-upgrade tests (which never ran 0008/0074). Create the view
    # only when all of its source tables exist; a DB without them has no
    # readers of the view either. DROP-then-CREATE keeps it idempotent.
    op.execute("DROP VIEW IF EXISTS v_thesis_status")
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if all(t in tables for t in _SOURCE_TABLES):
        op.execute(_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_thesis_status")
