"""Preflight PDFs and optionally persist governed local OCR evidence.

The command is read-only unless ``--apply`` is supplied. Full OCR apply mode
requires an explicit local tessdata directory and hash-binds every engine
artifact. ``--preflight-only`` persists only deterministic native-text
assessments, including pages that still require OCR. It performs no network
access or model download.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.ocr_extraction import (  # noqa: E402
    OCRBackfillRequest,
    TesseractCLIProvider,
    backfill_ocr_evidence,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Artifact root")
    parser.add_argument(
        "--content-root",
        type=Path,
        action="append",
        dest="content_roots",
        help="Additional allowed root for local evidence blobs; repeat as needed",
    )
    parser.add_argument("--batch-size", type=int, default=25, help="PDF document ids per batch")
    parser.add_argument("--task-id", default="ocr-evidence-backfill", help="Checkpoint namespace")
    parser.add_argument(
        "--source-lane",
        choices=("legacy", "evidence_native"),
        default="legacy",
        help="Select legacy PDFs or legacy-free evidence PDF versions",
    )
    parser.add_argument(
        "--document-version-id",
        action="append",
        dest="document_version_ids",
        help="Explicit evidence-native document version; repeat for a bounded targeted run",
    )
    parser.add_argument(
        "--language",
        action="append",
        dest="languages",
        help="Tesseract language code; repeat for multiple languages (default: eng)",
    )
    parser.add_argument(
        "--minimum-native-characters",
        type=int,
        default=32,
        help="Alphanumeric characters required per page before OCR is unnecessary",
    )
    parser.add_argument(
        "--minimum-mean-confidence",
        type=float,
        default=50.0,
        help="Minimum accepted Tesseract mean word confidence",
    )
    parser.add_argument("--dpi", type=int, default=300, help="Poppler render resolution")
    parser.add_argument("--page-segmentation-mode", type=int, default=6, help="Tesseract PSM")
    parser.add_argument("--engine-mode", type=int, default=1, help="Tesseract OEM")
    parser.add_argument(
        "--timeout-seconds", type=int, default=120, help="Per local subprocess timeout"
    )
    parser.add_argument(
        "--tesseract",
        type=Path,
        default=Path("tesseract"),
        help="Local Tesseract executable path or command",
    )
    parser.add_argument(
        "--pdftoppm",
        type=Path,
        default=Path("pdftoppm"),
        help="Local Poppler pdftoppm executable path or command",
    )
    parser.add_argument(
        "--tessdata-dir",
        type=Path,
        help="Local directory containing exact <language>.traineddata files",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Persist deterministic native-text assessments without an OCR engine",
    )
    parser.add_argument("--apply", action="store_true", help="Persist this bounded batch")
    args = parser.parse_args(argv)
    if args.preflight_only and not args.apply:
        parser.error("--preflight-only requires --apply")
    if args.preflight_only and args.tessdata_dir is not None:
        parser.error("--preflight-only and --tessdata-dir are mutually exclusive")

    languages = tuple(args.languages or ["eng"])
    request = OCRBackfillRequest(
        repo_root=args.repo_root,
        content_roots=tuple(args.content_roots or ()),
        apply=args.apply,
        batch_size=args.batch_size,
        task_id=args.task_id,
        source_lane=args.source_lane,
        document_version_ids=tuple(args.document_version_ids or ()),
        languages=languages,
        minimum_native_characters=args.minimum_native_characters,
        minimum_mean_confidence=args.minimum_mean_confidence,
        dpi=args.dpi,
        page_segmentation_mode=args.page_segmentation_mode,
        engine_mode=args.engine_mode,
        timeout_seconds=args.timeout_seconds,
    )
    provider = None
    if request.apply and not args.preflight_only:
        if args.tessdata_dir is None:
            parser.error("--tessdata-dir is required with --apply")
        provider = TesseractCLIProvider(
            tesseract_executable=args.tesseract,
            pdftoppm_executable=args.pdftoppm,
            tessdata_directory=args.tessdata_dir,
            languages=request.languages,
        )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
    try:
        result = backfill_ocr_evidence(conn, request, provider=provider)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
