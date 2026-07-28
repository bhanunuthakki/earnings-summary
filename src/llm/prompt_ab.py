"""Automated prompt A/B testing + the prompt-override auto-apply
(meta_eval_governance.md §4 + §10 Q1, PR5).

Improves the platform's own prompts, not just the model behind them. A VARIANT
is a deterministic textual transformation of the rendered prompt: an ordered
list of exact-match edits on the instruction scaffold — the parts identical
across all captured renders of a purpose (§4.1). ``apply_edits`` rejects any
edit whose anchor doesn't occur EXACTLY ONCE — before a cent is spent — so the
only delta between baseline and variant is the intended prompt change, never
harness bookkeeping (isolation invariant I1 for A/B).

Owner decision Q1 (§10): promotion AUTO-APPLIES. ``prompt_pin_overrides``
mirrors ``model_pin_overrides`` — reversible rows (purpose → ordered edits),
applied at ``call_llm`` time to PRODUCTION traffic only:

* eval/meta scopes are NEVER override-applied — replays must stay byte-identical
  to their captured prompt (I1), and an already-edited prompt would fail the
  exactly-once anchors anyway;
* an anchor failure at apply time (the underlying template drifted) FAILS OPEN
  to the original prompt with a loud log — production never breaks on a stale
  override;
* auto-demote: a later experiment concluding KEEP_BASELINE deactivates the
  override (mirrors the model-loop's regression revert);
* the git-reconciliation trail: every activation logs + stores the exact edits
  and the experiment id, so catching the checked-in prompt constant up to the
  live override is a routine copy-paste PR (``reason_json.edits``).

Judging composes the existing machinery unchanged: ``judge_pair`` (brand-blind,
position-swapped, dual-judge) with §3 criteria derived from the BASELINE prompt
(the baseline defines the task; a variant that games semantics is penalized by
construction). Judges never see the edits, the hypothesis, or which side is the
variant (I6).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

PROPOSE_PURPOSE = "prompt_variant_propose"

# Verdict labels (§4.3) — deliberately parallel to the model-loop's, relabeled.
PROMOTE_VARIANT = "PROMOTE_VARIANT"
KEEP_BASELINE = "KEEP_BASELINE"
AB_HOLD = "HOLD"
AB_INSUFFICIENT = "INSUFFICIENT_DATA"
VARIANT_ERRORED = "VARIANT_ERRORED"
# A composed arm that scores BELOW the best of the arms it was built from: its
# two edits are individually fine and jointly harmful. Distinct from
# KEEP_BASELINE, which would wrongly imply the components were bad too.
INTERACTION_NEGATIVE = "INTERACTION_NEGATIVE"
# The TRANSPORT failed on both sides — nothing was measured about the prompt.
# Without this, a failing CLI reads as "the variant is broken" (VARIANT_ERRORED)
# and the bandit learns to avoid whichever strategy was unlucky enough to be
# drawn during an outage. Measured 2026-07-24: the platform-wide llm_calls error
# rate ran 72% for the month, so this is the common case, not a corner one.
TRANSPORT_DEGRADED = "TRANSPORT_DEGRADED"

# Promotion bar (§4.4): asymmetric to the model-switch bar — a prompt change is
# cheap and reversible, but churn has cost and "provably better" still rules.
PROMOTION_MIN_WIN_RATE = 0.60  # variant strictly wins >= 60% of judged cases
PROMOTION_MAX_BASELINE_WINS = 0.20  # zero-regression guard
PROMOTION_MIN_AGREEMENT = 0.6
PROMOTION_MIN_RUNS = 2
PROMOTION_MIN_CASES = 10
VARIANT_ERROR_RATE_THRESHOLD = 0.5  # mirrors CANDIDATE_ERROR_RATE_THRESHOLD
# Baseline error rate above which a variant's failures are attributed to the
# transport rather than to the edit. The baseline is UNEDITED, so anything it
# fails on is not the variant's fault by construction.
TRANSPORT_FLOOR = 0.25

SET_BY_AUTO_AB = "auto:prompt_ab_loop"

# Instruction scaffold for prompt_variant_propose (v1 — prompt_versions).
PROPOSE_PROMPT = """\
You are improving one production LLM prompt via a SMALL, surgical edit set.

