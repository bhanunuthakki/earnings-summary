"""Backfill ``decisions.basis_*`` for advisor recommendations made before capture.

The advisor decisions (five_min_reread lens) predate basis capture (migration 0137
+ the PR2 write-path): each recorded the DCF fair value it stood on only in its
rationale prose ("... vs. NPV $91 ..."). This parses that value out and lands it as
a structured basis snapshot, so ``v_decision_freshness`` can flag the row once the
current model has moved (e.g. RBRK's corrected $91 → $66.45). Idempotent: only rows
with ``basis_kind`` NULL are touched.

The historical run itself is gone — dcf_runs overwrote before 0137 — so
``basis_ref_id`` stays NULL and ``basis_as_of`` is the decision's ``made_at`` date.
The freshness view compares the snapshotted VALUE to the current model, which is
what determines staleness; the exact prior run id is not needed.

Usage (run against the MAIN checkout's data/ dir):

    python execution/backfill_decision_basis.py            # dry-run (prints plan)
    python execution/backfill_decision_basis.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ordered most-specific first. The five_min_reread DCF snapshot renders
# "NPV/share: $91 · Live: $78 · ..."; the extracted rationale says "vs. NPV $91".
_FAIR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"NPV\s*/?\s*share\s*[:=]?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    re.compile(r"\bNPV\b[^$0-9]{0,8}\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    re.compile(r"\bfair(?:\s+value)?\b[^$0-9]{0,10}\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
)


def parse_fair_value(text: str | None) -> float | None:
    """Pull the DCF fair value the memo cited, or None if none is stated."""
    if not text:
        return None
    for pat in _FAIR_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if v > 0:
                return v
    return None


def backfill(db_path: Path | str, *, apply: bool) -> dict[str, int]:
    """Set a DCF basis snapshot on basis-less decisions from their rationale.
    Returns a tally. Raises if the schema predates 0137."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(decisions)")}
        if "basis_kind" not in cols:
            raise SystemExit(
                "decisions.basis_* columns are missing — run `alembic upgrade head` "
                "(migration 0137) before backfilling"
            )
        rows = conn.execute(
            "SELECT id, ticker, made_at, rationale_excerpt, source_prose "
            "FROM decisions WHERE basis_kind IS NULL"
        ).fetchall()
        tally = {"scanned": 0, "parsed": 0, "updated": 0, "no_value": 0}
        meta = json.dumps({"source": "rationale_backfill"})
        for r in rows:
            tally["scanned"] += 1
            text = f"{r['rationale_excerpt'] or ''}\n{r['source_prose'] or ''}"
            fair = parse_fair_value(text)
            if fair is None:
                tally["no_value"] += 1
                continue
            tally["parsed"] += 1
            as_of = (str(r["made_at"] or "")[:10]) or None
            if apply:
                conn.execute(
                    "UPDATE decisions SET basis_kind='dcf', basis_ref_id=NULL, basis_value=?, "
                    "basis_as_of=?, basis_meta_json=? WHERE id=? AND basis_kind IS NULL",
                    (fair, as_of, meta, int(r["id"])),
                )
                tally["updated"] += 1
            else:
                print(
                    f"  decision {r['id']} ({r['ticker']}): basis_value=${fair:,.2f} as_of={as_of}"
                )
        if apply:
            conn.commit()
        return tally
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    tally = backfill(args.db, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] scanned={tally['scanned']} parsed={tally['parsed']} "
        f"updated={tally['updated']} no_value={tally['no_value']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
