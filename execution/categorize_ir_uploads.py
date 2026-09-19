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
from typing import cast

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

import index_manager
from compute.evidence_snapshot import capture_snapshot
from ir_uploads import (
    CategorizationFailure,
    canonical_path,
    classify_ir_file,
    iter_uncategorized_files,
    parse_canonical_path,
    set_runtime_registry,
    sha256_of,
    ticker_hint_from_path,
)
from models.documents import DocType, FetchStatus, SourceType
from provenance.evidence_backfill import ensure_legacy_document_evidence
from provenance.immutable_artifact import publish_bytes_no_clobber
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# When running from a git worktree, the gitignored data/ and ir_documents/
# folders typically live next to the main checkout, not the worktree root.
# Honor IR_PROJECT_ROOT for that case — purely a path override; logic identical.
_PROJECT_ROOT_OVERRIDE = os.environ.get("IR_PROJECT_ROOT")
if _PROJECT_ROOT_OVERRIDE:
    PROJECT_ROOT = Path(_PROJECT_ROOT_OVERRIDE).resolve()

IR_DIR = PROJECT_ROOT / "ir_documents"
UNSORTED_DIR = IR_DIR / "_unsorted"
RUN_MANIFEST_DIR = PROJECT_ROOT / ".tmp" / "ir_categorization"
DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
DEFAULT_REL_PATH_ROOT = PROJECT_ROOT
# {staging_filename: source_url} written by fetch_ir_documents.py, so auto-fetched
# docs carry their real IR URL as documents.source_url (not a manual_upload: stub).
INCOMING_URLS_PATH = PROJECT_ROOT / ".tmp" / "ir_incoming_urls.json"

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger("categorize_ir_uploads")

