"""Run the discovery pipelines and land candidates in the approval queue.

Factor screens over the index-member universe (canonical financial facts — no
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

Scoring (the Discovery rule; ``src/discovery/scoring.py``): a weighted sum of
typed, dated signals through the ``discovery_sources`` weight registry, NOT an
equal-weight count. Each screen pass and each adjacency channel is a typed
``Signal`` carrying its source's editable ``base_weight`` and a recency decay.
A NEW name must clear ``ENTRY_THRESHOLD`` to enter the queue at all; an existing
candidate is always refreshed. The candidate's ``score_json`` carries the
per-class ``score_why`` the panel peeks.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pydantic import TypeAdapter

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from db_paths import require_db_path
from discovery.adjacency import AdjacencyHit, mine_adjacency
from discovery.need_rank import (
    NeedRank,
    compute_need_rank,
    need_rank_to_json,
)
from discovery.scoring import (
    ScoreResult,
    Signal,
    adjacency_signal,
    score_candidate,
    screen_signal,
)
from discovery.screens import ScreenHit, run_screens
from discovery.sources import load_source_map, weight_for
from discovery.store import (
    CandidateWrite,
    SignalWrite,
    load_discovery_refresh_state,
    persist_discovery_refresh,
)
from identity import DEFAULT_USER_ID
from runtime.job_runtime import portfolio_db_path
from sources.discovery_financials import GrowthFinancials

#: How many interim-ranked candidates get the (heavier, price-history-reading)
#: coarse diversifier leg each run — the PRD's "reusing the candidate-fit/ΔSR
#: machinery at coarse precision" for the names that matter, not every
#: candidate the screens/adjacency turned up.
NEED_RANK_DIVERSIFIER_TOP_N = 25

# The screen+adjacency run owns exactly these signal classes.
_FUNDAMENTAL_CLASSES: tuple[str, ...] = ("screen", "adjacency")


def _new_evidence() -> list[dict[str, object]]:
    return []


def _new_signals() -> list[Signal]:
    return []


@dataclass(slots=True)
class _Acc:
    """Per-ticker accumulator across both pipelines."""

    name: str | None = None
    evidence: list[dict[str, object]] = field(default_factory=_new_evidence)
    signals: list[Signal] = field(default_factory=_new_signals)


@dataclass(slots=True)
class _AdjAgg:
    """Per (ticker, channel) adjacency aggregate before it becomes one Signal."""

    count: int = 0
    holdings: list[str] = field(default_factory=list[str])


def discover(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | None = None,
    growth_coverage_sink: Callable[[GrowthFinancials], None] | None = None,
    include_screens: bool = True,
    include_adjacency: bool = True,
    per_holding_transcripts: int = 4,
    min_transcript_mentions: int = 3,
) -> list[tuple[str, float, int]]:
    """Run the pipelines, score by weighted typed signals, persist the signals
    and the scored candidates. Returns (ticker, score, evidence_count) tuples
    sorted by score for the caller's summary."""
    if db_path is None:
        db_path = portfolio_db_path(repo_root)
        if db_path.resolve() == (repo_root / "data" / "portfolio.db").resolve():
            raise RuntimeError(
                "Discovery requires an explicit configured database; checkout database is prohibited"
            )
    db_path = require_db_path(db_path)
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    source_map = load_source_map(db_path=db_path)
    as_of = date.today()

    financial_coverage: dict[str, dict[str, object]] = {}

    def collect_financial_coverage(ticker: str, evidence: dict[str, object]) -> None:
        financial_coverage[ticker] = evidence

    screen_hits: list[ScreenHit] = (
        run_screens(
            db_path,
            fmp_dir,
            user_id=user_id,
            growth_coverage_sink=growth_coverage_sink,
            financial_coverage_sink=collect_financial_coverage,
        )
        if include_screens
        else []
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
        acc.evidence.append(
            {"source": f"screen:{sh.screen}", "detail": sh.detail, "calculation": sh.evidence}
        )
        acc.signals.append(
            screen_signal(
                sh.screen,
                weight=weight_for(source_map, sh.screen),
                observed_at=as_of,
                detail=sh.detail,
            )
        )

    # Adjacency: aggregate every mention of a ticker through one channel into a
    # single weighted signal whose strength is the (capped) holding count.
    adj_agg: dict[tuple[str, str], _AdjAgg] = {}
    for ah in adjacency_hits:
        _slot(ah.ticker, ah.name)
        acc = by_ticker[ah.ticker]
        acc.evidence.append(
            {"source": f"adjacency:{ah.source}", "holding": ah.holding, "detail": ah.detail}
        )
        agg = adj_agg.setdefault((ah.ticker, ah.source), _AdjAgg())
        agg.count += 1
        agg.holdings.append(ah.holding)
    for (ticker, channel), agg in adj_agg.items():
        near = ", ".join(sorted(set(agg.holdings))[:5])
        detail = f"named near {len(set(agg.holdings))} holding(s): {near}"
        by_ticker[ticker].signals.append(
            adjacency_signal(
                channel,
                weight=weight_for(source_map, channel),
                observed_at=as_of,
                occurrences=float(agg.count),
                detail=detail,
            )
        )

    existing_rows, retained_signals = load_discovery_refresh_state(user_id=user_id, db_path=db_path)
    existing = set(existing_rows)
    active_classes = {
        key
        for key, enabled in (("screen", include_screens), ("adjacency", include_adjacency))
        if enabled
    }
    for ticker, coverage in financial_coverage.items():
        if ticker in existing and ticker not in by_ticker:
            previous = existing_rows.get(ticker)
            acc = _slot(ticker, previous.name if previous else None)
            acc.evidence.append(
                {
                    "source": "screen:coverage",
                    "detail": "No current canonical screen signal; see financial coverage",
                    "calculation": coverage,
                }
            )
    # An owned signal can disappear when a name leaves the screened universe
    # or loses its last adjacency mention. Refresh its candidate in the same
    # transaction as removing that signal, even without a new screen receipt.
    for ticker, previous in existing_rows.items():
        if any(
            item.signal_class in active_classes for item in retained_signals.get(ticker, [])
        ) or any(
            str(item.get("source", "")).split(":", 1)[0] in active_classes
            for item in previous.evidence
        ):
            _slot(ticker, previous.name)
    for ticker, acc in by_ticker.items():
        for retained in retained_signals.get(ticker, []):
            if retained.signal_class in active_classes:
                continue
            action = retained.meta.get("action")
            acc.signals.append(
                Signal(
                    signal_class=retained.signal_class,
                    source_key=retained.source_key,
                    weight=retained.weight,
                    raw_strength=retained.raw_strength,
                    observed_at=datetime.fromisoformat(retained.observed_at),
                    detail=retained.detail or "",
                    action=action if isinstance(action, str) else None,
                    style_tags=TypeAdapter(tuple[str, ...]).validate_python(
                        retained.meta.get("style_tags", ())
                    ),
                )
            )
        previous = existing_rows.get(ticker)
        if previous is not None:
            acc.evidence.extend(
                item
                for item in previous.evidence
                if str(item.get("source", "")).split(":", 1)[0] not in active_classes
            )
    qualifying: list[tuple[str, _Acc, ScoreResult]] = []
    for ticker, acc in by_ticker.items():
        result = score_candidate(acc.signals)
        # A NEW name must clear the entry bar; an existing candidate is always
        # refreshed (its score/lifecycle stays current even if it slips below).
        if ticker in existing or result.passes_entry:
            qualifying.append((ticker, acc, result))

    need_ranks = _compute_need_ranks(
        repo_root,
        db_path,
        {t: r.score for t, _a, r in qualifying},
        user_id=user_id,
    )

    candidate_writes: list[CandidateWrite] = []
    signal_writes: list[SignalWrite] = []
    results: list[tuple[str, float, int]] = []
    for ticker, acc, result in qualifying:
        why = dict(result.why)
        if ticker in financial_coverage:
            why["financial_coverage"] = financial_coverage[ticker]
        rank = need_ranks.get(ticker)
        if rank is not None:
            why["need_rank"] = need_rank_to_json(rank)
        candidate_writes.append(
            CandidateWrite(
                ticker=ticker,
                name=acc.name,
                score=result.score,
                evidence=acc.evidence,
                score_json=why,
            )
        )
        signal_writes.extend(_to_signal_writes(ticker, acc.signals))
        results.append((ticker, result.score, len(acc.evidence)))

    persist_discovery_refresh(
        candidate_writes, signal_writes, classes=active_classes, user_id=user_id, db_path=db_path
    )
    results.sort(key=lambda r: (-r[1], r[0]))
    return results


def _compute_need_ranks(
    repo_root: Path,
    db_path: Path,
    base_scores: dict[str, float],
    *,
    user_id: str,
) -> dict[str, NeedRank]:
    """P1-B (PRD §8.2): portfolio-need ranking for every qualifying candidate.

    Two passes so the (heavier, per-name price-history read) diversifier leg
    only runs for the interim top ``NEED_RANK_DIVERSIFIER_TOP_N`` — cheap legs
    (adjacency, GARP, effort, first-rejection) for everyone, then one shared
    ``BookContext`` assembly reused across the coarse-fit recompute for the
    names that actually matter. Never raises: an import/compute failure here
    must not take down the discovery run — a candidate simply keeps its cheap
    (diversifier=None) NeedRank."""
    from discovery.need_rank import load_eval_names

    fmp_dir = repo_root / "data" / "historical" / "fmp"
    eval_names = load_eval_names(db_path, fmp_dir, user_id=user_id)

    interim: dict[str, NeedRank] = {
        ticker: compute_need_rank(
            repo_root,
            db_path,
            ticker,
            base_score,
            eval_names=eval_names,
            coarse_fit=False,
            user_id=user_id,
        )
        for ticker, base_score in base_scores.items()
    }
    if not interim:
        return interim

    top = sorted(interim.items(), key=lambda kv: -kv[1].composite)[:NEED_RANK_DIVERSIFIER_TOP_N]
    top_tickers = {t for t, _r in top}
    if not top_tickers:
        return interim

    book = _assemble_book_context_safe(repo_root, db_path)
    if book is None:
        return interim

    for ticker in top_tickers:
        interim[ticker] = compute_need_rank(
            repo_root,
            db_path,
            ticker,
            base_scores[ticker],
            book=book,
            eval_names=eval_names,
            coarse_fit=True,
            user_id=user_id,
        )
    return interim


def _assemble_book_context_safe(repo_root: Path, db_path: Path) -> object | None:
    """The one shared ``BookContext`` the coarse diversifier pass reuses across
    every top-N candidate — assembled once (a tracker round-trip + cache
    reads), never a crash: a tracker outage or missing cache degrades to
    ``None`` (the diversifier leg then stays unset for this run, not faked)."""
    try:
        from candidate_fit_cache import assemble_book_context
    except ImportError:
        return None
    try:
        return assemble_book_context(repo_root, db_path=db_path)
    except Exception:  # a network/cache edge case must not take down discovery
        return None


def _to_signal_writes(ticker: str, signals: list[Signal]) -> list[SignalWrite]:
    """Persistence view of one candidate's screen and adjacency signals."""
    return [
        SignalWrite(
            ticker=ticker,
            signal_class=s.signal_class,
            source_key=s.source_key,
            weight=s.weight,
            raw_strength=s.raw_strength,
            observed_at=s.observed_at.isoformat(),
            detail=s.detail or None,
            meta={"action": s.action} if s.action else {},
        )
        for s in signals
        if s.signal_class in _FUNDAMENTAL_CLASSES
    ]


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

    growth_coverage: list[GrowthFinancials] = []
    results = discover(
        args.repo_root.resolve(),
        growth_coverage_sink=growth_coverage.append,
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
                "canonical_growth_coverage": dict(Counter(item.status for item in growth_coverage)),
                "canonical_growth_unavailable_reasons": dict(
                    Counter(reason for item in growth_coverage for reason in item.reason_codes)
                ),
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
