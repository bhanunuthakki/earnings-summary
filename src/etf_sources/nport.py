"""SEC N-PORT holdings spine — full ETF portfolios from EDGAR, any issuer.

Form NPORT-P is the monthly portfolio report every registered US fund files;
the most recent quarter-end report is public with a ~60-day lag. For the
factor/thematic ETFs this lane evaluates (AVDV / AVUV / VWO — slow-churn
rules-based baskets), that staleness is acceptable: the *strategy* moves
slower than the disclosure. Freshness, when an issuer publishes it, comes
from the ``issuer_registry`` overlay.

Resolution chain (all endpoints are the same EDGAR rungs
``discovery/thirteenf.py`` already uses — UA, throttle, degrade conventions
mirrored from there):

  1. ``www.sec.gov/files/company_tickers_mf.json`` — the fund-ticker map
     (cik, seriesId, classId, symbol) → the ETF's trust CIK + series id.
  2. ``data.sec.gov/submissions/CIK##########.json`` — the trust's filing
     index → recent NPORT-P accessions (a trust files one NPORT-P per series,
     so several probes may be needed to hit the right series).
  3. each accession's ``primary_doc.xml`` → parse; keep the one whose
     ``<seriesId>`` matches.

Schema-drift defense per AGENTS.md: a document that *fetches* but does not
*parse* (no genInfo, no invstOrSecs) raises ``NportParseError``; the ingest
wrapper dumps the raw XML to ``.tmp/etf_nport/`` and halts loudly instead of
guess-fixing. A document for the wrong series is not an error — it's skipped.

``pctVal`` is percent-of-net-assets (1.23 = 1.23%); rows are normalized to
the repo-wide decimal-fraction convention (0.0123) matching ``etf_holdings``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

import requests

from models.instruments import EtfHolding
from ticker_validation import safe_ticker

log = logging.getLogger(__name__)

MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EDGAR_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{name}"

DEFAULT_USER_AGENT = "earnings-summary/1.0 (+https://github.com/bhanunuthakki/earnings-summary)"
MIN_REQUEST_INTERVAL_S = 0.15  # SEC fair-access; same spacing as thirteenf/news
REQUEST_TIMEOUT = 30

#: How many recent NPORT-P accessions to probe looking for the right series.
#: A large trust (e.g. American Century ETF Trust) interleaves dozens of
#: series; quarterly public filings put the newest report for any one series
#: within the first ~2 filing cycles.
MAX_ACCESSION_PROBES = 24

SOURCE = "nport"


class NportParseError(ValueError):
    """A fetched NPORT-P primary document did not match the expected schema."""


@dataclass(frozen=True, slots=True)
class FundRef:
    """One row of the EDGAR fund-ticker map."""

    ticker: str
    cik: int
    series_id: str
    class_id: str


@dataclass(frozen=True, slots=True)
class NportReport:
    """A parsed NPORT-P primary document for one series."""

    series_id: str
    rep_period_date: date  # <repPdDate> — the portfolio as-of date
    holdings: list[EtfHolding]
    accession: str
    n_rows_skipped: int  # invstOrSec elements that lacked even a name/value


# ---------------------------------------------------------------------------
# Throttled EDGAR GET (mirrors discovery/thirteenf.py)
# ---------------------------------------------------------------------------

_last_request = 0.0


def _throttle() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
    _last_request = time.monotonic()


def _sec_get(url: str, *, user_agent: str, as_json: bool) -> object | str | None:
    """One throttled SEC GET. Returns parsed JSON / text, or None on any
    failure (callers degrade; only a *parse* failure of a fetched primary
    document is loud)."""
    _throttle()
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning({"event": "nport_fetch_failed", "url": url, "error": str(exc)[:200]})
        return None
    if as_json:
        try:
            return cast("object", resp.json())
        except ValueError:
            return None
    return resp.text


# ---------------------------------------------------------------------------
# Fund resolution — ticker → (trust CIK, series id)
# ---------------------------------------------------------------------------


def resolve_fund(ticker: str, *, user_agent: str = DEFAULT_USER_AGENT) -> FundRef | None:
    """Look the ETF up in the EDGAR fund-ticker map.

    The payload is column-oriented: ``{"fields": ["cik","seriesId","classId",
    "symbol"], "data": [[...], ...]}``. Returns None when the symbol isn't in
    the map (non-US fund, brand-new listing) — the caller reports the miss.
    """
    payload = _sec_get(MF_TICKERS_URL, user_agent=user_agent, as_json=True)
    return _resolve_fund_from_payload(payload, ticker)


def _resolve_fund_from_payload(payload: object, ticker: str) -> FundRef | None:
    """Pure lookup half of :func:`resolve_fund` (fixture-testable)."""
    if not isinstance(payload, dict):
        return None
    d = cast("dict[str, object]", payload)
    fields_raw, data_raw = d.get("fields"), d.get("data")
    if not isinstance(fields_raw, list) or not isinstance(data_raw, list):
        return None
    fields = [str(f) for f in cast("list[object]", fields_raw)]
    try:
        i_cik = fields.index("cik")
        i_series = fields.index("seriesId")
        i_class = fields.index("classId")
        i_symbol = fields.index("symbol")
    except ValueError:
        return None
    upper = ticker.upper()
    for row_obj in cast("list[object]", data_raw):
        if not isinstance(row_obj, list):
            continue
        row = cast("list[object]", row_obj)
        width = max(i_cik, i_series, i_class, i_symbol)
        if len(row) <= width:
            continue
        if str(row[i_symbol]).upper() != upper:
            continue
        try:
            cik = int(str(row[i_cik]))
        except ValueError:
            continue
        return FundRef(
            ticker=upper,
            cik=cik,
            series_id=str(row[i_series]).upper(),
            class_id=str(row[i_class]).upper(),
        )
    return None


# ---------------------------------------------------------------------------
# Pure parsing — one primary_doc.xml → NportReport
# ---------------------------------------------------------------------------


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(parent: ET.Element, name: str) -> str | None:
    for el in parent.iter():
        if _localname(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return None


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return None


def parse_nport(
    xml_text: str,
    ticker: str,
    *,
    accession: str = "",
    fetched_at: datetime | None = None,
) -> NportReport:
    """Parse an NPORT-P primary document into typed holdings rows.

    Namespace-agnostic (EDGAR renders the nport namespace with and without
    prefixes). Raises :class:`NportParseError` when the document is not an
    NPORT-P at all (unparseable XML, missing genInfo/seriesId/repPdDate, or
    zero ``invstOrSec`` elements) — the schema-drift halt. Individual rows
    missing optional fields are kept sparse; rows with neither a name nor a
    value are counted in ``n_rows_skipped``.
    """
    ts = fetched_at or datetime.now()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NportParseError(f"unparseable XML: {exc}") from exc

    gen_info: ET.Element | None = None
    for el in root.iter():
        if _localname(el.tag) == "genInfo":
            gen_info = el
            break
    if gen_info is None:
        raise NportParseError("no <genInfo> element — not an NPORT-P primary document")
    series_id = _child_text(gen_info, "seriesId")
    rep_pd_raw = _child_text(gen_info, "repPdDate")
    if not series_id or not rep_pd_raw:
        raise NportParseError("genInfo lacks seriesId/repPdDate")
    try:
        rep_period = date.fromisoformat(rep_pd_raw[:10])
    except ValueError as exc:
        raise NportParseError(f"bad repPdDate {rep_pd_raw!r}") from exc

    rows: list[EtfHolding] = []
    skipped = 0
    seen_any = False
    for sec in root.iter():
        if _localname(sec.tag) != "invstOrSec":
            continue
        seen_any = True
        fields: dict[str, str] = {}
        for el in sec.iter():
            ln = _localname(el.tag)
            if ln in ("name", "title", "cusip", "balance", "valUSD", "pctVal", "invCountry"):
                if el.text and el.text.strip() and ln not in fields:
                    fields[ln] = el.text.strip()
            elif ln in ("ticker", "isin") and f"id_{ln}" not in fields:
                # identifiers carry the value as an attribute, not text
                value = el.get("value")
                if value and value.strip():
                    fields[f"id_{ln}"] = value.strip()
        name = fields.get("name") or fields.get("title")
        val_usd = _to_float(fields.get("valUSD"))
        if name is None and val_usd is None:
            skipped += 1
            continue
        pct = _to_float(fields.get("pctVal"))
        raw_ticker = fields.get("id_ticker")
        constituent = raw_ticker.upper()[:16] if raw_ticker else None
        rows.append(
            EtfHolding(
                ticker=ticker.upper(),
                as_of_date=rep_period,
                constituent_ticker=constituent,
                name=name,
                weight_pct=pct / 100.0 if pct is not None else None,
                shares_held=_to_float(fields.get("balance")),
                market_value_usd=val_usd,
                sector=None,  # NPORT carries assetCat, not GICS sectors
                asset_class=None,
                rank_position=None,  # assigned after the weight sort below
                country=fields.get("invCountry"),
                source=SOURCE,
                fetched_at=ts,
            )
        )
    if not seen_any:
        raise NportParseError("no <invstOrSec> elements — empty or foreign document")

    rows.sort(key=lambda h: (h.weight_pct is None, -(h.weight_pct or 0.0)))
    ranked = [h.model_copy(update={"rank_position": i}) for i, h in enumerate(rows, start=1)]
    return NportReport(
        series_id=series_id.upper(),
        rep_period_date=rep_period,
        holdings=ranked,
        accession=accession,
        n_rows_skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Fetch orchestration — find the series' newest report
# ---------------------------------------------------------------------------


def _recent_nport_accessions(
    submissions: object, limit: int = MAX_ACCESSION_PROBES
) -> list[tuple[str, str, str]]:
    """The most recent (filing_date, accession, primary_document) NPORT-P
    filings, newest first."""
    if not isinstance(submissions, dict):
        return []
    filings = cast("dict[str, object]", submissions).get("filings")
    recent = cast("dict[str, object]", filings).get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return []
    r = cast("dict[str, object]", recent)

    def _col(key: str) -> list[str]:
        v = r.get(key)
        return [str(x) for x in cast("list[object]", v)] if isinstance(v, list) else []

    forms = _col("form")
    accs = _col("accessionNumber")
    dates = _col("filingDate")
    docs = _col("primaryDocument")
    hits = [
        (
            dates[i] if i < len(dates) else "",
            accs[i] if i < len(accs) else "",
            docs[i] if i < len(docs) else "primary_doc.xml",
        )
        for i, f in enumerate(forms)
        if f == "NPORT-P" and i < len(accs)
    ]
    # The submissions index lists the XSL-RENDERED path for NPORT-P (e.g.
    # "xslFormNPORT-P_X01/primary_doc.xml"), which serves HTML. The raw XML is
    # the bare basename in the accession folder — normalize to it (caught by
    # the AVDV end-to-end: the rendered doc halted the parser).
    hits = [(d, a, (doc.rsplit("/", 1)[-1] or "primary_doc.xml")) for d, a, doc in hits if d and a]
    hits.sort(reverse=True)
    return hits[:limit]


def fetch_latest_report(
    ref: FundRef,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    tmp_dir: Path | None = None,
) -> NportReport | None:
    """The newest NPORT-P report for the fund's series, or None.

    None means "couldn't find it" (network trouble, series not in the recent
    filing window) — the caller degrades. A document that fetched but failed
    to parse raises :class:`NportParseError` after dumping the raw XML to
    ``tmp_dir`` (default ``.tmp/etf_nport/``) for inspection — the directive
    needs updating, not a retry.
    """
    submissions = _sec_get(
        EDGAR_SUBMISSIONS_URL.format(cik=ref.cik), user_agent=user_agent, as_json=True
    )
    accessions = _recent_nport_accessions(submissions)
    if not accessions:
        log.warning({"event": "nport_no_accessions", "ticker": ref.ticker, "cik": ref.cik})
        return None
    for _filing_date, accession, primary_doc in accessions:
        acc = accession.replace("-", "")
        url = EDGAR_FILE_URL.format(cik_int=ref.cik, acc=acc, name=primary_doc)
        xml_text = _sec_get(url, user_agent=user_agent, as_json=False)
        if not isinstance(xml_text, str) or not xml_text.strip():
            continue
        head = xml_text.lstrip()[:200].lower()
        if head.startswith("<!doctype html") or head.startswith("<html"):
            # An HTML rendering, not the raw filing — a URL-shape surprise,
            # not schema drift: skip the accession rather than halting.
            log.warning({"event": "nport_html_not_xml", "ticker": ref.ticker, "url": url})
            continue
        try:
            report = parse_nport(xml_text, ref.ticker, accession=accession)
        except NportParseError as exc:
            safe_t = safe_ticker(ref.ticker)
            safe_acc = re.sub(r"[^a-zA-Z0-9]", "", acc) or "unknown_acc"
            dump_dir = tmp_dir or Path(".tmp") / "etf_nport"
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_path = dump_dir / f"{safe_t}_{safe_acc}.xml"
            dump_path.write_text(xml_text, encoding="utf-8")
            raise NportParseError(
                f"{ref.ticker} accession {accession}: {exc} — raw XML dumped to {dump_path}"
            ) from exc
        if report.series_id == ref.series_id:
            return report
        # Wrong series in the same trust — normal, keep probing.
    log.warning(
        {
            "event": "nport_series_not_found",
            "ticker": ref.ticker,
            "series_id": ref.series_id,
            "probed": len(accessions),
        }
    )
    return None


def fetch_holdings(
    ticker: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    tmp_dir: Path | None = None,
) -> NportReport | None:
    """Resolve + fetch in one call: the ticker's newest N-PORT report."""
    ref = resolve_fund(ticker, user_agent=user_agent)
    if ref is None:
        log.warning({"event": "nport_ticker_unresolved", "ticker": ticker.upper()})
        return None
    return fetch_latest_report(ref, user_agent=user_agent, tmp_dir=tmp_dir)


def summarize(report: NportReport) -> str:
    """One structured line for stage logs."""
    return json.dumps(
        {
            "event": "nport_report",
            "ticker": report.holdings[0].ticker if report.holdings else "?",
            "series_id": report.series_id,
            "as_of": report.rep_period_date.isoformat(),
            "rows": len(report.holdings),
            "rows_skipped": report.n_rows_skipped,
            "accession": report.accession,
        }
    )
