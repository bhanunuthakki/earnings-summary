"""Extract structured footnote_facts from 10-K Item 8.

Item 8 is the long, dense "Financial Statements and Supplementary Data"
section. Buried in its 50-150 pages are the high-signal disclosures
analysts mostly skip:

  - Related-party transactions
  - Contingent liabilities (litigation reserves, indemnifications)
  - Lease commitments (operating + finance leases)
  - Stock-based compensation footnote (grant value vs P&L expense)
  - Off-balance-sheet items
  - Goodwill / impairment events
  - Segment realignment disclosures
  - Critical accounting estimate references

Pipeline:
  1. Fetch Item 8 text via filing_text_fetcher.
  2. Single Sonnet call with the Item 8 text + a structured-extraction
     prompt → JSON list of footnote_facts rows.
  3. Persist via INSERT (idempotent on a composite natural key).

Cost: Item 8 is large (~80-200KB of text); one Sonnet call per ticker per
year. Cached after first run.

Usage:
    python execution/extract_footnotes.py --ticker GOOG
    python execution/extract_footnotes.py --all-portfolio
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

from filing_text_fetcher import fetch_latest_10k_text  # noqa: E402
from llm_client import JSON_FENCE_RE, call_llm  # noqa: E402

log = logging.getLogger("extract_footnotes")

# Cap Item 8 input to keep prompts bounded. 120KB ~= 30K tokens; well under
# Sonnet's 200K context but big enough to capture every footnote.
_MAX_ITEM_8_CHARS = 120_000


_PROMPT = """You are extracting structured footnote disclosures from {ticker}'s
fiscal year {fiscal_year} 10-K Item 8 (Financial Statements + Supplementary Data).

Your job: surface ONLY the high-signal disclosures — the ones that 99% of
analysts skip but that matter for the investment thesis. SKIP routine
revenue-recognition policy boilerplate, generic depreciation schedules,
standard income tax descriptions.

For each disclosure you decide is high-signal, emit a JSON row with:

{{
  "fact_type": "lease_commitment" | "contingent_liability" | "related_party_tx" |
               "stock_based_comp" | "off_balance_sheet" | "goodwill_change" |
               "segment_realignment" | "restructuring_charge" | "litigation_reserve",
  "footnote_number": "Note 7" | "Note 12" | ... (when stated),
  "description_md": "<2-4 sentences with the SPECIFIC disclosure>",
  "amount": <number, the dollar amount when given>,
  "currency": "USD" | "EUR" | ... ,
  "unit": "thousands" | "millions" | "billions" | ... ,
  "due_period": "YYYY-MM-DD" (when a payment/maturity date is named, else null),
  "counterparty": "<named counterparty, when applicable>",
  "status": "active" | "resolved" | "reclassified",
  "source_excerpt": "<short verbatim quote, 50-200 chars max>"
}}

Rules:
  - Return ONLY a JSON array of rows. No commentary. No markdown fence.
  - 5-15 rows for most large 10-Ks; SKIP if no high-signal disclosures (return []).
  - The source_excerpt must be a real verbatim phrase from the input.
  - Amounts: if the text says "$2.4 billion", emit amount=2400, unit=millions.
  - PRIORITIZE: items that (a) name a specific dollar amount, (b) name a
    counterparty, (c) flag a change from prior year, or (d) have legal /
    regulatory implications.

