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

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from log_redact import redact as _redact  # noqa: E402
from macro_series import REGISTRY, ProviderSpec, SeriesSpec  # noqa: E402
from macro_store import upsert_series_value  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
FMP_API_KEY = os.environ.get("FMP_API_KEY")
FMP_STABLE = "https://financialmodelingprep.com/stable"
FMP_V3 = "https://financialmodelingprep.com/api/v3"
FMP_V4 = "https://financialmodelingprep.com/api/v4"

log = logging.getLogger("fetch_macro_series")


def _sync_db_path(repo_root: Path) -> None:
    """Re-point the db module + macro_store DB target at the caller's repo."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def _resolve_url(provider: ProviderSpec) -> str:
    # `historical-price-full/<symbol>` is /api/v3; `historical-price-eod/full`
    # and `treasury-rates` + `economic-indicators` are /stable. The kind tag
    # + path together steer us at one base; trust the path's leading slash if
    # it's not just an endpoint name.
    if provider.path.startswith(("http://", "https://")):
        return provider.path
    if provider.path.startswith("v3/"):
        return f"{FMP_V3}/{provider.path[3:]}"
    if provider.path.startswith("v4/"):
        return f"{FMP_V4}/{provider.path[3:]}"
    if "historical-price-full/" in provider.path:
        return f"{FMP_V3}/{provider.path}"
    return f"{FMP_STABLE}/{provider.path}"


def _fetch_json(provider: ProviderSpec, *, sleep_seconds: float = 0.0) -> object | None:
    if not FMP_API_KEY:
        log.warning({"event": "macro_no_api_key", "series_path": provider.path})
        return None
    params = dict(provider.params)
    params["apikey"] = FMP_API_KEY
    url = _resolve_url(provider)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    try:
        resp = requests.get(url, params=params, timeout=30)
    except Exception as exc:  # noqa: BLE001 — fail-soft per spec; covers ConnectionError, timeout, ...
        log.info({"event": "macro_fetch_error", "url": url, "error": _redact(exc)})
        return None
    if resp.status_code == 429:
        log.warning(
            {"event": "macro_rate_limited", "url": url, "status": resp.status_code}
        )
        return "RATE_LIMITED"
    try:
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        log.info(
            {"event": "macro_http_error", "url": url, "status": resp.status_code, "error": _redact(exc)}
        )
        return None
    except Exception as exc:  # noqa: BLE001 — JSON decode + other parse failures
        log.info({"event": "macro_parse_error", "url": url, "error": _redact(exc)})
        return None


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


def _populate_one_series(
    series: SeriesSpec, *, dry_run: bool, sleep_seconds: float
) -> int:
    """Try each provider in order. Returns the number of rows persisted (0
    means every candidate failed for this series)."""
    for provider in series.providers:
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
        {"event": "macro_series_empty", "series_id": series.series_id, "tried": len(series.providers)}
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series",
        help="Comma-separated series_ids. Omit for all 12.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse but don't write to DB."
    )
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
        print("Warning: FMP_API_KEY not set in .env — every series will be skipped.", file=sys.stderr)

    summary: dict[str, int] = {}
    for sid in ids:
        spec = REGISTRY[sid]
        n = _populate_one_series(spec, dry_run=args.dry_run, sleep_seconds=args.sleep)
        summary[sid] = n

    populated = sum(1 for n in summary.values() if n > 0)
    total = len(summary)
    print(json.dumps({"populated": populated, "total": total, "per_series": summary}, indent=2))
    return 0 if populated > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
