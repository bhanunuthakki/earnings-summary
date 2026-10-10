"""Long lock keys retain single-flight ownership without oversized filenames."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

import runtime.job_runtime as runtime
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    allow_nested_job_locks,
    current_lock_claim,
    inherited_lock_is_valid,
)

ROOT = Path(__file__).resolve().parents[1]


def _held_path(lock: JobLock, write_set: str) -> Path:
    value: object = json.loads(lock.inheritance_proof())
    assert isinstance(value, dict)
    proof = cast(dict[str, object], value)
    value = proof[write_set]
    assert isinstance(value, dict)
    entry = cast(dict[str, object], value)
    path = entry["path"]
    assert isinstance(path, str)
    return Path(path)


@pytest.mark.parametrize("kind", ["ascii", "unicode"])
def test_long_artifact_write_set_acquires_contends_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    state = tmp_path / ("a" * 70) / ("b" * 70) / ("研究" * 20 if kind == "unicode" else "c" * 70)
    first = f"artifact:{state}/request-one/http-control"
    second = f"artifact:{state}/request-two/http-control"
    with JobLock(state, "holder", [first], wait_s=0) as owner:
        path = _held_path(owner, first)
        assert len(path.name.encode("utf-8")) <= 255 and path.suffix == ".lock"
        original = path.read_bytes()
        claim = current_lock_claim(state, first)
        assert claim is not None
        with allow_nested_job_locks(), JobLock(state, "borrower", [first], wait_s=0):
            assert path.read_bytes() == original
            assert current_lock_claim(state, first) == claim
        assert path.read_bytes() == original
        with JobLock(state, "distinct", [second], wait_s=0) as other:
            other_path = _held_path(other, second)
            assert other_path != path and other_path.exists()
        assert not other_path.exists() and path.read_bytes() == original
        # The real child proves inheritance and then independently attempts acquisition.
        script = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                "from runtime.job_runtime import JobLock, JobAlreadyRunningError, inherited_lock_is_valid",
                "root, key = Path(sys.argv[1]), sys.argv[2]",
                "assert inherited_lock_is_valid(root, key)",
                "os.environ.pop('EARNINGS_SUMMARY_JOB_LOCK_PROOF')",
                "try:",
                "    with JobLock(root, 'contender', [key], wait_s=0): raise AssertionError('concurrent ownership')",
                "except JobAlreadyRunningError: pass",
            )
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(state), first],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "EARNINGS_SUMMARY_JOB_LOCK_PROOF": owner.inheritance_proof(),
            },
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == original
        proof = owner.inheritance_proof()
    assert not path.exists() and current_lock_claim(state, first) is None
    monkeypatch.setenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", proof)
    assert not inherited_lock_is_valid(state, first)
    with JobLock(state, "successor", [first], wait_s=0) as successor:
        assert _held_path(successor, first) == path
        assert current_lock_claim(state, first) != claim
    assert not path.exists()
    assert not tuple((state / ".tmp/job_locks").glob("*.lock"))
    guards = tuple((state / ".tmp/job_locks").glob("*.guard"))
    if os.name == "posix":
        assert guards and all(len(p.name.encode("utf-8")) <= 255 for p in guards)


@pytest.mark.skipif(os.name != "posix", reason="POSIX persistent transition-guard files")
@pytest.mark.parametrize("size", [249, 250, 255])
def test_valid_lock_filename_stays_exact_when_only_guard_is_overlong(
    tmp_path: Path, size: int
) -> None:
    key = "a" * (size - len(".lock"))
    expected = tmp_path / ".tmp/job_locks" / (key + ".lock")
    with JobLock(tmp_path, "boundary", [key], wait_s=0) as lock:
        assert _held_path(lock, key) == expected and expected.exists()
        guards = tuple(expected.parent.glob("*.guard"))
        assert len(guards) == 1
        assert len(guards[0].name.encode("utf-8")) <= 255
        if size == 249:
            assert guards[0].name == expected.name + ".guard"
        else:
            assert guards[0].name != expected.name + ".guard"
    assert not expected.exists()


def test_unicode_database_names_keep_whole_identity_and_cross_checkout_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "研究"
    first_db = shared / ("é" * 119 + "A.db")
    second_db = shared / ("é" * 119 + "B.db")
    first, alias, second = (tmp_path / name for name in ("first", "alias", "second"))
    assert len(first_db.name.encode("utf-8")) < 255
    assert len((first_db.name + ".sec-companyfacts.lock").encode("utf-8")) > 255

    def configured_database(root: Path) -> Path:
        return second_db if root == second else first_db

    monkeypatch.setattr(runtime, "portfolio_db_path", configured_database)
    with JobLock(first, "first", ["sec-companyfacts"], wait_s=0) as held:
        path = _held_path(held, "sec-companyfacts")
        assert path.parent == shared and path.suffix == ".lock"
        with (
            pytest.raises(JobAlreadyRunningError),
            JobLock(alias, "alias", ["sec-companyfacts"], wait_s=0),
        ):
            pytest.fail("same canonical database must contend across checkouts")
        with JobLock(second, "second", ["sec-companyfacts"], wait_s=0) as distinct:
            other = _held_path(distinct, "sec-companyfacts")
            assert other != path and other.parent == shared
            assert other.exists() and path.exists()
    assert not path.exists() and not other.exists()
    assert not first_db.exists() and not second_db.exists()
    assert not tuple(shared.glob("*.lock"))
    assert all(len(p.name.encode("utf-8")) <= 255 for p in shared.iterdir())


def test_existing_short_lock_paths_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "synthetic" / "short.db"

    def configured_database(_root: Path) -> Path:
        return database

    monkeypatch.setattr(runtime, "portfolio_db_path", configured_database)
    for key, expected in (
        ("unit-lane", tmp_path / ".tmp/job_locks/unit-lane.lock"),
        ("artifact:/a/b", tmp_path / ".tmp/job_locks/artifact-a-b.lock"),
        ("sec-companyfacts", database.with_name("short.db.sec-companyfacts.lock")),
        ("portfolio-db", database.with_name("short.db.write.lock")),
    ):
        with JobLock(tmp_path, "short", [key], wait_s=0) as held:
            assert _held_path(held, key) == expected
            if os.name == "posix":
                assert expected.with_name(expected.name + ".guard").exists()
        assert not expected.exists()
