"""Tests for src/pipeline/dashboard_html.py.

Verifies structural elements of the rendered overview body: section headers,
table rows per ticker, breach badges, comment counts, transcript Q&A markers,
plus the IR-refresh + maintenance action blocks. Style / CSS specifics are not
asserted — those can drift freely.

``render_dashboard_html`` (the old standalone two-table page + ``_PAGE_TEMPLATE``)
was retired when ``GET /`` became the unified command-center shell; its content
now ships as the shell's inlined Overview tab via ``render_status_overview``,
which is what these tests cover.
"""

from __future__ import annotations

from pipeline.dashboard_html import render_status_overview
from pipeline.dashboard_status import DashboardRow, TranscriptStatus


def _row(
    ticker: str,
    list_type: str = "portfolio",
    *,
    fmp_last_pulled: str | None = "2026-05-11T01:02:14",
    last_transcript: TranscriptStatus | None = TranscriptStatus(
        period_end="2026-03-31", has_qa_section=True, call_date=None
    ),
    last_build_at: str | None = "2026-05-18T11:00:00+00:00",
    open_comments_count: int = 0,
    breach_status: str | None = "intact",
) -> DashboardRow:
    return DashboardRow(
        ticker=ticker,
        list_type=list_type,
        fmp_last_pulled=fmp_last_pulled,
        last_transcript=last_transcript,
        last_build_at=last_build_at,
        open_comments_count=open_comments_count,
        breach_status=breach_status,
    )


def test_render_includes_both_sections():
    html = render_status_overview(
        {"portfolio": [_row("NU")], "evaluation": [_row("MELI", "evaluation")]}
    )
    assert "Portfolio" in html
    assert "Evaluation" in html
    assert "<td class='ticker'><a href='/ticker/NU'>NU</a></td>" in html
    assert "<td class='ticker'><a href='/ticker/MELI'>MELI</a></td>" in html


def test_render_shows_section_counts():
    rows = {"portfolio": [_row("NU"), _row("GOOG")], "evaluation": [_row("MELI", "evaluation")]}
    html = render_status_overview(rows)
    assert 'Portfolio <span class="count">(2)</span>' in html
    assert 'Evaluation <span class="count">(1)</span>' in html


def test_render_empty_sections_show_friendly_message():
    html = render_status_overview({"portfolio": [], "evaluation": []})
    assert "No portfolio tickers." in html
    assert "No evaluation tickers." in html


def test_render_breach_badge_uses_status_color():
    html = render_status_overview(
        {"portfolio": [_row("NU", breach_status="watch")], "evaluation": []}
    )
    assert "breach-badge" in html
    assert ">watch<" in html  # the visible label, between badge tags


def test_render_unknown_breach_status_renders_with_neutral_color():
    html = render_status_overview(
        {"portfolio": [_row("NU", breach_status="experimental_state")], "evaluation": []}
    )
    assert ">experimental_state<" in html


def test_render_open_comments_count_shown_when_nonzero():
    html = render_status_overview(
        {"portfolio": [_row("NU", open_comments_count=3)], "evaluation": []}
    )
    assert "3 open comments" in html


def test_render_open_comments_singular_for_one():
    html = render_status_overview(
        {"portfolio": [_row("NU", open_comments_count=1)], "evaluation": []}
    )
    assert "1 open comment<" in html  # exactly singular, no trailing 's'


def test_render_zero_comments_shows_em_dash():
    html = render_status_overview(
        {"portfolio": [_row("NU", open_comments_count=0)], "evaluation": []}
    )
    # The NU row's comments cell shows the muted em-dash, never "X open comments".
    assert "muted" in html
    assert "open comment" not in html


def test_render_transcript_qa_marker_present_when_has_qa():
    html = render_status_overview(
        {
            "portfolio": [
                _row(
                    "NU",
                    last_transcript=TranscriptStatus(
                        period_end="2026-03-31", has_qa_section=True, call_date=None
                    ),
                )
            ],
            "evaluation": [],
        }
    )
    assert "qa-yes" in html
    assert "2026-03-31" in html


def test_render_transcript_no_qa_marker_when_prepared_only():
    html = render_status_overview(
        {
            "portfolio": [
                _row(
                    "NU",
                    last_transcript=TranscriptStatus(
                        period_end="2026-03-31", has_qa_section=False, call_date=None
                    ),
                )
            ],
            "evaluation": [],
        }
    )
    assert "qa-no" in html


def test_render_open_link_only_when_workspace_html_exists():
    rows = {
        "portfolio": [
            _row("NU", last_build_at="2026-05-18T11:00:00+00:00"),
            _row("GOOG", last_build_at=None),
        ],
        "evaluation": [],
    }
    html = render_status_overview(rows)
    assert "href='/reports/NU'" in html
    # GOOG has no build; its open cell should be muted, not a link
    assert "href='/reports/GOOG'" not in html


def test_render_escapes_ticker_in_links():
    """A ticker with HTML-special chars must not break the page."""
    row = _row("BRK-B")
    html = render_status_overview({"portfolio": [row], "evaluation": []})
    assert "BRK-B" in html
    # Ticker cell links to the drill-down; the ticker is escaped into both the
    # href and the label (BRK-B has no HTML-special chars, so it's identity).
    assert "<td class='ticker'><a href='/ticker/BRK-B'>BRK-B</a></td>" in html


# ----- Action blocks: IR-refresh + maintenance (rendered above the tables) -----


def test_render_includes_refresh_ir_control():
    """The IR-refresh form, its endpoint POST, and the SSE wiring all render —
    even with no tickers (the control is page-level, not per-row)."""
    html = render_status_overview({"portfolio": [], "evaluation": []})
    assert 'id="refresh-ir-form"' in html
    assert 'id="ir-ticker"' in html  # ticker input
    assert 'id="ir-quarters"' in html  # quarters input
    assert "fetch('/actions/refresh-ir'" in html  # POSTs to the endpoint
    assert "new EventSource(r.body.stream_url)" in html  # streams via the job's stream_url
    # Handles each SSE frame the registry emits (start / log / done).
    assert "m.event === 'start'" in html
    assert "m.event === 'log'" in html
    assert "m.event === 'done'" in html


def test_render_ir_control_js_escapes_newline_not_raw():
    """Regression: the log-append must emit a JS `\\n` escape. A raw newline in
    the Python source would land inside a single-quoted JS string and break it."""
    html = render_status_overview({"portfolio": [], "evaluation": []})
    assert "outputEl.textContent += line + '\\n'" in html  # two-char escape
    assert "outputEl.textContent += line + '\n'" not in html  # never a raw newline


def test_render_includes_maintenance_controls():
    """The repo-wide maintenance panel renders above the status tables and POSTs
    its chores to /actions/maintenance."""
    html = render_status_overview({"portfolio": [], "evaluation": []})
    assert "Maintenance" in html
    assert 'data-action="seed_kpis"' in html
    assert "/actions/maintenance" in html
