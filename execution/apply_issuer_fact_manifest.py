"""Validate or atomically apply a typed issuer KPI/segment manifest.

Validation/dry-run is the default.  ``--apply`` is the only mode that writes
SQLite, and it writes facts plus the coverage receipt in one transaction.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline.issuer_fact_manifest import (  # noqa: E402
    IssuerFactManifest,
    apply_issuer_fact_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite database path")
    parser.add_argument("--manifest", type=Path, required=True, help="Manifest JSON path")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist facts and the coverage receipt; omitted means validation-only",
    )
    args = parser.parse_args()
    manifest = IssuerFactManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        result = apply_issuer_fact_manifest(conn, manifest, apply=args.apply)
    finally:
        conn.close()
    print(result.model_dump_json(exclude_none=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
