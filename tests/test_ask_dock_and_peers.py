"""Ask v4 surfaces: the Home Ask dock (markup + pop-out handoff contract)
and the /api/peers/<ticker> route (the PR #400 scored comparable set the
"+ Peers" actions inject)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from pipeline.ask_dock import render_ask_dock
from pipeline.command_center_shell import render_overview_panel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

# ----------------------------------------------------------------------------
# The Home dock
# ----------------------------------------------------------------------------


def test_dock_fragment_is_self_contained() -> None:
    html = render_ask_dock()
    for dom_id in (
        "ask-dock",
        "ask-dock-toggle",
        "ask-dock-thread",
        "ask-dock-form",
        "ask-dock-q",
        "ask-dock-pop",
    ):
        assert f'id="{dom_id}"' in html
    # Same engine as the Ask tab, collapsed by default.
    assert "/api/ask/stream" in html
    assert 'data-collapsed="1"' in html
    # It renders the full event vocabulary: stages, prose, views, citations.
    for marker in ("'stage'", "'delta'", "'fragment'", "'final'", "'citations'", "'error'"):
        assert marker in html


def test_dock_popout_hands_thread_to_the_ask_tab() -> None:
    """The ⇗ pop-out uses the palette's handoff contract: thread under
    cc-ask-thread, pending input under cc-ask-q, jump to #explore + event."""
    html = render_ask_dock()
    assert "cc-ask-thread" in html
    assert "cc-ask-q" in html
    assert "#explore" in html
    assert "new Event('cc-ask-q')" in html


def test_overview_panel_carries_the_dock() -> None:
    plain = render_overview_panel({}, None)
    assert 'id="ask-dock"' in plain
    with_rail = render_overview_panel({}, None, inbox_html="<div>inbox</div>")
    assert 'id="ask-dock"' in with_rail
    assert "cc-home-grid" in with_rail


# ----------------------------------------------------------------------------
# /api/peers/<ticker>
# ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    (tmp_path / "data").mkdir()
    return comments_server.create_app(tmp_path).test_client()


def test_peers_api_returns_scored_set(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from report.sections import p3_data

    def fake_load(ticker: str, *, repo_root: Path, max_peers: int = 6) -> list[object]:
        assert ticker == "NU"
        return [
            p3_data.PeerCompRow(
                peer_ticker="MELI",
                peer_name="MercadoLibre",
                market_cap_usd=None,
                revenue_ttm_usd=None,
                net_margin_ttm=None,
                roic_ttm=None,
                match_reasons=("named rival", "same industry"),
            )
        ]

    monkeypatch.setattr(p3_data, "load_peer_comp", fake_load)
    res = client.get("/api/peers/nu")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ticker"] == "NU"
    assert body["peers"] == [
        {"ticker": "MELI", "name": "MercadoLibre", "reasons": ["named rival", "same industry"]}
    ]


def test_peers_api_degrades_on_lookup_failure(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report.sections import p3_data

    def boom(*_a: object, **_kw: object) -> list[object]:
        raise RuntimeError("no peer file")

    monkeypatch.setattr(p3_data, "load_peer_comp", boom)
    res = client.get("/api/peers/NU")
    assert res.status_code == 200  # best-effort surface, never a 500
    body = res.get_json()
    assert body["peers"] == []
    assert "peer lookup failed" in body["error"]
