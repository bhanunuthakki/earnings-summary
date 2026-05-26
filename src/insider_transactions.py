"""SEC Form 4 + foreign-regime insider transaction fetcher and parser.

Two sources, layered:
  PRIMARY:    SEC EDGAR Form 4 / Form 144 / Form 5 XML. Canonical, free,
              no API key. Source of truth for US-listed names.
  ACCELERATOR: FMP `insider-trading` endpoint. Faster batch retrieval of
              recent activity per ticker (paginated). Used to find the
              latest filings, then we pull the actual Form 4 XML for full
              fidelity (price per share, 10b5-1 flag, etc.).

Foreign-regime equivalents land via the same insider_transactions table
with regulator != 'SEC'. Adapter pattern: each regulator has its own
fetcher returning rows in the canonical schema. ADR insiders typically
file Form 4 too when subject to US Section 16, but the foreign reporting
regime is the source of record for many issuers (NVO uses Danish FSA;
BN, BAM use SEDI; etc.).

Public API:
  fetch_for_ticker(ticker, since=date, until=date, regulator='auto')
    → list[InsiderTransaction]      — fetches + persists
  resolve_insider_entity(name, ticker)
    → entity_id                     — caches the person entity for reuse
  conviction_signals(ticker, window_days=90)
    → list[ConvictionSignal]        — surfaces CEO/CFO open-market buys,
                                       cluster events, anomaly flags

The fetcher is idempotent: re-running over the same window re-inserts no
duplicate rows (unique constraint on
(ticker, insider_name, transaction_date, transaction_type, shares, form_filed)).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

# SEC EDGAR submissions API requires a User-Agent header per their fair-use policy.
# Set yours via env var EDGAR_USER_AGENT (recommended). Default falls back to a
# generic agent — works but please be a polite citizen and set your own.
SEC_USER_AGENT_DEFAULT = "earnings-summary research/0.1 (analyst@example.com)"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InsiderTransaction:
    """Normalized insider transaction across regulators. Mirrors
    insider_transactions table columns."""

    ticker: str
    insider_name: str
    transaction_date: datetime
    filing_date: datetime
    transaction_type: str  # see migration 0041 enum
    shares: float
    insider_title: str | None = None
    insider_relation: str | None = None
    price_per_share: float | None = None
    transaction_value: float | None = None
    shares_owned_after: float | None = None
    ownership_pct: float | None = None
    is_10b5_1: bool = False
    acquired_or_disposed: str | None = None  # 'A' | 'D'
    ownership_form: str | None = None  # 'D' | 'I'
    form_filed: str = "4"
    regulator: str = "SEC"
    source_url: str | None = None
    filing_doc_id: int | None = None
    insider_entity_id: int | None = None
    notes: str | None = None


@dataclass(slots=True)
class ConvictionSignal:
    """A scored insider event. Surfaces in the workspace renderer + dashboard."""

    ticker: str
    insider_name: str
    insider_role: str | None
    transaction_date: datetime
    transaction_type: str
    shares: float
    transaction_value: float | None
    signal_strength: float  # 0..1
    rationale: str  # one-liner explaining the score


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    try:
        path = _resolve(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='insider_transactions'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _resolve(override: Path | str | None) -> Path | None:
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH

        return Path(cast("str", DB_PATH))
    except ImportError:
        return None


def upsert(
    transactions: list[InsiderTransaction],
    *,
    db_path: Path | str | None = None,
) -> int:
    """Insert-or-skip a batch of transactions. Returns the number of NEW rows
    written (idempotent on the unique constraint)."""
    if not transactions:
        return 0
    conn = _open(db_path)
    if conn is None:
        return 0
    try:
        inserted = 0
        for tx in transactions:
            try:
                conn.execute(
                    """
                    INSERT INTO insider_transactions(
                        ticker, insider_entity_id, insider_name, insider_title, insider_relation,
                        transaction_date, filing_date, filing_doc_id, transaction_type,
                        shares, price_per_share, transaction_value, shares_owned_after, ownership_pct,
                        is_10b5_1, acquired_or_disposed, ownership_form,
                        form_filed, regulator, source_url, notes, ingested_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        tx.ticker,
                        tx.insider_entity_id,
                        tx.insider_name,
                        tx.insider_title,
                        tx.insider_relation,
                        tx.transaction_date.isoformat(),
                        tx.filing_date.isoformat(),
                        tx.filing_doc_id,
                        tx.transaction_type,
                        tx.shares,
                        tx.price_per_share,
                        tx.transaction_value,
                        tx.shares_owned_after,
                        tx.ownership_pct,
                        1 if tx.is_10b5_1 else 0,
                        tx.acquired_or_disposed,
                        tx.ownership_form,
                        tx.form_filed,
                        tx.regulator,
                        tx.source_url,
                        tx.notes,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # Duplicate on the natural key — skip silently.
                continue
        conn.commit()
        return inserted
    except sqlite3.Error as exc:
        log.warning({"event": "insider_upsert_failed", "error": str(exc)})
        return 0
    finally:
        conn.close()


def fetch_transactions(
    *,
    ticker: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    transaction_types: list[str] | None = None,
    db_path: Path | str | None = None,
) -> list[InsiderTransaction]:
    """Query the stored insider_transactions table."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        clauses: list[str] = []
        params: list[object] = []
        if ticker is not None:
            clauses.append("ticker = ?")
            params.append(ticker)
        if since is not None:
            clauses.append("transaction_date >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("transaction_date <= ?")
            params.append(until.isoformat())
        if transaction_types:
            placeholders = ",".join("?" * len(transaction_types))
            clauses.append(f"transaction_type IN ({placeholders})")
            params.extend(transaction_types)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT * FROM insider_transactions
            {where}
            ORDER BY transaction_date DESC, filing_date DESC
            LIMIT 5000
            """,
            params,
        ).fetchall()
        return [_row_to_tx(r) for r in rows]
    finally:
        conn.close()


