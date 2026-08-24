"""Read-only composition for the unified Performance & Risk destination.

The page owns destination composition only.  Performance, posture, allocation,
and risk calculations remain in their established modules so their provenance
and degradation behavior cannot drift between surfaces.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from html import escape
from pathlib import Path

from integrations.portfolio_allocation import (
    PortfolioAllocationBucket,
    PortfolioAllocationBuckets,
    PortfolioAllocationProjection,
    fetch_portfolio_allocation,
)
from pipeline.allocation_recommendation_panel import render_portfolio_posture_section
from pipeline.portfolio_panel import render_portfolio_panel
from pipeline.portfolio_styles import portfolio_css

_TABS: tuple[tuple[str, str, str], ...] = (
    ("exposure", "Exposure", "exposure"),
    ("drawdown", "Drawdown", "drawdown"),
    ("correlation", "Correlation", "correlation"),
    ("tail", "Tail", "tail"),
)

_TABS_JS = """
<script>
(function () {
  function load(panel) {
    if (!panel || panel.dataset.loaded === '1') return;
    panel.dataset.loaded = '1';
    fetch(panel.dataset.src).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.text();
    }).then(function (html) {
      panel.innerHTML = html;
      var scripts = panel.querySelectorAll('script');
      for (var i = 0; i < scripts.length; i++) {
        var old = scripts[i]; var script = document.createElement('script');
        if (old.src) script.src = old.src; else script.textContent = old.textContent;
        old.parentNode.replaceChild(script, old);
      }
    }).catch(function () {
      panel.dataset.loaded = '';
      panel.innerHTML = '<p class="muted" role="status">Risk view unavailable — select the tab to retry.</p>';
    });
  }
  function activate(root, key, focus) {
    var tabs = root.querySelectorAll('[data-pr-tab]');
    var panels = root.querySelectorAll('[data-pr-panel]');
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].dataset.prTab === key;
      tabs[i].setAttribute('aria-selected', active ? 'true' : 'false');
      tabs[i].tabIndex = active ? 0 : -1;
      if (active && focus) tabs[i].focus();
    }
    for (var j = 0; j < panels.length; j++) {
      var shown = panels[j].dataset.prPanel === key;
      panels[j].hidden = !shown;
      if (shown) load(panels[j]);
    }
  }
  var roots = document.querySelectorAll('[data-performance-risk-tabs]');
  for (var r = 0; r < roots.length; r++) {
    (function (root) {
      root.addEventListener('click', function (event) {
        var tab = event.target.closest('[data-pr-tab]');
        if (tab) activate(root, tab.dataset.prTab, false);
      });
      root.addEventListener('keydown', function (event) {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-pr-tab]'));
        var index = tabs.indexOf(document.activeElement);
        if (index < 0) return;
        event.preventDefault();
        if (event.key === 'Home') index = 0;
        else if (event.key === 'End') index = tabs.length - 1;
        else index = (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        activate(root, tabs[index].dataset.prTab, true);
      });
      activate(root, 'correlation', false);
    })(roots[r]);
  }
})();
</script>
""".strip()


def _percent(bucket: PortfolioAllocationBucket) -> str:
    if bucket.weight_pct is None:
        return "—"
    return f"{bucket.weight_pct:.1f}%"


def _row(label: str, bucket: PortfolioAllocationBucket) -> str:
    width = max(Decimal(0), min(Decimal(100), bucket.weight_pct or Decimal(0)))
    return (
        '<div class="pr-allocation-row">'
        f'<span>{escape(label)}</span><span class="pr-allocation-track" aria-hidden="true">'
        f'<span style="width:{width}%"></span></span>'
        f"<strong>{_percent(bucket)}</strong></div>"
    )


def render_allocation_card(allocation: PortfolioAllocationProjection) -> str:
    """Render typed allocation truth without silently filling unavailable data."""
    head = '<section class="panel pr-allocation"><h2>Portfolio Allocation</h2>'
    if allocation.state == "unavailable":
        reasons = ", ".join(allocation.reason_codes) or "allocation source unavailable"
        return f'{head}<p class="muted">Allocation unavailable — {escape(reasons)}.</p></section>'
    b: PortfolioAllocationBuckets = allocation.buckets
    us_etf = b.us_etf
    international_etf = b.international_etf
    us_equity = b.us_equity
    international_equity = b.international_equity
    cash = b.cash
    unclassified = b.unclassified
    rows = "".join(
        (
            _row("US ETF", us_etf),
            _row("Intl ETF", international_etf),
            _row("US Equity", us_equity),
            _row("Intl Equity", international_equity),
            _row("Cash", cash),
            _row("Unclassified", unclassified),
        )
    )
    status = "Incomplete classification" if allocation.state == "incomplete" else "Current"
    as_of = allocation.as_of.isoformat() if allocation.as_of else "observation date unavailable"
    reconciliation = "reconciled" if allocation.reconciliation.is_reconciled else "not reconciled"
    return (
        f'{head}<p class="sub">{escape(status)} · {escape(allocation.source_identity)} · '
        f"{escape(as_of)} · {escape(reconciliation)}.</p>"
        f'<div class="pr-allocation-rows">{rows}</div></section>'
    )


def _risk_explorer() -> str:
    tabs = "".join(
        f'<button type="button" class="k-chip k-chip-btn k-chip-tab" role="tab" '
        f'id="pr-tab-{key}" aria-controls="pr-panel-{key}" data-pr-tab="{key}" '
        f'aria-selected="{str(key == "correlation").lower()}" tabindex="{0 if key == "correlation" else -1}">{label}</button>'
        for key, label, _fragment in _TABS
    )
    panels = "".join(
        f'<div id="pr-panel-{key}" role="tabpanel" aria-labelledby="pr-tab-{key}" '
        f'data-pr-panel="{key}" data-src="/api/panel/performance_risk?fragment={fragment}"'
        f'{"" if key == "correlation" else " hidden"}><p class="muted" role="status">Loading…</p></div>'
        for key, _label, fragment in _TABS
    )
    return (
        '<section class="panel pr-risk" data-performance-risk-tabs><h2>Risk Explorer</h2>'
        '<div class="k-chip-tabs" role="tablist" aria-label="Risk Explorer views">'
        f"{tabs}</div>{panels}</section>{_TABS_JS}"
    )


def render_performance_risk_panel(
    db_path: Path,
    repo_root: Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    include_backfill: bool = False,
    allocation: PortfolioAllocationProjection | None = None,
    performance_renderer: Callable[[], str] | None = None,
) -> str:
    """Compose the approved read-only destination over canonical renderers."""
    performance = (
        performance_renderer
        or (
            lambda: render_portfolio_panel(
                start_date=start_date,
                end_date=end_date,
                include_backfill=include_backfill,
                db_path=db_path,
                performance_title="Index Benchmarking",
                include_position_drivers=False,
                refresh_endpoint="/api/panel/performance_risk",
                refresh_target_selector="#workOsPerformanceMount",
            )
        )
    )()
    allocation_card = render_allocation_card(
        allocation if allocation is not None else fetch_portfolio_allocation()
    )
    posture = render_portfolio_posture_section(db_path, repo_root, include_actions=False)
    policy_note = (
        '<p class="muted pr-policy-note">Policy mix remains read-only until the tracker exposes '
        "an authorized revisioned write and fresh benchmark receipt.</p>"
    )
    return (
        portfolio_css()
        + '<div class="performance-risk-panel">'
        + performance
        + policy_note
        + '<div class="pr-secondary-grid">'
        + posture
        + allocation_card
        + "</div>"
        + _risk_explorer()
        + "</div>"
    )


__all__ = ["render_allocation_card", "render_performance_risk_panel"]
