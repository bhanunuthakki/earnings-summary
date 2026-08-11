"""Run the Sunday portfolio synthesis as one fail-fast, resumable job.

The scheduler wrapper owns ``portfolio-db`` once around this entire process.
Each successful stage is checkpointed so a scheduler retry resumes at the
first incomplete stage, while a roster change invalidates the old checkpoint.
Exactly one typed terminal receipt is emitted to stdout; stage output and
structured progress events go to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_paths import configured_db_path  # noqa: E402
from runtime.python_process import managed_python_argv  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_CHECKPOINT_VERSION = 2


@dataclass(frozen=True, slots=True)
class _Stage:
    key: str
    script: str
    arguments: tuple[str, ...] = ()


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    scope: str
    completed_stages: list[str]
    updated_at: str


class StageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    status: Literal["ok", "failed"]
    exit_code: int
    elapsed_seconds: float
    detail: str | None = None


class WeeklySynthesisReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: Literal["weekly_synthesis"] = "weekly_synthesis"
    status: Literal["ok", "failed"]
    db_path: str | None
    scope: str | None
    portfolio_tickers: list[str]
    resumed_stages: list[str]
    stages: list[StageReceipt]
    failed_stage: str | None = None
    detail: str | None = None


class CheckpointError(RuntimeError):
    """The persisted resume state is malformed and cannot be trusted."""


class DatabaseIdentityError(RuntimeError):
    """The outer lock DB cannot be threaded through every child stage."""

    def __init__(self, configured: Path, stage_db: Path) -> None:
        self.configured = configured
        self.stage_db = stage_db
        super().__init__(
            "cannot safely thread EARNINGS_SUMMARY_DB_PATH to weekly synthesis stages: "
            f"configured={configured}, repo_root_stage_db={stage_db}"
        )


def _resolve_db_identity(repo_root: Path) -> Path:
    configured = configured_db_path(repo_root).resolve()
    stage_db = (repo_root / "data" / "portfolio.db").resolve()
    if configured != stage_db:
        raise DatabaseIdentityError(configured, stage_db)
    return configured


def _active_portfolio_tickers(repo_root: Path, db_path: Path) -> list[str]:
    """Read the governed active portfolio roster from the canonical database."""
    del repo_root
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE archived_at IS NULL AND list_type = 'portfolio' "
            "ORDER BY UPPER(ticker)"
        ).fetchall()
    finally:
        conn.close()
    return sorted({str(row[0]).strip().upper() for row in rows if row and str(row[0]).strip()})


def _scope_for(tickers: list[str], db_path: Path, now: datetime | None = None) -> str:
    observed = now or datetime.now(UTC)
    iso_year, iso_week, _weekday = observed.isocalendar()
    roster_hash = hashlib.sha256("\n".join(tickers).encode("utf-8")).hexdigest()[:16]
    db_hash = hashlib.sha256(str(db_path).casefold().encode("utf-8")).hexdigest()[:16]
    return f"{iso_year}-W{iso_week:02d}:{db_hash}:{roster_hash}:v{_CHECKPOINT_VERSION}"


def _checkpoint_path(repo_root: Path) -> Path:
    return repo_root / ".tmp" / "weekly_synthesis" / "state.json"


def _load_completed(repo_root: Path, scope: str, stage_keys: tuple[str, ...]) -> set[str]:
    path = _checkpoint_path(repo_root)
    if not path.exists():
        return set()
    try:
        checkpoint = _Checkpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise CheckpointError(f"invalid checkpoint {path}: {exc}") from exc
    if checkpoint.scope != scope:
        return set()
    unknown = set(checkpoint.completed_stages).difference(stage_keys)
    if unknown:
        raise CheckpointError(f"checkpoint contains unknown stages: {sorted(unknown)}")
    return set(checkpoint.completed_stages)


def _record_completed(
    repo_root: Path,
    scope: str,
    completed: set[str],
    stage_keys: tuple[str, ...],
) -> None:
    path = _checkpoint_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = _Checkpoint(
        scope=scope,
        completed_stages=[key for key in stage_keys if key in completed],
        updated_at=datetime.now(UTC).isoformat(),
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(checkpoint.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _emit_receipt(receipt: WeeklySynthesisReceipt) -> None:
    sys.stdout.write(receipt.model_dump_json() + "\n")
    sys.stdout.flush()


def _echo_to_stderr(value: str | bytes | None) -> None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not value:
        return
    sys.stderr.write(value)
    if not value.endswith("\n"):
        sys.stderr.write("\n")


def _stages(repo_root: Path, tickers: list[str]) -> tuple[_Stage, ...]:
    root_args = ("--repo-root", str(repo_root))
    ticker_stages = tuple(
        _Stage(
            f"ticker_lenses:{ticker}",
            "execution/run_lens.py",
            (*root_args, "--ticker", ticker, "--all"),
        )
        for ticker in tickers
    )
    return (
        _Stage(
            "refresh_dirty_artifacts",
            "execution/refresh_dirty_artifacts.py",
            (*root_args, "--manifest-only"),
        ),
        *ticker_stages,
        _Stage(
            "cross_portfolio_synthesis",
            "execution/run_lens.py",
            (*root_args, "--lens", "cross_portfolio_synthesis"),
        ),
        _Stage(
            "analytical_dashboard",
            "execution/build_analytical_dashboard.py",
            root_args,
        ),
    )


def _run_stage(repo_root: Path, stage: _Stage) -> StageReceipt:
    argv = managed_python_argv(
        repo_root,
        stage.script,
        *stage.arguments,
        unbuffered=True,
    )
    print(
        json.dumps({"event": "weekly_synthesis_stage_started", "stage": stage.key}),
        file=sys.stderr,
        flush=True,
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return StageReceipt(
            key=stage.key,
            status="failed",
            exit_code=1,
            elapsed_seconds=round(time.monotonic() - started, 3),
            detail=f"spawn failed: {type(exc).__name__}: {exc}",
        )
    _echo_to_stderr(result.stdout)
    _echo_to_stderr(result.stderr)
    elapsed = round(time.monotonic() - started, 3)
    if result.returncode != 0:
        return StageReceipt(
            key=stage.key,
            status="failed",
            exit_code=int(result.returncode),
            elapsed_seconds=elapsed,
            detail=f"child exited {result.returncode}",
        )
    return StageReceipt(
        key=stage.key,
        status="ok",
        exit_code=0,
        elapsed_seconds=elapsed,
    )


def _terminal_exit_code(code: int) -> int:
    return code if 1 <= code <= 255 else 1


def run(repo_root: Path) -> int:
    root = repo_root.resolve()
    try:
        db_path = _resolve_db_identity(root)
    except DatabaseIdentityError as exc:
        _emit_receipt(
            WeeklySynthesisReceipt(
                status="failed",
                db_path=str(exc.configured),
                scope=None,
                portfolio_tickers=[],
                resumed_stages=[],
                stages=[],
                detail=str(exc),
            )
        )
        return 2
    try:
        tickers = _active_portfolio_tickers(root, db_path)
    except (OSError, sqlite3.Error) as exc:
        _emit_receipt(
            WeeklySynthesisReceipt(
                status="failed",
                db_path=str(db_path),
                scope=None,
                portfolio_tickers=[],
                resumed_stages=[],
                stages=[],
                detail=f"portfolio roster load failed: {type(exc).__name__}: {exc}",
            )
        )
        return 2
    if not tickers:
        _emit_receipt(
            WeeklySynthesisReceipt(
                status="failed",
                db_path=str(db_path),
                scope=None,
                portfolio_tickers=[],
                resumed_stages=[],
                stages=[],
                detail="active portfolio roster is empty",
            )
        )
        return 2

    scope = _scope_for(tickers, db_path)
    stages = _stages(root, tickers)
    stage_keys = tuple(stage.key for stage in stages)
    try:
        completed = _load_completed(root, scope, stage_keys)
    except CheckpointError as exc:
        _emit_receipt(
            WeeklySynthesisReceipt(
                status="failed",
                db_path=str(db_path),
                scope=scope,
                portfolio_tickers=tickers,
                resumed_stages=[],
                stages=[],
                detail=str(exc),
            )
        )
        return 1
    resumed = [key for key in stage_keys if key in completed]
    stage_receipts: list[StageReceipt] = []
    for stage in stages:
        if stage.key in completed:
            continue
        receipt = _run_stage(root, stage)
        stage_receipts.append(receipt)
        if receipt.status == "failed":
            _emit_receipt(
                WeeklySynthesisReceipt(
                    status="failed",
                    db_path=str(db_path),
                    scope=scope,
                    portfolio_tickers=tickers,
                    resumed_stages=resumed,
                    stages=stage_receipts,
                    failed_stage=stage.key,
                    detail=receipt.detail,
                )
            )
            return _terminal_exit_code(receipt.exit_code)
        completed.add(stage.key)
        try:
            _record_completed(root, scope, completed, stage_keys)
        except OSError as exc:
            _emit_receipt(
                WeeklySynthesisReceipt(
                    status="failed",
                    db_path=str(db_path),
                    scope=scope,
                    portfolio_tickers=tickers,
                    resumed_stages=resumed,
                    stages=stage_receipts,
                    failed_stage=stage.key,
                    detail=f"checkpoint persistence failed: {type(exc).__name__}: {exc}",
                )
            )
            return 1

    try:
        _checkpoint_path(root).unlink(missing_ok=True)
    except OSError as exc:
        _emit_receipt(
            WeeklySynthesisReceipt(
                status="failed",
                db_path=str(db_path),
                scope=scope,
                portfolio_tickers=tickers,
                resumed_stages=resumed,
                stages=stage_receipts,
                detail=f"checkpoint cleanup failed: {type(exc).__name__}: {exc}",
            )
        )
        return 1
    _emit_receipt(
        WeeklySynthesisReceipt(
            status="ok",
            db_path=str(db_path),
            scope=scope,
            portfolio_tickers=tickers,
            resumed_stages=resumed,
            stages=stage_receipts,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    return run(cast("Path", args.repo_root))


if __name__ == "__main__":
    raise SystemExit(main())
