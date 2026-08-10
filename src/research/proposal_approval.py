"""Durable approval authority for actionable Copilot Ask proposals.

The immutable diff lives on ``research_proposals``.  Approval is the only path
that may turn one of those rows into a holdings-file mutation: it validates a
closed thesis/KPI operation, holds the shared portfolio write lock, compares the
exact target-file precondition, replaces the file atomically, synchronizes the
database mirrors, and commits a replayable decision receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from compute.holdings_sanitize import sanitize_holdings_scalars
from compute.thesis_evaluator import HoldingsSpec
from identity import DEFAULT_USER_ID
from research.proposals import ResearchProposal, create_proposal, get_proposal
from run_lock import hold_run_lock
from user_state._db import now_iso, open_conn

ProposalStatus: TypeAlias = Literal["pending", "approved", "rejected", "superseded"]
ProposalDecision: TypeAlias = Literal["approve", "reject"]
KpiScalar: TypeAlias = str | int | float | bool | None
KpiTargetPath: TypeAlias = Literal["/tier_1_kpis", "/tier_2_kpis", "/tier_3_kpis"]

_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,23}")
_TARGET_FILE_RE = re.compile(r"micro_thesis/holdings/([A-Z0-9][A-Z0-9.-]{0,23})\.json")
_MAX_HOLDINGS_BYTES = 2_000_000


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


class ProposalValidationError(ValueError):
    """The proposed operation is not a closed, currently valid thesis/KPI edit."""


class StoredProposalError(RuntimeError):
    """Stored governed proposal data failed its canonical integrity checks."""


class ProposalConflictError(RuntimeError):
    """A decision violated the proposal revision, status, or idempotency CAS."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        proposal_id: int,
        current_proposal_revision: int | None = None,
        current_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.proposal_id = proposal_id
        self.current_proposal_revision = current_proposal_revision
        self.current_status = current_status


class TargetDriftError(RuntimeError):
    """The holdings file no longer matches the proposal's exact snapshot."""

    def __init__(
        self,
        *,
        proposal_id: int,
        expected_target_sha256: str,
        actual_target_sha256: str,
    ) -> None:
        super().__init__("proposal target changed after the proposal was created")
        self.proposal_id = proposal_id
        self.expected_target_sha256 = expected_target_sha256
        self.actual_target_sha256 = actual_target_sha256


