"""Falsifiable decision conditions — extraction + attachment (fund-grade S5).

The decisions ledger records WHAT was recommended; the falsifiability — the
"## What would change my mind" section every five_min_reread artifact and
Socratic advisor memo carries — lived only in prose. This module turns that
prose into the structured ``decisions.decision_conditions`` JSON (alembic
0086) so the evaluator rung (``triggers.decision_condition``) can check each
condition against incoming kpi_facts / financial_facts and resurface the
decision when one is satisfied.

Condition schema (one JSON array element per condition):

    {"metric": "NPL 90d+",        # canonical kpi_definitions.name /
                                  #   financial_facts.line_item, or the prose
                                  #   phrase when neither matched
     "metric_source": "kpi",      # 'kpi' | 'financial' | null (unresolved —
                                  #   displayed, never evaluated)
     "op": "ge",                  # Comparator vocabulary (lt/le/gt/ge) —
                                  #   the direction that SATISFIES the condition
     "threshold": 7.0,
     "unit": "percent",           # models.facts.Unit value
     "for_periods": 2,            # consecutive periods required
     "note": "90d+ NPL above 7% for two straight quarters"}

``op``/``unit``/``for_periods`` deliberately mirror ``BreakRule`` fields
(compute.thesis_evaluator) so evaluation reuses ``evaluate_rule`` verbatim —
same ``convert_unit`` reconciliation, same consecutive-periods semantics; the
break-rule unit-bug class (#317/#320) cannot reappear here.

Extraction is a new LLM purpose ``decision_conditions_extract`` (Haiku pin in
LLM_MODELS; golden set in evals/golden/decision_conditions_extract.json) via
the shared ``call_llm_structured`` parse-retry helper. Metric resolution
happens AT extraction time: the prompt carries the ticker's known KPI names +
financial line items and the model copies one verbatim (or marks the
condition unresolved) — evaluation stays deterministic.

Two write rungs, both idempotent:

  record_socratic_decisions — Socratic memos live in advisor_memos (not
      llm_artifacts), so they get decisions rows here, keyed by the partial
      UNIQUE on source_memo_id (the artifact UNIQUE's twin). The stance maps
      onto recommendation_kind (buy→add: the Socratic flow runs on holdings).
  attach_conditions — walks decisions rows not yet extracted
      (conditions_extracted_at IS NULL), pulls the source prose (artifact
      content_md or memo body_md), extracts, stamps. "Extracted, prose had no
      falsifiable conditions" is a stamped '[]', distinct from NULL.

The numeric pass deliberately skips QUALITATIVE triggers that carry no number
("the CEO departs", "a credible competitor enters") — a falsifiable condition
no reported figure can evaluate, which therefore never fired (it waited on
next quarter's XBRL, which for a qualitative event never comes). The L9-PR2
qualitative twin (``attach_qualitative_conditions`` → ``qualitative_conditions``
column, alembic 0108, own ``qualitative_conditions_extract`` purpose) extracts
those into ``QualitativeCondition`` (phrase + news/earnings ``watch_for`` tag) so
``load_open_qualitative_conditions`` can surface them on the existing
material_news / earnings_tone trigger alerts — firing them on NEWS, not a
number. Kept entirely apart from the numeric path: separate column, separate
stamp, separate prompt + golden, so the two never collide.

Both follow decision_extractor's best-effort posture against a missing DB /
table; LLM hard stops (budget/setup) propagate per the repo-wide policy.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter, field_validator

from clock import now_iso
from llm.cli import is_hard_stop
from llm.structured import StructuredParseError, call_llm_structured
from models.facts import Unit
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

if TYPE_CHECKING:
    from compute.thesis_evaluator import KpiObservation

log = logging.getLogger(__name__)

PURPOSE = "decision_conditions_extract"

# The companion qualitative pass (L9 PR2): non-numeric "what would change my
# mind" triggers the numeric extractor is told to skip. Routed to the
# material_news / earnings_tone triggers so they fire on NEWS, not next
# quarter's XBRL. Its own purpose so the numeric path and golden set are
# untouched.
QUALITATIVE_PURPOSE = "qualitative_conditions_extract"

# Which event surface a qualitative condition is watched on — the trigger that
# should surface it. 'both' surfaces on either. 'news' → material_news,
# 'earnings' → earnings_tone.
QUALITATIVE_WATCH_SURFACES: frozenset[str] = frozenset({"news", "earnings", "both"})

# Direction vocabulary — the four directional members of
# compute.thesis_evaluator.Comparator. EQ is excluded on purpose: a
# falsifiable "what would change my mind" threshold is directional.
CONDITION_OPS: frozenset[str] = frozenset({"lt", "le", "gt", "ge"})
METRIC_SOURCES: frozenset[str] = frozenset({"kpi", "financial"})
_UNIT_VALUES: frozenset[str] = frozenset(u.value for u in Unit)

_MAX_CONDITIONS_PER_DECISION = 8
_MAX_QUALITATIVE_PER_DECISION = 6
_MAX_VOCAB_ENTRIES = 80


class _DecisionConditionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=200)
    metric_source: Literal["kpi", "financial"] | None
    op: Literal["lt", "le", "gt", "ge"]
    threshold: float
    unit: Literal["actual", "thousands", "millions", "billions", "percent", "ratio", "bps", "count"]
    for_periods: int = Field(ge=1)
    not_before: str | None = None
    note: str | None = Field(default=None, max_length=300)


class _QualitativeConditionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=1, max_length=200)
    watch_for: Literal["news", "earnings", "both"] = "both"
    note: str | None = Field(default=None, max_length=300)


class _DecisionConditionsWire(RootModel[list[_DecisionConditionWire]]):
    @field_validator("root", mode="before")
    @classmethod
    def _drop_invalid(cls, value: object) -> list[_DecisionConditionWire]:
        if not isinstance(value, list):
            raise ValueError("decision conditions must be an array")
        valid: list[_DecisionConditionWire] = []
        for entry in cast("list[object]", value):
            try:
                valid.append(_DecisionConditionWire.model_validate(entry))
            except ValueError:
                continue
        return valid


class _QualitativeConditionsWire(RootModel[list[_QualitativeConditionWire]]):
    @field_validator("root", mode="before")
    @classmethod
    def _drop_invalid(cls, value: object) -> list[_QualitativeConditionWire]:
        if not isinstance(value, list):
            raise ValueError("qualitative conditions must be an array")
        valid: list[_QualitativeConditionWire] = []
        for entry in cast("list[object]", value):
            try:
                valid.append(_QualitativeConditionWire.model_validate(entry))
            except ValueError:
                continue
        return valid


_DECISION_CONDITIONS_ADAPTER = TypeAdapter(_DecisionConditionsWire)
_QUALITATIVE_CONDITIONS_ADAPTER = TypeAdapter(_QualitativeConditionsWire)

# Matches "## What would change my mind" in both source shapes: the
# five_min_reread lens numbers it ("## 3. What would change my mind"), the
# Socratic memo doesn't. Same tolerance as decision_extractor._SECTION_RX.
_SECTION_RX = re.compile(
    r"##\s*(?:\d+\.\s*)?What\s+would\s+change\s+my\s+mind\s*\n+(?P<body>.*?)(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# The Socratic memo's stance section — its head paragraph becomes the
# rationale_excerpt for the memo-sourced decisions row.
_STANCE_SECTION_RX = re.compile(
    r"##\s*Stance\s+if\s+forced\s*\n+(?P<body>.*?)(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# advisor_memos.stance vocabulary → decisions.recommendation_kind. 'buy' maps
# to 'add' because the Socratic flow runs on the owner's HOLDINGS — a "buy"
# stance on a held name is an add. The other four are shared vocabulary.
_STANCE_TO_KIND: dict[str, str] = {
    "buy": "add",
    "add": "add",
    "hold": "hold",
    "trim": "trim",
    "sell": "sell",
}


@dataclass(frozen=True, slots=True)
class DecisionCondition:
    """One validated falsifiable condition (see the module docstring schema).

    ``baseline_period_end`` is the breach-freshness watermark (alert-quality
    gates, 2026-07-15): the latest period_end already observed for the metric
    AT ATTACH TIME. The evaluator only push-fires on an observation strictly
    AFTER it — a condition that was already breached when it was written is
    displayed, never news. ``breached_at_attach`` records that already-breached
    state for owner-facing surfaces. Both are backward-compatible JSON:
    legacy stored conditions simply lack the keys.

    ``not_before`` (ISO date, 2026-07-16) is the milestone window: a
    FORWARD-LOOKING condition ("Base44 ARR reaches $200M in ~12 months")
    encodes a threshold that is trivially true/false TODAY — the tripwire
    only means something once the stated horizon has elapsed. The extractor
    sets it to decision date + the stated horizon; the evaluator skips the
    condition entirely while ``not_before`` is in the future (panels still
    display it). None (the default, and every legacy row) = evaluable now.
    """

    metric: str
    metric_source: str | None
    op: str
    threshold: float
    unit: str
    for_periods: int
    note: str | None
    not_before: str | None = None
    baseline_period_end: str | None = None
    breached_at_attach: bool = False

    def as_json_obj(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "metric_source": self.metric_source,
            "op": self.op,
            "threshold": self.threshold,
            "unit": self.unit,
            "for_periods": self.for_periods,
            "note": self.note,
            "not_before": self.not_before,
            "baseline_period_end": self.baseline_period_end,
            "breached_at_attach": self.breached_at_attach,
        }


@dataclass(frozen=True, slots=True)
class QualitativeCondition:
    """One non-numeric falsifiable condition — a phrase no reported number can
    evaluate, watched on a news/earnings surface instead.

    ``watch_for`` selects which trigger surfaces it ('news' | 'earnings' |
    'both'); ``phrase`` is the short canonical event ("the CEO departs"); ``note``
    quotes the source prose.
    """

    phrase: str
    watch_for: str
    note: str | None

    def as_json_obj(self) -> dict[str, object]:
        return {"phrase": self.phrase, "watch_for": self.watch_for, "note": self.note}


@dataclass(frozen=True, slots=True)
class OpenDecision:
    """A not-yet-graded decision with its parsed conditions — the evaluator
    rung's read shape. ``decided_by`` is None on a pre-0130 schema (column
    absent) — treated as advisor-authored by the push-lane relevance gate."""

    decision_id: int
    ticker: str
    recommendation_kind: str
    recommendation_value: float | None
    made_at: str
    source_lens: str | None
    conditions: tuple[DecisionCondition, ...]
    decided_by: str | None = None


# ===========================================================================
# Parsing + validation — pure functions
# ===========================================================================


def extract_section(content_md: str | None) -> str | None:
    """The "What would change my mind" section body, or None when absent."""
    if not content_md:
        return None
    match = _SECTION_RX.search(content_md)
    if match is None:
        return None
    body = match.group("body").strip()
    # An empty section directly followed by the next header can backtrack the
    # regex into capturing that header as the body — treat it as absent.
    if not body or body.startswith("##"):
        return None
    return body


def resolve_extraction_section(
    artifact_md: object, memo_md: object, owner_prose: object
) -> str | None:
    """The "what would change my mind" text for a decision, whatever its source.

    Artifact / Socratic-memo decisions carry a full memo, so the section is
    pulled by header (:func:`extract_section`). An owner-authored pass/avoid
    (L11, schema 0110) has no memo — its ``source_prose`` IS the falsifiability
    text, taken whole and capped to the extractors' budget. Precedence mirrors a
    decision's single source: a row has at most one of the three populated."""
    if isinstance(artifact_md, str) and artifact_md.strip():
        return extract_section(artifact_md)
    if isinstance(memo_md, str) and memo_md.strip():
        return extract_section(memo_md)
    if isinstance(owner_prose, str) and owner_prose.strip():
        return owner_prose.strip()[:4000]
    return None


