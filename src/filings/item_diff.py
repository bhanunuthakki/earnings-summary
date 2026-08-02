"""Align ``filing_section_items`` across consecutive periods and classify change.

The core P0 engine (``docs/design/disclosure_change_build_stack.md`` §P0):
deterministic item alignment, zero LLM. For one (ticker, canonical_id) timeline
it walks consecutive period pairs — via ``store.section_timeline``, which
returns OLDEST FIRST, never re-sorted here — and classifies every item as:

  * identical ``body_sha256`` in both periods  -> unchanged, emit nothing
  * same ``match_key``, bodies differ          -> ``item_reworded``
  * unmatched by hash or match_key, but a
    content-similarity match exists            -> ``item_reworded``
    (the fallback that stops a reworded HEADING from producing a spurious
    add-plus-remove — see ``SIMILARITY_THRESHOLD``)
  * unmatched in the prior period               -> ``item_added``
  * unmatched in the current period              -> ``item_removed``

Removals are first-class: they get exactly the same treatment as additions,
never dropped as a side effect of only scanning forward. ``verdict`` on every
emitted event stays ``"unclassified"`` — direction is not implied by event
type (migration 0203 docstring; concealment vs. maturity is a judgment call
for a later, LLM-backed phase).
"""

from __future__ import annotations

import itertools
import logging
import re
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from filings import section_items, store
from filings.models import FilingForm, FiscalPeriod, HardStopError
from filings.section_items import SectionItem
from filings.specificity import looks_tabular

log = logging.getLogger(__name__)

EVENTS_TABLE = "disclosure_events"
DETECTOR_VERSION = "item_diff_v1"

#: Below this token-set Jaccard on normalized bodies, two unmatched items are
#: treated as an independent add + an independent remove, not one reworded
#: item. Calibration reasoning: a heading-only edit (the failure mode this
#: fallback exists to catch) leaves the body's word set almost entirely
#: intact — realistically well above 0.7 overlap. Two genuinely DIFFERENT risk
#: factors sharing only generic disclosure boilerplate ("could adversely
#: affect our business, financial condition and results of operations")
#: measure well under 0.4 in spot checks, because that shared boilerplate is a
#: small fraction of either body's total token set. 0.55 sits in the gap
#: between those two populations, closer to the boilerplate-overlap ceiling
#: than the reword floor, so it favors NOT collapsing two truly independent
#: items over missing a genuine reword — the cheaper mistake, because a missed
#: reword still surfaces correctly as its own add+remove pair.
SIMILARITY_THRESHOLD = 0.55

QUOTE_LEN = 280
EXCERPT_LEN = 500

_TOKEN_RX = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class DisclosureEvent:
    """One row bound for ``disclosure_events`` — see migration 0203 for schema."""

    ticker: str
    event_type: str
    form: FilingForm | None
    fiscal_year: int | None
    fiscal_period: FiscalPeriod | None
    prior_fiscal_year: int | None
    prior_fiscal_period: FiscalPeriod | None
    source_ref: str | None
    source_doc_id: int | None
    canonical_id: str | None
    subject: str
    subject_label: str | None
    prior_excerpt: str | None
    current_excerpt: str | None
    evidence_quote: str
    materiality: float | None = None
    verdict: str = "unclassified"
    detector_version: str = DETECTOR_VERSION


def _tokens(body: str) -> frozenset[str]:
    return frozenset(_TOKEN_RX.findall(body.lower()))


