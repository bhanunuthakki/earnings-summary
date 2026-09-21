"""Read-only Settings view of collection policy and FMP recovery telemetry."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from models.companies import ListType
from pipeline.fmp_operations_view import FmpOperationalDetails, read_fmp_operational_details
from pipeline.fmp_recovery import ContainmentReason, ExecutionMode, OutcomeCode
from pipeline.ir_approval_panel import read_ir_approval_review, render_ir_approval_panel
from pipeline.operations_styles import OPERATIONS_STYLE
from pipeline.sec_operations_view import (
    SecCoverageSummaryView,
    read_sec_coverage_state,
)
from pipeline.source_policy import (
    DISPLAY_ROLE_ORDER,
    POLICY_VERSION,
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    AuthorizationReason,
    CollectionMode,
    CollectionSource,
    decision_for,
    issuer_policy,
    mode_for_role,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

Tone = Literal["ok", "warn", "bad"]


class PolicyDisplayState(StrEnum):
    AUTOMATIC = "automatic"
    ON_DEMAND = "on_demand"
    SCREENING_ONLY = "screening_only"
    NEVER = "never"


class CoverageRoleView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ListType
    label: str
    mode: CollectionMode
    mode_label: str
    summary: str


class PolicyCellView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ListType
    state: PolicyDisplayState
    label: str
    note: str


class PolicyRowView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    source: CollectionSource
    artifact_kind: ArtifactKind
    label: str
    detail: str
    cells: tuple[PolicyCellView, ...]


class ApprovedIssuerView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    authority_url: str
    adapter_key: str
    quarter_window: int
    sec_forms: tuple[str, ...]
    accepts_text_transcripts: bool
    accepts_webcasts: bool
    policy_sha256: str


class FmpRecoveryEventView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: str
    reason_code: str | None = None
    state_from: str | None = None
    state_to: str | None = None
    circuit_revision: int | None = None
    recorded_at: str


FmpCircuitDisplayState = Literal["CLOSED", "OPEN", "HALF_OPEN", "UNINITIALIZED", "UNAVAILABLE"]
FmpCorpusDisplayState = Literal["available", "empty", "unavailable"]
FmpCircuitAdmission = Literal["permitted", "blocked", "probe_only", "unknown", "unavailable"]
FmpProviderAvailability = Literal[
    "available", "permitted_unverified", "degraded", "unknown", "unavailable"
]
FmpProviderFreshness = Literal["recent", "stale", "unverified"]


class FmpProviderFreshnessPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success_max_age: timedelta


FMP_PROVIDER_FRESHNESS_POLICY = FmpProviderFreshnessPolicy(success_max_age=timedelta(hours=24))


class FmpOperationalReadModel(BaseModel):
    """Sanitized read-only projection of the active FMP recovery schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    circuit_state: FmpCircuitDisplayState
    circuit_admission: FmpCircuitAdmission
    provider_availability: FmpProviderAvailability
    backlog_count: int | None = None
    pending_count: int | None = None
    leased_count: int | None = None
    satisfied_count: int | None = None
    terminal_count: int | None = None
    pending_tickers: tuple[str, ...] = ()
    next_probe_at: str | None = None
    last_reason_code: str | None = None
    last_success_at: str | None = None
    last_success_freshness: FmpProviderFreshness = "unverified"
    corpus_state: FmpCorpusDisplayState
    corpus_ticker_count: int | None = None
    last_corpus_at: str | None = None
    recent_events: tuple[FmpRecoveryEventView, ...] = ()
    details: FmpOperationalDetails = Field(default_factory=FmpOperationalDetails)


class DataPolicySettingsView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str
    roles: tuple[CoverageRoleView, ...]
    rows: tuple[PolicyRowView, ...]
    approved_issuers: tuple[ApprovedIssuerView, ...]
    fmp_state: FmpOperationalReadModel
    sec_coverage: SecCoverageSummaryView = Field(default_factory=SecCoverageSummaryView)


_ROLE_CONTENT: dict[ListType, tuple[str, str, str]] = {
    ListType.PORTFOLIO: (
        "Portfolio",
        "Automatic full",
        "Automatic full collection with portfolio-first scheduling.",
    ),
    ListType.EVALUATION: (
        "Evaluation",
        "Automatic full",
        "Automatic full collection with the same source and evidence requirements as portfolio.",
    ),
    ListType.WATCHLIST: (
        "Watchlist",
        "Automatic full",
        "Automatic full collection with the same source and evidence requirements as portfolio.",
    ),
    ListType.INDEX_MEMBER: (
        "Index members",
        "Screening only",
        "Screening facts only from FMP. No document or transcript collection.",
    ),
}

