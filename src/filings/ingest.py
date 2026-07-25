"""Orchestrate section ingestion for one ticker across both partitions.

The two lanes are deliberately independent: ``ingest_fmp`` reads only cached
payloads (no network) and ``ingest_edgar`` reads only EDGAR (plus already-
downloaded 6-K exhibits). Neither can fail the other, because the common real
state on this book is exactly one of them being available — NU's FMP annuals
stop at 2022 and its FPI endpoint 403s, while NU's 6-K exhibits are cached and
current. A design where one lane's failure aborted the run would leave those
tickers with nothing.

What "robust" concretely means here, in the order the failures actually occur:

1. **Only one source available** — each lane records its own coverage verdict,
   so the read side (``store.period_availability``) can state which source
   backed a period and why the other did not. A single-source period is
   usable; it is just labeled.
2. **The sources disagree about what the filing is** — the filing's own
   declaration wins (FMP payloads carry ``Document Type``; EDGAR carries the
   form on the accession), never the filename and never
   ``tracked_companies.filing_regime``. The disagreement is recorded as a
   reason code, not resolved silently.
3. **The sources disagree about the period** — a year gap of more than one is
   treated as fatal for that document (sections are NOT written) because a
   mis-yeared section poisons every longitudinal alignment built on top. A gap
   of exactly one is a legitimate fiscal-year labeling difference and is
   recorded, not rejected.
4. **The payload shape changed** — dumped to ``.tmp/`` and recorded as
   ``SCHEMA_DRIFT``; never guess-fixed in-loop, per AGENTS.md.
5. **Anything transient** — recorded as ``FETCH_FAILED`` for that document and
   the run continues; the next run resumes because writes are idempotent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filings import edgar_fetch, edgar_sections, fmp_sections, store
from filings.models import (
    CoverageRecord,
    CoverageStatus,
    FilingForm,
    FilingSection,
    FiscalPeriod,
    HardStopError,
    SectionSource,
    SourceContractError,
    TransientError,
    most_severe_warning,
)

log = logging.getLogger(__name__)

#: Forms whose narrative we partition from EDGAR. 40-F is excluded on purpose:
#: MJDS filers incorporate disclosure by reference to Canadian AIF/MD&A
#: documents instead of numbering items, so there is nothing to partition and
#: the lane records UNSUPPORTED_FORM rather than inventing a partition.
DEFAULT_EDGAR_FORMS: frozenset[FilingForm] = frozenset(
    {FilingForm.FORM_10K, FilingForm.FORM_10Q, FilingForm.FORM_20F}
)

_QUARTER_BY_INDEX = (FiscalPeriod.Q1, FiscalPeriod.Q2, FiscalPeriod.Q3, FiscalPeriod.Q4)
#: Year gap beyond which two sources are describing different filings.
_MAX_YEAR_GAP = 1


@dataclass(slots=True)
class IngestReport:
    """Per-lane outcome, for the CLI's stdout summary and for tests."""

    ticker: str
    source: SectionSource
    documents_seen: int = 0
    documents_ingested: int = 0
    sections_written: int = 0
    coverage_rows: int = 0
    failures: list[str] = field(default_factory=list[str])
    mismatches: list[str] = field(default_factory=list[str])

    def as_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "source": self.source.value,
            "documents_seen": self.documents_seen,
            "documents_ingested": self.documents_ingested,
            "sections_written": self.sections_written,
            "coverage_rows": self.coverage_rows,
            "failures": self.failures,
            "mismatches": self.mismatches,
        }


# ---------------------------------------------------------------------------
# Fiscal-period arithmetic
# ---------------------------------------------------------------------------


