"""P1 discontinued-metric detector — deterministic stages only.

Fully specified in ``docs/design/disclosure_change_signals.md`` §2-3 and
``docs/design/disclosure_change_build_stack.md`` (P1). Everything in this
module is zero-LLM, zero-token: candidate generation + suppression over the
cached SEC ``companyfacts`` payloads (``data/historical/sec/{TICKER}_companyfacts.json``).
The one LLM step (relevance/prior triage) lives in ``filings.metric_triage``
and is deliberately a separate module so this one stays free of any call
boundary a test would need to mock.

Naive detection ("tag reported >=6 periods, absent from the last 4") is
~90% noise — measured at 175 candidates for META alone, including
``PropertyPlantAndEquipmentNet``, which META obviously still reports (under
a renamed tag — see below). Four independent, deterministically-killable
noise sources:

1. **Axis conflation.** A tag's disclosure cadence is bound to its FORM
   FAMILY: annual forms (10-K/20-F/40-F, i.e. once a year) vs 10-Q (once a
   quarter). Comparing an annual-only tag's silence against a quarterly
   cadence, or vice versa, manufactures false positives. Fix: compute
   absence entirely WITHIN one axis at a time (``Axis.ANNUAL`` /
   ``Axis.QUARTERLY``); a tag is only promoted to a candidate when it is
   silent beyond tolerance on EVERY axis it has ever reported on (see the
   "still alive elsewhere" gate in ``run_lifecycle_detection``).
2. **Irregular cadence.** Many tags legitimately skip periods (a footnote
   schedule item that only applies some years). A fixed "absent >= N
   periods" rule flags these constantly. Fix: calibrate each tag against
   ITS OWN historical maximum gap (the largest span it has ever gone silent
   for between two of its own appearances) and flag only when the CURRENT
   silence exceeds that tag's own precedent — the Zhou & Zhou "expected
   disclosure baseline" made concrete, built from the company's own filing
   history rather than a universal rule or wall-clock "today".
3. **Taxonomy relabels.** The disclosure continues, verbatim in substance,
   under a new tag name — a pure XBRL rename with zero information content.
   Confirmed on real data twice in this session: META's
   ``PropertyPlantAndEquipmentNet`` (last 2020 Q3) was renamed to
   ``PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization``
   (starts 2020 FY) when ASU 842 folded finance leases into the PP&E
   concept; MELI's ``PaymentsToAcquirePropertyPlantAndEquipment`` (last
   2024 Q3) was renamed to ``PaymentsToAcquireProductiveAssets`` (starts
   2024 FY / 2025 Q1). Fix: for every candidate, search the same taxonomy
   for a tag whose FIRST-EVER appearance lands within
   ``RELABEL_PERIOD_WINDOW`` periods of the stopped tag's last appearance,
   with comparable magnitude at the nearest overlapping period
   (``find_relabel_target``). A match reclassifies the candidate as
   ``metric_relabeled`` rather than ``metric_discontinued`` — it must never
   reach the LLM triage step, let alone a surface.
4. **Accounting-standard transitions.** A tag stops not because ONE company
   decided anything, but because an ASU/ASC forced the WHOLE taxonomy off
   it, industry-wide, in the same 1-3 year window — and, unlike noise
   source 3, sometimes with no distinct successor tag at all (the
   information simply gets folded into a schedule the new standard already
   requires elsewhere, or picked up by a tag that ALREADY existed rather
   than one freshly debuting — see the "already-active broader tag" known
   limitation below, which this gate independently resolves for at least
   one real case). Confirmed industry-wide across the full 99-ticker cached
   book: ``OperatingLeasesFutureMinimumPaymentsDueThereafter``/
   ``...DueCurrent``/``...Due`` (ASU 842, FY2018-2020) independently stopped
   for 40-47 tickers within this same ±2-period window;
   ``ExcessTaxBenefitFromShareBasedCompensationFinancingActivities``
   (ASU 2016-09) for 32; ``DeferredRevenueCurrent``/``SalesRevenueServicesNet``
   (ASC 606) for 24/13. This is the same principle as P2 "cross-sectional
   detrending" in ``docs/design/disclosure_change_build_stack.md``, applied
   at TAG level instead of at a similarity-score level: benchmark each
   candidate against the same-window cross-section of the WHOLE cached book
   rather than judging it in isolation. Fix: ``build_standard_transition_corpus``
   scans every OTHER cached ticker's Stage 0 output once per run and counts,
   for a given candidate, how many other tickers stopped the SAME tag within
   ``RELABEL_PERIOD_WINDOW`` periods; at or above
   ``STANDARD_TRANSITION_MIN_OTHER_TICKERS`` the candidate becomes
   ``metric_standard_transition`` rather than ``metric_discontinued`` — a
   *different verdict from ``metric_relabeled``* (no successor tag is
   claimed; the disclosure may simply be gone) but the same "mechanical, not
   a company decision" classification. Deliberately NOT a hardcoded list of
   known ASU/ASC standards — a hardcoded list rots the day a NEW standard
   ships; the cross-sectional count is self-calibrating and requires no
   maintenance to catch a transition this repo has never seen. Corpus
   BREADTH matters: on an 11-ticker (portfolio-only) corpus META's #1
   candidate by materiality was still ``AdvertisingRevenue`` (0.95 — the
   size of revenue itself), fixed instead by the ``_DEPRECATED_ANNOTATION_RX``
   strip below (a relabel-gate fix, not this one — it shares its tag with
   only 1 other cached ticker, below this gate's threshold); running the
   corpus over the full 99-ticker book is what surfaces the 24-47-ticker
   clusters above with headroom to spare.

Materiality ranks (never filters) surviving candidates: the last reported
value relative to a scale anchor (revenue, falling back to total assets)
taken from the SAME filing (matched by accession number) or, failing that,
the nearest date in the same payload.

Do NOT encode "stopped reporting = bearish" anywhere downstream of this
module. ``disclosure_events.verdict`` (migration 0203) exists precisely so a
detector commits to concealment vs. maturity vs. mechanical rather than
letting the event type imply a direction it has not earned; this module
only emits the deterministic ``mechanical`` verdict for confirmed relabels
and standard transitions, and leaves every ``metric_discontinued``
candidate ``unclassified`` until ``filings.metric_triage`` (or a human)
judges it.

**Known limitation — an already-active broader tag absorbing a narrower
one is invisible to the relabel gate, by design.** ``find_relabel_target``
only matches a replacement whose OWN first-ever appearance is close in time
to the stop (noise source 3 is specifically about a NEW tag debuting).
When a company instead folds a narrower line into a tag it has ALREADY been
reporting continuously for years, there is no "new tag debuting" to find —
confirmed on META: ``Cash`` (last 2015 Q3) has 100% label-token containment
with the still-active ``CashAndCashEquivalentsAtCarryingValue`` (period gap
-13, i.e. that tag already existed 13 unified periods before ``Cash``
stopped) and ``NetIncomeLossAvailableToCommonStockholdersBasic`` (last
FY2020) similarly contains 3/7 of its tokens in the already-active
``NetIncomeLoss`` (gap -34). Both correctly fall through as
``metric_discontinued`` rather than a false ``metric_relabeled`` suppression
— which is arguably the RIGHT outcome for the second case in particular
(the "available to common stockholders" adjustment disappearing can reflect
a real capital-structure change, e.g. preferred stock redemption, that is
worth an LLM/human look, not noise to suppress). A related case, META's
``IntangibleAssetsNetIncludingGoodwill`` (last 2014 Q2, containment 0.6
against the still-active ``IntangibleAssetsNetExcludingGoodwill``, debuting
one period later): the antonym guard correctly blocks this pairing despite
the tight timing, because "including" vs "excluding" goodwill are genuinely
different carrying values, not a rename — META split a combined figure into
components, a real disclosure-granularity decision. Widening the period
window to also match "already active" targets, or loosening the antonym
guard, was deliberately NOT done — it would trade these honest gaps for
false-suppressing real, judgment-worthy disclosure changes. (VEEV's
``PaymentsToAcquirePropertyPlantAndEquipment``, which looked like this same
"no successor found" case on an 11-ticker corpus, resolved once
``build_standard_transition_corpus`` ran over the full 99-ticker cached
book: NVDA and WIX independently stopped the identical tag within the same
quarter, so it correctly reclassifies as ``metric_standard_transition``
instead — direct evidence that corpus breadth matters for this gate.)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, field_validator

from filings.models import HardStopError
from filings.store import table_exists

log = logging.getLogger(__name__)

DETECTOR_VERSION = "metric_lifecycle_v1"
EVENTS_TABLE = "disclosure_events"

#: Cover-page (dei), exec-comp (ecd) and financial-footnote-disclosure (ffd)
#: taxonomies are SEC/XBRL plumbing, never a business metric — skip outright.
_SKIP_TAXONOMIES = frozenset({"dei", "ecd", "ffd"})

_ANNUAL_FORM_PREFIXES = ("10-K", "20-F", "40-F")
_QUARTERLY_FORM_PREFIXES = ("10-Q",)

_VALID_FP = frozenset({"Q1", "Q2", "Q3", "FY"})
_FP_QUARTER: dict[str, int] = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}

#: A tag needs at least this many distinct periods on an axis before its own
#: cadence is judgeable at all ("too little history" per Stage 1).
MIN_OBSERVATIONS = 6
#: Never flag a tag merely one period "late" — silence must be at least 2
#: periods before it can possibly exceed a historical gap of 1.
MIN_CURRENT_SILENCE = 2
#: How close (in unified cross-axis periods) a replacement tag's first-ever
#: appearance must land to the stopped tag's last appearance.
RELABEL_PERIOD_WINDOW = 2
#: Generous order-of-magnitude tolerance: cumulative-duration bases
#: (9-month YTD vs. 12-month FY) differ even for the exact same underlying
#: disclosure, so this is deliberately loose — it exists to reject
#: unrelated tags, not to fine-grain-match magnitude. MANDATORY (a pairing
#: with no computable magnitude is REJECTED, not passed through) — a large
#: filer introduces dozens of unrelated new tags in any given ±2-period
#: window (e.g. a whole batch from a single accounting-standard adoption),
#: so period proximity alone produces rampant false matches (measured:
#: 69/72 META candidates paired before this + the label gate below were
#: added — nonsense pairs like AccountsPayableCurrent ->
#: EffectiveIncomeTaxRateReconciliationNondeductibleExpenseResearchAndDevelopment).
RELABEL_MAGNITUDE_RATIO = 5.0
#: Minimum fraction of the STOPPED tag's significant label tokens that must
#: reappear in the candidate replacement's label. The strongest of the three
#: signals in practice: real XBRL relabels are English-description renames
#: ("Payments to Acquire Property, Plant and Equipment" -> "Payments to
#: Acquire Productive Assets" keeps "payments"/"acquire"; "Property, Plant
#: and Equipment, Net" -> "...and Finance Lease Right-of-Use Asset, After
#: Accumulated Depreciation..." keeps "property"/"plant"/"equipment").
#: Unrelated tags essentially never share this much core vocabulary AND
#: land within the period window AND the magnitude band simultaneously.
RELABEL_LABEL_CONTAINMENT_MIN = 0.4
#: Noise source 4 (accounting-standard transitions): a candidate is
#: reclassified ``metric_standard_transition`` when at least this many OTHER
#: cached tickers stopped the identical tag within ``RELABEL_PERIOD_WINDOW``
#: periods of this ticker's stop. Measured on the full 99-ticker cached
#: book: at this threshold 219/2258 distinct stopped tags (~9.7%) qualify —
#: a minority, so real company-specific events are not swept up — while the
#: confirmed ASU-842/ASU-2016-09/ASC-606 examples in the module docstring
#: clear it by 10-20x margin (13-47 tickers each), giving ample headroom.
STANDARD_TRANSITION_MIN_OTHER_TICKERS = 2

_LABEL_STOPWORDS = frozenset(
    {"and", "of", "the", "to", "for", "on", "in", "at", "by", "or", "with", "from"}
)
_CAMEL_BOUNDARY_RX = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_RX = re.compile(r"[A-Za-z]+")
#: The US-GAAP taxonomy annotates a retired element's OWN label with this
#: suffix, e.g. ``"Advertising Revenue (Deprecated 2018-01-31)"`` — confirmed
#: on 2,991 label instances across the full cached book, always a trailing
#: suffix. This is administrative taxonomy metadata, not content describing
#: what the tag measures, and must be stripped before tokenizing: left in,
#: it inflates the OLD tag's token count and can drop containment below
#: threshold for a tag that is otherwise an exact-vocabulary match — real
#: case found in review: "Advertising Revenue (Deprecated 2018-01-31)" vs.
#: "Revenue from Contract with Customer, Excluding Assessed Tax" scored
#: containment 1/3 (denominator inflated by "deprecated") instead of the
#: correct 1/2, missing META's #1-by-materiality candidate.
_DEPRECATED_ANNOTATION_RX = re.compile(r"\(deprecated[^)]*\)", re.IGNORECASE)


def _singularize(token: str) -> str:
    """Crude, deterministic de-pluralization — strip a lone trailing 's'
    (never 'ss'). Exists for exactly one real, high-materiality case: the
    ASC 606 (2018) taxonomy-wide switch from ``Revenues`` to
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` — the single
    highest-materiality candidate on the real book (materiality 1.0, i.e.
    the size of revenue itself) — is invisible to a bare string-equality
    token match ("revenues" vs "revenue") without this. Does not handle
    ``-ies``/``-es`` pluralization (e.g. "liabilities"); that is an accepted
    imperfection, not a correctness requirement for the tags this detector
    has been validated against."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _label_tokens(label: str) -> set[str]:
    """Significant (non-stopword, len>2) lowercase word tokens in a label,
    crudely de-pluralized (see ``_singularize``).

    Splits camelCase boundaries first so a bare tag name (no human label
    available) still tokenizes sensibly — real companyfacts ``label`` values
    are already space-separated prose, so this is a no-op for them. Strips
    a trailing ``(Deprecated ...)`` taxonomy annotation first (see
    ``_DEPRECATED_ANNOTATION_RX``) — it is metadata about the tag's
    lifecycle, not a description of what it measures."""
    without_deprecation = _DEPRECATED_ANNOTATION_RX.sub("", label)
    spaced = _CAMEL_BOUNDARY_RX.sub(" ", without_deprecation)
    return {
        _singularize(t.lower())
        for t in _WORD_RX.findall(spaced)
        if len(t) > 2 and t.lower() not in _LABEL_STOPWORDS
    }


#: Modifier pairs that flip a label's MEANING rather than merely rewording
#: it. Two labels sharing a lot of generic vocabulary ("intangible",
#: "assets", "goodwill") can still describe near-OPPOSITE measures — real
#: false positive found on META: ``IntangibleAssetsNetIncludingGoodwill``
#: ("Intangible Assets, Net (Including Goodwill)") token-matched
#: ``IntangibleAssetsGrossExcludingGoodwill`` ("...Gross (Excluding
#: Goodwill)") at containment 0.6 — net-vs-gross AND including-vs-excluding
#: simultaneously, neither of which is a rename, both a different carrying
#: value. Reject whenever a label pair contains opposite members of any pair
#: below.
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("gross", "net"),
    ("including", "excluding"),
    ("before", "after"),
)


def _has_antonym_conflict(old_tokens: set[str], new_tokens: set[str]) -> bool:
    return any(
        (a in old_tokens and b in new_tokens) or (b in old_tokens and a in new_tokens)
        for a, b in _ANTONYM_PAIRS
    )


class Axis(StrEnum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class CandidateKind(StrEnum):
    DISCONTINUED = "metric_discontinued"
    RELABELED = "metric_relabeled"
    STANDARD_TRANSITION = "metric_standard_transition"


def _axis_for_form(form: str) -> Axis | None:
    base = form.removesuffix("/A")
    if base in _ANNUAL_FORM_PREFIXES:
        return Axis.ANNUAL
    if base in _QUARTERLY_FORM_PREFIXES:
        return Axis.QUARTERLY
    return None


class XbrlObservation(BaseModel):
    """One (period, form, value) row for a single XBRL tag+unit.

    ``fiscal_year``/``fiscal_period`` are the XBRL ``fy``/``fp`` CONTEXT
    fields — which report this fact was disclosed IN — not derived from
    ``end``. That distinction is what makes relabel-timing detection work: a
    10-K's comparative columns (prior two fiscal years' figures, reprinted
    for comparison) all carry the CURRENT report's fy/fp, so a brand-new
    tag's earliest fy/fp correctly identifies the filing it DEBUTED in, even
    though its earliest ``end`` date is an older comparative period.
    """

    fiscal_year: int
    fiscal_period: str
    form: str
    end: str
    start: str | None = None
    val: float
    accn: str
    filed: str

    @field_validator("fiscal_period")
    @classmethod
    def _known_fp(cls, v: str) -> str:
        if v not in _VALID_FP:
            raise ValueError(f"unrecognized fiscal_period {v!r}")
        return v

    @property
    def axis(self) -> Axis | None:
        return _axis_for_form(self.form)

    @property
    def period_rank(self) -> int:
        """Unified cross-axis rank — used ONLY for relabel-timing proximity
        (per-tag absence detection stays strictly axis-split; see module
        docstring noise source 1)."""
        return self.fiscal_year * 4 + _FP_QUARTER[self.fiscal_period]

    @property
    def duration_days(self) -> int | None:
        if not self.start:
            return None
        try:
            d0 = date.fromisoformat(self.start)
            d1 = date.fromisoformat(self.end)
        except ValueError:
            return None
        days = (d1 - d0).days
        return days if days > 0 else None


def _axis_native_rank(obs: XbrlObservation) -> int:
    """Rank in units native to the observation's own axis: +1 per year for
    the annual axis, +1 per quarter for the quarterly axis. Deliberately
    NOT the same as ``period_rank`` (which is +1 per quarter for BOTH axes,
    for relabel matching only) — mixing the two would silently reintroduce
    axis conflation into the gap-calibration math."""
    axis = obs.axis
    if axis is Axis.ANNUAL:
        return obs.fiscal_year
    if axis is Axis.QUARTERLY:
        q = _FP_QUARTER.get(obs.fiscal_period)
        if q is None or q > 3:
            # A quarterly-form entry tagged FY is malformed/rare; anchor it
            # past Q3 so it never collides with a real Q1-Q3 rank.
            return obs.fiscal_year * 4 + 4
        return obs.fiscal_year * 4 + q
    raise ValueError("_axis_native_rank requires a classified (10-K/20-F/40-F/10-Q) axis")


def _period_label(axis: Axis, fiscal_year: int, fiscal_period: str) -> str:
    return f"FY{fiscal_year}" if axis is Axis.ANNUAL else f"{fiscal_year}{fiscal_period}"


class TagSeries(BaseModel):
    """Every parsed observation for one (taxonomy, tag, unit)."""

    taxonomy: str
    tag: str
    label: str
    unit: str
    observations: list[XbrlObservation] = Field(default_factory=list["XbrlObservation"])

    @property
    def qualified_name(self) -> str:
        return f"{self.taxonomy}:{self.tag}"


def _parse_tag_unit(
    *, taxonomy: str, tag: str, label: str, unit: str, raw_entries: list[object]
) -> TagSeries:
    """Defensive parse: a malformed entry is dropped and counted, never
    crashes the whole ticker (schema-drift rule — see module callers for
    where the drop count is logged)."""
    obs: list[XbrlObservation] = []
    dropped = 0
    for raw in raw_entries:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        row = cast("dict[str, object]", raw)
        fy, fp, form, end, val, accn, filed = (
            row.get("fy"),
            row.get("fp"),
            row.get("form"),
            row.get("end"),
            row.get("val"),
            row.get("accn"),
            row.get("filed"),
        )
        if None in (fy, fp, form, end, val, accn, filed):
            dropped += 1
            continue
        try:
            start_raw = row.get("start")
            obs.append(
                XbrlObservation(
                    fiscal_year=int(cast("int", fy)),
                    fiscal_period=str(fp),
                    form=str(form),
                    end=str(end),
                    start=str(start_raw) if start_raw else None,
                    val=float(cast("float", val)),
                    accn=str(accn),
                    filed=str(filed),
                )
            )
        except (ValueError, TypeError):
            dropped += 1
            continue
    if dropped:
        log.debug({"event": "metric_lifecycle_entries_dropped", "tag": tag, "count": dropped})
    return TagSeries(taxonomy=taxonomy, tag=tag, label=label, unit=unit, observations=obs)


def load_tag_series(repo_root: Path, ticker: str) -> list[TagSeries] | None:
    """Parse a ticker's cached companyfacts into per-(taxonomy, tag, unit) series.

    Returns ``None`` when no cache file exists — a normal per-ticker case
    (some names have no SEC XBRL presence at all, e.g. an unsupported 40-F
    filer), not an error. Skips ``dei``/``ecd``/``ffd`` taxonomies.
    """
    path = repo_root / "data" / "historical" / "sec" / f"{ticker.upper()}_companyfacts.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            {
                "event": "metric_lifecycle_companyfacts_unreadable",
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return None
    if not isinstance(payload, dict):
        return None
    payload_obj = cast("dict[str, object]", payload)
    facts = payload_obj.get("facts")
    if not isinstance(facts, dict):
        return None
    facts_obj = cast("dict[str, object]", facts)

    series: list[TagSeries] = []
    for taxonomy, tags in facts_obj.items():
        if taxonomy in _SKIP_TAXONOMIES or not isinstance(tags, dict):
            continue
        tags_obj = cast("dict[str, object]", tags)
        for tag, obj in tags_obj.items():
            if not isinstance(obj, dict):
                continue
            obj_dict = cast("dict[str, object]", obj)
            label = str(obj_dict.get("label") or tag)
            units = obj_dict.get("units")
            if not isinstance(units, dict):
                continue
            units_obj = cast("dict[str, object]", units)
            for unit, entries in units_obj.items():
                if not isinstance(entries, list):
                    continue
                series.append(
                    _parse_tag_unit(
                        taxonomy=taxonomy,
                        tag=tag,
                        label=label,
                        unit=str(unit),
                        raw_entries=cast("list[object]", entries),
                    )
                )
    return series


def group_by_concept(series: list[TagSeries]) -> dict[tuple[str, str], TagSeries]:
    """One canonical ``TagSeries`` per (taxonomy, tag).

    A tag reporting under more than one unit is rare and not a case this
    detector needs to split further — keep whichever unit has the most
    observations."""
    buckets: dict[tuple[str, str], list[TagSeries]] = {}
    for s in series:
        buckets.setdefault((s.taxonomy, s.tag), []).append(s)
    return {key: max(group, key=lambda s: len(s.observations)) for key, group in buckets.items()}


class AxisSummary(BaseModel):
    """The most recent period the TICKER ITSELF is known to have reported
    ANYTHING on this axis — the Zhou & Zhou 'expected disclosure baseline',
    built from the company's own filing history, never wall-clock 'today'.
    """

    axis: Axis
    last_period_rank: int
    last_fiscal_year: int
    last_fiscal_period: str


def compute_axis_summaries(series: list[TagSeries]) -> dict[Axis, AxisSummary]:
    best: dict[Axis, XbrlObservation] = {}
    for s in series:
        for obs in s.observations:
            axis = obs.axis
            if axis is None:
                continue
            rank = _axis_native_rank(obs)
            cur = best.get(axis)
            if cur is None or rank > _axis_native_rank(cur):
                best[axis] = obs
    return {
        axis: AxisSummary(
            axis=axis,
            last_period_rank=_axis_native_rank(obs),
            last_fiscal_year=obs.fiscal_year,
            last_fiscal_period=obs.fiscal_period,
        )
        for axis, obs in best.items()
    }


class TagLifecycleStats(BaseModel):
    """Axis-scoped absence stats for one tag, calibrated against ITS OWN
    historical cadence."""

    axis: Axis
    n_periods: int
    first_rank: int
    last_rank: int
    last_observation: XbrlObservation
    historical_max_gap: int
    current_silence: int
    axis_last_rank: int

    @property
    def last_period_label(self) -> str:
        return _period_label(
            self.axis, self.last_observation.fiscal_year, self.last_observation.fiscal_period
        )


def compute_tag_lifecycle(
    series: TagSeries, axis: Axis, axis_summary: AxisSummary
) -> TagLifecycleStats | None:
    """``None`` when the tag has fewer than ``MIN_OBSERVATIONS`` distinct
    periods on this axis — too little history for its own cadence to be
    judgeable (Stage 1's "drop tags with too little history")."""
    axis_obs = [o for o in series.observations if o.axis is axis]
    if not axis_obs:
        return None
    # Dedupe to one observation per (fy, fp): keep the one with the latest
    # `filed` date, so a later filing's restated comparative column doesn't
    # get counted as a second, spurious appearance of the same period.
    by_period: dict[tuple[int, str], XbrlObservation] = {}
    for o in axis_obs:
        key = (o.fiscal_year, o.fiscal_period)
        cur = by_period.get(key)
        if cur is None or o.filed > cur.filed:
            by_period[key] = o
    if len(by_period) < MIN_OBSERVATIONS:
        return None

    ranked = sorted(by_period.values(), key=_axis_native_rank)
    ranks = [_axis_native_rank(o) for o in ranked]
    historical_max_gap = max((b - a for a, b in pairwise(ranks)), default=1)
    historical_max_gap = max(historical_max_gap, 1)
    last_obs = ranked[-1]
    last_rank = ranks[-1]
    return TagLifecycleStats(
        axis=axis,
        n_periods=len(by_period),
        first_rank=ranks[0],
        last_rank=last_rank,
        last_observation=last_obs,
        historical_max_gap=historical_max_gap,
        current_silence=axis_summary.last_period_rank - last_rank,
        axis_last_rank=axis_summary.last_period_rank,
    )


class MetricCandidate(BaseModel):
    """A tag flagged absent (Stage 0), possibly reclassified as a relabel
    pair (Stage 1) — before any LLM triage (Stage 2, ``filings.metric_triage``).
    """

    ticker: str
    taxonomy: str
    tag: str
    label: str
    axis: Axis
    last_observation: XbrlObservation
    current_silence: int
    historical_max_gap: int
    materiality: float | None = None
    kind: CandidateKind = CandidateKind.DISCONTINUED
    relabeled_to: str | None = None
    relabel_period_gap: int | None = None
    relabel_magnitude_ratio: float | None = None
    #: Set only when ``kind is CandidateKind.STANDARD_TRANSITION`` — how many
    #: OTHER cached tickers stopped this identical tag within the same
    #: period window (see ``STANDARD_TRANSITION_MIN_OTHER_TICKERS``).
    standard_transition_other_tickers: int | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.taxonomy}:{self.tag}"

    @property
    def last_value(self) -> float:
        return self.last_observation.val

    @property
    def last_period_label(self) -> str:
        return _period_label(
            self.axis, self.last_observation.fiscal_year, self.last_observation.fiscal_period
        )


_REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
_ASSET_TAGS = ("Assets",)


def _nearest_value(
    observations: list[XbrlObservation], *, accn: str, near_end: str
) -> float | None:
    """Prefer the SAME filing (matched by accession number); fall back to
    the nearest ``end`` date in the payload."""
    for o in observations:
        if o.accn == accn:
            return o.val
    try:
        target = date.fromisoformat(near_end)
    except ValueError:
        return None
    best: tuple[int, float] | None = None
    for o in observations:
        try:
            delta = abs((date.fromisoformat(o.end) - target).days)
        except ValueError:
            continue
        if best is None or delta < best[0]:
            best = (delta, o.val)
    return best[1] if best is not None else None


def _scale_anchor(
    concepts: dict[tuple[str, str], TagSeries], taxonomy: str, *, accn: str, near_end: str
) -> float | None:
    """Revenue, falling back to total assets, from the taxonomy the
    candidate itself reports under."""
    for tag in _REVENUE_TAGS:
        s = concepts.get((taxonomy, tag))
        if s and s.observations:
            v = _nearest_value(s.observations, accn=accn, near_end=near_end)
            if v:
                return abs(v)
    for tag in _ASSET_TAGS:
        s = concepts.get((taxonomy, tag))
        if s and s.observations:
            v = _nearest_value(s.observations, accn=accn, near_end=near_end)
            if v:
                return abs(v)
    return None


def _rate(obs: XbrlObservation) -> float | None:
    """Value per day for duration concepts (so a 9-month YTD figure is
    comparable to a 12-month FY figure); the raw value for instant concepts."""
    days = obs.duration_days
    if days:
        return obs.val / days
    return obs.val


def _magnitude_ratio(a: float | None, b: float | None) -> float | None:
    """``None`` means "not comparable" — the caller MUST treat that as a
    rejection, never a pass-through (see ``RELABEL_MAGNITUDE_RATIO``)."""
    if a is None or b is None:
        return None
    if a == 0 and b == 0:
        return 1.0
    if a == 0 or b == 0:
        # A jump to/from exactly zero is a real change, not a renaming
        # artifact — reject rather than silently treat as incomparable.
        return float("inf")
    return max(abs(a), abs(b)) / min(abs(a), abs(b))


class RelabelMatch(BaseModel):
    qualified_name: str
    period_gap: int
    magnitude_ratio: float
    label_containment: float


def find_relabel_target(
    stopped: TagSeries,
    last_obs: XbrlObservation,
    concepts: dict[tuple[str, str], TagSeries],
) -> RelabelMatch | None:
    """Search same-taxonomy tags for a plausible replacement for ``stopped``.

    ALL THREE gates are mandatory — period proximity alone is far too weak a
    filter on a large filer (a single accounting-standard adoption can
    introduce dozens of unrelated new tags within any given ±2-period
    window; measured on real data: mandatory magnitude + label gates below
    dropped a naive proximity-only pass from 69/72 META candidates
    "relabeled" to 3):

    1. the candidate's OWN first-ever appearance (unified cross-axis
       ``period_rank``) lands within ``RELABEL_PERIOD_WINDOW`` of
       ``last_obs``;
    2. a COMPUTABLE, comparable magnitude at the nearest overlapping period
       (``RELABEL_MAGNITUDE_RATIO`` — a pairing with no computable magnitude
       is REJECTED, never passed through as inconclusive);
    3. the candidate's label retains at least ``RELABEL_LABEL_CONTAINMENT_MIN``
       of the stopped tag's significant label vocabulary — real XBRL
       relabels are English-description renames, and this is the strongest
       single discriminator against coincidental proximity+magnitude matches.

    Returns the closest-matching candidate (by period gap, then magnitude
    ratio), or ``None``.
    """
    stopped_rank = last_obs.period_rank
    stopped_rate = _rate(last_obs)
    stopped_tokens = _label_tokens(stopped.label)
    matches: list[RelabelMatch] = []
    for (taxonomy, tag), candidate in concepts.items():
        if taxonomy != stopped.taxonomy or tag == stopped.tag or not candidate.observations:
            continue
        first_rank = min(o.period_rank for o in candidate.observations)
        gap = first_rank - stopped_rank
        if abs(gap) > RELABEL_PERIOD_WINDOW:
            continue

        # Directional containment (not Jaccard): a relabel's new description
        # is typically a superset/expansion of the old one ("Payments to
        # Acquire Property, Plant and Equipment" -> "...Productive Assets"),
        # so "how much of the OLD vocabulary survived" is the right measure.
        candidate_tokens = _label_tokens(candidate.label)
        containment = (
            len(stopped_tokens & candidate_tokens) / len(stopped_tokens) if stopped_tokens else 0.0
        )
        if containment < RELABEL_LABEL_CONTAINMENT_MIN:
            continue
        if _has_antonym_conflict(stopped_tokens, candidate_tokens):
            continue

        def _proximity(o: XbrlObservation) -> tuple[int, int]:
            try:
                end_delta = abs((date.fromisoformat(o.end) - date.fromisoformat(last_obs.end)).days)
            except ValueError:
                end_delta = 10**9
            return (abs(o.period_rank - stopped_rank), end_delta)

        nearest = min(candidate.observations, key=_proximity)
        ratio = _magnitude_ratio(stopped_rate, _rate(nearest))
        if ratio is None or ratio > RELABEL_MAGNITUDE_RATIO:
            continue
        matches.append(
            RelabelMatch(
                qualified_name=f"{taxonomy}:{tag}",
                period_gap=gap,
                magnitude_ratio=ratio,
                label_containment=containment,
            )
        )
    if not matches:
        return None
    return min(matches, key=lambda m: (abs(m.period_gap), m.magnitude_ratio))


def naive_candidate_count(concepts: dict[tuple[str, str], TagSeries]) -> tuple[int, list[str]]:
    """Reproduces the naive baseline from the empirical calibration session:
    a tag reported in >= ``MIN_OBSERVATIONS`` distinct periods, absent from
    the ticker's 4 most recent distinct periodic-report end dates (mixing
    forms/axes — exactly the conflation Stage 0 exists to fix). Used ONLY
    for the before/after reporting comparison, never for real detection.
    """
    all_ends: set[str] = set()
    for s in concepts.values():
        for o in s.observations:
            if o.axis is not None:
                all_ends.add(o.end)
    recent4 = set(sorted(all_ends)[-4:])

    flagged: list[str] = []
    for (taxonomy, tag), s in concepts.items():
        distinct_ends = {o.end for o in s.observations if o.axis is not None}
        if len(distinct_ends) < MIN_OBSERVATIONS:
            continue
        if distinct_ends & recent4:
            continue
        flagged.append(f"{taxonomy}:{tag}")
    return len(flagged), flagged


def _stage0_candidates(
    ticker: str,
    concepts: dict[tuple[str, str], TagSeries],
    axis_summaries: dict[Axis, AxisSummary],
) -> tuple[list[MetricCandidate], int]:
    """Stage 0: axis-aware, gap-calibrated absence detection (noise sources
    1+2). Returns ``(candidates, tags_skipped_no_min_observations)``.

    Factored out of ``run_lifecycle_detection`` so ``build_standard_transition_corpus``
    can reuse the identical per-ticker Stage 0 pass when building the
    cross-sectional histogram — the two must compute IDENTICAL candidate
    sets, or the cross-ticker counts would be comparing apples to oranges.
    """
    stage0: list[MetricCandidate] = []
    skipped_min_obs = 0
    for (taxonomy, tag), s in concepts.items():
        axes_with_data = {o.axis for o in s.observations if o.axis is not None}
        if not axes_with_data:
            continue

        # "Still alive elsewhere" gate: a tag currently reported (within the
        # last period or so) on ANY axis it has ever appeared on is not
        # discontinued — at most its disclosure frequency changed, a
        # different, more nuanced signal this detector deliberately does
        # not claim to detect. This is what kills axis conflation: an
        # annual-only tag has zero quarterly observations, so it is never
        # even evaluated against the quarterly axis in the first place.
        alive = False
        for axis in axes_with_data:
            summary = axis_summaries.get(axis)
            if summary is None:
                continue
            axis_obs = [o for o in s.observations if o.axis is axis]
            last_rank = max(_axis_native_rank(o) for o in axis_obs)
            if summary.last_period_rank - last_rank <= 1:
                alive = True
                break
        if alive:
            continue

        hit: TagLifecycleStats | None = None
        had_any_qualifying_axis = False
        for axis in axes_with_data:
            summary = axis_summaries.get(axis)
            if summary is None:
                continue
            stats = compute_tag_lifecycle(s, axis, summary)
            if stats is None:
                continue
            had_any_qualifying_axis = True
            if (
                stats.current_silence >= MIN_CURRENT_SILENCE
                and stats.current_silence > stats.historical_max_gap
                and (hit is None or stats.current_silence > hit.current_silence)
            ):
                hit = stats
        if not had_any_qualifying_axis:
            skipped_min_obs += 1
            continue
        if hit is None:
            continue

        anchor = _scale_anchor(
            concepts, taxonomy, accn=hit.last_observation.accn, near_end=hit.last_observation.end
        )
        materiality = (abs(hit.last_observation.val) / anchor) if anchor else None
        stage0.append(
            MetricCandidate(
                ticker=ticker,
                taxonomy=taxonomy,
                tag=tag,
                label=s.label,
                axis=hit.axis,
                last_observation=hit.last_observation,
                current_silence=hit.current_silence,
                historical_max_gap=hit.historical_max_gap,
                materiality=materiality,
            )
        )
    return stage0, skipped_min_obs


def list_cached_tickers(repo_root: Path) -> list[str]:
    """Every ticker with a cached companyfacts payload — the corpus scope
    for the standard-transition gate. Deliberately the FULL cached book
    (``data/historical/sec/*_companyfacts.json``), not just the actively
    held/tracked portfolio: cross-sectional statistical power comes from
    MORE comparison points, and the confirmed cross-ticker clustering
    (module docstring, noise source 4) includes plenty of pure comparables
    that are never held positions."""
    sec_dir = repo_root / "data" / "historical" / "sec"
    if not sec_dir.is_dir():
        return []
    return sorted(
        p.name.removesuffix("_companyfacts.json") for p in sec_dir.glob("*_companyfacts.json")
    )


class StandardTransitionCorpus(BaseModel):
    """Per-tag histogram of (ticker, stop-period-rank) built from a Stage 0
    sweep across every cached ticker — the P2 "cross-sectional detrending"
    principle (``docs/design/disclosure_change_build_stack.md``) applied at
    tag level. Build ONCE per run (``build_standard_transition_corpus``),
    never per ticker — it is a full corpus pass.
    """

    #: qualified_name -> [(ticker, unified period_rank), ...] for every
    #: ticker whose Stage 0 pass flagged that tag as absent.
    stop_events: dict[str, list[tuple[str, int]]] = Field(
        default_factory=dict[str, list[tuple[str, int]]]
    )
    #: Tickers actually swept into this corpus (had cached companyfacts).
    tickers_covered: list[str] = Field(default_factory=list[str])

    def cross_ticker_count(
        self, qualified_name: str, *, exclude_ticker: str, near_rank: int, window: int
    ) -> int:
        """How many OTHER tickers stopped ``qualified_name`` within
        ``window`` unified periods of ``near_rank``."""
        others = {
            t
            for t, r in self.stop_events.get(qualified_name, [])
            if t != exclude_ticker and abs(r - near_rank) <= window
        }
        return len(others)


def build_standard_transition_corpus(
    repo_root: Path, tickers: list[str] | None = None
) -> StandardTransitionCorpus:
    """Sweep Stage 0 across every ticker (default: ``list_cached_tickers``)
    once, to power the cross-sectional standard-transition gate. Cache the
    result and reuse it across every per-ticker ``run_lifecycle_detection``
    call in a run — recomputing this per ticker would be an O(n^2) full
    corpus re-scan for every ticker instead of one.
    """
    scope = tickers if tickers is not None else list_cached_tickers(repo_root)
    stop_events: dict[str, list[tuple[str, int]]] = {}
    covered: list[str] = []
    for ticker in scope:
        series = load_tag_series(repo_root, ticker)
        if series is None:
            continue
        covered.append(ticker)
        concepts = group_by_concept(series)
        axis_summaries = compute_axis_summaries(series)
        stage0, _ = _stage0_candidates(ticker, concepts, axis_summaries)
        for cand in stage0:
            stop_events.setdefault(cand.qualified_name, []).append(
                (ticker, cand.last_observation.period_rank)
            )
    return StandardTransitionCorpus(stop_events=stop_events, tickers_covered=covered)


class LifecycleRunResult(BaseModel):
    """Per-ticker Stage 0 + Stage 1 + Stage "1.5" output, with before/after
    counts at each suppression stage for the verification report."""

    ticker: str
    naive_count: int
    after_gap_calibration: int
    after_relabel_suppression: int
    after_standard_transition_suppression: int
    candidates: list[MetricCandidate]
    relabeled_pairs: list[MetricCandidate]
    standard_transition_pairs: list[MetricCandidate]
    tags_considered: int
    tags_skipped_no_min_observations: int
    #: Whether the cross-sectional gate ran at all (``False`` when the
    #: caller passed no corpus — a single-ticker run without one is NOT
    #: silently treated as "no standard transitions found"; the CLI/report
    #: must say the gate was skipped, per the absence-is-never-silent rule).
    standard_transition_gate_ran: bool
    #: How many OTHER tickers the gate actually had to compare against
    #: (``tickers_covered`` minus this ticker if present). Zero means the
    #: gate ran but had no comparison data — also worth surfacing rather
    #: than silently reading as "confirmed company-specific".
    standard_transition_comparison_tickers: int


def run_lifecycle_detection(
    repo_root: Path,
    ticker: str,
    *,
    standard_transition_corpus: StandardTransitionCorpus | None = None,
) -> LifecycleRunResult | None:
    """Stage 0 (axis-aware, gap-calibrated absence) + Stage 1 (relabel
    suppression) + Stage "1.5" (cross-sectional standard-transition
    suppression, when ``standard_transition_corpus`` is supplied). Zero LLM.
    Returns ``None`` when the ticker has no cached companyfacts (an
    explained absence, not an error — the CLI records it as such).

    ``standard_transition_corpus`` should be built ONCE per run via
    ``build_standard_transition_corpus`` and passed to every per-ticker call
    — building it here per-ticker would silently turn an O(n) run into
    O(n^2). Passing ``None`` (e.g. a genuinely single-ticker, no-corpus
    call) skips the gate honestly: ``standard_transition_gate_ran=False`` on
    the result, never a silent "nothing found".
    """
    series = load_tag_series(repo_root, ticker)
    if series is None:
        return None
    concepts = group_by_concept(series)
    axis_summaries = compute_axis_summaries(series)
    naive_count, _ = naive_candidate_count(concepts)

    stage0, skipped_min_obs = _stage0_candidates(ticker, concepts, axis_summaries)
    after_gap = len(stage0)

    discontinued: list[MetricCandidate] = []
    relabeled: list[MetricCandidate] = []
    for cand in stage0:
        s = concepts[(cand.taxonomy, cand.tag)]
        match = find_relabel_target(s, cand.last_observation, concepts)
        if match is not None:
            relabeled.append(
                cand.model_copy(
                    update={
                        "kind": CandidateKind.RELABELED,
                        "relabeled_to": match.qualified_name,
                        "relabel_period_gap": match.period_gap,
                        "relabel_magnitude_ratio": match.magnitude_ratio,
                    }
                )
            )
        else:
            discontinued.append(cand)

    # Stage "1.5" — cross-sectional standard-transition suppression (noise
    # source 4). Runs over what's left after relabel suppression: a tag
    # already reclassified metric_relabeled never needs this check.
    final_discontinued: list[MetricCandidate] = []
    standard_transitions: list[MetricCandidate] = []
    comparison_tickers = 0
    if standard_transition_corpus is not None:
        comparison_tickers = len(
            [t for t in standard_transition_corpus.tickers_covered if t != ticker]
        )
    for cand in discontinued:
        if standard_transition_corpus is not None:
            count = standard_transition_corpus.cross_ticker_count(
                cand.qualified_name,
                exclude_ticker=ticker,
                near_rank=cand.last_observation.period_rank,
                window=RELABEL_PERIOD_WINDOW,
            )
            if count >= STANDARD_TRANSITION_MIN_OTHER_TICKERS:
                standard_transitions.append(
                    cand.model_copy(
                        update={
                            "kind": CandidateKind.STANDARD_TRANSITION,
                            "standard_transition_other_tickers": count,
                        }
                    )
                )
                continue
        final_discontinued.append(cand)

    final_discontinued.sort(key=lambda c: (c.materiality is None, -(c.materiality or 0.0)))

    return LifecycleRunResult(
        ticker=ticker,
        naive_count=naive_count,
        after_gap_calibration=after_gap,
        after_relabel_suppression=len(discontinued),
        after_standard_transition_suppression=len(final_discontinued),
        candidates=final_discontinued,
        relabeled_pairs=relabeled,
        standard_transition_pairs=standard_transitions,
        tags_considered=len(concepts),
        tags_skipped_no_min_observations=skipped_min_obs,
        standard_transition_gate_ran=standard_transition_corpus is not None,
        standard_transition_comparison_tickers=comparison_tickers,
    )


class MetricLifecycleEvent(BaseModel):
    """The write contract for one ``disclosure_events`` row (migration 0203)."""

    ticker: str
    event_type: CandidateKind
    fiscal_year: int | None
    fiscal_period: str | None
    subject: str
    subject_label: str
    prior_excerpt: str | None = None
    current_excerpt: str | None = None
    evidence_quote: str
    materiality: float | None
    verdict: str = "unclassified"
    interpretation_md: str | None = None
    confidence: float | None = None
    detector_version: str = DETECTOR_VERSION


def candidate_to_event(
    candidate: MetricCandidate,
    *,
    verdict: str = "unclassified",
    interpretation_md: str | None = None,
) -> MetricLifecycleEvent:
    """Every event carries a verbatim quote: the tag's last reported value
    and period, which for XBRL data (not prose) IS the receipt."""
    evidence = (
        f"{candidate.qualified_name} ({candidate.label}) = {candidate.last_value:,.0f} "
        f"as of {candidate.last_period_label} [{candidate.axis.value} axis]"
    )
    if candidate.kind is CandidateKind.RELABELED:
        ratio_part = (
            ""
            if candidate.relabel_magnitude_ratio is None
            else f", magnitude ratio {candidate.relabel_magnitude_ratio:.2f}x"
        )
        current_excerpt = (
            f"relabeled to {candidate.relabeled_to} "
            f"(debut {candidate.relabel_period_gap:+d} unified period(s) from stop{ratio_part})"
        )
    elif candidate.kind is CandidateKind.STANDARD_TRANSITION:
        current_excerpt = (
            f"{candidate.standard_transition_other_tickers} other cached ticker(s) stopped "
            f"this same tag within {RELABEL_PERIOD_WINDOW} periods — an accounting-standard "
            "transition, not a company-specific decision (no successor tag claimed)"
        )
    else:
        current_excerpt = (
            f"silent for {candidate.current_silence} {candidate.axis.value} period(s); "
            f"historical max gap was {candidate.historical_max_gap}"
        )
    return MetricLifecycleEvent(
        ticker=candidate.ticker,
        event_type=candidate.kind,
        fiscal_year=candidate.last_observation.fiscal_year,
        fiscal_period=candidate.last_observation.fiscal_period,
        subject=candidate.qualified_name,
        subject_label=candidate.label,
        prior_excerpt=evidence,
        current_excerpt=current_excerpt,
        evidence_quote=evidence,
        materiality=candidate.materiality,
        verdict=verdict,
        interpretation_md=interpretation_md,
    )


def _now_naive_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def write_lifecycle_events(conn: sqlite3.Connection, events: list[MetricLifecycleEvent]) -> int:
    """Idempotent upsert on ``disclosure_events``'s unique key (ticker,
    event_type, fiscal_year, fiscal_period, subject, detector_version).

    Uses an explicit NULL-safe lookup rather than ``ON CONFLICT``, matching
    ``filings.store.record_coverage``: SQLite does not treat two NULL
    ``fiscal_year``/``fiscal_period`` values as conflicting in a UNIQUE
    index, so ``ON CONFLICT`` would silently accumulate duplicates for
    exactly the rows whose period we failed to resolve.
    """
    if not events:
        return 0
    if not table_exists(conn, EVENTS_TABLE):
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0203) first"
        )
    now = _now_naive_utc()
    written = 0
    for e in events:
        existing = conn.execute(
            f"""
            SELECT id FROM {EVENTS_TABLE}
            WHERE ticker = ? AND event_type = ? AND fiscal_year IS ? AND fiscal_period IS ?
              AND subject = ? AND detector_version = ?
            LIMIT 1
            """,
            (
                e.ticker,
                e.event_type.value,
                e.fiscal_year,
                e.fiscal_period,
                e.subject,
                e.detector_version,
            ),
        ).fetchone()
        common = (
            e.subject_label,
            e.prior_excerpt,
            e.current_excerpt,
            e.evidence_quote,
            e.materiality,
            e.verdict,
            e.interpretation_md,
            e.confidence,
        )
        if existing is None:
            conn.execute(
                f"""
                INSERT INTO {EVENTS_TABLE}
                    (ticker, event_type, fiscal_year, fiscal_period, subject, subject_label,
                     prior_excerpt, current_excerpt, evidence_quote, materiality, verdict,
                     interpretation_md, confidence, detector_version, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e.ticker,
                    e.event_type.value,
                    e.fiscal_year,
                    e.fiscal_period,
                    e.subject,
                    *common,
                    e.detector_version,
                    "new",
                    now,
                ),
            )
        else:
            conn.execute(
                f"""
                UPDATE {EVENTS_TABLE}
                SET subject_label=?, prior_excerpt=?, current_excerpt=?, evidence_quote=?,
                    materiality=?, verdict=?, interpretation_md=?, confidence=?
                WHERE id=?
                """,
                (*common, int(existing[0])),
            )
        written += 1
    return written