_ROW_SPECS: tuple[tuple[str, CollectionSource, ArtifactKind, str, str], ...] = (
    (
        "fmp_financial_facts",
        CollectionSource.FMP,
        ArtifactKind.FINANCIAL_FACT,
        "FMP financial facts",
        "Use the existing on-disk corpus when live FMP is unavailable; refresh recovery is queued once wired.",
    ),
    (
        "sec_companyfacts",
        CollectionSource.SEC,
        ArtifactKind.COMPANY_FACTS,
        "SEC CompanyFacts",
        "Issuer-level fact feed. Kept distinct from accession-scoped native filings.",
    ),
    (
        "sec_native_filings",
        CollectionSource.SEC,
        ArtifactKind.FILING_PACKAGE,
        "SEC native filings",
        "Accession-scoped filings and relevant sections, bounded by company priority and issuer rules.",
    ),
    (
        "ir_documents",
        CollectionSource.IR,
        ArtifactKind.IR_DOCUMENT,
        "IR financial documents",
        "Owner-approved issuer pages; last "
        f"{SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters} reported quarters; "
        "presentations and releases before web search.",
    ),
    (
        "text_transcripts",
        CollectionSource.TRANSCRIPT,
        ArtifactKind.TEXT_TRANSCRIPT,
        "Text transcripts",
        "Prefer stored or publisher text transcripts. Audio extraction is not the default path.",
    ),
    (
        "webcasts",
        CollectionSource.TRANSCRIPT,
        ArtifactKind.WEBCAST,
        "Webcasts",
        "Excluded for every coverage role.",
    ),
)


def _display_cell(
    role: ListType,
    source: CollectionSource,
    artifact_kind: ArtifactKind,
) -> PolicyCellView:
    automatic = decision_for(role, source, artifact_kind, requested=False)
    requested = decision_for(role, source, artifact_kind, requested=True)
    if automatic.allowed:
        if automatic.reason is AuthorizationReason.SCREENING_FACT_ALLOWED:
            return PolicyCellView(
                role=role,
                state=PolicyDisplayState.SCREENING_ONLY,
                label="Automatic · screening only",
                note="Financial screening facts only",
            )
        return PolicyCellView(
            role=role,
            state=PolicyDisplayState.AUTOMATIC,
            label="Automatic",
            note="Runs without an owner request",
        )
    if requested.allowed:
        return PolicyCellView(
            role=role,
            state=PolicyDisplayState.ON_DEMAND,
            label="On demand",
            note="Requires an explicit owner request",
        )
    return PolicyCellView(
        role=role,
        state=PolicyDisplayState.NEVER,
        label="Never",
        note=(
            "Webcasts are excluded"
            if automatic.reason is AuthorizationReason.WEBCAST_EXCLUDED
            else "Coverage depth denied"
        ),
    )


def _provider_success_freshness(
    last_success_at: str | None,
    *,
    as_of: datetime,
) -> FmpProviderFreshness:
    if last_success_at is None:
        return "unverified"
    try:
        parsed = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
    except ValueError:
        return "unverified"
    observed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    now = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    age = now - observed
    if timedelta(0) <= age <= FMP_PROVIDER_FRESHNESS_POLICY.success_max_age:
        return "recent"
    return "stale"


