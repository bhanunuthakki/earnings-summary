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

import ipaddress
import urllib.parse


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
