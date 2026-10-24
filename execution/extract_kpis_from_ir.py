"""Extract structured KPIs from IR documents → kpi_facts.

Two-step protocol so an in-session LLM (Claude) does the structured extraction
while the persistence stays deterministic Python:

  1. `--list-pending --ticker <X>`: print the IR documents for ticker X that
     lack a terminal issuer coverage receipt. The user (or LLM) reads each PDF,
     populates a manifest JSON of (kpi name, value, unit) tuples per doc.

  2. `--apply <manifest.json>`: read the manifest, validate it (Pydantic),
     persist into kpi_facts via src/pipeline/kpi_persistence.py, and emit
     validation_issues for any failed sanity check. Wrapped in a run_id.

  3. `--capture`: enumerate bounded IR supplement PDF sections and retain exact
     raw source candidates. Publication requires an exact source-context review.
     `--ocr-projection-file` with `--ticker` selects one sealed OCR projection.
     `--refresh` re-enumerates through the existing governed call route.

  4. `--source-review-file`: validate and preview one visual OCR review. Add
     `--apply-source-review` to persist its exact review node. A later exact
     cached capture can publish accepted facts through the existing owner.

Manifest JSON shape:
    {
      "manifests": [
        {
          "ticker": "MELI",
          "period_end": "2025-09-30",
          "fiscal_period_type": "Q3",
          "source_doc_id": 13123,
          "values": [
            {"name": "Revenue Growth (FXN)", "value": "0.34",
             "unit": "ratio", "confidence": 0.95,
             "source_excerpt": "FX-neutral revenue grew 34% YoY",
             "locator": {"pdf_page": 7}},
            ...
          ]
        }
      ]
    }

`source_excerpt` (verbatim quote supporting the value) is optional per value.

`locator` (sub-document position, e.g. the PDF page the value was read from
— see data_provenance.md §7) is REQUIRED per value as of the persist-time
enforcement flip (docs/design/provenance_clickthrough.md §4.1) — every
value's JSON must carry EITHER a real locator object (e.g.
`{"pdf_page": 7}`) when reading the PDF, OR an explicit escape hatch
`{"reason": "<why no locator was possible>"}` (min 8 characters) when it
genuinely wasn't -- a value with no `"locator"` key at all now fails
`--apply`'s Pydantic validation rather than silently landing with no
provenance.

Phase B pairing rule (§3.2): a value citing `{"pdf_page": N}` MUST also carry
a non-empty `source_excerpt` — the verbatim quote is what the pdf_slide peek
renders under the page image. `--apply` upgrades every pdf_page locator to
the v2 `pdf_slide` shape on persist, deriving a bounding box via PyMuPDF's
`page.search_for(excerpt)` when the source PDF is on disk (an optional
`"pdf_bbox": [x0, y0, x1, y1]` in the manifest is honored as-is).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

# Direct invocation imports the shared bootstrap before application modules.
from _lib import PROJECT_ROOT
from pydantic import BaseModel

from compute.ir_supplement_capture import OCRCaptureSelection
from compute.ir_supplement_revalidation import RevalidationManifest
from compute.kpi_extract_summaries import (
    capture_for_ir_pdf_docs,
    revalidate_ir_pdf_doc,
    write_log,
)
from models.facts import FactLocator, LocatorKind
from models.runs import StageName, StageStatus
from pipeline.document_completeness import (
    DocumentCompletenessStatus,
    document_completeness,
)
from pipeline.invocation_fingerprint import files_fingerprint, payload_sha256
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.locators import pdf_slide_locator
from pipeline.pdf_render import find_quote_bbox
from pipeline.queries import open_db
from pipeline.refresh_eval import refresh_for_tickers
from pipeline.run_accounting import (
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)
from provenance.evidence_native_candidates import (
    read_verified_local_evidence_bytes,
    select_evidence_native_candidates_by_id,
)
from provenance.immutable_artifact import read_stable_artifact
from provenance.ir_reported_facts import (
    IrCandidatePopulation,
    IrOCRSourceContextReview,
    persist_ir_source_context_review,
    validate_ir_source_context_review,
)
from provenance.source_read_context import SourceReadContext
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"

_IR_DOC_TYPES = (
    "ir_presentation",
    "ir_press_release",
    "ir_supplement",
    "ir_investor_update",
    "ir_transcript",
)


class ManifestFile(BaseModel):
    """Wrapper validating the on-disk manifest file format."""

    manifests: list[KpiExtractionManifest]


def list_pending_documents(conn: sqlite3.Connection, ticker: str | None) -> list[dict[str, object]]:
    """Return IR docs without a terminal issuer coverage receipt.

    ``kpi_facts`` rows are extraction evidence, not a completeness signal: a
    partial extraction must remain visible until the typed coverage ledger
    proves every expected output was captured or explicitly rejected.
    """
    placeholders = ",".join("?" for _ in _IR_DOC_TYPES)
    sql = (
        f"SELECT d.id, d.ticker, d.doc_type, d.period_end, d.file_path "
        f"FROM documents d "
        f"WHERE d.source_type = 'ir_doc' AND d.doc_type IN ({placeholders}) "
    )
    params: tuple[str, ...] = tuple(_IR_DOC_TYPES)
    if ticker is not None:
        sql += "AND d.ticker = ? "
        params = (*params, ticker.upper())
    sql += "ORDER BY d.ticker, d.period_end, d.doc_type, d.id"
    cur = conn.execute(sql, params)
    out: list[dict[str, object]] = []
    for row in cur.fetchall():
        if (
            document_completeness(conn, int(row["id"])).status
            is DocumentCompletenessStatus.COMPLETE
        ):
            continue
        out.append(
            {
                "document_id": int(row["id"]),
                "ticker": row["ticker"],
                "doc_type": row["doc_type"],
                "period_end": row["period_end"][:10] if row["period_end"] else None,
                "file_path": row["file_path"],
            }
        )
    return out


def _source_pdf_path(
    conn: sqlite3.Connection, doc_id: int, *, repo_root: Path = PROJECT_ROOT
) -> Path | None:
    """Resolve the manifest's source document to an on-disk PDF, else None."""
    row = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None or not row["file_path"]:
        return None
    raw = str(row["file_path"])
    if not raw.lower().endswith(".pdf"):
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path if path.exists() else None


