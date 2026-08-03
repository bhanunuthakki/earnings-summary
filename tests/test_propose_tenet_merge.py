"""Tests for execution/propose_tenet_merge.py (B5) — the one-off owner-tapped
merge-candidate stager for two duplicate current Tenets (prod evidence:
tenets 20/31, see src/synthesis/semantic_tension.py).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from synthesis.tenets import list_tenets, record_tenet

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def _argv(db_path: Path, keep: int, merge: int, *, apply: bool = False) -> list[str]:
    argv = [
        "propose_tenet_merge.py",
        "--keep",
        str(keep),
        "--merge",
        str(merge),
        "--db-path",
        str(db_path),
    ]
    if apply:
        argv.append("--apply")
    return argv


def test_dry_run_prints_and_does_not_mutate(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keep = record_tenet(
        body_md="I hold retirement-account positions through drawdowns without exception.",
        scope_key="retirement-account-hold-discipline",
        db_path=db_path,
    )
    merge = record_tenet(
        body_md="I hold my tax-advantaged accounts through drawdowns without exception.",
        scope_key="tax-account-holding-discipline",
        db_path=db_path,
    )

    monkeypatch.setattr(sys, "argv", _argv(db_path, keep.id, merge.id))
    from execution import propose_tenet_merge

    rc = propose_tenet_merge.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert f"T{keep.id}" in out
    assert f"T{merge.id}" in out
    assert list_tenets(status="proposed", db_path=db_path) == []  # nothing was staged


def test_apply_stages_exactly_one_proposed_row(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = record_tenet(
        body_md="I hold retirement-account positions through drawdowns without exception.",
        scope_key="retirement-account-hold-discipline",
        source_note_ids=(1,),
        db_path=db_path,
    )
    merge = record_tenet(
        body_md="I hold my tax-advantaged accounts through drawdowns without exception.",
        scope_key="tax-account-holding-discipline",
        source_note_ids=(2,),
        db_path=db_path,
    )

    monkeypatch.setattr(sys, "argv", _argv(db_path, keep.id, merge.id, apply=True))
    from execution import propose_tenet_merge

    rc = propose_tenet_merge.main()
    assert rc == 0

    proposed = list_tenets(status="proposed", db_path=db_path)
    assert len(proposed) == 1
    row = proposed[0]
    assert row.scope_key == keep.scope_key
    assert row.body_md == keep.body_md
    assert row.provenance == "owner_merge_candidate"
    assert row.meta.get("tensions") == [merge.id]
    assert set(row.source_note_ids) == {1, 2}
    # both original current rows are untouched — owner-tapped approve does
    # the supersede, never this script.
    current_ids = {t.id for t in list_tenets(status="current", db_path=db_path)}
    assert current_ids == {keep.id, merge.id}


def test_rejects_missing_or_wrong_status(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keep = record_tenet(body_md="a belief", scope_key="a-belief", db_path=db_path)

    monkeypatch.setattr(sys, "argv", _argv(db_path, keep.id, 999999))
    from execution import propose_tenet_merge

    rc = propose_tenet_merge.main()
    assert rc == 1
    assert list_tenets(status="proposed", db_path=db_path) == []


def test_rejects_same_id(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keep = record_tenet(body_md="a belief", scope_key="a-belief", db_path=db_path)

    monkeypatch.setattr(sys, "argv", _argv(db_path, keep.id, keep.id))
    from execution import propose_tenet_merge

    rc = propose_tenet_merge.main()
    assert rc == 1
    assert list_tenets(status="proposed", db_path=db_path) == []


def test_rejects_non_tenet_kind(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from synthesis.insights import record_insight

    keep = record_tenet(body_md="a belief", scope_key="a-belief", db_path=db_path)
    stance_id = record_insight(
        scope_key="MELI",
        kind="stance",
        body_md="conviction intact",
        source_note_ids=[],
        watermark_id=None,
        db_path=db_path,
    )

    monkeypatch.setattr(sys, "argv", _argv(db_path, keep.id, stance_id))
    from execution import propose_tenet_merge

    rc = propose_tenet_merge.main()
    assert rc == 1
    assert list_tenets(status="proposed", db_path=db_path) == []
