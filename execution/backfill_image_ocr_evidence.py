"""OCR captured standalone JPEG/PNG evidence with explicit local Tesseract.

The command is read-only by default. Apply mode requires exact local
Tesseract and tessdata paths; it performs no installation, download, or
network request.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.image_ocr_extraction import (  # noqa: E402
    ImageOCRRequest,
    TesseractImageProvider,
    backfill_image_ocr_evidence,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Portfolio SQLite path")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--content-root",
        type=Path,
        action="append",
        dest="content_roots",
        help="Allowed local blob root; repeat as needed",
    )
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--task-id", default="image-ocr-evidence-backfill")
    parser.add_argument(
        "--document-version-id",
        action="append",
        dest="document_version_ids",
        help="Explicit evidence-native image version; repeat for a bounded targeted run",
    )
    parser.add_argument("--language", action="append", dest="languages")
    parser.add_argument("--maximum-image-bytes", type=int, default=25 * 1024 * 1024)
    parser.add_argument("--maximum-width", type=int, default=20_000)
    parser.add_argument("--maximum-height", type=int, default=20_000)
    parser.add_argument("--maximum-pixels", type=int, default=40_000_000)
    parser.add_argument("--minimum-substantive-characters", type=int, default=8)
    parser.add_argument("--minimum-mean-confidence", type=float, default=50.0)
    parser.add_argument("--page-segmentation-mode", type=int, default=6)
    parser.add_argument("--engine-mode", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument(
        "--tesseract",
        type=Path,
        help="Explicit local Tesseract executable path; required with --apply",
    )
    parser.add_argument(
        "--tessdata-dir",
        type=Path,
        help="Explicit local directory containing exact traineddata; required with --apply",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.tesseract is None:
        parser.error("--tesseract is required with --apply")
    if args.apply and args.tessdata_dir is None:
        parser.error("--tessdata-dir is required with --apply")

    request = ImageOCRRequest(
        repo_root=args.repo_root,
        content_roots=tuple(args.content_roots or ()),
        apply=args.apply,
        batch_size=args.batch_size,
        task_id=args.task_id,
        document_version_ids=tuple(args.document_version_ids or ()),
        languages=tuple(args.languages or ("eng",)),
        maximum_image_bytes=args.maximum_image_bytes,
        maximum_width=args.maximum_width,
        maximum_height=args.maximum_height,
        maximum_pixels=args.maximum_pixels,
        minimum_substantive_characters=args.minimum_substantive_characters,
        minimum_mean_confidence=args.minimum_mean_confidence,
        page_segmentation_mode=args.page_segmentation_mode,
        engine_mode=args.engine_mode,
        timeout_seconds=args.timeout_seconds,
    )
    provider = (
        None
        if not request.apply
        else TesseractImageProvider(
            tesseract_executable=args.tesseract,
            tessdata_directory=args.tessdata_dir,
            languages=request.languages,
        )
    )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
    try:
        result = backfill_image_ocr_evidence(conn, request, provider=provider)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
