# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""Focused CLI-boundary coverage for scheduled pipeline single-flight.

These tests deliberately exercise private fingerprint seams and dynamically
replace expensive scheduled-job dependencies with bounded test doubles.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from models.runs import StageStatus
from pipeline.run_accounting import PipelineRunSuppressedError


class _Conn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        return None


def _fail_if_called(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("expensive work ran after pipeline suppression")


def _empty_transcript_preflight(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {}


@pytest.mark.parametrize(
    ("module_name", "expensive_name"),
    [
        ("execution.check_comp_set_drift", "compute_drift"),
        ("execution.fetch_sec_xbrl", "ingest_for_ticker"),
        ("execution.quarterly_refresh", "refresh_portfolio"),
        ("execution.run_validation_engine", "run_all_checks"),
        ("execution.run_weekly_validation", "apply_confidence_scores"),
        ("execution.track_comp_metrics", "compute_comparable_set_metrics"),
    ],
)
@pytest.mark.parametrize(
    ("run_status", "output_status"),
    [
        (StageStatus.IN_PROGRESS, "already_running"),
        (StageStatus.OK, "already_done"),
    ],
)
def test_scheduled_cli_suppression_is_a_successful_terminal_noop(
    module_name: str,
    expensive_name: str,
    run_status: StageStatus,
    output_status: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module(module_name)
    conn = _Conn()

    monkeypatch.setattr(module, "open_db", lambda _db: conn)
    if hasattr(module, "_resolve_tickers"):
        monkeypatch.setattr(module, "_resolve_tickers", lambda *_args: ["NU"])
    if module_name == "execution.check_comp_set_drift":
        monkeypatch.setattr(module, "load_fmp_pe_snapshot", lambda *_args: [])
        monkeypatch.setattr(module, "_drift_input_fingerprint", lambda *_args, **_kwargs: "fp")
    if module_name == "execution.track_comp_metrics":
        monkeypatch.setattr(
            module,
            "_metric_input_fingerprint",
            lambda *_args, **_kwargs: ({"NU": None}, {}, "selection", "sources", 0),
        )
    if module_name == "execution.quarterly_refresh":
        monkeypatch.setattr(
            module,
            "stage_pending_issuer_transcripts",
            _empty_transcript_preflight,
        )
    monkeypatch.setattr(module, expensive_name, _fail_if_called)

    def _suppress(*_args: object, **_kwargs: object) -> str:
        raise PipelineRunSuppressedError("pipeline_key", "attempt_id", run_status)

    monkeypatch.setattr(module, "start_run", _suppress)
    monkeypatch.setattr(sys, "argv", ["scheduled-job"])

    assert module.main() == 0
    assert conn.closed is True
    assert json.loads(capsys.readouterr().out) == {
        "status": output_status,
        "pipeline_key": "pipeline_key",
        "attempt_id": "attempt_id",
    }


def test_track_comp_metrics_fingerprints_material_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import track_comp_metrics as module

    conn = _Conn()
    captured: dict[str, object] = {}
    repo_root = tmp_path / "repo"

    monkeypatch.setattr(module, "open_db", lambda _db: conn)
    monkeypatch.setattr(module, "_resolve_tickers", lambda *_args: ["NU"])
    monkeypatch.setattr(
        module,
        "_metric_input_fingerprint",
        lambda *_args, **_kwargs: (
            {"NU": None},
            {},
            "selection_sha",
            "source_sha",
            12,
        ),
    )

    def _capture(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        raise PipelineRunSuppressedError("pipeline_key", "attempt_id", StageStatus.IN_PROGRESS)

    monkeypatch.setattr(module, "start_run", _capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scheduled-job",
            "--date",
            "2026-07-28",
            "--repo-root",
            str(repo_root),
        ],
    )

    assert module.main() == 0
    assert captured["invocation_inputs"] == {
        "as_of": "2026-07-28",
        "include_pool_scopes": True,
        "method_version": module.METHOD_VERSION,
        "repo_root": str(repo_root.resolve()),
        "selection_fingerprint": "selection_sha",
        "source_file_count": 12,
        "source_files_fingerprint": "source_sha",
    }
    assert captured["deduplicate_completed"] is True


def test_source_file_budget_covers_pool_wide_slice_scope() -> None:
    """Phase 2 (§6) fingerprints every US-listed pool member's cache files.

    The Phase-1-era 5,000 ceiling silently became unreachable-by-design once
    pool-wide industry/sector slices landed: the pool is ~2,000 names x 4
    suffixes, so every no-``--ticker`` run raised before ``start_run`` and the
    daily job died with no pipeline-run row. Pin the budget to the scope it
    actually has to cover.
    """
    from execution import track_comp_metrics as module

    us_listed_pool_ceiling = 2_000
    required = us_listed_pool_ceiling * len(module._MEMBER_SOURCE_SUFFIXES)
    assert required <= module._MAX_FINGERPRINT_FILES


def test_source_file_budget_breach_reports_the_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breach must name actual-vs-limit; a bare message costs a DB audit."""
    from execution import track_comp_metrics as module

    monkeypatch.setattr(module, "_MAX_FINGERPRINT_FILES", 4)
    with pytest.raises(ValueError) as excinfo:
        module._source_files_fingerprint(tmp_path, {"NU", "MELI"})

    message = str(excinfo.value)
    assert "8 files from 2 tickers > 4" in message


def test_drift_fingerprint_changes_with_selected_rows() -> None:
    from execution import check_comp_set_drift as module

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE comp_set_metrics_daily ("
        "scope_type TEXT, scope_key TEXT, as_of_date TEXT, metric TEXT, "
        "stat_type TEXT, value REAL, n_members INTEGER, n_valid INTEGER, "
        "method_version INTEGER, method_flags TEXT)"
    )
    conn.execute(
        "INSERT INTO comp_set_metrics_daily VALUES "
        "('industry', 'Banks', '2026-07-27', 'pe_ttm', 'median', "
        "12.0, 8, 7, ?, '{}')",
        (module.METHOD_VERSION,),
    )
    entries = {
        "industry": [
            module.SnapshotEntry(
                kind="industry",
                scope_key="Banks",
                as_of=date(2026, 7, 26),
                pe=10.0,
                exchanges=("NASDAQ",),
            )
        ]
    }

    original = module._drift_input_fingerprint(
        conn,
        scopes=["industry"],
        as_of=date(2026, 7, 28),
        entries_by_scope=entries,
    )
    changed_snapshot = module._drift_input_fingerprint(
        conn,
        scopes=["industry"],
        as_of=date(2026, 7, 28),
        entries_by_scope={
            "industry": [
                module.SnapshotEntry(
                    kind="industry",
                    scope_key="Banks",
                    as_of=date(2026, 7, 26),
                    pe=11.0,
                    exchanges=("NASDAQ",),
                )
            ]
        },
    )
    conn.execute("UPDATE comp_set_metrics_daily SET n_valid = 6 WHERE scope_type = 'industry'")
    changed_bottoms_up = module._drift_input_fingerprint(
        conn,
        scopes=["industry"],
        as_of=date(2026, 7, 28),
        entries_by_scope=entries,
    )

    assert original != changed_snapshot
    assert original != changed_bottoms_up
    conn.close()


def test_drift_cli_deduplicates_only_with_complete_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import check_comp_set_drift as module

    captured: dict[str, object] = {}
    monkeypatch.setattr(module, "open_db", lambda _db: _Conn())
    monkeypatch.setattr(module, "load_fmp_pe_snapshot", lambda *_args: [])
    monkeypatch.setattr(
        module,
        "_drift_input_fingerprint",
        lambda *_args, **_kwargs: "drift_sha",
    )

    def _capture(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        raise PipelineRunSuppressedError("pipeline_key", "attempt_id", StageStatus.IN_PROGRESS)

    monkeypatch.setattr(module, "start_run", _capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scheduled-job",
            "--date",
            "2026-07-28",
            "--scope",
            "industry",
            "--repo-root",
            str(tmp_path),
        ],
    )

    assert module.main() == 0
    assert captured["invocation_inputs"] == {
        "as_of": "2026-07-28",
        "drift_alert_threshold": module.DRIFT_ALERT_THRESHOLD,
        "input_fingerprint": "drift_sha",
        "method_version": module.METHOD_VERSION,
        "repo_root": str(tmp_path.resolve()),
        "scopes": ["industry"],
    }
    assert captured["deduplicate_completed"] is True


def test_metric_fingerprint_tracks_memberships_and_source_files(
    tmp_path: Path,
) -> None:
    from execution import track_comp_metrics as module

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE comparable_sets (comparable_set_id TEXT PRIMARY KEY, metric_class TEXT)"
    )
    conn.execute(
        "CREATE TABLE comparable_set_members ("
        "comparable_set_id TEXT, member_ticker TEXT, membership_reason TEXT, "
        "context_only INTEGER, valid_to TEXT)"
    )
    set_id = module.comparable_set_id("NU", module.METHOD_VERSION)
    conn.execute("INSERT INTO comparable_sets VALUES (?, 'operating')", (set_id,))
    conn.execute(
        "INSERT INTO comparable_set_members VALUES (?, 'SOFI', 'industry', 0, NULL)",
        (set_id,),
    )
    source_dir = tmp_path / "data" / "historical" / "fmp"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "SOFI_historical_market_cap.json"
    source_file.write_text('[{"date":"2026-07-28","marketCap":100}]', encoding="utf-8")

    def _fingerprint() -> tuple[
        dict[str, module.FrozenSet | None], module.ScopeSlices, str, str, int
    ]:
        return module._metric_input_fingerprint(
            conn,
            tickers=["NU"],
            repo_root=tmp_path,
            include_pool_scopes=False,
        )

    # Source files are identified by (st_mtime_ns, st_size), so pin mtime
    # explicitly rather than leaning on filesystem timestamp granularity.
    os.utime(source_file, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    frozen, slices, selection_a, sources_a, file_count = _fingerprint()

    # Nothing touched: identical fingerprint, so a same-day re-run is still
    # correctly deduplicated.
    _, _, _, sources_repeat, _ = _fingerprint()

    # A refetch rewrites the file and moves mtime, even at an identical size.
    source_file.write_text('[{"date":"2026-07-28","marketCap":200}]', encoding="utf-8")
    assert source_file.stat().st_size == len('[{"date":"2026-07-28","marketCap":100}]')
    os.utime(source_file, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    _, _, selection_b, sources_mtime, _ = _fingerprint()

    # A size change is caught even if mtime were somehow preserved.
    source_file.write_text('[{"date":"2026-07-28","marketCap":2000}]', encoding="utf-8")
    os.utime(source_file, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    _, _, _, sources_size, _ = _fingerprint()

    conn.execute(
        "UPDATE comparable_set_members SET membership_reason = 'fallback' "
        "WHERE comparable_set_id = ?",
        (set_id,),
    )
    _, _, selection_c, _, _ = _fingerprint()

    assert frozen["NU"] == ("operating", [("SOFI", "industry", False)])
    assert slices == {}
    assert file_count == len(module._MEMBER_SOURCE_SUFFIXES)
    assert selection_a == selection_b
    assert sources_a == sources_repeat
    assert sources_a != sources_mtime
    assert sources_mtime != sources_size
    assert selection_b != selection_c
    conn.close()


def test_validation_gate_is_part_of_the_invocation_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import run_validation_engine as module

    captured: dict[str, object] = {}
    monkeypatch.setattr(module, "open_db", lambda _db: _Conn())

    def _capture(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        raise PipelineRunSuppressedError("pipeline_key", "attempt_id", StageStatus.IN_PROGRESS)

    monkeypatch.setattr(module, "start_run", _capture)
    monkeypatch.setattr(
        sys,
        "argv",
        ["scheduled-job", "--gate", "--ticker", "NU", "--user-id", "bhanu"],
    )

    assert module.main() == 0
    assert captured["invocation_inputs"] == {"gate": True, "user_id": "bhanu"}
    assert "deduplicate_completed" not in captured


def test_morning_suppression_stops_stages_and_force_reaches_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import run_morning_pipeline as module

    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    captured: dict[str, object] = {}

    def _suppress(_db_path: Path, **kwargs: object) -> str:
        captured.update(kwargs)
        raise PipelineRunSuppressedError("morning_key", "morning_attempt", StageStatus.IN_PROGRESS)

    monkeypatch.setattr(module, "_record_run", _suppress)
    monkeypatch.setattr(module, "_run_stage", _fail_if_called)

    assert (
        module.main(
            [
                "--db-path",
                str(db_path),
                "--force",
                "--max-cost-usd",
                "7.5",
                "--news-source",
                "websearch",
                "--skip-news",
                "--skip-standup",
                "--skip-triggers",
                "--skip-validation",
                "--user-id",
                "alice",
            ]
        )
        == 0
    )
    assert captured == {
        "start": True,
        "invocation_inputs": {
            "max_cost_usd": 7.5,
            "news_source": "websearch",
            "only": "",
            "from_stage": "",
            "run_date": date.today().isoformat(),
            "skip_news": True,
            "skip_standup": True,
            "skip_triggers": True,
            "skip_validation": True,
            "user_id": "alice",
        },
        "force": True,
    }
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_running",
        "pipeline_key": "morning_key",
        "attempt_id": "morning_attempt",
    }


def test_restore_drill_suppression_precedes_restore_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import restore_drill as module

    def _suppress(*_args: object, **_kwargs: object) -> None:
        raise PipelineRunSuppressedError(
            "restore_key",
            "restore_attempt",
            StageStatus.IN_PROGRESS,
        )

    monkeypatch.setattr(module, "_start_accounting", _suppress)
    monkeypatch.setattr(module, "run_drill", _fail_if_called)

    assert (
        module.main(
            [
                "--backup-dir",
                str(tmp_path / "backups"),
                "--db",
                str(tmp_path / "portfolio.db"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_running",
        "pipeline_key": "restore_key",
        "attempt_id": "restore_attempt",
    }


def test_restore_accounting_fingerprints_paths_before_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import restore_drill as module

    live_db = tmp_path / "portfolio.db"
    live_db.touch()
    backup_dir = tmp_path / "backups"
    conn = _Conn()
    captured: dict[str, object] = {}

    monkeypatch.setattr(module, "open_db", lambda _db: conn)

    def _capture(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "RID"

    monkeypatch.setattr(module, "start_run", _capture)

    accounting = module._start_accounting(
        live_db,
        backup_dir=backup_dir,
        keep=True,
        snapshot_name="portfolio.db.20260728.gz.enc",
        snapshot_sha256="a" * 64,
        live_schema="0192_schema",
    )

    assert accounting == (conn, "RID")
    assert captured["invocation_inputs"] == {
        "backup_dir": str(backup_dir.resolve()),
        "keep": True,
        "live_db": str(live_db.resolve()),
        "live_schema": "0192_schema",
        "snapshot_name": "portfolio.db.20260728.gz.enc",
        "snapshot_sha256": "a" * 64,
    }
    assert captured["deduplicate_completed"] is True
    conn.close()


def test_morning_accounting_deduplicates_completed_same_day_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import run_morning_pipeline as module
    from pipeline import run_accounting

    conn = _Conn()
    captured: dict[str, object] = {}

    monkeypatch.setattr(module, "connect_sqlite", lambda *_args, **_kwargs: conn)

    def _capture(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "RID"

    monkeypatch.setattr(run_accounting, "start_run", _capture)

    assert (
        module._record_run(
            tmp_path / "portfolio.db",
            start=True,
            invocation_inputs={"run_date": "2026-07-28"},
            force=True,
        )
        == "RID"
    )
    assert captured["invocation_inputs"] == {"run_date": "2026-07-28"}
    assert captured["force"] is True
    assert captured["deduplicate_completed"] is True
    assert conn.closed is True


def test_restore_main_hashes_and_reuses_the_selected_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import restore_drill as module

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    snapshot = backup_dir / "portfolio.db.20260728T090000Z.gz.enc"
    snapshot.write_bytes(b"immutable encrypted snapshot")
    live_db = tmp_path / "absent.db"
    observed: dict[str, object] = {}

    def _start(
        _live_db: Path,
        **kwargs: object,
    ) -> None:
        observed["accounting"] = kwargs

    def _drill(
        _backup_dir: Path,
        _live_db: Path,
        *,
        keep: bool,
        snapshot: Path | None,
    ) -> tuple[bool, dict[str, object]]:
        observed["drill"] = {"keep": keep, "snapshot": snapshot}
        return True, {"status": "ok"}

    monkeypatch.setattr(module, "_start_accounting", _start)
    monkeypatch.setattr(module, "run_drill", _drill)

    assert (
        module.main(
            [
                "--backup-dir",
                str(backup_dir),
                "--db",
                str(live_db),
            ]
        )
        == 0
    )
    accounting = observed["accounting"]
    assert isinstance(accounting, dict)
    assert accounting["snapshot_name"] == snapshot.name
    assert accounting["snapshot_sha256"] == module._snapshot_sha256(snapshot)
    assert accounting["live_schema"] is None
    assert observed["drill"] == {"keep": False, "snapshot": snapshot}
