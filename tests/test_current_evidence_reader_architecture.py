"""Architecture ratchet for current-versus-historical evidence selection."""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "execution")
_RAW_RELATION = re.compile(r"\b(?:FROM|JOIN)\s+(?:transcripts|filing_sections)\b", re.IGNORECASE)

# This exact inventory is migration debt, not an approved extension surface.
# Every remaining entry is intentionally history-aware: backfill/dedupe/repair
# tools and immutable-evidence auditors must see superseded rows; transcript ingestion and refetch mutate a
# specifically identified legacy row; section-item, longitudinal, and tone
# readers validate an explicit historical identifier; quarterly refresh and the
# transcript acquisition boundary avoid reparsing a document after a transcript
# already exists while constructing the sealed authorization preflight. New raw readers
# must not appear, and deleting an entry is always allowed.
AUDITED_RAW_RELATION_READS = {
    "execution/audit_transcript_evidence.py": 1,
    "execution/backfill_transcripts.py": 2,
    "execution/dedupe_transcripts.py": 1,
    "execution/ingest_filing_sections.py": 1,
    "execution/refetch_aggregator_transcripts.py": 1,
    "execution/repair_document_parent_links.py": 1,
    "execution/scan_ir_transcripts.py": 1,
    "src/compute/transcript_ingest.py": 2,
    "src/filings/section_items.py": 1,
    "src/pipeline/quarterly_refresh.py": 3,
    "src/pipeline/transcript_acquisition.py": 1,
    "src/transcripts/longitudinal.py": 1,
    "src/triggers/earnings_tone.py": 1,
}


def _raw_relation_reads(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        len(_RAW_RELATION.findall(node.value))
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"\bSELECT\b", node.value, re.IGNORECASE)
    )


def test_current_evidence_readers_use_the_selection_boundary() -> None:
    """New raw readers must opt into history or use provenance.selection."""
    actual = Counter(
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        for _ in range(_raw_relation_reads(path))
    )
    assert dict(actual) == AUDITED_RAW_RELATION_READS
