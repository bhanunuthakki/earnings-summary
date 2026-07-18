"""Typed, units-explicit read of the tracker's exit-quality payload for ONE
ticker — the Phase-2 "tracker exit-quality join" repair item named in
``docs/design/owner_context_federation.md`` §3.2 ("Not yet read from the
tracker") and §4 delivery seam 1 of ``docs/design/tenet2_advisory_program.md``.

The ES tracker client (``integrations.portfolio_tracker_client``) already
fetches ``GET /api/portfolio/exit-quality`` via :func:`fetch_exit_quality` and
parses it into dataclasses with the client's own ``_f()``/``_s()``/``_i()``
coercion helpers — that parsing stays as-is (a wholesale client rewrite is out
of scope). This module is the CONTRACT-FORMALIZATION layer on top: a small
Pydantic model over JUST the fields the ``/review`` capacity block and the
verdict prompt actually consume, with units spelled out per field (every
dollar amount here is absolute USD, never a percent or a fraction — the
tracker's exit-quality payload carries no percent fields at all, unlike the
beta/positioning endpoints).

Never-raises: :func:`read_ticker_exit_quality` returns ``None`` on ANY
problem — tracker offline, HTTP error, malformed JSON, no exit-quality row for
this ticker, or a value that fails Pydantic validation. The caller (the
capacity block) simply omits its line — the same hide-don't-stub contract
every federation boundary in this program follows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TickerExitQuality(BaseModel):
    """The tracker's per-ticker exit-quality read, narrowed to the fields the
    capacity block renders. ALL dollar fields are absolute USD amounts, never
    a percent or a fraction — the tracker's ``ExitQualityRow`` carries no
    percent-shaped fields at all (contrast with ``BetaStats``/``Positioning``,
    where several fields ARE fractions per the client's own docstrings)."""

    ticker: str
    sold_shares: float | None = Field(default=None, description="Share count, not USD.")
    sold_proceeds_usd: float | None = Field(
        default=None, description="Absolute USD proceeds from the sale(s)."
    )
    value_if_held_usd: float | None = Field(
        default=None, description="Absolute USD — the sold shares marked at today's price."
    )
    regret_vs_hold_usd: float | None = Field(
        default=None,
        description=(
            "Absolute USD, signed. value_if_held − sold_proceeds; POSITIVE means "
            "selling cost the owner money vs. holding."
        ),
    )
    exit_alpha_vs_spy_usd: float | None = Field(
        default=None,
        description=(
            "Absolute USD, signed. Proceeds reinvested in SPY vs. holding; POSITIVE "
            "means the exit + redeploy beat just holding."
        ),
    )
    still_held: bool = Field(
        default=False,
        description="True when only PART of the position was sold — not a full exit.",
    )


def read_ticker_exit_quality(
    ticker: str, *, api_url: str | None = None
) -> TickerExitQuality | None:
    """Fetch the tracker's exit-quality payload and narrow it to ``ticker``.

    Never raises: an offline tracker, an HTTP/JSON failure, no row for this
    ticker, or a row that fails validation all degrade to ``None``. The
    underlying ``fetch_exit_quality`` already has its own never-raise
    contract (returns ``None`` on any tracker problem); this wraps that in a
    ``try/except`` too so a FUTURE change to the client's contract can never
    turn this read into a crash on the ``/review`` path.
    """
    try:
        from integrations.portfolio_tracker_client import fetch_exit_quality

        payload = fetch_exit_quality(api_url=api_url)
    except Exception:
        return None
    if payload is None:
        return None
    match = next(
        (row for row in payload.rows if (row.ticker or "").upper() == ticker.upper()), None
    )
    if match is None:
        return None
    try:
        return TickerExitQuality(
            ticker=ticker.upper(),
            sold_shares=match.sold_shares,
            sold_proceeds_usd=match.sold_proceeds,
            value_if_held_usd=match.value_if_held,
            regret_vs_hold_usd=match.regret_vs_hold,
            exit_alpha_vs_spy_usd=match.exit_alpha_vs_spy,
            still_held=match.still_held,
        )
    except Exception:
        return None


def render_exit_quality_note(eq: TickerExitQuality) -> str:
    """One-line, human-readable read of a :class:`TickerExitQuality` for the
    capacity block: did the owner's prior exit on this ticker beat holding?

    ``regret_vs_hold_usd`` is signed (positive = selling cost the owner
    money); this renders the plain-English direction plus the magnitude, and
    flags a partial exit so the line doesn't read as "you're fully out" when
    the owner still holds a remainder.
    """
    regret = eq.regret_vs_hold_usd
    if regret is None:
        verdict = "outcome vs. holding unknown"
    elif regret > 0:
        verdict = f"cost you ~${regret:,.0f} vs. holding"
    elif regret < 0:
        verdict = f"beat holding by ~${abs(regret):,.0f}"
    else:
        verdict = "matched holding"
    partial = " (partial exit — still held)" if eq.still_held else ""
    return f"prior realized exit on {eq.ticker} {verdict}{partial}"


__all__ = ["TickerExitQuality", "read_ticker_exit_quality", "render_exit_quality_note"]