INPUT TEXT (Item 8, may be truncated):
{item_8_text}
"""


def extract_for_ticker(
    *,
    ticker: str,
    repo_root: Path,
    user_agent: str,
    fiscal_year: int | None = None,
) -> dict[str, object]:
    cache_dir = repo_root / "data" / "sec_text"
    result = fetch_latest_10k_text(
        ticker=ticker, user_agent=user_agent, cache_dir=cache_dir, fiscal_year=fiscal_year
    )
    if result is None or not result.item_8_text:
        return {"ticker": ticker, "status": "no_item_8", "n": 0}

    item_8 = result.item_8_text[:_MAX_ITEM_8_CHARS]
    prompt = _PROMPT.format(
        ticker=ticker, fiscal_year=result.fiscal_year, item_8_text=item_8
    )

    try:
        raw = call_llm(
            prompt,
            purpose="footnote_extraction",
            ticker=ticker,
            model="claude-sonnet-4-6",
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "footnote_llm_failed", "ticker": ticker, "error": str(exc)})
        return {"ticker": ticker, "status": "llm_failed", "n": 0}

    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        log.warning({"event": "footnote_parse_failed", "ticker": ticker, "head": raw[:200]})
        return {"ticker": ticker, "status": "parse_failed", "n": 0}
    if not isinstance(rows, list):
        return {"ticker": ticker, "status": "not_a_list", "n": 0}

    # Persist
    db_path = repo_root / "data" / "portfolio.db"
    inserted = _persist_rows(
        ticker=ticker, fiscal_year=result.fiscal_year, rows=rows, db_path=db_path
    )
    return {
        "ticker": ticker,
        "fiscal_year": result.fiscal_year,
        "n_proposed": len(rows),
        "n_inserted": inserted,
        "status": "ok",
    }


def _persist_rows(
    *, ticker: str, fiscal_year: int, rows: list[object], db_path: Path
) -> int:
    if not db_path.exists():
        return 0
    period_end = datetime(fiscal_year, 12, 31).isoformat()  # rough; FMP gives precise dates
    conn = sqlite3.connect(str(db_path))
    inserted = 0
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue
            r = cast("dict[str, object]", row)
            fact_type = str(r.get("fact_type") or "").strip()
            if not fact_type:
                continue
            description = str(r.get("description_md") or "")
            amount_raw = r.get("amount")
            amount: float | None = None
            if isinstance(amount_raw, (int, float)):
                amount = float(amount_raw)
            try:
                conn.execute(
                    """
                    INSERT INTO footnote_facts(
                        ticker, period_end, fiscal_period_type,
                        footnote_number, fact_type, description_md,
                        amount, currency, unit, due_period, counterparty,
                        status, source_excerpt, extracted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ticker,
                        period_end,
                        "FY",
                        str(r.get("footnote_number") or ""),
                        fact_type,
                        description,
                        amount,
                        str(r.get("currency") or "USD"),
                        str(r.get("unit") or ""),
                        str(r.get("due_period") or "") or None,
                        str(r.get("counterparty") or "") or None,
                        str(r.get("status") or "active"),
                        str(r.get("source_excerpt") or "")[:1024],
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                continue
        conn.commit()
    finally:
        conn.close()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker.")
    g.add_argument("--all-portfolio", action="store_true", help="Run for all portfolio holdings.")
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--user-agent",
        default=os.environ.get(
            "EDGAR_USER_AGENT", "earnings-summary research/0.1 (analyst@example.com)"
        ),
        help="SEC EDGAR User-Agent. Set EDGAR_USER_AGENT in env (recommended).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db.PROJECT_ROOT = str(args.repo_root)
    db.DATA_DIR = str(args.repo_root / "data")
    db.DB_PATH = str(args.repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(args.repo_root / "data" / "historical" / "fmp")

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        conn = sqlite3.connect(str(args.repo_root / "data" / "portfolio.db"))
        tickers = [
            r[0]
            for r in conn.execute(
                "SELECT ticker FROM tracked_companies WHERE archived_at IS NULL AND list_type = 'portfolio' ORDER BY ticker"
            )
        ]
        conn.close()

    total = 0
    for t in tickers:
        outcome = extract_for_ticker(
            ticker=t,
            repo_root=args.repo_root,
            user_agent=args.user_agent,
            fiscal_year=args.fiscal_year,
        )
        log.info(outcome)
        total += int(outcome.get("n_inserted", 0))
    print(f"\nFootnote extraction done · {total} rows inserted across {len(tickers)} ticker(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
