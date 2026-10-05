"""Plan or build a sealed, evidence-grounded SQLite lexical corpus.

The reporting universe is projected from complete sealed acquisition inventories.
Caller-supplied JSON requires explicit administrative compatibility mode.
Without ``--apply`` this command opens SQLite read-only and emits only a
deterministic plan; its stdout is exactly one JSON result and stderr is JSONL.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtime.job_runtime import JobAlreadyRunningError, JobLock
from search.corpus_builder import (
    ChunkerConfig,
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_coverage_expected_document_inventory,
    load_expected_document_inventory,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    if args.coverage_inventory_keys and args.allow_unsealed_inventory:
        parser.error("--allow-unsealed-inventory only applies to --inventory")
    if args.coverage_inventory_keys and args.knowledge_cutoff is None:
        parser.error("--coverage-inventory-key requires --knowledge-cutoff")
    if args.apply:
        try:
            with JobLock(
                PROJECT_ROOT,
                "build-grounded-search-corpus",
                [
                    "portfolio-db",
                    f"sqlite:{args.db.resolve()}",
                    f"search-corpus:{args.corpus_key}",
                ],
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
        if args.inventory is not None:
            inventory = load_expected_document_inventory(str(args.inventory))
            snapshot_ids: tuple[str, ...] = ()
        else:
            inventory, snapshot_ids = load_coverage_expected_document_inventory(
                conn,
                tuple(args.coverage_inventory_keys),
                knowledge_cutoff=args.knowledge_cutoff,
                observed_through=args.recorded_at,
            )
            _event(
                "grounded_search_reporting_projection",
                knowledge_cutoff=inventory.knowledge_cutoff.isoformat(),
                observed_through=inventory.observed_through.isoformat(),
                source_inventory_snapshot_ids=list(snapshot_ids),
                decisions=[item.model_dump(mode="json") for item in inventory.reporting_decisions],
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
        )
        _event(
            "grounded_search_corpus_started",
            corpus_key=request.corpus_key,
            revision=request.revision,
            mode="apply" if request.apply else "dry_run",
        )
        result = build_grounded_search_corpus(conn, request)
    finally:
        conn.close()
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
