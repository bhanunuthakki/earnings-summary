"""Foreign Private Issuer (FPI) SEC Ingestion Pipeline for Form 6-K and Form 20-F.

Handles automated discovery, retrieval, document registration, evidence ledger
anchoring, table/narrative extraction, and fact persistence for FPI filers
(e.g., WIX, NU, NVO, ASML) who file 6-Ks quarterly and 20-Fs annually.
"""

from __future__ import annotations

import hashlib
import html
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
from bs4 import BeautifulSoup, Tag

from log_redact import redact as _redact
from models.documents import SourceType
from models.facts import Currency, FactLocator, Unit
from models.kpis import DefinitionOrigin
from pipeline import locators
from pipeline.kpi_atomic_admission import ensure_deterministic_kpi_semantic_context
from pipeline.kpi_persistence import (
    KpiValue,
    find_or_create_kpi_definition,
)
from pipeline.restatement_detector import (
    insert_kpi_with_restatement_detection,
    insert_with_restatement_detection,
)
from pipeline.sec_6k_fetch import register_sec_exhibit_snapshot
from pipeline.sec_xbrl import CIK_MAP
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
    "BN": re.compile(r"(?:interimreport|bn-\d{8})\.htm$", re.IGNORECASE),
    "NU": re.compile(r"^nufs\d.*_6k\.htm$", re.IGNORECASE),
    "NVO": re.compile(r"^caq?\d[\dA-Za-z]*\.htm$", re.IGNORECASE),
    "TSM": re.compile(r"^a20\d{2}q[1-4]consolidatedreport.*\.htm$", re.IGNORECASE),
    "WIX": re.compile(r"^(?:first|second|third|fourth)quarter", re.IGNORECASE),
    "ASML": re.compile(r"financialstatements|pressrelease", re.IGNORECASE),
}

# Per-ticker reporting currency fallback when a 6-K interim financial table omits
# an explicit currency code (e.g. Brookfield Corporation reports in USD with bare "$" symbols).
_ISSUER_FALLBACK_CURRENCY: dict[str, Currency] = {
    "BN": Currency.USD,
}

