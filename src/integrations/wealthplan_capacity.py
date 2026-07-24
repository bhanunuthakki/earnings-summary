"""Wealthplan capacity reader — the Phase-2 repair item named in
``docs/design/owner_context_federation.md`` §3.2 ("Not yet read from the
tracker" — actually from wealthplan) and §4 delivery seam 1 of
``docs/design/tenet2_advisory_program.md``.

Computes a BAND-LEVEL near-term cash-need summary only: over the next ~24
months, is the household's cash-need posture ``"normal"`` or
``"elevated"`` (with label-only reasons, e.g. "baby window", "work break")?
This is a RUNTIME READER, not an importer — unlike
``execution/import_owner_capacity.py`` it never writes a fact to
``owner_profile_facts``; the ``/review`` capacity block calls it directly on
every review and degrades to ``available=False`` on any failure.

Same exclusion discipline as the Tier-A importer (owner decision 1, §7 of
the strategy doc): amounts may exist TRANSIENTLY in-memory while this module
reads wealthplan's own Pydantic models (``ExpensePlan``, life events, etc.),
but no dollar figure is ever returned, logged, or persisted — only the band
label and label-only reasons cross this boundary. Reuses the same
``sys.path`` sibling-import pattern as the importer (a wealthplan checkout
this script can borrow models from, without depending on it as an installed
package).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

_DEFAULT_WEALTHPLAN_ROOT = Path(r"C:\Users\Bhanu\.gemini\antigravity\scratch\wealthplan")

# ~24 months — matches the position-review capacity block's own life-event
# lookahead window (advisor.position_review.CAPACITY_HORIZON_DAYS) so a "baby
# window" that shows up in one shows up in the other.
DEFAULT_LOOKAHEAD_DAYS = 730

CashNeedBand = Literal["normal", "elevated"]

# Label-only reasons an "elevated" band cites — no amounts, ever. Keyed by the
# wealthplan event class name (checked via isinstance, not this dict) so a
# reader of this module can see the full vocabulary in one place.
_EVENT_LABELS: dict[str, str] = {
    "BabyEvent": "baby window",
    "BuyHouseEvent": "home purchase",
    "MoveCityEvent": "relocation",
    "WorkBreakEvent": "work break",
    "StartupEvent": "startup transition",
    "ExitPayoutEvent": "liquidity event",
}
_EVENT_DATE_ATTR: dict[str, str] = {
    "BabyEvent": "birth_date",
    "BuyHouseEvent": "purchase_date",
    "MoveCityEvent": "move_date",
    "WorkBreakEvent": "start_date",
    "StartupEvent": "start_date",
    "ExitPayoutEvent": "payout_date",
}


@dataclass(frozen=True, slots=True)
class WealthplanCashNeedSummary:
    """Band-level near-term cash-need read. ``reasons`` are label-only (no
    amounts, no dates beyond what already lives in ``owner_profile_facts``'
    dated life events) — this object is safe to log, persist, or quote back
    to the owner verbatim."""

    available: bool
    band: CashNeedBand | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None  # degrade reason, set only when available=False


def read_cash_need_summary(
    *,
    wealthplan_root: Path = _DEFAULT_WEALTHPLAN_ROOT,
    as_of: date | None = None,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
) -> WealthplanCashNeedSummary:
    """Never raises. Returns ``available=False`` (+ a reason string) on a
    missing wealthplan checkout, a missing/unparseable plan file, or any
    error while scanning events — the caller (the capacity block) simply
    omits its line.

    Amounts may pass through wealthplan's OWN model objects transiently while
    this function runs (e.g. an ``ExpenseCategory.annual`` field exists on
    the loaded object), but nothing here ever reads, logs, or returns one —
    only event TYPE + whether its date falls in the lookahead window.
    """
    src = str(wealthplan_root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from wealthplan.models import (
            BabyEvent,
            BuyHouseEvent,
            ExitPayoutEvent,
            MoveCityEvent,
            ParentCareEvent,
            StartupEvent,
            WorkBreakEvent,
        )
        from wealthplan.persistence import load_plan
    except ImportError as exc:
        return WealthplanCashNeedSummary(
            available=False, reason=f"wealthplan import failed: {type(exc).__name__}"
        )
    # sys.modules caching can satisfy the import with a module from a DIFFERENT
    # root (the real sibling, once anything imported it) — this reader would
    # then answer from the wrong root's plan. File-less injected fakes (the
    # test seam) are exempt. Order-dependent full-suite failure, 2026-07-24.
    mod = sys.modules.get("wealthplan")
    mod_file = getattr(mod, "__file__", None) if mod is not None else None
    if mod_file is not None:
        try:
            in_root = Path(mod_file).resolve().is_relative_to((wealthplan_root / "src").resolve())
        except OSError:
            in_root = False
        if not in_root:
            return WealthplanCashNeedSummary(
                available=False, reason="wealthplan import resolved outside the requested root"
            )

    try:
        plan = load_plan()
    except Exception as exc:  # never raises across this federation boundary
        return WealthplanCashNeedSummary(
            available=False, reason=f"load_plan failed: {type(exc).__name__}"
        )
    if plan is None:
        return WealthplanCashNeedSummary(available=False, reason="no plan.local.json on disk")

    _household, baseline = plan
    today = as_of or date.today()
    horizon = today + timedelta(days=lookahead_days)

    _kind_to_label = {
        BabyEvent: _EVENT_LABELS["BabyEvent"],
        BuyHouseEvent: _EVENT_LABELS["BuyHouseEvent"],
        MoveCityEvent: _EVENT_LABELS["MoveCityEvent"],
        WorkBreakEvent: _EVENT_LABELS["WorkBreakEvent"],
        StartupEvent: _EVENT_LABELS["StartupEvent"],
        ExitPayoutEvent: _EVENT_LABELS["ExitPayoutEvent"],
    }
    _kind_to_date_attr = {
        BabyEvent: _EVENT_DATE_ATTR["BabyEvent"],
        BuyHouseEvent: _EVENT_DATE_ATTR["BuyHouseEvent"],
        MoveCityEvent: _EVENT_DATE_ATTR["MoveCityEvent"],
        WorkBreakEvent: _EVENT_DATE_ATTR["WorkBreakEvent"],
        StartupEvent: _EVENT_DATE_ATTR["StartupEvent"],
        ExitPayoutEvent: _EVENT_DATE_ATTR["ExitPayoutEvent"],
    }

    reasons: list[str] = []
    try:
        for event in baseline.events:
            if isinstance(event, ParentCareEvent):
                continue  # age-keyed, not date-keyed — no calendar horizon to compare
            kind = type(event)
            label = _kind_to_label.get(kind)
            attr = _kind_to_date_attr.get(kind)
            if label is None or attr is None:
                continue  # an event type this reader doesn't yet recognize
            event_date = getattr(event, attr, None)
            if not isinstance(event_date, date):
                continue
            if today <= event_date <= horizon and label not in reasons:
                reasons.append(label)
    except Exception as exc:  # never raises — a shape surprise degrades quietly
        return WealthplanCashNeedSummary(
            available=False, reason=f"event scan failed: {type(exc).__name__}"
        )

    if reasons:
        return WealthplanCashNeedSummary(available=True, band="elevated", reasons=tuple(reasons))
    return WealthplanCashNeedSummary(available=True, band="normal")


__all__ = [
    "DEFAULT_LOOKAHEAD_DAYS",
    "CashNeedBand",
    "WealthplanCashNeedSummary",
    "read_cash_need_summary",
]
