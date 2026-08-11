"""Review → Ledger composite console (Phase-5 IA: Review 3 sub-tabs → 1).

The Review section's three panels — Ledger (musings feed), Triage, and Journal —
are all lenses over the same ``analyst_notes`` spine. Per the owner's
aggressive-redesign verdict they collapse into ONE Ledger page that COMPOSES the
three builders behind an anchor-nav band — the S10 Provenance-console pattern
(``pipeline/console_scaffold.py``).

The composite REUSES the ``musings`` panel id (the Ledger feed leads, so the
landing keeps its id); ``triage`` / ``journal`` alias to it via
the Work OS deep-link map. Each builder's own
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
    Journal lifecycle — three lenses over ``analyst_notes`` on one page.

    ONE merged nav band (Phase-5 verifier: double-chrome): the feed renders
    ``embedded`` (its internal chip toolbar suppressed) and contributes its six
    jump chips to the console band via ``extra_nav`` — ``data-ledger-jump``
    behavior included, since the chips ship with their own nav listener. The
    feed's own "Ledger" chip is dropped from the band (``nav_exclude``): its
    ``<h2>Ledger</h2>`` sits directly below; Triage + Journal + Decisions chips
    stay. Decisions (P2.1, PRD §9.3) is a FOURTH section — a separate
    ``v_decision_journal`` reader, not the ``analyst_notes`` lifecycle Journal
    above it."""
    from pipeline.decision_journal_panel import render_decision_journal_panel
    from pipeline.journal_panel import render_journal_panel
    from pipeline.ledger_panel import render_ledger_jump_chips, render_ledger_panel
    from pipeline.triage_panel import render_triage_panel

    sections: list[ConsoleSection] = [
        ("feed", "Ledger", lambda: render_ledger_panel(db_path, user_id=user_id, embedded=True)),
        # Triage/Journal/Decisions render embedded too: their tab-level chrome
        # (a panel_toolbar / an <h2>) collapses to a section h3 — the band's
        # chip names the section, and a second tab title under it was the
        # remaining sliver of the double-chrome defect.
        ("triage", "Triage", lambda: render_triage_panel(db_path, user_id=user_id, embedded=True)),
        (
            "journal",
            "Journal",
            lambda: render_journal_panel(db_path, user_id=user_id, embedded=True),
        ),
        (
            "decisions",
            "Decisions",
            lambda: render_decision_journal_panel(db_path, embedded=True),
        ),
    ]
    return render_console(
        "Ledger",
        sections,
        wrap_class="ledger-console",
        extra_nav=render_ledger_jump_chips(db_path),
        nav_exclude=("feed",),
    )
