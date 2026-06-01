"""Sections forgo their LLM call + surface a BudgetSkip when a purpose is at/over
its monthly cap in ``on_exceed='skip'`` mode (PR1).

Full-LLM sections (bear_case, recent_developments) become BUDGET_SKIPPED; partial
sections (qa_roster) drop just the LLM sub-part and keep rendering. Each test
asserts the section's LLM generator is NOT called and the skip is attributed via
``budget_skip``. The gate reads ``repo_root/data/portfolio.db``; with no budget
configured (the other section tests) the gate is a no-op, so this is the only
suite that exercises the skip path end to end.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from report.models import (
    AppendixSection,
    BearCaseSection,
    EarningsSection,
    FinancialsSection,
    QARosterSection,
    RecentDevelopmentsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
    TranscriptEntry,
)
from report.sections import bear_case, qa_roster, recent_developments

_QA_TEXT = (
    "=== Q&A SEGMENT ===\n"
    "first question comes from Brian Nowak with Morgan Stanley.\n\n"
    "B Brian Nowak How are you thinking about CapEx going forward? "
    "We see big numbers ahead.\n\n"
    "S Sundar Pichai We are investing opportunistically with a clear ROIC framework.\n"
)


def _seed_skip_budget(
    repo_root: Path, purpose: str, *, cap: float = 10.0, spend: float = 20.0
) -> None:
    """Create ``repo_root/data/portfolio.db`` with ``purpose`` in skip mode and
    a month-to-date spend over its cap, so ``budget_gate`` forgoes the call."""
    db_dir = repo_root / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(db_dir / "portfolio.db"))
    try:
        conn.executescript(
            """
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, called_at DATETIME NOT NULL,
                purpose VARCHAR(64), model VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL,
                prompt_chars INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL, cost_estimate_usd FLOAT);
            CREATE TABLE llm_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, purpose VARCHAR(64) NOT NULL,
                monthly_cap_usd NUMERIC(10,2) NOT NULL, warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
                hard_block BOOLEAN NOT NULL DEFAULT 0,
                on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (on_exceed IN ('skip', 'block', 'warn')),
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, notes TEXT,
                CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose));
            """
        )
        conn.execute(
            "INSERT INTO llm_budgets (purpose, monthly_cap_usd, warn_threshold_pct, hard_block, "
            "on_exceed, created_at, updated_at) VALUES (?, ?, 0.80, 0, 'skip', ?, ?)",
            (purpose, cap, now, now),
        )
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, model, prompt_sha256, prompt_chars, "
            "elapsed_ms, cost_estimate_usd) VALUES (?, ?, 'm', 'x', 1, 1, ?)",
            (now, purpose, spend),
        )
        conn.commit()
    finally:
        conn.close()


def _boom(*args: object, **kwargs: object) -> str:
    del args, kwargs
    raise AssertionError("LLM generator must not be called when budget-skipped")


def _thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK,
        thesis_full="NU is LatAm's leading digital bank.",
        break_conditions=["ROE sustains below 25% for two quarters"],
    )


def test_bear_case_budget_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_skip_budget(tmp_path, "bear_case", cap=10.0, spend=20.0)
    monkeypatch.setattr("report.sections.bear_case.generate_bear_case", _boom)
    section = bear_case.build(
        ticker="NU",
        repo_root=tmp_path,
        enable_llm=True,
        thesis=_thesis(),
        financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
        segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
        earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
        force_refresh=True,
    )
    assert isinstance(section, BearCaseSection)
    assert section.status == SectionStatus.BUDGET_SKIPPED
    assert section.budget_skip is not None
    assert section.budget_skip.purpose == "bear_case"
    assert section.budget_skip.cap_usd == 10.0
    assert section.budget_skip.spend_usd == 20.0
    assert section.failure_modes == []


def test_recent_developments_budget_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_skip_budget(tmp_path, "recent_developments")
    monkeypatch.setattr("report.sections.recent_developments.generate_recent_developments", _boom)
    section = recent_developments.build(
        ticker="NU", repo_root=tmp_path, enable_llm=True, force_refresh=True
    )
    assert isinstance(section, RecentDevelopmentsSection)
    assert section.status == SectionStatus.BUDGET_SKIPPED
    assert section.budget_skip is not None
    assert section.budget_skip.purpose == "recent_developments"
    assert section.content_md is None


def test_qa_roster_drops_topic_llm_on_budget_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Partial-LLM: the roster still renders (regex labels); only the topic-
    # labeling LLM is forgone, attributed via budget_skip — status stays OK.
    _seed_skip_budget(tmp_path, "qa_topics")
    monkeypatch.setattr("report.sections.qa_roster.generate_qa_topics", _boom)
    appendix = AppendixSection(
        status=SectionStatus.OK,
        transcripts=[
            TranscriptEntry(quarter="Q1", year=2026, source_path="/fake.txt", text=_QA_TEXT)
        ],
    )
    section = qa_roster.build(appendix=appendix, ticker="NU", repo_root=tmp_path, enable_llm=True)
    assert isinstance(section, QARosterSection)
    assert section.status == SectionStatus.OK
    assert section.budget_skip is not None
    assert section.budget_skip.purpose == "qa_topics"
    assert section.quarters and section.quarters[0].entries  # regex labels survived


def test_no_skip_without_budget_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Positive control: no portfolio.db → gate is a no-op → the LLM path runs
    # (here it would be called, so we stub a valid response instead of _boom).
    def _ok(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "### Material news\n- **X** — y. [Source: Z]"

    monkeypatch.setattr("report.sections.recent_developments.generate_recent_developments", _ok)
    section = recent_developments.build(
        ticker="NU", repo_root=tmp_path, enable_llm=True, force_refresh=True
    )
    assert section.status == SectionStatus.OK
    assert section.budget_skip is None
    assert section.content_md is not None
