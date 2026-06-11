"""Weekly model-downgrade evaluation sweep — standing accumulating loop.

Discovers which purposes are active (via recent llm_calls), loads prompt cases
from capture JSONL files, evaluates every cheaper candidate per purpose, and
INSERTs rows into model_eval_verdicts (rolling history — never upserts).

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

from llm.backend_judge import CLAUDE, GEMINI  # noqa: E402
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.model_eval import (  # noqa: E402
    SWITCH_DOWN,
    CandidateVerdict,
    PromptCase,
    decide_switch,
    judge_case,
    run_model,
)
from llm.model_ladder import cheaper_candidates  # noqa: E402

log = logging.getLogger("run_model_eval_sweep")

# Lookback window for discovering active purposes from llm_calls.
DEFAULT_LOOKBACK_DAYS = 30

# Default cases sampled per purpose (keeps each sweep under ~1hr wall-clock
# even if a purpose has hundreds of captures). Override via --limit.
DEFAULT_CASES_PER_PURPOSE = 6

# Minimum cases before a verdict is anything but INSUFFICIENT_DATA.
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
        conn = sqlite3.connect(str(db_path), timeout=10.0)
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
    return "claude-opus-4-8" if judge_backend == CLAUDE else "gemini-2.5-pro"


def _agreement(judged: list[tuple[str, object]], judges: list[str]) -> float:
    from llm.backend_judge import JudgedPair  # local import avoids circular at module level

    by_label: dict[str, list[str]] = {}
    for _jb, jp in judged:
        if isinstance(jp, JudgedPair):
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
    judges: list[str],
    run_id: str,
    min_n: int,
    parity_threshold: float,
    timeout_seconds: int | None,
) -> CandidateVerdict:
    tally: dict[str, list[int]] = {jb: [0, 0, 0] for jb in judges}
    judged: list[tuple[str, object]] = []

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
            log.info("    case %s: candidate FAILED -> incumbent win (%s)", case.label, cand.error)
            for jb in judges:
                tally[jb][1] += 1
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

    per_judge = {jb: (t[0], t[1], t[2]) for jb, t in tally.items()}
    return decide_switch(
        purpose=purpose,
        incumbent=incumbent,
        candidate=candidate,
        per_judge=per_judge,
        judge_agreement=_agreement(judged, judges),
        min_n=min_n,
        parity_threshold=parity_threshold,
    )


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


def _insert_verdict(
    db_path: Path,
    verdict: CandidateVerdict,
    *,
    evaluated_at: datetime,
) -> None:
    """INSERT one verdict row into model_eval_verdicts (rolling history)."""
    summary = dataclasses.asdict(verdict)
    n_parity = verdict.candidate_wins + verdict.ties
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute(
            """
            INSERT INTO model_eval_verdicts
                (purpose, incumbent_model, candidate_model, verdict,
                 judge_agreement, n_cases, n_parity, evaluated_at, summary_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verdict.purpose,
                verdict.incumbent,
                verdict.candidate,
                verdict.recommendation,
                verdict.judge_agreement,
                verdict.n,
                n_parity,
                evaluated_at.isoformat(),
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main sweep logic
# ---------------------------------------------------------------------------


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
) -> list[CandidateVerdict]:
    """Run one full sweep across all active purposes. Returns all verdicts made."""
    capture_files = _find_capture_files(capture_dir)
    if not capture_files:
        log.warning("no capture files found in %s; nothing to evaluate", capture_dir)
        return []

    log.info("capture files: %d file(s) in %s", len(capture_files), capture_dir)

    active_purposes = _discover_active_purposes(
        db_path,
        lookback_days=lookback_days,
        explicit_purposes=purposes,
    )
    if not active_purposes:
        log.info("no active purposes found; nothing to evaluate")
        return []

    log.info("active purposes (%d): %s", len(active_purposes), ", ".join(active_purposes))

    run_id = uuid.uuid4().hex
    evaluated_at = datetime.now(UTC).replace(tzinfo=None)
    all_verdicts: list[CandidateVerdict] = []

    for purpose in active_purposes:
        incumbent = LLM_MODELS.get(purpose, DEFAULT_MODEL)
        candidates = cheaper_candidates(incumbent)
        if not candidates:
            log.info("[%s] incumbent=%s is already cheapest; skip", purpose, incumbent)
            continue

        cases = _load_cases_from_files(capture_files, purpose=purpose, limit=limit)
        if not cases:
            log.info("[%s] no capture cases found; skip this sweep", purpose)
            continue

        log.info(
            "[%s] incumbent=%s  cases=%d  candidates=%s",
            purpose,
            incumbent,
            len(cases),
            ", ".join(candidates),
        )

        for candidate in candidates:
            log.info("  evaluating candidate %s ...", candidate)
            verdict = _evaluate_candidate(
                cases,
                purpose=purpose,
                incumbent=incumbent,
                candidate=candidate,
                judges=judges,
                run_id=run_id,
                min_n=min_n,
                parity_threshold=parity_threshold,
                timeout_seconds=timeout_seconds,
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
                _insert_verdict(db_path, verdict, evaluated_at=evaluated_at)
                log.info("  persisted to model_eval_verdicts")

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
    valid_judges = (CLAUDE, GEMINI)
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
    )

    _emit_summary(verdicts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
