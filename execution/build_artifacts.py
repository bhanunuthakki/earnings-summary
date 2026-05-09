"""Build the unified research artifacts for a ticker.

Emits four files under output/research/{TICKER}/{DATE}_*:
  - {DATE}_report.html        long-form research doc (HTML, primary deliverable;
                              full transcripts embedded in §10 — single self-contained file)
  - {DATE}_report.md          markdown source (diff-friendly)
  - {DATE}_sections.json      frontend section payloads (consumed by /api/research/<ticker>)
  - {DATE}_dcf.xlsx           DCF workbook with supporting tabs

Usage:
    python execution/build_artifacts.py --ticker GOOG
    python execution/build_artifacts.py --ticker GOOG --repo-root /abs/path
    python execution/build_artifacts.py --all-tracked

For non-tracked tickers, pass --allow-untracked. The script emits a JSON event
log to stderr (one event per emitted file) and a path summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402  (must precede report imports — we override paths below)
from report.builder import build_report  # noqa: E402
from report.renderers.html import render as render_html  # noqa: E402
from report.renderers.markdown import render as render_markdown  # noqa: E402
from report.renderers.sections_json import render as render_sections_json  # noqa: E402
from report.renderers.workbook import render as render_workbook  # noqa: E402


def _sync_db_to_repo(repo_root: Path) -> None:
    """Override db module-level paths so scan_and_sync_artifacts hits the right repo.

    The build CLI runs from this checkout but reads/writes against an arbitrary
    --repo-root (typically the parent repo with the real data). The db module
    computes its paths from its own __file__ at import time; we patch them
    here so coverage syncs land in the right portfolio.db.
    """
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print("[]")
        return 0

    summary: list[dict[str, object]] = []
    for ticker in tickers:
        result = _build_one(ticker, repo_root, enable_llm=args.enable_llm)
        summary.append(result)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to build for")
    g.add_argument("--all-tracked", action="store_true", help="Build for all rows in tracked_companies")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, transcripts/, micro_thesis/. Default: this repo.",
    )
    parser.add_argument("--allow-untracked", action="store_true", help="Build even if ticker not in tracked_companies")
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Run the §7 bear-case LLM call. Routes through llm_client.call_llm: "
        "Claude CLI first (subscription billing), Gemini Flash fallback if Claude "
        "fails. Model selected via LLM_MODELS['bear_case']. Without --enable-llm, "
        "§7 is stubbed to keep the build offline.",
    )
    return parser.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        ticker = args.ticker.upper()
        if not args.allow_untracked and not _is_tracked(repo_root, ticker):
            _emit("warn_untracked", {"ticker": ticker, "hint": "pass --allow-untracked to build anyway"})
        return [ticker]
    return _all_tracked(repo_root)


def _is_tracked(repo_root: Path, ticker: str) -> bool:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM tracked_companies WHERE ticker = ? LIMIT 1", (ticker,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def _all_tracked(repo_root: Path) -> list[str]:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT ticker FROM tracked_companies WHERE list_type IN ('portfolio', 'watchlist') ORDER BY ticker"
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def _build_one(ticker: str, repo_root: Path, enable_llm: bool) -> dict[str, object]:
    out_dir = repo_root / "output" / "research" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    html_path = out_dir / f"{today}_report.html"
    md_path = out_dir / f"{today}_report.md"
    json_path = out_dir / f"{today}_sections.json"
    xlsx_path = out_dir / f"{today}_dcf.xlsx"

    # Refresh quarterly_artifacts (audio/transcript/release/slides + step_* flags)
    # from the filesystem before the provenance section reads them. This is what
    # links the coverage matrix to the DB — without this, new .tmp summaries and
    # SayDo files don't land in the table until something else syncs.
    _sync_db_to_repo(repo_root)
    db.scan_and_sync_artifacts(ticker)
    _emit("synced_quarterly_artifacts", {"ticker": ticker})

    spec = build_report(
        ticker=ticker,
        repo_root=repo_root,
        model_link=xlsx_path.name,
        enable_llm=enable_llm,
    )

    html_path.write_text(render_html(spec), encoding="utf-8")
    _emit("wrote_html", {"ticker": ticker, "path": str(html_path)})

    md_path.write_text(render_markdown(spec), encoding="utf-8")
    _emit("wrote_markdown", {"ticker": ticker, "path": str(md_path)})

    json_path.write_text(render_sections_json(spec), encoding="utf-8")
    _emit("wrote_sections_json", {"ticker": ticker, "path": str(json_path)})

    render_workbook(spec, xlsx_path)
    _emit("wrote_workbook", {"ticker": ticker, "path": str(xlsx_path)})

    return {
        "ticker": ticker,
        "report_html": str(html_path),
        "report_md": str(md_path),
        "sections_json": str(json_path),
        "dcf_xlsx": str(xlsx_path),
        "section_status": {
            "snapshot": spec.snapshot.status.value,
            "thesis": spec.thesis.status.value,
            "financials": spec.financials.status.value,
            "segments": spec.segments.status.value,
            "earnings": spec.earnings.status.value,
            "saydo": spec.saydo.status.value,
            "ir_docs": spec.ir_docs.status.value,
            "bear_case": spec.bear_case.status.value,
            "provenance": spec.provenance.status.value,
        },
    }


def _emit(event: str, payload: dict[str, object]) -> None:
    """One JSON line per event to stderr."""
    sys.stderr.write(json.dumps({"event": event, **payload}) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
