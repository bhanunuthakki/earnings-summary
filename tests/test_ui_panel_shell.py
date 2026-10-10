"""Characterization pins for the shared panel shell (``ui.panel``).

The M5 panel-shell consolidation is a structural refactor: the shell literals
hand-copied through the ``*_panel.py`` family move behind
:func:`ui.panel.panel_section` / :func:`ui.panel.panel_empty` with **identical
emitted bytes**. These tests pin that identity against the exact literal
compositions the panels carried before the migration — every case transcribes
a real pre-migration shape (title, style prefix, extra classes, data
attributes, ``<h2>`` attributes, script tail, dynamic f-string titles, the
muted empty state), so any drift in the contract is a red test, not a silent
visual change.

Adversarial inventory: ``test_migrated_panel_modules_own_no_raw_section_literal``
freezes the migrated modules — a hand-reverted panel (or a new raw literal
added to a migrated one) fails here instead of re-seeding the duplication the
consolidation removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ui.panel import panel_empty, panel_section

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_titled_section_matches_legacy_concat() -> None:
    # Pre-migration shape (credibility/ir_coverage/dcf_coverage family):
    #   "".join([STYLE, '<section class="panel"><h2>T</h2>', sub, body, "</section>"])
    style = "<style>.x{color:var(--fg)}</style>"
    legacy = (
        style + '<section class="panel"><h2>IR document coverage</h2>'
        '<p class="sub">Headless auto-fetch.</p>'
        "<table><tr><td>1</td></tr></table>"
        "</section>"
    )
    assert (
        panel_section(
            '<p class="sub">Headless auto-fetch.</p>',
            "<table><tr><td>1</td></tr></table>",
            title="IR document coverage",
            style=style,
        )
        == legacy
    )


def test_untitled_toolbar_shape_matches_legacy_fstring() -> None:
    # Pre-migration shape (annual_letter_panel):
    #   f'<section class="panel">{toolbar}<div class="prose">{body}</div></section>'
    toolbar = '<div class="k-toolbar"><h2 class="k-toolbar-title">Letter to self</h2></div>'
    legacy = f'<section class="panel">{toolbar}<div class="prose">read me</div></section>'
    assert panel_section(toolbar, '<div class="prose">read me</div>') == legacy


def test_extra_classes_match_legacy_literal() -> None:
    # Pre-migration shape (redteam_pnl_panel / calibration_scorecard_panel):
    #   '<section class="panel rtp">' + toolbar + '<p class="sub">…</p>' … '</section>'
    legacy = (
        '<section class="panel rtp">'
        '<div class="k-toolbar"></div>'
        '<p class="sub">Every REFUTE/ACCEPT/DEFER.</p>'
        "</section>"
    )
    assert (
        panel_section(
            '<div class="k-toolbar"></div>',
            '<p class="sub">Every REFUTE/ACCEPT/DEFER.</p>',
            cls="rtp",
        )
        == legacy
    )


def test_data_attributes_match_legacy_fstring() -> None:
    # Pre-migration shape (position_lifecycle_panel):
    #   f'<section class="panel" data-plc-root data-plc-ticker="{t}">'
    #   f"<h2>Position lifecycle</h2>{inner}</section>"
    attrs = ' data-plc-root data-plc-ticker="NU"'
    legacy = f'<section class="panel"{attrs}><h2>Position lifecycle</h2><ul></ul></section>'
    assert panel_section("<ul></ul>", title="Position lifecycle", attrs=attrs) == legacy


def test_title_attributes_match_legacy_literal() -> None:
    # Pre-migration shape (diet_panel): '<h2 title="…">Information diet</h2>'
    title_attrs = ' title="Pull lane — what to READ on your names."'
    legacy = f'<section class="panel"><h2{title_attrs}>Information diet</h2></section>'
    assert panel_section(title="Information diet", title_attrs=title_attrs) == legacy


def test_script_tail_matches_legacy_concat() -> None:
    # Pre-migration shape (ticker_settings_panel): "…" + "</section>" + _SCRIPT
    script = "<script>(function(){})();</script>"
    legacy = '<section class="panel"><h2>Ticker settings</h2><table></table></section>' + script
    assert panel_section("<table></table>", title="Ticker settings", tail=script) == legacy


def test_dynamic_title_matches_legacy_fstring() -> None:
    # Pre-migration shape (portfolio_panel):
    #   f'<section class="panel"><h2>Business-factor exposure {cov_pill}</h2>'
    cov_pill = '<span class="k-pill">3</span>'
    legacy = (
        f'<section class="panel"><h2>Business-factor exposure {cov_pill}</h2><div></div></section>'
    )
    assert panel_section("<div></div>", title=f"Business-factor exposure {cov_pill}") == legacy


def test_empty_state_matches_legacy_muted_paragraph() -> None:
    # Pre-migration shape (credibility_panel's absent-ledger state, verbatim):
    message = (
        "No confidence-observation ledger in this DB — run "
        "<code>alembic upgrade head</code> (0106) then "
        "<code>python execution/build_confidence_observations.py --apply</code>."
    )
    legacy = f'<section class="panel"><h2>Credibility</h2><p class="muted">{message}</p></section>'
    assert panel_empty(message, title="Credibility") == legacy


def test_empty_state_with_style_and_classes_matches_legacy() -> None:
    style = "<style>.s{}</style>"
    legacy = (
        style + '<section class="panel cs-stub"><h2>Calibration coach</h2>'
        '<p class="muted cs-caption">no scorecard yet</p></section>'
    )
    # The cs-stub empty state carries a paragraph with extra classes, so its
    # paragraph stays in the body (panel_empty owns only the plain muted shape).
    assert (
        panel_section(
            '<p class="muted cs-caption">no scorecard yet</p>',
            title="Calibration coach",
            cls="cs-stub",
            style=style,
        )
        == legacy
    )


def test_escaped_entity_titles_are_not_re_escaped() -> None:
    # Pre-migration shapes carry entity-escaped literals (portfolio_panel:
    # '<h2>Risk &amp; efficiency</h2>'); the contract must not re-escape.
    legacy = '<section class="panel"><h2>Risk &amp; efficiency</h2><div></div></section>'
    assert panel_section("<div></div>", title="Risk &amp; efficiency") == legacy


# --- Adversarial inventory: the migrated modules own no raw shell literal. ---
# Any site that keeps a raw literal carries an explicit receipt entry in the
# M5 consolidation receipt; a new raw literal in a migrated module (or a
# hand-reverted panel) fails here instead of re-seeding the duplication.

_MIGRATED_PANEL_MODULES: tuple[str, ...] = (
    "advisor_memos_panel.py",
    "allocation_decisions_panel.py",
    "annual_letter_panel.py",
    "attribution_panel.py",
    "calibration_scorecard_panel.py",
    "credibility_panel.py",
    "cron_health_panel.py",
    "dcf_coverage_panel.py",
    "dcf_globals_panel.py",
    "diet_panel.py",
    "evals_panel.py",
    "fact_overrides_panel.py",
    "ir_coverage_panel.py",
    "ledger_panel.py",
    "model_eval_panel.py",
    "performance_risk_panel.py",
    "portfolio_console_panel.py",
    "portfolio_panel.py",
    "position_lifecycle_panel.py",
    "positioning_panel.py",
    "redteam_pnl_panel.py",
    "restatements_panel.py",
    "section_coverage_panel.py",
    "source_calls_panel.py",
    "thesis_ledger_panel.py",
    "ticker_settings_panel.py",
    "validation_issues_panel.py",
)


@pytest.mark.parametrize("module", _MIGRATED_PANEL_MODULES)
def test_migrated_panel_modules_own_no_raw_section_literal(module: str) -> None:
    path = PROJECT_ROOT / "src" / "pipeline" / module
    assert path.exists(), f"missing panel module: {module}"
    text = path.read_text(encoding="utf-8")
    assert '<section class="panel' not in text, (
        f"{module} re-introduced a raw panel-shell literal; compose through "
        "ui.panel.panel_section / panel_empty"
    )
