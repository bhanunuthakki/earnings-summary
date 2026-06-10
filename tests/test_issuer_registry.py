"""Tests for issuer_registry — the tracked-companies-synced IR issuer registry.

``issuer_registry`` maintains ``data/issuer_registry.json`` (fiscal calendar +
name aliases per active tracked ticker) so the IR-document categorizer resolves
eval/portfolio tickers without a code edit. Curated ``ir_uploads.ISSUER_REGISTRY``
entries win; the generic ``fye_MM`` calendar covers off-cycle FYEs the curated
table doesn't list (NVDA 01-25, AVGO 11-02).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import issuer_registry as reg  # noqa: E402
from ir_uploads import _period_end_for, calendar_id_from_fye  # noqa: E402


def _make_repo(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    """rows = (ticker, name, list_type, fiscal_year_end). Returns the repo root."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    try:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
            "fiscal_year_end TEXT, archived_at TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, archived_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


# --- calendar derivation (extends ir_uploads.calendar_id_from_fye) ----------


def test_calendar_id_from_fye_generic_fallback() -> None:
    # exact curated-table matches still win
    assert calendar_id_from_fye("01-31") == "veeva"
    assert calendar_id_from_fye("12-31") == "calendar"
    # off-cycle months not in the table -> generic fye_MM
    assert calendar_id_from_fye("01-25") == "fye_01"  # NVDA
    assert calendar_id_from_fye("11-02") == "fye_11"  # AVGO
    # december / junk / missing -> Dec calendar
    assert calendar_id_from_fye("12-28") == "calendar"
    assert calendar_id_from_fye("99-99") == "calendar"
    assert calendar_id_from_fye(None) == "calendar"


def test_period_end_generic_fye_matches_nvda() -> None:
    # NVDA late-Jan FYE: FY2026 quarters end Apr/Jul/Oct 2025 then Jan 2026.
    assert [_period_end_for("fye_01", 2026, q) for q in (1, 2, 3, 4)] == [
        date(2025, 4, 30),
        date(2025, 7, 31),
        date(2025, 10, 31),
        date(2026, 1, 31),
    ]


def test_period_end_generic_fye_november() -> None:
    assert [_period_end_for("fye_11", 2026, q) for q in (1, 2, 3, 4)] == [
        date(2026, 2, 28),
        date(2026, 5, 31),
        date(2026, 8, 31),
        date(2026, 11, 30),
    ]


# --- name aliases ----------------------------------------------------------


def test_derive_name_aliases_strips_suffix() -> None:
    assert reg.derive_name_aliases("NVIDIA Corporation", "NVDA") == ["NVIDIA Corporation", "NVIDIA"]
    assert reg.derive_name_aliases("Broadcom Inc.", "AVGO") == ["Broadcom Inc.", "Broadcom"]
    assert reg.derive_name_aliases("", "XYZ") == ["XYZ"]
    assert reg.derive_name_aliases(None, "XYZ") == ["XYZ"]


# --- register / deregister / sync / effective merge ------------------------


def test_register_and_effective_entries(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [("NVDA", "NVIDIA Corporation", "evaluation", "01-25")])
    assert reg.register_issuer(repo, "NVDA") is True
    store = reg.load_store(repo)
    assert store["NVDA"]["calendar"] == "fye_01"
    assert "NVIDIA" in store["NVDA"]["name_aliases"]
    tickers = [t for t, *_ in reg.effective_entries(repo)]
    assert "NVDA" in tickers  # from the store
    assert "NU" in tickers  # curated still present


def test_register_skips_curated(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [("NU", "Nu Holdings", "portfolio", "12-31")])
    assert reg.register_issuer(repo, "NU") is False  # NU is curated in code → store untouched
    assert "NU" not in reg.load_store(repo)


def test_sync_all_adds_then_removes(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [
            ("NVDA", "NVIDIA Corporation", "evaluation", "01-25"),
            ("AVGO", "Broadcom Inc.", "evaluation", "11-02"),
            ("NU", "Nu Holdings", "portfolio", "12-31"),  # curated → skipped
        ],
    )
    summary = reg.sync_all(repo)
    assert set(summary["added"]) == {"NVDA", "AVGO"}
    assert "NU" not in summary["added"]
    # drop AVGO from the active list → re-sync removes its store row
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.execute("DELETE FROM tracked_companies WHERE ticker = 'AVGO'")
    conn.commit()
    conn.close()
    summary2 = reg.sync_all(repo)
    assert "AVGO" in summary2["removed"]
    assert "AVGO" not in reg.load_store(repo)


def test_manual_override_never_clobbered(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [("AVGO", "Broadcom Inc.", "evaluation", "11-02")])
    reg.register_issuer(repo, "AVGO")
    store = reg.load_store(repo)
    store["AVGO"]["calendar"] = "custom_override"
    store["AVGO"]["manual_override"] = True
    reg.save_store(repo, store)
    # re-register + sync must preserve the hand-tuned calendar
    reg.register_issuer(repo, "AVGO")
    reg.sync_all(repo)
    assert reg.load_store(repo)["AVGO"]["calendar"] == "custom_override"
    # deregister must refuse to drop a manual override
    assert reg.deregister_issuer(repo, "AVGO") is False
    assert "AVGO" in reg.load_store(repo)
