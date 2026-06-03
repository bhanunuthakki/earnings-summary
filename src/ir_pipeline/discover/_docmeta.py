"""Identify an IR document by the filename its server advertises.

The results-center exposes opaque hash URLs; the only reliable label is the
``Content-Disposition`` filename the file host returns (e.g. "Nu Holdings
Historical Data 1Q26.xlsx" → spreadsheet). A 1-byte ranged GET fetches just the
headers.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


@dataclass(frozen=True)
class CandidateDoc:
    """One discoverable IR document link from the generic crawler.

    ``doc_type_guess`` (legacy alias) / ``year_guess`` / ``quarter_guess`` are
    cheap link-text + filename heuristics used to *select* which links to keep;
    authoritative attribution happens later from the downloaded file's content.
    Any of them may be ``None`` when the link text was uninformative.
    """

    url: str
    link_text: str
    filename_hint: str
    doc_type_guess: str | None
    year_guess: int | None
    quarter_guess: int | None  # 1..4
    source_page: str


# Canonical doc_type → regexes matched (case-insensitively) against the filename.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spreadsheet", (r"historical data", r"\.xlsx?", r"\.xls")),
    ("deck", (r"presentation", r"webslides", r"\bslides\b")),
    ("press_release", (r"press release", r"earnings release", r"earnings-release")),
    ("transcript", (r"transcript",)),
)


def filename_for_url(url: str, timeout: int = 30) -> str:
    """Return the server's advertised filename for `url` (Content-Disposition), or ''."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        cd = resp.headers.get("Content-Disposition", "") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    return urllib.parse.unquote(m.group(1)).strip() if m else ""


def classify(filename: str) -> str | None:
    """Map a filename to a canonical doc_type, or None if unrecognized."""
    low = filename.lower()
    for doc_type, patterns in _PATTERNS:
        if any(re.search(p, low) for p in patterns):
            return doc_type
    return None
