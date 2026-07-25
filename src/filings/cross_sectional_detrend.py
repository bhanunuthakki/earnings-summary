"""P2 cross-sectional detrending (``docs/design/disclosure_change_build_stack.md`` §P2).

Why (do not re-derive): median 10-K length roughly doubled 1996-2013,
mechanically, driven mostly by three mandated topics rather than firm
behavior (Dyer, Lang & Stice-Lawrence, *JAE* 64, 2017). An un-detrended
similarity/length measure manufactures a spurious "everyone changes more
every year" trend. Cohen, Malloy & Nguyen (*JF* 2020, "Lazy Prices") handle
this by ranking each firm's raw score against the **same-period
cross-sectional distribution across the tracked book** — never a fixed
historical baseline — and this module does the same, at item-change-count
and section-length-delta granularity (``docs/design/
disclosure_change_signals.md`` §1.3).

Precedent this mirrors one level down: ``filings.metric_lifecycle
.build_standard_transition_corpus`` (P1) sweeps every cached ticker's Stage 0
output ONCE per run to build a cross-ticker histogram, then reuses it for
every per-ticker lookup — an O(n) corpus build instead of an O(n^2)
per-ticker rescan. ``build_cross_sectional_corpus`` here is the same pattern,
built from ``disclosure_events`` (P0's item-level events) and
``filing_sections`` (stored section lengths) instead of cached XBRL facts.

**Where the percentile is written, and — just as important — where it is
NOT.** ``disclosure_events.materiality`` (migration 0200) already carries a
detector-specific meaning for two of the three item-level event types:

* ``item_reworded`` — P0 (``filings.item_diff._reworded_event``) sets it to
  ``1 - token_jaccard_similarity``, a PER-ITEM raw magnitude. Its own
  docstring anticipates P2 turning this into something cross-sectionally
  comparable, but this module deliberately does **not** overwrite it in
  place: ``item_diff.write_events`` UPDATEs ``materiality`` unconditionally
  on every re-run of the P0 detector (even resetting it to ``NULL`` for
  ``item_added``/``item_removed``), so anything this module wrote there would
  be silently clobbered the next time P0 re-runs for that ticker — a real
  ownership collision, not a hypothetical one. Overwriting a column another
  detector actively re-asserts is exactly the "overload it silently" failure
  the build-stack doc warns against.
* ``item_added`` / ``item_removed`` — P0 leaves ``materiality`` ``NULL`` for
  these (there is no "within-item" magnitude for something that has no prior
  or no current body to compare against). This module fills that gap: for
  these two event types only, ``materiality`` becomes the ticker-period
  cross-sectional percentile of item-change volume — a genuinely new,
  previously-absent number, not an overload of an existing one.

Because of the P0 rewrite behavior above, **this module must be re-run after
every P0 (re)run that touches the same tickers/periods** to stay current —
the same enrichment-layer relationship ``filings.metric_triage``'s Stage 2 LLM
triage already has with ``filings.metric_lifecycle``'s Stage 0/1 output. This
is why it is a separate CLI step (``execution/detrend_disclosure_events.py``),
never fused into ``execution/detect_disclosure_changes.py``.

**Length deltas are exposed but never blended into the persisted score.**
The build-stack doc is explicit that "total length ... [is] the weakest,
noisiest version of th[e] measure and actively misleading alone" — so
``length_delta_percentile`` is computed and returned for transparency/
diagnostics, but ``combined_percentile`` (the value actually persisted) is
always the item-change-count percentile, the literature-preferred primary
axis (Lyle, Riedl & Siano, *TAR* 98(6), 2023; Campbell et al., *RAST* 19,
2014).

**A known, inherited asymmetry in event-period bucketing (P0's, not
introduced here).** ``filings.item_diff`` stamps ``item_added``/
``item_reworded`` events with the CURRENT (new) filing's fiscal_year/period,
but stamps ``item_removed`` events with the item's OWN (i.e. the PRIOR/last-
seen) fiscal_year/period — ``_removed_event`` sets both ``fiscal_year`` and
``prior_fiscal_year`` to the same value, the period the removed item was last
present in. This module buckets every event type by whatever fiscal_year/
fiscal_period is already stored on the row (no attempt to "correct" P0's
convention), so a ticker's bucket score combines "what was ADDED/REWORDED in
period Y" with "what was last seen IN period Y and did not survive to the
next filing" — a coherent reading of "how much of period Y's disclosure
changed," just not the naive "everything the CURRENT filing changed"
framing a reader might expect. Documented here so it is not a silent
surprise; applied identically to every ticker, so cross-sectional comparison
stays apples-to-apples.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from filings.models import HardStopError
from filings.store import table_exists

log = logging.getLogger(__name__)

DETECTOR_VERSION = "cross_sectional_detrend_v1"
EVENTS_TABLE = "disclosure_events"
SECTIONS_TABLE = "filing_sections"

#: Item-level event types this module detrends. Mirrors ``filings.item_diff``'s
#: vocabulary structurally (string literals, not an import) so any FUTURE
#: item-level detector emitting these same event types is picked up
#: automatically without this module importing item_diff at all.
ITEM_EVENT_TYPES: tuple[str, ...] = ("item_added", "item_removed", "item_reworded")

#: Event types this module is willing to WRITE a detrended materiality onto.
#: Deliberately excludes item_reworded — see module docstring.
_WRITABLE_EVENT_TYPES: tuple[str, ...] = ("item_added", "item_removed")

#: Below this many OTHER tickers in the same-period cross-section, a
#: percentile is statistically meaningless (with 1 peer, either ticker IS the
#: 0th or 100th percentile by construction) — degrade honestly rather than
#: report a fabricated extreme.
MIN_CROSS_SECTION_PEERS = 3

_PERIOD_RANK: dict[str, int] = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}


@dataclass(slots=True, frozen=True)
class PeriodBucket:
    """The cross-sectional comparison key: same concept, same fiscal period."""

    canonical_id: str | None
    fiscal_year: int | None
    fiscal_period: str | None


@dataclass(slots=True)
class TickerPeriodMagnitude:
    """Raw (undetrended) change-magnitude inputs for one (ticker, bucket)."""

    ticker: str
    bucket: PeriodBucket
    n_added: int = 0
    n_removed: int = 0
    n_reworded: int = 0
    #: Total stored char_len across filing_sections rows for this bucket.
    length_chars: int | None = None
    #: Same, for the immediately preceding STORED period in this ticker's
    #: (canonical_id) timeline — not necessarily the prior calendar quarter,
    #: since a gap in stored filings skips straight to whatever came before.
    prior_length_chars: int | None = None

    @property
    def item_change_count(self) -> int:
        return self.n_added + self.n_removed + self.n_reworded

    @property
    def length_delta_chars(self) -> int | None:
        if self.length_chars is None or self.prior_length_chars is None:
            return None
        return self.length_chars - self.prior_length_chars


@dataclass(slots=True)
class CrossSectionalCorpus:
    """Every (ticker, bucket) magnitude in the tracked book — built ONCE per
    run (``build_cross_sectional_corpus``) and reused for every per-ticker
    score lookup, mirroring ``metric_lifecycle.StandardTransitionCorpus``.
    """

    by_bucket: dict[PeriodBucket, list[TickerPeriodMagnitude]] = field(
        default_factory=dict[PeriodBucket, list["TickerPeriodMagnitude"]]
    )
    tickers_covered: list[str] = field(default_factory=list[str])

    def peers(self, bucket: PeriodBucket, *, exclude_ticker: str) -> list[TickerPeriodMagnitude]:
        """Every OTHER tracked ticker's magnitude in this same bucket."""
        exclude = exclude_ticker.strip().upper()
        return [m for m in self.by_bucket.get(bucket, []) if m.ticker != exclude]


