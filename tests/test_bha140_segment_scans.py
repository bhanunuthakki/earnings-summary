"""BHA-140 regressions against the migrated receipt and transcript schema."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from compute.management_indicators import ManagementIndicatorExtractionManifest, persist_indicators
from compute.say_do_extractor import CommitmentParseError, extract_for_transcript


def _seed_transcript(path: Path, *, segments: list[str]) -> int:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO documents (ticker,source_type,doc_type,file_path,sha256,fetch_status,"
            "fetched_at,raw_bytes_size) VALUES ('ACME','ir_doc','earnings_call_transcript',?,?, 'ok', ?, ?)",
            ("transcripts/processed/ACME_Q2_2026.txt", "a" * 64, "2026-07-01", 1),
        )
        document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO transcripts (document_id,ticker,call_date,fiscal_period_type,period_end,"
            "source,is_active,is_current,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                "ACME",
                "2026-07-01",
                "Q2",
                "2026-06-30",
                "issuer_ir",
                1,
                1,
                "2026-07-01",
            ),
        )
        transcript_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.executemany(
            "INSERT INTO transcript_segments (transcript_id,seq,text) VALUES (?,?,?)",
            [(transcript_id, index, text) for index, text in enumerate(segments)],
        )
        conn.commit()
    return transcript_id


def test_extractor_calls_once_per_segment_in_source_order(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "segments.db")
    transcript_id = _seed_transcript(path, segments=["first segment", "second segment"])
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"commitments": [], "novel_indicators": []})

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        manifest = extract_for_transcript(conn, transcript_id, llm_call=llm)

    assert len(prompts) == 2
    assert "first segment" in prompts[0]
    assert "second segment" in prompts[1]
    assert manifest.commitments == []
    assert manifest.indicators == []
    assert [item.source.segment_id for item in manifest.observed_segments]
    assert [item.disposition for item in manifest.observed_segments] == [
        "parsed_no_output",
        "parsed_no_output",
    ]
    assert all(len(item.source.text_sha256) == 64 for item in manifest.observed_segments)


def test_oversized_segment_fails_before_any_llm_call(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "oversized.db")
    transcript_id = _seed_transcript(path, segments=["ok", "x" * 60_001])
    calls = 0

    def llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"commitments": [], "novel_indicators": []}'

    with (
        sqlite3.connect(path) as conn,
        pytest.raises(CommitmentParseError, match="complete-coverage limit"),
    ):
        conn.row_factory = sqlite3.Row
        extract_for_transcript(conn, transcript_id, llm_call=llm)

    assert calls == 0


def test_duplicate_excerpt_retains_each_exact_originating_segment(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "duplicate-excerpt.db")
    excerpt = "We launched 42 enterprise pilots."
    transcript_id = _seed_transcript(path, segments=[excerpt, excerpt])

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = extract_for_transcript(
            conn,
            transcript_id,
            llm_call=lambda _prompt: json.dumps(
                {
                    "commitments": [],
                    "novel_indicators": [
                        {
                            "raw_label": "Enterprise pilots",
                            "value": "42",
                            "unit": "count",
                            "scope": "product",
                            "recurrence": "one_off",
                            "source_excerpt": excerpt,
                        }
                    ],
                }
            ),
        )
        expected_ids = [item.source.segment_id for item in result.observed_segments]
        assert [item.transcript_segment_id for item in result.indicators] == expected_ids
        persisted = persist_indicators(
            conn, ManagementIndicatorExtractionManifest(indicators=result.indicators)
        )
        actual_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT transcript_segment_id FROM management_indicator_observations "
                "WHERE id IN (?,?) ORDER BY id",
                persisted,
            )
        ]
    assert actual_ids == expected_ids


def test_cross_segment_excerpts_cannot_swap_exact_anchors(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "swapped-excerpts.db")
    first = "We launched 41 enterprise pilots."
    second = "We launched 42 enterprise pilots."
    transcript_id = _seed_transcript(path, segments=[first, second])
    calls = 0

    def llm(prompt: str) -> str:
        nonlocal calls
        calls += 1
        wrong_excerpt = second if first in prompt else first
        return json.dumps(
            {
                "commitments": [],
                "novel_indicators": [
                    {
                        "raw_label": "Enterprise pilots",
                        "value": "42",
                        "unit": "count",
                        "scope": "product",
                        "recurrence": "one_off",
                        "source_excerpt": wrong_excerpt,
                    }
                ],
            }
        )

    with (
        sqlite3.connect(path) as conn,
        pytest.raises(CommitmentParseError, match="anchored transcript segment"),
    ):
        conn.row_factory = sqlite3.Row
        extract_for_transcript(conn, transcript_id, llm_call=llm)
    assert calls == 2


@pytest.mark.parametrize("response", ["{}", '{"commitments":[]}', '{"novel_indicators":[]}'])
def test_partial_object_is_not_explicit_zero_coverage(
    response: str, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "partial-json.db")
    transcript_id = _seed_transcript(path, segments=["text"])
    calls = 0

    def llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return response

    with (
        sqlite3.connect(path) as conn,
        pytest.raises(CommitmentParseError, match="schema validation"),
    ):
        conn.row_factory = sqlite3.Row
        extract_for_transcript(conn, transcript_id, llm_call=llm)
    assert calls == 2


@pytest.mark.parametrize(
    "response",
    [
        '{"commitments":[],"commitments":[],"novel_indicators":[]}',
        '{"commitments":[],"novel_indicators":[],"unexpected":true}',
        '{"commitments":[{"kpi_name":"X","comparator":"ge","target_value":true,'
        '"unit":"percent","period_target":"2026-09-30","narrative":"x"}],'
        '"novel_indicators":[]}',
    ],
)
def test_structured_response_rejects_duplicate_extra_and_boolean_tampering(
    response: str, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / f"strict-{hash(response)}.db")
    transcript_id = _seed_transcript(path, segments=["text"])
    with (
        sqlite3.connect(path) as conn,
        pytest.raises(CommitmentParseError),
    ):
        conn.row_factory = sqlite3.Row
        extract_for_transcript(conn, transcript_id, llm_call=lambda _prompt: response)
