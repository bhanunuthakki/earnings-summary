"""Extract structured footnote_facts from 10-K Item 8.

Thin CLI adapter over ``src/filings/footnote_drivers.py``. Reads retained SEC
10-K text in ``data/sec_text/<TICKER>_10k_<FY>.txt`` by default; pass
``--fetch`` to permit network retrieval via ``filing_text_fetcher``.

Usage:
    python execution/extract_footnotes.py --ticker GOOG --db /path/to/db
    python execution/extract_footnotes.py --ticker GOOG --db /path/to/db --fetch
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_paths import db_path_context, require_product_db_path  # noqa: E402
from filing_text_fetcher import fetch_latest_10k_text  # noqa: E402
from filings.footnote_drivers import (  # noqa: E402
    extract_footnote_drivers_from_text,
    load_cached_item_8_text,
    persist_footnote_drivers,
)
from llm.structured import call_llm_structured  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("extract_footnotes")


def extract_for_ticker(
    *,
    ticker: str,
    repo_root: Path,
    user_agent: str,
    db_path: Path | None = None,
    fiscal_year: int | None = None,
    allow_fetch: bool = True,
) -> dict[str, object]:
    """Extract and persist footnote drivers for one ticker."""
    upper = ticker.upper()
    cached = load_cached_item_8_text(
        repo_root=repo_root,
        ticker=upper,
        fiscal_year=fiscal_year,
    )
    resolved_fy: int
    item_8_text: str | None = None
    if cached is not None:
        resolved_fy, item_8_text = cached
    elif allow_fetch:
        fetched = fetch_latest_10k_text(
            ticker=upper,
            user_agent=user_agent,
            cache_dir=repo_root / "data" / "sec_text",
            fiscal_year=fiscal_year,
        )
        if fetched is None or not fetched.item_8_text:
            return {"ticker": upper, "status": "no_item_8", "n": 0}
        resolved_fy = fetched.fiscal_year
        item_8_text = fetched.item_8_text
    else:
        return {"ticker": upper, "status": "missing_retained_text", "n": 0}

    with db_path_context(db_path):
        outcome = extract_footnote_drivers_from_text(
            ticker=upper,
            fiscal_year=resolved_fy,
            item_8_text=item_8_text,
            llm_caller=call_llm_structured,
        )
    if outcome.status != "ok":
        return {"ticker": upper, "fiscal_year": resolved_fy, "status": outcome.status, "n": 0}

    target_db = (
        db_path
        if db_path is not None
        else (fallback if (fallback := repo_root / "data" / "portfolio.db").exists() else None)
    )
    inserted = 0
    if target_db is not None:
        conn = connect_sqlite(target_db, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            inserted = persist_footnote_drivers(
                conn,
                ticker=upper,
                fiscal_year=resolved_fy,
                rows=outcome.accepted_rows,
            )
        finally:
            conn.close()

    return {
        "ticker": upper,
        "fiscal_year": resolved_fy,
        "n_proposed": len(outcome.accepted_rows) + outcome.rejected_count,
        "n_rejected": outcome.rejected_count,
        "n_inserted": inserted,
        "status": "ok",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker.")
    g.add_argument("--all-portfolio", action="store_true", help="Run for all portfolio holdings.")
    parser.add_argument("--db", type=Path, default=None, help="Explicit database path.")
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Allow network retrieval from SEC EDGAR when retained text is missing.",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get(
            "EDGAR_USER_AGENT", "earnings-summary research/0.1 (analyst@example.com)"
        ),
        help="SEC EDGAR User-Agent. Set EDGAR_USER_AGENT in env (recommended).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root = args.repo_root.resolve()
    db_path = require_product_db_path(repo_root, explicit=args.db)

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            tickers = [
                str(r[0])
                for r in conn.execute(
                    "SELECT ticker FROM tracked_companies "
                    "WHERE archived_at IS NULL AND list_type = 'portfolio' ORDER BY ticker"
                )
            ]
        finally:
            conn.close()

    total = 0
    for t in tickers:
        outcome = extract_for_ticker(
            ticker=t,
            repo_root=repo_root,
            db_path=db_path,
            user_agent=args.user_agent,
            fiscal_year=args.fiscal_year,
            allow_fetch=args.fetch,
        )
        log.info(outcome)
        n_inserted = outcome.get("n_inserted", 0)
        if isinstance(n_inserted, int) and not isinstance(n_inserted, bool):
            total += n_inserted
    print(f"\nFootnote extraction done · {total} rows inserted across {len(tickers)} ticker(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
