"""Morning materialization for small, per-ticker cockpit file inputs.

The cockpit reads this one bounded, versioned cache per build.  Missing,
unreadable, or invalid cache entries retain the original direct-read path.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from materialized_cache import (
    cache_metadata,
    read_fresh_payload,
    write_payload_atomically,
)
from pipeline.queries import BRIEFED_LIST_TYPES, tracked_companies_for_user

_CACHE_SCHEMA = "cockpit-file-inputs/v1"
_CACHE_PATH = Path(".cache") / "cockpit_file_inputs.json"
_EXCERPT_LIMIT = 320
_EXCERPT_SUFFIX = "…"
_MACHINE_REFERENCE_RE = re.compile(r"\b(?:sha256:)?[a-f0-9]{40,}\b", re.IGNORECASE)

HoldingsReadError = Literal["none", "missing", "io", "unicode", "json"]
CockpitInputKind = Literal["profile", "peg", "earnings_calendar", "holdings"]


class TickerCockpitFileInputs(BaseModel):
    """Validated, per-ticker projection of the disk inputs used by cockpit rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_readable: bool
    profile_mtime_ns: int | None = None
    profile_size_bytes: int | None = None
    price: float | None = None
    day_move_pct: float | None = None
    price_asof: str | None = None
    peg_readable: bool
    peg_mtime_ns: int | None = None
    peg_size_bytes: int | None = None
    peg_ratio: float | None = None
    earnings_calendar_readable: bool
    earnings_calendar_mtime_ns: int | None = None
    earnings_calendar_size_bytes: int | None = None
    earnings_dates: tuple[str, ...] = ()
    holdings_readable: bool
    holdings_mtime_ns: int | None = None
    holdings_size_bytes: int | None = None
    holdings_payload: object | None = None
    holdings_error: HoldingsReadError


def _holdings_projection(payload: object | None) -> object | None:
    """Retain only the fields consumed by thesis and DCF-link projection."""
    if not isinstance(payload, dict):
        return payload
    raw = cast("dict[str, object]", payload)
    thesis_excerpt = project_micro_thesis_excerpt(raw)
    dcf_defaults = raw.get("dcf_defaults")
    dcf_projection: dict[str, object] | None = None
    if isinstance(dcf_defaults, dict):
        gsheet_id = cast("dict[str, object]", dcf_defaults).get("gsheet_id")
        if isinstance(gsheet_id, str):
            dcf_projection = {"gsheet_id": gsheet_id}
    return {
        "thesis_excerpt": thesis_excerpt,
        "dcf_defaults": dcf_projection,
    }


def project_micro_thesis_excerpt(payload: object) -> str | None:
    """Apply the evaluation surface's existing bounded thesis projection."""
    if not isinstance(payload, dict):
        return None
    thesis = cast("dict[str, object]", payload).get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        return None
    normalized = " ".join(thesis.split())
    normalized = _MACHINE_REFERENCE_RE.sub("source reference", normalized)
    if len(normalized) <= _EXCERPT_LIMIT:
        return normalized
    candidate = normalized[: _EXCERPT_LIMIT - len(_EXCERPT_SUFFIX)]
    boundary = candidate.rsplit(" ", 1)[0] or candidate
    return boundary.rstrip() + _EXCERPT_SUFFIX


def _json_read(path: Path) -> tuple[object | None, HoldingsReadError]:
    try:
        if not path.is_file():
            return None, "missing"
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "unicode"
    except OSError:
        return None, "io"
    try:
        return json.loads(text), "none"
    except json.JSONDecodeError:
        return None, "json"


def _fingerprint(path: Path) -> tuple[int | None, int | None]:
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None, None


