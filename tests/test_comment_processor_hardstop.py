# pyright: reportPrivateUsage=false
#
# A few tests drive the processor's module-private routers directly
# (`_synthesize_holdings_coherence`, `_route_extract_kpi`) to exercise each
# fixed `except` branch in isolation. The module-level directive silences only
# the cross-module private-usage rule, matching the repo convention (see
# test_llm_call_exception_resilience.py's header for the same).
"""Follow-up G — process_comments_for_ticker must PROPAGATE LLM hard-stops.

Budget/setup hard-stops (is_hard_stop) re-running won't help, so they must
surface — not get swallowed into a per-comment "error" result. Transient
errors still degrade per-comment (re-running might help). Mirrors the #210
policy that PR D enforced at the /api/thesis/<t>/preview boundary; this covers
the processor's own draft / apply / sequence / synthesis / extract_kpi paths,
for both hard-stop types (LLMBudgetExceeded and LLMSetupError).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import process_report_comments as prc  # noqa: E402

import comments  # noqa: E402
from llm.cli import LLMBudgetExceeded, LLMSetupError  # noqa: E402

_DATE = date(2026, 5, 18)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        json.dumps({"ticker": "NU", "name": "Nu", "thesis": "Original.", "verdict": "Pending"}),
        encoding="utf-8",
    )
    comments.append_comment(
        tmp_path,
        "NU",
        _DATE,
        anchor=comments.Anchor(type="thesis_lede", key="thesis_lede"),
        text="Tighten ROE.",
        selected_text=None,
        intent="edit_thesis",
    )
    return tmp_path


def _raise_budget(*_a: object, **_k: object):
    raise LLMBudgetExceeded("monthly cap exceeded for company_description")


def _raise_transient(*_a: object, **_k: object):
    raise ValueError("transient LLM hiccup")


def test_hardstop_propagates_on_dry_run(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(prc, "call_llm", _raise_budget)
    with pytest.raises(LLMBudgetExceeded):
        prc.process_comments_for_ticker(repo, "NU", _DATE, apply=False, clear=False)


def test_hardstop_propagates_on_apply(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(prc, "call_llm", _raise_budget)
    with pytest.raises(LLMBudgetExceeded):
        prc.process_comments_for_ticker(repo, "NU", _DATE, apply=True, clear=False)
    # And the thesis on disk is untouched (we bailed, didn't half-apply).
    data = json.loads((repo / "micro_thesis" / "holdings" / "NU.json").read_text(encoding="utf-8"))
    assert data["thesis"] == "Original."


def test_transient_error_still_degrades_per_comment(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(prc, "call_llm", _raise_transient)
    res = prc.process_comments_for_ticker(repo, "NU", _DATE, apply=False, clear=False)
    assert res["applied"] is False
    assert any(r["status"] == "error" for r in res["results"])  # degraded, not raised


# ---------------------------------------------------------------------------
# Extra coverage beyond the draft/apply pair above: the OTHER hard-stop type,
# plus the sequence / synthesis / extract_kpi guards — each has its own
# `except` branch the two tests above don't reach.
# ---------------------------------------------------------------------------


def _raise_setup(*_a: object, **_k: object):
    raise LLMSetupError("Claude Code CLI ('claude') not found in PATH.")


def test_setup_error_also_propagates(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The other hard-stop type (CLI not installed) propagates too — not just budget.
    monkeypatch.setattr(prc, "call_llm", _raise_setup)
    with pytest.raises(LLMSetupError):
        prc.process_comments_for_ticker(repo, "NU", _DATE, apply=True, clear=False)


def test_hardstop_propagates_in_sequencer(monkeypatch: pytest.MonkeyPatch) -> None:
    # PASS 2: the sequencer ranks >=2 editable comments via one LLM call. A cap
    # there must propagate, not silently fall back to the original order.
    c1 = comments.Comment(
        id="c1",
        anchor=comments.Anchor(type="thesis_lede", key="thesis_lede"),
        comment="reframe the bet",
        intent="edit_thesis",
    )
    c2 = comments.Comment(
        id="c2",
        anchor=comments.Anchor(type="kpi_ledger_row", key="ROE"),
        comment="add an ROE break rule",
        intent="edit_structured",
    )
    plan: list[dict[str, object]] = [
        {"comment": c1, "intent": "edit_thesis", "dry": {"diff_summary": "d1"}, "_error": None},
        {"comment": c2, "intent": "edit_structured", "dry": {"diff_summary": "d2"}, "_error": None},
    ]
    monkeypatch.setattr(prc, "call_llm", _raise_budget)
    with pytest.raises(LLMBudgetExceeded):
        prc.sequence_resolutions(plan, "NU")


def test_hardstop_propagates_in_synthesis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PASS 3.5: _synthesize_holdings_coherence needs structured fields present
    # (the `repo` fixture's holdings has none, so it would no-op). Give it one.
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        json.dumps({"ticker": "NU", "thesis": "Original.", "tier_1_kpis": [{"name": "ROE"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prc, "call_llm", _raise_budget)
    with pytest.raises(LLMBudgetExceeded):
        prc._synthesize_holdings_coherence(tmp_path, "NU")


def _extract_kpi_setup(tmp_path: Path) -> comments.Comment:
    """A portfolio.db with one kpi_definitions row + a kpi_ledger_row comment —
    the minimum _route_extract_kpi needs to reach its LLM call."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    try:
        conn.execute(
            "CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY, ticker TEXT, name TEXT, unit TEXT)"
        )
        conn.execute(
            "INSERT INTO kpi_definitions (ticker, name, unit) VALUES ('NU', 'ROE', 'percent')"
        )
        conn.commit()
    finally:
        conn.close()
    return comments.append_comment(
        tmp_path,
        "NU",
        _DATE,
        anchor=comments.Anchor(type="kpi_ledger_row", key="ROE"),
        text="ROE in Q3 2025 was 32.0%.",
        selected_text=None,
        intent="extract_kpi",
    )


def test_hardstop_propagates_in_extract_kpi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _route_extract_kpi catches Exception around its OWN call_llm, so without
    # the guard a cap would be swallowed into a per-comment summary BEFORE the
    # apply loop could see it (and the comment would be marked "addressed").
    c = _extract_kpi_setup(tmp_path)
    monkeypatch.setattr(prc, "call_llm", _raise_budget)
    with pytest.raises(LLMBudgetExceeded):
        prc._route_extract_kpi(tmp_path, "NU", c, apply=False)


def test_extract_kpi_transient_still_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _extract_kpi_setup(tmp_path)
    monkeypatch.setattr(prc, "call_llm", _raise_transient)
    out = cast("dict[str, object]", prc._route_extract_kpi(tmp_path, "NU", c, apply=False))
    assert "extract_kpi: error" in str(out.get("summary"))  # degraded, not raised
