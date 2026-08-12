"""Register IR documents from explicitly-provided URLs — the browser-assisted fallback.

Some issuer IR sites tarpit or deny the headless crawler (the *page* refuses
automation) yet render perfectly in a REAL browser — ServiceNow is the canonical
case: headless gets an empty page, but a real Chrome session exposes the actual
Q4-CDN PDF links. For those names a browser agent (e.g. the Claude-in-Chrome
bridge) extracts the document URLs and hands them to this CLI, which bridges them
into the SAME pipeline the automated crawler uses:

    URLs -> manifest (ir_pipeline.manifest.merge_write)
         -> download + content-classify + register (fetch_ir_documents.process_ticker)
         -> ir_narrative anchor + brief_dirty  (so the docs feed --enable-llm)
         -> ir_fetch_status  (so the dashboard reflects the recovery)

So a browser-recovered name is first-class downstream, identical to an
auto-crawled one. The download uses fetch_ir_documents' browser-UA downloader, so
the file CDN serves the bytes even when the IR *page* tarpits automation (the
files usually live on a separate, non-blocking CDN — verified for q4cdn).

This is deliberately NOT a cron: it needs a real browser to source the URLs, so
it is an on-demand assist for the handful of names the headless crawler can't
crack. robots.txt is honored by the crawler; this registers public, human-/agent-
curated document URLs.

Usage:
    python execution/fetch_ir_from_urls.py --ticker NOW \
        --url https://s205.q4cdn.com/916135447/files/doc_financials/2026/q1/ER-Q1-FY26.pdf \
        --url https://s205.q4cdn.com/916135447/files/doc_financials/2026/q1/ServiceNow-1Q26-Investor-Presentation.pdf
    python execution/fetch_ir_from_urls.py --ticker NOW --urls-file now_urls.txt
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import ir_fetch_status  # noqa: E402
from execution.fetch_ir_documents import process_ticker  # noqa: E402
from ir_pipeline.manifest import ManifestEntry, merge_write  # noqa: E402
from ir_uploads import calendar_id_from_fye  # noqa: E402
from pipeline.source_policy import (  # noqa: E402
    ArtifactKind,
    CollectionSource,
    authorize_stored_collection_target,
    reported_quarter_is_in_window,
)
from runtime.python_process import managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _ticker_fye(db_path: Path, ticker: str) -> str | None:
    """``tracked_companies.fiscal_year_end`` ("MM-DD") for ``ticker``, or None."""
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ? LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    val = row["fiscal_year_end"]
    return str(val) if val else None


def _set_brief_dirty(db_path: Path, ticker: str) -> None:
    """Queue the ticker for the daily ``--enable-llm`` rebuild (best-effort)."""
    if not db_path.exists():
        return
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            conn.execute(
                "UPDATE tracked_companies SET brief_dirty = 1 WHERE ticker = ?", (ticker.upper(),)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _run_narrative(ticker: str, repo_root: Path) -> None:
    """Refresh the IR anchor (cheap pypdf extraction) so the new docs feed prompts."""
    subprocess.run(
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(PROJECT_ROOT / "src" / "compute" / "ir_narrative.py"),
            "--ticker",
            ticker,
            "--repo-root",
            str(repo_root),
        ],
        cwd=str(PROJECT_ROOT),
        check=False,
    )


def collect_urls(url_args: list[str] | None, urls_file: Path | None) -> list[str]:
    """Union of explicit URLs + non-empty, non-comment lines of ``urls_file`` (deduped, ordered)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in url_args or []:
        u = raw.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    if urls_file and urls_file.exists():
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            u = line.strip()
            if u and not u.startswith("#") and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def register_urls(
    ticker: str,
    urls: list[str],
    *,
    repo_root: Path,
    db_path: Path,
    year: int,
    quarter: int,
    process: bool = True,
) -> int:
    """Seed the manifest with ``urls`` then download+register (+anchor). Returns count downloaded."""
    ticker = ticker.upper()
    authorization = authorize_stored_collection_target(
        db_path,
        ticker,
        requested=True,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    in_window = authorization.fiscal_year_end_month is not None and reported_quarter_is_in_window(
        fiscal_year=year,
        fiscal_quarter=quarter,
        fiscal_year_end_month=authorization.fiscal_year_end_month,
        as_of=date.today(),
    )
    if not authorization.allowed or not in_window:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": ticker,
                    "reason": (
                        "reported_quarter_window_denied"
                        if authorization.allowed
                        else authorization.status.value
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    entries = [
        ManifestEntry(
            ticker=ticker,
            doc_type="document",
            url=u,
            year=year,
            quarter=f"Q{quarter}",
        )
        for u in urls
    ]
    added, total = merge_write(repo_root, ticker, entries)
    sys.stdout.write(f"[{ticker}] manifest: +{added} new, {total} total\n")

    calendar = calendar_id_from_fye(_ticker_fye(db_path, ticker))
    summary = process_ticker(
        ticker,
        root=repo_root,
        db_path=db_path,
        categorize=True,
        calendar=calendar,
        owner_requested=True,
    )
    downloaded = summary.get("downloaded")
    downloaded_n = downloaded if isinstance(downloaded, int) else 0

    if downloaded_n and process:
        _run_narrative(ticker, repo_root)
        _set_brief_dirty(db_path, ticker)

    ir_fetch_status.record_attempt(
        db_path,
        ticker,
        status="ok" if downloaded_n else "failed",
        discovered=len(urls),
        downloaded=downloaded_n,
        reason=None if downloaded_n else "no documents downloaded from the provided URLs",
    )
    sys.stdout.write(
        f"[{ticker}] done: downloaded={downloaded_n}, processed={bool(downloaded_n and process)}\n"
    )
    return downloaded_n


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root)
    db_path = cast("Path", args.db) if args.db else repo_root / "data" / "portfolio.db"
    urls = collect_urls(cast("list[str] | None", args.url), cast("Path | None", args.urls_file))
    if not urls:
        sys.stderr.write("No URLs provided (use --url and/or --urls-file).\n")
        return 2
    downloaded = register_urls(
        cast("str", args.ticker),
        urls,
        repo_root=repo_root,
        db_path=db_path,
        year=cast("int", args.year),
        quarter=cast("int", args.quarter),
        process=not cast("bool", args.no_process),
    )
    return 0 if downloaded else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker to register documents for")
    p.add_argument("--year", required=True, type=int, help="Reported fiscal year")
    p.add_argument("--quarter", required=True, type=int, choices=[1, 2, 3, 4])
    p.add_argument("--url", action="append", help="A document URL (repeatable)")
    p.add_argument(
        "--urls-file", type=Path, help="File with one document URL per line (# comments ok)"
    )
    p.add_argument(
        "--no-process",
        action="store_true",
        help="Register only; skip the ir_narrative anchor refresh + brief_dirty flag",
    )
    p.add_argument(
        "--db", type=Path, help="portfolio.db path (default: <repo-root>/data/portfolio.db)"
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
