"""Tests for the IR outbound-fetch URL guard (src/ir_pipeline/_net.py) and the
Content-Disposition filename hardening in src/ir_pipeline/download.py.

The discovery crawler dereferences raw ``<a href>`` values harvested off an
externally controlled issuer page, so a crafted link must not be able to read a
local file (``file://``) or reach an internal host, and a server-supplied
filename must not steer the write out of the destination directory.
"""

from __future__ import annotations

import email.message
import io
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline import _net  # noqa: E402
from ir_pipeline._net import UnsafeURLError, ensure_safe_public_url  # noqa: E402
from ir_pipeline.discover import generic, mz  # noqa: E402
from ir_pipeline.download import _filename_from_content_disposition  # noqa: E402


@pytest.fixture(autouse=True)
def public_test_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep URL-guard tests hermetic while exercising hostname resolution."""

    monkeypatch.setattr(
        "ir_pipeline._net.socket.getaddrinfo",
        lambda host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://s201.q4cdn.com/files/doc.xlsx",
        "http://investors.example.com/q1.pdf",
        "https://example.com:8443/files/historical.xlsx",
    ],
)
def test_public_urls_pass_through(url: str) -> None:
    assert ensure_safe_public_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Users/bhanu/.env",
        "file:///etc/passwd",
        "ftp://example.com/secret.xlsx",
        "data:text/plain,hello",
        "gopher://example.com/",
    ],
)
def test_non_http_schemes_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/files/x.pdf",
        "http://127.0.0.1:8000/files/x.pdf",
        "http://[::1]/x.pdf",
        "http://10.0.0.5/internal.xlsx",
        "http://192.168.1.10/internal.xlsx",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata pivot
        "http://172.16.5.5/x.pdf",
    ],
)
def test_internal_hosts_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url(url)


def test_missing_host_blocked() -> None:
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url("http:///no-host")


def test_hostname_resolving_to_private_target_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ir_pipeline._net.socket.getaddrinfo",
        lambda host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))
        ],
    )
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url("https://investor-files.example/report.pdf")


def test_hostname_with_any_private_resolution_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ir_pipeline._net.socket.getaddrinfo",
        lambda host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port)),
        ],
    )
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url("https://mixed-resolution.example/report.pdf")


def test_pinned_connection_uses_validated_numeric_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[object, ...]] = []

    class _Socket:
        def settimeout(self, _timeout: object) -> None:
            pass

        def bind(self, _address: object) -> None:
            pass

        def setsockopt(self, *_args: object) -> None:
            pass

        def connect(self, address: tuple[object, ...]) -> None:
            connected.append(address)

        def close(self) -> None:
            pass

    monkeypatch.setattr(_net.socket, "socket", lambda *_args: _Socket())
    conn = _net.PinnedPublicHTTPConnection("issuer.example", 80, timeout=1)
    conn.connect()

    assert connected == [("93.184.216.34", 80)]


def test_rebinding_to_private_address_is_blocked_before_socket_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _dns(_host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(_net.socket, "getaddrinfo", _dns)
    ensure_safe_public_url("https://rebind.example/report.pdf")
    conn = _net.PinnedPublicHTTPSConnection("rebind.example", 443, timeout=1)
    with pytest.raises(UnsafeURLError):
        conn.connect()


def test_public_opener_disables_environment_proxies() -> None:
    opener = _net.build_public_opener()
    proxy_handlers = [
        handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert proxy_handlers == []


def test_browser_public_opener_follows_only_guarded_redirects() -> None:
    opener = _net._build_browser_public_opener()

    assert any(isinstance(handler, _net.GuardedHTTPRedirectHandler) for handler in opener.handlers)
    assert not any(isinstance(handler, _net.NoRedirectHandler) for handler in opener.handlers)


class _BrowserRequest:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.url = url
        self.method = method
        self.headers = {"Accept": "text/html", "Cookie": "must-not-forward"}


class _BrowserRoute:
    def __init__(self, request: _BrowserRequest) -> None:
        self.request = request
        self.aborted = False
        self.fulfilled: dict[str, Any] | None = None

    def abort(self, _reason: str) -> None:
        self.aborted = True

    def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled = kwargs


class _BrowserContext:
    def __init__(self) -> None:
        self.handler: Any = None
        self.websocket_handler: Any = None

    def route(self, _pattern: str, handler: Any) -> None:
        self.handler = handler

    def route_web_socket(self, _pattern: str, handler: Any) -> None:
        self.websocket_handler = handler


class _PinnedResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {
        "Content-Type": "text/html",
        "Content-Length": "4",
    }

    def __enter__(self) -> _PinnedResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b"safe"


class _PinnedOpener:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, **_kwargs: object) -> object:
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_browser_route_fulfills_public_get_without_forwarding_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _PinnedOpener(_PinnedResponse())
    monkeypatch.setattr(_net, "_build_browser_public_opener", lambda: opener)
    context = _BrowserContext()
    _net.install_public_only_playwright_routing(context, timeout_s=60)
    route = _BrowserRoute(_BrowserRequest("https://issuer.example/report"))

    context.handler(route)

    assert route.aborted is False
    assert route.fulfilled == {
        "status": 200,
        "headers": {"Content-Type": "text/html"},
        "body": b"safe",
    }
    assert opener.requests[0].headers.get("Cookie") is None


def test_browser_route_blocks_private_and_non_get_without_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _PinnedOpener(_PinnedResponse())
    monkeypatch.setattr(_net, "_build_browser_public_opener", lambda: opener)
    context = _BrowserContext()
    _net.install_public_only_playwright_routing(context, timeout_s=5)

    private_route = _BrowserRoute(_BrowserRequest("http://127.0.0.1/admin"))
    context.handler(private_route)
    post_route = _BrowserRoute(_BrowserRequest("https://issuer.example/api", "POST"))
    context.handler(post_route)

    assert private_route.aborted is True
    assert post_route.aborted is True
    assert opener.requests == []


def test_browser_route_blocks_private_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = email.message.Message()
    headers["Location"] = "http://169.254.169.254/latest/meta-data/"
    redirect = urllib.error.HTTPError(
        "https://issuer.example/start",
        302,
        "Found",
        headers,
        io.BytesIO(b""),
    )
    context = _BrowserContext()
    opener = _PinnedOpener(redirect)
    monkeypatch.setattr(_net, "_build_browser_public_opener", lambda: opener)
    _net.install_public_only_playwright_routing(context, timeout_s=5)
    route = _BrowserRoute(_BrowserRequest("https://issuer.example/start"))
    context.handler(route)

    assert route.aborted is True
    assert route.fulfilled is None


def test_browser_route_returns_not_modified_without_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_modified = urllib.error.HTTPError(
        "https://issuer.example/app.js",
        304,
        "Not Modified",
        email.message.Message(),
        io.BytesIO(b""),
    )
    context = _BrowserContext()
    monkeypatch.setattr(
        _net,
        "_build_browser_public_opener",
        lambda: _PinnedOpener(not_modified),
    )
    _net.install_public_only_playwright_routing(context, timeout_s=5)
    route = _BrowserRoute(_BrowserRequest("https://issuer.example/app.js"))

    context.handler(route)

    assert route.aborted is False
    assert route.fulfilled == {"status": 304, "headers": {}, "body": b""}


def test_browser_websockets_are_closed_fail_closed() -> None:
    context = _BrowserContext()
    _net.install_public_only_playwright_routing(context, timeout_s=5)

    class _WebSocket:
        closed: tuple[int, str] | None = None

        def close(self, *, code: int, reason: str) -> None:
            self.closed = (code, reason)

    websocket = _WebSocket()
    context.websocket_handler(websocket)
    assert websocket.closed == (1008, "blocked")


class _PlaywrightPage:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def goto(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("goto")

    def wait_for_selector(self, *_args: object, **_kwargs: object) -> None:
        pass

    def wait_for_timeout(self, *_args: object, **_kwargs: object) -> None:
        pass

    def eval_on_selector_all(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


class _PlaywrightContext(_BrowserContext):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def route(self, pattern: str, handler: Any) -> None:
        self.events.append("route")
        super().route(pattern, handler)

    def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.events.append("websocket")
        super().route_web_socket(pattern, handler)

    def new_page(self) -> _PlaywrightPage:
        self.events.append("new_page")
        return _PlaywrightPage(self.events)


class _PlaywrightBrowser:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.context_kwargs: dict[str, object] = {}

    def new_context(self, **kwargs: object) -> _PlaywrightContext:
        self.context_kwargs = kwargs
        return _PlaywrightContext(self.events)

    def close(self) -> None:
        pass


class _PlaywrightChromium:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.launch_kwargs: dict[str, object] = {}
        self.browser = _PlaywrightBrowser(events)

    def launch(self, **kwargs: object) -> _PlaywrightBrowser:
        self.launch_kwargs = kwargs
        return self.browser


class _PlaywrightManager:
    def __init__(self, chromium: _PlaywrightChromium) -> None:
        self.chromium = chromium

    def __enter__(self) -> _PlaywrightManager:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize("renderer", [generic._playwright_render, mz._visible_filemanager_hrefs])
def test_playwright_renderers_lock_network_before_navigation(
    renderer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    events: list[str] = []
    chromium = _PlaywrightChromium(events)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _PlaywrightManager(chromium))

    assert renderer("https://issuer.example/results", 1000) == []

    args = chromium.launch_kwargs["args"]
    assert _net.PLAYWRIGHT_NETWORK_LOCKDOWN_ARG in args
    assert _net.PLAYWRIGHT_NO_PROXY_ARG in args
    assert chromium.browser.context_kwargs["service_workers"] == "block"
    assert events[:4] == ["route", "websocket", "new_page", "goto"]


def test_robots_policy_uses_pinned_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RobotsResponse(_PinnedResponse):
        def read(self, _limit: int = -1) -> bytes:
            return b"User-agent: *\nAllow: /\n"

    opener = _PinnedOpener(_RobotsResponse())
    monkeypatch.setattr(generic, "build_public_opener", lambda: opener)
    monkeypatch.setattr(
        generic.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw urlopen used")),
    )

    can_fetch, delay = generic._robots_policy("https://issuer.example/results")

    assert can_fetch("https://issuer.example/public") is True
    assert delay == 0.0
    assert len(opener.requests) == 1


def test_redirect_to_private_target_is_blocked() -> None:
    from ir_pipeline._net import safe_redirect_url

    with pytest.raises(UnsafeURLError):
        safe_redirect_url("https://issuer.example/a.pdf", "http://127.0.0.1/admin")


def test_relative_redirect_is_resolved_and_allowed() -> None:
    from ir_pipeline._net import safe_redirect_url

    assert safe_redirect_url("https://issuer.example/a.pdf", "/documents/q1.pdf") == (
        "https://issuer.example/documents/q1.pdf"
    )


@pytest.mark.parametrize(
    ("cd", "expected"),
    [
        ('attachment; filename="Nu Historical 1Q26.xlsx"', "Nu Historical 1Q26.xlsx"),
        # path separators in the advertised name must collapse to the leaf only
        ('attachment; filename="../../evil.xlsx"', "evil.xlsx"),
        ('attachment; filename="..\\..\\evil.xlsx"', "evil.xlsx"),
        ('attachment; filename="/abs/path/report.pdf"', "report.pdf"),
    ],
)
def test_content_disposition_keeps_leaf_name_only(cd: str, expected: str) -> None:
    assert _filename_from_content_disposition(cd, "fallback.xlsx") == expected


@pytest.mark.parametrize("cd", ["", "attachment", 'attachment; filename=".."'])
def test_content_disposition_falls_back(cd: str) -> None:
    assert _filename_from_content_disposition(cd, "fallback.xlsx") == "fallback.xlsx"
