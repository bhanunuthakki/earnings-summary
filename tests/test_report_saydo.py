"""Say-Do report reads recorded management commitments and outcomes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import cast

import report.renderers.markdown as markdown_renderer
from report.models import SayDoHistoricalMetric, SayDoSection, SayDoVerdictRow, SectionStatus
from report.renderers.workspace_sections.saydo import _saydo_tab
from report.sections.saydo import build as build_saydo

render_saydo_markdown = cast(
    "Callable[[StringIO, SayDoSection], None]", getattr(markdown_renderer, "_saydo")
)


def test_section_reads_commitments_and_keeps_source_locator(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "synthetic.sqlite")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO management_commitments "
            "(ticker, period_made, transcript_segment_id, period_target, kpi_name, comparator, "
            "target_value, unit, narrative, realized_value, outcome, evaluated_at, created_at) "
            "VALUES ('TEST', '2025-10-30', 1, '2025-12-31', 'Cloud growth', 'ge', 30, 'percent', "
            "'Original management wording', 32, 'met', '2026-01-30', '2025-10-30')"
        )
        section = build_saydo("TEST", tmp_path, conn=conn)
        assert section.status == SectionStatus.OK
        assert section.verdicts_availability == "present"
        assert len(section.verdicts) == 1
        row = section.verdicts[0]
        assert row.narrative == "Original management wording"
        assert row.realized_value == 32
        assert row.source_ref == "transcript_segment:1"
        assert row.source_document_id is None
        assert row.source_url is None
        assert conn.in_transaction
        assert conn.row_factory is sqlite3.Row
        encoded = section.model_dump_json()
    restored = SayDoSection.model_validate_json(encoded)
    assert restored.verdicts[0].source_ref == "transcript_segment:1"


def test_renderer_shows_promise_actual_verdict_and_locator() -> None:
    section = SayDoSection(
        status=SectionStatus.OK,
        verdicts=[
            SayDoVerdictRow(
                period_made=datetime(2025, 10, 30),
                period_target=datetime(2025, 12, 31),
                kpi_name="Cloud growth",
                comparator="ge",
                target_value=30,
                unit="percent",
                narrative="Original management wording",
                realized_value=32,
                outcome="met",
                evaluated_at=datetime(2026, 1, 30),
                source_ref="transcript_segment:1",
            )
        ],
        verdicts_availability="present",
    )
    body = StringIO()
    _saydo_tab(body, section)
    html = body.getvalue()
    for expected in ("Cloud growth", "≥ 30 percent", "32 percent", "MET", "Source unavailable"):
        assert expected in html
    assert "transcript_segment:1" not in html


def test_markdown_renderer_links_exact_source_document() -> None:
    section = SayDoSection(
        status=SectionStatus.OK,
        verdicts=[
            SayDoVerdictRow(
                period_made=datetime(2025, 10, 30),
                period_target=datetime(2025, 12, 31),
                kpi_name="Cloud growth",
                comparator="ge",
                target_value=30,
                unit="percent",
                narrative="Original management wording",
                realized_value=32,
                outcome="met",
                evaluated_at=datetime(2026, 1, 30),
                source_ref="transcript_segment:1",
                source_document_id=731,
                source_url="https://example.test/transcript/1",
            )
        ],
    )
    body = StringIO()
    render_saydo_markdown(body, section)
    markdown = body.getvalue()
    assert "[Transcript](/source/731)" in markdown
    assert "transcript_segment:1" not in markdown


def test_markdown_historical_comparison_keeps_unknown_units_explicit() -> None:
    section = SayDoSection(
        status=SectionStatus.OK,
        historical_metrics=[
            SayDoHistoricalMetric(
                id=1,
                period_made=datetime(2026, 1, 1),
                period_target=datetime(2026, 4, 1),
                kpi_name="Revenue",
                comparator="ge",
                target_value=12,
                realized_value=13,
                outcome="met",
            )
        ],
    )
    body = StringIO()
    render_saydo_markdown(body, section)
    assert "12 → 13 (unit unavailable)" in body.getvalue()
    assert "%" not in body.getvalue()
