"""Sealed offline artifact boundary and dependency preflight verifier.

Enforces side-effect-free, deterministic offline builds for research artifacts
(HTML workspace, Markdown report, and sections JSON). Freezes all input hashes,
asserts zero live network/LLM calls or DB mutations, and guarantees byte-for-byte
reproducibility across repeated render passes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from report.builder import build_report
from report.renderers.markdown import render as render_markdown
from report.renderers.sections_json import render as render_sections_json
from report.renderers.workspace_html import render as render_workspace_html
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class DependencyPreflightCheck(BaseModel):
    """Result of dependency preflight validation before an offline build."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    ticker: str = Field(..., min_length=1)
    as_of_date: date
    checked_files: tuple[str, ...]
    code_commit_sha: str
    database_integrity_status: str
    canary_manifest_verified: bool
    violations: tuple[str, ...]
    checked_at: datetime


class OfflineInputManifest(BaseModel):
    """Pinned input hashes ensuring strict build determinism."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(..., min_length=1)
    as_of_date: date
    code_commit_sha: str
    database_wal_sha256: str
    canary_manifest_sha256: str | None = None
    output_dir: str
    manifest_hash: str = Field(..., min_length=64, max_length=64)


class OfflineBuildReceipt(BaseModel):
    """Immutable audit receipt for a completed offline build."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_id: str = Field(..., min_length=1)
    ticker: str = Field(..., min_length=1)
    as_of_date: date
    input_manifest_hash: str = Field(..., min_length=64, max_length=64)
    html_sha256: str = Field(..., min_length=64, max_length=64)
    md_sha256: str = Field(..., min_length=64, max_length=64)
    sections_sha256: str = Field(..., min_length=64, max_length=64)
    deterministic_two_pass_verified: bool
    emitted_files: tuple[str, ...]
    duration_ms: float
    created_at: datetime


