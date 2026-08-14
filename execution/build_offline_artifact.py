"""Build one sealed research artifact without network or canonical mutation.

The parent phase snapshots a caller-selected database and a closed set of local
inputs into a temporary repository. A fresh Python worker then imports code
from that isolated tree, renders with an explicit as-of, and writes only to the
explicit output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import ReportFlavor  # noqa: E402
from report.offline_artifact import (  # noqa: E402
    DependencyRecord,
    OfflineArtifactPayload,
    OfflineBoundaryError,
    offline_runtime_guard,
    stage_offline_repository,
    write_offline_artifact,
)


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
        choices=[flavor.value for flavor in ReportFlavor],
        default=ReportFlavor.PORTFOLIO.value,
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dependency-manifest", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _parent(args: argparse.Namespace) -> int:
    source_repo = args.repo_root.resolve()
    output_dir = args.output_dir.resolve()
    if _is_within(output_dir, source_repo):
        raise OfflineBoundaryError("offline output directory must be outside the source repository")
    if not args.database.resolve().is_file():
        raise FileNotFoundError(args.database.resolve())

    with tempfile.TemporaryDirectory(prefix="earnings-offline-artifact-") as temporary:
        sandbox = Path(temporary)
        isolated_repo = sandbox / "earnings-summary"
        dependencies = stage_offline_repository(
            source_repo=source_repo,
            isolated_repo=isolated_repo,
            database=args.database.resolve(),
            ticker=args.ticker,
            portfolio_database=(
                args.portfolio_database.resolve() if args.portfolio_database is not None else None
            ),
        )
        manifest_path = sandbox / "dependencies.json"
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
        command = [
            sys.executable,
            str(worker),
            "--worker",
            "--ticker",
            args.ticker.upper(),
            "--repo-root",
            str(isolated_repo),
            "--database",
            str(isolated_repo / "data" / "portfolio.db"),
            "--output-dir",
            str(output_dir),
            "--as-of",
            args.as_of.isoformat(),
            "--flavor",
            args.flavor,
            "--dependency-manifest",
            str(manifest_path),
        ]
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(isolated_repo / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "EARNINGS_OFFLINE_RENDER": "1",
                "LLM_FALLBACK_DISABLED": "1",
            }
        )
        completed = subprocess.run(
            command,
            cwd=isolated_repo,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "offline worker failed").strip()
            raise OfflineBoundaryError(detail[-4_000:])
        print(completed.stdout.strip())
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


def _worker(args: argparse.Namespace) -> int:
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

    with offline_runtime_guard(output_dir.parent) as metrics:
        connection = connect_sqlite(
            database,
            role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
            schema_preflight=True,
        )
        try:
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
        finally:
            connection.close()

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
        receipt = write_offline_artifact(
            output_dir=output_dir,
            ticker=args.ticker,
            as_of=args.as_of,
            payload=payload,
            dependencies=dependencies,
        )
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
