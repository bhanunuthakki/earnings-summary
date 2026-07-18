"""Deterministic collectors for the tenet-2 Phase 3 governor moment classes —
``capacity_breach``, ``life_event_checkpoint``, ``profile_drift`` (docs/design/
tenet2_advisory_program.md §4 delivery seam 3, §6 Phase-3 bullet, §7 rulings
4 + 6). Zero LLM.

Kept out of ``research.governor`` so the heavier reads (the materialized
weights cache, the live tracker, ``owner_profile_facts``) stay lazily
imported and independently unit-testable; ``governor.collect_moments``
imports this module lazily inside its own try/except block, matching the
file's existing per-class degrade discipline — a capacity check that can't
run (missing table, offline tracker, no repo_root) contributes nothing and
never breaks the other two governor classes.

``capacity_breach`` mirrors ``falsifier_breach``'s episode keying exactly:
the underlying condition (a human-capital bucket over cap, or the cash floor
violated) is written as its OWN ``alerts`` row (trigger_kind
``owner_capacity_breach``) via the SAME ``alerts.store`` sensor contract
every other trigger uses (``compute_signature_sha`` + ``find_by_signature``
dedup), and the governor Moment's key is ``f"alert:{alert.id}"` — the exact
shape ``falsifier_breach`` uses. One alert row per (bucket|floor) BREACH
EPISODE: a persistent breach keeps its signature and its already-considered
key, so it does not re-ping daily; once it resolves and the alert clears
(self-heal below), a LATER fresh breach gets a new alert id and a new key.
This is also what wires §4 delivery seam 4 (owner-policy-breach alert) for
free — the alert row this module writes is exactly what
``dashboard.inbox_rank.decisive_alert_reason`` recognizes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from owner_profile.models import HumanCapitalBucket, LifeEventFact
from owner_profile.store import get_current_profile, list_expiring_facts

if TYPE_CHECKING:
    from research.governor import Moment

log = logging.getLogger(__name__)

__all__ = [
    "CAPACITY_ALERT_TICKER",
    "CAPACITY_BREACH_TRIGGER_KIND",
    "LIFE_EVENT_LOOKAHEAD_DAYS",
    "TAX_DRIFT_DIVERGENCE",
    "TAX_DRIFT_STALE_DAYS",
    "capacity_breach_still_active",
    "collect_capacity_breach_moments",
    "collect_life_event_moments",
    "collect_profile_drift_moments",
    "repo_root_from_db_path",
]

CAPACITY_BREACH_TRIGGER_KIND = "owner_capacity_breach"
# Sentinel ticker for a book-level (not single-name) alert — alerts.ticker is
# NOT NULL by schema (0063), and a human-capital bucket or the cash floor is
# a PORTFOLIO-scope condition, not one ticker's. Mirrors the "PORTFOLIO" chip
# label the Ledger packet already uses for ticker=None items.
CAPACITY_ALERT_TICKER = "PORTFOLIO"

# A dated life event fires ONCE, this many days before it lands (§4: "the
# position's horizon window" precedent CAPACITY_HORIZON_DAYS uses 730 days for
# a HOLDING's review; this is the tighter governor-push window — a 2-year-out
# baby doesn't page, a 90-day-out one does).
LIFE_EVENT_LOOKAHEAD_DAYS = 90

# profile_drift's "contradicted by observed state" leg (tax-bucket totals):
# stale threshold ~2 quarters, divergence threshold 25% — both named in §6's
# Phase-3 bullet example verbatim.
TAX_DRIFT_STALE_DAYS = 180
TAX_DRIFT_DIVERGENCE = 0.25

# Tracker tax-treatment buckets comparable to the owner-profile's household
# tax-bucket vocabulary, EXCLUDING 'cash' and 'illiquid' — the tracker only
# ever observes brokerage-held investments, so comparing its total against an
# affirmed total that includes household cash/illiquid would manufacture a
# divergence out of buckets the tracker was never going to see in the first
# place. This keeps the comparison honest (like-for-like), at the cost of the
# check being narrower than "all household wealth."
_TRACKED_TAX_BUCKETS: frozenset[str] = frozenset({"pretax", "roth", "hsa", "taxable"})

# The tracker's `/api/portfolio/positioning` by_asset_type label vocabulary is
# owned by the sibling tracker service, not this repo — matched case-
# insensitively against the labels a cash/money-market sleeve is known to use.
_CASH_ASSET_LABELS: frozenset[str] = frozenset({"cash", "cash & equivalents", "money market"})
# Near-zero cash allocation is the one unambiguous, false-positive-safe read
# available: converting a "months of spend" floor into a percent-of-book
# comparison needs a monthly-spend figure, and decision 1 (§7 of the strategy
# doc) forbids importing expense-line dollar amounts into this repo. Below
# this threshold the check fires; above it, no comparison is attempted rather
# than guessing at a spend rate.
_CASH_NEAR_ZERO_PCT = 2.0


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).replace(tzinfo=None)


def repo_root_from_db_path(db_path: Path | str | None) -> Path | None:
    """``<repo_root>/data/portfolio.db`` -> ``<repo_root>``, or ``None`` when
    the path can't be resolved (no override and no ``db.DB_PATH``) — callers
    already tolerate a ``None`` repo_root by skipping every capacity check
    that needs a repo-relative read (the materialized weights cache)."""
    from db_paths import resolve_db_path

    resolved = resolve_db_path(db_path)
    if resolved is None:
        return None
    return resolved.parent.parent


@dataclass(frozen=True, slots=True)
class _BreachFinding:
    """One CURRENTLY-active capacity_breach condition, as evaluated right now."""

    capacity_kind: str  # "human_capital" | "cash_floor"
    bucket: str  # bucket name, or "cash" for the floor leg
    current_pct: float
    cap_pct: float
    detail: str  # human-readable — feeds both the alert evidence and the ping body


# ---------------------------------------------------------------------------
# capacity_breach — pure(ish) evaluators (conn + already-fetched I/O only)
# ---------------------------------------------------------------------------


def _human_capital_breaches(
    conn: sqlite3.Connection, repo_root: Path | None
) -> list[_BreachFinding]:
    """Human-capital stacking: for each AFFIRMED ``human_capital.<bucket>``
    fact, sum the CURRENT portfolio weight of its member tickers (the SAME
    never-raises materialized-weights cache the positioning/risk panels read
    — never the live tracker on this path) and flag a bucket over its cap.
    ``repo_root=None`` (unresolvable db path) degrades to no findings."""
    if repo_root is None:
        return []
    from portfolio_weights import read_materialized_weights

    try:
        grouped = get_current_profile(conn)
    except Exception as exc:
        log.debug({"event": "human_capital_breach_profile_read_failed", "error": str(exc)})
        return []
    weights = read_materialized_weights(repo_root)  # {} on a missing/unreadable cache
    if not weights:
        return []
    findings: list[_BreachFinding] = []
    for row in grouped.get("capacity", []):
        if not row.key.startswith("human_capital."):
            continue
        try:
            bucket = HumanCapitalBucket.model_validate(row.value)
        except Exception:
            continue
        if not bucket.members:
            continue
        pct = sum(weights.get(t, 0.0) for t in bucket.members) * 100.0
        if pct > bucket.cap_pct:
            bucket_name = row.key.removeprefix("human_capital.")
            findings.append(
                _BreachFinding(
                    capacity_kind="human_capital",
                    bucket=bucket_name,
                    current_pct=pct,
                    cap_pct=bucket.cap_pct,
                    detail=(
                        f"{', '.join(bucket.members)} together are {pct:.1f}% of the book — "
                        f"over your {bucket_name} human-capital cap ({bucket.cap_pct:g}%)"
                    ),
                )
            )
    return findings


def _cash_floor_breach(conn: sqlite3.Connection, *, api_url: str | None) -> _BreachFinding | None:
    """Cash floor: an affirmed ``cash_buffer_months`` policy fact exists AND
    the tracker's own ``by_asset_type`` cash/money-market sleeve reads
    near-zero. See the module-level note on ``_CASH_NEAR_ZERO_PCT`` for why
    this is the narrower, honest version of the check (no spend-rate figure
    crosses the federation boundary to convert "months" into a percent)."""
    try:
        grouped = get_current_profile(conn)
    except Exception as exc:
        log.debug({"event": "cash_floor_profile_read_failed", "error": str(exc)})
        return None
    floor_months: float | None = None
    for row in grouped.get("capacity", []):
        if row.key == "cash_buffer_months":
            months = row.value.get("months")
            if isinstance(months, (int, float)) and not isinstance(months, bool):
                floor_months = float(months)
    if floor_months is None:
        return None
    try:
        from integrations.portfolio_tracker_client import fetch_portfolio_analytics

        analytics = fetch_portfolio_analytics(api_url=api_url, only={"positioning"})
    except Exception as exc:
        log.debug({"event": "cash_floor_tracker_read_failed", "error": str(exc)})
        return None
    if analytics.positioning is None:
        return None
    cash_pct: float | None = None
    for bucket in analytics.positioning.by_asset_type:
        label = (bucket.label or "").strip().lower()
        if label in _CASH_ASSET_LABELS and bucket.weight_pct is not None:
            cash_pct = float(bucket.weight_pct)
            break
    if cash_pct is None or cash_pct > _CASH_NEAR_ZERO_PCT:
        return None
    return _BreachFinding(
        capacity_kind="cash_floor",
        bucket="cash",
        current_pct=cash_pct,
        cap_pct=floor_months,
        detail=(
            f"cash allocation reads ~{cash_pct:.1f}% of the book against your "
            f"{floor_months:g}-month cash-buffer policy — worth a look"
        ),
    )


def _key_evidence(finding: _BreachFinding) -> dict[str, object]:
    return {
        "policy_kind": "owner_capacity",
        "capacity_kind": finding.capacity_kind,
        "bucket": finding.bucket,
    }


# ---------------------------------------------------------------------------
# capacity_breach — alert-episode sync + Moment construction
# ---------------------------------------------------------------------------


def collect_capacity_breach_moments(
    conn: sqlite3.Connection,
    *,
    repo_root: Path | None,
    api_url: str | None = None,
    now: datetime | None = None,
    db_path: Path | str | None = None,
) -> list[Moment]:
    """Evaluate both capacity_breach legs, upsert/self-heal the matching
    ``alerts`` row per breach episode, and return one Moment per CURRENTLY
    breached + CURRENTLY pending alert. Never raises — every leg degrades to
    "no findings" independently (a missing table, a locked DB, an offline
    tracker each just contribute nothing, exactly like every other governor
    collector)."""

    stamp = _now(now)
    findings: list[_BreachFinding] = []
    try:
        findings.extend(_human_capital_breaches(conn, repo_root))
    except Exception as exc:
        log.debug({"event": "capacity_breach_human_capital_failed", "error": str(exc)})
    try:
        cash = _cash_floor_breach(conn, api_url=api_url)
        if cash is not None:
            findings.append(cash)
    except Exception as exc:
        log.debug({"event": "capacity_breach_cash_floor_failed", "error": str(exc)})

    moments: list[Moment] = []
    active_signatures: set[str] = set()
    for finding in findings:
        try:
            moment, sig = _sync_finding_alert(finding, stamp=stamp, db_path=db_path)
        except Exception as exc:
            log.warning({"event": "capacity_breach_alert_sync_failed", "error": str(exc)})
            continue
        active_signatures.add(sig)
        if moment is not None:
            moments.append(moment)

    _self_heal_resolved_alerts(active_signatures, db_path=db_path)
    return moments


def _sync_finding_alert(
    finding: _BreachFinding, *, stamp: datetime, db_path: Path | str | None
) -> tuple[Moment | None, str]:
    from alerts.store import compute_signature_sha, find_by_signature, fire_alert
    from research.governor import Moment

    key_evidence = _key_evidence(finding)
    sig = compute_signature_sha(CAPACITY_BREACH_TRIGGER_KIND, CAPACITY_ALERT_TICKER, key_evidence)
    existing = find_by_signature(signature_sha=sig, db_path=db_path)
    if existing is not None and existing.status == "pending":
        alert = existing
    else:
        evidence_json = json.dumps(
            {
                **key_evidence,
                "current_pct": finding.current_pct,
                "cap_pct": finding.cap_pct,
                "detail": finding.detail,
                # "summary" is the evidence_drawer.py convention (renders under
                # "Why this fired") — kept alongside "detail" (used internally
                # by the ping body / freshness recheck) rather than renaming,
                # so this evidence shape stays self-describing either way.
                "summary": finding.detail,
            },
            sort_keys=True,
        )
        alert = fire_alert(
            ticker=CAPACITY_ALERT_TICKER,
            trigger_kind=CAPACITY_BREACH_TRIGGER_KIND,
            fired_at=stamp,
            evidence_json=evidence_json,
            signature_sha=sig,
            db_path=db_path,
        )
    moment = Moment(
        class_="capacity_breach",
        key=f"alert:{alert.id}",
        ticker=None,
        body=f"Capacity check: {finding.detail}.",
        source_ref=f"alert:{alert.id}",
    )
    return moment, sig


def _self_heal_resolved_alerts(active_signatures: set[str], *, db_path: Path | str | None) -> None:
    """Derive, don't ask: a PENDING capacity_breach alert whose condition no
    longer holds this run gets auto-dismissed — otherwise a resolved breach
    would sit "pending" forever, wrongly keeping its tier-1 alert-feed
    treatment (decisive_alert_reason) alive after the owner has already fixed
    the thing it was warning about."""
    try:
        from alerts.store import dismiss_alert, list_alerts
    except Exception:
        return
    try:
        pending = list_alerts(status="pending", db_path=db_path)
    except Exception as exc:
        log.debug({"event": "capacity_breach_self_heal_list_failed", "error": str(exc)})
        return
    for alert in pending:
        if alert.trigger_kind != CAPACITY_BREACH_TRIGGER_KIND:
            continue
        if alert.signature_sha in active_signatures:
            continue
        try:
            dismiss_alert(alert.id, db_path=db_path, reason="capacity breach resolved (automatic)")
        except Exception as exc:
            log.debug({"event": "capacity_breach_self_heal_dismiss_failed", "error": str(exc)})


def capacity_breach_still_active(
    conn: sqlite3.Connection,
    alert_id: int,
    *,
    repo_root: Path | None,
    api_url: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """The freshness-gate recheck for a ``capacity_breach`` moment: the alert
    must still be pending AND the SAME evaluator that fired it must still say
    the condition holds RIGHT NOW. Can't verify -> False (don't ping) —
    reuses the exact pure evaluators above so "what counts as a breach" can
    never drift between the firing pass and the freshness recheck."""
    from alerts.store import get_alert

    try:
        alert = get_alert(alert_id, db_path=db_path)
    except LookupError:
        return False
    except Exception:
        return False
    if alert.status != "pending":
        return False
    try:
        raw_evidence = json.loads(alert.evidence_json)
    except (ValueError, TypeError):
        return False
    if not isinstance(raw_evidence, dict):
        return False
    # JSON-boundary cast: isinstance(..., dict) just confirmed the runtime
    # shape; this evidence blob is written only by _sync_finding_alert above.
    evidence = cast("dict[str, object]", raw_evidence)
    capacity_kind = evidence.get("capacity_kind")
    bucket = evidence.get("bucket")
    if capacity_kind == "human_capital":
        return any(
            f.capacity_kind == "human_capital" and f.bucket == bucket
            for f in _human_capital_breaches(conn, repo_root)
        )
    if capacity_kind == "cash_floor":
        return _cash_floor_breach(conn, api_url=api_url) is not None
    return False


# ---------------------------------------------------------------------------
# life_event_checkpoint
# ---------------------------------------------------------------------------


def collect_life_event_moments(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[Moment]:
    """An affirmed ``life_event.*`` fact whose date enters the
    :data:`LIFE_EVENT_LOOKAHEAD_DAYS` lookahead window fires ONCE (key = the
    fact's own id) with a capacity-framing line, e.g. "Work break (person_a)
    window approaching — 2027-03-01 — dry-powder posture check." — never a
    date/amount recap, since the owner already wrote the event down."""
    from research.governor import Moment

    stamp = _now(now)
    today = stamp.date()
    horizon = today + timedelta(days=LIFE_EVENT_LOOKAHEAD_DAYS)
    try:
        grouped = get_current_profile(conn)
    except Exception as exc:
        log.debug({"event": "life_event_profile_read_failed", "error": str(exc)})
        return []
    moments: list[Moment] = []
    for row in grouped.get("capacity", []):
        if not row.key.startswith("life_event."):
            continue
        try:
            life = LifeEventFact.model_validate(row.value)
        except Exception:
            continue
        if not (today <= life.date <= horizon):
            continue
        who = f" ({life.person})" if life.person else ""
        moments.append(
            Moment(
                class_="life_event_checkpoint",
                key=f"fact:{row.id}",
                ticker=None,
                body=(
                    f"{life.label}{who} window approaching — {life.date.isoformat()} — "
                    "dry-powder posture check."
                ),
                source_ref=f"fact:{row.id}",
            )
        )
    return moments


# ---------------------------------------------------------------------------
# profile_drift
# ---------------------------------------------------------------------------


def _live_tracked_tax_bucket_total(*, api_url: str | None) -> float | None:
    try:
        from integrations.portfolio_tracker_client import fetch_live_portfolio

        live = fetch_live_portfolio(api_url=api_url)
    except Exception as exc:
        log.debug({"event": "profile_drift_tracker_read_failed", "error": str(exc)})
        return None
    if not live.available:
        return None
    return sum(
        v for k, v in live.by_tax_treatment.items() if k in ("taxable", "tax_deferred", "tax_free")
    )


def collect_profile_drift_moments(
    conn: sqlite3.Connection,
    *,
    repo_root: Path | None = None,
    api_url: str | None = None,
    now: datetime | None = None,
) -> list[Moment]:
    """``profile_drift`` fires as a re-affirmation ASK, never an accusation,
    on two independent legs (either can fire alone):

    1. Any affirmed fact past its review horizon (:func:`owner_profile.store.
       list_expiring_facts` — the SAME predicate the packet walk uses).
    2. An affirmed ``tax_bucket_balances`` fact, stale >2 quarters, whose
       TRACKED-bucket total (pretax/roth/hsa/taxable — see
       ``_TRACKED_TAX_BUCKETS``) diverges >25% from the tracker's live read.

    Keyed by the fact's own id either way: once the owner re-affirms/updates
    (via Update/Rewrite, which supersedes to a NEW fact id), the old key can
    never fire again and any FUTURE drift on the new fact gets its own fresh
    key — no separate re-ask cadence needed here."""
    from research.governor import Moment

    _ = repo_root  # unused today; kept for signature symmetry with the sibling collectors
    stamp = _now(now)
    moments: list[Moment] = []
    try:
        expiring = list_expiring_facts(conn, now=stamp)
    except Exception as exc:
        log.debug({"event": "profile_drift_expiring_read_failed", "error": str(exc)})
        expiring = []
    covered_ids = {row.id for row in expiring}
    for row in expiring:
        moments.append(
            Moment(
                class_="profile_drift",
                key=f"drift:{row.id}",
                ticker=None,
                body=(
                    f'Re-affirmation due: "{row.narrative[:160]}" — still current? '
                    "(a quiet check-in, not an accusation)"
                ),
                source_ref=f"fact:{row.id}",
            )
        )

    try:
        grouped = get_current_profile(conn)
    except Exception as exc:
        log.debug({"event": "profile_drift_profile_read_failed", "error": str(exc)})
        return moments
    for row in grouped.get("capacity", []):
        if row.key != "tax_bucket_balances" or row.id in covered_ids or not row.affirmed_at:
            continue
        try:
            affirmed_at = datetime.fromisoformat(row.affirmed_at)
        except ValueError:
            continue
        if stamp - affirmed_at < timedelta(days=TAX_DRIFT_STALE_DAYS):
            continue
        raw_balances = row.value.get("balances")
        if not isinstance(raw_balances, dict):
            continue
        # JSON-boundary cast: isinstance(..., dict) just confirmed the runtime
        # shape (this is TaxBucketBalances.balances, dict[str, float] on the
        # writer side).
        balances = cast("dict[str, object]", raw_balances)
        affirmed_total = 0.0
        for k, v in balances.items():
            if k not in _TRACKED_TAX_BUCKETS:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            affirmed_total += float(v)
        if affirmed_total <= 0:
            continue
        live_total = _live_tracked_tax_bucket_total(api_url=api_url)
        if live_total is None:
            continue
        divergence = abs(live_total - affirmed_total) / affirmed_total
        if divergence <= TAX_DRIFT_DIVERGENCE:
            continue
        as_of = row.value.get("as_of", "?")
        moments.append(
            Moment(
                class_="profile_drift",
                key=f"drift:{row.id}",
                ticker=None,
                body=(
                    f"Your tax-bucket balances on file (${affirmed_total:,.0f} tracked total, "
                    f"as of {as_of}) read {divergence * 100:.0f}% off the tracker's live "
                    f"balance (${live_total:,.0f}) — worth a re-affirm, not an accusation."
                ),
                source_ref=f"fact:{row.id}",
            )
        )
    return moments
