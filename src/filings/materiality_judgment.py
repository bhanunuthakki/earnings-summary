"""Thesis-materiality judgment — the ONE gate that lets disclosure drift elevate.

Owner ruling (2026-08-02): raw disclosure drift is far too onerous to put in
front of the owner. An event may be ELEVATED to an owner-facing surface only
when an LLM judges that it exceeds materiality in one strict sense: the change
**fundamentally restricts the ability to MEASURE the thesis** — the issuer
stopped disclosing, aggregated away, redefined, or obscured something the
thesis' tier-1 KPIs / break conditions / key driver depend on.

This module writes that judgment to ``disclosure_events.thesis_materiality``
(migration 0271) — the "distinct judgment column, NOT another float threshold"
contract the 2026-07-30 inbox root-cause investigation required. The stored
``materiality`` float mixes three incommensurable per-detector scales and is
never consulted here or by any surface gating on this column.

Shape mirrors ``filings.boilerplate_triage`` (call → parse → degrade-honestly,
bounded recovery call for omitted ids), with two deliberate differences:

* The prompt carries the ticker's THESIS ANCHOR (``llm.anchors.
  load_thesis_anchor`` — thesis statement, tier-1 KPIs with break conditions,
  quantitative thesis-breakers). Without a thesis on file the question "does
  this restrict measuring the thesis?" is unanswerable, so the ticker is
  SKIPPED with a logged event and every row stays NULL (= not elevated) —
  never guessed.
* Sonnet tier (``disclosure_thesis_materiality`` in ``llm.cli.LLM_MODELS``),
  not Haiku: this verdict is the sole elevation gate, and the judgment is
  thesis-vs-disclosure semantics, not a closed-vocabulary text property.

Degradation contract (silent-degradation-class rule): a failed or unparseable
call leaves every candidate NULL — surfaces treat NULL as not-elevated, and
the next weekly sweep run retries. Hard stops (budget cap / missing CLI, per
``llm.cli.is_hard_stop``) propagate so the caller fails loudly. Table-shaped
hunks (``filings.specificity.looks_tabular``) are DEFERRED before the call —
tallied, left NULL, never judged and never charged for — because the stored
backlog still carries mangled 10-K table scrapings that predate the P0 emit
gate.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from filings.models import HardStopError
from filings.specificity import looks_tabular
from filings.store import table_exists
from llm.cli import is_hard_stop
from llm.structured import call_llm_structured

log = logging.getLogger(__name__)

EVENTS_TABLE = "disclosure_events"


def _require_events_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, EVENTS_TABLE):
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migrations 0203 + 0271) "
            "before judging thesis materiality"
        )


JUDGMENT_PURPOSE = "disclosure_thesis_materiality"

#: Verdicts that can never elevate regardless of judgment — the same
#: noise-class exclusion the retired inbox eligibility filter used.
NOISE_CLASS_VERDICTS: tuple[str, ...] = ("noise", "mechanical", "boilerplate_update")

#: Candidates per LLM call. Batched like boilerplate_triage; the anchor is
#: repeated per call, so batches stay large enough to amortize it.
BATCH_SIZE = 40


class ThesisMateriality(StrEnum):
    RESTRICTS_MEASUREMENT = "restricts_measurement"
    NOT_MATERIAL = "not_material"


class MaterialityVerdict(BaseModel):
    event_id: int
    materiality: ThesisMateriality
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=300)


class JudgmentOutcome(BaseModel):
    """Per-ticker judgment result.

    ``degraded=True`` means an LLM call/parse failed; every id it covered is
    absent from ``verdicts`` and must stay NULL — never defaulted either way.
    ``skipped_no_thesis`` / ``deferred_tabular`` make the two non-LLM exits
    countable instead of invisible.
    """

    ticker: str
    verdicts: dict[int, MaterialityVerdict] = Field(default_factory=dict[int, MaterialityVerdict])
    degraded: bool = False
    degrade_reason: str | None = None
    skipped_no_thesis: bool = False
    deferred_tabular: int = 0


class JudgmentCandidate(BaseModel):
    """Read-side projection of one elevation-eligible ``disclosure_events`` row."""

    model_config = ConfigDict(frozen=True)

    id: int
    ticker: str
    event_type: str
    canonical_id: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    subject: str
    subject_label: str | None
    verdict: str
    excerpt: str


def fetch_judgment_candidates(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    only_unjudged: bool = True,
    limit: int = 150,
) -> list[JudgmentCandidate]:
    """Elevation-eligible rows for one ticker, newest first.

    Eligible = not dismissed, not a noise-class verdict, and carrying SOME
    quoted evidence to judge. ``only_unjudged`` (the default) restricts to
    ``thesis_materiality IS NULL`` so re-runs are idempotent and a weekly
    sweep chews through the backlog incrementally under ``limit``.
    """
    _require_events_table(conn)
    marks = ",".join("?" * len(NOISE_CLASS_VERDICTS))
    clauses = [
        "ticker = ?",
        "status != 'dismissed'",
        f"verdict NOT IN ({marks})",
        "(COALESCE(evidence_quote,'') != '' OR COALESCE(current_excerpt,'') != '' "
        "OR COALESCE(prior_excerpt,'') != '')",
    ]
    params: list[object] = [ticker.strip().upper(), *NOISE_CLASS_VERDICTS]
    if only_unjudged:
        clauses.append("thesis_materiality IS NULL")
    params.append(int(limit))

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT id, ticker, event_type, canonical_id, fiscal_year, fiscal_period,
                   subject, subject_label, prior_excerpt, current_excerpt,
                   evidence_quote, verdict
            FROM {EVENTS_TABLE}
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,  # nosec B608 -- fixed clause fragments; values remain bound
            tuple(params),
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    out: list[JudgmentCandidate] = []
    for r in rows:
        excerpt = str(
            r["evidence_quote"] or r["current_excerpt"] or r["prior_excerpt"] or ""
        ).strip()
        out.append(
            JudgmentCandidate(
                id=int(r["id"]),
                ticker=str(r["ticker"]),
                event_type=str(r["event_type"]),
                canonical_id=r["canonical_id"],
                fiscal_year=r["fiscal_year"],
                fiscal_period=r["fiscal_period"],
                subject=str(r["subject"]),
                subject_label=r["subject_label"],
                verdict=str(r["verdict"]),
                excerpt=excerpt,
            )
        )
    return out


def _build_prompt(ticker: str, thesis_anchor: str, batch: Sequence[JudgmentCandidate]) -> str:
    rows: list[str] = []
    for c in batch:
        period = " ".join(
            str(part) for part in (c.fiscal_year, c.fiscal_period) if part not in (None, "")
        )
        rows.append(
            f"- id={c.id} | type: {c.event_type} | concept: {c.canonical_id or 'cross-document'}"
            f" | period: {period or 'unknown'} | subject: {(c.subject_label or c.subject)!r}\n"
            f"  change: {c.excerpt}"
        )
    listing = "\n".join(rows)
    return f"""{thesis_anchor}

