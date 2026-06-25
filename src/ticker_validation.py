"""Validate an externally-supplied ticker before it reaches a filesystem path.

Tickers flow from request bodies into file names: ``dcf/<T>.xlsx``,
``dcf/facts/<T>.xlsx``, ``data/report_comments/<T>/<date>.json``,
``data/dcf_assumptions/<T>.json``, ``micro_thesis/holdings/<T>.json``, … Without
a shape check, an attacker-influenced ticker like ``../../etc`` would traverse
out of the intended per-ticker directory.

This is the single allowlist gate. :func:`safe_ticker` returns the uppercased
ticker when it matches the universe's real shape, or raises ``ValueError`` — the
web layer turns that into a 400, and the storage/refresh seams refuse to act on
it (defense in depth, so a missed route can't open a traversal).

The pattern must START with an alphanumeric, which (together with the
no-separator character class) rejects the dangerous path tokens ``.`` and ``..``
that a bare ``[A-Z0-9.\\-]+`` would let through.
"""

from __future__ import annotations

import re

# Letters/digits/dot/hyphen, 1-12 chars, first char alphanumeric. Covers the
# real universe (BRK-B, BRK.A, FLKR, GOOGL, …) and rejects '.', '..', '-x',
# whitespace, path separators, and over-long input.
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}")


def safe_ticker(value: object) -> str:
    """Return the validated, uppercased ticker, or raise ``ValueError``.

    Accepts the universe's real shapes; rejects anything that could escape the
    per-ticker directory it names (path separators, ``.``/``..``, whitespace,
    empties, over-long input, non-strings).
    """
    if not isinstance(value, str):
        raise ValueError(f"ticker must be a string, got {type(value).__name__}")
    candidate = value.strip().upper()
    if not _TICKER_RE.fullmatch(candidate):
        raise ValueError(f"invalid ticker: {value!r}")
    return candidate
