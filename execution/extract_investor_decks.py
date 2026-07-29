"""Extract strategic targets from cached investor decks.

Walks ir_documents/<TICKER>/ for deck-shaped PDFs (investor day,
capital-markets day, AGM, quarterly investor update) and runs the
LLM-backed extractor at src/table_extractors/investor_decks.py over each.

Rows land in strategic_targets (every target) and forward_looking_statements
(quantitative targets with resolvable target_period). Idempotent via the
unique index on (ticker, deck_doc_id, target_kind, target_period,
substr(narrative_excerpt, 1, 128)) — re-runs are no-ops.

Opportunistic: tickers with no deck PDFs cached are silently skipped (logged
at INFO with 'no_decks_found'). The CLI exits 0 even when zero rows land —
that just means the tracked universe doesn't have any investor decks yet,
not that anything is broken.

Usage:
    # One ticker
    python execution/extract_investor_decks.py --ticker ABNB

    # All tracked tickers (portfolio + watchlist + evaluation, archived excluded)
    python execution/extract_investor_decks.py --all-tracked

    # Force re-extraction (re-spend LLM budget on decks already processed)
    python execution/extract_investor_decks.py --ticker ABNB --force-refresh
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


def _sync_db_path(repo_root: Path) -> None:
    """Mirror execution/extract_document_tables.py:_sync_db_path so the
    artifact / ledger writers land in the caller's repo."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from table_extractors.investor_decks import extract_for_ticker  # noqa: E402

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger("extract_investor_decks")


def _tracked_tickers(db_path: Path) -> list[str]:
    """Active tracked tickers — portfolio, watchlist, evaluation; archived excluded."""
    if not db_path.exists():
        return []
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = conn.execute(
            """
            SELECT ticker FROM tracked_companies
            WHERE archived_at IS NULL
              AND list_type IN ('portfolio', 'watchlist', 'evaluation')
            ORDER BY ticker
            """
        ).fetchall()
        return [str(r[0]).upper() for r in rows]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to extract.")
    group.add_argument(
        "--all-tracked",
        action="store_true",
        help="All active tracked tickers (portfolio + watchlist + evaluation).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-run the LLM even on decks that already have strategic_targets rows.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Override repo root (default: this CLI's parent dir).",
    )
    parser.add_argument(
        "--ir-root",
        type=Path,
        default=None,
        help=(
            "Override ir_documents location. Default: <repo_root>/ir_documents. "
            "Use this when running from a worktree whose ir_documents/ symlink "
            "lives elsewhere."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)
    db_path = repo_root / "data" / "portfolio.db"

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = _tracked_tickers(db_path)
        if not tickers:
            log.warning({"event": "no_tracked_tickers", "db_path": str(db_path)})

    totals = {
        "tickers_processed": 0,
        "decks_found": 0,
        "decks_processed": 0,
        "rows_inserted": 0,
        "forward_inserted": 0,
        "llm_failed": 0,
        "parse_failed": 0,
    }

    for t in tickers:
        counts = extract_for_ticker(
            t,
            db_path,
            repo_root=repo_root,
            ir_root=args.ir_root,
            force_refresh=args.force_refresh,
        )
        totals["tickers_processed"] += 1
        for k in (
            "decks_found",
            "decks_processed",
            "rows_inserted",
            "forward_inserted",
            "llm_failed",
            "parse_failed",
        ):
            totals[k] += counts.get(k, 0)
        log.info({"event": "ticker_summary", "ticker": t, **counts})

    print(
        "\nInvestor deck extraction done · "
        f"{totals['tickers_processed']} ticker(s), "
        f"{totals['decks_found']} deck(s) found, "
        f"{totals['decks_processed']} processed, "
        f"{totals['rows_inserted']} strategic_targets row(s), "
        f"{totals['forward_inserted']} forward_looking_statements row(s)."
    )
    if totals["llm_failed"] or totals["parse_failed"]:
        print(
            f"  ⚠  {totals['llm_failed']} deck(s) failed LLM, "
            f"{totals['parse_failed']} failed to parse — see logs."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
