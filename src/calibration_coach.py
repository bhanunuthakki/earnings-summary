"""Calibration coach (Close-the-Loops L8) — the compounding loop's voice.

PR1 built the deterministic substrate: period-over-period cohorts (am I getting
better?) and the shared selection/sizing/timing attribution engine (which
repeatable decision is my edge vs my leak). This module turns that substrate
into COACHING — the three things a measurement panel can't do on its own:

  named biases      — 2-3 NAMED recurring mistakes ("you reverse winners
      early", "overconfident on high-multiple adds"), each evidenced ONLY by
      the owner's own decisions / cohorts / closed-position lessons. Grounded,
      not generic: the directive's whole point is to name HIS patterns, not a
      textbook's.
  pre-mortem        — at the decision moment (a fresh add/initiate/pass), "the
      3 ways this most resembles bets you got wrong" + auto-drafted candidate
      decision_conditions calibrated to documented blind spots. Injected
      alongside L1's calibration block in the Socratic flow.
  behavioral experiment — one falsifiable behavioural experiment per period,
      written back as a tracked-and-graded decision_condition, so the
      META-decisions ("size up my high-conviction winners") get the same
      falsifiable -> graded loop the stock calls already have.

Everything is grounded in the owner's OWN history (decisions, lessons,
outcome_vs_thesis, the PR1 cohorts + attribution) — never a general market
view. Two non-negotiable guardrails:

  * the SHARED minimum-n guard (calibration_guard): a coach that confronts the
    owner from n=3 graded calls is noise wearing authority. Below the floor we
    do not call the LLM at all — the scorecard renders the substrate with an
    honest "too thin to coach yet" note.
  * an EVAL GATE (L3's rubric pattern): the synthesised prose is judged against
    evals/rubrics/calibration_coach.md before it ever reaches the owner. A
    coach that misreads his history is worse than silence — below threshold,
    the LLM prose is suppressed and only the deterministic substrate shows.

LLM transport rides the canonical seams (call_llm / call_llm_structured) so the
purpose enters the model-downgrade ledger and tests monkeypatch one place. Hard
stops (budget/setup) propagate; transient failures degrade to "no coach output
this period" — never a fabricated bias.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from attribution import SkillDecomposition, decompose_alpha
from calibration_guard import confidence_note, is_confident
from decision_calibration import (
    CalibrationStats,
    ConvictionBucket,
    build_calibration,
    omission_clause,
)
from decision_conditions import DecisionCondition, parse_condition
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import fetch_portfolio_analytics
from llm.cli import call_llm, is_hard_stop
from llm.structured import StructuredParseError, call_llm_structured
from position_lifecycle import list_entries
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# Generation purpose: the coach's reasoning over the owner's own track record —
# judgement-heavy (naming HIS recurring biases, not a textbook's), low volume
# (monthly + on a decision), off the hot path. Opus tier; pinned in LLM_MODELS.
COACH_PURPOSE = "calibration_coach"

# Below this many GRADED decisions the coach stays silent (the shared floor).
# The owner is never confronted, nor an experiment proposed, from a denominator
# too thin to carry a pattern.
MIN_COACH_GRADED = 10  # == calibration_guard.MIN_CONFIDENT_N

_MAX_BIASES = 3
_MIN_BIASES = 2
_MAX_CLOSED_LESSONS = 12
_MAX_EVIDENCE = 4

_SCORECARD_DIRNAME = "calibration_scorecard"


# ===========================================================================
# Grounding inputs (deterministic — the owner's own history, no LLM)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ClosedLesson:
    """One closed position's post-mortem grading — the richest first-person
    signal the coach has (the owner's own words on what a name taught)."""

    ticker: str
    entry_conviction: str | None
    outcome_vs_thesis: str | None  # played_out | broke | mixed | unrelated
    exit_reason: str | None
    lessons: str | None


@dataclass(frozen=True, slots=True)
class CoachInputs:
    """Everything the coach grounds in, fetched once. All first-person: the
    owner's graded calls, his conviction calibration, his realized skill split,
    and his closed-position lessons. ``can_coach`` gates the LLM entirely."""

    calibration: CalibrationStats | None
    skill: SkillDecomposition | None
    closed_lessons: list[ClosedLesson]
    n_graded: int

    @property
    def can_coach(self) -> bool:
        """The shared min-n gate: enough graded history to name a pattern."""
        return is_confident(self.n_graded, min_n=MIN_COACH_GRADED)


def _closed_lessons(db_path: Path, *, user_id: str) -> list[ClosedLesson]:
    """Closed lifecycle rows that carry the owner's grading — newest first,
    capped. A closed row with neither a lesson nor an outcome is silent on what
    it taught, so it is skipped (no signal to ground a bias in)."""
    out: list[ClosedLesson] = []
    for e in list_entries(db_path=db_path, user_id=user_id, limit=200):
        if e.is_open:
            continue
        if not (e.lessons or e.outcome_vs_thesis or e.exit_reason):
            continue
        out.append(
            ClosedLesson(
                ticker=e.ticker.upper(),
                entry_conviction=e.entry_conviction,
                outcome_vs_thesis=e.outcome_vs_thesis,
                exit_reason=e.exit_reason,
                lessons=e.lessons,
            )
        )
        if len(out) >= _MAX_CLOSED_LESSONS:
            break
    return out


def gather_coach_inputs(
    repo_root: Path | str,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
) -> CoachInputs:
    """Assemble the coach's grounding from the owner's ledgers + the tracker.

    Best-effort throughout (the established posture): a missing decisions table
    yields ``calibration=None`` and ``n_graded=0`` (can_coach False); an offline
    tracker yields ``skill=None`` (the coach reasons on calibration alone).

    The realized-skill decomposition needs a tracker round-trip, so it is only
    fetched when the graded ledger is thick enough to coach — a thin ledger
    produces no coaching either way, and the decision-moment pre-mortem must not
    pay a network hit on every call just to be gated out a line later."""
    root = Path(repo_root)
    db_path = root / "data" / "portfolio.db"
    calibration = build_calibration(db_path=db_path)
    n_graded = calibration.graded if calibration is not None else 0
    closed = _closed_lessons(db_path, user_id=user_id)
    skill = (
        _skill_decomposition(db_path, api_url=api_url)
        if is_confident(n_graded, min_n=MIN_COACH_GRADED)
        else None
    )
    return CoachInputs(
        calibration=calibration,
        skill=skill,
        closed_lessons=closed,
        n_graded=n_graded,
    )


def _skill_decomposition(db_path: Path, *, api_url: str | None) -> SkillDecomposition | None:
    """The realized selection/sizing/timing split, conviction-joined — reuses
    PR1's shared engine. None when the tracker is offline / nothing priced."""
    analytics = fetch_portfolio_analytics(api_url=api_url, only={"position_alpha"})
    if not analytics.available or analytics.position_alpha is None:
        return None
    conviction_by_ticker = _conviction_map(db_path)
    return decompose_alpha(analytics.position_alpha, conviction_by_ticker=conviction_by_ticker)


# The position_sizing_intent.intent_kind that records stated conviction (1-5) —
# the same key the sizing audit reads. Hard-coded (not imported from the panel)
# so this module never depends on the decisions panel, which depends on it.
_CONVICTION_INTENT_KIND = "conviction"


def _conviction_map(db_path: Path) -> dict[str, float]:
    """ticker -> latest stated conviction (1-5), read straight from the sizing
    intents the owner recorded — the same number the panel feeds decompose_alpha,
    but WITHOUT importing the panel (which imports this module — that would be a
    cycle). Best-effort: any failure -> empty map (the decomposition lands every
    name in the 'unstated' bucket)."""
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, intent_value FROM position_sizing_intent i "
            "WHERE intent_kind = ? AND intent_value IS NOT NULL AND created_at = ("
            "  SELECT MAX(created_at) FROM position_sizing_intent j "
            "  WHERE j.ticker = i.ticker AND j.intent_kind = i.intent_kind"
            ")",
            (_CONVICTION_INTENT_KIND,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, float] = {}
    for row in rows:
        try:
            out[str(row["ticker"]).upper()] = float(row["intent_value"])
        except (TypeError, ValueError):
            continue
    return out


# ===========================================================================
# Grounding block (the prompt's evidence — the owner's own numbers)
# ===========================================================================


def _conviction_line(b: ConvictionBucket) -> str:
    rate = f"{b.hit_rate * 100:.0f}%" if b.hit_rate is not None else "n/a"
    return f"{b.conviction}: {rate} correct ({b.correct}/{b.graded} graded, {b.ungraded} ungraded)"


def grounding_block(inputs: CoachInputs) -> str:
    """The owner's own track record, rendered as the prompt's evidence. Every
    line is a queryable fact — the coach must build its biases out of THESE, not
    invent a generic pattern. Kept compact (it rides Opus prompts)."""
    cal = inputs.calibration
    lines: list[str] = []
    if cal is not None:
        overall = (
            f"{cal.overall_hit_rate * 100:.0f}%" if cal.overall_hit_rate is not None else "n/a"
        )
        lines.append(
            f"OVERALL: {cal.graded} graded calls, {overall} correct (of {cal.total} logged)."
        )
        if cal.by_conviction:
            lines.append(
                "BY CONVICTION — " + "; ".join(_conviction_line(b) for b in cal.by_conviction)
            )
        if cal.cohorts:
            trend = "; ".join(
                f"{c.period} {f'{c.hit_rate * 100:.0f}%' if c.hit_rate is not None else 'n/a'}"
                f" ({c.graded} graded)"
                for c in cal.cohorts
            )
            lines.append(f"TREND BY {cal.cohort_granularity.upper()} — {trend}")
            if cal.hit_rate_delta is not None:
                direction = "improving" if cal.improving else "slipping/flat"
                lines.append(
                    f"LATEST DIRECTION — {direction} ({cal.hit_rate_delta * 100:+.0f}pp "
                    "vs prior period)"
                )
        if cal.reversals_vindicated or cal.reversals_cost:
            lines.append(
                f"REVERSALS — {cal.reversals_vindicated} vindicated (override was right) vs "
                f"{cal.reversals_cost} cost (you overrode a call that graded correct)"
            )
        om_clause = omission_clause(cal.omissions)
        if om_clause is not None:
            # The omission side (L11): how the names he PASSED on graded. Names a
            # "passes winners" bias in HIS own avoided tickers, not a textbook's.
            lines.append("OMISSIONS — " + om_clause)
    skill = inputs.skill
    if skill is not None:

        def _m(v: float | None) -> str:
            return f"${v:+,.0f}" if v is not None else "n/a"

        lines.append(
            f"REALIZED SKILL (window {skill.window_start} -> {skill.window_end}): "
            f"selection {_m(skill.selection_usd)}, sizing {_m(skill.sizing_usd)}, "
            f"timing {_m(skill.timing_usd)} (n={skill.n_names} priced names)."
        )
        for c in skill.by_conviction:
            lines.append(
                f"  conviction {c.conviction}: alpha {_m(c.alpha_usd)}, "
                f"sizing contribution {_m(c.sizing_usd)} ({c.n} names)"
            )
    if inputs.closed_lessons:
        lines.append("CLOSED POSITIONS (your own grading):")
        for cl in inputs.closed_lessons:
            bits = [cl.ticker]
            if cl.entry_conviction:
                bits.append(f"entered {cl.entry_conviction} conviction")
            if cl.outcome_vs_thesis:
                bits.append(f"outcome: thesis {cl.outcome_vs_thesis}")
            head = " · ".join(bits)
            tail = " ".join(p for p in (cl.exit_reason, cl.lessons) if p).strip()
            lines.append(f"  - {head}" + (f" — {tail}" if tail else ""))
    return "\n".join(lines) if lines else "(no graded history)"


# ===========================================================================
# Named biases (LLM, grounded + min-n gated)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class NamedBias:
    """One named recurring mistake, evidenced by the owner's own history."""

    name: str  # short label, e.g. "reverses winners early"
    pattern: str  # 1-2 sentences describing the recurring behaviour
    evidence: list[str]  # the owner's own facts this is built from
    tell: str  # the observable signal that he's doing it again


_BIAS_PROMPT = """You are the owner's calibration coach. From his OWN track record below —
graded decisions, conviction calibration, realized selection/sizing/timing
skill, and his own closed-position lessons — name the {min_b}-{max_b} most
important RECURRING biases in his investing behaviour.

A bias here is a repeatable mistake the EVIDENCE supports, named in his terms
(e.g. "reverses winners early", "oversizes high-multiple adds", "high
conviction doesn't earn its label"). Do NOT invent generic market wisdom — every
bias must be built from the specific facts below, and you must cite them.

HIS TRACK RECORD:
{grounding}

Rules:
- Ground EVERY bias in the evidence above. If the evidence does not support a
  recurring pattern, return fewer biases — never pad to {max_b}.
- "evidence" is a list of short strings quoting the specific facts (a conviction
  hit-rate, a reversal count, a named closed position's lesson) the bias rests on.
- "tell" is the observable, checkable signal that he is repeating it RIGHT NOW
  (e.g. "an add on a name already above your median weight at 5/5 conviction").
- Be terse and PM-to-PM. No preamble.

Return ONLY a JSON object, no fences, no prose:
{{"biases": [{{"name": "<short label>", "pattern": "<1-2 sentences>",
  "evidence": ["<fact>", ...], "tell": "<observable signal>"}}]}}"""


def _parse_bias(raw: object) -> NamedBias | None:
    if not isinstance(raw, dict):
        return None
    obj = cast("dict[str, object]", raw)
    name = obj.get("name")
    pattern = obj.get("pattern")
    tell = obj.get("tell")
    evidence_raw = obj.get("evidence")
    if not (isinstance(name, str) and name.strip()):
        return None
    if not (isinstance(pattern, str) and pattern.strip()):
        return None
    evidence = (
        [str(e).strip() for e in cast("list[object]", evidence_raw) if str(e).strip()][
            :_MAX_EVIDENCE
        ]
        if isinstance(evidence_raw, list)
        else []
    )
    if not evidence:  # a bias with no cited evidence is exactly the failure mode we reject
        return None
    return NamedBias(
        name=name.strip()[:80],
        pattern=" ".join(pattern.split())[:400],
        evidence=evidence,
        tell=(" ".join(tell.split())[:300] if isinstance(tell, str) and tell.strip() else ""),
    )


class TransientCoachError(RuntimeError):
    """The coach's LLM leg failed TRANSIENTLY (CLI error, quota-dead window,
    parse miss) — the scorecard must NOT be persisted this run.

    2026-07 incident (program review 2026-07-19): the one scorecard ever
    generated hit a quota-exhausted window, ``synthesize_biases`` degraded the
    ``CalledProcessError`` to ``[]``, and the card persisted a confident "No
    biases met the grounding bar" for the month — while the ledger held a 33%
    high-conviction hit-rate. A transient failure is a DEFERRAL (quota rule 3:
    defer + tally + retry next run), never a finding."""


def synthesize_biases(inputs: CoachInputs) -> list[NamedBias]:
    """The owner's 2-3 named recurring biases. Returns [] (no LLM call) when the
    graded denominator is below the shared floor — a thin ledger can't carry a
    pattern. Transient LLM/parse failures raise ``TransientCoachError`` (the
    caller defers the whole scorecard rather than persisting a false "no
    biases"); hard stops propagate as themselves. Tests monkeypatch
    ``calibration_coach.call_llm_structured``."""
    if not inputs.can_coach:
        return []
    prompt = _BIAS_PROMPT.format(
        min_b=_MIN_BIASES, max_b=_MAX_BIASES, grounding=grounding_block(inputs)
    )
    try:
        payload = call_llm_structured(prompt, purpose=COACH_PURPOSE, expect="object")
    except StructuredParseError as exc:
        log.warning({"event": "coach_biases_parse_failed", "error": str(exc)})
        raise TransientCoachError(f"bias synthesis parse miss: {exc}") from exc
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "coach_biases_llm_failed", "error": f"{type(exc).__name__}: {exc}"})
        raise TransientCoachError(f"bias synthesis LLM failure: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        return []
    biases_raw = cast("dict[str, object]", payload).get("biases")
    if not isinstance(biases_raw, list):
        return []
    out: list[NamedBias] = []
    for entry in cast("list[object]", biases_raw):
        bias = _parse_bias(entry)
        if bias is not None:
            out.append(bias)
        if len(out) >= _MAX_BIASES:
            break
    return out


# ===========================================================================
# Behavioral experiment (LLM) — a falsifiable meta-decision for the period
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BehavioralExperiment:
    """One falsifiable behavioural experiment for the period — a meta-decision
    the owner can run and have graded like any stock call. ``condition`` is the
    falsifiable test (a tracked decision_condition); ``hypothesis`` is the
    behaviour change it tests."""

    hypothesis: str
    rationale: str
    condition: DecisionCondition | None  # the falsifiable, gradeable test


_EXPERIMENT_PROMPT = """You are the owner's calibration coach. From his track record below, propose
ONE falsifiable behavioural EXPERIMENT for the coming period — a single change
to how he DECIDES (not a stock pick), chosen to attack his biggest documented
weakness, and written so it can be graded later like any other call.

HIS TRACK RECORD:
{grounding}

THE BIASES JUST IDENTIFIED:
{biases}

The experiment must be:
- behavioural (about his process: sizing discipline, reversal discipline,
  conviction hygiene) — NOT "buy/sell ticker X";
- falsifiable: it ends in a numeric condition future data can satisfy or not;
- attached to his biggest leak from the evidence above.

Return ONLY a JSON object, no fences:
{{"hypothesis": "<the behaviour change being tested, one sentence>",
  "rationale": "<why this, grounded in his evidence, one sentence>",
  "condition": {{"metric": "<short name>", "metric_source": null,
    "op": "lt"|"le"|"gt"|"ge", "threshold": <number>,
    "unit": "actual"|"percent"|"ratio"|"count"|"bps", "for_periods": <int>=1>,
    "note": "<the falsifiable test in plain words>"}}}}"""


def propose_experiment(inputs: CoachInputs, biases: list[NamedBias]) -> BehavioralExperiment | None:
    """One falsifiable behavioural experiment for the period. None when the
    ledger is too thin to coach. Transient LLM/parse failures raise
    ``TransientCoachError`` (same defer-don't-persist contract as
    ``synthesize_biases``); hard stops propagate as themselves. The condition
    is validated through decision_conditions' parser so a malformed test
    degrades to a hypothesis with no gradeable condition rather than a crash.
    Tests monkeypatch ``call_llm_structured``."""
    if not inputs.can_coach:
        return None
    biases_text = (
        "\n".join(f"- {b.name}: {b.pattern}" for b in biases) if biases else "(none named)"
    )
    prompt = _EXPERIMENT_PROMPT.format(grounding=grounding_block(inputs), biases=biases_text)
    try:
        payload = call_llm_structured(prompt, purpose=COACH_PURPOSE, expect="object")
    except StructuredParseError as exc:
        log.warning({"event": "coach_experiment_parse_failed", "error": str(exc)})
        raise TransientCoachError(f"experiment parse miss: {exc}") from exc
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "coach_experiment_llm_failed", "error": str(exc)})
        raise TransientCoachError(f"experiment LLM failure: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        return None
    obj = cast("dict[str, object]", payload)
    hypothesis = obj.get("hypothesis")
    rationale = obj.get("rationale")
    if not (isinstance(hypothesis, str) and hypothesis.strip()):
        return None
    condition = parse_condition(obj.get("condition"))
    return BehavioralExperiment(
        hypothesis=" ".join(hypothesis.split())[:300],
        rationale=(
            " ".join(rationale.split())[:300]
            if isinstance(rationale, str) and rationale.strip()
            else ""
        ),
        condition=condition,
    )


# ===========================================================================
# Pre-mortem from your own history (L8 item d) — at the decision moment
# ===========================================================================

_MAX_RESEMBLANCES = 3
_MAX_DRAFT_CONDITIONS = 3
# Closed-position outcomes that count as a loss/miss worth resembling against.
_LOSS_OUTCOMES: frozenset[str] = frozenset({"broke", "mixed"})


@dataclass(frozen=True, slots=True)
class Premortem:
    """The decision-moment pre-mortem for one name: the ways this stance most
    resembles bets the owner got wrong, plus candidate falsifiable conditions
    calibrated to his documented blind spots. Grounded ONLY in his own losses +
    calibration — never a generic risk list."""

    ticker: str
    stance: str | None  # the call being weighed (add | initiate | trim | ...)
    resemblances: list[str]  # "the N ways this resembles bets you got wrong"
    drafted_conditions: list[DecisionCondition]  # candidate conditions to commit to


_PREMORTEM_PROMPT = """You are the owner's calibration coach, called at the DECISION MOMENT: he is
about to {stance_phrase} {ticker}. Before he commits, run a pre-mortem grounded
ONLY in his OWN documented losses and calibration below — name the ways THIS bet
most resembles ones he got wrong, and draft the falsifiable conditions that would
catch the failure early.

HIS TRACK RECORD AND PAST LOSSES:
{grounding}

THIS DECISION: {stance_phrase} {ticker}{conviction_phrase}.

Produce:
- "resemblances": the {min_r}-{max_r} most important ways this {ticker} decision
  resembles a bet he got wrong. Each MUST reference a specific documented loss or
  calibration fact above and draw the parallel to THIS name. Generic market risks
  ("the market could fall") are forbidden — only patterns HIS history shows.
- "conditions": 1-{max_c} candidate falsifiable decision-conditions calibrated to
  the blind spot each resemblance exposes — the early-warning he should commit to
  now. Each is a numeric test future data can satisfy or not.

Return ONLY a JSON object, no fences:
{{"resemblances": ["<grounded parallel>", ...],
  "conditions": [{{"metric": "<short name>", "metric_source": "kpi"|"financial"|null,
    "op": "lt"|"le"|"gt"|"ge", "threshold": <number>,
    "unit": "actual"|"percent"|"ratio"|"count"|"bps"|"millions"|"billions",
    "for_periods": <int >= 1>, "note": "<the test in plain words>"}}]}}"""


def _stance_phrase(stance: str | None) -> str:
    return {
        "add": "ADD to",
        "buy": "ADD to",
        "initiate": "INITIATE",
        "trim": "TRIM",
        "sell": "SELL",
        "hold": "HOLD",
        "pass": "PASS on",
        "avoid": "AVOID",
    }.get((stance or "").lower(), "act on")


def _premortem_grounding(inputs: CoachInputs) -> str:
    """The pre-mortem's evidence: the owner's conviction calibration + realized
    skill leak + his CLOSED LOSSES (broke/mixed, with his own lessons). Losses
    lead — a pre-mortem is built out of what actually went wrong before."""
    losses = [c for c in inputs.closed_lessons if (c.outcome_vs_thesis or "") in _LOSS_OUTCOMES]
    lines: list[str] = []
    if losses:
        lines.append("DOCUMENTED LOSSES (your own grading):")
        for c in losses:
            bits = [c.ticker]
            if c.entry_conviction:
                bits.append(f"entered {c.entry_conviction} conviction")
            bits.append(f"thesis {c.outcome_vs_thesis}")
            tail = " ".join(p for p in (c.exit_reason, c.lessons) if p).strip()
            lines.append(f"  - {' · '.join(bits)}" + (f" — {tail}" if tail else ""))
    # Reuse the calibration/skill half of the standard grounding for the rest.
    base = grounding_block(inputs)
    return ("\n".join(lines) + "\n" + base) if lines else base


def compose_premortem(
    repo_root: Path | str,
    ticker: str,
    *,
    stance: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    inputs: CoachInputs | None = None,
) -> Premortem | None:
    """Compose the decision-moment pre-mortem for ``ticker``. None when the
    ledger is too thin to coach (the shared min-n gate — a pre-mortem from a
    sparse record is just anxiety), when no resemblance comes back grounded, or
    on transient LLM/parse failure; hard stops propagate. ``inputs`` injects an
    already-gathered grounding (the caller batches it); None gathers.

    Deliberately NOT gated here — generation and the eval gate (gate_premortem)
    are separable so the caller decides whether to judge before surfacing, the
    same split as build_scorecard / gate_scorecard."""
    t = ticker.upper()
    ctx = inputs or gather_coach_inputs(repo_root, user_id=user_id, api_url=api_url)
    if not ctx.can_coach:
        return None
    conviction = _conviction_map(Path(repo_root) / "data" / "portfolio.db").get(t)
    conviction_phrase = f" (you rate it {conviction:g}/5)" if conviction is not None else ""
    prompt = _PREMORTEM_PROMPT.format(
        ticker=t,
        stance_phrase=_stance_phrase(stance),
        grounding=_premortem_grounding(ctx),
        conviction_phrase=conviction_phrase,
        min_r=2,
        max_r=_MAX_RESEMBLANCES,
        max_c=_MAX_DRAFT_CONDITIONS,
    )
    try:
        payload = call_llm_structured(prompt, purpose=COACH_PURPOSE, ticker=t, expect="object")
    except StructuredParseError as exc:
        log.warning({"event": "premortem_parse_failed", "ticker": t, "error": str(exc)})
        return None
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "premortem_llm_failed", "ticker": t, "error": str(exc)})
        return None
    if not isinstance(payload, dict):
        return None
    obj = cast("dict[str, object]", payload)
    resemblances_raw = obj.get("resemblances")
    resemblances = (
        [
            " ".join(str(r).split())[:400]
            for r in cast("list[object]", resemblances_raw)
            if str(r).strip()
        ][:_MAX_RESEMBLANCES]
        if isinstance(resemblances_raw, list)
        else []
    )
    if not resemblances:  # a pre-mortem with no grounded parallel is exactly what we reject
        return None
    conditions_raw = obj.get("conditions")
    drafted: list[DecisionCondition] = []
    if isinstance(conditions_raw, list):
        for entry in cast("list[object]", conditions_raw):
            cond = parse_condition(entry)
            if cond is not None:
                drafted.append(cond)
            if len(drafted) >= _MAX_DRAFT_CONDITIONS:
                break
    return Premortem(ticker=t, stance=stance, resemblances=resemblances, drafted_conditions=drafted)


