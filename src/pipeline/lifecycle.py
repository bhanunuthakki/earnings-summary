"""Pipeline lifecycle, retention policies, and safe database pruning helpers.

Enforces explicit retention rules across ephemeral artifacts, IR documents,
and derived outputs. Provides SafeRowPruner with dry-run default, threshold gates,
and restorable JSON audit receipts.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict


class RetentionClass(StrEnum):
    EPHEMERAL_STAGING = "ephemeral_staging"  # .tmp/ files: 18h default TTL
    INTERMEDIATE_BACKTEST = "intermediate_backtest"  # .tmp/backtests: 7d TTL
    DERIVED_OUTPUT = "derived_output"  # outputs/: 30d TTL or generation-bound
    ADMITTED_IR_CORPUS = "admitted_ir_corpus"  # ir_documents/: immutable
    SEALED_CANARY_CORPUS = "sealed_canary_corpus"  # data/historical/fmp: immutable
    DATABASE_BACKUP = "database_backup"  # data/*.db snapshots: 14d default TTL


@dataclass(frozen=True)
class RetentionPolicy:
    retention_class: RetentionClass
    default_ttl_hours: int | None
    is_immutable: bool
    description: str


RETENTION_MATRIX: dict[RetentionClass, RetentionPolicy] = {
    RetentionClass.EPHEMERAL_STAGING: RetentionPolicy(
        retention_class=RetentionClass.EPHEMERAL_STAGING,
        default_ttl_hours=18,
        is_immutable=False,
        description="Ephemeral pipeline staging, lock files, and raw response dumps",
    ),
    RetentionClass.INTERMEDIATE_BACKTEST: RetentionPolicy(
        retention_class=RetentionClass.INTERMEDIATE_BACKTEST,
        default_ttl_hours=168,  # 7 days
        is_immutable=False,
        description="Intermediate evaluation runs and backtest scratch files",
    ),
    RetentionClass.DERIVED_OUTPUT: RetentionPolicy(
        retention_class=RetentionClass.DERIVED_OUTPUT,
        default_ttl_hours=720,  # 30 days
        is_immutable=False,
        description="Generated reports and dashboard HTML/JSON artifacts",
    ),
    RetentionClass.ADMITTED_IR_CORPUS: RetentionPolicy(
        retention_class=RetentionClass.ADMITTED_IR_CORPUS,
        default_ttl_hours=None,
        is_immutable=True,
        description="Governed issuer investor relations filings, transcripts, and decks",
    ),
    RetentionClass.SEALED_CANARY_CORPUS: RetentionPolicy(
        retention_class=RetentionClass.SEALED_CANARY_CORPUS,
        default_ttl_hours=None,
        is_immutable=True,
        description="Sealed comparison cache oracle files for foreign filers",
    ),
    RetentionClass.DATABASE_BACKUP: RetentionPolicy(
        retention_class=RetentionClass.DATABASE_BACKUP,
        default_ttl_hours=336,  # 14 days
        is_immutable=False,
        description="Timestamped SQLite snapshot files",
    ),
}


class PruneReceipt(BaseModel):
    """Immutable receipt emitted before and after any row pruning operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    target_table: str
    dry_run: bool
    rows_matched: int
    rows_deleted: int
    threshold_limit: int
    executed_at: datetime
    predicate_sql: str
    restorable_snapshot_json: str | None = None


