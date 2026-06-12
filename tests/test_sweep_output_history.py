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


def test_keeps_latest_of_each_kind(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(
        td,
        [
            "2026-05-04_report.html",
            "2026-05-05_report.html",
            "2026-05-06_report.html",
            "2026-05-07_report.html",
        ],
    )

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert kept == 1
    assert archived == 3
    top_level = sorted(p.name for p in td.iterdir() if p.is_file())
    assert top_level == ["2026-05-07_report.html"]
    archive_files = sorted(p.name for p in (td / "archive").iterdir())
    assert archive_files == [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
        "2026-05-06_report.html",
    ]


def test_per_kind_independence(tmp_path: Path) -> None:
    """The latest of each kind survives at the top level even when produced
    on different dates."""
    mod = _load_module()
    td = tmp_path / "META"
    _seed(
        td,
        [
            # Older eval/portfolio batch — these are the latest of their kind
            "2026-05-10_eval_report.html",
            "2026-05-10_portfolio_report.html",
            # Newer standard reports — should not displace the older eval/portfolio
            "2026-05-17_report.html",
            "2026-05-17_report.md",
            "2026-05-17_dcf.xlsx",
            # Stale standard reports — should go to archive
            "2026-05-09_report.html",
            "2026-05-09_dcf.xlsx",
        ],
    )

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert (
        kept == 5
    )  # one per kind: report.html, report.md, dcf.xlsx, eval_report.html, portfolio_report.html
    assert archived == 2  # 2026-05-09_report.html + 2026-05-09_dcf.xlsx
    top_level = sorted(p.name for p in td.iterdir() if p.is_file())
    assert top_level == [
        "2026-05-10_eval_report.html",
        "2026-05-10_portfolio_report.html",
        "2026-05-17_dcf.xlsx",
        "2026-05-17_report.html",
        "2026-05-17_report.md",
    ]
    archive_files = sorted(p.name for p in (td / "archive").iterdir())
    assert archive_files == [
        "2026-05-09_dcf.xlsx",
        "2026-05-09_report.html",
    ]


def test_preserves_files_without_date_prefix(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(
        td,
        [
            "2026-05-04_report.html",
            "2026-05-05_report.html",
            "index.json",
            "README.md",
        ],
    )

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert kept == 1
    assert archived == 1
    top_level = sorted(p.name for p in td.iterdir() if p.is_file())
    assert top_level == ["2026-05-05_report.html", "README.md", "index.json"]


def test_dry_run_moves_nothing(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(
        td,
        [
            "2026-05-04_report.html",
            "2026-05-05_report.html",
            "2026-05-06_report.html",
        ],
    )

    kept, archived = mod.archive_ticker_history(td, dry_run=True)

    assert kept == 1
    assert archived == 2  # counted as if archived
    # Nothing actually moved
    top_level = sorted(p.name for p in td.iterdir() if p.is_file())
    assert top_level == [
        "2026-05-04_report.html",
        "2026-05-05_report.html",
        "2026-05-06_report.html",
    ]
    assert not (td / "archive").exists()


def test_empty_dir_is_a_no_op(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    td.mkdir()

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert kept == 0
    assert archived == 0
    assert not (td / "archive").exists()


def test_single_date_is_a_no_op(tmp_path: Path) -> None:
    mod = _load_module()
    td = tmp_path / "META"
    _seed(td, ["2026-05-04_report.html", "2026-05-04_dcf.xlsx"])

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert kept == 2  # one per kind, two kinds
    assert archived == 0
    assert not (td / "archive").exists()
    top_level = sorted(p.name for p in td.iterdir() if p.is_file())
    assert top_level == ["2026-05-04_dcf.xlsx", "2026-05-04_report.html"]


def test_existing_archive_dir_is_not_rescanned(tmp_path: Path) -> None:
    """Files already in archive/ should not be considered when picking the
    latest-of-kind, and the archive/ subdir is never traversed as a ticker."""
    mod = _load_module()
    td = tmp_path / "META"
    _seed(
        td,
        [
            "2026-05-09_report.html",
            "2026-05-10_report.html",
        ],
    )
    archive_dir = td / "archive"
    archive_dir.mkdir()
    (archive_dir / "2026-05-01_report.html").write_text("old", encoding="utf-8")

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert kept == 1
    assert archived == 1
    top_level = sorted(p.name for p in td.iterdir() if p.is_file())
    assert top_level == ["2026-05-10_report.html"]
    archive_files = sorted(p.name for p in archive_dir.iterdir())
    assert archive_files == [
        "2026-05-01_report.html",  # was already in archive
        "2026-05-09_report.html",  # newly archived
    ]


def test_archive_overwrites_existing_destination(tmp_path: Path) -> None:
    """Re-running after a stale copy already exists in archive should overwrite
    (deterministic artifacts) rather than raise."""
    mod = _load_module()
    td = tmp_path / "META"
    _seed(
        td,
        [
            "2026-05-09_report.html",
            "2026-05-10_report.html",
        ],
    )
    archive_dir = td / "archive"
    archive_dir.mkdir()
    (archive_dir / "2026-05-09_report.html").write_text("stale", encoding="utf-8")
    (td / "2026-05-09_report.html").write_text("fresh", encoding="utf-8")

    kept, archived = mod.archive_ticker_history(td, dry_run=False)

    assert kept == 1
    assert archived == 1
    assert (archive_dir / "2026-05-09_report.html").read_text(encoding="utf-8") == "fresh"


def test_resolve_skips_archive_subdir_under_research(tmp_path: Path) -> None:
    """If someone creates output/research/archive/ by accident, we don't treat
    it as a ticker directory."""
    mod = _load_module()
    research = tmp_path / "output" / "research"
    (research / "META").mkdir(parents=True)
    (research / "archive").mkdir()
    (research / "META" / "2026-05-04_report.html").write_text("x", encoding="utf-8")

    dirs = mod._resolve_ticker_dirs(research, None)

    assert [d.name for d in dirs] == ["META"]


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


def test_main_end_to_end(tmp_path: Path) -> None:
    mod = _load_module()
    research = tmp_path / "output" / "research"
    (research / "META").mkdir(parents=True)
    _seed(
        research / "META",
        [
            "2026-05-04_report.html",
            "2026-05-05_report.html",
        ],
    )
    old_argv = sys.argv
    sys.argv = ["sweep_output_history.py", "--repo-root", str(tmp_path)]
    try:
        rc = mod.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    top_level = sorted(p.name for p in (research / "META").iterdir() if p.is_file())
    assert top_level == ["2026-05-05_report.html"]
    archive_files = sorted(p.name for p in (research / "META" / "archive").iterdir())
    assert archive_files == ["2026-05-04_report.html"]
