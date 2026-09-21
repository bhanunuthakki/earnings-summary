"""Safe persisted FMP timing, backlog distribution and terminal-receipt projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pipeline.fmp_recovery import FinalRefreshReceipt


class FmpBacklogBucket(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: str
    priority: int
    count: int


class FmpOperationalDetails(BaseModel):
    model_config = ConfigDict(frozen=True)
    opened_at: str | None = None
    last_transition_at: str | None = None
    deferred_count: int | None = None
    oldest_backlog_age_seconds: float | None = None
    role_priority_counts: tuple[FmpBacklogBucket, ...] = ()
    receipt_state: Literal[
        "fresh",
        "partial",
        "degraded_corpus",
        "stale",
        "unavailable",
        "disabled",
        "not_yet_wired",
        "empty",
    ] = "not_yet_wired"
    latest_receipt: FinalRefreshReceipt | None = None
    backlog_record_ids: tuple[str, ...] = ()
    backlog_record_count: int = 0


def _age(value: str, now: datetime) -> float | None:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        current = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        age = (current - stamp).total_seconds()
        return age if age >= 0 else None
    except ValueError:
        return None


def read_fmp_operational_details(
    conn: sqlite3.Connection, *, as_of: datetime, receipt_max_age: timedelta
) -> FmpOperationalDetails:
    """Schema absence is explicit; no mutable endpoint row is a recovery receipt."""
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fmp_work_backlog)")}
    if not {"coverage_role", "available_at", "created_at"} <= columns:
        return FmpOperationalDetails()
    circuit_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(provider_circuit_state)")
    }
    opened_at = None
    if "opened_at" in circuit_columns:
        opened = conn.execute(
            "SELECT opened_at FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        opened_at = str(opened[0]) if opened is not None and opened[0] else None
    transition = None
    if "fmp_recovery_events" in tables:
        transition = conn.execute(
            "SELECT MAX(recorded_at) FROM fmp_recovery_events WHERE provider='fmp' AND state_from IS NOT state_to AND state_to IS NOT NULL"
        ).fetchone()[0]
    pending = conn.execute(
        "SELECT created_at,available_at FROM fmp_work_backlog WHERE state IN ('PENDING','LEASED')"
    ).fetchall()
    ages = [age for row in pending if (age := _age(str(row["created_at"]), as_of)) is not None]
    current = (
        as_of.replace(tzinfo=None)
        if as_of.tzinfo is None
        else as_of.astimezone(UTC).replace(tzinfo=None)
    )
    deferred = int(
        conn.execute(
            "SELECT COUNT(*) FROM fmp_work_backlog WHERE state='PENDING' AND datetime(available_at)>datetime(?)",
            (current.isoformat(),),
        ).fetchone()[0]
    )
    buckets = tuple(
        FmpBacklogBucket(
            role=str(row[0])
            if str(row[0]) in {"portfolio", "evaluation", "watchlist", "index_member"}
            else "unknown",
            priority=int(row[1]),
            count=int(row[2]),
        )
        for row in conn.execute(
            "SELECT coverage_role,priority,COUNT(*) FROM fmp_work_backlog WHERE state IN ('PENDING','LEASED') GROUP BY coverage_role,priority ORDER BY priority DESC,coverage_role"
        )
    )
    data = FmpOperationalDetails(
        opened_at=opened_at,
        last_transition_at=str(transition) if transition else None,
        deferred_count=deferred,
        oldest_backlog_age_seconds=max(ages) if ages else None,
        role_priority_counts=buckets,
        backlog_record_ids=tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT work_id FROM fmp_work_backlog WHERE state IN ('PENDING','LEASED') ORDER BY priority DESC,created_at,work_id LIMIT 100"
            )
        ),
        backlog_record_count=len(pending),
    )
    if "fmp_refresh_receipts" not in tables:
        return data
    row = conn.execute(
        "SELECT run_id,receipt_id,payload_json,payload_sha256,recorded_at FROM fmp_refresh_receipts ORDER BY recorded_at DESC,receipt_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return data.model_copy(update={"receipt_state": "empty"})
    try:
        payload = str(row["payload_json"])
        if hashlib.sha256(payload.encode()).hexdigest() != str(row["payload_sha256"]):
            raise ValueError("receipt digest mismatch")
        receipt = FinalRefreshReceipt.model_validate_json(payload)
        if (
            receipt.run_id != row["run_id"]
            or receipt.receipt_id != row["receipt_id"]
            or receipt.receipt_id != hashlib.sha256(receipt.run_id.encode()).hexdigest()
            or receipt.recorded_at != datetime.fromisoformat(str(row["recorded_at"]))
        ):
            raise ValueError("receipt envelope identity/time mismatch")
        actual = tuple(
            str(item[0])
            for item in conn.execute(
                "SELECT attempt_id FROM fmp_work_attempts WHERE run_id=? ORDER BY attempt_id",
                (receipt.run_id,),
            )
        )
        if actual != receipt.attempt_ids:
            raise ValueError("receipt/attempt membership mismatch")
        reused = tuple(
            str(item[0])
            for item in conn.execute(
                "SELECT attempt_id FROM fmp_work_attempts WHERE attempt_id IN (SELECT value FROM json_each(?)) AND outcome_code IN ('live_success','alternative_success','reconciled_success','corpus_success') ORDER BY attempt_id",
                (json.dumps(receipt.reused_attempt_ids),),
            )
        )
        if reused != receipt.reused_attempt_ids:
            raise ValueError("receipt reused proof membership mismatch")
        age = _age(str(row["recorded_at"]), as_of)
        state = (
            "unavailable"
            if age is None
            else (
                "stale"
                if age > receipt_max_age.total_seconds()
                else {
                    "FRESH": "fresh",
                    "PARTIAL": "partial",
                    "DEGRADED_CORPUS": "degraded_corpus",
                    "FAILED": "unavailable",
                }[receipt.status.value]
            )
        )
        reason = conn.execute(
            "SELECT last_reason_code FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        if (
            reason is not None
            and reason[0] in {"auth_missing", "auth_invalid"}
            and state == "unavailable"
        ):
            state = "disabled"
        return data.model_copy(update={"receipt_state": state, "latest_receipt": receipt})
    except ValueError:
        return data.model_copy(update={"receipt_state": "unavailable"})
