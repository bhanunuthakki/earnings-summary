"""Seed one-row-per-metric kpi_definitions from each ticker's holdings JSON.

Reads `micro_thesis/holdings/<TICKER>.json` and upserts a kpi_definitions row
for every entry in `tier_1_kpis` / `tier_2_kpis` / `tier_3_kpis`. Idempotent:
INSERT OR IGNORE on (ticker, name).

Replaces the prior hardcoded-snapshot design — the analyst edits tier_*_kpis
via the comment processor's `edit_structured` route, and that drift makes any
hardcoded list stale immediately. Reading the holdings JSON dynamically keeps
the kpi_definitions table in sync with the analyst's current monitorables.

Unit is inferred heuristically from the KPI name (margin/rate/% → PERCENT,
millions/customers/count → COUNT, USD/$ → ACTUAL, etc.). Primary source
defaults to IR_DOC since the bulk of bank/SaaS-specific KPIs aren't FMP-derivable.

Usage:
    python execution/seed_kpi_definitions.py --ticker NU
    python execution/seed_kpi_definitions.py --all
    python execution/seed_kpi_definitions.py --ticker NU --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import SourceType  # noqa: E402
from models.facts import Unit  # noqa: E402
from models.kpis import ThesisTier  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

_TIER_MAP: dict[str, ThesisTier] = {
    "tier_1_kpis": ThesisTier.TIER_1_BREAK,
    "tier_2_kpis": ThesisTier.TIER_2_MONITOR,
    # tier_3_kpis is informational — treat as tier-2 for kpi_definitions seeding
    # since the ThesisTier enum only has BREAK + MONITOR levels.
    "tier_3_kpis": ThesisTier.TIER_2_MONITOR,
}


def infer_unit(name: str) -> Unit:
    """Best-effort unit from the KPI name. Defaults to PERCENT.

    Order matters — more specific patterns first so e.g. "ratio" + "%" name
    doesn't fall through to RATIO when PERCENT is more idiomatic.
    """
    n = name.lower()
    # COUNT for headcount / customer count / number-of-X
    if any(tok in n for tok in (
        "customers", "customer count", "millions", "headcount", "subscribers",
        "users", "accounts", "members", "merchants",
    )):
        return Unit.COUNT
    # ACTUAL for dollar-denominated absolute values (ARPAC, cost-to-serve, $X revenue)
    if any(tok in n for tok in ("usd", "$", "dollar", "arpac", "cost-to-serve", "cost to serve")):
        return Unit.ACTUAL
    # BPS for explicit basis-point measures (rare but possible)
    if "bps" in n or "basis point" in n:
        return Unit.BPS
    # RATIO when explicitly "ratio" without a % suffix or "rate" word
    if "ratio" in n and "%" not in n and "rate" not in n:
        return Unit.RATIO
    # PERCENT is the most common — margins, growth rates, NPL%, NIM%, ROE%, penetration%
    return Unit.PERCENT


def collect_kpis_from_holdings(
    repo_root: Path, ticker: str
) -> list[tuple[str, Unit, SourceType, ThesisTier]]:
    """Read tier_*_kpis from the holdings JSON; return [(name, unit, source, tier), ...]."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    specs: list[tuple[str, Unit, SourceType, ThesisTier]] = []
    seen_names: set[str] = set()
    for tier_key, tier_enum in _TIER_MAP.items():
        kpis = payload.get(tier_key) or []
        if not isinstance(kpis, list):
            continue
        for k in kpis:
            if not isinstance(k, dict):
                continue
            name = str(k.get("name", "")).strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            unit = infer_unit(name)
            # `source` field in holdings JSON is freeform (e.g. "earnings release",
            # "IR press release"). Map to IR_DOC by default — that's the canonical
            # source for bank/SaaS-specific KPIs that the LLM extractor targets.
            source = SourceType.IR_DOC
            specs.append((name, unit, source, tier_enum))
    return specs


def seed_for_ticker(
    conn, repo_root: Path, ticker: str, *, dry_run: bool
) -> dict[str, object]:
    """Upsert kpi_definitions rows for one ticker. Returns counts + name list."""
    specs = collect_kpis_from_holdings(repo_root, ticker)
    if not specs:
        return {"ticker": ticker, "inserted": 0, "skipped_existing": 0, "names": []}

    inserted = 0
    skipped = 0
    inserted_names: list[str] = []
    for name, unit, src, tier in specs:
        if dry_run:
            cur = conn.execute(
                "SELECT 1 FROM kpi_definitions WHERE ticker = ? AND name = ?",
                (ticker, name),
            )
            if cur.fetchone() is None:
                inserted += 1
                inserted_names.append(name)
            else:
                skipped += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO kpi_definitions "
            "(ticker, name, unit, primary_source, fallback_source, ir_url, "
            " threshold_tier, threshold_low, threshold_high, notes) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL)",
            (ticker, name, unit.value, src.value, tier.value),
        )
        if cur.rowcount > 0:
            inserted += 1
            inserted_names.append(name)
        else:
            skipped += 1
    return {
        "ticker": ticker, "inserted": inserted,
        "skipped_existing": skipped,
        "names": inserted_names,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Seed one ticker (uppercase)")
    g.add_argument("--all", action="store_true", help="Seed every ticker with a holdings JSON")
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT,
        help="Repo root containing micro_thesis/holdings",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print planned upserts without writing to the DB",
    )
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()

    if args.all:
        holdings_dir = repo_root / "micro_thesis" / "holdings"
        tickers = sorted(p.stem.upper() for p in holdings_dir.glob("*.json"))
    else:
        tickers = [args.ticker.upper()]

    conn = open_db(args.db)
    try:
        results = [
            seed_for_ticker(conn, repo_root, t, dry_run=args.dry_run)
            for t in tickers
        ]
        if not args.dry_run:
            conn.commit()
        total_inserted = sum(int(r["inserted"]) for r in results)
        total_skipped = sum(int(r["skipped_existing"]) for r in results)
        print(json.dumps({
            "dry_run": args.dry_run,
            "tickers": [r["ticker"] for r in results],
            "inserted": total_inserted,
            "skipped_existing": total_skipped,
            "per_ticker": results,
        }, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
