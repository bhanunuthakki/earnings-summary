"""Follow-up G — process_comments_for_ticker must PROPAGATE LLM hard-stops.

Budget/setup hard-stops (is_hard_stop) re-running won't help, so they must
surface — not get swallowed into a per-comment "error" result. Transient
errors still degrade per-comment (re-running might help). Mirrors the #210
policy that PR D enforced at the /api/thesis/<t>/preview boundary; this covers
the processor's own apply / sequence / synthesis paths.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import process_report_comments as prc  # noqa: E402

import comments  # noqa: E402
from llm.cli import LLMBudgetExceeded  # noqa: E402

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


def _raise_budget(*_a, **_k):
    raise LLMBudgetExceeded("monthly cap exceeded for company_description")


def _raise_transient(*_a, **_k):
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
