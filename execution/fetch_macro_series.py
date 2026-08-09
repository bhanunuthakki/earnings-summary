"""execution/fetch_macro_series.py — populate the macro_series table.

Iterates the 12-series registry in src/macro_series.py, tries each series'
provider candidates in order, and upserts whatever rows come back into
macro_series. Idempotent: re-fetches the same date overwrite the prior
value rather than duplicating.

Failure modes — none of these raise; each just logs + skips:
  - missing FMP_API_KEY        → all series logged as skipped
  - endpoint 4xx/5xx           → that provider candidate skipped, next tried
  - empty response             → next candidate tried
  - rate-limited (429)         → backs off to single-threaded with sleeps
  - response shape unexpected  → parsed loosely; rows with unparseable date
                                 silently dropped

CLI:
    python execution/fetch_macro_series.py                # all 12
    python execution/fetch_macro_series.py --series fed_funds,vix
    python execution/fetch_macro_series.py --dry-run      # no DB writes

Exit code 0 if at least one series populated rows; 1 if every series failed
(the spec says success ≥ 6 of 12; the script only signals all-failed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, cast

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from log_redact import redact as _redact  # noqa: E402
from macro_series import REGISTRY, ProviderSpec, SeriesSpec  # noqa: E402
from macro_store import upsert_series_value  # noqa: E402
from net.client import (  # noqa: E402
    DEFAULT_FMP_BASE_URL,
    FMP_CLIENT,
    HttpCallError,
    HttpErrorKind,
    JsonShape,
)
from runtime.secrets import load_project_env  # noqa: E402

load_project_env(PROJECT_ROOT)
FMP_API_KEY = os.environ.get("FMP_API_KEY")
FMP_STABLE = DEFAULT_FMP_BASE_URL

log = logging.getLogger("fetch_macro_series")


def _sync_db_path(repo_root: Path) -> None:
    """Re-point the db module + macro_store DB target at the caller's repo."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def _resolve_url(provider: ProviderSpec) -> str:
    # All macro providers are on /stable (treasury-rates, economic-indicators,
    # historical-price-eod/full). The legacy /api/v3 `historical-price-full`
    # fallbacks were retired in the v3->stable migration (they 403 as "Legacy
    # Endpoint" for non-legacy accounts). A fully-qualified URL is honoured
    # verbatim; every other path is a /stable endpoint name.
    if provider.path.startswith(("http://", "https://")):
        return provider.path
    return f"{FMP_STABLE}/{provider.path}"


def _fetch_json(provider: ProviderSpec, *, sleep_seconds: float = 0.0) -> object | None:
    if not FMP_API_KEY:
        log.warning({"event": "macro_no_api_key", "series_path": provider.path})
        return None
    params = dict(provider.params)
    url = _resolve_url(provider)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    try:
        response = FMP_CLIENT.get_url_json(
            url,
            params=params,
            api_key=FMP_API_KEY,
            expected=JsonShape.ANY,
        )
    except HttpCallError as exc:
        if exc.kind is HttpErrorKind.RATE_LIMIT:
            log.warning({"event": "macro_rate_limited", "url": url, "status": exc.status_code})
            return "RATE_LIMITED"
        log.info(
            {
                "event": "macro_fetch_error",
                "url": url,
                "status": exc.status_code,
                "error": str(exc),
            }
        )
        return None
    return response.payload


def _extract_rows(payload: object, provider: ProviderSpec) -> list[dict[str, Any]]:
    """Pull the row-list out of FMP's various response shapes."""
    if payload is None:
        return []
    if provider.row_field:
        if isinstance(payload, dict):
            inner = cast("dict[str, object]", payload).get(provider.row_field)
            if isinstance(inner, list):
                return [r for r in cast("list[Any]", inner) if isinstance(r, dict)]
        return []
    if isinstance(payload, list):
        return [r for r in cast("list[Any]", payload) if isinstance(r, dict)]
    # Some endpoints return an object with `historical` key without our hint;
    # try a best-effort fallback.
    if isinstance(payload, dict):
        for k in ("historical", "data", "rates", "values"):
            inner = cast("dict[str, object]", payload).get(k)
            if isinstance(inner, list):
                return [r for r in cast("list[Any]", inner) if isinstance(r, dict)]
    return []


def _parse_date(raw: object) -> date | None:
    if raw is None:
        return None
    s = str(raw)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        f = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # Filter NaN / inf so downstream regression isn't poisoned.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _yfinance_rows(provider: ProviderSpec, *, dry_run: bool, series_id: str) -> int:
    """Fetch a yfinance provider (2026-07-19 review: FMP had been 429ing all
    12 series daily). Reuses factor_proxies.fetch_proxy_series — the repo's
    established offline-degrade yfinance reader — and applies the provider's
    scale (e.g. ^TNX yield×10 → percent). Returns rows persisted; 0 = failed
    (next candidate tried)."""
    from factor_proxies import fetch_proxy_series

    rows = fetch_proxy_series(provider.path)
    n_written = 0
    for d, v in rows:
        value = v * provider.scale
        if dry_run:
            n_written += 1
            continue
        row_id = upsert_series_value(
            series_id=series_id,
            rate_date=d,
            value=value,
            source=provider.source,
        )
        if row_id is not None:
            n_written += 1
    return n_written


