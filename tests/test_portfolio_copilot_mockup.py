"""Structural contract for the isolated Portfolio Copilot page mockup."""

from __future__ import annotations

import re
from pathlib import Path

MOCKUP_PATH = Path(__file__).resolve().parents[1] / "mockups" / "portfolio_copilot_mockup.html"


def _mockup() -> str:
    return MOCKUP_PATH.read_text(encoding="utf-8")


def test_mockup_is_isolated_from_the_production_work_os_source() -> None:
    renderer = (
        Path(__file__).resolve().parents[1] / "src" / "pipeline" / "work_os_shell.py"
    ).read_text(encoding="utf-8")

    assert MOCKUP_PATH.exists()
    assert "portfolio_copilot_mockup.html" not in renderer
    assert '"mockups" / "harvey_sidebar_flow.html"' in renderer


def test_topline_has_nav_and_compact_actions_without_performance_or_exposures() -> None:
    html = _mockup()
    topline = html.split('data-testid="portfolio-topline"', 1)[1].split(
        'data-testid="portfolio-section"', 1
    )[0]

    assert topline.count('data-testid="nav-card"') == 1
    assert topline.count('data-testid="actions-rail"') == 1
    assert "Portfolio NAV" in topline
    assert "Portfolio Companies" not in topline
    nav = topline.split('data-testid="nav-card"', 1)[1].split("</article>", 1)[0]
    allocation_rows = re.findall(
        r'<div class="stat-subtext allocation-row" data-allocation-pct="([^"]+)">([^<]+)</div>',
        nav,
    )
    assert allocation_rows == [
        ("18", "18% Domestic ETF"),
        ("7", "7% Intl ETF"),
        ("41", "41% Domestic Equity"),
        ("20", "20% Intl Equity"),
    ]
    assert sum(int(value) for value, _ in allocation_rows) + 14 == 100
    assert "Performance" not in topline
    assert "data-period" not in html
    assert "performancePeriods" not in html
    assert "Key Exposures" not in html
    assert "exposure-grid" not in html
    assert "grid-template-columns: minmax(0, 0.75fr) minmax(0, 2.25fr)" in html
    assert "@media (max-width: 1000px)" in html
    assert ".portfolio-topline { grid-template-columns: minmax(0, 1fr); }" in html


def test_redundant_page_heading_is_removed_but_refresh_remains_in_header() -> None:
    html = _mockup()

    assert '<div class="page-heading">' not in html
    assert '<p class="eyebrow">Portfolio Intelligence</p>' not in html
    assert '<header class="app-header">' in html
    assert "Workspaces / <strong>Portfolio Copilot</strong>" in html
    assert ">Refresh</button>" in html


def test_portfolio_table_has_exact_labels_sort_controls_and_integer_prices() -> None:
    html = _mockup()
    table = html.split('data-testid="portfolio-table"', 1)[1].split("</table>", 1)[0]

    assert "Portfolio at a Glance" in html
    assert re.findall(r'<span class="sort-label">([^<]+)</span>', table) == [
        "Company",
        "Weight",
        "Price/Target",
        "Status",
        "Key Links",
    ]
    assert table.count('class="sort-button k-btn k-btn-quiet k-btn-sm"') == 5
    assert table.count('class="sort-icon"') == 5
    assert "sort-glyphs" not in table
    assert "↑" in table and "↓" not in table
    for column in ("weight", "price-target", "status"):
        assert f'class="col-{column}"' in table
        assert f'class="cell-{column}' in table
    assert not re.search(r"\$[\d,]+\.\d+", table)


def test_threshold_bands_and_key_links_are_visible_per_company() -> None:
    html = _mockup()
    table = html.split('data-testid="portfolio-table"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr data-company-row[^>]*>(.*?)</tr>", table, re.DOTALL)

    assert len(rows) == 4
    for row in rows:
        assert "Add below" in row
        assert "Hold till" in row
        assert "Sell at" in row
        for label in ("DCF", "Brief", "Pre-Earnings", "Post-Earnings"):
            assert f">{label}</button>" in row


def test_actions_have_all_quick_responses_and_notification_side_panel() -> None:
    html = _mockup()
    actions = html.split('data-testid="actions-rail"', 1)[1].split(
        'data-testid="portfolio-section"', 1
    )[0]
    cards = re.findall(r'<article class="k-well action-card".*?</article>', actions, re.DOTALL)

    assert ">Actions<" in actions
    assert "Completing an item clears it from this session" not in html
    assert len(cards) == 3
    assert "Threshold watch" in cards[0]
    assert ">Proposed<" in cards[0]
    assert "Add below $13" in cards[0]
    for card in cards:
        for action in ("Dismiss", "Approve", "Snooze", "Open notification"):
            assert f">{action}</button>" in card

    assert 'id="notificationPanel"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert "openNotificationPanel" in html
    assert "closeNotificationPanel" in html
    assert "notificationApprove" in html
    assert "notificationSnooze" in html
    assert "notificationDismiss" in html
    assert "event.key === 'Escape'" in html

    assert (
        ".actions-rail { min-inline-size: 0; padding: var(--sp-2); display: grid; "
        "gap: var(--sp-1); }" in html
    )
    assert ".action-list { display: grid; gap: var(--sp-1); }" in html
    assert ".k-well.action-card { padding: var(--sp-1) var(--sp-2); overflow: hidden; }" in html
    assert ".action-copy p { margin: 0;" in html
    assert ".action-copy .ticker-tile { min-inline-size: var(--icon-button-size);" in html
    assert ".action-controls .k-btn { min-block-size: var(--touch-target); }" in html


def test_evaluation_dialogues_are_compact_grounded_and_follow_the_portfolio_table() -> None:
    html = _mockup()
    table_position = html.index('data-testid="portfolio-section"')
    dialogue_position = html.index('data-testid="evaluation-dialogue"')
    dialogue = html[dialogue_position : html.index("</section>", dialogue_position)]

    assert dialogue_position > table_position
    assert "Evaluation dialogues" in dialogue
    assert "Recent owner dialogue and ready-to-discuss workups" in dialogue
    assert "not the full evaluation list" in dialogue
    assert dialogue.count('data-testid="evaluation-thread"') == 3
    for ticker in ("TOST", "QCOM", "AVDV"):
        assert f'data-ticker="{ticker}"' in dialogue
    assert ">Stock<" in dialogue
    assert ">ETF<" in dialogue
    assert dialogue.count(">Continue dialogue</button>") == 2
    assert dialogue.count(">Start dialogue</button>") == 1
    assert dialogue.count(">Open workup</button>") == 3
    assert dialogue.count(">Compare</button>") == 3
    assert "Partial workup" in dialogue
    assert "holdings snapshot Feb 28" in dialogue
    assert "Full evaluation list" not in dialogue


def test_new_controls_use_the_existing_kit_and_mockup_prices_are_integer_formatted() -> None:
    html = _mockup()

    assert "k-btn k-btn-primary" in html
    assert "k-btn k-btn-quiet" in html
    assert "k-chip" in html
    assert "k-pill" in html
    assert "k-well" in html
    assert "function formatPrice(value)" in html
    assert "maximumFractionDigits: 0" in html
    assert "minimumFractionDigits: 0" in html
