"""
execution/fetch_lly_sec_filings.py
-----------------------------------
Layer 3 CLI: Fetch LLY (Eli Lilly) recent 10-Q, 10-K, and earnings 8-K filings
from SEC EDGAR (free, no auth), strip HTML to plain text, and write to
transcripts/processed/ in the canonical `<TICKER>_Q<N>_<YYYY>.txt` filename
format so the existing market-signals extractor can pick them up.

LLY is held in the watchlist (not as a portfolio holding) as the cleanest
competitive cross-check for the NVO thesis — see
directives/nvo_external_sources.md and micro_thesis/holdings/NVO.json.

Outputs:
  transcripts/processed/LLY_Q<N>_<YYYY>.txt — one per quarterly period
  .tmp/lly_filings/LLY_filing_index.json — metadata for what was fetched
Stdout: one-line JSON summary.
Stderr: structured event logs.

Usage:
    python execution/fetch_lly_sec_filings.py
    python execution/fetch_lly_sec_filings.py --max-filings 8
    python execution/fetch_lly_sec_filings.py --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from lxml import html as lxml_html

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent

LLY_CIK = "0000059478"
LLY_TICKER = "LLY"

PROCESSED_DIR = PROJECT_ROOT / "transcripts" / "processed"
INDEX_DIR = PROJECT_ROOT / ".tmp" / "lly_filings"

INTEREST_FORMS = ("10-Q", "10-K", "8-K")
SEC_USER_AGENT = "InvestorResearchBot/1.0 contact@example.com"
SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
RATE_LIMIT_SECONDS = 0.2  # SEC asks for <=10 req/s; we run at 5/s


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _log(event: str, **kwargs: Any) -> None:
    payload = {"event": event, "ts": _now_utc().isoformat() + "Z", **kwargs}
    sys.stderr.write(json.dumps(payload) + "\n")


@dataclass(frozen=True)
class Filing:
    accession_number: str
    form: str
    filing_date: str  # YYYY-MM-DD
    primary_document: str
    period_year: int
    period_quarter: str  # Q1..Q4


def _parse_period(form: str, filing_date: str, primary_doc: str) -> tuple[int, str] | None:
    """Best-effort: derive (period_year, period_quarter) from doc filename.

    LLY's primary docs are named lly-YYYYMMDD.htm where YYYYMMDD is the
    period-end date. e.g. lly-20260331.htm = Q1 2026; lly-20251231.htm = Q4 2025.
    """
    m = re.search(r"lly-(\d{4})(\d{2})(\d{2})\.htm", primary_doc)
    if m is None:
        return None
    year = int(m.group(1))
    month = int(m.group(2))
    if form == "10-K":
        return year, "Q4"  # Annual report = full FY = anchored to Q4
    if month in (1, 2, 3):
        return year, "Q1"
    if month in (4, 5, 6):
        return year, "Q2"
    if month in (7, 8, 9):
        return year, "Q3"
    return year, "Q4"


def fetch_filing_metadata(max_filings: int) -> list[Filing]:
    """Pull SEC submissions JSON for LLY and filter to forms of interest."""
    url = f"{SEC_DATA}/submissions/CIK{LLY_CIK}.json"
    headers = {"User-Agent": SEC_USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    resp.raise_for_status()
    payload = resp.json()
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    docs = recent.get("primaryDocument", [])

    out: list[Filing] = []
    seen_periods: set[tuple[str, int, str]] = set()
    for i, form in enumerate(forms):
        if form not in INTEREST_FORMS:
            continue
        period = _parse_period(form, dates[i], docs[i])
        if period is None:
            continue
        # For 8-K, only keep if it looks like an earnings release (use file pattern)
        # 8-K earnings exhibits aren't always identifiable from the index alone;
        # we accept them and let the text content speak for itself.
        period_key = (form, *period)
        if period_key in seen_periods:
            continue
        seen_periods.add(period_key)
        out.append(
            Filing(
                accession_number=accs[i],
                form=form,
                filing_date=dates[i],
                primary_document=docs[i],
                period_year=period[0],
                period_quarter=period[1],
            )
        )
        if len(out) >= max_filings:
            break
    return out


def filing_url(filing: Filing) -> str:
    """Construct the canonical EDGAR Archives URL for the primary document."""
    acc_clean = filing.accession_number.replace("-", "")
    cik_int = int(LLY_CIK)
    return f"{SEC_BASE}/Archives/edgar/data/{cik_int}/{acc_clean}/{filing.primary_document}"


def fetch_html_bytes(url: str) -> bytes:
    headers = {"User-Agent": SEC_USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    resp.raise_for_status()
    return resp.content


def html_to_text(html_bytes: bytes) -> str:
    """Strip HTML to plain text using lxml; collapse whitespace."""
    tree = lxml_html.fromstring(html_bytes)
    # Remove script + style nodes
    for tag in tree.xpath("//script | //style"):
        parent = tag.getparent()
        if parent is not None:
            parent.remove(tag)
    text = tree.text_content()
    # Collapse whitespace runs but preserve paragraph separation
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def output_path_for(filing: Filing) -> Path:
    """Where the text version of this filing lands.

    Uses canonical TICKER_QN_YYYY.txt naming so the existing pipeline
    (market signals extractor, transcript index manager) picks it up.

    For form 10-Q / 10-K → primary file. For 8-K we suffix the form to avoid
    colliding with the same-period 10-Q text (which is the richer source).
    """
    base = f"{LLY_TICKER}_{filing.period_quarter}_{filing.period_year}"
    if filing.form in ("10-Q", "10-K"):
        return PROCESSED_DIR / f"{base}.txt"
    return PROCESSED_DIR / f"{base}_8K_{filing.accession_number.replace('-', '')}.txt"


def process_filing(filing: Filing, force: bool) -> dict[str, Any]:
    out_path = output_path_for(filing)
    if out_path.exists() and not force:
        _log(
            "skip_existing",
            form=filing.form,
            period=f"{filing.period_quarter}_{filing.period_year}",
            path=str(out_path),
        )
        return {
            "form": filing.form,
            "period": f"{filing.period_quarter}_{filing.period_year}",
            "status": "skipped",
            "path": str(out_path.relative_to(PROJECT_ROOT)),
        }

    url = filing_url(filing)
    _log(
        "fetching",
        form=filing.form,
        period=f"{filing.period_quarter}_{filing.period_year}",
        url=url,
    )
    html_bytes = fetch_html_bytes(url)
    text = html_to_text(html_bytes)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    _log(
        "saved",
        form=filing.form,
        period=f"{filing.period_quarter}_{filing.period_year}",
        path=str(out_path),
        chars=len(text),
    )
    return {
        "form": filing.form,
        "period": f"{filing.period_quarter}_{filing.period_year}",
        "status": "saved",
        "path": str(out_path.relative_to(PROJECT_ROOT)),
        "chars": len(text),
        "filing_date": filing.filing_date,
        "accession_number": filing.accession_number,
        "source_url": url,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch LLY recent SEC filings as plain text.")
    parser.add_argument("--max-filings", type=int, default=6, help="Max filings to fetch (default: 6)")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if text file exists")
    args = parser.parse_args()

    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    _log("metadata_fetch_start", ticker=LLY_TICKER, cik=LLY_CIK)
    filings = fetch_filing_metadata(args.max_filings)
    _log("metadata_fetch_done", count=len(filings))

    if not filings:
        sys.stderr.write("No filings of interest found.\n")
        sys.exit(1)

    results: list[dict[str, Any]] = []
    for f in filings:
        results.append(process_filing(f, args.force))
        time.sleep(RATE_LIMIT_SECONDS)

    index_path = INDEX_DIR / "LLY_filing_index.json"
    index_path.write_text(
        json.dumps({"fetched_at": _now_utc().isoformat() + "Z", "results": results}, indent=2),
        encoding="utf-8",
    )

    sys.stdout.write(
        json.dumps({
            "ticker": LLY_TICKER,
            "total_filings": len(filings),
            "saved": sum(1 for r in results if r["status"] == "saved"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "index_file": str(index_path.relative_to(PROJECT_ROOT)),
        })
        + "\n"
    )


if __name__ == "__main__":
    main()