Purpose: "{purpose}". Below are (1) the SCAFFOLD BLOCKS — the parts of this
prompt that are byte-identical across every captured production render, i.e.
the instruction text as opposed to the per-request data, (2) one real rendered
example so you can see scaffold and data in context, (3) the improvement signal
showing where this prompt's outputs recently lost quality, and (4) the
DIRECTION your edit must take this cycle.

Propose ONE variant as an ordered list of exact-match edits.
Hard rules:
- Every "find" string MUST be copied verbatim from inside a SCAFFOLD BLOCK. That
  is the only region proven safe to edit; anything else risks mutating the
  per-request data on some future render.
- Each "find" must occur EXACTLY ONCE in the block you took it from.
- Preserve WHAT is asked (same task, same output consumer). Changing task
  semantics is a product change, out of scope.
- Follow the REQUIRED DIRECTION. Do not substitute a different improvement you
  happen to prefer — other directions are being tested as separate arms, and an
  off-direction edit corrupts the comparison.
- Fewest edits that plausibly work. 1-4 edits.

Respond with ONLY a JSON object:
{{"hypothesis": "<one line: what should improve and why>",
 "edits": [{{"find": "<exact substring>", "replace": "<replacement>"}}, ...],
 "expected_effect": "<which facet/criteria should move>"}}

=== SCAFFOLD BLOCKS (the only legal edit targets) ===
{scaffold}

=== RENDERED EXAMPLE ===
{example}

=== IMPROVEMENT SIGNAL ===
{signal}