def render_premortem_prose(pm: Premortem) -> str:
    """The pre-mortem as plain prose — what the eval gate judges and what the
    Socratic block embeds."""
    lines = [
        f"Pre-mortem — the ways {_stance_phrase(pm.stance)} {pm.ticker} resembles bets "
        "you got wrong:"
    ]
    lines.extend(f"{i}. {r}" for i, r in enumerate(pm.resemblances, 1))
    if pm.drafted_conditions:
        lines.append("Candidate conditions to commit to now (calibrated to your blind spots):")
        for c in pm.drafted_conditions:
            lines.append(f"- {c.note or c.metric} [{c.metric} {c.op} {c.threshold:g} {c.unit}]")
    return "\n".join(lines)


def gate_premortem(
    pm: Premortem,
    *,
    code_root: Path | str,
    judge_caller: Callable[..., str] | None = None,
) -> tuple[bool, float]:
    """Eval-gate the pre-mortem against the calibration_coach rubric before it
    reaches the owner (the directive's "eval-gated advisor composes"). Returns
    ``(passed, score)``. Hard stops on the judge propagate (config, not
    quality); the caller suppresses on a fail."""
    from evals.corpora import AuditItem
    from evals.rubric_judge import AUDIT_SPECS, judge_item, load_rubric

    spec = AUDIT_SPECS[COACH_PURPOSE]
    rubric = load_rubric(Path(code_root) / spec.rubric_relpath, purpose=COACH_PURPOSE)
    item = AuditItem(
        item_id=f"premortem:{pm.ticker}",
        label=f"calibration_coach/premortem/{pm.ticker}",
        ticker=pm.ticker,
        content=render_premortem_prose(pm),
    )
    caller = judge_caller if judge_caller is not None else call_llm
    result = judge_item(rubric, item, run_id=f"premortem-{pm.ticker}", caller=caller)
    return result.passed, result.score


