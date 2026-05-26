"""Tests for the §1 Snapshot "Recent decisions" sidebar.

Covers the new `recent_decisions` field on SnapshotSection (3 most recent
decisions from the audit ledger introduced in migration 0046) and how each
renderer surfaces it: HTML sidebar with outcome-colored badges, workspace
panel with the same outcome classes, markdown bullet list. The sidebar is
suppressed entirely when there are no rows for the ticker.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from report.models import (
    BearCaseSection,
    DecisionBadge,
    SectionStatus,
    SnapshotSection,
    ThesisSection,
    ValuationSnapshot,
)
from report.renderers.html import _snapshot as _snapshot_html
from report.renderers.markdown import _snapshot as _snapshot_md
from report.renderers.workspace_html import _thesis_tab as _thesis_tab_workspace
from report.sections.snapshot import build as build_snapshot

# ---------------------------------------------------------------------------
# Builder fixture: temp repo_root with a minimal `decisions` table
# ---------------------------------------------------------------------------


def _create_repo_with_decisions(tmp_path: Path) -> Path:
    """Build a repo layout with `data/portfolio.db` and a decisions table.

    Mirrors migration 0046's columns + the unique source_artifact_id index
    so we exercise the same idempotency contract the production extractor uses.
    `tracked_companies` is created empty so the snapshot builder's
    `_company_name` query doesn't trip on a missing table.
    """
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    try:
        conn.executescript(
            """
            CREATE TABLE tracked_companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                list_type TEXT NOT NULL DEFAULT 'portfolio'
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR(16) NOT NULL,
                recommendation_kind VARCHAR(32) NOT NULL,
                recommendation_value FLOAT,
                conviction VARCHAR(16),
                source_artifact_id INTEGER NOT NULL,
                source_lens VARCHAR(64),
                rationale_excerpt TEXT,
                made_at DATETIME NOT NULL,
                user_acted_at DATETIME,
                user_action_kind VARCHAR(32),
                user_notes TEXT,
                outcome_at DATETIME,
                outcome_label VARCHAR(16),
                outcome_pct FLOAT,
                outcome_notes TEXT,
                created_at DATETIME NOT NULL
            );
            CREATE UNIQUE INDEX idx_decisions_source_artifact
                ON decisions(source_artifact_id);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


