"""execution/build_investment_decision_card.py — generate (or cache-hit) the
Investment Decision Card (PRD ``docs/design/personal_investment_partner_prd.md``
§8.1, P1.1) for one ticker, then re-render today's static workspace HTML so
the strip reflects the fresh artifact.

The workspace is a build-time artifact (execution/comments_server.py serves
the latest ``output/research/<T>/*_workspace.html`` straight off disk) — this
script rebuilds it via the SAME call path ``execution/build_artifacts.py``
uses (``report.builder.build_report`` +
``report.renderers.workspace_html.render``), with ``enable_llm=False`` so the
rebuild is a pure cache read for every OTHER section (bear case, recent
developments, etc.) — only the card generation step itself spends.

Wired as:
  - a step in ``execution/discovery_build.py::build_one`` (every evaluation
    build ends with a card or an explicit failed step);
  - ``execution/refresh_dirty_artifacts.py``'s regenerator for the
    ``investment_decision_card`` purpose;
  - the owner-triggered refresh route (``POST
    /api/research/card/<ticker>/refresh``, comments_server.py).

CLI:
    python execution/build_investment_decision_card.py --ticker NU
    python execution/build_investment_decision_card.py --ticker NU --force

Exit 0 on a persisted card (LLM-authored OR a labeled deterministic
fallback — both are a successful build). Exit 1 on an explicit generation
failure (``CardResult.failure_reason`` set) or any hard-stop exception
(auth/setup) propagating from the governed call.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from db_paths import resolve_db_path  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402
from report.builder import build_report  # noqa: E402
from report.models import ReportFlavor  # noqa: E402
from report.renderers.workspace_html import render as render_workspace_html  # noqa: E402
from research.investment_decision_card import generate_card  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="ticker symbol")
    parser.add_argument("--db-path", type=Path, default=None, help="portfolio.db override")
    parser.add_argument(
        "--repo-root", type=Path, default=None, help="repo root (default: this repo)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="no functional effect on generation (generate_card is already "
        "input-sha cache-keyed); kept for CLI-shape parity with sibling scripts",
    )
    return parser.parse_args(argv)


def _sync_db_to_repo(repo_root: Path) -> None:
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")


def _flavor_for_ticker(db_path: Path, ticker: str) -> ReportFlavor:
    import sqlite3

    if not db_path.exists():
        return ReportFlavor.PORTFOLIO
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return ReportFlavor.PORTFOLIO
    try:
        row = conn.execute(
            "SELECT list_type FROM tracked_companies WHERE user_id = ? AND UPPER(ticker) = ?",
            (DEFAULT_USER_ID, ticker.upper()),
        ).fetchone()
    except sqlite3.Error:
        return ReportFlavor.PORTFOLIO
    finally:
        conn.close()
    if row is not None and str(row[0]) == "evaluation":
        return ReportFlavor.EVALUATION
    return ReportFlavor.PORTFOLIO


def rerender_workspace(ticker: str, repo_root: Path) -> Path:
    """Rebuild + rewrite today's static workspace HTML — the same call path
    execution/build_artifacts.py uses. ``enable_llm=False`` keeps this a pure
    cache read for every section besides the card itself (already generated
    by the time this runs)."""
    _sync_db_to_repo(repo_root)
    db_path = repo_root / "data" / "portfolio.db"
    flavor = _flavor_for_ticker(db_path, ticker)
    spec = build_report(
        ticker=ticker,
        repo_root=repo_root,
        model_link=f"dcf/{ticker}.xlsx",
        enable_llm=False,
        flavor=flavor,
    )
    out_dir = repo_root / "output" / "research" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace_html_path = out_dir / f"{date.today().isoformat()}_workspace.html"
    workspace_html_path.write_text(render_workspace_html(spec), encoding="utf-8")
    return workspace_html_path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ticker = args.ticker.strip().upper()
    db_path = resolve_db_path(args.db_path)
    if db_path is None:
        _log("halt", reason="DB path not configured")
        return 1
    repo_root = args.repo_root or db_path.parent.parent

    try:
        result = generate_card(db_path, repo_root, ticker)
    except Exception as exc:
        if is_hard_stop(exc):
            _log("hard_stop", ticker=ticker, error=str(exc))
            raise
        _log("failed", ticker=ticker, reason=str(exc))
        return 1

    if result.failure_reason is not None:
        _log(
            "card_generation_failed",
            ticker=ticker,
            reason=result.failure_reason,
            degraded_reasons=list(result.degraded_reasons),
        )
        return 1

    _log(
        "card_generated",
        ticker=ticker,
        artifact_id=result.artifact_id,
        cache_hit=result.cache_hit,
        selection_mode=result.selection_mode,
        degraded_reasons=list(result.degraded_reasons),
    )

    try:
        workspace_path = rerender_workspace(ticker, repo_root)
    except Exception as exc:
        # The card itself is already persisted — a workspace re-render
        # failure is a degraded (stale HTML) outcome, not a generation
        # failure, so it doesn't flip the exit code.
        _log("workspace_rerender_failed", ticker=ticker, error=str(exc))
        print(
            json.dumps(
                {
                    "ticker": ticker,
                    "artifact_id": result.artifact_id,
                    "cache_hit": result.cache_hit,
                    "selection_mode": result.selection_mode,
                    "workspace_rerendered": False,
                }
            )
        )
        return 0

    print(
        json.dumps(
            {
                "ticker": ticker,
                "artifact_id": result.artifact_id,
                "cache_hit": result.cache_hit,
                "selection_mode": result.selection_mode,
                "workspace_rerendered": True,
                "workspace_path": str(workspace_path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
