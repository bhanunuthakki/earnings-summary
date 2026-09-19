"""Isolated regressions for the September codebase hardening audit."""

from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable, Iterator
from datetime import date
from io import StringIO
from pathlib import Path
from typing import ParamSpec, TypeVar

import pytest
from bs4 import BeautifulSoup, Tag
from flask import Flask

import comments
from execution import comments_server
from report.models import ReportSpec
from report.renderers.workspace_sections.boot import _comment_boot_data
from server_runtime.access import is_allowed_origin


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", "test-capability")
    return comments_server.create_app(tmp_path, db_path=tmp_path / "isolated.db")


def test_comment_boot_cannot_escape_json_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    attack = '</script><script id="injected">window.injected=true</script>'
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", "test-capability")

    def store(*_args: object) -> object:
        return object()

    def payload(*_args: object) -> dict[str, object]:
        return {"comments": [{"comment": attack}]}

    monkeypatch.setattr(comments, "load_store", store)
    monkeypatch.setattr(comments, "to_json_payload", payload)
    spec = ReportSpec.model_construct(
        ticker="TEST", generation_date=date(2026, 9, 19), repo_root=str(tmp_path)
    )
    out = StringIO()
    _comment_boot_data(out, spec)
    dom = BeautifulSoup(out.getvalue(), "html.parser")
    assert dom.find(id="injected") is None
    element = dom.find(id="workspace-comments")
    assert isinstance(element, Tag) and element.string is not None
    assert json.loads(element.string)["comments"][0]["comment"] == attack


@pytest.mark.parametrize(
    "original", [b'{"comments": [', b'{"ticker": "TEST", "comments": "invalid"}']
)
def test_corrupt_store_refuses_append_and_preserves_bytes(tmp_path: Path, original: bytes):
    path = tmp_path / "data/report_comments/TEST/2026-09-19.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    with pytest.raises(comments.CommentStoreReadError, match="comment store"):
        comments.append_comment(
            tmp_path,
            "TEST",
            date(2026, 9, 19),
            comments.Anchor(type="kpi_ledger_row", key="r"),
            "new",
        )
    assert path.read_bytes() == original


def test_sensitive_opaque_origin_reads_need_capability(app: Flask) -> None:
    @app.get("/audit-private")
    def private():
        return {"private": "owner data"}

    client = app.test_client()
    assert client.get("/audit-private", headers={"Origin": "null"}).status_code == 403
    assert (
        client.get(
            "/audit-private", headers={"Origin": "null", "X-Report-Capability": "test-capability"}
        ).status_code
        == 200
    )
    assert client.get("/healthz", headers={"Origin": "null"}).status_code == 200
    assert client.options("/audit-private", headers={"Origin": "null"}).status_code == 200


def test_host_rebinding_is_refused(app: Flask) -> None:
    assert (
        app.test_client().get("/healthz", base_url="http://attacker.example:7421").status_code
        == 403
    )


