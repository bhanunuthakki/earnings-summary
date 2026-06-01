"""Fetch FMP ETF profile + holdings for a single ETF ticker.

Two ingest modes:
    --live (default) — HTTP call to FMP /stable/etf/info and
                       /stable/etf/holdings, writes responses to
                       data/historical/fmp/<TICKER>_etf_{info,holdings}.json,
                       upserts to etf_profile / etf_holdings.

    --from-cache    — skip HTTP, read the cached JSON files in
                       data/historical/fmp/ written by a prior --live run
                       (or seeded manually). Same DB writes.

Usage:
    python execution/fetch_etf_data.py --ticker SOXX
    python execution/fetch_etf_data.py --ticker SOXX --from-cache
    python execution/fetch_etf_data.py --ticker SOXX --as-of 2026-05-25

The parsers (parse_etf_info, parse_etf_holdings) are exposed for direct use
by tests and other ingest paths. Each takes a raw JSON payload and returns
typed Pydantic models — no I/O, no DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instrument_store import upsert_etf_holdings, upsert_etf_profile  # noqa: E402
from models.instruments import EtfHolding, EtfProfile  # noqa: E402

FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
FMP_BASE = "https://financialmodelingprep.com"

# Tier gate: on free FMP the /api/v3 fallback 403s (v3 deprecated 2025-08-31),
# so skip it and hit /stable only when FMP_TIER=free. Mirrors the gate in
# execution/save_fmp_data.py; a deliberate flip, not auto-detected.
_STABLE_ONLY = os.environ.get("FMP_TIER", "").strip().lower() == "free"


# ---------------------------------------------------------------------------
# Parsers — pure, no I/O
# ---------------------------------------------------------------------------


def parse_etf_info(payload: object, ticker: str, fetched_at: datetime) -> EtfProfile:
    """Parse FMP /stable/etf/info response into EtfProfile.

    FMP returns a JSON array with one object. Tolerates a single-object payload
    (some endpoint variants return that directly). All fields are optional.
    """
    record = _first_record(payload)
    record = cast("dict[str, object]", record or {})

    return EtfProfile(
        ticker=ticker.upper(),
        name=_str(record.get("name")),
        issuer=_str(record.get("etfCompany")) or _str(record.get("issuer")),
        expense_ratio=_pct_to_decimal(record.get("expenseRatio")),
        aum_usd_m=_aum_to_millions(record.get("aum")) or _aum_to_millions(record.get("assetsUnderManagement")),
        inception_date=_iso_date(record.get("inceptionDate")),
        asset_class=_str(record.get("assetClass")),
        benchmark_index=_str(record.get("etfIndex")) or _str(record.get("benchmark")),
        domicile=_str(record.get("domicile")),
        listed_exchange=_str(record.get("exchange")),
        distribution_yield=_pct_to_decimal(record.get("yield")) or _pct_to_decimal(record.get("dividendYield")),
        description=_str(record.get("description")),
        sector_label=_str(record.get("sectorsList")) or _str(record.get("sector")),
        nav=_float(record.get("nav")),
        price=_float(record.get("price")),
        premium_discount_pct=_pct_to_decimal(record.get("navPremiumDiscount")),
        source="fmp",
        profile_fetched_at=fetched_at,
    )


def parse_etf_holdings(
    payload: object, ticker: str, as_of_date: date, fetched_at: datetime
) -> list[EtfHolding]:
    """Parse FMP /stable/etf/holdings response into a list of EtfHolding rows.

    Expected shape: JSON array of objects with `asset` (ticker), `name`,
    `weightPercentage`, `sharesNumber`, `marketValue`. FMP returns weight as a
    percentage (e.g. 7.42), not a decimal — we normalize to decimal here.
    Items lacking an asset key are kept with constituent_ticker=None (cash
    sleeves, futures, currency hedges).
    """
    items: list[object] = cast("list[object]", payload) if isinstance(payload, list) else []
    out: list[EtfHolding] = []
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            continue
        record = cast("dict[str, object]", raw)
        constituent = _str(record.get("asset")) or _str(record.get("symbol"))
        weight_pct_raw = record.get("weightPercentage") or record.get("weight")
        out.append(
            EtfHolding(
                ticker=ticker.upper(),
                as_of_date=as_of_date,
                constituent_ticker=constituent.upper() if constituent else None,
                name=_str(record.get("name")),
                weight_pct=_pct_to_decimal(weight_pct_raw),
                shares_held=_float(record.get("sharesNumber")) or _float(record.get("shares")),
                market_value_usd=_float(record.get("marketValue")),
                sector=_str(record.get("sector")),
                asset_class=_str(record.get("assetClass")),
                rank_position=idx,
                source="fmp",
                fetched_at=fetched_at,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Ingest orchestrators
# ---------------------------------------------------------------------------


def ingest_from_payloads(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    info_payload: object,
    holdings_payload: object,
    as_of_date: date | None = None,
    fetched_at: datetime | None = None,
) -> tuple[EtfProfile, int]:
    """Persist parsed payloads to the DB. Returns (profile, num_holdings)."""
    ts = fetched_at or datetime.now()
    resolved_date = as_of_date or ts.date()
    profile = parse_etf_info(info_payload, ticker, ts)
    holdings = parse_etf_holdings(holdings_payload, ticker, resolved_date, ts)
    upsert_etf_profile(conn, profile)
    upsert_etf_holdings(conn, ticker, resolved_date, holdings)
    return profile, len(holdings)


def ingest_from_cache(
    conn: sqlite3.Connection,
    ticker: str,
    fmp_dir: Path = FMP_DIR,
    *,
    as_of_date: date | None = None,
) -> tuple[EtfProfile, int]:
    """Read cached JSON files from `fmp_dir` and ingest.

    Expects files named `<TICKER>_etf_info.json` and `<TICKER>_etf_holdings.json`.
    """
    info_path = fmp_dir / f"{ticker.upper()}_etf_info.json"
    holdings_path = fmp_dir / f"{ticker.upper()}_etf_holdings.json"
    if not info_path.exists():
        raise FileNotFoundError(f"Missing ETF info cache: {info_path}")
    if not holdings_path.exists():
        raise FileNotFoundError(f"Missing ETF holdings cache: {holdings_path}")
    with open(info_path, encoding="utf-8") as f:
        info_payload = json.load(f)
    with open(holdings_path, encoding="utf-8") as f:
        holdings_payload = json.load(f)
    file_mtime = datetime.fromtimestamp(holdings_path.stat().st_mtime)
    return ingest_from_payloads(
        conn,
        ticker,
        info_payload=info_payload,
        holdings_payload=holdings_payload,
        as_of_date=as_of_date,
        fetched_at=file_mtime,
    )


def ingest_live(
    conn: sqlite3.Connection,
    ticker: str,
    fmp_dir: Path = FMP_DIR,
    *,
    as_of_date: date | None = None,
) -> tuple[EtfProfile, int]:
    """Hit FMP, write JSON to `fmp_dir`, ingest. Requires FMP_API_KEY in env."""
    import requests  # local import keeps test imports clean

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FMP_API_KEY not set. Use --from-cache mode with pre-seeded JSON, "
            "or set FMP_API_KEY in .env."
        )
    session = requests.Session()
    session.headers["User-Agent"] = "earnings-summary/1.0"

    info_payload = _fmp_get(session, api_key, ticker, "etf/info")
    holdings_payload = _fmp_get(session, api_key, ticker, "etf/holdings")

    fmp_dir.mkdir(parents=True, exist_ok=True)
    with open(fmp_dir / f"{ticker.upper()}_etf_info.json", "w", encoding="utf-8") as f:
        json.dump(info_payload, f, indent=2)
    with open(fmp_dir / f"{ticker.upper()}_etf_holdings.json", "w", encoding="utf-8") as f:
        json.dump(holdings_payload, f, indent=2)

    return ingest_from_payloads(
        conn,
        ticker,
        info_payload=info_payload,
        holdings_payload=holdings_payload,
        as_of_date=as_of_date,
        fetched_at=datetime.now(),
    )


def _fmp_get(
    session: "requests.Session", api_key: str, ticker: str, path: str
) -> object:
    """Try /stable (then /api/v3 unless stable-only); return first 200 JSON body."""
    import requests

    last_err: str | None = None
    bases = [f"{FMP_BASE}/stable/{path}/{ticker}"]
    if not _STABLE_ONLY:
        bases.append(f"{FMP_BASE}/api/v3/{path.replace('/', '-')}/{ticker}")
    for base in bases:
        try:
            r = session.get(base, params={"apikey": api_key}, timeout=(10, 60))
        except requests.RequestException as e:
            last_err = f"{base}: {e}"
            continue
        if r.status_code == 200:
            return cast("object", r.json())
        last_err = f"{base}: HTTP {r.status_code}"
    raise RuntimeError(f"FMP fetch failed for {ticker} {path}: {last_err}")


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _iso_date(v: object) -> date | None:
    s = _str(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _pct_to_decimal(v: object) -> float | None:
    """Coerce '7.42', 7.42, or '7.42%' to 0.0742.

    FMP returns weights, expense ratios, yields, and premium/discount in
    *percent units* (e.g. `weightPercentage: 9.42` means 9.42%, not 9.42).
    We always divide by 100 to produce a canonical decimal in [0, 1ish].

    Trailing '%' is tolerated for the string-shaped inputs FMP occasionally
    returns for descriptive fields.
    """
    f = _float(v) if not (isinstance(v, str) and v.endswith("%")) else _float(v[:-1].strip())
    if f is None:
        return None
    return f / 100.0


def _aum_to_millions(v: object) -> float | None:
    """Coerce FMP AUM (returned as raw USD or already in millions) to millions.

    FMP's /stable/etf/info returns AUM in raw dollars; some legacy endpoints
    return millions. Anything >= 1e6 is assumed raw dollars and divided by 1e6;
    smaller numbers are assumed already-millions.
    """
    f = _float(v)
    if f is None:
        return None
    return f / 1_000_000.0 if f >= 1_000_000 else f


def _first_record(payload: object) -> object:
    """FMP often returns [{...}] for single-symbol endpoints; unwrap to {...}."""
    if isinstance(payload, list):
        seq = cast("list[object]", payload)
        return seq[0] if seq else None
    if isinstance(payload, dict):
        return cast("dict[str, object]", payload)
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True)
    ap.add_argument(
        "--from-cache",
        action="store_true",
        help="Read pre-cached JSON from data/historical/fmp/ instead of hitting FMP",
    )
    ap.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="as_of_date for holdings rows (default: today)",
    )
    args = ap.parse_args()

    # Connect via the canonical helper so module-level init runs first.
    import db as portfolio_db

    conn = portfolio_db.get_connection()
    try:
        if args.from_cache:
            profile, n = ingest_from_cache(conn, args.ticker, as_of_date=args.as_of)
        else:
            profile, n = ingest_live(conn, args.ticker, as_of_date=args.as_of)
        conn.commit()
    finally:
        conn.close()

    print(
        f"[ok] {args.ticker.upper()} — profile fetched ({profile.issuer or '?'}), "
        f"{n} holdings rows",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