def _similarity(a: str, b: str) -> float:
    """Token-set Jaccard — cheap, symmetric, and robust to word-order shuffles
    that a straight ``difflib`` ratio would penalize unnecessarily."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _quote(body: str, n: int = QUOTE_LEN) -> str:
    """Verbatim excerpt — collapsed whitespace, but every character is from
    the source body. Never paraphrased; per the repo rule, no event without
    a real quote to back it."""
    return " ".join(body.split())[:n].strip()


def _excerpt(body: str, n: int = EXCERPT_LEN) -> str:
    return " ".join(body.split())[:n].strip()


def _is_tabular_item(body: str) -> bool:
    """Whether this item body is extracted TABLE content rather than narrative.

    This detector diffs the NARRATIVE items of a filing section. A financial
    table that survived extraction as flattened text ("(in millions)20242023
    Interest expense$(1,295)$(897)44.3 %") is out of its subject matter: the
    numbers it carries are the facts/XBRL pipeline's job, and diffing them as
    prose produces an event on every period simply because the figures moved.

    Suppressing at emit is the top-of-funnel fix. Left to run, these items
    dominate everything downstream — they were ~36% of all events, they defeat
    body-level dedupe (every quarter's numbers differ, so no two are alike),
    and the P3 specificity proxy actively PROMOTES them because it reads number
    density as firm-specificity (``specificity.looks_tabular``). Dropping them
    here is cheaper and more honest than paying an LLM to reject each one.
    """
    return looks_tabular(body)


def _removed_event(item: SectionItem) -> DisclosureEvent | None:
    if _is_tabular_item(item.body):
        return None
    quote = _quote(item.body)
    if not quote:
        return None
    return DisclosureEvent(
        ticker=item.ticker,
        event_type="item_removed",
        form=item.form,
        fiscal_year=item.fiscal_year,
        fiscal_period=item.fiscal_period,
        prior_fiscal_year=item.fiscal_year,
        prior_fiscal_period=item.fiscal_period,
        source_ref=item.source_ref,
        source_doc_id=item.doc_id,
        canonical_id=item.canonical_id,
        subject=item.match_key,
        subject_label=item.heading,
        prior_excerpt=_excerpt(item.body),
        current_excerpt=None,
        evidence_quote=quote,
    )


def _added_event(item: SectionItem) -> DisclosureEvent | None:
    if _is_tabular_item(item.body):
        return None
    quote = _quote(item.body)
    if not quote:
        return None
    return DisclosureEvent(
        ticker=item.ticker,
        event_type="item_added",
        form=item.form,
        fiscal_year=item.fiscal_year,
        fiscal_period=item.fiscal_period,
        prior_fiscal_year=None,
        prior_fiscal_period=None,
        source_ref=item.source_ref,
        source_doc_id=item.doc_id,
        canonical_id=item.canonical_id,
        subject=item.match_key,
        subject_label=item.heading,
        prior_excerpt=None,
        current_excerpt=_excerpt(item.body),
        evidence_quote=quote,
    )


def _reworded_event(prior: SectionItem, current: SectionItem) -> DisclosureEvent | None:
    # Gate on the CURRENT body only — the side this event's quote and excerpt
    # are drawn from. An earlier version also suppressed when the PRIOR side was
    # tabular; the one-ticker dry sweep caught that dropping real narrative
    # events whose prior-period counterpart happened to match a table (the
    # aligner can pair a narrative item against a tabular one via the
    # content-similarity fallback). Losing narrative is the expensive error
    # here; a spurious table-to-prose "rewording" is merely noisy, and the P3
    # specificity gate still sees it.
    if _is_tabular_item(current.body):
        return None
    quote = _quote(current.body)
    if not quote:
        return None
    similarity = _similarity(prior.body, current.body)
    return DisclosureEvent(
        ticker=current.ticker,
        event_type="item_reworded",
        form=current.form,
        fiscal_year=current.fiscal_year,
        fiscal_period=current.fiscal_period,
        prior_fiscal_year=prior.fiscal_year,
        prior_fiscal_period=prior.fiscal_period,
        source_ref=current.source_ref,
        source_doc_id=current.doc_id,
        canonical_id=current.canonical_id,
        subject=current.match_key,
        subject_label=current.heading or prior.heading,
        prior_excerpt=_excerpt(prior.body),
        current_excerpt=_excerpt(current.body),
        evidence_quote=quote,
        # Degree of change, not direction — higher means less of the prior
        # body's vocabulary survived. P2 (cross-sectional detrending) is
        # where this becomes comparable across tickers/years.
        materiality=round(1.0 - similarity, 4),
    )


def align_period_pair(
    prior_items: Sequence[SectionItem], current_items: Sequence[SectionItem]
) -> list[DisclosureEvent]:
    """Classify one consecutive period pair. Pure — no DB access.

    Order of passes matters and mirrors the P0 directive exactly:
    1. exact body hash -> unchanged (dropped from further consideration)
    2. same match_key among the rest -> reworded
    3. content-similarity fallback among what's STILL unmatched -> reworded
       (this is what stops a reworded heading from producing add+remove)
    4. whatever remains -> removed (prior) / added (current)
    """
    if not prior_items and not current_items:
        return []

    events: list[DisclosureEvent] = []

    # --- pass 1: exact body hash --------------------------------------------
    prior_by_hash: dict[str, list[SectionItem]] = defaultdict(list)
    for it in prior_items:
        prior_by_hash[it.body_sha256].append(it)
    remaining_current: list[SectionItem] = []
    consumed_prior: set[int] = set()
    for it in current_items:
        bucket = prior_by_hash.get(it.body_sha256)
        if bucket:
            consumed_prior.add(id(bucket.pop(0)))
            continue
        remaining_current.append(it)
    remaining_prior = [it for it in prior_items if id(it) not in consumed_prior]

    # --- pass 2: same match_key, bodies differ ------------------------------
    prior_by_key: dict[str, list[SectionItem]] = defaultdict(list)
    for it in remaining_prior:
        prior_by_key[it.match_key].append(it)
    still_current: list[SectionItem] = []
    consumed_prior_2: set[int] = set()
    for it in remaining_current:
        bucket = prior_by_key.get(it.match_key)
        if bucket:
            match = bucket.pop(0)
            consumed_prior_2.add(id(match))
            event = _reworded_event(match, it)
            if event is not None:
                events.append(event)
            continue
        still_current.append(it)
    still_prior = [it for it in remaining_prior if id(it) not in consumed_prior_2]

    # --- pass 3: content-similarity fallback --------------------------------
    if still_prior and still_current:
        candidates: list[tuple[float, SectionItem, SectionItem]] = []
        for pi in still_prior:
            for ci in still_current:
                score = _similarity(pi.body, ci.body)
                if score >= SIMILARITY_THRESHOLD:
                    candidates.append((score, pi, ci))
        # Highest-score pairs claimed first so one prior item can't be
        # "stolen" by a worse match while its best match goes unpaired.
        candidates.sort(key=lambda c: c[0], reverse=True)
        matched_prior: set[int] = set()
        matched_current: set[int] = set()
        for _score, pi, ci in candidates:
            if id(pi) in matched_prior or id(ci) in matched_current:
                continue
            matched_prior.add(id(pi))
            matched_current.add(id(ci))
            event = _reworded_event(pi, ci)
            if event is not None:
                events.append(event)
        still_prior = [it for it in still_prior if id(it) not in matched_prior]
        still_current = [it for it in still_current if id(it) not in matched_current]

    # --- pass 4: genuine removals / additions -------------------------------
    for it in still_prior:
        event = _removed_event(it)
        if event is not None:
            events.append(event)
    for it in still_current:
        event = _added_event(it)
        if event is not None:
            events.append(event)

    return events


def _period_key(
    section_fiscal_year: int | None, section_fiscal_period: FiscalPeriod
) -> tuple[int, str]:
    return (section_fiscal_year or 0, section_fiscal_period.value)


def diff_ticker_concept(
    conn: sqlite3.Connection,
    ticker: str,
    canonical_id: str,
    *,
    missing_ok: bool = False,
) -> tuple[list[SectionItem], list[DisclosureEvent]]:
    """Full P0 pipeline for one (ticker, canonical_id): split every stored
    period, then diff consecutive pairs.

    Returns ``(items, events)`` so a caller can persist the split items to
    ``filing_section_items`` and the events to ``disclosure_events`` from a
    single pass — splitting is done exactly once per section, never
    recomputed for persistence and again for diffing.

    A ticker with fewer than two periods on file returns ``([], [])`` (or the
    lone period's items with no events) rather than raising: there is nothing
    to align yet, which is a normal state, not a failure.
    """
    sections = store.section_timeline(
        conn, ticker, canonical_id=canonical_id, missing_ok=missing_ok
    )
    if not sections:
        return [], []

    all_items: list[SectionItem] = []
    # Ordered list of (period_key, items-for-that-period); sections arrive
    # oldest-first from section_timeline and are never re-sorted here.
    periods: list[tuple[tuple[int, str], list[SectionItem]]] = []
    for sec in sections:
        if sec.id is None:
            log.warning(
                {
                    "event": "section_missing_id_skipped",
                    "ticker": ticker,
                    "canonical_id": canonical_id,
                    "source_ref": sec.source_ref,
                }
            )
            continue
        split = section_items.split_section(sec)
        sec_items = section_items.to_section_items(sec, split)
        all_items.extend(sec_items)
        key = _period_key(sec.fiscal_year, sec.fiscal_period)
        if periods and periods[-1][0] == key:
            periods[-1][1].extend(sec_items)
        else:
            periods.append((key, sec_items))

    events: list[DisclosureEvent] = []
    for (_prior_key, prior_items), (_cur_key, current_items) in itertools.pairwise(periods):
        events.extend(align_period_pair(prior_items, current_items))

    # A suppressed item is invisible in the output by construction, so report
    # it: a silent 36% drop in event volume must be attributable to this gate
    # rather than read as the detector quietly going quiet.
    tabular_items = sum(1 for it in all_items if _is_tabular_item(it.body))
    if tabular_items:
        log.info(
            {
                "event": "item_diff_tabular_items_suppressed",
                "ticker": ticker,
                "canonical_id": canonical_id,
                "tabular_items": tabular_items,
                "items_total": len(all_items),
                "note": "extracted table content is not narrative drift; "
                "numeric change belongs to the facts/XBRL pipeline",
            }
        )

    return all_items, events


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _require_table(conn: sqlite3.Connection, *, missing_ok: bool) -> bool:
    if table_exists(conn, EVENTS_TABLE):
        return True
    if missing_ok:
        log.info({"event": "disclosure_events_table_absent", "degraded": True})
        return False
    raise HardStopError(
        f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0203) "
        "before writing or querying disclosure events"
    )


def write_events(conn: sqlite3.Connection, events: Sequence[DisclosureEvent]) -> int:
    """Upsert events on the unique key (ticker, event_type, fiscal_year,
    fiscal_period, canonical_id, subject, detector_version) — matching
    ``uq_disclosure_events`` from migration 0203.

    Uses an explicit NULL-safe SELECT-then-INSERT/UPDATE rather than
    ``ON CONFLICT``, for the same reason as ``store.record_coverage``: SQLite
    does not treat two NULL ``fiscal_year``/``fiscal_period`` values as
    conflicting under a UNIQUE index, so ``ON CONFLICT`` would silently
    accumulate duplicate rows for exactly the events whose period we could not
    resolve. ``item_added`` events legitimately carry no prior period, but
    their OWN ``fiscal_year``/``fiscal_period`` (the period they were first
    seen in) is always set, so this only matters if a future detector emits
    with a null current period — handled the same way regardless.

    ``canonical_id`` is NOT NULL in the table (empty-string sentinel), so a
    ``None`` from ``SectionItem.canonical_id`` (unclassified section types)
    is coerced to ``""`` here — the one place every event funnels through
    before it touches the row-identity columns.
    """
    if not events:
        return 0
    _require_table(conn, missing_ok=False)
    now = _now()
    written = 0
    for e in events:
        canonical_id = e.canonical_id or ""
        existing = conn.execute(
            f"""
            SELECT id FROM {EVENTS_TABLE}
            WHERE ticker = ? AND event_type = ? AND fiscal_year IS ? AND fiscal_period IS ?
              AND canonical_id = ? AND subject = ? AND detector_version = ?
            LIMIT 1
            """,
            (
                e.ticker,
                e.event_type,
                e.fiscal_year,
                e.fiscal_period.value if e.fiscal_period else None,
                canonical_id,
                e.subject,
                e.detector_version,
            ),
        ).fetchone()
        if existing is None:
            conn.execute(
                f"""
                INSERT INTO {EVENTS_TABLE}
                    (ticker, event_type, form, fiscal_year, fiscal_period,
                     prior_fiscal_year, prior_fiscal_period, source_ref,
                     source_doc_id, canonical_id, subject, subject_label,
                     prior_excerpt, current_excerpt, evidence_quote,
                     materiality, verdict, detector_version, status,
                     created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?)
                """,
                (
                    e.ticker,
                    e.event_type,
                    e.form.value if e.form else None,
                    e.fiscal_year,
                    e.fiscal_period.value if e.fiscal_period else None,
                    e.prior_fiscal_year,
                    e.prior_fiscal_period.value if e.prior_fiscal_period else None,
                    e.source_ref,
                    e.source_doc_id,
                    canonical_id,
                    e.subject,
                    e.subject_label,
                    e.prior_excerpt,
                    e.current_excerpt,
                    e.evidence_quote,
                    e.materiality,
                    e.verdict,
                    e.detector_version,
                    now,
                ),
            )
        else:
            conn.execute(
                f"""
                UPDATE {EVENTS_TABLE}
                SET form = ?, prior_fiscal_year = ?, prior_fiscal_period = ?,
                    source_ref = ?, source_doc_id = ?, canonical_id = ?,
                    subject_label = ?, prior_excerpt = ?, current_excerpt = ?,
                    evidence_quote = ?, materiality = ?
                WHERE id = ?
                """,
                (
                    e.form.value if e.form else None,
                    e.prior_fiscal_year,
                    e.prior_fiscal_period.value if e.prior_fiscal_period else None,
                    e.source_ref,
                    e.source_doc_id,
                    canonical_id,
                    e.subject_label,
                    e.prior_excerpt,
                    e.current_excerpt,
                    e.evidence_quote,
                    e.materiality,
                    int(existing[0]),
                ),
            )
        written += 1
    return written


def get_events(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    event_type: str | None = None,
    canonical_id: str | None = None,
    fiscal_year: int | None = None,
    missing_ok: bool = False,
) -> list[DisclosureEvent]:
    """Read back persisted events for a ticker, for tests and future surfaces."""
    if not _require_table(conn, missing_ok=missing_ok):
        return []
    clauses = ["ticker = ?"]
    params: list[object] = [ticker.strip().upper()]
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if canonical_id is not None:
        clauses.append("canonical_id = ?")
        params.append(canonical_id)
    if fiscal_year is not None:
        clauses.append("fiscal_year = ?")
        params.append(fiscal_year)

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {EVENTS_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY fiscal_year DESC, fiscal_period DESC, subject",
            tuple(params),
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    out: list[DisclosureEvent] = []
    for row in rows:
        try:
            out.append(
                DisclosureEvent(
                    ticker=row["ticker"],
                    event_type=row["event_type"],
                    form=FilingForm(row["form"]) if row["form"] else None,
                    fiscal_year=row["fiscal_year"],
                    fiscal_period=FiscalPeriod(row["fiscal_period"])
                    if row["fiscal_period"]
                    else None,
                    prior_fiscal_year=row["prior_fiscal_year"],
                    prior_fiscal_period=(
                        FiscalPeriod(row["prior_fiscal_period"])
                        if row["prior_fiscal_period"]
                        else None
                    ),
                    source_ref=row["source_ref"],
                    source_doc_id=row["source_doc_id"],
                    canonical_id=row["canonical_id"],
                    subject=row["subject"],
                    subject_label=row["subject_label"],
                    prior_excerpt=row["prior_excerpt"],
                    current_excerpt=row["current_excerpt"],
                    evidence_quote=row["evidence_quote"],
                    materiality=row["materiality"],
                    verdict=row["verdict"],
                    detector_version=row["detector_version"],
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            log.warning({"event": "disclosure_event_row_invalid", "error": str(exc)})
    return out
