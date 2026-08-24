"""Decision-condition trigger — resurfaces a decision when its falsifiable
"what would change my mind" condition is satisfied by incoming data.

The extraction side (src/decision_conditions.py) turns memo prose into
structured conditions on ``decisions.decision_conditions`` (alembic 0086).
This sensor is the evaluation side: every morning-driver run it walks the
ticker's OPEN decisions (``outcome_at IS NULL``), converts each resolvable
condition into a ``BreakRule`` and evaluates it through the break-rule
engine — ``fetch_kpi_observations`` (canonical-name resolver, per-period
dedup, cadence awareness) + ``evaluate_rule`` (shared ``convert_unit``
reconciliation, consecutive-periods semantics). A condition that reaches
BREACH becomes an alert: "the thing you said would change your mind just
happened — revisit the decision."

Deterministic end to end — no LLM call anywhere in the lifecycle. The memo
is templated from the condition + observations; the materiality judgment
was made by the analyst when they wrote the condition.

Evidence keying on ``(decision_id, condition_index, period_end)`` mirrors
kpi_inflection's ``(kpi_name, period_end)``: a given period's satisfaction
fires once; a later period still satisfying fires fresh (the condition is
*still* true — silence would read as recovery).

Conditions whose metric didn't resolve at extraction time
(``metric_source`` null) or whose metric_source is 'financial' but the line
item has no rows are skipped here — they render on the decisions panel as
unevaluable, they just can't fire.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from alerts.store import compute_signature_sha
from compute.thesis_evaluator import (
    BreakRule,
    Comparator,
    KpiObservation,
    evaluate_rule,
    fetch_kpi_observations,
)
from decision_conditions import (
    DecisionCondition,
    OpenDecision,
    fetch_financial_history,
    load_open_decisions,
)
from models.facts import Unit
from models.kpis import BreachStatus
from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)

log = logging.getLogger(__name__)

_OP_LABELS: dict[str, str] = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}

# Legacy freshness fallback (conditions stamped before the baseline watermark
# existed carry no ``baseline_period_end``): a breaching observation older than
# this many days is history, not news — it must not push-fire. 135 days ≈ one
# fiscal quarter plus reporting lag, so a genuinely fresh print always passes.
_LEGACY_FRESHNESS_DAYS = 135


def _is_portfolio_ticker(conn: sqlite3.Connection, ticker: str) -> bool:
    """True when the ticker's active tracked_companies row is portfolio-listed.

    Best-effort: a missing table (stripped-down test DB) reads as
    not-portfolio — the push lane stays quiet rather than guessing.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM tracked_companies "
            "WHERE ticker = ? AND list_type = 'portfolio' AND archived_at IS NULL",
            (ticker.upper(),),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _breach_is_fresh(cond: DecisionCondition, breach_period_end: datetime, now: datetime) -> bool:
    """The breach-freshness watermark gate (fix for stale-data 'news').

    With a baseline: the breaching observation must be STRICTLY AFTER the
    latest period already observed when the condition was attached — data
    that predates the decision can never resurface it. Legacy conditions
    (no baseline stamped) fall back to requiring the breaching observation
    to be a fresh print (within ``_LEGACY_FRESHNESS_DAYS`` of now).
    """
    breach_date = breach_period_end.date().isoformat()
    if cond.baseline_period_end:
        return breach_date > cond.baseline_period_end[:10]
    age_days = (now - breach_period_end).days
    return age_days <= _LEGACY_FRESHNESS_DAYS


def condition_to_rule(decision_id: int, index: int, cond: DecisionCondition) -> BreakRule | None:
    """A condition as a BreakRule, so evaluation IS the break-rule engine.
    None when the op/unit doesn't validate (corrupt stored JSON survives
    parse but not the Pydantic gate) — skipped, logged."""
    try:
        return BreakRule(
            rule_id=f"decision:{decision_id}:{index}",
            kpi_name=cond.metric,
            comparator=Comparator(cond.op),
            threshold=Decimal(str(cond.threshold)),
            unit=Unit(cond.unit),
            consecutive_periods=cond.for_periods,
            narrative=(cond.note or cond.metric)[:1000],
        )
    except (ValueError, InvalidOperation) as exc:
        log.warning(
            {
                "event": "decision_condition_rule_invalid",
                "decision_id": decision_id,
                "index": index,
                "error": str(exc),
            }
        )
        return None


