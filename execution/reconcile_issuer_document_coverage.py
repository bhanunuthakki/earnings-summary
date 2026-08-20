"""Render a read-only issuer-document coverage receipt from an extractor frame.

This is intentionally an observation-only CLI.  It does not start an
extractor, contact a provider, or write SQLite; production orchestration may
pass a previously produced frame once its own transaction has completed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline.issuer_document_coverage import (  # noqa: E402
    ExtractorFactPopulationFrame,
    reconcile_extractor_fact_population,
    reconciliation_output,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite database to read")
    parser.add_argument("--frame", type=Path, required=True, help="Extractor population-frame JSON")
    parser.add_argument("--output", type=Path, required=True, help="Receipt JSON destination")
    args = parser.parse_args()
    frame = ExtractorFactPopulationFrame.model_validate_json(args.frame.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{args.db.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        receipt = reconcile_extractor_fact_population(conn, frame)
    finally:
        conn.close()
    output = reconciliation_output(receipt)
    args.output.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "issuer_document_coverage_reconciled",
                "document_id": frame.document_id,
                "idempotency_key": output.idempotency_key,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
