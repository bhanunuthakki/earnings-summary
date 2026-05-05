"""
execution/categorize_ir_uploads.py
-----------------------------------
Layer 3 CLI: classify and register manually-uploaded IR documents.

For every PDF/XLSX dropped at the root of `ir_documents/`, this script:
  1. Identifies (ticker, doc_type, period_end) deterministically — filename
     heuristics first, then first-page content fingerprinting (no LLM).
  2. Moves the file to the canonical path
       ir_documents/{TICKER}/{period_end_iso}/{doc_type}__{sha8}.{ext}
  3. Inserts a row into the `documents` table with source_type='ir_doc',
     source_url='manual_upload:{original-filename}', sha256-keyed for
     idempotence (a re-upload of identical bytes is a silent no-op).
  4. Mirrors compatible doc_types into the legacy `document_index.json` so
     the existing `process_ir_documents.py` LLM step keeps working.

Files the classifier cannot place go to `ir_documents/_unsorted/` next to a
`.error.json` sidecar containing the failure reason and partial evidence —
never silently dropped.

Usage:
    python execution/categorize_ir_uploads.py --dry-run
    python execution/categorize_ir_uploads.py
    python execution/categorize_ir_uploads.py --ticker NU
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

# When running from a git worktree, the gitignored data/ and ir_documents/
# folders typically live next to the main checkout, not the worktree root.
# Honor IR_PROJECT_ROOT for that case — purely a path override; logic identical.
_PROJECT_ROOT_OVERRIDE = os.environ.get("IR_PROJECT_ROOT")
if _PROJECT_ROOT_OVERRIDE:
    PROJECT_ROOT = Path(_PROJECT_ROOT_OVERRIDE).resolve()

import index_manager
from ir_uploads import (
    CategorizationFailure,
    CategorizationResult,
    canonical_path,
    classify_ir_file,
    iter_uncategorized_files,
    sha256_of,
)
from models.documents import DocType, FetchStatus, SourceType

IR_DIR = PROJECT_ROOT / "ir_documents"
UNSORTED_DIR = IR_DIR / "_unsorted"
RUN_MANIFEST_DIR = PROJECT_ROOT / ".tmp" / "ir_categorization"
DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
DEFAULT_REL_PATH_ROOT = PROJECT_ROOT

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger("categorize_ir_uploads")

# Subset of DocType values that the legacy `process_ir_documents.py` step
# knows how to LLM-process. Other doc_types are still registered in `documents`
# (canonical) but not mirrored to the legacy JSON index.
_LEGACY_INDEX_MAP: dict[DocType, str] = {
    DocType.IR_PRESS_RELEASE: "press_release",
    DocType.IR_PRESENTATION: "presentation",
    DocType.IR_TRANSCRIPT: "transcript",
}


def _quarter_label_from_period_end(period_end: dt.date) -> tuple[int, str]:
    """Return (year, 'Qn') for the quarter that ends on `period_end`.

    Used only for the legacy JSON-index mirror — the canonical `documents`
    table uses the ISO period_end directly.
    """
    month = period_end.month
    if month <= 3:
        return period_end.year, "Q1"
    if month <= 6:
        return period_end.year, "Q2"
    if month <= 9:
        return period_end.year, "Q3"
    return period_end.year, "Q4"


def _connect_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"portfolio.db not found at {db_path}. Run `alembic upgrade head` first."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_document_row(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    doc_type: DocType,
    period_end: dt.date,
    file_path: Path,
    sha256: str,
    fetched_at: dt.datetime,
    raw_bytes_size: int,
    source_url: str,
    rel_root: Path,
) -> bool:
    """INSERT OR IGNORE on the `documents` table. Returns True if inserted."""
    rel = _safe_rel(file_path, rel_root)
    cur = conn.execute(
        "INSERT OR IGNORE INTO documents "
        "(ticker, source_type, doc_type, period_end, file_path, sha256, "
        " fetched_at, fetch_status, raw_bytes_size, source_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            SourceType.IR_DOC.value,
            doc_type.value,
            dt.datetime.combine(period_end, dt.time(0, 0)).isoformat(),
            rel,
            sha256,
            fetched_at.isoformat(),
            FetchStatus.OK.value,
            raw_bytes_size,
            source_url,
        ),
    )
    return cur.rowcount > 0


def _move_or_error(src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Same sha8 prefix, same bytes → idempotent re-run. Just delete src.
        if sha256_of(dest) == sha256_of(src):
            src.unlink()
            return
        raise FileExistsError(
            f"refuse_to_clobber: {dest} exists with different bytes than {src}"
        )
    shutil.move(str(src), str(dest))


def _quarantine(
    path: Path,
    failure: CategorizationFailure,
    dry_run: bool,
    ir_dir: Path,
) -> Path:
    """Move a rejected file under `_unsorted/` and write a sidecar `.error.json`."""
    unsorted_dir = ir_dir / "_unsorted"
    if dry_run:
        return unsorted_dir / path.name
    unsorted_dir.mkdir(parents=True, exist_ok=True)
    dest = unsorted_dir / path.name
    if dest.exists() and dest != path:
        # Avoid clobber by suffixing with a counter.
        i = 1
        while True:
            cand = unsorted_dir / f"{dest.stem}__{i}{dest.suffix}"
            if not cand.exists():
                dest = cand
                break
            i += 1
    shutil.move(str(path), str(dest))
    sidecar = dest.with_suffix(dest.suffix + ".error.json")
    sidecar.write_text(failure.model_dump_json(indent=2), encoding="utf-8")
    return dest


def _safe_rel(p: Path, root: Path) -> str:
    """Best-effort relative path for logs; absolute fallback if outside root."""
    try:
        return str(p.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _process_one(
    path: Path,
    conn: sqlite3.Connection | None,
    dry_run: bool,
    ticker_filter: str | None,
    ir_dir: Path,
    rel_root: Path,
) -> dict[str, object]:
    """Classify, move, register one file. Returns a record for the run manifest."""
    outcome = classify_ir_file(path)
    if isinstance(outcome, CategorizationFailure):
        log.warning(
            {"event": "rejected", "file": path.name, "reason": outcome.reason}
        )
        moved = _quarantine(path, outcome, dry_run, ir_dir)
        return {
            "status": "rejected",
            "original": path.name,
            "moved_to": _safe_rel(moved, rel_root),
            "reason": outcome.reason,
            "ticker_guess": outcome.ticker_guess,
        }

    if ticker_filter and outcome.ticker != ticker_filter:
        log.info(
            {"event": "skipped_filter", "file": path.name, "ticker": outcome.ticker}
        )
        return {
            "status": "skipped",
            "original": path.name,
            "ticker": outcome.ticker,
        }

    sha = sha256_of(path)
    raw_bytes_size = path.stat().st_size
    new_path = canonical_path(ir_dir, outcome, sha, path.suffix)

    log.info(
        {
            "event": "categorized",
            "file": path.name,
            "ticker": outcome.ticker,
            "doc_type": outcome.doc_type.value,
            "period_end": outcome.period_end.isoformat(),
            "confidence": outcome.confidence.value,
            "new_path": _safe_rel(new_path, rel_root),
        }
    )

    _move_or_error(path, new_path, dry_run)

    fetched_at = dt.datetime.fromtimestamp(path.stat().st_mtime if path.exists() else new_path.stat().st_mtime)

    db_inserted = False
    if not dry_run and conn is not None:
        db_inserted = _insert_document_row(
            conn,
            ticker=outcome.ticker,
            doc_type=outcome.doc_type,
            period_end=outcome.period_end,
            file_path=new_path,
            sha256=sha,
            fetched_at=fetched_at,
            raw_bytes_size=raw_bytes_size,
            source_url=f"manual_upload:{path.name}",
            rel_root=rel_root,
        )
        legacy = _LEGACY_INDEX_MAP.get(outcome.doc_type)
        if legacy is not None:
            year, qlabel = _quarter_label_from_period_end(outcome.period_end)
            index_manager.register_ir_document(
                ticker=outcome.ticker,
                year=year,
                quarter=qlabel,
                doc_type=legacy,
                ir_url=f"manual_upload:{path.name}",
                local_path=str(new_path),
                fiscal_label=outcome.period_label,
                note="categorized_by:categorize_ir_uploads",
                processed=False,
            )

    return {
        "status": "categorized",
        "original": path.name,
        "ticker": outcome.ticker,
        "doc_type": outcome.doc_type.value,
        "period_end": outcome.period_end.isoformat(),
        "period_label": outcome.period_label,
        "confidence": outcome.confidence.value,
        "sha256": sha,
        "raw_bytes_size": raw_bytes_size,
        "new_path": _safe_rel(new_path, rel_root),
        "documents_inserted": db_inserted,
        "ticker_evidence": outcome.ticker_evidence,
        "doc_type_evidence": outcome.doc_type_evidence,
        "period_evidence": outcome.period_evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Categorize manually-uploaded IR documents under ir_documents/."
    )
    parser.add_argument("--dry-run", action="store_true", help="Classify and report; no moves, no DB writes.")
    parser.add_argument(
        "--ticker",
        type=str,
        help="Only categorize files that classify to this ticker; others left in place.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=IR_DIR,
        help="Override the source directory (default: ir_documents/).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DB_PATH,
        help="Override the portfolio.db path (default: data/portfolio.db).",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=RUN_MANIFEST_DIR,
        help="Override the run-manifest output directory (default: .tmp/ir_categorization/).",
    )
    parser.add_argument(
        "--rel-root",
        type=Path,
        default=DEFAULT_REL_PATH_ROOT,
        help="Root that `documents.file_path` and log paths are reported relative to.",
    )
    args = parser.parse_args()

    src_dir: Path = args.source_dir.resolve()
    if not src_dir.exists():
        log.info({"event": "no_source_dir", "path": str(src_dir)})
        print(json.dumps({"status": "no_source_dir", "categorized": 0, "rejected": 0, "skipped": 0}))
        return 0

    files = iter_uncategorized_files(src_dir)
    if not files:
        log.info({"event": "no_uncategorized_files", "path": str(src_dir)})
        print(json.dumps({"status": "empty", "categorized": 0, "rejected": 0, "skipped": 0}))
        return 0

    conn: sqlite3.Connection | None = None
    if not args.dry_run:
        conn = _connect_db(args.db_path)

    records: list[dict[str, object]] = []
    counts = {"categorized": 0, "rejected": 0, "skipped": 0}
    try:
        for f in files:
            record = _process_one(
                f,
                conn,
                args.dry_run,
                args.ticker,
                ir_dir=src_dir,
                rel_root=args.rel_root.resolve(),
            )
            records.append(record)
            counts[record["status"]] = counts.get(record["status"], 0) + 1
            if conn is not None:
                conn.commit()
    finally:
        if conn is not None:
            conn.close()

    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    manifest = args.manifest_dir / f"run_{run_id}.json"
    if not args.dry_run:
        manifest.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        log.info({"event": "manifest_written", "path": str(manifest)})

    summary = {
        "status": "done",
        "dry_run": args.dry_run,
        **counts,
        "manifest": _safe_rel(manifest, args.rel_root.resolve()) if not args.dry_run else None,
    }
    print(json.dumps(summary))
    return 1 if counts["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