def fiscal_year_end_for(conn: sqlite3.Connection, ticker: str) -> tuple[int, int, bool]:
    """Return (month, day, was_known) for the ticker's fiscal year end.

    Falls back to 12-31 when ``tracked_companies.fiscal_year_end`` is unset,
    and reports that fact rather than hiding it: for a January-FYE filer like
    RBRK the fallback shifts every quarter label by one, so callers record
    ``fye_assumed`` in the coverage detail instead of presenting a guessed
    period axis as a known one.
    """
    try:
        row = conn.execute(
            "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ? LIMIT 1",
            (ticker.strip().upper(),),
        ).fetchone()
    except sqlite3.Error:
        return 12, 31, False
    if row is None or not row[0]:
        return 12, 31, False
    raw = str(row[0]).strip()
    parts = raw.split("-")
    if len(parts) != 2:
        return 12, 31, False
    try:
        month, day = int(parts[0]), int(parts[1])
    except ValueError:
        return 12, 31, False
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return 12, 31, False
    return month, day, True


def fiscal_period_for(period_end: datetime, fye_month: int, fye_day: int) -> FiscalPeriod:
    """Which fiscal quarter a period-end date falls in, given the FYE month.

    Months are counted forward from the fiscal year's first month, so a
    December FYE puts a March period-end in Q1 and a January FYE (RBRK) puts an
    April period-end in Q1.
    """
    index = ((period_end.month - fye_month - 1) % 12) // 3
    return _QUARTER_BY_INDEX[index]


