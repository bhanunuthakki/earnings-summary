"""Tests for two reviewer-flagged issues in execution/comments_server.py:

1. CORS — the server never emits `Access-Control-Allow-Origin: *`. It echoes
   back only the file:// renderer's `null` Origin and loopback Origins (so the
   local dashboard works); a cross-site Origin gets no CORS header even when the
   server is bound to localhost (CSRF defense). For a non-loopback bind, the
   header is set to the request's Origin iff it is in
   `COMMENTS_SERVER_CORS_WHITELIST`.

2. Legacy report chat — all verbs fail closed with a durable-Copilot handoff,
   leaving no second persistence or mutation authority.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from capture import decision_draft_actions  # noqa: E402


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    """Minimal repo with an empty portfolio.db — enough for /healthz +
    /chat to spin up without create_app touching missing schema."""
    (tmp_path / "data").mkdir()
    sqlite3.connect(str(tmp_path / "data" / "portfolio.db")).close()
    return tmp_path


@pytest.fixture
def client(app_repo: Path):
    app = comments_server.create_app(app_repo)
    return app.test_client()


# --- CORS ---------------------------------------------------------------


def test_cors_null_origin_allowed_for_file_renderer(client):
    """The workspace HTML opens via file://, so its browser Origin is the
    literal string "null". That must be echoed back (never `*`)."""
    resp = client.get("/healthz", headers={"Origin": "null"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "null"


def test_cors_echoes_loopback_origin(client):
    """A page served by the dashboard itself carries a loopback Origin, which
    is echoed back so same-tool fetches keep working."""
    resp = client.get(
        "/healthz",
        base_url="http://127.0.0.1:7421",
        headers={"Origin": "http://127.0.0.1:7421"},
    )
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:7421"


def test_cors_blocks_cross_site_origin_even_on_localhost(client):
    """CSRF defense: a cross-site Origin gets NO CORS header even though the
    server is bound to localhost — so the browser blocks its preflighted,
    state-changing request. (Previously this path returned `*`.)"""
    resp = client.get(
        "/healthz",
        base_url="http://127.0.0.1:7421",
        headers={"Origin": "https://evil.example"},
    )
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_no_header_when_no_origin(client):
    """A same-origin / non-browser caller sends no Origin; no CORS header is
    needed and none is emitted."""
    resp = client.get("/healthz")
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_no_wildcard_for_non_localhost(client):
    """If someone runs --host 0.0.0.0 and a non-localhost client hits the
    server, the wildcard must not leak."""
    resp = client.get("/healthz", base_url="http://example.com")
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_echoes_whitelisted_origin(monkeypatch, client):
    monkeypatch.setenv("COMMENTS_SERVER_CORS_WHITELIST", "https://my.app,https://other.app")
    resp = client.get(
        "/healthz",
        base_url="http://example.com",
        headers={"Origin": "https://my.app"},
    )
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://my.app"


def test_cors_rejects_unlisted_origin(monkeypatch, client):
    monkeypatch.setenv("COMMENTS_SERVER_CORS_WHITELIST", "https://my.app")
    resp = client.get(
        "/healthz",
        base_url="http://example.com",
        headers={"Origin": "https://bad.app"},
    )
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_empty_whitelist_blocks_non_localhost(monkeypatch, client):
    """Default env (no whitelist) must not echo any origin for non-localhost."""
    monkeypatch.delenv("COMMENTS_SERVER_CORS_WHITELIST", raising=False)
    resp = client.get(
        "/healthz",
        base_url="http://example.com",
        headers={"Origin": "https://my.app"},
    )
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_service_config_allows_only_exact_private_origin(
    monkeypatch: pytest.MonkeyPatch, app_repo: Path
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_ALLOW_TAILSCALE", "1")
    monkeypatch.delenv("COMMENTS_SERVER_CORS_WHITELIST", raising=False)
    monkeypatch.delenv("EARNINGS_SUMMARY_PRIVATE_BASE_URL", raising=False)
    secret_dir = app_repo / "data" / "secrets"
    secret_dir.mkdir()
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(secret_dir))
    (secret_dir / "private_mobile_base_url").write_text(
        "https://desktop.example.ts.net\n",
        encoding="utf-8",
    )
    local_client = comments_server.create_app(app_repo).test_client()

    allowed = local_client.options(
        "/healthz",
        headers={"Origin": "https://desktop.example.ts.net"},
    )
    hostile = local_client.options(
        "/healthz",
        headers={"Origin": "https://attacker-funnel.example.ts.net"},
    )
    assert allowed.headers.get("Access-Control-Allow-Origin") == ("https://desktop.example.ts.net")
    assert "Access-Control-Allow-Origin" not in hostile.headers


def test_cors_methods_and_headers_always_set(client):
    """The Allow-Methods/Allow-Headers don't depend on host."""
    for base in ("http://localhost", "http://example.com"):
        resp = client.get("/healthz", base_url=base)
        assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")
        assert "Content-Type" in resp.headers.get("Access-Control-Allow-Headers", "")


