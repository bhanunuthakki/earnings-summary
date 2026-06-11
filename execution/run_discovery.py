"""Run the discovery pipelines and land candidates in the approval queue.

Factor screens over the index-member universe (local FMP caches — no
network, no LLM) plus the adjacency miner over the holdings' competitive
watchlists, transcripts, and news rows (master build P5.3). Hits aggregate
per ticker into discovery_candidates (alembic 0081) with the "why
surfaced" evidence; re-running refreshes evidence and score but never
touches a candidate's status — dismissed stays dismissed, built stays
built. NOTHING here triggers an eval build: the P5.4 queue is the budget
gate.

Usage:
    python execution/run_discovery.py                  # screens + adjacency
    python execution/run_discovery.py --skip-adjacency
    python execution/run_discovery.py --repo-root /path --top 30

Scoring (rank-only, deliberately coarse): one point per screen passed plus
one per adjacency source that named the ticker (capped at 3) — a name that
screens well AND keeps coming up near the portfolio outranks either alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from discovery.adjacency import AdjacencyHit, mine_adjacency  # noqa: E402
from discovery.screens import ScreenHit, run_screens  # noqa: E402
from discovery.store import upsert_candidate  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402


def _new_evidence() -> list[dict[str, object]]:
    return []


def _new_sources() -> set[str]:
    return set()


@dataclass(slots=True)
class _Acc:
    """Per-ticker accumulator across both pipelines."""

    name: str | None = None
    evidence: list[dict[str, object]] = field(default_factory=_new_evidence)
    screens: int = 0
    adj_sources: set[str] = field(default_factory=_new_sources)


def discover(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    include_screens: bool = True,
    include_adjacency: bool = True,
    per_holding_transcripts: int = 4,
    min_transcript_mentions: int = 3,
) -> list[tuple[str, float, int]]:
    """Run the pipelines and upsert candidates. Returns (ticker, score,
    evidence_count) tuples sorted by score for the caller's summary."""
    db_path = repo_root / "data" / "portfolio.db"
    fmp_dir = repo_root / "data" / "historical" / "fmp"

    screen_hits: list[ScreenHit] = (
        run_screens(db_path, fmp_dir, user_id=user_id) if include_screens else []
    )
    adjacency_hits: list[AdjacencyHit] = (
        mine_adjacency(
            repo_root,
            db_path,
            user_id=user_id,
            fmp_dir=fmp_dir,
            per_holding_transcripts=per_holding_transcripts,
            min_transcript_mentions=min_transcript_mentions,
        )
        if include_adjacency
        else []
    )

    by_ticker: dict[str, _Acc] = {}

    def _slot(ticker: str, name: str | None) -> _Acc:
        acc = by_ticker.setdefault(ticker, _Acc())
        if acc.name is None and name:
            acc.name = name
        return acc

    for sh in screen_hits:
        acc = _slot(sh.ticker, sh.name)
        acc.evidence.append({"source": f"screen:{sh.screen}", "detail": sh.detail})
        acc.screens += 1

    for ah in adjacency_hits:
        acc = _slot(ah.ticker, ah.name)
        acc.evidence.append(
            {"source": f"adjacency:{ah.source}", "holding": ah.holding, "detail": ah.detail}
        )
        acc.adj_sources.add(ah.source)

    results: list[tuple[str, float, int]] = []
    for ticker, acc in by_ticker.items():
        score = float(acc.screens + min(len(acc.adj_sources), 3))
        upsert_candidate(
            ticker=ticker,
            name=acc.name,
            score=score,
            evidence=acc.evidence,
            user_id=user_id,
            db_path=db_path,
        )
        results.append((ticker, score, len(acc.evidence)))
    results.sort(key=lambda r: (-r[1], r[0]))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--skip-screens", action="store_true")
    parser.add_argument("--skip-adjacency", action="store_true")
    parser.add_argument(
        "--transcripts-per-holding", type=int, default=4, help="recent calls mined per holding"
    )
    parser.add_argument(
        "--min-transcript-mentions",
        type=int,
        default=3,
        help="phrase count below this is noise, not adjacency",
    )
    parser.add_argument("--top", type=int, default=20, help="summary rows to print")
    args = parser.parse_args(argv)

    results = discover(
        args.repo_root.resolve(),
        user_id=args.user_id,
        include_screens=not args.skip_screens,
        include_adjacency=not args.skip_adjacency,
        per_holding_transcripts=args.transcripts_per_holding,
        min_transcript_mentions=args.min_transcript_mentions,
    )
    print(
        json.dumps(
            {
                "event": "discovery_run_done",
                "candidates_upserted": len(results),
                "top": [
                    {"ticker": t, "score": s, "evidence": n} for t, s, n in results[: args.top]
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
