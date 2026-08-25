"""Narrow, read-only Company Desk contract for the Work OS.

The Desk composes stable governed identities from the repository database and
the compact report-artifact index.  It never runs research, scans report
directories, or invokes an LLM while a screen is loading.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from advisor.price_action_bands import (
    PriceActionBandProjection,
    resolve_price_action_bands,
)
from advisor.sizing_intent_review import load_sizing_intent_review_from_connection
from calendar_clock import calendar_today
from dcf.availability import resolve_dcf_route_artifact
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
from report.models import BreakRuleEvaluation, KpiLedgerRow, SectionStatus, ThesisSection
from report.sections import thesis as thesis_section

CoverageRole = Literal["portfolio", "evaluation", "unknown"]
KpiProjectionState = Literal[
    "tracked",
    "awaiting_data",
    "stale",
    "improving",
    "deteriorating",
    "material_exception",
]


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
    dcf_url: str | None = None


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
    """One Tier-1 KPI with an explicit state for every evidence condition."""

    model_config = ConfigDict(frozen=True)

    name: str
    tier: Literal["tier_1"]
    unit: str | None = None
    latest_period: str | None = None
    latest_value: float | None = None
    current_status: Literal["green", "yellow", "red", "unknown"] = "unknown"
    state: KpiProjectionState
    evidence_ref: str | None = None
    source_hint: str | None = None


class DeskBreakRuleEvidence(BaseModel):
    """One canonical hard-break evaluation, distinct from decision conditions."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    kpi_name: str
    comparator: str
    threshold: float
    status: Literal["ok", "warn", "breach", "unresolved"]
    latest_period: str | None = None
    latest_value: float | None = None
    unit: str | None = None
    distance_to_threshold: float | None = None
    detail: str | None = None
    provenance_ref: str


class ThesisRiskProjection(BaseModel):
    """Read-only thesis state, withheld unless its evaluated facts are current."""

    model_config = ConfigDict(frozen=True)

    status: Literal["available", "unavailable"]
    thesis: str | None = None
    overall_breach_status: Literal["ok", "warn", "breach", "unresolved"] | None = None
    evaluated_at: str | None = None
    break_rules: list[DeskBreakRuleEvidence] = []
    unavailable_reason: Literal["missing", "stale", "incomplete"] | None = None


class KpiSummaryProjection(BaseModel):
    """Exact-evidence Tier-1 KPI summary for the Company Desk."""

    model_config = ConfigDict(frozen=True)

    status: Literal["available", "unavailable"]
    items: list[DeskKpiEvidence] = []
    unavailable_reason: Literal["missing", "stale", "incomplete"] | None = None


class DeskSayDoCommitment(BaseModel):
    """One canonical management commitment with exact quarter and outcome evidence."""

    model_config = ConfigDict(frozen=True)

    id: int
    period_made: str
    period_target: str
    kpi_name: str
    comparator: str
    target_value: float
    unit: str
    narrative: str
    realized_value: float | None = None
    outcome: Literal["hit", "miss", "beat", "mixed", "no_data"] | None = None
    evaluated_at: str | None = None
    source_ref: str


class DeskSayDoProjection(BaseModel):
    """At most four statement quarters from the canonical commitment ledger."""

    model_config = ConfigDict(frozen=True)

    status: Literal["available", "unavailable"]
    quarters: list[str] = Field(default_factory=lambda: list[str]())
    commitments: list[DeskSayDoCommitment] = Field(
        default_factory=lambda: list[DeskSayDoCommitment]()
    )
    as_of: str | None = None
    unavailable_reason: Literal[
        "missing_source", "schema_mismatch", "query_failed", "malformed_row"
    ] | None = None


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
    price_action_bands: PriceActionBandProjection
    say_do: DeskSayDoProjection
    warnings: list[str]


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
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    live: LivePortfolio | None = None,
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
        dcf_url=(f"/dcf/{ticker}" if resolve_dcf_route_artifact(repo_root, ticker) else None),
    )


def _price_action_bands(
    conn: sqlite3.Connection,
    ticker: str,
) -> PriceActionBandProjection:
    review = load_sizing_intent_review_from_connection(conn)
    if not review.sizing_intent_source_available:
        return resolve_price_action_bands(owner_ratified=None, source_available=False)
    candidates = [entry for entry in review.entries if entry.intent.ticker.upper() == ticker]
    if not candidates:
        return resolve_price_action_bands(owner_ratified=None)
    selected = max(
        candidates,
        key=lambda entry: (
            entry.price_action_bands.is_actionable,
            entry.intent.updated_at,
            entry.intent.id,
        ),
    )
    return selected.price_action_bands


