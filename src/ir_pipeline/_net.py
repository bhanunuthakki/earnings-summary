"""Shared URL-safety guard and pinned transport for outbound IR fetches.

The IR discovery crawler dereferences links harvested from externally
controlled issuer pages.  Fetches are restricted to public HTTP(S) targets.
Every transport resolves and validates all candidate addresses, then connects
directly to one of those numeric addresses while retaining the original
hostname for the HTTP Host header and TLS SNI.  This prevents a DNS rebinding
between validation and socket connection.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import IO, Any, cast

_DEFAULT_TIMEOUT: Any = vars(socket)["_GLOBAL_DEFAULT_TIMEOUT"]
PLAYWRIGHT_NETWORK_LOCKDOWN_ARG = "--host-resolver-rules=MAP * ~NOTFOUND"
PLAYWRIGHT_NO_PROXY_ARG = "--no-proxy-server"
_MAX_BROWSER_RESOURCE_BYTES = 32 * 1024 * 1024
_BROWSER_FORWARD_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "if-modified-since",
        "if-none-match",
        "range",
        "user-agent",
    }
)


class UnsafeURLError(ValueError):
    """Raised when a URL is not a safe public HTTP(S) target."""


def _ensure_global_address(address: str, *, url: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise UnsafeURLError(f"unparseable resolved address blocked: {url!r}") from exc
    if not ip.is_global:
        raise UnsafeURLError(f"non-public host blocked: {url!r}")


def _resolve_public_addresses(
    host: str,
    port: int,
    *,
    url: str,
) -> tuple[tuple[int, int, int, tuple[Any, ...]], ...]:
    """Return an all-public, connect-ready DNS snapshot for ``host``."""
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise UnsafeURLError(f"host resolution failed: {url!r}") from exc
    if not resolved:
        raise UnsafeURLError(f"host resolved to no addresses: {url!r}")

    endpoints: list[tuple[int, int, int, tuple[Any, ...]]] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for family, socktype, proto, _canonname, sockaddr in resolved:
        address = str(sockaddr[0])
        _ensure_global_address(address, url=url)
        key = (family, tuple(sockaddr))
        if key in seen:
            continue
        seen.add(key)
        endpoints.append((family, socktype, proto, tuple(sockaddr)))
    return tuple(endpoints)


def ensure_safe_public_url(url: str) -> str:
    """Return ``url`` unchanged if it is a public HTTP(S) target, else raise."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"non-http(s) URL scheme blocked: {url!r}")
    host = parsed.hostname or ""
    if not host:
        raise UnsafeURLError(f"URL has no host: {url!r}")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeURLError(f"loopback host blocked: {url!r}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        _resolve_public_addresses(host, port, url=url)
    else:
        _ensure_global_address(host, url=url)
    return url


def _connect_public_socket(
    host: str,
    port: int,
    *,
    timeout: Any,
    source_address: tuple[str, int] | None,
) -> socket.socket:
    """Connect to the exact address snapshot that passed the public-IP guard."""
    endpoints = _resolve_public_addresses(host, port, url=f"tcp://{host}:{port}")
    last_error: OSError | None = None
    for family, socktype, proto, sockaddr in endpoints:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not _DEFAULT_TIMEOUT:
                sock.settimeout(cast(float | None, timeout))
            if source_address:
                sock.bind(source_address)
            if hasattr(socket, "TCP_NODELAY"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise UnsafeURLError(f"host resolved to no connectable public address: {host!r}")


class PinnedPublicHTTPConnection(http.client.HTTPConnection):
    """HTTP connection pinned to the same public-IP snapshot it validates."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: Any = _DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        blocksize: int = 8192,
    ) -> None:
        super().__init__(
            host,
            port,
            timeout=timeout,
            source_address=source_address,
            blocksize=blocksize,
        )
        self._public_source_address = source_address

    def connect(self) -> None:
        self.sock = _connect_public_socket(
            self.host,
            self.port,
            timeout=self.timeout,
            source_address=self._public_source_address,
        )
        tunnel_host = cast(str | None, getattr(self, "_tunnel_host", None))
        if tunnel_host:
            tunnel = cast(Callable[[], None], cast(Any, self)._tunnel)
            tunnel()


class PinnedPublicHTTPSConnection(PinnedPublicHTTPConnection):
    """HTTPS connection preserving the original hostname for certificate/SNI."""

    default_port = http.client.HTTPS_PORT

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: Any = _DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        context: ssl.SSLContext | None = None,
        blocksize: int = 8192,
        **_kwargs: Any,
    ) -> None:
        super().__init__(
            host,
            port,
            timeout=timeout,
            source_address=source_address,
            blocksize=blocksize,
        )
        self._context = context or ssl.create_default_context()

    def connect(self) -> None:
        super().connect()
        tunnel_host = cast(str | None, getattr(self, "_tunnel_host", None))
        server_hostname = tunnel_host or self.host
        assert self.sock is not None
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class PinnedPublicHTTPHandler(urllib.request.HTTPHandler):
    """urllib HTTP handler using the pinned public connection."""

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(PinnedPublicHTTPConnection, req)


class PinnedPublicHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib HTTPS handler using the pinned public connection."""

    def https_open(self, req: urllib.request.Request) -> Any:
        context = cast(ssl.SSLContext | None, getattr(self, "_context", None))
        return self.do_open(PinnedPublicHTTPSConnection, req, context=context)


def safe_redirect_url(current_url: str, location: str) -> str:
    """Resolve and validate one server-controlled HTTP redirect target."""
    if not location:
        raise UnsafeURLError("redirect response has no Location header")
    return ensure_safe_public_url(urllib.parse.urljoin(current_url, location))


class GuardedHTTPRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that checks every server-controlled hop."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        safe_redirect_url(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirects to the caller instead of following them internally."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def build_public_opener() -> urllib.request.OpenerDirector:
    """Build a redirect-guarded urllib opener with DNS-pinned connections."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        PinnedPublicHTTPHandler(),
        PinnedPublicHTTPSHandler(),
        GuardedHTTPRedirectHandler(),
    )


def _build_browser_public_opener() -> urllib.request.OpenerDirector:
    """Pinned, proxy-free opener that resolves guarded redirects before Chromium.

    Chromium's ambient resolver is deliberately disabled. Returning a 3xx to it
    would make the browser attempt the next hop itself and fail closed with
    ``ERR_NAME_NOT_RESOLVED`` before our route proxy can fulfil that request.
    Following redirects here keeps every hop inside the same public-IP guard and
    returns the final response body to Chromium without enabling ambient DNS.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        PinnedPublicHTTPHandler(),
        PinnedPublicHTTPSHandler(),
        GuardedHTTPRedirectHandler(),
    )


def curl_resolve_entries(url: str) -> list[str]:
    """Return libcurl ``CURLOPT_RESOLVE`` entries pinned to validated addresses."""
    ensure_safe_public_url(url)
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    endpoints = _resolve_public_addresses(host, port, url=url)
    entries: list[str] = []
    for _family, _socktype, _proto, sockaddr in endpoints:
        address = str(sockaddr[0])
        rendered = f"[{address}]" if ":" in address else address
        entries.append(f"{host}:{port}:{rendered}")
    return entries


def install_public_only_playwright_routing(context: Any, *, timeout_s: float) -> None:
    """Fulfil every browser request through the pinned transport.

    Chromium must also be launched with :data:`PLAYWRIGHT_NETWORK_LOCKDOWN_ARG`.
    The resolver rule makes direct browser network escape fail closed; this
    route supplies approved GET/HEAD resources after public-IP validation.
    """

    def _route_public_request(route: Any) -> None:
        request = route.request
        method = str(request.method).upper()
        if method not in {"GET", "HEAD"}:
            route.abort("blockedbyclient")
            return
        url = str(request.url)
        try:
            ensure_safe_public_url(url)
            headers = {
                str(key): str(value)
                for key, value in dict(request.headers).items()
                if str(key).lower() in _BROWSER_FORWARD_HEADERS
            }
            pinned_request = urllib.request.Request(url, headers=headers, method=method)
            with _build_browser_public_opener().open(
                pinned_request,
                timeout=min(max(timeout_s, 1.0), 15.0),
            ) as response:
                body = response.read(_MAX_BROWSER_RESOURCE_BYTES + 1)
                if len(body) > _MAX_BROWSER_RESOURCE_BYTES:
                    route.abort("blockedbyclient")
                    return
                response_headers = {
                    str(key): str(value)
                    for key, value in response.headers.items()
                    if str(key).lower() not in {"content-length", "transfer-encoding"}
                }
                route.fulfill(status=response.status, headers=response_headers, body=body)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400 and exc.code != http.client.NOT_MODIFIED:
                try:
                    safe_redirect_url(url, exc.headers.get("Location", ""))
                except UnsafeURLError:
                    route.abort("blockedbyclient")
                    return
            body = exc.read(_MAX_BROWSER_RESOURCE_BYTES + 1)
            if len(body) > _MAX_BROWSER_RESOURCE_BYTES:
                route.abort("blockedbyclient")
                return
            response_headers = {
                str(key): str(value)
                for key, value in exc.headers.items()
                if str(key).lower() not in {"content-length", "transfer-encoding"}
            }
            route.fulfill(status=exc.code, headers=response_headers, body=body)
        except (UnsafeURLError, urllib.error.URLError, OSError, ValueError):
            route.abort("blockedbyclient")

    context.route("**/*", _route_public_request)
    route_web_socket = getattr(context, "route_web_socket", None)
    if not callable(route_web_socket):
        raise RuntimeError("Playwright >=1.48 is required for fail-closed WebSocket routing")

    def _block_websocket(websocket: Any) -> None:
        websocket.close(code=1008, reason="blocked")

    route_web_socket("**/*", _block_websocket)
