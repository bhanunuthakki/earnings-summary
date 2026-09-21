"""Adopted advice requires affirmative adoption or an explicit owner attestation."""

# Dates below are fictional test data.
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from pipeline.decision_journal_panel import render_decision_journal_list


def test_adopted_filter_requires_followed_or_attested_advice(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "adoption.db")
    rows: list[tuple[int, bool]] = []
    with sqlite3.connect(db_path) as conn:
        for who, action, attested, expected in (
            ("advisor", "followed", False, True),
            ("advisor", "ignored", False, False),
            ("advisor", "partial", False, False),
            ("advisor", "reversed", False, False),
            ("advisor", None, False, False),
            ("advisor", None, True, True),
            ("advisor", "partial", True, True),
            ("owner", "followed", True, False),
        ):
            memo = conn.execute(
                "INSERT INTO advisor_memos (user_id,kind,ticker,title,body_md,context_json,"
                "stance,score_status,created_at) VALUES ('bhanu','position_review','NU','t','b',"
                "?,'hold','scored','2035-05-05T00:00:00')",
                (json.dumps({"owner_attested_change": attested}),),
            ).lastrowid
            row_id = conn.execute(
                "INSERT INTO decisions (ticker,recommendation_kind,decided_by,made_at,"
                "created_at,user_action_kind,source_memo_id) VALUES ('NU','hold',?,"
                "'2035-05-05T00:00:00','2035-05-05T00:00:00',?,?)",
                (who, action, memo),
            ).lastrowid
            assert row_id is not None
            rows.append((row_id, expected))
        conn.commit()

    html = render_decision_journal_list(db_path, filter_="adopted")
    for row_id, expected in rows:
        assert (f'data-decision-id="{row_id}"' in html) is expected
