"""Offline single-writer CLI for externalizing leaf evidence nodes and pruning coverage bloat.

Dry-run by default (read-only measurement, zero writes). ``--apply`` acquires
the canonical database write lock (``run_lock.hold_run_lock``) or honors an
inherited ``portfolio-db`` ``JobLock`` and delegates to
``provenance.evidence_compaction.run_compaction``.

Structured JSON events go to stderr; one deterministic JSON receipt goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

try:
    from execution._lib import (
        PROJECT_ROOT,
        add_database_argument,
        command_parser,
        log_event,
        require_db_path,
    )
except ImportError:
    from _lib import (
        PROJECT_ROOT,
        add_database_argument,
        command_parser,
        log_event,
        require_db_path,
    )

from db_paths import default_product_state_root
from provenance.evidence_compaction import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LOCK_TIMEOUT_S,
    CompactionOptions,
    run_compaction,
)
from provenance.evidence_node_store import EvidenceArchiveError
from run_lock import RunLockHeldError


def build_parser() -> argparse.ArgumentParser:
    parser = command_parser(
        description=(
            "Externalize unreferenced leaf evidence_nodes table_cell rows and "
            "optionally prune index_member coverage, inactive filing_sections, "
            "and unreferenced non-accepted legacy fact match revisions."
        )
    )
    add_database_argument(parser)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Product state root for data/derived_evidence archives (default: inferred from db-path)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Measure eligible rows and savings without writing (default)",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Apply archiving, pruning, and optional VACUUM under the DB write lock",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Bounded delete batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--archive-substantive-nodes",
        action="store_true",
        help=(
            "Also externalize unreferenced fulltext-evidence-backfill table_row, table, "
            "and passage nodes (with bottom-up leaf peel and merge into existing archives)"
        ),
    )
    parser.add_argument(
        "--prune-index-member-coverage",
        action="store_true",
        help="Prune expected_documents coverage chains for strictly index_member-only issuers",
    )
    parser.add_argument(
        "--prune-inactive-filing-sections",
        action="store_true",
        help="Prune inactive (is_active=0) and index_member-only filing_sections rows",
    )
    parser.add_argument(
        "--prune-unreferenced-legacy-matches",
        action="store_true",
        help="Prune non-accepted, unreferenced legacy_fact_evidence_match_revisions rows",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Verify foreign keys and integrity, then run VACUUM + ANALYZE (with --apply)",
    )
    parser.add_argument(
        "--lock-timeout-s",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_S,
        help=f"Seconds to wait for the DB write lock (default: {DEFAULT_LOCK_TIMEOUT_S})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        db_path = require_db_path(args.db_path)
        state_root = (
            args.state_root.expanduser().resolve()
            if args.state_root is not None
            else default_product_state_root(PROJECT_ROOT, db_path=db_path)
        )
        options = CompactionOptions(
            db_path=db_path,
            state_root=state_root,
            project_root=PROJECT_ROOT,
            apply=bool(args.apply),
            batch_size=int(args.batch_size),
            archive_substantive_nodes=bool(args.archive_substantive_nodes),
            prune_index_member_coverage=bool(args.prune_index_member_coverage),
            prune_inactive_filing_sections=bool(args.prune_inactive_filing_sections),
            prune_unreferenced_legacy_matches=bool(args.prune_unreferenced_legacy_matches),
            vacuum=bool(args.vacuum),
            lock_timeout_s=float(args.lock_timeout_s),
        )
        receipt = run_compaction(options, log_event=log_event)
    except RunLockHeldError as exc:
        log_event("compact_deferred", reason="run_lock_held", detail=str(exc))
        return 75
    except (
        EvidenceArchiveError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        log_event("compact_failed", error_type=type(exc).__name__, reason=str(exc))
        return 2

    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
