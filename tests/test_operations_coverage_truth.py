"""Operations distinguishes authorization from observed source evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.data_policy_settings_panel import read_sec_coverage_state


def test_missing_sec_database_is_not_a_successful_empty_roster(tmp_path: Path) -> None:
    view = read_sec_coverage_state(tmp_path / "absent.db")
    assert view.state == "unavailable"
    assert not (tmp_path / "absent.db").exists()


def test_sec_eligibility_never_claims_observed_coverage(tmp_path: Path) -> None:
    db = tmp_path / "synthetic.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies(ticker TEXT,name TEXT,list_type TEXT,sec_validated INTEGER,filing_regime TEXT,instrument_type TEXT,archived_at TEXT)"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES('ACME','Synthetic issuer','portfolio',1,'10-K','equity',NULL)"
        )
    view = read_sec_coverage_state(db)
    assert view.companies[0].coverage_status != "Automatic full"
    assert view.companies[0].coverage_status == "Not yet wired"
