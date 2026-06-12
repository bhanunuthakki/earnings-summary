"""SEDI insider-transaction adapter (Canadian Securities Administrators).

The Canadian counterpart to SEC Form 4 lives in SEDI — the System for
Electronic Disclosure by Insiders. It covers the Brookfield family
(BN, BAM, BEPC, BIPC), Enbridge / TC Energy, the Canadian miners (HBM,
IVN, TECK, FNV, WPM), and others.

Unlike SEC EDGAR, SEDI does not expose a clean public JSON API. Data is
behind an HTML-form-based UI at sedi.ca. Two fetching strategies:

  1. Public ad-hoc scrape of sedi.ca's "Insider transactions" form. Fragile
     and rate-limited; requires session-cookie handling.
  2. Third-party aggregators (canadianinsider.com, INK Research) — paid
     APIs with stable JSON.

This module defines the adapter INTERFACE and a deterministic mapping from
SEDI rows to insider_transactions schema. The actual fetcher is a stub —
implementations can swap in either strategy without changing the calling
code in execution/backfill_insider_transactions.py.

When implemented, the upsert path is identical to SEC Form 4:
insider_transactions row with regulator='SEDI'.

To stub-implement: drop a CSV/JSON dump at
`data/sedi_dumps/<TICKER>_sedi.json` with the SEDI columns and call
``ingest_sedi_dump(path, ticker)``.

Schema mapping (SEDI columns → our insider_transactions columns):
  insider_name        → insider_name
  position            → insider_title
  relationship        → insider_relation ("Director", "Senior Officer",
                        "10% Holder", "Issuer", etc.)
  transaction_date    → transaction_date
  filing_date         → filing_date
  nature_of_transaction → transaction_type (mapped per _SEDI_TYPE_MAP below)
  shares              → shares
  unit_price          → price_per_share
  total_consideration → transaction_value
  shares_owned_after  → shares_owned_after
  ownership_form      → ownership_form ('D' direct / 'I' indirect)

Currency: SEDI prices are in CAD for most issuers but USD for cross-listed
US-domiciled companies (BN reports in USD). The adapter records the
currency unchanged; downstream consumers should translate via fx_rates
when comparing cross-border.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import cast

from insider_transactions import InsiderTransaction, upsert

log = logging.getLogger(__name__)


# SEDI nature-of-transaction codes (per the official code table) mapped to
# our canonical transaction_type values. Codes 00-99 are the SEDI standard.
_SEDI_TYPE_MAP: dict[str, str] = {
    "10": "open_market_buy",  # Acquisition in the public market
    "11": "open_market_sell",  # Disposition in the public market
    "15": "open_market_buy",  # Acquisition under a stock dividend program
    "16": "open_market_sell",  # Disposition - tax
    "22": "stock_grant",  # Acquisition pursuant to a security-based comp arrangement
    "30": "option_exercise",  # Acquisition under exercise of options
    "35": "option_exercise_and_sell",  # Exercise then immediate disposition
    "40": "gift",  # Gift
    "47": "stock_grant",  # Grant or award
    "50": "rsu_vesting",  # Distribution under RSU plan
    "55": "rsu_vesting",  # Conversion / vesting
    "97": "other",  # Other
    "99": "other",  # Other
}


def _map_sedi_type(code: str) -> str:
    """Map SEDI transaction code to canonical type. Unknown → 'other'."""
    return _SEDI_TYPE_MAP.get(str(code).strip(), "other")


def ingest_sedi_dump(
    *,
    dump_path: Path,
    ticker: str,
    db_path: Path | None = None,
) -> int:
    """Ingest a SEDI dump file (JSON array of row objects) → insider_transactions.

    Expected JSON shape (one record per row):
        [
          {{
            "insider_name": "...",
            "position": "Chief Executive Officer",
            "relationship": "Senior Officer",
            "transaction_date": "2026-04-15",
            "filing_date": "2026-04-17",
            "nature_of_transaction_code": "10",
            "shares": 1000.0,
            "unit_price": 84.32,
            "total_consideration": 84320.0,
            "shares_owned_after": 50000.0,
            "ownership_form": "D" | "I",
            "currency": "CAD" | "USD"
          }},
          ...
        ]

    Returns the number of rows inserted (idempotent on the natural key
    enforced by insider_transactions).
    """
    if not dump_path.exists():
        log.warning({"event": "sedi_dump_missing", "path": str(dump_path)})
        return 0

    try:
        rows = json.loads(dump_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning({"event": "sedi_dump_read_failed", "error": str(exc)})
        return 0
    if not isinstance(rows, list):
        return 0

    transactions: list[InsiderTransaction] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        r = cast("dict[str, object]", raw)
        try:
            tx_date = _parse_date(r.get("transaction_date"))
            filing_date = _parse_date(r.get("filing_date"))
            if tx_date is None:
                continue
            shares = float(r.get("shares") or 0)
            if shares <= 0:
                continue
            price_raw = r.get("unit_price")
            total_raw = r.get("total_consideration")
            price = float(price_raw) if isinstance(price_raw, (int, float)) else None
            total = (
                float(total_raw)
                if isinstance(total_raw, (int, float))
                else (shares * price if price is not None else None)
            )
            owned_after_raw = r.get("shares_owned_after")
            owned_after = (
                float(owned_after_raw) if isinstance(owned_after_raw, (int, float)) else None
            )
            transactions.append(
                InsiderTransaction(
                    ticker=ticker,
                    insider_name=str(r.get("insider_name") or "(unknown)"),
                    insider_title=str(r.get("position") or "") or None,
                    insider_relation=str(r.get("relationship") or "") or None,
                    transaction_date=tx_date,
                    filing_date=filing_date or tx_date,
                    transaction_type=_map_sedi_type(
                        str(r.get("nature_of_transaction_code") or "99")
                    ),
                    shares=shares,
                    price_per_share=price,
                    transaction_value=total,
                    shares_owned_after=owned_after,
                    ownership_form=str(r.get("ownership_form") or "")[:1] or None,
                    form_filed="SEDI",
                    regulator="SEDI",
                    notes=f"currency={r.get('currency') or 'CAD'}",
                )
            )
        except (ValueError, TypeError) as exc:
            log.debug({"event": "sedi_row_skip", "error": str(exc)})
            continue

    inserted = upsert(transactions, db_path=db_path)
    log.info({"event": "sedi_dump_ingested", "ticker": ticker, "inserted": inserted})
    return inserted


def _parse_date(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw)
        if len(s) == 10:
            return datetime.fromisoformat(s + "T00:00:00+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Stub fetcher — placeholder for a real SEDI scraper
# ---------------------------------------------------------------------------


def fetch_sedi_for_ticker(ticker: str) -> int:
    """Live fetch from SEDI for a single ticker.

    NOT IMPLEMENTED — SEDI requires HTML form submission with session cookies
    plus careful rate limiting. Suggested implementations:

      1. Headless browser (Playwright) walking the SEDI public insider-trans
         form. Slow (~10-30s per ticker) but the cleanest legal stance.
      2. Third-party API: canadianinsider.com offers structured access at a
         monthly cost; INK Research has a paid feed.
      3. Manual CSV export from SEDI's "Public filings" search → save to
         ``data/sedi_dumps/<TICKER>_sedi.json`` then call
         ``ingest_sedi_dump(dump_path, ticker)``.

    Until implemented, this function logs a no-op and returns 0. The
    backfill orchestrator (execution/backfill_insider_transactions.py) should
    catch the absence and skip cleanly.
    """
    log.info(
        {
            "event": "sedi_fetcher_stub",
            "ticker": ticker,
            "message": (
                "SEDI live fetch not implemented. Drop a JSON dump at "
                f"data/sedi_dumps/{ticker}_sedi.json and call ingest_sedi_dump."
            ),
        }
    )
    return 0