def _upgrade_pdf_locators(
    conn: sqlite3.Connection, manifest: KpiExtractionManifest, *, repo_root: Path = PROJECT_ROOT
) -> KpiExtractionManifest:
    """Promote v1 ``{"pdf_page": N}`` manifest locators to renderable v2
    ``pdf_slide`` locators before persist (provenance click-through Phase B,
    docs/design/provenance_clickthrough.md §3.2's extract_kpis_from_ir row).

    Schema pairing rule: a value citing a ``pdf_page`` MUST carry a non-empty
    ``source_excerpt`` (a page without a quote is a weaker locator — the peek
    would have nothing to show the reader beyond the raw page). Violations
    raise ValueError, failing that one manifest loudly in --apply's
    per-manifest error accounting rather than landing a hollow locator.

    Bbox enrichment is best-effort: when the source PDF is on disk and
    PyMuPDF finds the excerpt on the cited page, the locator also carries the
    bounding box; otherwise page + snippet alone (still fully renderable).
    """
    upgraded: list[KpiValue] = []
    changed = False
    pdf_path: Path | bool | None = False  # False = not yet resolved (resolve lazily, once)
    for value in manifest.values:
        loc = value.locator
        if (
            not isinstance(loc, FactLocator)
            or loc.effective_kind() != LocatorKind.PDF_SLIDE
            or loc.pdf_page is None
        ):
            upgraded.append(value)
            continue
        excerpt = (value.source_excerpt or "").strip()
        if loc.kind is None and not excerpt:
            raise ValueError(
                f"value {value.name!r}: locator.pdf_page={loc.pdf_page} requires a "
                "non-empty source_excerpt (the verbatim quote the peek renders)"
            )
        if loc.kind is not None and loc.pdf_bbox is not None:
            upgraded.append(value)  # already fully-enriched v2
            continue
        if pdf_path is False:
            pdf_path = _source_pdf_path(conn, manifest.source_doc_id, repo_root=repo_root)
        bbox = loc.pdf_bbox
        if bbox is None and isinstance(pdf_path, Path) and excerpt:
            bbox = find_quote_bbox(pdf_path, loc.pdf_page, excerpt)
        snippet = excerpt or (loc.verbatim_snippet or "")
        upgraded.append(
            value.model_copy(
                update={
                    "locator": pdf_slide_locator(
                        pdf_page=loc.pdf_page, verbatim_snippet=snippet, bbox=bbox
                    )
                }
            )
        )
        changed = True
    if not changed:
        return manifest
    return manifest.model_copy(update={"values": upgraded})


