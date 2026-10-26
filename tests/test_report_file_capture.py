"""Unit tests for bounded report file capture primitive.

Tests pre-read size refusal, cumulative budget exhaustion, mid-enumeration
ceilings, forbidden selectors, winner semantics for S1-S16, replay verification,
tamper detection, and sealed isolation.
"""

from __future__ import annotations

import json
import os
import shutil
import types
from collections.abc import Iterable
from pathlib import Path

import pytest
from pydantic import ValidationError

from report.file_capture import (
    FileCaptureBudget,
    FileCaptureMismatchError,
    FileCaptureUnavailable,
    FileCaptureUnavailableError,
    ReportFileCaptureSession,
    replay_file_capture,
)


def _setup_base_tree(root: Path) -> tuple[Path, Path, Path, Path]:
    tr = root / "transcripts"
    tr_p = tr / "processed"
    tr_r = tr / "raw"
    tmp = root / ".tmp"
    data = root / "data"
    thesis = root / "micro_thesis" / "holdings"

    tr_p.mkdir(parents=True, exist_ok=True)
    tr_r.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    thesis.mkdir(parents=True, exist_ok=True)

    return tr, tmp, data, thesis


def test_pre_read_size_refusal_zero_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_base_tree(tmp_path)
    large_file = tmp_path / ".tmp" / "large.txt"
    large_file.write_bytes(b"x" * 500)

    budget = FileCaptureBudget(max_bytes_per_file=100)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    read_calls = 0
    orig_read = os.read

    def _spy_read(fd: int, n: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return orig_read(fd, n)

    monkeypatch.setattr(os, "read", _spy_read)

    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.read_bytes(".tmp/large.txt")

    assert exc_info.value.reason == "budget_exhausted"
    assert read_calls == 0
    assert session.total_bytes_captured == 0
    assert not session.has_cached_bytes(".tmp/large.txt")


def test_cumulative_byte_budget_exhaustion(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    f1 = tmp_path / ".tmp" / "f1.txt"
    f2 = tmp_path / ".tmp" / "f2.txt"
    f3 = tmp_path / ".tmp" / "f3.txt"
    f1.write_bytes(b"a" * 60)
    f2.write_bytes(b"b" * 60)
    f3.write_bytes(b"c" * 10)

    budget = FileCaptureBudget(max_bytes_per_file=100, max_total_bytes=100)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    b1 = session.read_bytes(".tmp/f1.txt")
    assert len(b1) == 60
    assert session.total_bytes_captured == 60

    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.read_bytes(".tmp/f2.txt")
    assert exc_info.value.reason == "budget_exhausted"

    # Session is now invalidated; subsequent reads fail closed
    with pytest.raises(FileCaptureUnavailableError) as exc_info2:
        session.read_bytes(".tmp/f3.txt")
    assert exc_info2.value.reason == "session_invalidated"


def test_mid_enumeration_exhaustion_directories(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    d1 = tmp_path / "data" / "qa_topics"
    d2 = tmp_path / "data" / "filing_intelligence"
    d1.mkdir(parents=True, exist_ok=True)
    d2.mkdir(parents=True, exist_ok=True)
    (d1 / "TEST.json").write_text("{}", encoding="utf-8")
    (d2 / "TEST.json").write_text("{}", encoding="utf-8")

    budget = FileCaptureBudget(max_visited_directories=1)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    # First directory scan succeeds
    session.record_directory_selection("S6", "TEST")
    # Second directory scan exceeds ceiling
    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.record_directory_selection("S7", "TEST")
    assert exc_info.value.reason == "budget_exhausted"
    assert session.total_directories_visited == 2


def test_mid_enumeration_exhaustion_entries(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    for i in range(10):
        (tmp_path / ".tmp" / f"TEST_Q{i % 4 + 1}_202{i // 4}_summary.txt").write_text(
            f"summary {i}", encoding="utf-8"
        )

    budget = FileCaptureBudget(max_examined_entries=5)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.record_directory_selection("S1", "TEST")
    assert exc_info.value.reason == "budget_exhausted"
    assert session.total_examined_entries == 6

    # Generator scandir_fn proves lazy consumption and no list() materialization
    yield_count = 0

    def _counting_scandir(p: str | Path) -> Iterable[os.DirEntry[str]]:
        nonlocal yield_count
        with os.scandir(p) as it:
            for entry in it:
                yield_count += 1
                yield entry

    session_gen = ReportFileCaptureSession(
        repo_root=tmp_path, budget=budget, scandir_fn=_counting_scandir
    )
    with pytest.raises(FileCaptureUnavailableError) as exc_info2:
        session_gen.record_directory_selection("S1", "TEST")
    assert exc_info2.value.reason == "budget_exhausted"
    assert session_gen.total_examined_entries == 6
    assert yield_count == 6  # Exactly ceiling + 1; proves list(...) was never called


def test_mid_enumeration_exhaustion_selected_members(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    for i in range(5):
        (tmp_path / ".tmp" / f"TEST_Q{i + 1}_2025_summary.txt").write_text(
            f"summary {i}", encoding="utf-8"
        )

    budget = FileCaptureBudget(max_selected_members=3)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.record_directory_selection("S1", "TEST")
    assert exc_info.value.reason == "budget_exhausted"
    assert session.total_selected_members == 4


def test_mid_enumeration_exhaustion_retained_metadata(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    (tmp_path / ".tmp" / "TEST_Q1_2025_summary.txt").write_text("s1", encoding="utf-8")
    (tmp_path / ".tmp" / "TEST_Q2_2025_summary.txt").write_text("s2", encoding="utf-8")

    budget = FileCaptureBudget(max_retained_metadata_bytes=100)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.record_directory_selection("S1", "TEST")
    assert exc_info.value.reason == "budget_exhausted"
    assert session.total_retained_metadata_bytes > 100


def test_forbidden_selector_and_path_inputs(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)

    s1 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailable) as exc1:
        s1.record_directory_selection("arbitrary_glob_v1", "TEST")
    assert exc1.value.reason == "forbidden_selector"
    with pytest.raises(FileCaptureUnavailable) as seal_exc1:
        s1.seal()
    assert seal_exc1.value.reason == "session_invalidated"

    s2 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailable) as exc2:
        s2.record_directory_selection("S1", "../../etc")
    assert exc2.value.reason == "forbidden_selector"
    with pytest.raises(FileCaptureUnavailable) as seal_exc2:
        s2.seal()
    assert seal_exc2.value.reason == "session_invalidated"

    s3 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailable) as exc3:
        s3.record_directory_selection("S1", "TEST/secret")
    assert exc3.value.reason == "forbidden_selector"
    with pytest.raises(FileCaptureUnavailable) as seal_exc3:
        s3.seal()
    assert seal_exc3.value.reason == "session_invalidated"

    s4 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailable) as exc4:
        s4.read_bytes("../escape.txt")
    assert exc4.value.reason == "forbidden_selector"
    with pytest.raises(FileCaptureUnavailable) as seal_exc4:
        s4.seal()
    assert seal_exc4.value.reason == "session_invalidated"

    s5 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailable) as exc5:
        s5.read_bytes("/absolute/path.txt")
    assert exc5.value.reason == "forbidden_selector"
    with pytest.raises(FileCaptureUnavailable) as seal_exc5:
        s5.seal()
    assert seal_exc5.value.reason == "session_invalidated"


def test_transcripts_dual_dir_winner_and_duplicate_keys(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    p_dir = tmp_path / "transcripts" / "processed"
    r_dir = tmp_path / "transcripts" / "raw"

    # In processed: Q1_2025, Q4_2025.txt, Q4_2025.pdf
    (p_dir / "MELI_Q1_2025.txt").write_text("meli q1 proc", encoding="utf-8")
    (p_dir / "MELI_Q4_2025.txt").write_text("meli q4 txt", encoding="utf-8")
    (p_dir / "MELI_Q4_2025.pdf").write_bytes(b"%PDF-meli-q4-pdf")

    # In raw: Q1_2025 (should lose to processed), Q2_2025 (should win because absent from processed)
    (r_dir / "MELI_Q1_2025.txt").write_text("meli q1 raw loser", encoding="utf-8")
    (r_dir / "MELI_Q2_2025.txt").write_text("meli q2 raw only", encoding="utf-8")

    def _order_forward(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name)

    def _order_reverse(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name, reverse=True)

    session1 = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_order_forward)
    comm1 = session1.record_directory_selection("transcripts_dual_dir_v1", "MELI")

    assert comm1.effective_winners["Q1_2025"] == "transcripts/processed/MELI_Q1_2025.txt"
    assert comm1.effective_winners["Q2_2025"] == "transcripts/raw/MELI_Q2_2025.txt"
    # In forward alphabetical order: MELI_Q4_2025.pdf comes before MELI_Q4_2025.txt
    assert comm1.effective_winners["Q4_2025"] == "transcripts/processed/MELI_Q4_2025.pdf"

    session2 = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_order_reverse)
    comm2 = session2.record_directory_selection("transcripts_dual_dir_v1", "MELI")
    # In reverse alphabetical order: MELI_Q4_2025.txt comes first
    assert comm2.effective_winners["Q4_2025"] == "transcripts/processed/MELI_Q4_2025.txt"

    # Replay verification fails when order reverses
    receipt1 = session1.seal()
    with pytest.raises(FileCaptureMismatchError):
        replay_file_capture(receipt1, tmp_path, scandir_fn=_order_reverse)