def _require_events_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, EVENTS_TABLE):
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0200) "
            "before computing cross-sectional detrending"
        )


def _fetch_item_event_counts(
    conn: sqlite3.Connection, tickers: Sequence[str] | None
) -> dict[str, dict[PeriodBucket, dict[str, int]]]:
    """ticker -> bucket -> {event_type: count} for item-level events."""
    _require_events_table(conn)
    placeholders = ",".join("?" * len(ITEM_EVENT_TYPES))
    params: list[object] = list(ITEM_EVENT_TYPES)
    ticker_clause = ""
    if tickers:
        upper = [t.strip().upper() for t in tickers]
        ticker_clause = f" AND ticker IN ({','.join('?' * len(upper))})"
        params.extend(upper)
    rows = conn.execute(
        f"""
        SELECT ticker, canonical_id, fiscal_year, fiscal_period, event_type, COUNT(*)
        FROM {EVENTS_TABLE}
        WHERE event_type IN ({placeholders}){ticker_clause}
        GROUP BY ticker, canonical_id, fiscal_year, fiscal_period, event_type
        """,
        tuple(params),
    ).fetchall()
    out: dict[str, dict[PeriodBucket, dict[str, int]]] = {}
    for ticker, canonical_id, fy, fp, event_type, n in rows:
        bucket = PeriodBucket(canonical_id=canonical_id, fiscal_year=fy, fiscal_period=fp)
        out.setdefault(str(ticker).upper(), {}).setdefault(bucket, {})[str(event_type)] = int(n)
    return out


