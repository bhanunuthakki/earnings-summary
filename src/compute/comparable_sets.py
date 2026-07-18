"""Deterministic comparable-set identification (docs/design/comparable_sets_bottoms_up.md
sections 2-3). Phase 1 of the bottoms-up comparable-sets program.

Mirrors ``compute.peer_selection``'s shape: pure resolver functions + a freeze step,
reading only local caches (``data/historical/fmp/{T}_profile.json``,
``data/peer_selection/{T}.json``) plus ``tracked_companies`` -- no network, no LLM call
(Step C reuses the EXISTING peer_selection cache, never triggers a new suggestion).

Rule ladder (section 3.1), run in this order:
  A. industry + size-band seed within the candidate pool.
  B. sector widen, only if Step A under-fills (< ``MIN_COMPARABLE_SET_SIZE``).
  C. union LLM-ratified peers from the existing ``data/peer_selection/{T}.json`` cache.
  D. per-ticker pinned override (``compute.comparable_set_overrides``), always wins.

Versioning/freezing (section 3.3): ``freeze_comparable_set`` writes/updates
``comparable_sets`` + ``comparable_set_members`` idempotently -- a re-run whose
resolved membership is unchanged from the currently-open (``valid_to IS NULL``) set
is a no-op unless ``refresh=True``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from compute.comparable_set_overrides import ComparableSetOverride, get_override
from identity import DEFAULT_USER_ID
from pipeline.fmp_doc_index import classify_instrument_type_from_profile

# Floor below which the median/aggregate calculations in comp_set_metrics stop being
# meaningful -- a named, greppable knob, not a magic number (doc section 3.1).
MIN_COMPARABLE_SET_SIZE = 8

# Bumped whenever the rule ladder, size-band constants, or metric-class keyword list
# changes. Never mutate rows already frozen under an old version -- a resolve under a
# new version always inserts a new `comparable_sets` row (doc section 3.3).
CURRENT_METHOD_VERSION = 1

_US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "NYSEARCA"})

_POOL_LIST_TYPES: tuple[str, ...] = ("portfolio", "watchlist", "evaluation", "index_member")

_FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "bank",
    "diversified financial",
    "insurance",
    "asset management",
    "credit services",
    "capital markets",
)
_REIT_KEYWORDS: tuple[str, ...] = ("reit", "real estate")


class MetricClass(StrEnum):
    """Which metrics are primary for a subject/member (doc section 3.2). Does NOT
    exclude a member from a set -- a bank in an operating comp set still contributes
    its P/B, just not its PE to the headline PE line."""

    OPERATING = "operating"
    FINANCIAL = "financial"
    REIT = "reit"


class MembershipReason(StrEnum):
    INDUSTRY_SEED = "industry_seed"
    SECTOR_WIDENED = "sector_widened"
    LLM_RATIFIED = "llm_ratified"
    PINNED_OVERRIDE = "pinned_override"


def metric_class_for(sector: str | None, industry: str | None) -> MetricClass:
    """Keyword-blob classification, mirroring the existing precedent
    ``valuation_basis.py::_sector_fallback`` (same technique, new keyword set)."""
    blob = f"{sector or ''} {industry or ''}".lower()
    if any(term in blob for term in _FINANCIAL_KEYWORDS):
        return MetricClass.FINANCIAL
    if any(term in blob for term in _REIT_KEYWORDS):
        return MetricClass.REIT
    return MetricClass.OPERATING


@dataclass(slots=True)
class PoolMember:
    """One candidate-pool ticker's screenable identity, from ``tracked_companies`` +
    its cached FMP profile. Absent from the pool entirely if it has no cached
    profile (§1's data inventory means there's nothing to screen on) or if it's an
    ETF by either belt-and-suspenders check (doc section 2)."""

    ticker: str
    name: str | None
    list_type: str
    instrument_type: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    exchange: str | None
    is_actively_trading: bool


@dataclass(slots=True)
class MemberResolution:
    ticker: str
    reason: MembershipReason
    context_only: bool


@dataclass(slots=True)
class ResolvedSet:
    ticker: str
    metric_class: MetricClass
    members: list[MemberResolution]
    source_summary: dict[str, object]
    method_flags: dict[str, bool] = field(default_factory=dict[str, bool])


@dataclass(slots=True)
class FreezeOutcome:
    comparable_set_id: str
    changed: bool
    members_added: list[str]
    members_removed: list[str]


# ---------------------------------------------------------------------------
# Pool loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _first_record(path: Path) -> dict[str, object] | None:
    raw = _read_json(path)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return cast("dict[str, object]", raw[0])
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return None


def _num(record: dict[str, object], key: str) -> float | None:
    v = record.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _str_or_none(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def load_pool(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> list[PoolMember]:
    """The candidate pool: ``tracked_companies`` rows in the four boundary
    list_types, ETF-excluded, merged with their cached FMP profile (doc section
    2). A ticker with no cached profile is silently unresolvable and dropped --
    that's a pool-membership gap, not a metric-coverage gap (comp_set_metrics'
    coverage honesty is about MEMBERS' metrics, not pool eligibility)."""
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in _POOL_LIST_TYPES)
    cur.execute(
        "SELECT ticker, list_type, instrument_type FROM tracked_companies "
        f"WHERE user_id = ? AND list_type IN ({placeholders}) AND archived_at IS NULL",
        (user_id, *_POOL_LIST_TYPES),
    )
    rows = cur.fetchall()
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    out: list[PoolMember] = []
    seen: set[str] = set()
    for row in rows:
        ticker = str(row["ticker"]).upper()
        if ticker in seen:
            continue
        instrument_type = row["instrument_type"]
        if instrument_type == "etf":
            continue
        profile = _first_record(fmp_dir / f"{ticker}_profile.json")
        if profile is None:
            continue
        if classify_instrument_type_from_profile(profile) == "etf":
            continue
        seen.add(ticker)
        exchange = _str_or_none(profile.get("exchangeShortName")) or _str_or_none(
            profile.get("exchange")
        )
        out.append(
            PoolMember(
                ticker=ticker,
                name=_str_or_none(profile.get("companyName")),
                list_type=str(row["list_type"]),
                instrument_type=str(instrument_type) if instrument_type else None,
                sector=_str_or_none(profile.get("sector")),
                industry=_str_or_none(profile.get("industry")),
                market_cap=_num(profile, "marketCap"),
                exchange=exchange,
                is_actively_trading=profile.get("isActivelyTrading") is not False,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule ladder — Steps A / B
# ---------------------------------------------------------------------------


def _passes_guards(m: PoolMember) -> bool:
    return m.is_actively_trading and m.instrument_type != "etf" and m.exchange in _US_EXCHANGES


def _step_a_industry_seed(subject: PoolMember, pool: list[PoolMember]) -> set[str]:
    if subject.industry is None or subject.market_cap is None or subject.market_cap <= 0:
        return set()
    lo, hi = subject.market_cap / 4, subject.market_cap * 4
    out: set[str] = set()
    for m in pool:
        if m.ticker == subject.ticker:
            continue
        if m.industry != subject.industry:
            continue
        if m.market_cap is None or not (lo <= m.market_cap <= hi):
            continue
        if not _passes_guards(m):
            continue
        out.add(m.ticker)
    return out


def _step_b_sector_widen(subject: PoolMember, pool: list[PoolMember]) -> set[str]:
    if subject.sector is None or subject.market_cap is None or subject.market_cap <= 0:
        return set()
    lo, hi = subject.market_cap / 10, subject.market_cap * 10
    out: set[str] = set()
    for m in pool:
        if m.ticker == subject.ticker:
            continue
        if m.sector != subject.sector:
            continue
        if m.market_cap is None or not (lo <= m.market_cap <= hi):
            continue
        if not _passes_guards(m):
            continue
        out.add(m.ticker)
    return out


# ---------------------------------------------------------------------------
# Rule ladder — Step C (union existing LLM-ratified peer cache)
# ---------------------------------------------------------------------------


def _load_peer_selection_cache(repo_root: Path, ticker: str) -> dict[str, object] | None:
    path = repo_root / "data" / "peer_selection" / f"{ticker.upper()}.json"
    data = _read_json(path)
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def _step_c_llm_ratified(ticker: str, repo_root: Path) -> tuple[dict[str, bool], int]:
    """Read the EXISTING ``data/peer_selection/{ticker}.json`` cache (no new LLM
    call). Returns ``{peer_ticker: context_only}`` plus the count of suggestions
    considered for ``source_summary``.

    ``fetched_complete`` peers are full members (contribute to every metric);
    ``fetched_peers``-only peers (market-cap-only, the documented 402-on-new-symbol
    outcome) are roster-visible but ``context_only=True``, excluded from every
    median/aggregate. A suggestion that resolved neither is dropped -- there's no
    data of any kind to roster it with."""
    cache = _load_peer_selection_cache(repo_root, ticker)
    if cache is None:
        return {}, 0
    raw_suggestions_obj = cache.get("suggestions")
    if not isinstance(raw_suggestions_obj, list):
        return {}, 0
    raw_suggestions = cast("list[object]", raw_suggestions_obj)
    fetched_complete = {
        str(t).upper() for t in cast("list[object]", cache.get("fetched_complete") or [])
    }
    fetched_peers = {str(t).upper() for t in cast("list[object]", cache.get("fetched_peers") or [])}
    out: dict[str, bool] = {}
    for entry in raw_suggestions:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast("dict[str, object]", entry)
        raw_ticker = entry_dict.get("ticker")
        if not isinstance(raw_ticker, str) or not raw_ticker.strip():
            continue
        peer = raw_ticker.strip().upper()
        if peer == ticker:
            continue
        if peer in fetched_complete:
            out[peer] = False
        elif peer in fetched_peers:
            out[peer] = True
        # else: suggested but nothing at all resolved for it — not rosterable.
    return out, len(raw_suggestions)


# ---------------------------------------------------------------------------
# Rule ladder — Step D (pinned override) + full resolve
# ---------------------------------------------------------------------------


def _apply_override(
    candidates: dict[str, MembershipReason],
    context_only: dict[str, bool],
    override: ComparableSetOverride | None,
) -> bool:
    """Splice ``force_include``/``force_exclude`` in place. Returns whether any
    override actually changed the resolved set (for ``source_summary``)."""
    if override is None:
        return False
    applied = False
    for t in override.force_exclude:
        tu = t.upper()
        if candidates.pop(tu, None) is not None:
            applied = True
        context_only.pop(tu, None)
    for t in override.force_include:
        tu = t.upper()
        if tu not in candidates:
            candidates[tu] = MembershipReason.PINNED_OVERRIDE
            context_only[tu] = False
            applied = True
    return applied


def resolve_comparable_set(
    ticker: str,
    pool: list[PoolMember],
    repo_root: Path,
) -> ResolvedSet:
    """Run the full A→B→C→D rule ladder for ``ticker``. Raises ``ValueError`` if
    ``ticker`` isn't itself in the candidate pool (no profile / list-type
    mismatch / archived) -- there's nothing to seed Step A/B from."""
    ticker = ticker.upper()
    subject = next((m for m in pool if m.ticker == ticker), None)
    if subject is None:
        raise ValueError(f"{ticker} is not in the comparable-set candidate pool")

    step_a = _step_a_industry_seed(subject, pool)
    candidates: dict[str, MembershipReason] = {t: MembershipReason.INDUSTRY_SEED for t in step_a}

    step_b: set[str] = set()
    if len(candidates) < MIN_COMPARABLE_SET_SIZE:
        step_b = _step_b_sector_widen(subject, pool)
        for t in step_b:
            candidates.setdefault(t, MembershipReason.SECTOR_WIDENED)

    context_only: dict[str, bool] = dict.fromkeys(candidates, False)

    step_c_map, step_c_n = _step_c_llm_ratified(ticker, repo_root)
    for t, is_context_only in step_c_map.items():
        if t not in candidates:
            candidates[t] = MembershipReason.LLM_RATIFIED
            context_only[t] = is_context_only

    override = get_override(ticker)
    override_applied = _apply_override(candidates, context_only, override)
    candidates.pop(ticker, None)  # never include self, defensively

    members = [
        MemberResolution(t, candidates[t], context_only.get(t, False)) for t in sorted(candidates)
    ]

    return ResolvedSet(
        ticker=ticker,
        metric_class=metric_class_for(subject.sector, subject.industry),
        members=members,
        source_summary={
            "step_a_n": len(step_a),
            "step_b_n": len(step_b - step_a),
            "step_c_n": step_c_n,
            "override_applied": override_applied,
        },
        method_flags=dict(override.method_flags) if override else {},
    )


# ---------------------------------------------------------------------------
# Versioning & freezing (doc section 3.3)
# ---------------------------------------------------------------------------


def comparable_set_id_for(ticker: str, method_version: int = CURRENT_METHOD_VERSION) -> str:
    return f"{ticker.upper()}_{method_version}"


def freeze_comparable_set(
    conn: sqlite3.Connection,
    resolved: ResolvedSet,
    *,
    as_of: date | None = None,
    refresh: bool = False,
    method_version: int = CURRENT_METHOD_VERSION,
) -> FreezeOutcome:
    """Write/update ``comparable_sets`` + ``comparable_set_members``.

    Idempotent: if a set already exists for ``(ticker, method_version)`` with
    identical currently-open (``valid_to IS NULL``) membership, this is a no-op
    unless ``refresh=True``. Otherwise this diffs the resolved set against the
    currently-open members: dropped members get ``valid_to = as_of``, new members
    get a fresh row with ``valid_from = as_of`` — the row/id itself never changes
    (only ``method_version`` bumps create a new ``comparable_sets`` row, doc
    section 3.3)."""
    as_of = as_of or date.today()
    comparable_set_id = comparable_set_id_for(resolved.ticker, method_version)

    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM comparable_sets WHERE comparable_set_id = ?",
        (comparable_set_id,),
    )
    exists = cur.fetchone() is not None

    cur.execute(
        "SELECT member_ticker, membership_reason, context_only FROM comparable_set_members "
        "WHERE comparable_set_id = ? AND valid_to IS NULL",
        (comparable_set_id,),
    )
    current_members = {
        str(r["member_ticker"]): (str(r["membership_reason"]), bool(r["context_only"]))
        for r in cur.fetchall()
    }
    resolved_members = {m.ticker: (m.reason.value, m.context_only) for m in resolved.members}

    if exists and not refresh and current_members == resolved_members:
        return FreezeOutcome(comparable_set_id, changed=False, members_added=[], members_removed=[])

    now = datetime.now()
    method_flags_json = json.dumps(resolved.method_flags) if resolved.method_flags else None
    source_summary_json = json.dumps(resolved.source_summary)

    if not exists:
        cur.execute(
            "INSERT INTO comparable_sets "
            "(comparable_set_id, ticker, method_version, resolved_at, metric_class, "
            " method_flags, source_summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                comparable_set_id,
                resolved.ticker,
                method_version,
                now,
                resolved.metric_class.value,
                method_flags_json,
                source_summary_json,
            ),
        )
    else:
        cur.execute(
            "UPDATE comparable_sets SET resolved_at = ?, metric_class = ?, "
            "method_flags = ?, source_summary = ? WHERE comparable_set_id = ?",
            (
                now,
                resolved.metric_class.value,
                method_flags_json,
                source_summary_json,
                comparable_set_id,
            ),
        )

    removed = sorted(set(current_members) - set(resolved_members))
    added = sorted(set(resolved_members) - set(current_members))

    for t in removed:
        cur.execute(
            "UPDATE comparable_set_members SET valid_to = ? "
            "WHERE comparable_set_id = ? AND member_ticker = ? AND valid_to IS NULL",
            (as_of, comparable_set_id, t),
        )
    for t in added:
        reason, context_only_flag = resolved_members[t]
        cur.execute(
            "INSERT INTO comparable_set_members "
            "(comparable_set_id, member_ticker, membership_reason, context_only, valid_from, valid_to) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (comparable_set_id, t, reason, context_only_flag, as_of),
        )

    conn.commit()
    return FreezeOutcome(
        comparable_set_id, changed=True, members_added=added, members_removed=removed
    )


