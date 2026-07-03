"""PR8: verdict badge dating/staleness, KPI-strip fact doorways, the
"what changed" reread hoist, quarter-selector broadcast JS, and the
Position-tab coaching lines.

Each behavior is pinned at the render-function level (StringIO in, markup
out) rather than through the full golden document — the golden fixture
doesn't set ``verdict_as_of``/``kpi_definition_id`` so those code paths
render as no-ops there by design (see ``tests/test_workspace_golden.py``).
"""

from __future__ import annotations

from datetime import date, datetime
from io import StringIO
from pathlib import Path

from report.models import (
    FinancialsSection,
    KpiLedgerRow,
    PortfolioPositionSection,
    SectionStatus,
    SynthesisLensRow,
    SynthesisSection,
)
from report.renderers.workspace_data import KpiStripTile, select_kpi_strip
from report.renderers.workspace_html import (
    _kpi_tile,  # pyright: ignore[reportPrivateUsage]
    _position_coaching,  # pyright: ignore[reportPrivateUsage]
    _reread_strip,  # pyright: ignore[reportPrivateUsage]
    _verdict_badge,  # pyright: ignore[reportPrivateUsage]
)
from report.renderers.workspace_script import JS

# ---------------------------------------------------------------------------
# 1. Verdict badge dating + staleness grey
# ---------------------------------------------------------------------------


def test_verdict_badge_carries_as_of_when_dated() -> None:
    out = _verdict_badge("intact", datetime(2026, 5, 20, 12, 0, 0), "Q1 2026")
    assert "as of 05-20" in out
    assert 'title="Evaluated 2026-05-20"' in out
    # Fresh (postdates the newest quarter's end) -> keeps its own green tone.
    assert "var(--ok)" in out


def test_verdict_badge_greys_when_evaluation_predates_newest_quarter() -> None:
    # Newest reported quarter is Q1 2026 (ends 2026-03-31); the verdict was
    # last evaluated back in Q3 2025 -- stale. The dot must fall back to the
    # 'pending' muted tone (honest-grey), never the verdict's own green.
    out = _verdict_badge("intact", datetime(2025, 9, 15, 0, 0, 0), "Q1 2026")
    assert "var(--muted-2)" in out
    assert "var(--ok)" not in out
    assert "predates the Q1 2026 print" in out
    # The label text itself still reports the actual verdict.
    assert "Thesis Intact" in out


def test_verdict_badge_no_as_of_when_never_evaluated() -> None:
    out = _verdict_badge("pending", None, "Q1 2026")
    assert "as of" not in out
    assert "title=" not in out


# ---------------------------------------------------------------------------
# 2. KPI-strip fact doorways
# ---------------------------------------------------------------------------


def test_kpi_strip_tile_carries_definition_id_passthrough() -> None:
    row = KpiLedgerRow(
        name="Net revenue retention",
        tier="tier_1",
        unit="%",
        kpi_definition_id=42,
        history=[("2025-12-31", 117.5), ("2026-03-31", 117.0)],
    )
    tiles = select_kpi_strip([row])
    assert len(tiles) == 1
    assert tiles[0].kpi_definition_id == 42


def _tile(**overrides: object) -> KpiStripTile:
    base: dict[str, object] = {
        "name": "Net revenue retention",
        "values": [117.5, 117.0],
        "labels": ["2025-12-31", "2026-03-31"],
        "latest_label": "2026-03-31",
        "latest_display": "117%",
        "delta_display": "↓ 0.5pp",
        "delta_sign": "neg",
        "kpi_definition_id": None,
    }
    base.update(overrides)
    return KpiStripTile(**base)  # type: ignore[arg-type]


def test_kpi_tile_is_a_fact_doorway_button_when_def_id_present() -> None:
    out = StringIO()
    _kpi_tile(out, _tile(kpi_definition_id=42), "TEST")
    html = out.getvalue()
    assert '<button class="kpi-tile fact-doorway" type="button"' in html
    assert 'data-fact-ref="kpi:TEST:42"' in html
    assert html.strip().endswith("</button>")


def test_kpi_tile_stays_inert_div_when_def_id_absent() -> None:
    out = StringIO()
    _kpi_tile(out, _tile(kpi_definition_id=None), "TEST")
    html = out.getvalue()
    assert '<div class="kpi-tile">' in html
    assert "fact-doorway" not in html
    assert "data-fact-ref" not in html
    assert html.strip().endswith("</div>")


# ---------------------------------------------------------------------------
# 3. five_min_reread hoist strip
# ---------------------------------------------------------------------------


def _synthesis_with_reread(content_md: str) -> SynthesisSection:
    return SynthesisSection(
        status=SectionStatus.OK,
        ticker="TEST",
        lenses=[SynthesisLensRow(name="five_min_reread", content_md=content_md)],
    )


def test_reread_strip_renders_first_substantive_line() -> None:
    section = _synthesis_with_reread(
        "## 1. What changed\n\n- NRR held above 117% despite the guide-down noise.\n"
        "- Services decline overshot plan by 400bps.\n"
    )
    out = StringIO()
    _reread_strip(out, section)
    html = out.getvalue()
    assert "l1-reread" in html
    assert "NRR held above 117% despite the guide-down noise." in html
    assert 'data-xtab="synthesis"' in html
    # The heading line itself must not leak through as the lede.
    assert "1. What changed" not in html