def _decision_columns(conn: sqlite3.Connection) -> set[str]:
    """The ``decisions`` column set — lets the attach rungs select the
    L11 ``source_prose`` column only when the 0110 migration has landed."""
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    except sqlite3.Error:
        return set()


def parse_condition(raw: object) -> DecisionCondition | None:
    """Validate one extracted condition object. None (with a logged reason)
    when the entry is unusable — the caller drops it and keeps the rest."""
    if not isinstance(raw, dict):
        log.warning({"event": "decision_condition_invalid", "reason": "not an object"})
        return None
    obj = cast("dict[str, object]", raw)

    metric = obj.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        log.warning({"event": "decision_condition_invalid", "reason": "missing metric"})
        return None
    metric = metric.strip()

    source_raw = obj.get("metric_source")
    metric_source: str | None
    if source_raw is None:
        metric_source = None
    elif isinstance(source_raw, str) and source_raw in METRIC_SOURCES:
        metric_source = source_raw
    else:
        log.warning(
            {"event": "decision_condition_invalid", "metric": metric, "reason": "bad metric_source"}
        )
        return None

    op = obj.get("op")
    if not isinstance(op, str) or op not in CONDITION_OPS:
        log.warning({"event": "decision_condition_invalid", "metric": metric, "reason": "bad op"})
        return None

    threshold_raw = obj.get("threshold")
    if isinstance(threshold_raw, bool) or not isinstance(threshold_raw, (int, float)):
        log.warning(
            {"event": "decision_condition_invalid", "metric": metric, "reason": "bad threshold"}
        )
        return None

    unit = obj.get("unit")
    if not isinstance(unit, str) or unit not in _UNIT_VALUES:
        log.warning({"event": "decision_condition_invalid", "metric": metric, "reason": "bad unit"})
        return None

    periods_raw = obj.get("for_periods", 1)
    if (
        isinstance(periods_raw, bool)
        or not isinstance(periods_raw, int)
        or not (1 <= periods_raw <= 12)
    ):
        log.warning(
            {"event": "decision_condition_invalid", "metric": metric, "reason": "bad for_periods"}
        )
        return None

    note_raw = obj.get("note")
    note = note_raw.strip()[:300] if isinstance(note_raw, str) and note_raw.strip() else None

    # Milestone window (extractor-set on forward-looking conditions, absent on
    # legacy JSON). Tolerant like the watermark fields below: a malformed value
    # degrades to None (evaluable now) rather than rejecting the condition.
    not_before_raw = obj.get("not_before")
    not_before = (
        not_before_raw.strip()[:32]
        if isinstance(not_before_raw, str) and not_before_raw.strip()
        else None
    )

    # Watermark annotations (stamped by attach_conditions, absent on legacy
    # JSON and on fresh extractor output). Tolerant: a malformed value
    # degrades to the default rather than rejecting the whole condition.
    baseline_raw = obj.get("baseline_period_end")
    baseline = (
        baseline_raw.strip()[:32]
        if isinstance(baseline_raw, str) and baseline_raw.strip()
        else None
    )
    breached_at_attach = obj.get("breached_at_attach") is True

    return DecisionCondition(
        metric=metric[:200],
        metric_source=metric_source,
        op=op,
        threshold=float(threshold_raw),
        unit=unit,
        for_periods=periods_raw,
        note=note,
        not_before=not_before,
        baseline_period_end=baseline,
        breached_at_attach=breached_at_attach,
    )


