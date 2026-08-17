"""Foreign Private Issuer (FPI) SEC Ingestion Pipeline for Form 6-K and Form 20-F.

Handles automated discovery, retrieval, document registration, evidence ledger
anchoring, table/narrative extraction, and fact persistence for FPI filers
(e.g., WIX, NU, NVO, ASML) who file 6-Ks quarterly and 20-Fs annually.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import requests
from bs4 import BeautifulSoup

from log_redact import redact as _redact
from models.documents import SourceType
from models.facts import FactLocator, Unit
from models.kpis import DefinitionOrigin
from pipeline import locators
from pipeline.kpi_persistence import (
    KpiValue,
    find_or_create_kpi_definition,
)
from pipeline.sec_xbrl import CIK_MAP
from provenance.evidence_backfill import ensure_legacy_document_evidence
from sec_identity import sec_user_agent
from table_extractors.period_axis import NominalQuarter, expected_period_ends

log = logging.getLogger(__name__)

USER_AGENT = sec_user_agent()
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/"
_TIMEOUT = (10, 45)
_DELAY_S = 0.25
_FILING_WINDOW_DAYS = (10, 110)

_MIN_TEXT_DENSITY = 0.02
_MIN_TEXT_CHARS = 800

# Per-ticker exhibit regex heuristics for quarterly 6-K earnings releases
_TICKER_EXHIBIT_HINT: dict[str, re.Pattern[str]] = {
    "NU": re.compile(r"^nufs\d.*_6k\.htm$", re.IGNORECASE),
    "NVO": re.compile(r"^caq?\d[\dA-Za-z]*\.htm$", re.IGNORECASE),
    "WIX": re.compile(r"^(?:first|second|third|fourth)quarter", re.IGNORECASE),
    "ASML": re.compile(r"financialstatements|pressrelease", re.IGNORECASE),
}

# Fallback keywords for exhibit discovery when primary hint does not match
_FALLBACK_EXHIBIT_KEYWORDS = [
    "results",
    "earnings",
    "financial statements",
    "press release",
    "quarterly report",
    "interim report",
]


@dataclass(slots=True)
class LocatedFpiExhibit:
    ticker: str
    cik: str
    form_type: str
    accession: str
    filing_date: str
    exhibit_filename: str
    exhibit_url: str
    description: str = ""


@dataclass(slots=True)
class FetchedFpiExhibit:
    located: LocatedFpiExhibit
    raw_html: str
    plain_text: str
    is_image_only: bool
    sha256: str


@dataclass(slots=True)
class FpiIngestResult:
    ticker: str
    form_type: str
    accession: str
    filing_date: str
    document_id: int
    facts_inserted: int
    kpis_inserted: int
    status: Literal["ok", "skipped", "failed", "image_only"]
    error_message: str | None = None


def resolve_cik(ticker: str) -> str | None:
    """Resolve ticker to zero-padded 10-digit CIK string."""
    return CIK_MAP.get(ticker.upper())


def _fetch_json(url: str, session: requests.Session) -> object | None:
    try:
        r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _candidate_filings(
    cik: str, form_filter: tuple[str, ...], session: requests.Session
) -> list[tuple[str, str, str]]:
    """List [(accession, filingDate, form)] for matching forms from SEC submissions API."""
    payload = _fetch_json(_SUBMISSIONS_URL.format(cik=cik), session)
    if not isinstance(payload, dict):
        return []
    filings = cast("dict[str, object]", payload).get("filings")
    if not isinstance(filings, dict):
        return []
    recent = cast("dict[str, object]", filings).get("recent")
    if not isinstance(recent, dict):
        return []
    recent_obj = cast("dict[str, object]", recent)
    forms = recent_obj.get("form")
    dates = recent_obj.get("filingDate")
    accns = recent_obj.get("accessionNumber")
    if not (isinstance(forms, list) and isinstance(dates, list) and isinstance(accns, list)):
        return []
    forms_l = cast("list[object]", forms)
    dates_l = cast("list[object]", dates)
    accns_l = cast("list[object]", accns)
    out: list[tuple[str, str, str]] = []
    upper_filter = tuple(f.upper() for f in form_filter)
    for i in range(min(len(forms_l), len(dates_l), len(accns_l))):
        f = str(forms_l[i]).upper()
        if f in upper_filter:
            out.append((str(accns_l[i]), str(dates_l[i]), str(forms_l[i])))
    return out


def locate_fpi_exhibit(
    ticker: str,
    *,
    quarter: NominalQuarter | None = None,
    year: int,
    form: str = "6-K",
    fye_month: int = 12,
    fye_day: int = 31,
    session: requests.Session | None = None,
) -> LocatedFpiExhibit | None:
    """Locate primary financial statements or earnings release exhibit for an FPI."""
    ticker = ticker.upper()
    cik = resolve_cik(ticker)
    if cik is None:
        log.warning(f"No CIK found for ticker {ticker}")
        return None

    sess = session or requests.Session()
    form_filter = ("6-K", "6-K/A") if form.upper() in ("6-K", "6-K/A") else ("20-F", "20-F/A")
    candidates = _candidate_filings(cik, form_filter, sess)

    window_start = None
    window_end = None
    if quarter is not None:
        current_end, _prior = expected_period_ends(quarter, year, fye_month, fye_day)
        window_start = current_end.toordinal() + _FILING_WINDOW_DAYS[0]
        window_end = current_end.toordinal() + _FILING_WINDOW_DAYS[1]

    hint = _TICKER_EXHIBIT_HINT.get(ticker)

    for accn, filing_date, form_type in candidates:
        if window_start is not None and window_end is not None:
            try:
                fdate = date.fromisoformat(filing_date)
                if not (window_start <= fdate.toordinal() <= window_end):
                    continue
            except ValueError:
                continue

        time.sleep(_DELAY_S)
        accn_nodash = accn.replace("-", "")
        index_url = _ARCHIVES_BASE.format(cik_int=int(cik), accn_nodash=accn_nodash) + "index.json"
        index_payload = _fetch_json(index_url, sess)
        if not isinstance(index_payload, dict):
            continue
        directory = cast("dict[str, object]", index_payload).get("directory")
        if not isinstance(directory, dict):
            continue
        items = cast("dict[str, object]", directory).get("item")
        if not isinstance(items, list):
            continue

        # Strategy 1: Match with ticker exhibit hint regex
        if hint is not None:
            for item in cast("list[object]", items):
                if not isinstance(item, dict):
                    continue
                name = cast("dict[str, object]", item).get("name")
                desc = str(cast("dict[str, object]", item).get("description", ""))
                if isinstance(name, str) and hint.search(name):
                    exhibit_url = (
                        _ARCHIVES_BASE.format(cik_int=int(cik), accn_nodash=accn_nodash) + name
                    )
                    return LocatedFpiExhibit(
                        ticker=ticker,
                        cik=cik,
                        form_type=form_type,
                        accession=accn,
                        filing_date=filing_date,
                        exhibit_filename=name,
                        exhibit_url=exhibit_url,
                        description=desc,
                    )

        # Strategy 2: Fallback search on item descriptions or keywords
        for item in cast("list[object]", items):
            if not isinstance(item, dict):
                continue
            name = cast("dict[str, object]", item).get("name")
            desc = str(cast("dict[str, object]", item).get("description", "")).lower()
            if not isinstance(name, str) or not (name.endswith(".htm") or name.endswith(".html")):
                continue
            name_lower = name.lower()
            if any(kw in desc or kw in name_lower for kw in _FALLBACK_EXHIBIT_KEYWORDS):
                exhibit_url = (
                    _ARCHIVES_BASE.format(cik_int=int(cik), accn_nodash=accn_nodash) + name
                )
                return LocatedFpiExhibit(
                    ticker=ticker,
                    cik=cik,
                    form_type=form_type,
                    accession=accn,
                    filing_date=filing_date,
                    exhibit_filename=name,
                    exhibit_url=exhibit_url,
                    description=desc,
                )

    return None


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_WS_RE = re.compile(r"&nbsp;|&#\d+;")
_WS_RE = re.compile(r"\s+")


def _strip_html(raw_html: str) -> str:
    plain = _TAG_RE.sub(" ", raw_html)
    plain = _ENTITY_WS_RE.sub(" ", plain)
    return _WS_RE.sub(" ", plain).strip()


def fetch_fpi_exhibit(
    located: LocatedFpiExhibit, *, session: requests.Session | None = None
) -> FetchedFpiExhibit | None:
    """Download the exhibit HTML and compute content statistics."""
    sess = session or requests.Session()
    try:
        r = sess.get(located.exhibit_url, headers={"User-Agent": USER_AGENT}, timeout=_TIMEOUT)
    except requests.RequestException as e:
        sys.stderr.write(f"  FAIL fetch {located.ticker} {located.exhibit_url}: {_redact(e)}\n")
        return None
    if r.status_code != 200:
        return None

    raw_html = r.text
    plain_text = _strip_html(raw_html)
    density = len(plain_text) / max(len(raw_html), 1)
    is_image_only = len(plain_text) < _MIN_TEXT_CHARS or density < _MIN_TEXT_DENSITY
    sha256 = hashlib.sha256(raw_html.encode("utf-8")).hexdigest()

    return FetchedFpiExhibit(
        located=located,
        raw_html=raw_html,
        plain_text=plain_text,
        is_image_only=is_image_only,
        sha256=sha256,
    )


def register_and_anchor_fpi_document(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fetched: FetchedFpiExhibit,
    period_end: datetime,
    repo_root: Path,
) -> int:
    """Persist exhibit HTML, upsert documents row, and anchor in immutable evidence ledger."""
    out_dir = repo_root / "data" / "historical" / "sec"
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_form = "20f" if "20-F" in fetched.located.form_type.upper() else "6k"
    doc_type_val = "sec_20f" if doc_form == "20f" else "sec_6k"

    out_path = out_dir / f"{ticker.upper()}_{doc_form}_{fetched.located.filing_date}.html"
    out_path.write_bytes(fetched.raw_html.encode("utf-8"))
    rel_path = str(out_path.relative_to(repo_root)).replace("\\", "/")


    existing = conn.execute(
        "SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (fetched.sha256,)
    ).fetchone()

    if existing is not None:
        doc_id = int(existing[0])
    else:
        cur = conn.execute(
            """
            INSERT INTO documents 
            (ticker, source_type, doc_type, period_start, period_end, file_path, 
             sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, 
             parent_document_id, accession_number, filing_date) 
            VALUES (?, 'sec_xbrl', ?, NULL, ?, ?, ?, ?, 'ok', 200, ?, ?, NULL, ?, ?)
            """,
            (
                ticker.upper(),
                doc_type_val,
                period_end,
                rel_path,
                fetched.sha256,
                datetime.now(UTC),
                len(fetched.raw_html.encode("utf-8")),
                fetched.located.exhibit_url,
                fetched.located.accession,
                fetched.located.filing_date,
            ),
        )
        doc_id = int(cur.lastrowid) if cur.lastrowid is not None else 0

    # Anchor into evidence ledger
    ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=doc_id)
    return doc_id


def _parse_numeric(val_str: str) -> Decimal | None:
    """Parse string formatted currency/number into Decimal."""
    cleaned = val_str.replace("$", "").replace(",", "").replace("%", "").strip()
    if not cleaned:
        return None
    is_neg = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        is_neg = True
        cleaned = cleaned[1:-1].strip()
    elif cleaned.startswith("-"):
        is_neg = True
        cleaned = cleaned[1:].strip()
    try:
        val = Decimal(cleaned)
        return -val if is_neg else val
    except Exception:
        return None


def extract_fpi_financial_facts_html(
    html_text: str, ticker: str, period_end: datetime, fiscal_period_type: str
) -> dict[str, tuple[Decimal, str, str]]:
    """Extract standard GAAP/IFRS line items from 6-K or 20-F HTML tables.

    Returns dict mapping line_item -> (Decimal value in whole currency, unit, locator/excerpt).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    results: dict[str, tuple[Decimal, str, str]] = {}

    for table in tables:
        # Scale detection (check table text and preceding headers)
        t_text = table.get_text().lower()
        prev_context = " ".join([h.get_text().lower() for h in table.find_all_previous(["h1", "h2", "h3", "h4", "h5", "p", "div"], limit=3)])
        context_text = f"{t_text} {prev_context}"
        scale = Decimal(1)
        if "in thousands" in context_text or "(in thousands" in context_text or "in thousand" in context_text:
            scale = Decimal("1000")
        elif "in millions" in context_text or "(in millions" in context_text:
            scale = Decimal("1000000")



        # Line item matching patterns
        rows = table.find_all("tr")
        for r in rows:
            cells = [c.get_text().strip() for c in r.find_all(["td", "th"]) if c.get_text().strip()]
            if len(cells) < 2:
                continue
            label = cells[0].lower()
            nums = []
            for c in cells[1:]:
                num = _parse_numeric(c)
                if num is not None:
                    nums.append((num, c))

            if not nums:
                continue
            first_val, raw_str = nums[0]

            # Revenues
            if (
                ("total revenue" in label or label == "revenues" or label == "revenue")
                and "revenue" not in results
            ):
                results["revenue"] = (
                    first_val * scale,
                    "USD",
                    f"Table: {cells[0]} = {raw_str}",
                )
            elif "gross profit" in label and "gross_profit" not in results:
                results["gross_profit"] = (
                    first_val * scale,
                    "USD",
                    f"Table: {cells[0]} = {raw_str}",
                )
            elif (
                "operating income" in label or "operating profit" in label or "operating (loss) income" in label
            ) and "operating_income" not in results:
                results["operating_income"] = (
                    first_val * scale,
                    "USD",
                    f"Table: {cells[0]} = {raw_str}",
                )
            elif (
                "net income" in label or "net loss" in label or "net (loss) income" in label
            ) and "net_income" not in results:
                results["net_income"] = (
                    first_val * scale,
                    "USD",
                    f"Table: {cells[0]} = {raw_str}",
                )
            elif (
                "net cash provided by operating activities" in label
                or "operating cash flow" in label
                or "cash flows from operating activities" in label
            ) and "operating_cash_flow" not in results:
                results["operating_cash_flow"] = (
                    first_val * scale,
                    "USD",
                    f"Table: {cells[0]} = {raw_str}",
                )
            elif (
                "capital expenditures" in label
                or "purchase of property and equipment" in label
                or "purchase of property, plant and equipment" in label
            ) and "capital_expenditure" not in results:
                # Store capex as negative per FMP/system convention
                results["capital_expenditure"] = (
                    -abs(first_val * scale),
                    "USD",
                    f"Table: {cells[0]} = {raw_str}",
                )

    # If both OCF and Capex exist, compute FCF
    if "operating_cash_flow" in results and "capital_expenditure" in results:
        ocf = results["operating_cash_flow"][0]
        capex = results["capital_expenditure"][0]
        fcf = ocf + capex  # capex is negative
        results["free_cash_flow"] = (
            fcf,
            "USD",
            f"Derived: OCF ({ocf}) + Capex ({capex})",
        )

    return results


