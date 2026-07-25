"""Fetch EDGAR primary documents by accession, with classified failures.

``filing_text_fetcher`` already fetches the *latest* 10-K (and DEF 14A / S-1)
for a ticker; longitudinal tracking needs the opposite shape — every accession
of several forms, walked historically — so this module adds the by-accession
lane rather than bending that one. CIK resolution and HTML stripping are
reused from the existing modules; only the enumeration and the error taxonomy
are new.

Failures are classified per AGENTS.md's error classes, because the three
demand different responses and collapsing them is how a run either spins on an
unfixable error or silently swallows a real one:

* ``HardStopError`` — 401/403. Retrying without a fix is pointless; the run
  surfaces it and stops.
* ``TickerNotResolvableError`` — no CIK for this ticker. A fact about ONE
  issuer (an ADR with no direct SEC presence), so the batch continues and the
  ticker gets a SOURCE_MISSING coverage row.
* ``TransientError`` — timeouts, connection errors, 429, 5xx. The orchestrator
  records ``FETCH_FAILED`` and continues to the next accession.
* ``SourceContractError`` — 404 and other 4xx on a URL we constructed, i.e.
  our understanding of the archive layout is wrong. Not retried, surfaced as
  drift.

SEC asks for a declared User-Agent and modest request rates; ``_DELAY_S``
matches the 0.25s ``pipeline.sec_6k_fetch`` already uses against the same host.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filings.models import (
    FilingForm,
    HardStopError,
    SourceContractError,
    TickerNotResolvableError,
    TransientError,
)

log = logging.getLogger(__name__)

USER_AGENT_DEFAULT = "EarningsSummary/1.0 (research@example.com)"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{doc}"
_TIMEOUT = (10, 60)
_DELAY_S = 0.25

#: EDGAR form strings mapped to our taxonomy. Amendments partition identically
#: to their base form, so they map to the same value rather than being skipped
#: — an amended 20-F is still a 20-F worth diffing.
_FORM_MAP: dict[str, FilingForm] = {
    "10-K": FilingForm.FORM_10K,
    "10-K/A": FilingForm.FORM_10K,
    "10-Q": FilingForm.FORM_10Q,
    "10-Q/A": FilingForm.FORM_10Q,
    "20-F": FilingForm.FORM_20F,
    "20-F/A": FilingForm.FORM_20F,
    "40-F": FilingForm.FORM_40F,
    "6-K": FilingForm.FORM_6K,
    "S-1": FilingForm.FORM_S1,
    "S-1/A": FilingForm.FORM_S1,
}

_ACCN_RX = re.compile(r"^\d{10}-\d{2}-\d{6}$")


@dataclass(frozen=True, slots=True)
class FilingRef:
    """One accession worth fetching."""

    ticker: str
    cik: str
    form: FilingForm
    form_raw: str
    accession: str
    filing_date: str
    report_date: str
    primary_document: str

    @property
    def url(self) -> str:
        return _ARCHIVE_URL.format(
            cik_int=int(self.cik),
            accn_nodash=self.accession.replace("-", ""),
            doc=self.primary_document,
        )

    @property
    def cache_name(self) -> str:
        slug = self.form.value.replace("-", "").lower()
        return f"{self.ticker}_{slug}_{self.accession.replace('-', '')}.txt"


def _classify_response(resp: requests.Response, url: str) -> None:
    """Raise the right error class for a non-200, or return for a 200."""
    if resp.status_code == 200:
        return
    if resp.status_code in (401, 403):
        raise HardStopError(
            f"SEC returned {resp.status_code} for {url} — check the declared "
            "User-Agent; retrying without a fix will keep failing"
        )
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientError(f"SEC returned {resp.status_code} for {url}")
    raise SourceContractError(f"SEC returned {resp.status_code} for {url}")


def _get(
    url: str, *, user_agent: str, session: requests.Session | None = None
) -> requests.Response:
    client = session or requests
    try:
        resp = client.get(url, headers={"User-Agent": user_agent}, timeout=_TIMEOUT)
    except requests.Timeout as exc:
        raise TransientError(f"timeout fetching {url}: {exc}") from exc
    except requests.RequestException as exc:
        raise TransientError(f"network error fetching {url}: {exc}") from exc
    _classify_response(resp, url)
    return resp


def resolve_cik(ticker: str, *, user_agent: str = USER_AGENT_DEFAULT) -> str:
    """Ticker → zero-padded CIK, preferring the hand-curated map.

    ``pipeline.sec_xbrl.CIK_MAP`` already covers every 20-F/40-F name on the
    roster; the ``insider_transactions`` lookup is the fallback for anything
    else.

    Raises ``TickerNotResolvableError`` when neither resolves. That is a fact
    about ONE issuer — an ADR such as NTDOY files nothing directly with the SEC
    — so the caller records ``SOURCE_MISSING`` for it and carries on with the
    rest of the batch. (It was a ``HardStopError`` until a 38-ticker eval run
    aborted on reaching the first such name.) A lookup that fails for
    infrastructure reasons is still transient, and is raised as such.
    """
    upper = ticker.strip().upper()
    try:
        from pipeline.sec_xbrl import CIK_MAP

        mapped = CIK_MAP.get(upper)
        if mapped:
            return mapped.zfill(10)
    except ImportError:  # pragma: no cover — sec_xbrl is always importable in-repo
        log.debug({"event": "cik_map_unavailable", "ticker": upper})

    try:
        from insider_transactions import _lookup_cik_for_ticker

        found = _lookup_cik_for_ticker(upper, user_agent=user_agent)
    except Exception as exc:
        # The lookup itself broke (network, SEC ticker file unavailable) —
        # transient, not a statement about this issuer.
        raise TransientError(f"CIK lookup failed for {upper}: {exc}") from exc
    if not found:
        raise TickerNotResolvableError(
            f"no CIK for ticker {upper} (ADR or foreign issuer with no direct SEC presence?)"
        )
    return str(found).zfill(10)


def list_filings(
    ticker: str,
    *,
    forms: frozenset[FilingForm],
    user_agent: str = USER_AGENT_DEFAULT,
    limit_per_form: int = 6,
    session: requests.Session | None = None,
    cik: str | None = None,
) -> list[FilingRef]:
    """Enumerate the most recent accessions per form from the submissions API.

    One request per ticker. ``limit_per_form`` bounds history depth (the
    default of 6 covers ~5 fiscal years of annuals, or 6 quarters of 10-Qs);
    the caller is expected to set it from its own scope decision rather than
    this module guessing.
    """
    resolved_cik = cik or resolve_cik(ticker, user_agent=user_agent)
    url = _SUBMISSIONS_URL.format(cik=resolved_cik)
    resp = _get(url, user_agent=user_agent, session=session)
    try:
        payload = cast("dict[str, object]", resp.json())
    except ValueError as exc:
        raise SourceContractError(f"submissions JSON unparseable for {ticker}: {exc}") from exc

    filings = payload.get("filings")
    if not isinstance(filings, dict):
        raise SourceContractError(f"submissions payload for {ticker} has no 'filings' object")
    recent = cast("dict[str, object]", filings).get("recent")
    if not isinstance(recent, dict):
        raise SourceContractError(f"submissions payload for {ticker} has no 'filings.recent'")
    recent_map = cast("dict[str, object]", recent)

    def _col(name: str) -> list[str]:
        value = recent_map.get(name)
        if not isinstance(value, list):
            return []
        return [str(v) if v is not None else "" for v in cast("list[object]", value)]

    forms_col = _col("form")
    accessions = _col("accessionNumber")
    filing_dates = _col("filingDate")
    report_dates = _col("reportDate")
    primary_docs = _col("primaryDocument")
    if not forms_col or not accessions:
        raise SourceContractError(
            f"submissions payload for {ticker} has empty form/accession lists"
        )

    counts: dict[FilingForm, int] = {}
    out: list[FilingRef] = []
    for idx, form_raw in enumerate(forms_col):
        mapped = _FORM_MAP.get(form_raw)
        if mapped is None or mapped not in forms:
            continue
        if counts.get(mapped, 0) >= limit_per_form:
            continue
        accession = accessions[idx] if idx < len(accessions) else ""
        primary = primary_docs[idx] if idx < len(primary_docs) else ""
        if not _ACCN_RX.match(accession) or not primary:
            # A row without a usable accession/primary document can't be
            # fetched; skipping it silently is fine here because the caller
            # records coverage from what it did get, not from this list.
            continue
        counts[mapped] = counts.get(mapped, 0) + 1
        out.append(
            FilingRef(
                ticker=ticker.strip().upper(),
                cik=resolved_cik,
                form=mapped,
                form_raw=form_raw,
                accession=accession,
                filing_date=filing_dates[idx] if idx < len(filing_dates) else "",
                report_date=report_dates[idx] if idx < len(report_dates) else "",
                primary_document=primary,
            )
        )
    return out


def fetch_text(
    ref: FilingRef,
    *,
    cache_dir: Path,
    user_agent: str = USER_AGENT_DEFAULT,
    session: requests.Session | None = None,
    offline: bool = False,
) -> str:
    """Return the filing's plain text, from cache when present.

    ``offline=True`` restricts to the cache and raises ``TransientError`` on a
    miss — the same class a network failure raises, so an offline run records
    the same honest ``FETCH_FAILED`` coverage and stays resumable rather than
    looking like the filing does not exist.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / ref.cache_name
    if cache_path.exists():
        try:
            cached = cache_path.read_text(encoding="utf-8")
            if cached.strip():
                return cached
        except OSError as exc:
            log.debug(
                {"event": "filing_cache_read_failed", "path": str(cache_path), "error": str(exc)}
            )

    if offline:
        raise TransientError(f"offline: no cached text for {ref.ticker} {ref.accession}")

    from filing_text_fetcher import _strip_html

    time.sleep(_DELAY_S)
    resp = _get(ref.url, user_agent=user_agent, session=session)
    text = _strip_html(resp.text)
    if not text.strip():
        raise SourceContractError(
            f"{ref.ticker} {ref.accession}: primary document stripped to empty"
        )
    try:
        cache_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.debug(
            {"event": "filing_cache_write_failed", "path": str(cache_path), "error": str(exc)}
        )
    return text
