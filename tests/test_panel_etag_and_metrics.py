"""S14 perceived-latency server seams: ETag/304 on /api/panel/* + the
in-memory panel-timing metrics endpoints.

The ETag layer is an after_request hook — every 200-OK GET panel fragment
carries a content ETag and `Cache-Control: no-cache`, and a matching
If-None-Match revalidation comes back 304 with an empty body (the
stale-while-revalidate client then keeps its cached copy). The metrics pair
(`POST`/`GET /api/metrics/panel`) is a deque ring for latency — plus, since
navigation_ia §5 (instrument-first), a durable ``panel_activation_counts``
(panel_id, day) counter bumped only by user-perceived activations (cold|swr).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite3.connect(str(data_dir / "portfolio.db")).close()
    app = comments_server.create_app(tmp_path)
    return app.test_client()


# ---------------------------------------------------------------------------
# ETag / 304 on panel fragments
# ---------------------------------------------------------------------------


def test_panel_fragment_carries_etag_and_no_cache(client: FlaskClient) -> None:
    # /api/panel/actions renders with no DB dependency — fully deterministic.
    resp = client.get("/api/panel/actions")
    assert resp.status_code == 200
    assert resp.headers.get("ETag")
    assert resp.headers.get("Cache-Control") == "no-cache"
    assert resp.data  # the fragment body itself


def test_panel_fragment_revalidation_is_a_304(client: FlaskClient) -> None:
    first = client.get("/api/panel/actions")
    etag = first.headers["ETag"]
    again = client.get("/api/panel/actions", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.data == b""
    # A stale validator gets the full body back.
    stale = client.get("/api/panel/actions", headers={"If-None-Match": '"deadbeef"'})
    assert stale.status_code == 200
    assert stale.data == first.data


def test_etag_scope_is_panel_gets_only(client: FlaskClient) -> None:
    """The hook must not touch non-panel routes (e.g. the JSON healthz) —
    they keep their default cache headers."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") != "no-cache"
    # 404 panels pass through the hook untouched.
    missing = client.get("/api/panel/definitely_not_a_panel")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Panel timing metrics ring
# ---------------------------------------------------------------------------


def test_metrics_post_then_aggregate(client: FlaskClient) -> None:
    samples = [
        {
            "panel": "portfolio",
            "cache": "cold",
            "fetch_ms": 410.0,
            "render_ms": 40,
            "total_ms": 450,
        },
        {"panel": "portfolio", "cache": "swr", "render_ms": 12, "total_ms": 12.4},
        {
            "panel": "portfolio",
            "cache": "revalidate",
            "fetch_ms": 95,
            "total_ms": 95,
            "status": 304,
        },
        {"panel": "evals", "cache": "prefetch", "total_ms": 8},
    ]
    for s in samples:
        assert client.post("/api/metrics/panel", json=s).status_code == 204

    agg = client.get("/api/metrics/panel").get_json()
    assert agg["samples"] == 4
    by_key = {(r["panel"], r["cache"]): r for r in agg["rows"]}
    assert by_key[("portfolio", "cold")]["n"] == 1
    assert by_key[("portfolio", "cold")]["p50_ms"] == 450
    assert by_key[("evals", "prefetch")]["p50_ms"] == 8
    # Perceived = activations only; the 95ms background revalidate is excluded
    # (with it, p95 of {450, 12.4, 8} could not sit at 450).
    assert by_key[("portfolio", "revalidate")]["n"] == 1
    assert agg["perceived_p95_ms"] == 450
    assert agg["perceived_p50_ms"] == pytest.approx(12.4, abs=0.5)


def test_metrics_post_rejects_malformed(client: FlaskClient) -> None:
    assert client.post("/api/metrics/panel", json={"cache": "cold"}).status_code == 400
    assert (
        client.post("/api/metrics/panel", json={"panel": "x", "cache": "warp"}).status_code == 400
    )
    # Out-of-range / non-numeric timings are dropped to null, not 400.
    ok = client.post(
        "/api/metrics/panel",
        json={"panel": "x", "cache": "cold", "total_ms": "fast", "fetch_ms": -3},
    )
    assert ok.status_code == 204
    agg = client.get("/api/metrics/panel").get_json()
    # The null-total sample is held in the ring but aggregates to no row.
    assert agg["samples"] == 1
    assert agg["rows"] == []


def test_activation_counts_persist_per_panel_per_day(client: FlaskClient, tmp_path: Path) -> None:
    """navigation_ia §5: cold|swr samples bump the durable (panel_id, day)
    counter — prefetch/revalidate are speculative/background and must NOT
    count as visits. The GET aggregate surfaces the 30-day totals."""
    samples = [
        {"panel": "musings", "cache": "cold", "total_ms": 100},
        {"panel": "musings", "cache": "swr", "total_ms": 10},
        {"panel": "musings", "cache": "prefetch", "total_ms": 8},  # not a visit
        {"panel": "musings", "cache": "revalidate", "total_ms": 90},  # background
        {"panel": "portfolio_synthesis", "cache": "cold", "total_ms": 200},
    ]
    for s in samples:
        assert client.post("/api/metrics/panel", json=s).status_code == 204
    # Durable: the counter lives in the DB, not the ring.
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    try:
        rows = dict(
            conn.execute(
                "SELECT panel_id, SUM(count) FROM panel_activation_counts GROUP BY panel_id"
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows == {"musings": 2, "portfolio_synthesis": 1}
    agg = client.get("/api/metrics/panel").get_json()
    assert agg["activations_30d"] == {"musings": 2, "portfolio_synthesis": 1}


def test_source_calls_panel_carries_latency_readout(client: FlaskClient) -> None:
    """System → Data Cache surfaces the timings: the fragment ships the
    latency section + the script that fetches the GET aggregate — including
    on the empty-DB degrade path."""
    resp = client.get("/api/panel/source_calls")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="sc-panel-latency"' in html
    assert "'/api/metrics/panel'" in html
    assert "Panel latency" in html
