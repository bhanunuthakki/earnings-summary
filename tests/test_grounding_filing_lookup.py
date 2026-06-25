"""Guards for the filing-path resolver in ``src/ask/grounding.py``.

The live ask path used to glob ``data/historical/fmp`` — ~110k files — once per
ticker per question (the documented "never glob fmp" trap). These tests prove
the resolver now goes through the indexed ``documents`` table, never globs, and
still degrades correctly without a DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ask.grounding import _latest_filing_paths

_DOCS_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL
);
"""


def _conn_with_docs(tmp_path: Path, docs: list[tuple[str, str, str]]) -> sqlite3.Connection:
    """docs: list of (ticker, doc_type, repo_relative_file_path)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_DOCS_DDL)
    conn.executemany(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256)"
        " VALUES (?, 'fmp', ?, ?, 'x')",
        [(t, dt, fp) for (t, dt, fp) in docs],
    )
    conn.commit()
    return conn


def _touch_filing(repo_root: Path, name: str) -> Path:
    p = repo_root / "data" / "historical" / "fmp" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[]", encoding="utf-8")
    return p


def test_resolves_newest_registered_filing_via_documents(tmp_path: Path) -> None:
    for year in (2023, 2024, 2025):
        _touch_filing(tmp_path, f"TST_form_10k_{year}.json")
    conn = _conn_with_docs(
        tmp_path,
        [
            ("TST", "fmp_10k_json", f"data/historical/fmp/TST_form_10k_{y}.json")
            for y in (2023, 2024, 2025)
        ],
    )
    result = _latest_filing_paths(tmp_path, "TST", conn)
    assert result == [
        ("10k", 2025, tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2025.json")
    ]


def test_never_globs_the_fmp_directory(tmp_path: Path, monkeypatch) -> None:
    """Any directory glob is a hard failure — the resolver must use the index."""

    def _boom(_self: Path, _pattern: str):
        raise AssertionError("globbed data/historical/fmp on the hot path")

    monkeypatch.setattr(Path, "glob", _boom)
    _touch_filing(tmp_path, "TST_form_10k_2025.json")
    conn = _conn_with_docs(
        tmp_path, [("TST", "fmp_10k_json", "data/historical/fmp/TST_form_10k_2025.json")]
    )
    # DB path: resolves without globbing.
    assert _latest_filing_paths(tmp_path, "TST", conn) == [
        ("10k", 2025, tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2025.json")
    ]
    # No-DB fallback: bounded exact-name probe, still never globs.
    assert _latest_filing_paths(tmp_path, "TST", None) == [
        ("10k", 2025, tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2025.json")
    ]


def test_skips_registered_file_missing_on_disk(tmp_path: Path) -> None:
    """A registered-but-deleted newest file is skipped for the newest that
    actually exists — matching the old glob, which only saw real files."""
    _touch_filing(tmp_path, "TST_form_10k_2024.json")  # 2025 registered but NOT written
    conn = _conn_with_docs(
        tmp_path,
        [
            ("TST", "fmp_10k_json", "data/historical/fmp/TST_form_10k_2025.json"),
            ("TST", "fmp_10k_json", "data/historical/fmp/TST_form_10k_2024.json"),
        ],
    )
    assert _latest_filing_paths(tmp_path, "TST", conn) == [
        ("10k", 2024, tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2024.json")
    ]


def test_falls_back_to_disk_probe_without_db(tmp_path: Path) -> None:
    _touch_filing(tmp_path, "TST_form_10k_2022.json")
    assert _latest_filing_paths(tmp_path, "TST", None) == [
        ("10k", 2022, tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2022.json")
    ]


def test_empty_when_nothing_on_disk_or_registered(tmp_path: Path) -> None:
    assert _latest_filing_paths(tmp_path, "TST", None) == []
    conn = _conn_with_docs(tmp_path, [])
    assert _latest_filing_paths(tmp_path, "TST", conn) == []
