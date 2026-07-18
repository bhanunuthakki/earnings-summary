"""Deterministic bottoms-up comparable-set resolution
(docs/design/comparable_sets_bottoms_up.md §2-3). Phase 1 foundation.

Mirrors ``peer_selection.py``'s shape: pure resolver functions + a
cache/freeze step, but the "cache" here is the ``comparable_sets`` /
``comparable_set_members`` tables (§3.3), not a JSON file — freezing needs
``valid_from``/``valid_to`` history, which a flat JSON cache doesn't model
well.

Pipeline per subject ticker:
  1. Build the **pool** (§2): tracked_companies rows with list_type in
     portfolio/watchlist/evaluation/index_member, non-ETF, joined against
     each ticker's cached FMP ``_profile.json`` for sector/industry/market
     cap/exchange/trading-status.
  2. Run the rule ladder (§3.1): industry+size-band seed (Step A) -> sector
     widen if under-filled (Step B) -> union LLM-ratified peers from
     ``data/peer_selection/{TICKER}.json`` (Step C, no new LLM call) ->
     owner pinned override from ``comparable_set_overrides`` (Step D,
     always wins).
  3. Classify the subject's metric_class (§3.2): financial / reit /
     operating, via a keyword-blob match on sector+industry (same technique
     as ``valuation_basis._sector_fallback``).
  4. Freeze into ``comparable_sets`` + ``comparable_set_members``
     (§3.3) — idempotent unless membership actually changed or
     ``--refresh``.

Deviation from the doc, noted here per Directive Maintenance convention:
§3.1 Step A's US-listed guard names ``exchangeShortName``. Empirically (this
account's FMP profile cache), ``exchangeShortName`` is populated ``None``
for every checked ticker (NU, AMZN, GOOGL, META, MELI, NVO, NOW, WIX, RBRK,
VEEV, BN) while ``exchange`` carries the real value ("NYSE"/"NASDAQ"). The
guard below checks ``exchange`` first, falling back to ``exchangeShortName``
if ever populated, rather than the literal (always-null) field name.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from compute.comparable_set_overrides import ComparableSetOverride, get_override
from compute.peer_selection import load_suggestions
from identity import DEFAULT_USER_ID

# Bumped whenever the rule ladder, size-band constants, or metric-class
# keyword list changes (§3.3). A resolve under a NEW version always inserts a
# new comparable_sets row (never mutates an old version's rows); a resolve
# under the SAME version that finds different membership updates that same
# comparable_set_id's members (closes superseded ones, opens new ones) — see
# `freeze_comparable_set`.
METHOD_VERSION = 1

# The median calculations in §5 aren't meaningful below single digits — a
# named, greppable knob per §3.1 Step B, not a magic number.
MIN_COMPARABLE_SET_SIZE = 8

_US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "NYSEARCA"})

_POOL_LIST_TYPES = frozenset({"portfolio", "watchlist", "evaluation", "index_member"})

MembershipReason = str  # 'industry_seed'|'sector_widened'|'llm_ratified'|'pinned_override'
MetricClass = str  # 'operating'|'financial'|'reit'


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolProfile:
    """One pool member's identity fields, joined from tracked_companies +
    its cached FMP profile.json."""

    ticker: str
    list_type: str
    sector: str | None
    industry: str | None
    market_cap: float | None
    exchange: str | None
    is_actively_trading: bool
    is_etf: bool


@dataclass
class ComparableSetMember:
    member_ticker: str
    membership_reason: MembershipReason
    context_only: bool = False


@dataclass
class ComparableSetResolution:
    ticker: str
    method_version: int
    metric_class: MetricClass
    members: list[ComparableSetMember]
    method_flags: dict[str, object]
    source_summary: dict[str, object]
    skipped_reason: str | None = None


@dataclass
class FreezeOutcome:
    comparable_set_id: str
    action: str  # 'inserted'|'updated'|'unchanged'
    n_members: int
    opened: list[str] = field(default_factory=list[str])
    closed: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# §3.2 metric-class classification
# ---------------------------------------------------------------------------


def metric_class(sector: str | None, industry: str | None) -> MetricClass:
    """Derive the metric-class from sector+industry via a keyword-blob
    match — mirrors the existing precedent
    ``valuation_basis._sector_fallback`` (same technique, new keyword set)."""
    blob = f"{sector or ''} {industry or ''}".lower()
    if any(
        t in blob
        for t in (
            "bank",
            "diversified financial",
            "insurance",
            "asset management",
            "credit services",
            "capital markets",
        )
    ):
        return "financial"
    if any(t in blob for t in ("reit", "real estate")):
        return "reit"
    return "operating"


# ---------------------------------------------------------------------------
# Pool construction
# ---------------------------------------------------------------------------


def _profile_fields(repo_root: Path, ticker: str) -> dict[str, object] | None:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, list):
        rows = cast("list[object]", raw)
        raw = rows[0] if rows else None
    if not isinstance(raw, dict):
        return None
    return cast("dict[str, object]", raw)


def load_pool(
    conn: sqlite3.Connection,
    repo_root: Path,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, PoolProfile]:
    """Build the pool (§2): every non-archived tracked_companies row in
    portfolio/watchlist/evaluation/index_member for ``user_id``, joined
    against its cached FMP profile, EXCLUDING ETFs (belt-and-suspenders:
    both ``list_type='etf'`` and a profile ``isEtf``/``isFund`` truthy flag
    are excluded, even though `_POOL_LIST_TYPES` above already omits the
    'etf' list_type — a mislabeled instrument_type='etf' row under a
    different list_type is still caught here)."""
    placeholders = ",".join("?" for _ in _POOL_LIST_TYPES)
    cur = conn.execute(
        "SELECT ticker, list_type, instrument_type FROM tracked_companies "
        f"WHERE user_id = ? AND list_type IN ({placeholders}) AND archived_at IS NULL",
        (user_id, *sorted(_POOL_LIST_TYPES)),
    )
    pool: dict[str, PoolProfile] = {}
    for row in cur.fetchall():
        ticker = str(row["ticker"]).upper()
        if row["instrument_type"] == "etf":
            continue
        rec = _profile_fields(repo_root, ticker)
        if rec is None:
            continue
        is_etf = bool(rec.get("isEtf")) or bool(rec.get("isFund"))
        if is_etf:
            continue
        sector = _str_or_none(rec.get("sector"))
        industry = _str_or_none(rec.get("industry"))
        market_cap = _float(rec.get("marketCap"))
        # Deviation from the doc's literal field name — see module docstring.
        exchange = _str_or_none(rec.get("exchangeShortName")) or _str_or_none(rec.get("exchange"))
        is_active = rec.get("isActivelyTrading")
        pool[ticker] = PoolProfile(
            ticker=ticker,
            list_type=str(row["list_type"]),
            sector=sector,
            industry=industry,
            market_cap=market_cap,
            exchange=exchange,
            is_actively_trading=is_active is not False,
            is_etf=is_etf,
        )
    return pool


def _is_us_listed(profile: PoolProfile) -> bool:
    return profile.exchange is not None and profile.exchange.upper() in _US_EXCHANGES


# ---------------------------------------------------------------------------
# §6 pool-wide industry/sector slices (Phase 2)
# ---------------------------------------------------------------------------

# Sector-scope metric-class map (Phase 2). The §3.2 keyword blob is tuned for
# `industry` strings ("Banks - Regional", "Financial - Credit Services", ...);
# FMP's coarse *sector* strings don't hit those keywords ("Financial
# Services" contains none of them), so sector slices use this explicit,
# owner-visible map instead of widening the keyword list — widening it would
# reclassify existing per-ticker subjects and force a METHOD_VERSION bump for
# what is purely a new-scope concern.
SECTOR_SCOPE_METRIC_CLASS: dict[str, MetricClass] = {
    "Financial Services": "financial",
    "Real Estate": "reit",
}


def scope_metric_class(scope_type: str, scope_key: str) -> MetricClass:
    """Metric-class for a pool-wide slice row (`scope_type` 'industry' or
    'sector'; §6). Industry slices reuse the §3.2 keyword classifier;
    sector slices use `SECTOR_SCOPE_METRIC_CLASS` (see its comment)."""
    if scope_type == "industry":
        return metric_class(None, scope_key)
    return SECTOR_SCOPE_METRIC_CLASS.get(scope_key, "operating")


def pool_scope_slices(pool: dict[str, PoolProfile]) -> dict[tuple[str, str], list[str]]:
    """(scope_type, scope_key) -> sorted member tickers, for every industry
    and sector present in the pool (§6: "the pool-wide slice — every pool
    member in that industry/sector, not a per-ticker market-cap band").

    The §3.1 US-listed + actively-trading guards still apply (§7's expected-
    deviation notes describe the bottoms-up universe as US-listed-only; a
    non-US listing in the pool would silently break that documented
    comparison basis). No market-cap band and no MIN_COMPARABLE_SET_SIZE
    floor — thin slices are written with honest coverage (§5.5), never
    dropped.
    """
    slices: dict[tuple[str, str], list[str]] = {}
    for ticker, member in pool.items():
        if not _is_us_listed(member) or not member.is_actively_trading:
            continue
        if member.industry is not None:
            slices.setdefault(("industry", member.industry), []).append(ticker)
        if member.sector is not None:
            slices.setdefault(("sector", member.sector), []).append(ticker)
    return {key: sorted(tickers) for key, tickers in sorted(slices.items())}


# ---------------------------------------------------------------------------
# §3.1 rule ladder
# ---------------------------------------------------------------------------


def _step_a(subject: PoolProfile, pool: dict[str, PoolProfile]) -> list[str]:
    if subject.market_cap is None or subject.industry is None:
        return []
    lo, hi = subject.market_cap / 4, subject.market_cap * 4
    out: list[str] = []
    for t, m in pool.items():
        if t == subject.ticker:
            continue
        if m.industry != subject.industry:
            continue
        if m.market_cap is None or not (lo <= m.market_cap <= hi):
            continue
        if not _is_us_listed(m):
            continue
        if not m.is_actively_trading:
            continue
        out.append(t)
    return sorted(out)


def _step_b(subject: PoolProfile, pool: dict[str, PoolProfile]) -> list[str]:
    if subject.market_cap is None or subject.sector is None:
        return []
    lo, hi = subject.market_cap / 10, subject.market_cap * 10
    out: list[str] = []
    for t, m in pool.items():
        if t == subject.ticker:
            continue
        if m.sector != subject.sector:
            continue
        if m.market_cap is None or not (lo <= m.market_cap <= hi):
            continue
        if not _is_us_listed(m):
            continue
        if not m.is_actively_trading:
            continue
        out.append(t)
    return sorted(out)


def _step_c(
    ticker: str,
    repo_root: Path,
    pool: dict[str, PoolProfile],
    override: ComparableSetOverride | None,
) -> tuple[list[str], list[str]]:
    """Return (full_members, context_only_members) from the LLM-ratified
    peer_selection cache. No new LLM call — reads the existing
    ``data/peer_selection/{TICKER}.json`` cache directly (§3.1 Step C).

    Ratification gate: an owner `force_exclude` always vetoes a suggested
    peer (checked here, per §3.1's "the owner's veto always wins"). Full
    staleness re-validation of the suggestion cache (recomputing its
    ``inputs_sha256`` from current business-description/segment inputs) is
    NOT implemented in Phase 1 — the on-disk cache is trusted as-is, which
    is consistent with "no new LLM call for membership" and is the simplest
    honest starting point; flagged as a Phase 2 hardening candidate.

    Deviation from the doc's literal §3.1 Step C gate, verified against real
    caches (NU/MELI/WIX/...): the doc's 3-way split gates full membership on
    ``ticker in fetched_complete`` — but ``fetched_complete`` only records
    peers ``peer_selection.py`` freshly fetched *in that run*; a suggested
    peer that's independently already in-pool (e.g. NU's suggestion `MELI`,
    itself a portfolio ticker with its own long-standing FMP cache from the
    normal per-ticker fetch job) is never fetched by peer_selection at all —
    its cache files already exist, so `_fetch_peer_fundamentals`'s
    if-not-todo-skip never adds it to `fetched_complete`. Gating on that flag
    would wrongly demote nearly every genuinely-complete in-pool suggestion
    to `context_only`. Fixed here: pool membership alone (§2's pool
    definition already guarantees real financials via the independent
    universe-pool fetch job) is the full-member gate; `fetched_complete` is
    unused. An out-of-pool suggestion is always `context_only`, matching the
    doc's own "cannot happen today" caveat for the out-of-pool+complete case.
    """
    suggestions = load_suggestions(repo_root, ticker)
    if not suggestions:
        return [], []
    excluded = frozenset(t.upper() for t in (override.force_exclude if override else ()))

    full: list[str] = []
    context_only: list[str] = []
    for sug in suggestions:
        peer = sug.ticker.upper()
        if peer in excluded or peer == ticker.upper():
            continue
        if peer in pool:
            full.append(peer)
        else:
            # Out-of-pool suggestion: per §1's confirmed 402 wall, only
            # `profile` (if that) resolves for a never-before-served symbol
            # — never a full data point.
            context_only.append(peer)
    return full, context_only


def _apply_recently_ipod_tag(repo_root: Path, ticker: str) -> bool:
    """Best-effort read of `recently_ipod` from micro_thesis/holdings/{T}.json
    — annotation only, never a gate (§3.2: the natural 4-trailing-quarter gate
    in the metrics layer already excludes thin-history members)."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return False
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("recently_ipod"))