# Public seam used by the manifest-application regression suite.
upgrade_pdf_locators = _upgrade_pdf_locators


def _apply_manifest(
    conn: sqlite3.Connection,
    manifest_file: ManifestFile,
    *,
    with_eval: bool = False,
    force: bool = False,
) -> dict[str, object]:
    """Apply each KpiExtractionManifest under one ingestion run."""
    if not manifest_file.manifests:
        return {"manifests": 0, "inserted": 0, "skipped_existing": 0, "validation_issues": 0}

    ticker_scope = sorted({m.ticker.upper() for m in manifest_file.manifests})
    run_id = start_run(
        conn,
        directive="extract_kpis_from_ir",
        ticker_scope=ticker_scope,
        invocation_inputs=_manifest_invocation_inputs(conn, manifest_file, with_eval=with_eval),
        force=force,
        deduplicate_completed=not with_eval,
    )

    inserted = 0
    skipped = 0
    issues = 0
    failed = 0
    for m in manifest_file.manifests:
        try:
            result = persist_manifest(conn, run_id=run_id, manifest=_upgrade_pdf_locators(conn, m))
        except (ValueError, KeyError) as e:
            record_stage(
                conn,
                run_id,
                m.ticker,
                StageName.PERSIST,
                StageStatus.FAILED,
                period_end=m.period_end,
                error_msg=f"doc#{m.source_doc_id}: {type(e).__name__}: {e}"[:500],
            )
            failed += 1
            sys.stderr.write(f"FAILED {m.ticker} doc#{m.source_doc_id}: {type(e).__name__}: {e}\n")
            continue
        record_stage(
            conn,
            run_id,
            m.ticker,
            StageName.PERSIST,
            StageStatus.OK,
            period_end=m.period_end,
        )
        inserted += result.inserted
        skipped += result.skipped_existing
        issues += result.validation_issues

    terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
    end_run(conn, run_id, terminal, error_summary=f"{failed} manifests failed" if failed else None)

    eval_results: list[dict[str, object]] = []
    if with_eval and failed == 0:
        refresh = refresh_for_tickers(
            conn, tickers=ticker_scope, holdings_dir=_HOLDINGS_DIR, run_id=run_id
        )
        for r in refresh:
            eval_results.append(
                {
                    "ticker": r.ticker,
                    "derived_kpi_rows_inserted": r.derived_kpi_rows_inserted,
                    "eval_status": r.eval_status.value if r.eval_status is not None else None,
                    "eval_error": r.eval_error,
                }
            )

    return {
        "run_id": run_id,
        "manifests": len(manifest_file.manifests),
        "inserted": inserted,
        "skipped_existing": skipped,
        "validation_issues": issues,
        "failed": failed,
        "with_eval": with_eval,
        "eval_results": eval_results,
    }


def _manifest_invocation_inputs(
    conn: sqlite3.Connection, manifest_file: ManifestFile, *, with_eval: bool
) -> dict[str, JsonValue]:
    """Fingerprint manifest content and PDFs used for locator enrichment."""
    manifest_payload = cast(
        "dict[str, JsonValue]",
        manifest_file.model_dump(mode="json"),
    )
    pdf_paths = [
        path
        for manifest in manifest_file.manifests
        if (
            path := _source_pdf_path(
                conn,
                manifest.source_doc_id,
            )
        )
        is not None
    ]
    return {
        "manifest_sha256": payload_sha256(manifest_payload),
        "source_pdfs": files_fingerprint(pdf_paths, root=PROJECT_ROOT),
        "with_eval": with_eval,
    }


def _tickers_with_capture_docs(conn: sqlite3.Connection) -> list[str]:
    """Distinct tickers that have at least one capture-eligible IR supplement PDF."""
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM documents "
        "WHERE source_type = 'ir_doc' AND doc_type = 'ir_supplement' "
        "AND LOWER(file_path) LIKE '%.pdf' ORDER BY ticker"
    ).fetchall()
    return [str(r["ticker"]).upper() for r in rows]


