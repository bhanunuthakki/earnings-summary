"""Unavailable WIX/AVDV postmortem until execution and owner evidence is wired.

A decision record cannot prove a fill, a position exit, or a thesis break.
Retain the read interface and reject durable writes until refreshed holdings,
execution details, the frozen decision/horizon, and an owner-reviewed lesson
can be resolved through their canonical authorities.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from integrations.portfolio_tracker_v1 import PortfolioSnapshotV1, TransactionsV1Result
from research.owner_decision_checkpoint import OwnerDecisionCheckpointPayload
from user_state.notes import supersede_note


class FactorAttribution(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: str
    sizing: str
    timing: str
    price_luck: str


class WixAvdvPostmortemResult(BaseModel):
    """Read-compatible envelope with explicit missing evidence and no invented facts."""

    model_config = ConfigDict(frozen=True)

    ticker: Literal["WIX"] = "WIX"
    position_entry_id: int
    status: Literal["unavailable"] = "unavailable"
    missing_evidence: tuple[str, ...] = (
        "refreshed_holdings_proving_WIX_absent",
        "verified_exit_execution_date_and_price",
        "frozen_exit_decision_thesis_and_review_horizon",
        "owner_reviewed_lesson_and_attribution",
        "AVDV_execution_timing_and_size",
    )
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    lessons: str | None = None
    outcome_vs_thesis: Literal["broke", "mixed", "played_out", "unrelated"] | None = None
    avdv_status: Literal["counterfactual_not_executed", "realized"] = "counterfactual_not_executed"
    avdv_allocation_pct: float | None = None
    factor_attribution: FactorAttribution | None = None
    evaluated_at: str


def evaluate_wix_avdv_postmortem(
    conn: sqlite3.Connection,
    *,
    entry_id: int | None = None,
) -> WixAvdvPostmortemResult:
    """Identify the WIX entry without treating its decision as execution evidence."""
    if entry_id is None:
        entry_row = conn.execute(
            "SELECT id, ticker FROM position_entries WHERE ticker = 'WIX' AND superseded_by_entry_id IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        entry_row = conn.execute(
            "SELECT id, ticker FROM position_entries WHERE id = ?", (entry_id,)
        ).fetchone()
    if entry_row is None:
        raise LookupError("WIX position entry not found in position_entries")
    if str(entry_row[1]).upper() != "WIX":
        raise ValueError("position entry must belong to WIX")
    return WixAvdvPostmortemResult(
        position_entry_id=int(entry_row[0]),
        evaluated_at=datetime.now(UTC).isoformat(),
    )


def persist_wix_avdv_postmortem(
    conn: sqlite3.Connection,
    result: WixAvdvPostmortemResult,
    *,
    force: bool = False,
) -> bool:
    """Refuse all writes until the missing evidence contract is implemented.

    ``force`` cannot substitute for execution evidence or owner approval. Keep
    the callable boundary so existing callers fail explicitly before mutation.
    """
    raise ValueError(
        "WIX postmortem evidence is unavailable; lifecycle and owner records unchanged"
    )


class WixHistoryCorrection(BaseModel):
    """Exact, reviewable repair; never an owner-approved investment lesson."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_database: str = Field(min_length=1)
    entry_id: int = Field(gt=0)
    note_id: int = Field(gt=0)
    duplicate_entry_id: int | None = Field(default=None, gt=0)
    duplicate_before_json: str | None = None
    decision_id: int = Field(gt=0)
    entry_before_json: str
    note_before_json: str
    decision_before_json: str
    checkpoint_before_json: str | None = None
    source_evidence_json: str
    exit_date: str
    exit_price: Decimal = Field(gt=0, allow_inf_nan=False)
    exit_reason: str = Field(min_length=1)
    prepared_at: datetime
    lesson_state: Literal["owner_review_pending"] = "owner_review_pending"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def _row_json(conn: sqlite3.Connection, table: str, row_id: int) -> str:
    if table not in {
        "position_entries",
        "analyst_notes",
        "decisions",
        "owner_decision_checkpoints",
    }:
        raise ValueError("unsupported correction table")
    cursor = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,))
    row = cursor.fetchone()
    if row is None or cursor.description is None:
        raise LookupError("correction source row unavailable")
    data = {column[0]: row[index] for index, column in enumerate(cursor.description)}
    return json.dumps(data, sort_keys=True, allow_nan=False, separators=(",", ":"))


