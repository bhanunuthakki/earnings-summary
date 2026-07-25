"""One randomized prompt-improvement CYCLE (meta_eval_governance.md §4,
extended 2026-07-24 — the automation §4.6 called for and PR5 never wired).

The §4 build shipped a driver you must hand-drive: pick the purpose yourself,
supply the prompt template as a file, run one two-arm experiment. Production
proof that this does not sustain itself: ``prompt_experiments`` had ZERO rows
between the PR5 merge (2026-07-02) and 2026-07-24.

A cycle here is fully self-steering:

  0. BUDGET — refuse to start if this month's meta spend is at the ceiling.
  1. DRAW A PURPOSE — weighted by ``ab_leverage`` (production cost x measured
     quality deficit), excluding purposes with a live experiment and those the
     nominator excluded. Seeded.
  2. DERIVE THE SCAFFOLD from captured renders (no template file, no registry).
     A purpose with no derivable scaffold is SKIPPED LOUDLY.
  3. DRAW STRATEGIES — Thompson sampling over the edit-strategy taxonomy, so
     each cycle explores a different direction and the winners compound.
  4. PROPOSE one variant per strategy (Opus), each constrained to its direction
     and to quoting the scaffold blocks. Anchors validated against every sampled
     render BEFORE any spend.
  5. COMPOSE — if two proposals are compatible, add a third arm carrying BOTH
     edit sets. This is the only way a negative interaction between two
     individually-good edits ever becomes visible.
  6. RUN + JUDGE all arms against ONE shared case sample, then decide per arm.
  7. PROMOTE any arm clearing the pooled §4.4 bar (owner decision Q1: auto-apply).

Usage:
    python execution/run_prompt_ab_cycle.py --repo-root <MAIN>
    python execution/run_prompt_ab_cycle.py --repo-root <MAIN> --purpose bear_case
    python execution/run_prompt_ab_cycle.py --repo-root <MAIN> --dry-run

``--dry-run`` performs every draw, derivation and proposal and prints the plan
WITHOUT running or judging any arm — the cheap way to see what the loop intends.

Run with LLM_CAPTURE_DIR UNSET (this process pops it): A/B traffic must never
enter a harvest corpus (isolation invariant I5).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.sampler import load_frame  # noqa: E402
from llm.backend_judge import CLAUDE, GEMINI  # noqa: E402
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.eval_scopes import EVAL_SCOPES  # noqa: E402
from llm.prompt_ab import (  # noqa: E402
    PromptArm,
    VariantProposal,
    compose,
    create_experiment,
    propose_variant,
    validate_edits_against,
    write_arms,
)
from llm.prompt_scaffold import Scaffold, block_containing, derive_scaffold  # noqa: E402
from llm.prompt_signal import ImprovementSignal, ab_leverage, build_improvement_signal  # noqa: E402
from llm.prompt_strategies import (  # noqa: E402
    EditStrategy,
    draw_purpose,
    draw_strategies,
    load_strategy_stats,
    render_strategy_directive,
)
from llm.prompt_versions import prompt_version_for  # noqa: E402

log = logging.getLogger("run_prompt_ab_cycle")

DEFAULT_ARMS = 2  # fresh proposals; a composed arm may be added on top
DEFAULT_N_CASES = 12
# Owner ceiling 2026-07-24: ~$40/mo across 2-3 cycles/week => ~$3.30/cycle.
# Enforced as a monthly ledger check, not a per-call guess.
DEFAULT_MONTHLY_BUDGET_USD = 40.0

# Purposes whose captured renders exist but which must never be A/B'd: the
# meta-machinery itself (I5 — the optimizer must not optimise its own prompts
# while it is the thing doing the measuring).
_SELF_PURPOSES = frozenset(
    {
        "prompt_variant_propose",
        "optimizer_nominator",
        "model_frontier_research",
        "query_criteria_derive",
        "case_difficulty_classify",
        "backend_compare_judge",
        "eval_judge",
    }
)


@dataclass(frozen=True, slots=True)
class CyclePlan:
    """Everything a cycle decided before spending anything."""

    cycle_id: str
    purpose: str
    rng_seed: str
    scaffold: Scaffold
    signal: ImprovementSignal
    strategies: tuple[EditStrategy, ...]
    arms: tuple[PromptArm, ...]
    frozen_model: str


def _month_start() -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def meta_spend_this_month(db_path: Path) -> float:
    """Month-to-date spend on measurement scopes (the meta budget line)."""
    if not db_path.exists():
        return 0.0
    scopes = sorted(EVAL_SCOPES)
    placeholders = ",".join("?" * len(scopes))
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            row = conn.execute(
                f"SELECT COALESCE(SUM(cost_estimate_usd), 0) FROM llm_calls "
                f"WHERE scope IN ({placeholders}) AND called_at >= ?",
                (*scopes, _month_start()),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0
    return float(row[0] or 0.0) if row else 0.0


def _live_experiment_purposes(db_path: Path, *, max_age_days: int = 14) -> set[str]:
    """Purposes with a RECENT undecided experiment — §4.6 allows one at a time.

    The age window is load-bearing, not cosmetic: a process that dies between
    persisting an experiment and deciding it leaves status='proposed' forever,
    and an unbounded exclusion would then lock that purpose out of every future
    draw — silently, one purpose per failure, until the loop is dead again
    (adversarial-review finding, 2026-07-25). Fourteen days is two weekly
    cycles: long enough that a genuinely live experiment is never preempted,
    short enough that a stuck one costs at most two skipped draws.
    """
    if not db_path.exists():
        return set()
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).replace(tzinfo=None).isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_experiments'"
            ).fetchone():
                return set()
            rows = conn.execute(
                "SELECT DISTINCT purpose FROM prompt_experiments "
                "WHERE status IN ('proposed', 'running') AND created_at >= ?",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows}


def _purpose_costs(db_path: Path, *, window_days: int = 30) -> dict[str, float]:
    """Production 30d cost per purpose (measurement scopes excluded)."""
    if not db_path.exists():
        return {}
    scopes = sorted(EVAL_SCOPES)
    placeholders = ",".join("?" * len(scopes))
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).replace(tzinfo=None).isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            rows = conn.execute(
                f"SELECT purpose, COALESCE(SUM(cost_estimate_usd), 0) AS c FROM llm_calls "
                f"WHERE called_at >= ? AND (scope IS NULL OR scope NOT IN ({placeholders})) "
                f"GROUP BY purpose",
                (cutoff, *scopes),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {str(r[0]): float(r[1] or 0.0) for r in rows if r[0]}


def _capturable_purposes(files: list[Path], *, min_renders: int = 2) -> set[str]:
    """Purposes with >= min_renders DISTINCT captured prompts — the scaffold
    derivation's hard prerequisite. One cheap pass over the capture files
    (counting distinct prompt hashes per purpose), not a load_frame per
    candidate."""
    import hashlib

    seen: dict[str, set[str]] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            record = cast("dict[str, object]", rec)
            purpose = record.get("purpose")
            prompt = record.get("prompt")
            if isinstance(purpose, str) and isinstance(prompt, str) and prompt.strip():
                digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
                seen.setdefault(purpose, set()).add(digest)
    return {p for p, digests in seen.items() if len(digests) >= min_renders}


def plan_cycle(
    db_path: Path,
    capture_dir: Path,
    *,
    forced_purpose: str | None = None,
    seed: str | None = None,
    n_arms: int = DEFAULT_ARMS,
    n_cases: int = DEFAULT_N_CASES,
) -> CyclePlan | None:
    """Steps 1-5: every decision, no spend beyond the proposal calls."""
    cycle_id = uuid.uuid4().hex
    rng_seed = seed or f"cycle:{cycle_id}"
    rng = random.Random(rng_seed)

    files = sorted(capture_dir.glob("capture_*.jsonl"))
    if not files:
        log.error("no capture files under %s — nothing to derive a scaffold from", capture_dir)
        return None

    # --- 1. draw the purpose -------------------------------------------------
    if forced_purpose:
        purpose = forced_purpose
        signal = build_improvement_signal(db_path, purpose)
    else:
        costs = _purpose_costs(db_path)
        live = _live_experiment_purposes(db_path)
        # A scaffold needs >=2 captured renders, so the draw pool is restricted
        # to purposes the corpus can actually serve. Found on the first prod
        # dry-run (2026-07-25): the unrestricted draw picked key_metrics (high
        # leverage, ZERO captures) and burned the whole cycle on a skip. An
        # uncaptured purpose's fix is more harvest, not a wasted draw.
        capturable = _capturable_purposes(files)
        weights: dict[str, float] = {}
        signals: dict[str, ImprovementSignal] = {}
        skipped_uncaptured: list[str] = []
        for candidate, cost in costs.items():
            if candidate in live or candidate in _SELF_PURPOSES:
                continue
            if candidate not in capturable:
                skipped_uncaptured.append(candidate)
                continue
            sig = build_improvement_signal(db_path, candidate)
            signals[candidate] = sig
            weights[candidate] = ab_leverage(cost, sig.deficit)
        if skipped_uncaptured:
            log.info(
                "draw pool excludes %d uncaptured purpose(s) (top by cost: %s) — "
                "harvest them to make them A/B-eligible",
                len(skipped_uncaptured),
                ", ".join(sorted(skipped_uncaptured, key=lambda p: -costs[p])[:5]),
            )
        drawn = draw_purpose(weights, rng)
        if drawn is None:
            log.error(
                "no eligible purpose to experiment on "
                "(%d live experiment(s), %d costed purposes) — nothing drawn",
                len(live),
                len(costs),
            )
            return None
        purpose = drawn
        signal = signals[purpose]

    # --- 2. derive the scaffold ---------------------------------------------
    frame = load_frame(files, purpose)
    if not frame:
        log.error("[%s] no captured renders — cannot derive a scaffold", purpose)
        return None
    renders = [rec.prompt for rec in frame.values()][:8]
    scaffold = derive_scaffold(renders)
    if not scaffold.eligible:
        log.error("[%s] SKIPPED: %s", purpose, scaffold.reason)
        return None
    log.info(
        "[%s] scaffold: %d block(s), %d chars, %.0f%% of the render, from %d renders",
        purpose,
        len(scaffold.blocks),
        scaffold.scaffold_chars,
        100 * scaffold.coverage,
        scaffold.n_renders,
    )

    # --- 3. draw strategies --------------------------------------------------
    stats = load_strategy_stats(db_path)
    strategies = draw_strategies(n_arms, stats, rng)
    log.info(
        "[%s] strategies drawn: %s",
        purpose,
        ", ".join(
            f"{s.key}(a={stats[s.key].alpha:.0f},b={stats[s.key].beta:.0f})" for s in strategies
        ),
    )

    # --- 4. propose one variant per strategy --------------------------------
    menu = scaffold.render_block_menu()
    example = renders[0]
    arms: list[PromptArm] = []
    for index, strategy in enumerate(strategies):
        proposal = propose_variant(
            purpose=purpose,
            template=menu,
            rendered_example=example,
            improvement_signal=signal.text,
            direction=render_strategy_directive((strategy,)),
        )
        if proposal is None:
            log.warning("[%s] strategy %s: proposal failed — arm dropped", purpose, strategy.key)
            continue
        arm = _validated_arm(
            proposal,
            label=chr(ord("A") + index),
            strategy_key=strategy.key,
            scaffold=scaffold,
            renders=renders,
            purpose=purpose,
        )
        if arm is not None:
            arms.append(arm)

    if not arms:
        log.error("[%s] every proposed arm failed validation — cycle aborted pre-spend", purpose)
        return None

    # --- 5. compose ----------------------------------------------------------
    if len(arms) >= 2:
        combined = compose(
            arms[0],
            arms[1],
            arm_label=chr(ord("A") + len(arms)),
            scaffold_text=example,
        )
        if combined is None:
            log.info(
                "[%s] arms %s + %s are not composable (overlapping anchors) — "
                "no combination arm this cycle",
                purpose,
                arms[0].arm_label,
                arms[1].arm_label,
            )
        else:
            arms.append(combined)
            log.info(
                "[%s] combination arm %s = %s + %s",
                purpose,
                combined.arm_label,
                arms[0].arm_label,
                arms[1].arm_label,
            )

    return CyclePlan(
        cycle_id=cycle_id,
        purpose=purpose,
        rng_seed=rng_seed,
        scaffold=scaffold,
        signal=signal,
        strategies=strategies,
        arms=tuple(arms),
        frozen_model=LLM_MODELS.get(purpose, DEFAULT_MODEL),
    )


def _validated_arm(
    proposal: VariantProposal,
    *,
    label: str,
    strategy_key: str,
    scaffold: Scaffold,
    renders: list[str],
    purpose: str,
) -> PromptArm | None:
    """Reject a proposal that anchors outside the scaffold or fails to splice.

    Both checks matter and neither implies the other: an edit can splice
    cleanly on today's renders while sitting in the per-request data region
    (fine now, mutates real data later), and an edit can quote the scaffold yet
    still collide with a sibling edit in the same set.
    """
    outside = [e.find for e in proposal.edits if block_containing(scaffold, e.find) is None]
    if outside:
        log.warning(
            "[%s] arm %s (%s): %d edit(s) anchor OUTSIDE the scaffold — rejected pre-spend "
            "(first: %r)",
            purpose,
            label,
            strategy_key,
            len(outside),
            outside[0][:60],
        )
        return None
    if not validate_edits_against(proposal.edits, renders[0], renders):
        log.warning(
            "[%s] arm %s (%s): edits fail exactly-once anchoring across renders — "
            "rejected pre-spend",
            purpose,
            label,
            strategy_key,
        )
        return None
    return PromptArm(
        arm_label=label,
        edits=proposal.edits,
        hypothesis=proposal.hypothesis,
        strategy_key=strategy_key,
        source="fresh",
    )


def persist_plan(db_path: Path, plan: CyclePlan) -> str:
    """Create the experiment row + its arms; returns the experiment id."""
    experiment_id = create_experiment(
        db_path,
        purpose=plan.purpose,
        baseline_prompt_version=prompt_version_for(plan.purpose),
        hypothesis=plan.arms[0].hypothesis,
        edits=plan.arms[0].edits,  # legacy single-arm mirror (see mig 0200)
        frozen_model=plan.frozen_model,
    )
    write_arms(db_path, experiment_id, plan.arms)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(
            "UPDATE prompt_experiments SET cycle_id = ?, rng_seed = ?, signal_json = ? "
            "WHERE experiment_id = ?",
            (
                plan.cycle_id,
                plan.rng_seed,
                json.dumps(
                    {
                        "deficit": plan.signal.deficit,
                        "has_eval_coverage": plan.signal.has_eval_coverage,
                        "avg_eval_score": plan.signal.avg_eval_score,
                        "error_rate": plan.signal.error_rate,
                        "n_rationales": plan.signal.n_rationales,
                        "n_infra_excluded": plan.signal.n_infra_excluded,
                        "evidence_backed": plan.signal.is_evidence_backed,
                        "strategies": [s.key for s in plan.strategies],
                        "scaffold_blocks": len(plan.scaffold.blocks),
                        "scaffold_coverage": plan.scaffold.coverage,
                    },
                    ensure_ascii=False,
                ),
                experiment_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return experiment_id


def _print_plan(plan: CyclePlan) -> None:
    print(f"\n=== CYCLE {plan.cycle_id[:8]} — purpose '{plan.purpose}' ===")
    print(f"seed          : {plan.rng_seed}")
    print(f"frozen model  : {plan.frozen_model}")
    print(
        f"signal        : deficit={plan.signal.deficit:.3f} "
        f"eval_coverage={plan.signal.has_eval_coverage} "
        f"evidence_backed={plan.signal.is_evidence_backed} "
        f"(rationales={plan.signal.n_rationales}, "
        f"infra_excluded={plan.signal.n_infra_excluded})"
    )
    print(
        f"scaffold      : {len(plan.scaffold.blocks)} blocks / "
        f"{plan.scaffold.scaffold_chars} chars / {plan.scaffold.coverage:.0%} coverage"
    )
    for arm in plan.arms:
        kind = "COMBINATION" if arm.is_composed else "fresh"
        print(f"\n  arm {arm.arm_label} [{kind}] strategy={arm.strategy_key}")
        print(f"    hypothesis: {arm.hypothesis[:200]}")
        for edit in arm.edits:
            print(f"      - find   : {edit.find[:90]!r}")
            print(f"        replace: {edit.replace[:90]!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--capture-dir", type=Path, default=None)
    parser.add_argument("--purpose", default=None, help="force a purpose instead of drawing one")
    parser.add_argument("--seed", default=None, help="replay a prior cycle's rng_seed")
    parser.add_argument("--arms", type=int, default=DEFAULT_ARMS)
    parser.add_argument("--n", type=int, default=DEFAULT_N_CASES)
    parser.add_argument("--judges", default=f"{CLAUDE},{GEMINI}")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument(
        "--monthly-budget",
        type=float,
        default=DEFAULT_MONTHLY_BUDGET_USD,
        help="abort if month-to-date measurement spend already exceeds this",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan + propose, print the arms, spend nothing on running or judging",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    capture_dir = args.capture_dir or (repo_root / "data" / "llm_capture")
    if os.environ.pop("LLM_CAPTURE_DIR", None):
        log.warning("cleared inherited LLM_CAPTURE_DIR (A/B traffic must not be captured)")
    if not db_path.exists():
        log.error("DB not found at %s", db_path)
        return 1
    import db as _db

    _db.set_db_path(db_path)

    spent = meta_spend_this_month(db_path)
    if spent >= args.monthly_budget:
        log.error(
            "meta spend month-to-date $%.2f >= ceiling $%.2f — cycle refused",
            spent,
            args.monthly_budget,
        )
        return 2
    log.info("meta spend month-to-date: $%.2f / $%.2f", spent, args.monthly_budget)

    plan = plan_cycle(
        db_path,
        capture_dir,
        forced_purpose=args.purpose,
        seed=args.seed,
        n_arms=args.arms,
        n_cases=args.n,
    )
    if plan is None:
        return 1
    _print_plan(plan)

    if args.dry_run:
        print("\n[dry-run] nothing persisted, nothing run, nothing judged.")
        return 0

    experiment_id = persist_plan(db_path, plan)
    log.info("persisted experiment %s with %d arm(s)", experiment_id, len(plan.arms))
    print(
        f"\nRun it with:\n"
        f"  python execution/run_prompt_ab.py --experiment {experiment_id} "
        f"--repo-root {repo_root}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
