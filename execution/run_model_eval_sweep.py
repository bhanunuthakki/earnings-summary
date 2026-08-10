"""Weekly model-downgrade evaluation sweep — standing accumulating loop.

Discovers which purposes are active (via recent llm_calls), draws a STRATIFIED
sample of prompt cases per purpose (``evals.sampler`` — census-grounded, hard-
case-oversampled, seeded; meta_eval_governance.md §2), evaluates every cheaper
candidate, and INSERTs rows into model_eval_verdicts (rolling history — never
upserts). When the ledger census can't vouch for the sample (thin capture
frame), the verdict is the advisory ``INSUFFICIENT_FRAME`` instead of a graded
result — never grade a bad sample and call it evidence. Fixture/bootstrap DBs
with no census degrade to the legacy newest-N loader (visible in the manifest).

Designed to run weekly (Sundays, off-peak) via Windows Task Scheduler so the
evaluation history accumulates automatically. Each sweep is self-contained: if
capture files are missing for a purpose that period is simply skipped (logged)
rather than erroring the whole run.

Critical design constraints
---------------------------
* Run with LLM_CAPTURE_DIR UNSET — eval traffic must never land in a harvest
  corpus (same rule as eval_model_downgrade.py).
* Capture files ARE required as the prompt source — if none are present for a
  purpose the purpose is skipped this sweep. Schedule a capture window first
  (set LLM_CAPTURE_DIR during a normal build run, then run this sweep).
* Every verdict is INSERTed (not upserted). The rolling history is the product;
  the time-series of verdicts for a purpose is what detects drift.
* The sweep is idempotent if interrupted mid-run: already-written verdicts stay.
  Partial runs just have fewer rows than a full sweep would.

Usage:
    python execution/run_model_eval_sweep.py \\
        --capture-dir data/llm_capture \\
        --repo-root <MAIN>

    # Restrict to specific purposes (e.g. when adding a new capture window):
    python execution/run_model_eval_sweep.py \\
        --capture-dir data/llm_capture \\
        --purposes bear_case,transcript_summary \\
        --repo-root <MAIN>

    # Dry-run: evaluate but write no DB rows (useful for testing a new capture):
    python execution/run_model_eval_sweep.py \\
        --capture-dir data/llm_capture \\
        --no-persist \\
        --repo-root <MAIN>
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.coverage import (  # noqa: E402
    build_eval_run_receipt,
    persist_eval_run_receipt,
)
from evals.sampler import (  # noqa: E402
    CLASSIFY_PURPOSE,
    ensure_difficulty_features,
    load_census,
    load_frame,
    recent_sample_shas,
    sample_cases,
)
from llm.backend_judge import CLAUDE, GEMINI  # noqa: E402
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.frontier import merged_backend_for, merged_cheaper_candidates  # noqa: E402
from llm.model_eval import (  # noqa: E402
    INSUFFICIENT_FRAME,
    SWITCH_DOWN,
    CandidateVerdict,
    PromptCase,
    decide_switch,
    judge_case,
    run_model,
)
from llm.model_ladder import cheaper_candidates  # noqa: E402
from llm.model_overrides import record_verdict  # noqa: E402
from llm.nominator import (  # noqa: E402
    KIND_MODEL_DOWNGRADE,
    Nomination,
    excluded_purposes,
    mark_nomination,
    pending_nominations,
)
from llm.prompt_versions import prompt_version_for  # noqa: E402
from llm.query_criteria import (  # noqa: E402
    CRITERIA_PURPOSE,
    derive_or_load,
    render_criteria_block,
)
from llm.workload_inventory import risk_note_for  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("run_model_eval_sweep")

# Lookback window for discovering active purposes from llm_calls.
DEFAULT_LOOKBACK_DAYS = 30

# Per-tier sample sizes (meta_eval_governance.md §2.4): RISKY purposes
# (RISK_NOTES / advisor_* — silent-portfolio-harm blast radius) need the larger
# n AND min_n; everything else defaults to the CANDIDATE tier. SAFE-tier 8s
# arrive with nominations (PR3). --limit still CAPS n for quick manual runs.
RISKY_TIER_N = 16
RISKY_TIER_MIN_N = 16
DEFAULT_TIER_N = 12
DEFAULT_TIER_MIN_N = 8

# Default --limit: the max tier size, so default runs never clamp a risky tier.
DEFAULT_CASES_PER_PURPOSE = RISKY_TIER_N

# Minimum cases before a verdict is anything but INSUFFICIENT_DATA (CLI floor;
# the per-tier minimum raises it — see _tier_params).
DEFAULT_MIN_N = 4

# Fraction of cases where candidate must win-or-tie (per judge) to SWITCH_DOWN.
DEFAULT_PARITY_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Capture-file helpers
# ---------------------------------------------------------------------------


def _find_capture_files(capture_dir: Path) -> list[Path]:
    """All capture_*.jsonl files in ``capture_dir``, sorted by name (newest last)."""
    if not capture_dir.is_dir():
        return []
    return sorted(capture_dir.glob("capture_*.jsonl"))


def _load_cases_from_files(
    files: list[Path],
    *,
    purpose: str,
    limit: int,
) -> list[PromptCase]:
    """Load up to ``limit`` deduplicated prompt cases for ``purpose`` from a list
    of capture JSONL files (newest-first so the most recent prompts are sampled).
    """
    seen: set[str] = set()
    cases: list[PromptCase] = []

    for path in reversed(files):  # newest-first (files are sorted oldest-first)
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec = cast("dict[str, object]", parsed)
            if rec.get("purpose") != purpose:
                continue
            response = rec.get("response")
            prompt = rec.get("prompt")
            if not isinstance(response, str) or not response.strip():
                continue
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            sha = rec.get("prompt_sha256")
            key = sha if isinstance(sha, str) else prompt[:64]
            if key in seen:
                continue
            seen.add(key)
            ticker = rec.get("ticker")
            ticker_s = ticker if isinstance(ticker, str) else None
            label = f"{purpose}:{ticker_s or '-'}:{str(key)[:8]}"
            cases.append(
                PromptCase(
                    label=label,
                    prompt=prompt,
                    ticker=ticker_s,
                    incumbent_response=response,
                )
            )
            if len(cases) >= limit:
                return cases

    return cases


def _tier_params(purpose: str, limit: int, min_n: int) -> tuple[int, int]:
    """(n, effective_min_n) for a purpose by risk tier. RISKY (RISK_NOTES /
    advisor_*) gets 16/16; the rest the CANDIDATE default 12/8. ``limit`` caps n
    (quick manual runs); the effective minimum never exceeds the achievable n
    and never drops below the CLI ``min_n`` floor."""
    if risk_note_for(purpose):
        tier_n, tier_min = RISKY_TIER_N, RISKY_TIER_MIN_N
    else:
        tier_n, tier_min = DEFAULT_TIER_N, DEFAULT_TIER_MIN_N
    n = min(tier_n, limit) if limit > 0 else tier_n
    return n, max(min_n, min(tier_min, n))


# ---------------------------------------------------------------------------
# Active-purpose discovery via llm_calls
# ---------------------------------------------------------------------------


def _discover_active_purposes(
    db_path: Path,
    *,
    lookback_days: int,
    explicit_purposes: list[str] | None,
) -> list[str]:
    """Return purposes seen in llm_calls in the last ``lookback_days`` days.

    If ``explicit_purposes`` is given, filter to those (still checking the DB
    so we skip purposes with no recent calls — avoids wasted capture lookups).
    Falls back to the explicit list if the DB query errors (e.g. table absent).
    """
    if explicit_purposes is not None and not db_path.exists():
        return list(explicit_purposes)

    cutoff = datetime.now(UTC).replace(tzinfo=None)
    from datetime import timedelta

    cutoff_str = (cutoff - timedelta(days=lookback_days)).isoformat()

    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT purpose
            FROM llm_calls
            WHERE purpose IS NOT NULL
              AND (scope IS NULL OR scope != 'model_eval')
              AND called_at >= ?
            ORDER BY purpose
            """,
            (cutoff_str,),
        ).fetchall()
        conn.close()
        active = [r["purpose"] for r in rows]
    except Exception as exc:
        log.warning("could not query llm_calls for active purposes: %s; using explicit list", exc)
        return list(explicit_purposes) if explicit_purposes else []

    if explicit_purposes is not None:
        explicit_set = set(explicit_purposes)
        active = [p for p in active if p in explicit_set]
        # Include explicitly requested purposes even if not recently active —
        # the caller knows best.
        for p in explicit_purposes:
            if p not in active:
                log.warning(
                    "purpose %r not active in llm_calls in last %d days; including anyway",
                    p,
                    lookback_days,
                )
                active.append(p)

    return active


