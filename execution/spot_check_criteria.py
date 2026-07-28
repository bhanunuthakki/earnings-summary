"""Manual spot-check of derived per-case checklists (meta_eval_governance.md §3.5).

Samples N cached ``query_criteria`` rows and prints each checklist beside the
head of the prompt it was derived from, for the one question a human can answer
better than a meter: **is each item actually entailed by the prompt?** (Rule 2 —
a checklist item asserting world facts the prompt doesn't supply is a smuggled
pseudo-golden answer.) Mirrors ``spot_check_eval_judge.py``; run them together.

Pure read; safe anywhere:
    python execution/spot_check_criteria.py --repo-root <MAIN> --n 5
    python execution/spot_check_criteria.py --repo-root <MAIN> --purpose bear_case
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--n", type=int, default=5, help="checklists to sample (default 5)")
    parser.add_argument("--purpose", default=None, help="restrict to one purpose")
    args = parser.parse_args()

    db_path: Path = args.db or (args.repo_root / "data" / "portfolio.db")
    if not db_path.exists():
        sys.stderr.write(f"DB not found at {db_path}\n")
        return 2

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "query_criteria" not in tables:
            print("query_criteria table absent — nothing derived yet (PR4 migration 0135).")
            return 0
        where = " WHERE purpose = ?" if args.purpose else ""
        params: tuple[str, ...] = (args.purpose,) if args.purpose else ()
        rows = conn.execute(
            f"""
            SELECT purpose, prompt_sha256, criteria_version, criteria_json,
                   derived_by_model, derived_at
            FROM query_criteria{where}
            ORDER BY RANDOM() LIMIT ?
            """,
            (*params, args.n),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("no cached checklists to sample.")
        return 0

    print(f"=== CRITERIA SPOT-CHECK — {len(rows)} sampled checklist(s) ===")
    print("For each item ask: is it ENTAILED by the task prompt (rule 2), decidable")
    print("from a response alone (rule 1), and binary/ternary (rule 3)?\n")
    for r in rows:
        print(
            f"--- {r['purpose']} · sha {str(r['prompt_sha256'])[:12]} · "
            f"{r['criteria_version']} · {r['derived_by_model']} · {str(r['derived_at'])[:16]}"
        )
        try:
            items: object = json.loads(str(r["criteria_json"]))
        except json.JSONDecodeError:
            print("  (unparseable criteria_json)")
            continue
        if isinstance(items, list):
            for item_obj in cast("list[object]", items):
                if isinstance(item_obj, dict):
                    item = cast("dict[str, object]", item_obj)
                    print(
                        f"  {item.get('id')} ({item.get('kind')}, w{item.get('weight')}): "
                        f"{item.get('statement')}"
                    )
        print()
    print("Record the verdicts as manual:criteria_spot_check notes (mirrors the")
    print("judge-agreement spot-check convention).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