class SafeRowPruner:
    """Safe row deletion and pruning helper with dry-run default and safety tripwires."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str | None = None,
        max_rows_threshold: int = 1000,
    ) -> None:
        self.conn = conn
        self.run_id = run_id or f"prune_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        self.max_rows_threshold = max_rows_threshold

    def prune_rows(
        self,
        *,
        table_name: str,
        where_clause: str | None = None,
        filters: dict[str, Any] | None = None,
        params: Sequence[Any] = (),
        dry_run: bool = True,
        save_snapshot: bool = True,
    ) -> PruneReceipt:
        """Evaluate matching rows, enforce safety gates, and delete conditionally."""
        # Sanitize table_name to protect against SQL injection
        if not table_name.isidentifier():
            raise ValueError(f"Invalid table name: {table_name}")

        final_where = ""
        final_params: list[Any] = []

        if filters is not None:
            clauses: list[str] = []
            for col, val in filters.items():
                if not str(col).isidentifier():
                    raise ValueError(f"Invalid column identifier in filter: {col}")
                clauses.append(f'"{col}" = ?')
                final_params.append(val)
            final_where = " AND ".join(clauses)
        elif where_clause is not None:
            # Enforce strict syntax guard against comment, semicolon, and isolated DDL/DML keywords
            if any(sym in where_clause for sym in (";", "--", "/*", "*/")):
                raise ValueError(
                    f"Forbidden comment or delimiter symbol in where_clause: {where_clause}"
                )
            import re

            match = re.search(
                r"\b(union|drop|delete|insert|update|exec)\b", where_clause, re.IGNORECASE
            )
            if match:
                raise ValueError(
                    f"Forbidden SQL keyword '{match.group(0)}' in pruning where_clause: {where_clause}"
                )
            final_where = where_clause
            final_params.extend(params)
        else:
            raise ValueError("Must provide either 'filters' dict or sanitized 'where_clause'")

        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()

        # 1. Select matching rows
        select_sql = f"SELECT * FROM {table_name} WHERE {final_where}"  # nosec B608
        cursor.execute(select_sql, final_params)
        matched_rows = cursor.fetchall()
        matched_count = len(matched_rows)

        # 2. Enforce safety threshold
        if matched_count > self.max_rows_threshold:
            raise ValueError(
                f"Pruning tripwire exceeded: matched {matched_count} rows, "
                f"exceeding max limit of {self.max_rows_threshold} rows"
            )

        # 3. Snapshot restorable rows
        snapshot_json: str | None = None
        if save_snapshot and matched_rows:
            dict_rows = [dict(r) for r in matched_rows]
            snapshot_json = json.dumps(dict_rows, default=str)

        # 4. Perform deletion if not dry_run inside transaction
        deleted_count = 0
        if not dry_run and matched_count > 0:
            with self.conn:
                delete_sql = f"DELETE FROM {table_name} WHERE {final_where}"  # nosec B608
                cursor.execute(delete_sql, final_params)
                deleted_count = cursor.rowcount

        return PruneReceipt(
            run_id=self.run_id,
            target_table=table_name,
            dry_run=dry_run,
            rows_matched=matched_count,
            rows_deleted=deleted_count,
            threshold_limit=self.max_rows_threshold,
            executed_at=datetime.now(UTC),
            predicate_sql=final_where,
            restorable_snapshot_json=snapshot_json,
        )

    def restore_from_receipt(self, receipt: PruneReceipt) -> int:
        """Restore rows from a PruneReceipt snapshot atomically."""
        if not receipt.restorable_snapshot_json:
            return 0

        raw_rows: object = json.loads(receipt.restorable_snapshot_json)
        if not isinstance(raw_rows, list) or not raw_rows:
            return 0

        raw_list = cast("list[object]", raw_rows)
        sample_raw: object = raw_list[0]
        if not isinstance(sample_raw, dict):
            return 0
        sample: dict[str, object] = cast("dict[str, object]", sample_raw)
        rows = cast("list[dict[str, object]]", raw_rows)

        cols: list[str] = [str(k) for k in sample]
        for col in cols:
            if not col.isidentifier():
                raise ValueError(f"Invalid column identifier in snapshot: {col}")

        table_name = receipt.target_table
        if not table_name.isidentifier():
            raise ValueError(f"Invalid target table: {table_name}")

        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(f'"{c}"' for c in cols)
        sql = f"INSERT OR REPLACE INTO {table_name} ({col_names}) VALUES ({placeholders})"

        cursor = self.conn.cursor()
        with self.conn:
            for r in rows:
                cursor.execute(sql, [r.get(c) for c in cols])
        return len(rows)
