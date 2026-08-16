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
from pipeline.ir_approval_panel import read_ir_approval_review, render_ir_approval_panel
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


class SecCoverageCompanyView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    name: str
    role: str
    sec_validated: bool
    filing_regime: str
    coverage_status: str
    coverage_tone: Tone
    notes: str


class SecCoverageSummaryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_tracked: int = 0
    portfolio_count: int = 0
    evaluation_count: int = 0
    watchlist_count: int = 0
    validated_count: int = 0
    gap_count: int = 0
    companies: tuple[SecCoverageCompanyView, ...] = ()


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
        "Automatic full collection. Portfolio companies receive the deepest recurring coverage.",
    ),
    ListType.EVALUATION: (
        "Evaluation",
        "On demand",
        "Full collection only after an owner request. No background document crawl.",
    ),
    ListType.WATCHLIST: (
        "Watchlist",
        "Metadata only",
        "Metadata only. No financial-fact, filing, IR-document, or transcript hydration.",
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
    )


def read_sec_coverage_state(db_path: Path | None) -> SecCoverageSummaryView:
    """Read SEC collection priority and company coverage gaps without taking write locks."""

    if db_path is None or not db_path.is_file():
        return SecCoverageSummaryView()
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracked_companies'"
            ).fetchone()
            if not has_table:
                return SecCoverageSummaryView()
            rows = conn.execute(
                "SELECT ticker, name, list_type, sec_validated, filing_regime, archived_at "
                "FROM tracked_companies WHERE archived_at IS NULL "
                "ORDER BY CASE list_type WHEN 'portfolio' THEN 1 WHEN 'evaluation' THEN 2 ELSE 3 END, ticker"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return SecCoverageSummaryView()

    companies: list[SecCoverageCompanyView] = []
    portfolio_count = 0
    evaluation_count = 0
    watchlist_count = 0
    validated_count = 0
    gap_count = 0

    for row in rows:
        ticker = str(row["ticker"])
        name = str(row["name"])
        role = str(row["list_type"])
        sec_validated = bool(row["sec_validated"])
        filing_regime = str(row["filing_regime"] or "10-K")

        if role == "portfolio":
            portfolio_count += 1
            if sec_validated:
                status = "Automatic full"
                tone: Tone = "ok"
                notes = f"Active SEC collection ({filing_regime})"
                validated_count += 1
            else:
                status = "Coverage gap"
                tone = "warn"
                notes = "Portfolio issuer pending SEC profile validation"
                gap_count += 1
        elif role == "evaluation":
            evaluation_count += 1
            if sec_validated:
                status = "On demand"
                tone = "ok"
                notes = f"Owner-requested SEC collection ready ({filing_regime})"
                validated_count += 1
            else:
                status = "Pending validation"
                tone = "warn"
                notes = "Evaluation issuer pending SEC profile validation"
                gap_count += 1
        else:
            watchlist_count += 1
            status = "Metadata only"
            tone = "ok"
            notes = "SEC document crawl excluded by policy"

        companies.append(
            SecCoverageCompanyView(
                ticker=ticker,
                name=name,
                role=role.capitalize(),
                sec_validated=sec_validated,
                filing_regime=filing_regime,
                coverage_status=status,
                coverage_tone=tone,
                notes=notes,
            )
        )

    return SecCoverageSummaryView(
        total_tracked=len(rows),
        portfolio_count=portfolio_count,
        evaluation_count=evaluation_count,
        watchlist_count=watchlist_count,
        validated_count=validated_count,
        gap_count=gap_count,
        companies=tuple(companies),
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
    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax('
        'var(--grid-card-sm),1fr));gap:var(--sp-3);">'
        f"{cards}</div>"
    )


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
        '<div style="overflow-x:auto;">'
        '<table class="p-table" aria-label="Collection behavior by company priority">'
        f"<thead><tr><th>Source and artifact</th>{header}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_issuers(view: DataPolicySettingsView) -> str:
    cards = "".join(
        '<article class="k-well">'
        '<div style="display:flex;align-items:center;justify-content:space-between;'
        'gap:var(--sp-3);flex-wrap:wrap;">'
        f'<div><span class="k-ticker-symbol">{escape(issuer.ticker)}</span>'
        f'<div class="k-card-meta">Adapter <code>{escape(issuer.adapter_key)}</code> · '
        f"policy <code>{escape(issuer.policy_sha256[:12])}</code></div></div>"
        f'<a class="k-btn k-btn-quiet k-btn-sm" data-capability="source-policy.open-authority" '
        f'href="{escape(issuer.authority_url, quote=True)}" target="_blank" rel="noopener">'
        "Open approved IR page ↗</a></div>"
        '<div style="display:flex;gap:var(--sp-2);flex-wrap:wrap;margin-top:var(--sp-3);">'
        f'<span class="k-chip">Last {issuer.quarter_window} reported quarters</span>'
        f'<span class="k-chip k-chip-mono">SEC {escape(", ".join(issuer.sec_forms))}</span>'
        f'<span class="k-chip">Text transcripts {"allowed" if issuer.accepts_text_transcripts else "excluded"}</span>'
        f'<span class="k-chip">Webcasts {"allowed" if issuer.accepts_webcasts else "excluded"}</span>'
        "</div></article>"
        for issuer in view.approved_issuers
    )
    return f'<div style="display:grid;gap:var(--sp-3);">{cards}</div>'


def _render_sec_coverage(coverage: SecCoverageSummaryView) -> str:
    if coverage.total_tracked == 0:
        return (
            '<div class="k-well">'
            '<div class="k-card-row-title">SEC collection priority &amp; coverage gaps</div>'
            '<p class="k-card-meta">No tracked company records found in the database. SEC collection requires registered company targets.</p></div>'
        )
    cards = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax('
        'var(--grid-card-sm),1fr));gap:var(--sp-3);margin-bottom:var(--sp-3);">'
        f'<div class="k-well"><div class="k-label">Portfolio issuers</div><div class="k-card-row-title">{coverage.portfolio_count}</div><div class="k-card-meta">Automatic SEC collection</div></div>'
        f'<div class="k-well"><div class="k-label">Evaluation issuers</div><div class="k-card-row-title">{coverage.evaluation_count}</div><div class="k-card-meta">On-demand collection</div></div>'
        f'<div class="k-well"><div class="k-label">Watchlist / Index</div><div class="k-card-row-title">{coverage.watchlist_count}</div><div class="k-card-meta">Crawl excluded by policy</div></div>'
        f'<div class="k-well"><div class="k-label">SEC Profile Gaps</div><div class="k-card-row-title">{coverage.gap_count}</div><div class="k-card-meta">Pending SEC validation</div></div>'
        "</div>"
    )
    rows = "".join(
        "<tr>"
        f'<td><span class="k-ticker-symbol">{escape(c.ticker)}</span> <span class="k-card-meta">{escape(c.name)}</span></td>'
        f'<td><span class="k-chip">{escape(c.role)}</span></td>'
        f'<td><span class="k-chip k-chip-mono">{escape(c.filing_regime)}</span></td>'
        f'<td><span class="k-pill k-pill-{c.coverage_tone}">{escape(c.coverage_status)}</span></td>'
        f'<td><span class="k-card-meta">{escape(c.notes)}</span></td>'
        "</tr>"
        for c in coverage.companies
    )
    table = (
        '<div style="overflow-x:auto;">'
        '<table class="p-table" aria-label="SEC Collection Priority and Company Coverage">'
        "<thead><tr><th>Company</th><th>Priority role</th><th>Regime</th><th>SEC status</th><th>Policy notes</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )
    return (
        '<div class="k-well">'
        '<div class="k-card-row-title">SEC collection priority &amp; coverage gaps</div>'
        '<p class="k-card-meta">Priority-governed SEC CompanyFacts and native filing collection status across tracked companies.</p>'
        f"{cards}{table}</div>"
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
    backlog = str(state.backlog_count or 0)
    next_probe = state.next_probe_at or "not scheduled"
    reason = state.last_reason_code or "none"
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
            f'<td><span class="k-chip">{escape(ev.event_type)}</span></td>'
            f'<td><span class="k-card-meta">{escape(ev.reason_code or "—")}</span></td>'
            f'<td><span class="k-card-meta">{escape(str(ev.state_from or "—"))} → {escape(str(ev.state_to or "—"))}</span></td>'
            "</tr>"
            for ev in state.recent_events
        )
        events_html = (
            '<div style="margin-top:var(--sp-3);">'
            '<div class="k-label">Recent recovery receipts &amp; transitions</div>'
            '<div style="overflow-x:auto;"><table class="p-table" aria-label="Recent FMP recovery events">'
            "<thead><tr><th>Timestamp</th><th>Event type</th><th>Reason</th><th>State transition</th></tr></thead>"
            f"<tbody>{event_rows}</tbody></table></div></div>"
        )
    return (
        '<div class="k-well">'
        '<div style="display:flex;align-items:center;justify-content:space-between;'
        'gap:var(--sp-3);flex-wrap:wrap;">'
        '<div class="k-card-row-title">FMP recovery telemetry</div>'
        f'<span class="k-pill {tone}">{escape(availability_labels[state.provider_availability])}</span>'
        "</div>"
        '<dl style="display:grid;grid-template-columns:repeat(auto-fit,minmax('
        'var(--grid-card-sm),1fr));gap:var(--sp-3);margin:var(--sp-3) 0 0;">'
        f'<div><dt class="k-label">Circuit state</dt><dd>{escape(state.circuit_state)}</dd></div>'
        f'<div><dt class="k-label">Network admission</dt><dd>{escape(admission_labels[state.circuit_admission])}</dd></div>'
        f'<div><dt class="k-label">Provider availability</dt><dd>{escape(availability_labels[state.provider_availability])}</dd></div>'
        f'<div><dt class="k-label">Refresh backlog</dt><dd>{escape(backlog)}</dd></div>'
        f'<div><dt class="k-label">Pending / leased</dt><dd>{state.pending_count or 0} / {state.leased_count or 0}</dd></div>'
        f'<div><dt class="k-label">Satisfied / terminal</dt><dd>{state.satisfied_count or 0} / {state.terminal_count or 0}</dd></div>'
        f'<div><dt class="k-label">Next recovery probe</dt><dd>{escape(next_probe)}</dd></div>'
        f'<div><dt class="k-label">Last reason code</dt><dd>{escape(reason)}</dd></div>'
        f'<div><dt class="k-label">Last successful request</dt><dd>{escape(provider_last_success)}</dd></div>'
        f'<div><dt class="k-label">Success evidence</dt><dd>{escape(provider_success_evidence)}</dd></div>'
        f'<div><dt class="k-label">Corpus coverage</dt><dd>{state.corpus_ticker_count or 0} companies</dd></div>'
        f'<div><dt class="k-label">Latest corpus capture</dt><dd>{escape(corpus_last_seen)}</dd></div>'
        "</dl>"
        + (
            '<div class="k-label">Queued companies</div>'
            f'<div style="display:flex;gap:var(--sp-2);flex-wrap:wrap;">{queue}</div>'
            if queue
            else ""
        )
        + events_html
        + "</div>"
    )


def render_data_policy_settings_panel(
    view: DataPolicySettingsView | None = None,
    *,
    db_path: Path | None = None,
) -> str:
    """Render policy plus a read-only runtime projection when a DB is supplied."""

    resolved = view or build_data_policy_settings_view(db_path=db_path)
    return (
        '<section class="k-card k-card-stack" data-settings-panel="data-collection" '
        'aria-labelledby="data-policy-settings-title">'
        '<div class="k-toolbar">'
        '<div><h2 class="k-toolbar-title" id="data-policy-settings-title">Data collection policy</h2>'
        f'<div class="k-card-meta">Read-only · policy {escape(resolved.policy_version)}</div></div>'
        '<span class="k-pill k-pill-ok">Policy enforced</span></div>'
        "<p>Company priority controls collection depth. Portfolio runs automatically; evaluation "
        "runs only after an owner request; watchlist remains metadata-only; index members receive "
        "FMP screening facts only. Webcasts are excluded.</p>"
        + _render_roles(resolved)
        + '<h3 class="k-card-title">Source behavior by company priority</h3>'
        + _render_matrix(resolved)
        + '<h3 class="k-card-title">SEC collection priority &amp; coverage gaps</h3>'
        + _render_sec_coverage(resolved.sec_coverage)
        + '<h3 class="k-card-title">Owner-approved issuer adapters</h3>'
        + _render_issuers(resolved)
        + render_ir_approval_panel(read_ir_approval_review(db_path))
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
        'style="min-block-size:var(--touch-target-size);" tabindex="0" '
        "onclick=\"switchOpsTab('queue')\">Operations</button>"
        '<button type="button" id="opsTabSettings" class="k-chip k-chip-btn k-chip-tab" '
        'role="tab" aria-selected="false" aria-controls="opsPaneSettings" '
        'style="min-block-size:var(--touch-target-size);" tabindex="-1" '
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
