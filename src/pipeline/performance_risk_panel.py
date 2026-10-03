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
from urllib.parse import urlencode

from integrations.portfolio_allocation import (
    PortfolioAllocationBucket,
    PortfolioAllocationBuckets,
    PortfolioAllocationProjection,
    fetch_portfolio_allocation,
)
from integrations.portfolio_tracker_client import PolicyMix, PortfolioAnalytics
from pipeline.console_scaffold import render_console
from pipeline.portfolio_decision_support import render_portfolio_posture_section
from pipeline.portfolio_panel import render_portfolio_panel
from pipeline.portfolio_styles import portfolio_css

_POLICY_TICKERS: tuple[str, ...] = ("QQQ", "SGOV", "VTI", "VWO")

_TABS: tuple[tuple[str, str, str], ...] = (
    ("exposure", "Exposure", "exposure"),
    ("drawdown", "Drawdown", "drawdown"),
    ("correlation", "Correlation", "correlation"),
    ("tail", "Tail", "tail"),
)

_TABS_JS = """
<script>
(function () {
  var reads = new WeakMap();
  function load(panel) {
    if (!panel || panel.dataset.loaded === '1') return;
    panel.dataset.loaded = '1';
    var controller = new AbortController();
    reads.set(panel, controller);
    var timer = window.setTimeout(function () { controller.abort(); }, 45000);
    panel.setAttribute('aria-busy', 'true');
    (window.uiFetch || fetch)(panel.dataset.src, {signal: controller.signal, timeoutMs: 45000}).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.text();
    }).then(function (html) {
      if (!panel.isConnected || reads.get(panel) !== controller || panel.hidden) return;
      panel.innerHTML = html;
      var scripts = panel.querySelectorAll('script');
      for (var i = 0; i < scripts.length; i++) {
        var old = scripts[i]; var script = document.createElement('script');
        if (old.src) script.src = old.src; else script.textContent = old.textContent;
        old.parentNode.replaceChild(script, old);
      }
    }).catch(function () {
      if (reads.get(panel) !== controller || !panel.isConnected) return;
      panel.dataset.loaded = '';
      panel.innerHTML = '<p class="muted" role="status">Risk view unavailable — select the tab to retry.</p>';
    }).finally(function () {
      window.clearTimeout(timer);
      if (reads.get(panel) === controller) {
        reads.delete(panel);
        panel.removeAttribute('aria-busy');
      }
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
      if (!shown && reads.has(panels[j])) {
        reads.get(panels[j]).abort(); reads.delete(panels[j]);
        panels[j].dataset.loaded = ''; panels[j].removeAttribute('aria-busy');
      }
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

_POLICY_EDITOR_JS = """
<script>
(function () {
  function wire(root) {
    if (!root || root.dataset.wired === '1') return;
    root.dataset.wired = '1';
    var form = root.querySelector('[data-policy-form]');
    var status = root.querySelector('[data-policy-status]');
    if (!form || !status) return;
    function say(message, tone) {
      status.textContent = message;
      status.dataset.tone = tone || 'neutral';
    }
    var refreshController = null;
    var refreshVersion = 0;
    var receiptTimer = null;
    function visible() {
      return root.isConnected && !document.hidden && !root.closest('[hidden], [aria-hidden="true"]');
    }
    var wasHidden = !visible();
    function stopHiddenRead() {
      var hidden = !visible();
      var resumed = wasHidden && !hidden;
      wasHidden = hidden;
      if (hidden && receiptTimer) {
        window.clearTimeout(receiptTimer); receiptTimer = null;
      }
      if (hidden && refreshController) {
        refreshVersion += 1;
        refreshController.abort(); refreshController = null;
      }
      if (!root.isConnected) {
        if (receiptTimer) window.clearTimeout(receiptTimer);
        lifecycle.disconnect(); document.removeEventListener('visibilitychange', stopHiddenRead);
      }
      if (resumed) checkReceipt();
    }
    var lifecycle = new MutationObserver(stopHiddenRead);
    lifecycle.observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'aria-hidden']});
    document.addEventListener('visibilitychange', stopHiddenRead);
    function refreshPanel() {
      if (!visible()) return;
      var section = root.closest('[data-console-endpoint]');
      var target = section || root.closest('#workOsPerformanceMount') || root.parentElement;
      if (!target) { say('Fresh policy read is ready; refresh this panel to display it.', 'error'); return; }
      var endpoint = section ? section.dataset.consoleEndpoint : '/api/panel/performance_risk';
      var version = ++refreshVersion;
      if (refreshController) refreshController.abort();
      var controller = new AbortController(); refreshController = controller;
      var timer = window.setTimeout(function () { controller.abort(); }, 45000);
      (window.uiFetch || fetch)(endpoint, {signal: controller.signal, timeoutMs: 45000}).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
      }).then(function (html) {
        if (version !== refreshVersion || !visible()) return;
        target.innerHTML = html;
      var scripts = target.querySelectorAll('script');
      for (var i = 0; i < scripts.length; i++) {
        var old = scripts[i]; var script = document.createElement('script');
        if (old.src) script.src = old.src; else script.textContent = old.textContent;
        old.parentNode.replaceChild(script, old);
      }
      }).catch(function () {
        if (version !== refreshVersion || !visible()) return;
        say('Could not complete the fresh policy read. The confirmed chart and mix are unchanged.', 'error');
      }).finally(function () {
        window.clearTimeout(timer);
        if (refreshController === controller) refreshController = null;
      });
    }
    function checkReceipt() {
      var raw = window.sessionStorage.getItem('bha79-policy-receipt');
      if (!raw) return;
      var receipt;
      try { receipt = JSON.parse(raw); } catch (_) { window.sessionStorage.removeItem('bha79-policy-receipt'); return; }
      var revision = Number(root.dataset.policyRevision);
      if (root.dataset.recomputationStatus === 'current' && Number.isFinite(revision) && revision >= receipt.revision) {
        window.sessionStorage.removeItem('bha79-policy-receipt');
        say('Policy mix is current at revision ' + revision + '.', 'success');
        return;
      }
      if (receipt.attempts >= 8) {
        say('Receipt accepted; benchmark recomputation is still pending. Refresh the panel to check again.', 'pending');
        return;
      }
      receipt.attempts += 1;
      window.sessionStorage.setItem('bha79-policy-receipt', JSON.stringify(receipt));
      say('Receipt accepted; benchmark recomputation pending. Checking the fresh panel…', 'pending');
      receiptTimer = window.setTimeout(function () { receiptTimer = null; refreshPanel(); }, 1250);
    }
    checkReceipt();
    if (form.dataset.writeReady !== '1') return;
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var inputs = form.querySelectorAll('input[data-policy-ticker]');
      var weights = []; var total = 0; var firstInvalid = null;
      for (var i = 0; i < inputs.length; i++) {
        var input = inputs[i]; var value = Number(input.value);
        input.removeAttribute('aria-invalid');
        if (!Number.isFinite(value) || value < 0 || value > 100) {
          input.setAttribute('aria-invalid', 'true'); if (!firstInvalid) firstInvalid = input; continue;
        }
        weights.push({ticker: input.dataset.policyTicker, weight_pct: value, notes: null}); total += value;
      }
      if (firstInvalid) { say('Enter a finite weight from 0 to 100 for each fund.', 'error'); firstInvalid.focus(); return; }
      if (Math.abs(total - 100) > 0.01) {
        say('Policy weights must total 100.00%.', 'error'); inputs[0].focus(); return;
      }
      var apply = form.querySelector('button[type=submit]');
      apply.disabled = true; say('Applying policy mix…', 'pending');
      var key = 'policy:' + Date.now() + ':' + Math.random().toString(36).slice(2);
      fetch('/api/portfolio/policy', {
        method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Portfolio-Write-Intent': 'replace-policy'},
        body: JSON.stringify({weights: weights, expected_revision: Number(form.dataset.revision), idempotency_key: key, source: 'earnings_summary', as_of: new Date().toISOString()})
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (response.status === 200 || response.status === 202) {
            var confirmedRevision = Number(body && body.policy && body.policy.revision);
            if (!Number.isInteger(confirmedRevision) || confirmedRevision <= Number(form.dataset.revision)) {
              say('Tracker returned no newer confirmed revision. The displayed mix is unchanged.', 'error');
              apply.disabled = false; return;
            }
            window.sessionStorage.setItem('bha79-policy-receipt', JSON.stringify({revision: confirmedRevision, attempts: 0}));
            refreshPanel(); return;
          }
          if (response.status === 409) say('Policy revision changed elsewhere. Refresh the panel before applying again.', 'error');
          else if (response.status === 403) say('Policy write is unauthorized. The confirmed mix is unchanged.', 'error');
          else if (response.status === 503) say(body.error === 'recomputation_failure' ? 'Policy recomputation failed. The confirmed mix is unchanged.' : 'Tracker is offline. The confirmed mix is unchanged.', 'error');
          else if (response.status === 422 || response.status === 400) say('Tracker rejected this mix. Check the values and try again.', 'error');
          else say('Policy update failed. The confirmed mix is unchanged.', 'error');
          apply.disabled = false;
        });
      }).catch(function () { say('Tracker is offline. The confirmed mix is unchanged.', 'error'); apply.disabled = false; });
    });
  }
  var editors = document.querySelectorAll('[data-policy-editor]');
  for (var i = 0; i < editors.length; i++) wire(editors[i]);
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


