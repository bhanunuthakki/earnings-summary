"""Tests for two reviewer-flagged issues in execution/comments_server.py:

1. CORS — the server never emits `Access-Control-Allow-Origin: *`. It echoes
   back only the file:// renderer's `null` Origin and loopback Origins (so the
   local dashboard works); a cross-site Origin gets no CORS header even when the
   server is bound to localhost (CSRF defense). For a non-loopback bind, the
   header is set to the request's Origin iff it is in
   `COMMENTS_SERVER_CORS_WHITELIST`.

2. Concurrent chat — the LLM subprocess is now dispatched to a dedicated
   thread pool, so two concurrent `/chat/<ticker>` requests proceed in
   parallel. We assert that with `threading.Barrier(2)`: if requests are
   serialized, only one ever reaches the fake stream and the barrier
   times out.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402


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


def test_cors_methods_and_headers_always_set(client):
    """The Allow-Methods/Allow-Headers don't depend on host."""
    for base in ("http://localhost", "http://example.com"):
        resp = client.get("/healthz", base_url=base)
        assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")
        assert "Content-Type" in resp.headers.get("Access-Control-Allow-Headers", "")


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


def test_file_routes_reject_malformed_ticker(client):
    """The file-serving routes validate the ticker BEFORE it reaches a
    filesystem path — a malformed ticker is a 400, never a traversal. A
    well-shaped ticker passes validation (404 here: no build in the tmp repo)."""
    for route in ("/dcf/{}", "/reports/{}"):
        assert client.get(route.format("TOOLONGTICKER123")).status_code == 400
        assert client.get(route.format("NU")).status_code == 404


# --- Concurrent chat ----------------------------------------------------


def test_chat_dispatches_to_pool_and_streams_chunks(monkeypatch, client):
    """Smoke-test the new pool path — chunks from a fake stream_response
    flow through the queue and into the SSE response."""

    # extra_context carries grounding evidence and the S7 NEED-protocol —
    # armed turns always pass it, so fakes must accept it.
    def fake_stream_response(*, repo_root, ticker, report_date, user_message, extra_context=""):
        yield {"type": "delta", "text": f"hello {ticker}"}
        yield {"type": "final", "text": "done"}

    monkeypatch.setattr(
        comments_server.build_chat_response,
        "stream_response",
        fake_stream_response,
    )
    resp = client.post(
        "/chat/NU",
        json={"report_date": "2026-05-01", "message": "test"},
    )
    body = resp.get_data(as_text=True)
    assert resp.mimetype == "text/event-stream"
    assert resp.headers.get("Cache-Control") == "no-cache"
    assert "hello NU" in body
    assert '"type": "final"' in body
    # SSE frame format preserved
    assert body.startswith("data: ")
    assert "\n\n" in body


def test_chat_surfaces_subprocess_errors_as_sse(monkeypatch, client):
    """If stream_response raises mid-flight, the pool catches it and
    emits an error SSE frame instead of leaving the queue stuck."""

    def boom(*, repo_root, ticker, report_date, user_message, extra_context=""):
        yield {"type": "delta", "text": "partial"}
        raise RuntimeError("subprocess died")

    monkeypatch.setattr(comments_server.build_chat_response, "stream_response", boom)
    resp = client.post(
        "/chat/NU",
        json={"report_date": "2026-05-01", "message": "test"},
    )
    body = resp.get_data(as_text=True)
    assert "partial" in body
    assert "chat stream failed" in body
    # The client gets a stable retry message, while the detailed exception is
    # retained only in the redacted server log.
    assert "subprocess died" not in body


