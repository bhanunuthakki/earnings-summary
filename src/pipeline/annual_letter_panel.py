"""Annual letter-to-self — the smallest honest surface (monthly_red_team.md
Phase 3, PR7). ``execution/draft_annual_letter.py`` writes the deliverable to
``data/annual_letters/<year>.md``; this module just finds the latest one and
renders it through the shared prose boundary. No new CSS/tokens — it composes
entirely from existing kit classes (``.panel`` / ``.prose``), so it needs no
entry in ``tests/test_ui_controls.py``'s REGISTERED set.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from ui.controls import panel_toolbar
from ui.prose import render_prose

_DIR_NAME = "annual_letters"


def latest_letter_path(repo_root: Path | str) -> Path | None:
    """The most recent ``data/annual_letters/<year>.md`` by filename (years
    sort lexicographically), or None when none has been drafted yet."""
    out_dir = Path(repo_root) / "data" / _DIR_NAME
    if not out_dir.is_dir():
        return None
    files = sorted(out_dir.glob("*.md"))
    return files[-1] if files else None


def render_annual_letter_section(repo_root: Path | str) -> str:
    """Always renders (never "") — an undrafted letter is an honest state, not
    a silent absence, matching the scorecard panel's REQ-6 posture."""
    toolbar = panel_toolbar("Letter to self")
    path = latest_letter_path(repo_root)
    if path is None:
        return (
            '<section class="panel">'
            f"{toolbar}"
            '<p class="muted">No annual letter drafted yet — run '
            "<code>execution/draft_annual_letter.py</code> each January to draft the prior "
            "year's letter-to-self from the ledger, position changes, Red Team responses, "
            "and calibration/Brier trajectory.</p></section>"
        )
    try:
        body_md = path.read_text(encoding="utf-8")
    except OSError:
        return (
            '<section class="panel">'
            f"{toolbar}"
            f'<p class="muted">Failed to read {escape(path.name)} — see logs.</p></section>'
        )
    return f'<section class="panel">{toolbar}<div class="prose">{render_prose(body_md)}</div></section>'


__all__ = ["latest_letter_path", "render_annual_letter_section"]
