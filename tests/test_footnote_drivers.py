"""Tests for pure SEC note-targeted footnote driver extraction (Item 2B)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from execution.extract_footnotes import extract_for_ticker
from filings.footnote_drivers import (
    MAX_ROWS_PER_FILING,
    FootnoteDriverRow,
    extract_footnote_drivers_from_text,
    persist_footnote_drivers,
    slice_high_signal_notes,
    verify_source_excerpt,
)

_SAMPLE_ITEM_8 = """
Item 8. Financial Statements and Supplementary Data

Note 1 — Summary of Significant Accounting Policies
We prepare our consolidated financial statements in accordance with US GAAP.
Revenue is recognized when control of goods transfers to customers.

Note 5 — Property and Equipment
Construction in progress totaled $90.0 billion as of September 30, 2026 and is
not depreciated until placed in service. In January 2025 we extended server
useful lives from 4 years to 5.5 years.

Note 9 — Stock-Based Compensation
As of December 31, 2025, total unrecognized compensation cost related to
nonvested RSUs was $28.5 billion, expected to be recognized over a weighted-average
period of 2.4 years.

Note 12 — Commitments and Contingencies
Non-cancelable purchase obligations for compute infrastructure and network
capacity totaled $121.0 billion due in 2027, including data center joint venture
lease commitments starting in 2028.

Note 14 — Income Taxes
The effective tax rate was 14.2% primarily due to foreign rate differentials.
"""


def test_note_slicer_selects_high_signal_notes_and_skips_boilerplate() -> None:
    res = slice_high_signal_notes(_SAMPLE_ITEM_8, max_chars=750)
    assert res.notes_found == 5
    assert not res.fallback_used
    joined = " | ".join(res.notes_selected)
    assert "Note 12" in joined
    assert "Note 5" in joined
    assert "Note 14" not in joined


def test_note_slicer_falls_back_cleanly_when_no_headers_present() -> None:
    res = slice_high_signal_notes("Plain narrative without note headings " * 20, max_chars=120)
    assert res.fallback_used
    assert res.notes_found == 0
    assert len(res.sliced_text) == 120
    assert res.truncated


def test_quote_verification_admits_ws_normalized_and_rejects_fabricated() -> None:
    src = "Construction in progress totaled\n  $90.0 billion as of September 30, 2026."
    assert verify_source_excerpt("Construction in progress totaled $90.0 billion", src)
    assert not verify_source_excerpt("Construction in progress totaled $99.9 billion", src)


def test_extract_filters_ungrounded_quotes_and_enforces_row_cap() -> None:
    valid_excerpt = "Construction in progress totaled $90.0 billion as of September 30, 2026"
    many_rows: list[dict[str, object]] = [
        {
            "fact_type": "cip_depreciation_policy",
            "footnote_number": "Note 5",
            "description_md": f"Row {i} CIP timing.",
            "amount": 90000,
            "currency": "USD",
            "unit": "millions",
            "status": "active",
            "source_excerpt": valid_excerpt,
        }
        for i in range(MAX_ROWS_PER_FILING + 4)
    ]
    many_rows.append(
        {
            "fact_type": "purchase_commitment",
            "footnote_number": "Note 12",
            "description_md": "Hallucinated quote row.",
            "amount": 500,
            "currency": "USD",
            "unit": "millions",
            "status": "active",
            "source_excerpt": "Completely fabricated sentence absent from the filing.",
        }
    )

    def fake_caller(prompt: str, **_kw: object) -> list[dict[str, object]]:
        assert "Note 12" in prompt
        return many_rows

    result = extract_footnote_drivers_from_text(
        ticker="META",
        fiscal_year=2025,
        item_8_text=_SAMPLE_ITEM_8,
        llm_caller=fake_caller,
    )
    assert result.status == "ok"
    assert len(result.accepted_rows) == MAX_ROWS_PER_FILING
    # The fabricated row sits after the cap or gets rejected before cap depending on order;
    # run a second small check with fabricated row first to assert rejected_count == 1.
    small = extract_footnote_drivers_from_text(
        ticker="META",
        fiscal_year=2025,
        item_8_text=_SAMPLE_ITEM_8,
        llm_caller=lambda *_a, **_k: [many_rows[-1], many_rows[0]],
    )
    assert len(small.accepted_rows) == 1
    assert small.rejected_count == 1


def test_persist_footnote_drivers_is_idempotent_and_binds_doc_id(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "footnote_drivers.db")
    conn = sqlite3.connect(db_path)
    try:
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        cur = conn.execute(
            """
            INSERT INTO documents (
                ticker, source_type, doc_type, file_path, sha256,
                fetched_at, fetch_status, raw_bytes_size, source_quality_tier
            ) VALUES ('META', 'sec_xbrl', '10-K', 'data/sec_text/META_10k_2025.txt',
                      ?, ?, 'ok', 100, 'sec_official')
            """,
            ("0" * 64, now),
        )
        doc_id = int(cur.lastrowid or 0)
        rows = [
            FootnoteDriverRow(
                fact_type="purchase_commitment",
                footnote_number="Note 12",
                description_md="Non-cancelable compute purchase commitments due in 2027.",
                amount=121000.0,
                currency="USD",
                unit="millions",
                due_period="2027-12-31",
                status="active",
                source_excerpt="Non-cancelable purchase obligations for compute infrastructure",
            )
        ]
        first = persist_footnote_drivers(
            conn, ticker="META", fiscal_year=2025, rows=rows, filing_doc_id=doc_id
        )
        second = persist_footnote_drivers(
            conn, ticker="META", fiscal_year=2025, rows=rows, filing_doc_id=doc_id
        )
        assert first == 1
        assert second == 0
        stored = conn.execute(
            "SELECT ticker, filing_doc_id, fact_type, amount FROM footnote_facts WHERE ticker='META'"
        ).fetchall()
        assert stored == [("META", doc_id, "purchase_commitment", 121000.0)]
    finally:
        conn.close()


def test_cli_extract_for_ticker_does_not_fetch_without_flag(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "cli_no_fetch.db")
    outcome = extract_for_ticker(
        ticker="META",
        repo_root=tmp_path,
        db_path=db_path,
        user_agent="test/0.1",
        allow_fetch=False,
    )
    assert outcome["status"] == "missing_retained_text"
    assert outcome["n"] == 0
