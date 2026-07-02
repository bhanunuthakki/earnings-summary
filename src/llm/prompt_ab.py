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

log = logging.getLogger(__name__)

PROPOSE_PURPOSE = "prompt_variant_propose"

# Verdict labels (§4.3) — deliberately parallel to the model-loop's, relabeled.
PROMOTE_VARIANT = "PROMOTE_VARIANT"
KEEP_BASELINE = "KEEP_BASELINE"
AB_HOLD = "HOLD"
AB_INSUFFICIENT = "INSUFFICIENT_DATA"
VARIANT_ERRORED = "VARIANT_ERRORED"

# Promotion bar (§4.4): asymmetric to the model-switch bar — a prompt change is
# cheap and reversible, but churn has cost and "provably better" still rules.
PROMOTION_MIN_WIN_RATE = 0.60  # variant strictly wins >= 60% of judged cases
PROMOTION_MAX_BASELINE_WINS = 0.20  # zero-regression guard
PROMOTION_MIN_AGREEMENT = 0.6
PROMOTION_MIN_RUNS = 2
PROMOTION_MIN_CASES = 10
VARIANT_ERROR_RATE_THRESHOLD = 0.5  # mirrors CANDIDATE_ERROR_RATE_THRESHOLD

SET_BY_AUTO_AB = "auto:prompt_ab_loop"

# Instruction scaffold for prompt_variant_propose (v1 — prompt_versions).
PROPOSE_PROMPT = """\
You are improving one production LLM prompt via a SMALL, surgical edit set.

Purpose: "{purpose}". Below are (1) the CURRENT prompt template (the checked-in
instruction scaffold), (2) one real rendered example (scaffold + per-ticker
data), and (3) the improvement signal — where this prompt's outputs recently
lost quality facets or eval points.

Propose ONE variant as an ordered list of exact-match edits ON THE SCAFFOLD.
Hard rules:
- Each "find" string must occur EXACTLY ONCE in the template AND inside the
  rendered example — never touch the per-ticker data region.
- Preserve WHAT is asked (same task, same output consumer). Changing task
  semantics is a product change, out of scope.
- Fewest edits that plausibly fix the signal. 1-4 edits.

Respond with ONLY a JSON object:
{{"hypothesis": "<one line: what should improve and why>",
 "edits": [{{"find": "<exact substring>", "replace": "<replacement>"}}, ...],
 "expected_effect": "<which facet/criteria should move>"}}

=== CURRENT TEMPLATE ===
{template}

=== RENDERED EXAMPLE ===
{example}

=== IMPROVEMENT SIGNAL ===
{signal}
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
) -> VariantProposal | None:
    """One proposal pass (Opus). Returns None on any failure — proposing is
    steering, never load-bearing."""
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
                template=template[:12000],
                example=rendered_example[:8000],
                signal=improvement_signal[:4000],
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
    conn = sqlite3.connect(str(db_path), timeout=10.0)
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
) -> None:
    """Append one experiment run's verdict (rolling INSERTs, never upserts)."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(
            """
            INSERT INTO prompt_ab_verdicts
                (experiment_id, purpose, run_id, n_cases, variant_wins,
                 baseline_wins, ties, win_rate, judge_agreement, recommendation,
                 reason, summary_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
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
) -> tuple[str, str]:
    """One run's recommendation from per-judge tallies (§4.3 — the
    decide_switch math, relabeled). The PROMOTION bar additionally pools >=2
    runs / >=10 cases via ``promotion_ready``."""
    error_rate = n_variant_errors / n_cases_attempted if n_cases_attempted else 0.0
    if n_cases_attempted > 0 and error_rate >= VARIANT_ERROR_RATE_THRESHOLD:
        return (
            VARIANT_ERRORED,
            f"variant failed operationally on {n_variant_errors}/{n_cases_attempted} case(s) "
            f"({error_rate:.0%}) — an authoring failure, not a quality verdict",
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


def promotion_ready(db_path: Path, experiment_id: str) -> tuple[bool, str]:
    """§4.4 pooled bar: >=PROMOTION_MIN_RUNS runs recommending PROMOTE_VARIANT
    and >=PROMOTION_MIN_CASES distinct judged cases total, no KEEP_BASELINE."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if not _has_table(conn, "prompt_ab_verdicts"):
                return False, "prompt_ab_verdicts absent"
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
    promotes = [r for r in rows if str(r["recommendation"]) == PROMOTE_VARIANT]
    total_cases = sum(int(r["n_cases"] or 0) for r in promotes)
    if len(promotes) >= PROMOTION_MIN_RUNS and total_cases >= PROMOTION_MIN_CASES:
        return True, f"{len(promotes)} promoting run(s), {total_cases} pooled cases"
    return (
        False,
        f"{len(promotes)}/{PROMOTION_MIN_RUNS} promoting run(s), "
        f"{total_cases}/{PROMOTION_MIN_CASES} pooled cases",
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
        conn = sqlite3.connect(str(path), timeout=5.0)
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
    conn = sqlite3.connect(str(path), timeout=10.0)
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
        conn = sqlite3.connect(str(path), timeout=10.0)
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
