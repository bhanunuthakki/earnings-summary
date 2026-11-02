"""Versioned quality bars for replay audits of captured production LLM output.

These specs close the legacy eval gap without putting evaluation traffic on the
live request path.  Each purpose is assigned:

* a product-risk priority;
* a traffic/performance tier that controls the default bounded sample;
* a task family with deterministic rubric facets;
* a purpose-specific objective and critical failure.

The capture audit reads the private opt-in capture archive. A purpose with no
real production captures fails loudly when invoked; an empty corpus is never
treated as a passing quality signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Priority = Literal["P0", "P1", "P2"]
TrafficTier = Literal["hot", "warm", "cold"]
TaskFamily = Literal[
    "classification",
    "coaching",
    "extraction",
    "judgment",
    "research",
    "synthesis",
    "visualization",
]


@dataclass(frozen=True, slots=True)
class CaptureQualitySpec:
    purpose: str
    priority: Priority
    traffic_tier: TrafficTier
    family: TaskFamily
    objective: str
    critical_failure: str

    @property
    def pass_threshold(self) -> float:
        return {"P0": 0.82, "P1": 0.78, "P2": 0.74}[self.priority]

    @property
    def default_limit(self) -> int:
        return {"hot": 20, "warm": 10, "cold": 5}[self.traffic_tier]


def _q(
    purpose: str,
    priority: Priority,
    traffic: TrafficTier,
    family: TaskFamily,
    objective: str,
    critical_failure: str,
) -> CaptureQualitySpec:
    return CaptureQualitySpec(
        purpose=purpose,
        priority=priority,
        traffic_tier=traffic,
        family=family,
        objective=objective,
        critical_failure=critical_failure,
    )


_SPECS: tuple[CaptureQualitySpec, ...] = (
    # P0: consequential decisions, portfolio controls, or the highest-volume
    # mutation/extraction paths. A bad answer can directly corrupt durable
    # state or materially distort an investment decision.
    _q(
        "ask_answer",
        "P0",
        "warm",
        "judgment",
        "Answer the analyst's actual question from supplied evidence with balanced risk/reward.",
        "Invented facts, hidden uncertainty, or a recommendation unsupported by the evidence.",
    ),
    _q(
        "dcf_assumptions",
        "P0",
        "cold",
        "judgment",
        "Propose internally consistent, evidence-grounded DCF assumptions with explicit uncertainty.",
        "False precision, inconsistent units, or assumptions detached from company evidence.",
    ),
    _q(
        "exec_comp_alignment",
        "P0",
        "hot",
        "judgment",
        "Assess whether executive incentives align with durable shareholder value using disclosed facts.",
        "A strong alignment verdict that ignores pay design, dilution, or contrary disclosed evidence.",
    ),
    _q(
        "exec_comp_extraction",
        "P0",
        "cold",
        "extraction",
        "Extract compensation metrics, targets, periods, and units exactly from disclosures.",
        "Wrong executive, period, unit, or target attributed as a disclosed fact.",
    ),
    _q(
        "extract_8k_overrides",
        "P0",
        "cold",
        "extraction",
        "Extract only explicit 8-K overrides to modeled financial or guidance inputs.",
        "Treating commentary as a numeric override or applying an override to the wrong period.",
    ),
    _q(
        "material_news_classification",
        "P0",
        "hot",
        "classification",
        "Separate decision-relevant company news from noise using portfolio materiality.",
        "Suppressing genuinely material news or escalating immaterial/adversarial content.",
    ),
    _q(
        "qualitative_conditions_extract",
        "P0",
        "hot",
        "extraction",
        "Turn decision text into explicit, testable qualitative conditions without changing intent.",
        "Inventing a condition, dropping a critical qualifier, or reversing the owner's decision rule.",
    ),
    _q(
        "red_team_attack",
        "P0",
        "cold",
        "judgment",
        "Produce the strongest evidence-grounded attack on the active investment thesis.",
        "A superficial strawman or fabricated disconfirming evidence.",
    ),
    _q(
        "red_team_cross_book",
        "P0",
        "cold",
        "judgment",
        "Identify correlated portfolio failure modes and cross-position thesis collisions.",
        "Missing a shared risk because positions were assessed independently.",
    ),
    _q(
        "research_adversarial_assess",
        "P0",
        "cold",
        "judgment",
        "Adversarially assess whether research evidence supports, weakens, or fails to resolve a claim.",
        "Converting weak or conflicting evidence into unjustified certainty.",
    ),
    _q(
        "saydo_commitment_extract",
        "P0",
        "hot",
        "extraction",
        "Extract explicit, attributable management commitments with metric, target, and time horizon.",
        "Recording aspiration as commitment or assigning the wrong speaker, metric, or deadline.",
    ),
    _q(
        "strategic_analysis",
        "P0",
        "cold",
        "judgment",
        "Connect strategy, economics, competitive response, and capital allocation into a falsifiable view.",
        "A confident strategic conclusion with no causal chain or disconfirming case.",
    ),
    _q(
        "thesis_collision",
        "P0",
        "cold",
        "judgment",
        "Identify direct contradictions between current evidence, decisions, and the active thesis.",
        "Failing to surface a contradiction that should change the investment decision.",
    ),
    _q(
        "thesis_pass_a",
        "P0",
        "cold",
        "judgment",
        "Construct the strongest evidence-grounded affirmative investment thesis.",
        "Unsupported claims or omission of a material known risk.",
    ),
    _q(
        "thesis_pass_b",
        "P0",
        "cold",
        "judgment",
        "Independently challenge and refine the first-pass thesis using contrary evidence.",
        "Rubber-stamping pass A instead of testing its weakest causal links.",
    ),
    _q(
        "valuation_basis",
        "P0",
        "hot",
        "judgment",
        "Select and justify the valuation basis that best matches the company's economic model.",
        "Using an economically inappropriate multiple or silently mixing incompatible periods.",
    ),
    _q(
        "advisor_swap_check",
        "P0",
        "cold",
        "judgment",
        "Compare a proposed capital swap using opportunity cost, taxes, risk, and thesis strength.",
        "Ignoring switching costs or comparing positions on inconsistent assumptions.",
    ),
    _q(
        "pressure_test_thesis",
        "P0",
        "cold",
        "judgment",
        "Pressure-test the thesis against explicit falsifiers and alternative explanations.",
        "A cosmetic challenge that cannot change the decision.",
    ),
    # P1: report-critical extraction, classification, and synthesis. Failures
    # degrade research quality but are less likely to mutate consequential
    # state directly.
    _q(
        "business_factor_taxonomy",
        "P1",
        "cold",
        "classification",
        "Classify company drivers into stable, non-overlapping business-factor categories.",
        "Category drift that makes periods or companies incomparable.",
    ),
    _q(
        "canonicalize_segments",
        "P1",
        "hot",
        "extraction",
        "Map disclosed segment names to stable canonical identities without merging distinct economics.",
        "Combining distinct segments or splitting one segment across aliases.",
    ),
    _q(
        "company_description",
        "P1",
        "hot",
        "synthesis",
        "Describe business economics and moat mechanisms with public evidence, counter-evidence and alternative causes.",
        "Invented precision/competitors, generic claims, or private-data checks offered as public evidence.",
    ),
    _q(
        "customer_concentration_extraction",
        "P1",
        "cold",
        "extraction",
        "Extract customer concentration, periods, thresholds, and disclosed customer identity accurately.",
        "Wrong period/percentage or inferred customer identity presented as disclosed.",
    ),
    _q(
        "diet_source_quality",
        "P1",
        "warm",
        "classification",
        "Grade source reliability and relevance using provenance rather than narrative appeal.",
        "Treating low-quality or circular sourcing as primary evidence.",
    ),
    _q(
        "disclosure_thesis_materiality",
        "P1",
        "cold",
        "judgment",
        "Elevate a disclosure change only when it restricts measuring the thesis' KPIs or "
        "break rules, naming the affected input; default to not_material.",
        "Elevating drift with no measurement impact, or suppressing a change that removes "
        "a tier-1 KPI/break-rule input from observability.",
    ),
    _q(
        "earnings_tone_diff",
        "P1",
        "warm",
        "judgment",
        "Compare cited same-speaker/topic/context language with coverage and personnel limits; separate textual observation from inference.",
        "Unsupported avoidance/dropped-topic/motive inference, unmatched comparisons, or calibrated sentiment claimed without qualification.",
    ),
    _q(
        "footnote_extraction",
        "P1",
        "cold",
        "extraction",
        "Extract material footnote facts with period, unit, and source context intact.",
        "Unit/period errors or loss of a qualifier that changes meaning.",
    ),
    _q(
        "guidance_lifecycle_triage",
        "P1",
        "cold",
        "classification",
        "Classify guidance as introduced, reiterated, raised, lowered, withdrawn, or fulfilled.",
        "Wrong lifecycle state that reverses the apparent direction of management guidance.",
    ),
    _q(
        "investor_deck_extraction",
        "P1",
        "cold",
        "extraction",
        "Extract decision-relevant claims and metrics from investor materials with provenance.",
        "Deck aspiration or adjusted metric presented as audited fact.",
    ),
    _q(
        "ir_sheet_kpi_map",
        "P1",
        "cold",
        "extraction",
        "Map IR spreadsheet rows to canonical KPIs with correct units and periods.",
        "Mapping a row to the wrong KPI or silently changing scale.",
    ),
    _q(
        "kpi_inflection_context",
        "P1",
        "cold",
        "synthesis",
        "Explain a KPI inflection using grounded company and period context.",
        "Post-hoc causal claims unsupported by the supplied evidence.",
    ),
    _q(
        "kpi_registry_auto_proposal",
        "P1",
        "warm",
        "extraction",
        "Propose canonical KPI definitions, aliases, units, and provenance for review.",
        "A proposal that conflates metrics or overwrites an existing definition.",
    ),
    _q(
        "kpi_registry_proposal",
        "P1",
        "warm",
        "extraction",
        "Propose reviewable canonical KPI definitions, aliases, units, and provenance.",
        "A proposal that conflates metrics, invents provenance, or overwrites a definition.",
    ),
    _q(
        "kpi_summary_enumerate",
        "P1",
        "cold",
        "extraction",
        "Enumerate every material KPI disclosed in the source without duplication.",
        "Missing a headline KPI or inventing one not present in the source.",
    ),
    _q(
        "market_signals",
        "P1",
        "cold",
        "synthesis",
        "Summarize market signals while separating observation, interpretation, and uncertainty.",
        "Presenting noisy price action or consensus as causal fact.",
    ),
    _q(
        "recent_developments",
        "P1",
        "hot",
        "research",
        "Identify recent decision-relevant developments from credible, current sources.",
        "Fabricated/stale events, weak provenance, or omission of the most material development.",
    ),
    _q(
        "research_fetch",
        "P1",
        "cold",
        "research",
        "Retrieve sources that directly address the research question with clear provenance.",
        "Irrelevant, stale, circular, or untraceable sourcing.",
    ),
    _q(
        "research_narrate",
        "P1",
        "cold",
        "synthesis",
        "Synthesize research evidence into claims, uncertainty, and remaining gaps.",
        "A narrative that outruns its sources or hides unresolved contradictions.",
    ),
    _q(
        "risk_factor_classify",
        "P1",
        "cold",
        "classification",
        "Classify risk-factor changes by economic mechanism and materiality.",
        "Boilerplate labeled material or a substantive new risk labeled unchanged.",
    ),
    _q(
        "risk_factor_diff",
        "P1",
        "cold",
        "synthesis",
        "Explain substantive risk-factor changes with exact before/after grounding.",
        "Invented differences or loss of a qualifier that changes the risk.",
    ),
    _q(
        "segment_6k_breakdown_extract",
        "P1",
        "cold",
        "extraction",
        "Extract 6-K segment breakdowns with correct hierarchy, period, currency, and scale.",
        "Segment totals that do not reconcile or values assigned to the wrong period.",
    ),
    _q(
        "segment_crosstab_extract",
        "P1",
        "cold",
        "extraction",
        "Extract segment crosstabs without mixing dimensions, periods, or units.",
        "Combining geography and product dimensions or misreading cumulative columns.",
    ),
    _q(
        "segment_definition_extract",
        "P1",
        "cold",
        "extraction",
        "Extract disclosed segment definitions and boundary changes verbatim enough to compare periods.",
        "Missing a segment-boundary change or inferring an undisclosed definition.",
    ),
    _q(
        "transcript_qa_judgment",
        "P1",
        "hot",
        "judgment",
        "Assess whether management directly answered the analyst's question and what remains unresolved.",
        "Rewarding evasion as a complete answer or missing a material concession.",
    ),
    _q(
        "transcript_topic_triage",
        "P1",
        "warm",
        "classification",
        "Route transcript passages to material business topics with stable labels.",
        "Topic drift that hides a material discussion or duplicates it across lanes.",
    ),
    # P2: low-frequency communication, coaching, and presentation surfaces.
    # Quality matters, but failures are reversible and rarely mutate data.
    _q(
        "event_brief",
        "P2",
        "cold",
        "synthesis",
        "Produce a concise event brief with facts, implications, uncertainty, and follow-up.",
        "Mixing fact and interpretation or omitting the decision relevance.",
    ),
    _q(
        "patent_timeline",
        "P2",
        "cold",
        "research",
        "Build a dated, sourced patent timeline tied to economically relevant developments.",
        "Wrong dates, duplicate families, or unsupported economic conclusions.",
    ),
    _q(
        "platform_diagram",
        "P2",
        "cold",
        "visualization",
        "Represent platform components and dependencies accurately and legibly.",
        "A diagram that implies nonexistent flows or omits a critical dependency.",
    ),
    _q(
        "pre_earnings_brief",
        "P2",
        "cold",
        "synthesis",
        "Preserve dated guidance, consensus and owner bars separately; link public observables, accepted rules and material questions.",
        "Invented thresholds/consensus, missing dated baseline or private-data homework, or ignoring accepted rules.",
    ),
    _q(
        "post_earnings_readout",
        "P2",
        "cold",
        "synthesis",
        "Compare the same verified pre-call bar with actuals; distinguish later context and assess complete material exchanges with source limits.",
        "Retrospective invented expectations, unsupported non-answer/tone judgments, blurred current versus call evidence, or no public next check.",
    ),
    _q(
        "research_code_spec",
        "P2",
        "cold",
        "synthesis",
        "Translate a research need into a bounded, testable implementation specification.",
        "A spec that embeds unstated business logic or lacks acceptance criteria.",
    ),
    _q(
        "saydo_due_context",
        "P2",
        "cold",
        "synthesis",
        "Explain why a tracked commitment is due using the original horizon and current evidence.",
        "Inventing a deadline or claiming fulfillment without matching evidence.",
    ),
    _q(
        "thesis_entry_draft",
        "P2",
        "cold",
        "synthesis",
        "Draft a thesis entry with claim, evidence, uncertainty, falsifier, and provenance.",
        "A claim without falsifier or evidence presented as certainty.",
    ),
    # Dynamic lens calls share one generator and quality contract.
    _q(
        "lens:*",
        "P2",
        "warm",
        "synthesis",
        "Apply the named lens with evidence/counter-evidence and explicit missing prior view, model and portfolio context.",
        "Invented evidence, prior baseline, numeric valuation/size or approved triggers; absent inputs treated as zero.",
    ),
)

CAPTURE_QUALITY_SPECS: dict[str, CaptureQualitySpec] = {spec.purpose: spec for spec in _SPECS}
if len(CAPTURE_QUALITY_SPECS) != len(_SPECS):
    raise RuntimeError("duplicate capture-quality purpose")

CAPTURE_QUALITY_PURPOSES: tuple[str, ...] = tuple(
    spec.purpose
    for spec in sorted(
        _SPECS,
        key=lambda item: (
            {"P0": 0, "P1": 1, "P2": 2}[item.priority],
            {"hot": 0, "warm": 1, "cold": 2}[item.traffic_tier],
            item.purpose,
        ),
    )
)

P0_CAPTURE_QUALITY_PURPOSES: tuple[str, ...] = tuple(
    purpose
    for purpose in CAPTURE_QUALITY_PURPOSES
    if CAPTURE_QUALITY_SPECS[purpose].priority == "P0"
)