def _seed(
    repo: Path,
    ticker: str,
    *,
    source_artifact_id: int,
    kind: str,
    rationale: str,
    made_at: datetime,
    outcome_label: str | None = None,
) -> None:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    try:
        conn.execute(
            """
            INSERT INTO decisions
                (ticker, recommendation_kind, source_artifact_id, rationale_excerpt,
                 made_at, outcome_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker.upper(),
                kind,
                source_artifact_id,
                rationale,
                made_at.isoformat(),
                outcome_label,
                made_at.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Builder — decisions land on the section
# ---------------------------------------------------------------------------


def test_builder_loads_three_most_recent_decisions(tmp_path: Path) -> None:
    """4 seeded decisions, only the newest 3 land on the section, newest first."""
    repo = _create_repo_with_decisions(tmp_path)
    rationale_long = (
        "Premium too wide vs sum-of-segments fair value; AI capex "
        "amortization headwind should compress operating margins next year."
    )
    _seed(
        repo,
        "TEST",
        source_artifact_id=1,
        kind="trim",
        rationale=rationale_long,
        made_at=datetime(2026, 5, 1, tzinfo=UTC),
        outcome_label="correct",
    )
    _seed(
        repo,
        "TEST",
        source_artifact_id=2,
        kind="hold",
        rationale="Within MoS band, no action.",
        made_at=datetime(2026, 4, 1, tzinfo=UTC),
        outcome_label="pending",
    )
    _seed(
        repo,
        "TEST",
        source_artifact_id=3,
        kind="add",
        rationale="Q4 cleared every KPI, discount real.",
        made_at=datetime(2026, 3, 1, tzinfo=UTC),
        outcome_label="mixed",
    )
    _seed(
        repo,
        "TEST",
        source_artifact_id=4,
        kind="hold",
        rationale="Older entry that should fall off the 3-row window.",
        made_at=datetime(2025, 12, 1, tzinfo=UTC),
    )

    section = build_snapshot("TEST", repo, model_link=None)
    assert section.status == SectionStatus.MISSING_DATA  # no holdings/dcf, but...
    assert len(section.recent_decisions) == 3
    # Newest first
    kinds = [d.recommendation_kind for d in section.recent_decisions]
    assert kinds == ["trim", "hold", "add"]
    # Date short = YYYY-MM
    assert section.recent_decisions[0].date_short == "2026-05"
    # Rationale truncated to ~80 chars with ellipsis when over the limit
    first = section.recent_decisions[0]
    assert len(first.rationale_short) <= 83  # 80 + "..."
    assert first.rationale_short.endswith("...")
    # Short rationale survives intact (no ellipsis)
    second = section.recent_decisions[1]
    assert second.rationale_short == "Within MoS band, no action."
    # Outcome labels propagate; missing → pending
    assert section.recent_decisions[0].outcome_label == "correct"
    assert section.recent_decisions[1].outcome_label == "pending"
    assert section.recent_decisions[2].outcome_label == "mixed"


def test_builder_returns_empty_list_when_no_decisions_table(tmp_path: Path) -> None:
    """Repo with no portfolio.db → no decisions, section still builds cleanly."""
    section = build_snapshot("TEST", tmp_path, model_link=None)
    assert section.recent_decisions == []


def test_builder_returns_empty_list_when_table_has_no_rows_for_ticker(
    tmp_path: Path,
) -> None:
    """Table exists, rows exist for a different ticker → empty list for this one."""
    repo = _create_repo_with_decisions(tmp_path)
    _seed(
        repo,
        "OTHER",
        source_artifact_id=1,
        kind="trim",
        rationale="Different ticker.",
        made_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    section = build_snapshot("TEST", repo, model_link=None)
    assert section.recent_decisions == []


# ---------------------------------------------------------------------------
# Renderers — fixture sections with seeded badges
# ---------------------------------------------------------------------------


def _section_with_badges(badges: list[DecisionBadge]) -> SnapshotSection:
    return SnapshotSection(
        status=SectionStatus.OK,
        ticker="TEST",
        company_name="Test Co",
        thesis_one_liner="Test thesis line.",
        verdict="intact",
        valuation=ValuationSnapshot(
            consolidated_npv_per_share=100.0,
            current_price=90.0,
        ),
        recent_decisions=badges,
    )


def _empty_thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK,
        thesis_full="t",
        overall_breach_status="ok",
    )


_BADGES_FIXTURE = [
    DecisionBadge(
        date_short="2026-05",
        recommendation_kind="trim",
        outcome_label="correct",
        rationale_short="Premium too wide vs fair value.",
    ),
    DecisionBadge(
        date_short="2026-04",
        recommendation_kind="hold",
        outcome_label="pending",
        rationale_short="Within MoS band, no action.",
    ),
    DecisionBadge(
        date_short="2026-03",
        recommendation_kind="add",
        outcome_label="mixed",
        rationale_short="KPIs cleared, discount real.",
    ),
]


def test_html_renderer_emits_sidebar_with_outcome_classes() -> None:
    section = _section_with_badges(_BADGES_FIXTURE)
    out = StringIO()
    _snapshot_html(out, section)
    html = out.getvalue()
    assert 'class="decision-history"' in html
    # All three outcomes have their CSS class attached
    assert "outcome-correct" in html
    assert "outcome-pending" in html
    assert "outcome-mixed" in html
    # Date + kind + outcome surface in the badge text
    assert "2026-05" in html
    assert "TRIM" in html
    assert "correct" in html
    # Hover tooltip wires the rationale excerpt
    assert 'title="Premium too wide vs fair value."' in html


def test_html_renderer_hides_sidebar_when_empty() -> None:
    """No decisions → no `decision-history` block at all (not even a placeholder)."""
    section = _section_with_badges([])
    out = StringIO()
    _snapshot_html(out, section)
    html = out.getvalue()
    assert "decision-history" not in html
    assert "Recent decisions" not in html


def test_workspace_renderer_emits_decision_panel() -> None:
    section = _section_with_badges(_BADGES_FIXTURE)
    out = StringIO()
    _thesis_tab_workspace(out, section, _empty_thesis(), bear=_bear_stub())
    html = out.getvalue()
    assert "decision-history-panel" in html
    assert "Recent decisions" in html
    # Outcome classes on the badge divs
    assert "outcome-correct" in html
    assert "outcome-pending" in html
    assert "outcome-mixed" in html
    # Each kind label is uppercased for display
    assert ">TRIM<" in html
    assert ">HOLD<" in html
    assert ">ADD<" in html


def test_workspace_renderer_hides_panel_when_empty() -> None:
    section = _section_with_badges([])
    out = StringIO()
    _thesis_tab_workspace(out, section, _empty_thesis(), bear=_bear_stub())
    html = out.getvalue()
    assert "decision-history-panel" not in html


def test_markdown_renderer_emits_bullet_list() -> None:
    section = _section_with_badges(_BADGES_FIXTURE)
    out = StringIO()
    _snapshot_md(out, section)
    md = out.getvalue()
    assert "#### Recent decisions" in md
    # One bullet per decision, with kind bolded + outcome in code formatting
    assert "- 2026-05 · **TRIM** · `correct`" in md
    assert "- 2026-04 · **HOLD** · `pending`" in md
    assert "- 2026-03 · **ADD** · `mixed`" in md
    # Rationale tail attached after an em-dash
    assert "Premium too wide vs fair value." in md


def test_markdown_renderer_hides_section_when_empty() -> None:
    section = _section_with_badges([])
    out = StringIO()
    _snapshot_md(out, section)
    md = out.getvalue()
    assert "Recent decisions" not in md


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bear_stub() -> BearCaseSection:
    """Workspace `_thesis_tab` requires a BearCaseSection — empty one suffices
    for our scope (it's threaded through to a separate tab now)."""
    return BearCaseSection(status=SectionStatus.OK)
