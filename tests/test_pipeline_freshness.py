"""Tests for src/pipeline/freshness.py — the per-source freshness verdict
shared by pipeline.research_cockpit and pipeline.ticker_command_center.

Pins the warn/bad boundaries on EACH side of the per-source rule (FMP
3/14d, build 10/30d) since neither original implementation had a test
naming these exact numbers, plus the naive/aware datetime-handling
contract (repo convention: naive-UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pipeline.freshness import (
    BUILD_BAD_DAYS,
    BUILD_WARN_DAYS,
    FMP_BAD_DAYS,
    FMP_WARN_DAYS,
    TONE_BAD,
    TONE_OK,
    TONE_WARN,
    age_days,
    freshness_verdict,
    parse_age_days,
    per_source_tone,
)

NOW = datetime(2026, 8, 1, 12, 0, 0)  # naive-UTC, per repo convention


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# --------------------------------------------------------------------------- #
# per_source_tone — the worst-of-two-bars rule, boundaries pinned each side
# --------------------------------------------------------------------------- #


def test_both_fresh_is_ok() -> None:
    assert per_source_tone(0.5, 1.0) == TONE_OK


def test_fmp_exactly_at_warn_boundary_is_ok() -> None:
    assert per_source_tone(FMP_WARN_DAYS, 0.0) == TONE_OK  # > required, not >=


def test_fmp_5d_is_warn() -> None:
    """FMP 5d (between the 3d warn and 14d bad bars) -> warn, build fresh."""
    assert per_source_tone(5.0, 0.0) == TONE_WARN


def test_fmp_just_past_bad_boundary_is_bad() -> None:
    assert per_source_tone(FMP_BAD_DAYS + 0.1, 0.0) == TONE_BAD


def test_build_exactly_at_warn_boundary_is_ok() -> None:
    assert per_source_tone(0.0, BUILD_WARN_DAYS) == TONE_OK


def test_build_25d_is_warn() -> None:
    """Build 25d (between the 10d warn and 30d bad bars) -> warn, FMP fresh."""
    assert per_source_tone(0.0, 25.0) == TONE_WARN


def test_build_just_past_bad_boundary_is_bad() -> None:
    assert per_source_tone(0.0, BUILD_BAD_DAYS + 0.1) == TONE_BAD


def test_fmp_5d_and_build_25d_both_warn_together() -> None:
    """The concrete pair named in the PR: FMP 5d -> warn, build 25d -> warn
    under the per-source rule (worst-of-two, both land in their own warn
    band) -> overall warn, not bad."""
    assert per_source_tone(5.0, 25.0) == TONE_WARN


def test_either_age_missing_is_bad() -> None:
    assert per_source_tone(None, 0.0) == TONE_BAD
    assert per_source_tone(0.0, None) == TONE_BAD
    assert per_source_tone(None, None) == TONE_BAD


# --------------------------------------------------------------------------- #
# The rule DIVERGENCE from the old ticker_command_center max-of-both/7-21d
# bar — concrete ages that flip verdict under the new per-source rule.
# --------------------------------------------------------------------------- #


def test_stale_fmp_fresh_build_flips_from_old_tcc_ok_to_warn() -> None:
    """Old TCC rule: max(10d build, 0d fmp)=10d <= 21 and > 7 -> warn already
    matches; but FMP 5d alone (old TCC max=5d -> ok, since <=7) now reads
    warn under the per-source rule (5d > FMP's own 3d bar) even though the
    build is perfectly fresh — the exact case the old shared-bar rule hid."""
    assert per_source_tone(5.0, 0.0) == TONE_WARN  # per-source: warn
    # (old TCC: max(5, 0) = 5 <= 7 -> "ok" — the divergence this PR fixes)


def test_moderately_stale_build_fresh_fmp_flips_from_old_tcc_warn_to_ok() -> None:
    """Old TCC rule: max(9d build, 0d fmp)=9d > 7 -> warn. Per-source: build
    9d is under its own 10d warn bar, FMP fresh -> ok. A build cadence that
    is completely normal (builds are weekly-ish) no longer reads as a warning
    just because the shared 7d bar was tuned for the faster FMP cadence."""
    assert per_source_tone(0.0, 9.0) == TONE_OK  # per-source: ok
    # (old TCC: max(9, 0) = 9 > 7 -> "warn" — the divergence this PR fixes)


# --------------------------------------------------------------------------- #
# age_days / parse_age_days — naive-UTC convention + aware normalization
# --------------------------------------------------------------------------- #


def test_age_days_none_stamp_is_none() -> None:
    assert age_days(None, now=NOW) is None


def test_age_days_naive_stamp_treated_as_utc() -> None:
    stamp = NOW - timedelta(days=3)
    assert age_days(stamp, now=NOW) == 3.0


def test_age_days_normalizes_aware_stamp_and_aware_now() -> None:
    aware_now = NOW.replace(tzinfo=UTC)
    aware_stamp = (NOW - timedelta(days=2)).replace(tzinfo=UTC)
    assert age_days(aware_stamp, now=aware_now) == 2.0


def test_age_days_mixed_naive_and_aware_do_not_crash() -> None:
    aware_stamp = (NOW - timedelta(days=1)).replace(tzinfo=UTC)
    assert age_days(aware_stamp, now=NOW) == 1.0  # naive now, aware stamp


def test_parse_age_days_missing_or_bad_iso_is_none() -> None:
    assert parse_age_days(None, now=NOW) is None
    assert parse_age_days("", now=NOW) is None
    assert parse_age_days("not-a-date", now=NOW) is None


def test_parse_age_days_parses_naive_iso() -> None:
    assert parse_age_days(_iso(4.0), now=NOW) == 4.0


# --------------------------------------------------------------------------- #
# freshness_verdict — the one call both call sites make
# --------------------------------------------------------------------------- #


def test_freshness_verdict_both_missing_is_bad() -> None:
    v = freshness_verdict(fmp_at=None, build_at=None, now=NOW)
    assert v.tone == TONE_BAD
    assert v.fmp_age_days is None
    assert v.build_age_days is None


def test_freshness_verdict_pins_ages_and_tone() -> None:
    v = freshness_verdict(fmp_at=_iso(5.0), build_at=_iso(25.0), now=NOW)
    assert v.fmp_age_days == 5.0
    assert v.build_age_days == 25.0
    assert v.tone == TONE_WARN