def _parse_say_do_date(value: object) -> datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("date is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
    return parsed


def _normalize_say_do_outcome(
    value: object,
) -> Literal["hit", "miss", "beat", "mixed", "no_data"] | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    outcome_map: dict[str, Literal["hit", "miss", "beat", "mixed", "no_data"]] = {
        "hit": "hit",
        "met": "hit",
        "beat": "beat",
        "exceeded": "beat",
        "miss": "miss",
        "missed": "miss",
        "no_data": "no_data",
        "awaiting_data": "no_data",
        "mixed": "mixed",
        "partial": "mixed",
    }
    return outcome_map.get(normalized)


def _say_do_projection(conn: sqlite3.Connection, ticker: str) -> DeskSayDoProjection:
    required = {
        "id",
        "ticker",
        "period_made",
        "period_target",
        "transcript_segment_id",
        "kpi_name",
        "comparator",
        "target_value",
        "unit",
        "narrative",
        "realized_value",
        "outcome",
        "evaluated_at",
    }
    columns = _table_columns(conn, "management_commitments")
    if not columns:
        return DeskSayDoProjection(status="unavailable", unavailable_reason="missing_source")
    if not required.issubset(columns):
        return DeskSayDoProjection(status="unavailable", unavailable_reason="schema_mismatch")
    try:
        rows = conn.execute(
            "SELECT id, period_made, period_target, transcript_segment_id, kpi_name, "
            "comparator, target_value, unit, narrative, realized_value, outcome, evaluated_at "
            "FROM management_commitments WHERE UPPER(ticker) = ? "
            "ORDER BY period_made DESC, id DESC",
            (ticker,),
        ).fetchall()
    except sqlite3.Error:
        return DeskSayDoProjection(status="unavailable", unavailable_reason="query_failed")

    parsed_rows: list[tuple[sqlite3.Row, datetime, datetime, datetime | None, str]] = []
    for row in rows:
        try:
            period_made = _parse_say_do_date(row["period_made"])
            period_target = _parse_say_do_date(row["period_target"])
            evaluated_at = (
                _parse_say_do_date(row["evaluated_at"])
                if row["evaluated_at"] is not None
                else None
            )
            target_value = float(row["target_value"])
            if not math.isfinite(target_value):
                raise ValueError("target_value is non-finite")
            if row["realized_value"] is not None and not math.isfinite(float(row["realized_value"])):
                raise ValueError("realized_value is non-finite")
        except (TypeError, ValueError, OverflowError):
            return DeskSayDoProjection(status="unavailable", unavailable_reason="malformed_row")
        parsed_rows.append((row, period_made, period_target, evaluated_at, period_made.strftime("%Y-%m")))

    quarters: list[str] = []
    commitments: list[DeskSayDoCommitment] = []
    as_of: str | None = None
    for row, period_made_dt, period_target_dt, evaluated_at_dt, quarter in parsed_rows:
        if quarter in quarters:
            continue
        if len(quarters) == 4:
            break
        quarters.append(quarter)
        period_made = period_made_dt.date().isoformat()
        period_target = period_target_dt.date().isoformat()
        evaluated_at = evaluated_at_dt.date().isoformat() if evaluated_at_dt else None
        as_of = max(value for value in (as_of, evaluated_at, period_target, period_made) if value is not None)
        outcome = _normalize_say_do_outcome(row["outcome"])
        commitments.append(
            DeskSayDoCommitment(
                id=int(row["id"]),
                period_made=period_made,
                period_target=period_target,
                kpi_name=str(row["kpi_name"]),
                comparator=str(row["comparator"]),
                target_value=float(row["target_value"]),
                unit=str(row["unit"]),
                narrative=str(row["narrative"]),
                realized_value=(
                    float(row["realized_value"]) if row["realized_value"] is not None else None
                ),
                outcome=outcome,
                evaluated_at=evaluated_at,
                source_ref=f"transcript_segment:{int(row['transcript_segment_id'])}",
            )
        )
    return DeskSayDoProjection(
        status="available",
        quarters=quarters,
        commitments=commitments,
        as_of=as_of,
    )


def _is_fresh_thesis_timestamp(value: datetime | None, *, as_of: datetime) -> bool:
    """Require a recent evaluated state rather than implying current thesis health."""

    if value is None:
        return False
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    age = as_of - normalized.astimezone(UTC)
    return 0 <= age.total_seconds() <= _THESIS_FACT_FRESHNESS_DAYS * 86_400


def _kpi_state(
    row: KpiLedgerRow,
    latest_period: date | None,
    latest_value: float | None,
    *,
    as_of: datetime,
) -> DeskKpiEvidence:
    """Project a ledger row without hiding partial or stale evidence."""

    evidence_ref = (
        f"kpi:{row.name}:{row.kpi_definition_id}" if row.kpi_definition_id is not None else None
    )
    latest_label = latest_period.isoformat() if latest_period is not None else None
    if latest_period is None or latest_value is None:
        state: KpiProjectionState = "awaiting_data"
    elif not 0 <= (as_of.date() - latest_period).days <= _THESIS_FACT_FRESHNESS_DAYS:
        state = "stale"
    elif row.current_status == "red":
        state = "material_exception"
    else:
        values = [value for _, value in row.history if value is not None]
        if len(values) >= 2 and values[-1] > values[-2]:
            state = "improving"
        elif len(values) >= 2 and values[-1] < values[-2]:
            state = "deteriorating"
        else:
            state = "tracked"
    return DeskKpiEvidence(
        name=row.name,
        tier="tier_1",
        unit=row.unit,
        latest_period=latest_label,
        latest_value=latest_value,
        current_status=row.current_status,
        state=state,
        evidence_ref=evidence_ref,
        source_hint=row.source_hint,
    )


def _tier_one_evidence(
    thesis: ThesisSection, ticker: str, *, as_of: datetime
) -> KpiSummaryProjection:
    """Return every Tier-1 row with explicit partial-data states and provenance."""

    tier_one = [row for row in thesis.kpi_ledger if row.tier == "tier_1"]
    if not tier_one:
        return KpiSummaryProjection(status="unavailable", unavailable_reason="missing")
    items: list[DeskKpiEvidence] = []
    for row in tier_one:
        latest_period: date | None = None
        latest_value: float | None = None
        if row.history:
            raw_period, latest_value = row.history[-1]
            try:
                latest_period = date.fromisoformat(raw_period)
            except ValueError:
                latest_value = None
        item = _kpi_state(row, latest_period, latest_value, as_of=as_of)
        item = item.model_copy(
            update={
                "evidence_ref": (
                    f"kpi:{ticker}:{row.kpi_definition_id}"
                    if row.kpi_definition_id is not None
                    else None
                )
            }
        )
        items.append(item)
    return KpiSummaryProjection(status="available", items=items)


def _break_rule_evidence(
    rules: list[BreakRuleEvaluation], ticker: str
) -> list[DeskBreakRuleEvidence]:
    """Keep rule-level status, distance, and provenance with the Desk projection."""

    evidence: list[DeskBreakRuleEvidence] = []
    for rule in rules:
        latest = rule.observations[-1] if rule.observations else None
        evidence.append(
            DeskBreakRuleEvidence(
                rule_id=rule.rule_id,
                kpi_name=rule.kpi_name,
                comparator=rule.comparator,
                threshold=rule.threshold,
                status=rule.status,
                latest_period=latest.period_end if latest else None,
                latest_value=latest.value if latest else None,
                unit=latest.unit if latest else None,
                distance_to_threshold=(latest.value - rule.threshold) if latest else None,
                detail=rule.detail or None,
                provenance_ref=f"thesis_evaluation:{ticker}:{rule.rule_id}",
            )
        )
    return evidence


def _thesis_projections(
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of: datetime,
) -> tuple[ThesisRiskProjection, KpiSummaryProjection, list[str]]:
    """Project the canonical thesis section without inventing live facts."""

    thesis = thesis_section.build(ticker, repo_root, conn=conn)
    kpi_summary = _tier_one_evidence(thesis, ticker, as_of=as_of)
    break_rules = _break_rule_evidence(thesis.break_rule_evaluations, ticker)
    evaluation_is_fresh = _is_fresh_thesis_timestamp(thesis.last_evaluated_at, as_of=as_of)
    if (
        thesis.status != SectionStatus.OK
        or thesis.stub_warning is not None
        or not thesis.thesis_full
        or not break_rules
        or not evaluation_is_fresh
        or thesis.overall_breach_status in {"unknown", "unresolved"}
    ):
        if thesis.status == SectionStatus.MISSING_DATA or not thesis.thesis_full:
            reason: Literal["missing", "stale", "incomplete"] = "missing"
        elif not evaluation_is_fresh:
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
            break_rules=break_rules,
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
    position = _position_snapshot(repo_root, conn, normalized, live)
    thesis_risk, kpi_summary, thesis_warnings = _thesis_projections(
        repo_root, conn, normalized, as_of=built_at
    )
    price_action_bands = _price_action_bands(conn, normalized)
    say_do = _say_do_projection(conn, normalized)
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
        price_action_bands=price_action_bands,
        say_do=say_do,
        warnings=warnings,
    )


__all__ = ["CompanyDeskResponse", "build_company_desk", "build_earnings_doorway"]