def premortem_block(
    repo_root: Path | str,
    ticker: str,
    *,
    stance: str | None = None,
    code_root: Path | str | None = None,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    inputs: CoachInputs | None = None,
) -> str:
    """The decision-moment pre-mortem as a prompt-ready block for the advisor —
    composed, EVAL-GATED, and rendered, or "" when unavailable (thin ledger,
    nothing grounded, the gate failed, or any transient error). Best-effort: a
    pre-mortem must never break the flow it rides on, so every failure degrades
    to the empty string and the advisor proceeds on the calibration block alone.

    This is the L1 calibration-pack injection point EXTENDED — the Socratic
    flow appends it next to ``advisor.context.calibration_block``."""
    try:
        pm = compose_premortem(
            repo_root, ticker, stance=stance, user_id=user_id, api_url=api_url, inputs=inputs
        )
        if pm is None:
            return ""
        if code_root is not None:
            passed, score = gate_premortem(pm, code_root=code_root)
            if not passed:
                log.info({"event": "premortem_gate_suppressed", "ticker": ticker, "score": score})
                return ""
        return (
            f"**Pre-mortem (challenge this {_stance_phrase(stance)} {ticker.upper()} against "
            "your OWN documented losses — if a parallel holds, the burden is to say why THIS "
            f"one is different):**\n{render_premortem_prose(pm)}"
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "premortem_block_failed", "ticker": ticker, "error": str(exc)})
        return ""


