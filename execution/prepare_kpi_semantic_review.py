"""Write a bounded, read-only KPI semantic review queue to an explicit path."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from identity import DEFAULT_USER_ID  # noqa: E402
from operations.kpi_semantic_review_export import (  # noqa: E402
    KPI_SEMANTIC_EXPORT_RELATIVE_ROOT,
    MAX_KPI_SEMANTIC_EXPORT_ITEMS,
    KpiSemanticReviewExport,
    KpiSemanticReviewExportError,
    KpiSemanticReviewExportIndex,
    encoded_kpi_semantic_review_export,
    publish_kpi_semantic_review_exports,
    seal_kpi_semantic_review_export,
)
from operations.paths import configured_product_state_root  # noqa: E402
from operations.review_bundle import (  # noqa: E402
    database_lineage_identity,
    review_code_identity,
)
from pipeline.kpi_semantic_review import (  # noqa: E402
    MAX_KPI_SEMANTIC_REVIEW_ITEMS,
    build_kpi_semantic_review_batch,
)
from pipeline.kpi_semantic_scope import portfolio_tickers  # noqa: E402
from runtime.job_runtime import JobLock, inherited_lock_is_valid  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class KpiSemanticReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi_semantic_review_summary.v1"] = "kpi_semantic_review_summary.v1"
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: int = Field(ge=0)
    output: str = Field(min_length=1)
    state_counts: dict[str, int]
    truncated: bool


OPERATIONS_GOVERNANCE_DISPOSITION = "primary_surface_dynamic_jobs_projection"
OPERATIONS_GOVERNANCE_OWNER = r"\earnings-summary\prepare_kpi_semantic_review"


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
    parser.add_argument("--code-root", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Deprecated code-root alias retained for isolated callers",
    )
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--ticker")
    parser.add_argument("--limit", type=_bounded_limit, default=MAX_KPI_SEMANTIC_EXPORT_ITEMS)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument(
        "--artifact-root",
        type=Path,
        help="Publish bounded immutable ticker partitions plus one atomic complete index",
    )
    destination.add_argument(
        "--publish",
        action="store_true",
        help="Publish to the configured product-state root's fixed semantic-review directory",
    )
    return parser


def _schema_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1 or not str(rows[0][0]).strip():
        raise ValueError("semantic review export requires exactly one schema revision")
    return str(rows[0][0]).strip()


def _identity_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_bounded_review_partition(
    conn: sqlite3.Connection,
    *,
    code_root: Path,
    user_id: str,
    ticker: str,
    requested_limit: int,
    observed_at: datetime,
    code_instance_sha256: str,
    database_instance_sha256: str,
    schema_revision: str,
    partition_ordinal: int,
    after_fact_id: int,
) -> KpiSemanticReviewExport:
    """Build the largest bounded partition that fits the immutable byte contract."""

    if not 0 < requested_limit <= MAX_KPI_SEMANTIC_EXPORT_ITEMS:
        raise ValueError(f"partition limit must be between 1 and {MAX_KPI_SEMANTIC_EXPORT_ITEMS}")
    limit = requested_limit
    while True:
        review = build_kpi_semantic_review_batch(
            conn,
            repo_root=code_root,
            user_id=user_id,
            ticker=ticker,
            limit=limit,
            observed_at=observed_at,
            after_fact_id=after_fact_id,
        )
        next_after_fact_id = review.items[-1].fact_id if review.truncated else None
        export = seal_kpi_semantic_review_export(
            review=review,
            code_instance_sha256=code_instance_sha256,
            database_instance_sha256=database_instance_sha256,
            schema_revision=schema_revision,
            partition_ordinal=partition_ordinal,
            after_fact_id=after_fact_id,
            next_after_fact_id=next_after_fact_id,
        )
        try:
            encoded_kpi_semantic_review_export(export)
        except KpiSemanticReviewExportError:
            if limit == 1:
                raise
            limit = max(1, limit // 2)
            continue
        return export


def build_ticker_review_exports(
    conn: sqlite3.Connection,
    *,
    code_root: Path,
    user_id: str,
    ticker: str,
    requested_limit: int,
    observed_at: datetime,
    code_instance_sha256: str,
    database_instance_sha256: str,
    schema_revision: str,
) -> tuple[KpiSemanticReviewExport, ...]:
    """Traverse one ticker's complete keyset queue into bounded partitions."""

    exports: list[KpiSemanticReviewExport] = []
    after_fact_id = 0
    partition_ordinal = 0
    while True:
        export = build_bounded_review_partition(
            conn,
            code_root=code_root,
            user_id=user_id,
            ticker=ticker,
            requested_limit=requested_limit,
            observed_at=observed_at,
            code_instance_sha256=code_instance_sha256,
            database_instance_sha256=database_instance_sha256,
            schema_revision=schema_revision,
            partition_ordinal=partition_ordinal,
            after_fact_id=after_fact_id,
        )
        exports.append(export)
        if export.next_after_fact_id is None:
            return tuple(exports)
        after_fact_id = export.next_after_fact_id
        partition_ordinal += 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.db.resolve()
    if args.code_root is not None and args.repo_root is not None:
        raise ValueError("pass --code-root or the deprecated --repo-root alias, not both")
    code_root = (args.code_root or args.repo_root or PROJECT_ROOT).resolve()
    output = None if args.output is None else args.output.resolve()
    artifact_root = None if args.artifact_root is None else args.artifact_root.resolve()
    state_root: Path | None = None
    if args.publish:
        state_root = configured_product_state_root(code_root)
        expected_database = (state_root / "data" / "portfolio.db").resolve()
        if database != expected_database:
            raise ValueError("configured product-state database does not match --db")
        artifact_root = (state_root / KPI_SEMANTIC_EXPORT_RELATIVE_ROOT).resolve()
    if output == database or artifact_root == database:
        raise ValueError("review output must not overwrite the source database")
    batch = None
    index: KpiSemanticReviewExportIndex | None = None
    exports: tuple[KpiSemanticReviewExport, ...] = ()
    resources = ExitStack()
    if state_root is not None and not inherited_lock_is_valid(
        state_root, "kpi-semantic-review-export"
    ):
        resources.enter_context(
            JobLock(
                state_root,
                "prepare-kpi-semantic-review-publish",
                ["kpi-semantic-review-export"],
                wait_s=0,
            )
        )
    try:
        conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    except Exception:
        resources.close()
        raise
    try:
        # Hold one WAL snapshot across identity, roster, source evidence, and
        # semantic-head reads so a concurrent writer cannot produce a mixed
        # observation while this producer remains strictly read-only.
        conn.execute("BEGIN")
        if artifact_root is None:
            batch = build_kpi_semantic_review_batch(
                conn,
                repo_root=code_root,
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
            code_instance = _identity_sha256(review_code_identity(code_root))
            database_instance = _identity_sha256(database_lineage_identity(conn))
            schema_revision = _schema_revision(conn)
            tickers = portfolio_tickers(conn, user_id=args.user_id)
            built_exports: list[KpiSemanticReviewExport] = []
            for ticker in tickers:
                built_exports.extend(
                    build_ticker_review_exports(
                        conn,
                        code_root=code_root,
                        user_id=args.user_id,
                        ticker=ticker,
                        requested_limit=args.limit,
                        observed_at=observed_at,
                        code_instance_sha256=code_instance,
                        database_instance_sha256=database_instance,
                        schema_revision=schema_revision,
                    )
                )
            exports = tuple(built_exports)
            index = publish_kpi_semantic_review_exports(
                root=artifact_root,
                exports=exports,
                expected_tickers=tickers,
            )
    finally:
        conn.close()
        resources.close()
    if artifact_root is not None:
        assert index is not None
        summary = KpiSemanticReviewSummary(
            content_sha256=index.content_sha256,
            items=index.total_items,
            output=str(artifact_root / "latest.json"),
            state_counts=index.state_counts,
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
