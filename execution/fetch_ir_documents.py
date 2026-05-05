"""
execution/fetch_ir_documents.py
--------------------------------
Layer 3 CLI: Download IR documents (press releases, presentations, transcripts)
from investor relations websites for a given ticker.

Reads discovered URLs from .tmp/ir_url_manifest/<TICKER>_urls.json (populated by
the browser discovery sessions), downloads each PDF to ir_documents/<TICKER>/<YEAR>_<QUARTER>/,
and registers each in the document_index.

Usage:
    python execution/fetch_ir_documents.py --ticker GOOG
    python execution/fetch_ir_documents.py --ticker GOOG --verify
    python execution/fetch_ir_documents.py --all
"""

import os
import sys
import argparse
import json
import time
import logging
from pathlib import Path

import requests

# Paths
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from alias_manager import resolve_ticker
import index_manager

IR_DOCS_DIR = PROJECT_ROOT / "ir_documents"
URL_MANIFEST_DIR = PROJECT_ROOT / ".tmp" / "ir_url_manifest"

LOG_FORMAT = json.dumps({
    "level": "%(levelname)s",
    "ts": "%(asctime)s",
    "msg": "%(message)s",
})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)

# Request settings
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; InvestorResearchBot/1.0; "
        "+https://github.com/bhanunuthakki/earnings-summary)"
    )
})
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60


def load_url_manifest(ticker: str) -> list[dict]:
    """Load the URL manifest for a ticker from .tmp/ir_url_manifest/<TICKER>_urls.json."""
    path = URL_MANIFEST_DIR / f"{ticker.upper()}_urls.json"
    if not path.exists():
        log.warning({"event": "manifest_not_found", "ticker": ticker, "path": str(path)})
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    log.info({"event": "manifest_loaded", "ticker": ticker, "count": len(data)})
    return data


def dest_path(ticker: str, year, quarter: str, doc_type: str) -> Path:
    """Canonical local path for an IR document."""
    quarter = quarter.upper().lstrip("Q")
    folder = IR_DOCS_DIR / ticker.upper() / f"{year}_Q{quarter}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{doc_type}.pdf"


def download_pdf(url: str, dest: Path) -> bool:
    """Download a PDF from `url` to `dest`. Returns True on success."""
    if dest.exists():
        log.info({"event": "already_exists", "path": str(dest)})
        return True
    try:
        log.info({"event": "downloading", "url": url, "dest": str(dest)})
        resp = SESSION.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            # Some URLs serve HTML even for PDF links — still try to save
            log.warning({"event": "unexpected_content_type", "url": url, "content_type": content_type})

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        size_kb = dest.stat().st_size // 1024
        log.info({"event": "downloaded", "dest": str(dest), "size_kb": size_kb})
        return True

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "unknown"
        if status in (401, 403):
            log.error({"event": "auth_error", "url": url, "status": status})
            raise  # Auth errors: halt, don't retry
        log.error({"event": "http_error", "url": url, "status": status, "error": str(e)})
        return False

    except Exception as e:
        log.error({"event": "download_failed", "url": url, "error": str(e)})
        return False


def process_ticker(ticker: str, dry_run: bool = False) -> dict:
    """Download all URLs in the manifest for a ticker. Returns a summary dict."""
    ticker = resolve_ticker(ticker).upper()
    entries = load_url_manifest(ticker)
    if not entries:
        print(json.dumps({"ticker": ticker, "status": "no_manifest", "downloaded": 0, "skipped": 0, "failed": 0}))
        return {}

    downloaded = 0
    skipped = 0
    failed = 0

    for entry in entries:
        year = entry["year"]
        quarter = entry["quarter"]
        doc_type = entry["doc_type"]
        url = entry["url"]
        fiscal_label = entry.get("fiscal_label")
        note = entry.get("note")

        # Idempotency: check if already registered and file exists
        existing = index_manager.has_document(ticker, year, quarter, doc_type)
        local = existing.get("local_path") if existing else None
        if local and Path(local).exists():
            log.info({"event": "skip_registered", "ticker": ticker, "year": year, "quarter": quarter, "doc_type": doc_type})
            skipped += 1
            continue

        dest = dest_path(ticker, year, quarter, doc_type)

        if dry_run:
            print(f"[DRY RUN] Would download: {url} -> {dest}")
            skipped += 1
            continue

        ok = download_pdf(url, dest)
        time.sleep(0.5)  # Polite rate limit

        if ok:
            index_manager.register_ir_document(
                ticker=ticker,
                year=year,
                quarter=quarter,
                doc_type=doc_type,
                ir_url=url,
                local_path=str(dest),
                fiscal_label=fiscal_label,
                note=note,
                processed=False,
            )
            downloaded += 1
        else:
            # Register with no local_path so it can be retried
            index_manager.register_ir_document(
                ticker=ticker,
                year=year,
                quarter=quarter,
                doc_type=doc_type,
                ir_url=url,
                local_path=None,
                fiscal_label=fiscal_label,
                note=note,
                processed=False,
            )
            failed += 1

    summary = {"ticker": ticker, "status": "done", "downloaded": downloaded, "skipped": skipped, "failed": failed}
    print(json.dumps(summary))
    return summary


def verify_ticker(ticker: str) -> None:
    """Print verification status for all registered documents for a ticker."""
    ticker = resolve_ticker(ticker).upper()
    docs = index_manager.get_documents_for_ticker(ticker)
    if not docs:
        print(f"[{ticker}] No documents registered in document_index.")
        return
    for doc in docs:
        lp = doc.get("local_path")
        exists = Path(lp).exists() if lp else False
        status = "✓" if exists else ("✗ missing" if lp else "✗ no path")
        print(f"  [{status}] {ticker} {doc['year']} {doc['quarter']} {doc['doc_type']} — {lp or doc.get('ir_url', 'N/A')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download IR documents for a ticker from discovered URL manifests.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Company ticker (e.g. GOOG)")
    group.add_argument("--all", action="store_true", help="Process all tickers with a manifest in .tmp/ir_url_manifest/")
    parser.add_argument("--verify", action="store_true", help="Verify downloaded files exist; don't download")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded without doing it")
    args = parser.parse_args()

    if args.verify:
        if args.ticker:
            verify_ticker(args.ticker)
        elif args.all:
            for path in URL_MANIFEST_DIR.glob("*_urls.json"):
                ticker = path.stem.replace("_urls", "")
                verify_ticker(ticker)
        return

    if args.all:
        manifests = list(URL_MANIFEST_DIR.glob("*_urls.json"))
        if not manifests:
            print(f"No URL manifests found in {URL_MANIFEST_DIR}", file=sys.stderr)
            sys.exit(1)
        for path in manifests:
            ticker = path.stem.replace("_urls", "")
            process_ticker(ticker, dry_run=args.dry_run)
    else:
        process_ticker(args.ticker, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
