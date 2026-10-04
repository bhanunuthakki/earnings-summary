"""Real process and thread contention for the DCF artifact write set."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import cast

import build_bank_dcf
import build_fintech_sotp
import build_holdco_sotp
import build_meli_platform_dcf
import build_nu_platform_dcf
import openpyxl
import pytest
import refresh_dcf

from dcf import artifact_promotion
from runtime.job_runtime import JobAlreadyRunningError
from tests.test_comments_server_dcf import BASE_INPUTS


@pytest.mark.parametrize(
    "model", ["bank_excess_return", "holdco_sotp", "fintech_sotp", "platform_dcf"]
)
def test_specialized_refresh_child_receives_exact_caller_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    database = tmp_path / "configured-state" / "portfolio.sqlite"
    database.parent.mkdir()
    database.touch()
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "conflicting.sqlite"))
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps({"valuation_model": model}))

    def child_boundary(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = cast("dict[str, str]", kwargs["env"])
        assert environment["EARNINGS_SUMMARY_DB_PATH"] == str(database)
        assert "EARNINGS_SUMMARY_JOB_LOCK_PROOF" in environment
        Path(environment["DCF_PROMOTE_DEST"]).write_bytes(b"admitted candidate")
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="RESULT\tNU\tdcf_runs=ok\n", stderr=""
        )

    monkeypatch.setattr(refresh_dcf.subprocess, "run", child_boundary)
    result = refresh_dcf.refresh_one("NU", tmp_path, database, valuation_year=2026)
    assert result["status"] == "ok", result
    assert not (tmp_path / "data" / "portfolio.db").exists()


@pytest.mark.parametrize(
    "model", ["bank_excess_return", "holdco_sotp", "fintech_sotp", "platform_dcf"]
)
def test_specialized_refresh_retains_committed_cleanup_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    database = tmp_path / "configured.sqlite"
    database.touch()
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps({"valuation_model": model}))

    def child_boundary(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = cast("dict[str, str]", kwargs["env"])
        Path(environment["DCF_PROMOTE_DEST"]).write_bytes(b"committed candidate")
        return subprocess.CompletedProcess(
            args=[],
            returncode=3,
            stdout="",
            stderr="COMMITTED\tNU\tdcf_committed_cleanup_failed\tretained backup\n",
        )

    monkeypatch.setattr(refresh_dcf.subprocess, "run", child_boundary)
    result = refresh_dcf.refresh_one("NU", tmp_path, database, valuation_year=2026)
    assert result["status"] == "committed_cleanup_failed", result
    assert result["recovery_required"] is True
    assert "retained backup" in str(result["cleanup_warning"])
    assert (tmp_path / "dcf" / "NU.xlsx").read_bytes() == b"committed candidate"


def test_holdco_post_commit_cleanup_preserves_matching_json_and_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = build_holdco_sotp
    database = migrated_db(tmp_path / "data" / "portfolio.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    monkeypatch.setattr(h, "REPO", tmp_path)
    monkeypatch.setattr(h, "T", "BN")
    accepted = tmp_path / "dcf" / "BN.xlsx"
    monkeypatch.setattr(h, "DEST", accepted)
    monkeypatch.setattr(h, "OWNER_INPUTS_DEST", accepted)
    assumptions = tmp_path / "data" / "dcf_assumptions" / "BN.json"
    assumptions.parent.mkdir(parents=True)
    assumptions.write_text(json.dumps({"ticker": "BN", "sotp": {"marks": {"bam_fre": 4.0}}}))

    def synthetic_price(
        _root: Path, _ticker: str, **_kwargs: object
    ) -> build_holdco_sotp.SpecializedPriceObservation:
        return h.SpecializedPriceObservation(50.0, None, "model_seed", None)

    monkeypatch.setattr(h, "resolve_specialized_price", synthetic_price)
    assert h.main() == 0
    h.build(h.Sotp(bam_fre=6.0), accepted)
    prior_workbook = accepted.read_bytes()
    candidate = accepted.with_name("BN.rebuild.synthetic.xlsx")
    monkeypatch.setattr(h, "DEST", candidate)
    monkeypatch.setenv("DCF_PROMOTE_DEST", str(accepted))
    original_unlink = Path.unlink

    def fail_backup(path: Path, missing_ok: bool = False) -> None:
        if ".rollback." in path.name:
            raise OSError("synthetic cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_backup)
    assert h.main() != 0
    output = capsys.readouterr()
    assert "dcf_committed_cleanup_failed" in output.err + output.out
    assert json.loads(assumptions.read_text())["sotp"]["marks"]["bam_fre"] == {"value": 6.0}
    workbook = openpyxl.load_workbook(accepted, data_only=False)
    try:
        assert workbook["Dashboard"]["B3"].value == 6.0
    finally:
        workbook.close()
    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            "SELECT is_latest, assumption_snapshot_json FROM dcf_runs ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == [0, 1]
    assert json.loads(rows[-1][1])["marks"]["bam_fre"] == 6.0
    backups = list(accepted.parent.glob("BN.rollback.*.xlsx"))
    assert len(backups) == 1 and backups[0].read_bytes() == prior_workbook
    assert not candidate.exists()
    assert h.main() != 0
    assert "dcf_recovery_required" in capsys.readouterr().err


def test_holdco_direct_builder_persists_to_separate_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "configured-state" / "portfolio.sqlite")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    h = build_holdco_sotp
    monkeypatch.setattr(h, "REPO", tmp_path)
    monkeypatch.setattr(h, "T", "BN")
    accepted = tmp_path / "dcf" / "BN.xlsx"
    monkeypatch.setattr(h, "DEST", accepted)
    monkeypatch.setattr(h, "OWNER_INPUTS_DEST", accepted)

    def synthetic_price(
        _root: Path, _ticker: str, **_kwargs: object
    ) -> build_holdco_sotp.SpecializedPriceObservation:
        return h.SpecializedPriceObservation(50.0, None, "model_seed", None)

    monkeypatch.setattr(h, "resolve_specialized_price", synthetic_price)
    assert h.main() == 0
    with sqlite3.connect(database) as conn:
        rows = conn.execute("SELECT ticker, is_latest FROM dcf_runs").fetchall()
    assert rows == [("BN", 1)]
    assert accepted.is_file()
    assert not (tmp_path / "data" / "portfolio.db").exists()


@pytest.mark.parametrize(
    "builder",
    [
        build_bank_dcf,
        build_holdco_sotp,
        build_fintech_sotp,
        build_nu_platform_dcf,
        build_meli_platform_dcf,
    ],
)
def test_direct_specialized_builder_requires_database_before_artifact_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: ModuleType
) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setattr(builder, "REPO", tmp_path)
    monkeypatch.setattr(builder, "T", "BN")

    def forbidden_operation() -> int:
        pytest.fail("direct builder started artifact work without a database authority")

    monkeypatch.setattr(builder, "_main_owned", forbidden_operation)
    assert builder.main() != 0
    assert not (tmp_path / "data" / "portfolio.db").exists()


def test_same_process_dcf_writers_contend(tmp_path: Path) -> None:
    def competing_writer() -> None:
        with artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="second", wait_s=0):
            pytest.fail("a second writer acquired the same ticker")

    with artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="first", wait_s=0):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(competing_writer)
            with pytest.raises(JobAlreadyRunningError):
                pending.result(timeout=5)
        with artifact_promotion.hold_dcf_artifacts(tmp_path, "NU", owner="other", wait_s=0):
            pass
    with artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="released", wait_s=0):
        pass


def test_child_inherits_only_exact_live_dcf_claim(tmp_path: Path) -> None:
    child = """
