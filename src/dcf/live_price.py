"""Read the current market price + timestamp for a ticker.

For Phase 3 the only source is the FMP `profile.json` cache. The autopilot
data pipeline refreshes profile.json on each fetch cycle, so the price
should be no more than a day stale in normal operation. Phase 4 will add a
dedicated quote fetcher with its own freshness budget.

Returns None if the file is missing, malformed, or lacks a usable price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class LivePrice:
    price: float
    fetched_at: datetime  # mtime of the profile.json cache file (UTC)


def read_live_price(repo_root: Path, ticker: str) -> LivePrice | None:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    if not path.exists():
        return None
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    rec: dict[str, object] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rec = cast("dict[str, object]", payload[0])
    elif isinstance(payload, dict):
        rec = cast("dict[str, object]", payload)
    if rec is None:
        return None

    raw_price = rec.get("price")
    if not isinstance(raw_price, (int, float)):
        return None
    price = float(raw_price)
    if price <= 0:
        return None

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return LivePrice(price=price, fetched_at=mtime)
