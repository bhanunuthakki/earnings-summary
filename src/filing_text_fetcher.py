"""SEC EDGAR 10-K / 10-Q full-text fetcher + section extraction.

Many of the analytical layers downstream (risk_factors, footnote_facts,
critical_accounting_estimates) need the NARRATIVE text of 10-K filings,
not the financial-statement JSONs from FMP. This module is the
text-fetching primitive they share.

Two-step pattern (mirrors insider_transactions._lookup_cik):
  1. Find the most-recent 10-K accession number via SEC submissions JSON.
  2. Fetch the primary document, strip HTML, return as plain text.

Section locators (Item 1A, Item 7, Item 8 footnotes) use heuristic regex
that survives most 10-K formatting variants. Falls back to LLM-driven
section identification when heuristics miss.

Cached at `data/sec_text/<TICKER>_10k_<FY>.txt` so subsequent extractors
read from disk rather than re-fetching.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from insider_transactions import (  # type: ignore[import-not-found]
    SEC_USER_AGENT_DEFAULT,
    _http_get_json,
    _http_get_text,
    _lookup_cik_for_ticker,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FilingTextResult:
    """One 10-K text fetch outcome."""

    ticker: str
    accession_number: str
    fiscal_year: int
    filing_date: str
    primary_doc_url: str
    text: str  # plain-text (HTML stripped)
    item_1a_text: str | None = None
    item_7_text: str | None = None
    item_8_text: str | None = None


# Item-finding regexes. SEC filings are wildly inconsistent — these handle
# the common cases: "Item 1A.", "ITEM 1A", "Item 1A - Risk Factors",
# "Item 1A. Risk Factors", with optional whitespace and punctuation.
_ITEM_1A_RX = re.compile(
    r"item\s*1\s*a\.?\s*[—–\-:]?\s*risk\s+factors?",
    re.IGNORECASE,
)
_ITEM_1B_RX = re.compile(
    r"item\s*1\s*b\.?\s*[—–\-:]?\s*unresolved",
    re.IGNORECASE,
)
_ITEM_2_RX = re.compile(r"item\s*2\.?\s*[—–\-:]?\s*properties", re.IGNORECASE)
_ITEM_7_RX = re.compile(
    r"item\s*7\.?\s*[—–\-:]?\s*management",
    re.IGNORECASE,
)
_ITEM_7A_RX = re.compile(
    r"item\s*7\s*a\.?\s*[—–\-:]?\s*quantitative",
    re.IGNORECASE,
)
_ITEM_8_RX = re.compile(
    r"item\s*8\.?\s*[—–\-:]?\s*financial\s+statements",
    re.IGNORECASE,
)
_ITEM_9_RX = re.compile(
    r"item\s*9\.?\s*[—–\-:]?\s*changes",
    re.IGNORECASE,
)


def fetch_latest_10k_text(
    *,
    ticker: str,
    user_agent: str | None = None,
    cache_dir: Path | None = None,
    fiscal_year: int | None = None,
) -> FilingTextResult | None:
    """Fetch the most-recent (or specific-year) 10-K for `ticker`.

    Returns None when:
      - CIK can't be resolved (foreign issuer with no SEC presence)
      - No 10-K found in submissions
      - HTML fetch fails
    """
    ua = user_agent or SEC_USER_AGENT_DEFAULT
    cik = _lookup_cik_for_ticker(ticker, user_agent=ua)
    if cik is None:
        log.warning({"event": "filing_text_no_cik", "ticker": ticker})
        return None

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    # 1. Find 10-K accession via submissions API
    cik_padded = cik.zfill(10)
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        payload = _http_get_json(submissions_url, user_agent=ua)
    except Exception as exc:  # noqa: BLE001 — log + return
        log.warning({"event": "filing_text_submissions_failed", "ticker": ticker, "error": str(exc)})
        return None

    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []
    report_dates = recent.get("reportDate") or []

    target_accession = None
    target_date = None
    target_doc = None
    target_report = None
    for form, acc, fdate, pdoc, rdate in zip(forms, accessions, dates, primary_docs, report_dates):
        if form != "10-K":
            continue
        if fiscal_year is not None:
            # Match by reportDate year
            if rdate.startswith(str(fiscal_year)):
                target_accession = acc
                target_date = fdate
                target_doc = pdoc
                target_report = rdate
                break
        else:
            # First match = most recent
            target_accession = acc
            target_date = fdate
            target_doc = pdoc
            target_report = rdate
            break

    if not target_accession or not target_doc:
        log.info({"event": "filing_text_no_10k", "ticker": ticker})
        return None

    # 2. Fetch the primary document HTML
    acc_clean = target_accession.replace("-", "")
    primary_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{target_doc}"

    # Cache check
    fy_int = _parse_fy(target_report) or 0
    cache_path = (
        cache_dir / f"{ticker}_10k_{fy_int}.txt" if cache_dir is not None else None
    )
    text: str | None = None
    if cache_path is not None and cache_path.exists():
        try:
            text = cache_path.read_text(encoding="utf-8")
            log.debug({"event": "filing_text_cache_hit", "ticker": ticker, "fy": fy_int})
        except OSError:
            text = None

    if text is None:
        try:
            html = _http_get_text(primary_url, user_agent=ua, timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            log.warning({"event": "filing_text_html_fetch_failed", "ticker": ticker, "error": str(exc)})
            return None
        text = _strip_html(html)
        if cache_path is not None:
            try:
                cache_path.write_text(text, encoding="utf-8")
            except OSError as exc:
                log.debug({"event": "filing_text_cache_write_failed", "error": str(exc)})

    # 3. Extract sections
    item_1a = _extract_between(text, _ITEM_1A_RX, _ITEM_1B_RX) or _extract_between(
        text, _ITEM_1A_RX, _ITEM_2_RX
    )
    item_7 = _extract_between(text, _ITEM_7_RX, _ITEM_7A_RX) or _extract_between(
        text, _ITEM_7_RX, _ITEM_8_RX
    )
    item_8 = _extract_between(text, _ITEM_8_RX, _ITEM_9_RX)

    return FilingTextResult(
        ticker=ticker,
        accession_number=target_accession,
        fiscal_year=fy_int,
        filing_date=target_date or "",
        primary_doc_url=primary_url,
        text=text,
        item_1a_text=item_1a,
        item_7_text=item_7,
        item_8_text=item_8,
    )


def fetch_latest_def14a_text(
    *,
    ticker: str,
    user_agent: str | None = None,
    cache_dir: Path | None = None,
    fiscal_year: int | None = None,
) -> FilingTextResult | None:
    """Fetch the most-recent DEF 14A proxy filing for `ticker`.

    Similar to fetch_latest_10k_text but targets form type 'DEF 14A'. Item
    section extractors don't apply (proxies have their own structure); the
    consumer prompts the LLM directly with the full text.
    """
    ua = user_agent or SEC_USER_AGENT_DEFAULT
    cik = _lookup_cik_for_ticker(ticker, user_agent=ua)
    if cik is None:
        return None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    cik_padded = cik.zfill(10)
    try:
        payload = _http_get_json(
            f"https://data.sec.gov/submissions/CIK{cik_padded}.json", user_agent=ua
        )
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "def14a_submissions_failed", "ticker": ticker, "error": str(exc)})
        return None

    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []
    report_dates = recent.get("reportDate") or []

    target_accession = None
    target_date = None
    target_doc = None
    target_report = None
    for form, acc, fdate, pdoc, rdate in zip(forms, accessions, dates, primary_docs, report_dates):
        # DEF 14A and DEFA14A (additional materials) — pick straight DEF 14A.
        if form not in ("DEF 14A", "DEFA14A"):
            continue
        if fiscal_year is not None and not (rdate or fdate).startswith(str(fiscal_year)):
            continue
        target_accession = acc
        target_date = fdate
        target_doc = pdoc
        target_report = rdate
        break

    if not target_accession or not target_doc:
        log.info({"event": "def14a_not_found", "ticker": ticker})
        return None

    acc_clean = target_accession.replace("-", "")
    primary_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{target_doc}"
    fy_int = _parse_fy(target_report) or _parse_fy(target_date) or 0
    cache_path = cache_dir / f"{ticker}_def14a_{fy_int}.txt" if cache_dir is not None else None

    text: str | None = None
    if cache_path is not None and cache_path.exists():
        try:
            text = cache_path.read_text(encoding="utf-8")
        except OSError:
            text = None
    if text is None:
        try:
            html = _http_get_text(primary_url, user_agent=ua, timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            log.warning({"event": "def14a_fetch_failed", "ticker": ticker, "error": str(exc)})
            return None
        text = _strip_html(html)
        if cache_path is not None:
            try:
                cache_path.write_text(text, encoding="utf-8")
            except OSError:
                pass

    return FilingTextResult(
        ticker=ticker,
        accession_number=target_accession,
        fiscal_year=fy_int,
        filing_date=target_date or "",
        primary_doc_url=primary_url,
        text=text,
    )


def fetch_latest_s1_text(
    *,
    ticker: str,
    user_agent: str | None = None,
    cache_dir: Path | None = None,
) -> FilingTextResult | None:
    """Fetch the most-recent S-1 / S-1/A registration statement for `ticker`.

    For recently-IPO'd companies that don't have a 10-K yet, the S-1 (and its
    amendments S-1/A, plus the final 424B prospectus) carries the same
    narrative content the per-ticker prompts otherwise pull from Item 1 /
    Item 1A / Item 7: business description, TAM, risk factors, MD&A, and
    audited historical financials. Form-type preference order:

      1. ``S-1/A``  — most recent amendment (final pre-IPO version)
      2. ``S-1``    — original filing
      3. ``424B4`` / ``424B3`` / ``424B1`` — final prospectus filed post-IPO

    Cached at ``data/sec_text/<TICKER>_s1_<FY>.txt`` (FY = filing-year fallback
    when there's no reportDate, which is the common case for S-1s).
    """
    ua = user_agent or SEC_USER_AGENT_DEFAULT
    cik = _lookup_cik_for_ticker(ticker, user_agent=ua)
    if cik is None:
        log.warning({"event": "s1_no_cik", "ticker": ticker})
        return None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    cik_padded = cik.zfill(10)
    try:
        payload = _http_get_json(
            f"https://data.sec.gov/submissions/CIK{cik_padded}.json", user_agent=ua
        )
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "s1_submissions_failed", "ticker": ticker, "error": str(exc)})
        return None

    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []
    report_dates = recent.get("reportDate") or []

    # Preference order: S-1/A (final amendment) wins over S-1 (original) wins
    # over 424B (post-effectiveness prospectus). Within each tier, most recent.
    preference = ("S-1/A", "S-1", "424B4", "424B3", "424B1")
    best_by_form: dict[str, tuple[str, str, str, str]] = {}
    for form, acc, fdate, pdoc, rdate in zip(
        forms, accessions, dates, primary_docs, report_dates, strict=False
    ):
        if form in preference and form not in best_by_form:
            best_by_form[form] = (acc, fdate, pdoc, rdate)

    target_form: str | None = None
    for tier in preference:
        if tier in best_by_form:
            target_form = tier
            break
    if target_form is None:
        log.info({"event": "s1_not_found", "ticker": ticker})
        return None
    target_accession, target_date, target_doc, target_report = best_by_form[target_form]

    acc_clean = target_accession.replace("-", "")
    primary_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{target_doc}"
    fy_int = _parse_fy(target_report) or _parse_fy(target_date) or 0
    cache_path = (
        cache_dir / f"{ticker}_s1_{fy_int}.txt" if cache_dir is not None else None
    )

    text: str | None = None
    if cache_path is not None and cache_path.exists():
        try:
            text = cache_path.read_text(encoding="utf-8")
        except OSError:
            text = None
    if text is None:
        try:
            html = _http_get_text(primary_url, user_agent=ua, timeout=120.0)
        except Exception as exc:  # noqa: BLE001
            log.warning({"event": "s1_fetch_failed", "ticker": ticker, "error": str(exc)})
            return None
        text = _strip_html(html)
        if cache_path is not None:
            try:
                cache_path.write_text(text, encoding="utf-8")
            except OSError as exc:
                log.debug({"event": "s1_cache_write_failed", "error": str(exc)})

    # S-1s have the same Item 1A "Risk Factors" structure as 10-Ks.
    item_1a = _extract_between(text, _ITEM_1A_RX, _ITEM_1B_RX) or _extract_between(
        text, _ITEM_1A_RX, _ITEM_2_RX
    )

    log.info(
        {
            "event": "s1_fetched",
            "ticker": ticker,
            "form_type": target_form,
            "accession": target_accession,
            "filing_date": target_date,
            "chars": len(text),
        },
    )
    return FilingTextResult(
        ticker=ticker,
        accession_number=target_accession,
        fiscal_year=fy_int,
        filing_date=target_date or "",
        primary_doc_url=primary_url,
        text=text,
        item_1a_text=item_1a,
    )


def split_risk_factors(item_1a_text: str) -> list[tuple[str, str]]:
    """Split Item 1A into individual risk factors.

    Heuristics:
      - Bold/header-like lines (often ALL CAPS or sentence-cased) act as
        risk-factor headers.
      - Body is everything between two consecutive headers.
      - Returns [(heading, body), ...]; the first chunk before any heading
        is dropped (boilerplate intro).
    """
    if not item_1a_text:
        return []
    # Split on lines that look like headings. The pattern: 5-150 char
    # capitalized phrase ending in a period or colon, possibly starting
    # with a number / bullet.
    lines = item_1a_text.splitlines()
    # Detect candidate headings: lines that are short, have title-case or
    # ALL CAPS, and aren't sentence continuations.
    sections: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_body: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_heading is not None:
                current_body.append("")
            continue
        if _looks_like_risk_heading(stripped):
            if current_heading is not None and current_body:
                sections.append((current_heading, current_body))
            current_heading = stripped.rstrip(".:—-")
            current_body = []
        elif current_heading is not None:
            current_body.append(stripped)
    if current_heading is not None and current_body:
        sections.append((current_heading, current_body))

    out: list[tuple[str, str]] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if len(body) < 60:  # skip false-positive headings with no body
            continue
        out.append((heading, body))
    return out


def _looks_like_risk_heading(line: str) -> bool:
    """Heuristic: short (5-180 chars), starts with a capital, mostly Title
    Case or All Caps, doesn't end with mid-sentence punctuation."""
    if not (5 < len(line) < 180):
        return False
    if line.endswith(("..", ",", ";", " ")):
        return False
    if not line[0].isupper():
        return False
    # Reject lines that look like body sentences ("The Company ...", with
    # comma-heavy / verb-heavy structure)
    words = line.split()
    if len(words) < 2:
        return False
    if len(words) > 28:
        return False
    upper_count = sum(1 for w in words if w[:1].isupper())
    # At least half the words should start uppercase (header-ish)
    if upper_count < len(words) * 0.4:
        return False
    return True


def _extract_between(text: str, start_rx: re.Pattern, end_rx: re.Pattern) -> str | None:
    """Return the slice between the first match of start_rx and the first
    match of end_rx that occurs AFTER start_rx."""
    m_start = start_rx.search(text)
    if m_start is None:
        return None
    after = text[m_start.end():]
    m_end = end_rx.search(after)
    if m_end is None:
        # No end marker — take up to next 200KB as a reasonable cap
        return after[:200_000].strip()
    return after[: m_end.start()].strip()


def _strip_html(html: str) -> str:
    """Strip HTML tags + common entities + collapse whitespace.

    Keeps the structure as plain text — paragraph breaks are preserved as
    double newlines, but inline tags are removed wholesale.
    """
    # Remove script + style blocks first
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert block elements to newlines for layout preservation
    text = re.sub(r"<\s*/?\s*(p|div|li|tr|br|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common entities
    entities = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&apos;": "'",
        "&#160;": " ",
        "&#8217;": "'",
        "&#8216;": "'",
        "&#8220;": '"',
        "&#8221;": '"',
        "&#8211;": "-",
        "&#8212;": "—",
    }
    for k, v in entities.items():
        text = text.replace(k, v)
    # Numeric entities catch-all
    text = re.sub(r"&#\d+;", " ", text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _parse_fy(report_date: str) -> int | None:
    if not report_date:
        return None
    try:
        return int(report_date[:4])
    except ValueError:
        return None


def load_canonical_narrative(
    *,
    ticker: str,
    repo_root: Path,
    user_agent: str | None = None,
) -> FilingTextResult | None:
    """Return the canonical narrative filing text for ``ticker``.

    Reads ``micro_thesis/holdings/<TICKER>.json``; if ``data_anchor == "s1"``
    (recently-IPO'd issuers — no 10-K yet), loads the S-1 cache (preferring
    ``s1_cache_path`` when set and present on disk, else falling back to
    ``fetch_latest_s1_text`` which itself caches at
    ``data/sec_text/<TICKER>_s1_<FY>.txt``). Otherwise — the default case for
    every mature issuer — dispatches to ``fetch_latest_10k_text``.

    Returns ``None`` when neither source is available. Callers must tolerate
    None; the downstream consumers fall back to "no narrative" mode (skipped
    extraction, MISSING_DATA section status, etc.).

    The helper is the single entry point so consumers don't need to repeat the
    holdings-JSON anchor check — extend this function when more anchor modes
    appear (e.g. ``data_anchor == "10q"`` for issuers ~6mo past IPO with one
    10-Q on file but no 10-K).
    """
    ticker = ticker.upper()
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    data_anchor = "10k"
    s1_cache_rel: str | None = None
    if holdings_path.exists():
        try:
            payload = cast("dict[str, object]", json.loads(holdings_path.read_text(encoding="utf-8")))
            raw_anchor = payload.get("data_anchor")
            if isinstance(raw_anchor, str) and raw_anchor.strip():
                data_anchor = raw_anchor.strip().lower()
            raw_cache = payload.get("s1_cache_path")
            if isinstance(raw_cache, str) and raw_cache.strip():
                s1_cache_rel = raw_cache.strip()
        except (OSError, json.JSONDecodeError) as exc:
            log.debug({"event": "canonical_narrative_holdings_read_failed", "ticker": ticker, "error": str(exc)})

    cache_dir = repo_root / "data" / "sec_text"

    if data_anchor == "s1":
        # Fast path: the holdings file points at a known cache file. Avoid the
        # SEC round-trip when the analyst has already populated the cache.
        if s1_cache_rel:
            cache_path = repo_root / s1_cache_rel
            if cache_path.exists():
                try:
                    text = cache_path.read_text(encoding="utf-8")
                except OSError as exc:
                    log.debug({"event": "s1_cache_read_failed", "ticker": ticker, "path": str(cache_path), "error": str(exc)})
                    text = ""
                if text.strip():
                    item_1a = _extract_between(text, _ITEM_1A_RX, _ITEM_1B_RX) or _extract_between(
                        text, _ITEM_1A_RX, _ITEM_2_RX
                    )
                    # Parse FY from the cache filename (e.g. ``FRVO_s1_2026.txt``)
                    m = re.search(r"_s1_(\d{4})\.txt$", cache_path.name)
                    fy_int = int(m.group(1)) if m else 0
                    return FilingTextResult(
                        ticker=ticker,
                        accession_number="",
                        fiscal_year=fy_int,
                        filing_date="",
                        primary_doc_url=str(cache_path),
                        text=text,
                        item_1a_text=item_1a,
                    )
        # Cache miss — fall through to the SEC fetcher (which also caches).
        return fetch_latest_s1_text(
            ticker=ticker, user_agent=user_agent, cache_dir=cache_dir
        )

    return fetch_latest_10k_text(
        ticker=ticker, user_agent=user_agent, cache_dir=cache_dir
    )
