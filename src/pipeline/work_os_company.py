"""Narrow, read-only Company Desk contract for the Work OS.

The Desk composes stable governed identities from the repository database and
the compact report-artifact index.  It never runs research, scans report
directories, or invokes an LLM while a screen is loading.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from calendar_clock import calendar_today
from dcf.latest import latest_dcf_row
from decision_conditions import conditions_from_json
from pipeline.earnings_doorway import (
    POST_EARNINGS_WINDOW_DAYS,
    PRE_EARNINGS_WINDOW_DAYS,
    EarningsDoorway,
    resolve_earnings_doorway,
)
from provenance.selection import selected_transcripts_relation
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
    price_as_of: str | None = None
    fair_value_as_of: str | None = None


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
    earnings_doorway: EarningsDoorway
    warnings: list[str]


_PRE_EARNINGS_PURPOSE = "pre_earnings_brief"
_POST_EARNINGS_PURPOSE = "post_earnings_readout"


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _calendar_event_date(conn: sqlite3.Connection, ticker: str, today: date) -> date | None:
    try:
        rows = conn.execute(
            "SELECT expected_date FROM expected_earnings WHERE UPPER(ticker) = ?",
            (ticker,),
        ).fetchall()
    except sqlite3.Error:
        return None
    candidates = [parsed for row in rows if (parsed := _parse_date(row[0])) is not None]
    eligible = [
        candidate
        for candidate in candidates
        if -POST_EARNINGS_WINDOW_DAYS <= (today - candidate).days <= PRE_EARNINGS_WINDOW_DAYS
    ]
    return min(eligible, key=lambda candidate: abs((today - candidate).days)) if eligible else None


def _release_for_period(conn: sqlite3.Connection, ticker: str, period_end: date) -> date | None:
    try:
        rows = conn.execute(
            "SELECT release_date FROM earnings_surprises WHERE UPPER(ticker) = ?",
            (ticker,),
        ).fetchall()
    except sqlite3.Error:
        return None
    releases = [parsed for row in rows if (parsed := _parse_date(row[0])) is not None]
    in_quarter_window = [release for release in releases if 0 <= (release - period_end).days <= 120]
    return min(in_quarter_window) if in_quarter_window else None


def _latest_reported_period(
    conn: sqlite3.Connection, ticker: str
) -> tuple[str, date | None] | None:
    try:
        relation = selected_transcripts_relation(conn)
        row = conn.execute(
            f"SELECT period_end, call_date FROM {relation.sql} "  # nosec B608 -- trusted selected-relation shape; ticker remains bound
            "WHERE UPPER(ticker) = ? AND period_end IS NOT NULL "
            "AND (UPPER(COALESCE(fiscal_period_type, '')) GLOB 'Q[1-4]*' "
            "OR UPPER(COALESCE(fiscal_period_type, '')) = 'QUARTER') "
            "ORDER BY period_end DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    period_end = _parse_date(row[0])
    if period_end is None:
        return None
    event_date = _release_for_period(conn, ticker, period_end) or _parse_date(row[1])
    return period_end.isoformat(), event_date


def _actual_event_date(conn: sqlite3.Connection, ticker: str, today: date) -> date | None:
    candidates: list[date] = []
    try:
        rows = conn.execute(
            "SELECT release_date FROM earnings_surprises WHERE UPPER(ticker) = ?",
            (ticker,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    candidates.extend(parsed for row in rows if (parsed := _parse_date(row[0])) is not None)
    latest_period = _latest_reported_period(conn, ticker)
    if latest_period is not None and latest_period[1] is not None:
        candidates.append(latest_period[1])
    eligible = [
        candidate
        for candidate in candidates
        if 0 <= (today - candidate).days <= POST_EARNINGS_WINDOW_DAYS
    ]
    return max(eligible) if eligible else None


def _artifact_exists(
    conn: sqlite3.Connection,
    ticker: str,
    purpose: str,
    fiscal_period: str,
) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM llm_artifacts "
            "WHERE UPPER(COALESCE(ticker, '')) = ? AND scope = 'ticker' "
            "AND purpose = ? AND fiscal_period = ? AND superseded_by_id IS NULL "
            "AND TRIM(COALESCE(content_md, '')) != '' LIMIT 1",
            (ticker, purpose, fiscal_period),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def build_earnings_doorway(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    today: date,
) -> EarningsDoorway:
    """Resolve one artifact-backed doorway from persisted event evidence."""

    event_date = _actual_event_date(conn, ticker, today) or _calendar_event_date(
        conn, ticker, today
    )
    pre_route = None
    post_route = None
    if event_date is not None and _artifact_exists(
        conn,
        ticker,
        _PRE_EARNINGS_PURPOSE,
        event_date.isoformat(),
    ):
        pre_route = f"/api/peek/earnings-prep?ticker={ticker}"
    latest_period = _latest_reported_period(conn, ticker)
    if (
        event_date is not None
        and latest_period is not None
        and latest_period[1] == event_date
        and _artifact_exists(conn, ticker, _POST_EARNINGS_PURPOSE, latest_period[0])
    ):
        post_route = f"/api/peek/earnings-readout?ticker={ticker}"
    return resolve_earnings_doorway(
        today=today,
        event_date=event_date,
        pre_route=pre_route,
        post_route=post_route,
    )


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
    required = {
        "id",
        "ticker",
        "recommendation_kind",
        "recommendation_value",
        "conviction",
        "source_lens",
        "decided_by",
        "decision_conditions",
        "made_at",
    }
    if not required <= columns:
        return None, [], ["decision_store_unavailable"]
    try:
        owner_row = conn.execute(
            "SELECT id, recommendation_kind, recommendation_value, conviction, "
            "source_lens, decided_by, decision_conditions, made_at FROM decisions "
            "WHERE UPPER(ticker) = ? AND decided_by = 'owner' "
            "ORDER BY made_at DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        model_row = conn.execute(
            "SELECT id, recommendation_kind, recommendation_value, conviction, "
            "source_lens, decided_by, decision_conditions, made_at FROM decisions "
            "WHERE UPPER(ticker) = ? AND decided_by != 'owner' "
            "ORDER BY made_at DESC, id DESC LIMIT 1",
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
    owner_state = str(owner_row["recommendation_kind"]) if owner_row is not None else None
    model_recommendation = str(model_row["recommendation_kind"]) if model_row is not None else None
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
    required = {
        "id",
        "ticker",
        "kind",
        "status",
        "body",
        "source",
        "source_ref",
        "fact_ref",
        "created_at",
        "updated_at",
    }
    if not required <= columns:
        return [], ["analyst_notes_unavailable"]
    try:
        notes = conn.execute(
            "SELECT id, body, source, updated_at, "
            "COALESCE(fact_ref, source_ref) AS evidence_ref FROM analyst_notes "
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
            owner=("owner" if str(note["source"]) in {"manual", "capture", "comment"} else "model"),
            evidence_ref=(str(note["evidence_ref"]) if note["evidence_ref"] is not None else None),
        )
        for note in notes
    ]
    return questions, []


def _latest_brief(repo_root: Path, ticker: str) -> ReportArtifactRef | None:
    index = load_report_artifact_index(repo_root)
    return next((item for item in index.items if item.ticker == ticker), None)


def _position_snapshot(conn: sqlite3.Connection, ticker: str) -> DeskPositionSnapshot:
    """Return the latest governed DCF basis without inventing a live position."""

    dcf = latest_dcf_row(conn, ticker)
    if dcf is None:
        return DeskPositionSnapshot()
    return DeskPositionSnapshot(
        price=dcf.live_price,
        fair_value=dcf.npv_per_share,
        currency=dcf.currency,
        as_of=dcf.live_price_at or dcf.valuation_date,
        source="latest_governed_dcf_run",
        price_as_of=dcf.live_price_at,
        fair_value_as_of=dcf.valuation_date,
    )


def build_company_desk(
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    generated_at: datetime | None = None,
    today: date | None = None,
) -> CompanyDeskResponse:
    """Build one cheap Desk snapshot from an existing request connection."""

    normalized = ticker.strip().upper()
    company = _company(conn, normalized)
    if company is None:
        raise LookupError(normalized)
    decision, conditions, decision_warnings = _decision(conn, normalized)
    questions, question_warnings = _questions(conn, normalized)
    latest_brief = _latest_brief(repo_root, normalized)
    earnings_doorway = build_earnings_doorway(
        conn,
        normalized,
        today=today or calendar_today(generated_at),
    )
    position = _position_snapshot(conn, normalized)
    warnings = [*decision_warnings, *question_warnings]
    if position.price is None and position.fair_value is None:
        warnings.insert(0, "position_snapshot_unavailable")
    if latest_brief is None:
        warnings.append("research_brief_unavailable")
    built_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    return CompanyDeskResponse(
        status="degraded" if warnings else "ok",
        generated_at=built_at.isoformat().replace("+00:00", "Z"),
        company=company,
        position=position,
        current_decision=decision,
        conditions=conditions,
        open_questions=questions,
        latest_brief=latest_brief,
        earnings_doorway=earnings_doorway,
        warnings=warnings,
    )


__all__ = ["CompanyDeskResponse", "build_company_desk", "build_earnings_doorway"]
