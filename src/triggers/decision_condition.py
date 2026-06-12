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
from decision_conditions import DecisionCondition, OpenDecision, load_open_decisions
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


def _fetch_financial_history(
    conn: sqlite3.Connection,
    ticker: str,
    line_item: str,
    n_periods: int,
) -> list[KpiObservation] | None:
    """financial_facts twin of ``fetch_kpi_observations``.

    Line items are already canonical (the extraction prompt copies them
    verbatim from the ticker's vocabulary), so no resolver pass. Coexisting
    rows per period dedup to the latest-ingested source (MAX(source_doc_id)),
    and one observation survives per period_end DATE — a same-date FY/Q4 pair
    collapses rather than double-counting a consecutive-periods window.
    Returns None when the line item has no rows at all (unresolvable),
    matching the kpi fetcher's no-definition signal.
    """
    rows = conn.execute(
        "SELECT ff.period_end, ff.value, ff.unit "
        "FROM financial_facts ff "
        "WHERE ff.ticker = ? AND ff.line_item = ? "
        "  AND ff.id = ("
        "      SELECT f2.id FROM financial_facts f2 "
        "      WHERE f2.ticker = ff.ticker AND f2.line_item = ff.line_item "
        "        AND f2.period_end = ff.period_end "
        "      ORDER BY f2.source_doc_id DESC, f2.id DESC LIMIT 1) "
        "ORDER BY ff.period_end DESC LIMIT ?",
        (ticker.upper(), line_item, n_periods),
    ).fetchall()
    if not rows:
        return None
    out: list[KpiObservation] = []
    for row in rows:
        period = row["period_end"]
        if isinstance(period, str):
            period = datetime.fromisoformat(period)
        try:
            value = Decimal(str(row["value"]))
            unit = Unit(row["unit"]) if row["unit"] else Unit.ACTUAL
        except (InvalidOperation, ValueError):
            continue
        out.append(KpiObservation(period_end=period, value=value, unit=unit))
    return out or None


def _condition_rule(decision_id: int, index: int, cond: DecisionCondition) -> BreakRule | None:
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
        emit a candidate per condition whose status is BREACH."""
        # The driver opens this connection fresh per (ticker, trigger) scan and
        # closes it right after; the fetchers (fetch_kpi_observations and the
        # local financial one) require Row access, so set it here.
        db.row_factory = sqlite3.Row
        ticker = ticker.upper()
        candidates: list[TriggerCandidate] = []
        now = datetime.now(UTC).replace(tzinfo=None)

        for decision in load_open_decisions(db, ticker):
            for index, cond in enumerate(decision.conditions):
                if cond.metric_source not in ("kpi", "financial"):
                    continue  # unresolved at extraction — display-only
                rule = _condition_rule(decision.decision_id, index, cond)
                if rule is None:
                    continue
                if cond.metric_source == "kpi":
                    observations = fetch_kpi_observations(db, ticker, cond.metric, cond.for_periods)
                else:
                    observations = _fetch_financial_history(
                        db, ticker, cond.metric, cond.for_periods
                    )
                evaluation = evaluate_rule(rule, observations)
                if evaluation.status is not BreachStatus.BREACH:
                    continue
                latest = evaluation.observations[0]
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
        "source_lens": decision.source_lens,
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
