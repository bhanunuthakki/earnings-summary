"""Evaluate whether a purpose can switch DOWN to a cheaper model.

For one purpose: take a corpus of real prompts (the incumbent's responses are
reused from a capture/harvest — no re-run), run each cheaper candidate model on
the same prompts, judge incumbent-vs-candidate head to head (brand-blind,
position-swapped, dual judge), and emit a per-candidate switch verdict. The
cheapest candidate that holds parity is the downgrade recommendation.

This is the generalization of compare_backends/grade_backends to model-space:
"cheaper candidate" includes cheaper Claude tiers AND Gemini (see model_ladder).

Usage:
    # incumbent = LLM_MODELS[bear_case]; candidates = everything cheaper.
    python execution/eval_model_downgrade.py --purpose bear_case \\
        --from-capture data/llm_capture/capture_<date>.jsonl --repo-root <MAIN>

    # restrict to a specific candidate (e.g. only test the cheaper Claude tier):
    python execution/eval_model_downgrade.py --purpose bear_case \\
        --from-capture <jsonl> --candidates claude-haiku-4-5-20251001 --repo-root <MAIN>

Output: data/model_eval/verdicts_<runid>.jsonl (one row per (candidate, judge))
+ summary_<runid>.json (the switch decision per candidate) + a console table.
Run with capture OFF so eval traffic never lands in a harvest corpus.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from llm.backend_judge import CLAUDE, GEMINI, JudgedPair  # noqa: E402
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.model_eval import (  # noqa: E402
    CandidateVerdict,
    PromptCase,
    decide_switch,
    judge_case,
    run_model,
)
from llm.model_ladder import cheaper_candidates, estimated_call_usd, model_cost  # noqa: E402

log = logging.getLogger("eval_model_downgrade")

VALID_JUDGES = (CLAUDE, GEMINI)


def _load_cases(capture_path: Path, purpose: str, limit: int | None) -> list[PromptCase]:
    """Load real prompts for ``purpose`` from a capture JSONL. The captured
    response was produced by the incumbent pin, so it IS the incumbent response —
    reused directly (no re-run). Deduped by prompt_sha256."""
    seen: set[str] = set()
    cases: list[PromptCase] = []
    for line in capture_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parsed: object = json.loads(line)
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
            PromptCase(label=label, prompt=prompt, ticker=ticker_s, incumbent_response=response)
        )
        if limit is not None and len(cases) >= limit:
            break
    return cases


def _judge_model_for(judge_backend: str) -> str | None:
    # The judge purpose is pinned to Opus on the Claude side; Gemini side resolves
    # to Pro. The driver lets backend_judge resolve it (judge_model is advisory
    # metadata here), so we just label it.
    return "claude-opus-4-8" if judge_backend == CLAUDE else "gemini-2.5-pro"


def _agreement(judged: list[tuple[str, JudgedPair]], judges: list[str]) -> float:
    """Fraction of cases (labels) on which all judges agreed on the winner."""
    by_label: dict[str, list[str]] = {}
    for _jb, jp in judged:
        by_label.setdefault(jp.label, []).append(jp.winner)
    multi = [winners for winners in by_label.values() if len(winners) >= 2]
    if not multi:
        return 0.0
    agree = sum(1 for winners in multi if len(set(winners)) == 1)
    return agree / len(multi)


def _evaluate_candidate(
    cases: list[PromptCase],
    *,
    purpose: str,
    incumbent: str,
    candidate: str,
    judges: list[str],
    run_id: str,
    min_n: int,
    parity_threshold: float,
    timeout_seconds: int | None,
) -> tuple[CandidateVerdict, list[dict[str, object]]]:
    """Run the candidate on every case, judge vs the incumbent (dual judge),
    tally, and decide. A candidate that FAILS a case counts as an incumbent win
    (a model that can't produce output for the purpose isn't switch-worthy)."""
    judged: list[tuple[str, JudgedPair]] = []
    # per judge: [cand_wins, inc_wins, ties]
    tally: dict[str, list[int]] = {jb: [0, 0, 0] for jb in judges}
    rows: list[dict[str, object]] = []

    for case in cases:
        cand = run_model(
            case.prompt,
            model_id=candidate,
            purpose=purpose,
            ticker=case.ticker,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )
        if not cand.ok:
            log.info("    %s candidate FAILED -> incumbent win (%s)", case.label, cand.error)
            for jb in judges:
                tally[jb][1] += 1  # incumbent win
            continue
        for jb in judges:
            jp = judge_case(
                case,
                cand.response,
                purpose=purpose,
                judge_backend=jb,
                judge_model=_judge_model_for(jb),
                run_id=run_id,
            )
            judged.append((jb, jp))
            if jp.winner == GEMINI:  # GEMINI slot == candidate
                tally[jb][0] += 1
            elif jp.winner == CLAUDE:  # CLAUDE slot == incumbent
                tally[jb][1] += 1
            else:
                tally[jb][2] += 1
            row = dataclasses.asdict(jp)
            row["judge"] = jb
            row["candidate"] = candidate
            row["incumbent"] = incumbent
            # Relabel winner into incumbent/candidate terms for the ledger.
            row["winner_model"] = (
                candidate if jp.winner == GEMINI else (incumbent if jp.winner == CLAUDE else "tie")
            )
            rows.append(row)

    per_judge = {jb: (t[0], t[1], t[2]) for jb, t in tally.items()}
    verdict = decide_switch(
        purpose=purpose,
        incumbent=incumbent,
        candidate=candidate,
        per_judge=per_judge,
        judge_agreement=_agreement(judged, judges),
        min_n=min_n,
        parity_threshold=parity_threshold,
    )
    return verdict, rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate whether a purpose can switch to a cheaper model."
    )
    parser.add_argument(
        "--purpose", required=True, help="the purpose to evaluate (key in LLM_MODELS)"
    )
    parser.add_argument(
        "--from-capture",
        type=Path,
        required=True,
        help="capture JSONL with real prompts for the purpose",
    )
    parser.add_argument(
        "--candidates",
        help="comma list of candidate model ids (default: every model cheaper than the incumbent)",
    )
    parser.add_argument(
        "--judges",
        default=f"{CLAUDE},{GEMINI}",
        help=f"comma judge backends (default '{CLAUDE},{GEMINI}')",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="evaluate only the first N prompt cases"
    )
    parser.add_argument(
        "--min-n", type=int, default=4, help="min cases before a non-INSUFFICIENT call"
    )
    parser.add_argument(
        "--parity-threshold",
        type=float,
        default=0.8,
        help="candidate wins-or-ties rate (per judge) required to SWITCH_DOWN",
    )
    parser.add_argument("--timeout", type=int, default=None, help="per-call timeout seconds")
    parser.add_argument("--out", type=Path, default=None, help="verdicts JSONL output path")
    parser.add_argument(
        "--no-persist", action="store_true", help="evaluate + print, write no files"
    )
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="repo whose data/ + DB are used"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    judges = [j.strip() for j in str(args.judges).split(",") if j.strip()]
    if not judges or any(j not in VALID_JUDGES for j in judges):
        parser.error(f"--judges must be a comma list of {VALID_JUDGES}; got {args.judges!r}")

    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if db_path.exists():
        import db

        db.set_db_path(db_path)

    purpose = str(args.purpose)
    incumbent = LLM_MODELS.get(purpose, DEFAULT_MODEL)
    if args.candidates:
        candidates = [c.strip() for c in str(args.candidates).split(",") if c.strip()]
    else:
        candidates = cheaper_candidates(incumbent)

    cases = _load_cases(args.from_capture, purpose, args.limit)
    log.info("purpose=%s  incumbent=%s  cases=%d", purpose, incumbent, len(cases))
    if not candidates:
        log.info("no cheaper candidate than %s (already cheapest tier). nothing to do.", incumbent)
        return 0
    if not cases:
        log.info("no prompt cases for %s in %s.", purpose, args.from_capture)
        return 0
    log.info("candidates (cheapest first): %s", ", ".join(candidates))

    run_id = uuid.uuid4().hex
    verdicts: list[CandidateVerdict] = []
    all_rows: list[dict[str, object]] = []
    for candidate in candidates:
        log.info(
            "[eval] %s vs %s on %d case(s), judges=%s",
            candidate,
            incumbent,
            len(cases),
            ",".join(judges),
        )
        verdict, rows = _evaluate_candidate(
            cases,
            purpose=purpose,
            incumbent=incumbent,
            candidate=candidate,
            judges=judges,
            run_id=run_id,
            min_n=args.min_n,
            parity_threshold=args.parity_threshold,
            timeout_seconds=args.timeout,
        )
        verdicts.append(verdict)
        all_rows.extend(rows)
        log.info(
            "    -> %s  (cand %d / inc %d / tie %d, parity %.0f%%, agree %.0f%%) %s",
            verdict.recommendation,
            verdict.candidate_wins,
            verdict.incumbent_wins,
            verdict.ties,
            verdict.parity_rate * 100,
            verdict.judge_agreement * 100,
            verdict.reason,
        )

    # The switch decision: cheapest candidate that cleared SWITCH_DOWN.
    from llm.model_eval import SWITCH_DOWN

    switch_to = next((v.candidate for v in verdicts if v.recommendation == SWITCH_DOWN), None)

    log.info("")
    log.info("run_id=%s  purpose=%s", run_id, purpose)
    log.info(
        "%-30s %-10s %-12s %6s %6s  %s",
        "candidate",
        "verdict",
        "cand/inc/tie",
        "parity",
        "agree",
        "~$/call vs inc",
    )
    inc_cost_ref = estimated_call_usd(incumbent, 25_000, 5_000)
    for v in verdicts:
        cand_cost = estimated_call_usd(v.candidate, 25_000, 5_000)
        saving = inc_cost_ref - cand_cost
        log.info(
            "%-30s %-10s %2d/%2d/%2d %9.0f%% %5.0f%%  $%.4f vs $%.4f (save $%.4f)",
            v.candidate,
            v.recommendation,
            v.candidate_wins,
            v.incumbent_wins,
            v.ties,
            v.parity_rate * 100,
            v.judge_agreement * 100,
            cand_cost,
            inc_cost_ref,
            saving,
        )
    if switch_to is not None:
        log.info(
            "\nRECOMMENDATION: switch %s from %s -> %s (cheapest at-parity candidate).",
            purpose,
            incumbent,
            switch_to,
        )
        log.info("This is ADVISORY — the allowlist/pin is changed by a human PR citing this run.")
    else:
        log.info(
            "\nRECOMMENDATION: keep %s on %s (no cheaper model reached parity).", purpose, incumbent
        )

    if args.no_persist:
        log.info("\n--no-persist: wrote no files.")
        return 0

    out_path = args.out or (repo_root / "data" / "model_eval" / f"verdicts_{run_id[:8]}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with out_path.open("w", encoding="utf-8") as fh:
        for row in all_rows:
            row["run_id"] = run_id
            row["recorded_at"] = recorded_at
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary_path = out_path.with_name(f"summary_{run_id[:8]}.json")
    inc_cost = model_cost(incumbent)
    summary = {
        "run_id": run_id,
        "recorded_at": recorded_at,
        "purpose": purpose,
        "incumbent": incumbent,
        "incumbent_cost": dataclasses.asdict(inc_cost) if inc_cost is not None else None,
        "n_cases": len(cases),
        "judges": judges,
        "switch_to": switch_to,
        "verdicts": [dataclasses.asdict(v) for v in verdicts],
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("\nwrote %s", out_path)
    log.info("wrote %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
