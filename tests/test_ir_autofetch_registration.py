"""Tests for the auto-fetch registration chain (PR4).

fetch_ir_documents.py stages downloads under the ticker folder + records the
source URL; categorize_ir_uploads.py then content-classifies, moves to the
canonical path, and inserts the documents row stamped with that real URL — and,
with --calendar, registers a ticker not in ISSUER_REGISTRY.

Injection is via monkeypatch string-paths / a fake urlopen (no private-symbol
access); the categorizer is exercised as a real subprocess (matching how
fetch_ir_documents invokes it), which also sidesteps its import-time root.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import fetch_ir_documents as fid  # noqa: E402

_CATEGORIZER = PROJECT_ROOT / "execution" / "categorize_ir_uploads.py"


def _write_manifest(root: Path, ticker: str, entries: list[dict[str, object]]) -> None:
    d = root / ".tmp" / "ir_url_manifest"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_urls.json").write_text(json.dumps(entries), encoding="utf-8")


def _make_documents_db(db: Path, ticker: str = "NU") -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE documents ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source_type TEXT,"
        " doc_type TEXT, period_end TEXT, file_path TEXT, sha256 TEXT UNIQUE,"
        " fetched_at TEXT, fetch_status TEXT, raw_bytes_size INTEGER, source_url TEXT)"
    )
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)")
    conn.execute(
        "INSERT INTO tracked_companies VALUES (?, 'portfolio', NULL)",
        (ticker,),
    )
    conn.commit()
    conn.close()


class _FakeResp:
    def __init__(self, headers: dict[str, str], body: bytes) -> None:
        self._headers = headers
        self._body = body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    def read(self) -> bytes:
        return self._body


def _safe_url(url: str) -> str:
    return url


# ---------------------------------------------------------------------------
# fetch_ir_documents.py
# ---------------------------------------------------------------------------


def test_no_manifest_status(tmp_path: Path) -> None:
    db = tmp_path / "db"
    _make_documents_db(db, "ZZ")
    summary = fid.process_ticker("ZZ", root=tmp_path, db_path=db)
    assert summary["status"] == "no_manifest"


def test_real_download_picks_xlsx_extension_from_content_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "db"
    _make_documents_db(db, "NU")
    _write_manifest(
        tmp_path,
        "NU",
        [{"url": "https://x/opaque", "doc_type": "supplement", "year": 2026, "quarter": "Q1"}],
    )

    def _nosleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("execution.fetch_ir_documents.time.sleep", _nosleep)
    monkeypatch.setattr(
        "execution.fetch_ir_documents.ensure_safe_public_url",
        _safe_url,
    )

    class _FakeOpener:
        def open(self, req: object, timeout: int = 0) -> _FakeResp:
            return _FakeResp(
                {"Content-Disposition": 'attachment; filename="Nu 1Q26.xlsx"'},
                b"PKfake",
            )

    def _fake_opener(*_handlers: object) -> _FakeOpener:
        return _FakeOpener()

    monkeypatch.setattr("execution.fetch_ir_documents.urllib.request.build_opener", _fake_opener)
    fid.process_ticker("NU", root=tmp_path, db_path=db)

    staged = list((tmp_path / "ir_documents" / "NU").glob("*"))
    assert len(staged) == 1
    assert staged[0].suffix == ".xlsx"
    assert staged[0].name.startswith("NU_supplement_2026Q1__")
    # Sidecar maps the staged filename → the real URL.
    sidecar = json.loads((tmp_path / ".tmp" / "ir_incoming_urls.json").read_text(encoding="utf-8"))
    assert sidecar[staged[0].name] == "https://x/opaque"


def test_skips_already_registered_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_documents_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO documents (ticker, source_url) VALUES ('NU', 'https://x/a.pdf')")
    conn.commit()
    conn.close()
    _write_manifest(
        tmp_path,
        "NU",
        [{"url": "https://x/a.pdf", "doc_type": "press_release", "year": 2026, "quarter": "Q1"}],
    )

    calls: list[str] = []

    def _record(url: str, _dest_dir: Path, _base: str) -> None:
        calls.append(url)

    monkeypatch.setattr("execution.fetch_ir_documents._download", _record)
    summary = fid.process_ticker("NU", root=tmp_path, db_path=db)
    assert summary["downloaded"] == 0
    assert summary["skipped"] == 1
    assert calls == []  # never attempted


# ---------------------------------------------------------------------------
# categorize_ir_uploads.py (real subprocess)
# ---------------------------------------------------------------------------


def test_categorize_registers_unregistered_ticker_with_source_url(tmp_path: Path) -> None:
    root = tmp_path
    db = root / "data" / "portfolio.db"
    _make_documents_db(db)

    # ZZZ is NOT in ISSUER_REGISTRY — only --calendar makes it registrable.
    staged_dir = root / "ir_documents" / "ZZZ"
    staged_dir.mkdir(parents=True)
    staged = staged_dir / "ZZZ_supplement_2025Q3__deadbeef.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.cell(1, 1, "Some Company Q3 2025 Financial Supplement")
    wb.save(str(staged))

    (root / ".tmp").mkdir(parents=True, exist_ok=True)
    (root / ".tmp" / "ir_incoming_urls.json").write_text(
        json.dumps({staged.name: "https://ir.zzz.com/q3-supp.xlsx"}), encoding="utf-8"
    )

    env = {"IR_PROJECT_ROOT": str(root)}
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py"),
            str(_CATEGORIZER),
            "--ticker",
            "ZZZ",
            "--source-dir",
            str(root / "ir_documents"),
            "--db-path",
            str(db),
            "--rel-root",
            str(root),
            "--calendar",
            "calendar",
        ],
        capture_output=True,
        text=True,
        env={**_os_environ(), **env},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT doc_type, source_url, file_path, period_end FROM documents WHERE ticker='ZZZ'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "ir_supplement"
    assert row[1] == "https://ir.zzz.com/q3-supp.xlsx"  # source_url from the sidecar
    assert "ir_supplement__" in row[2]  # canonical filename
    assert row[3].startswith("2025-09-30")  # calendar Q3 → Sep-30


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)
