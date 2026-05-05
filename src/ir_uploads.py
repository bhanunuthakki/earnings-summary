"""Deterministic classifier for manually-uploaded IR documents.

Given a file dropped in `ir_documents/` at the root level, decide its
`(ticker, doc_type, period_end)` from filename heuristics + first-page content
fingerprinting. This is the manual-upload counterpart to the URL-manifest based
`fetch_ir_documents.py` flow.

No LLM calls. Every classification is rule-driven and explainable: the
`CategorizationResult` carries the textual evidence used. If neither filename
nor content gives a confident answer, a `CategorizationFailure` is returned and
the file is quarantined — never silently dropped.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader

from models.documents import DocType
from models.ir_uploads import (
    CategorizationFailure,
    CategorizationResult,
    Confidence,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issuer / ticker registry
# ---------------------------------------------------------------------------

# (ticker, fiscal_calendar_id, ordered list of issuer-name substrings that
# appear in cover-page text). Order matters: more specific names first to
# avoid false-positive collisions (e.g. "Mercado Libre" before "MELI").
class FiscalCalendar(str):
    """Calendar ID — see `_period_end_for()` for mapping."""


_CAL_CALENDAR = "calendar"  # FY-end Dec 31 — Q1=Mar-31, Q2=Jun-30, Q3=Sep-30, Q4=Dec-31
_CAL_VEEV = "veeva"  # FY-end Jan 31
_CAL_RUBRIK = "rubrik"  # FY-end Apr 30 — per fetch_ir_documents.md quarter table
_CAL_NVO = "nvo"  # Calendar, but H1=Q2, 9M=Q3, FY=Q4

ISSUER_REGISTRY: list[tuple[str, str, tuple[str, ...]]] = [
    ("MELI", _CAL_CALENDAR, ("MercadoLibre", "Mercado Libre", "Mercado  Libre")),
    ("NU", _CAL_CALENDAR, ("Nu Holdings", "Nu's Investor", "Nubank")),
    ("RBRK", _CAL_RUBRIK, ("Rubrik",)),
    ("NOW", _CAL_CALENDAR, ("ServiceNow",)),
    ("WIX", _CAL_CALENDAR, ("Wix.com", "Wix Ltd", "Wix's", "Wix ", "WIX ")),
    ("NVO", _CAL_NVO, ("Novo Nordisk", "Amounts in DKK million", "Amounts  in  DKK")),
    ("GOOG", _CAL_CALENDAR, ("Alphabet Inc", "Alphabet's")),
    ("META", _CAL_CALENDAR, ("Meta Platforms", "Meta Reports")),
    ("AMZN", _CAL_CALENDAR, ("Amazon.com", "AMAZON.COM")),
    ("VEEV", _CAL_VEEV, ("Veeva Systems", "Veeva ")),
    ("BN", _CAL_CALENDAR, ("Brookfield Corporation", "Brookfield Asset Management")),
]


# ---------------------------------------------------------------------------
# Doc-type detection
# ---------------------------------------------------------------------------

# Each rule: (DocType, regex, evidence-label). First rule that matches wins.
# Patterns are deliberately precise — they look for cover-page artifact labels,
# not random prose. Anchored to compiled regex for speed; case-insensitive.
_DOC_TYPE_RULES: list[tuple[DocType, re.Pattern[str], str]] = [
    # Order matters. Specific cover-page / opening-paragraph signals first;
    # broad phrases that often appear in disclaimers (e.g. "Conference Call",
    # "Annual Report") sit at the end so they only fire when nothing more
    # decisive matched.

    # ---- Transcripts: only the strongest cover-page signals up-front. ----
    (
        DocType.IR_TRANSCRIPT,
        re.compile(r"\A\s*Operator\s*[:.]", re.IGNORECASE),
        "transcript_operator_opener",
    ),
    (
        DocType.IR_TRANSCRIPT,
        re.compile(r"\b(Earnings\s+Call\s+Script|Prepared\s+Remarks)\b", re.IGNORECASE),
        "transcript_title",
    ),

    # ---- Shareholder letters first: their bodies contain press-release-
    # style "today reported financial results" language verbatim, so the
    # cover-page title has to win over those broader signals. ----
    (
        DocType.IR_INVESTOR_UPDATE,
        re.compile(
            r"\b(Letter\s+to\s+(?:Our\s+)?Shareholders?|To\s+Our\s+Shareholders?|Shareholder\s+Letter)\b",
            re.IGNORECASE,
        ),
        "shareholder_letter_label",
    ),

    # ---- Presentations: cover-page titles. ----
    (
        DocType.IR_PRESENTATION,
        re.compile(r"\b(Investor|Earnings|Results)\s*Presentation\b", re.IGNORECASE),
        "presentation_label",
    ),
    (
        DocType.IR_PRESENTATION,
        re.compile(r"\bEarnings\s*Slides?\b", re.IGNORECASE),
        "earnings_slides_label",
    ),
    (
        DocType.IR_PRESENTATION,
        re.compile(r"\bCompany\s+Overview\b", re.IGNORECASE),
        "company_overview_label",
    ),

    # ---- Press releases: opening-paragraph / dateline signals. ----
    (
        DocType.IR_PRESS_RELEASE,
        re.compile(
            r"\btoday\s+(?:reported|announced|reports|announces)\s+(?:its\s+)?(?:financial\s+)?results?\b",
            re.IGNORECASE,
        ),
        "today_reported",
    ),
    (
        DocType.IR_PRESS_RELEASE,
        re.compile(
            r"\bReports?\s+(First|Second|Third|Fourth)\s+Quarter\b",
            re.IGNORECASE,
        ),
        "reports_quarter_phrase",
    ),
    (
        DocType.IR_PRESS_RELEASE,
        # Press-release dateline: "CITY[, STATE-OR-COUNTRY][;,] Month DD, YYYY".
        # Allow optional ", State" / "; Country" between city and date so
        # MELI's "MONTEVIDEO, Uruguay; October 29, 2025" matches alongside
        # the simpler "SEATTLE, WA -- January 30, 2026" form.
        re.compile(
            r"^[A-Z][A-Z\s]{2,40}(?:,\s*[A-Za-z\.]+)?[;,\s\-]+"
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
            re.MULTILINE,
        ),
        "press_release_dateline",
    ),

    # ---- Loose transcript fallback: only fires when none of the above
    # presentation/press-release/transcript-title rules matched. Disclaimers
    # in presentations and press releases routinely mention "earnings
    # conference call", which is why this is last. ----
    (
        DocType.IR_TRANSCRIPT,
        re.compile(r"\b(Earnings\s+Conference\s+Call|Conference\s+Call)\b", re.IGNORECASE),
        "transcript_phrase",
    ),

    # ---- Annual report tightened to require year context, so it doesn't
    # misfire on disclaimer phrases like "annual report on Form 20-F". ----
    (
        DocType.IR_INVESTOR_UPDATE,
        re.compile(
            r"\bAnnual\s+Report(?:\s+(?:for\s+|fiscal\s+year\s+))?\s+(?:20)?\d{2}\b",
            re.IGNORECASE,
        ),
        "annual_report_label",
    ),
]


# Rules to apply against `re.sub(r"\s+", "", text).lower()`. Maintain the same
# DocType outcomes as `_DOC_TYPE_RULES`, just glue the keywords together so we
# match pypdf's no-space output ("EarningsCall", "PreparedRemarks", etc).
_DOC_TYPE_SQUASHED_RULES: list[tuple[DocType, re.Pattern[str], str]] = [
    # Same priority ordering as `_DOC_TYPE_RULES`. Patterns assume
    # `re.sub(r"\s+", "", text).lower()` — i.e. pypdf's no-space output.
    (DocType.IR_TRANSCRIPT, re.compile(r"earningscallscript"), "transcript_title"),
    (DocType.IR_TRANSCRIPT, re.compile(r"preparedremarks"), "transcript_title"),
    (DocType.IR_INVESTOR_UPDATE, re.compile(r"lettertoshareholders"), "shareholder_letter_label"),
    (DocType.IR_INVESTOR_UPDATE, re.compile(r"toourshareholders"), "shareholder_letter_label"),
    (DocType.IR_INVESTOR_UPDATE, re.compile(r"shareholderletter"), "shareholder_letter_label"),
    (DocType.IR_PRESENTATION, re.compile(r"investorpresentation"), "presentation_label"),
    (DocType.IR_PRESENTATION, re.compile(r"earningspresentation"), "presentation_label"),
    (DocType.IR_PRESENTATION, re.compile(r"resultspresentation"), "presentation_label"),
    (DocType.IR_PRESENTATION, re.compile(r"companyoverview"), "company_overview_label"),
    (
        DocType.IR_PRESS_RELEASE,
        re.compile(r"todayreported(?:its)?(?:financial)?results"),
        "today_reported",
    ),
    (
        DocType.IR_PRESS_RELEASE,
        re.compile(r"reports(?:first|second|third|fourth)quarter"),
        "reports_quarter_phrase",
    ),
    (DocType.IR_TRANSCRIPT, re.compile(r"earningsconferencecall"), "transcript_phrase"),
    (DocType.IR_TRANSCRIPT, re.compile(r"earningscall"), "transcript_phrase"),
    (DocType.IR_TRANSCRIPT, re.compile(r"conferencecall"), "transcript_phrase"),
    (DocType.IR_INVESTOR_UPDATE, re.compile(r"annualreport20\d{2}"), "annual_report_label"),
]


# ---------------------------------------------------------------------------
# Period extraction
# ---------------------------------------------------------------------------

_RX_QUARTER_APOSTROPHE = re.compile(r"Q(?P<q>[1-4])['‘’ʼ]\s*(?P<yy>\d{2})\b")
_RX_QUARTER_SPACE = re.compile(r"Q(?P<q>[1-4])\s*(?P<y>20\d{2})\b")
_RX_QUARTER_FULL = re.compile(
    r"\b(?P<word>First|Second|Third|Fourth)\s+Quarter(?:\s+Fiscal)?\s+(?:20)?(?P<y>\d{2,4})\b",
    re.IGNORECASE,
)
_RX_QUARTER_FY = re.compile(
    r"\b(?P<word>First|Second|Third|Fourth)\s+Quarter\s+Fiscal\s+(?P<y>20\d{2})\b",
    re.IGNORECASE,
)
_RX_FY_QY = re.compile(r"\bQ(?P<q>[1-4])\s+FY\s*(?P<y>\d{2,4})\b", re.IGNORECASE)
_RX_NVO_PERIOD = re.compile(
    r"\b(?P<period>First\s+three\s+months|First\s+six\s+months|First\s+nine\s+months|Full\s+year)\s+(?:of\s+)?(?P<y>20\d{2})\b",
    re.IGNORECASE,
)

_WORD_TO_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4}

# "Quarter Ended March 31, 2025" / "September 30 Quarter End 2025" — common in
# MELI's IR-published transcripts and conference-call decks.
_RX_QUARTER_ENDED_DATE = re.compile(
    r"\bQuarter\s+(?:E|e)nded?\s+"
    r"(?P<m>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<d>\d{1,2}),?\s+(?P<y>20\d{2})\b",
    re.IGNORECASE,
)
_RX_DATE_QUARTER_ENDED = re.compile(
    r"\b(?P<m>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<d>\d{1,2})\s+Quarter\s+End(?:ed)?\s+(?P<y>20\d{2})\b",
    re.IGNORECASE,
)
# "Fourth Quarter & Full Year 2025" / "Fourth Quarter and Full Year 2025"
_RX_QUARTER_FULL_YEAR = re.compile(
    r"\b(?P<word>First|Second|Third|Fourth)\s+Quarter\s+(?:&|and)\s+Full\s+Year\s+(?P<y>20\d{2})\b",
    re.IGNORECASE,
)

_MONTH_TO_Q: dict[str, int] = {
    "january": 1, "february": 1, "march": 1,
    "april": 2, "may": 2, "june": 2,
    "july": 3, "august": 3, "september": 3,
    "october": 4, "november": 4, "december": 4,
}


def _yy_to_yyyy(yy: int) -> int:
    """Two-digit year → four-digit; window: 2000-2099."""
    if yy < 100:
        return 2000 + yy
    return yy


def _period_end_for(ticker_calendar: str, year: int, q: int) -> date:
    """Map (calendar, year, q) → period-end ISO date.

    See `directives/fetch_ir_documents.md` for the per-issuer fiscal calendar.
    """
    if ticker_calendar == _CAL_CALENDAR:
        return [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)][q - 1]
    if ticker_calendar == _CAL_VEEV:
        # FY-N Q1 ends Apr 30 (N-1), Q4 ends Jan 31 (N).
        return [
            date(year - 1, 4, 30),
            date(year - 1, 7, 31),
            date(year - 1, 10, 31),
            date(year, 1, 31),
        ][q - 1]
    if ticker_calendar == _CAL_RUBRIK:
        # FY-N Q1 ends Jul 31 (N-1), Q4 ends Apr 30 (N).
        return [
            date(year - 1, 7, 31),
            date(year - 1, 10, 31),
            date(year, 1, 31),
            date(year, 4, 30),
        ][q - 1]
    if ticker_calendar == _CAL_NVO:
        return [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)][q - 1]
    raise ValueError(f"Unknown calendar id: {ticker_calendar!r}")


def _calendar_for(ticker: str) -> str:
    for tk, cal, _ in ISSUER_REGISTRY:
        if tk == ticker:
            return cal
    raise ValueError(f"No calendar registered for ticker {ticker!r}")


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

_PDF_FINGERPRINT_PAGES = 2
_PDF_FINGERPRINT_CHARS = 6000


def fingerprint_pdf(path: Path) -> str:
    """Return the first ~2 pages of text from a PDF, capped at 6000 chars."""
    reader = PdfReader(str(path))
    chunks: list[str] = []
    for i in range(min(_PDF_FINGERPRINT_PAGES, len(reader.pages))):
        chunks.append(reader.pages[i].extract_text() or "")
    text = "\n".join(chunks)
    return text[:_PDF_FINGERPRINT_CHARS]


def fingerprint_xlsx(path: Path) -> str:
    """Stringify the first sheet's first ~12 rows for substring matching.

    `wb.close()` is required on Windows: read_only mode keeps the underlying
    zipfile handle open until explicitly closed, which blocks any subsequent
    move/unlink of the source file.
    """
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts: list[str] = [f"sheets={wb.sheetnames}"]
        for sn in wb.sheetnames[:2]:
            ws = wb[sn]
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 12:
                    break
                parts.append(" | ".join("" if v is None else str(v)[:80] for v in row))
        return "\n".join(parts)[:_PDF_FINGERPRINT_CHARS]
    finally:
        wb.close()


def fingerprint(path: Path) -> str:
    sfx = path.suffix.lower()
    if sfx == ".pdf":
        return fingerprint_pdf(path)
    if sfx == ".xlsx":
        return fingerprint_xlsx(path)
    raise ValueError(f"Unsupported extension {sfx!r} for {path.name}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Classification primitives
# ---------------------------------------------------------------------------


def _detect_ticker(text: str) -> tuple[str | None, list[str]]:
    """Substring-match against the issuer registry. Returns (ticker, evidence)."""
    for ticker, _cal, names in ISSUER_REGISTRY:
        for name in names:
            if name in text:
                return ticker, [f"issuer_name:{name!r}"]
    return None, []


_RX_WIX_CDN_PREFIX = re.compile(r"^(?:2e8ef1|4f4a31)_[0-9a-f]{32}\.pdf$", re.IGNORECASE)
_RX_NU_RESULTS_DECK = re.compile(r"^[1-4]Q\d{2} Results Presentation\.pdf$", re.IGNORECASE)
_RX_NU_TRANSCRIPT_FILE = re.compile(r"^Transcript [1-4]Q\d{2}\.pdf$", re.IGNORECASE)


def _detect_ticker_from_filename(name: str) -> tuple[str | None, list[str]]:
    """Filename prefix → ticker, for the small set of upload-naming conventions
    we actually see (RBRK-*, ServiceNow-*, novo-nordisk-*, Wix CDN hex names,
    NU's `NQYY Results Presentation` / `Transcript NQYY` convention). Hex/UUID
    names without a known prefix fall through to content detection.
    """
    upper = name.upper()
    if upper.startswith("RBRK-") or upper.startswith("RBRK_"):
        return "RBRK", ["filename_prefix:RBRK"]
    if upper.startswith("SERVICENOW") or upper.startswith("ER-Q"):
        return "NOW", ["filename_prefix:ServiceNow"]
    if upper.startswith("NOVO-NORDISK") or upper.startswith("NOVO_NORDISK"):
        return "NVO", ["filename_prefix:novo-nordisk"]
    if _RX_WIX_CDN_PREFIX.match(name):
        return "WIX", [f"filename_wix_cdn:{name[:7]}"]
    if _RX_NU_RESULTS_DECK.match(name) or _RX_NU_TRANSCRIPT_FILE.match(name):
        return "NU", ["filename_pattern:nu_default_naming"]
    return None, []


_RX_FILENAME_ANNUAL_REPORT = re.compile(r"annual[-_\s]?report", re.IGNORECASE)


def _detect_doc_type(
    text: str,
    ext: str,
    filename: str = "",
) -> tuple[DocType | None, list[str]]:
    """Apply doc-type rules. xlsx files always classify as IR_SUPPLEMENT.

    Strategy: scan **all** rules and pick the one whose pattern appears
    earliest in the text. Rule order in `_DOC_TYPE_RULES` is the tiebreaker
    when two rules match at the same position. This handles MELI press
    releases that include the "To our Shareholders" letter as a later section
    of the same PDF — the press-release dateline appears first, so press
    release wins; a standalone shareholder letter has "To our Shareholders"
    at position 0, so it wins.

    pypdf often emits glyphs without spaces ("EarningsCall", "PreparedRemarks").
    The squashed rule set runs only when no full-text rule matched at all.

    The filename is consulted as a final fallback so unambiguous cases like
    `novo-nordisk-annual-report-2025.pdf` don't depend on regex hits in the
    body of the PDF (where "Annual Report" may not appear verbatim).
    """
    if ext == ".xlsx":
        return DocType.IR_SUPPLEMENT, ["xlsx_extension"]

    earliest_pos: int | None = None
    earliest_idx: int | None = None
    earliest_match: tuple[DocType, str, str] | None = None
    for idx, (doc_type, rx, label) in enumerate(_DOC_TYPE_RULES):
        m = rx.search(text)
        if m is None:
            continue
        pos = m.start()
        if (
            earliest_pos is None
            or pos < earliest_pos
            or (pos == earliest_pos and idx < earliest_idx)  # type: ignore[operator]
        ):
            earliest_pos = pos
            earliest_idx = idx
            earliest_match = (doc_type, label, m.group(0))
    if earliest_match is not None:
        doc_type, label, hit = earliest_match
        return doc_type, [f"{label}:{hit!r}@{earliest_pos}"]

    flat = re.sub(r"\s+", "", text).lower()
    for doc_type, rx, label in _DOC_TYPE_SQUASHED_RULES:
        m = rx.search(flat)
        if m:
            return doc_type, [f"{label}_squashed:{m.group(0)!r}"]
    if filename and _RX_FILENAME_ANNUAL_REPORT.search(filename):
        return DocType.IR_INVESTOR_UPDATE, ["filename_annual_report"]
    return None, []


def _detect_period(
    text: str,
    ticker_calendar: str,
) -> tuple[tuple[int, int] | None, list[str]]:
    """Find (year, quarter) in text. NVO H1/9M/FY mapped per the directive.

    Returns the *fiscal-year-and-quarter* pair the issuer would label this
    period with — `_period_end_for()` then converts to a calendar period_end.
    """
    if ticker_calendar == _CAL_NVO:
        m = _RX_NVO_PERIOD.search(text)
        if m:
            phrase = m.group("period").lower().replace("\xa0", " ")
            year = int(m.group("y"))
            if "three months" in phrase:
                return (year, 1), [f"nvo_period:'three months {year}'"]
            if "six months" in phrase:
                return (year, 2), [f"nvo_period:'six months {year}'"]
            if "nine months" in phrase:
                return (year, 3), [f"nvo_period:'nine months {year}'"]
            if "full year" in phrase:
                return (year, 4), [f"nvo_period:'full year {year}'"]

    m = _RX_QUARTER_FULL_YEAR.search(text)
    if m:
        q = _WORD_TO_Q[m.group("word").lower()]
        y = int(m.group("y"))
        return (y, q), [f"quarter_full_year:{m.group(0)!r}"]

    m = _RX_QUARTER_ENDED_DATE.search(text)
    if m:
        q = _MONTH_TO_Q[m.group("m").lower()]
        y = int(m.group("y"))
        return (y, q), [f"quarter_ended_date:{m.group(0)!r}"]

    m = _RX_DATE_QUARTER_ENDED.search(text)
    if m:
        q = _MONTH_TO_Q[m.group("m").lower()]
        y = int(m.group("y"))
        return (y, q), [f"date_quarter_ended:{m.group(0)!r}"]

    m = _RX_FY_QY.search(text)
    if m:
        q = int(m.group("q"))
        y = _yy_to_yyyy(int(m.group("y")))
        return (y, q), [f"fy_qy:{m.group(0)!r}"]

    m = _RX_QUARTER_FY.search(text)
    if m:
        q = _WORD_TO_Q[m.group("word").lower()]
        y = _yy_to_yyyy(int(m.group("y")))
        return (y, q), [f"fiscal_word:{m.group(0)!r}"]

    m = _RX_QUARTER_FULL.search(text)
    if m:
        q = _WORD_TO_Q[m.group("word").lower()]
        y = _yy_to_yyyy(int(m.group("y")))
        return (y, q), [f"quarter_word:{m.group(0)!r}"]

    m = _RX_QUARTER_APOSTROPHE.search(text)
    if m:
        q = int(m.group("q"))
        y = _yy_to_yyyy(int(m.group("yy")))
        return (y, q), [f"quarter_apostrophe:{m.group(0)!r}"]

    m = _RX_QUARTER_SPACE.search(text)
    if m:
        q = int(m.group("q"))
        y = int(m.group("y"))
        return (y, q), [f"quarter_space:{m.group(0)!r}"]

    flat = re.sub(r"\s+", "", text).lower()
    m = re.search(r"q(?P<q>[1-4])(?P<y>20\d{2})", flat)
    if m:
        return (int(m.group("y")), int(m.group("q"))), [f"quarter_squashed:{m.group(0)!r}"]
    m = re.search(r"(?P<word>first|second|third|fourth)quarter(?:fiscal)?(?P<y>20\d{2})", flat)
    if m:
        q = _WORD_TO_Q[m.group("word")]
        return (int(m.group("y")), q), [f"quarter_word_squashed:{m.group(0)!r}"]

    return None, []


def _filename_period_hint(name: str) -> tuple[tuple[int, int] | None, list[str]]:
    """Best-effort period hint from filename — used to disambiguate when
    content gives a less-specific match (e.g. cover slide doesn't say which Q).
    """
    rx = re.compile(r"(?P<a>[1-4])Q(?P<b>\d{2})", re.IGNORECASE)
    m = rx.search(name)
    if m:
        q = int(m.group("a"))
        y = _yy_to_yyyy(int(m.group("b")))
        return (y, q), [f"filename_period:{m.group(0)!r}"]
    rx2 = re.compile(r"\bq(?P<q>[1-4])[-_](?P<y>20\d{2})\b", re.IGNORECASE)
    m2 = rx2.search(name)
    if m2:
        return (int(m2.group("y")), int(m2.group("q"))), [f"filename_period:{m2.group(0)!r}"]
    rx3 = re.compile(r"\bq(?P<q>[1-4])[-_](?P<yy>\d{2})\b", re.IGNORECASE)
    m3 = rx3.search(name)
    if m3:
        y = _yy_to_yyyy(int(m3.group("yy")))
        return (y, int(m3.group("q"))), [f"filename_period:{m3.group(0)!r}"]
    rx4 = re.compile(r"annual[-_\s]report[-_\s](?P<y>20\d{2})", re.IGNORECASE)
    m4 = rx4.search(name)
    if m4:
        return (int(m4.group("y")), 4), [f"filename_annual:{m4.group(0)!r}"]
    return None, []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_ir_file(path: Path) -> CategorizationResult | CategorizationFailure:
    """Classify a single IR-uploads file. Pure function — only reads the file.

    Returns a `CategorizationResult` when ticker, doc_type, and period are all
    determined. Otherwise a `CategorizationFailure` carrying the partial
    evidence so the user can repair (rename, delete, or extend the registry).
    """
    ext = path.suffix.lower()
    if ext not in {".pdf", ".xlsx"}:
        return CategorizationFailure(
            reason=f"unsupported_extension:{ext!r}",
            text_sample="",
        )

    try:
        raw_text = fingerprint(path)
    except Exception as e:  # pypdf / openpyxl can throw a wide tree
        return CategorizationFailure(
            reason=f"fingerprint_error:{type(e).__name__}:{e}",
            text_sample="",
        )

    # Many issuer PDFs land with multi-space inter-word formatting after pypdf
    # extraction (e.g. "Nu  Holdings"). Normalize whitespace once so substring
    # rules don't have to multiply-match every variant.
    text = re.sub(r"[ \t]+", " ", raw_text)

    file_ticker, file_ev = _detect_ticker_from_filename(path.name)
    content_ticker, content_ev = _detect_ticker(text)
    ticker_evidence = file_ev + content_ev

    if file_ticker and content_ticker and file_ticker != content_ticker:
        return CategorizationFailure(
            reason=f"ticker_conflict:filename={file_ticker} content={content_ticker}",
            text_sample=text[:600],
            ticker_guess=file_ticker,
        )
    ticker = file_ticker or content_ticker
    if ticker is None:
        return CategorizationFailure(
            reason="ticker_unidentified",
            text_sample=text[:600],
        )

    doc_type, doc_ev = _detect_doc_type(text, ext, path.name)
    if doc_type is None:
        return CategorizationFailure(
            reason="doc_type_unidentified",
            text_sample=text[:600],
            ticker_guess=ticker,
        )

    cal = _calendar_for(ticker)
    fname_yq, fname_period_ev = _filename_period_hint(path.name)
    content_yq, content_period_ev = _detect_period(text, cal)

    # Filename takes precedence (it disambiguates near-identical cover slides).
    yq = fname_yq or content_yq
    period_evidence = fname_period_ev + content_period_ev
    if yq is None:
        return CategorizationFailure(
            reason="period_unidentified",
            text_sample=text[:600],
            ticker_guess=ticker,
            doc_type_guess=doc_type,
        )

    year, q = yq
    period_end = _period_end_for(cal, year, q)
    period_label = _period_label(cal, year, q)

    confidence = (
        Confidence.HIGH
        if (file_ev and (fname_yq or content_yq))
        or (content_ev and content_yq)
        else Confidence.MEDIUM
    )

    return CategorizationResult(
        ticker=ticker,
        doc_type=doc_type,
        period_end=period_end,
        period_label=period_label,
        confidence=confidence,
        ticker_evidence=ticker_evidence,
        doc_type_evidence=doc_ev,
        period_evidence=period_evidence,
    )


def _period_label(cal: str, year: int, q: int) -> str:
    """Render a human-readable label, e.g. 'Q3 2024' or 'FY26 Q1'."""
    if cal in (_CAL_VEEV, _CAL_RUBRIK):
        return f"FY{year % 100:02d} Q{q}"
    if cal == _CAL_NVO:
        return ["Q1", "H1", "9M", "FY"][q - 1] + f" {year}"
    return f"Q{q} {year}"


def canonical_path(
    root: Path,
    result: CategorizationResult,
    sha256: str,
    suffix: str,
) -> Path:
    """ir_documents/{TICKER}/{period_end_iso}/{doc_type}__{sha8}.{ext}.

    The sha8 prefix means rerunning with a modified file produces a new path,
    not an overwrite — preserving the supersession invariant from §2 of the
    data-provenance directive.
    """
    short = sha256[:8]
    return (
        root
        / result.ticker
        / result.period_end.isoformat()
        / f"{result.doc_type.value}__{short}{suffix.lower()}"
    )


def iter_uncategorized_files(root: Path) -> list[Path]:
    """Files at the root of `ir_documents/` that need categorization.

    Already-categorized files live under `ir_documents/{TICKER}/{period}/`.
    The `_unsorted/` quarantine and any other subdirs are skipped.
    """
    out: list[Path] = []
    for p in sorted(root.iterdir()):
        if p.is_dir():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in {".pdf", ".xlsx"}:
            continue
        out.append(p)
    return out


__all__: Sequence[str] = (
    "ISSUER_REGISTRY",
    "CategorizationFailure",
    "CategorizationResult",
    "Confidence",
    "canonical_path",
    "classify_ir_file",
    "fingerprint",
    "fingerprint_pdf",
    "fingerprint_xlsx",
    "iter_uncategorized_files",
    "sha256_of",
)
