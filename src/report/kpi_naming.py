"""Pure KPI-name presentation helpers.

KPI ledger names in the holdings JSON often inline their definition as a
parenthetical qualifier — ``"Risk-adjusted NIM (NIM minus cost of risk)"``,
``"ROE (annualized, consolidated)"``. The §2 ledger wants to show a *clean*
name with the qualifier demoted to a muted definition line, so the row reads
as a definition + data rather than one long label.

These helpers live in ``report`` (a leaf with no section/renderer deps) so both
the §2 section builder (``thesis._build_ledger``, which composes the model's
``definition``) and the workspace renderer (which strips the name for display)
share one source of truth for what counts as the parenthetical qualifier — they
can't drift apart and leave the qualifier showing in both the name and the
definition line.
"""

from __future__ import annotations

import re

# Innermost parenthetical group, e.g. the "(USD)" in "Monthly ARPAC (USD)".
# Non-greedy and non-nesting; applied repeatedly so a name with several
# parentheticals yields each qualifier.
_PAREN_RX = re.compile(r"\(([^()]*)\)")

# Connective punctuation left dangling at the *ends* after a parenthetical is
# removed (e.g. "Risk metric — (legacy)" → "Risk metric"); interior separators
# are untouched. The en/em dashes are built via chr() so the literal characters
# don't trip ruff's RUF001 (ambiguous-unicode) check.
_DANGLING_PUNCT = " -" + chr(0x2013) + chr(0x2014) + ":;,"


def clean_kpi_name(name: str) -> str:
    """Return ``name`` with parenthetical qualifiers removed, for display.

    ``"ROE (annualized, consolidated)"`` → ``"ROE"``;
    ``"Operating margin (GAAP) consolidated"`` → ``"Operating margin consolidated"``.
    Collapses the whitespace the removal leaves behind. Falls back to the
    stripped original when removal would empty the string (a name that is
    *only* a parenthetical).
    """
    stripped = " ".join(_PAREN_RX.sub(" ", name).split()).strip(_DANGLING_PUNCT)
    return stripped or name.strip()


def kpi_qualifier(name: str) -> str | None:
    """Return the parenthetical qualifier text, or None when there is none.

    ``"Risk-adjusted NIM (NIM minus cost of risk)"`` → ``"NIM minus cost of risk"``.
    Multiple parentheticals are joined with a middot. Whitespace-only
    qualifiers are dropped.
    """
    quals = [q.strip() for q in _PAREN_RX.findall(name) if q.strip()]
    return " · ".join(quals) or None


__all__ = ["clean_kpi_name", "kpi_qualifier"]
