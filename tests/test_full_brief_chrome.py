"""Hermetic unit and integration tests for Full Brief chrome, masthead, decision band, and status tones (BHA-70)."""

from __future__ import annotations

import inspect
from datetime import datetime

from bs4 import BeautifulSoup

from pipeline.work_os_research import render_brief_reader_shell
from pipeline.work_os_shell import render_work_os_shell
from report.legacy_body import extract_legacy_reader_body
from report.renderers.workspace_sections.chrome import _verdict_badge


def test_verdict_badge_renders_semantic_pill_classes() -> None:
    """_verdict_badge must emit semantic .k-pill classes (ok, warn, bad, neutral)."""
    # 1. Fresh intact -> k-pill-ok
    fresh = _verdict_badge("intact", datetime(2026, 5, 20, 12, 0, 0), "Q1 2026")
    assert "k-pill" in fresh
    assert "k-pill-ok" in fresh
    assert "Thesis Intact" in fresh
    assert "as of 05-20" in fresh

    # 2. Watch -> k-pill-warn
    watch = _verdict_badge("watch", datetime(2026, 5, 20, 12, 0, 0), "Q1 2026")
    assert "k-pill-warn" in watch
    assert "Watch" in watch

    # 3. Broken -> k-pill-bad
    broken = _verdict_badge("broken", datetime(2026, 5, 20, 12, 0, 0), "Q1 2026")
    assert "k-pill-bad" in broken
    assert "Broken" in broken

    # 4. Stale -> neutral base pill with a muted status dot
    stale = _verdict_badge("intact", datetime(2025, 9, 15, 0, 0, 0), "Q1 2026")
    assert 'class="k-pill"' in stale
    assert "k-dot-muted" in stale
    assert "predates the Q1 2026 print" in stale

    # 5. Pending / un-evaluated -> neutral base pill
    pending = _verdict_badge("pending", None, "Q1 2026")
    assert 'class="k-pill"' in pending
    assert "k-dot-muted" in pending
    assert "Pending" in pending


def test_shared_body_sanitization_preserves_semantic_status_classes() -> None:
    """When inline styles are stripped during shared-body extraction, semantic .k-pill-ok classes must survive."""
    badge_html = _verdict_badge("intact", datetime(2026, 5, 20, 12, 0, 0), "Q1 2026")
    mock_source = f'<div class="l1-root"><div class="company-row">{badge_html}</div></div>'

    extracted = extract_legacy_reader_body(mock_source, artifact_id="test_meli_brief")
    soup = BeautifulSoup(extracted.body_html, "html.parser")
    pill = soup.select_one(".k-pill")
    assert pill is not None
    classes = pill.get("class")
    assert isinstance(classes, list)
    assert "k-pill" in classes
    assert "k-pill-ok" in classes
    assert "Thesis Intact" in pill.get_text()


def test_brief_reader_shell_and_decision_band_markup() -> None:
    """Brief reader shell must contain non-corrupted em-dashes and structured decision elements."""
    html = render_brief_reader_shell()
    assert "workOsBriefReaderDecision" in html
    assert "workOsBriefOwnerState" in html
    assert "workOsBriefModelState" in html
    assert "workOsBriefDecisionRelationship" in html
    assert "â€”" not in html
    assert "—" in html


def test_full_brief_has_a_live_research_items_band_outside_the_persisted_body() -> None:
    html = render_brief_reader_shell()
    assert 'id="workOsBriefResearchItemsMount"' in html
    assert html.index("workOsBriefResearchItemsMount") < html.index("workOsBriefReaderBody")

    shell = render_work_os_shell()
    assert "function workOsLoadBriefResearchItems(ticker)" in shell
    assert "items=1&band=brief&ticker=" in shell
    assert shell.count("void workOsLoadBriefResearchItems(artifact.ticker)") == 1


def test_full_brief_research_items_band_has_reachable_archive_restore_and_retry_chrome() -> None:
    shell = render_work_os_shell()

    assert "workOsBriefResearchItemsMount" in shell
    assert "items=1&band=brief&ticker=" in shell
    # The mounted Full Brief band owns the live controls; its renderer owns
    # the archived filter, restore action, and non-OK retry handling.
    from pipeline import journal_panel

    source = inspect.getsource(journal_panel.render_research_items_band)
    assert 'data-rib-status="archived"' in source
    assert 'data-rib-retry' in source
    assert "response.status === 409" in source


def test_work_os_shell_css_decision_band_rules() -> None:
    """Work OS shell CSS must define auto height and zero overflow for .work-os-reader-decision."""
    shell_html = render_work_os_shell()
    assert ".work-os-reader-decision" in shell_html
    assert "overflow: visible" in shell_html
    assert "flex-shrink: 0" in shell_html
    assert "workOsFormatDecisionDate(state.revision)" in shell_html
    assert "state.revision + asOf" not in shell_html
