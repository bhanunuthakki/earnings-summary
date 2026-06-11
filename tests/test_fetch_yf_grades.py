"""Tests for the additive yfinance rating-changes feed (``execution/fetch_yf_grades.py``).

Hermetic — yfinance is never imported (the ``records_loader`` seam injects
fixture records; one test exercises the real-pandas ``_frame_to_records``
bridge since pandas ships with the venv as a yfinance dependency). Covers the
action-verb headline mapping, price-target suffixes (NaN-safe), the GradeDate
window, the synthetic per-event URL uniqueness, drop-never-pad on missing
essentials, and full degradation when the loader raises.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import execution.fetch_yf_grades as yfgrades
from dashboard.inbox_rank import _RATING_HEADLINE_RX  # pyright: ignore[reportPrivateUsage]
from news.store import SOURCE_FEED_YF_GRADES, NewsRow

NOW = datetime(2026, 6, 11, 6, 0, 0)


def _record(
    *,
    when: datetime | None = None,
    firm: str = "Morgan Stanley",
    to_grade: str = "Overweight",
    from_grade: str = "Equal-Weight",
    action: str = "up",
    pt_action: str = "Raises",
    current_pt: object = 700.0,
    prior_pt: object = 620.0,
) -> dict[str, object]:
    return {
        "GradeDate": when or datetime(2026, 6, 10, 14, 30, 0),
        "Firm": firm,
        "ToGrade": to_grade,
        "FromGrade": from_grade,
        "Action": action,
        "priceTargetAction": pt_action,
        "currentPriceTarget": current_pt,
        "priorPriceTarget": prior_pt,
    }


def _fetch(records: list[dict[str, object]], days: int = 3) -> list[NewsRow]:
    return yfgrades.fetch_grades_for_ticker(
        "META", days=days, now=NOW, records_loader=lambda _t: records
    )


# ---------------------------------------------------------------------------
# Headline mapping
# ---------------------------------------------------------------------------


def test_upgrade_with_price_target_change() -> None:
    (row,) = _fetch([_record()])
    assert (
        row.headline
        == "Morgan Stanley upgrades META to Overweight from Equal-Weight; PT $620 → $700"
    )
    assert row.ticker == "META"
    assert row.source == "Morgan Stanley"
    assert row.source_feed == SOURCE_FEED_YF_GRADES
    assert row.published_at == "2026-06-10 14:30:00"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("down", "JP Morgan downgrades META to Neutral from Buy"),
        ("init", "JP Morgan initiates coverage on META at Neutral"),
        ("reit", "JP Morgan reiterates Neutral on META"),
        ("main", "JP Morgan maintains Neutral on META"),
        ("mystery", "JP Morgan rates META Neutral"),  # unknown verbs degrade, not drop
    ],
)
def test_action_verbs(action: str, expected: str) -> None:
    (row,) = _fetch(
        [
            _record(
                firm="JP Morgan",
                to_grade="Neutral",
                from_grade="Buy",
                action=action,
                current_pt=None,
                prior_pt=None,
            )
        ]
    )
    assert row.headline == expected


def test_headlines_match_the_inbox_rating_regex() -> None:
    """The shape contract with inbox_rank: even without the source_feed tag,
    these headlines would categorize as Rating changes."""
    for action in ("up", "down", "init", "reit", "main"):
        (row,) = _fetch([_record(action=action)])
        assert _RATING_HEADLINE_RX.search(row.headline), row.headline


def test_price_target_suffix_variants() -> None:
    nan = float("nan")
    unchanged, only_current, none_at_all, cents = (
        _fetch([_record(current_pt=700.0, prior_pt=700.0)])[0],
        _fetch([_record(current_pt=700.0, prior_pt=nan)])[0],
        _fetch([_record(current_pt=nan, prior_pt=nan)])[0],
        _fetch([_record(current_pt=712.5, prior_pt=620.0)])[0],
    )
    assert unchanged.headline.endswith("; PT $700")
    assert only_current.headline.endswith("; PT $700")
    assert "PT" not in none_at_all.headline
    assert cents.headline.endswith("; PT $620 → $712.50")


def test_redundant_from_grade_is_omitted() -> None:
    (row,) = _fetch([_record(to_grade="Buy", from_grade="Buy", current_pt=None, prior_pt=None)])
    assert row.headline == "Morgan Stanley upgrades META to Buy"


# ---------------------------------------------------------------------------
# Window, essentials, degradation
# ---------------------------------------------------------------------------


def test_window_excludes_old_grades() -> None:
    rows = _fetch(
        [
            _record(when=datetime(2026, 6, 10, 9, 0, 0)),
            _record(when=datetime(2026, 6, 1, 9, 0, 0), firm="Baird"),
        ],
        days=3,
    )
    assert [r.source for r in rows] == ["Morgan Stanley"]


def test_rows_missing_essentials_are_dropped_never_padded() -> None:
    assert _fetch([_record(firm=""), _record(to_grade=""), {"GradeDate": "bogus"}]) == []


def test_same_day_events_get_distinct_urls() -> None:
    a, b = _fetch(
        [
            _record(when=datetime(2026, 6, 10, 9, 0, 0)),
            _record(when=datetime(2026, 6, 10, 11, 0, 0), firm="B of A Securities"),
        ]
    )
    assert a.url != b.url
    assert a.url.startswith("https://finance.yahoo.com/quote/META/analysis#grade-")


def test_injected_loader_exceptions_propagate() -> None:
    """The injected-loader seam does NOT swallow exceptions — degradation is
    layered where it belongs: inside the LIVE loader (_load_grade_records
    catches its own yfinance failures) and in the dispatcher's _safe_grades
    wrapper. A buggy test fixture should fail loudly here, not silently
    produce zero rows."""

    def _boom(_t: str) -> list[dict[str, object]]:
        raise RuntimeError("yahoo melted")

    with pytest.raises(RuntimeError):
        yfgrades.fetch_grades_for_ticker("META", now=NOW, records_loader=_boom)


def test_frame_to_records_with_real_pandas() -> None:
    """The duck-typed DataFrame bridge against the real library (pandas is in
    the venv as a yfinance dependency): index name restored as a column, cell
    values preserved."""
    pd = pytest.importorskip("pandas")
    frame_to_records = yfgrades._frame_to_records  # pyright: ignore[reportPrivateUsage]
    grade_datetime = yfgrades._grade_datetime  # pyright: ignore[reportPrivateUsage]
    frame = pd.DataFrame(
        {"Firm": ["Wedbush"], "ToGrade": ["Outperform"]},
        index=pd.DatetimeIndex([datetime(2026, 6, 10, 14, 0, 0)], name="GradeDate"),
    )
    records = frame_to_records(frame)
    assert len(records) == 1
    assert records[0]["Firm"] == "Wedbush"
    assert grade_datetime(records[0]["GradeDate"]) == datetime(2026, 6, 10, 14, 0, 0)
    # Non-frames degrade to [] rather than raising.
    assert frame_to_records(None) == []
    assert frame_to_records("bogus") == []