class DecisionConditionTrigger:
    """Sensor over open decisions' falsifiable conditions (lifecycle docs in
    the module docstring; contract in triggers.base.Trigger)."""

    kind: ClassVar[str] = "decision_condition"
    cadence: ClassVar[Cadence] = Cadence.DAILY

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]:
        """Evaluate every resolvable condition on the ticker's open decisions;
        emit a candidate per condition whose status is BREACH — gated so the
        PUSH lane only carries breaches worth interrupting the owner for.

        Two gates (alert-quality fix, 2026-07-15; both live here, not in the
        generic driver — the panels that render armed falsifiers read
        ``load_open_decisions`` directly and stay ungated):

        - **Relevance**: the decision is owner-authored (their own falsifier —
          always), or the ticker is an active portfolio holding (an advisor
          condition guarding money at work). Advisor conditions on
          evaluation/watchlist names stay evaluable on panels but never page.
        - **Freshness** (:func:`_breach_is_fresh`): the breaching observation
          must postdate the condition's attach-time baseline (legacy
          conditions: must be a recent print) — a condition already breached
          when it was written is display state, never news.
        """
        # The driver opens this connection fresh per (ticker, trigger) scan and
        # closes it right after; the fetchers (fetch_kpi_observations and the
        # shared financial one) require Row access, so set it here.
        db.row_factory = sqlite3.Row
        ticker = ticker.upper()
        candidates: list[TriggerCandidate] = []
        now = datetime.now(UTC).replace(tzinfo=None)
        is_portfolio = _is_portfolio_ticker(db, ticker)

        for decision in load_open_decisions(db, ticker):
            if decision.decided_by != "owner" and not is_portfolio:
                log.info(
                    {
                        "event": "decision_condition_push_gated",
                        "ticker": ticker,
                        "decision_id": decision.decision_id,
                        "decided_by": decision.decided_by,
                        "reason": "advisor decision on a non-portfolio name",
                    }
                )
                continue
            for index, cond in enumerate(decision.conditions):
                if cond.not_before and cond.not_before[:10] > now.date().isoformat():
                    # Milestone window not yet open ("reaches $200M ARR in
                    # ~12 months"): the tripwire means nothing before the
                    # stated horizon — skipped BEFORE any observation fetch
                    # (cheapest gate first). Panels/armed-falsifier surfaces
                    # still display the condition; only evaluation defers.
                    log.info(
                        {
                            "event": "decision_condition_not_yet_open",
                            "ticker": ticker,
                            "decision_id": decision.decision_id,
                            "condition_index": index,
                            "not_before": cond.not_before,
                        }
                    )
                    continue
                if cond.metric_source not in ("kpi", "financial"):
                    continue  # unresolved at extraction — display-only
                rule = condition_to_rule(decision.decision_id, index, cond)
                if rule is None:
                    continue
                if cond.metric_source == "kpi":
                    observations = fetch_kpi_observations(db, ticker, cond.metric, cond.for_periods)
                else:
                    observations = fetch_financial_history(
                        db, ticker, cond.metric, cond.for_periods
                    )
                evaluation = evaluate_rule(rule, observations)
                if evaluation.status is not BreachStatus.BREACH:
                    continue
                latest = evaluation.observations[0]
                if not _breach_is_fresh(cond, latest.period_end, now):
                    log.info(
                        {
                            "event": "decision_condition_stale_breach_gated",
                            "ticker": ticker,
                            "decision_id": decision.decision_id,
                            "condition_index": index,
                            "period_end": latest.period_end.date().isoformat(),
                            "baseline_period_end": cond.baseline_period_end,
                        }
                    )
                    continue
                candidates.append(
                    TriggerCandidate(
                        ticker=ticker,
                        kind=self.kind,
                        key=f"{decision.decision_id}:{index}:{latest.period_end.date().isoformat()}",
                        evidence=_evidence(decision, index, cond, evaluation.observations),
                        computed_at=now,
                    )
                )
        return candidates

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool:
        """Scan already gated on BREACH; this re-checks only the evidence
        invariants (driver-side dedup owns dismissed-alert suppression)."""
        _ = user_state
        evidence = candidate.evidence
        return (
            isinstance(evidence.get("decision_id"), int)
            and isinstance(evidence.get("condition_index"), int)
            and isinstance(evidence.get("period_end"), str)
        )

    def signature_key_evidence(self, candidate: TriggerCandidate) -> Mapping[str, object]:
        """Dedup on (decision_id, condition_index, period_end): one fire per
        satisfied condition per period; the next period satisfying fires
        fresh. Must match what build_alert feeds compute_signature_sha."""
        return {
            "decision_id": candidate.evidence["decision_id"],
            "condition_index": candidate.evidence["condition_index"],
            "period_end": candidate.evidence["period_end"],
        }

    def build_alert(
        self,
        candidate: TriggerCandidate,
        anchor: ThesisAnchor | None,
    ) -> AlertDraft:
        """Deterministic memo from the stored evidence — no LLM, stable
        across re-runs, fires even with the LLM down."""
        _ = anchor  # the condition already encodes the analyst's thesis link
        ticker = candidate.ticker
        evidence = candidate.evidence

        memo_text = _compose_memo(evidence)
        evidence_json = json.dumps(dict(evidence), sort_keys=True, ensure_ascii=False, default=str)
        signature_sha = compute_signature_sha(
            self.kind, ticker, self.signature_key_evidence(candidate)
        )
        fired_at = datetime.now(UTC).replace(tzinfo=None)

        return AlertDraft(
            trigger_kind=self.kind,
            ticker=ticker,
            fired_at=fired_at,
            evidence_json=evidence_json,
            signature_sha=signature_sha,
            memo_text=memo_text,
        )

    def draft_actions(
        self,
        alert: AlertDraft,
        candidate: TriggerCandidate,
    ) -> list[QueuedActionDraft]:
        """One ``thesis_update`` proposing the revisit. Evidence, never a
        directive (advisor posture): the alert says the analyst's own
        falsifiability bar was hit — the user decides what follows. No
        sizing_update is drafted; the decision being resurfaced already
        carries the size question."""
        _ = alert
        evidence = candidate.evidence
        decision_id = evidence.get("decision_id")
        summary = evidence.get("decision_summary")
        condition_label = evidence.get("condition_label")
        if decision_id is None or not isinstance(condition_label, str):
            return []
        body = (
            f"Falsifiable condition met on {summary}: {condition_label}. "
            "Revisit that decision — you wrote this condition as the thing "
            "that would change your mind."
        )
        return [
            QueuedActionDraft(
                action_kind="thesis_update",
                payload={
                    "body": body,
                    "decision_id": decision_id,
                    "condition_index": evidence.get("condition_index"),
                },
            )
        ]