def test_quarterly_summaries_last_write_wins_duplicate_keys(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    tmp_dir = tmp_path / ".tmp"
    f_sum = tmp_dir / "NU_Q4_2025_summary.txt"
    f_inv = tmp_dir / "NU_Q4_2025_investor_update_summary.txt"
    f_sum.write_text("standard summary", encoding="utf-8")
    f_inv.write_text("investor update summary", encoding="utf-8")

    # Force ordering: f_sum then f_inv
    def _order_forward(_path: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(_path), key=lambda e: e.name)

    session1 = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_order_forward)
    comm1 = session1.record_directory_selection("quarterly_summaries_tmp_v1", "NU")
    # NU_Q4_2025_summary.txt comes after NU_Q4_2025_investor_update_summary.txt in sort order
    assert comm1.effective_winners["Q4_2025"] == ".tmp/NU_Q4_2025_summary.txt"

    # Force reverse ordering: f_inv comes last
    def _order_reverse(_path: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(_path), key=lambda e: e.name, reverse=True)

    session2 = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_order_reverse)
    comm2 = session2.record_directory_selection("quarterly_summaries_tmp_v1", "NU")
    assert comm2.effective_winners["Q4_2025"] == ".tmp/NU_Q4_2025_investor_update_summary.txt"


def test_themes_recent_transcripts(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    p_dir = tmp_path / "transcripts" / "processed"
    quarters = [
        (1, 2024),
        (2, 2024),
        (3, 2024),
        (4, 2024),
        (1, 2025),
        (2, 2025),
    ]
    for q, y in quarters:
        (p_dir / f"TEST_Q{q}_{y}.txt").write_text(f"text {q} {y}", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    comm = session.record_directory_selection("themes_recent_transcripts_v1", "TEST")

    # Expect exactly the 4 most recent quarters: Q3_2024, Q4_2024, Q1_2025, Q2_2025
    expected_keys = {"Q3_2024", "Q4_2024", "Q1_2025", "Q2_2025"}
    assert set(comm.effective_winners.keys()) == expected_keys


def test_ir_briefs_and_saydo_and_price_chart_selectors(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    tmp_dir = tmp_path / ".tmp"
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)

    # IR briefs
    (tmp_dir / "TEST_Q1_2025_press_release_summary.txt").write_text("pr", encoding="utf-8")
    (tmp_dir / "TEST_Q1_2025_presentation_brief.txt").write_text("pres", encoding="utf-8")

    # SayDo
    (tmp_dir / "SayDo_TEST_Q4_2024_Q1_2025.txt").write_text("saydo", encoding="utf-8")

    # FMP price chart
    (fmp_dir / "GOOG_historical_price_chart_v1.json").write_text("{}", encoding="utf-8")
    (fmp_dir / "GOOGL_historical_price_chart_v1.json").write_text("{}", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)

    ir_comm = session.record_directory_selection("ir_briefs_tmp_v1", "TEST")
    assert "2025_Q1_press_release_summary" in ir_comm.effective_winners
    assert "2025_Q1_presentation_brief" in ir_comm.effective_winners

    saydo_comm = session.record_directory_selection("saydo_cards_tmp_v1", "TEST")
    assert "Q1_2025_Q4_2024" in saydo_comm.effective_winners

    goog_comm = session.record_directory_selection("fmp_price_chart_v1", "GOOG")
    assert len(goog_comm.selected_relative_paths) == 2


def test_fixed_path_selectors(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    qa_dir = tmp_path / "data" / "qa_topics"
    thesis_dir = tmp_path / "micro_thesis" / "holdings"
    qa_dir.mkdir(parents=True, exist_ok=True)
    thesis_dir.mkdir(parents=True, exist_ok=True)

    (qa_dir / "AAPL.json").write_text('{"topics": []}', encoding="utf-8")
    (thesis_dir / "AAPL.json").write_text('{"thesis": "long"}', encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    s6_comm = session.record_directory_selection("S6", "AAPL")
    s14_comm = session.record_directory_selection("S14", "AAPL")

    assert s6_comm.directory_exists is True
    assert "AAPL.json" in s6_comm.effective_winners
    assert s14_comm.directory_exists is True
    assert "AAPL.json" in s14_comm.effective_winners


def test_replay_verification_and_tamper_detection(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    tmp_dir = tmp_path / ".tmp"
    f1 = tmp_dir / "TEST_Q1_2025_summary.txt"
    f1.write_text("original summary", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    session.record_directory_selection("S1", "TEST")
    session.read_text(".tmp/TEST_Q1_2025_summary.txt")
    receipt = session.seal()

    # Replay succeeds on untouched tree
    replayed = replay_file_capture(receipt, tmp_path)
    assert replayed.receipt_digest == receipt.receipt_digest

    # Tamper with file content
    f1.write_text("tampered summary", encoding="utf-8")
    with pytest.raises(FileCaptureMismatchError):
        replay_file_capture(receipt, tmp_path)

    # Restore content, now add an unexpected matching file
    f1.write_text("original summary", encoding="utf-8")
    f2 = tmp_dir / "TEST_Q2_2025_summary.txt"
    f2.write_text("new quarter", encoding="utf-8")
    with pytest.raises(FileCaptureMismatchError):
        replay_file_capture(receipt, tmp_path)

    # Remove unexpected file, now delete expected file
    f2.unlink()
    f1.unlink()
    with pytest.raises(FileCaptureMismatchError):
        replay_file_capture(receipt, tmp_path)


def test_replay_mismatch_on_winner_order_reversal(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    tmp_dir = tmp_path / ".tmp"
    (tmp_dir / "NU_Q4_2025_summary.txt").write_text("sum", encoding="utf-8")
    (tmp_dir / "NU_Q4_2025_investor_update_summary.txt").write_text("inv", encoding="utf-8")

    def _fwd(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name)

    def _rev(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name, reverse=True)

    session = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_fwd)
    session.record_directory_selection("S1", "NU")
    receipt = session.seal()

    # Replaying with reversed order alters effective_winners -> must fail closed
    with pytest.raises(FileCaptureMismatchError):
        replay_file_capture(receipt, tmp_path, scandir_fn=_rev)


def test_directory_state_distinction_missing_vs_empty(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    session1 = ReportFileCaptureSession(repo_root=tmp_path)
    # S6 directory data/qa_topics does not exist
    comm1 = session1.record_directory_selection("S6", "TEST")
    assert comm1.directory_exists is False
    assert comm1.directory_states == {"data/qa_topics": False}

    # Now create the directory, but leave it empty
    (tmp_path / "data" / "qa_topics").mkdir(parents=True, exist_ok=True)
    session2 = ReportFileCaptureSession(repo_root=tmp_path)
    comm2 = session2.record_directory_selection("S6", "TEST")
    assert comm2.directory_exists is True
    assert comm2.directory_states == {"data/qa_topics": True}

    # Distinct commitments and digests
    assert comm1.selection_digest != comm2.selection_digest


def test_missing_file_and_sealed_isolation(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    session = ReportFileCaptureSession(repo_root=tmp_path)

    # Reading missing path records file_exists=False and raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        session.read_bytes(".tmp/missing.txt")

    comm = session.get_file_commitment(".tmp/missing.txt")
    assert comm is not None
    assert comm.file_exists is False

    # Capture one present file
    present = tmp_path / ".tmp" / "present.txt"
    present.write_bytes(b"hello world")
    data = session.read_bytes(".tmp/present.txt")
    assert data == b"hello world"

    # Repeated read uses cache
    assert session.read_bytes(".tmp/present.txt") == b"hello world"

    receipt = session.seal()
    assert receipt.status == "sealed"

    # In sealed mode, repeated read succeeds from cache
    assert session.read_bytes(".tmp/present.txt") == b"hello world"
    # In sealed mode, missing file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        session.read_bytes(".tmp/missing.txt")
    # In sealed mode, uncaptured path raises not_captured
    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.read_bytes(".tmp/other.txt")
    assert exc_info.value.reason == "not_captured"


def test_reparse_point_and_containment_defense(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    session = ReportFileCaptureSession(repo_root=tmp_path)

    # Outside content roots
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(FileCaptureUnavailableError) as exc_info:
        session.read_bytes("outside.txt")
    assert exc_info.value.reason == "artifact_unstable"

    # Symlink defense
    target = tmp_path / ".tmp" / "real.txt"
    target.write_text("real content", encoding="utf-8")
    symlink_file = tmp_path / ".tmp" / "symlink.txt"
    try:
        symlink_file.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not supported on this environment")

    session2 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc_info2:
        session2.read_bytes(".tmp/symlink.txt")
    assert exc_info2.value.reason == "artifact_unstable"


def test_parser_failures_invalidate_session(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    bad_utf8 = tmp_path / ".tmp" / "bad_utf8.txt"
    bad_utf8.write_bytes(b"\xff\xfe\x00")

    session1 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc1:
        session1.read_text(".tmp/bad_utf8.txt")
    assert exc1.value.reason == "decode_failed"

    # Invalidation prevents sealing
    with pytest.raises(FileCaptureUnavailableError) as exc_seal:
        session1.seal()
    assert exc_seal.value.reason == "session_invalidated"

    bad_json = tmp_path / ".tmp" / "bad.json"
    bad_json.write_text("{not valid json", encoding="utf-8")

    session2 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc2:
        session2.read_json(".tmp/bad.json")
    assert exc2.value.reason == "json_failed"

    with pytest.raises(FileCaptureUnavailableError) as exc_seal2:
        session2.seal()
    assert exc_seal2.value.reason == "session_invalidated"


def test_encoding_and_missing_reuse_isolation(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    doc = tmp_path / ".tmp" / "doc.txt"
    doc.write_text("hello text", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        session.read_bytes(".tmp/never_there.txt")
    # Repeated missing read raises FileNotFoundError from recorded commitment
    with pytest.raises(FileNotFoundError):
        session.read_bytes(".tmp/never_there.txt")

    assert session.read_text(".tmp/doc.txt", encoding="utf-8") == "hello text"
    # Conflicting encoding before sealing invalidates session
    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.read_text(".tmp/doc.txt", encoding="ascii")
    assert exc.value.reason == "decode_failed"
    with pytest.raises(FileCaptureUnavailableError) as seal_exc:
        session.seal()
    assert seal_exc.value.reason == "session_invalidated"

    # Sealed session encoding mismatch
    session2 = ReportFileCaptureSession(repo_root=tmp_path)
    assert session2.read_text(".tmp/doc.txt", encoding="utf-8") == "hello text"
    session2.seal()
    assert session2.read_text(".tmp/doc.txt", encoding="utf-8") == "hello text"
    with pytest.raises(FileCaptureUnavailableError) as exc2:
        session2.read_text(".tmp/doc.txt", encoding="latin-1")
    assert exc2.value.reason == "not_captured"


def test_peer_fundamentals_s13_expansion_and_fmp_isolation(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    p_sel = tmp_path / "data" / "peer_selection"
    fmp = tmp_path / "data" / "historical" / "fmp"
    p_sel.mkdir(parents=True, exist_ok=True)
    fmp.mkdir(parents=True, exist_ok=True)

    (p_sel / "MELI.json").write_text(
        '{"suggestions": [{"ticker": "AMZN"}, {"ticker": "SE"}]}', encoding="utf-8"
    )
    (fmp / "MELI_profile.json").write_text('{"companyName": "MercadoLibre"}', encoding="utf-8")
    (fmp / "MELI_peers.json").write_text('{"peersList": ["EBAY"]}', encoding="utf-8")
    (fmp / "MELI_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    (fmp / "AMZN_profile.json").write_text('{"companyName": "Amazon"}', encoding="utf-8")
    (fmp / "AMZN_key_metrics_ttm.json").write_text("[]", encoding="utf-8")
    (fmp / "AMZN_income_statement_quarterly.json").write_text("[]", encoding="utf-8")
    (fmp / "SE_profile.json").write_text('{"companyName": "Sea Limited"}', encoding="utf-8")
    (fmp / "EBAY_profile.json").write_text('{"companyName": "eBay"}', encoding="utf-8")

    # Unrelated files in data/historical/fmp must NOT be captured
    (fmp / "BABA_profile.json").write_text('{"companyName": "Alibaba"}', encoding="utf-8")
    (fmp / "XYZ_quote.json").write_text('{"price": 100}', encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    s13 = session.record_directory_selection("peer_fundamentals_json_v1", "MELI")

    assert "MELI_profile.json" in s13.effective_winners
    assert "MELI_peers.json" in s13.effective_winners
    assert "MELI_income_statement_quarterly.json" not in s13.effective_winners
    assert "AMZN_profile.json" in s13.effective_winners
    assert "AMZN_key_metrics_ttm.json" in s13.effective_winners
    assert "AMZN_income_statement_quarterly.json" in s13.effective_winners
    assert "SE_profile.json" in s13.effective_winners

    # EBAY is only in MELI_peers.json, NOT in captured S12 suggestions.
    # Per Sol finding 1, S13 must derive peer expansion EXCLUSIVELY from captured S12.
    assert "EBAY_profile.json" not in s13.effective_winners

    # Unrelated files excluded
    assert "BABA_profile.json" not in s13.effective_winners
    assert "XYZ_quote.json" not in s13.effective_winners

    # S12 was automatically captured
    s12 = session.get_directory_selection("peer_selection_json_v1", "MELI")
    assert s12 is not None

    receipt = session.seal()
    replay = replay_file_capture(receipt, tmp_path)
    assert replay.receipt_digest == receipt.receipt_digest


def test_non_competing_reorder_invariance(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    (tmp_path / ".tmp" / "NU_Q1_2025_summary.txt").write_text("q1 summary", encoding="utf-8")
    (tmp_path / ".tmp" / "NU_Q2_2025_summary.txt").write_text("q2 summary", encoding="utf-8")

    def _fwd(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name)

    def _rev(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name, reverse=True)

    session_fwd = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_fwd)
    c_fwd = session_fwd.record_directory_selection("S1", "NU")

    session_rev = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_rev)
    c_rev = session_rev.record_directory_selection("S1", "NU")

    assert c_fwd.effective_winners == c_rev.effective_winners
    assert c_fwd.selection_digest == c_rev.selection_digest


def test_all_fixed_path_selectors_s6_through_s14_and_s16(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    files = {
        "S6": ("data/qa_topics/GOOG.json", "{}"),
        "S7": ("data/filing_intelligence/GOOG.json", "{}"),
        "S8": ("data/bear_case/GOOG.json", "{}"),
        "S9": (".tmp/news_cache/GOOG.json", "{}"),
        "S10": ("data/saydo_filter/GOOG.json", "{}"),
        "S11": ("data/segment_definitions/GOOG.json", "{}"),
        "S12": ("data/peer_selection/GOOG.json", "{}"),
        "S14": ("micro_thesis/holdings/GOOG.json", "{}"),
        "S16": ("data/report_comments/GOOG/2026-01-01.json", "{}"),
    }
    for _sel, (rel, content) in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    for sel, (rel, _) in files.items():
        comm = session.record_directory_selection(sel, "GOOG")
        assert comm.directory_exists is True
        assert Path(rel).name in comm.effective_winners


def test_symlinked_directory_rejection(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    real_dir = tmp_path / "data" / "qa_topics_real"
    real_dir.mkdir(parents=True, exist_ok=True)
    sym_dir = tmp_path / "data" / "qa_topics"
    try:
        sym_dir.symlink_to(real_dir)
    except OSError:
        pytest.skip("symlinks not supported on this platform")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.record_directory_selection("S6", "TEST")
    assert exc.value.reason == "artifact_unstable"


def test_narrow_holdings_root_enforcement(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    unrelated = tmp_path / "micro_thesis" / "unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.read_bytes("micro_thesis/unrelated.json")
    assert exc.value.reason == "artifact_unstable"
    with pytest.raises(FileCaptureUnavailableError) as seal_exc:
        session.seal()
    assert seal_exc.value.reason == "session_invalidated"

    # Reject overly broad content roots and holdings_dir in constructor
    with pytest.raises(ValueError, match="Overly broad content root"):
        ReportFileCaptureSession(repo_root=tmp_path, content_roots=[tmp_path])
    with pytest.raises(ValueError, match="Overly broad content root"):
        ReportFileCaptureSession(repo_root=tmp_path, content_roots=[tmp_path / "micro_thesis"])
    with pytest.raises(ValueError, match="Overly broad holdings_dir"):
        ReportFileCaptureSession(repo_root=tmp_path, holdings_dir=tmp_path / "micro_thesis")
    with pytest.raises(ValueError, match="Overly broad holdings_dir"):
        ReportFileCaptureSession(repo_root=tmp_path, holdings_dir=tmp_path)


def test_mapping_proxy_runtime_immutability(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    (tmp_path / ".tmp" / "f.txt").write_text("sample", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    session.read_text(".tmp/f.txt")
    comm = session.record_directory_selection("S1", "TEST")
    receipt = session.seal()

    for target in (
        comm.effective_winners,
        comm.directory_states,
        comm.member_sha256s,
        receipt.files,
        receipt.selections,
    ):
        assert isinstance(target, types.MappingProxyType)
        with pytest.raises(TypeError, match="does not support item assignment"):
            exec("target['tamper'] = 'val'", {"target": target})


def test_budget_validation_and_zero_byte_exhaustion(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    with pytest.raises(ValidationError):
        FileCaptureBudget(max_bytes_per_file=0)
    with pytest.raises(ValidationError):
        FileCaptureBudget(max_total_bytes=-10)

    f_full = tmp_path / ".tmp" / "full.txt"
    f_empty = tmp_path / ".tmp" / "empty.txt"
    f_full.write_bytes(b"a" * 50)
    f_empty.write_bytes(b"")

    budget = FileCaptureBudget(max_bytes_per_file=100, max_total_bytes=50)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)
    session.read_bytes(".tmp/full.txt")
    assert session.total_bytes_captured == 50

    # Reading 0-byte file when total budget is exhausted fails closed
    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.read_bytes(".tmp/empty.txt")
    assert exc.value.reason == "budget_exhausted"
    with pytest.raises(FileCaptureUnavailableError) as seal_exc:
        session.seal()
    assert seal_exc.value.reason == "session_invalidated"


def test_disk_free_sealed_access_and_unencoded_rejection(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    (tmp_path / ".tmp" / "binary.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / ".tmp" / "text.txt").write_text("hello text", encoding="utf-8")
    (tmp_path / ".tmp" / "data.json").write_text('{"status": "ok"}', encoding="utf-8")
    (tmp_path / "data" / "qa_topics").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "qa_topics" / "TEST.json").write_text('{"topic": "qa"}', encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    session.read_bytes(".tmp/binary.bin")  # captured only as bytes
    session.read_text(".tmp/text.txt", encoding="utf-8")  # captured as text
    session.read_json(".tmp/data.json")  # captured as json
    session.record_directory_selection("S6", "TEST")
    receipt = session.seal()
    assert receipt.status == "sealed"

    # Delete all source files from disk to prove sealed session operates offline
    shutil.rmtree(tmp_path / ".tmp")
    shutil.rmtree(tmp_path / "data")

    # Offline reads succeed from cache
    assert session.read_bytes(".tmp/binary.bin") == b"\x00\x01\x02"
    assert session.read_text(".tmp/text.txt", encoding="utf-8") == "hello text"
    assert session.read_json(".tmp/data.json") == {"status": "ok"}
    sel = session.record_directory_selection("S6", "TEST")
    assert sel.effective_winners["TEST.json"] == "data/qa_topics/TEST.json"

    # Reading text on a file captured ONLY as bytes raises not_captured
    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.read_text(".tmp/binary.bin", encoding="utf-8")
    assert exc.value.reason == "not_captured"


def test_replay_failure_on_deleted_file(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    (tmp_path / ".tmp" / "present.txt").write_text("present content", encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    session.read_text(".tmp/present.txt", encoding="utf-8")
    receipt = session.seal()

    # Delete captured file
    (tmp_path / ".tmp" / "present.txt").unlink()

    with pytest.raises(FileCaptureMismatchError):
        replay_file_capture(receipt, tmp_path)


def test_missing_file_commitment_ceiling(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    budget = FileCaptureBudget(max_files=1)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    # First missing file records commitment and raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        session.read_bytes(".tmp/missing_1.txt")
    comm1 = session.get_file_commitment(".tmp/missing_1.txt")
    assert comm1 is not None and not comm1.file_exists

    # Reading the same missing file again uses cached commitment without consuming a new slot
    with pytest.raises(FileNotFoundError):
        session.read_bytes(".tmp/missing_1.txt")

    # Second distinct missing file exceeds max_files=1 and raises budget_exhausted
    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.read_bytes(".tmp/missing_2.txt")
    assert exc.value.reason == "budget_exhausted"

    # Session is invalidated
    with pytest.raises(FileCaptureUnavailableError) as seal_exc:
        session.seal()
    assert seal_exc.value.reason == "session_invalidated"


def test_mtime_ns_capture_and_replay_mismatch_on_timestamp_mutation(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    (tmp_path / "data" / "bear_case").mkdir(parents=True, exist_ok=True)
    f = tmp_path / "data" / "bear_case" / "TEST.json"
    f.write_text('{"thesis": "bear"}', encoding="utf-8")

    session = ReportFileCaptureSession(repo_root=tmp_path)
    # read_mtime_ns helper returns integer nanoseconds
    mtime1 = session.read_mtime_ns("data/bear_case/TEST.json")
    assert isinstance(mtime1, int) and mtime1 > 0
    comm = session.get_file_commitment("data/bear_case/TEST.json")
    assert comm is not None and comm.mtime_ns == mtime1

    receipt = session.seal()
    # In sealed mode, read_mtime_ns works without hitting disk
    assert session.read_mtime_ns("data/bear_case/TEST.json") == mtime1

    # Replay immediately matches
    replay_file_capture(receipt, tmp_path)

    # Mutate timestamp only (+1_000_000_000 ns) without altering file bytes
    cur_stat = f.stat()
    new_mtime_ns = cur_stat.st_mtime_ns + 1_000_000_000
    os.utime(f, ns=(cur_stat.st_atime_ns, new_mtime_ns))

    # Replay fails with mtime_ns mismatch
    with pytest.raises(FileCaptureMismatchError, match="mtime_ns mismatch"):
        replay_file_capture(receipt, tmp_path)


def test_read_text_lookup_error_and_sealed_precheck(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    good_file = tmp_path / ".tmp" / "good.txt"
    good_file.write_text("valid text", encoding="utf-8")

    # Unknown codec in active session raises decode_failed and invalidates
    s1 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc_codec:
        s1.read_text(".tmp/good.txt", encoding="no-such-codec-xyz")
    assert exc_codec.value.reason == "decode_failed"
    with pytest.raises(FileCaptureUnavailableError) as seal_exc1:
        s1.seal()
    assert seal_exc1.value.reason == "session_invalidated"

    # Unknown error handler in active session raises decode_failed and invalidates
    s2 = ReportFileCaptureSession(repo_root=tmp_path)
    with pytest.raises(FileCaptureUnavailableError) as exc_err:
        s2.read_text(".tmp/good.txt", errors="no-such-handler")
    assert exc_err.value.reason == "decode_failed"
    with pytest.raises(FileCaptureUnavailableError) as seal_exc2:
        s2.seal()
    assert seal_exc2.value.reason == "session_invalidated"

    # Sealed pre-check on invalid UTF-8 bytes:
    # File has invalid UTF-8 bytes, captured via read_bytes
    bin_file = tmp_path / ".tmp" / "invalid_utf8.bin"
    bin_file.write_bytes(b"\xff\xfe\x00\x00")
    s3 = ReportFileCaptureSession(repo_root=tmp_path)
    s3.read_bytes(".tmp/invalid_utf8.bin")
    receipt3 = s3.seal()
    assert receipt3.status == "sealed"

    # Calling read_text with unrecorded encoding raises not_captured BEFORE decoding,
    # and does NOT invalidate the sealed session
    with pytest.raises(FileCaptureUnavailableError) as exc_sealed:
        s3.read_text(".tmp/invalid_utf8.bin", encoding="utf-8")
    assert exc_sealed.value.reason == "not_captured"

    # Subsequent valid cached read still works (session was NOT invalidated)
    assert s3.read_bytes(".tmp/invalid_utf8.bin") == b"\xff\xfe\x00\x00"


def test_directory_state_replay_mismatches(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    # 1. Absent directory becomes present (empty) during replay:
    session1 = ReportFileCaptureSession(repo_root=tmp_path)
    comm1 = session1.record_directory_selection("saydo_filter_json_v1", "TEST")
    assert not comm1.directory_exists
    receipt1 = session1.seal()

    # Create the directory before replay
    (tmp_path / "data" / "saydo_filter").mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileCaptureMismatchError, match="mismatch"):
        replay_file_capture(receipt1, tmp_path)

    # 2. Present empty directory is deleted before replay:
    (tmp_path / "data" / "bear_case").mkdir(parents=True, exist_ok=True)
    session2 = ReportFileCaptureSession(repo_root=tmp_path)
    comm2 = session2.record_directory_selection("bear_case_json_v1", "TEST")
    assert comm2.directory_exists
    receipt2 = session2.seal()

    # Delete the directory before replay
    (tmp_path / "data" / "bear_case").rmdir()
    with pytest.raises(FileCaptureMismatchError, match="mismatch"):
        replay_file_capture(receipt2, tmp_path)


def test_sorted_selectors_reorder_invariance_s4_s5_s15(tmp_path: Path) -> None:
    _setup_base_tree(tmp_path)
    # S4 (ir_briefs_tmp_v1)
    (tmp_path / ".tmp" / "TEST_Q1_2024_presentation_brief.txt").write_text("b1", encoding="utf-8")
    (tmp_path / ".tmp" / "TEST_Q1_2024_press_release_summary.txt").write_text(
        "b2", encoding="utf-8"
    )
    (tmp_path / ".tmp" / "TEST_Q2_2025_presentation_brief.txt").write_text("b3", encoding="utf-8")

    # S5 (saydo_cards_tmp_v1)
    (tmp_path / ".tmp" / "SayDo_TEST_Q1_2024_Q2_2024.txt").write_text("sd1", encoding="utf-8")
    (tmp_path / ".tmp" / "SayDo_TEST_Q2_2024_Q3_2024.txt").write_text("sd2", encoding="utf-8")

    # S15 (fmp_price_chart_v1)
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / "TEST_price_chart_1y.json").write_text("[]", encoding="utf-8")
    (fmp / "TEST_price_chart_5y.json").write_text("[]", encoding="utf-8")

    def _fwd(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name)

    def _rev(p: str | Path) -> Iterable[os.DirEntry[str]]:
        return sorted(os.scandir(p), key=lambda e: e.name, reverse=True)

    for sel in ("ir_briefs_tmp_v1", "saydo_cards_tmp_v1", "fmp_price_chart_v1"):
        s_fwd = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_fwd)
        c_fwd = s_fwd.record_directory_selection(sel, "TEST")
        r_fwd = s_fwd.seal()

        s_rev = ReportFileCaptureSession(repo_root=tmp_path, scandir_fn=_rev)
        c_rev = s_rev.record_directory_selection(sel, "TEST")
        r_rev = s_rev.seal()

        assert c_fwd.effective_winners == c_rev.effective_winners
        assert c_fwd.selection_digest == c_rev.selection_digest
        assert r_fwd.receipt_digest == r_rev.receipt_digest
        replay_file_capture(r_fwd, tmp_path, scandir_fn=_rev)


def test_fstat_size_refusal_when_file_grows_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_base_tree(tmp_path)
    f = tmp_path / ".tmp" / "growing.txt"
    f.write_bytes(b"small")

    budget = FileCaptureBudget(max_bytes_per_file=10)
    session = ReportFileCaptureSession(repo_root=tmp_path, budget=budget)

    orig_lstat = os.lstat
    call_count = 0

    def _mock_lstat(
        path: os.PathLike[str] | str, *args: object, **kwargs: object
    ) -> os.stat_result:
        nonlocal call_count
        res = orig_lstat(path, *args, **kwargs)
        if "growing.txt" in str(path):
            call_count += 1
            if call_count == 1:
                f.write_bytes(b"x" * 25)
        return res

    monkeypatch.setattr(os, "lstat", _mock_lstat)

    with pytest.raises(FileCaptureUnavailableError) as exc:
        session.read_bytes(".tmp/growing.txt")
    assert exc.value.reason == "budget_exhausted"


def test_hold_capture_write_set_reentrancy_and_thread_contention(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from report.file_capture import CAPTURE_WRITE_SETS, hold_capture_write_set
    from runtime.job_runtime import JobAlreadyRunningError

    _setup_base_tree(tmp_path)
    assert "capture-news-cache" in CAPTURE_WRITE_SETS
    assert "capture-bear-cache" in CAPTURE_WRITE_SETS

    with hold_capture_write_set(tmp_path, "capture-news-cache", wait_s=0.1):
        # Nested call in the same context reuses the held lock and acquires only missing sets.
        with hold_capture_write_set(
            tmp_path, ["capture-news-cache", "capture-bear-cache"], wait_s=0.1
        ):
            pass

        # A separate thread does not inherit the ContextVar claim and must observe contention.
        def _contend() -> None:
            with hold_capture_write_set(tmp_path, "capture-news-cache", wait_s=0.0):
                pass

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_contend)
            with pytest.raises(JobAlreadyRunningError):
                future.result()


def test_candidate_boot_isolation_and_bear_case_input_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date
    from io import StringIO
    from typing import Any, cast

    from report.models import (
        EarningsSection,
        FinancialsSection,
        SectionStatus,
        SegmentsSection,
        ThesisSection,
    )
    from report.renderers.workspace_sections.boot import _comment_boot_data
    from report.sections import bear_case
    from server_runtime.access import ReportCapabilityStore

    _setup_base_tree(tmp_path)

    def _forbid_capability(*args: object, **kwargs: object) -> str:
        raise AssertionError("candidate_mode must not read or mint capability secret")

    monkeypatch.setattr(ReportCapabilityStore, "load", _forbid_capability)
    monkeypatch.setattr(ReportCapabilityStore, "load_or_create", _forbid_capability)

    class _StubSpec:
        ticker = "TEST"
        generation_date = date(2026, 4, 20)
        repo_root = str(tmp_path)

    out = StringIO()
    _comment_boot_data(out, cast("Any", _StubSpec()), candidate_mode=True)
    rendered = out.getvalue()
    assert '"report_capability": ""' in rendered

    # Bear case input digest binding: cache stamped for one thesis must miss when thesis changes.
    valid_json = (
        '{"failure_modes": [{"hypothesis": "h", "evidence_in_data": "e", '
        '"leading_indicator": "l", "quantitative_impact": "q", '
        '"refutation_criteria": "r"}], "most_underweighted": "m", '
        '"out_of_scope_flags": []}'
    )
    thesis_a = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="Thesis A",
        break_conditions=["Break A"],
    )
    thesis_b = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="Thesis B",
        break_conditions=["Break B"],
    )
    fin = FinancialsSection(status=SectionStatus.MISSING_DATA)
    seg = SegmentsSection(status=SectionStatus.MISSING_DATA)
    earn = EarningsSection(status=SectionStatus.MISSING_DATA)

    def _stub_generate_bear(*_args: object, **_kwargs: object) -> str:
        return valid_json

    monkeypatch.setattr("report.sections.bear_case.generate_bear_case", _stub_generate_bear)
    sec_a = bear_case.build(
        ticker="TEST",
        repo_root=tmp_path,
        enable_llm=True,
        thesis=thesis_a,
        financials=fin,
        segments=seg,
        earnings=earn,
        force_refresh=True,
    )
    assert sec_a.status == SectionStatus.OK
    assert (tmp_path / "data" / "bear_case" / "TEST.json").exists()

    # Same inputs hit cache even with enable_llm=False
    sec_cached = bear_case.build(
        ticker="TEST",
        repo_root=tmp_path,
        enable_llm=False,
        thesis=thesis_a,
        financials=fin,
        segments=seg,
        earnings=earn,
    )
    assert sec_cached.status == SectionStatus.OK

    # Changed thesis invalidates the input-bound cache
    sec_miss = bear_case.build(
        ticker="TEST",
        repo_root=tmp_path,
        enable_llm=False,
        thesis=thesis_b,
        financials=fin,
        segments=seg,
        earnings=earn,
    )
    assert sec_miss.status == SectionStatus.LLM_PENDING


def test_fmp_price_chart_selection_preserves_primary_ticker_precedence(tmp_path: Path) -> None:
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "GOOG_price_chart_1y.json").write_text(
        json.dumps([{"date": "2026-10-24", "close": 123.45}]), encoding="utf-8"
    )
    (fmp_dir / "GOOGL_price_chart_1y.json").write_text(
        json.dumps([{"date": "2026-10-24", "close": 987.65}]), encoding="utf-8"
    )
    session = ReportFileCaptureSession(tmp_path)
    comm = session.record_directory_selection("fmp_price_chart_v1", "GOOG")
    winners = list(comm.effective_winners.values())
    assert winners[0] == "data/historical/fmp/GOOG_price_chart_1y.json"
    assert winners[1] == "data/historical/fmp/GOOGL_price_chart_1y.json"


def test_fmp_price_chart_alias_only_fallback(tmp_path: Path) -> None:
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "GOOGL_price_chart_1y.json").write_text(
        json.dumps([{"date": "2026-10-24", "close": 987.65}]), encoding="utf-8"
    )
    session = ReportFileCaptureSession(tmp_path)
    comm = session.record_directory_selection("fmp_price_chart_v1", "GOOG")
    winners = list(comm.effective_winners.values())
    assert winners == ["data/historical/fmp/GOOGL_price_chart_1y.json"]


def test_fmp_price_chart_multiple_primary_files_standalone_parity(tmp_path: Path) -> None:
    from report.sections.snapshot import latest_price

    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "GOOG_price_chart_5y.json").write_text(
        json.dumps([{"date": "2026-10-24", "close": 123.45}]), encoding="utf-8"
    )
    (fmp_dir / "GOOG_price_chart_1y.json").write_text(
        json.dumps([{"date": "2026-10-24", "close": 987.65}]), encoding="utf-8"
    )

    standalone = latest_price("GOOG", tmp_path)
    session = ReportFileCaptureSession(tmp_path)
    session.record_directory_selection("fmp_price_chart_v1", "GOOG")
    with session:
        captured = latest_price("GOOG", tmp_path)

    assert standalone == 987.65
    assert captured == standalone


def test_fmp_price_chart_non_goog_suffix_l_file_rejected_by_both_readers(tmp_path: Path) -> None:
    from report.sections.snapshot import latest_price

    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "CL_price_chart_1y.json").write_text(
        json.dumps([{"date": "2026-10-24", "close": 987.65}]), encoding="utf-8"
    )

    standalone = latest_price("C", tmp_path)
    session = ReportFileCaptureSession(tmp_path)
    comm = session.record_directory_selection("fmp_price_chart_v1", "C")
    with session:
        captured = latest_price("C", tmp_path)

    assert standalone is None
    assert captured is None
    assert comm.effective_winners == {}
