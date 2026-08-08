"""Build the unified research artifacts for a ticker.

Emits three files under output/research/{TICKER}/{DATE}_*:
  - {DATE}_workspace.html     tabbed workspace report (HTML, primary deliverable;
                              self-contained — opens via file:// or /reports/<ticker>)
  - {DATE}_report.md          markdown source (diff-friendly)
  - {DATE}_sections.json      frontend section payloads (consumed by /api/research/<ticker>)

The DCF workbook is referenced, not re-rendered: the canonical copy lives at
dcf/{TICKER}.xlsx (refreshed by execution/refresh_dcf.py).

Usage:
    python execution/build_artifacts.py --ticker GOOG
    python execution/build_artifacts.py --ticker GOOG --repo-root /abs/path
    python execution/build_artifacts.py --all-tracked

For non-tracked tickers, pass --allow-untracked. The script emits a JSON event
log to stderr (one event per emitted file) and a path summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402  (must precede report imports — we override paths below)
import ticker_settings  # noqa: E402
from compute.key_metrics import (  # noqa: E402
    extract_for_ticker as _extract_key_metrics,
)
from compute.peer_selection import (  # noqa: E402
    extract_for_ticker as _extract_peer_selection,
)
from compute.segment_definitions import (  # noqa: E402
    extract_for_ticker as _extract_segment_definitions,
)
from instrument_store import get_instrument_kind  # noqa: E402
from models.companies import InstrumentType  # noqa: E402
from report.builder import build_report  # noqa: E402
from report.models import ReportFlavor  # noqa: E402
from report.renderers.etf_markdown import render as render_etf_markdown  # noqa: E402
from report.renderers.markdown import render as render_markdown  # noqa: E402
from report.renderers.sections_json import render as render_sections_json  # noqa: E402
from report.renderers.workspace_html import render as render_workspace_html  # noqa: E402
from report.sections import financials as financials_section_mod  # noqa: E402
from report.sections import snapshot as snapshot_section_mod  # noqa: E402
from report.sections.etf_holdings import build_etf_brief  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# Ticker-specific extractors that auto-populate data/ticker_specific/<T>/
# Each entry maps a ticker to a list of (script_name, extra_args) pairs.
# The dispatcher runs them subprocess-isolated before build_report so the
# per-ticker enhancement JSON is fresh when the bear-case prompt reads it
# via _ticker_specific_md. Idempotent: each extractor is responsible for
# skipping when its own freshness check passes.
_TICKER_SPECIFIC_EXTRACTORS: dict[str, list[tuple[str, list[str]]]] = {
    "NVO": [("extract_nvo_patent_timeline.py", [])],
    # Add per-ticker entries here. Keep the script idempotent — the dispatcher
    # will fire on every build for the ticker; cost discipline lives in the
    # script's own freshness check, not here.
}


def _run_ticker_specific_extractors(ticker: str, repo_root: Path) -> None:
    """Subprocess-isolate per-ticker enhancement extractors.

    Failures don't abort the build — they emit a diagnostic event so the
    bear-case prompt falls back to its universal shape without the
    per-ticker context for that ticker. Skipped silently for tickers
    not in _TICKER_SPECIFIC_EXTRACTORS.
    """
    entries = _TICKER_SPECIFIC_EXTRACTORS.get(ticker.upper())
    if not entries:
        return
    for script_name, extra_args in entries:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "execution" / script_name),
            *extra_args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            _emit(
                "ticker_specific_extractor",
                {
                    "ticker": ticker,
                    "script": script_name,
                    "exit_code": proc.returncode,
                    "stderr_tail": (proc.stderr or "")[-300:],
                },
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _emit(
                "ticker_specific_extractor_failed",
                {
                    "ticker": ticker,
                    "script": script_name,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )


def _ensure_segment_definitions(ticker: str, repo_root: Path) -> None:
    """Idempotently populate data/segment_definitions/<T>.json before render.

    Hashes the latest form_10k JSON; if the cache already matches the sha,
    returns in milliseconds. Otherwise fires one Haiku call (~20s) and
    writes the cache. Failures don't abort the build — they emit a
    diagnostic and leave the tooltips empty for this ticker.
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        _emit("segment_defs_skipped", {"ticker": ticker, "reason": "no DB"})
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        result = _extract_segment_definitions(ticker, repo_root, conn)
        _emit(
            "segment_defs_refreshed",
            {
                "ticker": ticker,
                "fiscal_year": result.fiscal_year,
                "definitions_found": sum(1 for v in result.definitions.values() if v),
                "skipped": result.skipped_reason,
            },
        )
    except Exception as e:
        _emit(
            "segment_defs_error",
            {
                "ticker": ticker,
                "error": f"{type(e).__name__}: {e}",
            },
        )
    finally:
        conn.close()


