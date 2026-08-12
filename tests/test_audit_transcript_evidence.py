"""Read-only transcript evidence-integrity audit regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "audit_transcript_evidence.py"
    spec = importlib.util.spec_from_file_location("audit_transcript_evidence", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_transcript_evidence"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_auditor_classifies_integrity_states_without_repair(tmp_path: Path) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, ticker TEXT NOT NULL,
            fiscal_period_type TEXT, period_end TEXT
        );
        """
    )
    raw = tmp_path / "transcripts" / "raw"
    processed = tmp_path / "transcripts" / "processed"
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)

    ok = raw / "OK_Q1_2026.txt"
    ok.write_text("ok", encoding="utf-8")
    exact_alias = processed / "EXACT_Q1_2026.txt"
    exact_alias.write_text("same", encoding="utf-8")
    mismatch_alias = processed / "MISMATCH_Q1_2026.txt"
    mismatch_alias.write_text("different", encoding="utf-8")
    corrupt = raw / "CORRUPT_Q1_2026.txt"
    corrupt.write_text("changed", encoding="utf-8")

    rows = [
        (1, "OK", "transcripts/raw/OK_Q1_2026.txt", _sha(ok)),
        (2, "EXACT", "transcripts/raw/EXACT_Q1_2026.txt", _sha(exact_alias)),
        (
            3,
            "MISMATCH",
            "transcripts/raw/MISMATCH_Q1_2026.txt",
            hashlib.sha256(b"expected").hexdigest(),
        ),
        (4, "MISSING", "transcripts/raw/MISSING_Q1_2026.txt", hashlib.sha256(b"gone").hexdigest()),
        (
            5,
            "CORRUPT",
            "transcripts/raw/CORRUPT_Q1_2026.txt",
            hashlib.sha256(b"original").hexdigest(),
        ),
        (6, "UNSAFE", "../outside.txt", hashlib.sha256(b"outside").hexdigest()),
        (7, "ABSOLUTE", str(ok.resolve()), _sha(ok)),
        (8, "DOTDOT", "transcripts/raw/../raw/OK_Q1_2026.txt", _sha(ok)),
    ]
    conn.executemany("INSERT INTO documents VALUES (?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO transcripts VALUES (?, ?, ?, 'Q1', '2026-03-31')",
        [(item[0], item[0], item[1]) for item in rows],
    )
    conn.commit()
    conn.close()

    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.txt"))
    report = mod.audit_transcript_evidence(tmp_path, db_path)
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.txt"))

    statuses = {item.ticker: item.status.value for item in report.items}
    assert statuses == {
        "OK": "ok",
        "EXACT": "missing_exact_alias",
        "MISMATCH": "missing_alias_mismatch",
        "MISSING": "missing",
        "CORRUPT": "hash_mismatch",
        "UNSAFE": "unsafe_path",
        "ABSOLUTE": "unsafe_path",
        "DOTDOT": "unsafe_path",
    }
    assert before == after
    assert not (raw / "EXACT_Q1_2026.txt").exists()


def test_auditor_classifies_hash_open_errors_as_unreadable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    path = tmp_path / "transcripts" / "raw" / "NU_Q1_2026.txt"
    path.parent.mkdir(parents=True)
    path.write_text("bytes", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        INSERT INTO documents VALUES (1, 'NU', 'transcripts/raw/NU_Q1_2026.txt', 'abc');
        INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31');
        """
    )
    conn.commit()
    conn.close()

    def fail_hash(_path: Path, _root: Path | None = None) -> str:
        raise OSError("secret")

    monkeypatch.setattr(mod, "_sha256", fail_hash)
    report = mod.audit_transcript_evidence(tmp_path, db_path)
    assert report.items[0].status.value == "unreadable"
    assert "secret" not in report.model_dump_json()


def test_auditor_handle_swap_is_typed_unreadable(tmp_path: Path, monkeypatch: Any) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    raw = tmp_path / "transcripts" / "raw"
    raw.mkdir(parents=True)
    path = raw / "NU_Q1_2026.txt"
    path.write_text("bytes", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        INSERT INTO documents VALUES (1, 'NU', 'transcripts/raw/NU_Q1_2026.txt', 'abc');
        INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31');
        """
    )
    conn.commit()
    conn.close()

    def swapped_hash(_path: Path, _root: Path | None = None) -> str:
        raise OSError("handle swap")

    monkeypatch.setattr(mod, "_sha256", swapped_hash)

    report = mod.audit_transcript_evidence(tmp_path, db_path)
    assert report.items[0].status.value == "unreadable"
