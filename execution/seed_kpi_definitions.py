"""Seed one-row-per-metric kpi_definitions from each ticker's holdings JSON.

Reads `micro_thesis/holdings/<TICKER>.json` and upserts a kpi_definitions row
for every entry in `tier_1_kpis` / `tier_2_kpis` / `tier_3_kpis`. Idempotent:
new (ticker, name) pairs are inserted; an already-correct row is left untouched.

Replaces the prior hardcoded-snapshot design — the analyst edits tier_*_kpis
via the comment processor's `edit_structured` route, and that drift makes any
hardcoded list stale immediately. Reading the holdings JSON dynamically keeps
the kpi_definitions table in sync with the analyst's current monitorables.

Unit resolution prefers the metric's RECORDED kpi_facts unit (authoritative)
over a name guess. For a brand-new definition no facts exist yet (they FK to
it), so the unit falls back to `infer_unit` — a name heuristic of last resort.
When facts DO exist and disagree in dimensional FAMILY with the stored unit
(a dollar LEVEL stamped a rate, e.g. AWS RPO "$364B" tagged `percent`), the row
is HEALED to the fact unit. The name is never reliable enough to be canonical:
"AWS Remaining Performance Obligations (RPO)" carries no `$`, and a YoY-growth
break-rule phrases the metric's CHANGE as a percent, not its level. Primary
source defaults to IR_DOC since the bulk of bank/SaaS-specific KPIs aren't
FMP-derivable.

Usage:
    python execution/seed_kpi_definitions.py --ticker NU
    python execution/seed_kpi_definitions.py --all
    python execution/seed_kpi_definitions.py --ticker NU --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import SourceType  # noqa: E402
from models.facts import Unit  # noqa: E402
from models.kpis import ThesisTier  # noqa: E402
from models.unit_convert import same_family  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

_TIER_MAP: dict[str, ThesisTier] = {
    "tier_1_kpis": ThesisTier.TIER_1_BREAK,
    "tier_2_kpis": ThesisTier.TIER_2_MONITOR,
    # tier_3_kpis is informational — treat as tier-2 for kpi_definitions seeding
    # since the ThesisTier enum only has BREAK + MONITOR levels.
    "tier_3_kpis": ThesisTier.TIER_2_MONITOR,
}


# Name fragments that mark a metric as a RATE/PROPORTION (stored "percent")
# even when the name also carries a currency annotation. Checked BEFORE the
# dollar-token branch so a parenthetical "(USD)" on a growth/margin metric
# ("Revenue YoY Growth (USD)") can't flip it to ACTUAL.
_RATE_NAME_TOKENS: tuple[str, ...] = (
    "growth",
    "yoy",
    "y/y",
    "deceleration",
    "decel",
    "retention",
    "churn",
    "margin",
    "yield",
    "penetration",
    "take rate",
    "attach rate",
    "%",
)
# Absolute monetary LEVELS whose names rarely carry a "$"/"USD" token but are
# always dollar magnitudes. Without these the PERCENT default mis-tags them as a
# rate — the AWS RPO bug, where a ~$364B backlog was stamped "percent" because
# the name has no currency token and the break-rule phrases RPO *growth* as a %.
_DOLLAR_LEVEL_NAME_TOKENS: tuple[str, ...] = (
    "remaining performance obligation",
    "rpo",
    "backlog",
    "bookings",
    "deferred revenue",
    "gross merchandise",
    "gmv",
    "payment volume",
    "tpv",
    "assets under management",
    "aum",
    "deposits",
    "loans outstanding",
)


def infer_unit(name: str) -> Unit:
    """Best-effort unit from the KPI name — the NO-FACTS bootstrap only.

    A heuristic of last resort: when a definition is first created no kpi_facts
    exist yet (they FK to it), so the name is all there is. ``seed_for_ticker``
    defers to the recorded fact unit as soon as one lands. Resolve the unit of
    the metric's REPORTED VALUE, never the unit its break-rule phrases a *change*
    in — a YoY-growth threshold does not make the metric itself a percentage.

    Order matters: a RATE word (growth/margin/%) wins over a currency token so
    "Revenue YoY Growth (USD)" is PERCENT not ACTUAL; an explicit dollar LEVEL
    (RPO/backlog/deposits) wins over the PERCENT default so a $-backlog isn't
    mis-tagged a rate. PERCENT stays the default — margins/growth/NIM/ROE/NPL
    dominate the analyst's monitorables and seldom carry a distinguishing token.
    """
    n = name.lower()
    # RATE / proportion — checked first so a currency annotation can't flip it.
    if any(tok in n for tok in _RATE_NAME_TOKENS):
        return Unit.PERCENT
    # COUNT for headcount / customer count / number-of-X
    if any(
        tok in n
        for tok in (
            "customers",
            "customer count",
            "millions",
            "headcount",
            "subscribers",
            "users",
            "accounts",
            "members",
            "merchants",
        )
    ):
        return Unit.COUNT
    # ACTUAL for dollar-denominated absolute values: explicit currency tokens
    # (ARPAC, cost-to-serve, $X revenue) plus dollar LEVELS that omit a "$".
    if any(
        tok in n
        for tok in ("usd", "$", "dollar", "arpac", "arpu", "cost-to-serve", "cost to serve")
    ) or any(tok in n for tok in _DOLLAR_LEVEL_NAME_TOKENS):
        return Unit.ACTUAL
    # BPS for explicit basis-point measures (rare but possible)
    if "bps" in n or "basis point" in n:
        return Unit.BPS
    # RATIO when explicitly "ratio" without a % suffix or "rate" word
    if "ratio" in n and "%" not in n and "rate" not in n:
        return Unit.RATIO
    # PERCENT is the most common — margins, growth rates, NPL%, NIM%, ROE%, penetration%
    return Unit.PERCENT


def _dominant_fact_unit(conn: sqlite3.Connection, ticker: str, name: str) -> Unit | None:
    """The recorded unit of (ticker, name)'s kpi_facts, or None when none exist.

    The fact unit is the unit a value was actually stored in — a far more
    reliable signal than the metric's name, and the one the renderer trusts.
    Returns the most-common unit (ties broken by the most recent period). A
    legacy value outside the :class:`Unit` enum is treated as absent.
    """
    row = conn.execute(
        "SELECT f.unit FROM kpi_facts f "
        "JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
        "WHERE d.ticker = ? AND d.name = ? AND f.unit IS NOT NULL "
        "GROUP BY f.unit ORDER BY COUNT(*) DESC, MAX(f.period_end) DESC LIMIT 1",
        (ticker, name),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return Unit(str(row[0]))
    except ValueError:
        return None


def collect_kpis_from_holdings(
    repo_root: Path, ticker: str
) -> list[tuple[str, Unit, SourceType, ThesisTier]]:
    """Read tier_*_kpis from the holdings JSON; return [(name, unit, source, tier), ...]."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return []
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return []

    specs: list[tuple[str, Unit, SourceType, ThesisTier]] = []
    seen_names: set[str] = set()
    for tier_key, tier_enum in _TIER_MAP.items():
        kpis = payload.get(tier_key)
        if not isinstance(kpis, list):
            continue
        for k in cast("list[object]", kpis):
            if not isinstance(k, dict):
                continue
            name = str(cast("dict[str, object]", k).get("name", "")).strip()
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