def cockpit_input_is_current(
    repo_root: Path,
    ticker: str,
    inputs: TickerCockpitFileInputs,
    kind: CockpitInputKind,
) -> bool:
    """Reject a morning snapshot if its source changed after materialization."""
    paths: dict[CockpitInputKind, tuple[Path, int | None, int | None]] = {
        "profile": (
            repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json",
            inputs.profile_mtime_ns,
            inputs.profile_size_bytes,
        ),
        "peg": (
            repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json",
            inputs.peg_mtime_ns,
            inputs.peg_size_bytes,
        ),
        "earnings_calendar": (
            repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_earnings_calendar.json",
            inputs.earnings_calendar_mtime_ns,
            inputs.earnings_calendar_size_bytes,
        ),
        "holdings": (
            repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json",
            inputs.holdings_mtime_ns,
            inputs.holdings_size_bytes,
        ),
    }
    path, cached_mtime_ns, cached_size = paths[kind]
    if cached_mtime_ns is None or cached_size is None:
        return False
    current_mtime_ns, current_size = _fingerprint(path)
    return current_mtime_ns == cached_mtime_ns and current_size == cached_size


def _profile_from_payload(
    repo_root: Path, ticker: str, payload: object | None, read_error: HoldingsReadError
) -> tuple[float | None, float | None, str | None, bool]:
    if read_error != "none":
        return None, None, None, False
    rec: dict[str, object] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rec = cast("dict[str, object]", payload[0])
    elif isinstance(payload, dict):
        rec = cast("dict[str, object]", payload)
    if rec is None:
        return None, None, None, True
    raw_price = rec.get("price")
    price = float(raw_price) if isinstance(raw_price, (int, float)) and raw_price > 0 else None
    raw_move = rec.get("changePercentage", rec.get("changesPercentage"))
    move = float(raw_move) if isinstance(raw_move, (int, float)) else None
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    try:
        asof = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    except OSError:
        return None, None, None, False
    return price, move, asof, True


def profile_quote(repo_root: Path, ticker: str) -> tuple[float | None, float | None, str | None]:
    """Read price, day move, and source-file mtime from the FMP profile cache."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    payload, error = _json_read(path)
    price, move, asof, _readable = _profile_from_payload(repo_root, ticker, payload, error)
    return price, move, asof


def _peg_from_payload(
    payload: object | None, read_error: HoldingsReadError
) -> tuple[float | None, bool]:
    if read_error != "none":
        return None, False
    if not isinstance(payload, dict):
        return None, True
    value = cast("dict[str, object]", payload).get("peg_ratio")
    return (float(value) if isinstance(value, (int, float)) else None), True


def read_peg_ratio(repo_root: Path, ticker: str) -> float | None:
    """Read PEG from the tolerant valuation-basis cache."""
    path = repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json"
    payload, error = _json_read(path)
    value, _readable = _peg_from_payload(payload, error)
    return value


def _earnings_dates(payload: object | None, read_error: HoldingsReadError) -> tuple[str, ...]:
    if read_error != "none" or not isinstance(payload, list):
        return ()
    dates: list[str] = []
    for item in cast("list[object]", payload):
        if not isinstance(item, dict):
            continue
        value = cast("dict[str, object]", item).get("date")
        if isinstance(value, str) and len(value) >= 10:
            dates.append(value[:10])
    return tuple(dates)


def _next_earnings_from_dates(dates: tuple[str, ...], now: datetime) -> str | None:
    today = now.date().isoformat()
    upcoming = [value for value in dates if value >= today]
    return min(upcoming) if upcoming else None


def next_earnings(repo_root: Path, ticker: str, now: datetime) -> str | None:
    """Earliest upcoming date from the read-only FMP earnings calendar cache."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_earnings_calendar.json"
    payload, error = _json_read(path)
    return _next_earnings_from_dates(_earnings_dates(payload, error), now)


