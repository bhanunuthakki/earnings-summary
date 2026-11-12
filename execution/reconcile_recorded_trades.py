"""Reconcile approved private owner requests against stored broker activity."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

try:
    from _lib import add_database_argument, command_parser, log_event, require_db_path
except ImportError:  # pragma: no cover - package import
    from execution._lib import add_database_argument, command_parser, log_event, require_db_path

from compute.evidence_snapshot import capture_snapshot, hold_windows_evidence_snapshot
from integrations.portfolio_tracker_v1 import (
    AccountsV1Result,
    TrackerV1Client,
    TransactionsV1Result,
    TransactionV1,
    V1Fetch,
    V1Meta,
)
from research.owner_reconciliation import (
    ReconciliationRequest,
    apply_reconciliation,
    evaluate_request,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock

OPERATIONS_GOVERNANCE_DISPOSITION = "primary_surface_dynamic_jobs_projection"
OPERATIONS_GOVERNANCE_OWNER = r"\earnings-summary\reconcile-recorded-trades"


class TransactionReader(Protocol):
    def get_accounts(self) -> V1Fetch[AccountsV1Result]: ...

    def get_transactions_page(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> V1Fetch[TransactionsV1Result]: ...


def _snapshot_identity(meta: V1Meta) -> str:
    return json.dumps(
        meta.model_dump(mode="json", exclude={"generated_at", "links"}), sort_keys=True
    )


def collect_transactions(
    client: TransactionReader, *, start: date, end: date
) -> tuple[list[TransactionV1], V1Meta, int]:
    """Traverse the window completely and reject envelope drift between pages."""
    if start > end:
        raise ValueError("invalid_query_window")
    cursor: str | None = None
    cursors: set[str] = set()
    transactions: list[TransactionV1] = []
    meta: V1Meta | None = None
    for page_number in range(1, 101):
        fetch = client.get_transactions_page(
            start_date=start.isoformat(), end_date=end.isoformat(), cursor=cursor, limit=1000
        )
        if not fetch.available or fetch.data is None:
            raise ValueError("transaction_page_unavailable")
        page = fetch.data
        if page.start_date != start or page.end_date != end:
            raise ValueError("transaction_window_mismatch")
        if meta is not None and _snapshot_identity(meta) != _snapshot_identity(page.meta):
            raise ValueError("transaction_snapshot_changed")
        meta = page.meta
        if any(not start <= transaction.date <= end for transaction in page.transactions):
            raise ValueError("transaction_outside_query_window")
        transactions.extend(page.transactions)
        cursor = page.next_cursor
        if cursor is None:
            return transactions, meta, page_number
        if not cursor or cursor in cursors:
            raise ValueError("transaction_cursor_cycle")
        cursors.add(cursor)
    raise ValueError("transaction_page_limit")


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_request(
    path: Path, *, db_path: Path, client: TransactionReader, today: date, apply: bool
) -> tuple[int, bool]:
    # Production writes require an OS-enforced lease over the approved request.
    # The existing fence denies its mutation/replacement and ancestor renames.
    fence = (
        hold_windows_evidence_snapshot(path, path.parent, max_bytes=128 * 1024)
        if apply
        else nullcontext(capture_snapshot(path, path.parent))
    )
    with fence as snapshot:
        request = ReconciliationRequest.model_validate_json(snapshot.payload)
        transactions, meta, pages = collect_transactions(
            client, start=request.start_date, end=today
        )
        # V1 has no dataset revision across cursor pages. One page is one
        # producer SELECT over the complete bounded activity set. Multiple
        # pages can be previewed, but cannot prove uniqueness for a write.
        if apply and pages != 1:
            raise ValueError("transaction_snapshot_unproven_across_pages")
        if path.read_bytes() != snapshot.payload:
            raise ValueError("approved_request_changed")
        accounts_fetch = client.get_accounts()
        if not accounts_fetch.available or accounts_fetch.data is None:
            raise ValueError("tracker_accounts_unavailable")
        result = evaluate_request(request, transactions, meta, accounts_fetch.data)
        changed = 0
        if apply:
            with JobLock(
                db_path.parent.parent, "owner-reconciliation-write", ["portfolio-db"], wait_s=0
            ):
                changed = apply_reconciliation(request, result, db_path=db_path)
        if apply:
            receipt_result = result.model_dump(mode="json", exclude={"accounts"})
            receipt_result["account_sync_evidence"] = {
                "meta": result.accounts.meta.model_dump(mode="json"),
                "accounts": [
                    {
                        "account_id": account.account_id,
                        "provider": account.provider,
                        "last_successful_sync_at": account.last_successful_sync_at.isoformat()
                        if account.last_successful_sync_at is not None
                        else None,
                    }
                    for account in result.accounts.accounts
                    if account.account_id in result.meta.account_coverage.included_account_ids
                ],
            }
            _write_receipt(
                path.with_name("reconciliation-latest.json"),
                {
                    "schema_version": 1,
                    "checked_at_utc": datetime.now(UTC).isoformat(),
                    "event_id": request.event_id,
                    "request_sha256": snapshot.sha256,
                    "query_start": request.start_date.isoformat(),
                    "query_end": today.isoformat(),
                    "complete_cursor_traversal": True,
                    "selection_snapshot": "single_provider_query",
                    "pages": pages,
                    "changed_decisions": changed,
                    "result": receipt_result,
                },
            )
        needs_review = any(leg.disposition == "ambiguous" for leg in result.legs) or (
            result.complete and result.funding_status == "unresolved"
        )
        return changed, needs_review


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    add_database_argument(parser)
    parser.add_argument("--requests-root", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Persist confirmations and private receipts"
    )
    args = parser.parse_args(argv)
    try:
        db_path = require_db_path(args.db_path)
        state_root = db_path.parent.parent
        root = args.requests_root or state_root / "output" / "research" / "owner-decisions"
        requests = sorted(root.glob("*/reconciliation-request.json"))
        if len(requests) > 100:
            raise ValueError("request_limit")
        if not requests:
            log_event("owner_reconciliation", requests=0, changed=0)
            return 0
        base = os.environ.get("PORTFOLIO_TRACKER_API_URL", "")
        parsed = urlsplit(base)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("configured_loopback_tracker_required")
        client = TrackerV1Client(base, total_timeout_seconds=60)
        if not client.probe_v1().available:
            raise ValueError("tracker_v1_unavailable")
        changed = 0
        needs_review = False
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        for path in requests:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("request_outside_private_root")
            count, review = run_request(
                path, db_path=db_path, client=client, today=today, apply=args.apply
            )
            changed += count
            needs_review = needs_review or review
        log_event(
            "owner_reconciliation",
            requests=len(requests),
            changed=changed,
            applied=args.apply,
            needs_review=needs_review,
        )
        return 2 if needs_review else 0
    except JobAlreadyRunningError:
        log_event("owner_reconciliation_deferred", reason="writer_busy")
        return 3
    except Exception as exc:
        # Exception text can contain private plan values or provider payloads.
        log_event("owner_reconciliation_failed", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