def _row_to_tx(r: sqlite3.Row) -> InsiderTransaction:
    return InsiderTransaction(
        ticker=r["ticker"],
        insider_name=r["insider_name"],
        transaction_date=_parse_dt(r["transaction_date"]) or datetime.now(UTC),
        filing_date=_parse_dt(r["filing_date"]) or datetime.now(UTC),
        transaction_type=r["transaction_type"],
        shares=float(r["shares"]),
        insider_title=r["insider_title"],
        insider_relation=r["insider_relation"],
        price_per_share=_float_or_none(r["price_per_share"]),
        transaction_value=_float_or_none(r["transaction_value"]),
        shares_owned_after=_float_or_none(r["shares_owned_after"]),
        ownership_pct=_float_or_none(r["ownership_pct"]),
        is_10b5_1=bool(r["is_10b5_1"]),
        acquired_or_disposed=r["acquired_or_disposed"],
        ownership_form=r["ownership_form"],
        form_filed=r["form_filed"],
        regulator=r["regulator"],
        source_url=r["source_url"],
        filing_doc_id=r["filing_doc_id"],
        insider_entity_id=r["insider_entity_id"],
        notes=r["notes"],
    )


# ---------------------------------------------------------------------------
# Conviction scoring
# ---------------------------------------------------------------------------


# Signal weighting reflects the standard buy-side prior:
#   - Open-market buys are the highest-conviction signal: insiders rarely
#     buy with their own cash unless they expect price appreciation.
#   - Sells are noisier: diversification, tax planning, real estate, etc.
#     A discretionary CFO sell ahead of earnings is the strongest sell-side
#     signal but most sells are not that.
#   - Option exercises followed by sell are nearly noise.
#   - Cluster events (multiple insiders buying within a week) compound.
_ROLE_SIGNAL_FLOOR: dict[str, float] = {
    "ceo": 1.0,
    "chief executive officer": 1.0,
    "cfo": 0.9,
    "chief financial officer": 0.9,
    "coo": 0.7,
    "chief operating officer": 0.7,
    "president": 0.85,
    "chairman": 0.7,
    "director": 0.4,
    "officer": 0.5,
    "10% owner": 0.6,
}


