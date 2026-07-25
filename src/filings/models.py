"""Typed contracts for the filing-section partition store (migration 0198).

Every value that crosses a module boundary in ``src/filings`` is one of these —
no ``dict[str, Any]`` between the extractor, the store, and the CLI, per the
repo's Layer-3 contract.

The vocabularies here are ``StrEnum``s rather than free-text because they are
consumed by *code* (the store validates against them before writing, the query
layer branches on them). That differs deliberately from
``pipeline.segment_quarterly_coverage``'s free-text ``CoverageStatus``, whose
values are only ever rendered — here a typo in a status would silently make a
period look covered when it failed, which is the exact class of bug this store
exists to prevent.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class SectionSource(StrEnum):
    """Which partition a section row came from.

    The two are independent: a period can be covered by one, both, or neither,
    and consumers must never assume the presence of one implies the other.
    """

    EDGAR_TEXT = "edgar_text"
    FMP_RFILE = "fmp_rfile"


class FilingForm(StrEnum):
    """Filing form the section was partitioned out of.

    Sourced from the DOCUMENT ITSELF wherever the document declares it (FMP
    payloads carry a ``Document Type`` cover row; EDGAR submissions carry the
    form on the accession) — never inferred from a filename or from
    ``tracked_companies.filing_regime``, both of which the audit found
    disagreeing with reality (NU's FMP files are named ``form_10k`` but declare
    ``20-F``).
    """

    FORM_10K = "10-K"
    FORM_10Q = "10-Q"
    FORM_20F = "20-F"
    FORM_40F = "40-F"
    FORM_6K = "6-K"
    FORM_S1 = "S-1"


class FiscalPeriod(StrEnum):
    FY = "FY"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"


class CoverageStatus(StrEnum):
    """The verdict for one (ticker, source, form, period) ingestion attempt.

    Split into three bands, because callers treat them differently:

    * Success — ``OK`` (sections written), ``PARTIAL`` (some sections written
      but a known sub-part failed, e.g. one 6-K exhibit of several was
      image-only).
    * Absence with a known cause — everything from ``SOURCE_MISSING`` through
      ``UNSUPPORTED_FORM``. These are *findings*: the period genuinely has no
      sections from this source and we know why. Safe to consume as "single
      source" after checking.
    * ``NOT_ATTEMPTED`` — we never looked. Distinct from every absence above;
      a consumer must not read it as evidence of anything.
    """

    OK = "ok"
    PARTIAL = "partial"
    SOURCE_MISSING = "source_missing"
    FETCH_FAILED = "fetch_failed"
    AUTH_BLOCKED = "auth_blocked"
    PARSE_FAILED = "parse_failed"
    SCHEMA_DRIFT = "schema_drift"
    NO_SECTIONS_FOUND = "no_sections_found"
    REGIME_MISMATCH = "regime_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    IMAGE_ONLY = "image_only"
    UNSUPPORTED_FORM = "unsupported_form"
    NOT_ATTEMPTED = "not_attempted"


#: Statuses that mean "sections from this source exist for this period".
COVERED_STATUSES: frozenset[CoverageStatus] = frozenset({CoverageStatus.OK, CoverageStatus.PARTIAL})

#: Statuses that mean "we looked and established there is nothing here".
#: ``NOT_ATTEMPTED`` is deliberately absent — never looked is not a finding.
EXPLAINED_ABSENCE_STATUSES: frozenset[CoverageStatus] = frozenset(
    {
        CoverageStatus.SOURCE_MISSING,
        CoverageStatus.FETCH_FAILED,
        CoverageStatus.AUTH_BLOCKED,
        CoverageStatus.PARSE_FAILED,
        CoverageStatus.SCHEMA_DRIFT,
        CoverageStatus.NO_SECTIONS_FOUND,
        CoverageStatus.REGIME_MISMATCH,
        CoverageStatus.PERIOD_MISMATCH,
        CoverageStatus.IMAGE_ONLY,
        CoverageStatus.UNSUPPORTED_FORM,
    }
)


#: Extraction warnings ranked worst-first. A document routinely produces
#: several at once, and only ONE of them fits the queryable ``reason_code``
#: column, so the choice must be the most severe rather than whichever the
#: splitter happened to append first. The ordering is by what a consumer would
#: do about it: a partition that is WRONG (boundaries shifted, so one item's
#: prose is filed under another) outranks one that is merely INCOMPLETE
#: (a sub-item that failed to split), which outranks a labeling nuance.
_WARNING_SEVERITY: tuple[str, ...] = (
    "slice_boundaries_suspect",
    "trivial_section_oversized",
    "slice_starts_mid_sentence",
    "part_boundary_unresolved",
    "no_item_headers_located",
    "missing_risk_factors",
    "missing_mdna",
    "no_cover_section",
    "unparseable_period_end",
    "unparseable_year_field",
)


def most_severe_warning(warnings: list[str]) -> str | None:
    """Pick the warning that best explains a degraded verdict.

    Ranked entries win in ``_WARNING_SEVERITY`` order; anything unranked (the
    parameterized ones like ``subsplit_incomplete_item_3`` or
    ``empty_sections:4``) falls back to first-seen, so a new warning is never
    silently dropped just for being unlisted.
    """
    if not warnings:
        return None
    for candidate in _WARNING_SEVERITY:
        for warning in warnings:
            if warning == candidate or warning.startswith(f"{candidate}:"):
                return warning
    return warnings[0]


class FilingSectionIngestError(Exception):
    """Base for every failure this package raises.

    Exists so a caller can distinguish "this package gave up on an item" from
    an arbitrary ``Exception`` escaping a dependency.
    """


class TickerNotResolvableError(FilingSectionIngestError):
    """This ticker cannot be fetched from EDGAR at all — but others can.

    Deliberately NOT a ``HardStopError``: an unresolvable CIK is a fact about
    one issuer (an ADR like NTDOY with no direct SEC presence), not a broken
    run. Treating it as a hard stop aborted a 38-ticker batch on reaching the
    first such name; it now records ``SOURCE_MISSING`` for that ticker and the
    batch continues.
    """


class HardStopError(FilingSectionIngestError):
    """A failure that must NOT be retried and must NOT degrade silently.

    Setup and contract errors: the table is missing, credentials are rejected,
    an argument is invalid. Per AGENTS.md's error classification, retrying
    these without a fix is wrong, so they propagate to the CLI, which exits
    non-zero.
    """


class TransientError(FilingSectionIngestError):
    """Network / 5xx / rate-limit failure — retrying later is appropriate.

    The orchestrator records ``FETCH_FAILED`` coverage and moves to the next
    item rather than aborting the run.
    """


class SourceContractError(FilingSectionIngestError):
    """The payload arrived but does not match the shape we parse.

    Per the repo's schema-drift rule this is never guess-fixed in-loop: the raw
    payload is dumped to ``.tmp/`` and the period gets ``SCHEMA_DRIFT``
    coverage so the drift is visible rather than absorbed.
    """


_WS_RX = re.compile(r"\s+")
#: FMP appends ``_2``/``_10`` to disambiguate section keys that collide after
#: its ~31-char truncation. The suffix is assigned by document ORDER, so it is
#: not stable across years and must be stripped before any cross-period match.
_DISAMBIGUATOR_RX = re.compile(r"_\d+$")
#: XBRL R-file qualifiers. ``Revenue``, ``Revenue (Tables)`` and
#: ``Revenue - Narrative (Details)`` are the same disclosure rendered three
#: ways; they share a stem so a consumer can ask for the disclosure once.
_QUALIFIER_RX = re.compile(
    r"\s*[-–—]?\s*\(?(tables?|details?(\s+\d+)?|details?\s+narrative|narrative|parenthetical)\)?\s*$",  # noqa: RUF001 — EN/EM dash are real SEC item separators
    re.IGNORECASE,
)
#: FMP truncates section keys to this width; a key at or above it is a prefix,
#: so equality against it is prefix-equality and consumers should say so.
FMP_KEY_TRUNCATION_WIDTH = 31


def normalize_stem(section_key: str) -> str:
    """Reduce a raw section key to its cross-period-comparable stem.

    Strips the ``_N`` disambiguator and any trailing ``(Tables)`` /
    ``(Details)`` / ``Narrative`` qualifier, collapses whitespace, lowercases.
    Applied repeatedly until stable, because real keys stack qualifiers
    (``"Income Taxes - Schedule of Provision (Details)"``).

    Measured effect (docs/design/filing_longitudinal_language.md §1.3): raw-key
    year-over-year overlap runs 75–91%, stem overlap 85–98% on the same files.
    """
    stem = _DISAMBIGUATOR_RX.sub("", section_key.strip())
    for _ in range(4):
        reduced = _QUALIFIER_RX.sub("", stem).strip(" -–—")  # noqa: RUF001 — EN/EM dash are real SEC separators
        if reduced == stem:
            break
        stem = reduced
    return _WS_RX.sub(" ", stem).strip().lower()


def sha256_text(text: str) -> str:
    """Content hash used for unchanged-detection across periods."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FilingSection(BaseModel):
    """One partitioned section, ready to persist.

    ``section_stem`` / ``text_sha256`` / ``char_len`` / ``key_truncated`` are
    derived rather than caller-supplied (see ``build``) so every writer in the
    system computes them identically — a stem computed two ways is a silent
    cross-period join failure.
    """

    ticker: str
    source: SectionSource
    source_ref: str = Field(min_length=1, max_length=255)
    doc_id: int | None = None
    accession_number: str | None = None
    form: FilingForm
    fiscal_year: int | None = None
    fiscal_period: FiscalPeriod
    period_end: datetime | None = None
    filing_date: str | None = None
    section_key_raw: str = Field(min_length=1, max_length=255)
    section_stem: str = Field(min_length=1, max_length=255)
    canonical_id: str | None = None
    title: str | None = None
    ordinal: int = Field(ge=0)
    text: str
    text_sha256: str = Field(min_length=64, max_length=64)
    char_len: int = Field(ge=0)
    key_truncated: bool = False
    extractor_version: str

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, v: str) -> str:
        out = v.strip().upper()
        if not out:
            raise ValueError("ticker must be non-empty")
        return out

    @field_validator("fiscal_year")
    @classmethod
    def _plausible_year(cls, v: int | None) -> int | None:
        # A fiscal year outside this window is a parse error, not a real
        # filing — persisting it would corrupt every cross-period alignment
        # that sorts on it. Same "validate before persisting" rule the repo
        # applies to financial magnitudes.
        if v is not None and not (1990 <= v <= 2100):
            raise ValueError(f"implausible fiscal_year {v!r}")
        return v

    @classmethod
    def build(
        cls,
        *,
        ticker: str,
        source: SectionSource,
        source_ref: str,
        form: FilingForm,
        fiscal_period: FiscalPeriod,
        section_key_raw: str,
        text: str,
        ordinal: int,
        extractor_version: str,
        doc_id: int | None = None,
        accession_number: str | None = None,
        fiscal_year: int | None = None,
        period_end: datetime | None = None,
        filing_date: str | None = None,
        canonical_id: str | None = None,
        title: str | None = None,
    ) -> FilingSection:
        """Construct with every derived field computed here, once."""
        key = section_key_raw.strip()[:255]
        stem = normalize_stem(key)[:255] or key.lower()[:255]
        return cls(
            ticker=ticker,
            source=source,
            source_ref=source_ref[:255],
            doc_id=doc_id,
            accession_number=accession_number,
            form=form,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            period_end=period_end,
            filing_date=filing_date,
            section_key_raw=key,
            section_stem=stem,
            canonical_id=canonical_id,
            title=(title or key)[:2000],
            ordinal=ordinal,
            text=text,
            text_sha256=sha256_text(text),
            char_len=len(text),
            key_truncated=len(key) >= FMP_KEY_TRUNCATION_WIDTH,
            extractor_version=extractor_version,
        )


