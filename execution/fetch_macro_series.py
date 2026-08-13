"""Acquire macro series without bypassing provider governance.

Yahoo candidates are timeout bounded. FMP candidates are deliberately disabled
until macro work is admitted by the shared FMP circuit/budget/recovery service;
this job never calls FMP directly. All values are validated in memory and then
committed in one short, value-change-aware transaction.
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from log_redact import redact  # noqa: E402
from macro_series import REGISTRY, ProviderSpec  # noqa: E402
from macro_store import (  # noqa: E402
    SeriesBatchWriteError,
    SeriesValue,
    SeriesWriteReceipt,
    latest_series_value,
    upsert_series_value,
    upsert_series_values,
)
from net.client import DEFAULT_FMP_BASE_URL  # noqa: E402
from sources import registry as source_calls  # noqa: E402


class AttemptOutcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    ERROR = "error"
    DISABLED = "disabled"


class SeriesAvailability(StrEnum):
    FRESH = "fresh"
    CACHED_DEGRADED = "cached_degraded"
    UNAVAILABLE = "unavailable"


class RefreshStatus(StrEnum):
    FRESH = "fresh"
    CACHED_DEGRADED = "cached_degraded"
    PARTIAL = "partial"
    FAILED = "failed"


class _ReceiptModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderAttempt(_ReceiptModel):
    source: str
    provider: str
    outcome: AttemptOutcome
    reason: str | None = None
    latency_ms: int | None = None
    rows: int = 0


class SeriesReceipt(_ReceiptModel):
    series_id: str
    availability: SeriesAvailability
    rows_acquired: int
    cached_through: date | None = None
    cache_age_days: int | None = None
    attempts: tuple[ProviderAttempt, ...]


class WriteReceipt(_ReceiptModel):
    inserted: int
    updated: int
    unchanged: int


class MacroRefreshReceipt(_ReceiptModel):
    status: RefreshStatus
    compute_eligible: bool
    series: tuple[SeriesReceipt, ...]
    write: WriteReceipt
    store_error: str | None = None


YFinanceLoader = Callable[..., list[tuple[date, float]]]
DEFAULT_TIMEOUT_SECONDS = 12.0
MAX_CACHED_AGE_DAYS = 45
FMP_STABLE = DEFAULT_FMP_BASE_URL


def _resolve_url(provider: ProviderSpec) -> str:
    """Compatibility resolver; URL construction does not authorize a call."""
    if provider.path.startswith(("http://", "https://")):
        return provider.path
    return f"{FMP_STABLE}/{provider.path}"


def _fetch_json(provider: ProviderSpec, *, sleep_seconds: float = 0.0) -> None:
    """Fail-closed compatibility seam: macro FMP calls belong to recovery."""
    del provider, sleep_seconds


def _extract_rows(payload: object, provider: ProviderSpec) -> list[dict[str, Any]]:
    if provider.row_field and isinstance(payload, dict):
        inner = cast("dict[str, object]", payload).get(provider.row_field)
        if not isinstance(inner, list):
            return []
        typed_inner = cast("list[object]", inner)
        return [cast("dict[str, Any]", item) for item in typed_inner if isinstance(item, dict)]
    if isinstance(payload, list):
        typed_payload = cast("list[object]", payload)
        return [cast("dict[str, Any]", item) for item in typed_payload if isinstance(item, dict)]
    return []


def _yfinance_rows(provider: ProviderSpec, *, dry_run: bool, series_id: str) -> int:
    """Legacy focused-test seam; production uses the bounded batch path."""
    from factor_proxies import fetch_proxy_series

    rows = fetch_proxy_series(provider.path)
    if dry_run:
        return len(rows)
    return sum(
        upsert_series_value(
            series_id=series_id,
            rate_date=rate_date,
            value=value * provider.scale,
            source=provider.source,
        )
        is not None
        for rate_date, value in rows
    )


def _populate_one_series(series: Any, *, dry_run: bool, sleep_seconds: float) -> int:
    """Legacy parser-test seam; production routes through :func:`refresh_series`."""
    for provider in series.providers:
        if provider.kind == "yfinance":
            count = _yfinance_rows(provider, dry_run=dry_run, series_id=series.series_id)
            if count:
                return count
            continue
        payload = _fetch_json(provider, sleep_seconds=sleep_seconds)
        rows = _extract_rows(payload, provider)
        valid = [
            row
            for row in rows
            if isinstance(row.get(provider.date_key), str)
            and isinstance(row.get(provider.value_key), (int, float))
        ]
        if valid and dry_run:
            return len(valid)
    return 0


def _sync_db_path(repo_root: Path) -> Path:
    """Re-point repository DB helpers at a caller-selected root."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    path = repo_root / "data" / "portfolio.db"
    source_calls.set_db_path(path)
    return path