def test_tracker_group_correction_route_uses_shared_action_core(
    monkeypatch: pytest.MonkeyPatch, client: FlaskClient
) -> None:
    captured: dict[str, object] = {}

    def fake_correct(
        draft_id: int,
        corrected_fields: dict[str, object],
        *,
        db_path: Path | str | None = None,
    ) -> dict[str, object]:
        captured.update(
            {"draft_id": draft_id, "corrected_fields": corrected_fields, "db_path": db_path}
        )
        return {"draft_id": draft_id, "decision_id": 11, "receipt": "tracker_group_corrected"}

    monkeypatch.setattr(
        decision_draft_actions,
        "correct_tracker_fill_group",
        fake_correct,
    )
    payload = {
        "proposed_ticker": "NU",
        "proposed_action": "buy",
        "proposed_amount_usd": 300.0,
    }

    response = client.post("/api/decision-draft-groups/7/correct", json=payload)

    assert response.status_code == 200
    assert response.get_json()["receipt"] == "tracker_group_corrected"
    assert captured["draft_id"] == 7
    assert captured["corrected_fields"] == payload


# --- Security hardening (dashboard is network-reachable over Tailscale) ---


def test_security_headers_present(client):
    """Every response carries the baseline security headers. X-Frame-Options is
    SAMEORIGIN (not DENY) because the command center embeds /reports/<T> in a
    same-origin iframe."""
    resp = client.get("/healthz")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


def test_healthz_does_not_leak_repo_root(client):
    """A network-reachable liveness endpoint must not disclose the server's
    absolute filesystem path."""
    resp = client.get("/healthz")
    assert resp.get_json() == {"status": "ok"}


