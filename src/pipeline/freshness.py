"""Per-source data-freshness verdict — ONE age-math + tone rule for every
surface that renders a "how current is this ticker's data" dot.

Two implementations disagreed before this module existed.
``pipeline.research_cockpit._staleness_dot`` judged the FMP pull age and the
build age against their OWN separate warn/bad bars (3/14d and 10/30d), worst
tone wins. ``pipeline.ticker_command_center._freshness_dot`` instead took
``max(fmp_age, build_age)`` against one shared 7/21d bar. The per-source rule
is the ratified platform-wide behavior: FMP is a daily pull and a build is
weekly-ish, so folding both into one number lets a genuinely stale FMP pull
hide behind a fresh build (or the reverse) — two different failure modes with
two different normal cadences need two different bars, not one blended one.

This module is NOT ``src/ui/time.py`` — that module owns DOM timestamp
*formatting* (``fmt_reltime`` et al). This module owns the *verdict*: how old
is too old, per source.

Naive-UTC convention (repo-wide — comparing an aware and a naive datetime
raises ``TypeError`` elsewhere in the codebase): every stamp is treated as
naive-UTC. An aware datetime passed in is normalized (converted to UTC, then
stripped of tzinfo) defensively so a caller that still has one doesn't crash;
a naive datetime is trusted as already-UTC and never re-interpreted — that
silent-assumption mismatch (cockpit attached UTC to a naive stamp explicitly,
ticker_command_center stripped tzinfo from an aware one and compared directly
against a naive ``now`` without ever attaching UTC to a naive one) is exactly
what let the two verdicts diverge for years without either side noticing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "BUILD_BAD_DAYS",
    "BUILD_WARN_DAYS",
    "FMP_BAD_DAYS",
    "FMP_WARN_DAYS",
    "TONE_BAD",
    "TONE_OK",
    "TONE_WARN",
    "FreshnessVerdict",
    "age_days",
    "freshness_verdict",
    "parse_age_days",
    "per_source_tone",
]

# Per-source staleness bars, in days (ported from research_cockpit's
# _FMP_WARN_DAYS/_FMP_BAD_DAYS/_BUILD_WARN_DAYS/_BUILD_BAD_DAYS — canonical
# now; ticker_command_center's old max-of-both 7/21d bar is retired).
FMP_WARN_DAYS = 3.0
FMP_BAD_DAYS = 14.0
BUILD_WARN_DAYS = 10.0
BUILD_BAD_DAYS = 30.0

TONE_OK = "ok"
TONE_WARN = "warn"
TONE_BAD = "bad"


@dataclass(frozen=True, slots=True)
class FreshnessVerdict:
    """The per-source freshness read: the worst-of-two-bars tone, plus each
    source's own age (``None`` when that source has never fired) so a caller
    can render the per-source detail in a tooltip."""

    tone: str  # "ok" | "warn" | "bad"
    fmp_age_days: float | None
    build_age_days: float | None


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def age_days(stamp: datetime | None, *, now: datetime) -> float | None:
    """Days between a (naive-UTC, by convention) ``stamp`` and ``now``.
    ``None`` when ``stamp`` is None. An aware argument on either side is
    normalized to naive-UTC before subtracting."""
    if stamp is None:
        return None
    return (_to_naive_utc(now) - _to_naive_utc(stamp)).total_seconds() / 86400.0


def parse_age_days(iso: str | None, *, now: datetime) -> float | None:
    """Same as :func:`age_days`, parsing an ISO timestamp string first — the
    shape both call sites store the stamp in. ``None`` on missing / unparsable
    input."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return age_days(dt, now=now)


def per_source_tone(fmp_age_days: float | None, build_age_days: float | None) -> str:
    """Worst-of-two-independent-bars verdict. Either age missing (the source
    has never fired) reads as freshness-unknown -> 'bad'."""
    if fmp_age_days is None or build_age_days is None:
        return TONE_BAD
    tone = TONE_OK
    if fmp_age_days > FMP_WARN_DAYS or build_age_days > BUILD_WARN_DAYS:
        tone = TONE_WARN
    if fmp_age_days > FMP_BAD_DAYS or build_age_days > BUILD_BAD_DAYS:
        tone = TONE_BAD
    return tone


def freshness_verdict(
    *, fmp_at: str | None, build_at: str | None, now: datetime
) -> FreshnessVerdict:
    """The per-source verdict from the raw (ISO, naive-UTC by convention)
    stamps — the one call both ``research_cockpit`` and
    ``ticker_command_center`` make."""
    fmp_age = parse_age_days(fmp_at, now=now)
    build_age = parse_age_days(build_at, now=now)
    return FreshnessVerdict(
        tone=per_source_tone(fmp_age, build_age),
        fmp_age_days=fmp_age,
        build_age_days=build_age,
    )