def _new_str_list() -> list[str]:
    """Typed factory for the dataclass list fields (keeps pyright-strict happy:
    a bare ``default_factory=list`` infers ``list[Unknown]``)."""
    return []


@dataclass(slots=True)
class SeedSummary:
    """Per-ticker tally from :func:`seed_for_ticker`.

    ``inserted`` rows are brand-new definitions; ``healed`` rows had a stored
    unit in the wrong dimensional family corrected to the recorded fact unit
    (the AWS RPO case); everything already consistent is ``skipped_existing``.
    """

    ticker: str
    inserted: int = 0
    skipped_existing: int = 0
    healed: int = 0
    names: list[str] = field(default_factory=_new_str_list)
    healed_names: list[str] = field(default_factory=_new_str_list)


def seed_for_ticker(
    conn: sqlite3.Connection, repo_root: Path, ticker: str, *, dry_run: bool
) -> SeedSummary:
    """Upsert kpi_definitions rows for one ticker. Returns a per-ticker tally.

    For each monitored KPI: insert a new row (unit from the name heuristic, the
    only signal before facts exist), leave an already-consistent row untouched,
    or HEAL a row whose stored unit is in a different dimensional family than its
    recorded kpi_facts — deferring to the fact unit, which is authoritative for
    what the value actually is. This is what keeps a re-seed from re-stamping a
    dollar LEVEL (AWS RPO) as a rate.
    """
    specs = collect_kpis_from_holdings(repo_root, ticker)
    if not specs:
        return SeedSummary(ticker=ticker)

    inserted = 0
    skipped = 0
    healed = 0
    inserted_names: list[str] = []
    healed_names: list[str] = []
    for name, heuristic_unit, src, tier in specs:
        fact_unit = _dominant_fact_unit(conn, ticker, name)
        existing = conn.execute(
            "SELECT id, unit FROM kpi_definitions WHERE ticker = ? AND name = ?",
            (ticker, name),
        ).fetchone()

        if existing is None:
            # Brand-new definition: no facts FK to it yet, so the name heuristic
            # is the only signal (fact_unit is virtually always None here).
            write_unit = fact_unit or heuristic_unit
            inserted += 1
            inserted_names.append(name)
            if not dry_run:
                conn.execute(
                    "INSERT INTO kpi_definitions "
                    "(ticker, name, unit, primary_source, fallback_source, ir_url, "
                    " threshold_tier, threshold_low, threshold_high, notes) "
                    "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL)",
                    (ticker, name, write_unit.value, src.value, tier.value),
                )
            continue

        # Existing row: heal only a cross-family unit drift, deferring to the
        # authoritative fact unit. A matching / same-family / fact-less row is
        # left as the analyst (or first writer) set it.
        try:
            existing_unit = Unit(str(existing[1])) if existing[1] is not None else None
        except ValueError:
            existing_unit = None
        if (
            fact_unit is not None
            and existing_unit is not None
            and not same_family(existing_unit, fact_unit)
        ):
            healed += 1
            healed_names.append(name)
            if not dry_run:
                conn.execute(
                    "UPDATE kpi_definitions SET unit = ? WHERE id = ?",
                    (fact_unit.value, int(existing[0])),
                )
        else:
            skipped += 1
    return SeedSummary(
        ticker=ticker,
        inserted=inserted,
        skipped_existing=skipped,
        healed=healed,
        names=inserted_names,
        healed_names=healed_names,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Seed one ticker (uppercase)")
    g.add_argument("--all", action="store_true", help="Seed every ticker with a holdings JSON")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing micro_thesis/holdings",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
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
        results = [seed_for_ticker(conn, repo_root, t, dry_run=args.dry_run) for t in tickers]
        if not args.dry_run:
            conn.commit()
        total_inserted = sum(r.inserted for r in results)
        total_skipped = sum(r.skipped_existing for r in results)
        total_healed = sum(r.healed for r in results)
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "tickers": [r.ticker for r in results],
                    "inserted": total_inserted,
                    "skipped_existing": total_skipped,
                    "healed": total_healed,
                    "per_ticker": [asdict(r) for r in results],
                },
                indent=2,
            )
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