# ---------------------------------------------------------------------------
# Per-candidate evaluation (adapted from eval_model_downgrade._evaluate_candidate)
# ---------------------------------------------------------------------------


def _judge_model_for(judge_backend: str) -> str | None:
    return "claude-opus-4-8" if judge_backend == CLAUDE else "gemini-3.1-pro-preview"


def _checklist_discrimination(case_audit: list[dict[str, object]]) -> float | None:
    """Fraction of checklist items that resolved non-tie across a candidate's
    cases (§3.5). Persistently ~0 ⇒ the derived criteria are too generic —
    tighten the deriver prompt. ``None`` when no case carried a checklist."""
    total = 0
    non_tie = 0
    for row in case_audit:
        checklist = row.get("checklist")
        if not isinstance(checklist, dict):
            continue
        for side in cast("dict[str, object]", checklist).values():
            total += 1
            if side != "tie":
                non_tie += 1
    return (non_tie / total) if total else None


def _agreement(judged: list[tuple[str, object]], judges: list[str]) -> float:
    from llm.backend_judge import JudgedPair  # local import avoids circular at module level

    by_label: dict[str, list[str]] = {}
    for _jb, jp in judged:
        # Errored judgments are excluded: two judges both failing on a case is
        # not "agreement" — during the July-2026 CLI outage every failed pair
        # resolved to tie==tie and agreement read 1.0 while nothing was judged.
        if isinstance(jp, JudgedPair) and not jp.error:
            by_label.setdefault(jp.label, []).append(jp.winner)
    multi = [w for w in by_label.values() if len(w) >= 2]
    if not multi:
        return 0.0
    agree = sum(1 for w in multi if len(set(w)) == 1)
    return agree / len(multi)


