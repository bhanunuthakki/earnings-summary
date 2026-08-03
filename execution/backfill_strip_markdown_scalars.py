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
  - analyst_notes.body                        (SQLite; FTS mirror rebuilt)
  - standup_messages.headline                 (SQLite)
  - standup_messages.conclusion               (SQLite)
  - predictions.kpi_name                      (SQLite)
  - micro_thesis/holdings/<T>.json scalars    (compute.holdings_sanitize map)

``analyst_notes.body`` is the note-sized feed scalar (``"{title} — {summary}"``);
two legacy advisor-memo notes (source_ref advisor_memo:1 / :4) predate the
persist-time strippers and still carry raw ``**``/``##``. Its column feeds an
external-content FTS5 index (``analyst_notes_fts``) kept in sync by AFTER-UPDATE
triggers — fragile (any ``batch_alter_table('analyst_notes')`` drops them, per
alembic 0128/0130), so after rewriting any body we REBUILD the index from the
base table to guarantee the mirror matches regardless of trigger state.

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
import contextlib
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
    ("analyst_notes", "id", "body"),
    ("standup_messages", "id", "headline"),
    ("standup_messages", "id", "conclusion"),
    ("predictions", "id", "kpi_name"),
)


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    """True when ``name`` is a table (incl. FTS5 virtual tables) in this DB."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def sweep_db(conn: sqlite3.Connection, *, apply: bool) -> Counter[str]:
    """Strip markdown from the scalar DB columns; returns per-target counts."""
    tally: Counter[str] = Counter()
    conn.row_factory = sqlite3.Row
    for table, id_col, col in _DB_TARGETS:
        target = f"{table}.{col}"
        # Skip a target whose table this DB doesn't have (e.g. a checkout that
        # predates the migration adding it) — a generic sweep must degrade, not
        # crash, on a missing table. At head every target table exists.
        if not _table_present(conn, table):
            tally[f"{target} table absent"] += 1
            continue
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
        # analyst_notes.body feeds the external-content FTS5 index
        # analyst_notes_fts, synced by AFTER-UPDATE triggers that a later
        # batch_alter can silently drop. If we rewrote any body, rebuild the
        # index from the base table so the search mirror matches whether or not
        # the trigger fired. No-op when FTS5 is unavailable (table absent).
        if tally.get("analyst_notes.body updated") and _table_present(conn, "analyst_notes_fts"):
            conn.execute("INSERT INTO analyst_notes_fts(analyst_notes_fts) VALUES ('rebuild')")
            tally["analyst_notes_fts rebuilt"] += 1
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
    # Windows consoles default to cp1252; the swept scalars carry em-dashes and
    # math symbols (e.g. ``≥``) from advisor memos / standup briefs that a
    # cp1252 ``print`` cannot encode — reconfigure our own streams to UTF-8 so
    # the row-diff log never aborts the sweep mid-run. (Repo Windows rule:
    # be explicit about UTF-8 for subprocess text.)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError):
                reconfigure(encoding="utf-8")
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
