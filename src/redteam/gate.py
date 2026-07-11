"""Month-close gate + escalation query (PR6 — monthly_red_team.md Phase 2:
"The month is not closed until all items are answered").

Precise contract (task spec): a month is CLOSED when zero items for its
``run_key`` have status ``open`` or ``deferred`` — i.e. ``open`` items are
"not yet responded", and ``deferred`` items count as UNRESOLVED for the
close (a defer buys time, not a close). Only ``refuted`` / ``accepted`` /
``closed`` items count as answered.

Escalation is a separate, cross-run concern: any item currently sitting in
``status='deferred'`` has already used its one allowed defer
(``redteam.response`` rejects a second) and is therefore overdue for a real
answer. That set is read across ALL run_keys, not just the latest one, so
the Home-band banner (``pipeline.open_loops``) stays truthful even after a
new month's run starts and a prior month's deferred item is still sitting
unanswered.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from redteam import store
from redteam.models import RedTeamItemRow

UNRESOLVED_STATUSES: tuple[str, ...] = ("open", "deferred")
ESCALATED_STATUSES: tuple[str, ...] = ("deferred",)


@dataclass(slots=True, frozen=True)
class MonthStatus:
    """The Red Team panel's month-status header pill contract: ``run_key``
    (``None`` when no run exists yet), how many items are still unresolved,
    and whether the month counts as closed."""

    run_key: str | None
    unresolved_count: int
    is_closed: bool


def month_status(*, db_path: Path | str | None, run_key: str | None) -> MonthStatus:
    """CLOSED iff every item for ``run_key`` is refuted/accepted/closed (or
    the run has no items / doesn't exist yet — nothing to answer reads as
    closed, matching the panel's empty-state)."""
    if run_key is None:
        return MonthStatus(run_key=None, unresolved_count=0, is_closed=True)
    items = store.list_items_for_run(db_path=db_path, run_key=run_key)
    unresolved = [i for i in items if i.status in UNRESOLVED_STATUSES]
    return MonthStatus(
        run_key=run_key, unresolved_count=len(unresolved), is_closed=(len(unresolved) == 0)
    )


def escalated_items(*, db_path: Path | str | None) -> list[RedTeamItemRow]:
    """Every item, across every run, still sitting ``deferred`` — the
    persistent nag for the Home band. ``[]`` on a missing DB/table (never
    raises — matches every other ``redteam.store`` read)."""
    return store.list_items_by_status(db_path=db_path, statuses=ESCALATED_STATUSES)


__all__ = [
    "ESCALATED_STATUSES",
    "UNRESOLVED_STATUSES",
    "MonthStatus",
    "escalated_items",
    "month_status",
]
