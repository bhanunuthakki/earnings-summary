"""Fetch FMP ETF profile + holdings for a single ETF ticker.

Ingest modes:
    ingest_live  — HTTP call to FMP /stable/etf/info and /stable/etf/holdings
                   via the shared client with capture enabled, then registers
                   each exact response body as an immutable FMP snapshot and
                   binds the resulting captures to the ETF document within a
                   caller-owned transaction. The live path never publishes a
                   mutable cache file or a new manifest: its only durable
                   output is the immutable snapshot plus the DB rows it
                   writes (left uncommitted for the caller to commit).

    --from-cache — skip HTTP, read the saved JSON files in
                   data/historical/fmp/ (written by a prior --live run under
                   the previous contract, or seeded manually). These saved
                   files are unverified cache: field_evidence is always empty
                   for cache ingests. Same DB writes.

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
import math
import os
import re
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_paths import require_db_path
from etf_sources.profile_evidence import (
    bind_fmp_profile_fields,
    capture_profile_fields,
)
from etf_sources.profile_evidence import (
    fmp_float_value as _float,
)
from etf_sources.profile_evidence import (
    fmp_percent_decimal as _pct_to_decimal,
)
from etf_sources.profile_evidence import (
    fmp_text_value as _str,
)
from instrument_store import (
    upsert_etf_holdings,
    upsert_etf_profile,
)
from models.instruments import EtfHolding, EtfProfile
from net.client import (
    FMP_CLIENT,
    HttpCallError,
    HttpJsonResponse,
    HttpResponseCapture,
    JsonShape,
    JsonValue,
)
from operations.paths import configured_product_state_root
from pipeline.fmp_doc_index import bind_etf_capture, register_fmp_snapshot
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"

_FMP_ETF_INFO_PATH = "etf/info"
_FMP_ETF_HOLDINGS_PATH = "etf/holdings"
_FMP_ORIGIN = "https://financialmodelingprep.com"
_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,15}$")
_PROVIDER_ERROR_KEYS = ("error", "Error Message", "message")
_FUND_ID_KEYS = ("etfSymbol", "fundSymbol")


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

    profile = EtfProfile(
        ticker=ticker.upper(),
        name=_str(record.get("name")),
        issuer=_str(record.get("etfCompany")) or _str(record.get("issuer")),
        expense_ratio=_pct_to_decimal(record.get("expenseRatio")),
        aum_usd_m=_first_valid_float(
            _aum_to_millions(record.get("aum")),
            _aum_to_millions(record.get("assetsUnderManagement")),
        ),
        inception_date=_iso_date(record.get("inceptionDate")),
        asset_class=_str(record.get("assetClass")),
        benchmark_index=_str(record.get("etfIndex")) or _str(record.get("benchmark")),
        domicile=_str(record.get("domicile")),
        listed_exchange=_str(record.get("exchange")),
        distribution_yield=_first_valid_float(
            _pct_to_decimal(record.get("yield")),
            _pct_to_decimal(record.get("dividendYield")),
        ),
        description=_str(record.get("description")),
        sector_label=_str(record.get("sectorsList")) or _str(record.get("sector")),
        nav=_float(record.get("nav")),
        price=_float(record.get("price")),
        premium_discount_pct=_pct_to_decimal(record.get("navPremiumDiscount")),
        source="fmp",
        profile_fetched_at=fetched_at,
    )

    source_as_of = _iso_date(record.get("asOfDate"))
    if record.get("asOfDate") is not None and source_as_of is None:
        return profile  # Malformed source dating cannot be relabeled as undated evidence.
    return capture_profile_fields(profile, source_as_of=source_as_of)


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
        out.append(
            EtfHolding(
                ticker=ticker.upper(),
                as_of_date=as_of_date,
                constituent_ticker=constituent.upper() if constituent else None,
                name=_str(record.get("name")),
                weight_pct=_first_valid_float(
                    _pct_to_decimal(record.get("weightPercentage")),
                    _pct_to_decimal(record.get("weight")),
                ),
                shares_held=_first_valid_float(
                    _float(record.get("sharesNumber")),
                    _float(record.get("shares")),
                ),
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
    ts = fetched_at or datetime.now(UTC)
    resolved_date = as_of_date or ts.date()
    profile = parse_etf_info(info_payload, ticker, ts)
    profile = profile.model_copy(update={"field_evidence": {}})
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
    conn: sqlite3.Connection, ticker: str, state_root: Path, *, as_of_date: date | None = None
) -> tuple[EtfProfile, int]:
    if not state_root.is_absolute():
        raise RuntimeError(f"state_root must be absolute: {state_root!r}")
    normalized = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,15}", normalized):
        raise ValueError(f"invalid ticker: {ticker!r}")
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("FMP_API_KEY environment variable is not set")
    response_info = _fmp_get(api_key, normalized, "etf/info")
    response_holdings = _fmp_get(api_key, normalized, "etf/holdings")
    capture_info, payload_info = _validated_capture(response_info, normalized, "etf/info")
    capture_holdings, payload_holdings = _validated_capture(
        response_holdings, normalized, "etf/holdings"
    )
    profile_clock = capture_info.received_at
    profile = parse_etf_info(payload_info, normalized, profile_clock)
    holdings_clock = capture_holdings.received_at
    resolved_date = as_of_date if as_of_date is not None else capture_holdings.received_at.date()
    holdings = parse_etf_holdings(payload_holdings, normalized, resolved_date, holdings_clock)
    filename_info = f"{normalized}_etf_info.json"
    filename_holdings = f"{normalized}_etf_holdings.json"
    source_url_info = f"https://financialmodelingprep.com/stable/etf/info?symbol={normalized}"
    source_url_holdings = (
        f"https://financialmodelingprep.com/stable/etf/holdings?symbol={normalized}"
    )
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    savepoint_open = False
    try:
        conn.execute("SAVEPOINT etf_fmp_capture")
        savepoint_open = True
        document_info, _ = register_fmp_snapshot(
            conn,
            ticker=normalized,
            source_filename=filename_info,
            content=capture_info.body,
            project_root=state_root,
            captured_at=capture_info.received_at,
            period_end=None,
            source_url=source_url_info,
        )
        document_holdings, _ = register_fmp_snapshot(
            conn,
            ticker=normalized,
            source_filename=filename_holdings,
            content=capture_holdings.body,
            project_root=state_root,
            captured_at=capture_holdings.received_at,
            period_end=None,
            source_url=source_url_holdings,
        )
        receipt_info = bind_etf_capture(
            conn,
            document_id=document_info,
            ticker=normalized,
            source_filename=filename_info,
            capture=capture_info,
        )
        bind_etf_capture(
            conn,
            document_id=document_holdings,
            ticker=normalized,
            source_filename=filename_holdings,
            capture=capture_holdings,
        )
        profile = bind_fmp_profile_fields(profile, receipt_info, payload_info)
        upsert_etf_profile(conn, profile)
        upsert_etf_holdings(conn, normalized, resolved_date, holdings)
        conn.execute("RELEASE SAVEPOINT etf_fmp_capture")
        return profile, len(holdings)
    except BaseException:
        try:
            if savepoint_open:
                conn.execute("ROLLBACK TO SAVEPOINT etf_fmp_capture")
                conn.execute("RELEASE SAVEPOINT etf_fmp_capture")
        finally:
            if started:
                conn.rollback()
        raise


def _same_json(left: object, right: object) -> bool:
    """Compare JSON values without treating booleans or integers as floats."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        lhs = cast("dict[str, object]", left)
        rhs = cast("dict[str, object]", right)
        return lhs.keys() == rhs.keys() and all(
            _same_json(value, rhs[key]) for key, value in lhs.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        lhs_list = cast("list[object]", left)
        rhs_list = cast("list[object]", right)
        return len(lhs_list) == len(rhs_list) and all(
            _same_json(a, b) for a, b in zip(lhs_list, rhs_list, strict=True)
        )
    if isinstance(left, float) and not math.isfinite(left):
        return False
    return left is None or (isinstance(left, (str, int, float, bool)) and left == right)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ETF response contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("ETF response contains a nonstandard JSON constant")


def _validated_capture(
    response: HttpJsonResponse, ticker: str, path: str
) -> tuple[HttpResponseCapture, JsonValue]:
    capture = response.capture
    if not 200 <= response.status_code < 300 or capture is None:
        raise ValueError("ETF response has no successful HTTP capture")
    if capture.received_at.utcoffset() is None or capture.received_at > datetime.now(UTC):
        raise ValueError("ETF capture requires a non-future aware timestamp")
    try:
        UUID(capture.capture_id)
    except ValueError:
        raise ValueError("ETF capture identity is invalid") from None
    if capture.origin != "https://financialmodelingprep.com" or capture.path != f"/stable/{path}":
        raise ValueError("ETF capture final endpoint does not match the request")
    try:
        decoded: object = json.loads(
            capture.body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, UnicodeError):
        raise ValueError("ETF response is not strict JSON") from None
    if not _same_json(decoded, response.payload):
        raise ValueError("ETF capture does not match its parsed payload")
    if path == "etf/info":
        record: object = decoded
        if isinstance(decoded, list):
            rows = cast("list[object]", decoded)
            record = rows[0] if len(rows) == 1 else None
        if not isinstance(record, dict):
            raise ValueError("ETF info must contain exactly one object")
        info = cast("dict[str, object]", record)
        symbol = info.get("symbol")
        if (
            not isinstance(symbol, str)
            or symbol.strip().upper() != ticker
            or any(key in info for key in ("error", "Error Message", "message"))
        ):
            raise ValueError("ETF info identity is missing or does not match")
    else:
        if not isinstance(decoded, list):
            raise ValueError("ETF holdings must be an array of objects")
        for row in cast("list[object]", decoded):
            if not isinstance(row, dict):
                raise ValueError("ETF holdings must be an array of objects")
            holding = cast("dict[str, object]", row)
            for key in ("etfSymbol", "fundSymbol"):
                if key in holding:
                    fund = holding[key]
                    if not isinstance(fund, str) or fund.strip().upper() != ticker:
                        raise ValueError("ETF holdings fund identity does not match")
    # Equality proves that this decoded value has the shared JSON type grammar.
    return capture, response.payload


def _fmp_get(api_key: str, ticker: str, path: str) -> HttpJsonResponse:
    """Fetch a FMP endpoint via the shared client; return the complete response.

    The shared client strips query strings and redacts transport exceptions,
    so the runtime error raised here never carries the raw URL or API key.
    """
    try:
        response = FMP_CLIENT.get_json(
            path,
            params={"symbol": ticker},
            api_key=api_key,
            expected=JsonShape.ANY,
            timeout=(10, 60),
            retain_capture=True,
        )
    except HttpCallError as exc:
        raise RuntimeError(f"FMP fetch failed for {ticker} {path}: {exc}") from None
    return response


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _first_valid_float(*values: float | None) -> float | None:
    """Return the first non-None value in `values`, else None."""
    for value in values:
        if value is not None:
            return value
    return None


def _iso_date(v: object) -> date | None:
    s = _str(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


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
    ap.add_argument("--db", type=Path, help="Existing database in the product state root")
    ap.add_argument(
        "--repo-root", type=Path, help="Product state root; otherwise use configured authority"
    )
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
    load_project_env(PROJECT_ROOT)

    state_root = (
        args.repo_root.expanduser().resolve()
        if args.repo_root
        else configured_product_state_root(PROJECT_ROOT)
    )
    database = require_db_path(args.db or state_root / "data" / "portfolio.db")
    if database != (state_root / "data" / "portfolio.db").resolve():
        raise ValueError("ETF database must match the product state root/data/portfolio.db")
    conn = connect_sqlite(database, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        if args.from_cache:
            profile, n = ingest_from_cache(
                conn, args.ticker, state_root / "data/historical/fmp", as_of_date=args.as_of
            )
        else:
            profile, n = ingest_live(conn, args.ticker, state_root, as_of_date=args.as_of)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
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
