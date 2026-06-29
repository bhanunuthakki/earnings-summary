"""Light deterministic PII scrub applied BEFORE any LLM hop.

The owner has no MNPI in their musings (confirmed at design time), so this is
NOT a name/entity redactor — it is a narrow, regex-grade scrub of the few PII
shapes that could leak into a transcript: email, phone, US SSN, and long
account/card numbers. It is deliberately CONSERVATIVE: it must never over-redact
the things a musing is *about* — ticker symbols, percentages, and dollar amounts
(which carry commas/decimals, not 13+ straight digits) all survive untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (kind, compiled pattern, replacement). Applied in order; SSN before phone so a
# 3-2-4 SSN isn't half-eaten by the 3-3-4 phone shape (they don't overlap, but
# order keeps it obvious). Account (13-19 straight digits) is card-length, so it
# never touches a year (4) or a $ amount (comma/decimal separated).
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (
        "phone",
        re.compile(r"\+?\d{0,2}[\s.\-]?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
        "[phone]",
    ),
    ("account", re.compile(r"\b\d{13,19}\b"), "[account]"),
)


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """Scrubbed text plus per-kind redaction counts (the audit-log ``detail``)."""

    text: str
    redactions: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.redactions.values())


def scrub(text: str) -> ScrubResult:
    """Redact PII shapes; return the scrubbed text + counts. Pure + deterministic."""
    counts: dict[str, int] = {}
    out = text or ""
    for kind, pattern, replacement in _PATTERNS:
        out, n = pattern.subn(replacement, out)
        if n:
            counts[kind] = n
    return ScrubResult(text=out, redactions=counts)
