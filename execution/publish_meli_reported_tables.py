"""Publish three source-reported MELI H1 table rows from captured native evidence.

Dry-run is default. This does not bind canonical metrics, promote valuation
inputs, or declare complete filing extraction. Missing members remain rejected.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

try:  # direct script invocation
    from _lib import command_parser
except ImportError:  # package import
    from execution._lib import command_parser

from provenance.analysis_scope import AnalysisEvidenceScope
from provenance.immutable_artifact import read_stable_artifact
from provenance.meli_reported_tables import ReportedTableRequest, publish_meli_reported_tables
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class _PublicationResult(Protocol):
    @property
    def source_population_complete(self) -> bool: ...

    def model_dump_json(self) -> str: ...


def main(
    argv: list[str] | None = None,
    *,
    publisher: Callable[[sqlite3.Connection, ReportedTableRequest], _PublicationResult]
    | None = None,
    description: str | None = None,
) -> int:
    # Both displayed-source recipes share one CLI request and DB-role boundary.
    parser = command_parser(__doc__ if description is None else description)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inventory-key", required=True)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--document-version-id", required=True)
    parser.add_argument("--fulltext-run-id", required=True)
    parser.add_argument("--content-root", type=Path, action="append", required=True)
    parser.add_argument("--analysis-scope", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        request = ReportedTableRequest(
            inventory_key=args.inventory_key,
            accession_number=args.accession,
            document_version_id=args.document_version_id,
            fulltext_run_id=args.fulltext_run_id,
            content_roots=tuple(args.content_root),
            recorded_at=datetime.now(UTC),
            apply=args.apply,
            analysis_scope=(
                None
                if args.analysis_scope is None
                else AnalysisEvidenceScope.model_validate_json(
                    read_stable_artifact(args.analysis_scope, max_bytes=16 * 1024 * 1024)[1]
                )
            ),
        )
        conn = connect_sqlite(
            args.db,
            role=SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY,
            schema_preflight=request.apply,
        )
        result = (publish_meli_reported_tables if publisher is None else publisher)(conn, request)
        sys.stdout.write(result.model_dump_json() + "\n")
        return 0 if result.source_population_complete else 2
    except ValidationError:
        sys.stdout.write(
            json.dumps(
                {
                    "outcome": "unavailable",
                    "reason": "invalid publication request or analysis scope",
                }
            )
            + "\n"
        )
        return 3
    except (ValueError, LookupError, OSError, RuntimeError, sqlite3.Error) as error:
        # A typed unavailable result cannot be mistaken for an empty population.
        sys.stdout.write(json.dumps({"outcome": "unavailable", "reason": str(error)}) + "\n")
        return 3
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
