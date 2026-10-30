"""Saved SEC plan schemas and seals remain supported after creator retirement."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

import pytest

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
