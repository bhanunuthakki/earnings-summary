"""Pure authority for manually reviewed issuer transcript URLs.

This module deliberately depends only on the standard library.  Both the IR
fetcher and the database-bound acquisition boundary can ask the same typed
policy question without importing one another (or the database runtime).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class ReviewedIssuerTranscript:
    """One exact issuer-owned transcript URL admitted by manual review."""

    ticker: str
    year: int
    quarter: int
    url: str
    filename: str


_REVIEWED_ISSUER_TRANSCRIPTS = (
    ReviewedIssuerTranscript(
        ticker="BN",
        year=2026,
        quarter=2,
        url=(
            "https://bn.brookfield.com/sites/brookfield-bn-v2/files/"
            "Brookfield-BN-IR-V2/2026/Q2/BN%20Q2-2026-transcript.pdf"
        ),
        filename="BN Q2 2026 transcript.pdf",
    ),
)


def canonical_dns_host(host: str) -> str | None:
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
    """Return the strict host/path identity used by reviewed URL matching."""

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


def reviewed_issuer_transcript(
    ticker: str, year: int, quarter: int
) -> ReviewedIssuerTranscript | None:
    """Return the manually reviewed transcript for an exact period, if any."""

    normalized = ticker.strip().upper()
    return next(
        (
            item
            for item in _REVIEWED_ISSUER_TRANSCRIPTS
            if (item.ticker, item.year, item.quarter) == (normalized, year, quarter)
        ),
        None,
    )


def reviewed_issuer_transcript_url_is_authorized(
    ticker: str,
    year: int,
    quarter: int,
    source_url: str,
) -> bool:
    """Return True only for an exact URL in the reviewed issuer set."""

    candidate = canonical_https_url(source_url)
    return candidate is not None and any(
        (item.ticker, item.year, item.quarter) == (ticker.strip().upper(), year, quarter)
        and canonical_https_url(item.url) == candidate
        for item in _REVIEWED_ISSUER_TRANSCRIPTS
    )


__all__ = [
    "ReviewedIssuerTranscript",
    "canonical_dns_host",
    "canonical_https_url",
    "canonical_safe_path",
    "reviewed_issuer_transcript",
    "reviewed_issuer_transcript_url_is_authorized",
]