def extract_fpi_kpis_narrative(
    html_text: str, plain_text: str, ticker: str
) -> list[tuple[str, Decimal, Unit, str]]:
    """Extract key metrics and KPIs mentioned in 6-K press releases."""
    kpis: list[tuple[str, Decimal, Unit, str]] = []

    # Bookings regex: e.g. "Total bookings in the second quarter of 2026 were $569.1 million"
    b_match = re.search(
        r"(?:total\s+)?bookings.*?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*million",
        plain_text,
        re.IGNORECASE,
    )
    if b_match:
        val = Decimal(b_match.group(1)) * Decimal("1000000")
        kpis.append(("bookings", val, Unit.ACTUAL, b_match.group(0)[:250]))

    # Free cash flow ex-restructuring or FCF margin
    fcf_m = re.search(
        r"free\s+cash\s+flow\s+margin.*?([0-9]+(?:\.[0-9]+)?)\s*%",
        plain_text,
        re.IGNORECASE,
    )
    if fcf_m:
        val = Decimal(fcf_m.group(1))
        kpis.append(("free_cash_flow_margin", val, Unit.PERCENT, fcf_m.group(0)[:250]))

    # Creative subscriptions revenue / bookings for Wix
    if ticker.upper() == "WIX":
        cs_rev = re.search(
            r"Creative\s+Subscriptions\s+revenue.*?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*million",
            plain_text,
            re.IGNORECASE,
        )
        if cs_rev:
            val = Decimal(cs_rev.group(1)) * Decimal("1000000")
            kpis.append(
                (
                    "creative_subscriptions_revenue",
                    val,
                    Unit.ACTUAL,
                    cs_rev.group(0)[:250],
                )
            )

        cs_book = re.search(
            r"Creative\s+Subscriptions\s+bookings.*?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*million",
            plain_text,
            re.IGNORECASE,
        )
        if cs_book:
            val = Decimal(cs_book.group(1)) * Decimal("1000000")
            kpis.append(
                (
                    "creative_subscriptions_bookings",
                    val,
                    Unit.ACTUAL,
                    cs_book.group(0)[:250],
                )
            )

    return kpis


