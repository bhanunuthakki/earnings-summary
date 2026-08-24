"""Composite-console scaffold — sticky chip-tab nav band (owner directive
2026-08-02: "the chips need to be sticky … instead accentuate with a line or
shade or color, something that doesn't take up space").

``render_console`` is the shared assembler for the Portfolio composites
(Health/Allocation/Record) and the Ledger console. These pin: the nav band
opts into ``panel_toolbar(sticky=True)`` (the ``.k-toolbar-sticky`` kit
modifier), its jump chips carry the ``.k-chip-tab`` underline-active
modifier, and none of that disturbs the ``data-console-jump`` /
``_CONSOLE_NAV_JS`` scroll-jump contract other tests (test_ledger_panel,
test_portfolio_console_lazy) already pin.
"""

from __future__ import annotations

from pipeline.console_scaffold import ConsoleSection, render_console


def _sections() -> list[ConsoleSection]:
    return [
        ("alpha", "Alpha", lambda: '<section class="panel"><p>alpha body</p></section>'),
        ("beta", "Beta", lambda: '<section class="panel"><p>beta body</p></section>'),
    ]


def test_nav_band_is_sticky() -> None:
    html = render_console("Demo", _sections(), wrap_class="demo-console")
    assert 'class="k-toolbar k-toolbar-sticky"' in html


def test_nav_chips_carry_the_tab_modifier() -> None:
    html = render_console("Demo", _sections(), wrap_class="demo-console")
    assert 'class="k-chip k-chip-btn k-chip-tab" data-console-jump="csec-alpha"' in html
    assert 'class="k-chip k-chip-btn k-chip-tab" data-console-jump="csec-beta"' in html


def test_jump_hooks_and_nav_js_survive_sticky() -> None:
    """The sticky restyle must not disturb the scroll-jump contract: the data
    attribute (never an href, which would trip the shell's hash router) and
    the guarded document-level listener both still ship."""
    html = render_console("Demo", _sections(), wrap_class="demo-console")
    assert 'data-console-jump="csec-alpha"' in html
    assert 'data-console-jump="csec-beta"' in html
    assert 'href="#csec-' not in html
    assert "window.__ccConsoleNav" in html
    assert "scrollIntoView" in html
    assert 'id="csec-alpha"' in html and 'id="csec-beta"' in html


def test_extra_nav_and_excluded_anchor_still_compose_inside_the_sticky_band() -> None:
    """``extra_nav`` (the Ledger feed's own chips) leads the band and
    ``nav_exclude`` still drops a section's own chip — both compose fine
    inside the now-sticky wrapper."""
    html = render_console(
        "Demo",
        _sections(),
        wrap_class="demo-console",
        extra_nav='<button type="button" class="k-chip k-chip-btn">Extra</button>',
        nav_exclude=("alpha",),
    )
    assert 'class="k-toolbar k-toolbar-sticky"' in html
    assert ">Extra</button>" in html
    assert 'data-console-jump="csec-alpha"' not in html  # excluded from the nav…
    assert 'id="csec-alpha"' in html  # …but the section itself still renders
    assert 'data-console-jump="csec-beta"' in html


def test_console_hides_exact_duplicate_fragment_heading() -> None:
    html = render_console(
        "Portfolio",
        [
            (
                "risk",
                "Risk Budget",
                lambda: '<section><h2 id="risk-title">Risk Budget</h2><p>Body</p></section>',
            )
        ],
        wrap_class="portfolio-console",
    )

    assert '<h2 class="k-card-title">Risk Budget</h2>' in html
    assert '<h2 id="risk-title" hidden>Risk Budget</h2>' in html


def test_console_preserves_more_specific_fragment_heading() -> None:
    html = render_console(
        "Portfolio",
        [("posture", "Posture", lambda: "<section><h2>Portfolio Posture</h2></section>")],
        wrap_class="portfolio-console",
    )

    assert "<h2>Portfolio Posture</h2>" in html
    assert "<h2 hidden>Portfolio Posture</h2>" not in html


def test_console_omits_only_an_explicitly_excluded_section_heading() -> None:
    html = render_console(
        "Demo",
        _sections(),
        wrap_class="demo-console",
        heading_exclude=("alpha",),
    )

    assert ">Alpha</h2>" not in html
    assert ">Beta</h2>" in html
