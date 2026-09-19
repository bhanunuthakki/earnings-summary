"""Native SEC text capture retains versions without platform newline rewriting."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from pipeline import sec_6k_fetch
from pipeline.sec_6k_fetch import FetchedExhibit, LocatedExhibit, register_6k_document
from pipeline.sec_fpi_ingest import (
    FetchedFpiExhibit,
    LocatedFpiExhibit,
    register_and_anchor_fpi_document,
)


@pytest.mark.parametrize("lane", ["6k", "fpi"])
def test_same_filing_new_content_preserves_each_sec_version(
    tmp_path: Path, migrated_db: Callable[..., Path], lane: str
) -> None:
    root = tmp_path / "repo"
    bodies = ("<html>first\r\nline\n</html>", "<html>revised\n</html>")
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        ids: list[int] = []
        for body in (*bodies, bodies[-1]):
            if lane == "6k":
                located = LocatedExhibit(
                    "NU",
                    "0001691493",
                    "0001292814-26-003053",
                    "2026-05-14",
                    "exhibit.html",
                    "https://www.sec.gov/exhibit.html",
                )
                document_id = register_6k_document(
                    conn,
                    ticker="NU",
                    fetched=FetchedExhibit(located, body, "text", False),
                    repo_root=root,
                    period_end=datetime(2026, 3, 31),
                )
            else:
                located_fpi = LocatedFpiExhibit(
                    "NU",
                    "0001691493",
                    "6-K",
                    "0001292814-26-003053",
                    "2026-05-14",
                    "exhibit.html",
                    "https://www.sec.gov/exhibit.html",
                )
                document_id = register_and_anchor_fpi_document(
                    conn,
                    ticker="NU",
                    fetched=FetchedFpiExhibit(
                        located_fpi, body, "text", False, hashlib.sha256(body.encode()).hexdigest()
                    ),
                    repo_root=root,
                    period_end=datetime(2026, 3, 31),
                )
            ids.append(document_id)
            assert conn.in_transaction
            conn.commit()
        assert ids[0] != ids[1] == ids[2]
        rows = conn.execute(
            "SELECT id,file_path,sha256,raw_bytes_size FROM documents ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["file_path"] != rows[1]["file_path"]
        for row, body in zip(rows, bodies, strict=True):
            retained = root / row["file_path"]
            assert retained.read_bytes() == body.encode()
            assert row["sha256"] == hashlib.sha256(retained.read_bytes()).hexdigest()
            assert row["raw_bytes_size"] == len(body.encode())
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 2


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_sec_evidence_failure_rolls_back_registration_without_losing_caller_work(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    outer_transaction: bool,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected evidence failure")

    monkeypatch.setattr(sec_6k_fetch, "ensure_legacy_document_evidence", fail)
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.execute("CREATE TABLE caller_work (value TEXT)")
        if outer_transaction:
            conn.execute("INSERT INTO caller_work VALUES ('retain')")
        with pytest.raises(RuntimeError, match="injected evidence failure"):
            sec_6k_fetch.register_sec_exhibit_snapshot(
                conn,
                ticker="NU",
                raw_html="<html>first\r\nline\n</html>",
                repo_root=tmp_path / "repo",
                period_end=datetime(2026, 3, 31),
                doc_type="sec_6k",
                source_url="https://www.sec.gov/exhibit.html",
                accession="0001292814-26-003053",
                filing_date="2026-05-14",
            )
        assert conn.in_transaction is outer_transaction
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0] == int(
            outer_transaction
        )