You are the ELEVATION GATE for {ticker}'s disclosure-drift events. Each item below is a \
detected change in what the company discloses (filings, XBRL metrics, guidance, transcripts). \
Almost all disclosure drift is NOT worth the owner's attention — your default answer is \
"not_material".

An item is "restricts_measurement" ONLY if the change fundamentally restricts the ability to \
MEASURE the thesis above: the company stopped disclosing, aggregated away, redefined, replaced, \
or obscured a metric, segment, cohort, or driver that a tier-1 KPI, break condition, \
quantitative thesis-breaker, or the key driver depends on. The bar is measurement continuity, \
not newsworthiness.

NOT sufficient, no matter how notable: new or reworded risk language, added disclosures, tone \
shifts, legal/boilerplate housekeeping, restated tables that keep the metric observable, \
changes to metrics the thesis does not track. When uncertain, answer "not_material" — a missed \
elevation costs a weekly re-look; a false elevation erodes trust in the gate.

For EACH item return:
1. materiality: "restricts_measurement" or "not_material".
2. confidence: 0.0-1.0.
3. rationale: one sentence naming WHICH thesis KPI/break-rule/driver is affected (for \
"restricts_measurement") or why measurement is unaffected (for "not_material").

Items:
{listing}

Return ONLY a JSON object: {{"<event_id>": {{"materiality": "...", "confidence": 0.0, \
"rationale": "..."}}, ...}}
Every id listed above MUST appear as a key exactly once, using its exact integer id as a string."""


def _parse_verdicts(payload: object) -> tuple[dict[int, MaterialityVerdict], int]:
    parsed: dict[int, MaterialityVerdict] = {}
    dropped = 0
    for key, raw in cast("dict[str, object]", payload).items():
        try:
            event_id = int(key)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not isinstance(raw, dict):
            dropped += 1
            continue
        row = cast("dict[str, object]", raw)
        try:
            parsed[event_id] = MaterialityVerdict(
                event_id=event_id,
                materiality=ThesisMateriality(str(row.get("materiality"))),
                confidence=float(cast("float", row.get("confidence", 0.0))),
                rationale=str(row.get("rationale") or "")[:300],
            )
        except (ValueError, TypeError):
            dropped += 1
    return parsed, dropped


def judge_ticker_events(
    ticker: str,
    candidates: Sequence[JudgmentCandidate],
    thesis_anchor: str,
    *,
    db_path: Path | str | None = None,
) -> JudgmentOutcome:
    """Judge one ticker's eligible events against its thesis anchor.

    Empty/absent ``thesis_anchor`` → ``skipped_no_thesis`` outcome with no LLM
    call. Table-shaped excerpts are deferred (counted, not judged). Hard stops
    propagate; any other failure degrades to an outcome whose uncovered ids
    simply stay NULL.
    """
    if not thesis_anchor.strip():
        log.info(
            {
                "event": "thesis_materiality_skipped_no_thesis",
                "ticker": ticker,
                "n_candidates": len(candidates),
                "note": "no micro_thesis holdings JSON — measurement question unanswerable; "
                "rows stay unjudged and therefore not elevated",
            }
        )
        return JudgmentOutcome(ticker=ticker, skipped_no_thesis=True)

    judgeable = [c for c in candidates if not looks_tabular(c.excerpt)]
    deferred = len(candidates) - len(judgeable)
    if deferred:
        log.info(
            {
                "event": "thesis_materiality_tabular_deferred",
                "ticker": ticker,
                "deferred": deferred,
                "note": "table-shaped excerpts left unjudged (backlog rows predating the "
                "P0 emit gate); NULL = not elevated",
            }
        )
    if not judgeable:
        return JudgmentOutcome(ticker=ticker, deferred_tabular=deferred)

    verdicts: dict[int, MaterialityVerdict] = {}
    degraded = False
    degrade_reason: str | None = None

    for start in range(0, len(judgeable), BATCH_SIZE):
        batch = judgeable[start : start + BATCH_SIZE]
        try:
            decoded = call_llm_structured(
                _build_prompt(ticker, thesis_anchor, batch),
                purpose=JUDGMENT_PURPOSE,
                ticker=ticker,
                expect="object",
                db_path=db_path,
            )
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            degraded = True
            degrade_reason = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.error(
                {
                    "event": "thesis_materiality_call_failed_degrading",
                    "ticker": ticker,
                    "batch_start": start,
                    "n_candidates": len(batch),
                    "error": degrade_reason,
                }
            )
            continue

        parsed, dropped = _parse_verdicts(decoded)
        if dropped:
            log.warning(
                {"event": "thesis_materiality_rows_dropped", "ticker": ticker, "count": dropped}
            )
        verdicts.update(parsed)

        missing = {c.id for c in batch} - verdicts.keys()
        if not missing:
            continue
        log.warning(
            {
                "event": "thesis_materiality_missing_verdicts",
                "ticker": ticker,
                "missing": sorted(missing),
            }
        )
        recovery_batch = [c for c in batch if c.id in missing]
        try:
            recovery_decoded = call_llm_structured(
                _build_prompt(ticker, thesis_anchor, recovery_batch),
                purpose=JUDGMENT_PURPOSE,
                ticker=ticker,
                expect="object",
                db_path=db_path,
            )
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.error(
                {
                    "event": "thesis_materiality_recovery_failed",
                    "ticker": ticker,
                    "n_candidates": len(recovery_batch),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            recovered, recovery_dropped = _parse_verdicts(recovery_decoded)
            verdicts.update(recovered)
            if recovery_dropped:
                log.warning(
                    {
                        "event": "thesis_materiality_recovery_rows_dropped",
                        "ticker": ticker,
                        "count": recovery_dropped,
                    }
                )
            still_missing = {c.id for c in batch} - verdicts.keys()
            if still_missing:
                log.warning(
                    {
                        "event": "thesis_materiality_recovery_missing_verdicts",
                        "ticker": ticker,
                        "missing": sorted(still_missing),
                    }
                )

    return JudgmentOutcome(
        ticker=ticker,
        verdicts=verdicts,
        degraded=degraded,
        degrade_reason=degrade_reason,
        deferred_tabular=deferred,
    )


def write_judgments(
    conn: sqlite3.Connection,
    verdicts: Sequence[MaterialityVerdict],
    *,
    judged_at: datetime | None = None,
) -> int:
    """Persist judgments onto ``disclosure_events`` by id. Idempotent."""
    _require_events_table(conn)
    stamp = judged_at or datetime.now(UTC).replace(tzinfo=None)
    written = 0
    for v in verdicts:
        conn.execute(
            f"UPDATE {EVENTS_TABLE} SET thesis_materiality = ?, "
            "thesis_materiality_rationale = ?, thesis_materiality_judged_at = ? "
            "WHERE id = ?",  # nosec B608 -- fixed table name; values bound
            (v.materiality.value, v.rationale, stamp.isoformat(), v.event_id),
        )
        written += 1
    return written