def active_members(
    conn: sqlite3.Connection, comparable_set_id: str
) -> list[tuple[str, MembershipReason, bool]]:
    """Currently-open (``valid_to IS NULL``) members of a frozen set, for the
    aggregate-computation CLI to consume."""
    cur = conn.cursor()
    cur.execute(
        "SELECT member_ticker, membership_reason, context_only FROM comparable_set_members "
        "WHERE comparable_set_id = ? AND valid_to IS NULL ORDER BY member_ticker",
        (comparable_set_id,),
    )
    return [
        (str(r["member_ticker"]), MembershipReason(r["membership_reason"]), bool(r["context_only"]))
        for r in cur.fetchall()
    ]


def get_method_flags(conn: sqlite3.Connection, comparable_set_id: str) -> dict[str, object]:
    """Parsed ``comparable_sets.method_flags`` JSON for the given set, ``{}`` on
    any miss/malformed value — this is a passthrough annotation (e.g. a holdco's
    ``whole_co_pe_not_meaningful``), never load-bearing for computation itself."""
    cur = conn.cursor()
    cur.execute(
        "SELECT method_flags FROM comparable_sets WHERE comparable_set_id = ?",
        (comparable_set_id,),
    )
    row = cur.fetchone()
    if row is None or row["method_flags"] is None:
        return {}
    try:
        parsed = json.loads(row["method_flags"])
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def open_comparable_sets(conn: sqlite3.Connection) -> list[tuple[str, str, MetricClass, int]]:
    """(comparable_set_id, ticker, metric_class, method_version) for every set that
    still has open members — what ``track_comp_metrics.py`` iterates over."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT comparable_set_id, ticker, metric_class, method_version "
        "FROM comparable_sets ORDER BY ticker"
    )
    return [
        (
            str(r["comparable_set_id"]),
            str(r["ticker"]),
            MetricClass(r["metric_class"]),
            int(r["method_version"]),
        )
        for r in cur.fetchall()
    ]
