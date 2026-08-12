"""Discover a ticker's IR documents headlessly and write its URL manifest.

Resolves the ticker's IR URL (curated override -> ``ir_config.results_center_url``
-> ``tracked_companies.ir_url``), crawls it via the hybrid discovery (generic
crawler + the mz precise fast-path), and merges the discovered document URLs into
``.tmp/ir_url_manifest/<T>_urls.json`` --the manifest
``execution/fetch_ir_documents.py`` downloads from.

Best-effort by design: a ticker with no resolvable IR URL prints status
``no_ir_url`` and exits 0 (NOT a failure); a crawl that finds nothing prints
``no_docs``. The live crawl needs the optional ``ir`` extra (headless browser);
a missing extra surfaces as a non-zero exit with the ImportError in stderr.

Usage:
    python execution/discover_ir_documents.py --ticker NU
    python execution/discover_ir_documents.py --ticker ORCL --max-quarters 5
    python execution/discover_ir_documents.py --ticker ZZ --url https://ir.zz.com/quarterly-results
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.config import get_config  # noqa: E402
from ir_pipeline.discover import (  # noqa: E402
    IrDiscoveryAuthenticationDeniedError,
    discover_history_hybrid,
)
from ir_pipeline.discover._docmeta import CandidateDoc  # noqa: E402
from ir_pipeline.ir_url_overrides import resolve_ir_url  # noqa: E402
from ir_pipeline.manifest import ManifestEntry, manifest_path, merge_write  # noqa: E402
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    StoredCollectionAuthorization,
    authorize_stored_collection_target,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _db_ir_url(db_path: Path, ticker: str) -> str | None:
    """Read ``tracked_companies.ir_url`` for ``ticker``; None on any failure.

    Graceful when the DB is absent (gitignored; in a worktree it lives next to
    the main checkout) --discovery then relies on the curated override / config.
    """
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT ir_url FROM tracked_companies WHERE ticker = ? LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    val = row["ir_url"]
    return str(val) if val else None


def _to_entries(ticker: str, cands: list[CandidateDoc]) -> list[ManifestEntry]:
    """Map discovered candidates to manifest rows (period/doc_type are hints)."""
    return [
        ManifestEntry(
            ticker=ticker.upper(),
            doc_type=c.doc_type_guess or "document",
            url=c.url,
            year=c.year_guess,
            quarter=f"Q{c.quarter_guess}" if c.quarter_guess else None,
            note=f"discovered:{c.source_page}" if c.source_page else None,
        )
        for c in cands
    ]


def _emit_policy_denial(ticker: str, authorization: StoredCollectionAuthorization) -> None:
    target = authorization.target
    decision = authorization.decision
    sys.stderr.write(
        json.dumps(
            {
                "event": "source_collection_policy_denied",
                "ticker": ticker,
                "coverage_role": target.coverage_role.value if target is not None else None,
                "source": CollectionSource.IR.value,
                "artifact_kind": ArtifactKind.IR_DOCUMENT.value,
                "reason": decision.reason.value
                if decision is not None
                else authorization.status.value,
            },
            sort_keys=True,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = cast("Path", args.repo_root).resolve()
    ticker = cast("str", args.ticker).upper()
    authorization = authorize_stored_collection_target(
        cast("Path", args.db),
        ticker,
        requested=not cast("bool", args.automatic),
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not authorization.allowed:
        _emit_policy_denial(ticker, authorization)
        return 2

    cfg = get_config(ticker, repo_root)
    config_url = cfg.results_center_url if cfg else None
    db_ir_url = _db_ir_url(cast("Path", args.db), ticker)
    crawl_url = cast("str | None", args.url) or resolve_ir_url(ticker, db_ir_url, config_url)

    if not crawl_url:
        print(json.dumps({"ticker": ticker, "status": "no_ir_url", "discovered": 0, "added": 0}))
        return 0

    try:
        cands = discover_history_hybrid(
            ir_url=crawl_url,
            config=cfg,
            max_quarters=cast("int", args.max_quarters),
            timeout_ms=cast("int", args.timeout),
        )
    except IrDiscoveryAuthenticationDeniedError as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_authentication_denied",
                    "ticker": ticker,
                    "status": exc.status_code,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 10
    added, total = merge_write(repo_root, ticker, _to_entries(ticker, cands))
    print(
        json.dumps(
            {
                "ticker": ticker,
                "status": "done" if cands else "no_docs",
                "ir_url": crawl_url,
                "discovered": len(cands),
                "added": added,
                "manifest_total": total,
                "manifest": str(manifest_path(repo_root, ticker)),
            }
        )
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker to discover IR documents for")
    p.add_argument(
        "--max-quarters",
        type=int,
        default=SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters,
        help="Newest reported quarters to keep (default: policy maximum)",
    )
    p.add_argument(
        "--timeout", type=int, default=60_000, help="Per-page headless-browser timeout (ms)"
    )
    p.add_argument("--url", help="Override the resolved IR URL (a ticker's first run / testing)")
    p.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "data" / "portfolio.db",
        help="portfolio.db to read tracked_companies.ir_url from (default: %(default)s)",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument(
        "--automatic",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = p.parse_args(argv)
    bound = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters
    if cast("int", args.max_quarters) < 1 or cast("int", args.max_quarters) > bound:
        p.error(f"--max-quarters must be between 1 and {bound} (source policy)")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
