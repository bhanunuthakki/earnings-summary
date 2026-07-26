"""CSRF Origin-verification guard on the control-plane server (2026-06-18
refresh finding authz-3/authz-7).

The server is unauthenticated and binds loopback; a site the operator is
visiting could drive a cross-origin state-changing request at it. The
before_request guard refuses unsafe-method requests whose browser Origin is
cross-site, while leaving the loopback UI and the file:// ("null") workspace
renderer working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

# A POST route that returns a clean 400 on an empty body — lets a test assert
# "the guard let it through" (status != 403) without depending on handler internals.
_POST_ROUTE = "/chat/NU/apply"


@pytest.fixture
def client(tmp_path: Path):
    app = comments_server.create_app(tmp_path)
    return app.test_client()


def test_cross_origin_post_is_refused(client) -> None:
    r = client.post(_POST_ROUTE, json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert b"cross-origin" in r.data


def test_loopback_origin_passes_guard(client) -> None:
    r = client.post(_POST_ROUTE, json={}, headers={"Origin": "http://127.0.0.1:7421"})
    assert r.status_code != 403  # guard passes; handler 400s on the empty body


def test_null_origin_requires_report_capability(client) -> None:
    r = client.post(_POST_ROUTE, json={}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_null_origin_with_report_capability_passes_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "test-report-capability"
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", token)
    app = comments_server.create_app(tmp_path)
    r = app.test_client().post(
        _POST_ROUTE,
        json={},
        headers={
            "Origin": "null",
            "X-Report-Capability": token,
        },
    )
    assert r.status_code != 403


def test_non_tailnet_client_is_refused_in_tailscale_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_ALLOW_TAILSCALE", "1")
    app = comments_server.create_app(tmp_path)
    r = app.test_client().get(
        "/healthz",
        environ_base={"REMOTE_ADDR": "192.168.1.40"},
    )
    assert r.status_code == 403


def test_tailnet_client_and_origin_pass_in_tailscale_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_ALLOW_TAILSCALE", "1")
    app = comments_server.create_app(tmp_path)
    r = app.test_client().post(
        _POST_ROUTE,
        json={},
        base_url="http://100.100.1.2:7421",
        environ_base={"REMOTE_ADDR": "100.100.1.3"},
        headers={"Origin": "http://100.100.1.2:7421"},
    )
    assert r.status_code != 403


def test_absent_origin_passes_guard(client) -> None:
    # Local CLI / curl / same-origin non-browser callers send no Origin.
    r = client.post(_POST_ROUTE, json={})
    assert r.status_code != 403


def test_options_preflight_is_exempt(client) -> None:
    r = client.options(_POST_ROUTE, headers={"Origin": "https://evil.example"})
    assert r.status_code != 403


def test_safe_get_is_exempt(client) -> None:
    r = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
