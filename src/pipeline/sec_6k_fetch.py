"""Locate + fetch a single 6-K exhibit for an FPI ticker (docs/design/
segment_quarterly_framework.md §1.1, Phase 3 — ``fpi_6k`` route).

Why this exists: ``pipeline.sec_xbrl.upsert_accession_documents`` already
registers a ``documents`` row (``doc_type='sec_6k'``) for every 6-K accession
a CIK_MAP ticker has filed, but that row's ``file_path`` is a synthetic
pointer into the aggregated companyfacts.json response
(``data/historical/sec/{T}_companyfacts.json#accn=...``) — there is no real
per-accession HTML file behind it, because that pipeline only cares about
XBRL companyfacts tags, not filing narrative. The segment/product-revenue
tables this framework needs (Phase-3 spike, docs/design/
segment_quarterly_framework.md) live in 6-K EXHIBIT narrative text (an HTML
document, sometimes an image-scanned slide deck — see ``fetch_6k_exhibit_text``'s
image-only guard) that has to be downloaded and read separately.

Per-ticker exhibit-filename heuristics (`_TICKER_EXHIBIT_HINT`) are seeded
ONLY for tickers this framework's spike actually validated (NU, NVO, WIX) —
see ``compute.segment_quarterly_6k`` for the classification table. Do not add
a ticker here without first confirming its exhibit-naming convention the same
way the spike did (fetch one live 6-K, inspect ``index.json``, check the
exhibit is real narrative/table HTML and not an image-scanned slide deck).

WIX validated 2026-07-25 against its live 1Q26 6-K (accession
0001628280-26-034370, filed 2026-05-13, 44 days after the March 31 2026
quarter-end — the same lag band NU/NVO showed): ``index.json`` lists
``firstquarter2026results.htm`` (the earnings-release exhibit, 630KB raw /
32.8KB stripped text, density 0.052 — well above ``_MIN_TEXT_DENSITY``) and a
10KB ``wix-6xkxfirstquarter2026.htm`` (the bare 6-K cover letter, no
financials). The results exhibit is real narrative + tabular text with
per-line-of-business revenue and bookings breakdowns (Creative Subscriptions /
Business Solutions / Transaction / Partners), not an image-scanned deck. Cross-
checked against three more quarters' index.json (2026-03-04 Q4/FY,
2025-05-21 Q1'25, 2025-08-06 Q2'25): the earnings-release exhibit always
starts with a quarter word ("firstquarter...", "fourthquarterandfullyear...")
regardless of the trailing suffix ("results", a bare year, "andfullyear...");
WIX's OTHER 6-Ks in the same window (share-repurchase announcements, AGM
proxy/results, all named "pr...-6xk.htm" / "wix-...agm...htm" / "wix-6xkx...")
never start that way, so the hint does not need the filing-window narrowing
NU/NVO's exhibits already get from ``_FILING_WINDOW_DAYS``.

CIK resolution reuses ``pipeline.sec_xbrl.CIK_MAP`` (the existing hand-curated
resolver — segment_quarterly_framework.md §7 risk #2 flagged checking for one
before writing a new one; it already covers every 20-F/40-F name on the
roster).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from log_redact import redact as _redact
from pipeline.sec_xbrl import CIK_MAP
from sec_identity import sec_user_agent
from table_extractors.period_axis import NominalQuarter, expected_period_ends

#: Declared to the SEC via ``sec_identity`` so this project has ONE contact,
#: not one per module. Override with the EDGAR_USER_AGENT env var.
USER_AGENT = sec_user_agent()
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/"
_TIMEOUT = (10, 30)
_DELAY_S = 0.25
# 6-Ks reporting a quarter typically land 25-60 days after the quarter-end
# for this roster (NU: ~44d, NVO: ~36d, observed during the Phase-3 spike) —
# window padded generously since this is the only anchor we have without a
# per-ticker earnings-calendar cross-check.
_FILING_WINDOW_DAYS = (10, 100)

# Per-ticker "which exhibit in a candidate 6-K is the financial-statements /
# earnings-release exhibit" heuristic, validated against exactly one quarter
# each during the Phase-3 spike (2026-07-18, NU 1Q26 / NVO 1Q26 filings) plus
# WIX (2026-07-25, 1Q26). Filename conventions are filing-agent-specific and
# may drift across quarters or years -- if this regex stops matching for a
# ticker already marked "supported" in segment_quarterly_6k._TICKER_6K_STATUS,
# that's a fpi_6k_exhibit_not_located coverage row, not a silent guess.
_TICKER_EXHIBIT_HINT: dict[str, re.Pattern[str]] = {
    # "nufs1q26_6k.htm" -- Nu Financial Statements <quarter><yy>.
    "NU": re.compile(r"^nufs\d.*_6k\.htm$", re.IGNORECASE),
    # "caq12026.htm" -- Novo Nordisk "Company Announcement" quarterly report.
    "NVO": re.compile(r"^caq?\d[\dA-Za-z]*\.htm$", re.IGNORECASE),
    # "firstquarter2026results.htm" / "fourthquarterandfullyear20.htm" -- Wix's
    # earnings-release exhibit always opens with the quarter word regardless of
    # the trailing suffix; confirmed distinct from WIX's OTHER 6-K exhibits in
    # the same accession window (share-repurchase and AGM filings), which are
    # all named "pr...-6xk.htm" / "wix-...agm...htm" / "wix-6xkx...".
    "WIX": re.compile(r"^(?:first|second|third|fourth)quarter", re.IGNORECASE),
}

# Below this many plain-text characters per HTML byte, treat the exhibit as
# image-scanned (slide-deck JPGs referenced by <img>, no real text content) --
# this is exactly the shape the spike found for ASML's quarterly financial
# statements exhibit (12 <img> slides, ~1500 chars of boilerplate). Guards
# future tickers too, not just the 3 the spike sampled.
_MIN_TEXT_DENSITY = 0.02
_MIN_TEXT_CHARS = 800


@dataclass(slots=True)
class LocatedExhibit:
    ticker: str
    cik: str
    accession: str
    filing_date: str
    exhibit_filename: str
    exhibit_url: str


@dataclass(slots=True)
class FetchedExhibit:
    located: LocatedExhibit
    raw_html: str
    plain_text: str
    is_image_only: bool


def resolve_cik(ticker: str) -> str | None:
    """Ticker -> zero-padded CIK via the existing hand-curated CIK_MAP.

    Returns None for tickers absent from the map (never guesses)."""
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


def _candidate_6k_filings(cik: str, session: requests.Session) -> list[tuple[str, str]]:
    """[(accession, filingDate)] for every 6-K/6-K-A in the submissions
    'recent' window (most recent ~1000 filings — sufficient for the
    going-forward quarterly gap this framework fills; historical backfill
    beyond that window would need the submissions 'files' pagination, out of
    scope for Phase 3's spike-validated roster)."""
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
    out: list[tuple[str, str]] = []
    for i in range(min(len(forms_l), len(dates_l), len(accns_l))):
        if str(forms_l[i]).upper() in ("6-K", "6-K/A"):
            out.append((str(accns_l[i]), str(dates_l[i])))
    return out


def locate_6k_exhibit(
    ticker: str,
    *,
    quarter: NominalQuarter,
    year: int,
    fye_month: int = 12,
    fye_day: int = 31,
    session: requests.Session | None = None,
) -> LocatedExhibit | None:
    """Find the 6-K accession + exhibit filename for one nominal fiscal
    quarter of an FPI ticker, using the ticker's exhibit-filename hint
    (``_TICKER_EXHIBIT_HINT``). Returns None when the ticker has no hint
    registered, no CIK resolves, or no candidate 6-K in the expected filing
    window has a matching exhibit -- never guesses at an exhibit."""
    ticker = ticker.upper()
    hint = _TICKER_EXHIBIT_HINT.get(ticker)
    if hint is None:
        return None
    cik = resolve_cik(ticker)
    if cik is None:
        return None

    sess = session or requests.Session()
    current_end, _prior = expected_period_ends(quarter, year, fye_month, fye_day)
    window_start = current_end.toordinal() + _FILING_WINDOW_DAYS[0]
    window_end = current_end.toordinal() + _FILING_WINDOW_DAYS[1]

    candidates = _candidate_6k_filings(cik, sess)
    for accn, filing_date in candidates:
        try:
            fdate = date.fromisoformat(filing_date)
        except ValueError:
            continue
        if not (window_start <= fdate.toordinal() <= window_end):
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
        for item in cast("list[object]", items):
            if not isinstance(item, dict):
                continue
            name = cast("dict[str, object]", item).get("name")
            if not isinstance(name, str):
                continue
            if hint.match(name):
                exhibit_url = (
                    _ARCHIVES_BASE.format(cik_int=int(cik), accn_nodash=accn_nodash) + name
                )
                return LocatedExhibit(
                    ticker=ticker,
                    cik=cik,
                    accession=accn,
                    filing_date=filing_date,
                    exhibit_filename=name,
                    exhibit_url=exhibit_url,
                )
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_WS_RE = re.compile(r"&nbsp;|&#\d+;")
_WS_RE = re.compile(r"\s+")


def _strip_html(raw_html: str) -> str:
    plain = _TAG_RE.sub(" ", raw_html)
    plain = _ENTITY_WS_RE.sub(" ", plain)
    return _WS_RE.sub(" ", plain).strip()


def fetch_6k_exhibit_text(
    located: LocatedExhibit, *, session: requests.Session | None = None
) -> FetchedExhibit | None:
    """Download one exhibit and strip to plain text. Returns None on a
    network/HTTP failure (caller records ``source_missing``). Sets
    ``is_image_only=True`` when the stripped text is too sparse relative to
    the raw HTML size to contain a real narrative table -- the shape the
    spike found for ASML's quarterly exhibit (slide-deck JPGs, no OCR here)."""
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
    return FetchedExhibit(
        located=located, raw_html=raw_html, plain_text=plain_text, is_image_only=is_image_only
    )


def register_6k_document(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fetched: FetchedExhibit,
    repo_root: Path,
    period_end: datetime,
) -> int:
    """Persist the raw exhibit HTML to ``data/historical/sec/`` and insert one
    ``documents`` row (``doc_type='sec_6k'``, ``source_type='sec_xbrl'`` --
    the same source_type ``pipeline.sec_xbrl.upsert_accession_documents``
    already uses for every SEC-EDGAR-origin document, tier=SEC_OFFICIAL; this
    IS a bona fide SEC-official primary filing, the LLM-extraction method is
    recorded on the segment_dimensions rows' own extracted_by/method_version,
    not by picking a different document source_type -- same convention
    ``compute.segment_crosstabs_llm`` uses for its ``fmp_10k_json`` documents).

    Idempotent on sha256 (mirrors ``upsert_accession_documents``): re-running
    against the same exhibit content returns the existing row's id."""
    sha256 = hashlib.sha256(fetched.raw_html.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha256,)
    ).fetchone()
    if existing is not None:
        return int(existing[0])

    out_dir = repo_root / "data" / "historical" / "sec"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}_6k_{fetched.located.filing_date}.html"
    out_path.write_text(fetched.raw_html, encoding="utf-8")
    rel_path = str(out_path.relative_to(repo_root)).replace("\\", "/")

    cur = conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_start, period_end, file_path, "
        " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
        " parent_document_id, accession_number, filing_date) "
        "VALUES (?, 'sec_xbrl', 'sec_6k', NULL, ?, ?, ?, ?, 'ok', 200, ?, ?, NULL, ?, ?)",
        (
            ticker.upper(),
            period_end,
            rel_path,
            sha256,
            datetime.now(),
            len(fetched.raw_html.encode("utf-8")),
            fetched.located.exhibit_url,
            fetched.located.accession,
            fetched.located.filing_date,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0
