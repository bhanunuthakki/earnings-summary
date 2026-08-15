"""Build one sealed research artifact without network or canonical mutation.

The parent phase snapshots a caller-selected database and a closed set of local
inputs into a temporary repository. A fresh Python worker then imports code
from that isolated tree, renders with an explicit as-of, and writes only to the
explicit output directory.
"""

from __future__ import annotations

import argparse
import atexit
import faulthandler
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--portfolio-database", type=Path)
    parser.add_argument(
        "--flavor",
        choices=("portfolio", "evaluation"),
        default="portfolio",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dependency-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--environment-body-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=int, default=300, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _parent(args: argparse.Namespace) -> int:
    from report.offline_artifact import (
        OfflineBoundaryError,
        runtime_closure_dependency_records,
        runtime_root_specs,
        stage_offline_repository,
    )
    from report.windows_appcontainer import (
        minimal_worker_environment,
        run_appcontainer_worker,
    )

    source_repo = args.repo_root.resolve()
    output_dir = args.output_dir.resolve()
    if _is_within(output_dir, source_repo):
        raise OfflineBoundaryError("offline output directory must be outside the source repository")
    if not args.database.resolve().is_file():
        raise FileNotFoundError(args.database.resolve())
    database = args.database.resolve()
    if (
        _is_within(output_dir, source_repo)
        or _is_within(source_repo, output_dir)
        or output_dir == database
    ):
        raise OfflineBoundaryError(
            "offline output directory must be disjoint from canonical repository and database roots"
        )
    environment_body_output = (
        args.environment_body_output.resolve() if args.environment_body_output is not None else None
    )
    if environment_body_output is not None and (
        environment_body_output.exists()
        or _is_within(environment_body_output, source_repo)
        or environment_body_output in (database, output_dir)
    ):
        raise OfflineBoundaryError("environment diagnostic output must be a new external file")

    with tempfile.TemporaryDirectory(prefix="earnings-offline-artifact-") as temporary:
        sandbox = Path(temporary)
        isolated_repo = sandbox / "earnings-summary"
        private_write_root = sandbox / "private-write"
        private_write_root.mkdir()
        dependencies = stage_offline_repository(
            source_repo=source_repo,
            isolated_repo=isolated_repo,
            database=args.database.resolve(),
            ticker=args.ticker,
            portfolio_database=(
                args.portfolio_database.resolve() if args.portfolio_database is not None else None
            ),
        )
        runtime_specs = runtime_root_specs()
        runtime_dependencies = runtime_closure_dependency_records()
        dependencies = (*dependencies, *runtime_dependencies)
        manifest_path = isolated_repo / "config" / "offline_dependencies.json"
        manifest_path.write_text(
            json.dumps(
                [record.model_dump(mode="json") for record in dependencies],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        worker = isolated_repo / "execution" / "build_offline_artifact.py"
        staged_artifact = private_write_root / "artifact"
        worker_executable = Path(sys.executable).resolve()
        venv_launcher: str | None = None
        if Path(sys.prefix).resolve() != Path(sys.base_prefix).resolve():
            base_executable = getattr(sys, "_base_executable", None)
            if not isinstance(base_executable, str) or not base_executable:
                raise OfflineBoundaryError("virtual environment base executable is unavailable")
            worker_executable = Path(base_executable).resolve()
            venv_launcher = str(Path(sys.executable).resolve())
        command = [
            str(worker_executable),
            str(worker),
            "--worker",
            "--ticker",
            args.ticker.upper(),
            "--repo-root",
            str(isolated_repo),
            "--database",
            str(isolated_repo / "data" / "portfolio.db"),
            "--output-dir",
            str(staged_artifact),
            "--as-of",
            args.as_of.isoformat(),
            "--flavor",
            args.flavor,
            "--dependency-manifest",
            str(manifest_path),
            "--timeout-seconds",
            str(args.timeout_seconds),
        ]
        if environment_body_output is not None:
            command.extend(
                [
                    "--environment-body-output",
                    str(private_write_root / "environment.json"),
                ]
            )
        environment = minimal_worker_environment(
            isolated_repo=isolated_repo,
            write_root=private_write_root,
        )
        if venv_launcher is not None:
            # Avoid the Windows venv redirector spawning a second process inside
            # the one-process job while retaining the exact venv prefix/identity.
            environment["__PYVENV_LAUNCHER__"] = venv_launcher
        returncode, stdout, stderr = run_appcontainer_worker(
            command,
            cwd=isolated_repo,
            read_roots=(*(root for _logical, root in runtime_specs), isolated_repo),
            write_root=private_write_root,
            environment=environment,
            timeout_seconds=args.timeout_seconds,
            export_names=(
                ("artifact", "environment.json")
                if environment_body_output is not None
                else ("artifact",)
            ),
        )
        if returncode != 0:
            detail = (stderr or stdout or "offline worker failed").strip()
            raise OfflineBoundaryError(detail[-4_000:])
        if runtime_closure_dependency_records() != runtime_dependencies:
            raise OfflineBoundaryError("Python runtime changed during sealed offline execution")
        if not staged_artifact.is_dir():
            raise OfflineBoundaryError("AppContainer worker did not produce an artifact")
        if environment_body_output is not None:
            environment_body = private_write_root / "environment.json"
            if not environment_body.is_file():
                raise OfflineBoundaryError(
                    "AppContainer worker did not produce environment evidence"
                )
            environment_body_output.parent.mkdir(parents=True, exist_ok=True)
            environment_body.replace(environment_body_output)
        if output_dir.exists():
            from report.offline_artifact import verify_artifact_copy

            verify_artifact_copy(staged_artifact, output_dir)
        else:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            staged_artifact.replace(output_dir)
        print(stdout.strip())
    return 0


def _normalize_path_text(value: str, repo_root: Path) -> str:
    normalized = value.replace(str(repo_root), "$SEALED_REPO")
    return normalized.replace(repo_root.as_posix(), "$SEALED_REPO")


def _normalize_json_paths(value: object, repo_root: Path) -> object:
    if isinstance(value, str):
        return _normalize_path_text(value, repo_root)
    if isinstance(value, list):
        items = cast("list[object]", value)
        return [_normalize_json_paths(item, repo_root) for item in items]
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {
            str(key): _normalize_json_paths(item, repo_root)
            for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _section_status(spec: object) -> dict[str, object]:
    fields = (
        "portfolio_position",
        "valuation_basis",
        "snapshot",
        "evaluation_snapshot",
        "company_description",
        "thesis",
        "financials",
        "signals",
        "segments",
        "earnings",
        "saydo",
        "ir_docs",
        "recent_developments",
        "bear_case",
        "provenance",
        "appendix",
        "qa_roster",
        "filing_intelligence",
        "exec_compensation",
        "synthesis",
        "investment_decision_card",
    )
    status: dict[str, object] = {}
    for field in fields:
        section = getattr(spec, field, None)
        section_status = getattr(section, "status", None)
        status[field] = getattr(section_status, "value", None)
    return status


def _worker_event(
    stage: str,
    phase: str = "mark",
    elapsed_ms: int | None = None,
) -> None:
    event: dict[str, object] = {
        "event": "offline_worker_stage",
        "phase": phase,
        "stage": stage,
    }
    if elapsed_ms is not None:
        event["elapsed_ms"] = elapsed_ms
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)


def _arm_worker_diagnostics(timeout_seconds: int) -> None:
    started_ns = time.perf_counter_ns()

    def emit_shutdown() -> None:
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        _worker_event("interpreter_shutdown", "complete", elapsed_ms)

    atexit.register(emit_shutdown)
    faulthandler.enable(file=sys.stderr, all_threads=True)
    dump_after_seconds = max(1, timeout_seconds - 5)
    faulthandler.dump_traceback_later(dump_after_seconds, file=sys.stderr, repeat=False)
    _worker_event("timeout_stack_dump_armed", elapsed_ms=dump_after_seconds * 1_000)


def _worker(args: argparse.Namespace) -> int:
    _arm_worker_diagnostics(args.timeout_seconds)
    from report.windows_appcontainer import is_current_process_appcontainer

    if not is_current_process_appcontainer():
        raise RuntimeError("offline worker refused: process is not an AppContainer")
    from report.models import ReportFlavor
    from report.offline_artifact import (
        DependencyRecord,
        OfflineArtifactPayload,
        OfflineBoundaryError,
        normalized_runtime_environment_body,
        offline_runtime_guard,
        runtime_dependency_records,
        write_offline_artifact,
    )
    from report.render_clock import fixed_render_clock, require_fixed_clock

    if args.dependency_manifest is None:
        raise OfflineBoundaryError("offline worker requires a dependency manifest")
    dependencies = tuple(
        DependencyRecord.model_validate(item)
        for item in json.loads(args.dependency_manifest.read_text(encoding="utf-8"))
    )
    repo_root = args.repo_root.resolve()
    database = args.database.resolve()
    output_dir = args.output_dir.resolve()

    # Imports happen only after PYTHONPATH points at the isolated code copy.
    from report.builder import build_report
    from report.renderers.markdown import render as render_markdown
    from report.renderers.sections_json import render as render_sections_json
    from report.renderers.workspace_html import render_report_body, render_standalone_report
    from report.sections import financials as financials_section
    from report.sections import snapshot as snapshot_section
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

    with fixed_render_clock(args.as_of), offline_runtime_guard(output_dir.parent) as metrics:
        require_fixed_clock()
        _worker_event("sandbox_verified")
        connection = connect_sqlite(
            database,
            role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
            schema_preflight=True,
        )
        try:
            _worker_event("build_report_start")
            spec = build_report(
                ticker=args.ticker,
                repo_root=repo_root,
                model_link=f"dcf/{args.ticker.upper()}.xlsx",
                enable_llm=False,
                refresh_news=False,
                flavor=ReportFlavor(args.flavor),
                force_budget_bypass=False,
                force_refresh=False,
                conn=connection,
                generation_date=args.as_of,
            )
            numeric_provenance: dict[str, object] = {}
            numeric_provenance.update(
                snapshot_section.build_per_metric(args.ticker, repo_root, conn=connection)
            )
            numeric_provenance.update(
                financials_section.build_per_metric(args.ticker, repo_root, conn=connection)
            )
            _worker_event("build_report_complete")
        finally:
            connection.close()

        _worker_event("render_start")
        report_body = render_report_body(spec)
        html = render_standalone_report(spec, report_body)
        sections = json.loads(render_sections_json(spec))
        normalized_sections = _normalize_json_paths(sections, repo_root)
        normalized_provenance = _normalize_json_paths(numeric_provenance, repo_root)
        if not isinstance(normalized_sections, dict) or not isinstance(normalized_provenance, dict):
            raise OfflineBoundaryError("offline renderer produced a non-object JSON boundary")
        payload = OfflineArtifactPayload(
            html=_normalize_path_text(html, repo_root),
            markdown=_normalize_path_text(render_markdown(spec), repo_root),
            sections=cast("dict[str, object]", normalized_sections),
            status={
                "as_of": args.as_of.isoformat(),
                "llm_enabled": False,
                "sections": _section_status(spec),
            },
            numeric_provenance=cast("dict[str, object]", normalized_provenance),
        )
        _worker_event("render_complete")
        environment_body = normalized_runtime_environment_body(
            isolated_repo=repo_root,
            private_write_root=output_dir.parent,
        )
        if args.environment_body_output is not None:
            environment_body_output = args.environment_body_output.resolve()
            if environment_body_output.parent != output_dir.parent:
                raise OfflineBoundaryError(
                    "environment diagnostic must stay in the private output root"
                )
            environment_body_output.write_bytes(environment_body)
        runtime_dependencies = runtime_dependency_records(
            isolated_repo=repo_root,
            private_write_root=output_dir.parent,
            environment_body=environment_body,
        )
        _worker_event("runtime_attested")
        receipt = write_offline_artifact(
            output_dir=output_dir,
            ticker=args.ticker,
            as_of=args.as_of,
            payload=payload,
            dependencies=(*dependencies, *runtime_dependencies),
            telemetry=_worker_event,
            attested_root=output_dir.parent,
        )
        _worker_event("artifact_written")
    if any(
        (
            metrics.network_attempts,
            metrics.subprocess_attempts,
            metrics.llm_attempts,
            metrics.denied_writes,
        )
    ):
        raise OfflineBoundaryError(
            f"offline runtime recorded a denied capability attempt: {metrics.model_dump()}"
        )
    print(receipt.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return _worker(args) if args.worker else _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
