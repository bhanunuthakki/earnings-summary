"""Immutable, locally owned design-language vocabulary and approvals.

This module intentionally contains declarations and derived projections only.
It does not scan files, render CSS, or make policy decisions at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
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

REGISTRY_VERSION = "1.0.0"

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
    "CCACTION_PINNED",
    "CCACTION_REGRESSION_FLOOR",
    "CHROME_TOKENS",
    "DOCUMENTATION_PROJECTIONS",
    "EXEMPT",
    "FONT_FAMILY_KEYWORDS",
    "FONT_SIZE_EXEMPT",
    "GRIDS_BY_SELECTOR",
    "GRID_ARCHETYPES",
    "INDENT_TOKENS",
    "INDENT_TOKEN_NAMES",
    "INDENT_TOKEN_VALUES",
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
    "SHAPES_BY_SELECTOR",
    "SHAPE_ARCHETYPES",
    "SURFACE_SANCTIONS",
    "TITLES_BY_SELECTOR",
    "TITLE_PLACEMENTS",
    "TYPE_SCALE_PX",
    "BespokeButtonApproval",
    "CCActionRegressionFloor",
    "GridArchetype",
    "GridSignature",
    "MonoTableApproval",
    "PermanentExemption",
    "QuarantineEntry",
    "ShapeArchetype",
    "ShapeSignature",
    "SurfaceSanction",
    "TitlePlacement",
    "palette_css",
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


SHAPE_ARCHETYPES = (
    ShapeArchetype(
        "macro-container",
        (ShapeSignature(".k-card", "radius-card", "bw-thin solid border", "shadow-card"),),
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
        "report/renderers/charts_v2.py",
        "research-ui",
        "Owns chart geometry-specific SVG labels and fills.",
    ),
)

_QUARANTINE_EXPIRY = date(2026, 10, 1)
QUARANTINE_ENTRIES = (
    QuarantineEntry(
        "report/renderers/workspace_charts.py",
        "radius",
        "BHA-92",
        "Editorial chart marks need a sanctioned micro radius.",
        _QUARANTINE_EXPIRY,
    ),
    QuarantineEntry(
        "report/renderers/workspace_comments.py",
        "radius",
        "BHA-92",
        "Report-comment geometry remains in the serialized report migration.",
        _QUARANTINE_EXPIRY,
    ),
    QuarantineEntry(
        "report/renderers/workspace_styles.py",
        "radius",
        "BHA-92",
        "Editorial micro marks need a deliberate replacement token.",
        _QUARANTINE_EXPIRY,
    ),
    QuarantineEntry(
        "ui/cite_marks.py",
        "color",
        "BHA-92",
        "Minimal-host fallback color remains until report host wiring lands.",
        _QUARANTINE_EXPIRY,
    ),
    QuarantineEntry(
        "ui/cite_marks.py",
        "radius",
        "BHA-92",
        "Minimal-host fallback radius remains until report host wiring lands.",
        _QUARANTINE_EXPIRY,
    ),
)

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

REGISTERED = frozenset(
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
        "report/renderers/workspace_reader_assets.py",
        "report/renderers/workspace_styles.py",
        "ui/cite_marks.py",
        "ui/controls.py",
        "ui/living_grid.py",
        "ui/source_chip.py",
        "ui/tokens.py",
        "viewspec/render.py",
    }
)

EXEMPT = frozenset(entry.surface for entry in PERMANENT_EXEMPTIONS)
QUARANTINE = MappingProxyType(
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

DOCUMENTATION_PROJECTIONS = MappingProxyType(
    {
        "shapes": tuple(archetype.name for archetype in SHAPE_ARCHETYPES),
        "grids": tuple(archetype.name for archetype in GRID_ARCHETYPES),
        "indents": ("indent-0", "indent-1", "indent-2", "indent-3", "indent-4"),
        "titles": tuple(placement.key for placement in TITLE_PLACEMENTS),
        "exemptions": tuple(entry.surface for entry in PERMANENT_EXEMPTIONS),
        "quarantine": tuple(f"{entry.surface}:{entry.dimension}" for entry in QUARANTINE_ENTRIES),
        "bespoke-buttons": tuple(entry.class_name for entry in BESPOKE_BUTTON_APPROVALS),
        "mono-tables": tuple(entry.selector for entry in MONO_TABLE_APPROVALS),
        "sanctions": tuple(f"{entry.surface}:{entry.dimension}" for entry in SURFACE_SANCTIONS),
        "ccaction-floor": tuple(entry.surface for entry in CCACTION_REGRESSION_FLOOR),
    }
)