def _policy_editor(policy: PolicyMix | None) -> str:
    """Render only provider-confirmed policy weights; never invent a mix."""
    if policy is None:
        return (
            '<section class="panel pr-policy-editor" data-policy-editor><h2>Policy mix</h2>'
            '<p class="muted">Policy mix unavailable from the tracker.</p></section>'
        )
    confirmed = {weight.ticker.upper(): weight for weight in policy.weights}
    supported = all(
        ticker in confirmed and confirmed[ticker].weight_pct is not None
        for ticker in _POLICY_TICKERS
    ) and set(confirmed) == set(_POLICY_TICKERS)
    ready = policy.write_ready and policy.is_balanced and supported
    reason = (
        "Policy metadata is not current; Apply mix stays disabled."
        if not policy.write_ready
        else "This tracker policy cannot be safely edited from the approved four-fund mix."
        if not supported
        else "Policy weights are not balanced; Apply mix stays disabled."
        if not policy.is_balanced
        else ""
    )
    rows = "".join(
        '<label class="pr-policy-row"><span>{ticker}</span>'
        '<input type="number" inputmode="decimal" min="0" max="100" step="0.01" '
        'data-policy-ticker="{ticker}" aria-label="{ticker} target weight" value="{weight}"></label>'.format(
            ticker=ticker,
            weight=escape(f"{confirmed[ticker].weight_pct:.2f}")
            if ticker in confirmed and confirmed[ticker].weight_pct is not None
            else "",
        )
        for ticker in _POLICY_TICKERS
    )
    revision = "" if policy.revision is None else str(policy.revision)
    recomputation = policy.recomputation_status or "unavailable"
    disabled = "" if ready else " disabled"
    status = "Policy mix is current." if ready else reason
    return (
        '<section class="panel pr-policy-editor" data-policy-editor '
        f'data-policy-revision="{escape(revision, quote=True)}" '
        f'data-recomputation-status="{escape(recomputation, quote=True)}"><h2>Policy mix</h2>'
        '<p class="sub">Provider-confirmed targets only · revision '
        f"{escape(revision or 'unavailable')} · recomputation {escape(recomputation)}.</p>"
        f'<form data-policy-form data-write-ready="{str(ready).lower()}" data-revision="{escape(revision, quote=True)}">'
        f'<div class="pr-policy-grid">{rows}</div>'
        f'<div class="pr-policy-actions"><button type="submit" class="k-btn k-btn-primary"{disabled}>Apply mix</button>'
        f'<p class="pr-policy-status" data-policy-status role="status">{escape(status)}</p></div></form></section>'
        + _POLICY_EDITOR_JS
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
    policy: PolicyMix | None = None,
    lazy: bool = False,
) -> str:
    """Compose Performance & Risk with one shared tracker policy read."""
    if lazy:
        window = {
            key: value
            for key, value in {
                "start_date": start_date,
                "end_date": end_date,
                "include_backfill": "1" if include_backfill else None,
            }.items()
            if value is not None
        }
        anchors = (
            ("performance", "Index Benchmarking"),
            ("posture", "Portfolio Posture"),
            ("allocation", "Portfolio Allocation"),
        )
        return (
            portfolio_css()
            + render_console(
                "Performance & Risk",
                [(key, label, lambda: "") for key, label in anchors],
                wrap_class="performance-risk-panel",
                deferred={
                    key: "/api/panel/performance_risk?" + urlencode({"fragment": key, **window})
                    for key, _label in anchors
                },
            )
            + _risk_explorer()
        )
    captured: dict[str, PolicyMix | None] = {"policy": None}
    if performance_renderer is not None:
        performance = performance_renderer()
    else:

        def capture(analytics: PortfolioAnalytics) -> None:
            captured["policy"] = analytics.policy

        performance = render_portfolio_panel(
            start_date=start_date,
            end_date=end_date,
            include_backfill=include_backfill,
            db_path=db_path,
            performance_title="Index Benchmarking",
            include_position_drivers=False,
            refresh_endpoint="/api/panel/performance_risk",
            refresh_target_selector="#workOsPerformanceMount",
            analytics_observer=capture,
        )
    allocation_card = render_allocation_card(
        allocation if allocation is not None else fetch_portfolio_allocation()
    )

    posture = render_portfolio_posture_section(db_path, repo_root, include_actions=False)
    policy_editor = _policy_editor(policy if policy is not None else captured["policy"])
    return (
        portfolio_css()
        + '<div class="performance-risk-panel">'
        + performance
        + policy_editor
        + '<div class="pr-secondary-grid">'
        + posture
        + allocation_card
        + "</div>"
        + _risk_explorer()
        + "</div>"
    )


def render_performance_risk_fragment(
    db_path: Path,
    repo_root: Path,
    fragment: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    include_backfill: bool = False,
) -> str:
    """Render independent performance sections through their existing owners."""
    if fragment == "allocation":
        return portfolio_css() + render_allocation_card(fetch_portfolio_allocation())
    if fragment == "posture":
        return portfolio_css() + render_portfolio_posture_section(
            db_path, repo_root, include_actions=False
        )
    if fragment != "performance":
        raise ValueError("Unknown performance fragment")
    captured: dict[str, PolicyMix | None] = {"policy": None}

    def capture(analytics: PortfolioAnalytics) -> None:
        captured["policy"] = analytics.policy

    performance = render_portfolio_panel(
        start_date=start_date,
        end_date=end_date,
        include_backfill=include_backfill,
        db_path=db_path,
        performance_title="Index Benchmarking",
        include_position_drivers=False,
        refresh_endpoint="/api/panel/performance_risk?fragment=performance",
        refresh_target_selector='[data-console-endpoint*="fragment=performance"]',
        analytics_observer=capture,
    )
    return portfolio_css() + performance + _policy_editor(captured["policy"])


__all__ = ["render_allocation_card", "render_performance_risk_panel"]