# Subset of DocType values that the legacy `process_ir_documents.py` step
# knows how to LLM-process via the (ticker, year, quarter, doc_type) keying.
# `IR_EVENT` uses a separate event-keyed path — handled below in `process_ir_document`.
# Other doc_types (SEC_10K/10Q, IR_SUPPLEMENT) are still registered in `documents`
# (canonical) but not mirrored to the legacy JSON index because the LLM step
# has no handler for them.
_LEGACY_INDEX_MAP: dict[DocType, str] = {
    DocType.IR_PRESS_RELEASE: "press_release",
    DocType.IR_PRESENTATION: "presentation",
    DocType.IR_TRANSCRIPT: "transcript",
    DocType.IR_INVESTOR_UPDATE: "investor_update",
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
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_url_overrides(path: Path) -> dict[str, str]:
    """``{staging_filename: source_url}`` from fetch_ir_documents.py's sidecar."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in cast("dict[str, object]", data).items()}


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
    """Admit verified bytes and same-identity relocation as one database unit."""
    snapshot = capture_snapshot(file_path, file_path.parent)
    if snapshot.sha256 != sha256 or len(snapshot.payload) != raw_bytes_size:
        raise ValueError("IR document changed before registration")
    rel = _safe_rel(file_path, rel_root)
    conn.execute("SAVEPOINT ir_document_capture")
    try:
        existing = conn.execute(
            "SELECT id,ticker,source_type,doc_type,period_end,raw_bytes_size "
            "FROM documents WHERE sha256=?",
            (sha256,),
        ).fetchone()
        inserted = existing is None
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO documents "
                "(ticker,source_type,doc_type,period_end,file_path,sha256,"
                "fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ticker,
                    SourceType.IR_DOC.value,
                    doc_type.value,
                    dt.datetime.combine(period_end, dt.time()).isoformat(),
                    rel,
                    sha256,
                    fetched_at.isoformat(),
                    FetchStatus.OK.value,
                    raw_bytes_size,
                    source_url,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("IR document registration returned no identity")
            document_id = cursor.lastrowid
        else:
            existing_period = dt.datetime.fromisoformat(str(existing[4])).date()
            if (
                tuple(existing[1:4]) != (ticker, SourceType.IR_DOC.value, doc_type.value)
                or existing_period != period_end
                or existing[5] != raw_bytes_size
            ):
                raise ValueError("IR bytes already have conflicting registered identity or scope")
            document_id = int(existing[0])
            conn.execute("UPDATE documents SET file_path=? WHERE id=?", (rel, document_id))
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_document_versions'"
            ).fetchone()
            is not None
        ):
            ensure_legacy_document_evidence(conn, repo_root=rel_root, document_id=document_id)
        else:
            log.warning(
                {"event": "ir_capture_legacy_schema_without_evidence", "document_id": document_id}
            )
        conn.execute("RELEASE SAVEPOINT ir_document_capture")
        return inserted
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ir_document_capture")
        conn.execute("RELEASE SAVEPOINT ir_document_capture")
        raise


def _retire_unreferenced_input(
    source: Path, destination: Path, conn: sqlite3.Connection, digest: str, root: Path
) -> None:
    """Remove only a committed, unchanged duplicate with no retained evidence alias."""
    if conn.in_transaction or source.resolve() == destination.resolve():
        return
    if (
        conn.execute(
            "SELECT 1 FROM documents WHERE file_path IN (?,?)",
            (_safe_rel(source, root), str(source.resolve())),
        ).fetchone()
        is not None
    ):
        return
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_content_blobs'"
        ).fetchone()
        is not None
        and conn.execute(
            "SELECT 1 FROM evidence_content_blobs WHERE storage_uri=? "
            "UNION ALL SELECT 1 FROM evidence_blob_location_observations WHERE storage_uri=? LIMIT 1",
            (source.resolve().as_uri(), source.resolve().as_uri()),
        ).fetchone()
        is not None
    ):
        return
    if capture_snapshot(source, source.parent).sha256 != digest:
        raise ValueError("IR incoming source changed after its snapshot was registered")
    source.unlink()


def _move_or_error(src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        return
    # Source already AT the destination (a file whose name already matches its
    # canonical doc_type — e.g. a `sec_10k__<sha8>.pdf` re-encountered on reindex).
    # Without this guard the `dest.exists() and same-sha` branch below would unlink
    # the file as if it were a duplicate of itself — silent data loss.
    if src.resolve() == dest.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Same sha8 prefix, same bytes → idempotent re-run. Just delete src.
        if sha256_of(dest) == sha256_of(src):
            src.unlink()
            return
        raise FileExistsError(f"refuse_to_clobber: {dest} exists with different bytes than {src}")
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


def process_ir_document(
    path: Path,
    conn: sqlite3.Connection | None,
    dry_run: bool,
    ticker_filter: str | None,
    ir_dir: Path,
    rel_root: Path,
    *,
    url_overrides: dict[str, str] | None = None,
    calendar_override: str | None = None,
) -> dict[str, object]:
    """Classify, move, register one file. Returns a record for the run manifest.

    Files already at canonical position (`<TICKER>/<period_end>/<doc_type>__<sha8>.<ext>`)
    short-circuit the classifier and the move step — only the DB row is written.
    Files inside a ticker subdir but not at a canonical filename get the parent
    folder's ticker passed to `classify_ir_file` as a hint.
    """
    try:
        relative_source = path.relative_to(ir_dir)
    except ValueError:
        relative_source = path.relative_to(ir_dir.resolve())
    path = ir_dir.resolve() / relative_source
    source_snapshot = capture_snapshot(path, path.parent)
    canonical = parse_canonical_path(path.resolve(), ir_dir.resolve())
    if canonical is not None:
        if ticker_filter and canonical.ticker != ticker_filter:
            log.info({"event": "skipped_filter", "file": path.name, "ticker": canonical.ticker})
            return {"status": "skipped", "original": path.name, "ticker": canonical.ticker}
        sha = source_snapshot.sha256
        raw_bytes_size = len(source_snapshot.payload)
        if canonical_path(ir_dir, canonical, sha, path.suffix).resolve() != path.resolve():
            raise ValueError("IR canonical filename does not match its bytes")
        log.info(
            {
                "event": "reindexed",
                "ticker": canonical.ticker,
                "doc_type": canonical.doc_type.value,
                "period_end": canonical.period_end.isoformat(),
                "path": _safe_rel(path, rel_root),
            }
        )
        db_inserted = False
        if not dry_run and conn is not None:
            fetched_at = dt.datetime.fromtimestamp(path.stat().st_mtime)
            db_inserted = _insert_document_row(
                conn,
                ticker=canonical.ticker,
                doc_type=canonical.doc_type,
                period_end=canonical.period_end,
                file_path=path,
                sha256=sha,
                fetched_at=fetched_at,
                raw_bytes_size=raw_bytes_size,
                source_url=f"reindex_subdir:{path.name}",
                rel_root=rel_root,
            )
            legacy = _LEGACY_INDEX_MAP.get(canonical.doc_type)
            if legacy is not None:
                year, qlabel = _quarter_label_from_period_end(canonical.period_end)
                existing_idx = cast(
                    object, index_manager.has_document(canonical.ticker, year, qlabel, legacy)
                )
                already_processed = (
                    isinstance(existing_idx, dict) and existing_idx.get("processed") is True
                )
                if not already_processed:
                    index_manager.register_ir_document(
                        ticker=canonical.ticker,
                        year=year,
                        quarter=qlabel,
                        doc_type=legacy,
                        ir_url=f"reindex_subdir:{path.name}",
                        local_path=str(path),
                        fiscal_label=canonical.period_label,
                        note="reindexed_by:categorize_ir_uploads",
                        processed=False,
                    )
        return {
            "status": "reindexed",
            "original": path.name,
            "ticker": canonical.ticker,
            "doc_type": canonical.doc_type.value,
            "period_end": canonical.period_end.isoformat(),
            "sha256": sha,
            "raw_bytes_size": raw_bytes_size,
            "documents_inserted": db_inserted,
        }

    hint = ticker_hint_from_path(path, ir_dir)
    outcome = classify_ir_file(path, ticker_hint=hint, calendar_override=calendar_override)
    if capture_snapshot(path, path.parent).sha256 != source_snapshot.sha256:
        raise ValueError("IR source changed during classification")
    src_url = (url_overrides or {}).get(path.name)
    if isinstance(outcome, CategorizationFailure):
        log.warning({"event": "rejected", "file": path.name, "reason": outcome.reason})
        if hint is not None:
            return {
                "status": "rejected",
                "original": path.name,
                "moved_to": _safe_rel(path, rel_root),
                "reason": outcome.reason,
                "ticker_guess": outcome.ticker_guess,
                "left_in_place": True,
            }
        moved = _quarantine(path, outcome, dry_run, ir_dir)
        return {
            "status": "rejected",
            "original": path.name,
            "moved_to": _safe_rel(moved, rel_root),
            "reason": outcome.reason,
            "ticker_guess": outcome.ticker_guess,
        }

    if ticker_filter and outcome.ticker != ticker_filter:
        log.info({"event": "skipped_filter", "file": path.name, "ticker": outcome.ticker})
        return {
            "status": "skipped",
            "original": path.name,
            "ticker": outcome.ticker,
        }

    sha = source_snapshot.sha256
    raw_bytes_size = len(source_snapshot.payload)
    new_path = canonical_path(ir_dir.resolve(), outcome, sha, path.suffix)

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

    if not dry_run:
        snapshot = capture_snapshot(path, path.parent)
        if snapshot.sha256 != sha or len(snapshot.payload) != raw_bytes_size:
            raise ValueError("IR source changed during categorization")
        publish_bytes_no_clobber(new_path, snapshot.payload)

    fetched_at = dt.datetime.fromtimestamp(path.stat().st_mtime)

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
            source_url=src_url or f"manual_upload:{path.name}",
            rel_root=rel_root,
        )
        _retire_unreferenced_input(path, new_path, conn, sha, rel_root)
        legacy = _LEGACY_INDEX_MAP.get(outcome.doc_type)
        if legacy is not None:
            year, qlabel = _quarter_label_from_period_end(outcome.period_end)
            index_manager.register_ir_document(
                ticker=outcome.ticker,
                year=year,
                quarter=qlabel,
                doc_type=legacy,
                ir_url=src_url or f"manual_upload:{path.name}",
                local_path=str(new_path),
                fiscal_label=outcome.period_label,
                note="categorized_by:categorize_ir_uploads",
                processed=False,
            )
        elif outcome.doc_type == DocType.IR_EVENT:
            # Events are keyed by (ticker, event_date, sha256[:8]) — see
            # `index_manager.register_event_document`. period_end is the event date.
            index_manager.register_event_document(
                ticker=outcome.ticker,
                event_date=outcome.period_end.isoformat(),
                local_path=str(new_path),
                sha256=sha,
                confidence=1.0 if outcome.confidence.value == "high" else 0.6,
                note="categorized_by:categorize_ir_uploads",
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
    parser.add_argument(
        "--dry-run", action="store_true", help="Classify and report; no moves, no DB writes."
    )
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
    parser.add_argument(
        "--calendar",
        default=None,
        help="Fiscal-calendar id for the auto-fetch path: trust the parent-folder "
        "ticker hint and attribute periods for tickers not in ISSUER_REGISTRY. "
        "Omit for the strict manual-upload behavior.",
    )
    args = parser.parse_args()

    # Install the effective registry (curated ISSUER_REGISTRY + the
    # tracked-companies-synced data/issuer_registry.json) so eval/portfolio
    # tickers categorize without a code edit. No-op when the store is absent.
    import issuer_registry

    set_runtime_registry(issuer_registry.effective_entries(PROJECT_ROOT))

    src_dir: Path = args.source_dir.resolve()
    if not src_dir.exists():
        log.info({"event": "no_source_dir", "path": str(src_dir)})
        print(
            json.dumps({"status": "no_source_dir", "categorized": 0, "rejected": 0, "skipped": 0})
        )
        return 0

    files = iter_uncategorized_files(src_dir)
    if not files:
        log.info({"event": "no_uncategorized_files", "path": str(src_dir)})
        print(json.dumps({"status": "empty", "categorized": 0, "rejected": 0, "skipped": 0}))
        return 0

    conn: sqlite3.Connection | None = None
    if not args.dry_run:
        conn = _connect_db(args.db_path)

    url_overrides = _load_url_overrides(INCOMING_URLS_PATH)

    records: list[dict[str, object]] = []
    counts = {"categorized": 0, "rejected": 0, "skipped": 0}
    try:
        for f in files:
            record = process_ir_document(
                f,
                conn,
                args.dry_run,
                args.ticker,
                ir_dir=src_dir,
                rel_root=args.rel_root.resolve(),
                url_overrides=url_overrides,
                calendar_override=args.calendar,
            )
            records.append(record)
            status = record["status"]
            if not isinstance(status, str):
                raise RuntimeError("IR categorization returned an invalid status")
            counts[status] = counts.get(status, 0) + 1
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
