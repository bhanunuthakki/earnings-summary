"""The monthly Red Team engine (directives/monthly_red_team.md Phase 2, PR5).

First-Saturday adversarial pipeline: one rotating-lens LLM attack per held
name (``lenses.py``) plus three cross-book passes (``cross_book.py``),
orchestrated by ``engine.py`` with per-item degrade (transient LLM failure
defers that item; a hard stop propagates), persisted to ``red_team_items``
(``store.py``) and rendered as one dense Red Team Brief (``brief.py``).

The forced-response loop (REFUTE/ACCEPT/DEFER) is PR6's scope — this package
only generates and persists ``status="open"`` items and renders them
read-only.
"""

from __future__ import annotations
