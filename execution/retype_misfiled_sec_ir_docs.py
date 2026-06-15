"""Retype SEC filings that were mis-stored under an IR doc_type.

The LLM intake classifier (`llm_client.classify_intake_document`) has no SEC form
doc-types in its closed enum, so a 10-K/10-Q dropped into the inbox was forced
into the nearest IR bucket (a numbers-heavy 10-K → `ir_supplement`) and named
`ir_<type>__<sha8>.pdf`; the reindexer then registered that filename verbatim.
`src/intake.py` now vetoes this at the source (deterministic `detect_sec_form`
override), but the rows already in `documents` need a one-shot correction.

For every `(source_type='ir_doc', doc_type=ir_*)` PDF whose COVER PAGE is actually
a SEC form, this:
  1. renames the file `ir_<type>__<sha8>.pdf` → `<sec_type>__<sha8>.pdf` (same dir),
  2. `UPDATE documents SET doc_type=<sec_type>, file_path=<renamed>` for that row.

`source_type` stays `ir_doc` — these PDFs WERE obtained via the IR channel, and
`(sec_*, ir_doc)` is the established shape for 150+ IR-sourced SEC PDFs already in
the store. Only `doc_type` (and the matching filename) was wrong. Rows with facts
attached are reported but skipped unless `--force` (retyping shouldn't strand
provenance silently). Idempotent: a re-run finds nothing once corrected.

Usage:
    python execution/retype_misfiled_sec_ir_docs.py            # dry-run (default)
    python execution/retype_misfiled_sec_ir_docs.py --apply
    python execution/retype_misfiled_sec_ir_docs.py --apply --force
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# data/ + ir_documents/ live in the MAIN checkout, not a worktree — honor the same
# override the categorizer uses so this runs against the real store from anywhere.
_OVERRIDE = os.environ.get("IR_PROJECT_ROOT")
if _OVERRIDE:
    PROJECT_ROOT = Path(_OVERRIDE).resolve()

from ir_uploads import detect_sec_form, fingerprint  # noqa: E402

_IR_DOC_TYPES = (
    "ir_press_release",
    "ir_presentation",
    "ir_supplement",
    "ir_investor_update",
    "ir_transcript",
    "ir_event",
)


@dataclass
class Retype:
    """One planned correction."""

    doc_id: int
    ticker: str
    old_doc_type: str
    new_doc_type: str
    old_file_path: str
    new_file_path: str
    kpi_facts: int
    financial_facts: int


def _detect_sec_for(repo_root: Path, rel_path: str) -> str | None:
    """Detected SEC DocType value for a stored file_path, or None (non-PDF, missing,
    or not a SEC form). Resolves a relative file_path against ``repo_root``."""
    if not rel_path.lower().endswith(".pdf"):
        return None
    p = Path(rel_path)
    if not p.is_absolute():
        p = repo_root / p
    if not p.exists():
        return None
    try:
        hit = detect_sec_form(fingerprint(p))
    except Exception:
        return None
    return hit.value if hit is not None else None


def plan_retypes(conn: sqlite3.Connection, repo_root: Path) -> list[Retype]:
    """Scan ir_doc PDFs and return the SEC-form mis-types that need correcting."""
    placeholders = ",".join("?" for _ in _IR_DOC_TYPES)
    rows = conn.execute(
        f"SELECT id, ticker, doc_type, file_path FROM documents "
        f"WHERE source_type = 'ir_doc' AND doc_type IN ({placeholders}) "
        f"AND LOWER(file_path) LIKE '%.pdf' ORDER BY ticker, id",
        _IR_DOC_TYPES,
    ).fetchall()
    out: list[Retype] = []
    for r in rows:
        old_path = str(r["file_path"])
        sec = _detect_sec_for(repo_root, old_path)
        if sec is None or sec == r["doc_type"]:
            continue
        # Rename the basename's doc_type token: ir_<type>__<sha8> -> <sec>__<sha8>.
        old_p = Path(old_path)
        stem = old_p.name
        sep = "__"
        new_name = f"{sec}{sep}{stem.split(sep, 1)[1]}" if sep in stem else f"{sec}{sep}{stem}"
        new_path = old_p.with_name(new_name).as_posix()
        kf = int(
            conn.execute(
                "SELECT COUNT(*) FROM kpi_facts WHERE source_doc_id=?", (r["id"],)
            ).fetchone()[0]
        )
        try:
            ff = int(
                conn.execute(
                    "SELECT COUNT(*) FROM financial_facts WHERE source_doc_id=?", (r["id"],)
                ).fetchone()[0]
            )
        except sqlite3.Error:
            ff = 0
        out.append(
            Retype(
                doc_id=int(r["id"]),
                ticker=str(r["ticker"]),
                old_doc_type=str(r["doc_type"]),
                new_doc_type=sec,
                old_file_path=old_path,
                new_file_path=new_path,
                kpi_facts=kf,
                financial_facts=ff,
            )
        )
    return out


def apply_retype(conn: sqlite3.Connection, repo_root: Path, rt: Retype) -> None:
    """Rename the file on disk (if present) and UPDATE the documents row. The DB
    write is the source of truth; a missing/already-renamed file is tolerated so a
    partially-applied run can be safely re-run."""
    old_abs = (
        repo_root / rt.old_file_path
        if not Path(rt.old_file_path).is_absolute()
        else Path(rt.old_file_path)
    )
    new_abs = (
        repo_root / rt.new_file_path
        if not Path(rt.new_file_path).is_absolute()
        else Path(rt.new_file_path)
    )
    if old_abs.exists() and not new_abs.exists():
        old_abs.rename(new_abs)
    conn.execute(
        "UPDATE documents SET doc_type = ?, file_path = ? WHERE id = ?",
        (rt.new_doc_type, rt.new_file_path, rt.doc_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Execute (default: dry-run).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also retype rows that already have kpi_facts/financial_facts attached.",
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        plan = plan_retypes(conn, repo_root)
        applied = 0
        skipped_facts = 0
        for rt in plan:
            has_facts = rt.kpi_facts or rt.financial_facts
            if has_facts and not args.force:
                skipped_facts += 1
                continue
            if args.apply:
                apply_retype(conn, repo_root, rt)
                applied += 1
        if args.apply:
            conn.commit()
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "found": len(plan),
                    "applied": applied,
                    "skipped_with_facts": skipped_facts,
                    "retypes": [asdict(rt) for rt in plan],
                },
                indent=2,
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
