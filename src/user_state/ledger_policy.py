"""Canonical admission policy for thesis-ledger records.

The same rule protects future writes and projects historical append-only rows.
Raw rows remain in ``thesis_ledger_entries`` for audit and recovery. Working
research state such as earnings questions, and entries whose source alert kind
is ineligible, are absent from every default ledger read.
"""

from __future__ import annotations

from typing import Final

INELIGIBLE_LEDGER_ALERT_KINDS: Final[tuple[str, ...]] = ("material_news",)
INELIGIBLE_LEDGER_ENTRY_KINDS: Final[tuple[str, ...]] = ("earnings_prep_append",)
INELIGIBLE_LEDGER_ALERT_ENTRY_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("earnings_tone", "thesis_update"),
    ("earnings_tone", "bear_append"),
)


def is_ledger_source_eligible(trigger_kind: str, entry_kind: str) -> bool:
    """Whether an alert/entry pair may create or remain in the active ledger."""
    if entry_kind in INELIGIBLE_LEDGER_ENTRY_KINDS:
        return False
    if trigger_kind in INELIGIBLE_LEDGER_ALERT_KINDS:
        return False
    return (trigger_kind, entry_kind) not in INELIGIBLE_LEDGER_ALERT_ENTRY_PAIRS
