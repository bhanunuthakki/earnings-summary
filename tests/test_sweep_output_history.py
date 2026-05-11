"""Unit tests for execution/sweep_output_history.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    src = PROJECT_ROOT / "execution" / "sweep_output_history.py"
    spec = importlib.util.spec_from_file_location("sweep_output_history", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sweep_output_history"] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed(ticker_dir: Path, filenames: list[str]) -> None:
    ticker_dir.mkdir(parents=True, exist_ok=True)
    for n in filenames:
        (ticker_dir / n).write_text("x", encoding="utf-8")


def test_keeps_latest_n_dates(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(td, [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
        "2026-05-06_report.html",
        "2026-05-07_report.html",
        "2026-05-08_report.html",
        "2026-05-09_report.html",
    ])

    kept, deleted = mod.sweep_ticker(td, keep=3, dry_run=False)

    assert kept == 3
    assert deleted == 3
    surviving = sorted(p.name for p in td.iterdir())
    assert surviving == [
        "2026-05-07_report.html",
        "2026-05-08_report.html",
        "2026-05-09_report.html",
    ]


def test_groups_flavor_variants_under_one_date_slot(tmp_path: Path) -> None:
    """All files sharing a date count as one kept slot."""
    mod = _load_module()
    td = tmp_path / "META"
    _seed(td, [
        # Date A — 4 flavor variants
        "2026-05-10_report.html",
        "2026-05-10_eval_report.html",
        "2026-05-10_portfolio_report.html",
        "2026-05-10_dcf.xlsx",
        # Date B — 1 file (older)
        "2026-05-04_report.html",
    ])

    kept, deleted = mod.sweep_ticker(td, keep=1, dry_run=False)

    assert kept == 1  # one date slot retained
    assert deleted == 1  # only the older single file got deleted
    surviving = sorted(p.name for p in td.iterdir())
    assert surviving == [
        "2026-05-10_dcf.xlsx",
        "2026-05-10_eval_report.html",
        "2026-05-10_portfolio_report.html",
        "2026-05-10_report.html",
    ]


def test_preserves_files_without_date_prefix(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(td, [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
        "index.json",
        "README.md",
    ])

    kept, deleted = mod.sweep_ticker(td, keep=1, dry_run=False)

    assert kept == 1
    assert deleted == 1  # only the older dated file
    surviving = sorted(p.name for p in td.iterdir())
    assert surviving == ["2026-05-05_report.html", "README.md", "index.json"]


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(td, [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
        "2026-05-06_report.html",
    ])

    kept, deleted = mod.sweep_ticker(td, keep=1, dry_run=True)

    assert kept == 1
    assert deleted == 2  # counted as if deleted
    assert sorted(p.name for p in td.iterdir()) == [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
        "2026-05-06_report.html",
    ]


def test_empty_dir_is_a_no_op(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    td.mkdir()

    kept, deleted = mod.sweep_ticker(td, keep=5, dry_run=False)

    assert kept == 0
    assert deleted == 0


def test_fewer_dates_than_keep_deletes_nothing(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(td, ["2026-05-04_report.html", "2026-05-05_report.html"])

    kept, deleted = mod.sweep_ticker(td, keep=5, dry_run=False)

    assert kept == 2
    assert deleted == 0
    assert sorted(p.name for p in td.iterdir()) == [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
    ]


def test_main_returns_2_when_research_dir_missing(tmp_path: Path) -> None:
    mod = _load_module()
    args = [
        "--repo-root",
        str(tmp_path),
        "--dry-run",
    ]
    old_argv = sys.argv
    sys.argv = ["sweep_output_history.py", *args]
    try:
        rc = mod.main()
    finally:
        sys.argv = old_argv
    assert rc == 2


def test_main_rejects_keep_below_one(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "output" / "research").mkdir(parents=True)
    old_argv = sys.argv
    sys.argv = ["sweep_output_history.py", "--repo-root", str(tmp_path), "--keep", "0"]
    try:
        rc = mod.main()
    finally:
        sys.argv = old_argv
    assert rc == 2
