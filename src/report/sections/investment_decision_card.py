"""Investment Decision Card section (PRD §8.1, P1.1). Read-only: reads the
current ``llm_artifacts`` row for purpose='investment_decision_card' — this
module NEVER generates. Generation is
``execution/build_investment_decision_card.py``'s job, run at the end of a
successful evaluation build or on an explicit owner refresh (never on a
workspace GET — PRD §8.1: "Do not run an LLM on every workspace GET").

Built for BOTH report flavors (portfolio and evaluation) — the workspace
renderer decides how prominently to show the strip; the section itself is
flavor-agnostic (a held name can carry a card just as an evaluation name
can).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from report.models import (
    DecisionCardDisconfirmingCase,
    DecisionCardEvidenceReadiness,
    DecisionCardHypothesis,
    DecisionCardPortfolioFit,
    DecisionCardSecuritySetup,
    DecisionCardUncertainty,
    InvestmentDecisionCardSection,
    SectionStatus,
)

log = logging.getLogger(__name__)

PURPOSE = "investment_decision_card"

# Mirrors the workspace's general "stale" framing (synthesis.py's
# STALE_THRESHOLD_DAYS-style convention) — informational only, never hides
# the strip; the renderer decides how to word it.
_STALE_THRESHOLD_DAYS = 45


def _str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in cast("list[object]", raw) if isinstance(x, str)]


def build(ticker: str, repo_root: Path) -> InvestmentDecisionCardSection | None:
    """The current card, or ``None`` when none has been generated yet, the
    artifact's content_json failed to decode, or the store is unavailable —
    every case the renderer must hide the strip entirely rather than stub it
    (PRD §8.1's frontend rule)."""
    ticker = ticker.upper()
    try:
        import llm_artifact_store
    except ImportError as exc:  # pragma: no cover — the report layer always
        # ships with llm_artifact_store; guarded the same way synthesis.py
        # guards its own late import.
        log.warning({"event": "decision_card_import_failed", "error": str(exc)})
        return None

    db_path = repo_root / "data" / "portfolio.db"
    artifact = llm_artifact_store.read_current(
        ticker=ticker, purpose=PURPOSE, scope="ticker", db_path=db_path
    )
    if artifact is None or not isinstance(artifact.content_json, dict):
        return None

    raw = cast("dict[str, object]", artifact.content_json)
    try:
        generated_at = artifact.generated_at
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        is_stale = generated_at < (datetime.now(UTC) - timedelta(days=_STALE_THRESHOLD_DAYS))
        section = InvestmentDecisionCardSection(
            status=SectionStatus.OK,
            ticker=ticker,
            artifact_id=artifact.id,
            generated_at=artifact.generated_at,
            dirty=artifact.dirty,
            dirty_reason=artifact.dirty_reason,
            is_stale=is_stale,
            as_of=str(raw.get("as_of") or "") or None,
            hypothesis_origin=str(raw.get("hypothesis_origin") or "") or None,
            suggested_disposition=str(raw.get("suggested_disposition") or "") or None,
            source_refs=_str_list(raw.get("source_refs")),
            company_hypothesis=DecisionCardHypothesis.model_validate(
                raw.get("company_hypothesis") or {}
            ),
            security_setup=DecisionCardSecuritySetup.model_validate(
                raw.get("security_setup") or {}
            ),
            portfolio_fit=DecisionCardPortfolioFit.model_validate(raw.get("portfolio_fit") or {}),
            disconfirming_case=DecisionCardDisconfirmingCase.model_validate(
                raw.get("disconfirming_case") or {}
            ),
            evidence_readiness=DecisionCardEvidenceReadiness.model_validate(
                raw.get("evidence_readiness") or {}
            ),
            uncertainty=DecisionCardUncertainty.model_validate(raw.get("uncertainty") or {}),
        )
    except Exception as exc:
        log.warning(
            {"event": "decision_card_section_validate_failed", "ticker": ticker, "error": str(exc)}
        )
        return None
    return section
