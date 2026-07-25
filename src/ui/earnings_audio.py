"""Deep link from the transcripts/earnings surface to Google Finance's
per-ticker earnings page — P7 of ``docs/design/disclosure_change_build_stack.md``.

Google Finance beta (verified in-browser 2026-07-24 for NU:NYSE, WIX:NASDAQ)
exposes, per ticker, at
``https://www.google.com/finance/beta/quote/{TICKER}:{EXCHANGE}?tab=earnings``:
a recorded-call audio player, a speaker-attributed call transcript, and a
Gemini-generated summary for a COMPLETED call, or a "waiting for the call"
card plus consensus estimates for an upcoming one.

This module builds that URL and NOTHING else. It does not fetch, scrape,
cache, or re-host any of Google's audio/transcript/summary content — that is
a terms-of-service question nobody has cleared, and the only value we need is
one-click owner access to Google's own page.

Never guess the exchange or the ticker's Google Finance spelling. Both are
resolved from data already on file (the cached FMP profile) or verified
in-browser this session; anything that does not resolve renders no link. A
dead link is worse than no link — this is the same silent-guessing failure
mode ``feedback_silent_degradation_class`` calls out, applied to a URL
instead of a data field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

# FMP's cached-profile ``exchange``/``exchangeShortName`` value -> the
# Google Finance quote-URL exchange suffix. Deliberately small and
# hand-verified, not derived: OTC-listed names (foreign ADRs, e.g. NTDOY)
# DO resolve to a valid Google Finance page under "OTCMKTS", but that page
# carries no recorded-call audio/transcript panel at all (verified
# in-browser 2026-07-24) — the entire point of this link — so OTC is
# deliberately excluded rather than producing a technically-loading but
# useless link.
_GOOGLE_FINANCE_EXCHANGE: dict[str, str] = {
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
}


# Share-class tickers FMP spells with a dash (BRK-B, BF-B, ...) that Google
# Finance spells with a dot (BRK.B). Verified in-browser 2026-07-24:
# BRK.B:NYSE resolves, BRK-B:NYSE does not (falls through to the generic
# Google Finance home page). This is a known, deterministic ticker-spelling
# convention -- applied generically, not guessed per name.
def _google_finance_symbol(ticker: str) -> str:
    return ticker.replace("-", ".")


def _load_profile(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read the cached FMP ``{TICKER}_profile.json``, or None if absent/bad.

    Mirrors the read idiom already duplicated across
    ``compute.valuation_basis``, ``compute.peer_selection``,
    ``compute.comparable_sets``, and the report section builders (no shared
    helper exists for this one-file read; per repo convention this small
    logic is duplicated rather than factored into a new shared module).
    """
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, list):
        rows = cast("list[object]", raw)
        raw = rows[0] if rows else None
    if not isinstance(raw, dict):
        return None
    return cast("dict[str, object]", raw)


def google_finance_earnings_url(repo_root: Path | str, ticker: str) -> str | None:
    """Deep link to Google Finance's earnings tab (audio + transcript +
    AI summary) for ``ticker``, or None when the exchange can't be resolved
    to one Google Finance is verified to accept.

    None cases (all deliberate, never a guessed fallback):
      - no cached FMP profile for this ticker
      - profile has no ``exchangeShortName``/``exchange`` field
      - the exchange isn't NASDAQ or NYSE (see ``_GOOGLE_FINANCE_EXCHANGE``
        docstring for why OTC is excluded even though its URL loads)
    """
    root = repo_root if isinstance(repo_root, Path) else Path(repo_root)
    t = ticker.upper().strip()
    if not t:
        return None
    profile = _load_profile(root, t)
    if profile is None:
        return None
    exch = profile.get("exchangeShortName") or profile.get("exchange")
    if not isinstance(exch, str) or not exch.strip():
        return None
    suffix = _GOOGLE_FINANCE_EXCHANGE.get(exch.strip().upper())
    if suffix is None:
        return None
    symbol = _google_finance_symbol(t)
    return f"https://www.google.com/finance/beta/quote/{symbol}:{suffix}?tab=earnings"
