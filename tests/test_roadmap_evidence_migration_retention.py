"""Later schema rollback must not erase durable roadmap evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


@pytest.mark.parametrize(
    ("table", "parent", "insert"),
    [
        (
            "macro_sensitivity_estimates",
            "0040_fmp_watchlist_recovery",
            "INSERT INTO macro_sensitivity_estimates(ticker,series_id,metric_version,shock_unit,"
            "return_unit,beta,r_squared,n_obs,lookback_window_days,input_sha,inputs_json,"
            "source_as_of,computed_at) VALUES('NU','us_10y','v2_rate_diff','percentage_point',"
            "'log_return',0.1,0.8,12,365,?, '{}','2026-09-19','2026-09-19')",
        ),
        (
            "etf_profile",
            "0042_governed_ir_event_revisions",
            "INSERT INTO etf_profile(ticker,profile_fetched_at,field_evidence_json) "
            "VALUES('AVDV','2026-09-19',json_object('source_hash',?))",
        ),
        (
            "fmp_refresh_receipts",
            "0044_position_entry_supersession",
            "INSERT INTO fmp_refresh_receipts VALUES('run',?,'2026-09-19','{}',printf('%064d',0))",
        ),
        (
            "sec_execution_receipts",
            "0045_fmp_recovery_receipts",
            "INSERT INTO sec_execution_receipts VALUES('attempt','request',0,'requested',"
            "'2026-09-19','{}',?)",
        ),
    ],
)
def test_populated_roadmap_evidence_refuses_downgrade(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    table: str,
    parent: str,
    insert: str,
) -> None:
    database = migrated_db(tmp_path / "retained.db")
    with sqlite3.connect(database) as conn:
        conn.execute(insert, ("a" * 64,))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    with pytest.raises(RuntimeError, match=r"preserved|retained"):
        command.downgrade(config, parent)
    with sqlite3.connect(database) as conn:
        assert conn.execute(f'SELECT count(*) FROM "{table}"').fetchone() == (1,)