def prepare_wix_history_correction(
    conn: sqlite3.Connection,
    *,
    entry_id: int,
    note_id: int,
    decision_id: int,
    snapshot: PortfolioSnapshotV1,
    transaction_pages: tuple[TransactionsV1Result, ...],
    transaction_request_cursors: tuple[str | None, ...],
    now: datetime,
    duplicate_entry_id: int | None = None,
) -> WixHistoryCorrection:
    """Bind actual tracker evidence and the owner's exact recorded rationale.

    The final disposal fill supplies the lifecycle exit marker, not an average
    price for all historical sales. AVDV trades are retained as evidence only;
    this correction does not attribute proceeds or grade the investment outcome.
    """
    if now.tzinfo is None or not transaction_pages:
        raise ValueError("dated, complete tracker evidence required")
    envelopes = [snapshot.meta, *(page.meta for page in transaction_pages)]
    for meta in envelopes:
        generated = (
            meta.generated_at.replace(tzinfo=UTC)
            if meta.generated_at.tzinfo is None
            else meta.generated_at
        )
        if (
            not meta.schema_version.startswith("1.")
            or meta.is_partial
            or meta.is_stale
            or meta.as_of != now.date()
            or meta.account_coverage.lagging_account_ids
            or not meta.account_coverage.included_account_ids
            or not timedelta(0) <= now - generated <= timedelta(hours=24)
        ):
            raise ValueError("current complete tracker evidence required")
    expected_accounts = snapshot.meta.account_coverage
    if any(
        set(page.meta.account_coverage.included_account_ids)
        != set(expected_accounts.included_account_ids)
        or set(page.meta.account_coverage.excluded_account_ids)
        != set(expected_accounts.excluded_account_ids)
        for page in transaction_pages
    ):
        raise ValueError("transaction account coverage differs from holdings")
    if (
        len(transaction_request_cursors) != len(transaction_pages)
        or transaction_request_cursors[0] is not None
        or len(set(transaction_request_cursors)) != len(transaction_request_cursors)
        or any(
            cursor != previous.next_cursor
            for cursor, previous in zip(
                transaction_request_cursors[1:], transaction_pages[:-1], strict=True
            )
        )
    ):
        raise ValueError("transaction request cursor chain is incomplete")
    if transaction_pages[-1].next_cursor is not None or any(
        page.next_cursor is None for page in transaction_pages[:-1]
    ):
        raise ValueError("transaction pagination is incomplete")
    if any(position.ticker == "WIX" and position.quantity != 0 for position in snapshot.positions):
        raise ValueError("WIX is still held")
    entry_json = _row_json(conn, "position_entries", entry_id)
    note_json = _row_json(conn, "analyst_notes", note_id)
    decision_json = _row_json(conn, "decisions", decision_id)
    entry, note, decision = (json.loads(value) for value in (entry_json, note_json, decision_json))
    if (
        any(row.get("ticker") != "WIX" for row in (entry, note, decision))
        or entry.get("user_id") != note.get("user_id")
        or note.get("position_entry_id") != entry_id
        or note.get("status") != "open"
        or not str(note.get("body", "")).startswith("WIX Exit Postmortem:")
        or decision.get("decided_by") != "owner"
        or decision.get("recommendation_kind") != "sell"
        or not decision.get("rationale_excerpt")
    ):
        raise ValueError("WIX correction identity or owner evidence mismatch")
    checkpoints = conn.execute(
        "SELECT c.id FROM owner_decision_checkpoints c "
        "JOIN owner_decision_checkpoint_decisions l ON l.checkpoint_id=c.id "
        "WHERE l.decision_id=? AND c.user_id=?",
        (decision_id, entry["user_id"]),
    ).fetchall()
    if len(checkpoints) > 1:
        raise ValueError("ambiguous frozen owner checkpoint")
    checkpoint_json = None
    if checkpoints:
        checkpoint_json = _row_json(conn, "owner_decision_checkpoints", int(checkpoints[0][0]))
        checkpoint = json.loads(checkpoint_json)
        OwnerDecisionCheckpointPayload.model_validate_json(checkpoint["payload_json"])
        # The frozen serialized artifact owns its hash. New optional defaults
        # added to today's schema must not rewrite or invalidate old evidence.
        if (
            hashlib.sha256(checkpoint["payload_json"].encode("utf-8")).hexdigest()
            != checkpoint["payload_sha256"]
        ):
            raise ValueError("frozen owner checkpoint integrity mismatch")
    duplicate_json = None
    if entry.get("superseded_by_entry_id") is not None:
        raise ValueError("canonical WIX entry is superseded")
    if duplicate_entry_id is not None:
        duplicate_json = _row_json(conn, "position_entries", duplicate_entry_id)
        duplicate = json.loads(duplicate_json)
        if (
            duplicate_entry_id == entry_id
            or duplicate.get("ticker") != "WIX"
            or duplicate.get("user_id") != entry.get("user_id")
            or duplicate.get("source") != "reconciler"
            or duplicate.get("exit_date") is not None
            or duplicate.get("superseded_by_entry_id") is not None
            or not duplicate.get("entry_date")
            or not str(entry.get("exit_date", ""))
            < str(duplicate["entry_date"])
            <= now.date().isoformat()
        ):
            raise ValueError("duplicate lifecycle identity requires review")
    open_ids = {
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM position_entries WHERE ticker='WIX' AND user_id=? "
            "AND exit_date IS NULL AND superseded_by_entry_id IS NULL",
            (entry["user_id"],),
        )
    }
    if open_ids - {entry_id, duplicate_entry_id}:
        raise ValueError("unreviewed open WIX lifecycle exists")
    decision_day = str(decision["made_at"])[:10]
    if any(
        page.start_date.isoformat() > decision_day or page.end_date != now.date()
        for page in transaction_pages
    ):
        raise ValueError("transaction window does not cover the exit decision through today")
    transactions = [txn for page in transaction_pages for txn in page.transactions]
    if len({txn.transaction_id for txn in transactions}) != len(transactions):
        raise ValueError("duplicate transaction identity")
    sells = [txn for txn in transactions if txn.ticker == "WIX" and txn.type == "sell"]
    if not sells:
        raise ValueError("verified WIX disposal is unavailable")
    latest = max(txn.date for txn in sells)
    if latest.isoformat() < decision_day:
        raise ValueError("final disposal predates the bound owner decision")
    final_fills = [txn for txn in sells if txn.date == latest]
    accounts = set(snapshot.meta.account_coverage.included_account_ids)
    if any(
        txn.currency != "USD"
        or txn.quantity >= 0
        or txn.price is None
        or txn.price <= 0
        or txn.account_id not in accounts
        for txn in final_fills
    ):
        raise ValueError("unsupported final disposal evidence")
    if latest > now.date() or any(
        txn.ticker == "WIX" and txn.type != "sell" and txn.quantity != 0 and txn.date >= latest
        for txn in transactions
    ):
        raise ValueError("WIX lifecycle requires ambiguous trade review")
    if duplicate_json is not None:
        duplicate_day = str(json.loads(duplicate_json)["entry_date"])
        if duplicate_day > latest.isoformat() or any(
            txn.ticker == "WIX"
            and txn.type == "buy"
            and str(entry["exit_date"]) <= txn.date.isoformat() <= latest.isoformat()
            for txn in transactions
        ):
            raise ValueError("a new WIX purchase prevents duplicate supersession")
    quantity = sum((abs(txn.quantity) for txn in final_fills), Decimal(0))
    value = sum((abs(txn.quantity) * (txn.price or Decimal(0)) for txn in final_fills), Decimal(0))
    evidence = {
        "snapshot": snapshot.model_dump(mode="json"),
        "transaction_pages": [page.model_dump(mode="json") for page in transaction_pages],
        "transaction_request_cursors": transaction_request_cursors,
    }
    target = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if not target:
        raise ValueError("correction requires an explicit persistent database target")
    return WixHistoryCorrection(
        target_database=str(Path(target).resolve()),
        entry_id=entry_id,
        note_id=note_id,
        duplicate_entry_id=duplicate_entry_id,
        duplicate_before_json=duplicate_json,
        decision_id=decision_id,
        entry_before_json=entry_json,
        note_before_json=note_json,
        decision_before_json=decision_json,
        checkpoint_before_json=checkpoint_json,
        source_evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        exit_date=latest.isoformat(),
        exit_price=value / quantity,
        exit_reason=str(decision["rationale_excerpt"]),
        prepared_at=now,
    )