# Fallback keywords for exhibit discovery when primary hint does not match
_FALLBACK_EXHIBIT_KEYWORDS = [
    "interimreport",
    "consolidatedreport",
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
    upper_form = form.upper()
    if "6-K" in upper_form:
        form_filter: tuple[str, ...] = ("6-K", "6-K/A")
    elif "40-F" in upper_form:
        form_filter = ("40-F", "40-F/A")
    else:
        form_filter = ("20-F", "20-F/A")
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

        base_url = _ARCHIVES_BASE.format(cik_int=int(cik), accn_nodash=accn_nodash)
        item_dicts: list[dict[str, object]] = [
            cast("dict[str, object]", it)
            for it in cast("list[object]", items)
            if isinstance(it, dict)
        ]
        matched: tuple[str, str] | None = None
        if hint is not None:
            for item in item_dicts:
                name = item.get("name")
                if isinstance(name, str) and hint.search(name):
                    matched = (name, str(item.get("description", "")))
                    break
        if matched is None:
            for item in item_dicts:
                name = item.get("name")
                desc = str(item.get("description", "")).lower()
                if isinstance(name, str) and name.endswith((".htm", ".html")):
                    name_lower = name.lower()
                    if any(kw in desc or kw in name_lower for kw in _FALLBACK_EXHIBIT_KEYWORDS):
                        matched = (name, desc)
                        break
        if matched is not None:
            return LocatedFpiExhibit(
                ticker=ticker,
                cik=cik,
                form_type=form_type,
                accession=accn,
                filing_date=filing_date,
                exhibit_filename=matched[0],
                exhibit_url=base_url + matched[0],
                description=matched[1],
            )

    return None


_SCRIPT_STYLE_RE = re.compile(
    r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", re.IGNORECASE | re.DOTALL
)
_DATA_URI_RE = re.compile(r"data:[^\s\"'<>]+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(raw_html: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    plain = _TAG_RE.sub(" ", cleaned)
    plain = html.unescape(plain)
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
    plain_text = strip_html(raw_html)
    density_html = _DATA_URI_RE.sub("", raw_html)
    density = len(plain_text) / max(len(density_html), 1)
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
    """Retain exact UTF-8 serialization bytes with caller-owned evidence admission."""
    if hashlib.sha256(fetched.raw_html.encode("utf-8")).hexdigest() != fetched.sha256:
        raise ValueError("SEC FPI decoded HTML conflicts with its supplied digest")
    return register_sec_exhibit_snapshot(
        conn,
        ticker=ticker,
        raw_html=fetched.raw_html,
        repo_root=repo_root,
        period_end=period_end,
        doc_type="sec_20f" if "20-F" in fetched.located.form_type.upper() else "sec_6k",
        source_url=fetched.located.exhibit_url,
        accession=fetched.located.accession,
        filing_date=fetched.located.filing_date,
    )


_UNICODE_MINUS_RE = re.compile(r"[\u2212\u2013\u2014]")
_YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}$")
_NOTE_COL_RE = re.compile(r"^(?:notes?|refs?|references?)$", re.IGNORECASE)
_EXCLUDED_COL_RE = re.compile(
    r"(?:%|growth|change|\bat\s+cer\b|effect\s+of|adjusted|provisions?|impairments?|legal|ex\s*rate)",
    re.IGNORECASE,
)
_PERIOD_HDR_RE = re.compile(
    r"(?:\b20\d{2}\b|\bq[1-4]\b|\b(?:three|six|nine|twelve)[- ]months?\b|"
    r"\b(?:3|6|9|12)m\b|\bmonths?\s+ended\b|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_YTD_HDR_RE = re.compile(
    r"(?:\b(?:6|9)m\b|\b(?:six|nine)[- ]?months?(?:\s+periods?)?\b|\bytd\b|\bhalf[- ]?year\b|\bfirst\s+half\b)",
    re.IGNORECASE,
)
_STANDALONE_Q_HDR_RE = re.compile(
    r"(?:\b3m\b|\bthree[- ]?months?(?:\s+periods?)?\b|\bquarterly\b)",
    re.IGNORECASE,
)
_CURRENCY_ALIASES: tuple[tuple[str, Currency], ...] = (
    (r"\bUS\s*\$|\bU\.?S\.?\s+DOLLARS?\b|\bUS\s+DOLLARS?\b", Currency.USD),
    (r"\bDANISH\s+KRONER?\b", Currency.DKK),
    (r"\bBRAZILIAN\s+REAIS?\b", Currency.BRL),
    (r"\bEUROS?\b|€", Currency.EUR),
    (r"\bPOUNDS?\s+STERLING\b|£", Currency.GBP),
    (r"NT\$|\bNEW\s+TAIWAN\s+DOLLARS?\b", Currency.TWD),
    (r"\bCANADIAN\s+DOLLARS?\b", Currency.CAD),
    (r"\bSWISS\s+FRANCS?\b", Currency.CHF),
)


def _scale_re(word: str, *, extra: str = "") -> re.Pattern[str]:
    extra_alt = f"|{extra}" if extra else ""
    return re.compile(
        rf"(?:\bin\s+(?:[a-z]{{2,4}}\s+|us\$\s*|nt\$\s*|u\.?s\.?\s+dollars?\s+)?{word}s?\b|"
        rf"\b{word}s?\s+of\s+(?:[a-z.]+\s+){{0,3}}(?:dollars?|kroner|reais|euros?|pounds?|francs?)\b|"
        rf"\b(?:dkk|usd|eur|cad|twd|gbp|chf|brl|kr)\s+{word}s?\b|"
        rf"(?:€|£|\$|nt\$|us\$)\s+{word}s?\b|"
        rf"\(\s*(?:us\$?\s*)?{word}s?\b{extra_alt})",
        re.IGNORECASE,
    )


_SCALE_BILLIONS_RE = _scale_re("billion")
_SCALE_MILLIONS_RE = _scale_re("million")
_SCALE_THOUSANDS_RE = _scale_re("thousand", extra=r"\(000'?s?\)")
_PAIRED_CURR_ALT = (
    r"\b(?:USD|EUR|GBP|JPY|DKK|BRL|CAD|INR|AUD|KRW|CHF|TWD)\b"
    r"|\bUS\s*\$|\bU\.?S\.?\s+DOLLARS?\b|\bUS\s+DOLLARS?\b"
    r"|NT\$|\bNEW\s+TAIWAN\s+DOLLARS?\b|\bDANISH\s+KRONER?\b"
    r"|\bCANADIAN\s+DOLLARS?\b|\bSWISS\s+FRANCS?\b|\bBRAZILIAN\s+REAIS?\b"
    r"|\bEUROS?\b|€|\bPOUNDS?\s+STERLING\b|£"
)
_SCALE_PAIRED_CURRENCY_RE = re.compile(
    rf"(?:{_PAIRED_CURR_ALT})\s+(?:in\s+)?(?:thousands?|millions?|billions?)"
    rf"|(?:thousands?|millions?|billions?)\s+(?:of\s+)?(?:{_PAIRED_CURR_ALT})",
    re.IGNORECASE,
)


def parse_numeric(val_str: str) -> Decimal | None:
    """Parse string formatted currency/number into Decimal."""
    cleaned = html.unescape(val_str)
    for token in ("US$", "NT$", "$", "€", "£", ",", "%"):
        cleaned = cleaned.replace(token, "")
    cleaned = _UNICODE_MINUS_RE.sub("-", cleaned).strip()
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


def _all_declared_currencies(text: str) -> set[Currency]:
    upper = text.upper()
    found: set[Currency] = set()
    for currency in Currency:
        if re.search(rf"\b{currency.value}\b", upper):
            found.add(currency)
    for pattern, currency in _CURRENCY_ALIASES:
        if re.search(pattern, upper):
            found.add(currency)
    return found


def _declared_currency(text: str) -> Currency | None:
    """Return an explicitly declared filing currency; a dollar glyph is ambiguous."""
    found = _all_declared_currencies(text)
    return next(iter(found)) if len(found) == 1 else None


def _extract_scale(text: str) -> Decimal:
    if _SCALE_BILLIONS_RE.search(text):
        return Decimal("1000000000")
    if _SCALE_MILLIONS_RE.search(text):
        return Decimal("1000000")
    if _SCALE_THOUSANDS_RE.search(text):
        return Decimal("1000")
    return Decimal(1)


def _preceding_table_headers(table: Tag) -> list[str]:
    """Collect up to 5 non-empty block headers immediately preceding ``table``,
    stopping at the previous table and skipping ancestor wrappers."""
    ancestors = {id(parent) for parent in table.parents}
    headers: list[str] = []
    cur: Tag = table
    steps = 0
    while steps < 24 and len(headers) < 5:
        prev = cur.find_previous(
            ["p", "div", "span", "h1", "h2", "h3", "h4", "h5", "b", "strong", "table"]
        )
        if prev is None or prev.name == "table":
            break
        cur = prev
        steps += 1
        if id(prev) in ancestors:
            continue
        txt = _WS_RE.sub(" ", prev.get_text(" ", strip=True)).strip()
        if not txt or len(txt) > 400:
            continue
        if not headers or headers[-1] != txt:
            headers.append(txt)
    headers.reverse()
    return headers


def _detect_table_currency_and_scale(
    header_text: str, table_text: str
) -> tuple[Currency | None, Decimal]:
    """Detect currency and scale with header priority and scale-paired tie-breaking."""
    scale = _extract_scale(header_text)
    if scale == Decimal(1):
        scale = _extract_scale(table_text)

    currency = _declared_currency(header_text)
    if currency is None:
        paired: set[Currency] = set()
        for match in _SCALE_PAIRED_CURRENCY_RE.finditer(header_text):
            paired.update(_all_declared_currencies(match.group(0)))
        if len(paired) == 1:
            currency = next(iter(paired))
    if currency is None:
        currency = _declared_currency(table_text)
    return currency, scale


def _expand_table_rows(table: Tag) -> list[tuple[list[str], int]]:
    """Expand ``tr`` elements into colspan-aligned cell lists plus non-empty cell count."""
    expanded: list[tuple[list[str], int]] = []
    for tr in table.find_all("tr"):
        raw_cells = tr.find_all(["td", "th"])
        row_cells: list[str] = []
        non_empty = 0
        for cell in raw_cells:
            txt = _WS_RE.sub(" ", html.unescape(cell.get_text(" ", strip=True))).strip()
            raw_cs = cell.get("colspan")
            try:
                cs = max(1, min(int(str(raw_cs)), 32)) if raw_cs is not None else 1
            except ValueError:
                cs = 1
            if txt:
                non_empty += 1
            row_cells.extend([txt] * cs)
        if non_empty > 0:
            expanded.append((row_cells, non_empty))
    return expanded


def _looks_like_data_row(cells: list[str]) -> bool:
    if len(cells) < 2 or not cells[0]:
        return False
    numeric_cells = [c for c in cells[1:] if c and "%" not in c and parse_numeric(c) is not None]
    if not numeric_cells:
        return False
    # Exclude pure year header rows like ["Period", "2026", "2025"].
    return not all(_YEAR_ONLY_RE.match(c) for c in numeric_cells)


def _split_headers_and_data(
    rows: list[tuple[list[str], int]],
) -> tuple[list[list[str]], list[str], list[list[str]]]:
    """Split expanded table rows into multi-column header rows, full-width banners, and data rows."""
    header_rows: list[list[str]] = []
    banner_texts: list[str] = []
    data_start = len(rows)
    for idx, (row_cells, non_empty) in enumerate(rows):
        if _looks_like_data_row(row_cells):
            data_start = idx
            break
        if non_empty == 1 and len(row_cells) > 1:
            unique_non_empty = [c for c in row_cells if c]
            if unique_non_empty:
                banner_texts.append(unique_non_empty[0])
        else:
            header_rows.append(row_cells)
    data_rows = [r for r, _ in rows[data_start:]]
    return header_rows, banner_texts, data_rows


def _is_ytd_only_table(context_text: str, fiscal_period_type: str) -> bool:
    fpt = fiscal_period_type.upper()
    if fpt not in ("Q1", "Q2", "Q3"):
        return False
    if not _YTD_HDR_RE.search(context_text):
        return False
    if _STANDALONE_Q_HDR_RE.search(context_text):
        return False
    return re.search(rf"\b{re.escape(fpt.lower())}\b", context_text, re.IGNORECASE) is None


def _select_target_column(
    header_rows: list[list[str]],
    *,
    period_end: datetime,
    fiscal_period_type: str,
) -> tuple[int | None, set[int]]:
    """Build combined per-column headers and return ``(target_col_idx, excluded_cols)``."""
    if not header_rows:
        return None, set()
    width = max(len(r) for r in header_rows)
    col_headers: list[str] = []
    excluded: set[int] = set()
    for col in range(width):
        parts: list[str] = []
        for row in header_rows:
            val = row[col] if col < len(row) else ""
            if val and (not parts or parts[-1] != val):
                parts.append(val)
        hdr = " ".join(parts).strip()
        col_headers.append(hdr)
        if col >= 1 and (_NOTE_COL_RE.match(hdr) or _EXCLUDED_COL_RE.search(hdr)):
            excluded.add(col)

    period_cols = [c for c in range(1, width) if _PERIOD_HDR_RE.search(col_headers[c])]
    if not period_cols:
        return None, excluded

    target_q = fiscal_period_type.lower()
    target_year = str(period_end.year)
    best_col: int | None = None
    best_score = 0.0
    for col in period_cols:
        if col in excluded:
            continue
        hdr_l = col_headers[col].lower()
        score = 0.0
        years_in_hdr = re.findall(r"\b20\d{2}\b", hdr_l)
        if years_in_hdr and target_year not in years_in_hdr:
            score -= 100.0
        elif target_year in years_in_hdr:
            score += 100.0

        has_q = re.search(rf"\b{re.escape(target_q)}\b", hdr_l) is not None
        has_3m = _STANDALONE_Q_HDR_RE.search(hdr_l) is not None
        has_ytd = _YTD_HDR_RE.search(hdr_l) is not None
        if fiscal_period_type.upper() in ("Q1", "Q2", "Q3") and has_ytd and not (has_q or has_3m):
            score -= 200.0
        if has_q:
            score += 100.0
        elif has_3m:
            score += 80.0
        elif not has_ytd:
            # Prefer a matching-year period date over unrelated columns when unlabeled by duration.
            score += 20.0

        score += 10.0 - (col * 0.01)
        if score > best_score:
            best_score = score
            best_col = col
    return best_col, excluded


_LABEL_FOOTNOTE_RE = re.compile(r"(?:\s+\d+\))+\s*$")
_LABEL_NOTE_RE = re.compile(r"\s*\(notes?\b[^)]*\)\s*$", re.IGNORECASE)
_RATIO_WORD_RE = re.compile(r"\bratios?\b")
_DISQUALIFY_LABEL_TOKENS = (
    "margin",
    "%",
    "non-controlling",
    "noncontrolling",
    "adjusted",
    "at cer",
    "effect of",
    "previous period",
    "reconciliation",
    "other operating",
    "segment operating",
)


def _match_line_item_label(raw_label: str) -> tuple[str, int, bool] | None:
    """Map a normalized table row label to ``(line_item, priority, is_unscaled)``."""
    norm = _UNICODE_MINUS_RE.sub("-", _WS_RE.sub(" ", raw_label)).strip().lower()
    norm = _LABEL_NOTE_RE.sub("", norm).strip()
    norm = _LABEL_FOOTNOTE_RE.sub("", norm).strip()
    if (
        not norm
        or any(tok in norm for tok in _DISQUALIFY_LABEL_TOKENS)
        or _RATIO_WORD_RE.search(norm)
    ):
        return None

    if "earnings per share" in norm or "diluted eps" in norm:
        if "diluted" in norm and "basic" not in norm:
            return ("eps_diluted", 1, True)
        return None

    if "per share" in norm or "cost of" in norm or "changes in" in norm:
        return None

    if norm in (
        "total revenue",
        "total revenues",
        "revenue",
        "revenues",
        "net revenue",
        "net revenues",
        "net sales",
        "net sales ifrs",
    ) or norm.startswith(("total revenue", "net sales", "net revenue")):
        return ("revenue", 1, False)

    if norm == "gross profit" or norm.startswith("gross profit"):
        return ("gross_profit", 1, False)

    if norm in (
        "operating income",
        "operating profit",
        "operating profit (ebit)",
        "operating (loss) income",
        "operating profit ifrs",
        "profit from operations",
        "income from operations",
    ) or norm.startswith(("operating income", "operating profit", "operating (loss) income")):
        return ("operating_income", 1, False)

    if (
        "attributable to" in norm
        and any(k in norm for k in ("shareholder", "owner", "parent"))
        and any(k in norm for k in ("net income", "net profit", "net loss", "profit"))
    ):
        return ("net_income", 2, False)

    if norm in (
        "net income",
        "net profit",
        "net loss",
        "net (loss) income",
        "net income for the period",
        "profit for the period",
        "net profit for the period",
    ) or norm.startswith(("net income", "net profit", "net loss", "net (loss) income")):
        return ("net_income", 1, False)

    if "before" not in norm and (
        "net cash provided by operating activities" in norm
        or "net cash generated from operating activities" in norm
        or "operating cash flow" in norm
        or "cash flows from operating activities" in norm
        or "cash flows generated from (used in) operating activities" in norm
    ):
        return ("operating_cash_flow", 1, False)

    if (
        "capital expenditures" in norm
        or "purchase of property and equipment" in norm
        or "purchase of property, plant and equipment" in norm
        or "purchases of property, plant and equipment" in norm
    ):
        return ("capital_expenditure", 1, False)

    if norm == "free cash flow" or norm.startswith("free cash flow"):
        return ("free_cash_flow", 2, False)

    return None


def _pick_row_cell_value(
    row_cells: list[str], target_col: int | None, excluded_cols: set[int]
) -> tuple[Decimal, str] | None:
    if target_col is not None:
        if target_col >= len(row_cells):
            return None
        raw = row_cells[target_col]
        if raw in ("$", "€", "£", "US$", "NT$") and target_col + 1 < len(row_cells):
            raw = row_cells[target_col + 1]
        if not raw or "%" in raw:
            return None
        num = parse_numeric(raw)
        return (num, raw) if num is not None else None

    for idx, raw in enumerate(row_cells[1:], start=1):
        if idx in excluded_cols or not raw or "%" in raw:
            continue
        num = parse_numeric(raw)
        if num is not None:
            return num, raw
    return None


def extract_fpi_financial_facts_html(
    html_text: str,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    *,
    fallback_currency: Currency | None = None,
) -> dict[str, tuple[Decimal, str, str]]:
    """Extract standard GAAP/IFRS line items from 6-K or 20-F HTML tables.

    Returns dict mapping line_item -> (Decimal value in whole currency, unit, locator/excerpt).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    results: dict[str, tuple[Decimal, str, str]] = {}
    results_priority: dict[str, int] = {}

    for table in soup.find_all("table"):
        rows = _expand_table_rows(table)
        if not rows:
            continue
        header_rows, banner_texts, data_rows = _split_headers_and_data(rows)
        if not data_rows:
            continue

        caption = table.find("caption")
        caption_txt = (
            _WS_RE.sub(" ", caption.get_text(" ", strip=True)).strip()
            if caption is not None
            else ""
        )
        preceding = _preceding_table_headers(table)
        hdr_rows_text = " ".join(" ".join(c for c in r if c) for r in header_rows)
        header_text = " ".join(
            part for part in (*preceding, caption_txt, *banner_texts, hdr_rows_text) if part
        )
        t_text = _WS_RE.sub(" ", table.get_text(" ", strip=True)).strip()

        if _is_ytd_only_table(header_text, fiscal_period_type):
            continue

        currency, scale = _detect_table_currency_and_scale(header_text, t_text)
        if currency is None:
            if fallback_currency is not None and not _all_declared_currencies(
                header_text + " " + t_text
            ):
                currency = fallback_currency
            else:
                # Never turn an unqualified "$" into USD without an issuer fallback.
                continue

        target_col, excluded_cols = _select_target_column(
            header_rows, period_end=period_end, fiscal_period_type=fiscal_period_type
        )
        # If header explicitly declared multiple period columns and none matched the target quarter, skip.
        if (
            header_rows
            and target_col is None
            and any(_PERIOD_HDR_RE.search(" ".join(r)) for r in header_rows)
        ):
            continue

        for row_cells in data_rows:
            if len(row_cells) < 2:
                continue
            matched = _match_line_item_label(row_cells[0])
            if matched is None:
                continue
            line_item, priority, is_unscaled = matched
            if results_priority.get(line_item, 0) >= priority:
                continue

            picked = _pick_row_cell_value(row_cells, target_col, excluded_cols)
            if picked is None:
                continue
            raw_val, raw_str = picked
            effective_scale = Decimal(1) if is_unscaled else scale
            scaled_val = raw_val * effective_scale
            if line_item == "capital_expenditure":
                scaled_val = -abs(scaled_val)

            results[line_item] = (
                scaled_val,
                currency.value,
                f"Table: {row_cells[0]} = {raw_str}",
            )
            results_priority[line_item] = priority

    # Derive FCF only when not directly reported in standalone quarterly statements.
    if (
        "free_cash_flow" not in results
        and "operating_cash_flow" in results
        and "capital_expenditure" in results
        and results["operating_cash_flow"][1] == results["capital_expenditure"][1]
    ):
        ocf: Decimal = results["operating_cash_flow"][0]
        capex: Decimal = results["capital_expenditure"][0]
        fcf: Decimal = ocf + capex  # capex is negative
        results["free_cash_flow"] = (
            fcf,
            results["operating_cash_flow"][1],
            f"Derived: OCF ({ocf}) + Capex ({capex})",
        )

    return results


def extract_fpi_kpis_narrative(
    html_text: str, plain_text: str, ticker: str
) -> list[tuple[str, Decimal, Unit, Currency | None, str]]:
    """Extract key metrics and KPIs mentioned in 6-K press releases."""
    kpis: list[tuple[str, Decimal, Unit, Currency | None, str]] = []
    declared_currency = _declared_currency(plain_text)

    # Bookings regex: e.g. "Total bookings in the second quarter of 2026 were $569.1 million"
    b_match = re.search(
        r"(?:total\s+)?bookings.*?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*million",
        plain_text,
        re.IGNORECASE,
    )
    if b_match and declared_currency is not None:
        val = Decimal(b_match.group(1)) * Decimal("1000000")
        kpis.append(("bookings", val, Unit.ACTUAL, declared_currency, b_match.group(0)[:250]))

    # Free cash flow ex-restructuring or FCF margin
    fcf_m = re.search(
        r"free\s+cash\s+flow\s+margin.*?([0-9]+(?:\.[0-9]+)?)\s*%",
        plain_text,
        re.IGNORECASE,
    )
    if fcf_m:
        val = Decimal(fcf_m.group(1))
        kpis.append(("free_cash_flow_margin", val, Unit.PERCENT, None, fcf_m.group(0)[:250]))

    # Creative subscriptions revenue / bookings for Wix
    if ticker.upper() == "WIX":
        cs_rev = re.search(
            r"Creative\s+Subscriptions\s+revenue.*?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*million",
            plain_text,
            re.IGNORECASE,
        )
        if cs_rev and declared_currency is not None:
            val = Decimal(cs_rev.group(1)) * Decimal("1000000")
            kpis.append(
                (
                    "creative_subscriptions_revenue",
                    val,
                    Unit.ACTUAL,
                    declared_currency,
                    cs_rev.group(0)[:250],
                )
            )

        cs_book = re.search(
            r"Creative\s+Subscriptions\s+bookings.*?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*million",
            plain_text,
            re.IGNORECASE,
        )
        if cs_book and declared_currency is not None:
            val = Decimal(cs_book.group(1)) * Decimal("1000000")
            kpis.append(
                (
                    "creative_subscriptions_bookings",
                    val,
                    Unit.ACTUAL,
                    declared_currency,
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
    kpis: list[tuple[str, Decimal, Unit, Currency | None, str]],
    force: bool = False,
) -> tuple[int, int]:
    """Persist financial_facts and kpi_facts with transactional replacement when force=True."""
    ff_inserted = 0
    kpi_inserted = 0

    # 1. Insert financial facts
    for line_item, (val, currency_str, excerpt) in financial_facts.items():
        loc = FactLocator(section="consolidated_financial_statements", verbatim_snippet=excerpt)
        loc_str = loc.to_json()
        new_id, _ = insert_with_restatement_detection(
            conn,
            ticker=ticker.upper(),
            period_end=period_end,
            fiscal_period_type=fiscal_period_type.upper(),
            line_item=line_item,
            value=val,
            currency=currency_str,
            unit="actual",
            source_doc_id=doc_id,
            confidence=0.98,
            extracted_by="sec_fpi_ingest@1",
            locator=loc_str,
        )
        if new_id is not None or force:
            ff_inserted += 1

    # 2. Insert KPI facts using manifest
    if kpis:
        kpi_values: list[KpiValue] = []
        for name, val, unit_enum, currency, excerpt in kpis:
            kpi_values.append(
                KpiValue(
                    name=name,
                    value=val,
                    unit=unit_enum,
                    currency=currency,
                    confidence=0.95,
                    source_excerpt=excerpt,
                    locator=FactLocator(section="press_release", verbatim_snippet=excerpt),
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

            new_kpi_id, _ = insert_kpi_with_restatement_detection(
                conn,
                ticker=ticker.upper(),
                period_end=period_end,
                fiscal_period_type=fiscal_period_type.upper(),
                kpi_definition_id=kpi_def_id,
                value=kpv.value,
                unit=kpv.unit.value,
                currency=kpv.currency.value if kpv.currency is not None else None,
                source_doc_id=doc_id,
                confidence=kpv.confidence,
                extracted_by="sec_fpi_ingest@1",
                locator=loc_str,
                source_excerpt=kpv.source_excerpt,
            )
            target_kpi_id = new_kpi_id
            if target_kpi_id is None:
                existing_kpi = conn.execute(
                    "SELECT id FROM kpi_facts WHERE UPPER(ticker) = ? AND period_end = ? "
                    "AND fiscal_period_type = ? AND kpi_definition_id = ? AND source_doc_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (
                        ticker.upper(),
                        period_end,
                        fiscal_period_type.upper(),
                        kpi_def_id,
                        doc_id,
                    ),
                ).fetchone()
                if existing_kpi is not None:
                    target_kpi_id = int(existing_kpi[0])
            if target_kpi_id is not None:
                ensure_deterministic_kpi_semantic_context(
                    conn,
                    kpi_fact_id=target_kpi_id,
                    metric_name=kpv.name,
                    period_end=period_end.date(),
                    value=kpv.value,
                    unit=kpv.unit,
                    extracted_by="sec_fpi_ingest@1",
                    locator=kpv.locator,
                    source_excerpt=kpv.source_excerpt,
                    reviewed_by="deterministic:sec_fpi_ingest@1",
                )
            if new_kpi_id is not None or force:
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
    fye_row = conn.execute(
        "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    fye_str = fye_row[0] if fye_row and fye_row[0] else "12-31"
    try:
        parts = fye_str.split("-")
        fye_month, fye_day = int(parts[0]), int(parts[1])
    except Exception:
        fye_month, fye_day = 12, 31

    located = locate_fpi_exhibit(
        ticker,
        quarter=quarter,
        year=year,
        form=form,
        fye_month=fye_month,
        fye_day=fye_day,
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
    nom_q: NominalQuarter = quarter if quarter is not None else "Q4"
    current_end, _ = expected_period_ends(nom_q, year, fye_month=fye_month, fye_day=fye_day)
    period_end_dt = datetime(current_end.year, current_end.month, current_end.day, tzinfo=UTC)

    doc_id = register_and_anchor_fpi_document(
        conn,
        ticker=ticker,
        fetched=fetched,
        period_end=period_end_dt,
        repo_root=repo_root,
    )

    fallback_currency = _ISSUER_FALLBACK_CURRENCY.get(ticker)

    financial_facts = extract_fpi_financial_facts_html(
        fetched.raw_html,
        ticker,
        period_end_dt,
        p_quarter,
        fallback_currency=fallback_currency,
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
