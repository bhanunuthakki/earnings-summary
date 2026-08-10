"""Narrow, read-only Company Desk contract for the Work OS.

The Desk composes stable governed identities from the repository database and
the compact report-artifact index.  It never runs research, scans report
directories, or invokes an LLM while a screen is loading.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from decision_conditions import conditions_from_json
from report.artifacts import ReportArtifactRef, load_report_artifact_index

CoverageRole = Literal["portfolio", "evaluation", "unknown"]


class CompanyIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str
    coverage_role: CoverageRole


class DeskPositionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    weight_pct: float | None = None
    market_value: float | None = None
    price: float | None = None
    fair_value: float | None = None
    currency: str | None = None
    as_of: str | None = None
    source: str | None = None


class DeskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: int
    revision: str
    made_at: str
    owner_state: str | None
    model_recommendation: str | None
    target_weight_pct: float | None
    conviction: str | None
    source_lens: str | None


class DeskCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    decision_id: int
    revision: str
    metric: str
    operator: str
    threshold: float
    unit: str
    for_periods: int
    note: str | None
    evidence_ref: str | None


class DeskQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    note_id: int
    revision: str
    body: str
    owner: Literal["owner", "model", "system"]
    evidence_ref: str | None


class CompanyDeskResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["company_desk.v1"] = "company_desk.v1"
    status: Literal["ok", "degraded"]
    generated_at: str
    company: CompanyIdentity
    position: DeskPositionSnapshot
    current_decision: DeskDecision | None
    conditions: list[DeskCondition]
    open_questions: list[DeskQuestion]
    latest_brief: ReportArtifactRef | None
    warnings: list[str]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _company(conn: sqlite3.Connection, ticker: str) -> CompanyIdentity | None:
    try:
        row = conn.execute(
            "SELECT ticker, name, list_type FROM tracked_companies "
            "WHERE UPPER(ticker) = ? AND archived_at IS NULL LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    raw_role = str(row["list_type"] or "unknown").lower()
    if raw_role == "portfolio":
        role: CoverageRole = "portfolio"
    elif raw_role == "evaluation":
        role = "evaluation"
    else:
        role = "unknown"
    return CompanyIdentity(ticker=ticker, name=str(row["name"] or ticker), coverage_role=role)


def _decision(
    conn: sqlite3.Connection, ticker: str
) -> tuple[DeskDecision | None, list[DeskCondition], list[str]]:
    columns = _table_columns(conn, "decisions")
    required = {"id", "ticker", "recommendation_kind", "made_at"}
    if not required <= columns:
        return None, [], ["decision_store_unavailable"]
    selected = ["id", "recommendation_kind", "made_at"]
    for optional in (
        "recommendation_value",
        "conviction",
        "source_lens",
        "decided_by",
        "decision_conditions",
    ):
        if optional in columns:
            selected.append(optional)
    try:
        base_query = (
            f"SELECT {', '.join(selected)} FROM decisions WHERE UPPER(ticker) = ?"
        )
        if "decided_by" in columns:
            owner_row = conn.execute(
                base_query
                + " AND decided_by = 'owner' ORDER BY made_at DESC, id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            model_row = conn.execute(
                base_query
                + " AND decided_by != 'owner' ORDER BY made_at DESC, id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        else:
            owner_row = None
            model_row = conn.execute(
                base_query + " ORDER BY made_at DESC, id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
    except sqlite3.Error:
        return None, [], ["decision_store_unavailable"]
    row = owner_row or model_row
    if row is None:
        return None, [], []
    values = dict(row)
    decision_id = int(values["id"])
    made_at = str(values["made_at"])
    warnings: list[str] = []
    if "decided_by" not in columns:
        warnings.append("decision_owner_provenance_unavailable")
    owner_state = (
        str(owner_row["recommendation_kind"])
        if owner_row is not None
        else None
    )
    model_recommendation = (
        str(model_row["recommendation_kind"])
        if model_row is not None
        else None
    )
    raw_value = values.get("recommendation_value")
    target = float(raw_value) if isinstance(raw_value, (int, float)) else None
    if target is not None and abs(target) <= 1:
        target *= 100
    decision = DeskDecision(
        decision_id=decision_id,
        revision=made_at,
        made_at=made_at,
        owner_state=owner_state,
        model_recommendation=model_recommendation,
        target_weight_pct=target,
        conviction=(str(values["conviction"]) if values.get("conviction") is not None else None),
        source_lens=(str(values["source_lens"]) if values.get("source_lens") is not None else None),
    )
    conditions: list[DeskCondition] = []
    raw_conditions = values.get("decision_conditions")
    for index, condition in enumerate(conditions_from_json(raw_conditions), start=0):
        conditions.append(
            DeskCondition(
                stable_id=f"decision:{decision_id}:condition:{index}",
                decision_id=decision_id,
                revision=made_at,
                metric=condition.metric,
                operator=condition.op,
                threshold=condition.threshold,
                unit=condition.unit,
                for_periods=condition.for_periods,
                note=condition.note,
                evidence_ref=condition.metric_source,
            )
        )
    return decision, conditions, warnings


def _questions(conn: sqlite3.Connection, ticker: str) -> tuple[list[DeskQuestion], list[str]]:
    columns = _table_columns(conn, "analyst_notes")
    required = {"id", "ticker", "kind", "status", "body", "source", "updated_at"}
    if not required <= columns:
        return [], ["analyst_notes_unavailable"]
    try:
        optional_ref = (
            "COALESCE(fact_ref, source_ref)" if {"fact_ref", "source_ref"} <= columns else "NULL"
        )
        notes = conn.execute(
            "SELECT id, body, source, updated_at, "
            f"{optional_ref} AS evidence_ref FROM analyst_notes "
            "WHERE UPPER(ticker) = ? AND kind = 'question' AND status = 'open' "
            "ORDER BY created_at DESC, id DESC LIMIT 20",
            (ticker,),
        ).fetchall()
    except sqlite3.Error:
        return [], ["analyst_notes_unavailable"]
    questions = [
        DeskQuestion(
            stable_id=f"analyst_note:{int(note['id'])}",
            note_id=int(note["id"]),
            revision=str(note["updated_at"]),
            body=str(note["body"]),
            owner=(
                "owner" if str(note["source"]) in {"manual", "capture", "comment"} else "model"
            ),
            evidence_ref=(str(note["evidence_ref"]) if note["evidence_ref"] is not None else None),
        )
        for note in notes
    ]
    return questions, []


def _latest_brief(repo_root: Path, ticker: str) -> ReportArtifactRef | None:
    index = load_report_artifact_index(repo_root)
    return next((item for item in index.items if item.ticker == ticker), None)


def build_company_desk(
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    generated_at: datetime | None = None,
) -> CompanyDeskResponse:
    """Build one cheap Desk snapshot from an existing request connection."""

    normalized = ticker.strip().upper()
    company = _company(conn, normalized)
    if company is None:
        raise LookupError(normalized)
    decision, conditions, decision_warnings = _decision(conn, normalized)
    questions, question_warnings = _questions(conn, normalized)
    latest_brief = _latest_brief(repo_root, normalized)
    warnings = [
        "position_snapshot_unavailable",
        *decision_warnings,
        *question_warnings,
    ]
    if latest_brief is None:
        warnings.append("research_brief_unavailable")
    built_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    return CompanyDeskResponse(
        status="degraded" if warnings else "ok",
        generated_at=built_at.isoformat().replace("+00:00", "Z"),
        company=company,
        position=DeskPositionSnapshot(),
        current_decision=decision,
        conditions=conditions,
        open_questions=questions,
        latest_brief=latest_brief,
        warnings=warnings,
    )


__all__ = ["CompanyDeskResponse", "build_company_desk"]
