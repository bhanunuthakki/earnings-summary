# pyright: reportPrivateUsage=false
# Tests intentionally reach for the module's private helpers (_path_lock,
# _store_path) to exercise the concurrency primitives directly.
"""Concurrent mutators on the same comments file no longer lose updates.

The Flask dev server runs with `threaded=True` (see comments_server.py),
so two POST /comments hitting the same (ticker, report_date) can race
their load-modify-save sequences. Without the per-file lock added in
Fix 4, the second writer overwrites the first writer's update and that
comment is silently lost.

This test exercises the real concurrency by spawning N threads each
calling `append_comment` and asserts the final on-disk store contains
exactly N entries. With the prior (lock-less) implementation, this
test reliably loses some on contended runs.

Also covers atomic-write semantics: a save that crashes mid-write must
leave either the prior file or a clean replacement, never a partial.
The tempfile-then-os.replace pattern guarantees this.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest

import comments


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Empty repo layout — comments.py creates the report_comments dir."""
    (tmp_path / "data").mkdir()
    return tmp_path


def _anchor() -> comments.Anchor:
    return comments.Anchor(type="kpi_ledger_row", key="revenue")


# ---------------------------------------------------------------------------
# Concurrent append — N threads, one comment each, exactly N comments end up
# on disk.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_threads", [10, 25])
def test_concurrent_appends_lose_no_comments(repo_root: Path, n_threads: int) -> None:
    """The bug this fix exists to kill — concurrent writers used to overwrite
    each other when they raced on the same (ticker, report_date)."""
    ticker, rdate = "AMZN", date(2026, 6, 4)
    barrier = Barrier(n_threads)

    def writer(i: int) -> str:
        # All threads release together so they actually race the critical
        # section, not run serialised by Python's GIL+IO scheduling.
        barrier.wait(timeout=5)
        c = comments.append_comment(
            repo_root,
            ticker,
            rdate,
            anchor=_anchor(),
            text=f"thread-{i:03d}",
        )
        return c.id

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(writer, i) for i in range(n_threads)]
        ids = [f.result(timeout=10) for f in as_completed(futures)]

    # Every writer got a unique id back.
    assert len(set(ids)) == n_threads

    # And every comment landed on disk — the real assertion.
    final = comments.load_store(repo_root, ticker, rdate)
    assert len(final.comments) == n_threads, (
        f"lost {n_threads - len(final.comments)} comments under contention"
    )
    bodies = sorted(c.comment for c in final.comments)
    assert bodies == sorted(f"thread-{i:03d}" for i in range(n_threads))


# ---------------------------------------------------------------------------
# Concurrent update — N threads each PATCH-ing the same comment must each
# observe + transform the latest state, not stomp each other.
# ---------------------------------------------------------------------------


def test_concurrent_updates_apply_in_sequence(repo_root: Path) -> None:
    ticker, rdate = "AMZN", date(2026, 6, 4)
    c = comments.append_comment(
        repo_root,
        ticker,
        rdate,
        anchor=_anchor(),
        text="seed",
    )

    n_threads = 10
    barrier = Barrier(n_threads)

    def updater(i: int) -> None:
        barrier.wait(timeout=5)
        comments.update_comment(
            repo_root,
            ticker,
            rdate,
            c.id,
            append_thread=comments.ThreadEntry(role="user", text=f"turn-{i}"),
        )

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(updater, range(n_threads)))

    final = comments.load_store(repo_root, ticker, rdate)
    assert len(final.comments) == 1
    only = final.comments[0]
    # Every concurrent append_thread must be preserved — without the lock,
    # later writers overwrite the load-time snapshot and earlier turns are
    # lost.
    assert len(only.follow_up_thread) == n_threads
    texts = sorted(t.text for t in only.follow_up_thread)
    assert texts == sorted(f"turn-{i}" for i in range(n_threads))


# ---------------------------------------------------------------------------
# Different stores don't serialise — Brazil customers and Mexico customers
# both writing means no avoidable contention.
# ---------------------------------------------------------------------------


def test_different_stores_get_different_locks(repo_root: Path) -> None:
    a = comments._path_lock(comments._store_path(repo_root, "AMZN", date(2026, 6, 4)))
    b = comments._path_lock(comments._store_path(repo_root, "META", date(2026, 6, 4)))
    c = comments._path_lock(comments._store_path(repo_root, "AMZN", date(2026, 6, 4)))
    assert a is not b, "different (ticker, date) must not share a lock"
    assert a is c, "same (ticker, date) must return the same lock"