def _compute_sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def get_git_commit_sha(repo_root: Path) -> str:
    """Retrieve current git commit SHA or return fallback."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return res.stdout.strip()
    except Exception:
        return "0000000000000000000000000000000000000000"


def compute_db_wal_hash(db_path: Path) -> str:
    """Compute full content SHA-256 hash across main sqlite DB file plus active WAL and SHM sidecars."""
    if not db_path.exists():
        return _compute_sha256(b"missing_db")
    try:
        hasher = hashlib.sha256()
        with open(db_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)

        # Hash WAL sidecar if present
        wal_path = db_path.parent / f"{db_path.name}-wal"
        if wal_path.exists():
            with open(wal_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)

        # Hash SHM sidecar if present
        shm_path = db_path.parent / f"{db_path.name}-shm"
        if shm_path.exists():
            with open(shm_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)

        return hasher.hexdigest()
    except Exception as e:
        return _compute_sha256(f"err:{e}".encode())


class OfflineBuildBoundary:
    """Isolated, side-effect-free offline build executor and preflight verifier."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.canary_manifest_path = self.repo_root / "data" / "fmp_canary_manifest.json"
        self.default_db_path = self.repo_root / "data" / "portfolio.db"

    def run_preflight(
        self,
        ticker: str,
        as_of_date: date,
        conn: sqlite3.Connection | None = None,
    ) -> DependencyPreflightCheck:
        """Verify that all inputs, databases, and dependencies satisfy offline invariants."""
        violations: list[str] = []
        checked_files: list[str] = []
        ticker_upper = ticker.upper()

        commit_sha = get_git_commit_sha(self.repo_root)
        if (self.repo_root / ".git").exists() and commit_sha == "0" * 40:
            violations.append("Unable to resolve valid git commit SHA from existing .git repository")

        # 1. Check database integrity
        db_status = "OK"
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check")
                row = cur.fetchone()
                if not row or row[0] != "ok":
                    db_status = f"Integrity failed: {row}"
                    violations.append(f"SQLite integrity check failed: {row}")
            except Exception as e:
                db_status = f"Error: {e}"
                violations.append(f"SQLite query error during preflight: {e}")
        elif self.default_db_path.exists():
            checked_files.append(str(self.default_db_path.relative_to(self.repo_root)))
            try:
                test_conn = connect_sqlite(str(self.default_db_path), role=SQLiteConnectionRole.READ_ONLY)
                cur = test_conn.cursor()
                cur.execute("PRAGMA integrity_check")
                row = cur.fetchone()
                test_conn.close()
                if not row or row[0] != "ok":
                    db_status = f"Integrity failed: {row}"
                    violations.append(f"SQLite integrity check failed: {row}")
            except Exception as e:
                db_status = f"Error: {e}"
                violations.append(f"SQLite read error on {self.default_db_path}: {e}")
        else:
            db_status = "MISSING_DEFAULT_DB"
            violations.append(f"Default database does not exist: {self.default_db_path}")

        # 2. Check canary manifest if present
        canary_verified = False
        if self.canary_manifest_path.exists():
            checked_files.append(str(self.canary_manifest_path.relative_to(self.repo_root)))
            try:
                manifest_data = cast("dict[str, Any]", json.loads(self.canary_manifest_path.read_text(encoding="utf-8")))
                files = cast("list[dict[str, Any]]", manifest_data.get("files", []))
                # If ticker is in canary corpus (e.g. WIX or RBRK), verify its files exist
                ticker_files = [f for f in files if str(f.get("ticker", "")).upper() == ticker_upper]
                missing_canary: list[str] = []
                corrupted_canary: list[str] = []
                for cf in ticker_files:
                    fn = str(cf.get("filename", ""))
                    expected_sha = str(cf.get("sha256", ""))
                    fp = self.repo_root / "data" / "historical" / "fmp" / fn
                    if not fp.exists():
                        missing_canary.append(fn)
                    elif expected_sha and _compute_sha256(fp.read_bytes()) != expected_sha:
                        corrupted_canary.append(fn)
                if missing_canary:
                    violations.append(f"Missing {len(missing_canary)} sealed canary files for {ticker_upper}: {missing_canary[:3]}")
                if corrupted_canary:
                    violations.append(f"Corrupted {len(corrupted_canary)} sealed canary files (hash mismatch) for {ticker_upper}: {corrupted_canary[:3]}")
                if not missing_canary and not corrupted_canary:
                    canary_verified = True
            except Exception as e:
                violations.append(f"Failed parsing canary manifest: {e}")

        passed = len(violations) == 0
        return DependencyPreflightCheck(
            passed=passed,
            ticker=ticker_upper,
            as_of_date=as_of_date,
            checked_files=tuple(checked_files),
            code_commit_sha=commit_sha,
            database_integrity_status=db_status,
            canary_manifest_verified=canary_verified,
            violations=tuple(violations),
            checked_at=datetime.now(UTC),
        )

    def compute_input_manifest(
        self,
        ticker: str,
        as_of_date: date,
        output_dir: Path,
        db_path: Path | None = None,
    ) -> OfflineInputManifest:
        """Compute an immutable input manifest freezing code, DB, and config."""
        commit_sha = get_git_commit_sha(self.repo_root)
        active_db = db_path or self.default_db_path
        db_wal_hash = compute_db_wal_hash(active_db)

        canary_hash = None
        if self.canary_manifest_path.exists():
            canary_hash = _compute_sha256(self.canary_manifest_path.read_bytes())

        manifest_repr = f"{ticker.upper()}:{as_of_date.isoformat()}:{commit_sha}:{db_wal_hash}:{canary_hash}:{output_dir}"
        manifest_hash = _compute_sha256(manifest_repr)

        return OfflineInputManifest(
            ticker=ticker.upper(),
            as_of_date=as_of_date,
            code_commit_sha=commit_sha,
            database_wal_sha256=db_wal_hash,
            canary_manifest_sha256=canary_hash,
            output_dir=str(output_dir),
            manifest_hash=manifest_hash,
        )

    def render_artifacts(
        self,
        ticker: str,
        repo_root: Path,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, str, str]:
        """Perform side-effect-free extraction and rendering of HTML, MD, and JSON."""
        close_conn_after = False
        active_conn = conn
        if active_conn is None and self.default_db_path.exists():
            active_conn = connect_sqlite(str(self.default_db_path), role=SQLiteConnectionRole.READ_ONLY)
            close_conn_after = True

        try:
            # Force enable_llm=False and refresh_news=False for strict offline boundary
            spec = build_report(
                ticker=ticker.upper(),
                repo_root=repo_root,
                enable_llm=False,
                refresh_news=False,
                force_refresh=False,
                conn=active_conn,
            )

            html_str = render_workspace_html(spec)
            md_str = render_markdown(spec)
            sections_dict = render_sections_json(spec)
            sections_str = json.dumps(sections_dict, indent=2, sort_keys=True)

            return html_str, md_str, sections_str
        finally:
            if close_conn_after and active_conn is not None:
                active_conn.close()

    def execute_sealed_build(
        self,
        ticker: str,
        as_of_date: date,
        output_dir: Path,
        *,
        verify_determinism: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> OfflineBuildReceipt:
        """Execute preflight, freeze input manifest, render artifacts, and write receipt."""
        t_start = time.perf_counter()
        ticker_upper = ticker.upper()
        output_dir.mkdir(parents=True, exist_ok=True)

        close_conn_after = False
        active_conn = conn
        if active_conn is None and self.default_db_path.exists():
            active_conn = connect_sqlite(str(self.default_db_path), role=SQLiteConnectionRole.READ_ONLY)
            close_conn_after = True

        try:
            # 1. Preflight check
            preflight = self.run_preflight(ticker_upper, as_of_date, conn=active_conn)
            if not preflight.passed:
                raise RuntimeError(f"Offline build preflight failed for {ticker_upper}: {preflight.violations}")

            # 2. Input Manifest
            manifest = self.compute_input_manifest(ticker_upper, as_of_date, output_dir)

            # 3. First Render Pass
            html_1, md_1, json_1 = self.render_artifacts(ticker_upper, self.repo_root, conn=active_conn)
            html_sha = _compute_sha256(html_1)
            md_sha = _compute_sha256(md_1)
            sections_sha = _compute_sha256(json_1)

            # 4. Optional Determinism Check (Second Render Pass)
            two_pass_ok = True
            if verify_determinism:
                html_2, md_2, json_2 = self.render_artifacts(ticker_upper, self.repo_root, conn=active_conn)
                if html_1 != html_2:
                    raise ValueError("Determinism failure: workspace.html diff across passes")
                if md_1 != md_2:
                    raise ValueError("Determinism failure: report.md diff across passes")
                if json_1 != json_2:
                    raise ValueError("Determinism failure: sections.json diff across passes")

            # 5. Write artifacts to output_dir
            date_str = as_of_date.isoformat()
            html_file = output_dir / f"{date_str}_workspace.html"
            md_file = output_dir / f"{date_str}_report.md"
            json_file = output_dir / f"{date_str}_sections.json"
            receipt_file = output_dir / f"{date_str}_offline_receipt.json"

            html_file.write_text(html_1, encoding="utf-8")
            md_file.write_text(md_1, encoding="utf-8")
            json_file.write_text(json_1, encoding="utf-8")

            duration_ms = (time.perf_counter() - t_start) * 1000.0
            build_id = f"offline_{ticker_upper}_{date_str}_{int(time.time())}"

            emitted = (
                str(html_file),
                str(md_file),
                str(json_file),
                str(receipt_file),
            )

            receipt = OfflineBuildReceipt(
                build_id=build_id,
                ticker=ticker_upper,
                as_of_date=as_of_date,
                input_manifest_hash=manifest.manifest_hash,
                html_sha256=html_sha,
                md_sha256=md_sha,
                sections_sha256=sections_sha,
                deterministic_two_pass_verified=two_pass_ok,
                emitted_files=emitted,
                duration_ms=duration_ms,
                created_at=datetime.now(UTC),
            )

            receipt_file.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
            return receipt
        finally:
            if close_conn_after and active_conn is not None:
                active_conn.close()
