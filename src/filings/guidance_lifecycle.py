"""D2.1 guidance-withdrawal detector (docs/design/disclosure_intelligence_v1_prd.md
D2.1; evidence: Zhou & Zhou, *JAR* 2020, -41bps around the next earnings
announcement for expected-but-missing disclosure; Chen, Matsumoto & Rajgopal,
*JAE* 2011, -4.8% 3-day CAR for EPS-guidance stoppers).

Per ``docs/design/disclosure_gap_scoping.md`` Gap 1: this is **not** a second
detector. ``filings.metric_lifecycle`` is, read structurally, a general
"expected periodic disclosure, judged against the issuer's own cadence"
engine where the subject happens to be an XBRL ``(taxonomy, tag)`` pair. This
module feeds that SAME engine a second subject population — "did this ticker
issue guidance this period?" — by reusing two of its exports directly:

* ``filings.metric_lifecycle.gap_calibration`` — the Stage 0 own-cadence math
  (historical_max_gap / current_silence), extracted to be subject-agnostic
  for exactly this reuse.
* ``filings.metric_lifecycle.StandardTransitionCorpus`` — Stage "1.5"'s
  cross-sectional suppression gate (already keyed by an arbitrary string, not
  literally an XBRL tag name), reused here under a single shared key
  (``GUIDANCE_WAVE_KEY``) so a macro-wide guidance-suspension wave (the
  COVID-2020 case Call, Melessa & Volant (2024 WP) document — ~40% of
  suspenders never restarted and subsequently OUTPERFORMED) reclassifies as
  ``mechanical`` rather than a company-specific decision. **Do not encode
  "stopped = bearish" anywhere downstream of Stage 0** — every
  ``guidance_withdrawn`` candidate stays ``verdict="unclassified"`` until
  ``filings.guidance_triage`` (or a human) judges it, exactly like
  ``metric_lifecycle``'s ``metric_discontinued``.

Stage 1 (relabel suppression) has **no analog here** — deliberately, not by
omission. XBRL relabeling is a noise source because Stage 0 tracks individual
TAGS (a company can swap "Payments to Acquire PP&E" for "...Productive
Assets" and look like two different subjects). This module tracks guidance at
TICKER grain (Lane A) or MD&A-heading grain (Lane B), never per-KPI — a
company swapping a quarterly EPS point estimate for a full-year revenue range
is invisible to a ticker-level "did they guide at all this quarter" signal or
a heading-level "does this Outlook sub-section still exist" signal, so the
relabel-noise-source Stage 1 exists to suppress for XBRL tags does not arise
at this grain in the first place.

Two independent, complementary candidate lanes (``docs/design/
disclosure_gap_scoping.md`` Gap 1's "what genuinely does not exist yet"):

**Lane A — management_commitments own-cadence** (``load_commitment_periods``,
``detect_commitment_lifecycle``). The scoping doc's own-cadence engine
generalization, applied to Say-Do's forward-looking commitment feed
(``compute.say_do`` / migration 0017). The scoping doc calls out its own
"single biggest correctness risk, bigger than anything in the literature
caveats": ``management_commitments`` extraction is session-triggered, not a
guaranteed per-quarter batch job, so an absent commitment can mean "no
guidance this quarter" OR "extraction was never attempted this quarter" —
indistinguishable without a coverage marker. Measured on the real cached book
(2026-07-25): of 637 persisted commitments across 76 tickers, only 8 came
through a ``commitment_scan_log``-marked ``--auto`` run; the rest arrived via
session ``--apply`` calls that never touch ``commitment_scan_log`` at all. So
a period counts as **coverage-known** here only when it has EITHER a real
commitment OR a ``commitment_scan_log`` row for one of its transcripts —
periods with neither are dropped from the series entirely (never counted as
presence, never counted as a gap), and ``current_silence`` is measured
against the ticker's own last COVERAGE-KNOWN period, never wall-clock "most
recent transcript" (which could itself be a coverage-unknown period and would
manufacture a spurious "just went silent" reading against data we never
looked at).

**Lane B — MD&A guidance/outlook heading own-cadence**
(``load_mdna_guidance_periods``, ``detect_mdna_lifecycle``). Corpus check run
against the real cached book (2026-07-25) confirmed MD&A splitting DOES
capture "guidance"/"outlook" sub-headings — but also confirmed a sharp,
UNANTICIPATED false-positive source: most real "*Guidance*" MD&A headings are
**"Recently Adopted Accounting Guidance"** / "Recent Accounting Guidance"
(AVGO, QCOM) — ASU/ASC footnote boilerplate, not forward operating/financial
guidance at all. ``_ACCOUNTING_GUIDANCE_RX`` exists specifically to reject
these. Real "Outlook" headings (AGX, DHR) DO exist and DO come and go, but a
naive one-shot ``item_removed`` keyword scan cannot tell a genuine withdrawal
from ordinary heading churn (AGX's "Market Outlook" split into "Outlook for
Natural Gas-Fired Power Plants" + "Industrial Construction Services Outlook"
the very next year — a relabel-shaped split, not a withdrawal) or from a
structurally recurring pattern (DHR's "Business Performance and Outlook"
disappears every Q3 for four straight years — a cadence quirk, not a
withdrawal). So Lane B does NOT scan ``item_removed`` rows directly; it
builds a per-(ticker, match_key) presence series from
``filing_section_items`` (the same "own cadence" math as Lane A and as P1),
which is what correctly absorbs both real cases above: DHR's recurring Q3 gap
becomes its own ``historical_max_gap`` and never flags; a genuine NEW,
longer-than-usual silence would.

Neither lane's Stage 0 output is ever written to ``disclosure_events``
without Stage 2/3 triage (``filings.guidance_triage``) — the scoping doc's
"small triage for what counts as a guidance statement" — because both lanes'
raw candidates conflate "this reads as forward guidance" with "this is
industry commentary / accounting boilerplate that happens to contain the
word".
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from filings import section_items
from filings.metric_lifecycle import StandardTransitionCorpus, gap_calibration
from filings.models import HardStopError

log = logging.getLogger(__name__)

EVENTS_TABLE = "disclosure_events"
DETECTOR_VERSION_COMMITMENTS = "guidance_lifecycle_commitments_v1"
DETECTOR_VERSION_MDNA = "guidance_lifecycle_mdna_v1"

#: Quarterly call cadence needs less history than XBRL's dual annual/quarterly
#: axis to judge a real pattern, but still enough to avoid over-fitting a
#: 2-3-observation "cadence". Four quarters (one year) is the minimum any
#: reasonable reader would accept as "this ticker has an established habit".
MIN_OBSERVATIONS = 4
MIN_CURRENT_SILENCE = 2

#: Stage "1.5" cross-sectional wave-suppression gate — reuses
#: ``StandardTransitionCorpus`` under ONE shared key (every ticker's stop is
#: recorded against the same "guidance_practice" subject) so it answers "how
#: many OTHER tickers also stopped guiding in the same window", not "did
#: another ticker stop the same named tag" (there is no cross-ticker shared
#: tag identity at this grain — the confound is about TIMING coincidence).
GUIDANCE_WAVE_KEY = "guidance_practice"
MDNA_WAVE_KEY = "mdna_guidance_heading"
#: Unified-quarter window for the wave gate — mirrors
#: ``metric_lifecycle.RELABEL_PERIOD_WINDOW``'s role but tuned separately:
#: call cadence is coarser than XBRL fact cadence.
WAVE_PERIOD_WINDOW = 1
#: Lower than XBRL's STANDARD_TRANSITION_MIN_OTHER_TICKERS=2 would be too
#: sensitive at this book's size (~44-99 tracked names, most without commitment
#: coverage at all) -- require a wider synchronized cluster before
#: reclassifying a candidate as a macro wave rather than company-specific.
WAVE_MIN_OTHER_TICKERS = 3

#: Real false positive confirmed on the cached book (2026-07-25): "Recently
#: Adopted Accounting Guidance" / "Recent Accounting Guidance" (AVGO, QCOM) —
#: ASU/ASC footnote headings, not forward operating/financial guidance. Any
#: heading matching this is rejected outright, even if it also matches
#: ``_OUTLOOK_RX``.
_ACCOUNTING_GUIDANCE_RX = re.compile(
    r"account(ing)?\s+guidance|guidance\s+.{0,20}\baccounting\b|\bASU\b|\bASC\b|\bFASB\b|"
    r"recently\s+(adopted|issued)",
    re.IGNORECASE,
)
_OUTLOOK_RX = re.compile(r"\bguidance\b|\boutlook\b|\bforecast\b", re.IGNORECASE)

#: Mirrors ``filings.cross_sectional_detrend``'s identical convention
#: (duplicated rather than imported — small, stable literal, per the repo's
#: "duplicate simple shared logic" convention rather than a cross-module
#: coupling for six characters of dict).
_PERIOD_RANK: dict[str, int] = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}


def _require_events_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (EVENTS_TABLE,)
    ).fetchone()
    if row is None:
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0203) first"
        )


def looks_like_guidance_heading(heading: str | None) -> bool:
    """Lane B's relevance pre-filter: matches an outlook/guidance/forecast
    heading UNLESS it also reads as accounting-standard boilerplate (the
    confirmed AVGO/QCOM false positive — see module docstring)."""
    if not heading:
        return False
    if _ACCOUNTING_GUIDANCE_RX.search(heading):
        return False
    return bool(_OUTLOOK_RX.search(heading))


def _quarter_rank(iso_date: str) -> int:
    """Unified year*4+quarter rank from an ISO date string's calendar
    quarter. Guidance is a call-cadence concept (tied to when the ticker
    talks, not to its XBRL fiscal-year-end), so this deliberately does NOT
    reuse metric_lifecycle's fiscal-year-aware axis machinery — a calendar
    quarter is a stable, company-agnostic clock for "how long since anyone
    last spoke"."""
    year = int(iso_date[0:4])
    month = int(iso_date[5:7])
    quarter = (month - 1) // 3 + 1
    return year * 4 + quarter


