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

Scoring (the Discovery rule; ``src/discovery/scoring.py``): a weighted sum of
typed, dated signals through the ``discovery_sources`` weight registry, NOT an
equal-weight count. Each screen pass and each adjacency channel is a typed
``Signal`` carrying its source's editable ``base_weight`` and a recency decay;
the 13F miner adds ``investor_13f`` signals (clamped so an investor-only name
can surface but not top the queue). A NEW name must clear ``ENTRY_THRESHOLD``
to enter the queue at all; an existing candidate is always refreshed. The
candidate's ``score_json`` carries the per-class ``score_why`` the panel peeks.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from discovery.adjacency import AdjacencyHit, mine_adjacency  # noqa: E402
from discovery.scoring import (  # noqa: E402
    Signal,
    adjacency_signal,
    score_candidate,
    screen_signal,
)
from discovery.screens import ScreenHit, run_screens  # noqa: E402
from discovery.sources import SourceRow, load_source_map, weight_for  # noqa: E402
from discovery.store import (  # noqa: E402
    SignalWrite,
    existing_candidate_tickers,
    replace_signals,
    signals_by_ticker,
    upsert_candidate,
)
from identity import DEFAULT_USER_ID  # noqa: E402

# The screen+adjacency run owns exactly these signal classes; the 13F miner
# owns 'investor_13f'. replace_signals refreshes only the classes a producer
# owns, so the two never clobber each other's rows.
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
    include_screens: bool = True,
    include_adjacency: bool = True,
    per_holding_transcripts: int = 4,
    min_transcript_mentions: int = 3,
) -> list[tuple[str, float, int]]:
    """Run the pipelines, score by weighted typed signals, persist the signals
    and the scored candidates. Returns (ticker, score, evidence_count) tuples
    sorted by score for the caller's summary."""
    db_path = repo_root / "data" / "portfolio.db"
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    source_map = load_source_map(db_path=db_path)
    as_of = date.today()

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

    # Fold in the 13F miner's investor_13f signals (written by execution/
    # fetch_13f.py). run_discovery is the single scorer, so the clamp and
    # corroboration bump only work when one scorer sees every class — an
    # investor-only name enters here (and stays clamped below the fundamentals).
    _attach_investor_signals(by_ticker, _slot, source_map, db_path, user_id)

    existing = existing_candidate_tickers(user_id=user_id, db_path=db_path)
    signal_writes: list[SignalWrite] = []
    results: list[tuple[str, float, int]] = []
    for ticker, acc in by_ticker.items():
        result = score_candidate(acc.signals)
        # A NEW name must clear the entry bar; an existing candidate is always
        # refreshed (its score/lifecycle stays current even if it slips below).
        if ticker in existing or result.passes_entry:
            upsert_candidate(
                ticker=ticker,
                name=acc.name,
                score=result.score,
                evidence=acc.evidence,
                score_json=result.why,
                user_id=user_id,
                db_path=db_path,
            )
            signal_writes.extend(_to_signal_writes(ticker, acc.signals))
            results.append((ticker, result.score, len(acc.evidence)))

    replace_signals(signal_writes, classes=_FUNDAMENTAL_CLASSES, user_id=user_id, db_path=db_path)
    results.sort(key=lambda r: (-r[1], r[0]))
    return results


def _to_signal_writes(ticker: str, signals: list[Signal]) -> list[SignalWrite]:
    """Persistence view of one candidate's FUNDAMENTAL signals — investor_13f
    rows belong to the 13F miner, which replaces that class separately."""
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


def _attach_investor_signals(
    by_ticker: dict[str, _Acc],
    slot: Callable[[str, str | None], _Acc],
    source_map: dict[str, SourceRow],
    db_path: Path,
    user_id: str,
) -> None:
    """Load the 13F miner's persisted ``investor_13f`` signals and attach them
    to the per-ticker accumulators (creating a slot for an investor-only name),
    so the single scorer ranks every class together."""
    investor_rows = signals_by_ticker("investor_13f", user_id=user_id, db_path=db_path)
    if not investor_rows:
        return
    names = _ticker_names(db_path, user_id)
    for ticker, rows in investor_rows.items():
        acc = by_ticker.get(ticker) or slot(ticker, names.get(ticker))
        for r in rows:
            try:
                observed = datetime.fromisoformat(r.observed_at)
            except ValueError:
                continue  # an unparseable stamp can't decay correctly — skip it
            src = source_map.get(r.source_key)
            action = r.meta.get("action")
            acc.signals.append(
                Signal(
                    signal_class="investor_13f",
                    source_key=r.source_key,
                    weight=r.weight,
                    raw_strength=r.raw_strength,
                    observed_at=observed,
                    detail=r.detail or "",
                    action=action if isinstance(action, str) else None,
                    style_tags=src.style_tags if src is not None else (),
                )
            )
            acc.evidence.append({"source": f"investor:{r.source_key}", "detail": r.detail or ""})


def _ticker_names(db_path: Path, user_id: str) -> dict[str, str]:
    """``{ticker: name}`` across the tracked universe — fills the company name
    for an investor-only candidate (which has no screen/adjacency name).
    Best-effort: ``{}`` on a missing DB."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, name FROM tracked_companies WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {str(t).upper(): str(n) for t, n in rows if n}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


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
