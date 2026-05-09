"""Bootstrap a deep-dive on a watchlist ticker.

Drops two artifacts that follow the user's pre-trade discipline:
  - micro_thesis/holdings/<TICKER>.json — starter schema with auto-filled
    structural fields (sector, industry, peers, segment names) from cached
    FMP data + TBV qualitative fields the user fills in by hand.
  - micro_thesis/diligence/<TICKER>-checklist.md — markdown worksheet keyed
    to the pre-trade checklist (one-page thesis, killer variables, invalidation
    triggers, sizing, time horizon).

Usage:
    python execution/start_diligence.py --ticker MSFT
    python execution/start_diligence.py --next 2          # pick top-N watchlist
                                                          # tickers without holdings
    python execution/start_diligence.py --ticker MSFT --force  # overwrite

The picker for --next sorts by: (a) FMP-data completeness (more endpoints OK
= readier for analysis), (b) ticker A->Z. The user can then deep-dive these
1-2 per month and write the longform thesis paragraph by hand.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
DILIGENCE_DIR = PROJECT_ROOT / "micro_thesis" / "diligence"


# ---------------------------------------------------------------------------
# FMP cache readers (best-effort — None on missing/corrupt files)
# ---------------------------------------------------------------------------


def _read_fmp_json(ticker: str, suffix: str) -> object | None:
    path = FMP_DIR / f"{ticker}_{suffix}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _profile_fields(ticker: str) -> dict[str, object]:
    """Pull sector, industry, description, market cap, employee count from profile."""
    body = _read_fmp_json(ticker, "profile")
    if not body:
        return {}
    rec = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else {})
    return {
        "sector": rec.get("sector"),
        "industry": rec.get("industry"),
        "country": rec.get("country"),
        "company_name": rec.get("companyName") or rec.get("name"),
        "description": (rec.get("description") or "")[:600] or None,
        "ceo": rec.get("ceo"),
        "exchange": rec.get("exchange") or rec.get("exchangeShortName"),
    }


def _peers(ticker: str) -> list[str]:
    body = _read_fmp_json(ticker, "peers")
    if not body:
        return []
    if isinstance(body, list) and body and isinstance(body[0], dict):
        peers = body[0].get("peersList") or body[0].get("peers") or []
        return list(peers)[:10]
    if isinstance(body, list):
        return [str(p) for p in body][:10]
    return []


def _segment_names(ticker: str) -> list[str]:
    """Most-recent annual product segments — used to seed Tier-2 KPI hints."""
    body = _read_fmp_json(ticker, "product_segments_annual")
    if not isinstance(body, list) or not body:
        return []
    # FMP segment-product format: list of {date: {segmentName: revenue, ...}}
    latest = body[0]
    if isinstance(latest, dict):
        # Each top-level key is a date; grab segment dict from most recent date
        for _, segs in sorted(latest.items(), reverse=True):
            if isinstance(segs, dict):
                return [name for name in segs.keys() if name][:8]
    return []


def _endpoint_completeness(conn: sqlite3.Connection, ticker: str) -> tuple[int, int]:
    """Return (ok_count, total_status_rows) for ticker."""
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), COUNT(*) "
        "FROM fmp_endpoint_status WHERE ticker = ?",
        (ticker,),
    )
    row = cur.fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_TBV = "TBV — fill in during deep-dive"


def build_holdings_schema(ticker: str, name: str) -> dict[str, object]:
    """Starter holdings JSON. Structural fields from FMP; qualitative TBV."""
    profile = _profile_fields(ticker)
    peers = _peers(ticker)
    segments = _segment_names(ticker)

    tier_2_seeds: list[dict[str, str]] = []
    for seg in segments:
        tier_2_seeds.append({
            "name": f"{seg} segment revenue + margin",
            "source": "earnings release segment",
        })

    return {
        "ticker": ticker,
        "name": profile.get("company_name") or name,
        "last_updated": date.today().isoformat(),
        "diligence_status": "draft",
        "thesis": _TBV,
        "verdict": "Pending",
        "verdict_color": "gray",
        "key_driver": _TBV,
        "structural": {
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "country": profile.get("country"),
            "exchange": profile.get("exchange"),
            "ceo": profile.get("ceo"),
            "description_excerpt": profile.get("description"),
        },
        "killer_variables": [_TBV, _TBV],
        "invalidation_triggers": [
            {
                "trigger": _TBV,
                "observable_in": "next earnings",
                "consecutive_periods": 1,
            },
        ],
        "sizing": {
            "tier": _TBV,
            "rationale": _TBV,
            "max_position_pct": None,
        },
        "time_horizon": {
            "minimum_quarters": _TBV,
            "rationale": _TBV,
        },
        "tier_1_kpis": [
            {
                "name": "Revenue YoY Growth (USD)",
                "current": _TBV,
                "prior": None,
                "yoy": None,
                "status": "Unknown",
                "break_condition": _TBV,
                "source": "earnings release",
            },
            {
                "name": "Operating Margin (GAAP)",
                "current": _TBV,
                "prior": None,
                "yoy": None,
                "status": "Unknown",
                "break_condition": _TBV,
                "source": "earnings release",
            },
        ],
        "tier_2_kpis": tier_2_seeds or [{"name": _TBV, "source": "transcript"}],
        "tier_3_kpis": [],
        "competitive_watchlist": peers,
        "thesis_breakers_qualitative": [_TBV],
        "flags": [],
        "break_rules": [
            {
                "rule_id": "universal_revenue_decline",
                "kpi_name": "Revenue YoY Growth (USD)",
                "comparator": "lt",
                "threshold": 0,
                "unit": "percent",
                "consecutive_periods": 2,
                "narrative": "Revenue declining YoY for 2 consecutive quarters — outright top-line contraction.",
            },
            {
                "rule_id": "universal_operating_loss",
                "kpi_name": "Operating Margin (GAAP)",
                "comparator": "lt",
                "threshold": 0,
                "unit": "percent",
                "consecutive_periods": 2,
                "narrative": "GAAP operating margin negative for 2 consecutive quarters.",
            },
        ],
    }


def build_checklist_markdown(ticker: str, name: str, schema: dict[str, object]) -> str:
    structural = schema.get("structural", {})
    sector = structural.get("sector") or "—"
    industry = structural.get("industry") or "—"
    description = structural.get("description_excerpt") or "—"
    peers = schema.get("competitive_watchlist", [])
    segments = [
        kpi.get("name", "") for kpi in schema.get("tier_2_kpis", [])
        if kpi.get("name") and kpi.get("name") != _TBV
    ]

    peer_block = ", ".join(peers[:8]) if peers else "—"
    segment_block = "\n".join(f"- {s}" for s in segments) if segments else "- (no segment disclosure detected in FMP cache)"

    return f"""# {ticker} — Pre-trade Diligence Checklist