class KpiEntryV1(BaseModel):
    """The bounded KPI entry shape already used by holdings tier arrays."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    current: KpiScalar = None
    prior: KpiScalar = None
    yoy: KpiScalar = None
    status: KpiScalar = None
    break_condition: KpiScalar = None
    source: KpiScalar = None
    frequency: KpiScalar = None
    as_of: KpiScalar = None
    note: KpiScalar = None
    notes: KpiScalar = None

    @field_validator("name")
    @classmethod
    def _sanitize_name(cls, value: str) -> str:
        from llm.postprocess import strip_inline_markdown

        return strip_inline_markdown(value).strip()

    @field_validator(
        "current",
        "prior",
        "yoy",
        "status",
        "break_condition",
        "source",
        "frequency",
        "as_of",
        "note",
        "notes",
    )
    @classmethod
    def _bound_scalar(cls, value: KpiScalar) -> KpiScalar:
        if isinstance(value, str) and len(value) > 2_000:
            raise ValueError("KPI scalar exceeds the 2000-character limit")
        return value


class ThesisProposalContentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["ask_proposal_content.v1"] = "ask_proposal_content.v1"
    ticker: str = Field(min_length=1, max_length=24)
    kind: Literal["thesis"] = "thesis"
    summary: str = Field(min_length=1, max_length=500)
    target_path: Literal["/thesis"] = "/thesis"
    old_value: str = Field(min_length=1, max_length=100_000)
    new_value: str = Field(min_length=1, max_length=100_000)


class KpiProposalContentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["ask_proposal_content.v1"] = "ask_proposal_content.v1"
    ticker: str = Field(min_length=1, max_length=24)
    kind: Literal["kpi"] = "kpi"
    summary: str = Field(min_length=1, max_length=500)
    target_path: KpiTargetPath
    old_value: list[KpiEntryV1] = Field(max_length=100)
    new_value: list[KpiEntryV1] = Field(max_length=100)


AskProposalContentV1: TypeAlias = ThesisProposalContentV1 | KpiProposalContentV1
_KPI_LIST_ADAPTER = TypeAdapter(list[KpiEntryV1])


class AskProposalRefV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ask_proposal_ref.v1"] = "ask_proposal_ref.v1"
    proposal_id: int = Field(gt=0)
    proposal_revision: int = Field(ge=0)
    status: ProposalStatus
    detail_url: str
    decision_url: str
    allowed_actions: list[ProposalDecision] = Field(max_length=2)


class AskProposalDetailV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ask_proposal.v1"] = "ask_proposal.v1"
    proposal_id: int = Field(gt=0)
    proposal_revision: int = Field(ge=0)
    status: ProposalStatus
    detail_url: str
    decision_url: str
    allowed_actions: list[ProposalDecision] = Field(max_length=2)
    ticker: str
    kind: Literal["thesis", "kpi"]
    summary: str
    target_path: Literal["/thesis", "/tier_1_kpis", "/tier_2_kpis", "/tier_3_kpis"]
    old_value: str | list[dict[str, KpiScalar]]
    new_value: str | list[dict[str, KpiScalar]]
    canonical_content_sha256: str = Field(min_length=64, max_length=64)
    target_precondition_sha256: str = Field(min_length=64, max_length=64)


class AskProposalDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["ask_proposal_decision.v1"] = "ask_proposal_decision.v1"
    proposal_id: int = Field(gt=0)
    decision: ProposalDecision
    expected_proposal_revision: int = Field(ge=0)
    decision_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class AskProposalDecisionReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ask_proposal_decision_receipt.v1"] = "ask_proposal_decision_receipt.v1"
    proposal_id: int = Field(gt=0)
    proposal_revision: int = Field(ge=1)
    status: Literal["approved", "rejected"]
    applied: bool
    message: str
    replayed: bool = False
    canonical_content_sha256: str = Field(min_length=64, max_length=64)
    target_postcondition_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class _AskDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    target_file: str = Field(min_length=1, max_length=256)
    target_path: str = Field(min_length=1, max_length=128)
    old_value: object
    new_value: object
    summary: str = Field(min_length=1, max_length=500)


def _status(value: str) -> ProposalStatus:
    if value not in {"pending", "approved", "rejected", "superseded"}:
        raise StoredProposalError(f"invalid governed proposal status {value!r}")
    return cast("ProposalStatus", value)


def _urls(proposal_id: int) -> tuple[str, str]:
    return (
        f"/api/research/proposals/{proposal_id}",
        f"/api/research/proposals/{proposal_id}/decision",
    )


def _ref(
    proposal_id: int,
    proposal_revision: int,
    status: ProposalStatus,
    *,
    actionable: bool = False,
) -> AskProposalRefV1:
    detail_url, decision_url = _urls(proposal_id)
    return AskProposalRefV1(
        proposal_id=proposal_id,
        proposal_revision=proposal_revision,
        status=status,
        detail_url=detail_url,
        decision_url=decision_url,
        allowed_actions=["approve", "reject"] if status == "pending" and actionable else [],
    )


def _proposal_path(repo_root: Path, ticker: str) -> Path:
    if _TICKER_RE.fullmatch(ticker) is None:
        raise ProposalValidationError("proposal ticker is invalid")
    root = repo_root.resolve()
    path = (root / "micro_thesis" / "holdings" / f"{ticker}.json").resolve()
    expected_parent = (root / "micro_thesis" / "holdings").resolve()
    if path.parent != expected_parent:
        raise ProposalValidationError("proposal target is outside the canonical holdings directory")
    return path


def _load_holdings(path: Path) -> tuple[bytes, dict[str, object]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProposalValidationError("canonical holdings file is unavailable") from exc
    if not raw or len(raw) > _MAX_HOLDINGS_BYTES:
        raise ProposalValidationError("canonical holdings file has an invalid size")
    try:
        decoded: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalValidationError("canonical holdings file is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProposalValidationError("canonical holdings JSON must be an object")
    return raw, cast("dict[str, object]", decoded)


def _kpi_value(value: object) -> list[KpiEntryV1]:
    try:
        return _KPI_LIST_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ProposalValidationError("KPI proposal value is invalid") from exc


def _dump_kpis(value: list[KpiEntryV1]) -> list[dict[str, KpiScalar]]:
    return [
        cast("dict[str, KpiScalar]", item.model_dump(mode="json", exclude_unset=True))
        for item in value
    ]


def _validated_content(
    diff: _AskDiffInput, *, ticker: str, current: dict[str, object]
) -> AskProposalContentV1:
    if str(current.get("ticker") or "").upper() != ticker:
        raise ProposalValidationError("holdings ticker does not match the proposal target")
    if diff.target_path == "/thesis":
        if not isinstance(diff.old_value, str) or not isinstance(diff.new_value, str):
            raise ProposalValidationError("thesis proposals require string old_value/new_value")
        if current.get("thesis") != diff.old_value:
            raise ProposalValidationError("proposal old_value does not match the current thesis")
        try:
            return ThesisProposalContentV1(
                ticker=ticker,
                summary=diff.summary,
                old_value=diff.old_value,
                new_value=diff.new_value,
            )
        except ValidationError as exc:
            raise ProposalValidationError("thesis proposal is invalid") from exc
    if diff.target_path not in {"/tier_1_kpis", "/tier_2_kpis", "/tier_3_kpis"}:
        raise ProposalValidationError("proposal target_path is not an approved thesis/KPI path")
    old = _kpi_value(diff.old_value)
    new = _kpi_value(diff.new_value)
    current_value = _dump_kpis(_kpi_value(current.get(diff.target_path.removeprefix("/"))))
    if current_value != _dump_kpis(old):
        raise ProposalValidationError("proposal old_value does not match the current KPI tier")
    return KpiProposalContentV1(
        ticker=ticker,
        summary=diff.summary,
        target_path=cast("KpiTargetPath", diff.target_path),
        old_value=old,
        new_value=new,
    )


def create_ask_proposal(
    raw_diff: object,
    *,
    repo_root: Path,
    db_path: Path | str,
    exchange_request_id: str,
) -> AskProposalRefV1:
    """Validate and persist one immutable actionable Ask diff."""

    if (
        not 1 <= len(exchange_request_id) <= 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", exchange_request_id) is None
    ):
        raise ProposalValidationError("Ask exchange request_id is invalid")

    try:
        diff = _AskDiffInput.model_validate(raw_diff)
    except ValidationError as exc:
        raise ProposalValidationError("Ask proposal diff is invalid") from exc
    match = _TARGET_FILE_RE.fullmatch(diff.target_file)
    if match is None:
        raise ProposalValidationError("proposal target_file is not a canonical holdings file")
    ticker = match.group(1)
    path = _proposal_path(repo_root, ticker)
    target_bytes, current = _load_holdings(path)
    content = _validated_content(diff, ticker=ticker, current=current)
    content_payload: dict[str, object] = {
        "schema_version": "ask_proposal_content.v1",
        "ticker": content.ticker,
        "kind": content.kind,
        "summary": content.summary,
        "target_path": content.target_path,
        "old_value": (
            content.old_value
            if isinstance(content, ThesisProposalContentV1)
            else _dump_kpis(content.old_value)
        ),
        "new_value": (
            content.new_value
            if isinstance(content, ThesisProposalContentV1)
            else _dump_kpis(content.new_value)
        ),
    }
    content_json = _canonical_json(content_payload)
    content_sha256 = _sha256_bytes(content_json.encode("utf-8"))
    proposal_id = create_proposal(
        task_id=None,
        kind="ask_thesis_edit" if content.kind == "thesis" else "ask_kpi_edit",
        ticker=ticker,
        title=content.summary,
        body_md=content.summary,
        evidence_json="[]",
        provenance="derived",
        canonical_content_json=content_json,
        canonical_content_sha256=content_sha256,
        target_precondition_sha256=_sha256_bytes(target_bytes),
        ask_exchange_request_id=exchange_request_id,
        db_path=db_path,
    )
    return _ref(proposal_id, 0, "pending")


def _content(prop: ResearchProposal) -> AskProposalContentV1:
    raw = prop.canonical_content_json
    expected = prop.canonical_content_sha256
    if raw is None or expected is None or _sha256_bytes(raw.encode("utf-8")) != expected:
        raise StoredProposalError("governed proposal canonical content hash mismatch")
    try:
        parsed: object = json.loads(raw)
        if not isinstance(parsed, dict):
            raise StoredProposalError("governed proposal canonical content is not an object")
        payload = cast("dict[str, object]", parsed)
        if payload.get("kind") == "thesis":
            return ThesisProposalContentV1.model_validate(payload)
        if payload.get("kind") == "kpi":
            return KpiProposalContentV1.model_validate(payload)
        raise StoredProposalError("governed proposal kind is invalid")
    except (json.JSONDecodeError, ValidationError) as exc:
        raise StoredProposalError("governed proposal canonical content is invalid") from exc


def _detail(prop: ResearchProposal) -> AskProposalDetailV1:
    content = _content(prop)
    precondition = prop.target_precondition_sha256
    content_sha = prop.canonical_content_sha256
    if precondition is None or content_sha is None:
        raise StoredProposalError("governed proposal hashes are missing")
    ref = _ref(
        prop.id,
        prop.proposal_revision,
        _status(prop.status),
        actionable=prop.actionable_at is not None and prop.invalidated_at is None,
    )
    return AskProposalDetailV1(
        **ref.model_dump(exclude={"schema_version"}),
        ticker=content.ticker,
        kind=content.kind,
        summary=content.summary,
        target_path=content.target_path,
        old_value=(
            content.old_value
            if isinstance(content, ThesisProposalContentV1)
            else _dump_kpis(content.old_value)
        ),
        new_value=(
            content.new_value
            if isinstance(content, ThesisProposalContentV1)
            else _dump_kpis(content.new_value)
        ),
        canonical_content_sha256=content_sha,
        target_precondition_sha256=precondition,
    )


def get_ask_proposal_detail(proposal_id: int, *, db_path: Path | str) -> AskProposalDetailV1 | None:
    prop = get_proposal(proposal_id, db_path=db_path)
    if prop is None or prop.canonical_content_json is None:
        return None
    return _detail(prop)


def activate_ask_proposal(
    reference: AskProposalRefV1,
    *,
    exchange_request_id: str,
    connection: sqlite3.Connection,
    timestamp: str,
) -> AskProposalRefV1:
    """Make a pending proposal actionable inside the exchange-completion transaction."""

    exchange = connection.execute(
        "SELECT status FROM ask_exchanges WHERE request_id=?",
        (exchange_request_id,),
    ).fetchone()
    if exchange is None or str(exchange["status"]) != "pending":
        raise StoredProposalError("proposal activation requires its pending Ask exchange")
    updated = connection.execute(
        "UPDATE research_proposals SET actionable_at=?, updated_at=? "
        "WHERE id=? AND ask_exchange_request_id=? AND status='pending' "
        "AND actionable_at IS NULL AND invalidated_at IS NULL",
        (timestamp, timestamp, reference.proposal_id, exchange_request_id),
    )
    if updated.rowcount != 1:
        row = connection.execute(
            "SELECT * FROM research_proposals WHERE id=? AND ask_exchange_request_id=?",
            (reference.proposal_id, exchange_request_id),
        ).fetchone()
        if row is None:
            raise StoredProposalError("proposal is not linked to this Ask exchange")
        prop = _row_to_proposal(row)
        if prop.status != "pending" or prop.invalidated_at is not None:
            raise StoredProposalError("proposal cannot be activated")
    return _ref(reference.proposal_id, reference.proposal_revision, "pending", actionable=True)


def invalidate_exchange_proposals(
    exchange_request_id: str,
    *,
    connection: sqlite3.Connection,
    timestamp: str,
    reason: str,
) -> None:
    """Fail closed for every proposal emitted by an exchange that did not commit."""

    connection.execute(
        "UPDATE research_proposals SET status='superseded', "
        "proposal_revision=proposal_revision+1, invalidated_at=?, invalidation_reason=?, "
        "updated_at=? WHERE ask_exchange_request_id=? AND status='pending'",
        (timestamp, reason[:256], timestamp, exchange_request_id),
    )


def _row_to_proposal(row: sqlite3.Row) -> ResearchProposal:
    """Map the approval transaction's selected row without opening another connection."""

    return ResearchProposal(
        id=int(row["id"]),
        task_id=None if row["task_id"] is None else int(row["task_id"]),
        kind=str(row["kind"]),
        ticker=None if row["ticker"] is None else str(row["ticker"]),
        title=str(row["title"]),
        body_md="" if row["body_md"] is None else str(row["body_md"]),
        evidence_json="[]" if row["evidence_json"] is None else str(row["evidence_json"]),
        status=str(row["status"]),
        adversarial_verdict=(
            None if row["adversarial_verdict"] is None else str(row["adversarial_verdict"])
        ),
        budget_tier=None if row["budget_tier"] is None else str(row["budget_tier"]),
        provenance=str(row["provenance"]),
        tainted_by_proposal_id=(
            None if row["tainted_by_proposal_id"] is None else int(row["tainted_by_proposal_id"])
        ),
        artifact_json=None if row["artifact_json"] is None else str(row["artifact_json"]),
        canonical_content_json=str(row["canonical_content_json"]),
        canonical_content_sha256=str(row["canonical_content_sha256"]),
        proposal_revision=int(row["proposal_revision"]),
        target_precondition_sha256=str(row["target_precondition_sha256"]),
        target_postcondition_sha256=(
            None
            if row["target_postcondition_sha256"] is None
            else str(row["target_postcondition_sha256"])
        ),
        ask_exchange_request_id=(
            None if row["ask_exchange_request_id"] is None else str(row["ask_exchange_request_id"])
        ),
        actionable_at=None if row["actionable_at"] is None else str(row["actionable_at"]),
        invalidated_at=(None if row["invalidated_at"] is None else str(row["invalidated_at"])),
        invalidation_reason=(
            None if row["invalidation_reason"] is None else str(row["invalidation_reason"])
        ),
    )


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()


