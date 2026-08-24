"""Narrow, read-only Company Desk contract for the Work OS.

The Desk composes stable governed identities from the repository database and
the compact report-artifact index.  It never runs research, scans report
directories, or invokes an LLM while a screen is loading.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from calendar_clock import calendar_today
from dcf.latest import latest_dcf_row
from integrations.portfolio_tracker_client import LivePortfolio
from pipeline.earnings_doorway import (
    POST_EARNINGS_WINDOW_DAYS,
    PRE_EARNINGS_WINDOW_DAYS,
    EarningsDoorway,
    resolve_earnings_doorway,
)
from pipeline.work_os_briefs import BriefLibraryItem, build_brief_descriptor
from pipeline.work_os_decisions import (
    DecisionCondition,
    DecisionProjection,
    build_decision_projection,
)
from pipeline.work_os_earnings import EarningsReadoutSummary, load_latest_earnings_readouts
from provenance.selection import selected_transcripts_relation
from report.artifacts import load_report_artifact_index
from report.models import SectionStatus, ThesisSection
from report.sections import thesis as thesis_section

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
    position_state: Literal["held", "not_held", "unavailable"] = "unavailable"
    position_source: Literal["portfolio_tracker_api"] | None = None
    position_as_of: str | None = None
    price: float | None = None
    fair_value: float | None = None
    currency: str | None = None
    as_of: str | None = None
    source: str | None = None
    price_as_of: str | None = None
    fair_value_as_of: str | None = None


class DeskQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    note_id: int
    revision: str
    body: str
    origin: Literal["owner", "model", "thesis", "engagement", "system"]
    approval: Literal["owner-authored", "owner-approved", "system"]
    evidence_ref: str | None


class DeskKpiEvidence(BaseModel):
    """One Tier-1 KPI only when its canonical series is safe to surface."""

    model_config = ConfigDict(frozen=True)

    name: str
    tier: Literal["tier_1"]
    unit: str | None = None
    latest_period: str
    latest_value: float
    current_status: Literal["green", "yellow", "red"]
    evidence_ref: str


class ThesisRiskProjection(BaseModel):
    """Read-only thesis state, withheld unless its evaluated facts are current."""

    model_config = ConfigDict(frozen=True)

    status: Literal["available", "unavailable"]
    thesis: str | None = None
    overall_breach_status: Literal["ok", "warn", "breach", "unresolved"] | None = None
    evaluated_at: str | None = None
    break_conditions: list[str] = []
    unavailable_reason: Literal["missing", "stale", "incomplete"] | None = None


class KpiSummaryProjection(BaseModel):
    """Exact-evidence Tier-1 KPI summary for the Company Desk."""

    model_config = ConfigDict(frozen=True)

    status: Literal["available", "unavailable"]
    items: list[DeskKpiEvidence] = []
    unavailable_reason: Literal["missing", "stale", "incomplete"] | None = None


class CompanyDeskResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["company_desk.v1"] = "company_desk.v1"
    status: Literal["ok", "degraded"]
    generated_at: str
    company: CompanyIdentity
    position: DeskPositionSnapshot
    current_decision: DecisionProjection
    conditions: list[DecisionCondition]
    open_questions: list[DeskQuestion]
    question_store_status: Literal["ok", "unavailable"]
    latest_brief: BriefLibraryItem | None
    latest_earnings_readout: EarningsReadoutSummary | None
    earnings_doorway: EarningsDoorway
    thesis_risk: ThesisRiskProjection
    kpi_summary: KpiSummaryProjection
    warnings: list[str]
    say_do: dict[str, object] | None = None


_PRE_EARNINGS_PURPOSE = "pre_earnings_brief"
_POST_EARNINGS_PURPOSE = "post_earnings_readout"
_THESIS_FACT_FRESHNESS_DAYS = 180


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


def _questions(
    conn: sqlite3.Connection, ticker: str
) -> tuple[list[DeskQuestion], Literal["ok", "unavailable"], list[str]]:
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
        return [], "unavailable", ["analyst_notes_unavailable"]
    try:
        notes = conn.execute(
            "SELECT id, body, source, updated_at, context_json, "
            "COALESCE(fact_ref, source_ref) AS evidence_ref FROM analyst_notes "
            "WHERE UPPER(ticker) = ? AND kind = 'question' AND status = 'open' "
            "ORDER BY created_at DESC, id DESC LIMIT 20",
            (ticker,),
        ).fetchall()
    except sqlite3.Error:
        return [], "unavailable", ["analyst_notes_unavailable"]
    questions: list[DeskQuestion] = []
    for note in notes:
        context: dict[str, object] = {}
        if note["context_json"]:
            try:
                decoded: object = json.loads(str(note["context_json"]))
                if isinstance(decoded, dict):
                    context = cast("dict[str, object]", decoded)
            except (TypeError, ValueError):
                pass
        source = str(note["source"])
        raw_origin = str(context.get("origin") or "")
        origin: Literal["owner", "model", "thesis", "engagement", "system"]
        if raw_origin == "thesis":
            origin = "thesis"
        elif raw_origin == "engagement":
            origin = "engagement"
        elif raw_origin == "model":
            origin = "model"
        elif source in {"manual", "capture", "comment"}:
            origin = "owner"
        elif source == "advisor":
            origin = "model"
        else:
            origin = "system"
        approval: Literal["owner-authored", "owner-approved", "system"]
        if origin == "owner":
            approval = "owner-authored"
        elif context.get("approval") == "owner-approved":
            approval = "owner-approved"
        else:
            approval = "system"
        questions.append(
            DeskQuestion(
                stable_id=f"analyst_note:{int(note['id'])}",
                note_id=int(note["id"]),
                revision=str(note["updated_at"]),
                body=str(note["body"]),
                origin=origin,
                approval=approval,
                evidence_ref=(
                    str(note["evidence_ref"]) if note["evidence_ref"] is not None else None
                ),
            )
        )
    return questions, "ok", []


def _latest_brief(repo_root: Path, ticker: str) -> BriefLibraryItem | None:
    index = load_report_artifact_index(repo_root)
    matches = [item for item in index.items if item.ticker == ticker]
    if not matches:
        return None
    latest = max(matches, key=lambda item: (item.generated_at, item.artifact_id))
    return build_brief_descriptor(repo_root, latest)


def _position_snapshot(
    conn: sqlite3.Connection, ticker: str, live: LivePortfolio | None = None
) -> DeskPositionSnapshot:
    """Join canonical live position data to the governed DCF basis."""

    dcf = latest_dcf_row(conn, ticker)
    position = next(
        (
            candidate
            for candidate in (live.positions if live is not None and live.available else ())
            if candidate.ticker and candidate.ticker.strip().upper() == ticker
        ),
        None,
    )
    position_state: Literal["held", "not_held", "unavailable"]
    if live is None or not live.available:
        position_state = "unavailable"
    elif position is None:
        position_state = "not_held"
    else:
        position_state = "held"
    return DeskPositionSnapshot(
        weight_pct=position.percent_of_portfolio if position is not None else None,
        market_value=position.market_value if position is not None else None,
        position_source=("portfolio_tracker_api" if live is not None and live.available else None),
        position_as_of=live.as_of if live is not None and live.available else None,
        position_state=position_state,
        price=dcf.live_price if dcf is not None else None,
        fair_value=dcf.npv_per_share if dcf is not None else None,
        currency=dcf.currency if dcf is not None else None,
        as_of=(dcf.live_price_at or dcf.valuation_date) if dcf is not None else None,
        source="latest_governed_dcf_run" if dcf is not None else None,
        price_as_of=dcf.live_price_at if dcf is not None else None,
        fair_value_as_of=dcf.valuation_date if dcf is not None else None,
    )


def _is_fresh_thesis_timestamp(value: datetime | None, *, as_of: datetime) -> bool:
    """Require a recent evaluated state rather than implying current thesis health."""

    if value is None:
        return False
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return (as_of - normalized.astimezone(UTC)).days <= _THESIS_FACT_FRESHNESS_DAYS


def _tier_one_evidence(
    thesis: ThesisSection,
    ticker: str,
    *,
    as_of: datetime,
) -> tuple[list[DeskKpiEvidence], Literal["missing", "stale", "incomplete"] | None]:
    """Return every Tier-1 metric only when each has fresh, exact evidence.

    The report's ledger owns KPI resolution and status.  This adapter deliberately
    does not fall back to free-text holdings values: the Desk either has the
    canonical definition-backed series for every Tier-1 metric or exposes no KPI
    summary at all.
    """

    tier_one = [row for row in thesis.kpi_ledger if row.tier == "tier_1"]
    if not tier_one:
        return [], "missing"
    evidence: list[DeskKpiEvidence] = []
    for row in tier_one:
        if row.kpi_definition_id is None or row.current_status == "unknown" or not row.history:
            return [], "incomplete"
        latest_period, latest_value = row.history[-1]
        try:
            period_end = date.fromisoformat(latest_period)
        except ValueError:
            return [], "incomplete"
        if (as_of.date() - period_end).days > _THESIS_FACT_FRESHNESS_DAYS:
            return [], "stale"
        if latest_value is None:
            return [], "incomplete"
        status = row.current_status
        if status not in {"green", "yellow", "red"}:
            return [], "incomplete"
        evidence.append(
            DeskKpiEvidence(
                name=row.name,
                tier="tier_1",
                unit=row.unit,
                latest_period=latest_period,
                latest_value=latest_value,
                current_status=status,
                evidence_ref=f"kpi:{ticker}:{row.kpi_definition_id}",
            )
        )
    return evidence, None


def _thesis_projections(
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of: datetime,
) -> tuple[ThesisRiskProjection, KpiSummaryProjection, list[str]]:
    """Project the canonical thesis section without inventing live facts."""

    thesis = thesis_section.build(ticker, repo_root, conn=conn)
    evidence, evidence_reason = _tier_one_evidence(thesis, ticker, as_of=as_of)
    kpi_summary = KpiSummaryProjection(
        status="available" if evidence_reason is None else "unavailable",
        items=evidence,
        unavailable_reason=evidence_reason,
    )
    evaluation_is_fresh = _is_fresh_thesis_timestamp(thesis.last_evaluated_at, as_of=as_of)
    if (
        thesis.status != SectionStatus.OK
        or thesis.stub_warning is not None
        or not thesis.thesis_full
        or evidence_reason is not None
        or not evaluation_is_fresh
        or thesis.overall_breach_status in {"unknown", "unresolved"}
    ):
        if thesis.status == SectionStatus.MISSING_DATA or not thesis.thesis_full:
            reason: Literal["missing", "stale", "incomplete"] = "missing"
        elif evidence_reason == "stale" or not evaluation_is_fresh:
            reason = "stale"
        else:
            reason = "incomplete"
        thesis_risk = ThesisRiskProjection(status="unavailable", unavailable_reason=reason)
    else:
        # The guard above has established both values; assertions retain that
        # narrowing for static analysis as well as documenting the fail-closed
        # boundary.
        assert thesis.last_evaluated_at is not None
        if thesis.overall_breach_status == "ok":
            overall_breach_status: Literal["ok", "warn", "breach", "unresolved"] = "ok"
        elif thesis.overall_breach_status == "warn":
            overall_breach_status = "warn"
        elif thesis.overall_breach_status == "breach":
            overall_breach_status = "breach"
        elif thesis.overall_breach_status == "unresolved":
            overall_breach_status = "unresolved"
        else:
            raise AssertionError("unavailable thesis status passed availability guard")
        thesis_risk = ThesisRiskProjection(
            status="available",
            thesis=thesis.thesis_full,
            overall_breach_status=overall_breach_status,
            evaluated_at=thesis.last_evaluated_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            break_conditions=thesis.break_conditions,
        )
    warnings: list[str] = []
    if thesis_risk.status == "unavailable":
        warnings.append(f"thesis_risk_{thesis_risk.unavailable_reason}")
    if kpi_summary.status == "unavailable":
        warnings.append(f"kpi_summary_{kpi_summary.unavailable_reason}")
    return thesis_risk, kpi_summary, warnings


def build_company_desk(
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    generated_at: datetime | None = None,
    today: date | None = None,
    live: LivePortfolio | None = None,
) -> CompanyDeskResponse:
    """Build one cheap Desk snapshot from an existing request connection."""

    normalized = ticker.strip().upper()
    company = _company(conn, normalized)
    if company is None:
        raise LookupError(normalized)
    built_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    decision, conditions, decision_warnings = build_decision_projection(
        conn, normalized, as_of=generated_at
    )
    questions, question_store_status, question_warnings = _questions(conn, normalized)
    latest_brief = _latest_brief(repo_root, normalized)
    readout_projection = load_latest_earnings_readouts(conn, [normalized])
    latest_earnings_readout = readout_projection.readouts.get(normalized)
    earnings_doorway = build_earnings_doorway(
        conn,
        normalized,
        today=today or calendar_today(generated_at),
    )
    position = _position_snapshot(conn, normalized, live)
    thesis_risk, kpi_summary, thesis_warnings = _thesis_projections(
        repo_root, conn, normalized, as_of=built_at
    )
    warnings = [
        *decision_warnings,
        *question_warnings,
        *readout_projection.warnings,
        *thesis_warnings,
    ]
    if live is not None and not live.available:
        warnings.append("portfolio_tracker_unavailable")
    elif live is not None and live.is_stale:
        warnings.append("portfolio_tracker_stale")
    if live is not None and live.is_partial:
        warnings.append("portfolio_tracker_partial")
    if position.price is None and position.fair_value is None:
        warnings.insert(0, "position_snapshot_unavailable")
    if latest_brief is None:
        warnings.append("research_brief_unavailable")
    return CompanyDeskResponse(
        status="degraded" if warnings else "ok",
        generated_at=built_at.isoformat().replace("+00:00", "Z"),
        company=company,
        position=position,
        current_decision=decision,
        conditions=conditions,
        open_questions=questions,
        question_store_status=question_store_status,
        latest_brief=latest_brief,
        latest_earnings_readout=latest_earnings_readout,
        earnings_doorway=earnings_doorway,
        thesis_risk=thesis_risk,
        kpi_summary=kpi_summary,
        warnings=warnings,
    )


__all__ = ["CompanyDeskResponse", "build_company_desk", "build_earnings_doorway"]
