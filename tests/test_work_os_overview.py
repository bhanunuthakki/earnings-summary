"""Contracts for the retained Overview drill-through fragment."""

from __future__ import annotations

from pipeline.dashboard_status import DashboardRow
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_overview import render_overview_panel


def test_overview_keeps_evaluation_table_without_retired_composites() -> None:
    evaluation = CockpitRow(
        base=DashboardRow(
            ticker="NU",
            list_type="evaluation",
            fmp_last_pulled=None,
            last_transcript=None,
            last_build_at=None,
            open_comments_count=0,
            breach_status=None,
        ),
        attractiveness=1.25,
        fit=1.10,
    )

    html = render_overview_panel({"portfolio": [], "evaluation": [evaluation]}, {})

    # The thin Evaluation table is retained in the legacy drill-through …
    assert "<h2>Evaluation <span class='count'>" in html
    # … but the retired scalar Score/Fit presentations and their peek doorways
    # must not be recoverable through it.
    assert ">Score<" not in html
    assert ">Fit<" not in html
    assert "/api/peek/score" not in html
    assert "/api/peek/fit" not in html
