"""execution/get_company_say_do.py
--------------------------------
Single-purpose CLI returning a company's historical Say/Do tracking,
management commitments, guidance falsifiers, and post-earnings readouts.

Usage:
    python execution/get_company_say_do.py --ticker NU
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from db_paths import configured_db_path  # noqa: E402
from llm.postprocess import strip_inline_markdown  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from ticker_validation import safe_ticker  # noqa: E402


class SayDoCommitment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    period: str | None = None
    statement: str
    category: Literal["guidance", "milestone", "thesis_driver", "falsifier"] = "milestone"
    status: Literal["delivered", "in_progress", "missed", "evaluating"] = "evaluating"
    reported_actual: str | None = None
    source_ref: str | None = None
    as_of: str | None = None


class CompanySayDoResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    commitments: list[SayDoCommitment] = Field(default_factory=lambda: list[SayDoCommitment]())
    total_commitments: int = 0
    delivered_count: int = 0
    missed_count: int = 0
    in_progress_count: int = 0
    readout_summary: str | None = None
    as_of: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def load_company_say_do(db_path: Path, ticker: str) -> CompanySayDoResponse:
    ticker = safe_ticker(ticker)
    if not db_path.exists():
        return CompanySayDoResponse(ticker=ticker)

    commitments: list[SayDoCommitment] = []
    readout_summary: str | None = None

    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return CompanySayDoResponse(ticker=ticker)

    try:
        # 1. Query decisions table for falsifiable conditions & milestones
        cursor = conn.execute(
            """
            SELECT id, fiscal_period, action_memo_md, decision_conditions,
                   conditions_extracted_at, created_at
            FROM decisions
            WHERE UPPER(ticker) = ?
            ORDER BY created_at DESC
            LIMIT 25
            """,
            (ticker,),
        )
        rows = cursor.fetchall()
        for r in rows:
            decision_id = int(r["id"])
            period = str(r["fiscal_period"]) if r["fiscal_period"] else None
            memo = str(r["action_memo_md"]) if r["action_memo_md"] else ""
            created_at = str(r["created_at"]) if r["created_at"] else None

            # Check decision conditions
            raw_conds = r["decision_conditions"]
            if raw_conds:
                try:
                    cond_list = cast("object", json.loads(str(raw_conds)))
                    if isinstance(cond_list, list):
                        for idx, cond in enumerate(cast("list[object]", cond_list)):
                            if isinstance(cond, dict):
                                condition = cast("dict[str, object]", cond)
                                note = (
                                    condition.get("note")
                                    or condition.get("metric")
                                    or "Milestone condition"
                                )
                                commitments.append(
                                    SayDoCommitment(
                                        id=decision_id * 100 + idx,
                                        period=period,
                                        statement=strip_inline_markdown(str(note)),
                                        category="falsifier",
                                        status="in_progress",
                                        source_ref=f"Decision #{decision_id}",
                                        as_of=created_at,
                                    )
                                )
                except (json.JSONDecodeError, TypeError):
                    pass

            if memo and len(memo) > 10 and not raw_conds:
                first_line = strip_inline_markdown(memo.strip().split("\n")[0])[:120]
                commitments.append(
                    SayDoCommitment(
                        id=decision_id,
                        period=period,
                        statement=first_line,
                        category="thesis_driver",
                        status="delivered",
                        source_ref=f"Decision #{decision_id}",
                        as_of=created_at,
                    )
                )

        # 2. Query latest post-earnings readout artifact for context
        readout_row = conn.execute(
            """
            SELECT body_md FROM llm_artifacts
            WHERE UPPER(ticker) = ? AND purpose = 'post_earnings_readout'
            ORDER BY created_at DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if readout_row and readout_row["body_md"]:
            readout_summary = strip_inline_markdown(str(readout_row["body_md"]))[:300]

    except sqlite3.Error:
        pass
    finally:
        conn.close()

    delivered = sum(1 for c in commitments if c.status == "delivered")
    missed = sum(1 for c in commitments if c.status == "missed")
    in_progress = sum(1 for c in commitments if c.status in {"in_progress", "evaluating"})

    return CompanySayDoResponse(
        ticker=ticker,
        commitments=commitments,
        total_commitments=len(commitments),
        delivered_count=delivered,
        missed_count=missed,
        in_progress_count=in_progress,
        readout_summary=readout_summary,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Stock ticker (e.g. NU)")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    db_path = configured_db_path(args.repo_root.resolve())
    response = load_company_say_do(db_path, args.ticker)
    print(response.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
