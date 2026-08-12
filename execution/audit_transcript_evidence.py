"""Read-only integrity audit for transcript document evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.evidence_snapshot import (  # noqa: E402
    capture_snapshot,
    recorded_evidence_location,
)


class IntegrityStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"
    MISSING_EXACT_ALIAS = "missing_exact_alias"
    MISSING_ALIAS_MISMATCH = "missing_alias_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNSAFE_PATH = "unsafe_path"
    UNREADABLE = "unreadable"


class EvidenceAuditItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int
    ticker: str
    period: str
    recorded_path: str
    recorded_sha256: str
    status: IntegrityStatus
    alias_path: str | None = None
    observed_sha256: str | None = None


class EvidenceAuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    documents_scanned: int
    failures: int
    counts: dict[IntegrityStatus, int]
    items: tuple[EvidenceAuditItem, ...]


def _sha256(path: Path, allowed_root: Path | None = None) -> str:
    return capture_snapshot(path, allowed_root or path.parent).sha256


def _safe_path(repo_root: Path, recorded: str) -> Path | None:
    candidate = Path(recorded)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = candidate.parts
    if len(parts) < 3 or parts[0] != "transcripts" or parts[1] not in {"raw", "processed"}:
        return None
    root = repo_root.resolve()
    intended = (root / parts[0] / parts[1]).resolve()
    lexical = root / candidate
    current = lexical
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        if current.is_symlink() or (isinstance(attributes, int) and bool(attributes & 0x400)):
            return None
        if current == root:
            break
        if root not in current.parents:
            return None
        current = current.parent
    try:
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(intended)
    except (OSError, ValueError):
        return None
    return resolved


def _alias_for(repo_root: Path, recorded: str) -> Path | None:
    path = Path(recorded)
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part == "raw":
            parts[index] = "processed"
            return _safe_path(repo_root, Path(*parts).as_posix())
        if part == "processed":
            parts[index] = "raw"
            return _safe_path(repo_root, Path(*parts).as_posix())
    return None


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def audit_transcript_evidence(
    repo_root: Path, db_path: Path, ticker: str | None = None
) -> EvidenceAuditReport:
    conn = _open_read_only(db_path)
    try:
        sql = (
            "SELECT DISTINCT d.id, d.ticker, d.file_path, d.sha256, "
            "t.fiscal_period_type, t.period_end FROM documents AS d "
            "JOIN transcripts AS t ON t.document_id = d.id"
        )
        params: tuple[str, ...] = ()
        if ticker:
            sql += " WHERE UPPER(d.ticker) = ?"
            params = (ticker.upper(),)
        sql += " ORDER BY d.ticker, t.period_end, d.id"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    items: list[EvidenceAuditItem] = []
    for row in rows:
        recorded = str(row["file_path"])
        expected = str(row["sha256"])
        path = _safe_path(repo_root, recorded)
        alias_path: str | None = None
        observed: str | None = None
        if path is None:
            status = IntegrityStatus.UNSAFE_PATH
        else:
            location = recorded_evidence_location(repo_root, recorded)
            if location is None:
                status = IntegrityStatus.UNSAFE_PATH
                items.append(
                    EvidenceAuditItem(
                        document_id=int(row["id"]),
                        ticker=str(row["ticker"]),
                        period=f"{row['fiscal_period_type']}:{row['period_end']}",
                        recorded_path=recorded,
                        recorded_sha256=expected,
                        status=status,
                    )
                )
                continue
            evidence_path, evidence_root = location
            try:
                observed = _sha256(evidence_path, evidence_root)
                status = (
                    IntegrityStatus.OK if observed == expected else IntegrityStatus.HASH_MISMATCH
                )
            except FileNotFoundError:
                alias = _alias_for(repo_root, recorded)
                if alias is None:
                    status = IntegrityStatus.MISSING
                else:
                    try:
                        alias_relative = alias.relative_to(repo_root.resolve())
                        alias_root = repo_root / alias_relative.parts[0] / alias_relative.parts[1]
                        observed = _sha256(alias, alias_root)
                        alias_path = str(alias.relative_to(repo_root.resolve())).replace("\\", "/")
                        status = (
                            IntegrityStatus.MISSING_EXACT_ALIAS
                            if observed == expected
                            else IntegrityStatus.MISSING_ALIAS_MISMATCH
                        )
                    except FileNotFoundError:
                        status = IntegrityStatus.MISSING
                    except OSError:
                        status = IntegrityStatus.UNREADABLE
            except OSError:
                status = IntegrityStatus.UNREADABLE
        items.append(
            EvidenceAuditItem(
                document_id=int(row["id"]),
                ticker=str(row["ticker"]),
                period=f"{row['fiscal_period_type']}:{row['period_end']}",
                recorded_path=recorded,
                recorded_sha256=expected,
                status=status,
                alias_path=alias_path,
                observed_sha256=observed,
            )
        )

    counts = {status: 0 for status in IntegrityStatus}
    for item in items:
        counts[item.status] += 1
    failures = len(items) - counts[IntegrityStatus.OK]
    return EvidenceAuditReport(
        documents_scanned=len(items),
        failures=failures,
        counts=counts,
        items=tuple(items),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--db", type=Path)
    parser.add_argument("--ticker")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    db_path = args.db.resolve() if args.db else repo_root / "data" / "portfolio.db"
    report = audit_transcript_evidence(repo_root, db_path, args.ticker)
    print(report.model_dump_json())
    sys.stderr.write(
        json.dumps(
            {
                "event": "transcript_evidence_audit_complete",
                "documents_scanned": report.documents_scanned,
                "failures": report.failures,
            }
        )
        + "\n"
    )
    return 0 if report.failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