def _evaluate_candidate(
    cases: list[PromptCase],
    *,
    purpose: str,
    incumbent: str,
    candidate: str,
    candidate_backend: str,
    judges: list[str],
    run_id: str,
    min_n: int,
    parity_threshold: float,
    timeout_seconds: int | None,
    criteria_blocks: dict[str, str] | None = None,
) -> tuple[CandidateVerdict, list[dict[str, object]]]:
    """Returns the verdict AND the per-case judge audit (label, judge, winner,
    margin, rationales, checklist) so the full decision trail lands in
    summary_json. ``criteria_blocks`` maps case label -> the rendered per-case
    checklist (§3) — judge-side only; the generation replay never sees it."""
    from llm.backend_judge import JudgedPair

    tally: dict[str, list[int]] = {jb: [0, 0, 0] for jb in judges}
    judged: list[tuple[str, object]] = []
    error_audit: list[dict[str, object]] = []
    cand_chars_total = 0
    inc_chars_total = 0
    n_ok = 0  # cases where the candidate succeeded (used for char mean denominator)
    n_errors = 0  # operational failures — decide_switch separates these from quality
    n_judgments = 0  # judge calls attempted (per case per judge)
    n_judge_errors = 0  # judge calls that errored — EXCLUDED from tallies

    for case in cases:
        cand = run_model(
            case.prompt,
            model_id=candidate,
            backend=candidate_backend,
            purpose=purpose,
            ticker=case.ticker,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )
        if not cand.ok:
            log.info("    case %s: candidate FAILED -> incumbent win (%s)", case.label, cand.error)
            n_errors += 1
            error_audit.append(
                {"label": case.label, "candidate_error": cand.error, "winner_model": incumbent}
            )
            for jb in judges:
                tally[jb][1] += 1
            continue
        cand_chars_total += cand.output_chars
        inc_chars_total += len(case.incumbent_response)
        n_ok += 1
        for jb in judges:
            jp = judge_case(
                case,
                cand.response,
                purpose=purpose,
                judge_backend=jb,
                judge_model=_judge_model_for(jb),
                run_id=run_id,
                criteria_block=(criteria_blocks or {}).get(case.label),
            )
            judged.append((jb, jp))
            n_judgments += 1
            if jp.error:
                # A failed judge call resolves to winner="tie" with .error set.
                # Booking it as a tie counted it toward parity_rate — during
                # the July-2026 CLI outage that path could have built a
                # SWITCH_DOWN streak out of pure transport failure. Excluded
                # from the tallies; counted for the JUDGE_DEGRADED verdict.
                n_judge_errors += 1
                error_audit.append(
                    {"label": case.label, "judge": jb, "judge_error": jp.error[:300]}
                )
                continue
            if jp.winner == GEMINI:  # GEMINI slot == candidate
                tally[jb][0] += 1
            elif jp.winner == CLAUDE:  # CLAUDE slot == incumbent
                tally[jb][1] += 1
            else:
                tally[jb][2] += 1

    case_audit: list[dict[str, object]] = list(error_audit)
    for jb, jp in judged:
        if not isinstance(jp, JudgedPair) or jp.error:
            continue  # errored judgments live in error_audit as judge_error rows
        winner_model = (
            candidate if jp.winner == GEMINI else (incumbent if jp.winner == CLAUDE else "tie")
        )
        case_audit.append(
            {
                "label": jp.label,
                "judge": jb,
                "winner_model": winner_model,
                "margin": jp.margin,
                "position_consistent": jp.position_consistent,
                "rationales": jp.rationales,
                # Per-item checklist outcomes (§3) — None when the case ran
                # facet-only (no criteria derived).
                "checklist": jp.checklist_winners,
            }
        )

    per_judge = {jb: (t[0], t[1], t[2]) for jb, t in tally.items()}
    cand_chars_mean = cand_chars_total / n_ok if n_ok else 0.0
    inc_chars_mean = inc_chars_total / n_ok if n_ok else 0.0
    verdict = decide_switch(
        purpose=purpose,
        incumbent=incumbent,
        candidate=candidate,
        per_judge=per_judge,
        judge_agreement=_agreement(judged, judges),
        min_n=min_n,
        parity_threshold=parity_threshold,
        candidate_output_chars_mean=cand_chars_mean,
        incumbent_output_chars_mean=inc_chars_mean,
        n_cases_attempted=len(cases),
        n_candidate_errors=n_errors,
        n_judgments_attempted=n_judgments,
        n_judge_errors=n_judge_errors,
    )
    return verdict, case_audit