def test_unrelated_loopback_origin_is_not_trusted(app: Flask) -> None:
    @app.post("/audit-mutation")
    def mutation():
        return {"ok": True}

    response = app.test_client().post(
        "/audit-mutation",
        base_url="http://localhost:7421",
        headers={"Origin": "http://localhost:9876"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin", ["http://localhost:invalid", "http://localhost:0", "http://localhost:65536"]
)
def test_invalid_origin_ports_fail_closed(origin: str):
    assert is_allowed_origin(origin, allow_tailscale=False, whitelist=()) is None


def test_cache_invalidation_follows_mutation_commit(app: Flask) -> None:
    value = ["old"]

    @app.get("/api/panel/audit")
    def panel():
        return value[0]

    @app.post("/audit-mutation")
    def mutation():
        # A reader races with the write and populates the old value.
        assert app.test_client().get("/api/panel/audit").text == "old"
        value[0] = "new"
        return {"ok": True}

    client = app.test_client()
    assert client.get("/api/panel/audit").text == "old"
    assert client.post("/audit-mutation").status_code == 200
    assert client.get("/api/panel/audit").text == "new"


_P = ParamSpec("_P")
_T = TypeVar("_T")


class HeldExecutor(concurrent.futures.Executor):
    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def submit(
        self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> concurrent.futures.Future[_T]:
        future: concurrent.futures.Future[_T] = concurrent.futures.Future()

        def run() -> None:
            future.set_result(fn(*args, **kwargs))

        self.pending.append(run)
        return future

    def finish(self) -> None:
        self.pending.pop(0)()


def test_chat_admission_is_bounded_before_session_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", "test-capability")
    monkeypatch.setattr(comments_server, "_MAX_ADMITTED_CHATS", 2, raising=False)
    sessions: list[object] = []

    def ensure(*args: object, **kwargs: object) -> None:
        sessions.append(object())

    monkeypatch.setattr(comments_server, "ensure_session", ensure)

    def pack(*args: object) -> object:
        return object()

    def events(*args: object, **kwargs: object) -> Iterator[dict[str, object]]:
        return iter([])

    monkeypatch.setattr(comments_server, "build_portfolio_pack", pack)
    monkeypatch.setattr(comments_server, "respond_turn", events)
    executor = HeldExecutor()
    app = comments_server.create_app(tmp_path, chat_executor=executor)
    client = app.test_client()
    first = client.post("/api/ask/stream", json={"query": "one"}, buffered=False)
    first.close()
    second = client.post("/api/ask/stream", json={"query": "two"}, buffered=False)
    second.close()
    rejected = client.post("/api/ask/stream", json={"query": "three"}, buffered=False)
    try:
        assert rejected.status_code == 429
        assert len(sessions) == 2
    finally:
        rejected.close()
        while executor.pending:
            executor.finish()
    accepted = client.post("/api/ask/stream", json={"query": "after completion"}, buffered=False)
    assert accepted.status_code == 200
    accepted.close()
    executor.finish()


def test_extreme_pdf_dimensions_rejected_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from pipeline import pdf_render

    class Rect:
        width = 100_000
        height = 100_000

    class Page:
        rect = Rect()

        def get_pixmap(self, **kwargs: object) -> None:
            pytest.fail("oversized raster allocation attempted")

    class Doc:
        page_count = 1

        def load_page(self, page: int) -> Page:
            return Page()

        def close(self) -> None:
            pass

    def open_doc(path: Path) -> Doc:
        return Doc()

    monkeypatch.setattr(pdf_render, "_open_pdf", open_doc)
    assert (
        pdf_render.render_page_image(
            tmp_path, pdf_path=tmp_path / "fake.pdf", sha256="a" * 64, page=1
        )
        is None
    )


def test_private_origin_host_survives_https_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", "test-capability")
    monkeypatch.setenv("EARNINGS_SUMMARY_PRIVATE_BASE_URL", "https://desktop.example.ts.net")
    app = comments_server.create_app(tmp_path)
    client = app.test_client()
    # TLS terminates at the approved private proxy; the backend sees HTTP.
    assert client.get("/healthz", base_url="http://desktop.example.ts.net").status_code == 200
    assert client.get("/healthz", base_url="https://desktop.example.ts.net").status_code == 200
    assert client.get("/healthz", base_url="https://other.example.ts.net").status_code == 403


def test_registered_tailnet_bind_accepts_only_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", "test-capability")
    monkeypatch.setenv("COMMENTS_SERVER_ALLOW_TAILSCALE", "1")
    app = comments_server.create_app(tmp_path, server_origin="http://100.100.1.2:7421")
    client = app.test_client()
    assert client.get("/healthz", base_url="http://100.100.1.2:7421").status_code == 200
    assert client.get("/healthz", base_url="http://100.100.1.3:7421").status_code == 403


def test_report_capability_cannot_be_read_by_opaque_origin(app: Flask, tmp_path: Path) -> None:
    folder = tmp_path / "output/research/TEST"
    folder.mkdir(parents=True)
    (folder / "2026_workspace.html").write_text('<script>"test-capability"</script>')
    client = app.test_client()
    blocked = client.get("/reports/TEST", headers={"Origin": "null"})
    assert blocked.status_code == 403
    assert "test-capability" not in blocked.text
    allowed = client.get(
        "/reports/TEST", headers={"Origin": "null", "X-Report-Capability": "test-capability"}
    )
    assert allowed.status_code == 200
    assert "test-capability" in allowed.text


def test_unreadable_comment_store_refuses_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data/report_comments/TEST/2026-09-19.json"
    path.parent.mkdir(parents=True)
    original = b'{"retained": "bytes"}'
    path.write_bytes(original)
    original_read = Path.read_text

    def fail_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("temporarily unavailable")
        return original_read(self)

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(comments.CommentStoreReadError, match="unreadable"):
        comments.append_comment(
            tmp_path,
            "TEST",
            date(2026, 9, 19),
            comments.Anchor(type="kpi_ledger_row", key="r"),
            "new",
        )
    assert path.read_bytes() == original