def persist_fpi_facts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    doc_id: int,
    financial_facts: dict[str, tuple[Decimal, str, str]],
    kpis: list[tuple[str, Decimal, Unit, str]],
    force: bool = False,
) -> tuple[int, int]:
    """Persist financial_facts and kpi_facts with transactional replacement when force=True."""
    ff_inserted = 0
    kpi_inserted = 0

    # 1. Insert financial facts
    for line_item, (val, currency_str, _excerpt) in financial_facts.items():
        loc = FactLocator(line_item=line_item, section_name="consolidated_financial_statements")
        loc_str = loc.model_dump_json() if hasattr(loc, "model_dump_json") else json.dumps({"line_item": line_item})
        conn.execute(
            """
            INSERT INTO financial_facts
            (ticker, period_end, fiscal_period_type, line_item, value, unit, currency,
             source_doc_id, locator, confidence, extracted_by)
            VALUES (?, ?, ?, ?, ?, 'actual', ?, ?, ?, 0.98, 'sec_fpi_ingest@1')
            ON CONFLICT (ticker, period_end, fiscal_period_type, line_item, source_doc_id) DO UPDATE
            SET value = excluded.value, locator = excluded.locator
            """,
            (
                ticker.upper(),
                period_end,
                fiscal_period_type.upper(),
                line_item,
                float(val),
                currency_str,
                doc_id,
                loc_str,
            ),
        )
        ff_inserted += 1

    # 2. Insert KPI facts using manifest
    if kpis:
        kpi_values = []
        for name, val, unit_enum, excerpt in kpis:
            kpi_values.append(
                KpiValue(
                    name=name,
                    value=val,
                    unit=unit_enum,
                    confidence=0.95,
                    source_excerpt=excerpt,
                    locator=FactLocator(section_name="press_release", line_item=name),
                )
            )



        for kpv in kpi_values:
            kpi_def_id = find_or_create_kpi_definition(
                conn,
                ticker=ticker.upper(),
                name=kpv.name,
                unit=kpv.unit,
                primary_source=SourceType.SEC_XBRL,
                origin=DefinitionOrigin.CAPTURE,
            )

            loc_str = locators.resolve_locator_for_persist(
                conn,
                locator=kpv.locator,
                run_id="sec_fpi_ingest",
                source_doc_id=doc_id,
                ticker=ticker.upper(),
            )

            conn.execute(
                """
                INSERT INTO kpi_facts
                (kpi_definition_id, ticker, period_end, fiscal_period_type, value, unit,
                 source_doc_id, locator, source_excerpt, confidence, extracted_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_fpi_ingest@1')
                ON CONFLICT (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id) DO UPDATE
                SET value = excluded.value, locator = excluded.locator, source_excerpt = excluded.source_excerpt
                """,
                (
                    kpi_def_id,
                    ticker.upper(),
                    period_end,
                    fiscal_period_type.upper(),
                    float(kpv.value),
                    kpv.unit.value,
                    doc_id,
                    loc_str,
                    kpv.source_excerpt,
                    kpv.confidence,
                ),
            )
            kpi_inserted += 1



    # Force-flip tracked_companies.brief_dirty=1
    conn.execute(
        "UPDATE tracked_companies SET brief_dirty = 1 WHERE ticker = ?",
        (ticker.upper(),),
    )

    return ff_inserted, kpi_inserted