# ===========================================================================
# Monthly scorecard — assembled, EVAL-GATED, persisted
# ===========================================================================


@dataclass(frozen=True, slots=True)
class CalibrationScorecard:
    """The owner's monthly personal scorecard — the deterministic substrate
    (cohort trend + realized skill split) plus the eval-gated coach prose
    (named biases + the period's behavioural experiment). Persisted to
    data/calibration_scorecard/<period>.json; the panel renders the latest.

    ``coach_quality_ok`` is the eval gate's verdict: False means the synthesised
    prose was suppressed before it reached the owner (a coach that misreads his
    history is worse than silence). None means it was never gated (nothing to
    gate — too thin to coach, or no biases came back)."""

    period: str
    generated_at: str
    granularity: str
    can_coach: bool
    n_graded: int
    overall_hit_rate: float | None
    improving: bool | None
    hit_rate_delta: float | None
    latest_period: str | None
    latest_hit_rate: float | None
    selection_usd: float | None
    sizing_usd: float | None
    timing_usd: float | None
    biases: list[NamedBias]
    experiment: BehavioralExperiment | None
    coach_quality_ok: bool | None
    coach_quality_score: float | None
    notes: list[str] = field(default_factory=list[str])


def render_scorecard_prose(card: CalibrationScorecard) -> str:
    """The coach prose the rubric judges (and the panel can show verbatim). The
    deterministic context is included so the judge can check that every bias is
    grounded in the owner's actual numbers."""
    lines: list[str] = [f"Calibration scorecard — {card.period}"]
    hit = f"{card.overall_hit_rate * 100:.0f}%" if card.overall_hit_rate is not None else "n/a"
    lines.append(f"Overall: {card.n_graded} graded calls, {hit} correct.")
    if card.hit_rate_delta is not None:
        direction = "improving" if card.improving else "slipping/flat"
        lines.append(f"Trend: {direction} ({card.hit_rate_delta * 100:+.0f}pp last period).")
    read = _skill_read_from_card(card)
    if read:
        lines.append(f"Realized skill — {read}.")
    if card.biases:
        lines.append("\nRecurring biases:")
        for b in card.biases:
            lines.append(f"- {b.name}: {b.pattern}")
            lines.append(f"  evidence: {'; '.join(b.evidence)}")
            if b.tell:
                lines.append(f"  tell: {b.tell}")
    if card.experiment is not None:
        e = card.experiment
        lines.append("\nThis period's experiment:")
        lines.append(f"- {e.hypothesis}")
        if e.rationale:
            lines.append(f"  why: {e.rationale}")
        if e.condition is not None:
            lines.append(f"  falsifiable test: {e.condition.note or e.condition.metric}")
    return "\n".join(lines)


