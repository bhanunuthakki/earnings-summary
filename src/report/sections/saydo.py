"""Read recorded management commitments and their realized outcomes."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from report.models import SayDoHistoricalMetric, SayDoSection, SectionStatus
from report.sections._common import has_table, missing, open_repo_db
from report.sections.p3_data import load_saydo_verdicts

log = logging.getLogger(__name__)


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> SayDoSection:
    """Load the source-linked commitment ledger and its recorded verdicts."""
    try:
        db_conn = open_repo_db(repo_root, conn)
    except (sqlite3.Error, OSError):
        db_conn = None
    if db_conn is None:
        log.warning({"event": "saydo_verdicts_unavailable", "ticker": ticker})
        return SayDoSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(management_commitments)",
                fix_command="Check the configured portfolio database and management_commitments table.",
                detail="Source-linked management commitments are unavailable.",
            ),
            verdicts_availability="unavailable",
        )
    try:
        if not has_table(db_conn, "management_commitments"):
            return SayDoSection(
                status=SectionStatus.MISSING_DATA,
                missing=missing(
                    stage="SYNTHESIZE(management_commitments)",
                    fix_command="Restore the management_commitments table from the canonical schema.",
                    detail="Source-linked management commitments are unavailable.",
                ),
                verdicts_availability="unavailable",
            )
        verdicts = load_saydo_verdicts(ticker, conn=db_conn)
        historical_metrics: list[SayDoHistoricalMetric] = []
        if has_table(db_conn, "saydo_historical_metrics"):
            rows = db_conn.execute(
                "SELECT id, period_made, period_target, kpi_name, comparator, "
                "target_value, realized_value, outcome, guidance_narrative, realized_narrative "
                "FROM saydo_historical_metrics WHERE ticker = ? "
                "ORDER BY period_target DESC, period_made DESC, id DESC",
                (ticker.upper(),),
            ).fetchall()
            for row in rows:
                period_made = row["period_made"]
                period_target = row["period_target"]
                if isinstance(period_made, str):
                    period_made = datetime.fromisoformat(period_made)
                if isinstance(period_target, str):
                    period_target = datetime.fromisoformat(period_target)
                historical_metrics.append(
                    SayDoHistoricalMetric(
                        id=int(row["id"]),
                        period_made=period_made,
                        period_target=period_target,
                        kpi_name=row["kpi_name"],
                        comparator=row["comparator"],
                        target_value=float(row["target_value"]),
                        realized_value=(
                            float(row["realized_value"])
                            if row["realized_value"] is not None
                            else None
                        ),
                        outcome=row["outcome"],
                        guidance_narrative=row["guidance_narrative"],
                        realized_narrative=row["realized_narrative"],
                    )
                )
        return SayDoSection(
            status=SectionStatus.OK,
            verdicts=verdicts,
            verdicts_availability="present" if verdicts else "empty",
            historical_metrics=historical_metrics,
        )
    except (sqlite3.Error, OSError, ValueError, KeyError):
        log.warning({"event": "saydo_verdicts_unavailable", "ticker": ticker})
        return SayDoSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(management_commitments)",
                fix_command="Check the canonical management commitment ledger.",
                detail="Source-linked management commitments could not be read.",
            ),
            verdicts_availability="unavailable",
        )
    finally:
        if conn is None:
            db_conn.close()
