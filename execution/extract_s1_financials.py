"""Extract audited financials from a cached S-1 into financial_facts.

For recently-IPO'd holdings (recently_ipod / data_anchor=s1) FMP returns empty
statement endpoints, so financial_facts is empty and the thesis / timeseries /
DCF layers have nothing to anchor on. This driver registers the cached S-1 text
as a `documents` row (source_type=sec_s1, tier=s1_provisional — the lowest
precedence), parses its income statement / balance sheet / cash-flow statement,
and writes the figures as financial_facts with extracted_by='s1'.

When FMP / SEC-XBRL later ingest the company's real filings, those higher-tier
rows supersede these for the same period (the loaders rank by source tier).

Usage:
    python execution/extract_s1_financials.py --ticker FRVO --repo-root /path/to/repo
    python execution/extract_s1_financials.py --ticker FRVO --dry-run
    python execution/extract_s1_financials.py --ticker FRVO --s1-path data/sec_text/FRVO_s1_2026.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute._common import insert_financial_facts  # noqa: E402
from compute.s1_financials import (  # noqa: E402
    EXTRACTED_BY,
    build_financial_facts,
    parse_s1_text,
)
from models.documents import DocType, FetchStatus, SourceType, tier_for_source_type  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def _resolve_s1_path(repo_root: Path, ticker: str, override: Path | None) -> Path | None:
    """Locate the cached S-1 text: --s1-path wins, else newest TICKER_s1_*.txt."""
    if override is not None:
        p = override if override.is_absolute() else repo_root / override
        return p if p.exists() else None
    candidates = sorted(
        (repo_root / "data" / "sec_text").glob(f"{ticker.upper()}_s1_*.txt"),
        reverse=True,
    )
    return candidates[0] if candidates else None


def _upsert_s1_document(
    conn,
    *,
    ticker: str,
    rel_path: str,
    sha256: str,
    period_end: datetime,
    raw_bytes_size: int,
    source_url: str | None,
) -> tuple[int, bool]:
    """Find the S-1 document by sha256, else insert it. Returns (doc_id, created)."""
    row = conn.execute(
        "SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha256,)
    ).fetchone()
    if row is not None:
        return int(row["id"]), False

    tier = tier_for_source_type(SourceType.SEC_S1).value
    cur = conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_start, period_end, file_path, "
        " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
        " parent_document_id, source_quality_tier) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?)",
        (
            ticker.upper(),
            SourceType.SEC_S1.value,
            DocType.SEC_S1.value,
            period_end,
            rel_path,
            sha256,
            datetime.now(),
            FetchStatus.OK.value,
            raw_bytes_size,
            source_url,
            tier,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Ticker symbol (e.g. FRVO).")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db and data/sec_text/. "
        "Defaults to the worktree root.",
    )
    parser.add_argument(
        "--s1-path",
        type=Path,
        default=None,
        help="Path to the cached S-1 text (relative to repo-root or absolute). "
        "Defaults to the newest data/sec_text/<TICKER>_s1_*.txt.",
    )
    parser.add_argument(
        "--source-url", default=None, help="Optional EDGAR URL recorded on the document row."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without touching the database.",
    )
    args = parser.parse_args()

    ticker = args.ticker.upper()
    repo_root: Path = args.repo_root.resolve()
    s1_path = _resolve_s1_path(repo_root, ticker, args.s1_path)
    if s1_path is None:
        print(
            json.dumps(
                {"ticker": ticker, "error": "no cached S-1 text found", "repo_root": str(repo_root)},
                indent=2,
            )
        )
        return 1

    text = s1_path.read_text(encoding="utf-8")
    data = parse_s1_text(text)
    if not data:
        print(json.dumps({"ticker": ticker, "error": "parsed no financial facts from S-1", "s1_path": str(s1_path)}, indent=2))
        return 1

    periods = sorted({d.period_end for d in data})
    summary = {
        "ticker": ticker,
        "s1_path": str(s1_path),
        "periods": [p.date().isoformat() for p in periods],
        "line_items": sorted({d.line_item for d in data}),
        "parsed_facts": len(data),
    }

    if args.dry_run:
        summary["dry_run"] = True
        print(json.dumps(summary, indent=2))
        return 0

    raw_bytes_size = len(text.encode("utf-8"))
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        rel_path = str(s1_path.relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        rel_path = str(s1_path).replace("\\", "/")
    latest_period = max(periods)

    conn = open_db(str(repo_root / "data" / "portfolio.db"))
    try:
        doc_id, created = _upsert_s1_document(
            conn,
            ticker=ticker,
            rel_path=rel_path,
            sha256=sha256,
            period_end=latest_period,
            raw_bytes_size=raw_bytes_size,
            source_url=args.source_url,
        )
        facts = build_financial_facts(data, source_doc_id=doc_id, ticker=ticker)
        inserted = insert_financial_facts(conn, facts, extracted_by=EXTRACTED_BY)
        conn.commit()
    finally:
        conn.close()

    summary.update(
        {
            "document_id": doc_id,
            "document_created": created,
            "facts_built": len(facts),
            "facts_inserted": inserted,
            "extracted_by": EXTRACTED_BY,
        }
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
