"""Immutable, locally owned design-language vocabulary and approvals.

This module intentionally contains declarations and derived projections only.
It does not scan files, render CSS, or make policy decisions at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from ui.tokens import (
    CHROME_TOKENS as _CHROME_TOKENS,
)
from ui.tokens import (
    FONT_FAMILY_KEYWORDS,
    INDENT_TOKEN_NAMES,
    INDENT_TOKEN_VALUES,
    RADIUS_PX,
    RAIL_TOKEN_NAMES,
    RAIL_TOKEN_VALUES,
    TYPE_SCALE_PX,
    palette_css,
)
from ui.tokens import (
    INDENT_TOKENS as _INDENT_TOKENS,
)
from ui.tokens import (
    PALETTE_DARK as _PALETTE_DARK,
)
from ui.tokens import (
    PALETTE_LIGHT as _PALETTE_LIGHT,
)
from ui.tokens import (
    RAIL_TOKENS as _RAIL_TOKENS,
)

REGISTRY_VERSION = "1.10.0"

# The canonical token module owns mutable dictionaries for generation and
# composition. This registry exposes read-only views so its public import
# surface cannot mutate that canonical state at runtime.
CHROME_TOKENS: Mapping[str, str] = MappingProxyType(_CHROME_TOKENS)
INDENT_TOKENS: Mapping[str, str] = MappingProxyType(_INDENT_TOKENS)
RAIL_TOKENS: Mapping[str, str] = MappingProxyType(_RAIL_TOKENS)
PALETTE_DARK: Mapping[str, str] = MappingProxyType(_PALETTE_DARK)
PALETTE_LIGHT: Mapping[str, str] = MappingProxyType(_PALETTE_LIGHT)

__all__ = (
    "BESPOKE_BUTTON_APPROVALS",
    "BESPOKE_BUTTON_OK",
    "CARD_ARCHETYPES",
    "CCACTION_PINNED",
    "CCACTION_REGRESSION_FLOOR",
    "CHROME_TOKENS",
    "DYNAMIC_VISUAL_CONTRACTS",
    "EXEMPT",
    "FAMILY_MASTER_SOURCES",
    "FONT_FAMILY_KEYWORDS",
    "FONT_SIZE_EXEMPT",
    "GLOBAL_MASTER_SOURCES",
    "GOVERNED",
    "GRIDS_BY_SELECTOR",
    "GRID_ARCHETYPES",
    "INDENT_TOKENS",
    "INDENT_TOKEN_NAMES",
    "INDENT_TOKEN_VALUES",
    "LOCAL_PROPERTY_CONTRACTS",
    "MASTER_GEOMETRY_CONTRACTS",
    "MASTER_SOURCES",
    "MONO_TABLE_ALLOWLIST",
    "MONO_TABLE_APPROVALS",
    "PALETTE_DARK",
    "PALETTE_LIGHT",
    "PERMANENT_EXEMPTIONS",
    "QUARANTINE",
    "QUARANTINE_ENTRIES",
    "RADIUS_PX",
    "RADIUS_SANCTIONED",
    "RAIL_TOKENS",
    "RAIL_TOKEN_NAMES",
    "RAIL_TOKEN_VALUES",
    "REGISTERED",
    "REGISTRY_VERSION",
    "RUNTIME_VISUAL_CONTRACTS",
    "SHAPES_BY_SELECTOR",
    "SHAPE_ARCHETYPES",
    "SURFACE_SANCTIONS",
    "TITLES_BY_SELECTOR",
    "TITLE_PLACEMENTS",
    "TYPE_SCALE_PX",
    "VISUAL_EMITTER_MANIFEST",
    "BespokeButtonApproval",
    "CCActionRegressionFloor",
    "CardArchetype",
    "DynamicVisualContract",
    "EmitterDisposition",
    "EvidenceAdapter",
    "EvidenceMode",
    "GridArchetype",
    "GridSignature",
    "LocalPropertyContract",
    "MasterGeometryContract",
    "MonoTableApproval",
    "PermanentExemption",
    "QuarantineEntry",
    "RuntimeVisualContract",
    "ShapeArchetype",
    "ShapeSignature",
    "SurfaceSanction",
    "TitlePlacement",
    "VisualEmitterEntry",
    "palette_css",
    "validate_visual_emitter_manifest",
)


@dataclass(frozen=True)
class ShapeSignature:
    selector: str
    radius_token: str | None
    border_signature: str | None
    elevation_token: str | None


@dataclass(frozen=True)
class ShapeArchetype:
    name: str
    signatures: tuple[ShapeSignature, ...]


@dataclass(frozen=True)
class CardArchetype:
    """One closed card composition verified in production-rendered routes."""

    name: str
    selector: str
    padding_block_token: str
    padding_inline_token: str
    title_selector: str | None
    title_size_token: str | None
    title_family_token: str | None
    title_color_token: str | None
    title_weight: int | None


@dataclass(frozen=True)
class GridSignature:
    selector: str
    column_signature: str


@dataclass(frozen=True)
class GridArchetype:
    name: str
    signatures: tuple[GridSignature, ...]


@dataclass(frozen=True)
class TitlePlacement:
    key: str
    selector: str | None
    placement: str


@dataclass(frozen=True)
class PermanentExemption:
    surface: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class QuarantineEntry:
    surface: str
    dimension: str
    owner: str
    rationale: str
    expires_on: date


@dataclass(frozen=True)
class BespokeButtonApproval:
    class_name: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class MonoTableApproval:
    selector: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class SurfaceSanction:
    surface: str
    dimension: str
    values: frozenset[str]
    owner: str
    rationale: str


@dataclass(frozen=True)
class CCActionRegressionFloor:
    surface: str
    owner: str
    rationale: str


class EmitterDisposition(StrEnum):
    """Lifecycle status for a visual emitter in the locally owned product."""

    PRODUCTION = "production"
    GENERATED = "generated"
    VENDOR = "vendor"
    NONVISUAL = "nonvisual"


class EvidenceAdapter(StrEnum):
    """Static evidence acquisition adapters required for one visual emitter."""

    PYTHON_CSS = "python-css"
    HTML = "html"
    SVG = "svg"
    RUNTIME_JS = "runtime-js"
    TOKEN = "token"


class EvidenceMode(StrEnum):
    """How the shared scanner may interpret evidence from an emitter."""

    STATIC = "static"
    SCOPED = "scoped"


@dataclass(frozen=True)
class VisualEmitterEntry:
    """One shipped visual-emitter contract.

    A path may have multiple adapters (for example, Python CSS plus inline SVG),
    but it may appear only once.  This makes omission and conflicting ownership
    deterministic errors instead of a scanner convention.
    """

    path: str
    disposition: EmitterDisposition
    adapter_kinds: frozenset[EvidenceAdapter]
    evidence_modes: frozenset[EvidenceMode]
    owner: str
    rationale: str


@dataclass(frozen=True)
class LocalPropertyContract:
    """A scoped component variable, never a duplicate canonical token value."""

    name: str
    surfaces: frozenset[str]
    value_grammar: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class MasterGeometryContract:
    surface: str
    digest: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class DynamicVisualContract:
    surface: str
    digest: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class RuntimeVisualContract:
    """One dynamic geometry property owned by a closed interaction primitive."""

    surface: str
    property_name: str
    value_pattern: str
    owner: str
    rationale: str


def validate_visual_emitter_manifest(entries: tuple[VisualEmitterEntry, ...]) -> None:
    """Reject ambiguous registry data before any derived projection is exposed."""

    paths: set[str] = set()
    for entry in entries:
        if not entry.path or entry.path != entry.path.strip():
            raise ValueError("visual emitter path must be nonblank and normalized")
        if entry.path in paths:
            raise ValueError(f"duplicate visual emitter path: {entry.path}")
        paths.add(entry.path)
        if not entry.adapter_kinds:
            raise ValueError(f"visual emitter requires an adapter: {entry.path}")
        if not entry.evidence_modes:
            raise ValueError(f"visual emitter requires an evidence mode: {entry.path}")
        if not entry.owner.strip():
            raise ValueError(f"visual emitter requires an owner: {entry.path}")
        if not entry.rationale.strip():
            raise ValueError(f"visual emitter requires a rationale: {entry.path}")


SHAPE_ARCHETYPES = (
    ShapeArchetype(
        "macro-container",
        (
            ShapeSignature(".k-card", "radius-card", "bw-thin solid border", "shadow-card"),
            ShapeSignature(".k-desk-hero", "radius-card", "bw-thin solid border", "shadow-card"),
            ShapeSignature(".k-say-do-timeline", "radius-card", "bw-thin solid border", None),
            ShapeSignature(".k-action-card", "radius-card", "bw-thin solid border", "shadow-card"),
        ),
    ),
    ShapeArchetype("micro-inset", (ShapeSignature(".k-well", "radius", None, None),)),
    ShapeArchetype(
        "slide-drawer",
        (
            ShapeSignature(
                ".k-overlay.k-drawer", "radius-drawer", "bw-thin solid border", "shadow-drawer"
            ),
        ),
    ),
    ShapeArchetype(
        "control-button", (ShapeSignature(".k-btn", "radius", "bw-thin solid transparent", None),)
    ),
    ShapeArchetype(
        "pill-chip",
        (
            ShapeSignature(".k-chip", "radius-full", "bw-thin solid border", None),
            ShapeSignature(".k-pill", "radius-full", None, None),
        ),
    ),
    ShapeArchetype("micro-mark", (ShapeSignature(".k-dot", "radius-full", None, None),)),
)

CARD_ARCHETYPES = (
    CardArchetype(
        "section",
        ".k-card-section",
        "sp-3",
        "sp-3",
        ".k-card-title",
        "fs-title",
        "sans",
        "fg",
        600,
    ),
    CardArchetype(
        "stat",
        ".k-card-stat",
        "indent-0",
        "indent-0",
        ".stat-heading",
        "fs-caption",
        "sans",
        "muted",
        600,
    ),
    CardArchetype(
        "action",
        ".k-card-action",
        "sp-2",
        "sp-3",
        ".k-card-row-title",
        "fs-body",
        "sans",
        "fg",
        600,
    ),
    CardArchetype(
        "navigation",
        ".k-card-nav",
        "sp-3",
        "sp-3",
        None,
        None,
        None,
        None,
        None,
    ),
)

GRID_ARCHETYPES = (
    GridArchetype("shell-ground", (GridSignature(".k-grid-shell", "minmax(0, 1fr)"),)),
    GridArchetype("single-column", (GridSignature(".k-grid-single", "minmax(0, 1fr)"),)),
    GridArchetype(
        "two-column-split-rail",
        (
            GridSignature(".k-grid-split-rail", "minmax(0, 1fr) rail-sm"),
            GridSignature(".k-grid-split-rail-lg", "minmax(0, 1fr) rail-lg"),
        ),
    ),
    GridArchetype(
        "three-column-matrix", (GridSignature(".k-grid-matrix", "repeat(3, minmax(0, 1fr))"),)
    ),
    GridArchetype(
        "auto-fit-card-grid",
        (
            GridSignature(".card-grid-stat", "repeat(auto-fit, minmax(grid-card-sm, 1fr))"),
            GridSignature(".card-grid-risk", "repeat(auto-fit, minmax(grid-card-md, 1fr))"),
            GridSignature(".card-grid-action", "repeat(auto-fit, minmax(grid-card-lg, 1fr))"),
            GridSignature(".k-matrix-grid", "repeat(auto-fit, minmax(grid-card-md, 1fr))"),
        ),
    ),
)

TITLE_PLACEMENTS = (
    TitlePlacement("card-title", ".k-card-title", "interior"),
    TitlePlacement("well-title", ".k-well-title", "interior"),
    TitlePlacement("drawer-head", ".cc-drawer-head", "interior"),
    TitlePlacement("peek-head", ".cc-peek-head", "interior"),
    TitlePlacement("section-heading", None, "exterior-semantic-h2"),
)

PERMANENT_EXEMPTIONS = (
    PermanentExemption(
        "ui/tokens.py", "design-system", "Owns the canonical palette and scale literals."
    ),
    PermanentExemption(
        "ui/conformance_scan.py",
        "design-system",
        "Owns scanner diagnostics containing the CSS discovery signal; renders no surface.",
    ),
)

QUARANTINE_ENTRIES: tuple[QuarantineEntry, ...] = ()

BESPOKE_BUTTON_APPROVALS = (
    BespokeButtonApproval("cc-drawer-close", "work-os", "Named drawer close glyph."),
    BespokeButtonApproval("tcc-drawer-close", "work-os", "Named ticker drawer close glyph."),
    BespokeButtonApproval("cc-peek-close", "work-os", "Named peek close glyph."),
    BespokeButtonApproval("cc-palette-close", "work-os", "Named palette close glyph."),
    BespokeButtonApproval("tri-d-close", "research-ui", "Named triage drawer close glyph."),
    BespokeButtonApproval("chat-close", "research-ui", "Named chat close glyph."),
    BespokeButtonApproval("cmt-close", "research-ui", "Named comment close glyph."),
    BespokeButtonApproval("ask-pop-close", "research-ui", "Named Ask popover close glyph."),
    BespokeButtonApproval("cc-palette-btn", "work-os", "Icon-only command launcher."),
    BespokeButtonApproval("cc-theme-toggle", "work-os", "Icon-only theme control."),
    BespokeButtonApproval("cc-tab", "work-os", "Specialized tab control."),
    BespokeButtonApproval("fact-doorway", "research-ui", "Datum doorway into Ask."),
    BespokeButtonApproval("prep-ask", "research-ui", "Earnings-prep doorway into Ask."),
    BespokeButtonApproval("ask-dock-ctl", "research-ui", "Specialized Ask-dock control cluster."),
    BespokeButtonApproval("up-watch-item", "research-ui", "Grandfathered interactive watch row."),
    BespokeButtonApproval("tri-text", "research-ui", "Grandfathered triage text control."),
    BespokeButtonApproval("dq-peek", "research-ui", "Grandfathered data-quality peek control."),
)

MONO_TABLE_APPROVALS = (
    MonoTableApproval(
        ".pfc-table", "portfolio", "Correlation headers are ticker identifiers, not prose labels."
    ),
)

SURFACE_SANCTIONS: tuple[SurfaceSanction, ...] = ()

CCACTION_REGRESSION_FLOOR = (
    CCActionRegressionFloor("dashboard/inbox.py", "work-os", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/advisor_memos_panel.py", "work-os", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/allocation_decisions_panel.py", "work-os", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/allocation_recommendation_panel.py", "work-os", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("pipeline/cc_action.py", "work-os", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/dcf_globals_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/decision_journal_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("pipeline/diet_panel.py", "research-ui", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/discovery_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("pipeline/evals_panel.py", "research-ui", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/explore_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/journal_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("pipeline/ledger_panel.py", "research-ui", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/mobile_inbox_panel.py", "work-os", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("pipeline/peeks.py", "work-os", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/portfolio_panel.py", "portfolio", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/position_lifecycle_panel.py", "portfolio", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/positioning_panel.py", "portfolio", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/ticker_command_center.py", "work-os", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/ticker_settings_panel.py", "work-os", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("pipeline/triage_panel.py", "research-ui", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "pipeline/validation_issues_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "pipeline/worldview_panel.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("redteam/brief.py", "research-ui", "Current CCAction adopter."),
    CCActionRegressionFloor(
        "report/renderers/workspace_comments.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "report/renderers/workspace_dcf.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor(
        "report/renderers/workspace_decision_card.py", "research-ui", "Current CCAction adopter."
    ),
    CCActionRegressionFloor("ui/controls.py", "design-system", "Current CCAction adopter."),
)

_BHA_92_SURFACES = frozenset(
    {
        "dashboard/_styles.py",
        "dashboard/inbox.py",
        "dashboard/upcoming.py",
        "pipeline/advisor_memos_panel.py",
        "pipeline/allocation_decisions_panel.py",
        "pipeline/allocation_recommendation_panel.py",
        "pipeline/analytical_dashboard_html.py",
        "pipeline/attribution_panel.py",
        "pipeline/calibration_scorecard_panel.py",
        "pipeline/cc_action.py",
        "pipeline/cc_overlay.py",
        "pipeline/credibility_panel.py",
        "pipeline/cron_health_panel.py",
        "pipeline/dashboard_html.py",
        "pipeline/data_policy_settings_panel.py",
        "pipeline/dcf_coverage_panel.py",
        "pipeline/dcf_globals_panel.py",
        "pipeline/decision_journal_panel.py",
        "pipeline/diet_panel.py",
        "pipeline/discovery_panel.py",
        "pipeline/etf_workup.py",
        "pipeline/evals_panel.py",
        "pipeline/explore_panel.py",
        "pipeline/fact_overrides_panel.py",
        "pipeline/ir_coverage_panel.py",
        "pipeline/journal_panel.py",
        "pipeline/ledger_panel.py",
        "pipeline/mobile_inbox_panel.py",
        "pipeline/model_eval_panel.py",
        "pipeline/open_loops.py",
        "pipeline/operations_panel.py",
        "pipeline/peeks.py",
        "pipeline/portfolio_console_panel.py",
        "pipeline/portfolio_panel.py",
        "pipeline/position_lifecycle_panel.py",
        "pipeline/positioning_panel.py",
        "pipeline/redteam_pnl_panel.py",
        "pipeline/research_cockpit.py",
        "pipeline/restatements_panel.py",
        "pipeline/section_coverage_panel.py",
        "pipeline/senior_partner_brief_panel.py",
        "pipeline/source_calls_panel.py",
        "pipeline/source_viewers.py",
        "pipeline/thesis_ledger_panel.py",
        "pipeline/ticker_command_center.py",
        "pipeline/ticker_settings_panel.py",
        "pipeline/triage_panel.py",
        "pipeline/validation_issues_panel.py",
        "pipeline/work_os_copilot.py",
        "pipeline/work_os_shell.py",
        "pipeline/worldview_panel.py",
        "redteam/brief.py",
        "report/renderers/charts_v2.py",
        "report/renderers/workspace_charts.py",
        "report/renderers/workspace_chat.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_dcf.py",
        "report/renderers/workspace_sections/chrome.py",
        "report/renderers/workspace_sections/company.py",
        "report/renderers/workspace_sections/thesis_risk.py",
        "report/renderers/workspace_styles.py",
        "ui/cite_marks.py",
        "ui/conformance_scan.py",
        "ui/controls.py",
        "ui/living_grid.py",
        "ui/source_chip.py",
        "ui/tokens.py",
        "viewspec/render.py",
    }
)

_BHA_89_TO_92_ADDITIONAL_EMITTERS = (
    "advisor/sizing_intent_review_page.py",
    "dashboard/_card.py",
    "dashboard/evidence_drawer.py",
    "dashboard/feed.py",
    "pipeline/annual_letter_panel.py",
    "pipeline/analysis_styles.py",
    "pipeline/calibration_receipt.py",
    "pipeline/console_scaffold.py",
    "pipeline/ir_approval_panel.py",
    "pipeline/key_metrics.py",
    "pipeline/operations_styles.py",
    "pipeline/performance_risk_panel.py",
    "pipeline/provenance_panel.py",
    "pipeline/portfolio_styles.py",
    "pipeline/research_panel_styles.py",
    "pipeline/since_last.py",
    "pipeline/three_regime_renderer.py",
    "pipeline/work_os_overview.py",
    "pipeline/work_os_styles.py",
    "pipeline/work_os_research.py",
    "pipeline/you_said.py",
    "report/renderers/markdown.py",
    "report/renderers/workspace_decision_card.py",
    "report/renderers/workspace_html.py",
    "report/renderers/workspace_script.py",
    "report/renderers/workspace_sections/_shared.py",
    "report/renderers/workspace_sections/boot.py",
    "report/renderers/workspace_sections/earnings.py",
    "report/renderers/workspace_sections/eval_screen.py",
    "report/renderers/workspace_sections/exec_comp.py",
    "report/renderers/workspace_sections/financials.py",
    "report/renderers/workspace_sections/position.py",
    "report/renderers/workspace_sections/saydo.py",
    "report/renderers/workspace_sections/sources.py",
    "report/renderers/workspace_sections/synthesis.py",
    "report/renderers/workspace_sections/valuation.py",
    "ui/prose.py",
    "ui/time.py",
    "execution/build_earnings_calendar.py",
    "execution/comments_server_alert_routes.py",
    "execution/comments_server_governed_alert_routes.py",
)

_GENERATED_FRONTEND_EMITTERS = (
    "design-system/src/components/Button.tsx",
    "design-system/src/components/Chip.tsx",
    "design-system/src/components/DateField.tsx",
    "design-system/src/components/Dot.tsx",
    "design-system/src/components/Input.tsx",
    "design-system/src/components/Label.tsx",
    "design-system/src/components/Menu.tsx",
    "design-system/src/components/MultiSelect.tsx",
    "design-system/src/components/NumText.tsx",
    "design-system/src/components/Pill.tsx",
    "design-system/src/components/Select.tsx",
    "design-system/src/components/TickerLabel.tsx",
    "design-system/src/components/Textarea.tsx",
    "design-system/src/components/Toolbar.tsx",
    "design-system/src/components/Well.tsx",
    "design-system/src/index.css",
    "design-system/src/index.ts",
    "design-system/src/lib/tone.ts",
    "design-system/src/styles/controls.css",
    "design-system/src/theme/ThemeProvider.tsx",
    "design-system/src/tokens/tokens.css",
)

_NONVISUAL_CENSUS_CLASSIFICATIONS = (
    "aggregator_sources.py",
    "advisor/memos.py",
    "compute/soft_rule_evaluator.py",
    "decision_conditions.py",
    "decision_extractor.py",
    "etf_sources/nport.py",
    "execution/design_route_canaries.py",
    "execution/land_session_notes.py",
    "execution/verify_design_conformance.py",
    "execution/sync_list_type_from_holdings.py",
    "filing_text_fetcher.py",
    "filings/fmp_sections.py",
    "provenance/fulltext_backfill.py",
    "ir_uploads.py",
    "llm_client.py",
    "pipeline/cc_state.py",
    "redteam/telegram_cmd.py",
    "report/sections/qa_roster.py",
    "research/dcf_tweak.py",
    "report/renderers/workspace_data.py",
    "scheduler_manifest.py",
    "transcript_qa.py",
    "synthesis/tenet_accountability.py",
)

_PYTHON_CSS_SURFACES = frozenset(
    {
        "advisor/memos.py",
        "advisor/sizing_intent_review_page.py",
        "compute/soft_rule_evaluator.py",
        "dashboard/_styles.py",
        "dashboard/feed.py",
        "dashboard/inbox.py",
        "etf_sources/nport.py",
        "execution/build_earnings_calendar.py",
        "execution/comments_server_alert_routes.py",
        "execution/design_route_canaries.py",
        "execution/verify_design_conformance.py",
        "pipeline/advisor_memos_panel.py",
        "pipeline/allocation_decisions_panel.py",
        "pipeline/allocation_recommendation_panel.py",
        "pipeline/analysis_styles.py",
        "pipeline/analytical_dashboard_html.py",
        "pipeline/cc_action.py",
        "pipeline/cc_overlay.py",
        "pipeline/cc_state.py",
        "pipeline/console_scaffold.py",
        "pipeline/cron_health_panel.py",
        "pipeline/dashboard_html.py",
        "pipeline/dcf_globals_panel.py",
        "pipeline/decision_journal_panel.py",
        "pipeline/diet_panel.py",
        "pipeline/discovery_panel.py",
        "pipeline/evals_panel.py",
        "pipeline/explore_panel.py",
        "pipeline/ir_approval_panel.py",
        "pipeline/journal_panel.py",
        "pipeline/ledger_panel.py",
        "pipeline/mobile_inbox_panel.py",
        "pipeline/operations_panel.py",
        "pipeline/operations_styles.py",
        "pipeline/peeks.py",
        "pipeline/performance_risk_panel.py",
        "pipeline/portfolio_panel.py",
        "pipeline/portfolio_styles.py",
        "pipeline/position_lifecycle_panel.py",
        "pipeline/positioning_panel.py",
        "pipeline/provenance_panel.py",
        "pipeline/research_panel_styles.py",
        "pipeline/research_cockpit.py",
        "pipeline/source_viewers.py",
        "pipeline/ticker_command_center.py",
        "pipeline/ticker_settings_panel.py",
        "pipeline/triage_panel.py",
        "pipeline/validation_issues_panel.py",
        "pipeline/work_os_copilot.py",
        "pipeline/work_os_shell.py",
        "pipeline/work_os_styles.py",
        "pipeline/worldview_panel.py",
        "redteam/brief.py",
        "report/renderers/charts_v2.py",
        "report/renderers/workspace_chat.py",
        "report/renderers/workspace_charts.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_dcf.py",
        "report/renderers/workspace_html.py",
        "report/renderers/workspace_decision_card.py",
        "report/renderers/workspace_script.py",
        "report/renderers/workspace_sections/company.py",
        "report/renderers/workspace_sections/thesis_risk.py",
        "report/renderers/workspace_styles.py",
        "ui/cite_marks.py",
        "ui/controls.py",
        "ui/living_grid.py",
        "ui/source_chip.py",
        "viewspec/render.py",
    }
)

_NON_HTML_PYTHON_SURFACES = frozenset(
    {
        "advisor/memos.py",
        "compute/soft_rule_evaluator.py",
        "dashboard/_styles.py",
        "execution/land_session_notes.py",
        "execution/verify_design_conformance.py",
        "pipeline/cc_overlay.py",
        "pipeline/cc_state.py",
        "redteam/telegram_cmd.py",
        "report/renderers/workspace_chat.py",
        "report/renderers/workspace_decision_card.py",
        "scheduler_manifest.py",
        "transcript_qa.py",
        "ui/tokens.py",
    }
)

_RUNTIME_JS_SURFACES = frozenset(
    {
        "dashboard/inbox.py",
        "execution/verify_design_conformance.py",
        "pipeline/advisor_memos_panel.py",
        "pipeline/allocation_decisions_panel.py",
        "pipeline/allocation_recommendation_panel.py",
        "pipeline/cc_action.py",
        "pipeline/cc_overlay.py",
        "pipeline/cron_health_panel.py",
        "pipeline/decision_journal_panel.py",
        "pipeline/dashboard_html.py",
        "pipeline/diet_panel.py",
        "pipeline/discovery_panel.py",
        "pipeline/evals_panel.py",
        "pipeline/explore_panel.py",
        "pipeline/ir_approval_panel.py",
        "pipeline/journal_panel.py",
        "pipeline/ledger_panel.py",
        "pipeline/mobile_inbox_panel.py",
        "pipeline/operations_panel.py",
        "pipeline/performance_risk_panel.py",
        "pipeline/portfolio_console_panel.py",
        "pipeline/portfolio_panel.py",
        "pipeline/position_lifecycle_panel.py",
        "pipeline/positioning_panel.py",
        "pipeline/provenance_panel.py",
        "pipeline/source_calls_panel.py",
        "pipeline/ticker_command_center.py",
        "pipeline/ticker_settings_panel.py",
        "pipeline/triage_panel.py",
        "pipeline/work_os_copilot.py",
        "pipeline/work_os_shell.py",
        "pipeline/worldview_panel.py",
        "redteam/brief.py",
        "report/renderers/workspace_chat.py",
        "report/renderers/workspace_comments.py",
        "report/renderers/workspace_dcf.py",
        "report/renderers/workspace_script.py",
        "ui/conformance_scan.py",
        "ui/living_grid.py",
    }
)

_SVG_SURFACES = frozenset(
    {
        "execution/land_session_notes.py",
        "pipeline/allocation_decisions_panel.py",
        "pipeline/portfolio_panel.py",
        "redteam/telegram_cmd.py",
        "report/renderers/charts_v2.py",
        "report/renderers/workspace_charts.py",
        "scheduler_manifest.py",
        "transcript_qa.py",
        "ui/controls.py",
        "ui/tokens.py",
    }
)

_FRONTEND_ADAPTERS: Mapping[str, frozenset[EvidenceAdapter]] = MappingProxyType(
    {
        "design-system/src/components/DateField.tsx": frozenset(
            {EvidenceAdapter.HTML, EvidenceAdapter.SVG}
        ),
        "design-system/src/components/Menu.tsx": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
        "design-system/src/components/MultiSelect.tsx": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
        "design-system/src/components/NumText.tsx": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
        "design-system/src/components/Select.tsx": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
        "design-system/src/components/TickerLabel.tsx": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
        "design-system/src/index.ts": frozenset({EvidenceAdapter.PYTHON_CSS}),
        "design-system/src/lib/tone.ts": frozenset({EvidenceAdapter.PYTHON_CSS}),
        "design-system/src/styles/controls.css": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
        "design-system/src/theme/ThemeProvider.tsx": frozenset(
            {EvidenceAdapter.PYTHON_CSS, EvidenceAdapter.HTML}
        ),
    }
)


def _owner_for_surface(path: str) -> str:
    if path.startswith("dashboard/") or "work_os" in path:
        return "work-os"
    if path.startswith("pipeline/portfolio") or path.startswith("pipeline/position"):
        return "portfolio"
    if path.startswith("ui/"):
        return "design-system"
    return "research-ui"


def _adapters_for_surface(path: str) -> frozenset[EvidenceAdapter]:
    if path in _FRONTEND_ADAPTERS:
        return _FRONTEND_ADAPTERS[path]
    if path == "ui/tokens.py":
        return frozenset({EvidenceAdapter.SVG})
    if path.endswith(".py"):
        adapters: set[EvidenceAdapter] = set()
        if path not in _NON_HTML_PYTHON_SURFACES:
            adapters.add(EvidenceAdapter.HTML)
        if path in _PYTHON_CSS_SURFACES:
            adapters.add(EvidenceAdapter.PYTHON_CSS)
        if path in _RUNTIME_JS_SURFACES:
            adapters.add(EvidenceAdapter.RUNTIME_JS)
        if path in _SVG_SURFACES:
            adapters.add(EvidenceAdapter.SVG)
        return frozenset(adapters)
    if path.endswith(".css"):
        return frozenset({EvidenceAdapter.PYTHON_CSS})
    if path.endswith((".ts", ".tsx")):
        return frozenset({EvidenceAdapter.HTML})
    raise ValueError(f"unsupported visual emitter path: {path}")


def _evidence_modes_for_surface(path: str) -> frozenset[EvidenceMode]:
    if path == "report/renderers/charts_v2.py":
        # Chart plot geometry is intentionally local, while its visual type,
        # palette, and SVG attributes remain governed by scoped evidence.
        return frozenset({EvidenceMode.STATIC, EvidenceMode.SCOPED})
    return frozenset({EvidenceMode.STATIC})


VISUAL_EMITTER_MANIFEST = (
    tuple(
        VisualEmitterEntry(
            path,
            EmitterDisposition.PRODUCTION,
            _adapters_for_surface(path),
            _evidence_modes_for_surface(path),
            _owner_for_surface(path),
            "Shipped visual output is governed by the design-language master.",
        )
        for path in (*sorted(_BHA_92_SURFACES), *_BHA_89_TO_92_ADDITIONAL_EMITTERS)
    )
    + tuple(
        VisualEmitterEntry(
            path,
            EmitterDisposition.GENERATED,
            _adapters_for_surface(path),
            frozenset({EvidenceMode.STATIC}),
            "design-system",
            "React adapter is governed by deterministic Python generation and parity checks.",
        )
        for path in _GENERATED_FRONTEND_EMITTERS
    )
    + tuple(
        VisualEmitterEntry(
            path,
            EmitterDisposition.NONVISUAL,
            _adapters_for_surface(path),
            frozenset({EvidenceMode.STATIC, EvidenceMode.SCOPED}),
            "data-platform",
            "Markup-like content is parsed data or diagnostics, not rendered product UI.",
        )
        for path in _NONVISUAL_CENSUS_CLASSIFICATIONS
    )
    + (
        VisualEmitterEntry(
            "ui/htmx_runtime.py",
            EmitterDisposition.VENDOR,
            frozenset({EvidenceAdapter.HTML}),
            frozenset({EvidenceMode.STATIC, EvidenceMode.SCOPED}),
            "design-system",
            "Vendored HTMX runtime wrapper emits behavior but owns no product visual language.",
        ),
    )
)
validate_visual_emitter_manifest(VISUAL_EMITTER_MANIFEST)

# This is a projection, not a separately maintained scanner allowlist.
REGISTERED = frozenset(
    entry.path
    for entry in VISUAL_EMITTER_MANIFEST
    if entry.disposition is EmitterDisposition.PRODUCTION
)
GOVERNED = frozenset(
    entry.path
    for entry in VISUAL_EMITTER_MANIFEST
    if entry.disposition in {EmitterDisposition.PRODUCTION, EmitterDisposition.GENERATED}
)

# Literal visual definitions are legal only in these canonical master sources.
# The generated projections are byte-for-byte checked against the Python
# sources by check_design_sync.py; consumer surfaces remain fully scanned.
GLOBAL_MASTER_SOURCES = frozenset(
    {
        "ui/controls.py",
        "design-system/src/styles/controls.css",
        "design-system/src/tokens/tokens.css",
    }
)
FAMILY_MASTER_SOURCES = frozenset(
    {
        "ui/cite_marks.py",
        "ui/source_chip.py",
        "dashboard/_styles.py",
        "execution/build_earnings_calendar.py",
        "pipeline/analysis_styles.py",
        "pipeline/portfolio_styles.py",
        "pipeline/operations_styles.py",
        "pipeline/research_panel_styles.py",
        "report/renderers/workspace_styles.py",
        "pipeline/work_os_styles.py",
        "report/renderers/workspace_charts.py",
        "ui/living_grid.py",
        "viewspec/render.py",
    }
)
MASTER_SOURCES = GLOBAL_MASTER_SOURCES | FAMILY_MASTER_SOURCES

# Exact normalized geometry recipes make a master edit an explicit registry
# mutation.  The compact digest avoids duplicating every selector/property
# identity here while still rejecting any unregistered layout change.
_MASTER_GEOMETRY_DIGESTS: Mapping[str, str] = MappingProxyType(
    {
        "dashboard/_styles.py": "06bef447e15928290d39104bbf46e7741a77547aa46a7e766a3aceb388ec5b4a",  # pragma: allowlist secret
        "design-system/src/styles/controls.css": "4a086f122923dc5ea35c588e370545e7a6cd600852e8d45bd4dce2033f3f9336",  # pragma: allowlist secret
        "design-system/src/tokens/tokens.css": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # pragma: allowlist secret
        "execution/build_earnings_calendar.py": "a2257779753cf8476f0ab93478569ffbd1d116856e596b46d12afcf8e45de114",  # pragma: allowlist secret
        "pipeline/analysis_styles.py": "75476869a35c0e1f08ba3faa1d35c1f08d5e506efe77ed3b1a52f33bc937942a",  # pragma: allowlist secret
        "pipeline/operations_styles.py": "80479df16d7055543dc3af1010c69ff7542234b2b2c21ba59f7179f1bc58e1f4",  # pragma: allowlist secret
        "pipeline/portfolio_styles.py": "8241af8be09f54d58d430febf0199fdbbdd0790c2289ea2153cd43128cf63bdc",  # pragma: allowlist secret
        "pipeline/research_panel_styles.py": "430015d0f97507f8d418d90199dd4a9db3217be2dec492a93c54bf13f41ece0f",  # pragma: allowlist secret
        "pipeline/work_os_styles.py": "3f1aabb52c3afe225131bb0d2f12bdd6c6eda35a6c6688280cc97728bcfc0760",  # pragma: allowlist secret
        "report/renderers/workspace_charts.py": "e55dff6926088b1c08aa42dc69fad725a1f55c15d46a8d9f5c60e60f1773b13a",  # pragma: allowlist secret
        "report/renderers/workspace_styles.py": "27eba0547bdad4a8bf4178452b7e8f5e8ba947a3f8d141ce05f4c5a4e90573a1",  # pragma: allowlist secret
        "ui/cite_marks.py": "0c45d7eefb5ef340b1ec58036f32ec4042f69c41473850fc8624f4968e95783e",  # pragma: allowlist secret
        "ui/controls.py": "5fc62b0e29a1d9202dc816efe2d4cef81ed7f9ee71a166f3c5ed6bfe2b69dc97",  # pragma: allowlist secret
        "ui/living_grid.py": "e95fb454ffbc17e2d248d48f9b5e7563ecd7383a063ad94e0b3ef9088dab4374",  # pragma: allowlist secret
        "ui/source_chip.py": "374338d4d1132239c3de1c91fb84f1214e87b57472f6c5df2c0582708792f141",  # pragma: allowlist secret
        "viewspec/render.py": "743c2211158fef8be8fc86530004b6dbed51c6f9c862743bf42c8b7d677fff11",  # pragma: allowlist secret
    }
)

MASTER_GEOMETRY_CONTRACTS = tuple(
    MasterGeometryContract(
        surface,
        digest,
        _owner_for_surface(surface),
        "Normalized master geometry is pinned; recipe changes require a registry update.",
    )
    for surface, digest in sorted(_MASTER_GEOMETRY_DIGESTS.items())
)

_DYNAMIC_VISUAL_DIGESTS: Mapping[str, str] = MappingProxyType(
    {
        "advisor/sizing_intent_review_page.py": "441b8fb18ddaba95b98c58a4977006718b24ace5d19d11d9be742f14d32883f1",  # pragma: allowlist secret
        "dashboard/_styles.py": "124c663aa46bb419635e76850e7d0dc03f4749b008e6a5bfa1a972e525e68dba",  # pragma: allowlist secret
        "dashboard/feed.py": "11a36cf4d3d850906aa5bbec059ba7f7b9d132cb1398269ed4c0cf8f9fa5820a",  # pragma: allowlist secret
        "execution/build_earnings_calendar.py": "6177205661002d8572d7b790e97f4e3bbf6b43d8d07589d37b774be632b9200b",  # pragma: allowlist secret
        "execution/comments_server_alert_routes.py": "d8d2e88171b61d42b5b2fb3a8317869ac6c575409b3e151cbbe41f62d5267a33",  # pragma: allowlist secret
        "pipeline/advisor_memos_panel.py": "febd8519250f55eb0f11a0797315ae6d02fcf13ba5c6edd91c28e6d3ba1b89a1",  # pragma: allowlist secret
        "pipeline/allocation_decisions_panel.py": "d6d9f74f8d6282f475b9250a1949ead4bb7a5a9966ad1fd3bf3772f1f562fc4b",  # pragma: allowlist secret
        "pipeline/analytical_dashboard_html.py": "75ba11f347cd89fcd99a541ef0d89bdc544313e17e0ae363a53235e2484635f8",  # pragma: allowlist secret
        "pipeline/calibration_scorecard_panel.py": "1edfbfb1291c38be645133eaab45f78343920da72bf42b3e96c1a36e60a91eac",  # pragma: allowlist secret
        "pipeline/cron_health_panel.py": "084eb62653f0ea3583f0c8347e7b12626f8235b0498e3c4c3141b1723eec490c",  # pragma: allowlist secret
        "pipeline/explore_panel.py": "fc1dd2e109d4f31a63e523c6f6ba3bc016b10b1faa443d32240e35fabed2fa8c",  # pragma: allowlist secret
        "pipeline/mobile_inbox_panel.py": "a79bb9271eb683af81c99be486b8bcc3487f601002b74437d39fb50b5dd631a3",  # pragma: allowlist secret
        "pipeline/model_eval_panel.py": "f4af1f25d2641ba46a64a043073730df7b11ef1e96f3b3ad4d71801e17c3e983",  # pragma: allowlist secret
        "pipeline/peeks.py": "14c26a54b7381a925ecb47a4975282e17b68f6115dde3d3314f8bb25fa855bb9",  # pragma: allowlist secret
        "pipeline/performance_risk_panel.py": "f4399458107940b948664fe2e664892e2390d0f100cc1ad6f300e5233e2dd831",  # pragma: allowlist secret
        "pipeline/portfolio_panel.py": "71d2e9b39601d9e97dd395b70a405c15f6575c71d32f512fc7c04b4d128a6060",  # pragma: allowlist secret
        "pipeline/portfolio_styles.py": "397f25bbb814a248a8c887faf5ac75284c69d2e22336f7cb04c7b7d020f72ab6",  # pragma: allowlist secret
        "pipeline/research_cockpit.py": "397f25bbb814a248a8c887faf5ac75284c69d2e22336f7cb04c7b7d020f72ab6",  # pragma: allowlist secret
        "pipeline/source_viewers.py": "2fa6149b5c3e81709c2fb6e3199b236c32b5ea951ef5a328ee414c6c80bcdab5",  # pragma: allowlist secret
        "pipeline/work_os_copilot.py": "46a2dc3469b3e57e8365049f250e60da96ba9ed8780043be11e8c4044eb1bf1d",  # pragma: allowlist secret
        "pipeline/provenance_panel.py": "084eb62653f0ea3583f0c8347e7b12626f8235b0498e3c4c3141b1723eec490c",  # pragma: allowlist secret
        "pipeline/work_os_shell.py": "4ccb91be5f49cac92d331d3b4ff19c08051ebd327452eaad5042b72166887408",  # pragma: allowlist secret
        "pipeline/work_os_styles.py": "dc8c2615add4455efea1095cb501f00b0fcbdcc29e0171a8abda4ea234c6a14a",  # pragma: allowlist secret
        "report/renderers/charts_v2.py": "65f82d255c249213e51e3572a04594925ec497b16daae4705cceac4d02f8f53e",  # pragma: allowlist secret
        "report/renderers/workspace_html.py": "88f9c90d589e7c12f98fb7ce97f7af0e6f2b985b59839f4c737957066e9c07f5",  # pragma: allowlist secret
        "report/renderers/workspace_sections/company.py": "a2a7e88bb845c9eafa39e7673ac009ee3220e068430e91dc0160c3ef62b53551",  # pragma: allowlist secret
        "report/renderers/workspace_sections/thesis_risk.py": "ea6efda97e1eba69cdeb02e03744784f89ff7e606dcb86ccddceb1d4d12c0fea",  # pragma: allowlist secret
        "report/renderers/workspace_styles.py": "c166389b6f9260cf74379856f43c0e903ba0c8a2f72837c5802a4f836189a8f0",  # pragma: allowlist secret
        "ui/cite_marks.py": "3f76335ae581654995341743785185a7c317ac131a9c2e95182775ba1c4a6a7d",  # pragma: allowlist secret
        "ui/controls.py": "e4480459ae842f18982fc17967c6c6673a87588887097786508ce94f17e2483d",  # pragma: allowlist secret
        "viewspec/render.py": "7c533b52604f1cd0ad7564644dbeee92631041a047434299cc88fcc32fbf7e74",  # pragma: allowlist secret
    }
)

DYNAMIC_VISUAL_CONTRACTS = tuple(
    DynamicVisualContract(
        surface,
        digest,
        _owner_for_surface(surface),
        "Data-driven visual values are pinned as a closed source recipe.",
    )
    for surface, digest in sorted(_DYNAMIC_VISUAL_DIGESTS.items())
)

LOCAL_PROPERTY_CONTRACTS = (
    LocalPropertyContract(
        "--gap",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "work-os",
        "Legacy shell spacing alias retained only while its callers are migrated.",
    ),
    LocalPropertyContract(
        "--gap-lg",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "work-os",
        "Legacy large shell spacing alias retained only while its callers are migrated.",
    ),
    LocalPropertyContract(
        "--kpi-pad",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "research-ui",
        "Legacy KPI interior spacing alias retained during migration.",
    ),
    LocalPropertyContract(
        "--pad-x",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "work-os",
        "Legacy horizontal shell padding alias retained during migration.",
    ),
    LocalPropertyContract(
        "--panel-pad-x",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "research-ui",
        "Legacy panel horizontal padding alias retained during migration.",
    ),
    LocalPropertyContract(
        "--panel-pad-y",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "research-ui",
        "Legacy panel vertical padding alias retained during migration.",
    ),
    LocalPropertyContract(
        "--row-pad-y",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "research-ui",
        "Legacy table-row vertical padding alias retained during migration.",
    ),
    LocalPropertyContract(
        "--section-gap",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "research-ui",
        "Legacy workspace-section spacing alias retained during migration.",
    ),
    LocalPropertyContract(
        "--sidebar-open-width",
        frozenset(
            {
                "report/renderers/workspace_chat.py",
                "report/renderers/workspace_comments.py",
                "report/renderers/workspace_sections/boot.py",
                "report/renderers/workspace_styles.py",
            }
        ),
        "CSS <length>",
        "research-ui",
        "Chat drawer's open-width runtime contract.",
    ),
    LocalPropertyContract(
        "--table-pad-y",
        frozenset({"report/renderers/workspace_styles.py"}),
        "CSS <length>",
        "research-ui",
        "Legacy table vertical padding alias retained during migration.",
    ),
)

RUNTIME_VISUAL_CONTRACTS = (
    RuntimeVisualContract(
        "pipeline/allocation_recommendation_panel.py",
        "outerHTML",
        r"html",
        "portfolio",
        "A governed server fragment replaces the allocation recommendation section.",
    ),
    RuntimeVisualContract(
        "pipeline/decision_journal_panel.py",
        "outerHTML",
        r"'[<]span class=\"k-chip\">process:'\+quality\+'[<]/span>'",
        "research-ui",
        "The journal replaces its process state with the canonical chip primitive.",
    ),
    RuntimeVisualContract(
        "pipeline/ir_approval_panel.py",
        "outerHTML",
        r"result\.payload\.panel_html",
        "research-ui",
        "A governed server fragment refreshes the IR approval panel.",
    ),
    RuntimeVisualContract(
        "pipeline/ledger_panel.py",
        "outerHTML",
        r"h",
        "research-ui",
        "Governed ledger endpoint fragments replace their matching panel roots.",
    ),
    RuntimeVisualContract(
        "pipeline/position_lifecycle_panel.py",
        "outerHTML",
        r"html",
        "portfolio",
        "A governed server fragment refreshes the position lifecycle section.",
    ),
    RuntimeVisualContract(
        "pipeline/positioning_panel.py",
        "outerHTML",
        r"res\.text",
        "portfolio",
        "A governed response fragment refreshes the active position card.",
    ),
    RuntimeVisualContract(
        "pipeline/worldview_panel.py",
        "outerHTML",
        r"h",
        "research-ui",
        "A governed server fragment refreshes the worldview panel.",
    ),
    RuntimeVisualContract(
        "pipeline/cc_action.py",
        "height",
        r"el\.offsetHeight\+['\"]px['\"]",
        "work-os",
        "Measured collapse height preserves the zero-layout-pop dismissal contract.",
    ),
    RuntimeVisualContract(
        "pipeline/cc_action.py",
        "overflow",
        r"['\"]hidden['\"]",
        "work-os",
        "Collapse overflow is hidden only for the measured dismissal transition.",
    ),
    RuntimeVisualContract(
        "pipeline/cc_overlay.py",
        "zIndex",
        r"String\(zOf\(s\.el\)-1\)",
        "design-system",
        "The overlay master computes scrim order from the registered layer stack.",
    ),
    RuntimeVisualContract(
        "report/renderers/workspace_comments.py",
        "left",
        r"Math\.round\(rect\.left\+window\.scrollX\+rect\.width\s*/\s*2-56\)\+['\"]px['\"]",
        "research-ui",
        "The selection comment affordance is positioned from the live text range.",
    ),
    RuntimeVisualContract(
        "report/renderers/workspace_comments.py",
        "top",
        r"Math\.round\(rect\.bottom\+window\.scrollY\+6\)\+['\"]px['\"]",
        "research-ui",
        "The selection comment affordance is positioned from the live text range.",
    ),
)

EXEMPT = frozenset(entry.surface for entry in PERMANENT_EXEMPTIONS)
QUARANTINE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        surface: frozenset(
            entry.dimension for entry in QUARANTINE_ENTRIES if entry.surface == surface
        )
        for surface in {entry.surface for entry in QUARANTINE_ENTRIES}
    }
)
BESPOKE_BUTTON_OK = frozenset(entry.class_name for entry in BESPOKE_BUTTON_APPROVALS)
MONO_TABLE_ALLOWLIST = frozenset(entry.selector for entry in MONO_TABLE_APPROVALS)
FONT_SIZE_EXEMPT: frozenset[str] = frozenset(
    entry.surface for entry in SURFACE_SANCTIONS if entry.dimension == "font-size"
)
RADIUS_SANCTIONED: Mapping[str, frozenset[str]] = MappingProxyType(
    {entry.surface: entry.values for entry in SURFACE_SANCTIONS if entry.dimension == "radius"}
)
CCACTION_PINNED = frozenset(entry.surface for entry in CCACTION_REGRESSION_FLOOR)

SHAPES_BY_SELECTOR = MappingProxyType(
    {
        signature.selector: signature
        for archetype in SHAPE_ARCHETYPES
        for signature in archetype.signatures
    }
)
GRIDS_BY_SELECTOR = MappingProxyType(
    {
        signature.selector: signature
        for archetype in GRID_ARCHETYPES
        for signature in archetype.signatures
    }
)
TITLES_BY_SELECTOR = MappingProxyType(
    {
        placement.selector: placement
        for placement in TITLE_PLACEMENTS
        if placement.selector is not None
    }
)