def _populate_one_series(series: SeriesSpec, *, dry_run: bool, sleep_seconds: float) -> int:
    """Try each provider in order. Returns the number of rows persisted (0
    means every candidate failed for this series)."""
    for provider in series.providers:
        if provider.kind == "yfinance":
            n_yf = _yfinance_rows(provider, dry_run=dry_run, series_id=series.series_id)
            if n_yf > 0:
                log.info(
                    {
                        "event": "macro_series_populated",
                        "series_id": series.series_id,
                        "provider": provider.path,
                        "source": provider.source,
                        "rows": n_yf,
                        "dry_run": dry_run,
                    }
                )
                return n_yf
            continue
        payload = _fetch_json(provider, sleep_seconds=sleep_seconds)
        if payload == "RATE_LIMITED":
            # Spec: switch to single-threaded with sleeps if rate limited.
            time.sleep(2.0)
            payload = _fetch_json(provider, sleep_seconds=sleep_seconds + 1.0)
        if payload is None:
            continue
        rows = _extract_rows(payload, provider)
        if not rows:
            continue
        n_written = 0
        for raw in rows:
            d = _parse_date(raw.get(provider.date_key))
            v = _parse_float(raw.get(provider.value_key))
            if d is None or v is None:
                continue
            if dry_run:
                n_written += 1
                continue
            row_id = upsert_series_value(
                series_id=series.series_id,
                rate_date=d,
                value=v,
                source=provider.source,
            )
            if row_id is not None:
                n_written += 1
        if n_written > 0:
            log.info(
                {
                    "event": "macro_series_populated",
                    "series_id": series.series_id,
                    "provider": provider.path,
                    "rows": n_written,
                    "dry_run": dry_run,
                }
            )
            return n_written
    log.warning(
        {
            "event": "macro_series_empty",
            "series_id": series.series_id,
            "tried": len(series.providers),
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series",
        help="Comma-separated series_ids. Omit for all 12.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse but don't write to DB.")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between requests (raise on rate limit).",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root containing data/."
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _sync_db_path(args.repo_root.resolve())

    if args.series:
        ids = [s.strip() for s in args.series.split(",") if s.strip()]
        missing = [sid for sid in ids if sid not in REGISTRY]
        if missing:
            print(f"Unknown series ids: {missing}", file=sys.stderr)
            return 2
    else:
        ids = list(REGISTRY.keys())

    if not FMP_API_KEY:
        print(
            "Warning: FMP_API_KEY not set in .env — every series will be skipped.", file=sys.stderr
        )

    summary: dict[str, int] = {}
    for sid in ids:
        spec = REGISTRY[sid]
        n = _populate_one_series(spec, dry_run=args.dry_run, sleep_seconds=args.sleep)
        summary[sid] = n

    populated = sum(1 for n in summary.values() if n > 0)
    total = len(summary)
    print(json.dumps({"populated": populated, "total": total, "per_series": summary}, indent=2))
    if populated == 0 and not args.dry_run:
        _fire_deadman(args.repo_root.resolve(), tried=total)
    return 0 if populated > 0 else 1


def _fire_deadman(repo_root: Path, *, tried: int) -> None:
    """The 'data_feed_stale' dead-man (0183): a full run that populated ZERO
    series is an outage, not a quiet day — the 2026-07 incident had every FMP
    series 429ing daily with "populated": 0 in a cron log nobody reads while
    betas silently recomputed off frozen series. One book-level alert per day,
    signature-deduped; never raises into the run it watches."""
    try:
        from datetime import UTC, datetime

        from alerts import store as alerts_store

        db_path = repo_root / "data" / "portfolio.db"
        now = datetime.now(UTC).replace(tzinfo=None)
        sig = alerts_store.compute_signature_sha(
            "data_feed_stale", "PORTFOLIO", {"feed": "macro_series", "date": now.date().isoformat()}
        )
        if alerts_store.find_by_signature(signature_sha=sig, db_path=db_path) is not None:
            return
        alerts_store.fire_alert(
            ticker="PORTFOLIO",
            trigger_kind="data_feed_stale",
            fired_at=now,
            evidence_json=json.dumps(
                {"feed": "macro_series", "series_tried": tried, "populated": 0}
            ),
            signature_sha=sig,
            db_path=db_path,
        )
        log.warning({"event": "macro_deadman_fired", "series_tried": tried})
    except Exception as exc:
        log.warning({"event": "macro_deadman_failed", "error": _redact(exc)})


if __name__ == "__main__":
    sys.exit(main())