# DB persistence: the sweep writes through llm.model_overrides.record_verdict
# (the single canonical writer, shared with the apply_model_switches reader) —
# see the call in run_sweep. The old divergent-schema _insert_verdict was removed
# when the duplicate 0084 migration was reconciled.


# ---------------------------------------------------------------------------
# Main sweep logic
# ---------------------------------------------------------------------------


def _work_items(
    db_path: Path,
    *,
    purposes: list[str] | None,
    lookback_days: int,
    nominations: list[Nomination] | None,
) -> list[tuple[str, list[str], int | None, int | None]]:
    """(purpose, candidates, suggested_min_n, nomination_row_id) work list.

    Nominated mode: pending model_downgrade nominations in priority order; the
    candidate list is re-checked at sweep time against the CURRENT merged
    frontier (the incumbent may have moved) with ``include_openrouter=True`` —
    a validated nomination IS the OpenRouter opt-in (§1.4/§6). An empty
    intersection marks the nomination ``skipped``.

    Default mode: today's discovery (static ladder, OpenRouter opt-out) MINUS
    live exclusions (Q2 — TTL'd + rotation-floored in ``excluded_purposes``).
    """
    if nominations:
        work: list[tuple[str, list[str], int | None, int | None]] = []
        for nom in nominations:
            if nom.kind != KIND_MODEL_DOWNGRADE:
                continue
            incumbent = LLM_MODELS.get(nom.purpose, DEFAULT_MODEL)
            allowed = set(merged_cheaper_candidates(db_path, incumbent, include_openrouter=True))
            cands = [c for c in nom.candidates if c in allowed]
            if not cands:
                log.info(
                    "[%s] nomination has no valid candidates at sweep time; skipped",
                    nom.purpose,
                )
                if nom.row_id is not None:
                    mark_nomination(db_path, nom.row_id, "skipped")
                continue
            work.append((nom.purpose, cands, nom.suggested_min_n, nom.row_id))
        return work

    active = _discover_active_purposes(
        db_path, lookback_days=lookback_days, explicit_purposes=purposes
    )
    excluded = excluded_purposes(db_path)
    if excluded & set(active):
        log.info(
            "excluded by live nominator exclusions (TTL'd, rotation-floored): %s",
            ", ".join(sorted(excluded & set(active))),
        )
        active = [p for p in active if p not in excluded]
    out: list[tuple[str, list[str], int | None, int | None]] = []
    for purpose in active:
        incumbent = LLM_MODELS.get(purpose, DEFAULT_MODEL)
        candidates = cheaper_candidates(incumbent)
        if not candidates:
            log.info("[%s] incumbent=%s is already cheapest; skip", purpose, incumbent)
            continue
        out.append((purpose, candidates, None, None))
    return out