# ---------------------------------------------------------------------------
# Lane A — management_commitments own-cadence
# ---------------------------------------------------------------------------


class CommitmentPeriod(BaseModel):
    """One (ticker, calendar-quarter-of-period_end) bucket, coverage-graded.

    ``coverage_known`` is True only when at least one transcript in this
    bucket has a real commitment OR a ``commitment_scan_log`` row — see
    module docstring for why this distinction is the single biggest
    correctness gate for this lane.
    """

    period_end: str
    period_rank: int
    n_commitments: int
    coverage_known: bool


def load_commitment_periods(conn: sqlite3.Connection, ticker: str) -> list[CommitmentPeriod]:
    """Group every transcript for ``ticker`` by ``period_end`` (NOT by raw
    ``transcript_id`` — the same reporting period commonly has more than one
    transcript row from different sources, e.g. an aggregator AND a FactSet
    pull; treating those as separate periods would fabricate phantom gaps),
    counting commitments across ANY of that period's transcripts and marking
    coverage-known when ANY of them has a commitment or a scan-log row."""
    rows = conn.execute(
        """
        SELECT tr.period_end,
               COUNT(DISTINCT mc.id) AS n_commit,
               MAX(CASE WHEN csl.transcript_id IS NOT NULL THEN 1 ELSE 0 END) AS any_scanned
        FROM transcripts tr
        LEFT JOIN transcript_segments ts ON ts.transcript_id = tr.id
        LEFT JOIN management_commitments mc ON mc.transcript_segment_id = ts.id
        LEFT JOIN commitment_scan_log csl ON csl.transcript_id = tr.id
        WHERE tr.ticker = ? AND tr.period_end IS NOT NULL
        GROUP BY tr.period_end
        ORDER BY tr.period_end
        """,
        (ticker.strip().upper(),),
    ).fetchall()
    out: list[CommitmentPeriod] = []
    for period_end, n_commit, any_scanned in rows:
        pe = str(period_end)[:10]
        out.append(
            CommitmentPeriod(
                period_end=pe,
                period_rank=_quarter_rank(pe),
                n_commitments=int(n_commit or 0),
                coverage_known=bool(n_commit) or bool(any_scanned),
            )
        )
    return out