# ---------------------------------------------------------------------------
# Resolution entry point
# ---------------------------------------------------------------------------


def resolve_comparable_set(
    ticker: str,
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    pool: dict[str, PoolProfile] | None = None,
    method_version: int = METHOD_VERSION,
    user_id: str = DEFAULT_USER_ID,
) -> ComparableSetResolution:
    """Run the full §3.1 rule ladder for one subject ticker."""
    ticker = ticker.upper()
    if pool is None:
        pool = load_pool(conn, repo_root, user_id=user_id)
    subject = pool.get(ticker)
    if subject is None:
        return ComparableSetResolution(
            ticker=ticker,
            method_version=method_version,
            metric_class="operating",
            members=[],
            method_flags={},
            source_summary={},
            skipped_reason=f"{ticker} not found in the pool (not tracked, or no cached FMP profile)",
        )

    mclass = metric_class(subject.sector, subject.industry)
    override = get_override(ticker)

    step_a = _step_a(subject, pool)
    members: dict[str, ComparableSetMember] = {
        t: ComparableSetMember(t, "industry_seed") for t in step_a
    }

    step_b: list[str] = []
    if len(members) < MIN_COMPARABLE_SET_SIZE:
        step_b = _step_b(subject, pool)
        for t in step_b:
            members.setdefault(t, ComparableSetMember(t, "sector_widened"))

    full_c, context_c = _step_c(ticker, repo_root, pool, override)
    for t in full_c:
        members.setdefault(t, ComparableSetMember(t, "llm_ratified", context_only=False))
    for t in context_c:
        members.setdefault(t, ComparableSetMember(t, "llm_ratified", context_only=True))

    override_applied = False
    if override is not None:
        for t in override.force_include:
            t = t.upper()
            members[t] = ComparableSetMember(t, "pinned_override", context_only=t not in pool)
            override_applied = True
        for t in override.force_exclude:
            if members.pop(t.upper(), None) is not None:
                override_applied = True

    method_flags: dict[str, object] = dict(override.method_flags) if override else {}
    if override is not None and override.note:
        method_flags["override_note"] = override.note
    if _apply_recently_ipod_tag(repo_root, ticker):
        method_flags["subject_recently_ipod"] = True
    if mclass == "financial":
        # §5.4: PE is still computed and stored (never suppressed) but this
        # tells a consumer which column to headline.
        method_flags["primary_metric"] = "p_tbv"

    source_summary: dict[str, object] = {
        "step_a_n": len(step_a),
        "step_b_n": len(step_b),
        "step_c_n": len(full_c) + len(context_c),
        "step_c_full_n": len(full_c),
        "step_c_context_only_n": len(context_c),
        "override_applied": override_applied,
    }

    return ComparableSetResolution(
        ticker=ticker,
        method_version=method_version,
        metric_class=mclass,
        members=sorted(members.values(), key=lambda m: m.member_ticker),
        method_flags=method_flags,
        source_summary=source_summary,
    )


