"""Grade prior LLM bear-case hypotheses against subsequent realized data.

The LLM emits failure-mode hypotheses in `purpose='bear_case'` artifacts.
Each hypothesis has:
  - hypothesis (one-sentence concrete failure mode)
  - evidence_in_data (the prior data point that suggested it)
  - leading_indicator (what would confirm in next 1-2 prints)
  - quantitative_impact (the $ / margin delta if it materializes)
  - refutation_criteria (what would refute over 2-4 quarters)

This module:
  1. Reads ALL prior bear cases for a ticker (history, via supersession chain).
  2. For each historical bear case that is N+ quarters old, instantiates
     each failure mode as a `predictions` row with source_kind='llm_bear_case'.
  3. Runs an LLM grading pass: read the failure mode + the subsequent
     quarters of data → outcome='met' | 'missed' | 'mixed' | 'unfalsifiable'
     with confidence + notes.
  4. Records the outcomes in the predictions table.

Net effect: the LLM gets its own SayDo scoreboard. The mgmt_credibility
lens can then cite "the LLM bear case for GOOG was right 3/5 quarters."

This is a cross-quarter analytical capability that almost no automated
system does. Real differentiator.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, TypeAdapter

from llm.structured import StructuredParseError, call_llm_structured
from llm.untrusted import spotlight
from llm_artifact_store import history as artifact_history
from predictions_store import grade as grade_prediction
from predictions_store import history as prediction_history
from predictions_store import record as record_prediction
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

DEFAULT_GRADE_AGE_QUARTERS = 1  # grade hypotheses at least 1 quarter old


class _BearGrade(BaseModel):
    outcome: Literal["met", "missed", "mixed", "unfalsifiable"]
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str


@dataclass(slots=True)
class FailureMode:
    """One row parsed out of a bear-case artifact's content_json."""

    hypothesis: str
    evidence_in_data: str
    leading_indicator: str
    quantitative_impact: str
    refutation_criteria: str


def materialize_predictions(*, ticker: str, repo_root: Path, max_history: int = 8) -> int:
    """For every historical bear case for ticker, write its failure modes as
    rows in `predictions` (idempotent via predictions_store.record's natural key).

    Returns the count of newly-inserted predictions. Existing rows are skipped.
    """
    ticker = ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"
    bears = artifact_history(ticker=ticker, purpose="bear_case", limit=max_history, db_path=db_path)
    inserted = 0
    for art in bears:
        modes = _parse_failure_modes(art.content_md, art.content_json)
        if not modes:
            continue
        # Use the artifact's generated_at as made_at; target_period = +90d (next quarter)
        made_at = art.generated_at
        target_period = made_at + timedelta(days=95)
        for idx, fm in enumerate(modes):
            pid = record_prediction(
                ticker=ticker,
                source_kind="llm_bear_case",
                source_artifact_id=art.id,
                source_excerpt=fm.evidence_in_data[:512],
                made_at=made_at,
                target_period=target_period,
                prediction_md=(
                    f"**{fm.hypothesis}**\n"
                    f"- Leading indicator: {fm.leading_indicator}\n"
                    f"- Quant impact: {fm.quantitative_impact}\n"
                    f"- Refutation: {fm.refutation_criteria}"
                ),
                # No structured KPI; the grader will read the prediction_md
                # against subsequent data
                kpi_name=f"bear_fm_{idx}",
                db_path=db_path,
            )
            if pid is not None:
                inserted += 1
    return inserted


def grade_due_predictions(
    *,
    ticker: str,
    repo_root: Path,
    grade_age_quarters: int = DEFAULT_GRADE_AGE_QUARTERS,
) -> dict[str, int]:
    """Pull bear-case predictions whose target_period has passed and grade
    them via LLM. Returns count by outcome.

    Each ungraded prediction gets ONE LLM call (Sonnet). The grader is given
    the failure mode + the most recent quarter's earnings summary + recent
    financials + recent insider activity, and emits an outcome verdict.

    Cheap: typically 5-10 predictions per ticker per cycle, sub-$0.50.
    """
    ticker = ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"

    cutoff = datetime.now(UTC) - timedelta(days=90 * grade_age_quarters)
    preds = prediction_history(
        ticker=ticker, source_kinds=["llm_bear_case"], limit=200, db_path=db_path
    )
    due = [
        p
        for p in preds
        if p.outcome == "pending"
        and p.target_period is not None
        and p.target_period <= datetime.now(UTC)
        and p.made_at <= cutoff
    ]
    if not due:
        log.info({"event": "bear_grader_idle", "ticker": ticker, "n_pending": 0})
        return {"met": 0, "missed": 0, "mixed": 0, "unfalsifiable": 0}

    # Load the corpus the grader will read against
    corpus = _load_grading_corpus(ticker, repo_root)

    summary: dict[str, int] = {"met": 0, "missed": 0, "mixed": 0, "unfalsifiable": 0}
    for pred in due:
        verdict = _grade_one_prediction(ticker=ticker, pred=pred, corpus=corpus)
        if verdict is None:
            continue
        outcome = verdict.outcome
        confidence = verdict.confidence
        notes = verdict.notes
        ok = grade_prediction(
            prediction_id=pred.id,
            outcome=outcome,
            outcome_confidence=float(confidence) if confidence else None,
            notes=notes,
            db_path=db_path,
        )
        if ok:
            summary[outcome] = summary.get(outcome, 0) + 1
    return summary