**Company:** {name}
**Sector / Industry:** {sector} / {industry}
**Auto-generated:** {date.today().isoformat()}

> Fill this in BEFORE writing the holdings/{ticker}.json thesis. The discipline:
> if you can't put a coherent paragraph here, the position doesn't earn capital.

---

## 0. Auto-filled context (from FMP cache)

**Description excerpt:** {description}

**FMP-disclosed peer set:** {peer_block}

**Reportable segments:**
{segment_block}

---

## 1. One-page thesis (≤200 words)

> What does the company do? Why is the market mispricing it? What specifically
> catalyzes the re-rating? When?

[ Write the paragraph here. If you can't, the position is not yet researched. ]

---

## 2. Primary sources read

- [ ] Latest 10-K (or 20-F / 40-F)
- [ ] Last 3-5 earnings call transcripts
- [ ] Two most recent competitor 10-Ks: ____________
- [ ] Sell-side research (skim, discount heavily)

---

## 3. Simple model

- [ ] Rough DCF range or comparable-company multiple range
- [ ] Fair-value low end: $______
- [ ] Fair-value mid: $______
- [ ] Fair-value high end: $______
- [ ] Current price: $______
- [ ] Margin of safety vs. low end: ______%

---

## 4. Killer variables (2-3 — the things that ACTUALLY drive the outcome)

1. **____________** — why it matters / how I'll observe it: ____________
2. **____________** — why it matters / how I'll observe it: ____________
3. (optional) **____________** — why / how: ____________

---

## 5. Invalidation triggers (specific fundamental events, NOT price)

- [ ] Trigger 1: e.g. "if Q3 revenue growth falls below X% for 2 consecutive quarters"
- [ ] Trigger 2: e.g. "if competitor [Y] launches [Z]"
- [ ] Trigger 3: e.g. "if regulator [A] does [B]"

> A 20% price decline is NOT an invalidation trigger.

---

## 6. Sizing by conviction

- [ ] **High conviction (up to ~8-10%)** — clean thesis, low ambiguity, observable catalysts
- [ ] **Standard (3-5%)** — thesis intact, some ambiguity
- [ ] **Speculative (1-2%)** — asymmetric option, early stage, or broken-thesis-with-optionality

**Selected tier:** ____________
**Sum of conviction-tier % allocations across positions:** ______% (can exceed 100% with leverage; don't over-concentrate)

---

## 7. Time horizon

- **Pre-commit holding period:** ______ quarters
- **Re-evaluation cadence:** every earnings + on any invalidation-trigger fire
- **What would shorten this:** only the named invalidation triggers above

---

## 8. Sell discipline (preview)

I will sell ONLY if:
- [ ] Thesis fully realized — target valuation hit / planned re-rating happened
- [ ] Thesis broken — specific named invalidation trigger fires
- [ ] Better opportunity available — explicit IRR comparison, not "this looks cheaper"

I will NOT sell on:
- Bad week / bad month
- Boredom
- Hot tip on something else
- Tax-loss harvesting at the cost of the thesis

---

## 9. After the buy — monitoring journal

- [ ] Read every quarterly earnings release + transcript
- [ ] Update thesis status: still valid? evolved? broken?
- [ ] Don't check daily price unless an invalidation trigger has fired

---

## Final go / no-go

- [ ] **GO** — thesis is coherent, killer variables identified, triggers defined, sizing tier selected, horizon committed.
  → run `python execution/update_thesis_tracker.py --ticker {ticker}` after each earnings release.
- [ ] **PASS** — too much ambiguity to write a coherent thesis. Revisit in N quarters.
"""


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------


def _pick_next_watchlist(conn: sqlite3.Connection, n: int) -> list[tuple[str, str]]:
    """Return up to N watchlist tickers without an existing holdings JSON.

    Sort key: most-complete FMP data first (proxy for readiness), then ticker.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, name FROM tracked_companies "
        "WHERE list_type = 'watchlist' AND archived_at IS NULL"
    )
    candidates: list[tuple[int, str, str]] = []
    for ticker, name in cur.fetchall():
        if (HOLDINGS_DIR / f"{ticker}.json").exists():
            continue
        ok_count, _ = _endpoint_completeness(conn, ticker)
        candidates.append((ok_count, ticker, name))
    # Most-complete first, then alphabetical
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return [(t, n) for _, t, n in candidates[:n]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def bootstrap_one(ticker: str, name: str, *, force: bool) -> dict[str, str]:
    """Drop holdings JSON + checklist markdown for one ticker."""
    HOLDINGS_DIR.mkdir(parents=True, exist_ok=True)
    DILIGENCE_DIR.mkdir(parents=True, exist_ok=True)

    holdings_path = HOLDINGS_DIR / f"{ticker}.json"
    checklist_path = DILIGENCE_DIR / f"{ticker}-checklist.md"

    if holdings_path.exists() and not force:
        return {"ticker": ticker, "status": "skipped_existing", "holdings": str(holdings_path)}

    schema = build_holdings_schema(ticker, name)
    checklist = build_checklist_markdown(ticker, name, schema)

    holdings_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    checklist_path.write_text(checklist, encoding="utf-8")
    return {
        "ticker": ticker,
        "status": "created",
        "holdings": str(holdings_path.relative_to(PROJECT_ROOT)),
        "checklist": str(checklist_path.relative_to(PROJECT_ROOT)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Bootstrap one ticker by symbol")
    g.add_argument("--next", type=int, metavar="N",
                   help="Pick top-N watchlist tickers without a holdings JSON")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing holdings JSON / checklist")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        if args.ticker:
            cur = conn.cursor()
            cur.execute("SELECT ticker, name FROM tracked_companies WHERE ticker = ?",
                        (args.ticker.upper(),))
            row = cur.fetchone()
            if row is None:
                print(json.dumps({"error": f"ticker {args.ticker} not in tracked_companies"}))
                return 2
            results = [bootstrap_one(row[0], row[1], force=args.force)]
        else:
            picks = _pick_next_watchlist(conn, args.next)
            if not picks:
                print(json.dumps({"info": "no eligible watchlist tickers"}))
                return 0
            results = [bootstrap_one(t, n, force=args.force) for t, n in picks]
    finally:
        conn.close()

    print(json.dumps({"results": results, "count": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
