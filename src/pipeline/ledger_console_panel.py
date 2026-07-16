"""Review → Ledger composite console (Phase-5 IA: Review 3 sub-tabs → 1).

The Review section's three panels — Ledger (musings feed), Triage, and Journal —
are all lenses over the same ``analyst_notes`` spine. Per the owner's
aggressive-redesign verdict they collapse into ONE Ledger page that COMPOSES the
three builders behind an anchor-nav band — the S10 Provenance-console pattern
(``pipeline/console_scaffold.py``).

The composite REUSES the ``musings`` panel id (the Ledger feed leads, so the
landing keeps its id); ``triage`` / ``journal`` alias to it via
``command_center_shell._LEGACY_PANEL_REDIRECTS``. Each builder's own
``/api/panel/<id>`` route stays live — the composite's own ``?fragment=…``
sub-routes (list / onmymind / research / reconcile / worldview) keep flowing
through the ``musings`` route to the Ledger builder, and the Journal/Triage
routes still serve their in-panel refresh fragments. No new CSS.
"""

from __future__ import annotations

from pathlib import Path

from identity import DEFAULT_USER_ID
from pipeline.console_scaffold import ConsoleSection, render_console


def render_ledger_console(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Review → Ledger: the capture-and-disposition console. Composes the Ledger
    capture feed (landing), the parked-comment Triage queue, and the analyst
    Journal lifecycle — three lenses over ``analyst_notes`` on one page."""
    from pipeline.journal_panel import render_journal_panel
    from pipeline.ledger_panel import render_ledger_panel
    from pipeline.triage_panel import render_triage_panel

    sections: list[ConsoleSection] = [
        ("feed", "Ledger", lambda: render_ledger_panel(db_path, user_id=user_id)),
        ("triage", "Triage", lambda: render_triage_panel(db_path, user_id=user_id)),
        ("journal", "Journal", lambda: render_journal_panel(db_path, user_id=user_id)),
    ]
    return render_console("Ledger", sections, wrap_class="ledger-console")
