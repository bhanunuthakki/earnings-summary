"""Saved SEC plan schemas and seals remain supported after creator retirement."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from pipeline import sec_delta_planner as planner
from pipeline import source_policy
from pipeline.sec_xbrl import CIK_MAP
from provenance.data_backbone_rehearsal import database_storage_identity
from runtime.deterministic import content_sha256_ascii


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def saved_plan(
    database: Path,
    *,
    ticker: str = "RBRK",
    role: Literal["portfolio", "evaluation", "watchlist"] = "portfolio",
    owner_request_id: str | None = None,
) -> planner.SecDeltaPlanReceipt:
    """Build one typed synthetic saved receipt, without selecting or planning work."""
    storage = database_storage_identity(database)
    policy = source_policy.issuer_policy(ticker)
    policy_sha = _sha256(Path(source_policy.__file__))
    states: tuple[tuple[planner.TaskKind, planner.TaskStatus], ...] = (
        ("COMPANYFACTS_DELTA", "READY"),
        ("NATIVE_INVENTORY_PACKAGES", "READY"),
        ("NATIVE_FILING_SECTIONS", "BLOCKED_EVIDENCE_LINKAGE"),
    )
    tasks = tuple(
        planner.SecDeltaTask(
            task_id=f"saved-fixture:{kind}:{ticker}",
            ticker=ticker,
            cik=CIK_MAP[ticker],
            kind=kind,
            status=status,
            reason_code="synthetic_saved_plan",
            authorization="AUTOMATIC" if owner_request_id is None else "OWNER_REQUEST",
            authorization_attestation="NOT_APPLICABLE"
            if owner_request_id is None
            else "CALLER_ATTESTED",
            owner_request_id=owner_request_id,
            source_policy_version=source_policy.POLICY_VERSION,
            source_policy_sha256=policy_sha,
            issuer_policy_version=policy.policy_version,
            issuer_policy_sha256=policy.policy_sha256,
            dependencies=(
                planner.DependencyStatus(
                    name="SYNTHETIC_RECEIPT", status="SATISFIED", detail="Retained reader fixture"
                ),
            ),
        )
        for kind, status in states
    )
    roster = (
        planner.RosterEntry(
            ticker=ticker,
            list_type=role,
            archived=False,
            selection="AUTOMATIC_FULL"
            if owner_request_id is None
            else "OWNER_REQUESTED_EVALUATION",
            owner_request_id=owner_request_id,
        ),
    )
    draft = planner.SecDeltaPlanReceipt.model_construct(
        schema_version="sec_delta_plan_receipt.v2",
        planner_version="sec-delta-planner.v2",
        as_of=date(2026, 8, 12),
        network_policy="FORBIDDEN",
        status="BLOCKED",
        alembic_revision=planner.SUPPORTED_ALEMBIC_REVISION,
        database_path=str(database.resolve()),
        database_storage_identity=storage,
        database_sha256=storage.entries[0].content_sha256,
        database_total_changes=0,
        source_policy_version=source_policy.POLICY_VERSION,
        source_policy_sha256=policy_sha,
        request_sha256="a" * 64,
        roster=roster,
        roster_sha256=content_sha256_ascii([item.model_dump(mode="json") for item in roster]),
        ticker_plans=(
            planner.TickerPlan(
                ticker=ticker,
                list_type=role,
                authorization="AUTOMATIC_FULL" if owner_request_id is None else "OWNER_REQUEST",
                authorization_attestation="NOT_APPLICABLE"
                if owner_request_id is None
                else "CALLER_ATTESTED",
                owner_request_id=owner_request_id,
                cik=CIK_MAP[ticker],
                inventory=planner.NativeInventoryState(
                    inventory_key=f"{policy.issuer_id}:sec-submissions",
                    next_inventory_revision=1,
                    sealed_native_document_count=0,
                    outstanding_native_document_count=0,
                ),
                tasks=tasks,
            ),
        ),
        authorization_rejections=(),
        missing_schema=(),
        blocked_task_count=1,
        receipt_sha256="0" * 64,
    )
    return planner.SecDeltaPlanReceipt.model_validate(
        {**draft.model_dump(), "receipt_sha256": draft.computed_receipt_sha256()}
    )


@pytest.fixture
def receipt(tmp_path: Path) -> planner.SecDeltaPlanReceipt:
    database = tmp_path / "synthetic-snapshot.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE synthetic (value INTEGER)")
    return saved_plan(database)


def test_saved_v2_receipt_roundtrip_preserves_fields_and_commitments(
    receipt: planner.SecDeltaPlanReceipt,
) -> None:
    raw = receipt.model_dump_json(exclude_none=False)
    loaded = planner.SecDeltaPlanReceipt.model_validate_json(raw)
    assert loaded == receipt
    assert loaded.model_dump_json(exclude_none=False) == raw
    assert loaded.computed_roster_sha256() == receipt.roster_sha256
    assert loaded.computed_receipt_sha256() == receipt.receipt_sha256
    assert loaded.network_policy == "FORBIDDEN"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(loaded, "status", "READY")


@pytest.mark.parametrize("field", ["as_of", "source_policy_sha256", "request_sha256"])
def test_saved_receipt_rejects_changed_committed_fields(
    receipt: planner.SecDeltaPlanReceipt, field: str
) -> None:
    payload = receipt.model_dump(mode="json")
    payload[field] = "2026-08-13" if field == "as_of" else "f" * 64
    with pytest.raises(ValidationError, match="receipt_sha256"):
        planner.SecDeltaPlanReceipt.model_validate_json(json.dumps(payload))


def test_saved_receipt_rejects_changed_nested_task_and_roster(
    receipt: planner.SecDeltaPlanReceipt,
) -> None:
    task = receipt.ticker_plans[0].tasks[0].model_copy(update={"reason_code": "tampered"})
    changed = receipt.model_copy(
        update={
            "ticker_plans": (
                receipt.ticker_plans[0].model_copy(
                    update={"tasks": (task, *receipt.ticker_plans[0].tasks[1:])}
                ),
            )
        }
    )
    with pytest.raises(ValidationError, match="receipt_sha256"):
        planner.SecDeltaPlanReceipt.model_validate_json(changed.model_dump_json())
    roster = receipt.roster[0].model_copy(update={"archived": True})
    with pytest.raises(ValidationError, match="roster_sha256"):
        planner.SecDeltaPlanReceipt.model_validate_json(
            receipt.model_copy(update={"roster": (roster,)}).model_dump_json()
        )


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"schema_version": "sec_delta_plan_receipt.v1"}, "schema_version"),
        ({"alembic_revision": "obsolete-head"}, "supported planner schema revision"),
        ({"database_sha256": "f" * 64}, "sealed main storage"),
        ({"blocked_task_count": 0}, "blocked_task_count"),
        ({"status": "READY"}, "blocked work"),
        ({"nonce": "unrecognized"}, "extra_forbidden"),
    ],
)
def test_saved_receipt_preserves_schema_and_consistency_refusals(
    receipt: planner.SecDeltaPlanReceipt, update: dict[str, object], reason: str
) -> None:
    payload = {**receipt.model_dump(mode="json"), **update}
    payload["receipt_sha256"] = content_sha256_ascii(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )
    with pytest.raises(ValidationError, match=reason):
        planner.SecDeltaPlanReceipt.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"outstanding_native_document_count": 1}, "outstanding native documents"),
        ({"current_sealed_snapshot_id": "snapshot"}, "must be present together"),
    ],
)
def test_saved_inventory_retains_count_and_snapshot_constraints(
    receipt: planner.SecDeltaPlanReceipt, update: dict[str, object], reason: str
) -> None:
    payload = {**receipt.ticker_plans[0].inventory.model_dump(), **update}
    with pytest.raises(ValidationError, match=reason):
        planner.NativeInventoryState.model_validate(payload)


def test_saved_terminal_receipt_retains_seal_and_count_checks(
    receipt: planner.SecDeltaPlanReceipt,
) -> None:
    draft = planner.SecDeltaPlanTerminalReceipt.model_construct(
        schema_version="sec_delta_plan_terminal_receipt.v1",
        status="BLOCKED",
        plan_path="/synthetic/saved-plan.json",
        plan_sha256="b" * 64,
        plan_size_bytes=1024,
        plan_receipt_sha256=receipt.receipt_sha256,
        database_storage_sha256=receipt.database_storage_identity.aggregate_sha256,
        source_policy_sha256=receipt.source_policy_sha256,
        ticker_count=1,
        task_count=3,
        blocked_task_count=1,
        blocked_finding_count=1,
        receipt_sha256="0" * 64,
    )
    valid = planner.SecDeltaPlanTerminalReceipt.model_validate(
        {**draft.model_dump(), "receipt_sha256": draft.computed_receipt_sha256()}
    )
    assert planner.SecDeltaPlanTerminalReceipt.model_validate_json(valid.model_dump_json()) == valid
    changed = valid.model_copy(update={"plan_sha256": "f" * 64})
    with pytest.raises(ValidationError, match="terminal receipt seal"):
        planner.SecDeltaPlanTerminalReceipt.model_validate_json(changed.model_dump_json())
    for update in (
        {"blocked_task_count": 4},
        {"blocked_finding_count": 0},
        {"status": "READY"},
    ):
        changed = valid.model_copy(update=update)
        with pytest.raises(ValidationError):
            planner.SecDeltaPlanTerminalReceipt.model_validate(
                {**changed.model_dump(), "receipt_sha256": changed.computed_receipt_sha256()}
            )