def _apply_content(content: AskProposalContentV1, payload: dict[str, object]) -> dict[str, object]:
    updated = dict(payload)
    if isinstance(content, ThesisProposalContentV1):
        updated["thesis"] = content.new_value
    else:
        updated[content.target_path.removeprefix("/")] = _dump_kpis(content.new_value)
    sanitize_holdings_scalars(updated)
    if str(updated.get("ticker") or "").upper() != content.ticker:
        raise ProposalValidationError("holdings ticker changed during approval")
    HoldingsSpec.model_validate(updated)
    for target in ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis"):
        updated[target] = _dump_kpis(_kpi_value(updated.get(target, [])))
    seen_kpis: dict[str, str] = {}
    for target in ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis"):
        for item in _kpi_value(updated[target]):
            normalized = item.name.casefold()
            previous = seen_kpis.get(normalized)
            if previous is not None:
                raise ProposalValidationError(
                    f"KPI {item.name!r} appears in both {previous} and {target}"
                )
            seen_kpis[normalized] = target
    encoded = json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > _MAX_HOLDINGS_BYTES:
        raise ProposalValidationError("approved holdings payload exceeds the size limit")
    return updated


def _target_value(content: AskProposalContentV1, payload: dict[str, object]) -> object:
    value = payload.get(content.target_path.removeprefix("/"))
    if isinstance(content, ThesisProposalContentV1):
        return value
    return _dump_kpis(_kpi_value(value))


