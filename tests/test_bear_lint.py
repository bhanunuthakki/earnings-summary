"""Bear-realism lint (Monthly Red Team Phase 1 guard 2) — classification over
``dcf_runs`` bear scenarios, plus the Risk-tab section markup."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bear_lint import (
    STATUS_MISSING,
    STATUS_NOT_A_BEAR,
    STATUS_OK,
    STATUS_SHALLOW,
    BearLintFinding,
    BearLintReport,
    build_bear_lint,
)
from pipeline.portfolio_panel import _bear_lint_section  # pyright: ignore[reportPrivateUsage]


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, is_latest INTEGER, "
        "segment_name TEXT, valuation_date TEXT, npv_per_share REAL, live_price REAL, "
        "assumption_snapshot_json TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def _snapshot(bear_fv: float | None, *, provenance: str | None = None) -> str | None:
    if bear_fv is None:
        return None
    bear: dict[str, object] = {"fair_value_per_share_usd": bear_fv}
    if provenance is not None:
        bear["provenance"] = provenance
    return json.dumps({"scenarios": {"bear": bear, "base": {"fair_value_per_share_usd": 120.0}}})


def _insert_run(
    db_path: Path,
    ticker: str,
    *,
    created_at: str = "2026-06-01T00:00:00",
    is_latest: int | None = 1,
    segment_name: str | None = None,
    live_price: float | None = 100.0,
    bear_fv: float | None = 60.0,
    provenance: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs "
        "(ticker, created_at, is_latest, segment_name, valuation_date, npv_per_share, "
        "live_price, assumption_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            created_at,
            is_latest,
            segment_name,
            "2026-06-15",
            120.0,
            live_price,
            _snapshot(bear_fv, provenance=provenance),
        ),
    )
    conn.commit()
    conn.close()


def _weights_cache(repo_root: Path, weights: dict[str, float]) -> None:
    cache_dir = repo_root / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "portfolio_weights.json").write_text(
        json.dumps({"weights": weights}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# build_bear_lint — classification
# ---------------------------------------------------------------------------


def test_no_weights_returns_empty_report(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    report = build_bear_lint(db_path, tmp_path)
    assert report.findings == []


def test_not_a_bear_when_bear_fv_at_or_above_price(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"UBER": 0.5})
    _insert_run(db_path, "UBER", live_price=100.0, bear_fv=105.0)  # bear ABOVE price
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_NOT_A_BEAR
    assert "AT/ABOVE live price" in f.reason


def test_not_a_bear_when_bear_fv_equals_price(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"WIX": 0.3})
    _insert_run(db_path, "WIX", live_price=100.0, bear_fv=100.0)
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_NOT_A_BEAR


def test_shallow_when_bear_below_price_but_under_floor(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NVO": 0.2})
    _insert_run(db_path, "NVO", live_price=100.0, bear_fv=90.0)  # -10%, under 15% floor
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_SHALLOW
    assert f.bear_return_pct is not None and abs(f.bear_return_pct - (-10.0)) < 1e-9


def test_ok_when_bear_clears_the_floor(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.4})
    _insert_run(db_path, "AAA", live_price=100.0, bear_fv=60.0)  # -40%
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_OK
    assert report.flagged == []  # OK findings are not flagged


def test_missing_when_no_run_on_file(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"MELI": 0.3})
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_MISSING
    assert f.reason == "no top-level DCF run on file"


def test_missing_when_run_has_no_bear_scenario(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.3})
    _insert_run(db_path, "NU", live_price=100.0, bear_fv=None)
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_MISSING
    assert f.reason == "no bear scenario persisted"


def test_segment_level_rows_are_ignored(tmp_path: Path) -> None:
    """A segment-level (SOTP sub-model) row must not stand in for the
    top-level unsegmented run — the lint reads segment_name IS NULL only."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"BN": 0.2})
    _insert_run(db_path, "BN", segment_name="Insurance", live_price=100.0, bear_fv=60.0)
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_MISSING  # the segment row doesn't count


def test_superseded_rows_are_ignored(tmp_path: Path) -> None:
    """A superseded (is_latest=0) row must not be read as current."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.2})
    _insert_run(db_path, "AAA", is_latest=0, live_price=100.0, bear_fv=60.0)
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_MISSING


def test_provenance_rides_along(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"UBER": 0.5})
    _insert_run(db_path, "UBER", live_price=100.0, bear_fv=105.0, provenance="seed")
    report = build_bear_lint(db_path, tmp_path)
    (f,) = report.findings
    assert f.provenance == "seed"


def test_findings_sorted_by_severity_then_ticker(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"ZZZ": 0.1, "AAA": 0.1, "BBB": 0.1, "CCC": 0.1})
    _insert_run(db_path, "ZZZ", live_price=100.0, bear_fv=60.0)  # ok
    _insert_run(db_path, "AAA", live_price=100.0, bear_fv=105.0)  # not_a_bear
    _insert_run(db_path, "BBB", live_price=100.0, bear_fv=90.0)  # shallow
    # CCC gets no run -> missing
    report = build_bear_lint(db_path, tmp_path)
    statuses = [f.status for f in report.findings]
    assert statuses == [STATUS_NOT_A_BEAR, STATUS_MISSING, STATUS_SHALLOW, STATUS_OK]


def test_missing_db_degrades_to_missing_findings(tmp_path: Path) -> None:
    _weights_cache(tmp_path, {"AAA": 0.5})
    report = build_bear_lint(tmp_path / "nope.db", tmp_path)
    (f,) = report.findings
    assert f.status == STATUS_MISSING


# ---------------------------------------------------------------------------
# Risk-tab section markup
# ---------------------------------------------------------------------------


def test_bear_lint_section_empty_state() -> None:
    html = _bear_lint_section(None)
    assert "Bear-realism lint" in html
    assert "No weighted holdings to lint yet" in html


def test_bear_lint_section_all_clear() -> None:
    report = BearLintReport(
        findings=[
            BearLintFinding(
                ticker="AAA",
                status=STATUS_OK,
                weight_pct=10.0,
                live_price=100.0,
                bear_fv=60.0,
                bear_return_pct=-40.0,
                provenance="thesis",
                reason="bear case -40% below live",
            )
        ]
    )
    html = _bear_lint_section(report)
    assert "Bear-realism lint" in html
    assert "Every held name clears the lint" in html
    assert "AAA" not in html  # no per-row table when nothing is flagged


def test_bear_lint_section_renders_flagged_rows() -> None:
    report = BearLintReport(
        findings=[
            BearLintFinding(
                ticker="UBER",
                status=STATUS_NOT_A_BEAR,
                weight_pct=8.0,
                live_price=90.0,
                bear_fv=95.0,
                bear_return_pct=5.5,
                provenance="seed",
                reason="bear fair value $95.00 sits AT/ABOVE live price $90.00 — not downside",
            ),
            BearLintFinding(
                ticker="MELI",
                status=STATUS_MISSING,
                weight_pct=20.0,
                live_price=None,
                bear_fv=None,
                bear_return_pct=None,
                provenance=None,
                reason="no top-level DCF run on file",
            ),
        ]
    )
    html = _bear_lint_section(report)
    assert "UBER" in html and "k-pill-bad" in html and "NOT A BEAR" in html
    assert "MELI" in html and "MISSING" in html
    assert "seed" in html  # provenance chip
    assert "AT/ABOVE live price" in html
