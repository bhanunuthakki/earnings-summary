"""Renderers surface "forgone due to budget" (PR2): a brief-level header rollup
plus a distinct per-section banner, across all 3 brief renderers (markdown /
html / workspace). Tests the new render helpers directly, matching the repo's
render-helper test convention.
"""

from __future__ import annotations

from io import StringIO

from report.models import BudgetSkip, MissingReason, SectionStatus
from report.renderers import html as html_r
from report.renderers import markdown as md_r
from report.renderers import workspace_html as ws_r


def _skips() -> list[BudgetSkip]:
    return [
        BudgetSkip(
            section="Bear case (§7)",
            purpose="bear_case",
            cap_usd=50.0,
            spend_usd=61.0,
            headroom_pct=-0.22,
        ),
        BudgetSkip(
            section="Recent developments (§9)",
            purpose="recent_developments",
            cap_usd=20.0,
            spend_usd=25.0,
            headroom_pct=-0.25,
        ),
    ]


def _budget_missing() -> MissingReason:
    return MissingReason(
        stage="BUDGET(bear_case)",
        fix_command="python execution/manage_llm_budget.py --set bear_case --cap <higher>",
        detail=(
            "Forgone to stay under budget — bear_case is at/over its $50 monthly "
            "cap ($61 spent this month, ticker NU)."
        ),
    )


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def test_markdown_forgone_rollup() -> None:
    out = StringIO()
    md_r._forgone_rollup(out, _skips())
    s = out.getvalue()
    assert "2 analyses forgone to stay under budget" in s
    assert "Bear case (§7)" in s and "Recent developments (§9)" in s


def test_markdown_forgone_rollup_empty() -> None:
    out = StringIO()
    md_r._forgone_rollup(out, [])
    assert out.getvalue() == ""


def test_markdown_missing_block_budget_is_distinct() -> None:
    out = StringIO()
    short = md_r._missing_block(out, SectionStatus.BUDGET_SKIPPED, _budget_missing())
    s = out.getvalue()
    assert short is True  # short-circuits the section body
    assert "Forgone to stay under budget" in s
    assert "Override" in s
    assert "Pending stage" not in s  # NOT the generic pipeline-gap banner


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------


def test_html_forgone_callout() -> None:
    out = StringIO()
    html_r._forgone_callout(out, _skips())
    s = out.getvalue()
    assert "2 analyses forgone to stay under budget" in s
    assert "Bear case (§7)" in s


def test_html_forgone_callout_empty() -> None:
    out = StringIO()
    html_r._forgone_callout(out, [])
    assert out.getvalue() == ""


def test_html_missing_callout_budget_is_distinct() -> None:
    out = StringIO()
    short = html_r._missing_callout(out, SectionStatus.BUDGET_SKIPPED, _budget_missing())
    s = out.getvalue()
    assert short is True
    assert "Forgone to stay under budget" in s
    assert "Override" in s
    assert "Pending stage" not in s


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def test_workspace_forgone_strip() -> None:
    body = StringIO()
    ws_r._forgone_strip(body, _skips())
    s = body.getvalue()
    assert "2 analyses forgone to stay under budget" in s
    assert "Bear case (§7)" in s


def test_workspace_forgone_strip_empty() -> None:
    body = StringIO()
    ws_r._forgone_strip(body, [])
    assert body.getvalue() == ""


def test_workspace_missing_panel_budget_is_distinct() -> None:
    body = StringIO()
    ws_r._missing_panel(body, SectionStatus.BUDGET_SKIPPED, _budget_missing())
    s = body.getvalue()
    assert "Forgone — budget" in s  # distinct panel title
    assert "Forgone to stay under budget" in s  # the detail