def _default_yfinance_loader(symbol: str, *, timeout_seconds: float) -> list[tuple[date, float]]:
    import yfinance as yf  # type: ignore[import-untyped]

    history = (
        cast("Any", yf)
        .Ticker(symbol)
        .history(
            period="2y",
            interval="1d",
            auto_adjust=True,
            timeout=timeout_seconds,
        )
    )
    stamps: list[Any] = history.index.tolist()
    closes: list[Any] = history["Close"].tolist()
    rows: list[tuple[date, float]] = []
    for stamp, close in zip(stamps, closes, strict=False):
        raw_date = stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])
        if isinstance(raw_date, datetime):
            raw_date = raw_date.date()
        rows.append((raw_date, float(close)))
    return rows


def _bounded_yfinance_call(
    loader: YFinanceLoader,
    symbol: str,
    *,
    timeout_seconds: float,
) -> tuple[AttemptOutcome, list[tuple[date, float]], int, str | None]:
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put((True, loader(symbol, timeout_seconds=timeout_seconds)))
        except Exception as exc:  # provider boundary: converted to typed receipt
            result.put((False, exc))

    started = time.monotonic()
    worker = threading.Thread(target=invoke, daemon=True, name=f"macro-yf-{symbol}")
    worker.start()
    worker.join(timeout_seconds)
    latency_ms = int((time.monotonic() - started) * 1000)
    if worker.is_alive():
        return AttemptOutcome.TIMEOUT, [], latency_ms, "timeout"
    ok, payload = result.get_nowait()
    if not ok:
        return AttemptOutcome.ERROR, [], latency_ms, type(cast("Exception", payload)).__name__
    rows = cast("list[tuple[date, float]]", payload)
    return (AttemptOutcome.OK if rows else AttemptOutcome.EMPTY), rows, latency_ms, None


def _validate_rows(
    rows: list[tuple[date, float]], provider: ProviderSpec, *, today: date
) -> tuple[SeriesValue, ...]:
    staged: dict[date, SeriesValue] = {}
    for rate_date, raw_value in rows:
        value = float(raw_value) * provider.scale
        if rate_date > today or not math.isfinite(value) or value <= 0:
            raise ValueError("provider returned an implausible macro value")
        point = SeriesValue(
            series_id="__pending__",
            rate_date=rate_date,
            value=value,
            source=provider.source,
        )
        prior = staged.get(rate_date)
        if prior is not None and prior.value != point.value:
            raise ValueError("provider returned conflicting duplicate dates")
        staged[rate_date] = point
    return tuple(staged[key] for key in sorted(staged))


def _fmp_disabled_reason(db_path: Path) -> str:
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT state FROM provider_circuit_state WHERE provider='fmp'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return "circuit_unverified"
    if row is None:
        return "circuit_unverified"
    state = str(row[0]).upper()
    if state == "OPEN":
        return "circuit_open"
    if state == "HALF_OPEN":
        return "circuit_half_open"
    return "shared_recovery_only"


def _source_call(attempt: ProviderAttempt, series_id: str) -> source_calls.PendingSourceCall:
    status = {
        AttemptOutcome.OK: source_calls.CallStatus.OK,
        AttemptOutcome.EMPTY: source_calls.CallStatus.NOT_FOUND,
        AttemptOutcome.DISABLED: source_calls.CallStatus.SKIPPED,
        AttemptOutcome.TIMEOUT: source_calls.CallStatus.ERROR,
        AttemptOutcome.ERROR: source_calls.CallStatus.ERROR,
    }[attempt.outcome]
    return source_calls.PendingSourceCall(
        source_name=attempt.source,
        kind=f"macro_series:{series_id}",
        ticker=None,
        status=status,
        latency_ms=attempt.latency_ms,
        record_count=attempt.rows,
        notes=f"{attempt.provider}: {attempt.reason}" if attempt.reason else attempt.provider,
    )


