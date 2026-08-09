"""Follow-up J — Apply reuses the Preview's thesis draft (one LLM call, no drift).

preview_thesis_edits caches the revised thesis keyed by (current thesis + exact
comment set). A subsequent apply reuses it instead of re-calling the model — but
ONLY when nothing changed in between (else the key misses and it re-runs).
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

_DATE = date(2026, 5, 18)
_REVISED = "REVISED THESIS TEXT"


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_a: object, **_k: object) -> object:
        self.calls += 1
        return prc._ThesisRevision(revised_thesis=_REVISED, diff_summary="tightened")


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


def _disk_thesis(repo: Path) -> str:
    return json.loads((repo / "micro_thesis" / "holdings" / "NU.json").read_text(encoding="utf-8"))[
        "thesis"
    ]


def _set_thesis(repo: Path, text: str) -> None:
    p = repo / "micro_thesis" / "holdings" / "NU.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["thesis"] = text
    p.write_text(json.dumps(data), encoding="utf-8")


def test_apply_reuses_preview_draft(repo: Path, monkeypatch) -> None:
    llm = _CountingLLM()
    monkeypatch.setattr(prc, "call_llm_structured", llm)

    prev = prc.preview_thesis_edits(repo, "NU", _DATE)
    assert llm.calls == 1
    assert prev["after_thesis"] == _REVISED

    prc.process_comments_for_ticker(
        repo, "NU", _DATE, apply=True, clear=False, sequence=False, auto_rebuild=False
    )
    assert llm.calls == 1  # reused the cached draft — no second LLM call
    assert _disk_thesis(repo) == _REVISED  # and wrote exactly what was previewed


def test_cache_miss_when_thesis_changed(repo: Path, monkeypatch) -> None:
    llm = _CountingLLM()
    monkeypatch.setattr(prc, "call_llm_structured", llm)

    prc.preview_thesis_edits(repo, "NU", _DATE)
    assert llm.calls == 1

    _set_thesis(repo, "A completely different thesis now.")  # invalidates the key
    prc.process_comments_for_ticker(
        repo, "NU", _DATE, apply=True, clear=False, sequence=False, auto_rebuild=False
    )
    assert llm.calls > 1  # cache key no longer matches → fresh LLM call


def test_preview_reuses_its_own_cache(repo: Path, monkeypatch) -> None:
    llm = _CountingLLM()
    monkeypatch.setattr(prc, "call_llm_structured", llm)
    prc.preview_thesis_edits(repo, "NU", _DATE)
    prc.preview_thesis_edits(repo, "NU", _DATE)  # same state → cache hit
    assert llm.calls == 1