def test_reread_strip_hides_when_no_synthesis_section() -> None:
    out = StringIO()
    _reread_strip(out, None)
    assert out.getvalue() == ""


def test_reread_strip_hides_when_lens_absent() -> None:
    section = SynthesisSection(status=SectionStatus.OK, ticker="TEST", lenses=[])
    out = StringIO()
    _reread_strip(out, section)
    assert out.getvalue() == ""


def test_reread_strip_hides_when_lens_has_no_substantive_line() -> None:
    section = _synthesis_with_reread("## 1. What changed\n\n## 2. Recommended action\n")
    out = StringIO()
    _reread_strip(out, section)
    assert out.getvalue() == ""


# ---------------------------------------------------------------------------
# 4. Quarter-selector broadcast JS
# ---------------------------------------------------------------------------


def test_quarter_broadcast_js_present() -> None:
    assert "quarterGroups.forEach(function (other)" in JS
    assert "other.querySelector('button[data-quarter=\"' + q + '\"]')" in JS
    assert "swapQuarterGroup(other, q)" in JS


# ---------------------------------------------------------------------------
# 5. Position-tab coaching lines
# ---------------------------------------------------------------------------


def test_position_coaching_renders_guard_line_and_review_doorway() -> None:
    out = StringIO()
    _position_coaching(out, "TEST", 3, None)
    html = out.getvalue()
    assert "Guard: never run on this name" in html
    assert "3 position reviews" in html
    assert 'href="http://localhost:7421/socratic/TEST"' in html
    assert "Review this position" in html
    # No graded-sells line yet -- that ledger is a parallel, unmerged PR.
    assert "graded" not in html.lower()


def test_position_coaching_singular_review_count() -> None:
    out = StringIO()
    _position_coaching(out, "TEST", 1, None)
    assert "1 position review" in out.getvalue()
    assert "1 position reviews" not in out.getvalue()


def test_position_coaching_includes_graded_sell_line_when_provided() -> None:
    out = StringIO()
    _position_coaching(out, "TEST", 2, "3 of 4 graded sells beat a 90-day hold (75%).")
    html = out.getvalue()
    assert "3 of 4 graded sells beat a 90-day hold (75%)." in html


def test_load_graded_sell_base_rate_absent_module_degrades_to_none(tmp_path: Path) -> None:
    """``advisor.position_review.graded_sell_record`` does not exist on this
    branch (a parallel PR adds it) -- the guarded import must return None,
    never raise."""
    from report.renderers.workspace_data import load_graded_sell_base_rate

    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    assert load_graded_sell_base_rate("TEST", db_path) is None


def _raw_advisor_memos_db(tmp_path: Path) -> Path:
    """A minimal ``advisor_memos`` table built with raw sqlite3 (no alembic) --
    just the columns ``advisor.store.list_memos`` selects, so the count
    loader can be exercised without the full migration chain."""
    import sqlite3

    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE advisor_memos (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            ticker TEXT,
            counter_ticker TEXT,
            title TEXT NOT NULL,
            body_md TEXT NOT NULL,
            context_json TEXT,
            stance TEXT,
            horizon_days INTEGER,
            score_status TEXT NOT NULL,
            note_id INTEGER,
            ledger_entry_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    rows = [
        ("bhanu", "position_review", "TEST", "First review"),
        ("bhanu", "position_review", "TEST", "Second review"),
        ("bhanu", "position_review", "OTHER", "Different ticker"),
        ("bhanu", "next_dollar", "TEST", "Not a position review"),
    ]
    for user_id, kind, ticker, title in rows:
        conn.execute(
            "INSERT INTO advisor_memos "
            "(user_id, kind, ticker, title, body_md, score_status, created_at) "
            "VALUES (?, ?, ?, ?, 'body', 'pending', '2026-06-01T00:00:00')",
            (user_id, kind, ticker, title),
        )
    conn.commit()
    conn.close()
    return db_path


def test_position_review_count_loader_counts_only_this_tickers_kind(tmp_path: Path) -> None:
    from report.renderers.workspace_data import (
        _load_position_review_count_safe,  # pyright: ignore[reportPrivateUsage]
    )

    db_path = _raw_advisor_memos_db(tmp_path)
    assert _load_position_review_count_safe("TEST", db_path) == 2
    assert _load_position_review_count_safe("OTHER", db_path) == 1
    assert _load_position_review_count_safe("NOTHELD", db_path) == 0


def test_position_review_count_loader_degrades_to_zero_without_db(tmp_path: Path) -> None:
    from report.renderers.workspace_data import (
        _load_position_review_count_safe,  # pyright: ignore[reportPrivateUsage]
    )

    missing = tmp_path / "does_not_exist.db"
    assert _load_position_review_count_safe("TEST", missing) == 0


# ---------------------------------------------------------------------------
# Sanity: the fixtures above actually exercise real model shapes (financials/
# position sections construct without error under minimal fields).
# ---------------------------------------------------------------------------


def test_minimal_financials_and_position_fixtures_construct() -> None:
    fin = FinancialsSection(status=SectionStatus.OK, quarter_labels=["Q4 2025", "Q1 2026"])
    assert fin.quarter_labels[-1] == "Q1 2026"
    pos = PortfolioPositionSection(status=SectionStatus.OK, held=True, position_as_of=date.today())
    assert pos.held