def refresh_series(
    *,
    series_ids: tuple[str, ...],
    db_path: Path,
    yfinance_loader: YFinanceLoader = _default_yfinance_loader,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> MacroRefreshReceipt:
    """Finish acquisition first, then persist all staged values atomically."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    today = stamp.date()
    staged: list[SeriesValue] = []
    raw_receipts: list[tuple[str, tuple[ProviderAttempt, ...], int, date | None]] = []
    call_receipts: list[source_calls.PendingSourceCall] = []

    fmp_reason = _fmp_disabled_reason(db_path)
    for series_id in series_ids:
        spec = REGISTRY[series_id]
        attempts: list[ProviderAttempt] = []
        acquired = 0
        newest_acquired: date | None = None
        for provider in spec.providers:
            if provider.kind != "yfinance":
                attempt = ProviderAttempt(
                    source="FMP",
                    provider=provider.path,
                    outcome=AttemptOutcome.DISABLED,
                    reason=fmp_reason,
                )
                attempts.append(attempt)
                call_receipts.append(_source_call(attempt, series_id))
                continue
            outcome, rows, latency_ms, reason = _bounded_yfinance_call(
                yfinance_loader,
                provider.path,
                timeout_seconds=timeout_seconds,
            )
            validated: tuple[SeriesValue, ...] = ()
            if outcome is AttemptOutcome.OK:
                try:
                    pending = _validate_rows(rows, provider, today=today)
                    validated = tuple(
                        SeriesValue(
                            series_id=series_id,
                            rate_date=item.rate_date,
                            value=item.value,
                            source=item.source,
                        )
                        for item in pending
                    )
                except ValueError as exc:
                    outcome = AttemptOutcome.ERROR
                    reason = redact(exc)
            attempt = ProviderAttempt(
                source="yfinance",
                provider=provider.path,
                outcome=outcome,
                reason=reason,
                latency_ms=latency_ms,
                rows=len(validated),
            )
            attempts.append(attempt)
            call_receipts.append(_source_call(attempt, series_id))
            if validated:
                staged.extend(validated)
                acquired = len(validated)
                newest_acquired = validated[-1].rate_date
                break
        raw_receipts.append((series_id, tuple(attempts), acquired, newest_acquired))

    write = SeriesWriteReceipt(inserted=0, updated=0, unchanged=0)
    store_error: str | None = None
    if staged and not dry_run:
        try:
            write = upsert_series_values(tuple(staged), db_path=db_path)
        except SeriesBatchWriteError as exc:
            store_error = redact(exc)
    if not dry_run:
        source_calls.set_db_path(db_path)
        source_calls.log_calls_batch(call_receipts)

    series_receipts: list[SeriesReceipt] = []
    for series_id, raw_attempts, acquired, newest_acquired in raw_receipts:
        cached = latest_series_value(series_id=series_id, db_path=db_path)
        persisted_date = cached.rate_date if cached is not None else None
        effective_date = persisted_date
        if (
            dry_run
            and newest_acquired is not None
            and (effective_date is None or newest_acquired > effective_date)
        ):
            effective_date = newest_acquired
        age = (today - effective_date).days if effective_date is not None else None
        acquired_age = (today - newest_acquired).days if newest_acquired is not None else None
        acquired_is_fresh = (
            acquired > 0 and acquired_age is not None and 0 <= acquired_age <= MAX_CACHED_AGE_DAYS
        )
        effective_is_fresh = age is not None and 0 <= age <= MAX_CACHED_AGE_DAYS
        if acquired_is_fresh and effective_is_fresh and store_error is None:
            availability = SeriesAvailability.FRESH
        elif cached is not None and effective_is_fresh:
            availability = SeriesAvailability.CACHED_DEGRADED
        else:
            availability = SeriesAvailability.UNAVAILABLE
        series_receipts.append(
            SeriesReceipt(
                series_id=series_id,
                availability=availability,
                rows_acquired=acquired,
                cached_through=effective_date,
                cache_age_days=age,
                attempts=raw_attempts,
            )
        )

    available = sum(
        item.availability is not SeriesAvailability.UNAVAILABLE for item in series_receipts
    )
    fresh = sum(item.availability is SeriesAvailability.FRESH for item in series_receipts)
    if store_error is not None or available == 0:
        status = RefreshStatus.FAILED
    elif available < len(series_receipts):
        status = RefreshStatus.PARTIAL
    elif fresh == len(series_receipts):
        status = RefreshStatus.FRESH
    else:
        status = RefreshStatus.CACHED_DEGRADED
    return MacroRefreshReceipt(
        status=status,
        compute_eligible=status in {RefreshStatus.FRESH, RefreshStatus.CACHED_DEGRADED},
        series=tuple(series_receipts),
        write=WriteReceipt(
            inserted=write.inserted,
            updated=write.updated,
            unchanged=write.unchanged,
        ),
        store_error=store_error,
    )


def _exit_code(status: RefreshStatus) -> int:
    return {
        RefreshStatus.FRESH: 0,
        RefreshStatus.CACHED_DEGRADED: 2,
        RefreshStatus.PARTIAL: 3,
        RefreshStatus.FAILED: 1,
    }[status]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", help="Comma-separated series IDs; default is all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    ids = (
        tuple(item.strip() for item in args.series.split(",") if item.strip())
        if args.series
        else tuple(REGISTRY)
    )
    missing = tuple(item for item in ids if item not in REGISTRY)
    if missing:
        print(json.dumps({"event": "macro_unknown_series", "series_ids": missing}), file=sys.stderr)
        return 1
    db_path = _sync_db_path(args.repo_root.resolve())
    receipt = refresh_series(
        series_ids=ids,
        db_path=db_path,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )
    print(receipt.model_dump_json(indent=2))
    return _exit_code(receipt.status)


if __name__ == "__main__":
    raise SystemExit(main())
