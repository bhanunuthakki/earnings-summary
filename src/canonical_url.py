"""Dependency-free canonicalization for public HTTPS URL identities."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def canonical_dns_host(host: str) -> str | None:
    """Return a strict lowercase DNS hostname, excluding IP literals."""

    if host != host.casefold() or not host.isascii() or len(host) > 253:
        return None
    if host != host.strip() or host.endswith(".") or ".." in host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    labels = host.split(".")
    if len(labels) < 2 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        return None
    return host


def canonical_safe_path(raw_path: str) -> str | None:
    """Return a path with no traversal, control, slash, or backslash ambiguity."""

    if not raw_path.startswith("/") or not raw_path.isascii():
        return None
    current = raw_path
    for _ in range(5):
        if any(ord(character) < 32 or ord(character) == 127 for character in current):
            return None
        if (
            "\\" in current
            or "//" in current
            or any(segment in {".", ".."} for segment in current.split("/"))
        ):
            return None
        decoded = unquote(current)
        if decoded == current:
            return current
        if decoded.count("/") != current.count("/") or "\\" in decoded:
            return None
        current = decoded
    return None


def canonical_https_url(url: str) -> tuple[str, str] | None:
    """Return the strict host/path identity used by URL authorization."""

    try:
        parsed = urlsplit(url)
        explicit_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or explicit_port is not None
        or parsed.fragment
    ):
        return None
    host = canonical_dns_host(parsed.hostname)
    path = canonical_safe_path(parsed.path or "/")
    if host is None or path is None:
        return None
    return host, path


__all__ = ["canonical_dns_host", "canonical_https_url", "canonical_safe_path"]