# ---------------------------------------------------------------------------
# Atomic write — a crash mid-write leaves the prior file intact, no
# half-written JSON readable by a concurrent reader.
# ---------------------------------------------------------------------------


def test_partial_write_failure_leaves_prior_store_intact(repo_root: Path) -> None:
    ticker, rdate = "AMZN", date(2026, 6, 4)
    # Seed one good comment.
    seed = comments.append_comment(
        repo_root,
        ticker,
        rdate,
        anchor=_anchor(),
        text="seed",
    )
    path = comments._store_path(repo_root, ticker, rdate)
    before = path.read_text(encoding="utf-8")

    # Force a write failure inside save_store. With path.write_text the prior
    # file could be partially truncated; with tempfile + os.replace, the
    # tempfile is the casualty and the real path stays untouched.
    real_replace = comments.os.replace

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic write failure")

    with patch.object(comments.os, "replace", boom), pytest.raises(OSError):
        comments.append_comment(
            repo_root,
            ticker,
            rdate,
            anchor=_anchor(),
            text="lost",
        )

    # Prior store byte-identical — no partial write, no truncation.
    assert path.read_text(encoding="utf-8") == before
    # And readable as the seed-only store.
    final = comments.load_store(repo_root, ticker, rdate)
    assert [c.id for c in final.comments] == [seed.id]

    # No leftover tempfile littering the dir.
    leftovers = list(path.parent.glob("*.tmp"))
    assert leftovers == [], f"leaked tempfiles: {leftovers}"
    # Sanity — real os.replace is still available (the patch was scoped).
    assert comments.os.replace is real_replace


# ---------------------------------------------------------------------------
# Lock context manager release semantics — exceptions must release.
# ---------------------------------------------------------------------------


def test_lock_releases_on_exception(repo_root: Path) -> None:
    ticker, rdate = "AMZN", date(2026, 6, 4)
    # Seed so subsequent operations don't no-op.
    comments.append_comment(repo_root, ticker, rdate, anchor=_anchor(), text="seed")

    # First, take the lock via the context manager and raise inside it.
    with pytest.raises(RuntimeError), comments.store_lock(repo_root, ticker, rdate):
        raise RuntimeError("inside lock")

    # Now a follow-up append must succeed — if the prior exception had
    # leaked the lock, this would deadlock the test (the timeout makes
    # the failure mode visible instead of hanging forever).
    c = comments.append_comment(
        repo_root,
        ticker,
        rdate,
        anchor=_anchor(),
        text="follow",
    )
    assert c.comment == "follow"


# ---------------------------------------------------------------------------
# Sanity — ordinary single-threaded API surface is unchanged.
# ---------------------------------------------------------------------------


def test_basic_append_load_roundtrip(repo_root: Path) -> None:
    ticker, rdate = "AMZN", date(2026, 6, 4)
    c = comments.append_comment(
        repo_root,
        ticker,
        rdate,
        anchor=_anchor(),
        text="hello",
    )
    listed: Iterable[comments.Comment] = comments.list_comments(repo_root, ticker, rdate)
    assert [x.id for x in listed] == [c.id]


# ---------------------------------------------------------------------------
# Windows reader-during-rename race — os.replace fails with PermissionError
# when another handle has the destination open. The retry loop covers it;
# without the retry, every concurrent GET racing a write produces 500s.
# This test simulates the race by mocking os.replace to fail-then-succeed.
# ---------------------------------------------------------------------------


def test_save_store_retries_on_transient_replace_error(repo_root: Path) -> None:
    ticker, rdate = "AMZN", date(2026, 6, 4)
    comments.append_comment(repo_root, ticker, rdate, anchor=_anchor(), text="seed")

    real_replace = comments.os.replace
    attempts = {"n": 0}

    def flaky_replace(src: object, dst: object) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 3:  # fail 3 times, succeed on the 4th
            raise PermissionError("[WinError 5] synthetic")
        real_replace(src, dst)

    with patch.object(comments.os, "replace", flaky_replace):
        c = comments.append_comment(
            repo_root,
            ticker,
            rdate,
            anchor=_anchor(),
            text="after-retry",
        )

    # Comment landed despite the transient failures.
    assert c.comment == "after-retry"
    final = comments.load_store(repo_root, ticker, rdate)
    assert [x.comment for x in final.comments] == ["seed", "after-retry"]
    # Retry actually ran multiple times (3 failures + 1 success).
    assert attempts["n"] >= 4