def _sync_after_replace(
    connection: sqlite3.Connection,
    *,
    repo_root: Path,
    content: AskProposalContentV1,
) -> None:
    from compute.thesis_evaluator import refresh_thesis_mirror

    refresh_thesis_mirror(
        connection,
        content.ticker,
        repo_root / "micro_thesis" / "holdings",
    )
    if isinstance(content, ThesisProposalContentV1):
        stamp = now_iso()
        connection.execute(
            "INSERT INTO thesis_ledger_entries "
            "(user_id,ticker,entry_kind,body,source_alert_id,created_at,accepted_at) "
            "VALUES (?,?,?,?,NULL,?,?)",
            (DEFAULT_USER_ID, content.ticker, "thesis_update", content.new_value, stamp, stamp),
        )


@dataclass(frozen=True, slots=True)
class CanonicalAskApplyResult:
    content: AskProposalContentV1
    holdings_payload: dict[str, object]
    target_postcondition_sha256: str
    recovered: bool


def apply_canonical_ask_change(
    proposal: ResearchProposal,
    *,
    repo_root: Path,
    connection: sqlite3.Connection,
    after_replace: Callable[[], None] | None = None,
) -> CanonicalAskApplyResult:
    """Validate, atomically replace, and mirror one canonical holdings change."""

    content = _content(proposal)
    path = _proposal_path(repo_root, content.ticker)
    current_bytes, current_payload = _load_holdings(path)
    actual_sha256 = _sha256_bytes(current_bytes)
    proposed_payload = _apply_content(content, current_payload)
    proposed_bytes = json.dumps(proposed_payload, ensure_ascii=False, indent=2).encode("utf-8")
    proposed_sha256 = _sha256_bytes(proposed_bytes)
    expected_target = (
        content.new_value
        if isinstance(content, ThesisProposalContentV1)
        else _dump_kpis(content.new_value)
    )
    already_applied = (
        actual_sha256 == proposed_sha256
        and _target_value(content, current_payload) == expected_target
    )
    if not already_applied:
        if actual_sha256 != proposal.target_precondition_sha256:
            raise TargetDriftError(
                proposal_id=proposal.id,
                expected_target_sha256=str(proposal.target_precondition_sha256),
                actual_target_sha256=actual_sha256,
            )
        _atomic_replace(path, proposed_bytes)
        if after_replace is not None:
            after_replace()
    _sync_after_replace(connection, repo_root=repo_root, content=content)
    return CanonicalAskApplyResult(
        content=content,
        holdings_payload=proposed_payload,
        target_postcondition_sha256=proposed_sha256,
        recovered=already_applied,
    )


