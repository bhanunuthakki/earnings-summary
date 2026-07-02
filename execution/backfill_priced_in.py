"""Backfill the reverse-DCF ``priced_in`` block into existing ``dcf_runs`` rows.

``execution/refresh_dcf`` writes the ``priced_in`` block (``dcf.reverse``) into
``dcf_runs.assumption_snapshot_json`` on every redesign refresh, but rows valued
before that shipped carry no block, so their valuation card can't show "Priced in
vs your case" until the next full refresh. This script backfills them WITHOUT an
FMP rebuild: for each redesigned ``dcf/<T>.xlsx`` it reads the workbook inputs,
solves the market-implied assumption set at the row's persisted live price, and
patches ONLY the ``priced_in`` key into the existing snapshot JSON — every other
field (npv, over_under, scenarios, sync status) is left byte-for-byte untouched.

Idempotent and safe: re-running just recomputes the same block. Defaults to a
dry run; pass ``--apply`` to write. Bespoke-archetype rows (bank / holdco /
fintech / platform) are skipped — they have no redesigned workbook to invert, so
the card shows an explicit n/a instead.
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

from db_paths import resolve_db_path  # noqa: E402
from dcf import redesign as redesign_mod  # noqa: E402
from dcf import reverse as reverse_mod  # noqa: E402
from dcf import universe as universe_mod  # noqa: E402

DCF_DIR_NAME = "dcf"


def patch_snapshot_priced_in(snapshot_json: str, priced_in: dict[str, object] | None) -> str:
    """Return ``snapshot_json`` with its top-level ``priced_in`` key set to
    ``priced_in`` (or removed when None), preserving every other key.

    Pure + total: the single point that mutates the snapshot, so the backfill
    can never accidentally rewrite npv / over_under / scenarios. Raises
    ``ValueError`` on a non-object snapshot (a corrupt row the caller should skip,
    not silently overwrite)."""
    data: object = json.loads(snapshot_json) if snapshot_json else {}
    if not isinstance(data, dict):
        raise ValueError("assumption_snapshot_json is not a JSON object")
    obj = cast("dict[str, object]", data)
    if priced_in is None:
        obj.pop("priced_in", None)
    else:
        obj["priced_in"] = priced_in
    return json.dumps(obj, indent=2)


def _priced_in_for(
    repo_root: Path, ticker: str, live_price: float | None
) -> dict[str, object] | None:
    """Solve the ``priced_in`` block for one ticker's redesigned workbook, at the
    row's persisted live price (the value-of-record price, so the block agrees
    with the card's over/under). None when there is no redesigned workbook, the
    inputs are unreadable, or no usable price/base value."""
    dest = repo_root / DCF_DIR_NAME / f"{ticker.upper()}.xlsx"
    if not redesign_mod.is_redesign_format(dest):
        return None
    try:
        inp = redesign_mod.read_inputs(dest)
    except redesign_mod.RedesignError:
        return None
    if inp is None:
        return None
    pi = reverse_mod.solve_priced_in(inp, price=live_price)
    return pi.to_snapshot_dict() if pi is not None else None


def backfill(repo_root: Path, db_path: Path, *, apply: bool) -> dict[str, int]:
    """Patch the ``priced_in`` block into every redesigned name's ``dcf_runs``
    row. Returns a small counts summary. Read-only unless ``apply`` is True."""
    tickers = universe_mod.dcf_universe(repo_root)
    counts = {"eligible": 0, "patched": 0, "skipped_no_workbook": 0, "no_row": 0, "unchanged": 0}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for ticker in tickers:
            row = conn.execute(
                "SELECT assumption_snapshot_json, live_price FROM dcf_runs WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
            if row is None:
                counts["no_row"] += 1
                continue
            live = float(row["live_price"]) if row["live_price"] is not None else None
            pi = _priced_in_for(repo_root, ticker, live)
            if pi is None:
                counts["skipped_no_workbook"] += 1
                continue
            counts["eligible"] += 1
            existing = row["assumption_snapshot_json"] or ""
            try:
                patched = patch_snapshot_priced_in(str(existing), pi)
            except ValueError:
                counts["skipped_no_workbook"] += 1
                continue
            if patched == str(existing):
                counts["unchanged"] += 1
                continue
            counts["patched"] += 1
            g = pi.get("growth")
            gi = g.get("implied_value") if isinstance(g, dict) else None
            print(f"  {ticker.upper():6s} priced_in patched (implied growth CAGR={gi})")
            if apply:
                conn.execute(
                    "UPDATE dcf_runs SET assumption_snapshot_json = ? WHERE ticker = ?",
                    (patched, ticker.upper()),
                )
        if apply:
            conn.commit()
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="repo root (holds dcf/<T>.xlsx)")
    parser.add_argument("--db-path", default=None, help="portfolio.db (default: resolved)")
    parser.add_argument("--apply", action="store_true", help="write the patches (default: dry run)")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    db_path = resolve_db_path(args.db_path)
    if db_path is None or not db_path.exists():
        print(f"ERROR: no portfolio.db found (db_path={db_path})", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"Backfilling priced_in — {mode}\n  repo_root={repo_root}\n  db={db_path}")
    counts = backfill(repo_root, db_path, apply=args.apply)
    print(
        "\nSummary: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
        + ("" if args.apply else "  (dry run — nothing written)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