def _ensure_peer_selection(ticker: str, repo_root: Path) -> None:
    """Idempotently populate data/peer_selection/<T>.json before render (LLM
    builds only).

    Keyed on a hash of the LLM inputs (name + business description + segments +
    sector/industry); a fast no-op when unchanged. On a fresh input it fires one
    peer_selection call (~Sonnet) for business-model comparables and a best-effort
    free-tier FMP fetch of each suggestion's fundamentals. Failures don't abort
    the build — the renderer falls back to the FMP sector/cap screen for this
    ticker (directives/peer_selection_llm.md)."""
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        _emit("peer_selection_skipped", {"ticker": ticker, "reason": "no DB"})
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        result = _extract_peer_selection(ticker, repo_root, conn)
        _emit(
            "peer_selection_refreshed",
            {
                "ticker": ticker,
                "suggested": len(result.suggestions),
                "fetched": len(result.fetched_peers),
                "fetched_complete": len(result.fetched_complete),
                "skipped": result.skipped_reason,
            },
        )
    except Exception as e:
        _emit("peer_selection_error", {"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


def _ensure_key_metrics(ticker: str, repo_root: Path) -> None:
    """Idempotently populate data/key_metrics/<T>.json before render (LLM builds
    only).

    Keyed on a hash of the LLM inputs (name + business description + the picker
    catalog vocabulary); a fast no-op when unchanged. On a fresh input it fires
    one key_metrics call (~Sonnet) ranking the metrics MOST important to the
    business over its available extract vocabulary. Failures don't abort the
    build — the DIY picker's preselect row falls back to the deterministic
    tier-graded baseline for this ticker (directives/key_metrics_picker.md)."""
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        _emit("key_metrics_skipped", {"ticker": ticker, "reason": "no DB"})
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        result = _extract_key_metrics(ticker, repo_root, conn)
        _emit(
            "key_metrics_refreshed",
            {
                "ticker": ticker,
                "metrics": len(result.metrics),
                "skipped": result.skipped_reason,
            },
        )
    except Exception as e:
        _emit("key_metrics_error", {"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


def _sync_db_to_repo(repo_root: Path) -> None:
    """Override db module-level paths so scan_and_sync_artifacts hits the right repo.

    The build CLI runs from this checkout but reads/writes against an arbitrary
    --repo-root (typically the parent repo with the real data). The db module
    computes its paths from its own __file__ at import time; we patch them
    here so coverage syncs land in the right portfolio.db.
    """
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print("[]")
        return 0

    flavor = ReportFlavor(args.flavor)
    summary: list[dict[str, object]] = []
    for ticker in tickers:
        result = _build_one(
            ticker,
            repo_root,
            enable_llm=args.enable_llm,
            news_days=args.news_days,
            news_cache_ttl_days=args.news_cache_ttl_days,
            refresh_news=args.refresh_news,
            flavor=flavor,
            trigger=args.trigger,
            force_budget_bypass=args.force_budget_bypass,
            force_refresh=args.force_refresh,
        )
        summary.append(result)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to build for")
    g.add_argument(
        "--all-tracked", action="store_true", help="Build for all rows in tracked_companies"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, transcripts/, micro_thesis/. Default: this repo.",
    )
    parser.add_argument(
        "--allow-untracked",
        action="store_true",
        help="Build even if ticker not in tracked_companies",
    )
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Run the §8 recent-developments and §9 bear-case LLM calls. Routes "
        "through llm_client: Claude CLI first, Gemini Flash fallback if Claude fails. "
        "Without --enable-llm, both sections are stubbed.",
    )
    parser.add_argument(
        "--news-days",
        type=int,
        default=7,
        help="Lookback window (days) for the §8 recent-developments WebSearch. Default 7.",
    )
    parser.add_argument(
        "--news-cache-ttl-days",
        type=int,
        default=7,
        help="How long the §8 news cache stays fresh between regenerations. Default 7.",
    )
    parser.add_argument(
        "--refresh-news",
        action="store_true",
        help="Force a fresh WebSearch for §8 (bypasses the cache for this build).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass the on-disk cache for the IR-anchor LLM sections (bear case §7 + "
        "recent developments §8) so they re-run with the latest fetched documents.",
    )
    parser.add_argument(
        "--flavor",
        choices=[f.value for f in ReportFlavor],
        default=ReportFlavor.PORTFOLIO.value,
        help=(
            "Brief shape. 'portfolio' (default) renders the full Snapshot at §1. "
            "'evaluation' renders an EvaluationSnapshot (3y quick-categorization "
            "data table) at §1 instead — for new-name screening."
        ),
    )
    parser.add_argument(
        "--trigger",
        choices=("earnings", "news_refresh", "manual", "on_demand", "daily_worker"),
        default="manual",
        help=(
            "What triggered this brief build. Logged to brief_provenance_log for audit. "
            "Daily worker passes 'daily_worker'; refresh_news.py passes 'news_refresh'."
        ),
    )
    parser.add_argument(
        "--renderer",
        choices=("default", "workspace", "both"),
        default="workspace",
        help=(
            "Compatibility no-op. The legacy long-form renderer was retired; "
            "every value emits only {DATE}_workspace.html via the tabbed "
            "workspace renderer. 'default'/'both' remain accepted so existing "
            "scripts and scheduled tasks don't crash."
        ),
    )
    parser.add_argument(
        "--force-budget-bypass",
        action="store_true",
        help=(
            "Ignore per-purpose LLM budget caps for this build — the 'run anyway' "
            "override. Analyses a skip-mode cap would forgo are run regardless."
        ),
    )
    return parser.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        ticker = args.ticker.upper()
        if not args.allow_untracked and not _is_tracked(repo_root, ticker):
            _emit(
                "warn_untracked",
                {"ticker": ticker, "hint": "pass --allow-untracked to build anyway"},
            )
        return [ticker]
    return _all_tracked(repo_root)


def _is_tracked(repo_root: Path, ticker: str) -> bool:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return False
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM tracked_companies WHERE ticker = ? LIMIT 1", (ticker,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def _all_tracked(repo_root: Path) -> list[str]:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def _build_one(
    ticker: str,
    repo_root: Path,
    enable_llm: bool,
    news_days: int = 7,
    news_cache_ttl_days: int = 7,
    refresh_news: bool = False,
    flavor: ReportFlavor = ReportFlavor.PORTFOLIO,
    trigger: str = "manual",
    force_budget_bypass: bool = False,
    force_refresh: bool = False,
) -> dict[str, object]:
    out_dir = repo_root / "output" / "research" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    workspace_html_path = out_dir / f"{today}_workspace.html"
    md_path = out_dir / f"{today}_report.md"
    json_path = out_dir / f"{today}_sections.json"
    # The DCF model is the canonical redesigned workbook at dcf/<T>.xlsx (refreshed
    # by execution/refresh_dcf.py). The brief references it rather than re-rendering
    # a per-date copy here; the dashboard serves it via the /dcf/<T> route.
    canonical_dcf = repo_root / "dcf" / f"{ticker}.xlsx"

    _sync_db_to_repo(repo_root)

    # Cross-asset dispatch: ETF tickers get the ETF brief shape (no DCF,
    # no segments, no 10-K narrative). See directives/cross_asset_data_model.md.
    if _resolve_kind(repo_root, ticker) is InstrumentType.ETF:
        return _build_etf(ticker, repo_root, md_path, today)

    # Refresh quarterly_artifacts (audio/transcript/release/slides + step_* flags)
    # from the filesystem before the provenance section reads them. This is what
    # links the coverage matrix to the DB — without this, new .tmp summaries and
    # SayDo files don't land in the table until something else syncs.
    db.scan_and_sync_artifacts(ticker)
    _emit("synced_quarterly_artifacts", {"ticker": ticker})

    # Refresh segment_definitions cache. sha256-keyed on the latest form_10k JSON
    # — only fires a Haiku call when the 10-K source changes; otherwise a fast
    # no-op. Without this step, the segments tab matrix has no tooltips and the
    # 📖 mark never appears (renderer correctly drops the mark when no
    # definition is loaded).
    _ensure_segment_definitions(ticker, repo_root)

    # Per-ticker enhancement extractors (e.g. extract_nvo_patent_timeline.py).
    # Auto-populates data/ticker_specific/<T>/ so the bear-case prompt's
    # ticker_specific block is fresh. Subprocess-isolated; failures land
    # in stderr but don't abort the build.
    _run_ticker_specific_extractors(ticker, repo_root)

    # LLM business-model peer selection (the §4 peer-comp generator). LLM build
    # only — it makes an LLM call + a free-tier FMP fetch; input-sha-keyed so
    # it's a fast no-op when the inputs are unchanged. The renderer degrades to
    # the FMP screen when the cache is absent, so this is purely additive.
    if enable_llm:
        _ensure_peer_selection(ticker, repo_root)
        # LLM key-metrics preselect for the DIY picker (key_metrics_picker.md).
        # Same discipline: LLM build only, input-sha-keyed no-op when unchanged,
        # renderer degrades to the tier-graded baseline when the cache is absent.
        _ensure_key_metrics(ticker, repo_root)

    # Persistent per-ticker override: the dashboard's "always ignore caps" toggle
    # (ticker_settings.bypass_budget) ORs with the one-shot --force-budget-bypass.
    effective_bypass = force_budget_bypass or ticker_settings.get_bypass_budget(
        ticker, db_path=repo_root / "data" / "portfolio.db"
    )
    report_db_path = repo_root / "data" / "portfolio.db"
    report_conn = (
        connect_sqlite(report_db_path, role=SQLiteConnectionRole.READ_ONLY)
        if report_db_path.exists()
        else None
    )
    if report_conn is not None:
        report_conn.row_factory = sqlite3.Row
    try:
        spec = build_report(
            ticker=ticker,
            repo_root=repo_root,
            model_link=f"dcf/{ticker}.xlsx",
            enable_llm=enable_llm,
            news_days=news_days,
            news_cache_ttl_days=news_cache_ttl_days,
            refresh_news=refresh_news,
            flavor=flavor,
            force_budget_bypass=effective_bypass,
            force_refresh=force_refresh,
            conn=report_conn,
        )
        per_metric_provenance = _collect_per_metric_provenance(ticker, repo_root, conn=report_conn)
    finally:
        if report_conn is not None:
            report_conn.close()

    workspace_html_path.write_text(render_workspace_html(spec), encoding="utf-8")
    _emit(
        "wrote_workspace_html",
        {"ticker": ticker, "path": str(workspace_html_path)},
    )

    md_path.write_text(render_markdown(spec), encoding="utf-8")
    _emit("wrote_markdown", {"ticker": ticker, "path": str(md_path)})

    json_path.write_text(render_sections_json(spec), encoding="utf-8")
    _emit("wrote_sections_json", {"ticker": ticker, "path": str(json_path)})

    sections_status = {
        "snapshot": spec.snapshot.status.value,
        "company_description": spec.company_description.status.value,
        "thesis": spec.thesis.status.value,
        "financials": spec.financials.status.value,
        "segments": spec.segments.status.value,
        "earnings": spec.earnings.status.value,
        "saydo": spec.saydo.status.value,
        "ir_docs": spec.ir_docs.status.value,
        "recent_developments": spec.recent_developments.status.value,
        "bear_case": spec.bear_case.status.value,
        "provenance": spec.provenance.status.value,
    }
    # Provenance rows always point at the workspace HTML — the only rendered
    # report — so audit consumers can deep-link.
    canonical_artifact = workspace_html_path
    _log_brief_provenance(
        repo_root=repo_root,
        ticker=ticker,
        generation_date=today,
        sections_status=sections_status,
        per_metric=per_metric_provenance,
        trigger=trigger,
        artifact_path=canonical_artifact,
    )

    return {
        "ticker": ticker,
        "workspace_html": str(workspace_html_path),
        "report_md": str(md_path),
        "sections_json": str(json_path),
        "dcf_xlsx": str(canonical_dcf),
        "section_status": sections_status,
    }


def _collect_per_metric_provenance(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, object]]:
    """Collate per-line-item provenance across the sections wired for it.

    Today: snapshot (DCF valuation) + financials (8 quarterly line items).
    Other sections are expansion territory — sparse merge keeps the dict
    shape stable. Each section's builder returns {} when it has no facts
    to surface, so the unioned dict is naturally sparse for new tickers
    or synthetic test environments.
    """
    merged: dict[str, dict[str, object]] = {}
    merged.update(snapshot_section_mod.build_per_metric(ticker, repo_root, conn=conn))
    merged.update(financials_section_mod.build_per_metric(ticker, repo_root, conn=conn))
    return merged


def _log_brief_provenance(
    *,
    repo_root: Path,
    ticker: str,
    generation_date: str,
    sections_status: dict[str, str],
    trigger: str,
    artifact_path: Path,
    per_metric: dict[str, dict[str, object]] | None = None,
) -> None:
    """Append a `brief_provenance_log` row for the render.

    `sources_used` is a JSON envelope with two keys:

      * `sections` — section-level status snapshot (LIVE / STUB / etc.),
        unchanged from the original §section-level audit.
      * `per_metric` — per-line-item provenance: which fact row drove
        each rendered metric. Sparse: only metrics whose section opted
        into provenance reporting (snapshot + financials today) appear.
        Each value is `{"source": <tier>, "fact_id": <id>,
        "fetched_at": <iso>, ...}` — see
        `load_financial_fact_provenance` / `snapshot.build_per_metric`
        for the exact shape per source.

    Silently skips when the table is missing (synthetic environments
    without migrations applied).
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        table_present = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='brief_provenance_log' LIMIT 1"
        ).fetchone()
        if table_present is None:
            return
        try:
            rel_artifact = str(artifact_path.relative_to(repo_root))
        except ValueError:
            rel_artifact = str(artifact_path)
        sources_used: dict[str, object] = {"sections": sections_status}
        if per_metric:
            sources_used["per_metric"] = per_metric
        conn.execute(
            "INSERT INTO brief_provenance_log "
            "(ticker, generation_date, sources_used, sections_status, "
            " trigger, artifact_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                generation_date,
                json.dumps(sources_used, sort_keys=True),
                json.dumps(sections_status, sort_keys=True),
                trigger,
                rel_artifact,
            ),
        )
        conn.commit()
        _emit(
            "wrote_provenance_log",
            {
                "ticker": ticker,
                "generation_date": generation_date,
                "trigger": trigger,
                "per_metric_count": len(per_metric or {}),
            },
        )
    except sqlite3.Error as exc:
        _emit(
            "provenance_log_failed",
            {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        conn.close()


def _emit(event: str, payload: dict[str, object]) -> None:
    """One JSON line per event to stderr."""
    sys.stderr.write(json.dumps({"event": event, **payload}) + "\n")


def _resolve_kind(repo_root: Path, ticker: str) -> InstrumentType | None:
    """Look up tracked_companies.instrument_type for `ticker`."""
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return None
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        return get_instrument_kind(conn, ticker)
    finally:
        conn.close()


def _build_etf(ticker: str, repo_root: Path, md_path: Path, today: str) -> dict[str, object]:
    """ETF brief build path — markdown only for the MVP.

    HTML / workspace / xlsx renderers are equity-shaped and not adapted yet;
    they'll graduate to multi-kind dispatch in a follow-up. For now the
    markdown brief is the canary deliverable that proves the dispatch works.
    """
    brief = build_etf_brief(ticker, repo_root)
    md_path.write_text(render_etf_markdown(brief), encoding="utf-8")
    _emit("wrote_etf_markdown", {"ticker": ticker, "path": str(md_path)})
    return {
        "ticker": ticker,
        "report_md": str(md_path),
        "instrument_kind": InstrumentType.ETF.value,
        "section_status": {
            "etf_profile": brief.profile.status.value,
            "etf_holdings": brief.holdings.status.value,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
