"""Populate governed lexical corpora and prove semantic retrieval readiness."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.population_retrieval_runtime import (  # noqa: E402
    RetrievalRuntimePopulationRequest,
    RetrievalRuntimePopulationResult,
    populate_retrieval_runtime,
)
from runtime.job_runtime import JobLock  # noqa: E402
from search.corpus_builder import ChunkerConfig  # noqa: E402
from search.embedding_promotion import LocalVectorRuntimeConfig  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_parse_datetime, required=True)
    parser.add_argument(
        "--operation-recorded-at",
        "--recorded-at",
        dest="operation_recorded_at",
        type=_parse_datetime,
        required=True,
    )
    parser.add_argument("--phase", choices=("corpus", "qualify", "all"), default="all")
    parser.add_argument("--after-issuer-id")
    parser.add_argument("--max-issuers", type=int)
    parser.add_argument(
        "--selector-code-version",
        default="governed-investor-reporting-selector@1",
    )
    parser.add_argument(
        "--extractor-name",
        action="append",
        dest="extractor_names",
    )
    parser.add_argument("--max-characters", type=int, default=1_200)
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--exact-row-cap", type=int, default=100_000)
    parser.add_argument("--index-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.index_root is None) != (args.runtime_root is None):
        raise SystemExit("--index-root and --runtime-root must be supplied together")
    runtime = (
        None
        if args.index_root is None
        else LocalVectorRuntimeConfig(
            index_root=args.index_root,
            runtime_root=args.runtime_root,
        )
    )
    request = RetrievalRuntimePopulationRequest(
        cutoff_at=args.cutoff_at,
        operation_recorded_at=args.operation_recorded_at,
        apply=bool(args.apply),
        phase=args.phase,
        after_issuer_id=args.after_issuer_id,
        max_issuers=args.max_issuers,
        selector_code_version=args.selector_code_version,
        required_extractor_names=tuple(
            args.extractor_names
            or (
                "fulltext-evidence-backfill",
                "governed-pdf-ocr",
                "governed-image-ocr",
            )
        ),
        chunker=ChunkerConfig(
            max_characters=args.max_characters,
            max_tokens=args.max_tokens,
        ),
        exact_row_cap=args.exact_row_cap,
        input_commitment_sha256=args.input_commitment_sha256,
        plan_commitment_sha256=args.plan_commitment_sha256,
    )
    _event(
        "retrieval_runtime_population_started",
        mode="apply" if request.apply else "dry_run",
        phase=request.phase,
    )
    try:
        result = _execute(args.db, request, runtime=runtime)
    except Exception as exc:
        _event(
            "retrieval_runtime_population_failed",
            error_type=type(exc).__name__,
        )
        return 1
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "retrieval_runtime_population_completed",
        processed_issuers=result.processed_issuer_count,
        ready_issuers=result.ready_issuer_count,
        blocked_issuers=result.failed_issuer_count,
        lexical_manifests=result.lexical_manifest_count,
        vector_projections=result.vector_projection_count,
    )
    if request.phase == "corpus" and set(result.failed_reason_counts) <= {
        "retrieval_qualification_not_run"
    }:
        return 0
    return 0 if result.failed_issuer_count == 0 else 2


def _execute(
    db_path: Path,
    request: RetrievalRuntimePopulationRequest,
    *,
    runtime: LocalVectorRuntimeConfig | None,
) -> RetrievalRuntimePopulationResult:
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY

    def run() -> RetrievalRuntimePopulationResult:
        conn = connect_sqlite(
            db_path,
            role=role,
            schema_preflight=request.apply,
        )
        try:
            return populate_retrieval_runtime(conn, request, runtime=runtime)
        finally:
            conn.close()

    if not request.apply:
        return run()
    with JobLock(
        PROJECT_ROOT,
        "retrieval-runtime-population",
        [
            f"sqlite:{db_path.resolve()}",
            "retrieval-runtime-population",
        ],
    ):
        return run()


if __name__ == "__main__":
    raise SystemExit(main())
