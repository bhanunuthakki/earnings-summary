"""Prompt A/B experiment driver (meta_eval_governance.md §4.3, PR5).

One run of one experiment:
  1. FREEZE ``model = _model_for(purpose)`` into the experiment row — a
     mid-experiment model switch must not confound the comparison.
  2. SAMPLE cases via the §2 stratified sampler (fresh vs prior runs of this
     experiment); validate the edit anchors against EVERY sampled rendered
     prompt BEFORE any spend (anchor failure ⇒ status ``rejected_anchor``).
  3. BASELINE side: reuse the captured incumbent response IF the capture's
     model equals the frozen model (zero extra spend); else re-run fresh under
     the frozen model, ``scope="prompt_ab"``.
  4. VARIANT side: ``apply_edits(case.prompt)`` under the SAME frozen model,
     ``scope="prompt_ab"`` (capture OFF in this process; budget bypassed —
     measurement, not production).
  5. JUDGE with the brand-blind pairwise judge (baseline → slot A, variant →
     slot B; position-swap randomizes presentation), dual judge, plus the §3
     criteria derived from the BASELINE prompt (cached). Judges never see the
     edits, the hypothesis, or which side is the variant.
  6. VERDICT via the decide_switch-style tallies, relabeled: PROMOTE_VARIANT |
     KEEP_BASELINE | HOLD | INSUFFICIENT_DATA | VARIANT_ERRORED. Promotion
     (auto-apply, owner decision Q1) additionally needs the pooled §4.4 bar —
     ``--promote`` applies it; a KEEP_BASELINE run AUTO-DEMOTES an active
     override from this experiment.

Usage:
    python execution/run_prompt_ab.py --purpose bear_case --propose \\
        --template-file path/to/template.txt --repo-root <MAIN>
    python execution/run_prompt_ab.py --experiment <id> --repo-root <MAIN>
    python execution/run_prompt_ab.py --experiment <id> --promote --repo-root <MAIN>

Run with LLM_CAPTURE_DIR UNSET (this process pops it) — A/B traffic must never
enter a harvest corpus (I5).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.sampler import (  # noqa: E402
    CLASSIFY_PURPOSE,
    ensure_difficulty_features,
    load_census,
    load_frame,
    sample_cases,
)
from llm.backend_judge import CLAUDE, GEMINI, JudgedPair, judge_pair  # noqa: E402
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.model_eval import run_model  # noqa: E402
from llm.model_ladder import JUDGE_POOL  # noqa: E402
from llm.prompt_ab import (  # noqa: E402
    AB_INSUFFICIENT,
    KEEP_BASELINE,
    PROMOTE_VARIANT,
    apply_edits,
    create_experiment,
    deactivate_prompt_override,
    decide_ab,
    edits_from_json,
    promotion_ready,
    propose_variant,
    record_ab_verdict,
    validate_edits_against,
    write_prompt_override,
)
from llm.prompt_versions import prompt_version_for  # noqa: E402
from llm.query_criteria import (  # noqa: E402
    CRITERIA_PURPOSE,
    derive_or_load,
    render_criteria_block,
)

log = logging.getLogger("run_prompt_ab")

DEFAULT_N = 12


def _experiment_row(db_path: Path, experiment_id: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM prompt_experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
    finally:
        conn.close()


def _set_experiment_status(
    db_path: Path, experiment_id: str, status: str, *, decision: str | None = None
) -> None:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        conn.execute(
            "UPDATE prompt_experiments SET status = ?, decision = COALESCE(?, decision), "
            "decided_at = CASE WHEN ? IS NOT NULL THEN ? ELSE decided_at END "
            "WHERE experiment_id = ?",
            (status, decision, decision, now, experiment_id),
        )
        conn.commit()
    finally:
        conn.close()


def _prior_run_shas(db_path: Path, experiment_id: str) -> set[str]:
    """Shas sampled by prior runs of THIS experiment (§4.4 fresh-sample rule)."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        rows = conn.execute(
            "SELECT summary_json FROM prompt_ab_verdicts WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchall()
    finally:
        conn.close()
    shas: set[str] = set()
    for (raw,) in rows:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed: object = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        manifest = cast("dict[str, object]", parsed).get("sample_manifest")
        if not isinstance(manifest, dict):
            continue
        case_rows = cast("dict[str, object]", manifest).get("cases")
        if not isinstance(case_rows, list):
            continue
        for entry in cast("list[object]", case_rows):
            if isinstance(entry, dict):
                sha = cast("dict[str, object]", entry).get("sha")
                if isinstance(sha, str):
                    shas.add(sha)
    return shas