def _fetch_section_length_deltas(
    conn: sqlite3.Connection, tickers: Sequence[str] | None
) -> dict[str, dict[PeriodBucket, tuple[int, int | None]]]:
    """ticker -> bucket -> (length_chars, prior_length_chars-or-None).

    "Prior" is the immediately preceding STORED period for that (ticker,
    canonical_id) pair, ordered by (fiscal_year, period_rank) — same
    consecutive-pairs convention as ``filings.store.section_timeline`` /
    ``filings.item_diff.diff_ticker_concept``.
    """
    if not table_exists(conn, SECTIONS_TABLE):
        raise HardStopError(
            f"{SECTIONS_TABLE} missing — run `alembic upgrade head` (migration 0198) "
            "before computing cross-sectional detrending"
        )
    params: list[object] = []
    ticker_clause = ""
    if tickers:
        upper = [t.strip().upper() for t in tickers]
        ticker_clause = f"AND ticker IN ({','.join('?' * len(upper))}) "
        params.extend(upper)
    rows = conn.execute(
        f"""
        SELECT ticker, canonical_id, fiscal_year, fiscal_period, SUM(char_len)
        FROM {SECTIONS_TABLE}
        WHERE canonical_id IS NOT NULL {ticker_clause}
        GROUP BY ticker, canonical_id, fiscal_year, fiscal_period
        """,
        tuple(params),
    ).fetchall()

    # (ticker, canonical_id) -> [(fy_sort, rank, fy, fp, chars)], ordered later.
    by_series: dict[tuple[str, str], list[tuple[int, int, int | None, str | None, int]]] = {}
    for ticker, canonical_id, fy, fp, chars in rows:
        key = (str(ticker).upper(), str(canonical_id))
        rank = _PERIOD_RANK.get(str(fp) if fp is not None else "", 0)
        by_series.setdefault(key, []).append(
            (int(fy) if fy is not None else 0, rank, fy, fp, int(chars))
        )

    out: dict[str, dict[PeriodBucket, tuple[int, int | None]]] = {}
    for (ticker, canonical_id), entries in by_series.items():
        entries.sort(key=lambda e: (e[0], e[1]))
        for idx, (_fy_sort, _rank, fy, fp, chars) in enumerate(entries):
            bucket = PeriodBucket(canonical_id=canonical_id, fiscal_year=fy, fiscal_period=fp)
            prior_chars = entries[idx - 1][4] if idx > 0 else None
            out.setdefault(ticker, {})[bucket] = (chars, prior_chars)
    return out


