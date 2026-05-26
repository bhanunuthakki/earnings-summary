"""Tests for two reviewer-flagged issues in execution/comments_server.py:

1. CORS — `Access-Control-Allow-Origin: *` must only be emitted for
   localhost-bound requests. For other hosts, the header is set to the
   request's Origin iff it appears in `COMMENTS_SERVER_CORS_WHITELIST`.

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


def test_cors_wildcard_for_localhost(client):
    """Default base_url is http://localhost — `*` is required because the
    workspace HTML opens via file:// (origin `null`)."""
    resp = client.get("/healthz")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_cors_wildcard_for_127_0_0_1(client):
    resp = client.get("/healthz", base_url="http://127.0.0.1:7421")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_cors_no_wildcard_for_non_localhost(client):
    """If someone runs --host 0.0.0.0 and a non-localhost client hits the
    server, the wildcard must not leak."""
    resp = client.get("/healthz", base_url="http://example.com")
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_echoes_whitelisted_origin(monkeypatch, client):
    monkeypatch.setenv(
        "COMMENTS_SERVER_CORS_WHITELIST", "https://my.app,https://other.app"
    )
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


# --- Concurrent chat ----------------------------------------------------


def test_chat_dispatches_to_pool_and_streams_chunks(monkeypatch, client):
    """Smoke-test the new pool path — chunks from a fake stream_response
    flow through the queue and into the SSE response."""

    def fake_stream_response(*, repo_root, ticker, report_date, user_message):
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

    def boom(*, repo_root, ticker, report_date, user_message):
        yield {"type": "delta", "text": "partial"}
        raise RuntimeError("subprocess died")

    monkeypatch.setattr(
        comments_server.build_chat_response, "stream_response", boom
    )
    resp = client.post(
        "/chat/NU",
        json={"report_date": "2026-05-01", "message": "test"},
    )
    body = resp.get_data(as_text=True)
    assert "partial" in body
    assert "chat stream failed" in body
    assert "subprocess died" in body


def test_concurrent_chat_requests_proceed_in_parallel(monkeypatch, client):
    """Two concurrent /chat requests must both reach the LLM call. We
    enforce this with a 2-way Barrier: if the second request is held
    waiting for the first, the barrier times out and the test fails.

    This guards against regressions where the chat endpoint inadvertently
    holds a lock or otherwise serializes calls.
    """
    barrier = threading.Barrier(2, timeout=5)

    def fake_stream_response(*, repo_root, ticker, report_date, user_message):
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