def run_sweep(
    *,
    capture_dir: Path,
    db_path: Path,
    purposes: list[str] | None,
    judges: list[str],
    limit: int,
    lookback_days: int,
    min_n: int,
    parity_threshold: float,
    timeout_seconds: int | None,
    persist: bool,
    nominations: list[Nomination] | None = None,
    run_id: str | None = None,
) -> list[CandidateVerdict]:
    """Run one full sweep across all active purposes. Returns all verdicts made."""
    capture_files = _find_capture_files(capture_dir)
    if not capture_files:
        log.warning("no capture files found in %s; nothing to evaluate", capture_dir)
        return []

    log.info("capture files: %d file(s) in %s", len(capture_files), capture_dir)

    work = _work_items(
        db_path, purposes=purposes, lookback_days=lookback_days, nominations=nominations
    )
    if not work:
        log.info("no active purposes found; nothing to evaluate")
        return []

    log.info(
        "work items (%d, %s): %s",
        len(work),
        "nominated" if nominations else "discovered",
        ", ".join(p for p, _c, _m, _r in work),
    )

    run_id = run_id or uuid.uuid4().hex
    all_verdicts: list[CandidateVerdict] = []

    for purpose, candidates, suggested_min_n, nomination_row_id in work:
        incumbent = LLM_MODELS.get(purpose, DEFAULT_MODEL)

        n_target, eff_min_n = _tier_params(purpose, limit, min_n)
        if suggested_min_n is not None:
            # A nomination's suggested_min_n can only RAISE the bar; n follows
            # so the bar stays achievable (nomination authority > --limit cap).
            eff_min_n = max(eff_min_n, suggested_min_n)
            n_target = max(n_target, eff_min_n)

        # Census-grounded stratified sampling (§2). No census (fixture DBs,
        # bootstrap) → the legacy newest-N loader, visible in the manifest.
        census = load_census(db_path, purpose)
        frame = load_frame(capture_files, purpose) if census else {}
        legacy_cases: list[PromptCase] = []
        features: dict[str, str] = {}
        if census:
            if not frame:
                log.info("[%s] no capture cases found; skip this sweep", purpose)
                continue
            features, deferred = ensure_difficulty_features(
                db_path,
                purpose,
                frame,
                census,
                classifier_version=prompt_version_for(CLASSIFY_PURPOSE),
            )
            if deferred:
                log.info(
                    "[%s] %d sha(s) deferred to next sweep's classifier budget", purpose, deferred
                )
        else:
            legacy_cases = _load_cases_from_files(capture_files, purpose=purpose, limit=n_target)
            if not legacy_cases:
                log.info("[%s] no capture cases found; skip this sweep", purpose)
                continue

        log.info(
            "[%s] incumbent=%s  n=%d min_n=%d  census=%d frame=%d  candidates=%s",
            purpose,
            incumbent,
            n_target,
            eff_min_n,
            len(census),
            len(frame) or len(legacy_cases),
            ", ".join(candidates),
        )

        for candidate in candidates:
            candidate_backend = merged_backend_for(db_path, candidate)
            manifest: dict[str, object]
            if census:
                # Per-candidate draw: dedup excludes shas this pair graded in the
                # previous 2 sweeps (fresh evidence accumulates; a sha may still
                # serve a DIFFERENT candidate). Seeded → reproducible.
                sample = sample_cases(
                    purpose=purpose,
                    n=n_target,
                    min_n=eff_min_n,
                    census=census,
                    frame=frame,
                    features=features,
                    rng_seed=f"{run_id}:{purpose}:{candidate}",
                    exclude_shas=recent_sample_shas(db_path, purpose, candidate),
                )
                if sample.insufficient_frame:
                    verdict = CandidateVerdict(
                        purpose=purpose,
                        incumbent=incumbent,
                        candidate=candidate,
                        n=0,
                        candidate_wins=0,
                        incumbent_wins=0,
                        ties=0,
                        parity_rate=0.0,
                        judge_agreement=0.0,
                        recommendation=INSUFFICIENT_FRAME,
                        reason=sample.reason,
                    )
                    log.info("  %s -> INSUFFICIENT_FRAME: %s", candidate, sample.reason)
                    all_verdicts.append(verdict)
                    if persist and db_path.exists():
                        record_verdict(
                            purpose=purpose,
                            candidate=candidate,
                            incumbent=incumbent,
                            verdict=INSUFFICIENT_FRAME,
                            run_id=run_id,
                            parity_rate=0.0,
                            judge_agreement=0.0,
                            n_cases=0,
                            n_parity=0,
                            summary_json=json.dumps(
                                {
                                    "verdict": dataclasses.asdict(verdict),
                                    "sample_manifest": sample.manifest,
                                },
                                ensure_ascii=False,
                            ),
                            db_path=db_path,
                        )
                    continue
                cases = sample.cases
                manifest = sample.manifest
            else:
                cases = legacy_cases
                manifest = {"mode": "legacy_no_census", "n_drawn": len(legacy_cases)}

            # Per-case checklists (§3): derive-or-load per sha (cached forever;
            # the first candidate of a sweep pays derivation, the rest cache-hit).
            # A deriver failure degrades that case to facet-only — visible in
            # the criteria telemetry, never blocking.
            criteria_blocks: dict[str, str] = {}
            criteria_missing = 0
            criteria_version = prompt_version_for(CRITERIA_PURPOSE)
            for case in cases:
                if case.prompt_sha256 is None:
                    criteria_missing += 1
                    continue
                crits = derive_or_load(
                    db_path,
                    purpose,
                    case.prompt_sha256,
                    case.prompt,
                    criteria_version=criteria_version,
                )
                if crits is None:
                    criteria_missing += 1
                else:
                    criteria_blocks[case.label] = render_criteria_block(crits)

            log.info(
                "  evaluating candidate %s on %d case(s) (%d with checklists) ...",
                candidate,
                len(cases),
                len(criteria_blocks),
            )
            verdict, case_audit = _evaluate_candidate(
                cases,
                purpose=purpose,
                incumbent=incumbent,
                candidate=candidate,
                candidate_backend=candidate_backend,
                judges=judges,
                run_id=run_id,
                min_n=eff_min_n,
                parity_threshold=parity_threshold,
                timeout_seconds=timeout_seconds,
                criteria_blocks=criteria_blocks or None,
            )
            log.info(
                "  -> %s  (parity %.0f%%  agree %.0f%%)  %s",
                verdict.recommendation,
                verdict.parity_rate * 100,
                verdict.judge_agreement * 100,
                verdict.reason,
            )
            all_verdicts.append(verdict)

            if persist and db_path.exists():
                # Single canonical writer (shared with the auto-switch reader).
                # summary_json = full verdict + every per-case judge rationale +
                # the sample manifest, so the switch decision AND its sample's
                # provenance are auditable from the DB alone.
                summary_json = json.dumps(
                    {
                        "verdict": dataclasses.asdict(verdict),
                        "token_efficiency": {
                            "candidate_output_chars_mean": verdict.candidate_output_chars_mean,
                            "incumbent_output_chars_mean": verdict.incumbent_output_chars_mean,
                            "ratio": verdict.token_efficiency_ratio,
                        },
                        "cases": case_audit,
                        "sample_manifest": manifest,
                        # §3.5 quality-of-criteria telemetry: ~0 discrimination
                        # means the checklists are too generic; missing counts
                        # the facet-only degradations (a freshness-alarm input).
                        "criteria": {
                            "with_checklist": len(criteria_blocks),
                            "missing": criteria_missing,
                            "discrimination": _checklist_discrimination(case_audit),
                        },
                    },
                    ensure_ascii=False,
                )
                record_verdict(
                    purpose=verdict.purpose,
                    candidate=verdict.candidate,
                    incumbent=verdict.incumbent,
                    verdict=verdict.recommendation,
                    run_id=run_id,
                    parity_rate=verdict.parity_rate,
                    judge_agreement=verdict.judge_agreement,
                    n_cases=verdict.n,
                    n_parity=verdict.candidate_wins + verdict.ties,
                    summary_json=summary_json,
                    db_path=db_path,
                )
                log.info("  persisted to model_eval_verdicts")

        # This purpose's candidates all ran (or were honestly labeled) — the
        # nomination is consumed. A purpose skipped BEFORE the candidate loop
        # (no frame yet) stays pending for the next harvest to unblock.
        if nomination_row_id is not None and persist:
            mark_nomination(db_path, nomination_row_id, "swept")

    return all_verdicts