def conditions_from_json(raw: str | None) -> tuple[DecisionCondition, ...]:
    """Decode a stored decision_conditions column back into typed conditions.
    Tolerant: a corrupt column yields () with a log line, never a crash —
    the evaluator must not die on one bad row."""
    if not raw:
        return ()
    try:
        decoded: object = json.loads(raw)
    except (ValueError, TypeError):
        log.warning({"event": "decision_conditions_column_corrupt", "head": str(raw)[:120]})
        return ()
    if not isinstance(decoded, list):
        log.warning({"event": "decision_conditions_column_corrupt", "head": str(raw)[:120]})
        return ()
    out: list[DecisionCondition] = []
    for entry in cast("list[object]", decoded):
        cond = parse_condition(entry)
        if cond is not None:
            out.append(cond)
    return tuple(out)


def parse_qualitative_condition(raw: object) -> QualitativeCondition | None:
    """Validate one extracted qualitative condition. None (logged) when unusable
    — the caller drops it and keeps the rest. ``watch_for`` defaults to 'both'
    when missing/invalid (surface on either trigger rather than dropping a real
    condition over a routing nit)."""
    if not isinstance(raw, dict):
        log.warning({"event": "qualitative_condition_invalid", "reason": "not an object"})
        return None
    obj = cast("dict[str, object]", raw)

    phrase = obj.get("phrase")
    if not isinstance(phrase, str) or not phrase.strip():
        log.warning({"event": "qualitative_condition_invalid", "reason": "missing phrase"})
        return None
    phrase = phrase.strip()

    watch_raw = obj.get("watch_for")
    watch_for = (
        watch_raw
        if isinstance(watch_raw, str) and watch_raw in QUALITATIVE_WATCH_SURFACES
        else "both"
    )

    note_raw = obj.get("note")
    note = note_raw.strip()[:300] if isinstance(note_raw, str) and note_raw.strip() else None

    return QualitativeCondition(phrase=phrase[:200], watch_for=watch_for, note=note)


def qualitative_conditions_from_json(raw: str | None) -> tuple[QualitativeCondition, ...]:
    """Decode a stored qualitative_conditions column back into typed conditions.
    Tolerant: a corrupt column yields () with a log line, never a crash."""
    if not raw:
        return ()
    try:
        decoded: object = json.loads(raw)
    except (ValueError, TypeError):
        log.warning({"event": "qualitative_conditions_column_corrupt", "head": str(raw)[:120]})
        return ()
    if not isinstance(decoded, list):
        log.warning({"event": "qualitative_conditions_column_corrupt", "head": str(raw)[:120]})
        return ()
    out: list[QualitativeCondition] = []
    for entry in cast("list[object]", decoded):
        cond = parse_qualitative_condition(entry)
        if cond is not None:
            out.append(cond)
    return tuple(out)


# ===========================================================================
# LLM extraction
# ===========================================================================

