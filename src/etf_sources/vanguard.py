"""Vanguard issuer adapter — investor.vanguard.com's public JSON API.

Endpoints (scouted 2026-07-10; both plain GETs, no auth, UA only):

  …/profile/api/{T}/portfolio-holding/stock
      Paginated full equity holdings. Payload: ``{"size": N, "asOfDate":
      "2026-05-31", "next": <url|absent>, "fund": {"entity": [{"longName",
      "shortName", "sharesHeld": "336692988", "marketValue": 24944013049.51,
      "ticker": "2330", "isin", "cusip", "percentWeight": "14.64"}, …]}}``.
      ``percentWeight`` is percent units (14.64 = 14.64%); tickers are local
      exchange symbols for non-US listings.

  …/profile/api/{T}/profile
      Fund identity. ``fundProfile.expenseRatio`` is percent units
      ("0.0600" = 6 bps). No basket P/E / P/B is published on this API —
      those stay None (the score's valuation factor degrades per idiom).

Defensive throughout: any shape surprise returns None (the registry logs and
the caller falls back to the N-PORT spine). Page count is capped — VWO's
~5,000 rows arrive in well under the cap.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import cast

import requests

from etf_sources.issuer_registry import IssuerCharacteristics, IssuerData
from models.instruments import EtfHolding

log = logging.getLogger(__name__)

API_BASE = "https://investor.vanguard.com/investment-products/etfs/profile/api"
USER_AGENT = "earnings-summary/1.0 (personal research; bhanumufcpraneeth@gmail.com)"
REQUEST_TIMEOUT = (10, 30)
MAX_PAGES = 120
SOURCE = "issuer:vanguard"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"] = "application/json"
    return s


def _get_json(session: requests.Session, url: str) -> object | None:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return cast("object", resp.json())
    except (requests.RequestException, ValueError) as exc:
        log.warning({"event": "vanguard_fetch_failed", "url": url, "error": str(exc)[:200]})
        return None


def _str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _float(v: object) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pct_to_decimal(v: object) -> float | None:
    f = _float(v)
    return f / 100.0 if f is not None else None


def parse_holdings_page(
    payload: object, ticker: str, fetched_at: datetime
) -> tuple[list[EtfHolding], date | None, str | None]:
    """One holdings page → (rows, as_of, next_url). Pure; fixture-tested."""
    if not isinstance(payload, dict):
        return [], None, None
    d = cast("dict[str, object]", payload)
    as_of: date | None = None
    as_of_raw = _str(d.get("asOfDate"))
    if as_of_raw:
        try:
            as_of = date.fromisoformat(as_of_raw[:10])
        except ValueError:
            as_of = None
    next_url = _str(d.get("next"))
    fund = d.get("fund")
    entities = cast("dict[str, object]", fund).get("entity") if isinstance(fund, dict) else None
    if not isinstance(entities, list):
        return [], as_of, next_url
    rows: list[EtfHolding] = []
    for raw in cast("list[object]", entities):
        if not isinstance(raw, dict):
            continue
        r = cast("dict[str, object]", raw)
        name = _str(r.get("longName")) or _str(r.get("shortName"))
        raw_ticker = _str(r.get("ticker"))
        if name is None and raw_ticker is None:
            continue
        rows.append(
            EtfHolding(
                ticker=ticker.upper(),
                as_of_date=as_of or fetched_at.date(),
                constituent_ticker=raw_ticker.upper()[:16] if raw_ticker else None,
                name=name,
                weight_pct=_pct_to_decimal(r.get("percentWeight")),
                shares_held=_float(r.get("sharesHeld")),
                market_value_usd=_float(r.get("marketValue")),
                sector=None,
                asset_class=None,
                rank_position=None,
                country=None,  # not published here; N-PORT supplies country
                source=SOURCE,
                fetched_at=fetched_at,
            )
        )
    return rows, as_of, next_url


def fetch(ticker: str) -> IssuerData | None:
    """Full holdings (paginated) + profile characteristics for one ETF."""
    upper = ticker.upper()
    fetched_at = datetime.now()
    session = _session()

    rows: list[EtfHolding] = []
    as_of: date | None = None
    url: str | None = f"{API_BASE}/{upper}/portfolio-holding/stock"
    for _ in range(MAX_PAGES):
        if url is None:
            break
        payload = _get_json(session, url)
        if payload is None:
            break
        page_rows, page_as_of, next_url = parse_holdings_page(payload, upper, fetched_at)
        rows.extend(page_rows)
        as_of = as_of or page_as_of
        if next_url == url:  # self-referential next would loop forever
            break
        url = next_url
    else:
        log.warning({"event": "vanguard_pagination_capped", "ticker": upper, "pages": MAX_PAGES})

    rows.sort(key=lambda h: (h.weight_pct is None, -(h.weight_pct or 0.0)))
    rows = [h.model_copy(update={"rank_position": i}) for i, h in enumerate(rows, start=1)]

    chars: IssuerCharacteristics | None = None
    profile_payload = _get_json(session, f"{API_BASE}/{upper}/profile")
    if isinstance(profile_payload, dict):
        fund_profile = cast("dict[str, object]", profile_payload).get("fundProfile")
        if isinstance(fund_profile, dict):
            fp = cast("dict[str, object]", fund_profile)
            chars = IssuerCharacteristics(
                source=SOURCE,
                as_of=as_of,
                name=_str(fp.get("longName")) or _str(fp.get("fundName")),
                issuer="Vanguard",
                expense_ratio=_pct_to_decimal(fp.get("expenseRatio")),
            )

    if not rows and chars is None:
        return None
    return IssuerData(source=SOURCE, holdings=rows, holdings_as_of=as_of, characteristics=chars)
