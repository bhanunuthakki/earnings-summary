"""Producer-side pruning of earlier pytest temp runs whose owner is gone."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from src.operations import storage_reservation as storage
from src.operations import temp_run_retention as retention

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
Status = Literal["active", "failed", "completed"]


def _run(base: Path, name: str, status: Status | None, age: timedelta) -> Path:
    """A synthetic run folder; ``status=None`` leaves it without a manifest."""
    root = base / name
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "payload.db").write_bytes(b"x")
    stamp = NOW - age
    if status is None:
        for path in (root / "fixtures" / "payload.db", root / "fixtures", root):
            os.utime(path, (stamp.timestamp(), stamp.timestamp()))
        return root
    manifest = retention.TempRunManifest(
        run_id=f"run-{name}",
        run_root=root,
        started_at=stamp - timedelta(minutes=5),
        completed_at=None if status == "active" else stamp,
        status=status,
        disposable_class="isolated_test_fixture",
        purpose="pytest synthetic fixtures",
    )
    (root / retention.MANIFEST_NAME).write_text(manifest.model_dump_json(), encoding="utf-8")
    return root


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "system-temp"
    root.mkdir()
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger"


def _prune(base: Path, ledger: Path, *, apply: bool = True) -> retention.TempRunPruneResult:
    return retention.prune_abandoned_test_runs(temp_root=base, store=ledger, now=NOW, apply=apply)


@pytest.mark.parametrize(
    ("name", "status", "age", "removed"),
    [
        ("earnings-summary-pytest-controller-done2d", "completed", timedelta(days=2), True),
        ("earnings-summary-pytest-gw0-done2h", "completed", timedelta(hours=2), False),
        ("earnings-summary-pytest-controller-fail8d", "failed", timedelta(days=8), True),
        ("earnings-summary-pytest-gw1-fail3d", "failed", timedelta(days=3), False),
        ("earnings-summary-pytest-controller-crash8d", "active", timedelta(days=8), True),
        ("earnings-summary-pytest-controller-crash3d", "active", timedelta(days=3), False),
        ("earnings-summary-pytest-controller-bare2d", None, timedelta(days=2), True),
        ("earnings-summary-pytest-gw3-bare2h", None, timedelta(hours=2), False),
    ],
)
def test_prunes_owner_gone_runs_only_after_their_age(
    base: Path, ledger: Path, name: str, status: Status | None, age: timedelta, removed: bool
) -> None:
    root = _run(base, name, status, age)
    result = _prune(base, ledger)
    assert root.exists() is not removed
    assert (root in result.removed) is removed


def test_recent_file_inside_a_bare_folder_keeps_it(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-bareold", None, timedelta(days=3))
    (root / "fixtures" / "fresh.db").write_bytes(b"y")
    stamp = (NOW - timedelta(minutes=10)).timestamp()
    os.utime(root / "fixtures" / "fresh.db", (stamp, stamp))
    _prune(base, ledger)
    assert root.exists()


def test_live_lease_keeps_even_an_old_run(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-leased", "active", timedelta(days=30))
    token = storage.reserve_peak(root, 1, host_limit=10, reserve_bytes=0, store=ledger)
    try:
        result = _prune(base, ledger)
        assert root.exists()
        assert root not in result.removed
    finally:
        storage.release_peak(root, token, store=ledger)


def test_never_touches_shared_secrets_links_unknown_names_or_bad_manifests(
    base: Path, ledger: Path, tmp_path: Path
) -> None:
    old = timedelta(days=30)
    secrets = _run(base, "earnings-summary-pytest-secrets", None, old)
    worker_secrets = _run(base, "earnings-summary-pytest-secrets-gw0", None, old)
    unrelated = _run(base, "earnings-summary-other-controller-x", None, old)
    target = tmp_path / "elsewhere"
    (target / "keep").mkdir(parents=True)
    link = base / "earnings-summary-pytest-controller-linked"
    link.symlink_to(target, target_is_directory=True)
    corrupt = _run(base, "earnings-summary-pytest-controller-corrupt", None, old)
    (corrupt / retention.MANIFEST_NAME).write_text("not json", encoding="utf-8")
    stamp = (NOW - old).timestamp()
    os.utime(corrupt / retention.MANIFEST_NAME, (stamp, stamp))

    result = _prune(base, ledger)

    for kept in (secrets, worker_secrets, unrelated, corrupt):
        assert kept.exists()
    assert link.is_symlink() and (target / "keep").is_dir()
    assert result.removed == ()


def test_preview_reports_without_removing(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-done9d", "completed", timedelta(days=9))
    result = _prune(base, ledger, apply=False)
    assert result.removed == (root,)
    assert root.exists()


def test_unreadable_lease_ledger_stops_pruning(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-done9d", "completed", timedelta(days=9))
    ledger.mkdir()
    (ledger / f"{'a' * 32}.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _prune(base, ledger)
    assert root.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX TMPDIR selects the system temp root")
def test_session_start_prunes_earlier_runs_from_the_controller(tmp_path: Path) -> None:
    """Importing the harness as an xdist controller prunes an old abandoned run in
    its own (isolated) system temp, and keeps the run it just claimed."""
    system_temp = (tmp_path / "tmp").resolve()
    system_temp.mkdir()
    old = system_temp / "earnings-summary-pytest-controller-ancient"
    (old / "fixtures").mkdir(parents=True)
    stamp = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    for path in (old / "fixtures", old):
        os.utime(path, (stamp, stamp))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_XDIST_WORKER", "GITHUB_ACTIONS", "EARNINGS_SUMMARY_DB_PATH"}
        and not key.startswith("_EARNINGS_SUMMARY_PYTEST")
    }
    environment.update(
        {
            "TMPDIR": str(system_temp),
            "PYTHONPATH": os.pathsep.join((str(PROJECT_ROOT), str(PROJECT_ROOT / "src"))),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import tests.conftest"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert not old.exists()
    claimed = [path for path in system_temp.glob("earnings-summary-pytest-controller-*")]
    assert len(claimed) == 1 and (claimed[0] / retention.MANIFEST_NAME).exists()


def test_failed_run_payload_is_trimmed_after_grace_and_evidence_kept(
    base: Path, ledger: Path, tmp_path: Path
) -> None:
    """A failed run whose owner is gone keeps only review evidence after an hour:
    a full-suite failure otherwise held tens of gigabytes for the whole week."""
    root = _run(base, "earnings-summary-pytest-controller-failpay", "failed", timedelta(hours=2))
    (root / "fixtures" / "junit.xml").write_text("<testsuite/>", encoding="utf-8")
    (root / "session.log").write_text("FAILED", encoding="utf-8")
    (root / "fixtures" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    receipts = root / ".earnings-temp-retirement"
    receipts.mkdir()
    (receipts / "receipt.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"keep")
    (root / "fixtures" / "linked.db").symlink_to(outside)

    result = _prune(base, ledger)

    assert root.exists() and root not in result.removed
    assert result.trimmed == (root,)
    assert result.trimmed_bytes >= 1
    assert not (root / "fixtures" / "payload.db").exists()
    assert not (root / "fixtures" / "linked.db").is_symlink()
    assert outside.read_bytes() == b"keep"
    for kept in (
        root / retention.MANIFEST_NAME,
        root / "fixtures" / "junit.xml",
        root / "session.log",
        root / "fixtures" / "events.jsonl",
        receipts / "receipt.json",
    ):
        assert kept.exists(), kept


@pytest.mark.parametrize(
    ("status", "age"), [("failed", timedelta(minutes=30)), ("active", timedelta(minutes=20))]
)
def test_recent_failed_or_crashed_payload_waits_for_the_grace_period(
    base: Path, ledger: Path, status: Status, age: timedelta
) -> None:
    root = _run(base, f"earnings-summary-pytest-controller-{status}recent", status, age)
    result = _prune(base, ledger)
    assert (root / "fixtures" / "payload.db").exists()
    assert result.trimmed == ()


def test_crashed_active_run_payload_is_trimmed(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-crash2h", "active", timedelta(hours=2))
    result = _prune(base, ledger)
    assert result.trimmed == (root,)
    assert not (root / "fixtures" / "payload.db").exists()
    assert (root / retention.MANIFEST_NAME).exists()


def test_live_lease_run_payload_is_never_trimmed(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-leasedpay", "failed", timedelta(days=2))
    token = storage.reserve_peak(root, 1, host_limit=10, reserve_bytes=0, store=ledger)
    try:
        result = _prune(base, ledger)
        assert result.trimmed == ()
        assert (root / "fixtures" / "payload.db").exists()
    finally:
        storage.release_peak(root, token, store=ledger)


def test_trim_preview_reports_without_deleting(base: Path, ledger: Path) -> None:
    root = _run(base, "earnings-summary-pytest-controller-failprev", "failed", timedelta(hours=3))
    result = _prune(base, ledger, apply=False)
    assert result.trimmed == (root,)
    assert (root / "fixtures" / "payload.db").exists()


def test_future_timestamp_does_not_keep_a_bare_folder_forever(base: Path, ledger: Path) -> None:
    """Some tests stamp fixtures years ahead; that is not a recent change."""
    root = _run(base, "earnings-summary-pytest-controller-futurebare", None, timedelta(days=3))
    future = root / "fixtures" / "future.db"
    future.write_bytes(b"x")
    ahead = (NOW + timedelta(days=6 * 365)).timestamp()
    os.utime(future, (ahead, ahead))
    stamp = (NOW - timedelta(days=3)).timestamp()
    for path in (root / "fixtures", root):
        os.utime(path, (stamp, stamp))
    result = _prune(base, ledger)
    assert root in result.removed and not root.exists()
