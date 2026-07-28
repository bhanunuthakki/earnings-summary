"""Read-only audit and quarantine plan for document parent links.

This command deliberately never updates ``documents.parent_document_id``.
Missing parents can reflect a pruned raw input or changed bytes; guessing a
replacement would create false provenance. The JSON report is a repair plan
for an analyst, with every dangling row explicitly quarantined.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def audit_parent_links(db_path: Path) -> dict[str, object]:
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if exists is None:
            return {"database": str(db_path), "status": "no_documents_table", "quarantine": []}
        rows = conn.execute(
            "SELECT child.id, child.ticker, child.doc_type, child.sha256, child.parent_document_id "
            "FROM documents child LEFT JOIN documents parent ON parent.id=child.parent_document_id "
            "WHERE child.parent_document_id IS NOT NULL AND parent.id IS NULL ORDER BY child.id"
        ).fetchall()
        quarantine = [
            {
                "document_id": int(row["id"]),
                "ticker": row["ticker"],
                "doc_type": row["doc_type"],
                "parent_document_id": int(row["parent_document_id"]),
                "sha256": row["sha256"],
                "classification": "dangling_parent_reference",
                "action": "quarantine_for_manual_provenance_review",
                "automatic_repair": False,
                "reason": "no parent is relinked automatically; candidate bytes may differ",
            }
            for row in rows
        ]
        return {
            "database": str(db_path),
            "status": "ok",
            "checked": "documents.parent_document_id",
            "dangling_count": len(quarantine),
            "quarantine": quarantine,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only document parent-link audit")
    parser.add_argument("--db-path", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit_parent_links(args.db_path)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
