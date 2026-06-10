"""execution/backfill_document_accessions.py
---------------------------------------------
One-shot (re-runnable) backfill of the filing-identity columns added by
alembic 0075 — ``documents.accession_number`` + ``documents.filing_date``.

Derivation, best-effort per row and never overwriting a non-NULL value:

* **accession_number** — three sources, in order:
    1. our own sec_xbrl ingestion convention: a ``#accn=<accession>``
       fragment on ``documents.file_path`` (see src/pipeline/sec_xbrl.py);
    2. a dashed accession embedded as a ``file_path`` segment
       (the ``data/historical/sec/{TICKER}/{accession}/`` layout);
    3. an EDGAR URL in ``documents.source_url`` — sec.gov URLs embed the
       accession either dashed (``0001628280-26-033127``) or as the
       18-digit no-dash Archives directory; both normalize to the
       canonical dashed form. Non-sec.gov URLs are never scanned (an
       18-digit run in a CDN URL is not an accession).

* **filing_date** — for rows whose accession is known and whose base file
  is a JSON document, the SEC companyfacts payload maps every ``accn`` to
  its ``filed`` date (ISO YYYY-MM-DD); the row gets that date. FMP endpoint
  dumps span many filings, so a document-level filing_date is undefined for
  them and deliberately stays NULL (per-fact provenance is the locator
  column's job — P3.2 wires it at extraction time).

Fully idempotent: only NULL columns are ever written, so re-running after
new documents land backfills just the new rows.

Usage:
    python execution/backfill_document_accessions.py
    python execution/backfill_document_accessions.py --ticker NU --dry-run
    python execution/backfill_document_accessions.py --repo-root C:/path/to/repo
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

_DASHED_RE = re.compile(r"(?<!\d)(\d{10}-\d{2}-\d{6})(?!\d)")
_NODASH_RE = re.compile(r"(?<![\d-])(\d{18})(?![\d-])")

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent


def normalize_accession(raw: str) -> str | None:
    """Canonical dashed EDGAR accession, or None if `raw` is not one.

    Accepts the dashed form (``0001628280-21-010389``) or the 18-digit
    no-dash form EDGAR uses in Archives directory names.
    """
    s = raw.strip()
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", s):
        return s
    if re.fullmatch(r"\d{18}", s):
        return f"{s[:10]}-{s[10:12]}-{s[12:]}"
    return None


def derive_accession(source_url: str | None, file_path: str | None) -> str | None:
    """Best-effort accession for a documents row; None when underivable."""
    if file_path and "#accn=" in file_path:
        acc = normalize_accession(file_path.split("#accn=", 1)[1])
        if acc is not None:
            return acc
    if file_path:
        m = _DASHED_RE.search(file_path)
        if m is not None:
            return m.group(1)
    if source_url and "sec.gov" in source_url:
        m = _DASHED_RE.search(source_url)
        if m is not None:
            return m.group(1)
        m = _NODASH_RE.search(source_url)
        if m is not None:
            return normalize_accession(m.group(1))
    return None


def accession_filed_map(path: Path) -> dict[str, str]:
    """accn -> filed (ISO date) parsed from a SEC companyfacts JSON.

    Returns {} for missing, unreadable, or non-companyfacts payloads —
    callers treat an empty map as "no filing dates derivable from this file".
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = cast("object", json.load(f))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    facts = cast("dict[str, object]", payload).get("facts")
    if not isinstance(facts, dict):
        return {}
    out: dict[str, str] = {}
    for taxonomy in cast("dict[str, object]", facts).values():
        if not isinstance(taxonomy, dict):
            continue
        for concept in cast("dict[str, object]", taxonomy).values():
            if not isinstance(concept, dict):
                continue
            units = cast("dict[str, object]", concept).get("units")
            if not isinstance(units, dict):
                continue
            for items in cast("dict[str, object]", units).values():
                if not isinstance(items, list):
                    continue
                for item in cast("list[object]", items):
                    if not isinstance(item, dict):
                        continue
                    record = cast("dict[str, object]", item)
                    accn = record.get("accn")
                    filed = record.get("filed")
                    if isinstance(accn, str) and isinstance(filed, str) and accn not in out:
                        out[accn] = filed
    return out