def _emit_summary(verdicts: list[CandidateVerdict]) -> None:
    if not verdicts:
        log.info("\n(no verdicts this sweep)")
        return
    log.info("\n=== Sweep summary ===")
    log.info("%-30s %-35s %-20s %s", "purpose", "candidate", "verdict", "parity")
    for v in sorted(verdicts, key=lambda v: (v.purpose, v.candidate)):
        log.info(
            "%-30s %-35s %-20s %.0f%%",
            v.purpose,
            v.candidate,
            v.recommendation,
            v.parity_rate * 100,
        )
    switches = [v for v in verdicts if v.recommendation == SWITCH_DOWN]
    if switches:
        log.info("\nSWITCH_DOWN recommendations (human PR required to apply):")
        for v in switches:
            log.info("  %s: %s -> %s", v.purpose, v.incumbent, v.candidate)
    else:
        log.info("\nNo SWITCH_DOWN recommendations this sweep.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Weekly sweep: evaluate cheaper model candidates for every active purpose."
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "llm_capture",
        help="directory containing capture_*.jsonl files (default: data/llm_capture)",
    )
    parser.add_argument(
        "--purposes",
        help="comma list of purposes to evaluate (default: all active in llm_calls)",
    )
    parser.add_argument(
        "--judges",
        default=f"{CLAUDE},{GEMINI}",
        help=f"comma judge backends (default '{CLAUDE},{GEMINI}')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CASES_PER_PURPOSE,
        help=f"max cases per purpose (default {DEFAULT_CASES_PER_PURPOSE})",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"days back to scan llm_calls for active purposes (default {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--min-n",
        type=int,
        default=DEFAULT_MIN_N,
        help=f"min cases before a non-INSUFFICIENT verdict (default {DEFAULT_MIN_N})",
    )
    parser.add_argument(
        "--parity-threshold",
        type=float,
        default=DEFAULT_PARITY_THRESHOLD,
        help=f"wins-or-ties rate required for SWITCH_DOWN (default {DEFAULT_PARITY_THRESHOLD})",
    )
    parser.add_argument("--timeout", type=int, default=None, help="per-LLM-call timeout seconds")
    parser.add_argument(
        "--from-nominations",
        action="store_true",
        help="evaluate the PENDING optimizer nominations (priority order, merged "
        "frontier incl. OpenRouter) instead of discovering active purposes",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="evaluate but write no DB rows",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="repo root whose data/portfolio.db is used",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    judges = [j.strip() for j in str(args.judges).split(",") if j.strip()]
    valid_judges = (CLAUDE, GEMINI, "deepseek", "codex")
    if not judges or any(j not in valid_judges for j in judges):
        parser.error(f"--judges must be a comma list of {valid_judges}; got {args.judges!r}")

    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"

    if db_path.exists():
        import db

        db.set_db_path(db_path)

    explicit_purposes: list[str] | None = None
    if args.purposes:
        explicit_purposes = [p.strip() for p in str(args.purposes).split(",") if p.strip()]

    capture_dir = args.capture_dir
    if not capture_dir.is_absolute():
        capture_dir = repo_root / capture_dir

    nominations: list[Nomination] | None = None
    if args.from_nominations:
        nominations = pending_nominations(db_path)
        if not nominations:
            log.info("--from-nominations: no pending nominations; falling back to discovery")
            nominations = None

    run_id = uuid.uuid4().hex
    started_at = datetime.now(UTC)
    try:
        verdicts = run_sweep(
            capture_dir=capture_dir,
            db_path=db_path,
            purposes=explicit_purposes,
            judges=judges,
            limit=args.limit,
            lookback_days=args.lookback_days,
            min_n=args.min_n,
            parity_threshold=args.parity_threshold,
            timeout_seconds=args.timeout,
            persist=not args.no_persist,
            nominations=nominations,
            run_id=run_id,
        )
    except Exception:
        receipt = build_eval_run_receipt(
            ["CANDIDATE_ERRORED"],
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        path = persist_eval_run_receipt(repo_root, receipt, dry_run=args.no_persist)
        log.error("eval sweep receipt: %s", path)
        raise

    _emit_summary(verdicts)
    receipt = build_eval_run_receipt(
        [verdict.recommendation for verdict in verdicts],
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    path = persist_eval_run_receipt(repo_root, receipt, dry_run=args.no_persist)
    log.info("eval sweep receipt: %s", path)
    log.info(
        "eval sweep counts: attempted=%d graded=%d insufficient=%d errors=%d alerts=%d",
        receipt.attempted,
        receipt.graded,
        receipt.insufficient,
        receipt.errors,
        receipt.alert_count,
    )
    return 0 if receipt.status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
