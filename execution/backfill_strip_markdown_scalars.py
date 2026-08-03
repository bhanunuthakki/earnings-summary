"""execution/backfill_strip_markdown_scalars.py
------------------------------------------------
One-shot (re-runnable) sweep stripping inline markdown out of LLM-persisted
SCALAR fields that predate the persist-time strippers
(``llm.postprocess.strip_inline_markdown``, wired 2026-08 into the Say-Do
``CommitmentInput`` validator, ``advisor.memos.persist_memo``, and the
holdings-JSON edit routes via ``compute.holdings_sanitize``).

Observed contamination class: a Say-Do metric name stored as
``**Risk-adj. NIM**``, a thesis priority stored as
``**Priority #1 — Mexico momentum**``. The rule: scalars plain, prose/body
fields keep markdown — prose columns (advisor_memos.body_md,
llm_artifacts.content_md, predictions.prediction_md, holdings ``thesis``)
are deliberately NOT touched.

Swept targets:
  - management_commitments.kpi_name           (SQLite)
  - advisor_memos.title                       (SQLite)
  - micro_thesis/holdings/<T>.json scalars    (compute.holdings_sanitize map)

Idempotent: the stripper is a fixpoint transform, so re-running rewrites
nothing. Rows/files already plain are skipped and only counted.

Note: holdings JSON edits are mirrored into thesis_state.raw_json by the
evaluator — after an --apply that touches holdings files, the next
run_thesis_evaluator.py pass refreshes the mirror (this script prints a
reminder when that applies).

Usage:
    python execution/backfill_strip_markdown_scalars.py             # dry run
    python execution/backfill_strip_markdown_scalars.py --apply
    python execution/backfill_strip_markdown_scalars.py --db C:/path/portfolio.db --apply
    python execution/backfill_strip_markdown_scalars.py --holdings-dir C:/path/holdings --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import cast

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from compute.holdings_sanitize import sanitize_holdings_scalars  # noqa: E402
from llm.postprocess import strip_inline_markdown  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# (table, id column, scalar column) triples swept in the DB.
_DB_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("management_commitments", "id", "kpi_name"),
    ("advisor_memos", "id", "title"),
)


def sweep_db(conn: sqlite3.Connection, *, apply: bool) -> Counter[str]:
    """Strip markdown from the scalar DB columns; returns per-target counts."""
    tally: Counter[str] = Counter()
    conn.row_factory = sqlite3.Row
    for table, id_col, col in _DB_TARGETS:
        target = f"{table}.{col}"
        # Identifiers come from the _DB_TARGETS module literal, never from
        # input; values are bound parameters.
        rows = conn.execute(
            f"SELECT {id_col} AS id, {col} AS val FROM {table}"  # nosec B608
        ).fetchall()
        for row in rows:
            tally[f"{target} scanned"] += 1
            val = row["val"]
            if not isinstance(val, str):
                continue
            plain = strip_inline_markdown(val)
            if plain == val:
                continue
            tally[f"{target} dirty"] += 1
            print(f"  {target} id={row['id']}: {val!r} -> {plain!r}")
            if apply:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {id_col} = ?",  # nosec B608
                    (plain, row["id"]),
                )
                tally[f"{target} updated"] += 1
    if apply:
        conn.commit()
    return tally


def sweep_holdings(holdings_dir: Path, *, apply: bool) -> Counter[str]:
    """Strip markdown from the scalar fields of every holdings JSON file."""
    tally: Counter[str] = Counter()
    for path in sorted(holdings_dir.glob("*.json")):
        tally["holdings scanned"] += 1
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        changed = sanitize_holdings_scalars(cast("dict[str, object]", payload))
        if not changed:
            continue
        tally["holdings dirty"] += 1
        print(f"  {path.name}: {', '.join(changed)}")
        if apply:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tally["holdings updated"] += 1
    return tally


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path (default: <repo-root>/data/portfolio.db)",
    )
    parser.add_argument(
        "--holdings-dir",
        type=Path,
        default=None,
        help="holdings JSON dir (default: <repo-root>/micro_thesis/holdings)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the stripped values (default: dry run, report only)",
    )
    args = parser.parse_args()

    db_path = (args.db or (PROJECT_ROOT / "data" / "portfolio.db")).resolve()
    holdings_dir = (args.holdings_dir or (PROJECT_ROOT / "micro_thesis" / "holdings")).resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    mode = "applying" if args.apply else "dry-run"
    print(f"{mode} markdown-scalar strip on {db_path}")
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        tally = sweep_db(conn, apply=args.apply)
    finally:
        conn.close()
    if holdings_dir.is_dir():
        print(f"{mode} markdown-scalar strip on {holdings_dir}")
        tally += sweep_holdings(holdings_dir, apply=args.apply)
    else:
        print(f"holdings dir not found, skipping: {holdings_dir}")

    print("tally:")
    for key in sorted(tally):
        print(f"  {key}: {tally[key]}")
    if args.apply and tally.get("holdings updated"):
        print(
            "holdings files changed — run execution/run_thesis_evaluator.py to "
            "refresh the thesis_state mirror."
        )
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
