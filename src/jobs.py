"""In-process background-job manager for the Flask backend.

Spawns CLI scripts (build_artifacts.py, run_pipeline.py, categorize_ir_uploads.py)
in subprocesses, captures merged stdout/stderr line-by-line into a bounded
deque, and exposes JSON-serializable status to the frontend.

Single-process Flask dev server only — state is in-memory and lost on restart.
A real deployment would swap this for RQ / Celery / arq behind the same
JobManager.start_*() interface.
"""

from __future__ import annotations

import enum
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_LOG_BUFFER_LINES = 500


class JobKind(str, enum.Enum):
    BUILD = "build"
    PIPELINE = "pipeline"
    CATEGORIZE = "categorize"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class JobState:
    id: str
    kind: JobKind
    ticker: str | None
    cmd: list[str]
    status: JobStatus = JobStatus.PENDING
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_BUFFER_LINES))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "ticker": self.ticker,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "log_tail": list(self.log)[-50:],
        }


class JobManager:
    """Process-local job registry. Thread-safe.

    Two roots, often the same but separable for the worktree case:
    - code_root: where the execution/ CLIs live (typically this checkout)
    - repo_root: where data/, output/, ir_documents/ live (PORTFOLIO_REPO_ROOT)

    For pipeline runs we prefer the repo_root copy of run_pipeline.py because
    it expects a sibling venv/ and .env that only exist in the data repo. When
    that copy is missing (e.g. the data repo predates the script), we fall
    back to code_root.
    """

    def __init__(self, repo_root: Path, code_root: Path | None = None) -> None:
        self._repo_root = repo_root
        self._code_root = code_root if code_root is not None else repo_root
        self._jobs: dict[str, JobState] = {}
        self._active_by_key: dict[tuple[JobKind, str | None], str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_build(self, ticker: str, *, enable_llm: bool = False) -> JobState:
        cmd = [
            sys.executable,
            str(self._code_root / "execution" / "build_artifacts.py"),
            "--ticker",
            ticker,
            "--repo-root",
            str(self._repo_root),
            "--allow-untracked",
        ]
        if enable_llm:
            cmd.append("--enable-llm")
        return self._spawn(JobKind.BUILD, ticker, cmd)

    def start_pipeline(self, ticker: str) -> JobState:
        repo_copy = self._repo_root / "execution" / "run_pipeline.py"
        script = repo_copy if repo_copy.exists() else self._code_root / "execution" / "run_pipeline.py"
        cmd = [sys.executable, str(script), "--company", ticker]
        return self._spawn(JobKind.PIPELINE, ticker, cmd, cwd=self._repo_root)

    def start_categorize(self, ticker: str | None = None) -> JobState:
        cmd = [
            sys.executable,
            str(self._code_root / "execution" / "categorize_ir_uploads.py"),
        ]
        if ticker:
            cmd += ["--ticker", ticker]
        env_extra = {"IR_PROJECT_ROOT": str(self._repo_root)}
        return self._spawn(JobKind.CATEGORIZE, ticker, cmd, env_extra=env_extra)

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[JobState]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda j: j.started_at or "",
                reverse=True,
            )
            return jobs[:limit]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn(
        self,
        kind: JobKind,
        ticker: str | None,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env_extra: Mapping[str, str] | None = None,
    ) -> JobState:
        key = (kind, ticker)
        with self._lock:
            existing_id = self._active_by_key.get(key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and existing.status in {
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                }:
                    return existing

            job = JobState(
                id=uuid.uuid4().hex,
                kind=kind,
                ticker=ticker,
                cmd=list(cmd),
            )
            self._jobs[job.id] = job
            self._active_by_key[key] = job.id

        thread = threading.Thread(
            target=self._run,
            args=(job, cwd, env_extra),
            daemon=True,
            name=f"job-{kind.value}-{ticker or '*'}",
        )
        thread.start()
        return job

    def _run(
        self,
        job: JobState,
        cwd: Path | None,
        env_extra: Mapping[str, str] | None,
    ) -> None:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        job.started_at = datetime.now(timezone.utc).isoformat()
        job.status = JobStatus.RUNNING
        job.log.append(f"$ {' '.join(job.cmd)}")
        try:
            proc = subprocess.Popen(
                job.cmd,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as e:
            job.status = JobStatus.FAILED
            job.error = f"failed to spawn: {e}"
            job.ended_at = datetime.now(timezone.utc).isoformat()
            return

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line:
                job.log.append(line)
        proc.wait()
        job.exit_code = proc.returncode
        job.ended_at = datetime.now(timezone.utc).isoformat()
        job.status = JobStatus.DONE if proc.returncode == 0 else JobStatus.FAILED
        if proc.returncode != 0:
            job.error = f"exit code {proc.returncode}"
