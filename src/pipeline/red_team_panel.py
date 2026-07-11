"""Portfolio -> Red Team panel: composes ``redteam.store`` (DB read) with
``redteam.brief`` (pure render) for the command-center shell
(``/api/panel/red_team``, monthly_red_team.md Phase 2, PR5).

No CSS of its own — ``redteam.brief`` owns the ``<style>`` block, so this
module never needs a ``REGISTERED`` entry in ``tests/test_ui_controls.py``.
"""

from __future__ import annotations

from pathlib import Path

from redteam import store
from redteam.brief import render_red_team_brief


def render_red_team_panel(*, db_path: Path) -> str:
    """The Red Team Brief for the most recent run on file. An empty brief
    (no runs yet) renders the quiet empty state — never an error."""
    run_key = store.latest_run_key(db_path=db_path)
    items = store.list_items_for_run(db_path=db_path, run_key=run_key) if run_key else []
    return render_red_team_brief(items, run_key=run_key)
