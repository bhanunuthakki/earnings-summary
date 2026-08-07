"""backfill_tracked_companies_fiscal_year_end — populate fiscal_year_end (MM-DD).

Reads each tracked_companies row in portfolio/watchlist, opens the matching
`data/historical/fmp/{TICKER}_income_statement_annual.json` cache, picks the
most recent `period == 'FY'` row, and writes its date's MM-DD into the column.
Tickers without FMP annual coverage (e.g. KRX-listed names) fall back to a
small explicit override map.

Idempotent: only updates rows where fiscal_year_end IS NULL.

Revision ID: 0021_backfill_tracked_companies_fiscal_year_end
Revises: 0020_tracked_companies_archived_at
Create Date: 2026-05-10
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0021_backfill_tracked_companies_fiscal_year_end"
down_revision: str | Sequence[str] | None = "0020_tracked_companies_archived_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tickers without FMP annual income-statement coverage. KRX-listed names report
# on calendar year (Dec 31 fiscal year end).
_EXPLICIT_OVERRIDES: dict[str, str] = {
    "000660.KS": "12-31",
    "005930.KS": "12-31",
}


def _resolve_fmp_dir() -> Path:
    """Locate the FMP cache dir alongside the DB the migration is upgrading.

    Resolves the directory next to the configured `sqlalchemy.url` so the same
    migration applies cleanly whether you run it from the main checkout or from
    a git worktree (where `data/` may not be a sibling of `alembic/`).
    """
    bind = op.get_bind()
    url_db = Path(str(bind.engine.url.database))
    return url_db.resolve().parent / "historical" / "fmp"


def _fye_from_fmp(fmp_dir: Path, ticker: str) -> str | None:
    """Return MM-DD from the most recent FY annual row, or None if unavailable."""
    path = fmp_dir / f"{ticker}_income_statement_annual.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        return None
    fy_rows = [
        r for r in rows
        if isinstance(r, dict)
        and r.get("period") == "FY"
        and isinstance(r.get("date"), str)
        and len(r["date"]) >= 10
    ]
    if not fy_rows:
        return None
    fy_rows.sort(key=lambda r: r["date"], reverse=True)
    latest_date = fy_rows[0]["date"]
    return latest_date[5:10]  # "YYYY-MM-DD" → "MM-DD"


def upgrade() -> None:
    bind = op.get_bind()
    fmp_dir = _resolve_fmp_dir()
    rows = bind.execute(
        sa.text(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type IN ('portfolio', 'watchlist') "
            "AND fiscal_year_end IS NULL"
        )
    ).fetchall()

    update_sql = sa.text(
        "UPDATE tracked_companies SET fiscal_year_end = :fye WHERE ticker = :ticker"
    )

    populated = 0
    skipped: list[str] = []
    for (ticker,) in rows:
        fye = _fye_from_fmp(fmp_dir, ticker) or _EXPLICIT_OVERRIDES.get(ticker)
        if fye is None:
            skipped.append(ticker)
            continue
        bind.execute(update_sql, {"fye": fye, "ticker": ticker})
        populated += 1

    sys.stderr.write(f"0021: populated fiscal_year_end for {populated} tickers\n")
    if skipped:
        sys.stderr.write(
            f"0021: left fiscal_year_end NULL for {len(skipped)}: {', '.join(skipped)}\n"
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE tracked_companies SET fiscal_year_end = NULL"))