def read_holdings_payload(repo_root: Path, ticker: str) -> tuple[object | None, HoldingsReadError]:
    """Read one holdings file while retaining the failure kind for parity."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    return _json_read(path)


def read_ticker_cockpit_file_inputs(repo_root: Path, ticker: str) -> TickerCockpitFileInputs:
    """Read each real per-ticker source once and pluck the cockpit fields."""
    normalized = ticker.strip().upper()
    fmp = repo_root / "data" / "historical" / "fmp"
    profile_path = fmp / f"{normalized}_profile.json"
    profile_raw, profile_error = _json_read(profile_path)
    price, move, asof, profile_readable = _profile_from_payload(
        repo_root, normalized, profile_raw, profile_error
    )
    profile_mtime_ns, profile_size = _fingerprint(profile_path)
    peg_path = repo_root / "data" / "valuation_basis" / f"{normalized}.json"
    peg_raw, peg_error = _json_read(peg_path)
    peg, peg_readable = _peg_from_payload(peg_raw, peg_error)
    peg_mtime_ns, peg_size = _fingerprint(peg_path)
    earnings_path = fmp / f"{normalized}_earnings_calendar.json"
    earnings_raw, earnings_error = _json_read(earnings_path)
    earnings_readable = earnings_error == "none"
    earnings_mtime_ns, earnings_size = _fingerprint(earnings_path)
    holdings_raw, holdings_error = read_holdings_payload(repo_root, normalized)
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{normalized}.json"
    holdings_mtime_ns, holdings_size = _fingerprint(holdings_path)
    return TickerCockpitFileInputs(
        profile_readable=profile_readable,
        profile_mtime_ns=profile_mtime_ns,
        profile_size_bytes=profile_size,
        price=price,
        day_move_pct=move,
        price_asof=asof,
        peg_readable=peg_readable,
        peg_mtime_ns=peg_mtime_ns,
        peg_size_bytes=peg_size,
        peg_ratio=peg,
        earnings_calendar_readable=earnings_readable,
        earnings_calendar_mtime_ns=earnings_mtime_ns,
        earnings_calendar_size_bytes=earnings_size,
        earnings_dates=_earnings_dates(earnings_raw, earnings_error),
        holdings_readable=holdings_error == "none",
        holdings_mtime_ns=holdings_mtime_ns,
        holdings_size_bytes=holdings_size,
        holdings_payload=_holdings_projection(holdings_raw),
        holdings_error=holdings_error,
    )


def materialize_cockpit_file_inputs(conn: sqlite3.Connection, repo_root: Path) -> int:
    """Atomically publish per-ticker file inputs for the active briefed roster."""
    companies = tracked_companies_for_user(conn, list_types=BRIEFED_LIST_TYPES)
    tickers = sorted({company.ticker.strip().upper() for company in companies if company.ticker})
    inputs = {
        ticker: read_ticker_cockpit_file_inputs(repo_root, ticker).model_dump(mode="json")
        for ticker in tickers
    }
    payload: dict[str, object] = {
        **cache_metadata(_CACHE_SCHEMA),
        "tickers": inputs,
    }
    write_payload_atomically(
        repo_root / _CACHE_PATH,
        payload,
        prefix="cockpit_file_inputs.",
    )
    return len(inputs)


def read_materialized_cockpit_file_inputs(
    repo_root: Path,
) -> dict[str, TickerCockpitFileInputs]:
    """Read the bounded cache, returning only individually valid ticker entries."""
    payload = read_fresh_payload(repo_root / _CACHE_PATH, schema=_CACHE_SCHEMA)
    raw_tickers = payload.get("tickers")
    if not isinstance(raw_tickers, dict):
        return {}
    parsed: dict[str, TickerCockpitFileInputs] = {}
    for raw_ticker, raw_input in cast("dict[object, object]", raw_tickers).items():
        if not isinstance(raw_ticker, str) or not isinstance(raw_input, dict):
            continue
        try:
            parsed[raw_ticker.strip().upper()] = TickerCockpitFileInputs.model_validate(raw_input)
        except (TypeError, ValueError, ValidationError):
            continue
    return parsed


def cached_next_earnings(inputs: TickerCockpitFileInputs, now: datetime) -> str | None:
    """Project a cached calendar against today's date, not the cache's date."""
    return _next_earnings_from_dates(inputs.earnings_dates, now)


__all__ = [
    "CockpitInputKind",
    "HoldingsReadError",
    "TickerCockpitFileInputs",
    "cached_next_earnings",
    "cockpit_input_is_current",
    "materialize_cockpit_file_inputs",
    "next_earnings",
    "profile_quote",
    "project_micro_thesis_excerpt",
    "read_holdings_payload",
    "read_materialized_cockpit_file_inputs",
    "read_peg_ratio",
]