def ingest_fpi_for_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    year: int,
    quarter: NominalQuarter | None = None,
    form: str = "6-K",
    repo_root: Path,
    force: bool = False,
    session: requests.Session | None = None,
) -> FpiIngestResult:
    """Execute end-to-end ingestion and extraction for one FPI ticker/period."""
    ticker = ticker.upper()
    located = locate_fpi_exhibit(
        ticker,
        quarter=quarter,
        year=year,
        form=form,
        session=session,
    )
    if located is None:
        return FpiIngestResult(
            ticker=ticker,
            form_type=form,
            accession="",
            filing_date="",
            document_id=0,
            facts_inserted=0,
            kpis_inserted=0,
            status="failed",
            error_message="Could not locate primary financial exhibit in SEC Submissions",
        )

    fetched = fetch_fpi_exhibit(located, session=session)
    if fetched is None:
        return FpiIngestResult(
            ticker=ticker,
            form_type=form,
            accession=located.accession,
            filing_date=located.filing_date,
            document_id=0,
            facts_inserted=0,
            kpis_inserted=0,
            status="failed",
            error_message=f"HTTP download failed for {located.exhibit_url}",
        )

    if fetched.is_image_only:
        return FpiIngestResult(
            ticker=ticker,
            form_type=form,
            accession=located.accession,
            filing_date=located.filing_date,
            document_id=0,
            facts_inserted=0,
            kpis_inserted=0,
            status="image_only",
            error_message="Exhibit text density below threshold (image-only slide deck)",
        )


    p_quarter = quarter or "FY"
    # Resolve company FYE
    fye_row = conn.execute(
        "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ?",
        (ticker.upper(),),
    ).fetchone()
    fye_str = fye_row[0] if fye_row and fye_row[0] else "12-31"
    try:
        parts = fye_str.split("-")
        fye_month, fye_day = int(parts[0]), int(parts[1])
    except Exception:
        fye_month, fye_day = 12, 31

    nom_q = cast("NominalQuarter", quarter if quarter in ("Q1", "Q2", "Q3", "Q4") else "Q4")
    current_end, _ = expected_period_ends(nom_q, year, fye_month=fye_month, fye_day=fye_day)
    period_end_dt = datetime(current_end.year, current_end.month, current_end.day, tzinfo=UTC)

    doc_id = register_and_anchor_fpi_document(
        conn,
        ticker=ticker,
        fetched=fetched,
        period_end=period_end_dt,
        repo_root=repo_root,
    )

    financial_facts = extract_fpi_financial_facts_html(
        fetched.raw_html, ticker, period_end_dt, p_quarter
    )
    kpis = extract_fpi_kpis_narrative(fetched.raw_html, fetched.plain_text, ticker)

    ff_n, kpi_n = persist_fpi_facts(
        conn,
        ticker=ticker,
        period_end=period_end_dt,
        fiscal_period_type=p_quarter,
        doc_id=doc_id,
        financial_facts=financial_facts,
        kpis=kpis,
        force=force,
    )

    return FpiIngestResult(
        ticker=ticker,
        form_type=form,
        accession=located.accession,
        filing_date=located.filing_date,
        document_id=doc_id,
        facts_inserted=ff_n,
        kpis_inserted=kpi_n,
        status="ok",
    )
