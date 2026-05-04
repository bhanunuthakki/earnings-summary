"""classify_10q_files — backfill documents rows for newly fetched 10-Q JSONs.

Walks data/historical/fmp/ for `{TICKER}_form_10q_{YEAR}_{QUARTER}.json` files
and inserts one documents row per file with source_type='fmp',
doc_type='fmp_10q_json'. Idempotent on the sha256 UNIQUE constraint.

Uses INSERT OR IGNORE so re-running after additional 10-Q fetches keeps
adding new rows without touching existing ones.

Revision ID: 0015_classify_10q_files
Revises: 0014_dcf_runs_segment_name
Create Date: 2026-05-03
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0015_classify_10q_files"
down_revision: str | Sequence[str] | None = "0014_dcf_runs_segment_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FMP_DIR = _PROJECT_ROOT / "data" / "historical" / "fmp"

_NAME_RX = re.compile(r"^([A-Z]+)_form_10q_(\d{4})_(Q[1-4])\.json$")
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _max_date_in_record(path: Path) -> datetime | None:
    """Walk the 10-Q JSON record looking for any 'items' row dates; return max."""
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    max_iso: str | None = None
    for section in record.values():
        if not isinstance(section, list):
            continue
        for row in section[:10]:
            if not isinstance(row, dict):
                continue
            items = row.get("items")
            if not isinstance(items, list):
                continue
            for v in items:
                if not isinstance(v, str):
                    continue
                # Try parsing 'Sep. 30, 2024' or '2024-09-30'
                if _DATE_RX.match(v):
                    d = v[:10]
                    if max_iso is None or d > max_iso:
                        max_iso = d
            break
    if max_iso is None:
        return None
    return datetime.fromisoformat(max_iso)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upgrade() -> None:
    if not _FMP_DIR.exists():
        sys.stderr.write("0015: FMP dir missing; nothing to classify\n")
        return

    bind = op.get_bind()
    insert_sql = sa.text(
        "INSERT OR IGNORE INTO documents "
        "(ticker, source_type, doc_type, period_end, file_path, sha256, "
        " fetched_at, fetch_status, raw_bytes_size, source_url) "
        "VALUES (:ticker, 'fmp', 'fmp_10q_json', :period_end, :file_path, :sha256, "
        " :fetched_at, 'ok', :raw_bytes_size, NULL)"
    )

    inserted = 0
    skipped_existing = 0
    skipped_pattern = 0

    for fname in sorted(os.listdir(_FMP_DIR)):
        match = _NAME_RX.match(fname)
        if not match:
            skipped_pattern += 1
            continue
        path = _FMP_DIR / fname
        if not path.is_file():
            continue
        ticker = match.group(1)
        sha = _sha256_of(path)
        period_end = _max_date_in_record(path)
        stat = path.stat()
        result = bind.execute(
            insert_sql,
            {
                "ticker": ticker,
                "period_end": period_end,
                "file_path": str(path.relative_to(_PROJECT_ROOT)).replace("\\", "/"),
                "sha256": sha,
                "fetched_at": datetime.fromtimestamp(stat.st_mtime),
                "raw_bytes_size": stat.st_size,
            },
        )
        if result.rowcount > 0:
            inserted += 1
        else:
            skipped_existing += 1

    sys.stderr.write(
        f"0015: 10-Q classify — inserted={inserted} "
        f"skipped_existing={skipped_existing} non-matching={skipped_pattern}\n"
    )


def downgrade() -> None:
    op.execute("DELETE FROM documents WHERE source_type = 'fmp' AND doc_type = 'fmp_10q_json'")
