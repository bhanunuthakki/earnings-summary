"""Earnings/diagram/artifact scanners must read from both transcripts/raw/
and transcripts/processed/.

Before this fix, every reader looked only at `transcripts/processed/`, but
`fetch_qa_transcript.py` writes to `transcripts/raw/`. New auto-fetched
transcripts were silently invisible to the workspace renderer — discovered
2026-05-21 when portfolio workspaces showed 2-3 quarters despite 5-6 on disk.

Convention now matches `ingest_transcripts.py`: scan both dirs, processed
wins on collision (it's the promoted canonical location per index_manager.py).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest

import db
from compute import platform_diagram
from models.artifacts import ArtifactFlags, Quarter
from report.sections import earnings


class LatestTranscripts(Protocol):
    def __call__(self, repo_root: Path, ticker: str, n: int) -> list[tuple[Path, str]]: ...


scan_transcripts = cast(
    "Callable[[Path, str], dict[tuple[int, int], str]]",
    getattr(earnings, "_scan_transcripts"),
)
scan_processed_dir = cast(
    "Callable[[str, dict[tuple[int, Quarter], ArtifactFlags]], None]",
    getattr(db, "_scan_processed_dir"),
)
latest_transcripts = cast("LatestTranscripts", getattr(platform_diagram, "_latest_transcripts"))


# ---------------------------------------------------------------------------
# earnings section: _scan_transcripts
# ---------------------------------------------------------------------------


def _make(path: Path, content: str = "stub transcript text") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_transcripts_picks_up_raw_dir_only(tmp_path: Path) -> None:
    """A file only in raw/ must surface — this is the regression scenario."""
    _make(tmp_path / "transcripts" / "raw" / "AMZN_Q1_2025.txt")

    result = scan_transcripts(tmp_path / "transcripts", "AMZN")

    assert (1, 2025) in result
    assert result[(1, 2025)].endswith("AMZN_Q1_2025.txt")


def test_scan_transcripts_picks_up_processed_dir_only(tmp_path: Path) -> None:
    _make(tmp_path / "transcripts" / "processed" / "NU_Q4_2025.txt")

    result = scan_transcripts(tmp_path / "transcripts", "NU")

    assert (4, 2025) in result
    assert result[(4, 2025)].endswith("NU_Q4_2025.txt")


def test_scan_transcripts_unions_both_dirs(tmp_path: Path) -> None:
    """The portfolio workspace bug: raw and processed each held different
    quarters; the union is what should land in the workspace."""
    # processed has the older quarter, raw has the newer (typical after a
    # backfill_transcripts.py run lands new files in raw/).
    _make(tmp_path / "transcripts" / "processed" / "META_Q4_2024.txt")
    _make(tmp_path / "transcripts" / "raw" / "META_Q1_2025.txt")
    _make(tmp_path / "transcripts" / "raw" / "META_Q2_2025.txt")

    result = scan_transcripts(tmp_path / "transcripts", "META")

    assert set(result.keys()) == {(4, 2024), (1, 2025), (2, 2025)}


def test_scan_transcripts_processed_wins_on_collision(tmp_path: Path) -> None:
    """If both dirs have the same (Q, Y), processed/ is the canonical promoted
    version and must win — matches the promotion semantics documented in
    src/index_manager.py."""
    _make(
        tmp_path / "transcripts" / "processed" / "GOOG_Q1_2026.txt",
        content="processed-canonical",
    )
    _make(
        tmp_path / "transcripts" / "raw" / "GOOG_Q1_2026.txt",
        content="raw-staging",
    )

    result = scan_transcripts(tmp_path / "transcripts", "GOOG")

    chosen = Path(result[(1, 2026)])
    assert chosen.parent.name == "processed", f"expected processed/ to win, got {chosen}"
    assert chosen.read_text(encoding="utf-8") == "processed-canonical"


def test_scan_transcripts_filters_by_ticker(tmp_path: Path) -> None:
    """A file for another ticker in the same dir must not leak through."""
    _make(tmp_path / "transcripts" / "raw" / "AMZN_Q1_2025.txt")
    _make(tmp_path / "transcripts" / "raw" / "META_Q1_2025.txt")

    result = scan_transcripts(tmp_path / "transcripts", "AMZN")

    assert set(result.keys()) == {(1, 2025)}
    assert "AMZN" in result[(1, 2025)]


def test_scan_transcripts_handles_missing_dirs(tmp_path: Path) -> None:
    """Neither dir exists — return empty, don't raise."""
    # tmp_path / "transcripts" does not exist
    result = scan_transcripts(tmp_path / "transcripts", "AMZN")

    assert result == {}


# ---------------------------------------------------------------------------
# db: _scan_processed_dir (drives has_transcript_file flag)
# ---------------------------------------------------------------------------


def test_db_scan_processed_dir_unions_both_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quarterly_artifacts has_transcript_file flag must light up for
    files in either dir; the workspace 'no transcript' stub renders off it."""
    _make(tmp_path / "transcripts" / "processed" / "NU_Q3_2025.txt")
    _make(tmp_path / "transcripts" / "raw" / "NU_Q4_2025.txt")
    _make(tmp_path / "transcripts" / "raw" / "NU_Q1_2026.txt")

    monkeypatch.setattr(db, "STATE_ROOT", str(tmp_path))

    artifacts: dict[tuple[int, Quarter], ArtifactFlags] = {}
    scan_processed_dir("NU", artifacts)

    keys_with_transcripts = {
        (year, q) for (year, q), f in artifacts.items() if f.has_transcript_file
    }
    assert keys_with_transcripts == {
        (2025, Quarter.Q3),
        (2025, Quarter.Q4),
        (2026, Quarter.Q1),
    }


def test_db_scan_processed_dir_no_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "STATE_ROOT", str(tmp_path))

    artifacts: dict[tuple[int, Quarter], ArtifactFlags] = {}
    scan_processed_dir("NU", artifacts)

    assert artifacts == {}


# ---------------------------------------------------------------------------
# platform_diagram: _latest_transcripts
# ---------------------------------------------------------------------------


def test_latest_transcripts_unions_and_sorts_desc(tmp_path: Path) -> None:
    """Pulls from both dirs, returns up to n in (year, quarter) desc order."""
    _make(tmp_path / "transcripts" / "processed" / "GOOG_Q4_2024.txt", "p-q4")
    _make(tmp_path / "transcripts" / "raw" / "GOOG_Q1_2025.txt", "r-q1")
    _make(tmp_path / "transcripts" / "raw" / "GOOG_Q2_2025.txt", "r-q2")

    out = latest_transcripts(tmp_path, "GOOG", n=2)

    assert len(out) == 2
    assert out[0][1] == "r-q2"  # most recent
    assert out[1][1] == "r-q1"


def test_latest_transcripts_processed_wins_collision(tmp_path: Path) -> None:
    _make(tmp_path / "transcripts" / "processed" / "GOOG_Q1_2026.txt", "canonical")
    _make(tmp_path / "transcripts" / "raw" / "GOOG_Q1_2026.txt", "staging")

    out = latest_transcripts(tmp_path, "GOOG", n=1)

    assert len(out) == 1
    assert out[0][1] == "canonical"
    assert out[0][0].parent.name == "processed"
