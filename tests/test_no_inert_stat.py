"""``no-inert-stat`` guard — every cockpit stat with a deeper view is a doorway.

The Instrument Paradigm's Law 2 ("every datum is a doorway", directive
``interaction_paradigm_2026_06.md`` §1/§3-row-4/§4) forbids an inert ``<span>``
whose only depth is a ``title=`` tooltip when the datum HAS a deeper view: such
a stat must be an ``<a>``/``<button>`` carrying exactly ONE shell-handled action
attribute (``data-ask-q`` / ``data-peek-url`` / ``data-fact-ref``).

``research_cockpit._render_row`` is a pure string function, so — exactly like
the ``no-raw-hex`` guard over rendered CSS — this guard renders a fully
populated cockpit and scans the OUTPUT. It enforces the four stats the paradigm
defines as doorways:

* the tier-1 KPI chip  → ``data-ask-q`` (relative-window phrasing — see below),
* the "N new docs" pill → ``data-peek-url`` (``/api/peek/documents``),
* the pending-alerts pill → ``data-peek-url`` (``/api/peek/alerts``, pre-S9),
* the ops-freshness dot → ``data-peek-url`` (``/api/peek/provenance``, pre-S9).

Out of scope (documented so the boundary is explicit, not a silent cap): the
attractiveness score and verdict badge carry self-contained explanatory math in
a tooltip with no separate surface to open, and the open-comments pill has no
per-ticker peek route — none is a *buried doorway*, so a tooltip there is a
legitimate supplement, not a Law-2 violation.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.dashboard_status import DashboardRow, TranscriptStatus  # noqa: E402
from pipeline.research_cockpit import CockpitRow, KpiDelta, render_research_cockpit  # noqa: E402

# The shell-handled action attributes (command_center_shell delegates).
_ACTION_ATTRS = ("data-ask-q", "data-peek-url", "data-fact-ref")
# Cockpit stats the paradigm defines as doorways — each must be a non-span
# doorway with exactly one action attr.
_DOORWAY_CLASSES = ("kpi-chip", "pill-accent", "pill-bad", "stale-dot")


class _DoorwayScan(HTMLParser):
    """Collect ``(marker_class, tag, [action_attrs])`` for every element whose
    class is one of the paradigm's doorway markers."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        classes = a.get("class", "").split()
        marker = next((c for c in _DOORWAY_CLASSES if c in classes), None)
        if marker is None:
            return
        self.found.append((marker, tag, [k for k in _ACTION_ATTRS if k in a]))


def _populated_cockpit() -> str:
    """One portfolio row carrying every doorway stat at once."""
    base = DashboardRow(
        ticker="NU",
        list_type="portfolio",
        fmp_last_pulled="2026-06-12T10:00:00",
        last_transcript=TranscriptStatus("2026-03-31", True, "2026-04-25"),
        last_build_at="2026-06-10T00:00:00",
        open_comments_count=1,  # the open-comments pill (deliberately NOT a doorway)
        breach_status="warn",
    )
    row = CockpitRow(
        base=base,
        name="Nu Holdings Ltd.",
        price=12.29,
        fv_gap_pct=-50.0,
        peg_ratio=0.5,
        next_earnings="2026-07-15",
        pending_alerts=2,
        new_docs=3,
        kpi_deltas=[
            KpiDelta("Monthly ARPAC (USD)", "USD", 11.2, 10.1, "2026-03-31", "2025-12-31", "ok"),
            KpiDelta("Net margin (percent)", "%", 18.0, 19.5, "2026-03-31", "2025-12-31", "warn"),
        ],
        rule_summary="warn: Net margin (percent)",
        evaluated_at="2026-06-10T00:00:00",
    )
    return render_research_cockpit({"portfolio": [row], "evaluation": []})


def test_cockpit_stats_are_doorways_not_inert_spans() -> None:
    """Every paradigm doorway stat is an ``<a>``/``<button>`` with exactly one
    action attr — never an inert ``<span>`` whose only depth is a tooltip."""
    scan = _DoorwayScan()
    scan.feed(_populated_cockpit())
    seen = {marker for marker, _tag, _actions in scan.found}
    # All four doorway stats actually rendered (else the guard tests nothing).
    assert seen == set(_DOORWAY_CLASSES), f"missing doorway stats: {set(_DOORWAY_CLASSES) - seen}"
    for marker, tag, actions in scan.found:
        assert tag in ("a", "button"), f"{marker} is an inert <{tag}>, not a doorway"
        assert len(actions) == 1, f"{marker} must carry exactly one action attr, got {actions}"


def test_kpi_chip_uses_relative_window_phrasing() -> None:
    """The KPI chip's ``data-ask-q`` phrases the window as a period COUNT, never
    an ISO date range — the ViewSpec compiler parses counts (nl_compile), so an
    ISO range compiles to an empty chart. The period dates stay in ``title=``."""
    html = _populated_cockpit()
    # The chip carries a relative window and names the ticker.
    assert "data-ask-q='Monthly ARPAC for NU, last 12 quarters'" in html
    # No ISO date (YYYY-MM-DD) ever leaks into an ask-q payload.
    for m in re.finditer(r"data-ask-q='([^']*)'", html):
        assert not re.search(r"\d{4}-\d{2}-\d{2}", m.group(1)), f"ISO date in ask-q: {m.group(1)!r}"
    # The period dates DO live in the chip title (the supplement).
    assert "2025-12-31 &rarr; 2026-03-31" in html or "2025-12-31 → 2026-03-31" in html


def test_new_docs_pill_peeks_documents() -> None:
    """The "N new docs" pill opens ``/api/peek/documents`` in place; the count
    is no longer a dead end."""
    html = _populated_cockpit()
    assert "data-peek-url='/api/peek/documents?ticker=NU'" in html
    # Specifically NOT the pre-S9 inert span.
    assert not re.search(r"<span class='pill pill-accent'[^>]*>\s*\d+ new doc", html)


def test_ticker_cell_is_in_shell_link() -> None:
    """The ticker cell is an ``<a>`` the shell delegate routes to ``#holding=``
    (``td.ticker a``) — an in-shell drill-down, not a hard navigation."""
    html = _populated_cockpit()
    assert re.search(r"<td class='ticker'[^>]*><a href='/ticker/NU'>NU</a>", html)