def _skill_read_from_card(card: CalibrationScorecard) -> str:
    legs = [
        ("selection", card.selection_usd),
        ("sizing", card.sizing_usd),
        ("timing", card.timing_usd),
    ]
    present = [(n, v) for n, v in legs if v is not None]
    if not present:
        return ""
    edge_name, edge_v = max(present, key=lambda kv: kv[1])
    leak_name, leak_v = min(present, key=lambda kv: kv[1])
    bits: list[str] = []
    if edge_v > 0:
        bits.append(f"edge {edge_name} ${edge_v:+,.0f}")
    if leak_v < 0 and leak_name != edge_name:
        bits.append(f"leak {leak_name} ${leak_v:+,.0f}")
    return ", ".join(bits)


def build_scorecard(
    repo_root: Path | str,
    *,
    period: str,
    generated_at: str,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
) -> CalibrationScorecard:
    """Assemble the period's scorecard. Below the min-n floor it returns the
    deterministic substrate with an honest 'too thin to coach' note and NO LLM
    call. Above it, synthesises biases + the behavioural experiment. The eval
    gate is a SEPARATE step (gate_scorecard) the CLI runs before persisting, so
    generation and judging are independently testable."""
    inputs = gather_coach_inputs(repo_root, user_id=user_id, api_url=api_url)
    cal = inputs.calibration
    skill = inputs.skill
    latest = cal.cohorts[-1] if (cal is not None and cal.cohorts) else None

    def _make(
        *,
        can_coach: bool,
        biases: list[NamedBias],
        experiment: BehavioralExperiment | None,
        notes: list[str],
    ) -> CalibrationScorecard:
        return CalibrationScorecard(
            period=period,
            generated_at=generated_at,
            granularity=cal.cohort_granularity if cal is not None else "quarter",
            can_coach=can_coach,
            n_graded=inputs.n_graded,
            overall_hit_rate=cal.overall_hit_rate if cal is not None else None,
            improving=cal.improving if cal is not None else None,
            hit_rate_delta=cal.hit_rate_delta if cal is not None else None,
            latest_period=latest.period if latest is not None else None,
            latest_hit_rate=latest.hit_rate if latest is not None else None,
            selection_usd=skill.selection_usd if skill is not None else None,
            sizing_usd=skill.sizing_usd if skill is not None else None,
            timing_usd=skill.timing_usd if skill is not None else None,
            biases=biases,
            experiment=experiment,
            coach_quality_ok=None,
            coach_quality_score=None,
            notes=notes,
        )

    if not inputs.can_coach:
        return _make(
            can_coach=False,
            biases=[],
            experiment=None,
            notes=[
                f"{confidence_note(inputs.n_graded, min_n=MIN_COACH_GRADED)} — too thin to "
                "coach yet. The substrate (cohort trend, skill split) renders; the named "
                "biases + experiment unlock once the graded denominator clears the floor."
            ],
        )
    biases = synthesize_biases(inputs)
    experiment = propose_experiment(inputs, biases)
    notes: list[str] = []
    if not biases:
        notes.append("No biases met the grounding bar this period — none invented to fill space.")
    return _make(can_coach=True, biases=biases, experiment=experiment, notes=notes)