def run_experiment(
    db_path: Path,
    capture_dir: Path,
    experiment_id: str,
    *,
    judges: list[str],
    n: int,
    timeout_seconds: int | None,
) -> str:
    """One experiment run; returns the recommendation label."""
    row = _experiment_row(db_path, experiment_id)
    if row is None:
        log.error("experiment %s not found", experiment_id)
        return AB_INSUFFICIENT
    purpose = str(row["purpose"])
    frozen_model = str(row["frozen_model"])
    edits = edits_from_json(str(row["edits_json"]))
    if not edits:
        log.error("experiment %s has no valid edits", experiment_id)
        return AB_INSUFFICIENT

    from evals.sampler import FrameRecord  # local: keep module import surface tight

    census = load_census(db_path, purpose)
    files = sorted(capture_dir.glob("capture_*.jsonl"))
    frame: dict[str, FrameRecord] = load_frame(files, purpose)
    if not frame:
        log.warning("[%s] no capture frame; cannot run experiment", purpose)
        return AB_INSUFFICIENT
    features, _deferred = (
        ensure_difficulty_features(
            db_path,
            purpose,
            frame,
            census,
            classifier_version=prompt_version_for(CLASSIFY_PURPOSE),
        )
        if census
        else ({}, 0)
    )
    run_id = uuid.uuid4().hex
    sample = sample_cases(
        purpose=purpose,
        n=n,
        min_n=4,
        census=census,
        frame=frame,
        features=features,
        rng_seed=f"ab:{experiment_id}:{run_id}",
        exclude_shas=_prior_run_shas(db_path, experiment_id),
    )
    if sample.insufficient_frame or not sample.cases:
        log.warning("[%s] %s", purpose, sample.reason or "no cases drawn")
        return AB_INSUFFICIENT

    # §4.1/§4.2: anchors must hold on EVERY sampled rendered prompt — checked
    # BEFORE any spend; a failure is an authoring problem, not a quality result.
    # (At run time the renders themselves are the validation targets; the
    # checked-in template was validated at proposal time.)
    if not validate_edits_against(edits, sample.cases[0].prompt, [c.prompt for c in sample.cases]):
        log.error("[%s] edit anchors failed on sampled renders — rejected_anchor", purpose)
        _set_experiment_status(db_path, experiment_id, "rejected_anchor")
        return AB_INSUFFICIENT

    _set_experiment_status(db_path, experiment_id, "running")
    criteria_version = prompt_version_for(CRITERIA_PURPOSE)
    tally: dict[str, list[int]] = {jb: [0, 0, 0] for jb in judges}  # variant, baseline, tie
    judged: list[tuple[str, JudgedPair]] = []
    case_audit: list[dict[str, object]] = []
    n_variant_errors = 0

    for case in sample.cases:
        # Baseline side: reuse the captured incumbent output when its model is
        # the frozen model (zero spend); else re-run fresh.
        rec = frame.get(case.prompt_sha256 or "")
        if rec is not None and rec.model == frozen_model:
            baseline_out = rec.response
        else:
            base = run_model(
                case.prompt,
                model_id=frozen_model,
                purpose=purpose,
                ticker=case.ticker,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                scope="prompt_ab",
            )
            if not base.ok:
                log.info("  case %s: baseline re-run failed (%s) — skipped", case.label, base.error)
                continue
            baseline_out = base.response
        variant = run_model(
            apply_edits(case.prompt, edits),
            model_id=frozen_model,
            purpose=purpose,
            ticker=case.ticker,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            scope="prompt_ab",
        )
        if not variant.ok:
            n_variant_errors += 1
            case_audit.append(
                {"label": case.label, "variant_error": variant.error, "winner": "baseline"}
            )
            for jb in judges:
                tally[jb][1] += 1
            continue
        criteria_block: str | None = None
        if case.prompt_sha256 is not None:
            crits = derive_or_load(
                db_path,
                purpose,
                case.prompt_sha256,
                case.prompt,  # criteria from the BASELINE prompt (§4.3 step 5)
                criteria_version=criteria_version,
            )
            if crits is not None:
                criteria_block = render_criteria_block(crits)
        for jb in judges:
            jp = judge_pair(
                purpose=purpose,
                label=case.label,
                ticker=case.ticker,
                claude_response=baseline_out,  # slot A == baseline
                gemini_response=variant.response,  # slot B == variant
                task_prompt=case.prompt,  # the BASELINE defines the task
                judge_backend=jb,
                judge_model=JUDGE_POOL.get(jb),
                run_id=run_id,
                criteria_block=criteria_block,
            )
            judged.append((jb, jp))
            if jp.winner == GEMINI:
                tally[jb][0] += 1
            elif jp.winner == CLAUDE:
                tally[jb][1] += 1
            else:
                tally[jb][2] += 1
            case_audit.append(
                {
                    "label": jp.label,
                    "judge": jb,
                    "winner": "variant"
                    if jp.winner == GEMINI
                    else ("baseline" if jp.winner == CLAUDE else "tie"),
                    "margin": jp.margin,
                    "position_consistent": jp.position_consistent,
                    "rationales": jp.rationales,
                    "checklist": jp.checklist_winners,
                }
            )

    by_label: dict[str, list[str]] = {}
    for _jb, jp in judged:
        by_label.setdefault(jp.label, []).append(jp.winner)
    multi = [w for w in by_label.values() if len(w) >= 2]
    agreement = (sum(1 for w in multi if len(set(w)) == 1) / len(multi)) if multi else 0.0

    per_judge = {jb: (t[0], t[1], t[2]) for jb, t in tally.items()}
    recommendation, reason = decide_ab(
        per_judge=per_judge,
        judge_agreement=agreement,
        n_cases_attempted=len(sample.cases),
        n_variant_errors=n_variant_errors,
    )
    variant_wins = sum(t[0] for t in tally.values())
    baseline_wins = sum(t[1] for t in tally.values())
    ties = sum(t[2] for t in tally.values())
    total = variant_wins + baseline_wins + ties
    record_ab_verdict(
        db_path,
        experiment_id=experiment_id,
        purpose=purpose,
        run_id=run_id,
        n_cases=len(sample.cases),
        variant_wins=variant_wins,
        baseline_wins=baseline_wins,
        ties=ties,
        win_rate=(variant_wins / total) if total else 0.0,
        judge_agreement=agreement,
        recommendation=recommendation,
        reason=reason,
        summary_json=json.dumps(
            {"cases": case_audit, "sample_manifest": sample.manifest}, ensure_ascii=False
        ),
    )
    _set_experiment_status(db_path, experiment_id, "decided", decision=recommendation)
    log.info("[%s] %s — %s", purpose, recommendation, reason)

    # Auto-demote (Q1): a KEEP_BASELINE run pulls an active override from this
    # experiment (mirrors the model loop's regression revert).
    if recommendation == KEEP_BASELINE and deactivate_prompt_override(purpose, db_path=db_path):
        log.info("[%s] active prompt override auto-demoted (KEEP_BASELINE)", purpose)
    return recommendation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--capture-dir", type=Path, default=None)
    parser.add_argument("--purpose", default=None)
    parser.add_argument("--experiment", default=None, help="existing experiment id to run")
    parser.add_argument("--propose", action="store_true", help="propose a new variant")
    parser.add_argument(
        "--template-file", type=Path, default=None, help="the checked-in prompt template text"
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="if the §4.4 pooled bar holds, AUTO-APPLY the variant (prompt_pin_overrides)",
    )
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--judges", default=f"{CLAUDE},{GEMINI}")
    parser.add_argument("--timeout", type=int, default=None)
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
    judges = [j.strip() for j in str(args.judges).split(",") if j.strip()]

    if args.propose:
        if not args.purpose or not args.template_file:
            parser.error("--propose requires --purpose and --template-file")
        template = args.template_file.read_text(encoding="utf-8")
        files = sorted(capture_dir.glob("capture_*.jsonl"))
        frame = load_frame(files, args.purpose)
        if not frame:
            log.error("no captured renders for %s — cannot propose", args.purpose)
            return 1
        example = next(iter(frame.values())).prompt
        proposal = propose_variant(
            purpose=args.purpose,
            template=template,
            rendered_example=example,
            improvement_signal="(operator-initiated; see recent verdict rationales)",
        )
        if proposal is None:
            log.error("proposal failed")
            return 1
        renders = [rec.prompt for rec in frame.values()][:8]
        if not validate_edits_against(proposal.edits, template, renders):
            log.error("proposed edits fail anchor validation — rejected pre-spend")
            frozen = LLM_MODELS.get(args.purpose, DEFAULT_MODEL)
            experiment_id = create_experiment(
                db_path,
                purpose=args.purpose,
                baseline_prompt_version=prompt_version_for(args.purpose),
                hypothesis=proposal.hypothesis,
                edits=proposal.edits,
                frozen_model=frozen,
                status="rejected_anchor",
            )
            log.info("recorded rejected experiment %s", experiment_id)
            return 1
        frozen = LLM_MODELS.get(args.purpose, DEFAULT_MODEL)
        experiment_id = create_experiment(
            db_path,
            purpose=args.purpose,
            baseline_prompt_version=prompt_version_for(args.purpose),
            hypothesis=proposal.hypothesis,
            edits=proposal.edits,
            frozen_model=frozen,
        )
        log.info("proposed experiment %s: %s", experiment_id, proposal.hypothesis)
        return 0

    if not args.experiment:
        parser.error("pass --experiment <id> (or --propose)")

    if args.promote:
        ready, why = promotion_ready(db_path, args.experiment)
        row = _experiment_row(db_path, args.experiment)
        if row is None:
            log.error("experiment %s not found", args.experiment)
            return 1
        if not ready:
            log.info("NOT promoting: %s", why)
            return 1
        edits = edits_from_json(str(row["edits_json"]))
        write_prompt_override(
            str(row["purpose"]),
            edits,
            experiment_id=args.experiment,
            reason={"pooled_bar": why, "hypothesis": str(row["hypothesis"])},
            db_path=db_path,
        )
        _set_experiment_status(db_path, args.experiment, "promoted", decision=PROMOTE_VARIANT)
        log.info(
            "PROMOTED %s — override active; reconcile the checked-in constant via a "
            "routine PR (reason_json.edits carries the exact diff) and bump "
            "prompt_versions",
            args.experiment,
        )
        return 0

    run_experiment(
        db_path,
        capture_dir,
        args.experiment,
        judges=judges,
        n=args.n,
        timeout_seconds=args.timeout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
