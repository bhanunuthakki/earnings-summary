"""Tests for the estimates-widening revenue enrichment in
src/surprise_sources.py — archived pre-release 0q consensus + Yahoo quarterly
actuals filling the yfinance source's revenue gap.

All archive access uses fixture snapshot dirs; quarterly actuals are injected
mappings. No yfinance import, no network."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from surprise_sources import (
    SurpriseHit,
    archived_yf_revenue_estimate,
    default_sources,
    enrich_hits_with_yf_revenue,
    quarter_end_for_release,
)


def _write_yf_snapshot(
    root: Path, snap_date: str, ticker: str, *, rev_0q_avg: float, analysts: int = 20
) -> None:
    d = root / snap_date
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "asof_date": snap_date,
        "fetched_at": f"{snap_date}T12:00:00+00:00",
        "source": "yfinance",
        "revenue_estimate": [
            {"period": "0q", "avg": rev_0q_avg, "numberOfAnalysts": analysts},
            {"period": "+1y", "avg": rev_0q_avg * 4.4},
        ],
    }
    (d / f"{ticker}_yf_estimates.json").write_text(json.dumps(payload), encoding="utf-8")


def _hit(release: date, *, revenue_actual: Decimal | None = None) -> SurpriseHit:
    return SurpriseHit(
        ticker="WIX",
        release_date=release,
        eps_estimate=Decimal("1.50"),
        eps_actual=Decimal("1.62"),
        revenue_estimate=None,
        revenue_actual=revenue_actual,
        eps_surprise_pct=Decimal("8.00"),
        revenue_surprise_pct=None,
        num_analysts_eps=None,
        num_analysts_revenue=None,
        source_name="yfinance",
        source_url=None,
    )


# --- archived_yf_revenue_estimate: the snapshot window rule ------------------


def test_archive_uses_latest_snapshot_in_post_quarter_pre_release_window(
    tmp_path: Path,
) -> None:
    root = tmp_path / "yf_snaps"
    _write_yf_snapshot(root, "2026-07-01", "WIX", rev_0q_avg=480e6)  # early consensus
    _write_yf_snapshot(root, "2026-07-20", "WIX", rev_0q_avg=485e6)  # final consensus
    got = archived_yf_revenue_estimate(
        "WIX",
        quarter_end=date(2026, 6, 30),
        release_date=date(2026, 7, 28),
        snapshots_dir=root,
    )
    assert got is not None
    est, analysts, snap_date = got
    assert est == Decimal("485000000")
    assert analysts == 20
    assert snap_date == "2026-07-20"


def test_archive_ignores_within_quarter_and_post_release_snapshots(tmp_path: Path) -> None:
    """A snapshot DURING the quarter (0q mapping ambiguous) or ON/AFTER the
    release day must not be used."""
    root = tmp_path / "yf_snaps"
    _write_yf_snapshot(root, "2026-06-15", "WIX", rev_0q_avg=470e6)  # within quarter
    _write_yf_snapshot(root, "2026-07-28", "WIX", rev_0q_avg=999e6)  # release day
    assert (
        archived_yf_revenue_estimate(
            "WIX",
            quarter_end=date(2026, 6, 30),
            release_date=date(2026, 7, 28),
            snapshots_dir=root,
        )
        is None
    )


def test_archive_missing_dir_or_ticker_is_none(tmp_path: Path) -> None:
    assert (
        archived_yf_revenue_estimate(
            "WIX",
            quarter_end=date(2026, 6, 30),
            release_date=date(2026, 7, 28),
            snapshots_dir=tmp_path / "nope",
        )
        is None
    )
    root = tmp_path / "yf_snaps"
    _write_yf_snapshot(root, "2026-07-20", "OTHER", rev_0q_avg=1e6)
    assert (
        archived_yf_revenue_estimate(
            "WIX",
            quarter_end=date(2026, 6, 30),
            release_date=date(2026, 7, 28),
            snapshots_dir=root,
        )
        is None
    )


# --- quarter_end_for_release ------------------------------------------------


def test_quarter_end_mapping_picks_latest_within_lag() -> None:
    actuals = {
        date(2026, 3, 31): Decimal("450000000"),
        date(2026, 6, 30): Decimal("487000000"),
    }
    assert quarter_end_for_release(date(2026, 7, 28), actuals) == date(2026, 6, 30)
    # a release 8 months after the last known period end maps to nothing
    assert quarter_end_for_release(date(2027, 3, 1), actuals) is None


# --- enrich_hits_with_yf_revenue --------------------------------------------


def test_enrichment_fills_revenue_and_recomputes_surprise(tmp_path: Path) -> None:
    root = tmp_path / "yf_snaps"
    _write_yf_snapshot(root, "2026-07-20", "WIX", rev_0q_avg=480e6)
    actuals = {date(2026, 6, 30): Decimal("487200000")}
    [hit] = enrich_hits_with_yf_revenue(
        [_hit(date(2026, 7, 28))],
        ticker="WIX",
        snapshots_dir=root,
        quarterly_revenue=actuals,
    )
    assert hit.revenue_estimate == Decimal("480000000")
    assert hit.revenue_actual == Decimal("487200000")
    assert hit.revenue_surprise_pct == Decimal("1.50")
    assert hit.num_analysts_revenue == 20
    # provenance: still a single-vendor record
    assert hit.source_name == "yfinance"
    # EPS fields untouched
    assert hit.eps_actual == Decimal("1.62")


def test_enrichment_honest_gap_before_archive_start(tmp_path: Path) -> None:
    """A quarter released before the archive existed keeps revenue estimate
    None (actual may still fill) — never backfilled from later snapshots."""
    root = tmp_path / "yf_snaps"
    _write_yf_snapshot(root, "2026-07-20", "WIX", rev_0q_avg=480e6)
    actuals = {
        date(2026, 3, 31): Decimal("450000000"),
        date(2026, 6, 30): Decimal("487200000"),
    }
    old, new = enrich_hits_with_yf_revenue(
        [_hit(date(2026, 4, 29)), _hit(date(2026, 7, 28))],
        ticker="WIX",
        snapshots_dir=root,
        quarterly_revenue=actuals,
    )
    assert old.revenue_estimate is None
    assert old.revenue_actual == Decimal("450000000")  # actual is known
    assert old.revenue_surprise_pct is None  # no estimate -> no surprise claim
    assert new.revenue_estimate == Decimal("480000000")


def test_enrichment_leaves_already_filled_hits_untouched(tmp_path: Path) -> None:
    filled = _hit(date(2026, 7, 28), revenue_actual=Decimal("1"))
    [out] = enrich_hits_with_yf_revenue(
        [filled], ticker="WIX", snapshots_dir=tmp_path, quarterly_revenue={}
    )
    assert out is filled


def test_enrichment_unmappable_release_passes_through(tmp_path: Path) -> None:
    [out] = enrich_hits_with_yf_revenue(
        [_hit(date(2026, 7, 28))], ticker="WIX", snapshots_dir=tmp_path, quarterly_revenue={}
    )
    assert out.revenue_estimate is None and out.revenue_actual is None


# --- default_sources wiring --------------------------------------------------


def test_default_sources_signature_accepts_yf_snapshots_dir(tmp_path: Path) -> None:
    sources = default_sources(tmp_path / "fmp", yf_snapshots_dir=tmp_path / "yf_snaps")
    assert [s.name for s in sources] == ["fmp_calendar", "yfinance"]
    # omitting the kwarg keeps the legacy shape working
    legacy = default_sources(tmp_path / "fmp")
    assert [s.name for s in legacy] == ["fmp_calendar", "yfinance"]
