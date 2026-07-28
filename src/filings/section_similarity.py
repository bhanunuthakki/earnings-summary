"""D2.2 — detrended document-level YoY similarity (the Lazy Prices construct
itself). docs/design/disclosure_intelligence_v1_prd.md D2.2;
docs/design/disclosure_gap_scoping.md Gap 4; evidence: Cohen, Malloy &
Nguyen, *JF* 75(3), 2020 — shorting filers that changed their filing
language and going long non-changers earns up to 188bps/month; ranked on
QUINTILES of the prior year's cross-sectional distribution, never a fixed
baseline.

Per the scoping doc: this does NOT unify with D2.1's ``metric_lifecycle``/
``guidance_lifecycle`` engine (a continuous magnitude over stored text, not
an expected-vs-actual presence/absence judgment) — what it reuses is:

* ``filings.item_diff._similarity`` / ``_tokens`` — the SAME token-set
  Jaccard measure P0 already uses at item grain, applied here to whole,
  concatenated ``filing_sections`` text instead of individual item bodies
  (Cohen/Malloy/Nguyen's own unit is closer to whole-Item-1A than to
  individual risk-factor bullets, so this is one level UP from P0's grain,
  not a competing measure at the same grain).
* ``filings.cross_sectional_detrend``'s corpus-sweep + mid-rank-percentile
  shape (``build_cross_sectional_corpus`` / ``_percentile_rank``) — the SAME
  "build the whole-book distribution once, rank each ticker against it"
  pattern, applied to a continuous similarity score instead of item-change
  counts. The percentile function is duplicated rather than imported (a
  ~6-line pure function; matches the repo's "duplicate simple shared logic"
  convention over a cross-module coupling for it).

**Mandatory detrending, per two independent axes** (owner ruling 1: cross-
company analysis is industry/peer-group comparison, not wide-index
screening):

1. **Same-period whole-tracked-book percentile** — Dyer, Lang &
   Stice-Lawrence, *JAE* 64, 2017: median 10-K length roughly doubled
   1996-2013 for reasons unrelated to any one firm. Skipping this
   "isn't a minor omission — it inverts the finding" (scoping doc). Always
   attempted; requires ``MIN_CROSS_SECTION_PEERS`` peers in the same
   (canonical_id, fiscal_year, fiscal_period) bucket or the event is not
   emitted at all (never persisted un-detrended).
2. **Same-period comparable-set (peer-group) percentile** — reuses
   ``compute.comparable_sets``' frozen ``comparable_set_members`` (built by
   ``execution/build_comparable_sets.py``; NOT re-resolved here). When a
   ticker has no frozen comparable set, or too few peers carry a score in
   the bucket, the peer percentile is honestly absent (``None`` +
   ``peer_reason``) — book-level percentile alone still ships, per the
   owner's ruling that book-level-only is an acceptable honest degradation.

**Where the two percentiles live in ``disclosure_events`` (migration 0203),
and why**: the scoping doc's own steer is to reuse the existing schema with
zero migration ("fits with... a fixed informational verdict... not a schema
question"), and P2 (``cross_sectional_detrend``) already establishes the
precedent that ``materiality`` IS a persisted percentile for an event type
that starts with no other natural magnitude. This module follows that
precedent:

* ``materiality`` = the WHOLE-BOOK percentile (the mandatory, always-gating
  one).
* ``confidence`` = the peer-group (comparable-set) percentile. This
  deliberately repurposes a column named for LLM confidence — there is no
  LLM involved in this detector at all — because it is the one other
  queryable float column disclosure_events offers, and a WHERE-clause on
  peer standing is exactly the kind of query the workspace surface (D4) will
  want. Every reader of a ``section_similarity_shift`` row must treat
  ``confidence`` as "peer-group percentile", never as a judgment confidence;
  documented here, in the CLI's help text, and in each row's
  ``interpretation_md``.
* ``interpretation_md`` carries a deterministic (NOT LLM-authored) structured
  note: raw similarity, raw change magnitude, book percentile + peer count,
  peer percentile + comp-set id + peer count (or the reason it is absent).
  If D4 surfaces later need to sort/filter peer-percentile independently of
  this free text, promoting it to a first-class column (or a small dedicated
  table) is a clean, low-risk follow-up — flagged here rather than built
  now, per the PRD's "little or no new surface" instruction.
* ``verdict`` stays ``"unclassified"`` — Cohen/Malloy/Nguyen's finding is
  "change is bad news" essentially unconditionally, so unlike D2.1 there is
  no concealment-vs-maturity judgment call to make, but "unclassified" (never
  a NEW verdict vocabulary value) matches ``item_diff``'s existing stance for
  ``item_reworded``: a magnitude of change, direction never implied by the
  event type.

**No abnormal return around the filing date — this is drift, not a day-of
alert.** A surface consuming this event type must present it as relevant for
weeks, never as news of the day (the ruling's general surface rule, uniquely
load-bearing here since this is literally the paper's own structural
finding).

**Section-boundary quality risk applies MORE severely here than at item
grain** (a bad splitting boundary corrupts the WHOLE aggregate, not one
item) — this module does not itself re-check ``filing_section_coverage``
flags; a caller wanting that gate should cross-reference
``filings.store.get_coverage`` before trusting a score, exactly as already
documented for item-level diffs.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise

from compute.comparable_sets import comparable_set_id
from filings import store
from filings.item_diff import (  # intra-package reuse, see module docstring
    _quote,  # pyright: ignore[reportPrivateUsage]
    _similarity,  # pyright: ignore[reportPrivateUsage]
)
from filings.models import FilingSection, HardStopError
from provenance.selection import selected_filing_sections_relation

log = logging.getLogger(__name__)

EVENTS_TABLE = "disclosure_events"
DETECTOR_VERSION = "section_similarity_v1"

#: Below this many OTHER tickers in the same-period cross-section, a
#: percentile is statistically meaningless — mirrors
#: ``cross_sectional_detrend.MIN_CROSS_SECTION_PEERS`` exactly.
MIN_CROSS_SECTION_PEERS = 3

#: Mirrors ``cross_sectional_detrend``'s identical convention.
_PERIOD_RANK: dict[str, int] = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}

_SENTENCE_RX = re.compile(r"(?<=[.!?])\s+")
_MAX_SENTENCES_SCANNED = 200


# ---------------------------------------------------------------------------
# Whole-section text per period
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SectionPeriodText:
    """One (ticker, canonical_id, fiscal_year, fiscal_period)'s concatenated
    text across EVERY stored ``filing_sections`` row for that period (any
    source, any ordinal) — the same "merge everything in this period bucket"
    convention ``item_diff.diff_ticker_concept`` already uses, so whole-
    section scoring stays consistent with the item-level P0 pipeline it
    complements rather than inventing a stricter single-source rule."""

    ticker: str
    canonical_id: str
    fiscal_year: int | None
    fiscal_period: str
    text: str
    source_doc_id: int | None
    source_ref: str | None


def load_concatenated_section_periods(
    conn: sqlite3.Connection, ticker: str, canonical_id_value: str
) -> list[SectionPeriodText]:
    """Oldest-first, one entry per stored period — the whole-section-grain
    analog of ``item_diff.diff_ticker_concept``'s period grouping."""
    sections = store.section_timeline(
        conn, ticker, canonical_id=canonical_id_value, missing_ok=True
    )
    if not sections:
        return []
    periods: list[tuple[tuple[int, str], list[FilingSection]]] = []
    for sec in sections:
        key = (sec.fiscal_year or 0, sec.fiscal_period.value)
        if periods and periods[-1][0] == key:
            periods[-1][1].append(sec)
        else:
            periods.append((key, [sec]))
    out: list[SectionPeriodText] = []
    for (fy, fp), secs in periods:
        out.append(
            SectionPeriodText(
                ticker=ticker.strip().upper(),
                canonical_id=canonical_id_value,
                fiscal_year=fy or None,
                fiscal_period=fp,
                text="\n\n".join(s.text for s in secs),
                source_doc_id=secs[0].doc_id,
                source_ref=secs[0].source_ref,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Consecutive-pair similarity
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SimilarityPair:
    ticker: str
    canonical_id: str
    fiscal_year: int | None
    fiscal_period: str
    prior_fiscal_year: int | None
    prior_fiscal_period: str
    similarity: float
    change_magnitude: float  # 1 - similarity; the primary raw axis
    current_text: str
    prior_text: str
    source_doc_id: int | None
    source_ref: str | None


def compute_similarity_pairs(periods: list[SectionPeriodText]) -> list[SimilarityPair]:
    """Pure — no DB access. One entry per consecutive stored-period pair."""
    pairs: list[SimilarityPair] = []
    for prior, current in pairwise(periods):
        sim = _similarity(prior.text, current.text)
        pairs.append(
            SimilarityPair(
                ticker=current.ticker,
                canonical_id=current.canonical_id,
                fiscal_year=current.fiscal_year,
                fiscal_period=current.fiscal_period,
                prior_fiscal_year=prior.fiscal_year,
                prior_fiscal_period=prior.fiscal_period,
                similarity=sim,
                change_magnitude=round(1.0 - sim, 4),
                current_text=current.text,
                prior_text=prior.text,
                source_doc_id=current.source_doc_id,
                source_ref=current.source_ref,
            )
        )
    return pairs


def _novel_excerpt(prior_text: str, current_text: str, *, n: int = 280) -> str:
    """A verbatim sentence from ``current_text`` with the LEAST token overlap
    against ``prior_text`` — a real, quotable "here is what's new" receipt,
    never a paraphrase. Falls back to a plain truncated excerpt when no
    sentence boundary is found (the section is one long unstructured blob) or
    the text is too short to have a genuinely novel sentence."""
    prior_tokens = frozenset(re.findall(r"[a-z0-9]+", prior_text.lower()))
    sentences = [s.strip() for s in _SENTENCE_RX.split(current_text) if len(s.strip()) >= 40]
    best: tuple[float, str] | None = None
    for sentence in sentences[:_MAX_SENTENCES_SCANNED]:
        sentence_tokens = frozenset(re.findall(r"[a-z0-9]+", sentence.lower()))
        if not sentence_tokens:
            continue
        overlap = len(sentence_tokens & prior_tokens) / len(sentence_tokens)
        if best is None or overlap < best[0]:
            best = (overlap, sentence)
    if best is not None:
        return _quote(best[1], n)
    return _quote(current_text, n)


# ---------------------------------------------------------------------------
# Cross-sectional corpus (whole tracked book)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SimilarityBucket:
    canonical_id: str
    fiscal_year: int | None
    fiscal_period: str


@dataclass(slots=True)
class SimilarityCorpus:
    by_bucket: dict[SimilarityBucket, list[SimilarityPair]] = field(
        default_factory=dict[SimilarityBucket, list["SimilarityPair"]]
    )
    tickers_covered: list[str] = field(default_factory=list[str])


def _tracked_tickers(conn: sqlite3.Connection) -> list[str]:
    """Every ticker with stored filing_sections — mirrors
    ``cross_sectional_detrend.build_cross_sectional_corpus``'s default scope
    (every ticker present in the substrate table, not just the held
    portfolio) for the same statistical-power reason."""
    filing_sections = selected_filing_sections_relation(conn)
    rows = conn.execute(f"SELECT DISTINCT ticker FROM {filing_sections} ORDER BY ticker").fetchall()
    return [str(r[0]).upper() for r in rows]


def build_similarity_corpus(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    canonical_ids: tuple[str, ...] = ("risk_factors", "mdna"),
) -> SimilarityCorpus:
    """Sweep every tracked ticker's consecutive-period pairs ONCE per run —
    build ONCE, reuse for every per-ticker score lookup, mirroring
    ``cross_sectional_detrend.build_cross_sectional_corpus`` /
    ``metric_lifecycle.build_standard_transition_corpus``."""
    scope = tickers if tickers is not None else _tracked_tickers(conn)
    by_bucket: dict[SimilarityBucket, list[SimilarityPair]] = {}
    covered: list[str] = []
    for ticker in scope:
        found_any = False
        for cid in canonical_ids:
            periods = load_concatenated_section_periods(conn, ticker, cid)
            if len(periods) < 2:
                continue
            found_any = True
            for pair in compute_similarity_pairs(periods):
                bucket = SimilarityBucket(cid, pair.fiscal_year, pair.fiscal_period)
                by_bucket.setdefault(bucket, []).append(pair)
        if found_any:
            covered.append(ticker)
    log.info(
        {
            "event": "similarity_corpus_built",
            "tickers_covered": len(covered),
            "buckets": len(by_bucket),
        }
    )
    return SimilarityCorpus(by_bucket=by_bucket, tickers_covered=covered)


def _percentile_rank(value: float, population: list[float]) -> float | None:
    """Mid-rank percentile in [0, 1] — duplicated from
    ``cross_sectional_detrend._percentile_rank`` (see module docstring)."""
    if not population:
        return None
    less = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    return (less + 0.5 * equal) / len(population)


# ---------------------------------------------------------------------------
# Comparable-set (peer-group) percentile
# ---------------------------------------------------------------------------


def _open_comparable_set_members(conn: sqlite3.Connection, set_id: str) -> set[str]:
    """Currently-open members of a frozen comparable set (any membership
    reason, full or context-only — a text-similarity peer comparison needs no
    financials, so the full/context-only distinction that matters for
    ``comp_set_metrics`` is irrelevant here)."""
    try:
        rows = conn.execute(
            "SELECT member_ticker FROM comparable_set_members "
            "WHERE comparable_set_id = ? AND valid_to IS NULL",
            (set_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(r[0]).upper() for r in rows}


@dataclass(slots=True)
class SimilarityScore:
    """One ticker's detrended whole-section change score for one period."""

    pair: SimilarityPair
    bucket: SimilarityBucket
    book_percentile: float | None
    book_peer_count: int
    peer_percentile: float | None
    peer_count: int
    comparable_set_id: str | None
    peer_reason: str | None


def score_pair(
    conn: sqlite3.Connection, corpus: SimilarityCorpus, pair: SimilarityPair
) -> SimilarityScore:
    bucket = SimilarityBucket(pair.canonical_id, pair.fiscal_year, pair.fiscal_period)
    entries = corpus.by_bucket.get(bucket, [])
    book_peers = [p.change_magnitude for p in entries if p.ticker != pair.ticker]
    book_pct = (
        _percentile_rank(pair.change_magnitude, book_peers)
        if len(book_peers) >= MIN_CROSS_SECTION_PEERS
        else None
    )

    set_id = comparable_set_id(pair.ticker)
    members = _open_comparable_set_members(conn, set_id)
    peer_pct: float | None = None
    peer_reason: str | None = None
    peer_scores: list[float] = []
    if not members:
        peer_reason = "no_comparable_set"
    else:
        peer_scores = [p.change_magnitude for p in entries if p.ticker in members]
        if len(peer_scores) < MIN_CROSS_SECTION_PEERS:
            peer_reason = (
                f"insufficient_comparable_set_peers_in_bucket:"
                f"{len(peer_scores)}<{MIN_CROSS_SECTION_PEERS}"
            )
        else:
            peer_pct = _percentile_rank(pair.change_magnitude, peer_scores)

    return SimilarityScore(
        pair=pair,
        bucket=bucket,
        book_percentile=book_pct,
        book_peer_count=len(book_peers),
        peer_percentile=peer_pct,
        peer_count=len(peer_scores),
        comparable_set_id=set_id if members else None,
        peer_reason=peer_reason,
    )


def score_all(
    conn: sqlite3.Connection, corpus: SimilarityCorpus, *, tickers: list[str] | None = None
) -> list[SimilarityScore]:
    scope = {t.strip().upper() for t in tickers} if tickers else None
    scores: list[SimilarityScore] = []
    for _bucket, pairs in corpus.by_bucket.items():
        for pair in pairs:
            if scope is not None and pair.ticker not in scope:
                continue
            scores.append(score_pair(conn, corpus, pair))
    return scores


# ---------------------------------------------------------------------------
# disclosure_events write contract
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SimilarityEvent:
    ticker: str
    canonical_id: str
    fiscal_year: int | None
    fiscal_period: str
    prior_fiscal_year: int | None
    prior_fiscal_period: str
    source_ref: str | None
    source_doc_id: int | None
    evidence_quote: str
    prior_excerpt: str
    current_excerpt: str
    book_percentile: float
    peer_percentile: float | None
    interpretation_md: str


EVENT_TYPE = "section_similarity_shift"


def score_to_event(score: SimilarityScore) -> SimilarityEvent | None:
    """``None`` when the MANDATORY book-level detrending has no computable
    percentile (too few peers this run) — never persist an un-detrended
    score (scoping doc: skipping this "isn't a minor omission -- it inverts
    the finding")."""
    if score.book_percentile is None:
        return None
    pair = score.pair
    interpretation = (
        f"Raw similarity: {pair.similarity:.3f} (change magnitude {pair.change_magnitude:.3f}). "
        f"Book percentile: {score.book_percentile:.3f} ({score.book_peer_count} peers, "
        f"whole tracked book). "
        + (
            f"Peer-group percentile: {score.peer_percentile:.3f} "
            f"({score.peer_count} peers, comparable set {score.comparable_set_id})."
            if score.peer_percentile is not None
            else f"Peer-group percentile: unavailable ({score.peer_reason})."
        )
        + " Drift signal (Cohen/Malloy/Nguyen 2020) -- relevant over subsequent weeks/months, "
        "never a day-of alert; no abnormal return was found around the filing date itself."
    )
    return SimilarityEvent(
        ticker=pair.ticker,
        canonical_id=pair.canonical_id,
        fiscal_year=pair.fiscal_year,
        fiscal_period=pair.fiscal_period,
        prior_fiscal_year=pair.prior_fiscal_year,
        prior_fiscal_period=pair.prior_fiscal_period,
        source_ref=pair.source_ref,
        source_doc_id=pair.source_doc_id,
        evidence_quote=_novel_excerpt(pair.prior_text, pair.current_text),
        prior_excerpt=_quote(pair.prior_text, 500),
        current_excerpt=_quote(pair.current_text, 500),
        book_percentile=score.book_percentile,
        peer_percentile=score.peer_percentile,
        interpretation_md=interpretation,
    )


def _now_naive_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_events_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (EVENTS_TABLE,)
    ).fetchone()
    if row is None:
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0203) first"
        )