def _receipt_from_row(
    row: sqlite3.Row, *, request_sha256: str, proposal_id: int
) -> AskProposalDecisionReceiptV1:
    if str(row["request_sha256"]) != request_sha256:
        raise ProposalConflictError(
            "idempotency_conflict",
            "decision_request_id was already used for a different payload",
            proposal_id=proposal_id,
        )
    try:
        parsed: object = json.loads(str(row["response_json"]))
        receipt = AskProposalDecisionReceiptV1.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise StoredProposalError("stored proposal decision receipt is invalid") from exc
    if _sha256_json(receipt.model_dump(mode="json")) != str(row["response_sha256"]):
        raise StoredProposalError("stored proposal decision receipt hash mismatch")
    return receipt.model_copy(update={"replayed": True})


def decide_ask_proposal(
    request: AskProposalDecisionV1,
    *,
    repo_root: Path,
    db_path: Path | str,
    after_replace: Callable[[], None] | None = None,
) -> AskProposalDecisionReceiptV1:
    """CAS one explicit decision and return its durable idempotent receipt."""

    request_payload = request.model_dump(mode="json")
    request_sha256 = _sha256_json(request_payload)
    resolved_db = Path(db_path).resolve()
    with hold_run_lock(
        resolved_db,
        owner="ask_proposal_approval",
        timeout_s=5.0,
        poll_s=0.05,
    ):
        connection = open_conn(resolved_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            receipt_row = connection.execute(
                "SELECT * FROM research_proposal_decision_receipts WHERE decision_request_id=?",
                (request.decision_request_id,),
            ).fetchone()
            if receipt_row is not None:
                receipt = _receipt_from_row(
                    receipt_row,
                    request_sha256=request_sha256,
                    proposal_id=request.proposal_id,
                )
                connection.rollback()
                return receipt
            row = connection.execute(
                "SELECT * FROM research_proposals WHERE id=?",
                (request.proposal_id,),
            ).fetchone()
            if row is None or row["canonical_content_json"] is None:
                raise ProposalConflictError(
                    "proposal_not_found",
                    "governed proposal was not found",
                    proposal_id=request.proposal_id,
                )
            prop = _row_to_proposal(row)
            detail = _detail(prop)
            if request.expected_proposal_revision != detail.proposal_revision:
                raise ProposalConflictError(
                    "revision_conflict",
                    "proposal revision does not match",
                    proposal_id=prop.id,
                    current_proposal_revision=detail.proposal_revision,
                    current_status=detail.status,
                )
            if detail.status != "pending":
                raise ProposalConflictError(
                    "status_conflict",
                    "proposal is no longer pending",
                    proposal_id=prop.id,
                    current_proposal_revision=detail.proposal_revision,
                    current_status=detail.status,
                )
            if prop.actionable_at is None or prop.invalidated_at is not None:
                raise ProposalConflictError(
                    "proposal_inactive",
                    "proposal is not actionable because its Ask exchange did not complete",
                    proposal_id=prop.id,
                    current_proposal_revision=detail.proposal_revision,
                    current_status=detail.status,
                )
            exchange_row = connection.execute(
                "SELECT status FROM ask_exchanges WHERE request_id=?",
                (prop.ask_exchange_request_id,),
            ).fetchone()
            if exchange_row is None or str(exchange_row["status"]) != "completed":
                raise ProposalConflictError(
                    "proposal_inactive",
                    "proposal is not actionable because its Ask exchange is unavailable",
                    proposal_id=prop.id,
                    current_proposal_revision=detail.proposal_revision,
                    current_status=detail.status,
                )
            next_revision = detail.proposal_revision + 1
            target_postcondition_sha256: str | None = None
            applied = False
            if request.decision == "approve":
                from research.apply import MutationApplyResult, apply_governed_proposal

                result = apply_governed_proposal(
                    prop,
                    proposal_id=prop.id,
                    db_path=resolved_db,
                    steer_authorized=True,
                    repo_root=repo_root,
                    connection=connection,
                    after_replace=after_replace,
                )
                if not isinstance(result, MutationApplyResult) or not result.applied:
                    raise StoredProposalError("governed Ask proposal applier did not apply")
                target_postcondition_sha256 = result.target_postcondition_sha256
                applied = result.applied
                status: Literal["approved", "rejected"] = "approved"
                message = result.message
            else:
                status = "rejected"
                message = "Rejected; no canonical data was changed"
            updated = connection.execute(
                "UPDATE research_proposals SET status=?, proposal_revision=?, "
                "target_postcondition_sha256=?, updated_at=? "
                "WHERE id=? AND status='pending' AND proposal_revision=?",
                (
                    status,
                    next_revision,
                    target_postcondition_sha256,
                    now_iso(),
                    prop.id,
                    detail.proposal_revision,
                ),
            )
            if updated.rowcount != 1:
                raise ProposalConflictError(
                    "revision_conflict",
                    "proposal changed during approval",
                    proposal_id=prop.id,
                    current_proposal_revision=detail.proposal_revision,
                    current_status=detail.status,
                )
            receipt = AskProposalDecisionReceiptV1(
                proposal_id=prop.id,
                proposal_revision=next_revision,
                status=status,
                applied=applied,
                message=message,
                canonical_content_sha256=detail.canonical_content_sha256,
                target_postcondition_sha256=target_postcondition_sha256,
            )
            response_json = _canonical_json(receipt.model_dump(mode="json"))
            connection.execute(
                "INSERT INTO research_proposal_decision_receipts "
                "(decision_request_id,proposal_id,request_sha256,decision,"
                " expected_proposal_revision,response_json,response_sha256,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    request.decision_request_id,
                    prop.id,
                    request_sha256,
                    request.decision,
                    request.expected_proposal_revision,
                    response_json,
                    _sha256_bytes(response_json.encode("utf-8")),
                    now_iso(),
                ),
            )
            connection.commit()
            return receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def bind_ask_proposal_events(
    events: Iterable[dict[str, object]],
    *,
    repo_root: Path,
    db_path: Path | str,
    exchange_request_id: str,
) -> Iterator[dict[str, object]]:
    """Replace an untrusted engine diff with its durable governed reference."""

    for event in events:
        if event.get("type") != "diff_proposal":
            yield event
            continue
        raw_diff = event.get("diff")
        try:
            reference = create_ask_proposal(
                raw_diff,
                repo_root=repo_root,
                db_path=db_path,
                exchange_request_id=exchange_request_id,
            )
        except ProposalValidationError:
            yield {
                "type": "proposal_error",
                "code": "registration_failed",
                "message": "proposal could not be registered as an actionable thesis/KPI change",
                "error": "proposal could not be registered as an actionable thesis/KPI change",
            }
            continue
        yield {"type": "proposal_ref", "proposal_ref": reference.model_dump(mode="json")}