def gate_scorecard(
    card: CalibrationScorecard,
    *,
    code_root: Path | str,
    judge_caller: Callable[..., str] | None = None,
) -> CalibrationScorecard:
    """Run the L3-style rubric gate on the coach prose. On a pass the card is
    returned with ``coach_quality_ok=True``; on a fail the synthesised biases +
    experiment are SUPPRESSED (replaced with a note) so miscalibrated coaching
    never reaches the owner. Nothing-to-gate (too thin / no biases) is a no-op
    pass. Hard stops on the judge propagate (config, not quality)."""
    if not card.biases and card.experiment is None:
        # Nothing synthesised to gate (too thin to coach, or no biases met the
        # grounding bar) — leave the verdict unset rather than fabricate a pass.
        return _with_gate(card, ok=None, score=None)
    from evals.corpora import AuditItem
    from evals.rubric_judge import AUDIT_SPECS, judge_item, load_rubric

    spec = AUDIT_SPECS[COACH_PURPOSE]
    rubric = load_rubric(Path(code_root) / spec.rubric_relpath, purpose=COACH_PURPOSE)
    item = AuditItem(
        item_id=f"scorecard:{card.period}",
        label=f"calibration_coach/{card.period}",
        ticker=None,
        content=render_scorecard_prose(card),
    )
    caller = judge_caller if judge_caller is not None else call_llm
    result = judge_item(rubric, item, run_id=f"coach-{card.period}", caller=caller)
    if result.passed:
        return _with_gate(card, ok=True, score=result.score)
    log.warning(
        {
            "event": "coach_scorecard_gate_failed",
            "period": card.period,
            "score": result.score,
            "rationale": result.judge_rationale,
        }
    )
    suppressed = [
        *card.notes,
        f"Coach prose suppressed — it scored {result.score:.2f} on the calibration_coach "
        "rubric, below threshold. A coach that misreads your history is worse than silence; "
        "the deterministic substrate still renders.",
    ]
    return CalibrationScorecard(
        period=card.period,
        generated_at=card.generated_at,
        granularity=card.granularity,
        can_coach=card.can_coach,
        n_graded=card.n_graded,
        overall_hit_rate=card.overall_hit_rate,
        improving=card.improving,
        hit_rate_delta=card.hit_rate_delta,
        latest_period=card.latest_period,
        latest_hit_rate=card.latest_hit_rate,
        selection_usd=card.selection_usd,
        sizing_usd=card.sizing_usd,
        timing_usd=card.timing_usd,
        biases=[],
        experiment=None,
        coach_quality_ok=False,
        coach_quality_score=result.score,
        notes=suppressed,
    )


