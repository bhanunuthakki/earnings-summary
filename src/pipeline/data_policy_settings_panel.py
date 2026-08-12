"""Read-only Settings view of the canonical source collection policy.

The panel deliberately exposes policy before runtime telemetry.  The recovery
tables that will eventually report the FMP circuit and refresh backlog are not
on ``origin/main`` yet, so this module does not probe for them or guess at live
health.  Its typed read model says ``not_yet_wired`` until that integration is
released explicitly.
"""

from __future__ import annotations

from enum import StrEnum
from html import escape
from typing import Literal

from pydantic import BaseModel, ConfigDict

from models.companies import ListType
from pipeline.source_policy import (
    DISPLAY_ROLE_ORDER,
    POLICY_VERSION,
    ArtifactKind,
    AuthorizationReason,
    CollectionMode,
    CollectionSource,
    decision_for,
    issuer_policy,
    mode_for_role,
)


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


class FmpOperationalReadModel(BaseModel):
    """Truthful placeholder for the unreleased recovery-state integration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    integration_state: Literal["not_yet_wired"] = "not_yet_wired"
    provider_mode: Literal["not_yet_wired"] = "not_yet_wired"
    circuit_state: Literal["not_yet_wired"] = "not_yet_wired"
    backlog_count: int | None = None
    next_probe_at: str | None = None


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
        "Owner-approved issuer pages; last 5 reported quarters; presentations and releases before web search.",
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


def build_data_policy_settings_view() -> DataPolicySettingsView:
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
        fmp_state=FmpOperationalReadModel(),
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
    return (
        '<div class="k-well k-well-warn">'
        '<div class="k-card-row-title">FMP recovery telemetry is not yet wired</div>'
        "<p>No live health claim is shown. The released Settings surface does not query an "
        "unreleased recovery migration or infer circuit state from ordinary failures.</p>"
        '<dl style="display:grid;grid-template-columns:repeat(auto-fit,minmax('
        'var(--grid-card-sm),1fr));gap:var(--sp-3);margin:var(--sp-3) 0 0;">'
        f'<div><dt class="k-label">Provider mode</dt><dd>{escape(state.provider_mode.replace("_", " ").capitalize())}</dd></div>'
        f'<div><dt class="k-label">Circuit</dt><dd>{escape(state.circuit_state.replace("_", " ").capitalize())}</dd></div>'
        '<div><dt class="k-label">Refresh backlog</dt><dd>not yet wired</dd></div>'
        '<div><dt class="k-label">Next recovery probe</dt><dd>not yet wired</dd></div>'
        "</dl></div>"
    )


def render_data_policy_settings_panel(
    view: DataPolicySettingsView | None = None,
) -> str:
    """Render the policy and honest runtime boundary; performs no I/O."""

    resolved = view or build_data_policy_settings_view()
    return (
        '<section class="k-card k-card-stack" data-settings-panel="data-collection" '
        'aria-labelledby="data-policy-settings-title">'
        '<div class="k-toolbar">'
        '<div><h2 class="k-toolbar-title" id="data-policy-settings-title">Data collection policy</h2>'
        f'<div class="k-card-meta">Read-only · policy {escape(resolved.policy_version)}</div></div>'
        '<span class="k-pill">Behavior dormant</span></div>'
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


def render_operations_settings_shell() -> str:
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
        + render_data_policy_settings_panel()
        + "</div></section>"
    )
