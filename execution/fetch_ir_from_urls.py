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
from pathlib import Path
from typing import cast

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

try:
    from .fetch_ir_documents import process_ticker
except ImportError:
    from fetch_ir_documents import process_ticker

import ir_fetch_status
from ir_pipeline.manifest import ManifestEntry, merge_write
from ir_uploads import calendar_id_from_fye
from pipeline.issuer_document_inventory import FiscalPeriodWitness
from pipeline.managed_ir_sources import (
    IssuerDocumentPreparationError,
    resolve_fiscal_period_witness,
)
from pipeline.reported_period_inventory import reported_quarter_selection_in_connection
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_stored_collection_target,
    current_utc_date,
)
from runtime.python_process import managed_python_prefix
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


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


def _set_brief_dirty(db_path: Path, ticker: str) -> bool:
    """Confirm the ticker was queued for the daily ``--enable-llm`` rebuild."""
    if not db_path.exists():
        return False
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            cursor = conn.execute(
                "UPDATE tracked_companies SET brief_dirty=1 WHERE ticker=?", (ticker.upper(),)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _run_narrative(ticker: str, repo_root: Path) -> bool:
    """Confirm deterministic IR anchor extraction completed."""
    try:
        result = subprocess.run(
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
    except OSError:
        return False
    return result.returncode == 0


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
    document_version_id: str | None = None,
) -> int:
    """Return ready URL entries, including repaired/unchanged registrations.

    Any candidate failure returns zero. Ready or repaired work still completes
    deterministic anchor/dirty effects when process=True, including coherent
    replay and partial attempts. Effect failure also returns zero.
    """
    ticker = ticker.upper()
    authorization = authorize_stored_collection_target(
        db_path,
        ticker,
        requested=True,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not authorization.allowed:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": ticker,
                    "reason": authorization.status.value,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    fiscal_witness: FiscalPeriodWitness | None = None
    if document_version_id is not None:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            fiscal_witness = resolve_fiscal_period_witness(
                conn,
                document_version_id,
                ticker=ticker,
                fiscal_year=year,
                fiscal_quarter=quarter,
            )
            witness_month = int(fiscal_witness.fiscal_year_end[:2])
            if (
                authorization.fiscal_year_end_month is not None
                and authorization.fiscal_year_end_month != witness_month
            ):
                raise IssuerDocumentPreparationError("fiscal_witness_calendar_mismatch")
        except IssuerDocumentPreparationError as exc:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "fiscal_witness_rejected",
                        "ticker": ticker,
                        "reason": exc.code,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            ir_fetch_status.record_attempt(
                db_path,
                ticker,
                status="failed",
                discovered=len(urls),
                downloaded=0,
                reason=exc.code,
            )
            return 0
        finally:
            conn.close()
    fiscal_year_end_month = (
        int(fiscal_witness.fiscal_year_end[:2])
        if fiscal_witness is not None
        else authorization.fiscal_year_end_month
    )
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        in_window = reported_quarter_selection_in_connection(
            conn,
            ticker=ticker,
            fiscal_year=year,
            fiscal_quarter=quarter,
            fiscal_year_end_month=fiscal_year_end_month,
            as_of=current_utc_date(),
        )
    finally:
        conn.close()
    if in_window is False:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": ticker,
                    "reason": "reported_quarter_window_denied",
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

    if in_window is None:
        summary = process_ticker(
            ticker,
            root=repo_root,
            db_path=db_path,
            categorize=False,
            calendar=None,
            owner_requested=True,
        )
        downloaded = summary.get("downloaded")
        downloaded_n = downloaded if type(downloaded) is int else 0
        sys.stdout.write(
            json.dumps(
                {
                    "event": "reported_period_selection_pending",
                    "ticker": ticker,
                    "retained_candidates": len(urls),
                    "downloaded_raw": downloaded_n,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return downloaded_n

    calendar = calendar_id_from_fye(
        fiscal_witness.fiscal_year_end
        if fiscal_witness is not None
        else _ticker_fye(db_path, ticker)
    )
    summary = process_ticker(
        ticker,
        root=repo_root,
        db_path=db_path,
        categorize=True,
        calendar=calendar,
        owner_requested=True,
        fiscal_witness_document_version_id=document_version_id,
        fiscal_witness_urls=frozenset(urls) if document_version_id is not None else frozenset(),
    )
    downloaded = summary.get("downloaded")
    downloaded_n = downloaded if isinstance(downloaded, int) else 0
    transport_skips = summary.get("transport_skipped")
    transport_skipped_n = transport_skips if isinstance(transport_skips, int) else 0

    registered = summary.get("ready")
    registered_n = registered if isinstance(registered, int) else 0

    recovered = summary.get("projection_repaired")
    recovered_n = recovered if isinstance(recovered, int) else 0

    existing = summary.get("ready_existing")
    existing_n = existing if isinstance(existing, int) else 0
    ready_urls = registered_n + existing_n
    failed = summary.get("failed")
    projection_failed = summary.get("projection_failed")
    attempt_failed = (
        summary.get("status") not in ("done", "recovered")
        or (isinstance(failed, int) and failed > 0)
        or (isinstance(projection_failed, int) and projection_failed > 0)
    )
    processed = False
    effects_failed = False
    if ready_urls and process:
        processed = _run_narrative(ticker, repo_root) and _set_brief_dirty(db_path, ticker)
        effects_failed = not processed
    success = ready_urls > 0 and not attempt_failed and not effects_failed

    ir_fetch_status.record_attempt(
        db_path,
        ticker,
        status="ok" if success else "failed",
        discovered=len(urls),
        downloaded=downloaded_n,
        reason=(
            f"{transport_skipped_n} transport/no-document skips; registrations retained"
            if transport_skipped_n
            else None
        )
        if success
        else "deterministic anchor or brief-dirty update failed"
        if effects_failed
        else "registration or required metadata failed"
        if attempt_failed
        else "no registration completed with required metadata",
    )
    sys.stdout.write(
        f"[{ticker}] done: downloaded={downloaded_n}, registered={registered_n}, "
        f"repaired={recovered_n}, transport_skipped={transport_skipped_n}, "
        f"processed={processed}, success={success}\n"
    )
    return ready_urls if success else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root)
    db_path = cast("Path", args.db) if args.db else repo_root / "data" / "portfolio.db"
    urls = collect_urls(cast("list[str] | None", args.url), cast("Path | None", args.urls_file))
    if not urls:
        sys.stderr.write("No URLs provided (use --url and/or --urls-file).\n")
        return 2
    registered = register_urls(
        cast("str", args.ticker),
        urls,
        repo_root=repo_root,
        db_path=db_path,
        year=cast("int", args.year),
        quarter=cast("int", args.quarter),
        process=not cast("bool", args.no_process),
        document_version_id=cast("str | None", args.document_version_id),
    )
    return 0 if registered else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker to register documents for")
    p.add_argument("--year", required=True, type=int, help="Reported fiscal year")
    p.add_argument("--quarter", required=True, type=int, choices=[1, 2, 3, 4])
    p.add_argument(
        "--document-version-id",
        help="Exact current SEC filing version that proves this fiscal period.",
    )
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
