"""Append-only receipts for actual SEC apply attempts, separate from source coverage.

The execution writers own request/start/completion. Planning and dry runs never
write here. Retry creates a new attempt of the same logical request; no historical
state is inferred. Receipt replay preserves the original terminal result and time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExecutionKind = Literal["native_capture", "inventory_sync"]
ExecutionState = Literal["requested", "running", "succeeded", "partial", "deferred", "failed"]
TerminalState = Literal["succeeded", "partial", "deferred", "failed"]
ReasonCode = Literal[
    "requested",
    "started",
    "batch_complete",
    "batch_partial",
    "source_deferred",
    "authorization_failed",
    "capture_failed",
    "inventory_complete",
    "inventory_partial",
    "inventory_failed",
]


class SecExecutionScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: ExecutionKind
    tickers: tuple[str, ...]
    inventory_keys: tuple[str, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    expected_document_ids: tuple[str, ...] = ()

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value or len(value) > 32 or not all(c.isalnum() or c in ".-_" for c in value)
            for value in values
        ):
            raise ValueError("invalid execution ticker")
        return tuple(sorted(set(values)))


class SecExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state: TerminalState
    reason_code: ReasonCode
    considered: int = Field(default=0, ge=0)
    captured: int = Field(default=0, ge=0)
    deferred: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    has_more: bool = False
    snapshot_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _complete(self) -> SecExecutionResult:
        if self.state == "succeeded" and (self.deferred or self.failed or self.has_more):
            raise ValueError("successful completion cannot contain unresolved work")
        if self.captured > self.considered:
            raise ValueError("captures exceed considered work")
        return self


class SecExecutionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["sec-execution.v1"] = "sec-execution.v1"
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=0, le=2)
    state: ExecutionState
    recorded_at: datetime
    scope: SecExecutionScope
    result: SecExecutionResult | None = None

    @field_validator("recorded_at")
    @classmethod
    def _clock(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _phase(self) -> SecExecutionReceipt:
        if self.sequence == 0 and (self.state != "requested" or self.result is not None):
            raise ValueError("requested phase requires no terminal result")
        if self.sequence == 1 and (self.state != "running" or self.result is not None):
            raise ValueError("running phase requires no terminal result")
        if self.sequence == 2 and (self.result is None or self.state != self.result.state):
            raise ValueError("terminal phase requires its exact typed result")
        return self


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _append(conn: sqlite3.Connection, receipt: SecExecutionReceipt) -> None:
    payload = receipt.model_dump_json()
    conn.execute(
        "INSERT INTO sec_execution_receipts VALUES(?,?,?,?,?,?,?)",
        (
            receipt.attempt_id,
            receipt.request_id,
            receipt.sequence,
            receipt.state,
            receipt.recorded_at.isoformat(),
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
        ),
    )


def _load(row: tuple[object, ...] | sqlite3.Row) -> SecExecutionReceipt:
    payload = str(row[5])
    if hashlib.sha256(payload.encode()).hexdigest() != row[6]:
        raise ValueError("SEC execution receipt hash mismatch")
    receipt = SecExecutionReceipt.model_validate_json(payload)
    if (
        receipt.attempt_id,
        receipt.request_id,
        receipt.sequence,
        receipt.state,
        receipt.recorded_at.isoformat(),
    ) != tuple(row[:5]):
        raise ValueError("SEC execution receipt envelope mismatch")
    return receipt


def begin_sec_execution(
    conn: sqlite3.Connection, *, request_key: str, scope: SecExecutionScope, now: datetime
) -> SecExecutionReceipt:
    """Record the actual request/start before network; never own caller transactions."""
    if conn.in_transaction:
        raise RuntimeError("SEC execution requires no active caller transaction")
    request_id = _digest({"key": request_key, "scope": scope.model_dump(mode="json")})
    conn.execute("BEGIN IMMEDIATE")
    try:
        number = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM sec_execution_receipts WHERE request_id=? AND sequence=0",
                    (request_id,),
                ).fetchone()[0]
            )
            + 1
        )
        attempt_id = _digest({"request_id": request_id, "attempt": number})
        requested = SecExecutionReceipt(
            request_id=request_id,
            attempt_id=attempt_id,
            sequence=0,
            state="requested",
            recorded_at=now,
            scope=scope,
        )
        running = SecExecutionReceipt(
            request_id=request_id,
            attempt_id=attempt_id,
            sequence=1,
            state="running",
            recorded_at=now,
            scope=scope,
        )
        _append(conn, requested)
        _append(conn, running)
        conn.commit()
        return running
    except Exception:
        conn.rollback()
        raise


def finish_sec_execution(
    conn: sqlite3.Connection,
    started: SecExecutionReceipt,
    result: SecExecutionResult,
    *,
    now: datetime,
) -> SecExecutionReceipt:
    """Append once, or return an identical previous terminal result and its time."""
    if conn.in_transaction:
        raise RuntimeError("SEC execution finalization requires no active caller transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM sec_execution_receipts WHERE attempt_id=? ORDER BY sequence",
            (started.attempt_id,),
        ).fetchall()
        records = tuple(_load(row) for row in rows)
        if len(records) < 2 or records[1] != started:
            raise ValueError("SEC execution start identity mismatch")
        if len(records) == 3:
            terminal = records[2]
            if terminal.result != result:
                raise ValueError("SEC terminal replay changed its result")
            conn.commit()
            return terminal
        terminal = SecExecutionReceipt(
            request_id=started.request_id,
            attempt_id=started.attempt_id,
            sequence=2,
            state=result.state,
            recorded_at=now,
            scope=started.scope,
            result=result,
        )
        if terminal.recorded_at < started.recorded_at:
            raise ValueError("SEC completion precedes its start")
        _append(conn, terminal)
        conn.commit()
        return terminal
    except Exception:
        conn.rollback()
        raise


def read_sec_executions(
    conn: sqlite3.Connection, *, ticker: str
) -> tuple[SecExecutionReceipt, ...]:
    """Current attempt per lane, preserving incomplete starts and exact terminal data."""
    selected: list[SecExecutionReceipt] = []
    for kind in ("native_capture", "inventory_sync"):
        row = conn.execute(
            "SELECT receipt.* FROM sec_execution_receipts AS receipt WHERE json_extract(receipt.payload_json,'$.scope.kind')=? AND EXISTS(SELECT 1 FROM json_each(receipt.payload_json,'$.scope.tickers') WHERE value=?) AND NOT EXISTS(SELECT 1 FROM sec_execution_receipts newer WHERE newer.attempt_id=receipt.attempt_id AND newer.sequence>receipt.sequence) ORDER BY (SELECT started.rowid FROM sec_execution_receipts AS started WHERE started.attempt_id=receipt.attempt_id AND started.sequence=0) DESC LIMIT 1",
            (kind, ticker),
        ).fetchone()
        if row is not None:
            selected.append(_load(row))
    return tuple(selected)