_EXTRACTION_PROMPT = """You are a strict JSON extractor for falsifiable investment conditions.

The markdown below is the "What would change my mind" section of an analyst
memo for {ticker}. The decision was made on {decision_date}. Extract every
FALSIFIABLE numeric condition — a metric, a direction, and a number that
future reported data can satisfy or not. Skip purely qualitative triggers
that carry no number (e.g. "a credible competitor enters the market").

Known metric names for {ticker}. When the prose refers to one of these, copy
the name EXACTLY as written here and set metric_source accordingly; when
neither list matches, keep a short name from the prose and set metric_source
to null.

KPI series (metric_source "kpi"):
{kpi_names}

Financial line items (metric_source "financial"):
{line_items}

Return ONLY a JSON array — no markdown fences, no commentary. Each element:

{{
  "metric": "<string>",
  "metric_source": "kpi" | "financial" | null,
  "op": "lt" | "le" | "gt" | "ge",
  "threshold": <number>,
  "unit": "actual" | "thousands" | "millions" | "billions" | "percent" | "ratio" | "bps" | "count",
  "for_periods": <integer >= 1>,
  "not_before": "<YYYY-MM-DD>" | null,
  "note": "<short quote of the source phrase>"
}}

Rules:
- "op" is the direction that SATISFIES (fires) the condition: "growth
  dropping below 25%" -> "lt"; "NPLs above 7%" -> "gt"; "at or above" -> "ge".
- Units: "25% YoY" -> threshold 25, unit "percent". "$500M" -> 500,
  "millions". "$1.2B" -> 1.2, "billions". "150bps" -> 150, "bps". A bare
  customer/user count -> "count".
- "for_periods": "for 2 consecutive quarters" -> 2. Unstated -> 1.
- "not_before": for a FORWARD-LOOKING MILESTONE — the prose states a future
  horizon for the condition ("in ~12 months", "by FY27", "within 2 quarters",
  "ARR reaches $200M in a year") — set not_before to the decision date
  ({decision_date}) plus the stated horizon, as YYYY-MM-DD ("by FY27"-style
  deadlines -> that fiscal year's end). The condition will NOT be evaluated
  before that date, so a "fails to reach X by then" milestone doesn't fire
  the day it is written. A condition with no stated future horizon (an
  ordinary "if the metric crosses X" tripwire) -> null.
- If the section contains no falsifiable numeric conditions, return [].

Section:
{section}
"""


def _format_vocab(names: list[str]) -> str:
    if not names:
        return "(none on file)"
    return "\n".join(f"- {n}" for n in names[:_MAX_VOCAB_ENTRIES])


def metric_vocabulary(conn: sqlite3.Connection, ticker: str) -> tuple[list[str], list[str]]:
    """The ticker's resolvable metric names: kpi_definitions names that carry
    facts, plus financial_facts line items. Missing tables degrade to empty
    (extraction still runs — every condition just lands unresolved)."""
    ticker = ticker.upper()
    kpi_names: list[str] = []
    line_items: list[str] = []
    try:
        rows = conn.execute(
            "SELECT DISTINCT kd.name FROM kpi_definitions kd "
            "JOIN kpi_facts kf ON kf.kpi_definition_id = kd.id "
            "WHERE kd.ticker = ? ORDER BY kd.name LIMIT ?",
            (ticker, _MAX_VOCAB_ENTRIES),
        ).fetchall()
        kpi_names = [str(r[0]) for r in rows]
    except sqlite3.Error:
        pass
    try:
        rows = conn.execute(
            "SELECT DISTINCT line_item FROM financial_facts "
            "WHERE ticker = ? ORDER BY line_item LIMIT ?",
            (ticker, _MAX_VOCAB_ENTRIES),
        ).fetchall()
        line_items = [str(r[0]) for r in rows]
    except sqlite3.Error:
        pass
    return kpi_names, line_items


def extract_conditions(
    section: str,
    *,
    ticker: str,
    kpi_names: list[str],
    line_items: list[str],
    made_at: str | None = None,
) -> list[DecisionCondition]:
    """One LLM pass over a "What would change my mind" section.

    ``made_at`` is the decision's timestamp — the anchor the model adds a
    stated horizon ("in ~12 months") to when setting ``not_before``. Optional
    (the eval harness calls without it): absent, the model is told the date
    is unknown and milestone conditions simply stay undated.

    Raises whatever ``call_llm_structured`` raises (hard stops propagate;
    ``StructuredParseError`` after the built-in retry) — the batch rung
    decides how to degrade. Individually invalid entries are dropped with a
    log line; the valid remainder survives.
    """
    decision_date = (made_at or "")[:10] or "unknown"
    prompt = _EXTRACTION_PROMPT.format(
        ticker=ticker.upper(),
        decision_date=decision_date,
        kpi_names=_format_vocab(kpi_names),
        line_items=_format_vocab(line_items),
        section=section[:4000],
    )
    payload = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        ticker=ticker.upper(),
        expect="array",
        schema=_DECISION_CONDITIONS_ADAPTER,
    )
    out: list[DecisionCondition] = []
    for entry in _DecisionConditionsWire.model_validate(payload).root:
        cond = parse_condition(entry.model_dump())
        if cond is not None:
            out.append(cond)
        if len(out) >= _MAX_CONDITIONS_PER_DECISION:
            break
    return out


_QUALITATIVE_PROMPT = """You are a strict JSON extractor for QUALITATIVE investment conditions.

The markdown below is the "What would change my mind" section of an analyst
memo for {ticker}. Extract the FALSIFIABLE NON-NUMERIC conditions — events or
state changes that future NEWS or an earnings call could confirm, that carry NO
number a financial statement could measure. These are exactly the triggers a
numeric extractor would skip: "the CEO departs", "a credible competitor enters
the market", "the FDA rejects the filing", "management's tone on margins turns
defensive", "they lose the anchor customer".

Do NOT extract numeric thresholds (e.g. "NPLs above 7%", "growth below 25%") —
those are handled separately. If a condition has both a qualitative event AND a
number, only extract it here if the EVENT is the falsifiable part.

For each condition decide where it would surface:
- "news"     — a discrete event that breaks as a news story (exec change,
               competitor/product launch, regulatory action, lawsuit, M&A).
- "earnings" — something only an earnings call / transcript would reveal
               (management tone, guidance language, qualitative commentary).
- "both"     — when either could surface it, or you are unsure.

Return ONLY a JSON array — no markdown fences, no commentary. Each element:

{{
  "phrase": "<short canonical event, <=120 chars>",
  "watch_for": "news" | "earnings" | "both",
  "note": "<short quote of the source phrase>"
}}

If the section contains no qualitative conditions, return [].

Section:
{section}
"""