def test_chat_data_question_streams_view_fragment(
    monkeypatch: pytest.MonkeyPatch, client: FlaskClient, app_repo: Path
) -> None:
    """The unified engine inside /chat/<ticker>: a metric-shaped question
    routes to the ViewSpec path and streams a fragment frame + the message
    line as final — no narrative LLM involved (the conftest guard would
    trip if it were)."""
    from types import SimpleNamespace

    import ask.engine as ask_engine
    from viewspec import nl_compile
    from viewspec.spec import ViewSpec

    spec = ViewSpec.from_dict(
        {
            "tickers": ["NU"],
            "metrics": ["fin:revenue"],
            "transform": "yoy",
            "cadence": "quarterly",
            "periods": 8,
        }
    )

    def fake_compile(query: str, **_kw: object) -> nl_compile.NLCompileResult:
        return nl_compile.NLCompileResult(status="ok", spec=spec)

    def fake_execute(s: ViewSpec, *, db_path: Path) -> SimpleNamespace:
        # Faithful ViewResult shape: respond_turn summarizes via view_summary(),
        # which reads spec + period_labels + each row's cells.
        return SimpleNamespace(
            spec=s,
            period_labels=[f"P{i}" for i in range(s.periods)],
            rows=[SimpleNamespace(cells=[])],
        )

    def fake_render(view: object, **_kw: object) -> str:
        return "<div>FRAG</div>"

    monkeypatch.setattr(nl_compile, "compile_nl_to_viewspec", fake_compile)
    monkeypatch.setattr(ask_engine, "execute_view", fake_execute)
    monkeypatch.setattr(ask_engine, "render_view_fragment", fake_render)
    resp = client.post(
        "/chat/NU",
        json={"report_date": "2026-05-01", "message": "NU revenue growth, last 8 quarters"},
    )
    body = resp.get_data(as_text=True)
    assert resp.mimetype == "text/event-stream"
    assert '"type": "fragment"' in body
    assert "FRAG" in body
    assert "1 series" in body
    # The data turn persisted to the per-report thread (ticker pack).
    thread_file = app_repo / "data" / "report_chats" / "NU" / "2026-05-01.json"
    assert thread_file.exists()


def test_chat_discovery_command_short_circuits(client: FlaskClient) -> None:
    """/help answers deterministically through the engine's command route —
    instant SSE reply, no LLM (conftest guard enforces it)."""
    resp = client.post("/chat/NU", json={"report_date": "2026-05-01", "message": "/help"})
    body = resp.get_data(as_text=True)
    assert '"type": "final"' in body
    assert "/discovery" in body


def test_chat_drawer_js_carries_engine_contract() -> None:
    """Ask v2: the drawer client understands the engine's frame vocabulary —
    stage progress on the hint line, fragment rendering, and the
    context_spec refinement round-trip (lastSpec from fragment frames)."""
    from report.renderers import workspace_chat

    assert "context_spec" in workspace_chat.JS
    assert "'fragment'" in workspace_chat.JS
    assert "'stage'" in workspace_chat.JS
    assert "lastSpec" in workspace_chat.JS


def test_concurrent_chat_requests_proceed_in_parallel(monkeypatch, client):
    """Two concurrent /chat requests must both reach the LLM call. We
    enforce this with a 2-way Barrier: if the second request is held
    waiting for the first, the barrier times out and the test fails.

    This guards against regressions where the chat endpoint inadvertently
    holds a lock or otherwise serializes calls.
    """
    barrier = threading.Barrier(2, timeout=5)

    def fake_stream_response(*, repo_root, ticker, report_date, user_message, extra_context=""):
        # Both workers must reach this point within 5s. If chat requests
        # are serialized, only one ever does, and barrier.wait raises
        # BrokenBarrierError after the timeout.
        barrier.wait()
        yield {"type": "delta", "text": f"hi {ticker}"}
        yield {"type": "final", "text": f"done {ticker}"}

    monkeypatch.setattr(
        comments_server.build_chat_response,
        "stream_response",
        fake_stream_response,
    )

    results: dict[str, str] = {}
    errors: dict[str, BaseException] = {}

    def run_chat(ticker: str) -> None:
        try:
            resp = client.post(
                f"/chat/{ticker}",
                json={"report_date": "2026-05-01", "message": "test"},
            )
            results[ticker] = resp.get_data(as_text=True)
        except BaseException as e:  # capture rather than crash the thread
            errors[ticker] = e

    t1 = threading.Thread(target=run_chat, args=("NU",))
    t2 = threading.Thread(target=run_chat, args=("MELI",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive(), "chat threads stuck"
    assert not errors, f"errors during concurrent chat: {errors}"
    # Both completed *and* both made it past the barrier
    assert "hi NU" in results.get("NU", ""), results
    assert "done NU" in results.get("NU", ""), results
    assert "hi MELI" in results.get("MELI", ""), results
    assert "done MELI" in results.get("MELI", ""), results
