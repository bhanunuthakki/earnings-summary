"""Pure authority for manually reviewed issuer transcript URLs.

This module deliberately depends only on the standard library.  Both the IR
fetcher and the database-bound acquisition boundary can ask the same typed
policy question without importing one another (or the database runtime).
"""

from __future__ import annotations

from dataclasses import dataclass

from canonical_url import canonical_dns_host, canonical_https_url, canonical_safe_path


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