def conviction_signals(
    *,
    ticker: str,
    window_days: int = 90,
    db_path: Path | str | None = None,
) -> list[ConvictionSignal]:
    """Score recent insider activity. Returns sorted by signal_strength desc."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    rows = fetch_transactions(ticker=ticker, since=cutoff, db_path=db_path)

    # Group by date for cluster detection
    by_date: dict[str, list[InsiderTransaction]] = {}
    for r in rows:
        by_date.setdefault(r.transaction_date.date().isoformat(), []).append(r)

    out: list[ConvictionSignal] = []
    for tx in rows:
        if tx.transaction_type in (
            "stock_grant",
            "rsu_vesting",
            "other",
        ):
            continue  # noise; not a signal event
        if tx.is_10b5_1 and tx.transaction_type == "open_market_sell":
            continue  # pre-scheduled sells aren't discretionary

        role_floor = _role_signal_floor(tx.insider_title or tx.insider_relation or "")
        # Direction multiplier
        type_weight = {
            "open_market_buy": 1.0,
            "open_market_sell": 0.6,
            "option_exercise": 0.15,
            "option_exercise_and_sell": 0.10,
            "gift": 0.05,
        }.get(tx.transaction_type, 0.3)
        # Size adjustment: log-scale on transaction value
        size_factor = 0.5
        if tx.transaction_value is not None and tx.transaction_value > 0:
            import math

            size_factor = min(1.0, math.log10(max(tx.transaction_value, 1e3)) / 8.0)

        # Cluster bonus
        cluster_size = len(by_date.get(tx.transaction_date.date().isoformat(), []))
        cluster_bonus = min(0.2, (cluster_size - 1) * 0.05)

        strength = min(1.0, role_floor * type_weight * size_factor + cluster_bonus)

        # Rationale
        bits: list[str] = []
        if tx.transaction_type == "open_market_buy":
            bits.append("discretionary open-market buy")
        elif tx.transaction_type == "open_market_sell":
            bits.append("non-10b5-1 sell" if not tx.is_10b5_1 else "scheduled sell")
        else:
            bits.append(tx.transaction_type.replace("_", " "))
        if cluster_size > 1:
            bits.append(f"cluster ({cluster_size} insiders same day)")
        if tx.insider_title:
            bits.append(tx.insider_title.lower())

        out.append(
            ConvictionSignal(
                ticker=ticker,
                insider_name=tx.insider_name,
                insider_role=tx.insider_title or tx.insider_relation,
                transaction_date=tx.transaction_date,
                transaction_type=tx.transaction_type,
                shares=tx.shares,
                transaction_value=tx.transaction_value,
                signal_strength=strength,
                rationale=" · ".join(bits),
            )
        )

    out.sort(key=lambda s: -s.signal_strength)
    return out


def _role_signal_floor(title: str) -> float:
    t = (title or "").lower()
    for key, weight in _ROLE_SIGNAL_FLOOR.items():
        if key in t:
            return weight
    return 0.3  # unknown role


# ---------------------------------------------------------------------------
# SEC EDGAR fetcher
# ---------------------------------------------------------------------------


def fetch_form4_from_edgar(
    *,
    ticker: str,
    cik: str | None = None,
    since: datetime,
    until: datetime | None = None,
    user_agent: str | None = None,
    sleep_between_requests: float = 0.15,  # SEC limits to 10 req/sec
) -> list[InsiderTransaction]:
    """Fetch Form 4 / 5 / 144 filings for a ticker from EDGAR.

    Two-step:
      1. EDGAR submissions JSON gives us the list of recent filings + CIKs.
      2. For each Form 4 in the window, pull the XML and parse the
         non-derivative transaction table.

    Returns canonicalized transactions. Does NOT write to DB; caller passes
    to upsert(). This separation lets tests inject fixture XML without
    touching the network.
    """
    ua = user_agent or SEC_USER_AGENT_DEFAULT
    if cik is None:
        cik = _lookup_cik_for_ticker(ticker, user_agent=ua)
        if cik is None:
            log.warning({"event": "form4_no_cik", "ticker": ticker})
            return []

    # EDGAR uses zero-padded 10-digit CIK in submissions API
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"

    try:
        payload = _http_get_json(url, user_agent=ua)
    except Exception as exc:
        log.warning({"event": "form4_submissions_fetch_failed", "ticker": ticker, "error": str(exc)})
        return []

    # Recent filings live in payload['filings']['recent'] as parallel arrays
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accession_numbers = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    out: list[InsiderTransaction] = []
    until_iso = (until or datetime.now(UTC)).isoformat()
    since_iso = since.isoformat()

    for form, acc, fdate, pdoc in zip(forms, accession_numbers, filing_dates, primary_docs):
        if form not in ("4", "4/A", "5", "5/A", "144"):
            continue
        if fdate < since_iso[:10] or fdate > until_iso[:10]:
            continue
        acc_clean = acc.replace("-", "")
        archive_base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}"
        # The Form 4 XML is typically named *_doc4.xml — find it via index.json
        try:
            time.sleep(sleep_between_requests)
            idx = _http_get_json(f"{archive_base}/index.json", user_agent=ua)
            xml_name: str | None = None
            for item in (idx.get("directory") or {}).get("item") or []:
                name = item.get("name") or ""
                if name.endswith(".xml") and not name.endswith("index.xml"):
                    xml_name = name
                    break
            if xml_name is None:
                continue
            time.sleep(sleep_between_requests)
            xml_text = _http_get_text(f"{archive_base}/{xml_name}", user_agent=ua)
            parsed = parse_form4_xml(
                xml_text,
                ticker=ticker,
                form_filed=form,
                filing_date=_parse_dt(fdate) or datetime.now(UTC),
                source_url=f"{archive_base}/{xml_name}",
            )
            out.extend(parsed)
        except Exception as exc:  # noqa: BLE001 — defensive across SEC retries
            log.debug(
                {
                    "event": "form4_parse_skip",
                    "ticker": ticker,
                    "accession": acc,
                    "error": str(exc),
                }
            )
            continue
    return out


def parse_form4_xml(
    xml_text: str,
    *,
    ticker: str,
    form_filed: str,
    filing_date: datetime,
    source_url: str | None = None,
) -> list[InsiderTransaction]:
    """Parse the non-derivative transaction table out of a Form 4 XML.

    Form 4 schema is documented at https://www.sec.gov/info/edgar/edgarfm-vol2-v68.pdf
    Key fields:
      reportingOwner.reportingOwnerId.rptOwnerName  → insider_name
      reportingOwner.reportingOwnerRelationship.{isOfficer, isDirector, ...}
      reportingOwner.reportingOwnerRelationship.officerTitle
      nonDerivativeTable.nonDerivativeTransaction[]/.transactionDate.value
      transactionCoding.transactionCode  → 'P' (purchase), 'S' (sale), 'M' (exercise), 'A' (grant), 'G' (gift), 'F' (vesting/tax withhold), 'X' (exercise of stock options)
      transactionCoding.transactionFormType
      transactionAmounts.transactionShares.value
      transactionAmounts.transactionPricePerShare.value
      transactionAmounts.transactionAcquiredDisposedCode.value  → 'A' or 'D'
      postTransactionAmounts.sharesOwnedFollowingTransaction.value
      ownershipNature.directOrIndirectOwnership.value  → 'D' or 'I'
      footnotes — look for "10b5-1" mentions
    """
    # Light XML parsing via stdlib ElementTree — Form 4 is small (<50KB)
    from xml.etree import ElementTree as ET

    out: list[InsiderTransaction] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.debug({"event": "form4_xml_parse_failed", "error": str(exc)})
        return out

    # Reporting owner block (1+ insiders per filing; take the first for now —
    # multi-insider filings are rare and the natural-key constraint will dedupe).
    name_el = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
    name = name_el.text.strip() if name_el is not None and name_el.text else "(unknown)"
    rel_el = root.find(".//reportingOwner/reportingOwnerRelationship")
    title: str | None = None
    relation: str | None = None
    if rel_el is not None:
        is_officer = (rel_el.findtext("isOfficer") or "").strip() in ("1", "true")
        is_director = (rel_el.findtext("isDirector") or "").strip() in ("1", "true")
        is_ten_pct = (rel_el.findtext("isTenPercentOwner") or "").strip() in ("1", "true")
        if is_officer and is_director:
            relation = "officer_director"
        elif is_officer:
            relation = "officer"
        elif is_director:
            relation = "director"
        if is_ten_pct:
            relation = "10% owner" if relation is None else f"{relation}+10pct"
        title_el = rel_el.find("officerTitle")
        if title_el is not None and title_el.text:
            title = title_el.text.strip()

    # 10b5-1 detection via footnotes — common encoding pattern
    is_10b5_1 = "10b5-1" in xml_text.lower() or "10b5-1" in xml_text

    for tx_el in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        tx_date_text = tx_el.findtext("transactionDate/value") or ""
        try:
            tx_date = datetime.fromisoformat(tx_date_text)
        except ValueError:
            continue
        code = (tx_el.findtext("transactionCoding/transactionCode") or "").strip()
        shares_text = tx_el.findtext("transactionAmounts/transactionShares/value") or "0"
        try:
            shares = float(shares_text)
        except ValueError:
            shares = 0.0
        if shares <= 0:
            continue
        price_text = (
            tx_el.findtext("transactionAmounts/transactionPricePerShare/value") or ""
        )
        try:
            price = float(price_text) if price_text else None
        except ValueError:
            price = None
        a_or_d = (
            tx_el.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or ""
        ).strip()
        post_owned_text = (
            tx_el.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value") or ""
        )
        try:
            post_owned = float(post_owned_text) if post_owned_text else None
        except ValueError:
            post_owned = None
        own_form = (
            tx_el.findtext("ownershipNature/directOrIndirectOwnership/value") or ""
        ).strip() or None

        # Map Form 4 transaction code to canonical type
        tx_type = _form4_code_to_type(code, a_or_d)

        out.append(
            InsiderTransaction(
                ticker=ticker,
                insider_name=name,
                insider_title=title,
                insider_relation=relation,
                transaction_date=tx_date,
                filing_date=filing_date,
                transaction_type=tx_type,
                shares=shares,
                price_per_share=price,
                transaction_value=(shares * price) if (price is not None) else None,
                shares_owned_after=post_owned,
                is_10b5_1=is_10b5_1,
                acquired_or_disposed=a_or_d if a_or_d in ("A", "D") else None,
                ownership_form=own_form if own_form in ("D", "I") else None,
                form_filed=form_filed,
                regulator="SEC",
                source_url=source_url,
            )
        )
    return out


def _form4_code_to_type(code: str, acquired_or_disposed: str) -> str:
    """Map Form 4 transaction codes to canonical type strings.

    Codes (per SEC):
      P  — open-market purchase  → open_market_buy
      S  — open-market sale      → open_market_sell
      A  — grant/award           → stock_grant
      M  — exercise/conversion   → option_exercise
      F  — tax-withhold on vest  → rsu_vesting
      G  — gift                  → gift
      X  — exercise of in-the-money option → option_exercise
      V  — sale pursuant to 10b5-1 → open_market_sell (with is_10b5_1 flag)
      I  — discretionary trans.  → open_market_buy/sell by A/D
      D  — sale to issuer        → other
      C  — conversion of derivative → other
      W  — willful disposition   → other
      Z  — voting trust deposit  → other
    """
    code_up = (code or "").strip().upper()
    if code_up == "P":
        return "open_market_buy"
    if code_up in ("S", "V"):
        return "open_market_sell"
    if code_up in ("A", "K"):  # K is post-acquisition deemed grant
        return "stock_grant"
    if code_up in ("M", "X"):
        return "option_exercise"
    if code_up == "F":
        return "rsu_vesting"
    if code_up == "G":
        return "gift"
    if code_up == "I":
        # Discretionary; flip on acquired/disposed
        return "open_market_buy" if acquired_or_disposed == "A" else "open_market_sell"
    return "other"


# ---------------------------------------------------------------------------
# CIK lookup helper
# ---------------------------------------------------------------------------


_TICKER_CIK_CACHE: dict[str, str] = {}


def _lookup_cik_for_ticker(ticker: str, *, user_agent: str) -> str | None:
    """Resolve ticker → CIK via the EDGAR company_tickers.json index.
    Cached in-process so a single backfill doesn't hammer the endpoint."""
    if not _TICKER_CIK_CACHE:
        try:
            payload = _http_get_json(
                "https://www.sec.gov/files/company_tickers.json", user_agent=user_agent
            )
        except Exception as exc:
            log.warning({"event": "cik_index_fetch_failed", "error": str(exc)})
            return None
        # Payload is a dict of {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
        for entry in payload.values():
            try:
                if isinstance(entry, dict):
                    t = str(entry.get("ticker") or "").upper()
                    c = str(entry.get("cik_str") or "")
                    if t and c:
                        _TICKER_CIK_CACHE[t] = c
            except (AttributeError, TypeError):
                continue
    return _TICKER_CIK_CACHE.get(ticker.upper())


