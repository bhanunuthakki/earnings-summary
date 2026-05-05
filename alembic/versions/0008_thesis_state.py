"""thesis_state — convert micro_thesis/holdings/*.json into thesis_state rows.

Creates the thesis_state table and ingests each per-ticker JSON as a single
row. The raw JSON is preserved as raw_json for provenance; structured columns
(thesis, last_updated, breach_status) are populated where the source file's
shape matches a known schema. Files that don't match are still ingested with
their JSON payload intact.

Revision ID: 0008_thesis_state
Revises: 0007_kpi_definitions
Create Date: 2026-05-03
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0008_thesis_state"
down_revision: str | Sequence[str] | None = "0007_kpi_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_HOLDINGS_DIR = _PROJECT_ROOT / "micro_thesis" / "holdings"


def _parse_iso_or_none(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def upgrade() -> None:
    op.create_table(
        "thesis_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.Column("breach_status", sa.String(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ticker", name="uq_thesis_state_ticker"),
    )

    if not _HOLDINGS_DIR.exists():
        sys.stderr.write("0008: micro_thesis/holdings/ missing, skipping seed\n")
        return

    insert_sql = sa.text(
        "INSERT OR REPLACE INTO thesis_state "
        "(ticker, thesis, last_updated, breach_status, raw_json, ingested_at) "
        "VALUES (:ticker, :thesis, :last_updated, 'ok', :raw_json, :ingested_at)"
    )

    bind = op.get_bind()
    seeded = 0
    for fname in sorted(os.listdir(_HOLDINGS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = _HOLDINGS_DIR / fname
        ticker = path.stem.upper()
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"0008: skip {fname}: {e}\n")
            continue
        if not isinstance(payload, dict):
            sys.stderr.write(f"0008: skip {fname}: top level is not a dict\n")
            continue

        thesis = payload.get("thesis") if isinstance(payload.get("thesis"), str) else None
        last_updated = _parse_iso_or_none(payload.get("last_updated"))

        bind.execute(
            insert_sql,
            {
                "ticker": ticker,
                "thesis": thesis,
                "last_updated": last_updated,
                "raw_json": json.dumps(payload),
                "ingested_at": datetime.now(),
            },
        )
        seeded += 1

    sys.stderr.write(f"0008: thesis_state seeded {seeded} rows\n")


def downgrade() -> None:
    op.drop_table("thesis_state")
