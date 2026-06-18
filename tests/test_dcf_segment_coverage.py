"""Tests for the base-FY segment-coverage guard (``src/dcf/segment_coverage.py``).

The redesigned per-segment DCF derives its modeled segments from the *latest quarter's*
FMP product segmentation. When FMP drops a big segment in that latest quarter (or a
quarter goes missing), the modeled set silently covers only a fraction of the company and
the whole DCF is built on that fraction. The guard generalises the builder's original
``len(prod) < 2`` / ``seg_base_total <= 0`` checks to also catch this *partial*-coverage
case by comparing the modeled segment revenue to the income statement's base-year total.

The headline case is the realistic VEEV shape: the latest quarter reports two (small)
segments — so ``len >= 2`` and the original guard would NOT fire — but the two larger
segments it dropped mean the modeled set covers <50% of base-year revenue.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import segment_coverage as sc  # noqa: E402

PERIODS = ("Q1", "Q2", "Q3", "Q4")
BASE_FY = 2025
LATEST = (2025, "Q4")  # newest quarter, inside the base fiscal year


def _income(rev_per_q: float) -> dict[tuple[int, str], dict[str, object]]:
    """Income-statement index with constant quarterly revenue for the base FY."""
    return {(BASE_FY, p): {"revenue": rev_per_q} for p in PERIODS}


def _segs(
    history: dict[str, float], latest: dict[str, float]
) -> dict[tuple[int, str], dict[str, object]]:
    """Product-segment index: Q1-Q3 carry ``history``; the latest quarter carries ``latest``,
    so a contaminated/partial latest quarter can differ from the full base-year history."""
    out: dict[tuple[int, str], dict[str, object]] = {
        (BASE_FY, p): dict(history) for p in ("Q1", "Q2", "Q3")
    }
    out[LATEST] = dict(latest)
    return out


# Four segments: a large pair (the VEEV R&D shape) and a small pair (Commercial).
_FULL = {"BigA": 300.0, "BigB": 250.0, "SmallA": 120.0, "SmallB": 80.0}  # sums to 750/q
_REV_PER_Q = 750.0  # income matches the full segment total exactly


def test_full_coverage_keeps_per_segment_build() -> None:
    """Latest quarter reports the COMPLETE segment set -> per-segment build stands."""
    cov = sc.resolve_product_segments(
        _segs(_FULL, _FULL), _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST
    )
    assert cov.single_seg is False
    assert cov.reason is None
    assert cov.prod == ["BigA", "BigB", "SmallA", "SmallB"]  # largest-first
    assert cov.coverage == 1.0


def test_partial_coverage_latest_quarter_dropped_big_segments() -> None:
    """THE bug: the latest quarter reports only the two SMALL segments (len >= 2, so the
    original len/zero guard misses it), but they cover ~27% of base-year revenue."""
    contaminated_latest = {"SmallA": 120.0, "SmallB": 80.0}  # the two Big segments dropped
    assert len(contaminated_latest) >= 2  # the original len(prod) < 2 guard would NOT fire
    cov = sc.resolve_product_segments(
        _segs(_FULL, contaminated_latest), _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST
    )
    assert cov.single_seg is True
    assert cov.reason is not None and cov.reason.startswith("coverage")
    assert cov.prod == [sc.TOTAL_COMPANY]
    # modeled = (120+80)*4 = 800; income = 750*4 = 3000 -> 0.2667
    assert cov.coverage is not None and abs(cov.coverage - 0.2667) < 0.001


def test_zero_coverage_segments_absent_in_base_year() -> None:
    """LITE shape: the latest quarter (a current, INCOMPLETE FY) reports segments, but the
    base COMPLETE fiscal year had no segment disclosure -> base-year segment sum is zero."""
    latest_key = (2026, "Q2")  # newest quarter sits in the incomplete current FY, not base_fy
    segs: dict[tuple[int, str], dict[str, object]] = {latest_key: {"BigA": 300.0, "BigB": 250.0}}
    cov = sc.resolve_product_segments(segs, _income(_REV_PER_Q), PERIODS, BASE_FY, latest_key)
    assert cov.single_seg is True
    assert cov.reason == "zero base-year segment revenue"  # seg_base_total == 0 over 2025
    assert cov.coverage == 0.0
    assert cov.prod == [sc.TOTAL_COMPANY]


def test_single_segment_triggers_len_guard() -> None:
    """Latest quarter reports one segment -> the original <2 fallback still fires."""
    cov = sc.resolve_product_segments(
        _segs(_FULL, {"BigA": 300.0}), _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST
    )
    assert cov.single_seg is True
    assert cov.reason == "<2 segments (1)"
    assert cov.prod == [sc.TOTAL_COMPANY]


def test_no_segments_at_all_triggers_len_guard() -> None:
    """No product-segment record for the latest quarter at all -> whole-company."""
    cov = sc.resolve_product_segments({}, _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST)
    assert cov.single_seg is True
    assert cov.reason == "<2 segments (0)"
    assert cov.coverage == 0.0  # no segment revenue, income is present


def test_coverage_at_floor_is_kept_strict_comparison() -> None:
    """Coverage exactly at the floor is KEPT (the comparison is strict ``<``); a hair below
    downgrades. Driven through an explicit floor so it is robust to float representation."""
    segs = _segs({"Big": 510.0, "Small": 340.0}, {"Big": 510.0, "Small": 340.0})  # 850/q
    base = sc.resolve_product_segments(segs, _income(1000.0), PERIODS, BASE_FY, LATEST)
    assert base.coverage is not None and abs(base.coverage - 0.85) < 1e-12  # 3400/4000
    at = sc.resolve_product_segments(
        segs, _income(1000.0), PERIODS, BASE_FY, LATEST, floor=base.coverage
    )
    assert at.single_seg is False, "coverage exactly at the floor must be kept"
    below = sc.resolve_product_segments(
        segs, _income(1000.0), PERIODS, BASE_FY, LATEST, floor=base.coverage + 1e-6
    )
    assert below.single_seg is True


def test_default_floor_downgrades_partial_but_custom_floor_keeps_it() -> None:
    """The default floor (0.80-0.85) downgrades the ~27% case; an explicit lower floor
    (DCF_COVERAGE_FLOOR plumbs this) reverts to the original len/zero-only behaviour."""
    segs = _segs(_FULL, {"SmallA": 120.0, "SmallB": 80.0})
    assert sc.resolve_product_segments(
        segs, _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST
    ).single_seg
    kept = sc.resolve_product_segments(
        segs, _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST, floor=0.0
    )
    assert kept.single_seg is False
    assert kept.prod == ["SmallA", "SmallB"]  # largest-first


def test_missing_income_base_does_not_downgrade() -> None:
    """When the income base is zero/absent, coverage is undefined: the guard must NOT
    force whole-company off a None ratio — the builder's separate rev_ly<=0 SKIP owns that."""
    cov = sc.resolve_product_segments(_segs(_FULL, _FULL), {}, PERIODS, BASE_FY, LATEST)
    assert cov.coverage is None
    assert cov.single_seg is False  # >=2 segments, nonzero seg total, no coverage verdict
    assert cov.income_base_total == 0.0


def test_floor_is_in_sane_range() -> None:
    """Document the intended floor band (0.80-0.85): generous enough to ignore normal
    unallocated/eliminations, strict enough to catch a dropped ~15%+ segment."""
    assert 0.80 <= sc.COVERAGE_FLOOR <= 0.85


def test_bool_segment_value_is_not_summed() -> None:
    """A stray boolean must never be coerced to 0/1 into a revenue total."""
    segs = _segs({"BigA": 300.0, "BigB": True}, {"BigA": 300.0, "BigB": True})  # type: ignore[dict-item]
    cov = sc.resolve_product_segments(segs, _income(_REV_PER_Q), PERIODS, BASE_FY, LATEST)
    # Only BigA is numeric -> len(prod) < 2 -> whole-company (bool dropped, not counted).
    assert cov.prod == [sc.TOTAL_COMPANY]
    assert cov.reason == "<2 segments (1)"
