"""Recompute or atomically seal one full-universe population cutover."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.population_completeness import (  # noqa: E402
    PopulationTemporalScope,
    canonical_json,
    digest_text,
)
from provenance.population_cutover import (  # noqa: E402
    PopulationCutoverBlockedError,
    PopulationCutoverEvaluationRequest,
    evaluate_population_cutover,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def _failure_receipt(
    request: PopulationCutoverEvaluationRequest | None,
    *,
    stage: str,
    error_type: str,
    detail: str,
) -> str:
    payload = {
        "error_type": error_type,
        "mode": "unknown" if request is None else ("apply" if request.apply else "dry_run"),
        "policy_config_sha256": None if request is None else request.policy_config_sha256,
        "source_snapshot_sha256": None if request is None else request.source_snapshot_sha256,
        "stage": stage,
        "status": "blocked",
        "temporal_scope": (
            None if request is None else request.temporal_scope.model_dump(mode="json")
        ),
        "detail": detail,
    }
    return canonical_json({**payload, "receipt_sha256": digest_text(canonical_json(payload))})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--knowledge-cutoff", type=datetime.fromisoformat, required=True)
    parser.add_argument("--observed-through", type=datetime.fromisoformat, required=True)
    parser.add_argument("--policy-config-sha256", required=True)
    parser.add_argument("--source-snapshot-sha256", required=True)
    parser.add_argument("--evaluated-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--sealed-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--parity-page-size", type=int, default=1_000)
    parser.add_argument("--parity-max-pages", type=int, default=10_000)
    parser.add_argument("--parity-max-rows-per-issuer", type=int, default=2_000_000)
    parser.add_argument("--audit-sample-limit", type=int, default=20)
    parser.add_argument("--audit-fetch-size", type=int, default=250)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    request: PopulationCutoverEvaluationRequest | None = None
    conn: sqlite3.Connection | None = None
    try:
        request = PopulationCutoverEvaluationRequest(
            temporal_scope=PopulationTemporalScope(
                knowledge_cutoff=args.knowledge_cutoff,
                observed_through=args.observed_through,
            ),
            policy_config_sha256=args.policy_config_sha256,
            source_snapshot_sha256=args.source_snapshot_sha256,
            evaluated_at=args.evaluated_at,
            sealed_at=args.sealed_at,
            apply=args.apply,
            parity_page_size=args.parity_page_size,
            parity_max_pages=args.parity_max_pages,
            parity_max_rows_per_issuer=args.parity_max_rows_per_issuer,
            audit_sample_limit=args.audit_sample_limit,
            audit_fetch_size=args.audit_fetch_size,
        )
        _event(
            "population_cutover_evaluation_started",
            mode="apply" if request.apply else "dry_run",
            knowledge_cutoff=request.temporal_scope.knowledge_cutoff.isoformat(),
            observed_through=request.temporal_scope.observed_through.isoformat(),
        )
        conn = connect_sqlite(
            args.db_path,
            role=(SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY),
            schema_preflight=request.apply,
        )
        result = evaluate_population_cutover(conn, request)
        if request.apply:
            conn.commit()
    except (
        PopulationCutoverBlockedError,
        ValidationError,
        ValueError,
        RuntimeError,
        sqlite3.DatabaseError,
    ) as exc:
        if conn is not None:
            conn.rollback()
        stage = exc.stage if isinstance(exc, PopulationCutoverBlockedError) else "evaluation"
        print(
            _failure_receipt(
                request,
                stage=stage,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
        )
        _event(
            "population_cutover_evaluation_blocked",
            error_type=type(exc).__name__,
            stage=stage,
        )
        return 2
    finally:
        if conn is not None:
            conn.close()
    print(result.model_dump_json())
    _event(
        "population_cutover_evaluation_finished",
        evaluation_sha256=result.evaluation_sha256,
        status=result.status,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
