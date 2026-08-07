"""backfill_documents_from_fmp_files — populate documents from data/historical/fmp/.

Walks the post-backfill FMP JSON cache, computes sha256 per file, classifies by
filename pattern, extracts period_end from JSON content, and inserts one
documents row per file with source_type='fmp'.

Idempotent: UNIQUE constraint on sha256 means re-running is a no-op for files
whose content hasn't changed.

Revision ID: 0003_backfill_documents_from_fmp_files
Revises: 0002_documents_table
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

revision: str = "0003_backfill_documents_from_fmp_files"
down_revision: str | Sequence[str] | None = "0002_documents_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FMP_DIR = _PROJECT_ROOT / "data" / "historical" / "fmp"

_PERIOD_SUFFIXES = ("annual", "quarterly", "ttm")
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")

_FMP_DOC_TYPE_MAP: dict[str, str] = {
    "income_statement": "fmp_income_statement",
    "balance_sheet": "fmp_balance_sheet",
    "cash_flow": "fmp_cashflow",
    "as_reported_income": "fmp_as_reported_income",
    "as_reported_balance": "fmp_as_reported_balance",
    "as_reported_cashflow": "fmp_as_reported_cashflow",
    "as_reported_financial": "fmp_as_reported_financial",
    "income_growth": "fmp_financial_growth",
    "balance_growth": "fmp_financial_growth",
    "cashflow_growth": "fmp_financial_growth",
    "financial_growth": "fmp_financial_growth",
    "product_segments": "fmp_segment_product",
    "geo_segments": "fmp_segment_geographic",
    "key_metrics": "fmp_key_metrics",
    "financial_ratios": "fmp_financial_ratios",
    "enterprise_values": "fmp_enterprise_values",
    "owner_earnings": "fmp_owner_earnings",
    "dcf_basic": "fmp_dcf",
    "dcf_levered": "fmp_dcf_levered",
    "profile": "fmp_profile",
    "earnings_calendar": "fmp_earnings_calendar",
    "peers": "fmp_peers",
    "company_executives": "fmp_executives",
    "historical_employee_count": "fmp_historical_employees",
    "historical_market_cap": "fmp_historical_market_cap",
    "historical_rating": "fmp_grades",
    "price_target_consensus": "fmp_price_target_consensus",
    "price_chart_10y_div_adj": "fmp_historical_price",
    "financial_reports_dates": "fmp_financial_reports_dates",
    "analyst_estimates": "fmp_analyst_estimates",
    "financial_scores": "fmp_other",
    "shares_float": "fmp_other",
    "ratings_snapshot": "fmp_grades",
    "grades_summary": "fmp_grades",
    "grades_consensus": "fmp_grades",
    "price_target_summary": "fmp_price_target_consensus",
}


def _classify(stem: str) -> tuple[str, str]:
    """Return (ticker, doc_type) from filename stem; period suffix is stripped."""
    parts = stem.split("_", 1)
    if len(parts) < 2:
        return (stem.upper(), "fmp_other")
    ticker, rest = parts[0].upper(), parts[1]
    for p in _PERIOD_SUFFIXES:
        if rest.endswith(f"_{p}"):
            rest = rest[: -len(p) - 1]
            break
    return (ticker, _FMP_DOC_TYPE_MAP.get(rest, "fmp_other"))


def _max_date_in_records(path: Path) -> datetime | None:
    """Scan FMP JSON for max `date` or `fillingDate`; return as datetime or None."""
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(records, list):
        return None
    max_iso: str | None = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key in ("date", "fillingDate"):
            v = rec.get(key)
            if isinstance(v, str) and _DATE_RX.match(v):
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
        sys.stderr.write("0003: FMP dir missing, no rows to backfill\n")
        return

    bind = op.get_bind()
    insert_sql = sa.text(
        "INSERT OR IGNORE INTO documents "
        "(ticker, source_type, doc_type, period_end, file_path, sha256, "
        " fetched_at, fetch_status, raw_bytes_size, source_url) "
        "VALUES (:ticker, 'fmp', :doc_type, :period_end, :file_path, :sha256, "
        " :fetched_at, 'ok', :raw_bytes_size, NULL)"
    )

    inserted = 0
    skipped = 0
    for fname in sorted(os.listdir(_FMP_DIR)):
        path = _FMP_DIR / fname
        if not path.is_file():
            continue
        stem, ext = os.path.splitext(fname)
        if ext.lower() != ".json":
            skipped += 1
            continue

        ticker, doc_type = _classify(stem)
        period_end = _max_date_in_records(path)
        sha = _sha256_of(path)
        stat = path.stat()

        result = bind.execute(
            insert_sql,
            {
                "ticker": ticker,
                "doc_type": doc_type,
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
            skipped += 1

    sys.stderr.write(f"0003: documents backfill — inserted={inserted} skipped={skipped}\n")


def downgrade() -> None:
    op.execute("DELETE FROM documents WHERE source_type = 'fmp'")