def build_cross_sectional_corpus(
    conn: sqlite3.Connection, *, tickers: Sequence[str] | None = None
) -> CrossSectionalCorpus:
    """Sweep ``disclosure_events`` + ``filing_sections`` ONCE, across every
    tracked ticker (default: every ticker present in either table — the
    FULL tracked book, not just a subset, for the same reason
    ``metric_lifecycle.build_standard_transition_corpus`` scans the full
    cached book: cross-sectional statistical power comes from MORE
    comparison points). Cache the result and reuse across every per-ticker
    ``score_ticker_bucket`` call in a run.
    """
    counts = _fetch_item_event_counts(conn, tickers)
    lengths = _fetch_section_length_deltas(conn, tickers)
    all_tickers = sorted(set(counts) | set(lengths))

    by_bucket: dict[PeriodBucket, list[TickerPeriodMagnitude]] = {}
    for ticker in all_tickers:
        buckets = set(counts.get(ticker, {})) | set(lengths.get(ticker, {}))
        for bucket in buckets:
            type_counts = counts.get(ticker, {}).get(bucket, {})
            chars, prior_chars = lengths.get(ticker, {}).get(bucket, (None, None))
            mag = TickerPeriodMagnitude(
                ticker=ticker,
                bucket=bucket,
                n_added=type_counts.get("item_added", 0),
                n_removed=type_counts.get("item_removed", 0),
                n_reworded=type_counts.get("item_reworded", 0),
                length_chars=chars,
                prior_length_chars=prior_chars,
            )
            by_bucket.setdefault(bucket, []).append(mag)

    log.info(
        {
            "event": "cross_sectional_corpus_built",
            "tickers_covered": len(all_tickers),
            "buckets": len(by_bucket),
        }
    )
    return CrossSectionalCorpus(by_bucket=by_bucket, tickers_covered=all_tickers)


def _percentile_rank(value: float, population: Sequence[float]) -> float | None:
    """Mid-rank percentile in ``[0, 1]``; ``None`` on an empty population.

    Mid-rank (ties split the difference) rather than strict ``<=`` so a
    population with many ties at the same value doesn't push everyone
    tied at that value to the same boundary percentile.
    """
    if not population:
        return None
    less = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    return (less + 0.5 * equal) / len(population)


@dataclass(slots=True)
class CrossSectionalScore:
    """One ticker's detrended change-magnitude for one period bucket."""

    ticker: str
    bucket: PeriodBucket
    raw_item_change_count: int
    raw_length_delta_chars: int | None
    peer_count: int
    #: Percentile [0, 1] of item-change VOLUME against same-period peers.
    #: This is what gets persisted (see module docstring).
    item_change_percentile: float | None
    #: Percentile [0, 1] of |length delta| against same-period peers.
    #: Diagnostic only — the literature's "weakest, noisiest" axis; never
    #: blended into ``combined_percentile``.
    length_delta_percentile: float | None
    #: The persisted value. Always equal to ``item_change_percentile``.
    combined_percentile: float | None
    #: Whether the cross-sectional gate had enough peers to score at all.
    gate_ran: bool
    #: Non-None whenever ``gate_ran`` is False or a score could not be
    #: computed — absence-is-never-silent.
    reason: str | None
    detector_version: str = DETECTOR_VERSION

    @property
    def quintile(self) -> int | None:
        """Lazy-Prices-style 1-5 bucket, derived from ``combined_percentile``."""
        if self.combined_percentile is None:
            return None
        return min(5, max(1, math.ceil(self.combined_percentile * 5) or 1))