def _source_review_preview(
    conn: sqlite3.Connection,
    review: IrOCRSourceContextReview,
    *,
    source_context: SourceReadContext,
    apply: bool,
    ticker: str | None,
) -> dict[str, object]:
    if source_context.ocr_state_root is None:
        raise ValueError("IR OCR source review requires explicit product state root")
    row = conn.execute(
        "SELECT document_version_id,input_sha256,extractor_config_sha256,output_sha256 "
        "FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (review.extraction_run_id,),
    ).fetchone()
    if row is None or row[0] != review.document_version_id or row[1] != review.source_sha256:
        raise ValueError("IR OCR review extraction run is absent or differs")
    if ticker is not None:
        registered = conn.execute(
            "SELECT ticker FROM evidence_document_versions WHERE document_version_id=?",
            (review.document_version_id,),
        ).fetchone()
        if registered is None or str(registered[0]).upper() != ticker.upper():
            raise ValueError("IR OCR review ticker differs from source document")
    node_ids = tuple(
        str(node_id)
        for node_id, text_value in conn.execute(
            "SELECT node_id,text FROM evidence_nodes WHERE extraction_run_id=? ORDER BY node_id",
            (review.extraction_run_id,),
        )
        if json.loads(str(text_value)).get("schema_version")
        in {"ir-pdf-unreviewed-candidate.v3", "ir-pdf-unreviewed-ocr-candidate.v1"}
    )
    population = IrCandidatePopulation(
        document_version_id=review.document_version_id,
        source_sha256=review.source_sha256,
        extraction_run_id=review.extraction_run_id,
        extractor_config_sha256=str(row[2]),
        candidate_population_sha256=str(row[3]),
        candidate_node_ids=node_ids,
        retained_node_count=0,
        exact_replay=True,
    )
    candidates = select_evidence_native_candidates_by_id(
        conn, document_version_ids=(review.document_version_id,), include_legacy=True
    )
    if len(candidates) != 1 or candidates[0].media_type != "application/pdf":
        raise ValueError("IR OCR review source PDF is unavailable")
    source = candidates[0]
    if (
        source.byte_size > source_context.max_document_bytes
        or source.byte_size > source_context.max_total_bytes
    ):
        raise ValueError("IR OCR review source PDF exceeds read limit")
    raw_pdf_bytes = read_verified_local_evidence_bytes(
        conn,
        storage_uri=source.storage_uri,
        expected_sha256=review.source_sha256,
        expected_byte_size=source.byte_size,
        allowed_roots=source_context.content_roots,
        document_version_id=review.document_version_id,
    ).raw_bytes
    validate_ir_source_context_review(
        conn,
        review,
        population,
        raw_pdf_bytes=raw_pdf_bytes,
        ocr_repo_root=source_context.ocr_state_root,
    )
    review_node_id = "ir-pdf-review-ocr:" + review.review_sha256
    if apply:
        review_node_id = persist_ir_source_context_review(
            conn,
            review,
            population,
            raw_pdf_bytes=raw_pdf_bytes,
            ocr_repo_root=source_context.ocr_state_root,
        )
    return {
        "status": "review_persisted" if apply else "review_validated_preview",
        "review_node_id": review_node_id,
        "document_version_id": review.document_version_id,
        "candidate_count": len(node_ids),
        "accepted_count": sum(entry.disposition == "accepted" for entry in review.entries),
        "rejected_count": sum(entry.disposition == "rejected" for entry in review.entries),
        "financial_publication_count": 0,
    }