def all_commitment_tickers(conn: sqlite3.Connection) -> list[str]:
    """Every ticker with at least one transcript-linked coverage signal
    (a commitment OR a scan-log row) — the corpus scope for the Stage 1.5
    wave gate. Deliberately the full set with ANY signal, not just tickers
    with commitments, so a ticker that was scanned and found nothing still
    counts as a real (negative) comparison point."""
    rows = conn.execute(
        """
        SELECT DISTINCT tr.ticker
        FROM transcripts tr
        WHERE EXISTS (
            SELECT 1 FROM transcript_segments ts
            JOIN management_commitments mc ON mc.transcript_segment_id = ts.id
            WHERE ts.transcript_id = tr.id
        ) OR EXISTS (
            SELECT 1 FROM commitment_scan_log csl WHERE csl.transcript_id = tr.id
        )
        ORDER BY tr.ticker
        """
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


@dataclass(slots=True)
class GuidanceCandidate:
    """A ticker (Lane A) or (ticker, match_key) (Lane B) flagged as either
    having gone abnormally silent (``kind="guidance_withdrawn"``) or having
    resumed after an abnormal silence (``kind="guidance_resumed"``) —
    before Stage 2/3 triage (``filings.guidance_triage``)."""

    ticker: str
    kind: str  # 'guidance_withdrawn' | 'guidance_resumed'
    lane: str  # 'commitments' | 'mdna_heading'
    subject_key: str  # ticker itself (Lane A) or match_key (Lane B)
    subject_label: str
    last_present_period: str
    last_present_rank: int
    as_of_period: str
    as_of_rank: int
    current_silence: int
    historical_max_gap: int
    n_known_periods: int
    standard_transition_other_tickers: int | None = None


@dataclass(slots=True)
class CommitmentLifecycleResult:
    ticker: str
    n_known_periods: int
    n_present_periods: int
    withdrawn: GuidanceCandidate | None
    resumed: GuidanceCandidate | None
    #: Too few coverage-known present periods to judge a cadence at all.
    insufficient_history: bool


def detect_commitment_lifecycle(
    ticker: str, periods: list[CommitmentPeriod]
) -> CommitmentLifecycleResult:
    """Stage 0 for Lane A: own-cadence gap calibration over coverage-known
    periods only. Pure — no DB access, so it's independently testable against
    synthetic period sequences."""
    known = sorted((p for p in periods if p.coverage_known), key=lambda p: p.period_rank)
    present = [p for p in known if p.n_commitments > 0]

    if len(present) < MIN_OBSERVATIONS or not known:
        return CommitmentLifecycleResult(
            ticker=ticker,
            n_known_periods=len(known),
            n_present_periods=len(present),
            withdrawn=None,
            resumed=None,
            insufficient_history=True,
        )

    ranks = [p.period_rank for p in present]
    as_of = known[-1]

    withdrawn: GuidanceCandidate | None = None
    calib = gap_calibration(ranks, as_of.period_rank, min_observations=MIN_OBSERVATIONS)
    if calib is not None and (
        calib.current_silence >= MIN_CURRENT_SILENCE
        and calib.current_silence > calib.historical_max_gap
    ):
        withdrawn = GuidanceCandidate(
            ticker=ticker,
            kind="guidance_withdrawn",
            lane="commitments",
            subject_key=ticker,
            subject_label=f"{ticker} management guidance practice",
            last_present_period=present[-1].period_end,
            last_present_rank=ranks[-1],
            as_of_period=as_of.period_end,
            as_of_rank=as_of.period_rank,
            current_silence=calib.current_silence,
            historical_max_gap=calib.historical_max_gap,
            n_known_periods=len(known),
        )

    # Resumption: the ticker's last KNOWN period is itself present, and the
    # gap immediately before it exceeded the cadence established by every
    # EARLIER present period — the symmetric counterpart to withdrawal,
    # checked one step back rather than at the trailing edge.
    resumed: GuidanceCandidate | None = None
    if known[-1].period_rank == ranks[-1] and len(ranks) >= MIN_OBSERVATIONS + 1:
        prior_ranks = ranks[:-1]
        calib2 = gap_calibration(prior_ranks, ranks[-1], min_observations=MIN_OBSERVATIONS)
        if calib2 is not None and (
            calib2.current_silence >= MIN_CURRENT_SILENCE
            and calib2.current_silence > calib2.historical_max_gap
        ):
            resumed = GuidanceCandidate(
                ticker=ticker,
                kind="guidance_resumed",
                lane="commitments",
                subject_key=ticker,
                subject_label=f"{ticker} management guidance practice",
                last_present_period=present[-1].period_end,
                last_present_rank=ranks[-1],
                as_of_period=as_of.period_end,
                as_of_rank=as_of.period_rank,
                current_silence=calib2.current_silence,
                historical_max_gap=calib2.historical_max_gap,
                n_known_periods=len(known),
            )

    return CommitmentLifecycleResult(
        ticker=ticker,
        n_known_periods=len(known),
        n_present_periods=len(present),
        withdrawn=withdrawn,
        resumed=resumed,
        insufficient_history=False,
    )


# ---------------------------------------------------------------------------
# Lane B — MD&A guidance/outlook heading own-cadence
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MdnaHeadingObservation:
    fiscal_year: int | None
    fiscal_period: str
    heading: str
    body_excerpt: str


def _mdna_unified_rank(fiscal_year: int | None, fiscal_period: str) -> int:
    return (fiscal_year or 0) * 5 + _PERIOD_RANK.get(fiscal_period, 0)


def load_mdna_guidance_periods(
    conn: sqlite3.Connection, ticker: str
) -> dict[str, list[MdnaHeadingObservation]]:
    """match_key -> every period this heading was present, oldest first.
    Restricted to headings that pass ``looks_like_guidance_heading``; every
    OTHER mdna heading is irrelevant to this detector."""
    items = section_items.get_items(conn, ticker, canonical_id="mdna", missing_ok=True)
    by_key: dict[str, list[MdnaHeadingObservation]] = {}
    for it in items:
        if not looks_like_guidance_heading(it.heading):
            continue
        by_key.setdefault(it.match_key, []).append(
            MdnaHeadingObservation(
                fiscal_year=it.fiscal_year,
                fiscal_period=it.fiscal_period.value,
                heading=it.heading or it.match_key,
                body_excerpt=" ".join(it.body.split())[:280],
            )
        )
    for obs_list in by_key.values():
        obs_list.sort(key=lambda o: _mdna_unified_rank(o.fiscal_year, o.fiscal_period))
    return by_key


def _mdna_last_known_rank(conn: sqlite3.Connection, ticker: str) -> int | None:
    """The ticker's own last stored mdna-section period, ACROSS every
    heading — the "as of" reference (mirrors ``compute_axis_summaries``):
    a heading missing from the last filed mdna section is real silence; the
    ticker simply having no recent filing at all is a data-coverage
    question, not a guidance signal."""
    items = section_items.get_items(conn, ticker, canonical_id="mdna", missing_ok=True)
    if not items:
        return None
    return max(_mdna_unified_rank(it.fiscal_year, it.fiscal_period.value) for it in items)


def detect_mdna_lifecycle(conn: sqlite3.Connection, ticker: str) -> list[GuidanceCandidate]:
    """Stage 0 for Lane B: one gap-calibration per (ticker, match_key)
    guidance/outlook heading. Returns withdrawn candidates only — a resumed
    heading is a less interesting Lane B signal (a heading reappearing after
    ordinary MD&A restructuring churn is common and low-value) and is
    intentionally not built here to avoid manufacturing noise the scoping
    doc didn't ask for."""
    as_of_rank = _mdna_last_known_rank(conn, ticker)
    if as_of_rank is None:
        return []
    by_key = load_mdna_guidance_periods(conn, ticker)
    out: list[GuidanceCandidate] = []
    for match_key, obs_list in by_key.items():
        ranks = [_mdna_unified_rank(o.fiscal_year, o.fiscal_period) for o in obs_list]
        calib = gap_calibration(ranks, as_of_rank, min_observations=MIN_OBSERVATIONS)
        if calib is None:
            continue
        if calib.current_silence >= MIN_CURRENT_SILENCE and (
            calib.current_silence > calib.historical_max_gap
        ):
            last = obs_list[-1]
            out.append(
                GuidanceCandidate(
                    ticker=ticker,
                    kind="guidance_withdrawn",
                    lane="mdna_heading",
                    subject_key=match_key,
                    subject_label=last.heading,
                    last_present_period=f"{last.fiscal_year}{last.fiscal_period}",
                    last_present_rank=ranks[-1],
                    as_of_period=str(as_of_rank),
                    as_of_rank=as_of_rank,
                    current_silence=calib.current_silence,
                    historical_max_gap=calib.historical_max_gap,
                    n_known_periods=len(ranks),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Stage "1.5" — cross-sectional wave suppression (shared by both lanes)
# ---------------------------------------------------------------------------


def build_commitment_wave_corpus(
    conn: sqlite3.Connection, tickers: list[str] | None = None
) -> StandardTransitionCorpus:
    """Sweep Lane A's Stage 0 across every ticker with commitment coverage
    signal, once per run — mirrors
    ``metric_lifecycle.build_standard_transition_corpus``'s "build once,
    reuse per ticker" shape. Every stop is recorded under the SAME
    ``GUIDANCE_WAVE_KEY`` (there is no per-ticker "tag" identity to match —
    the confound is synchronized TIMING, not a shared label)."""
    scope = tickers if tickers is not None else all_commitment_tickers(conn)
    stop_events: dict[str, list[tuple[str, int]]] = {GUIDANCE_WAVE_KEY: []}
    covered: list[str] = []
    for ticker in scope:
        periods = load_commitment_periods(conn, ticker)
        result = detect_commitment_lifecycle(ticker, periods)
        covered.append(ticker)
        if result.withdrawn is not None:
            stop_events[GUIDANCE_WAVE_KEY].append((ticker, result.withdrawn.last_present_rank))
    return StandardTransitionCorpus(stop_events=stop_events, tickers_covered=covered)


def all_mdna_tickers(conn: sqlite3.Connection) -> list[str]:
    """Every ticker with at least one stored ``mdna`` section item — the
    corpus scope for Lane B's wave gate. Mirrors ``all_commitment_tickers``:
    the full discoverable set, never just whatever subset a particular run
    happens to be scoring (corpus BREADTH matters here exactly as it does for
    ``metric_lifecycle.build_standard_transition_corpus`` — a narrow corpus
    cannot detect a synchronized wave it wasn't given enough peers to see)."""
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM filing_section_items WHERE canonical_id = 'mdna' "
        "ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def build_mdna_wave_corpus(
    conn: sqlite3.Connection, tickers: list[str] | None = None
) -> StandardTransitionCorpus:
    """Same shape as ``build_commitment_wave_corpus``, for Lane B. Default
    scope (``tickers=None``) is EVERY ticker with mdna items, not just the
    ones a particular run is scoring — see ``all_mdna_tickers``."""
    scope = tickers if tickers is not None else all_mdna_tickers(conn)
    stop_events: dict[str, list[tuple[str, int]]] = {MDNA_WAVE_KEY: []}
    covered: list[str] = []
    for ticker in scope:
        covered.append(ticker)
        for cand in detect_mdna_lifecycle(conn, ticker):
            stop_events[MDNA_WAVE_KEY].append((ticker, cand.last_present_rank))
    return StandardTransitionCorpus(stop_events=stop_events, tickers_covered=covered)


def apply_wave_suppression(
    candidate: GuidanceCandidate,
    corpus: StandardTransitionCorpus | None,
    *,
    wave_key: str,
) -> tuple[GuidanceCandidate, bool]:
    """Returns ``(candidate, is_mechanical_wave)``. When the wave gate fires
    (>= ``WAVE_MIN_OTHER_TICKERS`` other tickers also stopped within
    ``WAVE_PERIOD_WINDOW``), the candidate is annotated but still returned —
    the caller decides verdict; this function never drops a candidate."""
    if corpus is None:
        return candidate, False
    count = corpus.cross_ticker_count(
        wave_key,
        exclude_ticker=candidate.ticker,
        near_rank=candidate.last_present_rank,
        window=WAVE_PERIOD_WINDOW,
    )
    if count >= WAVE_MIN_OTHER_TICKERS:
        candidate.standard_transition_other_tickers = count
        return candidate, True
    return candidate, False


# ---------------------------------------------------------------------------
# disclosure_events write contract
# ---------------------------------------------------------------------------


class GuidanceLifecycleEvent(BaseModel):
    """The write contract for one ``disclosure_events`` row (migration 0203)."""

    ticker: str
    event_type: str  # 'guidance_withdrawn' | 'guidance_resumed'
    canonical_id: str = ""
    subject: str
    subject_label: str
    evidence_quote: str
    prior_excerpt: str | None = None
    current_excerpt: str | None = None
    materiality: float | None = None
    verdict: str = "unclassified"
    interpretation_md: str | None = None
    confidence: float | None = None
    detector_version: str


def candidate_to_event(
    candidate: GuidanceCandidate,
    *,
    is_mechanical_wave: bool,
    verdict: str = "unclassified",
    interpretation_md: str | None = None,
) -> GuidanceLifecycleEvent:
    """Every event carries a verbatim receipt: for Lane A that's the
    coverage-known period ledger itself (there is no prose to quote — the
    "statement" is a structured fact); for Lane B it's the heading's own last
    body excerpt, which IS the prior guidance language."""
    detector_version = (
        DETECTOR_VERSION_COMMITMENTS if candidate.lane == "commitments" else DETECTOR_VERSION_MDNA
    )
    canonical_id = "" if candidate.lane == "commitments" else "mdna"
    if candidate.lane == "commitments":
        evidence = (
            f"{candidate.ticker} issued forward guidance commitments across "
            f"{candidate.n_known_periods} coverage-known quarter(s), most recently "
            f"{candidate.last_present_period}; silent for {candidate.current_silence} "
            f"quarter(s) since (as of {candidate.as_of_period}), beyond its own historical "
            f"tolerance of {candidate.historical_max_gap} quarter(s)."
        )
    else:
        evidence = (
            f"MD&A heading {candidate.subject_label!r} last present in "
            f"{candidate.last_present_period}; absent for {candidate.current_silence} "
            f"period(s) since, beyond its own historical tolerance of "
            f"{candidate.historical_max_gap} period(s)."
        )
    wave_note = (
        f" Reclassified mechanical: {candidate.standard_transition_other_tickers} other "
        "cached ticker(s) also stopped within the same window — a synchronized wave, "
        "not a company-specific decision (Call, Melessa & Volant 2024: ~40% of COVID-era "
        "guidance suspensions never restarted and subsequently outperformed)."
        if is_mechanical_wave
        else ""
    )
    return GuidanceLifecycleEvent(
        ticker=candidate.ticker,
        event_type=candidate.kind,
        canonical_id=canonical_id,
        subject=candidate.subject_key,
        subject_label=candidate.subject_label,
        evidence_quote=evidence,
        current_excerpt=wave_note or None,
        verdict="mechanical" if is_mechanical_wave else verdict,
        interpretation_md=interpretation_md,
        detector_version=detector_version,
    )


def _now_naive_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def write_guidance_events(conn: sqlite3.Connection, events: list[GuidanceLifecycleEvent]) -> int:
    """Idempotent upsert on ``disclosure_events``'s unique key (ticker,
    event_type, fiscal_year, fiscal_period, canonical_id, subject,
    detector_version). Guidance events carry no natural fiscal_year/period
    (Lane A is ticker-scoped, not filing-scoped; Lane B's "period" is a
    unified rank, not a real fiscal_year/fiscal_period pair suitable for the
    column) so both are left NULL — matching ``metric_lifecycle``'s existing
    NULL-safe SELECT-then-INSERT/UPDATE pattern (SQLite does not treat two
    NULL fiscal_year/fiscal_period as conflicting under a UNIQUE index, so
    ``ON CONFLICT`` would silently accumulate duplicates here)."""
    if not events:
        return 0
    _require_events_table(conn)
    now = _now_naive_utc()
    written = 0
    for e in events:
        existing = conn.execute(
            f"""
            SELECT id FROM {EVENTS_TABLE}
            WHERE ticker = ? AND event_type = ? AND fiscal_year IS NULL AND fiscal_period IS NULL
              AND canonical_id = ? AND subject = ? AND detector_version = ?
            LIMIT 1
            """,
            (e.ticker, e.event_type, e.canonical_id, e.subject, e.detector_version),
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
                    (ticker, event_type, canonical_id, subject, subject_label,
                     prior_excerpt, current_excerpt, evidence_quote, materiality, verdict,
                     interpretation_md, confidence, detector_version, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e.ticker,
                    e.event_type,
                    e.canonical_id,
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
