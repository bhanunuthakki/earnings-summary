"""Extract structured KPIs from IR documents → kpi_facts.

Two-step protocol so an in-session LLM (Claude) does the structured extraction
while the persistence stays deterministic Python:

  1. `--list-pending --ticker <X>`: print the IR documents for ticker X that
     have not yet contributed any kpi_facts rows. The user (or LLM) reads each
     PDF, populates a manifest JSON of (kpi name, value, unit) tuples per doc.

  2. `--apply <manifest.json>`: read the manifest, validate it (Pydantic),
     persist into kpi_facts via src/pipeline/kpi_persistence.py, and emit
     validation_issues for any failed sanity check. Wrapped in a run_id.

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
             "unit": "ratio", "confidence": 0.95},
            ...
          ]
        }
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.kpi_persistence import (  # noqa: E402
    KpiExtractionManifest,
    persist_manifest,
)
from pipeline.queries import open_db  # noqa: E402
from pipeline.refresh_eval import refresh_for_tickers  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402

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


def _list_pending(conn, ticker: str | None) -> list[dict[str, object]]:
    """Return IR docs that have no kpi_facts rows tied to them yet.

    `kpi_facts.source_doc_id` provenance is the join key. A document is
    "pending" if its id never appears in kpi_facts.
    """
    placeholders = ",".join("?" for _ in _IR_DOC_TYPES)
    sql = (
        f"SELECT d.id, d.ticker, d.doc_type, d.period_end, d.file_path "
        f"FROM documents d "
        f"WHERE d.source_type = 'ir_doc' AND d.doc_type IN ({placeholders}) "
        f"AND NOT EXISTS (SELECT 1 FROM kpi_facts kf WHERE kf.source_doc_id = d.id) "
    )
    params: tuple[str, ...] = tuple(_IR_DOC_TYPES)
    if ticker is not None:
        sql += "AND d.ticker = ? "
        params = (*params, ticker.upper())
    sql += "ORDER BY d.ticker, d.period_end, d.doc_type, d.id"
    cur = conn.execute(sql, params)
    out: list[dict[str, object]] = []
    for row in cur.fetchall():
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


def _apply_manifest(
    conn, manifest_file: ManifestFile, *, with_eval: bool = False
) -> dict[str, object]:
    """Apply each KpiExtractionManifest under one ingestion run."""
    if not manifest_file.manifests:
        return {"manifests": 0, "inserted": 0, "skipped_existing": 0, "validation_issues": 0}

    ticker_scope = sorted({m.ticker.upper() for m in manifest_file.manifests})
    run_id = start_run(conn, directive="extract_kpis_from_ir", ticker_scope=ticker_scope)

    inserted = 0
    skipped = 0
    issues = 0
    failed = 0
    for m in manifest_file.manifests:
        try:
            result = persist_manifest(conn, run_id=run_id, manifest=m)
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
            sys.stderr.write(
                f"FAILED {m.ticker} doc#{m.source_doc_id}: {type(e).__name__}: {e}\n"
            )
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list-pending",
        action="store_true",
        help="Print IR documents not yet attached to any kpi_facts row.",
    )
    group.add_argument(
        "--apply",
        type=Path,
        help="Path to a manifest JSON file with KPI extractions to persist.",
    )
    parser.add_argument("--ticker", help="Restrict --list-pending to a single ticker.")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--with-eval",
        action="store_true",
        help="After --apply, re-run the thesis evaluator for each affected ticker.",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        if args.list_pending:
            pending = _list_pending(conn, args.ticker)
            print(json.dumps({"pending_count": len(pending), "documents": pending}, indent=2))
            return 0

        with open(args.apply, encoding="utf-8") as f:
            payload = json.load(f)
        manifest_file = ManifestFile.model_validate(payload)
        result = _apply_manifest(conn, manifest_file, with_eval=args.with_eval)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("failed", 0) == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
