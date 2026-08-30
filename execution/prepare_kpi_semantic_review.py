"""Write a bounded, read-only KPI semantic review queue to an explicit path."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.kpi_semantic_review import (  # noqa: E402
    MAX_KPI_SEMANTIC_REVIEW_ITEMS,
    build_kpi_semantic_review_batch,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class KpiSemanticReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi_semantic_review_summary.v1"] = "kpi_semantic_review_summary.v1"
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: int = Field(ge=0)
    output: str = Field(min_length=1)
    state_counts: dict[str, int]
    truncated: bool


def _bounded_limit(value: str) -> int:
    parsed = int(value)
    if not 0 < parsed <= MAX_KPI_SEMANTIC_REVIEW_ITEMS:
        raise argparse.ArgumentTypeError(
            f"limit must be between 1 and {MAX_KPI_SEMANTIC_REVIEW_ITEMS}"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--ticker")
    parser.add_argument("--limit", type=_bounded_limit, default=MAX_KPI_SEMANTIC_REVIEW_ITEMS)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.db.resolve()
    output = args.output.resolve()
    if output == database:
        raise ValueError("review output must not overwrite the source database")
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        batch = build_kpi_semantic_review_batch(
            conn,
            repo_root=args.repo_root,
            user_id=args.user_id,
            ticker=args.ticker,
            limit=args.limit,
        )
    finally:
        conn.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(batch.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    summary = KpiSemanticReviewSummary(
        content_sha256=batch.content_sha256,
        items=batch.total_items,
        output=str(output),
        state_counts=batch.state_counts,
        truncated=batch.truncated,
    )
    print(summary.model_dump_json())
    print(
        json.dumps(
            {"event": "kpi_semantic_review_prepared", **summary.model_dump(mode="json")},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
