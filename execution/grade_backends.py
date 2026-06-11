"""Grade a backend-compare corpus: is Gemini at parity with Claude, per purpose?

The judging half of the eval-gated Gemini backend. ``compare_backends.py`` writes
paired Claude/Gemini outputs to ``data/backend_compare/compare_<runid>.jsonl``;
this reads one such corpus, runs a brand-blind, position-swapped pairwise judge
(``src/llm/backend_judge.py``) over every record where BOTH backends produced a
response, and emits a per-purpose promotion recommendation. With ``--judges
claude,gemini`` (the default) it runs a dual judge — Claude Opus AND Gemini Pro —
and reports their cross-agreement, the headline signal that cancels same-family
favouritism.

The recommendation is ADVISORY: a PROMOTE_CANDIDATE verdict is the evidence you
cite in the PR that adds a purpose to ``GEMINI_BACKEND_ALLOWED_PURPOSES`` — not an
automatic gate. (See directives/gemini_backend.md.)

Usage:
    # Grade the most recent corpus with both judges.
    python execution/grade_backends.py --repo-root <MAIN repo>

    # Grade a specific compare run, Claude judge only, one purpose.
    python execution/grade_backends.py --run-id c64bbd98 --judges claude \\
        --purpose viewspec_compile --repo-root <MAIN repo>

Output: ``data/backend_compare/graded_<grade_runid>.jsonl`` (one line per judged
pair) + ``summary_<grade_runid>.json`` (rollups + cross-judge agreement), plus a
console table. Judge LLM calls go through ``call_llm`` under
``purpose="backend_compare_judge"`` and share the grade run_id so their cost joins
from ``llm_calls``. Each judged pair costs two judge calls per judge (the position
swap); a corpus where the Gemini side failed (e.g. pre-login) grades to all-skips
with a clear message rather than empty output.
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


from llm.backend_judge import (  # noqa: E402
    CLAUDE,
    GEMINI,
    JUDGE_PURPOSE,
    JudgedPair,
    aggregate_by_purpose,
    cross_judge_agreement,
    gradable_from_record,
    judge_pair,
)
from llm.cli import DEFAULT_MODEL, LLM_MODELS  # noqa: E402
from llm.gemini_backend import gemini_model_for  # noqa: E402

log = logging.getLogger("grade_backends")

VALID_JUDGES = (CLAUDE, GEMINI)


def _resolve_corpus(repo_root: Path, file_arg: Path | None, run_id: str | None) -> Path:
    """Pick the corpus JSONL: explicit --file, else --run-id, else newest."""
    if file_arg is not None:
        return file_arg
    compare_dir = repo_root / "data" / "backend_compare"
    if run_id is not None:
        stem = run_id[:8]
        candidate = compare_dir / f"compare_{stem}.jsonl"
        if not candidate.exists():
            matches = sorted(compare_dir.glob(f"compare_{stem}*.jsonl"))
            if not matches:
                raise FileNotFoundError(f"no compare corpus for run-id {run_id!r} in {compare_dir}")
            return matches[0]
        return candidate
    corpora = sorted(
        compare_dir.glob("compare_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not corpora:
        raise FileNotFoundError(f"no compare_*.jsonl corpus found in {compare_dir}")
    return corpora[0]


def _read_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parsed: object = json.loads(line)
        if isinstance(parsed, dict):
            records.append(cast("dict[str, object]", parsed))
    return records


def _judge_model_for(judge_backend: str) -> str:
    if judge_backend == CLAUDE:
        return LLM_MODELS.get(JUDGE_PURPOSE, DEFAULT_MODEL)
    return gemini_model_for(JUDGE_PURPOSE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grade a backend-compare corpus with a pairwise Claude-vs-Gemini judge."
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--file", type=Path, help="explicit corpus JSONL path")
    src.add_argument("--run-id", help="compare run id (full or 8-char) under data/backend_compare/")
    parser.add_argument(
        "--judges",
        default=f"{CLAUDE},{GEMINI}",
        help=f"comma list of judge backends (default '{CLAUDE},{GEMINI}'); each adds a position-swapped pass",
    )
    parser.add_argument("--purpose", help="grade only records with this purpose")
    parser.add_argument(
        "--limit", type=int, default=None, help="grade only the first N gradable records"
    )
    parser.add_argument(
        "--max-prompt-chars",
        type=int,
        default=8000,
        help="truncate the task prompt in the judge context",
    )
    parser.add_argument(
        "--min-n", type=int, default=3, help="min judged pairs before a non-INSUFFICIENT call"
    )
    parser.add_argument(
        "--promote-threshold",
        type=float,
        default=0.8,
        help="Gemini parity-or-better rate required for PROMOTE_CANDIDATE",
    )
    parser.add_argument("--out", type=Path, default=None, help="graded JSONL output path")
    parser.add_argument(
        "--no-persist", action="store_true", help="grade + print but write no files"
    )
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="repo whose data/ + DB are used"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    judges = [j.strip() for j in str(args.judges).split(",") if j.strip()]
    bad = [j for j in judges if j not in VALID_JUDGES]
    if bad or not judges:
        parser.error(f"--judges must be a comma list of {VALID_JUDGES}; got {args.judges!r}")

    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if db_path.exists():
        import db

        db.set_db_path(db_path)

    corpus_path = _resolve_corpus(repo_root, args.file, args.run_id)
    records = _read_records(corpus_path)
    gradables = [gradable_from_record(r) for r in records]
    if args.purpose is not None:
        gradables = [g for g in gradables if g.purpose == args.purpose]

    judgeable = [g for g in gradables if g.skip_reason is None]
    skipped = [g for g in gradables if g.skip_reason is not None]
    if args.limit is not None:
        judgeable = judgeable[: args.limit]

    log.info("corpus: %s", corpus_path)
    log.info(
        "%d record(s); %d judgeable, %d skipped (a backend did not succeed); judges=%s",
        len(gradables),
        len(judgeable),
        len(skipped),
        ",".join(judges),
    )
    for g in skipped:
        log.info("  skip [%s] %s - %s", g.purpose, g.label, g.skip_reason)

    if not judgeable:
        log.info("nothing to judge (no record has both backends succeeding).")
        return 0

    grade_run_id = uuid.uuid4().hex
    judge_models = {j: _judge_model_for(j) for j in judges}

    judged: list[JudgedPair] = []
    total = len(judgeable) * len(judges)
    done = 0
    for judge_backend in judges:
        for g in judgeable:
            done += 1
            log.info("[%d/%d] judge=%s [%s] %s", done, total, judge_backend, g.purpose, g.label)
            jp = judge_pair(
                purpose=g.purpose,
                label=g.label,
                ticker=g.ticker,
                claude_response=g.claude_response,
                gemini_response=g.gemini_response,
                task_prompt=g.task_prompt,
                judge_backend=judge_backend,
                judge_model=judge_models[judge_backend],
                run_id=grade_run_id,
                max_prompt_chars=args.max_prompt_chars,
            )
            judged.append(jp)
            verdict = jp.winner if jp.error is None else f"FAIL ({jp.error})"
            log.info(
                "      -> %s  margin=%.2f  consistent=%s",
                verdict,
                jp.margin,
                jp.position_consistent,
            )

    rollups = aggregate_by_purpose(
        judged, min_n=args.min_n, promote_win_or_tie_rate=args.promote_threshold
    )
    agreements = cross_judge_agreement(judged)

    # --- Console summary ----------------------------------------------------
    log.info("")
    log.info("grade_run_id=%s  corpus=%s", grade_run_id, corpus_path.name)
    log.info(
        "%-22s %-7s %3s  %-11s %7s %6s  %s",
        "purpose",
        "judge",
        "n",
        "G/C/T",
        "margin",
        "cons",
        "recommendation",
    )
    for r in rollups:
        log.info(
            "%-22s %-7s %3d  %-11s %+7.2f %5.0f%%  %s",
            str(r.purpose),
            r.judge_backend,
            r.n,
            f"{r.gemini_wins}/{r.claude_wins}/{r.ties}",
            r.signed_margin,
            r.position_consistent_rate * 100,
            r.recommendation,
        )
        log.info("%48s%s", "", f"  {r.reason}")
    if agreements:
        log.info("")
        log.info("cross-judge agreement (pairs seen by >=2 judges):")
        for c in agreements:
            log.info(
                "  %-22s %d/%d agree (%.0f%%)",
                str(c.purpose),
                c.n_agree,
                c.n_pairs,
                c.agreement_rate * 100,
            )

    # --- Persist ------------------------------------------------------------
    if args.no_persist:
        log.info("\n--no-persist: wrote no files.")
        return 0

    out_path = args.out or (
        repo_root / "data" / "backend_compare" / f"graded_{grade_run_id[:8]}.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with out_path.open("w", encoding="utf-8") as fh:
        for jp in judged:
            row = dataclasses.asdict(jp)
            row["grade_run_id"] = grade_run_id
            row["recorded_at"] = recorded_at
            row["corpus"] = corpus_path.name
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary_path = out_path.with_name(f"summary_{grade_run_id[:8]}.json")
    summary = {
        "grade_run_id": grade_run_id,
        "recorded_at": recorded_at,
        "corpus": str(corpus_path),
        "judges": judges,
        "judge_models": judge_models,
        "n_judgeable": len(judgeable),
        "n_skipped": len(skipped),
        "rollups": [dataclasses.asdict(r) for r in rollups],
        "cross_judge_agreement": [dataclasses.asdict(c) for c in agreements],
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("\nwrote %s", out_path)
    log.info("wrote %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
