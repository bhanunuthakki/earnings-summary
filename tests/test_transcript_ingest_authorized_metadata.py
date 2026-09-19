from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from compute import transcript_ingest
from models.documents import SourceType

InsertDocument = Callable[..., int]


def test_insert_document_preserves_authorized_source_metadata(tmp_path: Path) -> None:
    database = tmp_path / "metadata.db"
    source = tmp_path / "transcripts" / "processed" / "ACME_Q2_2026.txt"
    source.parent.mkdir(parents=True)
    payload = b"authorized issuer transcript"
    source.write_bytes(payload)
    source_url = "https://issuer.example.invalid/q2-2026-transcript"

    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE documents ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,ticker TEXT,source_type TEXT,doc_type TEXT,"
            "period_start TEXT,period_end TEXT,file_path TEXT,sha256 TEXT,fetched_at TEXT,"
            "fetch_status TEXT,http_code INTEGER,raw_bytes_size INTEGER,source_url TEXT,"
            "parent_document_id INTEGER)"
        )
        insert_document = cast(
            InsertDocument,
            getattr(transcript_ingest, "_insert_document"),
        )
        document_id = insert_document(
            conn,
            ticker="ACME",
            file_path=source,
            sha256=hashlib.sha256(payload).hexdigest(),
            period_end=datetime(2026, 6, 30),
            project_root=tmp_path,
            source_type=SourceType.IR_DOC,
            source_url=source_url,
        )
        row = conn.execute(
            "SELECT source_type,source_url,raw_bytes_size FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()

    assert row == (SourceType.IR_DOC.value, source_url, len(payload))