def extract_qualitative_conditions(section: str, *, ticker: str) -> list[QualitativeCondition]:
    """One LLM pass extracting the NON-numeric falsifiable conditions.

    Raises whatever ``call_llm_structured`` raises (hard stops propagate;
    ``StructuredParseError`` after the built-in retry) — the batch rung decides
    how to degrade. Individually invalid entries are dropped with a log line.
    """
    prompt = _QUALITATIVE_PROMPT.format(ticker=ticker.upper(), section=section[:4000])
    payload = call_llm_structured(
        prompt,
        purpose=QUALITATIVE_PURPOSE,
        ticker=ticker.upper(),
        expect="array",
        schema=_QUALITATIVE_CONDITIONS_ADAPTER,
    )
    out: list[QualitativeCondition] = []
    for entry in _QualitativeConditionsWire.model_validate(payload).root:
        cond = parse_qualitative_condition(entry.model_dump())
        if cond is not None:
            out.append(cond)
        if len(out) >= _MAX_QUALITATIVE_PER_DECISION:
            break
    return out


# ===========================================================================
# DB rungs
# ===========================================================================


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    """Best-effort open, mirroring decision_extractor: None when the DB or
    the decisions table (with the 0086 columns) is unavailable."""
    try:
        path = Path(db_path)
        if not path.exists():
            return None
        conn = connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "decision_conditions" not in cols:
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _now_iso() -> str:
    # Naive-UTC, the repo-wide convention.
    return now_iso()


