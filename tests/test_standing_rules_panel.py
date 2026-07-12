"""Standing-rules surface wiring (Monthly Red Team PR10).

The per-ticker brief's Position tab historically rendered zero
``position_sizing_intent`` content — the FLKR exit ladder, RBRK/BKNG target
weights and the DRAFT add-rungs were visible to the Risk panel, /review and
Ask but invisible in the brief. These tests pin:

* ``load_standing_rules`` reads the ticker's full intent history newest-first,
  detects the machine-proposed DRAFT prefixes ("[draft, pending owner
  review]" / "[red-team accepted, pending owner edit]"), strips them from the
  displayed narrative (the chip carries the provenance), and derives the
  coverage flags via ``position_guard``'s OWN ``evaluate_downside_trigger`` /
  ``evaluate_add_trigger`` — never re-derived;
* the loader degrades to ``None`` on a missing DB / table / zero rows
  (hide-don't-stub), same contract as every other workspace loader;
* ``_standing_rules_block`` renders one dense kit row per intent (kind chip,
  value, narrative, updated stamp) plus the coverage read, and renders
  nothing when there are no rows;
* ``_position_tab`` shows the block for a held name and for an UNHELD name
  that carries intents, and still collapses to the empty panel when neither.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import PortfolioPositionSection, SectionStatus  # noqa: E402
from report.renderers.workspace_data import (  # noqa: E402
    StandingRuleRow,
    StandingRulesPanel,
    load_standing_rules,
)
from report.renderers.workspace_sections.position import (  # noqa: E402
    _position_tab,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    _standing_rules_block,  # pyright: ignore[reportPrivateUsage]
)

_TS = "2026-07-10T08:07:05.021439"


def _intent_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE position_sizing_intent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, ticker TEXT, intent_kind TEXT,
            intent_value REAL, narrative TEXT,
            created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert(
    db_path: Path,
    ticker: str,
    kind: str,
    value: float | None,
    narrative: str,
    updated_at: str = _TS,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO position_sizing_intent"
        "(user_id, ticker, intent_kind, intent_value, narrative, created_at, updated_at)"
        " VALUES ('bhanu', ?, ?, ?, ?, ?, ?)",
        (ticker, kind, value, narrative, updated_at, updated_at),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_reads_rows_newest_first_and_detects_draft(tmp_path: Path) -> None:
    db = _intent_db(tmp_path)
    _insert(
        db,
        "TEST",
        "target_weight_pct",
        6.0,
        "Two-sided exit ladder: close <$57.50 -> cut to 3.5%; close <$50 -> exit.",
        updated_at="2026-07-01T00:00:00",
    )
    _insert(
        db,
        "TEST",
        "add_rung",
        None,
        "[draft, pending owner review] Add-rung: add <$10.32 -> +1% of book.",
        updated_at="2026-07-12T00:00:00",
    )
    panel = load_standing_rules("TEST", db, tmp_path)
    assert panel is not None
    assert [r.intent_kind for r in panel.rows] == ["add_rung", "target_weight_pct"]
    draft, owner = panel.rows
    assert draft.is_draft
    # the raw provenance prefix is stripped — the DRAFT chip carries it
    assert draft.narrative.startswith("Add-rung: add <$10.32")
    assert not owner.is_draft
    assert owner.intent_value == 6.0
    assert owner.updated_at == datetime(2026, 7, 1)


def test_loader_red_team_accepted_prefix_is_draft(tmp_path: Path) -> None:
    db = _intent_db(tmp_path)
    _insert(
        db,
        "TEST",
        "sizing_note",
        None,
        "[red-team accepted, pending owner edit] Trim to 5% on the next rebalance.",
    )
    panel = load_standing_rules("TEST", db, tmp_path)
    assert panel is not None
    assert panel.rows[0].is_draft
    assert panel.rows[0].narrative == "Trim to 5% on the next rebalance."


def test_loader_coverage_reuses_position_guard_detection(tmp_path: Path) -> None:
    """Downside from the FLKR-shaped narrative parse; add-rung from the
    explicit add_rung kind — a DRAFT-only add-rung reads add_is_draft."""
    db = _intent_db(tmp_path)
    _insert(
        db,
        "TEST",
        "target_weight_pct",
        7.0,
        "DOWNSIDE: close <$57.50 -> cut to 3.5%; close <$50 -> exit entirely.",
    )
    _insert(
        db,
        "TEST",
        "add_rung",
        None,
        "[draft, pending owner review] Add-rung: add <$10.32 -> +1% of book.",
    )
    panel = load_standing_rules("TEST", db, tmp_path)
    assert panel is not None
    assert panel.downside_passed is True
    assert panel.add_passed is True
    assert panel.add_is_draft is True  # only the draft row encodes the add


def test_loader_owner_add_rung_is_not_draft(tmp_path: Path) -> None:
    db = _intent_db(tmp_path)
    _insert(db, "TEST", "add_rung", None, "Add below $50, thesis intact.")
    panel = load_standing_rules("TEST", db, tmp_path)
    assert panel is not None
    assert panel.add_passed is True
    assert panel.add_is_draft is False


def test_loader_degrades_to_none(tmp_path: Path) -> None:
    # missing DB
    assert load_standing_rules("TEST", tmp_path / "nope.db", tmp_path) is None
    # DB without the table
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    assert load_standing_rules("TEST", bare, tmp_path) is None
    # table with no rows for this ticker
    db = _intent_db(tmp_path)
    _insert(db, "OTHER", "target_weight_pct", 4.0, "Target 4%.")
    assert load_standing_rules("TEST", db, tmp_path) is None


# ---------------------------------------------------------------------------
# Renderer block
# ---------------------------------------------------------------------------


def _panel(rows: list[StandingRuleRow], **kw: object) -> StandingRulesPanel:
    return StandingRulesPanel(
        rows=rows,
        downside_passed=kw.get("downside_passed", True),  # type: ignore[arg-type]
        add_passed=kw.get("add_passed", True),  # type: ignore[arg-type]
        add_is_draft=bool(kw.get("add_is_draft", False)),
    )


_ROWS = [
    StandingRuleRow(
        intent_kind="add_rung",
        intent_value=None,
        narrative="Add-rung: add <$10.32 -> +1% of book.",
        is_draft=True,
        updated_at=datetime(2026, 7, 12),
    ),
    StandingRuleRow(
        intent_kind="target_weight_pct",
        intent_value=6.0,
        narrative="Target 6% after the Q1 print.",
        is_draft=False,
        updated_at=datetime(2026, 7, 1),
    ),
]


def test_block_renders_kind_chip_draft_chip_and_coverage() -> None:
    out = StringIO()
    _standing_rules_block(out, _panel(_ROWS, add_is_draft=True))
    html = out.getvalue()
    assert "Standing rules" in html
    assert "downside rule ✓ · add-rung ✓ (draft)" in html
    assert '<span class="k-chip k-chip-mono">ADD RUNG</span>' in html
    assert '<span class="k-chip">DRAFT</span>' in html
    assert html.count('<span class="k-chip">DRAFT</span>') == 1  # owner row has none
    assert "6.0%" in html  # ..._pct kind formats as percent
    assert "2026-07-12" in html and "2026-07-01" in html


def test_block_hidden_without_rows() -> None:
    out = StringIO()
    _standing_rules_block(out, None)
    _standing_rules_block(out, _panel([]))
    assert out.getvalue() == ""


def test_block_failed_downside_renders_cross() -> None:
    out = StringIO()
    _standing_rules_block(out, _panel(_ROWS, downside_passed=False, add_passed=False))
    html = out.getvalue()
    assert "downside rule ✗" in html
    assert "add-rung —" in html


# ---------------------------------------------------------------------------
# Position tab gating
# ---------------------------------------------------------------------------


def test_position_tab_unheld_with_rules_shows_block() -> None:
    out = StringIO()
    _position_tab(
        out,
        PortfolioPositionSection(status=SectionStatus.NOT_APPLICABLE),
        ticker="test",
        standing_rules=_panel(_ROWS),
    )
    html = out.getvalue()
    assert "Standing rules" in html
    assert "No broker position on file for TEST" in html
    assert "not held" not in html  # the empty panel is replaced, not stacked


def test_position_tab_unheld_without_rules_keeps_empty_panel() -> None:
    out = StringIO()
    _position_tab(out, None, ticker="TEST", standing_rules=None)
    html = out.getvalue()
    assert "not held" in html
    assert "Standing rules" not in html


def test_position_tab_held_appends_block_after_decisions() -> None:
    pp = PortfolioPositionSection(status=SectionStatus.OK, held=True, total_quantity=10.0)
    out = StringIO()
    _position_tab(out, pp, ticker="TEST", standing_rules=_panel(_ROWS))
    html = out.getvalue()
    assert "Your position" in html
    assert "Standing rules" in html
    assert html.index("Your position") < html.index("Standing rules")


# ---------------------------------------------------------------------------
# Markdown mirror
# ---------------------------------------------------------------------------


def test_markdown_mirror_renders_rules_from_same_loader(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from typing import cast

    from report.models import ReportSpec
    from report.renderers.markdown import (
        _standing_rules_md,  # pyright: ignore[reportPrivateUsage]
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _intent_db(data_dir)
    _insert(
        db,
        "TEST",
        "add_rung",
        None,
        "[draft, pending owner review] Add-rung: add <$10.32 -> +1% of book.",
    )
    out = StringIO()
    spec = cast("ReportSpec", SimpleNamespace(ticker="TEST", repo_root=str(tmp_path)))
    _standing_rules_md(out, spec)
    s = out.getvalue()
    assert "**Standing rules**" in s
    assert "`[DRAFT]`" in s
    assert "[draft, pending owner review]" not in s  # prefix stripped, chip carries it
    assert "add-rung ✓ (draft)" in s


def test_markdown_mirror_silent_without_rules(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from typing import cast

    from report.models import ReportSpec
    from report.renderers.markdown import (
        _standing_rules_md,  # pyright: ignore[reportPrivateUsage]
    )

    out = StringIO()
    spec = cast("ReportSpec", SimpleNamespace(ticker="TEST", repo_root=str(tmp_path)))
    _standing_rules_md(out, spec)
    assert out.getvalue() == ""