def write_similarity_events(conn: sqlite3.Connection, events: list[SimilarityEvent]) -> int:
    """Idempotent upsert on ``disclosure_events``'s unique key. ``subject`` is
    always set equal to ``canonical_id`` — this event type has no finer
    subject grain than the whole section, so the two columns are
    intentionally redundant for this event type (harmless: the shared-table
    design already accepts one column meaning nothing for some detectors)."""
    if not events:
        return 0
    _require_events_table(conn)
    now = _now_naive_utc()
    written = 0
    for e in events:
        existing = conn.execute(
            f"""
            SELECT id FROM {EVENTS_TABLE}
            WHERE ticker = ? AND event_type = ? AND fiscal_year IS ? AND fiscal_period IS ?
              AND canonical_id = ? AND subject = ? AND detector_version = ?
            LIMIT 1
            """,
            (
                e.ticker,
                EVENT_TYPE,
                e.fiscal_year,
                e.fiscal_period,
                e.canonical_id,
                e.canonical_id,
                DETECTOR_VERSION,
            ),
        ).fetchone()
        if existing is None:
            conn.execute(
                f"""
                INSERT INTO {EVENTS_TABLE}
                    (ticker, event_type, fiscal_year, fiscal_period, prior_fiscal_year,
                     prior_fiscal_period, source_ref, source_doc_id, canonical_id, subject,
                     subject_label, prior_excerpt, current_excerpt, evidence_quote,
                     materiality, verdict, interpretation_md, confidence, detector_version,
                     status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e.ticker,  # ticker
                    EVENT_TYPE,  # event_type
                    e.fiscal_year,  # fiscal_year
                    e.fiscal_period,  # fiscal_period
                    e.prior_fiscal_year,  # prior_fiscal_year
                    e.prior_fiscal_period,  # prior_fiscal_period
                    e.source_ref,  # source_ref
                    e.source_doc_id,  # source_doc_id
                    e.canonical_id,  # canonical_id
                    e.canonical_id,  # subject (== canonical_id; see module docstring)
                    e.canonical_id,  # subject_label
                    e.prior_excerpt,  # prior_excerpt
                    e.current_excerpt,  # current_excerpt
                    e.evidence_quote,  # evidence_quote
                    e.book_percentile,  # materiality == book-level percentile
                    "unclassified",  # verdict
                    e.interpretation_md,  # interpretation_md
                    e.peer_percentile,  # confidence == peer-group percentile (see docstring)
                    DETECTOR_VERSION,  # detector_version
                    "new",  # status
                    now,  # created_at
                ),
            )
        else:
            conn.execute(
                f"""
                UPDATE {EVENTS_TABLE}
                SET prior_excerpt=?, current_excerpt=?, evidence_quote=?,
                    materiality=?, confidence=?, interpretation_md=?
                WHERE id=?
                """,
                (
                    e.prior_excerpt,
                    e.current_excerpt,
                    e.evidence_quote,
                    e.book_percentile,
                    e.peer_percentile,
                    e.interpretation_md,
                    int(existing[0]),
                ),
            )
        written += 1
    return written