def _new_counter() -> Counter[str]:
    return Counter()


@dataclass(slots=True)
class BackfillResult:
    """Aggregate outcome of one backfill pass."""

    scanned: int = 0
    accession_set: int = 0
    filing_date_set: int = 0
    accession_underivable: int = 0
    filing_date_underivable: int = 0
    underivable_by_source: Counter[str] = field(default_factory=_new_counter)


def backfill(
    repo_root: Path,
    db_path: Path,
    *,
    only_ticker: str | None = None,
    dry_run: bool = False,
    log: bool = True,
) -> BackfillResult:
    """Walk incomplete documents rows and fill accession_number/filing_date."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        if not {"accession_number", "filing_date"} <= cols:
            raise RuntimeError(
                "documents is missing accession_number/filing_date — run "
                "`alembic upgrade head` (0075) before backfilling"
            )

        sql = (
            "SELECT id, ticker, source_type, file_path, source_url, "
            "       accession_number, filing_date "
            "FROM documents "
            "WHERE (accession_number IS NULL OR filing_date IS NULL)"
        )
        params: tuple[str, ...] = ()
        if only_ticker is not None:
            sql += " AND ticker = ?"
            params = (only_ticker.upper(),)

        result = BackfillResult()
        filed_maps: dict[Path, dict[str, str]] = {}
        for row in conn.execute(sql, params).fetchall():
            result.scanned += 1
            file_path: str | None = row["file_path"]
            accession: str | None = row["accession_number"] or derive_accession(
                row["source_url"], file_path
            )
            if accession is None:
                result.accession_underivable += 1
                result.underivable_by_source[str(row["source_type"])] += 1
                continue

            filing_date: str | None = row["filing_date"]
            if filing_date is None and file_path:
                base = repo_root / file_path.split("#", 1)[0]
                if base.suffix == ".json":
                    if base not in filed_maps:
                        filed_maps[base] = accession_filed_map(base)
                    filing_date = filed_maps[base].get(accession)
            if filing_date is None:
                result.filing_date_underivable += 1

            new_accession = accession if row["accession_number"] is None else None
            new_filing = filing_date if row["filing_date"] is None and filing_date else None
            if new_accession is None and new_filing is None:
                continue
            if new_accession is not None:
                result.accession_set += 1
            if new_filing is not None:
                result.filing_date_set += 1
            if not dry_run:
                conn.execute(
                    "UPDATE documents SET "
                    "  accession_number = COALESCE(accession_number, ?), "
                    "  filing_date = COALESCE(filing_date, ?) "
                    "WHERE id = ?",
                    (new_accession, new_filing, row["id"]),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    if log:
        verb = "would backfill" if dry_run else "backfilled"
        print(f"scanned {result.scanned} incomplete documents row(s)")
        print(f"  accession_number {verb}: {result.accession_set}")
        print(f"  filing_date {verb}:      {result.filing_date_set}")
        print(f"  accession underivable (left NULL): {result.accession_underivable}")
        if result.underivable_by_source:
            by_src = ", ".join(
                f"{src}={n}" for src, n in result.underivable_by_source.most_common()
            )
            print(f"    by source_type: {by_src}")
        print(
            "  filing_date underivable (accession known, no filed source): "
            f"{result.filing_date_underivable}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path (default: <repo-root>/data/portfolio.db)",
    )
    parser.add_argument("--ticker", default=None, help="limit to one ticker")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="derive and report counts without writing",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    db_path = (args.db or (repo_root / "data" / "portfolio.db")).resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    print(f"backfilling documents filing identity in {db_path}")
    try:
        backfill(repo_root, db_path, only_ticker=args.ticker, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
