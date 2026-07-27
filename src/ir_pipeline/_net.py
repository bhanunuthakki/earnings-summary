"""Shared URL-safety guard for outbound IR fetches.

The IR discovery crawler harvests every ``<a href>`` off an externally
controlled issuer results-center page and then dereferences the raw href to
download spreadsheets/PDFs. ``urllib.request.urlopen`` will happily follow a
``file://`` (local file read) or ``ftp://`` URL, and an attacker-influenced
link could point at a loopback/internal host. This guard restricts fetches to
public ``http(s)`` targets before any request is made.

It is a pragmatic check for a single-user localhost tool — not a defense
against DNS-rebinding — but it closes the concrete ``file://`` local-read and
the obvious internal-host SSRF pivots.
"""

from __future__ import annotations

import http.client
import ipaddress
import urllib.parse
import urllib.request
from typing import IO


class UnsafeURLError(ValueError):
    """Raised when a URL is not a safe public http(s) target."""


def ensure_safe_public_url(url: str) -> str:
    """Return ``url`` unchanged if it is a public http(s) target, else raise.

    Blocks non-http(s) schemes (``file:``, ``ftp:``, ``data:`` …) and hosts
    that resolve to loopback / private / link-local / reserved ranges.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"non-http(s) URL scheme blocked: {url!r}")
    host = parsed.hostname or ""
    if not host:
        raise UnsafeURLError(f"URL has no host: {url!r}")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeURLError(f"loopback host blocked: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # hostname, not a literal IP — allowed (no DNS resolution here)
    if ip is not None and (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise UnsafeURLError(f"non-public host blocked: {url!r}")
    return url


def safe_redirect_url(current_url: str, location: str) -> str:
    """Resolve and validate one HTTP redirect target.

    Redirect ``Location`` is server-controlled input.  Call this before every
    hop rather than relying on a client's automatic redirect support, which
    would otherwise turn a safe issuer URL into an internal request.
    """
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
