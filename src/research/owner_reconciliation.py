"""Exact, source-backed reconciliation of owner-reported execution legs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from integrations.portfolio_tracker_v1 import AccountsV1Result, TransactionV1, V1Meta
from user_state._db import open_conn
from user_state.notes import supersede_note


class ExecutionLeg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: int = Field(gt=0)
    source_event_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    direction: Literal["buy", "sell"]
    currency: str = Field(min_length=1)


class ReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    event_id: str = Field(min_length=1)
    start_date: date
    decision_date: date
    legs: list[ExecutionLeg] = Field(min_length=1)
    funding_tickers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_request(self) -> ReconciliationRequest:
        if not self.event_id.strip() or self.start_date > self.decision_date:
            raise ValueError("invalid event or date window")
        if len({leg.decision_id for leg in self.legs}) != len(self.legs):
            raise ValueError("duplicate decision")
        keys = {(leg.ticker.upper(), leg.direction, leg.currency.upper()) for leg in self.legs}
        if len(keys) != len(self.legs):
            raise ValueError("execution legs must have distinct matching keys")
        if any(
            not leg.ticker.strip() or not leg.currency.strip() or not leg.source_event_id.strip()
            for leg in self.legs
        ):
            raise ValueError("blank ticker or currency")
        if len({leg.source_event_id for leg in self.legs}) != len(self.legs):
            raise ValueError("duplicate source event")
        return self


class LegResult(BaseModel):
    decision_id: int
    disposition: Literal["matched", "pending", "ambiguous"]
    transaction: TransactionV1 | None = None
    candidate_transaction_ids: list[str] = Field(default_factory=list)


class ReconciliationResult(BaseModel):
    event_id: str
    legs: list[LegResult]
    complete: bool
    funding_status: Literal["unresolved", "not_requested"]
    funding_candidates: list[TransactionV1]
    meta: V1Meta
    accounts: AccountsV1Result


def _tracker_sync_utc(value: datetime) -> datetime:
    # Tracker writes UTC but SQLite can return its sync timestamp without an offset.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def evaluate_request(
    request: ReconciliationRequest,
    transactions: list[TransactionV1],
    meta: V1Meta,
    accounts: AccountsV1Result,
) -> ReconciliationResult:
    coverage = meta.account_coverage
    included = set(coverage.included_account_ids)
    excluded = set(coverage.excluded_account_ids)
    lagging = set(coverage.lagging_account_ids)
    if (
        meta.is_stale
        or meta.is_partial
        or meta.as_of is None
        or meta.last_successful_sync_at is None
        or meta.as_of < request.decision_date
        or not meta.currency.strip()
        or not included
        or len(included) != len(coverage.included_account_ids)
        or included & excluded
        or not lagging <= included
        or lagging
    ):
        raise ValueError("tracker metadata is incomplete or inconsistent")
    pacific = ZoneInfo("America/Los_Angeles")
    generated = meta.generated_at
    sync = meta.last_successful_sync_at
    account_meta = accounts.meta
    sync_utc = _tracker_sync_utc(sync)
    if (
        generated.tzinfo is None
        or account_meta.generated_at.tzinfo is None
        or sync_utc > generated
        or generated.astimezone(pacific).date() != meta.as_of
        or sync_utc.astimezone(pacific).date() != meta.as_of
        or set(account_meta.account_coverage.included_account_ids) != included
        or len(account_meta.account_coverage.included_account_ids) != len(included)
        or set(account_meta.account_coverage.excluded_account_ids) != excluded
        or bool(account_meta.account_coverage.lagging_account_ids)
        or account_meta.last_successful_sync_at is None
        or _tracker_sync_utc(account_meta.last_successful_sync_at) != sync_utc
        or account_meta.currency != meta.currency
        or set(account_meta.source_providers) != set(meta.source_providers)
        or account_meta.as_of != meta.as_of
        or account_meta.is_partial
        or account_meta.is_stale
    ):
        raise ValueError("tracker sync evidence is incomplete or outdated")
    by_id = {
        account.account_id: account
        for account in accounts.accounts
        if account.account_id in included
    }
    if len(by_id) != len(included) or sum(
        account.account_id in included for account in accounts.accounts
    ) != len(included):
        raise ValueError("tracker sync account coverage mismatch")
    for account in by_id.values():
        if not account.active or not account.included_in_totals:
            raise ValueError("tracker sync account coverage mismatch")
        last_sync = account.last_successful_sync_at
        if (
            last_sync is None
            or _tracker_sync_utc(last_sync) > generated
            or _tracker_sync_utc(last_sync) > account_meta.generated_at
            or _tracker_sync_utc(last_sync).astimezone(pacific).date() != meta.as_of
        ):
            raise ValueError("tracker account sync evidence is incomplete or outdated")
    unique: dict[str, TransactionV1] = {}
    for tx in transactions:
        if not tx.transaction_id.strip():
            raise ValueError("transaction identity missing")
        previous = unique.get(tx.transaction_id)
        if previous is not None and previous != tx:
            raise ValueError("conflicting transaction identity")
        unique[tx.transaction_id] = tx
    legs: list[LegResult] = []
    for leg in request.legs:
        candidates = [
            tx
            for tx in unique.values()
            if tx.date == request.decision_date
            and (tx.ticker or "").upper() == leg.ticker.upper()
            and tx.type.lower() == leg.direction
            and tx.currency.upper() == leg.currency.upper()
        ]
        for tx in candidates:
            if (
                tx.account_id not in included
                or not tx.quantity.is_finite()
                or abs(tx.quantity) <= 0
                or tx.price is None
                or not tx.price.is_finite()
                or tx.price <= 0
                or not tx.amount.is_finite()
                or tx.amount == 0
                or (tx.fees is not None and not tx.fees.is_finite())
            ):
                raise ValueError("matching transaction is incomplete")
        disposition: Literal["matched", "pending", "ambiguous"] = (
            "matched" if len(candidates) == 1 else "pending" if not candidates else "ambiguous"
        )
        legs.append(
            LegResult(
                decision_id=leg.decision_id,
                disposition=disposition,
                transaction=candidates[0] if disposition == "matched" else None,
                candidate_transaction_ids=sorted(tx.transaction_id for tx in candidates),
            )
        )
    funding = {ticker.upper() for ticker in request.funding_tickers}
    return ReconciliationResult(
        event_id=request.event_id,
        legs=legs,
        complete=all(leg.disposition == "matched" for leg in legs),
        funding_status="unresolved" if funding else "not_requested",
        funding_candidates=[
            tx
            for tx in unique.values()
            if (tx.ticker or "").upper() in funding
            and request.start_date <= tx.date <= request.decision_date
        ],
        meta=meta,
        accounts=accounts,
    )


def _object(raw: str | None) -> dict[str, object]:
    value: object = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


def _ids(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {v for v in cast(list[object], value) if isinstance(v, int)}


_BLOCK_START = "<!-- owner-execution-confirmation:start -->"
_BLOCK_END = "<!-- owner-execution-confirmation:end -->"


def _replace_confirmation(body: str, block: str) -> str:
    starts, ends = body.count(_BLOCK_START), body.count(_BLOCK_END)
    if starts == ends == 0:
        return body + "\n\n" + block
    if starts != 1 or ends != 1:
        raise ValueError("invalid execution confirmation delimiters")
    start, end = body.index(_BLOCK_START), body.index(_BLOCK_END)
    if end < start:
        raise ValueError("invalid execution confirmation delimiters")
    return body[:start] + block + body[end + len(_BLOCK_END) :]


def apply_reconciliation(
    request: ReconciliationRequest, result: ReconciliationResult, *, db_path: Path | str
) -> int:
    """Apply matched legs and update one linked active note in a single transaction."""
    if result.event_id != request.event_id or [x.decision_id for x in result.legs] != [
        x.decision_id for x in request.legs
    ]:
        raise ValueError("result does not match request")
    evaluate_request(request, [], result.meta, result.accounts)
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = 0
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        changed_decisions: set[int] = set()
        matched_ids: set[str] = set()
        existing_bindings: dict[str, tuple[int, object]] = {}
        for row in conn.execute(
            "SELECT id, user_notes FROM decisions WHERE user_notes IS NOT NULL"
        ):
            try:
                metadata = _object(row["user_notes"])
            except (ValueError, TypeError):
                continue
            binding = metadata.get("broker_execution")
            if isinstance(binding, dict):
                binding_dict = cast(dict[str, object], binding)
                tx_id = binding_dict.get("transaction_id")
                if not isinstance(tx_id, str):
                    continue
                if (
                    binding_dict.get("event_id") == request.event_id
                    and binding_dict.get("request_hash") != request_hash
                ):
                    raise ValueError("event already bound to a different request")
                if tx_id in existing_bindings:
                    raise ValueError("duplicate existing transaction binding")
                existing_bindings[tx_id] = (int(row["id"]), binding_dict)
        for leg, leg_result in zip(request.legs, result.legs, strict=True):
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (leg.decision_id,)
            ).fetchone()
            if (
                row is None
                or row["decided_by"] != "owner"
                or str(row["ticker"]).upper() != leg.ticker.upper()
            ):
                raise ValueError("decision identity mismatch")
            valid_kinds = {"initiate", "add"} if leg.direction == "buy" else {"trim", "sell"}
            if (
                row["recommendation_kind"] not in valid_kinds
                or str(row["made_at"])[:10] != request.decision_date.isoformat()
            ):
                raise ValueError("decision direction or date mismatch")
            metadata = _object(row["user_notes"])
            if metadata.get("source_event_id") != leg.source_event_id:
                raise ValueError("decision source event mismatch")
            status = metadata.get("execution_verification")
            if status not in {"owner_reported_not_broker_reconciled", "broker_reconciled"}:
                raise ValueError("decision verification marker missing")
            tx = leg_result.transaction
            if leg_result.disposition != "matched":
                if tx is not None:
                    raise ValueError("inconsistent result")
                if metadata.get("broker_execution") is not None or status == "broker_reconciled":
                    raise ValueError(
                        "execution evidence conflict: previously confirmed match is no longer unique"
                    )
                continue
            if tx is None or tx.transaction_id in matched_ids:
                raise ValueError("duplicate or missing execution")
            matched_ids.add(tx.transaction_id)
            # Revalidate the match and envelope before using a result supplied by a caller.
            check = evaluate_request(
                request.model_copy(update={"legs": [leg]}), [tx], result.meta, result.accounts
            )
            if check.legs[0].disposition != "matched":
                raise ValueError("execution does not match request")
            binding = {
                "event_id": request.event_id,
                "request_hash": request_hash,
                "transaction_id": tx.transaction_id,
                "account_id": tx.account_id,
                "account_name": tx.account_name,
                "date": tx.date.isoformat(),
                "ticker": tx.ticker,
                "type": tx.type,
                "currency": tx.currency,
                "quantity": str(tx.quantity),
                "price": str(tx.price),
                "amount": str(tx.amount),
                "fees": str(tx.fees) if tx.fees is not None else None,
                "source": result.meta.model_dump(mode="json"),
            }
            old = metadata.get("broker_execution")
            other = existing_bindings.get(tx.transaction_id)
            if other is not None and other[0] != leg.decision_id:
                raise ValueError("transaction already bound to another decision")
            if old is not None:
                if not isinstance(old, dict):
                    raise ValueError("decision already has a different execution")
                old_dict = cast(dict[str, object], old)
                if any(old_dict.get(k) != binding[k] for k in binding if k != "source"):
                    raise ValueError("decision already has a different execution")
                if status != "broker_reconciled":
                    raise ValueError("execution marker conflict")
            else:
                if status != "owner_reported_not_broker_reconciled":
                    raise ValueError("execution binding missing")
                metadata["broker_execution"] = binding
                metadata["execution_verification"] = "broker_reconciled"
                conn.execute(
                    "UPDATE decisions SET user_notes = ? WHERE id = ?",
                    (json.dumps(metadata), leg.decision_id),
                )
                changed += 1
                changed_decisions.add(leg.decision_id)
        if changed:
            groups: dict[int, tuple[sqlite3.Row, dict[str, object], set[int]]] = {}
            notes = list(conn.execute("SELECT * FROM analyst_notes WHERE status = 'open'"))
            for decision_id in changed_decisions:
                candidates: list[tuple[sqlite3.Row, dict[str, object]]] = []
                for note in notes:
                    context = _object(note["context_json"])
                    context_ids = context.get("decision_ids")
                    linked = _ids(context_ids)
                    if note["decision_id"] is not None:
                        linked.add(int(note["decision_id"]))
                    if decision_id in linked:
                        candidates.append((note, context))
                if len(candidates) != 1:
                    raise ValueError("expected one linked active note per decision")
                note, context = candidates[0]
                note_id = int(note["id"])
                if note_id not in groups:
                    groups[note_id] = (note, context, set())
                groups[note_id][2].add(decision_id)
            for note_id, (note, context, decision_ids) in groups.items():
                prior_ids = context.get("decision_ids")
                group_ids = _ids(prior_ids)
                group_ids.update(decision_ids)
                if note["decision_id"] is not None:
                    group_ids.add(int(note["decision_id"]))
                context["decision_ids"] = sorted(group_ids)
                context["execution_reconciliation_event_id"] = request.event_id
                lines: list[str] = []
                group_metadata: dict[int, dict[str, object]] = {}
                for decision_id in sorted(group_ids):
                    row = conn.execute(
                        "SELECT user_notes FROM decisions WHERE id = ?", (decision_id,)
                    ).fetchone()
                    if row is None:
                        raise ValueError("linked decision missing")
                    metadata = _object(row["user_notes"])
                    group_metadata[decision_id] = metadata
                    execution = metadata.get("broker_execution")
                    if execution is None:
                        continue
                    if not isinstance(execution, dict):
                        raise ValueError("invalid linked execution")
                    execution = cast(dict[str, object], execution)
                    fields = (
                        "ticker",
                        "type",
                        "amount",
                        "currency",
                        "quantity",
                        "price",
                        "account_id",
                        "transaction_id",
                        "date",
                    )
                    if (
                        any(execution.get(field) is None for field in fields)
                        or "fees" not in execution
                    ):
                        raise ValueError("incomplete linked execution")
                    fee = execution["fees"] if execution["fees"] is not None else "not supplied"
                    lines.append(
                        f"{execution['ticker']} {execution['type']}: {execution['amount']} {execution['currency']}; "
                        f"quantity: {execution['quantity']}; price: {execution['price']}; fee: {fee}; "
                        f"account: {execution['account_id']}; transaction: {execution['transaction_id']}; date: {execution['date']}."
                    )
                state = (
                    "broker_reconciled"
                    if len(lines) == len(group_ids)
                    else "partially_broker_reconciled"
                )
                context["execution_verification"] = state
                context["reconciliation_status"] = state
                suffix = _BLOCK_START + "\nExecution confirmation\n" + "\n".join(lines)
                funding = context.get("funding_tickers")
                funding_items = cast(list[object], funding) if isinstance(funding, list) else []
                note_funding = {value.upper() for value in funding_items if isinstance(value, str)}
                if note_funding & {ticker.upper() for ticker in request.funding_tickers}:
                    suffix += "\nFunding remains unverified."
                suffix += "\n" + _BLOCK_END
                evidence_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()
                replacement = supersede_note(
                    note_id,
                    body=_replace_confirmation(str(note["body"]), suffix),
                    source="advisor",
                    context=context,
                    source_ref=f"execution:{request.event_id}:{note_id}:{evidence_hash}",
                    expected_revision=str(note["updated_at"]),
                    conn=conn,
                )
                for decision_id, metadata in group_metadata.items():
                    if "current_rationale_note_id" in metadata:
                        metadata["current_rationale_note_id"] = replacement.id
                        conn.execute(
                            "UPDATE decisions SET user_notes = ? WHERE id = ?",
                            (json.dumps(metadata), decision_id),
                        )
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