def _grade_one_prediction(
    *,
    ticker: str,
    pred: object,  # Prediction dataclass
    corpus: dict[str, str],
) -> _BearGrade | None:
    """Single LLM grading call. Returns {outcome, confidence, notes}."""
    p = pred  # type: ignore[assignment]
    evidence = spotlight(
        f"""Hypothesis date: {p.made_at.date().isoformat() if hasattr(p, "made_at") and p.made_at else "?"}
Hypothesis: {p.prediction_md}
Recent earnings summary: {corpus.get("latest_summary", "(none)")}
Recent financials: {corpus.get("financials", "(none)")}
Recent insider activity: {corpus.get("insiders", "(none)")}""",
        source="prior LLM hypothesis and subsequent issuer/market evidence",
    )
    prompt = f"""You are grading a prior LLM-generated bear-case hypothesis for {ticker}
against subsequent realized data. The hypothesis was made on {p.made_at.date().isoformat() if hasattr(p, "made_at") and p.made_at else "?"}.

{evidence}

**Your task:** read the hypothesis + the subsequent data and emit ONE
JSON object with these fields. Return ONLY the JSON, no commentary:

{{
  "outcome": "met" | "missed" | "mixed" | "unfalsifiable",
  "confidence": <float 0..1>,
  "notes": "<one-paragraph reasoning citing the specific data point that
            drives the verdict>"
}}

Definitions:
  met            — the hypothesis materialized as expected
  missed         — the hypothesis was refuted by data
  mixed          — partially confirmed, partially refuted
  unfalsifiable  — the data doesn't speak to it yet (the leading indicator
                   wasn't disclosed, the period is too short, etc.)
"""
    try:
        # Model resolves from LLM_MODELS["bear_case_grading"] — the registry
        # is the single reviewable surface for pins (llm_evals_plan.md §5.5).
        grade = cast(
            "_BearGrade",
            call_llm_structured(
                prompt,
                purpose="bear_case_grading",
                ticker=ticker,
                scope="bear_case_grading",
                schema=TypeAdapter(_BearGrade),
            ),
        )
    except StructuredParseError as exc:
        log.warning({"event": "grader_structured_failed", "error": str(exc)})
        return None
    except Exception as exc:  # log and skip — grading is retried next run
        log.warning({"event": "grader_llm_failed", "error": str(exc)})
        return None
    return grade


def _load_grading_corpus(ticker: str, repo_root: Path) -> dict[str, str]:
    """The data the grader reads against."""
    tmp = repo_root / ".tmp"
    summaries: list[tuple[str, str]] = []
    for p in sorted(tmp.glob(f"{ticker}_Q*_*_summary.txt"), reverse=True):
        try:
            summaries.append((p.stem, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
        if len(summaries) >= 2:
            break

    db_path = repo_root / "data" / "portfolio.db"
    fin_lines: list[str] = []
    insider_lines: list[str] = []
    if db_path.exists():
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                """
                SELECT period_end, line_item, value FROM financial_facts
                WHERE ticker = ? AND fiscal_period_type IN ('Q1','Q2','Q3','Q4')
                ORDER BY period_end DESC LIMIT 80
                """,
                (ticker,),
            ):
                fin_lines.append(
                    f"- {str(r['period_end'])[:10]} · {r['line_item']}: {float(r['value'] or 0):,.0f}"
                )
            # Insiders
            cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
            if (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='insider_transactions'"
                ).fetchone()
                is not None
            ):
                for r in conn.execute(
                    """
                    SELECT transaction_date, insider_name, insider_title,
                           transaction_type, shares, transaction_value
                    FROM insider_transactions
                    WHERE ticker = ? AND transaction_date >= ?
                    ORDER BY transaction_value DESC NULLS LAST LIMIT 30
                    """,
                    (ticker, cutoff),
                ):
                    val = r["transaction_value"]
                    val_str = (
                        f"${float(val) / 1e6:.1f}M"
                        if val and float(val) >= 1e6
                        else f"${float(val or 0) / 1e3:.0f}K"
                    )
                    insider_lines.append(
                        f"- {str(r['transaction_date'])[:10]} · {r['insider_name']} "
                        f"({r['insider_title'] or '?'}) · {r['transaction_type']} · {val_str}"
                    )
        finally:
            conn.close()

    return {
        "latest_summary": "\n\n".join(f"### {q}\n{txt[:3000]}" for q, txt in summaries)
        or "(no summaries)",
        "financials": "\n".join(fin_lines) or "(no financials)",
        "insiders": "\n".join(insider_lines) or "(no insiders)",
    }


def _parse_failure_modes(content_md: str | None, content_json: object | None) -> list[FailureMode]:
    """Parse failure modes out of either the JSON or markdown content of a
    bear case artifact. Defensive — returns [] on any parse issue."""
    if isinstance(content_json, dict):
        fms_raw = content_json.get("failure_modes")
        if isinstance(fms_raw, list):
            return _coerce_failure_modes(fms_raw)
    if not content_md:
        return []
    # Try JSON within markdown
    s = content_md.strip()
    if s.startswith("{"):
        try:
            decoded = json.loads(s)
            if isinstance(decoded, dict):
                fms_raw = decoded.get("failure_modes")
                if isinstance(fms_raw, list):
                    return _coerce_failure_modes(fms_raw)
        except json.JSONDecodeError:
            pass
    return []


def _coerce_failure_modes(raw: list[object]) -> list[FailureMode]:
    out: list[FailureMode] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        h = e.get("hypothesis")
        if not isinstance(h, str) or not h.strip():
            continue
        out.append(
            FailureMode(
                hypothesis=h.strip(),
                evidence_in_data=str(e.get("evidence_in_data", "")),
                leading_indicator=str(e.get("leading_indicator", "")),
                quantitative_impact=str(e.get("quantitative_impact", "")),
                refutation_criteria=str(e.get("refutation_criteria", "")),
            )
        )
    return out
