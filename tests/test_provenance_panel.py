"""Consolidated Provenance console assembler + route (S10 PR2).

The 8-tab System diagnostics strip collapsed into one page composing the 8
builders, Coverage prominent. These pin: the composition (all 8 sections,
Coverage first, anchor nav), the degrade-don't-crash contract, and the
/api/panel/provenance route + the old-id deep-link aliases.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from pipeline.command_center_shell import (  # noqa: E402
    _LEGACY_PANEL_REDIRECTS,  # pyright: ignore[reportPrivateUsage]
    _SKELETON_KINDS,  # pyright: ignore[reportPrivateUsage]
    _THEMES,  # pyright: ignore[reportPrivateUsage]
)
from pipeline.provenance_panel import render_provenance_panel  # noqa: E402

_SECTIONS = (
    "coverage",
    "validation",
    "evals",
    "ir_coverage",
    "source_calls",
    "cron_health",
    "dcf_coverage",
    "restatements",
)


def _empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    sqlite3.connect(str(db)).close()
    return db


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


def test_assembler_composes_all_eight_sections(tmp_path: Path) -> None:
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert 'class="prov-console"' in html
    for anchor in _SECTIONS:
        assert f'id="prov-{anchor}"' in html  # the section wrapper
        assert f'href="#prov-{anchor}"' in html  # the anchor-nav chip


def test_coverage_leads_prominently(tmp_path: Path) -> None:
    """The owner: 'Coverage already makes sense, keep it prominent.' It is the
    first section, not buried mid-page."""
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert html.index('id="prov-coverage"') < html.index('id="prov-validation"')
    assert html.index('id="prov-validation"') < html.index('id="prov-evals"')


def test_assembler_degrades_on_empty_db(tmp_path: Path) -> None:
    """Every builder degrades to a stub on missing tables — the console renders
    rather than crashing on a bare DB."""
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert len(html) > 0
    # A bare DB still produces each builder's missing/empty state, not a 500.
    assert "Validation" in html


def test_one_broken_builder_does_not_blank_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_safe isolates a raised builder into an error card; the rest still render.
    The assembler re-imports each builder per call, so patching the source name
    takes effect."""

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("synthetic builder failure")

    monkeypatch.setattr("pipeline.validation_issues_panel.render_validation_panel", _boom)
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert "synthetic builder failure" in html  # the error card
    assert "This diagnostic failed to render" in html
    assert 'id="prov-coverage"' in html  # the other sections survived


# ---------------------------------------------------------------------------
# Shell collapse: one Provenance sub-tab; old ids alias here
# ---------------------------------------------------------------------------


def test_system_section_has_one_provenance_subtab() -> None:
    system = next(subs for tid, _lbl, subs in _THEMES if tid == "system")
    assert len(system) == 1
    assert system[0][0] == "provenance"
    assert system[0][2] == "/api/panel/provenance"


def test_collapsed_ids_alias_to_provenance() -> None:
    # The 8 old System panel ids (note: console anchor "coverage" maps to the
    # "section_coverage" panel id) all alias to the one console.
    for panel_id in (
        "section_coverage",
        "ir_coverage",
        "source_calls",
        "cron_health",
        "dcf_coverage",
        "evals",
        "validation",
        "restatements",
    ):
        assert _LEGACY_PANEL_REDIRECTS[panel_id] == "provenance"
    assert "provenance" in _SKELETON_KINDS


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def test_provenance_route_renders(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _empty_db(tmp_path / "data")
    client = comments_server.create_app(tmp_path).test_client()
    resp = client.get("/api/panel/provenance")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="prov-console"' in body
    assert 'id="prov-coverage"' in body


def test_old_system_panel_routes_still_serve(tmp_path: Path) -> None:
    """The /api/panel/<id> fetch routes for each builder stay live (the console
    composes them; a direct fetch / cached deep-link still resolves)."""
    (tmp_path / "data").mkdir()
    _empty_db(tmp_path / "data")
    client = comments_server.create_app(tmp_path).test_client()
    for panel_id in ("section_coverage", "validation", "evals", "restatements"):
        assert client.get(f"/api/panel/{panel_id}").status_code == 200