def _run_capture(
    conn: sqlite3.Connection,
    *,
    ticker: str | None,
    refresh: bool,
    ocr_selection: OCRCaptureSelection | None = None,
) -> dict[str, object]:
    """Capture-every-number from IR supplement PDFs for one ticker (or all)."""
    tickers = [ticker.upper()] if ticker else _tickers_with_capture_docs(conn)
    logs = [
        capture_for_ir_pdf_docs(t, PROJECT_ROOT, conn, refresh=refresh, ocr_selection=ocr_selection)
        for t in tickers
    ]
    write_log(PROJECT_ROOT, logs)
    return {
        "mode": "capture",
        "tickers": tickers,
        "ok": all(log.error is None for log in logs),
        "inserted": sum(log.kpis_inserted_total for log in logs),
        "per_ticker": [
            {
                "ticker": log.ticker,
                "docs_captured": len(log.quarters_extracted),
                "docs_skipped": len(log.quarters_skipped_no_missing),
                "kpis_inserted": log.kpis_inserted_total,
                "error": log.error,
            }
            for log in logs
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list-pending",
        action="store_true",
        help="Print IR documents without a terminal issuer coverage receipt.",
    )
    group.add_argument(
        "--apply",
        type=Path,
        help="Path to a manifest JSON file with KPI extractions to persist.",
    )
    group.add_argument(
        "--capture",
        action="store_true",
        help="Capture-every-number mode: enumerate EVERY labeled number from each "
        "ir_supplement PDF (no allowlist) and persist at IR_DOC tier with "
        "origin=CAPTURE. Scopes to --ticker, else all tickers with supplement PDFs.",
    )
    group.add_argument(
        "--revalidate-only",
        type=Path,
        help="Revalidate one explicit complete V16 IR section manifest without model calls.",
    )
    group.add_argument(
        "--source-review-file",
        type=Path,
        help="Validate and preview one exact visual OCR source review.",
    )
    parser.add_argument("--ticker", help="Restrict --list-pending / --capture to a single ticker.")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="With --capture, re-enumerate supplement PDFs already captured.",
    )
    parser.add_argument(
        "--ocr-projection-file",
        type=Path,
        help="With --capture and --ticker, pin one sealed OCR run for one exact document; retain candidates only.",
    )
    parser.add_argument(
        "--apply-source-review",
        action="store_true",
        help="With --source-review-file, persist the exact validated review node.",
    )
    parser.add_argument(
        "--source-state-root",
        type=Path,
        help="Explicit product state root for source PDF and retained OCR byte reads.",
    )
    parser.add_argument("--db", help="Path to portfolio.db")
    parser.add_argument(
        "--with-eval",
        action="store_true",
        help="After --apply, re-run the thesis evaluator for each affected ticker.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --apply, persist again even when the same immutable manifest already completed.",
    )
    args = parser.parse_args()

    if args.ocr_projection_file is not None and (not args.capture or not args.ticker):
        parser.error("--ocr-projection-file requires --capture and --ticker")
    if args.apply_source_review and args.source_review_file is None:
        parser.error("--apply-source-review requires --source-review-file")
    if args.source_review_file is not None:
        if args.db is None or args.source_state_root is None:
            parser.error("--source-review-file requires explicit --db and --source-state-root")
        if args.refresh or args.with_eval or args.force or args.ocr_projection_file:
            parser.error("IR OCR review does not permit capture or evaluation modifiers")
        source_context = SourceReadContext.for_product_state_root(args.source_state_root)
        _, payload = read_stable_artifact(args.source_review_file, max_bytes=1_000_000)
        review = IrOCRSourceContextReview.model_validate_json(payload)
        review_conn = connect_sqlite(
            args.db,
            role=(
                SQLiteConnectionRole.WRITER
                if args.apply_source_review
                else SQLiteConnectionRole.READ_ONLY
            ),
        )
        try:
            result = _source_review_preview(
                review_conn,
                review,
                source_context=source_context,
                apply=args.apply_source_review,
                ticker=args.ticker,
            )
        finally:
            review_conn.close()
        print(json.dumps(result, indent=2))
        return 0

    conn = open_db(args.db or str(PROJECT_ROOT / "data" / "portfolio.db"))
    try:
        if args.list_pending:
            pending = list_pending_documents(conn, args.ticker)
            print(json.dumps({"pending_count": len(pending), "documents": pending}, indent=2))
            return 0

        try:
            if args.revalidate_only:
                manifest = RevalidationManifest.model_validate_json(
                    args.revalidate_only.read_text(encoding="utf-8")
                )
                if args.ticker and args.ticker.upper() != manifest.ticker:
                    raise ValueError("IR revalidation ticker differs from the manifest")
                if args.refresh or args.with_eval or args.force:
                    raise ValueError(
                        "IR revalidation does not permit capture or evaluation modifiers"
                    )
                receipt = revalidate_ir_pdf_doc(manifest, PROJECT_ROOT, conn)
                print(receipt.model_dump_json())
                return 0
            if args.capture:
                ocr_selection = (
                    OCRCaptureSelection.model_validate_json(
                        args.ocr_projection_file.read_text(encoding="utf-8")
                    )
                    if args.ocr_projection_file is not None
                    else None
                )
                result = _run_capture(
                    conn, ticker=args.ticker, refresh=args.refresh, ocr_selection=ocr_selection
                )
                print(json.dumps(result, indent=2, default=str))
                return 0 if result["ok"] else 1

            with open(args.apply, encoding="utf-8") as f:
                payload = json.load(f)
            manifest_file = ManifestFile.model_validate(payload)
            result = _apply_manifest(
                conn,
                manifest_file,
                with_eval=args.with_eval,
                force=bool(args.force),
            )
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("failed", 0) == 0 else 1
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
