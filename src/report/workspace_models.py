"""Prepared workspace values shared by report builders and pure renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from llm.calibration import VersionSummary
from report.sections.comp_set_context import CompSetContextSection
from user_state.notes import AnalystNoteRow


@dataclass(frozen=True)
class MacroSensitivityRow:
    """One row of macro_sensitivities for a ticker."""

    series_id: str
    beta: float
    r_squared: float | None
    lookback_window_days: int
    computed_at: datetime
    metric_version: str = "legacy_unversioned"
    shock_unit: str = "log_return"
    input_sha: str | None = None
    source_as_of: str | None = None


@dataclass(frozen=True)
class StrategicTargetRow:
    """One row of strategic_targets — long-term mgmt commitment from an investor deck."""

    target_kind: str
    target_value: float | None
    target_unit: str
    target_period: str
    target_currency: str | None
    narrative_excerpt: str
    confidence: float


@dataclass(frozen=True)
class CustomerConcentrationRow:
    """One named customer's share of a ticker's revenue for a period."""

    fiscal_period: str
    fiscal_period_type: str
    customer_label: str
    pct_of_revenue: float
    revenue_amount: float | None
    revenue_currency: str | None


@dataclass(frozen=True)
class LeaseLadderRow:
    """One year of a lease maturity ladder."""

    fiscal_year: int
    as_of_date: date
    lease_type: str
    ladder_year: str
    ladder_calendar_year: int | None
    amount: float
    currency: str
    unit: str


@dataclass(frozen=True)
class DecisionRow:
    """One decision_audit row."""

    recommendation_kind: str
    recommendation_value: float | None
    conviction: str | None
    made_at: datetime
    outcome_pct: float | None
    outcome_at: datetime | None
    rationale_excerpt: str | None


@dataclass(frozen=True)
class DecisionHistorySummary:
    """Aggregated view: counts by (kind, conviction) + win-rate."""

    total: int
    by_kind: dict[str, int]
    by_conviction: dict[str, int]
    win_rate_overall: float | None
    rows: list[DecisionRow]


@dataclass(frozen=True)
class PeerCompRow:
    """One peer's TTM metrics for the evaluation-snapshot peer-comp table.

    ``match_reasons`` carries why this peer was selected (P4.2 scored
    selection) so the renderer can show the basis instead of presenting an
    unexplained list — e.g. ``("named rival", "same industry", "similar
    scale")``.
    """

    peer_ticker: str
    peer_name: str | None
    market_cap_usd: float | None
    revenue_ttm_usd: float | None
    net_margin_ttm: float | None
    roic_ttm: float | None
    match_reasons: tuple[str, ...] = ()
    coverage_notes: tuple[str, ...] = ()
    source_evidence: dict[str, object] | None = None


_DATABASE_PANELS = (
    "macro_sensitivities",
    "strategic_targets",
    "customer_concentrations",
    "lease_ladder",
    "decision_history",
    "open_notes",
    "position_review_count",
    "standing_rules",
    "comp_set_context",
    "peer_comp",
)


class PanelAvailability(StrEnum):
    PRESENT = "present"
    EMPTY = "empty"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"


def _new_availability() -> dict[str, PanelAvailability]:
    return {}


def _new_open_notes() -> list[AnalystNoteRow]:
    return []


@dataclass(frozen=True)
class WorkspaceP3Panels:
    """All P3 accessor rows for one (ticker, repo) render, pre-loaded.

    Availability records distinguish successful empty reads from unconfigured
    or unavailable sources. Data fields retain the renderer's empty shapes;
    review counts stay None unless their source was successfully read.
    """

    macro_sensitivities: list[MacroSensitivityRow]
    strategic_targets: list[StrategicTargetRow]
    customer_concentrations: list[CustomerConcentrationRow]
    lease_ladder: list[LeaseLadderRow]
    decision_history: DecisionHistorySummary
    peer_comp: list[PeerCompRow]
    # P4.4: the owner's open analyst notes for this name — new builds lead
    # with the standing watch-items (the strip under the thesis lede).
    open_notes: list[AnalystNoteRow] = field(default_factory=_new_open_notes)
    # Position-tab coaching line (REQ-3/REQ-6): count of advisor_memos rows
    # with kind='position_review' for this ticker — "Guard: never run on this
    # name · N position reviews". None means the history was not available;
    # zero is reserved for a successful count with no matching rows.
    position_review_count: int | None = None
    # PR10 (Monthly Red Team surface wiring): the ticker's standing sizing
    # rules (position_sizing_intent history + guard coverage read) for the
    # Position tab's Standing-rules block. None = no rows on file / DB absent
    # (the block hides, hide-don't-stub). StandingRulesPanel is defined later
    # in this module — fine under `from __future__ import annotations`.
    standing_rules: StandingRulesPanel | None = None
    # Phase 3 (comparable_sets_bottoms_up.md §11): the Company tab's "Sector
    # context" card — subject vs comp-set median/aggregate vs pool-wide
    # industry/sector benchmark, reading comp_set_metrics_daily. None = no
    # frozen comparable set for this ticker yet (hide-don't-stub — most of
    # the pool, on a phased rollout).
    comp_set_context: CompSetContextSection | None = None

    availability: dict[str, PanelAvailability] = field(default_factory=_new_availability)

    @classmethod
    def empty(
        cls, status: PanelAvailability = PanelAvailability.NOT_CONFIGURED
    ) -> WorkspaceP3Panels:
        return cls(
            macro_sensitivities=[],
            strategic_targets=[],
            customer_concentrations=[],
            lease_ladder=[],
            decision_history=DecisionHistorySummary(
                total=0,
                by_kind={},
                by_conviction={},
                win_rate_overall=None,
                rows=[],
            ),
            peer_comp=[],
            open_notes=[],
            position_review_count=None,
            availability=dict.fromkeys(_DATABASE_PANELS, status),
        )


@dataclass(frozen=True)
class StandingRuleRow:
    """One ``position_sizing_intent`` row, render-ready."""

    intent_kind: str
    intent_value: float | None
    narrative: str
    is_draft: bool
    updated_at: datetime


@dataclass(frozen=True)
class StandingRulesPanel:
    """The Position tab's Standing-rules block: every sizing intent on file
    for one ticker, newest first, plus the guard's coverage read.

    ``downside_passed`` / ``add_passed`` are ``None`` when the guard module
    was unavailable (guarded import), never a guess. ``add_is_draft`` marks an
    add-rung whose only evidence rows are machine-proposed drafts."""

    rows: list[StandingRuleRow]
    downside_passed: bool | None
    add_passed: bool | None
    add_is_draft: bool = False


@dataclass(frozen=True)
class WorkspacePrepared:
    """Analytical values prepared once by the report build owner."""

    p3: WorkspaceP3Panels = field(default_factory=WorkspaceP3Panels.empty)
    graded_sell_line: str | None = None
    you_said_html: str = ""
    earnings_audio_url: str | None = None
    prompt_summaries: list[VersionSummary] = field(default_factory=lambda: list[VersionSummary]())
    prompt_daily: dict[tuple[str, str], list[tuple[str, float]]] = field(
        default_factory=lambda: dict[tuple[str, str], list[tuple[str, float]]]()
    )


@dataclass(frozen=True)
class WorkspaceDelivery:
    """Fresh comment/capability state supplied by delivery composition."""

    boot: dict[str, object]
    comments: dict[str, object]