class CoverageRecord(BaseModel):
    """One (ticker, source, form, period) ingestion verdict."""

    ticker: str
    source: SectionSource
    form: FilingForm
    fiscal_year: int | None
    fiscal_period: FiscalPeriod
    source_ref: str | None = None
    status: CoverageStatus
    reason_code: str | None = None
    detail: str | None = None
    sections_written: int = Field(default=0, ge=0)
    extractor_version: str

    @property
    def is_covered(self) -> bool:
        return self.status in COVERED_STATUSES


class PeriodAvailability(BaseModel):
    """What each source can offer for one (ticker, form, period).

    The read-side answer to "is this period safe to diff, and against what?".
    Consumers branch on ``sources_present`` and MUST check ``is_single_source``
    before treating a diff as complete.
    """

    ticker: str
    form: FilingForm
    fiscal_year: int | None
    fiscal_period: FiscalPeriod
    sources_present: set[SectionSource] = Field(default_factory=set[SectionSource])
    section_counts: dict[str, int] = Field(default_factory=dict[str, int])
    #: source -> (status, reason_code) for sources that produced no sections.
    absent_sources: dict[str, str] = Field(default_factory=dict[str, str])
    #: Cross-source disagreements recorded at ingest (period/regime mismatch).
    mismatches: list[str] = Field(default_factory=list[str])

    @property
    def is_single_source(self) -> bool:
        return len(self.sources_present) == 1

    @property
    def is_empty(self) -> bool:
        return not self.sources_present
