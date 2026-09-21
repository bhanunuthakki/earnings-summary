"""Discovery ranks from frozen comparable membership, never vendor peer lists."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from compute.comparable_sets import (
    ComparableSetMember,
    ComparableSetResolution,
    freeze_comparable_set,
)
from discovery.need_rank import compute_need_rank, need_rank_from_json, need_rank_to_json


def test_rank_uses_frozen_members_and_retains_provenance(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        result = freeze_comparable_set(
            conn,
            ComparableSetResolution(
                ticker="NEW",
                method_version=1,
                metric_class="operating",
                members=[
                    ComparableSetMember("GOOD", "pinned_override"),
                    ComparableSetMember("CONTEXT", "llm_ratified", True),
                ],
                method_flags={},
                source_summary={"test": "synthetic"},
            ),
            as_of=date(2020, 1, 1),
        )
    fmp = tmp_path / "data/historical/fmp"
    fmp.mkdir(parents=True)
    (fmp / "NEW_peers.json").write_text(json.dumps(["RAW", "CONTEXT"]))
    names: dict[str, tuple[str | None, str | None]] = {
        t: (None, None) for t in ("GOOD", "RAW", "CONTEXT")
    }
    rank = compute_need_rank(tmp_path, db, "NEW", 0, eval_names=names)
    assert rank.eval_adjacency == 2.0
    assert any("GOOD" in reason for reason in rank.adjacency_reasons)
    assert all("RAW" not in reason and "CONTEXT" not in reason for reason in rank.adjacency_reasons)
    blob = need_rank_to_json(rank)
    assert blob["peer_source"] == result.comparable_set_id
    assert "pinned_override" in str(blob["peer_membership_reasons"])
    assert need_rank_from_json(blob) == rank


@pytest.mark.parametrize("corrupt_database", [False, True])
def test_raw_peer_cache_does_not_rank_when_canonical_set_missing(
    tmp_path: Path, corrupt_database: bool
) -> None:
    fmp = tmp_path / "data/historical/fmp"
    fmp.mkdir(parents=True)
    (fmp / "NEW_peers.json").write_text(json.dumps(["RAW"]))
    db_path = tmp_path / "absent.db"
    if corrupt_database:
        db_path.write_bytes(b"invalid synthetic database")
    rank = compute_need_rank(tmp_path, db_path, "NEW", 0, eval_names={"RAW": (None, None)})
    assert rank.eval_adjacency == 0.0
    assert rank.peer_source == "unavailable"
    assert rank.garp == 0
    assert rank.financial_evidence is not None
    if corrupt_database:
        assert db_path.read_bytes() == b"invalid synthetic database"
    else:
        assert "canonical database unavailable" in rank.garp_reason
        assert rank.financial_evidence == {
            "status": "unavailable",
            "reason_codes": ["canonical_database_unavailable"],
            "decision_grade": False,
        }
        assert not db_path.exists()


def test_closed_future_and_old_method_members_do_not_rank(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        freeze_comparable_set(
            conn,
            ComparableSetResolution(
                ticker="NEW",
                method_version=1,
                metric_class="operating",
                members=[
                    ComparableSetMember("CLOSED", "industry_seed"),
                    ComparableSetMember("FUTURE", "pinned_override"),
                ],
                method_flags={},
                source_summary={},
            ),
            as_of=date(2020, 1, 1),
        )
        conn.execute(
            "UPDATE comparable_set_members SET valid_to='2020-01-02' WHERE member_ticker='CLOSED'"
        )
        conn.execute(
            "UPDATE comparable_set_members SET valid_from='9999-01-01' WHERE member_ticker='FUTURE'"
        )
        freeze_comparable_set(
            conn,
            ComparableSetResolution(
                ticker="OLD",
                method_version=0,
                metric_class="operating",
                members=[ComparableSetMember("CLOSED", "pinned_override")],
                method_flags={},
                source_summary={},
            ),
            as_of=date(2020, 1, 1),
        )
    names: dict[str, tuple[str | None, str | None]] = {
        "CLOSED": (None, None),
        "FUTURE": (None, None),
    }
    assert compute_need_rank(tmp_path, db, "NEW", 0, eval_names=names).eval_adjacency == 0
    assert compute_need_rank(tmp_path, db, "OLD", 0, eval_names=names).eval_adjacency == 0
