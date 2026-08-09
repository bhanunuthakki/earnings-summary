"""PR D — /api/thesis/<t>/preview: the LLM-degradation contract.

The preview routes Opus directly (apply=False), so this locks the project rule:
- transient / unparseable LLM response -> 200 degraded (component scope),
- budget/setup hard stop -> propagate (402/503),
- and a preview NEVER writes the holdings JSON.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Protocol, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
import process_report_comments as prc  # noqa: E402

import comments  # noqa: E402
from llm.cli import LLMBudgetExceeded  # noqa: E402
from llm.structured import StructuredParseError  # noqa: E402

_HOLDINGS = {
    "ticker": "NU",
    "name": "Nu Holdings",
    "thesis": "Original thesis about ROE.",
    "verdict": "Pending",
}
_BEFORE = "Original thesis about ROE."


class _Validator(Protocol):
    def validate_python(self, value: object) -> object: ...


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    sqlite3.connect(str(tmp_path / "data" / "portfolio.db")).close()  # create_app wants the path
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps(_HOLDINGS), encoding="utf-8")
    comments.append_comment(
        tmp_path,
        "NU",
        date(2026, 5, 18),
        anchor=comments.Anchor(type="thesis_lede", key="thesis_lede"),
        text="Tighten the ROE language.",
        selected_text=None,
        intent="edit_thesis",
    )
    return tmp_path


@pytest.fixture
def client(repo: Path):
    return comments_server.create_app(repo).test_client()


def _on_disk_thesis(repo: Path) -> str:
    data = json.loads((repo / "micro_thesis" / "holdings" / "NU.json").read_text(encoding="utf-8"))
    return data["thesis"]


def _structured_thesis_response(*_args: object, **kwargs: object) -> object:
    schema = cast("_Validator", kwargs["schema"])
    return schema.validate_python(
        {"revised_thesis": "Revised: ROE durably above 30%.", "diff_summary": "tightened"}
    )


def _unparseable_structured_response(*_args: object, **_kwargs: object) -> object:
    raise StructuredParseError("structured response invalid", raw_head="redacted")


def test_preview_happy_returns_before_after_diff(client, repo, monkeypatch) -> None:
    monkeypatch.setattr(prc, "call_llm_structured", _structured_thesis_response)
    resp = client.post("/api/thesis/NU/preview", json={"report_date": "2026-05-18"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["before_thesis"] == _BEFORE
    assert body["after_thesis"] == "Revised: ROE durably above 30%."
    assert body["thesis_diff"]  # non-empty unified diff
    assert _on_disk_thesis(repo) == _BEFORE  # preview never writes


def test_preview_degrades_on_empty_response(client, repo, monkeypatch) -> None:
    monkeypatch.setattr(prc, "call_llm_structured", _unparseable_structured_response)
    resp = client.post("/api/thesis/NU/preview", json={"report_date": "2026-05-18"})
    assert resp.status_code == 200
    assert resp.get_json()["degraded"] is True
    assert _on_disk_thesis(repo) == _BEFORE


def test_preview_degrades_on_unparseable_response(client, repo, monkeypatch) -> None:
    monkeypatch.setattr(prc, "call_llm_structured", _unparseable_structured_response)
    resp = client.post("/api/thesis/NU/preview", json={"report_date": "2026-05-18"})
    assert resp.status_code == 200
    assert resp.get_json()["degraded"] is True


def test_preview_propagates_budget_hard_stop(client, repo, monkeypatch) -> None:
    def _boom(*a, **k):
        raise LLMBudgetExceeded("monthly cap exceeded?api_key=secret-value")

    monkeypatch.setattr(comments_server, "preview_thesis_edits", _boom)
    resp = client.post(
        "/api/thesis/NU/preview",
        json={"report_date": "2026-05-18"},
        headers={"X-Correlation-ID": "thesis-budget-test"},
    )
    assert resp.status_code == 402  # propagated, not degraded
    assert resp.get_json() == {
        "error": "thesis preview unavailable; retry the request",
        "correlation_id": "thesis-budget-test",
    }
    assert "secret-value" not in resp.get_data(as_text=True)
    assert _on_disk_thesis(repo) == _BEFORE


def test_preview_unexpected_failure_is_generic_and_correlated(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("provider failed?api_key=secret-value")

    monkeypatch.setattr(comments_server, "preview_thesis_edits", _boom)
    resp = client.post(
        "/api/thesis/NU/preview",
        json={"report_date": "2026-05-18"},
        headers={"X-Correlation-ID": "thesis-degraded-test"},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {
        "degraded": True,
        "reason": "thesis preview unavailable; retry the request",
        "correlation_id": "thesis-degraded-test",
    }
    assert "secret-value" not in resp.get_data(as_text=True)


def test_preview_requires_report_date(client) -> None:
    resp = client.post("/api/thesis/NU/preview", json={})
    assert resp.status_code == 400


def test_preview_no_relevant_comments_is_empty(client, repo, monkeypatch) -> None:
    # An empty-string LLM would crash IF a router ran — assert it never runs
    # when there are no edit_thesis/edit_structured comments for the date.
    monkeypatch.setattr(prc, "call_llm_structured", _unparseable_structured_response)
    resp = client.post("/api/thesis/NU/preview", json={"report_date": "2026-01-01"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comment_ids"] == []
    assert body["after_thesis"] is None