def score_ticker_bucket(
    corpus: CrossSectionalCorpus, ticker: str, bucket: PeriodBucket
) -> CrossSectionalScore:
    """Detrend one (ticker, bucket) pair against its cross-sectional peers."""
    ticker = ticker.strip().upper()
    mine = next((m for m in corpus.by_bucket.get(bucket, []) if m.ticker == ticker), None)
    if mine is None:
        return CrossSectionalScore(
            ticker=ticker,
            bucket=bucket,
            raw_item_change_count=0,
            raw_length_delta_chars=None,
            peer_count=0,
            item_change_percentile=None,
            length_delta_percentile=None,
            combined_percentile=None,
            gate_ran=False,
            reason="ticker_not_in_bucket",
        )

    peers = corpus.peers(bucket, exclude_ticker=ticker)
    if len(peers) < MIN_CROSS_SECTION_PEERS:
        return CrossSectionalScore(
            ticker=ticker,
            bucket=bucket,
            raw_item_change_count=mine.item_change_count,
            raw_length_delta_chars=mine.length_delta_chars,
            peer_count=len(peers),
            item_change_percentile=None,
            length_delta_percentile=None,
            combined_percentile=None,
            gate_ran=False,
            reason=f"insufficient_cross_section_peers:{len(peers)}<{MIN_CROSS_SECTION_PEERS}",
        )

    item_population = [float(p.item_change_count) for p in peers]
    item_pct = _percentile_rank(float(mine.item_change_count), item_population)

    length_population = [
        abs(float(p.length_delta_chars)) for p in peers if p.length_delta_chars is not None
    ]
    length_pct = (
        _percentile_rank(abs(float(mine.length_delta_chars)), length_population)
        if mine.length_delta_chars is not None and length_population
        else None
    )

    return CrossSectionalScore(
        ticker=ticker,
        bucket=bucket,
        raw_item_change_count=mine.item_change_count,
        raw_length_delta_chars=mine.length_delta_chars,
        peer_count=len(peers),
        item_change_percentile=item_pct,
        length_delta_percentile=length_pct,
        combined_percentile=item_pct,
        gate_ran=True,
        reason=None,
    )


def score_all(
    corpus: CrossSectionalCorpus, *, tickers: Sequence[str] | None = None
) -> list[CrossSectionalScore]:
    """Score every (ticker, bucket) pair in the corpus (optionally scoped to
    ``tickers``). Peer lists are already bucket-local, so this is O(total
    rows), not O(n^2)."""
    scope = {t.strip().upper() for t in tickers} if tickers else None
    scores: list[CrossSectionalScore] = []
    for bucket, magnitudes in corpus.by_bucket.items():
        for mag in magnitudes:
            if scope is not None and mag.ticker not in scope:
                continue
            scores.append(score_ticker_bucket(corpus, mag.ticker, bucket))
    return scores


def write_detrended_materiality(
    conn: sqlite3.Connection, scores: Sequence[CrossSectionalScore]
) -> int:
    """UPDATE-only enrichment of ``disclosure_events.materiality`` for
    ``item_added``/``item_removed`` rows in each scored (ticker, bucket) —
    NEVER ``item_reworded`` (see module docstring). Skips scores with no
    computed percentile (``gate_ran=False``) rather than writing a
    fabricated 0.0 — the absent value stays absent, honestly.

    Idempotent: re-running with the same corpus produces the same UPDATE,
    which is a no-op in content terms. Must be re-run after any P0 (re)run
    that touches these rows — see module docstring.
    """
    _require_events_table(conn)
    placeholders = ",".join("?" * len(_WRITABLE_EVENT_TYPES))
    written = 0
    for s in scores:
        if s.combined_percentile is None:
            continue
        cur = conn.execute(
            f"""
            UPDATE {EVENTS_TABLE}
            SET materiality = ?
            WHERE ticker = ? AND canonical_id IS ? AND fiscal_year IS ? AND fiscal_period IS ?
              AND event_type IN ({placeholders})
            """,
            (
                s.combined_percentile,
                s.ticker,
                s.bucket.canonical_id,
                s.bucket.fiscal_year,
                s.bucket.fiscal_period,
                *_WRITABLE_EVENT_TYPES,
            ),
        )
        written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return written
