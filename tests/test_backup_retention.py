"""Failure and identity boundaries for immutable backup retirement."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime import backup_retention as retention


def _snapshot(root: Path, stamp: str, *, uploaded: bool = True, source: str = "live.db") -> Path:
    path = root / f"portfolio.db.{stamp}.gz.enc"
    path.write_bytes(stamp.encode())
    retention.record_snapshot(
        path, family="portfolio-db", source=root / source, snapshot_sha256="a" * 64
    )
    if uploaded:
        retention.mark_uploaded(path, family="portfolio-db")
    return path


def test_success_keeps_latest_verified_per_source_and_unknown_files(tmp_path: Path) -> None:
    old = _snapshot(tmp_path, "20261001")
    latest = _snapshot(tmp_path, "20261002")
    foreign_source = _snapshot(tmp_path, "20260901", source="different.db")
    unknown = tmp_path / "portfolio.db.20260801.gz.enc"
    unknown.write_bytes(b"legacy")

    assert retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1) == [old.name]
    assert not old.exists()
    assert retention.receipt_path(old).exists()
    assert latest.exists() and foreign_source.exists() and unknown.exists()
    assert retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1) == []


@pytest.mark.parametrize("damage", ["pending", "corrupt", "unclassified"])
def test_newer_failed_attempt_keeps_previous_good_set(tmp_path: Path, damage: str) -> None:
    old = _snapshot(tmp_path, "20261001")
    good = _snapshot(tmp_path, "20261002")
    pending = _snapshot(tmp_path, "20261003", uploaded=False)
    if damage == "corrupt":
        pending.write_bytes(b"broken")
    elif damage == "unclassified":
        retention.receipt_path(pending).unlink()

    assert retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1) == []
    assert all(path.exists() for path in (old, good, pending))


def test_pinned_backup_and_incomplete_database_sidecars_survive(tmp_path: Path) -> None:
    pinned = _snapshot(tmp_path, "20261001")
    sidecar_base = _snapshot(tmp_path, "20261002")
    latest = _snapshot(tmp_path, "20261003")
    receipt = retention.validated_receipt(pinned, family="portfolio-db")
    assert receipt is not None
    retention.write_receipt(pinned, receipt.model_copy(update={"recovery_pin": True}))
    Path(str(sidecar_base) + "-wal").write_bytes(b"pending WAL")

    assert retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1) == []
    assert all(path.exists() for path in (pinned, sidecar_base, latest))


def test_links_and_live_source_are_not_backup_files(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    live.write_bytes(b"live")
    with pytest.raises(ValueError, match="unsafe"):
        retention.record_snapshot(
            live, family="portfolio-db", source=live, snapshot_sha256="a" * 64
        )
    link = tmp_path / "portfolio.db.20261001.gz.enc"
    link.symlink_to(live)
    assert retention.validated_receipt(link, family="portfolio-db") is None
    assert not retention.safe_file(link)
    assert live.read_bytes() == b"live"


def test_tampered_or_missing_completion_cannot_prune(tmp_path: Path) -> None:
    _snapshot(tmp_path, "20261001", uploaded=False)
    with pytest.raises(RuntimeError, match="uploaded survivor"):
        retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1)


def test_retention_is_independent_for_each_source(tmp_path: Path) -> None:
    old_a = _snapshot(tmp_path, "20261001", source="a.db")
    old_b = _snapshot(tmp_path, "20261002", source="b.db")
    latest_a = _snapshot(tmp_path, "20261003", source="a.db")
    latest_b = _snapshot(tmp_path, "20261004", source="b.db")
    assert set(retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1)) == {
        old_a.name,
        old_b.name,
    }
    assert latest_a.exists() and latest_b.exists()


def test_changed_survivor_between_plan_and_unlink_keeps_old_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _snapshot(tmp_path, "20261001")
    latest = _snapshot(tmp_path, "20261002")
    real = retention.validated_receipt
    old_checks = 0

    def change_survivor(path: Path, *, family: str) -> retention.BackupReceipt | None:
        nonlocal old_checks
        receipt = real(path, family=family)
        if path == old:
            old_checks += 1
            if old_checks == 2:
                latest.write_bytes(b"changed during apply")
        return receipt

    monkeypatch.setattr(retention, "validated_receipt", change_survivor)
    with pytest.raises(RuntimeError, match="survivor changed"):
        retention.prune_uploaded(tmp_path, family="portfolio-db", retain=1)
    assert old.exists()
