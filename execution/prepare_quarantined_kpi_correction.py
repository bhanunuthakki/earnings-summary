"""Seal one read-only review export for an exact quarantined KPI predecessor."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from identity import DEFAULT_USER_ID  # noqa: E402
from operations.kpi_semantic_review_export import (  # noqa: E402
    encoded_kpi_semantic_review_export,
    seal_kpi_semantic_review_export,
)
from operations.review_bundle import (  # noqa: E402
    database_lineage_identity,
    review_code_identity,
)
from pipeline.kpi_semantic_review import (  # noqa: E402
    build_quarantined_kpi_correction_review,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

OPERATIONS_GOVERNANCE_DISPOSITION = "no_surface_change_bounded_read_only_kpi_correction_review"
OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT = (
    "src/operations/registry.py:OperationsRegistry",
    "src/pipeline/operations_panel.py:visible_surface_dispositions",
)


def _schema_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1 or not str(rows[0][0]).strip():
        raise ValueError("correction review requires exactly one schema revision")
    return str(rows[0][0]).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--fact-id", type=int, required=True)
    parser.add_argument("--source-value-text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.db.resolve()
    code_root = args.code_root.resolve()
    output = args.output.resolve()
    if output == database:
        raise ValueError("review output must not overwrite the database")
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.execute("BEGIN")
        observed_at = datetime.now(UTC)
        review = build_quarantined_kpi_correction_review(
            conn,
            repo_root=code_root,
            user_id=args.user_id,
            fact_id=args.fact_id,
            source_value_text=args.source_value_text,
            observed_at=observed_at,
        )
        export = seal_kpi_semantic_review_export(
            review=review,
            code_instance_sha256=hashlib.sha256(
                review_code_identity(code_root).encode("utf-8")
            ).hexdigest(),
            database_instance_sha256=hashlib.sha256(
                database_lineage_identity(conn).encode("utf-8")
            ).hexdigest(),
            schema_revision=_schema_revision(conn),
        )
    finally:
        conn.close()
    encoded = encoded_kpi_semantic_review_export(export)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + f".{uuid4().hex}.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(output)
    print(export.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
