"""Hermetic route contracts for immediate shells and bounded cache admission."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))
import comments_server
from comments_server_panel_cache import PanelCacheBusy, PanelResponseCache


def test_cache_busy_returns_retryable_uncached_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def busy(_self: PanelResponseCache, _key: str) -> PanelCacheBusy:
        return PanelCacheBusy(2)

    monkeypatch.setattr(PanelResponseCache, "get_or_reserve", busy)
    app = comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
    response = app.test_client().get("/api/panel/portfolio_record")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.headers["X-Panel-Cache"] == "busy"
    assert response.headers["Cache-Control"] == "no-store"
    assert "ETag" not in response.headers
    assert "Server-Timing" in response.headers
    assert response.get_json()["error"]


def test_live_composites_request_lazy_shells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import pipeline.performance_risk_panel as performance
    import pipeline.portfolio_console_panel as record

    calls: list[dict[str, object]] = []

    def shell(*args: object, **kwargs: object) -> str:
        calls.append(kwargs)
        return "IMMEDIATE SHELL"

    def forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("The shell route must not build a detail section")

    monkeypatch.setattr(record, "render_portfolio_record_panel", shell)
    monkeypatch.setattr(record, "render_portfolio_record_fragment", forbidden)
    monkeypatch.setattr(performance, "render_performance_risk_panel", shell)
    monkeypatch.setattr(performance, "render_performance_risk_fragment", forbidden)
    app = comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
    client = app.test_client()
    for name in ("portfolio_record", "performance_risk"):
        response = client.get("/api/panel/" + name)
        assert response.status_code == 200
        assert response.get_data(as_text=True) == "IMMEDIATE SHELL"
        assert calls[-1]["lazy"] is True
        assert client.get("/api/panel/" + name).headers["X-Panel-Cache"] == "hit"
    assert len(calls) == 2


@pytest.mark.parametrize("fragment", ["brief", "decisions", "research-items", "memos", "triggers"])
def test_record_dispatches_exact_fragment(
    fragment: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import pipeline.portfolio_console_panel as record

    def render(db_path: Path, selected: str, **kwargs: object) -> str:
        assert db_path == tmp_path / "unused.db"
        assert selected == fragment
        return "ONLY " + selected

    monkeypatch.setattr(record, "render_portfolio_record_fragment", render)
    app = comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
    response = app.test_client().get("/api/panel/portfolio_record?fragment=" + fragment)
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ONLY " + fragment
    assert app.test_client().get("/api/panel/portfolio_record?fragment=unknown").status_code == 404


@pytest.mark.parametrize("fragment", ["performance", "allocation", "posture"])
def test_performance_dispatch_preserves_window(
    fragment: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import pipeline.performance_risk_panel as performance

    def render(db_path: Path, repo_root: Path, selected: str, **kwargs: object) -> str:
        assert selected == fragment
        assert kwargs == {
            "start_date": "2026-01-01",
            "end_date": "2026-06-30",
            "include_backfill": True,
        }
        return "ONLY " + selected

    monkeypatch.setattr(performance, "render_performance_risk_fragment", render)
    app = comments_server.create_app(tmp_path, db_path=tmp_path / "unused.db")
    response = app.test_client().get(
        "/api/panel/performance_risk?fragment="
        + fragment
        + "&start_date=2026-01-01&end_date=2026-06-30&include_backfill=1"
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ONLY " + fragment