def _with_gate(
    card: CalibrationScorecard, *, ok: bool | None, score: float | None
) -> CalibrationScorecard:
    return CalibrationScorecard(
        period=card.period,
        generated_at=card.generated_at,
        granularity=card.granularity,
        can_coach=card.can_coach,
        n_graded=card.n_graded,
        overall_hit_rate=card.overall_hit_rate,
        improving=card.improving,
        hit_rate_delta=card.hit_rate_delta,
        latest_period=card.latest_period,
        latest_hit_rate=card.latest_hit_rate,
        selection_usd=card.selection_usd,
        sizing_usd=card.sizing_usd,
        timing_usd=card.timing_usd,
        biases=card.biases,
        experiment=card.experiment,
        coach_quality_ok=ok,
        coach_quality_score=score,
        notes=card.notes,
    )


# ---------------------------------------------------------------------------
# Persistence (data/calibration_scorecard/<period>.json)
# ---------------------------------------------------------------------------


def _bias_to_dict(b: NamedBias) -> dict[str, object]:
    return {"name": b.name, "pattern": b.pattern, "evidence": list(b.evidence), "tell": b.tell}


def _experiment_to_dict(e: BehavioralExperiment) -> dict[str, object]:
    return {
        "hypothesis": e.hypothesis,
        "rationale": e.rationale,
        "condition": e.condition.as_json_obj() if e.condition is not None else None,
    }