import sys
from pathlib import Path
from dcf.artifact_promotion import hold_dcf_artifacts
from runtime.job_runtime import JobAlreadyRunningError
try:
    with hold_dcf_artifacts(Path(sys.argv[1]), sys.argv[2], owner='child', wait_s=0):
        pass
except JobAlreadyRunningError:
    raise SystemExit(75)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.pop("EARNINGS_SUMMARY_JOB_LOCK_PROOF", None)
    with artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="parent", wait_s=0) as claim:
        blocked = subprocess.run(
            [sys.executable, "-c", child, str(tmp_path), "META"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert blocked.returncode == 75, blocked.stderr
        inherited = dict(env, **claim.child_environment)
        accepted = subprocess.run(
            [sys.executable, "-c", child, str(tmp_path), "META"],
            env=inherited,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert accepted.returncode == 0, accepted.stderr
        with artifact_promotion.hold_dcf_artifacts(tmp_path, "NU", owner="other", wait_s=0):
            wrong_ticker = subprocess.run(
                [sys.executable, "-c", child, str(tmp_path), "NU"],
                env=inherited,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert wrong_ticker.returncode == 75, wrong_ticker.stderr


def test_save_and_refresh_share_busy_owner(tmp_path: Path) -> None:
    with (
        artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="import", wait_s=0),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        saved = pool.submit(
            refresh_dcf.apply_edits, "META", tmp_path, tmp_path / "no.db", BASE_INPUTS
        )
        refreshed = pool.submit(
            refresh_dcf.refresh_one, "META", tmp_path, tmp_path / "no.db", valuation_year=2026
        )
        assert saved.result(timeout=5)["reason"] == "dcf_writer_busy"
        assert refreshed.result(timeout=5)["reason"] == "dcf_writer_busy"
    assert not (tmp_path / "no.db").exists()


def test_same_artifact_directory_contends_across_checkout_names(tmp_path: Path) -> None:
    actual = tmp_path / "actual" / "dcf"
    actual.mkdir(parents=True)
    alias = tmp_path / "other-checkout"
    alias.mkdir()
    try:
        (alias / "dcf").symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create the synthetic directory symlink")
    with (
        artifact_promotion.hold_dcf_artifacts(actual.parent, "META", owner="first", wait_s=0),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):

        def contender() -> None:
            with artifact_promotion.hold_dcf_artifacts(alias, "META", owner="alias", wait_s=0):
                pytest.fail("an alias checkout admitted a second writer")

        pending = pool.submit(contender)
        with pytest.raises(JobAlreadyRunningError):
            pending.result(timeout=5)


def test_dead_child_claim_can_be_recovered(tmp_path: Path) -> None:
    child = """
import os, sys
from pathlib import Path
from dcf.artifact_promotion import hold_dcf_artifacts
with hold_dcf_artifacts(Path(sys.argv[1]), 'META', owner='crashed', wait_s=0):
    os._exit(7)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("EARNINGS_SUMMARY_JOB_LOCK_PROOF", None)
    proc = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 7, proc.stderr
    with artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="recovery", wait_s=0):
        pass


def test_unresolved_recovery_refuses_new_writer_and_retains_bytes(tmp_path: Path) -> None:
    directory = tmp_path / "dcf"
    directory.mkdir()
    backup = directory / "META.rollback.failed.xlsx"
    backup.write_bytes(b"original accepted workbook")
    with (
        pytest.raises(artifact_promotion.DcfRecoveryError, match="dcf_recovery_required"),
        artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="new", wait_s=0),
    ):
        pytest.fail("a new writer entered unresolved recovery")
    assert backup.read_bytes() == b"original accepted workbook"


def test_exception_cleans_only_owned_candidates(tmp_path: Path) -> None:
    foreign = tmp_path / "dcf" / "META.edit.foreign.xlsx"
    foreign.parent.mkdir()
    foreign.write_bytes(b"another attempt")
    candidate: Path | None = None
    with (
        pytest.raises(ValueError, match="synthetic failure"),
        artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="attempt", wait_s=0),
    ):
        candidate = artifact_promotion.unique_staged_path(foreign.parent / "META.xlsx", "edit")
        candidate.write_bytes(b"owned candidate")
        raise ValueError("synthetic failure")
    assert candidate is not None and not candidate.exists()
    assert foreign.read_bytes() == b"another attempt"


def test_committed_backup_cleanup_failure_is_explicit_and_blocks_next_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "dcf" / "META.xlsx"
    live.parent.mkdir()
    live.write_bytes(b"accepted original")
    staged = live.with_name("candidate.xlsx")
    staged.write_bytes(b"committed candidate")
    original_unlink = Path.unlink

    def fail_backup_unlink(path: Path, missing_ok: bool = False) -> None:
        if ".rollback." in path.name:
            raise OSError("synthetic backup cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
    with (
        pytest.raises(artifact_promotion.DcfRecoveryError, match="dcf_committed_cleanup_failed"),
        artifact_promotion.StagedArtifactBundle([(staged, live)]),
    ):
        pass
    assert live.read_bytes() == b"committed candidate"
    backups = list(live.parent.glob("META.rollback.*.xlsx"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"accepted original"
    with (
        pytest.raises(artifact_promotion.DcfRecoveryError, match="dcf_recovery_required"),
        artifact_promotion.hold_dcf_artifacts(tmp_path, "META", owner="next", wait_s=0),
    ):
        pytest.fail("cleanup-required publication admitted another writer")


@pytest.mark.parametrize(
    ("builder", "first_read"),
    [
        (build_bank_dcf, "load_assumptions"),
        (build_holdco_sotp, "_run_bn"),
        (build_fintech_sotp, "_load"),
        (build_nu_platform_dcf, "load_assumptions"),
        (build_meli_platform_dcf, "promotion_from_env"),
    ],
)
def test_direct_specialized_builder_contends_before_reading_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: ModuleType, first_read: str
) -> None:
    monkeypatch.setattr(builder, "REPO", tmp_path)
    monkeypatch.setattr(builder, "T", "SYNTHETIC")

    def forbidden_read(*_args: object, **_kwargs: object) -> None:
        pytest.fail("busy specialized entrypoint read mutable inputs")

    monkeypatch.setattr(builder, first_read, forbidden_read)
    with (
        artifact_promotion.hold_dcf_artifacts(tmp_path, "SYNTHETIC", owner="save", wait_s=0),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        result = pool.submit(builder.main).result(timeout=5)
    assert result == 75