=== REQUIRED DIRECTION FOR THIS EDIT ===
{direction}
"""

StructCall = Callable[..., object]


class EditAnchorError(ValueError):
    """An edit's ``find`` doesn't occur exactly once in the target text."""


@dataclass(frozen=True, slots=True)
class PromptEdit:
    find: str
    replace: str


def apply_edits(prompt: str, edits: tuple[PromptEdit, ...]) -> str:
    """Deterministic edit-splice (§4.1): every ``find`` must occur EXACTLY ONCE
    at its turn, else ``EditAnchorError`` — rejected BEFORE any LLM spend."""
    out = prompt
    for e in edits:
        if out.count(e.find) != 1:
            raise EditAnchorError(e.find[:80])
        out = out.replace(e.find, e.replace, 1)
    return out


def edits_from_json(raw: str) -> tuple[PromptEdit, ...]:
    """Parse a stored edits_json array (fail-closed: malformed ⇒ empty)."""
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    out: list[PromptEdit] = []
    for entry_obj in cast("list[object]", parsed):
        if not isinstance(entry_obj, dict):
            return ()
        entry = cast("dict[str, object]", entry_obj)
        find = entry.get("find")
        replace = entry.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            return ()
        out.append(PromptEdit(find=find, replace=replace))
    return tuple(out)


def edits_to_json(edits: tuple[PromptEdit, ...]) -> str:
    return json.dumps([{"find": e.find, "replace": e.replace} for e in edits], ensure_ascii=False)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


# ---------------------------------------------------------------------------
# Variant proposal (§4.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantProposal:
    hypothesis: str
    edits: tuple[PromptEdit, ...]
    expected_effect: str


def validate_edits_against(
    edits: tuple[PromptEdit, ...], template: str, rendered_samples: list[str]
) -> bool:
    """§4.1/§4.2 pre-spend validation: every ``find`` anchors exactly once in
    the template AND in EVERY sampled rendered prompt (edits that touch the
    per-ticker data region can't anchor uniquely across renders)."""
    if not edits:
        return False
    for target in [template, *rendered_samples]:
        try:
            apply_edits(target, edits)
        except EditAnchorError:
            return False
    return True


def propose_variant(
    *,
    purpose: str,
    template: str,
    rendered_example: str,
    improvement_signal: str,
    struct: StructCall | None = None,
    direction: str = "No specific direction — make the single highest-value edit.",
) -> VariantProposal | None:
    """One proposal pass (Opus). Returns None on any failure — proposing is
    steering, never load-bearing.

    ``template`` carries the scaffold the edits must be quoted from (the derived
    block menu from ``llm.prompt_scaffold``, or a checked-in template for the
    legacy ``--template-file`` path). ``direction`` is the drawn strategy's
    constraint; the default keeps the single-shot legacy behaviour usable.
    """
    struct_fn: StructCall
    if struct is None:
        from llm.structured import call_llm_structured

        struct_fn = call_llm_structured
    else:
        struct_fn = struct
    try:
        payload = struct_fn(
            PROPOSE_PROMPT.format(
                purpose=purpose,
                scaffold=template[:12000],
                example=rendered_example[:8000],
                signal=improvement_signal[:4000],
                direction=direction[:1200],
            ),
            purpose=PROPOSE_PURPOSE,
            scope="meta_eval",
            expect="object",
            required_keys=("hypothesis", "edits"),
        )
    except Exception as exc:
        log.warning("variant proposal failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None
    if not isinstance(payload, dict):
        return None
    obj = cast("dict[str, object]", payload)
    hypothesis = obj.get("hypothesis")
    expected = obj.get("expected_effect")
    edits_raw = obj.get("edits")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        return None
    if not isinstance(edits_raw, list) or not edits_raw:
        return None
    edits: list[PromptEdit] = []
    for entry_obj in cast("list[object]", edits_raw)[:4]:
        if not isinstance(entry_obj, dict):
            return None
        entry = cast("dict[str, object]", entry_obj)
        find = entry.get("find")
        replace = entry.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            return None
        edits.append(PromptEdit(find=find, replace=replace))
    return VariantProposal(
        hypothesis=hypothesis.strip()[:400],
        edits=tuple(edits),
        expected_effect=(expected.strip()[:200] if isinstance(expected, str) else ""),
    )


# ---------------------------------------------------------------------------
# Experiments (rows in prompt_experiments / prompt_ab_verdicts)
# ---------------------------------------------------------------------------


def create_experiment(
    db_path: Path,
    *,
    purpose: str,
    baseline_prompt_version: str,
    hypothesis: str,
    edits: tuple[PromptEdit, ...],
    frozen_model: str,
    status: str = "proposed",
) -> str:
    """Insert a prompt_experiments row; returns the experiment_id."""
    experiment_id = uuid.uuid4().hex
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        conn.execute(
            """
            INSERT INTO prompt_experiments
                (experiment_id, purpose, baseline_prompt_version, variant_label,
                 hypothesis, edits_json, frozen_model, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                purpose,
                baseline_prompt_version,
                f"exp-{experiment_id[:8]}",
                hypothesis,
                edits_to_json(edits),
                frozen_model,
                status,
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return experiment_id


# ---------------------------------------------------------------------------
# Arms (mig 0200) — parallel + composed variants against one shared sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptArm:
    """One non-baseline arm. The baseline is implicit and never an arm."""

    arm_label: str
    edits: tuple[PromptEdit, ...]
    hypothesis: str
    strategy_key: str  # comma-separated for a composed arm
    source: str = "fresh"  # 'fresh' | 'composed'
    composed_from: tuple[str, ...] = ()

    @property
    def is_composed(self) -> bool:
        return self.source == "composed"


def compose(
    left: PromptArm, right: PromptArm, *, arm_label: str, scaffold_text: str | None = None
) -> PromptArm | None:
    """Union two arms into a combination arm, or None if they cannot compose.

    Two edit sets conflict when they touch overlapping text: applying one
    destroys the other's anchor, so ``apply_edits`` would raise mid-splice and
    the arm would read as an authoring failure. Detecting it here keeps the
    conflict out of the spend path entirely.

    The check is a substring-overlap test on the ``find`` strings plus, when a
    scaffold is supplied, a real dry-run splice — the dry run is what actually
    proves composability, since two non-overlapping finds can still collide if
    one's REPLACE text contains the other's find.
    """
    left_finds = [e.find for e in left.edits]
    right_finds = [e.find for e in right.edits]
    for lf in left_finds:
        for rf in right_finds:
            if lf in rf or rf in lf:
                return None
    merged = (*left.edits, *right.edits)
    if scaffold_text is not None:
        try:
            apply_edits(scaffold_text, merged)
        except EditAnchorError:
            return None
    return PromptArm(
        arm_label=arm_label,
        edits=merged,
        hypothesis=(
            f"COMBINATION of {left.arm_label} + {right.arm_label}: "
            f"{left.hypothesis[:150]} || {right.hypothesis[:150]}"
        )[:400],
        strategy_key=",".join(
            dict.fromkeys([*left.strategy_key.split(","), *right.strategy_key.split(",")])
        ),
        source="composed",
        composed_from=(left.arm_label, right.arm_label),
    )


def write_arms(db_path: Path, experiment_id: str, arms: tuple[PromptArm, ...]) -> None:
    """Persist an experiment's arms (mig 0200)."""
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        conn.executemany(
            """
            INSERT INTO prompt_arms
                (experiment_id, arm_label, edits_json, hypothesis, strategy_key,
                 source, composed_from, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    experiment_id,
                    arm.arm_label,
                    edits_to_json(arm.edits),
                    arm.hypothesis,
                    arm.strategy_key,
                    arm.source,
                    ",".join(arm.composed_from) if arm.composed_from else None,
                    _now_iso(),
                )
                for arm in arms
            ],
        )
        conn.commit()
    finally:
        conn.close()


def load_arms(db_path: Path, experiment_id: str) -> tuple[PromptArm, ...]:
    """The experiment's arms, or a single synthesised arm from the legacy
    ``prompt_experiments.edits_json`` when no arm rows exist.

    The two shapes are distinguishable by construction (arm rows present or
    absent), so the legacy path is a visible fallback, not a silent
    reinterpretation of new-shape data.
    """
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        if _has_table(conn, "prompt_arms"):
            rows = conn.execute(
                "SELECT * FROM prompt_arms WHERE experiment_id = ? ORDER BY arm_label",
                (experiment_id,),
            ).fetchall()
            if rows:
                return tuple(
                    PromptArm(
                        arm_label=str(r["arm_label"]),
                        edits=edits_from_json(str(r["edits_json"])),
                        hypothesis=str(r["hypothesis"] or ""),
                        strategy_key=str(r["strategy_key"] or ""),
                        source=str(r["source"] or "fresh"),
                        composed_from=tuple(
                            p for p in str(r["composed_from"] or "").split(",") if p
                        ),
                    )
                    for r in rows
                )
        legacy = conn.execute(
            "SELECT edits_json, hypothesis FROM prompt_experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
    finally:
        conn.close()
    if legacy is None:
        return ()
    edits = edits_from_json(str(legacy["edits_json"]))
    if not edits:
        return ()
    return (
        PromptArm(
            arm_label="A",
            edits=edits,
            hypothesis=str(legacy["hypothesis"] or ""),
            strategy_key="",
            source="fresh",
        ),
    )


def detect_negative_interaction(
    arm_results: dict[str, tuple[str, float]],
    arms: tuple[PromptArm, ...],
) -> dict[str, str]:
    """Relabel composed arms that underperform their own components.

    ``arm_results`` maps arm_label -> (recommendation, win_rate). A composed arm
    is flagged when its win rate falls below the best component's — the
    combination destroyed value the parts had. Only DECIDED components count:
    comparing against a component whose run was TRANSPORT_DEGRADED would compare
    a real number against an artefact of the outage.
    """
    relabelled: dict[str, str] = {}
    neutral = {TRANSPORT_DEGRADED, AB_INSUFFICIENT, VARIANT_ERRORED}
    for arm in arms:
        if not arm.is_composed or arm.arm_label not in arm_results:
            continue
        own_rec, own_rate = arm_results[arm.arm_label]
        if own_rec in neutral:
            continue
        component_rates = [
            arm_results[label][1]
            for label in arm.composed_from
            if label in arm_results and arm_results[label][0] not in neutral
        ]
        if not component_rates:
            continue
        if own_rate < max(component_rates):
            relabelled[arm.arm_label] = INTERACTION_NEGATIVE
    return relabelled


def record_ab_verdict(
    db_path: Path,
    *,
    experiment_id: str,
    purpose: str,
    run_id: str,
    n_cases: int,
    variant_wins: int,
    baseline_wins: int,
    ties: int,
    win_rate: float,
    judge_agreement: float,
    recommendation: str,
    reason: str,
    summary_json: str | None = None,
    arm_label: str | None = None,
) -> None:
    """Append one experiment run's verdict (rolling INSERTs, never upserts).

    ``arm_label`` is written only when the column exists (mig 0200) — the
    function stays usable against a pre-0200 DB rather than raising, which keeps
    the legacy two-arm path and its tests working unchanged.
    """
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        has_arm_col = any(
            r[1] == "arm_label" for r in conn.execute("PRAGMA table_info(prompt_ab_verdicts)")
        )
        columns = [
            "experiment_id",
            "purpose",
            "run_id",
            "n_cases",
            "variant_wins",
            "baseline_wins",
            "ties",
            "win_rate",
            "judge_agreement",
            "recommendation",
            "reason",
            "summary_json",
            "recorded_at",
        ]
        values: list[object] = [
            experiment_id,
            purpose,
            run_id,
            n_cases,
            variant_wins,
            baseline_wins,
            ties,
            win_rate,
            judge_agreement,
            recommendation,
            reason,
            summary_json,
            _now_iso(),
        ]
        if has_arm_col:
            columns.append("arm_label")
            values.append(arm_label)
        placeholders = ",".join("?" * len(columns))
        conn.execute(
            f"INSERT INTO prompt_ab_verdicts ({','.join(columns)}) VALUES ({placeholders})",
            tuple(values),
        )
        conn.commit()
    finally:
        conn.close()


def decide_ab(
    *,
    per_judge: dict[str, tuple[int, int, int]],  # judge -> (variant_wins, baseline_wins, ties)
    judge_agreement: float,
    n_cases_attempted: int,
    n_variant_errors: int,
    min_cases: int = 4,
    n_baseline_errors: int = 0,
) -> tuple[str, str]:
    """One run's recommendation from per-judge tallies (§4.3 — the
    decide_switch math, relabeled). The PROMOTION bar additionally pools >=2
    runs / >=10 cases via ``promotion_ready``.

    ``n_baseline_errors`` separates an authoring failure from a transport
    failure. The baseline prompt is UNEDITED, so its failures cannot be caused
    by the variant's edits; when it is failing too, the run measured the CLI,
    not the prompt, and must not be recorded as evidence about either.
    """
    error_rate = n_variant_errors / n_cases_attempted if n_cases_attempted else 0.0
    baseline_error_rate = n_baseline_errors / n_cases_attempted if n_cases_attempted else 0.0
    if n_cases_attempted > 0 and baseline_error_rate >= TRANSPORT_FLOOR:
        return (
            TRANSPORT_DEGRADED,
            f"baseline itself failed on {n_baseline_errors}/{n_cases_attempted} case(s) "
            f"({baseline_error_rate:.0%}) — the transport is down, nothing was measured "
            "about the prompt; neutral for the bandit and the promotion bar",
        )
    if n_cases_attempted > 0 and error_rate >= VARIANT_ERROR_RATE_THRESHOLD:
        return (
            VARIANT_ERRORED,
            f"variant failed operationally on {n_variant_errors}/{n_cases_attempted} case(s) "
            f"({error_rate:.0%}) while the baseline held "
            f"({baseline_error_rate:.0%}) — an authoring failure, not a quality verdict",
        )
    totals = [vw + bw + ti for (vw, bw, ti) in per_judge.values()]
    n = max(totals) if totals else 0
    if n < min_cases:
        return AB_INSUFFICIENT, f"only {n} judged case(s); need >={min_cases}"
    win_rates = [
        vw / (vw + bw + ti) if (vw + bw + ti) else 0.0 for (vw, bw, ti) in per_judge.values()
    ]
    base_rates = [
        bw / (vw + bw + ti) if (vw + bw + ti) else 0.0 for (vw, bw, ti) in per_judge.values()
    ]
    if all(w >= PROMOTION_MIN_WIN_RATE for w in win_rates) and all(
        b <= PROMOTION_MAX_BASELINE_WINS for b in base_rates
    ):
        if judge_agreement >= PROMOTION_MIN_AGREEMENT:
            return (
                PROMOTE_VARIANT,
                f"variant strictly wins >={PROMOTION_MIN_WIN_RATE:.0%} per judge with "
                f"baseline <={PROMOTION_MAX_BASELINE_WINS:.0%}; agreement {judge_agreement:.0%}",
            )
        return AB_HOLD, f"judges disagree (agreement {judge_agreement:.0%})"
    if any(b > w for w, b in zip(win_rates, base_rates, strict=True)):
        return KEEP_BASELINE, "a judge has the baseline winning a majority"
    return AB_HOLD, "mixed: below the promotion bar for some judge"


def promotion_ready(
    db_path: Path, experiment_id: str, *, arm_label: str | None = None
) -> tuple[bool, str]:
    """§4.4 pooled bar: >=PROMOTION_MIN_RUNS runs recommending PROMOTE_VARIANT
    and >=PROMOTION_MIN_CASES distinct judged cases total, no KEEP_BASELINE.

    With ``arm_label`` the bar is evaluated for THAT arm alone — arms compete
    independently, and one arm losing says nothing about another's edits.
    Passing None keeps the legacy whole-experiment behaviour.

    TRANSPORT_DEGRADED and INTERACTION_NEGATIVE are handled explicitly:
    the former is neutral (it measured the CLI, not the prompt) while the latter
    is disqualifying (the combination was measured, and it was worse).
    """
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            if not _has_table(conn, "prompt_ab_verdicts"):
                return False, "prompt_ab_verdicts absent"
            has_arm_col = any(
                r[1] == "arm_label" for r in conn.execute("PRAGMA table_info(prompt_ab_verdicts)")
            )
            if arm_label is not None and has_arm_col:
                rows = conn.execute(
                    "SELECT recommendation, n_cases FROM prompt_ab_verdicts "
                    "WHERE experiment_id = ? AND arm_label = ? ORDER BY recorded_at",
                    (experiment_id, arm_label),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT recommendation, n_cases FROM prompt_ab_verdicts "
                    "WHERE experiment_id = ? ORDER BY recorded_at",
                    (experiment_id,),
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"read failed: {exc}"
    recs = [str(r["recommendation"]) for r in rows]
    if KEEP_BASELINE in recs:
        return False, "a run concluded KEEP_BASELINE"
    if INTERACTION_NEGATIVE in recs:
        return False, "a run concluded INTERACTION_NEGATIVE (the combination hurt)"
    promotes = [r for r in rows if str(r["recommendation"]) == PROMOTE_VARIANT]
    total_cases = sum(int(r["n_cases"] or 0) for r in promotes)
    if len(promotes) >= PROMOTION_MIN_RUNS and total_cases >= PROMOTION_MIN_CASES:
        return True, f"{len(promotes)} promoting run(s), {total_cases} pooled cases"
    n_transport = sum(1 for r in recs if r == TRANSPORT_DEGRADED)
    suffix = f" ({n_transport} run(s) neutral: transport degraded)" if n_transport else ""
    return (
        False,
        f"{len(promotes)}/{PROMOTION_MIN_RUNS} promoting run(s), "
        f"{total_cases}/{PROMOTION_MIN_CASES} pooled cases{suffix}",
    )


# ---------------------------------------------------------------------------
# prompt_pin_overrides — the Q1 auto-apply (mirrors model_pin_overrides)
# ---------------------------------------------------------------------------


def active_prompt_override(
    purpose: str, *, db_path: Path | str | None = None
) -> tuple[PromptEdit, ...] | None:
    """The active override edits for ``purpose``, or None. Fail-safe: any DB
    problem reads as no-override (production never blocks on telemetry)."""
    from db_paths import resolve_db_path

    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return None
    try:
        conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            if not _has_table(conn, "prompt_pin_overrides"):
                return None
            row = conn.execute(
                "SELECT edits_json FROM prompt_pin_overrides "
                "WHERE purpose = ? AND active = 1 ORDER BY set_at DESC LIMIT 1",
                (purpose,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    edits = edits_from_json(str(row[0]))
    return edits or None


def write_prompt_override(
    purpose: str,
    edits: tuple[PromptEdit, ...],
    *,
    experiment_id: str,
    set_by: str = SET_BY_AUTO_AB,
    reason: dict[str, object] | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Activate an override (deactivating any prior active row — one active row
    per purpose; deactivated rows stay as the audit trail). The reason_json
    carries the exact edits: the git-reconciliation trail for catching the
    checked-in prompt constant up in a routine PR (§10 Q1)."""
    from db_paths import resolve_db_path

    path = resolve_db_path(db_path)
    if path is None:
        return
    payload: dict[str, object] = dict(reason or {})
    payload.setdefault("experiment_id", experiment_id)
    payload.setdefault("edits", json.loads(edits_to_json(edits)))
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        conn.execute(
            "UPDATE prompt_pin_overrides SET active = 0 WHERE purpose = ? AND active = 1",
            (purpose,),
        )
        conn.execute(
            """
            INSERT INTO prompt_pin_overrides
                (purpose, edits_json, experiment_id, set_by, set_at, reason_json, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                purpose,
                edits_to_json(edits),
                experiment_id,
                set_by,
                _now_iso(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(
        {
            "event": "prompt_pin_override_activated",
            "purpose": purpose,
            "experiment_id": experiment_id,
            "n_edits": len(edits),
            "note": "reconcile the checked-in prompt constant via a routine PR "
            "(reason_json.edits carries the exact diff)",
        }
    )


def deactivate_prompt_override(purpose: str, *, db_path: Path | str | None = None) -> bool:
    """Auto-demote / manual revert. Idempotent; history rows stay."""
    from db_paths import resolve_db_path

    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    try:
        conn = connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            if not _has_table(conn, "prompt_pin_overrides"):
                return False
            cur = conn.execute(
                "UPDATE prompt_pin_overrides SET active = 0 WHERE purpose = ? AND active = 1",
                (purpose,),
            )
            conn.commit()
            deactivated = cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if deactivated:
        log.info({"event": "prompt_pin_override_deactivated", "purpose": purpose})
    return deactivated


def apply_prompt_override(purpose: str | None, scope: str | None, prompt: str) -> str:
    """The ``call_llm`` render-time hook (Q1). PRODUCTION scopes only:

    * eval/meta scopes are exempt — a replay must stay byte-identical to its
      captured prompt (I1), and an already-edited prompt would fail the
      exactly-once anchors anyway;
    * an anchor failure (template drift under the override) FAILS OPEN to the
      original prompt with a loud log — never breaks a live call.
    """
    if purpose is None:
        return prompt
    from llm.eval_scopes import EVAL_SCOPES

    if scope is not None and scope in EVAL_SCOPES:
        return prompt
    edits = active_prompt_override(purpose)
    if not edits:
        return prompt
    try:
        out = apply_edits(prompt, edits)
    except EditAnchorError as exc:
        log.warning(
            {
                "event": "prompt_pin_override_anchor_failed",
                "purpose": purpose,
                "anchor_head": str(exc)[:80],
                "note": "template drifted under the override — sent the ORIGINAL "
                "prompt; demote or re-run the experiment",
            }
        )
        return prompt
    log.info({"event": "prompt_pin_override_applied", "purpose": purpose, "n_edits": len(edits)})
    return out