def scorecard_to_dict(card: CalibrationScorecard) -> dict[str, object]:
    return {
        "period": card.period,
        "generated_at": card.generated_at,
        "granularity": card.granularity,
        "can_coach": card.can_coach,
        "n_graded": card.n_graded,
        "overall_hit_rate": card.overall_hit_rate,
        "improving": card.improving,
        "hit_rate_delta": card.hit_rate_delta,
        "latest_period": card.latest_period,
        "latest_hit_rate": card.latest_hit_rate,
        "selection_usd": card.selection_usd,
        "sizing_usd": card.sizing_usd,
        "timing_usd": card.timing_usd,
        "biases": [_bias_to_dict(b) for b in card.biases],
        "experiment": _experiment_to_dict(card.experiment) if card.experiment is not None else None,
        "coach_quality_ok": card.coach_quality_ok,
        "coach_quality_score": card.coach_quality_score,
        "notes": list(card.notes),
        # Denormalised so the eval corpus loader reads the judged text directly
        # without re-importing the (heavy) coach module into evals.corpora.
        "prose": render_scorecard_prose(card),
    }


def save_scorecard(repo_root: Path | str, card: CalibrationScorecard) -> Path:
    """Persist one period's scorecard (overwriting that period's prior run).
    Returns the written path. The directory is created on demand."""
    out_dir = Path(repo_root) / "data" / _SCORECARD_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{card.period}.json"
    path.write_text(
        json.dumps(scorecard_to_dict(card), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _f(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def scorecard_from_dict(raw: dict[str, object]) -> CalibrationScorecard:
    """Decode a persisted scorecard, tolerant of missing keys (a scorecard
    written by an older build still loads)."""

    def _biases() -> list[NamedBias]:
        biases_raw = raw.get("biases")
        if not isinstance(biases_raw, list):
            return []
        out: list[NamedBias] = []
        for entry in cast("list[object]", biases_raw):
            b = _parse_bias(entry)
            if b is not None:
                out.append(b)
        return out

    def _experiment() -> BehavioralExperiment | None:
        exp = raw.get("experiment")
        if not isinstance(exp, dict):
            return None
        e = cast("dict[str, object]", exp)
        hyp = e.get("hypothesis")
        if not isinstance(hyp, str):
            return None
        rat = e.get("rationale")
        return BehavioralExperiment(
            hypothesis=hyp,
            rationale=rat if isinstance(rat, str) else "",
            condition=parse_condition(e.get("condition")),
        )

    ok_raw = raw.get("coach_quality_ok")
    imp_raw = raw.get("improving")
    notes_raw = raw.get("notes")
    notes = [str(n) for n in cast("list[object]", notes_raw)] if isinstance(notes_raw, list) else []
    return CalibrationScorecard(
        period=str(raw.get("period", "")),
        generated_at=str(raw.get("generated_at", "")),
        granularity=str(raw.get("granularity", "quarter")),
        can_coach=bool(raw.get("can_coach")),
        n_graded=int(_f(raw.get("n_graded")) or 0),
        overall_hit_rate=_f(raw.get("overall_hit_rate")),
        improving=imp_raw if isinstance(imp_raw, bool) else None,
        hit_rate_delta=_f(raw.get("hit_rate_delta")),
        latest_period=(
            str(raw["latest_period"]) if isinstance(raw.get("latest_period"), str) else None
        ),
        latest_hit_rate=_f(raw.get("latest_hit_rate")),
        selection_usd=_f(raw.get("selection_usd")),
        sizing_usd=_f(raw.get("sizing_usd")),
        timing_usd=_f(raw.get("timing_usd")),
        biases=_biases(),
        experiment=_experiment(),
        coach_quality_ok=ok_raw if isinstance(ok_raw, bool) else None,
        coach_quality_score=_f(raw.get("coach_quality_score")),
        notes=notes,
    )


def load_latest_scorecard(repo_root: Path | str) -> CalibrationScorecard | None:
    """The most recent persisted scorecard (by period sort key), or None when
    none has been generated. Best-effort: a corrupt file is skipped."""
    out_dir = Path(repo_root) / "data" / _SCORECARD_DIRNAME
    if not out_dir.is_dir():
        return None
    files = sorted(out_dir.glob("*.json"))
    for path in reversed(files):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            return scorecard_from_dict(cast("dict[str, object]", raw))
    return None


__all__ = [
    "COACH_PURPOSE",
    "MIN_COACH_GRADED",
    "BehavioralExperiment",
    "CalibrationScorecard",
    "ClosedLesson",
    "CoachInputs",
    "NamedBias",
    "Premortem",
    "build_scorecard",
    "compose_premortem",
    "gate_premortem",
    "gate_scorecard",
    "gather_coach_inputs",
    "grounding_block",
    "load_latest_scorecard",
    "premortem_block",
    "propose_experiment",
    "render_premortem_prose",
    "render_scorecard_prose",
    "save_scorecard",
    "scorecard_from_dict",
    "scorecard_to_dict",
    "synthesize_biases",
]
