"""Write a bounded, read-only KPI semantic review queue to an explicit path."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from operations.kpi_semantic_review_export import (  # noqa: E402
    MAX_KPI_SEMANTIC_EXPORT_ITEMS,
    KpiSemanticReviewExport,
    KpiSemanticReviewExportIndex,
    publish_kpi_semantic_review_exports,
    seal_kpi_semantic_review_export,
)
from operations.review_bundle import (  # noqa: E402
    database_lineage_identity,
    review_code_identity,
)
from pipeline.kpi_semantic_review import (  # noqa: E402
    MAX_KPI_SEMANTIC_REVIEW_ITEMS,
    build_kpi_semantic_review_batch,
)
from pipeline.kpi_semantic_scope import portfolio_tickers  # noqa: E402
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
    parser.add_argument("--limit", type=_bounded_limit, default=MAX_KPI_SEMANTIC_EXPORT_ITEMS)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument(
        "--artifact-root",
        type=Path,
        help="Publish one immutable artifact per portfolio ticker plus an atomic latest index",
    )
    return parser


def _schema_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1 or not str(rows[0][0]).strip():
        raise ValueError("semantic review export requires exactly one schema revision")
    return str(rows[0][0]).strip()


def _identity_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.db.resolve()
    output = None if args.output is None else args.output.resolve()
    artifact_root = None if args.artifact_root is None else args.artifact_root.resolve()
    if output == database or artifact_root == database:
        raise ValueError("review output must not overwrite the source database")
    batch = None
    index: KpiSemanticReviewExportIndex | None = None
    exports: tuple[KpiSemanticReviewExport, ...] = ()
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        if artifact_root is None:
            batch = build_kpi_semantic_review_batch(
                conn,
                repo_root=args.repo_root,
                user_id=args.user_id,
                ticker=args.ticker,
                limit=args.limit,
            )
        else:
            if args.ticker is not None:
                raise ValueError("artifact publication always covers the complete owner portfolio")
            if args.limit > MAX_KPI_SEMANTIC_EXPORT_ITEMS:
                raise ValueError(
                    f"artifact publication limit must not exceed {MAX_KPI_SEMANTIC_EXPORT_ITEMS}"
                )
            observed_at = datetime.now(UTC)
            code_instance = _identity_sha256(review_code_identity(args.repo_root.resolve()))
            database_instance = _identity_sha256(database_lineage_identity(conn))
            schema_revision = _schema_revision(conn)
            exports = tuple(
                seal_kpi_semantic_review_export(
                    review=build_kpi_semantic_review_batch(
                        conn,
                        repo_root=args.repo_root,
                        user_id=args.user_id,
                        ticker=ticker,
                        limit=args.limit,
                        observed_at=observed_at,
                    ),
                    code_instance_sha256=code_instance,
                    database_instance_sha256=database_instance,
                    schema_revision=schema_revision,
                )
                for ticker in portfolio_tickers(conn, user_id=args.user_id)
            )
            index = publish_kpi_semantic_review_exports(root=artifact_root, exports=exports)
    finally:
        conn.close()
    if artifact_root is not None:
        assert index is not None
        summary = KpiSemanticReviewSummary(
            content_sha256=index.content_sha256,
            items=sum(export.review.total_items for export in exports),
            output=str(artifact_root / "latest.json"),
            state_counts={
                state: sum(export.review.state_counts.get(state, 0) for export in exports)
                for state in sorted(
                    {state for export in exports for state in export.review.state_counts}
                )
            },
            truncated=False,
        )
        print(summary.model_dump_json())
        print(
            json.dumps(
                {"event": "kpi_semantic_review_published", **summary.model_dump(mode="json")},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 0
    assert output is not None
    assert batch is not None
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