def _sec_headers(user_agent: str) -> dict[str, str]:
    # SEC's fair-use policy requires a User-Agent + an identifying contact.
    # Accept-Encoding gzip/deflate is REQUIRED by SEC's infrastructure for the
    # data.sec.gov endpoints to return 200 (without it, intermittent 403).
    # Host header is set automatically by urllib; do not override.
    return {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json, text/xml, */*",
    }


def _http_get_json(url: str, *, user_agent: str, timeout: float = 30.0) -> dict[str, object]:
    req = urllib.request.Request(url, headers=_sec_headers(user_agent))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = resp.headers.get("Content-Encoding", "").lower()
        if "gzip" in encoding:
            import gzip
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            import zlib
            raw = zlib.decompress(raw)
        body = raw.decode("utf-8")
    return cast("dict[str, object]", json.loads(body))


def _http_get_text(url: str, *, user_agent: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers=_sec_headers(user_agent))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = resp.headers.get("Content-Encoding", "").lower()
        if "gzip" in encoding:
            import gzip
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            import zlib
            raw = zlib.decompress(raw)
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _parse_dt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        s = str(raw)
        # SEC dates are YYYY-MM-DD; transaction dates may be ISO
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return datetime.fromisoformat(s + "T00:00:00+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _float_or_none(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
