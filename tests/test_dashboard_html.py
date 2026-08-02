"""Tests for src/pipeline/dashboard_html.py.

The ops-status tables this module used to render were superseded by the
Research cockpit (``pipeline.research_cockpit``, master build P1.2 — covered
by tests/test_research_cockpit.py). What remains here is the Governance →
Actions fragment: the IR-KPI refresh + repo-maintenance blocks with their
SSE job wiring, relocated out of the Overview tab.
"""

from __future__ import annotations

from pipeline.dashboard_html import render_actions_panel


def test_actions_panel_includes_refresh_ir_control():
    """The IR-refresh form, its endpoint POST, and the SSE wiring all render."""
    html = render_actions_panel()
    assert 'id="refresh-ir-form"' in html
    assert 'id="ir-ticker"' in html  # ticker input
    assert 'id="ir-quarters"' in html  # quarters input
    assert "fetch('/actions/refresh-ir'" in html  # POSTs to the endpoint
    assert "new EventSource(r.body.stream_url)" in html  # streams via the job's stream_url
    # Handles each SSE frame the registry emits (start / log / done).
    assert "m.event === 'start'" in html
    assert "m.event === 'log'" in html
    assert "m.event === 'done'" in html


def test_actions_panel_js_escapes_newline_not_raw():
    """Regression: the log-append must emit a JS `\\n` escape. A raw newline in
    the Python source would land inside a single-quoted JS string and break it.
    (PR8 prefixes each line with the [m:ss] elapsed stamp.)"""
    html = render_actions_panel()
    assert "outputEl.textContent += elapsed() + line + '\\n'" in html  # two-char escape
    assert "+ line + '\n'" not in html  # never a raw newline


def test_actions_panel_includes_maintenance_controls():
    """The repo-wide maintenance block POSTs its chores to /actions/maintenance."""
    html = render_actions_panel()
    assert "Maintenance" in html
    assert 'data-action="seed_kpis"' in html
    assert 'data-action="process_inbox"' in html
    assert 'data-action="sweep_history"' in html
    assert 'data-action="onboard_pending"' in html
    assert "/actions/maintenance" in html


def test_actions_panel_includes_cio_export_link():
    """Task 5 (wave3b): the CIO workbook export used to be reachable only
    from the command palette (goUrl('/export/cio')) — a plain kit link in
    the Maintenance block gives it a visible doorway. Not a `.maint-btn`
    (that class is wired to the maintenance-job click delegator, which would
    misfire a bogus job start for a plain download link)."""
    html = render_actions_panel()
    assert 'href="/export/cio"' in html
    assert 'class="k-btn k-btn-quiet k-btn-sm"' in html


def test_actions_panel_is_self_contained():
    """Both blocks carry their own <style> + <script> so the fragment works
    when lazily injected by the shell (which re-executes script tags)."""
    html = render_actions_panel()
    assert html.count("<script>") == 2
    assert html.count("<style>") == 2