def _evidence(
    decision: OpenDecision,
    index: int,
    cond: DecisionCondition,
    observations: tuple[KpiObservation, ...],
) -> dict[str, Any]:
    """The per-candidate evidence dict (schema owned by this kind).

    ``observations`` arrive already reconciled to the condition's unit by
    ``evaluate_rule`` — the snapshot the alert stores reads in the same unit
    as the threshold.
    """
    latest = observations[0]
    kind_label = decision.recommendation_kind.upper()
    if decision.recommendation_value is not None:
        kind_label += f" {decision.recommendation_value:g}%"
    made_on = decision.made_at[:10]
    source = decision.source_lens or "memo"
    op_label = _OP_LABELS.get(cond.op, cond.op)
    condition_label = f"{cond.metric} {op_label} {cond.threshold:g} {cond.unit}" + (
        f" for {cond.for_periods} consecutive periods" if cond.for_periods > 1 else ""
    )
    return {
        "decision_id": decision.decision_id,
        "condition_index": index,
        "decision_summary": f"the {made_on} {kind_label} decision ({source})",
        "recommendation_kind": decision.recommendation_kind,
        "recommendation_value": decision.recommendation_value,
        "made_at": decision.made_at,
        "decided_by": decision.decided_by,
        "source_lens": decision.source_lens,
        "baseline_period_end": cond.baseline_period_end,
        "not_before": cond.not_before,
        "metric": cond.metric,
        "metric_source": cond.metric_source,
        "op": cond.op,
        "threshold": cond.threshold,
        "unit": cond.unit,
        "for_periods": cond.for_periods,
        "note": cond.note,
        "condition_label": condition_label,
        "latest_value": float(latest.value),
        "period_end": latest.period_end.date().isoformat(),
        "observed": [
            {"period_end": o.period_end.date().isoformat(), "value": float(o.value)}
            for o in observations
        ],
    }


def _compose_memo(evidence: Mapping[str, Any]) -> str:
    latest_value = evidence.get("latest_value")
    latest_label = f"{latest_value:g}" if isinstance(latest_value, (int, float)) else "?"
    memo = (
        f"Falsifiable condition met on {evidence.get('decision_summary')}: "
        f"{evidence.get('condition_label')} — latest {latest_label} "
        f"{evidence.get('unit')} @ {evidence.get('period_end')}. "
        "Revisit that decision."
    )
    note = evidence.get("note")
    if isinstance(note, str) and note:
        memo += f' You wrote: "{note}"'
    return memo
