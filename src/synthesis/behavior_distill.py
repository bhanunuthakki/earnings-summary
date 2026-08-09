"""The behavioral-rules distiller (tenet-2 Phase 4, ``docs/design/
tenet2_advisory_program.md`` §3.2 Tier C / §6 Phase-4 bullet / §7 ruling 3).

Replaces the frozen five-line ``_behavioral_rules`` string
(``src/advisor/position_review.py``) with rows distilled from the owner's OWN
graded ``decisions`` corpus (``decided_by='owner'``, ``outcome_label !=
'pending'``) plus the behavioral musings the Ledger seed carried — the SAME
distill -> propose -> ratify machinery as the Worldview Tenets
(``synthesis.tenet_distill``), applied to a narrower, more falsifiable claim:
"you have a documented pattern of doing X, and here is the graded evidence."

Rule 1 (sell-winners-too-early) distills identically today from the seed
corpus; the difference this pipeline buys is that the rule *keeps being
re-derived* as the graded corpus grows, so a pattern that stops confirming
naturally weakens instead of nagging forever — and nothing the distiller
proposes becomes authoritative without an explicit owner tap (§7.1 gated
assertion: ambient learning is always-on, assertion is affirmation-gated).

Hallucination guard (mirrors ``thesis_collision._coerce_tickers``): the model
cites ``decisions.id`` values plus its claimed outcome for each one. Every
citation is validated IN CODE against the real corpus before a rule can
stage — an id outside the corpus, or an id whose claimed outcome doesn't match
what's actually graded on that row, is dropped (logged at WARNING, a
hallucination-pattern signal). A rule left with zero valid citations is
discarded whole, never staged with fabricated evidence. The wrong/total tally
that ends up in the staged fact is computed from the VALIDATED citations only
— never the model's own arithmetic.

Tension handling: staging goes through the same ``owner_profile.store.
append_fact`` append-and-supersede path every Tier A/B fact uses. When an
AFFIRMED behavioral fact already exists for the proposed rule's key, the new
row still supersedes it (the store's generic contract), but lands ``status=
'proposed'`` like every derived fact — never auto-affirmed — so the packet
walk shows the old (still readable via the superseded-row chain) vs the new
proposal side by side, exactly like a Tier A re-import over a ratified fact.

Cost control: this is a batch distill over the WHOLE graded corpus, not a
per-decision call — one LLM call proposes 0-6 candidate rules per run. Run on
a cadence (post-grading, ``execution/run_calibration_grading.py``) or on
demand (``execution/run_behavior_distill.py``); never a per-request call.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from calibration_guard import confidence_note
from identity import DEFAULT_USER_ID
from owner_profile.models import BehavioralRule
from owner_profile.store import OwnerProfileFactRow, append_fact, list_facts
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

PURPOSE = "behavior_distill"

# A distill call's raw shape: the graded corpus + musing context in ->
# candidate rule dicts | None. Injected (``call=``) so the engine is
# unit-testable without the CLI, exactly like tenet_distill.DistillCall.
ProposedRule = dict[str, object]
DistillCall = Callable[[str], "list[ProposedRule] | None"]

_KEY_RX = re.compile(r"^[a-z][a-z0-9_.]*$")
_KEY_PREFIX = "behavior."
_MAX_MUSINGS = 20
_MAX_DECISIONS = 200  # defensive cap on the prompt; the corpus is small today


@dataclass(slots=True, frozen=True)
class GradedDecisionRow:
    """One graded owner decision — the deterministic, citable evidence base."""

    id: int
    ticker: str | None
    recommendation_kind: str
    outcome_label: str
    process_quality: str | None
    falsifier: str | None
    rationale_excerpt: str | None
    made_at: str


def graded_decision_corpus(
    db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID
) -> list[GradedDecisionRow]:
    """Owner-decided, graded (non-pending) decisions, oldest first — the
    citable evidence base every candidate rule must ground into. Degrades to
    ``[]`` on a missing DB/table/column (nothing to distil ⇒ the caller's $0
    short-circuit), never raises. ``user_id`` is accepted for API symmetry
    with the rest of the store (``decisions`` itself is not user-scoped yet,
    so it is currently unused here)."""
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except (sqlite3.Error, OSError, ValueError):
        return []
    try:
        conn.row_factory = sqlite3.Row
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if not cols:
            return []
        proc_col = "process_quality" if "process_quality" in cols else "NULL"
        fals_col = "falsifier" if "falsifier" in cols else "NULL"
        rows = conn.execute(
            f"SELECT id, ticker, recommendation_kind, outcome_label, "
            f"{proc_col} AS process_quality, {fals_col} AS falsifier, "
            "rationale_excerpt, made_at FROM decisions "
            "WHERE decided_by = 'owner' AND outcome_label IS NOT NULL "
            "AND outcome_label != 'pending' ORDER BY made_at ASC LIMIT ?",
            (_MAX_DECISIONS,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        GradedDecisionRow(
            id=int(r["id"]),
            ticker=(str(r["ticker"]) if r["ticker"] else None),
            recommendation_kind=str(r["recommendation_kind"]),
            outcome_label=str(r["outcome_label"]),
            process_quality=(str(r["process_quality"]) if r["process_quality"] else None),
            falsifier=(str(r["falsifier"]) if r["falsifier"] else None),
            rationale_excerpt=(str(r["rationale_excerpt"]) if r["rationale_excerpt"] else None),
            made_at=str(r["made_at"]),
        )
        for r in rows
    ]


def _behavioral_musings(db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID) -> list[str]:
    """The owner's captured musings (context, not citable) — best-effort;
    degrades to ``[]`` on any read failure so a musing-store hiccup never
    blocks the distill over the (more important) graded decisions."""
    try:
        from user_state.notes import list_notes

        notes = list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=_MAX_MUSINGS)
    except Exception as exc:  # best-effort context only
        log.debug({"event": "behavior_distill_musings_load_failed", "error": str(exc)})
        return []
    return [n.body for n in notes if n.body.strip()]


def _build_prompt(decisions: Sequence[GradedDecisionRow], musings: Sequence[str]) -> str:
    # key=value evidence lines (NOT prose like "-> graded wrong") so a model
    # that echoes the outcome value verbatim reproduces exactly the closed
    # token ("wrong"/"correct"/"mixed"/"unfalsifiable"), never a decorated
    # phrase the deterministic validator would then reject as a miscitation.
    dec_lines = [
        f"- id={d.id} date={d.made_at[:10]} ticker={d.ticker or 'PORTFOLIO'} "
        f"kind={d.recommendation_kind} outcome={d.outcome_label}"
        + (f" falsifier={d.falsifier!r}" if d.falsifier else "")
        + (f" process_quality={d.process_quality}" if d.process_quality else "")
        for d in decisions
    ]
    musing_lines = [f"- {m}" for m in musings]
    return (
        "You are distilling an investor's OWN graded decision history into candidate "
        "BEHAVIORAL RULES — durable, falsifiable patterns in HOW they decide (not calls "
        "on any single name). Ground every rule ONLY in the graded decisions below and "
        "cite the SPECIFIC decision ids it rests on, together with each cited decision's "
        "outcome. Propose a rule only when the graded evidence genuinely supports "
        "a repeating pattern; if it does not, return an empty list. Write each rule_text "
        'in the SECOND PERSON, as a direct instruction ("You sell your winners too '
        'early — ...").\n\n'
        "Graded decisions (one per line, key=value fields):\n"
        + ("\n".join(dec_lines) if dec_lines else "(none)")
        + "\n\nThe owner's own captured musings (context only — NOT citable evidence; "
        "never cite a musing as if it were a decision id):\n"
        + ("\n".join(musing_lines) if musing_lines else "(none)")
        + "\n\nReturn JSON ONLY: a list of 0-6 objects, each:\n"
        '{"key": "<stable slug, e.g. behavior.sell_winners_early>", '
        '"rule_text": "<the rule, second person, one to two sentences>", '
        '"citations": [{"decision_id": <int, must be one of the ids above>, '
        '"outcome": "<COPY the exact outcome= value from that decision\'s line above, '
        "e.g. \"wrong\" — one bare word, NEVER a phrase like 'graded wrong' or "
        '"the outcome was wrong">}], '
        '"confidence_note": "<one short phrase>"}'
    )


def _slugify_key(raw: object, *, fallback: str) -> str:
    """Normalize the model's proposed key to ``behavior.<snake_case>`` — a
    stable slug within the ``behavioral`` category (e.g.
    ``behavior.sell_winners_early``). Any existing ``behavior.``/``behavior_``
    prefix is stripped before slugifying the remainder so it is never
    doubled; an empty/unusable input falls back to a positional slug."""
    text = str(raw).strip().lower() if isinstance(raw, str) and raw.strip() else fallback
    if text.startswith(_KEY_PREFIX):
        text = text[len(_KEY_PREFIX) :]
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        text = fallback
    key = f"{_KEY_PREFIX}{text}"
    return key if _KEY_RX.match(key) else f"{_KEY_PREFIX}rule"


# The closed outcome vocabulary a citation's claim is normalized against
# (mirrors the decisions.outcome_label CHECK constraint, minus 'pending' —
# graded_decision_corpus already excludes pending rows from the citable set).
_OUTCOME_LABELS: tuple[str, ...] = ("correct", "wrong", "mixed", "unfalsifiable")


def _normalize_outcome_claim(raw: str) -> str | None:
    """Extract a bare outcome token from a citation's ``outcome`` claim,
    tolerating a decorated phrase (``"graded wrong"``, ``"the outcome was
    wrong"``) the model wrote despite the prompt asking for a bare token.
    Returns the single matched label, or ``None`` when zero or more than one
    label appears (ambiguous — never guess which one was meant)."""
    lowered = raw.strip().lower()
    matches = [label for label in _OUTCOME_LABELS if re.search(rf"\b{label}\b", lowered)]
    return matches[0] if len(matches) == 1 else None


def _validate_citations(
    raw: object, corpus_by_id: dict[int, GradedDecisionRow]
) -> tuple[list[int], list[object]]:
    """Every cited ``{"decision_id", "outcome"}`` pair, validated against the
    REAL corpus: the id must exist AND the claimed outcome must match what is
    actually graded on that row (tolerating a decorated phrase around the
    bare label — see :func:`_normalize_outcome_claim` — but never a genuinely
    different or ambiguous outcome). Mirrors ``thesis_collision._coerce_
    tickers`` — every dropped citation is logged at WARNING (a
    hallucination-pattern signal), never silently swallowed. Returns (valid
    distinct decision ids, the raw dropped entries)."""
    if not isinstance(raw, list):
        return [], []
    valid: list[int] = []
    dropped: list[object] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            dropped.append(entry)
            continue
        d = cast("dict[str, object]", entry)
        did_raw = d.get("decision_id")
        outcome_raw = d.get("outcome")
        did = did_raw if isinstance(did_raw, int) and not isinstance(did_raw, bool) else None
        row = corpus_by_id.get(did) if did is not None else None
        claimed = _normalize_outcome_claim(outcome_raw) if isinstance(outcome_raw, str) else None
        if row is None or claimed is None or row.outcome_label != claimed:
            dropped.append(d)
            continue
        if did not in valid:
            valid.append(cast(int, did))
    if dropped:
        log.warning(
            {
                "event": "behavior_distill_dropped_hallucinated_citations",
                "dropped": dropped[:10],
                "dropped_count": len(dropped),
                "corpus_size": len(corpus_by_id),
            }
        )
    return valid, dropped


def run_behavior_distill(
    db_path: Path | str | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    call: DistillCall | None = None,
) -> dict[str, int]:
    """Distil the graded owner-decision corpus into ``proposed`` behavioral
    ``owner_profile_facts`` (category='behavioral', provenance='derived').

    Returns a tally. $0 LLM spend when the graded corpus is empty (nothing to
    distil). Degrade-safe: a raising/None/malformed ``call`` result leaves the
    profile untouched (mirrors ``run_tenet_distill``) — callers that need hard
    stops to propagate (budget cap / missing CLI) pass a ``call`` that raises
    them; this function itself never converts an exception into a tally, it
    only tolerates one from the injected callable so tests can simulate a
    transient failure without a live LLM.
    """
    counts = {
        "candidates": 0,
        "proposed": 0,
        "tensions": 0,
        "skipped_uncited": 0,
        "skipped_invalid": 0,
        "deferred_transient": 0,
    }
    decisions = graded_decision_corpus(db_path, user_id=user_id)
    counts["candidates"] = len(decisions)
    if not decisions:
        return counts  # deterministic $0 short-circuit — no LLM

    musings = _behavioral_musings(db_path, user_id=user_id)
    prompt = _build_prompt(decisions, musings)

    if call is None:
        from llm.contracts import BEHAVIOR_RULE_BATCH_SCHEMA
        from llm.structured import StructuredParseError, call_llm_structured

        def _call(_prompt: str) -> list[ProposedRule] | None:
            try:
                obj = call_llm_structured(
                    _prompt,
                    purpose=PURPOSE,
                    scope="owner_profile",
                    expect="array",
                    schema=BEHAVIOR_RULE_BATCH_SCHEMA,
                    db_path=db_path,
                )
            except StructuredParseError:
                return None  # unparseable output degrades to "no proposals"
            if not isinstance(obj, list):
                return None
            return [
                cast("ProposedRule", x) for x in cast("list[object]", obj) if isinstance(x, dict)
            ]

        the_call: DistillCall = _call
    else:
        the_call = call

    try:
        proposals = the_call(prompt)
    except Exception as exc:
        # Hard stops (LLMBudgetExceeded / LLMSetupError — llm.cli.is_hard_stop)
        # are configuration problems, not distill quality: they must PROPAGATE
        # so the cadence hook's caller sees the run as failed, loudly (the
        # post-#814 attach_conditions pattern). Everything else is a transient
        # per-run failure: the profile is left untouched and the run is
        # tallied "deferred" — the next scheduled distill retries for free
        # (nothing here is stateful across runs beyond the profile itself).
        from llm.cli import is_hard_stop

        if is_hard_stop(exc):
            raise
        log.warning({"event": "behavior_distill_call_failed_transient", "error": str(exc)})
        counts["deferred_transient"] = 1
        return counts
    if not proposals:
        return counts

    corpus_by_id = {d.id: d for d in decisions}
    already_affirmed = {
        row.key for row in list_facts_safe(db_path, status="affirmed", category="behavioral")
    }

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.execute("PRAGMA busy_timeout = 30000")  # see reference_platform_invariants memory
    try:
        # ``proposals`` is already dict-only by the DistillCall contract (the
        # default ``_call`` filters non-dict JSON items before this point,
        # mirroring tenet_distill.run_tenet_distill's loop — no redundant
        # isinstance re-check here).
        for idx, p in enumerate(proposals):
            rule_text = str(p.get("rule_text") or "").strip()
            valid_ids, _dropped = _validate_citations(p.get("citations"), corpus_by_id)
            if not rule_text or not valid_ids:
                counts["skipped_uncited"] += 1
                continue
            wrong = sum(1 for i in valid_ids if corpus_by_id[i].outcome_label == "wrong")
            total = len(valid_ids)
            try:
                rule = BehavioralRule(
                    rule_text=rule_text, citations=valid_ids, wrong=wrong, total=total
                )
            except ValidationError as exc:
                log.warning(
                    {"event": "behavior_distill_schema_invalid", "index": idx, "error": str(exc)}
                )
                counts["skipped_invalid"] += 1
                continue
            key = _slugify_key(p.get("key"), fallback=f"rule_{idx}")
            narrative = (
                f"{rule.rule_text} — graded record: {rule.wrong} of {rule.total} cited "
                f"decisions wrong ({confidence_note(rule.total)})."
            )
            append_fact(
                conn,
                category="behavioral",
                key=key,
                value=rule.model_dump(mode="json"),
                narrative=narrative,
                provenance="derived",
                status="proposed",
                review_horizon_days=None,  # Tier C reviews after each grading batch, not on a fixed clock
                source_detail=f"behavior_distill run over {len(decisions)} graded decisions",
                user_id=user_id,
            )
            counts["proposed"] += 1
            if key in already_affirmed:
                counts["tensions"] += 1
        conn.commit()
    finally:
        conn.close()
    return counts


def list_facts_safe(
    db_path: Path | str | None, *, status: str, category: str, user_id: str = DEFAULT_USER_ID
) -> list[OwnerProfileFactRow]:
    """``owner_profile.store.list_facts`` over a fresh connection, degrading to
    ``[]`` on a missing DB/table — the pre-distill "does an affirmed row
    already exist for this key" probe, kept separate from the write
    connection so a read failure never blocks the whole run."""
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except (sqlite3.Error, OSError, ValueError):
        return []
    try:
        return list_facts(conn, status=status, category=category, user_id=user_id)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


__all__ = [
    "GradedDecisionRow",
    "ProposedRule",
    "graded_decision_corpus",
    "run_behavior_distill",
]
