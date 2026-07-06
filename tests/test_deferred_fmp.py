"""Tests for the deferred-FMP-task backlog (src/pipeline/deferred_fmp.py).

Covers the idempotency contract (dedupe on (area, task, ticker)), status
transitions, open/all filtering, and round-trip persistence through the JSONL
store.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.deferred_fmp import (  # noqa: E402
    DeferredFmpTask,
    DeferredStatus,
    list_tasks,
    log_deferred,
    mark_done,
)


def _task(area: str, task: str, ticker: str | None = None, context: str = "") -> DeferredFmpTask:
    return DeferredFmpTask(
        area=area, task=task, blocked_on="fmp_splits_feed", ticker=ticker, context=context
    )


def test_first_log_creates(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    stored, created = log_deferred(_task("split_normalization", "adj BKNG", "BKNG"), store)
    assert created is True
    assert stored.status is DeferredStatus.OPEN
    assert len(list_tasks(store)) == 1


def test_relog_same_key_dedupes(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("split_normalization", "adj BKNG", "BKNG", context="first"), store)
    _, created = log_deferred(
        _task("split_normalization", "adj BKNG", "BKNG", context="second"), store
    )
    assert created is False
    open_tasks = list_tasks(store)
    assert len(open_tasks) == 1
    # context refreshed to the newer value
    assert open_tasks[0].context == "second"


def test_ticker_is_part_of_key(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("split_normalization", "adj", "BKNG"), store)
    log_deferred(_task("split_normalization", "adj", "AZO"), store)
    # same area+task but different ticker -> two distinct rows
    assert len(list_tasks(store)) == 2


def test_none_ticker_dedupes_against_none(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("auth", "re-auth key", None), store)
    _, created = log_deferred(_task("auth", "re-auth key", None), store)
    assert created is False
    assert len(list_tasks(store)) == 1


def test_mark_done_filters_out_of_open(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("auth", "re-auth key", None), store)
    assert mark_done("auth", "re-auth key", None, store) is True
    assert list_tasks(store, status=DeferredStatus.OPEN) == []
    assert len(list_tasks(store, status=None)) == 1


def test_mark_done_no_match_returns_false(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("auth", "re-auth key", None), store)
    assert mark_done("auth", "nonexistent", None, store) is False


def test_done_row_not_reopened_by_done_relog(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("auth", "re-auth key", None), store)
    mark_done("auth", "re-auth key", None, store)
    # re-logging (default OPEN status) reopens a regressed fix
    _, created = log_deferred(_task("auth", "re-auth key", None), store)
    assert created is False
    assert len(list_tasks(store, status=DeferredStatus.OPEN)) == 1


def test_persistence_round_trip(tmp_path: Path) -> None:
    store = tmp_path / "d.jsonl"
    log_deferred(_task("consensus_cache", "re-pull BKNG", "BKNG", context="ctx"), store)
    # fresh read via list reconstructs the model faithfully
    reread = list_tasks(store, status=None)[0]
    assert reread.area == "consensus_cache"
    assert reread.ticker == "BKNG"
    assert reread.context == "ctx"
    assert reread.blocked_on == "fmp_splits_feed"


def test_empty_store_lists_empty(tmp_path: Path) -> None:
    store = tmp_path / "does_not_exist.jsonl"
    assert list_tasks(store) == []