def record_socratic_decisions(
    *,
    db_path: Path | str,
    since_days: int = 90,
) -> dict[str, int]:
    """Create decisions rows for Socratic advisor memos that don't have one.

    Idempotent on ``source_memo_id`` (partial UNIQUE, 0086). Memos whose
    stance is NULL (shouldn't happen for kind='socratic', but the column
    allows it) are skipped. Returns a tally.
    """
    tally = {"inserted": 0, "skipped_existing": 0, "skipped_no_stance": 0, "db_unavailable": 0}
    conn = _open(db_path)
    if conn is None:
        tally["db_unavailable"] = 1
        return tally
    try:
        try:
            cutoff = (
                datetime.now(UTC).replace(tzinfo=None) - timedelta(days=since_days)
            ).isoformat()
            memos = conn.execute(
                "SELECT id, ticker, stance, body_md, created_at FROM advisor_memos "
                "WHERE kind = 'socratic' AND ticker IS NOT NULL AND created_at >= ? "
                "ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            tally["db_unavailable"] = 1
            return tally

        for memo in memos:
            stance = memo["stance"]
            kind = _STANCE_TO_KIND.get(str(stance or "").lower())
            if kind is None:
                tally["skipped_no_stance"] += 1
                continue
            existing = conn.execute(
                "SELECT 1 FROM decisions WHERE source_memo_id = ?", (int(memo["id"]),)
            ).fetchone()
            if existing is not None:
                tally["skipped_existing"] += 1
                continue
            rationale = _stance_excerpt(memo["body_md"])
            conn.execute(
                """
                INSERT INTO decisions(
                    ticker, recommendation_kind, recommendation_value, conviction,
                    source_artifact_id, source_memo_id, source_lens,
                    rationale_excerpt, made_at, outcome_label, created_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, 'advisor_socratic', ?, ?, 'pending', ?)
                """,
                (
                    str(memo["ticker"]).upper(),
                    kind,
                    int(memo["id"]),
                    rationale,
                    str(memo["created_at"]),
                    _now_iso(),
                ),
            )
            tally["inserted"] += 1
        conn.commit()
        return tally
    finally:
        conn.close()


def _stance_excerpt(body_md: object) -> str | None:
    """The Socratic memo's stance-if-forced paragraph, capped for the
    rationale_excerpt column. Falls back to the body head."""
    if not isinstance(body_md, str) or not body_md.strip():
        return None
    match = _STANCE_SECTION_RX.search(body_md)
    text = match.group("body").strip() if match else body_md.strip()
    return text[:512] or None


def fetch_financial_history(
    conn: sqlite3.Connection,
    ticker: str,
    line_item: str,
    n_periods: int,
) -> list[KpiObservation] | None:
    """financial_facts twin of ``compute.thesis_evaluator.fetch_kpi_observations``.

    Line items are already canonical (the extraction prompt copies them
    verbatim from the ticker's vocabulary), so no resolver pass. Coexisting
    rows per period dedup to the latest-ingested source (MAX(source_doc_id)),
    and one observation survives per period_end DATE — a same-date FY/Q4 pair
    collapses rather than double-counting a consecutive-periods window.
    Returns None when the line item has no rows at all (unresolvable),
    matching the kpi fetcher's no-definition signal. Shared by the
    decision-condition trigger (evaluation side) and the attach-time
    watermark stamping below, so the two can never read different histories.
    """
    from compute.thesis_evaluator import KpiObservation

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


def stamp_condition_baselines(
    conn: sqlite3.Connection,
    ticker: str,
    conditions: list[DecisionCondition],
) -> list[DecisionCondition]:
    """Stamp each freshly-extracted condition's breach-freshness watermark.

    ``baseline_period_end`` becomes the latest period_end ALREADY OBSERVED for
    the condition's metric at attach time; when the condition is already
    breached against that history, ``breached_at_attach`` is set too. The
    evaluator (``triggers.decision_condition``) then push-fires only on an
    observation strictly after the baseline — "the thing you said would change
    your mind just happened", never "here is data that predates your decision".

    Best-effort per condition: an unresolved metric, a missing table, or an
    invalid op/unit leaves the condition unstamped (baseline None — the
    evaluator's legacy freshness fallback governs it).
    """
    from compute.thesis_evaluator import (
        BreakRule,
        Comparator,
        evaluate_rule,
        fetch_kpi_observations,
    )
    from models.kpis import BreachStatus

    out: list[DecisionCondition] = []
    for cond in conditions:
        if cond.metric_source not in METRIC_SOURCES:
            out.append(cond)
            continue
        try:
            if cond.metric_source == "kpi":
                observations = fetch_kpi_observations(conn, ticker, cond.metric, cond.for_periods)
            else:
                observations = fetch_financial_history(conn, ticker, cond.metric, cond.for_periods)
        except sqlite3.Error:
            out.append(cond)
            continue
        if not observations:
            out.append(cond)
            continue
        latest = observations[0]  # fetchers return newest-first
        baseline = latest.period_end.date().isoformat()
        breached = False
        try:
            rule = BreakRule(
                rule_id=f"attach_baseline:{cond.metric[:64]}",
                kpi_name=cond.metric,
                comparator=Comparator(cond.op),
                threshold=Decimal(str(cond.threshold)),
                unit=Unit(cond.unit),
                consecutive_periods=cond.for_periods,
                narrative=(cond.note or cond.metric)[:1000],
            )
            evaluation = evaluate_rule(rule, observations)
            breached = evaluation.status is BreachStatus.BREACH
        except (ValueError, InvalidOperation):
            pass  # corrupt op/unit — baseline still worth keeping
        out.append(replace(cond, baseline_period_end=baseline, breached_at_attach=breached))
        if breached:
            log.info(
                {
                    "event": "decision_condition_breached_at_attach",
                    "ticker": ticker,
                    "metric": cond.metric,
                    "baseline_period_end": baseline,
                }
            )
    return out


def attach_conditions(
    *,
    db_path: Path | str,
    limit: int = 50,
) -> dict[str, int]:
    """Extract + attach conditions for decisions rows not yet processed.

    Walks ``conditions_extracted_at IS NULL`` rows oldest-first, resolves each
    row's source prose (lens artifact ``content_md`` via source_artifact_id,
    or Socratic memo ``body_md`` via source_memo_id), extracts, stamps.

    Per-row degradation: a missing source or absent section stamps the row
    with '[]' (done — re-running won't re-pay); a ``StructuredParseError``
    (bad JSON twice) or any other TRANSIENT LLM-call failure (CLI subprocess
    non-zero exit, timeout, both Claude + Gemini momentarily unavailable)
    leaves the row unstamped (retried next run) and is tallied. Either way the
    loop CONTINUES to the next decision — one flaky call must not deny every
    other decision its conditions for the day. LLM hard stops
    (``LLMBudgetExceeded`` / ``LLMSetupError``, see ``llm.cli.is_hard_stop``)
    still PROPAGATE — they are configuration problems, not extraction quality,
    and must halt the batch loudly (re-running won't help until fixed).
    """
    tally = {
        "extracted": 0,
        "no_conditions": 0,
        "no_section": 0,
        "parse_failed": 0,
        "deferred_transient": 0,
        "awaiting_ratification": 0,
        "db_unavailable": 0,
    }
    conn = _open(db_path)
    if conn is None:
        tally["db_unavailable"] = 1
        return tally
    try:
        cols = _decision_columns(conn)
        # Owner rows (0130) carry their falsifiability text ON the row: the
        # falsifier column IS the condition prose (the rationale in source_prose
        # is the WHY, not the tripwire — feeding it would extract spurious
        # conditions). Falsifier-first, source_prose as the L11 fallback.
        if "falsifier" in cols and "source_prose" in cols:
            prose_col = "COALESCE(NULLIF(TRIM(d.falsifier), ''), d.source_prose) AS owner_prose"
        elif "source_prose" in cols:
            prose_col = "d.source_prose AS owner_prose"
        else:
            prose_col = "NULL AS owner_prose"
        rows = conn.execute(
            f"""
            SELECT d.id, d.ticker, d.made_at, d.source_artifact_id, d.source_memo_id,
                   a.content_md AS artifact_md, m.body_md AS memo_md, {prose_col}
            FROM decisions d
            LEFT JOIN llm_artifacts a ON a.id = d.source_artifact_id
            LEFT JOIN advisor_memos m ON m.id = d.source_memo_id
            WHERE d.conditions_extracted_at IS NULL
            ORDER BY d.made_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        for row in rows:
            decision_id = int(row["id"])
            if row["ticker"] is None:
                # Portfolio-scope owner decision (0130): no per-ticker metric
                # vocabulary to resolve against — displayed, never evaluated.
                _stamp(conn, decision_id, [])
                conn.commit()
                tally["no_section"] += 1
                continue
            ticker = str(row["ticker"]).upper()
            owner_prose = row["owner_prose"]
            if isinstance(owner_prose, str) and "(inferred)" in owner_prose.lower():
                # An unratified falsifier (the interview wrote it, the owner
                # hasn't). Leave UNSTAMPED — zero LLM spend, retried after the
                # reconcile pass strips the marker. Coaching on words the owner
                # never said is the trust-destroying failure mode.
                tally["awaiting_ratification"] += 1
                continue
            section = resolve_extraction_section(row["artifact_md"], row["memo_md"], owner_prose)
            if section is None:
                _stamp(conn, decision_id, [])
                conn.commit()
                tally["no_section"] += 1
                continue

            kpi_names, line_items = metric_vocabulary(conn, ticker)
            made_at = str(row["made_at"]) if row["made_at"] is not None else None
            try:
                conditions = extract_conditions(
                    section,
                    ticker=ticker,
                    kpi_names=kpi_names,
                    line_items=line_items,
                    made_at=made_at,
                )
            except StructuredParseError as exc:
                log.error(
                    {
                        "event": "decision_conditions_extract_failed",
                        "decision_id": decision_id,
                        "ticker": ticker,
                        "error": str(exc),
                    }
                )
                tally["parse_failed"] += 1
                continue
            except Exception as exc:
                # Hard stops (budget cap / missing CLI) are configuration
                # problems, not this decision's extraction quality — they must
                # halt the WHOLE batch loudly (see llm.cli.is_hard_stop).
                # Everything else (subprocess non-zero exit, timeout, both
                # Claude + Gemini momentarily unavailable) is a TRANSIENT
                # per-call failure: leave this row unstamped (conditions_
                # extracted_at IS NULL keeps it in next run's queue) and move
                # on so one bad call can't deny every other decision that
                # day's condition extraction.
                if is_hard_stop(exc):
                    raise
                log.warning(
                    {
                        "event": "decision_conditions_extract_transient_failure",
                        "decision_id": decision_id,
                        "ticker": ticker,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
                tally["deferred_transient"] += 1
                continue
            # Breach-freshness watermark: record what was already observed for
            # each metric at attach time so an already-true condition can never
            # push-fire later as "news" (alert-quality gates, 2026-07-15).
            conditions = stamp_condition_baselines(conn, ticker, conditions)
            _stamp(conn, decision_id, conditions)
            # The LLM ledger writes through a separate connection. Release
            # this row's UPDATE before the next model call or every later
            # ledger write waits for SQLite's busy timeout.
            conn.commit()
            if conditions:
                tally["extracted"] += 1
            else:
                tally["no_conditions"] += 1
        conn.commit()
        return tally
    finally:
        conn.close()


def arming_status(decision_id: int, db_path: Path | str) -> str:
    """Zero-LLM read of one decision's tripwire-arming state, for the ratify
    route's inline receipt (consequence receipts PR).

    ``attach_conditions`` is the only path that STAMPS ``decision_conditions``,
    and it always calls the ``decision_conditions_extract`` LLM purpose for an
    unstamped row — real spend, and the wrong place to trigger it as a
    side-effect of a ratify click. So this is a READ, not a run: it reports
    whether the batch pass has already reached this decision and found at
    least one condition ('armed'), already reached it and found none
    ('no_conditions'), or hasn't reached it yet ('unextracted' — the ratify
    route's cue to ship "queued for arming" wording rather than pretend
    something ran). Best-effort like every other reader here: a missing DB/
    table/row degrades to 'unextracted' rather than raising — a ratify click
    must never 500 on this receipt lookup."""
    conn = _open(db_path)
    if conn is None:
        return "unextracted"
    try:
        row = conn.execute(
            "SELECT decision_conditions, conditions_extracted_at FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return "unextracted"
        if row["conditions_extracted_at"] is None:
            return "unextracted"
        conditions = conditions_from_json(row["decision_conditions"])
        return "armed" if conditions else "no_conditions"
    except sqlite3.Error:
        return "unextracted"
    finally:
        conn.close()


def _stamp(conn: sqlite3.Connection, decision_id: int, conditions: list[DecisionCondition]) -> None:
    conn.execute(
        "UPDATE decisions SET decision_conditions = ?, conditions_extracted_at = ? WHERE id = ?",
        (
            json.dumps([c.as_json_obj() for c in conditions], ensure_ascii=False),
            _now_iso(),
            decision_id,
        ),
    )


def _has_qualitative_column(conn: sqlite3.Connection) -> bool:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    except sqlite3.Error:
        return False
    return "qualitative_conditions" in cols


def attach_qualitative_conditions(
    *,
    db_path: Path | str,
    limit: int = 50,
) -> dict[str, int]:
    """Extract + attach QUALITATIVE conditions for decisions not yet processed.

    The non-numeric twin of ``attach_conditions``: walks
    ``qualitative_conditions_extracted_at IS NULL`` rows oldest-first over the
    SAME "What would change my mind" prose, runs the qualitative pass, stamps the
    separate column. Independent of the numeric stamp, so the two passes never
    block each other. Same degradation contract as ``attach_conditions``:
    missing source / no section → stamped '[]'; a ``StructuredParseError`` or
    any other TRANSIENT LLM-call failure → unstamped, retried next run, loop
    continues to the next decision; LLM hard stops (``is_hard_stop``)
    propagate and halt the batch loudly. No-op on a pre-0108 DB (the column is
    absent).
    """
    tally = {
        "extracted": 0,
        "no_conditions": 0,
        "no_section": 0,
        "parse_failed": 0,
        "deferred_transient": 0,
        "db_unavailable": 0,
    }
    conn = _open(db_path)
    if conn is None or not _has_qualitative_column(conn):
        tally["db_unavailable"] = 1
        if conn is not None:
            conn.close()
        return tally
    try:
        prose_col = (
            "d.source_prose AS owner_prose"
            if "source_prose" in _decision_columns(conn)
            else "NULL AS owner_prose"
        )
        rows = conn.execute(
            f"""
            SELECT d.id, d.ticker, d.source_artifact_id, d.source_memo_id,
                   a.content_md AS artifact_md, m.body_md AS memo_md, {prose_col}
            FROM decisions d
            LEFT JOIN llm_artifacts a ON a.id = d.source_artifact_id
            LEFT JOIN advisor_memos m ON m.id = d.source_memo_id
            WHERE d.qualitative_conditions_extracted_at IS NULL
            ORDER BY d.made_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        for row in rows:
            decision_id = int(row["id"])
            ticker = str(row["ticker"]).upper()
            section = resolve_extraction_section(
                row["artifact_md"], row["memo_md"], row["owner_prose"]
            )
            if section is None:
                _stamp_qualitative(conn, decision_id, [])
                conn.commit()
                tally["no_section"] += 1
                continue
            try:
                conditions = extract_qualitative_conditions(section, ticker=ticker)
            except StructuredParseError as exc:
                log.error(
                    {
                        "event": "qualitative_conditions_extract_failed",
                        "decision_id": decision_id,
                        "ticker": ticker,
                        "error": str(exc),
                    }
                )
                tally["parse_failed"] += 1
                continue
            except Exception as exc:
                # Same hard-stop-vs-transient split as attach_conditions: a
                # budget cap / missing CLI must halt the whole batch loudly;
                # everything else (subprocess non-zero exit, timeout, both
                # backends momentarily down) leaves this row unstamped
                # (retried next run) and the loop moves to the next decision.
                if is_hard_stop(exc):
                    raise
                log.warning(
                    {
                        "event": "qualitative_conditions_extract_transient_failure",
                        "decision_id": decision_id,
                        "ticker": ticker,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
                tally["deferred_transient"] += 1
                continue
            _stamp_qualitative(conn, decision_id, conditions)
            # The next model call must be able to ledger through its separate
            # writer connection without waiting for this loop transaction.
            conn.commit()
            if conditions:
                tally["extracted"] += 1
            else:
                tally["no_conditions"] += 1
        conn.commit()
        return tally
    finally:
        conn.close()


def _stamp_qualitative(
    conn: sqlite3.Connection, decision_id: int, conditions: list[QualitativeCondition]
) -> None:
    conn.execute(
        "UPDATE decisions SET qualitative_conditions = ?, "
        "qualitative_conditions_extracted_at = ? WHERE id = ?",
        (
            json.dumps([c.as_json_obj() for c in conditions], ensure_ascii=False),
            _now_iso(),
            decision_id,
        ),
    )


def load_open_decisions(conn: sqlite3.Connection, ticker: str) -> list[OpenDecision]:
    """Decisions carrying at least one condition that the evaluator rung should
    still check. Two lifetimes (0130):

    - an ADVISOR recommendation's conditions retire once the decision is
      graded (``outcome_at`` set) — the call has been scored, the window closed;
    - an OWNER decision's falsifier guards the POSITION, not the past trade —
      it stays evaluable after grading for as long as the name is still a
      portfolio holding (the daily list_type reconciler keeps that current).

    Tolerant of the pre-0086/pre-0130 schema (returns [] / falls back)."""
    owner_alive = ""
    decided_by_col = "NULL AS decided_by"
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "decided_by" in cols:
            decided_by_col = "d.decided_by"
            owner_alive = (
                " OR (d.decided_by = 'owner' AND EXISTS ("
                "SELECT 1 FROM tracked_companies t WHERE t.ticker = d.ticker"
                " AND t.list_type = 'portfolio'))"
            )
    except sqlite3.Error:
        owner_alive = ""
    try:
        rows = conn.execute(
            f"""
            SELECT d.id, d.ticker, d.recommendation_kind, d.recommendation_value,
                   d.made_at, d.source_lens, d.decision_conditions, {decided_by_col}
            FROM decisions d
            WHERE d.ticker = ? AND (d.outcome_at IS NULL{owner_alive})
              AND d.decision_conditions IS NOT NULL AND d.decision_conditions != '[]'
            ORDER BY d.made_at DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[OpenDecision] = []
    for row in rows:
        conditions = conditions_from_json(row["decision_conditions"])
        if not conditions:
            continue
        value = row["recommendation_value"]
        out.append(
            OpenDecision(
                decision_id=int(row["id"]),
                ticker=str(row["ticker"]),
                recommendation_kind=str(row["recommendation_kind"]),
                recommendation_value=float(value) if value is not None else None,
                made_at=str(row["made_at"]),
                source_lens=(str(row["source_lens"]) if row["source_lens"] is not None else None),
                conditions=conditions,
                decided_by=(str(row["decided_by"]) if row["decided_by"] is not None else None),
            )
        )
    return out


def load_all_open_decisions(db_path: Path | str) -> list[OpenDecision]:
    """Every open decision across every ticker that carries at least one
    armed condition — the Ledger panel's "Armed falsifiers" table reads this,
    the SAME set ``DecisionConditionTrigger.scan`` evaluates (per-ticker, via
    :func:`load_open_decisions`): tickers are enumerated the identical way
    ``standup.signals._decision_condition_signals`` does (distinct
    ``decisions.ticker`` with a non-empty ``decision_conditions``), then each
    ticker's rows are pulled through the real per-ticker reader — never a
    flat cross-ticker query — so this table can never show a row the trigger
    itself would skip. Best-effort: a missing DB/table degrades to []."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        try:
            # No outcome_at filter here — a graded OWNER decision can still be
            # open (the falsifier guards the position, not the trade); the
            # per-ticker load_open_decisions call below re-applies the real
            # (advisor-vs-owner) lifetime rule per row, so this pre-filter
            # only needs to find candidate TICKERS, never candidate rows.
            tickers = [
                str(r["ticker"]).upper()
                for r in conn.execute(
                    "SELECT DISTINCT ticker FROM decisions "
                    "WHERE ticker IS NOT NULL "
                    "  AND decision_conditions IS NOT NULL AND decision_conditions != '[]'"
                ).fetchall()
                if r["ticker"]
            ]
        except sqlite3.Error:
            return []
        out: list[OpenDecision] = []
        for ticker in tickers:
            out.extend(load_open_decisions(conn, ticker))
        out.sort(key=lambda d: d.made_at, reverse=True)
        return out
    finally:
        conn.close()


# ===========================================================================
# Qualitative read side — the news/earnings-tone trigger bridge
# ===========================================================================


@dataclass(frozen=True, slots=True)
class OpenQualitativeCondition:
    """A qualitative condition on an open decision, the shape the material_news /
    earnings_tone triggers surface as an alert annotation."""

    decision_id: int
    decision_summary: str
    phrase: str
    watch_for: str
    note: str | None

    def as_payload(self) -> dict[str, object]:
        """The serializable form for an alert's evidence_json."""
        return {
            "decision_id": self.decision_id,
            "decision_summary": self.decision_summary,
            "phrase": self.phrase,
            "note": self.note,
        }


def _decision_summary(made_at: object, kind: object, value: object, source_lens: object) -> str:
    made_on = str(made_at or "")[:10]
    kind_label = str(kind or "").upper()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        kind_label += f" {value:g}%"
    source = str(source_lens) if source_lens else "memo"
    return f"the {made_on} {kind_label} decision ({source})"


def load_open_qualitative_conditions(
    conn: sqlite3.Connection, ticker: str, *, surface: str
) -> list[OpenQualitativeCondition]:
    """Open (ungraded) decisions' qualitative conditions for ``ticker`` whose
    ``watch_for`` matches the firing ``surface`` ('news' or 'earnings'; 'both'
    always matches). Tolerant of the pre-0108 schema (returns [])."""
    matches = {surface, "both"}
    conn.row_factory = sqlite3.Row  # the trigger's connection may not have set it
    try:
        rows = conn.execute(
            """
            SELECT id, recommendation_kind, recommendation_value, made_at, source_lens,
                   qualitative_conditions
            FROM decisions
            WHERE ticker = ? AND outcome_at IS NULL
              AND qualitative_conditions IS NOT NULL AND qualitative_conditions != '[]'
            ORDER BY made_at DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[OpenQualitativeCondition] = []
    for row in rows:
        summary = _decision_summary(
            row["made_at"],
            row["recommendation_kind"],
            row["recommendation_value"],
            row["source_lens"],
        )
        for cond in qualitative_conditions_from_json(row["qualitative_conditions"]):
            if cond.watch_for not in matches:
                continue
            out.append(
                OpenQualitativeCondition(
                    decision_id=int(row["id"]),
                    decision_summary=summary,
                    phrase=cond.phrase,
                    watch_for=cond.watch_for,
                    note=cond.note,
                )
            )
    return out


def overlay_payloads(conds: list[OpenQualitativeCondition]) -> list[dict[str, object]]:
    """The serializable overlay a trigger stores in a candidate's evidence."""
    return [c.as_payload() for c in conds]


def render_qualitative_overlay(payloads: list[dict[str, object]]) -> str:
    """The one-line annotation a trigger appends to its memo when a held name's
    qualitative conditions may bear on the firing event — rendered from the
    evidence payloads ``build_alert`` already carries. Empty when none."""
    phrases = [str(p.get("phrase")) for p in payloads if p.get("phrase")]
    if not phrases:
        return ""
    shown = "; ".join(f'"{p}"' for p in phrases[:3])
    more = f" (+{len(phrases) - 3} more)" if len(phrases) > 3 else ""
    return (
        f" ⚠ May bear on a 'what would change my mind' condition you set: {shown}{more} "
        "— review whether this development satisfies it."
    )
