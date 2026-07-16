"""v_thesis_status — the STUB filter must match EMBEDDED markers, not prefixes.

Red-team wave B (2026-07-16): 0151 excluded stub placeholders with a PREFIX
predicate (``thesis NOT LIKE 'STUB:%'``), but prod carries stub rows with the
marker embedded mid-text — live example (ROP): ``"Roper Technologies —
diversified industrial software. STUB: needs user-authored thesis…"``. Those
rows still read ``has_written_thesis=1``, so the Memos swap screen kept
crowning ROP "best available alternative". Recreate the view with a SUBSTRING
predicate (``thesis NOT LIKE '%STUB:%'`` — the literal ``STUB:`` token, colon
included, so prose containing "stubborn"/"STUBHUB" is untouched); everything
else is byte-identical to 0151.

Guard pattern (0143 post-#883, verbatim): SQLite validates EVERY view in the
schema during ``ALTER TABLE ... RENAME`` (any batch_alter_table), so a view
over absent tables (stamp-then-upgrade test DBs) aborts unrelated later
migrations. Create the view only when ALL source tables exist;
DROP VIEW IF EXISTS first keeps it idempotent. Downgrade restores the 0151
(prefix) definition under the same guard.

Revision ID: 0152_v_thesis_status_stub_substring
Revises: 0151_v_thesis_status_stub_filter
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0152_v_thesis_status_stub_substring"
down_revision: str | None = "0151_v_thesis_status_stub_filter"
branch_labels = None
depends_on = None

# Every table the view SELECTs from — the view is only (re)created when ALL
# are present (see the guard rationale in the module docstring / 0143).
_SOURCE_TABLES = ("thesis_state", "thesis_ledger_entries", "analyst_notes")

# One shared skeleton; only the has_thesis CASE differs between revisions.
_VIEW_SQL_TEMPLATE = """
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
           CASE WHEN {has_thesis_predicate} THEN 1 ELSE 0 END AS has_thesis
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

# This revision: non-empty AND no embedded bulk-onboarding stub marker.
_VIEW_SQL_STUB_SUBSTRING = _VIEW_SQL_TEMPLATE.format(
    has_thesis_predicate=("TRIM(COALESCE(thesis, '')) <> '' AND thesis NOT LIKE '%STUB:%'")
)

# The 0151 definition (prefix-only stub filter), restored on downgrade.
_VIEW_SQL_0151 = _VIEW_SQL_TEMPLATE.format(
    has_thesis_predicate=("TRIM(COALESCE(thesis, '')) <> '' AND thesis NOT LIKE 'STUB:%'")
)


def _recreate_view(view_sql: str) -> None:
    op.execute("DROP VIEW IF EXISTS v_thesis_status")
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if all(t in tables for t in _SOURCE_TABLES):
        op.execute(view_sql)


def upgrade() -> None:
    _recreate_view(_VIEW_SQL_STUB_SUBSTRING)


def downgrade() -> None:
    _recreate_view(_VIEW_SQL_0151)
