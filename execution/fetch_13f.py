"""execution/fetch_13f.py — the EDGAR 13F-HR miner's writer (the Discovery
rule's investor lane).

Polls every active rostered manager (a ``discovery_sources`` row with a
``cik``), diffs its two latest 13F-HR filings, and routes each buy by the
ticker it maps to:

  * an UNTRACKED universe name → an ``investor_13f`` ``discovery_signals`` row
    (a discovery candidate; the next ``run_discovery`` scores it alongside the
    fundamentals, clamped so investor weight alone can't top the queue), and
  * a name already in the active book → a ``news`` row (informational; the
    45-day-lagged filing date keeps it out of the material-news recency window).

Each run REPLACES the ``investor_13f`` signal class (the same full-refresh the
screen/adjacency run does for its classes), so a position the manager has since
exited drops out. Best-effort: an unreachable manager / missing news table
degrades to a partial write rather than failing the run.

Usage:
    python execution/fetch_13f.py                 # all active rostered managers
    python execution/fetch_13f.py --repo-root /path
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from discovery.scoring import investor_signal  # noqa: E402
from discovery.sources import SourceRow, active_investor_sources, load_source_map  # noqa: E402
from discovery.store import SignalWrite, replace_signals  # noqa: E402
from discovery.thirteenf import InvestorHit, mine_13f  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402

_INVESTOR_CLASS = "investor_13f"


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _to_signal_write(hit: InvestorHit, source: SourceRow | None) -> SignalWrite:
    """One InvestorHit → a discovery_signals row, normalizing the position size
    to a comparable ``raw_strength`` through the same scoring constructor the
    fundamentals use."""
    weight = source.base_weight if source is not None else 1.0
    tags = source.style_tags if source is not None else ()
    sig = investor_signal(
        hit.source_key,
        weight=weight,
        observed_at=date.fromisoformat(hit.observed_at),
        pct_of_book=hit.pct_of_book,
        action=hit.action,
        style_tags=tags,
        detail=hit.detail,
    )
    return SignalWrite(
        ticker=hit.ticker,
        signal_class=_INVESTOR_CLASS,
        source_key=hit.source_key,
        weight=sig.weight,
        raw_strength=sig.raw_strength,
        observed_at=sig.observed_at.isoformat(),
        detail=hit.detail,
        meta={"action": hit.action, "pct_of_book": round(hit.pct_of_book, 4)},
    )


def _write_tracked_news(
    hits: list[InvestorHit], source_map: dict[str, SourceRow], db_path: Path
) -> int:
    """Write a `news` row per move on a TRACKED ticker. Best-effort: a missing
    `news` table (older DB) just skips this lane. Returns rows inserted."""
    from pydantic import ValidationError

    from news.store import NewsRow, drop_duplicate_stories, upsert_news_rows

    rows: list[NewsRow] = []
    for hit in hits:
        src = source_map.get(hit.source_key)
        display = src.display_name if src is not None else hit.source_key
        try:
            rows.append(
                NewsRow(
                    ticker=hit.ticker,
                    headline=f"{display} {hit.detail}",
                    url=f"{hit.filing_url}#{hit.source_key}",
                    published_at=f"{hit.observed_at} 00:00:00",
                    snippet=f"13F-HR position change disclosed by {display}.",
                    source="SEC EDGAR 13F",
                    source_feed="edgar_13f",
                )
            )
        except ValidationError:
            _log("thirteenf_news_rejected", ticker=hit.ticker, source=hit.source_key)
    if not rows:
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        fresh = drop_duplicate_stories(conn, rows)
        inserted, _deduped = upsert_news_rows(conn, fresh)
        return inserted
    except sqlite3.OperationalError as exc:
        _log("thirteenf_news_table_missing", error=str(exc)[:200])
        return 0
    finally:
        conn.close()


def persist_hits(
    hits: list[InvestorHit],
    *,
    db_path: Path,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, int]:
    """Persist mined hits: untracked → investor_13f signals (full-class
    replace), tracked → news rows. The network half is upstream (``mine_13f``),
    so this is unit-testable with hand-built hits."""
    source_map = load_source_map(db_path=db_path)
    untracked = [h for h in hits if not h.tracked]
    tracked = [h for h in hits if h.tracked]

    writes = [_to_signal_write(h, source_map.get(h.source_key)) for h in untracked]
    replace_signals(writes, classes=(_INVESTOR_CLASS,), user_id=user_id, db_path=db_path)

    news_written = _write_tracked_news(tracked, source_map, db_path) if tracked else 0
    return {
        "signals": len(writes),
        "untracked_names": len({h.ticker for h in untracked}),
        "tracked_news": news_written,
    }


def run(repo_root: Path, *, user_id: str = DEFAULT_USER_ID) -> dict[str, int]:
    """Mine every active rostered manager and persist. A roster with no
    CIK-resolved managers is a no-op (configured-yet-dormant), not an error."""
    db_path = repo_root / "data" / "portfolio.db"
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    sources = active_investor_sources(db_path=db_path)
    if not sources:
        _log("thirteenf_no_active_managers")
        return {"signals": 0, "untracked_names": 0, "tracked_news": 0, "managers": 0}
    hits = mine_13f(db_path, fmp_dir, sources=sources)
    summary = persist_hits(hits, db_path=db_path, user_id=user_id)
    summary["managers"] = len(sources)
    _log("thirteenf_done", **summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    args = parser.parse_args(argv)
    summary = run(args.repo_root.resolve(), user_id=args.user_id)
    print(json.dumps({"event": "thirteenf_run_done", **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