def read_fmp_operational_state(
    db_path: Path | None,
    *,
    as_of: datetime | None = None,
) -> FmpOperationalReadModel:
    """Read recovery state without creating a database or taking a write lock."""

    unavailable = FmpOperationalReadModel(
        circuit_state="UNAVAILABLE",
        circuit_admission="unavailable",
        provider_availability="unavailable",
        corpus_state="unavailable",
    )
    if db_path is None or not db_path.is_file():
        return unavailable
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            details = read_fmp_operational_details(
                conn,
                as_of=as_of or datetime.now(UTC),
                receipt_max_age=FMP_PROVIDER_FRESHNESS_POLICY.success_max_age,
            )
            circuit = conn.execute(
                "SELECT state,next_probe_at,last_reason_code,last_success_at "
                "FROM provider_circuit_state WHERE provider='fmp'"
            ).fetchone()
            counts = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state,COUNT(*) AS count FROM fmp_work_backlog GROUP BY state"
                ).fetchall()
            }
            pending_tickers = tuple(
                str(row["ticker"])
                for row in conn.execute(
                    "SELECT ticker FROM fmp_work_backlog "
                    "WHERE state IN ('PENDING','LEASED') GROUP BY ticker "
                    "ORDER BY MAX(priority) DESC,MIN(created_at),ticker LIMIT 12"
                ).fetchall()
            )
            corpus = conn.execute(
                "SELECT COUNT(DISTINCT work.ticker) AS ticker_count,"
                "MAX(attempt.corpus_captured_at) AS last_corpus_at "
                "FROM fmp_work_attempts AS attempt "
                "JOIN fmp_work_backlog AS work ON work.work_id=attempt.work_id "
                "WHERE attempt.corpus_content_sha256 IS NOT NULL"
            ).fetchone()
            has_events = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fmp_recovery_events'"
            ).fetchone()
            events_rows = (
                conn.execute(
                    "SELECT event_id,event_type,reason_code,state_from,state_to,circuit_revision,recorded_at "
                    "FROM fmp_recovery_events ORDER BY recorded_at DESC LIMIT 5"
                ).fetchall()
                if has_events
                else []
            )
            recent_events = tuple(
                FmpRecoveryEventView(
                    event_id=str(row["event_id"]),
                    event_type=str(row["event_type"]),
                    reason_code=str(row["reason_code"]) if row["reason_code"] is not None else None,
                    state_from=str(row["state_from"]) if row["state_from"] is not None else None,
                    state_to=str(row["state_to"]) if row["state_to"] is not None else None,
                    circuit_revision=int(row["circuit_revision"])
                    if row["circuit_revision"] is not None
                    else None,
                    recorded_at=str(row["recorded_at"]),
                )
                for row in events_rows
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return unavailable
    pending = counts.get("PENDING", 0)
    leased = counts.get("LEASED", 0)
    corpus_ticker_count = int(corpus["ticker_count"]) if corpus is not None else 0
    last_corpus_at = (
        str(corpus["last_corpus_at"])
        if corpus is not None and corpus["last_corpus_at"] is not None
        else None
    )
    corpus_state: FmpCorpusDisplayState = "available" if corpus_ticker_count > 0 else "empty"
    if circuit is None:
        return FmpOperationalReadModel(
            circuit_state="UNINITIALIZED",
            circuit_admission="unknown",
            provider_availability="unknown",
            backlog_count=pending + leased,
            pending_count=pending,
            leased_count=leased,
            satisfied_count=counts.get("SATISFIED", 0),
            terminal_count=counts.get("TERMINAL", 0),
            pending_tickers=pending_tickers,
            corpus_state=corpus_state,
            corpus_ticker_count=corpus_ticker_count,
            last_corpus_at=last_corpus_at,
            recent_events=recent_events,
            details=details,
        )
    state = str(circuit["state"])
    if state not in {"CLOSED", "OPEN", "HALF_OPEN"}:
        return unavailable
    normalized_state = cast("FmpCircuitDisplayState", state)
    admission_by_state: dict[str, FmpCircuitAdmission] = {
        "CLOSED": "permitted",
        "OPEN": "blocked",
        "HALF_OPEN": "probe_only",
    }
    last_success_at = (
        str(circuit["last_success_at"]) if circuit["last_success_at"] is not None else None
    )
    last_success_freshness = _provider_success_freshness(
        last_success_at,
        as_of=datetime.now(UTC) if as_of is None else as_of,
    )
    if state == "CLOSED":
        provider_availability: FmpProviderAvailability = (
            "available" if last_success_freshness == "recent" else "permitted_unverified"
        )
    else:
        provider_availability = "degraded"
    return FmpOperationalReadModel(
        circuit_state=normalized_state,
        circuit_admission=admission_by_state[state],
        provider_availability=provider_availability,
        backlog_count=pending + leased,
        pending_count=pending,
        leased_count=leased,
        satisfied_count=counts.get("SATISFIED", 0),
        terminal_count=counts.get("TERMINAL", 0),
        pending_tickers=pending_tickers,
        next_probe_at=(
            str(circuit["next_probe_at"]) if circuit["next_probe_at"] is not None else None
        ),
        last_reason_code=(
            str(circuit["last_reason_code"]) if circuit["last_reason_code"] is not None else None
        ),
        last_success_at=last_success_at,
        last_success_freshness=last_success_freshness,
        corpus_state=corpus_state,
        corpus_ticker_count=corpus_ticker_count,
        last_corpus_at=last_corpus_at,
        recent_events=recent_events,
        details=details,
    )


def build_data_policy_settings_view(*, db_path: Path | None = None) -> DataPolicySettingsView:
    roles = tuple(
        CoverageRoleView(
            role=role,
            label=_ROLE_CONTENT[role][0],
            mode=mode_for_role(role),
            mode_label=_ROLE_CONTENT[role][1],
            summary=_ROLE_CONTENT[role][2],
        )
        for role in DISPLAY_ROLE_ORDER
    )
    rows = tuple(
        PolicyRowView(
            key=key,
            source=source,
            artifact_kind=artifact_kind,
            label=label,
            detail=detail,
            cells=tuple(_display_cell(role, source, artifact_kind) for role in DISPLAY_ROLE_ORDER),
        )
        for key, source, artifact_kind, label, detail in _ROW_SPECS
    )
    issuers = tuple(
        ApprovedIssuerView(
            ticker=policy.ticker_aliases[0],
            authority_url=policy.ir.authority_url,
            adapter_key=policy.ir.adapter_key.value,
            quarter_window=policy.ir.reported_quarter_window,
            sec_forms=tuple(form.value for form in policy.sec.filing_forms),
            accepts_text_transcripts=policy.transcript.accepts_ir_text_transcripts,
            accepts_webcasts=policy.transcript.accepts_webcasts,
            policy_sha256=policy.policy_sha256,
        )
        for policy in (issuer_policy("RBRK"), issuer_policy("WIX"))
    )
    return DataPolicySettingsView(
        policy_version=POLICY_VERSION,
        roles=roles,
        rows=rows,
        approved_issuers=issuers,
        fmp_state=read_fmp_operational_state(db_path),
        sec_coverage=read_sec_coverage_state(db_path),
    )


_PILL_CLASS: dict[PolicyDisplayState, str] = {
    PolicyDisplayState.AUTOMATIC: "k-pill k-pill-ok",
    PolicyDisplayState.ON_DEMAND: "k-pill k-pill-accent",
    PolicyDisplayState.SCREENING_ONLY: "k-pill k-pill-warn",
    PolicyDisplayState.NEVER: "k-pill",
}


def _render_roles(view: DataPolicySettingsView) -> str:
    cards = "".join(
        '<article class="k-well">'
        f'<div class="k-card-row-title">{escape(role.label)}</div>'
        f'<div class="k-card-meta">{escape(role.mode_label)}</div>'
        f"<p>{escape(role.summary)}</p>"
        "</article>"
        for role in view.roles
    )
    return f'<div class="policy-grid">{cards}</div>'


def _render_matrix(view: DataPolicySettingsView) -> str:
    header = "".join(f"<th>{escape(role.label)}</th>" for role in view.roles)
    body = "".join(
        "<tr>"
        f'<th scope="row"><div class="k-card-row-title">{escape(row.label)}</div>'
        f'<div class="k-card-meta">{escape(row.detail)}</div></th>'
        + "".join(
            f'<td><span class="{_PILL_CLASS[cell.state]}">{escape(cell.label)}</span>'
            f'<div class="k-card-meta">{escape(cell.note)}</div></td>'
            for cell in row.cells
        )
        + "</tr>"
        for row in view.rows
    )
    return (
        '<div class="policy-scroll">'
        '<table class="p-table" aria-label="Collection behavior by company priority">'
        f"<thead><tr><th>Source and artifact</th>{header}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_issuers(view: DataPolicySettingsView) -> str:
    cards = "".join(
        '<article class="k-well">'
        '<div class="policy-toolbar">'
        f'<div><span class="k-ticker-symbol">{escape(issuer.ticker)}</span>'
        f'<div class="k-card-meta">Adapter <code>{escape(issuer.adapter_key)}</code> · '
        f"policy <code>{escape(issuer.policy_sha256[:12])}</code></div></div>"
        f'<a class="k-btn k-btn-quiet k-btn-sm" data-capability="source-policy.open-authority" '
        f'href="{escape(issuer.authority_url, quote=True)}" target="_blank" rel="noopener">'
        "Open approved IR page ↗</a></div>"
        '<div class="policy-chips policy-chips-top">'
        f'<span class="k-chip">Last {issuer.quarter_window} reported quarters</span>'
        f'<span class="k-chip k-chip-mono">SEC {escape(", ".join(issuer.sec_forms))}</span>'
        f'<span class="k-chip">Text transcripts {"allowed" if issuer.accepts_text_transcripts else "excluded"}</span>'
        f'<span class="k-chip">Webcasts {"allowed" if issuer.accepts_webcasts else "excluded"}</span>'
        "</div></article>"
        for issuer in view.approved_issuers
    )
    return f'<div class="policy-stack">{cards}</div>'


def _render_sec_coverage(coverage: SecCoverageSummaryView) -> str:
    if coverage.state == "unavailable":
        return '<div class="k-well k-well-warn" role="status">SEC evidence unavailable. The database or required schema could not be read; no zero-coverage or healthy claim is inferred.</div>'
    if coverage.state == "empty":
        return '<div class="k-well" role="status">No tracked company records found in the database. This is an empty roster, not a coverage-health result.</div>'
    cards = (
        '<div class="policy-grid">'
        f'<div class="k-well"><div class="k-label">Portfolio</div><div class="k-card-row-title">{coverage.portfolio_count}</div><div class="k-card-meta">Automatic authorization</div></div>'
        f'<div class="k-well"><div class="k-label">Evaluation</div><div class="k-card-row-title">{coverage.evaluation_count}</div><div class="k-card-meta">Automatic authorization</div></div>'
        f'<div class="k-well"><div class="k-label">Watchlist</div><div class="k-card-row-title">{coverage.watchlist_count}</div><div class="k-card-meta">Automatic authorization</div></div>'
        f'<div class="k-well"><div class="k-label">Coverage gaps</div><div class="k-card-row-title">{coverage.gap_count}</div><div class="k-card-meta">Missing inventory, identity or capture proof</div></div></div>'
    )
    rows: list[str] = []
    details: list[str] = []
    for company in coverage.companies:
        anchor = "sec-evidence-" + company.ticker
        rows.append(
            "<tr>"
            f'<th scope="row"><span class="k-ticker-symbol">{escape(company.ticker)}</span> <span class="k-card-meta">{escape(company.name)}</span></th>'
            f"<td>{escape(company.role)} · {escape(company.acquisition_mode)}</td>"
            f"<td>{escape(company.filing_regime)}</td>"
            f'<td><span class="k-pill k-pill-{company.coverage_tone}">{escape(company.coverage_status)}</span></td>'
            f'<td><a class="k-link" href="#{escape(anchor, quote=True)}">{company.captured_native_count}/{company.expected_native_count} native · {company.companyfacts_snapshot_count} aggregate</a></td>'
            f"<td>{escape(company.notes)}</td></tr>"
        )
        document_rows = "".join(
            "<tr>"
            f'<th scope="row">{escape(doc.family)}</th><td>{escape(doc.period)}</td>'
            f"<td>{escape(doc.state.replace('_', ' '))}</td>"
            f"<td>{escape((doc.captured_at or 'unavailable') + (f' · {doc.capture_age_seconds / 3600:.1f}h old' if doc.capture_age_seconds is not None else ' · age unavailable'))}</td>"
            f"<td>{'verified' if doc.exact_bytes else 'missing'} / {'available' if doc.locator_available else 'missing'}"
            + (
                f' · <a class="k-link" href="{escape(doc.locator_url, quote=True)}" target="_blank" rel="noopener noreferrer">SEC source</a>'
                if doc.locator_url
                else ""
            )
            + "</td>"
            f"<td>{'amendment' if doc.amendment else 'original'}{' · supersedes prior version' if doc.supersedes else ''}</td>"
            f"<td>{escape(doc.record_id)}<br>{escape(doc.document_version_id or 'no captured version')}</td></tr>"
            for doc in company.documents
        )
        evidence_table = (
            (
                '<div class="policy-scroll" tabindex="0" role="region" aria-label="SEC source evidence table">'
                f'<table class="p-table" aria-label="{escape(company.ticker)} SEC source evidence">'
                '<thead><tr><th scope="col">Family / governed source</th><th scope="col">Period</th><th scope="col">Coverage</th><th scope="col">Captured at</th><th scope="col">Exact bytes / locator</th><th scope="col">Revision</th><th scope="col">Persisted record</th></tr></thead>'
                f"<tbody>{document_rows}</tbody></table></div>"
            )
            if document_rows
            else '<p class="k-card-meta">No observed source records. Required coverage is unknown until a governed inventory is available.</p>'
        )
        execution_rows: list[str] = []
        for execution in company.executions:
            receipt = execution.receipt
            label = receipt.state
            if receipt.state in {"requested", "running"}:
                label = "Running / completion unconfirmed"
            if not execution.population_matches:
                label = "Historical population · " + label
            if not execution.timestamp_valid:
                label = "Unavailable · invalid execution time"
            result = receipt.result
            counts = (
                f"Batch totals: {result.considered} considered · {result.captured} captured · {result.deferred} deferred · {result.failed} failed"
                if result is not None
                else "No terminal result; running status does not establish process liveness."
            )
            execution_rows.append(
                f'<li class="ops-attention-ref">{escape(receipt.scope.kind.replace("_", " "))}: {escape(label)} · {escape(receipt.recorded_at.isoformat())}<br>{escape(counts)}<br>Attempt {escape(receipt.attempt_id)}</li>'
            )
        execution_html = (
            f'<details class="ops-task-card k-grid-single"><summary class="k-card-row-title">Actual SEC execution · {escape(company.execution_state.replace("_", " "))}</summary><ul>{"".join(execution_rows)}</ul></details>'
            if execution_rows
            else f'<p class="k-card-meta">Actual SEC execution: {escape(company.execution_state.replace("_", " "))}. Historical queue state is not inferred.</p>'
        )
        details.append(
            f'<details class="k-well ops-task-card k-grid-single" id="{escape(anchor, quote=True)}"><summary class="k-card-row-title">{escape(company.ticker)} — source evidence and periods</summary>'
            f'<p class="k-card-meta ops-attention-ref">Inventory: {escape(company.inventory_id or "unavailable")} · {escape(company.inventory_state)} · observed {escape(company.inventory_observed_at or "unknown")}</p>'
            + evidence_table
            + execution_html
            + "</details>"
        )
    return (
        '<div class="policy-stack">'
        '<p class="k-card-meta">Authorization is policy. Coverage below comes from sealed inventories, immutable captures and locators. CompanyFacts is one aggregate snapshot, not one document per accession. Deferred states require recorded execution or transient-fetch evidence; missing capture alone never establishes queued work.</p>'
        + cards
        + '<div class="policy-scroll" tabindex="0" role="region" aria-label="SEC coverage table"><table class="p-table" aria-label="SEC Collection Priority and Company Coverage">'
        '<thead><tr><th scope="col">Company</th><th scope="col">Role / acquisition</th><th scope="col">Regime</th><th scope="col">Observed coverage</th><th scope="col">Capture proof</th><th scope="col">Missing evidence / limits</th></tr></thead>'
        + "<tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        + "".join(details)
        + "</div>"
    )


def _safe_fmp_code(value: str | None) -> str:
    codes = (
        {code.value for code in OutcomeCode}
        | {code.value for code in ContainmentReason}
        | {mode.value.lower() for mode in ExecutionMode}
        | {
            "auth_missing",
            "auth_invalid",
            "lease_expired",
            "probe_window_reached",
            "rate_limit_probe",
            "circuit_half_open",
            "circuit_opened",
            "provider_success",
            "circuit_contained",
            "work_leased",
            "outcome_recorded",
        }
    )
    return value if value in codes else "unclassified"


def _render_fmp_receipt(details: FmpOperationalDetails) -> str:
    receipt = details.latest_receipt
    if receipt is None:
        return f'<p class="k-card-meta" role="status">Terminal run receipt: {escape(details.receipt_state.replace("_", " "))}. Events and attempts do not establish completed recovery.</p>'
    records = "".join(
        f'<li class="ops-attention-ref">{escape(identifier)}</li>'
        for identifier in (*receipt.attempt_ids, *receipt.reused_attempt_ids)
    )
    return (
        f'<details class="k-well" id="fmp-receipt-{escape(receipt.receipt_id)}"><summary class="k-card-row-title">Latest completed recovery · {escape(details.receipt_state.replace("_", " "))}</summary>'
        f'<p class="k-card-meta ops-attention-ref">Receipt {escape(receipt.receipt_id)} · recorded {escape(receipt.recorded_at.isoformat())}</p>'
        f"<p>{receipt.fresh_count} fresh · {receipt.corpus_count} corpus · {receipt.failed_count} failed · {receipt.unattempted_count} unattempted. Circuit revision {receipt.circuit_revision}.</p>"
        f"<details><summary>Persisted attempt records ({len(receipt.attempt_ids)}) and reused proof ({len(receipt.reused_attempt_ids)})</summary><ul>{records}</ul></details></details>"
    )


def _render_fmp_state(state: FmpOperationalReadModel) -> str:
    if state.provider_availability == "unavailable":
        return (
            '<div class="k-well k-well-warn">'
            '<div class="k-card-row-title">Telemetry unavailable</div>'
            "<p>The current recovery schema is not present or could not be read. No provider-health "
            "claim is inferred.</p></div>"
        )
    availability_labels: dict[FmpProviderAvailability, str] = {
        "available": "Available",
        "permitted_unverified": "Permitted / Unverified",
        "degraded": "Degraded",
        "unknown": "Unknown",
        "unavailable": "Unavailable",
    }
    admission_labels: dict[FmpCircuitAdmission, str] = {
        "permitted": "Permitted",
        "blocked": "Blocked",
        "probe_only": "Probe only",
        "unknown": "Unknown",
        "unavailable": "Unavailable",
    }
    tone = "k-pill-ok" if state.provider_availability == "available" else "k-pill-warn"
    details = state.details
    distribution = (
        " · ".join(
            f"{item.role}: {item.count} (priority {item.priority})"
            for item in details.role_priority_counts
        )
        or "none recorded"
    )
    oldest = (
        f"{details.oldest_backlog_age_seconds / 3600:.1f} hours"
        if details.oldest_backlog_age_seconds is not None
        else "unavailable"
    )
    backlog_records = "".join(
        f'<li class="ops-attention-ref">{escape(identifier)}</li>'
        for identifier in details.backlog_record_ids
    )
    backlog_proof = (
        f'<details class="k-well"><summary class="k-card-row-title">Persisted backlog records ({details.backlog_record_count}; first {len(details.backlog_record_ids)} shown)</summary><ul>{backlog_records}</ul></details>'
        if details.backlog_record_ids
        else ""
    )
    backlog = str(state.backlog_count or 0)
    next_probe = state.next_probe_at or "not scheduled"
    reason = _safe_fmp_code(state.last_reason_code) if state.last_reason_code else "none"
    corpus_last_seen = state.last_corpus_at or "none recorded"
    provider_last_success = state.last_success_at or "none recorded"
    provider_success_evidence = {
        "recent": "Recent",
        "stale": "Stale",
        "unverified": "Unverified",
    }[state.last_success_freshness]
    queue = "".join(
        f'<span class="k-chip k-chip-mono">{escape(ticker)}</span>'
        for ticker in state.pending_tickers
    )
    events_html = ""
    if state.recent_events:
        event_rows = "".join(
            "<tr>"
            f'<td><span class="k-chip k-chip-mono">{escape(ev.recorded_at[:19])}</span></td>'
            f'<td><span class="k-chip">{escape(_safe_fmp_code(ev.event_type))}</span></td>'
            f'<td><span class="k-card-meta">{escape(_safe_fmp_code(ev.reason_code)) if ev.reason_code else "—"}</span></td>'
            f'<td><span class="k-card-meta">{escape(ev.state_from if ev.state_from in {"CLOSED", "OPEN", "HALF_OPEN"} else "—")} → {escape(ev.state_to if ev.state_to in {"CLOSED", "OPEN", "HALF_OPEN"} else "—")}</span></td>'
            "</tr>"
            for ev in state.recent_events
        )
        events_html = (
            '<div class="policy-events">'
            '<div class="k-label">Recent recovery events &amp; transitions</div>'
            '<div class="policy-scroll"><table class="p-table" aria-label="Recent FMP recovery events">'
            "<thead><tr><th>Timestamp</th><th>Event type</th><th>Reason</th><th>State transition</th></tr></thead>"
            f"<tbody>{event_rows}</tbody></table></div></div>"
        )
    return (
        '<div class="k-well">'
        '<div class="policy-toolbar">'
        '<div class="k-card-row-title">FMP recovery telemetry</div>'
        f'<span class="k-pill {tone}">{escape(availability_labels[state.provider_availability])}</span>'
        "</div>"
        '<dl class="policy-dl">'
        f'<div><dt class="k-label">Circuit state</dt><dd>{escape(state.circuit_state)}</dd></div>'
        f'<div><dt class="k-label">Network admission</dt><dd>{escape(admission_labels[state.circuit_admission])}</dd></div>'
        f'<div><dt class="k-label">Provider availability</dt><dd>{escape(availability_labels[state.provider_availability])}</dd></div>'
        f'<div><dt class="k-label">Refresh backlog</dt><dd>{escape(backlog)}</dd></div>'
        f'<div><dt class="k-label">Pending / leased</dt><dd>{state.pending_count or 0} / {state.leased_count or 0}</dd></div>'
        f'<div><dt class="k-label">Satisfied / terminal</dt><dd>{state.satisfied_count or 0} / {state.terminal_count or 0}</dd></div>'
        f'<div><dt class="k-label">Circuit opened</dt><dd>{escape(details.opened_at or "none recorded")}</dd></div>'
        f'<div><dt class="k-label">Last circuit transition</dt><dd>{escape(details.last_transition_at or "none recorded")}</dd></div>'
        f'<div><dt class="k-label">Deferred until eligible</dt><dd>{details.deferred_count if details.deferred_count is not None else "unavailable"}</dd></div>'
        f'<div><dt class="k-label">Oldest backlog age</dt><dd>{escape(oldest)}</dd></div>'
        f'<div><dt class="k-label">Role / priority distribution</dt><dd>{escape(distribution)}</dd></div>'
        f'<div><dt class="k-label">Next recovery probe</dt><dd>{escape(next_probe)}</dd></div>'
        f'<div><dt class="k-label">Last reason code</dt><dd>{escape(reason)}</dd></div>'
        f'<div><dt class="k-label">Last successful request</dt><dd>{escape(provider_last_success)}</dd></div>'
        f'<div><dt class="k-label">Success evidence</dt><dd>{escape(provider_success_evidence)}</dd></div>'
        f'<div><dt class="k-label">Corpus coverage</dt><dd>{state.corpus_ticker_count or 0} companies</dd></div>'
        f'<div><dt class="k-label">Latest corpus capture</dt><dd>{escape(corpus_last_seen)}</dd></div>'
        "</dl>"
        + (
            f'<div class="k-label">Queued companies</div><div class="policy-chips">{queue}</div>'
            if queue
            else ""
        )
        + backlog_proof
        + _render_fmp_receipt(details)
        + events_html
        + "</div>"
    )


def render_data_policy_settings_panel(
    view: DataPolicySettingsView | None = None,
    *,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Render policy plus a read-only runtime projection when a DB is supplied."""

    resolved = view or build_data_policy_settings_view(db_path=db_path)
    return (
        OPERATIONS_STYLE
        + '<section class="k-card k-card-stack" data-settings-panel="data-collection" '
        'aria-labelledby="data-policy-settings-title">'
        '<div class="k-toolbar">'
        '<div><h2 class="k-card-title" id="data-policy-settings-title">Data collection policy</h2>'
        f'<div class="k-card-meta">Read-only · policy {escape(resolved.policy_version)}</div></div>'
        '<span class="k-pill k-pill-ok">Policy enforced</span></div>'
        "<p>Portfolio, evaluation, and watchlist receive automatic full collection with the same "
        "evidence requirements. Priority controls scheduling; index members receive FMP screening "
        "facts only. Webcasts are excluded. Collection authorization does not prove completeness.</p>"
        + _render_roles(resolved)
        + '<h3 class="k-card-title">Source behavior by company priority</h3>'
        + _render_matrix(resolved)
        + '<h3 class="k-card-title">SEC collection priority &amp; coverage gaps</h3>'
        + _render_sec_coverage(resolved.sec_coverage)
        + '<h3 class="k-card-title">Owner-approved issuer adapters</h3>'
        + _render_issuers(resolved)
        + render_ir_approval_panel(read_ir_approval_review(db_path, conn=conn))
        + '<h3 class="k-card-title">Current FMP operating state</h3>'
        + _render_fmp_state(resolved.fmp_state)
        + "</section>"
    )


def render_operations_settings_shell(*, db_path: Path | None = None) -> str:
    """Truthful Operations screen with the requested Settings sub-tab."""

    return (
        '<section id="screen-execution-queue" class="screen-view">'
        '<div class="k-toolbar">'
        '<div><h1 class="k-toolbar-title">Operations &amp; Execution Governance Hub</h1>'
        '<div class="k-card-meta">Pipeline evidence and legible collection rules</div></div>'
        '<div class="k-toolbar-controls" role="tablist" aria-label="Operations hub views">'
        '<button type="button" id="opsTabQueue" class="k-chip k-chip-btn k-chip-tab is-on" '
        'role="tab" aria-selected="true" aria-controls="opsPaneQueue" '
        'tabindex="0" '
        "onclick=\"switchOpsTab('queue')\">Operations</button>"
        '<button type="button" id="opsTabSettings" class="k-chip k-chip-btn k-chip-tab" '
        'role="tab" aria-selected="false" aria-controls="opsPaneSettings" '
        'tabindex="-1" '
        "onclick=\"switchOpsTab('settings')\">Settings</button>"
        "</div></div>"
        '<div id="opsPaneQueue" role="tabpanel" aria-labelledby="opsTabQueue">'
        '<div class="k-card k-card-stack">'
        '<h2 class="k-card-title">Live operations</h2>'
        "<p>Runtime status is read from the existing Provenance console on demand. This shell does "
        "not substitute prototype health, freshness, quota, or database claims.</p>"
        '<div><button type="button" class="k-btn k-btn-primary k-btn-sm" '
        'data-capability="operations.open-live-provenance" '
        "onclick=\"openLiveDetail('screen-execution-queue')\">Open live operations →</button></div>"
        "</div></div>"
        '<div id="opsPaneSettings" role="tabpanel" aria-labelledby="opsTabSettings" hidden>'
        + render_data_policy_settings_panel(db_path=db_path)
        + "</div></section>"
    )