# ---------------------------------------------------------------------------
# §3.3 versioning & freezing
# ---------------------------------------------------------------------------


def comparable_set_id(ticker: str, method_version: int = METHOD_VERSION) -> str:
    return f"{ticker.upper()}_{method_version}"


def _current_open_members(
    conn: sqlite3.Connection, set_id: str
) -> dict[str, tuple[str, bool, str]]:
    """member_ticker -> (membership_reason, context_only, valid_from) for
    currently-open (valid_to IS NULL) rows of this comparable_set_id."""
    cur = conn.execute(
        "SELECT member_ticker, membership_reason, context_only, valid_from "
        "FROM comparable_set_members WHERE comparable_set_id = ? AND valid_to IS NULL",
        (set_id,),
    )
    return {
        str(r["member_ticker"]): (
            str(r["membership_reason"]),
            bool(r["context_only"]),
            str(r["valid_from"]),
        )
        for r in cur.fetchall()
    }


def freeze_comparable_set(
    conn: sqlite3.Connection,
    resolution: ComparableSetResolution,
    *,
    refresh: bool = False,
    as_of: date | None = None,
) -> FreezeOutcome:
    """Write ``resolution`` into ``comparable_sets`` + ``comparable_set_members``.

    Idempotent (§3.3): a re-resolve whose membership set is byte-identical to
    the currently-open set is a no-op unless ``refresh=True``. Otherwise,
    members no longer resolved are closed (``valid_to`` = today) and newly
    resolved members are opened (``valid_from`` = today); unchanged members
    are left untouched (their original ``valid_from`` is preserved).

    Same-day churn: ``valid_from``/``valid_to`` are DATE-granularity, so a
    member whose row was already opened *today* and then changes again
    *today* (e.g. two ``--refresh`` runs in the same session) cannot be
    represented as close-today + reopen-today — that's the same primary key.
    Such a same-day change is updated in place (no valid_to close, no new
    row) rather than raising a UNIQUE-constraint error; a member's real
    membership history is still exact at day granularity, which is the
    schema's documented resolution (§3.3/§6).
    """
    as_of = as_of or datetime.now(UTC).date()
    as_of_iso = as_of.isoformat()
    set_id = comparable_set_id(resolution.ticker, resolution.method_version)
    new_state = {m.member_ticker: (m.membership_reason, m.context_only) for m in resolution.members}
    old_state = _current_open_members(conn, set_id)
    old_state_cmp = {t: (reason, ctx) for t, (reason, ctx, _vf) in old_state.items()}

    if not refresh and new_state == old_state_cmp:
        return FreezeOutcome(comparable_set_id=set_id, action="unchanged", n_members=len(new_state))

    conn.execute(
        "INSERT INTO comparable_sets "
        "(comparable_set_id, ticker, method_version, resolved_at, metric_class, "
        " method_flags, source_summary) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(comparable_set_id) DO UPDATE SET "
        "resolved_at=excluded.resolved_at, metric_class=excluded.metric_class, "
        "method_flags=excluded.method_flags, source_summary=excluded.source_summary",
        (
            set_id,
            resolution.ticker,
            resolution.method_version,
            datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
            resolution.metric_class,
            json.dumps(resolution.method_flags),
            json.dumps(resolution.source_summary),
        ),
    )

    closed: list[str] = []
    opened: list[str] = []
    for t, (old_reason, old_ctx, old_valid_from) in old_state.items():
        old = (old_reason, old_ctx)
        new = new_state.get(t)
        if new == old:
            continue
        if old_valid_from == as_of_iso:
            # Same-day update in place — see docstring.
            if new is not None:
                new_reason, new_ctx = new
                conn.execute(
                    "UPDATE comparable_set_members SET membership_reason = ?, context_only = ? "
                    "WHERE comparable_set_id = ? AND member_ticker = ? AND valid_from = ? "
                    "AND valid_to IS NULL",
                    (new_reason, new_ctx, set_id, t, old_valid_from),
                )
            else:
                conn.execute(
                    "DELETE FROM comparable_set_members WHERE comparable_set_id = ? "
                    "AND member_ticker = ? AND valid_from = ? AND valid_to IS NULL",
                    (set_id, t, old_valid_from),
                )
            continue
        conn.execute(
            "UPDATE comparable_set_members SET valid_to = ? "
            "WHERE comparable_set_id = ? AND member_ticker = ? AND valid_to IS NULL",
            (as_of_iso, set_id, t),
        )
        closed.append(t)
    for t, new in new_state.items():
        old_entry = old_state.get(t)
        old = (old_entry[0], old_entry[1]) if old_entry is not None else None
        if new == old:
            continue
        if old_entry is not None and old_entry[2] == as_of_iso:
            # Handled as an in-place update above.
            opened.append(t)
            continue
        reason, context_only = new
        conn.execute(
            "INSERT INTO comparable_set_members "
            "(comparable_set_id, member_ticker, membership_reason, context_only, "
            " valid_from, valid_to) VALUES (?, ?, ?, ?, ?, NULL)",
            (set_id, t, reason, context_only, as_of_iso),
        )
        opened.append(t)
    conn.commit()

    action = "inserted" if not old_state else "updated"
    return FreezeOutcome(
        comparable_set_id=set_id,
        action=action,
        n_members=len(new_state),
        opened=sorted(opened),
        closed=sorted(closed),
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _str_or_none(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None