def apply_wix_history_correction(
    conn: sqlite3.Connection,
    plan: WixHistoryCorrection,
    *,
    approved_fingerprint: str,
    now: datetime | None = None,
) -> int:
    """Atomic, source-bound, compare-and-swap correction with retained before-images."""
    target = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if not target or str(Path(target).resolve()) != plan.target_database:
        raise ValueError("correction plan belongs to another database target")
    applied_at = now or datetime.now(UTC)
    if (
        applied_at.tzinfo is None
        or plan.prepared_at.tzinfo is None
        or applied_at.date() != plan.prepared_at.date()
        or not timedelta(0) <= applied_at - plan.prepared_at <= timedelta(hours=1)
    ):
        raise ValueError("correction plan expired; refresh evidence and prepare again")
    if conn.in_transaction:
        raise ValueError("correction requires its own transaction")
    if approved_fingerprint != plan.fingerprint():
        raise ValueError("correction approval does not match the exact plan")
    # Revalidate even a model_copy-forged plan against its original source evidence.
    evidence = json.loads(plan.source_evidence_json)
    snapshot = PortfolioSnapshotV1.model_validate(evidence["snapshot"])
    pages = tuple(
        TransactionsV1Result.model_validate(page) for page in evidence["transaction_pages"]
    )
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, row_id, expected in (
            ("position_entries", plan.entry_id, plan.entry_before_json),
            ("analyst_notes", plan.note_id, plan.note_before_json),
            ("decisions", plan.decision_id, plan.decision_before_json),
        ):
            if _row_json(conn, table, row_id) != expected:
                raise ValueError("correction source changed; prepare a new plan")
        if (
            plan.duplicate_entry_id is not None
            and _row_json(conn, "position_entries", plan.duplicate_entry_id)
            != plan.duplicate_before_json
        ):
            raise ValueError("duplicate lifecycle changed; prepare a new plan")
        rebuilt = prepare_wix_history_correction(
            conn,
            entry_id=plan.entry_id,
            duplicate_entry_id=plan.duplicate_entry_id,
            note_id=plan.note_id,
            decision_id=plan.decision_id,
            snapshot=snapshot,
            transaction_pages=pages,
            transaction_request_cursors=TypeAdapter(tuple[str | None, ...]).validate_python(
                evidence.get("transaction_request_cursors")
            ),
            now=applied_at,
        )
        if rebuilt.model_copy(update={"prepared_at": plan.prepared_at}) != plan:
            raise ValueError("correction values disagree with source evidence")
        stamp = applied_at.isoformat()
        conn.execute(
            "UPDATE position_entries SET exit_date=?,exit_price=?,exit_reason=?,lessons=NULL,outcome_vs_thesis=NULL,updated_at=? WHERE id=?",
            (plan.exit_date, float(plan.exit_price), plan.exit_reason, stamp, plan.entry_id),
        )
        if plan.duplicate_entry_id is not None:
            conn.execute(
                "UPDATE position_entries SET superseded_by_entry_id=?,updated_at=? WHERE id=?",
                (plan.entry_id, stamp, plan.duplicate_entry_id),
            )
        note = supersede_note(
            plan.note_id,
            body=(
                f"Correction: the generated WIX thesis-break postmortem is withdrawn. "
                f"Current complete holdings and tracker fills establish final disposal on {plan.exit_date}. "
                f"Owner decision {plan.decision_id}: {plan.exit_reason} "
                "No investment outcome or owner-reviewed lesson is asserted. AVDV transaction evidence is retained without attributing proceeds."
            ),
            source="advisor",
            source_ref=f"wix-history-correction:{plan.fingerprint()}",
            context={
                "postmortem_type": "wix_history_correction",
                "lesson_state": plan.lesson_state,
                "plan": json.loads(plan.model_dump_json()),
                "approved_fingerprint": approved_fingerprint,
            },
            conn=conn,
        )
        conn.commit()
        return note.id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous_factory
