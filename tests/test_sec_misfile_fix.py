# pyright: reportPrivateUsage=false
"""Tests for the SEC-filing-misfiled-as-IR-doc fix.

A 10-K dropped into the inbox was classified by the LLM intake path (whose enum
has no SEC forms) as `ir_supplement` and cemented by the reindexer. These cover:
  - `ir_uploads.detect_sec_form` — the deterministic SEC-form veto
  - `parse_canonical_path` now round-trips `sec_*` canonical filenames
  - `categorize_ir_uploads._move_or_error` is a no-op when src == dest (no
    self-unlink data loss when a `sec_*`-named file reindexes)
  - `retype_misfiled_sec_ir_docs.plan_retypes` / `apply_retype` correct the rows
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import ir_uploads  # noqa: E402
from models.documents import DocType  # noqa: E402

_TENK_COVER = (
    "Table of Contents\nUNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\n"
    "Washington, D.C. 20549\nFORM 10-K\n(Mark One)\nANNUAL REPORT ...\n"
)
_TENQ_COVER = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nWashington, D.C. 20549\n"
    "FORM 10-Q\nQUARTERLY REPORT ...\nFor the quarterly period ended June 30, 2025\n"
)
_PRESS_RELEASE = (
    "MONTEVIDEO, Uruguay, May 14, 2025 — dLocal Limited today reported financial "
    "results for the first quarter 2025.\n"
)


# ---------------------------------------------------------------------------
# detect_sec_form
# ---------------------------------------------------------------------------


def test_detect_sec_form_identifies_10k_and_10q() -> None:
    assert ir_uploads.detect_sec_form(_TENK_COVER) is DocType.SEC_10K
    assert ir_uploads.detect_sec_form(_TENQ_COVER) is DocType.SEC_10Q


def test_detect_sec_form_ignores_ir_documents() -> None:
    assert ir_uploads.detect_sec_form(_PRESS_RELEASE) is None
    assert ir_uploads.detect_sec_form("Q1'25 Investor Presentation\nGMV grew 30%") is None


def test_detect_sec_form_anchored_to_cover_not_disclaimer() -> None:
    # A deck that merely *mentions* its Form 10-K filing far into the body must
    # NOT be treated as a SEC filing (the rule anchors to the first ~500 chars).
    text = "Investor Presentation\n" + ("blah " * 200) + "see our Annual Report on Form 10-K"
    assert ir_uploads.detect_sec_form(text) is None


# ---------------------------------------------------------------------------
# parse_canonical_path now recognizes sec_* filenames
# ---------------------------------------------------------------------------


def test_parse_canonical_path_recognizes_sec_10k(tmp_path: Path) -> None:
    root = tmp_path
    p = root / "RBRK" / "2026-01-31" / "sec_10k__deadbeef.pdf"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"%PDF-1.4\n")
    res = ir_uploads.parse_canonical_path(p, root)
    assert res is not None
    assert res.doc_type is DocType.SEC_10K
    assert res.ticker == "RBRK"
    assert res.period_end.isoformat() == "2026-01-31"


def test_parse_canonical_path_still_parses_ir_types(tmp_path: Path) -> None:
    root = tmp_path
    p = root / "NU" / "2025-03-31" / "ir_press_release__abcd1234.pdf"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"%PDF-1.4\n")
    res = ir_uploads.parse_canonical_path(p, root)
    assert res is not None
    assert res.doc_type is DocType.IR_PRESS_RELEASE


# ---------------------------------------------------------------------------
# _move_or_error src == dest safety
# ---------------------------------------------------------------------------


def test_move_or_error_noop_when_src_equals_dest(tmp_path: Path) -> None:
    import categorize_ir_uploads as cat

    f = tmp_path / "sec_10k__deadbeef.pdf"
    f.write_bytes(b"%PDF-1.4 payload")
    cat._move_or_error(f, f, dry_run=False)
    # The file must survive — the same-sha dedup branch would otherwise unlink it.
    assert f.exists()
    assert f.read_bytes() == b"%PDF-1.4 payload"


# ---------------------------------------------------------------------------
# retype_misfiled_sec_ir_docs
# ---------------------------------------------------------------------------


def _make_docs_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source_type TEXT,
            doc_type TEXT, period_end TIMESTAMP, file_path TEXT, sha256 TEXT UNIQUE,
            fetched_at TIMESTAMP, fetch_status TEXT, raw_bytes_size INTEGER, source_url TEXT
        );
        CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, source_doc_id INTEGER);
        """
    )
    conn.commit()
    conn.close()


def test_retype_plan_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import retype_misfiled_sec_ir_docs as rt

    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _make_docs_db(db)

    # A real on-disk file at the mis-typed canonical path.
    rel = "ir_documents/RBRK/2026-01-31/ir_supplement__cbe9c445.pdf"
    pdf = tmp_path / rel
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 10-K bytes")
    # A genuine IR supplement (xlsx-shaped name as pdf) that must NOT be retyped.
    rel_ok = "ir_documents/NU/2025-03-31/ir_press_release__aaaa1111.pdf"
    ok_pdf = tmp_path / rel_ok
    ok_pdf.parent.mkdir(parents=True)
    ok_pdf.write_bytes(b"%PDF-1.4 press release")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, sha256) "
        "VALUES ('RBRK','ir_doc','ir_supplement','2026-01-31 00:00:00', ?, 'sha_rbrk')",
        (rel,),
    )
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, sha256) "
        "VALUES ('NU','ir_doc','ir_press_release','2025-03-31 00:00:00', ?, 'sha_nu')",
        (rel_ok,),
    )
    conn.commit()

    # Stub the SEC sniff: the RBRK file is a 10-K, the NU one is not.
    def fake_detect(text: str) -> DocType | None:
        return DocType.SEC_10K if "10-K" in text else None

    def fake_fingerprint(p: object) -> str:
        return "FORM 10-K" if "cbe9c445" in str(p) else "earnings release"

    monkeypatch.setattr(rt, "detect_sec_form", fake_detect)
    monkeypatch.setattr(rt, "fingerprint", fake_fingerprint)

    plan = rt.plan_retypes(conn, tmp_path)
    assert len(plan) == 1
    only = plan[0]
    assert only.ticker == "RBRK"
    assert only.old_doc_type == "ir_supplement"
    assert only.new_doc_type == "sec_10k"
    assert only.new_file_path.endswith("/sec_10k__cbe9c445.pdf")
    assert only.kpi_facts == 0

    rt.apply_retype(conn, tmp_path, only)
    conn.commit()

    # DB row retyped + file renamed; source_type preserved.
    row = conn.execute(
        "SELECT doc_type, source_type, file_path FROM documents WHERE id=?", (only.doc_id,)
    ).fetchone()
    assert row["doc_type"] == "sec_10k"
    assert row["source_type"] == "ir_doc"
    assert row["file_path"].endswith("/sec_10k__cbe9c445.pdf")
    assert (tmp_path / row["file_path"]).exists()
    assert not pdf.exists()  # old name gone

    # Idempotent: re-planning finds nothing (the row is now sec_10k, out of scope).
    assert rt.plan_retypes(conn, tmp_path) == []
    conn.close()
