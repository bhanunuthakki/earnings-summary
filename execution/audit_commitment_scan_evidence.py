"""Read-only root-cause inventory for selected transcript scan evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path, PurePosixPath

from llm.prompt_versions import prompt_version_for
from pipeline.commitment_scan_receipts import (
    commitment_scan_coverage,
    current_transcript_scan_binding,
)
from provenance.selection import selected_transcripts_relation
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _recorded_path(project_root: Path, relative: str) -> Path | None:
    relpath = PurePosixPath(relative)
    if relpath.is_absolute() or ".." in relpath.parts or not relpath.parts:
        return None
    absolute_root = project_root.absolute()
    candidate = absolute_root.joinpath(*relpath.parts)
    try:
        candidate.relative_to(absolute_root)
    except ValueError:
        return None
    return candidate


def audit_commitment_scan_evidence(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
    ticker: str | None = None,
) -> dict[str, object]:
    """Classify root evidence failures without mutating source or database state."""

    relation = selected_transcripts_relation(conn).sql
    sql = (
        "SELECT t.id AS transcript_id,UPPER(t.ticker) AS ticker,t.period_end,"
        "d.id AS document_id,d.file_path,d.sha256 "
        f"FROM {relation} AS t JOIN documents AS d ON d.id=t.document_id "  # nosec B608
        "WHERE EXISTS (SELECT 1 FROM tracked_companies AS tc "
        "WHERE UPPER(tc.ticker)=UPPER(t.ticker) AND tc.list_type='portfolio' "
        "AND tc.archived_at IS NULL)"
    )
    params: tuple[object, ...] = ()
    if ticker is not None:
        sql += " AND UPPER(t.ticker)=?"
        params = (ticker.upper(),)
    sql += " ORDER BY UPPER(t.ticker),t.period_end DESC,t.id"
    version = prompt_version_for("saydo_commitment_extract")
    details: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for row in conn.execute(sql, params).fetchall():
        transcript_id = int(row["transcript_id"])
        recorded_path = _recorded_path(project_root, str(row["file_path"]))
        if recorded_path is None or not recorded_path.is_file():
            reason = "artifact_missing"
        else:
            with recorded_path.open("rb") as source:
                observed_sha = hashlib.file_digest(source, "sha256").hexdigest()
            if observed_sha != str(row["sha256"]):
                reason = "artifact_hash_mismatch"
            else:
                receipt_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM transcript_acquisition_receipts "
                        "WHERE artifact_sha256=?",
                        (str(row["sha256"]),),
                    ).fetchone()[0]
                )
                if receipt_count == 0:
                    reason = "acquisition_receipt_missing"
                elif current_transcript_scan_binding(conn, transcript_id) is None:
                    reason = "acquisition_binding_invalid"
                else:
                    coverage = commitment_scan_coverage(
                        conn,
                        transcript_id=transcript_id,
                        prompt_version=version,
                    )
                    reason = coverage.state.value
        counts[reason] += 1
        details.append(
            {
                "transcript_id": transcript_id,
                "ticker": str(row["ticker"]),
                "document_id": int(row["document_id"]),
                "period_end": str(row["period_end"]),
                "reason": reason,
            }
        )
    return {
        "schema_version": "commitment-scan-evidence-audit@1",
        "selected_transcript_count": len(details),
        "reason_counts": dict(sorted(counts.items())),
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--ticker")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    connection = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    connection.execute("PRAGMA query_only=ON")
    try:
        result = audit_commitment_scan_evidence(
            connection,
            project_root=args.project_root,
            ticker=args.ticker,
        )
    finally:
        connection.close()
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