def test_global_500_handler_redacts_and_drops_unsafe_traceback(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    app = client.application
    app.config["PROPAGATE_EXCEPTIONS"] = False
    leak_marker = "fixture-credential-material"

    def fail() -> str:
        raise RuntimeError(f"https://example.test?apikey={leak_marker}")

    app.add_url_rule("/_test/unhandled", "test_unhandled", fail)
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        response = client.get("/_test/unhandled")

    assert response.status_code == 500
    assert response.get_json()["error"] == "request failed; retry the request"
    assert leak_marker not in caplog.text
    assert "RuntimeError" in caplog.text


def test_handled_internal_failure_redacts_and_drops_unsafe_traceback(
    client: FlaskClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leak_marker = "-".join(("fixture", "credential", "material"))

    def fail_distill(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise RuntimeError(f"https://example.test?apikey={leak_marker}")

    monkeypatch.setattr(
        "synthesis.tenet_distill.run_tenet_distill",
        fail_distill,
    )
    app = client.application
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        response = client.post("/api/tenets/distill")

    assert response.status_code == 500
    assert response.get_json()["error"] == "distillation failed; retry the request"
    assert leak_marker not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "Traceback" not in caplog.text


def test_file_routes_reject_malformed_ticker(client):
    """The file-serving routes validate the ticker BEFORE it reaches a
    filesystem path — a malformed ticker is a 400, never a traversal. A
    well-shaped ticker passes validation (404 here: no build in the tmp repo)."""
    for route in ("/dcf/{}", "/reports/{}"):
        assert client.get(route.format("TOOLONGTICKER123")).status_code == 400
        assert client.get(route.format("NU")).status_code == 404


# --- Retired report chat ------------------------------------------------


def test_legacy_report_chat_routes_are_non_writing_migration_handoffs(
    client: FlaskClient, app_repo: Path
) -> None:
    """All legacy report-chat verbs fail closed and point to durable Ask."""
    legacy_dir = app_repo / "data" / "report_chats"
    database = app_repo / "data" / "portfolio.db"
    before_database = database.read_bytes()

    responses = (
        client.get("/chat/NU", query_string={"report_date": "2026-05-01"}),
        client.post(
            "/chat/NU",
            json={"report_date": "2026-05-01", "message": "change the thesis"},
        ),
        client.post(
            "/chat/NU/apply",
            json={"report_date": "2026-05-01", "proposal": {"target_path": "/thesis"}},
        ),
    )

    for response in responses:
        assert response.status_code == 410
        payload = response.get_json()
        assert payload["schema_version"] == "chat_migrated.v1"
        assert payload["status"] == "migrated"
        assert payload["replacement_url"] == "/#screen-copilot"
        assert payload["ticker"] == "NU"
    assert not legacy_dir.exists()
    assert database.read_bytes() == before_database


def test_ask_rejects_overlong_query_with_correlation_id(client: FlaskClient) -> None:
    resp = client.post(
        "/api/ask",
        json={"query": "x" * 8_001},
        headers={"X-Correlation-ID": "ask-limit-test"},
    )
    assert resp.status_code == 400
    assert resp.get_json() == {
        "error": "query exceeds the 8000 character limit",
        "correlation_id": "ask-limit-test",
    }


def test_ask_history_is_bounded_at_the_http_boundary(
    monkeypatch: pytest.MonkeyPatch, client: FlaskClient
) -> None:
    captured: dict[str, comments_server.AskTurn] = {}

    def _pack(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(default_tickers=[])

    def _respond(turn: comments_server.AskTurn, *_a: object, **_k: object):
        captured["turn"] = turn
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr(comments_server, "build_portfolio_pack", _pack)
    monkeypatch.setattr(comments_server, "respond_turn", _respond)
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "text": "z" * 2_000} for i in range(20)
    ]
    resp = client.post("/api/ask", json={"query": "bounded", "history": history})
    assert resp.status_code == 200
    turn = captured["turn"]
    assert len(turn.history) == 8
    assert all(len(item["text"]) == 1_200 for item in turn.history)


def test_buffered_ask_error_is_generic_and_correlated(
    monkeypatch: pytest.MonkeyPatch, client: FlaskClient
) -> None:
    def _pack(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(default_tickers=[])

    def _failed(*_a: object, **_k: object):
        yield {"type": "error", "error": "provider failed?api_key=secret-value"}

    monkeypatch.setattr(comments_server, "build_portfolio_pack", _pack)
    monkeypatch.setattr(comments_server, "respond_turn", _failed)
    resp = client.post(
        "/api/ask",
        json={"query": "what changed?"},
        headers={"X-Correlation-ID": "buffered-error-test"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["message"] == "ask failed; retry the request"
    assert payload["correlation_id"] == "buffered-error-test"
    assert "secret-value" not in resp.get_data(as_text=True)


def test_report_chat_ui_hands_off_to_the_durable_copilot() -> None:
    """Ask v2: the drawer client understands the engine's frame vocabulary —
    stage progress on the hint line, fragment rendering, and the
    context_spec refinement round-trip (lastSpec from fragment frames)."""
    from report.renderers import workspace_chat

    assert "openDurableCopilot" in workspace_chat.JS
    assert "Open in Copilot" in workspace_chat.JS
    assert "fetch(SERVER_URL + '/chat/'" not in workspace_chat.JS
    assert "'/apply'" not in workspace_chat.JS
