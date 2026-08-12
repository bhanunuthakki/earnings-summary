"""Read-only Settings view of collection policy and FMP recovery telemetry."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from models.companies import ListType
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


class DataPolicySettingsView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str
    roles: tuple[CoverageRoleView, ...]
    rows: tuple[PolicyRowView, ...]
    approved_issuers: tuple[ApprovedIssuerView, ...]
    fmp_state: FmpOperationalReadModel


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
        + '<h3 class="k-card-title">Owner-approved issuer adapters</h3>'
        + _render_issuers(resolved)
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
