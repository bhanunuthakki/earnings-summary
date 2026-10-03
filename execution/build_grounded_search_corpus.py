"""Plan or build a sealed, evidence-grounded SQLite lexical corpus.

The expected reporting universe comes from sealed coverage or a verified analysis
scope. A caller-supplied JSON inventory requires explicit administrative opt-in.
Without ``--apply`` this command opens SQLite read-only and emits only a
deterministic plan; its stdout is exactly one JSON result and stderr is JSONL.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from provenance.analysis_scope import AnalysisEvidenceScope
from provenance.immutable_artifact import (
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    read_stable_artifact,
    require_canonical_text_artifact,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from search.corpus_builder import (
    ChunkerConfig,
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_analysis_expected_document_inventory,
    load_coverage_expected_document_inventory,
    load_expected_document_inventory,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
    inventory_group = parser.add_mutually_exclusive_group(required=True)
    inventory_group.add_argument(
        "--inventory",
        type=Path,
        help="Closed JSON expected-document inventory (requires explicit unsafe opt-in)",
    )
    inventory_group.add_argument(
        "--analysis-scope",
        type=Path,
        help="Verified analysis evidence scope receipt; retain archive gaps outside this scope",
    )
    inventory_group.add_argument(
        "--coverage-inventory-key",
        action="append",
        dest="coverage_inventory_keys",
        help="Complete sealed source inventory key; repeat to union reporting universes",
    )
    parser.add_argument(
        "--allow-unsealed-inventory",
        action="store_true",
        help="Administrative compatibility mode for caller-supplied JSON inventories",
    )
    parser.add_argument("--corpus-key", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--selector-code-version", required=True)
    parser.add_argument("--recorded-at", type=_parse_datetime, required=True)
    parser.add_argument("--knowledge-cutoff", type=_parse_datetime)
    parser.add_argument(
        "--extractor-name",
        action="append",
        dest="extractor_names",
        help=(
            "Approved complete extraction profile; repeat to allow multiple. "
            "Defaults to native full-text plus governed PDF OCR."
        ),
    )
    parser.add_argument("--max-characters", type=int, default=1_200)
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--persist-batch-size", type=int, default=250)
    parser.add_argument(
        "--apply", action="store_true", help="Persist one immutable corpus revision"
    )
    args = parser.parse_args(argv)

    if args.inventory is not None and not args.allow_unsealed_inventory:
        parser.error("--inventory requires --allow-unsealed-inventory")
    if args.inventory is None and args.allow_unsealed_inventory:
        parser.error("--allow-unsealed-inventory only applies to --inventory")
    if args.apply:
        resources = [
            "portfolio-db",
            f"sqlite:{args.db.resolve()}",
            f"search-corpus:{args.corpus_key}",
        ]
        if args.analysis_scope is not None:
            resources.append(f"artifact:{args.analysis_scope.resolve()}")
        try:
            with JobLock(
                PROJECT_ROOT,
                "build-grounded-search-corpus",
                resources,
            ):
                return _run(args)
        except JobAlreadyRunningError as exc:
            _event("grounded_search_corpus_locked", detail=str(exc))
            return 75
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=args.apply)
    try:
        return _run_connected(args, conn)
    finally:
        conn.close()


def _run_connected(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    analysis_scope: AnalysisEvidenceScope | None = None
    scope_artifact: ImmutableArtifactSnapshot | None = None
    if args.inventory is not None:
        inventory = load_expected_document_inventory(str(args.inventory))
        snapshot_ids: tuple[str, ...] = ()
    elif args.analysis_scope is not None:
        scope_artifact, scope_payload = read_stable_artifact(args.analysis_scope)
        analysis_scope = AnalysisEvidenceScope.model_validate_json(scope_payload)
        require_canonical_text_artifact(scope_artifact, analysis_scope.model_dump_json())
        inventory, snapshot_ids = load_analysis_expected_document_inventory(
            conn,
            analysis_scope,
            cutoff_at=args.knowledge_cutoff or args.recorded_at,
            observed_through=args.recorded_at,
        )
    else:
        inventory, snapshot_ids = load_coverage_expected_document_inventory(
            conn, tuple(args.coverage_inventory_keys)
        )
    request = CorpusBuildRequest(
        corpus_key=args.corpus_key,
        revision=args.revision,
        selector_code_version=args.selector_code_version,
        recorded_at=args.recorded_at,
        knowledge_cutoff=args.knowledge_cutoff,
        expected_documents=inventory.expected_documents,
        source_inventory_snapshot_ids=snapshot_ids,
        chunker=ChunkerConfig(max_characters=args.max_characters, max_tokens=args.max_tokens),
        persist_batch_size=args.persist_batch_size,
        required_extractor_names=tuple(
            args.extractor_names
            or (
                "fulltext-evidence-backfill",
                "governed-pdf-ocr",
                "governed-image-ocr",
            )
        ),
        apply=args.apply,
        analysis_scope=analysis_scope,
    )
    _event(
        "grounded_search_corpus_started",
        corpus_key=request.corpus_key,
        revision=request.revision,
        mode="apply" if request.apply else "dry_run",
    )

    def verify_scope_artifact() -> None:
        if scope_artifact is not None:
            assert_artifact_unchanged(scope_artifact)

    verify_scope_artifact()
    result = build_grounded_search_corpus(
        conn,
        request,
        before_publish=None if scope_artifact is None else verify_scope_artifact,
    )
    if not request.apply:
        verify_scope_artifact()
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "grounded_search_corpus_finished",
        manifest_id=result.manifest_id,
        chunks_planned=result.chunks_planned,
        completion_status=result.completion_status,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
