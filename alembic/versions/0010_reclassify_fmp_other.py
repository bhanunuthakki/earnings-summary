"""reclassify_fmp_other — fix classifier gaps in 0003.

Three slug patterns landed in fmp_other in migration 0003 that should be
their own doc_types:

1. `{TICKER}_historical_ratings.json` and `_historical_grades.json` →
   fmp_grades (the 0003 map had a typo: `historical_rating` singular).
2. `{TICKER}_form_10k_{YEAR}.json` → fmp_10k_json (a new doc_type added
   to src/models/documents.py::DocType). These files are the FMP-served
   10-K JSON used as the gap-filler for segment operating income per
   project_data_process.md — they must NOT live in fmp_other.

`shares_float` and `financial_scores` intentionally stay in fmp_other
(summary stats, not core analytical data).

Revision ID: 0010_reclassify_fmp_other
Revises: 0009_remove_init_db_at_import
Create Date: 2026-05-03
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_reclassify_fmp_other"
down_revision: str | Sequence[str] | None = "0009_remove_init_db_at_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    grades_result = bind.execute(
        sa.text(
            "UPDATE documents "
            "SET doc_type = 'fmp_grades' "
            "WHERE source_type = 'fmp' "
            "  AND doc_type = 'fmp_other' "
            "  AND (file_path LIKE '%_historical_ratings.json' "
            "       OR file_path LIKE '%_historical_grades.json')"
        )
    )

    tenk_result = bind.execute(
        sa.text(
            "UPDATE documents "
            "SET doc_type = 'fmp_10k_json' "
            "WHERE source_type = 'fmp' "
            "  AND doc_type = 'fmp_other' "
            "  AND file_path LIKE '%_form_10k_%.json'"
        )
    )

    sys.stderr.write(
        f"0010: reclassified grades={grades_result.rowcount} 10k_json={tenk_result.rowcount}\n"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE documents SET doc_type = 'fmp_other' "
            "WHERE source_type = 'fmp' "
            "  AND doc_type IN ('fmp_grades', 'fmp_10k_json') "
            "  AND (file_path LIKE '%_historical_ratings.json' "
            "       OR file_path LIKE '%_historical_grades.json' "
            "       OR file_path LIKE '%_form_10k_%.json')"
        )
    )
