"""Prepare or apply an exact, evidence-bound correction of invalid WIX history.

Preparation is read-only. The resulting private plan contains original records
and tracker evidence; keep it in the private operational artifact directory.
Apply requires that exact plan fingerprint, current source rows, and fresh
same-day evidence. A correction never approves an investment lesson.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

try:
    from _lib import log_event
except ImportError:
    from execution._lib import log_event

from integrations.portfolio_tracker_v1 import PortfolioSnapshotV1, TransactionsV1Result
from provenance.immutable_artifact import publish_text_no_clobber
from run_lock import hold_run_lock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from synthesis.wix_avdv_postmortem import (
    WixHistoryCorrection,
    apply_wix_history_correction,
    prepare_wix_history_correction,
)


class TrackerCorrectionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: PortfolioSnapshotV1
    transaction_pages: tuple[TransactionsV1Result, ...]
    transaction_request_cursors: tuple[str | None, ...]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    modes = parser.add_subparsers(dest="mode", required=True)
    prepare = modes.add_parser("prepare")
    prepare.add_argument("--evidence", type=Path, required=True)
    prepare.add_argument("--entry-id", type=int, required=True)
    prepare.add_argument("--duplicate-entry-id", type=int)
    prepare.add_argument("--note-id", type=int, required=True)
    prepare.add_argument("--decision-id", type=int, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    apply = modes.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--approved-sha256", required=True)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(json.dumps({"status": "hold", "reason": "explicit_existing_database_required"}))
        return 2
    log_event("wix_history_correction_started", mode=args.mode)
    try:
        if args.mode == "prepare":
            evidence = TrackerCorrectionEvidence.model_validate_json(args.evidence.read_text())
            with closing(connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)) as conn:
                plan = prepare_wix_history_correction(
                    conn,
                    entry_id=args.entry_id,
                    note_id=args.note_id,
                    decision_id=args.decision_id,
                    duplicate_entry_id=args.duplicate_entry_id,
                    snapshot=evidence.snapshot,
                    transaction_pages=evidence.transaction_pages,
                    transaction_request_cursors=evidence.transaction_request_cursors,
                    now=datetime.now(UTC),
                )
            publish_text_no_clobber(args.output, plan.model_dump_json(indent=2))
            print(
                json.dumps(
                    {"status": "prepared", "fingerprint": plan.fingerprint(), "applied": False}
                )
            )
        else:
            plan = WixHistoryCorrection.model_validate_json(args.plan.read_text())
            with (
                hold_run_lock(args.db, owner="wix-history-correction"),
                closing(connect_sqlite(args.db, role=SQLiteConnectionRole.WRITER)) as conn,
            ):
                note_id = apply_wix_history_correction(
                    conn, plan, approved_fingerprint=args.approved_sha256
                )
            print(
                json.dumps(
                    {
                        "status": "corrected",
                        "replacement_note_id": note_id,
                        "lesson_state": "owner_review_pending",
                    }
                )
            )
        return 0
    except (OSError, ValueError, LookupError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "hold", "reason": type(exc).__name__, "applied": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
