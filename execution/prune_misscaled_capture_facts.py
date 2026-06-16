"""Prune share/unit-COUNT capture facts that were mis-stored as monetary.

The Stage-A XBRL capture extractor (``table_extractors.generic_xbrl_capture``)
scales a note-table cell by its section's ``$ in millions/thousands`` factor. A
row whose label DECLARES its unit as a share/unit count in a trailing
parenthetical — "Issued and Outstanding (in shares)", "Authorized Shares
(in shares)", "Outstanding (in units)" — is a COUNT, not a currency level, so the
$-scale turns it into phantom billions/trillions (BN preferred units: 24M shares
-> $24T; RBRK's preferred-share schedule -> ~$74B each). The extractor's per-row
count guard matched word-order tokens ("shares outstanding"/"shares issued") and
missed the parenthetical form; that is now fixed in ``generic_xbrl_capture``
(``_COUNT_TOKENS``), so fresh runs DEFER these rows. This script removes the rows
already written by the supervised first ``--all`` pass.

Scope is deliberately narrow and origin-gated: only ``definition_origin='capture'``
facts whose definition name carries an explicit count-unit parenthetical AND whose
stored unit is monetary (``actual``). A genuine dollar line item that merely
mentions "shares" without a count-unit parenthetical (e.g. "Common shares |
Common equity" = real $ equity) is NOT matched. Analyst-authored definitions are
never touched. Idempotent: a re-run finds nothing once pruned.

Usage:
    python execution/prune_misscaled_capture_facts.py            # dry-run (default)
    python execution/prune_misscaled_capture_facts.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Explicit count-unit parentheticals — MUST mirror the count-paren markers added to
# generic_xbrl_capture._COUNT_TOKENS, so this prune removes exactly the rows the
# fixed extractor would now defer. Matched case-insensitively against the
# definition name.
_COUNT_UNIT_PARENS: tuple[str, ...] = ("(in shares)", "(shares)", "(in units)", "(units)")

_SELECT = """
    SELECT kf.id AS fact_id, kd.id AS def_id, kd.ticker AS ticker,
           kd.name AS name, kf.value AS value
    FROM kpi_facts kf
    JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
    WHERE kd.definition_origin = 'capture'
      AND kf.unit = 'actual'
"""


def _matches(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _COUNT_UNIT_PARENS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Execute (default: dry-run).")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = [r for r in conn.execute(_SELECT).fetchall() if _matches(str(r["name"]))]

    if not rows:
        print("Nothing to prune — 0 mis-scaled count facts found (idempotent).")
        return 0

    by_ticker: dict[str, int] = {}
    for r in rows:
        by_ticker[r["ticker"]] = by_ticker.get(r["ticker"], 0) + 1
    mx = max(rows, key=lambda r: abs(r["value"]))
    print(f"Found {len(rows)} mis-scaled count facts across {len(by_ticker)} ticker(s):")
    for t, n in sorted(by_ticker.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<6} {n}")
    print(f"  largest: {mx['value']:.0f}  ({mx['ticker']}  {mx['name'][:60]})")

    if not args.apply:
        print("\n[dry-run] re-run with --apply to delete these facts + any orphaned capture defs.")
        return 0

    fact_ids = [(int(r["fact_id"]),) for r in rows]
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.executemany("DELETE FROM kpi_facts WHERE id = ?", fact_ids)
    n_facts = cur.rowcount
    # Drop capture definitions left with no facts (analyst defs untouched).
    cur.execute(
        """
        DELETE FROM kpi_definitions
        WHERE definition_origin = 'capture'
          AND id NOT IN (SELECT DISTINCT kpi_definition_id FROM kpi_facts)
        """
    )
    n_defs = cur.rowcount
    conn.commit()
    print(f"\n[applied] deleted {n_facts} facts; removed {n_defs} now-orphaned capture defs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