def fiscal_year_for(period_end: datetime, fye_month: int, fye_day: int) -> int:
    """The fiscal year a period-end belongs to, labeled by the year it ends in.

    Matches the convention FMP's own ``year`` field uses (and the one the rest
    of the platform's annual files are named after): the FY is the calendar
    year containing the fiscal year END, not the start.
    """
    if (period_end.month, period_end.day) <= (fye_month, fye_day):
        return period_end.year
    return period_end.year + 1


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dump_drift(
    repo_root: Path, ticker: str, source: SectionSource, ref: str, error: str
) -> Path | None:
    """Record a schema-drift incident under ``.tmp/`` for later inspection.

    Per AGENTS.md the agent loop must not guess-fix a changed schema, so this
    captures what failed and where instead of attempting a repair. Returns the
    dump path, or None if even the dump could not be written (which must not
    itself abort the run).
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out_dir = repo_root / ".tmp" / "filing_sections" / "schema_drift"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{ticker}_{source.value}_{stamp}.json"
        path.write_text(
            json.dumps(
                {
                    "ticker": ticker,
                    "source": source.value,
                    "source_ref": ref,
                    "error": error,
                    "captured_at": stamp,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        log.warning({"event": "drift_dump_failed", "ticker": ticker, "error": str(exc)})
        return None


def _lookup_doc_id(
    conn: sqlite3.Connection, ticker: str, *, accession: str | None, file_path: str | None
) -> int | None:
    """Resolve a ``documents.id`` for provenance, by accession then by path.

    Returns None when no row matches — a section without a document pointer is
    still worth storing (the store's own ``source_ref`` identifies it), and
    inventing an id would be worse than a NULL.
    """
    try:
        if accession:
            row = conn.execute(
                "SELECT id FROM documents WHERE ticker = ? AND accession_number = ? LIMIT 1",
                (ticker, accession),
            ).fetchone()
            if row is not None:
                return int(row[0])
        if file_path:
            row = conn.execute(
                "SELECT id FROM documents WHERE ticker = ? AND file_path = ? LIMIT 1",
                (ticker, file_path),
            ).fetchone()
            if row is not None:
                return int(row[0])
    except sqlite3.Error as exc:
        log.debug({"event": "doc_id_lookup_failed", "ticker": ticker, "error": str(exc)})
    return None


def _record(
    conn: sqlite3.Connection,
    report: IngestReport,
    *,
    ticker: str,
    source: SectionSource,
    form: FilingForm,
    fiscal_year: int | None,
    fiscal_period: FiscalPeriod,
    status: CoverageStatus,
    reason_code: str | None,
    detail: str | None,
    sections_written: int,
    source_ref: str | None,
    extractor_version: str,
) -> None:
    store.record_coverage(
        conn,
        CoverageRecord(
            ticker=ticker,
            source=source,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            source_ref=source_ref,
            status=status,
            reason_code=reason_code,
            detail=detail,
            sections_written=sections_written,
            extractor_version=extractor_version,
        ),
    )
    report.coverage_rows += 1
    if reason_code and "mismatch" in reason_code:
        report.mismatches.append(f"{fiscal_year}:{fiscal_period.value}:{reason_code}")


# ---------------------------------------------------------------------------
# FMP lane — cached payloads, no network
# ---------------------------------------------------------------------------


def ingest_fmp(
    conn: sqlite3.Connection,
    ticker: str,
    repo_root: Path,
    *,
    years: set[int] | None = None,
    fmp_dir: Path | None = None,
) -> IngestReport:
    """Ingest every cached ``financial-reports-json`` payload for one ticker.

    Zero network calls — this lane runs entirely off the 673 annual payloads
    already on disk, which is why it is the cheap half to backfill first.
    """
    ticker = ticker.strip().upper()
    report = IngestReport(ticker=ticker, source=SectionSource.FMP_RFILE)
    directory = fmp_dir or (repo_root / "data" / "historical" / "fmp")

    if not directory.is_dir():
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=FilingForm.FORM_10K,
            fiscal_year=None,
            fiscal_period=FiscalPeriod.FY,
            status=CoverageStatus.SOURCE_MISSING,
            reason_code="fmp_cache_dir_absent",
            detail=str(directory),
            sections_written=0,
            source_ref=None,
            extractor_version=fmp_sections.EXTRACTOR_VERSION,
        )
        return report

    paths = sorted(
        p
        for p in directory.glob(f"{ticker}_form_*.json")
        if fmp_sections.parse_filename(p) is not None
    )
    if not paths:
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=FilingForm.FORM_10K,
            fiscal_year=None,
            fiscal_period=FiscalPeriod.FY,
            status=CoverageStatus.SOURCE_MISSING,
            reason_code="no_cached_payloads",
            detail=(
                f"no {ticker}_form_*.json in {directory.name}; for a 20-F filer this is "
                "expected when the FMP financial-reports endpoint is tier-blocked"
            ),
            sections_written=0,
            source_ref=None,
            extractor_version=fmp_sections.EXTRACTOR_VERSION,
        )
        return report

    for path in paths:
        hint = fmp_sections.parse_filename(path)
        if hint is None:  # pragma: no cover — filtered above
            continue
        if years is not None and hint.fiscal_year not in years:
            continue
        report.documents_seen += 1
        _ingest_one_fmp_payload(conn, report, ticker, repo_root, path, hint)

    return report


def _ingest_one_fmp_payload(
    conn: sqlite3.Connection,
    report: IngestReport,
    ticker: str,
    repo_root: Path,
    path: Path,
    hint: fmp_sections.FmpFilenameHint,
) -> None:
    """Parse, reconcile, and persist one payload. Never raises for data reasons."""
    version = fmp_sections.EXTRACTOR_VERSION
    try:
        rel_ref = str(path.relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        rel_ref = path.name

    try:
        parsed = fmp_sections.parse_payload(path)
    except SourceContractError as exc:
        dump = _dump_drift(repo_root, ticker, SectionSource.FMP_RFILE, rel_ref, str(exc))
        report.failures.append(f"{path.name}: schema_drift")
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=hint.form,
            fiscal_year=hint.fiscal_year,
            fiscal_period=hint.fiscal_period,
            status=CoverageStatus.SCHEMA_DRIFT,
            reason_code="payload_unparseable",
            detail=f"{exc}; dump={dump}",
            sections_written=0,
            source_ref=rel_ref,
            extractor_version=version,
        )
        return

    meta = parsed.meta
    notes: list[str] = list(parsed.warnings)

    # --- form reconciliation: the filing's own declaration wins -------------
    form = meta.declared_form or hint.form
    reason_code: str | None = None
    if meta.declared_form is None:
        notes.append("form_undeclared_using_filename")
    elif meta.declared_form is not hint.form:
        reason_code = "regime_mismatch_resolved_to_declared"
        notes.append(
            f"declared={meta.declared_form.value} filename={hint.form.value} — declared wins"
        )

    # --- period reconciliation --------------------------------------------
    fiscal_year = meta.fiscal_year if meta.fiscal_year is not None else hint.fiscal_year
    if meta.fiscal_year is not None and abs(meta.fiscal_year - hint.fiscal_year) > _MAX_YEAR_GAP:
        report.mismatches.append(
            f"{path.name}: payload_year={meta.fiscal_year} filename_year={hint.fiscal_year}"
        )
        report.failures.append(f"{path.name}: period_mismatch")
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=form,
            fiscal_year=hint.fiscal_year,
            fiscal_period=hint.fiscal_period,
            status=CoverageStatus.PERIOD_MISMATCH,
            reason_code="payload_year_vs_filename_year",
            detail=(
                f"payload declares FY{meta.fiscal_year}, filename says FY{hint.fiscal_year}; "
                "sections withheld — a mis-yeared section corrupts every longitudinal alignment"
            ),
            sections_written=0,
            source_ref=rel_ref,
            extractor_version=version,
        )
        return
    if meta.fiscal_year is not None and meta.fiscal_year != hint.fiscal_year:
        reason_code = reason_code or "fiscal_year_label_differs"
        notes.append(f"payload_year={meta.fiscal_year} filename_year={hint.fiscal_year}")

    # `fiscal_year` is always resolved by here (payload value or filename
    # fallback), so only the period-end needs a presence check.
    if meta.period_end is not None and abs(meta.period_end.year - fiscal_year) > _MAX_YEAR_GAP:
        report.failures.append(f"{path.name}: period_end_mismatch")
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period=hint.fiscal_period,
            status=CoverageStatus.PERIOD_MISMATCH,
            reason_code="period_end_vs_fiscal_year",
            detail=(
                f"period_end={meta.period_end.date()} is more than one year from "
                f"FY{fiscal_year}; sections withheld"
            ),
            sections_written=0,
            source_ref=rel_ref,
            extractor_version=version,
        )
        return

    fiscal_period = meta.fiscal_period or hint.fiscal_period
    if meta.symbol and meta.symbol.upper() != ticker:
        # The payload is for a different issuer than its filename claims. This
        # would attribute one company's disclosures to another — always fatal.
        report.failures.append(f"{path.name}: symbol_mismatch")
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            status=CoverageStatus.PERIOD_MISMATCH,
            reason_code="symbol_mismatch",
            detail=f"payload symbol={meta.symbol} but file is named for {ticker}; sections withheld",
            sections_written=0,
            source_ref=rel_ref,
            extractor_version=version,
        )
        return

    if not parsed.slices:
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            status=CoverageStatus.NO_SECTIONS_FOUND,
            reason_code="payload_has_no_sections",
            detail="; ".join(notes) or None,
            sections_written=0,
            source_ref=rel_ref,
            extractor_version=version,
        )
        return

    doc_id = _lookup_doc_id(conn, ticker, accession=None, file_path=rel_ref)
    sections = [
        FilingSection.build(
            ticker=ticker,
            source=SectionSource.FMP_RFILE,
            source_ref=rel_ref,
            form=form,
            fiscal_period=fiscal_period,
            fiscal_year=fiscal_year,
            period_end=meta.period_end,
            section_key_raw=s.key,
            text=s.text,
            ordinal=s.ordinal,
            extractor_version=version,
            doc_id=doc_id,
        )
        for s in parsed.slices
    ]
    written = store.write_sections(conn, sections)
    report.sections_written += written
    report.documents_ingested += 1
    _record(
        conn,
        report,
        ticker=ticker,
        source=SectionSource.FMP_RFILE,
        form=form,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        status=CoverageStatus.PARTIAL if notes else CoverageStatus.OK,
        # A PARTIAL verdict always names WHY in the queryable column, not only
        # in free-text detail: "degraded, reason unknown" is the state this
        # store exists to make impossible.
        reason_code=reason_code or most_severe_warning(notes),
        detail="; ".join(notes) or None,
        sections_written=written,
        source_ref=rel_ref,
        extractor_version=version,
    )


# ---------------------------------------------------------------------------
# EDGAR lane — narrative text
# ---------------------------------------------------------------------------


def ingest_edgar(
    conn: sqlite3.Connection,
    ticker: str,
    repo_root: Path,
    *,
    forms: frozenset[FilingForm] = DEFAULT_EDGAR_FORMS,
    limit_per_form: int = 6,
    user_agent: str = edgar_fetch.USER_AGENT_DEFAULT,
    offline: bool = False,
    session: requests.Session | None = None,
) -> IngestReport:
    """Ingest narrative sections from EDGAR primary documents.

    Enumeration failures are classified: an auth failure raises (a 403 will
    keep failing until the User-Agent is fixed), while a transient failure
    records coverage and returns an empty report so the caller's other tickers
    still run.
    """
    ticker = ticker.strip().upper()
    report = IngestReport(ticker=ticker, source=SectionSource.EDGAR_TEXT)
    cache_dir = repo_root / "data" / "sec_text"
    fye_month, fye_day, fye_known = fiscal_year_end_for(conn, ticker)

    try:
        refs = edgar_fetch.list_filings(
            ticker,
            forms=forms,
            user_agent=user_agent,
            limit_per_form=limit_per_form,
            session=session,
        )
    except HardStopError:
        raise
    except (TransientError, SourceContractError) as exc:
        report.failures.append(f"list_filings: {exc}")
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.EDGAR_TEXT,
            form=next(iter(sorted(forms, key=lambda f: f.value))) if forms else FilingForm.FORM_10K,
            fiscal_year=None,
            fiscal_period=FiscalPeriod.FY,
            status=CoverageStatus.FETCH_FAILED
            if isinstance(exc, TransientError)
            else CoverageStatus.SCHEMA_DRIFT,
            reason_code="submissions_enumeration_failed",
            detail=str(exc),
            sections_written=0,
            source_ref=None,
            extractor_version=edgar_sections.EXTRACTOR_VERSION,
        )
        return report

    for ref in refs:
        report.documents_seen += 1
        _ingest_one_edgar_filing(
            conn,
            report,
            ref,
            repo_root=repo_root,
            cache_dir=cache_dir,
            fye=(fye_month, fye_day, fye_known),
            user_agent=user_agent,
            offline=offline,
            session=session,
        )
    return report


def _period_for_ref(
    ref: edgar_fetch.FilingRef, fye_month: int, fye_day: int
) -> tuple[int | None, FiscalPeriod, datetime | None, str | None]:
    """Resolve (fiscal_year, fiscal_period, period_end, note) for an accession."""
    raw = ref.report_date or ref.filing_date
    note = None if ref.report_date else "period_from_filing_date"
    period_end: datetime | None = None
    if raw:
        try:
            period_end = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            note = "unparseable_report_date"
    if period_end is None:
        return None, FiscalPeriod.FY, None, note or "no_report_date"
    fiscal_year = fiscal_year_for(period_end, fye_month, fye_day)
    if ref.form in (FilingForm.FORM_10Q, FilingForm.FORM_6K):
        return fiscal_year, fiscal_period_for(period_end, fye_month, fye_day), period_end, note
    return fiscal_year, FiscalPeriod.FY, period_end, note


def _ingest_one_edgar_filing(
    conn: sqlite3.Connection,
    report: IngestReport,
    ref: edgar_fetch.FilingRef,
    *,
    repo_root: Path,
    cache_dir: Path,
    fye: tuple[int, int, bool],
    user_agent: str,
    offline: bool,
    session: requests.Session | None,
) -> None:
    """Fetch, split, and persist one accession. Never raises for data reasons."""
    fye_month, fye_day, fye_known = fye
    version = edgar_sections.EXTRACTOR_VERSION
    fiscal_year, fiscal_period, period_end, period_note = _period_for_ref(ref, fye_month, fye_day)
    notes: list[str] = [n for n in (period_note,) if n]
    if not fye_known and ref.form in (FilingForm.FORM_10Q, FilingForm.FORM_6K):
        notes.append("fye_assumed_12_31")

    def record(
        status: CoverageStatus, reason: str | None, detail: str | None, written: int
    ) -> None:
        _record(
            conn,
            report,
            ticker=ref.ticker,
            source=SectionSource.EDGAR_TEXT,
            form=ref.form,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            status=status,
            reason_code=reason,
            detail=detail,
            sections_written=written,
            source_ref=ref.accession,
            extractor_version=version,
        )

    try:
        text = edgar_fetch.fetch_text(
            ref, cache_dir=cache_dir, user_agent=user_agent, session=session, offline=offline
        )
    except HardStopError:
        raise
    except TransientError as exc:
        report.failures.append(f"{ref.accession}: {exc}")
        record(CoverageStatus.FETCH_FAILED, "primary_document_fetch_failed", str(exc), 0)
        return
    except SourceContractError as exc:
        dump = _dump_drift(repo_root, ref.ticker, SectionSource.EDGAR_TEXT, ref.accession, str(exc))
        report.failures.append(f"{ref.accession}: contract")
        record(CoverageStatus.SCHEMA_DRIFT, "primary_document_unusable", f"{exc}; dump={dump}", 0)
        return

    result = edgar_sections.split_document(ref.form, text)
    if not result.slices:
        reason = result.warnings[0] if result.warnings else "no_sections"
        status = (
            CoverageStatus.UNSUPPORTED_FORM
            if reason == "unsupported_form"
            else CoverageStatus.NO_SECTIONS_FOUND
        )
        record(status, reason, "; ".join(result.warnings) or None, 0)
        return

    doc_id = _lookup_doc_id(conn, ref.ticker, accession=ref.accession, file_path=None)
    sections = [
        FilingSection.build(
            ticker=ref.ticker,
            source=SectionSource.EDGAR_TEXT,
            source_ref=ref.accession,
            form=ref.form,
            fiscal_period=fiscal_period,
            fiscal_year=fiscal_year,
            period_end=period_end,
            filing_date=ref.filing_date or None,
            accession_number=ref.accession,
            section_key_raw=s.key,
            text=s.text,
            ordinal=s.ordinal,
            canonical_id=s.concept,
            title=s.title,
            extractor_version=version,
            doc_id=doc_id,
        )
        for s in result.slices
    ]
    written = store.write_sections(conn, sections)
    report.sections_written += written
    report.documents_ingested += 1
    notes.extend(result.warnings)
    record(
        CoverageStatus.PARTIAL if notes else CoverageStatus.OK,
        most_severe_warning(notes),
        "; ".join(notes) or None,
        written,
    )


def ingest_local_exhibits(
    conn: sqlite3.Connection,
    ticker: str,
    repo_root: Path,
    *,
    doc_type: str = "sec_6k",
) -> IngestReport:
    """Partition already-downloaded 6-K exhibits registered in ``documents``.

    ``pipeline.sec_6k_fetch`` has already written real exhibit HTML to
    ``data/historical/sec/`` for the FPI names it supports (NU, NVO), so this
    lane re-reads those files instead of re-fetching them — the exhibits are
    1–2 MB each and the platform is pull-only about SEC bandwidth. Rows whose
    ``file_path`` is one of ``pipeline.sec_xbrl``'s synthetic companyfacts
    pointers (``...companyfacts.json#accn=...``) are skipped with
    ``SOURCE_MISSING``: there is no narrative behind them, which is a fact
    worth recording rather than an error.
    """
    ticker = ticker.strip().upper()
    report = IngestReport(ticker=ticker, source=SectionSource.EDGAR_TEXT)
    version = edgar_sections.EXTRACTOR_VERSION
    fye_month, fye_day, fye_known = fiscal_year_end_for(conn, ticker)

    try:
        rows = conn.execute(
            "SELECT id, file_path, accession_number, filing_date, period_end "
            "FROM documents WHERE ticker = ? AND doc_type = ? ORDER BY filing_date DESC",
            (ticker, doc_type),
        ).fetchall()
    except sqlite3.Error as exc:
        raise HardStopError(f"documents query failed for {ticker}: {exc}") from exc

    if not rows:
        _record(
            conn,
            report,
            ticker=ticker,
            source=SectionSource.EDGAR_TEXT,
            form=FilingForm.FORM_6K,
            fiscal_year=None,
            fiscal_period=FiscalPeriod.FY,
            status=CoverageStatus.SOURCE_MISSING,
            reason_code="no_local_exhibits",
            detail=f"no {doc_type} documents rows for {ticker}",
            sections_written=0,
            source_ref=None,
            extractor_version=version,
        )
        return report

    for row in rows:
        doc_id, file_path, accession, filing_date, period_end_raw = (
            int(row[0]),
            str(row[1] or ""),
            row[2],
            row[3],
            row[4],
        )
        report.documents_seen += 1
        period_end = _coerce_datetime(period_end_raw)
        fiscal_year = fiscal_year_for(period_end, fye_month, fye_day) if period_end else None
        fiscal_period = (
            fiscal_period_for(period_end, fye_month, fye_day) if period_end else FiscalPeriod.FY
        )

        # Loop variables are bound as defaults rather than captured: a closure
        # over the loop's names would silently record every iteration's verdict
        # against the LAST document's period if this helper ever outlived its
        # iteration (a deferred call, a comprehension).
        def record(
            status: CoverageStatus,
            reason: str,
            detail: str | None,
            written: int,
            *,
            _year: int | None = fiscal_year,
            _period: FiscalPeriod = fiscal_period,
            _ref: str = accession or file_path,
        ) -> None:
            _record(
                conn,
                report,
                ticker=ticker,
                source=SectionSource.EDGAR_TEXT,
                form=FilingForm.FORM_6K,
                fiscal_year=_year,
                fiscal_period=_period,
                status=status,
                reason_code=reason,
                detail=detail,
                sections_written=written,
                source_ref=_ref,
                extractor_version=version,
            )

        if "#" in file_path or not file_path:
            record(
                CoverageStatus.SOURCE_MISSING,
                "synthetic_companyfacts_pointer",
                f"file_path={file_path!r} has no narrative behind it",
                0,
            )
            continue

        path = repo_root / file_path
        if not path.is_file():
            record(CoverageStatus.SOURCE_MISSING, "exhibit_file_absent", str(path), 0)
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report.failures.append(f"{file_path}: {exc}")
            record(CoverageStatus.FETCH_FAILED, "exhibit_unreadable", str(exc), 0)
            continue

        result = edgar_sections.split_document(FilingForm.FORM_6K, raw, is_html=True)
        if not result.slices:
            record(
                CoverageStatus.IMAGE_ONLY
                if "empty_text" in result.warnings
                else CoverageStatus.NO_SECTIONS_FOUND,
                result.warnings[0] if result.warnings else "no_sections",
                "; ".join(result.warnings) or None,
                0,
            )
            continue

        sections = [
            FilingSection.build(
                ticker=ticker,
                source=SectionSource.EDGAR_TEXT,
                source_ref=accession or file_path,
                form=FilingForm.FORM_6K,
                fiscal_period=fiscal_period,
                fiscal_year=fiscal_year,
                period_end=period_end,
                filing_date=str(filing_date) if filing_date else None,
                accession_number=str(accession) if accession else None,
                section_key_raw=s.key,
                text=s.text,
                ordinal=s.ordinal,
                canonical_id=s.concept,
                title=s.title,
                extractor_version=version,
                doc_id=doc_id,
            )
            for s in result.slices
        ]
        written = store.write_sections(conn, sections)
        report.sections_written += written
        report.documents_ingested += 1
        notes = list(result.warnings)
        if not fye_known:
            notes.append("fye_assumed_12_31")
        record(
            CoverageStatus.PARTIAL if notes else CoverageStatus.OK,
            most_severe_warning(notes) or "ok",
            "; ".join(notes) or None,
            written,
        )

    return report


def _coerce_datetime(value: object) -> datetime | None:
    """Best-effort DATETIME coercion for values read back from SQLite."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str) and value.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value.strip()[:26], fmt)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Cross-source reconciliation
# ---------------------------------------------------------------------------


def reconcile_sources(conn: sqlite3.Connection, ticker: str) -> list[str]:
    """Flag periods where the two partitions disagree about the same filing.

    Both lanes bucket sections by (form, fiscal_year, fiscal_period), so a
    disagreement shows up as two different ``period_end`` values inside one
    bucket. That means one of the two mis-derived the period axis — most often
    an unset ``tracked_companies.fiscal_year_end`` shifting the quarter labels
    — and any diff over that bucket would be comparing different quarters.

    The mismatch is appended to BOTH sources' coverage reason codes rather
    than being resolved: with no third source there is no principled winner,
    and silently picking one is exactly the "don't silently merge" failure the
    repo's multi-source rule prohibits.
    """
    ticker = ticker.strip().upper()
    if not store.table_exists(conn, store.SECTIONS_TABLE):
        raise HardStopError(f"{store.SECTIONS_TABLE} missing — run alembic upgrade head")

    rows = conn.execute(
        f"""
        SELECT form, fiscal_year, fiscal_period, source,
               MIN(period_end) AS min_end, MAX(period_end) AS max_end
        FROM {store.SECTIONS_TABLE}
        WHERE ticker = ? AND period_end IS NOT NULL
        GROUP BY form, fiscal_year, fiscal_period, source
        """,
        (ticker,),
    ).fetchall()

    by_bucket: dict[tuple[str, object, str], dict[str, str]] = {}
    for form, fiscal_year, fiscal_period, source, min_end, _max_end in rows:
        by_bucket.setdefault((str(form), fiscal_year, str(fiscal_period)), {})[str(source)] = str(
            min_end
        )

    findings: list[str] = []
    for (form, fiscal_year, fiscal_period), by_source in by_bucket.items():
        if len(by_source) < 2:
            continue
        ends = {value[:10] for value in by_source.values()}
        if len(ends) <= 1:
            continue
        finding = (
            f"{form} FY{fiscal_year} {fiscal_period}: period_end disagreement "
            f"{sorted(ends)} across {sorted(by_source)}"
        )
        findings.append(finding)
        try:
            conn.execute(
                f"""
                UPDATE {store.COVERAGE_TABLE}
                SET reason_code = COALESCE(reason_code || '+', '') || 'cross_source_period_mismatch',
                    detail = COALESCE(detail || ' | ', '') || ?
                WHERE ticker = ? AND form = ? AND fiscal_year IS ? AND fiscal_period = ?
                  AND COALESCE(reason_code, '') NOT LIKE '%cross_source_period_mismatch%'
                """,
                (finding, ticker, form, fiscal_year, fiscal_period),
            )
        except sqlite3.Error as exc:
            log.warning({"event": "reconcile_update_failed", "ticker": ticker, "error": str(exc)})

    return findings
